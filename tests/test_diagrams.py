"""Sequence diagrams drawn from the map: every arrow an edge the map holds, in source order, with channels and guesses
drawn as such, and the calls and channel links a change gained and lost."""

import io
import json
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from leyline import cli, diagrams, diff, spec, store
from leyline.indexer import index

FIXTURE5 = Path(__file__).parent / "fixture5"

APP = {
    "app/__init__.py": "",
    "app/main.py": "from app.service import Service\n\n\ndef main():\n    s = Service()\n    s.run(\"x\")\n\n\n"
                   "if __name__ == \"__main__\":\n    main()\n",
    "app/service.py": "from app.store import load, save\n\n\nclass Service:\n    def run(self, name):\n        data = load(name)\n"
                      "        self.check(data)\n        save(name, data)\n        return data\n\n"
                      "    def check(self, data):\n        return bool(data)\n",
    "app/store.py": "import sqlite3\n\nDB = sqlite3.connect(\":memory:\")\n\n\ndef load(name):\n"
                    "    return DB.execute(\"SELECT body FROM items WHERE name = ?\", (name,)).fetchall()\n\n\n"
                    "def save(name, data):\n    DB.execute(\"INSERT INTO items (name, body) VALUES (?, ?)\", (name, data))\n",
}


def write(root: Path, files: dict) -> None:
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)


def node(con, name: str) -> str:
    return con.execute("SELECT id FROM nodes WHERE name = ? AND kind = 'callable' ORDER BY length(id)", (name,)).fetchone()[0]


@pytest.fixture
def app(tmp_path):
    root, db = tmp_path / "app-repo", tmp_path / "s.db"
    write(root, APP)
    index(root, db, "app")
    con = store.connect(db)
    yield root, db, con
    con.close()


def messages(d: dict) -> list[str]:
    return [ln.strip() for ln in d["mermaid"].split("\n") if ":" in ln and not ln.strip().startswith(("participant", "Note"))]


def test_the_path_runs_from_the_entry_point_through_the_changed_code_in_source_order(app):
    _, _, con = app
    d = diagrams.sequence(con, [node(con, "run")])
    assert d["mermaid"].startswith("sequenceDiagram\n")
    assert d["participants"] == ["main.py", "Service", "store.py"]
    assert "Note over P1: starts at the entry point app/main.py" in d["mermaid"]
    # main calls run, and run calls load, check and save in the order its text does
    assert messages(d) == ["P1->>P1: main()", "P1->>P2: run()", "P2->>P3: load()", "P2->>P2: check()", "P2->>P3: save()"]
    # the changed function's run is shaded, from the arrow into it to the end of what it calls
    body = d["mermaid"].split("\n")
    assert body.index("    rect " + diagrams.SHADE) == body.index("    P1->>P2: run()") - 1
    assert body[body.index("    P1->>P2: run()") + 1] == "    Note over P2: changed: Service.run"
    assert body[body.index("    P2->>P3: save()") + 1] == "    end"
    assert diagrams.unbacked(con, d) == []
    assert not d["guessed"] and "dotted" not in diagrams.legend(d)


def test_a_table_is_drawn_as_a_channel_and_never_as_a_call(app):
    _, _, con = app
    d = diagrams.sequence(con, [node(con, "save")])
    assert "P3-)P3: writes table items, which load() reads later" in d["mermaid"]
    hop = next(a for a in d["arrows"] if a["kind"] == "channel")
    assert (hop["channel"], hop["address"]) == ("db", "items")
    assert "open arrowhead crosses a channel" in diagrams.legend(d)
    assert diagrams.unbacked(con, d) == []


def test_an_arrow_the_map_does_not_hold_is_caught(app):
    _, _, con = app
    d = diagrams.sequence(con, [node(con, "run")])
    fake = {"from": node(con, "check"), "to": node(con, "save"), "kind": "call", "guess": False}
    d["arrows"].append(fake)
    assert diagrams.unbacked(con, d) == [fake]


