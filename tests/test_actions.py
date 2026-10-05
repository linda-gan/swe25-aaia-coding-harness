"""Tests for request validation (handout test: "Invalid request", validation part)."""
import pytest

from harness.actions import TOOLS, ValidationError, tool_schemas, validate
from harness.model import ToolCall

EDITABLE = ["src/app/model.py"]
CHECKS = ["unit_tests"]


def check(name, arguments):
    return validate(ToolCall(name, arguments), EDITABLE, CHECKS)


def test_valid_request_passes():
    action = check("read_file", {"path": "src/app/model.py"})
    assert action.name == "read_file"
    assert action.arguments == {"path": "src/app/model.py"}


def test_arguments_as_json_string_are_accepted():
    assert check("read_file", '{"path": "README.md"}').arguments == {"path": "README.md"}


def test_optional_argument_may_be_left_out():
    assert check("list_files", {}).arguments == {}


@pytest.mark.parametrize(
    "name, arguments, message",
    [
        ("delete_repo", {}, "Unknown tool"),
        ("read_file", {}, "Missing argument"),
        ("read_file", {"path": "a.py", "mode": "w"}, "Unknown argument"),
        ("read_file", {"path": 42}, "must be a string"),
        ("read_file", "{not json", "not valid JSON"),
        ("read_file", ["a.py"], "must be an object"),
        ("read_file", {"path": "x" * 30_000}, "too long"),
        ("edit_file", {"path": "tests/test_x.py", "old": "a", "new": "b"}, "not allowed"),
        ("edit_file", {"path": "src/app/../../tests/t.py", "old": "a", "new": "b"}, "not allowed"),
        ("run_check", {"name": "acceptance"}, "not available"),
    ],
)
def test_invalid_requests_are_refused_with_a_clear_message(name, arguments, message):
    with pytest.raises(ValidationError, match=message):
        check(name, arguments)


def test_editable_path_is_normalized():
    assert check("edit_file", {"path": "./src/app/model.py", "old": "a", "new": "b"})


def test_schemas_describe_every_tool_and_limit_checks():
    schemas = {s["function"]["name"]: s["function"] for s in tool_schemas(CHECKS)}
    assert set(schemas) == set(TOOLS)
    assert schemas["run_check"]["parameters"]["properties"]["name"]["enum"] == CHECKS
    assert schemas["edit_file"]["parameters"]["required"] == ["path", "old", "new"]
