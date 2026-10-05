"""Tests for the repository tools."""
import os

import pytest

from harness.repo_tools import RepositoryTools, ToolError


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src" / "shop").mkdir(parents=True)
    (root / "src" / "shop" / "model.py").write_text(
        "def can_allocate(qty):\n    return qty <= 10\n"
    )
    (root / "src" / "shop" / "services.py").write_text(
        "from shop.model import can_allocate\n"
    )
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".venv").mkdir()
    (root / ".venv" / "lib.py").write_text("can_allocate = None\n")

    # A file OUTSIDE the allowed root that the agent must never reach.
    (tmp_path / "secret.txt").write_text("TOP SECRET\n")
    return root


@pytest.fixture
def tools(repo):
    return RepositoryTools(repo)


# ---- intended behavior -----------------------------------------------------

def test_list_files_shows_repo_files(tools):
    listing = tools.list_files()
    assert "src/shop/model.py" in listing
    assert "src/shop/services.py" in listing


def test_list_files_skips_blocked_dirs(tools):
    listing = tools.list_files()
    assert ".git" not in listing
    assert ".venv" not in listing


def test_read_file(tools):
    assert "return qty <= 10" in tools.read_file("src/shop/model.py")


def test_search_finds_matches_with_line_numbers(tools):
    result = tools.search("can_allocate")
    assert "src/shop/model.py:1:" in result
    assert "src/shop/services.py:1:" in result
    assert ".venv" not in result


def test_search_without_matches(tools):
    assert "No matches" in tools.search("does_not_exist")


def test_edit_replaces_exact_text(tools, repo):
    tools.edit_file("src/shop/model.py", "qty <= 10", "0 < qty <= 10")
    assert "0 < qty <= 10" in (repo / "src/shop/model.py").read_text()


def test_edit_rejects_missing_text_and_leaves_file_unchanged(tools, repo):
    before = (repo / "src/shop/model.py").read_text()
    with pytest.raises(ToolError, match="not found"):
        tools.edit_file("src/shop/model.py", "qty >= 99", "x")
    assert (repo / "src/shop/model.py").read_text() == before


def test_edit_rejects_ambiguous_text(tools, repo):
    (repo / "twice.py").write_text("x = 1\nx = 1\n")
    with pytest.raises(ToolError, match="2 times"):
        tools.edit_file("twice.py", "x = 1", "x = 2")


def test_read_missing_file_gives_clear_error(tools):
    with pytest.raises(ToolError, match="Not a file"):
        tools.read_file("src/shop/nope.py")


# ---- paths outside the allowed area are rejected ---------------------------

@pytest.mark.parametrize(
    "bad_path",
    [
        "../secret.txt",               # climbing out with ..
        "src/../../secret.txt",        # hidden inside a longer path
        "/etc/passwd",                 # absolute path
        ".git/config",                 # Git internals
        ".venv/lib.py",                # virtual environment
        "",                            # empty
    ],
)
def test_read_rejects_disallowed_paths(tools, bad_path):
    with pytest.raises(ToolError):
        tools.read_file(bad_path)


def test_edit_rejects_path_outside_repo(tools, repo):
    with pytest.raises(ToolError, match="outside"):
        tools.edit_file("../secret.txt", "TOP SECRET", "changed")
    assert (repo.parent / "secret.txt").read_text() == "TOP SECRET\n"


def test_symlink_pointing_outside_is_rejected(tools, repo):
    os.symlink(repo.parent / "secret.txt", repo / "link.txt")
    with pytest.raises(ToolError, match="outside"):
        tools.read_file("link.txt")
    assert tools.search("TOP SECRET").startswith("No matches")
