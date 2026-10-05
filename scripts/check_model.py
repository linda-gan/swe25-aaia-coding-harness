"""Check that Ollama answers and that the model makes a tool call.

Usage, from the project root:
    python -m scripts.check_model                          # model from the config
"""
import argparse
import dataclasses
import sys
import time

from harness.actions import tool_schemas
from harness.config import load_config
from harness.model import ModelError
from harness.ollama_client import OllamaClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="Ollama model name (default: from config)")
    parser.add_argument("--no-think", action="store_true", help="turn thinking mode off")
    args = parser.parse_args()

    config = load_config().model
    if args.model:
        config = dataclasses.replace(config, name=args.model)
    if args.no_think:
        config = dataclasses.replace(config, think=False)

    messages = [
        {"role": "system", "content": "You are a coding agent. Use the tools you are given."},
        {"role": "user", "content": "Read the file README.md."},
    ]
    print(f"Asking {config.name} at {config.host} (think={config.think}) ...")
    start = time.monotonic()
    try:
        reply = OllamaClient(config).next_reply(messages, tool_schemas(["unit_tests"]))
    except ModelError as exc:
        print(f"Failed: {exc}")
        return 1
    print(f"Answered in {time.monotonic() - start:.1f}s")

    if reply.thinking:
        print(f"Thinking: {' '.join(reply.thinking.split())[:300]}")
    if reply.text:
        print(f"Text: {reply.text[:300]}")
    for call in reply.tool_calls:
        print(f"Tool call: {call.name}({call.arguments})")

    ok = any(c.name == "read_file" for c in reply.tool_calls)
    print("OK: the model made the expected tool call." if ok
          else "Problem: no read_file tool call in the reply.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
