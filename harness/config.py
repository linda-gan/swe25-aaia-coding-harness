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


def load_target_config(path: Path = DEFAULT_CONFIG) -> TargetConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)["target"]
    return TargetConfig(**data)
