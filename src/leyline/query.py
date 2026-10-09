"""The query layer. The MCP server, the CLI and the viewer all call these functions."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
import re
import sqlite3
from pathlib import Path
from typing import Optional
from . import store

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


def cross_repo(con, limit: int = 20) -> dict:
    """Links between the repositories of a workspace: calls, imports, type use and inheritance whose two
    ends are in different repos, the functions most called across, and the flows that cross."""
    pairs: dict[tuple, dict] = {}
    for r in con.execute("SELECT s.repo_id AS a, d.repo_id AS b, COUNT(*) AS n FROM calls c JOIN nodes s ON s.id = c.src_id"
                         " JOIN nodes d ON d.id = c.dst_id WHERE s.repo_id != d.repo_id GROUP BY a, b"):
        pairs.setdefault((r["a"], r["b"]), {})["calls"] = r["n"]
    for r in con.execute("SELECT s.repo_id AS a, d.repo_id AS b, e.kind AS k, COUNT(*) AS n FROM edges e"
                         " JOIN nodes s ON s.id = e.src_id JOIN nodes d ON d.id = e.dst_id"
                         " WHERE s.repo_id != d.repo_id AND e.kind != 'contains' GROUP BY a, b, k"):
        pairs.setdefault((r["a"], r["b"]), {})[r["k"]] = r["n"]
    called = [{"id": r["dst_id"], "name": r["name"], "repo": r["repo_id"], "callers": r["n"]} for r in con.execute(
        "SELECT c.dst_id, d.name, d.repo_id, COUNT(DISTINCT c.src_id) AS n FROM calls c JOIN nodes s ON s.id = c.src_id"
        " JOIN nodes d ON d.id = c.dst_id WHERE s.repo_id != d.repo_id GROUP BY c.dst_id ORDER BY n DESC, c.dst_id LIMIT ?", (limit,))]
    # A flow crosses when a step is in another repo than its entry, and comes back when a step in the
    # entry's repo was reached from a step in another (a framework calling the code that uses it).
    crossing = returning = 0
    if pairs:
        rows = con.execute(
            "SELECT f.id AS flow, e.repo_id AS home, s.seq, s.parent_seq, n.repo_id AS repo FROM flows f"
            " JOIN nodes e ON e.id = f.entry_id JOIN flow_steps s ON s.flow_id = f.id JOIN nodes n ON n.id = s.callable_id"
            " WHERE f.layer = 'fact' ORDER BY f.id, s.seq")
        cur, repo_at, crossed, back = None, {}, False, False
        for r in rows:
            if r["flow"] != cur:
                crossing, returning = crossing + crossed, returning + back
                cur, repo_at, crossed, back = r["flow"], {}, False, False
            repo_at[r["seq"]] = r["repo"]
            crossed = crossed or r["repo"] != r["home"]
            back = back or (r["repo"] == r["home"] and repo_at.get(r["parent_seq"], r["home"]) != r["home"])
        crossing, returning = crossing + crossed, returning + back
    return {"repos": [r[0] for r in con.execute("SELECT id FROM nodes WHERE kind = 'repo' ORDER BY id")],
            "pairs": [{"from": a, "to": b, **kinds, "total": sum(kinds.values())} for (a, b), kinds in sorted(pairs.items())],
            "most_called_across": called,
            "flows_crossing": crossing, "flows_crossing_and_back": returning}


def overview(con) -> dict:
    """The top-level map: repos, modules, how modules depend on each other, and what was analyzed."""
    repos = []
    for repo in con.execute("SELECT * FROM nodes WHERE kind = 'repo' ORDER BY id"):
        modules = []
        for m in con.execute("SELECT * FROM nodes WHERE kind = 'module' AND repo_id = ? ORDER BY path", (repo["id"],)):
            counts = {r["kind"]: r["n"] for r in con.execute(
                "SELECT n.kind, COUNT(*) AS n FROM ancestry a JOIN nodes n ON n.id = a.node_id"
                " WHERE a.module_id = ? AND n.id != ? GROUP BY n.kind", (m["id"], m["id"]))}
            # `+kind` keeps SQLite on the parent index: through the kind index it read every file of the repository
            # for each module (70 s on Kubernetes).
            files = con.execute(
                "SELECT language, COUNT(*) AS n, SUM(span_end) AS loc FROM nodes"
                " WHERE +kind = 'file' AND parent_id = ? GROUP BY language", (m["id"],)).fetchall()
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
    ws = con.execute("SELECT value FROM meta WHERE key = 'workspace'").fetchone()
    from .outline import named_parts   # names given to modules and their parts (module_outline, name_part)
    named = named_parts(con)
    for r in repos:
        for m in r["modules"]:
            if m["id"] in named:
                m["title"] = named[m["id"]]["name"]
                if named[m["id"]]["summary"]:
                    m["summary"] = named[m["id"]]["summary"]
    return {
        "repos": repos,
        **({"named_parts": [x for k, x in named.items() if x["kind"] != "module"]} if named else {}),
        # Repositories indexed together: names resolve across them, and these are the links between them.
        **({"workspace": {"repos": json.loads(ws[0]), "links": cross_repo(con)["pairs"]}} if ws else {}),
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
    data = _data_access(con, row)
    if data:
        out["data"] = data
    if row["kind"] in ("type", "callable", "field"):
        from . import patterns
        try:
            labels = patterns.listing(con, node_id=node_id, limit=10)["patterns"]
        except Exception:  # a store written before patterns existed
            labels = []
        if labels:
            out["patterns"] = [{"pattern": x["pattern"], "confidence": x["confidence"], "rationale": x["rationale"],
                                "roles_here": sorted(role for role, ns in x["roles"].items()
                                                     if any(n["id"] == node_id or n["id"].startswith(node_id + ".") for n in ns))}
                               for x in labels]
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
    # What the text names exactly comes first: the id, a file at that path (or ending in it), a node of that
    # name, or Owner.name. Ranking by words alone put local.ts 19th behind files that only mention "local".
    q = query.strip()
    exact = []
    if q:
        exact = con.execute(
            "SELECT * FROM nodes WHERE (id = ? OR name = ? OR (kind IN ('file', 'module') AND (path = ? OR path LIKE ? ESCAPE '\\')))"
            + (" AND kind = ?" if kind else "") + " ORDER BY id = ? DESC, path = ? DESC, name = ? DESC,"
            " kind IN ('file', 'type', 'callable') DESC, length(id) LIMIT ?",
            (q, q, q, "%/" + re.sub(r"([\\%_])", r"\\\1", q), *([kind] if kind else []), q, q, q, limit)).fetchall()
        if "." in q and not exact:
            owner, _, leaf = q.rpartition(".")
            exact = [r for r in con.execute("SELECT n.* FROM nodes n JOIN nodes p ON p.id = n.parent_id WHERE n.name = ?"
                                            " AND p.name = ?" + (" AND n.kind = ?" if kind else "") + " LIMIT ?",
                                            (leaf, owner.rsplit(".", 1)[-1], *([kind] if kind else []), limit))]
    first = {r["id"] for r in exact}
    rows = (exact + [r for r in rows if r["id"] not in first])[:limit]
    return {"query": query, "results": [_brief(r) for r in rows]}


def resolve(con, text: str) -> dict:
    """A node id, or the node a person means by a name: `Owner.method`, `method`, a file path. {"id": ...} when it is
    one thing; otherwise {"error": ..., "candidates": [...]}."""
    text = text.strip()
    if _node(con, text) is not None:
        return {"id": text}
    if "/" in text or re.search(r"\.\w{1,5}$", text) and con.execute(
            "SELECT 1 FROM nodes WHERE kind = 'file' AND (path = ? OR path LIKE ?) LIMIT 1", (text, "%/" + text)).fetchone():
        rows = con.execute("SELECT * FROM nodes WHERE kind = 'file' AND (path = ? OR path LIKE ?) ORDER BY length(path)",
                           (text, "%/" + text)).fetchall()
    else:
        parts = text.split(".")
        rows = [r for r in con.execute("SELECT * FROM nodes WHERE name = ? AND kind IN ('callable', 'type', 'field', 'test',"
                                       " 'module', 'system', 'entry_point') ORDER BY length(id)", (parts[-1],))]
        for depth, owner in enumerate(reversed(parts[:-1])):   # each written owner must be the next one up
            keep = []
            for r in rows:
                cur = r
                for _ in range(depth + 1):
                    cur = _node(con, cur["parent_id"]) if cur and cur["parent_id"] else None
                if cur is not None and cur["name"] == owner:
                    keep.append(r)
            rows = keep
    exact = [r for r in rows if r["path"] == text] or rows
    if len(exact) == 1:
        return {"id": exact[0]["id"]}
    if not exact:
        hits = search(con, text, limit=8)["results"]
        return {"error": f"nothing on the map is called {text!r}", "candidates": hits}
    return {"error": f"{text!r} could be {len(exact)} things; give the id, or write it as Owner.name",
            "candidates": [_brief(r) for r in exact[:20]]}


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


def impact(con, node_id: str, max_depth: int = 6, flows_limit: int = 40) -> dict:
    """Everything that can reach a node through calls and channels, grouped by module, plus the
    flows that pass through it (the first `flows_limit`, and their total). Use it before changing the node."""
    row = _node(con, node_id)
    if row is None:
        return {"error": f"No node with id {node_id!r}."}
    targets = {node_id} | {r[0] for r in con.execute(
        "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT n.id FROM nodes n JOIN d ON n.parent_id = d.id)"
        " SELECT id FROM d", (node_id,))}
    adj = _adjacency(con, reverse=True)
    from .change import enclosing
    shape = {r[0]: {"kind": r[1], "parent_id": r[2]} for r in con.execute("SELECT id, kind, parent_id FROM nodes")}
    dist: dict[str, int] = {t: 0 for t in targets}

    def lift(found, d):
        # A nested function runs when the function it is defined in runs, so that function's callers reach it.
        more = []
        for i in found:
            for e in enclosing(shape, i):
                if e in dist:
                    break
                dist[e] = d
                more.append(e)
        return found + more

    frontier, depth = lift(list(targets), 0), 0
    while frontier and depth < max_depth:
        nxt = []
        for cur in frontier:
            for src, _via in adj.get(cur, ()):
                if src not in dist:
                    dist[src] = depth + 1
                    nxt.append(src)
        frontier, depth = lift(nxt, depth + 1), depth + 1
    reached = [i for i in dist if i not in targets]
    by_module: dict[str, dict] = {}
    home = con.execute("SELECT module_id FROM ancestry WHERE node_id = ?", (node_id,)).fetchone()
    for i in reached:
        m = con.execute("SELECT module_id FROM ancestry WHERE node_id = ?", (i,)).fetchone()
        mod = (m[0] if m and m[0] else "?")
        g = by_module.setdefault(mod, {"module": mod.split(":module:")[-1], "repo": mod.split(":", 1)[0], "count": 0,
                                       "direct": [], "direct_total": 0})
        g["count"] += 1
        if dist[i] == 1:
            g["direct_total"] += 1
            if len(g["direct"]) < 15:
                g["direct"].append(i)
    marks = ",".join("?" * len(targets))
    through = [{"id": r["id"], "name": r["name"]} for r in con.execute(
        f"SELECT DISTINCT f.id, f.name FROM flows f JOIN flow_steps s ON s.flow_id = f.id"
        f" WHERE s.callable_id IN ({marks}) ORDER BY f.name", list(targets))]
    by_repo: dict[str, int] = defaultdict(int)
    for g in by_module.values():
        by_repo[g["repo"]] += g["count"]
    return {"id": node_id, "reached_by": len(reached), "depth_limit": max_depth,
            "crosses_module_boundary": any(m != (home[0] if home else None) for m in by_module),
            "by_repo": dict(by_repo), "crosses_repo_boundary": any(r != row["repo_id"] for r in by_repo),
            "by_module": sorted(by_module.values(), key=lambda g: -g["count"]),
            "flows_through": {"total": len(through), "items": through[:flows_limit]},
            "note": "Callers found from syntax. Code reached only through outside frameworks is not counted."}


def source(con, node_id: str, max_lines: int = 200) -> dict:
    """The source text of a node, read from the working tree it was indexed from."""
    row = _node(con, node_id)
    if row is None:
        return {"error": f"No node with id {node_id!r}."}
    if not row["path"] or not row["span_start"]:
        return {"error": f"{row['kind']} nodes have no source span."}
    root = store.roots(con).get(row["repo_id"])
    path = root / row["path"] if root else None
    if path is None or not path.is_file() or not store.inside(path, root):   # a link out, checked out since the map
        return {"error": f"Source file not found for {row['path']}."}
    from .indexer import source_lines
    try:
        lines = source_lines(path)
    except OSError as e:
        return {"error": f"Cannot read {row['path']}: {e.strerror or e}."}
    start, end = row["span_start"], row["span_end"] or row["span_start"]
    truncated = end - start + 1 > max_lines
    shown_end = start + max_lines - 1 if truncated else end
    return {"id": node_id, "path": row["path"], "lines": [start, end], "truncated": truncated,
            "text": "\n".join(lines[start - 1:shown_end])}


def _owner_type(con, node_id: str) -> Optional[str]:
    row = con.execute("SELECT parent_id FROM nodes WHERE id = ?", (node_id,)).fetchone()
    while row and row[0]:
        p = con.execute("SELECT id, kind, parent_id FROM nodes WHERE id = ?", (row[0],)).fetchone()
        if p is None:
            return None
        if p["kind"] == "type":
            return p["id"]
        row = (p["parent_id"],)
    return None


def _data_access(con, row) -> dict:
    """Which functions read and assign a field; which fields a function touches; a type's fields at a glance."""
    def users(field_id):
        r, w = [], []
        for e in con.execute("SELECT e.kind, e.src_id, e.precision, e.attrs, n.name FROM edges e JOIN nodes n ON n.id = e.src_id"
                             " WHERE e.dst_id = ? AND e.kind IN ('reads', 'writes') ORDER BY e.src_id", (field_id,)):
            a = json.loads(e["attrs"] or "{}")
            item = {"id": e["src_id"], "name": e["name"], "times": a.get("n", 1), "line": a.get("line"),
                    **({"guessed": True} if e["precision"] == "guess" else {}),
                    **({"only_when_creating": True} if a.get("init") else {})}
            (w if e["kind"] == "writes" else r).append(item)
        return r, w
    if row["kind"] == "field":
        r, w = users(row["id"])
        own = _owner_type(con, row["id"])
        outside = [x for x in w if _owner_type(con, x["id"]) != own]
        return {"read_by": r, "written_by": w, "written_outside_its_type": len(outside)} if (r or w) else {}
    if row["kind"] in ("callable", "test"):
        out = {"reads": [], "writes": []}
        for e in con.execute("SELECT e.kind, e.dst_id, e.precision, n.name, p.name AS owner FROM edges e JOIN nodes n ON n.id = e.dst_id"
                             " LEFT JOIN nodes p ON p.id = n.parent_id WHERE e.src_id = ? AND e.kind IN ('reads', 'writes')"
                             " ORDER BY e.dst_id", (row["id"],)):
            out[e["kind"]].append({"id": e["dst_id"], "name": f"{e['owner']}.{e['name']}" if e["owner"] else e["name"],
                                   **({"guessed": True} if e["precision"] == "guess" else {})})
        return out if (out["reads"] or out["writes"]) else {}
    if row["kind"] == "type":
        fields = []
        for f in con.execute("SELECT id, name FROM nodes WHERE parent_id = ? AND kind = 'field' ORDER BY name", (row["id"],)):
            r, w = users(f["id"])
            if not (r or w):
                continue
            outside = sorted({_owner_type(con, x["id"]) or x["id"] for x in w} - {row["id"]})
            fields.append({"id": f["id"], "name": f["name"], "readers": len(r), "writers": len(w),
                           "written_from": [o.split("::")[-1].split(":")[-1] for o in outside]})
        return {"fields": fields} if fields else {}
    return {}


