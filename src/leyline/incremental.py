"""Re-map after an edit: do again only what the edit can have changed.

A full index parses every file, resolves names across the whole repository, walks every flow and rewrites every
row of the store. After a small edit nearly all of that comes out as it did last time. A run made through this
module keeps, in a cache beside the store (`leyline.db` -> `leyline.cache.db`), what the next run needs to skip
that work:

- each file's parse output, under its content hash and module, so unchanged files are not parsed again;
- what resolving each file's calls produced (calls, edges, counts), so unaffected files are not resolved again;
- fingerprints of the last run: every node's shape, each file's imports, the base types, the call graph that
  flows walk, and each file's rows in the store.

The next run then parses the changed files, runs the whole-repository passes (indexes, imports, types, fields,
events, channels), resolves the calls of the files the edit can reach, walks again the flows that pass through a
node whose calls changed, and rewrites only the rows that differ. The store comes out as a full run would write it.

Which files an edit can reach. Calls resolve by name, scope and import, so a file is resolved again when:
- its content changed;
- it mentions (anywhere in its parse output) the name or id of a node that was added, removed or changed in a way a
  resolver can read (kind, name, parent, attributes; for a type also its text, which the generic resolver reads
  declarations from), or a name whose "seen on an outside type" standing changed, or a test whose fixture types
  changed;
- it imports, directly or through other files, a file whose import resolution changed.
A change to a type's base types, to a project file (package.json, .csproj, pyproject.toml, setup.py), or to which
directories are modules makes every file's calls be resolved again (still without parsing them again). With
LEYLINE_VERIFY=1 every incremental run is followed by a full one into a scratch store, and any row that differs is
reported (stats["incremental"]["differs"], and on stderr).

Systems are clustered again only in the modules whose rows changed; clustering is per module, so the rest are as a
full run would make them. Patterns and the orientation tour are always made again from the whole store.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import marshal
import os
import pickle
import re
import sqlite3
import sys
import uuid
import zlib
from array import array
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import store

CACHE_VERSION = "1"
POSITIONS = ("loc", "body_line")   # node attributes that only say where it is, as its span does
_TOKEN = re.compile(r"[A-Za-z_$][\w$]*")


# -- what a file mentions -----------------------------------------------------------------------------------
def mentions(res, decls) -> bytes:
    """Every string in a file's parse output, and every identifier inside those strings, as sorted crc32 values.
    Read field by field from whatever the adapter produced, so a new field on a record is covered without a change
    here. Collisions only make a file look affected when it is not."""
    strings: set = set()
    stack = [res, decls]
    while stack:
        o = stack.pop()
        if o is None or isinstance(o, (int, float, bool)):
            continue
        if isinstance(o, str):
            strings.add(o)
        elif isinstance(o, dict):
            stack.extend(o.keys())
            stack.extend(o.values())
        elif isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
        else:
            for slot in getattr(type(o), "__slots__", ()):
                stack.append(getattr(o, slot, None))
    toks = set()
    for s in strings:
        toks.add(zlib.crc32(s.encode()))
        for m in _TOKEN.findall(s):
            toks.add(zlib.crc32(m.encode()))
    return array("I", sorted(toks)).tobytes()


def _crc(obj) -> int:
    # marshal version 0 writes equal values as equal bytes (no references, no interning), so the hash is stable
    return zlib.crc32(marshal.dumps(obj, 0))


def code_version() -> str:
    """Changes whenever the leyline source does: cached parse and resolve output is only valid for the code that
    made it."""
    h = hashlib.sha1(f"{CACHE_VERSION}|{sys.version}".encode())
    root = Path(__file__).parent
    for p in sorted(root.rglob("*")):
        if p.suffix in (".py", ".sql") and "__pycache__" not in p.parts:
            st = p.stat()
            h.update(f"{p.relative_to(root)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def cache_path(db_path) -> Path:
    p = Path(db_path)
    return p.with_name(p.stem + ".cache" + (p.suffix or ".db"))


# -- the cache file -----------------------------------------------------------------------------------------
class _Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path))
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB);
            CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, sha TEXT, module TEXT, loc INTEGER, blob BLOB,
                                              err TEXT, toks BLOB);
            CREATE TABLE IF NOT EXISTS resolved (id TEXT PRIMARY KEY, outside BLOB, out BLOB);""")

    def get(self, key: str):
        row = self.con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None


