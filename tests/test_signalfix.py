"""What the Signal rehearsal found in the plan loop, on a small fixture of the same shape: a server program that a
client starts and talks to over its stdio pipe, a second client of the same program, and a client script that runs
its own checks and prints a PASS line for each. Each test names the rehearsal item it guards."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from leyline import diff, export, loop, query, spec, store

FILES = {
    "srv/core.py": '''class Config:
    QUEUE_SPEED = 2.0
    WAIT_SPEED = 3.0


class Metrics:
    def total(self, items):
        return len(items)


def slow(items):
    return [i for i in items if i < Config.QUEUE_SPEED]


def waiting(items):
    return [i for i in items if i < Config.WAIT_SPEED]
''',
    "srv/server.py": '''import json
import sys

from core import Metrics


def send_state(n):
    sys.stdout.write(json.dumps({"n": n, "total": Metrics().total([n])}) + "\\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        send_state(len(line))


if __name__ == "__main__":
    main()
''',
    "client/env.py": '''import json
import subprocess
import time


class Env:
    def __init__(self):
        self.proc = subprocess.Popen(["python", "srv/server.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def _recv_state(self):
        return json.loads(self.proc.stdout.readline())

    def step(self):
        self.proc.stdin.write(b"x\\n")
        return self._recv_state()


def check(ok, name):
    print(("PASS " if ok else "FAIL ") + name)


def main():
    env = Env()
    check(env.step()["n"] > 0, "state carries a count")
    check(True, "env starts cleanly")
    t = time.perf_counter()
    env.step()
    check(time.perf_counter() - t < 1, "pipe throughput adequate")


if __name__ == "__main__":
    main()
''',
    "client/trace.py": '''import json
import subprocess


class TraceEnv:
    def __init__(self):
        self.proc = subprocess.Popen(["python", "srv/server.py"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def _recv_state(self):
        return json.loads(self.proc.stdout.readline())


if __name__ == "__main__":
    print(TraceEnv()._recv_state())
''',
}

TASKS = """## 1. Core
- [ ] 1.1 Add `Metrics.queued` in `srv/core.py`, counting items slower than `Config.QUEUE_SPEED`
## 2. Server
- [ ] 2.1 Change `server.send_state` to add a `queued` count (one `Metrics.queued` per line) to the state
## 3. Client
- [ ] 3.1 Change `Env._recv_state` to read `queued` into `self.queued`
## 4. Tests
- [ ] 4.1 Add the check "queued is reported" to `client/env.py`
"""

BEFORE = "PASS state carries a count\nPASS env starts cleanly\nPASS pipe throughput adequate\n"
AFTER = BEFORE + "PASS queued is reported\n"


def make(root: Path) -> Path:
    for path, text in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    ch = root / "openspec" / "changes" / "report-queued"
    (ch / "specs" / "state").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Report queued items\n\n## Why\nThe client cannot see queues.\n")
    (ch / "tasks.md").write_text(TASKS)
    (ch / "specs" / "state" / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Queued count\nThe state SHALL carry a queued count.\n\n"
        "#### Scenario: queued is reported\n- **WHEN** the client steps\n- **THEN** it reads a queued count\n")
    return ch


def implement(root: Path, server: bool = True) -> None:
    def edit(path, a, b):
        (root / path).write_text((root / path).read_text().replace(a, b, 1))
    edit("srv/core.py", "        return len(items)\n",
         "        return len(items)\n\n    def queued(self, items):\n        return len([i for i in items if i < Config.QUEUE_SPEED])\n")
    if server:
        edit("srv/server.py", '"total": Metrics().total([n])', '"total": Metrics().total([n]), "queued": Metrics().queued([n])')
    edit("client/env.py", "        return json.loads(self.proc.stdout.readline())\n",
         "        state = json.loads(self.proc.stdout.readline())\n        self.queued = state.get(\"queued\", 0)\n        return state\n")
    edit("client/env.py", '    check(True, "env starts cleanly")\n',
         '    check(True, "env starts cleanly")\n    check(env.queued >= 0, "queued is reported")\n')


@pytest.fixture
def repo(tmp_path, monkeypatch):
    work = tmp_path / "repo"
    work.mkdir()
    ch = make(work)
    monkeypatch.chdir(work)
    db = work / ".leyline" / "leyline.db"
    loop.map_repos([str(work)], db, "p", exact="off", page=False)
    return work, ch, db


def results(text: str) -> list[dict]:
    return diff.parse_test_output(text)


def test_skills_say_how_to_work_from_the_cli():
    """Item 1: a CLI-only user was told to stop; a reviewer that cannot start a fresh agent had no way on."""
    root = Path(__file__).resolve().parents[1] / "skills"
    for name in ("leyline-spec", "leyline-adversarial-review"):
        text = (root / name / "SKILL.md").read_text()
        assert "If `plan` is missing, say so and stop" not in text and "It needs the Leyline MCP server" not in text
        assert "leyline spec facts <id> --reviewer logic" in text and "leyline spec finding <id>" in text
    review = (root / "leyline-adversarial-review" / "SKILL.md").read_text()
    assert "re-read only the change folder and `leyline.md`" in review and "not done by a fresh agent" in review
    assert "leyline spec resolve <finding id> accepted" in (root / "leyline-spec" / "SKILL.md").read_text()


def test_the_map_page_shows_the_folder_mapped(tmp_path):
    """Item 2: a clone of a local folder showed that folder (its git origin) as the repository's path."""
    work = tmp_path / "clone"
    work.mkdir()
    (work / "a.py").write_text("def f():\n    return 1\n")
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", "/home/someone/original"], check=True)
    db = work / ".leyline" / "leyline.db"
    loop.map_repos([str(work)], db, "c", exact="off", page=False)
    r = export.graph(store.connect(db), with_sources=False)["repos"][0]
    assert r["folder"] == str(work.resolve()) and "url" not in r
    assert export.remote_url("https://github.com/o/r.git") and export.remote_url("git@github.com:o/r.git")
    assert not export.remote_url("/home/someone/original") and not export.remote_url("file:///x")


