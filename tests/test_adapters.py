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


def test_csharp_a_named_arguments_label_and_an_anonymous_objects_member_are_not_field_reads(tmp_path):
    con = _map(tmp_path, {
        "App/App.csproj": CSPROJ,
        "App/A.cs": (
            "namespace App;\n"
            "public class A {\n"
            "    private int count;\n"
            "    private string Name;\n"
            "    void Labels() { Take(count: 5); var o = new { Name = \"x\" }; }\n"
            "    void Reads() { Take(count); var p = new { Name }; }\n"
            "    void Take(int count) {}\n"
            "}\n"),
    })
    reads = edges(con, "reads")
    assert ("App.A.Labels()", "App.A.count") not in reads
    assert ("App.A.Labels()", "App.A.Name") not in reads
    assert ("App.A.Reads()", "App.A.count") in reads and ("App.A.Reads()", "App.A.Name") in reads


def test_csharp_action_routes_from_a_verb_with_a_route_and_from_a_shared_attribute_list(tmp_path):
    con = _map(tmp_path, {
        "Api/Api.csproj": CSPROJ,
        "Api/ItemsController.cs": (
            "namespace Api;\n"
            "[ApiController]\n"
            "[Route(\"api/[controller]\")]\n"
            "public class ItemsController : ControllerBase {\n"
            "    [HttpGet]\n"
            "    public string List() => \"\";\n"
            "    [HttpGet]\n"
            "    [Route(\"{id}\")]\n"
            "    public string Get(int id) => \"\";\n"
            "    [Authorize, HttpPost(\"bulk\")]\n"
            "    public void Bulk() { }\n"
            "}\n"),
        "Client/Client.csproj": CSPROJ,
        "Client/Calls.cs": (
            "using System.Net.Http;\n"
            "namespace Client;\n"
            "public class Calls {\n"
            "    private readonly HttpClient _http = new HttpClient();\n"
            "    public async Task ListAll() { await _http.GetAsync(\"/api/Items\"); }\n"
            "    public async Task GetOne() { await _http.GetAsync(\"/api/Items/3\"); }\n"
            "    public async Task PostBulk() { await _http.PostAsync(\"/api/Items/bulk\", null); }\n"
            "}\n"),
    })
    got = http(con)
    # [HttpGet] [Route("{id}")] is GET api/Items/{id} only, so a GET of api/Items is List's alone.
    assert got["Client.Calls.ListAll()"] == ("Api.ItemsController.List()", "GET /api/Items")
    assert got["Client.Calls.GetOne()"] == ("Api.ItemsController.Get(int)", "GET /api/Items/{id}")
    # [Authorize, HttpPost("bulk")] is read: the post goes to Bulk, not to a route of any verb at {id}.
    assert got["Client.Calls.PostBulk()"] == ("Api.ItemsController.Bulk()", "POST /api/Items/bulk")


def test_csharp_a_positional_record_without_a_body_has_its_properties(tmp_path):
    con = _map(tmp_path, {
        "App/App.csproj": CSPROJ,
        "App/Item.cs": (
            "namespace App;\n"
            "public record Item(int Id, string Name);\n"
            "public record Tagged(string Tag) { public int Size => 1; }\n"
            "public class Use {\n"
            "    public string Of(Item it, Tagged t) { return it.Name + t.Tag; }\n"
            "}\n"),
    })
    fields = {short(r[0]) for r in con.execute("SELECT id FROM nodes WHERE kind = 'field'")}
    assert {"App.Item.Id", "App.Item.Name", "App.Tagged.Tag"} <= fields
    assert ("App.Use.Of(Item,Tagged)", "App.Item.Name") in edges(con, "reads")
