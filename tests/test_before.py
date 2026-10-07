"""The "before" diagram: a change's baseline keeps what a sequence diagram reads, so the code as it was is drawn from
the baseline alone, arrow for arrow what the full map of that code would draw, and every arrow is checked against the
baseline. A baseline written by an older Leyline still compares; it just is not drawn."""

import shutil
import sqlite3
from pathlib import Path

from leyline import diagrams, diff, store
from leyline.indexer import index

from test_diagrams import APP, FIXTURE5, node, write


def _map(tmp_path, files=None, fixture=None):
    root, db = tmp_path / "repo", tmp_path / "s.db"
    if fixture:
        shutil.copytree(fixture, root)
    else:
        write(root, files)
    index(root, db, "r")
    return root, db


def _same_as_full_map(db: Path, ids: list[str]) -> None:
    con = store.connect(db)
    try:
        snap = diff.snapshot(con, "x")
        before = diff._open(snap)
        try:
            assert diagrams.drawable(before)
            for i in ids:
                from_store, from_snap = diagrams.sequence(con, [i]), diagrams.sequence(before, [i])
                assert from_snap == from_store, i
                assert from_snap["mermaid"] and diagrams.unbacked(before, from_snap) == []
        finally:
            before.close()
    finally:
        con.close()


def test_a_baseline_draws_what_the_full_map_draws(tmp_path):
    root, db = _map(tmp_path, APP)
    con = store.connect(db)
    ids = [node(con, n) for n in ("run", "check", "load", "save", "main")]
    con.close()
    _same_as_full_map(db, ids)


def test_channels_interfaces_and_the_registered_implementation_survive_the_baseline(tmp_path):
    _, db = _map(tmp_path, fixture=FIXTURE5)
    con = store.connect(db)
    ids = [r[0] for r in con.execute("SELECT id FROM nodes WHERE kind = 'callable' AND layer = 'fact' AND"
                                     " (id LIKE '%OrderStore.Save(%' OR id LIKE '%.Pending(%')")]
    con.close()
    assert len(ids) >= 3
    _same_as_full_map(db, ids)


def test_the_code_as_it_was_comes_from_the_baseline_only(tmp_path):
    root, db = _map(tmp_path, APP)
    con = store.connect(db)
    diff.snapshot(con, "x")
    con.close()
    svc = root / "app/service.py"   # run stops checking and audits instead; check is deleted
    svc.write_text(svc.read_text().replace("        self.check(data)\n", "        audit(name)\n")
                   .replace("    def check(self, data):\n        return bool(data)\n", "")
                   + "\n\ndef audit(name):\n    return name\n")
    index(root, db, "r")
    con = store.connect(db)
    try:
        run, audit = node(con, "run"), node(con, "audit")
        snap = diff.snapshot_path(con, "x")
        assert con.execute("SELECT 1 FROM nodes WHERE name = 'check'").fetchone() is None   # gone from the store
        view = diagrams.for_snapshot(snap, con, [run, audit])
        before, after = view["before"], view["after"]
        assert "check()" in before["mermaid"] and "audit()" not in before["mermaid"]
        assert "audit()" in after["mermaid"] and "check()" not in after["mermaid"]
        b = diff._open(snap)
        try:
            assert diagrams.unbacked(b, before) == []
            # an arrow only the current map holds is caught against the baseline
            new_call = next(a for a in after["arrows"] if a["to"] == audit)
            assert diagrams.unbacked(b, {"arrows": [new_call]}) == [new_call]
        finally:
            b.close()
        page = "\n".join(diagrams.section(view, "### How it runs now"))
        assert page.index("**Before**") < page.index("**After**") < page.index("**What changed in how it runs**")
        assert page.count("```mermaid") == 2 and "- `Service.run` now calls `service.py.audit`" in page
    finally:
        con.close()


# What an older Leyline wrote: pairs and step sets, no order.
_OLD = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE keys (k INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE);
CREATE TABLE node_rows (node INTEGER PRIMARY KEY, kind TEXT, name TEXT, parent INTEGER, repo_id TEXT, path TEXT,
                        span_start INTEGER, span_end INTEGER, content_hash TEXT, layer TEXT);
