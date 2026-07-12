"""Operator configuration policy for proxy tool injection."""

from __future__ import annotations

import os
from typing import Literal, cast

TOOL_INJECTION_STICKY_ENV = "HEADROOM_TOOL_INJECTION_STICKY"
ToolInjectionStickyMode = Literal["enabled", "disabled"]
TOOL_INJECTION_STICKY_DEFAULT: ToolInjectionStickyMode = "enabled"

TOOL_TRACKER_MAX_SESSIONS_ENV = "HEADROOM_TOOL_TRACKER_MAX_SESSIONS"
TOOL_TRACKER_MAX_SESSIONS_DEFAULT = 1000


def get_tool_injection_sticky_mode() -> ToolInjectionStickyMode:
    """Return the active memory-tool stickiness mode."""

    raw = os.environ.get(TOOL_INJECTION_STICKY_ENV, "").strip().lower()
    if not raw:
        return TOOL_INJECTION_STICKY_DEFAULT
    if raw in ("enabled", "disabled"):
        return cast(ToolInjectionStickyMode, raw)
    raise ValueError(
        f"Invalid {TOOL_INJECTION_STICKY_ENV}={raw!r}; expected 'enabled' or 'disabled'"
    )


def get_tool_tracker_max_sessions() -> int:
    """Return the LRU bound for memory tool session tracking."""

    raw = os.environ.get(TOOL_TRACKER_MAX_SESSIONS_ENV, "").strip()
    if not raw:
        return TOOL_TRACKER_MAX_SESSIONS_DEFAULT
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid {TOOL_TRACKER_MAX_SESSIONS_ENV}={raw!r}; expected positive int"
        ) from exc
    if value <= 0:
        raise ValueError(f"Invalid {TOOL_TRACKER_MAX_SESSIONS_ENV}={raw!r}; expected positive int")
    return value
