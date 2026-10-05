"""Run the agent with the real model on a repository copy.

Usage, from the project root, with a fresh run copy:
    python -m scripts.agent_run workspace/runs/test-run
    python -m scripts.agent_run workspace/runs/test-run --no-think

Afterwards, check the result with:
    python -m scripts.check_sandbox workspace/runs/test-run
    git -C workspace/runs/test-run diff
"""
import argparse
import dataclasses
import sys
import time
from pathlib import Path

from harness.config import PROJECT_ROOT, load_config, load_task
from harness.controller import AgentController
from harness.ollama_client import OllamaClient
from harness.repo_tools import RepositoryTools
from harness.sandbox import DockerSandbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path, help="repository copy to work on")
    parser.add_argument("--task", type=Path, default=PROJECT_ROOT / "tasks" / "invalid_quantity.toml")
    parser.add_argument("--model", help="Ollama model name (default: from config)")
    parser.add_argument("--no-think", action="store_true", help="turn thinking mode off")
    args = parser.parse_args()

    config = load_config()
    model_config = config.model
    if args.model:
        model_config = dataclasses.replace(model_config, name=args.model)
    if args.no_think:
        model_config = dataclasses.replace(model_config, think=False)

    start = time.monotonic()

    def show(event):
        print(f"[{time.monotonic() - start:6.1f}s] [{event.kind}] {event.message}", flush=True)

    controller = AgentController(
        model=OllamaClient(model_config),
        tools=RepositoryTools(args.repo),
        sandbox=DockerSandbox(args.repo, config.sandbox, config.checks,
                              acceptance_dir=PROJECT_ROOT / "acceptance"),
        task=load_task(args.task),
        limits=config.limits,
        on_event=show,
    )
    print(f"Model: {model_config.name} (think={model_config.think})")
    run = controller.run()
    print(f"\nStopped: {run.stop_reason} | actions {run.actions}, "
          f"refused {run.denied}, retries {run.retries}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
