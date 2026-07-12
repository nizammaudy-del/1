"""Graph-scoped context builder — a Graphify-style proxy in front of the LLM.

Instead of reading every file in a project, this module builds (or loads
from cache) a directed import graph over the project's source files — nodes
are files, edges are internal (project-local) imports/includes. From a
user-supplied entrypoint, it walks the graph with a depth-bounded BFS and
concatenates the full contents of every file reached into one context
string, ready to be injected into an LLM prompt alongside the user's query.

Python gets full AST-based import resolution (handles relative imports,
``from X import Y`` submodule-vs-attribute ambiguity, etc.). JS/TS, Rust,
and C/C++ get a deliberately simple regex scan for their own import/include
syntax — good enough to catch the common, literal cases (``import x from
'./y'``, ``mod foo;``, ``#include "foo.h"``) but not a real parser. Known
unresolved cases, all of which just yield *fewer* edges (never wrong ones):
JS/TS dynamic ``import('./x')`` and bundler path aliases (``@/x``), Rust
``use crate::`` paths (only ``mod`` declarations are file-graph edges and
those are caught), and macro- or ``<system>``-style C includes. A miss here
just means that file's edges are incomplete, not wrong — same fail-open
philosophy as the Python path's entrypoint detection. Note the regex
scanners are not comment-aware, so an import mentioned inside a comment or a
disabled ``#if 0`` block can add a spurious edge; that only ever *widens*
the neighborhood (keeps an extra file), never drops a real one.

Ten more languages (Go, Java, C#, Ruby, PHP, Kotlin, Scala, Dart, Lua, Zig —
see the "Tree-sitter multi-language support" section) route through
`tree_sitter_language_pack` instead of a hand-written regex, the same
optional ``[code]`` extra `headroom.transforms.code_compressor` already
depends on. Each import statement resolves through one of two verified-safe
strategies, never a guess: an explicit relative path (Ruby
``require_relative``, PHP ``require``/``include``, Dart/Zig relative
imports) resolves like JS/TS does; a dotted qualified name (Java, Kotlin,
Scala, C# ``using``, Lua ``require("a.b")``) resolves only when it maps to a
UNIQUE file by path suffix across the whole project — an ambiguous or
unresolvable name (an external package, a static-import member access, a
nested class) yields no edge rather than a wrong one. Without the ``[code]``
extra installed, these ten languages' files still become graph nodes (so
BFS/cache invariants hold) but contribute zero edges — degrades gracefully,
never raises. Not attempted at all (module-based imports with no reliable
file-level resolution, e.g. Swift ``import Foundation``, Elixir ``alias
Foo.Bar``) or out of scope entirely (infrastructure/config formats with
their own cross-file references — Terraform module sources, Dockerfile
``FROM``, CMake ``include()`` — which aren't source-code import graphs and
would need their own resolution semantics).

External/third-party imports are not resolvable to a project file and are
simply not added as edges — the graph only ever contains project-internal
files.

No token-limiting, truncation, or summarization happens here by design: the
full content of every reachable file is returned verbatim. Bounding total
size is the caller's (or the LLM API's) responsibility.

The graph is cached under ``headroom.paths.graph_context_cache_dir()``,
keyed by content hashes of every supported source file under the project
root. Any file addition, removal, or edit invalidates the cache and
triggers a rebuild; an unchanged tree is loaded straight from cache. Writes
to that cache file are atomic (temp file + rename) so a reader never
observes a half-written file, even with multiple Headroom processes
sharing the same cache path.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from headroom.paths import graph_context_cache_dir

logger = logging.getLogger(__name__)

DEFAULT_MAX_DEPTH = 2

_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "target",  # Rust/Java build output
    "dist",
    "build",
}

# ---------------------------------------------------------------------------
# Multi-language support. Python uses real AST parsing (see `_extract_imports`
# below); JS/TS, Rust, and C/C++ go through the regex-based extractors in the
# "Multi-language regex fallback" section further down; everything else goes
# through the tree-sitter-backed extractors in the "Tree-sitter multi-
# language support" section after that. Keep this in sync with the
# (deliberately separate, lazily-imported) entrypoint-detection regex in
# `headroom.transforms.content_router` if you add a language here.
# ---------------------------------------------------------------------------
PYTHON_EXTENSIONS = frozenset({".py"})
JS_TS_EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})
RUST_EXTENSIONS = frozenset({".rs"})
C_CPP_EXTENSIONS = frozenset({".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"})

# Tree-sitter-backed languages (see "Tree-sitter multi-language support"
# section below). Unlike the four families above, these are extracted via
# the optional `[code]` extra's tree-sitter grammars rather than a hand-
# written regex per language — the same `tree_sitter_language_pack`
# dependency `headroom.transforms.code_compressor` already uses. Degrades to
# zero edges (never a crash) when the extra isn't installed, same fail-open
# contract as every other extractor in this module.
GO_EXTENSIONS = frozenset({".go"})
JAVA_EXTENSIONS = frozenset({".java"})
CSHARP_EXTENSIONS = frozenset({".cs"})
RUBY_EXTENSIONS = frozenset({".rb"})
PHP_EXTENSIONS = frozenset({".php"})
KOTLIN_EXTENSIONS = frozenset({".kt", ".kts"})
SCALA_EXTENSIONS = frozenset({".scala"})
DART_EXTENSIONS = frozenset({".dart"})
LUA_EXTENSIONS = frozenset({".lua"})
ZIG_EXTENSIONS = frozenset({".zig"})

TREE_SITTER_EXTENSIONS = (
    GO_EXTENSIONS
    | JAVA_EXTENSIONS
    | CSHARP_EXTENSIONS
    | RUBY_EXTENSIONS
    | PHP_EXTENSIONS
    | KOTLIN_EXTENSIONS
    | SCALA_EXTENSIONS
    | DART_EXTENSIONS
    | LUA_EXTENSIONS
    | ZIG_EXTENSIONS
)

SOURCE_EXTENSIONS = (
    PYTHON_EXTENSIONS | JS_TS_EXTENSIONS | RUST_EXTENSIONS | C_CPP_EXTENSIONS | TREE_SITTER_EXTENSIONS
)


@dataclass
class ProjectGraph:
    """Directed import graph for a project: nodes are files, edges are imports.

    Paths in ``edges`` and ``file_hashes`` are project-root-relative, POSIX-
    style strings so the graph is portable across machines/OSes.
    """

    root: Path
    edges: dict[str, list[str]]
    file_hashes: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "edges": self.edges,
            "file_hashes": self.file_hashes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ProjectGraph:
        return cls(
            root=Path(data["root"]),
            edges=data["edges"],
            file_hashes=data["file_hashes"],
        )


@dataclass
class GraphContextResult:
    """Result of assembling graph-scoped context for an entrypoint + query."""

    entrypoint: str
    query: str
    max_depth: int
    files: list[str]
    context: str
    prompt: str = field(init=False)

    def __post_init__(self) -> None:
        self.prompt = f"{self.query}\n\n{self.context}" if self.query else self.context


def _cache_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def _cache_path(root: Path) -> Path:
    return graph_context_cache_dir() / f"{_cache_key(root)}.json"


def _hash_file(path: Path) -> str | None:
    """SHA-256 of the file's bytes, or None if it can't be read.

    Returns None (rather than raising) when the file vanished or became
    unreadable between directory enumeration and this call — a real race
    when an agent edits/deletes files while the proxy rebuilds the graph.
    Callers skip a None-hashed file, so a mid-build delete degrades to
    "that file isn't in this graph revision" instead of crashing the build.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _iter_source_files(root: Path) -> Iterator[Path]:
    """Every supported source file under ``root``, any of the languages in
    `SOURCE_EXTENSIONS`, skipping ignored/build/vendor directories.

    Uses ``os.walk`` (not ``Path.rglob``) for two reasons that matter on the
    supported Python range (>=3.10):

    * **Symlink safety.** ``os.walk`` defaults to ``followlinks=False`` on
      every version, so a directory symlink cycle can never make this loop
      forever. ``Path.rglob("*")`` expands to a ``**`` glob that *does*
      follow symlinks before Python 3.13 (the ``recurse_symlinks=False``
      default only exists from 3.13 on) — a symlink loop there would hang
      the build. ``os.walk`` sidesteps that entirely.
    * **Pruning.** Ignored/vendor dirs (``node_modules``, ``.git``,
      ``target``, …) are pruned from ``dirnames`` in place, so the walk
      never descends into them at all — instead of walking the whole tree
      and filtering per file. On a real repo with a big ``node_modules``
      that is the difference between milliseconds and seconds.
    """

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for name in filenames:
            if Path(name).suffix.lower() in SOURCE_EXTENSIONS:
                candidate = Path(dirpath) / name
                if candidate.is_file():
                    yield candidate


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write ``data`` as JSON to ``path`` without a reader ever seeing a
    partial file — write to a uniquely-named temp file in the same
    directory, then rename over the target. `Path.replace` maps to a single
    atomic filesystem rename on both POSIX and Windows, so concurrent
    Headroom processes/threads racing to refresh the same cache file always
    resolve to "last writer wins, but always a complete, valid JSON file" —
    never a half-written one.

    Verified under real concurrency (many threads racing to refresh the
    same cache path): Windows' ``MoveFileEx`` (what ``os.replace`` calls)
    can transiently raise ``PermissionError`` when another thread/process
    has the target open at the exact same instant, even though the rename
    itself is atomic once it succeeds — POSIX rename has no such window.
    Retried a few times with a short backoff before giving up; harmless
    (never triggers) on POSIX.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        last_exc: OSError | None = None
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.02 * (attempt + 1))
        raise last_exc  # type: ignore[misc]
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _current_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for f in _iter_source_files(root):
        h = _hash_file(f)
        if h is not None:  # skip files that vanished mid-scan (see _hash_file)
            result[f.relative_to(root).as_posix()] = h
    return result


