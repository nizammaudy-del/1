"""Guard test: no PEP 604 ``X | Y`` unions in *runtime* operand positions.

The package supports Python 3.9 (``requires-python = ">=3.9"``), where PEP 604
unions are only legal inside annotations (and only because every module carries
``from __future__ import annotations``, which turns annotations into strings).
Used as a *runtime value* — e.g. ``isinstance(x, int | float)`` or
``cast(A | B, v)`` where the first arg is a real expression, not a string — the
``|`` is evaluated eagerly and raises ``TypeError`` on 3.9.

This regressed once (``isinstance(value, int | float)`` in the savings-history
test, and again via an upstream refactor of ``_identity_mismatch`` in
``headroom/cli/wrap.py``) precisely because nothing exercised the pattern. This
test is that missing coverage: it walks every source file in the package, the
test suite, and helper scripts, and fails on any runtime union operand. Keeping
it green is what lets us honestly claim 3.9 support.

If this test fails, rewrite the flagged expression:

* ``isinstance(x, A | B)``   -> ``isinstance(x, (A, B))``
* ``issubclass(x, A | B)``   -> ``issubclass(x, (A, B))``
* ``cast(A | B, v)``         -> ``cast(Union[A, B], v)`` / ``cast(Optional[A], v)``
                               (or quote it: ``cast("A | B", v)`` — a string is
                               not evaluated at runtime)

Annotations (``x: A | B``, ``def f() -> A | B``) are fine and are NOT flagged;
``__future__`` annotations make them lazy strings on 3.9.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Directories whose ``.py`` files ship in / are exercised by the 3.9 runtime.
SCAN_DIRS = ["headroom", "tests", "scripts"]


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for rel in SCAN_DIRS:
        root = REPO_ROOT / rel
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            # Skip anything under a build/cache dir if it ever appears.
            if "__pycache__" in path.parts:
                continue
            files.append(path)
    return files


def _is_union_binop(node: ast.expr) -> bool:
    """True if ``node`` is a bare ``A | B`` expression (``ast.BinOp`` with
    ``ast.BitOr``). A quoted ``"A | B"`` is an ``ast.Constant`` and is NOT a
    runtime union — those are deliberately allowed.
    """
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr)


def _collect_runtime_unions() -> list[tuple[str, int, str]]:
    """Return ``(relpath, lineno, snippet)`` for every runtime union operand.

    Runtime operand positions checked:

    * ``isinstance(x, <union>)`` / ``issubclass(x, <union>)`` — 2nd positional arg
    * ``cast(<union>, v)`` — 1st positional arg (a *string* first arg is fine)
    """
    hits: list[tuple[str, int, str]] = []
    for path in _iter_python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:  # pragma: no cover - a real syntax error fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr

            if name in ("isinstance", "issubclass") and len(node.args) >= 2:
                target = node.args[1]
                if _is_union_binop(target):
                    hits.append((rel, target.lineno, f"{name}(..., <A | B>)"))
            elif name == "cast" and len(node.args) >= 1:
                target = node.args[0]
                if _is_union_binop(target):
                    hits.append((rel, target.lineno, "cast(<A | B>, ...)"))
    return hits


def test_no_runtime_pep604_union_operands() -> None:
    """No ``X | Y`` union may appear as a runtime operand anywhere in the
    package, tests, or scripts — it crashes with ``TypeError`` on Python 3.9.
    """
    hits = _collect_runtime_unions()
    if hits:
        formatted = "\n".join(f"  {rel}:{lineno}: {snippet}" for rel, lineno, snippet in hits)
        pytest.fail(
            "Found PEP 604 `X | Y` union(s) in runtime operand positions — these "
            "raise TypeError on Python 3.9:\n"
            f"{formatted}\n"
            "Fix: isinstance/issubclass -> use a tuple `(A, B)`; "
            "cast -> use `Union[A, B]`/`Optional[A]` or a quoted string.",
        )


def test_guard_detects_a_planted_violation() -> None:
    """Meta-check: the AST detector actually fires on the bad pattern, so a
    silently-broken detector can't let real regressions through.
    """
    bad = "isinstance(x, int | float)\ncast(str | None, y)\n"
    tree = ast.parse(bad)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else None
            if fname == "isinstance" and _is_union_binop(node.args[1]):
                found.append("isinstance")
            elif fname == "cast" and _is_union_binop(node.args[0]):
                found.append("cast")
    assert found == ["isinstance", "cast"]

    # And the allowed forms must NOT trip it.
    ok = 'isinstance(x, (int, float))\ncast("str | None", y)\n'
    ok_tree = ast.parse(ok)
    for node in ast.walk(ok_tree):
        if isinstance(node, ast.Call) and node.args:
            assert not _is_union_binop(node.args[-1])
