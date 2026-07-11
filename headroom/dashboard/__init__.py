"""Headroom Dashboard - Real-time proxy monitoring UI."""

import re
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent
TEMPLATES_DIR = DASHBOARD_DIR / "templates"
PARTIALS_DIR = TEMPLATES_DIR / "partials"

INCLUDE_RE = re.compile(r"[ \t]*<!-- INCLUDE: ([\w./-]+) -->\n?")


def get_dashboard_html() -> str:
    """Load the dashboard HTML template, inlining any partials it references."""
    shell = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")

    def _inline(match: re.Match) -> str:
        partial_path = (PARTIALS_DIR / match.group(1)).resolve()
        # Defense in depth: INCLUDE markers only ever come from our own
        # shipped dashboard.html, never user input, but a `../` in one
        # should still never be able to read a file outside partials/.
        if PARTIALS_DIR.resolve() not in partial_path.parents:
            raise ValueError(f"INCLUDE path escapes partials directory: {match.group(1)!r}")
        return partial_path.read_text(encoding="utf-8")

    return INCLUDE_RE.sub(_inline, shell)
