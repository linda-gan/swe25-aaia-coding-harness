"""Show that a task's acceptance check is sound, without a model.

Runs the task's final checks twice on one fresh run copy, inside the sandbox:
  1. on the unchanged starting code: the acceptance check must fail,
  2. after applying the reference solution: every final check must pass.

The reference solution is a patch written by hand, kept in reference/ (outside
the run copy and outside the acceptance folder, so neither the agent nor the
code under test can see it). It only proves the checks can pass; the agent
never gets it.

Usage, from the project root:
    python -m scripts.check_reference tasks/batch_expiry.toml
    python -m scripts.check_reference tasks/batch_expiry.toml --patch reference/other.patch

Exit code 0 when both expectations hold, 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from harness.cli import Style, format_verification, heading
from harness.config import PROJECT_ROOT, HarnessConfig, load_config, load_task
from harness.projects import select_project
from harness.baseline import BaselineSandbox
from harness.runner import capture_baselines, check_task, make_sandbox
from harness.sandbox import DockerSandbox
from harness.verification import VerificationReport, verify
from harness.workspace import RunCopy, apply_patch, create_project_copy

REFERENCE_DIR = PROJECT_ROOT / "reference"


@dataclass
class ReferenceResult:
    run_copy: RunCopy
    before: VerificationReport  # starting code
    after: VerificationReport  # with the reference solution

    @property
    def acceptance_failed_before(self) -> bool:
        return any(c.name == "acceptance" and c.status == "failed" for c in self.before.checks)

    @property
    def ok(self) -> bool:
        return self.acceptance_failed_before and self.after.passed


def default_patch(task_path: Path) -> Path:
    """tasks/batch_expiry.toml -> reference/batch_expiry.patch"""
    return REFERENCE_DIR / f"{Path(task_path).stem}.patch"


def check_reference(task_path: Path, patch: Path, config: HarnessConfig,
                    run_copy: RunCopy, sandbox_factory=DockerSandbox) -> ReferenceResult:
    task = load_task(task_path)
    project = select_project(config, task)
    check_task(task, project)
    baselines = capture_baselines(run_copy, project, task, sandbox_factory)
    sandbox = BaselineSandbox(make_sandbox(run_copy, project, task, sandbox_factory), baselines)
    before = verify(sandbox, run_copy.repo, task)
    apply_patch(run_copy.repo, patch)
    after = verify(sandbox, run_copy.repo, task)
    return ReferenceResult(run_copy, before, after)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", type=Path, help="task file, e.g. tasks/batch_expiry.toml")
    parser.add_argument("--patch", type=Path, help="reference patch (default: reference/<task>.patch)")
    args = parser.parse_args(argv)

    style = Style()
    config = load_config()
    patch = args.patch or default_patch(args.task)
    run_copy = create_project_copy(select_project(config, load_task(args.task)))
    print(style.bold("Reference check: are the acceptance checks sound?"))
    print(f"  Task:  {args.task}")
    print(f"  Patch: {patch}")
    print(f"  Copy:  {run_copy.repo}")

    result = check_reference(args.task, patch, config, run_copy)

    print(heading(style, "1. Starting code (acceptance must fail)"))
    print(format_verification(style, result.before))
    print(heading(style, "2. With the reference solution (everything must pass)"))
    print(format_verification(style, result.after))
    print()
    if result.ok:
        print(style.bold(style.green("OK - acceptance fails on the starting code and "
                                     "all final checks pass with the reference solution.")))
        return 0
    if not result.acceptance_failed_before:
        print(style.bold(style.red("PROBLEM - the acceptance check did not fail on the starting code.")))
    if not result.after.passed:
        print(style.bold(style.red("PROBLEM - not every final check passed with the reference solution.")))
    return 1


if __name__ == "__main__":
    sys.exit(main())
