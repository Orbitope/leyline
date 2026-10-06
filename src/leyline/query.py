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
    systems = systems_list(con)
    return {
        "repos": repos,
        "systems": systems,
        "module_edges": module_edges(con),
        "externals": externals,
        "coverage": coverage,
        "notes": ([] if systems else ["No systems: no module is large enough to be split."])
        + ["Call, type-use and inheritance edges are heuristic (tree-sitter). Import and containment edges are exact."],
    }


def _notes(con, node_id: str) -> dict:
    """Annotations on a node, keyed by annotation key. Intent wins over inferred."""
    out: dict = {}
    for a in con.execute("SELECT * FROM annotations WHERE node_id = ? ORDER BY layer = 'intent'", (node_id,)):
        out[a["key"]] = {"value": a["value"], "layer": a["layer"], "confidence": a["confidence"],
                         "stale": bool(a["stale"]), "source": a["source"]}
    return out


def systems_list(con) -> list[dict]:
    """Proposed and confirmed systems, with their members."""
    out = []
    for s in con.execute("SELECT * FROM nodes WHERE kind = 'system' ORDER BY parent_id, id"):
        a, notes = _attrs(s), _notes(con, s["id"])
        members = [r[0] for r in con.execute(
            "SELECT n.name FROM edges e JOIN nodes n ON n.id = e.dst_id WHERE e.kind = 'groups' AND e.src_id = ?"
            " ORDER BY n.name", (s["id"],))]
        out.append({"id": s["id"], "name": notes.get("name", {}).get("value", s["name"]),
                    "named": "name" in notes, "module": (s["parent_id"] or "").split(":module:")[-1],
                    "layer": s["layer"], "responsibility": notes.get("responsibility", {}).get("value"),
                    "cohesion": a.get("cohesion"), "members": members,
                    "stale": any(n["stale"] for n in notes.values())})
    return out


def annotate(con, node_id: str, key: str, value: str, evidence: Optional[list[str]] = None,
             confidence: Optional[float] = None, layer: str = "inferred", source: str = "mcp") -> dict:
    """Record an inferred or intent statement about a node. Inferred statements must cite evidence."""
    from . import store
    try:
        return store.annotate(con, node_id, key, value, layer, source, confidence, evidence or [])
    except ValueError as exc:
        return {"error": str(exc)}


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
    if row["kind"] == "system":
        out["members"] = [_brief(r) for r in con.execute(
            "SELECT n.* FROM edges e JOIN nodes n ON n.id = e.dst_id WHERE e.kind = 'groups' AND e.src_id = ?"
            " ORDER BY n.name", (node_id,))]
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


def flows(con, kind: Optional[str] = None, through: Optional[str] = None) -> dict:
    """Flows: paths walked from an entry point or a test. `through` keeps flows that pass a node."""
    sql = "SELECT f.* FROM flows f"
    args: list = []
    if through:
        sql += " WHERE f.id IN (SELECT flow_id FROM flow_steps WHERE callable_id = ?)"
        args.append(through)
    out = []
    for f in con.execute(sql + " ORDER BY f.name", args):
        a = json.loads(f["attrs"]) if f["attrs"] else {}
        if kind and a.get("kind") != kind:
            continue
        out.append({"id": f["id"], "name": f["name"], "origin": f["origin"], "kind": a.get("kind"),
                    "trigger": a.get("detail"), "steps": a.get("steps"), "truncated": a.get("truncated", False),
                    "modules": [m.split(":module:")[-1] for m in a.get("modules", [])], "entry": f["entry_id"]})
    return {"total": len(out), "flows": out,
            "note": "Static flows list each function once, in source order. They show what can run, not what did."}


def flow(con, flow_id: str, max_steps: int = 400) -> dict:
    """One flow, step by step. `depth` is the call depth and `parent` the step it was reached from."""
    f = con.execute("SELECT * FROM flows WHERE id = ?", (flow_id,)).fetchone()
    if f is None:
        return {"error": f"No flow with id {flow_id!r}. Use `flows` to list them."}
    steps = []
    for s in con.execute(
            "SELECT s.seq, s.depth, s.via, s.site_line, s.parent_seq, n.* FROM flow_steps s"
            " JOIN nodes n ON n.id = s.callable_id WHERE s.flow_id = ? ORDER BY s.seq LIMIT ?", (flow_id, max_steps)):
        a = _attrs(s)
        steps.append({"seq": s["seq"], "depth": s["depth"], "parent": s["parent_seq"], "via": s["via"],
                      "call_line": s["site_line"], "id": s["id"], "name": s["name"], "path": s["path"],
                      "lines": [s["span_start"], s["span_end"]], "signature": a.get("signature")})
    a = json.loads(f["attrs"]) if f["attrs"] else {}
    return {"id": f["id"], "name": f["name"], "origin": f["origin"], "kind": a.get("kind"),
            "truncated": a.get("truncated", False), "total_steps": a.get("steps"), "steps": steps}


