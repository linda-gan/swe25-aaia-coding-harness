"""Sessions: a run's saved state, so it can be shown, reopened and later resumed.

A session is a run folder (workspace/runs/<id>/). Next to the task record
(task.json) it holds session.json, the checkpoint:

  status            running, verifying, passed or failed (as saved)
  counters          actions, refused actions, retries, fix rounds, steps:
                    the limits already used
  pending_action    the step that has started but not finished, or null
  context           the ContextManager state (marker code, shortened results)
  messages          the full conversation
  events, checks, errors
  working_copy      a fingerprint: starting commit plus a SHA-256 hash of
                    every changed file, including the harness's own edits
  verification      the final check results, once the run is over

The checkpoint is written after every completed step, and pending_action just
before each action or finish check starts. Every write replaces the file in
one step, so it is never half-written.

Is a session still running? While a run is active, its process holds an
exclusive lock (flock) on session.lock in the run folder. The operating system
releases the lock when the process ends in any way, even when it is killed.
So a session saved as running or verifying whose lock is free belongs to a
process that is gone: it is shown as interrupted, and an action that was
pending at that moment is marked as uncertain, because nobody knows whether it
completed.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from harness.config import ProjectConfig
from harness.task_record import RECORD_NAME, TaskRecord, write_json_atomic
from harness.workspace import RUNS_DIR, RunCopy, changed_files, git

SESSION_FILE = "session.json"
LOCK_FILE = "session.lock"
SESSION_ID = re.compile(r"[A-Za-z0-9-]+")

# Saved statuses
RUNNING = "running"  # the agent loop is going
VERIFYING = "verifying"  # the harness is running the final checks
PASSED = "passed"  # over: all final checks passed
FAILED = "failed"  # over: at least one final check failed or could not run
# Shown (never saved) for a running or verifying session whose process is gone
INTERRUPTED = "interrupted"


class SessionError(Exception):
    """A session could not be found or read."""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def fingerprint(repo: Path) -> dict:
    """The working copy's state: HEAD plus a hash of every changed file (None if deleted)."""
    files = {}
    for status, path in changed_files(repo):
        target = repo / path
        files[path] = (hashlib.sha256(target.read_bytes()).hexdigest()
                       if target.is_file() else None)
    return {"head": git(repo, "rev-parse", "HEAD").strip(), "files": files}


