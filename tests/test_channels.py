"""Channels that are not calls: dependency injection, queues and buses, databases, RPC."""

import json
from pathlib import Path

import pytest

from leyline import change, store
from leyline.channels import sql_tables
from leyline.indexer import index

FIXTURE5 = Path(__file__).parent / "fixture5"


@pytest.fixture(scope="module")
def con5(tmp_path_factory):
    db = tmp_path_factory.mktemp("f5") / "f5.db"
    index(FIXTURE5, db, "f5")
    c = store.connect(db)
    yield c
    c.close()


def short(i):
    return i.split(":", 2)[-1].split("::")[-1].split("(")[0]


def links(c, channel):
    return {(short(r[0]), short(r[1])): (r[2], json.loads(r[3])["address"]) for r in c.execute(
        "SELECT src_id, dst_id, precision, attrs FROM edges WHERE kind = 'communicates'")
        if json.loads(r[3])["channel"] == channel}


def test_sql_tables_reads_statements_not_prose():
    assert sql_tables("SELECT o.id FROM orders o JOIN customers c ON c.id = o.cid") == [("read", "orders"), ("read", "customers")]
    assert sql_tables("insert into [dbo].[Audit] (a) select a from staging") == [("write", "Audit"), ("read", "staging")]
    assert sql_tables("UPDATE users SET name = ? WHERE id = ?") == [("write", "users")]
    assert sql_tables("DELETE FROM sessions WHERE expires < now()") == [("write", "sessions")]   # not also a read
    assert sql_tables("WITH recent AS (SELECT * FROM events) SELECT * FROM recent") == [("read", "events")]
    assert sql_tables("SELECT EXTRACT(year FROM created) FROM orders") == [("read", "orders")]
    assert sql_tables("Select a file from disk") == [] and sql_tables("Update the list from the server") == []


def test_dependency_injection(con5):
    di = links(con5, "di")
    # A call on the interface reaches the implementation the container registers, with where it was registered.
    assert di[("Shop.IOrderStore.Save", "Shop.SqlOrderStore.Save")] == ("heuristic", "IOrderStore -> SqlOrderStore")
    assert di[("Shop.INotifier.Notify", "Shop.SmsNotifier.Notify")][0] == "guess"          # read off a factory's `new`
    assert di[("Shop.Program.Configure", "Shop.OutboxWorker.ExecuteAsync")] == ("heuristic", "hosted OutboxWorker")
    assert di[("py.app.list_orders", "py.db.get_db")] == ("heuristic", "Depends(get_db)")      # FastAPI runs it first
    assert ("py.container.Alerts.notify", "py.container.EmailAlerts.notify") in di            # no subclassing, only the container
    assert ("ts.nest.Mailer.send", "ts.nest.SmtpMailer.send") in di                            # { provide, useClass }
    attrs = json.loads(con5.execute("SELECT attrs FROM edges WHERE kind = 'communicates' AND dst_id LIKE '%SqlOrderStore.Save%'"
                                    " AND attrs LIKE '%\"di\"%'").fetchone()[0])
    assert attrs["registered_in"].endswith("Program.Configure(IServiceCollection)") and attrs["registered_at"] == 17


def test_queues_and_buses(con5):
    q = links(con5, "queue")
    assert q[("Shop.OrderService.Place", "Shop.OrderPlacedHandler.Handle")] == ("heuristic", "OrderPlaced")   # MediatR by type
    assert q[("Shop.OrderService.Place", "py.consumer.on_order")] == ("heuristic", "orders.placed")         # RabbitMQ, C# to Python
    assert q[("py.consumer.on_order", "py.tasks.send_receipt")] == ("heuristic", "send_receipt")           # Celery .delay
    assert q[("ts.events.ship", "ts.events.onShipped")] == ("heuristic", "order.shipped")                  # EventEmitter
    assert q[("ts.nest.Signup.register", "ts.nest.Listener.handleUserCreated")] == ("heuristic", "user_created")
    assert not any(addr in ("error", "exit") for _, addr in q.values())   # events streams and processes raise themselves


def test_databases(con5):
    d = links(con5, "db")
    assert d[("Shop.SqlOrderStore.Save", "Shop.Reports.Count")] == ("heuristic", "Orders")        # EF DbSet written, then read
    assert d[("Shop.SqlOrderStore.Save", "py.app.list_orders")] == ("heuristic", "Orders")        # __tablename__ = "orders"
    assert d[("py.audit.record", "py.audit.recent")] == ("heuristic", "audit")                    # SQL in strings
    assert d[("ts.repo.addUser", "ts.repo.listUsers")] == ("guess", "user ~ users")               # Prisma model, knex table
    assert not con5.execute("SELECT 1 FROM flow_steps WHERE via = 'db'").fetchone()               # data, not control


