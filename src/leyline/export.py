"""Export a store as one self-contained viewer page (or as the JSON the viewer reads)."""

from __future__ import annotations

import html
import json
from importlib import resources
from pathlib import Path

KEEP_ATTRS = ("framework", "runner", "native_kind", "visibility", "signature", "declared_type", "trigger", "is_static",
              "is_abstract", "marker", "ecosystem", "category", "also_in", "namespace", "version",
              "target_framework", "url")

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
    for r in rows:
        attrs = json.loads(r["attrs"]) if r["attrs"] else {}
        n = {"i": r["id"], "k": r["kind"], "n": r["name"]}
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
    for e in con.execute("SELECT kind, src_id, dst_id, precision, attrs FROM edges WHERE kind != 'contains'"):
        if e["src_id"] in index and e["dst_id"] in index:
            attrs = json.loads(e["attrs"]) if e["attrs"] else {}
            extra = attrs.get("role") or ""
            if e["kind"] == "communicates":
                extra = {"channel": attrs.get("channel"), "address": attrs.get("address"),
                         "handler": attrs.get("handler"), "pipes": attrs.get("pipes")}
            edges.append([e["kind"], index[e["src_id"]], index[e["dst_id"]], rank.get(e["precision"], 1), extra])
    calls = []
    for c in con.execute(
            "SELECT src_id, dst_id, COUNT(*) AS n, MIN(site_start) AS line,"
            " MIN(CASE precision WHEN 'exact' THEN 2 WHEN 'guess' THEN 0 ELSE 1 END) AS rank"
            " FROM calls GROUP BY src_id, dst_id"):
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
    return {"version": 2, "repos": repos, "nodes": nodes, "edges": edges, "calls": calls, "flows": flows, "notes": notes, "views": views,
            "coverage": coverage, "sources": sources}


def fragment(con, with_sources: bool = True) -> str:
    """The viewer with data embedded, without an html/head/body wrapper."""
    template = resources.files("leyline").joinpath("viewer/viewer.html").read_text()
    data = json.dumps(graph(con, with_sources), separators=(",", ":")).replace("</", "<\\/")
    name = next((r[0] for r in con.execute("SELECT name FROM nodes WHERE kind = 'repo' ORDER BY id")), "repo")
    return template.replace("__LEYLINE_REPO__", html.escape(name)).replace("__LEYLINE_DATA__", data)


def page(con, with_sources: bool = True) -> str:
    return PAGE.format(fragment=fragment(con, with_sources))
