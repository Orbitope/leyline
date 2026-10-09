"""Property tests for invariant scenarios, renames in spec drift, and removals checked by what is left.

The runner outputs in tests/fixture_props are real, captured from failing property tests: Hypothesis 6.168.5 and
6.100.0 under pytest -rA, and fast-check 3.23.2 under vitest 1.6.1 --reporter=tap (from a copy of Parlance's
editor). FsCheck could not be fetched here (nuget.org is not reachable), so its file is written to the format in
FsCheck's Runner.fs (`onFailureToString`: "Falsifiable, after N tests (M shrinks) ...", "Original:", "Shrunk:",
one argument per line), inside `dotnet test` console output.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from leyline import diff, props, removal, spec, store, verdicts
from leyline.cli import main
from leyline.indexer import index

from test_drift import done, edit, run   # noqa: F401  (`done`: fixture2 with the loud-engine change checked)

HERE = Path(__file__).parent
OUT = HERE / "fixture_props"
FIXTURE2 = HERE / "fixture2"


# -- A. property tests -------------------------------------------------------------------------------
@pytest.mark.parametrize("name, then, yes", [
    ("Balance is never negative", [], True),
    ("Merge keeps items", ["for any two lists, every item of each is in the result"], True),
    ("Sorted", ["the keys are always sorted"], True),
    ("Start", ["it returns its name in upper case"], False),
    ("Each request is logged", ["each request writes one line"], False),
])
def test_invariant_scenarios_are_found_by_their_words(name, then, yes):
    assert props.invariant({"name": name, "when": [], "then": then}) is yes


@pytest.mark.parametrize("paths, lib", [(["a/b.py"], "Hypothesis"), (["src/x.ts", "src/y.tsx"], "fast-check"),
                                        (["Mod/A.cs"], "FsCheck"), (["main.go"], None)])
def test_the_library_follows_the_language(paths, lib):
    assert props.LIBRARIES.get(props._language(paths) or "") == lib


@pytest.mark.parametrize("fixture", ["hypothesis_6.168.txt", "hypothesis_6.100.txt"])
def test_hypothesis_counterexamples_from_real_pytest_output(fixture):
    got = {r["name"]: r for r in diff.parse_test_output((OUT / fixture).read_text())}
    assert got["tests/test_ledger.py::test_balance_is_never_negative"]["message"] == (
        "fails for balance=0, amount=-1 (Hypothesis).\nassert -1 >= 0")
    # the generator's comment and `self` are left out; the class's test is found by its function's name
    assert props.from_message(got["tests/test_ledger.py::test_merge_keeps_every_item"]["message"]) == "a=[], b=[0, 0]"
    assert props.from_message(got["tests/test_ledger.py::TestOrder::test_merge_is_sorted"]["message"]) == "a=[0, 0]"
    assert got["tests/test_ledger.py::test_plain_example"]["status"] == "pass"


def test_hypothesis_counterexample_with_an_escaped_quote_in_a_string():
    # repr() of a string holding both quotes escapes one: s='a\'b"c'. The escaped quote does not close the string.
    text = "Falsifying example: test_quote(\n    s='a\\'b\"c',\n    n=1,\n)\n"
    assert [c["example"] for c in props.counterexamples(text)] == ["s='a\\'b\"c', n=1"]


def test_fast_check_counterexamples_from_real_vitest_tap():
    got = {r["name"].split(" > ")[-1]: r for r in diff.parse_test_output((OUT / "fastcheck_vitest_tap.txt").read_text())}
    assert got["Balance is never negative"]["message"].startswith("fails for 0, -1 (fast-check).\nProperty failed after 1 tests")
    assert props.from_message(got["Serialized keys are always sorted"]["message"]) == '{"":0}'   # TAP's escapes undone
    assert got["Plain example"]["status"] == "pass"
    ces = props.counterexamples((OUT / "fastcheck_vitest_tap.txt").read_text())
    assert [c["tests"] for c in ces] == [1, 1]


def test_fscheck_counterexamples_shrunk_and_original():
    text = (OUT / "fscheck_dotnet_test.txt").read_text()
    ces = props.counterexamples(text)
    assert [(c["example"], c["tests"], c["shrinks"]) for c in ces] == [("0, -1", 4, 3), ('(3, "")', 1, 0)]
    # dotnet's lines are not read as results; results passed whole are found by the line that names them
    results = [{"name": "PropDemo.LedgerProperties.Transfer_keeps_the_total", "status": "fail"},
               {"name": "PropDemo.LedgerProperties.Balance_is_never_negative", "status": "fail"}]
    props.annotate(results, text)
    assert [props.from_message(r["message"]) for r in results] == ['(3, "")', "0, -1"]
    # ... or by the counterexample in their own message, as an agent passes them to `record_test_run`
    one = props.annotate([{"name": "x", "status": "fail", "message": text.split("Error Message:")[1]}])[0]
    assert one["message"].startswith("fails for 0, -1 (FsCheck).")
    assert props.annotate(one and [dict(one)])[0]["message"] == one["message"]   # once only


def test_a_counterexample_is_the_verdict_s_reason():
    fail = {"status": "fail", "message": "fails for amount=-1 (Hypothesis).\nassert -1 >= 0"}
    s = {"state": "fails", "test": "t", "reaches_the_change": True, "measured_running_the_change": None}
    assert verdicts._scenario_verdict(s, [fail], None) == ("contradicted", "fails for amount=-1")
    assert verdicts._scenario_verdict(s, [fail], [{"status": "pass"}])[1] == (
        "fails for amount=-1; its test passed before the change")
    assert "one fails for amount=-1" in verdicts._scenario_verdict(s, [fail, {"status": "pass"}], None)[1]


TASKS = ("- [ ] 1.1 Change `Engine.start` to return the name in upper case\n"
         "- [ ] 1.2 Add `Engine.shout`, the name with an exclamation mark\n"
         "- [ ] 1.3 Add the test \"Shout\"\n")
SPEC = ("## ADDED Requirements\n### Requirement: Loud names\nThe engine SHALL report its name loudly.\n\n"
        "#### Scenario: Start\n- **WHEN** an engine starts\n- **THEN** it returns its name in upper case\n\n"
        "#### Scenario: Shout\n- **WHEN** an engine shouts, whatever its name\n"
        "- **THEN** the name always ends with an exclamation mark\n")


def _change(tmp_path, tasks=TASKS, spec_text=SPEC, before=None):
    work = tmp_path / "repo"
    shutil.copytree(FIXTURE2, work)
    ch = work / "openspec" / "changes" / "c"
    (ch / "specs" / "engine").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: C\n\n## Why\nBecause.\n")
    (ch / "tasks.md").write_text(tasks)
    (ch / "specs" / "engine" / "spec.md").write_text(spec_text)
    db = tmp_path / "s.db"
    index(work, db, "f2")
    c = store.connect(db)
    diff.record_tests(c, "before", before or [{"name": n, "status": "pass"} for n in ("test_start", "test_made", "test_chain")])
    b = spec.brief(c, ch)
    c.close()
    return work, ch, db, b


def _verify(work, db, ch, after):
    index(work, db, "f2")
    c = store.connect(db)
    try:
        with c:
            c.execute("DELETE FROM test_results WHERE run = 'after'")
        diff.record_tests(c, "after", after)
        return spec.verify(c, ch, before_run="before", after_run="after")
    finally:
        c.close()


def test_plan_marks_invariants_and_check_shows_the_counterexample(tmp_path):
    work, ch, db, b = _change(tmp_path)
    sc = {s["name"]: s for s in b["scenarios"]}
    assert sc["Shout"]["invariant"] and sc["Shout"]["property_library"] == "Hypothesis" and "invariant" not in sc["Start"]
    page = (ch / "leyline.md").read_text()
    assert "| Shout |" in page and "to be written, with this name; invariant: a property test fits (Hypothesis) |" in page
    assert "| Start |" in page and page.count("invariant: a property test fits") == 1

    core = work / "py/src/pkg/core.py"
    edit(core, "        return self.name\n", "        return self.name.upper()\n\n    def shout(self):\n        return self.name\n")
    t = work / "py/tests/test_engine.py"
    t.write_text(t.read_text() + "\n\ndef test_shout(engine):\n    assert engine.shout().endswith(\"!\")\n")
    # A Hypothesis failure as the runner prints it, results read from the text (what `check --tests` does).
    text = ("_____ test_shout _____\n\n    def test_shout(engine, name):\n>       assert engine.shout().endswith(\"!\")\n"
            "E       AssertionError: assert False\nE       Falsifying example: test_shout(\nE           engine=<pkg.core.Engine>,\n"
            "E           name='',\nE       )\n\ntests/test_engine.py:31: AssertionError\n"
            "PASSED tests/test_engine.py::test_start\nPASSED tests/test_engine.py::test_made\n"
            "PASSED tests/test_engine.py::test_chain\nFAILED tests/test_engine.py::test_shout - AssertionError: assert False\n")
    v = _verify(work, db, ch, diff.parse_test_output(text))
    s = {x["name"]: x for x in v["scenarios"]}["Shout"]
    assert s["verdict"] == "contradicted" and s["verdict_why"] == "fails for engine=<pkg.core.Engine>, name=''"
    assert s["counterexample"] == "engine=<pkg.core.Engine>, name=''"
    page = (ch / "leyline.md").read_text()
    assert "| Shout | fails | contradicted | fails for engine=<pkg.core.Engine>, name='' (Hypothesis). AssertionError" in page
    assert "- scenario \"Shout\", contradicted: fails for engine=<pkg.core.Engine>, name=''" in page
    json.dumps(v)


# -- B. renames in spec drift --------------------------------------------------------------------------
def test_a_renamed_method_is_said_renamed_not_gone(done, capsys):   # noqa: F811
    core = done / "py/src/pkg/core.py"
    edit(core, "    def shout(self):", "    def yell(self):")
    edit(core, "\n    def count(self):\n        return len(self.items) + len(self.by)\n", "\n")   # deleted, not renamed
    code, out = run(capsys, "drift")
    assert code == 1, out
    change = out.split("## Change loud-engine")[1]
    assert ("- `Engine.shout` has been renamed to `Engine.yell` in py/src/pkg/core.py: nothing answers to the old name"
            " now, and `Engine.yell` has the same body.") in change
    # Journal.count has siblings with its declaration (`def (self)`), none of them new: it is gone, not renamed.
    assert "- `Journal.count` is gone" in change
    assert "2 names no longer match the code: 1 is gone and 1 was renamed." in out

    # A plan that touches the new name hears that the spec names the old one.
    ch = done / "openspec/changes/yell-twice"
    ch.mkdir()
    (ch / "proposal.md").write_text("# Change: Yell twice\n\n## Why\nLouder.\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `Engine.yell` to yell twice\n")
    main(["plan", "yell-twice"])
    capsys.readouterr()
    assert ("The change loud-engine names `Engine.shout`, which has been renamed to `Engine.yell`."
            in (ch / "leyline.md").read_text())


def _git(work, *args):
    subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(work), "PATH": "/usr/bin:/bin:/usr/local/bin"})


def test_git_says_what_is_new_and_where_a_file_went(done, capsys):   # noqa: F811
    living = done / "openspec/specs/engine/spec.md"
    living.write_text(living.read_text() + "The web client is in `py/web/client.py`.\n")
    assert run(capsys, "drift", "--accept")[0] == 0
    _git(done, "init", "-q")
    _git(done, "add", "-A")
    _git(done, "commit", "-qm", "base")
    # Renamed, and its body changed too: only the declaration (`def ()`) is left to go by, and git says the new name
    # was not in the file while the old one was.
    edit(done / "py/src/pkg/core.py", 'def make_engine():\n    return Engine("made")',
         'def build_engine():\n    return Engine("built")')
    _git(done, "mv", "py/web/client.py", "py/web/web_client.py")
    code, out = run(capsys, "drift")
    assert code == 1, out
    assert ("`core.make_engine` has been renamed to `build_engine` in py/src/pkg/core.py: nothing answers to the old"
            " name now, and `build_engine` has the same declaration.") in out
    assert "`py/web/client.py` has been renamed to `py/web/web_client.py`: git records the file as renamed." in out
    # Committed, the same holds: git finds the commit that took the old name out.
    _git(done, "commit", "-qam", "rename")
    code, out = run(capsys, "drift")
    assert "`core.make_engine` has been renamed to `build_engine`" in out and "`py/web/web_client.py`" in out
    # A function that was there all along is not a rename, however alike: `poke(thing)` is not `make_engine()`.
    edit(done / "py/src/pkg/core.py", 'def build_engine():\n    return Engine("built")\n', "")
    code, out = run(capsys, "drift")
    assert "`core.make_engine` is gone" in out


# -- C. removals and renames checked by what is left ----------------------------------------------------
@pytest.mark.parametrize("text, got", [
    ("Remove `make_engine`", ("remove", ["make_engine"], None)),
    ("Delete the unused helper `core.make_engine` and `poke`", ("remove", ["core.make_engine", "poke"], None)),
    ("Remove `make_engine` and add `build_engine` in `core.py`", ("remove", ["make_engine"], None)),
    ("Rename `Engine.child` to `Engine.copy`", ("rename", ["Engine.child"], "Engine.copy")),
    ("Remove a parameter from `Engine.start`", None),
    ("Remove the call to `poke` from `Engine.child`", None),
    ("Remove `Engine.start`'s parameter", None),
    ("Change `Engine.start` to return the name", None),
])
def test_which_tasks_are_removals(text, got):
    assert removal._targets(text) == got


def test_callers_are_named_code_first_and_tests_counted():
    assert removal._who([("parse", "calls", False)]) == "parse still calls it"
    assert removal._who([("documentXml", "calls", False), ("OfferChanges", "calls", False), ("says a", "calls", True),
                         ("says b", "calls", True)]) == "documentXml, OfferChanges and 2 tests still call it"
    assert removal._who([(f"f{k}", "uses", False) for k in range(5)]) == "f0, f1, f2 and 2 more still use it"


def test_remove_is_done_when_gone_and_no_longer_called(tmp_path):
    work, ch, db, _ = _change(tmp_path, "- [ ] 1.1 Remove `make_engine`\n", "")
    core, tests, init = work / "py/src/pkg/core.py", work / "py/tests/test_engine.py", work / "py/src/pkg/__init__.py"
    after = [{"name": n, "status": "pass"} for n in ("test_start", "test_chain")]
    edit(core, 'return Engine("made")', 'return Engine("built")')   # edited, not removed
    t = _verify(work, db, ch, after)["tasks"][0]
    assert (t["state"], t["verdict"], t["verdict_why"]) == (
        "not done", "contradicted", "`make_engine` still exists; it was edited, not removed")

    edit(core, 'def make_engine():\n    return Engine("built")\n', "")   # gone, and the test still calls it
    v = _verify(work, db, ch, after)
    t = v["tasks"][0]
    assert t["verdict"] == "partial" and t["state"] == "partly"
    assert t["verdict_why"].startswith("`make_engine` is gone but ") and "test_made" in t["verdict_why"]
    assert "still call" in t["verdict_why"] and "some tasks are not done" in v["why_not"]

    edit(tests, 'from pkg import make_engine\n', "")
    edit(tests, 'def test_made():\n    assert make_engine().name == "made"\n', "")
    edit(init, "from .core import make_engine\n", "")
    t = _verify(work, db, ch, after)["tasks"][0]
    assert (t["state"], t["verdict"], t["verdict_why"]) == ("done", "proven", "`make_engine` is gone, and nothing still calls it")


def test_rename_needs_the_old_name_gone_the_new_there_and_its_callers_moved(tmp_path):
    work, ch, db, b = _change(tmp_path, "- [ ] 1.1 Rename `Engine.child` to `Engine.copy`\n", "")
    core, conf, tests = work / "py/src/pkg/core.py", work / "py/tests/conftest.py", work / "py/tests/test_engine.py"
    after = [{"name": n, "status": "pass"} for n in ("test_start", "test_chain")]
    edit(core, "    def child(self)", "    def copy(self)")
    v = _verify(work, db, ch, after)
    t = v["tasks"][0]
    assert not any(n["name"].endswith("copy") for n in v["drift"])   # the new name is the task's, not an edit of its own
    assert t["verdict"] == "partial" and t["verdict_why"].startswith("`Engine.child` is gone but ")
    assert "poke" in t["verdict_why"] and "still call it" in t["verdict_why"]

    edit(core, "    handler = thing.child\n    return thing.child()", "    handler = thing.copy\n    return thing.copy()")
    conf.write_text(conf.read_text().replace("engine.child()", "engine.copy()"))
    tests.write_text(tests.read_text().replace(".child()", ".copy()"))
    t = _verify(work, db, ch, after)["tasks"][0]
    assert (t["verdict"], t["verdict_why"]) == ("proven", "`Engine.child` is gone and `Engine.copy` is there, and nothing still calls it")

    edit(core, '    def copy(self) -> "Engine":\n        return Engine(self.name)\n', "")   # gone, and no copy either
    t = _verify(work, db, ch, after)["tasks"][0]
    assert (t["verdict"], t["verdict_why"]) == ("contradicted", "`Engine.child` is gone, but `Engine.copy` is not on the map")
