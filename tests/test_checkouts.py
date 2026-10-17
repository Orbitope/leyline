"""An agent working in another checkout of the server's repository (a git worktree) maps that checkout: the server
does not fold it onto the repository that contains it, reads its store from the map on, keeps the old store when the
map is refused, follows the store with its default paths, and runs no git for a path inside a mapped repository."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from test_server import Agent, TESTS, serve  # noqa: E402

from leyline import loop  # noqa: E402


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def checkouts(tmp_path):
    """main (a repository), a linked worktree under it, and another outside it."""
    main = tmp_path / "main"
    shutil.copytree(TESTS / "fixture2", main)
    git(main, "init", "-q")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "first")
    under = main / ".claude" / "worktrees" / "wt"
    git(main, "worktree", "add", "-q", str(under), "-b", "wt")
    away = tmp_path / "away"
    git(main, "worktree", "add", "-q", str(away), "-b", "away")
    return main, under, away


def store_of(checkout: Path) -> Path:
    return (checkout / ".leyline" / "leyline.db").resolve()


def test_a_linked_worktree_under_the_main_checkout_is_mapped_as_itself(checkouts):
    main, under, away = checkouts

    async def script(a: Agent, tools, init):
        m = await a.call("map", paths=[str(under)])
        assert Path(m["db"]) == store_of(under)                          # not folded onto main
        assert (await a.call("overview"))["repos"][0]["id"] == "wt"
        m = await a.call("map", paths=[str(away)])                        # a worktree outside the main checkout
        assert Path(m["db"]) == store_of(away)
        assert (await a.call("overview"))["repos"][0]["id"] == "away"
        assert "outside" in await a.fail("map", paths=[str(main.parent)])    # a folder that is no worktree is still refused
    serve(main, script)
    assert not (main / ".leyline").exists()


def test_mapping_another_checkout_moves_the_store_to_it(checkouts):
    main, under, away = checkouts

    async def script(a: Agent, tools, init):
        m = await a.call("map", paths=[str(under)])
        assert "until it restarts" in m["store_note"] and str(store_of(under)) in m["store_note"]
        assert store_of(under).is_file() and not (main / ".leyline").exists()
        assert (await a.call("search", text="Engine start"))["results"]       # later tools read that store
        back = await a.call("map", paths=["."])                               # the server's own checkout: its own store
        assert Path(back["db"]) == store_of(main) and "started with" in back["store_note"]
        assert (await a.call("overview"))["repos"][0]["id"] == "main"
    serve(main, script)


def test_a_refused_store_leaves_the_old_one_current(checkouts):
    main, under, away = checkouts
    (under / ".leyline").mkdir()
    (under / ".leyline" / "notes.txt").write_text("committed by the branch\n")
    git(under, "add", "-f", ".leyline/notes.txt")
    git(under, "commit", "-q", "-m", "tracks .leyline")

    async def script(a: Agent, tools, init):
        await a.call("map", paths=["."])
        refused = await a.fail("map", paths=[str(under)])
        assert ".leyline" in refused
        assert (await a.call("overview"))["repos"][0]["id"] == "main"          # still the store it read before
    serve(main, script)


def test_the_nothing_mapped_error_names_the_directory_the_server_started_in(checkouts):
    main, under, away = checkouts

    async def script(a: Agent, tools, init):
        said = await a.fail("overview")
        assert str(main.resolve()) in said and "another checkout" in said and "absolute path" in said and "`map`" in said
    serve(main, script)


@pytest.fixture
def inproc(checkouts, monkeypatch):
    """The server's functions called in this process, as if it had been started in main."""
    from leyline import server
    main, under, away = checkouts
    monkeypatch.setenv("LEYLINE_DB", str(main / ".leyline" / "leyline.db"))
    monkeypatch.setattr(server, "_START", main.resolve())
    monkeypatch.setattr(server, "_start_repo_cache", [])
    monkeypatch.setattr(server, "_worktrees", {"list": None})
    monkeypatch.setattr(server, "_moved", [None])
    monkeypatch.chdir(main)
    return server


def test_after_the_move_the_paths_the_server_defaults_to_follow_the_store(checkouts, inproc):
    main, under, away = checkouts
    for where in (main, under):                                   # the same change id in both checkouts
        (where / "openspec" / "changes" / "x").mkdir(parents=True)
    json.loads(inproc.map_code(paths=[str(under)]))
    assert inproc._home() == under.resolve()                      # what review_pr and drift look at with no path
    assert loop.find_change("x", inproc._path()) == under.resolve() / "openspec" / "changes" / "x"
    assert inproc._outside(main, "The directory") is None          # the server's own directory is still its own


def test_accepting_a_worktree_runs_no_git_per_call(checkouts, inproc, monkeypatch):
    main, under, away = checkouts
    json.loads(inproc.map_code(paths=["."]))
    asked = []
    monkeypatch.setattr(loop, "worktrees_of", lambda top: asked.append(top) or [])
    for inside in (main, main / "py", main / "py" / "src", under):
        assert inproc._outside(inside, "The path") is None
    assert asked == []
