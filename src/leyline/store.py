"""The SQLite store. Everything else reads and writes through this module."""

from __future__ import annotations

import array
import hashlib
import itertools
import json
import re
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Iterable, Optional

from .model import Edge, Node

SCHEMA_VERSION = "0"


BUSY_SECONDS = 60


def _current(con) -> bool:
    """Whether the store is already in this version's shape: the three views in place and the version noted."""
    kinds = {r[0]: r[1] for r in con.execute("SELECT name, type FROM sqlite_master WHERE name IN ('flow_steps', 'calls', 'edges')")}
    if kinds != {"flow_steps": "view", "calls": "view", "edges": "view"}:
        return False
    row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return row is not None and row[0] == SCHEMA_VERSION


def write_file(path: str | Path, data: str | bytes) -> None:
    """Write a file Leyline makes inside a repository (a page, the store's folder, a skill) without following a link
    at its name. The repository may be someone else's, and a link committed where Leyline writes (`.leyline/map.html`
    pointing at ~/.bashrc) would otherwise have Leyline write over the file it points at. The text goes to a new
    file beside it, which then takes the name's place: a link there is replaced, not written through."""
    import os
    import tempfile
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb" if isinstance(data, bytes) else "w", **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
            f.write(data)
        os.chmod(tmp, 0o644)   # a temporary file is private; the file it becomes is not
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Another leyline run may be writing (a map while a plan starts): wait for it rather than fail at once.
    con = sqlite3.connect(str(db_path), timeout=BUSY_SECONDS)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=OFF")  # edges may point at nodes written later in a run
    con.executescript(resources.files("leyline").joinpath("schema.sql").read_text(encoding="utf-8"))
    have = {r[1] for r in con.execute("PRAGMA table_info(rules)")}
    for col in ("status", "source", "created"):
        if col not in have:
            con.execute(f"ALTER TABLE rules ADD COLUMN {col} TEXT")
    for table, cols in (("tours", ("repo_id", "created")), ("tour_stops", ("title",)), ("pattern_instances", ("attrs",))):
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    if _current(con):
        return con   # nothing to move: a reader takes no write lock, so it is not held up by a run that is writing
    # One transaction, committed here: moving an older store's rows drops the old table, and a caller that only
    # reads and never commits would otherwise roll back the moved rows and lose them. IMMEDIATE takes the write
    # lock first: a deferred one that read first could not wait for a writer and would fail at once.
    con.execute("BEGIN IMMEDIATE")
    try:
        _flow_steps_view(con)
        _calls_view(con)
        _edges_view(con)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return con


# Every callable on some test's path, as `SELECT DISTINCT callable_id FROM flow_steps` over the test flows
# would give it. Done on the keyed table: the flows are filtered first (reading the kind out of each flow's
# attrs once per step was most of the cost on a large repo), and only the distinct callables are turned
# back into ids.
TESTED = ("SELECT id AS callable_id FROM keys WHERE k IN (SELECT DISTINCT callable FROM steps WHERE flow IN"
          " (SELECT k FROM keys WHERE id IN (SELECT id FROM flows WHERE json_extract(attrs, '$.kind') = 'test')))")


def flow_callables(con, least: int, most: int) -> dict:
    """flow id -> the callable ids of its steps in order, for every flow of least..most steps. One pass over the
    keyed table: a query per flow cost seconds on a large repo, and through the view every step of every flow
    would be joined to its ids just to count them."""
    out: dict[str, list] = {}
    for f, c in con.execute(
            "SELECT fk.id, ck.id FROM steps s JOIN keys fk ON fk.k = s.flow JOIN keys ck ON ck.k = s.callable"
            " WHERE s.flow IN (SELECT flow FROM steps GROUP BY flow HAVING COUNT(*) BETWEEN ? AND ?)"
            " ORDER BY s.flow, s.seq", (least, most)):
        out.setdefault(f, []).append(c)
    return out


VIA = ("start", "calls", "runs", "dispatch", "event", "process", "http", "file", "channel", "di", "queue", "rpc", "db")
_VIA_CASE = "CASE s.via " + " ".join(f"WHEN {i} THEN '{v}'" for i, v in enumerate(VIA)) + " END"


