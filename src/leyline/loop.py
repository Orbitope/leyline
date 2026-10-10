"""The short path: map the code, plan a change, check it was done as agreed.

Three verbs over the rest of Leyline, for a person who should not need to know about snapshots, run labels
or the order of the spec commands. The CLI and the MCP server both call these.

    map    index one or more repositories; a short overview and a page to browse
    plan   brief an OpenSpec change folder: leyline.md, what is still needed, what to do next
    check  after the change: re-index, read the test results, verify, and say what is left
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from . import diff, spec, store
from . import verdicts

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
            places += list(store.roots(con).values())
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
        return p.relative_to(Path.cwd().resolve()).as_posix()   # the same on every system
    except ValueError:
        return str(path)


def _roots(con) -> dict[str, Path]:
    return store.roots(con)


def changed_files(db: str | Path) -> list[str]:
    """Source files added, edited or deleted since the store was indexed. Cheap next to indexing: it only hashes."""
    return _changes(db)[0]


def _changes(db: str | Path) -> tuple[list[str], dict]:
    """changed_files, and each repository's listing it read (an index run right after takes it, not listing again)."""
    from .adapters import BY_EXTENSION
    from .indexer import read_source, scan

    con = store.connect(db)
    try:
        roots = _roots(con)
        known = {(r["repo_id"], r["path"]): r["content_hash"] for r in con.execute(
            "SELECT repo_id, path, content_hash FROM nodes WHERE kind = 'file' AND layer = 'fact'")}
    finally:
        con.close()
    out, listings = [], {}
    for repo, root in roots.items():
        if not root.is_dir():
            continue
        now = set()
        listings[repo] = listing = scan(root)
        for f in listing.files:
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
    return sorted(set(out)), listings


def made_by_another_version(db: str | Path) -> bool:
    """True when the map was made by another version of Leyline (or one that did not say), so what it found may
    differ from what this version would find in the same files."""
    from .incremental import code_version
    con = store.connect(db)
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'made_by'").fetchone()
    finally:
        con.close()
    return row is None or row[0] != code_version()


def refresh(db: str | Path, force: bool = False, full: bool = False) -> Optional[dict]:
    """Re-index the store's repositories when their code has changed (only what changed is done again, see
    leyline.incremental; `full` does everything). Returns the index stats, or None."""
    from .indexer import index

    changed, listed = _changes(db) if not force else ([], {})
    if not force and not changed and not made_by_another_version(db):
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
        return index(root, db, rid, exact, full=full, listed=listed)
    return index(list(roots.values()), db, None, exact, full=full, listed=listed)


# -- map ------------------------------------------------------------------------------------------
def forget(db: str | Path, ids: list[str]) -> dict:
    """Drop repositories from the store: their facts, where they were, and their place in the workspace. What the
    others know is kept; they are mapped again in full, so no link into a dropped one is left. Notes and decisions
    written about a dropped one's code (inferred and intent rows) are kept, as when code is deleted."""
    if not Path(db).is_file():
        return {"error": f"no store at {db}: nothing to forget"}
    con = store.connect(db)
    try:
        held = sorted(set(store.roots(con)) | {r[0] for r in con.execute("SELECT id FROM nodes WHERE kind = 'repo'")})
        unknown = [i for i in ids if i not in held]
        if unknown:
            return {"error": f"no repository {unknown[0]!r} in the store (it holds {', '.join(held) or 'none'})"}
        row = con.execute("SELECT value FROM meta WHERE key = 'workspace'").fetchone()
        members = [m for m in (json.loads(row[0]) if row else []) if m not in ids]
        with con:
            for rid in ids:
                store.clear_facts(con, rid)
                for prefix in ("root", "rel", "left_out", "timing", "first_commit"):
                    con.execute("DELETE FROM meta WHERE key = ?", (f"{prefix}:{rid}",))
                for table in ("coupling_runs", "coupling_files", "coupling_pairs", "coupling_dirs"):
                    con.execute(f"DELETE FROM {table} WHERE repo_id = ?", (rid,))
            if len(members) > 1:
                con.execute("INSERT OR REPLACE INTO meta VALUES ('workspace', ?)", (json.dumps(members),))
            else:
                con.execute("DELETE FROM meta WHERE key = 'workspace'")
        left = sorted(store.roots(con))
    finally:
        con.close()
    said = f"Forgot {', '.join(ids)}."
    if not left:
        con = store.connect(db)
        try:
            with con:
                store.rebuild_derived(con)
        finally:
            con.close()
        return {"said": said + " Nothing else is mapped in this store."}
    out = map_repos(None, db, full=True)
    return {**out, "said": said} if "error" not in out else out


