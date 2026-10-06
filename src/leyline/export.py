"""Export a store as one self-contained viewer page (or as the JSON the viewer reads)."""

from __future__ import annotations

import html
import json
from importlib import resources
from pathlib import Path

KEEP_ATTRS = ("native_kind", "visibility", "signature", "declared_type", "trigger", "is_static",
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
        if x:
            n["x"] = x
        nodes.append(n)
    edges = []
    for e in con.execute("SELECT kind, src_id, dst_id, precision, attrs FROM edges WHERE kind != 'contains'"):
        if e["src_id"] in index and e["dst_id"] in index:
            role = (json.loads(e["attrs"]) if e["attrs"] else {}).get("role")
            edges.append([e["kind"], index[e["src_id"]], index[e["dst_id"]],
                          1 if e["precision"] == "exact" else 0, role or ""])
    calls = []
    for c in con.execute(
            "SELECT src_id, dst_id, COUNT(*) AS n, MIN(site_start) AS line, MAX(precision = 'exact') AS exact"
            " FROM calls GROUP BY src_id, dst_id"):
        if c["src_id"] in index and c["dst_id"] in index:
            calls.append([index[c["src_id"]], index[c["dst_id"]], c["n"], c["line"], c["exact"]])
    coverage = [{"repo": r["repo_id"], "extractor": r["extractor"], "status": r["status"],
                 "stats": json.loads(r["stats"]) if r["stats"] else {}}
                for r in con.execute("SELECT * FROM extractor_coverage ORDER BY extractor")]
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
    return {"version": 1, "repos": repos, "nodes": nodes, "edges": edges, "calls": calls,
            "coverage": coverage, "sources": sources}


def fragment(con, with_sources: bool = True) -> str:
    """The viewer with data embedded, without an html/head/body wrapper."""
    template = resources.files("leyline").joinpath("viewer/viewer.html").read_text()
    data = json.dumps(graph(con, with_sources), separators=(",", ":")).replace("</", "<\\/")
    name = next((r[0] for r in con.execute("SELECT name FROM nodes WHERE kind = 'repo' ORDER BY id")), "repo")
    return template.replace("__LEYLINE_REPO__", html.escape(name)).replace("__LEYLINE_DATA__", data)


def page(con, with_sources: bool = True) -> str:
    return PAGE.format(fragment=fragment(con, with_sources))
