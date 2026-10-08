"""One verdict per task and scenario in `check`, and the project's say in which of them block."""

import json
import shutil
from pathlib import Path

import pytest

from leyline import diff, spec, store, verdicts
from leyline.indexer import index

FIXTURE2 = Path(__file__).parent / "fixture2"


# -- the config file ---------------------------------------------------------------------------------
def test_the_small_toml_reader_reads_what_tomllib_reads():
    text = ('# what blocks\n[check]\nblocking = [\n  "contradicted",  # a comment\n  \'partial\', "needs a person",\n]\n'
            'name = "a # not a comment"\nstrict = true\nlevel = 3\n\n[other.part]\nx = "y"\n')
    got = verdicts._tiny_toml(text)
    assert got == {"check": {"blocking": ["contradicted", "partial", "needs a person"], "name": "a # not a comment",
                             "strict": True, "level": 3}, "other": {"part": {"x": "y"}}}
    try:
        import tomllib
    except ImportError:
        return
    assert tomllib.loads(text) == got
    for bad in ("[check\nblocking = []\n", "[check]\nblocking = [\"a\"\n", "[check]\nblocking\n", "[check]\nx = nope\n"):
        with pytest.raises(ValueError):
            verdicts._tiny_toml(bad)


def _openspec(tmp_path, toml=None) -> Path:
    ch = tmp_path / "openspec" / "changes" / "c"
    ch.mkdir(parents=True)
    if toml is not None:
        (tmp_path / "openspec" / "leyline.toml").write_text(toml)
    return ch


def test_config_default_override_and_mistakes(tmp_path):
    assert verdicts.load_config(_openspec(tmp_path / "a")) == {
        "blocking": ["partial", "contradicted", "inconclusive"], "file": None, "notes": []}
    c = verdicts.load_config(_openspec(tmp_path / "b", '[check]\nblocking = ["contradicted", "needs-you", "maybe", "proven"]\n'))
    assert c["blocking"] == ["contradicted", "needs a person"] and c["file"] == "openspec/leyline.toml"
    assert c["notes"] == ['openspec/leyline.toml names "maybe", which is not a verdict; it was left out.'
                          " The verdicts are proven, partial, contradicted, inconclusive, needs a person."]
    c = verdicts.load_config(_openspec(tmp_path / "c", "[check\nblocking = 3\n"))
    assert c["blocking"] == list(verdicts.DEFAULT_BLOCKING) and "could not be read" in c["notes"][0]
    c = verdicts.load_config(_openspec(tmp_path / "d", "[check]\nblocking = 3\n"))
    assert c["blocking"] == list(verdicts.DEFAULT_BLOCKING) and "should be a list" in c["notes"][0]
    assert verdicts.load_config(_openspec(tmp_path / "e", "[check]\nblocking = []\n"))["blocking"] == []
    assert verdicts.load_config(_openspec(tmp_path / "f", "[other]\nx = 1\n"))["file"] is None
    assert verdicts.load_config(tmp_path / "not-in-openspec")["file"] is None


# -- scenarios, from the results ---------------------------------------------------------------------
def _s(state="passes", test="t", static=True, measured=None):
    return {"state": state, "test": test, "reaches_the_change": static, "measured_running_the_change": measured}


P, F, SK = {"status": "pass"}, {"status": "fail"}, {"status": "skip"}


@pytest.mark.parametrize("s, after, before, verdict, says", [
    (_s("fails"), [P, F], [P], "partial", "1 of 2 results"),
    (_s("fails"), [F], [P], "contradicted", "passed before the change and fails now"),
    (_s("fails"), [F], None, "contradicted", "fails after the change"),
    (_s("skipped"), [SK], [P], "inconclusive", "skipped"),
    (_s("test exists, not run"), [], [P], "inconclusive", "no result"),
    (_s("no test", test=None), [], [], "inconclusive", "has no test"),
    (_s(static=False), [P], [], "proven", "new since the plan"),
    (_s(static=False), [P], [F], "proven", "failed before"),
    (_s(), [P], [P], "proven", "runs the changed code, as it did before"),
    (_s(static=False, measured=True), [P], [P], "proven", "runs the changed code"),
    (_s(), [P], None, "proven", "runs the changed code"),
    (_s(static=False), [P], [P], "needs a person", "does not reach the changed code"),
    (_s(measured=False), [P], [P], "needs a person", "measured coverage"),
    (_s(test=None, static=False), [P], [P], "needs a person", "passed before the change too"),
    (_s(test=None, static=False), [P], None, "needs a person", "no run from before"),
])
def test_scenario_verdicts(s, after, before, verdict, says):
    got, why = verdicts._scenario_verdict(s, after, before)
    assert got == verdict and says in why


