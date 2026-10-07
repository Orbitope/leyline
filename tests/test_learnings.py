"""Learnings: a finding a person rejects with a reason is kept in a committed file, shown to later reviewers, and
marks a later finding that repeats it. A learning that keeps being overruled is retired."""

import json

import pytest

from leyline import learnings, spec, store
from test_pr import branch, git, run   # noqa: F401  (the fixture and helpers)

CLAIM = "count still calls load with one argument, so it reads the file without an encoding"
REASON = "load keeps a default encoding on purpose; one-argument callers are fine."


def _id(con, name):
    return con.execute("SELECT id FROM nodes WHERE name = ?", (name,)).fetchone()[0]


def _second_change(root, name):
    """A later change on the same code: a new branch off feature that edits `count`."""
    git(root, "checkout", "-q", "-b", name)
    use = root / "app/use.py"
    use.write_text(use.read_text().replace("return len(load(p))", "return len(load(p)) + 0"))
    git(root, "commit", "-qam", "Tidy count")


@pytest.fixture
def decided(branch, monkeypatch):
    """The feature branch reviewed, one finding filed on `count` and rejected with a reason."""
    monkeypatch.chdir(branch)
    assert run("pr", "main")[0] == 0
    con = store.connect(branch / ".leyline/leyline.db")
    f = spec.add_finding(con, "pr-feature", "logic", "high", CLAIM, [_id(con, "count")], "pass utf-8")
    r = spec.resolve_finding(con, f["id"], "rejected", REASON)
    assert r["status"] == "rejected" and r["learning"].startswith("l-")
    con.close()
    return branch, r["learning"]


def test_words_and_similarity():
    assert learnings.words("EntryQueues reads files") == {"entry", "queu", "read", "fil"}
    assert learnings.words("the entry queue read a file") == {"entry", "queu", "read", "fil"}
    assert learnings.similarity(CLAIM, "The caller count was not updated for load's new encoding argument") >= learnings.SAME_CODE
    assert learnings.similarity(CLAIM, "count returns the wrong length for an empty file") < learnings.SAME_CODE
    # the same code and some of the same words, a different worry
    assert learnings.similarity(CLAIM, "count reads the whole file into memory on every call") < learnings.SAME_CODE
    assert learnings.similarity("", CLAIM) == 0.0


def test_a_rejection_is_kept_in_a_committed_canonical_file(decided):
    root, lid = decided
    path = root / ".leyline-learnings.json"          # no openspec folder: at the root
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    [l] = data["learnings"]
    assert l["id"] == lid and l["status"] == "active" and l["reason"] == REASON and l["claim"] == CLAIM
    assert l["reviewer"] == "logic" and (l["hits"], l["dismissals"]) == (0, 0)
    # ids without the repository's id, so a clone in another folder reads them; with the type, file and module
    assert l["scope"]["nodes"] == ["python:app.use.count"] and l["scope"]["files"] == ["file:app/use.py"]
    assert l["scope"]["modules"] == ["module:app"] and l["scope"]["paths"] == ["app/use.py"]
    assert l["source"]["change"] == "pr-feature"
    code, out = run("learnings")
    assert code == 0 and lid in out and "Decided: " + REASON in out


def test_with_an_openspec_folder_the_file_is_there(tmp_path):
    (tmp_path / "openspec").mkdir()
    assert learnings.path_for(tmp_path) == tmp_path / "openspec/leyline-learnings.json"
    assert learnings.path_for(tmp_path / "x") == tmp_path / "x/.leyline-learnings.json"


def test_a_rejection_with_no_reason_keeps_nothing(branch, monkeypatch):
    monkeypatch.chdir(branch)
    run("pr", "main")
    con = store.connect(branch / ".leyline/leyline.db")
    f = spec.add_finding(con, "pr-feature", "logic", "low", CLAIM, [_id(con, "count")])
    r = spec.resolve_finding(con, f["id"], "rejected", "")
    assert "give a reason" in r["learning_note"] and not (branch / ".leyline-learnings.json").exists()


