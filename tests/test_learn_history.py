"""Tests for headroom.cli.learn_history — the /learn/history reader.

Covers the fix from a Copilot review: read_learn_history() now streams the
file through a bounded deque instead of loading it fully into memory before
slicing, since the dashboard polls this endpoint frequently.
"""

import json

import pytest

from headroom.cli import learn_history


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))
    yield


def test_missing_file_returns_empty():
    assert learn_history.read_learn_history() == []


def test_record_then_read_round_trips():
    learn_history.record_learn_run({"run": 1})
    learn_history.record_learn_run({"run": 2})
    result = learn_history.read_learn_history()
    assert [r["run"] for r in result] == [1, 2]


def test_read_keeps_only_last_limit_entries():
    for i in range(10):
        learn_history.record_learn_run({"run": i})
    result = learn_history.read_learn_history(limit=3)
    assert [r["run"] for r in result] == [7, 8, 9]


def test_limit_zero_or_negative_is_clamped_to_one():
    learn_history.record_learn_run({"run": 1})
    learn_history.record_learn_run({"run": 2})
    assert len(learn_history.read_learn_history(limit=0)) == 1
    assert len(learn_history.read_learn_history(limit=-5)) == 1


def test_corrupt_lines_are_skipped(tmp_path):
    path = learn_history.workspace_dir() / learn_history._HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "not json\n" + json.dumps({"run": 1}) + "\n[1, 2]\n" + json.dumps({"run": 2}) + "\n",
        encoding="utf-8",
    )
    result = learn_history.read_learn_history()
    assert [r["run"] for r in result] == [1, 2]


def test_record_learn_run_never_raises_on_write_failure(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _boom)
    learn_history.record_learn_run({"run": 1})  # must not raise
