from pathlib import Path

import pytest

from leyline import query, store
from leyline.indexer import index

FIXTURE = Path(__file__).parent / "fixture"


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "t.db"
    index(FIXTURE, db, "fx")
    c = store.connect(db)
    yield c
    c.close()


def ids(con, sql, *args):
    return {r[0] for r in con.execute(sql, args)}


def calls(con, src_like):
    return ids(con, "SELECT dst_id FROM calls WHERE src_id LIKE ?", src_like)


def test_modules_and_project_reference(con):
    mods = ids(con, "SELECT id FROM nodes WHERE kind = 'module'")
    assert mods == {"fx:module:App", "fx:module:Lib", "fx:module:scripts"}
    assert ("fx:module:App", "fx:module:Lib") in {
        (r[0], r[1]) for r in con.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'imports'")}
    assert "fx:ext:nuget:Newtonsoft.Json" in ids(con, "SELECT dst_id FROM edges WHERE kind = 'depends_on'")


def test_same_type_name_in_two_projects_stays_separate(con):
    programs = ids(con, "SELECT id FROM nodes WHERE kind = 'type' AND name = 'Program'")
    assert programs == {"fx:csharp:App::Program", "fx:csharp:Lib::Lib.Program"}


def test_overloads_are_separate_callables(con):
    totals = ids(con, "SELECT id FROM nodes WHERE kind = 'callable' AND name = 'Total' AND language = 'csharp'")
    assert totals == {"fx:csharp:Lib::Lib.Canvas.Total()", "fx:csharp:Lib::Lib.Canvas.Total(double)"}


def test_inheritance_and_generic_type_use(con):
    assert ("fx:csharp:Lib::Lib.Circle", "fx:csharp:Lib::Lib.IShape") in {
        (r[0], r[1]) for r in con.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'implements'")}
    # List<IShape> links to IShape even though List is outside the workspace.
    assert "fx:csharp:Lib::Lib.IShape" in ids(
        con, "SELECT dst_id FROM edges WHERE kind = 'uses_type' AND src_id = 'fx:csharp:Lib::Lib.Canvas._shapes'")


def test_csharp_calls(con):
    main = calls(con, "fx:csharp:App::Program.Main(%)")
    assert "fx:csharp:Lib::Lib.Canvas.Add(IShape)" in main
    assert "fx:csharp:Lib::Lib.Canvas.Total()" in main          # argc picks the right overload
    assert "fx:csharp:Lib::Lib.Canvas.Total(double)" not in main
    assert "fx:csharp:Lib::Lib.Circle..ctor(double)" in main
    assert any(i.endswith("/Twice(double)") for i in main)       # local function
    total = calls(con, "fx:csharp:Lib::Lib.Canvas.Total()")
    assert "fx:csharp:Lib::Lib.IShape.Area()" in total


def test_outside_calls_are_not_guessed(con):
    # _shapes.Add is List.Add, and File.Open is System.IO: neither may link to workspace code.
    assert calls(con, "fx:csharp:Lib::Lib.Canvas.Add(%)") == set()
    assert not any("Open" in i for i in calls(con, "fx:csharp:App::Program.Main(%)"))


def test_python_imports_and_calls(con):
    main = calls(con, "fx:python:scripts.run.main")
    assert main == {"fx:python:scripts.util.load", "fx:python:scripts.util.Report.__init__",
                    "fx:python:scripts.util.Report.total"}
    assert "fx:python:scripts.run.main" in calls(con, "fx:python:scripts.run.<module>")
    imports = {(r[0], r[1]) for r in con.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'imports'")}
    assert ("fx:file:scripts/run.py", "fx:file:scripts/util.py") in imports
    assert ("fx:file:scripts/run.py", "fx:ext:python:sys") in imports


def test_entry_points(con):
    eps = ids(con, "SELECT dst_id FROM edges WHERE kind = 'exposes'")
    assert "fx:python:scripts.run.<module>" in eps
    assert any(i.startswith("fx:csharp:App::Program.Main(") for i in eps)


def test_overview_rolls_up_to_modules(con):
    o = query.overview(con)
    edge = next(e for e in o["module_edges"] if e["from"] == "fx:module:App" and e["to"] == "fx:module:Lib")
    assert edge["calls"] >= 3 and edge["imports"] >= 1
    assert {c["status"] for c in o["coverage"] if c["extractor"].startswith("communicates")} == {"not_analyzed"}


def test_expand_and_search(con):
    hit = query.search(con, "canvas total")["results"][0]
    assert hit["name"] == "Total"
    t = query.expand(con, "fx:csharp:Lib::Lib.Canvas")
    assert t["contains"]["callable"]["total"] == 3
    c = query.expand(con, "fx:csharp:Lib::Lib.Canvas.Total()")
    assert c["called_by"]["total"] >= 2
    assert "did_you_mean" in query.expand(con, "fx:csharp:Nope")


def test_reindex_is_idempotent(con, tmp_path):
    db = tmp_path / "again.db"
    index(FIXTURE, db, "fx")
    c2 = store.connect(db)
    before = c2.execute("SELECT (SELECT COUNT(*) FROM nodes), (SELECT COUNT(*) FROM edges), (SELECT COUNT(*) FROM calls)").fetchone()
    c2.close()
    index(FIXTURE, db, "fx")
    c2 = store.connect(db)
    after = c2.execute("SELECT (SELECT COUNT(*) FROM nodes), (SELECT COUNT(*) FROM edges), (SELECT COUNT(*) FROM calls)").fetchone()
    assert tuple(before) == tuple(after)


def test_export_embeds_a_loadable_graph(con):
    import json
    import re

    from leyline import export

    g = export.graph(con)
    kinds = {n["k"] for n in g["nodes"]}
    assert {"repo", "module", "file", "type", "callable"} <= kinds
    assert all(0 <= e[1] < len(g["nodes"]) and 0 <= e[2] < len(g["nodes"]) for e in g["edges"])
    assert "scripts/run.py" in g["sources"]
    page = export.page(con)
    data = re.search(r'<script id="leyline-data" type="application/json">(.*?)</script>', page, re.S).group(1)
    assert json.loads(data)["nodes"][0]["k"] == "repo"
    assert "__LEYLINE" not in page
