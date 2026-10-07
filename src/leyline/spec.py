"""The spec loop: a change stated in an open spec format, tied to the map before and after it is made.

A change is an OpenSpec change folder (`openspec/changes/<id>/` with proposal.md, tasks.md and spec
deltas). Leyline reads it and writes one page back, `leyline.md`, that answers four questions:

    What code will be written?      tasks, each tied to nodes on the map
    What will it affect?            blast radius, and anything it reaches that no task covers
    How will I know it was done?    scenarios, each tied to a test of the same name
    Was it done as agreed?          after implementation: tasks, scenarios, drift, rules

Conventions a spec author follows, and nothing more:
- Name code in backticks in tasks: `Vehicle.Speed`, `SignalController.Tick`, `Signal.Core/Controls.cs`.
  A name that is not on the map yet is taken as new code; write it as `Owner.NewName` to say where it goes.
- Start a task with what it does: add, remove, rename, change. Anything else is read as a change in behavior.
- Give each scenario a test with the same name.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import change, diff, rules

TASK = re.compile(r"^\s*[-*]\s*\[([^\]]*)\]\s*(\d+(?:\.\d+)*)?\.?\s*(.+?)\s*$")
CODE = re.compile(r"`([^`\n]+)`")
VERBS = (("add", ("add ", "create ", "introduce ", "new ", "implement ")), ("remove", ("remove ", "delete ", "drop ")),
         ("rename", ("rename ",)), ("signature", ("change the signature", "change signature", "add a parameter", "add parameter",
                                                  "remove a parameter", "change the return", "change return")))
BEGIN, END = "<!-- leyline:begin -->", "<!-- leyline:end -->"
REVIEWERS = ("logic", "performance")


def run_label(change_id: str, when: str) -> str:
    """The label a change's own test runs are stored under: before:spec-<id> and after:spec-<id>."""
    return f"{when}:{change_id}"


def record_review(con, change_id: str, reviewer: str) -> None:
    """Note that a review of this kind ran. A review that finds nothing files nothing, so this is its only trace."""
    row = con.execute("SELECT value FROM meta WHERE key = ?", ("review:" + change_id,)).fetchone()
    done = json.loads(row[0]) if row else {}
    done[reviewer] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ("review:" + change_id, json.dumps(done)))


def reviews(con, change_id: str) -> list[str]:
    """The kinds of review that have run on a change: recorded ones, and any that filed a finding."""
    row = con.execute("SELECT value FROM meta WHERE key = ?", ("review:" + change_id,)).fetchone()
    done = set(json.loads(row[0])) if row else set()
    done |= {r[0] for r in con.execute("SELECT DISTINCT reviewer FROM findings WHERE change_id = ?", (change_id,))}
    return sorted(done)


