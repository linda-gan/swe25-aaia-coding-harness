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