def test_a_large_diagram_is_cut_and_says_what_it_left_out(app):
    _, _, con = app
    d = diagrams.sequence(con, [node(con, "run")], max_messages=2)
    assert len(d["arrows"]) == 2 and d["left_out"] == 3
    assert d["mermaid"].rstrip().endswith("and 3 more calls not drawn")
    # a participant cap keeps the changed code's own unit and the path to it, and drops the rest
    d = diagrams.sequence(con, [node(con, "run")], max_participants=2)
    assert "Service" in d["participants"] and "store.py" not in d["participants"]
    assert diagrams.unbacked(con, d) == []


def test_interfaces_containers_and_tables_in_csharp(tmp_path):
    db = tmp_path / "f5.db"
    work = tmp_path / "f5"
    shutil.copytree(FIXTURE5, work)
    index(work, db, "f5")
    con = store.connect(db)
    try:
        sql = con.execute("SELECT id FROM nodes WHERE id LIKE '%SqlOrderStore.Save(%'").fetchone()[0]
        d = diagrams.sequence(con, [sql])
        m = d["mermaid"]
        assert "Note over P1: starts at the entry point Program.Main" in m
        assert "P3-)P4: Save(), the registered implementation" in m          # di: a channel, not a direct call
        assert "P4-)P5: writes table Orders, which Pending() reads later" in m
        assert diagrams.unbacked(con, d) == []
        mem = con.execute("SELECT id FROM nodes WHERE id LIKE '%MemoryOrderStore.Save(%'").fetchone()[0]
        d = diagrams.sequence(con, [mem])
        assert "P3->>P4: Save() (implementation)" in d["mermaid"]             # through the interface
        assert [a["kind"] for a in d["arrows"]][-1] == "dispatch" and diagrams.unbacked(con, d) == []
    finally:
        con.close()


def test_a_route_is_reached_over_http_and_a_guess_is_dotted(tmp_path):
    root, db = tmp_path / "web", tmp_path / "w.db"
    write(root, {
        "server.ts": 'import Fastify from "fastify";\nexport function build() {\n  const server = Fastify();\n'
                     '  server.get("/api/things/:id", async () => ({ thing: 1 }));\n  return server;\n}\n',
        "client.ts": 'export async function thing(id: string) {\n  return (await fetch(`/api/things/${id}`)).json();\n}\n',
    })
    index(root, db, "web")
    con = store.connect(db)
    try:
        build = node(con, "GET /api/things/:id")   # the route's own handler, which the request lands on
        d = diagrams.sequence(con, [build])
        assert "P1-)P2: http GET /api/things/:id" in d["mermaid"] and diagrams.unbacked(con, d) == []
        # a link the map guessed by name is dotted, and the legend says so
        with con:
            con.execute("UPDATE links SET precision = 'guess' WHERE kind = 'communicates'")
        d = diagrams.sequence(con, [build])
        assert "P1--)P2: http GET /api/things/:id" in d["mermaid"]
        assert "dotted arrow is a link the map guessed by name" in diagrams.legend(d)
    finally:
        con.close()


def test_what_changed_in_how_it_runs(app):
    root, db, con = app
    full = db.parent / "before-full.db"
    con.execute("VACUUM INTO ?", (str(full),))
    diff.snapshot(con, "x")                                    # the slim baseline a change keeps
    con.close()
    svc = root / "app/service.py"
    svc.write_text(svc.read_text().replace("        self.check(data)\n", "        audit(name)\n")
                   + "\n\ndef audit(name):\n    return name\n")
    index(root, db, "app")
    con = store.connect(db)
    run, audit = node(con, "run"), node(con, "audit")
    slim = diff.snapshot_path(con, "x")
    view = diagrams.for_snapshot(slim, con, [run, audit])
    c = view["changes"]
    assert [(x["from"], x["to"]) for x in c["calls_added"]] == [("Service.run", "service.py.audit")]
    assert [(x["from"], x["to"]) for x in c["calls_removed"]] == [("Service.run", "Service.check")]
    assert "before" not in view                               # a slim baseline keeps no order: listed, not drawn
    assert "service.py" in view["after"]["participants"] and ": audit()" in view["after"]["mermaid"]
    assert "check()" not in view["after"]["mermaid"] and diagrams.unbacked(con, view["after"]) == []
    lines = diagrams.section(view, "### How it runs now")
    assert "- `Service.run` now calls `service.py.audit`" in lines and "- `Service.run` no longer calls `Service.check`" in lines
    # a baseline that keeps the whole map is drawn as it was, too
    before = store.connect(full)
    try:
        view = diagrams.for_change(before, con, [run, audit])
        assert "check()" in view["before"]["mermaid"] and "audit()" not in view["before"]["mermaid"]
        assert diagrams.unbacked(before, view["before"]) == []
    finally:
        before.close()
    con.close()


