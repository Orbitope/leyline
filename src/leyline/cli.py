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
    args = ap.parse_args(argv)

    if args.cmd == "index":
        db = args.db if args.db != DEFAULT_DB else str(Path(args.path) / DEFAULT_DB)
        stats = index(args.path, db, args.repo)
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
    if args.cmd == "overview":
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
