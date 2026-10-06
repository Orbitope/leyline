"""Compare two stores of the same repo: what was edited, what links changed, and what that did
to the module and system structure. Also check a finished change against its proposal."""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import change, rules

EDGE_KINDS = ("extends", "implements", "uses_type", "instantiates", "imports", "communicates", "overrides", "depends_on")
CODE_KINDS = ("type", "callable", "field", "test")


def store_path(con) -> Path:
    return Path(next(r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"))


def snapshot(con, name: str) -> Path:
    """Copy the store as it is now. Returns the snapshot's path."""
    target = store_path(con).parent / "snapshots" / f"{name}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    out = sqlite3.connect(str(target))
    con.commit()
    con.backup(out)
    out.close()
    return target


def _open(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _base(qualified_id: str) -> str:
    # The id without its parameter list: a method keeps this when only its signature changes.
    head, sep, _ = qualified_id.rpartition("(")
    return head if sep and qualified_id.endswith(")") else qualified_id


def compare(before: sqlite3.Connection, after: sqlite3.Connection) -> dict:
    def nodes(con):
        return {r["id"]: r for r in con.execute(
            f"SELECT id, kind, name, parent_id, path, span_start, content_hash, attrs FROM nodes"
            f" WHERE layer = 'fact' AND kind IN ({','.join('?' * len(CODE_KINDS))})", CODE_KINDS)}
    b, a = nodes(before), nodes(after)
    removed, added = set(b) - set(a), set(a) - set(b)
    # A method whose parameters changed has a new id. Pair it with the old one by owner and name.
    resigned: dict[str, str] = {}
    by_base = defaultdict(list)
    for i in added:
        by_base[(_base(i), a[i]["kind"])].append(i)
    for i in sorted(removed):
        cands = by_base.get((_base(i), b[i]["kind"]), [])
        if b[i]["kind"] == "callable" and len(cands) == 1 and cands[0] not in resigned.values():
            resigned[i] = cands[0]
    removed -= set(resigned)
    added -= set(resigned.values())
    # Anything nested under a re-signed method moved with it.
    moved = {}
    for old, new in resigned.items():
        for i in list(removed):
            if i.startswith(old + "/") and (new + i[len(old):]) in added:
                moved[i] = new + i[len(old):]
    for old, new in moved.items():
        removed.discard(old)
        added.discard(new)
    remap = {**resigned, **moved}
    edited = {i for i in set(a) & set(b) if a[i]["content_hash"] != b[i]["content_hash"] and a[i]["kind"] != "type"}
    edited |= {new for old, new in moved.items() if a[new]["content_hash"] != b[old]["content_hash"]}
    types_edited = {i for i in set(a) & set(b) if a[i]["kind"] == "type" and a[i]["content_hash"] != b[i]["content_hash"]}

    def links(con, mapping):
        out = set()
        for r in con.execute("SELECT DISTINCT src_id, dst_id FROM calls"):
            out.add(("calls", mapping.get(r[0], r[0]), mapping.get(r[1], r[1])))
        for r in con.execute(f"SELECT kind, src_id, dst_id FROM edges WHERE kind IN ({','.join('?' * len(EDGE_KINDS))})", EDGE_KINDS):
            out.add((r[0], mapping.get(r[1], r[1]), mapping.get(r[2], r[2])))
        return out
    lb, la = links(before, remap), links(after, {})
    links_added, links_removed = la - lb, lb - la

    def rollup(con, link_set):
        mod = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
        for r in con.execute("SELECT id FROM nodes WHERE kind IN ('module', 'external')"):
            mod[r[0]] = r[0]
        out = defaultdict(int)
        for k, s, d in link_set:
            ms, md = mod.get(s), mod.get(d)
            if ms and md and ms != md:
                out[(ms, md)] += 1
        return out
    names_a = {r["id"]: r["name"] for r in after.execute("SELECT id, name FROM nodes")}
    names_b = {r["id"]: r["name"] for r in before.execute("SELECT id, name FROM nodes")}
    rb, ra = rollup(before, links(before, {})), rollup(after, la)
    new_deps = [{"from": names_a.get(s, s), "to": names_a.get(d, d), "links": n, "from_id": s, "to_id": d}
                for (s, d), n in sorted(ra.items()) if (s, d) not in rb]
    gone_deps = [{"from": names_b.get(s, s), "to": names_b.get(d, d), "links": n}
                 for (s, d), n in sorted(rb.items()) if (s, d) not in ra]

    def flows(con, mapping):
        out = {}
        for f in con.execute("SELECT id, name, entry_id FROM flows"):
            steps = [mapping.get(r[0], r[0]) for r in con.execute(
                "SELECT callable_id FROM flow_steps WHERE flow_id = ? ORDER BY seq", (f["id"],))]
            out[mapping.get(f["entry_id"], f["entry_id"])] = (f["name"], steps)
        return out
    fb, fa = flows(before, remap), flows(after, {})
    flow_changes = []
    flow_gained, flow_lost = defaultdict(int), defaultdict(int)
    for key in sorted(set(fb) | set(fa)):
        if key not in fb:
            flow_changes.append({"name": fa[key][0], "change": "new", "entry": key})
        elif key not in fa:
            flow_changes.append({"name": fb[key][0], "change": "gone"})
        else:
            gained, lost = set(fa[key][1]) - set(fb[key][1]), set(fb[key][1]) - set(fa[key][1])
            for i in gained:
                flow_gained[i] += 1
            for i in lost:
                flow_lost[i] += 1
            if gained or lost:
                flow_changes.append({"name": fa[key][0], "change": "path changed", "entry": key,
                                     "gained": len(gained), "lost": len(lost)})

    def label(con_nodes, i):
        n = con_nodes[i]
        parent = con_nodes.get(n["parent_id"])
        name = f"{parent['name']}.{n['name']}" if parent is not None and parent["kind"] == "type" else n["name"]
        return {"id": i, "name": name,
                "kind": n["kind"], "path": n["path"], "line": n["span_start"]}
    return {
        "nodes": {
            "added": [label(a, i) for i in sorted(added)],
            "removed": [label(b, i) for i in sorted(removed)],
            "resigned": [{**label(a, new), "was": old.rsplit("(", 1)[-1].rstrip(")"),
                          "now": new.rsplit("(", 1)[-1].rstrip(")")} for old, new in sorted(resigned.items())],
            "edited": [label(a, i) for i in sorted(edited)],
            "types_edited": [label(a, i) for i in sorted(types_edited)],
        },
        "links": {"added": len(links_added), "removed": len(links_removed),
                  "added_examples": [{"kind": k, "from": names_a.get(s, s), "to": names_a.get(d, d), "from_id": s, "to_id": d}
                                     for k, s, d in sorted(links_added)[:40]],
                  "removed_examples": [{"kind": k, "from": names_b.get(s, names_a.get(s, s)), "to": names_b.get(d, names_a.get(d, d))}
                                       for k, s, d in sorted(links_removed)[:40]]},
        "structure": {"new_dependencies": new_deps, "removed_dependencies": gone_deps},
        "flows": {"changed": len(flow_changes), "items": flow_changes[:60],
                  "now_pass_through": [{**label(a, i), "flows": n} for i, n in sorted(flow_gained.items(), key=lambda x: -x[1])[:15] if i in a],
                  "no_longer_pass_through": [{"id": i, "name": names_b.get(i, names_a.get(i, i)), "flows": n}
                                             for i, n in sorted(flow_lost.items(), key=lambda x: -x[1])[:15]]},
        "remap": remap,
    }


def record_tests(con, run: str, results: list[dict]) -> dict:
    """Store one test run. Each result is {name, status: pass|fail|skip, message?}."""
    ids = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM nodes WHERE kind = 'test'")}
    # A test named with an interpolated string ("{tag}: reproduces ...") matches any text in the holes.
    templates = [(re.compile("^" + ".+".join(re.escape(part) for part in re.split(r"\{[^}]*\}", name)) + "$"), i)
                 for name, i in ids.items() if "{" in name]

    def node(name):
        return ids.get(name) or next((i for pattern, i in templates if pattern.match(name)), None)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    rows = [(run, r["name"], node(r["name"]), r["status"], r.get("message"), now) for r in results]
    with con:
        con.execute("DELETE FROM test_results WHERE run = ?", (run,))
        con.executemany("INSERT OR REPLACE INTO test_results VALUES (?,?,?,?,?,?)", rows)
    counts = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1
    return {"run": run, **counts, "matched_to_test_nodes": sum(1 for r in rows if r[2])}


_RESULT = re.compile(r"^\s*(PASS(?:ED)?|FAIL(?:ED)?|SKIP(?:PED)?|ok|not ok)\b[\s:.\-]*(.+?)\s*$", re.I)


def parse_test_output(text: str) -> list[dict]:
    """Read results from runner output that prints one `PASS name` or `FAIL name: message` line per
    test (also pytest -rA short summaries and TAP). Other formats: pass results to record_tests directly."""
    out = {}
    for line in text.splitlines():
        m = _RESULT.match(line)
        if not m:
            continue
        word, rest = m.group(1).lower(), m.group(2)
        status = "fail" if word.startswith(("fail", "not")) else "skip" if word.startswith("skip") else "pass"
        name, message = rest, None
        if status == "fail":
            for sep in (": ", " - "):
                if sep in rest:
                    name, message = rest.split(sep, 1)
                    break
        out[name.strip()] = {"name": name.strip(), "status": status, "message": message}
    return list(out.values())


def review_text(r: dict) -> str:
    p, g = r["prediction"], r["graph"]["nodes"]
    lines = [f"Review of {r['change_id']}: {r['intent']}", "",
             f"Prediction: {p['as_predicted']} of {p['predicted']} predicted edits happened, "
             f"{p['not_predicted']} edits were not predicted, {p['predicted_untouched']} predicted edits did not happen. "
             f"{p['new_as_declared']} new nodes belong to what the proposal declared."]
    for n in r["not_predicted"]:
        lines.append(f"  not predicted: {n['name']}  {n['path']}:{n['line']}")
    for n in r["predicted_untouched"]:
        lines.append(f"  not edited:    {n['name']}")
    lines.append(f"Graph: {len(g['added'])} added, {len(g['removed'])} removed, {len(g['resigned'])} signatures changed, "
                 f"{len(g['edited'])} bodies edited; {r['graph']['links']['added']} links added, {r['graph']['links']['removed']} removed.")
    for d in r["graph"]["structure"]["new_dependencies"]:
        lines.append(f"  new dependency: {d['from']} -> {d['to']} ({d['links']} links)")
    lines.append(f"Flows with a changed path: {r['graph']['flows']['changed']}")
    for x in r["graph"]["flows"]["now_pass_through"][:5]:
        lines.append(f"  {x['flows']} flows now pass through {x['name']}")
    for x in r["graph"]["flows"]["no_longer_pass_through"][:5]:
        lines.append(f"  {x['flows']} flows no longer pass through {x['name']}")
    lines.append(f"Rules: {r['rules']['checked']} checked, {r['rules']['failing']} failing, {len(r['rules']['new_violations'])} newly failing.")
    t = r["tests"]
    if t:
        lines.append(f"Tests: {t['before']['passed']}/{t['before']['total']} before, {t['after']['passed']}/{t['after']['total']} after; "
                     f"{len(t['newly_failing'])} newly failing.")
        lines += [f"  now fails: {x['name']}: {x['message']}" for x in t["newly_failing"]]
    for v in r["verdict"]:
        lines.append(f"[{v['level']}] {v['what']}")
    if not r["verdict"]:
        lines.append("Nothing flagged.")
    return "\n".join(lines)


def test_delta(con, before_run: str, after_run: str) -> Optional[dict]:
    def load(run):
        return {r["name"]: r for r in con.execute("SELECT * FROM test_results WHERE run = ?", (run,))}
    b, a = load(before_run), load(after_run)
    if not b or not a:
        return None
    return {
        "before": {"run": before_run, "passed": sum(1 for r in b.values() if r["status"] == "pass"), "total": len(b)},
        "after": {"run": after_run, "passed": sum(1 for r in a.values() if r["status"] == "pass"), "total": len(a)},
        "newly_failing": [{"name": n, "message": a[n]["message"]} for n in sorted(a) if a[n]["status"] == "fail" and b.get(n) and b[n]["status"] == "pass"],
        "newly_passing": [n for n in sorted(a) if a[n]["status"] == "pass" and b.get(n) and b[n]["status"] == "fail"],
        "added": [n for n in sorted(a) if n not in b], "removed": [n for n in sorted(b) if n not in a],
    }


def review(con, change_id: str, before_run: Optional[str] = None, after_run: Optional[str] = None) -> dict:
    """After a change is implemented and the repo re-indexed: compare the graph with the snapshot
    taken when the change was proposed, and the edits made with the edits predicted."""
    row = con.execute("SELECT * FROM change_proposals WHERE id = ?", (change_id,)).fetchone()
    if row is None:
        return {"error": f"No change {change_id!r}."}
    snap = store_path(con).parent / "snapshots" / f"{change_id}.db"
    if not snap.exists():
        return {"error": "No snapshot was kept for this change, so there is nothing to compare with."}
    before = _open(snap)
    try:
        d = compare(before, con)
        # Today's rules, evaluated on the graph as it was: a rule added after the proposal still counts.
        rules_before = rules.check(before, rules_from=con)
    finally:
        before.close()
    view = change.get_view(con, "view-" + change_id)
    remap = d.pop("remap")
    predicted = {remap.get(m["id"], m["id"]): m for m in view.get("marks", []) if m["role"] in ("changed", "must_edit", "contract")}
    declared_new = {n["name"].split("(")[0].split(".")[-1] for n in view.get("new_nodes", [])}

    def segments(i):
        return set(re.split(r"[.:/]+", _base(i)))
    touched = {n["id"]: n for key in ("resigned", "edited") for n in d["nodes"][key]}
    # A type's text changes whenever a member's does. Report a type only when no member explains it.
    for t in d["nodes"]["types_edited"]:
        inside = [i for i in list(touched) + [n["id"] for n in d["nodes"]["added"]] if i.startswith(t["id"] + ".")]
        if not inside:
            touched[t["id"]] = {**t, "why": "declaration or fields changed"}
    as_predicted = [touched[i] for i in sorted(touched) if i in predicted]
    not_predicted = [touched[i] for i in sorted(touched) if i not in predicted]
    new_declared, new_undeclared = [], []
    for n in d["nodes"]["added"]:
        (new_declared if segments(n["id"]) & declared_new else new_undeclared).append(n)
    # Members of an undeclared new type are noise: keep the outermost new nodes only.
    outer = [n for n in new_undeclared if not any(n["id"].startswith(o["id"] + ".") for o in new_undeclared if o is not n)]
    not_predicted += [{**n, "why": "new, not declared in the proposal"} for n in outer]
    untouched = [{"id": i, "name": m.get("name", i), "note": m.get("note", "")} for i, m in sorted(predicted.items())
                 if i not in touched]
    gone = {n["id"] for n in d["nodes"]["removed"]}
    untouched = [u for u in untouched if u["id"] not in gone]
    removed_predicted = [n for n in d["nodes"]["removed"] if n["id"] in predicted]
    not_predicted += [{**n, "why": "removed"} for n in d["nodes"]["removed"] if n["id"] not in predicted]
    as_predicted += removed_predicted
    now_rules = rules.check(con)
    was_failing = {r["id"] for r in rules_before.get("rules", []) if not r["passes"]}
    new_violations = [r for r in now_rules["rules"] if not r["passes"] and r["id"] not in was_failing]
    tests = test_delta(con, before_run, after_run) if before_run and after_run else None
    verdict = []
    if not_predicted:
        verdict.append({"level": "medium", "what": f"{len(not_predicted)} edits were not in the proposal."})
    if untouched:
        verdict.append({"level": "medium", "what": f"{len(untouched)} predicted edits did not happen."})
    if d["structure"]["new_dependencies"]:
        verdict.append({"level": "high", "what": f"{len(d['structure']['new_dependencies'])} new dependencies between modules."})
    if new_violations:
        verdict.append({"level": "high", "what": f"{len(new_violations)} rules now fail that passed before."})
    if tests and tests["newly_failing"]:
        verdict.append({"level": "high", "what": f"{len(tests['newly_failing'])} tests fail that passed before."})
    if tests is None:
        verdict.append({"level": "medium", "what": "No test runs were recorded, so behavior is unchecked."})
    report = {
        "change_id": change_id, "intent": row["intent"],
        "prediction": {"predicted": len(predicted), "as_predicted": len(as_predicted),
                       "not_predicted": len(not_predicted), "predicted_untouched": len(untouched),
                       "new_as_declared": len(new_declared)},
        "as_predicted": as_predicted, "not_predicted": not_predicted, "predicted_untouched": untouched,
        "graph": d, "rules": {"checked": now_rules["total"], "failing": now_rules["failing"],
                              "new_violations": new_violations, "all": now_rules["rules"]},
        "tests": tests, "verdict": verdict,
        "limits": "The graph comparison shows structure, not behavior. Tests are the only behavior check here, "
                  "and only as far as they reach.",
    }
    head = con.execute("SELECT commit_sha FROM nodes WHERE kind = 'repo' LIMIT 1").fetchone()
    attrs = json.loads(row["attrs"] or "{}")
    attrs["review"] = {k: v for k, v in report.items() if k != "graph"} | {"graph_summary": {
        "added": len(d["nodes"]["added"]), "removed": len(d["nodes"]["removed"]), "resigned": len(d["nodes"]["resigned"]),
        "edited": len(d["nodes"]["edited"]), "links_added": d["links"]["added"], "links_removed": d["links"]["removed"]}}
    with con:
        con.execute("UPDATE change_proposals SET status = 'implemented', head_commit = ?, attrs = ? WHERE id = ?",
                    (head[0] if head else None, json.dumps(attrs), change_id))
    marks = ([{"id": n["id"], "role": "edited as predicted", "note": n.get("why", "") or n.get("now", "")} for n in as_predicted]
             + [{"id": n["id"], "role": "new, as declared", "note": n["kind"]} for n in new_declared if n["kind"] != "field"]
             + [{"id": n["id"], "role": "edited, not predicted", "note": n.get("why") or n["kind"]} for n in not_predicted if n["id"] not in gone]
             + [{"id": n["id"], "role": "predicted, not edited", "note": n["note"]} for n in untouched])
    title = "Review: " + (attrs.get("title") or row["intent"][:60])
    saved = change.save_view(
        con, title, row["intent"], marks or [{"id": m["id"], "role": "changed", "note": ""} for m in view.get("marks", [])[:1]],
        kind="review", source="leyline-review", change_id=change_id, view_id="view-review-" + change_id,
        legend={"edited as predicted": "edited, as the proposal said", "edited, not predicted": "edited or added, but not in the proposal",
                "new, as declared": "new code the proposal said it would add",
                "predicted, not edited": "in the proposal, but not edited"},
        extra={"review": {k: v for k, v in report.items() if k not in ("as_predicted", "intent")}})
    report["view_id"] = saved.get("id")
    return report
