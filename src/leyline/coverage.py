"""Measured coverage: which functions ran, and under which test.

Static flows say what a test can reach. A coverage file says what it did reach. Importing one lets
the map tell the two apart: a step on a test's static path that never ran sits behind a branch the
test does not take, and a function that ran without being on the path was reached through a link
the indexer could not see.

Formats read:
- coverage.py's data file (`.coverage`). Run the tests with `--cov-context=test` (pytest-cov) or
  `dynamic_context = test_function` and each function is tied to the tests that ran it.
- Cobertura XML (coverlet, `coverage xml`, many others). No per-test detail: a function ran or did not.
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Optional


def _functions(con):
    """(repo, path) -> [(first body line, last line, node id)] for every function on the map."""
    out = defaultdict(list)
    for r in con.execute("SELECT id, repo_id, path, span_start, span_end, attrs FROM nodes"
                         " WHERE kind IN ('callable', 'test') AND path IS NOT NULL"):
        if not r["span_start"]:
            continue
        a = json.loads(r["attrs"] or "{}")
        end = r["span_end"] or r["span_start"]
        # The line that declares a function runs when its file loads or its parent runs. Only the body counts.
        first = a.get("body_line") or (r["span_start"] + 1 if end > r["span_start"] else r["span_start"])
        out[(r["repo_id"], r["path"])].append((min(first, end), end, r["id"]))
    return out


def _owners(funcs, lines):
    """For each executed line, the innermost function whose body holds it. Returns node id -> line count."""
    hit = defaultdict(int)
    for line in lines:
        best = None
        for first, end, node in funcs:
            if first <= line <= end and (best is None or first > best[0]):
                best = (first, node)
        if best:
            hit[best[1]] += 1
    return hit


def _relative(path: str, roots: dict, known: set) -> Optional[tuple]:
    """A measured file's (repo, path) on the map. `roots` is repo id -> the directory it was indexed from."""
    p = path.replace("\\", "/")
    for repo, root in roots.items():
        if root and p.startswith(root.rstrip("/") + "/") and (repo, p[len(root.rstrip("/")) + 1:]) in known:
            return repo, p[len(root.rstrip("/")) + 1:]
    exact = sorted(k for k in known if k[1] == p)
    if exact:
        return exact[0]
    hits = [k for k in known if p.endswith("/" + k[1]) or k[1].endswith("/" + p)]   # a different checkout location
    return max(hits, key=lambda k: (len(k[1]), k)) if hits else None


def _numbits(blob: bytes) -> list[int]:
    return [i * 8 + b for i, byte in enumerate(blob) for b in range(8) if byte & (1 << b)]


def _test_node(con, context: str, cache: dict, home: str = "") -> Optional[str]:
    """`tests/test_x.py::TestC::test_m[param]|run` -> the test function's node id. In a workspace, a test
    in `home` (the repo whose code was measured) wins over one at the same path in another repo."""
    name = context.split("|", 1)[0]
    name = re.sub(r"\[.*\]$", "", name)
    if name in cache:
        return cache[name]
    node = None
    order, extra = (" ORDER BY repo_id != ?", (home,)) if home else ("", ())
    if "::" in name:
        path, *parts = name.split("::")
        row = con.execute("SELECT id FROM nodes WHERE kind IN ('callable', 'test') AND path = ? AND id LIKE ?" + order,
                          (path, "%." + ".".join(parts), *extra)).fetchone()
        if row is None:
            row = con.execute("SELECT id FROM nodes WHERE kind IN ('callable', 'test') AND path LIKE ? AND id LIKE ?" + order,
                              ("%" + path, "%." + ".".join(parts), *extra)).fetchone()
        node = row[0] if row else None
    elif name:
        row = con.execute("SELECT id FROM nodes WHERE kind IN ('callable', 'test') AND (id LIKE ? OR name = ?)" + order,
                          ("%" + name, name, *extra)).fetchone()   # dynamic_context = test_function gives a dotted name
        node = row[0] if row else None
    cache[name] = node
    return node


