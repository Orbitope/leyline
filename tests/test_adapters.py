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


def test_csharp_a_name_in_nameof_is_not_a_field_read(tmp_path):
    con = _map(tmp_path, {
        "App/App.csproj": CSPROJ,
        "App/A.cs": (
            "namespace App;\n"
            "public class A {\n"
            "    private object _repo;\n"
            "    public string FullName => \"\";\n"
            "    public A(object r) {\n"
            "        _repo = r ?? throw new System.ArgumentNullException(nameof(_repo));\n"
            "        Changed(nameof(FullName));\n"
            "    }\n"
            "    void Changed(string n) {}\n"
            "}\n"),
    })
    reads = edges(con, "reads")
    assert ("App.A..ctor(object)", "App.A._repo") not in reads
    assert ("App.A..ctor(object)", "App.A.FullName") not in reads
    assert ("App.A..ctor(object)", "App.A._repo") in edges(con, "writes")


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


def test_python_and_csharp_the_route_that_names_more_of_the_path_wins(tmp_path):
    con = _map(tmp_path, {
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "\n"
            "@app.get('/items/{item_id}')\n"
            "def read_item(item_id: int):\n"
            "    return item_id\n"
            "\n"
            "@app.get('/items/special')\n"
            "def special():\n"
            "    return 1\n"),
        "tests/test_api.py": (
            "def test_special(client):\n"
            "    client.get('/items/special')\n"
            "\n"
            "def test_one(client):\n"
            "    client.get('/items/3')\n"),
        "Api/Api.csproj": CSPROJ,
        "Api/OrdersController.cs": (
            "namespace Api;\n"
            "[Route(\"api/orders\")]\n"
            "public class OrdersController : ControllerBase {\n"
            "    [HttpGet(\"{id}\")]\n"
            "    public string Get(string id) => id;\n"
            "    [HttpGet(\"latest\")]\n"
            "    public string Latest() => \"\";\n"
            "}\n"),
        "Api/Calls.cs": (
            "namespace Api;\n"
            "public class Calls {\n"
            "    private readonly HttpClient _http = new HttpClient();\n"
            "    public async Task Newest() { await _http.GetAsync(\"/api/orders/latest\"); }\n"
            "}\n"),
    })
    got = http(con)
    assert got["tests.test_api.test_special"] == ("app.main.special", "GET /items/special")
    assert got["tests.test_api.test_one"] == ("app.main.read_item", "GET /items/{item_id}")
    assert got["Api.Calls.Newest()"] == ("Api.OrdersController.Latest()", "GET /api/orders/latest")


def test_an_outside_import_in_go_or_java_is_labelled_by_its_own_language_not_npm(tmp_path):
    con = _map(tmp_path, {
        "main.go": (
            "package main\n"
            "\n"
            "import (\n"
            "\t\"fmt\"\n"
            "\t\"example.com/lib/store\"\n"
            ")\n"
            "\n"
            "func main() { fmt.Println(store.New()) }\n"),
        "src/app/Main.java": (
            "package app;\n"
            "import java.util.List;\n"
            "public class Main { public static void main(String[] a) { } }\n"),
    })
    ext = {r[0].split(":ext:", 1)[1] for r in con.execute("SELECT id FROM nodes WHERE kind = 'external'")}
    assert {"go:fmt", "go:example.com/lib/store", "java:java.util.List"} <= ext
    assert not any(x.startswith("npm:") for x in ext)


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


def test_csharp_a_test_is_marked_by_its_attributes_name_not_a_word_in_its_arguments(tmp_path):
    con = _map(tmp_path, {
        "App/App.csproj": CSPROJ,
        "App/Api.cs": (
            "namespace App;\n"
            "public class Api {\n"
            "    [SwaggerOperation(Summary = \"Test the connection\")]\n"
            "    public void Ping() { }\n"
            "    [Obsolete(\"Fact: use Ping\")]\n"
            "    public void Old() { }\n"
            "}\n"
            "public class ApiTests {\n"
            "    [Xunit.Fact]\n"
            "    public void Pings() { }\n"
            "    [TestCaseAttribute(1)]\n"
            "    public void Cases(int n) { }\n"
            "}\n"),
    })
    marked = {short(r[0]) for r in con.execute("SELECT id FROM nodes WHERE json_extract(attrs, '$.is_test')")}
    assert marked == {"App.ApiTests.Pings()", "App.ApiTests.Cases(int)"}


