"""Export a store as one self-contained viewer page (or as the JSON the viewer reads)."""

from __future__ import annotations

import base64
import gzip
import html
import json
import re
from importlib import resources
from pathlib import Path
from typing import Optional

from . import store

KEEP_ATTRS = ("framework", "runner", "native_kind", "visibility", "signature", "declared_type", "trigger", "is_static",
              "is_abstract", "marker", "ecosystem", "category", "also_in", "namespace", "version",
              "target_framework", "url", "is_test")

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


def remote_url(url: Optional[str]) -> Optional[str]:
    """A git remote that is a URL (https://, ssh://, git@host:path), or None for a path on this machine."""
    ok = url and not url.lower().startswith("file:") and re.match(r"^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:)", url, re.I)
    return url if ok else None


def graph(con, with_sources: bool = True, memory: Optional[Path] = None) -> dict:
    """The whole store in the compact shape the viewer loads. `memory` is the file that remembers where boxes
    were drawn last time (see `layout`); without it the layout is worked out afresh, the same way each time."""
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
        # A step is [node, depth] or [node, depth, how it was reached] when that is not a plain call.
        steps = [[index[s["callable_id"]], s["depth"]] + ([s["via"]] if s["via"] not in (None, "calls") else [])
                 for s in con.execute("SELECT callable_id, depth, via FROM flow_steps WHERE flow_id = ? ORDER BY seq", (f["id"],))
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
            views.append({"id": v["id"], "title": v["title"], "kind": v["kind"], "created": v["created"],
                          "change_id": v["change_id"], **spec})
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
    sources = {}
    roots = {k: str(v) for k, v in store.roots(con).items()}
    # The folder mapped is where the code is; git's origin is shown apart, and only when it is a URL (a clone of a
    # local folder has that folder as its origin, which is not this one).
    repos = [{"id": r["id"], "commit": r["commit_sha"], **(json.loads(r["attrs"]) if r["attrs"] else {}), "folder": roots.get(r["id"], "")}
             for r in rows if r["kind"] == "repo"]
    for r in repos:
        if not remote_url(r.get("url")):
            r.pop("url", None)
    if with_sources:
        from .indexer import source_lines
        # Keyed by path; in a workspace by repo/path, since two repositories can hold the same path.
        several = len(repos) > 1
        for r in rows:
            if r["kind"] == "file" and r["repo_id"] in roots:
                p = Path(roots[r["repo_id"]]) / r["path"]
                try:
                    # numbered as the parsers number lines, so a span points at the same text in the page
                    text = "\n".join(source_lines(p)) if p.is_file() else None
                except OSError:
                    text = None
                if text is not None:
                    sources[f"{r['repo_id']}/{r['path']}" if several else r["path"]] = text
    out = {"version": 3, "repos": repos, "nodes": nodes, "edges": edges, "calls": calls, "flows": flows, "notes": notes, "views": views, "patterns": pattern_list, "tours": tour_list, "access": access, "state": state, "measured": measured,
           "coverage": coverage, "changes": changes(con, index, roots), "sources": sources}
    out["layout"] = layout(out, memory)
    out["parts"] = parts(con, index, nodes)
    return out


def parts(con, index: dict, nodes: list) -> list:
    """Large modules drawn as their parts, as `leyline outline` splits them (two levels): each part is added to `nodes`
    as a node of kind "part" (after the layout, which does not draw them), and listed with the files it holds."""
    from . import outline
    try:
        found = outline.map_parts(con)
    except Exception:   # the page is still worth having without them
        return []
    out = []

    def add(x, parent):
        k = len(nodes)
        extra = {key: x[key] for key in ("kind", "files", "functions", "lines", "summary", "stale", "named")
                 if x.get(key) not in (None, "", False)}
        extra["files"] = len(x["files"])
        if x["label"] != x["name"]:
            extra["label"] = x["label"]
        nodes.append({"i": "part:" + x["id"], "k": "part", "n": x["name"], "p": parent, "x": extra})
        out.append({"i": k, "id": x["id"], "files": [index[f] for f in x["files"] if f in index]})
        for s in x.get("parts", ()):
            add(s, k)
    for mp in found:
        if mp["module"] in index:
            for x in mp["parts"]:
                add(x, index[mp["module"]])
    return out


# -- changes ---------------------------------------------------------------------------------------
# After `leyline check`, the page names what was done in the words of leyline.md's last section.
AFTER_ROLE = {"edited as predicted": "done", "new, as declared": "done_new", "edited, not predicted": "drift",
              "predicted, not edited": "missed"}


def changes(con, index: dict, roots: dict) -> list[dict]:
    """Each planned change: its page (leyline.md), its tasks and scenarios tied to nodes, and the views that
    hold what it was expected to touch and, after a check, what it did touch."""
    try:
        props = con.execute("SELECT id, intent, status, attrs FROM change_proposals").fetchall()
        views = {r["id"]: r for r in con.execute("SELECT id, kind, created, spec FROM views WHERE change_id IS NOT NULL")}
    except Exception:  # a store written before changes existed
        return []
    tests = set()
    try:
        from . import spec
        tests = set(spec._tests(con).values())
    except Exception:
        pass
    out = []
    for p in props:
        cid = p["id"]
        attrs = json.loads(p["attrs"] or "{}")
        name = cid[len("spec-"):] if cid.startswith("spec-") else cid
        plan, review = views.get("view-" + cid), views.get("view-review-" + cid)
        if plan is None:
            continue
        c = {"id": cid, "name": name, "title": attrs.get("title") or p["intent"] or name, "status": p["status"],
             "planned": plan["created"], "plan_view": plan["id"], "review_view": review["id"] if review else None,
             "checked": review["created"] if review else None, "tasks": [], "scenarios": [], "page": None, "folder": None}
        for root in roots.values():   # the change folder sits in one of the mapped repositories
            folder = Path(root) / "openspec" / "changes" / name
            if (folder / "leyline.md").is_file() and store.inside(folder / "leyline.md", root):
                c["page"] = (folder / "leyline.md").read_text(encoding="utf-8", errors="replace")
                c["folder"] = f"openspec/changes/{name}/"
                break
        try:
            for r in con.execute("SELECT * FROM spec_items WHERE change_id = ? ORDER BY kind, key", (cid,)):
                ids = [index[i] for i in json.loads(r["nodes"] or "[]") if i in index]
                extra = json.loads(r["attrs"] or "{}")
                if r["kind"] == "task":
                    c["tasks"].append({"key": r["key"], "text": r["text"], "action": r["action"], "nodes": ids,
                                       "new": [{"name": n.get("label") or n["name"], "pi": index.get(n.get("parent"))}
                                               for n in extra.get("new", [])],
                                       "into": [index[i] for i in extra.get("into", []) if i in index]})
                else:
                    c["scenarios"].append({"key": r["key"], "name": r["text"], "test": ids[0] if ids else None})
        except Exception:
            pass
        if review:
            # leyline.md does not count a test the change edits (that is how a scenario gets proven), a new
            # function only the planned code calls (a helper), or new code inside a container a task names, as
            # outside the spec. The map says the same.
            ids = {k: nid for nid, k in index.items()}
            marks = [m for m in json.loads(review["spec"]).get("marks", []) if m["id"] in index]
            named = {ids[i] for t in c["tasks"] for i in t["nodes"]} | {
                m["id"] for m in marks if m["role"] in ("edited as predicted", "new, as declared")}
            into = [ids[i] for t in c["tasks"] for i in t["into"]]
            after = []
            for m in marks:
                role = AFTER_ROLE.get(m["role"], m["role"])
                if role == "drift":
                    if m["id"] in tests or "/test:" in m["id"]:
                        role = "test_changed"
                    elif any(m["id"].startswith(x + ".") or m["id"].startswith(x + "/") for x in into):
                        role = "done"
                    elif str(m.get("note", "")).startswith("new") and _only_called_from(con, m["id"], named):
                        role = "helper"
                after.append({"i": index[m["id"]], "role": role, "note": m.get("note") or ""})
            c["after"] = after
        out.append(c)
    out.sort(key=lambda c: c["checked"] or c["planned"] or "", reverse=True)
    return out


def _only_called_from(con, node_id: str, named: set) -> bool:
    """True when something calls the node and every caller is named code (or inside it)."""
    callers = {r[0] for r in con.execute("SELECT DISTINCT src_id FROM calls WHERE dst_id = ?", (node_id,))} - {node_id}
    return bool(callers) and all(c in named or any(c.startswith(n + ".") or c.split("(")[0] == n.split("(")[0] for n in named)
                                 for c in callers)


# -- a layout that stays put -----------------------------------------------------------------------
# The page draws a level of the map in rows, what depends on something above it. For the levels a person
# comes back to (the repositories of a workspace, the modules of a repository, the parts of a module) the
# rows are worked out here and remembered in a file beside the store, so a re-index keeps the map familiar:
# a box that was there before keeps its row and its place, a new box goes next to what it links to, and a
# box that went away leaves its neighbours where they were. Delete the file to lay the map out afresh.
LINK_KINDS = {"calls": "calls", "uses_type": "types", "instantiates": "types", "extends": "inherit", "implements": "inherit",
              "imports": "imports", "depends_on": "imports", "communicates": "channels"}
ROW_WIDTH = 1300     # px; wider rows wrap
GAP_X, GAP_X_THIN = 26, 12


def memory_path(con) -> Optional[Path]:
    """Where a store's remembered layout lives: beside it, as <store>.layout.json."""
    try:
        for row in con.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return Path(row[2]).with_suffix(".layout.json")
    except Exception:
        pass
    return None


def layout(g: dict, memory: Optional[Path] = None) -> dict:
    """{level: {node index: [row, x]}} for the workspace, each repository and each module. x is the centre
    of the box in px; the page keeps that order and moves a box right only as far as needed to fit."""
    N = g["nodes"]
    unit, module, repo, system = _ownership(N, g["edges"])
    links = [(k, s, d, (x or {}).get("channel") if k == "communicates" and isinstance(x, dict) else None)
             for k, s, d, _, x in g["edges"] if k in LINK_KINDS] + [("calls", s, d, None) for s, d, *_ in g["calls"]]
    kids: dict[int, list[int]] = {}
    for i, n in enumerate(N):
        if n.get("p") is not None:
            kids.setdefault(n["p"], []).append(i)
    levels: dict[str, tuple[list[int], set]] = {}
    repos = [i for i, n in enumerate(N) if n["k"] == "repo"]
    if len(repos) > 1:
        pairs = {(repo[s], repo[d]) for _, s, d, ch in links if repo[s] is not None and repo[d] is not None and repo[s] != repo[d]}
        levels["ws"] = (repos, pairs)
    mods = {}
    for i, n in enumerate(N):
        if n["k"] == "module" and repo[i] is not None:
            mods.setdefault(repo[i], []).append(i)
    for r in repos:
        own = set(mods.get(r, []))
        pairs = set()
        for _, s, d, ch in links:
            a, b = module[s], module[d]
            if a in own and b in own and a != b:
                pairs.add((b, a) if ch == "event" else (a, b))
        levels["repo:" + N[r]["i"]] = (sorted(own), pairs)
    inner = {"calls", "inherit", "channels"}   # what the page shows inside a module until a person asks for more
    in_sys = {d: s for k, s, d, *_ in g["edges"] if k == "groups"}
    grp = lambda u: in_sys.get(u, u)
    # The links inside each module, found in one pass: going over every link for every module took minutes and
    # most of the memory on a repository of thousands of modules.
    within: dict[int, list] = {}
    for k, s, d, ch in links:
        if LINK_KINDS[k] in inner and module[s] is not None and module[s] == module[d] \
                and unit[s] is not None and unit[d] is not None:
            within.setdefault(module[s], []).append((s, d, ch))
    for m in (i for i, n in enumerate(N) if n["k"] == "module"):
        nodes = [k for k in kids.get(m, []) if N[k]["k"] == "system"]
        stack = [k for k in kids.get(m, []) if N[k]["k"] == "file"]
        while stack:
            f = stack.pop()
            for k in [f] + kids.get(f, []):
                if unit[k] == k and k not in in_sys and (N[k]["k"] == "type" or any(N[c]["k"] == "callable" for c in kids.get(k, []))):
                    nodes.append(k)
        if len(nodes) < 2:
            continue
        own = set(nodes)
        pairs = set()
        for s, d, ch in within.get(m, ()):
            a, b = grp(unit[s]), grp(unit[d])
            if a in own and b in own and a != b:
                pairs.add((b, a) if ch == "event" else (a, b))
        levels["module:" + N[m]["i"]] = (sorted(own), pairs)

    old = {}
    if memory and memory.is_file():
        try:
            old = json.loads(memory.read_text(encoding="utf-8")).get("levels", {})
        except (OSError, ValueError):
            old = {}
    width = lambda i: _box_width(N, i, kids)
    out, keep = {}, {}
    for key, (nodes, pairs) in levels.items():
        fresh = _layered(nodes, sorted(pairs), width)
        placed = _remembered(fresh, {N[i]["i"]: i for i in nodes}, old.get(key), pairs, width)
        out[key] = {str(i): v for i, v in placed.items()}
        keep[key] = {N[i]["i"]: v for i, v in placed.items()}
    if memory:
        try:
            body = json.dumps({"version": 1, "levels": keep}, separators=(",", ":"), sort_keys=True)
            if not memory.is_file() or memory.read_text(encoding="utf-8") != body:
                store.write_file(memory, body)
        except OSError:
            pass
    return out


def _ownership(N: list[dict], edges: list) -> tuple[list, list, list, list]:
    """For each node: its unit (outermost type, or its file), module, repository and system, as the page has them."""
    unit, module, repo, system = [None] * len(N), [None] * len(N), [None] * len(N), [None] * len(N)
    for i, n in enumerate(N):   # parents come before children only by accident, so climb
        c, f, top = i, None, None
        while c is not None:
            k = N[c]["k"]
            if k == "file" and f is None:
                f = c
            if k == "module" and module[i] is None:
                module[i] = c
            if k == "repo":
                repo[i] = c
            c = N[c].get("p")
        if n["k"] in ("module", "repo", "external", "system"):
            continue
        if n["k"] == "file":
            unit[i] = i
            continue
        c = i
        while N[c].get("p") is not None and N[N[c]["p"]]["k"] != "file":
            c = N[c]["p"]
        unit[i] = c if N[c]["k"] == "type" else f
    for k, s, d, *_ in edges:
        if k == "groups":
            system[d] = s
    return unit, module, repo, system


def _box_width(N: list[dict], i: int, kids: dict) -> int:
    """About as wide as the page will draw the box: the label in a 12.5px monospace face, or the line under it."""
    n = N[i]
    meta = 30 if n["k"] in ("module", "repo", "system") else 0
    return int(max(len(n["n"]) * 7.6 + 34, meta * 6.2 + 32, 70))


WAYPOINTS = 200_000


def _layered(nodes: list[int], pairs: list[tuple], width) -> dict[int, list]:
    """The page's layered layout: break cycles, layer by longest path, order by barycentre, wrap wide rows,
    route long edges through waypoints so they pass between boxes. Same input, same answer."""
    out = {v: [] for v in nodes}
    inn = {v: [] for v in nodes}
    for a, b in pairs:
        if b not in out[a]:
            out[a].append(b)
            inn[b].append(a)
    linked = [v for v in nodes if out[v] or inn[v]]
    loose = [v for v in nodes if not out[v] and not inn[v]]
    back, state = set(), {}
    for root in sorted(linked, key=lambda v: (len(inn[v]), -len(out[v]), v)):
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(out[root]))]
        while stack:
            v, it = stack[-1]
            w = next(it, None)
            if w is None:
                state[v] = 2
                stack.pop()
            elif state.get(w) == 1:
                back.add((v, w))
            elif w not in state:
                state[w] = 1
                stack.append((w, iter(out[w])))
    layer: dict[int, int] = {}
    pending = {v: sum(1 for u in inn[v] if (u, v) not in back) for v in linked}
    ready = [v for v in linked if not pending[v]]
    while ready:
        v = ready.pop()
        layer.setdefault(v, 0)
        for w in out[v]:
            if (v, w) in back:
                continue
            layer[w] = max(layer.get(w, 0), layer[v] + 1)
            pending[w] -= 1
            if not pending[w]:
                ready.append(w)
    rows: list[list[int]] = []
    for v in linked:
        while len(rows) <= layer.get(v, 0):
            rows.append([])
        rows[layer.get(v, 0)].append(v)
    rows = [r for r in rows if r]
    pos: dict = {}

    def sweep(rs, up, down, times):
        for r in rs:
            for j, v in enumerate(r):
                pos[v] = j
        for k in range(times):
            for r in (reversed(range(len(rs))) if k % 2 else range(len(rs))):
                nb = down if k % 2 else up
                bary = {}
                for v in rs[r]:
                    ns = [u for u in nb.get(v, []) if u in pos]
                    bary[v] = sum(pos[u] for u in ns) / len(ns) if ns else pos[v]
                rs[r].sort(key=lambda v: bary[v])
                for j, v in enumerate(rs[r]):
                    pos[v] = j

    at = {v: k for k, r in enumerate(rows) for v in r}
    up = {v: [u for u in inn[v] + out[v] if at[u] < at[v]] for v in linked}
    down = {v: [u for u in inn[v] + out[v] if at[u] > at[v]] for v in linked}
    sweep(rows, up, down, 4)
    ranks: list[list] = []
    for r in rows:
        line, lw = [], 0
        for v in r:
            if line and lw + width(v) > ROW_WIDTH:
                ranks.append(line)
                line, lw = [], 0
            line.append(v)
            lw += width(v) + GAP_X
        if line:
            ranks.append(line)
    rank = {v: k for k, r in enumerate(ranks) for v in r}
    up2: dict = {}
    down2: dict = {}

    def link(a, b):
        down2.setdefault(a, []).append(b)
        up2.setdefault(b, []).append(a)
    # A waypoint per row a long edge crosses: on a level of thousands of boxes in hundreds of rows that is tens of
    # millions of them (GBs), so past a budget long edges are ordered by their ends alone.
    route = sum(max(0, abs(rank[a] - rank[b]) - 1) for a, b in pairs) <= WAYPOINTS
    for a, b in pairs:
        ra, rb = rank[a], rank[b]
        if ra == rb:
            continue
        top, bottom = (a, b) if ra < rb else (b, a)
        prev = top
        for r in range(min(ra, rb) + 1, max(ra, rb)) if route else ():
            d = ("~", a, b, r)
            ranks[r].append(d)
            rank[d] = r
            link(prev, d)
            prev = d
        link(prev, bottom)
    sweep(ranks, up2, down2, 8)
    w = lambda v: 2 if isinstance(v, tuple) else width(v)

    def gap(a, b):
        da, db = isinstance(a, tuple), isinstance(b, tuple)
        return 5 if da and db else GAP_X_THIN if da or db else GAP_X
    placed: dict[int, list] = {}

    def place(line, row):
        total = sum(w(v) for v in line) + sum(gap(line[j], line[j + 1]) for j in range(len(line) - 1))
        x = -total / 2
        for j, v in enumerate(line):
            if not isinstance(v, tuple):
                placed[v] = [row, round(x + w(v) / 2)]
            x += w(v) + (gap(v, line[j + 1]) if j + 1 < len(line) else 0)
    for k, r in enumerate(ranks):
        place(r, k)
    row = len(ranks)
    line, lw = [], 0
    for v in sorted(loose, key=lambda v: v):
        if line and lw + width(v) > ROW_WIDTH:
            place(line, row)
            row += 1
            line, lw = [], 0
        line.append(v)
        lw += width(v) + GAP_X
    if line:
        place(line, row)
    return placed


