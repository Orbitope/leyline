"""Command line: map, plan and check first; the rest (index, query, serve, the spec steps) after."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from . import __version__, query, store
from .indexer import index

DEFAULT_DB = ".leyline/leyline.db"


def _spec(con, args) -> int:
    from . import loop, spec
    if args.action != "resolve" and args.target.startswith("pr-") and not Path(args.target).is_dir():
        return _pr_spec(con, args)   # a pull request reviewed with `leyline pr`: no folder behind it
    if args.action != "resolve":   # a change folder or its id, as plan and check take it
        folder = loop.find_change(args.target, args.db)
        if folder is None and args.action in ("brief", "verify", "facts"):
            print(f"leyline: no change folder {args.target!r}: looked for it as a path and under openspec/changes/.",
                  file=sys.stderr)
            return 2
        args.target = str(folder) if folder is not None else args.target
    if args.action == "brief":
        r = spec.brief(con, args.target, new_baseline=args.new_baseline)
        if "error" in r:
            _print(r)
            return 1
        print(spec.brief_text(r))
        if r.get("baseline") == "kept":
            print("The code has changed since the first brief; verify will still compare with the code as it was then.")
        print(f"written to {r['written']}")
        return 0 if r["ready"] else 1
    if args.action == "verify":
        r = spec.verify(con, args.target, args.before, args.after)
        if "error" in r:
            _print(r)
            return 1
        print(spec.verify_text(r))
        return 0 if r["done_as_agreed"] else 1
    if args.action == "facts":
        _print(spec.review_facts(con, args.target, args.reviewer))
    elif args.action == "findings":
        cid = "spec-" + Path(args.target).name
        for f in spec.findings(con, cid)["findings"]:
            print(f"{f['id']}  {f['status']:<9} {f['severity']:<6} {f['reviewer']}: {f['claim']}")
    elif args.action in ("finding", "file"):   # `file` is the older name
        r = spec.add_finding(con, "spec-" + Path(args.target).name, args.reviewer or "", args.severity or "", args.claim or "",
                             args.evidence, args.proposal)
        _print(r)
        return 1 if "error" in r else 0
    elif args.action == "forget":   # the copy of the code from before the change, kept to compare with
        from . import diff
        cid = "spec-" + Path(args.target).name
        print(f"Deleted the baseline of {cid}." if diff.drop_snapshot(con, cid) else f"No baseline is kept for {cid}.")
    elif args.action == "resolve":
        _print(spec.resolve_finding(con, args.target, args.status, args.reason or ""))
    return 0


def _pr_spec(con, args) -> int:
    """The spec steps that make sense for a pull request: its reviewers' facts, their findings, and forgetting it."""
    from . import diff, pr, spec
    cid = args.target
    if args.action == "facts":
        r = pr.review_facts(con, cid, args.reviewer)
        _print(r)
        return 1 if "error" in r else 0
    if args.action == "findings":
        for f in spec.findings(con, cid)["findings"]:
            print(f"{f['id']}  {f['status']:<9} {f['severity']:<6} {f['reviewer']}: {f['claim']}")
        return 0
    if args.action in ("finding", "file"):
        r = spec.add_finding(con, cid, args.reviewer or "", args.severity or "", args.claim or "", args.evidence, args.proposal)
        _print(r)
        return 1 if "error" in r else 0
    if args.action == "forget":
        gone = diff.drop_snapshot(con, cid)
        diff.snapshot_path(con, cid).with_suffix(".base").unlink(missing_ok=True)
        print(f"Deleted the map of {cid}'s base." if gone else f"No base is kept for {cid}.")
        return 0
    print(f"leyline: {args.action} is for a spec folder; a pull request has `leyline pr`, then facts, finding, findings,"
          " resolve and forget", file=sys.stderr)
    return 2


