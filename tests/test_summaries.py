"""What each function and type is for: the comment or docstring its author wrote, kept on the map at index time, and
an annotation a person or an agent wrote after reading the code, which wins. Every tool that shows a node shows it,
and `find_flows` ranks by it."""

import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from leyline import cli, context, explain, outline, query, store, summaries
from leyline.indexer import index

LONG = ("Walks every file of the repository in the order the index reads them and, for each one, works out what it"
        " declares, what it calls and what it imports, which takes a while on a large tree.")
FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        "def answers():\n    return 1\n\n\n"
        "def find_flows():\n    \"\"\"Answer a question about the server from the map. It also says why.\"\"\"\n    return 2\n\n\n"
        "# Sums two numbers. Never more than two.\n"
        "def plain(a, b):\n    return a + b\n\n\n"
        "def helper():\n    return 0\n\n\n"
        f"def long_story():\n    \"\"\"{LONG}\"\"\"\n    return 4\n"),
    "pkg/other.py": (
        "from pkg.core import plain, helper\n\n\n"
        "def call_plain():\n    return plain(1, 2) + helper()\n\n\n"
        "def other_helper():\n    return helper()\n"),
    "pkg/third.py": "from pkg.core import helper\n\n\ndef third():\n    return helper()\n",
    "tests/test_core.py": "from pkg.core import plain\n\n\ndef test_plain_adds():\n    assert plain(1, 2) == 3\n",
}


def write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


@pytest.fixture
def mapped(tmp_path):
    root = tmp_path / "repo"
    write(root, FILES)
    db = tmp_path / "s.db"
    index(root, db, "s")
    con = store.connect(db)
    yield root, db, con
    con.close()


def node(con, name: str) -> str:
    found = query.find(con, name)
    assert "error" not in found, found
    return found["id"]


def test_the_docstring_of_a_function_is_on_the_map(mapped):
    root, db, con = mapped
    attrs = json.loads(con.execute("SELECT attrs FROM nodes WHERE id = ?", (node(con, "find_flows"),)).fetchone()[0])
    assert attrs["doc"] == "Answer a question about the server from the map. It also says why."
    above = json.loads(con.execute("SELECT attrs FROM nodes WHERE id = ?", (node(con, "plain"),)).fetchone()[0])
    assert above["doc"] == "Sums two numbers. Never more than two."          # a comment above works the same
    assert "doc" not in json.loads(con.execute("SELECT attrs FROM nodes WHERE id = ?", (node(con, "answers"),)).fetchone()[0] or "{}")


def test_a_comment_edited_above_a_function_re_parses_only_its_own_file(tmp_path):
    root = tmp_path / "repo"
    write(root, FILES)
    db = tmp_path / "s.db"
    index(root, db, "s")
    core = root / "pkg" / "core.py"
    core.write_text(core.read_text().replace("# Sums two numbers. Never more than two.", "# Adds two numbers."))
    docs = core.read_text().replace("def helper():\n    return 0", "# Gives zero.\ndef helper():\n    return 0")
    core.write_text(docs)                                      # a comment above a name that other files call too
    second = index(root, db, "s")["incremental"]
    assert second["mode"] == "incremental"
    assert second["files_resolved"] <= 1                       # its own file, and no file that mentions `helper`
    con = store.connect(db)
    try:
        assert summaries.for_nodes(con, [node(con, "plain"), node(con, "helper")]) == {
            node(con, "plain"): "Adds two numbers.", node(con, "helper"): "Gives zero."}
    finally:
        con.close()


