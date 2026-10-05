"""The interface between the controller and a language model.

The controller only knows ModelClient. Tests use ScriptedModelClient, which
plays back prepared replies, so they run without a model, account or API key.
The real client for Ollama is in harness/ollama_client.py.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Protocol


class ModelError(Exception):
    """The model could not be reached or gave no reply."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    # Models send arguments as a dict or as a JSON string. Both are untrusted
    # and are checked by harness.actions.validate before anything runs.
    arguments: dict | str = field(default_factory=dict)


@dataclass(frozen=True)
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""  # the model's reasoning, if thinking mode is on


class ModelClient(Protocol):
    def next_reply(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        """Send the conversation and the tool definitions, return the model's reply."""
        ...


def tool_reply(tool_name: str, /, **arguments) -> ModelReply:
    """Shortcut for a reply with one tool call, used to write test scripts.

    tool_name is positional-only so tools can have an argument called "name".
    """
    return ModelReply(tool_calls=[ToolCall(tool_name, arguments)])


class ScriptedModelClient:
    """Plays back a fixed list of replies in order.

    With repeat_last=True the last reply is repeated forever, which is how
    the action-limit test simulates a model that never stops.
    """

    def __init__(self, replies: list[ModelReply], repeat_last: bool = False):
        self._replies = list(replies)
        self.repeat_last = repeat_last
        self.received: list[list[dict]] = []  # what the model was sent, per call

    def next_reply(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.received.append(copy.deepcopy(messages))
        if not self._replies:
            raise ModelError("The scripted model has no replies left.")
        if self.repeat_last and len(self._replies) == 1:
            return self._replies[0]
        return self._replies.pop(0)
