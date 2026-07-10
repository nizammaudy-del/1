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


def get_sync_stats() -> dict:
    """Return a per-agent summary of the persisted memory-sync state.

    Shape::

        {"agents": [{"name", "last_imported", "last_exported",
                     "imported_count", "exported_count", "skipped_dedup"}...],
         "dedup_rate": <float|None>}

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
    total_imported = 0
    total_dedup = 0
    for adapter_key, entry in state.items():
        if not isinstance(entry, dict):
            continue
        # adapter_key is "<agent_name>:<user_id>"; keep the agent name.
        name = adapter_key.split(":", 1)[0]
        imported = int(entry.get("last_imported") or 0)
        exported = int(entry.get("last_exported") or 0)
        # skipped_dedup is not persisted (see module docstring); default 0.
        skipped_dedup = int(entry.get("skipped_dedup") or 0)
        agents.append(
            {
                "name": name,
                "last_imported": entry.get("last_sync"),
                "last_exported": entry.get("last_sync"),
                "imported_count": imported,
                "exported_count": exported,
                "skipped_dedup": skipped_dedup,
            }
        )
        total_imported += imported
        total_dedup += skipped_dedup

    # dedup_rate = deduped / (deduped + imported); None when nothing to divide.
    denom = total_imported + total_dedup
    dedup_rate = (total_dedup / denom) if denom > 0 else None

    return {"agents": agents, "dedup_rate": dedup_rate}
