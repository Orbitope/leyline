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
    status = {c["extractor"]: c["status"] for c in o["coverage"]}
    assert status["communicates:event"] == "ok" and status["communicates:process"] == "ok"
    assert status["communicates:http"] == "not_analyzed"


def test_expand_and_search(con):
    hit = query.search(con, "canvas total", kind="callable")["results"][0]
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


def comm(con):
    import json
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in con.execute(
        "SELECT src_id, dst_id, precision, attrs FROM edges WHERE kind = 'communicates'")]


def test_event_links_raiser_to_handler(con):
    events = [c for c in comm(con) if c[3]["channel"] == "event"]
    assert [(s, d) for s, d, _, _ in events] == [
        ("fx:csharp:Lib::Lib.Bus.Set(int)", "fx:csharp:App::Checks.OnChanged(int)")]
    assert events[0][3]["address"] == "fx:csharp:Lib::Lib.Bus.Changed"
    assert events[0][3]["handler"] == "method"


def test_process_launch_links_to_the_program_entry(con):
    procs = [c for c in comm(con) if c[3]["channel"] == "process"]
    assert len(procs) == 1
    src, dst, precision, attrs = procs[0]
    assert src == "fx:python:scripts.launch.start"
    assert dst.startswith("fx:csharp:App::Program.Main(")
    assert precision == "heuristic" and attrs["pipes"] is True


def test_inline_tests_become_nodes_with_their_own_calls(con):
    tests = {r[0]: r[1] for r in con.execute("SELECT name, id FROM nodes WHERE kind = 'test'")}
    assert set(tests) == {"bus notifies a subscriber", "canvas totals"}
    assert calls(con, tests["canvas totals"]) == {"fx:csharp:Lib::Lib.Canvas.Total()"}
    assert "fx:csharp:Lib::Lib.Bus.Set(int)" in calls(con, tests["bus notifies a subscriber"])
    # The enclosing method runs the tests but does not make their calls itself.
    assert "fx:csharp:Lib::Lib.Canvas.Total()" not in calls(con, "fx:csharp:App::Checks.All()")


def test_flows_follow_calls_and_only_subscribed_events(con):
    from leyline import query
    listed = query.flows(con, kind="test")["flows"]
    assert {f["name"] for f in listed} == {"bus notifies a subscriber", "canvas totals"}
    bus = query.flow(con, next(f["id"] for f in listed if f["name"].startswith("bus")))
    names = [s["name"] for s in bus["steps"]]
    assert names[0] == "bus notifies a subscriber" and "Set" in names
    assert names.index("OnChanged") > names.index("Set")      # reached through the event
    assert bus["steps"][names.index("OnChanged")]["via"] == "event"
    canvas = query.flow(con, next(f["id"] for f in listed if f["name"].startswith("canvas")))
    assert "OnChanged" not in [s["name"] for s in canvas["steps"]]


def test_trace_and_impact(con):
    from leyline import query
    t = query.trace(con, "fx:python:scripts.launch.start", "fx:csharp:Lib::Lib.Canvas.Total()")
    assert t["found"] and t["path"][1]["via"] == "communicates"
    assert [p["name"] for p in t["path"]] == ["start", "Main", "Total"]
    i = query.impact(con, "fx:csharp:Lib::Lib.Canvas.Total()")
    assert i["crosses_module_boundary"]
    assert {g["module"] for g in i["by_module"]} >= {"App", "Lib", "scripts"}
    assert i["flows_through"]["total"] >= 2