def test_an_annotation_overrides_the_docstring_in_a_summary(mapped):
    root, db, con = mapped
    fid, helper = node(con, "find_flows"), node(con, "helper")
    assert summaries.for_nodes(con, [fid]) == {fid: "Answer a question about the server from the map."}   # first sentence
    assert query.annotate(con, fid, "responsibility", "Finds where a behavior starts.", [helper])["key"] == "responsibility"
    assert summaries.for_nodes(con, [fid]) == {fid: "Finds where a behavior starts."}
    query.annotate(con, fid, "responsibility", "The person says: it picks the start of a flow.", layer="intent")
    assert summaries.for_nodes(con, [fid]) == {fid: "The person says: it picks the start of a flow."}   # intent first
    with con:
        con.execute("UPDATE annotations SET stale = 1 WHERE node_id = ?", (fid,))
    assert summaries.for_nodes(con, [fid]) == {fid: "Answer a question about the server from the map."}  # stale loses
    assert summaries.for_nodes(con, [node(con, "answers")]) == {}                                         # nothing to say
    long = summaries.for_nodes(con, [node(con, "long_story")], width=60)
    assert len(next(iter(long.values()))) <= 60 and next(iter(long.values())).endswith("…")


def test_search_and_expand_say_what_a_function_does(mapped):
    root, db, con = mapped
    fid = node(con, "find_flows")
    query.annotate(con, fid, "responsibility", "Finds where a behavior starts.", [node(con, "helper")])
    hit = next(r for r in query.search(con, "find_flows")["results"] if r["id"] == fid)
    assert hit["summary"] == "Finds where a behavior starts."          # the annotation, not the docstring
    got = query.expand(con, node(con, "helper"))
    assert "summary" not in got                                          # `helper` has no doc
    callers = {x["name"]: x for x in got["called_by"]["items"]}
    assert "summary" not in callers["call_plain"]
    plain = query.expand(con, node(con, "plain"))
    assert plain["summary"] == "Sums two numbers."
    assert plain["doc"] == "Sums two numbers. Never more than two."    # the whole doc stays where it says more
    assert {x["name"]: x.get("summary") for x in plain["called_by"]["items"]}["call_plain"] is None
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["--db", str(db), "search", "find_flows"])
        cli.main(["--db", str(db), "show", "plain"])
    assert "Finds where a behavior starts." in out.getvalue() and "Sums two numbers." in out.getvalue()


def test_the_context_outline_says_what_the_focus_does(mapped):
    root, db, con = mapped
    r = context.build(con, ["plain"], 400)
    assert "-- Sums two numbers." in r["text"]
    assert r["tokens"] <= 400
    tiny = context.build(con, ["plain"], 200)                           # too small for anything but the focus's own lines
    assert tiny["tokens"] <= 200 and "def plain" in tiny["text"]


def test_the_busiest_functions_of_an_outline_carry_their_summary(mapped):
    root, db, con = mapped
    leaf = outline.outline(con, "s:file:pkg/core.py")
    key = {k["name"]: k for k in leaf["key"]}
    assert key["plain"]["summary"] == "Sums two numbers."
    assert "summary" not in key["answers"]
    assert "      Sums two numbers." in outline.text(leaf)


def test_each_step_of_a_walk_says_what_it_does(mapped):
    root, db, con = mapped
    walk = explain.explain_path(con, "call_plain")
    steps = {s["name"]: s for s in walk["steps"]}
    assert steps["plain"]["summary"] == "Sums two numbers."
    assert "summary" not in steps["helper"]
    assert "Sums two numbers." in walk["text"]
    long = explain.explain_path(con, node(con, "long_story"))["steps"][0]["summary"]
    assert len(long) <= 120 and long.endswith("…")


def test_a_match_on_several_words_outranks_a_match_on_one_word_in_a_name(mapped):
    root, db, con = mapped
    got = explain.find_flows(con, "how the server answers a question", 5)["candidates"]
    names = [c["name"] for c in got]
    assert names[0] == "find_flows" and "answers" in names
    assert any("its comment" in w for w in got[0]["why"])


def test_an_annotation_written_after_the_map_is_found_at_once(mapped):
    root, db, con = mapped
    assert explain.find_flows(con, "totals the invoice lines for the billing run")["candidates"] == [] or \
        all(c["name"] != "helper" for c in explain.find_flows(con, "totals the invoice lines for the billing run")["candidates"])
    query.annotate(con, node(con, "helper"), "responsibility", "Totals the invoice lines for the billing run.",
                   [node(con, "plain")])
    got = explain.find_flows(con, "totals the invoice lines for the billing run")["candidates"]
    assert got[0]["name"] == "helper"
    assert any("its note" in w for w in got[0]["why"])