def _flow_steps_view(con) -> None:
    """`flow_steps` as readers know it, over the integer tables. A store written before the change has a
    real `flow_steps` table: its rows are moved across once."""
    kind = con.execute("SELECT type FROM sqlite_master WHERE name = 'flow_steps'").fetchone()
    if kind is not None and kind[0] == "table":
        rows = con.execute("SELECT flow_id, seq, depth, callable_id, via, site_line, parent_seq FROM flow_steps").fetchall()
        con.execute("DROP TABLE flow_steps")
        _insert_steps(con, rows)
        kind = None
    if kind is None:
        con.execute(f"""CREATE VIEW flow_steps AS
            SELECT fk.id AS flow_id, s.seq, s.depth, ck.id AS callable_id, NULL AS edge_id, {_VIA_CASE} AS via,
                   s.site_line, s.parent_seq
            FROM steps s JOIN keys fk ON fk.k = s.flow JOIN keys ck ON ck.k = s.callable""")


def _calls_view(con) -> None:
    """`calls` as readers know it, over `call_sites`, which holds the two ends as keys. A store written before
    the change has a real `calls` table: its rows are moved across once. The old table's index happened to
    return whole-table groups sorted by callee, then caller; through the view a reader that needs that order
    asks for it."""
    kind = con.execute("SELECT type FROM sqlite_master WHERE name = 'calls'").fetchone()
    if kind is not None and kind[0] == "table":
        rows = con.execute("SELECT src_id, dst_id, dispatch, precision, site_start, site_end, hit_count, commit_sha"
                           " FROM calls ORDER BY rowid").fetchall()
        con.execute("DROP TABLE calls")
        _insert_calls(con, rows)
        kind = None
    if kind is None:
        con.execute("""CREATE VIEW calls AS
            SELECT sk.id AS src_id, dk.id AS dst_id, c.dispatch, c.precision, c.site_start, c.site_end, c.hit_count,
                   c.commit_sha
            FROM call_sites c JOIN keys sk ON sk.k = c.src JOIN keys dk ON dk.k = c.dst""")


EDGE_COLUMNS = "id, kind, src_id, dst_id, precision, layer, source, commit_sha, attrs"


def _edges_view(con) -> None:
    """`edges` as readers and writers know it, over `links`, which holds the two ends as keys. A store written
    before the change has a real `edges` table: its rows are moved across once, keeping their ids. Inserts
    into and deletes from the view go to `links` through triggers."""
    kind = con.execute("SELECT type FROM sqlite_master WHERE name = 'edges'").fetchone()
    if kind is not None and kind[0] == "table":
        rows = con.execute(f"SELECT {EDGE_COLUMNS} FROM edges ORDER BY id").fetchall()
        con.execute("DROP TABLE edges")
        insert_edges(con, rows)
        kind = None
    if kind is None:
        con.execute("""CREATE VIEW edges AS
            SELECT l.id, l.kind, sk.id AS src_id, dk.id AS dst_id, l.precision, l.layer, l.source, l.commit_sha, l.attrs
            FROM links l JOIN keys sk ON sk.k = l.src JOIN keys dk ON dk.k = l.dst""")
        con.execute("""CREATE TRIGGER IF NOT EXISTS edges_insert INSTEAD OF INSERT ON edges BEGIN
            INSERT OR IGNORE INTO keys (id) VALUES (NEW.src_id);
            INSERT OR IGNORE INTO keys (id) VALUES (NEW.dst_id);
            INSERT INTO links (id, kind, src, dst, precision, layer, source, commit_sha, attrs)
            VALUES (NEW.id, NEW.kind, (SELECT k FROM keys WHERE id = NEW.src_id), (SELECT k FROM keys WHERE id = NEW.dst_id),
                    NEW.precision, COALESCE(NEW.layer, 'fact'), NEW.source, NEW.commit_sha, NEW.attrs);
            END""")
        con.execute("CREATE TRIGGER IF NOT EXISTS edges_delete INSTEAD OF DELETE ON edges BEGIN"
                    " DELETE FROM links WHERE id = OLD.id; END")