def test_implementations_link_to_interface_methods(con):
    from leyline import query
    pairs = {(r[0], r[1]) for r in con.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'overrides'")}
    assert ("fx:csharp:Lib::Lib.Circle.Area()", "fx:csharp:Lib::Lib.IShape.Area()") in pairs
    # A flow that calls the interface method continues into the implementation.
    listed = query.flows(con, kind="test")["flows"]
    steps = query.flow(con, next(f["id"] for f in listed if f["name"].startswith("canvas")))["steps"]
    names = [(s["name"], s["via"]) for s in steps]
    assert ("Area", "dispatch") in names
    # And changing the implementation is reported as reaching the interface's callers.
    i = query.impact(con, "fx:csharp:Lib::Lib.Circle.Area()")
    assert i["reached_by"] >= 2 and i["flows_through"]["total"] >= 1


def test_systems_are_proposed_and_named_through_annotations(tmp_path):
    import shutil

    from leyline import cluster, query

    work = tmp_path / "repo"
    shutil.copytree(FIXTURE, work)
    db = tmp_path / "s.db"
    index(work, db, "fx")
    c = store.connect(db)
    # The fixture is too small to split by default; lower the bar to exercise the path.
    made = cluster.propose(c, "fx", min_units=2, min_modularity=0.0)
    assert made["Lib"]["systems"] >= 1
    systems = query.systems_list(c)
    lib = next(s for s in systems if s["module"] == "Lib")
    assert not lib["named"] and len(lib["members"]) >= 2
    evidence = [r[0] for r in c.execute("SELECT dst_id FROM edges WHERE kind = 'groups' AND src_id = ?", (lib["id"],))]

    assert "error" in query.annotate(c, lib["id"], "name", "Shapes", [], 0.8)            # no evidence
    assert "error" in query.annotate(c, lib["id"], "name", "Shapes", evidence, 0.8, "fact")
    assert "error" not in query.annotate(c, lib["id"], "name", "Shapes", evidence, 0.8)
    named = next(s for s in query.systems_list(c) if s["id"] == lib["id"])
    assert named["name"] == "Shapes" and named["named"] and not named["stale"]

    # Editing a file behind the evidence marks the annotation stale on the next index.
    target = work / "Lib" / "Shapes.cs"
    target.write_text(target.read_text() + "\n// changed\n")
    c.close()
    stats = index(work, db, "fx")
    assert stats["stale_annotations"] >= 1


def test_change_assessment_and_saved_views(tmp_path):
    import shutil

    from leyline import change, export

    work = tmp_path / "repo"
    shutil.copytree(FIXTURE, work)
    db = tmp_path / "c.db"
    index(work, db, "fx")
    c = store.connect(db)
    area = "fx:csharp:Lib::Lib.IShape.Area()"

    bad = change.propose(c, "x", [{"id": "nope", "action": "signature"}])
    assert "error" in bad

    r = change.propose(c, "Make Area take a unit", [{"id": area, "action": "signature", "note": "adds a unit"}])
    must = {m["id"]: m for m in r["must_edit"]}
    assert "fx:csharp:Lib::Lib.Circle.Area()" in must                      # the implementation
    assert "fx:csharp:Lib::Lib.Canvas.Total()" in must                     # the direct caller
    assert any(t["name"] == "canvas totals" for t in r["tests_to_run"])
    assert {g["module"] for g in r["by_module"]} >= {"Lib", "App"}
    assert any("outside its own module" in k["what"] for k in r["risks"])
    # The Python launcher reaches it through the process channel.
    assert any(ch["channel"] == "process" for ch in r["channels"])

    # Changing one implementation pulls in the interface and its other callers.
    r2 = change.assess(c, "Circle area in square metres", [{"id": "fx:csharp:Lib::Lib.Circle.Area()", "action": "signature"}])
    roles = {m["id"]: m["role"] for m in r2["marks"]}
    assert roles[area] == "contract"
    assert roles["fx:csharp:Lib::Lib.Canvas.Total()"] == "must_edit"

    # Something new attaches to what will call it.
    r3 = change.assess(c, "Add a perimeter", [{"action": "add", "name": "Canvas.Perimeter()",
                                               "parent": "fx:csharp:Lib::Lib.Canvas",
                                               "used_by": ["fx:csharp:App::Program.Main(string[])"]}])
    assert r3["summary"]["added"] == 1 and r3["must_edit"][0]["id"].startswith("fx:csharp:App::Program.Main(")

    v = change.save_view(c, "Shapes", "The shape types.", [
        {"id": "fx:csharp:Lib::Lib.Circle", "role": "shape", "note": "a circle"}, {"id": "missing", "role": "x"}])
    assert v["marks"] == 1 and v["missing"] == ["missing"]
    listed = change.list_views(c)["views"]
    assert {x["kind"] for x in listed} == {"change", "custom"}
    assert change.get_view(c, r["view_id"])["summary"]["must_edit"] == r["summary"]["must_edit"]
    g = export.graph(c)
    assert len(g["views"]) == 2 and all("i" in m for vw in g["views"] for m in vw["marks"])

    # Views and proposals survive a re-index; they are not facts.
    c.close()
    index(work, db, "fx")
    c = store.connect(db)
    assert change.list_views(c)["total"] == 2


def test_rules_are_checked_against_the_graph(tmp_path):
    from leyline import rules

    db = tmp_path / "r.db"
    index(FIXTURE, db, "fx")
    c = store.connect(db)
    assert "error" in rules.add_rule(c, "forbid", "module:Nope", "module:Lib")
    ok = rules.add_rule(c, "forbid", "module:Lib", "module:App", reason="the library must not know the app")
    assert ok["status"] == "suggested"
    bad = rules.add_rule(c, "forbid", "module:App", "module:Lib", status="confirmed")
    rules.add_rule(c, "no_cycle", "modules")
    r = {x["id"]: x for x in rules.check(c)["rules"]}
    assert r[ok["id"]]["passes"]
    assert not r[bad["id"]]["passes"] and r[bad["id"]]["examples"]
    assert all(x["passes"] for x in r.values() if x["kind"] == "no_cycle")
    assert rules.confirm_rule(c, ok["id"])["status"] == "confirmed"


def test_review_compares_an_implemented_change_with_its_proposal(tmp_path):
    import shutil

    from leyline import change, diff, export, rules

    work = tmp_path / "repo"
    shutil.copytree(FIXTURE, work)
    db = tmp_path / "store" / "d.db"
    db.parent.mkdir()
    index(work, db, "fx")
    c = store.connect(db)
    rules.add_rule(c, "forbid", "module:Lib", "external:System.IO")
    p = change.propose(c, "Circle area in square metres",
                       [{"id": "fx:csharp:Lib::Lib.Circle.Area()", "action": "behavior"}])
    assert (db.parent / "snapshots" / f"{p['change_id']}.db").exists()
    assert "error" in diff.review(c, "chg-none")

    results = diff.parse_test_output("  PASS  canvas totals\n  FAIL  other thing: boom\nnoise\n")
    assert results == [{"name": "canvas totals", "status": "pass", "message": None},
                       {"name": "other thing", "status": "fail", "message": "boom"}]
    assert diff.record_tests(c, "before", [{"name": "canvas totals", "status": "pass"}])["matched_to_test_nodes"] == 1

    shapes = work / "Lib" / "Shapes.cs"
    text = shapes.read_text()
    text = text.replace("Math.PI * R * R;", "Math.PI * R * R / 10000.0;")                         # the predicted edit
    text = text.replace("{ _shapes.Add(s); }", "{ File.Delete(\"x\"); _shapes.Add(s); }")            # not predicted
    text = text.replace("using System;", "using System;\nusing System.IO;")                      # breaks the rule
    text = text.replace("Total(double scale) => Total() * scale;",
                        "Total(double scale, int n) => Total() * scale * n;")                     # a new signature
    shapes.write_text(text)
    index(work, db, "fx")
    c = store.connect(db)
    diff.record_tests(c, "after", [{"name": "canvas totals", "status": "fail", "message": "expected 3.14"}])

    r = diff.review(c, p["change_id"], "before", "after")
    assert [n["name"] for n in r["as_predicted"]] == ["Circle.Area"]
    surprise = {n["name"] for n in r["not_predicted"]}
    assert {"Canvas.Add", "Canvas.Total"} <= surprise and "Canvas" not in surprise
    assert r["predicted_untouched"] == []
    assert [(n["was"], n["now"]) for n in r["graph"]["nodes"]["resigned"]] == [("double", "double,int")]
    assert [x["kind"] for x in r["rules"]["new_violations"]] == ["forbid"]
    assert r["tests"]["newly_failing"][0]["name"] == "canvas totals"
    assert {v["level"] for v in r["verdict"]} == {"medium", "high"}
    assert "not predicted" in diff.review_text(r)

    view = change.get_view(c, r["view_id"])
    assert view["kind"] == "review" and {m["role"] for m in view["marks"]} == {"edited as predicted", "edited, not predicted"}
    exported = next(v for v in export.graph(c, with_sources=False)["views"] if v["kind"] == "review")
    assert all(isinstance(n["i"], int) for n in exported["review"]["not_predicted"])


FIXTURE2 = Path(__file__).parent / "fixture2"


@pytest.fixture(scope="module")
def con2(tmp_path_factory):
    db = tmp_path_factory.mktemp("db2") / "t.db"
    index(FIXTURE2, db, "f2")
    c = store.connect(db)
    yield c
    c.close()


def test_file_scoped_namespace_and_generic_arity(con2):
    types = ids(con2, "SELECT id FROM nodes WHERE kind = 'type' AND language = 'csharp'")
    assert {"f2:csharp:cs/Mod::Mod.Boxes.Box", "f2:csharp:cs/Mod::Mod.Boxes.Box`1", "f2:csharp:cs/Mod::Mod.Limit"} <= types
    links = {(r[0], r[1].split("::")[1], r[2].split("::")[1]) for r in con2.execute(
        "SELECT kind, src_id, dst_id FROM edges WHERE kind IN ('extends', 'implements') AND src_id LIKE 'f2:csharp:%'")}
    assert ("implements", "Mod.Boxes.Box", "Mod.Boxes.IBox") in links
    assert ("extends", "Mod.Boxes.Box`1", "Mod.Boxes.Box") in links          # the non-generic base, not itself
    assert ("extends", "Mod.Boxes.Crate", "Mod.Boxes.Box`1") in links        # Box<string> is the generic one
    made = {r[1].split("::")[1] for r in con2.execute(
        "SELECT kind, dst_id FROM edges WHERE kind = 'instantiates' AND src_id LIKE '%Runner.Main()'")}
    assert {"Mod.Boxes.Box", "Mod.Boxes.Box`1", "Mod.Limit"} == made


def test_overloads_are_told_apart_by_what_the_call_passes(con2):
    main = {r[0].split("Runner.")[1] for r in con2.execute("SELECT dst_id FROM calls WHERE src_id LIKE '%Runner.Main()'")
            if "Runner." in r[0]}
    assert main == {"Run(Action<int>)", "Run(Action<int,int>)", "Wait(Limit)"}
    make = calls(con2, "%Runner.Make(int)")
    assert make == {"f2:csharp:cs/Mod::Mod.Runner.Make`1(int)"}              # Make<int>(...) is not recursion


def test_python_package_roots_reexports_and_fixtures(con2):
    p = "f2:python:py."
    assert (p + "tests.conftest.engine", p + "src.pkg.core.Engine.__init__") in {
        (r[0], r[1]) for r in con2.execute("SELECT src_id, dst_id FROM calls")}       # pkg.Engine through __init__
    assert calls(con2, p + "tests.test_engine.test_start") == {p + "tests.conftest.engine", p + "src.pkg.core.Engine.start"}
    assert p + "tests.conftest.child" in calls(con2, p + "tests.test_engine.test_child")
    assert calls(con2, p + "tests.test_engine.test_child.inner") == {p + "src.pkg.core.Engine.start"}   # typed by the fixture
    assert calls(con2, p + "tests.conftest.child") == {p + "tests.conftest.engine", p + "src.pkg.core.Engine.child"}
    assert calls(con2, p + "tests.test_engine.test_made") == {p + "src.pkg.core.make_engine"}
    assert calls(con2, p + "src.pkg.core.Engine.stop") == {p + "src.pkg.core.Base.stop"}               # super()
    flow = con2.execute("SELECT id FROM flows WHERE name LIKE '%test_start'").fetchone()
    steps = ids(con2, "SELECT callable_id FROM flow_steps WHERE flow_id = ?", flow[0])
    assert p + "src.pkg.core.Engine.__init__" in steps


def test_receivers_typed_by_what_a_call_returns(con2):
    go = {r[0].split("Mod.Fluent.")[1] for r in con2.execute(
        "SELECT dst_id FROM calls WHERE src_id LIKE '%Use.Go(Plan)' AND precision != 'guess'")}
    assert go == {"Plan.First()", "Step.Then()", "Step.Done()", "StepExtensions.Twice(Step)", "Plan.FirstAsync()"}
    p = "f2:python:py."
    assert calls(con2, p + "tests.test_engine.test_chain") == {
        p + "tests.conftest.engine", p + "src.pkg.core.Engine.child", p + "src.pkg.core.Engine.start",
        p + "src.pkg.core.Engine.stop"}


def test_patterns_are_found_by_shape(con2, con):
    from leyline import patterns

    r = patterns.listing(con2, limit=200)
    by = {}
    for p in r["patterns"]:
        by.setdefault(p["pattern"], []).append({role: sorted(n["name"] for n in ns) for role, ns in p["roles"].items()})
    assert by["decorator"] == [{"component": ["IPricer"], "decorator": ["TaxedPricer"], "wrapped": ["_inner"]}]
    assert by["composite"] == [{"component": ["IPricer"], "composite": ["SumPricer"], "children": ["_parts"]}]
    strategy = by["strategy"][0]
    assert strategy["context"] == ["Checkout"]                       # not the decorator or the composite
    assert set(strategy["implementation"]) == {"FlatPricer", "BulkPricer", "TaxedPricer", "SumPricer"}
    assert {"factory": ["For"], "product": ["BulkPricer", "FlatPricer"], "product type": ["IPricer"]} in by["factory"]
    assert {"template": ["Run"], "step": ["Step"], "base": ["Job"], "subclass": ["PrintJob"]} in by["template method"]
    assert by["singleton"] == [{"singleton": ["Clock"], "instance": ["Instance"]}]
    assert by["builder"][0]["builder"] == ["OrderBuilder"] and by["builder"][0]["build"] == ["Build"]
    assert all(p["rationale"] and 0 < p["confidence"] <= 1 for p in r["patterns"])

    first = patterns.listing(con)                                     # the first fixture: an event and a launched program
    assert {"observer", "process boundary"} <= set(first["by_pattern"])
    shown = query.expand(con2, "f2:csharp:cs/Mod::Mod.Shapes.TaxedPricer")["patterns"]
    assert {"decorator", "strategy"} == {p["pattern"] for p in shown}

    made = patterns.label(con2, "Adapter", {"adapter": ["f2:csharp:cs/Mod::Mod.Shapes.Checkout"], "x": ["nope"]},
                          "Checkout turns a quantity into a call on IPricer.", 0.4, "test")
    assert made["missing"] == ["nope"] and "error" in patterns.label(con2, "x", {"a": ["nope"]}, "why")
    assert "adapter" in patterns.listing(con2)["by_pattern"]
    patterns.run(con2)                                                # a fresh pass keeps labels written by others
    assert "adapter" in patterns.listing(con2)["by_pattern"]


def test_orientation_tour_and_authored_tours(con, con2):
    from leyline import export, tours

    listed = tours.listing(con)["tours"]
    assert listed[0]["id"] == "tour:orientation:fx" and listed[0]["stops"] >= 4
    t = tours.get(con, "tour:orientation:fx")
    assert t["stops"][0]["kind"] == "repo" and all(s["exists"] and s["narrative"] for s in t["stops"])
    kinds = {s["kind"] for s in t["stops"]}
    assert {"repo", "node", "pattern"} <= kinds
    assert "cannot see" in t["stops"][-1]["title"]

    assert "error" in tours.save(con2, "Empty", [{"ref": "nope", "narrative": "x"}])
    assert "error" in tours.save(con2, "No words", [{"ref": "f2:csharp:cs/Mod::Mod.Shapes.Checkout", "narrative": " "}])
    saved = tours.save(con2, "How a price is made", [
        {"title": "Start", "ref": "f2:csharp:cs/Mod::Mod.Shapes.Checkout", "narrative": "Checkout asks its pricer."},
        {"title": "Gone", "ref": "missing", "narrative": "skipped"},
        {"title": "The choice", "kind": "pattern",
         "ref": next(p["id"] for p in __import__("leyline").patterns.listing(con2)["patterns"] if p["pattern"] == "factory"),
         "narrative": "Pricers.For picks the implementation."}], audience="a new contributor")
    assert saved["stops"] == 2 and saved["missing"] == ["missing"]
    assert [s["title"] for s in tours.get(con2, saved["id"])["stops"]] == ["Start", "The choice"]
    data = export.graph(con2, with_sources=False)
    assert any(t["id"] == saved["id"] and len(t["stops"]) == 2 for t in data["tours"])
    assert data["patterns"] and all(isinstance(m["i"], int) for p in data["patterns"] for m in p["marks"])
