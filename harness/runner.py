"""One complete run: agent on a fresh copy, then verification, then a record.

The CLI calls these functions. Tests call them with a scripted model and a
fake sandbox, so the whole flow is tested without Ollama or Docker.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from harness.baseline import BaselineSandbox, capture
from harness.config import HarnessConfig, LimitsConfig, ProjectConfig, Task
from harness.cancellation import (
    CancellableModel, CancellableSandbox, Cancelled, CancelToken, handle_signals,
)
from harness.controller import CANCELLED, AgentController, Event, RunResult
from harness.repo_tools import RepositoryTools
from harness.sandbox import DockerSandbox
from harness.resume import (
    RESUMABLE, ResumeCheck, check_project_and_task, check_resume, close_open_calls, resume_note,
)
from harness.session import RUNNING, VERIFYING, Session, SessionError
from harness.task_record import RECORD_NAME, TaskRecord
from harness.verification import VerificationReport, verify
from harness.workspace import RunCopy, diff

# Checks whose command contains a path under this mount need the task's
# acceptance folder.
ACCEPTANCE_MOUNT = "/acceptance"


@dataclass
class RunReport:
    task_path: Path
    run_copy: RunCopy
    model_name: str
    agent: RunResult
    verification: VerificationReport | None  # None when the run was cancelled
    started_at: str = ""  # local date and time, ISO format
    finished_at: str = ""
    verification_seconds: float = 0.0
    total_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        """Decided only by the harness's own checks, never by the model."""
        return self.verification is not None and self.verification.passed

    @property
    def cancelled(self) -> bool:
        return self.agent.stop_reason == CANCELLED


def check_task(task: Task, project: ProjectConfig) -> None:
    """Fail early, with a clear message, if the task cannot run as written."""
    for name in [*task.agent_checks, *task.final_checks]:
        if name not in project.checks:
            raise ValueError(
                f"The task uses check {name!r}, which is not defined under "
                f"[projects.{project.name}.checks] in config/target.toml. "
                f"Defined: {', '.join(project.checks)}."
            )
        if task.acceptance_dir is None and uses_acceptance_mount(project.checks[name]):
            raise ValueError(
                f"The task uses check {name!r}, which runs acceptance tests, but the "
                "task file names no acceptance folder. Add acceptance_tests = \"<folder>\"."
            )


def effective_limits(task: Task, limits: LimitsConfig) -> LimitsConfig:
    """The config's limits, with the task's own action limit if it sets one."""
    if task.max_actions is None:
        return limits
    if task.max_actions > limits.max_actions_ceiling:
        raise ValueError(
            f"The task sets max_actions = {task.max_actions}, above the ceiling of "
            f"{limits.max_actions_ceiling} in config/target.toml."
        )
    return replace(limits, max_actions=task.max_actions)


def uses_acceptance_mount(argv: list[str]) -> bool:
    return any(part == ACCEPTANCE_MOUNT or part.startswith(ACCEPTANCE_MOUNT + "/")
               for part in argv)


def baseline_names(project: ProjectConfig, task: Task) -> list[str]:
    """The task's checks that are judged against the starting code."""
    used = [*task.agent_checks, *task.final_checks]
    return [name for name in project.baseline_checks if name in used]


def capture_baselines(run_copy: RunCopy, project: ProjectConfig, task: Task,
                      sandbox_factory=DockerSandbox) -> dict:
    """Findings of the baseline checks on the copy as it is now (the starting code)."""
    names = baseline_names(project, task)
    if not names:
        return {}
    return capture(make_sandbox(run_copy, project, task, sandbox_factory), names)


def make_sandbox(run_copy: RunCopy, project: ProjectConfig, task: Task,
                 sandbox_factory=DockerSandbox):
    """A sandbox for this run: the project's image and checks, and only this
    task's acceptance folder."""
    return sandbox_factory(
        run_copy.repo, project.sandbox, project.checks, acceptance_dir=task.acceptance_dir
    )


