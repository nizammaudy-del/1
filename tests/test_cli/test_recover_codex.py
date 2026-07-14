from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from headroom.cli.main import main
from headroom.providers.codex.recovery import discover_dangling_homes, recover_codex_home


def _write_db(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT NOT NULL)")
        connection.executemany("INSERT INTO threads VALUES (?, ?)", rows)


def test_discover_dangling_homes_only_returns_codex_homes(tmp_path: Path) -> None:
    candidate = tmp_path / "headroom-codex-home-abc"
    candidate.mkdir()
    (candidate / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    (tmp_path / "headroom-codex-home-empty").mkdir()
    (tmp_path / "other").mkdir()

    assert discover_dangling_homes(tmp_path) == [candidate]


def test_recovery_merges_files_config_and_sqlite_with_backups(tmp_path: Path) -> None:
    target = tmp_path / "codex"
    source = tmp_path / "headroom-codex-home-broken"
    target.mkdir()
    source.mkdir()
    (target / "config.toml").write_text(
        'model = "target-model"\n[features]\nexisting = true\n', encoding="utf-8"
    )
    (source / "config.toml").write_text(
        'model = "source-model"\n[features]\nfrom_wrap = true\n', encoding="utf-8"
    )
    rollout = source / "sessions" / "2026" / "07" / "14" / "rollout.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    _write_db(target / "sqlite" / "state_5.sqlite", [("target", "Target")])
    _write_db(source / "sqlite" / "state_5.sqlite", [("source", "Source")])

    report = recover_codex_home(source=source, target=target)

    config = (target / "config.toml").read_text(encoding="utf-8")
    assert 'model = "source-model"' in config
    assert "existing = true" in config
    assert "from_wrap = true" in config
    assert rollout.relative_to(source).with_name("rollout.jsonl")
    assert (target / rollout.relative_to(source)).read_text(encoding="utf-8") == (
        '{"type":"session_meta"}\n'
    )
    with sqlite3.connect(target / "sqlite" / "state_5.sqlite") as connection:
        assert connection.execute("SELECT id, title FROM threads ORDER BY id").fetchall() == [
            ("source", "Source"),
            ("target", "Target"),
        ]
    assert report.backup_dir.is_dir()
    assert (report.backup_dir / "target-before").is_dir()
    assert (report.backup_dir / "source-pinned").is_dir()
    assert (report.backup_dir / "manifest.json").is_file()


def test_recovery_rolls_back_when_sqlite_schema_differs(tmp_path: Path) -> None:
    target = tmp_path / "codex"
    source = tmp_path / "headroom-codex-home-broken"
    target.mkdir()
    source.mkdir()
    original = 'model = "target"\n'
    (target / "config.toml").write_text(original, encoding="utf-8")
    _write_db(target / "sqlite" / "state_5.sqlite", [("target", "Target")])
    source_db = source / "sqlite" / "state_5.sqlite"
    source_db.parent.mkdir(parents=True)
    with sqlite3.connect(source_db) as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title BLOB)")

    with pytest.raises(RuntimeError, match="schema mismatch"):
        recover_codex_home(source=source, target=target)

    assert (target / "config.toml").read_text(encoding="utf-8") == original
    with sqlite3.connect(target / "sqlite" / "state_5.sqlite") as connection:
        assert connection.execute("SELECT id, title FROM threads").fetchall() == [
            ("target", "Target")
        ]


def test_recover_codex_cli_previews_then_merges(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / ".codex"
    source = tmp_path / "headroom-codex-home-broken"
    target.mkdir(parents=True)
    source.mkdir()
    (source / "history.jsonl").write_text('{"session_id":"new"}\n', encoding="utf-8")
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["recover", "codex", "--source", str(source), "--target", str(target), "--yes"],
        env={"HOME": str(home)},
    )

    assert result.exit_code == 0, result.output
    assert "Recovery complete" in result.output
    assert json.loads((target / "history.jsonl").read_text(encoding="utf-8"))["session_id"] == (
        "new"
    )
