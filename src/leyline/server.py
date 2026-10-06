"""MCP server over a Leyline store. Read-only in this version."""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import query, store

mcp = FastMCP(
    "leyline",
    instructions=(
        "Leyline is a graph of a codebase. Start with `overview` for the module map, `search` to find a"
        " node id by name, then `expand` a node to see what it contains and how it connects."
        " Edges marked heuristic come from syntax alone and can be wrong; exact edges cannot."
    ),
)

_con: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _con
    if _con is None:
        _con = store.connect(os.environ.get("LEYLINE_DB", ".leyline/leyline.db"))
    return _con


@mcp.tool()
def overview() -> dict:
    """The top-level map: repos, modules with sizes, module-to-module dependencies with counts per
    edge kind, external packages, and which extractors ran. Call this first."""
    return query.overview(_db())


@mcp.tool()
def expand(node_id: str, limit: int = 50) -> dict:
    """One node in detail. A module returns its files, public types, entry points and dependencies.
    A type returns its members, bases and users. A callable returns its signature, callers and callees.
    Ids come from `overview` or `search`."""
    return query.expand(_db(), node_id, limit)


@mcp.tool()
def search(text: str, kind: Optional[str] = None, limit: int = 20) -> dict:
    """Find nodes by name, qualified name or path. `kind` narrows to one of: module, file, type,
    callable, field, entry_point, external."""
    return query.search(_db(), text, kind, limit)


@mcp.tool()
def neighbors(node_id: str, direction: str = "both", kinds: Optional[list[str]] = None,
              limit: int = 100) -> dict:
    """Edges around a node. `direction` is out, in or both. `kinds` filters by edge kind: calls,
    imports, uses_type, extends, implements, instantiates, has_field, exposes, depends_on."""
    return query.neighbors(_db(), node_id, direction, kinds, limit)


@mcp.tool()
def source(node_id: str, max_lines: int = 200) -> dict:
    """The source text of a type, callable or field, read from the indexed working tree."""
    return query.source(_db(), node_id, max_lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
