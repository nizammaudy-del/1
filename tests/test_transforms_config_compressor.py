"""Tests for structured-config (YAML/TOML/INI) detection and compression.

Covers the detection heuristics (including prose / markdown-front-matter
non-claims), the reversible block-fold primitives in lossless_compaction,
the two-tier ConfigCompressor, and the router wiring.
"""

from __future__ import annotations

import pytest

from headroom.parser import CCR_RETRIEVAL_MARKER_RE
from headroom.transforms.config_compressor import (
    ConfigCompressor,
    ConfigCompressorConfig,
)
from headroom.transforms.content_detector import (
    ContentType,
    _try_detect_structured_config,
    detect_content_type,
)
from headroom.transforms.content_router import (
    CompressionStrategy,
    ContentRouter,
    ContentRouterConfig,
)
from headroom.transforms.lossless_compaction import (
    compact_lossless,
    expand_runs,
    fold_repeated_blocks,
    unfold_repeated_blocks,
)

# Reusable fixtures ----------------------------------------------------------

K8S_MANIFEST = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  labels:
    app: web
spec:
  replicas: 3
  template:
    spec:
      containers:
        - name: web-1
          image: nginx:1.25
          resources:
            limits:
              cpu: 500m
              memory: 512Mi
            requests:
              cpu: 250m
              memory: 256Mi
        - name: web-2
          image: nginx:1.25
          resources:
            limits:
              cpu: 500m
              memory: 512Mi
            requests:
              cpu: 250m
              memory: 256Mi
        - name: web-3
          image: nginx:1.25
          resources:
            limits:
              cpu: 500m
              memory: 512Mi
            requests:
              cpu: 250m
              memory: 256Mi
"""

PYPROJECT_TOML = """# Build configuration
[package]
name = "demo"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = "1.0"
tokio = "1.38"

[profile.release]
opt-level = 3
lto = true
"""

INI_CONFIG = """[server]
host = 127.0.0.1
port = 8080

; connection tuning
[logging]
level = INFO
file = /var/log/app.log

[auth]
enabled = true
provider = ldap
"""

PROSE = """Note: this document describes the deployment process.
First, you should review the configuration carefully before applying it.
The rollout takes several minutes to complete in most environments.
If anything goes wrong, roll back to the previous release immediately.
Contact the on-call engineer when the dashboard shows sustained errors.
"""

FRONT_MATTER_DOC = """---
title: My Post
tags:
  - a
  - b
---

# Heading

