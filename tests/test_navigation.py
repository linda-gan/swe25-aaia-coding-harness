"""Tests for the navigation tools: symbols, outlines, dependencies, Git state.

Uses a small repository laid out like Cosmic Python (src/ layout, packages,
relative imports). One test runs on the real target when the pristine clone
exists, and finds what the batch expiry feature needs.
"""
import subprocess

import pytest

from harness.config import PROJECT_ROOT, LimitsConfig, Task, load_config, load_task
from harness.controller import AgentController
from harness.model import ScriptedModelClient, tool_reply
from harness.navigation import CodeNavigator
from harness.repo_tools import RepositoryTools, ToolError
from harness.workspace import create_project_copy

TASK = Task("Look around.", ["src/shop/domain/model.py"], [], ["unit_tests"])
LIMITS = LimitsConfig(max_actions=10, max_denied_actions=5, max_retries=1,
                      max_tool_output_chars=5000)


class NoSandbox:
    def run_check(self, name):
        raise AssertionError("no check should run")


FILES = {
    "src/shop/__init__.py": "",
    "src/shop/domain/__init__.py": "",
    "src/shop/domain/model.py": (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from datetime import date\n"
        "from typing import TYPE_CHECKING\n"
        "from . import events\n"
        "if TYPE_CHECKING:\n"
        "    from shop.adapters import orm\n"
        "\n"
        "MAX_QTY = 100\n"
        "\n"
        "\n"
        "@dataclass\n"
        "class Line:\n"
        "    sku: str\n"
        "    qty: int = 1\n"
        "\n"
        "\n"
        "class Batch:\n"
        "    def __init__(self, ref: str, eta: date | None = None):\n"
        "        self.ref = ref\n"
        "\n"
        "    def can_allocate(self, line: Line) -> bool:\n"
        "        return line.qty <= MAX_QTY\n"
    ),
    "src/shop/domain/events.py": "class Allocated:\n    pass\n",
    "src/shop/adapters/__init__.py": "",
    "src/shop/adapters/orm.py": (
        "from shop.domain import model\n\n"
        "def make_table(name):\n    return name\n\n"
        "batches = make_table('batches')\n"
    ),
    "src/shop/handlers.py": (
        "from shop.domain.model import Batch\n"
        "from .domain import events\n\n"
        "def allocate(batch: Batch) -> None:\n    pass\n"
    ),
    "tests/test_model.py": "from shop.domain import model\n\ndef test_x():\n    pass\n",
    "README.md": "# Shop\n",
}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    for path, text in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@e.com",
                    "commit", "-q", "-m", "start"], check=True)
    return root


@pytest.fixture
def nav(repo):
    return CodeNavigator(RepositoryTools(repo))


# ---- symbols ----------------------------------------------------------------

def test_finds_a_class_with_its_location(nav):
    assert nav.find_symbol("Batch") == "src/shop/domain/model.py:18: class Batch"


def test_finds_methods_by_short_or_qualified_name(nav):
    assert "src/shop/domain/model.py:22: method Batch.can_allocate(self, line: Line) -> bool" \
        == nav.find_symbol("Batch.can_allocate")
    assert nav.find_symbol("can_allocate") == nav.find_symbol("Batch.can_allocate")


def test_same_name_in_several_modules_lists_all(nav):
    assert nav.find_symbol("allocate") == \
        "src/shop/handlers.py:4: function allocate(batch: Batch) -> None"
    assert "class Allocated" in nav.find_symbol("Allocated")


def test_finds_module_variables_and_dataclass_fields(nav):
    assert "src/shop/adapters/orm.py:6: variable batches = make_table('batches')" == \
        nav.find_symbol("batches")
    assert "field Line.qty: int = 1" in nav.find_symbol("qty")


def test_unknown_or_wrongly_cased_name_says_so(nav):
    assert nav.find_symbol("batch").startswith("No definition named 'batch'")


def test_empty_symbol_name_is_refused(nav):
    with pytest.raises(ToolError):
        nav.find_symbol("  ")