def insert_edges(con, rows) -> None:
    """rows: (id or None, kind, src_id, dst_id, precision, layer, source, commit_sha, attrs). Straight into `links`,
    which is quicker for many rows than the view's trigger."""
    key = _keys(con, itertools.chain.from_iterable((r[2], r[3]) for r in rows))
    con.executemany("INSERT INTO links (id, kind, src, dst, precision, layer, source, commit_sha, attrs)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", ((r[0], r[1], key[r[2]], key[r[3]], *r[4:]) for r in rows))


def _insert_calls(con, rows) -> None:
    """rows: (src_id, dst_id, dispatch, precision, site_start, site_end, hit_count, commit_sha)"""
    key = _keys(con, itertools.chain.from_iterable((r[0], r[1]) for r in rows))
    con.executemany("INSERT INTO call_sites (src, dst, dispatch, precision, site_start, site_end, hit_count, commit_sha)"
                    " VALUES (?,?,?,?,?,?,?,?)", ((key[r[0]], key[r[1]], *r[2:]) for r in rows))


def _keys(con, ids) -> dict:
    ids = list(dict.fromkeys(ids))
    con.executemany("INSERT OR IGNORE INTO keys (id) VALUES (?)", ((i,) for i in ids))
    out = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        out.update((r[1], r[0]) for r in con.execute(f"SELECT k, id FROM keys WHERE id IN ({','.join('?' * len(chunk))})", chunk))
    return out


def _insert_steps(con, steps, reindex: bool = True) -> None:
    """steps: (flow id, seq, depth, callable id, via, site line, parent seq) rows. A collection that can list
    the flow ids and then the callable ids itself (indexer.FlowSteps) saves a pass over millions of rows."""
    ids = getattr(steps, "ids", None)
    key = _keys(con, ids() if ids else itertools.chain((s[0] for s in steps), (s[3] for s in steps)))
    via = {v: i for i, v in enumerate(VIA)}
    # Millions of rows on a large repo: the callable index is built once at the end rather than kept up to date
    # row by row, and the rows are made as they are inserted, not held in a second list. A few rows added to a
    # large table (reindex False) go in with the index kept.
    if reindex:
        con.execute("DROP INDEX IF EXISTS steps_callable")
    con.executemany("INSERT OR REPLACE INTO steps (flow, seq, depth, callable, via, site_line, parent_seq) VALUES (?,?,?,?,?,?,?)",
                    ((key[f], seq, depth, key[c], via.get(v, via["channel"]), line, parent)
                     for f, seq, depth, c, v, line, parent in steps))
    con.execute("CREATE INDEX IF NOT EXISTS steps_callable ON steps(callable, flow)")


def clear_facts(con: sqlite3.Connection, repo_id: str) -> None:
    """Drop every fact row for a repo. Inferred and intent rows are left alone."""
    ids = "SELECT id FROM nodes WHERE repo_id = ? AND layer = 'fact'"
    con.execute(f"DELETE FROM call_sites WHERE src IN (SELECT k FROM keys WHERE id IN ({ids}))", (repo_id,))
    con.execute(f"DELETE FROM links WHERE layer = 'fact' AND src IN (SELECT k FROM keys WHERE id IN ({ids}))", (repo_id,))
    con.execute(f"DELETE FROM ancestry WHERE node_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM search WHERE node_id IN ({ids})", (repo_id,))
    con.execute(f"DELETE FROM steps WHERE flow IN (SELECT k FROM keys WHERE id IN (SELECT id FROM flows WHERE entry_id IN ({ids})))",
                (repo_id,))
    con.execute(f"DELETE FROM flows WHERE layer = 'fact' AND entry_id IN ({ids})", (repo_id,))
    con.execute("DELETE FROM nodes WHERE repo_id = ? AND layer = 'fact'", (repo_id,))
    con.execute("DELETE FROM extractor_coverage WHERE repo_id = ?", (repo_id,))


