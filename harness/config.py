"""Load harness settings from config/target.toml and tasks from tasks/*.toml.

The config lists the allowed projects under [projects.<name>]. Each project
brings everything that depends on the repository: where it comes from, the
exact starting commit, where its pristine clone lives, its sandbox image and
the only commands that may run for it. Limits, sandbox resources and the
model are shared by all projects.
"""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "target.toml"
# Pristine clones and run folders live here, never anywhere else on the host.
WORKSPACE_DIR = PROJECT_ROOT / "workspace"
# Each task keeps its acceptance tests in its own folder under acceptance/.
# The folder is outside every run copy, so the agent cannot change it.
ACCEPTANCE_ROOT = PROJECT_ROOT / "acceptance"
# Plain names for projects and acceptance folders, such as "cosmicpython":
# no slashes and no "..", so a name cannot point anywhere else.
FOLDER_NAME = re.compile(r"[A-Za-z0-9_-]+")
COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class SandboxConfig:
    image: str
    timeout_seconds: int
    memory: str
    cpus: str
    max_output_chars: int


@dataclass(frozen=True)
class LimitsConfig:
    max_actions: int  # default for every task; a task file may set its own
    max_denied_actions: int
    max_retries: int
    max_tool_output_chars: int
    max_fix_rounds: int = 3  # finish attempts with failing checks before the run stops
    max_actions_ceiling: int = 60  # a task file may not set max_actions above this


@dataclass(frozen=True)
class ContextConfig:
    """How much is sent to the model per request. See harness/context.py."""
    max_tokens: int = 24000  # size limit per request, estimated at 3 characters per token
    keep_recent_results: int = 6  # newest tool results kept in full while the limit allows
    repo_map_chars: int = 3000  # repository map in the first message
    selected_files: int = 4  # files whose outline is shown in the first message
    selected_chars: int = 4000  # for those outlines together


@dataclass(frozen=True)
class ModelConfig:
    host: str
    name: str
    think: bool
    num_ctx: int
    timeout_seconds: int


@dataclass(frozen=True)
class ProjectConfig:
    """One allowed project: a repository at an exact commit, and how to check it."""
    name: str
    url: str
    commit: str  # the exact starting commit, 40 hex characters
    pristine: Path  # absolute path of the clean clone, inside workspace/
    sandbox: SandboxConfig  # this project's image plus the shared resources
    checks: dict[str, list[str]]  # the only commands that may run, by name


@dataclass(frozen=True)
class HarnessConfig:
    projects: dict[str, ProjectConfig]  # the allowlist
    limits: LimitsConfig
    model: ModelConfig
    context: ContextConfig = ContextConfig()

    def project(self, name: str) -> ProjectConfig:
        """The allowed project with this name, or a clear refusal."""
        if not isinstance(name, str) or name not in self.projects:
            raise ValueError(
                f"Project {str(name)[:60]!r} is not allowed. "
                f"Allowed projects: {', '.join(self.projects)}."
            )
        return self.projects[name]


@dataclass(frozen=True)
class Task:
    description: str
    editable_files: list[str]
    agent_checks: list[str]
    final_checks: list[str]
    # This task's acceptance tests, mounted read-only at /acceptance in the
    # sandbox. None if the task has none.
    acceptance_dir: Path | None = None
    # The project the task belongs to. Required in task files; whether it is
    # allowed is decided against the config when a run starts.
    project: str | None = None
    # Optional action limit for this task, instead of the config's default.
    # It may not exceed [limits] max_actions_ceiling (checked when a run starts).
    max_actions: int | None = None