def test_rpc(con5):
    r = links(con5, "rpc")
    assert r[("py.client.greet", "Shop.GreeterService.SayHello")] == ("heuristic", "Greeter/SayHello")
    assert r[("Shop.GreeterCaller.Call", "Shop.GreeterService.SayHello")] == ("heuristic", "Greeter/SayHello")  # Client().SayHelloAsync


def test_flows_and_briefs_cross_the_new_channels(con5):
    main = "f5:csharp:shop::Shop.Program.Main(string[])"
    steps = {short(r[0]): r[1] for r in con5.execute(
        "SELECT callable_id, via FROM flow_steps WHERE flow_id = ?", (f"flow:{main}",))}
    assert steps["Shop.SqlOrderStore.Save"] == "di" and steps["Shop.OutboxWorker.ExecuteAsync"] == "di"
    assert steps["Shop.OrderPlacedHandler.Handle"] == "queue" and steps["py.consumer.on_order"] == "queue"
    assert steps["py.tasks.send_receipt"] == "queue"
    # Changing what is written reaches whoever reads it.
    r = change.assess(con5, "save more", [{"id": "f5:csharp:shop::Shop.SqlOrderStore.Save(Order)", "action": "behavior"}])
    got = {(c["channel"], c["from_name"], c["address"]) for c in r["channels"]}
    assert ("db", "app.py.list_orders", "Orders") in got and ("di", "IOrderStore.Save", "IOrderStore -> SqlOrderStore") in got
    r = change.assess(con5, "greet", [{"id": "f5:csharp:shop::Shop.GreeterService.SayHello(HelloRequest,ServerCallContext)",
                                       "action": "behavior"}])
    assert sorted((c["channel"], c["from_name"]) for c in r["channels"]) == [("rpc", "GreeterCaller.Call"), ("rpc", "client.py.greet")]
    stats = {row[0]: json.loads(row[1]) for row in con5.execute(
        "SELECT extractor, stats FROM extractor_coverage WHERE extractor LIKE 'communicates:%' AND status = 'ok'")}
    assert {"communicates:di", "communicates:queue", "communicates:db", "communicates:rpc"} <= set(stats)
    assert stats["communicates:di"]["registered_as_itself"] == 1


def endpoints(adapter, path, src):
    return [(e.channel, e.role, e.address) for e in adapter.parse("r", path, f"r:file:{path}", src.encode()).endpoints]


def test_shapes_that_are_not_channels():
    from leyline.adapters import csharp, python, typescript
    # HttpClient sends a request, not a message; a Mongo client is not a gRPC stub; requests.get is not a query.
    assert not endpoints(csharp, "a.cs", "class A { HttpClient _http; void F() { _http.SendAsync(new HttpRequestMessage()); } }")
    assert not endpoints(typescript, "a.ts", "export function f(url: string) { const c = new MongoClient(url); c.connect(); }")
    assert not endpoints(python, "a.py", "import requests\nURL = 'x'\ndef f():\n    return requests.get(URL)\n")


def test_registrations_overloads_and_factories(con5):
    di = links(con5, "di")
    assert di[("Shop.IClock.Now", "Shop.SystemClock.Now")][0] == "guess"        # sp.GetRequiredService<SystemClock>()
    printed = {(s, d) for s, d in con5.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'communicates'"
                                                " AND src_id LIKE '%IPrinter.Print%'")}
    # Each overload reaches its own, once, though the pair is registered twice.
    assert sorted(printed) == [("f5:csharp:shop::Shop.IPrinter.Print(int)", "f5:csharp:shop::Shop.Printer.Print(int)"),
                               ("f5:csharp:shop::Shop.IPrinter.Print(string)", "f5:csharp:shop::Shop.Printer.Print(string)")]


def test_messages_raised_or_only_created(con5):
    q = links(con5, "queue")
    assert q[("Shop.Shipment.Ship", "Shop.ShippedHandler.Handle")] == ("heuristic", "OrderShipped")       # AddDomainEvent
    # Created and handed to an outbox: the handler for its type is a guess.
    assert q[("Shop.Billing.Bill", "Shop.ReceiptRequestedEventHandler.Handle")] == ("guess", "ReceiptRequestedEvent")
    assert q[("ts.subs.RecipesResolver.addRecipe", "ts.subs.RecipesResolver.recipeAdded")] == ("heuristic", "recipeAdded")


