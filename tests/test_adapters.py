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


def test_csharp_signatures_in_a_file_that_starts_with_a_byte_order_mark(tmp_path):
    root = tmp_path / "repo"
    (root / "App").mkdir(parents=True)
    (root / "App/App.csproj").write_text(CSPROJ)
    (root / "App/Tests.cs").write_bytes(
        b"\xef\xbb\xbfnamespace App;\npublic class OrderTests\n{\n    [Fact]\n    public void Totals() { }\n}\n")
    index(root, tmp_path / "s.db", "a")
    con = store.connect(tmp_path / "s.db")
    sigs = {short(r[0]): json.loads(r[1])["signature"] for r in con.execute("SELECT id, attrs FROM nodes WHERE kind IN ('type', 'callable')")}
    assert sigs["App.OrderTests"] == "public class OrderTests"
    assert sigs["App.OrderTests.Totals()"] == "[Fact] public void Totals()"


def test_python_attributes_assigned_together_are_fields(tmp_path):
    con = _map(tmp_path, {
        "pkg/box.py": (
            "class Box:\n"
            "    def __init__(self, w, h):\n"
            "        self.w, self.h = w, h\n"
            "        (self.x, self.y) = (0, 0)\n"
            "\n"
            "    def area(self):\n"
            "        return self.w * self.h + self.y\n"),
    })
    fields = {short(r[0]) for r in con.execute("SELECT id FROM nodes WHERE kind = 'field'")}
    assert {"pkg.box.Box.w", "pkg.box.Box.h", "pkg.box.Box.x", "pkg.box.Box.y"} <= fields
    assert ("pkg.box.Box.__init__", "pkg.box.Box.h") in edges(con, "writes")
    assert ("pkg.box.Box.area", "pkg.box.Box.h") in edges(con, "reads")


def test_python_functions_defined_under_if_try_and_with_at_the_top_of_a_module_or_class(tmp_path):
    con = _map(tmp_path, {
        "pkg/compat.py": (
            "import sys\n"
            "\n"
            "try:\n"
            "    from fast import dumps\n"
            "except ImportError:\n"
            "    def dumps(x):\n"
            "        return helper(x)\n"
            "\n"
            "if sys.platform == 'win32':\n"
            "    def home():\n"
            "        return helper(1)\n"
            "else:\n"
            "    def home():\n"
            "        return helper(2)\n"
            "\n"
            "class Box:\n"
            "    if sys.version_info >= (3, 8):\n"
            "        def size(self):\n"
            "            return helper(3)\n"
            "\n"
            "def helper(x):\n"
            "    return x\n"
            "\n"
            "def main(b: Box):\n"
            "    dumps(1)\n"
            "    home()\n"
            "    b.size()\n"),
    })
    nodes = {short(r[0]): r[1] for r in con.execute("SELECT id, kind FROM nodes WHERE kind = 'callable'")}
    assert {"pkg.compat.dumps", "pkg.compat.home", "pkg.compat.Box.size"} <= set(nodes)
    got = calls(con)
    for caller in ("pkg.compat.dumps", "pkg.compat.home", "pkg.compat.Box.size"):
        assert (caller, "pkg.compat.helper") in got
    for callee in ("pkg.compat.dumps", "pkg.compat.home", "pkg.compat.Box.size"):
        assert got[("pkg.compat.main", callee)] == "heuristic"


def test_python_a_call_on_an_object_made_in_place_is_on_its_class(tmp_path):
    con = _map(tmp_path, {
        "pkg/shapes.py": (
            "from dataclasses import dataclass\n"
            "\n"
            "@dataclass\n"
            "class Box:\n"
            "    w: int = 1\n"
            "\n"
            "    def size(self):\n"
            "        return self.w\n"
            "\n"
            "class Other:\n"
            "    w = 2\n"
            "\n"
            "    def size(self):\n"
            "        return 2\n"
            "\n"
            "def main():\n"
            "    Box().size()\n"
            "    return Box(3).w\n"),
    })
    assert calls(con)[("pkg.shapes.main", "pkg.shapes.Box.size")] == "heuristic"
    assert ("pkg.shapes.main", "pkg.shapes.Box.w") in edges(con, "reads")