def write_nodes(con, nodes: Iterable[Node], repo_id: str, source: str, commit: str | None) -> None:
    # Rows are made as they are inserted, not held in a list: on a large repository that list was hundreds of MB.
    con.executemany(
        "INSERT OR IGNORE INTO nodes (id, kind, name, parent_id, repo_id, language, path,"
        " span_start, span_end, content_hash, layer, source, commit_sha, attrs)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,'fact',?,?,?)",
        ((n.id, n.kind, n.name, n.parent_id, repo_id, n.language, n.path, n.span_start,
          n.span_end, n.content_hash, source, commit, json.dumps(n.attrs) if n.attrs else None)
         for n in nodes))


def write_edges(con, edges: Iterable[Edge], source: str, commit: str | None) -> None:
    edges = edges if isinstance(edges, list) else list(edges)
    key = _keys(con, itertools.chain.from_iterable((e.src_id, e.dst_id) for e in edges))
    con.executemany("INSERT INTO links (kind, src, dst, precision, layer, source, commit_sha, attrs) VALUES (?,?,?,?,'fact',?,?,?)",
                    ((e.kind, key[e.src_id], key[e.dst_id], e.precision, source, commit,
                      json.dumps(e.attrs) if e.attrs else None) for e in edges))


def write_calls(con, rows: Iterable[tuple], commit: str | None) -> None:
    """rows: (src_id, dst_id, dispatch, precision, line)"""
    rows = rows if isinstance(rows, list) else list(rows)
    key = _keys(con, itertools.chain.from_iterable((r[0], r[1]) for r in rows))
    con.executemany("INSERT INTO call_sites (src, dst, dispatch, precision, site_start, site_end, hit_count, commit_sha)"
                    " VALUES (?,?,?,?,?,?,0,?)", ((key[s], key[d], disp, prec, line, line, commit) for s, d, disp, prec, line in rows))


def write_flows(con, repo_id: str, flows, steps, reindex: bool = True) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO flows (id, name, origin, entry_id, weight, group_id, layer, source, attrs)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(f[0], f[1], f[2], f[3], f[4], f[5], f[6], f[7], json.dumps(f[8])) for f in flows])
    _insert_steps(con, steps, reindex)


def evidence_hash(con, evidence: Iterable[str]) -> str:
    """A hash of the files the evidence nodes live in. It changes when any of them is edited."""
    parts = []
    for node_id in sorted(set(evidence)):
        row = con.execute(
            "SELECT COALESCE(f.content_hash, n.content_hash, '') FROM nodes n"
            " LEFT JOIN ancestry a ON a.node_id = n.id LEFT JOIN nodes f ON f.id = a.file_id WHERE n.id = ?",
            (node_id,)).fetchone()
        parts.append(f"{node_id}:{row[0] if row else 'missing'}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def annotate(con, node_id: str, key: str, value: str, layer: str, source: str,
             confidence: float | None, evidence: list[str]) -> dict:
    """Write one inferred or intent statement about a node, replacing an earlier one with the same key."""
    if layer not in ("inferred", "intent"):
        raise ValueError("layer must be 'inferred' or 'intent'; facts come only from extractors")
    if con.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone() is None:
        raise ValueError(f"no node with id {node_id!r}")
    if layer == "inferred":
        if not evidence:
            raise ValueError("an inferred annotation needs evidence: the node ids it is based on")
        missing = [e for e in evidence if con.execute("SELECT 1 FROM nodes WHERE id = ?", (e,)).fetchone() is None]
        if missing:
            raise ValueError(f"evidence ids not in the store: {missing[:3]}")
    with con:
        con.execute("DELETE FROM annotations WHERE node_id = ? AND key = ? AND layer = ?", (node_id, key, layer))
        con.execute(
            "INSERT INTO annotations (node_id, key, value, layer, source, confidence, evidence, evidence_hash, stale)"
            " VALUES (?,?,?,?,?,?,?,?,0)",
            (node_id, key, value, layer, source, confidence, json.dumps(evidence or []),
             evidence_hash(con, evidence or [])))
    return {"node_id": node_id, "key": key, "layer": layer, "evidence": len(evidence or [])}