def test_tables_from_bases_repositories_not_tests_or_migrations(con5):
    d = links(con5, "db")
    assert d[("py.models.bill", "py.models.invoices")] == ("heuristic", "Invoice")         # an ORM base, no table name
    assert d[("Shop.Billing.Close", "Shop.Reports.Count")] == ("heuristic", "Orders")       # IRepository<Order>
    assert ("Shop.SqlOrderStore.Save", "Shop.Billing.Pending") in d
    assert not any("test_" in s or "test_" in r or s == "py.migrations.0001_audit.upgrade" for s, r in d)
    stats = json.loads(con5.execute("SELECT stats FROM extractor_coverage WHERE extractor = 'communicates:db'").fetchone()[0])
    assert stats["in_tests"] >= 2 and stats["in_migrations"] == 1


def test_rpc_prefers_the_nearest_server(con5):
    r = links(con5, "rpc")
    assert ("rpc.py.client.hello", "rpc.py.server.Greeter.SayHello") in r
    assert ("rpc.py.client.hello", "Shop.GreeterService.SayHello") not in r       # same service, further away


def test_brief_names_the_channels_a_change_crosses(tmp_path):
    from leyline import spec
    import shutil
    work, db = tmp_path / "repo", tmp_path / "s.db"
    shutil.copytree(FIXTURE5, work)
    index(work, db, "f5")
    ch = work / "openspec" / "changes" / "stamp-orders"
    (ch / "specs" / "orders").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Stamp orders\n\n## Why\nSaves go unrecorded.\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `SqlOrderStore.Save` to stamp each order\n")
    (ch / "specs" / "orders" / "spec.md").write_text(
        "## MODIFIED Requirements\n### Requirement: Stamped orders\nThe store SHALL stamp orders.\n\n"
        "#### Scenario: Save\n- **WHEN** an order is saved\n- **THEN** it carries a stamp\n")
    c = store.connect(db)
    text = spec.brief_text(spec.brief(c, ch))
    c.close()
    assert "Crosses a di boundary (IOrderStore -> SqlOrderStore): calls to IOrderStore.Save reach SqlOrderStore.Save" in text
    assert "Crosses a db boundary (Orders): Billing.Pending, Reports.Count and app.py.list_orders read what SqlOrderStore.Save writes." in text
    # Each reader of what changed must agree with it, and no task names one (Signal item 5).
    assert "- app.py.list_orders (py/app.py): also reads what SqlOrderStore.Save writes" in text


def test_requests_through_a_wrapper_or_inject_find_their_route(tmp_path):
    """A client that calls its own fetch wrapper, and a test that calls server.inject, request a route as surely as
    fetch does. They are linked only when exactly one route of the program serves the path."""
    root = tmp_path / "web"
    (root / "test").mkdir(parents=True)
    (root / "server.ts").write_text(
        'import Fastify from "fastify";\nexport function build() {\n  const server = Fastify();\n'
        '  server.get("/api/things/:id", async () => ({ thing: 1 }));\n'
        '  server.post("/api/things", async () => ({ ok: true }));\n  return server;\n}\n')
    (root / "api.ts").write_text(
        'async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> { return (await fetch(url, init)).json(); }\n'
        'export const api = {\n  thing: (id: string) => apiFetch<{ thing: number }>(`/api/things/${id}`),\n'
        '  add: () => apiFetch("/api/things", { method: "POST" }),\n'
        '  nothing: () => apiFetch("/api/none/here"),\n'
        '  config: () => readConfig("/etc/app/config"),\n};\nfunction readConfig(p: string) { return p; }\n')
    (root / "test" / "server.test.ts").write_text(
        'import { build } from "../server";\nimport { it, expect } from "vitest";\n'
        'it("answers a thing", async () => {\n  const s = build();\n'
        '  const r = await s.inject({ method: "GET", url: "/api/things/7" });\n  expect(r.statusCode).toBe(200);\n});\n')
    db = tmp_path / "s.db"
    index(root, db, "web")
    c = store.connect(db)
    got = links(c, "http")
    assert got[("api.api.thing", "server.build")][1] == "GET /api/things/:id"
    assert got[("api.api.add", "server.build")][1] == "POST /api/things"
    assert any(src.endswith("test:answers-a-thing") for src, dst in got if dst == "server.build")
    assert set(got) == {("api.api.thing", "server.build"), ("api.api.add", "server.build"),
                        ("test.server.test.<module>/test:answers-a-thing", "server.build")}   # not nothing, not config