# -- tasks and scenarios on a real change ------------------------------------------------------------
TASKS = ("- [ ] 1.1 Change `Engine.start` to return the name in upper case\n"
         "- [ ] 1.2 Add `Engine.shout`, the name with an exclamation mark\n"
         "- [ ] 1.3 Add the test \"Shout\"\n"
         "- [ ] 1.4 Say so in the README\n")
SPEC = ("## ADDED Requirements\n### Requirement: Loud names\nThe engine SHALL report its name loudly.\n\n"
        "#### Scenario: Start\n- **WHEN** an engine starts\n- **THEN** it returns its name in upper case\n\n"
        "#### Scenario: Shout\n- **WHEN** an engine shouts\n- **THEN** the name ends with an exclamation mark\n\n"
        "#### Scenario: Made\n- **WHEN** an engine is made\n- **THEN** it is named made\n")
BEFORE = [{"name": n, "status": "pass"} for n in ("test_start", "test_child", "test_made", "test_chain", "test_rename")]


@pytest.fixture
def loud(tmp_path):
    work = tmp_path / "repo"
    shutil.copytree(FIXTURE2, work)
    ch = work / "openspec" / "changes" / "loud-engine"
    (ch / "specs" / "engine").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Loud engine\n\n## Why\nNames are hard to read in logs.\n\n"
                                    "## What Changes\n- `Engine.start` returns the name in upper case\n- A new `Engine.shout`\n")
    (ch / "tasks.md").write_text(TASKS)
    (ch / "specs" / "engine" / "spec.md").write_text(SPEC)
    db = tmp_path / "s.db"
    index(work, db, "f2")
    c = store.connect(db)
    diff.record_tests(c, "before", BEFORE)
    assert "error" not in spec.brief(c, ch)
    c.close()
    return work, ch, db


def _implement(work: Path, start=True):
    core = work / "py/src/pkg/core.py"
    text = core.read_text()
    if start:
        text = text.replace("        return self.name\n", "        return self.name.upper()\n", 1)
    core.write_text(text.replace("    def stop(self):\n        super().stop()\n",
                                 "    def stop(self):\n        super().stop()\n\n    def shout(self):\n        return self.name + \"!\"\n", 1))
    tests = work / "py/tests/test_engine.py"
    tests.write_text(tests.read_text().replace('engine.start() == "fixture"', 'engine.start() == "FIXTURE"')
                     + "\n\ndef test_shout(engine):\n    assert engine.shout().endswith(\"!\")\n")


def _check(work, db, ch, after):
    index(work, db, "f2")
    c = store.connect(db)
    try:
        with c:
            c.execute("DELETE FROM test_results WHERE run = 'after'")
        diff.record_tests(c, "after", after)
        return spec.verify(c, ch, before_run="before", after_run="after")
    finally:
        c.close()


def _compatible(v):
    """Under the default config, the verdicts hold up exactly what the task and scenario states used to."""
    tasks = any(t["state"] in ("not done", "partly") for t in v["tasks"])
    scenarios = any(s["state"] != "passes" for s in v["scenarios"])
    assert ("some tasks are not done" in v["why_not"]) == tasks
    assert ("some scenarios are not proven" in v["why_not"]) == scenarios


AFTER = [{"name": n, "status": "pass"} for n in ("test_start", "test_child", "test_made", "test_chain", "test_rename", "test_shout")]


