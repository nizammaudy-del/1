"""Tests for ContentRouter's graph-scoped narrowing of Glob/Grep/LS results.

Headroom's native, Graphify-style proxy behavior (`headroom.graph_context`):
whole-repo discovery dumps are narrowed to the import-graph neighborhood of
whatever file the agent last Read, instead of being sent to the LLM in full.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from headroom import graph_context as gc
from headroom.cache.compression_store import get_compression_store, reset_compression_store
from headroom.transforms.content_router import ContentRouter


@pytest.fixture(autouse=True)
def reset_ccr_store():
    """Reset the global CCR store so hash collisions can't leak across tests."""
    reset_compression_store()
    yield


@pytest.fixture(autouse=True)
def _stop_watchers_between_tests():
    """`_graph_narrow*` now reads graphs via `get_live_graph`, which starts a
    background watcher per root -- clean it up so threads don't leak/collide
    across tests (mirrors the fixture in test_graph_context.py).
    """
    yield
    gc.stop_all_watchers()


@pytest.fixture
def fake_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path / "_ws"))


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """main.py -> pkg/util.py, plus an unrelated.py outside the import graph."""

    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "main.py").write_text("from pkg import util\n", encoding="utf-8")
    (root / "pkg" / "util.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "unrelated.py").write_text("Z = 1\n", encoding="utf-8")

    monkeypatch.chdir(root)
    return root


def _wide_grep_dump() -> str:
    lines = [f"unrelated.py:{i}: Z = 1" for i in range(20)]
    lines.append("main.py:1: from pkg import util")
    lines.append("pkg/util.py:1: VALUE = 1")
    lines += [f"unrelated.py:{i + 20}: Z = 1" for i in range(22)]
    return "\n".join(lines)


def _wide_grep_dump_with_substantial_remainder() -> str:
    """Wide enough that even after graph-narrowing, > min_chars (500) remains.

    `_wide_grep_dump()` narrows down to ~2 short lines -- too small to reach
    the downstream compressor at all (falls into the router's own "small,
    nothing left to compress" bucket), which would make a "downstream still
    runs" assertion pass for the wrong reason. This fixture gives main.py and
    pkg/util.py many matches each so the narrowed remainder is still large
    enough to require further (relevance-ranked, capped) compression.
    """
    lines = [f"unrelated.py:{i}: Z = 1 some noise padding text here" for i in range(100)]
    lines += [f"main.py:{i}: from pkg import util  # match number {i} padding text" for i in range(30)]
    lines += [f"pkg/util.py:{i}: VALUE = 1  # match number {i} more padding text" for i in range(30)]
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
            "content": [
                {"type": "tool_result", "tool_use_id": "call_read", "content": "from pkg import util"}
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_grep", "name": "Grep", "input": {"pattern": "Z"}}
            ],
        },
    ]


def test_narrows_wide_grep_dump_to_files_reachable_from_last_read(
    fake_workspace: None, project: Path
) -> None:
    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages())
    assert router._last_read_path == "main.py"

    content = _wide_grep_dump()
    narrowed = router._graph_narrow("grep", "call_grep", content)

    assert narrowed is not None
    assert "main.py" in narrowed
    assert "pkg/util.py" in narrowed
    assert "unrelated.py" not in narrowed


def test_noop_for_non_discovery_tool(fake_workspace: None, project: Path) -> None:
    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages())
    assert router._graph_narrow("bash", "call_grep", _wide_grep_dump()) is None


# =============================================================================
# Bash-shelled discovery (issue #1925 follow-up): harnesses without dedicated
# Grep/Glob/LS tools (e.g. opencode) shell the same wide dumps out through a
# generic bash tool instead. `_graph_narrow`/`_graph_narrow_lossless` must
# reach those too, gated on the *parsed shell command* (`find`, `ls -R`,
# `grep -r`, …), not just the harness's own tool name.
# =============================================================================