def _remembered(fresh: dict, ids: dict, old: Optional[dict], pairs: set, width) -> dict:
    """Keep the boxes the remembered layout knows where they were, and fit new ones in beside their links.
    When most of the boxes are new, the remembered layout is for other code: start again."""
    if not old:
        return fresh
    at = {i: old[nid] for nid, i in ids.items() if nid in old}
    if len(at) * 2 < len(ids):
        return fresh
    rows = [r for r, _ in at.values()]
    first, last = min(rows), max(rows)
    ins: dict = {}
    outs: dict = {}
    for a, b in pairs:   # looked up per box below; scanning every pair for every new box was quadratic
        outs.setdefault(a, []).append(b)
        ins.setdefault(b, []).append(a)
    in_row: dict = {}
    for j, (r, _) in at.items():
        in_row.setdefault(r, []).append(j)
    for nid, i in sorted(ids.items()):
        if i in at:
            continue
        above = [at[a][0] for a in ins.get(i, ()) if a in at]     # what depends on it
        below = [at[b][0] for b in outs.get(i, ()) if b in at]    # what it depends on
        row = max(above) + 1 if above else min(below) - 1 if below else last
        near = [at[j][1] for j in ins.get(i, []) + outs.get(i, []) if j in at]
        here = in_row.get(row, [])
        if near:
            x = sum(near) / len(near)
        else:   # at the right-hand end of its row
            x = max([at[j][1] + width(j) / 2 for j in here] or [0]) + GAP_X + width(i) / 2
        at[i] = [row, round(_free(x, width(i), [(at[j][1], width(j)) for j in here]))]
        in_row.setdefault(row, []).append(i)
    return at