def _file_for(candidate: Path) -> Path | None:
    """Return ``candidate.py`` or ``candidate/__init__.py`` if either exists."""

    for option in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        if option.is_file():
            return option
    return None


def _resolve_absolute(root: Path, module: str) -> Path | None:
    """Resolve a plain ``import a.b.c`` to a project-internal file, or None."""

    if not module:
        return None
    return _file_for(root.joinpath(*module.split(".")))


def _base_path(root: Path, importer: Path, module: str, level: int) -> Path | None:
    """Directory/module path implied by the ``X`` in ``from X import ...``.

    May point at a location that doesn't exist (e.g. an external package) —
    callers must check with :func:`_file_for` before treating it as a file.
    """

    if level > 0:
        # Relative import: level=1 is the importer's own package.
        base = importer.parent
        for _ in range(level - 1):
            base = base.parent
        return base.joinpath(*module.split(".")) if module else base
    if not module:
        return None
    return root.joinpath(*module.split("."))


def _resolve_from_import(root: Path, importer: Path, node: ast.ImportFrom) -> list[Path]:
    """Resolve ``from X import a, b`` — submodules of X take priority over X itself.

    ``from pkg import util`` should resolve to ``pkg/util.py`` (a submodule)
    rather than ``pkg/__init__.py`` (importing an attribute named ``util``
    from the package) whenever the submodule actually exists on disk.
    """

    base = _base_path(root, importer, node.module or "", node.level)
    if base is None:
        return []

    submodules = [f for alias in node.names if (f := _file_for(base / alias.name)) is not None]
    if submodules:
        return submodules

    module_file = _file_for(base)
    return [module_file] if module_file is not None else []


def _extract_imports(root: Path, file_path: Path) -> list[Path]:
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []

    resolved: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_absolute(root, alias.name)
                if target is not None:
                    resolved.append(target)
        elif isinstance(node, ast.ImportFrom):
            resolved.extend(_resolve_from_import(root, file_path, node))
    return resolved


# ---------------------------------------------------------------------------
# Multi-language regex fallback (JS/TS, Rust, C/C++). Deliberately simple:
# one regex per language's import/include syntax, resolved against the
# filesystem the same way the Python path does (relative-first, verified by
# `is_file()` before ever being added as an edge). No bundler alias
# resolution, no Rust `use crate::` path resolution, no macro expansion —
# those need real per-language tooling; this catches the literal, common
# case cheaply, and silently produces fewer edges (not wrong ones) when it
# can't resolve something.
# ---------------------------------------------------------------------------

