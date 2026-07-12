#!/usr/bin/env python3
"""Standalone benchmark for graph-scoped narrowing (issue #1925).

Answers the two questions that matter for this feature:

1. Token savings -- does narrowing a whole-repo Grep/Glob dump down to the
   import-graph neighborhood actually shrink what reaches the LLM, and by how
   much, across realistic repo shapes?
2. CPU cost -- what does the feature cost to run: `build_graph` (cold file
   walk + AST parse), the cache-hit re-hash path, `get_live_graph`'s one-time
   watcher startup, and the `_graph_narrow`/`_graph_narrow_lossless` line-
   filtering hot path -- and how does each scale with repo size?

Run with:
    python benchmarks/bench_graph_narrow.py

Note: on Windows, the FIRST read of a batch of just-written files pays a
one-off OS-level cost (looks like AV real-time-scan-on-first-access -- ~35s
was observed for 5000 freshly created files vs <1s on a second pass). Every
scenario below does one untimed warm-up build before its timed runs so the
numbers reflect headroom's own algorithm, not first-touch file I/O. See
tests/test_graph_narrow_performance.py for the pytest regression guards
derived from this script's numbers.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from headroom import graph_context as gc
from headroom.transforms.content_router import ContentRouter

REPO_SIZES = [250, 1_650, 5_050]  # total files (50 related + N noise)


def build_synthetic_repo(root: Path, n_related: int, n_noise: int) -> tuple[list[str], list[str]]:
    """``main.py`` -> ``pkg/mod1_i.py`` -> ``pkg/mod2_j.py`` (depth-2 fan-out),
    reaching exactly ``n_related`` files from ``main.py`` at max_depth=2, plus
    ``n_noise`` files under ``noise/`` that nothing imports -- a stand-in for
    "grep the whole repo" where only a small slice is actually relevant.
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


def wide_dump(related: list[str], noise: list[str]) -> str:
    """Simulate a ripgrep-style dump: one match line per related file, one per
    noise file -- realistic shape for `grep -rn <term>` across a whole repo.
    """
    lines = [f"{f}:1: VALUE = 1" for f in related]
    lines += [f"{f}:1: Z = 1" for f in noise]
    return "\n".join(lines)


def read_then_grep_messages() -> list[dict]:
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


@dataclass
class ScalingResult:
    total_files: int
    build_graph_ms: float
    bfs_ms: float
    cache_cold_ms: float
    cache_warm_ms: float


def bench_scaling(root: Path, n_related: int, n_noise: int, workspace: Path) -> ScalingResult:
    os.environ["HEADROOM_WORKSPACE_DIR"] = str(workspace)
    build_synthetic_repo(root, n_related, n_noise)

    gc.build_graph(root)  # untimed warm-up -- absorbs first-touch cost

    t0 = time.perf_counter()
    graph = gc.build_graph(root)
    build_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    gc.bfs_related_files(graph, "main.py", max_depth=2)
    bfs_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    gc.load_or_build_graph(root)
    cold_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    gc.load_or_build_graph(root)
    warm_ms = (time.perf_counter() - t0) * 1000

    return ScalingResult(len(graph.edges), build_ms, bfs_ms, cold_ms, warm_ms)


def print_scaling_report(results: list[tuple[int, ScalingResult]]) -> None:
    print(f"\n{'=' * 88}")
    print("SCALING: build_graph / bfs_related_files / load_or_build_graph")
    print(f"{'=' * 88}")
    header = f"{'total_files':>12} | {'build_graph':>12} | {'bfs':>10} | {'cache_cold':>11} | {'cache_warm':>11}"
    print(header)
    print("-" * len(header))
    for _, r in results:
        print(
            f"{r.total_files:>12} | {r.build_graph_ms:>10.2f}ms | {r.bfs_ms:>8.4f}ms | "
            f"{r.cache_cold_ms:>9.2f}ms | {r.cache_warm_ms:>9.2f}ms"
        )

    if len(results) >= 2:
        first, last = results[0][1], results[-1][1]
        file_ratio = last.total_files / first.total_files
        time_ratio = last.build_graph_ms / first.build_graph_ms
        print(
            f"\n{file_ratio:.1f}x more files -> {time_ratio:.1f}x build_graph time "
            f"({'roughly linear' if time_ratio < file_ratio * 2 else 'WORSE THAN LINEAR'})"
        )


@dataclass
class NarrowResult:
    label: str
    chars_before: int
    chars_after: int
    reduction_pct: float
    latency_ms: float