def _read(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config(path: Path = DEFAULT_CONFIG, workspace_dir: Path = WORKSPACE_DIR) -> HarnessConfig:
    try:
        return _build_config(_read(path), workspace_dir)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{path}: missing or unexpected setting ({exc}).") from None


def _build_config(data: dict, workspace_dir: Path) -> HarnessConfig:
    resources = data["sandbox"]  # shared by every project
    if "image" in resources:
        raise ValueError("[sandbox] must not set an image; each project sets its own.")
    projects_data = data["projects"]
    if not isinstance(projects_data, dict) or not projects_data:
        raise ValueError("The config must list at least one project under [projects.<name>].")

    projects = {}
    for name, values in projects_data.items():
        projects[name] = _build_project(name, values, resources, workspace_dir)

    clones = [p.pristine for p in projects.values()]
    if len(set(clones)) != len(clones):
        raise ValueError("Two projects use the same pristine folder.")

    model = ModelConfig(**data["model"])
    context_values = dict(data.get("context", {}))
    # Without an explicit limit, use the default, but never more than fits the model.
    context_values.setdefault("max_tokens", min(ContextConfig.max_tokens,
                                                int(model.num_ctx * CONTEXT_SAFETY)))
    context = ContextConfig(**context_values)
    _check_context(context, model)
    limits = LimitsConfig(**data["limits"])
    if not 0 < limits.max_actions <= limits.max_actions_ceiling:
        raise ValueError("[limits] max_actions must be between 1 and max_actions_ceiling.")
    if limits.max_fix_rounds < 0:
        raise ValueError("[limits] max_fix_rounds must not be negative.")
    return HarnessConfig(
        projects=projects,
        limits=limits,
        model=model,
        context=context,
    )


# The Ollama client refuses requests above this share of num_ctx, to leave
# room for the reply. The context limit must fit below it.
CONTEXT_SAFETY = 0.9
# Size estimate used everywhere: about 3 characters per token (errs on the safe side).
CHARS_PER_TOKEN = 3


def _check_context(context: ContextConfig, model: ModelConfig) -> None:
    ceiling = int(model.num_ctx * CONTEXT_SAFETY)
    if not 0 < context.max_tokens <= ceiling:
        raise ValueError(
            f"[context] max_tokens must be between 1 and {ceiling} "
            f"(90% of the model's num_ctx = {model.num_ctx})."
        )
    for name in ["keep_recent_results", "repo_map_chars", "selected_files", "selected_chars"]:
        if getattr(context, name) < 0:
            raise ValueError(f"[context] {name} must not be negative.")


def _build_project(name: str, values: dict, resources: dict, workspace_dir: Path) -> ProjectConfig:
    where = f"[projects.{name}]"
    if not FOLDER_NAME.fullmatch(name):
        raise ValueError(f"{where}: a project name may only use letters, digits, '_' and '-'.")
    unknown = set(values) - {"url", "commit", "pristine", "image", "checks"}
    if unknown:
        raise ValueError(f"{where}: unknown setting(s) {', '.join(sorted(unknown))}.")

    url, commit, image = values["url"], values["commit"], values["image"]
    for key, value in [("url", url), ("image", image)]:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{where}: {key} must be a non-empty string.")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ValueError(f"{where}: commit must be a full 40-character commit hash.")

    checks = values["checks"]
    if not isinstance(checks, dict) or not checks:
        raise ValueError(f"{where}: at least one check is needed under [projects.{name}.checks].")
    for check, argv in checks.items():
        if not isinstance(argv, list) or not argv or not all(isinstance(p, str) for p in argv):
            raise ValueError(f"{where}: check {check!r} must be a non-empty list of strings.")

    return ProjectConfig(
        name=name,
        url=url,
        commit=commit,
        pristine=_pristine_dir(where, values["pristine"], workspace_dir),
        sandbox=SandboxConfig(image=image, **resources),
        checks=dict(checks),
    )


def _pristine_dir(where: str, value, workspace_dir: Path) -> Path:
    """The pristine clone must lie inside workspace/, but not in the run folders.

    "fetch" replaces this folder, so it must never be able to name a folder
    elsewhere on the host.
    """
    workspace = workspace_dir.resolve()
    if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
        raise ValueError(f"{where}: pristine must be a relative path such as "
                         "\"workspace/target-pristine\".")
    folder = (workspace.parent / value).resolve()
    if folder == workspace or not folder.is_relative_to(workspace):
        raise ValueError(f"{where}: pristine must be a folder inside {workspace}.")
    if folder.is_relative_to(workspace / "runs"):
        raise ValueError(f"{where}: pristine must not be inside the run folders.")
    return folder


def load_task(path: Path | str, acceptance_root: Path = ACCEPTANCE_ROOT) -> Task:
    data = _read(Path(path))
    try:
        task = Task(
            description=data["description"].strip(),
            editable_files=list(data["editable_files"]),
            agent_checks=list(data["agent_checks"]),
            final_checks=list(data["final_checks"]),
            acceptance_dir=_acceptance_dir(path, data.get("acceptance_tests"), acceptance_root),
            project=data["project"],
            max_actions=data.get("max_actions"),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"{path}: missing or unexpected setting ({exc}).") from None
    if not isinstance(task.project, str) or not FOLDER_NAME.fullmatch(task.project):
        raise ValueError(f"{path}: project must be the plain name of a project, "
                         f"such as \"cosmicpython\" (got {task.project!r}).")
    if task.max_actions is not None and (
            isinstance(task.max_actions, bool) or not isinstance(task.max_actions, int)
            or task.max_actions < 1):
        raise ValueError(f"{path}: max_actions must be a positive whole number.")
    if not task.description:
        raise ValueError(f"Task {path} has an empty description.")
    if not task.final_checks:
        raise ValueError(f"Task {path} must list at least one final check.")
    return task


def _acceptance_dir(task_path, name, acceptance_root: Path) -> Path | None:
    """Turn acceptance_tests = "<folder>" into acceptance/<folder>, or refuse.

    The folder is mounted into the sandbox, so a task file must not be able
    to name any other place on the host, such as a folder with credentials.
    """
    if name is None:
        return None
    if not isinstance(name, str) or not FOLDER_NAME.fullmatch(name):
        raise ValueError(
            f"{task_path}: acceptance_tests must be the plain name of a folder "
            f"under acceptance/, such as \"invalid_quantity\" (got {name!r})."
        )
    folder = (acceptance_root / name).resolve()
    if not folder.is_dir():
        raise ValueError(f"{task_path}: acceptance folder {folder} does not exist.")
    # resolve() follows symlinks, so a symlinked folder pointing elsewhere is refused.
    if folder.parent != acceptance_root.resolve():
        raise ValueError(f"{task_path}: acceptance folder {name!r} points outside acceptance/.")
    return folder
