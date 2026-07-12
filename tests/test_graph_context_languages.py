"""Tests for the tree-sitter-backed language extractors in
``headroom.graph_context`` (Go, Java, C#, Ruby, PHP, Kotlin, Scala, Dart,
Lua, Zig).

Requires the optional ``[code]`` extra (``tree-sitter-language-pack``) --
every extractor is fail-open without it (see `test_missing_tree_sitter_*`
below), so these tests are skipped rather than failed when it isn't
installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from headroom import graph_context as gc

pytest.importorskip("tree_sitter_language_pack")


@pytest.fixture(autouse=True)
def _stop_watchers_between_tests():
    yield
    gc.stop_all_watchers()


def test_go_resolves_internal_package_directory_not_stdlib(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "pkg" / "util").mkdir(parents=True)
    (root / "go.mod").write_text("module myproj\n\ngo 1.21\n", encoding="utf-8")
    (root / "main.go").write_text(
        'package main\n\nimport (\n\t"fmt"\n\t"myproj/pkg/util"\n)\n', encoding="utf-8"
    )
    (root / "pkg" / "util" / "util.go").write_text("package util\nvar X = 1\n", encoding="utf-8")
    (root / "pkg" / "util" / "other.go").write_text("package util\nvar Y = 2\n", encoding="utf-8")

    graph = gc.build_graph(root)

    # A Go import resolves to every file in the PACKAGE (directory), not one file.
    assert set(graph.edges["main.go"]) == {"pkg/util/util.go", "pkg/util/other.go"}


def test_go_package_import_excludes_the_importer_itself(tmp_path: Path) -> None:
    """A file's own package can never be a real dependency of itself in
    valid Go, but the directory-glob resolution (`_extract_go_imports`)
    would otherwise include the importer if it happens to sit in the
    resolved package directory -- `build_graph` filters self-edges
    regardless of which extractor produced them.
    """
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "go.mod").write_text("module myproj\n", encoding="utf-8")
    (root / "root.go").write_text("package main\n", encoding="utf-8")
    (root / "main.go").write_text('package main\nimport "myproj"\n', encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["main.go"] == ["root.go"]  # not main.go itself


def test_csharp_namespace_import_excludes_the_importer_itself(tmp_path: Path) -> None:
    """A redundant `using` of one's own namespace (legal, if pointless, C#)
    would otherwise include the importer via the directory glob -- must not.
    """
    root = tmp_path / "proj"
    (root / "Self").mkdir(parents=True)
    (root / "Self" / "Program.cs").write_text(
        "using Self;\nclass Program {}\n", encoding="utf-8"
    )
    (root / "Self" / "Other.cs").write_text("namespace Self { class Other {} }\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["Self/Program.cs"] == ["Self/Other.cs"]  # not Self/Program.cs itself


def test_go_import_without_go_mod_is_unresolved(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "main.go").write_text('package main\nimport "fmt"\n', encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["main.go"] == []


def test_java_resolves_qualified_import_to_unique_file(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.java").write_text("import com.acme.pkg.Util;\nclass Main {}\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.java").write_text(
        "package com.acme.pkg;\nclass Util {}\n", encoding="utf-8"
    )

    graph = gc.build_graph(root)

    assert graph.edges["Main.java"] == ["com/acme/pkg/Util.java"]


def test_java_external_jdk_import_is_unresolved(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "Main.java").write_text("import java.util.List;\nclass Main {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["Main.java"] == []


def test_java_static_import_member_access_does_not_resolve_wrong(tmp_path: Path) -> None:
    """`import static a.b.Other.thing;` must not resolve to a nonexistent
    `Other/thing.java` -- the trailing member segment isn't a file.
    """
    root = tmp_path / "proj"
    (root / "a" / "b").mkdir(parents=True)
    (root / "Main.java").write_text(
        "import static a.b.Other.thing;\nclass Main {}\n", encoding="utf-8"
    )
    (root / "a" / "b" / "Other.java").write_text("package a.b;\nclass Other {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["Main.java"] == []


def test_java_ambiguous_qualified_name_across_two_files_does_not_resolve(tmp_path: Path) -> None:
    """A dotted name matching MORE THAN ONE file must resolve to neither --
    never wrong, only fewer edges.
    """
    root = tmp_path / "proj"
    (root / "a" / "pkg").mkdir(parents=True)
    (root / "b" / "pkg").mkdir(parents=True)
    (root / "Main.java").write_text("import pkg.Util;\nclass Main {}\n", encoding="utf-8")
    (root / "a" / "pkg" / "Util.java").write_text("package pkg;\nclass Util {}\n", encoding="utf-8")
    (root / "b" / "pkg" / "Util.java").write_text("package pkg;\nclass Util {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["Main.java"] == []


def test_csharp_resolves_using_namespace_to_every_file_in_directory(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "Services").mkdir(parents=True)
    (root / "Program.cs").write_text("using Services;\nclass Program {}\n", encoding="utf-8")
    (root / "Services" / "Thing.cs").write_text(
        "namespace Services { class Thing {} }\n", encoding="utf-8"
    )
    (root / "Services" / "Other.cs").write_text(
        "namespace Services { class Other {} }\n", encoding="utf-8"
    )

    graph = gc.build_graph(root)

    assert set(graph.edges["Program.cs"]) == {"Services/Thing.cs", "Services/Other.cs"}


def test_csharp_using_static_and_alias_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "Services").mkdir(parents=True)
    (root / "Program.cs").write_text(
        "using static Services.Thing;\nusing S = Services;\nclass Program {}\n", encoding="utf-8"
    )
    (root / "Services" / "Thing.cs").write_text(
        "namespace Services { class Thing {} }\n", encoding="utf-8"
    )

    graph = gc.build_graph(root)

    assert graph.edges["Program.cs"] == []


def test_ruby_resolves_require_relative_but_not_bare_require(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "lib").mkdir(parents=True)
    (root / "main.rb").write_text(
        'require "json"\nrequire_relative "lib/helper"\n', encoding="utf-8"
    )
    (root / "lib" / "helper.rb").write_text("def helper; end\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["main.rb"] == ["lib/helper.rb"]


def test_php_resolves_require_but_not_use_namespace(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "lib").mkdir(parents=True)
    (root / "App" / "Services").mkdir(parents=True)
    (root / "main.php").write_text(
        '<?php\nuse App\\Services\\Thing;\nrequire_once __DIR__ . "/lib/helper.php";\n',
        encoding="utf-8",
    )
    (root / "lib" / "helper.php").write_text("<?php\nfunction helper() {}\n", encoding="utf-8")
    (root / "App" / "Services" / "Thing.php").write_text("<?php\nclass Thing {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    # `require_once __DIR__ . "/lib/helper.php"` resolves; `use` (PSR-4) does not.
    assert graph.edges["main.php"] == ["lib/helper.php"]


def test_kotlin_resolves_qualified_import(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.kt").write_text("import com.acme.pkg.Util\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.kt").write_text(
        "package com.acme.pkg\nclass Util\n", encoding="utf-8"
    )

    graph = gc.build_graph(root)

    assert graph.edges["Main.kt"] == ["com/acme/pkg/Util.kt"]


def test_scala_brace_multi_import_expands_and_wildcard_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.scala").write_text(
        "import com.acme.pkg.{Other, Thing}\nimport com.acme.pkg._\n", encoding="utf-8"
    )
    (root / "com" / "acme" / "pkg" / "Other.scala").write_text(
        "package com.acme.pkg\nclass Other\n", encoding="utf-8"
    )
    (root / "com" / "acme" / "pkg" / "Thing.scala").write_text(
        "package com.acme.pkg\nclass Thing\n", encoding="utf-8"
    )

    graph = gc.build_graph(root)

    assert set(graph.edges["Main.scala"]) == {"com/acme/pkg/Other.scala", "com/acme/pkg/Thing.scala"}


def test_dart_resolves_relative_import_without_dot_prefix(tmp_path: Path) -> None:
    """Idiomatic Dart writes relative imports WITHOUT `./` (unlike JS/TS)."""
    root = tmp_path / "proj"
    (root / "lib").mkdir(parents=True)
    (root / "main.dart").write_text(
        'import "lib/helper.dart";\nimport "package:foo/bar.dart";\n', encoding="utf-8"
    )
    (root / "lib" / "helper.dart").write_text("void helper() {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["main.dart"] == ["lib/helper.dart"]


def test_lua_resolves_dotted_require_and_relative_require(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "foo").mkdir(parents=True)
    (root / "main.lua").write_text('local bar = require("foo.bar")\n', encoding="utf-8")
    (root / "foo" / "bar.lua").write_text("return {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["main.lua"] == ["foo/bar.lua"]


def test_zig_resolves_relative_import_but_not_std(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "main.zig").write_text(
        'const std = @import("std");\nconst foo = @import("./foo.zig");\n', encoding="utf-8"
    )
    (root / "foo.zig").write_text("pub const X = 1;\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["main.zig"] == ["foo.zig"]


def test_swift_and_elixir_are_not_source_extensions() -> None:
    """Module-based imports with no reliable file-level resolution are not
    attempted at all -- documented accepted gap, not an oversight.
    """
    assert ".swift" not in gc.SOURCE_EXTENSIONS
    assert ".ex" not in gc.SOURCE_EXTENSIONS


def test_new_languages_degrade_to_zero_edges_without_tree_sitter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-open contract: with the `[code]` extra unavailable, files in the
    ten new languages still become graph nodes but contribute no edges --
    never a crash, matching every other extractor in this module.
    """
    from headroom.transforms import code_compressor

    monkeypatch.setattr(code_compressor, "_tree_sitter_importable", lambda: False)

    root = tmp_path / "proj"
    (root / "com" / "acme" / "pkg").mkdir(parents=True)
    (root / "Main.java").write_text("import com.acme.pkg.Util;\nclass Main {}\n", encoding="utf-8")
    (root / "com" / "acme" / "pkg" / "Util.java").write_text(
        "package com.acme.pkg;\nclass Util {}\n", encoding="utf-8"
    )

    graph = gc.build_graph(root)

    assert "Main.java" in graph.edges  # still a node
    assert graph.edges["Main.java"] == []  # but no edges without tree-sitter


