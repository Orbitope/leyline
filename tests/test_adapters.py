"""What the hand-written adapters read from ordinary code, each pinned on a small repository: calls on `this` and
`base` in C#, and the declarations, fields and routes that were missed or misread."""

import json
from pathlib import Path

from leyline import store
from leyline.indexer import index


def _map(tmp_path: Path, files: dict):
    root = tmp_path / "repo"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    db = tmp_path / "s.db"
    index(root, db, "a")
    return store.connect(db)


def short(i: str) -> str:
    return i.split(":", 2)[-1].split("::")[-1]


def calls(con) -> dict:
    return {(short(r[0]), short(r[1])): r[2] for r in con.execute("SELECT src_id, dst_id, precision FROM calls")}


def edges(con, kind: str) -> dict:
    return {(short(r[0]), short(r[1])): r[2] for r in con.execute(
        "SELECT src_id, dst_id, precision FROM edges WHERE kind = ?", (kind,))}


def http(con) -> dict:
    out = {}
    for r in con.execute("SELECT src_id, dst_id, attrs FROM edges WHERE kind = 'communicates'"):
        a = json.loads(r[2])
        if a["channel"] == "http":
            out[short(r[0])] = (short(r[1]), a["address"])
    return out


CSPROJ = '<Project Sdk="Microsoft.NET.Sdk"></Project>\n'


def test_csharp_calls_and_fields_on_this_and_base_are_on_the_own_type(tmp_path):
    con = _map(tmp_path, {
        "App/App.csproj": CSPROJ,
        "App/A.cs": (
            "namespace App;\n"
            "public class B { protected virtual void Run() {} }\n"
            "public class A : B {\n"
            "    private int total;\n"
            "    protected override void Run() { this.Go(); base.Run(); this.total += 1; }\n"
            "    void Go() {}\n"
            "}\n"
            "public class Other { public void Go() {} }\n"),
    })
    got = calls(con)
    assert got[("App.A.Run()", "App.A.Go()")] == "heuristic"      # not left unlinked beside Other.Go
    assert got[("App.A.Run()", "App.B.Run()")] == "heuristic"     # not a guess
    assert edges(con, "writes")[("App.A.Run()", "App.A.total")] == "heuristic"
