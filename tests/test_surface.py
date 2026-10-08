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