def import_file(con, path: str | Path, run: str = "default") -> dict:
    """Read a coverage file into the store, replacing any earlier import under the same run name."""
    path = Path(path)
    if not path.is_file():
        return {"error": f"no file at {path}"}
    head = path.read_bytes()[:64]
    roots = {r[0].split(":", 1)[1]: r[1] for r in con.execute("SELECT key, value FROM meta WHERE key LIKE 'root:%'")}
    funcs = _functions(con)
    known = set(funcs)
    rows: list[tuple] = []
    tests_seen, tests_matched, files_matched, files_unknown = set(), set(), 0, 0
    if head.startswith(b"SQLite format 3"):
        fmt = "coverage.py"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        contexts = dict(src.execute("SELECT id, context FROM context"))
        cache: dict = {}
        per: dict[tuple, set] = defaultdict(set)       # (file path, context id) -> lines
        files = dict(src.execute("SELECT id, path FROM file"))
        rel = {i: _relative(p, roots, known) for i, p in files.items()}
        files_matched = sum(1 for v in rel.values() if v)
        files_unknown = sum(1 for v in rel.values() if not v)
        home = max(roots, key=lambda r: sum(1 for v in rel.values() if v and v[0] == r)) if len(roots) > 1 else ""
        for fid, cid, a, b in src.execute("SELECT file_id, context_id, fromno, tono FROM arc"):
            if rel.get(fid):
                per[(rel[fid], cid)].update(x for x in (a, b) if x > 0)
        for fid, cid, bits in src.execute("SELECT file_id, context_id, numbits FROM line_bits"):
            if rel.get(fid):
                per[(rel[fid], cid)].update(_numbits(bits))
        src.close()
        merged: dict[tuple, int] = defaultdict(int)    # (test name, node) -> lines, with setup, run and teardown together
        for (file, cid), lines in per.items():
            ctx = contexts.get(cid, "")
            test = re.sub(r"\[.*\]$", "", ctx.split("|", 1)[0])
            for node, n in _owners(funcs[file], lines).items():
                merged[(test, node)] += n
        for (test, node), n in merged.items():
            tid = _test_node(con, test, cache, home) if test else None
            if test:
                tests_seen.add(test)
                if tid:
                    tests_matched.add(test)
            rows.append((run, test, tid, node, n))
    elif b"<?xml" in head or b"<coverage" in head:
        fmt = "cobertura"
        tree = ET.parse(path)
        sources = [s.text for s in tree.getroot().iter("source") if s.text]
        per_file: dict[str, set] = defaultdict(set)
        for cls in tree.getroot().iter("class"):
            name = cls.get("filename") or ""
            rel = _relative(name, roots, known) or next(
                (r for s in sources for r in [_relative(s.rstrip("/") + "/" + name, roots, known)] if r), None)
            if not rel:
                files_unknown += 1
                continue
            for ln in cls.iter("line"):
                if int(ln.get("hits", "0") or 0) > 0:
                    per_file[rel].add(int(ln.get("number")))
        files_matched = len(per_file)
        for file, lines in per_file.items():
            for node, n in _owners(funcs[file], lines).items():
                rows.append((run, "", None, node, n))
    else:
        return {"error": "not a coverage.py data file or a Cobertura XML report"}
    head_commit = con.execute("SELECT commit_sha FROM nodes WHERE kind = 'repo' LIMIT 1").fetchone()
    ran = {r[3] for r in rows}
    stats = {"functions_ran": len(ran), "tests": len(tests_seen), "tests_matched_to_the_map": len(tests_matched),
             "files_matched": files_matched, "files_not_on_the_map": files_unknown, "per_test": bool(tests_seen)}
    with con:
        con.execute("DELETE FROM covered WHERE run = ?", (run,))
        con.executemany("INSERT INTO covered VALUES (?,?,?,?,?)", rows)
        con.execute("INSERT OR REPLACE INTO coverage_runs VALUES (?,?,?,?,?)",
                    (run, fmt, datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                     head_commit[0] if head_commit else None, json.dumps(stats)))
        for repo in con.execute("SELECT id FROM nodes WHERE kind = 'repo'").fetchall():
            con.execute("INSERT OR REPLACE INTO extractor_coverage VALUES (?,?,?,?,?,?)",
                        (repo[0], "coverage", fmt, "ok", head_commit[0] if head_commit else None, json.dumps(stats)))
    try:  # the orientation tour quotes these numbers
        from . import tours
        for repo in con.execute("SELECT id FROM nodes WHERE kind = 'repo'").fetchall():
            tours.generate(con, repo[0])
    except Exception:
        pass
    return {"run": run, "format": fmt, **stats}