# `import ... from "x"` / `export ... from "x"` / `require("x")` / dynamic
# `import("x")`. The dynamic-import alternation was added after an audit
# found `import('./x')` silently dropped; captured into group 3.
_JS_IMPORT_RE = re.compile(
    r"""(?:^|[;\s])(?:import|export)(?:[^'";]*?\bfrom\s*)?['"]([^'"]+)['"]"""
    r"""|require\(\s*['"]([^'"]+)['"]\s*\)"""
    r"""|import\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)
_RUST_MOD_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.MULTILINE)
_C_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)

_JS_RESOLVE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Comment strippers — run before the import/include scan so a spec mentioned
# inside a comment can't create a spurious edge (an audit found both
# `// import x from "./dead"` in JS and `#if 0 ... #include "dead.h"` in C
# producing false edges). Deliberately lightweight, NOT a real lexer:
# `/* */` uses non-greedy DOTALL, `//` runs to end of line. A `//` sitting
# inside a string literal would be over-stripped, but that only ever
# truncates a NON-import tail of the line (a relative import spec never
# contains `//`), so it can't drop a real edge — same fail-open direction
# as everything else here.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
# Best-effort removal of literal `#if 0 ... #endif` blocks (non-nested) so a
# disabled C include isn't scanned. Nested/`#elif` cases fall through — they
# only ever add an edge, never drop one.
_C_IF0_RE = re.compile(r"^\s*#\s*if\s+0\b.*?^\s*#\s*endif\b[^\n]*", re.DOTALL | re.MULTILINE)


def _strip_c_like_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _read_text_lenient(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _resolve_js_import(importer: Path, spec: str) -> Path | None:
    """Resolve a relative JS/TS import specifier (``./x``, ``../y/z``).

    Bare specifiers (package names, path aliases like ``@/x``) are external
    or need bundler config to resolve — deliberately not attempted here.
    """
    if not spec.startswith("."):
        return None
    base = importer.parent / spec
    if base.is_file():
        return base
    for suf in _JS_RESOLVE_SUFFIXES:
        candidate = base.with_name(base.name + suf)
        if candidate.is_file():
            return candidate
    if base.is_dir():
        for suf in _JS_RESOLVE_SUFFIXES:
            candidate = base / f"index{suf}"
            if candidate.is_file():
                return candidate
    return None


def _extract_js_imports(file_path: Path) -> list[Path]:
    text = _read_text_lenient(file_path)
    if text is None:
        return []
    text = _strip_c_like_comments(text)  # kill comment-mention false positives
    resolved: list[Path] = []
    for match in _JS_IMPORT_RE.finditer(text):
        spec = match.group(1) or match.group(2) or match.group(3)  # static / require / dynamic
        if not spec:
            continue
        target = _resolve_js_import(file_path, spec)
        if target is not None:
            resolved.append(target)
    return resolved


def _resolve_rust_mod(importer: Path, name: str) -> Path | None:
    """Resolve ``mod name;`` to ``name.rs`` or ``name/mod.rs`` next to the importer.

    This is the actual file-graph edge in Rust (``mod`` is what pulls a file
    into the crate); ``use`` only imports symbols that a ``mod`` already
    declared, so it's not a separate edge and isn't scanned for.
    """
    sibling = importer.parent / f"{name}.rs"
    if sibling.is_file():
        return sibling
    nested = importer.parent / name / "mod.rs"
    if nested.is_file():
        return nested
    return None


def _extract_rust_imports(file_path: Path) -> list[Path]:
    text = _read_text_lenient(file_path)
    if text is None:
        return []
    resolved = []
    for match in _RUST_MOD_RE.finditer(text):
        target = _resolve_rust_mod(file_path, match.group(1))
        if target is not None:
            resolved.append(target)
    return resolved


def _resolve_c_include(root: Path, importer: Path, spec: str) -> Path | None:
    """Resolve ``#include "spec"`` (quoted/local only — ``<system.h>`` skipped)."""
    local = importer.parent / spec
    if local.is_file():
        return local
    from_root = root / spec
    if from_root.is_file():
        return from_root
    return None


def _extract_c_includes(root: Path, file_path: Path) -> list[Path]:
    text = _read_text_lenient(file_path)
    if text is None:
        return []
    text = _C_IF0_RE.sub("", _strip_c_like_comments(text))  # drop comments + `#if 0` blocks
    resolved = []
    for match in _C_INCLUDE_RE.finditer(text):
        target = _resolve_c_include(root, file_path, match.group(1))
        if target is not None:
            resolved.append(target)
    return resolved


# ---------------------------------------------------------------------------
# Tree-sitter multi-language support (Go, Java, C#, Ruby, PHP, Kotlin, Scala,
# Dart, Lua, Zig). Unlike the regex-per-language section above, these route
# through `tree_sitter_language_pack` -- the same optional `[code]` extra
# `headroom.transforms.code_compressor` already depends on -- so adding a
# language here is a query against a real grammar, not a hand-written regex.
# Degrades to zero edges (never a crash, never a raised ImportError) when the
# extra isn't installed: `_ts_parse` returns None and every extractor below
# short-circuits on that, same fail-open contract as the rest of this module.
# ---------------------------------------------------------------------------


def _ts_parse(language: str, file_path: Path) -> Any | None:
    """Parse ``file_path`` with the tree-sitter grammar for ``language``.

    Lazily imports `headroom.transforms.code_compressor`'s thread-local
    parser cache rather than importing tree-sitter directly, so this module
    keeps its own "no eager heavy imports" contract (see the module
    docstring) and the two callers share one parser-cache implementation
    instead of two. Returns None -- not a raised exception -- for every
    failure mode (extra not installed, unreadable file, parse error): callers
    treat that identically to "this file has zero edges".
    """
    try:
        from .transforms.code_compressor import _get_parser, _tree_sitter_importable
    except Exception:  # noqa: BLE001
        return None
    if not _tree_sitter_importable():
        return None
    text = _read_text_lenient(file_path)
    if text is None:
        return None
    try:
        parser = _get_parser(language)
        return parser.parse(text.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _ts_find_statements(node: Any, types: Collection[str]) -> list[Any]:
    """Every descendant of ``node`` whose type is in ``types`` (full walk).

    Deliberately does NOT prune a matched subtree from further descent: for
    most of these grammars the target type only ever appears at the
    top level (an ``import_declaration`` never contains another), so pruning
    would be harmless there -- but Ruby/Lua's target type is the generic
    ``call``/``function_call`` node also used for ordinary method calls,
    which genuinely can nest one inside another (``foo(require_relative(...))``).
    Pruning would silently miss that case; a full walk never does.
    """
    found: list[Any] = []
    stack = list(node.children)
    while stack:
        child = stack.pop()
        if child.type in types:
            found.append(child)
        stack.extend(child.children)
    return found


def _ts_first_descendant_text(node: Any, types: Collection[str]) -> str | None:
    """BFS for the shallowest descendant of ``node`` whose type is in ``types``.

    BFS (not DFS) matters: several grammars nest a leaf of a type in the same
    family *inside* the node that actually holds the full text we want (e.g.
    Java's ``scoped_identifier`` for ``a.b.C`` contains further ``identifier``
    leaves for ``a``, ``b``, ``C`` individually) -- level-order search
    guarantees the outermost, complete match wins instead of a fragment.
    """
    queue: deque[Any] = deque(node.children)
    while queue:
        child = queue.popleft()
        if child.type in types:
            return str(child.text.decode("utf-8", "replace"))
        queue.extend(child.children)
    return None


def _build_qualified_index(file_hashes: dict[str, str]) -> dict[str, list[str]]:
    """Group already-known project-relative paths by extension.

    Built once per `build_graph` call from the hashes already collected
    while walking the tree (no extra filesystem pass), and consumed by
    `_resolve_qualified_name` for the languages whose imports are a dotted
    qualified name rather than a filesystem path (Java/Kotlin/Scala/C#/Lua).
    """
    index: dict[str, list[str]] = {}
    for rel in file_hashes:
        index.setdefault(Path(rel).suffix.lower(), []).append(rel)
    return index


def _resolve_qualified_name(root: Path, index: dict[str, list[str]], dotted: str, ext: str) -> Path | None:
    """Resolve a dotted qualified name (``com.acme.pkg.Util``) via a UNIQUE
    path-suffix match against the project's own files, never a first/naive hit.

    Java/Kotlin/Scala/C# don't declare their own source root in the import
    statement (unlike Python's project-root-relative absolute imports), so
    the same dotted name could in principle match a file at any depth.
    Requiring the match to be unique across the whole project is what keeps
    this in the "never wrong, only fewer" contract the rest of the module
    holds to: a static-import member access (``Other.thing``), a nested
    class (``Outer.Inner``), or a name that collides across two files all
    simply fail to resolve (no unique suffix match) rather than guessing and
    risking a wrong edge.
    """
    candidates = index.get(ext, [])
    if not candidates:
        return None
    target = dotted.replace(".", "/") + ext
    matches = [c for c in candidates if c == target or c.endswith("/" + target)]
    if len(matches) != 1:
        return None
    return root / matches[0]


def _resolve_relative_path_import(importer: Path, spec: str, suffixes: tuple[str, ...]) -> Path | None:
    """Resolve an explicit relative import spec (``./x``, ``../y/z``) against
    ``importer``'s own directory, trying ``suffixes`` in order when the spec
    has no extension of its own. Mirrors `_resolve_js_import`'s relative-path
    case for the tree-sitter-backed languages using the same convention.
    """
    if not spec.startswith("."):
        return None
    base = importer.parent / spec
    if base.is_file():
        return base
    for suf in suffixes:
        candidate = base.with_name(base.name + suf)
        if candidate.is_file():
            return candidate
    return None


_GO_MODULE_RE = re.compile(r"^\s*module\s+(\S+)", re.MULTILINE)


def _go_module_prefix(root: Path) -> str | None:
    """Read the module path declared in ``go.mod`` at the project root, if any.

    Go import paths are package (directory) paths rooted at the module name
    (e.g. ``module myproj`` + ``import "myproj/pkg/util"`` -> ``pkg/util/``),
    not a plain relative filesystem path -- this prefix is what turns an
    import spec into a project-relative directory. No ``go.mod`` (or no
    ``module`` line) means every Go import is left unresolved.
    """
    text = _read_text_lenient(root / "go.mod")
    if text is None:
        return None
    match = _GO_MODULE_RE.search(text)
    return match.group(1) if match else None


def _extract_go_imports(root: Path, file_path: Path, module_prefix: str | None) -> list[Path]:
    """Go imports resolve to a PACKAGE (directory), not a single file -- a Go
    package is every ``.go`` file in that directory. An import outside the
    project's own module prefix is a third-party/stdlib package and is left
    unresolved (external, same as everywhere else in this module).
    """
    if not module_prefix:
        return []
    tree = _ts_parse("go", file_path)
    if tree is None:
        return []
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, {"import_spec"}):
        spec = _ts_first_descendant_text(stmt, {"interpreted_string_literal_content"})
        if spec is None:
            continue
        if spec == module_prefix:
            rel_dir = ""
        elif spec.startswith(module_prefix + "/"):
            rel_dir = spec[len(module_prefix) + 1 :]
        else:
            continue  # external package (stdlib or third-party)
        pkg_dir = (root / rel_dir) if rel_dir else root
        if pkg_dir.is_dir():
            resolved.extend(sorted(p for p in pkg_dir.glob("*.go") if p.is_file()))
    return resolved


def _extract_dotted_imports(
    root: Path,
    file_path: Path,
    index: dict[str, list[str]],
    *,
    language: str,
    statement_types: Collection[str],
    name_types: Collection[str],
    exts: tuple[str, ...],
) -> list[Path]:
    """Shared extractor for a dotted-qualified-name import statement
    (``import a.b.C;``) resolved via `_resolve_qualified_name` -- Java and
    Kotlin (C# needs an extra pre-filter for ``using static``/alias forms so
    it isn't routed through here; Scala's grammar flattens the dotted chain
    differently and isn't a fit either -- see their own extractors below).
    ``exts`` lets one grammar's imports resolve against more than one file
    extension (Kotlin's ``.kt``/``.kts``).
    """
    tree = _ts_parse(language, file_path)
    if tree is None:
        return []
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, statement_types):
        dotted = _ts_first_descendant_text(stmt, name_types)
        if dotted is None:
            continue
        for ext in exts:
            target = _resolve_qualified_name(root, index, dotted, ext)
            if target is not None:
                resolved.append(target)
                break
    return resolved