# -- reading the folder ------------------------------------------------------------------------
def parse(change_dir: str | Path) -> dict:
    """Read an OpenSpec change folder into its title, tasks, requirements and scenarios."""
    d = Path(change_dir)
    if not d.is_dir():
        return {"error": f"no change folder at {d}"}
    out = {"id": d.name, "dir": str(d), "title": d.name.replace("-", " "), "why": "", "what": "", "tasks": [], "scenarios": [],
           "requirements": [], "problems": []}
    proposal = d / "proposal.md"
    if proposal.is_file():
        text = proposal.read_text()
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            out["title"] = re.sub(r"^(change|proposal)\s*:\s*", "", m.group(1).strip(), flags=re.I)
        for key, head in (("why", "Why"), ("what", "What Changes")):
            m = re.search(rf"^##\s+{head}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
            if m:
                out[key] = " ".join(m.group(1).split())
    else:
        out["problems"].append("proposal.md is missing")
    tasks = d / "tasks.md"
    if tasks.is_file():
        n = 0
        for line in tasks.read_text().splitlines():
            m = TASK.match(line)
            if not m:
                continue
            n += 1
            text = m.group(3)
            lower = text.lower().lstrip("`* ")
            action = next((a for a, starts in VERBS if lower.startswith(starts) or any(s in lower for s in starts if len(s) > 12)), "behavior")
            out["tasks"].append({"key": m.group(2) or str(n), "text": text, "done": m.group(1).strip().lower() == "x",
                                 "action": action, "names": CODE.findall(text)})
    else:
        out["problems"].append("tasks.md is missing")
    for spec in sorted((d / "specs").rglob("spec.md")) if (d / "specs").is_dir() else []:
        capability = str(spec.parent.relative_to(d / "specs"))
        section = req = None
        cur = None
        for line in spec.read_text().splitlines():
            if line.startswith("## "):
                section = line[3:].strip().split()[0].upper()
            elif line.startswith("### Requirement:"):
                req = line.split(":", 1)[1].strip()
                out["requirements"].append({"capability": capability, "name": req, "kind": section or "ADDED"})
            elif line.startswith("#### Scenario:"):
                cur = {"key": f"{capability}/{line.split(':', 1)[1].strip()}", "name": line.split(":", 1)[1].strip(),
                       "requirement": req, "capability": capability, "kind": section or "ADDED", "when": [], "then": []}
                out["scenarios"].append(cur)
            elif cur is not None and re.match(r"^\s*[-*]\s*\*\*(WHEN|GIVEN|AND|THEN)\*\*", line):
                word = re.match(r"^\s*[-*]\s*\*\*(\w+)\*\*\s*(.*)$", line)
                (cur["then"] if word.group(1) == "THEN" or (word.group(1) == "AND" and cur["then"]) else cur["when"]).append(word.group(2))
            elif re.match(r"^###\s+Scenario", line):
                out["problems"].append(f"{spec.relative_to(d)}: a scenario heading needs four #, found three: {line.strip()}")
    for r in out["requirements"]:
        if r["kind"] != "REMOVED" and not any(s["requirement"] == r["name"] and s["capability"] == r["capability"] for s in out["scenarios"]):
            out["problems"].append(f"requirement \"{r['name']}\" has no scenario")
    return out


# -- tying names to the map ---------------------------------------------------------------------
class _Names:
    def __init__(self, con):
        self.con = con
        self.rows = con.execute("SELECT id, kind, name, parent_id, path FROM nodes WHERE layer = 'fact'"
                                " AND kind IN ('type', 'callable', 'field', 'file', 'module', 'test')").fetchall()
        self.by_id = {r["id"]: r for r in self.rows}
        self.by_name = defaultdict(list)
        for r in self.rows:
            self.by_name[r["name"]].append(r)
        self.module = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
        tests = {r[0] for r in con.execute("SELECT entry_id FROM flows WHERE json_extract(attrs, '$.kind') = 'test'")}
        self.test_modules = {self.module.get(t) for t in tests}

    def resolve(self, written: str) -> dict:
        """One backticked name -> {"ids": [...]} | {"new": name, "parent": id or None} | {"ambiguous": [...]} | {"skip": True}"""
        name = re.sub(r"\(.*\)$", "", written.strip())
        if not name or " " in name or name[0].isdigit() or name.startswith(("-", "/")) and "." not in name:
            return {"skip": True}
        if "/" in name or re.search(r"\.(cs|py|ts|tsx|js|gd|json|md)$", name):
            hits = [r for r in self.rows if r["kind"] in ("file", "module") and (r["path"] == name or (r["path"] or "").endswith("/" + name))]
            return {"ids": [h["id"] for h in hits]} if hits else {"new": name, "parent": None, "file": True}
        parts = name.split(".")
        leaf = parts[-1]
        cands = [r for r in self.by_name.get(leaf, []) if r["kind"] != "file"]
        if len(parts) > 1:
            owner = parts[-2]
            cands = [r for r in cands if r["parent_id"] in self.by_id and self.by_id[r["parent_id"]]["name"] == owner]
        if not cands:
            if len(parts) > 1:
                owners = [r for r in self.by_name.get(parts[-2], []) if r["kind"] in ("type", "module")]
                if len({o["id"] for o in owners}) == 1:
                    return {"new": leaf, "parent": owners[0]["id"]}
                if not owners and not re.fullmatch(r"[A-Za-z_]\w*", parts[-2]):
                    return {"skip": True}
            if not re.fullmatch(r"[A-Za-z_]\w*", leaf):
                return {"skip": True}
            return {"new": name, "parent": None}
        product = [r for r in cands if self.module.get(r["id"]) not in self.test_modules] or cands
        owners = {(r["parent_id"], r["kind"]) for r in product}
        if len(owners) == 1:
            return {"ids": sorted(r["id"] for r in product)}      # overloads of one method are one target
        types = [r for r in product if r["kind"] == "type"]
        if len(types) == 1 and len(parts) == 1:
            return {"ids": [types[0]["id"]]}                     # a bare name that is a type means the type
        return {"ambiguous": sorted(r["id"] for r in product)[:8]}


def _targets(names: _Names, parsed: dict) -> tuple[list[dict], list[dict]]:
    """Turn tasks into change targets. Returns (targets, per-task links)."""
    targets, links, seen = [], [], set()
    for t in parsed["tasks"]:
        link = {"key": t["key"], "text": t["text"], "action": t["action"], "nodes": [], "new": [], "ambiguous": [], "unknown": [],
                "into": [],
                # A task covers a scenario's test when it quotes the scenario's name.
                "scenarios": [s["key"] for s in parsed["scenarios"]
                              if _norm(s["name"]) in {_norm(q) for q in re.findall(r'["\u201c]([^"\u201d]+)["\u201d]', t["text"])}]}
        for written in t["names"]:
            r = names.resolve(written)
            if r.get("skip"):
                continue
            if "ids" in r and t["action"] == "add" and all(names.by_id[i]["kind"] in ("type", "file", "module") for i in r["ids"]):
                link["into"].extend(r["ids"])   # "add X to `Foo`": Foo is where it goes, not something whose behavior changes
            elif "ids" in r:
                link["nodes"].extend(r["ids"])
                for i in r["ids"]:
                    # Adding something to an existing type changes the type's behavior, not its contract.
                    action = "behavior" if t["action"] == "add" else t["action"]
                    if (i, action) not in seen and names.by_id[i]["kind"] != "module":
                        seen.add((i, action))
                        targets.append({"id": i, "action": action, "note": f"task {t['key']}: {t['text'][:120]}"})
            elif "ambiguous" in r:
                link["ambiguous"].append({"written": written, "could_be": r["ambiguous"]})
            elif t["action"] == "add" or r.get("parent"):
                owner = names.by_id[r["parent"]]["name"] + "." if r.get("parent") in names.by_id else ""
                link["new"].append({"name": r["new"], "parent": r.get("parent"), "label": owner + r["new"]})
                if r.get("parent"):
                    targets.append({"action": "add", "name": r["new"], "parent": r["parent"],
                                    "note": f"task {t['key']}", "used_by": []})
            else:
                link["unknown"].append(written)
        links.append(link)
    return targets, links


def _label(names: _Names, i: str) -> str:
    r = names.by_id.get(i)
    if r is None:
        return i
    owner = names.by_id.get(r["parent_id"])
    if r["kind"] in ("callable", "field") and owner is not None and owner["kind"] == "type":
        return f"{owner['name']}.{r['name']}"
    return r["path"] if r["kind"] == "file" else r["name"]


def _tests(con) -> dict:
    """Test name (lower-cased, punctuation squeezed) -> node id."""
    out = {}
    for r in con.execute("SELECT n.id, n.name FROM nodes n WHERE n.kind = 'test'"
                         " OR json_extract(n.attrs, '$.is_test') = 1"):
        out[_norm(r["name"])] = r["id"]
    return out


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("test_", "")).strip()


