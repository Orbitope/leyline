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
    assert status["communicates:http"] == "ok" and status["communicates:queue"] == "not_analyzed"


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


def test_field_reads_and_writes(con2):
    from leyline import change

    s = "f2:csharp:cs/Mod::Mod.State."

    def access(fn):
        return {(r[0], r[1].split(".")[-1], bool(__import__("json").loads(r[2]).get("init"))) for r in con2.execute(
            "SELECT kind, dst_id, attrs FROM edges WHERE kind IN ('reads', 'writes') AND src_id = ?", (s + fn,))}
    assert access("Counter.Bump()") == {("reads", "Value", False), ("writes", "Value", False),
                                        ("reads", "_hits", False), ("writes", "_hits", False)}
    assert access("Counter.Full()") == {("reads", "Value", False), ("reads", "Limit", False)}
    assert access("Driver.Make()") == {("writes", "Limit", True), ("writes", "Value", False)}   # Limit only while creating
    assert access("Driver.Peek(Counter)") == {("reads", "Limit", False)}                        # the local Value hides nothing
    assert access("Driver.Reset(Counter)") == {("writes", "Value", False), ("reads", "Slots", False), ("writes", "Slots", False)}

    # Calling a method that changes a collection writes the field that holds it.
    assert access("Journal.Note(int)") == {("reads", "_items", False), ("writes", "_items", False),
                                           ("reads", "_by", False), ("writes", "_by", False)}
    assert access("Journal.Count()") == {("reads", "_items", False), ("reads", "_by", False)}
    pj = {(r[0], r[1].split(".")[-1], r[2].split(".")[-1]) for r in con2.execute(
        "SELECT kind, src_id, dst_id FROM edges WHERE kind IN ('reads', 'writes') AND dst_id LIKE 'f2:python:py.src.pkg.core.Journal.%'")}
    assert {("writes", "note", "items"), ("writes", "note", "by")} <= pj and ("writes", "count", "items") not in pj

    field = query.expand(con2, s + "Counter.Value")["data"]
    assert {x["name"] for x in field["written_by"]} == {"Bump", "Make", "Reset"} and field["written_outside_its_type"] == 2
    assert {f["name"]: (f["readers"], f["writers"]) for f in query.expand(con2, s + "Counter")["data"]["fields"]}["Limit"] == (2, 1)
    shared = {f["name"]: f for f in query.shared_state(con2)["fields"]}
    assert shared["Counter.Value"]["written_from"] == ["Driver"] and "Counter.Limit" not in shared

    p = "f2:python:py."
    py = {(r[0], r[1].rsplit(".", 2)[-2]) for r in con2.execute(
        "SELECT kind, src_id FROM edges WHERE kind IN ('reads', 'writes') AND dst_id = ?", (p + "src.pkg.core.Engine.name",))}
    assert {("writes", "Engine"), ("reads", "Engine"), ("writes", "test_engine"), ("reads", "test_engine")} <= py
    assert "Engine.name" not in {f["name"] for f in query.shared_state(con2)["fields"]}     # only a test assigns it from outside

    r = change.assess(con2, "Drop Counter.Value", [{"id": s + "Counter.Value", "action": "remove"}])
    assert {m["id"].split(".")[-1] for m in r["must_edit"]} >= {"Bump()", "Full()", "Make()", "Reset(Counter)"}


def test_compiler_overrules_syntax_for_csharp(tmp_path):
    import shutil

    if not shutil.which("dotnet"):
        pytest.skip("needs the .NET SDK")
    db = tmp_path / "x.db"
    plain = tmp_path / "plain.db"
    index(FIXTURE2, plain, "f2")
    stats = index(FIXTURE2, db, "f2", exact="roslyn")
    assert stats["exact:roslyn"]["status"] == "ok" and stats["exact:roslyn"]["calls_removed"] >= 1
    c, p = store.connect(db), store.connect(plain)
    go = "f2:csharp:cs/Mod::Mod.Hard.Go()"
    pick = lambda con: {r[0].split("Hard.")[1]: r[1] for r in con.execute(
        "SELECT dst_id, precision FROM calls WHERE src_id = ? AND dst_id LIKE '%Pick%'", (go,))}
    assert pick(p) == {"Pick(int)": "heuristic", "Pick(double)": "heuristic"}      # syntax cannot tell: h has no written type
    assert pick(c) == {"Pick(double)": "exact"}                                    # the compiler can
    assert c.execute("SELECT precision FROM edges WHERE kind = 'writes' AND src_id LIKE '%Counter.Bump()' LIMIT 1").fetchone()[0] == "exact"
    row = c.execute("SELECT status, stats FROM extractor_coverage WHERE extractor = 'exact:roslyn'").fetchone()
    assert row["status"] == "ok"
    # Flows are rebuilt from the corrected calls.
    assert c.execute("SELECT COUNT(*) FROM calls WHERE precision = 'exact'").fetchone()[0] > 20