# ---- outlines -----------------------------------------------------------------

def test_outline_shows_structure_with_line_numbers(nav):
    text = nav.outline("src/shop/domain/model.py")
    assert text.splitlines()[0] == "src/shop/domain/model.py (module shop.domain.model)"
    assert "13: class Line  [@dataclass]" in text
    assert "  14: field sku: str" in text  # indented under its class
    assert "  22: method can_allocate(self, line: Line) -> bool" in text
    assert "9: variable MAX_QTY = 100" in text


@pytest.mark.parametrize("path, message", [
    ("README.md", "Not a Python file"),
    ("../outside.py", "outside the repository"),
    (".git/config", "not allowed"),
    ("src/missing.py", "Not a file"),
])
def test_outline_refuses_bad_paths(nav, path, message):
    with pytest.raises(ToolError, match=message):
        nav.outline(path)


# ---- dependencies -------------------------------------------------------------

def test_dependencies_show_imports_and_importers(nav):
    text = nav.dependencies("src/shop/domain/model.py")
    imports, imported_by = text.split("Imported by:")
    # "from . import events" (relative) and an import under "if TYPE_CHECKING:"
    assert "src/shop/domain/events.py" in imports
    assert "src/shop/adapters/orm.py" in imports
    assert "Other imports: __future__, dataclasses, datetime, typing" in imports
    for user in ["src/shop/adapters/orm.py", "src/shop/handlers.py", "tests/test_model.py"]:
        assert user in imported_by


def test_relative_import_from_a_package_is_resolved(nav):
    text = nav.dependencies("src/shop/domain/events.py")
    imported_by = text.split("Imported by:")[1]
    assert "src/shop/domain/model.py" in imported_by  # from . import events
    assert "src/shop/handlers.py" in imported_by      # from .domain import events


# ---- broken and hostile files ---------------------------------------------------

def test_a_file_that_does_not_parse_is_reported_not_fatal(repo, nav):
    (repo / "src/shop/broken.py").write_text("def oops(:\n")
    assert "class Batch" in nav.find_symbol("Batch")
    assert "could not parse src/shop/broken.py" in nav.find_symbol("Batch")
    assert "Could not parse this file: SyntaxError at line 1" in nav.outline("src/shop/broken.py")


def test_parsing_never_runs_repository_code(repo, nav):
    (repo / "src/shop/evil.py").write_text(
        "import pathlib\npathlib.Path('PWNED').write_text('x')\n"
        "def f():\n    pass\n"
    )
    nav.find_symbol("f")
    nav.outline("src/shop/evil.py")
    nav.dependencies("src/shop/evil.py")
    assert not (repo / "PWNED").exists()
    assert not (PROJECT_ROOT / "PWNED").exists()


# ---- Git state ------------------------------------------------------------------

def test_git_status_before_and_after_an_edit(repo, nav):
    assert "No files changed yet." in nav.git_status()
    RepositoryTools(repo).edit_file("src/shop/domain/model.py", "MAX_QTY = 100", "MAX_QTY = 50")
    status = nav.git_status()
    assert "M  src/shop/domain/model.py" in status
    assert "Starting commit:" in status


def test_git_diff_for_everything_or_one_path(repo, nav):
    RepositoryTools(repo).edit_file("src/shop/domain/model.py", "MAX_QTY = 100", "MAX_QTY = 50")
    assert "+MAX_QTY = 50" in nav.git_diff()
    assert "+MAX_QTY = 50" in nav.git_diff("src/shop/domain")
    assert nav.git_diff("src/shop/handlers.py") == "No changes in src/shop/handlers.py."


@pytest.mark.parametrize("path", ["../elsewhere", ".git", "/etc/passwd"])
def test_git_diff_uses_the_same_path_checks(nav, path):
    with pytest.raises(ToolError):
        nav.git_diff(path)


def test_git_state_without_a_repository_is_a_clear_error(tmp_path):
    (tmp_path / "plain").mkdir()
    with pytest.raises(ToolError, match="Git state is not available"):
        CodeNavigator(RepositoryTools(tmp_path / "plain")).git_status()


