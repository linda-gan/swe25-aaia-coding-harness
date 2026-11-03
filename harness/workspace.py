"""Pristine clones of the allowed projects, disposable run copies, and Git queries.

workspace/target-pristine   clean clone of a project at its pinned commit, never
                            edited (the folder is set per project in the config)
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

from harness.config import WORKSPACE_DIR, ProjectConfig

WORKSPACE = WORKSPACE_DIR
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


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            ["git", *SAFE_GIT, "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise WorkspaceError("Git is not installed.") from None
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"git {args[0] if args else ''} did not finish within {timeout}s.") from None
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


# ---- pristine clones ------------------------------------------------------

def check_pristine(expected_commit: str, pristine: Path, fix: str = "") -> None:
    """Refuse a pristine clone that is missing, at another commit, or changed.

    fix is appended to each message, e.g. "Run: python -m harness fetch cosmicpython".
    """
    hint = f" {fix}" if fix else ""
    if not (pristine / ".git").is_dir():
        raise WorkspaceError(f"No pristine copy at {pristine}.{hint}")
    head = git(pristine, "rev-parse", "HEAD").strip()
    if head != expected_commit:
        raise WorkspaceError(
            f"The pristine copy is at commit {head[:12]}, but the config expects "
            f"{expected_commit[:12]}.{hint}"
        )
    if git(pristine, "status", "--porcelain", "--untracked-files=no").strip():
        raise WorkspaceError(f"The pristine copy has local changes.{hint}")


def fetch_hint(project: ProjectConfig) -> str:
    return f"Run: python -m harness fetch {project.name}"


def fetch_project(project: ProjectConfig, force: bool = False) -> bool:
    """Make sure the project's pristine clone exists at the pinned commit.

    Returns False if a valid clone was already there (it is left untouched),
    True if a new clone was made. A missing, outdated or changed clone is
    replaced; force=True replaces a valid one too. The new clone is made in a
    temporary folder first, so a failed fetch never leaves a half-made clone.
    """
    pristine = project.pristine  # validated by the config: inside workspace/
    if not force:
        try:
            check_pristine(project.commit, pristine)
            return False
        except WorkspaceError:
            pass  # missing or not usable: fetch a new one

    pristine.parent.mkdir(parents=True, exist_ok=True)
    temp = pristine.parent / f".{pristine.name}.fetching-{secrets.token_hex(3)}"
    try:
        # No hooks run on clone, and the "ext" transport (which runs commands)
        # is refused explicitly.
        git(pristine.parent, "-c", "protocol.ext.allow=never",
            "clone", "--quiet", project.url, str(temp), timeout=600)
        git(temp, "-c", "advice.detachedHead=false", "checkout", "--quiet", project.commit)
        git(temp, "remote", "remove", "origin")  # no remote: nothing can be pushed
        check_pristine(project.commit, temp)
        if pristine.exists():
            shutil.rmtree(pristine)
        temp.rename(pristine)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return True


# ---- run copies -----------------------------------------------------------

def create_run_copy(
    expected_commit: str,
    pristine: Path,
    runs_dir: Path = RUNS_DIR,
    fix: str = "Run: python -m harness fetch <project>",
) -> RunCopy:
    """Copy the pristine clone into a new run folder, after checking it is untouched."""
    check_pristine(expected_commit, pristine, fix)

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    repo = run_dir / "repo"
    shutil.copytree(pristine, repo, symlinks=True, ignore=COPY_IGNORE)
    return RunCopy(run_id=run_id, run_dir=run_dir, repo=repo)


def create_project_copy(project: ProjectConfig, runs_dir: Path = RUNS_DIR) -> RunCopy:
    """A fresh run copy of an allowed project."""
    return create_run_copy(project.commit, project.pristine, runs_dir, fix=fetch_hint(project))


def apply_patch(repo: Path, patch: Path) -> None:
    """Apply a patch to a run copy, all or nothing.

    Used for reference solutions, never for model output. "git apply --check"
    runs first, so a patch that does not fit changes nothing.
    """
    patch = Path(patch).resolve()
    if not patch.is_file():
        raise WorkspaceError(f"Patch not found: {patch}")
    git(repo, "apply", "--check", str(patch))
    git(repo, "apply", str(patch))


def diff(repo: Path) -> str:
    """Changes to tracked files, compared with the starting commit."""
    # --no-ext-diff and --no-textconv: never run diff helper programs.
    return git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color")


def changed_files(repo: Path) -> list[tuple[str, str]]:
    """(status, path) for every modified, deleted or new file. ?? means new."""
    output = git(repo, "status", "--porcelain", "--untracked-files=all")
    return [(line[:2].strip(), line[3:]) for line in output.splitlines() if line.strip()]
