"""Final verification: the harness checks the resulting code itself.

This runs after every agent run, however the run ended, and never looks at
what the model claimed. The result passes only if every final check passed
and no file outside the task's editable files changed. A check that failed,
timed out or could not run makes the whole result fail and stays visible.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.actions import normalize_path
from harness.config import Task
from harness.sandbox import CheckResult
from harness.workspace import changed_files, diff


@dataclass(frozen=True)
class ScopeResult:
    passed: bool
    changed: list[str]
    outside: list[str]  # changed files the task did not allow


@dataclass(frozen=True)
class VerificationReport:
    checks: list[CheckResult]
    scope: ScopeResult
    diff: str
    changed_files: list[tuple[str, str]]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks) and self.scope.passed


def verify(sandbox, repo: Path, task: Task) -> VerificationReport:
    checks = [sandbox.run_check(name) for name in task.final_checks]

    files = changed_files(repo)
    allowed = {normalize_path(p) for p in task.editable_files}
    outside = [path for _, path in files if normalize_path(path) not in allowed]
    scope = ScopeResult(passed=not outside, changed=[p for _, p in files], outside=outside)

    return VerificationReport(checks=checks, scope=scope, diff=diff(repo), changed_files=files)


# ---- unfinished work ----------------------------------------------------------

def unfinished_work(stop_reason: str | None, stop_detail: str, uncertain: dict | None,
                    checks: list[dict] | None, scope_outside: list[str]) -> list[str]:
    """What is still open, from the harness's own records. Empty when nothing is.

    checks: the final checks as dicts with name, status and a short detail, or
    None when no final verification has run on the current state.
    """
    items = []
    if stop_reason == "cancelled":
        items.append(f"The run was cancelled ({stop_detail or 'on request'}).")
    elif stop_reason and stop_reason != "finished":
        detail = f": {stop_detail}" if stop_detail else ""
        items.append(f"The agent did not finish ({stop_reason}{detail}).")
    elif not stop_reason:
        items.append("The agent loop did not end normally (the process stopped).")
    if uncertain:
        target = f" {uncertain['target']}" if uncertain.get("target") else ""
        where = f"Step {uncertain['step']} " if uncertain.get("step") is not None else ""
        items.append(f"{where}({uncertain['tool']}{target}) was cut off; it is not known "
                     "whether it completed.")
    if checks is None:
        items.append("No final verification has run on the current state "
                     "(python -m harness review <id> runs it).")
    else:
        for check in checks:
            if check["status"] != "passed":
                detail = f": {check['detail']}" if check.get("detail") else ""
                items.append(f"Final check {check['name']} {check['status']}{detail}")
    if scope_outside:
        items.append("Files changed outside the task's allowed changes: "
                     + ", ".join(scope_outside) + ".")
    return items


def check_summaries(report: VerificationReport) -> list[dict]:
    """The final checks of a report as name, status and a one-line detail."""
    summaries = []
    for check in report.checks:
        lines = [line for line in check.output.splitlines() if line.strip()]
        summaries.append({"name": check.name, "status": check.status,
                          "detail": check.error or (lines[-1].strip() if lines else "")})
    return summaries