def test_the_plan_and_the_check_draw_how_it_runs(app):
    root, db, con = app
    ch = root / "openspec" / "changes" / "audit-runs"
    (ch / "specs" / "service").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Audit runs\n\n## Why\nRuns go unrecorded.\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `Service.run` to stop checking the data\n")
    (ch / "specs" / "service" / "spec.md").write_text(
        "## MODIFIED Requirements\n### Requirement: Runs\nThe service SHALL run.\n\n"
        "#### Scenario: Run\n- **WHEN** a name is run\n- **THEN** its data comes back\n")
    b = spec.brief(con, ch)
    page = (ch / "leyline.md").read_text()
    assert "### How it runs" in page and "```mermaid\nsequenceDiagram\n" in page
    assert "Note over P2: changed: Service.run" in page and "The code the tasks change is shaded." in page
    assert diagrams.unbacked(con, b["how_it_runs"]) == []
    svc = root / "app/service.py"
    svc.write_text(svc.read_text().replace("        self.check(data)\n", ""))
    index(root, db, "app")
    v = spec.verify(con, ch)
    page = (ch / "leyline.md").read_text()
    assert "### How it runs\n" in page                      # the plan's diagram stays
    assert "### How it runs now" in page and "- `Service.run` no longer calls `Service.check`" in page
    assert page.count("```mermaid") == 2
    assert diagrams.unbacked(con, v["how_it_runs"]["after"]) == []


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def test_the_pull_request_page_draws_the_change(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    write(root, APP)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    svc = root / "app/service.py"
    svc.write_text(svc.read_text().replace("        save(name, data)\n", ""))
    git(root, "commit", "-qam", "Stop saving runs")
    monkeypatch.chdir(root)
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["pr", "main"]) == 0
    page = out.getvalue()
    assert "## How it runs\n" in page and "```mermaid" in page
    assert "- `Service.run` no longer calls `store.py.save`" in page
    assert page.index("## How it runs") < page.index("## What it reaches")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the map page's reader")
def test_the_map_page_shows_a_diagram_as_its_source(tmp_path):
    """The map page reads leyline.md with a small reader of its own; a ```mermaid block stays one block, shown as
    its source (the page has no Mermaid renderer and loads nothing from the network)."""
    html = (Path(spec.__file__).parent / "viewer" / "viewer.html").read_text()
    start = html.index("function mdBlocks(text)")
    end = html.index("\n}\n", start) + 3
    script = tmp_path / "md.js"
    script.write_text(html[start:end] + "\nconsole.log(JSON.stringify(mdBlocks(require('fs').readFileSync(0, 'utf8'))));\n")
    page = "## 1. What\n\n### How it runs\n\nIntro line.\n\n```mermaid\nsequenceDiagram\n    P1->>P2: run()\n```\n\nLegend.\n"
    out = subprocess.run(["node", str(script)], input=page, capture_output=True, text=True, check=True).stdout
    blocks = json.loads(out)
    assert [b["t"] for b in blocks] == ["h", "sub", "p", "pre", "p"]
    assert blocks[3] == {"t": "pre", "lang": "mermaid", "text": "sequenceDiagram\n    P1->>P2: run()"}
