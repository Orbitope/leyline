"""The surface an agent or a person reads: the command line's exit codes, and the MCP tools' answers called in this
process (test_server.py drives them over stdio)."""

import json
import shutil
from pathlib import Path

import pytest

from leyline import store
from leyline.cli import main
from leyline.indexer import index

FIXTURE2 = Path(__file__).parent / "fixture2"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    work = tmp_path / "repo"
    shutil.copytree(FIXTURE2, work)
    index(str(work), str(work / ".leyline/leyline.db"))
    monkeypatch.chdir(work)
    return work


def test_an_unknown_id_fails_the_command(repo, capsys):
    for cmd in (["expand", "nope"], ["neighbors", "nope"], ["source", "nope"], ["review", "nope"]):
        assert main(cmd) == 1, cmd
        out = capsys.readouterr()
        assert "nope" in out.out + out.err
    assert main(["expand", "repo:python:py.src.pkg.core.Engine.start"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "start"
    assert main(["source", "repo:python:py.src.pkg.core.Engine.start"]) == 0
    assert "def start(self)" in capsys.readouterr().out


@pytest.fixture
def tools(monkeypatch):
    """The MCP tools, called in this process against the store at LEYLINE_DB."""
    pytest.importorskip("mcp")
    from leyline import server

    def use(db):
        monkeypatch.setenv("LEYLINE_DB", str(db))
        server._generation[0] += 1   # each thread opens the store afresh
        return server
    return use


@pytest.fixture
def busy(tmp_path):
    """A function called directly from 20 functions in its own module and run by 45 tests."""
    root = tmp_path / "busy"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg/__init__.py").write_text("")
    (root / "pkg/core.py").write_text("def target():\n    return 1\n")
    for k in range(20):
        (root / f"pkg/c{k}.py").write_text(f"from .core import target\n\n\ndef caller_{k}():\n    return target()\n")
    (root / "tests/test_target.py").write_text("from pkg.core import target\n" + "".join(
        f"\n\ndef test_target_{k}():\n    assert target() == 1\n" for k in range(45)))
    db = root / ".leyline/leyline.db"
    index(str(root), str(db))
    return db


def test_impact_counts_every_direct_caller_and_lists_as_many_flows_as_asked(busy, tools):
    server = tools(busy)
    tid = json.loads(server.search(text="target", kind="callable"))["results"][0]["id"]
    r = json.loads(server.impact(node_id=tid, limit=100))
    pkg = next(m for m in r["by_module"]["items"] if m["module"] == "pkg")
    assert len(pkg["direct"]) == 5 and pkg["direct_more"] == 15     # 20 callers in all
    assert "direct_total" not in pkg
    assert r["flows_through"]["total"] == 45 and len(r["flows_through"]["items"]) == 45


def test_an_answer_of_many_middling_strings_or_short_lists_is_still_cut_to_fit():
    """Strings just over 1,000 characters, or lists of 3 big items, used to be cut to their floor and picked again
    until the tries ran out, and the answer came back many thousands of characters over the limit."""
    pytest.importorskip("mcp")
    from leyline import server

    strings = {f"k{i}": "x" * 1100 for i in range(40)}           # 44,000 characters, and no list to cut
    text = server._fit(strings, {})
    out = json.loads(text)
    assert len(text) <= server.LIMIT + 300, len(text)
    assert out["cut"]["k0"].endswith("of 1,100 characters")      # the whole length, not the length after a cut
    assert "1,100 characters in all" in out["k0"]
    three = ["y" * 5000] * 3
    nested = {"a": [[three] * 3] * 3}                               # 135,000 characters in lists of 3
    text = server._fit(nested, {})
    assert len(text) <= server.LIMIT + 300, len(text)