class _Noting(set):
    """outside_names, noting what each file adds while its turn runs."""
    into: Optional[set] = None

    def add(self, x) -> None:
        set.add(self, x)
        if self.into is not None:
            self.into.add(x)


class _NotingDict(dict):
    """call_col, noting what each file sets while its turn runs."""
    into: Optional[list] = None

    def __setitem__(self, k, v) -> None:
        dict.__setitem__(self, k, v)
        if self.into is not None:
            self.into.append((k, v))


def _ancestry(nodes: dict) -> dict:
    """node id -> (file id, module id), as store.rebuild_derived works them out."""
    out = {}
    for i, n in nodes.items():
        file_id = module_id = None
        cur, seen = n, 0
        while cur is not None and seen < 64:
            if cur.kind == "file" and file_id is None:
                file_id = cur.id
            if cur.kind == "module" and module_id is None:
                module_id = cur.id
            cur = nodes.get(cur.parent_id) if cur.parent_id else None
            seen += 1
        out[i] = (file_id, module_id)
    return out


# -- a run ----------------------------------------------------------------------------------------------------
class Run:
    """Attach to an Indexer before Indexer.run: decides between a full and an incremental run, and supplies the
    hooks the indexer calls (parse_cache, _calls_pass, _flow_select, and _write for an incremental run)."""

    def __init__(self, con, db_path, ix, full: bool = False):
        self.ix, self.con = ix, con
        self.cache = _Cache(cache_path(db_path))
        self.key = json.dumps([code_version(), sorted((r, str(p)) for r, p in ix.repos.items()),
                               ix.exact_mode, sorted(ix.scip_paths)])
        gen = con.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
        self.prior = None
        if not full and gen is not None and self.cache.get("key") == self.key and self.cache.get("generation") == gen[0]:
            blob = self.cache.get("state")
            self.prior = pickle.loads(blob) if blob else None
        self.full = self.prior is None
        self.why_full = None if not self.full else ("asked" if full else "no usable cache from an earlier run")
        with con:   # until finish() writes a new token, the store matches no cache: a run that fails half way is not built on
            con.execute("DELETE FROM meta WHERE key = 'generation'")
        self.cache.con.execute("BEGIN")
        self.rows: dict = {}
        if not self.full:
            for r in self.cache.con.execute("SELECT id, sha, module, loc, blob, err, toks FROM files"):
                self.rows[r[0]] = r
        self.toks: dict[str, bytes] = {}
        self.seen: set = set()
        self.reparsed: set = set()
        self.resolved_rows: dict[str, tuple] = {}
        self.resolve_all = self.full
        self.flows_all = self.full
        self.affected: Optional[set] = None
        self.dirty_modules: dict[str, set] = {}
        self.systems_changed = False
        self.report: dict = {}
        ix.parse_cache = self
        ix._calls_pass = self._calls_pass
        ix._flow_select = self._flow_select
        if self.full:
            write = ix._write
            ix._write = lambda c: (write(c), self._after_full_write())
        else:
            ix._write = self._write_patch

    # -- parse cache ---------------------------------------------------------------------------------------
    def lookup(self, repo: str, root: Path, work: list) -> dict:
        """path -> the parse output kept for it, for the files whose content and module are unchanged."""
        if self.full:
            return {}
        out = {}
        for f, ext, mod_dir, mod_id in work:
            row = self.rows.get(f"{repo}:file:{f}")
            if row is None:
                continue
            data = (root / f).read_bytes()
            sha = hashlib.sha1(data).hexdigest()
            if row[1] == sha and row[2] == mod_dir:
                out[f] = (f, ext, mod_dir, mod_id, row[3], sha, row[4], row[5], row[6])
        return out

    def keep(self, file_id: str, got: tuple, fresh: bool) -> None:
        f, ext, mod_dir, mod_id, loc, sha, blob, failed, toks = got
        self.seen.add(file_id)
        self.toks[file_id] = toks
        if fresh:
            self.reparsed.add(file_id)
            self.cache.con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                                   (file_id, sha, mod_dir, loc, blob, failed, toks))

    # -- what changed --------------------------------------------------------------------------------------
    def _markers(self) -> list:
        """The directories that are modules, and the content of every project file a resolver reads."""
        from .indexer import _module_dirs
        out = []
        for repo, files in sorted(self.ix.files_of.items()):
            root = self.ix.repos[repo]
            marks = []
            for f in files:
                base = f.rsplit("/", 1)[-1]
                if base in ("package.json", "pyproject.toml", "setup.py") or base.endswith(".csproj"):
                    try:
                        marks.append((f, hashlib.sha1((root / f).read_bytes()).hexdigest()))
                    except OSError:
                        marks.append((f, None))
            out.append((repo, sorted(_module_dirs(files)), marks))
        return out

    def _shapes(self) -> dict:
        """node id -> (name, kind, hash of what a resolver can read of it, the text hash of a type). Where a node
        sits (its span, and the attributes that only repeat a line number) is left out: an edit above a node moves
        it without changing what it declares."""
        out = {}
        for i, n in self.ix.nodes.items():
            attrs = n.attrs
            if attrs and any(k in attrs for k in POSITIONS):
                attrs = {k: v for k, v in attrs.items() if k not in POSITIONS}
            out[i] = (n.name, n.kind, _crc((n.kind, n.name, n.parent_id, n.language, n.path, attrs)),
                      n.content_hash if n.kind == "type" else None)
        return out

    def _imports(self) -> dict:
        """file id -> (hash of how its imports resolved, the files it can see through them)."""
        ix, out = self.ix, {}
        g = lambda name: getattr(ix, name, {}).get
        py, targets, star = g("py_names"), g("import_targets"), g("star_exports")
        usings, static, alias, outside = g("cs_usings"), g("cs_static"), g("cs_alias"), g("outside_imports")
        for fid in ix.results:
            names = py(fid) or {}
            state = (sorted(names.items(), key=lambda kv: kv[0]), sorted(targets(fid) or ()), list(star(fid) or ()),
                     sorted(usings(fid) or ()), list(static(fid) or ()), sorted((alias(fid) or {}).items()),
                     sorted(outside(fid) or ()))
            seen = set(targets(fid) or ()) | {t for t, _ in names.values() if t} | set(star(fid) or ())
            out[fid] = (_crc(repr(state)), tuple(sorted(seen)))
        return out

    def _mentioning(self, names: set) -> set:
        crcs = {zlib.crc32(n.encode()) for n in names if isinstance(n, str)}
        if not crcs:
            return set()
        return {fid for fid in self.ix.results if self.toks.get(fid) and not crcs.isdisjoint(array("I", self.toks[fid]))}

    def _plan(self) -> None:
        """Work out which files' calls to resolve again. Runs when _resolve_calls starts, after the passes it
        reads from (indexes, imports, types, overrides, fixtures)."""
        ix = self.ix
        self.markers = self._markers()
        self.shapes = self._shapes()
        self.imports = self._imports()
        self.bases = {k: tuple(v) for k, v in ix.bases.items() if v}
        self.param_types = dict(getattr(ix, "py_param_type", {}))
        self.removed = {f for f in self.rows if f not in self.seen} if not self.full else set()
        if self.full:
            return
        p = self.prior
        if self.markers != p["markers"]:
            self.resolve_all = self.flows_all = True
            self.report["everything_because"] = "a project file or the module layout changed"
        old_shapes = p["shapes"]
        for t in set(self.bases) | set(p["bases"]):
            if t in self.shapes and t in old_shapes and self.bases.get(t) != p["bases"].get(t):
                self.resolve_all = True
                self.report["everything_because"] = f"the base types of {t} changed"
                break
        if self.resolve_all:
            return
        tokens: set = set()
        for i, s in self.shapes.items():
            o = old_shapes.get(i)
            if o == s:
                continue
            if o is not None and o[:3] == s[:3]:
                tokens.add(i)                  # only a type's text changed: what is declared inside it
            else:
                tokens.update((i, s[0]) if o is None else (i, s[0], o[0]))
        for i, o in old_shapes.items():
            if i not in self.shapes:
                tokens.update((i, o[0]))
        for k in set(self.param_types) | set(p["param_types"]):
            if self.param_types.get(k) != p["param_types"].get(k):
                tokens.add(k[0])
        old_imports = p["imports"]
        moved = {f for f in set(self.imports) | set(old_imports)
                 if self.imports.get(f, (None,))[0] != old_imports.get(f, (None,))[0]}
        rev = defaultdict(set)
        for f, (_, seen) in itertools.chain(old_imports.items(), self.imports.items()):
            for t in seen:
                rev[t].add(f)
        reach, todo = set(moved), list(moved)
        while todo:
            for f in rev.get(todo.pop(), ()):
                if f not in reach:
                    reach.add(f)
                    todo.append(f)
        self.tokens = tokens
        self.import_moved = moved
        self.affected = ((self.reparsed | reach | self._mentioning(tokens)) & set(ix.results))

    # -- the hooks the indexer calls ------------------------------------------------------------------------
    def _calls_pass(self, phase: int):
        if phase == 1:
            self._plan()
            if self.resolve_all:
                self.affected = None
            return self._pass_outside()
        return self._pass_calls()

    def _old(self, fid: str):
        if not hasattr(self, "_old_rows"):
            self._old_rows = {r[0]: (r[1], r[2]) for r in self.cache.con.execute("SELECT id, outside, out FROM resolved")} \
                if not self.full else {}
        return self._old_rows.get(fid)

    # Each file's turn in each pass of _resolve_calls is recorded as what it added: names seen on outside types,
    # calls, call columns, edges and counts. A file that is not resolved again gets its recorded turn replayed.
    def _mark(self):
        ix = self.ix
        ix.outside_names.into, ix.call_col.into = set(), []
        return len(ix.calls), len(ix.edges), {k: dict(v) for k, v in ix.stats.items()}

    def _since(self, mark) -> bytes:
        ix = self.ix
        n_calls, n_edges, before = mark
        delta = {}
        for k, v in ix.stats.items():
            was = before.get(k, {})
            d = {c: n - was.get(c, 0) for c, n in v.items() if n != was.get(c, 0)}
            if d:
                delta[k] = d
        got = (ix.outside_names.into, ix.calls[n_calls:], ix.call_col.into, ix.edges[n_edges:], delta)
        ix.outside_names.into = ix.call_col.into = None
        return pickle.dumps(got)   # now: the edges' attrs are changed later, when the run writes

    def _replay(self, turn: tuple) -> None:
        ix = self.ix
        names, calls, cols, edges, delta = turn
        set.update(ix.outside_names, names)
        ix.calls.extend(calls)
        for k, v in cols:
            dict.__setitem__(ix.call_col, k, v)
        ix.edges.extend(edges)
        for k, d in delta.items():
            ix.stats[k].update(d)

    def _pass_outside(self):
        """_resolve_calls' first pass: the names seen on outside types (working out a receiver's type here can add
        edges too). Its outcome decides which files the second pass resolves again."""
        ix = self.ix
        ix.outside_names, ix.call_col = _Noting(ix.outside_names), _NotingDict(ix.call_col)
        self.first: dict[str, bytes] = {}
        todo = self.affected
        order = list(ix.results.items())

        def turns(which):
            for fid, res in order:
                if which(fid):
                    mark = self._mark()
                    yield fid, res
                    self.first[fid] = self._since(mark)
        yield from turns(lambda f: todo is None or f in todo or self._old(f) is None)
        if todo is not None:
            todo |= set(self.first)
            kept = {fid: pickle.loads(self._old(fid)[0]) for fid, _ in order if fid not in self.first}
            names = set(ix.outside_names).union(*(t[0] for t in kept.values()))
            diff = frozenset(names) ^ self.prior["outside"]
            if diff:
                # a name's standing changed: the files that call it are resolved again, both passes
                todo |= self._mentioning({n for _, n in diff})
                yield from turns(lambda f: f in todo and f not in self.first)
            for fid, turn in kept.items():
                if fid not in self.first:
                    self._replay(turn)
                    self.first[fid] = self._old(fid)[0]
            self.report["files_resolved"] = len(todo)
        self.outside = frozenset(ix.outside_names)

    def _pass_calls(self):
        ix = self.ix
        todo = self.affected
        for fid, res in list(ix.results.items()):
            if todo is None or fid in todo:
                mark = self._mark()
                yield fid, res
                self.resolved_rows[fid] = (self.first[fid], self._since(mark))
            else:
                self._replay(pickle.loads(self._old(fid)[1]))

    def _flow_select(self, out: dict, starts: list) -> Optional[set]:
        """Hash each node's outgoing steps; walk again the flows that visit a node whose steps changed."""
        ix = self.ix
        nodes = ix.nodes
        self.flow_hashes = {n: _crc([(line, dst, via, sub, dst in nodes) for line, dst, via, sub in lst])
                            for n, lst in out.items()}
        self.starts = {}
        for start, kind, detail in starts:
            if start in nodes:
                n = nodes[start]
                name = n.name if n.kind == "test" else start.split(":", 2)[-1].split("::")[-1]
                self.starts.setdefault(start, []).append((name, kind, detail))
        if self.flows_all:
            return None
        old_hash = self.prior["flow_hashes"]
        changed = {n for n, h in self.flow_hashes.items() if old_hash.get(n) != h}
        changed |= {n for n in old_hash if n not in self.flow_hashes}
        old_flows = {}
        for r in self.con.execute("SELECT id, name, entry_id, attrs FROM flows WHERE layer = 'fact'"):
            if r[2] and r[2].split(":", 1)[0] in ix.repos:
                a = json.loads(r[3] or "{}")
                old_flows[r[0]] = (r[1], a.get("kind"), a.get("detail"))
        dirty = {s for s, metas in self.starts.items() if old_flows.get("flow:" + s) != metas[-1]}
        ids = list(changed)
        for k in range(0, len(ids), 900):
            chunk = ids[k:k + 900]
            for r in self.con.execute(
                    "SELECT DISTINCT fk.id FROM steps s JOIN keys fk ON fk.k = s.flow WHERE s.callable IN"
                    f" (SELECT k FROM keys WHERE id IN ({','.join('?' * len(chunk))}))", chunk):
                if r[0][5:] in self.starts:
                    dirty.add(r[0][5:])
        # A C# program's dispatch steps depend on what its module can see, which a C# file's usings can change.
        touched = self.reparsed | self.removed | getattr(self, "import_moved", set())
        if any(f.split(":file:", 1)[-1].endswith(".cs") for f in touched):
            dirty |= {s for s in self.starts if ix.file_lang.get(ix.file_of.get(s)) == "csharp"}
        self.flows_drop = {f for f in old_flows if f[5:] not in self.starts} | {"flow:" + s for s in dirty}
        self.report["flows_walked"] = len(dirty)
        return dirty

    # -- writing --------------------------------------------------------------------------------------------
    def _groups(self) -> tuple[dict, dict, dict]:
        """The rows a run writes, grouped by the file each row's node (for an edge or call, its source) sits in,
        or by the node itself when it is in no file; and a hash of each group."""
        ix = self.ix
        anc = self.anc = _ancestry(ix.nodes)
        group = {i: (a[0] or i) for i, a in anc.items()}
        rows: dict[str, list] = defaultdict(lambda: ([], [], []))
        for i, n in ix.nodes.items():
            rows[group[i]][0].append((i, n.kind, n.name, n.parent_id, ix._owner(i), n.language, n.path, n.span_start,
                                      n.span_end, n.content_hash, n.attrs or None))
        edges, calls = ix._final()
        for e in edges:
            rows[group[e.src_id]][1].append((e.kind, e.src_id, e.dst_id, e.precision, e.attrs or None))
        for c in calls:
            rows[group[c[0]]][2].append(c)
        # Edges are hashed in a sorted order: the same edges can come out of a run in another order (one found
        # while resolving a receiver in the first pass of a full run is found in the second pass of a later
        # one), and rewriting them would change nothing.
        hashes, self.pattern_groups = {}, {}
        for g, r in rows.items():
            edges_ = sorted(marshal.dumps(e, 0) for e in r[1])
            hashes[g] = hashlib.blake2b(marshal.dumps((r[0], r[2], edges_), 0), digest_size=8).digest()
            # what the pattern matchers read of the group besides its nodes: its edges and which functions call which
            self.pattern_groups[g] = hashlib.blake2b(marshal.dumps((edges_, sorted({c[:2] for c in r[2]})), 0),
                                                     digest_size=8).digest()
        return rows, hashes, group

    def _after_full_write(self) -> None:
        self.rows_by_group, self.groups, _ = self._groups()
        self.flow_sizes = self._sizes_of(self.ix.flows, self.ix.flow_steps, {})
        self.rows_by_group = None

    @staticmethod
    def _sizes_of(flows, steps, sizes: dict) -> dict:
        """start -> the step count of each flow walked from it, as Indexer.run counts them for the coverage row."""
        starts = [f for f, _ in steps.flows]
        ends = [s for _, s in steps.flows[1:]] + [len(steps)]
        out: dict = {}
        for fid, (_, s), e in zip(starts, steps.flows, ends):
            out.setdefault(fid[5:], []).append(e - s)
        sizes = {k: v for k, v in sizes.items() if k not in out}
        sizes.update(out)
        return sizes

    def _write_patch(self, con) -> None:
        ix = self.ix
        rows, hashes, group = self._groups()
        old = self.prior["groups"]
        changed = sorted(g for g in set(hashes) | set(old) if hashes.get(g) != old.get(g))
        self.groups = hashes
        self.report["groups_written"] = len(changed)
        dirty: dict[str, set] = defaultdict(set)
        with con:
            con.execute("CREATE TEMP TABLE IF NOT EXISTS gone (id TEXT PRIMARY KEY)")
            con.execute("DELETE FROM gone")
            for k in range(0, len(changed), 900):
                chunk = changed[k:k + 900]
                marks = ",".join("?" * len(chunk))
                for r in con.execute(
                        f"SELECT a.node_id, a.module_id FROM ancestry a JOIN nodes n ON n.id = a.node_id"
                        f" WHERE n.layer = 'fact' AND (a.file_id IN ({marks}) OR (a.file_id IS NULL AND a.node_id IN ({marks})))",
                        chunk + chunk):
                    con.execute("INSERT OR IGNORE INTO gone VALUES (?)", (r[0],))
                    if r[1]:
                        dirty[r[1].split(":", 1)[0]].add(r[1])
            gone = "SELECT k FROM keys WHERE id IN (SELECT id FROM gone)"
            con.execute(f"DELETE FROM call_sites WHERE src IN ({gone})")
            con.execute(f"DELETE FROM links WHERE layer = 'fact' AND src IN ({gone})")
            con.execute("DELETE FROM ancestry WHERE node_id IN (SELECT id FROM gone)")
            con.execute("DELETE FROM search WHERE node_id IN (SELECT id FROM gone)")
            con.execute("DELETE FROM nodes WHERE layer = 'fact' AND id IN (SELECT id FROM gone)")
            # The commit each row was read at: if HEAD moved, every row of that repository says so, as after a full run.
            for repo, commit in ix.commits.items():
                if self.prior["commits"].get(repo, commit) != commit:
                    con.execute("UPDATE nodes SET commit_sha = ? WHERE repo_id = ? AND layer = 'fact'", (commit, repo))
                    mine = "SELECT k FROM keys WHERE id IN (SELECT id FROM nodes WHERE repo_id = ? AND layer = 'fact')"
                    con.execute(f"UPDATE links SET commit_sha = ? WHERE layer = 'fact' AND src IN ({mine})", (commit, repo))
                    con.execute(f"UPDATE call_sites SET commit_sha = ? WHERE src IN ({mine})", (commit, repo))
            new_nodes, new_edges, new_calls = [], [], []
            for g in changed:
                if g in rows:
                    n, e, c = rows[g]
                    new_nodes += n
                    new_edges += e
                    new_calls += c
            for repo in ix.repos:
                commit = ix.commits[repo]
                store.write_nodes(con, [ix.nodes[r[0]] for r in new_nodes if r[4] == repo], repo, ix_source(), commit)
                con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"root:{repo}", str(ix.repos[repo])))
                store.insert_edges(con, [(None, k, s, d, p, "fact", ix_source(), commit, json.dumps(a) if a else None)
                                         for k, s, d, p, a in new_edges if ix._owner(s) == repo])
                store.write_calls(con, [c for c in new_calls if ix._owner(c[0]) == repo], commit)
            anc = self.anc
            con.executemany("INSERT INTO ancestry VALUES (?,?,?)", [(r[0], *anc[r[0]]) for r in new_nodes])
            con.executemany("INSERT INTO search (node_id, name, qualified, path, kind) VALUES (?,?,?,?,?)",
                            [(r[0], r[2], store._searchable(r[0]), r[6] or "", r[1]) for r in new_nodes if r[1] != "repo"])
            for r in new_nodes:
                m = anc[r[0]][1]
                if m:
                    dirty[r[4]].add(m)
            for g in changed:   # a module node is a group of its own; its contains edges list its files
                if g in ix.nodes and ix.nodes[g].kind == "module":
                    dirty[ix._owner(g)].add(g)
            # Flows: the ones walked again replace their old rows; the ones whose start is gone go.
            if self.flows_all:
                drop = [r[0] for r in con.execute("SELECT id, entry_id FROM flows WHERE layer = 'fact'")
                        if r[1] and r[1].split(":", 1)[0] in ix.repos]
            else:
                drop = sorted(self.flows_drop)
            con.execute("DELETE FROM gone")
            con.executemany("INSERT OR IGNORE INTO gone VALUES (?)", [(f,) for f in drop])
            con.execute("DELETE FROM steps WHERE flow IN (SELECT k FROM keys WHERE id IN (SELECT id FROM gone))")
            con.execute("DELETE FROM flows WHERE layer = 'fact' AND id IN (SELECT id FROM gone)")
            store.write_flows(con, ix.repo, ix.flows, ix.flow_steps, reindex=False)
            self.flow_sizes = self._sizes_of(ix.flows, ix.flow_steps, {} if self.flows_all else self.prior["flow_sizes"])
            self.flow_sizes = {k: v for k, v in self.flow_sizes.items() if k in self.starts}
            ix._write_coverage(con, sum(len(v) for v in self.flow_sizes.values()),
                               sum(sum(v) for v in self.flow_sizes.values()))
        self.dirty_modules = dict(dirty)
        self.report["modules_reclustered"] = sum(len(v) for v in dirty.values())

    # -- after the run --------------------------------------------------------------------------------------
    def systems(self, con, repo: str) -> dict:
        """Cluster again the modules whose rows changed (every module after a full run)."""
        from . import cluster
        if self.full:
            return cluster.propose(con, repo)
        mods = self.dirty_modules.get(repo, set())

        def made():
            rows = {r[0]: r[2] for r in con.execute("SELECT id, parent_id, attrs FROM nodes WHERE kind = 'system' AND repo_id = ?",
                                                    (repo,)) if r[1] in mods}
            grouped = sorted(tuple(r) for r in con.execute(
                "SELECT src_id, dst_id FROM edges WHERE kind = 'groups' AND src_id IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(rows)),)))
            return rows, grouped
        before = made()
        out = cluster.propose(con, repo, modules=mods)
        after = made()
        self.systems_changed = self.systems_changed or before != after
        with con:
            store.derive_some(con, before[0], after[0])
        return out

    def patterns(self, con, repo: str) -> dict:
        """Find patterns again, unless nothing the matchers read changed: the nodes (where they sit aside), the
        edges, which functions call which, and the systems. Then the matches are the ones already stored, and only
        the hashes of the files behind them are brought up to date, as a fresh pass would write them."""
        from . import patterns
        old = None if self.full else self.prior["shapes"]
        same = (old is not None and len(old) == len(self.shapes)
                and all(old.get(i, ())[:3] == s[:3] for i, s in self.shapes.items())   # a type's text is not read
                and self.pattern_groups == self.prior.get("pattern_groups") and not self.systems_changed)
        self.report["patterns"] = "kept" if same else "found again"
        if not same:
            return patterns.run(con, repo)
        roles = defaultdict(list)
        for r in con.execute("SELECT instance_id, node_id FROM pattern_roles"):
            roles[r[0]].append(r[1])
        with con:
            for r in con.execute("SELECT id, matcher, evidence_hash FROM pattern_instances").fetchall():
                h = store.evidence_hash(con, roles.get(r[0], []))
                if r[1] == patterns.MATCHER:
                    con.execute("UPDATE pattern_instances SET evidence_hash = ? WHERE id = ?", (h, r[0]))
                else:
                    con.execute("UPDATE pattern_instances SET stale = ? WHERE id = ?", (int(h != r[2]), r[0]))
        row = con.execute("SELECT stats FROM extractor_coverage WHERE repo_id = ? AND extractor = 'patterns:structural'",
                          (repo,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def finish(self, con) -> None:
        """Save what the next run needs, under a token the store also keeps: a store written by anything else (an
        older leyline, a run that failed) no longer matches the cache, and the next run is a full one."""
        if not hasattr(self, "shapes"):   # nothing was resolved (an empty repository): nothing to keep
            self.cache.con.rollback()
            return
        token = uuid.uuid4().hex
        state = {"shapes": self.shapes, "imports": self.imports, "bases": self.bases, "param_types": self.param_types,
                 "outside": self.outside, "markers": self.markers, "flow_hashes": self.flow_hashes,
                 "flow_sizes": self.flow_sizes, "groups": self.groups, "pattern_groups": self.pattern_groups,
                 "commits": self.commits}
        c = self.cache.con
        if self.full:
            c.execute("DELETE FROM resolved")
            c.execute("DELETE FROM files WHERE id NOT IN (SELECT value FROM json_each(?))", (json.dumps(sorted(self.seen)),))
        else:
            c.executemany("DELETE FROM files WHERE id = ?", [(f,) for f in self.removed])
            c.executemany("DELETE FROM resolved WHERE id = ?", [(f,) for f in self.removed])
        c.executemany("INSERT OR REPLACE INTO resolved VALUES (?,?,?)", [(f, *r) for f, r in self.resolved_rows.items()])
        c.execute("INSERT OR REPLACE INTO meta VALUES ('key', ?)", (self.key,))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('state', ?)", (pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL),))
        with con:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('generation', ?)", (token,))
        c.execute("INSERT OR REPLACE INTO meta VALUES ('generation', ?)", (token,))
        c.commit()
        c.close()

    def release(self) -> None:
        """Let go of the indexer and of the cached blobs once the run has written the store. The hooks are taken
        off the indexer too: they refer back to it, and the cycle would keep its memory until a garbage collection."""
        ix = self.ix
        self.commits = dict(ix.commits)
        for name in ("_calls_pass", "_flow_select", "_write"):
            ix.__dict__.pop(name, None)
        ix.parse_cache = None
        self.ix = None
        self.rows = self._old_rows = self.anc = None

    def abandon(self) -> None:
        try:
            self.cache.con.rollback()
            self.cache.con.close()
        except sqlite3.Error:
            pass

    def summary(self) -> dict:
        out = {"mode": "full" if self.full else "incremental", "files_parsed": len(self.reparsed),
               "files_removed": len(self.removed) if hasattr(self, "removed") else 0}
        if self.full:
            out["why"] = self.why_full
        out.update(self.report)
        return out


# The rows an index writes, as two stores are compared: an incremental run must leave them as a full run would.
COMPARED = {
    "nodes": "SELECT id, kind, name, parent_id, repo_id, language, path, span_start, span_end, content_hash, layer, attrs"
             " FROM nodes",
    "calls": "SELECT src_id, dst_id, dispatch, precision, site_start, site_end FROM calls",
    "edges": "SELECT kind, src_id, dst_id, precision, layer, attrs FROM edges",
    "flows": "SELECT id, name, entry_id, attrs FROM flows",
    "flow_steps": "SELECT flow_id, seq, depth, callable_id, via, site_line, parent_seq FROM flow_steps",
    "patterns": "SELECT id, pattern, rationale, confidence, evidence_hash, stale, attrs FROM pattern_instances",
    "pattern_roles": "SELECT instance_id, role, node_id FROM pattern_roles",
    "tour_stops": "SELECT tour_id, seq, ref_kind, ref_id, narrative, title FROM tour_stops",
    "search": "SELECT node_id, name, qualified, path, kind FROM search",
    "ancestry": "SELECT node_id, file_id, module_id FROM ancestry",
}


def differences(a, b, tables: Optional[dict] = None) -> dict:
    """table -> (rows in a, rows in b, the first rows that differ), for the tables an index writes; empty when the
    two stores agree. Both sides are read sorted by SQLite and compared as they stream, so two stores of a large
    repository are compared without holding either."""
    out = {}
    ca, cb = sqlite3.connect(str(a)), sqlite3.connect(str(b))
    try:
        for t, sql in (tables or COMPARED).items():
            n = len(ca.execute(sql + " LIMIT 0").description)
            order = f" ORDER BY {', '.join(str(i) for i in range(1, n + 1))}"
            ra, rb = ca.execute(sql + order), cb.execute(sql + order)
            na = nb = 0
            first = []
            for x, y in itertools.zip_longest(ra, rb):
                na += x is not None
                nb += y is not None
                if x != y and len(first) < 3:
                    first.append((x, y))
            if first:
                out[t] = (na, nb, first)
    finally:
        ca.close()
        cb.close()
    return out


def verify(db_path, members: list, exact: str, scip) -> dict:
    """LEYLINE_VERIFY=1: index the same tree in full into a scratch store and compare it with the store an
    incremental run just wrote. Slow (it is a full run); for checking this module against changes to the resolvers."""
    import tempfile
    from .indexer import index
    with tempfile.TemporaryDirectory() as d:
        other = Path(d) / "full.db"
        if len(members) == 1:
            index(members[0][0], other, members[0][1], exact, scip, full=True, _verify=False)
        else:
            index([r for r, _ in members], other, None, exact, scip, full=True, _verify=False)
        diff = differences(db_path, other)
    if diff:
        print(f"leyline: the incremental index differs from a full one: {diff}", file=sys.stderr)
    return diff


def ix_source() -> str:
    from .indexer import SOURCE
    return SOURCE
