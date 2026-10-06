"""The query layer. The MCP server, the CLI and the viewer all call these functions."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

ROLLUP_KINDS = ("imports", "uses_type", "extends", "implements", "instantiates")


def _attrs(row) -> dict:
    return json.loads(row["attrs"]) if row["attrs"] else {}


def _brief(row) -> dict:
    a = _attrs(row)
    out = {"id": row["id"], "kind": row["kind"], "name": row["name"]}
    if row["path"]:
        out["path"] = row["path"]
    if row["span_start"] and row["kind"] not in ("file", "module", "repo"):
        out["lines"] = [row["span_start"], row["span_end"]]
    for k in ("native_kind", "visibility", "signature", "declared_type", "trigger"):
        if a.get(k) is not None:
            out[k] = a[k]
    return out


def _node(con, node_id: str):
    return con.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()


def _module_of(alias: str) -> str:
    # A node's module: itself if it is a module, otherwise the cached ancestor.
    return (f"COALESCE((SELECT module_id FROM ancestry WHERE node_id = {alias}),"
            f" CASE WHEN (SELECT kind FROM nodes WHERE id = {alias}) = 'module' THEN {alias} END)")


def module_edges(con) -> list[dict]:
    """Every edge rolled up to module level, with a count per edge kind."""
    acc: dict[tuple, dict] = {}
    q = (f"SELECT {_module_of('e.src_id')} AS s, {_module_of('e.dst_id')} AS d, e.kind AS k, COUNT(*) AS n"
         f" FROM edges e WHERE e.kind IN ({','.join('?' * len(ROLLUP_KINDS))}) GROUP BY s, d, k")
    for r in con.execute(q, ROLLUP_KINDS):
        if r["s"] and r["d"] and r["s"] != r["d"]:
            acc.setdefault((r["s"], r["d"]), {})[r["k"]] = r["n"]
    q = (f"SELECT {_module_of('c.src_id')} AS s, {_module_of('c.dst_id')} AS d, COUNT(*) AS n"
         " FROM calls c GROUP BY s, d")
    for r in con.execute(q):
        if r["s"] and r["d"] and r["s"] != r["d"]:
            acc.setdefault((r["s"], r["d"]), {})["calls"] = r["n"]
    out = [{"from": s, "to": d, **kinds, "total": sum(kinds.values())} for (s, d), kinds in acc.items()]
    return sorted(out, key=lambda e: -e["total"])


def overview(con) -> dict:
    """The top-level map: repos, modules, how modules depend on each other, and what was analyzed."""
    repos = []
    for repo in con.execute("SELECT * FROM nodes WHERE kind = 'repo' ORDER BY id"):
        modules = []
        for m in con.execute("SELECT * FROM nodes WHERE kind = 'module' AND repo_id = ? ORDER BY path", (repo["id"],)):
            counts = {r["kind"]: r["n"] for r in con.execute(
                "SELECT n.kind, COUNT(*) AS n FROM ancestry a JOIN nodes n ON n.id = a.node_id"
                " WHERE a.module_id = ? AND n.id != ? GROUP BY n.kind", (m["id"], m["id"]))}
            files = con.execute(
                "SELECT language, COUNT(*) AS n, SUM(span_end) AS loc FROM nodes"
                " WHERE kind = 'file' AND parent_id = ? GROUP BY language", (m["id"],)).fetchall()
            modules.append({
                "id": m["id"], "name": m["name"], "path": m["path"] or ".",
                "languages": {f["language"]: f["n"] for f in files},
                "files": sum(f["n"] for f in files), "loc": sum(f["loc"] or 0 for f in files),
                "types": counts.get("type", 0), "callables": counts.get("callable", 0),
                "entry_points": counts.get("entry_point", 0),
                **({"marker": _attrs(m)["marker"]} if _attrs(m).get("marker") else {}),
            })
        repos.append({"id": repo["id"], "commit": repo["commit_sha"], **_attrs(repo), "modules": modules})
    externals = []
    for x in con.execute("SELECT * FROM nodes WHERE kind = 'external' ORDER BY name"):
        users = [r[0] for r in con.execute(
            f"SELECT DISTINCT {_module_of('e.src_id')} FROM edges e"
            " WHERE e.dst_id = ? AND e.kind IN ('imports', 'depends_on')", (x["id"],)) if r[0]]
        externals.append({"id": x["id"], "name": x["name"], **_attrs(x), "used_by": sorted(users)})
    coverage = [{"repo": r["repo_id"], "extractor": r["extractor"], "status": r["status"],
                 **({"stats": json.loads(r["stats"])} if r["stats"] and r["stats"] != "{}" else {})}
                for r in con.execute("SELECT * FROM extractor_coverage ORDER BY repo_id, status, extractor")]
    systems = [_brief(r) for r in con.execute("SELECT * FROM nodes WHERE kind = 'system'")]
    return {
        "repos": repos,
        "systems": systems,
        "module_edges": module_edges(con),
        "externals": externals,
        "coverage": coverage,
        "notes": ([] if systems else ["No systems yet: clustering and naming have not been run."])
        + ["Call, type-use and inheritance edges are heuristic (tree-sitter). Import and containment edges are exact."],
    }


def _edge_rows(con, node_id: str, direction: str, limit: int) -> dict:
    col, other = ("src_id", "dst_id") if direction == "out" else ("dst_id", "src_id")
    out: dict[str, dict] = {}
    for r in con.execute(
            f"SELECT e.kind AS k, e.precision, e.attrs AS eattrs, n.* FROM edges e JOIN nodes n ON n.id = e.{other}"
            f" WHERE e.{col} = ? AND e.kind != 'contains' ORDER BY e.kind, n.id", (node_id,)):
        group = out.setdefault(r["k"], {"total": 0, "items": []})
        group["total"] += 1
        if len(group["items"]) < limit:
            item = _brief(r)
            item["precision"] = r["precision"]
            if r["eattrs"]:
                item["edge"] = json.loads(r["eattrs"])
            group["items"].append(item)
    return out


def _call_rows(con, node_id: str, direction: str, limit: int) -> dict:
    col, other = ("src_id", "dst_id") if direction == "out" else ("dst_id", "src_id")
    rows = con.execute(
        f"SELECT n.*, COUNT(*) AS sites, MIN(c.site_start) AS first_line, c.dispatch, c.precision"
        f" FROM calls c JOIN nodes n ON n.id = c.{other} WHERE c.{col} = ?"
        f" GROUP BY n.id ORDER BY sites DESC, n.id", (node_id,)).fetchall()
    items = []
    for r in rows[:limit]:
        item = _brief(r)
        item.update({"sites": r["sites"], "dispatch": r["dispatch"], "precision": r["precision"]})
        if direction == "out":
            item["first_call_line"] = r["first_line"]
        items.append(item)
    return {"total": len(rows), "items": items}


def expand(con, node_id: str, limit: int = 50) -> dict:
    """One node in detail: what it contains, what it depends on, and what depends on it."""
    row = _node(con, node_id)
    if row is None:
        hits = search(con, node_id.split(":")[-1], limit=5)["results"]
        return {"error": f"No node with id {node_id!r}.", "did_you_mean": hits}
    out = _brief(row)
    out.update({k: v for k, v in _attrs(row).items() if k not in out and v not in (None, [], "")})
    out["layer"], out["source"] = row["layer"], row["source"]
    if row["parent_id"]:
        p = _node(con, row["parent_id"])
        out["parent"] = {"id": p["id"], "kind": p["kind"], "name": p["name"]} if p else row["parent_id"]
    children = con.execute(
        "SELECT * FROM nodes WHERE parent_id = ? ORDER BY kind, span_start, name", (node_id,)).fetchall()
    kind = row["kind"]
    if kind == "module":
        files = [c for c in children if c["kind"] == "file"]
        out["files"] = [{"id": f["id"], "name": f["name"], "language": f["language"], "loc": f["span_end"]}
                        for f in files[:limit]]
        types = con.execute(
            "SELECT n.* FROM ancestry a JOIN nodes n ON n.id = a.node_id"
            " WHERE a.module_id = ? AND n.kind = 'type' ORDER BY n.path, n.span_start", (node_id,)).fetchall()
        public = [t for t in types if "public" in (_attrs(t).get("visibility") or "")]
        out["public_types"] = {"total": len(public), "items": [_brief(t) for t in public[:limit]]}
        out["internal_types"] = len(types) - len(public)
        out["entry_points"] = [_brief(e) for e in con.execute(
            "SELECT n.* FROM ancestry a JOIN nodes n ON n.id = a.node_id"
            " WHERE a.module_id = ? AND n.kind = 'entry_point' ORDER BY n.path", (node_id,))][:limit]
        edges = module_edges(con)
        out["depends_on"] = [e for e in edges if e["from"] == node_id]
        out["depended_on_by"] = [e for e in edges if e["to"] == node_id]
        out["externals"] = sorted({r[0] for r in con.execute(
            "SELECT DISTINCT x.name FROM edges e JOIN nodes x ON x.id = e.dst_id AND x.kind = 'external'"
            f" WHERE {_module_of('e.src_id')} = ?", (node_id,))})
    else:
        by_kind: dict[str, list] = {}
        for c in children:
            by_kind.setdefault(c["kind"], []).append(c)
        if by_kind:
            out["contains"] = {k: {"total": len(v), "items": [_brief(c) for c in v[:limit]]}
                               for k, v in by_kind.items()}
    out["out"] = _edge_rows(con, node_id, "out", limit)
    out["in"] = _edge_rows(con, node_id, "in", limit)
    if kind in ("callable", "field"):
        calls, callers = _call_rows(con, node_id, "out", limit), _call_rows(con, node_id, "in", limit)
        if calls["total"]:
            out["calls"] = calls
        if callers["total"]:
            out["called_by"] = callers
    if kind == "type":
        users: dict[str, int] = {}
        for r in con.execute(
                "SELECT a.module_id AS m, COUNT(*) AS n FROM edges e JOIN ancestry a ON a.node_id = e.src_id"
                " WHERE e.dst_id = ? AND e.kind IN ('uses_type', 'instantiates', 'extends', 'implements')"
                " GROUP BY a.module_id", (node_id,)):
            if r["m"]:
                users[r["m"]] = r["n"]
        out["used_by_module"] = users
    notes = [{"key": a["key"], "value": a["value"], "layer": a["layer"], "confidence": a["confidence"],
              "stale": bool(a["stale"])}
             for a in con.execute("SELECT * FROM annotations WHERE node_id = ?", (node_id,))]
    if notes:
        out["annotations"] = notes
    for side in ("out", "in"):
        if not out[side]:
            del out[side]
    return out


def search(con, query: str, kind: Optional[str] = None, limit: int = 20) -> dict:
    """Find nodes by name, qualified name or path."""
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    rows = []
    if tokens:
        match = " ".join(f'"{t}"*' for t in tokens)
        sql = ("SELECT n.* FROM search s JOIN nodes n ON n.id = s.node_id WHERE search MATCH ?"
               + (" AND n.kind = ?" if kind else "")
               + " ORDER BY bm25(search, 0, 10.0, 3.0, 1.0), length(n.id) LIMIT ?")
        try:
            rows = con.execute(sql, (match, kind, limit) if kind else (match, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        like = f"%{query}%"
        sql = ("SELECT * FROM nodes WHERE (name LIKE ? OR id LIKE ?)" + (" AND kind = ?" if kind else "")
               + " ORDER BY length(id) LIMIT ?")
        rows = con.execute(sql, (like, like, kind, limit) if kind else (like, like, limit)).fetchall()
    return {"query": query, "results": [_brief(r) for r in rows]}


def neighbors(con, node_id: str, direction: str = "both", kinds: Optional[list[str]] = None,
              limit: int = 100) -> dict:
    """Raw edges around a node, optionally filtered by edge kind. 'calls' is a kind here too."""
    if _node(con, node_id) is None:
        return {"error": f"No node with id {node_id!r}."}
    out: dict = {"id": node_id}
    for d in (("out", "in") if direction == "both" else (direction,)):
        groups = _edge_rows(con, node_id, d, limit)
        calls = _call_rows(con, node_id, d, limit)
        if calls["total"]:
            groups["calls"] = calls
        if kinds:
            groups = {k: v for k, v in groups.items() if k in kinds}
        out[d] = groups
    return out


def source(con, node_id: str, max_lines: int = 200) -> dict:
    """The source text of a node, read from the working tree it was indexed from."""
    row = _node(con, node_id)
    if row is None:
        return {"error": f"No node with id {node_id!r}."}
    if not row["path"] or not row["span_start"]:
        return {"error": f"{row['kind']} nodes have no source span."}
    root = con.execute("SELECT value FROM meta WHERE key = ?", (f"root:{row['repo_id']}",)).fetchone()
    path = Path(root[0]) / row["path"] if root else None
    if path is None or not path.is_file():
        return {"error": f"Source file not found for {row['path']}."}
    lines = path.read_text(errors="replace").splitlines()
    start, end = row["span_start"], row["span_end"] or row["span_start"]
    truncated = end - start + 1 > max_lines
    shown_end = start + max_lines - 1 if truncated else end
    return {"id": node_id, "path": row["path"], "lines": [start, end], "truncated": truncated,
            "text": "\n".join(lines[start - 1:shown_end])}
