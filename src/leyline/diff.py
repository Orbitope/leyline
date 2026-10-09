"""Compare two stores of the same repo: what was edited, what links changed, and what that did
to the module and system structure. Also check a finished change against its proposal."""

from __future__ import annotations

import array
import datetime
import difflib
import json
import re
import hashlib
import sqlite3
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import change, rules

EDGE_KINDS = ("extends", "implements", "uses_type", "instantiates", "imports", "communicates", "overrides", "depends_on",
              "reads", "writes")
CODE_KINDS = ("type", "callable", "field", "test")


def store_path(con) -> Path:
    return Path(next(r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"))


def snapshot_path(con, name: str) -> Path:
    return store_path(con).parent / "snapshots" / f"{name}.db"


# A snapshot keeps only what a later comparison reads: the nodes with their content hashes, calls and edges by
# their two ends, which file and module each node sits in, the steps of each flow, and a hash of each line of each
# source file (so an edit can be placed inside one function and not another). Ids are stored once, in `keys`, and
# the views give the column names the store has, so compare() and the rules read a snapshot as they read a store.
# It also keeps what a sequence diagram of the code as it was reads (diagrams.sequence): the line of each pair's
# first call site and whether the map guessed it, each step's place in its flow (seq, parent_seq, depth, how it
# was reached and from which line), and the channel, address and line of each channel link. A snapshot taken
# before these were kept still compares; it just cannot be drawn (diagrams.drawable).
_SNAPSHOT_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE keys (k INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE);
CREATE TABLE node_rows (node INTEGER PRIMARY KEY, kind TEXT, name TEXT, parent INTEGER, repo_id TEXT, path TEXT,
                        span_start INTEGER, span_end INTEGER, content_hash TEXT, layer TEXT);
CREATE TABLE node_files (node INTEGER PRIMARY KEY, file INTEGER, module INTEGER);
CREATE TABLE call_pairs (src INTEGER, dst INTEGER, line INTEGER, guess INTEGER, PRIMARY KEY (src, dst)) WITHOUT ROWID;
CREATE TABLE edge_pairs (kind TEXT, src INTEGER, dst INTEGER, guess INTEGER, PRIMARY KEY (kind, src, dst)) WITHOUT ROWID;
CREATE TABLE channel_rows (src INTEGER, dst INTEGER, guess INTEGER, attrs TEXT);
CREATE TABLE flow_rows (flow INTEGER PRIMARY KEY, name TEXT, entry INTEGER, kind TEXT);
CREATE TABLE steps (flow INTEGER, seq INTEGER, depth INTEGER, callable INTEGER, via INTEGER, site_line INTEGER,
                    parent_seq INTEGER, PRIMARY KEY (flow, seq)) WITHOUT ROWID;
CREATE TABLE source_lines (repo_id TEXT, path TEXT, hashes BLOB, PRIMARY KEY (repo_id, path));
CREATE TABLE annotations (node_id TEXT, key TEXT, value TEXT);
CREATE TABLE rules (id INTEGER PRIMARY KEY, kind TEXT, selector_from TEXT, selector_to TEXT, edge_kinds TEXT,
                    severity TEXT, reason TEXT, status TEXT, source TEXT, created TEXT);
CREATE VIEW nodes AS SELECT n.id, r.kind, r.name, p.id AS parent_id, r.repo_id, r.path, r.span_start, r.span_end,
    r.content_hash, r.layer, NULL AS attrs FROM node_rows r JOIN keys n ON n.k = r.node LEFT JOIN keys p ON p.k = r.parent;
CREATE VIEW flows AS SELECT f.id, r.name, e.id AS entry_id, json_object('kind', r.kind) AS attrs
    FROM flow_rows r JOIN keys f ON f.k = r.flow LEFT JOIN keys e ON e.k = r.entry;
CREATE VIEW ancestry AS SELECT n.id AS node_id, f.id AS file_id, m.id AS module_id
    FROM node_files x JOIN keys n ON n.k = x.node LEFT JOIN keys f ON f.k = x.file LEFT JOIN keys m ON m.k = x.module;
CREATE VIEW calls AS SELECT s.id AS src_id, d.id AS dst_id, c.line AS site_start,
    CASE WHEN c.guess THEN 'guess' END AS precision FROM call_pairs c JOIN keys s ON s.k = c.src JOIN keys d ON d.k = c.dst;
CREATE VIEW edges AS SELECT e.kind, s.id AS src_id, d.id AS dst_id, CASE WHEN e.guess THEN 'guess' END AS precision,
    NULL AS attrs FROM edge_pairs e JOIN keys s ON s.k = e.src JOIN keys d ON d.k = e.dst WHERE e.kind != 'communicates'
    UNION ALL SELECT 'communicates', s.id, d.id, CASE WHEN c.guess THEN 'guess' END, c.attrs
    FROM channel_rows c JOIN keys s ON s.k = c.src JOIN keys d ON d.k = c.dst;
CREATE VIEW flow_steps AS SELECT f.id AS flow_id, s.seq, s.depth, c.id AS callable_id, __VIA__ AS via, s.site_line,
    s.parent_seq FROM steps s JOIN keys f ON f.k = s.flow JOIN keys c ON c.k = s.callable;
"""


def _snapshot_schema() -> str:
    from . import store
    return _SNAPSHOT_SCHEMA.replace("__VIA__", store._VIA_CASE)


# What a channel link's attrs keep in a snapshot: what a diagram names it by, and the line it sits on.
_CHANNEL_KEYS = ("channel", "address", "launched_at", "line")


def line_hashes(data: bytes) -> list[int]:
    """One number per line, ignoring the indentation and trailing space the content hashes also ignore."""
    return [zlib.crc32(ln.strip()) for ln in data.split(b"\n")]


def _pack(hashes: list[int]) -> bytes:
    return zlib.compress(array.array("I", hashes).tobytes(), 6)


def _unpack(blob: bytes) -> list[int]:
    a = array.array("I")
    a.frombytes(zlib.decompress(blob))
    return a.tolist()


def roots(con) -> dict[str, Path]:
    from . import store
    return store.roots(con)


def source(con, repo: str, path: str, root_of: Optional[dict] = None) -> Optional[bytes]:
    """A file's bytes as they are on disk now, if they are what the store indexed."""
    root = (root_of if root_of is not None else roots(con)).get(repo)
    if root is None:
        return None
    try:
        return (root / path).read_bytes()
    except OSError:
        return None


def snapshot(con, name: str) -> Path:
    """Keep the parts of the store a later check compares with. Returns the snapshot's path."""
    target = snapshot_path(con, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    for p in (tmp, Path(str(tmp) + "-journal")):
        if p.exists():
            p.unlink()
    out = sqlite3.connect(str(tmp))
    out.executescript(_snapshot_schema())
    out.close()
    con.commit()
    con.execute("ATTACH DATABASE ? AS snap", (str(tmp),))
    try:
        with con:
            con.execute("INSERT INTO snap.keys SELECT k, id FROM main.keys")
            con.execute("INSERT OR IGNORE INTO snap.keys (id) SELECT id FROM main.nodes")
            con.execute("INSERT OR IGNORE INTO snap.keys (id) SELECT id FROM main.flows UNION SELECT entry_id FROM main.flows"
                        " WHERE entry_id IS NOT NULL")
            con.execute("INSERT OR IGNORE INTO snap.keys (id) SELECT file_id FROM main.ancestry WHERE file_id IS NOT NULL"
                        " UNION SELECT module_id FROM main.ancestry WHERE module_id IS NOT NULL")
            con.execute("INSERT INTO snap.node_rows SELECT n.k, x.kind, x.name, p.k, x.repo_id, x.path, x.span_start,"
                        " x.span_end, x.content_hash, x.layer FROM main.nodes x JOIN snap.keys n ON n.id = x.id"
                        " LEFT JOIN snap.keys p ON p.id = x.parent_id")
            con.execute("INSERT INTO snap.node_files SELECT n.k, f.k, m.k FROM main.ancestry a JOIN snap.keys n ON n.id = a.node_id"
                        " LEFT JOIN snap.keys f ON f.id = a.file_id LEFT JOIN snap.keys m ON m.id = a.module_id")
            # One row per pair: where its first call sits in the caller, and whether every call of it is a guess.
            con.execute("INSERT INTO snap.call_pairs SELECT src, dst, MIN(site_start), MIN(precision = 'guess')"
                        " FROM main.call_sites GROUP BY src, dst")
            kinds = EDGE_KINDS + ("groups",)
            con.execute(f"INSERT INTO snap.edge_pairs SELECT kind, src, dst, MIN(precision = 'guess') FROM main.links"
                        f" WHERE kind IN ({','.join('?' * len(kinds))}) GROUP BY kind, src, dst", kinds)
            chans = set()
            for s, d, prec, raw in con.execute("SELECT src, dst, precision, attrs FROM main.links WHERE kind = 'communicates'"):
                try:
                    a = json.loads(raw) if raw else {}
                except ValueError:
                    a = {}
                a = {k: a[k] for k in _CHANNEL_KEYS if a.get(k) is not None} if isinstance(a, dict) else {}
                chans.add((s, d, int(prec == "guess"), json.dumps(a, sort_keys=True)))
            con.executemany("INSERT INTO snap.channel_rows VALUES (?,?,?,?)", sorted(chans))
            con.execute("INSERT INTO snap.flow_rows SELECT f.k, x.name, e.k, json_extract(x.attrs, '$.kind')"
                        " FROM main.flows x JOIN snap.keys f ON f.id = x.id LEFT JOIN snap.keys e ON e.id = x.entry_id")
            con.execute("INSERT OR IGNORE INTO snap.steps SELECT flow, seq, depth, callable, via, site_line, parent_seq"
                        " FROM main.steps")
            # the names given to systems, which a rule's `system:Name` selects by
            con.execute("INSERT INTO snap.annotations SELECT node_id, key, value FROM main.annotations WHERE key = 'name'")
            con.execute("INSERT INTO snap.meta SELECT key, value FROM main.meta WHERE key LIKE 'root:%'")
            con.execute("INSERT INTO snap.meta VALUES ('format', 'slim')")
            where = {}
            for r in con.execute("SELECT repo_id, path, content_hash FROM main.nodes WHERE kind = 'file' AND layer = 'fact'"):
                where[(r[0], r[1])] = r[2]
            root_of = roots(con)
            rows = []
            for (repo, path), sha in where.items():
                data = source(con, repo, path, root_of)
                if data is not None and hashlib.sha1(data).hexdigest() == sha:   # only text the store describes
                    rows.append((repo, path, _pack(line_hashes(data))))
            con.executemany("INSERT INTO snap.source_lines VALUES (?,?,?)", rows)
    finally:
        con.execute("DETACH DATABASE snap")
    for p in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
        if p.exists():
            p.unlink()
    tmp.replace(target)
    return target


def drop_snapshot(con, name: str) -> bool:
    """Forget a change's baseline. True when there was one."""
    target = snapshot_path(con, name)
    gone = False
    for p in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
        if p.exists():
            p.unlink()
            gone = True
    return gone


def _fingerprint(con) -> str:
    h = hashlib.sha1()
    for r in con.execute(f"SELECT id, content_hash FROM nodes WHERE layer = 'fact' AND kind IN ({','.join('?' * len(CODE_KINDS))})"
                         " ORDER BY id", CODE_KINDS):
        h.update(f"{r[0]}\0{r[1]}\n".encode())
    return h.hexdigest()


def moved_on(con, name: str) -> bool:
    """True when a snapshot of this name exists and the code has changed since it was taken."""
    snap = snapshot_path(con, name)
    if not snap.exists():
        return False
    before = _open(snap)
    try:
        return _fingerprint(before) != _fingerprint(con)
    finally:
        before.close()


def _open(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)   # quoted: `#`, `?`, `%` in a folder's name
    con.row_factory = sqlite3.Row
    return con


def _base(qualified_id: str) -> str:
    # The id without its parameter list: a method keeps this when only its signature changes.
    head, sep, _ = qualified_id.rpartition("(")
    return head if sep and qualified_id.endswith(")") else qualified_id


def _changed_lines(old: list[int], new: list[int], gone: Optional[set] = None) -> set[int]:
    """The lines of the new text (from 1) that are new or edited, plus the line where any line was deleted. A
    deletion of nothing but code that is gone from the map (`gone`, old line numbers from 1: a removed function)
    and blank lines marks no line: the removal is said as a removal, not as an edit of what was around it."""
    out = set()
    blank = zlib.crc32(b"")
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if op in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
        elif op == "delete":
            if gone and all(k + 1 in gone or old[k] == blank for k in range(i1, i2)) and any(k + 1 in gone for k in range(i1, i2)):
                continue
            out.add(max(1, j1))
    return out


def own_changes(before: sqlite3.Connection, after: sqlite3.Connection, ids: list[str]) -> Optional[dict]:
    """For each node, the changed lines inside it that are not inside a node nested in it, as (line, text). A node
    whose content hash changed only because something nested in it changed has none. None when the baseline
    keeps no line hashes (taken by an older Leyline)."""
    try:
        before.execute("SELECT 1 FROM source_lines LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    rows = {}
    for i in ids:
        r = after.execute("SELECT id, kind, repo_id, path, span_start, span_end FROM nodes WHERE id = ?", (i,)).fetchone()
        if r is not None:
            rows[i] = r
    root_of = roots(after)
    files: dict = {}
    out = {}
    for i in ids:
        n = rows.get(i)
        if n is None or not n["path"]:
            continue
        key = (n["repo_id"], n["path"])
        if key not in files:
            data = source(after, *key, root_of=root_of)
            old = before.execute("SELECT hashes FROM source_lines WHERE repo_id = ? AND path = ?", key).fetchone()
            if data is None:
                files[key] = None
            else:
                text = data.decode("utf-8", errors="replace").split("\n")
                gone = set()     # the old lines of code the map no longer has: removed functions, types, fields
                try:
                    for j, a, b in before.execute(
                            f"SELECT id, span_start, span_end FROM nodes WHERE repo_id = ? AND path = ? AND span_start IS NOT NULL"
                            f" AND kind IN ({','.join('?' * len(CODE_KINDS))})", (*key, *CODE_KINDS)).fetchall():
                        if after.execute("SELECT 1 FROM nodes WHERE id = ?", (j,)).fetchone() is None:
                            gone.update(range(a, (b or a) + 1))
                except sqlite3.Error:
                    gone = set()
                changed = _changed_lines(_unpack(old[0]) if old else [], line_hashes(data), gone)
                inner = [(r[0], r[1], r[2]) for r in after.execute(
                    f"SELECT id, span_start, span_end FROM nodes WHERE repo_id = ? AND path = ? AND span_start IS NOT NULL"
                    f" AND kind IN ({','.join('?' * len(CODE_KINDS))})", (*key, *CODE_KINDS))]
                files[key] = (text, changed, inner)
        if files[key] is None:
            continue
        text, changed, inner = files[key]
        s, e = (1, len(text)) if n["kind"] == "file" or not n["span_start"] else (n["span_start"], n["span_end"] or n["span_start"])
        nested = [(a, b) for j, a, b in inner if j != i and s <= a and b <= e and (a, b) != (s, e)]
        out[i] = [(ln, text[ln - 1] if ln <= len(text) else "") for ln in sorted(changed)
                  if s <= ln <= e and not any(a <= ln <= b for a, b in nested)]
    return out


IMPORT = re.compile(r"^\s*(import\s|from\s+\S+\s+import\s|using\s|#include\s|use\s|require\(|const\s.*=\s*require\(|export\s.*\sfrom\s)")


def explained(node: dict, spans: list[tuple[int, int]], names: set[str], reach: int = 2) -> bool:
    """Whether the changed lines a body holds around code a task covers (a module's top level, a class around a new
    method) are all part of that work: blank, an import, next to (within `reach` lines of) that code, or in a run
    of changed lines that names it (an entry in a registration table, up to 15 lines long). A function
    that holds no task code is never explained this way: its own lines are its own edit."""
    own = node.get("own") or []
    top = node.get("name") in ("<module>", "<top-level>")
    start, end = node.get("line") or 0, node.get("end") or 0
    if not top and not any(start <= a and b <= end for a, b in spans):
        return False

    def names_code(text):
        return any(re.search(r"(?<![A-Za-z0-9_$])" + re.escape(n) + r"(?![A-Za-z0-9_$])", text) for n in names if n)
    runs, cur = [], []
    for ln, text in own:      # runs of changed lines next to each other
        if cur and ln > cur[-1][0] + 1:
            runs.append(cur)
            cur = []
        cur.append((ln, text))
    runs += [cur] if cur else []
    for run in runs:
        if len(run) <= 15 and any(names_code(text) for _, text in run):
            continue
        for ln, text in run:
            if not text.strip() or (spans and IMPORT.match(text)) or any(a - reach <= ln <= b + reach for a, b in spans):
                continue
            return False
    return True


def compare(before: sqlite3.Connection, after: sqlite3.Connection) -> dict:
    def nodes(con):
        return {r["id"]: r for r in con.execute(
            f"SELECT id, kind, name, parent_id, path, span_start, span_end, content_hash, attrs FROM nodes"
            f" WHERE layer = 'fact' AND kind IN ({','.join('?' * len(CODE_KINDS))})", CODE_KINDS)}
    b, a = nodes(before), nodes(after)
    removed, added = set(b) - set(a), set(a) - set(b)
    # A method whose parameters changed has a new id. Pair it with the old one by owner and name.
    resigned: dict[str, str] = {}
    by_base = defaultdict(list)
    for i in added:
        by_base[(_base(i), a[i]["kind"])].append(i)
    for i in sorted(removed):
        cands = by_base.get((_base(i), b[i]["kind"]), [])
        if b[i]["kind"] == "callable" and len(cands) == 1 and cands[0] not in resigned.values():
            resigned[i] = cands[0]
    removed -= set(resigned)
    added -= set(resigned.values())
    # Anything nested under a re-signed method moved with it.
    moved = {}
    for old, new in resigned.items():
        for i in list(removed):
            if i.startswith(old + "/") and (new + i[len(old):]) in added:
                moved[i] = new + i[len(old):]
    for old, new in moved.items():
        removed.discard(old)
        added.discard(new)
    remap = {**resigned, **moved}
    edited = {i for i in set(a) & set(b) if a[i]["content_hash"] != b[i]["content_hash"] and a[i]["kind"] != "type"}
    edited |= {new for old, new in moved.items() if a[new]["content_hash"] != b[old]["content_hash"]}
    types_edited = {i for i in set(a) & set(b) if a[i]["kind"] == "type" and a[i]["content_hash"] != b[i]["content_hash"]}

    def links(con, mapping):
        out = set()
        for r in con.execute("SELECT DISTINCT src_id, dst_id FROM calls"):
            out.add(("calls", mapping.get(r[0], r[0]), mapping.get(r[1], r[1])))
        for r in con.execute(f"SELECT kind, src_id, dst_id FROM edges WHERE kind IN ({','.join('?' * len(EDGE_KINDS))})", EDGE_KINDS):
            out.add((r[0], mapping.get(r[1], r[1]), mapping.get(r[2], r[2])))
        return out
    lb, la = links(before, remap), links(after, {})
    links_added, links_removed = la - lb, lb - la

    def rollup(con, link_set):
        mod = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
        for r in con.execute("SELECT id FROM nodes WHERE kind IN ('module', 'external')"):
            mod[r[0]] = r[0]
        out = defaultdict(int)
        for k, s, d in link_set:
            ms, md = mod.get(s), mod.get(d)
            if ms and md and ms != md:
                out[(ms, md)] += 1
        return out
    names_a = {r["id"]: r["name"] for r in after.execute("SELECT id, name FROM nodes")}
    names_b = {r["id"]: r["name"] for r in before.execute("SELECT id, name FROM nodes")}
    rb, ra = rollup(before, links(before, {})), rollup(after, la)
    new_deps = [{"from": names_a.get(s, s), "to": names_a.get(d, d), "links": n, "from_id": s, "to_id": d}
                for (s, d), n in sorted(ra.items()) if (s, d) not in rb]
    gone_deps = [{"from": names_b.get(s, s), "to": names_b.get(d, d), "links": n}
                 for (s, d), n in sorted(rb.items()) if (s, d) not in ra]

    def flows(con, mapping):
        out = {}
        for f in con.execute("SELECT id, name, entry_id FROM flows"):
            steps = [mapping.get(r[0], r[0]) for r in con.execute(
                "SELECT callable_id FROM flow_steps WHERE flow_id = ? ORDER BY seq", (f["id"],))]
            out[mapping.get(f["entry_id"], f["entry_id"])] = (f["name"], steps)
        return out
    fb, fa = flows(before, remap), flows(after, {})
    flow_changes = []
    flow_gained, flow_lost = defaultdict(int), defaultdict(int)
    for key in sorted(set(fb) | set(fa)):
        if key not in fb:
            flow_changes.append({"name": fa[key][0], "change": "new", "entry": key})
        elif key not in fa:
            flow_changes.append({"name": fb[key][0], "change": "gone"})
        else:
            gained, lost = set(fa[key][1]) - set(fb[key][1]), set(fb[key][1]) - set(fa[key][1])
            for i in gained:
                flow_gained[i] += 1
            for i in lost:
                flow_lost[i] += 1
            if gained or lost:
                flow_changes.append({"name": fa[key][0], "change": "path changed", "entry": key,
                                     "gained": len(gained), "lost": len(lost)})

    def label(con_nodes, i):
        n = con_nodes[i]
        parent = con_nodes.get(n["parent_id"])
        name = f"{parent['name']}.{n['name']}" if parent is not None and parent["kind"] == "type" else n["name"]
        return {"id": i, "name": name,
                "kind": n["kind"], "path": n["path"], "line": n["span_start"], "end": n["span_end"]}
    return {
        "nodes": {
            "added": [label(a, i) for i in sorted(added)],
            "removed": [label(b, i) for i in sorted(removed)],
            "resigned": [{**label(a, new), "was": old.rsplit("(", 1)[-1].rstrip(")"),
                          "now": new.rsplit("(", 1)[-1].rstrip(")")} for old, new in sorted(resigned.items())],
            "edited": [label(a, i) for i in sorted(edited)],
            "types_edited": [label(a, i) for i in sorted(types_edited)],
        },
        "links": {"added": len(links_added), "removed": len(links_removed),
                  "added_examples": [{"kind": k, "from": names_a.get(s, s), "to": names_a.get(d, d), "from_id": s, "to_id": d}
                                     for k, s, d in sorted(links_added)[:40]],
                  "removed_examples": [{"kind": k, "from": names_b.get(s, names_a.get(s, s)), "to": names_b.get(d, names_a.get(d, d))}
                                       for k, s, d in sorted(links_removed)[:40]]},
        "structure": {"new_dependencies": new_deps, "removed_dependencies": gone_deps},
        "flows": {"changed": len(flow_changes), "items": flow_changes[:60],
                  "now_pass_through": [{**label(a, i), "flows": n} for i, n in sorted(flow_gained.items(), key=lambda x: -x[1])[:15] if i in a],
                  "no_longer_pass_through": [{"id": i, "name": names_b.get(i, names_a.get(i, i)), "flows": n}
                                             for i, n in sorted(flow_lost.items(), key=lambda x: -x[1])[:15]]},
        "remap": remap,
    }


_SOURCE_FILE = re.compile(r"\.(?:[cm]?[jt]sx?|py|cs|go|rb|java|kt|rs|php|swift|scala|lua|gd|dart|ex|exs)$")


def result_parts(name: str) -> dict:
    """A recorded result's name taken apart: the file, the suites around the test, the test's own name, and for a
    parametrized pytest test its function and parameter id. Names come as `file > suite > test` (TAP, vitest),
    `path.py::Class::test[param]` (pytest), or just the test's name."""
    out = {"file": None, "suites": [], "leaf": name, "func": None, "param": None}
    if "::" in name:
        parts = name.split("::")
        out["file"], out["suites"], out["leaf"] = parts[0], parts[1:-1], parts[-1]
    elif " > " in name:
        segs = name.split(" > ")
        # A file has a path or a source file's extension; a suite named for what it tests (`Engine.start`) is not one.
        if len(segs) > 1 and ("/" in segs[0] or "\\" in segs[0] or _SOURCE_FILE.search(segs[0])):
            out["file"], segs = segs[0], segs[1:]
        out["suites"], out["leaf"] = segs[:-1], segs[-1]
    m = re.match(r"^(.+?)\[(.*)\]$", out["leaf"])
    if m and out["file"] and out["file"].endswith(".py"):
        out["func"], out["param"] = m.group(1), m.group(2)
    return out


def _template(name: str):
    """A test named with an interpolated string ("{tag}: reproduces ...") matches any text in its holes. A name that
    is nearly all holes ("{}") would match every test, so it matches none."""
    literal = re.sub(r"\{[^}]*\}", "", name)
    if len(re.sub(r"[^A-Za-z0-9]", "", literal)) < 3:
        return None
    return re.compile("^" + "(.+?)".join(re.escape(part) for part in re.split(r"\{[^}]*\}", name)) + "$")


class TestNames:
    """The test nodes on the map, to tie a recorded result to the one test it ran. A result names its file and
    suites when the runner printed them, and only a test in that file and suite can be its node."""

    def __init__(self, con):
        self.by_name = defaultdict(list)
        self.templates = []
        self.parametrized = []    # pytest tests run once per parameter: test_x[a], test_x[b]
        for r in con.execute("SELECT id, name, path, attrs FROM nodes WHERE kind = 'test'"
                             " OR json_extract(attrs, '$.is_test') = 1"):
            a = json.loads(r["attrs"] or "{}")
            t = {"id": r["id"], "name": r["name"], "path": r["path"] or "", "suite": a.get("suite") or "",
                 "full": a.get("full_name") or ""}
            self.by_name[r["name"]].append(t)
            if any("parametrize" in d for d in a.get("decorators") or []):
                self.parametrized.append(t)
            if "{" in r["name"]:
                pattern = _template(r["name"])
                if pattern is not None:
                    self.templates.append((pattern, len(re.sub(r"\{[^}]*\}", "", r["name"])), t))

    @staticmethod
    def _narrow(cands: list, p: dict) -> list:
        if p["file"]:
            f = p["file"].replace("\\", "/").lstrip("./")
            cands = [t for t in cands if t["path"] == f or t["path"].endswith("/" + f) or f.endswith("/" + t["path"])]
        if p["suites"] and cands:
            chain = " > ".join(p["suites"])
            same = [t for t in cands if (t["full"] and t["full"].rsplit(" > ", 1)[0] in (chain, " > ".join(p["suites"][-1:])))
                    or (t["suite"] and t["suite"] in (chain, p["suites"][-1]))
                    or ("." + p["suites"][-1] + "." in t["id"])]
            if same or not p["file"]:
                cands = same
        return cands

    def node(self, name: str) -> Optional[str]:
        """The id of the test node a result ran, or None when there is none or it could be more than one."""
        p = result_parts(name)
        leaf = p["func"] or p["leaf"]
        cands = self._narrow(self.by_name.get(leaf, []), p)
        if not cands:
            hits = [(size, t) for pattern, size, t in self.templates if pattern.match(leaf)]
            hits = [(size, t) for size, t in hits if t in self._narrow([t for _, t in hits], p)]
            if hits:
                best = max(size for size, _ in hits)
                cands = [t for size, t in hits if size == best]
        ids = {t["id"] for t in cands}
        return ids.pop() if len(ids) == 1 else None


def record_tests(con, run: str, results: list[dict]) -> dict:
    """Store one test run. Each result is {name, status: pass|fail|skip, message?}."""
    names = TestNames(con)
    from . import props   # results passed whole (MCP `test_results`) may carry a property test's counterexample
    results = props.annotate([dict(r) for r in results])
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    rows = [(run, r["name"], names.node(r["name"]), r["status"], r.get("message"), now) for r in results]
    with con:
        con.execute("DELETE FROM test_results WHERE run = ?", (run,))
        con.executemany("INSERT OR REPLACE INTO test_results VALUES (?,?,?,?,?,?)", rows)
    counts = defaultdict(int)
    for r in results:
        counts[r["status"]] += 1
    return {"run": run, **counts, "matched_to_test_nodes": sum(1 for r in rows if r[2])}


_PLAIN = re.compile(r"^\s*(PASS(?:ED)?|FAIL(?:ED)?|ERROR|SKIP(?:PED)?|XFAIL|XPASS)\b[\s:]*(.+?)\s*$")
_POINT = re.compile(r"^(\s*)(not ok|ok)\b(?:\s+\d+)?(?:\s*-(?=\s|$))?\s*(.*?)\s*$")
_SUBTEST = re.compile(r"^(\s*)# Subtest:\s*(.*?)\s*$")


def _tap_point(rest: str) -> tuple[str, str, bool]:
    """A TAP test line after `ok N -`: (description, directive, opens a block of subtests)."""
    opens = rest.endswith("{")
    if opens:
        rest = rest[:-1].rstrip()
    m = re.search(r"(?<!\\)\s#\s*(.*)$", rest)
    desc = rest[:m.start()] if m else rest
    directive = m.group(1).strip() if m else ""
    return desc.strip().replace("\\#", "#").replace("\\\\", "\\"), directive, opens


_COLLECTED = re.compile(r"^(\S+\.py) - (.+)$")


def _outside_brackets(text: str, sep: str) -> int:
    """Where `sep` first appears outside a pytest parameter's brackets (`test[a - b] - message`), or -1."""
    depth = 0
    for k, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif not depth and text.startswith(sep, k):
            return k
    return -1


def parse_test_output(text: str) -> list[dict]:
    """Read test results from runner output. Understood: TAP 13 and 14 as vitest, node:test and tap print it, where
    nested subtests (indented, or opened with `{`) give names like `file > suite > test` and a suite is not a test of
    its own; pytest -rA summaries (`PASSED path::test[param]`), kept whole so each parameter is one result; and any
    runner that prints one `PASS name` or `FAIL name: message` line per test. Other formats: pass results to
    record_tests directly."""
    out: dict[str, dict] = {}
    stack: list[dict] = []        # open suites: {indent, name, kind: header|brace, status, children, failed}
    last: Optional[dict] = None   # the result a YAML block that follows belongs to
    in_yaml = False

    def emit(chain: list[str], status: str, message: Optional[str] = None) -> dict:
        for f in stack:
            f["children"] += 1
            f["failed"] |= status == "fail"
        name = " > ".join(x for x in chain if x)
        if name in out and out[name]["status"] == "fail":
            return out[name]      # the same name twice: a failure is not hidden by a pass
        out[name] = {"name": name, "status": status, "message": message}
        return out[name]

    def close(frame: dict) -> Optional[dict]:
        # A suite passes or fails with its tests, which are recorded already. Only a suite that failed with no
        # failing test inside it (a hook, an import) is a failure of its own.
        if frame["children"]:
            if frame["status"] == "fail" and not frame["failed"]:
                return emit([f["name"] for f in stack] + [frame["name"]], "fail")
            return None
        return emit([f["name"] for f in stack] + [frame["name"]], frame["status"])

    for line in text.splitlines():
        if in_yaml:
            if re.match(r"^\s*\.\.\.\s*$", line):
                in_yaml = False
            else:
                # vitest nests `message:` under `error:`; node:test prints the message as `error:` itself
                m = re.match(r"^\s*(?:message|error):\s*(?![|>][-+]?\s*$)(\S.*)$", line)
                if m and last is not None and last["status"] == "fail" and not last["message"]:
                    msg = m.group(1).strip()
                    if len(msg) > 1 and msg[0] == msg[-1] == '"':
                        try:
                            msg = json.loads(msg)    # YAML's double-quoted string escapes as JSON does
                        except ValueError:
                            msg = msg[1:-1]
                    elif len(msg) > 1 and msg[0] == msg[-1] == "'":
                        msg = msg[1:-1].replace("''", "'")
                    last["message"] = msg
            continue
        if re.match(r"^\s*---\s*$", line):
            in_yaml = True
            continue
        m = _SUBTEST.match(line)
        if m:
            indent = len(m.group(1).expandtabs())
            while stack and stack[-1]["kind"] == "header" and stack[-1]["indent"] >= indent:
                stack.pop()
            name = m.group(2).replace("\\#", "#").replace("\\\\", "\\")   # escaped as on a test line
            stack.append({"indent": indent, "name": name, "kind": "header", "status": "pass", "children": 0, "failed": False})
            continue
        if re.match(r"^\s*\}\s*$", line) and stack and stack[-1]["kind"] == "brace":
            frame = stack.pop()
            last = close(frame) or last
            continue
        m = _POINT.match(line)
        if m:
            indent = len(m.group(1).expandtabs())
            desc, directive, opens = _tap_point(m.group(3))
            word = directive.split()[0].upper() if directive else ""
            status = "skip" if word in ("SKIP", "TODO") else "fail" if m.group(2) == "not ok" else "pass"
            while stack and stack[-1]["kind"] == "header" and stack[-1]["indent"] > indent:
                stack.pop()       # a subtest that never printed its own line
            if stack and stack[-1]["kind"] == "header" and stack[-1]["indent"] == indent:
                frame = stack.pop()   # node:test and tap: the line after the subtests closes the suite
                frame.update(status=status, name=desc or frame["name"])
                last = close(frame)
            elif opens:            # vitest: `ok 1 - suite {` ... `}`
                stack.append({"indent": indent, "name": desc, "kind": "brace", "status": status, "children": 0, "failed": False})
                last = None
            else:
                last = emit([f["name"] for f in stack] + [desc], status)
            continue
        m = _PLAIN.match(line)
        if not m or m.group(2).startswith("["):   # pytest's `SKIPPED [1] file:line: reason` names no test
            continue
        word, rest = m.group(1).upper(), m.group(2)
        status = ("fail" if word.startswith(("FAIL", "ERROR")) else "skip" if word.startswith(("SKIP", "XFAIL"))
                  else "pass")
        name, message = rest, None
        if "::" in rest:          # pytest: `path::test[a - b] - message`, and XFAIL and XPASS give a reason the same way
            for sep in (" - ", ": "):
                k = _outside_brackets(rest, sep)
                if k >= 0:
                    name, message = rest[:k], (rest[k + len(sep):] if status == "fail" else None)
                    break
        elif status == "fail" and _COLLECTED.match(rest):   # pytest: `ERROR tests/x.py - ImportError: ...`, a file that did not load
            name, message = _COLLECTED.match(rest).groups()
        elif status == "fail":
            for sep in (": ", " - "):
                if sep in rest:
                    name, message = rest.split(sep, 1)
                    break
        last = emit([name.strip()], status, message)
    from . import props   # a failing property test's counterexample, from wherever in the text it was printed
    return props.annotate(list(out.values()), text)


def _n(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def review_text(r: dict) -> str:
    p, g = r["prediction"], r["graph"]["nodes"]
    lines = [f"Review of {r['change_id']}: {r['intent']}", "",
             f"Prediction: {p['as_predicted']} of {p['predicted']} predicted edits happened, "
             f"{_n(p['not_predicted'], 'edit was', 'edits were')} not predicted, {_n(p['predicted_untouched'], 'predicted edit', 'predicted edits')} did not happen. "
             f"{_n(p['new_as_declared'], 'new node belongs', 'new nodes belong')} to what the proposal declared."]
    for n in r["not_predicted"]:
        lines.append(f"  not predicted: {n['name']}  {n['path']}:{n['line']}")
    for n in r["predicted_untouched"]:
        lines.append(f"  not edited:    {n['name']}")
    lines.append(f"Graph: {len(g['added'])} added, {len(g['removed'])} removed, {len(g['resigned'])} signatures changed, "
                 f"{len(g['edited'])} bodies edited; {r['graph']['links']['added']} links added, {r['graph']['links']['removed']} removed.")
    for d in r["graph"]["structure"]["new_dependencies"]:
        lines.append(f"  new dependency: {d['from']} -> {d['to']} ({_n(d['links'], 'link')})")
    lines.append(f"Flows with a changed path: {r['graph']['flows']['changed']}")
    for x in r["graph"]["flows"]["now_pass_through"][:5]:
        lines.append(f"  {_n(x['flows'], 'flow now passes', 'flows now pass')} through {x['name']}")
    for x in r["graph"]["flows"]["no_longer_pass_through"][:5]:
        lines.append(f"  {_n(x['flows'], 'flow no longer passes', 'flows no longer pass')} through {x['name']}")
    lines.append(f"Rules: {r['rules']['checked']} checked, {r['rules']['failing']} failing, {len(r['rules']['new_violations'])} newly failing.")
    t = r["tests"]
    if t:
        lines.append(f"Tests: {t['before']['passed']}/{t['before']['total']} before, {t['after']['passed']}/{t['after']['total']} after; "
                     f"{len(t['newly_failing'])} newly failing.")
        lines += [f"  now fails: {x['name']}: {x['message']}" for x in t["newly_failing"]]
    for v in r["verdict"]:
        lines.append(f"[{v['level']}] {v['what']}")
    if not r["verdict"]:
        lines.append("Nothing flagged.")
    return "\n".join(lines)


def passed_text(side: dict) -> str:
    """`3854 of 3854 passed (1 skipped)`: a skipped test neither passed nor failed, so it is said apart."""
    skipped = side.get("skipped", 0)
    return f"{side['passed']} of {side['total'] - skipped} passed" + (f" ({skipped} skipped)" if skipped else "")


def passed_pair_text(before: dict, after: dict, now_first: bool = False) -> str:
    """`3854 of 3854 passed before, 3866 of 3866 after (1 skipped in each run)`; `now_first`: `7 of 7 pass, against
    7 of 7 before`. A skipped test neither passed nor failed, so it is said apart."""
    sb, sa = before.get("skipped", 0), after.get("skipped", 0)
    out = (f"{after['passed']} of {after['total'] - sa} pass, against {before['passed']} of {before['total'] - sb} before"
           if now_first else
           f"{before['passed']} of {before['total'] - sb} passed before, {after['passed']} of {after['total'] - sa} after")
    if sb or sa:
        out += (f" ({sb} skipped in each run)" if sb == sa else f" (skipped: {sb} before, {sa} after)")
    return out


def recorded_text(t: dict) -> str:
    """`3854 pass, 0 fail, 1 skipped`: the counts of a recorded run."""
    return f"{t.get('pass', 0)} pass, {t.get('fail', 0)} fail" + (f", {t['skip']} skipped" if t.get("skip") else "")


def test_delta(con, before_run: str, after_run: str) -> Optional[dict]:
    def load(run):
        return {r["name"]: r for r in con.execute("SELECT * FROM test_results WHERE run = ?", (run,))}
    b, a = load(before_run), load(after_run)
    if not b or not a:
        return None
    return {
        "before": {"run": before_run, "passed": sum(1 for r in b.values() if r["status"] == "pass"), "total": len(b),
                   "skipped": sum(1 for r in b.values() if r["status"] == "skip")},
        "after": {"run": after_run, "passed": sum(1 for r in a.values() if r["status"] == "pass"), "total": len(a),
                  "skipped": sum(1 for r in a.values() if r["status"] == "skip")},
        "newly_failing": [{"name": n, "message": a[n]["message"]} for n in sorted(a) if a[n]["status"] == "fail" and b.get(n) and b[n]["status"] == "pass"],
        "newly_passing": [n for n in sorted(a) if a[n]["status"] == "pass" and b.get(n) and b[n]["status"] == "fail"],
        # A test that was not there before and fails now: as wrong as one that broke.
        "new_failing": [{"name": n, "message": a[n]["message"]} for n in sorted(a) if a[n]["status"] == "fail" and n not in b],
        "added": [n for n in sorted(a) if n not in b], "removed": [n for n in sorted(b) if n not in a],
    }


def _spans(con, ids: list[str]) -> tuple[dict, set]:
    """path -> the line spans of these nodes, and their names: the code a task covers, for explained()."""
    spans, names = defaultdict(list), set()
    for i in dict.fromkeys(ids):
        r = con.execute("SELECT name, path, span_start, span_end FROM nodes WHERE id = ?", (i,)).fetchone()
        if r is None:
            continue
        if r["span_start"] and r["name"] not in ("<module>", "<top-level>"):
            spans[r["path"]].append((r["span_start"], r["span_end"] or r["span_start"]))
        if re.fullmatch(r"[A-Za-z_$][\w$]{2,}", r["name"] or ""):
            names.add(r["name"])
    return spans, names


def review(con, change_id: str, before_run: Optional[str] = None, after_run: Optional[str] = None) -> dict:
    """After a change is implemented and the repo re-indexed: compare the graph with the snapshot
    taken when the change was proposed, and the edits made with the edits predicted."""
    row = con.execute("SELECT * FROM change_proposals WHERE id = ?", (change_id,)).fetchone()
    if row is None:
        return {"error": f"No change {change_id!r}."}
    snap = snapshot_path(con, change_id)
    if not snap.exists():
        return {"error": "No snapshot is kept for this change (it was forgotten, or its folder archived), so there is"
                         " nothing to compare with. Plan the change again to take a new one."}
    before = _open(snap)
    try:
        d = compare(before, con)
        # Today's rules, evaluated on the graph as it was: a rule added after the proposal still counts.
        rules_before = rules.check(before, rules_from=con)
        changed_ids = [n["id"] for key in ("resigned", "edited", "types_edited") for n in d["nodes"][key]]
        # A file's top level (imports, constants, tables, calls outside any function) has a node of its own only in
        # some languages, so the file stands for it.
        fb = {r[0]: r[1] for r in before.execute("SELECT id, content_hash FROM nodes WHERE kind = 'file' AND layer = 'fact'")}
        files = [r for r in con.execute("SELECT id, name, path, content_hash, span_end FROM nodes WHERE kind = 'file' AND layer = 'fact'")
                 if fb.get(r["id"]) != r["content_hash"]]
        own = own_changes(before, con, changed_ids + [f["id"] for f in files])
    finally:
        before.close()
    view = change.get_view(con, "view-" + change_id)
    remap = d.pop("remap")
    predicted = {remap.get(m["id"], m["id"]): m for m in view.get("marks", []) if m["role"] in ("changed", "must_edit", "contract")}
    declared_new = {n["name"].split("(")[0].split(".")[-1] for n in view.get("new_nodes", [])}

    def segments(i):
        return set(re.split(r"[.:/]+", _base(i)))
    touched = {n["id"]: n for key in ("resigned", "edited") for n in d["nodes"][key]}
    # A type's text changes whenever a member's does. Report a type only when no member explains it.
    for t in d["nodes"]["types_edited"]:
        if own is not None:
            if own.get(t["id"]):
                touched[t["id"]] = {**t, "why": "declaration or fields changed"}
            continue
        inside = [i for i in list(touched) + [n["id"] for n in d["nodes"]["added"]] if i.startswith(t["id"] + ".")]
        if not inside:
            touched[t["id"]] = {**t, "why": "declaration or fields changed"}
    for i, n in touched.items():
        if own is not None and i in own:
            n["own"] = own[i]
    as_predicted = [touched[i] for i in sorted(touched) if i in predicted]
    # A function whose text changed only inside a function nested in it (a closure, a module's body around its
    # functions) was not edited itself: the nested one was, and it is reported on its own.
    not_predicted = [touched[i] for i in sorted(touched) if i not in predicted and touched[i].get("own", True) != []
                     and not (own is not None and touched[i]["name"] in ("<module>", "<top-level>"))]
    if own is not None:
        not_predicted += [{"id": f["id"], "name": "<top-level>", "kind": "file", "path": f["path"], "line": 1,
                           "end": f["span_end"], "why": "outside any function", "own": own[f["id"]]}
                          for f in files if own.get(f["id"]) and any(text.strip() for _, text in own[f["id"]])]
    new_declared, new_undeclared = [], []
    for n in d["nodes"]["added"]:
        (new_declared if segments(n["id"]) & declared_new else new_undeclared).append(n)
    # Members of an undeclared new type are noise: keep the outermost new nodes only.
    outer = [n for n in new_undeclared if not any(n["id"].startswith(o["id"] + ".") for o in new_undeclared if o is not n)]
    not_predicted += [{**n, "why": "new, not declared in the proposal"} for n in outer]
    # The rest of a body (a module's top level) changed only next to the predicted code, or to register it.
    spans, names_of = _spans(con, list(predicted) + [n["id"] for n in new_declared])
    not_predicted = [n for n in not_predicted if not (n.get("own") and explained(n, spans.get(n.get("path"), []), names_of))]
    untouched = [{"id": i, "name": m.get("name", i), "note": m.get("note", "")} for i, m in sorted(predicted.items())
                 if i not in touched]
    gone = {n["id"] for n in d["nodes"]["removed"]} | {n["id"] for n in d["nodes"]["added"]}   # new since the snapshot: it changed
    untouched = [u for u in untouched if u["id"] not in gone]
    removed_predicted = [n for n in d["nodes"]["removed"] if n["id"] in predicted]
    not_predicted += [{**n, "why": "removed"} for n in d["nodes"]["removed"] if n["id"] not in predicted]
    as_predicted += removed_predicted
    now_rules = rules.check(con)
    was_failing = {r["id"] for r in rules_before.get("rules", []) if not r["passes"]}
    new_violations = [r for r in now_rules["rules"] if not r["passes"] and r["id"] not in was_failing]
    tests = test_delta(con, before_run, after_run) if before_run and after_run else None
    verdict = []
    if not_predicted:
        verdict.append({"level": "medium", "what": f"{_n(len(not_predicted), 'edit was', 'edits were')} not in the proposal."})
    if untouched:
        verdict.append({"level": "medium", "what": f"{_n(len(untouched), 'predicted edit', 'predicted edits')} did not happen."})
    if d["structure"]["new_dependencies"]:
        verdict.append({"level": "high", "what": f"{_n(len(d['structure']['new_dependencies']), 'new dependency', 'new dependencies')} between modules."})
    if new_violations:
        verdict.append({"level": "high", "what": f"{_n(len(new_violations), 'rule now fails', 'rules now fail')} that passed before."})
    if tests and tests["newly_failing"]:
        verdict.append({"level": "high", "what": f"{_n(len(tests['newly_failing']), 'test fails', 'tests fail')} that passed before."})
    if tests is None:
        verdict.append({"level": "medium", "what": "No test runs were recorded, so behavior is unchecked."})
    report = {
        "change_id": change_id, "intent": row["intent"],
        "prediction": {"predicted": len(predicted), "as_predicted": len(as_predicted),
                       "not_predicted": len(not_predicted), "predicted_untouched": len(untouched),
                       "new_as_declared": len(new_declared)},
        "as_predicted": as_predicted, "not_predicted": not_predicted, "predicted_untouched": untouched,
        "graph": d, "rules": {"checked": now_rules["total"], "failing": now_rules["failing"],
                              "new_violations": new_violations, "all": now_rules["rules"]},
        "tests": tests, "verdict": verdict,
        "limits": "The graph comparison shows structure, not behavior. Tests are the only behavior check here, "
                  "and only as far as they reach.",
    }
    head = con.execute("SELECT commit_sha FROM nodes WHERE kind = 'repo' LIMIT 1").fetchone()
    attrs = json.loads(row["attrs"] or "{}")
    lean = {**report, **{k: [{x: y for x, y in n.items() if x != "own"} for n in report[k]]   # changed lines stay out of the store
                         for k in ("as_predicted", "not_predicted")}}
    attrs["review"] = {k: v for k, v in lean.items() if k != "graph"} | {"graph_summary": {
        "added": len(d["nodes"]["added"]), "removed": len(d["nodes"]["removed"]), "resigned": len(d["nodes"]["resigned"]),
        "edited": len(d["nodes"]["edited"]), "links_added": d["links"]["added"], "links_removed": d["links"]["removed"]}}
    with con:
        con.execute("UPDATE change_proposals SET status = 'implemented', head_commit = ?, attrs = ? WHERE id = ?",
                    (head[0] if head else None, json.dumps(attrs), change_id))
    marks = ([{"id": n["id"], "role": "edited as predicted", "note": n.get("why", "") or n.get("now", "")} for n in as_predicted]
             + [{"id": n["id"], "role": "new, as declared", "note": n["kind"]} for n in new_declared if n["kind"] != "field"]
             + [{"id": n["id"], "role": "edited, not predicted", "note": n.get("why") or n["kind"]} for n in not_predicted if n["id"] not in gone]
             + [{"id": n["id"], "role": "predicted, not edited", "note": n["note"]} for n in untouched])
    title = "Review: " + (attrs.get("title") or row["intent"][:60])
    saved = change.save_view(
        con, title, row["intent"], marks or [{"id": m["id"], "role": "changed", "note": ""} for m in view.get("marks", [])[:1]],
        kind="review", source="leyline-review", change_id=change_id, view_id="view-review-" + change_id,
        legend={"edited as predicted": "edited, as the proposal said", "edited, not predicted": "edited or added, but not in the proposal",
                "new, as declared": "new code the proposal said it would add",
                "predicted, not edited": "in the proposal, but not edited"},
        extra={"review": {k: v for k, v in lean.items() if k not in ("as_predicted", "intent")}})
    report["view_id"] = saved.get("id")
    report["baseline"] = (row["base_commit"] or "")[:7] or None
    return report
