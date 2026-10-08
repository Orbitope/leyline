"""Exact references: replace links worked out from syntax with links a compiler resolved.

Two sources produce the same record shape:

- `roslyn`: for C#. A small program built against the compiler inside the .NET SDK binds every
  module and prints each call and field access it resolved. It needs `dotnet` on the PATH and no
  package restore; a module whose packages are missing still binds whatever refers to source.
- `scip`: any language with a SCIP indexer (scip-python, scip-typescript, scip-java ...). Reads an
  `index.scip` file.

A record is {"k": call|read|write|readwrite|init|file, "f": path, "l": line, "n": name as written,
"s": ok|candidate|none|ambiguous|outside, "tf": target path, "tl": target line, "tn": target name}.

`apply` runs inside an index pass, after the syntax resolvers and before flows are built.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from importlib import resources
from pathlib import Path
from typing import Optional

from .model import Edge

TOOL_VERSION = "2"


def _timeout() -> float:
    """Seconds the compiler may take before the syntax-based links are kept instead. LEYLINE_EXACT_TIMEOUT."""
    try:
        return max(1.0, float(os.environ.get("LEYLINE_EXACT_TIMEOUT", "") or 1800))
    except ValueError:
        return 1800.0


def _quiet(timeout: float) -> dict:
    """subprocess.run arguments for dotnet: no first-run banner or telemetry prompt, nothing read from the terminal,
    output read as UTF-8 whatever the locale is, and a time limit."""
    env = {**os.environ, "DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1"}
    return {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "stdin": subprocess.DEVNULL,
            "timeout": timeout, "env": env}


# -- sources ---------------------------------------------------------------------------------
def _tool() -> Optional[Path]:
    """Build the exporter once per version into the user's cache. Returns the dll, or None."""
    if not shutil.which("dotnet"):
        return None
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "leyline" / f"roslyn-refs-{TOOL_VERSION}"
    dll = cache / "RoslynRefs.dll"
    if dll.exists():
        return dll
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src"
        src.mkdir()
        for name in ("Program.cs", "RoslynRefs.csproj"):
            (src / name).write_text(resources.files("leyline").joinpath(f"roslyn_refs/{name}").read_text(encoding="utf-8"),
                                    encoding="utf-8")
        empty = Path(tmp) / "no-packages"
        empty.mkdir()
        # The project has no package references; an empty source keeps restore from going to the network.
        try:
            run = subprocess.run(["dotnet", "build", str(src), "-c", "Release", "-o", str(cache), "--source", str(empty),
                                  "--nologo", "-v", "q"], **_quiet(_timeout()))
        except subprocess.TimeoutExpired:
            shutil.rmtree(cache, ignore_errors=True)
            raise RuntimeError(f"building the C# reference exporter took longer than {_timeout():g} s") from None
        if run.returncode != 0 or not dll.exists():
            shutil.rmtree(cache, ignore_errors=True)
            raise RuntimeError("could not build the C# reference exporter:\n" + (run.stdout + run.stderr)[-1500:])
    return dll


def roslyn(ix) -> tuple[list[dict], dict]:
    """Run the compiler over the C# modules of an indexer. Returns (records, per-module summary)."""
    dll = _tool()
    if dll is None:
        return [], {"status": "skipped", "reason": "dotnet is not on the PATH"}
    files_by_module = defaultdict(list)
    for fid, lang in ix.file_lang.items():
        if lang == "csharp":
            files_by_module[ix.nodes[fid].parent_id].append(ix.nodes[fid].path)
    if not files_by_module:
        return [], {"status": "no_files"}
    imports = defaultdict(set)
    for e in ix.edges:
        if e.kind == "imports" and e.dst_id in files_by_module:
            imports[ix.nodes[e.src_id].parent_id if e.src_id in ix.file_lang else e.src_id].add(e.dst_id)
    modules = []
    for mod, files in sorted(files_by_module.items()):
        refs = set(ix.project_refs.get(mod, ())) | imports.get(mod, set())
        modules.append({"name": mod, "files": sorted(files), "refs": sorted(r for r in refs if r != mod and r in files_by_module)})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump({"root": str(ix.root), "modules": modules}, f)
        manifest = f.name
    try:
        run = subprocess.run(["dotnet", str(dll), manifest], **_quiet(_timeout()))
    except subprocess.TimeoutExpired:
        return [], {"status": "failed", "reason": f"the compiler took longer than {_timeout():g} s"
                                                  " (LEYLINE_EXACT_TIMEOUT raises the limit)"}
    finally:
        os.unlink(manifest)
    if run.returncode != 0:
        return [], {"status": "failed", "reason": run.stderr[-800:]}
    records = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")]
    return records, {"status": "ok", "modules": len(modules)}