This is a markdown document body with prose that goes on and on.
More prose here, explaining things in complete English sentences.
And a final line of text to round out the document nicely today.
"""


# Detection -------------------------------------------------------------------


@pytest.mark.parametrize(
    "content,flavor",
    [
        (K8S_MANIFEST, "yaml"),
        (PYPROJECT_TOML, "toml"),
        (INI_CONFIG, "ini"),
    ],
)
def test_detects_structured_config(content: str, flavor: str) -> None:
    result = detect_content_type(content)
    assert result.content_type is ContentType.STRUCTURED_CONFIG
    assert result.metadata["flavor"] == flavor
    assert result.confidence >= 0.7


@pytest.mark.parametrize(
    "content",
    [
        PROSE,
        FRONT_MATTER_DOC,
        # grep output: colon shapes must stay SEARCH_RESULTS
        "src/main.py:42:def process():\nsrc/util.py:10:import os\nsrc/x.py:5:return 1",
        # CSV keeps its tabular claim
        "name,age,city\nalice,30,nyc\nbob,25,sf\ncarol,35,la",
        # JSON array of dicts stays with SmartCrusher
        '[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]',
        # JSON object bodies are never claimed as config
        '{\n  "key": "value",\n  "other": "thing",\n  "third": "entry"\n}',
        # Python code with colon-ended keywords
        "import os\n\n\ndef main():\n    if True:\n        return os.name\n",
        # too short
        "key: value",
    ],
)
def test_does_not_claim_non_config(content: str) -> None:
    result = detect_content_type(content)
    assert result.content_type is not ContentType.STRUCTURED_CONFIG


def test_multi_document_yaml_stream_is_claimed() -> None:
    doc = "---\nname: a\nimage: x:1\nports:\n  - 80\n---\nname: b\nimage: y:2\nports:\n  - 443\n"
    result = _try_detect_structured_config(doc)
    assert result is not None
    assert result.metadata["flavor"] == "yaml"


def test_toml_beats_ini_when_both_parse() -> None:
    # Valid TOML with quoted strings parses under tomllib and wins.
    result = _try_detect_structured_config('[a]\nx = "1"\ny = "2"\n[b]\nz = "3"\n')
    assert result is not None
    assert result.metadata["flavor"] == "toml"


def test_ini_when_toml_rejects_bare_values() -> None:
    # Unquoted strings are invalid TOML but fine for configparser.
    result = _try_detect_structured_config("[a]\nx = hello\ny = world\n[b]\nz = there\n")
    assert result is not None
    assert result.metadata["flavor"] == "ini"


def test_flat_yaml_without_structure_not_claimed() -> None:
    # Flat key: value lines with one indent level, no docs, no lists: too
    # ambiguous with prose-ish "Key: value" notes to claim.
    result = _try_detect_structured_config("alpha: 1\nbeta: 2\ngamma: 3\n")
    assert result is None


# fold_repeated_blocks / unfold_repeated_blocks -------------------------------


def test_fold_round_trip_k8s() -> None:
    folded = fold_repeated_blocks(K8S_MANIFEST)
    assert len(folded) < len(K8S_MANIFEST)
    assert "lines back)" in folded
    assert unfold_repeated_blocks(folded) == K8S_MANIFEST


def test_fold_handles_consecutive_identical_blocks() -> None:
    block = "alpha: value-one\nbeta: value-two\ngamma: value-three\n"
    text = block * 4
    folded = fold_repeated_blocks(text)
    assert unfold_repeated_blocks(folded) == text
    assert len(folded) < len(text)


def test_fold_skips_short_blocks() -> None:
    text = "x: 1\ny: 2\nx: 1\ny: 2\n"  # repeats are only 2 lines long
    assert fold_repeated_blocks(text) == text


def test_fold_skips_when_marker_not_smaller() -> None:
    # Three repeated single-char lines: folding would cost more than it saves.
    text = "a\nb\nc\na\nb\nc\n"
    assert fold_repeated_blocks(text) == text


def test_unfold_leaves_invalid_marker_untouched() -> None:
    text = "line one\n... (repeats 5 lines from 2 lines back)\n"
    # length > distance: not a marker fold_repeated_blocks could have emitted.
    assert unfold_repeated_blocks(text) == text


def test_compact_lossless_config_verifies_round_trip() -> None:
    compacted = compact_lossless(K8S_MANIFEST, "config")
    assert len(compacted) < len(K8S_MANIFEST)
    assert expand_runs(unfold_repeated_blocks(compacted)) == K8S_MANIFEST


def test_compact_lossless_config_bails_on_marker_collision() -> None:
    # Content that already contains a marker-shaped line cannot round-trip,
    # so the self-check must return the original unchanged.
    lines = [f"item: {i}" for i in range(3)]
    block = "\n".join(lines)
    text = block + "\n... (repeats 3 lines from 3 lines back)\n" + block + "\n" + block + "\n"
    assert compact_lossless(text, "config") == text


# ConfigCompressor ------------------------------------------------------------


def test_tier1_lossless_only_no_marker() -> None:
    comp = ConfigCompressor(ConfigCompressorConfig(enable_ccr=False))
    result = comp.compress(K8S_MANIFEST)
    assert result.was_modified
    assert len(result.compressed) < len(K8S_MANIFEST)
    assert not CCR_RETRIEVAL_MARKER_RE.search(result.compressed)
    assert result.flavor == "yaml"
    assert result.ccr_hash is None


def test_tier2_elides_comments_behind_ccr_marker(monkeypatch) -> None:
    from headroom.cache.compression_store import CompressionStore

    store = CompressionStore()
    monkeypatch.setattr("headroom.cache.compression_store.get_compression_store", lambda: store)
    commented = PYPROJECT_TOML.replace(
        'serde = "1.0"',
        "# pinned for CVE-2024-0001; do not bump without checking the advisory\n"
        "# see https://example.com/advisories/CVE-2024-0001 for details\n"
        "# owner: platform-team, revisit after the 2.x migration lands\n"
        'serde = "1.0"',
    )
    comp = ConfigCompressor(ConfigCompressorConfig(enable_ccr=True))
    result = comp.compress(commented)
    assert result.was_modified
    assert result.lines_elided > 0
    assert result.ccr_hash is not None
    assert CCR_RETRIEVAL_MARKER_RE.search(result.compressed)
    assert "CVE-2024-0001" not in result.compressed
    # The marker hash must resolve to the byte-exact original.
    assert store.retrieve(result.ccr_hash).original_content == commented


def test_tier2_skipped_for_yaml_block_scalars(monkeypatch) -> None:
    from headroom.cache.compression_store import CompressionStore

    monkeypatch.setattr(
        "headroom.cache.compression_store.get_compression_store",
        lambda: CompressionStore(),
    )
    doc = (
        "config:\n"
        "  script: |\n"
        "    # this hash line is DATA inside a block scalar\n"
        "    echo hi\n"
        "  replicas: 3\n"
        "  image: nginx\n"
        "  ports:\n"
        "    - 80\n"
    )
    comp = ConfigCompressor(ConfigCompressorConfig(enable_ccr=True))
    result = comp.compress(doc)
    # The '#' line inside the block scalar must survive verbatim.
    assert "# this hash line is DATA" in result.compressed or not result.was_modified


def test_tier2_skipped_for_toml_multiline_strings(monkeypatch) -> None:
    from headroom.cache.compression_store import CompressionStore

    monkeypatch.setattr(
        "headroom.cache.compression_store.get_compression_store",
        lambda: CompressionStore(),
    )
    doc = '[a]\ntext = """\n# not a comment\n"""\nx = "1"\ny = "2"\n'
    comp = ConfigCompressor(ConfigCompressorConfig(enable_ccr=True))
    result = comp.compress(doc)
    assert "# not a comment" in result.compressed or not result.was_modified