def map_repos(paths: Optional[list[str]], db: str | Path, repo_id: Optional[str] = None, exact: str = "auto",
              scip: Optional[list[str]] = None, page: bool = True, full: bool = False) -> dict:
    """Index, write the browsable map page next to the store, and count what was found. With no paths, map
    again the repositories the store already holds."""
    from .indexer import index

    began = time.perf_counter()
    if paths and len(paths) == 1 and not repo_id and Path(db).exists():
        # Mapping again a repository the store holds under another id (it was mapped with --repo) keeps that id,
        # rather than adding the same files a second time as a new repository.
        con = store.connect(db)
        try:
            held = {str(p.resolve()): r for r, p in _roots(con).items()}
        finally:
            con.close()
        repo_id = held.get(str(Path(paths[0]).resolve()))
    if paths:
        stats = index(paths if len(paths) > 1 else paths[0], db, repo_id, exact, scip or [], full=full)
    else:   # map again what the store already holds, the way it was mapped
        stats = refresh(db, force=True, full=full) if Path(db).exists() else None
        if stats is None:
            return {"error": "nothing is mapped in this store yet: name the repository directories"}
    ignore = Path(db).parent / ".gitignore"
    if Path(db).parent.name == ".leyline" and not ignore.exists():
        store.write_file(ignore, "# Leyline's map and baselines: local, rebuilt by `leyline map`.\n*\n")
    con = store.connect(db)
    try:
        prune_baselines(con)
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
    store.write_file(path, export.page(con, open_change=open_change))
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
    n = lambda k, word: f"{k:,} {word}{'' if k == 1 else 's'}"
    L = [f"Mapped {who} in {m['seconds']} s: {n(m['files'], 'file')}, {n(m['lines'], 'line')}, {n(len(mods), 'module')}.",
         f"Found {spec._n(m['types'], 'type')}, {spec._n(m['functions'], 'function')}, {spec._n(m['tests'], 'test')} and "
         f"{spec._n(m['entry_points'], 'entry point')} (where a program starts).",
         ("Largest modules: " if len(mods) > 5 else "Modules: ") + ", ".join(f"{x['name']} ({n(x['files'], 'file')})" for x in mods[:5])
         + (f" and {len(mods) - 5} more" if len(mods) > 5 else "")]
    if not mods:
        L.pop()
    if not m["files"]:
        from .adapters import ADAPTERS
        langs = sorted({a.LANGUAGE for a in ADAPTERS})
        L.insert(1, "No source files were found in a language Leyline reads (" + ", ".join(langs) + ").")
    left = {k: v for k, v in m.get("left_out", {}).items() if k not in QUIET_LEFT_OUT}
    if left:
        gone = sum(left.values())
        L.append(f"Not mapped: {gone:,} file{'s' * (gone != 1)} (named above): "
                 + ", ".join(f"{v} {k}" for k, v in sorted(left.items(), key=lambda kv: -kv[1])) + ".")
    if m["patterns"]:
        L.append("Design patterns found: " + ", ".join(f"{k} {v}" if v > 1 else k for k, v in m["patterns"].items()))
    for k, v in m.get("exact", {}).items():
        if isinstance(v, dict) and v.get("status") == "ok" and "calls_confirmed" in v:
            L.append(f"Checked by a compiler ({k.split(':')[1]}): {n(v['calls_confirmed'], 'call')} confirmed, "
                     f"{v.get('calls_removed', 0)} removed, {v.get('calls_added', 0)} added.")
    L.append(f"Store: {Path(m['db']).as_posix()}")
    if m.get("page"):
        L.append(f"Map page: {Path(m['page']).as_posix()} (open it in a browser)")
    L.append("Next: write the change you want as an OpenSpec folder, openspec/changes/<id>/ (ask your agent; the "
             "leyline-spec skill says how), then run `leyline plan <id>`.")
    return "\n".join(L)


def _record(con, run: str, results: list[dict]) -> dict:
    """Store a test run, and count how many results carry the name of a test on the map (how scenarios find them)."""
    out = diff.record_tests(con, run, results)
    out["on_map"] = out.pop("matched_to_test_nodes", 0)
    return out


def prune_baselines(con) -> list[str]:
    """Delete the baseline of each change whose folder is gone (archived or removed): nothing will check it again.
    A change's baseline is otherwise kept, done or not, so `check` can always run again."""
    gone = []
    for r in con.execute("SELECT id FROM change_proposals WHERE id LIKE 'spec-%'").fetchall():
        if spec.folder_gone(con, r[0]) and diff.drop_snapshot(con, r[0]):
            gone.append(r[0])
    return gone