def test_csharp_fields_assigned_by_deconstruction_are_written(tmp_path):
    con = _map(tmp_path, {
        "App/App.csproj": CSPROJ,
        "App/P.cs": (
            "namespace App;\n"
            "public class P {\n"
            "    private string _name; private int _age;\n"
            "    public P(string name, int age) { (_name, _age) = (name, age); }\n"
            "    public void Swap() { (this._age, this._name) = (1, \"x\"); }\n"
            "    public (string, int) Get() { return (_name, _age); }\n"
            "}\n"),
    })
    writes, reads = edges(con, "writes"), edges(con, "reads")
    for fn in ("App.P..ctor(string,int)", "App.P.Swap()"):
        assert (fn, "App.P._name") in writes and (fn, "App.P._age") in writes
        assert (fn, "App.P._name") not in reads
    assert ("App.P.Get()", "App.P._name") in reads and ("App.P.Get()", "App.P._name") not in writes


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
            "        return self.w * self.h + self.y\n"
            "\n"
            "    def flip(self):\n"
            "        [self.w, self.h] = self.h, self.w\n"),
    })
    fields = {short(r[0]) for r in con.execute("SELECT id FROM nodes WHERE kind = 'field'")}
    assert {"pkg.box.Box.w", "pkg.box.Box.h", "pkg.box.Box.x", "pkg.box.Box.y"} <= fields
    assert ("pkg.box.Box.__init__", "pkg.box.Box.h") in edges(con, "writes")
    assert ("pkg.box.Box.area", "pkg.box.Box.h") in edges(con, "reads")
    assert ("pkg.box.Box.flip", "pkg.box.Box.h") in edges(con, "writes")


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


def test_typescript_a_request_on_an_axios_instance_is_not_a_route_and_a_made_handler_still_is(tmp_path):
    con = _map(tmp_path, {
        "web/src/client.ts": (
            "import axios from 'axios';\n"
            "const instance = axios.create({ baseURL: '/' });\n"
            "\n"
            "export async function login(credentials: { user: string }) {\n"
            "  return instance.post('/api/login', credentials);\n"
            "}\n"),
        "server/src/routes.ts": (
            "const withAuth = (fn: any) => fn;\n"
            "const makeHandler = () => async () => [];\n"
            "const listItems = withAuth(async () => []);\n"
            "const listTags = makeHandler();\n"
            "\n"
            "export async function routes(instance: any) {\n"
            "  instance.post('/api/login', async (req: any) => ({ ok: true }));\n"
            "  instance.get('/api/items', listItems);\n"
            "}\n"
            "\n"
            "export function more(app: any) {\n"
            "  app.get('/api/tags', listTags);\n"
            "}\n"),
        "web/src/calls.ts": (
            "export async function items() { return fetch('/api/items'); }\n"
            "export async function tags() { return fetch('/api/tags'); }\n"),
    })
    got = http(con)
    assert got["web.src.client.login"] == ("server.src.routes.routes/route:POST /api/login", "POST /api/login")
    # A handler a call made is still a handler: the route stands, served by the function that registers it.
    assert got["web.src.calls.items"] == ("server.src.routes.routes", "GET /api/items")
    assert got["web.src.calls.tags"] == ("server.src.routes.more", "GET /api/tags")


def test_typescript_namespace_members_are_declared_under_the_namespace(tmp_path):
    con = _map(tmp_path, {
        "src/geo.ts": (
            "export namespace Geo {\n"
            "  export function area(): number {\n"
            "    return scale(2);\n"
            "  }\n"
            "  function scale(x: number): number { return x; }\n"
            "  export class Shape {\n"
            "    m(): number { return 1; }\n"
            "  }\n"
            "}\n"),
        "src/use.ts": (
            "import { Geo } from './geo';\n"
            "export function run(): number {\n"
            "  const s: Geo.Shape = new Geo.Shape();\n"
            "  s.m();\n"
            "  return Geo.area();\n"
            "}\n"),
    })
    nodes = {short(r[0]): r[1] for r in con.execute("SELECT id, kind FROM nodes")}
    assert nodes.get("src.geo.Geo") == "type"
    assert nodes.get("src.geo.Geo.area") == "callable" and nodes.get("src.geo.Geo.Shape") == "type"
    assert nodes.get("src.geo.Geo.Shape.m") == "callable"
    got = calls(con)
    assert ("src.use.run", "src.geo.Geo.area") in got
    assert ("src.use.run", "src.geo.Geo.Shape.m") in got
    assert ("src.geo.Geo.area", "src.geo.Geo.scale") in got


def test_typescript_fields_assigned_by_destructuring_are_written(tmp_path):
    con = _map(tmp_path, {
        "src/p.ts": (
            "export class P {\n"
            "  a = 1;\n"
            "  b = 2;\n"
            "  swap(): void { [this.a, this.b] = [1, 2]; }\n"
            "  load(o: any): void { ({ a: this.a, b: this.b } = o); }\n"
            "  sum(): number { return this.a + this.b; }\n"
            "}\n"),
    })
    writes, reads = edges(con, "writes"), edges(con, "reads")
    for fn in ("src.p.P.swap", "src.p.P.load"):
        assert (fn, "src.p.P.a") in writes and (fn, "src.p.P.b") in writes
        assert (fn, "src.p.P.a") not in reads
    assert ("src.p.P.sum", "src.p.P.a") in reads


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