def _read_then_bash_messages(command: str) -> list[dict]:
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
            "content": [
                {"type": "tool_result", "tool_use_id": "call_read", "content": "from pkg import util"}
            ],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_bash",
                    "name": "bash",
                    "input": {"command": command},
                }
            ],
        },
    ]


@pytest.mark.parametrize("command", ["grep -r Z .", "find . -name '*.py'", "ls -R"])
def test_narrows_wide_bash_search_dump_to_files_reachable_from_last_read(
    fake_workspace: None, project: Path, command: str
) -> None:
    router = ContentRouter()
    router._build_tool_name_map(_read_then_bash_messages(command))
    assert router._last_read_path == "main.py"

    narrowed = router._graph_narrow("bash", "call_bash", _wide_grep_dump())

    assert narrowed is not None
    assert "main.py" in narrowed
    assert "pkg/util.py" in narrowed
    assert "unrelated.py" not in narrowed


def test_noop_for_bash_command_that_is_not_a_discovery_command(
    fake_workspace: None, project: Path
) -> None:
    """Documents an accepted gap, not a bug: `cat`/`sed` reads of a SINGLE
    already-known file aren't a repo-wide discovery dump with a candidate
    list of paths to filter, so graph-narrow has nothing to do there --
    unlike `grep -r`/`find`/`ls -R`, whose output IS a list of paths.
    """
    router = ContentRouter()
    router._build_tool_name_map(_read_then_bash_messages("cat -n main.py"))
    assert router._graph_narrow("bash", "call_bash", _wide_grep_dump()) is None


def test_noop_for_bash_call_with_no_recorded_command(fake_workspace: None, project: Path) -> None:
    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages())  # no bash call recorded at all
    assert router._graph_narrow("bash", "call_grep", _wide_grep_dump()) is None


def test_noop_below_min_lines_threshold(fake_workspace: None, project: Path) -> None:
    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages())
    assert router._graph_narrow("grep", "call_grep", "main.py:1: x") is None


def test_noop_without_any_entrypoint_signal(fake_workspace: None, project: Path) -> None:
    router = ContentRouter()  # no _build_tool_name_map call -> no last_read_path
    assert router._graph_narrow("grep", "call_unknown", _wide_grep_dump()) is None


def test_noop_when_disabled_via_config(fake_workspace: None, project: Path) -> None:
    from headroom.transforms.content_router import ContentRouterConfig

    router = ContentRouter(config=ContentRouterConfig(enable_graph_narrow=False))
    router._build_tool_name_map(_read_then_grep_messages())
    assert router._graph_narrow("grep", "call_grep", _wide_grep_dump()) is None


def test_graph_narrow_composes_with_downstream_pipeline_instead_of_preempting_it(
    fake_workspace: None, project: Path
) -> None:
    """graph_narrow must be a pre-filter, not a short-circuit.

    Regression guard for the original wiring, which did ``result_slots[i] = ...;
    continue`` right after narrowing -- that skipped `relevance_split` and
    `SearchCompressor` entirely for the rest of that message, even though both
    are default-on and already rank/cap search-shaped output. This asserts the
    full `apply()` pipeline: narrowing must still fire (route recorded), AND
    the downstream compressor must still be invoked on the narrowed remainder
    rather than the graph-narrow branch finalizing the message itself.

    Grep/Glob are in `DEFAULT_EXCLUDE_TOOLS`, so `_graph_narrow` can only ever
    be reached once a tool result ages past the adaptive read-protection
    window (`protect_recent_reads_fraction`, 0.3 in the proxy's default token
    mode -- see `proxy/server.py`). This test reproduces that real production
    setting and pads the conversation so the Grep result is old enough to
    clear the window; without both, `_graph_narrow` is provably unreachable
    (verified manually while writing this test) and this assertion would fail
    for an unrelated reason (protection, not narrowing).
    """
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer
    from headroom.transforms.content_router import ContentRouterConfig

    provider = OpenAIProvider()
    tokenizer = Tokenizer(provider.get_token_counter("gpt-4o"), "gpt-4o")
    router = ContentRouter(ContentRouterConfig(min_section_tokens=10, protect_recent_reads_fraction=0.3))

    wide_dump = _wide_grep_dump_with_substantial_remainder()
    messages = [
        *_read_then_grep_messages(),
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_grep", "content": wide_dump}],
        },
    ]
    # Age the Grep result past the adaptive protection window (see docstring).
    for k in range(10):
        messages.append({"role": "assistant", "content": f"ok turn {k}"})
        messages.append({"role": "user", "content": f"next question {k}"})

    calls: list[str] = []
    original = ContentRouter._compress_block_content

    def _spy(self, *, content, **kwargs):  # noqa: ANN001
        calls.append(content)
        return original(self, content=content, **kwargs)

    router._compress_block_content = _spy.__get__(router, ContentRouter)

    result = router.apply(messages, tokenizer)

    assert "router:graph_narrow" in result.transforms_applied
    assert calls, "downstream compressor was never reached -- narrowing pre-empted the pipeline again"
    assert "unrelated.py" not in calls[0]
    assert len(calls[0]) < len(wide_dump)  # graph already shrank it before compression ran

    narrowed_block = result.messages[3]["content"][0]
    final_content = narrowed_block["content"]
    final_text = (
        final_content
        if isinstance(final_content, str)
        else "".join(b.get("text", "") for b in final_content)
    )
    assert "unrelated.py" not in final_text
    assert final_text != wide_dump  # something ran, this isn't a pass-through


