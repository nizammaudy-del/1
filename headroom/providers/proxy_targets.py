"""Provider upstream target resolution for proxy routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from headroom.providers.codex import resolve_codex_routing
from headroom.providers.codex.endpoints import CHATGPT_BACKEND_API_URL
from headroom.providers.vertex import vertex_target_for_location as _vertex_target_for_location
from headroom.proxy.agy_terminator import DEFAULT_ALLOWLIST

LEGACY_API_TARGET_ATTRS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_URL",
    "openai": "OPENAI_API_URL",
    "gemini": "GEMINI_API_URL",
    "cloudcode": "CLOUDCODE_API_URL",
    "vertex": "VERTEX_API_URL",
}


def api_target(proxy: Any, provider_name: str) -> str:
    """Return the proxy target for a provider, honoring legacy proxy attributes."""
    legacy_attr = LEGACY_API_TARGET_ATTRS[provider_name]
    return cast(str, getattr(proxy, legacy_attr, proxy.provider_runtime.api_target(provider_name)))


def vertex_target_for_location(proxy: Any, location: str) -> str:
    """Resolve the Vertex upstream host for a request, region-aware."""
    return _vertex_target_for_location(api_target(proxy, "vertex"), location)


def cloudcode_host_base(host: str) -> str | None:
    """Passthrough base for an allowlisted Cloud Code host, else ``None``.

    agy (Google Antigravity CLI) reaches the proxy via TLS-MITM that terminates
    the WHOLE connection to the Cloud Code host it addressed, so control-plane
    calls land on the catch-all rather than a recognized route. Those paths
    exist only on the Cloud Code host itself; forward them back to it.
    Membership in ``DEFAULT_ALLOWLIST`` — not a loose suffix match — is the trust
    boundary: a forged Host such as ``evilcloudcode-pa.googleapis.com`` returns
    ``None`` (closing the SSRF) so the caller falls back to the configured
    default.
    """
    return f"https://{host}" if host in DEFAULT_ALLOWLIST else None


def select_passthrough_base_url(proxy: Any, headers: Mapping[str, str]) -> str:
    """Resolve the upstream base URL for catch-all proxy passthrough requests."""
    if base := cloudcode_host_base(headers.get("host", "")):
        return base
    routing = resolve_codex_routing(headers)
    if routing.is_chatgpt_auth:
        return CHATGPT_BACKEND_API_URL
    if headers.get("x-goog-api-key"):
        return api_target(proxy, "gemini")
    if headers.get("api-key"):
        azure_base = headers.get("x-headroom-base-url", "")
        if azure_base:
            return azure_base.rstrip("/")
    provider_name = proxy.provider_runtime.model_metadata_provider(headers)
    return api_target(proxy, provider_name)
