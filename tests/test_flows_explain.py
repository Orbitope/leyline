"""Asking how something works: find_flows ranks where a described behavior starts, explain_path walks it step by step
across calls and channels, and every arrow of its diagram is a link on the map."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from leyline import cli, diagrams, explain, store
from leyline.indexer import index

NOTES = {
    "client/NoteEditor.tsx": (
        'import { putNote } from "./api";\n'
        "export function NoteEditor() {\n"
        "  // Saves the note the writer typed.\n"
        "  const onSave = () => {\n"
        '    putNote("n1", "text");\n'
        "  };\n"
        "  return <button onClick={onSave}>Save</button>;\n"
        "}\n"),
    "client/api.ts": (
        "export async function putNote(id: string, body: string) {\n"
        '  return fetch(`/api/notes/${id}`, { method: "PUT", body });\n'
        "}\n"),
    "server/server.ts": (
        'import Fastify from "fastify";\n'
        'import { storeNote } from "./notes";\n'
        "export function build() {\n"
        "  const server = Fastify();\n"
        '  server.put("/api/notes/:id", async (req) => {\n'
        "    storeNote(req.params.id);\n"
        "    return { ok: true };\n"
        "  });\n"
        "  return server;\n"
        "}\n"),
    "server/notes.ts": (
        'import { db } from "./db";\n'
        "/** Persists one note in the notes table. */\n"
        "export function storeNote(id: string) {\n"
        "  checkId(id);\n"
        '  db.exec("INSERT INTO notes (id) VALUES (?)", id);\n'
        "}\n"
        "export function checkId(id: string) {\n"
        "  return id.length > 0;\n"
        "}\n"
        "export function listNotes() {\n"
        '  return db.query("SELECT id FROM notes");\n'
        "}\n"),
    "server/db.ts": "export const db = { exec(sql: string, ...a: unknown[]) {}, query(sql: string) { return []; } };\n",
    "tool/export.py": (
        '"""Export every note to a file."""\n'
        "import sys\n\n\n"
        "def main():\n"
        "    print(sys.argv)\n\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n"),
    "tool/test_export.py": (
        "from export import main\n\n\n"
        "def test_export_prints_the_arguments():\n"
        "    main()\n"),
}


def write(root: Path, files: dict) -> None:
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)


@pytest.fixture(scope="module")
def notes(tmp_path_factory):
    root = tmp_path_factory.mktemp("notes")
    write(root, NOTES)
    db = root / ".leyline" / "leyline.db"
    index(root, db, "notes")
    con = store.connect(db)
    yield root, db, con
    con.close()


def node(con, name: str) -> str:
    return con.execute("SELECT id FROM nodes WHERE name = ? AND kind IN ('callable', 'test') ORDER BY length(id)",
                       (name,)).fetchone()[0]


# -- words ---------------------------------------------------------------------------------------------------------
def test_words_are_split_stemmed_and_matched_loosely():
    assert explain.split_words("saveDialogue save_dialogue HTTPServer") == ["save", "dialogue", "save", "dialogue",
                                                                             "http", "server"]
    assert explain.stem("saves") == explain.stem("saved") == explain.stem("saving") == explain.stem("save")
    assert explain.stem("validation") == explain.stem("validate") == explain.stem("validator")
    assert explain.stem("stepping") == explain.stem("step") and explain.stem("process") == explain.stem("processes")
    assert explain.stem("apply") == "apply"
    t = {x["word"]: x for x in explain.terms("what happens when a writer saves a note in the editor")}
    assert set(t) == {"writer", "saves", "note", "editor"}
    assert t["writer"]["weak"] and not t["writer"]["same"]   # who does it counts little and has no synonyms
    assert "persist" in t["saves"]["same"] and "put" in t["saves"]["same"]
    assert t["editor"]["same"] == []                          # an editor is not an edit
    assert explain.terms("how does it work") == []


def test_a_comment_above_and_a_docstring_below_are_read():
    lines = ["# Saves a note.", "@decorator", "def save(x):", '    """Writes it to disk."""', "    pass"]
    assert explain.leading_doc(lines, 3) == "Saves a note. Writes it to disk."
    assert explain.leading_doc(["/** Persists one note. */", "export function f() {"], 2) == "Persists one note."


# -- find_flows ----------------------------------------------------------------------------------------------------
def test_the_handler_and_the_route_rank_first_with_why(notes):
    _, _, con = notes
    r = explain.find_flows(con, "what happens when a writer saves a note")
    top = r["candidates"]
    names = [c["name"] for c in top]
    assert names[0] in ("NoteEditor.onSave", "PUT /api/notes/:id", "storeNote"), names
    by = {c["name"]: c for c in top}
    assert by["NoteEditor.onSave"]["kind"] == "UI event handler"
    assert by["PUT /api/notes/:id"]["kind"] == "route handler for PUT /api/notes/:id"
    assert "saves (as put, which means the same here) in its route" in by["PUT /api/notes/:id"]["why"]
    assert "saves (as store, which means the same here) in its name" in by["storeNote"]["why"]
    assert "writer in its comment" in by["NoteEditor.onSave"]["why"]   # "Saves the note the writer typed."
    assert by["storeNote"]["at"] == "server/notes.ts:3"
    assert r["next"].startswith("explain_path with start=")


def test_entry_points_and_tests_come_with_the_flows_that_start_there(notes):
    _, _, con = notes
    r = explain.find_flows(con, "export notes to a file")
    entry = next(c for c in r["candidates"] if c["kind"].startswith("program entry"))
    assert entry["name"] == "top level of tool/export.py" and entry["flows"]
    t = explain.find_flows(con, "the export prints its arguments")["candidates"]
    test = next(c for c in t if c["kind"] == "test")
    assert test["flows"] == ["flow:" + test["id"]]
    main = next(c for c in t if c["name"] == "main")
    assert main["reached_from"] and main["reached_from"][0]["flow"].startswith("flow:")


def test_nothing_to_look_for_is_an_error_and_unknown_words_find_nothing(notes):
    _, _, con = notes
    assert "error" in explain.find_flows(con, "how does it work")
    r = explain.find_flows(con, "quaternion slerp")
    assert r["candidates"] == [] and "Nothing on the map" in r["note"]


# -- explain_path ----------------------------------------------------------------------------------------------------
def test_the_walk_crosses_http_and_marks_the_table_written_for_later(notes):
    _, _, con = notes
    r = explain.explain_path(con, "NoteEditor.onSave")
    names = [s["name"] for s in r["steps"]]
    assert names[:4] == ["NoteEditor.onSave", "putNote", "the PUT /api/notes/:id handler", "storeNote"], names
    hop = r["steps"][2]
    assert hop["how"] == "http PUT /api/notes/:id" and hop["crosses"] == "to another process or service"
    assert hop["call"].startswith("return fetch(") and hop["call_line"] == "client/api.ts:2"
    assert hop["declaration"].startswith('server.put("/api/notes/:id"')
    assert r["steps"][1]["call"] == 'putNote("n1", "text");' and r["steps"][1]["at"] == "client/api.ts:1"
    store_step = r["steps"][3]
    later = store_step["later_elsewhere"][0]
    assert "table notes" in later["what"] and later["reader"] == "listNotes"
    assert any("later_elsewhere" in n for n in r["not_seen"])
    assert "listNotes" not in names   # a reader of the table does not run as part of the walk
    # the diagram draws the walk, and every arrow is a link on the map
    assert "-)" in r["mermaid"] and "http PUT /api/notes/:id" in r["mermaid"]
    d = explain.walk_diagram(con, diagrams._Nodes(con), *_walk_parts(con, "NoteEditor.onSave"))
    assert d["arrows"] and diagrams.unbacked(con, d) == []
    assert "1. NoteEditor.onSave" in r["text"] and "What the map could not see:" in r["text"]


def _walk_parts(con, start):
    g = explain.graph(con)
    steps, _, _ = explain._walk(con, g, explain._resolve(con, start)["id"])
    return steps, {s["seq"]: s for s in steps}, g


def test_a_path_to_a_target_and_through_a_node(notes):
    _, _, con = notes
    r = explain.explain_path(con, "NoteEditor.onSave", to="checkId")
    assert r["mode"] == "path" and [s["name"] for s in r["steps"]][-1] == "checkId"
    assert [s["how"] for s in r["steps"]] == ["start", "call", "http PUT /api/notes/:id", "call", "call"]
    t = explain.explain_path(con, "putNote", through="PUT /api/notes/:id")
    assert t["mode"] == "through" and t["steps"][1]["name"] == "the PUT /api/notes/:id handler"
    assert "checkId" in [s["name"] for s in t["steps"]]   # and on from it
    gone = explain.explain_path(con, "listNotes", to="putNote")
    assert gone["found"] is False and "No path on the map" in gone["note"]


def test_a_stored_flow_is_followed_and_helpers_are_counted(notes, monkeypatch):
    _, _, con = notes
    r = explain.explain_path(con, "flow:" + node(con, "test_export_prints_the_arguments"))
    assert r["flow"] and [s["name"] for s in r["steps"]][:2] == ["test_export_prints_the_arguments", "main"]
    monkeypatch.setattr(explain, "HELPER_CALLERS", 1)   # checkId has one caller: now it counts as a helper
    h = explain.explain_path(con, "storeNote")
    assert "checkId" in h["helpers"]["names"] and len(h["steps"]) == 1
    assert any("Nothing on the map is called" in n for n in explain.explain_path(con, "checkId")["not_seen"])


def test_max_steps_keeps_the_channel_hops_and_says_what_was_left(notes):
    _, _, con = notes
    r = explain.explain_path(con, "NoteEditor.onSave", max_steps=3)
    assert [s["how"] for s in r["steps"]] == ["start", "call", "http PUT /api/notes/:id"]
    assert r["left_out"] >= 1 and r["shown"] == 3


def test_bad_starting_points_are_errors_with_candidates(notes):
    _, _, con = notes
    assert "nothing on the map" in explain.explain_path(con, "nosuchthing")["error"]
    assert explain.explain_path(con, "NoteEditor.onSave", to="nosuchthing")["error"].startswith("to:")
    assert explain._resolve(con, "PUT /api/notes/:id")["id"].endswith("route:PUT /api/notes/:id")


ITEMS = {
    "web/app.py": (
        "class App:\n"
        "    def route(self, path):\n"
        "        return lambda f: f\n\n\n"
        "app = App()\n\n\n"
        "@app.route(\n"
        '    "/items/<int:item_id>",\n'
        ")\n"
        "def show(item_id):\n"
        "    return load(item_id)\n\n\n"
        "def load(item_id):\n"
        "    return item_id\n"),
    "web/client.py": (
        "def fetch(session):\n"
        '    return session.get("/items/3")\n'),
}


@pytest.fixture(scope="module")
def items(tmp_path_factory):
    root = tmp_path_factory.mktemp("items")
    write(root, ITEMS)
    db = root / ".leyline" / "leyline.db"
    index(root, db, "items")
    con = store.connect(db)
    yield con
    con.close()


def test_a_step_declares_the_function_not_its_decorator(items):
    r = explain.explain_path(items, "show")
    assert r["steps"][0]["declaration"] == "def show(item_id):", r["steps"][0]


def test_diagram_of_any_ids(notes):
    _, _, con = notes
    d = explain.diagram(con, ["storeNote", "nosuchthing"])
    assert d["mermaid"].startswith("sequenceDiagram") and "focus: notes.ts.storeNote" in d["mermaid"]
    assert d["missing"][0]["name"] == "nosuchthing" and "shaded" in d["legend"]
    assert "error" in explain.diagram(con, ["nosuchthing"])


def test_the_index_is_kept_between_questions(notes):
    _, _, con = notes
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('generation', 'g1')")
    assert explain.index(con) is explain.index(con)
    assert explain.graph(con) is explain.graph(con)


def test_the_commands(notes, capsys):
    root, db, _ = notes
    assert cli.main(["--db", str(db), "find-flows", "a writer saves a note"]) == 0
    out = capsys.readouterr().out
    assert "Where \"a writer saves a note\" could start" in out and "Next: leyline explain-path" in out
    assert cli.main(["--db", str(db), "explain-path", "NoteEditor.onSave", "--steps", "5"]) == 0
    out = capsys.readouterr().out
    assert "http PUT /api/notes/:id -> the PUT /api/notes/:id handler" in out and "```mermaid" in out
    assert cli.main(["--db", str(db), "explain-path", "nosuchthing"]) == 1
    assert cli.main(["--db", str(db), "diagram", "storeNote", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mermaid"].startswith("sequenceDiagram")


def test_the_skill_names_the_tools_and_their_commands():
    root = Path(__file__).resolve().parents[1]
    text = (root / "skills" / "leyline-explain-flow" / "SKILL.md").read_text()
    for said in ("`find_flows(description)`", "`leyline find-flows \"<description>\"`", "`explain_path(start",
                 "`leyline explain-path <start>", "`leyline diagram", "save_tour", "Every claim cites a node"):
        assert said in text, said
    readme = (root / "README.md").read_text()
    assert "### Asking how something works" in readme and "| `find_flows(description, limit?)` |" in readme
