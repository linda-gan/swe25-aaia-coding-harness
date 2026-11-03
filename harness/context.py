"""What the model sees: a repository overview, trusted and untrusted text kept
apart, and a history that stays within a documented size limit.

The controller keeps the full, unchanged history in RunResult.messages (that
is what the run record stores). Before every model request it asks
ContextManager.view() for the messages to send. The view:

1. Keeps the trusted messages unchanged and never shortens them: the system
   prompt (rules, editable files, checks) and the task message (the task
   description with its acceptance criteria). Both come from the harness and
   the task file, not from the repository.
2. Wraps all untrusted text in markers that contain a random code for this
   run: the repository overview and every tool result. Repository text cannot
   guess the code, and any copy of the end marker inside the text is defused,
   so it cannot close the block early and pretend to be an instruction.
3. Stays within [context] max_tokens (estimated at 3 characters per token,
   tool definitions included). When a request would be larger, the oldest
   tool results are replaced by short notes, and a summary written by the
   harness lists what those steps did. Old results are shortened down to 60%
   of the limit at once, so the start of the conversation then stays the same
   for several requests and Ollama can reuse its cache. The newest
   keep_recent_results results stay in full; only if the limit cannot be met
   otherwise are newer results shortened too (always keeping the very last
   one), and then the repository overview is removed.

The first messages also give the model a compact repository map and the
outlines of the files the harness ranks as most relevant for the task.
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass

from harness.config import CHARS_PER_TOKEN, ContextConfig, Task
from harness.navigation import CodeNavigator, ModuleInfo
from harness.prompts import system_prompt

COMPACT_TO = 0.6  # share of max_tokens to compact down to
MAX_NAMES_PER_FILE = 10
MAX_OTHER_FILES = 15
# Message keys that only the harness uses; they are never sent to the model.
HARNESS_KEYS = ("harness_untrusted", "harness_step")
STOPWORDS = set("""
a an and are as at be been but by can could do does for from has have if in into is it
its may must no not of on or should so than that the then there these this to too was
were when which while will with without would you your new old also only same such each
must other any all
""".split())


# ---- choosing files ---------------------------------------------------------

@dataclass(frozen=True)
class SelectedFile:
    path: str
    score: int
    reasons: list[str]


def task_terms(description: str) -> tuple[set[str], set[str]]:
    """(identifiers, words) from a task description.

    Identifiers are names written like code (CreateBatch, add_batch, eta2);
    words are all parts of all names, lowercased, without stopwords.
    """
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", description)
    identifiers = {t for t in tokens if "_" in t or re.search(r"[a-z][A-Z]|\d", t)
                   or (t[0].isupper() and any(c.islower() for c in t[1:]) and len(t) > 3)}
    words = set()
    for token in tokens:
        words.update(split_words(token))
    return identifiers, {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def split_words(name: str) -> set[str]:
    """CreateBatch -> {create, batch}; add_batch -> {add, batch}"""
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)
    return {p.lower() for p in parts}


def select_files(task: Task, modules: dict[str, ModuleInfo], count: int) -> list[SelectedFile]:
    """Rank Python files by how relevant they look for the task.

    Points: editable file 10, path named in the description 8, defines an
    identifier from the description 5 each, shares words with the description
    1 each (at most 5). Files without points are never selected.
    """
    identifiers, words = task_terms(task.description)
    editable = set(task.editable_files)
    ranked = []
    for path, module in modules.items():
        score, reasons = 0, []
        if path in editable:
            score += 10
            reasons.append("editable")
        if path in task.description:
            score += 8
            reasons.append("named in the task")
        defined = sorted({s.qualified_name.split(".")[-1] for s in module.symbols} & identifiers)
        if defined:
            score += 5 * len(defined)
            reasons.append("defines " + ", ".join(defined))
        bag = split_words(path.rsplit("/", 1)[-1].removesuffix(".py"))
        for symbol in module.symbols:
            bag |= split_words(symbol.qualified_name.split(".")[-1])
        shared = sorted(bag & words)[:5]
        if shared:
            score += len(shared)
            reasons.append("words: " + ", ".join(shared))
        if score:
            ranked.append(SelectedFile(path, score, reasons))
    ranked.sort(key=lambda f: (-f.score, f.path))
    return ranked[:count]


# ---- the repository map ------------------------------------------------------

def build_repo_map(modules: dict[str, ModuleInfo], other_files: list[str], limit: int,
                   keep_names_for: set[str] = frozenset()) -> str:
    """Folders, Python files and their top-level names, within limit characters.

    When it does not fit, details are dropped in this order: names of test
    files, names of files that were not selected, all names, then lines.
    """
    def is_test(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return path.startswith("tests/") or "/tests/" in path or name.startswith("test_")

    def render(show_names) -> list[str]:
        lines, folder = [], None
        # Grouped by folder, so each folder is listed once.
        for path in sorted(modules, key=lambda p: (p.rpartition("/")[0], p)):
            parent, _, name = path.rpartition("/")
            if parent != folder:
                folder = parent
                lines.append(f"{parent or '.'}/")
            module = modules[path]
            entry = f"  {name}"
            if module.error:
                entry += " (could not parse)"
            elif show_names(path):
                names = [s.qualified_name for s in module.symbols if "." not in s.qualified_name]
                if names:
                    shown = names[:MAX_NAMES_PER_FILE]
                    more = len(names) - len(shown)
                    entry += ": " + ", ".join(shown) + (f" (+{more})" if more else "")
            lines.append(entry)
        if other_files:
            shown = sorted(other_files)[:MAX_OTHER_FILES]
            more = len(other_files) - len(shown)
            lines.append("Other files: " + ", ".join(shown) + (f" (+{more} more)" if more else ""))
        return lines

    levels = [
        lambda p: True,
        lambda p: not is_test(p),
        lambda p: p in keep_names_for,
        lambda p: False,
    ]
    for show_names in levels:
        lines = render(show_names)
        text = "\n".join(lines)
        if len(text) <= limit:
            return text
    # Still too long: keep as many lines as fit and say so.
    kept, used = [], 0
    for line in lines:
        if used + len(line) + 1 > limit - 60:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept) + f"\n[map shortened: {len(lines) - len(kept)} more lines]"


# ---- the context manager ------------------------------------------------------

class ContextManager:
    def __init__(self, task: Task, navigator: CodeNavigator, config: ContextConfig,
                 tool_schemas: list[dict], nonce: str | None = None, fix_rounds: int = 3):
        self.task = task
        self.fix_rounds = fix_rounds
        self.navigator = navigator
        self.config = config
        self.tool_schemas = tool_schemas
        self.nonce = nonce or secrets.token_hex(4)
        self.pinned = 2  # system prompt and task message: never changed
        self.stubbed: set[int] = set()  # indices of tool results replaced by a note
        self.overview_removed = False
        self.compactions = 0
        self.largest_request = 0  # estimated tokens
        self.last_compaction: str | None = None  # set when the latest view() compacted

    # ---- the first messages ----------------------------------------------------

    def initial_messages(self) -> list[dict]:
        return [
            {"role": "system", "content": system_prompt(self.task, self.nonce, self.fix_rounds)},
            {"role": "user", "content": self.task.description},
            {"role": "user", "content": self.overview(),
             "harness_untrusted": "repository overview"},
        ]

    def overview(self) -> str:
        """Repository map, the files selected for the task, and their outlines."""
        modules = self.navigator.modules()
        others = [p.relative_to(self.navigator.root).as_posix()
                  for p in self.navigator.tools.files(".") if p.suffix != ".py"]
        selected = select_files(self.task, modules, self.config.selected_files)
        repo_map = build_repo_map(modules, others, self.config.repo_map_chars,
                                  {f.path for f in selected})
        parts = ["Repository overview, made by the harness from the repository files.",
                 "", "Repository map (folders, files, top-level names):", repo_map]
        if selected:
            parts += ["", "Files the harness selected as most relevant for the task:"]
            parts += [f"  {f.path} ({'; '.join(f.reasons)})" for f in selected]
            parts += ["", "Outlines of the selected files:"]
            budget = self.config.selected_chars
            for f in selected:
                outline = self.navigator.outline(f.path)
                if len(outline) <= budget:
                    parts.append(outline)
                    budget -= len(outline)
                else:
                    parts.append(f"{f.path} (outline left out to stay within the size "
                                 "limit; use the outline tool)")
        return "\n".join(parts)

    # ---- the view sent to the model -----------------------------------------------

    def view(self, messages: list[dict]) -> list[dict]:
        self.last_compaction = None
        sendable = self._render(messages)
        size = self.estimate(sendable)
        if size > self.config.max_tokens:
            before, removed_before = size, len(self.stubbed)
            self._compact(messages)
            sendable = self._render(messages)
            size = self.estimate(sendable)
            self.compactions += 1
            note = (f"History shortened to stay within {self.config.max_tokens} tokens: "
                    f"{len(self.stubbed) - removed_before} more older results replaced by a "
                    f"summary ({len(self.stubbed)} in total), about {before} -> {size} tokens")
            if self.overview_removed:
                note += "; repository overview removed"
            if size > self.config.max_tokens:
                note += ". Still over the limit: only the trusted messages and the last result are left"
            self.last_compaction = note
        self.largest_request = max(self.largest_request, size)
        return sendable

    def estimate(self, sendable: list[dict]) -> int:
        """Tokens for messages plus tool definitions, as the Ollama client estimates them."""
        return len(json.dumps({"messages": sendable, "tools": self.tool_schemas})) // CHARS_PER_TOKEN

    def stats(self) -> dict:
        return {
            "max_tokens": self.config.max_tokens,
            "largest_request_tokens": self.largest_request,
            "compactions": self.compactions,
            "results_shortened": len(self.stubbed),
            "overview_removed": self.overview_removed,
        }

    def wrap(self, content: str, source: str) -> str:
        """Mark text as untrusted. Copies of the markers inside it are defused."""
        source = re.sub(r"[^A-Za-z0-9_ -]", "", str(source))[:40] or "unknown"
        safe = (content.replace(f"<<<end {self.nonce}", "<<<end (defused)")
                       .replace(f"<<<untrusted {self.nonce}", "<<<untrusted (defused)"))
        return f"<<<untrusted {self.nonce} source={source}>>>\n{safe}\n<<<end {self.nonce}>>>"

    # ---- internals --------------------------------------------------------------

    def _render(self, messages: list[dict]) -> list[dict]:
        last_stub = max(self.stubbed, default=-1)
        summary_after = self._insert_summary_after(messages)
        out = []
        for i, message in enumerate(messages):
            sent = {k: v for k, v in message.items() if k not in HARNESS_KEYS}
            if i < self.pinned:
                pass  # trusted: sent exactly as written
            elif message.get("harness_untrusted"):
                if self.overview_removed:
                    sent["content"] = ("[The repository overview was removed to stay within the "
                                       "size limit. Use list_files, outline and find_symbol.]")
                else:
                    sent["content"] = self.wrap(message["content"], message["harness_untrusted"])
            elif message.get("role") == "tool":
                if i in self.stubbed:
                    sent["content"] = self._stub(message)
                else:
                    sent["content"] = self.wrap(message.get("content", ""),
                                                message.get("tool_name", "tool"))
            elif message.get("role") == "assistant" and i < last_stub and sent.get("tool_calls"):
                sent["tool_calls"] = [_shrink_call(c) for c in sent["tool_calls"]]
            out.append(sent)
            if self.stubbed and i == summary_after:
                out.append({"role": "user", "content": self._summary(messages)})
        return out

    def _insert_summary_after(self, messages: list[dict]) -> int:
        for i, m in enumerate(messages):
            if m.get("harness_untrusted"):
                return i
        return self.pinned - 1

    def _compact(self, messages: list[dict]) -> None:
        """Shorten the history in three rounds, each only if still needed.

        1. Results older than the newest keep_recent_results, oldest first,
           down to COMPACT_TO of the limit (room to grow, so the start of the
           conversation stays stable for the next requests).
        2. Only if still over the limit: newer results, always keeping the last.
        3. Only if still over the limit: the repository overview.
        """
        limit = self.config.max_tokens
        target = int(limit * COMPACT_TO)
        size = lambda: self.estimate(self._render(messages))
        tools = [i for i, m in enumerate(messages) if m.get("role") == "tool" and i not in self.stubbed]
        keep = self.config.keep_recent_results
        older = tools[:-keep] if keep else tools
        newer = [i for i in tools[:-1] if i not in older]  # the last result is always kept

        for i in older:
            if size() <= target:
                return
            self.stubbed.add(i)
        for i in newer:
            if size() <= limit:
                return
            self.stubbed.add(i)
        if size() > limit:
            self.overview_removed = True

    @staticmethod
    def _stub(message: dict) -> str:
        step = message.get("harness_step") or {}
        what = step.get("tool") or "tool"
        if step.get("target"):
            what += f" {step['target']}"
        return (f"[Result of step {step.get('step', '?')} ({what}) removed to stay within the "
                "size limit. See the summary of earlier steps; call the tool again if you need it.]")

    def _summary(self, messages: list[dict]) -> str:
        """Trusted summary of the shortened steps, built only from harness records.

        It names tools, checked paths, check names and statuses, never text
        from the repository or free text from the model.
        """
        inspected, edited, checks, other, problems = [], {}, [], {}, 0
        for i in sorted(self.stubbed):
            step = messages[i].get("harness_step") or {}
            tool, target = step.get("tool"), step.get("target")
            if not step.get("ok"):
                problems += 1
            elif tool == "edit_file":
                edited[target] = edited.get(target, 0) + 1
            elif tool == "run_check":
                checks.append(f"{target} {str(step.get('status', '')).upper()} (step {step.get('step')})")
            elif target and tool in ("read_file", "outline", "dependencies", "list_files", "git_diff"):
                if target not in inspected:
                    inspected.append(target)
            else:
                other[tool] = other.get(tool, 0) + 1
        lines = ["Summary of earlier steps whose results were removed to stay within the size "
                 "limit (written by the harness):"]
        if inspected:
            lines.append("- Read or inspected: " + ", ".join(inspected))
        if edited:
            lines.append("- Edited: " + ", ".join(f"{p} ({n}x)" for p, n in edited.items()))
        if checks:
            lines.append("- Checks: " + ", ".join(checks))
        if other:
            lines.append("- Other tools: " + ", ".join(f"{t} ({n}x)" for t, n in other.items()))
        if problems:
            lines.append(f"- Refused or failed requests: {problems}")
        lines.append("Your edits are still in the files: use git_status and git_diff to see them.")
        return "\n".join(lines)


def _shrink_call(call: dict) -> dict:
    """Replace the long text arguments of an old edit_file call by their length."""
    function = call.get("function", {})
    args = function.get("arguments")
    if function.get("name") != "edit_file" or not isinstance(args, dict):
        return call
    shrunk = {k: (f"[{len(v)} characters, shortened]" if k in ("old", "new") and isinstance(v, str)
                  else v) for k, v in args.items()}
    return {**call, "function": {**function, "arguments": shrunk}}