def _extract_java_imports(root: Path, file_path: Path, index: dict[str, list[str]]) -> list[Path]:
    return _extract_dotted_imports(
        root,
        file_path,
        index,
        language="java",
        statement_types={"import_declaration"},
        name_types={"scoped_identifier", "identifier"},
        exts=(".java",),
    )


def _extract_kotlin_imports(root: Path, file_path: Path, index: dict[str, list[str]]) -> list[Path]:
    return _extract_dotted_imports(
        root,
        file_path,
        index,
        language="kotlin",
        statement_types={"import_header"},
        name_types={"identifier"},
        exts=(".kt", ".kts"),
    )


def _extract_csharp_imports(root: Path, file_path: Path, index: dict[str, list[str]]) -> list[Path]:
    """``using X.Y;`` imports a NAMESPACE (every type inside it), unlike
    Java/Kotlin/Scala's ``import a.b.ClassName;`` which names one class --
    so this resolves the dotted name to a DIRECTORY by unique path-suffix
    match (the common "namespace mirrors folder structure" .NET convention)
    and returns every ``.cs`` file directly inside it, the same
    package-is-a-directory model `_extract_go_imports` uses; nothing is
    added when no such directory exists or the match is ambiguous.
    ``using static Type;`` and ``using Alias = X;`` are skipped -- neither
    is a plain namespace import, and guessing which segment to drop risks a
    wrong edge rather than just a missing one.
    """
    tree = _ts_parse("csharp", file_path)
    if tree is None:
        return []
    dirs = sorted({Path(p).parent.as_posix() for p in index.get(".cs", [])})
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, {"using_directive"}):
        raw = stmt.text.decode("utf-8", "replace")
        body = raw[len("using") :].strip() if raw.startswith("using") else raw
        if body.startswith("static ") or "=" in body:
            continue
        dotted = _ts_first_descendant_text(stmt, {"qualified_name", "identifier"})
        if dotted is None:
            continue
        target = dotted.replace(".", "/")
        matches = [d for d in dirs if d == target or d.endswith("/" + target)]
        if len(matches) != 1:
            continue
        ns_dir = root / matches[0]
        if ns_dir.is_dir():
            resolved.extend(sorted(p for p in ns_dir.glob("*.cs") if p.is_file()))
    return resolved


