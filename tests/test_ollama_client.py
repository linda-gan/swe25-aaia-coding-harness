"""Tests for the Ollama client.

A tiny fake Ollama server runs on a free local port, so the real HTTP code
is exercised without Ollama, a model or a GPU.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness.config import ModelConfig
from harness.model import ModelError
from harness.ollama_client import OllamaClient, parse_reply


class FakeOllama:
    """Answers POST /api/chat with a prepared status and JSON body."""

    def __init__(self):
        self.status = 200
        self.body = {}
        self.delay = 0.0
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                fake.requests.append((self.path, json.loads(self.rfile.read(length))))
                time.sleep(fake.delay)
                data = json.dumps(fake.body).encode()
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass  # keep test output quiet

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def ollama():
    fake = FakeOllama()
    yield fake
    fake.close()


def make_client(host, **overrides):
    values = dict(host=host, name="test-model", think=True, num_ctx=32768, timeout_seconds=5)
    values.update(overrides)
    return OllamaClient(ModelConfig(**values))


MESSAGES = [{"role": "user", "content": "Fix the bug."}]
TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]


def test_sends_model_messages_tools_and_settings(ollama):
    ollama.body = {"message": {"role": "assistant", "content": "ok"}}
    make_client(ollama.host).next_reply(MESSAGES, TOOLS)

    path, payload = ollama.requests[0]
    assert path == "/api/chat"
    assert payload["model"] == "test-model"
    assert payload["messages"] == MESSAGES
    assert payload["tools"] == TOOLS
    assert payload["stream"] is False
    assert payload["think"] is True
    assert payload["options"]["num_ctx"] == 32768


def test_parses_text_thinking_and_tool_calls(ollama):
    ollama.body = {"message": {
        "role": "assistant",
        "content": "Let me look.",
        "thinking": "Start with the handler.",
        "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}],
    }}
    reply = make_client(ollama.host).next_reply(MESSAGES, TOOLS)

    assert reply.text == "Let me look."
    assert reply.thinking == "Start with the handler."
    assert reply.tool_calls[0].name == "read_file"
    assert reply.tool_calls[0].arguments == {"path": "a.py"}


def test_malformed_tool_call_is_kept_for_validation_not_crashed():
    reply = parse_reply({"message": {"content": "", "tool_calls": ["garbage", {"function": 5}]}})
    assert [c.name for c in reply.tool_calls] == ["", ""]  # refused later by validate()


def test_unknown_model_gives_clear_error(ollama):
    ollama.status = 404
    ollama.body = {"error": "model 'test-model' not found"}
    with pytest.raises(ModelError, match="HTTP 404: model 'test-model' not found"):
        make_client(ollama.host).next_reply(MESSAGES, TOOLS)


def test_ollama_not_running_gives_clear_error():
    with socket.socket() as s:  # find a port nothing is listening on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(ModelError, match="Cannot reach Ollama"):
        make_client(f"http://127.0.0.1:{port}").next_reply(MESSAGES, TOOLS)


def test_slow_answer_times_out(ollama):
    ollama.delay = 2
    ollama.body = {"message": {"content": "too late"}}
    with pytest.raises(ModelError, match="did not answer within 1s"):
        make_client(ollama.host, timeout_seconds=1).next_reply(MESSAGES, TOOLS)


def test_conversation_too_long_is_refused_before_sending(ollama):
    long_messages = [{"role": "user", "content": "x" * 30_000}]
    with pytest.raises(ModelError, match="too long for the model's context"):
        make_client(ollama.host, num_ctx=4096).next_reply(long_messages, TOOLS)
    assert ollama.requests == []  # nothing was sent
