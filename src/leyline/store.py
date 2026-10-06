"""The SQLite store. Everything else reads and writes through this module."""

from __future__ import annotations

import json
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Iterable

from .model import Edge, Node

SCHEMA_VERSION = "0"


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=OFF")  # edges may point at nodes written later in a run
    con.executescript(resources.files("leyline").joinpath("schema.sql").read_text())
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    return con


def clear_facts(con: sqlite3.Connection, repo_id: str) -> None:
    """Drop every fact row for a repo. Inferred and intent rows are left alone."""
    ids = "SELECT id FROM nodes WHERE repo_id = ? AND layer = 'fact'"
    con.execute(f"DELETE FROM calls WHERE src_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM edges WHERE layer = 'fact' AND src_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM ancestry WHERE node_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM search WHERE node_id IN ({ids})", (repo_id,))
    con.execute("DELETE FROM nodes WHERE repo_id = ? AND layer = 'fact'", (repo_id,))
    con.execute("DELETE FROM extractor_coverage WHERE repo_id = ?", (repo_id,))


def write_nodes(con, nodes: Iterable[Node], repo_id: str, source: str, commit: str | None) -> None:
    con.executemany(
        "INSERT OR IGNORE INTO nodes (id, kind, name, parent_id, repo_id, language, path,"
        " span_start, span_end, content_hash, layer, source, commit_sha, attrs)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'fact',?,?,?)",
        [
            (n.id, n.kind, n.name, n.parent_id, repo_id, n.language, n.path, n.span_start,
             n.span_end, n.content_hash, source, commit, json.dumps(n.attrs) if n.attrs else None)
            for n in nodes
        ],
    )


def write_edges(con, edges: Iterable[Edge], source: str, commit: str | None) -> None:
    con.executemany(
        "INSERT INTO edges (kind, src_id, dst_id, precision, layer, source, commit_sha, attrs)"
        " VALUES (?,?,?,?,'fact',?,?,?)",
        [
            (e.kind, e.src_id, e.dst_id, e.precision, source, commit,
             json.dumps(e.attrs) if e.attrs else None)
            for e in edges
        ],
    )


def write_calls(con, rows: Iterable[tuple], commit: str | None) -> None:
    """rows: (src_id, dst_id, dispatch, precision, line)"""
    con.executemany(
        "INSERT INTO calls (src_id, dst_id, dispatch, precision, site_start, site_end, commit_sha)"
        " VALUES (?,?,?,?,?,?,?)",
        [(s, d, disp, prec, line, line, commit) for (s, d, disp, prec, line) in rows],
    )


def write_coverage(con, repo_id, extractor, version, status, commit, stats: dict) -> None:
    con.execute(
        "INSERT OR REPLACE INTO extractor_coverage VALUES (?,?,?,?,?,?)",
        (repo_id, extractor, version, status, commit, json.dumps(stats)),
    )


def rebuild_derived(con: sqlite3.Connection) -> None:
    """Recompute the ancestry cache and the search index from the node table."""
    con.execute("DELETE FROM ancestry")
    con.execute("DELETE FROM search")
    rows = con.execute("SELECT id, kind, name, parent_id, path FROM nodes").fetchall()
    by_id = {r["id"]: r for r in rows}
    out = []
    for r in rows:
        file_id = module_id = None
        cur = r
        seen = 0
        while cur is not None and seen < 64:
            if cur["kind"] == "file" and file_id is None:
                file_id = cur["id"]
            if cur["kind"] == "module" and module_id is None:
                module_id = cur["id"]
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] else None
            seen += 1
        out.append((r["id"], file_id, module_id))
    con.executemany("INSERT INTO ancestry VALUES (?,?,?)", out)
    con.executemany(
        "INSERT INTO search (node_id, name, qualified, path, kind) VALUES (?,?,?,?,?)",
        [
            (r["id"], r["name"], _searchable(r["id"]), r["path"] or "", r["kind"])
            for r in rows
            if r["kind"] not in ("repo",)
        ],
    )


def _searchable(node_id: str) -> str:
    # Split the qualified part of an id into words so "Simulation Step" finds Simulation.Step.
    tail = node_id.split(":", 2)[-1]
    for ch in ".()/,<>:_":
        tail = tail.replace(ch, " ")
    return tail
