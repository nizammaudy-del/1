"""Headroom Dashboard - Real-time proxy monitoring UI."""

import json
import re
from pathlib import Path
from typing import Any

DASHBOARD_DIR = Path(__file__).parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
PARTIALS_DIR = TEMPLATES_DIR / "partials"
HELP_TEXT_PATH = TEMPLATES_DIR / "help_text.json"

INCLUDE_RE = re.compile(r"[ \t]*<!-- INCLUDE: ([\w./-]+) -->\n?")

# Matches the placeholder tokens embedded in data-help-title/data-help
# attributes across the templates, e.g. {{help.windows.ttl_bucket.title}}.
HELP_TOKEN_RE = re.compile(r"\{\{help\.([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\.(title|body)\}\}")

# Placeholder swapped for the inlined help-text JSON blob, so JS that can't
# use a plain literal attribute (Alpine dynamic bindings, raw JS template
# strings) can still source the same single JSON file at runtime.
HELP_DATA_SCRIPT_RE = re.compile(r"[ \t]*<!-- HELP_TEXT_DATA -->\n?")


def _load_help_text() -> dict[str, Any]:
    return json.loads(HELP_TEXT_PATH.read_text(encoding="utf-8"))


def get_dashboard_html() -> str:
    """Load the dashboard HTML template, inlining any partials it references."""
    shell = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    def _inline(match: re.Match[str]) -> str:
        partial_path = (PARTIALS_DIR / match.group(1)).resolve()
        # Defense in depth: INCLUDE markers only ever come from our own
        # shipped dashboard.html, never user input, but a `../` in one
        # should still never be able to read a file outside partials/.
        if PARTIALS_DIR.resolve() not in partial_path.parents:
            raise ValueError(f"INCLUDE path escapes partials directory: {match.group(1)!r}")
        return partial_path.read_text(encoding="utf-8")

    html = INCLUDE_RE.sub(_inline, shell)

    help_text = _load_help_text()

    def _substitute_token(match: re.Match[str]) -> str:
        namespace, key, field = match.group(1), match.group(2), match.group(3)
        try:
            return str(help_text[namespace][key][field])
        except KeyError as exc:
            raise ValueError(f"Missing help_text.json entry for {namespace}.{key}.{field}") from exc

    html = HELP_TOKEN_RE.sub(_substitute_token, html)

    # Inline the same JSON so Alpine `:data-help`/`:data-help-title` dynamic
    # bindings and raw JS template strings can look strings up at runtime
    # too, instead of duplicating hardcoded copies of them.
    help_json = json.dumps(help_text, ensure_ascii=False).replace("</", "<\\/")
    help_script = f'<script id="help-text-data" type="application/json">{help_json}</script>\n'
    html = HELP_DATA_SCRIPT_RE.sub(help_script, html)

    return html
