"""One complete run: agent on a fresh copy, then verification, then a record.

The CLI calls these functions. Tests call them with a scripted model and a
fake sandbox, so the whole flow is tested without Ollama or Docker.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from harness.config import PROJECT_ROOT, HarnessConfig, Task
from harness.controller import AgentController, Event, RunResult
from harness.repo_tools import RepositoryTools
from harness.sandbox import DockerSandbox
from harness.verification import VerificationReport, verify
from harness.workspace import RunCopy

ACCEPTANCE_DIR = PROJECT_ROOT / "acceptance"


@dataclass
class RunReport:
    task_path: Path
    run_copy: RunCopy
    model_name: str
    agent: RunResult
    verification: VerificationReport
    started_at: str = ""  # local date and time, ISO format
    finished_at: str = ""
    verification_seconds: float = 0.0
    total_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        """Decided only by the harness's own checks, never by the model."""
        return self.verification.passed


def check_task(task: Task, config: HarnessConfig) -> None:
    """Fail early, with a clear message, if the task names unknown checks."""
    for name in [*task.agent_checks, *task.final_checks]:
        if name not in config.checks:
            raise ValueError(
                f"The task uses check {name!r}, which is not defined under [checks] "
                f"in config/target.toml. Defined: {', '.join(config.checks)}."
            )


def make_sandbox(run_copy: RunCopy, config: HarnessConfig, sandbox_factory=DockerSandbox):
    return sandbox_factory(
        run_copy.repo, config.sandbox, config.checks, acceptance_dir=ACCEPTANCE_DIR
    )


def run_task(
    task: Task,
    task_path: Path,
    config: HarnessConfig,
    model,
    model_name: str,
    run_copy: RunCopy,
    on_event: Callable[[Event], None] | None = None,
    sandbox_factory=DockerSandbox,
) -> RunReport:
    check_task(task, config)
    started_at = datetime.now().isoformat(timespec="seconds")
    start = time.monotonic()
    sandbox = make_sandbox(run_copy, config, sandbox_factory)
    controller = AgentController(
        model=model,
        tools=RepositoryTools(run_copy.repo),
        sandbox=sandbox,
        task=task,
        limits=config.limits,
        on_event=on_event,
    )
    agent = controller.run()
    # Verification always runs, whatever the reason the agent stopped.
    verification_start = time.monotonic()
    verification = verify(sandbox, run_copy.repo, task)
    end = time.monotonic()
    report = RunReport(
        task_path, run_copy, model_name, agent, verification,
        started_at=started_at,
        finished_at=datetime.now().isoformat(timespec="seconds"),
        verification_seconds=round(end - verification_start, 1),
        total_seconds=round(end - start, 1),
    )
    save_record(report, config)
    return report


def save_record(report: RunReport, config: HarnessConfig) -> Path:
    """Write run.json and changes.diff next to the repository copy."""
    agent, verification = report.agent, report.verification
    record = {
        "run_id": report.run_copy.run_id,
        "task_file": str(report.task_path),
        "target": {"url": config.target.url, "commit": config.target.commit},
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
            "events": [asdict(e) for e in agent.events],
            "messages": agent.messages,
        },
        "verification": {
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
    (report.run_copy.run_dir / "changes.diff").write_text(verification.diff, encoding="utf-8")
    return path
