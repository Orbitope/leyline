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
    # `count` was edited on this branch: the finding is still marked, with the change named
    assert ("(Matches a past decision, but the code it was about has changed since: `python:app.use.count` edited."
            f" Decided then: {REASON} Does it still hold?)") in page
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


def test_a_learnings_file_that_cannot_be_read_is_left_as_it_is(decided):
    """A file a merge left conflict markers in is the team's decisions, half merged: a new rejection must not
    write a file holding only itself over it, and a matched finding must not either."""
    root, lid = decided
    path = root / ".leyline-learnings.json"
    broken = "<<<<<<< HEAD\n" + path.read_text(encoding="utf-8") + "=======\n>>>>>>> other\n"
    path.write_text(broken, encoding="utf-8")
    con = store.connect(root / ".leyline/leyline.db")
    f = spec.add_finding(con, "pr-feature", "logic", "low", "first ignores every line after the first one",
                         [_id(con, "first")])
    r = spec.resolve_finding(con, f["id"], "rejected", "only the first line is wanted")
    assert path.read_text(encoding="utf-8") == broken
    assert "learning" not in r and "could not be read" in r["learning_note"]


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


# -- whether the code a learning is about changed since ----------------------------------------------------
SAME = "The caller count was not updated for load's new encoding argument"   # repeats the learning's claim


def _branch_with(root, name, old, new):
    """A later branch off feature whose app/use.py has `old` replaced by `new`, mapped."""
    git(root, "checkout", "-q", "-b", name)
    use = root / "app/use.py"
    assert old in use.read_text()
    use.write_text(use.read_text().replace(old, new))
    git(root, "commit", "-qam", "Later")
    assert run("pr", "feature")[0] == 0


def _learning(root):
    [l] = json.loads((root / ".leyline-learnings.json").read_text(encoding="utf-8"))["learnings"]
    return l


def test_a_fresh_learning_is_not_stale(decided):
    root, lid = decided
    l = _learning(root)
    # one hash per evidence node, keyed without the repository's id so a clone reads it the same
    assert list(l["fingerprint"]) == ["python:app.use.count"] and l["fingerprint"]["python:app.use.count"]
    # code above `count` moves it, but its own text is the same: not stale
    _branch_with(root, "moved", "def count(p):", "def extra():\n    return 0\n\n\ndef count(p):")
    con = store.connect(root / ".leyline/leyline.db")
    [a] = learnings.applying(con, "pr-moved", [_id(con, "count")])
    assert (a["id"], a["code"], a["stale"]) == (lid, "unchanged", False) and "code_note" not in a
    f = spec.add_finding(con, "pr-moved", "logic", "medium", SAME, [_id(con, "count")])
    assert f["learned"]["stale"] is False and "changed since" not in f["learned"]["note"]
    page = run("pr", "feature")[1]
    assert f"(Matches a past decision: {REASON})" in page
    r = json.loads(run("learnings", "--json")[1])
    assert r["stale"] == 0 and r["learnings"][0]["code"] == "unchanged"
    assert "Stale" not in run("learnings")[1]


def test_an_edited_node_makes_the_learning_stale_and_names_it(decided):
    root, lid = decided
    _second_change(root, "later")
    assert run("pr", "feature")[0] == 0
    con = store.connect(root / ".leyline/leyline.db")
    facts = json.loads(run("spec", "facts", "pr-later", "--reviewer", "logic")[1])
    [a] = facts["learnings_that_apply"]          # still applies: the person decides, not Leyline
    assert a["id"] == lid and a["stale"] is True and a["edited"] == ["python:app.use.count"] and a["gone"] == []
    assert "`python:app.use.count` edited" in a["code_note"]
    f = spec.add_finding(con, "pr-later", "logic", "medium", SAME, [_id(con, "count")])
    assert f["learned"]["id"] == lid and f["learned"]["stale"] is True   # the match is kept
    assert "question for the person" in f["learned"]["note"]
    assert _learning(root)["status"] == "active"                          # and nothing is retired
    r = json.loads(run("learnings", "--json")[1])
    assert r["stale"] == 1 and r["learnings"][0]["edited"] == ["python:app.use.count"]
    out = run("learnings")[1]
    assert "1 active of 1 (1 about code that has changed since)" in out
    assert "Stale: the code it was about has changed since: `python:app.use.count` edited." in out
    assert "fingerprint" in _learning(root) and "stale" not in _learning(root)   # what is worked out is not written


def test_a_deleted_node_makes_the_learning_stale_as_gone(decided):
    root, lid = decided
    _branch_with(root, "renamed", "def count(p):", "def tally(p):")
    con = store.connect(root / ".leyline/leyline.db")
    [l] = learnings.listing(con)["learnings"]
    assert l["stale"] is True and l["gone"] == ["python:app.use.count"] and l["edited"] == []
    assert "`python:app.use.count` gone" in run("learnings")[1]
    # it still applies to code in the same file
    [a] = learnings.applying(con, "pr-renamed", [_id(con, "tally")])
    assert a["close_on"] == "file" and a["gone"] == ["python:app.use.count"]


def test_confirm_takes_the_fingerprint_again(decided):
    root, lid = decided
    before = _learning(root)["fingerprint"]
    _second_change(root, "later")
    assert run("pr", "feature")[0] == 0
    code, out = run("learnings", "confirm", lid)
    assert code == 0 and f"Confirmed {lid}" in out
    l = _learning(root)
    assert l["fingerprint"] != before and l["confirmed"] and l["status"] == "active"
    con = store.connect(root / ".leyline/leyline.db")
    assert learnings.listing(con)["learnings"][0]["code"] == "unchanged"
    assert run("learnings", "confirm", "l-nope")[0] == 1 and run("learnings", "confirm")[0] == 2
    text = (root / ".leyline-learnings.json").read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def test_a_learning_kept_before_fingerprints_is_unknown_not_stale(decided):
    root, lid = decided
    path = root / ".leyline-learnings.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["learnings"][0]["fingerprint"]        # as a file written before this was recorded
    path.write_text(json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _second_change(root, "later")                   # even with the code edited, nothing says it changed since
    assert run("pr", "feature")[0] == 0
    con = store.connect(root / ".leyline/leyline.db")
    [l] = learnings.listing(con)["learnings"]
    assert (l["code"], l["stale"]) == ("unknown", False) and "not known" in l["code_note"]
    assert "whether it changed is not known" in run("learnings")[1]
    f = spec.add_finding(con, "pr-later", "logic", "medium", SAME, [_id(con, "count")])
    assert f["learned"]["code"] == "unknown" and f["learned"]["stale"] is False and "not known" in f["learned"]["note"]
    assert "Whether its code changed since is not known" in run("pr", "feature")[1]
    # the file is read and written as before; confirming gives it a fingerprint
    assert run("learnings", "confirm", lid)[0] == 0 and "fingerprint" in _learning(root)
    assert learnings.listing(con)["learnings"][0]["code"] == "unchanged"