# =============================================================================
# _graph_narrow_lossless: CCR-recoverable narrowing for FRESH Grep/Glob output
# (still inside the read-protection window, where `_graph_narrow` is
# unreachable by design -- see the test above's docstring).
# =============================================================================


def test_lossless_narrow_fires_on_fresh_result_and_is_retrievable(
    fake_workspace: None, project: Path
) -> None:
    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages())

    content = _wide_grep_dump()
    folded = router._graph_narrow_lossless("grep", "call_grep", content)

    assert folded is not None
    text, kind = folded
    assert kind == "graph_narrow"
    assert "main.py" in text
    assert "pkg/util.py" in text
    assert "unrelated.py" not in text.split("[")[0]  # not in the kept portion
    assert "Retrieve more: hash=" in text

    # The dropped lines are NOT gone -- they're recoverable via the CCR hash.
    match = re.search(r"hash=([0-9a-f]+)", text)
    assert match is not None
    entry = get_compression_store().retrieve(match.group(1))
    assert entry is not None
    assert entry.original_content == content  # byte-exact original, unrelated.py included


def test_lossless_narrow_noop_below_min_lines(fake_workspace: None, project: Path) -> None:
    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages())
    assert router._graph_narrow_lossless("grep", "call_grep", "main.py:1: x") is None


def test_lossless_narrow_noop_without_entrypoint_signal(fake_workspace: None, project: Path) -> None:
    router = ContentRouter()  # no _build_tool_name_map call -> no last_read_path
    assert router._graph_narrow_lossless("grep", "call_unknown", _wide_grep_dump()) is None


def test_lossless_narrow_fires_via_apply_on_the_very_last_message(
    fake_workspace: None, project: Path
) -> None:
    """The scenario the previous report flagged as uncovered: a Grep result

    that just happened (last message in the conversation, well inside the
    default `protect_recent_reads_fraction=0.3` window) must now be
    graph-narrowed CCR-recoverably instead of being passed through untouched.
    """
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer
    from headroom.transforms.content_router import ContentRouterConfig

    tokenizer = Tokenizer(OpenAIProvider().get_token_counter("gpt-4o"), "gpt-4o")
    router = ContentRouter(config=ContentRouterConfig(protect_recent_reads_fraction=0.3))

    content = _wide_grep_dump()
    messages = [
        *_read_then_grep_messages(),
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_grep", "content": content}],
        },
    ]

    result = router.apply(messages, tokenizer)

    assert "router:excluded:graph_narrow" in result.transforms_applied
    final_text = result.messages[3]["content"][0]["content"]
    assert "unrelated.py" not in final_text.split("[")[0]
    assert "Retrieve more: hash=" in final_text


