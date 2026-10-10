"""A small change with no spec folder: the plan's three answers for a one-line fix.

A spec is the right weight for a change that needs design. "Make the retry count 3" does not: writing a proposal,
spec deltas and a task list for it costs more than the change. `leyline quick` asks the same three questions with
no folder behind it:

    leyline quick "make the retry count 3" --about RETRIES fetch     # before: what it touches, what it reaches,
    <your test command> | leyline quick "..." --tests -              #   the tests that run it (and records them)
    <your test command> | leyline quick --done quick-make-the-retry-count-3 --tests -     # after: one verdict

Before, the named code is looked up on the map as a task's names are (a name in backticks in the sentence counts
too), its impact is assessed, the test run is recorded, and the baseline is kept. After, the code is mapped again
and compared with that baseline the way `leyline pr` compares a branch with its base. The change is stored as
`quick-<slug>`, so the review steps take it as they take `pr-<id>`: `leyline spec facts quick-<slug>`, findings,
`leyline spec forget quick-<slug>`. When a change grows past quick (several functions, a channel crossed, a caller
left), the page says so and gives the way into a spec, keeping the baseline (`--to-spec`).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Optional

from . import change, diff, spec, store, verdicts

PREFIX = "quick-"
GROWN_FUNCTIONS = 3   # more functions edited than this, and the change wants a spec


# -- ids and names ----------------------------------------------------------------------------------
def slug(text: str) -> str:
    """quick-<the first words of the sentence>: `make the retry count 3` -> quick-make-the-retry-count-3."""
    words = re.findall(r"[a-z0-9]+", re.sub(r"`[^`]*`", lambda m: m.group(0).strip("`"), text.lower()))
    out = ""
    for w in words:
        if len(out) + len(w) + 1 > 40:
            break
        out = f"{out}-{w}" if out else w
    return PREFIX + (out or "change")


def change_id(about: str, given: Optional[str] = None) -> str:
    if given:
        return PREFIX + re.sub(r"[^A-Za-z0-9._-]+", "-", given.removeprefix(PREFIX)).strip("-")
    return slug(about)


def names_in(about: str) -> list[str]:
    """Code named in backticks in the sentence."""
    return list(dict.fromkeys(w.strip() for w in spec.CODE.findall(about) if w.strip()))


def _action(about: str) -> str:
    lower = re.sub(r"`", "", about.lower()).strip()
    return spec.action_of(lower)


def _assignment(name: str):
    """A line that sets `name` at the top of a file or a type, or inside a function: `RETRIES = 3`, `const MAX = 3`,
    `public const int Max = 3;`."""
    mods = r"(?:(?:export|public|private|internal|protected|static|readonly|const|let|var|final|pub|default)\s+)*"
    return re.compile(r"^(\s{0,8})" + mods + r"(?:[\w<>\[\],.?]+\s+)?" + re.escape(name) + r"\s*(?::[^=\n]+)?=(?!=)")


def _statement(lines: list[str], at: int) -> tuple[int, int]:
    """The lines (from 1) of the statement that starts at line `at`: on to where brackets close."""
    depth, end = 0, at
    for k in range(at - 1, min(len(lines), at + 200)):
        depth += sum(lines[k].count(c) for c in "([{") - sum(lines[k].count(c) for c in ")]}")
        end = k + 1
        if depth <= 0 and not lines[k].rstrip().endswith(("\\", ",")):
            break
    return at, end


def _find_value(con, names: spec._Names, name: str) -> dict:
    """A name the map has no node for, looked up in the source: a constant or variable set at the top of a file
    or a type, or inside one function. {"value": {...}} | {"ambiguous": [...]} | {}"""
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
        return {}
    pattern, word = _assignment(name), re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    root_of = diff.roots(con)
    found, users = [], set()
    for f in [r for r in names.rows if r["kind"] == "file"]:
        data = diff.source(con, f["id"].split(":", 1)[0], f["path"], root_of)
        if not data or name.encode() not in data:
            continue
        lines = data.decode("utf-8", errors="replace").split("\n")
        inner = con.execute("SELECT id, kind, span_start, span_end FROM nodes WHERE path = ? AND repo_id = ? AND layer = 'fact'"
                            " AND kind IN ('callable', 'test', 'field', 'type') AND span_start IS NOT NULL"
                            " AND name NOT IN ('<module>', '<top-level>')", (f["path"], f["id"].split(":", 1)[0])).fetchall()
        for k, line in enumerate(lines, 1):
            if pattern.match(line):
                around = [r for r in inner if r["span_start"] <= k <= (r["span_end"] or r["span_start"])]
                host = min(around, key=lambda r: (r["span_end"] or r["span_start"]) - r["span_start"]) if around else None
                found.append({"name": name, "path": f["path"], "file": f["id"], "repo": f["id"].split(":", 1)[0], "line": k,
                              "in": host["id"] if host is not None and host["kind"] != "type" else None,
                              "type": host["id"] if host is not None and host["kind"] == "type" else None,
                              "test": names.in_tests(f["id"])})
        for r in inner:   # functions whose text names it: what runs the value
            if r["kind"] in ("callable", "test") and any(word.search(lines[k - 1]) for k in
                                                       range(r["span_start"], min(len(lines), r["span_end"] or r["span_start"]) + 1)):
                users.add(r["id"])
    product = [x for x in found if not x["test"]] or found
    if not product:
        return {}
    if len({(x["path"], x["in"]) for x in product}) > 1:
        return {"ambiguous": [f"{x['path']}:{x['line']}" for x in product][:8]}
    v = product[0]
    if v["in"]:   # a local inside one function: the function is what changes
        return {"ids": [v["in"]]}
    def nested(u):   # a function defined inside another reader runs as part of it
        p = (names.by_id.get(u) or {"parent_id": None})["parent_id"]
        while p in names.by_id:
            if p in users:
                return True
            p = names.by_id[p]["parent_id"]
        return False
    v["users"] = sorted(u for u in users if not nested(u))
    return {"value": v}


def resolve(con, names: spec._Names, written: list[str], about: str) -> dict:
    """The named code, as a task's names are read: targets for the impact assessment, the ids each name stands for,
    values (constants) the map has no node for, and the names it could not place."""
    action = _action(about)
    named, values, unplaced, new = [], [], [], []
    ids = [w for w in written if w in names.by_id]   # a node id, as `search` gives it, is taken as it is
    targets = [{"id": i, "action": action if action in change.ACTIONS and action != "add" else "behavior", "note": "named"}
               for i in ids]
    named += [{"written": i, "ids": [i], "labels": [spec._label(names, i)]} for i in ids]
    written = [w for w in written if w not in names.by_id]
    parsed = {"tasks": [{"key": str(k), "text": f"`{w}`", "action": action, "names": [w]} for k, w in enumerate(written, 1)],
              "scenarios": []}
    more, links = spec._targets(names, parsed)
    targets += more
    for w, l in zip(written, links):
        ids = l["nodes"] + l["into"]
        if l["into"]:   # "add `Foo`" where Foo exists: Foo is what changes
            targets += [{"id": i, "action": "behavior", "note": f"named: {w}"} for i in l["into"]
                        if names.by_id[i]["kind"] != "module" and not any(t.get("id") == i for t in targets)]
        if ids:
            named += [{"written": w, "ids": ids, "labels": [spec._label(names, i) for i in ids]}]
            continue
        if l["new"] and any(n.get("parent") for n in l["new"]):
            new += [{"written": w, **n} for n in l["new"]]
            continue
        found = _find_value(con, names, w.split(".")[-1]) if not l["ambiguous"] else {}
        if found.get("ids"):
            targets += [{"id": i, "action": "behavior", "note": f"sets {w}"} for i in found["ids"]]
            named.append({"written": w, "ids": found["ids"], "labels": [spec._label(names, i) for i in found["ids"]],
                          "local": True})
        elif found.get("value"):
            v = found["value"]
            values.append(v)
            targets += [{"id": u, "action": "behavior", "note": f"reads {w}"} for u in v["users"]
                        if not any(t.get("id") == u for t in targets)]
        else:
            could = l["ambiguous"][0]["could_be"] if l["ambiguous"] else found.get("ambiguous", [])
            unplaced.append({"written": w, "could_be": could})
    return {"action": action, "targets": targets, "named": named, "values": values, "new": new, "unplaced": unplaced}


# -- the baseline's source text, to read what a changed declaration was --------------------------------
def _src_path(con, cid: str) -> Path:
    return diff.snapshot_path(con, cid).with_suffix(".src.json")


def _git(root: Path, *args) -> Optional[str]:
    from . import pr
    try:
        return pr._git(root, *args)
    except pr.GitError:
        return None


def _keep_source(con, cid: str, files: set) -> dict:
    """The text of the files the change is likely to edit, and the commit each repository was at with the files that
    differed from it: enough to say later what a changed declaration was."""
    root_of = diff.roots(con)
    texts, repos = {}, {}
    for repo, root in root_of.items():
        head = _git(root, "rev-parse", "HEAD")
        dirty = (_git(root, "status", "--porcelain", "--untracked-files=all") or "").splitlines() if head else []
        repos[repo] = {"head": head, "dirty": sorted({d[3:].strip().split(" -> ")[-1].strip('"') for d in dirty})}
        files |= {(repo, p) for p in repos[repo]["dirty"]}
    for repo, path in files:
        data = diff.source(con, repo, path, root_of)
        if data is not None and len(data) < 2_000_000:
            texts[f"{repo}:{path}"] = data.decode("utf-8", errors="replace")
    out = {"texts": texts, "repos": repos}
    p = _src_path(con, cid)
    p.parent.mkdir(parents=True, exist_ok=True)
    store.write_file(p, json.dumps(out))
    return out


def _old_source(con, cid: str):
    """path -> the file's text when the baseline was taken, or None: the copy kept then, else git's copy at the commit
    the repository was at, for a file that did not differ from it."""
    p = _src_path(con, cid)
    kept = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"texts": {}, "repos": {}}
    root_of = diff.roots(con)

    def read(path: str) -> Optional[str]:
        for repo, root in root_of.items():
            if f"{repo}:{path}" in kept["texts"]:
                return kept["texts"][f"{repo}:{path}"]
        for repo, root in root_of.items():
            r = kept["repos"].get(repo) or {}
            if r.get("head") and path not in r.get("dirty", []) and (root / path).exists():
                return _git(root, "show", f"{r['head']}:{path}")
        return None
    return read


# -- before ---------------------------------------------------------------------------------------
def start(db: str | Path, about: str, written: Optional[list[str]] = None, results: Optional[list[dict]] = None,
          path: str | Path = ".", given_id: Optional[str] = None, new_baseline: bool = False) -> dict:
    """Look up the named code, assess what it reaches, record the tests, and keep the baseline."""
    from . import loop
    written = list(dict.fromkeys([*(written or []), *names_in(about)]))
    if not written:
        return {"error": "name the code the change touches: in backticks in the sentence (\"make `RETRIES` 3\"), or after"
                         " --about (`leyline quick \"make the retry count 3\" --about RETRIES fetch`)."}
    db = Path(db)
    if not db.exists():
        loop.map_repos([str(Path(path).resolve())], db, page=False)
        reindexed = True
    else:
        reindexed = bool(loop.refresh(db))
    cid = change_id(about, given_id)
    con = store.connect(db)
    try:
        names = spec._Names(con)
        r = resolve(con, names, written, about)
        if not r["targets"]:
            return {"error": "none of the names is code on the map: " + ", ".join(f"`{u['written']}`" for u in r["unplaced"])
                             + ". Write each as it is in the code (`Owner.name`, `module.func`, `path/to/file.py: func`);"
                               " `leyline search <word>` finds them.", "unplaced": r["unplaced"]}
        kept = not new_baseline and diff.snapshot_path(con, cid).exists() and diff.moved_on(con, cid)
        recorded = None
        if results is not None and not kept:
            from .loop import _record
            recorded = _record(con, spec.run_label(cid, "before"), results)
        title = about.strip().split("\n")[0][:120]
        earlier = stored(con, cid) if kept else None   # the same change, started again: names added since stay named
        if earlier:
            for key in ("named", "values", "new"):
                have = {json.dumps(x, sort_keys=True) for x in r[key]}
                r[key] = r[key] + [x for x in earlier.get(key) or [] if json.dumps(x, sort_keys=True) not in have]
            written = list(dict.fromkeys(written + (earlier.get("names") or [])))
        report = change.propose(con, about, r["targets"], title, source="quick", change_id=cid, keep_baseline=not new_baseline)
        if "error" in report:
            return report
        named_ids = [i for n in r["named"] for i in n["ids"]]
        links = [{"key": "1", "action": r["action"], "nodes": named_ids + [u for v in r["values"] for u in v["users"]],
                  "new": [{"name": n["name"], "parent": n["parent"], "label": n.get("label") or n["name"]} for n in r["new"]],
                  "into": [], "mention_ids": [], "scenarios": [], "notes": [], "ambiguous": []}]
        crossings, agree = spec._crossings(con, names, links)
        files = {(names.by_id[m["id"]]["id"].split(":", 1)[0], names.by_id[m["id"]]["path"]) for m in report["marks"]
                 if m["id"] in names.by_id and names.by_id[m["id"]]["path"] and m["role"] in ("changed", "must_edit", "contract", "direct")}
        files |= {(v["repo"], v["path"]) for v in r["values"]}
        if report.get("snapshot") in ("new", "same") or not _src_path(con, cid).is_file():
            _keep_source(con, cid, files)
        roots = store.roots(con)
        with con:
            row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
            attrs = json.loads(row[0] or "{}") if row else {}
            attrs.update({"kind": "quick", "about": about, "names": written, "action": r["action"],
                          "named": r["named"], "values": r["values"], "new": r["new"], "unplaced": r["unplaced"],
                          "must_edit_ids": [m["id"] for m in report["must_edit"]],
                          "root": str(next(iter(roots.values()))) if roots else str(Path(path).resolve())})
            con.execute("UPDATE change_proposals SET attrs = ? WHERE id = ?", (json.dumps(attrs), cid))
        from . import affected
        picked = affected.select(con, cid)
        out = {"change_id": cid, "title": title, "about": about, **r, "reindexed": reindexed,
               "must_edit": report["must_edit"], "reached": report["summary"]["reached"],
               "callers": [spec._label(names, m["id"]) for m in report["marks"] if m["role"] == "direct"],
               "reached_across": _across(names, report["channels"]),
               # the modules the code that runs into it is in
               "modules": sum(1 for g in report["by_module"] if g["reached"]) or report["summary"]["modules"],
               "channels": crossings, "must_agree": agree,
               "tests_to_run": [{**t, "name": spec._label(names, t["id"]) if t["id"] in names.by_id else t["name"]}
                                for t in report["tests_to_run"]],
               "untested": report["untested"], "risks": report["risks"],
               "commands": [c for c in (picked.get("commands") or []) if c.get("command")] if "error" not in picked else [],
               "baseline": "kept" if kept else report.get("snapshot"), "tests_recorded": recorded,
               "baseline_tests": con.execute("SELECT 1 FROM test_results WHERE run = ? LIMIT 1",
                                             (spec.run_label(cid, "before"),)).fetchone() is not None}
        out["grown"] = _grown_before(out)
        return out
    finally:
        con.close()


def _grown_before(b: dict) -> list[str]:
    why = []
    fns = len({i for t in b["targets"] if t.get("id") for i in [t["id"]]}) + len(b["new"])
    if fns > GROWN_FUNCTIONS:
        why.append(f"it names {fns} pieces of code")
    if b["must_edit"]:
        why.append(f"{spec._n(len(b['must_edit']), 'caller')} must be edited with it")
    if b["channels"]:
        why.append("it crosses " + _channels(b["channels"]))
    return why


def _across(names: spec._Names, links: list[dict]) -> list[dict]:
    """Code that reaches the change from the other side of a channel (a program that starts the one that runs it, a
    request to a route that runs it): it sees any change in behavior, with no compile-time link."""
    groups: dict = {}
    for c in links:
        key = (c["channel"], c.get("address") or c["to_name"])
        g = groups.setdefault(key, {"channel": c["channel"], "address": key[1], "to": c["to_name"], "from": [],
                                    "data": bool(c.get("data"))})
        g["from"].append(spec._label(names, c["from"]) if c["from"] in names.by_id else c["from_name"])
    return [{**g, "from": list(dict.fromkeys(g["from"]))} for g in groups.values()]


def _channels(cs: list[dict]) -> str:
    """`the http GET /api/x and the db orders`: each channel once."""
    said = list(dict.fromkeys(f"the {c['channel']} {c.get('program') or c.get('address') or ''}".strip() for c in cs))
    return spec._and(said, 2)


def _value_label(v: dict) -> str:
    return f"the value `{v['name']}` ({v['path']}, line {v['line']})"


def start_text(b: dict) -> str:
    L = [f"# Quick change: {b['title']}", ""]
    touch = [_names([l for n in b["named"] for l in n["labels"]])] if b["named"] else []
    touch += [_value_label(v) + (f", read by {_names([_label_id(b, u) for u in v['users']], 4)}" if v["users"] else "")
              for v in b["values"]]
    touch += [f"new `{n.get('label') or n['name']}`" for n in b["new"]]
    L.append("Will touch: " + "; ".join(touch) + ".")
    for u in b["unplaced"]:
        L.append(f"Not placed: `{u['written']}`" + (f" could be {len(u['could_be'])} things ({', '.join(u['could_be'][:3])});"
                                                    " write it as `Owner.name` or `path/to/file: name`." if u["could_be"]
                                                    else " is not on the map, so it is left out."))
    if b["must_edit"]:
        L.append("Must edit with it: " + "; ".join(f"`{m['name']}` ({m['note']})" for m in b["must_edit"][:5])
                 + (f"; and {len(b['must_edit']) - 5} more" if len(b["must_edit"]) > 5 else "") + ".")
    else:
        L.append("Must edit with it: nothing, as far as the map sees.")
    L.append(f"Runs into it: {spec._n(b['reached'], 'place')} in {spec._n(b['modules'], 'module')}"
             + (f"; the nearest {_nearest(b['callers'])}" if b["callers"] else "") + "." if b["reached"]
             else "Runs into it: nothing else on the map calls it.")
    if b["channels"]:
        L += ["Channels: " + spec._crossing_line(c) for c in b["channels"][:3]]
        L += [f"  Must agree: `{a['name']}`, which {a['why']}." for a in b["must_agree"][:3]]
    else:
        L.append("Channels: none touched.")
    for g in (b.get("reached_across") or [])[:3]:
        many = len(g["from"]) > 1
        verb = (f"{'read' if many else 'reads'} what `{g['to']}` writes" if g.get("data") else
                f"{'run' if many else 'runs'} `{g['to']}`")
        L.append(f"Reached across the {g['channel']} {g['address']}: {_names(g['from'], 3)} {verb} with no compile-time link,"
                 f" so {'they see' if many else 'it sees'} any change in what it does.")
    tests = [t["name"] for t in b["tests_to_run"]]
    L.append(f"Tests that run it: {_names(tests, 5)}." if tests else "Tests that run it: none on the map. Write one, or say"
                                                                     " how you will know it works.")
    if b["untested"] and tests:
        L.append("No test reaches " + _names([u["name"] for u in b["untested"]], 4) + ".")
    for c in b["commands"][:2]:
        L.append(f"  Run them: cd {c['cwd']} && {c['command']}" if len(c["command"]) <= 160 else
                 f"  Run them: `leyline affected-tests {b['change_id']}` prints the command ({spec._n(c['tests'], 'test')}).")
    t = b.get("tests_recorded")
    if t:
        L.append(f"Recorded the tests as they are now: {diff.recorded_text(t)}.")
    elif b["baseline"] == "kept":
        L.append("The code has changed since this change was first started; `--done` still compares with the code as it was"
                 " then (`--new-baseline` starts over).")
    if b["grown"]:
        L.append("This is more than a quick change (" + "; ".join(b["grown"]) + "): consider a spec, `leyline plan`.")
    L += ["", *next_after_start(b)]
    return "\n".join(L) + "\n"


def next_after_start(b: dict, for_agent: bool = False) -> list[str]:
    cid = b["change_id"]
    if not b["baseline_tests"]:
        if for_agent:
            return [f"Before editing, run the tests and call `quick` again with the same sentence and their output as"
                    f" test_output. Then make the change, run the tests, and call `quick` with done={cid!r} and their output."]
        return [f"Next: before editing, record how the tests pass now: `<your test command> | leyline quick \"{b['about']}\""
                f" --tests -`. Then make the change and run `<your test command> | leyline quick --done {cid} --tests -`."]
    if for_agent:
        return [f"Make the change, run the tests, and call `quick` with done={cid!r} and their output as test_output."]
    return [f"Next: make the change, then run `<your test command> | leyline quick --done {cid} --tests -`."]


def _nearest(callers: list[str]) -> str:
    """`is `build`, which calls the changed code` / `are `build` and `cost`, which call the changed code`."""
    xs = list(dict.fromkeys(callers))
    if len(xs) == 1:
        return f"is `{xs[0]}`, which calls the changed code"
    said = _names(xs, 4) if len(xs) > 4 else ", ".join(f"`{x}`" for x in xs[:-1]) + f" and `{xs[-1]}`"
    return f"are {said}, which call the changed code"


def _names(xs: list[str], k: int = 4) -> str:
    xs = list(dict.fromkeys(xs))
    return ", ".join(f"`{x}`" for x in xs[:k]) + (f" and {len(xs) - k} more" if len(xs) > k else "")


def _label_id(b: dict, i: str) -> str:
    return i.split(":")[-1].rsplit(".", 1)[-1].split("(")[0]


# -- after ----------------------------------------------------------------------------------------
def stored(con, cid: str) -> Optional[dict]:
    row = con.execute("SELECT intent, attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
    if row is None:
        return None
    a = json.loads(row["attrs"] or "{}")
    return {**a, "intent": row["intent"]} if a.get("kind") == "quick" else None


def _inside(i: str, named: set) -> bool:
    from .pr import _inside as inside
    return inside(i, named) or inside(diff._base(i), {diff._base(n) for n in named})


TOP_ASSIGN = re.compile(r"^(?:(?:export|const|let|var|final|static|pub)\s+)*([A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=(?!=)")


def _top_values(con, path: str, lines: list[int]) -> dict[str, list[int]]:
    """The values set at the top of a file (no indentation: `LINE_MAX = 200`, `export const MAX = 3`) whose
    statements hold some of `lines`: name -> those lines."""
    if not lines:
        return {}
    row = con.execute("SELECT repo_id FROM nodes WHERE path = ? AND kind = 'file' LIMIT 1", (path,)).fetchone()
    data = diff.source(con, row[0], path) if row else None
    if data is None:
        return {}
    text = data.decode("utf-8", errors="replace").split("\n")
    out: dict[str, list[int]] = {}
    for k, line in enumerate(text, 1):
        m = TOP_ASSIGN.match(line)
        if not m:
            continue
        s, e = _statement(text, k)
        held = [ln for ln in lines if s <= ln <= e]
        if held:
            out.setdefault(m.group(1), []).extend(held)
    return out


def _value_span(con, v: dict) -> Optional[tuple[int, int]]:
    """Where a value is set now: the lines of its statement in its file."""
    data = diff.source(con, v["repo"], v["path"])
    if data is None:
        return None
    lines = data.decode("utf-8", errors="replace").split("\n")
    pattern = _assignment(v["name"])
    hits = [k for k, line in enumerate(lines, 1) if pattern.match(line)]
    if not hits:
        return None
    return _statement(lines, min(hits, key=lambda k: abs(k - v["line"])))


def judge(con, cid: str, a: dict, facts: dict, before_run: Optional[str], after_run: Optional[str],
          old_top: dict) -> dict:
    """The few items a quick change is judged on, each with a verdict, and the lists behind them."""
    names = spec._Names(con)
    named = {i for n in a.get("named") or [] for i in n["ids"]}
    new_named = {(n.get("parent"), n["name"]) for n in a.get("new") or []}
    c = facts["changed"]

    def is_test(x):
        return bool(x.get("test")) or names.in_tests(x["id"])

    def inside(x):   # named code, something nested in it, or the new code the sentence named, where it said
        if _inside(x["id"], named):
            return True
        row = names.by_id.get(x["id"])
        return any(row is not None and row["parent_id"] == p and row["name"] == n for p, n in new_named)
    # The top of a file is judged by its lines (old_top), not as one function.
    top = lambda x: (names.by_id.get(x["id"]) or {"name": ""})["name"] in ("<module>", "<top-level>")
    items = [x for x in c["edited"] + c["types"] + c["added"] + c["removed"] if not top(x)]
    product = [x for x in items if not is_test(x)]
    ours = [x for x in product if inside(x)]
    # A new function only the named code calls is a helper of it, not an edit elsewhere.
    added_ids = {x["id"] for x in c["added"]}
    changed_inside = {x["id"] for x in ours} | named
    helpers = []
    for x in product:
        if x["id"] in added_ids and not inside(x):
            callers = {r[0] for r in con.execute("SELECT DISTINCT src_id FROM calls WHERE dst_id = ?", (x["id"],))}
            if callers and all(_inside(s, changed_inside) for s in callers):
                helpers.append(x)
    # A caller edited to match a named function whose signature changed, or one the start said must change with it,
    # is part of the change, not an edit elsewhere.
    resigned = {x["id"] for x in ours if x.get("signature")}
    must = set(a.get("must_edit_ids") or [])
    follows = []
    for x in product:
        if not inside(x) and x not in helpers and x["id"] in names.by_id:
            calls = {r[0] for r in con.execute("SELECT DISTINCT dst_id FROM calls WHERE src_id = ?", (x["id"],))}
            if x["id"] in must or calls & resigned:
                follows.append(x)
    outside = [x for x in product if not inside(x) and x not in helpers and x not in follows]
    # Edits at the top of a file: inside the named code when they are the lines that set a named value.
    spans = {}
    for v in a.get("values") or []:
        s = _value_span(con, v)
        if s:
            spans.setdefault(v["path"], []).append(s)
    value_edited, top_outside = [], []
    for path, lines in old_top.items():
        if TEST.search(path):
            continue
        mine = [ln for ln in lines if any(s <= ln <= e for s, e in spans.get(path, []))]
        if mine:
            value_edited.append(path)
        rest = [ln for ln in lines if ln not in mine]
        by_name = _top_values(con, path, rest)   # a constant set at the top of the file is named by its name
        for name, lns in by_name.items():
            top_outside.append({"name": name, "path": path, "lines": len(lns), "about": name})
        left = [ln for ln in rest if not any(ln in lns for lns in by_name.values())]
        if left:
            top_outside.append({"name": f"the top level of {path}", "path": path, "lines": len(left)})
    tests_edited = [x["name"] for x in items if is_test(x) and x["name"] not in ("<module>", "<top-level>")]
    r = facts["reaches"]
    label = lambda i, fallback: spec._label(names, i) if i in names.by_id else fallback
    broken = ([{"name": label(m["id"], m["name"]), "why": "calls code whose signature changed" if m.get("note") in (None, "", "its call must change")
                else m["note"], "test": m.get("test")} for m in r["signature_changed_callers_not_edited"]]
              + [{"name": label(i, cl), "why": f"calls {x['removed']}, which was removed", "test": cl in x["test_callers"]}
                 for x in r["removed_but_still_called"] for cl, i in zip(x["callers"], x["caller_ids"])])
    def written(x):   # how to name it with --about: its name when that finds it alone, else its id
        return x["name"] if names.resolve(x["name"]).get("ids") == [x["id"]] else x["id"]
    out_list = [{"name": x["name"], "path": x["path"], "id": x["id"], "about": written(x),
                 "kind": "removed" if x in c["removed"] else "added" if x["id"] in added_ids else "edited"}
                for x in outside] + top_outside
    anything = bool(product or old_top or tests_edited or facts["tests"]["test_files_changed"])

    # 1. scope
    did = bool(ours or value_edited)
    if not anything:
        scope = (verdicts.INCONCLUSIVE, "nothing on the map changed since the baseline; was the edit saved?")
    elif did and not out_list:
        scope = (verdicts.PROVEN, "only the named code changed" + (f", with {spec._n(len(helpers), 'new helper')} it calls"
                                                                     if helpers else ""))
    elif did:
        scope = (verdicts.PARTIAL, f"the named code changed, and so did {spec._n(len(out_list), 'piece')} of code it did not name")
    elif product or top_outside:
        scope = (verdicts.CONTRADICTED, "the named code did not change, though other code did")
    else:
        scope = (verdicts.INCONCLUSIVE, "only tests changed")
    # 2. callers
    callers = ((verdicts.CONTRADICTED, f"{spec._n(len(broken), 'caller')} of changed code {'was' if len(broken) == 1 else 'were'}"
                f" left as {'it was' if len(broken) == 1 else 'they were'} and will break") if broken else
               (verdicts.PROVEN, "no caller of a changed signature or of removed code was left as it was"))
    # 3. tests, against the run from before
    after_rows = con.execute("SELECT name, status, message FROM test_results WHERE run = ?", (after_run,)).fetchall() if after_run else []
    delta = diff.test_delta(con, before_run, after_run) if before_run and after_run else None
    broke = (delta["newly_failing"] + delta["new_failing"]) if delta else []
    failing_now = [x["name"] for x in after_rows if x["status"] == "fail"]
    if not after_rows:
        tests = (verdicts.INCONCLUSIVE, "no test results from after the change were passed")
    elif delta is not None:
        tests = ((verdicts.CONTRADICTED, f"{spec._n(len(broke), 'test')} {'fails' if len(broke) == 1 else 'fail'} that"
                  " passed before, or are new and fail") if broke else
                 (verdicts.PROVEN, diff.passed_pair_text(delta["before"], delta["after"], now_first=True)))
    elif failing_now:
        one = len(failing_now) == 1
        tests = (verdicts.INCONCLUSIVE, f"{spec._n(len(failing_now), 'test')} {'fails' if one else 'fail'}, and no run from"
                 f" before was recorded to tell whether the change broke {'it' if one else 'them'}")
    else:
        tests = (verdicts.PROVEN, f"all {len(after_rows)} pass (no run from before was recorded)")
    # 4. whether a test ran the changed code
    t = facts["tests"]
    ix = spec._results_index(after_rows)
    short = lambda x: (names.by_id.get(x.get("id") or "") or {"name": x.get("name")})["name"]
    to_run = list(t.get("tests_to_run") or [])
    readers = [u for v in a.get("values") or [] if v["path"] in value_edited for u in v.get("users") or [] if u in names.by_id]
    if readers:   # a changed constant runs wherever it is read
        to_run += change.assess(con, "", [{"id": u, "action": "behavior"} for u in readers]).get("tests_to_run") or []
    candidates = [(x.get("name"), "measured coverage shows it running the changed code")
                  for x in t.get("measured_running_the_change") or []]
    candidates += [(short(x), "the map shows it running the changed code") for x in to_run]
    candidates += [(n, "it was edited with the change") for n in tests_edited]
    ran_pass, ran_fail = [], []
    for name, how in candidates:
        got = spec._scenario_results(ix, name.split("::")[-1]) if name else []
        if got and all(g["status"] == "pass" for g in got):
            ran_pass.append((name, how))
        elif got:
            ran_fail.append(name)
    ran_pass = list(dict.fromkeys(ran_pass))
    if not anything:
        ran = (verdicts.INCONCLUSIVE, "nothing changed, so no test could run a change")
    elif not after_rows:
        ran = (verdicts.INCONCLUSIVE, "no test results from after the change were passed")
    elif ran_pass:
        many = len(ran_pass) > 1
        how = ran_pass[0][1].replace("shows it", "shows them").replace("it was", "they were") if many else ran_pass[0][1]
        ran = (verdicts.PROVEN, f"{_names([n for n, _ in ran_pass], 2)} passed, and {how}")
    elif ran_fail:
        ran = (verdicts.CONTRADICTED, f"every test that runs it fails: {_names(ran_fail, 3)}")
    else:
        ran = (verdicts.PERSON, "no test that passed runs the changed code, as far as the map can tell")
    items_out = [{"key": "scope", "what": "Edits stayed in the named code", "verdict": scope[0], "why": scope[1]},
                 {"key": "callers", "what": "No caller left broken", "verdict": callers[0], "why": callers[1]},
                 {"key": "tests", "what": "No test broke", "verdict": tests[0], "why": tests[1]},
                 {"key": "ran", "what": "A test ran the change", "verdict": ran[0], "why": ran[1]}]
    edited_fns = len([x for x in product if x["kind"] in ("callable", "test")])
    grown = []
    if edited_fns > GROWN_FUNCTIONS:
        grown.append(f"{edited_fns} functions edited")
    # A channel whose answer or data the edit changed, or whose request it changed; a read of data alone is not one.
    crossed = [x for x in r["channels_crossed"] if x["hub_touched"] or not x["data"]]
    if crossed:
        grown.append("it crosses " + _channels(crossed))
    if broken:
        grown.append(f"{spec._n(len(broken), 'caller')} that must change {'was' if len(broken) == 1 else 'were'} left")
    return {"items": items_out, "outside": out_list, "helpers": [x["name"] for x in helpers],
            "followed": [label(x["id"], x["name"]) for x in follows], "broken": broken,
            "tests_broke": [x["name"] for x in broke], "failing_now": failing_now, "tests_edited": tests_edited,
            "ran": [{"name": n, "how": how} for n, how in ran_pass], "grown": grown,
            "test_delta": delta}


TEST = re.compile(r"(^|/)(tests?|__tests__|specs?)(/|$)|[._-](test|spec)s?\.[A-Za-z]+$|(^|/)test_[^/]+$|Tests?\.cs$")


def done(db: str | Path, cid: str, results: Optional[list[dict]] = None, coverage_file: Optional[str | Path] = None,
         more: Optional[list[str]] = None) -> dict:
    """After the change: map again, record the tests, compare with the baseline, and judge it. `more` names code the
    change touched that the start did not name: it is added to the named code, and the baseline stays."""
    from . import loop, pr
    db = Path(db)
    if not db.exists():
        return {"error": "no map of this code yet; start the change with `leyline quick \"<what>\" --about <names>`"}
    reindexed = bool(loop.refresh(db))
    con = store.connect(db)
    try:
        a = stored(con, cid)
        snap = diff.snapshot_path(con, cid)
        if a is None or not snap.exists():
            begun = con.execute("SELECT 1 FROM change_proposals WHERE id = ?", (cid,)).fetchone()
            if a is None and cid.startswith(PREFIX) and begun:
                return {"error": f"the start of {cid} did not finish; start it again with the same words (`leyline quick"
                                 " \"<what>\" --about <names>`)" + ("; the baseline it took is kept." if snap.exists() else
                                                                       ", before editing.")}
            known = [r[0] for r in con.execute("SELECT id FROM change_proposals WHERE id LIKE 'quick-%' ORDER BY id")
                     if stored(con, r[0]) is not None and diff.snapshot_path(con, r[0]).exists()]
            return {"error": f"no quick change {cid!r} started here" + (f" (there are: {', '.join(known[:5])})" if known else "")
                             + "; start it with `leyline quick \"<what>\" --about <names>` before editing."}
        if more:
            a = _name_more(con, cid, a, more)
            if "error" in a:
                return a
        before_run, after_run = spec.run_label(cid, "before"), spec.run_label(cid, "after")
        if coverage_file is not None:
            from . import coverage as measured
            imported = measured.import_file(con, coverage_file, run=after_run)
            if "error" in imported:
                return {"error": f"cannot import the coverage file: {imported['error']}"}
        recorded = loop._record(con, after_run, results) if results is not None else None
        has = lambda run: con.execute("SELECT 1 FROM test_results WHERE run = ? LIMIT 1", (run,)).fetchone() is not None
        facts = pr.analyse(con, snap, a.get("about") or a["intent"], _old_source(con, cid))
        facts.pop("marks", None)
        before = diff._open(snap)
        try:   # the changed lines at the top of each file, outside any function: where a constant is set
            rows = con.execute("SELECT id, path FROM nodes WHERE layer = 'fact' AND (kind = 'file' OR name IN ('<module>',"
                               " '<top-level>')) AND path IN (SELECT value FROM json_each(?))",
                               (json.dumps(facts["changed"]["files"]),)).fetchall()
            own = diff.own_changes(before, con, [r[0] for r in rows]) or {}
        finally:
            before.close()
        old_top: dict = {}
        for i, path in rows:
            old_top.setdefault(path, []).extend(ln for ln, text in own.get(i) or [] if text.strip())
        old_top = {p: sorted(set(lines)) for p, lines in old_top.items() if lines}
        j = judge(con, cid, a, facts, before_run if has(before_run) else None, after_run if has(after_run) else None, old_top)
        config = verdicts.load_config(Path(a.get("root") or ".") / "openspec" / "changes" / cid)
        blocking = [x for x in j["items"] if x["verdict"] in config["blocking"]]
        out = {"change_id": cid, "title": a.get("title") or a["intent"], "about": a.get("about") or a["intent"],
               "done": not blocking, "blocking": config["blocking"], "config": config["file"], **j,
               "changed": facts["changed"], "size": facts["size"], "reindexed": reindexed, "tests_recorded": recorded,
               "findings": spec.findings(con, cid)["findings"]}
        with con:
            row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
            attrs = json.loads(row[0] or "{}")
            attrs["last_done"] = {"done": out["done"], "items": {x["key"]: x["verdict"] for x in j["items"]}}
            con.execute("UPDATE change_proposals SET attrs = ? WHERE id = ?", (json.dumps(attrs), cid))
        return out
    finally:
        con.close()


def _name_more(con, cid: str, a: dict, more: list[str]) -> dict:
    """Add names to a started change's named code, keeping its baseline."""
    r = resolve(con, spec._Names(con), more, a.get("about") or a["intent"])
    if not (r["named"] or r["values"] or r["new"]):
        return {"error": "none of those names is code on the map: " + ", ".join(
            f"`{u['written']}`" + (f" could be {len(u['could_be'])} things (give one of these ids: {', '.join(u['could_be'][:3])})"
                                   if u["could_be"] else "") for u in r["unplaced"])}
    for key in ("named", "values", "new", "unplaced"):
        a[key] = (a.get(key) or []) + r[key]
    a["names"] = list(dict.fromkeys((a.get("names") or []) + more))
    with con:
        row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
        attrs = json.loads(row[0] or "{}")
        attrs.update({k: a[k] for k in ("named", "values", "new", "unplaced", "names")})
        con.execute("UPDATE change_proposals SET attrs = ? WHERE id = ?", (json.dumps(attrs), cid))
    return a