_SCALA_IMPORT_BRACE_RE = re.compile(r"^(.*)\.\{([^}]*)\}$")


def _extract_scala_imports(root: Path, file_path: Path, index: dict[str, list[str]]) -> list[Path]:
    """Scala's grammar flattens ``import a.b.C`` into sibling ``identifier``/
    ``.`` tokens rather than one node holding the full dotted text (unlike
    Java/Kotlin), so this reads the statement's own raw text instead of
    walking for a name node. Multi-import ``pkg.{A, B}`` expands to both
    targets; a trailing wildcard (``pkg._`` Scala 2, ``pkg.*`` Scala 3) is a
    whole-package import with no single target file and is skipped, the same
    "no guess" rule as everywhere else in this module.
    """
    tree = _ts_parse("scala", file_path)
    if tree is None:
        return []
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, {"import_declaration"}):
        raw = stmt.text.decode("utf-8", "replace")
        body = raw[len("import") :].strip() if raw.startswith("import") else raw
        brace_match = _SCALA_IMPORT_BRACE_RE.match(body)
        if brace_match:
            base, members = brace_match.group(1), brace_match.group(2)
            dotted_candidates = [f"{base}.{m.strip()}" for m in members.split(",") if m.strip()]
        else:
            dotted_candidates = [body]
        for dotted in dotted_candidates:
            if not dotted or dotted.endswith("._") or dotted.endswith(".*"):
                continue
            target = _resolve_qualified_name(root, index, dotted, ".scala")
            if target is not None:
                resolved.append(target)
    return resolved


def _extract_ruby_imports(file_path: Path) -> list[Path]:
    """Only ``require_relative`` is resolved -- plain ``require "foo"``
    almost always names a gem/library on the load path, not a project file,
    and guessing wrong there would violate the "never wrong" contract.
    """
    tree = _ts_parse("ruby", file_path)
    if tree is None:
        return []
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, {"call"}):
        children = stmt.children
        if not children or children[0].type != "identifier":
            continue
        if children[0].text.decode("utf-8", "replace") != "require_relative":
            continue
        spec = _ts_first_descendant_text(stmt, {"string_content"})
        if spec is None:
            continue
        if not spec.startswith("."):
            spec = f"./{spec}"
        target = _resolve_relative_path_import(file_path, spec, (".rb",))
        if target is not None:
            resolved.append(target)
    return resolved


_PHP_INCLUDE_STATEMENT_TYPES = frozenset(
    {"require_expression", "require_once_expression", "include_expression", "include_once_expression"}
)


def _extract_php_imports(file_path: Path) -> list[Path]:
    """Only ``require``/``include`` (with a resolvable string literal) are
    handled. ``use App\\Services\\Thing;`` namespace imports follow PSR-4,
    which needs ``composer.json``'s autoload map to resolve correctly --
    guessing the directory layout would risk a wrong edge, so (matching Rust
    ``use crate::`` and Python's external-package imports) it's left alone.
    """
    tree = _ts_parse("php", file_path)
    if tree is None:
        return []
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, _PHP_INCLUDE_STATEMENT_TYPES):
        # `__DIR__ . "/foo.php"` concatenations are common; the LAST string
        # literal in the statement is the meaningful relative path either way.
        specs = [n.text.decode("utf-8", "replace") for n in _ts_find_statements(stmt, {"string_content"})]
        if not specs:
            continue
        spec = specs[-1].lstrip("/")
        if not spec.startswith("."):
            spec = f"./{spec}"
        target = _resolve_relative_path_import(file_path, spec, (".php",))
        if target is not None:
            resolved.append(target)
    return resolved


