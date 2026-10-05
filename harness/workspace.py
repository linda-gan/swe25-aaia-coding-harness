"""Disposable run copies of the target repository, and Git queries on them.

workspace/target-pristine   clean clone at the pinned commit, never edited
workspace/runs/<run id>/    one folder per run
    repo/                   the copy the agent works on
    run.json                everything that happened, written after the run
    changes.diff            the resulting diff
"""
from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from harness.config import PROJECT_ROOT

WORKSPACE = PROJECT_ROOT / "workspace"
PRISTINE_DIR = WORKSPACE / "target-pristine"
RUNS_DIR = WORKSPACE / "runs"

# Not copied into a run: local environments and caches from the pristine copy.
COPY_IGNORE = shutil.ignore_patterns(
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", "*.egg-info"
)

# Git is run on the host, on a folder the agent's code has touched. These
# settings stop Git from running programs configured inside a repository.
# (The sandbox also mounts .git read-only, so its config and hooks cannot be
# changed in the first place.)
SAFE_GIT = ["--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]


class WorkspaceError(Exception):
    """The run copy could not be prepared or inspected."""


@dataclass(frozen=True)
class RunCopy:
    run_id: str
    run_dir: Path  # workspace/runs/<run id>
    repo: Path  # workspace/runs/<run id>/repo


def git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *SAFE_GIT, "-C", str(repo), *args],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        raise WorkspaceError("Git is not installed.") from None
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def create_run_copy(
    expected_commit: str,
    pristine: Path = PRISTINE_DIR,
    runs_dir: Path = RUNS_DIR,
) -> RunCopy:
    """Copy the pristine clone into a new run folder, after checking it is untouched."""
    if not (pristine / ".git").is_dir():
        raise WorkspaceError(
            f"No pristine copy at {pristine}. Run ./scripts/fetch_target.sh first."
        )
    head = git(pristine, "rev-parse", "HEAD").strip()
    if head != expected_commit:
        raise WorkspaceError(
            f"The pristine copy is at commit {head[:12]}, but the config expects "
            f"{expected_commit[:12]}. Run ./scripts/fetch_target.sh again."
        )
    if git(pristine, "status", "--porcelain", "--untracked-files=no").strip():
        raise WorkspaceError(
            "The pristine copy has local changes. Run ./scripts/fetch_target.sh again."
        )

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    repo = run_dir / "repo"
    shutil.copytree(pristine, repo, symlinks=True, ignore=COPY_IGNORE)
    return RunCopy(run_id=run_id, run_dir=run_dir, repo=repo)


def diff(repo: Path) -> str:
    """Changes to tracked files, compared with the starting commit."""
    # --no-ext-diff and --no-textconv: never run diff helper programs.
    return git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color")


def changed_files(repo: Path) -> list[tuple[str, str]]:
    """(status, path) for every modified, deleted or new file. ?? means new."""
    output = git(repo, "status", "--porcelain", "--untracked-files=all")
    return [(line[:2].strip(), line[3:]) for line in output.splitlines() if line.strip()]