def verdict_line(v: dict) -> str:
    counts = {k: sum(x["verdict"] == k for x in v["items"]) for k in verdicts.VERDICTS}
    if v["done"]:
        let = [x for x in v["items"] if x["verdict"] != verdicts.PROVEN]
        if not let:
            return ("**Done.** The edits stayed in the named code, no caller was left broken, no test broke, and a test ran"
                    " the change.")
        return "**Done**, with this left to you: " + "; ".join(f"{x['why']} ({x['verdict']})" for x in let) + "."
    stop = [x for x in v["items"] if x["verdict"] in v["blocking"]]
    return f"**Not done** ({verdicts.summary_line(counts)}): " + "; ".join(x["why"] for x in stop) + "."


def done_text(v: dict) -> str:
    L = [f"# Quick change: {v['title']}", "", verdict_line(v), ""]
    L += [f"- {x['what']}: {x['verdict']}. {x['why'][0].upper() + x['why'][1:]}." for x in v["items"]]
    if v.get("followed"):
        L += ["", "Callers edited to match the named code: " + _names(v["followed"], 6) + "."]
    if v["outside"]:
        L += ["", "Edits outside the named code:"]
        L += [f"- `{x['name']}` ({x.get('kind', 'edited')}, {x['path']})" for x in v["outside"][:6]]
        if len(v["outside"]) > 6:
            L.append(f"- and {len(v['outside']) - 6} more")
    if v["broken"]:
        L += ["", "Callers left broken:"] + [f"- `{x['name']}`: {x['why']}" for x in v["broken"][:6]]
    if v["tests_broke"]:
        L += ["", "Tests that broke: " + _names(v["tests_broke"], 6) + "."]
    elif v["failing_now"] and not v["test_delta"]:
        L += ["", "Failing now: " + _names(v["failing_now"], 6) + "."]
    open_ = [f for f in v.get("findings") or [] if f["status"] == "open"]
    if open_:
        L += ["", f"Open review findings: {', '.join(f['id'] for f in open_)} (`leyline spec findings {v['change_id']}`)."]
    if v["grown"]:
        L += ["", "This has grown past a quick change (" + "; ".join(v["grown"]) + "). Write it as a spec instead:",
              "  1. Have your agent write openspec/changes/<id>/ with tasks naming this code (the leyline-spec skill says how).",
              f"  2. `leyline quick --to-spec <id> {v['change_id']}` hands this baseline and test run to the spec.",
              "  3. `leyline plan <id>`, then `<your test command> | leyline check <id> --tests -`."]
    L += ["", *next_after_done(v)]
    return "\n".join(L) + "\n"