def _extract_dart_imports(file_path: Path) -> list[Path]:
    """Relative Dart imports are conventionally written WITHOUT a ``./``
    prefix (``import "helper.dart";``, unlike JS/TS) -- only ``package:``
    (pub package) and ``dart:`` (SDK) prefixes mark an external, unresolvable
    import; everything else is a path relative to the importing file.
    """
    tree = _ts_parse("dart", file_path)
    if tree is None:
        return []
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, {"import_specification"}):
        spec = _ts_first_descendant_text(stmt, {"string_literal"})
        if spec is None:
            continue
        spec = spec.strip("'\"")
        if spec.startswith("package:") or spec.startswith("dart:"):
            continue
        if not spec.startswith("."):
            spec = f"./{spec}"
        target = _resolve_relative_path_import(file_path, spec, (".dart",))
        if target is not None:
            resolved.append(target)
    return resolved


def _extract_lua_imports(root: Path, file_path: Path, index: dict[str, list[str]]) -> list[Path]:
    """``require("foo.bar")`` follows Lua's dot-per-directory module
    convention (``foo.bar`` -> ``foo/bar.lua``), resolved via the same
    suffix-uniqueness index as Java/Kotlin/Scala/C#. A spec that already
    looks like an explicit relative path (``require("./foo")``) is resolved
    directly instead.
    """
    tree = _ts_parse("lua", file_path)
    if tree is None:
        return []
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, {"function_call"}):
        children = stmt.children
        if not children or children[0].type != "identifier":
            continue
        if children[0].text.decode("utf-8", "replace") != "require":
            continue
        spec = _ts_first_descendant_text(stmt, {"string_content"})
        if spec is None:
            continue
        if spec.startswith("."):
            target = _resolve_relative_path_import(file_path, spec, (".lua",))
        else:
            target = _resolve_qualified_name(root, index, spec, ".lua")
        if target is not None:
            resolved.append(target)
    return resolved


def _extract_zig_imports(file_path: Path) -> list[Path]:
    """Only relative ``@import("./x.zig")`` specs resolve -- a bare spec like
    ``@import("std")`` is the standard library or a ``build.zig`` package
    dependency, neither resolvable from the source tree alone.
    """
    tree = _ts_parse("zig", file_path)
    if tree is None:
        return []
    resolved: list[Path] = []
    for stmt in _ts_find_statements(tree.root_node, {"SuffixExpr"}):
        children = stmt.children
        if not children or children[0].type != "BUILTINIDENTIFIER":
            continue
        if children[0].text.decode("utf-8", "replace") != "@import":
            continue
        match = re.search(r'"([^"]+)"', stmt.text.decode("utf-8", "replace"))
        if match is None:
            continue
        spec = match.group(1)
        if not spec.startswith("."):
            continue
        target = _resolve_relative_path_import(file_path, spec, (".zig",))
        if target is not None:
            resolved.append(target)
    return resolved


@dataclass
class _ImportResolveContext:
    """Whole-project context the tree-sitter extractors need but a plain
    per-file dispatch doesn't have: the qualified-name suffix index
    (Java/Kotlin/Scala/C#/Lua) and the Go module prefix. Built once per
    `build_graph` call, not once per file.
    """

    index: dict[str, list[str]]
    go_module_prefix: str | None


def _extract_imports_any(root: Path, file_path: Path, ctx: _ImportResolveContext) -> list[Path]:
    """Dispatch to the right extractor for ``file_path``'s extension."""
    suffix = file_path.suffix.lower()
    if suffix in PYTHON_EXTENSIONS:
        return _extract_imports(root, file_path)
    if suffix in JS_TS_EXTENSIONS:
        return _extract_js_imports(file_path)
    if suffix in RUST_EXTENSIONS:
        return _extract_rust_imports(file_path)
    if suffix in C_CPP_EXTENSIONS:
        return _extract_c_includes(root, file_path)
    if suffix in GO_EXTENSIONS:
        return _extract_go_imports(root, file_path, ctx.go_module_prefix)
    if suffix in JAVA_EXTENSIONS:
        return _extract_java_imports(root, file_path, ctx.index)
    if suffix in KOTLIN_EXTENSIONS:
        return _extract_kotlin_imports(root, file_path, ctx.index)
    if suffix in SCALA_EXTENSIONS:
        return _extract_scala_imports(root, file_path, ctx.index)
    if suffix in CSHARP_EXTENSIONS:
        return _extract_csharp_imports(root, file_path, ctx.index)
    if suffix in RUBY_EXTENSIONS:
        return _extract_ruby_imports(file_path)
    if suffix in PHP_EXTENSIONS:
        return _extract_php_imports(file_path)
    if suffix in DART_EXTENSIONS:
        return _extract_dart_imports(file_path)
    if suffix in LUA_EXTENSIONS:
        return _extract_lua_imports(root, file_path, ctx.index)
    if suffix in ZIG_EXTENSIONS:
        return _extract_zig_imports(file_path)
    return []


