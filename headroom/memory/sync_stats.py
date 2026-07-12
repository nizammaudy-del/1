"""Read-only reader for persisted memory-sync state.

Surfaces the per-adapter sync counters recorded by :mod:`headroom.memory.sync`
(``sync_state.json`` in the workspace) so the dashboard can show what each
agent last imported/exported. This never mutates state; on a missing or
corrupt state file it returns an empty summary.

Note: ``skipped_dedup`` is a runtime field of ``SyncResult`` but is NOT
persisted to the sync-state file (only ``last_imported``/``last_exported`` are
written per adapter), so it defaults to 0 here.
"""

from __future__ import annotations

from .. import paths as _paths
from .sync import _load_sync_state


def _safe_int(value: object) -> int:
    """Coerce a persisted count to int, defaulting to 0 on any bad input."""
    if isinstance(value, bool):  # bool is an int subclass; not a real count
        return 0
    if not isinstance(value, int | float | str):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def get_sync_stats() -> dict:
    """Return a per-agent summary of the persisted memory-sync state.

    ``sync.py`` persists exactly one timestamp per adapter (``last_sync``,
    the moment of the most recent sync run) plus item counts for that run
    (``last_imported``/``last_exported``) — there are no separate
    import/export timestamps to report. It also never persists a dedup
    count, so ``dedup_rate`` is always ``None``: there's no real data to
    compute it from, and returning a fabricated 0% would be misleading.

    Shape::

        {"agents": [{"name", "last_sync", "imported_count", "exported_count"}...],
         "dedup_rate": None}

    On a missing or unreadable state file returns
    ``{"agents": [], "dedup_rate": None}``.
    """
    empty: dict = {"agents": [], "dedup_rate": None}
    try:
        state = _load_sync_state(_paths.sync_state_path())
    except Exception:
        return empty
    if not isinstance(state, dict) or not state:
        return empty

    agents: list[dict] = []
    for adapter_key, entry in state.items():
        if not isinstance(entry, dict):
            continue
        # adapter_key is "<agent_name>:<user_id>"; keep the agent name.
        name = adapter_key.split(":", 1)[0]
        agents.append(
            {
                "name": name,
                "last_sync": entry.get("last_sync"),
                "imported_count": _safe_int(entry.get("last_imported")),
                "exported_count": _safe_int(entry.get("last_exported")),
            }
        )

    return {"agents": agents, "dedup_rate": None}
