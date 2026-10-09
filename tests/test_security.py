"""Leyline runs on repositories someone else wrote (a pull request under review, a project just cloned), and its MCP
tools take arguments from an agent that may have read untrusted text. These tests hold the lines it must not cross:
no write outside the repository through a link committed in it, no file outside it read into a page."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from leyline import agent_skills, drift, loop, spec, store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def victim(tmp_path):
    """A file outside the repository, as ~/.bashrc would be."""
    f = tmp_path / "outside" / "bashrc"
    f.parent.mkdir()
    f.write_text("original\n")
    return f


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def f():\n    return 1\n")
    git(root, "init", "-q")
    return root


def test_map_does_not_write_through_links_committed_in_the_store_folder(repo, victim, tmp_path):
    (repo / ".leyline").mkdir()
    (repo / ".leyline" / "map.html").symlink_to(victim)
    created = tmp_path / "outside" / "created"
    (repo / ".leyline" / ".gitignore").symlink_to(created)   # a link to nothing: writing it would make the file
    loop.map_repos([str(repo)], repo / ".leyline" / "leyline.db")
    assert victim.read_text() == "original\n"
    assert not created.exists()
    assert "<html" in (repo / ".leyline" / "map.html").read_text().lower()
    assert not (repo / ".leyline" / "map.html").is_symlink()


def test_a_snapshot_is_not_made_through_a_link_to_nothing(repo, tmp_path):
    from leyline import diff
    db = repo / ".leyline" / "leyline.db"
    loop.map_repos([str(repo)], db, page=False)
    made = tmp_path / "outside" / "made.db"
    made.parent.mkdir(exist_ok=True)
    (repo / ".leyline" / "snapshots").mkdir()
    (repo / ".leyline" / "snapshots" / "spec-x.db.part").symlink_to(made)
    con = store.connect(db)
    try:
        diff.snapshot(con, "spec-x")
    finally:
        con.close()
    assert not made.exists()


def test_a_change_page_and_anchors_are_not_written_through_links(tmp_path, victim):
    page = tmp_path / "leyline.md"
    page.symlink_to(victim)
    spec._write(page, "the plan")
    assert victim.read_text() == "original\n" and "the plan" in page.read_text()
    openspec = tmp_path / "openspec"
    openspec.mkdir()
    (openspec / drift.ANCHOR_FILE).symlink_to(victim)
    drift._write_file(openspec, {})
    assert victim.read_text() == "original\n"


def test_a_review_page_is_not_written_through_a_link(repo, victim):
    from leyline import pr
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text("def f():\n    return 2\n")
    git(repo, "commit", "-qam", "change")
    (repo / ".leyline" / "reviews").mkdir(parents=True)
    (repo / ".leyline" / "reviews" / "pr-feature.md").symlink_to(victim)
    r = pr.review(repo / ".leyline" / "leyline.db", repo, base)
    assert "error" not in r, r
    assert victim.read_text() == "original\n"


def test_skills_install_does_not_write_through_a_committed_manifest_link(repo, victim):
    folder = repo / ".claude" / "skills"
    folder.mkdir(parents=True)
    (folder / agent_skills.MANIFEST).symlink_to(victim)
    agent_skills.install(repo)
    assert victim.read_text() == "original\n"
    assert '"skills"' in (folder / agent_skills.MANIFEST).read_text()


def test_skills_install_force_does_not_write_through_a_committed_skill_file_link(repo, victim):
    s = agent_skills.available()[0]
    dest = repo / ".claude" / "skills" / s.name
    dest.mkdir(parents=True)
    (dest / "SKILL.md").symlink_to(victim)
    agent_skills.install(repo, force=True, names=[s.name])
    assert victim.read_text() == "original\n"
    assert (dest / "SKILL.md").read_bytes() == s.files()["SKILL.md"]


def test_a_readme_linked_to_a_file_outside_is_not_read_into_the_tour_or_page(repo, tmp_path):
    secret = tmp_path / "outside" / "credentials"
    secret.parent.mkdir(exist_ok=True)
    secret.write_text("aws_access_key_id = AKIAEXAMPLEEXAMPLE and a secret long enough to be a paragraph\n")
    (repo / "README.md").symlink_to(secret)
    db = repo / ".leyline" / "leyline.db"
    loop.map_repos([str(repo)], db)
    from leyline import tours
    con = store.connect(db)
    try:
        said = str([tours.get(con, t["id"]) for t in tours.listing(con)["tours"]])
    finally:
        con.close()
    assert "AKIAEXAMPLE" not in said
    assert "AKIAEXAMPLE" not in (repo / ".leyline" / "map.html").read_text()


def test_a_change_folder_does_not_read_files_linked_from_outside(tmp_path):
    secret = tmp_path / "outside" / "credentials"
    secret.parent.mkdir()
    secret.write_text("# AKIAEXAMPLEEXAMPLE\n\n- [ ] 1.1 AKIAEXAMPLEEXAMPLE\n")
    d = tmp_path / "repo" / "openspec" / "changes" / "add-x"
    (d / "specs" / "a").mkdir(parents=True)
    (d / "proposal.md").symlink_to(secret)
    (d / "tasks.md").symlink_to(secret)
    (d / "specs" / "a" / "spec.md").symlink_to(secret)
    assert "AKIAEXAMPLE" not in str(spec.parse(d))


def test_write_file_replaces_a_link_and_keeps_text_and_bytes(tmp_path, victim):
    p = tmp_path / "x.txt"
    p.symlink_to(victim)
    store.write_file(p, "text")
    assert p.read_text() == "text" and not p.is_symlink() and victim.read_text() == "original\n"
    store.write_file(p, b"\x00bytes")
    assert p.read_bytes() == b"\x00bytes"
    assert [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp")] == []