def _pr(args) -> int:
    from . import pr
    about = args.about or ""
    if about == "-":
        about = sys.stdin.read()
    elif args.about_file:
        about = Path(args.about_file).read_text(encoding="utf-8", errors="replace")
    root = Path(args.path)
    try:
        top = pr.git_root(root.resolve())
    except pr.GitError as e:
        print(f"leyline: {args.path} is not inside a git repository ({e})", file=sys.stderr)
        return 2
    db = args.db or str(top / DEFAULT_DB)
    try:
        r = pr.review(db, top, args.base, about, args.github, args.id)
    except pr.GitError as e:
        print(f"leyline: {e}", file=sys.stderr)
        return 2
    if "error" in r:
        print(f"leyline: {r['error']}", file=sys.stderr)
        return 1
    if args.json:
        _print({k: v for k, v in r.items()})
        return 0
    print(pr.text(r))
    print(f"written to {r['page']}")
    print(f"Next: have it reviewed (`leyline spec facts {r['change_id']} --reviewer logic`, then `performance`;"
          " the leyline-adversarial-review skill runs both).")
    return 0


def _short(con, i: str) -> str:
    """A node as a person reads it: its name, after its owner's when it has one that is not a file."""
    n = query._node(con, i)
    if n is None:
        return i
    up = query._node(con, n["parent_id"]) if n["parent_id"] else None
    return f"{up['name']}.{n['name']}" if up is not None and up["kind"] in ("type", "callable") else n["name"]


def _impact(con, args) -> int:
    """`leyline impact <name or id>`: the CLI side of the MCP `impact` tool."""
    found = query.resolve(con, args.node)
    if "error" in found:
        print(f"leyline: {found['error']}", file=sys.stderr)
        for c in found.get("candidates", [])[:15]:
            print(f"  {c['id']}  ({c['kind']}{', ' + c['path'] if c.get('path') else ''})", file=sys.stderr)
        return 1
    r = query.impact(con, found["id"], args.depth)
    if args.json:
        _print(r)
        return 0
    print(f"{found['id']}\nreached by {_n(r['reached_by'], 'place')} within {_n(r['depth_limit'], 'call')}"
          + (", across modules" if r["crosses_module_boundary"] else "")
          + (", across repositories" if r["crosses_repo_boundary"] else ""))
    for m in r["by_module"]:
        near = [_short(con, d) for d in m["direct"]]
        print(f"  {m['module']:<28} {m['count']:>5}"
              + (f"   called directly by {', '.join(near[:5])}{' ...' if len(near) > 5 else ''}" if near else ""))
    t = r["flows_through"]
    if t["total"]:
        print(f"flows through it: {t['total']}")
        for f in t["items"][:10]:
            print(f"  {f['name']}")
        if t["total"] > 10:
            print(f"  and {t['total'] - 10} more (--json lists 40)")
    print(r["note"])
    return 0


def _n(n: int, word: str, plural: str = "") -> str:
    """A count and its noun: 1 file, 2 files."""
    return f"{n:,} {word if n == 1 else plural or word + 's'}"


def _print(obj) -> None:
    try:
        json.dump(obj, sys.stdout, indent=2)
    except BrokenPipeError:  # the reader (head, a closed pager) went away; that is not an error
        try:
            sys.stdout.close()
        except Exception:
            pass
        return
    sys.stdout.write("\n")


def _summary(o: dict) -> str:
    lines = []
    for repo in o["repos"]:
        lines.append(f"{repo['id']}  ({(repo.get('commit') or 'no commit')[:10]})")
        for m in repo["modules"]:
            langs = ", ".join(f"{k} {v}" for k, v in m["languages"].items())
            lines.append(f"  {m['path']:<22} {m['files']:>3} files {m['loc']:>6} loc "
                         f"{m['types']:>4} types {m['callables']:>4} callables  [{langs}]")
    multi = len(o["repos"]) > 1
    name = lambda i: i.replace(":module:", "/") if multi else i.split(":module:")[-1]
    if o.get("workspace"):
        lines.append("\nlinks between repositories")
        for e in o["workspace"]["links"]:
            kinds = ", ".join(f"{k} {v}" for k, v in e.items() if k not in ("from", "to", "total"))
            lines.append(f"  {e['from']:<18} -> {e['to']:<18} {kinds}")
    lines.append("\nmodule dependencies")
    for e in o["module_edges"]:
        kinds = ", ".join(f"{k} {v}" for k, v in e.items() if k not in ("from", "to", "total"))
        lines.append(f"  {name(e['from']):<18} -> {name(e['to']):<18} {kinds}")
    lines.append("\nextractors")
    for c in o["coverage"]:
        lines.append(f"  {(c['repo'] + '  ') if multi else ''}{c['extractor']:<24} {c['status']}")
    return "\n".join(lines)