def test_scip_index_confirms_and_adds_python_links(tmp_path):
    pb = pytest.importorskip("leyline.scip_pb2")

    core = (FIXTURE2 / "py/src/pkg/core.py").read_text().splitlines()
    line = lambda text: next(i for i, s in enumerate(core) if text in s)
    sym = "scip-python python f2 0 `src.pkg.core`/Engine#child()."
    idx = pb.Index()
    doc = idx.documents.add()
    doc.relative_path = "py/src/pkg/core.py"
    d = doc.occurrences.add()
    d.symbol, d.symbol_roles = sym, 1
    d.range.extend([line("def child"), 8, 13])
    mention = doc.occurrences.add()                                   # handler = thing.child : named, not called
    mention.symbol, mention.symbol_roles = sym, 8
    at = line("handler = thing.child")
    mention.range.extend([at, core[at].index("child"), core[at].index("child") + 5])
    call = doc.occurrences.add()                                      # return thing.child()
    call.symbol, call.symbol_roles = sym, 8
    at = line("return thing.child()")
    call.range.extend([at, core[at].index("child"), core[at].index("child") + 5])
    scip_file = tmp_path / "index.scip"
    scip_file.write_bytes(idx.SerializeToString())

    plain, db = tmp_path / "plain.db", tmp_path / "s.db"
    index(FIXTURE2, plain, "f2")
    stats = index(FIXTURE2, db, "f2", scip=[str(scip_file)])
    poke, child = "f2:python:py.src.pkg.core.poke", "f2:python:py.src.pkg.core.Engine.child"
    q = "SELECT precision, COUNT(*) FROM calls WHERE src_id = ? AND dst_id = ? GROUP BY precision"
    assert dict(store.connect(plain).execute(q, (poke, child)).fetchall()) == {"guess": 1}
    assert dict(store.connect(db).execute(q, (poke, child)).fetchall()) == {"exact": 1}     # confirmed once; the mention is not a call
    assert stats["exact:scip"]["status"] == "ok" and stats["exact:scip"]["calls_confirmed"] == 1


def test_grade_reads_typed_ranges_impl_methods_and_macros(tmp_path):
    """What rust-analyzer and scip-java write: typed single_line_range fields, `impl#[Type]method` symbols,
    an associated `new` called as Type::new(), and a macro called with its bang."""
    pb = pytest.importorskip("leyline.scip_pb2")
    from leyline.grade import grade

    root = Path(__file__).parent / "fixture4"
    idx = pb.Index()
    files = {"rs/src/geo.rs": [], "rs/src/main.rs": []}
    texts = {p: (root / p).read_text().splitlines() for p in files}

    def occ(path, text, word, sym, roles):
        row = next(i for i, s in enumerate(texts[path]) if text in s)
        col = texts[path][row].index(word, texts[path][row].index(text))
        files[path].append((sym, roles, row, col, col + len(word)))

    new, shout = "rust-analyzer cargo fx 0.1.0 geo/impl#[Circle]new().", "rust-analyzer cargo fx 0.1.0 geo/shout!"
    occ("rs/src/geo.rs", "pub fn new", "new", new, 1)
    occ("rs/src/geo.rs", "macro_rules! shout", "shout", shout, 1)
    occ("rs/src/main.rs", "let c = Circle::new", "new", new, 0)
    occ("rs/src/main.rs", "shout!(s)", "shout", shout, 0)
    for path, occs in files.items():
        doc = idx.documents.add()
        doc.relative_path = path
        for sym, roles, row, start, end in occs:
            o = doc.occurrences.add()
            o.symbol, o.symbol_roles = sym, roles
            o.single_line_range.line, o.single_line_range.start_character, o.single_line_range.end_character = row, start, end
    scip_file = tmp_path / "rs.scip"
    scip_file.write_bytes(idx.SerializeToString())
    g = grade(str(root), str(scip_file), db=str(tmp_path / "g.db"))
    assert g["compiler_links"] == 2 and g["recall"] == 1.0 and g["precision"] == 1.0


