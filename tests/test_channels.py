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
    assert links(con5, "rpc")[("py.client.greet", "Shop.GreeterService.SayHello")] == ("heuristic", "Greeter/SayHello")


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
    assert [(c["channel"], c["from_name"]) for c in r["channels"]] == [("rpc", "client.py.greet")]
    stats = {row[0]: json.loads(row[1]) for row in con5.execute(
        "SELECT extractor, stats FROM extractor_coverage WHERE extractor LIKE 'communicates:%' AND status = 'ok'")}
    assert {"communicates:di", "communicates:queue", "communicates:db", "communicates:rpc"} <= set(stats)
    assert stats["communicates:di"]["registered_as_itself"] == 1