# -- the brief -------------------------------------------------------------------------------------
def brief(con, change_dir: str | Path, write: bool = True, new_baseline: bool = False) -> dict:
    """Assess a spec before it is implemented, store the result, and write `leyline.md` into the folder.
    Once the code has moved on from the first brief, later briefs keep that first picture of the code to
    compare against, so a spec can be amended part-way through. `new_baseline` starts over from the code as it is."""
    parsed = parse(change_dir)
    if "error" in parsed:
        return parsed
    names = _Names(con)
    targets, links = _targets(names, parsed)
    cid = "spec-" + parsed["id"]
    intent = parsed["what"] or parsed["why"] or parsed["title"]
    report = change.propose(con, intent, targets, parsed["title"], source="spec", change_id=cid,
                            keep_baseline=not new_baseline) if targets else {
        "error": "no task names code that is on the map. Put code names in backticks in tasks.md."}
    tests = _tests(con)
    scenarios = []
    for s in parsed["scenarios"]:
        tid = tests.get(_norm(s["name"]))
        scenarios.append({**s, "test": tid, "test_exists": bool(tid)})
    tasked = {i for l in links for i in l["nodes"]}
    planned = {k for l in links for k in l["scenarios"]}   # scenarios some task says it will write the test for

    def covered(i):  # a node is covered by a task that names it, its type or its file
        cur = i
        while cur:
            if cur in tasked:
                return True
            cur = names.by_id[cur]["parent_id"] if cur in names.by_id else None
        row = con.execute("SELECT file_id FROM ancestry WHERE node_id = ?", (i,)).fetchone()
        return bool(row and row[0] in tasked)
    uncovered = [m for m in report.get("must_edit", []) if not covered(m["id"])] if "error" not in report else []
    if "error" not in report:
        # A channel matters to this change when the changed code is one of its two ends. Being reachable from
        # a program that some other program launches is true of nearly everything.
        ends = {r[0] for r in con.execute("SELECT src_id FROM edges WHERE kind = 'communicates'")} | {
            r[0] for r in con.execute("SELECT dst_id FROM edges WHERE kind = 'communicates'")}
        direct = [c for c in report.get("channels") or [] if c.get("to") in tasked and c.get("to") in ends]
        report["channels"] = direct
        if not direct:
            report["risks"] = [r for r in report.get("risks") or [] if "far side of a channel" not in r["what"]]
    state = _shared_state_touched(con, tasked)
    others = _left_alone(con, names, links)
    patterns = _patterns_touched(con, tasked)
    rule_state = rules.check(con)
    gaps = list(parsed["problems"])
    for l in links:
        gaps += [f"task {l['key']}: `{a['written']}` could be {len(a['could_be'])} things; write it as `Owner.Name`" for a in l["ambiguous"]]
        gaps += [f"task {l['key']}: `{u}` is not on the map and the task does not say it is new" for u in l["unknown"]]
        gaps += [f"task {l['key']}: new `{n['name']}` has no stated home; write it as `Owner.{n['name']}`" for n in l["new"] if not n["parent"]]
        if not (l["nodes"] or l["new"] or l["into"] or l["scenarios"]):
            gaps.append(f"task {l['key']} names no code, so it cannot be checked: {l['text'][:70]}")
    gaps += [f"must be edited but no task covers it: {m['name']} ({m.get('note', '')})" for m in uncovered]
    gaps += [f"scenario \"{s['name']}\" has no test of that name, and no task says it will add one"
             for s in scenarios if not s["test_exists"] and s["key"] not in planned]
    result = {"change_id": cid, "title": parsed["title"], "why": parsed["why"], "what": parsed["what"], "dir": parsed["dir"],
              "tasks": [{**l, "labels": [_label(names, i) for i in l["nodes"]], "into_labels": [_label(names, i) for i in l["into"]]} for l in links],
              "scenarios": scenarios, "impact": {k: report.get(k) for k in ("summary", "risks", "by_module", "tests_to_run", "channels", "untested")}
              if "error" not in report else {"error": report["error"]},
              "must_edit_uncovered": uncovered, "shared_state": state, "left_alone": others, "patterns": patterns,
              "rules_failing_now": [r for r in rule_state["rules"] if not r["passes"]],
              "findings": findings(con, cid)["findings"], "gaps": gaps,
              "baseline": report.get("snapshot"), "reviews": reviews(con, cid),
              "baseline_tests": con.execute("SELECT 1 FROM test_results WHERE run = ? LIMIT 1",
                                            (run_label(cid, "before"),)).fetchone() is not None,
              "ready": not gaps and not any(f["status"] == "open" and f["severity"] == "high" for f in findings(con, cid)["findings"])}
    with con:
        con.execute("DELETE FROM spec_items WHERE change_id = ?", (cid,))
        con.executemany("INSERT INTO spec_items VALUES (?,?,?,?,?,?,?)",
                        [(cid, "task", l["key"], l["text"], l["action"], json.dumps(l["nodes"]),
                          json.dumps({"new": l["new"], "into": l["into"], "scenarios": l["scenarios"]})) for l in links]
                        + [(cid, "scenario", s["key"], s["name"], s["kind"], json.dumps([s["test"]] if s["test"] else []),
                            json.dumps({"when": s["when"], "then": s["then"], "requirement": s["requirement"]})) for s in scenarios])
    if write:
        _write(Path(parsed["dir"]) / "leyline.md", brief_text(result))
        result["written"] = str(Path(parsed["dir"]) / "leyline.md")
    return result


def _shared_state_touched(con, tasked: set) -> list[dict]:
    """Fields the named code assigns that other types also assign."""
    from . import query
    shared = {f["id"]: f for f in query.shared_state(con, limit=100000)["fields"]}
    out = {}
    for i in tasked:
        for r in con.execute("SELECT dst_id FROM edges WHERE kind = 'writes' AND (src_id = ? OR src_id LIKE ?)", (i, i + ".%")):
            if r[0] in shared and len(shared[r[0]]["written_from"]) >= 2:
                out[r[0]] = {"name": shared[r[0]]["name"], "also_written_from": shared[r[0]]["written_from"][:5]}
        if i in shared:
            out[i] = {"name": shared[i]["name"], "also_written_from": shared[i]["written_from"][:5]}
    return sorted(out.values(), key=lambda x: x["name"])


