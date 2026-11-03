"""The task record: what was asked, from where, under which rules, and every step.

Written to workspace/runs/<run id>/task.json when a run starts, and updated
after every step, so it is complete at any moment, also when a run is stopped
unexpectedly. Each write goes to a temporary file first and then replaces the
old file in one step (os.replace), so a reader never sees a half-written file.

Contents:
  request           the task description (with its acceptance criteria)
  project           name, URL and pinned commit from the config
  starting_commit   read from the run copy itself and compared with the pin
  allowed_changes   the editable files
  checks            agent checks, final checks, and the exact command of each
  acceptance_tests  the task's acceptance folder (mounted read-only)
  limits            the limits that apply to this run
  steps             one entry per action or finish attempt (see controller._step)
  result            stop reason, fix rounds and verification, added at the end
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from harness.config import LimitsConfig, ProjectConfig, Task
from harness.workspace import RunCopy, WorkspaceError, git

RECORD_NAME = "task.json"


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON so that the file is always either the old or the new version."""
    temp = path.with_name(f".{path.name}.tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class TaskRecord:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def create(cls, run_copy: RunCopy, task: Task, task_path, project: ProjectConfig,
               limits: LimitsConfig) -> "TaskRecord":
        starting_commit = git(run_copy.repo, "rev-parse", "HEAD").strip()
        if starting_commit != project.commit:
            raise WorkspaceError(
                f"The run copy is at {starting_commit[:12]}, not at the pinned commit "
                f"{project.commit[:12]}."
            )
        checks = list(dict.fromkeys([*task.agent_checks, *task.final_checks]))
        data = {
            "run_id": run_copy.run_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "task_file": str(task_path),
            "request": task.description,
            "project": {"name": project.name, "url": project.url, "commit": project.commit},
            "starting_commit": starting_commit,
            "allowed_changes": list(task.editable_files),
            "checks": {
                "agent": list(task.agent_checks),
                "final": list(task.final_checks),
                "commands": {name: project.checks[name] for name in checks},
            },
            "acceptance_tests": task.acceptance_dir.name if task.acceptance_dir else None,
            "limits": asdict(limits),
            "status": "running",
            "steps": [],
            "result": None,
        }
        record = cls(run_copy.run_dir / RECORD_NAME, data)
        record.save()
        return record

    @classmethod
    def load(cls, path: Path) -> "TaskRecord":
        with open(path, encoding="utf-8") as f:
            return cls(Path(path), json.load(f))

    @property
    def steps(self) -> list[dict]:
        return self.data["steps"]

    def add_step(self, step: dict) -> None:
        """Append one step and save at once."""
        self.data["steps"].append(dict(step))
        self.save()

    def finish(self, stop_reason: str, fix_rounds: int, passed: bool | None,
               checks: list[dict] | None = None) -> None:
        self.data["status"] = "stopped"
        self.data["result"] = {
            "stop_reason": stop_reason,
            "fix_rounds": fix_rounds,
            "passed": passed,  # decided by the harness's final checks, never by the model
            "final_checks": checks or [],
        }
        self.save()

    def save(self) -> None:
        write_json_atomic(self.path, self.data)
