"""Show the edit-test-fix loop on the real feature task, without a model.

A scripted "model" implements the batch expiry feature, but forgets the
database mapping (orm.py) at first. When it calls finish, the harness runs the
agent checks: the SQLite integration tests fail, because batches loaded from
the database have no expiry date. The check output goes back, the script adds
the column in fix round 1, and calls finish again. The checks pass, and the
harness's final verification (with the hidden acceptance check) runs as in a
normal run. Everything runs in the Docker sandbox, as with a real model.

Usage, from the project root (needs Docker and the cosmic-sandbox image):
    python -m scripts.scripted_feature_run
"""
from __future__ import annotations

import sys

from harness.cli import Style, format_agent, format_event, format_result, format_verification
from harness.config import PROJECT_ROOT, load_config, load_task
from harness.model import ScriptedModelClient, tool_reply
from harness.projects import select_project
from harness.runner import run_task
from harness.sandbox import DockerSandbox
from harness.workspace import RUNS_DIR, create_project_copy

TASK_PATH = PROJECT_ROOT / "tasks" / "batch_expiry.toml"
COMMANDS = "src/allocation/domain/commands.py"
MODEL = "src/allocation/domain/model.py"
HANDLERS = "src/allocation/service_layer/handlers.py"
ORM = "src/allocation/adapters/orm.py"


def edit(path: str, old: str, new: str):
    return tool_reply("edit_file", path=path, old=old, new=new)


SCRIPT = [
    tool_reply("find_symbol", name="CreateBatch"),
    tool_reply("dependencies", path=MODEL),
    edit(COMMANDS, "    eta: Optional[date] = None\n",
         "    eta: Optional[date] = None\n    expires: Optional[date] = None\n"),
    edit(MODEL, "    def __init__(self, ref: str, sku: str, qty: int, eta: Optional[date]):\n"
                "        self.reference = ref\n",
         "    def __init__(\n        self, ref: str, sku: str, qty: int, eta: Optional[date],\n"
         "        expires: Optional[date] = None,\n    ):\n        self.expires = expires\n"
         "        self.reference = ref\n"),
    edit(MODEL, "        return self.sku == line.sku and self.available_quantity >= line.qty\n",
         "        expired = self.expires is not None and self.expires < date.today()\n"
         "        return (self.sku == line.sku and self.available_quantity >= line.qty\n"
         "                and not expired)\n"),
    edit(HANDLERS, "class InvalidSku(Exception):\n    pass\n",
         "class InvalidSku(Exception):\n    pass\n\n\nclass InvalidExpiry(Exception):\n    pass\n"),
    edit(HANDLERS, "    with uow:\n        product = uow.products.get(sku=cmd.sku)\n",
         "    if cmd.expires is not None and cmd.eta is not None and cmd.expires < cmd.eta:\n"
         "        raise InvalidExpiry(f\"Batch {cmd.ref} expires before it arrives\")\n"
         "    with uow:\n        product = uow.products.get(sku=cmd.sku)\n"),
    edit(HANDLERS, "model.Batch(cmd.ref, cmd.sku, cmd.qty, cmd.eta)",
         "model.Batch(cmd.ref, cmd.sku, cmd.qty, cmd.eta, cmd.expires)"),
    # Forgot orm.py: the harness's checks at finish will fail.
    tool_reply("finish", summary="Added optional expiry dates to batches."),
    # Fix round 1: the failing SQLite tests point to the database mapping.
    edit(ORM, '    Column("eta", Date, nullable=True),\n)',
         '    Column("eta", Date, nullable=True),\n    Column("expires", Date, nullable=True),\n)'),
    tool_reply("finish", summary="Added optional expiry dates to batches, stored in the database."),
]


def main(sandbox_factory=DockerSandbox, runs_dir=RUNS_DIR) -> int:
    style = Style()
    config = load_config()
    task = load_task(TASK_PATH)
    project = select_project(config, task)
    run_copy = create_project_copy(project, runs_dir=runs_dir)
    print(style.bold("Scripted feature run: the edit-test-fix loop without a model"))
    print(f"  Copy: {run_copy.repo}\n")
    report = run_task(
        task=task, task_path=TASK_PATH, config=config, project=project,
        model=ScriptedModelClient(SCRIPT), model_name="scripted", run_copy=run_copy,
        on_event=lambda e: print(format_event(style, e), flush=True),
        sandbox_factory=sandbox_factory,
    )
    print()
    print(format_agent(style, report.agent))
    print(format_verification(style, report.verification))
    print()
    print(format_result(style, report))
    return 0 if report.passed and report.agent.fix_rounds == 1 else 1


if __name__ == "__main__":
    sys.exit(main())
