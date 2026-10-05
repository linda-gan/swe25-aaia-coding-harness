"""Repository tools: list, read, search and edit files inside one allowed root."""
from __future__ import annotations

import os
from pathlib import Path

BLOCKED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}

MAX_READ_BYTES = 100_000
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 50
MAX_LINE_CHARS = 200


class ToolError(Exception):
    """A tool request was refused or failed. The message is shown to the model."""


class RepositoryTools:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"Repository root does not exist: {self.root}")

    # ---- path checks -------------------------------------------------------

    def _resolve(self, rel_path: str) -> Path:
        """Turn a path from the model into a safe absolute path, or refuse."""
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ToolError("Path must be a non-empty string.")
        if "\x00" in rel_path:
            raise ToolError("Path contains a null byte.")
        if Path(rel_path).is_absolute():
            raise ToolError(f"Absolute paths are not allowed: {rel_path}")

        # resolve() follows symlinks and removes "..", so a symlink or
        # "../" that points outside the root ends up outside and is refused.
        resolved = (self.root / rel_path).resolve()
        if not resolved.is_relative_to(self.root):
            raise ToolError(f"Path is outside the repository: {rel_path}")
        if self._is_blocked(resolved):
            raise ToolError(f"Access to this path is not allowed: {rel_path}")
        return resolved

    def _is_blocked(self, absolute: Path) -> bool:
        return any(part in BLOCKED_DIRS for part in absolute.relative_to(self.root).parts)

    def _rel(self, absolute: Path) -> str:
        return absolute.relative_to(self.root).as_posix()

    def _iter_files(self, start: Path):
        """Yield allowed files under start, in a stable order."""
        if start.is_file():
            yield start
            return
        for dirpath, dirnames, filenames in os.walk(start):  # does not follow symlinked dirs
            dirnames[:] = sorted(d for d in dirnames if d not in BLOCKED_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                # Skip file symlinks that point outside the root.
                if path.resolve().is_relative_to(self.root):
                    yield path

    # ---- tools -------------------------------------------------------------

    def list_files(self, path: str = ".") -> str:
        start = self._resolve(path)
        if not start.exists():
            raise ToolError(f"No such file or directory: {path}")
        files = [self._rel(p) for p in self._iter_files(start)]
        shown = files[:MAX_LIST_ENTRIES]
        result = "\n".join(shown) if shown else "(no files)"
        if len(files) > len(shown):
            result += f"\n[list shortened: {len(files) - len(shown)} more files not shown]"
        return result

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Not a file: {path}")
        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError(
                f"File is too large to read ({size} bytes, limit {MAX_READ_BYTES}). "
                "Use search to find the part you need."
            )
        try:
            with open(target, encoding="utf-8", newline="") as f:
                return f.read()
        except UnicodeDecodeError:
            raise ToolError(f"Not a UTF-8 text file: {path}") from None

    def search(self, query: str, path: str = ".") -> str:
        if not isinstance(query, str) or not query:
            raise ToolError("Search query must be a non-empty string.")
        start = self._resolve(path)
        if not start.exists():
            raise ToolError(f"No such file or directory: {path}")

        matches = []
        for file in self._iter_files(start):
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue  # skip binary or unreadable files
            for number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append(f"{self._rel(file)}:{number}: {line.strip()[:MAX_LINE_CHARS]}")
                    if len(matches) >= MAX_SEARCH_RESULTS:
                        matches.append(f"[search shortened: stopped at {MAX_SEARCH_RESULTS} matches]")
                        return "\n".join(matches)
        return "\n".join(matches) if matches else f"No matches for {query!r}."

    def edit_file(self, path: str, old: str, new: str) -> str:
        """Replace exactly one occurrence of old with new."""
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"Not a file: {path}")
        if not isinstance(old, str) or not old:
            raise ToolError("old must be a non-empty string copied exactly from the file.")
        if not isinstance(new, str):
            raise ToolError("new must be a string.")
        if old == new:
            raise ToolError("old and new are identical; nothing to change.")

        content = self.read_file(path)
        count = content.count(old)
        if count == 0:
            raise ToolError(
                f"Text not found in {path}. Read the file again and copy the exact text, "
                "including indentation."
            )
        if count > 1:
            raise ToolError(
                f"Text appears {count} times in {path}. Include more surrounding lines "
                "so it matches exactly once."
            )

        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(content.replace(old, new, 1))
        return f"Edited {self._rel(target)}: replaced 1 occurrence."
