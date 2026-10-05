"""Run every configured check against a repository copy inside the sandbox.

Usage, from the project root:
    python -m scripts.check_sandbox workspace/runs/test-run
"""
import sys
from pathlib import Path

from harness.config import PROJECT_ROOT, load_config
from harness.sandbox import DockerSandbox


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    config = load_config()
    sandbox = DockerSandbox(
        Path(sys.argv[1]),
        config.sandbox,
        config.checks,
        acceptance_dir=PROJECT_ROOT / "acceptance",
    )
    for name in config.checks:
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
