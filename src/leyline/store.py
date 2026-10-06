"""The SQLite store. Everything else reads and writes through this module."""

from __future__ import annotations

import hashlib
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
    have = {r[1] for r in con.execute("PRAGMA table_info(rules)")}
    for col in ("status", "source", "created"):
        if col not in have:
            con.execute(f"ALTER TABLE rules ADD COLUMN {col} TEXT")
    for table, cols in (("tours", ("repo_id", "created")), ("tour_stops", ("title",)), ("pattern_instances", ("attrs",))):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    return con


def clear_facts(con: sqlite3.Connection, repo_id: str) -> None:
    """Drop every fact row for a repo. Inferred and intent rows are left alone."""
    ids = "SELECT id FROM nodes WHERE repo_id = ? AND layer = 'fact'"
    con.execute(f"DELETE FROM calls WHERE src_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM edges WHERE layer = 'fact' AND src_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM ancestry WHERE node_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM search WHERE node_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM flow_steps WHERE flow_id IN (SELECT id FROM flows WHERE entry_id IN ({ids}))", (repo_id,))
    con.execute(f"DELETE FROM flows WHERE layer = 'fact' AND entry_id IN ({ids})", (repo_id,))
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


def write_flows(con, repo_id: str, flows, steps) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO flows (id, name, origin, entry_id, weight, group_id, layer, source, attrs)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(f[0], f[1], f[2], f[3], f[4], f[5], f[6], f[7], json.dumps(f[8])) for f in flows])
    con.executemany(
        "INSERT INTO flow_steps (flow_id, seq, depth, callable_id, via, site_line, parent_seq) VALUES (?,?,?,?,?,?,?)",
        steps)


def evidence_hash(con, evidence: Iterable[str]) -> str:
    """A hash of the files the evidence nodes live in. It changes when any of them is edited."""
    parts = []
    for node_id in sorted(set(evidence)):
        row = con.execute(
            "SELECT COALESCE(f.content_hash, n.content_hash, '') FROM nodes n"
            " LEFT JOIN ancestry a ON a.node_id = n.id LEFT JOIN nodes f ON f.id = a.file_id WHERE n.id = ?",
            (node_id,)).fetchone()
        parts.append(f"{node_id}:{row[0] if row else 'missing'}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def annotate(con, node_id: str, key: str, value: str, layer: str, source: str,
             confidence: float | None, evidence: list[str]) -> dict:
    """Write one inferred or intent statement about a node, replacing an earlier one with the same key."""
    if layer not in ("inferred", "intent"):
        raise ValueError("layer must be 'inferred' or 'intent'; facts come only from extractors")
    if con.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone() is None:
        raise ValueError(f"no node with id {node_id!r}")
    if layer == "inferred":
        if not evidence:
            raise ValueError("an inferred annotation needs evidence: the node ids it is based on")
        missing = [e for e in evidence if con.execute("SELECT 1 FROM nodes WHERE id = ?", (e,)).fetchone() is None]
        if missing:
            raise ValueError(f"evidence ids not in the store: {missing[:3]}")
    with con:
        con.execute("DELETE FROM annotations WHERE node_id = ? AND key = ? AND layer = ?", (node_id, key, layer))
        con.execute(
            "INSERT INTO annotations (node_id, key, value, layer, source, confidence, evidence, evidence_hash, stale)"
            " VALUES (?,?,?,?,?,?,?,?,0)",
            (node_id, key, value, layer, source, confidence, json.dumps(evidence or []),
             evidence_hash(con, evidence or [])))
    return {"node_id": node_id, "key": key, "layer": layer, "evidence": len(evidence or [])}


def refresh_stale(con) -> int:
    """Flag annotations whose evidence changed or whose node is gone. Returns how many are stale."""
    stale = 0
    with con:
        for a in con.execute("SELECT id, node_id, evidence, evidence_hash FROM annotations").fetchall():
            gone = con.execute("SELECT 1 FROM nodes WHERE id = ?", (a["node_id"],)).fetchone() is None
            changed = evidence_hash(con, json.loads(a["evidence"] or "[]")) != a["evidence_hash"]
            flag = 1 if gone or (changed and a["evidence"] not in (None, "[]")) else 0
            con.execute("UPDATE annotations SET stale = ? WHERE id = ?", (flag, a["id"]))
            stale += flag
    return stale


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
