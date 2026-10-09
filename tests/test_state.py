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