def scip(path: str | Path, root: Optional[str | Path] = None) -> list[dict]:
    """Read a SCIP index into records. Calls come from references to method symbols; field access
    from references to members, when the indexer says whether they read or write.
    With `root`, the source is consulted to tell a call from a method that is only mentioned."""
    try:
        from . import scip_pb2
    except Exception as exc:  # the generated reader needs a recent protobuf runtime
        raise RuntimeError(f"reading SCIP needs the protobuf package (pip install 'protobuf>=7.35'): {exc}") from exc
    index = scip_pb2.Index()
    index.ParseFromString(Path(path).read_bytes())
    definition: dict[str, tuple] = {}
    for doc in index.documents:
        for occ in doc.occurrences:
            if occ.symbol_roles & 1 and not occ.symbol.startswith("local ") and occ_range(occ):
                definition[occ.symbol] = (doc.relative_path, occ_range(occ)[0] + 1)
    # Some indexers (scip-python) mark every reference as a read. Then the roles say nothing about field access.
    roles_mean_something = any(occ.symbol_roles & 4 and not occ.symbol_roles & 1 for doc in index.documents for occ in doc.occurrences)
    out = []
    for doc in index.documents:
        out.append({"k": "file", "f": doc.relative_path, "e": 0, "unbound_unknown": True})
        src_path = Path(root) / doc.relative_path if root else None
        try:
            from .indexer import source_lines
            lines = source_lines(src_path) if src_path is not None and src_path.is_file() else None
        except OSError:
            lines = None
        for occ in doc.occurrences:
            sym = occ.symbol
            if occ.symbol_roles & 1 or sym.startswith("local ") or sym not in definition:
                continue
            row, col, end = occ_range(occ) or (-1, 0, 0)
            if row < 0:
                continue
            name = _scip_name(sym)
            if not name:
                continue
            tf, tl = definition[sym]
            if sym.endswith(")."):
                kind = "call"
                if lines is not None and row < len(lines):
                    # SCIP marks a mention, not a call. Keep it when the name is followed by "(", is used as a
                    # decorator, or is read like an attribute (a property); drop imports and methods passed as values.
                    text = lines[row]
                    after = text[end:].lstrip()
                    before = text[:col].strip()
                    called = after.startswith("(") or before.endswith("@") or before == "@"
                    if not called:
                        if before.startswith(("import ", "from ")) or " import " in before:
                            continue
                        kind = "mention"
            elif sym.endswith(".") and "#" in sym.rsplit("/", 1)[-1]:
                write, read = occ.symbol_roles & 4, occ.symbol_roles & 8
                if not roles_mean_something or not (write or read):
                    continue  # the indexer did not say; leave field access to the syntax pass
                kind = "readwrite" if write and read else "write" if write else "read"
            else:
                continue
            out.append({"k": kind, "f": doc.relative_path, "l": row + 1, "n": name, "s": "ok",
                        "tf": tf, "tl": tl, "tn": name})
    return out


def occ_range(occ) -> tuple:
    """(line, start column, end column) of an occurrence, 0-based. Newer indexers (scip-java) write the typed
    single_line_range / multi_line_range fields instead of the packed `range` list; None when there is neither."""
    r = list(occ.range)
    if r:
        return (r[0], r[1], r[2] if len(r) == 3 else r[3])
    if occ.HasField("single_line_range"):
        x = occ.single_line_range
        return (x.line, x.start_character, x.end_character)
    if occ.HasField("multi_line_range"):
        x = occ.multi_line_range
        return (x.start_line, x.start_character, x.end_character)
    return None


def scip_root(path: str | Path) -> Optional[Path]:
    """The directory a SCIP index was made in (its metadata's project root), or None."""
    from . import scip_pb2
    index = scip_pb2.Index()
    index.ParseFromString(Path(path).read_bytes())
    root = index.metadata.project_root
    if not root:
        return None
    return Path(root[len("file://"):] if root.startswith("file://") else root).resolve()


