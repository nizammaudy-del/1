"""Transactional recovery of Codex state left in a temporary Headroom home."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

_TEMP_HOME_PREFIX = "headroom-codex-home-"
_RECOVERY_DIR = ".headroom-codex-recovery"
_SQLITE_SUFFIXES = {".sqlite", ".db"}
_RUNTIME_NAMES = {".DS_Store"}
_RUNTIME_SUFFIXES = {".lock", ".sock", ".socket", "-shm", "-wal", "-journal"}


@dataclass
class RecoveryReport:
    source: Path
    target: Path
    backup_dir: Path
    copied: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    skipped_runtime: list[str] = field(default_factory=list)


def discover_dangling_homes(temp_root: Path | None = None) -> list[Path]:
    """Find non-empty Headroom temporary Codex homes, newest first."""
    root = temp_root or Path(tempfile.gettempdir())
    candidates: list[Path] = []
    for path in root.glob(f"{_TEMP_HOME_PREFIX}*"):
        if path.is_dir() and any(path.iterdir()):
            candidates.append(path)
    return sorted(candidates, key=lambda path: path.stat().st_mtime_ns, reverse=True)


def home_fingerprint(home: Path) -> str:
    """Return a content-independent fingerprint used to detect concurrent writes."""
    digest = hashlib.sha256()
    if not home.exists():
        return digest.hexdigest()
    for path in sorted(home.rglob("*")):
        relative = path.relative_to(home)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        digest.update(os.fsencode(str(relative)))
        digest.update(str(metadata.st_mode).encode())
        digest.update(str(metadata.st_size).encode())
        digest.update(str(metadata.st_mtime_ns).encode())
        if stat.S_ISLNK(metadata.st_mode):
            digest.update(os.fsencode(os.readlink(path)))
    return digest.hexdigest()


def _is_runtime_artifact(path: Path) -> bool:
    name = path.name
    return name in _RUNTIME_NAMES or any(name.endswith(suffix) for suffix in _RUNTIME_SUFFIXES)


def _secure_tree(path: Path) -> None:
    if not path.exists():
        return
    path.chmod(0o700)
    for entry in path.rglob("*"):
        if entry.is_symlink():
            continue
        entry.chmod(0o700 if entry.is_dir() else 0o600)


def _copy_home(source: Path, destination: Path, skipped: list[str]) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o700)
    for entry in source.rglob("*"):
        relative = entry.relative_to(source)
        output = destination / relative
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISSOCK(metadata.st_mode):
            skipped.append(str(relative))
            continue
        if entry.is_symlink():
            output.parent.mkdir(parents=True, exist_ok=True)
            output.symlink_to(os.readlink(entry))
        elif entry.is_dir():
            output.mkdir(parents=True, exist_ok=True)
        elif entry.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, output, follow_symlinks=False)
    _secure_tree(destination)


def _new_backup_dir(target: Path) -> Path:
    root = target.parent / _RECOVERY_DIR
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}-{os.getpid()}"
    for counter in range(1000):
        suffix = "" if counter == 0 else f"-{counter}"
        candidate = root / f"{stem}{suffix}"
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("could not allocate a Codex recovery backup directory")


def _clean_managed_codex_config(document: Any) -> None:
    if document.get("model_provider") == "headroom":
        del document["model_provider"]
    providers = document.get("model_providers")
    if providers is not None and "headroom" in providers:
        del providers["headroom"]
        if not providers:
            del document["model_providers"]


def _merge_toml_table(target: Any, source: Any, *, source_wins: bool) -> None:
    for key, source_value in source.items():
        if key not in target:
            target[key] = source_value
            continue
        target_value = target[key]
        if hasattr(target_value, "items") and hasattr(source_value, "items"):
            _merge_toml_table(target_value, source_value, source_wins=source_wins)
        elif source_wins:
            target[key] = source_value


def _merge_config(source: Path, target: Path) -> None:
    source_document = tomlkit.parse(source.read_text(encoding="utf-8"))
    _clean_managed_codex_config(source_document)
    if target.exists():
        target_document = tomlkit.parse(target.read_text(encoding="utf-8"))
        _clean_managed_codex_config(target_document)
        source_wins = source.stat().st_mtime_ns > target.stat().st_mtime_ns
        _merge_toml_table(target_document, source_document, source_wins=source_wins)
    else:
        target_document = source_document
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(tomlkit.dumps(target_document), encoding="utf-8")


def _read_jsonl(path: Path, quarantine: Path, report: RecoveryReport) -> list[str]:
    lines: list[str] = []
    malformed = False
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            malformed = True
            continue
        lines.append(raw_line)
    if malformed:
        destination = quarantine / path.name
        counter = 1
        while destination.exists():
            destination = quarantine / f"{path.stem}-{counter}{path.suffix}"
            counter += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        report.quarantined.append(str(path))
    return lines


def _merge_jsonl(source: Path, target: Path, quarantine: Path, report: RecoveryReport) -> None:
    existing = _read_jsonl(target, quarantine, report) if target.exists() else []
    incoming = _read_jsonl(source, quarantine, report)
    merged = list(existing)
    seen = set(existing)
    merged.extend(line for line in incoming if line not in seen and not seen.add(line))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"{line}\n" for line in merged), encoding="utf-8")


def _merge_rollout(source: Path, target: Path, quarantine: Path, report: RecoveryReport) -> None:
    incoming = _read_jsonl(source, quarantine, report)
    if target.exists() and source.stat().st_mtime_ns <= target.stat().st_mtime_ns:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(f"{line}\n" for line in incoming), encoding="utf-8")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _database_schema(connection: sqlite3.Connection, schema: str) -> dict[str, str]:
    rows = connection.execute(
        f"SELECT name, sql FROM {_quote(schema)}.sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return dict(rows)


def _table_columns(
    connection: sqlite3.Connection, schema: str, table: str
) -> list[tuple[Any, ...]]:
    return connection.execute(f"PRAGMA {_quote(schema)}.table_info({_quote(table)})").fetchall()


def _merge_database(source: Path, target: Path) -> None:
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        return
    source_is_newer = source.stat().st_mtime_ns > target.stat().st_mtime_ns
    with sqlite3.connect(target) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("ATTACH DATABASE ? AS incoming", (str(source),))
        target_schema = _database_schema(connection, "main")
        source_schema = _database_schema(connection, "incoming")
        if target_schema != source_schema:
            raise RuntimeError(f"SQLite schema mismatch for {target.name}")
        if "_sqlx_migrations" in target_schema:
            migration_columns = [
                str(column[1]) for column in _table_columns(connection, "main", "_sqlx_migrations")
            ]
            if "version" in migration_columns and "checksum" in migration_columns:
                target_migrations = dict(
                    connection.execute(
                        "SELECT version, checksum FROM main._sqlx_migrations"
                    ).fetchall()
                )
                source_migrations = dict(
                    connection.execute(
                        "SELECT version, checksum FROM incoming._sqlx_migrations"
                    ).fetchall()
                )
                for version in target_migrations.keys() & source_migrations.keys():
                    if target_migrations[version] != source_migrations[version]:
                        raise RuntimeError(
                            f"SQLite migration mismatch for {target.name} at version {version}"
                        )
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in target_schema:
                columns = _table_columns(connection, "main", table)
                source_columns = _table_columns(connection, "incoming", table)
                if columns != source_columns:
                    raise RuntimeError(f"SQLite schema mismatch for {target.name}:{table}")
                column_names = [str(column[1]) for column in columns]
                primary_key = [
                    str(column[1])
                    for column in sorted(columns, key=lambda row: row[5])
                    if column[5]
                ]
                quoted_columns = ", ".join(_quote(name) for name in column_names)
                rows = connection.execute(
                    f"SELECT {quoted_columns} FROM incoming.{_quote(table)}"
                ).fetchall()
                placeholders = ", ".join("?" for _ in column_names)
                verb = (
                    "INSERT OR REPLACE" if primary_key and source_is_newer else "INSERT OR IGNORE"
                )
                connection.executemany(
                    f"{verb} INTO {_quote(table)} ({quoted_columns}) VALUES ({placeholders})",
                    rows,
                )
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise RuntimeError(f"SQLite integrity check failed for {target.name}")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"SQLite foreign key check failed for {target.name}")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("DETACH DATABASE incoming")


def _merge_pinned_home(pinned: Path, target: Path, report: RecoveryReport) -> None:
    quarantine = report.backup_dir / "quarantine"
    for source in sorted(pinned.rglob("*")):
        relative = source.relative_to(pinned)
        destination = target / relative
        if source.is_dir() or source.is_symlink():
            if source.is_symlink() and not destination.exists() and not destination.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.symlink_to(os.readlink(source))
                report.copied.append(str(relative))
            continue
        if _is_runtime_artifact(source):
            report.skipped_runtime.append(str(relative))
            continue
        if source.name == "config.toml":
            _merge_config(source, destination)
            report.merged.append(str(relative))
        elif source.suffix == ".jsonl" and relative.parts[0] in {
            "sessions",
            "archived_sessions",
        }:
            _merge_rollout(source, destination, quarantine, report)
            report.merged.append(str(relative))
        elif source.suffix == ".jsonl":
            _merge_jsonl(source, destination, quarantine, report)
            report.merged.append(str(relative))
        elif source.suffix in _SQLITE_SUFFIXES:
            _merge_database(source, destination)
            report.merged.append(str(relative))
        elif not destination.exists() or source.stat().st_mtime_ns > destination.stat().st_mtime_ns:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            report.copied.append(str(relative))


def _restore_target(target: Path, target_backup: Path, target_existed: bool) -> None:
    if target.exists():
        shutil.rmtree(target)
    if target_existed:
        shutil.copytree(target_backup, target, symlinks=True)


def _capture_modes(home: Path) -> dict[str, int]:
    modes = {".": stat.S_IMODE(home.stat().st_mode)}
    for entry in home.rglob("*"):
        if not entry.is_symlink():
            modes[str(entry.relative_to(home))] = stat.S_IMODE(entry.stat().st_mode)
    return modes


def _restore_modes(home: Path, modes: dict[str, int]) -> None:
    for relative, mode in modes.items():
        path = home if relative == "." else home / relative
        if path.exists() and not path.is_symlink():
            path.chmod(mode)


def recover_codex_home(*, source: Path, target: Path) -> RecoveryReport:
    """Merge one quiet temporary Codex home into the active home transactionally."""
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_dir() or source == target:
        raise ValueError("source must be an existing Codex home different from target")
    if source in target.parents or target in source.parents or target == Path(target.anchor):
        raise ValueError("source and target Codex homes must not overlap")
    before = home_fingerprint(source)
    backup_dir = _new_backup_dir(target)
    report = RecoveryReport(source=source, target=target, backup_dir=backup_dir)
    pinned = backup_dir / "source-pinned"
    target_backup = backup_dir / "target-before"
    _copy_home(source, pinned, report.skipped_runtime)
    if home_fingerprint(source) != before:
        raise RuntimeError("source Codex home changed while it was being pinned")
    target_existed = target.exists()
    target_modes: dict[str, int] = {}
    if target_existed:
        target_modes = _capture_modes(target)
        target_fingerprint = home_fingerprint(target)
        _copy_home(target, target_backup, report.skipped_runtime)
        if home_fingerprint(target) != target_fingerprint:
            raise RuntimeError("target Codex home changed while it was being backed up")
    else:
        target.mkdir(mode=0o700, parents=True)
    try:
        _merge_pinned_home(pinned, target, report)
    except Exception:
        _restore_target(target, target_backup, target_existed)
        if target_existed:
            _restore_modes(target, target_modes)
        raise
    manifest = asdict(report)
    manifest.update(source=str(source), target=str(target), backup_dir=str(backup_dir))
    manifest_file = backup_dir / "manifest.json"
    manifest_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_file.chmod(0o600)
    return report
