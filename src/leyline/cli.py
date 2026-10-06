"""Command line: index a repo, query the store, or serve it over MCP."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, query, store
from .indexer import index

DEFAULT_DB = ".leyline/leyline.db"


def _print(obj) -> None:
    json.dump(obj, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _summary(o: dict) -> str:
    lines = []
    for repo in o["repos"]:
        lines.append(f"{repo['id']}  ({(repo.get('commit') or 'no commit')[:10]})")
        for m in repo["modules"]:
            langs = ", ".join(f"{k} {v}" for k, v in m["languages"].items())
            lines.append(f"  {m['path']:<22} {m['files']:>3} files {m['loc']:>6} loc "
                         f"{m['types']:>4} types {m['callables']:>4} callables  [{langs}]")
    name = lambda i: i.split(":module:")[-1]
    lines.append("\nmodule dependencies")
    for e in o["module_edges"]:
        kinds = ", ".join(f"{k} {v}" for k, v in e.items() if k not in ("from", "to", "total"))
        lines.append(f"  {name(e['from']):<18} -> {name(e['to']):<18} {kinds}")
    lines.append("\nextractors")
    for c in o["coverage"]:
        lines.append(f"  {c['extractor']:<24} {c['status']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="leyline", description="A layered graph of a codebase.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--db", default=os.environ.get("LEYLINE_DB", DEFAULT_DB), help="path to the store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="index a repository into the store")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--repo", help="repo id (defaults to the directory name)")
    p.add_argument("--exact", choices=["auto", "off", "roslyn", "scip"], default="auto",
                   help="let a compiler overrule the syntax-based links: the .NET SDK's for C#, a SCIP index for"
                        " other languages. auto (the default) uses whatever is available")
    p.add_argument("--scip", action="append", default=[], metavar="FILE", help="a SCIP index to read (repeatable)")
    p = sub.add_parser("overview", help="the module map")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("expand", help="one node in detail")
    p.add_argument("node_id")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("search", help="find nodes by name")
    p.add_argument("text")
    p.add_argument("--kind")
    p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("neighbors", help="edges around a node")
    p.add_argument("node_id")
    p.add_argument("--direction", default="both", choices=["in", "out", "both"])
    p.add_argument("--kinds", nargs="*")
    p = sub.add_parser("source", help="source text of a node")
    p.add_argument("node_id")
    sub.add_parser("serve", help="serve the store over MCP (stdio)")
    p = sub.add_parser("export", help="write the map as one self-contained HTML page")
    p.add_argument("-o", "--out", default="leyline-map.html")
    p.add_argument("--fragment", action="store_true", help="omit the html/head/body wrapper")
    p.add_argument("--no-sources", action="store_true", help="leave source text out of the page")
    p = sub.add_parser("record-tests", help="store a test run read from a test runner's output")
    p.add_argument("run", help="a label for the run, such as before or after")
    p.add_argument("file", help="runner output with one PASS or FAIL line per test; - for stdin")
    p = sub.add_parser("rules", help="check the architecture rules")
    p.add_argument("--confirm", type=int, metavar="ID", help="confirm a suggested rule")
    p = sub.add_parser("review", help="compare an implemented change with its proposal")
    p.add_argument("change_id")
    p.add_argument("--before", help="label of the test run recorded before the change")
    p.add_argument("--after", help="label of the test run recorded after it")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("patterns", help="design patterns found by their shape")
    p.add_argument("pattern", nargs="?", help="only this pattern, such as strategy")
    p.add_argument("--tests", action="store_true", help="include patterns inside test code")
    p = sub.add_parser("coverage", help="import a coverage file, or show what was measured")
    p.add_argument("file", nargs="?", help="a coverage.py data file (.coverage) or a Cobertura XML report")
    p.add_argument("--run", default="default", help="a name for this import")
    p = sub.add_parser("state", help="fields assigned from outside the type that declares them")
    p.add_argument("scope", nargs="?", help="a module id or an id prefix")
    p = sub.add_parser("tour", help="print a tour of the repository")
    p.add_argument("tour_id", nargs="?", help="a tour id; the orientation tour when left out")
    p = sub.add_parser("view", help="serve the map on localhost")
    p.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    if args.cmd == "index":
        db = args.db if args.db != DEFAULT_DB else str(Path(args.path) / DEFAULT_DB)
        stats = index(args.path, db, args.repo, args.exact, args.scip)
        print(f"indexed into {db}")
        _print(stats)
        return 0
    if args.cmd == "serve":
        os.environ["LEYLINE_DB"] = args.db
        from .server import main as serve
        serve()
        return 0
    if not Path(args.db).exists():
        print(f"leyline: no store at {args.db}. Run `leyline index` first.", file=sys.stderr)
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
