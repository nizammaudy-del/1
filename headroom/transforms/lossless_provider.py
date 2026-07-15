"""Pluggable lossless-compaction providers.

STAGE 0 of the ContentRouter (``_lossless_first``) is Headroom's single
byte/data-lossless pass. This module lets an external layer (an Enterprise
extension, a third-party package) contribute *additional* lossless folds there —
without ever being trusted for correctness.

The contract that makes external code safe to plug in:

    A provider only PROPOSES a compaction plus a way to recover the original.
    The open core VERIFIES the recovery itself (byte-exact, or canonical-JSON
    value equality — both computed here, un-cheatable) and DISCARDS anything that
    does not reproduce the input. Losslessness is enforced by Headroom, not by
    the provider.

Providers are opt-in: nothing is registered unless an extension calls
``register_lossless_provider`` (typically from its ``install(app, config)``), so
default OSS behaviour is unchanged. A provider that raises, returns ``None``, or
fails verification is simply skipped.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)

#: Entry-point group extensions may also register under (convenience; the
#: primary path is ``register_lossless_provider`` from an extension's install()).
ENTRY_POINT_GROUP = "headroom.lossless_provider"


@dataclass
class LosslessCtx:
    """What ``_lossless_first`` can tell a provider about the content in hand.

    Deliberately minimal + additive. ``content_type`` is the router's detected
    strategy value (e.g. ``"search"``, ``"log"``); ``tool_name``/``command`` are
    populated when a call site has them (empty at the current STAGE-0 call site,
    so providers must not rely on them for safety — gate on content)."""

    content_type: str = ""
    tool_name: str = ""
    command: str = ""


@dataclass
class Compaction:
    """A provider's proposed lossless compaction.

    ``recover()`` must reproduce the original content; the core checks it under
    ``equivalence`` before accepting. ``label`` is the strategy name recorded to
    the compression observer (so the saving is attributable in ``/stats``)."""

    text: str
    recover: Callable[[], str]
    equivalence: str = "byte"  # "byte" (exact) | "json" (canonical value equality)
    label: str = "external"


@runtime_checkable
class LosslessProvider(Protocol):
    name: str

    def propose(self, content: str, ctx: LosslessCtx) -> Compaction | None:
        """Return a Compaction, or None to decline this content."""


_providers: list[LosslessProvider] = []


def register_lossless_provider(provider: LosslessProvider) -> None:
    """Register a provider. Called by an extension's ``install(app, config)``."""
    _providers.append(provider)
    log.info("registered lossless provider: %s", getattr(provider, "name", type(provider).__name__))


def registered_lossless_providers() -> list[LosslessProvider]:
    return list(_providers)


def clear_lossless_providers() -> None:
    """Test/reset helper."""
    _providers.clear()


def _verify(content: str, c: Compaction) -> bool:
    """The safety gate. Re-derive the original and confirm equivalence — computed
    here so a provider cannot assert losslessness it doesn't have."""
    try:
        recovered = c.recover()
    except Exception:
        log.debug("lossless provider recover() raised", exc_info=True)
        return False
    if c.equivalence == "json":
        try:
            return _json_values_equal(json.loads(recovered), json.loads(content))
        except (ValueError, TypeError):
            return False
    return recovered == content  # byte-exact


def _json_values_equal(left: object, right: object) -> bool:
    """JSON value equality with JSON type identity preserved.

    Python's plain equality treats ``True == 1`` because ``bool`` subclasses
    ``int``. That is not JSON value equality: booleans and numbers are distinct
    JSON types, so a verified compaction must not be allowed to swap them.
    """
    if left is None or right is None:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, int | float) or isinstance(right, int | float):
        return (
            isinstance(left, int | float)
            and not isinstance(left, bool)
            and isinstance(right, int | float)
            and not isinstance(right, bool)
            and left == right
        )
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(_json_values_equal(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict) or isinstance(right, dict):
        return (
            isinstance(left, dict)
            and isinstance(right, dict)
            and left.keys() == right.keys()
            and all(_json_values_equal(left[key], right[key]) for key in left)
        )
    return left == right


def best_provider_fold(content: str, ctx: LosslessCtx) -> tuple[str, str] | None:
    """Run every registered provider, VERIFY each proposal, and return the
    ``(text, label)`` that shrank the most and passed verification — or ``None``
    when no provider is registered / none verified + shrank. Never raises."""
    if not _providers:
        return None
    best_text, best_label = content, None
    for provider in _providers:
        try:
            proposal = provider.propose(content, ctx)
        except Exception:
            log.debug(
                "lossless provider %s raised", getattr(provider, "name", provider), exc_info=True
            )
            continue
        if proposal is None or not proposal.text or len(proposal.text) >= len(best_text):
            continue
        if _verify(content, proposal):
            best_text, best_label = proposal.text, proposal.label
        else:
            log.debug(
                "lossless provider %s failed verification — discarded",
                getattr(provider, "name", provider),
            )
    return (best_text, best_label) if best_label is not None else None