def run_task(
    task: Task,
    task_path: Path,
    config: HarnessConfig,
    project: ProjectConfig,
    model,
    model_name: str,
    run_copy: RunCopy,
    on_event: Callable[[Event], None] | None = None,
    sandbox_factory=DockerSandbox,
    on_session: Callable[[Session], None] | None = None,
) -> RunReport:
    """A new run on a fresh copy."""
    check_task(task, project)
    limits = effective_limits(task, config.limits)
    # The session takes its lock first, so it shows as running from the start.
    session = Session.create(run_copy, task_path, project, model_name)
    try:
        if on_session:
            on_session(session)
        # Saved before the agent starts, and updated after every step.
        record = TaskRecord.create(run_copy, task, task_path, project, limits)
        # Checks judged against the starting code: run them once now, on the
        # untouched copy, and keep their findings with the task.
        record.set_baselines(capture_baselines(run_copy, project, task, sandbox_factory))
        return _execute(session, record, task, task_path, config, project, limits, model,
                        model_name, run_copy, on_event, sandbox_factory,
                        start=lambda controller: controller.run())
    finally:
        # Also after an error or a forced stop: the lock is released. A session
        # that was not saved as finished or cancelled then shows as interrupted,
        # with any pending action marked uncertain.
        session.close()


@dataclass
class ResumeOutcome:
    check: ResumeCheck
    report: RunReport | None  # None when the session was paused instead
    session_id: str


def resume_task(
    session: Session,
    config: HarnessConfig,
    model,
    model_name: str,
    accept_current_state: bool = False,
    on_event: Callable[[Event], None] | None = None,
    sandbox_factory=DockerSandbox,
    on_check: Callable[[ResumeCheck], None] | None = None,
) -> ResumeOutcome:
    """Continue a saved session from its last checkpoint, if that is safe.

    Everything is checked again first (see harness/resume.py). On any problem
    not explicitly accepted, the session is paused with the reasons, and
    nothing runs.
    """
    check = check_resume(session, config)
    if on_check:
        on_check(check)
    if not check.can_resume(accept_current_state):
        if session.status in RESUMABLE:
            session.pause(check.blocking + check.decisions)
        return ResumeOutcome(check, None, session.session_id)

    task, project, limits = check.task, check.project, check.limits
    data = session.data
    run_dir = session.run_dir
    run_copy = RunCopy(run_dir.name, run_dir, Path(data["repo"]))
    uncertain = session.uncertain_action
    note = resume_note(session, check, accepted=accept_current_state)
    accepted = (check.decisions if accept_current_state else [])
    session.reopen(accepted, model_name)
    try:
        record = TaskRecord.load(run_dir / RECORD_NAME)
        record.reopen()
        saved = RunResult(
            stop_reason=data["stop_reason"] or "",
            actions=data["counters"]["actions"], denied=data["counters"]["denied"],
            retries=data["counters"]["retries"], fix_rounds=data["counters"]["fix_rounds"],
            steps=data["counters"]["steps"],
            messages=list(data["messages"]), checks=list(data["checks"]),
        )
        # Every call of the model's last reply gets a result, then the harness's note.
        saved.messages += close_open_calls(saved.messages, uncertain)
        saved.messages.append({"role": "user", "content": note})
        loop_done = saved.stop_reason not in ("", CANCELLED)  # only verification was left
        context_state = data["context"]

        def start(controller: AgentController) -> RunResult:
            if loop_done:
                return saved  # e.g. cancelled during final verification: verify again
            return controller.resume(saved, context_state)

        report = _execute(session, record, task, data["task_file"], config, project, limits,
                          model, model_name, run_copy, on_event, sandbox_factory, start=start)
        return ResumeOutcome(check, report, session.session_id)
    finally:
        session.close()


def review_session(session: Session, config: HarnessConfig,
                   sandbox_factory=DockerSandbox) -> VerificationReport:
    """Run the final checks again on a session's working copy, and log the review.

    For a session that is not running: e.g. to see where a cancelled or
    interrupted run stands, or to check a finished one again. Nothing is
    changed; the session keeps its status. The project and task must still be
    as they were when the run started (the same rules as for resume), so the
    checks are the ones the run was started with.
    """
    status = session.status
    if status in (RUNNING, VERIFYING):
        raise SessionError(f"Session {session.session_id} is {status}; wait for it or cancel it.")
    record = session.task_record()
    if record is None:
        raise SessionError("The session has no task record (task.json).")
    check = ResumeCheck()
    if not check_project_and_task(record.data, config, check) or check.blocking:
        raise SessionError("Cannot review: " + " ".join(check.blocking))
    run_dir = session.run_dir
    run_copy = RunCopy(run_dir.name, run_dir, Path(session.data["repo"]))
    token = CancelToken()
    with session.hold(), handle_signals(token):  # no resume can start meanwhile
        sandbox = CancellableSandbox(
            BaselineSandbox(make_sandbox(run_copy, check.project, check.task, sandbox_factory),
                            record.data.get("baselines", {})),
            token)
        verification = verify(sandbox, run_copy.repo, check.task)
        session.add_review(verification)
    return verification