def _scip_name(symbol: str) -> str:
    """The last descriptor's name: `... pkg/Engine#start().` -> start."""
    tail = symbol.rstrip(".")
    if tail.endswith(")"):
        tail = tail[: tail.rfind("(")]
    for sep in ("#", "/", ".", " "):
        tail = tail.rsplit(sep, 1)[-1]
    # rust-analyzer names an impl's method `impl#[Type][Trait]method`
    return re.sub(r"^(\[[^\]]*\])+", "", tail).strip("`")


# -- applying ----------------------------------------------------------------------------------
class _Locator:
    def __init__(self, ix, repo: Optional[str] = None):
        self.nodes = ix.nodes
        self.by_path = defaultdict(list)
        for n in ix.nodes.values():
            if repo is not None and not n.id.startswith(repo + ":"):
                continue   # records name paths inside one repository of a workspace
            if n.kind in ("callable", "test", "field") and n.path and n.span_start:
                self.by_path[n.path].append((n.span_start, n.span_end or n.span_start, n))

    def enclosing(self, path: str, line: int):
        """The innermost function, test or property body a line sits in."""
        best = None
        for start, end, n in self.by_path.get(path, ()):
            if start <= line <= end and (best is None or (end - start, -start) < (best[1] - best[0], -best[0])):
                best = (start, end, n)
        return best[2] if best else None

    def caller(self, path: str, line: int):
        """The function that makes a call written at a line. Mostly the innermost one around it, but a Python
        function's decorators and default values run where the function is defined, and the call that
        registers an inline test (`T.Run("name", () => ...)`) is made by the test's parent."""
        best = None
        for start, end, n in self.by_path.get(path, ()):
            if not start <= line <= end:
                continue
            if n.language == "python" and line < (n.attrs.get("body_line") or start):
                continue
            if best is None or (end - start, -start) < (best[1] - best[0], -best[0]):
                best = (start, end, n)
        src = best[2] if best else None
        if src is not None and src.kind == "test" and line == src.span_start and src.parent_id in self.nodes:
            src = self.nodes[src.parent_id]
        return src

    def target(self, path: str, line: int, name: str, kinds: tuple):
        """The declaration a compiler pointed at: the innermost node of that name whose span holds the line."""
        best = None
        for start, end, n in self.by_path.get(path, ()):
            if n.kind in kinds and n.name == name and start <= line <= end and (best is None or start > best[0]):
                best = (start, n)
        if best is None:  # declaration line outside the span we recorded (attributes, decorators): nearest above
            near = [(abs(start - line), start, n) for start, end, n in self.by_path.get(path, ())
                    if n.kind in kinds and n.name == name and abs(start - line) <= 6]
            best = min(near, key=lambda x: x[:2])[1:] if near else None
        return best[1] if best else None


