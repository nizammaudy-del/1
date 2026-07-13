"""Tests for headroom.memory.sync_stats — the /debug/memory/sync reader.

Covers the fixes from a Copilot review: sync.py only ever persists one
combined `last_sync` timestamp plus item counts (`last_imported`/
`last_exported`), never a dedup count, so the reader must not fabricate two
separate timestamps or a fake dedup_rate, and must not crash on a corrupted
count field.
"""

import json

import pytest

from headroom.memory import sync_stats


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))
    yield


def _state_path(tmp_path):
    from headroom import paths

    return paths.sync_state_path()


def test_missing_state_file_returns_empty(tmp_path):
    assert sync_stats.get_sync_stats() == {"agents": [], "dedup_rate": None}


def test_corrupt_state_file_returns_empty(tmp_path):
    path = _state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert sync_stats.get_sync_stats() == {"agents": [], "dedup_rate": None}


def test_reports_one_honest_last_sync_timestamp_and_counts(tmp_path):
    path = _state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claude:user1": {
                    "last_sync": "2026-01-01T00:00:00+00:00",
                    "last_imported": 5,
                    "last_exported": 3,
                }
            }
        ),
        encoding="utf-8",
    )
    result = sync_stats.get_sync_stats()
    assert result["dedup_rate"] is None  # never fabricated
    assert result["agents"] == [
        {
            "name": "claude",
            "last_sync": "2026-01-01T00:00:00+00:00",
            "imported_count": 5,
            "exported_count": 3,
        }
    ]


def test_corrupted_count_field_does_not_crash(tmp_path):
    path = _state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "codex:user1": {
                    "last_sync": None,
                    "last_imported": "not-a-number",
                    "last_exported": None,
                }
            }
        ),
        encoding="utf-8",
    )
    result = sync_stats.get_sync_stats()
    assert result["agents"][0]["imported_count"] == 0
    assert result["agents"][0]["exported_count"] == 0


def test_non_dict_entries_are_skipped(tmp_path):
    path = _state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"broken:user1": "not-a-dict"}), encoding="utf-8")
    assert sync_stats.get_sync_stats() == {"agents": [], "dedup_rate": None}