# ---- search hint ----------------------------------------------------------------

def test_search_explains_plain_text_when_a_regex_finds_nothing(repo):
    tools = RepositoryTools(repo)
    assert "search matches plain text" in tools.search("qty.*0")
    assert tools.search("no_such_word") == "No matches for 'no_such_word'."


# ---- through the controller -------------------------------------------------------

def test_model_can_call_the_navigation_tools(repo):
    model = ScriptedModelClient([
        tool_reply("find_symbol", name="Batch"),
        tool_reply("outline", path="src/shop/domain/model.py"),
        tool_reply("dependencies", path="src/shop/domain/model.py"),
        tool_reply("git_status"),
        tool_reply("git_diff"),
        tool_reply("finish", summary="Looked around."),
    ])
    run = AgentController(model, RepositoryTools(repo), NoSandbox(), TASK, LIMITS).run()
    results = [m["content"] for m in run.messages if m["role"] == "tool"]
    assert results[0] == "src/shop/domain/model.py:18: class Batch"
    assert results[1].startswith("src/shop/domain/model.py (module shop.domain.model)")
    assert "Imported by:" in results[2]
    assert "No files changed yet." in results[3]
    assert results[4] == "No changes."
    assert run.actions == 5 and run.denied == 0


def test_invalid_navigation_requests_are_refused_before_running(repo):
    model = ScriptedModelClient([
        tool_reply("find_symbol"),                    # missing name
        tool_reply("git_status", path="src"),         # git_status takes no arguments
        tool_reply("outline", path="../outside.py"),  # path check in the tool
        tool_reply("finish", summary="done"),
    ])
    run = AgentController(model, RepositoryTools(repo), NoSandbox(), TASK, LIMITS).run()
    results = [m["content"] for m in run.messages if m["role"] == "tool"]
    assert results[0].startswith("Error: Missing argument(s) for find_symbol: name")
    assert results[1].startswith("Error: Unknown argument(s) for git_status: path")
    assert "outside the repository" in results[2]
    assert run.denied == 3


# ---- the real target --------------------------------------------------------------

PROJECT = load_config().project("cosmicpython")


@pytest.mark.skipif(not (PROJECT.pristine / ".git").is_dir(),
                    reason="needs the pristine clone (python -m harness fetch cosmicpython)")
def test_finds_what_the_batch_expiry_feature_needs(tmp_path):
    run_copy = create_project_copy(PROJECT, runs_dir=tmp_path / "runs")
    nav = CodeNavigator(RepositoryTools(run_copy.repo))

    # Where things are defined
    assert nav.find_symbol("CreateBatch").startswith("src/allocation/domain/commands.py:")
    assert nav.find_symbol("can_allocate").startswith("src/allocation/domain/model.py:")
    assert "src/allocation/adapters/orm.py" in nav.find_symbol("batches")
    assert "src/allocation/service_layer/handlers.py" in nav.find_symbol("add_batch")

    # Who depends on the domain model: the database mapping and the handlers
    imported_by = nav.dependencies("src/allocation/domain/model.py").split("Imported by:")[1]
    assert "src/allocation/adapters/orm.py" in imported_by
    assert "src/allocation/service_layer/handlers.py" in imported_by

    # Together these reach exactly the files the task may change
    editable = set(load_task(PROJECT_ROOT / "tasks" / "batch_expiry.toml").editable_files)
    reached = {line.split(":")[0] for q in ["CreateBatch", "can_allocate", "batches", "add_batch"]
               for line in nav.find_symbol(q).splitlines()}
    assert editable <= reached

    # Expected Git state after an edit
    RepositoryTools(run_copy.repo).edit_file(
        "src/allocation/domain/commands.py",
        "    eta: Optional[date] = None\n",
        "    eta: Optional[date] = None\n    expires: Optional[date] = None\n",
    )
    assert "M  src/allocation/domain/commands.py" in nav.git_status()
    assert "+    expires: Optional[date] = None" in nav.git_diff("src/allocation/domain/commands.py")
