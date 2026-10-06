"""Assess a proposed change before any code is written, and save views of the result.

A change is a list of targets, each with an action. The assessment is deterministic: it follows
callers, interface links, channels and flows in the store. Deciding which nodes a written spec
touches is the caller's job (a person, or an agent using search and expand).
"""

from __future__ import annotations

import datetime
import hashlib
import json
from collections import defaultdict
from typing import Optional

ACTIONS = ("behavior", "signature", "remove", "rename", "add")
BREAKING = ("signature", "remove", "rename")
ROLE_ORDER = ["changed", "new", "contract", "must_edit", "direct", "test", "indirect", "note"]


def _load(con):
    nodes = {r["id"]: r for r in con.execute("SELECT id, kind, name, parent_id, path, span_start, attrs FROM nodes")}
    anc = {r["node_id"]: (r["file_id"], r["module_id"]) for r in con.execute("SELECT * FROM ancestry")}
    return nodes, anc


def _unit(nodes, i: str) -> Optional[str]:
    n = nodes.get(i)
    if n is None or n["kind"] in ("module", "repo", "external", "system"):
        return None
    if n["kind"] == "file":
        return i
    cur = i
    while nodes[cur]["parent_id"] in nodes and nodes[nodes[cur]["parent_id"]]["kind"] != "file":
        cur = nodes[cur]["parent_id"]
    return cur if nodes[cur]["kind"] == "type" else nodes[cur]["parent_id"]


def _label(nodes, i: str) -> str:
    n = nodes[i]
    if n["kind"] in ("callable", "field", "test"):
        u = _unit(nodes, i)
        owner = nodes[u]["name"] if u and u != i else ""
        return f"{owner}.{n['name']}" if owner and n["kind"] != "test" else n["name"]
    return n["name"]