# =============================================================================
# Telemetry: route_counts token-saved keys, surfaced via the observer (the
# same path the proxy's PrometheusMetrics consumes). Without this, there is
# no way to tell whether either narrowing path earns its keep in production.
# =============================================================================


class _SpyObserver:
    def __init__(self) -> None:
        self.route_counts: dict | None = None

    def record_router_route_counts(self, route_counts: dict) -> None:
        self.route_counts = dict(route_counts)


def test_telemetry_records_tokens_saved_for_lossless_fresh_narrow(
    fake_workspace: None, project: Path
) -> None:
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer
    from headroom.transforms.content_router import ContentRouterConfig

    tokenizer = Tokenizer(OpenAIProvider().get_token_counter("gpt-4o"), "gpt-4o")
    spy = _SpyObserver()
    router = ContentRouter(
        config=ContentRouterConfig(protect_recent_reads_fraction=0.3), observer=spy
    )

    content = _wide_grep_dump()
    messages = [
        *_read_then_grep_messages(),
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_grep", "content": content}],
        },
    ]
    router.apply(messages, tokenizer)

    assert spy.route_counts is not None
    assert spy.route_counts.get("graph_narrow_lossless_tokens_saved", 0) > 0


def test_telemetry_records_tokens_saved_for_lossy_aged_narrow(
    fake_workspace: None, project: Path
) -> None:
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer
    from headroom.transforms.content_router import ContentRouterConfig

    tokenizer = Tokenizer(OpenAIProvider().get_token_counter("gpt-4o"), "gpt-4o")
    spy = _SpyObserver()
    router = ContentRouter(
        config=ContentRouterConfig(protect_recent_reads_fraction=0.3), observer=spy
    )

    content = _wide_grep_dump()
    messages = [
        *_read_then_grep_messages(),
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_grep", "content": content}],
        },
    ]
    for k in range(10):
        messages.append({"role": "assistant", "content": f"ok turn {k}"})
        messages.append({"role": "user", "content": f"next question {k}"})

    router.apply(messages, tokenizer)

    assert spy.route_counts is not None
    assert spy.route_counts.get("graph_narrow_tokens_saved", 0) > 0


# =============================================================================
# Regression: entrypoint-priority false positive (found via the heuristic
# stress battery). A Grep call's own free-text pattern can incidentally
# mention a real filename that has nothing to do with what the agent is
# actually working on -- `_detect_graph_entrypoint` must not let that
# override the file the agent actually just Read.
# =============================================================================


def test_last_read_wins_over_a_filename_mentioned_in_the_grep_pattern(
    fake_workspace: None, project: Path
) -> None:
    """Reproduces the exact failure found manually: Read main.py, then Grep
    for a pattern that happens to name `unrelated.py` in free text. Before
    the fix, the tool call's own args were checked BEFORE `_last_read_path`,
    so the entrypoint was hijacked to `unrelated.py` -- narrowing then kept
    the 42 irrelevant noise lines and DROPPED the 2 lines that were actually
    relevant (main.py / pkg/util.py). That's a correctness regression, not
    just a missed optimization: it inverts what should have been kept.
    """
    router = ContentRouter()
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "main.py"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "r1", "content": "..."}],
        },
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "g1",
                    "name": "Grep",
                    "input": {"pattern": "TODO see unrelated.py for context"},
                }
            ],
        },
    ]
    router._build_tool_name_map(messages)

    entrypoint = router._detect_graph_entrypoint("g1", project)
    assert entrypoint == "main.py"  # last Read wins, not the filename in the pattern

    narrowed = router._graph_narrow("grep", "g1", _wide_grep_dump())
    assert narrowed is not None
    assert "main.py" in narrowed
    assert "pkg/util.py" in narrowed
    assert "unrelated.py" not in narrowed.split("[")[0]


# =============================================================================
# Regression: get_live_graph() concurrency. Multiple threads calling it for
# the same root before any watcher exists must not each start their own
# watcher (found via a forced-race stress test with an injected delay).
# =============================================================================