ABOUT = """Leyline maps a codebase so that a person can plan a change, have an agent write the code, and check
that it was done as agreed. The usual path is three commands:

  leyline map [repo ...]                index the code; prints a short overview and where the map page is
  leyline plan <change>                 write and print the one-page plan for an OpenSpec change folder
                                        (openspec/changes/<id>/, or just <id>); says what is still needed
  leyline check <change> --tests FILE   after the change: re-map, read the test output, and say whether
                                        it was done as agreed

Each command ends with the next step. To review a change someone else wrote, with no spec:

  leyline pr [base]                     what the checkout's change reaches and did not change, for review"""

ADVANCED = """advanced commands (leyline <command> -h for each):
  index         index without the summary; prints the full statistics
  overview      the module map
  search        find nodes by name
  expand        one node in detail
  neighbors     edges around a node
  impact        what can reach a function, a type or a field: its callers near and far, and the flows through it
  source        source text of a node
  state         fields assigned from outside the type that declares them
  patterns      design patterns found by their shape
  tour          a guided walk through the repository
  coverage      import measured test coverage, or show it
  rules         check the architecture rules
  spec          the spec loop step by step: brief, facts, finding (or file), findings, resolve, verify
  learnings     past decisions on review findings, which later reviews read first; retire one
  record-tests  store a test run under a label
  review        compare an implemented change with a proposal made through MCP
  view          serve the map on localhost
  export        write the map as one self-contained HTML page
  serve         serve the store over MCP (stdio), for a coding agent
  grade         measure the call links found against a compiler's"""


