"""Token-optimization regression: graph-scoped narrowing must actually
shrink a wide discovery dump for every one of the ten tree-sitter-backed
languages added to `headroom.graph_context` (Go, Java, C#, Ruby, PHP,
Kotlin, Scala, Dart, Lua, Zig) -- not just resolve edges correctly at the
`build_graph` level (that's `test_graph_context_languages.py`).

This exercises the actual path an agent's tokens travel through:
`ContentRouter._graph_narrow`, fed by both a harness's own discovery tool
name (Grep/Glob/LS-equivalent) AND a bash-shelled discovery command
(`grep -r`/`find`/`ls -R`, the #1925 follow-up), for each language, and
measures the real token delta -- proving the savings claim holds, not just
that no exception is raised.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from headroom import graph_context as gc
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

_UNRELATED_LINE_COUNT = 40


@pytest.fixture(autouse=True)
def _stop_watchers_between_tests():
    yield
    gc.stop_all_watchers()


@dataclass
class _LangCase:
    id: str
    setup: Callable[[Path], tuple[str, str]]  # root -> (entry_rel, related_rel)


def _go_setup(root: Path) -> tuple[str, str]:
    (root / "pkg" / "util").mkdir(parents=True)
    (root / "go.mod").write_text("module myproj\n\ngo 1.21\n", encoding="utf-8")
    (root / "main.go").write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"myproj/pkg/util"\n)\n', encoding="utf-8"
    )
    (root / "pkg" / "util" / "util.go").write_text("package util\nvar X = 1\n", encoding="utf-8")
    return "main.go", "pkg/util/util.go"


def _java_setup(root: Path) -> tuple[str, str]:
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.java").write_text("import com.acme.pkg.Util;\nclass Main {}\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.java").write_text(
        "package com.acme.pkg;\nclass Util {}\n", encoding="utf-8"
    )
    return "Main.java", "com/acme/pkg/Util.java"


def _csharp_setup(root: Path) -> tuple[str, str]:
    (root / "Services").mkdir(parents=True)
    (root / "Program.cs").write_text("using Services;\nclass Program {}\n", encoding="utf-8")
    (root / "Services" / "Thing.cs").write_text(
        "namespace Services { class Thing {} }\n", encoding="utf-8"
    )
    return "Program.cs", "Services/Thing.cs"


def _ruby_setup(root: Path) -> tuple[str, str]:
    (root / "lib").mkdir(parents=True)
    (root / "main.rb").write_text('require_relative "lib/helper"\n', encoding="utf-8")
    (root / "lib" / "helper.rb").write_text("def helper; end\n", encoding="utf-8")
    return "main.rb", "lib/helper.rb"


def _php_setup(root: Path) -> tuple[str, str]:
    (root / "lib").mkdir(parents=True)
    (root / "main.php").write_text('<?php\nrequire "lib/helper.php";\n', encoding="utf-8")
    (root / "lib" / "helper.php").write_text("<?php\nfunction helper() {}\n", encoding="utf-8")
    return "main.php", "lib/helper.php"


def _kotlin_setup(root: Path) -> tuple[str, str]:
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.kt").write_text("import com.acme.pkg.Util\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.kt").write_text(
        "package com.acme.pkg\nclass Util\n", encoding="utf-8"
    )
    return "Main.kt", "com/acme/pkg/Util.kt"


def _scala_setup(root: Path) -> tuple[str, str]:
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.scala").write_text("import com.acme.pkg.Util\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.scala").write_text(
        "package com.acme.pkg\nclass Util\n", encoding="utf-8"
    )
    return "Main.scala", "com/acme/pkg/Util.scala"


def _dart_setup(root: Path) -> tuple[str, str]:
    (root / "lib").mkdir(parents=True)
    (root / "main.dart").write_text('import "lib/helper.dart";\n', encoding="utf-8")
    (root / "lib" / "helper.dart").write_text("void helper() {}\n", encoding="utf-8")
    return "main.dart", "lib/helper.dart"


def _lua_setup(root: Path) -> tuple[str, str]:
    (root / "foo").mkdir(parents=True)
    (root / "main.lua").write_text('local bar = require("foo.bar")\n', encoding="utf-8")
    (root / "foo" / "bar.lua").write_text("return {}\n", encoding="utf-8")
    return "main.lua", "foo/bar.lua"


def _zig_setup(root: Path) -> tuple[str, str]:
    (root / "main.zig").write_text('const foo = @import("./foo.zig");\n', encoding="utf-8")
    (root / "foo.zig").write_text("pub const X = 1;\n", encoding="utf-8")
    return "main.zig", "foo.zig"


_CASES = [
    _LangCase("go", _go_setup),
    _LangCase("java", _java_setup),
    _LangCase("csharp", _csharp_setup),
    _LangCase("ruby", _ruby_setup),
    _LangCase("php", _php_setup),
    _LangCase("kotlin", _kotlin_setup),
    _LangCase("scala", _scala_setup),
    _LangCase("dart", _dart_setup),
    _LangCase("lua", _lua_setup),
    _LangCase("zig", _zig_setup),
]


def _read_then_grep_messages(entry_rel: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_read", "name": "Read", "input": {"file_path": entry_rel}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_read", "content": "..."}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_grep", "name": "Grep", "input": {"pattern": "TODO"}}
            ],
        },
    ]


def _read_then_bash_messages(entry_rel: str, command: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_read", "name": "Read", "input": {"file_path": entry_rel}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_read", "content": "..."}],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call_bash", "name": "bash", "input": {"command": command}}
            ],
        },
    ]


def _wide_dump(entry_rel: str, related_rel: str) -> str:
    """`_graph_narrow` requires at least 2 kept (related) lines to bother
    narrowing at all (below that the "gain" isn't worth it) -- BFS from the
    entrypoint always includes the entrypoint itself as the first visited
    node, so one line has to mention `entry_rel` and one `related_rel` to
    clear that floor, matching `_wide_grep_dump()` in test_graph_narrow.py.
    """
    lines = [f"noise_{i}.txt:{i}: unrelated filler line number {i}" for i in range(_UNRELATED_LINE_COUNT)]
    lines.append(f"{entry_rel}:1: the entrypoint's own match")
    lines.append(f"{related_rel}:1: the actually relevant match")
    return "\n".join(lines)


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
def test_graph_narrow_reduces_tokens_via_native_discovery_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _LangCase
) -> None:
    """`ContentRouter._graph_narrow`, called the way the harness's own
    Grep/Glob/LS-equivalent tool would trigger it, must drop the unrelated
    noise and keep only the line(s) reachable from the entrypoint via the
    new language's import graph -- and that drop must be a REAL token
    reduction, not a no-op.
    """
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = case.setup(root)

    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages(entry_rel))
    assert router._last_read_path == entry_rel

    dump = _wide_dump(entry_rel, related_rel)
    narrowed = router._graph_narrow("grep", "call_grep", dump)

    assert narrowed is not None, f"{case.id}: narrowing did not fire (no edge resolved?)"
    assert related_rel in narrowed
    assert "noise_0.txt" not in narrowed

    original_tokens = len(dump) // 4
    narrowed_tokens = len(narrowed) // 4
    assert narrowed_tokens < original_tokens, f"{case.id}: narrowed output is not smaller"
    savings = 1 - (narrowed_tokens / original_tokens)
    assert savings > 0.5, f"{case.id}: only {savings:.0%} token savings, expected > 50%"


@pytest.mark.parametrize(
    "case,bash_command",
    [
        (_CASES[1], "grep -r Util ."),  # java
        (_CASES[3], "find . -name '*.rb'"),  # ruby
        (_CASES[8], "ls -R"),  # lua
    ],
    ids=["java+grep-r", "ruby+find", "lua+ls-R"],
)
def test_graph_narrow_reduces_tokens_via_bash_shelled_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _LangCase, bash_command: str
) -> None:
    """Same as above, but through the #1925-follow-up bash path: a harness
    without a dedicated discovery tool shells `grep -r`/`find`/`ls -R`
    instead. Proves the two features (bash-command detection + the new
    language's import resolution) compose correctly, not just each in
    isolation.
    """
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = case.setup(root)

    router = ContentRouter()
    router._build_tool_name_map(_read_then_bash_messages(entry_rel, bash_command))
    assert router._last_read_path == entry_rel

    dump = _wide_dump(entry_rel, related_rel)
    narrowed = router._graph_narrow("bash", "call_bash", dump)

    assert narrowed is not None, f"{case.id}: bash-shelled narrowing did not fire"
    assert related_rel in narrowed
    assert "noise_0.txt" not in narrowed
    assert len(narrowed) < len(dump)


def test_graph_narrow_telemetry_records_real_tokens_saved_for_a_new_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through `ContentRouter.apply()` (not the unit-level
    `_graph_narrow` call), matching the telemetry contract the proxy's
    PrometheusMetrics consumes -- proves the savings are visible on the
    metric a production dashboard would actually show, for a non-Python
    language.
    """
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = _java_setup(root)

    class _SpyObserver:
        def __init__(self) -> None:
            self.route_counts: dict | None = None

        def record_router_route_counts(self, route_counts: dict) -> None:
            self.route_counts = dict(route_counts)

    tokenizer = Tokenizer(OpenAIProvider().get_token_counter("gpt-4o"), "gpt-4o")
    spy = _SpyObserver()
    router = ContentRouter(
        config=ContentRouterConfig(protect_recent_reads_fraction=0.3), observer=spy
    )

    dump = _wide_dump(entry_rel, related_rel)
    messages = [
        *_read_then_grep_messages(entry_rel),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_grep", "content": dump}]},
    ]
    router.apply(messages, tokenizer)

    assert spy.route_counts is not None
    saved = spy.route_counts.get("graph_narrow_lossless_tokens_saved", 0)
    assert saved > 0, "no token savings recorded for a new-language (Java) graph-narrow pass"


def test_graph_narrow_lossless_roundtrip_retrieves_exact_original_for_new_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_graph_narrow_lossless` (the FRESH/still-protected path) must be
    byte-exactly recoverable via its CCR hash for a new language too, not
    just the drop-and-summarize `_graph_narrow` path already covered above.
    """
    import re

    from headroom.cache.compression_store import get_compression_store, reset_compression_store

    reset_compression_store()
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = _java_setup(root)

    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages(entry_rel))
    dump = _wide_dump(entry_rel, related_rel)

    folded = router._graph_narrow_lossless("grep", "call_grep", dump)

    assert folded is not None
    text, kind = folded
    assert kind == "graph_narrow"
    match = re.search(r"hash=([0-9a-f]+)", text)
    assert match is not None
    entry = get_compression_store().retrieve(match.group(1))
    assert entry is not None
    assert entry.original_content == dump


@pytest.mark.parametrize(
    "command",
    ["git grep Util .", 'sh -c "grep -r Util ."', "timeout 30 grep -r Util .", "cd . && grep -r Util ."],
    ids=["git-grep", "sh-c-wrapped", "timeout-wrapped", "cd-prefix"],
)
def test_graph_narrow_fires_for_wrapped_and_nested_bash_command_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """`_bash_command_is_search` (reused unchanged from the #1925 bash-search
    lossless fold) already peels `git grep`, `sh -c "..."`, wrapper commands
    (`timeout N ...`), and `cd X && ...` prefixes -- confirm graph_narrow
    still fires through each of those forms for a new language, not just
    the bare `grep -r` case the other tests use.
    """
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = _java_setup(root)

    router = ContentRouter()
    router._build_tool_name_map(_read_then_bash_messages(entry_rel, command))
    dump = _wide_dump(entry_rel, related_rel)

    narrowed = router._graph_narrow("bash", "call_bash", dump)

    assert narrowed is not None, f"command {command!r} did not trigger graph_narrow"
    assert related_rel in narrowed


def test_mixed_language_conversation_tracks_the_most_recent_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read a Java file, then a Kotlin file in the same conversation --
    `_last_read_path` (and therefore the graph-narrow entrypoint) must
    track the MOST RECENT read regardless of which language it is, not get
    stuck on whichever language was seen first.
    """
    root = tmp_path / "proj"
    (root / "com" / "acme").mkdir(parents=True)
    (root / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    (root / "Other.kt").write_text("import com.acme.Util\n", encoding="utf-8")
    (root / "com" / "acme" / "Util.kt").write_text("package com.acme\nclass Util\n", encoding="utf-8")
    monkeypatch.chdir(root)

    router = ContentRouter()
    messages = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "Main.java"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r1", "content": "..."}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "r2", "name": "Read", "input": {"file_path": "Other.kt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r2", "content": "..."}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "g1", "name": "Grep", "input": {"pattern": "Util"}}]},
    ]
    router._build_tool_name_map(messages)

    assert router._last_read_path == "Other.kt"

    dump = _wide_dump("Other.kt", "com/acme/Util.kt")
    narrowed = router._graph_narrow("grep", "g1", dump)

    assert narrowed is not None
    assert "com/acme/Util.kt" in narrowed


def test_java_graph_narrow_composes_with_downstream_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors the Python-only regression test in test_graph_narrow.py for a
    non-Python language: graph_narrow must be a pre-filter that still lets
    the downstream compressor (relevance_split/SearchCompressor) run on the
    narrowed remainder, not a short-circuit that finalizes the message
    itself.
    """
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = _java_setup(root)

    tokenizer = Tokenizer(OpenAIProvider().get_token_counter("gpt-4o"), "gpt-4o")
    router = ContentRouter(ContentRouterConfig(min_section_tokens=10, protect_recent_reads_fraction=0.3))

    wide_lines = [f"unrelated_{i}.java:{i}: Z = 1 some noise padding text here" for i in range(100)]
    wide_lines += [f"{entry_rel}:{i}: match number {i} padding text" for i in range(30)]
    wide_lines += [f"{related_rel}:{i}: match number {i} more padding text" for i in range(30)]
    wide_dump = "\n".join(wide_lines)

    messages = [
        *_read_then_grep_messages(entry_rel),
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_grep", "content": wide_dump}]},
    ]
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
    assert calls, "downstream compressor was never reached -- narrowing pre-empted the pipeline"
    assert "unrelated_0.java" not in calls[0]
    assert len(calls[0]) < len(wide_dump)


@pytest.mark.parametrize("case", _CASES, ids=[c.id for c in _CASES])
def test_real_tokenizer_savings_exceed_90_percent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: _LangCase
) -> None:
    """The len//4 heuristic used by the other tests in this file is a
    proxy -- this measures ACTUAL token count via the same tiktoken-backed
    counter the proxy uses in production (`OpenAIProvider`/gpt-4o encoding),
    confirming the savings claim holds on real tokens, not characters.
    """
    from headroom.providers import OpenAIProvider
    from headroom.tokenizer import Tokenizer

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.chdir(root)
    entry_rel, related_rel = case.setup(root)

    router = ContentRouter()
    router._build_tool_name_map(_read_then_grep_messages(entry_rel))
    dump = _wide_dump(entry_rel, related_rel)
    narrowed = router._graph_narrow("grep", "call_grep", dump)
    assert narrowed is not None

    tokenizer = Tokenizer(OpenAIProvider().get_token_counter("gpt-4o"), "gpt-4o")
    before = tokenizer.count_text(dump)
    after = tokenizer.count_text(narrowed)

    savings = 1 - (after / before)
    assert savings > 0.9, f"{case.id}: only {savings:.1%} real-token savings (before={before}, after={after})"