def refresh_stale(con) -> int:
    """Flag annotations whose evidence changed or whose node is gone. Returns how many are stale."""
    stale = 0
    with con:
        for a in con.execute("SELECT id, node_id, evidence, evidence_hash FROM annotations").fetchall():
            gone = con.execute("SELECT 1 FROM nodes WHERE id = ?", (a["node_id"],)).fetchone() is None
            changed = evidence_hash(con, json.loads(a["evidence"] or "[]")) != a["evidence_hash"]
            flag = 1 if gone or (changed and a["evidence"] not in (None, "[]")) else 0
            con.execute("UPDATE annotations SET stale = ? WHERE id = ?", (flag, a["id"]))
            stale += flag
    return stale


def write_coverage(con, repo_id, extractor, version, status, commit, stats: dict) -> None:
    # Keys sorted: the counts are added up in another order by an incremental run than by a full one.
    con.execute(
        "INSERT OR REPLACE INTO extractor_coverage VALUES (?,?,?,?,?,?)",
        (repo_id, extractor, version, status, commit, json.dumps(stats, sort_keys=True)),
    )


def rebuild_derived(con: sqlite3.Connection, systems_of: Optional[str] = None) -> None:
    """Recompute the ancestry cache and the search index from the node table. `systems_of` is for after
    clustering a repo, which removes and adds only that repo's system nodes, the new ones after every other
    node: the rows of other nodes stay, which gives the same tables as a full rebuild without rewriting every row."""
    have: set = set()
    if systems_of is not None:
        kept = "SELECT id FROM nodes WHERE NOT (kind = 'system' AND repo_id IS ?)"
        con.execute(f"DELETE FROM ancestry WHERE node_id NOT IN ({kept})", (systems_of,))
        con.execute(f"DELETE FROM search WHERE node_id NOT IN ({kept})", (systems_of,))
        have = {r[0] for r in con.execute("SELECT node_id FROM ancestry")}
    else:
        con.execute("DELETE FROM ancestry")
        con.execute("DELETE FROM search")
    # The node table is read three times, as rows come, and in between only each node's kind and parent is held
    # (by number): every row held at once, and every row made before inserting, was most of a GB on a large
    # repository. The same query each time, so the rows come in the same order.
    q = "SELECT id, kind, name, parent_id, path FROM nodes"
    share = {}.setdefault   # one string per kind
    index: dict[str, int] = {}
    ids: list[str] = []
    kinds: list[str] = []
    parents: list = []
    for r in con.execute(q):
        index[r[0]] = len(ids)
        ids.append(r[0])
        kinds.append(share(r[1], r[1]))
        parents.append(r[3])
    up = array.array("i", (index.get(p, -1) if p else -1 for p in parents))
    del parents

    def ancestry():
        for r in con.execute(q):
            if r[0] in have:
                continue
            file_id = module_id = None
            k, seen = index[r[0]], 0
            while k >= 0 and seen < 64:
                kind = kinds[k]
                if kind == "file" and file_id is None:
                    file_id = ids[k]
                if kind == "module" and module_id is None:
                    module_id = ids[k]
                k = up[k]
                seen += 1
            yield r[0], file_id, module_id

    con.executemany("INSERT INTO ancestry VALUES (?,?,?)", ancestry())
    con.executemany("INSERT INTO search (node_id, name, qualified, path, kind) VALUES (?,?,?,?,?)",
                    ((r[0], r[2], _searchable(r[0]), r[4] or "", r[1]) for r in con.execute(q)
                     if r[0] not in have and r[1] not in ("repo",)))


def derive_some(con, gone: Iterable[str], ids: Iterable[str]) -> None:
    """rebuild_derived for a few nodes: drop the ancestry and search rows of `gone` and of `ids`, and make them again
    for `ids` from the node table."""
    ids = list(dict.fromkeys(ids))
    drop = list(dict.fromkeys(itertools.chain(gone, ids)))
    for i in range(0, len(drop), 900):
        chunk = drop[i:i + 900]
        marks = ",".join("?" * len(chunk))
        con.execute(f"DELETE FROM ancestry WHERE node_id IN ({marks})", chunk)
        con.execute(f"DELETE FROM search WHERE node_id IN ({marks})", chunk)
    row = lambda i: con.execute("SELECT id, kind, name, parent_id, path FROM nodes WHERE id = ?", (i,)).fetchone()
    for i in ids:
        r = row(i)
        if r is None:
            continue
        file_id = module_id = None
        cur, seen = r, 0
        while cur is not None and seen < 64:
            if cur["kind"] == "file" and file_id is None:
                file_id = cur["id"]
            if cur["kind"] == "module" and module_id is None:
                module_id = cur["id"]
            cur = row(cur["parent_id"]) if cur["parent_id"] else None
            seen += 1
        con.execute("INSERT INTO ancestry VALUES (?,?,?)", (r["id"], file_id, module_id))
        if r["kind"] != "repo":
            con.execute("INSERT INTO search (node_id, name, qualified, path, kind) VALUES (?,?,?,?,?)",
                        (r["id"], r["name"], _searchable(r["id"]), r["path"] or "", r["kind"]))