def _tests_arg(value: Optional[str]) -> Optional[list[dict]]:
    """Test runner output, from a file or - for stdin, read into results."""
    if value is None:
        return None
    from . import diff
    return diff.parse_test_output(sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8", errors="replace"))


def _not_dirs(paths: list[str]) -> bool:
    """Say so, and return True, when a path to map is not a directory: mapping it would add it to the store as a
    repository with nothing in it (a typo), or with one file."""
    bad = [p for p in paths if not Path(p).is_dir()]
    for p in bad:
        what = "is a file" if Path(p).is_file() else "does not exist"
        print(f"leyline: {p} {what}. Name the repository's directory (relative paths are from {Path.cwd()}).",
              file=sys.stderr)
    return bool(bad)


def _loop(args) -> int:
    """map, plan and check: the short path."""
    from . import loop
    if args.cmd == "map":
        if len(args.path) > 1 and args.repo:
            print("leyline: --repo names one repository; a workspace takes its ids from the directory names", file=sys.stderr)
            return 2
        if _not_dirs(args.path):
            return 2
        db = args.db or (str(Path(args.path[0]) / DEFAULT_DB) if len(args.path) == 1 else DEFAULT_DB)
        print(loop.map_text(loop.map_repos(args.path, db, args.repo, args.exact, args.scip, full=args.full)))
        return 0
    change = loop.find_change(args.change, args.db or DEFAULT_DB)
    if change is None:
        print(f"leyline: no change folder {args.change!r}: looked for it as a path and under openspec/changes/.\n"
              "Write the change first (ask your agent; the leyline-spec skill says how).", file=sys.stderr)
        return 2
    db = args.db or loop.find_store(change) or DEFAULT_DB
    if not Path(db).exists():
        print("leyline: no map of this code yet. Run `leyline map <repo>` first.", file=sys.stderr)
        return 2
    try:
        results = _tests_arg(args.tests)
    except OSError as e:
        print(f"leyline: cannot read the test output: {e}", file=sys.stderr)
        return 2
    if results == []:
        print("leyline: found no test results in that output. It reads TAP (vitest --reporter=tap, node --test), pytest -rA, or one PASS or FAIL line per test"
              " (`pytest -rA` prints them); other formats can go through the record_test_run MCP tool.", file=sys.stderr)
        return 2
    name = args.change
    if args.cmd == "plan":
        r = loop.plan(db, change, results, args.new_baseline)
        if "error" in r:
            print(f"leyline: {r['error']}", file=sys.stderr)
            return 1
        print(loop.plan_text(r, name))
        from . import spec
        return 0 if spec.brief_status(r)["ready"] else 1
    r = loop.check(db, change, results)
    if "error" in r:
        print(f"leyline: {r['error']}", file=sys.stderr)
        return 1
    print(loop.check_text(r, name))
    return 0 if r["done_as_agreed"] else 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # A console or pipe in a legacy encoding (Windows) cannot print every file name or arrow: replace, not fail.
        if (getattr(stream, "encoding", None) or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nleyline: stopped", file=sys.stderr)
        return 130
    except BrokenPipeError:   # the reader (head, a closed pager) went away; that is not an error
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except sqlite3.DatabaseError as e:
        print(f"leyline: {_store_problem(e)}", file=sys.stderr)
        return 2
    except MemoryError:
        print("leyline: ran out of memory. A repository this large needs more than this machine has free: close other"
              " programs, or map one part of it (a subdirectory) at a time.", file=sys.stderr)
        return 2


def _store_problem(e: Exception) -> str:
    """What a person can do about an error from the store, in place of the traceback."""
    msg = str(e)
    if "locked" in msg or "busy" in msg:
        return ("the store is in use: another leyline run (a map, plan or check) is writing it. Try again when that"
                " run finishes.")
    if "not a database" in msg or "malformed" in msg or "corrupt" in msg:
        return (f"the store is damaged or is not a Leyline store ({msg}). Delete it (and the leyline.cache.db"
                " beside it) and run `leyline map` again.")
    if "readonly" in msg or "unable to open" in msg or "disk I/O" in msg or "full" in msg:
        return f"cannot write the store ({msg}): check the folder's permissions and free disk space."
    return f"the store could not be read ({type(e).__name__}: {msg}). Running `leyline map --full` rebuilds it."


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="leyline", usage="%(prog)s [-h] [--version] [--db DB] command ...",
                                 description=ABOUT, epilog=ADVANCED,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--db", default=os.environ.get("LEYLINE_DB"),
                    help="path to the store (default: .leyline/leyline.db in the repository)")
    # The commands are listed by ABOUT and ADVANCED, so the usual three come first.
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="command", help=argparse.SUPPRESS)
    p = sub.add_parser("map", description="Index one or more repositories, print a short overview, and write the map page.")
    p.add_argument("path", nargs="*", default=["."], help="the repository; name several to map them together")
    p.add_argument("--repo", help="repo id (defaults to the directory name; one repository only)")
    p.add_argument("--exact", choices=["auto", "off", "roslyn", "scip"], default="auto",
                   help="let a compiler overrule the syntax-based links (default: auto, whatever is available)")
    p.add_argument("--scip", action="append", default=[], metavar="FILE", help="a SCIP index to read (repeatable)")
    p.add_argument("--full", action="store_true", help="index everything again, not only what changed since the last map")
    p = sub.add_parser("plan", description="Write and print the one-page plan (leyline.md) for an OpenSpec change folder,"
                                           " re-mapping first if the code changed. Exits 1 while something blocks implementation.")
    p.add_argument("change", help="the change folder, or its id under openspec/changes/")
    p.add_argument("--tests", metavar="FILE", help="test runner output from before the change (- for stdin), kept to compare"
                                                  " with after; TAP, pytest -rA, or one PASS or FAIL line per test")
    p.add_argument("--new-baseline", action="store_true",
                   help="compare from the code as it is now, forgetting the picture kept from the first plan")
    p = sub.add_parser("check", description="After the change is made: re-map, record the test output, and say whether the"
                                            " change was done as agreed. Exits 0 only when it was.")
    p.add_argument("change", help="the change folder, or its id under openspec/changes/")
    p.add_argument("--tests", metavar="FILE", help="test runner output from after the change (- for stdin); one PASS or"
                                                  " FAIL line per test, as pytest -rA prints")
    p = sub.add_parser("pr", description="Review a branch or pull request someone else wrote: map the commit it left"
                                         " its base at, compare it with the checkout, and print what the change reaches"
                                         " and did not change, with no spec needed. Findings are filed against pr-<id>.")
    p.add_argument("base", nargs="?", help="the branch it will merge into (default: origin's default branch, or main)")
    p.add_argument("--about", help="what the change says it does (its title and description); - reads stdin")
    p.add_argument("--about-file", metavar="FILE", help="the same, from a file")
    p.add_argument("--github", metavar="NUMBER", help="take the base, title and description from this GitHub pull request"
                                                     " (needs gh; check the pull request out first)")
    p.add_argument("--id", help="name the review pr-<id> (default: the PR number, else the branch name)")
    p.add_argument("--path", default=".", help="the checkout (default: here)")
    p.add_argument("--json", action="store_true")
    # Advanced commands: no help= keeps them out of the list at the top of --help; ADVANCED lists them.
    p = sub.add_parser("grade", description="measure the call links found against a compiler's (a SCIP index, or roslyn for C#)")
    p.add_argument("root", help="the repository")
    p.add_argument("compiler", help="a .scip file, or roslyn")
    p.add_argument("--prefix", default="", help="the folder the SCIP index's paths are relative to, inside the repository")
    p = sub.add_parser("index", description="index a repository into the store, or several as one workspace")
    p.add_argument("path", nargs="*", default=["."],
                   help="the repository; name several to index them together, so calls between them are linked")
    p.add_argument("--repo", help="repo id (defaults to the directory name; one repository only)")
    p.add_argument("--exact", choices=["auto", "off", "roslyn", "scip"], default="auto",
                   help="let a compiler overrule the syntax-based links: the .NET SDK's for C#, a SCIP index for"
                        " other languages. auto (the default) uses whatever is available")
    p.add_argument("--scip", action="append", default=[], metavar="FILE", help="a SCIP index to read (repeatable)")
    p.add_argument("--full", action="store_true", help="index everything again, not only what changed since the last index")
    p = sub.add_parser("overview", description="the module map")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("expand", description="one node in detail")
    p.add_argument("node_id")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("search", description="find nodes by name")
    p.add_argument("text")
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("neighbors", description="edges around a node")
    p.add_argument("node_id")
    p.add_argument("--direction", default="both", choices=["in", "out", "both"])
    p.add_argument("--kinds", nargs="*")
    p = sub.add_parser("impact", description="what can reach a node through calls and channels, grouped by module,"
                                             " and the flows that pass through it")
    p.add_argument("node", help="a node id, or a name such as Owner.method or a file path")
    p.add_argument("--depth", type=int, default=6, help="how many calls back to follow (default 6)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("source", description="source text of a node")
    p.add_argument("node_id")
    sub.add_parser("serve", description="serve the store over MCP (stdio)")
    p = sub.add_parser("export", description="write the map as one self-contained HTML page")
    p.add_argument("-o", "--out", default="leyline-map.html")
    p.add_argument("--fragment", action="store_true", help="omit the html/head/body wrapper")
    p.add_argument("--no-sources", action="store_true", help="leave source text out of the page")
    p = sub.add_parser("record-tests", description="store a test run read from a test runner's output")
    p.add_argument("run", help="a label for the run, such as before or after")
    p.add_argument("file", help="runner output (TAP, pytest -rA, or one PASS or FAIL line per test); - for stdin")
    p = sub.add_parser("rules", description="check the architecture rules")
    p.add_argument("--confirm", type=int, metavar="ID", help="confirm a suggested rule")
    p = sub.add_parser("review", description="compare an implemented change with its proposal")
    p.add_argument("change_id")
    p.add_argument("--before", help="label of the test run recorded before the change")
    p.add_argument("--after", help="label of the test run recorded after it")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("patterns", description="design patterns found by their shape")
    p.add_argument("pattern", nargs="?", help="only this pattern, such as strategy")
    p.add_argument("--tests", action="store_true", help="include patterns inside test code")
    p = sub.add_parser("spec", description="a change stated as an OpenSpec folder, or a pull request reviewed with"
                                           " `leyline pr` (pr-<id>): brief it, review it, verify it",
                       formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""examples:
  leyline spec facts <change> --reviewer logic       the facts a logic reviewer works from (JSON)
  leyline spec finding <change> --reviewer logic --severity high \\
      --claim "one sentence a person can check" --evidence <node id> ... --proposal "what to change"
  leyline spec findings <change>                     the findings, one line each
  leyline spec resolve <finding id> accepted|rejected|deferred "why"
  leyline spec forget <change>                       delete the change's baseline (a pull request: its base's map)""")
    p.add_argument("action", choices=["brief", "verify", "facts", "finding", "file", "findings", "resolve", "forget"],
                   help="finding files one review finding (file is the same); forget deletes a change's baseline")
    p.add_argument("--new-baseline", action="store_true",
                   help="brief: compare from the code as it is now, forgetting the picture kept from the first brief")
    p.add_argument("--reviewer", help="finding, facts: logic or performance")
    p.add_argument("--severity", choices=["high", "medium", "low"], help="finding")
    p.add_argument("--claim", help="finding: one sentence a person can check")
    p.add_argument("--evidence", nargs="*", default=[], help="finding: node ids that show it")
    p.add_argument("--proposal", default="", help="finding: the change to the spec")
    p.add_argument("target", help="the change folder (openspec/changes/<id>) or its id, or a finding id for resolve")
    p.add_argument("status", nargs="?", choices=["accepted", "rejected", "deferred", "open"], help="for resolve")
    p.add_argument("reason", nargs="?", help="for resolve: why")
    p.add_argument("--before", help="verify: label of the test run recorded before the change")
    p.add_argument("--after", help="verify: label of the test run recorded after it")
    p = sub.add_parser("learnings", description="past decisions on review findings: each finding a person rejected,"
                                                " with the reason. Later reviews read them first.")
    p.add_argument("action", nargs="?", choices=["list", "retire"], default="list", help="retire: it no longer holds")
    p.add_argument("id", nargs="?", help="retire: the learning's id")
    p.add_argument("why", nargs="?", help="retire: why it no longer holds")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("coverage", description="import a coverage file, or show what was measured")
    p.add_argument("file", nargs="?", help="a coverage.py data file (.coverage) or a Cobertura XML report")
    p.add_argument("--run", default="default", help="a name for this import")
    p = sub.add_parser("state", description="fields assigned from outside the type that declares them")
    p.add_argument("scope", nargs="?", help="a module id or an id prefix")
    p = sub.add_parser("tour", description="print a tour of the repository")
    p.add_argument("tour_id", nargs="?", help="a tour id; the orientation tour when left out")
    p = sub.add_parser("view", description="serve the map on localhost")
    p.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    if args.cmd in ("map", "plan", "check"):
        return _loop(args)
    if args.cmd == "pr":
        return _pr(args)
    explicit, args.db = args.db is not None, args.db or DEFAULT_DB
    if args.cmd == "index":
        if len(args.path) > 1 and args.repo:
            print("leyline: --repo names one repository; a workspace takes its ids from the directory names", file=sys.stderr)
            return 2
        if _not_dirs(args.path):
            return 2
        # One repository keeps its store inside it; a workspace's store is in the current directory.
        db = args.db if explicit else str(Path(args.path[0]) / DEFAULT_DB) if len(args.path) == 1 else DEFAULT_DB
        stats = index(args.path if len(args.path) > 1 else args.path[0], db, args.repo, args.exact, args.scip, full=args.full)
        t = stats.get("timing", {})
        print(f"indexed into {db}: {_n(t.get('files', 0), 'file')}, {_n(t.get('lines', 0), 'line')} in {t.get('total_seconds', 0)} s"
              f" ({t.get('lines_per_second', 0):,} lines/s)")
        _print(stats)
        return 0
    if args.cmd == "serve":
        os.environ["LEYLINE_DB"] = args.db
        from .server import main as serve
        serve()
        return 0
    if args.cmd == "grade":
        from . import grade
        g = grade.grade(args.root, args.compiler, args.prefix, db=str(Path(args.db).with_suffix(".grade.db")))
        g.pop("_samples", None)
        _print(g)
        return 0
    if not Path(args.db).exists():
        print(f"leyline: no store at {args.db}. Run `leyline map` first.", file=sys.stderr)
        return 2
    con = store.connect(args.db)
    if args.cmd == "export":
        from . import export
        text = (export.fragment if args.fragment else export.page)(con, not args.no_sources)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text) // 1024} KB)")
        return 0
    if args.cmd == "view":
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from . import export

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # re-read the store on every load, so a re-index shows up on refresh
                body = export.page(store.connect(args.db)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        print(f"Leyline map at http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
        try:
            HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    if args.cmd == "record-tests":
        from . import diff
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8", errors="replace")
        _print(diff.record_tests(con, args.run, diff.parse_test_output(text)))
    elif args.cmd == "patterns":
        from . import patterns
        r = patterns.listing(con, args.pattern, None, args.tests, limit=500)
        print(", ".join(f"{k} {v}" for k, v in sorted(r["by_pattern"].items())) or "no patterns found")
        for x in r["patterns"]:
            print(f"\n[{x['pattern']}  {x['confidence']:.2f}{'  stale' if x['stale'] else ''}] {x['rationale']}")
    elif args.cmd == "spec":
        return _spec(con, args)
    elif args.cmd == "learnings":
        from . import learnings
        return learnings.cli(con, args)
    elif args.cmd == "coverage":
        from . import coverage
        if args.file:
            _print(coverage.import_file(con, args.file, args.run))
        r = coverage.summary(con)
        if not r["imported"]:
            print(r["how"])
            return 0
        print(f"{'module':<28}{'functions':>10}{'ran':>8}{'on a path, never ran':>24}{'ran off every path':>22}")
        for m in r["modules"]:
            print(f"{m['module']:<28}{m['functions']:>10}{m['ran']:>8}{m['path_but_never_ran']:>24}{m['ran_off_every_path']:>22}")
        for run in r["runs"]:
            if run["stale"]:
                print(f"\nrun {run['run']!r} was measured at another commit; import a fresh file")
        print("\n" + r["note"])
    elif args.cmd == "state":
        r = query.shared_state(con, args.scope, 60)
        print(f"{_n(r['total'], 'field is', 'fields are')} assigned from outside their own type\n")
        for f in r["fields"]:
            print(f"{f['name']:<40} {f['writers']:>3} writers in {', '.join(f['written_from'][:5])}"
                  f"{' ...' if len(f['written_from']) > 5 else ''}; {f['readers']} readers")
        print("\n" + r["note"])
    elif args.cmd == "tour":
        from . import tours
        listed = tours.listing(con)["tours"]
        t = tours.get(con, args.tour_id or (listed[0]["id"] if listed else ""))
        if "error" in t:
            _print(t)
            return 1
        print(f"{t['title']}\n")
        for st in t["stops"]:
            print(f"{st['seq']}. {st['title']}  [{st['kind']}: {st['ref']}]\n   {st['narrative']}\n")
        if len(listed) > 1:
            print("other tours: " + ", ".join(x["id"] for x in listed if x["id"] != t["id"]))
    elif args.cmd == "rules":
        from . import rules
        if args.confirm:
            _print(rules.confirm_rule(con, args.confirm))
        r = rules.check(con)
        for x in r["rules"]:
            scope = x["from"] + (" -> " + x["to"] if x["to"] else "")
            print(f"{'ok  ' if x['passes'] else 'FAIL'}  #{x['id']} {x['kind']} {scope}  [{x['status']}]"
                  + ("" if x["passes"] else f"  {x['violations']} violations"))
        return 1 if any(not x["passes"] and x["status"] == "confirmed" and x["severity"] == "error" for x in r["rules"]) else 0
    elif args.cmd == "review":
        from . import diff
        r = diff.review(con, args.change_id, args.before, args.after)
        if args.json or "error" in r:
            _print(r)
        else:
            print(diff.review_text(r))
    elif args.cmd == "overview":
        o = query.overview(con)
        _print(o) if args.json else print(_summary(o))
    elif args.cmd == "expand":
        _print(query.expand(con, args.node_id, args.limit))
    elif args.cmd == "search":
        _print(query.search(con, args.text, args.kind, args.limit))
    elif args.cmd == "impact":
        return _impact(con, args)
    elif args.cmd == "neighbors":
        _print(query.neighbors(con, args.node_id, args.direction, args.kinds))
    elif args.cmd == "source":
        r = query.source(con, args.node_id)
        print(r.get("text") or r.get("error"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
