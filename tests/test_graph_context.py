"""Tests for ``headroom.graph_context`` -- graph-scoped context builder."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from headroom import graph_context as gc


@pytest.fixture(autouse=True)
def _stop_watchers_between_tests():
    """Watchers are module-global state (one background thread per root).

    Without this, a watcher started in one test keeps running (and its
    debounce timer keeps firing) after the test's tmp_path is gone.
    """
    yield
    gc.stop_all_watchers()


@pytest.fixture
def fake_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect the Headroom workspace (graph cache root) into tmp_path."""

    workspace = tmp_path / "_workspace"
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    return workspace


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small project: main -> util (absolute import) -> helper (relative import).

    ``unused`` is not reachable from main and must never show up in BFS
    results. ``main`` also imports a third-party package, which must not
    appear as a graph edge at all.
    """

    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")

    (root / "main.py").write_text(
        "import os\nfrom pkg import util\n\ndef run():\n    return util.value()\n",
        encoding="utf-8",
    )
    (root / "pkg" / "util.py").write_text(
        "from . import helper\n\ndef value():\n    return helper.CONST\n",
        encoding="utf-8",
    )
    (root / "pkg" / "helper.py").write_text("CONST = 42\n", encoding="utf-8")
    (root / "unused.py").write_text("X = 1\n", encoding="utf-8")

    return root


def test_build_graph_resolves_internal_imports_only(project: Path) -> None:
    graph = gc.build_graph(project)

    assert graph.edges["main.py"] == ["pkg/util.py"]
    assert graph.edges["pkg/util.py"] == ["pkg/helper.py"]
    assert graph.edges["pkg/helper.py"] == []
    assert "unused.py" in graph.edges  # present as a node, just unreferenced


def test_bfs_respects_max_depth(project: Path) -> None:
    graph = gc.build_graph(project)

    assert gc.bfs_related_files(graph, "main.py", max_depth=0) == ["main.py"]
    assert gc.bfs_related_files(graph, "main.py", max_depth=1) == ["main.py", "pkg/util.py"]
    assert gc.bfs_related_files(graph, "main.py", max_depth=2) == [
        "main.py",
        "pkg/util.py",
        "pkg/helper.py",
    ]
    assert "unused.py" not in gc.bfs_related_files(graph, "main.py", max_depth=5)


def test_bfs_unknown_entrypoint_raises(project: Path) -> None:
    graph = gc.build_graph(project)
    with pytest.raises(KeyError):
        gc.bfs_related_files(graph, "does_not_exist.py")


def test_assemble_context_concatenates_full_file_contents(
    fake_workspace: Path, project: Path
) -> None:
    result = gc.assemble_context(project, "main.py", "how does run() work?", max_depth=2)

    assert result.files == ["main.py", "pkg/util.py", "pkg/helper.py"]
    assert "---- Arquivo: main.py ----" in result.context
    assert "---- Arquivo: pkg/util.py ----" in result.context
    assert "CONST = 42" in result.context  # helper.py content present verbatim
    assert "how does run() work?" in result.prompt
    assert result.context in result.prompt


def test_cache_is_reused_when_files_unchanged(fake_workspace: Path, project: Path) -> None:
    first = gc.load_or_build_graph(project)
    cache_file = gc._cache_path(project.resolve())
    assert cache_file.is_file()

    mtime_before = cache_file.stat().st_mtime_ns
    second = gc.load_or_build_graph(project)

    assert second.edges == first.edges
    assert cache_file.stat().st_mtime_ns == mtime_before  # not rewritten


def test_cache_invalidated_on_file_change(fake_workspace: Path, project: Path) -> None:
    gc.load_or_build_graph(project)

    (project / "pkg" / "helper.py").write_text("CONST = 99\nEXTRA = True\n", encoding="utf-8")

    graph = gc.load_or_build_graph(project)
    result = gc.assemble_context(project, "main.py", max_depth=2)

    assert graph.file_hashes["pkg/helper.py"] != ""
    assert "EXTRA = True" in result.context


def test_cache_invalidated_on_new_file(fake_workspace: Path, project: Path) -> None:
    first = gc.load_or_build_graph(project)

    (project / "pkg" / "new_module.py").write_text(
        "from . import helper\n\nVALUE = helper.CONST\n", encoding="utf-8"
    )

    second = gc.load_or_build_graph(project)
    assert "pkg/new_module.py" in second.edges
    assert "pkg/new_module.py" not in first.edges


# =============================================================================
# Push-based live graph: GraphContextWatcher / get_live_graph
# =============================================================================


def test_get_live_graph_starts_watcher_and_returns_correct_graph(
    fake_workspace: Path, project: Path
) -> None:
    root = project.resolve()
    assert root not in gc._live_watchers

    graph = gc.get_live_graph(root)

    assert root in gc._live_watchers  # watchdog is a hard dependency here, must have started
    assert graph.edges["main.py"] == ["pkg/util.py"]


def test_get_live_graph_second_call_is_in_memory_reuse(fake_workspace: Path, project: Path) -> None:
    root = project.resolve()
    first = gc.get_live_graph(root)
    second = gc.get_live_graph(root)

    assert second is first  # exact same object -- dict read, not a rebuild
    assert len(gc._live_watchers) == 1  # didn't start a second watcher for the same root


def test_watcher_picks_up_new_file_via_real_filesystem_event(
    fake_workspace: Path, project: Path
) -> None:
    """End-to-end: an actual watchdog Observer notices a real file write.

    Uses a short debounce directly (not `get_live_graph`'s 2s default) so the
    test doesn't need to sleep that long.
    """
    root = project.resolve()
    watcher = gc.GraphContextWatcher(root, debounce_seconds=0.3)
    try:
        assert watcher.start() is True  # watchdog is installed in this env
        assert "pkg/new_mod.py" not in gc._live_graphs[root].edges

        (project / "pkg" / "new_mod.py").write_text(
            "from . import helper\n\nVALUE = helper.CONST\n", encoding="utf-8"
        )

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if "pkg/new_mod.py" in gc._live_graphs[root].edges:
                break
            time.sleep(0.1)

        assert "pkg/new_mod.py" in gc._live_graphs[root].edges
    finally:
        watcher.stop()


def test_get_live_graph_falls_back_to_pull_model_when_watcher_cannot_start(
    fake_workspace: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `GraphContextWatcher.start()` fails (e.g. no watchdog), behavior is
    unchanged -- just costed differently (falls back to `load_or_build_graph`).
    """
    monkeypatch.setattr(gc.GraphContextWatcher, "start", lambda self: False)
    root = project.resolve()

    graph = gc.get_live_graph(root)

    assert root not in gc._live_watchers
    assert graph.edges["main.py"] == ["pkg/util.py"]


def test_get_live_graph_does_not_serve_stale_graph_after_watcher_start_failure(
    fake_workspace: Path, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the real `GraphContextWatcher.start()` runs its synchronous
    initial `_rebuild_live()` (which populates `_live_graphs[root]`) BEFORE it
    ever imports/starts watchdog -- so a failure there (watchdog missing,
    `Observer.start()` raising) can still leave `_live_graphs[root]` behind
    even though `start()` returns False. Without clearing it, every later
    `get_live_graph()` call would hit the in-memory fast path at the top of
    the function and serve that one-shot snapshot forever, instead of falling
    through to the hash-checked `load_or_build_graph` pull model -- so a file
    change made after the failed start would never be observed.

    The existing `..._falls_back_to_pull_model...` test above stubs `start`
    with a bare `lambda self: False` that never calls `_rebuild_live`, so it
    doesn't reproduce this: `_live_graphs[root]` stays empty either way. This
    test's stub matches the real method's actual sequence.
    """
    root = project.resolve()

    def _rebuild_then_fail(self: gc.GraphContextWatcher) -> bool:
        gc._rebuild_live(self.root)  # what the real start() does before the watchdog import
        return False  # simulate watchdog unavailable / Observer.start() failure

    monkeypatch.setattr(gc.GraphContextWatcher, "start", _rebuild_then_fail)

    first = gc.get_live_graph(root)
    assert first.edges["main.py"] == ["pkg/util.py"]
    assert root not in gc._live_watchers  # failed reservation was cleaned up
    assert root not in gc._live_graphs  # and so was the leftover in-memory graph

    (project / "pkg" / "new_module.py").write_text(
        "from . import helper\n\nVALUE = helper.CONST\n", encoding="utf-8"
    )

    second = gc.get_live_graph(root)

    assert "pkg/new_module.py" in second.edges, (
        "get_live_graph served a stale in-memory graph instead of falling "
        "through to load_or_build_graph after the watcher failed to start"
    )


def test_stop_all_watchers_clears_state(fake_workspace: Path, project: Path) -> None:
    root = project.resolve()
    gc.get_live_graph(root)
    assert gc._live_watchers  # something is running

    gc.stop_all_watchers()

    assert gc._live_watchers == {}
    assert gc._live_graphs == {}


# =============================================================================
# Multi-language regex fallback (JS/TS, Rust, C/C++). Python keeps real AST
# parsing; these three get a deliberately simple regex scan for their own
# import/include syntax -- see the module docstring for what it does and
# doesn't resolve.
# =============================================================================


@pytest.fixture
def multilang_project(tmp_path: Path) -> Path:
    """A tiny project in 4 languages, each with a main -> util edge and an
    unrelated sibling file that must never show up in the neighborhood."""

    root = tmp_path / "multilang"
    root.mkdir(parents=True)

    (root / "main.ts").write_text(
        'import { value } from "./util";\nconsole.log(value());\n', encoding="utf-8"
    )
    (root / "util.ts").write_text("export function value() { return 1; }\n", encoding="utf-8")
    (root / "unrelated.ts").write_text("export const Z = 1;\n", encoding="utf-8")

    (root / "main.rs").write_text("mod util;\nfn main() { util::value(); }\n", encoding="utf-8")
    (root / "util.rs").write_text("pub fn value() -> i32 { 1 }\n", encoding="utf-8")

    (root / "main.c").write_text(
        '#include "util.h"\nint main() { return value(); }\n', encoding="utf-8"
    )
    (root / "util.h").write_text("int value();\n", encoding="utf-8")

    return root


def test_js_ts_relative_import_becomes_an_edge(multilang_project: Path) -> None:
    graph = gc.build_graph(multilang_project)

    assert graph.edges["main.ts"] == ["util.ts"]
    assert "unrelated.ts" not in graph.edges["main.ts"]
    assert gc.bfs_related_files(graph, "main.ts", max_depth=2) == ["main.ts", "util.ts"]


def test_rust_mod_declaration_becomes_an_edge(multilang_project: Path) -> None:
    graph = gc.build_graph(multilang_project)

    assert graph.edges["main.rs"] == ["util.rs"]
    assert gc.bfs_related_files(graph, "main.rs", max_depth=2) == ["main.rs", "util.rs"]


def test_c_local_include_becomes_an_edge(multilang_project: Path) -> None:
    graph = gc.build_graph(multilang_project)

    assert graph.edges["main.c"] == ["util.h"]
    assert gc.bfs_related_files(graph, "main.c", max_depth=2) == ["main.c", "util.h"]


def test_js_bare_package_specifier_is_not_resolved(multilang_project: Path) -> None:
    """`import x from "react"` is a package, not a project file -- must not
    become an edge (there's nothing on disk to resolve it to anyway)."""

    (multilang_project / "with_package_import.ts").write_text(
        'import React from "react";\nimport { value } from "./util";\n', encoding="utf-8"
    )
    graph = gc.build_graph(multilang_project)

    assert graph.edges["with_package_import.ts"] == ["util.ts"]


def test_c_system_include_is_not_resolved(multilang_project: Path) -> None:
    """`#include <stdio.h>` is a system header -- must not become an edge."""

    (multilang_project / "with_system_include.c").write_text(
        '#include <stdio.h>\n#include "util.h"\n', encoding="utf-8"
    )
    graph = gc.build_graph(multilang_project)

    assert graph.edges["with_system_include.c"] == ["util.h"]


def test_project_with_no_python_files_still_builds_a_real_graph(
    fake_workspace: Path, multilang_project: Path
) -> None:
    """Regression guard: before multi-language support, a project with zero
    `.py` files produced a completely empty graph (0 nodes, 0 edges) --
    every entrypoint lookup failed and the narrowing feature was silently
    inert. This is the exact scenario reported as a limitation."""

    graph = gc.load_or_build_graph(multilang_project)

    assert len(graph.edges) > 0
    assert "main.ts" in graph.edges
    assert "main.rs" in graph.edges
    assert "main.c" in graph.edges


def test_dynamic_js_import_is_resolved(multilang_project: Path) -> None:
    """`import('./x')` dynamic imports ARE captured (the regex was extended
    in the pre-PR audit). Guards against a regression back to dropping them."""

    (multilang_project / "dyn.ts").write_text("const mod = import('./util');\n", encoding="utf-8")
    graph = gc.build_graph(multilang_project)

    assert graph.edges["dyn.ts"] == ["util.ts"]


def test_js_import_inside_a_comment_is_not_an_edge(multilang_project: Path) -> None:
    """Line and block comments are stripped before scanning, so an import
    mentioned only inside a comment must not create a spurious edge --
    while a real import with a trailing comment still resolves."""

    (multilang_project / "commented.ts").write_text(
        '// import { value } from "./util";\n'
        '/* import { value } from "./util"; */\n'
        "export const C = 1;\n",
        encoding="utf-8",
    )
    (multilang_project / "trailing.ts").write_text(
        'import { value } from "./util"; // keep this one\n', encoding="utf-8"
    )
    graph = gc.build_graph(multilang_project)

    assert graph.edges["commented.ts"] == []  # comment-only mention -> no edge
    assert graph.edges["trailing.ts"] == ["util.ts"]  # real import survives


def test_c_include_in_disabled_if0_block_is_not_an_edge(tmp_path: Path) -> None:
    """A `#include` inside a literal `#if 0 ... #endif` block is dropped, but
    an active include on the same file still resolves."""

    root = tmp_path / "cproj"
    root.mkdir(parents=True)
    (root / "main.c").write_text(
        '#if 0\n#include "disabled.h"\n#endif\n#include "active.h"\nint main(){return 0;}\n',
        encoding="utf-8",
    )
    (root / "disabled.h").write_text("int d();\n", encoding="utf-8")
    (root / "active.h").write_text("int a();\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["main.c"] == ["active.h"]  # disabled.h excluded


def test_ignored_and_vendor_dirs_are_pruned_from_the_walk(
    fake_workspace: Path, tmp_path: Path
) -> None:
    """A source file inside `node_modules` / `.git` / `target` must never be
    a node in the graph. This also exercises the os.walk-based pruning that
    replaced rglob (which avoids descending into huge vendor trees at all)."""

    root = tmp_path / "proj"
    (root / "node_modules" / "leftpad").mkdir(parents=True)
    (root / "target" / "debug").mkdir(parents=True)
    (root / "src").mkdir(parents=True)

    (root / "src" / "main.py").write_text("import os\n", encoding="utf-8")
    (root / "node_modules" / "leftpad" / "index.js").write_text(
        "module.exports=1\n", encoding="utf-8"
    )
    (root / "target" / "debug" / "build.rs").write_text("fn main() {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert "src/main.py" in graph.edges
    assert not any("node_modules" in f for f in graph.edges)
    assert not any("target" in f for f in graph.edges)


def test_build_graph_does_not_follow_directory_symlink_loops(
    fake_workspace: Path, tmp_path: Path
) -> None:
    """A directory symlink pointing back at its own ancestor must not make
    the tree walk loop forever. os.walk defaults to followlinks=False on
    every supported Python (the rglob path this replaced followed symlinks
    before 3.13). Skipped where the OS won't let us create the symlink
    (Windows without the privilege), since there's nothing to assert then."""

    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "main.py").write_text("import os\n", encoding="utf-8")
    try:
        (root / "loop").symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create directory symlink on this platform/privilege")

    import time as _time

    start = _time.monotonic()
    graph = gc.build_graph(root)  # must return promptly, not hang
    assert _time.monotonic() - start < 5.0
    assert "main.py" in graph.edges


# =============================================================================
# Atomic cache writes under real concurrency (found a genuine Windows-only
# transient PermissionError here: os.replace/MoveFileEx can fail when
# another thread has the target file open at the exact rename instant, even
# though POSIX rename never has that window. Retried with backoff.)
# =============================================================================


def test_atomic_write_survives_many_concurrent_writers(
    fake_workspace: Path, tmp_path: Path
) -> None:
    import json
    import threading

    target = tmp_path / "shared_cache.json"
    errors: list[Exception] = []

    def writer(n: int) -> None:
        try:
            gc._atomic_write_json(target, {"writer": n, "edges": {"a.py": ["b.py"]}})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # Whoever wrote last, the file must be a single complete, valid JSON
    # object -- never a torn/partial write from an interrupted concurrent one.
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "writer" in data
    assert data["edges"] == {"a.py": ["b.py"]}


# =============================================================================
# Robustness fixes from the pre-PR audit: race-delete tolerance and the
# watched-roots cap.
# =============================================================================


def test_build_graph_tolerates_file_deleted_mid_hash(
    fake_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file vanishing between enumeration and hashing (agent editing the
    tree while the proxy rebuilds) must degrade to "not in this revision",
    not crash the build."""

    root = tmp_path / "proj"
    root.mkdir(parents=True)
    for i in range(5):
        (root / f"f{i}.py").write_text("import os\n", encoding="utf-8")

    orig = gc._hash_file
    state = {"done": False}

    def racing_hash(path: Path):
        if not state["done"]:
            state["done"] = True
            for i in range(5):
                other = root / f"f{i}.py"
                if other != path and other.exists():
                    other.unlink()
                    break
        return orig(path)

    monkeypatch.setattr(gc, "_hash_file", racing_hash)

    graph = gc.build_graph(root)  # must not raise
    assert len(graph.edges) == 4  # the deleted one is simply absent


def test_watched_roots_are_capped_with_fifo_eviction(
    fake_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More than `_MAX_LIVE_ROOTS` distinct roots must not leak unbounded
    background watcher threads -- the oldest is stopped when the cap is hit."""

    monkeypatch.setattr(gc, "_MAX_LIVE_ROOTS", 3)

    roots = []
    for i in range(6):
        r = tmp_path / f"proj{i}"
        r.mkdir(parents=True)
        (r / "main.py").write_text("import os\n", encoding="utf-8")
        roots.append(r.resolve())
        gc.get_live_graph(r)

    assert len(gc._live_watchers) <= 3
    # Oldest three evicted, newest three kept.
    assert all(roots[i] not in gc._live_watchers for i in range(3))
    assert all(roots[i] in gc._live_watchers for i in range(3, 6))