CREATE TABLE node_files (node INTEGER PRIMARY KEY, file INTEGER, module INTEGER);
CREATE TABLE call_pairs (src INTEGER, dst INTEGER, PRIMARY KEY (src, dst)) WITHOUT ROWID;
CREATE TABLE edge_pairs (kind TEXT, src INTEGER, dst INTEGER, PRIMARY KEY (kind, src, dst)) WITHOUT ROWID;
CREATE TABLE flow_rows (flow INTEGER PRIMARY KEY, name TEXT, entry INTEGER, kind TEXT);
CREATE TABLE steps (flow INTEGER, callable INTEGER, PRIMARY KEY (flow, callable)) WITHOUT ROWID;
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
CREATE VIEW calls AS SELECT s.id AS src_id, d.id AS dst_id FROM call_pairs c JOIN keys s ON s.k = c.src JOIN keys d ON d.k = c.dst;
CREATE VIEW edges AS SELECT e.kind, s.id AS src_id, d.id AS dst_id FROM edge_pairs e JOIN keys s ON s.k = e.src
    JOIN keys d ON d.k = e.dst;
CREATE VIEW flow_steps AS SELECT f.id AS flow_id, 0 AS seq, c.id AS callable_id FROM steps s JOIN keys f ON f.k = s.flow
    JOIN keys c ON c.k = s.callable;
"""


def _old_snapshot(new: Path, old: Path) -> None:
    """The same baseline as an older Leyline kept it."""
    out = sqlite3.connect(str(old))
    out.executescript(_OLD)
    out.execute("ATTACH DATABASE ? AS n", (str(new),))
    with out:
        for t in ("meta", "keys", "node_rows", "node_files", "flow_rows", "source_lines"):
            out.execute(f"INSERT INTO main.{t} SELECT * FROM n.{t}")
        out.execute("INSERT INTO main.call_pairs SELECT src, dst FROM n.call_pairs")
        out.execute("INSERT INTO main.edge_pairs SELECT kind, src, dst FROM n.edge_pairs")
        out.execute("INSERT OR IGNORE INTO main.steps SELECT flow, callable FROM n.steps")
    out.execute("DETACH DATABASE n")
    out.close()


def test_a_baseline_from_an_older_leyline_still_compares_but_is_not_drawn(tmp_path):
    root, db = _map(tmp_path, APP)
    con = store.connect(db)
    new = diff.snapshot(con, "x")
    con.close()
    old = new.with_name("old.db")
    _old_snapshot(new, old)
    svc = root / "app/service.py"
    svc.write_text(svc.read_text().replace("        self.check(data)\n", ""))
    index(root, db, "r")
    con = store.connect(db)
    try:
        run = node(con, "run")
        results = []
        for snap in (new, old):
            b = diff._open(snap)
            try:
                results.append(diff.compare(b, con))
            finally:
                b.close()
        assert results[0]["nodes"] == results[1]["nodes"] and results[0]["links"] == results[1]["links"]
        assert results[0]["flows"] == results[1]["flows"]
        view = diagrams.for_snapshot(old, con, [run])
        assert "before" not in view and view["before_missing"] == "old"
        lines = diagrams.section(view, "### How it runs now")
        assert any("taken by an older Leyline" in ln for ln in lines)
        assert "- `Service.run` no longer calls `Service.check`" in lines
        assert "before" in diagrams.for_snapshot(new, con, [run])
    finally:
        con.close()


def test_a_baseline_stays_small(tmp_path):
    """What the drawing needs is a few integers a call pair and a flow step: the baseline grows by well under half."""
    _, db = _map(tmp_path, fixture=FIXTURE5)
    con = store.connect(db)
    try:
        new = diff.snapshot(con, "x")
    finally:
        con.close()
    old = new.with_name("old.db")
    _old_snapshot(new, old)
    assert new.stat().st_size <= old.stat().st_size * 1.5
