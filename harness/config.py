"""Load harness settings from config/target.toml and tasks from tasks/*.toml."""
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "target.toml"


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
class HarnessConfig:
    target: TargetConfig
    sandbox: SandboxConfig
    limits: LimitsConfig
    checks: dict[str, list[str]]


@dataclass(frozen=True)
class Task:
    description: str
    editable_files: list[str]
    agent_checks: list[str]


def _read(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_target_config(path: Path = DEFAULT_CONFIG) -> TargetConfig:
    return TargetConfig(**_read(path)["target"])


def load_config(path: Path = DEFAULT_CONFIG) -> HarnessConfig:
    data = _read(path)
    checks = data["checks"]
    for name, argv in checks.items():
        if not argv or not all(isinstance(part, str) for part in argv):
            raise ValueError(f"Check {name!r} must be a non-empty list of strings.")
    return HarnessConfig(
        target=TargetConfig(**data["target"]),
        sandbox=SandboxConfig(**data["sandbox"]),
        limits=LimitsConfig(**data["limits"]),
        checks=checks,
    )


def load_task(path: Path | str) -> Task:
    data = _read(Path(path))
    task = Task(
        description=data["description"].strip(),
        editable_files=list(data["editable_files"]),
        agent_checks=list(data["agent_checks"]),
    )
    if not task.description:
        raise ValueError(f"Task {path} has an empty description.")
    return task