def test_get_live_graph_does_not_start_duplicate_watchers_under_concurrency(
    fake_workspace: None, project: Path
) -> None:
    import threading
    import time

    start_calls = {"n": 0}
    lock = threading.Lock()
    original_start = gc.GraphContextWatcher.start

    def _slow_start(self):
        with lock:
            start_calls["n"] += 1
        time.sleep(0.2)  # widen the race window past normal thread-launch jitter
        return original_start(self)

    root = project.resolve()
    threads = []
    try:
        gc.GraphContextWatcher.start = _slow_start
        threads = [threading.Thread(target=gc.get_live_graph, args=(root,)) for _ in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        gc.GraphContextWatcher.start = original_start

    assert start_calls["n"] == 1, f"watcher.start() called {start_calls['n']} times, expected 1"
    assert len(gc._live_watchers) == 1


# =============================================================================
# Regression: kept-line matching must respect path boundaries, not do a raw
# substring check. Found via deep heuristic stress-testing: a related
# root-level file (e.g. `util.py`) substring-matched inside an UNRELATED
# file whose name merely ends with the same characters (`network_util.py`),
# wrongly keeping its line in the narrowed output.
# =============================================================================


def test_unrelated_file_with_overlapping_suffix_is_not_wrongly_kept(
    fake_workspace: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`network_util.py` must never be kept just because `util.py` (a
    related file) is a substring of its name. Requires files at the SAME
    directory level (root) with no directory prefix to disambiguate --
    that's exactly the case a naive `f in line` substring check misses.
    """
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "main.py").write_text("import util\n", encoding="utf-8")
    (root / "util.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "network_util.py").write_text("Z = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)

    router = ContentRouter()
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "main.py"}}
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": "..."}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "g1", "name": "Grep", "input": {"pattern": "Z"}}],
        },
    ]
    router._build_tool_name_map(messages)

    lines = [f"noise_{i}.py:{i}: filler line {i}" for i in range(38)]
    lines.append("main.py:1: import util")
    lines.append("util.py:1: VALUE = 1")
    lines.append("network_util.py:1: Z = 1")  # the trap: NOT related, name ends with "util.py"
    dump = "\n".join(lines)

    narrowed = router._graph_narrow("grep", "g1", dump)

    assert narrowed is not None
    assert "main.py" in narrowed
    assert "util.py:1: VALUE" in narrowed
    assert "network_util.py" not in narrowed


def test_known_limitation_bare_filename_without_directory_prefix_may_drop(
    fake_workspace: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documents an accepted trade-off, not a bug to fix: a related NESTED
    file (`pkg/util.py`) shown in the tool output as a bare filename
    (`util.py`, no `pkg/` prefix -- e.g. a search tool run from within that
    file's own directory) will NOT be matched by the path-boundary check.

    Matching bare basenames too would reopen the exact suffix-collision
    false positive the test above guards against (`util.py` would then also
    match `network_util.py`). Precision (never wrongly keep an unrelated
    file) was chosen over recall (never miss a related one shown without
    its directory) -- this test pins that choice down so it doesn't shift
    silently in either direction later.
    """
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(root)
    (root / "main.py").write_text("from pkg import util\n", encoding="utf-8")
    (root / "pkg" / "util.py").write_text("VALUE = 1\n", encoding="utf-8")

    router = ContentRouter()
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "main.py"}}
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": "..."}]},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "g1", "name": "Grep", "input": {"pattern": "Z"}}],
        },
    ]
    router._build_tool_name_map(messages)

    lines = [f"noise_{i}.py:{i}: filler line {i}" for i in range(38)]
    lines.append("main.py:1: from pkg import util")
    lines.append("util.py:1: VALUE = 1")  # bare filename, no "pkg/" prefix
    dump = "\n".join(lines)

    narrowed = router._graph_narrow("grep", "g1", dump)

    # Only "main.py" clears the 2-line floor, so this currently no-ops --
    # pinning today's accepted behavior, not asserting it's ideal.
    assert narrowed is None