def next_after_done(v: dict, for_agent: bool = False) -> list[str]:
    cid = v["change_id"]
    if v["done"]:
        unseen = any(x["key"] == "ran" and x["verdict"] != verdicts.PROVEN for x in v["items"])
        if for_agent:
            return ["Next: show the person the verdict and the diff" + (", and say that no test seen running the change"
                                                                       " passed, so it needs checking by hand." if unseen else ".")]
        return [("Next: check by hand that the change does what you meant (no passing test was seen running it), then"
                 " review the diff and commit it." if unseen else "Next: review the diff and commit it.")
                + f" `leyline spec forget {cid}` deletes the baseline."]
    out = []
    by = {x["key"]: x for x in v["items"]}
    if by["tests"]["verdict"] == verdicts.INCONCLUSIVE and not v["failing_now"]:
        out.append("run the tests and pass their output (" + ("test_output" if for_agent else "`--tests -`") + ").")
    if v["outside"]:
        import shlex
        more = " ".join(shlex.quote(x["about"]) for x in v["outside"][:3] if x.get("about"))
        out.append(f"undo the edits outside the named code, or, if they belong, name them: `leyline quick --done {cid}"
                   f" --about {more or '<names>'} --tests -`." if not for_agent else
                   "undo the edits outside the named code, or, if the person says they belong, call `quick` again with"
                   " done and those names in `names`.")
    if v["broken"]:
        out.append("update the callers left broken: " + _names([x["name"] for x in v["broken"]], 3) + ".")
    if v["tests_broke"]:
        out.append("fix what broke " + _names(v["tests_broke"], 3) + ".")
    if by["scope"]["verdict"] in (verdicts.CONTRADICTED, verdicts.INCONCLUSIVE) and not v["outside"]:
        out.append("make the change (nothing named has changed yet).")
    again = ("call `quick` again with done=" + repr(cid) + " and the tests' output." if for_agent
             else f"run `<your test command> | leyline quick --done {cid} --tests -` again.")
    if not out:
        out.append("fix what the verdict lists.")
    return ["Next: " + out[0], *("Also: " + x for x in out[1:]),
            *([] if len(out) == 1 and "--done" in out[0] else ["Then: " + again])]