def test_measured_coverage_is_set_against_static_paths(tmp_path):
    import sqlite3

    from leyline import change, coverage, export, tours

    db = tmp_path / "c.db"
    index(FIXTURE2, db, "f2")
    c = store.connect(db)
    assert coverage.summary(c)["imported"] is False
    core = (FIXTURE2 / "py/src/pkg/core.py").read_text().splitlines()
    line = lambda text: next(i for i, s in enumerate(core) if text in s) + 1
    data = tmp_path / ".coverage"
    cov = sqlite3.connect(data)
    cov.executescript("CREATE TABLE file (id INTEGER PRIMARY KEY, path TEXT); CREATE TABLE context (id INTEGER PRIMARY KEY, context TEXT);"
                      "CREATE TABLE line_bits (file_id INTEGER, context_id INTEGER, numbits BLOB);"
                      "CREATE TABLE arc (file_id INTEGER, context_id INTEGER, fromno INTEGER, tono INTEGER);")
    cov.execute("INSERT INTO file VALUES (1, ?)", (str(FIXTURE2 / "py/src/pkg/core.py"),))
    cov.executemany("INSERT INTO context VALUES (?, ?)", [(1, ""), (2, "py/tests/test_engine.py::test_start|setup"),
                                                           (3, "py/tests/test_engine.py::test_start|run")])
    bits = lambda lines: bytes(sum(1 << (n % 8) for n in lines if n // 8 == i) for i in range(max(lines) // 8 + 1))
    # Loading the file runs every `def` line; that must not count as running the functions.
    cov.execute("INSERT INTO line_bits VALUES (1, 1, ?)", (bits([line("def start"), line("def child"), line("def stop")]),))
    cov.execute("INSERT INTO line_bits VALUES (1, 2, ?)", (bits([line("self.name = name")]),))           # the fixture built an Engine
    cov.execute("INSERT INTO arc VALUES (1, 3, ?, ?)", (line("return self.name"), -1))                    # the test ran start
    cov.execute("INSERT INTO arc VALUES (1, 3, ?, ?)", (line("return Engine(self.name)"), -1))            # and child, off the static path
    cov.commit()
    cov.close()

    r = coverage.import_file(c, data)
    assert r["format"] == "coverage.py" and r["tests"] == 1 and r["tests_matched_to_the_map"] == 1
    p = "f2:python:py.src.pkg.core."
    assert coverage.ran(c) == {p + "Engine.__init__", p + "Engine.start", p + "Engine.child"}
    assert coverage.tests_for(c, p + "Engine.child")[0]["id"] == "f2:python:py.tests.test_engine.test_start"
    flow = c.execute("SELECT id FROM flows WHERE name LIKE '%test_engine.test_start'").fetchone()[0]
    cmp = coverage.compare_flow(c, flow)
    assert cmp["measured"] and [x["name"] for x in cmp["ran_but_not_on_path"]] == ["child"] and cmp["on_path_but_did_not_run"] == []
    mods = {m["module"]: m for m in coverage.summary(c)["modules"]}
    assert sum(m["ran"] for m in mods.values()) == 3 and sum(m["ran_off_every_path"] for m in mods.values()) >= 0

    # A change to child: no static path from test_start reaches it, the measurement does.
    a = change.assess(c, "child returns a copy", [{"id": p + "Engine.child", "action": "behavior"}])
    hit = [t for t in a["tests_to_run"] if t["name"].endswith("test_start")]
    assert hit and hit[0].get("measured")
    assert "Coverage was measured" in " ".join(s["narrative"] for s in tours.get(c, "tour:orientation:f2")["stops"])
    assert export.graph(c, with_sources=False)["measured"]["any"]

    index(FIXTURE2, db, "f2")                                              # a re-index keeps the import
    c = store.connect(db)
    assert coverage.has(c) and c.execute("SELECT status FROM extractor_coverage WHERE extractor = 'coverage'").fetchone()[0] == "ok"

    xml = tmp_path / "cobertura.xml"
    xml.write_text(f"""<?xml version="1.0"?><coverage><sources><source>{FIXTURE2}</source></sources><packages><package><classes>
      <class filename="py/src/pkg/core.py"><lines><line number="{line('return self.name')}" hits="3"/>
      <line number="{line('return Engine("made")')}" hits="0"/></lines></class></classes></package></packages></coverage>""")
    r = coverage.import_file(c, xml, run="xml")
    assert r["format"] == "cobertura" and r["functions_ran"] == 1 and not r["per_test"]


def test_http_and_file_channels(con2):
    import json

    links = {(r[0].split(":")[-1].split(".")[-1], r[1].split(":")[-1].split(".")[-1].split("(")[0], json.loads(r[3])["channel"]):
             (r[2], json.loads(r[3])["address"]) for r in con2.execute(
                 "SELECT src_id, dst_id, precision, attrs FROM edges WHERE kind = 'communicates'")}
    assert links[("fetch", "show", "http")] == ("heuristic", "GET /items/<int:item_id>")        # the request finds its route
    assert not any(k[0] == "other" for k in links)                                              # no route, no link
    assert links[("save", "Load", "file")] == ("guess", "reports/*.parity.json")                # Python writes what C# reads
    stats = {r[0]: json.loads(r[1]) for r in con2.execute(
        "SELECT extractor, stats FROM extractor_coverage WHERE extractor LIKE 'communicates:%' AND status = 'ok'")}
    assert stats["communicates:http"]["routes"] == 1 and stats["communicates:http"]["requests"] == 2
    # A file link is data, not control: no flow walks from the writer into the reader.
    assert not con2.execute("SELECT 1 FROM flow_steps WHERE via = 'file'").fetchone()


def test_spec_loop_from_openspec_folder_to_verified_change(tmp_path):
    import shutil

    from leyline import diff, spec

    work = tmp_path / "repo"
    shutil.copytree(FIXTURE2, work)
    ch = work / "openspec" / "changes" / "loud-engine"
    (ch / "specs" / "engine").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Loud engine\n\n## Why\nNames are hard to read in logs.\n\n"
                                    "## What Changes\n- `Engine.start` returns the name in upper case\n- A new `Engine.shout`\n")
    (ch / "tasks.md").write_text("## 1. Engine\n- [ ] 1.1 Change `Engine.start` to return the name in upper case\n"
                                 "- [ ] 1.2 Add `Engine.shout`, the name with an exclamation mark\n- [ ] 1.3 Tidy things up\n")
    (ch / "specs" / "engine" / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Loud names\nThe engine SHALL report its name loudly.\n\n"
        "#### Scenario: Start\n- **WHEN** an engine starts\n- **THEN** it returns its name\n\n"
        "#### Scenario: Shout\n- **WHEN** an engine shouts\n- **THEN** the name ends with an exclamation mark\n")
    db = tmp_path / "s.db"
    index(work, db, "f2")
    c = store.connect(db)

    b = spec.brief(c, ch)
    tasks = {t["key"]: t for t in b["tasks"]}
    assert tasks["1.1"]["labels"] == ["Engine.start"] and tasks["1.1"]["action"] == "behavior"
    assert tasks["1.2"]["new"] == [{"name": "shout", "parent": "f2:python:py.src.pkg.core.Engine", "label": "Engine.shout"}]
    assert {s["name"]: s["test_exists"] for s in b["scenarios"]} == {"Start": True, "Shout": False}
    assert any("names no code" in g for g in b["gaps"]) and any("Shout" in g for g in b["gaps"]) and not b["ready"]
    page = (ch / "leyline.md").read_text()
    assert "## 1. What code will be written" in page and "## 3. How you will know it was done" in page and "Engine.start" in page
    assert (tmp_path / "snapshots" / "spec-loud-engine.db").exists()

    assert b["baseline"] == "new"
    # Engine.child uses the field Engine.start uses and no task names it: the place a reviewer should look first.
    assert [(x["field"], x["also_used_by_unchanged"]) for x in b["left_alone"]["state"]] == [("Engine.name", ["Engine.child"])]
    assert "Engine.name (used by Engine.start) is also used by Engine.child" in page

    facts = spec.review_facts(c, ch)
    assert facts["logic"]["scenarios_with_no_test"] == ["Shout"]
    assert facts["performance"]["changed_functions_by_how_much_runs_through_them"][0]["name"] == "Engine.start"
    assert "error" in spec.add_finding(c, b["change_id"], "logic", "high", "No evidence", [])
    f = spec.add_finding(c, b["change_id"], "logic", "high", "Engine.child copies the name and will not be upper case.",
                         ["f2:python:py.src.pkg.core.Engine.child"], "Add a scenario for child.")
    assert spec.findings(c, b["change_id"])["open"] == 1 and "open, high" in spec.brief_text(spec.brief(c, ch))
    spec.resolve_finding(c, f["id"], "rejected", "child is out of scope")
    assert spec.findings(c, b["change_id"])["open"] == 0

    # Implement: both tasks, a test for the new scenario, and one edit nobody asked for.
    core = work / "py/src/pkg/core.py"
    text = core.read_text().replace("        return self.name\n", "        return self._loud()\n\n    def _loud(self):\n        return self.name.upper()\n\n    def shout(self):\n        return self.name + \"!\"\n", 1)
    core.write_text(text.replace("        return Engine(self.name)", "        return Engine(self.name + \"-child\")"))
    tests = work / "py/tests/test_engine.py"
    tests.write_text(tests.read_text() + "\n\ndef test_shout(engine):\n    assert engine.shout().endswith(\"!\")\n")
    (ch / "tasks.md").write_text((ch / "tasks.md").read_text().replace("- [ ] 1.1", "- [x] 1.1"))
    index(work, db, "f2")
    c = store.connect(db)
    diff.record_tests(c, "after", [{"name": "test_start", "status": "pass"}, {"name": "test_shout", "status": "pass"}])

    v = spec.verify(c, ch, after_run="after")
    done = {t["key"]: t["state"] for t in v["tasks"]}
    assert done["1.1"] == "done" and done["1.2"] == "done" and done["1.3"] == "cannot be checked"
    assert {s["name"]: s["state"] for s in v["scenarios"]} == {"Start": "passes", "Shout": "passes"}
    assert all(s["reaches_the_change"] for s in v["scenarios"])
    assert [n["name"] for n in v["drift"]] == ["Engine.child"]                       # the edit outside the spec
    assert not v["done_as_agreed"] and any("outside the spec" in w for w in v["why_not"])
    page = (ch / "leyline.md").read_text()
    assert "## 4. Was it done as agreed" in page and page.count("## 1. What code will be written") == 1
    assert "Engine.child" in page.split("## 4.")[1]
    assert [h["name"] for h in v["helpers_added"]] == ["Engine._loud"] and "Helpers added" in page   # not an edit outside the spec

    # Amend the spec after the code has changed: the first picture of the code is kept, so verify still compares with it.
    (ch / "tasks.md").write_text((ch / "tasks.md").read_text() + "- [ ] 1.4 Change `Engine.child` to mark the child's name\n")
    b2 = spec.brief(c, ch)
    assert b2["baseline"] == "kept"
    v2 = spec.verify(c, ch, after_run="after")
    assert v2["drift"] == [] and {t["key"]: t["state"] for t in v2["tasks"]}["1.4"] == "done"
    assert {t["key"]: t["state"] for t in v2["tasks"]}["1.2"] == "done"            # still seen as added, not as there all along
    assert spec.brief(c, ch, new_baseline=True)["baseline"] == "new"


def test_typescript(tmp_path):
    """A third language: workspace packages, re-exports, JSX, functions handed over by name, inline tests."""
    db = tmp_path / "ts.db"
    stats = index(Path(__file__).parent / "fixture3", db, "f3")
    c = store.connect(db)
    t = "f3:typescript:pkg."
    assert stats["tree-sitter-typescript"]["calls_resolved"] + stats["tree-sitter-typescript"]["calls_external"] == \
        stats["tree-sitter-typescript"]["calls_total"]                      # nothing left unresolved
    assert {r[0] for r in c.execute("SELECT id FROM nodes WHERE kind = 'module'")} == {"f3:module:pkg/app", "f3:module:pkg/core"}
    calls = {(r[0][len(t):], r[1][len(t):], r[2]) for r in c.execute("SELECT src_id, dst_id, dispatch FROM calls")}
    # @fx/core -> src/index.ts -> `export *` from graph.ts; `makeId as newId` re-exported under another name
    assert ("app.src.main.handlePick", "core.src.graph.build", "static") in calls
    assert ("app.src.main.handlePick", "core.src.ids.makeId", "static") in calls
    # typed by what build() returns; an object of functions is a type; a namespace import
    assert ("app.src.main.handlePick", "core.src.graph.Graph.touch", "static") in calls
    assert ("app.src.main.handlePick", "core.src.ids.store.save", "static") in calls
    assert ("app.src.main.Panel.refresh", "core.src.graph.build", "static") in calls
    # JSX renders a component; a handler passed by name is a reference
    assert ("app.src.main.Panel", "app.src.main.Row", "static") in calls
    assert ("app.src.main.Panel", "app.src.main.handlePick", "reference") in calls
    assert ("app.src.main.Panel", "app.src.main.Panel.refresh", "reference") in calls
    # a default import, constructed, then called
    assert ("app.src.main.main", "core.src.index.Registry.register", "static") in calls
    edges = {(r[0], r[1].split(":", 2)[-1], r[2].split(":", 2)[-1]) for r in c.execute("SELECT kind, src_id, dst_id FROM edges")}
    assert ("instantiates", "pkg.app.src.main.main", "pkg.core.src.index.Registry") in edges
    assert ("extends", "pkg.core.src.graph.Link", "pkg.core.src.graph.Stamped") in edges          # type X = A & { ... }
    assert ("reads", "pkg.core.src.graph.describeLink", "pkg.core.src.graph.Stamped.at") in edges  # a field of the base
    assert ("writes", "pkg.core.src.graph.Graph.add", "pkg.core.src.graph.Graph.items") in edges   # this.items.push(x)
    assert ("writes", "pkg.core.src.graph.Graph.rename", "pkg.core.src.graph.Item.label") in edges
    assert ("communicates", "pkg.app.src.main.main", "pkg.app.src.server.routes") in edges        # fetch -> app.post
    assert ("imports", "pkg/app/src/main.tsx", "npm:react") in edges
    tests = {r[0]: r[1] for r in c.execute("SELECT name, id FROM nodes WHERE kind = 'test'")}
    assert set(tests) == {"adds a node", "counts %d"}
    assert (tests["adds a node"][len(t):], "core.src.graph.Graph.add", "static") in calls
    flows = {r[0] for r in c.execute("SELECT entry_id FROM flows")}
    assert tests["adds a node"] in flows and "f3:typescript:pkg.app.src.main.<module>" in flows   # main() at the top of a file
    c.close()


def test_generic_languages(tmp_path):
    """Go, Rust and Java through the one generic adapter: no code in Leyline knows these languages."""
    db = tmp_path / "g.db"
    stats = index(Path(__file__).parent / "fixture4", db, "f4")
    assert {"generic-go", "generic-rust", "generic-java"} <= set(stats)
    c = store.connect(db)
    calls = {(r[0].split(":", 2)[2], r[1].split(":", 2)[2]) for r in c.execute("SELECT src_id, dst_id FROM calls")}
    # Go: a package-qualified call, a method on a typed parameter, a method declared outside its struct
    assert ("go.main.main", "go.shapes.shape.NewSquare") in calls
    assert ("go.main.report", "go.shapes.shape.Square.Area") in calls
    assert ("go.main.main", "go.main.report") in calls
    # Rust: Type::new(), a method through a local's constructor type, a macro is not a function
    assert ("rs.src.main.main", "rs.src.geo.Circle.new") in calls
    assert ("rs.src.main.main", "rs.src.geo.Circle.area") in calls
    assert ("rs.src.main.main", "rs.src.geo.shout!") in calls
    assert ("rs.src.main.main", "rs.src.geo.format") not in calls          # format!() is the standard macro
    # Java: overloaded constructors told apart by argument count; methods on a declared local
    assert ("java.com.acme.App.App.main", "java.com.acme.Counter.Counter.Counter~2") in calls
    assert ("java.com.acme.App.App.main", "java.com.acme.Counter.Counter.add") in calls
    assert ("java.com.acme.App.App.main", "java.com.acme.Counter.Counter.get") in calls
    # Go: a call in a struct literal's value is not made on the key; a method's bare call is never on its receiver
    assert ("go.main.main", "go.main.validate") in calls
    assert ("go.shapes.shape.Square.Scale", "go.shapes.shape.Scale") in calls
    # Rust: calls inside a macro's arguments; the type's own method before one from `impl Trait for`;
    # a module-qualified call reaches the module's function, not a method of that name
    assert ("rs.src.main.area_is_positive", "rs.src.geo.Circle.new") in calls
    assert ("rs.src.main.area_is_positive", "rs.src.geo.Circle.area") in calls
    assert ("rs.src.main.area_is_positive", "rs.src.geo.Circle.area~2") not in calls
    assert ("rs.src.main.measure", "rs.src.geo.Circle.area") in calls          # c: &geo::Circle
    assert ("rs.src.main.measure", "rs.src.geo.Circle.area~2") not in calls
    assert ("rs.src.main.main", "rs.src.geo.describe") in calls
    assert ("rs.src.main.main", "rs.src.geo.Circle.describe") not in calls
    # Java: overloads told apart by argument types; a call on what another call returns; an anonymous
    # class's method is not one of the outer type's
    assert ("java.com.acme.App.App.main", "java.com.acme.Counter.Counter.add~2") not in calls
    assert ("java.com.acme.App.App.words", "java.com.acme.Counter.Counter.add~2") in calls     # add(String)
    assert ("java.com.acme.App.App.words", "java.com.acme.Counter.Counter.add~3") in calls     # add(Counter)
    assert ("java.com.acme.App.App.words", "java.com.acme.Counter.Counter.add") not in calls
    assert ("java.com.acme.App.App.chained", "java.com.acme.Counter.Counter.plus") in calls
    assert ("java.com.acme.App.App.chained", "java.com.acme.Counter.Counter.get") in calls
    assert ("java.com.acme.App.App.again", "java.com.acme.Counter.Counter.run~2") in calls
    assert ("java.com.acme.App.App.again", "java.com.acme.Counter.Counter.run") not in calls
    tests ={r[0] for r in c.execute("SELECT name FROM nodes WHERE kind = 'test' OR json_extract(attrs, '$.is_test') = 1")}
    assert {"TestReport", "prints the area", "area_is_positive"} <= tests
    entries = {r[0] for r in c.execute("SELECT path FROM nodes WHERE kind = 'entry_point'")}
    assert {"go/main.go", "rs/src/main.rs", "java/com/acme/App.java"} <= entries
    c.close()


def _facts(db):
    c = store.connect(db)
    out = {name: sorted(map(tuple, c.execute(sql))) for name, sql in (
        ("nodes", "SELECT id, kind, parent_id, content_hash FROM nodes"),
        ("calls", "SELECT src_id, dst_id, dispatch, precision, site_start FROM calls"),
        ("edges", "SELECT kind, src_id, dst_id, precision, COALESCE(attrs, '') FROM edges"),
        ("steps", "SELECT flow_id, seq, depth, callable_id, via, COALESCE(site_line, -1), COALESCE(parent_seq, -1)"
                  " FROM flow_steps"))}
    c.close()
    return out


def test_parse_workers_do_not_change_the_index(tmp_path, monkeypatch):
    """Files parsed across processes give the same index as files parsed in order, in one process."""
    from leyline import indexer
    monkeypatch.setattr(indexer, "PARALLEL_MIN_FILES", 0)
    for fx in ("fixture", "fixture2", "fixture3", "fixture4"):
        got = []
        for jobs in ("1", "2"):
            monkeypatch.setenv("LEYLINE_JOBS", jobs)
            db = tmp_path / f"{fx}-{jobs}.db"
            index(Path(__file__).parent / fx, db, "fx")
            got.append(_facts(db))
        assert got[0] == got[1], fx
        assert got[0]["calls"] and got[0]["steps"]


def test_older_store_is_moved_to_keyed_tables_on_open(tmp_path):
    """A store whose calls and flow steps are plain tables is moved to the keyed tables when opened, and the
    move holds even when whoever opened it only reads."""
    import sqlite3
    db = tmp_path / "old.db"
    index(Path(__file__).parent / "fixture4", db, "f4")
    want = _facts(db)
    raw = sqlite3.connect(db)
    raw.executescript("""
        CREATE TABLE old_calls AS SELECT * FROM calls; DROP VIEW calls; ALTER TABLE old_calls RENAME TO calls;
        CREATE TABLE old_steps AS SELECT * FROM flow_steps; DROP VIEW flow_steps;
        ALTER TABLE old_steps RENAME TO flow_steps;
        DELETE FROM call_sites; DELETE FROM steps; DELETE FROM keys;""")
    raw.close()
    c = store.connect(db)
    c.execute("SELECT COUNT(*) FROM calls").fetchone()
    c.close()   # read only: nothing committed by the caller
    raw = sqlite3.connect(db)
    kinds = dict(raw.execute("SELECT name, type FROM sqlite_master WHERE name IN ('calls', 'flow_steps')").fetchall())
    raw.close()
    assert kinds == {"calls": "view", "flow_steps": "view"}
    assert _facts(db) == want
