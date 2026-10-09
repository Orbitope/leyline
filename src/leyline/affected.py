"""Tests chosen by what they ran: proving a scenario by execution, and picking the tests a change needs.

A scenario is proven by a passing test of the same name. With per-test coverage imported (`pytest --cov=<package>
--cov-context=test`, or one Istanbul report per test file), the map also knows which functions that test ran, so
`check` can say whether it ran the code the change edited. A test that passes without running the changed code
proves less: it would pass whatever the change did.

The same data picks the tests to run for a change, the way Datadog's Test Impact Analysis or `jest
--findRelatedTests` do: the tests measured running any changed function, and, for changed code no measured test
ran (new code, or code the measurement did not watch), the tests whose path on the map passes through it.
"""

from __future__ import annotations

import datetime
import json
import re
import os
import shlex
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import store

PY_TEST = re.compile(r"\.py$")
JS_TEST = re.compile(r"\.[cm]?[jt]sx?$")
GO_TEST = re.compile(r"_test\.go$")



def _quote(path: str) -> str:
    """A path as the person's shell takes it: POSIX quoting, or on Windows double quotes when it has a space."""
    if os.name == "nt":
        return f'"{path}"' if " " in path else path
    return shlex.quote(path)

def per_test(con) -> bool:
    """True when some imported coverage ties functions to the test (or test file) that ran them."""
    try:
        return con.execute("SELECT 1 FROM covered WHERE test != '' LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _json(ids) -> str:
    return json.dumps(sorted(set(ids)))


def _strip_param(name: str) -> str:
    return re.sub(r"\[.*\]$", "", name or "")


def _kinds(con, ids) -> dict:
    return {r[0]: r[1] for r in con.execute("SELECT id, kind FROM nodes WHERE id IN (SELECT value FROM json_each(?))", (_json(ids),))}


def _tests_among(con, ids) -> set:
    """The tests themselves among `ids`: test nodes, and functions a test flow starts from."""
    return {r[0] for r in con.execute(
        "SELECT id FROM nodes WHERE id IN (SELECT value FROM json_each(?)) AND (kind = 'test' OR json_extract(attrs, '$.is_test') = 1"
        " OR id IN (SELECT entry_id FROM flows WHERE json_extract(attrs, '$.kind') = 'test'))", (_json(ids),))}


def product_code(con, ids) -> set:
    """The functions among `ids` that are not test code: what a test has to run to prove a change. A file's top
    level (`<module>`) is left out: it runs when the file is imported, not under any one test."""
    tests, test_modules = store.test_places(con)
    out = set()
    for r in con.execute("SELECT n.id, n.kind, n.name, n.path, a.module_id FROM nodes n LEFT JOIN ancestry a ON a.node_id = n.id"
                         " WHERE n.id IN (SELECT value FROM json_each(?))", (_json(ids),)):
        if (r["kind"] == "callable" and not r["name"].startswith("<") and r["path"] not in tests
                and r["module_id"] not in test_modules):
            out.add(r["id"])
    return out


def _expand(con, ids) -> set:
    """Functions inside the types, files and modules among `ids`, with the functions and tests themselves. A
    container something else in `ids` sits in (`scale` in `ops.ts`) only says where that is, and is not expanded."""
    kinds = _kinds(con, ids)
    out = {i for i, k in kinds.items() if k in ("callable", "test")}
    homes = set()
    for r in con.execute("SELECT file_id, module_id FROM ancestry WHERE node_id IN (SELECT value FROM json_each(?))",
                         (_json(out | {i for i, k in kinds.items() if k == "type"}),)):
        homes |= {r[0], r[1]}
    for i, k in kinds.items():
        if i in homes or (k == "type" and any(o.startswith(i + ".") for o in out)):
            continue
        if k == "type":
            out |= {r[0] for r in con.execute("SELECT id FROM nodes WHERE kind IN ('callable', 'test') AND id LIKE ?",
                                              (i.replace("%", "\\%") + ".%",))}
        elif k in ("file", "module"):
            out |= {r[0] for r in con.execute("SELECT a.node_id FROM ancestry a JOIN nodes n ON n.id = a.node_id"
                                              " WHERE (a.file_id = ? OR a.module_id = ?) AND n.kind IN ('callable', 'test')", (i, i))}
    return out


# -- one scenario's test: did it run the changed code? -------------------------------------------------------
def _runs(con) -> dict:
    return {r["run"]: r for r in con.execute("SELECT run, created, commit_sha FROM coverage_runs")}


def _ran(con, ids: set, names: set, only: Optional[str] = None) -> tuple[Optional[set], Optional[str], Optional[str]]:
    """What a test ran, from the newest import that measured it (or from the import `only`): (functions, "test" or
    "test file", the run). Matched by its node on the map, or by the name the runner gave it; else, with coverage
    taken per test file, by the file it is in. (None, None, None) when no import measured it."""
    runs = _runs(con)

    def newest(rows):
        by_run = defaultdict(set)
        for r in rows:
            if only is None or r[0] == only:
                by_run[r[0]].add(r[1])
        if not by_run:
            return None, None
        run = max(by_run, key=lambda k: (runs.get(k)["created"] or "") if runs.get(k) else "")
        return by_run[run], run
    rows = con.execute("SELECT run, node_id FROM covered WHERE test != '' AND test_id IN (SELECT value FROM json_each(?))",
                       (_json(ids),)).fetchall()
    if names:
        wanted = {_strip_param(n) for n in names}
        rows += [(r[0], r[1]) for r in con.execute("SELECT run, node_id, test FROM covered WHERE test != '' AND test_id IS NULL")
                 if r[2] in wanted]
    nodes, run = newest(rows)
    if nodes is not None:
        return nodes, "test", run
    files = {r[0] for r in con.execute("SELECT file_id FROM ancestry WHERE node_id IN (SELECT value FROM json_each(?))", (_json(ids),))}
    nodes, run = newest(con.execute("SELECT run, node_id FROM covered WHERE test != '' AND test_id IN (SELECT value FROM json_each(?))",
                                    (_json(files),)).fetchall()) if files else (None, None)
    return (nodes, "test file", run) if nodes is not None else (None, None, None)


def _label(con, i: str) -> str:
    n = con.execute("SELECT n.name, p.name AS owner, p.kind AS owner_kind FROM nodes n LEFT JOIN nodes p ON p.id = n.parent_id"
                    " WHERE n.id = ?", (i,)).fetchone()
    if n is None:
        return i
    return f"{n['owner']}.{n['name']}" if n["owner_kind"] in ("type", "callable") else n["name"]


def _some(xs: list[str], k: int = 3) -> str:
    xs = sorted(xs)
    return ", ".join(xs[:k]) + (f" and {len(xs) - k} more" if len(xs) > k else "")


def _taken_before(con, run: Optional[str], change_id: str) -> bool:
    """True when the import was taken before the change's baseline: it measured the code as it was, not the change."""
    from . import diff
    row = con.execute("SELECT created FROM coverage_runs WHERE run = ?", (run,)).fetchone() if run else None
    snap = diff.snapshot_path(con, change_id)
    if row is None or not row[0] or not snap.exists():
        return False
    try:
        made = datetime.datetime.fromisoformat(row[0])
    except ValueError:
        return False
    return made.timestamp() < int(snap.stat().st_mtime)   # `created` is to the second: within one, say it is not


def mark_scenarios(con, scenarios: list[dict], changed: set, after_run: Optional[str], test_names, change_id: str = "") -> None:
    """Add `ran_changed_code` (True, False or None) and a one-line `ran_changed_code_note` to each scenario
    result from `spec.verify`. None, with no note, when no per-test coverage was imported."""
    from . import spec
    measured = per_test(con)
    code = product_code(con, changed)
    index = spec._results_index(con.execute("SELECT name, status, message, test_id FROM test_results WHERE run = ?",
                                            (after_run,)).fetchall()) if after_run and measured else None
    # `check --coverage` imports the coverage under the label of the test run it came with.
    same_run = bool(index) and con.execute("SELECT 1 FROM covered WHERE run = ? AND test != '' LIMIT 1",
                                           (after_run,)).fetchone() is not None
    for s in scenarios:
        s["ran_changed_code"] = None
        if not measured or s["state"] != "passes":
            continue
        rows = spec._scenario_results(index, s["name"]) if index else []
        ids = {s["test"]} if s.get("test") else set()
        if not ids:
            gen = spec._generated(test_names, s["name"])
            ids |= {gen["id"]} if gen else set()
        ids |= {r["test_id"] for r in rows if r["test_id"]}
        ran, per, run = _ran(con, ids, {r["name"] for r in rows}, after_run if same_run else None)
        if ran is None:
            if same_run:   # measured on the very run that passed it: it ran none of the measured code
                s["ran_changed_code"] = s["measured_running_the_change"] = False
                s["ran_changed_code_note"] = ("passed without running the changed code: the coverage of the same run records"
                                              " it running none of the measured code")
            else:
                s["ran_changed_code_note"] = ("its test left no record in the measured coverage: it ran none of the measured"
                                              " code, or was not in the measured run (pass the coverage of the same run to"
                                              " `leyline check --coverage` to tell which)")
            continue
        if change_id and _taken_before(con, run, change_id):
            s["ran_changed_code_note"] = ("the coverage was measured before the change was planned, so it shows what the test"
                                          " ran in the old code; measure it again (`leyline check --coverage`)")
            continue
        if not code:
            s["ran_changed_code_note"] = "the change edited no function the test could run"
            continue
        hit = ran & code
        where = " (measured per test file, so another test in the file may be the one that ran it)" if per == "test file" else ""
        s["ran_changed_code"] = bool(hit)
        s["measured_running_the_change"] = bool(hit)
        s["ran_changed_code_note"] = (f"ran the changed code: {_some([_label(con, i) for i in hit])}{where}" if hit else
                                      f"passed without running the changed code ({_some([_label(con, i) for i in code])}){where}")


# -- the tests a change needs ---------------------------------------------------------------------------------
def change_id_for(con, target: str, folder: Optional[Path] = None) -> str:
    """The stored id of a change: spec-<id> for a change folder or its id, or an id as stored (pr-123)."""
    if folder is not None:
        from . import spec
        parsed = spec.parse(folder)
        if "error" not in parsed:
            return "spec-" + parsed["id"]
    for cid in (target, "spec-" + target, "pr-" + target):
        if con.execute("SELECT 1 FROM change_proposals WHERE id = ? UNION SELECT 1 FROM views WHERE id = ?",
                       (cid, "view-" + cid)).fetchone():
            return cid
    return target


def change_code(con, change_id: str) -> tuple[set, Optional[str]]:
    """The code a change edits, adds or must edit: what its plan (or review) marked, the code its tasks name, and
    what has changed since its baseline. (ids, error)."""
    from . import diff
    ids = set()
    v = con.execute("SELECT spec FROM views WHERE id = ?", ("view-" + change_id,)).fetchone()
    found = v is not None
    if v is not None:
        ids |= {m["id"] for m in json.loads(v[0] or "{}").get("marks", [])
                if m.get("role") in ("changed", "new", "must_edit", "contract")}
    for r in con.execute("SELECT nodes FROM spec_items WHERE change_id = ? AND kind = 'task'", (change_id,)):
        found = True
        ids |= set(json.loads(r[0] or "[]"))
    snap = diff.snapshot_path(con, change_id)
    if snap.exists() and diff.moved_on(con, change_id):
        before = diff._open(snap)
        try:
            g = diff.compare(before, con)["nodes"]
        finally:
            before.close()
        ids |= {n["id"] for key in ("added", "resigned", "edited", "types_edited") for n in g[key]}
    if not found:
        return set(), (f"No change {change_id!r} on record. Run `leyline plan <change>` for a spec, or `leyline pr` for a"
                       " branch, first.")
    return {i for i in ids if i in _kinds(con, ids)}, None


def _test_row(con, i: str) -> Optional[dict]:
    r = con.execute("SELECT id, name, kind, repo_id, path, parent_id FROM nodes WHERE id = ?", (i,)).fetchone()
    if r is None:
        return None
    chain, up = [r["name"]], r["parent_id"]
    while up:   # a test method inside test classes: Class::method, as pytest names it
        p = con.execute("SELECT name, kind, parent_id FROM nodes WHERE id = ?", (up,)).fetchone()
        if p is None or p["kind"] != "type":
            break
        chain.insert(0, p["name"])
        up = p["parent_id"]
    out = {"name": r["name"], "id": r["id"], "repo": r["repo_id"], "path": r["path"] or ""}
    if r["kind"] == "file":
        out["file_only"] = True
    elif PY_TEST.search(out["path"]):
        out["pytest"] = out["path"] + "::" + "::".join(chain)
    return out


def _as_ran(test: str, row: dict) -> bool:
    """Whether a pytest node id as the run named it can stand for a test on the map: only when it names the test's
    file by its path in the repository. pytest run from a folder below (`backend/`) names it from there, and the
    command runs from the repository's root."""
    return "::" in test and bool(row.get("pytest")) and test.split("::", 1)[0] == row["path"]


def _file_repo(con, path: str) -> Optional[str]:
    r = con.execute("SELECT repo_id FROM nodes WHERE kind = 'file' AND (path = ? OR ? LIKE '%/' || path) ORDER BY length(path) DESC",
                    (path, path)).fetchone()
    return r[0] if r else None


def select(con, change_id: str) -> dict:
    """The tests to run for a change, why each one, and a command that runs them where the runner is known."""
    changed, err = change_code(con, change_id)
    if err:
        return {"error": err}
    fns = _expand(con, changed)
    fns -= {r[0] for r in con.execute("SELECT id FROM nodes WHERE id IN (SELECT value FROM json_each(?)) AND name LIKE '<%'",
                                      (_json(fns),))}   # a file's top level runs on import, under no one test
    picked: dict[str, dict] = {}

    def add(key, row, why):
        if key not in picked:
            picked[key] = {**row, "why": why}
    measured = per_test(con)
    ran_by_some = set()
    if measured and fns:
        for r in con.execute("SELECT test, test_id, node_id FROM covered WHERE test != '' AND node_id IN"
                             " (SELECT value FROM json_each(?)) ORDER BY test", (_json(fns),)):
            ran_by_some.add(r["node_id"])
            row = _test_row(con, r["test_id"]) if r["test_id"] else None
            if row is None:   # a test the map does not hold: the name its runner gave it
                path = r["test"].split("::", 1)[0]
                row = {"name": r["test"].split("::")[-1], "id": None, "repo": _file_repo(con, path), "path": path,
                       **({"pytest": r["test"]} if "::" in r["test"] and PY_TEST.search(path) else {"file_only": True})}
            elif _as_ran(r["test"], row):
                row["pytest"] = r["test"]          # exactly as pytest named it when it ran
            add(row["id"] or row.get("pytest") or row["path"], row,
                "ran the changed code when coverage was measured" + (" (measured per test file)" if row.get("file_only") else ""))
    tests_in_change = _tests_among(con, fns)
    for i in sorted(tests_in_change):
        row = _test_row(con, i)
        if row:
            add(row["id"], row, "is itself new or changed")
    unmeasured = set(fns - tests_in_change - ran_by_some)
    # The map's paths: every test without a measurement, and, for code no measured test ran (new since the
    # measurement, or not watched), every test that reaches it. A measured test that ran none of it is left out.
    seen = {r[0] for r in con.execute("SELECT DISTINCT test_id FROM covered WHERE test != '' AND test_id IS NOT NULL")}
    through = defaultdict(set)
    for r in con.execute("SELECT f.entry_id, s.callable_id FROM flows f JOIN flow_steps s ON s.flow_id = f.id"
                         " WHERE json_extract(f.attrs, '$.kind') = 'test' AND s.callable_id IN (SELECT value FROM json_each(?))",
                         (_json(fns - tests_in_change),)):
        through[r[0]].add(r[1])
    file_of = {r[0]: r[1] for r in con.execute("SELECT node_id, file_id FROM ancestry WHERE node_id IN (SELECT value FROM json_each(?))",
                                               (_json(through),))}
    from_map = 0
    for entry, hit in sorted(through.items()):
        was_measured = entry in seen or file_of.get(entry) in seen
        if measured and was_measured and not hit & unmeasured:
            continue
        row = _test_row(con, entry)
        if row:
            from_map += entry not in picked
            add(row["id"], row, "its path on the map passes through the change" + (
                "" if not measured else ", in code no measured test ran" if was_measured else
                "; the measured run did not include it"))
    unmeasured = sorted(unmeasured)
    rank = {"ran": 0, "is ": 1}
    tests = sorted(picked.values(), key=lambda t: (rank.get(t["why"][:3], 2), t.get("pytest") or t["path"], t["name"]))
    basis = ("measured coverage" if measured and not from_map else "measured coverage and the map" if measured else "the map")
    note = ("From per-test coverage: the tests that ran the changed code when it was measured"
            + (", and, from the map, the tests that reach changed code no measured test ran or that the measured run left"
               " out." if from_map else ".")
            if measured else
            "No per-test coverage is imported, so these are the tests whose path on the map passes through the change."
            " Import it (`pytest --cov=<package> --cov-context=test`, then `leyline coverage .coverage`) to pick by"
            " what each test ran.")
    return {"change_id": change_id, "basis": basis, "tests": tests, "commands": commands(con, tests),
            "changed_functions": len(fns), "changed_not_measured": [_label(con, i) for i in unmeasured][:30] if measured else [],
            "note": note}


def measured_tests(con, ids) -> list[dict]:
    """The tests measured running any of these functions (per-test coverage only), most lines first."""
    fns = product_code(con, _expand(con, ids))
    if not per_test(con) or not fns:
        return []
    out = []
    for r in con.execute("SELECT test, test_id, COUNT(DISTINCT node_id) AS hits FROM covered WHERE test != '' AND node_id IN"
                         " (SELECT value FROM json_each(?)) GROUP BY test ORDER BY hits DESC, test", (_json(fns),)):
        row = (_test_row(con, r["test_id"]) if r["test_id"] else None) or {
            "name": r["test"].split("::")[-1], "id": None, "repo": _file_repo(con, r["test"].split("::", 1)[0]),
            "path": r["test"].split("::", 1)[0]}
        if (_as_ran(r["test"], row) if row.get("id") else "::" in r["test"] and PY_TEST.search(row["path"])):
            row["pytest"] = r["test"]
        out.append({**row, "functions": r["hits"]})
    return out


# -- a command that runs them ----------------------------------------------------------------------------------
def _js_runner(root: Path, path: str) -> tuple[Optional[str], Path]:
    """vitest or jest, from the nearest package.json (or config file) above the test that names one."""
    here = (root / path).parent
    while True:
        for runner in ("vitest", "jest"):
            if any(here.glob(f"{runner}.config.*")):
                return runner, here
        pkg = here / "package.json"
        if pkg.is_file():
            text = pkg.read_text(encoding="utf-8", errors="replace")
            for runner in ("vitest", "jest"):
                if f'"{runner}"' in text:
                    return runner, here
        if here == root or root not in here.parents:
            return None, root
        here = here.parent


def commands(con, tests: list[dict]) -> list[dict]:
    """One command per runner and directory: pytest node ids, `npx vitest run <files>`, `npx jest <files>`, `go test
    -run`. Tests no runner was recognized for are listed by name instead."""
    roots = store.roots(con)
    groups: dict[tuple, list] = defaultdict(list)
    other = []
    for t in tests:
        root = roots.get(t.get("repo")) or (next(iter(roots.values())) if len(roots) == 1 else None)
        path = t.get("path") or ""
        if root is None or not path:
            other.append(t["name"])
        elif PY_TEST.search(path):
            groups[("pytest", str(root))].append(t.get("pytest") or path)
        elif GO_TEST.search(path):
            groups[("go", str(root))].append((str(Path(path).parent), t["name"]))
        elif JS_TEST.search(path):
            runner, cwd = _js_runner(Path(root), path)
            if runner:
                groups[(runner, str(cwd))].append(str((Path(root) / path).relative_to(cwd)).replace("\\", "/"))
            else:
                other.append(f"{t['name']} ({path})")
        else:
            other.append(f"{t['name']} ({path})")
    out = []
    for (runner, cwd), items in sorted(groups.items()):
        items = list(dict.fromkeys(items))
        if runner == "pytest":
            if len(items) > 200:   # too long for one command line: run their files
                items = list(dict.fromkeys(i.split("::", 1)[0] for i in items))
            cmd = "pytest " + " ".join(shlex.quote(i) for i in items)
        elif runner == "vitest":
            cmd = "npx vitest run " + " ".join(shlex.quote(i) for i in items)
        elif runner == "jest":
            cmd = "npx jest " + " ".join(shlex.quote(i) for i in items)
        else:
            by_dir = defaultdict(list)
            for d, name in items:
                by_dir[d].append(name)
            cmd = " && ".join(f"go test ./{d if d != '.' else ''} -run " + shlex.quote("^(" + "|".join(sorted(set(ns))) + ")$")
                              for d, ns in sorted(by_dir.items()))
        out.append({"runner": runner, "cwd": cwd, "command": cmd, "tests": len(items)})
    if other:
        out.append({"runner": None, "cwd": None, "command": None, "tests": len(other), "unrecognized": other[:40]})
    return out


def text(r: dict) -> str:
    """The selection as a person reads it."""
    t = r["tests"]
    if not t:
        return (f"No test runs the code {r['change_id']} changes, as far as {'the measurement and ' if 'measured' in r['basis'] else ''}"
                "the map can tell.\n\n" + r["note"])
    L = [f"{len(t)} test{'s' if len(t) != 1 else ''} to run for {r['change_id']} (chosen by {r['basis']}):", ""]
    by_why = defaultdict(list)
    for x in t:
        by_why[x["why"]].append(x.get("pytest") or (x["path"] if x.get("file_only") else x["name"]))
    for why, names in by_why.items():
        L.append(f"- {why}: {_some(names, 8)}")
    if r.get("changed_not_measured"):
        L += ["", "Changed code no measured test ran: " + _some(r["changed_not_measured"], 6) + "."]
    runnable = [c for c in r["commands"] if c["command"]]
    if runnable:
        L += ["", "Run them:" if len(runnable) == 1 else "Run them, from each directory:"]
        for c in runnable:
            L.append(f"  cd {_quote(c['cwd'])} && {c['command']}")
    for c in r["commands"]:
        if not c["command"]:
            L += ["", "No runner recognized for: " + _some(c["unrecognized"], 6) + "."]
    L += ["", r["note"]]
    return "\n".join(L)