def assess(con, intent: str, targets: list[dict], depth: int = 4) -> dict:
    """Work out what a change reaches. Each target is {id, action, note?}; for action `add` it is
    {action: "add", name, parent, uses?: [ids], used_by?: [ids], note?}."""
    nodes, anc = _load(con)
    problems = []
    for t in targets:
        if t.get("action") not in ACTIONS:
            problems.append(f"action must be one of {ACTIONS}, got {t.get('action')!r}")
        elif t["action"] == "add":
            if not t.get("name") or t.get("parent") not in nodes:
                problems.append(f"an added node needs a name and an existing parent id: {t}")
        elif t.get("id") not in nodes:
            problems.append(f"no node with id {t.get('id')!r}")
    if problems:
        return {"error": "; ".join(problems)}

    callers: dict[str, list] = defaultdict(list)      # callee -> [(caller, precision)]
    for r in con.execute("SELECT src_id, dst_id, MIN(precision = 'guess') AS sure FROM calls GROUP BY src_id, dst_id"):
        callers[r["dst_id"]].append((r["src_id"], "calls", not r["sure"]))
    for r in con.execute("SELECT src_id, dst_id, precision, attrs FROM edges WHERE kind = 'communicates'"):
        ch = (json.loads(r["attrs"]) if r["attrs"] else {}).get("channel", "channel")
        callers[r["dst_id"]].append((r["src_id"], ch, r["precision"] == "guess"))
    bases: dict[str, list] = defaultdict(list)
    impls: dict[str, list] = defaultdict(list)
    for r in con.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'overrides'"):
        bases[r["src_id"]].append(r["dst_id"])
        impls[r["dst_id"]].append(r["src_id"])
    type_users: dict[str, list] = defaultdict(list)
    for r in con.execute("SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('uses_type','instantiates','extends','implements')"):
        type_users[r["dst_id"]].append((r["src_id"], r["kind"]))
    field_users: dict[str, list] = defaultdict(list)   # field -> [(function, reads|writes, guessed)]
    for r in con.execute("SELECT src_id, dst_id, kind, precision FROM edges WHERE kind IN ('reads', 'writes')"):
        field_users[r["dst_id"]].append((r["src_id"], r["kind"], r["precision"] == "guess"))
    kids: dict[str, list] = defaultdict(list)
    for i, n in nodes.items():
        if n["parent_id"]:
            kids[n["parent_id"]].append(i)

    marks: dict[str, dict] = {}

    def mark(i: str, role: str, note: str = "", dist: Optional[int] = None) -> None:
        cur = marks.get(i)
        if cur is None or ROLE_ORDER.index(role) < ROLE_ORDER.index(cur["role"]):
            marks[i] = {"id": i, "role": role, "note": note or (cur or {}).get("note", ""), "distance": dist}
        elif note and not cur.get("note"):
            cur["note"] = note

    touched: set[str] = set()          # callables whose body or contract changes
    breaking: set[str] = set()
    new_nodes = []
    for t in targets:
        if t["action"] == "add":
            new_nodes.append({"name": t["name"], "parent": t["parent"], "note": t.get("note", ""),
                              "uses": [u for u in t.get("uses", []) if u in nodes],
                              "used_by": [u for u in t.get("used_by", []) if u in nodes]})
            for u in t.get("used_by", []):
                if u in nodes:
                    mark(u, "must_edit", f"will call the new {t['name']}", 1)
                    if nodes[u]["kind"] in ("callable", "test"):
                        touched.add(u)
            for u in t.get("uses", []):
                if u in nodes:
                    mark(u, "direct", f"used by the new {t['name']}", 1)
            continue
        i, n = t["id"], nodes[t["id"]]
        mark(i, "changed", t.get("note", "") or t["action"], 0)
        members = [i] if n["kind"] in ("callable", "test") else []
        stack = [i] if n["kind"] not in ("callable", "test") else []
        while stack:
            cur = stack.pop()
            for k in kids.get(cur, []):
                if nodes[k]["kind"] in ("callable", "test"):
                    members.append(k)
                elif nodes[k]["kind"] in ("type", "file"):
                    stack.append(k)
        if n["kind"] == "field":
            # Everything that reads or assigns the field depends on what it holds.
            how: dict[str, set] = defaultdict(set)
            for user, kind, guess in field_users.get(i, []):
                if user in nodes:
                    how[user].add(("assigns" if kind == "writes" else "reads") + (" (a guess)" if guess else ""))
            for user, verbs in sorted(how.items()):
                note = f"{' and '.join(sorted(verbs))} {n['name']}"
                mark(user, "must_edit" if t["action"] in BREAKING else "direct", note, 1)
                if t["action"] != "rename" and nodes[user]["kind"] in ("callable", "test"):
                    members.append(user)   # what it computes may change, so its callers are reached
        if n["kind"] == "system":
            for r in con.execute("SELECT dst_id FROM edges WHERE kind = 'groups' AND src_id = ?", (i,)):
                stack = [r[0]]
                while stack:
                    cur = stack.pop()
                    for k in kids.get(cur, []):
                        (members.append(k) if nodes[k]["kind"] in ("callable", "test") else stack.append(k))
        touched.update(members)
        if t["action"] in BREAKING:
            breaking.update(members if n["kind"] in ("callable",) else [])
            if n["kind"] == "type":
                for user, kind in type_users.get(i, []):
                    mark(user, "must_edit", f"{kind.replace('_', ' ')} {n['name']}", 1)
            if n["kind"] == "callable":
                for b in bases.get(i, []):
                    mark(b, "contract", "the interface or base method this implements; it must change too", 1)
                    for sib in impls.get(b, []):
                        if sib != i:
                            mark(sib, "must_edit", f"also implements {_label(nodes, b)}", 1)
                    for c, via, _g in callers.get(b, []):
                        mark(c, "must_edit", f"calls it through {_label(nodes, b)}", 1)
                for impl in impls.get(i, []):
                    mark(impl, "must_edit", f"implements {_label(nodes, i)}", 1)

    # Walk callers outward from everything touched.
    dist: dict[str, int] = {t: 0 for t in touched}
    guessed_links = 0
    channel_links = []
    frontier = list(touched)
    for d in range(1, depth + 1):
        nxt = []
        for cur in frontier:
            sources = list(callers.get(cur, []))
            for b in bases.get(cur, []):       # callers of the interface reach the implementation
                sources.extend(callers.get(b, []))
            for src, via, guess in sources:
                if src not in nodes:
                    continue
                if d == 1 and guess:
                    guessed_links += 1
                if via not in ("calls",) and src not in dist:
                    channel_links.append({"from": src, "to": cur, "channel": via, "guessed": bool(guess)})
                if src in dist:
                    continue
                dist[src] = d
                nxt.append(src)
                if d == 1:
                    role = "must_edit" if cur in breaking else "direct"
                    mark(src, role, ("its call must change" if cur in breaking else "calls it directly")
                         + (" (link is a guess)" if guess else ""), 1)
                else:
                    mark(src, "indirect", f"{d} calls away", d)
        frontier = nxt

    # Flows and tests.
    flows = []
    tested: set[str] = set()
    if touched:
        marks_sql = ",".join("?" * len(touched))
        for f in con.execute(
                f"SELECT f.id, f.name, f.entry_id, f.attrs, COUNT(*) AS hits FROM flows f JOIN flow_steps s"
                f" ON s.flow_id = f.id WHERE s.callable_id IN ({marks_sql}) GROUP BY f.id ORDER BY hits DESC, f.name",
                sorted(touched)):
            a = json.loads(f["attrs"]) if f["attrs"] else {}
            flows.append({"id": f["id"], "name": f["name"], "kind": a.get("kind"), "trigger": a.get("detail"),
                          "touched_steps": f["hits"], "entry": f["entry_id"]})
            if a.get("kind") == "test":
                mark(f["entry_id"], "test", "passes through the change; run it", None)
        for r in con.execute(
                f"SELECT DISTINCT s.callable_id FROM flow_steps s JOIN flows f ON f.id = s.flow_id"
                f" WHERE json_extract(f.attrs, '$.kind') = 'test' AND s.callable_id IN ({marks_sql})", sorted(touched)):
            tested.add(r[0])
        # Tests that were measured running the changed code, whether or not a static path leads there.
        from . import coverage as measured
        if measured.has(con):
            have = {f["entry"] for f in flows}
            for r in con.execute(
                    f"SELECT test, test_id, COUNT(DISTINCT node_id) AS hits FROM covered WHERE node_id IN ({marks_sql})"
                    f" AND test != '' GROUP BY test ORDER BY hits DESC, test", sorted(touched)):
                if r["test_id"] and r["test_id"] in nodes and r["test_id"] not in have:
                    have.add(r["test_id"])
                    flows.append({"id": f"flow:{r['test_id']}", "name": nodes[r["test_id"]]["name"], "kind": "test",
                                  "trigger": "measured", "touched_steps": r["hits"], "entry": r["test_id"], "measured": True})
                    mark(r["test_id"], "test", "ran the changed code when coverage was measured; run it", None)
                elif r["test_id"] in have:
                    next(f for f in flows if f["entry"] == r["test_id"])["measured"] = True
            for r in con.execute(f"SELECT DISTINCT node_id FROM covered WHERE node_id IN ({marks_sql})", sorted(touched)):
                tested.add(r[0])
    untested = sorted(t for t in touched if t not in tested and nodes[t]["kind"] == "callable")

    def module_of(i):
        return (anc.get(i) or (None, None))[1]

    system_of = {r["dst_id"]: r["src_id"] for r in con.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'groups'")}
    sys_name = {}
    for s in con.execute("SELECT n.id, n.name, (SELECT value FROM annotations a WHERE a.node_id = n.id AND a.key = 'name'"
                         " ORDER BY a.layer = 'intent' DESC LIMIT 1) AS given FROM nodes n WHERE n.kind = 'system'"):
        sys_name[s["id"]] = s["given"] or s["name"]
    by_module: dict[str, dict] = {}
    by_system: dict[str, dict] = {}
    home_modules = {module_of(t) for t in touched} | {module_of(n["parent"]) or n["parent"] for n in new_nodes}
    for i, m in marks.items():
        if m["role"] in ("test",):
            continue
        mod = module_of(i) or (i if nodes[i]["kind"] == "module" else None)
        if mod:
            g = by_module.setdefault(mod, {"module": nodes[mod]["name"], "id": mod, "changed": 0, "must_edit": 0, "reached": 0})
            g["changed" if m["role"] in ("changed", "new") else "must_edit" if m["role"] in ("must_edit", "contract") else "reached"] += 1
        u = _unit(nodes, i)
        s = system_of.get(u) if u else None
        if s:
            g = by_system.setdefault(s, {"system": sys_name.get(s, s), "id": s, "changed": 0, "must_edit": 0, "reached": 0})
            g["changed" if m["role"] in ("changed", "new") else "must_edit" if m["role"] in ("must_edit", "contract") else "reached"] += 1

    must = [m for m in marks.values() if m["role"] in ("must_edit", "contract")]
    tests = [f for f in flows if f["kind"] == "test"]
    risks = []
    crossing = sorted({by_module[m]["module"] for m in by_module if m not in home_modules
                       and (by_module[m]["must_edit"] or by_module[m]["reached"])})
    if crossing:
        risks.append({"level": "high" if any(by_module[m]["must_edit"] for m in by_module if m not in home_modules) else "medium",
                      "what": f"Reaches outside its own module: {', '.join(crossing)}."})
    if channel_links:
        chans = sorted({c["channel"] for c in channel_links})
        behavioral = any(t["action"] in ("behavior", "remove") for t in targets)
        risks.append({"level": "high" if behavioral else "low",
                      "what": f"Code on the far side of a channel ({', '.join(chans)}) reaches this. "
                      + ("Nothing type-checks the two sides against each other, so a change in what is sent or expected fails only at run time."
                         if behavioral else "A signature change cannot break it, but it will see any change in behavior.")})
    if untested:
        risks.append({"level": "high" if len(untested) == len([t for t in touched if nodes[t]['kind'] == 'callable']) else "medium",
                      "what": f"{len(untested)} of {len([t for t in touched if nodes[t]['kind'] == 'callable'])} changed functions are on no test's path."})
    if guessed_links:
        risks.append({"level": "low", "what": f"{guessed_links} direct caller links are guesses by name, so the caller list may be wrong."})
    if any(t["action"] in BREAKING for t in targets) and not must:
        risks.append({"level": "low", "what": "A breaking change with no callers found. Check for use through reflection, events or outside code."})
    fields = [t["id"] for t in targets if t["action"] != "add" and nodes[t["id"]]["kind"] == "field"]
    if fields:
        risks.append({"level": "medium", "what": "Reads and writes of fields are not tracked yet, so users of the changed fields are missing."})

    def brief(m):
        n = nodes[m["id"]]
        return {"id": m["id"], "name": _label(nodes, m["id"]), "kind": n["kind"], "path": n["path"],
                "line": n["span_start"], "module": nodes[module_of(m["id"])]["name"] if module_of(m["id"]) else None,
                "role": m["role"], "note": m["note"], "distance": m["distance"]}

    ordered = sorted(marks.values(), key=lambda m: (ROLE_ORDER.index(m["role"]), m["distance"] or 0, m["id"]))
    return {
        "intent": intent,
        "summary": {"changed": len([m for m in marks.values() if m["role"] == "changed"]), "added": len(new_nodes),
                    "must_edit": len(must), "reached": len([m for m in marks.values() if m["role"] in ("direct", "indirect")]),
                    "modules": len(by_module), "systems": len(by_system), "flows": len(flows), "tests_to_run": len(tests),
                    "depth": depth},
        "risks": risks,
        "must_edit": [brief(m) for m in ordered if m["role"] in ("must_edit", "contract")][:80],
        "tests_to_run": [{"name": f["name"], "id": f["entry"], "touched_steps": f["touched_steps"],
                          **({"measured": True} if f.get("measured") else {})} for f in tests],
        "entry_points_affected": [{"name": f["name"], "trigger": f["trigger"], "id": f["entry"]} for f in flows if f["kind"] != "test"][:40],
        "untested": [{"id": t, "name": _label(nodes, t)} for t in untested][:40],
        "channels": [{**c, "from_name": _label(nodes, c["from"]), "to_name": _label(nodes, c["to"])} for c in channel_links],
        "by_module": sorted(by_module.values(), key=lambda g: -(g["changed"] * 100 + g["must_edit"] * 10 + g["reached"])),
        "by_system": sorted(by_system.values(), key=lambda g: -(g["changed"] * 100 + g["must_edit"] * 10 + g["reached"])),
        "new_nodes": new_nodes,
        "marks": [brief(m) for m in ordered],
        "limits": "Static analysis: it shows what the change can reach, not whether behavior stays correct. "
                  "Links found only at run time, and field reads and writes, are not included.",
    }


def propose(con, intent: str, targets: list[dict], title: Optional[str] = None, depth: int = 4,
            source: str = "mcp") -> dict:
    """Assess a change, store it as a draft proposal, and save a view of its blast radius."""
    report = assess(con, intent, targets, depth)
    if "error" in report:
        return report
    cid = "chg-" + hashlib.sha1((intent + json.dumps(targets, sort_keys=True)).encode()).hexdigest()[:8]
    commit = con.execute("SELECT commit_sha FROM nodes WHERE kind = 'repo' LIMIT 1").fetchone()
    title = title or (intent if len(intent) <= 70 else intent[:67] + "...")
    with con:
        con.execute("INSERT OR REPLACE INTO change_proposals (id, intent, status, base_commit, head_commit, attrs)"
                    " VALUES (?,?,?,?,NULL,?)",
                    (cid, intent, "draft", commit[0] if commit else None,
                     json.dumps({"title": title, "targets": targets, "report": {k: v for k, v in report.items() if k != "marks"}})))
    view = save_view(con, title, intent, report["marks"], kind="change", source=source, change_id=cid,
                     extra={"summary": report["summary"], "risks": report["risks"], "tests_to_run": report["tests_to_run"],
                            "channels": report["channels"], "by_module": report["by_module"], "by_system": report["by_system"],
                            "new_nodes": report["new_nodes"], "untested": report["untested"],
                            "entry_points_affected": report["entry_points_affected"], "limits": report["limits"]},
                     view_id="view-" + cid)
    try:  # keep the graph as it was when the change was proposed, to compare against later
        from . import diff
        diff.snapshot(con, cid)
        report["snapshot"] = True
    except Exception:
        report["snapshot"] = False
    report["change_id"], report["view_id"] = cid, view["id"]
    report["marks"] = report["marks"][:60]
    return report


def save_view(con, title: str, narrative: str, marks: list[dict], kind: str = "custom", source: str = "mcp",
              change_id: Optional[str] = None, extra: Optional[dict] = None, view_id: Optional[str] = None,
              legend: Optional[dict] = None) -> dict:
    """Save a named view: marked nodes, each with a role and a note, plus a narrative.
    Roles are free text; the viewer colors changed, new, contract, must_edit, direct, indirect and test
    specially and shows any other role as a labelled group."""
    ids = {r[0] for r in con.execute("SELECT id FROM nodes")}
    clean, missing = [], []
    for m in marks:
        if m.get("id") in ids:
            clean.append({"id": m["id"], "role": str(m.get("role") or "note"), "note": str(m.get("note") or ""),
                          **({"distance": m["distance"]} if m.get("distance") is not None else {})})
        else:
            missing.append(m.get("id"))
    if not clean:
        return {"error": "a view needs at least one mark whose id exists in the store", "missing": missing[:5]}
    vid = view_id or "view-" + hashlib.sha1((title + narrative).encode()).hexdigest()[:8]
    spec = {"narrative": narrative, "marks": clean, "legend": legend or {}, **(extra or {})}
    with con:
        con.execute("INSERT OR REPLACE INTO views (id, title, kind, layer, source, created, change_id, spec)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (vid, title, kind, "inferred", source,
                     datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), change_id, json.dumps(spec)))
    return {"id": vid, "title": title, "marks": len(clean), "missing": missing[:10]}


def list_views(con) -> dict:
    out = []
    for v in con.execute("SELECT * FROM views ORDER BY created DESC"):
        spec = json.loads(v["spec"])
        out.append({"id": v["id"], "title": v["title"], "kind": v["kind"], "created": v["created"],
                    "change_id": v["change_id"], "marks": len(spec.get("marks", [])),
                    "summary": spec.get("summary")})
    return {"total": len(out), "views": out}


def get_view(con, view_id: str) -> dict:
    v = con.execute("SELECT * FROM views WHERE id = ?", (view_id,)).fetchone()
    if v is None:
        return {"error": f"No view with id {view_id!r}. Use `views` to list them."}
    return {"id": v["id"], "title": v["title"], "kind": v["kind"], "created": v["created"],
            "change_id": v["change_id"], **json.loads(v["spec"])}