def _searchable(node_id: str) -> str:
    # Split the qualified part of an id into words so "Simulation Step" finds Simulation.Step.
    tail = node_id.split(":", 2)[-1]
    for ch in ".()/,<>:_":
        tail = tail.replace(ch, " ")
    return tail


def store_file(con) -> Optional[Path]:
    """The file this connection's store lives in, or None for an in-memory store."""
    row = con.execute("PRAGMA database_list").fetchone()
    return Path(row[2]).resolve() if row and row[2] else None


def set_root(con, repo: str, root) -> None:
    """Record where a repository is: as given, and relative to the store, so a store that moves with its
    repository (a copied or renamed checkout, a store kept in .leyline/) still finds the code."""
    root = Path(root).resolve()
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"root:{repo}", str(root)))
    here = store_file(con)
    if here is not None:
        import os
        try:
            con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"rel:{repo}", os.path.relpath(root, here.parent)))
        except ValueError:   # another drive on Windows: no relative path exists
            pass


def roots(con) -> dict:
    """{repo id: the directory its code is in now}. The path relative to the store wins when it leads to a
    directory; a store written before that was recorded, sitting in X/.leyline/ for a single repository,
    belongs to X. Otherwise the path recorded when it was indexed."""
    here = store_file(con)
    meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta WHERE key LIKE 'root:%' OR key LIKE 'rel:%'")}
    out = {}
    repos = [k.split(":", 1)[1] for k in meta if k.startswith("root:")]
    for repo in repos:
        path = Path(meta[f"root:{repo}"])
        rel = meta.get(f"rel:{repo}")
        if here is not None and rel is not None and (here.parent / rel).is_dir():
            path = (here.parent / rel).resolve()
        elif here is not None and rel is None and len(repos) == 1 and here.parent.name == ".leyline" \
                and here.parent.parent.is_dir() and here.parent.parent.resolve() != path:
            path = here.parent.parent.resolve()
        out[repo] = path
    return out


def test_places(con) -> tuple[set, set]:
    """Where the tests are: the files that hold a test (a test node, or the entry of a test flow), and the modules
    that are mostly such files (a test project). A module that holds a few test files beside its product code (a
    TypeScript package with its *.test.ts next to the source) is not a test module: its other files are product."""
    files_of = {}
    for r in con.execute("SELECT n.id, n.path, a.module_id FROM nodes n LEFT JOIN ancestry a ON a.node_id = n.id"
                         " WHERE n.kind = 'file' AND n.layer = 'fact'"):
        files_of[r[1]] = r[2]
    tests = {r[0] for r in con.execute("SELECT path FROM nodes WHERE kind = 'test' AND path IS NOT NULL")}
    tests |= {r[0] for r in con.execute("SELECT n.path FROM flows f JOIN nodes n ON n.id = f.entry_id"
                                        " WHERE json_extract(f.attrs, '$.kind') = 'test' AND n.path IS NOT NULL")}
    per = {}
    for path, mod in files_of.items():
        t, n = per.get(mod, (0, 0))
        per[mod] = (t + (path in tests), n + 1)
    # A test project is named for it (tests/, Foo.Tests, spec), or is nearly all test files: a package with its
    # own test folder can hold as many test files as source files and still be the product.
    named = re.compile(r"(^|[/.:_-])(tests?|specs?|__tests__|testing|e2e)$", re.I)
    modules = {m for m, (t, n) in per.items() if m and n and t and (named.search(m) or t * 10 >= n * 8)}
    return tests, modules
