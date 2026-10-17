"""Which tests reach a function: `expand` and `show` say so from the reverse call graph, so a test whose stored flow
was cut short (depth 8, 300 steps) still counts, and tests measured running it are said apart."""

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from leyline import cli, query, store
from leyline.indexer import index

PKG = "pkg"


def _chain(n: int) -> str:
    """f0 calls f1 calls ... calls target: deeper than a flow walks."""
    lines = [f"def f{k}():\n    return f{k + 1}()\n" for k in range(n - 1)]
    lines.append(f"def f{n - 1}():\n    return target()\n")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def mapped(tmp_path_factory):
    root = tmp_path_factory.mktemp("reach")
    files = {
        f"{PKG}/__init__.py": "",
        f"{PKG}/deep.py": "def target():\n    return 1\n\n\ndef lonely():\n    return 2\n\n\n" + _chain(12),
        f"{PKG}/near.py": "from pkg.deep import target\n\n\ndef near():\n    return target()\n",
        "app.py": "from pkg.near import near\n\nif __name__ == '__main__':\n    near()\n",
        "tests/test_near.py": "from pkg.near import near\n\n\ndef test_near_calls_target():\n    assert near() == 1\n",
        "tests/test_deep.py": "from pkg.deep import f0\n\n\ndef test_the_long_chain_ends_in_target():\n    assert f0() == 1\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    db = root / ".leyline" / "leyline.db"
    index(root, db, "rx")
    con = store.connect(db)
    yield con, db
    con.close()


def _id(con, name: str) -> str:
    found = query.find(con, name)
    assert "error" not in found, found
    return found["id"]


def test_expand_says_which_tests_reach_a_function(mapped):
    con, _ = mapped
    r = query.expand(con, _id(con, "deep.target"))
    names = {x["name"] for x in r["tests_reaching"]["items"]}
    assert r["tests_reaching"]["total"] == 2
    # the long chain's flow stops before target; the reverse call graph still finds the test
    assert names == {"test_near_calls_target", "test_the_long_chain_ends_in_target"}
    assert r["tests_reaching"]["items"][0]["name"] == "test_near_calls_target"   # nearest first
    for x in r["tests_reaching"]["items"]:
        assert query.find(con, x["id"])["id"] == x["id"]   # a node id another tool takes
    assert r["entries_reaching"]["total"] >= 1


def test_a_function_no_tests_reach_says_so(mapped):
    con, _ = mapped
    r = query.expand(con, _id(con, "lonely"))
    assert r["tests_reaching"] == {"total": 0, "items": []}
    assert r["entries_reaching"]["total"] == 0


def test_measured_tests_are_shown_apart_from_static_ones(mapped):
    con, db = mapped
    target = _id(con, "deep.target")
    assert "tests_measured" not in query.expand(con, target)
    with con:
        con.execute("INSERT INTO covered (run, test, test_id, node_id, lines) VALUES ('r', 'test_near_calls_target', ?, ?, 1)",
                    (_id(con, "test_near_calls_target"), target))
    try:
        r = query.expand(con, target)
        assert r["tests_measured"]["total"] == 1
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["--db", str(db), "show", "deep.target"])
        assert "tests measured running it (1): test_near_calls_target" in out.getvalue()
    finally:
        with con:
            con.execute("DELETE FROM covered")


def test_show_prints_the_tests_that_reach_a_function(mapped):
    _, db = mapped
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["--db", str(db), "show", "deep.target"])
    text = out.getvalue()
    assert "tests that reach it on the map (2): test_near_calls_target, test_the_long_chain_ends_in_target" in text
    assert "reached from (1): the top level of app.py" in text


def test_expand_reads_the_flow_steps_once(mapped):
    con, _ = mapped
    busiest = con.execute("SELECT callable_id FROM flow_steps GROUP BY callable_id ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
    seen = []
    con.set_trace_callback(seen.append)
    try:
        query.expand(con, busiest)
    finally:
        con.set_trace_callback(None)
    assert sum("flow_steps" in s for s in seen) == 1