def _execute(session, record, task, task_path, config, project, limits, model, model_name,
             run_copy, on_event, sandbox_factory, start) -> RunReport:
    """The agent loop (new or continued), then verification, then the records."""
    # Ctrl+C and `cancel` (SIGTERM) become a cancel request while the run goes.
    token = CancelToken()
    with handle_signals(token):
        started_at = datetime.now().isoformat(timespec="seconds")
        begin = time.monotonic()
        sandbox = CancellableSandbox(
            BaselineSandbox(make_sandbox(run_copy, project, task, sandbox_factory),
                            record.data.get("baselines", {})),
            token)
        controller = AgentController(
            model=CancellableModel(model, token),
            tools=RepositoryTools(run_copy.repo),
            sandbox=sandbox,
            task=task,
            limits=limits,
            on_event=on_event,
            context=config.context,
            on_step=record.add_step,
            on_pending=session.pending,
            on_checkpoint=session.checkpoint,
            cancel=token,
        )
        agent = start(controller)
        verification = None
        verification_start = time.monotonic()
        if agent.stop_reason == CANCELLED:
            session.cancelled(token.reason or agent.stop_detail, agent.interrupted_action)
        else:
            # Verification always runs, whatever other reason the agent stopped for.
            session.verifying()
            try:
                verification = verify(sandbox, run_copy.repo, task)
            except Cancelled as exc:
                agent.stop_reason, agent.stop_detail = CANCELLED, exc.reason
                session.cancelled(exc.reason, {"step": None, "action": None,
                                               "tool": "final verification",
                                               "target": ", ".join(task.final_checks)})
        end = time.monotonic()
        report = RunReport(
            task_path, run_copy, model_name, agent, verification,
            started_at=started_at,
            finished_at=datetime.now().isoformat(timespec="seconds"),
            verification_seconds=(round(end - verification_start, 1)
                                  if verification is not None else 0.0),
            total_seconds=round(end - begin, 1),
        )
        save_record(report, project)
        final = ([{"name": c.name, "status": c.status} for c in verification.checks]
                 + [{"name": "scope", "status": "passed" if verification.scope.passed
                     else "failed"}]) if verification is not None else []
        record.finish(agent.stop_reason, agent.fix_rounds,
                      report.passed if verification is not None else None, final)
        if verification is not None:
            session.finish(verification)
        return report


def save_record(report: RunReport, project: ProjectConfig) -> Path:
    """Write run.json and changes.diff next to the repository copy."""
    agent, verification = report.agent, report.verification
    record = {
        "run_id": report.run_copy.run_id,
        "task_file": str(report.task_path),
        "project": {"name": project.name, "url": project.url, "commit": project.commit},
        "model": report.model_name,
        "passed": report.passed,
        "timing": {
            "started_at": report.started_at,
            "finished_at": report.finished_at,
            "agent_seconds": agent.duration_seconds,
            "verification_seconds": report.verification_seconds,
            "total_seconds": report.total_seconds,
        },
        "agent": {
            "stop_reason": agent.stop_reason,
            "summary": agent.summary,
            "actions": agent.actions,
            "denied": agent.denied,
            "retries": agent.retries,
            "fix_rounds": agent.fix_rounds,
            "steps": agent.steps,
            "context": agent.context,
            "events": [asdict(e) for e in agent.events],
            "messages": agent.messages,
        },
        "verification": None if verification is None else {
            "checks": [
                {"name": c.name, "status": c.status, "exit_code": c.exit_code,
                 "duration_seconds": round(c.duration_seconds, 2),
                 "error": c.error, "output": c.output}
                for c in verification.checks
            ],
            "scope": asdict(verification.scope),
            "changed_files": verification.changed_files,
        },
    }
    path = report.run_copy.run_dir / "run.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    # After a cancel there is no verification; the diff is read from the copy (Git, read-only).
    changes = verification.diff if verification is not None else diff(report.run_copy.repo)
    (report.run_copy.run_dir / "changes.diff").write_text(changes, encoding="utf-8")
    return path
