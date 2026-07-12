"""CLI: assemble graph-scoped context for an entrypoint, proxy-style.

Walks the project's import graph (AST-derived, cached, hash-invalidated)
from a given entrypoint instead of reading every file, then prints the full
content of every file reached — ready to paste into an LLM prompt alongside
the query.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from headroom.graph_context import DEFAULT_MAX_DEPTH, assemble_context

from .main import main


@main.command(name="graph-context")
@click.argument("entrypoint")
@click.argument("query")
@click.option(
    "--max-depth",
    type=click.IntRange(min=0),
    default=DEFAULT_MAX_DEPTH,
    show_default=True,
    help="Max BFS hops from the entrypoint over the import graph.",
)
@click.option(
    "--project-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root to build the import graph from.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit {entrypoint, query, files, context} as JSON.")
def graph_context(entrypoint: str, query: str, max_depth: int, project_root: Path, as_json: bool) -> None:
    """Assemble graph-scoped context from ENTRYPOINT for QUERY.

    \b
    Example:
        headroom graph-context src/main.py "how is auth handled?" --max-depth 2
    """

    result = assemble_context(project_root, entrypoint, query, max_depth=max_depth)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "entrypoint": result.entrypoint,
                    "query": result.query,
                    "max_depth": result.max_depth,
                    "files": result.files,
                    "context": result.context,
                },
                indent=2,
            )
        )
        return

    click.echo(result.prompt)
