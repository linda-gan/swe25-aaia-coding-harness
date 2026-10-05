"""Model client for a local Ollama server, using its /api/chat endpoint.

Uses only the Python standard library, so there is no extra dependency.
Every problem (Ollama not running, unknown model, timeout, bad response,
conversation too long) becomes a ModelError with a clear message, which
the controller turns into a clean stop.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from harness.config import ModelConfig
from harness.model import ModelError, ModelReply, ToolCall

# Rough size estimate. Real tokens are usually a bit larger than 3 characters,
# so this errs on the side of stopping early rather than overflowing.
CHARS_PER_TOKEN = 3
# Stop before the context is completely full, to leave room for the reply.
CONTEXT_SAFETY = 0.9


class OllamaClient:
    def __init__(self, config: ModelConfig):
        self.config = config
        self.url = config.host.rstrip("/") + "/api/chat"

    def next_reply(self, messages: list[dict], tools: list[dict]) -> ModelReply:
        payload = self.build_payload(messages, tools)

        # Ollama does not fail when the conversation is longer than the context
        # window: it silently drops the oldest part, which can include the task
        # and the rules. Refuse instead, so the run stops with a clear reason.
        estimated = estimate_tokens(payload)
        limit = int(self.config.num_ctx * CONTEXT_SAFETY)
        if estimated > limit:
            raise ModelError(
                f"The conversation is too long for the model's context window "
                f"(about {estimated} tokens, limit {limit}). Increase num_ctx or "
                "lower the output limits."
            )
        return parse_reply(self._post(payload))

    def build_payload(self, messages: list[dict], tools: list[dict]) -> dict:
        return {
            "model": self.config.name,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "think": self.config.think,
            "options": {"num_ctx": self.config.num_ctx},
            "keep_alive": "30m",  # keep the model loaded between replies
        }

    def _post(self, payload: dict) -> dict:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        timeout = self.config.timeout_seconds
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc.read())
            raise ModelError(f"Ollama returned HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ModelError(f"Ollama did not answer within {timeout}s.") from None
            raise ModelError(
                f"Cannot reach Ollama at {self.config.host} ({exc.reason}). Is it running?"
            ) from None
        except TimeoutError:
            raise ModelError(f"Ollama did not answer within {timeout}s.") from None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ModelError("Ollama sent a response that is not valid JSON.") from None


def parse_reply(data: dict) -> ModelReply:
    """Turn Ollama's response into a ModelReply. Arguments are validated later."""
    if not isinstance(data, dict):
        raise ModelError("Ollama sent an unexpected response.")
    if "error" in data:
        raise ModelError(f"Ollama error: {data['error']}")
    message = data.get("message")
    if not isinstance(message, dict):
        raise ModelError("Ollama's response contains no message.")

    calls = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(function, dict):
            function = {}
        calls.append(ToolCall(
            name=str(function.get("name", "")),
            arguments=function.get("arguments", {}),
        ))
    return ModelReply(
        text=message.get("content") or "",
        thinking=message.get("thinking") or "",
        tool_calls=calls,
    )


def estimate_tokens(payload: dict) -> int:
    return len(json.dumps(payload)) // CHARS_PER_TOKEN


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace")
    try:
        return str(json.loads(text).get("error", text))
    except (json.JSONDecodeError, AttributeError):
        return text[:300] or "(no details)"