def test_python_an_app_factory_declares_its_routes_and_requests_none(tmp_path):
    con = _map(tmp_path, {
        "app/factory.py": (
            "from fastapi import FastAPI\n"
            "\n"
            "def create_app():\n"
            "    app = FastAPI()\n"
            "\n"
            "    @app.get('/items')\n"
            "    def items():\n"
            "        return []\n"
            "\n"
            "    return app\n"),
        "tests/test_items.py": (
            "from app.factory import create_app\n"
            "\n"
            "def test_items():\n"
            "    client = TestClient(create_app())\n"
            "    client.get('/items')\n"),
    })
    got = http(con)
    assert "app.factory.create_app" not in got
    assert got["tests.test_items.test_items"] == ("app.factory.create_app.items", "GET /items")


def test_typescript_a_request_on_a_client_named_api_is_not_a_route(tmp_path):
    con = _map(tmp_path, {
        "web/src/api.ts": (
            "import axios from 'axios';\n"
            "export const api = axios.create({ baseURL: '/' });\n"
            "\n"
            "export async function login(credentials: { user: string }) {\n"
            "  return api.post('/api/login', credentials);\n"
            "}\n"
            "\n"
            "export async function items(params: object) {\n"
            "  return api.get('/api/items', params);\n"
            "}\n"),
        "server/src/main.ts": (
            "import Fastify from 'fastify';\n"
            "const server = Fastify();\n"
            "\n"
            "server.post('/api/login', async (req) => {\n"
            "  return { ok: true };\n"
            "});\n"
            "\n"
            "server.get('/api/items', listItems);\n"
            "\n"
            "async function listItems() {\n"
            "  return [];\n"
            "}\n"
            "\n"
            "server.listen({ port: 3000 });\n"),
    })
    got = http(con)
    assert got["web.src.api.login"] == ("server.src.main.<module>/route:POST /api/login", "POST /api/login")
    assert got["web.src.api.items"] == ("server.src.main.listItems", "GET /api/items")


def test_typescript_an_overloaded_method_spans_its_implementation(tmp_path):
    con = _map(tmp_path, {
        "src/shape.ts": (
            "export class Shape {\n"
            "  area(): number;\n"
            "  area(scale: number): number;\n"
            "  area(scale?: number): number {\n"
            "    return this.compute(scale ?? 1);\n"
            "  }\n"
            "\n"
            "  compute(s: number): number {\n"
            "    return s;\n"
            "  }\n"
            "}\n"),
    })
    span, attrs = con.execute("SELECT span_start || '-' || span_end, attrs FROM nodes WHERE id LIKE '%Shape.area'").fetchone()
    assert span == "4-6"
    assert not json.loads(attrs).get("is_abstract") and json.loads(attrs)["argc_max"] == 1


def test_typescript_an_anonymous_default_class_is_a_class_with_its_methods(tmp_path):
    con = _map(tmp_path, {
        "src/widget.ts": (
            "export default class {\n"
            "  render(): string {\n"
            "    return this.label();\n"
            "  }\n"
            "\n"
            "  label(): string {\n"
            "    return 'x';\n"
            "  }\n"
            "}\n"),
        "src/page.ts": (
            "import Widget from './widget';\n"
            "\n"
            "export function show(): string {\n"
            "  const w = new Widget();\n"
            "  return w.render();\n"
            "}\n"),
    })
    nodes = {short(r[0]): r[1] for r in con.execute("SELECT id, kind FROM nodes")}
    assert nodes.get("src.widget.default") == "type"
    assert nodes.get("src.widget.default.render") == "callable"
    got = calls(con)
    assert ("src.widget.default.render", "src.widget.default.label") in got
    assert ("src.page.show", "src.widget.default.render") in got