def test_printed_summaries_count_in_the_singular(tmp_path):
    """Item 3: "1 files", "1 modules"."""
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    m = loop.map_repos([str(tmp_path)], tmp_path / ".leyline" / "leyline.db", "one", exact="off", page=False)
    text = loop.map_text(m)
    assert "1 file," in text and "(1 file)" in text and "1 files" not in text and "1 modules" not in text
    s = {"changed": 1, "added": 0, "must_edit": 0, "reached": 1, "modules": 1, "tests_to_run": 1}
    page = spec.brief_text({"dir": "x", "title": "t", "why": "", "what": "", "tasks": [], "scenarios": [], "findings": [],
                            "gaps": [], "impact": {"summary": s, "risks": [], "by_module": []}, "must_edit_uncovered": [],
                            "patterns": [], "left_alone": {}, "reviews": []})
    assert "1 thing changes, 0 more must be edited with it, and 1 is reached" in page and "across 1 module." in page
    assert "1 existing test runs through the change." in page


def test_code_a_task_only_reads_is_mentioned_not_changed(repo):
    """Item 4: "slower than `SimConfig.QueueSpeed`" made the plan change QueueSpeed, doubled the impact, called it new
    in the look-alike list, and marked its readers (CaptureFrame) as changed functions."""
    assert spec._roles("Add `A.b` in `x.py`, counting items slower than `C.D`") == [
        ("A.b", "lead"), ("x.py", "home"), ("C.D", "mention")]
    assert spec._roles("Change `A` and `B`, and add `C` to `D`, reading `E`") == [
        ("A", "lead"), ("B", "lead"), ("C", "lead"), ("D", "home"), ("E", "mention")]
    work, ch, db = repo
    b = loop.plan(db, ch, results(BEFORE))
    t = {x["key"]: x for x in b["tasks"]}
    assert t["1.1"]["nodes"] == [] and t["1.1"]["mentions"] == ["Config.QUEUE_SPEED"]
    assert [n["name"] for n in t["1.1"]["new"]] == ["queued"]
    assert t["2.1"]["new"] == [] and "Metrics.queued (not on the map yet)" in t["2.1"]["mentions"]
    page = (ch / "leyline.md").read_text()
    assert "mentions Config.QUEUE_SPEED" in page and "QUEUE_SPEED (new)" not in page and "WAIT_SPEED" not in page
    assert "it changes Env._recv_state, send_state and adds Metrics.queued" in page
    assert "It is done when 1 scenario passes" in page

    # An add task that names a member already on the map: it is not new, and not beside its look-alike.
    con = store.connect(db)
    (ch / "tasks.md").write_text("- [ ] 1.1 Add `Config.QUEUE_SPEED` to the docs\n")
    assert spec.brief(con, ch, write=False)["left_alone"]["beside"] == []
    # A changed field: its readers are reached, not changed functions on no test's path.
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `Config.QUEUE_SPEED` to 1.5\n")
    imp = spec.brief(con, ch, write=False)["impact"]
    assert imp["untested"] == [] and not any("changed functions" in r["what"] for r in imp["risks"])


