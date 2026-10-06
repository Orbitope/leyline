"""Walk a repo, run the language adapters, resolve names across files, and write facts."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from . import store
from .adapters import BY_EXTENSION
from .adapters.python import module_path
from .model import CallSite, Edge, FieldUse, FileResult, Node

SOURCE = "leyline-indexer/0.1"
MODULE_MARKERS = ("pyproject.toml", "setup.py", "package.json", "__init__.py")
SKIP_DIRS = {".git", "node_modules", "bin", "obj", "__pycache__", ".venv", "venv", ".godot", ".leyline"}


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def list_files(root: Path) -> list[str]:
    tracked = _git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if tracked is not None:
        files = [f for f in tracked.splitlines() if f]
    else:
        files = []
        for p in root.rglob("*"):
            if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS):
                files.append(p.relative_to(root).as_posix())
    return sorted(files)


def _module_dirs(files: list[str]) -> set[str]:
    """Directories that are modules because they hold a project marker."""
    dirs = set()
    for f in files:
        d, _, base = f.rpartition("/")
        if base.endswith(".csproj") or base in MODULE_MARKERS:
            dirs.add(d)
    # A nested __init__.py belongs to its top-most package, not to a module of its own.
    return {d for d in dirs if not any(
        d != o and d.startswith(o + "/") and o != "" for o in dirs)}


def _module_for(path: str, module_dirs: set[str]) -> str:
    d = path.rpartition("/")[0]
    cur = d
    while True:
        if cur in module_dirs and cur != "":
            return cur
        if "/" not in cur:
            break
        cur = cur.rpartition("/")[0]
    if cur in module_dirs and cur != "":
        return cur
    return d  # no marker: the file's own directory ("" is the repo root)


def _arity(type_id: str) -> int:
    tail = type_id.rsplit("`", 1)
    return int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else 0


class Indexer:
    def __init__(self, root: str | Path, repo_id: Optional[str] = None):
        self.root = Path(root).resolve()
        self.repo = repo_id or self.root.name
        self.commit = _git(self.root, "rev-parse", "HEAD")
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.calls: list[tuple] = []
        self.results: dict[str, FileResult] = {}  # file id -> adapter output
        self.file_lang: dict[str, str] = {}
        self.stats: dict[str, Counter] = defaultdict(Counter)
        self.flows: list[tuple] = []
        self.call_col: dict[tuple, int] = {}
        self.flow_steps: list[tuple] = []
        self.channel_stats: dict[str, Counter] = defaultdict(Counter)
        self.project_refs: dict[str, set] = defaultdict(set)
        self._vis_cache: dict[str, set] = {}
        self._loose_reach: dict[str, set] = {}
        self.exact_mode = "off"          # off | auto | roslyn | scip
        self.scip_paths: list[str] = []
        self.exact_stats: dict[str, dict] = {}

    def _apply_exact(self) -> None:
        """Let a compiler overrule the syntax resolvers where one is available."""
        if self.exact_mode == "off":
            return
        from . import exact
        if self.exact_mode in ("auto", "roslyn") and any(v == "csharp" for v in self.file_lang.values()):
            try:
                records, info = exact.roslyn(self)
            except RuntimeError as exc:
                records, info = [], {"status": "failed", "reason": str(exc)[-600:]}
            if records:
                info.update(exact.apply(self, records, "roslyn"))
            self.exact_stats["exact:roslyn"] = info
        paths = list(self.scip_paths)
        if self.exact_mode in ("auto", "scip") and not paths:
            paths = [str(p) for p in (self.root / "index.scip", self.root / ".leyline" / "index.scip") if p.is_file()]
        for path in paths:
            try:
                info = {"status": "ok", "file": path, **exact.apply(self, exact.scip(path, self.root), "scip")}
            except Exception as exc:
                info = {"status": "failed", "reason": str(exc)[-600:]}
            self.exact_stats["exact:scip"] = info

    # -- public --------------------------------------------------------------
    def run(self, con) -> dict:
        files = list_files(self.root)
        module_dirs = _module_dirs(files)
        self._add(Node(id=self.repo, kind="repo", name=self.repo, path="",
                       attrs={"url": _git(self.root, "remote", "get-url", "origin"),
                              "branch": _git(self.root, "rev-parse", "--abbrev-ref", "HEAD")}))
        for f in files:
            ext = "." + f.rsplit(".", 1)[-1] if "." in f else ""
            adapter = BY_EXTENSION.get(ext)
            if adapter is None:
                continue
            mod_dir = _module_for(f, module_dirs)
            mod_id = self._module(mod_dir, files)
            data = (self.root / f).read_bytes()
            file_id = f"{self.repo}:file:{f}"
            self._add(Node(id=file_id, kind="file", name=f.rsplit("/", 1)[-1], parent_id=mod_id,
                           language=adapter.LANGUAGE, path=f, span_start=1,
                           span_end=data.count(b"\n") + 1,
                           content_hash=hashlib.sha1(data).hexdigest(),
                           attrs={"loc": data.count(b"\n") + 1}))
            try:
                res = adapter.parse(self.repo, f, file_id, data, mod_dir or '.')
            except Exception as exc:  # one bad file must not sink the run
                self.stats[adapter.NAME]["files_failed"] += 1
                print(f"leyline: failed to parse {f}: {exc}", file=sys.stderr)
                continue
            self.results[file_id] = res
            self.file_lang[file_id] = adapter.LANGUAGE
            self.stats[adapter.NAME]["files"] += 1
            lines = data.split(b"\n")
            for n in res.nodes:
                if n.span_start and n.kind in ("type", "callable", "test", "field"):
                    # A hash of the node's own text, so a later index can tell which nodes were edited.
                    body = b"\n".join(ln.strip() for ln in lines[n.span_start - 1:n.span_end])
                    n.content_hash = hashlib.sha1(body).hexdigest()[:16]
                self._add(n)
            self.edges.extend(res.edges)
        self._projects(files)
        self._build_indexes()
        self._resolve_imports()
        self._resolve_types()
        self._resolve_overrides()
        self._resolve_calls()
        self._resolve_events()
        self._resolve_fields()
        self._apply_exact()
        self._resolve_spawns()
        self._build_flows()
        self._write(con)
        return {k: dict(v) for k, v in self.stats.items()}

    # -- structure -----------------------------------------------------------
    def _add(self, n: Node) -> None:
        if n.id in self.nodes:
            # Partial classes: keep the first declaration, note the extra file.
            first = self.nodes[n.id]
            if n.kind == "type" and n.path != first.path:
                first.attrs.setdefault("also_in", []).append(n.path)
            return
        self.nodes[n.id] = n

    def _module(self, mod_dir: str, files: list[str]) -> str:
        mid = f"{self.repo}:module:{mod_dir or '.'}"
        if mid not in self.nodes:
            marker = next((f.rsplit("/", 1)[-1] for f in files
                           if f.rpartition("/")[0] == mod_dir and
                           (f.endswith(".csproj") or f.rsplit("/", 1)[-1] in MODULE_MARKERS)), None)
            self._add(Node(id=mid, kind="module", name=mod_dir.rsplit("/", 1)[-1] or self.repo,
                           parent_id=self.repo, path=mod_dir, attrs={"marker": marker}))
        return mid

    def _projects(self, files: list[str]) -> None:
        """Project files give exact module-to-module and module-to-package edges."""
        for f in files:
            if not f.endswith(".csproj"):
                continue
            d = f.rpartition("/")[0]
            mid = f"{self.repo}:module:{d or '.'}"
            if mid not in self.nodes:
                continue
            text = (self.root / f).read_text(errors="replace")
            for ref in re.findall(r'<ProjectReference\s+Include="([^"]+)"', text):
                target = (Path(d) / ref.replace("\\", "/")).parent
                tdir = Path(*_normalize(target.parts)).as_posix() if target.parts else ""
                tid = f"{self.repo}:module:{tdir or '.'}"
                if tid in self.nodes:
                    self.edges.append(Edge("imports", mid, tid, "exact", {"via": "ProjectReference"}))
                    self.project_refs[mid].add(tid)
            for name, ver in re.findall(r'<PackageReference\s+Include="([^"]+)"(?:\s+Version="([^"]+)")?', text):
                xid = self._external("nuget", name, {"category": "package", "version": ver or None})
                self.edges.append(Edge("depends_on", mid, xid, "exact", {"version_range": ver or None}))
            sdk = re.search(r'<Project\s+Sdk="([^"/]+)(?:/([^"]+))?"', text)
            if sdk and sdk.group(1) != "Microsoft.NET.Sdk":
                xid = self._external("nuget", sdk.group(1), {"category": "sdk", "version": sdk.group(2)})
                self.edges.append(Edge("depends_on", mid, xid, "exact", {"version_range": sdk.group(2)}))
            tf = re.search(r"<TargetFramework>([^<]+)<", text)
            if tf:
                self.nodes[mid].attrs["target_framework"] = tf.group(1)

    def _external(self, eco: str, name: str, attrs: Optional[dict] = None) -> str:
        xid = f"{self.repo}:ext:{eco}:{name}"
        if xid not in self.nodes:
            self._add(Node(id=xid, kind="external", name=name, parent_id=self.repo,
                           attrs={"ecosystem": eco, **(attrs or {})}))
        return xid

    # -- indexes -------------------------------------------------------------
    def _build_indexes(self) -> None:
        self.types_by_name: dict[tuple, list[str]] = defaultdict(list)   # (lang, name) -> type ids
        self.members: dict[str, dict[str, list[Node]]] = defaultdict(lambda: defaultdict(list))
        self.by_name: dict[tuple, list[Node]] = defaultdict(list)        # (lang, name) -> callables
        self.field_type: dict[str, dict[str, str]] = defaultdict(dict)   # type id -> field -> type name
        self.field_types_global: dict[tuple, set] = defaultdict(set)     # (lang, field) -> type names
        self.bases: dict[str, list[str]] = defaultdict(list)
        self.field_names: dict[str, set] = defaultdict(set)
        self.ns_modules: dict[str, set] = defaultdict(set)               # C# namespace -> module ids
        self.py_modules: dict[str, str] = {}                             # python module path -> file id
        self.file_of: dict[str, str] = {}
        for n in self.nodes.values():
            if n.kind == "type":
                self.types_by_name[(n.language, n.name)].append(n.id)
            elif n.kind == "callable":
                self.members[n.parent_id][n.name].append(n)
                self.by_name[(n.language, n.name)].append(n)
            elif n.kind == "field":
                self.field_names[n.parent_id].add(n.name)
                tn = n.attrs.get("type_name")
                if tn:
                    self.field_type[n.parent_id][n.name] = tn
                    self.field_types_global[(n.language, n.name)].add(tn)
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            mod_id = self.nodes[fid].parent_id
            for d in res.declares:
                if lang == "csharp":
                    self.ns_modules[d].add(mod_id)
                else:
                    self.py_modules[d] = fid
            for n in res.nodes:
                self.file_of[n.id] = fid
        # A package under a source root (src/flask) is imported by its own name (flask), not by its path.
        py_files = {self.nodes[f].path: f for f in self.results if self.file_lang[f] == "python"}
        dirs_with_init = {p.rsplit("/", 1)[0] if "/" in p else "" for p in py_files if p.endswith("__init__.py")}
        for path, fid in sorted(py_files.items()):
            parts = path.split("/")
            start = len(parts) - 1
            while start > 0 and "/".join(parts[:start]) in dirs_with_init:
                start -= 1
            if 0 < start < len(parts) - 1:
                self.py_modules.setdefault(module_path("/".join(parts[start:])), fid)

    # -- imports -------------------------------------------------------------
    def _resolve_imports(self) -> None:
        self.cs_usings: dict[str, set] = defaultdict(set)      # file -> namespaces
        self.cs_static: dict[str, list] = defaultdict(list)    # file -> type names from `using static`
        self.cs_alias: dict[str, dict] = defaultdict(dict)     # file -> alias -> type name
        self.py_names: dict[str, dict] = defaultdict(dict)     # file -> local name -> (file id, symbol|None)
        seen = set()
        stdlib = getattr(sys, "stdlib_module_names", frozenset())
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            for imp in res.imports:
                if lang == "csharp":
                    if imp.alias:
                        self.cs_alias[fid][imp.alias] = imp.target.rsplit(".", 1)[-1]
                    if imp.is_static:
                        self.cs_static[fid].append(imp.target.rsplit(".", 1)[-1])
                    ns = imp.target
                    mods = self.ns_modules.get(ns)
                    if not mods and (imp.is_static or imp.alias):
                        ns = imp.target.rsplit(".", 1)[0]
                        mods = self.ns_modules.get(ns)
                    self.cs_usings[fid].add(ns)
                    vis = self._visible(fid)
                    declared = bool(mods)
                    if mods and vis is not None:
                        # A namespace can be declared by several projects; only the referenced ones count.
                        mods = {m for m in mods if m in vis or m == self.nodes[fid].parent_id}
                    if mods:
                        for m in sorted(mods):
                            key = (fid, m, ns)
                            if key not in seen and m != self.nodes[fid].parent_id:
                                seen.add(key)
                                self.edges.append(Edge("imports", fid, m, "exact", {"namespace": ns}))
                    elif not declared:
                        xid = self._external("dotnet", imp.target if not (imp.is_static or imp.alias) else ns,
                                             {"category": "namespace"})
                        if (fid, xid) not in seen:
                            seen.add((fid, xid))
                            self.edges.append(Edge("imports", fid, xid, "exact"))
                else:
                    target = self._py_module(fid, imp.target)
                    if target:
                        if imp.symbols:
                            for s in imp.symbols:
                                name, _, alias = s.partition(" as ")
                                sub = self._py_module(fid, f"{imp.target}.{name}")
                                self.py_names[fid][alias or name] = (sub, None) if sub else (target, name)
                        else:
                            self.py_names[fid][imp.alias or imp.target] = (target, None)
                        if (fid, target) not in seen and target != fid:
                            seen.add((fid, target))
                            self.edges.append(Edge("imports", fid, target, "exact", {"symbols": imp.symbols}))
                    else:
                        top = imp.target.split(".")[0] or imp.target
                        xid = self._external("python", top,
                                             {"category": "stdlib" if top in stdlib else "package"})
                        if (fid, xid) not in seen:
                            seen.add((fid, xid))
                            self.edges.append(Edge("imports", fid, xid, "exact", {"symbols": imp.symbols}))

    def _py_module(self, fid: str, target: str) -> Optional[str]:
        if target in self.py_modules:
            return self.py_modules[target]
        own = module_path(self.nodes[fid].path)
        pkg = own.rsplit(".", 1)[0] if "." in own else ""
        sibling = f"{pkg}.{target}" if pkg else target
        if sibling in self.py_modules:  # script-style import of a file in the same directory
            return self.py_modules[sibling]
        return None

    def _py_export(self, target_fid: str, symbol: str, depth: int = 0) -> Optional[str]:
        """The node a module exposes under a name, following re-exports (`from .app import Flask`)."""
        cand = f"{self.repo}:python:{module_path(self.nodes[target_fid].path)}.{symbol}"
        if cand in self.nodes:
            return cand
        if depth < 5 and symbol in self.py_names.get(target_fid, {}):
            nxt, sub = self.py_names[target_fid][symbol]
            if sub is not None:
                return self._py_export(nxt, sub, depth + 1)
        return None

    def _py_fixtures(self) -> None:
        """pytest passes a test each fixture named by its parameters. Link them, and give the
        parameter the fixture's return type so calls on it can be resolved."""
        self.py_param_type: dict[tuple, str] = {}
        by_file: dict[str, dict[str, Node]] = defaultdict(dict)
        for n in self.nodes.values():
            if n.language == "python" and n.kind == "callable" and n.attrs.get("is_fixture"):
                by_file[self.file_of[n.id]][n.name] = n
        if not by_file:
            return
        conftests = {self.nodes[f].path.rsplit("/", 1)[0] if "/" in self.nodes[f].path else "": f
                     for f in by_file if self.nodes[f].path.rsplit("/", 1)[-1] == "conftest.py"}

        def find(fid: str, name: str, skip: Optional[str] = None) -> Optional[Node]:
            hit = by_file.get(fid, {}).get(name)
            if hit is not None and hit.id != skip:
                return hit
            d = self.nodes[fid].path.rsplit("/", 1)[0] if "/" in self.nodes[fid].path else ""
            while True:
                hit = by_file.get(conftests.get(d, ""), {}).get(name)
                if hit is not None and hit.id != skip:
                    return hit
                if not d:
                    return None
                d = d.rsplit("/", 1)[0] if "/" in d else ""
        memo: dict[str, Optional[str]] = {}

        def returns(fx: Node, depth: int = 0) -> Optional[str]:
            if fx.id in memo or depth > 4:
                return memo.get(fx.id)
            memo[fx.id] = None
            out = None
            if fx.attrs.get("returns"):
                out = self._type("python", fx.attrs["returns"], fx.id)
            elif fx.attrs.get("returns_call"):
                recv, method = fx.attrs["returns_call"]
                dep = find(self.file_of[fx.id], recv, fx.id) if recv in (fx.attrs.get("params") or []) else None
                owner = returns(dep, depth + 1) if dep is not None else None
                for m in self._methods(owner, method, 0) if owner else []:
                    if m.attrs.get("returns"):
                        out = self._type("python", m.attrs["returns"], m.id)
            memo[fx.id] = out
            return out
        for n in list(self.nodes.values()):
            if n.language != "python" or n.kind != "callable" or not (n.attrs.get("is_test") or n.attrs.get("is_fixture")):
                continue
            for pname in n.attrs.get("params") or []:
                fx = find(self.file_of[n.id], pname, n.id)
                if fx is None:
                    continue
                self.calls.append((n.id, fx.id, "fixture", "heuristic", n.span_start))
                tid = returns(fx)
                if tid:
                    self.py_param_type[(n.id, pname)] = tid

    # -- visibility ----------------------------------------------------------
    def _visible(self, fid: str) -> Optional[set]:
        """Modules whose symbols a file can reference, or None when that is not knowable."""
        mod = self.nodes[fid].parent_id
        if self.file_lang[fid] == "python":
            out = {fid}
            out.update(t for t, _ in self.py_names[fid].values())
            return out
        if not (self.nodes[mod].attrs.get("marker") or "").endswith(".csproj"):
            return None  # a loose .cs file: no project file says what it can see
        if mod not in self._vis_cache:
            seen, queue = {mod}, [mod]
            while queue:
                cur = queue.pop()
                for nxt in self.project_refs.get(cur, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append(nxt)
            self._vis_cache[mod] = seen
        return self._vis_cache[mod]

    def _can_see(self, fid: str, node_id: str) -> bool:
        vis = self._visible(fid)
        if vis is None:
            return True
        target_file = self.file_of.get(node_id)
        if self.file_lang[fid] == "python":
            return target_file in vis
        return target_file is not None and self.nodes[target_file].parent_id in vis

    # -- types ---------------------------------------------------------------
    def _type(self, lang: str, name: str, from_id: Optional[str]) -> Optional[str]:
        fid = self.file_of.get(from_id or "")
        arity = None
        if lang == "csharp":
            name, tick, count = name.partition("`")
            arity = int(count) if tick and count.isdigit() else 0
            if fid:
                name = self.cs_alias[fid].get(name, name)
        if lang == "python" and fid and name in self.py_names[fid]:
            target, symbol = self.py_names[fid][name]
            cand = self._py_export(target, symbol or name)
            if cand and self.nodes[cand].kind == "type":
                return cand
        cands = self.types_by_name.get((lang, name), [])
        if lang == "csharp" and fid:
            cands = [c for c in cands if self._can_see(fid, c)]
        if arity is not None and cands:
            # Foo and Foo<T> are different types. A plain name falls back to the generic one, because
            # some callers (a receiver's inferred type) do not carry the type arguments.
            exact = [c for c in cands if _arity(c) == arity]
            cands = exact or ([] if arity else cands)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        if fid:
            same_file = [c for c in cands if self.file_of.get(c) == fid]
            if same_file:
                return same_file[0]
            if lang == "csharp":
                here = set(self.cs_usings[fid])
                for ns in self.results[fid].declares:  # a namespace sees its parents
                    parts = ns.split(".")
                    here.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
                here.add("")
                visible = [c for c in cands if self.nodes[c].attrs.get("namespace") in here]
                if len(visible) >= 1:
                    return visible[0]
        return None

    def _resolve_types(self) -> None:
        seen = set()
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            adapter = f"tree-sitter-{'c-sharp' if lang == 'csharp' else lang}"
            for ref in res.type_refs:
                for i, name in enumerate(ref.names):
                    tid = self._type(lang, name, ref.src_id)
                    if tid is None:
                        self.stats[adapter]["type_refs_external"] += 1
                        continue
                    self.stats[adapter]["type_refs_resolved"] += 1
                    if ref.role == "base":
                        if i > 0:
                            kind, attrs = "uses_type", {"role": "generic_arg"}
                        else:
                            target_iface = self.nodes[tid].attrs.get("native_kind") == "interface"
                            src_iface = self.nodes[ref.src_id].attrs.get("native_kind") == "interface"
                            kind, attrs = ("implements" if target_iface and not src_iface else "extends"), {}
                            self.bases[ref.src_id].append(tid)
                    elif ref.role == "instantiate":
                        kind, attrs = ("instantiates" if i == 0 else "uses_type"), ({} if i == 0 else {"role": "generic_arg"})
                    else:
                        kind, attrs = "uses_type", {"role": ref.role if i == 0 else "generic_arg"}
                    key = (kind, ref.src_id, tid, attrs.get("role"))
                    if key in seen or ref.src_id == tid:
                        continue
                    seen.add(key)
                    self.edges.append(Edge(kind, ref.src_id, tid, "heuristic", attrs))

    # -- calls ---------------------------------------------------------------
    def _chain(self, type_id: Optional[str]) -> list[str]:
        """A type, its bases, and its enclosing types, nearest first."""
        out, queue = [], [type_id] if type_id else []
        while queue:
            t = queue.pop(0)
            if t in out or t not in self.nodes:
                continue
            out.append(t)
            queue.extend(self.bases.get(t, []))
            parent = self.nodes[t].parent_id
            if parent in self.nodes and self.nodes[parent].kind == "type":
                queue.append(parent)
        return out

    def _pick(self, cands: list[Node], argc: int, skip: int = 0) -> list[Node]:
        """Overloads that fit a call. `skip` is 1 for an extension method, whose first parameter is the receiver."""
        argc += skip
        fit = [c for c in cands if c.attrs.get("argc_min", 0) <= argc <= c.attrs.get("argc_max", 99)]
        call = getattr(self, "_call", None)
        if len(fit) > 1 and call is not None:
            # Several overloads take this many arguments. Narrow by what the call site shows.
            if call.targs:
                fit = [c for c in fit if c.attrs.get("generic_arity") == call.targs] or fit
            hints = call.args

            def possible(c: Node) -> bool:
                want = c.attrs.get("delegate_arity") or []
                for i, h in enumerate(hints, skip):
                    if isinstance(h, int) and not isinstance(h, bool) and i < len(want):
                        if want[i] == -2 or (want[i] >= 0 and want[i] != h):
                            return False  # a lambda passed where no delegate of that shape is taken
                    elif isinstance(h, str) and i < len(want) and want[i] >= 0 and h in ("int", "double", "bool", "char", "string"):
                        return False  # a literal passed where a delegate is taken
                return True

            def score(c: Node) -> int:
                want, types = c.attrs.get("delegate_arity") or [], c.attrs.get("param_types") or []
                n = 0
                for i, h in enumerate(hints, skip):
                    if isinstance(h, int) and i < len(want) and want[i] == h:
                        n += 1
                    elif isinstance(h, str) and i < len(types) and types[i].split("`")[0] == h:
                        n += 1
                return n
            fit = [c for c in fit if possible(c)] or fit
            best = max(score(c) for c in fit)
            fit = [c for c in fit if score(c) == best]
        return fit or cands

    def _methods(self, type_id: Optional[str], name: str, argc: int) -> list[Node]:
        for t in self._chain(type_id):
            cands = self.members.get(t, {}).get(name)
            if cands:
                return self._pick(cands, argc)
        return []

    def _receiver_type(self, lang: str, call: CallSite) -> tuple[Optional[str], bool]:
        """Returns (type id, known). known=True with None means an external type."""
        r = call.receiver
        if r in (None, "this"):
            return call.enclosing_type, call.enclosing_type is not None
        if r == "base":
            bases = self.bases.get(call.enclosing_type or "", [])
            return (bases[0] if bases else None), True
        if call.receiver_type:
            return self._type(lang, call.receiver_type, call.src_id), True
        if call.chain is not None and not call.receiver_type:
            got = self._returned_type(lang, call.chain)
            if got is not None:
                return got
        if r in ("?", "[]"):
            return None, False
        if r.startswith("."):
            names = self.field_types_global.get((lang, r[1:]), set())
            ids = {self._type(lang, n, call.src_id) for n in names}
            ids.discard(None)
            if len(ids) == 1:
                return ids.pop(), True
            if names and not ids:
                return None, True  # every field of that name has an external type
            return None, False
        if lang == "python":
            # A parameter filled by a pytest fixture, seen from the test or a function nested in it.
            cur = call.src_id
            while cur in self.nodes and self.nodes[cur].kind == "callable":
                if (cur, r) in self.py_param_type:
                    return self.py_param_type[(cur, r)], True
                if r in (self.nodes[cur].attrs.get("params") or []):
                    break
                cur = self.nodes[cur].parent_id
        # A bare identifier: a field of the enclosing type chain, or a type name (static call).
        for t in self._chain(call.enclosing_type):
            tn = self.field_type.get(t, {}).get(r)
            if tn:
                return self._type(lang, tn, call.src_id), True
        tid = self._type(lang, r, call.src_id)
        if tid:
            return tid, True
        if lang == "csharp" and r[:1].isupper() and not any(
                r in self.field_names.get(t, ()) for t in self._chain(call.enclosing_type)):
            return None, True  # PascalCase, not a member, not ours: a type from outside (File.Open)
        return None, False

    WRAPPERS = {"Task", "ValueTask", "Task`1", "ValueTask`1", "Nullable`1"}

    def _returned_type(self, lang: str, inner: CallSite, depth: int = 0) -> Optional[tuple]:
        """The type of a call's result, from the declared return type of what it resolves to.
        Returns (type id or None, known) like _receiver_type, or None when nothing can be said."""
        key = id(inner)
        if key in self._chain_memo:
            return self._chain_memo[key]
        self._chain_memo[key] = None
        if depth > 6:
            return None
        saved = (getattr(self, "_guessed", False), getattr(self, "_call", None), getattr(self, "_last_src", None))
        self._guessed = False
        stats_before = {k: dict(v) for k, v in self.stats.items()}
        self._call = inner
        fid = self.file_of.get(inner.src_id)
        targets = self._resolve_call(lang, fid, inner) if fid else None
        guessed = self._guessed
        self._guessed, self._call, self._last_src = saved[0], saved[1], saved[2]
        for k, v in stats_before.items():  # the inner call is counted when its own turn comes
            self.stats[k].clear()
            self.stats[k].update(v)
        out = None
        if targets and not guessed:
            t = targets[0]
            if t.name in (".ctor", "__init__"):
                out = (t.parent_id, True)
            elif lang == "python":
                if t.attrs.get("returns"):
                    tid = self._type(lang, t.attrs["returns"], t.id)
                    out = (tid, True) if tid else None
            else:
                names = [n for n in t.attrs.get("returns_names") or [] if n not in self.WRAPPERS]
                if names and names[0] not in (t.attrs.get("type_params") or []):
                    tid = self._type(lang, names[0], t.id)
                    owner_params = re.findall(r"[A-Za-z_]\w*", (self.nodes[t.parent_id].attrs.get("signature") or "").split(":")[0].partition("<")[2]) \
                        if t.parent_id in self.nodes else []
                    if tid:
                        out = (tid, True)
                    elif names[0].split("`")[0] not in owner_params and names[0] not in ("void", "var", "dynamic", "object"):
                        out = (None, True)  # a declared type from outside the workspace
        self._chain_memo[key] = out
        return out

    def _extensions(self, lang: str, fid: str, tid: str, name: str, argc: int) -> list[Node]:
        """Extension methods called on a receiver of a known type: static methods whose `this` parameter is that type."""
        cands = [c for c in self.by_name.get((lang, name), ()) if c.attrs.get("is_extension") and self._can_see(fid, c.id)]
        if not cands:
            return []
        # Foo and Foo<T> are different receivers: compare the name together with its generic arity.
        names = {self.nodes[t].name.split("`")[0] + (f"`{_arity(t)}" if _arity(t) else "")
                 for t in self._chain(tid) if t in self.nodes}
        exact = [c for c in cands if (c.attrs.get("param_types") or [""])[0] in names]
        loose = [c for c in cands if (c.attrs.get("param_types") or [""])[0] in (c.attrs.get("type_params") or [])]
        pool = exact or loose
        if not pool:
            return []
        if not exact:
            self._guessed = True  # `this TBuilder builder`: the constraint is not checked
        return self._pick(pool, argc, skip=1)

    def _resolve_calls(self) -> None:
        self._chain_memo: dict = {}
        # Names seen on receivers of a known outside type (List.Add, dict.get). A call to such a
        # name on a receiver of unknown type is never guessed.
        self.outside_names: set = set()
        self._py_fixtures()
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            for call in res.calls:
                if call.receiver not in (None, "this", "base") and call.name != ".ctor":
                    if lang == "python" and call.receiver in self.py_names[fid]:
                        continue
                    tid, known = self._receiver_type(lang, call)
                    if known and tid is None:
                        self.outside_names.add((lang, call.name))
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            adapter = f"tree-sitter-{'c-sharp' if lang == 'csharp' else lang}"
            st = self.stats[adapter]
            for call in res.calls:
                st["calls_total"] += 1
                self._guessed = False
                self._call = call
                targets = self._resolve_call(lang, fid, call)
                if targets is None:
                    st["calls_external"] += 1
                elif not targets:
                    st["calls_unresolved"] += 1
                else:
                    st["calls_resolved"] += 1
                    for t in targets:
                        dispatch = "virtual" if t.attrs.get("is_virtual") and lang == "csharp" else "static"
                        self.calls.append((call.src_id, t.id, dispatch,
                                           "guess" if self._guessed else "heuristic", call.line))
                        self.call_col[(call.src_id, t.id, call.line)] = call.col

    def _resolve_call(self, lang: str, fid: str, call: CallSite) -> Optional[list[Node]]:
        """None = defined outside the workspace; [] = defined here but not pinned down."""
        name, argc = call.name, call.argc
        self._last_src = call.src_id
        defined_here = any(self._can_see(fid, c.id) for c in self.by_name.get((lang, name), ())) or (
            lang == "python" and bool(self.types_by_name.get((lang, name))))
        if name == ".ctor":
            tid = self._type(lang, call.receiver_type or "", call.src_id)
            if tid is None:
                return None
            ctors = self.members.get(tid, {}).get(".ctor")
            return self._pick(ctors, argc) if ctors else None
        if call.receiver is None:
            # Local functions of the caller, innermost first.
            cur = call.src_id
            while cur in self.nodes and self.nodes[cur].kind in ("callable", "test"):
                local = self.members.get(cur, {}).get(name)
                if local:
                    return self._pick(local, argc)
                cur = self.nodes[cur].parent_id
            if lang == "python":
                return self._py_bare(fid, call, defined_here)
            found = self._methods(call.enclosing_type, name, argc)
            if found:
                return found
            for tn in self.cs_static[fid]:
                found = self._methods(self._type(lang, tn, call.src_id), name, argc)
                if found:
                    return found
            return [] if defined_here else None
        if lang == "python" and call.receiver not in ("this",) and call.receiver in self.py_names[fid]:
            target, symbol = self.py_names[fid][call.receiver]
            if symbol is None:
                return self._py_symbol(self._py_export(target, name) or "", argc)
            tid = self._py_export(target, symbol)
            found = self._methods(tid, name, argc)
            return found or ([] if defined_here else None)
        tid, known = self._receiver_type(lang, call)
        if tid:
            found = self._methods(tid, name, argc)
            if found:
                return found
            if lang == "csharp":
                found = self._extensions(lang, fid, tid, name, argc)
                if found:
                    return found
            return None  # the type is ours but the method is inherited from outside
        if known:
            return None  # receiver has a type that is not in the workspace
        if not defined_here:
            return None
        if (lang, name) in self.outside_names:
            self.stats[f"tree-sitter-{'c-sharp' if lang == 'csharp' else lang}"]["calls_guess_declined"] += 1
            return []
        self.stats[f"tree-sitter-{'c-sharp' if lang == 'csharp' else lang}"]["calls_by_unique_name"] += 1
        self._guessed = True
        # Receiver type unknown: accept only a name that is defined exactly once.
        cands = self._pick([c for c in self.by_name[(lang, name)] if self._can_see(fid, c.id)], argc)
        owners = {c.parent_id for c in cands}
        if len(owners) > 1:
            # Several declarations: if all but one are overrides or implementations of the same
            # root declaration, the call is to that root (virtual dispatch).
            roots = {o for o in owners if not any(b in owners for b in self._chain(o)[1:])}
            if len(roots) == 1 and all(next(iter(roots)) in self._chain(o) for o in owners):
                root = roots.pop()
                return [c for c in cands if c.parent_id == root]
            return []
        return cands

    def _resolve_overrides(self) -> None:
        """Link each method to the base or interface method it implements, by name and arity."""
        self.implementers: dict[str, list[str]] = defaultdict(list)
        for tid, bases in list(self.bases.items()):
            if not bases:
                continue
            for name, impls in self.members.get(tid, {}).items():
                if name in (".ctor", ".dtor"):
                    continue
                for impl in impls:
                    if impl.kind != "callable":
                        continue
                    for base in self._chain(tid)[1:]:
                        if self.nodes[base].kind != "type" or base == self.nodes[tid].parent_id:
                            continue
                        cands = [c for c in self.members.get(base, {}).get(name, [])
                                 if c.attrs.get("argc_max") == impl.attrs.get("argc_max")]
                        if cands:
                            self.edges.append(Edge("overrides", impl.id, cands[0].id, "heuristic"))
                            self.implementers[cands[0].id].append(impl.id)
                            break

    # -- channels ------------------------------------------------------------
    def _event_field(self, type_id: Optional[str], name: str) -> Optional[str]:
        for t in self._chain(type_id):
            fid = f"{t}.{name}"
            n = self.nodes.get(fid)
            if n is not None and n.kind == "field" and n.attrs.get("native_kind") == "event":
                return fid
        return None

    def _resolve_fields(self) -> None:
        """Link each function to the fields it reads and assigns."""
        field_id: dict[str, dict[str, str]] = defaultdict(dict)
        by_name: dict[tuple, list[str]] = defaultdict(list)
        for n in self.nodes.values():
            if n.kind == "field" and n.attrs.get("native_kind") not in ("enum_member", "event"):
                field_id[n.parent_id][n.name] = n.id
                by_name[(n.language, n.name)].append(n.id)
        found: dict[tuple, list] = {}
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            st = self.stats[f"tree-sitter-{'c-sharp' if lang == 'csharp' else lang}"]
            for u in res.field_uses:
                if u.src_id not in self.nodes:
                    continue
                target, guessed = None, False
                if u.receiver in (None, "this"):
                    if u.receiver is None and lang == "python":
                        continue  # a bare name in Python is a local or a global, never an attribute
                    owners = self._chain(u.enclosing_type)
                elif u.receiver == "base":
                    owners = self._chain(u.enclosing_type)[1:]
                else:
                    probe = CallSite(u.src_id, u.name, u.receiver, u.receiver_type, 0, u.line, u.enclosing_type, chain=u.chain)
                    tid, known = self._receiver_type(lang, probe)
                    owners = self._chain(tid) if tid else []
                    if lang == "csharp" and not tid and not known and len(u.name) > 3:
                        # Receiver type unknown: accept only a field name declared exactly once, and say it is a guess.
                        cands = [c for c in by_name.get((lang, u.name), ()) if self._can_see(fid, c)]
                        if len(cands) == 1 and (lang, u.name) not in self.outside_names:
                            target, guessed = cands[0], True
                for t in owners:
                    if u.name in field_id.get(t, ()):
                        target = field_id[t][u.name]
                        break
                if target is None or target == u.src_id:
                    continue
                st["field_uses"] += 1
                for kind in (("reads",) if u.access == "r" else ("writes",) if u.access in ("w", "i") else ("reads", "writes")):
                    slot = found.setdefault((kind, u.src_id, target), [0, u.line, False, 0])
                    slot[0] += 1
                    slot[1] = min(slot[1], u.line)
                    slot[2] = slot[2] or guessed
                    slot[3] += u.access == "i"
        for (kind, src, dst), (n, line, guessed, init) in found.items():
            attrs = {"n": n, "line": line}
            if init == n:
                attrs["init"] = True  # only ever set while creating the object, never changed afterwards
            self.edges.append(Edge(kind, src, dst, "guess" if guessed else "heuristic", attrs))

    def _resolve_events(self) -> None:
        """Link the code that raises an event to the code that handles it."""
        raisers: dict[str, list] = defaultdict(list)
        handlers: dict[str, list] = defaultdict(list)
        st = self.channel_stats["event"]
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            for ev in res.events:
                if ev.kind == "raise":
                    field = self._event_field(ev.enclosing_type, ev.event)
                    if field:
                        raisers[field].append((ev.src_id, ev.line))
                    continue
                if ev.receiver is None:
                    field = self._event_field(ev.enclosing_type, ev.event)
                else:
                    probe = CallSite(ev.src_id, ev.event, ev.receiver, ev.receiver_type, 0, ev.line, ev.enclosing_type)
                    tid, _ = self._receiver_type(lang, probe)
                    field = self._event_field(tid, ev.event) if tid else None
                if not field:
                    if ev.handler is None or ev.receiver is not None:
                        st["subscriptions_to_outside_events"] += 1
                    continue
                target, via = ev.src_id, "lambda"
                if ev.handler:
                    found = self._methods(ev.enclosing_type, ev.handler, 1) or self._methods(ev.enclosing_type, ev.handler, 0)
                    if found:
                        target, via = found[0].id, "method"
                handlers[field].append((target, via, ev.line, ev.src_id))
        seen = set()
        for field, subs in handlers.items():
            st["events_with_handlers"] += 1
            for raiser, _line in raisers.get(field, []):
                for target, via, line, subscriber in subs:
                    key = (raiser, target, field)
                    if key in seen or raiser not in self.nodes or target not in self.nodes:
                        continue
                    seen.add(key)
                    st["links"] += 1
                    self.edges.append(Edge("communicates", raiser, target, "heuristic", {
                        "channel": "event", "address": field, "direction": "push", "handler": via,
                        "subscriber": subscriber, "subscribed_at": line}))
            if field not in raisers:
                st["events_never_raised_here"] += 1
        st["events_declared"] = sum(1 for n in self.nodes.values()
                                    if n.kind == "field" and n.attrs.get("native_kind") == "event")

    def _resolve_spawns(self) -> None:
        """Link code that launches a program to that program's entry point, when it is in the workspace."""
        st = self.channel_stats["process"]
        entries: dict[str, list[str]] = defaultdict(list)  # module -> cli entry callables
        for e in self.edges:
            if e.kind == "exposes" and self.nodes[e.src_id].attrs.get("trigger") == "cli":
                mod = self.nodes[self.file_of[e.dst_id]].parent_id if e.dst_id in self.file_of else None
                if mod:
                    entries[mod].append(e.dst_id)
        files_by_path = {n.path: n.id for n in self.nodes.values() if n.kind == "file"}
        modules = [(n.path, n.id) for n in self.nodes.values() if n.kind == "module" and n.path]

        def match(text: str):
            text = text.strip().replace("\\", "/")
            if text in files_by_path:  # a script path
                fid = files_by_path[text]
                tops = [e.dst_id for e in self.edges if e.kind == "exposes" and self.file_of.get(e.dst_id) == fid]
                return tops[0] if tops else fid
            for path, mid in modules:
                stem = text.rsplit("/", 1)[-1].rsplit(".", 1)[0] if "." in text.rsplit("/", 1)[-1] else None
                if text == path or text.startswith(path + "/") or (stem and stem == path.rsplit("/", 1)[-1] and text.endswith((".dll", ".exe", ".csproj"))):
                    if not entries.get(mid):
                        continue
                    return entries[mid][0] if len(entries[mid]) == 1 else mid
            return None

        seen = set()
        for fid, res in self.results.items():
            for sp in res.spawns:
                st["launch_sites"] += 1
                hits = [(match(s), s, "heuristic") for s in sp.strings]
                hits = [h for h in hits if h[0]]
                if not hits:
                    hits = [(match(s), s, "guess") for s in dict.fromkeys(sp.file_strings)]
                    hits = [h for h in hits if h[0]]
                if not hits:
                    st["launches_of_outside_programs"] += 1
                    continue
                for target, text, precision in dict.fromkeys(hits):
                    if (sp.src_id, target) in seen or target == sp.src_id:
                        continue
                    seen.add((sp.src_id, target))
                    st["links"] += 1
                    self.edges.append(Edge("communicates", sp.src_id, target, precision, {
                        "channel": "process", "address": text,
                        "direction": "both" if sp.pipes else "start", "pipes": sp.pipes, "launched_at": sp.line}))

    # -- flows ---------------------------------------------------------------
    def _dispatch_reaches(self, home: str, impl: str) -> bool:
        """Can a program whose entry is in `home` be running this implementation of an interface?"""
        if self._visible(home) is not None:
            return self._can_see(home, impl)
        # A loose file with no project: it can only be composed of the modules its own module imports.
        mod = self.nodes[home].parent_id
        if mod not in self._loose_reach:
            files = {f for f in self.results if self.nodes[f].parent_id == mod}
            self._loose_reach[mod] = {mod} | {e.dst_id for e in self.edges if e.kind == "imports" and e.src_id in files}
        target = self.file_of.get(impl)
        return target is not None and self.nodes[target].parent_id in self._loose_reach[mod]

    def _build_flows(self, max_depth: int = 8, max_steps: int = 300) -> None:
        """Walk the call graph from every entry point and test, in source order."""
        out: dict[str, list] = defaultdict(list)
        for src, dst, _disp, _prec, line in self.calls:
            out[src].append(((line or 0) + self.call_col.get((src, dst, line), 0) / 10000, dst, "calls", None))
        for e in self.edges:
            if e.kind == "communicates":
                a = e.attrs or {}
                out[e.src_id].append((a.get("launched_at") or 10 ** 9, e.dst_id, a.get("channel", "channel"),
                                      a.get("subscriber")))
        # A call to an interface or base method may land in any implementation.
        for base, impls in self.implementers.items():
            for impl in impls:
                out[base].append((10 ** 9 - 1, impl, "dispatch", None))
        # A test declared inline runs where its runner call sits.
        for n in self.nodes.values():
            if n.kind == "test" and n.parent_id:
                out[n.parent_id].append((n.span_start or 0, n.id, "runs", None))
        for lst in out.values():
            lst.sort(key=lambda x: (x[0], x[1]))
        starts = []
        for e in self.edges:
            if e.kind == "exposes":
                ep = self.nodes[e.src_id]
                starts.append((e.dst_id, "entry", ep.attrs.get("trigger")))
        for n in self.nodes.values():
            if n.kind == "test" or n.attrs.get("is_test"):
                starts.append((n.id, "test", n.attrs.get("framework")))
        for start, kind, detail in starts:
            if start not in self.nodes:
                continue
            fid = f"flow:{start}"
            entry_file = self.file_of.get(start)
            seen, steps, truncated = {start}, [(0, 0, start, "start", None, None)], False
            stack = [(start, 0, 0)]
            # Depth-first, pre-order, each callable listed once per flow.
            def walk(node, depth, parent_seq, home):
                nonlocal truncated
                if depth >= max_depth:
                    if out.get(node):
                        truncated = True
                    return
                for line, dst, via, subscriber in out.get(node, ()):
                    if dst in seen or dst not in self.nodes:
                        continue
                    if via == "event" and subscriber not in seen:
                        continue  # nobody in this flow subscribed, so the handler does not run here
                    if via == "dispatch" and home and self.file_lang.get(home) == "csharp" \
                            and not self._dispatch_reaches(home, dst):
                        continue  # an implementation in a project this program does not reference
                    if kind == "test" and via == "runs":
                        continue
                    if len(steps) >= max_steps:
                        truncated = True
                        return
                    seen.add(dst)
                    seq = len(steps)
                    steps.append((seq, depth + 1, dst, via, int(line) if line < 10 ** 9 - 1 else None, parent_seq))
                    # A launched program is its own composition: what it can reach is judged from there.
                    walk(dst, depth + 1, seq, self.file_of.get(dst, home) if via == "process" else home)
            walk(start, 0, 0, entry_file)
            n = self.nodes[start]
            name = n.name if n.kind == "test" else start.split(":", 2)[-1].split("::")[-1]
            mods = {self.nodes[self.file_of[s[2]]].parent_id for s in steps if s[2] in self.file_of}
            self.flows.append((fid, name, "static", start, 0.0, None, "fact", SOURCE, {
                "kind": kind, "detail": detail, "steps": len(steps), "truncated": truncated,
                "modules": sorted(m for m in mods if m)}))
            self.flow_steps.extend((fid, *s) for s in steps)

    def _py_symbol(self, node_id: str, argc: int) -> Optional[list[Node]]:
        n = self.nodes.get(node_id)
        if n is None:
            return None
        if n.kind == "callable":
            return [n]
        if n.kind == "type":
            init = self._methods(n.id, "__init__", argc)
            self.edges.append(Edge("instantiates", self._last_src, n.id, "heuristic"))
            return init or None
        return None

    def _py_bare(self, fid: str, call: CallSite, defined_here: bool) -> Optional[list[Node]]:
        self._last_src = call.src_id
        mod = module_path(self.nodes[fid].path)
        hit = self._py_symbol(f"{self.repo}:python:{mod}.{call.name}", call.argc)
        if hit is not None:
            return hit
        if call.name in self.py_names[fid]:
            target, symbol = self.py_names[fid][call.name]
            return self._py_symbol(self._py_export(target, symbol or call.name) or "", call.argc)
        if f"{self.repo}:python:{mod}.{call.name}" in self.nodes:
            return None  # a class with no __init__
        return None  # builtins and star imports

    # -- write ---------------------------------------------------------------
    def _write(self, con) -> None:
        with con:
            store.clear_facts(con, self.repo)
            store.write_nodes(con, self.nodes.values(), self.repo, SOURCE, self.commit)
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"root:{self.repo}", str(self.root)))
            # Drop edges whose endpoints did not survive (defensive) and exact duplicates.
            seen, edges = set(), []
            for e in self.edges:
                key = (e.kind, e.src_id, e.dst_id, tuple(sorted((e.attrs or {}).items(), key=str)).__repr__())
                if e.src_id in self.nodes and e.dst_id in self.nodes and key not in seen:
                    seen.add(key)
                    edges.append(e)
            for n in self.nodes.values():
                if n.parent_id:
                    edges.append(Edge("contains", n.parent_id, n.id, "exact"))
            store.write_edges(con, edges, SOURCE, self.commit)
            calls = sorted({c for c in self.calls if c[0] in self.nodes})
            store.write_calls(con, calls, self.commit)
            from .adapters import ADAPTERS
            for a in ADAPTERS:
                st = dict(self.stats.get(a.NAME, {}))
                status = "ok" if st.get("files") else "no_files"
                store.write_coverage(con, self.repo, a.NAME, a.VERSION, status, self.commit, st)
            for channel in ("event", "process", "di", "http", "rpc", "queue", "db", "file"):
                ran = channel in ("event", "process")
                store.write_coverage(con, self.repo, f"communicates:{channel}", "0.1" if ran else "-",
                                     "ok" if ran else "not_analyzed", self.commit,
                                     dict(self.channel_stats.get(channel, {})))
            store.write_flows(con, self.repo, self.flows, self.flow_steps)
            store.write_coverage(con, self.repo, "flows:static", "0.1", "ok", self.commit,
                                 {"flows": len(self.flows), "steps": len(self.flow_steps)})
            for name in ("exact:roslyn", "exact:scip"):
                info = dict(self.exact_stats.get(name, {}))
                status = info.pop("status", "not_analyzed")
                store.write_coverage(con, self.repo, name, "1" if status == "ok" else "-",
                                     status if status in ("ok", "failed") else "not_analyzed", self.commit, info)
            store.write_coverage(con, self.repo, "coverage", "-", "not_analyzed", self.commit, {})
            store.rebuild_derived(con)


def _normalize(parts: tuple) -> list[str]:
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        elif p not in (".", ""):
            out.append(p)
    return out


def index(root: str | Path, db_path: str | Path, repo_id: Optional[str] = None, exact: str = "off",
          scip: Optional[list[str]] = None) -> dict:
    """Index a repository. `exact` is off, auto, roslyn or scip: whether a compiler's view of the
    references replaces the syntax-based one (see leyline.exact). `scip` lists index.scip files."""
    from . import cluster, patterns, tours

    con = store.connect(db_path)
    try:
        ix = Indexer(root, repo_id)
        ix.exact_mode, ix.scip_paths = ("scip" if scip and exact == "off" else exact), list(scip or [])
        stats = ix.run(con)
        stats.update(ix.exact_stats)
        stats["systems"] = cluster.propose(con, ix.repo)
        with con:
            store.rebuild_derived(con)
        stats["patterns"] = patterns.run(con, ix.repo)
        stats["tour"] = tours.generate(con, ix.repo)
        stats["stale_annotations"] = store.refresh_stale(con)
        return stats
    finally:
        con.close()
