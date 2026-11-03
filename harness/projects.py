"""Choosing an allowed project, and what the interface shows about projects.

A run always works on exactly one project from the allowlist in the config.
The task file names its project; the user may also name it on the command
line. Both must agree, and the project must be in the allowlist.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from harness.config import PROJECT_ROOT, HarnessConfig, ProjectConfig, Task, load_task
from harness.workspace import WorkspaceError, check_pristine, fetch_hint

TASKS_DIR = PROJECT_ROOT / "tasks"


def select_project(config: HarnessConfig, task: Task, requested: str | None = None) -> ProjectConfig:
    """The project for this run, or a clear refusal.

    requested is the name the user gave (e.g. --project), or None.
    """
    name = requested if requested is not None else task.project
    if name is None:
        raise ValueError("The task names no project. Add project = \"<name>\" to the task file.")
    project = config.project(name)  # refuses anything not in the allowlist
    if task.project is not None and task.project != name:
        raise ValueError(
            f"The task belongs to project {task.project!r}, not {name!r}. "
            "Choose the task's own project."
        )
    return project


def pristine_status(project: ProjectConfig) -> str:
    """'ready', or why the project's pristine clone cannot be used yet."""
    try:
        check_pristine(project.commit, project.pristine, fetch_hint(project))
    except WorkspaceError as exc:
        return str(exc)
    return "ready"


@dataclass(frozen=True)
class TaskEntry:
    path: Path
    task: Task | None  # None if the file could not be loaded
    error: str | None = None


def list_tasks(tasks_dir: Path = TASKS_DIR) -> list[TaskEntry]:
    """Every task file, loaded. Files that cannot be loaded keep their error."""
    entries = []
    for path in sorted(tasks_dir.glob("*.toml")):
        try:
            entries.append(TaskEntry(path, load_task(path)))
        except (ValueError, OSError) as exc:
            entries.append(TaskEntry(path, None, str(exc)))
    return entries
