"""Checks judged against the starting code: only new findings count.

Some checks report findings that were already there before the agent began.
Cosmic Python's starting code has 7 pyflakes warnings (unused imports), so a
plain lint check would fail for every solution, also a perfect one. For the
checks listed in a project's baseline_checks:

1. At the start of a run, the harness runs the check once on the fresh copy
   and saves its findings in the task record: the baseline.
2. Every later run of the check is compared with that baseline. A finding is
   its file and message, without the line number, because edits move lines.
   The check passes if no finding is new (more often than in the baseline),
   and it says so on its last line: "7 before, 7 now, 0 new". New findings are
   listed first, so the model and the user see them at once.

A check that could not run stays unavailable, and without a baseline the
comparison is unavailable too: never passed by default.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace

from harness.sandbox import CheckResult

# "src/allocation/domain/model.py:5:1: '.commands' imported but unused"
FINDING = re.compile(r"^(?P<path>[^\s:][^:]*):(?P<line>\d+):(?:\d+:)?\s*(?P<message>.+)$")


def findings(output: str) -> list[str]:
    """Findings as "path: message", in order; lines that are not findings are skipped."""
    found = []
    for line in output.splitlines():
        match = FINDING.match(line.strip())
        if match:
            found.append(f"{match['path']}: {match['message'].strip()}")
    return found


def capture(sandbox, names) -> dict[str, list[str] | None]:
    """Run each check once on the starting code. None if it could not run."""
    baselines = {}
    for name in names:
        result = sandbox.run_check(name)
        baselines[name] = None if (result.error or result.timed_out) else findings(result.output)
    return baselines


def compare(result: CheckResult, baseline: list[str] | None) -> CheckResult:
    """The check's result judged against the baseline."""
    if result.error or result.timed_out:
        return result  # could not run, or too slow: stays visible as it is
    if baseline is None:
        return replace(result, exit_code=None, error=(
            "No baseline from the starting code: the check could not run when the run "
            "started, so new findings cannot be told apart."))
    now = findings(result.output)
    new = list((Counter(now) - Counter(baseline)).elements())
    lines = []
    if new:  # first, so the model and the user see them at once
        lines.append("New findings:")
        lines.extend(f"  {finding}" for finding in new)
    lines.append("Full output:")
    lines.append(result.output.rstrip() or "(none)")
    # Last, because the interface shows a check's last line as its summary.
    lines.append(f"{len(baseline)} before, {len(now)} now, {len(new)} new "
                 "(compared with the starting code)")
    return replace(result, exit_code=0 if not new else 1, output="\n".join(lines))


class BaselineSandbox:
    """Wraps a sandbox: checks with a baseline are compared, all others pass through."""

    def __init__(self, sandbox, baselines: dict[str, list[str] | None]):
        self.sandbox = sandbox
        self.baselines = baselines

    def run_check(self, name):
        result = self.sandbox.run_check(name)
        if name in self.baselines:
            return compare(result, self.baselines[name])
        return result