def unmapped(con) -> Optional[dict]:
    """An error when the store holds no repository (a first map stopped before it wrote anything, or a store a reader
    created): planned against it, every name would read as new code and the baseline would be of nothing."""
    if con.execute("SELECT 1 FROM nodes WHERE kind = 'repo' LIMIT 1").fetchone() is None:
        return {"error": f"nothing is mapped in the store at {_show(diff.store_path(con))} yet: run `leyline map <repo>` first"}
    return None


# -- plan -----------------------------------------------------------------------------------------
def plan(db: str | Path, change_dir: str | Path, results: Optional[list[dict]] = None, new_baseline: bool = False) -> dict:
    """Bring the map up to date, write the brief, and record the tests as they pass before the change."""
    reindexed = refresh(db)
    con = store.connect(db)
    try:
        parsed = spec.parse(change_dir)
        if "error" in parsed:
            return parsed
        if bad := unmapped(con):
            return bad
        cid = "spec-" + parsed["id"]
        prune_baselines(con)
        if not new_baseline and diff.lost(con, cid):
            return {"error": f"the baseline of {parsed['id']} (.leyline/snapshots/{cid}.db) is gone, and was not forgotten"
                             " with `leyline spec forget`: planning now would take the code as it is as the start, and"
                             " check could no longer see what the change did. If the code is still as it was before the"
                             f" change, or you mean to start over, run `leyline plan {parsed['id']} --new-baseline`."}
        # Results passed in are the start only while the code is as it was when first planned (or a new baseline is
        # taken now). They are recorded before the brief, which reads them to find tests the map does not know.
        kept = not new_baseline and diff.snapshot_path(con, cid).exists() and diff.moved_on(con, cid)
        recorded = _record(con, spec.run_label(cid, "before"), results) if results is not None and not kept else None
        b = spec.brief(con, change_dir, new_baseline=new_baseline)
        if "error" in b:
            return b
        b["reindexed"] = bool(reindexed)
        if results is not None:
            # The code has moved on since the first plan: these results would describe the change, not the start.
            b["tests_recorded"] = recorded or {"error": "The code has changed since the first plan, so these results are not"
                                                        " a baseline. Pass them to `leyline check` instead."}
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
                 f"Recorded the tests as they are before the change: {diff.recorded_text(t)}"
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


def next_after_plan(b: dict, name: str, for_agent: bool = False) -> list[str]:
    """What to do after a plan: commands to type, or (for_agent) the MCP tools to call."""
    st = spec.brief_status(b)
    if st["blocking"]:
        decide = [x for x in st["blocking"] if x.startswith("decide")]
        if decide and len(decide) == len(st["blocking"]):
            ids = ", ".join(x.split()[4].rstrip(":") for x in decide)
            if for_agent:
                return [f"Next: ask the person to decide the open high {'finding' if len(decide) == 1 else 'findings'} {ids}."
                        " Record each decision with `spec_resolve` (accepted means the spec changes; edit it to match),"
                        " then call `plan` again."]
            return [f"Next: decide the open high {'finding' if len(decide) == 1 else 'findings'} {ids}: `leyline spec resolve"
                    f" <finding id> accepted|rejected|deferred \"why\"` (accepted means the spec changes), then run"
                    f" `leyline plan {name}` again."]
        first = st["blocking"][0]
        more = f" (and {len(st['blocking']) - 1} more under \"Before implementation\")" if len(st["blocking"]) > 1 else ""
        if for_agent:
            return [f"Next: fix the spec in {b['dir']} for: {first}{more}. Then call `plan` again."]
        return [f"Next: fix the spec for: {first}{more}. Edit the files in {_show(b['dir'])} (or ask your agent), then run"
                f" `leyline plan {name}` again."]
    out = []
    opened = [f["id"] for f in b.get("findings") or [] if f["status"] == "open"]
    if opened:   # the person decides each finding before the code is written; nothing else says so
        ids = ", ".join(opened)
        out.append(f"ask the person to decide the open review finding{'s' if len(opened) > 1 else ''} {ids} and record each"
                   " with `spec_resolve` (accepted means the spec changes; edit it to match), then call `plan` again."
                   if for_agent else
                   f"decide the open review finding{'s' if len(opened) > 1 else ''} {ids}: `leyline spec resolve <finding id>"
                   f" accepted|rejected|deferred \"why\"` (accepted means the spec changes), then run `leyline plan {name}` again."
                   " `leyline spec findings " + name + "` shows them in full.")
    if not b.get("baseline_tests") and b.get("baseline") != "kept":
        out.append("while the code is unchanged, run the tests and call `plan` again with their output as test_output"
                   f" ({diff.READS})." if for_agent else
                   f"while the code is unchanged, record how the tests pass now: `<your test command> | leyline plan {name}"
                   f" --tests -`. It reads {diff.READS}.")
    st_missing = [r for r in spec.REVIEWERS if r not in (b.get("reviews") or [])]
    if not st["reviewed"] or st_missing:
        which = " and ".join(st_missing)
        out.append(f"have the plan reviewed before code is written: call `spec_review_facts` with reviewer="
                   f"{' and again with reviewer='.join(st_missing)} (ideally each in a fresh agent), file real problems with"
                   " `spec_finding`, then call `plan` again. Or, if the person accepts the plan as it is, implement it."
                   if for_agent else
                   f"have the plan reviewed ({which}) before code is written (ask your agent to run the"
                   f" leyline-adversarial-review skill), then run `leyline plan {name}` again. Or, if you accept the plan as"
                   " it is, implement it.")
    else:
        out.append("implement the tasks, then run the tests and call `check` with their output as test_output."
                   if for_agent else
                   f"implement it (ask your agent to do the tasks), then pipe the tests' output into"
                   f" `leyline check {name} --tests -`.")
    return ["Next: " + out[0], *("Then: " + x for x in out[1:])]