def build_graph(root: Path) -> ProjectGraph:
    """Build the import graph for every supported source file under ``root`` (no cache).

    Two passes, not one: the qualified-name languages (Java/Kotlin/Scala/C#/
    Lua) resolve an import against the WHOLE project's file listing (see
    `_resolve_qualified_name`), which has to be known before any file's
    imports can be resolved. Pass 1 hashes every file and builds that
    listing; pass 2 extracts imports using it.
    """

    root = root.resolve()
    edges: dict[str, list[str]] = {}
    file_hashes: dict[str, str] = {}
    files = list(_iter_source_files(root))

    for file_path in files:
        file_hash = _hash_file(file_path)
        if file_hash is None:
            # Vanished between enumeration and hashing (race with an agent
            # editing the tree) — just omit it from this graph revision.
            continue
        file_hashes[file_path.relative_to(root).as_posix()] = file_hash

    ctx = _ImportResolveContext(
        index=_build_qualified_index(file_hashes),
        go_module_prefix=_go_module_prefix(root),
    )

    for file_path in files:
        rel = file_path.relative_to(root).as_posix()
        if rel not in file_hashes:
            continue  # vanished mid-scan, see the hashing pass above
        deps = _extract_imports_any(root, file_path, ctx)
        # Self-edges are never meaningful (a file is never a dependency of
        # itself) and are excluded here rather than per-extractor: most
        # extractors resolve one specific file and can't produce one by
        # construction, but Go/C# resolve to every file in a package/
        # namespace DIRECTORY, which — if the importer happens to sit in
        # that same directory — would otherwise include the importer itself.
        edges[rel] = sorted(
            {d.relative_to(root).as_posix() for d in deps} - {file_path.relative_to(root).as_posix()}
        )

    return ProjectGraph(root=root, edges=edges, file_hashes=file_hashes)


