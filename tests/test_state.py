"""State over time and the error paths: a run stopped part way, a command run again or in another order, files
Leyline writes into the repository, and what a later run makes of what an earlier one left."""

from __future__ import annotations

import pathlib

import pytest

from leyline import loop, spec
from test_signalfix import BEFORE, make


@pytest.fixture
def repo(tmp_path, monkeypatch):
    work = tmp_path / "repo"
    work.mkdir()
    ch = make(work)
    monkeypatch.chdir(work)
    db = work / ".leyline" / "leyline.db"
    loop.map_repos([str(work)], db, "p", exact="off", page=False)
    return work, ch, db


def test_notes_above_a_page_cut_off_before_its_end_marker_are_kept(repo):
    """A leyline.md whose generated block lost its end marker (a write cut off, a bad merge) still has the person's
    notes above the block: the next plan replaces the block and keeps them."""
    work, ch, db = repo
    loop.plan(db, ch)
    page = ch / "leyline.md"
    text = page.read_text(encoding="utf-8")
    page.write_text("My notes on this change.\n\n" + text[:len(text) // 2], encoding="utf-8")
    loop.plan(db, ch)
    again = page.read_text(encoding="utf-8")
    assert again.startswith("My notes on this change.\n\n" + spec.BEGIN) and again.rstrip().endswith(spec.END)
    assert again.count(spec.BEGIN) == 1


def test_a_write_of_leyline_md_that_fails_leaves_the_page_as_it_was(repo, monkeypatch):
    """Writing the page in place empties it first: a write that fails part way (a full disk, a killed process) left
    half a page, or none, in a file that is committed."""
    work, ch, db = repo
    loop.plan(db, ch)
    page = ch / "leyline.md"
    before = page.read_bytes()
    real = pathlib.Path.write_text

    def cut_off(self, data, *a, **k):
        if self.name == "leyline.md":
            real(self, data[:10], *a, **k)
            raise OSError(28, "No space left on device")
        return real(self, data, *a, **k)

    def fails_late(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pathlib.Path, "write_text", cut_off)
    monkeypatch.setattr("os.replace", fails_late)
    with pytest.raises(OSError):
        loop.check(db, ch, results=[{"name": n.split(" ", 1)[1], "status": "pass"} for n in BEFORE.splitlines()])
    assert page.read_bytes() == before
    assert sorted(p.name for p in ch.iterdir()) == ["leyline.md", "proposal.md", "specs", "tasks.md"]   # no temp file left


def test_a_plan_whose_baseline_cannot_be_kept_says_so(repo, monkeypatch):
    """The baseline is what `check` compares with. When it could not be written (a full disk, a read-only folder),
    the plan said nothing and the check afterwards blamed a forgotten or archived change."""
    from leyline import diff
    work, ch, db = repo

    def fails(con, name):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(diff, "snapshot", fails)
    b = loop.plan(db, ch)
    st = spec.brief_status(b)
    assert not st["ready"] and any("No space left on device" in x and "baseline" in x for x in st["blocking"])
    assert "No space left on device" in (ch / "leyline.md").read_text(encoding="utf-8")


def test_plan_and_check_on_a_store_with_nothing_mapped_say_so(repo):
    """A store with no repository in it (a first map stopped before it wrote anything, or one a reader created) is not
    a map of code where every name is new: plan said "ready to implement", wrote over leyline.md and kept a baseline
    of nothing."""
    from leyline import store
    work, ch, db = repo
    loop.plan(db, ch)
    page = (ch / "leyline.md").read_text(encoding="utf-8")
    empty = work / "empty" / "leyline.db"
    store.connect(empty).close()
    for r in (loop.plan(db=empty, change_dir=ch), loop.check(db=empty, change_dir=ch)):
        assert "nothing is mapped" in r.get("error", "")
    assert (ch / "leyline.md").read_text(encoding="utf-8") == page
    assert not (empty.parent / "snapshots").exists()


def git(root, *args):
    import subprocess
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def test_a_baseline_stays_while_its_change_folder_is_on_another_branch(tmp_path, monkeypatch):
    """A change folder committed on a feature branch is missing from the working tree while another branch is checked
    out. A map run there took the folder as removed and deleted the baseline, so back on the branch `check` had
    nothing to compare with."""
    from test_signalfix import FILES, implement
    work = tmp_path / "repo"
    for path, text in FILES.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    git(work, "switch", "-q", "-c", "feat")
    ch = make(work)
    git(work, "add", "-A")
    git(work, "commit", "-qm", "spec")
    monkeypatch.chdir(work)
    db = work / ".leyline" / "leyline.db"
    loop.map_repos([str(work)], db, "p", exact="off", page=False)
    loop.plan(db, ch)
    git(work, "add", "openspec")   # leyline.md is committed with the change
    git(work, "commit", "-qm", "plan")
    git(work, "switch", "-q", "main")
    assert not ch.exists()
    loop.map_repos(None, db, page=False)
    git(work, "switch", "-q", "feat")
    implement(work)
    v = loop.check(db, ch)
    assert "error" not in v, v
    assert {t["key"]: t["state"] for t in v["tasks"]}["1.1"] == "done"


def test_a_baseline_goes_when_its_change_folder_is_archived(tmp_path, monkeypatch):
    """What the README promises stays: an archived change's baseline is deleted on the next map."""
    from leyline import diff, store
    work = tmp_path / "repo"
    work.mkdir()
    ch = make(work)
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    monkeypatch.chdir(work)
    db = work / ".leyline" / "leyline.db"
    loop.map_repos([str(work)], db, "p", exact="off", page=False)
    loop.plan(db, ch)
    archived = ch.parent / "archive" / ("2026-01-01-" + ch.name)
    archived.parent.mkdir()
    git(work, "mv", str(ch), str(archived))
    git(work, "commit", "-qm", "archive")
    loop.map_repos(None, db, page=False)
    con = store.connect(db)
    try:
        assert not diff.snapshot_path(con, "spec-report-queued").exists()
    finally:
        con.close()


def test_check_says_when_the_baseline_was_taken_by_another_version(repo):
    """A plan made before upgrading Leyline keeps a baseline read by the old version. What the new one reads differently
    (a name defined twice is one node now) showed as edits outside the spec, with nothing to say why."""
    import sqlite3
    from test_signalfix import AFTER, implement
    from leyline import diff
    work, ch, db = repo
    loop.plan(db, ch)
    implement(work)
    v = loop.check(db, ch, diff.parse_test_output(AFTER))
    assert not v["baseline_other_version"] and "another version of Leyline" not in (ch / "leyline.md").read_text()
    snap = sqlite3.connect(work / ".leyline" / "snapshots" / "spec-report-queued.db")
    with snap:
        snap.execute("DELETE FROM meta WHERE key = 'made_by'")   # as every release before this one left it
    snap.close()
    v = loop.check(db, ch)
    assert v["baseline_other_version"] and "another version of Leyline" in (ch / "leyline.md").read_text()


def test_a_damaged_baseline_is_named_not_the_store(repo, capsys):
    """A baseline cut off (a disk error, a copy that stopped) made `check` say the store was damaged and should be
    deleted, which would throw away the findings, test runs and views kept in a store that was fine."""
    from leyline import cli
    work, ch, db = repo
    loop.plan(db, ch)
    snap = work / ".leyline" / "snapshots" / "spec-report-queued.db"
    data = snap.read_bytes()
    snap.write_bytes(data[:len(data) // 3])
    assert cli.main(["check", "report-queued"]) == 2
    err = capsys.readouterr().err
    assert "spec-report-queued.db" in err and "the store is damaged" not in err, err


def test_the_map_server_closes_the_store_after_each_page(repo, monkeypatch):
    """`leyline view` runs until stopped and opens the store on every page load; each connection was left open."""
    import gc
    import http.server
    import io
    import warnings
    from leyline import cli
    work, ch, db = repo

    class Server:
        def __init__(self, address, handler):
            self.handler = handler

        def serve_forever(self):
            h = object.__new__(self.handler)
            h.wfile = io.BytesIO()
            h.send_response = h.send_header = lambda *a: None
            h.end_headers = lambda: None
            h.headers = {"Host": "127.0.0.1:8765"}   # the page is only served to this machine by name
            for _ in range(3):
                h.do_GET()
            raise KeyboardInterrupt
    monkeypatch.setattr(http.server, "HTTPServer", Server)
    gc.collect()
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always", ResourceWarning)
        assert cli.main(["--db", str(db), "view"]) == 0
        gc.collect()
    assert not [w for w in seen if issubclass(w.category, ResourceWarning) and "database" in str(w.message)]


def test_check_after_a_plan_that_did_not_finish_says_to_plan_again(repo, monkeypatch):
    """A plan stopped after it stored the change and its baseline, but before it stored the tasks and scenarios: check
    compared the code with no tasks at all."""
    from test_signalfix import AFTER, implement
    from leyline import diff
    work, ch, db = repo

    def dies(*a, **k):
        raise KeyboardInterrupt
    with monkeypatch.context() as m:
        m.setattr(spec, "_crossings", dies)
        with pytest.raises(KeyboardInterrupt):
            loop.plan(db, ch)
    implement(work)
    v = loop.check(db, ch, diff.parse_test_output(AFTER))
    assert "error" in v and "plan" in v["error"], {k: v.get(k) for k in ("done_as_agreed", "tasks", "error")}
    assert "the baseline it took is kept" in v["next"][0]
    loop.plan(db, ch)   # as the error says: the first baseline stays, so the check compares with the code as it was
    v = loop.check(db, ch, diff.parse_test_output(AFTER))
    assert v["done_as_agreed"], v["why_not"]
