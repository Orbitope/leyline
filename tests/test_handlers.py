"""A server route's inline handler is a function of its own: `server.get("/api/x", async (req) => { ... })` makes
`GET /api/x`, nested in the function that registers it. Requests land on it, what it calls is its own, the registrar
registers it (so startup still reaches it), and a pull request that edits it touches that route and no other."""

import io
import json
import subprocess
from contextlib import redirect_stdout

import pytest

from leyline import cli, diagrams, store
from leyline.adapters import typescript
from leyline.indexer import index

ROUTES = '''import type { FastifyInstance } from "fastify";
import { listThings, loadData } from "./files.js";

function audit(x: string) { return x; }

export function registerRoutes(server: FastifyInstance): void {
  server.get("/api/health", async () => ({ ok: audit("h") }));

  server.get<{ Params: { id: string } }>("/api/things/:id", async (req, reply) => {
    const data = loadData();
    audit(req.params.id);
    return data;
  });

  server.post("/api/things", { schema: { body: {} } }, async (req) => {
    return listThings();
  });

  server.get("/api/list", listThings);

  server.route({
    method: "PUT",
    url: "/api/things/:id",
    handler: async (req, reply) => {
      audit("put");
      return null;
    },
  });

  server.route({ method: ["GET", "HEAD"], url: "/api/both", handler(req) { return audit("both"); } });
  server.get("/api/opts", { handler: async () => audit("opts") });
  server.get("/api/things/special", async () => audit("special"));
}
'''


def test_each_inline_handler_is_a_function_named_for_its_route():
    res = typescript.parse("r", "routes.ts", "r:file:routes.ts", ROUTES.encode())
    reg = "r:typescript:routes.registerRoutes"
    handlers = {n.name: n for n in res.nodes if n.attrs.get("native_kind") == "route_handler"}
    assert set(handlers) == {"GET /api/health", "GET /api/things/:id", "POST /api/things", "PUT /api/things/:id",
                             "GET, HEAD /api/both", "GET /api/opts", "GET /api/things/special"}
    one = handlers["GET /api/things/:id"]
    assert one.id == f"{reg}/route:GET /api/things/:id" and one.parent_id == reg
    assert (one.span_start, one.span_end) == (9, 13)          # the handler function, not the registration call
    assert handlers["PUT /api/things/:id"].span_start == 24   # server.route({ ..., handler: ... })
    serve = {e.address + " " + str(e.method): e for e in res.endpoints if e.role == "serve"}
    assert serve["/api/things/:id GET"].src_id == one.id
    assert serve["/api/things POST"].src_id == f"{reg}/route:POST /api/things"   # (path, { schema }, handler)
    assert serve["/api/list GET"].src_id == reg and serve["/api/list GET"].handler == "listThings"   # given by name
    assert serve["/api/both None"].src_id == f"{reg}/route:GET, HEAD /api/both"
    # what a handler calls is its own, not the registrar's; no handler comes out as a local function named "handler"
    calls = {(c.src_id.rsplit("/route:", 1)[-1] if "/route:" in c.src_id else c.src_id, c.name) for c in res.calls}
    assert ("GET /api/things/:id", "loadData") in calls and ("PUT /api/things/:id", "audit") in calls
    assert not any(c.src_id == reg and c.name in ("audit", "loadData") for c in res.calls)
    assert not any(n.name == "handler" for n in res.nodes)


@pytest.fixture
def web(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "routes.ts").write_text(ROUTES)
    (root / "files.ts").write_text("export function listThings() { return []; }\nexport function loadData() { return {}; }\n")
    (root / "client.ts").write_text(
        "export async function getThing(id: string) { return fetch(`/api/things/${id}`); }\n"
        "export async function listAll() { return fetch(\"/api/list\"); }\n"
        "export async function putThing(id: string) { return fetch(`/api/things/${id}`, { method: \"PUT\" }); }\n"
        "export async function special() { return fetch(\"/api/things/special\"); }\n")
    (root / "main.ts").write_text(
        "import Fastify from \"fastify\";\nimport { registerRoutes } from \"./routes.js\";\n"
        "export function main() {\n  const server = Fastify();\n  registerRoutes(server);\n  server.listen({ port: 1 });\n}\n"
        "main();\n")
    (root / "client.test.ts").write_text(
        "import { it } from \"vitest\";\nimport { putThing } from \"./client.js\";\n"
        "it(\"puts a thing\", async () => {\n  await putThing(\"1\");\n});\n")
    db = tmp_path / "w.db"
    index(root, db, "web")
    con = store.connect(db)
    yield con
    con.close()