# -- growing into a spec, and the review steps ------------------------------------------------------
def to_spec(con, cid: str, spec_id: str) -> dict:
    """Hand a quick change's baseline and its test run from before to a spec, so `plan` and `check` compare with the
    code as it was when the quick change started, not as it is now."""
    if stored(con, cid) is None or not diff.snapshot_path(con, cid).exists():
        return {"error": f"no quick change {cid!r} with a baseline here"}
    sid = "spec-" + spec_id.removeprefix("spec-")
    target = diff.snapshot_path(con, sid)
    if target.exists():
        return {"error": f"{sid} already has a baseline; `leyline spec forget {spec_id}` first if it should start from {cid}"}
    shutil.copyfile(diff.snapshot_path(con, cid), target)
    with con:
        rows = [(spec.run_label(sid, "before"), *tuple(r)[1:]) for r in con.execute(
            "SELECT * FROM test_results WHERE run = ?", (spec.run_label(cid, "before"),))]
        con.execute("DELETE FROM test_results WHERE run = ?", (spec.run_label(sid, "before"),))
        if rows:
            con.executemany(f"INSERT INTO test_results VALUES ({','.join('?' * len(rows[0]))})", rows)
        n = len(rows)
    return {"change_id": sid, "from": cid, "baseline": str(target), "tests": n}


