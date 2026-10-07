"""The short path: map the code, plan a change, check it was done as agreed.

Three verbs over the rest of Leyline, for a person who should not need to know about snapshots, run labels
or the order of the spec commands. The CLI and the MCP server both call these.

    map    index one or more repositories; a short overview and a page to browse
    plan   brief an OpenSpec change folder: leyline.md, what is still needed, what to do next
    check  after the change: re-index, read the test results, verify, and say what is left
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Optional

from . import diff, spec, store

STORE = Path(".leyline") / "leyline.db"


# -- finding things -------------------------------------------------------------------------------
def find_change(target: str | Path, db: Optional[str | Path] = None) -> Optional[Path]:
    """A change folder, or a change id looked up under openspec/changes/ here, above here, or in a mapped repo."""
    p = Path(target)
    if p.is_dir():
        return p
    here = Path.cwd().resolve()
    places = [here, *here.parents]
    if db and Path(db).exists():
        con = store.connect(db)
        try:
            places += [Path(r[0]) for r in con.execute("SELECT value FROM meta WHERE key LIKE 'root:%'")]
        finally:
            con.close()
    for d in places:
        hit = d / "openspec" / "changes" / str(target)
        if hit.is_dir():
            return hit
    return None


def find_store(start: Path) -> Optional[Path]:
    """The store of the repository a change folder sits in: the nearest .leyline/leyline.db above it."""
    for d in [start.resolve(), *start.resolve().parents]:
        if (d / STORE).is_file():
            return d / STORE
    return None


def _show(path: str | Path) -> str:
    """A path as a person would type it: relative when it is under the current directory."""
    p = Path(path).resolve()
    try:
        return str(p.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _roots(con) -> dict[str, Path]:
    return {r[0].split(":", 1)[1]: Path(r[1]) for r in con.execute("SELECT key, value FROM meta WHERE key LIKE 'root:%'")}


def changed_files(db: str | Path) -> list[str]:
    """Source files added, edited or deleted since the store was indexed. Cheap next to indexing: it only hashes."""
    from .adapters import BY_EXTENSION
    from .indexer import list_files, read_source

    con = store.connect(db)
    try:
        roots = _roots(con)
        known = {(r["repo_id"], r["path"]): r["content_hash"] for r in con.execute(
            "SELECT repo_id, path, content_hash FROM nodes WHERE kind = 'file' AND layer = 'fact'")}
    finally:
        con.close()
    out = []
    for repo, root in roots.items():
        if not root.is_dir():
            continue
        now = set()
        for f in list_files(root):
            if "." + f.rsplit(".", 1)[-1] not in BY_EXTENSION:
                continue
            now.add(f)
            try:
                data = read_source(root / f)
            except OSError:
                data = None
            if data is None or known.get((repo, f)) != hashlib.sha1(data).hexdigest():
                out.append(f)
        out += [p for (r, p) in known if r == repo and p not in now]
    return sorted(set(out))


def refresh(db: str | Path, force: bool = False, full: bool = False) -> Optional[dict]:
    """Re-index the store's repositories when their code has changed (only what changed is done again, see
    leyline.incremental; `full` does everything). Returns the index stats, or None."""
    from .indexer import index

    if not force and not changed_files(db):
        return None
    con = store.connect(db)
    try:
        roots = _roots(con)
        row = con.execute("SELECT value FROM meta WHERE key = 'exact'").fetchone()
    finally:
        con.close()
    exact = row[0] if row else "auto"
    if not roots:
        return None
    if len(roots) == 1:   # the repo id may not be the directory name (--repo), so pass it
        (rid, root), = roots.items()
        return index(root, db, rid, exact, full=full)
    return index(list(roots.values()), db, None, exact, full=full)


# -- map ------------------------------------------------------------------------------------------
def map_repos(paths: Optional[list[str]], db: str | Path, repo_id: Optional[str] = None, exact: str = "auto",
              scip: Optional[list[str]] = None, page: bool = True, full: bool = False) -> dict:
    """Index, write the browsable map page next to the store, and count what was found. With no paths, map
    again the repositories the store already holds."""
    from .indexer import index

    began = time.perf_counter()
    if paths:
        stats = index(paths if len(paths) > 1 else paths[0], db, repo_id, exact, scip or [], full=full)
    else:   # map again what the store already holds, the way it was mapped
        stats = refresh(db, force=True, full=full) if Path(db).exists() else None
        if stats is None:
            return {"error": "nothing is mapped in this store yet: name the repository directories"}
    con = store.connect(db)
    try:
        if paths:
            with con:   # so plan and check re-index the same way
                con.execute("INSERT OR REPLACE INTO meta VALUES ('exact', ?)", (exact,))
        out = {"db": str(db), "seconds": round(time.perf_counter() - began, 1), **counts(con),
               "timing": stats.get("timing", {}), "exact": {k: v for k, v in stats.items() if k.startswith("exact:")},
               "left_out": _left_out(stats.get("left_out") or {})}
        if page:
            out["page"] = str(write_page(con, db, always=True))
    finally:
        con.close()
    return out


def write_page(con, db: str | Path, open_change: Optional[str] = None, always: bool = False) -> Optional[Path]:
    """The map page beside the store. `map` writes it; `plan` and `check` rewrite it when it is there, opening
    on their change, so the page a person already has open shows the change after a reload."""
    from . import export

    path = Path(db).parent / "map.html"
    if not always and not path.is_file():
        return None
    path.write_text(export.page(con, open_change=open_change), encoding="utf-8")
    return path


def counts(con) -> dict:
    one = lambda sql: con.execute(sql).fetchone()[0]
    modules = [{"name": r["path"] or r["name"], "repo": r["repo_id"], "files": r["n"]} for r in con.execute(
        "SELECT m.path, m.name, m.repo_id, COUNT(f.id) AS n FROM nodes m LEFT JOIN nodes f ON f.parent_id = m.id AND f.kind = 'file'"
        " WHERE m.kind = 'module' GROUP BY m.id ORDER BY n DESC, m.path")]
    from . import patterns as found
    patterns = dict(sorted(found.listing(con, limit=1)["by_pattern"].items()))
    return {
        "repos": [r[0] for r in con.execute("SELECT id FROM nodes WHERE kind = 'repo' ORDER BY id")],
        "modules": modules,
        "files": one("SELECT COUNT(*) FROM nodes WHERE kind = 'file'"),
        "lines": one("SELECT COALESCE(SUM(span_end), 0) FROM nodes WHERE kind = 'file'"),
        "types": one("SELECT COUNT(*) FROM nodes WHERE kind = 'type'"),
        "functions": one("SELECT COUNT(*) FROM nodes WHERE kind = 'callable' AND COALESCE(json_extract(attrs, '$.is_test'), 0) = 0"),
        "tests": one("SELECT COUNT(*) FROM nodes WHERE kind = 'test' OR json_extract(attrs, '$.is_test') = 1"),
        "entry_points": one("SELECT COUNT(*) FROM nodes WHERE kind = 'entry_point'"),
        "patterns": patterns,
    }


# Left out as a matter of course; counted on stderr, not repeated in the summary.
QUIET_LEFT_OUT = ("dependency directory", "deleted (git still lists it)", "symlink to another listed file (indexed there)",
                  "symlink to a directory")


def _left_out(left: dict) -> dict:
    """reason -> count, over every repository (a workspace's stats hold one summary per repository)."""
    per_repo = left and all(isinstance(v, dict) and "count" not in v for v in left.values())
    out: dict = {}
    for summary in (left.values() if per_repo else [left]):
        for why, v in summary.items():
            out[why] = out.get(why, 0) + v["count"]
    return out


def map_text(m: dict) -> str:
    repos = m["repos"]
    who = repos[0] if len(repos) == 1 else f"{len(repos)} repositories ({', '.join(repos)})"
    mods = m["modules"]
    L = [f"Mapped {who} in {m['seconds']} s: {m['files']:,} files, {m['lines']:,} lines, {len(mods)} modules.",
         f"Found {spec._n(m['types'], 'type')}, {spec._n(m['functions'], 'function')}, {spec._n(m['tests'], 'test')} and "
         f"{spec._n(m['entry_points'], 'entry point')} (where a program starts).",
         ("Largest modules: " if len(mods) > 5 else "Modules: ") + ", ".join(f"{x['name']} ({x['files']} files)" for x in mods[:5])
         + (f" and {len(mods) - 5} more" if len(mods) > 5 else "")]
    if not mods:
        L.pop()
    if not m["files"]:
        from .adapters import ADAPTERS
        langs = sorted({a.LANGUAGE for a in ADAPTERS})
        L.insert(1, "No source files were found in a language Leyline reads (" + ", ".join(langs) + ").")
    left = {k: v for k, v in m.get("left_out", {}).items() if k not in QUIET_LEFT_OUT}
    if left:
        n = sum(left.values())
        L.append(f"Not mapped: {n:,} file{'s' * (n != 1)} (named above): "
                 + ", ".join(f"{v} {k}" for k, v in sorted(left.items(), key=lambda kv: -kv[1])) + ".")
    if m["patterns"]:
        L.append("Design patterns found: " + ", ".join(f"{k} {v}" if v > 1 else k for k, v in m["patterns"].items()))
    for k, v in m.get("exact", {}).items():
        if isinstance(v, dict) and v.get("status") == "ok" and "calls_confirmed" in v:
            L.append(f"Checked by a compiler ({k.split(':')[1]}): {v['calls_confirmed']:,} calls confirmed, "
                     f"{v.get('calls_removed', 0)} removed, {v.get('calls_added', 0)} added.")
    L.append(f"Store: {m['db']}")
    if m.get("page"):
        L.append(f"Map page: {m['page']} (open it in a browser)")
    L.append("Next: write the change you want as an OpenSpec folder, openspec/changes/<id>/ (ask your agent; the "
             "leyline-spec skill says how), then run `leyline plan <id>`.")
    return "\n".join(L)


def _record(con, run: str, results: list[dict]) -> dict:
    """Store a test run, and count how many results carry the name of a test on the map (how scenarios find them)."""
    out = diff.record_tests(con, run, results)
    out.pop("matched_to_test_nodes", None)   # counts only test nodes by exact name; on_map is what scenarios use
    on_map = spec._tests(con)
    out["on_map"] = sum(1 for r in results if spec._norm(r["name"]) in on_map)
    return out


# -- plan -----------------------------------------------------------------------------------------
def plan(db: str | Path, change_dir: str | Path, results: Optional[list[dict]] = None, new_baseline: bool = False) -> dict:
    """Bring the map up to date, write the brief, and record the tests as they pass before the change."""
    reindexed = refresh(db)
    con = store.connect(db)
    try:
        b = spec.brief(con, change_dir, new_baseline=new_baseline)
        if "error" in b:
            return b
        b["reindexed"] = bool(reindexed)
        if results is not None:
            if b.get("baseline") == "kept":
                # The code has moved on since the first plan: these results would describe the change, not the start.
                b["tests_recorded"] = {"error": "The code has changed since the first plan, so these results are not a "
                                                "baseline. Pass them to `leyline check` instead."}
            else:
                b["tests_recorded"] = _record(con, spec.run_label(b["change_id"], "before"), results)
                b["baseline_tests"] = True
                spec._write(Path(b["written"]), spec.brief_text(b))   # the page now knows the baseline is there
        page = write_page(con, db, b["change_id"])   # after leyline.md is final, since the page embeds it
        if page:
            b["page"] = str(page)
    finally:
        con.close()
    return b


def plan_text(b: dict, name: str) -> str:
    L = [spec.brief_text(b).rstrip(), ""]
    t = b.get("tests_recorded")
    if t:
        L.append(t["error"] if "error" in t else
                 f"Recorded the tests as they are before the change: {t.get('pass', 0)} pass, {t.get('fail', 0)} fail"
                 f" ({t['on_map']} named like tests on the map).")
    if b.get("reindexed"):
        L.append("The code had changed since it was mapped, so it was mapped again first.")
    if b.get("baseline") == "kept":
        L.append("The code has changed since the first plan; `leyline check` still compares with the code as it was then.")
    L.append(f"Written to {_show(b['written'])}")
    if b.get("page"):
        L.append(f"Map page: {_show(b['page'])} (opens on this change)")
    L += ["", *next_after_plan(b, name)]
    return "\n".join(L)


def next_after_plan(b: dict, name: str) -> list[str]:
    st = spec.brief_status(b)
    if st["blocking"]:
        if any(x.startswith("decide") for x in st["blocking"]) and not b["gaps"]:
            return [f"Next: decide each open high finding: `leyline spec resolve <finding id> accepted|rejected|deferred \"why\"`"
                    f" (accepted means the spec changes), then run `leyline plan {name}` again."]
        return [f"Next: fix the spec for each item under \"Before implementation\" (edit the files in {_show(b['dir'])}, or ask"
                f" your agent), then run `leyline plan {name}` again."]
    out = []
    if not b.get("baseline_tests") and b.get("baseline") != "kept":
        out.append(f"while the code is unchanged, record how the tests pass now: `<your test command> | leyline plan {name}"
                   " --tests -`. The output needs one PASS or FAIL line per test; `pytest -rA` prints that.")
    if not st["reviewed"]:
        out.append(f"have the plan reviewed before code is written (ask your agent to run the leyline-adversarial-review"
                   f" skill), then run `leyline plan {name}` again. Or, if you accept the plan as it is, implement it.")
    else:
        out.append(f"implement it (ask your agent to do the tasks), then run `leyline check {name} --tests <test output>`.")
    return ["Next: " + out[0], *("Then: " + x for x in out[1:])]


# -- check ----------------------------------------------------------------------------------------
def check(db: str | Path, change_dir: str | Path, results: Optional[list[dict]] = None) -> dict:
    """After the change: re-index if the code moved, record the test results, and verify against the plan."""
    reindexed = refresh(db)
    con = store.connect(db)
    try:
        parsed = spec.parse(change_dir)
        if "error" in parsed:
            return parsed
        cid = "spec-" + parsed["id"]
        before, after = spec.run_label(cid, "before"), spec.run_label(cid, "after")
        recorded = _record(con, after, results) if results is not None else None
        has = lambda run: con.execute("SELECT 1 FROM test_results WHERE run = ? LIMIT 1", (run,)).fetchone() is not None
        v = spec.verify(con, change_dir, before if has(before) else None, after if has(after) else None)
        if "error" in v:
            return v
        v["reindexed"] = bool(reindexed)
        v["tests_recorded"] = recorded
        # Results recorded earlier describe code that has since changed.
        v["tests_old"] = bool(reindexed) and results is None and has(after)
        v["tests_missing"] = not has(after)
        page = write_page(con, db, v["change_id"])
        if page:
            v["page"] = str(page)
    finally:
        con.close()
    return v


def check_text(v: dict, name: str) -> str:
    L = [f"Check of: {v['title']}", "", spec.verify_text(v).rstrip(), ""]
    if v.get("tests_recorded"):
        t = v["tests_recorded"]
        L.append(f"Recorded the test results: {t.get('pass', 0)} pass, {t.get('fail', 0)} fail"
                 f" ({t['on_map']} named like tests on the map).")
    if v.get("reindexed"):
        L.append("The code had changed since it was mapped, so it was mapped again first.")
    if v.get("written"):
        L.append(f"Written to {_show(v['written'])}")
    if v.get("page"):
        L.append(f"Map page: {_show(v['page'])} (opens on this change)")
    L += ["", *next_after_check(v, name)]
    return "\n".join(L)


def next_after_check(v: dict, name: str) -> list[str]:
    if v["done_as_agreed"]:
        return ["Next: nothing left to check; the change was done as agreed. Review the diff and commit it."]
    out = []
    if v.get("tests_missing") or v.get("tests_old"):
        out.append(("the test results on record are from before the code last changed. " if v.get("tests_old") else "")
                   + f"run the tests and pass the output: `<your test command> | leyline check {name} --tests -`"
                   " (one PASS or FAIL line per test; `pytest -rA` prints that).")
    if any(t["state"] in ("not done", "partly") for t in v["tasks"]):
        out.append(f"finish the tasks marked not done or partly (ask your agent), then run `leyline check {name}` again.")
    if v["drift"]:
        out.append(f"for each change not in the spec, decide: add a task for it (then `leyline plan {name}`) or undo it.")
    if any(s["state"] in ("fails", "no test") for s in v["scenarios"]) or (v["tests"] and v["tests"]["newly_failing"]):
        out.append("fix what fails, or add the missing tests, then check again.")
    if v["open_high_findings"]:
        out.append("decide the open high review findings: `leyline spec resolve <finding id> accepted|rejected|deferred \"why\"`.")
    if v["new_dependencies"] or v["rules_newly_failing"]:
        out.append("look at the new links between modules and the rules that now fail: keep them in the spec, or undo them.")
    if not out:
        out.append(f"fix what the verdict lists, then run `leyline check {name}` again.")
    return ["Next: " + out[0], *("Also: " + x for x in out[1:])]
