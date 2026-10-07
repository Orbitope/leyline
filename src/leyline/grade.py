"""Grade the call links an adapter found against a compiler's, read from a SCIP index.

A call in the compiler's index is a reference, at a place in the source where the name is followed
by `(` or opens a JSX tag, to a symbol whose definition lands on a function on the map. Both sides
are reduced to (calling function, called function) pairs and compared:

- precision: of the links the adapter made, the share the compiler also makes;
- recall: of the compiler's links, the share the adapter found.

Only files the compiler indexed count, and only links whose both ends are on the map, so a function
the adapter did not declare at all shows up as missed recall, not as a wrong link.
"""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from . import exact, store
from .indexer import Indexer


CTOR_NAMES = ("constructor", ".ctor", "__init__", "new", "init")


def compiler_sites(ix, scip_path: str, prefix: str = "") -> tuple[dict, set, set]:
    """What the compiler says at each call site.

    Returns (sites, files, mentions): sites maps (calling function, line, name) to the called function's
    id on the map, or to None when the compiler's target is not a function on the map (an interface
    member, a library); files are the repo paths it indexed; mentions are (function, function) pairs
    where one names the other without calling it."""
    from . import scip_pb2
    index = scip_pb2.Index()
    index.ParseFromString(Path(scip_path).read_bytes())
    loc = exact._Locator(ix)
    pre = prefix.strip("/") + "/" if prefix.strip("/") else ""
    types_at = defaultdict(list)
    for n in ix.nodes.values():
        if n.kind == "type" and n.path and n.span_start:
            types_at[n.path].append(n)
    members = defaultdict(list)
    for n in ix.nodes.values():
        if n.kind == "callable":
            members[n.parent_id].append(n)
    definition = {}
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & 1 and not occ.symbol.startswith("local "):
                definition[occ.symbol] = (pre + doc.relative_path, occ.range[0] + 1)
    sites, files, mentions = {}, set(), set()
    for doc in index.documents:
        path = pre + doc.relative_path
        files.add(path)
        src = ix.root / path
        lines = src.read_text(errors="replace").splitlines() if src.is_file() else []
        for occ in doc.occurrences:
            sym = occ.symbol
            if occ.symbol_roles & 1:
                continue
            row = occ.range[0]
            if row >= len(lines):
                continue
            end = occ.range[2] if len(occ.range) == 3 else occ.range[3]
            if sym.startswith("local "):
                # A local the compiler could not tie to a declaration (often an import it failed to follow):
                # a call through it is not judged either way.
                src_node = loc.enclosing(path, row + 1)
                word = lines[row][occ.range[1]:end].strip()
                if src_node is not None and lines[row][end:].lstrip().startswith("("):
                    sites.setdefault((src_node.id, row + 1, word), None)
                continue
            text = lines[row]
            after, before = text[end:].lstrip(), text[:occ.range[1]].rstrip()
            called = after.startswith(("(", "!(")) or (after.startswith("<") and "(" in after) or before.endswith(("<", "new"))
            word = text[occ.range[1]:end].strip()
            name = exact._scip_name(sym).strip("<>")
            src_node = loc.enclosing(path, row + 1)
            if src_node is None:
                continue
            dst = None
            if sym in definition:
                tf, tl = definition[sym]
                dst = loc.target(tf, tl, name, ("callable",))
                if dst is None and called and (name in CTOR_NAMES or word[:1].isupper()):
                    # `new Foo()` points at the class or at its constructor: either way, the constructor.
                    t = next((n for n in types_at.get(tf, ()) if n.name in (word, name) and n.span_start <= tl <= (n.span_end or n.span_start)), None)
                    ctor = [m for m in members.get(t.id, ()) if m.name in CTOR_NAMES] if t is not None else []
                    dst = ctor[0] if ctor else None
            if not called:
                if dst is not None and src_node.id != dst.id and not before.lstrip().startswith(("import", "export", "from")):
                    mentions.add((src_node.id, dst.id))
                continue
            key = (src_node.id, row + 1, word or name)
            if dst is not None and dst.id != src_node.id:
                sites[key] = dst.id
            else:
                sites.setdefault(key, None)
    return sites, files, mentions