def test_done_as_agreed_with_a_verdict_for_every_item(loud):
    work, ch, db = loud
    _implement(work)
    v = _check(work, db, ch, AFTER)
    _compatible(v)
    assert {t["key"]: t["verdict"] for t in v["tasks"]} == {"1.1": "proven", "1.2": "proven", "1.3": "proven",
                                                             "1.4": "needs a person"}
    sc = {s["name"]: s for s in v["scenarios"]}
    assert sc["Start"]["verdict"] == "proven" and "as it did before" in sc["Start"]["verdict_why"]
    assert sc["Shout"]["verdict"] == "proven" and "new since the plan" in sc["Shout"]["verdict_why"]
    # test_made passes before and after, and never reaches the changed code: a person says whether it tests the change.
    assert sc["Made"]["verdict"] == "needs a person"
    assert v["done_as_agreed"] and v["verdicts"]["counts"]["proven"] == 5 and v["verdicts"]["config"] is None
    json.dumps(v)                       # what MCP `check` returns, verdicts included
    page = (ch / "leyline.md").read_text()
    assert "| Task | Result | Verdict | Missing |" in page and "| Start | passes | proven |" in page
    assert "Verdicts: 5 proven, 2 needs you. Blocking: partial, contradicted, inconclusive. Not blocking: needs you." in page
    assert "- scenario \"Made\", needs a person: its test passes, but on the map it does not reach" in page
    assert "every scenario's test passes" in page and 'Scenario "Made" needs you to confirm it tests the change.' in page
    from leyline import loop
    assert 'Scenario "Made" needs a person to confirm' in loop.next_after_check(v, "loud-engine")[0]

    # A project that wants every item proven by the code says so; the same change is then not done.
    (work / "openspec" / "leyline.toml").write_text('[check]\nblocking = ["partial", "contradicted", "inconclusive",'
                                                     ' "needs a person"]\n')
    v = _check(work, db, ch, AFTER)
    assert not v["done_as_agreed"]
    assert v["why_not"] == ["some items need a person, and openspec/leyline.toml makes that block"]
    nxt = " ".join(loop.next_after_check(v, "loud-engine"))
    assert "task 1.4 names no code" in nxt and 'scenario "Made"' in nxt
    assert "(Set in openspec/leyline.toml.)" in (ch / "leyline.md").read_text()


@pytest.mark.parametrize("task", [
    '- [ ] 1.1 Add `Engine.label`, which puts "ENGINE " before what `Engine.start` returns\n',
    "- [ ] 1.1 Add `Engine.label`\n",
])
def test_a_change_that_only_adds_code(tmp_path, task):
    """Every task adds code, so nothing on the map changes: the plan is still written, its view shows where the new
    code goes, and code named beside it is related, not reached."""
    work = tmp_path / "repo"
    shutil.copytree(FIXTURE2, work)
    ch = work / "openspec" / "changes" / "engine-label"
    (ch / "specs" / "engine").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Engine label\n\n## Why\nLogs need a label.\n\n## What Changes\n- A new `Engine.label`\n")
    (ch / "tasks.md").write_text(task + '- [ ] 1.2 Add the test "Label"\n')
    (ch / "specs" / "engine" / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Label\nThe engine SHALL have a label.\n\n"
        "#### Scenario: Label\n- **WHEN** an engine is labelled\n- **THEN** the label starts with ENGINE\n")
    db = tmp_path / "s.db"
    index(work, db, "f2")
    c = store.connect(db)
    diff.record_tests(c, "before", BEFORE)
    b = spec.brief(c, ch)
    assert "error" not in b["impact"]
    view = json.loads(c.execute("SELECT spec FROM views WHERE id = 'view-spec-engine-label'").fetchone()[0])
    engine = "f2:python:py.src.pkg.core.Engine"
    assert {m["id"]: m["role"] for m in view["marks"]} == {engine: "new", **({engine + ".start": "note"} if "start" in task else {})}
    assert [(n["name"], n["parent"]) for n in view["new_nodes"]] == [("label", engine)]
    c.close()
    assert "Engine.label (new)" in (ch / "leyline.md").read_text()

    core = work / "py/src/pkg/core.py"
    core.write_text(core.read_text().replace("        return self.name\n",
                                             "        return self.name\n\n    def label(self):\n        return \"ENGINE \" + self.start()\n", 1))
    tests = work / "py/tests/test_engine.py"
    tests.write_text(tests.read_text() + '\n\ndef test_label(engine):\n    assert engine.label().startswith("ENGINE ")\n')
    v = _check(work, db, ch, BEFORE + [{"name": "test_label", "status": "pass"}])
    assert v["done_as_agreed"] and not v["drift"], v["why_not"]
    assert {s["name"]: s["verdict"] for s in v["scenarios"]} == {"Label": "proven"}