def bench_narrow_savings(root: Path, related: list[str], noise: list[str]) -> list[NarrowResult]:
    content = wide_dump(related, noise)
    cwd = os.getcwd()
    results: list[NarrowResult] = []
    try:
        os.chdir(root)
        gc.get_live_graph(root.resolve())  # untimed: pay watcher startup separately

        router = ContentRouter()
        router._build_tool_name_map(read_then_grep_messages())
        t0 = time.perf_counter()
        narrowed = router._graph_narrow("grep", "call_grep", content)
        elapsed = (time.perf_counter() - t0) * 1000
        assert narrowed is not None
        results.append(
            NarrowResult(
                "_graph_narrow (lossy, aged)",
                len(content),
                len(narrowed),
                (1 - len(narrowed) / len(content)) * 100,
                elapsed,
            )
        )

        router2 = ContentRouter()
        router2._build_tool_name_map(read_then_grep_messages())
        t0 = time.perf_counter()
        folded = router2._graph_narrow_lossless("grep", "call_grep2", content)
        elapsed = (time.perf_counter() - t0) * 1000
        assert folded is not None
        folded_text, _kind = folded
        results.append(
            NarrowResult(
                "_graph_narrow_lossless (fresh, CCR-recoverable)",
                len(content),
                len(folded_text),
                (1 - len(folded_text) / len(content)) * 100,
                elapsed,
            )
        )
    finally:
        os.chdir(cwd)
        gc.stop_all_watchers()

    return results


def print_narrow_report(results: list[NarrowResult]) -> None:
    print(f"\n{'=' * 88}")
    print("TOKEN SAVINGS: narrowing a wide Grep dump to the import-graph neighborhood")
    print(f"{'=' * 88}")
    for r in results:
        print(f"\n{r.label}")
        print(
            f"   chars: {r.chars_before:,} -> {r.chars_after:,}  ({r.reduction_pct:.1f}% reduction)"
        )
        print(f"   latency: {r.latency_ms:.2f}ms")


def bench_watcher_startup(root: Path) -> tuple[float, float]:
    cwd = os.getcwd()
    try:
        os.chdir(root)
        gc.build_graph(root)  # untimed warm-up

        t0 = time.perf_counter()
        gc.get_live_graph(root.resolve())
        first_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        gc.get_live_graph(root.resolve())
        second_ms = (time.perf_counter() - t0) * 1000
    finally:
        os.chdir(cwd)
        gc.stop_all_watchers()
    return first_ms, second_ms


def bench_memory(root: Path) -> float | None:
    try:
        import psutil
    except ImportError:
        return None
    import gc as pygc

    gc.build_graph(root)  # untimed warm-up
    proc = psutil.Process(os.getpid())
    pygc.collect()
    before = proc.memory_info().rss
    graph = gc.build_graph(root)
    pygc.collect()
    after = proc.memory_info().rss
    return float(after - before) / 1024 / len(graph.edges)


def main() -> None:
    print("Graph-Scoped Narrowing Benchmark (issue #1925)")
    print("=" * 88)

    tmp = Path(tempfile.mkdtemp(prefix="headroom_bench_graph_narrow_"))
    workspace = tmp / "_ws"
    try:
        # --- Scaling: build_graph / bfs / cache across repo sizes ---
        scaling_results = []
        for size in REPO_SIZES:
            root = tmp / f"repo_{size}"
            root.mkdir()
            n_noise = size - 51  # 50 related + 1 pkg/__init__.py
            result = bench_scaling(root, n_related=50, n_noise=n_noise, workspace=workspace)
            scaling_results.append((size, result))
            gc.stop_all_watchers()

        print_scaling_report(scaling_results)

        # --- Token savings + narrowing latency on a fresh large repo ---
        related, noise = build_synthetic_repo(tmp / "repo_narrow", n_related=50, n_noise=5_000)
        narrow_results = bench_narrow_savings(tmp / "repo_narrow", related, noise)
        print_narrow_report(narrow_results)

        # --- Watcher startup cost (isolated) ---
        watcher_root = tmp / "repo_watcher"
        watcher_root.mkdir()
        build_synthetic_repo(watcher_root, n_related=50, n_noise=2_000)
        first_ms, second_ms = bench_watcher_startup(watcher_root)
        print(f"\n{'=' * 88}")
        print("get_live_graph: first call (starts watchdog Observer) vs second (dict read)")
        print(f"{'=' * 88}")
        print(f"   first call:  {first_ms:.2f}ms")
        print(f"   second call: {second_ms:.4f}ms")

        # --- Memory ---
        mem_root = tmp / "repo_mem"
        mem_root.mkdir()
        build_synthetic_repo(mem_root, n_related=50, n_noise=5_000)
        kb_per_file = bench_memory(mem_root)
        print(f"\n{'=' * 88}")
        print("MEMORY: RSS delta building the graph")
        print(f"{'=' * 88}")
        if kb_per_file is None:
            print("   psutil not installed -- skipped")
        else:
            print(f"   {kb_per_file:.3f} KB/file")

    finally:
        gc.stop_all_watchers()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