class Session:
    def __init__(self, run_dir: Path, data: dict, lock_fd: int | None = None):
        self.run_dir = Path(run_dir)
        self.data = data
        self._lock_fd = lock_fd  # set while this process owns the session

    # ---- creating and loading ------------------------------------------------

    @classmethod
    def create(cls, run_copy: RunCopy, task_path, project: ProjectConfig,
               model_name: str) -> "Session":
        lock_fd = _try_lock(run_copy.run_dir / LOCK_FILE, create=True)
        if lock_fd is None:
            raise SessionError(f"Session {run_copy.run_id} is already in use.")
        data = {
            "session_id": run_copy.run_id,
            "created_at": now(),
            "updated_at": now(),
            "status": RUNNING,
            "pid": os.getpid(),
            "task_file": str(task_path),
            "project": project.name,
            "model": model_name,
            "repo": str(run_copy.repo),
            "stop_reason": None,
            "stop_detail": "",
            "counters": {"actions": 0, "denied": 0, "retries": 0, "fix_rounds": 0, "steps": 0},
            "pending_action": None,
            "context": None,
            "messages": [],
            "events": [],
            "checks": [],
            "errors": [],
            "working_copy": fingerprint(run_copy.repo),
            "verification": None,
        }
        session = cls(run_copy.run_dir, data, lock_fd)
        session.save()
        return session

    @classmethod
    def load(cls, run_dir: Path) -> "Session":
        path = Path(run_dir) / SESSION_FILE
        try:
            with open(path, encoding="utf-8") as f:
                return cls(Path(run_dir), json.load(f))
        except FileNotFoundError:
            raise SessionError(f"No session data in {run_dir}.") from None
        except json.JSONDecodeError as exc:
            raise SessionError(f"The session file {path} cannot be read: {exc}") from None

    def task_record(self) -> TaskRecord | None:
        path = self.run_dir / RECORD_NAME
        return TaskRecord.load(path) if path.is_file() else None

    # ---- saving progress -------------------------------------------------------

    def pending(self, info: dict) -> None:
        """Save, before it starts, the step that is about to run."""
        self.data["pending_action"] = {**info, "started_at": now()}
        self.save()

    def checkpoint(self, run, context_state: dict) -> None:
        """Save the run's state after a completed step. Clears the pending action."""
        self.data.update({
            "updated_at": now(),
            "stop_reason": run.stop_reason or None,
            "stop_detail": run.stop_detail,
            "counters": {"actions": run.actions, "denied": run.denied, "retries": run.retries,
                         "fix_rounds": run.fix_rounds, "steps": run.steps},
            "pending_action": None,
            "context": context_state,
            "messages": run.messages,
            "events": [asdict(e) for e in run.events],
            "checks": run.checks,
            "errors": _errors(run),
            "working_copy": fingerprint(Path(self.data["repo"])),
        })
        self.save()

    def verifying(self) -> None:
        self.data.update({"status": VERIFYING, "updated_at": now()})
        self.save()

    def finish(self, verification) -> None:
        """Save the final check results and release the session."""
        checks = [{"name": c.name, "status": c.status, "exit_code": c.exit_code,
                   "error": c.error, "output_tail": c.output[-1500:]}
                  for c in verification.checks]
        self.data.update({
            "status": PASSED if verification.passed else FAILED,
            "updated_at": now(),
            "verification": {
                "passed": verification.passed,
                "checks": checks,
                "scope": {"passed": verification.scope.passed, "outside": verification.scope.outside},
                "changed_files": verification.changed_files,
            },
        })
        self.save()
        self.close()

    def save(self) -> None:
        write_json_atomic(self.run_dir / SESSION_FILE, self.data)

    def close(self) -> None:
        """Release the lock. The session file stays as it is."""
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    # ---- what the session looks like now --------------------------------------

    @property
    def session_id(self) -> str:
        return self.data["session_id"]

    def is_active(self) -> bool:
        """True while some process holds the session's lock."""
        if self._lock_fd is not None:
            return True
        fd = _try_lock(self.run_dir / LOCK_FILE, create=False)
        if fd is None:
            return (self.run_dir / LOCK_FILE).exists()  # exists but locked: active
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        return False

    @property
    def status(self) -> str:
        saved = self.data["status"]
        if saved in (RUNNING, VERIFYING) and not self.is_active():
            return INTERRUPTED
        return saved

    @property
    def uncertain_action(self) -> dict | None:
        """The step that was running when the process ended, if any."""
        if self.status == INTERRUPTED:
            return self.data.get("pending_action")
        return None

    def progress(self) -> dict[str, int]:
        """Steps per phase, in the order the phases first appeared."""
        record = self.task_record()
        steps = record.steps if record else []
        return dict(Counter(step["phase"] for step in steps))


class SessionStore:
    """All sessions under one runs folder."""

    def __init__(self, runs_dir: Path = RUNS_DIR):
        self.runs_dir = Path(runs_dir)

    def all(self) -> list[Session]:
        """Every readable session, oldest first. Run folders without one are skipped."""
        if not self.runs_dir.is_dir():
            return []
        sessions = []
        for folder in sorted(self.runs_dir.iterdir()):
            if (folder / SESSION_FILE).is_file():
                try:
                    sessions.append(Session.load(folder))
                except SessionError:
                    continue
        return sessions

    def resolve(self, ref: str) -> Path:
        """A session folder from its full ID, or a unique start or end of it."""
        if not isinstance(ref, str) or not SESSION_ID.fullmatch(ref):
            raise SessionError(f"{ref!r} is not a session ID (letters, digits and '-' only).")
        ids = [s.session_id for s in self.all()]
        if ref in ids:
            return self.runs_dir / ref
        matches = [i for i in ids if i.startswith(ref) or i.endswith(ref)]
        if not matches:
            raise SessionError(f"No session matches {ref!r}. See: python -m harness sessions")
        if len(matches) > 1:
            raise SessionError(f"{ref!r} matches several sessions: {', '.join(matches)}. "
                               "Give more of the ID.")
        return self.runs_dir / matches[0]

    def open(self, ref: str) -> Session:
        return Session.load(self.resolve(ref))


# ---- helpers ----------------------------------------------------------------

def _try_lock(path: Path, create: bool) -> int | None:
    """An open, exclusively locked file descriptor, or None if another holds the lock."""
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    try:
        fd = os.open(path, flags, 0o644)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _errors(run) -> list[dict]:
    """Refused or failed steps with their message, and a stop caused by an error."""
    errors = []
    for message in run.messages:
        step = message.get("harness_step") or {}
        content = message.get("content", "")
        if message.get("role") == "tool" and content.startswith("Error:"):
            errors.append({"step": step.get("step"), "tool": step.get("tool"),
                           "message": content[:500]})
    if run.stop_reason in ("model error", "internal error"):
        errors.append({"step": None, "tool": "harness",
                       "message": f"{run.stop_reason}: {run.stop_detail}"[:500]})
    return errors
