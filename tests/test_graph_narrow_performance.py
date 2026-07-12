"""Performance regression guards for graph-scoped narrowing (issue #1925).

These are NOT micro-benchmarks -- thresholds carry a large (5-10x) margin over
what was actually measured on a dev machine (see benchmarks/bench_graph_narrow.py
for the human-readable timing/token-savings report these numbers come from).
Their only job is to catch a real algorithmic regression (e.g. an accidental
O(n^2) creeping into `build_graph`'s file walk or the `_graph_narrow` line-
filtering hot path) -- not to track tight wall-clock numbers, which would flake
under ordinary CI/dev-machine scheduling noise.

Windows gotcha that shaped these tests: the FIRST read of a batch of just-
written files pays a one-off OS-level cost (observed ~35s for 5000 freshly
created files vs <1s on a second pass -- looks like AV real-time-scan-on-first-
access, not anything in headroom's own code). Every test below does one
untimed warm-up pass (`build_graph`/`get_live_graph`) before starting its
clock, so the assertions measure headroom's algorithm, not first-touch I/O.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from headroom import graph_context as gc
from headroom.transforms.content_router import ContentRouter


@pytest.fixture(autouse=True)
def _stop_watchers_between_tests():
    yield
    gc.stop_all_watchers()


@pytest.fixture
def fake_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path / "_ws"))


def _build_synthetic_repo(root: Path, n_related: int, n_noise: int) -> tuple[list[str], list[str]]:
    """``main.py`` -> ``pkg/mod1_i.py`` -> ``pkg/mod2_j.py`` (depth-2 fan-out),
    reaching exactly ``n_related`` files from ``main.py`` at max_depth=2, plus
    ``n_noise`` files under ``noise/`` that nothing imports.
    """
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")

    depth1_count = max(1, n_related // 2)
    depth2_count = max(0, n_related - depth1_count - 1)

    (root / "main.py").write_text(
        "\n".join(f"from pkg import mod1_{i}" for i in range(depth1_count)) + "\n",
        encoding="utf-8",
    )

    assign: list[list[int]] = [[] for _ in range(depth1_count)]
    for j in range(depth2_count):
        assign[j % depth1_count].append(j)

    related = ["main.py"]
    for i in range(depth1_count):
        body = "\n".join(f"from . import mod2_{j}" for j in assign[i]) or "pass"
        (pkg / f"mod1_{i}.py").write_text(body + "\n", encoding="utf-8")
        related.append(f"pkg/mod1_{i}.py")
    for j in range(depth2_count):
        (pkg / f"mod2_{j}.py").write_text("VALUE = 1\n", encoding="utf-8")
        related.append(f"pkg/mod2_{j}.py")

    noise_dir = root / "noise"
    noise_dir.mkdir(parents=True, exist_ok=True)
    noise = []
    for k in range(n_noise):
        (noise_dir / f"noise_{k}.py").write_text(f"Z_{k} = {k}\n", encoding="utf-8")
        noise.append(f"noise/noise_{k}.py")

    return related, noise


def _wide_dump(related: list[str], noise: list[str]) -> str:
    lines = [f"{f}:1: VALUE = 1" for f in related]
    lines += [f"{f}:1: Z = 1" for f in noise]
    return "\n".join(lines)


def _read_then_grep_messages() -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_read",
                    "name": "Read",
                    "input": {"file_path": "main.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_read", "content": "..."}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_grep", "name": "Grep", "input": {"pattern": "Z"}}
            ],
        },
    ]


# =============================================================================
# CPU cost: build_graph (cold, no cache) must scale sub-quadratically.
# =============================================================================


def test_build_graph_scales_linearly_not_quadratically(tmp_path: Path) -> None:
    """Doubling repo size must not quadruple `build_graph` time.

    Measured baseline: 251 files ~33ms, 5051 files (20x) ~740ms (~22x) --
    linear. An accidental O(n^2) in the file walk / AST-parse loop would show
    up as ~400x for a 20x file-count increase; the assertion below allows up
    to 40x, comfortably clear of real linear cost but well short of quadratic.
    """
    small_root = tmp_path / "small"
    small_root.mkdir()
    _build_synthetic_repo(small_root, n_related=10, n_noise=200)

    big_root = tmp_path / "big"
    big_root.mkdir()
    _build_synthetic_repo(big_root, n_related=10, n_noise=4000)  # 20x file count

    gc.build_graph(small_root)  # untimed warm-up -- see module docstring
    gc.build_graph(big_root)

    t0 = time.perf_counter()
    gc.build_graph(small_root)
    small_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    gc.build_graph(big_root)
    big_elapsed = time.perf_counter() - t0

    assert big_elapsed < max(small_elapsed * 40, 3.0)


def test_build_graph_bounded_for_5000_files(tmp_path: Path) -> None:
    """Regression guard: building the graph for a 5000-file repo must stay
    well under a second on real hardware (measured ~740ms); 10s is a >10x
    margin that only trips on a genuine blow-up (e.g. an accidental
    subprocess-per-file or repeated full-tree rescans)."""
    root = tmp_path / "proj"
    root.mkdir()
    related, noise = _build_synthetic_repo(root, n_related=50, n_noise=5000)

    gc.build_graph(root)  # untimed warm-up

    t0 = time.perf_counter()
    graph = gc.build_graph(root)
    elapsed = time.perf_counter() - t0

    assert len(graph.edges) == len(related) + 1 + len(noise)  # +1 for pkg/__init__.py
    assert elapsed < 10.0


# =============================================================================
# CPU cost: BFS must stay cheap regardless of total repo size -- it only
# touches the reachable neighborhood, never the whole file list.
# =============================================================================


def test_bfs_related_files_stays_fast_regardless_of_repo_size(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _build_synthetic_repo(root, n_related=50, n_noise=5000)

    graph = gc.build_graph(root)  # untimed warm-up + build

    t0 = time.perf_counter()
    related_found = gc.bfs_related_files(graph, "main.py", max_depth=2)
    elapsed = time.perf_counter() - t0

    assert len(related_found) == 50
    assert elapsed < 0.5  # measured ~0.03ms -- BFS only walks 50 reachable nodes


# =============================================================================
# CPU cost: cache-hit path (`load_or_build_graph`) must not be slower than a
# cold build -- it still re-hashes every file by design (see graph_context.py
# module docstring), but it must never pay for AST-parsing on top of that.
# =============================================================================


def test_load_or_build_graph_cache_hit_not_slower_than_cold_build(
    fake_workspace: None, tmp_path: Path
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _build_synthetic_repo(root, n_related=50, n_noise=2000)

    gc.build_graph(root)  # untimed warm-up

    t0 = time.perf_counter()
    gc.load_or_build_graph(root)  # cold: builds + writes cache
    cold_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    gc.load_or_build_graph(root)  # cache-hit: re-hash + compare, no AST parse
    warm_elapsed = time.perf_counter() - t0

    assert warm_elapsed < max(cold_elapsed * 1.5, 2.0)


# =============================================================================
# CPU cost: `get_live_graph`'s first call per root starts a watchdog Observer
# (real filesystem watch registration -- the actually expensive part); every
# later call for the same root must be a plain in-memory dict read.
# =============================================================================


def test_get_live_graph_second_call_is_near_instant(fake_workspace: None, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _build_synthetic_repo(root, n_related=50, n_noise=2000)
    gc.build_graph(root)  # untimed warm-up

    t0 = time.perf_counter()
    gc.get_live_graph(root)  # starts the watcher -- generously bounded, not tight
    first_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    gc.get_live_graph(root)
    second_elapsed = time.perf_counter() - t0

    assert first_elapsed < 15.0
    assert second_elapsed < 0.1  # measured ~0.15ms -- dict read, no filesystem work
    assert second_elapsed < first_elapsed


# =============================================================================
# Token savings: the actual point of the feature -- a mostly-noise wide dump
# must shrink substantially once narrowed to the import-graph neighborhood.
# =============================================================================


def test_graph_narrow_achieves_substantial_token_savings(
    fake_workspace: None, tmp_path: Path
) -> None:
    """50 related files' worth of matches buried in 5000 unrelated noise
    matches -- a realistic "grep the whole repo" worst case. Measured
    reduction was ~99%; asserting >80% leaves headroom for legitimate future
    tuning while still catching the narrowing silently stopping working."""
    root = tmp_path / "proj"
    root.mkdir()
    related, noise = _build_synthetic_repo(root, n_related=50, n_noise=5000)
    gc.build_graph(root)  # untimed warm-up

    content = _wide_dump(related, noise)

    import os

    monkeypatch_cwd = os.getcwd()
    try:
        os.chdir(root)
        router = ContentRouter()
        router._build_tool_name_map(_read_then_grep_messages())
        narrowed = router._graph_narrow("grep", "call_grep", content)
    finally:
        os.chdir(monkeypatch_cwd)

    assert narrowed is not None
    reduction_pct = (1 - len(narrowed) / len(content)) * 100
    assert reduction_pct > 80
    assert "noise_" not in narrowed.split("[")[0]


def test_graph_narrow_latency_bounded_for_large_wide_dump(
    fake_workspace: None, tmp_path: Path
) -> None:
    """Measured ~290ms for 5050 lines / 50 related files once the live graph
    watcher is already warm (isolating the line-filtering cost itself from
    one-time watcher startup, covered separately above). 5s is a >15x margin.
    """
    root = tmp_path / "proj"
    root.mkdir()
    related, noise = _build_synthetic_repo(root, n_related=50, n_noise=5000)
    gc.build_graph(root)  # untimed warm-up

    content = _wide_dump(related, noise)

    import os

    monkeypatch_cwd = os.getcwd()
    try:
        os.chdir(root)
        gc.get_live_graph(root.resolve())  # untimed: pay watcher startup separately

        router = ContentRouter()
        router._build_tool_name_map(_read_then_grep_messages())

        t0 = time.perf_counter()
        narrowed = router._graph_narrow("grep", "call_grep", content)
        elapsed = time.perf_counter() - t0
    finally:
        os.chdir(monkeypatch_cwd)

    assert narrowed is not None
    assert elapsed < 5.0


def test_graph_narrow_scales_reasonably_with_line_count(
    fake_workspace: None, tmp_path: Path
) -> None:
    """10x more lines to filter must not cost more than ~40x (guards against
    the line x related-file filter degrading past linear-in-lines)."""
    root = tmp_path / "proj"
    root.mkdir()
    related, noise = _build_synthetic_repo(root, n_related=50, n_noise=5000)
    gc.build_graph(root)  # untimed warm-up

    small_content = _wide_dump(related, noise[:400])
    big_content = _wide_dump(related, noise)  # 10x the noise lines

    import os

    monkeypatch_cwd = os.getcwd()
    try:
        os.chdir(root)
        gc.get_live_graph(root.resolve())  # untimed: pay watcher startup separately

        router_small = ContentRouter()
        router_small._build_tool_name_map(_read_then_grep_messages())
        t0 = time.perf_counter()
        router_small._graph_narrow("grep", "call_grep", small_content)
        small_elapsed = time.perf_counter() - t0

        router_big = ContentRouter()
        router_big._build_tool_name_map(_read_then_grep_messages())
        t0 = time.perf_counter()
        router_big._graph_narrow("grep", "call_grep", big_content)
        big_elapsed = time.perf_counter() - t0
    finally:
        os.chdir(monkeypatch_cwd)

    assert big_elapsed < max(small_elapsed * 40, 3.0)


# =============================================================================
# Memory: building the graph must not balloon RSS per file. Skips cleanly
# where psutil isn't installed (it's an optional dep, not declared in
# pyproject.toml -- see test_memory_tracker.py for the same pattern).
# =============================================================================


def test_build_graph_memory_stays_bounded_per_file(tmp_path: Path) -> None:
    psutil = pytest.importorskip("psutil")
    import gc as pygc
    import os

    root = tmp_path / "proj"
    root.mkdir()
    _build_synthetic_repo(root, n_related=50, n_noise=5000)

    gc.build_graph(root)  # untimed warm-up

    proc = psutil.Process(os.getpid())
    pygc.collect()
    before = proc.memory_info().rss
    graph = gc.build_graph(root)
    pygc.collect()
    after = proc.memory_info().rss

    kb_per_file = (after - before) / 1024 / len(graph.edges)
    # Measured ~0.28 KB/file; 20 KB/file is a ~70x margin that only trips on a
    # real leak (e.g. accidentally retaining full file contents in the graph).
    assert kb_per_file < 20.0
