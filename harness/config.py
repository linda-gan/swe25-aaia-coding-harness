"""Load harness settings from config/target.toml."""
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
class HarnessConfig:
    target: TargetConfig
    sandbox: SandboxConfig
    checks: dict[str, list[str]]


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
        checks=checks,
    )