# -- check ----------------------------------------------------------------------------------------
def check(db: str | Path, change_dir: str | Path, results: Optional[list[dict]] = None,
          coverage_file: Optional[str | Path] = None) -> dict:
    """After the change: re-index if the code moved, record the test results, and verify against the plan.
    `coverage_file`, measured on the same run, is imported after the re-index so its lines land on the new code."""
    reindexed = refresh(db)
    con = store.connect(db)
    try:
        parsed = spec.parse(change_dir)
        if "error" in parsed:
            return parsed
        if bad := unmapped(con):
            return bad
        cid = "spec-" + parsed["id"]
        before, after = spec.run_label(cid, "before"), spec.run_label(cid, "after")
        if coverage_file is not None:
            from . import coverage as measured
            imported = measured.import_file(con, coverage_file, run=after)
            if "error" in imported:
                return {"error": f"cannot import the coverage file: {imported['error']}"}
        code = diff._fingerprint(con)
        recorded = _record(con, after, results) if results is not None else None
        if results is not None:
            with con:   # the code these results ran against
                con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ("tested:" + after, code))
        has = lambda run: con.execute("SELECT 1 FROM test_results WHERE run = ? LIMIT 1", (run,)).fetchone() is not None
        prune_baselines(con)
        # Results recorded earlier describe code that has since changed: they prove nothing about it.
        tested = con.execute("SELECT value FROM meta WHERE key = ?", ("tested:" + after,)).fetchone()
        old = results is None and has(after) and (tested[0] != code if tested else bool(reindexed))
        v = spec.verify(con, change_dir, before if has(before) else None, after if has(after) and not old else None,
                        old_run=after if old else None)
        if "error" in v:
            return v
        v["reindexed"] = bool(reindexed)
        v["tests_recorded"] = recorded
        v["tests_old"] = old
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
        L.append(f"Recorded the test results: {diff.recorded_text(t)}"
                 f" ({t['on_map']} named like tests on the map).")
    if v.get("reindexed"):
        L.append("The code had changed since it was mapped, so it was mapped again first.")
    if v.get("written"):
        L.append(f"Written to {_show(v['written'])}")
    if (v.get("anchors") or {}).get("count"):
        a = v["anchors"]
        L.append(f"Recorded what the spec's {spec._n(a['count'], 'code name')} mean now"
                 + (f" in {_show(a['file'])} (commit it)" if a.get("file") else "") + ", so `leyline drift` can tell when the code moves on.")
    if v.get("page"):
        L.append(f"Map page: {_show(v['page'])} (opens on this change)")
    L += ["", *next_after_check(v, name)]
    return "\n".join(L)