# =============================================================================
# Robustness battery: malformed/empty input, dedup, cache invalidation, and
# unusual-but-legal paths must not crash `build_graph` or silently misbehave.
# =============================================================================


def test_truncated_java_file_does_not_crash_build_graph(tmp_path: Path) -> None:
    """Mid-write/truncated source is a realistic live-agent scenario (a file
    the watcher observes between an agent's write() and a syntactically
    complete save). tree-sitter is error-tolerant; this pins that
    `build_graph` never raises on it, whatever it manages to resolve.
    """
    root = tmp_path / "proj"
    (root / "com" / "acme").mkdir(parents=True)
    (root / "Main.java").write_text("import com.acme.Util;\nclass Main { void x(", encoding="utf-8")
    (root / "com" / "acme" / "Util.java").write_text("package com.acme;\nclass Util {}\n", encoding="utf-8")

    graph = gc.build_graph(root)  # must not raise

    assert "Main.java" in graph.edges


@pytest.mark.parametrize(
    "ext", [".go", ".java", ".cs", ".rb", ".php", ".kt", ".scala", ".dart", ".lua", ".zig"]
)
def test_empty_file_does_not_crash_and_has_no_edges(tmp_path: Path, ext: str) -> None:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    if ext == ".go":
        (root / "go.mod").write_text("module myproj\n", encoding="utf-8")
    (root / f"empty{ext}").write_text("", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges[f"empty{ext}"] == []


def test_duplicate_import_statements_dedupe_to_one_edge(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "com" / "acme").mkdir(parents=True)
    (root / "Main.java").write_text(
        "import com.acme.Util;\nimport com.acme.Util;\nclass Main {}\n", encoding="utf-8"
    )
    (root / "com" / "acme" / "Util.java").write_text("package com.acme;\nclass Util {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["Main.java"] == ["com/acme/Util.java"]


def test_editing_an_import_invalidates_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`load_or_build_graph`'s content-hash cache must notice a NEW-language
    file's import changing, not just its existence -- editing `Main.java` to
    add an import must produce a fresh graph on the next call, not the
    stale cached one.
    """
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path / "_ws"))
    root = tmp_path / "proj"
    (root / "com" / "acme").mkdir(parents=True)
    (root / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    (root / "com" / "acme" / "Util.java").write_text("package com.acme;\nclass Util {}\n", encoding="utf-8")

    before = gc.load_or_build_graph(root)
    assert before.edges["Main.java"] == []

    (root / "Main.java").write_text("import com.acme.Util;\nclass Main {}\n", encoding="utf-8")
    after = gc.load_or_build_graph(root)

    assert after.edges["Main.java"] == ["com/acme/Util.java"]


def test_go_package_directory_with_a_space_in_its_name_resolves(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "my pkg").mkdir(parents=True)
    (root / "go.mod").write_text("module myproj\n", encoding="utf-8")
    (root / "main.go").write_text('package main\nimport "myproj/my pkg"\n', encoding="utf-8")
    (root / "my pkg" / "util.go").write_text("package pkg\nvar X = 1\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["main.go"] == ["my pkg/util.go"]


# =============================================================================
# BFS traversal correctness for the new languages: transitivity, cycles,
# case-sensitivity, package-glob depth accounting, and the end-to-end
# assemble_context() builder -- not just single-hop edge resolution.
# =============================================================================


def test_java_transitive_bfs_reaches_second_hop_at_depth_two(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "com" / "acme").mkdir(parents=True)
    (root / "A.java").write_text("import com.acme.B;\nclass A {}\n", encoding="utf-8")
    (root / "com" / "acme" / "B.java").write_text(
        "package com.acme;\nimport com.acme.C;\nclass B {}\n", encoding="utf-8"
    )
    (root / "com" / "acme" / "C.java").write_text("package com.acme;\nclass C {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert set(gc.bfs_related_files(graph, "A.java", max_depth=2)) == {
        "A.java",
        "com/acme/B.java",
        "com/acme/C.java",
    }
    assert set(gc.bfs_related_files(graph, "A.java", max_depth=1)) == {"A.java", "com/acme/B.java"}


def test_kotlin_circular_import_terminates(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "com" / "acme").mkdir(parents=True)
    (root / "com" / "acme" / "A.kt").write_text("package com.acme\nimport com.acme.B\n", encoding="utf-8")
    (root / "com" / "acme" / "B.kt").write_text("package com.acme\nimport com.acme.A\n", encoding="utf-8")

    graph = gc.build_graph(root)

    reached = gc.bfs_related_files(graph, "com/acme/A.kt", max_depth=5)
    assert set(reached) == {"com/acme/A.kt", "com/acme/B.kt"}


def test_java_case_sensitive_mismatch_does_not_resolve(tmp_path: Path) -> None:
    """Import references the wrong case (`util` vs `Util.java`) -- must fail
    to resolve, never crash, never guess a case-insensitive match.
    """
    root = tmp_path / "proj"
    (root / "com" / "acme").mkdir(parents=True)
    (root / "Main.java").write_text("import com.acme.util;\nclass Main {}\n", encoding="utf-8")
    (root / "com" / "acme" / "Util.java").write_text("package com.acme;\nclass Util {}\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert graph.edges["Main.java"] == []


def test_go_package_glob_files_all_land_at_depth_one(tmp_path: Path) -> None:
    """Importing a Go package with several files must count as ONE BFS hop
    for all of them, not force a caller to raise max_depth to reach later
    files in the same package.
    """
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "go.mod").write_text("module myproj\n", encoding="utf-8")
    (root / "main.go").write_text('package main\nimport "myproj/pkg"\n', encoding="utf-8")
    (root / "pkg" / "a.go").write_text("package pkg\nvar X = 1\n", encoding="utf-8")
    (root / "pkg" / "b.go").write_text("package pkg\nvar Y = 2\n", encoding="utf-8")
    (root / "pkg" / "c.go").write_text("package pkg\nvar Z = 3\n", encoding="utf-8")

    graph = gc.build_graph(root)

    assert set(gc.bfs_related_files(graph, "main.go", max_depth=1)) == {
        "main.go",
        "pkg/a.go",
        "pkg/b.go",
        "pkg/c.go",
    }


def test_assemble_context_includes_new_language_files_verbatim(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "lib").mkdir(parents=True)
    (root / "main.rb").write_text('require_relative "lib/helper"\nputs "hi"\n', encoding="utf-8")
    (root / "lib" / "helper.rb").write_text("def helper\n  42\nend\n", encoding="utf-8")

    result = gc.assemble_context(root, "main.rb", query="what does helper do?")

    assert "def helper" in result.context
    assert 'puts "hi"' in result.context
    assert "lib/helper.rb" in result.context
    assert set(result.files) == {"main.rb", "lib/helper.rb"}