def _words(name: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+", name)]


def _task_functions(names: _Names, links: list[dict]) -> list[str]:
    """Existing functions the tasks change. A named type stands for its functions."""
    fns = []
    for l in links:
        for i in l["nodes"]:
            kind = names.by_id[i]["kind"]
            if kind in ("callable", "test"):
                fns.append(i)
            elif kind == "type":
                fns += [r["id"] for r in names.rows if r["parent_id"] == i and r["kind"] == "callable"]
    return list(dict.fromkeys(fns))


def _left_alone(con, names: _Names, links: list[dict]) -> dict:
    """Code that shares something with the change and that no task names: other callers of a changed function,
    and other users of a field a changed function uses. A parallel edit is most often forgotten here."""
    fns = _task_functions(names, links)
    inside = set(fns)

    def product(i):
        return i in names.by_id and names.by_id[i]["kind"] != "test" and names.module.get(i) not in names.test_modules
    def owner(i):
        return names.by_id[i]["parent_id"] if i in names.by_id else None

    def is_ctor(i):
        return names.by_id[i]["name"] in (".ctor", "__init__", "constructor")
    callers, state, seen = [], [], set()
    for i in fns:
        who = sorted({r[0] for r in con.execute("SELECT DISTINCT src_id FROM calls WHERE dst_id = ?", (i,))
                      if r[0] not in inside and product(r[0])})
        if who:
            callers.append({"id": i, "changed": _label(names, i), "callers": [_label(names, w) for w in who], "caller_ids": who,
                            "far": len({names.module.get(w) for w in who} - {names.module.get(i)}),
                            "other_types": len({owner(w) for w in who} - {owner(i)})})
        for f in con.execute("SELECT dst_id, MAX(kind = 'writes') FROM edges WHERE kind IN ('reads', 'writes') AND src_id = ?"
                             " GROUP BY dst_id ORDER BY 2 DESC", (i,)):
            if f[0] in seen or f[0] not in names.by_id:
                continue
            # A constructor setting a field up is not a second user of it.
            users = sorted({r[0] for r in con.execute(
                "SELECT DISTINCT src_id FROM edges WHERE kind IN ('reads', 'writes') AND dst_id = ?", (f[0],))
                if r[0] not in inside and product(r[0]) and not is_ctor(r[0])})
            if 0 < len(users) <= 8:       # a field half the program uses says nothing about this change
                seen.add(f[0])
                state.append({"field": _label(names, f[0]), "used_by_changed": _label(names, i),
                              "also_used_by_unchanged": [_label(names, u) for u in users], "user_ids": users,
                              # State the changed type owns is where a parallel edit gets forgotten; a field of
                              # some other type that the change only reads rarely is.
                              "own": owner(f[0]) == owner(i), "changed_writes_it": bool(f[1]),
                              "other_types": len({owner(u) for u in users} - {owner(f[0])})})
    # A new member named like one its type already has (EmergencyQueues beside EntryQueues) is usually a second
    # one of the same thing, and whoever uses the first is a candidate to need the second.
    beside = []
    fresh = [(n["name"].split(".")[-1], n.get("parent")) for l in links for n in l["new"] if n.get("parent")]
    fresh += [(names.by_id[i]["name"], owner(i)) for l in links if l["action"] == "add" for i in l["nodes"]
              if names.by_id[i]["kind"] in ("field", "callable")]
    fresh_names = {(n, p) for n, p in fresh}
    for name, parent in dict.fromkeys(fresh):
        last = _words(name)[-1:]
        if not last or len(_words(name)) < 2:
            continue
        for r in names.rows:
            if r["parent_id"] != parent or r["kind"] != "field" or (r["name"], parent) in fresh_names or _words(r["name"])[-1:] != last:
                continue
            users = sorted({u[0] for u in con.execute(
                "SELECT DISTINCT src_id FROM edges WHERE kind IN ('reads', 'writes') AND dst_id = ?", (r["id"],))
                if u[0] not in inside and product(u[0]) and not is_ctor(u[0])})
            if users:
                beside.append({"new": _label(names, parent) + "." + name, "existing": _label(names, r["id"]),
                               "existing_used_by_unchanged": [_label(names, u) for u in users], "user_ids": users})
    callers.sort(key=lambda c: (-c["far"], -c["other_types"], len(c["callers"])))
    state.sort(key=lambda x: (not x["own"], not x["changed_writes_it"], -x["other_types"], len(x["also_used_by_unchanged"])))
    return {"beside": beside, "callers": callers, "state": state}


def _patterns_touched(con, tasked: set) -> list[dict]:
    from . import patterns
    out = []
    owner = {r["id"]: r["parent_id"] for r in con.execute("SELECT id, parent_id FROM nodes WHERE kind IN ('callable', 'field')")}
    near = set(tasked) | {owner[i] for i in tasked if i in owner}   # a method stands for its type here
    for p in patterns.listing(con, limit=2000)["patterns"]:
        ids = {n["id"] for ns in p["roles"].values() for n in ns}
        if p["pattern"] in ("factory", "process boundary") and not (ids & set(tasked)):
            continue
        if any(i in ids for i in near):
            out.append({"pattern": p["pattern"], "rationale": p["rationale"]})
    return out[:12]


def _write(path: Path, body: str) -> None:
    """Replace the generated block of a file, keeping anything a person wrote around it."""
    block = f"{BEGIN}\n{body.rstrip()}\n{END}\n"
    if path.is_file() and BEGIN in path.read_text() and END in path.read_text():
        old = path.read_text()
        path.write_text(old[:old.index(BEGIN)] + block + old[old.index(END) + len(END):].lstrip("\n"))
    else:
        path.write_text(block)


def _some(xs: list[str], n: int = 4) -> str:
    return ", ".join(xs[:n]) + (f" and {len(xs) - n} more" if len(xs) > n else "")


def _first_sentence(text: str, limit: int = 220) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip(), re.S)
    out = (m.group(1) if m else text.strip()).replace("\n", " ")
    return out if len(out) <= limit else out[:limit - 3].rstrip() + "..."


