"""Durable append-only log of ``headroom learn`` runs.

Each run appends one JSON line to ``<workspace>/learn_history.jsonl`` so the
dashboard can show a history of learning passes (when, which agent/project,
how many recommendations, whether they were applied). All I/O is best-effort:
recording never raises and reading tolerates a missing or corrupt file.
"""

from __future__ import annotations

import json
import logging
from collections import deque

from ..paths import ensure_workspace_dir, workspace_dir

logger = logging.getLogger("headroom.cli.learn_history")

_HISTORY_FILE = "learn_history.jsonl"


def record_learn_run(entry: dict) -> None:
    """Append one JSON line describing a learn run. Never raises."""
    try:
        ws = ensure_workspace_dir()
        path = ws / _HISTORY_FILE
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as exc:  # pragma: no cover - best-effort telemetry
        logger.debug("Could not record learn run: %s", exc)


def read_learn_history(limit: int = 50) -> list[dict]:
    """Return the last ``limit`` recorded runs. Tolerates missing/corrupt file.

    The dashboard polls ``/learn/history`` frequently, so this streams the
    file and keeps only the last ``limit`` entries in a bounded deque instead
    of holding the whole (potentially large) file in memory.
    """
    path = workspace_dir() / _HISTORY_FILE
    if not path.exists():
        return []
    entries: deque[dict] = deque(maxlen=max(1, limit))
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
    except OSError:
        return []
    return list(entries)