def load_or_build_graph(root: Path) -> ProjectGraph:
    """Load the cached graph if file hashes are unchanged; rebuild otherwise."""

    root = root.resolve()
    cache_file = _cache_path(root)
    current_hashes = _current_hashes(root)

    if cache_file.is_file():
        try:
            cached = ProjectGraph.from_dict(json.loads(cache_file.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, OSError):
            cached = None
        if cached is not None and cached.file_hashes == current_hashes:
            return cached

    graph = build_graph(root)
    try:
        _atomic_write_json(cache_file, graph.to_dict())
    except OSError as exc:
        # The in-memory graph is already correct and returned below either
        # way -- persisting it is best-effort. A transient write failure
        # (e.g. a concurrent writer racing for the same path) just means
        # the NEXT call rebuilds instead of hitting a fresh cache; it must
        # never fail the caller who only asked for the graph.
        logger.debug("graph_context: failed to persist cache for %s: %s", root, exc)
    return graph


# ---------------------------------------------------------------------------
# Push-based live graph (proxy usage): a background watchdog observer keeps
# an in-memory ProjectGraph current, so a long-running process (the proxy)
# never re-hashes every source file per request -- it just reads whatever
# the watcher last cached. Falls back to `load_or_build_graph` (the pull/hash
# model above) when `watchdog` isn't installed or a watcher fails to start.
# One-shot callers (the CLI) have no use for a background thread and should
# keep calling `load_or_build_graph`/`assemble_context` directly.
# ---------------------------------------------------------------------------

_live_lock = threading.Lock()
_live_graphs: dict[Path, ProjectGraph] = {}
_live_watchers: dict[Path, GraphContextWatcher] = {}

_WATCH_DEBOUNCE_SECONDS = 2.0
# Cap on how many project roots are watched at once. A single proxy normally
# watches one root (its cwd), but a long-lived process that hops between many
# projects would otherwise accumulate one background Observer thread per root
# forever. When the cap is exceeded the oldest watcher is stopped (FIFO —
# dict preserves insertion order). Overridable via env for unusual setups.
try:
    _MAX_LIVE_ROOTS = max(1, int(os.environ.get("HEADROOM_GRAPH_MAX_WATCHED_ROOTS", "16")))
except ValueError:
    _MAX_LIVE_ROOTS = 16


def _rebuild_live(root: Path) -> None:
    """Rebuild `root`'s graph and refresh both the in-memory and on-disk cache.

    Writing to the same on-disk cache file `load_or_build_graph` reads/writes
    is what makes this shared across processes: any other process (including
    ones without a watcher of their own) picks up the fresh graph through its
    normal hash-check the next time it calls `load_or_build_graph` — the
    watcher just keeps the shared file current without waiting to be asked.
    """
    try:
        graph = build_graph(root)
    except Exception as exc:  # noqa: BLE001 — background rebuild must never crash the thread
        logger.debug("graph_context: live rebuild failed for %s: %s", root, exc)
        return

    with _live_lock:
        _live_graphs[root] = graph

    try:
        _atomic_write_json(_cache_path(root), graph.to_dict())
    except OSError as exc:
        logger.debug("graph_context: failed to persist live graph for %s: %s", root, exc)


class GraphContextWatcher:
    """Background watchdog observer that keeps one root's ``ProjectGraph`` live.

    Mirrors `headroom.graph.watcher.CodeGraphWatcher`'s shape (debounced
    watchdog Observer in a daemon thread) applied to the import graph instead
    of the codebase-memory-mcp backend. Debounced so a burst of saves (a
    branch checkout, a formatter run) triggers one rebuild, not one per file.
    """

    def __init__(self, root: Path, debounce_seconds: float = _WATCH_DEBOUNCE_SECONDS) -> None:
        self.root = root
        self.debounce_seconds = debounce_seconds
        self._observer: Any = None
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        """Do one synchronous initial build, then start watching. False if watchdog is unavailable."""

        _rebuild_live(self.root)

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.debug("graph_context: watchdog not installed, staying on pull-based cache")
            return False

        outer = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                src_path = getattr(event, "src_path", "")
                if not src_path or Path(src_path).suffix.lower() not in SOURCE_EXTENSIONS:
                    return
                if set(Path(src_path).parts) & _IGNORED_DIRS:
                    return
                outer._schedule_rebuild()

        try:
            observer = Observer()
            observer.schedule(_Handler(), str(self.root), recursive=True)
            observer.daemon = True
            observer.start()
        except Exception as exc:  # noqa: BLE001 — watcher setup must never break the proxy
            logger.debug("graph_context: watcher failed to start for %s: %s", self.root, exc)
            return False

        self._observer = observer
        logger.info(
            "graph_context: live-watching %s (debounce=%.1fs)", self.root, self.debounce_seconds
        )
        return True

    def _schedule_rebuild(self) -> None:
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(self.debounce_seconds, self._rebuild)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _rebuild(self) -> None:
        _rebuild_live(self.root)

    def stop(self) -> None:
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None
        if self._observer:
            observer = self._observer
            self._observer = None
            observer.stop()
            observer.join(timeout=3)


def get_live_graph(root: Path) -> ProjectGraph:
    """Push-based graph access: in-memory read once a watcher is live for ``root``.

    Starts a `GraphContextWatcher` for `root` on first call (which does one
    synchronous initial build so this call always returns a graph). Later
    calls for the same root are a plain dict read — no filesystem walk, no
    hashing. Falls back to `load_or_build_graph` (hash-checked pull) when
    `watchdog` is unavailable or the watcher fails to start, so behavior is
    identical either way — only the cost model changes.
    """
    root = root.resolve()

    # Reserve the right to start `root`'s watcher atomically: without this,
    # concurrent callers can all observe "no watcher yet" and each start
    # their own Observer for the same root (verified as a real race by
    # inspection -- multiple threads can interleave between the absence
    # check and registration). The reservation itself is instant (just a
    # dict insert); the slow part (`watcher.start()`, which hashes/builds
    # the tree) runs OUTSIDE the lock so concurrent callers for OTHER roots
    # are never blocked on it.
    evicted: list[tuple[Path, GraphContextWatcher]] = []
    with _live_lock:
        cached = _live_graphs.get(root)
        if cached is not None:
            return cached
        existing_watcher = _live_watchers.get(root)
        reserved_watcher = None
        if existing_watcher is None:
            reserved_watcher = GraphContextWatcher(root)
            _live_watchers[root] = reserved_watcher
            # Evict oldest roots over the cap (FIFO). Collect victims under
            # the lock; stop them OUTSIDE it (stop() joins a thread).
            while len(_live_watchers) > _MAX_LIVE_ROOTS:
                old_root, old_watcher = next(iter(_live_watchers.items()))
                if old_root == root:
                    break  # never evict the one we just reserved
                _live_watchers.pop(old_root, None)
                _live_graphs.pop(old_root, None)
                evicted.append((old_root, old_watcher))

    for old_root, old_watcher in evicted:
        old_watcher.stop()
        logger.debug(
            "graph_context: evicted watcher for %s (over cap %d)", old_root, _MAX_LIVE_ROOTS
        )

    if existing_watcher is not None:
        # Another caller already owns this root's watcher (starting or
        # already live). Don't block on its first build -- fall back to a
        # direct load, which is at worst one extra hash-check.
        return load_or_build_graph(root)

    if reserved_watcher is not None:
        started = reserved_watcher.start()
        if started:
            with _live_lock:
                graph = _live_graphs.get(root)
            if graph is not None:
                return graph
        else:
            with _live_lock:
                # Only remove if it's still OUR reservation -- a concurrent
                # `stop_all_watchers()` may have already cleared it.
                if _live_watchers.get(root) is reserved_watcher:
                    _live_watchers.pop(root, None)
                    # `start()` runs its synchronous initial `_rebuild_live`
                    # BEFORE it ever imports/starts watchdog, so a failure
                    # here (watchdog missing, Observer.start() raising) can
                    # still have populated `_live_graphs[root]`. Clear it too
                    # -- otherwise the fast path at the top of this function
                    # would keep serving that one-shot snapshot on every later
                    # call instead of falling through to the hash-checked pull
                    # model below, which is what actually notices file changes
                    # without a live watcher.
                    _live_graphs.pop(root, None)

    return load_or_build_graph(root)


def stop_all_watchers() -> None:
    """Stop every live watcher. For clean shutdown/tests only."""
    with _live_lock:
        watchers = list(_live_watchers.values())
        _live_watchers.clear()
        _live_graphs.clear()
    for watcher in watchers:
        watcher.stop()


def bfs_related_files(
    graph: ProjectGraph, entrypoint: str, max_depth: int = DEFAULT_MAX_DEPTH
) -> list[str]:
    """BFS from ``entrypoint`` over the import graph, bounded by ``max_depth``.

    Returns unique files in visit order (entrypoint first).
    """

    entry = Path(entrypoint).as_posix()
    if entry not in graph.edges:
        raise KeyError(f"entrypoint not found in graph: {entrypoint!r}")

    visited: list[str] = []
    seen = {entry}
    queue: deque[tuple[str, int]] = deque([(entry, 0)])

    while queue:
        node, depth = queue.popleft()
        visited.append(node)
        if depth >= max_depth:
            continue
        for neighbor in graph.edges.get(node, []):
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append((neighbor, depth + 1))

    return visited


def assemble_context(
    root: Path,
    entrypoint: str,
    query: str = "",
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> GraphContextResult:
    """Build the graph-scoped, full-content context for an entrypoint + query.

    This is the Headroom "graph proxy" entrypoint: given where to start and
    what the caller is trying to find out, it returns the concatenated file
    contents (each prefixed with its path) ready to inject into an LLM
    prompt — no truncation, no summarization.
    """

    root = root.resolve()
    graph = load_or_build_graph(root)
    files = bfs_related_files(graph, entrypoint, max_depth=max_depth)

    sections = []
    for rel in files:
        try:
            content = (root / rel).read_text(encoding="utf-8")
        except OSError:
            # File vanished/unreadable since the graph was built — skip it
            # rather than fail the whole context assembly.
            continue
        sections.append(f"---- Arquivo: {rel} ----\n{content}")

    return GraphContextResult(
        entrypoint=entrypoint,
        query=query,
        max_depth=max_depth,
        files=files,
        context="\n\n".join(sections),
    )


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "SOURCE_EXTENSIONS",
    "PYTHON_EXTENSIONS",
    "JS_TS_EXTENSIONS",
    "RUST_EXTENSIONS",
    "C_CPP_EXTENSIONS",
    "GO_EXTENSIONS",
    "JAVA_EXTENSIONS",
    "CSHARP_EXTENSIONS",
    "RUBY_EXTENSIONS",
    "PHP_EXTENSIONS",
    "KOTLIN_EXTENSIONS",
    "SCALA_EXTENSIONS",
    "DART_EXTENSIONS",
    "LUA_EXTENSIONS",
    "ZIG_EXTENSIONS",
    "TREE_SITTER_EXTENSIONS",
    "ProjectGraph",
    "GraphContextResult",
    "GraphContextWatcher",
    "build_graph",
    "load_or_build_graph",
    "get_live_graph",
    "stop_all_watchers",
    "bfs_related_files",
    "assemble_context",
]