def forget(con, cid: str) -> bool:
    gone = diff.drop_snapshot(con, cid)
    p = _src_path(con, cid)
    if p.exists():
        p.unlink()
        gone = True
    return gone


def review_facts(con, cid: str, reviewer: Optional[str] = None) -> dict:
    """The facts behind a quick change, arranged for the adversarial reviewers as a pull request's are."""
    from . import learnings, pr
    a = stored(con, cid)
    snap = diff.snapshot_path(con, cid)
    if a is None or not snap.exists():
        return {"error": f"no quick change {cid!r} started here; run `leyline quick` first"}
    f = pr.analyse(con, snap, a.get("about") or "", _old_source(con, cid))
    if reviewer:
        spec.record_review(con, cid, reviewer)
    r, t = f["reaches"], f["tests"]
    return {
        "change_id": cid, "title": a.get("title"), "what_it_says_it_does": a.get("about") or a["intent"],
        "named_code": [l for n in a.get("named") or [] for l in n["labels"]] + [v["name"] for v in a.get("values") or []],
        "changed": f["changed"], "size": f["size"],
        "house_rules_to_read_first": pr._house_rules(Path(a.get("root") or "."), f["changed"]["files"]),
        "learnings_that_apply": learnings.applying(con, cid),
        "logic": {**{k: r[k] for k in ("signature_changed_callers_not_edited", "removed_but_still_called", "channels_crossed",
                                       "callers_left_alone", "state_shared_with_unchanged_code")},
                  "other_ends_of_those_channels_not_edited": r["other_ends_not_edited"],
                  "changed_code_no_test_reaches": t["changed_code_no_test_reaches"],
                  "ask": "Does the code do what the sentence says, and nothing else? Then, for each list: is the change"
                         " wrong, or is the map? Read the code before filing."},
        "performance": {**f["performance"], "ask": "For each changed function on a hot path: does the change add work per"
                                                   " call? Name the test that would show it."},
        "how_sure": f["how_sure"],
        "how_to_file": f"leyline spec finding {cid} --reviewer <logic|performance> --severity <high|medium|low>"
                       " --claim \"...\" --evidence <node id> --proposal \"...\" (or the spec_finding tool with this id).",
    }
