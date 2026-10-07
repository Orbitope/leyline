"""Command line: map, plan and check first; the rest (index, query, serve, the spec steps) after."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__, query, store
from .indexer import index

DEFAULT_DB = ".leyline/leyline.db"


def _spec(con, args) -> int:
    from . import spec
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
    elif args.action == "file":
        r = spec.add_finding(con, "spec-" + Path(args.target).name, args.reviewer or "", args.severity or "", args.claim or "",
                             args.evidence, args.proposal)
        _print(r)
        return 1 if "error" in r else 0
    elif args.action == "resolve":
        _print(spec.resolve_finding(con, args.target, args.status, args.reason or ""))
    return 0


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

Each command ends with the next step."""

ADVANCED = """advanced commands (leyline <command> -h for each):
  index         index without the summary; prints the full statistics
  overview      the module map
  search        find nodes by name
  expand        one node in detail
  neighbors     edges around a node
  source        source text of a node
  state         fields assigned from outside the type that declares them
  patterns      design patterns found by their shape
  tour          a guided walk through the repository
  coverage      import measured test coverage, or show it
  rules         check the architecture rules
  spec          the spec loop step by step: brief, facts, file, findings, resolve, verify
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
    return diff.parse_test_output(sys.stdin.read() if value == "-" else Path(value).read_text())


def _loop(args) -> int:
    """map, plan and check: the short path."""
    from . import loop
    if args.cmd == "map":
        if len(args.path) > 1 and args.repo:
            print("leyline: --repo names one repository; a workspace takes its ids from the directory names", file=sys.stderr)
            return 2
        db = args.db or (str(Path(args.path[0]) / DEFAULT_DB) if len(args.path) == 1 else DEFAULT_DB)
        print(loop.map_text(loop.map_repos(args.path, db, args.repo, args.exact, args.scip)))
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
        print("leyline: found no test results in that output. It needs one PASS or FAIL line per test"
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
    p = sub.add_parser("plan", description="Write and print the one-page plan (leyline.md) for an OpenSpec change folder,"
                                           " re-mapping first if the code changed. Exits 1 while something blocks implementation.")
    p.add_argument("change", help="the change folder, or its id under openspec/changes/")
    p.add_argument("--tests", metavar="FILE", help="test runner output from before the change (- for stdin), kept to compare"
                                                  " with after; one PASS or FAIL line per test, as pytest -rA prints")
    p.add_argument("--new-baseline", action="store_true",
                   help="compare from the code as it is now, forgetting the picture kept from the first plan")
    p = sub.add_parser("check", description="After the change is made: re-map, record the test output, and say whether the"
                                            " change was done as agreed. Exits 0 only when it was.")
    p.add_argument("change", help="the change folder, or its id under openspec/changes/")
    p.add_argument("--tests", metavar="FILE", help="test runner output from after the change (- for stdin); one PASS or"
                                                  " FAIL line per test, as pytest -rA prints")
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
    p = sub.add_parser("source", description="source text of a node")
    p.add_argument("node_id")
    sub.add_parser("serve", description="serve the store over MCP (stdio)")
    p = sub.add_parser("export", description="write the map as one self-contained HTML page")
    p.add_argument("-o", "--out", default="leyline-map.html")
    p.add_argument("--fragment", action="store_true", help="omit the html/head/body wrapper")
    p.add_argument("--no-sources", action="store_true", help="leave source text out of the page")
    p = sub.add_parser("record-tests", description="store a test run read from a test runner's output")
    p.add_argument("run", help="a label for the run, such as before or after")
    p.add_argument("file", help="runner output with one PASS or FAIL line per test; - for stdin")
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
    p = sub.add_parser("spec", description="a change stated as an OpenSpec folder: brief it, review it, verify it")
    p.add_argument("action", choices=["brief", "verify", "facts", "findings", "resolve", "file"])
    p.add_argument("--new-baseline", action="store_true",
                   help="brief: compare from the code as it is now, forgetting the picture kept from the first brief")
    p.add_argument("--reviewer", help="file, facts: logic or performance")
    p.add_argument("--severity", choices=["high", "medium", "low"], help="file")
    p.add_argument("--claim", help="file: one sentence a person can check")
    p.add_argument("--evidence", nargs="*", default=[], help="file: node ids that show it")
    p.add_argument("--proposal", default="", help="file: the change to the spec")
    p.add_argument("target", help="the change folder (openspec/changes/<id>), or a finding id for resolve")
    p.add_argument("status", nargs="?", choices=["accepted", "rejected", "deferred", "open"], help="for resolve")
    p.add_argument("reason", nargs="?", help="for resolve: why")
    p.add_argument("--before", help="verify: label of the test run recorded before the change")
    p.add_argument("--after", help="verify: label of the test run recorded after it")
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
    explicit, args.db = args.db is not None, args.db or DEFAULT_DB
    if args.cmd == "index":
        if len(args.path) > 1 and args.repo:
            print("leyline: --repo names one repository; a workspace takes its ids from the directory names", file=sys.stderr)
            return 2
        # One repository keeps its store inside it; a workspace's store is in the current directory.
        db = args.db if explicit else str(Path(args.path[0]) / DEFAULT_DB) if len(args.path) == 1 else DEFAULT_DB
        stats = index(args.path if len(args.path) > 1 else args.path[0], db, args.repo, args.exact, args.scip)
        t = stats.get("timing", {})
        print(f"indexed into {db}: {t.get('files', 0):,} files, {t.get('lines', 0):,} lines in {t.get('total_seconds', 0)} s"
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
        Path(args.out).write_text(text)
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
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        _print(diff.record_tests(con, args.run, diff.parse_test_output(text)))
    elif args.cmd == "patterns":
        from . import patterns
        r = patterns.listing(con, args.pattern, None, args.tests, limit=500)
        print(", ".join(f"{k} {v}" for k, v in sorted(r["by_pattern"].items())) or "no patterns found")
        for x in r["patterns"]:
            print(f"\n[{x['pattern']}  {x['confidence']:.2f}{'  stale' if x['stale'] else ''}] {x['rationale']}")
    elif args.cmd == "spec":
        return _spec(con, args)
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
        print(f"{r['total']} fields are assigned from outside their own type\n")
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
    elif args.cmd == "neighbors":
        _print(query.neighbors(con, args.node_id, args.direction, args.kinds))
    elif args.cmd == "source":
        r = query.source(con, args.node_id)
        print(r.get("text") or r.get("error"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
