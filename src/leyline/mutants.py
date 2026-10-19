"""A plan of small breaks for the code a change edited, and what their results say about the scenarios' tests.

`check` proves a scenario by its test passing and reaching the changed code. A test can run every changed line and
assert nothing about them. A mutation says so: break a changed line on purpose, run the tests, and see whether the
scenario's own tests fail.

Leyline never runs the code or the tests. `plan` writes, under `.leyline/mutants/<change>/`:

    plan.json     the mutants: file, line, what changes, the function, and its tests in two stages (the tests of the
                  scenarios that reach the function, then up to 40 others), and a plan id
    m-<n>.patch   one patch per mutant, in the file's own line endings, for `git apply` from the repository's root
    run.py        a program using only the standard library, which the person or the agent runs: it runs the tests
                  unmutated, then each mutant, always puts the file back, and appends a line to results.jsonl

`check --mutants results.jsonl` reads that file (`read_results`) and `mark_scenarios` says for each passing scenario
whether its own tests failed a mutant of the changed functions they reach. The verdict is `unasserted` (see verdicts)."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import affected, diff, query, spec, store

MAX_MUTANTS = 30          # at most this many in a plan
PER_LINE = 3              # and this many to a line
STAGE_TWO = 40            # tests besides the scenarios' own that a mutant is run against
STATUSES = ("killed", "survived", "timeout", "error", "equivalent")
LANGUAGES = {".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript", ".cs": "C#",
             ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP",
             ".cpp": "C++", ".c": "C", ".swift": "Swift", ".gd": "GDScript", ".lua": "Lua", ".scala": "Scala"}

COMPARE = {ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="), ast.Lt: ("<", ">="), ast.LtE: ("<=", ">"),
           ast.Gt: (">", "<="), ast.GtE: (">=", "<"), ast.Is: ("is", "is not"), ast.IsNot: ("is not", "is"),
           ast.In: ("in", "not in"), ast.NotIn: ("not in", "in")}
ARITHMETIC = {ast.Add: (r"\+", "-", "+"), ast.Sub: (r"-", "+", "-"), ast.Mult: (r"(?<!\*)\*(?!\*)", "/", "*"),
              ast.Div: (r"(?<!/)/(?!/)", "*", "/")}


# -- the lines the change edited --------------------------------------------------------------------------------
def _removed_lines(before, after) -> set:
    """The hashes of the lines of code the change removed from the baseline (a function that is gone): a function moved
    to another file is new code to the map, but its lines are not new."""
    live = {r[0] for r in after.execute("SELECT id FROM nodes WHERE kind IN ('callable', 'test')")}
    by_file = defaultdict(list)
    for i, repo, path, a, b in before.execute("SELECT id, repo_id, path, span_start, span_end FROM nodes"
                                              " WHERE kind IN ('callable', 'test') AND span_start IS NOT NULL"):
        if i not in live and path:
            by_file[(repo, path)].append((a, b or a))
    out = set()
    for (repo, path), spans in by_file.items():
        row = before.execute("SELECT hashes FROM source_lines WHERE repo_id = ? AND path = ?", (repo, path)).fetchone()
        if row:
            hashes = diff._unpack(row[0])
            for a, b in spans:
                out.update(hashes[a - 1:b])
    return out


def _edited_lines(con, cid: str) -> tuple[dict, Optional[str]]:
    """{function id: [line numbers]} of the lines whose text is new in the functions the change edited or added, in
    product code. The baseline's snapshot keeps a hash of every line; only the rows of the edited files are read. A line
    whose text stood in a function the change removed is not new (a function moved). ({}, why) when it cannot be said."""
    snap = diff.snapshot_path(con, cid)
    if not snap.exists():
        return {}, "no baseline is kept for this change: plan it, before it is implemented"
    before = diff._open(snap)
    try:
        nodes = diff.compare(before, con)["nodes"]
        edited = {n["id"] for key in ("resigned", "edited", "added") for n in nodes[key]}
        ids = sorted(affected.product_code(con, edited))
        own = diff.own_changes(before, con, ids)
        if own is None:
            return {}, "the baseline was taken by an older Leyline, which kept no line hashes: plan the change again"
        pool = _removed_lines(before, con)
    finally:
        before.close()
    out = {}
    for i in ids:
        lines = [ln for ln, text in own.get(i, ()) if diff._code_line(text)
                 and diff.line_hashes(text.encode("utf-8"))[0] not in pool]
        if lines:
            out[i] = lines
    return out, None


# -- the breaks ---------------------------------------------------------------------------------------------------
def _char(line: str, byte_col: int) -> int:
    """A column ast gives in UTF-8 bytes, as a position in the string."""
    return len(line.encode("utf-8")[:byte_col].decode("utf-8", "ignore"))


def _lines_of(text: str) -> list[str]:
    """The lines of a file with their own line endings, split as Python's tokenizer splits them (only at a newline)."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _sites(text: str, lines: list[str], wanted: set) -> list[dict]:
    """The places on the lines `wanted` where a comparison, a boolean operator, a negation, a returned value, an integer
    or an arithmetic operator can be broken: [{"line", "c0", "c1", "new", "operator"}], placed by character on the
    operator itself. The inside of an f-string is skipped (before Python 3.12 its positions are not true), and a break
    that leaves a file that does not parse is dropped."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    inside = set()
    for j in ast.walk(tree):
        if isinstance(j, ast.JoinedStr):
            inside.update(id(x) for x in ast.walk(j))
    found = []

    def at(node, which):
        line = node.lineno if which == "start" else node.end_lineno
        col = node.col_offset if which == "start" else node.end_col_offset
        return line, _char(lines[line - 1], col)

    def between(left, right, pattern):
        (l1, c1), (l2, c2) = at(left, "end"), at(right, "start")
        if l1 != l2:
            return None
        m = re.search(pattern, lines[l1 - 1][c1:c2])
        return (l1, c1 + m.start(), c1 + m.end(), m.group()) if m else None

    def add(line, c0, c1, new, what):
        if line in wanted:
            found.append({"line": line, "c0": c0, "c1": c1, "new": new, "operator": what})

    for n in ast.walk(tree):
        if id(n) in inside or not hasattr(n, "lineno"):
            continue
        if isinstance(n, ast.Compare):
            for k, op in enumerate(n.ops):
                old, new = COMPARE[type(op)]
                left = n.left if k == 0 else n.comparators[k - 1]
                pattern = (r"(?<!\w)" + old.replace(" ", r"\s+") + r"(?!\w)") if old[0].isalpha() else re.escape(old)
                hit = between(left, n.comparators[k], pattern)
                if hit:
                    add(hit[0], hit[1], hit[2], new, f"{old} to {new}")
        elif isinstance(n, ast.BoolOp):
            old, new = ("and", "or") if isinstance(n.op, ast.And) else ("or", "and")
            for k in range(len(n.values) - 1):
                hit = between(n.values[k], n.values[k + 1], r"(?<!\w)" + old + r"(?!\w)")
                if hit:
                    add(hit[0], hit[1], hit[2], new, f"{old} to {new}")
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            line, c0 = at(n, "start")
            m = re.match(r"not(?!\w)\s*", lines[line - 1][c0:])
            if m:
                add(line, c0, c0 + m.end(), "", "not removed")
        elif isinstance(n, ast.Return) and n.value is not None:
            v = n.value
            if v.lineno == v.end_lineno and not (isinstance(v, ast.Constant) and v.value is None):
                (line, c0), (_, c1) = at(v, "start"), at(v, "end")
                add(line, c0, c1, "None", "returned value to None")
        elif isinstance(n, ast.Constant) and type(n.value) is int and n.lineno == n.end_lineno:
            (line, c0), (_, c1) = at(n, "start"), at(n, "end")
            if lines[line - 1][c0:c1] == str(n.value):
                add(line, c0, c1, str(n.value + 1), f"{n.value} to {n.value + 1}")
        elif isinstance(n, ast.BinOp) and type(n.op) in ARITHMETIC:
            pattern, new, old = ARITHMETIC[type(n.op)]
            hit = between(n.left, n.right, pattern)
            if hit:
                add(hit[0], hit[1], hit[2], new, f"{old} to {new}")
    out = []
    for s in sorted(found, key=lambda s: (s["line"], s["c0"], s["operator"])):
        mutated = list(lines)
        mutated[s["line"] - 1] = lines[s["line"] - 1][:s["c0"]] + s["new"] + lines[s["line"] - 1][s["c1"]:]
        if not mutated[s["line"] - 1].endswith("\n") and s["line"] == len(lines):
            continue        # the last line of a file with no final newline: a patch of it needs a marker git apply wants
        try:
            ast.parse("".join(mutated))
        except (SyntaxError, ValueError):
            continue
        out.append(s)
    return out


def _patch(rel: str, old: list[str], line: int, new_line: str) -> bytes:
    """A unified diff of one changed line, in the file's own line endings, for `git apply` from the repository's top."""
    new = list(old)
    new[line - 1] = new_line
    return "".join(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}", n=3)).encode("utf-8")


def _choose(per_function: dict, limit: int) -> tuple[list[dict], list[str]]:
    """At most `limit` mutants, PER_LINE to a line, one to each edited function first and then a second to each, so no
    function is left without one while another has three. Numbered in that order. (mutants, functions left without)."""
    queues = {f: list(s) for f, s in sorted(per_function.items(), key=lambda kv: (kv[1][0]["file"], kv[1][0]["line"]))}
    per_line: dict = defaultdict(int)
    picked: list[dict] = []
    while len(picked) < limit and any(queues.values()):
        for f, q in queues.items():
            while q and len(picked) < limit:
                s = q.pop(0)
                if per_line[(s["file"], s["line"])] >= PER_LINE:
                    continue
                per_line[(s["file"], s["line"])] += 1
                picked.append(s)
                break
    for k, s in enumerate(picked, 1):
        s["id"] = f"m-{k}"
    have = {s["function"] for s in picked}
    return picked, sorted({per_function[f][0]["function_name"] for f in per_function if f not in have})


# -- the tests ----------------------------------------------------------------------------------------------------
def _tests_for(con, function: str, scenario_tests: set, cache: dict) -> tuple[list[str], list[str], int, int]:
    """The tests that reach a function, as pytest names them: (the tests of the scenarios, up to STAGE_TWO others, how
    many others there are, how many tests no runner here can run). Measured per test when coverage is imported, else
    from the map. Worked out once for each function."""
    if function in cache:
        return cache[function]
    ids = {x["id"] for x in query.reaching(con, function, 10 ** 6)["tests"]["items"]}
    if affected.per_test(con):
        from . import coverage
        ids |= {x["id"] for x in coverage.tests_for(con, function, 10 ** 6) if x.get("id")}
    first, rest, other = [], [], 0
    for i in sorted(ids):
        row = affected._test_row(con, i)
        if row is None or not row.get("pytest"):
            other += 1
        elif i in scenario_tests:
            first.append(row["pytest"])
        else:
            rest.append(row["pytest"])
    cache[function] = (sorted(set(first)), sorted(set(rest))[:STAGE_TWO], len(set(rest)), other)
    return cache[function]


# -- the plan -----------------------------------------------------------------------------------------------------
def plan_dir(con, change_id: str) -> Path:
    return diff.store_path(con).parent / "mutants" / change_id


def plan(con, change_dir, limit: int = MAX_MUTANTS) -> dict:
    """Write the mutation plan of a spec change (see the module's docstring) and describe it."""
    parsed = spec.parse(change_dir)
    if "error" in parsed:
        return parsed
    cid = "spec-" + parsed["id"]
    lines_of, why = _edited_lines(con, cid)
    if why:
        return {"error": why}
    roots = store.roots(con)
    rows = {r[0]: r for r in con.execute(
        "SELECT id, name, repo_id, path FROM nodes WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(sorted(lines_of)),))}
    scenario_tests = set()
    by_name = spec._tests(con)
    for s in parsed["scenarios"]:
        if spec._norm(s["name"]) in by_name:
            scenario_tests.add(by_name[spec._norm(s["name"])])
    per_function: dict = defaultdict(list)
    no_mutants: list[dict] = []
    files: dict = {}
    texts: dict = {}
    by_file = defaultdict(list)
    for i, r in rows.items():
        by_file[(r["repo_id"], r["path"])].append(i)
    for (repo, path), ids in sorted(by_file.items()):
        if not path.endswith(".py"):
            ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
            no_mutants.append({"file": path, "why": f"no mutation operators for {LANGUAGES.get(ext, ext or 'this kind of file')} yet (Python only)"})
            continue
        root = roots.get(repo)
        full = Path(root) / path if root else None
        if full is None or not full.is_file() or not store.inside(full, root):
            no_mutants.append({"file": path, "why": "the file is not on disk, or is a link out of the repository"})
            continue
        data = full.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            no_mutants.append({"file": path, "why": "the file is not UTF-8"})
            continue
        lines = _lines_of(text)
        wanted = {ln for i in ids for ln in lines_of[i]}
        sites = _sites(text, lines, wanted)
        owner = {ln: i for i in ids for ln in lines_of[i]}
        files[path] = {"repo": repo, "root": str(root), "sha1": hashlib.sha1(data).hexdigest()}
        texts[path] = lines
        for s in sites:
            f = owner[s["line"]]
            per_function[f].append({**s, "file": path, "repo": repo, "function": f, "function_name": rows[f]["name"]})
        for i in ids:
            if i not in per_function:
                no_mutants.append({"file": path, "function": rows[i]["name"], "why": "no comparison, boolean operator, negation, returned value, integer or arithmetic on the lines it edited"})
    picked, left_without = _choose(dict(per_function), limit)
    prefix = {}
    cache: dict = {}
    mutants, out_dir = [], plan_dir(con, parsed["id"])
    for old in out_dir.glob("m-*.patch") if out_dir.is_dir() else []:
        old.unlink()
    for m in picked:
        info = files[m["file"]]
        if info["root"] not in prefix:
            run = subprocess.run(["git", "-C", info["root"], "rev-parse", "--show-prefix"], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL)
            if run.returncode != 0:
                return {"error": f"{info['root']} is not a git checkout: the patches of a plan are applied with `git apply`"}
            prefix[info["root"]] = run.stdout.strip()
        lines = texts[m["file"]]
        new_line = lines[m["line"] - 1][:m["c0"]] + m["new"] + lines[m["line"] - 1][m["c1"]:]
        patch = _patch(prefix[info["root"]] + m["file"], lines, m["line"], new_line)
        first, rest, more, other = _tests_for(con, m["function"], scenario_tests, cache)
        name = f"{m['id']}.patch"
        store.write_file(out_dir / name, patch)
        mutants.append({"id": m["id"], "file": m["file"], "repo": m["repo"], "line": m["line"], "operator": m["operator"],
                        "before": lines[m["line"] - 1].strip(), "after": new_line.strip(), "function": m["function"],
                        "function_name": m["function_name"], "patch": name, "stage1": first, "stage2": rest,
                        "stage2_total": more, "tests_no_runner": other})
    plan_id = hashlib.sha1(json.dumps([[m["id"], m["file"], m["line"], m["operator"], m["after"]] for m in mutants]
                                      + [[p, f["sha1"]] for p, f in sorted(files.items())]).encode()).hexdigest()[:12]
    doc = {"version": 1, "change": parsed["id"], "plan": plan_id, "files": files, "mutants": mutants,
           "prefix": prefix, "no_mutants": no_mutants, "left_without": left_without, "cap": limit}
    store.write_file(out_dir / "plan.json", json.dumps(doc, indent=1))
    store.write_file(out_dir / "run.py", RUNNER)
    run_cmd = f"python {_shown(out_dir / 'run.py')}"
    return {"change": parsed["id"], "plan": plan_id, "dir": str(out_dir), "count": len(mutants),
            "mutants": [{"id": m["id"], "at": f"{m['file']}:{m['line']}", "operator": m["operator"],
                         "function": m["function_name"], "tests": len(m["stage1"]) + len(m["stage2"])} for m in mutants],
            "no_mutants": no_mutants, "left_without": left_without,
            "run": run_cmd, "results": str(out_dir / "results.jsonl"),
            "next": [f"Run the plan: `{run_cmd}`. It runs the project's tests with each mutant applied, always puts the file back,"
                     " and may be stopped at any time. Then: `leyline check " + parsed["id"] + " --tests - --mutants "
                     + _shown(out_dir / "results.jsonl") + "` (the results of a stopped run are read as far as they go)."]
            if mutants else ["No mutants: nothing the change edited has an operator to break. `check` is as it was."]}


def _shown(path: Path) -> str:
    try:
        return Path(path).relative_to(Path.cwd()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def plan_text(r: dict) -> str:
    L = [f"Mutation plan of {r['change']}: {r['count']} mutant{'s' if r['count'] != 1 else ''} (plan {r['plan']}), in {_shown(Path(r['dir']))}/."]
    L += [f"  {m['id']:<5} {m['at']:<34} {m['operator']:<22} in {m['function']}, {m['tests']} test{'s' if m['tests'] != 1 else ''}"
          for m in r["mutants"]]
    for x in r["no_mutants"]:
        L.append(f"  No mutants for {x['file']}" + (f" ({x['function']})" if x.get("function") else "") + f": {x['why']}.")
    if r["left_without"]:
        L.append("  The cap left these functions without a mutant: " + ", ".join(r["left_without"]) + ".")
    return "\n".join(L + ["", *r["next"]])


# -- the program a person or an agent runs ------------------------------------------------------------------------
RUNNER = r'''#!/usr/bin/env python3
"""Runs the mutation plan beside it, with the standard library only.

For each mutant: apply its patch with git, run the tests that reach its function (the scenarios' own tests first, in
full; then, only if none of them failed, up to 40 others, stopping at the first failure), and always put the file back,
whatever happens (an error, a timeout, Ctrl-C). One line per mutant is appended to results.jsonl, so the run may be
stopped at any time and started again where it was. It runs the project's own tests, as running pytest by hand does.
A run is given twice the time of the unmutated run, at least 30 seconds (LEYLINE_MUTANT_TIMEOUT sets the seconds).
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import hashlib

HERE = Path(__file__).resolve().parent
PLAN = json.loads((HERE / "plan.json").read_text(encoding="utf-8"))
RESULTS = HERE / "results.jsonl"
BACKUP = HERE / "backup"


def say(text):
    print(text, flush=True)


def tests(root, ids, fail_fast, timeout):
    """(the names of the tests that failed, seconds) or (None, seconds) when the run went past its timeout."""
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider", "-rfE"] + (["-x"] if fail_fast else []) + list(ids)
    began = time.time()
    try:
        # No bytecode is written: a mutant changes one character, so the file keeps its size, and compiled twice in the same
        # second it would keep its modification time too, and the compiled original would run in place of the mutant.
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        done = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return None, time.time() - began
    failed = [line.split(" ", 1)[1].split(" - ", 1)[0].strip() for line in done.stdout.splitlines()
              if line.startswith(("FAILED ", "ERROR ")) and " " in line]
    if done.returncode not in (0, 1) and not failed:
        raise RuntimeError("the test runner stopped with status %s: %s" % (done.returncode, (done.stdout + done.stderr)[-300:]))
    if done.returncode == 1 and not failed:
        failed = ["(a failure the runner did not name)"]
    return failed, time.time() - began


def forget_bytecode(path):
    """Delete the compiled copies of a file that is about to run changed, or has been put back."""
    for pyc in (Path(path).parent / "__pycache__").glob(Path(path).stem + ".*.pyc"):
        try:
            pyc.unlink()
        except OSError:
            pass


def restore():
    """Put back a file a run that was killed left changed."""
    marker = HERE / "applied.json"
    if marker.exists():
        info = json.loads(marker.read_text(encoding="utf-8"))
        Path(info["file"]).write_bytes((BACKUP / info["backup"]).read_bytes())
        forget_bytecode(info["file"])
        marker.unlink()
        say("Put back %s, which an earlier run left changed." % info["file"])


def record(rec):
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def main():
    restore()
    done = set()
    baseline = None
    if RESULTS.exists():
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("plan") != PLAN["plan"]:
                continue
            if "baseline_failed" in rec:
                baseline = rec
            elif rec.get("id") and rec.get("status") != "equivalent":
                done.add(rec["id"])
    if not PLAN["mutants"]:
        say("The plan has no mutants.")
        return 0
    for rel, info in PLAN["files"].items():
        now = hashlib.sha1((Path(info["root"]) / rel).read_bytes()).hexdigest()
        if now != info["sha1"]:
            say("%s has changed since the plan was made: make the plan again." % rel)
            return 2
    root_of = {m["id"]: PLAN["files"][m["file"]]["root"] for m in PLAN["mutants"]}
    if baseline is None:
        by_root = {}
        for m in PLAN["mutants"]:
            by_root.setdefault(root_of[m["id"]], set()).update(m["stage1"] + m["stage2"])
        failed, seconds = [], 0.0
        try:
            for root, ids in by_root.items():
                f, s = tests(root, sorted(ids), False, None)
                failed += f
                seconds += s
        except RuntimeError as e:
            say("The tests could not be run before any mutant was applied: %s" % e)
            return 2
        baseline = {"plan": PLAN["plan"], "baseline_failed": sorted(set(failed)), "baseline_seconds": round(seconds, 1)}
        record(baseline)
        say("Unmutated run: %d failing before any mutant (not counted), %.0f s." % (len(baseline["baseline_failed"]), seconds))
    limit = float(os.environ.get("LEYLINE_MUTANT_TIMEOUT") or max(30.0, 2 * baseline["baseline_seconds"]))
    known = set(baseline["baseline_failed"])
    for m in PLAN["mutants"]:
        if m["id"] in done:
            continue
        root = root_of[m["id"]]
        path = Path(root) / m["file"]
        original = path.read_bytes()
        BACKUP.mkdir(exist_ok=True)
        (BACKUP / m["id"]).write_bytes(original)
        (HERE / "applied.json").write_text(json.dumps({"file": str(path), "backup": m["id"]}), encoding="utf-8")
        rec = {"plan": PLAN["plan"], "id": m["id"], "status": "error", "killed_by": [], "ran": [0, len(m["stage1"]) + len(m["stage2"])]}
        try:
            applied = subprocess.run(["git", "-C", root, "apply", str(HERE / m["patch"])], capture_output=True, text=True)
            if applied.returncode != 0:
                rec["why"] = "git apply: " + applied.stderr.strip()[-200:]
            else:
                forget_bytecode(path)
                failed, _ = tests(root, m["stage1"], False, limit) if m["stage1"] else ([], 0)
                rec["ran"][0] = len(m["stage1"])
                if failed is None:
                    rec.update(status="timeout", killed_by=list(m["stage1"]))
                else:
                    new = [f for f in failed if f not in known]
                    if new:
                        rec.update(status="killed", killed_by=sorted(set(new)))
                    elif m["stage2"]:
                        failed, _ = tests(root, m["stage2"], True, limit)
                        rec["ran"][0] += len(m["stage2"])
                        if failed is None:
                            rec.update(status="timeout", killed_by=[])
                        else:
                            new = [f for f in failed if f not in known]
                            rec.update(status="killed" if new else "survived", killed_by=sorted(set(new)))
                    else:
                        rec.update(status="survived")
        except Exception as e:
            rec["why"] = str(e)[:300]
        finally:
            path.write_bytes(original)
            forget_bytecode(path)
            (HERE / "applied.json").unlink(missing_ok=True)
        record(rec)
        say("%s %s: %s" % (m["id"], m["operator"], rec["status"]))
    say("Done. Check the change with: leyline check %s --tests - --mutants %s" % (PLAN["change"], RESULTS))
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        restore()
        sys.exit(130)
'''


# -- the results ----------------------------------------------------------------------------------------------------
def read_results(con, change_id: str, path) -> dict:
    """The results file of a run of the plan: {"plan", "mutants": {id: mutant}, "results": {id: record}, "baseline_failed",
    "not_run": [ids], "equivalent": {id: why}} or {"error": ...}. Refused when it was made for another plan, or when a
    file the plan mutates has changed since the plan was made (the code is not what the results ran against, or a patch
    was left applied). A file a run was stopped part way is read as far as it goes; a mutant marked `equivalent` by a
    person, with a reason, is left out of every judgment."""
    path = Path(path)
    plan_file = path.parent / "plan.json"
    if not path.is_file() or not plan_file.is_file():
        return {"error": f"there is no results file at {path}, with a plan.json beside it: run `leyline mutants {change_id}`, then its run.py"}
    try:
        doc = json.loads(plan_file.read_text(encoding="utf-8"))
    except ValueError:
        return {"error": f"{plan_file} cannot be read"}
    if doc.get("change") != change_id:
        return {"error": f"{plan_file} is the plan of {doc.get('change')}, not of {change_id}"}
    roots = store.roots(con)
    for rel, info in doc["files"].items():
        root = roots.get(info["repo"]) or Path(info["root"])
        full = Path(root) / rel
        now = hashlib.sha1(full.read_bytes()).hexdigest() if full.is_file() else None
        if now != info["sha1"]:
            return {"error": f"{rel} has changed since the plan {doc['plan']} was made (or a mutant was left applied), so the results"
                             f" do not describe this code: make the plan again with `leyline mutants {change_id}` and run it"}
    results, equivalent, baseline, other = {}, {}, [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("plan") != doc["plan"]:
            other += 1
            continue
        if "baseline_failed" in rec:
            baseline = rec["baseline_failed"]
        elif rec.get("status") == "equivalent" and rec.get("id"):
            equivalent[rec["id"]] = rec.get("why") or ""
        elif rec.get("id") and rec.get("status") in STATUSES:
            results[rec["id"]] = rec
    if other and not results and not equivalent:
        return {"error": f"{path} was made for another plan than {doc['plan']}: run the current plan, or delete the file"}
    mutants = {m["id"]: m for m in doc["mutants"]}
    return {"plan": doc["plan"], "mutants": mutants, "results": results, "baseline_failed": baseline,
            "equivalent": equivalent, "no_mutants": doc.get("no_mutants", []), "left_without": doc.get("left_without", []),
            "not_run": [i for i in mutants if i not in results and i not in equivalent or results.get(i, {}).get("status") == "error"],
            "dir": str(path.parent)}


def _bare(name: str) -> str:
    """A test as the runner named it, without its parameters."""
    return re.sub(r"\[.*\]$", "", name or "")


def mark_scenarios(con, scenarios: list[dict], res: dict) -> None:
    """For each passing scenario: `asserted` (True when its own tests failed a mutant of a changed function they reach,
    False when every such mutant ran and none did, None when it cannot be told), and why. A function's mutants that were
    never run leave it unjudged; an equivalent one counts for nothing; one that timed out counts as caught by the tests
    that ran in the first stage. Where the changed functions its tests reach have no mutants, it is not unasserted."""
    mutants, results = res["mutants"], res["results"]
    for s in scenarios:
        s["asserted"], s["mutants_note"] = None, None
        if s.get("state") != "passes":
            continue
        own = set()
        if s.get("test"):
            row = affected._test_row(con, s["test"])
            if row and row.get("pytest"):
                own.add(row["pytest"])
        own |= {_bare(n) for n in s.get("result_names", ())}
        reached = defaultdict(list)
        for m in mutants.values():
            if own & set(m["stage1"]):
                reached[m["function"]].append(m)
        if not reached:
            s["mutants_note"] = "no mutants of the changed code its tests reach"
            continue
        judged = caught = 0
        not_run, elsewhere = 0, set()
        names = {}
        for f, ms in reached.items():
            live = [m for m in ms if m["id"] not in res["equivalent"]]
            if not live:
                continue
            ran = [results[m["id"]] for m in live if results.get(m["id"], {}).get("status") in ("killed", "survived", "timeout")]
            names[f] = live[0]["function_name"]
            if len(ran) < len(live):
                not_run += len(live) - len(ran)
                continue
            judged += 1
            for r in ran:
                by = {_bare(x) for x in r.get("killed_by", [])}
                if by & own:          # a hang in the first stage is recorded as failed by every test that ran there
                    caught += 1
                elif by:
                    elsewhere |= by
        if caught:
            s["asserted"] = True
        elif judged and not not_run:
            s["asserted"] = False
            n = sum(len([m for m in ms if m["id"] not in res["equivalent"]]) for ms in reached.values())
            s["unasserted_why"] = (f"its tests passed while none of them failed any of the {n} mutant{'s' if n != 1 else ''} of "
                                   + ", ".join(sorted(names.values()))
                                   + (f"; other tests did: {', '.join(sorted(elsewhere)[:3])}" if elsewhere else "; no test did"))
        elif not_run:
            s["mutants_note"] = f"{not_run} mutant{'s' if not_run != 1 else ''} of the changed code its tests reach were not run"


def page_data(res: dict) -> dict:
    """What the page says about the mutants: the survivors, the equivalent ones, those not run, and what had none."""
    m = res["mutants"]
    done = [r for r in res["results"].values() if r["status"] in ("killed", "survived", "timeout")]

    def line(i):
        x = m[i]
        return {"id": i, "at": f"{x['file']}:{x['line']}", "operator": x["operator"], "function": x["function_name"],
                "before": x["before"], "after": x["after"], "tests": x["stage1"] + x["stage2"]}
    return {"plan": res["plan"], "count": len(m), "run": len(done),
            "survived": [line(r["id"]) for r in done if r["status"] == "survived"],
            "equivalent": [{**line(i), "why": why} for i, why in res["equivalent"].items() if i in m],
            "not_run": [line(i) for i in res["not_run"]], "baseline_failed": res["baseline_failed"],
            "no_mutants": res["no_mutants"], "left_without": res["left_without"]}


def page_lines(d: Optional[dict]) -> list[str]:
    """The section of the page: "Covered, but not asserted"."""
    if not d:
        return []
    L = ["", f"**Covered, but not asserted** (mutation plan {d['plan']}: {d['count']} mutants, {d['run']} run):"]
    if d["survived"]:
        for x in d["survived"]:
            L.append(f"- survived: {x['at']} in {x['function']}, `{x['before']}` as `{x['after']}` ({x['operator']}); ran against"
                     f" {len(x['tests'])} test{'s' if len(x['tests']) != 1 else ''}")
    else:
        L.append("- no mutant survived every test that ran it.")
    for x in d["equivalent"]:
        L.append(f"- equivalent, counted for nothing: {x['at']} ({x['operator']}): {x['why'] or 'no reason given'}")
    if d["not_run"]:
        L.append(f"- not run: {', '.join(x['id'] for x in d['not_run'][:10])}" + (f" and {len(d['not_run']) - 10} more" if len(d["not_run"]) > 10 else ""))
    if d["baseline_failed"]:
        L.append(f"- failing before any mutant, so never counted as a kill: {', '.join(d['baseline_failed'][:5])}"
                 + (" and more" if len(d["baseline_failed"]) > 5 else ""))
    for x in d["no_mutants"]:
        L.append(f"- no mutants for {x['file']}" + (f" ({x['function']})" if x.get("function") else "") + f": {x['why']}")
    if d["left_without"]:
        L.append("- the cap left these functions without a mutant: " + ", ".join(d["left_without"]))
    return L
