"""Spec drift: `check` anchors what a finished change's code names meant, and `leyline drift` says which of them, and
which names in the living specs, have since gone, moved, changed signature or become ambiguous."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from leyline import drift
from leyline.cli import main

FIXTURE2 = Path(__file__).parent / "fixture2"
LIVING = ("# Engine\n\n### Requirement: Engines\nAn engine is made by `core.make_engine` and started by `Engine.start`.\n"
          "`Journal.note` records what happened, `Engine.child` makes a copy and `stop` stops it.\n"
          "Words that are not code: `true`, `GET`.\n\n```python\nEngine.missing()  # code in a fence is not a name\n```\n")


def suite_output(work: Path) -> str:
    return subprocess.run([sys.executable, "-m", "pytest", "-rA", "-q", "-p", "no:cacheprovider", "tests"],
                          cwd=work / "py", env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True).stdout


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new, 1))


def run(capsys, *argv) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


@pytest.fixture
def done(tmp_path, monkeypatch, capsys):
    """fixture2 with a living spec, and the loud-engine change planned, implemented and checked done as agreed."""
    work = tmp_path / "repo"
    shutil.copytree(FIXTURE2, work)
    (work / "openspec/specs/engine").mkdir(parents=True)
    (work / "openspec/specs/engine/spec.md").write_text(LIVING)
    ch = work / "openspec/changes/loud-engine"
    (ch / "specs/engine").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Loud engine\n\n## Why\nNames are hard to read in logs.\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `Engine.start` to return the name in upper case\n"
                                 "- [ ] 1.2 Add `Engine.shout`, the name with an exclamation mark\n"
                                 "- [ ] 1.3 Add the test \"Shout\"\n")
    (ch / "specs/engine/spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Loud names\nThe engine SHALL report its name loudly through `Engine.start`,"
        " and a journal keeps `Journal.note` and `Journal.count`.\n\n#### Scenario: Start\n- **WHEN** an engine starts\n"
        "- **THEN** it returns its name in upper case\n\n#### Scenario: Shout\n- **WHEN** an engine shouts\n"
        "- **THEN** the name ends with an exclamation mark\n\n## REMOVED Requirements\n### Requirement: Quiet\n"
        "`Engine.whisper` is gone.\n")
    monkeypatch.chdir(work)
    assert main(["map", ".", "--exact", "off"]) == 0
    (work / "before.txt").write_text(suite_output(work))
    assert main(["plan", "loud-engine", "--tests", str(work / "before.txt")]) == 0
    edit(work / "py/src/pkg/core.py", "        return self.name\n",
         "        return self.name.upper()\n\n    def shout(self):\n        return self.name + \"!\"\n")
    t = work / "py/tests/test_engine.py"
    t.write_text(t.read_text().replace('engine.start() == "fixture"', 'engine.start() == "FIXTURE"')
                 + "\n\ndef test_shout(engine):\n    assert engine.shout().endswith(\"!\")\n")
    (work / "after.txt").write_text(suite_output(work))
    code, out = run(capsys, "check", "loud-engine", "--tests", str(work / "after.txt"))
    assert code == 0, out
    assert "Recorded what the spec's 4 code names mean now in openspec/leyline-anchors.json (commit it)" in out
    return work


def test_check_anchors_each_code_name_in_a_committed_file_and_the_store(done):
    f = done / "openspec" / drift.ANCHOR_FILE
    text = f.read_text()
    data = json.loads(text)
    assert text == json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"   # canonical
    anchors = data["anchors"]["loud-engine"]["anchors"]
    by = {a["written"]: a for a in anchors}
    assert set(by) == {"Engine.start", "Engine.shout", "Journal.note", "Journal.count"}   # not the REMOVED one
    start = by["Engine.start"]
    assert start["node"] == "python:py.src.pkg.core.Engine.start"          # no repository id: another clone reads it
    assert start["decl"] == "def start(self)" and start["kind"] == "callable" and start["path"] == "py/src/pkg/core.py"
    assert start["from"] == ["specs/engine/spec.md", "tasks.md"] and len(start["body_hash"]) == 16
    rows = sqlite3.connect(done / ".leyline/leyline.db").execute(
        "SELECT written, node_id, decl FROM spec_anchors WHERE change_id = 'loud-engine' ORDER BY written").fetchall()
    assert ("Engine.start", "repo:python:py.src.pkg.core.Engine.start", "def start(self)") in rows and len(rows) == 4


def test_nothing_drifts_until_the_code_moves_on(done, capsys):
    code, out = run(capsys, "drift")
    assert code == 0, out
    assert "Every name that is code still matches it." in out
    # `stop` is Base.stop or Engine.stop: said, but not a failure
    assert "1 name could be several things" in out and "`stop` could be 2 things (Base.stop, Engine.stop)" in out
    assert "2 other words in backticks are not code on the map" in out    # `true`, `GET`; the fence is skipped


def test_gone_moved_signature_and_body_are_each_said(done, capsys):
    core = done / "py/src/pkg/core.py"
    edit(core, "    def start(self):", "    def start(self, loud=True):")      # signature
    edit(core, "def make_engine(", "def build_engine(")                        # gone (renamed)
    edit(core, "\n    def count(self):\n        return len(self.items) + len(self.by)\n", "\n")   # gone, and anchored
    edit(core, "        return self.name + \"!\"", "        return self.name + \"!!\"")   # body only
    text = core.read_text()
    at = text.index("class Journal")
    core.write_text(text[:at])
    (done / "py/src/pkg/journal.py").write_text(text[at:])                    # moved
    code, out = run(capsys, "drift")
    assert code == 1, out
    spec_part = out.split("## openspec/specs/engine/spec.md")[1]
    assert ("- `Engine.start` has changed signature since the spec was written: was `def start(self)`, now"
            " `def start(self, loud=True)`.") in spec_part
    assert "- `core.make_engine` is gone: py/src/pkg/core.py has no `make_engine` now." in spec_part
    assert "- `Journal.note` has moved: it is now in py/src/pkg/journal.py (was in py/src/pkg/core.py)." in spec_part
    assert ("Changed inside since the spec was written, same signature (read them to be sure the spec still holds):"
            " `Engine.shout`.") in out
    # Journal.count is named only by the change: reported under it, and Journal.note only once, under the spec.
    change_part = out.split("## Change loud-engine")[1]
    assert ("- `Journal.count` is gone: nothing on the map answers to it now (it was Journal.count in"
            " py/src/pkg/core.py).") in change_part and "Journal.note" not in change_part
    assert "4 names no longer match the code: 2 are gone, 1 changed signature and 1 moved." in out
    assert out.count("`Engine.start` has changed signature") == 1
    assert "Next: for each name above, update the spec" in out


def test_anchors_outlive_a_fresh_map(done, capsys):
    edit(done / "py/src/pkg/core.py", "    def start(self):", "    def start(self, loud):")
    shutil.rmtree(done / ".leyline")
    assert main(["map", ".", "--exact", "off"]) == 0
    capsys.readouterr()
    code, out = run(capsys, "drift")
    assert code == 1 and "`Engine.start` has changed signature" in out


def test_accept_records_the_code_as_it_is_and_keeps_what_is_gone(done, capsys):
    core = done / "py/src/pkg/core.py"
    edit(core, "    def child(self)", "    def child(self, n)")          # named only by the living spec
    code, out = run(capsys, "drift")
    assert code == 0 and "Engine.child" not in out                        # it had no anchor, so it is not seen
    code, out = run(capsys, "drift", "--accept")
    assert code == 0 and "Recorded the code as it is now" in out
    data = json.loads((done / "openspec" / drift.ANCHOR_FILE).read_text())["anchors"]
    assert {a["written"] for a in data["specs/engine"]["anchors"]} >= {"Engine.child", "core.make_engine", "Engine.start"}
    edit(core, "    def child(self, n)", "    def child(self)")
    edit(core, "def make_engine(", "def build_engine(")
    code, out = run(capsys, "drift", "--accept")
    assert code == 1 and "`Engine.child` has changed signature" not in out   # accepted again as it is now
    # Accepting does not make gone code agree. The anchor kept its body without its name, so the rename shows.
    assert "`core.make_engine` has been renamed to `build_engine` in py/src/pkg/core.py" in out
    edit(done / "openspec/specs/engine/spec.md", "`core.make_engine`", "`core.build_engine`")
    code, out = run(capsys, "drift", "--accept")
    assert code == 0, out


def test_archived_changes_are_read_and_what_they_removed_is_not_missed(done, capsys):
    archive = done / "openspec/changes/archive"
    archive.mkdir(parents=True)
    (done / "openspec/changes/loud-engine").rename(archive / "2026-10-01-loud-engine")
    old = archive / "2025-01-01-tidy"
    old.mkdir()
    (old / "tasks.md").write_text("- [x] 1.1 Remove `Engine.whisper`\n- [x] 1.2 Change `Engine.stop` to log\n"
                                  "- [x] 1.3 Change `Journal.forget` to clear the journal\n")
    code, out = run(capsys, "drift")
    assert code == 1, out
    tidy = out.split("## Change tidy (archived in openspec/changes/archive/2025-01-01-tidy)")[1]
    assert "- `Journal.forget` is gone: Journal has no `forget` now." in tidy
    assert "whisper" not in out                      # a task that removes code names what is meant to be gone
    edit(done / "py/src/pkg/core.py", "    def start(self):", "    def start(self, loud):")
    code, out = run(capsys, "drift")
    assert "`Engine.start` has changed signature" in out.split("## openspec/specs/engine/spec.md")[1]


def test_plan_names_drifted_specs_the_change_touches(done, capsys):
    edit(done / "py/src/pkg/core.py", "    def start(self):", "    def start(self, loud):")
    ch = done / "openspec/changes/quiet-engine"
    ch.mkdir()
    (ch / "proposal.md").write_text("# Change: Quiet engine\n\n## Why\nToo loud.\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `Engine.start` to whisper\n")
    main(["plan", "quiet-engine"])
    capsys.readouterr()
    page = (ch / "leyline.md").read_text()
    assert ("- The living spec engine/spec.md names `Engine.start`, which has changed signature since it was written."
            in page)
    # A change that touches none of the drifted code hears nothing about it.
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `poke` to poke twice\n")
    main(["plan", "quiet-engine"])
    capsys.readouterr()
    assert "Specs that no longer match" not in (ch / "leyline.md").read_text()


def test_no_specs_is_said_plainly(tmp_path, monkeypatch, capsys):
    work = tmp_path / "repo"
    shutil.copytree(FIXTURE2, work)
    monkeypatch.chdir(work)
    assert main(["map", ".", "--exact", "off"]) == 0
    capsys.readouterr()
    code, out = run(capsys, "drift")
    assert code == 0 and "No openspec/ folder here" in out


def test_the_store_keeps_anchors_when_the_file_is_lost_and_a_bad_file_is_left_alone(done, capsys):
    f = done / "openspec" / drift.ANCHOR_FILE
    f.unlink()
    edit(done / "py/src/pkg/core.py", "    def start(self):", "    def start(self, loud):")
    code, out = run(capsys, "drift")
    assert code == 1 and "`Engine.start` has changed signature" in out
    f.write_text("{not json")
    code, out = run(capsys, "drift", "--accept")
    assert "could not be read" in out and f.read_text() == "{not json"