def _n(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def brief_status(b: dict) -> dict:
    """Where a plan stands: what blocks implementation, what is left to decide, and what is worth knowing."""
    blocking = ([b["impact"]["error"]] if b["impact"].get("error") else []) + list(b["gaps"])
    opened = [f for f in b["findings"] if f["status"] == "open"]
    blocking += [f"decide the open high finding {f['id']}: {_first_sentence(f['claim'], 120)}" for f in opened if f["severity"] == "high"]
    decide = [f"decide the open {f['severity']} finding {f['id']}: {_first_sentence(f['claim'], 120)}"
              for f in opened if f["severity"] != "high"]
    done = b.get("reviews") or sorted({f["reviewer"] for f in b["findings"]})
    notes = []
    if not done:
        notes.append("No review has run. A logic and a performance review find what the plan misses before code exists.")
    elif missing := [r for r in REVIEWERS if r not in done]:
        notes.append(f"No {' or '.join(missing)} review has run.")
    if b.get("baseline_tests") is False and b.get("baseline") != "kept":
        notes.append("The tests have not been recorded as they pass now, so `leyline check` will not be able to tell a test "
                     "the change breaks from one that already failed.")
    return {"ready": not blocking, "blocking": blocking, "decide": decide, "notes": notes, "reviewed": bool(done)}


def _state_line(b: dict) -> str:
    st = brief_status(b)
    if st["blocking"]:
        return (f"**State: not ready to implement.** {_n(len(st['blocking']), 'thing')} to settle first, listed under "
                f"\"Before implementation\" at the end.")
    return "**State: ready to implement.**" + ("" if st["reviewed"] else " It has not been reviewed.")


def _plain_summary(b: dict) -> str:
    """One paragraph a newcomer can read without knowing Leyline."""
    S = [_first_sentence(b["why"], 300)] if b["why"] else []
    existing = sorted({x for t in b["tasks"] for x in t["labels"]})
    new = list(dict.fromkeys(n.get("label") or n["name"] for t in b["tasks"] for n in t["new"]))
    did = ([f"changes {_some(existing)}"] if existing else []) + ([f"adds {_some(new)}"] if new else [])
    S.append(f"The plan has {_n(len(b['tasks']), 'task')}" + (f": it {' and '.join(did)}." if did else "."))
    imp = b["impact"]
    if not imp.get("error"):
        s = imp["summary"]
        must = f"{_n(s['must_edit'], 'other place')} must be edited along with it" if s["must_edit"] else "Nothing else must be edited with it"
        reach = (f"{_n(s['reached'], 'place')} in {_n(s['modules'], 'module')} {'runs' if s['reached'] == 1 else 'run'} into the "
                 "changed code and may behave differently") if s["reached"] else "no other code runs into it"
        tests = (f"{_n(s['tests_to_run'], 'existing test')} already {'runs' if s['tests_to_run'] == 1 else 'run'} through it"
                 if s["tests_to_run"] else "no existing test runs through it")
        S.append(f"{must}; {reach}; {tests}.")
    sc = b["scenarios"]
    if sc:
        have = sum(1 for x in sc if x["test_exists"])
        S.append(f"It is done when {_n(len(sc), 'scenario')} pass" + (": " + ", ".join(
            x for x in ((f"{have} already {'has' if have == 1 else 'have'} a test" if have else ""),
                        (f"{len(sc) - have} {'needs' if len(sc) - have == 1 else 'need'} a test written" if len(sc) > have else "")) if x)
                                                                   ) + ".")
    else:
        S.append("No scenario says yet what done means.")
    return " ".join(S)


def brief_text(b: dict) -> str:
    d = Path(b["dir"])
    rel = "/".join(d.parts[-3:]) if d.parent.name == "changes" else d.name
    L = [f"# {b['title']}", "",
         f"The one-page plan for the change in `{rel}/`, written by Leyline from the spec files beside it. "
         "`leyline plan` rewrites it; edit the spec, not this page.", "",
         _state_line(b), "", _plain_summary(b), "",
         "## 1. What code will be written", ""]
    if b["tasks"]:
        L += ["| Task | Does | Code |", "| --- | --- | --- |"]
        for t in b["tasks"]:
            code = ", ".join(sorted(set(t["labels"]))[:6]) + (f" and {len(set(t['labels'])) - 6} more" if len(set(t["labels"])) > 6 else "")
            new = ", ".join(f"{n.get('label') or n['name']} (new)" for n in t["new"])
            into = ", ".join(f"in {x}" for x in t.get("into_labels", []))
            test = "a test for the scenario" if t.get("scenarios") else ""
            L.append(f"| {t['key']} | {t['text'].replace('|', '/')} | {', '.join(x for x in (code, new, test, into) if x) or 'not tied to code'} |")
    else:
        L.append("No tasks yet.")
    imp = b["impact"]
    L += ["", "## 2. What it will affect", ""]
    if imp.get("error"):
        L.append(imp["error"])
    else:
        s = imp["summary"]
        L.append(f"{s['changed'] + s['added']} things change, {s['must_edit']} more must be edited with them, and {s['reached']} "
                 f"are reached without needing an edit, across {s['modules']} modules. {s['tests_to_run']} existing tests run through the change.")
        L += ["", "*Must be edited*: code that breaks unless it changes too, such as the callers of a function whose parameters "
                  "change. *Reached*: code that runs into the change, directly or through other calls; it needs no edit but may "
                  "behave differently.", ""]
        for m in (imp.get("by_module") or [])[:8]:
            bits = [f"{m[k]} {label}" for k, label in (("changed", "changed"), ("must_edit", "to edit"), ("reached", "reached")) if m.get(k)]
            L.append(f"- {m['module']}: {', '.join(bits)}")
        for r in imp.get("risks") or []:
            L.append(f"- **{r['level']} risk:** {r['what']}")
        for c in (imp.get("channels") or [])[:6]:
            L.append(f"- Crosses a {c['channel']} boundary: {c['from_name']} to {c['to_name']}. The other side has no compile-time link to this change.")
    if b["must_edit_uncovered"]:
        L += ["", "**Must be edited, and no task covers it:**"] + [f"- {m['name']}: {m.get('note', '')}" for m in b["must_edit_uncovered"][:20]]
    la = {"beside": [], "callers": [], "state": [], **(b.get("left_alone") or {})}
    # One page: new members that double an existing one, callers outside the changed function's own type,
    # and state the changed type owns.
    cal = [c for c in la["callers"] if c.get("other_types", 1)]
    own = [x for x in la["state"] if x.get("own", True)]
    rows = [f"- {x['new']} (new) sits beside {x['existing']}, which is used by {_some(x['existing_used_by_unchanged'], 6)}" for x in la["beside"][:3]]
    rows += [f"- {c['changed']} is also called by {_some(c['callers'])}" for c in cal[:max(2, 5 - len(rows))]]
    rows += [f"- {x['field']} (used by {x['used_by_changed']}) is also used by {_some(x['also_used_by_unchanged'])}" for x in own[:max(2, 8 - len(rows))]]
    more = len(la["beside"]) + len(la["callers"]) + len(la["state"]) - len(rows)
    if rows:
        L += ["", "**Shares a caller or a field with the change, and no task names it.** Each line is either right to leave "
                  "alone or a missing task:"] + rows
        if more > 0:
            L.append(f"- and {more} more: `leyline spec facts`")
    if b["patterns"]:
        L += ["", "**Design patterns the change sits in** (found from the shape of the code):"] + [
            f"- {p['pattern']}: {p['rationale']}" for p in b["patterns"][:5]]
    L += ["", "## 3. How you will know it was done", "",
          "Each scenario is proven by a test with the same name. After the change, `leyline check` marks each one from "
          "the test results.", ""]
    if b["scenarios"]:
        L += ["| Scenario | When | Then | Test |", "| --- | --- | --- | --- |"]
        for s in b["scenarios"]:
            L.append(f"| {s['name']} | {'; '.join(s['when']).replace('|', '/')} | {'; '.join(s['then']).replace('|', '/')} | "
                     f"{'exists' if s['test_exists'] else 'to be written, with this name'} |")
    else:
        L.append("No scenarios yet: nothing says what done means.")
    opened = [f for f in b["findings"] if f["status"] == "open"]
    L += ["", "## Review findings", ""]
    if b["findings"]:
        order = {"high": 0, "medium": 1, "low": 2}
        for f in sorted(opened, key=lambda f: order.get(f["severity"], 3)):
            L.append(f"- **open, {f['severity']}** ({f['reviewer']}, {f['id']}): {f['claim']}" + (f" Proposed: {f['proposal']}" if f["proposal"] else ""))
        closed = [f for f in b["findings"] if f["status"] != "open"]
        if closed:   # a settled finding is one line: the full text stays in `leyline spec findings`
            L += ["", f"{len(closed)} settled (full text: `leyline spec findings`):"] if opened else [f"{len(closed)} raised, all settled (full text: `leyline spec findings`):"]
            for f in sorted(closed, key=lambda f: order.get(f["severity"], 3)):
                L.append(f"- {f['status']}, {f['severity']}: {_first_sentence(f['resolution'] or f['claim'])}")
    elif b.get("reviews"):
        L.append(f"Reviewed ({', '.join(b['reviews'])}); no findings were filed.")
    else:
        L.append("No review has been run.")
    st = brief_status(b)
    L += ["", "## Before implementation", ""]
    if st["blocking"]:
        L += [f"- {g}" for g in st["blocking"]]
    else:
        L.append("Nothing blocks implementation: every task is tied to code, everything that must be edited has a task, and "
                 "every scenario has a test or a task to write one.")
    if st["decide"]:
        L += ["", "Still to decide, not blocking:"] + [f"- {d}" for d in st["decide"]]
    if st["notes"]:
        L += [""] + [f"- {x}" for x in st["notes"]]
    return "\n".join(L) + "\n"


# -- findings ------------------------------------------------------------------------------------
def add_finding(con, change_id: str, reviewer: str, severity: str, claim: str, evidence: Optional[list[str]] = None,
                proposal: str = "") -> dict:
    """A reviewer's finding against a change: a claim, the nodes behind it, and what to change in the spec."""
    if severity not in ("high", "medium", "low"):
        return {"error": "severity must be high, medium or low"}
    if not con.execute("SELECT 1 FROM change_proposals WHERE id = ?", (change_id,)).fetchone():
        return {"error": f"no change {change_id!r}; run the brief first"}
    known = {r[0] for r in con.execute("SELECT id FROM nodes")}
    kept = [e for e in (evidence or []) if e in known]
    if not kept:
        return {"error": "a finding needs evidence: at least one node id on the map that shows the problem"}
    fid = "f-" + hashlib.sha1(f"{change_id}|{reviewer}|{claim}".encode()).hexdigest()[:6]
    with con:
        con.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,COALESCE((SELECT status FROM findings WHERE id = ?), 'open'),"
                    "(SELECT resolution FROM findings WHERE id = ?),?)",
                    (fid, change_id, reviewer, severity, claim.strip(), json.dumps(kept), proposal.strip(), fid, fid,
                     datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
    return {"id": fid, "status": "open"}


def resolve_finding(con, finding_id: str, status: str, resolution: str = "") -> dict:
    """The person's call on a finding: accepted (the spec changes), rejected or deferred, with the reason."""
    if status not in ("accepted", "rejected", "deferred", "open"):
        return {"error": "status must be accepted, rejected, deferred or open"}
    with con:
        n = con.execute("UPDATE findings SET status = ?, resolution = ? WHERE id = ?", (status, resolution, finding_id)).rowcount
    return {"id": finding_id, "status": status} if n else {"error": f"no finding {finding_id!r}"}


def findings(con, change_id: str) -> dict:
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM nodes")}
    out = [{"id": r["id"], "reviewer": r["reviewer"], "severity": r["severity"], "claim": r["claim"], "proposal": r["proposal"],
            "status": r["status"], "resolution": r["resolution"] or "",
            "evidence": [{"id": e, "name": names.get(e, e)} for e in json.loads(r["evidence"] or "[]")]}
           for r in con.execute("SELECT * FROM findings WHERE change_id = ? ORDER BY created", (change_id,))]
    return {"change_id": change_id, "open": sum(1 for f in out if f["status"] == "open"), "findings": out}


# -- verification --------------------------------------------------------------------------------
def verify(con, change_dir: str | Path, before_run: Optional[str] = None, after_run: Optional[str] = None, write: bool = True) -> dict:
    """After implementation and a re-index: was the change made as the spec says?"""
    parsed = parse(change_dir)
    if "error" in parsed:
        return parsed
    cid = "spec-" + parsed["id"]
    review = diff.review(con, cid, before_run, after_run)
    if "error" in review:
        return {"error": review["error"] + " Run `leyline plan` on the change before it is implemented."}
    names = _Names(con)
    g = review["graph"]["nodes"]
    touched = {n["id"] for key in ("added", "resigned", "edited", "types_edited") for n in g[key]}
    removed = {n["id"] for n in g["removed"]}
    added_names = {n["name"].split(".")[-1] for n in g["added"]} | {n["name"] for n in g["added"]}
    tasks = []
    tests_now = _tests(con)
    for row in con.execute("SELECT * FROM spec_items WHERE change_id = ? AND kind = 'task' ORDER BY key", (cid,)):
        extra = json.loads(row["attrs"] or "{}")
        ids, new = json.loads(row["nodes"] or "[]"), extra.get("new", [])
        hit = [i for i in ids if i in touched or i in removed or any(t.startswith(i + ".") or t.startswith(i + "/") or t.startswith(i + "(") or t.split("(")[0] == i.split("(")[0] for t in touched | removed)]
        made = [n["name"] for n in new if n["name"].split(".")[-1] in added_names]
        want = len({i.split("(")[0] for i in ids}) + len(new)
        got = len({i.split("(")[0] for i in hit}) + len(made)
        for key in extra.get("scenarios", []):       # a task to write a scenario's test is done when that test exists
            want += 1
            got += _norm(key.split("/", 1)[-1]) in tests_now
        if not want and extra.get("into"):           # "add something to `Foo`": done when something inside Foo is new or edited
            want = 1
            files = {r[0] for i in extra["into"] for r in con.execute("SELECT node_id FROM ancestry WHERE file_id = ? OR module_id = ?", (i, i))}
            got = int(any(t in files or any(t.startswith(i + ".") for i in extra["into"]) for t in touched))
        state = "done" if want and got >= want else "partly" if got else "not done" if want else "cannot be checked"
        checked = next((t["done"] for t in parsed["tasks"] if t["key"] == row["key"]), False)
        tasks.append({"key": row["key"], "text": row["text"], "state": state, "ticked": checked,
                      "missing": [_label(names, i) for i in ids if i not in hit][:6] + [n["name"] for n in new if n["name"] not in made][:6]})
    tests = _tests(con)
    results = {r["name"]: r for r in con.execute("SELECT * FROM test_results WHERE run = ?", (after_run,))} if after_run else {}
    by_norm = {_norm(k): v for k, v in results.items()}
    from . import coverage as measured
    scenarios = []
    for s in parsed["scenarios"]:
        tid = tests.get(_norm(s["name"]))
        res = by_norm.get(_norm(s["name"]))
        ran_change = None
        if tid and measured.has(con):
            ran = {r[0] for r in con.execute("SELECT node_id FROM covered WHERE test_id = ?", (tid,))}
            ran_change = bool(ran & touched) if ran else None
        static = bool(tid) and bool({r[0] for r in con.execute(
            "SELECT s.callable_id FROM flow_steps s JOIN flows f ON f.id = s.flow_id WHERE f.entry_id = ?", (tid,))} & touched)
        state = ("no test" if not tid else "fails" if res is not None and res["status"] != "pass"
                 else "passes" if res is not None else "test exists, not run")
        scenarios.append({"name": s["name"], "state": state, "test": tid, "reaches_the_change": static,
                          "measured_running_the_change": ran_change, "message": res["message"] if res is not None and res["status"] != "pass" else ""})
    all_findings = findings(con, cid)["findings"]
    open_high = [f for f in all_findings if f["status"] == "open" and f["severity"] == "high"]
    test_ids = set(tests.values())
    # A new or edited test is how a scenario gets proven, not an edit outside the spec.
    tests_touched = [n for n in review["not_predicted"] if n["id"] in test_ids or n["id"].split("/test:")[0] in test_ids and "/test:" in n["id"]]
    # "Add X to `Foo`" covers whatever is new or edited inside Foo.
    into = {i for row in con.execute("SELECT attrs FROM spec_items WHERE change_id = ? AND kind = 'task'", (cid,))
            for i in json.loads(row[0] or "{}").get("into", [])}
    inside = {r[0] for i in into for r in con.execute("SELECT node_id FROM ancestry WHERE file_id = ? OR module_id = ?", (i, i))}

    def in_container(i):
        return i in inside or any(i.startswith(c + ".") or i.startswith(c + "/") for c in into)
    drift = [n for n in review["not_predicted"] if n not in tests_touched and not in_container(n["id"])]
    # A new function that only code named in the spec calls is how a task got done, not a change of its own.
    added = {n["id"] for n in g["added"]}
    named = {i for row in con.execute("SELECT nodes FROM spec_items WHERE change_id = ? AND kind = 'task'", (cid,))
             for i in json.loads(row[0] or "[]")}
    in_spec = {t for t in touched if t not in {n["id"] for n in drift}} | named
    helpers = []
    for n in list(drift):
        if n["id"] not in added or n.get("kind") != "callable":
            continue
        callers = {r[0] for r in con.execute("SELECT DISTINCT src_id FROM calls WHERE dst_id = ?", (n["id"],))} - {n["id"]}
        if callers and all(c in in_spec or in_container(c) or any(c.split("(")[0] == i.split("(")[0] for i in named) for c in callers):
            helpers.append({"name": n["name"], "id": n["id"], "called_by": sorted(_label(names, c) for c in callers)})
            drift.remove(n)
    verdict = []
    if any(t["state"] in ("not done", "partly") for t in tasks):
        verdict.append("some tasks are not done")
    if any(s["state"] != "passes" for s in scenarios):
        verdict.append("some scenarios are not proven")
    if drift:
        verdict.append(f"{_n(len(drift), 'edit is', 'edits are')} outside the spec")
    if review["rules"]["new_violations"]:
        verdict.append("a rule that held now fails")
    if review["graph"]["structure"]["new_dependencies"]:
        verdict.append("new links between modules")
    if review["tests"] and review["tests"]["newly_failing"]:
        verdict.append("tests that passed now fail")
    if open_high:
        verdict.append("a high review finding is still open")
    out = {"change_id": cid, "title": parsed["title"], "tasks": tasks, "scenarios": scenarios, "drift": drift,
           "predicted_not_edited": review["predicted_untouched"], "new_dependencies": review["graph"]["structure"]["new_dependencies"],
           "rules_newly_failing": review["rules"]["new_violations"], "tests": review["tests"], "open_high_findings": open_high,
           "tests_added_or_changed": [n["name"] for n in tests_touched], "helpers_added": helpers,
           "review": {"findings": len(all_findings), "open": sum(f["status"] == "open" for f in all_findings),
                      "kinds": reviews(con, cid)},
           "baseline": review.get("baseline"),
           "after_tests": {"passed": sum(r["status"] == "pass" for r in results.values()), "total": len(results)} if results else None,
           "done_as_agreed": not verdict, "why_not": verdict, "view_id": review.get("view_id")}
    if write:
        path = Path(parsed["dir"]) / "leyline.md"
        body = path.read_text() if path.is_file() else ""
        head = body[body.index(BEGIN) + len(BEGIN):body.index(END)].strip() if BEGIN in body and END in body else ""
        head = head.split("\n## 4. Was it done as agreed")[0].rstrip()
        # The state at the top of the page is now the verdict.
        head = re.sub(r"^\*\*State:.*$", lambda _: check_state(out), head, count=1, flags=re.M)
        _write(path, head + "\n\n" + verify_text(out))
        out["written"] = str(path)
    return out


def check_state(v: dict) -> str:
    if v["done_as_agreed"]:
        return "**State: done as agreed.** Every task is done, every scenario is proven, and nothing outside the spec changed."
    return "**State: not done as agreed yet:** " + "; ".join(v["why_not"]) + ". Details under \"Was it done as agreed\"."


def verify_text(v: dict) -> str:
    L = ["## 4. Was it done as agreed", "",
         "Tasks are marked from what changed in the code since the plan was written; scenarios from the test results.", "",
         "**Yes.** Every task is done, every scenario is proven, and nothing outside the spec changed." if v["done_as_agreed"]
         else "**Not yet:** " + "; ".join(v["why_not"]) + ".", "",
         "| Task | Result | Missing |", "| --- | --- | --- |"]
    for t in v["tasks"]:
        L.append(f"| {t['key']} {t['text'][:70].replace('|', '/')} | {t['state']}{'' if t['ticked'] or t['state'] != 'done' else ' (not ticked in tasks.md)'} | {', '.join(t['missing'])} |")
    L += ["", "| Scenario | Result | Evidence |", "| --- | --- | --- |"]
    for s in v["scenarios"]:
        ev = ("measured running the changed code" if s["measured_running_the_change"] else
              "its test reaches the changed code on the map" if s["reaches_the_change"] else
              "its test does not reach the changed code" if s["test"] else "")
        L.append(f"| {s['name']} | {s['state']} | {s['message'] or ev} |")
    if v["drift"]:
        L += ["", "**Changed, but not in the spec:**"] + [f"- {n['name']} ({n.get('why') or n['kind']}) {n.get('path') or ''}" for n in v["drift"][:25]]
    if v.get("helpers_added"):
        L += ["", "**Helpers added** (new, and called only by code the spec names):"] + [
            f"- {h['name']}, called by {_some(h['called_by'])}" for h in v["helpers_added"][:15]]
    if v["predicted_not_edited"]:
        L += ["", "**Expected to change, and did not:**"] + [f"- {n['name']}" for n in v["predicted_not_edited"][:25]]
    if v["new_dependencies"]:
        L += ["", "**New links between modules:**"] + [f"- {d['from']} to {d['to']}" for d in v["new_dependencies"]]
    if v["rules_newly_failing"]:
        L += ["", "**Rules that held before and fail now:**"] + [f"- {r['kind']} {r['from']} {r['to']}" for r in v["rules_newly_failing"]]
    t = v["tests"]
    if t:
        L += ["", f"Tests: {t['before']['passed']} of {t['before']['total']} passed before, {t['after']['passed']} of {t['after']['total']} after."]
        L += [f"- now fails: {x['name']}: {x['message']}" for x in t["newly_failing"]]
    elif v.get("after_tests"):
        a = v["after_tests"]
        L += ["", f"Tests: {a['passed']} of {a['total']} passed after the change. No run from before it was recorded, so a test "
                  "the change broke cannot be told from one that already failed."]
    else:
        L += ["", "No test results were recorded after the change, so scenarios cannot be marked as passing."]
    r = v.get("review") or {}
    L.append(f"Review: {r['findings']} findings, {r['open']} still open." if r.get("findings")
             else f"Review ({', '.join(r['kinds'])}): no findings." if r.get("kinds")
             else "No review was recorded before implementation.")
    if v.get("baseline"):
        L.append(f"Compared with the code as it was at {v['baseline']}.")
    return "\n".join(L) + "\n"


# -- facts for reviewers ---------------------------------------------------------------------------
def review_facts(con, change_dir: str | Path, reviewer: Optional[str] = None) -> dict:
    """What the graph says about a change, arranged as the questions each reviewer must answer.
    A reviewer reads this, reads the code behind anything suspicious, and files findings. Naming the
    `reviewer` (logic or performance) records that this review ran, so the plan can say so."""
    b = brief(con, change_dir, write=False)
    if "error" in b:
        return b
    if reviewer:
        record_review(con, b["change_id"], reviewer)
    names = _Names(con)
    tasked = sorted({i for t in b["tasks"] for i in t["nodes"]})
    fns = [i for i in tasked if names.by_id[i]["kind"] in ("callable", "test")]
    for i in tasked:   # a named type stands for its functions
        if names.by_id[i]["kind"] == "type":
            fns += [r["id"] for r in names.rows if r["parent_id"] == i and r["kind"] == "callable"]
    n_flows = con.execute("SELECT COUNT(*) FROM flows").fetchone()[0] or 1
    hot = []
    for i in dict.fromkeys(fns):
        through = con.execute("SELECT COUNT(DISTINCT flow_id) FROM flow_steps WHERE callable_id = ?", (i,)).fetchone()[0]
        entry = con.execute("SELECT COUNT(DISTINCT s.flow_id) FROM flow_steps s JOIN flows f ON f.id = s.flow_id"
                            " WHERE s.callable_id = ? AND json_extract(f.attrs, '$.kind') != 'test'", (i,)).fetchone()[0]
        sites = con.execute("SELECT COUNT(*) FROM calls WHERE dst_id = ?", (i,)).fetchone()[0]
        hot.append({"id": i, "name": _label(names, i), "flows_through": through, "share_of_flows": round(through / n_flows, 2),
                    "program_entries_that_reach_it": entry, "call_sites": sites})
    hot.sort(key=lambda h: (-h["flows_through"], -h["call_sites"]))
    perf_tests = [{"id": r["id"], "name": r["name"]} for r in con.execute(
        "SELECT id, name FROM nodes WHERE (kind = 'test' OR json_extract(attrs, '$.is_test') = 1)") if re.search(
        r"per\s?sec|/sec|perf|bench|throughput|latency|\bfast|\bslow|\d+k\b|\bms\b", r["name"], re.I)]
    la = b["left_alone"]
    imp = b["impact"] if "error" not in b["impact"] else {}
    return {
        "change_id": b["change_id"], "title": b["title"],
        "logic": {
            "must_edit_with_no_task": b["must_edit_uncovered"],
            "channels_crossed": imp.get("channels") or [],
            "shared_state_written": b["shared_state"],
            "scenarios_with_no_test": [s["name"] for s in b["scenarios"] if not s["test_exists"]],
            "new_members_named_like_existing_ones": la["beside"][:20],
            "callers_of_changed_functions_the_spec_leaves_alone": la["callers"][:30],
            "state_shared_with_functions_the_spec_leaves_alone": la["state"][:30],
            "changed_code_no_test_reaches": imp.get("untested") or [],
            "patterns_involved": b["patterns"],
            "rules_failing_before_the_change": b["rules_failing_now"],
            "spec_problems": b["gaps"],
            "ask": "For each list: is the spec wrong, or is the map? Read the code before filing. Then ask what the scenarios "
                   "leave out: error paths, empty inputs, ordering, the second caller, the other end of each channel.",
        },
        "performance": {
            "changed_functions_by_how_much_runs_through_them": hot[:25],
            "tests_that_measure_speed": perf_tests[:20],
            "ask": "A function most flows pass through is on a hot path. For each one the spec changes: does the change add work "
                   "per call, allocation, I/O or a process hop? Name the test that would show a regression, or say none exists.",
        },
        "how_to_file": "Call spec_finding with a claim, a severity, node ids as evidence, and the change to the spec you propose.",
    }