def test_a_pipe_crossing_names_both_ends_and_the_second_reader(repo):
    """Item 5: `channels_crossed: []` though both ends of the env server's pipe changed, and a second program that
    reads the same output was in no list."""
    work, ch, db = repo
    b = loop.plan(db, ch, results(BEFORE))
    (c,) = b["impact"]["channels"]
    assert c["channel"] == "process" and c["hub_changed"] == ["send_state"] and c["program"] == "srv/server.py"
    assert {x["name"]: x["changed"] for x in c["spokes"]} == {"Env": ["Env._recv_state"], "TraceEnv": []}
    (other,) = b["must_agree"]
    assert other["name"] == "TraceEnv" and other["reads"] == ["TraceEnv._recv_state"]
    page = (ch / "leyline.md").read_text()
    assert ("**high risk:** Crosses a process boundary (srv/server.py): Env and TraceEnv start srv/server.py and talk to"
            " it over its stdio pipe. Both ends change (send_state and Env._recv_state)") in page
    assert "- TraceEnv._recv_state (client/trace.py): TraceEnv also starts srv/server.py and reads what it sends" in page
    facts = spec.review_facts(store.connect(db), ch)["logic"]
    assert facts["channels_crossed"] and facts["other_ends_of_those_channels_no_task_names"][0]["name"] == "TraceEnv"

    # Only the server changes: every program that reads what it sends must agree, the first client too.
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `server.send_state` to add a `queued` count\n")
    b = spec.brief(store.connect(db), ch, write=False)
    assert sorted(x["name"] for x in b["must_agree"]) == ["Env", "TraceEnv"]
    assert "One end changes (send_state)" in spec.brief_text(b)


def test_a_scripts_own_checks_are_a_test_path(repo):
    """Item 6: "2 of 2 changed functions are on no test's path" though the script that runs them printed PASS lines
    the check accepted. Its throughput check is a speed test."""
    work, ch, db = repo
    b = loop.plan(db, ch)       # no results recorded: nothing says a test runs it
    assert any("2 of 2 changed functions are on no test's path." == r["what"] for r in b["impact"]["risks"])
    b = loop.plan(db, ch, results(BEFORE))
    assert not any("no test's path" in r["what"] for r in b["impact"]["risks"])
    assert any(t.get("self_test") or "run as a script" in t["name"] for t in b["impact"]["tests_to_run"])
    speed = spec.review_facts(store.connect(db), ch)["performance"]["tests_that_measure_speed"]
    assert speed[0]["name"] == "pipe throughput adequate" and speed[0]["runs_the_change"]

    # Results the map cannot place: say what is known, not "no test's path".
    loop.plan(db, ch, results("PASS some check no file names\nPASS another one elsewhere\n"))
    risks = [r for r in spec.brief(store.connect(db), ch, write=False)["impact"]["risks"] if "no test's path" in r["what"]]
    assert risks and risks[0]["level"] == "medium" and "2 passing results the map cannot place" in risks[0]["what"]