def _adjacency(con, reverse: bool = False) -> dict:
    adj: dict[str, list] = {}
    a, b = ("dst_id", "src_id") if reverse else ("src_id", "dst_id")
    for r in con.execute(f"SELECT DISTINCT {a}, {b}, 'calls' FROM calls UNION"
                         f" SELECT {a}, {b}, 'communicates' FROM edges WHERE kind = 'communicates' UNION"
                         f" SELECT {b}, {a}, 'dispatch' FROM edges WHERE kind = 'overrides'"):
        adj.setdefault(r[0], []).append((r[1], r[2]))
    return adj


def trace(con, from_id: str, to_id: str, max_depth: int = 12) -> dict:
    """The shortest chain of calls (and channels) from one function to another."""
    for i in (from_id, to_id):
        if _node(con, i) is None:
            return {"error": f"No node with id {i!r}."}
    adj = _adjacency(con)
    prev: dict[str, tuple] = {from_id: (None, None)}
    frontier, depth = [from_id], 0
    while frontier and to_id not in prev and depth < max_depth:
        nxt = []
        for cur in frontier:
            for dst, via in adj.get(cur, ()):
                if dst not in prev:
                    prev[dst] = (cur, via)
                    nxt.append(dst)
        frontier, depth = nxt, depth + 1
    if to_id not in prev:
        return {"from": from_id, "to": to_id, "found": False,
                "note": "No static path. The link may run through a channel that was not analyzed."}
    path, cur = [], to_id
    while cur is not None:
        p, via = prev[cur]
        row = _node(con, cur)
        path.append({"id": cur, "name": row["name"], "path": row["path"], "via": via})
        cur = p
    return {"from": from_id, "to": to_id, "found": True, "hops": len(path) - 1, "path": path[::-1]}


def impact(con, node_id: str, max_depth: int = 6) -> dict:
    """Everything that can reach a node through calls and channels, grouped by module, plus the
    flows that pass through it. Use it before changing the node."""
    row = _node(con, node_id)
    if row is None:
        return {"error": f"No node with id {node_id!r}."}
    targets = {node_id} | {r[0] for r in con.execute(
        "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT n.id FROM nodes n JOIN d ON n.parent_id = d.id)"
        " SELECT id FROM d", (node_id,))}
    adj = _adjacency(con, reverse=True)
    dist: dict[str, int] = {t: 0 for t in targets}
    frontier, depth = list(targets), 0
    while frontier and depth < max_depth:
        nxt = []
        for cur in frontier:
            for src, _via in adj.get(cur, ()):
                if src not in dist:
                    dist[src] = depth + 1
                    nxt.append(src)
        frontier, depth = nxt, depth + 1
    reached = [i for i in dist if i not in targets]
    by_module: dict[str, dict] = {}
    home = con.execute("SELECT module_id FROM ancestry WHERE node_id = ?", (node_id,)).fetchone()
    for i in reached:
        m = con.execute("SELECT module_id FROM ancestry WHERE node_id = ?", (i,)).fetchone()
        mod = (m[0] if m and m[0] else "?")
        g = by_module.setdefault(mod, {"module": mod.split(":module:")[-1], "count": 0, "direct": []})
        g["count"] += 1
        if dist[i] == 1 and len(g["direct"]) < 15:
            g["direct"].append(i)
    marks = ",".join("?" * len(targets))
    through = [{"id": r["id"], "name": r["name"]} for r in con.execute(
        f"SELECT DISTINCT f.id, f.name FROM flows f JOIN flow_steps s ON s.flow_id = f.id"
        f" WHERE s.callable_id IN ({marks}) ORDER BY f.name", list(targets))]
    return {"id": node_id, "reached_by": len(reached), "depth_limit": max_depth,
            "crosses_module_boundary": any(m != (home[0] if home else None) for m in by_module),
            "by_module": sorted(by_module.values(), key=lambda g: -g["count"]),
            "flows_through": {"total": len(through), "items": through[:40]},
            "note": "Callers found from syntax. Code reached only through outside frameworks is not counted."}


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