def has(con) -> bool:
    try:
        return con.execute("SELECT 1 FROM covered LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False


def ran(con) -> set:
    """Every function some imported run executed."""
    return {r[0] for r in con.execute("SELECT DISTINCT node_id FROM covered")} if has(con) else set()


def tests_for(con, node_id: str, limit: int = 50) -> list[dict]:
    """The tests under which a function ran."""
    return [{"test": r["test"], "id": r["test_id"], "lines": r["n"]} for r in con.execute(
        "SELECT test, test_id, SUM(lines) AS n FROM covered WHERE node_id = ? AND test != '' GROUP BY test ORDER BY n DESC LIMIT ?",
        (node_id, limit))]


def summary(con) -> dict:
    if not has(con):
        return {"imported": False, "how": "Run the tests under a coverage tool, then `leyline coverage <file>`. "
                                          "With pytest: `pytest --cov=<package> --cov-context=test`."}
    module = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM nodes WHERE kind = 'module'")}
    executed = ran(con)
    measured_files = {r[0] for r in con.execute(
        "SELECT DISTINCT a.file_id FROM covered c JOIN ancestry a ON a.node_id = c.node_id")}
    static = {r[0] for r in con.execute("SELECT DISTINCT s.callable_id FROM flow_steps s JOIN flows f ON f.id = s.flow_id"
                                        " WHERE json_extract(f.attrs, '$.kind') = 'test'")}
    by_module = defaultdict(lambda: {"functions": 0, "ran": 0, "on_a_test_path": 0, "path_but_never_ran": 0, "ran_off_every_path": 0})
    for r in con.execute("SELECT n.id, a.file_id FROM nodes n JOIN ancestry a ON a.node_id = n.id WHERE n.kind = 'callable'"):
        if r["file_id"] not in measured_files:
            continue  # a file the coverage tool did not watch says nothing either way
        m = by_module[module.get(r["id"])]
        m["functions"] += 1
        m["ran"] += r["id"] in executed
        m["on_a_test_path"] += r["id"] in static
        m["path_but_never_ran"] += r["id"] in static and r["id"] not in executed
        m["ran_off_every_path"] += r["id"] in executed and r["id"] not in static
    head = con.execute("SELECT commit_sha FROM nodes WHERE kind = 'repo' LIMIT 1").fetchone()
    runs = [{"run": r["run"], "format": r["format"], "created": r["created"], **json.loads(r["stats"] or "{}"),
             "stale": bool(head and r["commit_sha"] and r["commit_sha"] != head[0])}
            for r in con.execute("SELECT * FROM coverage_runs ORDER BY created DESC")]
    return {"imported": True, "runs": runs,
            "modules": [{"module": names.get(m, m), **v} for m, v in sorted(by_module.items(), key=lambda kv: -kv[1]["functions"])],
            "note": "Only files the coverage tool watched are counted. 'Path but never ran' is code a test can reach on paper "
                    "and did not; 'ran off every path' is code that ran through a link the map does not have."}


def compare_flow(con, flow_id: str) -> dict:
    """One test's static path against what ran when it was measured."""
    f = con.execute("SELECT id, name, entry_id FROM flows WHERE id = ?", (flow_id,)).fetchone()
    if f is None:
        return {"error": f"No flow {flow_id!r}."}
    executed = {r[0] for r in con.execute("SELECT node_id FROM covered WHERE test_id = ?", (f["entry_id"],))}
    if not executed:
        return {"flow": flow_id, "measured": False}
    watched = {r[0] for r in con.execute("SELECT DISTINCT a.file_id FROM covered c JOIN ancestry a ON a.node_id = c.node_id")}
    file_of = {r["node_id"]: r["file_id"] for r in con.execute("SELECT node_id, file_id FROM ancestry")}
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM nodes")}
    steps = [r[0] for r in con.execute("SELECT callable_id FROM flow_steps WHERE flow_id = ? ORDER BY seq", (flow_id,))]
    in_scope = [s for s in steps if file_of.get(s) in watched]
    never = [s for s in in_scope if s not in executed]
    extra = sorted(i for i in executed - set(steps) if not names.get(i, "").startswith("<"))   # not module bodies
    return {"flow": flow_id, "test": f["name"], "measured": True, "steps_watched": len(in_scope),
            "ran": len(in_scope) - len(never),
            "on_path_but_did_not_run": [{"id": i, "name": names.get(i, i)} for i in never[:60]],
            "ran_but_not_on_path": [{"id": i, "name": names.get(i, i)} for i in extra[:60]],
            "ran_but_not_on_path_total": len(extra)}
