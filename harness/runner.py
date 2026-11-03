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

from harness.config import HarnessConfig, LimitsConfig, ProjectConfig, Task
from harness.cancellation import (
    CancellableModel, CancellableSandbox, Cancelled, CancelToken, handle_signals,
)
from harness.controller import CANCELLED, AgentController, Event, RunResult
from harness.repo_tools import RepositoryTools
from harness.sandbox import DockerSandbox
from harness.session import Session
from harness.task_record import TaskRecord
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
    check_task(task, project)
    limits = effective_limits(task, config.limits)
    # The session takes its lock first, so it shows as running from the start.
    session = Session.create(run_copy, task_path, project, model_name)
    # Ctrl+C and `cancel` (SIGTERM) become a cancel request while the run goes.
    token = CancelToken()
    try:
        with handle_signals(token):
            if on_session:
                on_session(session)
            # Saved before the agent starts, and updated after every step.
            record = TaskRecord.create(run_copy, task, task_path, project, limits)
            started_at = datetime.now().isoformat(timespec="seconds")
            start = time.monotonic()
            sandbox = CancellableSandbox(make_sandbox(run_copy, project, task, sandbox_factory),
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
            agent = controller.run()
            verification = None
            if agent.stop_reason == CANCELLED:
                session.cancelled(token.reason or agent.stop_detail, agent.interrupted_action)
            else:
                # Verification always runs, whatever other reason the agent stopped for.
                session.verifying()
                verification_start = time.monotonic()
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
                total_seconds=round(end - start, 1),
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
    finally:
        # Also after an error or a forced stop: the lock is released. A session
        # that was not saved as finished or cancelled then shows as interrupted,
        # with any pending action marked uncertain.
        session.close()


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
