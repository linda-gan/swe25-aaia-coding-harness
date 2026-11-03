"""Run every configured check against a repository copy inside the sandbox.

Usage, from the project root:
    python -m scripts.check_sandbox workspace/runs/test-run
    python -m scripts.check_sandbox workspace/runs/test-run --task tasks/invalid_quantity.toml

The task file decides the project (its image and checks) and which
acceptance folder is mounted at /acceptance.
"""
import argparse
import sys
from pathlib import Path

from harness.config import PROJECT_ROOT, load_config, load_task
from harness.projects import select_project
from harness.sandbox import DockerSandbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path, help="repository copy to check")
    parser.add_argument("--task", type=Path, default=PROJECT_ROOT / "tasks" / "invalid_quantity.toml",
                        help="task whose acceptance folder is mounted")
    args = parser.parse_args()

    config = load_config()
    task = load_task(args.task)
    project = select_project(config, task)
    sandbox = DockerSandbox(
        args.repo,
        project.sandbox,
        project.checks,
        acceptance_dir=task.acceptance_dir,
    )
    for name in project.checks:
        result = sandbox.run_check(name)
        print(f"=== {name}: {result.status} "
              f"(exit code {result.exit_code}, {result.duration_seconds:.1f}s)")
        if result.error:
            print(f"Error: {result.error}")
        print(result.output.rstrip())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