def test_requests_land_on_the_handler_and_startup_still_reaches_it(web):
    con = web
    reg = "web:typescript:routes.registerRoutes"
    got = {(r[0].split(":", 2)[-1], r[1].split(":", 2)[-1]): json.loads(r[2]) for r in con.execute(
        "SELECT src_id, dst_id, attrs FROM edges WHERE kind = 'communicates'") if json.loads(r[2])["channel"] == "http"}
    # fetch with no method is a GET: of GET and PUT on one path it reaches the GET handler
    assert got[("client.getThing", "routes.registerRoutes/route:GET /api/things/:id")]["handler"] is True
    assert ("client.putThing", "routes.registerRoutes/route:PUT /api/things/:id") in got
    assert ("client.listAll", "files.listThings") in got        # a handler given by name is that function
    # a route that names the path outright wins over one with a parameter there, as a router picks it
    assert [dst for src, dst in got if src == "client.special"] == ["routes.registerRoutes/route:GET /api/things/special"]
    assert not any(dst == "routes.registerRoutes" for _, dst in got)
    registered = {r[0].rsplit("/route:", 1)[-1] for r in con.execute(
        "SELECT dst_id FROM calls WHERE src_id = ? AND dispatch = 'registers'", (reg,))}
    assert len(registered) == 7 and "GET /api/things/:id" in registered
    # what the handler calls belongs to it
    assert con.execute("SELECT 1 FROM calls WHERE src_id = ? AND dst_id LIKE '%files.loadData'",
                       (reg + "/route:GET /api/things/:id",)).fetchone()
    assert not con.execute("SELECT 1 FROM calls WHERE src_id = ? AND dst_id LIKE '%files.loadData'", (reg,)).fetchone()
    # a client's flow goes into the handler; the program's startup still reaches the registrar and its handlers
    steps = lambda start: [r[0] for r in con.execute(
        "SELECT s.callable_id FROM flow_steps s JOIN flows f ON f.id = s.flow_id WHERE f.entry_id = ? ORDER BY s.seq", (start,))]
    put = steps("web:typescript:client.test.<module>/test:puts-a-thing")
    assert put[:3] == ["web:typescript:client.test.<module>/test:puts-a-thing", "web:typescript:client.putThing",
                       reg + "/route:PUT /api/things/:id"] and reg not in put
    start = steps("web:typescript:main.<module>")
    assert reg in start and reg + "/route:GET /api/things/:id" in start
    # a diagram of the handler draws the request landing on it, and the registrar registering it
    d = diagrams.sequence(con, [reg + "/route:GET /api/things/:id"])
    assert "http GET /api/things/:id" in d["mermaid"] and diagrams.unbacked(con, d) == []
    d = diagrams.sequence(con, [reg])
    assert "registers GET /api/things/:id" in d["mermaid"]


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


SERVER = '''import express from "express";
const app = express();
export function routes() {
  app.get("/api/items", (req, res) => {
    const items = [1, 2, 3];
    const a = 1;
    const b = 2;
    const c = 3;
    const d = 4;
    const e = 5;
    const f = 6;
    const g = 7;
    const h = 8;
    const i = 9;
    const j = 10;
    const k = 11;
    res.json({ items });
  });
  app.get("/api/users", (req, res) => {
    res.json({ users: [] });
  });
}
'''


def test_an_edit_inside_a_handler_touches_exactly_its_route(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "web").mkdir(parents=True)
    (root / "web/server.ts").write_text(SERVER)
    (root / "web/client.ts").write_text(
        "export async function items() {\n  return (await (await fetch(\"/api/items\")).json()).items;\n}\n"
        "export async function users() {\n  return (await (await fetch(\"/api/users\")).json()).users;\n}\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    # 13 lines below the route's path, past the window an edited line is read with
    (root / "web/server.ts").write_text(SERVER.replace("res.json({ items });", "res.json({ rows: items });"))
    git(root, "commit", "-qam", "Rename the items key")
    monkeypatch.chdir(root)
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["pr", "main"]) == 0
    page = out.getvalue()
    assert "Edited: `GET /api/items`" in page
    assert "Crosses the http GET /api/items" in page and "`client.ts.items`" in page
    assert "/api/users" not in page