def next_after_check(v: dict, name: str, for_agent: bool = False) -> list[str]:
    """What to do after a check, named for what is left: commands to type, or (for_agent) the MCP tools to call."""
    if v["done_as_agreed"]:
        mine = [t["key"] for t in v["tasks"] if t["state"] == "checked by you"]
        yours = (f" Check task{'s' if len(mine) > 1 else ''} {', '.join(mine)} by hand: {'they name' if len(mine) > 1 else 'it names'}"
                 " no code.") if mine else ""
        yours += verdicts.next_note(v)   # scenarios whose tests pass but may not run the change
        return [("Next: nothing left to check; the change was done as agreed. Show the person the verdict and the diff."
                 if for_agent else "Next: nothing left to check; the change was done as agreed. Review the diff and commit it.")
                + yours + (" The baseline is kept, so `check` can run again after later edits." if for_agent else
                           f" The baseline is kept, so a later edit can be checked the same way; `leyline spec forget {name}`"
                           " deletes it.")]
    again = ("call `check` again with the tests' output" if for_agent else
             f"run the tests again into `leyline check {name} --tests -`")
    out = []
    if v.get("tests_missing") or v.get("tests_old"):
        out.append(("the test results on record are from before the code last changed. " if v.get("tests_old") else "")
                   + (f"run the tests and call `check` again with their output as test_output ({diff.READS})." if for_agent else
                      f"run the tests and pass the output: `<your test command> | leyline check {name} --tests -`"
                      f" ({diff.READS})."))
    undone = [t for t in v["tasks"] if t["state"] in ("not done", "partly")]
    if undone:
        out.append(f"finish task{'s' if len(undone) > 1 else ''} " + ", ".join(
            t["key"] + (f" (missing {', '.join(t['missing'][:3])})" if t["missing"] else "") for t in undone)
                   + f"{'' if for_agent else ' (ask your agent)'}, then {again}.")
    if v["drift"]:
        what = spec._some([n["name"] if n["name"] not in ("<module>", "<top-level>") else f"the top level of {n.get('path')}"
                           for n in v["drift"]], 3)
        out.append(f"{what} changed outside the spec: ask the person to add a task for each (then call `plan`) or undo it."
                   if for_agent else
                   f"{what} changed outside the spec: add a task for each (then `leyline plan {name}`) or undo it.")
    if not (v.get("tests_missing") or v.get("tests_old")):
        fails = [s["name"] for s in v["scenarios"] if s["state"] == "fails"]
        missing = [s["name"] for s in v["scenarios"] if s["state"] == "no test"]
        unrun = [s["name"] for s in v["scenarios"] if s["state"] in ("test exists, not run", "skipped")]
        broke = [x["name"] for x in (v["tests"] or {}).get("newly_failing", []) + (v["tests"] or {}).get("new_failing", [])]
        if not v["tests"]:
            broke = [x["name"] for x in v.get("after_failing", [])]
        if fails:
            out.append(f"make the failing scenario{'s' if len(fails) > 1 else ''} pass: " + spec._some([f'"{x}"' for x in fails], 3) + ".")
        if broke:
            out.append(f"{len(broke)} {'tests fail that did not before' if len(broke) > 1 else 'test fails that did not before'}: "
                       + spec._some(broke, 3) + ". Fix the code, or the test if the spec changed what it checks.")
        if missing:
            out.append("write a test named for each scenario that has none: " + spec._some([f'"{x}"' for x in missing], 3) + ".")
        if unrun:
            out.append("run the tests for " + spec._some([f'"{x}"' for x in unrun], 3)
                       + " too; the output passed in has no result with that name.")
        weak = [s["name"] for s in v["scenarios"] if s.get("ran_changed_code") is False]
        if weak:
            out.append("the test for " + spec._some([f'"{x}"' for x in weak], 3) + " passed without running the changed"
                       " code: make it exercise the change, or say why it need not.")
    if v["open_high_findings"]:
        ids = ", ".join(f["id"] for f in v["open_high_findings"])
        out.append(f"ask the person to decide the open high review finding {ids}, and record it with `spec_resolve`."
                   if for_agent else
                   f"decide the open high review finding {ids}: `leyline spec resolve <finding id> accepted|rejected|deferred \"why\"`.")
    if v["new_dependencies"]:
        out.append("new links between modules: " + spec._some([f"{d['from']} to {d['to']}" for d in v["new_dependencies"]], 3)
                   + ". Keep them in the spec, or undo them.")
    if v["rules_newly_failing"]:
        out.append(f"{spec._n(len(v['rules_newly_failing']), 'rule')} that held now {'fail' if len(v['rules_newly_failing']) > 1 else 'fails'}:"
                   " fix the code or change the rule.")
    out += verdicts.waiting_lines(v)   # when the project makes a person's check block
    if not out:
        out.append(f"fix what the verdict lists, then {again}.")
    return ["Next: " + out[0], *("Also: " + x for x in out[1:])]
