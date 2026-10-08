"""Tours: an ordered walk through a codebase, each stop pointing at something on the map.

The orientation tour is generated from the graph alone, so every sentence in it is a count or a name
the store can back. An agent or the user can write further tours for a purpose (a feature end to end,
what a new contributor to one module needs) through `save`.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import store

SOURCE = "leyline-tour/0.1"
REF_KINDS = ("repo", "node", "flow", "pattern", "view")
SIDE_DIRS = ("sample", "samples", "example", "examples", "bench", "benchmarks", "docs", "doc", "demo", "demos", "snippets")


def _plural(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _names(items, limit=4) -> str:
    items = list(items)
    return ", ".join(items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _readme(con, repo_id: str) -> Optional[str]:
    here = store.roots(con).get(repo_id)
    row = (str(here),) if here is not None else None
    if not row:
        return None
    for name in ("README.md", "README.rst", "README.txt", "README"):
        p = Path(row[0]) / name
        if p.is_file():
            para: list[str] = []
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                s = line.strip()
                skip = (not s or s.startswith(("#", "=", "-", "!", "[", "<", "|", "..", "```", ">")) or set(s) <= set("=-~"))
                if skip and para:
                    break
                if not skip:
                    para.append(s)
            text = re.sub(r"\[([^\]]+)\](\([^)]*\)|\[[^\]]*\])?", r"\1", " ".join(para)).replace("**", "").replace("`", "")
            if len(text) > 40:
                return text[:420].rsplit(" ", 1)[0] + ("..." if len(text) > 420 else "")
    return None


def generate(con, repo_id: str) -> dict:
    """Write the orientation tour for a repo, replacing the previous one."""
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    repo = q("SELECT id, name FROM nodes WHERE id = ? AND kind = 'repo'", repo_id)
    if not repo:
        return {"error": f"no repo {repo_id!r}"}
    mods = {r["id"]: r for r in q("SELECT id, name, path FROM nodes WHERE kind = 'module' AND repo_id = ?", repo_id)}
    if not mods:
        return {"stops": 0}
    # The few module ids and kinds are one string each: every row read gives its own copy, and on a large
    # repository the copies were hundreds of MB.
    share = {}
    share = share.setdefault
    module = {r[0]: share(r[1], r[1]) for r in con.execute("SELECT node_id, module_id FROM ancestry")}
    names, kind_of, parent = {}, {}, {}
    for r in con.execute("SELECT id, name, kind, parent_id FROM nodes WHERE repo_id = ?", (repo_id,)):   # one pass, not three
        names[r[0]], kind_of[r[0]], parent[r[0]] = r[1], share(r[2], r[2]), r[3]

    def qual(i):
        p = parent.get(i)
        return f"{names[p]}.{names[i]}" if kind_of.get(p) == "type" and kind_of.get(i) in ("callable", "field") else names.get(i, i)
    # Only each flow's kind is read from its attrs, which list every module the flow passes (hundreds of MB in all).
    flows = q("SELECT id, name, entry_id, json_extract(attrs, '$.kind') AS kind FROM flows"
              " WHERE entry_id IN (SELECT id FROM nodes WHERE repo_id = ?)", repo_id)
    flow_kind = {f["id"]: f["kind"] for f in flows}
    test_mods = {module.get(f["entry_id"]) for f in flows if flow_kind[f["id"]] == "test"} - {None}

    def side(m):  # samples, docs, benchmarks and test helpers: real code, but not the product
        return any(part.lower() in SIDE_DIRS or part.lower().startswith("test") for part in (mods[m]["path"] or "").split("/"))
    core = [m for m in mods if m not in test_mods and not side(m)] or list(mods)
    size = {r["m"]: (r["files"], r["loc"]) for r in q(
        "SELECT a.module_id AS m, COUNT(*) AS files, SUM(COALESCE(n.span_end, 0)) AS loc FROM nodes n"
        " JOIN ancestry a ON a.node_id = n.id WHERE n.kind = 'file' GROUP BY a.module_id")}
    deps, users = defaultdict(set), defaultdict(set)
    for sql in ("SELECT src_id, dst_id FROM calls",
                "SELECT src_id, dst_id FROM edges WHERE kind IN ('imports','uses_type','instantiates','extends','implements')"):
        for r in con.execute(sql):   # read as they come: held all at once, millions of rows were GBs
            a, b = module.get(r[0], r[0] if r[0] in mods else None), module.get(r[1], r[1] if r[1] in mods else None)
            if a and b and a != b and a in mods and b in mods:
                deps[a].add(b)
                users[b].add(a)
    langs = [r[0] for r in q("SELECT language FROM nodes WHERE kind = 'file' AND repo_id = ? AND language IS NOT NULL"
                             " GROUP BY language ORDER BY COUNT(*) DESC", repo_id)]
    n_files = q("SELECT COUNT(*) FROM nodes WHERE kind = 'file' AND repo_id = ?", repo_id)[0][0]
    stops: list[dict] = []

    def stop(title, kind, ref, text):
        stops.append({"title": title, "ref_kind": kind, "ref_id": ref, "narrative": " ".join(text.split())})

    # 1. What it is.
    readme = _readme(con, repo_id)
    lang_names = {"csharp": "C#", "python": "Python"}
    stop("What this repository is", "repo", repo_id,
         f"{repo[0]['name']} has {_plural(len(mods), 'module')} and {_plural(n_files, 'source file')}, written in "
         f"{_names([lang_names.get(x, x) for x in langs])}. "
         + (f"{len(test_mods)} of the modules hold tests. " if test_mods else "No tests were found. ")
         + (f"Its README begins: \"{readme}\"" if readme else ""))

    # 2. Where it starts.
    entries = q("SELECT e.dst_id AS target, n.attrs FROM edges e JOIN nodes n ON n.id = e.src_id"
                " WHERE e.kind = 'exposes' AND n.kind = 'entry_point' AND n.repo_id = ?", repo_id)
    reach = {}
    for f in flows:
        if flow_kind[f["id"]] != "test":
            steps = [r[0] for r in q("SELECT callable_id FROM flow_steps WHERE flow_id = ?", f["id"])]
            reach[f["entry_id"]] = (f["id"], len(steps), {module.get(s) for s in steps} - {None})
    path_of = {r["id"]: r["path"] for r in q("SELECT id, path FROM nodes WHERE repo_id = ? AND kind IN ('callable', 'test')", repo_id)}
    all_starts = [e["target"] for e in entries if e["target"] in reach]
    # An entry that reaches almost nothing (a build script, a one-line sample) teaches little.
    starts = sorted((t for t in all_starts if reach[t][1] > 8),
                    key=lambda t: (module.get(t) not in core, -len(reach[t][2]), -reach[t][1], t))
    if not starts and core:
        lib = max(core, key=lambda m: len(users[m]))
        stop("No single place to start", "node", lib,
             "No entry point in the product code leads far, which is what a library looks like: it is entered through "
             "whatever its callers use. "
             + (f"The {_plural(len(all_starts), 'entry point')} found {'is a sample, benchmark or build script' if len(all_starts) == 1 else 'are samples, benchmarks or build scripts'}. " if all_starts else "")
             + "The tests are the best record of how it is meant to be called.")
    picked = []
    for t in starts:  # at most two, from different modules
        if len(picked) < 2 and module.get(t) not in {module.get(x) for x in picked}:
            picked.append(t)
    for t in picked:
        fid, n, ms = reach[t]
        m = module.get(t)
        script = names.get(t, "").startswith("<") or "top-level" in names.get(t, "")
        body = f"The top-level code of {path_of.get(t) or 'this file'} in" if script else f"{qual(t)} in"
        stop(f"Where {(path_of.get(t) or '').rsplit('/', 1)[-1] if script else qual(t)} starts", "node", t,
             f"{body} {mods[m]['name'] if m in mods else 'this module'} is a program entry point. From here the code can reach "
             f"{_plural(n - 1, 'function')} in {_plural(len(ms), 'module')}"
             + (f": {_names(sorted(mods[x]['name'] for x in ms if x in mods))}." if ms else ".")
             + " Open Flows to walk that path in order.")
    if len(all_starts) > len(picked) and picked:
        stops[-1]["narrative"] += f" There are {len(all_starts) - len(picked)} more entry points; the repository panel lists them."

    # 3. The modules, most depended-on first.
    order = sorted(core, key=lambda m: (-len(users[m] & set(core)), -len(users[m]), -(size.get(m, (0, 0))[1] or 0), m))
    top_types = lambda m: list(dict.fromkeys(r["name"] for r in q(
        "SELECT n.name, COUNT(*) AS c FROM edges e JOIN nodes n ON n.id = e.dst_id JOIN ancestry a ON a.node_id = n.id"
        " WHERE a.module_id = ? AND n.kind = 'type' AND e.kind IN ('uses_type','instantiates','extends','implements')"
        " GROUP BY n.id ORDER BY c DESC, n.name LIMIT 8", m)))[:4]
    systems = defaultdict(list)
    for r in q("SELECT n.id, n.parent_id, n.name, (SELECT value FROM annotations a WHERE a.node_id = n.id AND a.key = 'name'"
               " ORDER BY layer = 'intent' DESC LIMIT 1) AS given FROM nodes n WHERE n.kind = 'system'"):
        systems[r["parent_id"]].append(r["given"] or r["name"])
    for k, m in enumerate(order[:5]):
        files, loc = size.get(m, (0, 0))
        d, u = sorted(mods[x]["name"] for x in deps[m]), sorted(mods[x]["name"] for x in users[m])
        role = ("Nothing else in the repository is needed to read it, so it is the place to start." if not d and u else
                "Nothing depends on it, so it is an end point: an application, a tool or a host for the code below it." if d and not u else
                "It stands alone: no other module uses it and it uses none." if not d and not u else "")
        tt = top_types(m)
        stop(("The foundation: " if k == 0 and u else "Module: ") + mods[m]["name"], "node", m,
             f"{mods[m]['name']} has {_plural(files, 'file')} and about {loc:,} lines. "
             + (f"It is used by {_names(u)}. " if u else "")
             + (f"It depends on {_names(d)}. " if d else "")
             + role + " "
             + (f"Its most used types are {_names(tt)}. " if tt else "")
             + (f"Clustering splits it into {len(systems[m])} groups: {_names(sorted(systems[m]), 5)}." if systems.get(m) else ""))
    if len(order) > 5:
        stops[-1]["narrative"] += f" {len(order) - 5} smaller modules are left out of this tour."

    # 4. The main abstractions and boundaries, from the pattern labels.
    try:
        from . import patterns
        labels = patterns.listing(con, limit=500)["patterns"]
    except Exception:
        labels = []
    weight = lambda p: (p["confidence"], sum(len(v) for v in p["roles"].values()))
    shapes = sorted((p for p in labels if p["pattern"] in ("strategy", "composite", "decorator", "template method", "builder")
                     and p["confidence"] >= 0.7), key=weight, reverse=True)
    seen_kinds = set()
    for p in shapes:
        if p["pattern"] in seen_kinds or len(seen_kinds) >= 3:
            continue
        seen_kinds.add(p["pattern"])
        stop(f"A {p['pattern']} to know", "pattern", p["id"], f"{p['rationale']} {p['about']}")
    edges_ = sorted((x for x in labels if x["pattern"] in ("process boundary", "observer")),
                    key=lambda x: (x["pattern"] != "process boundary", "standard input" not in x["rationale"]))
    shown = set()
    for p in edges_:
        if p["pattern"] in shown:
            continue  # one of each kind; the rest are in the pattern list
        shown.add(p["pattern"])
        stop("A boundary" if p["pattern"] == "process boundary" else "An event", "pattern", p["id"],
             f"{p['rationale']} A change on one side of this needs a matching change on the other, and the compiler will not say so.")

    # State with more than one writer.
    try:
        from . import query
        shared = [f for f in query.shared_state(con, limit=100000)["fields"] if len(f["written_from"]) >= 2]
    except Exception:
        shared = []
    if shared:
        f = shared[0]
        stop("State changed from several places", "node", f["id"],
             f"{f['name']} is assigned by {_plural(f['writers'], 'function')} in {_names(f['written_from'])}, none of them the type "
             f"that declares it, and read by {_plural(f['readers'], 'function')}. No single piece of code keeps it valid, so a "
             f"change to what it means has to be checked at every one of those places. "
             + (f"{len(shared) - 1} more fields are assigned from two or more other types; `leyline state` lists them." if len(shared) > 1 else ""))

    # 5. A path worth tracing: a test that crosses the most modules without being huge.
    best = None
    sized = store.flow_callables(con, 6, 80)
    core_set = set(core)
    for f in flows:
        if flow_kind[f["id"]] != "test" or f["id"] not in sized:
            continue
        steps = sized[f["id"]]
        if 6 <= len(steps) <= 80:
            ms = {module.get(s) for s in steps} - {None}
            score = (len(ms & core_set), -abs(len(steps) - 25))
            if best is None or score > best[0]:
                best = (score, f, len(steps), ms)
    if best:
        _, f, n, ms = best
        stop("A path worth tracing", "flow", f["id"],
             f"The test \"{f['name']}\" passes through {_plural(n, 'function')} in {_names(sorted(mods[x]['name'] for x in ms if x in mods))}. "
             f"Reading it top to bottom shows how those modules work together on one concrete case.")

    # 6. Tests, and what the map does not show.
    n_tests = sum(1 for f in flows if flow_kind[f["id"]] == "test")
    if n_tests:
        tested = {r[0] for r in q(store.TESTED)}
        fns = [r[0] for r in q("SELECT id FROM nodes WHERE kind = 'callable' AND repo_id = ?", repo_id) if module.get(r[0]) in core]
        on = sum(1 for i in fns if i in tested)
        home = sorted(test_mods, key=lambda m: -(size.get(m, (0, 0))[1] or 0))
        from . import coverage as measured
        m = measured.summary(con)
        watched = sum(x["functions"] for x in m.get("modules", []))
        if m.get("imported") and watched:
            did = sum(x["ran"] for x in m["modules"])
            off = sum(x["ran_off_every_path"] for x in m["modules"])
            never = sum(x["path_but_never_ran"] for x in m["modules"])
            tail = (f"Coverage was measured: {did:,} of the {watched:,} functions in the watched files ran ({round(100 * did / watched)}%). "
                    f"{off:,} of those ran without being on any test's path on the map, so they were reached through links the map "
                    f"does not have; {never:,} are on a path and never ran.")
        else:
            tail = ("That is reachability read from the source, not measured coverage: a function on a path may sit behind a "
                    "branch the test never takes. Import a coverage file to see what ran.")
        stop("How it is tested", "node", home[0],
             f"There are {_plural(n_tests, 'test')} in {_names([mods[m_]['name'] for m_ in home if m_ in mods])}. "
             f"{on:,} of the {len(fns):,} functions outside test code are on some test's path ({round(100 * on / max(1, len(fns)))}%). " + tail)
    cov = q("SELECT extractor, status, stats FROM extractor_coverage WHERE repo_id = ?", repo_id)
    blind = sorted(c["extractor"].split(":", 1)[-1] for c in cov if c["status"] == "not_analyzed" and c["extractor"].startswith("communicates"))
    guess = q("SELECT COUNT(*) FROM calls WHERE precision = 'guess'")[0][0]
    total = q("SELECT COUNT(*) FROM calls")[0][0]
    open_calls = sum((json.loads(c["stats"] or "{}").get("calls_unresolved", 0) + json.loads(c["stats"] or "{}").get("calls_guess_declined", 0))
                     for c in cov if c["extractor"].startswith(("tree-sitter", "generic")))
    stop("What this map cannot see", "repo", repo_id,
         f"Links come from reading syntax, not from a compiler. Of {total:,} call links, {guess:,} are guesses by name, and "
         f"{open_calls:,} call sites could not be tied to a function at all. "
         + (f"Connections through {_names(blind, 6)} are not analyzed, so two parts that talk that way look unconnected. " if blind else "")
         + "Flows show what can run, not what did run.")

    tid = f"tour:orientation:{repo_id}"
    _write(con, tid, f"Orientation: {repo[0]['name']}", "someone new to the repository", "fact", SOURCE, repo_id, stops)
    return {"id": tid, "stops": len(stops)}


def _write(con, tid, title, audience, layer, source, repo_id, stops) -> None:
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute("DELETE FROM tour_stops WHERE tour_id = ?", (tid,))
        con.execute("INSERT OR REPLACE INTO tours (id, title, audience, layer, source, repo_id, created) VALUES (?,?,?,?,?,?,?)",
                    (tid, title, audience, layer, source, repo_id, now))
        con.executemany("INSERT INTO tour_stops (tour_id, seq, ref_kind, ref_id, narrative, title) VALUES (?,?,?,?,?,?)",
                        [(tid, k, s["ref_kind"], s["ref_id"], s["narrative"], s.get("title") or "") for k, s in enumerate(stops)])


def _exists(con, kind: str, ref: str) -> bool:
    table = {"node": "nodes", "repo": "nodes", "flow": "flows", "pattern": "pattern_instances", "view": "views"}[kind]
    return con.execute(f"SELECT 1 FROM {table} WHERE id = ?", (ref,)).fetchone() is not None


def save(con, title: str, stops: list[dict], audience: str = "", source: str = "mcp", layer: str = "inferred") -> dict:
    """Save an authored tour. Each stop is {"title", "ref": id, "kind": node|flow|pattern|view|repo, "narrative"}."""
    clean, missing = [], []
    for s in stops:
        kind = s.get("kind") or s.get("ref_kind") or "node"
        ref = s.get("ref") or s.get("ref_id") or s.get("id")
        if kind not in REF_KINDS:
            return {"error": f"stop kind must be one of {REF_KINDS}"}
        if not ref or not _exists(con, kind, ref):
            missing.append(ref)
            continue
        if not (s.get("narrative") or "").strip():
            return {"error": "every stop needs a narrative: what to notice here and why it matters"}
        clean.append({"title": s.get("title") or "", "ref_kind": kind, "ref_id": ref, "narrative": s["narrative"].strip()})
    if not clean:
        return {"error": "no stop refers to something in the store", "missing": missing[:5]}
    tid = "tour:" + hashlib.sha1(title.encode()).hexdigest()[:8]
    repo = con.execute("SELECT id FROM nodes WHERE kind = 'repo' LIMIT 1").fetchone()
    _write(con, tid, title, audience, layer, source, repo[0] if repo else None, clean)
    return {"id": tid, "title": title, "stops": len(clean), "missing": missing[:10]}


def listing(con) -> dict:
    out = [{"id": t["id"], "title": t["title"], "audience": t["audience"], "source": t["source"], "layer": t["layer"],
            "stops": con.execute("SELECT COUNT(*) FROM tour_stops WHERE tour_id = ?", (t["id"],)).fetchone()[0]}
           for t in con.execute("SELECT * FROM tours ORDER BY source = ? DESC, created DESC", (SOURCE,))]
    return {"total": len(out), "tours": out}


def get(con, tour_id: str) -> dict:
    t = con.execute("SELECT * FROM tours WHERE id = ?", (tour_id,)).fetchone()
    if t is None:
        return {"error": f"No tour {tour_id!r}. Use `tours` to list them."}
    stops = [{"seq": s["seq"] + 1, "title": s["title"], "kind": s["ref_kind"], "ref": s["ref_id"], "narrative": s["narrative"],
              "exists": _exists(con, s["ref_kind"], s["ref_id"])}
             for s in con.execute("SELECT * FROM tour_stops WHERE tour_id = ? ORDER BY seq", (tour_id,))]
    return {"id": t["id"], "title": t["title"], "audience": t["audience"], "source": t["source"], "layer": t["layer"], "stops": stops}
