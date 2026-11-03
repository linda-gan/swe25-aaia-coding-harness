"""Load harness settings from config/target.toml and tasks from tasks/*.toml."""
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "target.toml"
# Each task keeps its acceptance tests in its own folder under acceptance/.
# The folder is outside every run copy, so the agent cannot change it.
ACCEPTANCE_ROOT = PROJECT_ROOT / "acceptance"
# A task names its folder with a plain name such as "invalid_quantity".
# No slashes and no "..", so a task file cannot point anywhere else.
FOLDER_NAME = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class TargetConfig:
    url: str
    commit: str
    regression_tests: str


@dataclass(frozen=True)
class SandboxConfig:
    image: str
    timeout_seconds: int
    memory: str
    cpus: str
    max_output_chars: int


@dataclass(frozen=True)
class LimitsConfig:
    max_actions: int
    max_denied_actions: int
    max_retries: int
    max_tool_output_chars: int


@dataclass(frozen=True)
class ModelConfig:
    host: str
    name: str
    think: bool
    num_ctx: int
    timeout_seconds: int


@dataclass(frozen=True)
class HarnessConfig:
    target: TargetConfig
    sandbox: SandboxConfig
    limits: LimitsConfig
    model: ModelConfig
    checks: dict[str, list[str]]


@dataclass(frozen=True)
class Task:
    description: str
    editable_files: list[str]
    agent_checks: list[str]
    final_checks: list[str]
    # This task's acceptance tests, mounted read-only at /acceptance in the
    # sandbox. None if the task has none.
    acceptance_dir: Path | None = None


def _read(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_target_config(path: Path = DEFAULT_CONFIG) -> TargetConfig:
    return TargetConfig(**_read(path)["target"])


def load_config(path: Path = DEFAULT_CONFIG) -> HarnessConfig:
    try:
        return _build_config(_read(path))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{path}: missing or unexpected setting ({exc}).") from None


def _build_config(data: dict) -> HarnessConfig:
    checks = data["checks"]
    for name, argv in checks.items():
        if not argv or not all(isinstance(part, str) for part in argv):
            raise ValueError(f"Check {name!r} must be a non-empty list of strings.")
    return HarnessConfig(
        target=TargetConfig(**data["target"]),
        sandbox=SandboxConfig(**data["sandbox"]),
        limits=LimitsConfig(**data["limits"]),
        model=ModelConfig(**data["model"]),
        checks=checks,
    )


def load_task(path: Path | str, acceptance_root: Path = ACCEPTANCE_ROOT) -> Task:
    data = _read(Path(path))
    try:
        task = Task(
            description=data["description"].strip(),
            editable_files=list(data["editable_files"]),
            agent_checks=list(data["agent_checks"]),
            final_checks=list(data["final_checks"]),
            acceptance_dir=_acceptance_dir(path, data.get("acceptance_tests"), acceptance_root),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"{path}: missing or unexpected setting ({exc}).") from None
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
