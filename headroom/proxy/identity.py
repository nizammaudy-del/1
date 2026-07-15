"""Memory partition identity resolution (WEB-02).

``x-headroom-user-id`` is a partition *hint*, not an authenticated identity. In
the OSS proxy it is honored only for loopback callers (the single-user local
model) or hosts listed in ``HEADROOM_USER_ID_ALLOWLIST``. For other callers the
identity is bound to the proxy token (or the server's OS user) so a network
client cannot select another user's memory.

Multi-tenant deployments (e.g. headroom-managed) replace the default with an
authenticated resolver via :func:`set_identity_resolver`, typically from a
``headroom.proxy_extension`` install hook. This keeps real per-tenant identity —
the enterprise differentiator — out of the OSS proxy while giving it a clean,
secure single-user default.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Protocol

from headroom.proxy.loopback_guard import is_loopback_host

USER_ID_HEADER = "x-headroom-user-id"
ALLOWLIST_ENV = "HEADROOM_USER_ID_ALLOWLIST"


class IdentityResolver(Protocol):
    def __call__(self, request: Any, *, default: str) -> str: ...


_resolver: IdentityResolver | None = None


def set_identity_resolver(resolver: IdentityResolver | None) -> None:
    """Install (or clear) a custom identity resolver — the enterprise hook."""
    global _resolver
    _resolver = resolver


def _default_os_user() -> str:
    return os.environ.get("USER", os.environ.get("USERNAME", "default"))


def _client_host(request: Any) -> str | None:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return host if isinstance(host, str) else None


def _allowlist() -> set[str] | None:
    raw = os.environ.get(ALLOWLIST_ENV)
    if not raw or not raw.strip():
        return None
    return {value.strip() for value in raw.split(",") if value.strip()}


def _token_identity() -> str | None:
    token = os.environ.get("HEADROOM_PROXY_TOKEN")
    if not token:
        return None
    return "tok_" + hashlib.sha256(token.encode()).hexdigest()[:16]


def resolve_memory_identity(request: Any, *, default: str | None = None) -> str:
    """Resolve the memory partition id for a request.

    A registered custom resolver wins. Otherwise the header is honored only for
    loopback or allowlisted callers; every other caller is bound to the proxy
    token (or the OS user), so it can never address another user's partition.
    """
    fallback = default if default is not None else _default_os_user()

    if _resolver is not None:
        return _resolver(request, default=fallback)

    header_value: str | None
    try:
        header_value = request.headers.get(USER_ID_HEADER)
    except Exception:
        header_value = None
    if header_value is not None:
        header_value = header_value.strip() or None

    host = _client_host(request)
    is_local = host is None or is_loopback_host(host)

    if header_value is not None:
        if is_local:
            return header_value
        allow = _allowlist()
        if allow is not None and header_value in allow:
            return header_value
        # Non-loopback caller supplied an id it isn't allowed to select — ignore
        # it and fall through to its own authenticated scope.

    if not is_local:
        token_id = _token_identity()
        if token_id is not None:
            return token_id
    return fallback