def roslyn_sites(ix) -> tuple[dict, set, set]:
    """The same as compiler_sites, from the C# compiler run directly (no SCIP indexer needed)."""
    records, info = exact.roslyn(ix)
    loc = exact._Locator(ix)
    sites, files = {}, set()
    for r in records:
        if r["k"] == "file":
            files.add(r["f"])
            continue
        if r["k"] != "call":
            continue
        src = loc.enclosing(r["f"], r["l"])
        if src is None:
            continue
        name = r.get("tn") or r["n"]
        dst = None
        if r["s"] in ("ok", "candidate") and r.get("tf"):
            for nm in ((name, r["n"]) if name != ".ctor" else (".ctor", r["n"], "constructor")):
                dst = loc.target(r["tf"], r["tl"], nm, ("callable",))
                if dst is not None:
                    break
        key = (src.id, r["l"], ix.nodes[dst.parent_id].name if dst is not None and name == ".ctor" and dst.parent_id in ix.nodes else r["n"])
        if dst is not None and dst.id != src.id:
            sites[key] = dst.id
        else:
            sites.setdefault(key, None)
    return sites, files, set()


def grade(root: str, scip_path: str, prefix: str = "", repo_id: Optional[str] = None, db: Optional[str] = None) -> dict:
    root_p = Path(root).resolve()
    ix = Indexer(root_p, repo_id)
    started = time.perf_counter()
    db = db or str(root_p / ".leyline" / "grade.db")
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    con = store.connect(db)
    ix.run(con)
    con.close()
    seconds = round(time.perf_counter() - started, 2)
    sites, files, mentions = roslyn_sites(ix) if scip_path == "roslyn" else compiler_sites(ix, scip_path, prefix)
    on_map = {n.id for n in ix.nodes.values() if n.kind in ("callable", "test")}
    truth = {(k[0], v) for k, v in sites.items() if v and k[0] in on_map and v in on_map}
    by_line = defaultdict(dict)
    for (s_id, line, word), dst in sites.items():
        by_line[(s_id, line)][word] = dst
    judged, silent, offmap = defaultdict(set), defaultdict(set), 0
    refs = set()
    for src, dst, dispatch, precision, line in ix.calls:
        node = ix.nodes.get(src)
        if node is None or node.path not in files or src == dst:
            continue
        if dispatch == "fixture":
            continue    # pytest handing a test its fixture is not a call
        if dispatch == "reference":
            refs.add((src, dst))
            continue
        name = ix.nodes[dst].name
        verdicts = by_line.get((src, line), {})
        if name in CTOR_NAMES:
            name = ix.nodes[ix.nodes[dst].parent_id].name if ix.nodes[dst].parent_id in ix.nodes else name
        if name in verdicts:
            if verdicts[name] is None:
                offmap += 1        # the compiler resolved it to something that is not a function on the map: not judged
                continue
            judged[precision].add((src, dst, verdicts[name] == dst))
        else:
            silent[precision].add((src, dst))   # the compiler saw no call of that name there
    every = {(s_, d) for v in judged.values() for s_, d, _ in v} | {p for v in silent.values() for p in v}
    right = {(s_, d) for v in judged.values() for s_, d, ok in v if ok}
    out = {"seconds_to_index": seconds, "files_compared": len(files),
           "functions_on_map": len(on_map), "compiler_links": len(truth), "adapter_links": len(every),
           # Precision counts only links at sites the compiler resolved. A link at a site where the compiler
           # recorded no call of that name is listed apart: there the compiler could not see either.
           "precision": round(len(right) / max(1, sum(len(v) for v in judged.values())), 3) if judged else None,
           "compiler_silent": sum(len(v) for v in silent.values()),
           "recall": round(len(right & truth) / len(truth), 3) if truth else None,
           "by_kind": {k: {"judged": len(judged[k]), "right": sum(ok for *_, ok in judged[k]), "compiler_silent": len(silent[k])}
                       for k in sorted(set(judged) | set(silent))},
           "not_judged_compiler_target_off_map": offmap,
           "references": {"links": len(refs), "precision": round(len(refs & (mentions | truth)) / len(refs), 3) if refs else None},
           "adapters": sorted(k for k in ix.stats if k.startswith(("tree-sitter", "generic")))}
    wrong_pairs = {(s_, d) for v in judged.values() for s_, d, ok in v if not ok} | {p for v in silent.values() for p in v}
    every = wrong_pairs | right
    missed = Counter()
    for s, d in truth - right:
        missed[ix.nodes[d].name] += 1
    wrong = Counter()
    for s, d in wrong_pairs:
        wrong[ix.nodes[d].name] += 1
    out["most_missed"] = missed.most_common(12)
    out["most_wrong"] = wrong.most_common(12)
    out["_samples"] = {"missed": sorted(truth - right)[:400], "wrong": sorted(wrong_pairs)[:400]}
    return out