def _free(x: float, w: float, row: list[tuple]) -> float:
    """The nearest centre to x where a box w wide overlaps none in the row, so a new box never takes an old one's
    place (the page would then push the old one aside)."""
    clear = lambda c: all(abs(c - cx) >= (w + cw) / 2 + GAP_X for cx, cw in row)
    if clear(x):
        return x
    spots = [cx + s * ((w + cw) / 2 + GAP_X) for cx, cw in row for s in (-1, 1)]
    return min((c for c in spots if clear(c)), key=lambda c: (abs(c - x), c), default=x)


def fragment(con, with_sources: bool = True, open_change: Optional[str] = None, memory: Optional[Path] = None) -> str:
    """The viewer with data embedded, without an html/head/body wrapper. `open_change` names a change the page
    opens on; `memory` defaults to the layout file beside the store."""
    template = resources.files("leyline").joinpath("viewer/viewer.html").read_text(encoding="utf-8")
    g = graph(con, with_sources, memory if memory is not None else memory_path(con))
    if open_change:
        g["start"] = {"change": open_change}
    data = json.dumps(g, separators=(",", ":"))
    kind = "application/json"
    if len(data) > COMPRESS_OVER:
        # A big map goes in gzipped: the page unpacks it with the browser's own DecompressionStream.
        data, kind = base64.b64encode(gzip.compress(data.encode(), 6, mtime=0)).decode(), "application/gzip+base64"
    else:
        # No `<` at all inside the script element: `</script>` would end it, and `<!--` then `<script` in some
        # source text would make the browser read past its real end tag and into the viewer's code.
        data = data.replace("<", "\\u003c")
    repos = [r["id"] for r in g["repos"]] or ["repo"]
    name = repos[0] if len(repos) == 1 else " + ".join(repos[:3]) + (f" and {len(repos) - 3} more" if len(repos) > 3 else "")
    return (template.replace("__LEYLINE_REPO__", html.escape(name))
            .replace('type="application/json">__LEYLINE_DATA__', f'type="{kind}">' + data))


COMPRESS_OVER = 2_000_000   # characters of JSON; smaller pages stay readable as plain JSON


def page(con, with_sources: bool = True, open_change: Optional[str] = None, memory: Optional[Path] = None) -> str:
    return PAGE.format(fragment=fragment(con, with_sources, open_change, memory))