def test_guessed_field_links_are_not_evidence(repo):
    """Item 7: `ObsSchema.Size` was "also used by" two functions that read Godot's `.Size`: links guessed by name."""
    work, ch, db = repo
    con = store.connect(db)
    with con:
        con.execute("INSERT INTO edges (kind, src_id, dst_id, precision, source) VALUES ('reads', ?, ?, 'guess', 'test')",
                    ("p:python:srv.core.waiting", "p:python:client.env.Env.proc"))
    b = spec.brief(con, ch, write=False)
    users = [u for x in b["left_alone"]["state"] for u in x["also_used_by_unchanged"]]
    assert "Env.step" in users and not any("waiting" in u for u in users)
    assert all("waiting" not in w for f in query.shared_state(con, guesses=False)["fields"] for w in f["written_from"])


def test_every_spec_command_takes_the_change_id(repo, capsys):
    """Item 8: `leyline spec facts <id>` said "no change folder"; only the path worked."""
    from leyline.cli import main
    work, ch, db = repo
    loop.plan(db, ch)
    assert main(["spec", "facts", "report-queued"]) == 0
    assert json.loads(capsys.readouterr().out)["change_id"] == "spec-report-queued"
    assert main(["spec", "brief", "report-queued"]) in (0, 1) and "# Report queued items" in capsys.readouterr().out
    assert main(["spec", "findings", "report-queued"]) == 0


def test_next_names_the_open_findings_and_the_command(repo):
    """Item 9: with findings open, Next said "implement it" and never named `leyline spec resolve`."""
    work, ch, db = repo
    b = loop.plan(db, ch, results(BEFORE))
    for kind in ("logic", "performance"):
        spec.record_review(store.connect(db), b["change_id"], kind)
    f = spec.add_finding(store.connect(db), b["change_id"], "logic", "low", "TraceEnv will not expose queued.",
                         ["p:python:client.trace.TraceEnv._recv_state"])
    nxt = loop.next_after_plan(loop.plan(db, ch), "report-queued")
    assert f["id"] in nxt[0] and 'leyline spec resolve <finding id> accepted|rejected|deferred "why"' in nxt[0]
    assert any(x.startswith("Then: implement it") for x in nxt)


def test_check_runs_again_after_done_and_the_baseline_goes_with_the_folder(repo):
    """Items 10 and 11: after "done" check refused to run, so a later regression went unreported; task 2.1 showed
    "partly" with its code untouched; Next left out `--tests -`; a check proven by a script was called generated."""
    work, ch, db = repo
    b = loop.plan(db, ch, results(BEFORE))
    snap = diff.snapshot_path(store.connect(db), b["change_id"])
    implement(work)
    v = loop.check(db, ch, results(AFTER))
    assert v["done_as_agreed"], v["why_not"]
    sc = {s["name"]: s for s in v["scenarios"]}["queued is reported"]
    assert not sc["generated"] and sc["script"] == "client/env.py"
    assert "a check in client/env.py, which runs as a script; the test is not on the map" in (ch / "leyline.md").read_text()
    assert snap.exists()

    # A regression after done: the server's half undone. Check runs, and the task is not done (not "partly").
    (work / "srv/server.py").write_text(FILES["srv/server.py"])
    v = loop.check(db, ch, results(AFTER))
    assert not v["done_as_agreed"] and {t["key"]: t["state"] for t in v["tasks"]}["2.1"] == "not done"
    assert "`leyline check report-queued --tests -`" in loop.next_after_check(v, "report-queued")[0]
    (work / "srv/server.py").write_text(FILES["srv/server.py"].replace(
        '"total": Metrics().total([n])', '"total": Metrics().total([n]), "queued": Metrics().queued([n])'))
    assert loop.check(db, ch, results(AFTER))["done_as_agreed"]

    # Archived: the folder moves, and the baseline goes on the next map.
    archive = ch.parent / "archive" / "2026-10-07-report-queued"
    archive.parent.mkdir()
    ch.rename(archive)
    loop.map_repos(None, db, page=False)
    assert not snap.exists()


def test_task_text_is_cut_at_a_word():
    """Item 11: the check table cut task text mid-word."""
    assert spec._clip("Change `Program.SendState` to add a `queued` array to the header", 30) == "Change `Program.SendState` to…"
    assert spec._clip("Add `Metrics.QueuedVehicles` in `Signal.Core/Simulation.cs`", 40) == "Add `Metrics.QueuedVehicles` in…"
    assert spec._clip("short", 30) == "short"
