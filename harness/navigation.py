"""Language-aware navigation: symbols, file outlines, module dependencies, Git state.

Python files are parsed with the standard library's ast module. Parsing reads
the source as data; nothing from the repository is imported or executed.
All paths go through RepositoryTools, so the same path checks apply as for
reading files. Git runs read-only commands with the safe settings from
workspace.py.

Module names are worked out from the folder layout: a file belongs to a
package as long as its folder contains __init__.py. So
src/allocation/domain/model.py is the module allocation.domain.model.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from harness.repo_tools import RepositoryTools, ToolError
from harness.workspace import WorkspaceError, changed_files, git

MAX_SYMBOL_RESULTS = 50
MAX_SOURCE_CHARS = 100  # of a variable's value shown in results


@dataclass(frozen=True)
class Symbol:
    path: str  # relative to the repository root
    line: int
    kind: str  # class, function, method, field, variable
    qualified_name: str  # e.g. Product.allocate
    detail: str  # signature, bases or value

    def describe(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} {self.qualified_name}{self.detail}"


@dataclass
class ModuleInfo:
    path: str
    name: str  # dotted module name
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # dotted names, as resolved candidates
    error: str | None = None  # set when the file could not be parsed


class CodeNavigator:
    def __init__(self, tools: RepositoryTools):
        self.tools = tools
        self.root = tools.root

    # ---- tools -------------------------------------------------------------

    def find_symbol(self, name: str) -> str:
        """Definitions whose name, or qualified name, is exactly name."""
        if not isinstance(name, str) or not name.strip():
            raise ToolError("Symbol name must be a non-empty string, such as Batch or Product.allocate.")
        name = name.strip()
        modules = self.modules()
        found = [s for m in modules.values() for s in m.symbols
                 if s.qualified_name == name or s.qualified_name.split(".")[-1] == name]
        lines = [s.describe() for s in found[:MAX_SYMBOL_RESULTS]]
        if len(found) > MAX_SYMBOL_RESULTS:
            lines.append(f"[results shortened: {len(found) - MAX_SYMBOL_RESULTS} more not shown]")
        if not lines:
            lines.append(f"No definition named {name!r}. Names are case-sensitive; "
                         "use search to find other uses of the text.")
        return "\n".join(lines + self._parse_notes(modules))

    def outline(self, path: str) -> str:
        """The classes, functions, fields and variables of one Python file."""
        module = self._module_for(path)
        header = f"{module.path} (module {module.name})"
        if module.error:
            return f"{header}\nCould not parse this file: {module.error}"
        if not module.symbols:
            return f"{header}\n(no classes, functions or variables)"
        lines = [header]
        for symbol in module.symbols:
            depth = symbol.qualified_name.count(".")
            label = symbol.qualified_name.split(".")[-1]
            lines.append(f"{'  ' * depth}{symbol.line}: {symbol.kind} {label}{symbol.detail}")
        return "\n".join(lines)

    def dependencies(self, path: str) -> str:
        """Which repository modules a file imports, and which import it."""
        modules = self.modules()
        target = self._module_for(path, modules)
        by_name = {m.name: m for m in modules.values()}

        imports_here = sorted({self._resolve_import(name, by_name) for name in target.imports} - {None})
        imports_here = [p for p in imports_here if p != target.path]
        outside = sorted({name.split(".")[0] for name in target.imports
                          if self._resolve_import(name, by_name) is None})
        imported_by = sorted(
            m.path for m in modules.values()
            if m.path != target.path
            and target.path in {self._resolve_import(n, by_name) for n in m.imports}
        )

        lines = [f"{target.path} (module {target.name})"]
        if target.error:
            lines.append(f"Could not parse this file: {target.error}")
        lines.append("Imports from this repository:")
        lines.extend(f"  {p}" for p in imports_here or ["(none)"])
        lines.append("Other imports: " + (", ".join(outside) or "(none)"))
        lines.append("Imported by:")
        lines.extend(f"  {p}" for p in imported_by or ["(none)"])
        return "\n".join(lines + self._parse_notes(modules, skip=target.path))

    def git_status(self) -> str:
        """Files changed since the starting commit. Read-only."""
        try:
            head = git(self.root, "rev-parse", "--short=12", "HEAD").strip()
            files = changed_files(self.root)
        except WorkspaceError as exc:
            raise ToolError(f"Git state is not available: {exc}") from None
        lines = [f"Starting commit: {head}. Changes are not committed; the harness reviews them."]
        if not files:
            lines.append("No files changed yet.")
        else:
            lines.append("Changed files (M modified, D deleted, ?? new):")
            lines.extend(f"  {status:<2} {path}" for status, path in files)
        return "\n".join(lines)

    def git_diff(self, path: str = "") -> str:
        """Changes since the starting commit, for the whole copy or one path. Read-only."""
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if path:
            target = self.tools.resolve(path)  # same path checks as the file tools
            args += ["--", target.relative_to(self.root).as_posix() or "."]
        try:
            output = git(self.root, *args)
        except WorkspaceError as exc:
            raise ToolError(f"Git diff is not available: {exc}") from None
        return output if output.strip() else "No changes" + (f" in {path}." if path else ".")

    # ---- parsing -----------------------------------------------------------

    def modules(self) -> dict[str, ModuleInfo]:
        """Every Python file in the repository, parsed. Keyed by relative path."""
        modules = {}
        for file in self.tools.files("."):
            if file.suffix == ".py":
                info = self._parse(file)
                modules[info.path] = info
        return modules

    def _module_for(self, path: str, modules: dict[str, ModuleInfo] | None = None) -> ModuleInfo:
        file = self.tools.resolve(path)
        if not file.is_file():
            raise ToolError(f"Not a file: {path}")
        if file.suffix != ".py":
            raise ToolError(f"Not a Python file: {path}. Use read_file for other files.")
        rel = file.relative_to(self.root).as_posix()
        if modules is not None and rel in modules:
            return modules[rel]
        return self._parse(file)

    def _parse(self, file: Path) -> ModuleInfo:
        rel = file.relative_to(self.root).as_posix()
        info = ModuleInfo(path=rel, name=self._module_name(file))
        try:
            source = file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            line = getattr(exc, "lineno", None)
            info.error = f"{type(exc).__name__}" + (f" at line {line}" if line else "") + f": {exc}"
            return info
        package = info.name if file.name == "__init__.py" else info.name.rpartition(".")[0]
        info.symbols = list(_symbols(tree, rel))
        info.imports = list(_imports(tree, package))
        return info

    def _module_name(self, file: Path) -> str:
        """src/allocation/domain/model.py -> allocation.domain.model"""
        parts = [] if file.name == "__init__.py" else [file.stem]
        folder = file.parent
        while folder != self.root and (folder / "__init__.py").is_file():
            parts.insert(0, folder.name)
            folder = folder.parent
        return ".".join(parts) or file.stem

    @staticmethod
    def _resolve_import(name: str, by_name: dict[str, ModuleInfo]) -> str | None:
        """The repository file for an imported name, trying the longest module prefix."""
        parts = name.split(".")
        for end in range(len(parts), 0, -1):
            module = by_name.get(".".join(parts[:end]))
            if module is not None:
                return module.path
        return None

    @staticmethod
    def _parse_notes(modules: dict[str, ModuleInfo], skip: str = "") -> list[str]:
        broken = [m for m in modules.values() if m.error and m.path != skip]
        return [f"Note: could not parse {m.path} ({m.error}); its definitions are missing."
                for m in broken]


# ---- reading the syntax tree ----------------------------------------------

def _symbols(tree: ast.Module, rel: str):
    for node in tree.body:
        yield from _definitions(node, rel, prefix="", in_class=False)


def _definitions(node, rel: str, prefix: str, in_class: bool):
    if isinstance(node, ast.ClassDef):
        name = prefix + node.name
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        decorators = "".join(f" @{ast.unparse(d)}" for d in node.decorator_list)
        detail = (f"({bases})" if bases else "") + (f"  [{decorators.strip()}]" if decorators else "")
        yield Symbol(rel, node.lineno, "class", name, detail)
        for child in node.body:
            yield from _definitions(child, rel, prefix=name + ".", in_class=True)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        kind = "method" if in_class else "function"
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        prefix_async = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        yield Symbol(rel, node.lineno, prefix_async + kind, prefix + node.name,
                     f"({ast.unparse(node.args)}){returns}")
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        value = f" = {_short(ast.unparse(node.value))}" if node.value else ""
        yield Symbol(rel, node.lineno, "field" if in_class else "variable",
                     prefix + node.target.id, f": {ast.unparse(node.annotation)}{value}")
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                yield Symbol(rel, node.lineno, "field" if in_class else "variable",
                             prefix + target.id, f" = {_short(ast.unparse(node.value))}")


def _imports(tree: ast.Module, package: str):
    """Dotted names this module imports, including submodule candidates.

    "from allocation.domain import model" yields allocation.domain.model; if no
    such module exists, resolving falls back to allocation.domain.
    Relative imports are made absolute using the module's own package.
    """
    for node in ast.walk(tree):  # also finds imports inside functions and if-blocks
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                keep = len(parts) - (node.level - 1)
                if keep < 0:
                    continue  # goes above the top package; cannot be resolved
                base = ".".join(parts[:keep] + ([node.module] if node.module else []))
            if not base:
                continue
            for alias in node.names:
                yield base if alias.name == "*" else f"{base}.{alias.name}"


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_SOURCE_CHARS else text[:MAX_SOURCE_CHARS] + "..."
