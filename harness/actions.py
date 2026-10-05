"""The actions the model may request, and the checks every request must pass.

Model replies are untrusted input. A request runs only after validate()
has confirmed the tool exists, the arguments are complete and correctly
typed, and the task permits it (editable files, allowed checks).
"""
from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass

from harness.model import ToolCall

MAX_ARGUMENT_CHARS = 20_000


class ValidationError(Exception):
    """A request was refused before anything ran. The message goes to the model."""


@dataclass(frozen=True)
class Param:
    description: str
    required: bool = True


@dataclass(frozen=True)
class ToolSpec:
    description: str
    params: dict[str, Param]


# Every parameter is a string, which keeps validation simple and strict.
TOOLS: dict[str, ToolSpec] = {
    "list_files": ToolSpec(
        "List the files in the repository, or in one folder of it.",
        {"path": Param("Folder relative to the repository root. Default: whole repository.",
                       required=False)},
    ),
    "read_file": ToolSpec(
        "Read a text file.",
        {"path": Param("File path relative to the repository root.")},
    ),
    "search": ToolSpec(
        "Find lines containing some text. Returns path:line: text for each match.",
        {"query": Param("Exact text to look for (case-sensitive, plain text, not a regular expression)."),
         "path": Param("File or folder to search. Default: whole repository.", required=False)},
    ),
    "edit_file": ToolSpec(
        "Replace text in a file. old must appear exactly once in the file.",
        {"path": Param("File path relative to the repository root."),
         "old": Param("Exact text currently in the file, including indentation."),
         "new": Param("Text to put in its place.")},
    ),
    "run_check": ToolSpec(
        "Run a configured check, such as the test suite, and get its result.",
        {"name": Param("Name of the check to run.")},
    ),
    "finish": ToolSpec(
        "Call this when the task is complete.",
        {"summary": Param("Short summary of what you changed and why.")},
    ),
}


@dataclass(frozen=True)
class Action:
    """A request that passed validation and may run."""
    name: str
    arguments: dict[str, str]


def tool_schemas(agent_checks: list[str]) -> list[dict]:
    """Tool definitions in the function-calling format Ollama expects."""
    schemas = []
    for name, spec in TOOLS.items():
        properties = {
            param: {"type": "string", "description": p.description}
            for param, p in spec.params.items()
        }
        if name == "run_check":
            properties["name"]["enum"] = list(agent_checks)
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": [param for param, p in spec.params.items() if p.required],
                },
            },
        })
    return schemas


def normalize_path(path: str) -> str:
    """'./src/a/../a/b.py' -> 'src/a/b.py', so permission checks compare like with like."""
    return posixpath.normpath(path.replace("\\", "/"))


def validate(call: ToolCall, editable_files: list[str], agent_checks: list[str]) -> Action:
    name = call.name if isinstance(call.name, str) else repr(call.name)
    if name not in TOOLS:
        raise ValidationError(
            f"Unknown tool {name[:60]!r}. Available tools: {', '.join(TOOLS)}."
        )

    args = call.arguments
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            raise ValidationError(f"Arguments for {name} are not valid JSON.") from None
    if not isinstance(args, dict):
        raise ValidationError(f"Arguments for {name} must be an object with named fields.")

    params = TOOLS[name].params
    unknown = sorted(set(args) - set(params))
    if unknown:
        raise ValidationError(
            f"Unknown argument(s) for {name}: {', '.join(map(str, unknown))}. "
            f"Expected: {', '.join(params) or 'none'}."
        )
    missing = [param for param, p in params.items() if p.required and param not in args]
    if missing:
        raise ValidationError(f"Missing argument(s) for {name}: {', '.join(missing)}.")
    for key, value in args.items():
        if not isinstance(value, str):
            raise ValidationError(f"Argument {key!r} for {name} must be a string.")
        if len(value) > MAX_ARGUMENT_CHARS:
            raise ValidationError(
                f"Argument {key!r} for {name} is too long ({len(value)} characters, "
                f"limit {MAX_ARGUMENT_CHARS})."
            )

    # Permissions that depend on the task.
    if name == "edit_file":
        allowed = {normalize_path(p) for p in editable_files}
        if normalize_path(args["path"]) not in allowed:
            raise ValidationError(
                f"Editing {args['path']!r} is not allowed for this task. "
                f"Editable files: {', '.join(editable_files) or 'none'}."
            )
    if name == "run_check" and args["name"] not in agent_checks:
        raise ValidationError(
            f"Check {args['name']!r} is not available. "
            f"Available checks: {', '.join(agent_checks) or 'none'}."
        )

    return Action(name, dict(args))
