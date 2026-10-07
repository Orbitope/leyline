"""Export a store as one self-contained viewer page (or as the JSON the viewer reads)."""

from __future__ import annotations

import html
import json
from importlib import resources
from pathlib import Path

KEEP_ATTRS = ("framework", "runner", "native_kind", "visibility", "signature", "declared_type", "trigger", "is_static",
              "is_abstract", "marker", "ecosystem", "category", "also_in", "namespace", "version",
              "target_framework", "url")

# The role that names a pattern instance: a strategy is named after its abstraction, not its context.
PRIMARY_ROLE = {"strategy": "strategy", "decorator": "decorator", "composite": "composite", "template method": "template",
                "observer": "subject", "factory": "factory", "builder": "builder", "singleton": "singleton",
                "process boundary": "launcher"}

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>html{{color-scheme:light}}body{{margin:0;font:14px system-ui,sans-serif}}[hidden]{{display:none!important}}</style>
</head>
<body>
{fragment}
</body>
</html>
"""


def graph(con, with_sources: bool = True) -> dict:
    """The whole store in the compact shape the viewer loads."""
    rows = con.execute("SELECT * FROM nodes ORDER BY kind = 'repo' DESC, id").fetchall()
    index = {r["id"]: i for i, r in enumerate(rows)}
    nodes = []
    module_names: dict[str, int] = {}
    for r in rows:
        if r["kind"] == "module":
            module_names[r["name"]] = module_names.get(r["name"], 0) + 1
    for r in rows:
        attrs = json.loads(r["attrs"]) if r["attrs"] else {}
        n = {"i": r["id"], "k": r["kind"], "n": r["name"]}
        if r["kind"] == "module" and module_names[r["name"]] > 1:
            # Two modules with one name (src/Shared, test/Shared): show the path to tell them apart.
            n["n"] = r["path"] if r["path"] not in (None, "", ".") else "(repository root)"
        if r["parent_id"] in index:
            n["p"] = index[r["parent_id"]]
        if r["language"]:
            n["l"] = r["language"]
        if r["path"] is not None and r["kind"] != "repo":
            n["f"] = r["path"]
        if r["span_start"]:
            n["a"], n["b"] = r["span_start"], r["span_end"]
        x = {k: attrs[k] for k in KEEP_ATTRS if attrs.get(k) not in (None, "", [], False)}
        if r["kind"] == "system":
            x.update({k: attrs[k] for k in ("members", "cohesion", "top") if k in attrs})
            x["layer"] = r["layer"]
        if x:
            n["x"] = x
        nodes.append(n)
    rank = {"exact": 2, "observed": 2, "heuristic": 1, "guess": 0}
    edges = []
    access = []  # [function, field, 1 for an assignment, times, line, 1 if only while creating, 1 if guessed]
    for e in con.execute("SELECT kind, src_id, dst_id, precision, attrs FROM edges WHERE kind != 'contains'"):
        if e["src_id"] in index and e["dst_id"] in index:
            attrs = json.loads(e["attrs"]) if e["attrs"] else {}
            if e["kind"] in ("reads", "writes"):
                access.append([index[e["src_id"]], index[e["dst_id"]], int(e["kind"] == "writes"), attrs.get("n", 1),
                               attrs.get("line") or 0, int(bool(attrs.get("init"))), int(e["precision"] == "guess")])
                continue
            extra = attrs.get("role") or ""
            if e["kind"] == "communicates":
                extra = {"channel": attrs.get("channel"), "address": attrs.get("address"),
                         "handler": attrs.get("handler"), "pipes": attrs.get("pipes")}
            edges.append([e["kind"], index[e["src_id"]], index[e["dst_id"]], rank.get(e["precision"], 1), extra])
    calls = []
    for c in con.execute(
            "SELECT src_id, dst_id, COUNT(*) AS n, MIN(site_start) AS line,"
            " MIN(CASE precision WHEN 'exact' THEN 2 WHEN 'guess' THEN 0 ELSE 1 END) AS rank"
            " FROM calls GROUP BY src_id, dst_id ORDER BY dst_id, src_id"):
        if c["src_id"] in index and c["dst_id"] in index:
            calls.append([index[c["src_id"]], index[c["dst_id"]], c["n"], c["line"], c["rank"]])
    flows = []
    for f in con.execute("SELECT * FROM flows ORDER BY name"):
        a = json.loads(f["attrs"]) if f["attrs"] else {}
        steps = [[index[s["callable_id"]], s["depth"], s["via"], s["site_line"] or 0]
                 for s in con.execute("SELECT * FROM flow_steps WHERE flow_id = ? ORDER BY seq", (f["id"],))
                 if s["callable_id"] in index]
        if f["entry_id"] in index:
            flows.append({"id": f["id"], "name": f["name"], "kind": a.get("kind"), "detail": a.get("detail"),
                          "truncated": a.get("truncated", False), "entry": index[f["entry_id"]], "steps": steps})
    coverage = [{"repo": r["repo_id"], "extractor": r["extractor"], "status": r["status"],
                 "stats": json.loads(r["stats"]) if r["stats"] else {}}
                for r in con.execute("SELECT * FROM extractor_coverage ORDER BY extractor")]
    notes: dict[int, dict] = {}
    for a in con.execute("SELECT * FROM annotations ORDER BY layer = 'intent'"):
        if a["node_id"] in index:
            notes.setdefault(index[a["node_id"]], {})[a["key"]] = {
                "v": a["value"], "layer": a["layer"], "c": a["confidence"], "stale": bool(a["stale"])}
    views = []
    try:
        for v in con.execute("SELECT * FROM views ORDER BY created DESC"):
            spec = json.loads(v["spec"])
            spec["marks"] = [{**m, "i": index[m["id"]]} for m in spec.get("marks", []) if m["id"] in index]
            for key in ("tests_to_run", "entry_points_affected", "untested"):
                for item in spec.get(key, []):
                    item["i"] = index.get(item.get("id"))
            for c in spec.get("channels", []):
                c["fi"], c["ti"] = index.get(c.get("from")), index.get(c.get("to"))
            for g in spec.get("by_module", []) + spec.get("by_system", []):
                g["i"] = index.get(g.get("id"))
            for nn in spec.get("new_nodes", []):
                nn["pi"] = index.get(nn.get("parent"))
            def attach(x):
                # Give every record in a review that names a node the node's index.
                if isinstance(x, dict):
                    for key, out in (("id", "i"), ("from_id", "fi"), ("to_id", "ti")):
                        if isinstance(x.get(key), str):
                            x[out] = index.get(x[key])
                    for value in x.values():
                        attach(value)
                elif isinstance(x, list):
                    for value in x:
                        attach(value)
            attach(spec.get("review"))
            views.append({"id": v["id"], "title": v["title"], "kind": v["kind"], "created": v["created"], **spec})
    except Exception:  # a store written before views existed
        views = []
    pattern_list = []
    try:
        from . import patterns
        for x in patterns.listing(con, include_tests=True, limit=2000)["patterns"]:
            lead = PRIMARY_ROLE.get(x["pattern"])
            marks = [{"i": index[n["id"]], "role": role}
                     for role, ns in sorted(x["roles"].items(), key=lambda kv: (kv[0] != lead, kv[0]))
                     for n in ns if n["id"] in index]
            if marks:
                pattern_list.append({"id": x["id"], "pattern": x["pattern"], "about": x["about"], "rationale": x["rationale"],
                                     "c": x["confidence"], "source": x["source"], "stale": x["stale"], "tests": x["in_tests"],
                                     "marks": marks})
    except Exception:  # a store written before patterns existed
        pattern_list = []
    from . import query
    state = [{"i": index[f["id"]], "from": f["written_from"], "w": f["writers"], "r": f["readers"]}
             for f in query.shared_state(con, limit=60)["fields"] if f["id"] in index]
    measured = {"t": {}, "any": [], "watched": []}
    try:
        from . import coverage as cov_store
        if cov_store.has(con):
            per = {}
            for r in con.execute("SELECT DISTINCT test_id, node_id FROM covered WHERE test_id IS NOT NULL"):
                if r["test_id"] in index and r["node_id"] in index:
                    per.setdefault(index[r["test_id"]], []).append(index[r["node_id"]])
            measured["t"] = per
            measured["any"] = sorted(index[i] for i in cov_store.ran(con) if i in index)
            measured["watched"] = sorted(index[r[0]] for r in con.execute(
                "SELECT DISTINCT a.file_id FROM covered c JOIN ancestry a ON a.node_id = c.node_id") if r[0] in index)
    except Exception:
        pass
    tour_list = []
    try:
        from . import tours
        for t in tours.listing(con)["tours"]:
            full = tours.get(con, t["id"])
            tour_list.append({"id": t["id"], "title": t["title"], "audience": t["audience"], "source": t["source"],
                              "stops": [{"title": st["title"], "kind": st["kind"], "ref": st["ref"],
                                         "i": index.get(st["ref"]), "text": st["narrative"]} for st in full["stops"] if st["exists"]]})
    except Exception:  # a store written before tours existed
        tour_list = []
    repos = [{"id": r["id"], "commit": r["commit_sha"], **(json.loads(r["attrs"]) if r["attrs"] else {})}
             for r in rows if r["kind"] == "repo"]
    sources = {}
    if with_sources:
        roots = {k.split(":", 1)[1]: v for k, v in con.execute("SELECT key, value FROM meta WHERE key LIKE 'root:%'")}
        for r in rows:
            if r["kind"] == "file" and r["repo_id"] in roots:
                p = Path(roots[r["repo_id"]]) / r["path"]
                if p.is_file():
                    sources[r["path"]] = p.read_text(errors="replace")
    return {"version": 2, "repos": repos, "nodes": nodes, "edges": edges, "calls": calls, "flows": flows, "notes": notes, "views": views, "patterns": pattern_list, "tours": tour_list, "access": access, "state": state, "measured": measured,
            "coverage": coverage, "sources": sources}


def fragment(con, with_sources: bool = True) -> str:
    """The viewer with data embedded, without an html/head/body wrapper."""
    template = resources.files("leyline").joinpath("viewer/viewer.html").read_text()
    data = json.dumps(graph(con, with_sources), separators=(",", ":")).replace("</", "<\\/")
    name = next((r[0] for r in con.execute("SELECT name FROM nodes WHERE kind = 'repo' ORDER BY id")), "repo")
    return template.replace("__LEYLINE_REPO__", html.escape(name)).replace("__LEYLINE_DATA__", data)


def page(con, with_sources: bool = True) -> str:
    return PAGE.format(fragment=fragment(con, with_sources))
