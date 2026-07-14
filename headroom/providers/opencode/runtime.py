"""Runtime helpers for OpenCode integrations."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .config import install_headroom_opencode_plugin_files


def proxy_base_url(port: int) -> str:
    """Return the local proxy base URL used by OpenCode integrations."""
    return f"http://127.0.0.1:{port}/v1"


def proxy_server_url(port: int) -> str:
    """Return the local Headroom proxy origin used by the OpenCode plugin."""
    return f"http://127.0.0.1:{port}"


def build_opencode_config_content(
    *,
    port: int,
    include_mcp: bool = True,
    include_plugin: bool = True,
) -> dict[str, object]:
    """Build JSON payload for ``OPENCODE_CONFIG_CONTENT``.

    Runtime wrap keeps OpenCode's provider/model selection intact. The
    Headroom plugin itself is installed as a local plugin file under
    OpenCode's own plugin directory (see
    ``install_headroom_opencode_plugin_files``) rather than referenced here:
    OpenCode's local-plugin loader only resolves external npm dependencies
    (like ``@opencode-ai/plugin``) for files inside its own plugin/config
    tree, so a ``plugin`` entry pointing elsewhere — a ``file://`` URI or an
    unpublished npm package name — can never load. The plugin picks up its
    proxy URL/mode from environment variables set by ``build_launch_env``
    instead of constructor options.

    ``include_mcp``/``include_plugin`` are accepted for backward
    compatibility with existing call sites but no longer affect the emitted
    content; both are handled outside ``OPENCODE_CONFIG_CONTENT`` now
    (MCP via the OpenCode MCP registrar, the plugin via local plugin files).
    """
    del port, include_mcp, include_plugin
    return {}


def build_launch_env(
    port: int,
    environ: Mapping[str, str] | None = None,
    project: str | None = None,
    *,
    include_mcp: bool = True,
    include_plugin: bool = True,
) -> tuple[dict[str, str], list[str]]:
    """Build environment variables for launching OpenCode through Headroom.

    Installs the Headroom plugin bundle into OpenCode's local plugin
    directory (idempotent, always resolvable — no npm publish or local build
    required) and points it at the local proxy via ``HEADROOM_PROXY_URL``,
    which the plugin reads as a fallback when loaded without constructor
    options (as all OpenCode local-file plugins are).
    """
    env = dict(environ or os.environ)
    display: list[str] = []

    if include_plugin:
        install_headroom_opencode_plugin_files()
        env["HEADROOM_PROXY_URL"] = proxy_server_url(port)
        env.setdefault("HEADROOM_OPENCODE_MODE", "native-fetch")
        display.append(f"HEADROOM_PROXY_URL={proxy_server_url(port)}")
        display.append("plugin=~/.config/opencode/plugins/headroom-opencode-*.js (local file)")

    del include_mcp

    if project and "HEADROOM_PROJECT" not in env:
        env["HEADROOM_PROJECT"] = project

    return env, display
