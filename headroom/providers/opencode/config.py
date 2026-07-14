"""OpenCode config file helpers for wrap and persistent install."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import click

from headroom.install.paths import opencode_config_path

# Headroom-managed JSON marker comments for idempotent block injection.
_PROVIDER_MARKER_START = "// --- Headroom proxy provider ---"
_PROVIDER_MARKER_END = "// --- end Headroom proxy provider ---"
_MCP_MARKER_START = "// --- Headroom MCP server ---"
_MCP_MARKER_END = "// --- end Headroom MCP server ---"

# Regex to strip headroom blocks (including the marker comments).
_PROVIDER_BLOCK_RE = re.compile(
    re.escape(_PROVIDER_MARKER_START) + r".*?" + re.escape(_PROVIDER_MARKER_END),
    re.DOTALL,
)
_MCP_BLOCK_RE = re.compile(
    re.escape(_MCP_MARKER_START) + r".*?" + re.escape(_MCP_MARKER_END),
    re.DOTALL,
)
HEADROOM_OPENCODE_PLUGIN = "headroom-opencode"

# Filename of the manifest Headroom writes into OpenCode's local plugin
# directory, recording exactly which bundle filenames it owns there (see
# `install_headroom_opencode_plugin_files`/`remove_headroom_opencode_plugin_files`).
_PLUGIN_MANIFEST_FILENAME = ".headroom-plugin-manifest.json"


def _proxy_server_url(port: int) -> str:
    """Return the local Headroom proxy origin used by the OpenCode plugin."""
    return f"http://127.0.0.1:{port}"


def _opencode_home_dir() -> Path:
    """Return the OpenCode home/config directory."""
    env_path = os.environ.get("OPENCODE_HOME", "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".config" / "opencode"


def opencode_config_paths() -> tuple[Path, Path]:
    """Return ``(config_file, backup_file)`` for OpenCode."""
    config_file = opencode_config_path()
    backup_file = config_file.with_suffix(".json.headroom-backup")
    return config_file, backup_file


def snapshot_opencode_config_if_unwrapped(config_file: Path, backup_file: Path) -> None:
    """Snapshot ``opencode.json`` to ``backup_file`` before the first injection.

    Guarantees that ``headroom unwrap opencode`` can restore the user's
    original file byte-for-byte.
    """
    if backup_file.exists():
        return
    if not config_file.exists():
        return
    try:
        content = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if _PROVIDER_MARKER_START in content or _MCP_MARKER_START in content:
        return
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_file, backup_file)


def strip_opencode_headroom_blocks(content: str, *, remove_mcp: bool = True) -> str:
    """Remove all Headroom-managed blocks from opencode JSON text.

    Preserves user content. Returns the cleaned string.
    """
    content = _PROVIDER_BLOCK_RE.sub("", content)
    if remove_mcp:
        content = _MCP_BLOCK_RE.sub("", content)
    # Collapse multiple blank lines left behind by block removal.
    content = re.sub(r"\n{3,}", "\n\n", content)
    return content.strip()


def _render_provider_block(port: int) -> str:
    """Render a Headroom provider block as a JSON comment-wrapped snippet."""
    provider = {
        "headroom": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Headroom Proxy",
            "options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
        }
    }
    lines = [
        _PROVIDER_MARKER_START,
        f'"provider": {json.dumps(provider, indent=2)},',
        _PROVIDER_MARKER_END,
    ]
    return "\n".join(lines)


def _render_mcp_block(port: int) -> str:
    """Render a Headroom MCP block as a JSON comment-wrapped snippet."""
    mcp = {
        "headroom": {
            "type": "remote",
            "url": f"http://127.0.0.1:{port}/mcp",
            "enabled": True,
        }
    }
    lines = [
        _MCP_MARKER_START,
        f'"mcp": {json.dumps(mcp, indent=2)},',
        _MCP_MARKER_END,
    ]
    return "\n".join(lines)


def _parse_json_loose(text: str) -> dict[str, Any]:
    """Parse JSON text, stripping line comments (// ...) when needed.

    Tries standard JSON first to avoid corrupting URLs that contain ``//``.
    Falls back to stripping ``//`` comments when standard parsing fails.
    Two-pass: (1) remove comment-only lines, (2) strip inline trailing
    comments that follow a comma.
    """
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    # Pass 1: remove lines that are ONLY a comment.
    cleaned = re.sub(r"^\s*//[^\n]*\n", "", text, flags=re.MULTILINE)
    # Pass 2: remove inline trailing comments (", // comment").
    cleaned = re.sub(r",\s*//[^\n]*", ",", cleaned)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _inject_key_into_json(data: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """Merge ``value`` into ``data[key]`` idempotently."""
    existing = data.get(key)
    if isinstance(existing, dict) and isinstance(value, dict):
        merged = {**existing, **value}
        data[key] = merged
    else:
        data[key] = value
    return data


def _is_headroom_plugin_entry(entry: object) -> bool:
    """Return True when *entry* is the headroom-opencode plugin."""
    resolved_plugin_spec = _resolve_plugin_spec().rstrip("/")

    def _matches(spec: str) -> bool:
        normalized = spec.rstrip("/")
        return (
            normalized == HEADROOM_OPENCODE_PLUGIN
            or normalized == resolved_plugin_spec
            or normalized.endswith("/headroom-opencode")
        )

    if isinstance(entry, str):
        return _matches(entry)
    if isinstance(entry, list) and entry:
        spec = entry[0]
        if isinstance(spec, str):
            return _matches(spec)
    return False


_plugin_spec_override: str | None = None


def _packaged_plugin_dist_dir() -> Path | None:
    """Return the directory holding the pre-built OpenCode plugin bundle.

    Ships inside the ``headroom`` Python package itself
    (``headroom/providers/opencode/_plugin_dist/dist/``) so it is present in
    every install path (pip, pipx, uv tool, editable/dev checkout) without
    depending on an npm registry publish of ``headroom-opencode`` or on a
    local ``npm run build`` having been run. Falls back to a dev-checkout
    build under ``plugins/opencode/dist`` if present (e.g. running straight
    out of a repo checkout after ``npm run build`` there instead).
    """
    packaged = Path(__file__).resolve().parent / "_plugin_dist" / "dist"
    if (packaged / "index.js").is_file():
        return packaged
    for parents_up in (2, 3):
        candidate = Path(__file__).resolve().parents[parents_up] / "plugins" / "opencode" / "dist"
        if (candidate / "index.js").is_file():
            return candidate
    return None


def opencode_plugin_files_dir() -> Path:
    """Return OpenCode's global local-plugin directory.

    OpenCode auto-loads any JS/TS file placed here at startup ("local files"
    plugin loading; see https://opencode.ai/docs/plugins). Placing the
    Headroom plugin bundle here also lets its bundled ``@opencode-ai/plugin``
    import resolve via normal Node ESM parent-directory module resolution,
    since OpenCode already installs that package under
    ``~/.config/opencode/node_modules``.
    """
    return _opencode_home_dir() / "plugins"


def install_headroom_opencode_plugin_files() -> bool:
    """Copy the pre-built Headroom plugin bundle into OpenCode's plugin dir.

    Returns True if files were written (fresh install or content changed),
    False if an identical copy was already present (no-op).

    This replaces routing the plugin through ``OPENCODE_CONFIG_CONTENT`` /
    ``opencode.json``'s ``plugin`` array with a file:// or npm-package
    reference: OpenCode's local-plugin loader does not support external
    dependencies for arbitrary plugin paths (only for its own plugin
    directory or an npm-published package), so a package name that is never
    published to npm — or a file:// URI outside OpenCode's own plugin/config
    tree — can never resolve. Copying the compiled bundle into OpenCode's own
    plugin directory is the one loading path that always works, with no
    npm publish and no local build step required from the user.

    Filenames are kept exactly as tsup emitted them (e.g. ``index.js``,
    content-hashed ``chunk-<hash>.js``): the entry point's bundled relative
    imports (``./chunk-<hash>.js``) are not rewritten, so renaming any file
    here would break module resolution. A manifest file
    (``.headroom-plugin-manifest.json``) records which filenames Headroom
    owns in this directory, so stale files from a previous bundle version
    (chunk hashes change between builds) and the whole install can be
    cleaned up precisely without a naming convention that would collide
    with the bundle's own internal references.
    """
    dist_dir = _packaged_plugin_dist_dir()
    if dist_dir is None:
        raise click.ClickException(
            "Headroom's OpenCode plugin bundle is missing from this install. "
            "Reinstall headroom-ai, or run 'npm run build' in plugins/opencode "
            "if developing from a source checkout."
        )

    target_dir = opencode_plugin_files_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = target_dir / _PLUGIN_MANIFEST_FILENAME

    # Remove any previously-installed Headroom files first (from an older
    # bundle version, e.g. content-hashed chunk filenames that no longer
    # exist in the current build) so no orphaned chunk is left behind.
    changed = remove_headroom_opencode_plugin_files()

    written: list[str] = []
    for source_file in sorted(dist_dir.glob("*.js")):
        dest_file = target_dir / source_file.name
        new_bytes = source_file.read_bytes()
        if not dest_file.is_file() or dest_file.read_bytes() != new_bytes:
            dest_file.write_bytes(new_bytes)
            changed = True
        written.append(source_file.name)

    manifest_file.write_text(json.dumps({"files": written}, indent=2) + "\n", encoding="utf-8")
    return changed


def remove_headroom_opencode_plugin_files() -> bool:
    """Remove all Headroom-owned files from OpenCode's local plugin dir.

    Uses the manifest written by ``install_headroom_opencode_plugin_files``
    to know exactly which filenames Headroom owns, since bundle filenames
    are not prefixed (see that function's docstring) and could otherwise be
    confused with a user's own same-named plugin file.
    """
    target_dir = opencode_plugin_files_dir()
    manifest_file = target_dir / _PLUGIN_MANIFEST_FILENAME
    if not manifest_file.is_file():
        return False

    removed = False
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        filenames = manifest.get("files", [])
    except (OSError, ValueError):
        filenames = []

    for name in filenames:
        candidate = target_dir / name
        if candidate.is_file():
            candidate.unlink()
            removed = True
        map_candidate = target_dir / f"{name}.map"
        if map_candidate.is_file():
            map_candidate.unlink()

    manifest_file.unlink()
    return removed


def _resolve_plugin_spec() -> str:
    """Resolve a plugin spec OpenCode can load.
    """
    if _plugin_spec_override is not None:
        return _plugin_spec_override
    candidates = (
        Path(__file__).resolve().parents[2] / "plugins" / "opencode",
        Path(__file__).resolve().parents[3] / "plugins" / "opencode",
    )
    for candidate in candidates:
        manifest = candidate / "package.json"
        dist_entry = candidate / "dist" / "index.js"
        if manifest.is_file() and dist_entry.is_file():
            return candidate.as_uri()
    return HEADROOM_OPENCODE_PLUGIN


def _make_headroom_plugin_entry(
    *, proxy_url: str | None = None, mode: str | None = None
) -> object:
    """Build a headroom-opencode plugin entry."""
    options: dict[str, object] = {}
    if proxy_url is not None:
        options["proxyUrl"] = proxy_url
    if mode is not None:
        options["mode"] = mode
    if not options:
        return _resolve_plugin_spec()
    return [_resolve_plugin_spec(), options]


def append_headroom_plugin(
    config: dict[str, object], *, proxy_url: str | None = None, mode: str | None = None
) -> bool:
    """Append the Headroom OpenCode plugin entry if it is not already present."""
    plugin = config.get("plugin")
    headroom_entry = _make_headroom_plugin_entry(proxy_url=proxy_url, mode=mode)

    if plugin is None:
        config["plugin"] = [headroom_entry]
        return True

    if not isinstance(plugin, list):
        return False

    options_changed = headroom_entry != _resolve_plugin_spec()

    for index, existing in enumerate(plugin):
        if not _is_headroom_plugin_entry(existing):
            continue
        if not options_changed:
            return False
        if isinstance(existing, str) and isinstance(headroom_entry, list):
            plugin[index] = headroom_entry
            return True
        if existing == headroom_entry:
            return False
        plugin[index] = headroom_entry
        return True

    plugin.append(headroom_entry)
    return True


def remove_headroom_plugin(config: dict[str, object]) -> bool:
    """Remove any Headroom-owned OpenCode plugin entries from ``config``."""
    plugin = config.get("plugin")
    if not isinstance(plugin, list):
        return False

    filtered = [entry for entry in plugin if not _is_headroom_plugin_entry(entry)]
    if len(filtered) == len(plugin):
        return False

    if filtered:
        config["plugin"] = filtered
    else:
        config.pop("plugin", None)
    return True


def strip_opencode_runtime_plugin_config(config_file: Path) -> bool:
    """Remove persisted Headroom plugin entries from ``config_file``.

    Returns ``True`` when the file was changed or deleted. If the resulting
    config becomes empty, the file is removed so OpenCode falls back to its
    native defaults.
    """
    if not config_file.exists():
        return False

    content = config_file.read_text(encoding="utf-8", errors="replace")
    data = _parse_json_loose(content)
    if not data:
        return False
    if not remove_headroom_plugin(data):
        return False

    if data:
        config_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    else:
        config_file.unlink()
    return True


def inject_opencode_provider_config(port: int) -> None:
    """Install the Headroom OpenCode plugin for ``--prepare-only`` inspection.

    Installs the same local plugin files ``headroom wrap opencode`` installs
    at launch (see ``install_headroom_opencode_plugin_files``), so a
    ``--prepare-only`` run leaves OpenCode in the exact state a real launch
    would. Does not write a ``plugin`` entry into ``opencode.json``: OpenCode's
    local-plugin loader does not resolve external npm dependencies (like
    ``@opencode-ai/plugin``) for paths outside its own plugin/config tree, so
    a ``file://`` URI written here could never load.
    """
    config_file, backup_file = opencode_config_paths()
    config_dir = config_file.parent

    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        snapshot_opencode_config_if_unwrapped(config_file, backup_file)

        if config_file.exists():
            content = config_file.read_text(encoding="utf-8", errors="replace")
            # Strip any prior Headroom-managed blocks/plugin entries written
            # by older Headroom versions before this local-plugin-file
            # approach.
            if _PROVIDER_MARKER_START in content:
                content = strip_opencode_headroom_blocks(content)
                config_file.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(
            f"could not clean OpenCode config at {config_file}: {exc}"
        ) from exc

    try:
        strip_opencode_runtime_plugin_config(config_file)
    except OSError as exc:
        raise click.ClickException(
            f"could not clean stale Headroom OpenCode plugin config at {config_file}: {exc}"
        ) from exc

    install_headroom_opencode_plugin_files()