def shared_state(con, scope: Optional[str] = None, limit: int = 40, guesses: bool = True) -> dict:
    """Fields assigned from outside the type that declares them, most widely written first.
    `scope` narrows to a module, type or path prefix of the field's id. `guesses=False` leaves out links found only
    by a unique field name."""
    share = {}   # one string for each kind and module, not one per row (hundreds of MB on a large repository)
    share = share.setdefault
    parent = {r[0]: (r[1], share(r[2], r[2])) for r in con.execute("SELECT id, parent_id, kind FROM nodes")}
    module = {r[0]: share(r[1], r[1]) for r in con.execute("SELECT node_id, module_id FROM ancestry")}
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM nodes")}
    tests = {r[0] for r in con.execute("SELECT entry_id FROM flows WHERE json_extract(attrs, '$.kind') = 'test'")}
    test_mods = {module.get(t) for t in tests}

    def owner(i):
        cur = parent.get(i, (None, None))[0]
        while cur and parent.get(cur, (None, None))[1] != "type":
            cur = parent.get(cur, (None, None))[0]
        return cur
    supers = defaultdict(set)
    for e in con.execute("SELECT src_id, dst_id FROM edges WHERE kind IN ('extends', 'implements')"):
        supers[e["src_id"]].add(e["dst_id"])

    def is_a(t, base, seen=None):  # a subclass assigning an inherited field is still inside the type
        seen = seen or set()
        return t == base or any(b not in seen and not seen.add(b) and is_a(b, base, seen) for b in supers.get(t, ()))
    writers, readers = defaultdict(set), defaultdict(set)
    for e in con.execute("SELECT kind, src_id, dst_id, attrs FROM edges WHERE kind IN ('reads', 'writes')"
                         + ("" if guesses else " AND COALESCE(precision, '') != 'guess'")):
        if e["kind"] == "writes" and json.loads(e["attrs"] or "{}").get("init"):
            continue  # filling in a new object is construction, not a change to shared state
        if e["kind"] == "writes" and names.get(e["src_id"]) in (".ctor", "__init__") and owner(e["src_id"]) == owner(e["dst_id"]):
            continue
        (writers if e["kind"] == "writes" else readers)[e["dst_id"]].add(e["src_id"])
    rows = []
    for f, ws in writers.items():
        if scope and not (f.startswith(scope) or module.get(f) == scope):
            continue
        own = owner(f)
        product = {w for w in ws if module.get(w) not in test_mods}
        outside_types = sorted(t for t in {owner(w) or w for w in product} if not is_a(t, own))
        if not outside_types:
            continue
        rows.append({"id": f, "name": f"{names.get(own, '?')}.{names.get(f, f)}", "module": names.get(module.get(f), ""),
                     "writers": len(product), "written_from": [names.get(t, t) for t in outside_types],
                     "writer_modules": sorted({names.get(module.get(w), "") for w in product}),
                     "readers": len(readers.get(f, ()))})
    # Two fields can read the same (a Builder class in each of four scripts): name the file of each such one.
    seen = Counter(r["name"] for r in rows)
    for r in rows:
        if seen[r["name"]] > 1:
            path = con.execute("SELECT path FROM nodes WHERE id = ?", (r["id"],)).fetchone()
            r["path"] = path[0] if path else ""
            r["name"] += f" ({r['path']})"
    rows.sort(key=lambda r: (-len(r["written_from"]), -len(r["writer_modules"]), -r["writers"], r["name"]))
    return {"total": len(rows), "fields": rows[:limit],
            "note": "A field many types assign has no single place that keeps it valid. Test code, constructors and values set "
                    "while creating an object (new Foo { a = 1 }) are not counted. "
                    "A call that changes a collection in place (list.Add, items.append) counts as an assignment; any "
                    "other method called on the field does not, since the map cannot tell what it does."}