def test_broken_and_missing_results(loud):
    work, ch, db = loud
    _implement(work)
    broken = [r if r["name"] != "test_shout" else {"name": "test_shout", "status": "fail", "message": "no !"} for r in AFTER]
    v = _check(work, db, ch, broken)
    _compatible(v)
    sc = {s["name"]: s for s in v["scenarios"]}
    assert sc["Shout"]["verdict"] == "contradicted" and not v["done_as_agreed"]
    assert "1 contradicted" in (ch / "leyline.md").read_text()

    # Two results carry the name "Start": one passes, one fails.
    v = _check(work, db, ch, AFTER + [{"name": "tests/test_engine.py::test_start", "status": "fail"}])
    _compatible(v)
    assert {s["name"]: s["verdict"] for s in v["scenarios"]}["Start"] == "partial"

    # No result for "Shout": inconclusive, which blocks by default ...
    v = _check(work, db, ch, [r for r in AFTER if r["name"] != "test_shout"])
    _compatible(v)
    assert {s["name"]: s["verdict"] for s in v["scenarios"]}["Shout"] == "inconclusive" and not v["done_as_agreed"]
    # ... and does not where the project says only a contradiction blocks.
    (work / "openspec" / "leyline.toml").write_text('[check]\nblocking = ["contradicted"]\n')
    v = _check(work, db, ch, [r for r in AFTER if r["name"] != "test_shout"])
    assert v["done_as_agreed"]
    page = (ch / "leyline.md").read_text()
    assert "**Yes.** Nothing that blocks is left under openspec/leyline.toml" in page and "let through: 1 inconclusive" in page
    assert "- scenario \"Shout\", inconclusive: no result for its test was recorded (does not block)" in page


def test_task_verdicts(loud):
    work, ch, db = loud
    # Nothing changed yet: the map cannot tell a task undone from code not mapped again.
    v = _check(work, db, ch, BEFORE)
    _compatible(v)
    assert {t["key"]: t["verdict"] for t in v["tasks"]}["1.1"] == "inconclusive"

    # Only the shout is added: Engine.start did not change while other code did.
    _implement(work, start=False)
    v = _check(work, db, ch, AFTER)
    _compatible(v)
    t = {t["key"]: t for t in v["tasks"]}
    assert t["1.1"]["verdict"] == "contradicted" and "Engine.start did not change" in t["1.1"]["verdict_why"]
    assert t["1.2"]["verdict"] == "proven" and not v["done_as_agreed"]

    # A task that names two functions and changes one of them is partial.
    (ch / "tasks.md").write_text(TASKS + "- [ ] 1.5 Change `Engine.start` and `Engine.child` to agree\n")
    c = store.connect(db)
    spec.brief(c, ch)
    c.close()
    v = _check(work, db, ch, AFTER)
    _compatible(v)
    t = {t["key"]: t for t in v["tasks"]}
    assert t["1.5"]["state"] == "not done" and t["1.5"]["verdict"] == "contradicted"
    core = work / "py/src/pkg/core.py"   # now start changes too
    core.write_text(core.read_text().replace("        return self.name\n", "        return self.name.upper()\n", 1))
    v = _check(work, db, ch, AFTER)
    _compatible(v)
    t = {t["key"]: t for t in v["tasks"]}
    assert t["1.1"]["verdict"] == "proven" and t["1.5"]["state"] == "partly" and t["1.5"]["verdict"] == "partial"
    assert "not Engine.child" in t["1.5"]["verdict_why"]
