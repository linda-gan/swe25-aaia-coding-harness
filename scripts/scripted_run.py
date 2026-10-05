"""Run the whole loop on a real repository copy with a scripted model.

The script plays a model that makes the known fix for the Stage 1 task.
It checks that the controller, file tools and Docker sandbox work together
on Cosmic Python before a real model is involved.

Usage, from the project root, with a fresh run copy:
    python -m scripts.scripted_run workspace/runs/test-run
"""
import sys
from pathlib import Path

from harness.config import PROJECT_ROOT, load_config, load_task
from harness.controller import AgentController
from harness.model import ScriptedModelClient, tool_reply
from harness.repo_tools import RepositoryTools
from harness.sandbox import DockerSandbox

HANDLERS = "src/allocation/service_layer/handlers.py"

SCRIPT = [
    tool_reply("search", query="def allocate("),
    tool_reply("read_file", path=HANDLERS),
    tool_reply(
        "edit_file", path=HANDLERS,
        old="class InvalidSku(Exception):\n    pass\n",
        new="class InvalidSku(Exception):\n    pass\n\n\n"
            "class InvalidQuantity(Exception):\n    pass\n",
    ),
    tool_reply(
        "edit_file", path=HANDLERS,
        old="    line = OrderLine(cmd.orderid, cmd.sku, cmd.qty)\n",
        new="    if cmd.qty <= 0:\n"
            "        raise InvalidQuantity(f\"Invalid quantity {cmd.qty}\")\n"
            "    line = OrderLine(cmd.orderid, cmd.sku, cmd.qty)\n",
    ),
    tool_reply("run_check", name="unit_tests"),
    tool_reply("finish", summary="Reject non-positive quantities in the allocate handler."),
]


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1])
    config = load_config()
    task = load_task(PROJECT_ROOT / "tasks" / "invalid_quantity.toml")
    sandbox = DockerSandbox(repo, config.sandbox, config.checks,
                            acceptance_dir=PROJECT_ROOT / "acceptance")
    controller = AgentController(
        model=ScriptedModelClient(SCRIPT),
        tools=RepositoryTools(repo),
        sandbox=sandbox,
        task=task,
        limits=config.limits,
        on_event=lambda e: print(f"[{e.kind}] {e.message}"),
    )
    run = controller.run()
    print(f"\nStopped: {run.stop_reason} | actions {run.actions}, "
          f"refused {run.denied}, retries {run.retries}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
