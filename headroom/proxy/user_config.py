"""User-editable dashboard configuration (persistent).

Backs the dashboard Settings menu. Holds two kinds of overrides that a user
can edit from the UI and that the running proxy reads without a restart:

- ``pricing`` — per-model token price overrides (USD per 1M tokens). When a
  model is not listed here, pricing falls back to LiteLLM's database exactly
  as before, so an empty/unset config changes no behavior.
- ``settings`` — safe-to-edit defaults (dashboard poll intervals, budget,
  savings profile, target ratio).

Persisted as JSON under :func:`headroom.paths.config_dir` /
``dashboard_config.json``. The in-memory copy is the source of truth for the
process once loaded, and it is refreshed on write so ``POST /config`` takes
effect on the next request.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, TypeGuard, cast

from headroom import paths

logger = logging.getLogger("headroom.proxy")

CONFIG_FILENAME = "dashboard_config.json"

# Editable per-model pricing fields (USD per 1 million tokens). Only
# ``input_per_1m`` is required for an override to take effect; the cache
# fields default to the input rate when omitted, matching LiteLLM's fallback.
PRICING_FIELDS: frozenset[str] = frozenset(
    {
        "input_per_1m",
        "output_per_1m",
        "cache_read_per_1m",
        "cache_write_per_1m",
    }
)

# Editable non-pricing settings and their defaults. Defaults mirror current
# behavior so an unset config is a no-op.
SETTING_DEFAULTS: dict[str, Any] = {
    "dashboard_stats_poll_ms": 5000,
    "dashboard_history_poll_ms": 30000,
    "dashboard_feed_poll_ms": 5000,
    "budget_limit_usd": None,
    "budget_period": "daily",
    "savings_profile": None,
    "target_ratio": None,
}

# Settings that only take effect when the proxy (re)starts, so the UI can warn.
RESTART_REQUIRED_SETTINGS: frozenset[str] = frozenset(
    {"budget_limit_usd", "budget_period", "savings_profile", "target_ratio"}
)

_BUDGET_PERIODS: frozenset[str] = frozenset({"hourly", "daily", "monthly"})

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _config_path() -> Any:
    return paths.config_dir() / CONFIG_FILENAME


def _empty() -> dict[str, Any]:
    return {"pricing": {}, "settings": {}}


def _load_locked() -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    path = _config_path()
    data = _empty()
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                pricing = loaded.get("pricing")
                settings = loaded.get("settings")
                data["pricing"] = pricing if isinstance(pricing, dict) else {}
                data["settings"] = settings if isinstance(settings, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read %s: %s — using empty config", path, exc)
        data = _empty()
    _cache = data
    return _cache


def load() -> dict[str, Any]:
    """Return the raw stored config ({"pricing": ..., "settings": ...})."""
    with _lock:
        # json round-trip deep-copies so callers can't mutate the cached dict;
        # cast() only tells mypy what we already know the JSON shape is.
        return cast("dict[str, Any]", json.loads(json.dumps(_load_locked())))


def get_settings() -> dict[str, Any]:
    """Return effective settings (defaults merged with stored overrides)."""
    with _lock:
        stored = _load_locked()["settings"]
    effective = dict(SETTING_DEFAULTS)
    for key, value in stored.items():
        if key in SETTING_DEFAULTS:
            effective[key] = value
    return effective


def get_pricing_overrides() -> dict[str, dict[str, float]]:
    """Return the raw per-model pricing override map."""
    with _lock:
        return cast(
            "dict[str, dict[str, float]]", json.loads(json.dumps(_load_locked()["pricing"]))
        )


def pricing_override_per_token(model: str) -> dict[str, float] | None:
    """Return per-token price overrides for ``model``, or None if unset.

    Keys: ``input``, ``output``, ``cache_read``, ``cache_write`` (USD/token).
    Cache rates default to the input rate when not explicitly overridden,
    mirroring LiteLLM's own fallback. Returns None (defer to LiteLLM) unless a
    usable ``input_per_1m`` is configured for the model.
    """

    def _is_number(value: object) -> TypeGuard[int | float]:
        # bool is a subclass of int in Python; reject it so a hand-edited
        # `true`/`false` in the JSON can't silently become a numeric rate.
        return isinstance(value, int | float) and not isinstance(value, bool)

    with _lock:
        overrides = _load_locked()["pricing"]
        entry = overrides.get(model)
    if not isinstance(entry, dict):
        return None
    input_per_1m = entry.get("input_per_1m")
    if not _is_number(input_per_1m):
        return None
    if input_per_1m <= 0:
        return None
    input_pt = float(input_per_1m) / 1_000_000
    output_per_1m = entry.get("output_per_1m")
    output_pt = float(output_per_1m) / 1_000_000 if _is_number(output_per_1m) else input_pt
    cache_read = entry.get("cache_read_per_1m")
    cache_read_pt = float(cache_read) / 1_000_000 if _is_number(cache_read) else input_pt
    cache_write = entry.get("cache_write_per_1m")
    cache_write_pt = float(cache_write) / 1_000_000 if _is_number(cache_write) else input_pt
    return {
        "input": input_pt,
        "output": output_pt,
        "cache_read": cache_read_pt,
        "cache_write": cache_write_pt,
    }


def _validate_pricing(pricing: Any) -> dict[str, dict[str, float]]:
    if not isinstance(pricing, dict):
        raise ValueError("'pricing' must be an object mapping model -> prices")
    clean: dict[str, dict[str, float]] = {}
    for model, entry in pricing.items():
        if not isinstance(model, str) or not model.strip():
            raise ValueError("pricing model keys must be non-empty strings")
        if not isinstance(entry, dict):
            raise ValueError(f"pricing['{model}'] must be an object of price fields")
        unknown = set(entry) - PRICING_FIELDS
        if unknown:
            raise ValueError(
                f"unknown pricing field(s) for '{model}': {', '.join(sorted(unknown))}"
            )
        clean_entry: dict[str, float] = {}
        for field, value in entry.items():
            if value is None:
                continue
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise ValueError(f"pricing['{model}'].{field} must be a number")
            if field == "input_per_1m":
                # pricing_override_per_token() treats input_per_1m <= 0 as
                # "no override" — accepting 0 here would silently save a
                # value that then never takes effect.
                if value <= 0:
                    raise ValueError(f"pricing['{model}'].input_per_1m must be > 0")
            elif value < 0:
                raise ValueError(f"pricing['{model}'].{field} must be >= 0")
            clean_entry[field] = float(value)
        if clean_entry:
            clean[model] = clean_entry
    return clean


def _validate_settings(settings: Any) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("'settings' must be an object")
    unknown = set(settings) - set(SETTING_DEFAULTS)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    clean: dict[str, Any] = {}
    for key, value in settings.items():
        if key.endswith("_poll_ms"):
            if not isinstance(value, int) or isinstance(value, bool) or value < 250:
                raise ValueError(f"{key} must be an integer >= 250 (milliseconds)")
            clean[key] = int(value)
        elif key == "budget_limit_usd":
            if value is None:
                clean[key] = None
            elif isinstance(value, int | float) and not isinstance(value, bool) and value >= 0:
                # The Settings UI says "leave 0/blank for no limit" — normalize
                # 0 to None so it actually means unlimited, not "budget is $0"
                # (which would block all spend).
                clean[key] = float(value) if value > 0 else None
            else:
                raise ValueError("budget_limit_usd must be null or a number >= 0")
        elif key == "budget_period":
            if value not in _BUDGET_PERIODS:
                raise ValueError(f"budget_period must be one of {sorted(_BUDGET_PERIODS)}")
            clean[key] = value
        elif key == "savings_profile":
            if value is not None and not isinstance(value, str):
                raise ValueError("savings_profile must be null or a string")
            clean[key] = value or None
        elif key == "target_ratio":
            if value is None:
                clean[key] = None
            elif isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= 1:
                clean[key] = float(value)
            else:
                raise ValueError("target_ratio must be null or a number between 0 and 1")
    return clean


def update(body: dict[str, Any]) -> dict[str, Any]:
    """Validate, merge, and persist a config update.

    ``body`` may contain ``pricing`` and/or ``settings``. Provided sections
    replace the stored section wholesale (so the UI sends the full current
    state). Raises ``ValueError`` on any invalid or unknown input; nothing is
    written when validation fails.
    """
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    unknown_sections = set(body) - {"pricing", "settings"}
    if unknown_sections:
        raise ValueError(f"unknown section(s): {', '.join(sorted(unknown_sections))}")

    global _cache
    with _lock:
        current = cast("dict[str, Any]", json.loads(json.dumps(_load_locked())))
        if "pricing" in body:
            current["pricing"] = _validate_pricing(body["pricing"])
        if "settings" in body:
            current["settings"] = _validate_settings(body["settings"])

        path = _config_path()
        try:
            paths.ensure_config_dir()
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            # Message stays generic: /config's 400 response echoes this ValueError
            # straight to the caller, and a raw OSError includes the config
            # file's local filesystem path.
            logger.warning("Failed to persist dashboard config: %s", exc)
            raise ValueError("failed to persist config") from exc
        _cache = current
    return current


def config_response() -> dict[str, Any]:
    """Full payload for ``GET /config`` — effective values plus metadata.

    This route is intentionally unguarded (reachable without the loopback
    check), so it must never include local filesystem details like the
    config file's path.
    """
    return {
        "pricing": get_pricing_overrides(),
        "settings": get_settings(),
        "setting_defaults": dict(SETTING_DEFAULTS),
        "pricing_fields": sorted(PRICING_FIELDS),
        "restart_required_settings": sorted(RESTART_REQUIRED_SETTINGS),
    }
