"""An agent says how much of an answer it can afford: the read tools that do not size themselves take
`budget_tokens` and cut their answer to it; `context` keeps sizing its own outline."""

import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from leyline import cli, store, tours  # noqa: E402
from leyline.indexer import index  # noqa: E402

HERE = Path(__file__).parent
SMALLEST = 600


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    root = tmp_path_factory.mktemp("budgets") / "repo"
    shutil.copytree(HERE / "fixture2", root)
    path = root / ".leyline" / "leyline.db"
    index(root, path, "f2")
    return path


@pytest.fixture
def server(db, monkeypatch):
    monkeypatch.setenv("LEYLINE_DB", str(db))
    from leyline import server as s
    return s


def _calls(s):
    return {
        "overview": lambda **b: s.overview(**b),
        "expand": lambda **b: s.expand(node_id="Engine", **b),
        "impact": lambda **b: s.impact(node_id="Engine.start", **b),
        "module_outline": lambda **b: s.module_outline(**b),
        "explain_path": lambda **b: s.explain_path(start="Engine.start", **b),
        "find_flows": lambda **b: s.find_flows(description="engine start", **b),
        "neighbors": lambda **b: s.neighbors(node_id="Engine.start", **b),
    }


def _body(s, text: str) -> int:
    """The answer's length without the fields that say what was cut."""
    return len(s._dump({k: v for k, v in json.loads(text).items() if k not in ("cut", "more")}))


def test_a_read_tool_answers_within_the_budget_it_was_given(server):
    for name, call in _calls(server).items():
        usual = call()
        small = call(budget_tokens=SMALLEST)
        assert _body(server, small) <= SMALLEST * 4, name
        big = json.loads(call(budget_tokens=6000))
        big.pop("seconds", None)   # find_flows says how long it took
        usual = json.loads(usual)
        usual.pop("seconds", None)
        if len(server._dump(usual)) <= 6000 * 4:
            assert big == usual, name   # a budget above the answer changes nothing
        else:
            assert "cut" in big, name   # and one below it never makes it longer


def test_a_budget_below_the_default_cuts_lists_and_says_so(server):
    for name in ("module_outline", "find_flows", "expand"):
        call = _calls(server)[name]
        usual = call()
        assert len(usual) > SMALLEST * 4, f"{name} answers in {len(usual)} characters: too small to need a cut"
        r = json.loads(call(budget_tokens=SMALLEST))
        assert r["cut"], name
        assert "budget_tokens" in r["more"], name
        assert "cut" not in json.loads(usual), name


def test_context_keeps_its_own_budget(server):
    r = json.loads(server.context(focus=["Engine.start"], budget_tokens=500))
    assert r["tokens"] <= 500
    assert "Left out:" in r["text"]
    assert "cut" not in r


def test_tour_prints_json_when_asked(db):
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["--db", str(db), "tour", "--json"]) == 0
    printed = json.loads(out.getvalue())
    con = store.connect(db)
    try:
        assert printed == json.loads(json.dumps(tours.get(con, printed["id"])))
    finally:
        con.close()
    assert printed["stops"]