def apply(ix, records: list[dict], source: str, repo: Optional[str] = None) -> dict:
    """Correct an indexer's calls and field-access edges with compiler-resolved records.
    `repo`: in a workspace, the repository whose files the records' paths are relative to."""
    loc = _Locator(ix, repo)
    complete = {r["f"]: r.get("e", 0) == 0 and not r.get("unbound_unknown") for r in records if r["k"] == "file"}
    stats = defaultdict(int)

    # Calls, grouped by (calling function, name called).
    groups: dict[tuple, dict] = defaultdict(lambda: {"targets": {}, "open": False, "seen": 0})
    for r in records:
        if r["k"] == "mention":
            # A method named without being called. It runs here only if it is a property.
            hit = loc.target(r["tf"], r["tl"], r.get("tn") or r["n"], ("callable",))
            if hit is None or not any("property" in d for d in hit.attrs.get("decorators") or []):
                continue
        elif r["k"] != "call":
            continue
        src = loc.caller(r["f"], r["l"])
        if src is None:
            continue
        g = groups[(src.id, r.get("tn") or r["n"])] if r["s"] in ("ok", "candidate") else groups[(src.id, r["n"])]
        g["seen"] += 1
        g.setdefault("lines", defaultdict(set))
        if r["s"] in ("none", "ambiguous"):
            g["open"] = True
        elif r["s"] in ("ok", "candidate"):
            dst = loc.target(r["tf"], r["tl"], r.get("tn") or r["n"], ("callable",))
            if dst is None:
                stats["targets_not_on_the_map"] += 1
                g["open"] = True
            else:
                sure = r["s"] == "ok"
                g["targets"][dst.id] = (g["targets"].get(dst.id, (False, r["l"]))[0] or sure, r["l"])
                g["lines"][r["l"]].add(dst.id)
                if not sure:
                    g["open"] = True
    if groups:
        sites = defaultdict(int)   # how many times the syntax pass saw (function, name) called
        for fid, res in ix.results.items():
            for c in ix._open(fid, res).calls:
                sites[(c.src_id, c.name)] += 1
        ix._shut()
        kept = []
        have = defaultdict(set)
        for row in ix.calls:
            src, dst, dispatch, precision, line = row
            name = ix.nodes[dst].name if dst in ix.nodes else None
            g = groups.get((src, name))
            if g is None or dispatch == "fixture":
                kept.append(row)
                continue
            # A SCIP index does not say which call sites it failed to bind, so it may only overrule a link
            # made on a line where it bound that same name to something else.
            open_ = g["open"] if source == "roslyn" else not (g["lines"].get(line) and dst not in g["lines"][line])
            if dst in g["targets"]:
                sure = g["targets"][dst][0]
                kept.append((src, dst, dispatch, "exact" if sure else precision, line))
                have[(src, name)].add(dst)
                stats["calls_confirmed"] += sure
            elif open_:
                kept.append(row)
            else:
                stats["calls_removed"] += 1   # the compiler bound every such call in this function elsewhere
        for (src, name), g in groups.items():
            for dst, (sure, line) in g["targets"].items():
                if dst not in have[(src, name)]:
                    dispatch = "virtual" if ix.nodes[dst].attrs.get("is_virtual") and ix.nodes[dst].language == "csharp" else "static"
                    kept.append((src, dst, dispatch, "exact" if sure else "heuristic", line))
                    stats["calls_added"] += 1
        ix.calls[:] = kept

    # Field access, per function.
    found: dict[tuple, list] = {}
    touched_src = set()
    for r in records:
        if r["k"] not in ("read", "write", "readwrite", "init") or r["s"] != "ok":
            continue
        src = loc.enclosing(r["f"], r["l"])
        dst = loc.target(r["tf"], r["tl"], r.get("tn") or r["n"], ("field",))
        if src is None or dst is None or src.id == dst.id or dst.attrs.get("native_kind") in ("enum_member", "event"):
            continue
        touched_src.add(src.id)
        for kind in (("reads",) if r["k"] == "read" else ("writes",) if r["k"] in ("write", "init") else ("reads", "writes")):
            slot = found.setdefault((kind, src.id, dst.id), [0, r["l"], 0])
            slot[0] += 1
            slot[1] = min(slot[1], r["l"])
            slot[2] += r["k"] == "init"
    if found:
        edges = []
        for e in ix.edges:
            if e.kind not in ("reads", "writes") or (repo is not None and not e.src_id.startswith(repo + ":")):
                edges.append(e)
                continue
            key = (e.kind, e.src_id, e.dst_id)
            if key in found:
                continue  # rewritten below as exact
            path = ix.nodes[e.src_id].path if e.src_id in ix.nodes else None
            if complete.get(path, False):
                stats["field_links_removed"] += 1   # the file bound cleanly and the compiler saw no such access
            else:
                edges.append(e)
        syntax = {(e.kind, e.src_id, e.dst_id) for e in ix.edges if e.kind in ("reads", "writes")}
        for (kind, src, dst), (n, line, init) in found.items():
            attrs = {"n": n, "line": line}
            if init == n:
                attrs["init"] = True
            edges.append(Edge(kind, src, dst, "exact", attrs))
            stats["field_links_confirmed" if (kind, src, dst) in syntax else "field_links_added"] += 1
        ix.edges[:] = edges
    if source == "roslyn":
        stats["files_bound_cleanly"] = sum(1 for v in complete.values() if v)
        stats["files_with_binding_errors"] = sum(1 for v in complete.values() if not v)
    return dict(stats)