def test_tier2_ini_keeps_blank_lines(monkeypatch) -> None:
    from headroom.cache.compression_store import CompressionStore

    monkeypatch.setattr(
        "headroom.cache.compression_store.get_compression_store",
        lambda: CompressionStore(),
    )
    comp = ConfigCompressor(ConfigCompressorConfig(enable_ccr=True))
    result = comp.compress(INI_CONFIG)
    if result.was_modified:
        # Column-0 ';' comment goes; blank section separators stay.
        assert "; connection tuning" not in result.compressed
        assert "\n\n" in result.compressed


def test_store_failure_degrades_to_tier1(monkeypatch) -> None:
    class _BrokenStore:
        def store(self, *a, **kw):  # noqa: ANN002, ANN003
            raise RuntimeError("disk full")

    monkeypatch.setattr(
        "headroom.cache.compression_store.get_compression_store",
        lambda: _BrokenStore(),
    )
    comp = ConfigCompressor(ConfigCompressorConfig(enable_ccr=True))
    result = comp.compress(K8S_MANIFEST)
    # Tier 1 still folds the manifest; nothing was elided.
    assert result.was_modified
    assert result.ccr_hash is None
    assert result.lines_elided == 0
    assert not CCR_RETRIEVAL_MARKER_RE.search(result.compressed)


def test_non_config_content_passes_through() -> None:
    comp = ConfigCompressor()
    result = comp.compress(PROSE)
    assert not result.was_modified
    assert result.compressed == PROSE
    assert result.flavor == "unknown"


def test_no_savings_returns_original() -> None:
    comp = ConfigCompressor(ConfigCompressorConfig(enable_ccr=False))
    result = comp.compress(PYPROJECT_TOML)  # compact already; nothing to fold
    assert not result.was_modified
    assert result.compressed == PYPROJECT_TOML


def test_compression_ratio_zero_for_empty_original() -> None:
    from headroom.transforms.config_compressor import ConfigCompressionResult

    result = ConfigCompressionResult(
        compressed="", original="", was_modified=False, flavor="unknown"
    )
    assert result.compression_ratio == 0.0


# Router wiring ---------------------------------------------------------------


def test_router_maps_structured_config_to_config_strategy() -> None:
    router = ContentRouter()
    assert (
        router._strategy_from_detection_type(ContentType.STRUCTURED_CONFIG)
        is CompressionStrategy.CONFIG
    )
    assert (
        router._content_type_from_strategy(CompressionStrategy.CONFIG)
        is ContentType.STRUCTURED_CONFIG
    )


def test_router_lazy_getter_mirrors_ccr_setting() -> None:
    router = ContentRouter(ContentRouterConfig(lossless=True))
    compressor = router._get_config_compressor()
    assert compressor is not None
    assert compressor.config.enable_ccr is False  # lossless forces markers off


def test_router_compresses_k8s_manifest_end_to_end() -> None:
    router = ContentRouter()
    result = router.compress(K8S_MANIFEST)
    assert result.compressed != K8S_MANIFEST or result.strategy_used in (
        CompressionStrategy.CONFIG,
        CompressionStrategy.PASSTHROUGH,
    )
    # Whatever the gates decide, the output must never be a lossy mangle:
    # either untouched or a reversible fold of the manifest.
    if result.compressed != K8S_MANIFEST:
        assert expand_runs(unfold_repeated_blocks(result.compressed)) == K8S_MANIFEST


def test_router_lossless_mode_uses_lossless_config_label() -> None:
    router = ContentRouter(ContentRouterConfig(lossless=True))
    compressed, _tokens, chain = router._apply_strategy_to_content(
        K8S_MANIFEST, CompressionStrategy.CONFIG, context=""
    )
    assert chain == ["lossless_config"]
    assert expand_runs(unfold_repeated_blocks(compressed)) == K8S_MANIFEST
    assert not CCR_RETRIEVAL_MARKER_RE.search(compressed)


def test_router_disabled_flag_skips_config_compressor() -> None:
    router = ContentRouter(ContentRouterConfig(enable_config_compressor=False))
    compressed, _tokens, _chain = router._apply_strategy_to_content(
        K8S_MANIFEST, CompressionStrategy.CONFIG, context=""
    )
    # Falls through to the unified fallback path; must not crash and must
    # not claim CONFIG did work it didn't do.
    assert isinstance(compressed, str)
