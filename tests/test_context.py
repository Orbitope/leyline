"""`leyline context`: an outline of the code around a focus, ranked by personalized PageRank and cut to a token budget,
for an agent about to edit that code."""

import json
import shutil
from pathlib import Path

import pytest

from leyline import context, store
from leyline.cli import main
from leyline.indexer import index
from test_pr import branch, run as pr_run   # noqa: F401  (a repository with a reviewed branch)

FIXTURE2 = Path(__file__).parent / "fixture2"


@pytest.fixture
def con(tmp_path):
    work = tmp_path / "repo"
    shutil.copytree(FIXTURE2, work)
    db = work / ".leyline/leyline.db"
    index(str(work), str(db))
    c = store.connect(db)
    yield c
    c.close()


def lines(text):
    return text.splitlines()


def test_a_function_shows_its_callers_tests_and_neighbours_as_declarations(con):
    r = context.build(con, ["Engine.start"], 600)
    text = r["text"]
    assert r["focus"] == [{"focus": "Engine.start", "as": "name", "ids": ["repo:python:py.src.pkg.core.Engine.start"]}]
    assert "> " + "  def start(self)" in lines(text)            # the focus, marked, indented under its class
    assert "  class Engine(Base)" in lines(text)
    assert "return self.name" not in text                      # declarations, never bodies
    # the tests that call it, said so; files ordered with the focus's first
    i = lines(text).index("py/tests/test_engine.py")
    assert "  def test_start(engine)" in lines(text)[i:] and "-- calls Engine.start" in text
    assert lines(text)[2] == "py/src/pkg/core.py"
    assert "called from 3 places" in text
    assert text.rstrip().splitlines()[-2].startswith("Shown: ") and "Left out:" in text
    assert r["tokens"] <= 600


def test_channel_ends_are_said_in_words(con):
    text = context.build(con, ["show"], 400)["text"]
    assert "> def show(item_id)" in text and "-- answers GET /items/<int:item_id>" in text
    assert "  def fetch(session)" in text and "-- requests GET /items/<int:item_id>" in text
    assert "-- writes file reports/*.parity.json" in text and "-- reads file reports/*.parity.json" in text


def test_the_budget_decides_how_much_and_says_what_was_left_out(con):
    small, big = context.build(con, ["Engine.start"], 200), context.build(con, ["Engine.start"], 2000)
    assert small["tokens"] <= 200 and small["shown"]["symbols"] < big["shown"]["symbols"]
    assert "def start(self)" in small["text"]                   # the focus is always shown
    left = small["left_out"]
    assert left["direct"] + left["two_links_away"] > 0 and left["nearest"]
    assert f"Left out: {left['direct']} more symbol" in small["text"] and "Raise the budget" in small["text"]
    assert big["left_out"]["direct"] == 0
    # what the small outline left out is what the big one adds, nearest first
    assert left["nearest"][0]["name"] in big["text"]


def test_focus_by_id_path_words_and_several_at_once(con):
    by_id = context.build(con, ["repo:python:py.src.pkg.core.Engine.start"], 500)
    assert by_id["focus"][0]["as"] == "node" and "> " in by_id["text"]
    by_file = context.build(con, ["py/src/pkg/core.py"], 500)
    marked = [ln for ln in lines(by_file["text"]) if ln.startswith("> ")]
    assert any("class Engine" in ln for ln in marked) and any("def make_engine" in ln for ln in marked)
    words = context.build(con, ["engine start"], 500)
    assert words["focus"][0]["as"] == "search" and "repo:python:py.src.pkg.core.Engine.start" in words["focus"][0]["ids"]
    both = context.build(con, ["show", "Journal.note", "no_such_thing_anywhere"], 800)
    assert {f["focus"] for f in both["focus"] if "ids" in f} == {"show", "Journal.note"}
    assert "Not found: 'no_such_thing_anywhere'" in both["text"]
    assert "error" in context.build(con, ["no_such_thing_anywhere"], 500)


def test_the_graph_is_read_once_per_index_run(con, tmp_path):
    g = context.graph(con)
    assert context.graph(con) is g
    index(str(tmp_path / "repo"), str(tmp_path / "repo/.leyline/leyline.db"), full=True)
    assert context.graph(con) is not g


def test_a_change_focuses_on_the_code_it_marked(branch, monkeypatch):
    monkeypatch.chdir(branch)
    assert pr_run("pr", "main")[0] == 0
    c = store.connect(branch / ".leyline/leyline.db")
    r = context.build(c, ["pr-feature"], 800)
    assert r["focus"][0]["as"] == "change pr-feature"
    marked = [ln for ln in lines(r["text"]) if ln.startswith("> ")]
    assert any("def load(path, encoding)" in ln for ln in marked)
    assert "def count(p)" in r["text"]                          # the caller the branch forgot, next to it
    assert "-- answers GET /api/items" in r["text"] and "-- requests GET /api/items" in r["text"]
    c.close()


def test_the_command_line(con, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path / "repo")
    assert main(["context", "Engine.start", "--tokens", "300"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Code around Engine.start") and "def start(self)" in out and len(out) <= 300 * 4
    assert main(["context", "show", "fetch", "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["shown"]["symbols"] >= 2 and r["text"]
    assert main(["context", "no_such_thing_anywhere"]) == 1


def test_what_was_not_found_and_left_out_stays_inside_the_budget(con, tmp_path):
    # A focus item not on the map is named at the end, and so are the nearest symbols left out: both count against
    # the budget, however long the item or the paths.
    r = context.build(con, ["Engine.start", "qqqzzz" * 150], 200)
    assert "Not found: 'qqqzzz" in r["text"] and r["tokens"] <= 200, r["tokens"]
    deep = tmp_path / "deep"
    folder = deep / "/".join(["a_rather_long_folder_name_for_this_test"] * 4)
    folder.mkdir(parents=True)
    (folder / "core.py").write_text("def target():\n    return 1\n")
    for k in range(12):
        (folder / f"caller_with_a_long_file_name_number_{k}.py").write_text(
            f"from .core import target\n\n\ndef caller_with_a_long_function_name_{k}():\n    return target()\n")
    db = deep / ".leyline/leyline.db"
    index(str(deep), str(db))
    c = store.connect(db)
    r = context.build(c, ["target"], 200)
    c.close()
    assert "Left out:" in r["text"] and r["tokens"] <= 200, r["tokens"]