def test_a_later_similar_finding_is_marked_and_a_different_one_is_not(decided):
    root, lid = decided
    _second_change(root, "later")
    assert run("pr", "feature")[0] == 0
    con = store.connect(root / ".leyline/leyline.db")
    code, out = run("spec", "facts", "pr-later", "--reviewer", "logic")
    facts = json.loads(out)
    assert [(l["id"], l["close_on"], l["reason"]) for l in facts["learnings_that_apply"]] == [(lid, "node", REASON)]
    count = _id(con, "count")
    same = spec.add_finding(con, "pr-later", "logic", "medium",
                            "The caller count was not updated for load's new encoding argument", [count])
    assert same["learned"]["id"] == lid and same["learned"]["reason"] == REASON
    other = spec.add_finding(con, "pr-later", "logic", "medium", "count returns the wrong length for an empty file",
                             [count])
    assert "learned" not in other
    assert "learned" not in spec.add_finding(con, "pr-later", "logic", "low",
                                             "count reads the whole file into memory on every call", [count])
    perf = spec.add_finding(con, "pr-later", "performance", "low", CLAIM, [count])
    assert "learned" not in perf                    # a logic decision does not settle a performance finding
    far = spec.add_finding(con, "pr-later", "logic", "low", CLAIM, [_id(con, "routes")])
    assert "learned" not in far                     # different code
    page = run("pr", "feature")[1]
    assert f"(Matches a past decision: {REASON})" in page
    assert page.count("Matches a past decision") == 1
    [l] = json.loads((root / ".leyline-learnings.json").read_text())["learnings"]
    assert l["hits"] == 1 and l["dismissals"] == 0
    spec.resolve_finding(con, same["id"], "rejected", "as before")
    [l] = json.loads((root / ".leyline-learnings.json").read_text())["learnings"]
    assert l["dismissals"] == 1 and l["status"] == "active" and len(l["findings"]) == 1   # counted, not kept twice
    # the same finding filed again is not a second hit
    spec.add_finding(con, "pr-later", "logic", "medium", "The caller count was not updated for load's new encoding argument",
                     [count])
    assert json.loads((root / ".leyline-learnings.json").read_text())["learnings"][0]["hits"] == 1


def test_a_learning_people_keep_overruling_is_retired(decided):
    root, lid = decided
    _second_change(root, "later")
    run("pr", "feature")
    con = store.connect(root / ".leyline/leyline.db")
    count = _id(con, "count")
    for claim in ("The caller count was not updated for load's new encoding argument",
                  "count passes one argument to load and reads the file with no encoding"):
        f = spec.add_finding(con, "pr-later", "logic", "medium", claim, [count])
        assert f["learned"]["id"] == lid
        spec.resolve_finding(con, f["id"], "accepted", "yes, pass it")
    [l] = json.loads((root / ".leyline-learnings.json").read_text())["learnings"]
    assert l["status"] == "retired" and l["accepted"] == 2 and "no longer holds" in l["retired"]
    assert learnings.applying(con, "pr-later") == []
    f = spec.add_finding(con, "pr-later", "logic", "medium", "count calls load with one argument and no encoding at all",
                         [count])
    assert "learned" not in f


def test_retire_by_hand_and_the_source_finding_changing_its_mind(decided):
    root, lid = decided
    con = store.connect(root / ".leyline/leyline.db")
    code, out = run("learnings", "retire", lid, "we pass encodings now")
    assert code == 0 and lid in out
    [l] = json.loads((root / ".leyline-learnings.json").read_text())["learnings"]
    assert l["status"] == "retired" and "we pass encodings now" in l["retired"]
    assert run("learnings", "retire", "l-nope")[0] == 1
    assert "0 active of 1" in run("learnings")[1]
    # a fresh learning whose finding is then accepted instead stops applying
    f = spec.add_finding(con, "pr-feature", "logic", "low", "first ignores every line after the first one",
                         [_id(con, "first")])
    r = spec.resolve_finding(con, f["id"], "rejected", "only the first line is wanted")
    spec.resolve_finding(con, f["id"], "accepted", "changed our mind")
    l = next(x for x in json.loads((root / ".leyline-learnings.json").read_text())["learnings"] if x["id"] == r["learning"])
    assert l["status"] == "retired" and "marked accepted" in l["retired"]


def test_a_spec_review_lists_the_learnings_that_apply(decided):
    """A learning made on a pull request applies to a spec change that names the same code."""
    root, lid = decided
    ch = root / "openspec/changes/count-bytes"
    ch.mkdir(parents=True)
    (ch / "proposal.md").write_text("# Count bytes\n\n## Why\nBytes, not characters.\n\n## What Changes\nCount bytes.\n")
    (ch / "tasks.md").write_text("## 1. Code\n- [ ] 1.1 Change `count` to count bytes\n")
    con = store.connect(root / ".leyline/leyline.db")
    spec.brief(con, ch, write=False)
    facts = spec.review_facts(con, ch, "logic")
    assert [l["id"] for l in facts["learnings_that_apply"]] == [lid]
    # the file stays where it was made: an openspec folder that appears later does not move it
    assert learnings.path_for(root) == root / ".leyline-learnings.json"
    assert learnings.listing(con)["files"] == [str(root / ".leyline-learnings.json")]
