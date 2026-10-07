"""Channels that are not calls, beyond events, launches, HTTP and files: dependency injection, message queues
and buses, databases, and RPC.

Each adapter hands its syntax tree to `extract`, which records the ends of these channels as `Endpoint`s on
the file's result, placed in the function (or type) whose span holds them. `resolve` then links the ends
across the workspace as `communicates` edges. The two halves meet by name: a topic, a table, a message type,
a service and method, an interface. A link is "heuristic" when the names matched as written, and "guess"
when one side was inferred (a factory's `new X`, a table matched only after singular and plural are
folded together)."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Optional

from .model import CallSite, Edge, Endpoint, FileResult

CHANNELS = ("di", "queue", "db", "rpc")


def _t(node) -> str:
    return node.text.decode("utf8", "replace") if node is not None else ""


# -- shared shapes -----------------------------------------------------------------------------------------

# Names a topic or queue goes by in keyword arguments, most specific first: a RabbitMQ publish names an
# exchange and a routing key, and the key is the one a consumer binds to.
TOPIC_KW = ("topic", "topics", "subject", "routing_key", "routingKey", "queue", "queue_name", "queueName",
            "topicName", "channel", "stream", "exchange", "event", "pattern")
HANDLER_KW = ("callback", "cb", "handler", "listener", "on_message_callback", "on_message", "func", "fn", "f")
# A receiver that is a broker of some kind, for verbs too common to trust on anything (send, put).
BROKER = re.compile(r"(produc|publish|kafka|bus|broker|queue|channel|nats|sns|sqs|pubsub|redis|rabbit|amqp|emitter|"
                    r"events?$|hub|topic|mq|stream|sio|socket|client|proxy)", re.I)
# Event names that streams, sockets, processes and the DOM raise on their own. Code here that subscribes to one
# is listening to a library, and code that emits one rarely means the same subscriber.
COMMON_EVENTS = frozenset("""error data end close finish open message connect connection connected disconnect
disconnected exit ready change click load unload drain pipe unpipe readable response request timeout abort
listening upgrade resize scroll input submit keydown keyup mousedown mouseup focus blur online offline
beforeunload uncaughtException unhandledRejection SIGINT SIGTERM warning start stop done progress success fail
destroy init update""".split())
HANDLER_IFACES = {"IRequestHandler", "INotificationHandler", "IConsumer", "IHandleMessages", "ICommandHandler",
                  "IEventHandler", "IIntegrationEventHandler", "IQueryHandler", "IHandle", "IHandler",
                  "IStreamRequestHandler", "IMessageHandler", "IDomainEventHandler"}
HANDLE_METHODS = ("Handle", "HandleAsync", "Consume", "ConsumeAsync", "Execute", "ExecuteAsync", "Process", "Run")


def _sql_name(raw: str) -> Optional[str]:
    """A table name as written in SQL, without quotes, brackets or schema: [dbo].[Users] -> Users."""
    name = re.sub(r"[`\"\[\]]", "", raw).rsplit(".", 1)[-1]
    if not re.fullmatch(r"[A-Za-z_][\w$]*", name) or name.lower() in SQL_WORDS:
        return None
    return name


SQL_WORDS = frozenset("""select from where join on as set values into table only lateral unnest dual information_schema
pg_catalog sqlite_master generate_series json_each json_table openjson the a an this that it""".split())
_T = r"((?:[`\"\[]?[A-Za-z_][\w$]*[`\"\]]?\.){0,2}[`\"\[]?[A-Za-z_][\w$]*[`\"\]]?)"
_SQL_VERB = re.compile(r"^[\s(]*(select|insert|update|delete|with|merge|replace|upsert|truncate)\b", re.I)
_SQL_WRITES = [re.compile(p, re.I) for p in (
    rf"\binsert\s+(?:or\s+\w+\s+|ignore\s+)?into\s+{_T}", rf"\bupdate\s+(?:or\s+\w+\s+|only\s+)?{_T}\s+set\b",
    rf"\bdelete\s+from\s+(?:only\s+)?{_T}", rf"\bmerge\s+into\s+{_T}", rf"\breplace\s+into\s+{_T}",
    rf"\btruncate\s+(?:table\s+)?{_T}", rf"\bupsert\s+into\s+{_T}")]
_SQL_READS = [re.compile(p, re.I) for p in (rf"\bfrom\s+(?:only\s+)?{_T}", rf"\bjoin\s+{_T}")]
_SQL_NOT_TABLE = re.compile(r"\b(extract|substring|trim|position|overlay)\s*\([^()]*\)", re.I)


def sql_tables(text: str) -> list[tuple[str, str]]:
    """(role, table) for each table a SQL statement reads or writes. Only a string that starts like SQL, with
    its keywords in one case (SELECT ... FROM or select ... from), is read: prose starts "Select a file"."""
    m = _SQL_VERB.match(text)
    if not m or len(text) > 20000:
        return []
    verb = m.group(1)
    if not (verb.isupper() or verb.islower()):
        return []
    upper = verb.isupper()
    body = _SQL_NOT_TABLE.sub(" ", text)
    ctes = {c.lower() for c in re.findall(r"(\w+)\s+as\s*\(", body, re.I)}
    out = []
    for p in _SQL_WRITES:
        for w in p.finditer(body):
            kw = w.group(0).split()[0]
            if kw.isupper() != upper:
                continue
            name = _sql_name(w.group(1))
            if name and name.lower() not in ctes:
                out.append(("write", name))
            # The written table is not also read: blank out the clause before looking for FROM and JOIN.
            body = body[:w.start()] + " " * (w.end() - w.start()) + body[w.end():]
    for p in _SQL_READS:
        for r in p.finditer(body):
            kw = r.group(0).split()[0]
            if kw.isupper() != upper:
                continue
            name = _sql_name(r.group(1))
            if name and name.lower() not in ctes:
                out.append(("read", name))
    return list(dict.fromkeys(out))


# Database operations by name, for ORMs and document stores. A name is compared with underscores dropped and
# in lower case, so insert_one and insertOne are one operation.
DB_WRITE = frozenset("""create createmany insert insertone insertmany update updateone updatemany updatemany upsert delete
deleteone deletemany destroy remove save bulkcreate bulkupdate bulkwrite getorcreate updateorcreate replaceone
findoneandupdate findoneandreplace findoneanddelete findbyidandupdate findbyidanddelete findbyidandremove
findoneandremove softdelete restore increment decrement insertmany add addrange addasync addrangeasync
removerange updaterange attach executedelete executedeleteasync executeupdate executeupdateasync merge
deletebyid insertmany replace del truncate""".split())
DB_READ = frozenset("""find findall findone findmany findunique findfirst finduniqueorthrow findfirstorthrow findbyid
findbypk findby findoneby findandcount count countdocuments estimateddocumentcount aggregate groupby distinct
select get getornone getbyid filter all exclude first last exists where query watch values valueslist""".split())
PRISMA_OPS = frozenset("""findMany findUnique findFirst findUniqueOrThrow findFirstOrThrow count aggregate groupBy create
createMany createManyAndReturn update updateMany upsert delete deleteMany""".split())
MONGO_OPS = frozenset("""find findone insertone insertmany updateone updatemany replaceone deleteone deletemany aggregate
countdocuments estimateddocumentcount distinct findoneandupdate findoneandreplace findoneanddelete bulkwrite
watch drop createindex""".split())


def _op(name: str) -> str:
    return name.replace("_", "").lower()


def _db_role(name: str) -> Optional[str]:
    k = _op(name)
    return "write" if k in DB_WRITE else "read" if k in DB_READ else None


class _Where:
    """The innermost function, and type, a line sits in, from the spans of what the adapter already found."""

    def __init__(self, res: FileResult, file_id: str):
        self.file_id = file_id
        self.nodes = {n.id: n for n in res.nodes}
        self.fn_at: dict[int, str] = {}
        self.type_at: dict[int, str] = {}
        self.starts: list[tuple[int, int, str]] = []
        for kinds, at in ((("callable", "test"), self.fn_at), (("type",), self.type_at)):
            spans = sorted(((n.span_start, n.span_end, n.id) for n in res.nodes if n.kind in kinds and n.span_start),
                           key=lambda s: s[0] - s[1])   # outermost first, so an inner span paints over it
            for a, b, i in spans:
                for line in range(a, (b or a) + 1):
                    at[line] = i
            if at is self.fn_at:
                self.starts = sorted(spans)

    def fn(self, line: int) -> Optional[str]:
        return self.fn_at.get(line)

    def type(self, line: int) -> Optional[str]:
        return self.type_at.get(line)

    def next_fn(self, line: int, within: Optional[str] = None) -> Optional[str]:
        """The first function declared at or after a line: a TypeScript decorator sits outside the method it marks."""
        for a, _b, i in self.starts:
            if a >= line and (within is None or self.nodes[i].parent_id == within):
                return i
        return None


class _Out:
    """Collects endpoints for one file, once each."""

    def __init__(self, res: FileResult):
        self.res = res
        self.seen: set = set()

    def add(self, channel, role, src, address, line, method=None, literals=None, handler=None) -> None:
        if not src or not (address or role == "table"):   # a mapped class need not name its table
            return
        key = (channel, role, src, address, method, handler, tuple(literals or ()))
        if key in self.seen:
            return
        self.seen.add(key)
        self.res.endpoints.append(Endpoint(channel, role, src, address, line, method, list(literals or []), handler))


def extract(lang: str, tree, res: FileResult, file_id: str, consts: Optional[dict] = None) -> None:
    """Record the channel ends in one parsed file. A failure here loses this file's channels, not its parse."""
    try:
        where = _Where(res, file_id)
        out = _Out(res)
        {"python": _Python, "csharp": _CSharp, "typescript": _TypeScript}[lang](tree, where, out, consts or {}).run()
    except RecursionError:
        pass


# -- Python ------------------------------------------------------------------------------------------------

def _py_str(node) -> Optional[str]:
    if node is None or node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.children):
        return None
    return "".join(_t(c) for c in node.children if c.type == "string_content")


def _py_name(node) -> Optional[str]:
    """A dotted name (a.b.C), or None for anything else."""
    if node is not None and node.type in ("identifier", "attribute") and re.fullmatch(r"[\w.]+", _t(node)):
        return _t(node)
    return None


class _Python:
    DI_REGISTER = {"register", "bind", "add_singleton", "add_scoped", "add_transient", "register_singleton",
                   "register_transient", "register_scoped", "register_type"}
    PUB = {"publish", "basic_publish", "publish_message", "sendMessage", "send_message", "produce", "xadd",
           "lpush", "rpush", "send", "send_json", "put_message", "enqueue_message"}
    PUB_LOOSE = {"send", "send_json", "send_message", "put_message"}
    SUB = {"subscribe", "psubscribe", "basic_consume", "consume", "queue_bind", "blpop", "brpop", "xread",
           "xreadgroup", "KafkaConsumer", "AIOKafkaConsumer"}
    SESSION = re.compile(r"(session|db|tx|uow|conn|^s)$", re.I)   # a bare `s`, not requests

    def __init__(self, tree, where: _Where, out: _Out, consts: dict):
        self.tree, self.where, self.out = tree, where, out
        self.consts = {k: v[0] for k, v in consts.items() if len(v) == 1}
        self.sql_consts = {k: sql_tables(v) for k, v in self.consts.items()}
        self.sql_consts = {k: v for k, v in self.sql_consts.items() if v}
        self.rpc_vars: dict[str, str] = {}
        self.var_types: dict[tuple, str] = {}

    def _lit(self, node) -> Optional[str]:
        s = _py_str(node)
        if s is None and node is not None and node.type == "identifier":
            s = self.consts.get(_t(node))
        return s or None

    def run(self) -> None:
        stack = [self.tree.root_node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "call":
                self._call(n)
            elif t == "decorator":
                self._decorator(n)
            elif t == "string":
                s = _py_str(n)
                if s:
                    self._sql(s, n.start_point[0] + 1)
            elif t == "identifier" and _t(n) in self.sql_consts:
                cid = self.where.fn(n.start_point[0] + 1)
                if cid and not cid.endswith("<module>"):
                    for role, table in self.sql_consts[_t(n)]:
                        self.out.add("db", role, cid, table, n.start_point[0] + 1, "sql")
            elif t == "assignment":
                self._assign(n)
            elif t == "class_definition":
                self._class(n)
            stack.extend(reversed(n.children))

    def _sql(self, text: str, line: int) -> None:
        cid = self.where.fn(line)
        if cid:
            for role, table in sql_tables(text):
                self.out.add("db", role, cid, table, line, "sql")

    def _assign(self, n) -> None:
        left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
        if left is None or right is None:
            return
        line = n.start_point[0] + 1
        lname = _t(left)
        if right.type == "call":
            fn = _t(right.child_by_field_name("function"))
            last = fn.rsplit(".", 1)[-1]
            svc = None
            if last.endswith("Stub") and len(last) > 4 and last[0].isupper():
                svc = last[:-4]                               # helloworld_pb2_grpc.GreeterStub(channel)
            elif last == "Client" and "." in fn:
                svc = fn.rsplit(".", 2)[-2]                   # Thrift: Calculator.Client(protocol)
            if svc:
                self.rpc_vars[lname] = svc
            cid = self.where.fn(line)
            if cid and left.type == "identifier":
                head = fn.rsplit(".", 2)[-2] if "." in fn else ""
                rpos, _ = self._args(right.child_by_field_name("arguments"))
                first = _py_name(rpos[0]) if rpos else None
                if last[:1].isupper():
                    self.var_types[(cid, lname)] = last                     # x = User(...)
                elif head[:1].isupper() and not head.isupper():
                    self.var_types[(cid, lname)] = head                     # x = User.model_validate(...)
                elif first and first.rsplit(".", 1)[-1][:1].isupper() and self.SESSION.search(fn.rsplit(".", 1)[0]):
                    self.var_types[(cid, lname)] = first.rsplit(".", 1)[-1]  # x = session.get(User, id)
        elif left.type == "identifier" and lname in ("__tablename__", "db_table", "__collection__"):
            table = self._lit(right)
            tid = self.where.type(line)
            if table and tid:
                node = self.where.nodes[tid]
                if node.name == "Meta" and node.parent_id in self.where.nodes:
                    tid = node.parent_id                      # Django: class Meta: db_table = "x"
                self.out.add("db", "table", tid, table, line, "orm", [self.where.nodes[tid].name])

    def _class(self, n) -> None:
        sup = n.child_by_field_name("superclasses")
        if sup is None:
            return
        line = n.start_point[0] + 1
        tid = self.where.type(line)
        if not tid:
            return
        text = _t(sup)
        for svc in re.findall(r"(\w+)Servicer\b", text):
            self.out.add("rpc", "serve", tid, svc.rsplit(".", 1)[-1], line, "grpc")
        for svc in re.findall(r"(\w+)\.Iface\b", text):
            self.out.add("rpc", "serve", tid, svc, line, "thrift")
        if re.search(r"\b(Model|Base|SQLModel|DeclarativeBase|Document|DynamicDocument|EmbeddedDocument)\b", text) \
                and not re.search(r"\b(BaseModel|BaseSettings)\b", text):
            # A class built on an ORM base is a table whether or not it names one.
            self.out.add("db", "table", tid, "", line, "orm", [self.where.nodes[tid].name])

    def _args(self, args):
        pos, kw = [], {}
        if args is None or args.type != "argument_list":
            return pos, kw
        for a in args.named_children:
            if a.type == "keyword_argument":
                kw[_t(a.child_by_field_name("name"))] = a.child_by_field_name("value")
            elif a.type not in ("comment", "list_splat", "dictionary_splat"):
                pos.append(a)
        return pos, kw

    def _strings_of(self, nodes) -> list[str]:
        out = []
        for a in nodes:
            if a is None:
                continue
            if a.type in ("list", "tuple", "set"):
                out += [s for s in (self._lit(x) for x in a.named_children) if s]
            else:
                s = self._lit(a)
                if s:
                    out.append(s)
        return out

    def _topic(self, pos, kw) -> list[str]:
        for k in TOPIC_KW:
            if k in kw:
                found = self._strings_of([kw[k]])
                if found:
                    return found
        return self._strings_of(pos)

    def _handler(self, pos, kw) -> Optional[str]:
        for k in HANDLER_KW:
            if k in kw and _py_name(kw[k]):
                return _py_name(kw[k])
        for a in pos:
            if _py_name(a) and not _t(a) in self.consts:
                return _py_name(a)
        return None

    def _call(self, n) -> None:
        fn = n.child_by_field_name("function")
        if fn is None:
            return
        line = n.start_point[0] + 1
        cid = self.where.fn(line)
        if not cid:
            return
        full = _t(fn)
        last = full.rsplit(".", 1)[-1]
        obj = fn.child_by_field_name("object") if fn.type == "attribute" else None
        recv = _t(obj) if obj is not None else ""
        pos, kw = self._args(n.child_by_field_name("arguments"))
        out = self.out

        # Dependency injection.
        if last in ("Depends", "Security"):
            dep = pos[0] if pos else kw.get("dependency")
            name = _py_name(dep)
            if name is None and dep is None:
                name = self._annotated_type(n)
            if name:
                out.add("di", "depend", cid, name, line, "fastapi")
            return
        if last in self.DI_REGISTER and len(pos) >= 1:
            svc = _py_name(pos[0])
            impl = _py_name(pos[1]) if len(pos) > 1 else None
            for k in ("to", "implementation", "concrete", "impl", "provider"):
                impl = impl or _py_name(kw.get(k))
            if svc and impl and svc != impl and svc.rsplit(".", 1)[-1][:1].isupper() and impl.rsplit(".", 1)[-1][:1].isupper():
                out.add("di", "provide", cid, svc, line, "type", [impl])
                return
        if last == "to" and obj is not None and obj.type == "call":   # binder.bind(IFoo).to(Foo)
            inner = obj.child_by_field_name("function")
            ipos, _ = self._args(obj.child_by_field_name("arguments"))
            if _t(inner).endswith("bind") and ipos and _py_name(ipos[0]) and pos and _py_name(pos[0]):
                out.add("di", "provide", cid, _py_name(ipos[0]), line, "type", [_py_name(pos[0])])
                return

        # RPC: a call on a stub made from generated code.
        svc = self.rpc_vars.get(recv)
        if svc is None and obj is not None and obj.type == "call":
            inner = _t(obj.child_by_field_name("function")).rsplit(".", 1)[-1]
            if inner.endswith("Stub") and len(inner) > 4:
                svc = inner[:-4]
        if svc:
            out.add("rpc", "call", cid, f"{svc}/{last}", line, "grpc")
            return

        # Task queues: the task function itself is named, so it is resolved like a call.
        if last in ("delay", "apply_async") and _py_name(obj):
            out.add("queue", "publish", cid, recv.rsplit(".", 1)[-1], line, "task", handler=recv)
            return
        if last == "send_task" and pos and self._lit(pos[0]):
            out.add("queue", "publish", cid, self._lit(pos[0]).rsplit(".", 1)[-1], line, "task")
            return
        if last in ("enqueue", "enqueue_call", "enqueue_in", "enqueue_at", "add_job", "schedule"):
            h = _py_name(kw.get("func") or kw.get("f")) or next((_py_name(a) for a in pos if _py_name(a)), None)
            if h and h.rsplit(".", 1)[-1][:1].islower():
                out.add("queue", "publish", cid, h.rsplit(".", 1)[-1], line, "task", handler=h)
            return
        # Django signals: post_paid.send(sender=...) to @receiver(post_paid) and post_paid.connect(handler).
        if last in ("send", "send_robust") and obj is not None and obj.type == "identifier" and "sender" in kw:
            out.add("queue", "publish", cid, recv, line, "signal")
            return
        if last == "connect" and obj is not None and obj.type == "identifier" and pos and _py_name(pos[0]):
            out.add("queue", "subscribe", cid, recv, line, "signal", handler=_py_name(pos[0]))
            return
        # In-process and socket events.
        if last == "emit" and pos:
            topic = self._lit(pos[0])
            if topic:
                out.add("queue", "publish", cid, topic, line, "event")
            return
        if last in ("on", "once", "add_listener", "on_event") and pos and fn.type == "attribute":
            topic = self._lit(pos[0])
            if topic:
                out.add("queue", "subscribe", cid, topic, line, "event", handler=self._handler(pos[1:], kw))
            return
        # Brokers: Kafka, RabbitMQ, Redis, NATS and the like, matched by topic or queue name.
        if last in self.PUB and (last not in self.PUB_LOOSE or BROKER.search(recv)):
            for topic in self._topic(pos, kw)[:1]:
                out.add("queue", "publish", cid, topic, line, "topic")
            return
        if last in self.SUB:
            if last == "basic_consume" and not kw and len(pos) >= 2 and _py_name(pos[0]):
                h = _py_name(pos[0])                          # pika before 1.0: basic_consume(callback, queue=...)
            else:
                h = self._handler(pos, kw)
            topics = [self._lit(kw[k]) for k in ("routing_key", "routingKey") if k in kw and self._lit(kw[k])] \
                if last == "queue_bind" else []
            for topic in topics or self._topic(pos, kw):
                out.add("queue", "subscribe", cid, topic, line, "topic", handler=h)
            return

        # Databases.
        self._db(n, fn, full, last, obj, pos, kw, cid, line)

    def _annotated_type(self, call) -> Optional[str]:
        """Depends() with no argument: the dependency is the parameter's own type."""
        cur = call.parent
        for _ in range(4):
            if cur is None:
                return None
            if cur.type == "typed_default_parameter":
                return (re.findall(r"[A-Z]\w*", _t(cur.child_by_field_name("type"))) or [None])[0]
            if cur.type == "subscript" and _t(cur.child_by_field_name("value")).endswith("Annotated"):
                first = cur.child_by_field_name("subscript")
                return _py_name(first)
            cur = cur.parent
        return None

    def _db(self, n, fn, full, last, obj, pos, kw, cid, line) -> None:
        out = self.out
        # Only the outermost call of a chain is read, so X.objects.filter().update() is one write, not a read too.
        p = n.parent
        if p is not None and p.type == "attribute" and p.parent is not None and p.parent.type == "call":
            return
        chain, base = [], fn
        while base is not None:
            if base.type == "call":
                base = base.child_by_field_name("function")
            elif base.type == "attribute":
                chain.append((_t(base.child_by_field_name("attribute")), base))
                base = base.child_by_field_name("object")
            else:
                break
        names = [c[0] for c in chain]
        write = any(_db_role(x) == "write" or re.match(r"(create|update|delete|bulk|insert|remove)_", x) for x in names)
        # Django and other active-record models: User.objects.filter(...), User.select(), Book.create(...)
        if base is not None and base.type == "identifier" and _t(base)[:1].isupper() and names:
            first = names[-1]
            if first == "objects" or _db_role(first):
                out.add("db", "write" if write else "read", cid, "model:" + _t(base), line, "orm")
                return
        # Document stores: db.users.find(...), db["users"].insert_one(...), db.get_collection("users").find(...)
        if fn.type == "attribute" and _op(last) in MONGO_OPS and obj is not None:
            coll = None
            if obj.type == "attribute" and re.search(r"(db|database|mongo)\w*$", _t(obj.child_by_field_name("object")), re.I):
                coll = _t(obj.child_by_field_name("attribute"))
            elif obj.type == "subscript":
                coll = self._lit(obj.child_by_field_name("subscript"))
            elif obj.type == "call" and _t(obj.child_by_field_name("function")).endswith(("get_collection", "collection")):
                ipos, _ = self._args(obj.child_by_field_name("arguments"))
                coll = self._lit(ipos[0]) if ipos else None
            if coll:
                out.add("db", _db_role(last) or ("write" if _op(last) in ("drop", "createindex") else "read"), cid,
                        coll, line, "document")
                return
        # SQLAlchemy: session.query(User).filter(...), select(User), insert(User), session.get(User, 1)
        cur = n
        while cur is not None and cur.type == "call":
            cfn = cur.child_by_field_name("function")
            cpos, _ = self._args(cur.child_by_field_name("arguments"))
            cfull = _t(cfn)
            clast = cfull.rsplit(".", 1)[-1]
            model = _py_name(cpos[0]) if cpos else None
            mname = model.rsplit(".", 1)[-1] if model else ""
            if mname[:1].isupper() and not mname.isupper():   # a class, not a constant: requests.get(USER_URL)
                if clast in ("query", "get", "scalars") and cfn.type == "attribute" \
                        and (clast != "get" or self.SESSION.search(_t(cfn.child_by_field_name("object")))):
                    out.add("db", "write" if write else "read", cid, "model:" + mname, line, "orm")
                elif cfull in ("select", "sa.select", "sqlalchemy.select", "sqlmodel.select"):
                    out.add("db", "read", cid, "model:" + mname, line, "orm")
                elif cfull in ("insert", "update", "delete") or cfull.endswith((".insert", ".update", ".delete")) and \
                        cfull.split(".")[0] in ("sa", "sqlalchemy", "sqlmodel"):
                    out.add("db", "write", cid, "model:" + mname, line, "orm")
            cur = cfn.child_by_field_name("object") if cfn is not None and cfn.type == "attribute" else None
        if last in ("add", "add_all", "delete", "merge") and fn.type == "attribute" and self.SESSION.search(_t(obj) if obj is not None else "") and pos:
            a = pos[0]
            items = a.named_children if a.type == "list" else [a]
            for it in items:
                m = None
                if it.type == "call":
                    m = _t(it.child_by_field_name("function")).rsplit(".", 1)[-1]
                elif it.type == "identifier":
                    m = self.var_types.get((cid, _t(it)))
                if m and m[:1].isupper():
                    out.add("db", "write", cid, "model:" + m, line, "orm")

    def _decorator(self, n) -> None:
        line = n.start_point[0] + 1
        text = _t(n).lstrip("@").strip()
        cid = self.where.fn(line)
        defn = n.parent.child_by_field_name("definition") if n.parent is not None and n.parent.type == "decorated_definition" else None
        fname = _t(defn.child_by_field_name("name")) if defn is not None and defn.type == "function_definition" else None
        if not cid or not fname:
            return
        head = text.split("(", 1)[0]
        last = head.rsplit(".", 1)[-1]
        call = next((c for c in n.named_children if c.type == "call"), None)
        pos, kw = self._args(call.child_by_field_name("arguments")) if call is not None else ([], {})
        if last in ("task", "shared_task", "actor", "periodic_task", "job"):
            names = [fname] + ([self._lit(kw["name"]).rsplit(".", 1)[-1]] if "name" in kw and self._lit(kw["name"]) else [])
            for name in dict.fromkeys(names):
                self.out.add("queue", "subscribe", cid, name, line, "task")
        elif head == "receiver" or last == "receiver":
            sigs = []
            for a in pos:
                sigs += [_t(x) for x in (a.named_children if a.type in ("list", "tuple") else [a]) if x.type == "identifier"]
            for s in sigs:
                self.out.add("queue", "subscribe", cid, s, line, "signal")
        elif last == "event" and re.search(r"(sio|socket)", head, re.I):
            self.out.add("queue", "subscribe", cid, fname, line, "event")    # python-socketio: @sio.event
        elif last in ("on", "once"):
            for topic in self._strings_of(pos[:1]):
                self.out.add("queue", "subscribe", cid, topic, line, "event")
        elif last in ("subscriber", "subscribe", "consumer", "listener", "agent", "on_message", "event_handler",
                      "listen", "handler", "consume", "message_handler", "topic_handler"):
            for topic in self._topic(pos, kw):
                self.out.add("queue", "subscribe", cid, topic, line, "topic")
        elif last == "publisher":                    # FastStream: what the function returns goes to the topic
            for topic in self._topic(pos, kw)[:1]:
                self.out.add("queue", "publish", cid, topic, line, "topic")


# -- C# ----------------------------------------------------------------------------------------------------

_CS_STRINGS = ("string_literal", "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression")


def _cs_str(node) -> Optional[str]:
    if node is None or node.type not in _CS_STRINGS:
        return None
    if node.type == "interpolated_string_expression":
        return "".join("?" if c.type == "interpolation" else _t(c) for c in node.children
                       if c.type not in ('"', '$"', '@$"', '$@"', 'interpolation_start', 'interpolation_quote')).strip('"$@')
    text = _t(node)
    if node.type == "string_literal":
        return "".join(_t(c) for c in node.children if c.type in ("string_literal_content", "escape_sequence"))
    return text.lstrip("@").strip('"')


def _cs_generic(node) -> tuple[str, list[str]]:
    """(name, type arguments as written) of a name node: AddSingleton<IFoo, Foo> -> (AddSingleton, [IFoo, Foo])."""
    if node is None:
        return "", []
    if node.type == "generic_name":
        ident = next((c for c in node.children if c.type == "identifier"), None)
        targs = next((c for c in node.children if c.type == "type_argument_list"), None)
        return _t(ident), [_cs_type(c) for c in targs.named_children] if targs is not None else []
    return _t(node), []


def _cs_type(node) -> str:
    """The simple name of a type: Foo.Bar<int> -> Bar, IList<Foo> -> IList."""
    text = re.sub(r"<.*", "", _t(node)).strip().rstrip("?")
    return text.rsplit(".", 1)[-1]


def _cs_qual_type(node) -> str:
    return re.sub(r"<.*", "", _t(node)).strip().rstrip("?")


class _CSharp:
    DI_ADD = {"AddSingleton", "AddScoped", "AddTransient", "TryAddSingleton", "TryAddScoped", "TryAddTransient",
              "AddKeyedSingleton", "AddKeyedScoped", "AddKeyedTransient", "TryAddEnumerable", "RegisterType",
              "Register", "RegisterSingleton", "AddHttpClient"}
    SEND = {"Send", "Publish", "SendAsync", "PublishAsync", "SendLocal", "InvokeAsync", "Dispatch", "DispatchAsync",
            "SchedulePublish", "ScheduleSend", "Raise", "RaiseAsync", "Enqueue", "EnqueueAsync",
            "AddDomainEvent", "RaiseDomainEvent", "RegisterDomainEvent", "QueueDomainEvent"}   # domain events, sent on save
    # A type named like a message, created anywhere: handed to a wrapper or an outbox, it may still reach its handler.
    MESSAGE_NAME = re.compile(r"[A-Z]\w*(Event|Command|Query|Message|Notification|Request)$")
    PUB = {"Produce", "ProduceAsync", "BasicPublish", "BasicPublishAsync", "Publish", "PublishAsync", "CreateSender",
           "SendToQueue", "PublishMessage", "SendMessageAsync"}
    SUB = {"Subscribe", "SubscribeAsync", "BasicConsume", "BasicConsumeAsync", "CreateProcessor", "CreateReceiver",
           "QueueBind", "QueueBindAsync", "CreateSessionProcessor"}
    CTX = re.compile(r"(context|db|ctx|dbcontext|_uow|unitofwork)$", re.I)

    def __init__(self, tree, where: _Where, out: _Out, consts: dict):
        self.tree, self.where, self.out = tree, where, out
        self.consts: dict[str, str] = {}
        self.field_types: dict[tuple, str] = {}   # (type id, member) -> type as written, type arguments and all
        self.var_types: dict[tuple, str] = {}     # (function id, local) -> type as written
        self.methods: dict[tuple, list] = defaultdict(list)   # (type id, name) -> methods
        for n in where.nodes.values():
            if n.kind == "callable":
                self.methods[(n.attrs.get("type_id"), n.name)].append(n)

    def _lit(self, node) -> Optional[str]:
        s = _cs_str(node)
        if s is None and node is not None and node.type in ("identifier", "member_access_expression"):
            s = self.consts.get(_t(node).rsplit(".", 1)[-1])
        return s or None

    def run(self) -> None:
        root = self.tree.root_node
        nodes = []
        stack = [root]
        while stack:   # first the declarations, so a use reads a type or constant declared further down
            n = stack.pop()
            nodes.append(n)
            t = n.type
            if t == "field_declaration" or t == "property_declaration":
                self._member(n)
            elif t in ("parameter", "local_declaration_statement"):
                self._local(n)
            stack.extend(reversed(n.children))
        self.sql_consts = {k: v for k, v in ((k, sql_tables(v)) for k, v in self.consts.items()) if v}
        for n in nodes:
            t = n.type
            if t == "invocation_expression":
                self._invocation(n)
            elif t in _CS_STRINGS:
                line = n.start_point[0] + 1
                cid = self.where.fn(line)
                s = _cs_str(n)
                if cid and s:
                    for role, table in sql_tables(s):
                        self.out.add("db", role, cid, table, line, "sql")
            elif t == "identifier" and _t(n) in self.sql_consts:
                cid = self.where.fn(n.start_point[0] + 1)
                if cid:
                    for role, table in self.sql_consts[_t(n)]:
                        self.out.add("db", role, cid, table, n.start_point[0] + 1, "sql")
            elif t == "member_access_expression":
                self._dbset_read(n)
            elif t in ("class_declaration", "record_declaration"):
                self._class(n)
            elif t == "object_creation_expression":
                name = _cs_type(n.child_by_field_name("type"))
                cid = self.where.fn(n.start_point[0] + 1)
                if cid and self.MESSAGE_NAME.fullmatch(name) and not name.startswith("Http"):
                    self.out.add("queue", "publish", cid, name, n.start_point[0] + 1, "message-new")

    def _member(self, n) -> None:
        line = n.start_point[0] + 1
        tid = self.where.type(line)
        if n.type == "property_declaration":
            tnode, name = n.child_by_field_name("type"), _t(n.child_by_field_name("name"))
            if tid and tnode is not None:
                self.field_types[(tid, name)] = _t(tnode).strip()
                gname, targs = _cs_generic(tnode)
                if gname == "DbSet" and targs:
                    self.out.add("db", "table", tid, name, line, "dbset", [targs[0]])
            return
        decl = next((c for c in n.named_children if c.type == "variable_declaration"), None)
        if decl is None:
            return
        tnode = decl.child_by_field_name("type")
        for d in decl.named_children:
            if d.type != "variable_declarator":
                continue
            name = _t(d.child_by_field_name("name") or next((c for c in d.children if c.type == "identifier"), None))
            if tid and tnode is not None:
                self.field_types[(tid, name)] = _t(tnode).strip()
            value = next((c for c in d.named_children if c.type in _CS_STRINGS), None)
            if value is not None and _cs_str(value):
                self.consts[name] = _cs_str(value)

    def _local(self, n) -> None:
        line = n.start_point[0] + 1
        cid = self.where.fn(line)
        if not cid:
            return
        if n.type == "parameter":
            tnode = n.child_by_field_name("type")
            if tnode is not None:
                self.var_types[(cid, _t(n.child_by_field_name("name")))] = _t(tnode).strip()
            return
        decl = next((c for c in n.named_children if c.type == "variable_declaration"), None)
        if decl is None:
            return
        tnode = decl.child_by_field_name("type")
        for d in decl.named_children:
            if d.type != "variable_declarator":
                continue
            name = _t(next((c for c in d.children if c.type == "identifier"), None))
            if tnode is not None and _t(tnode) != "var":
                self.var_types[(cid, name)] = _t(tnode).strip()
            else:
                made = next((c for c in d.named_children if c.type == "object_creation_expression"), None)
                if made is not None:
                    self.var_types[(cid, name)] = _cs_qual_type(made.child_by_field_name("type"))

    def _type_of(self, expr, cid) -> Optional[str]:
        raw = self._raw_type_of(expr, cid)
        return re.sub(r"<.*", "", raw).strip().rstrip("?") if raw else None

    def _raw_type_of(self, expr, cid) -> Optional[str]:
        if expr is None:
            return None
        if expr.type == "object_creation_expression":
            return _t(expr.child_by_field_name("type"))
        if expr.type == "invocation_expression":
            # GetClient().Call(): typed by what a method of this class declares it returns.
            f = expr.child_by_field_name("function")
            if f is not None and f.type == "member_access_expression" and _t(f.child_by_field_name("expression")) == "this":
                f = f.child_by_field_name("name")
            own = self.where.nodes.get(cid)
            if f is None or f.type != "identifier" or own is None:
                return None
            found = self.methods.get((own.attrs.get("type_id"), _t(f)))
            if not found:
                return None
            m = re.search(r"([\w.<>,? ]+?)\s+" + re.escape(found[0].name) + r"\s*[<(]", found[0].attrs.get("signature", ""))
            ret = m.group(1).split()[-1] if m else ""
            inner = re.fullmatch(r"(?:Value)?Task<(.+)>", ret)
            return (inner.group(1) if inner else ret) or None
        name = _t(expr)
        if expr.type == "member_access_expression" and _t(expr.child_by_field_name("expression")) == "this":
            name = _t(expr.child_by_field_name("name"))
        elif expr.type != "identifier":
            return None
        own = self.where.nodes.get(cid)
        tid = own.attrs.get("type_id") if own is not None else None
        return self.var_types.get((cid, name)) or (self.field_types.get((tid, name)) if tid else None)

    def _args(self, args):
        pos, kw = [], {}
        if args is None:
            return pos, kw
        for a in args.named_children:
            if a.type != "argument" or not a.named_children:
                continue
            kids = a.named_children
            if len(kids) >= 2 and kids[0].type == "identifier" and any(_t(c) == ":" for c in a.children):
                kw[_t(kids[0])] = kids[-1]
            else:
                pos.append(kids[-1])
        return pos, kw

    def _topic(self, pos, kw) -> list[str]:
        for k in TOPIC_KW:
            if k in kw and self._lit(kw[k]):
                return [self._lit(kw[k])]
        return [s for s in (self._lit(a) for a in pos) if s][:1]

    def _invocation(self, n) -> None:
        fn = n.child_by_field_name("function")
        line = n.start_point[0] + 1
        cid = self.where.fn(line)
        if fn is None or not cid:
            return
        expr = None
        if fn.type == "member_access_expression":
            name, targs = _cs_generic(fn.child_by_field_name("name"))
            expr = fn.child_by_field_name("expression")
        else:
            name, targs = _cs_generic(fn)
        pos, kw = self._args(n.child_by_field_name("arguments"))
        out = self.out

        # Dependency injection: services.AddScoped<IFoo, Foo>(), AddHostedService<Worker>(), Autofac's As<IFoo>().
        if name in self.DI_ADD:
            typeofs = [_cs_type(a.named_children[0]) for a in pos if a.type == "typeof_expression" and a.named_children]
            if len(targs) >= 2:
                out.add("di", "provide", cid, targs[0], line, "type", [targs[1]])
            elif len(typeofs) >= 2:
                out.add("di", "provide", cid, typeofs[0], line, "type", [typeofs[1]])
            elif len(targs) == 1 or len(typeofs) == 1:
                svc = targs[0] if targs else typeofs[0]
                made = [_cs_type(m.child_by_field_name("type")) for a in pos for m in _descendants(a, "object_creation_expression")]
                # services.AddScoped<IFoo>(sp => sp.GetRequiredService<Foo>()): the factory hands back another registration.
                made += [ts[0] for a in pos for g in _descendants(a, "generic_name") for nm, ts in [_cs_generic(g)]
                         if nm in ("GetRequiredService", "GetService") and ts]
                made = [m for m in made if m != svc]
                out.add("di", "provide", cid, svc, line, "factory" if made else "self", made[:1])
            return
        if name == "AddHostedService" and targs:
            out.add("di", "start", cid, targs[0], line, "hosted")
            return
        if name == "As" and targs and expr is not None and expr.type == "invocation_expression":
            inner = expr.child_by_field_name("function")
            iname, itargs = _cs_generic(inner.child_by_field_name("name") if inner is not None and inner.type == "member_access_expression" else inner)
            if iname == "RegisterType" and itargs:
                out.add("di", "provide", cid, targs[0], line, "type", [itargs[0]])
            return

        # Messages sent by type, to the handler for that type: MediatR, MassTransit, NServiceBus, Wolverine.
        if name in self.SEND:
            first = pos[0] if pos else None
            if first is not None and first.type not in _CS_STRINGS and not (first.type == "identifier" and _t(first) in self.consts) \
                    and not re.search("http", _t(expr), re.I):   # HttpClient.SendAsync(request) sends a request, not a message
                msg = targs[0] if targs else None
                if msg is None and first is not None:
                    msg = self._type_of(first, cid)
                if msg and not msg.endswith("HttpRequestMessage"):
                    out.add("queue", "publish", cid, msg.rsplit(".", 1)[-1], line, "message")
                    return
        if name in ("Subscribe", "SubscribeAsync") and len(targs) >= 2:   # eventBus.Subscribe<TEvent, THandler>()
            out.add("queue", "subscribe", cid, targs[0], line, "message", handler="type:" + targs[1])
            return
        # Brokers by topic or queue name.
        if name in self.PUB:
            for topic in self._topic(pos, kw):
                out.add("queue", "publish", cid, topic, line, "topic")
                return
        if name in self.SUB:
            for topic in self._topic(pos, kw):
                out.add("queue", "subscribe", cid, topic, line, "topic")
                return

        # RPC: a call on a generated gRPC client, Greeter.GreeterClient.
        rtype = self._type_of(expr, cid)
        if rtype and rtype.endswith("Client") and len(rtype) > 6 and "Http" not in rtype:
            parts = rtype.split(".")
            svc = parts[-1][:-6]
            if svc and (len(parts) > 1 and parts[-2] == svc or "Grpc" in rtype or svc[0].isupper()):
                method = name[:-5] if name.endswith("Async") else name
                out.add("rpc", "call", cid, f"{svc}/{method}", line, "grpc" if len(parts) > 1 and parts[-2] == svc else "maybe")
                return

        # Entity Framework: context.Users.Add(x), context.Add(user), context.Set<User>(), ToTable("users").
        if name == "ToTable" and pos and self._lit(pos[0]) and expr is not None and expr.type == "invocation_expression":
            inner = expr.child_by_field_name("function")
            iname, itargs = _cs_generic(inner.child_by_field_name("name") if inner is not None and inner.type == "member_access_expression" else inner)
            if iname == "Entity" and itargs:
                tid = self.where.type(line) or cid
                out.add("db", "table", tid, self._lit(pos[0]), line, "fluent", [itargs[0]])
            return
        role = _db_role(name.removesuffix("Async"))
        repo = re.fullmatch(r"(?:[\w.]+\.)?I?\w*Repository(?:Base)?<\s*(?:[\w.]+\.)?(\w+)\s*>", self._raw_type_of(expr, cid) or "")
        if repo and not role and _op(name.removesuffix("Async")) in ("list", "firstordefault", "singleordefault", "single"):
            role = "read"
        if role and repo:
            # IRepository<Basket>: a repository typed by its entity reads and writes that entity's table.
            out.add("db", role, cid, "model:" + repo.group(1), line, "repository")
            return
        if role == "write" and expr is not None:
            if expr.type == "member_access_expression" and self.CTX.search(_t(expr.child_by_field_name("expression"))):
                out.add("db", "write", cid, "dbset:" + _t(expr.child_by_field_name("name")), line, "ef")
            elif self.CTX.search(_t(expr)) and pos:
                m = self._type_of(pos[0], cid)
                if m:
                    out.add("db", "write", cid, "model:" + m.rsplit(".", 1)[-1], line, "ef")
        if name == "Set" and targs and expr is not None and self.CTX.search(_t(expr)):
            out.add("db", "read", cid, "model:" + targs[0], line, "ef")

    def _dbset_read(self, n) -> None:
        """context.Users read anywhere but as the receiver of a write: a query over that table."""
        inner = n.child_by_field_name("expression")
        name = _t(n.child_by_field_name("name"))
        if inner is None or not name[:1].isupper() or not self.CTX.search(_t(inner)):
            return
        p = n.parent
        if p is not None and p.type == "invocation_expression" and p.child_by_field_name("function") == n:
            return   # context.SaveChanges(): a method, not a table
        if p is not None and p.type == "member_access_expression" and p.parent is not None \
                and p.parent.type == "invocation_expression" and _db_role(_t(p.child_by_field_name("name"))) == "write":
            return
        line = n.start_point[0] + 1
        cid = self.where.fn(line)
        if cid:
            self.out.add("db", "read", cid, "dbset:" + name, line, "ef")

    def _class(self, n) -> None:
        line = n.start_point[0] + 1
        tid = self.where.type(line)
        if not tid:
            return
        for al in (c for c in n.children if c.type == "attribute_list"):
            for a in (c for c in al.named_children if c.type == "attribute"):
                if _t(a.child_by_field_name("name")).rsplit(".", 1)[-1] in ("Table", "TableAttribute"):
                    s = next((x for x in _descendants(a, *_CS_STRINGS)), None)
                    if s is not None and _cs_str(s):
                        self.out.add("db", "table", tid, _cs_str(s), line, "attr", [self.where.nodes[tid].name])
        bases = next((c for c in n.children if c.type == "base_list"), None)
        if bases is None:
            return
        for b in bases.named_children:
            text = _t(b)
            m = re.fullmatch(r"(?:[\w.]+\.)?(\w+)\.(\w+)Base", text)
            if m and m.group(1) == m.group(2):
                self.out.add("rpc", "serve", tid, m.group(1), line, "grpc")
                continue
            if b.type == "generic_name":
                gname, targs = _cs_generic(b)
            elif b.type == "qualified_name" and b.named_children and b.named_children[-1].type == "generic_name":
                gname, targs = _cs_generic(b.named_children[-1])
            else:
                continue
            if gname in HANDLER_IFACES and targs:
                self.out.add("queue", "subscribe", tid, targs[0], line, "message", handler="type:")


def _descendants(node, *types):
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            yield n
        stack.extend(reversed(n.children))


# -- TypeScript --------------------------------------------------------------------------------------------

def _ts_str(node) -> Optional[str]:
    if node is None:
        return None
    if node.type == "string":
        return "".join(_t(c) for c in node.children if c.type in ("string_fragment", "escape_sequence"))
    if node.type == "template_string":
        return "".join("?" if c.type == "template_substitution" else _t(c) for c in node.children if c.type != "`")
    return None


def _ts_unwrap(node):
    while node is not None and node.type in ("parenthesized_expression", "await_expression", "as_expression",
                                             "non_null_expression", "satisfies_expression") and node.named_children:
        node = node.named_children[0]
    return node


def _ts_name(node) -> Optional[str]:
    node = _ts_unwrap(node)
    if node is not None and node.type in ("identifier", "member_expression") and re.fullmatch(r"[\w.$]+", _t(node)):
        return _t(node)
    return None


def _ts_pairs(obj) -> dict:
    out = {}
    if obj is not None and obj.type == "object":
        for p in obj.named_children:
            if p.type == "pair":
                k = _t(p.child_by_field_name("key")).strip("'\"")
                out[k] = p.child_by_field_name("value")
            elif p.type == "shorthand_property_identifier":
                out[_t(p)] = p
    return out


def _plural(name: str) -> str:
    low = name.lower()
    return low if low.endswith("s") else low[:-1] + "ies" if low.endswith("y") and low[-2:-1] not in "aeiou" else low + "s"


class _TypeScript:
    KNEX = {"knex", "db", "trx", "k", "database", "sql"}
    KNEX_WRITE = {"insert", "update", "del", "delete", "upsert", "merge", "truncate", "increment", "decrement", "onConflict"}

    def __init__(self, tree, where: _Where, out: _Out, consts: dict):
        self.tree, self.where, self.out = tree, where, out
        self.consts = {k: v[0] for k, v in consts.items() if len(v) == 1}
        self.field_types: dict[tuple, str] = {}
        self.rpc_vars: dict[str, str] = {}
        self.coll_vars: dict[str, str] = {}
        self.grpc = b"grpc" in tree.root_node.text.lower()   # new FooClient(...) is a stub only where gRPC is in sight

    def _lit(self, node) -> Optional[str]:
        node = _ts_unwrap(node)
        s = _ts_str(node)
        if s is None and node is not None and node.type in ("identifier", "member_expression"):
            s = self.consts.get(_t(node)) or self.consts.get(_t(node).rsplit(".", 1)[-1])
        if s is None and node is not None and node.type == "object":
            # NestJS message patterns: { cmd: 'sum' }
            pairs = {k: _ts_str(v) for k, v in _ts_pairs(node).items()}
            if pairs and all(pairs.values()) and not ({"topic", "topics", "messages"} & set(pairs)):
                s = ",".join(f"{k}:{v}" for k, v in sorted(pairs.items()))
        return s or None

    def run(self) -> None:
        nodes, stack = [], [self.tree.root_node]
        # Each node's parent, noted on the way down: tree-sitter finds a parent by walking down from the root, so
        # asking for it at every link of a long a.b().c()... chain was quadratic.
        self.up: dict = {}
        while stack:
            n = stack.pop()
            nodes.append(n)
            if n.type in ("call_expression", "member_expression"):
                for c in n.children:
                    self.up[c.id] = n
            if n.type == "method_definition" and _t(n.child_by_field_name("name")) == "constructor":
                self._ctor_fields(n)
            elif n.type in ("public_field_definition", "field_definition"):
                tid = self.where.type(n.start_point[0] + 1)
                ann = n.child_by_field_name("type")
                if tid and ann is not None:
                    self.field_types[(tid, _t(n.child_by_field_name("name")))] = _t(ann).lstrip(":").strip()
            stack.extend(reversed(n.children))
        self.sql_consts = {k: v for k, v in ((k, sql_tables(v)) for k, v in self.consts.items()) if v}
        for n in nodes:
            t = n.type
            if t == "call_expression":
                self._call(n)
            elif t == "decorator":
                self._decorator(n)
            elif t in ("string", "template_string"):
                s = _ts_str(n)
                line = n.start_point[0] + 1
                cid = self.where.fn(line)
                if s and cid:
                    for role, table in sql_tables(s):
                        self.out.add("db", role, cid, table, line, "sql")
            elif t == "identifier" and _t(n) in self.sql_consts:
                cid = self.where.fn(n.start_point[0] + 1)
                if cid and not cid.endswith("<module>"):
                    for role, table in self.sql_consts[_t(n)]:
                        self.out.add("db", role, cid, table, n.start_point[0] + 1, "sql")
            elif t == "object":
                self._provider(n)
            elif t == "variable_declarator" or t == "assignment_expression":
                self._assign(n)

    def _ctor_fields(self, n) -> None:
        tid = self.where.type(n.start_point[0] + 1)
        params = n.child_by_field_name("parameters")
        for p in (params.named_children if params is not None and tid else []):
            pat, ann = p.child_by_field_name("pattern"), p.child_by_field_name("type")
            if pat is not None and ann is not None:
                self.field_types[(tid, _t(pat))] = _t(ann).lstrip(":").strip()
                deco = next((c for c in p.children if c.type == "decorator"), None)
                if deco is not None:
                    call = next((c for c in deco.named_children if c.type == "call_expression"), None)
                    if call is not None and _t(call.child_by_field_name("function")) in ("Inject", "inject"):
                        args = [a for a in call.child_by_field_name("arguments").named_children]
                        token = self._lit(args[0]) or _ts_name(args[0]) if args else None
                        typ = re.sub(r"<.*", "", _t(ann).lstrip(":").strip())
                        cid = self.where.fn(p.start_point[0] + 1)
                        if token and cid and token != typ:
                            self.out.add("di", "inject", cid, token, p.start_point[0] + 1, "token", [typ])

    def _assign(self, n) -> None:
        if n.type == "variable_declarator":
            left, right = n.child_by_field_name("name"), _ts_unwrap(n.child_by_field_name("value"))
        else:
            left, right = n.child_by_field_name("left"), _ts_unwrap(n.child_by_field_name("right"))
        if left is None or right is None:
            return
        key = _t(left)
        line = n.start_point[0] + 1
        if right.type == "new_expression":
            ctor = _t(right.child_by_field_name("constructor"))
            args = right.child_by_field_name("arguments")
            last = ctor.rsplit(".", 1)[-1]
            if last.endswith("Client") and len(last) > 6 and last[0].isupper() and "Http" not in last and self.grpc:
                self.rpc_vars[key] = last[:-6]                 # grpc-js static code: new GreeterClient(addr, creds)
            elif "." in ctor and args is not None and "credentials" in _t(args):
                self.rpc_vars[key] = last                      # proto-loader: new hello_proto.Greeter(addr, creds)
        elif right.type == "call_expression":
            fn = _t(right.child_by_field_name("function"))
            args = [a for a in (right.child_by_field_name("arguments").named_children if right.child_by_field_name("arguments") else [])]
            last = fn.rsplit(".", 1)[-1]
            if last == "getService" and args and self._lit(args[0]):
                self.rpc_vars[key] = self._lit(args[0])        # NestJS: client.getService<HeroesService>('HeroesService')
            elif last == "collection" and args and self._lit(args[0]):
                self.coll_vars[key] = self._lit(args[0])
            elif last == "model" and args and self._lit(args[0]) and left.type == "identifier":
                name = self._lit(args[0])                      # mongoose.model('User', schema[, 'collection'])
                coll = self._lit(args[2]) if len(args) > 2 else None
                src = self.where.fn(line) or self.where.file_id
                self.out.add("db", "table", src, coll or _plural(name), line, "mongoose", [key, name])
            elif last == "define" and args and self._lit(args[0]) and left.type == "identifier":
                src = self.where.fn(line) or self.where.file_id   # sequelize.define('user', {...})
                table = self._lit(_ts_pairs(args[2]).get("tableName")) if len(args) > 2 else None
                self.out.add("db", "table", src, table or _plural(self._lit(args[0])), line, "sequelize", [key])

    def _provider(self, n) -> None:
        """{ provide: X, useClass: Y }, as Angular and NestJS take them."""
        pairs = _ts_pairs(n)
        impl = _ts_name(pairs.get("useClass")) or _ts_name(pairs.get("useExisting"))
        if not impl:
            return
        line = n.start_point[0] + 1
        cid = self.where.fn(line) or self.where.file_id
        svc = self._lit(pairs["provide"]) or _ts_name(pairs["provide"]) if "provide" in pairs else None
        if svc is None:   # tsyringe: container.register("Token", { useClass: Foo })
            call = n.parent.parent if n.parent is not None else None
            if call is not None and call.type == "call_expression" and _t(call.child_by_field_name("function")).endswith("register"):
                args = call.child_by_field_name("arguments").named_children
                svc = (self._lit(args[0]) or _ts_name(args[0])) if args else None
        if svc and svc != impl:
            self.out.add("di", "provide", cid, svc, line, "type", [impl])

    def _call(self, n) -> None:
        fn = _ts_unwrap(n.child_by_field_name("function"))
        argsn = n.child_by_field_name("arguments")
        args = [a for a in argsn.named_children if a.type != "comment"] if argsn is not None and argsn.type == "arguments" else []
        line = n.start_point[0] + 1
        cid = self.where.fn(line)
        if fn is None or not cid:
            return
        out = self.out
        if fn.type == "identifier":
            name, obj, recv = _t(fn), None, ""
        elif fn.type == "member_expression":
            name, obj = _t(fn.child_by_field_name("property")), _ts_unwrap(fn.child_by_field_name("object"))
            # A receiver this long is a builder chain or an inline expression, not a named client or collection;
            # reading its text at every link of a.b().c().d()... made a long chain quadratic.
            recv = _t(obj) if obj is not None and obj.end_byte - obj.start_byte <= 512 else ""
        else:
            return

        # Dependency injection: Inversify's container.bind<IFoo>(TYPES.Foo).to(Foo).
        if name in ("to", "toSelf") and obj is not None and obj.type == "call_expression":
            inner = _ts_unwrap(obj.child_by_field_name("function"))
            if inner is not None and inner.type == "member_expression" and _t(inner.child_by_field_name("property")) == "bind":
                targs = obj.child_by_field_name("type_arguments")
                iargs = obj.child_by_field_name("arguments").named_children
                svc = _t(targs.named_children[0]) if targs is not None and targs.named_children else (
                    (self._lit(iargs[0]) or _ts_name(iargs[0])) if iargs else None)
                impl = _ts_name(args[0]) if args else (_ts_name(iargs[0]) if iargs else None)
                if svc and impl and svc != impl:
                    out.add("di", "provide", cid, svc, line, "type", [impl])
            return

        # RPC.
        if obj is not None and recv in self.rpc_vars:
            out.add("rpc", "call", cid, f"{self.rpc_vars[recv]}/{name}", line, "grpc")
            return
        if name == "addService" and len(args) >= 2:
            svc_text = _t(args[0])
            svc = svc_text.rsplit(".", 2)[-2] if svc_text.endswith(".service") else re.sub(r"Service$", "", svc_text.rsplit(".", 1)[-1])
            for k, v in _ts_pairs(_ts_unwrap(args[1])).items():
                h = _ts_name(v) if v is not None and v.type != "shorthand_property_identifier" else k
                out.add("rpc", "serve-fn", cid, f"{svc}/{k}", line, "grpc", handler=h)
            return

        # Events and brokers.
        first = args[0] if args else None
        if name == "emit" and first is not None:
            topic = self._lit(first)
            if topic:
                fam = "topic" if re.search(r"(client|proxy)$", recv, re.I) else "event"
                out.add("queue", "publish", cid, topic, line, fam)
            return
        if name in ("on", "once", "addListener", "prependListener", "subscribe") and first is not None and self._lit(first) \
                and _ts_unwrap(first).type in ("string", "identifier", "member_expression"):
            fam = "event" if name != "subscribe" else "topic"
            h = _ts_name(args[1]) if len(args) > 1 else None
            out.add("queue", "subscribe", cid, self._lit(first), line, fam, handler=h)
            return
        if name in ("send", "publish", "sendToQueue", "produce", "xadd", "lpush", "rpush", "publishMessage", "emitAsync") \
                and first is not None and (name not in ("send",) or BROKER.search(recv)):
            pairs = _ts_pairs(_ts_unwrap(first))
            topic = self._lit(pairs.get("topic")) if "topic" in pairs else None
            if topic is None and name == "publish" and len(args) >= 2:
                topic = next((s for s in (self._lit(a) for a in args[:2]) if s), None)   # amqplib: publish(exchange, key, ...)
            topic = topic or self._lit(first)
            if topic:
                out.add("queue", "publish", cid, topic, line, "event" if name == "emitAsync" else "topic")
            return
        if name in ("subscribe", "consume", "bindQueue", "psubscribe", "blpop", "brpop", "asyncIterator",
                    "asyncIterableIterator") and first is not None:   # the last two: graphql-subscriptions' PubSub
            pairs = _ts_pairs(_ts_unwrap(first))
            topics = []
            if "topic" in pairs and self._lit(pairs["topic"]):
                topics = [self._lit(pairs["topic"])]
            elif "topics" in pairs and pairs["topics"] is not None:
                topics = [s for s in (self._lit(x) for x in pairs["topics"].named_children) if s]
            elif name == "bindQueue" and len(args) >= 3:
                topics = [s for s in (self._lit(a) for a in args[1:3]) if s][-1:]
            elif self._lit(first):
                topics = [self._lit(first)]
            h = _ts_name(args[1]) if len(args) > 1 else None
            for topic in topics:
                out.add("queue", "subscribe", cid, topic, line, "topic", handler=h)
            return

        # Databases: only the outermost call of a chain is read.
        p = self.up.get(n.id)
        if p is not None and p.type == "member_expression" and self.up.get(p.id) is not None \
                and self.up[p.id].type == "call_expression":
            return
        self._db(n, fn, name, obj, recv, args, cid, line)

    def _db(self, n, fn, name, obj, recv, args, cid, line) -> None:
        out = self.out
        chain, base = [], n
        while base is not None:
            base = _ts_unwrap(base)
            if base is None:
                break
            if base.type == "call_expression":
                f = _ts_unwrap(base.child_by_field_name("function"))
                bargs = base.child_by_field_name("arguments")
                if f is not None and f.type == "member_expression":
                    chain.append((_t(f.child_by_field_name("property")), bargs))
                    base = f.child_by_field_name("object")
                else:
                    chain.append((_t(f), bargs))
                    break
            elif base.type == "member_expression":
                chain.append((_t(base.child_by_field_name("property")), None))
                base = base.child_by_field_name("object")
            else:
                break
        names = [c[0] for c in chain]
        write = any(x in self.KNEX_WRITE or _db_role(x) == "write" for x in names)
        # Prisma: prisma.user.findMany(), this.prisma.post.create(), tx.user.update()
        if name in PRISMA_OPS and obj is not None and obj.type == "member_expression" \
                and re.search(r"(prisma|db|tx|client)$", _t(obj.child_by_field_name("object")), re.I):
            out.add("db", "write" if name in PRISMA_WRITE else "read", cid, "prisma:" + _t(obj.child_by_field_name("property")),
                    line, "prisma")
            return
        # Knex: knex('users').where(...), db('users').insert(...), knex.from('users')
        tables = []
        for nm, a in chain:
            al = [x for x in a.named_children if x.type != "comment"] if a is not None else []
            s = self._lit(al[0]) if al else None
            if s is None:
                continue
            if nm in self.KNEX or nm in ("from", "into", "table", "join", "innerJoin", "leftJoin", "rightJoin"):
                if re.fullmatch(r"[A-Za-z_][\w.]*", s):
                    tables.append((nm, s.split(" ")[0]))
        if tables and (chain[-1][0] in self.KNEX or any(nm in ("from", "into", "table") for nm, _ in tables)):
            for nm, table in tables:
                role = "write" if write and nm not in ("join", "innerJoin", "leftJoin", "rightJoin", "from") else "read"
                out.add("db", role, cid, table, line, "knex")
            return
        # Document stores: db.collection('users').find(), users.insertOne() where users = db.collection('users')
        if obj is not None and _op(name) in MONGO_OPS:
            coll = None
            if obj.type == "call_expression" and _t(obj.child_by_field_name("function")).endswith("collection"):
                oa = obj.child_by_field_name("arguments").named_children
                coll = self._lit(oa[0]) if oa else None
            elif recv in self.coll_vars:
                coll = self.coll_vars[recv]
            if coll:
                out.add("db", _db_role(name) or "read", cid, coll, line, "document")
                return
        # Repositories typed by their entity: this.users.find() where users: Repository<User>.
        if obj is not None and obj.type == "member_expression" and _t(obj.child_by_field_name("object")) == "this":
            own = self.where.nodes.get(cid)
            tid = own.parent_id if own is not None else None
            ftype = self.field_types.get((tid, _t(obj.child_by_field_name("property"))), "")
            m = re.fullmatch(r"(?:\w+\.)?(?:Repository|MongoRepository|TreeRepository|Model|EntityRepository|ModelType)<\s*(\w+)", ftype.split(">")[0])
            if m and (_db_role(name) or name in DB_TYPEORM):
                out.add("db", "write" if write or _db_role(name) == "write" else "read", cid, "model:" + m.group(1), line, "orm")
                return
        # Active-record and model calls: User.findAll(), Post.create(), getRepository(User).find()
        if obj is not None and obj.type == "identifier" and _t(obj)[:1].isupper() and _db_role(name):
            out.add("db", _db_role(name), cid, "model:" + _t(obj), line, "orm")
            return
        if obj is not None and obj.type == "call_expression" and _db_role(name):
            inner = _t(obj.child_by_field_name("function")).rsplit(".", 1)[-1]
            ia = obj.child_by_field_name("arguments").named_children
            if inner == "getRepository" and ia and _ts_name(ia[0]):
                out.add("db", _db_role(name), cid, "model:" + _ts_name(ia[0]).rsplit(".", 1)[-1], line, "orm")

    def _decorator(self, n) -> None:
        call = next((c for c in n.named_children if c.type == "call_expression"), None)
        ident = next((c for c in n.named_children if c.type in ("identifier", "member_expression")), None)
        dname = _t(call.child_by_field_name("function")) if call is not None else _t(ident)
        dname = dname.rsplit(".", 1)[-1]
        args = [a for a in call.child_by_field_name("arguments").named_children if a.type != "comment"] if call is not None else []
        line = n.start_point[0] + 1
        parent = n.parent
        if parent is not None and parent.type in ("class_declaration", "export_statement", "abstract_class_declaration"):
            cls = parent if parent.type != "export_statement" else next(
                (c for c in parent.named_children if c.type in ("class_declaration", "abstract_class_declaration")), None)
            tid = self.where.type(cls.start_point[0] + 1) if cls is not None else None
            if tid and dname in ("Entity", "Table", "Schema", "ViewEntity"):
                first = _ts_unwrap(args[0]) if args else None
                table = self._lit(first) if first is not None and first.type == "string" else None
                if table is None and first is not None:
                    pairs = _ts_pairs(first)
                    table = self._lit(pairs.get("name")) or self._lit(pairs.get("tableName")) or self._lit(pairs.get("collection"))
                self.out.add("db", "table", tid, table or "", line, "entity", [self.where.nodes[tid].name])
            return
        if parent is None or parent.type != "class_body":
            return
        tid = self.where.type(line)
        cid = self.where.next_fn(line, tid)
        if not cid:
            return
        first = args[0] if args else None
        if dname in ("EventPattern", "MessagePattern", "RabbitSubscribe", "Subscribe", "Consumer", "SqsMessageHandler",
                     "KafkaListener", "Process"):
            pairs = _ts_pairs(_ts_unwrap(first)) if first is not None else {}
            topic = self._lit(pairs.get("routingKey")) or self._lit(pairs.get("topic")) or self._lit(pairs.get("queue")) \
                if dname == "RabbitSubscribe" else (self._lit(first) if first is not None else None)
            if topic:
                self.out.add("queue", "subscribe", cid, topic, line, "task" if dname == "Process" else "topic")
        elif dname in ("OnEvent", "SubscribeMessage", "OnMessage"):
            topic = self._lit(first) if first is not None else None
            if topic:
                self.out.add("queue", "subscribe", cid, topic, line, "event")
        elif dname == "GrpcMethod" and first is not None and self._lit(first):
            method = self._lit(args[1]) if len(args) > 1 else None
            if method is None:
                fname = self.where.nodes[cid].name
                method = fname[:1].upper() + fname[1:]
            self.out.add("rpc", "serve-fn", cid, f"{self._lit(first)}/{method}", line, "grpc")


PRISMA_WRITE = frozenset("create createMany createManyAndReturn update updateMany upsert delete deleteMany".split())
DB_TYPEORM = frozenset("save insert update delete remove softDelete restore upsert increment decrement clear find findOne "
                       "findBy findOneBy findAndCount count createQueryBuilder exist exists query".split())


# -- resolution --------------------------------------------------------------------------------------------

def _repo_of(node_id: str) -> str:
    return node_id.split(":", 1)[0]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", name.lower())


def _stem(key: str) -> str:
    """Singular and plural folded together, so users and User meet (as a guess)."""
    if key.endswith("ies") and len(key) > 4:
        return key[:-3] + "y"
    if key.endswith(("sses", "xes", "ches", "shes")):
        return key[:-2]
    if key.endswith("s") and not key.endswith("ss") and len(key) > 3:
        return key[:-1]
    return key


TEST_DIRS = frozenset("test tests __tests__ spec specs e2e testing".split())
_TEST_SEG = re.compile(r"(^|[._-]|[a-z])Tests?$")                    # Basket.UnitTests, Application.FunctionalTests
_TEST_FILE = re.compile(r"^(test_.*\.py|.*_test\.py|conftest\.py|.*\.(test|spec)\.[cm]?[jt]sx?|.*Tests?\.cs)$")


def _test_path(path: str) -> bool:
    segs = path.replace("\\", "/").split("/")
    return any(s.lower() in TEST_DIRS or _TEST_SEG.search(s) for s in segs[:-1]) or bool(_TEST_FILE.match(segs[-1]))


def _migration_path(path: str) -> bool:
    segs = [s.lower() for s in path.replace("\\", "/").split("/")[:-1]]
    return any(s in ("migrations", "migration") for s in segs) or ("alembic" in segs and "versions" in segs)


def _shared_dirs(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/")[:-1], b.split("/")[:-1]):
        if x != y:
            break
        n += 1
    return n


class _Resolver:
    def __init__(self, ix):
        self.ix = ix
        self.by: dict[str, list] = defaultdict(list)   # channel -> [(endpoint, file id, language)]
        for fid, res in ix.results.items():
            lang = ix.file_lang[fid]
            for e in res.endpoints:
                if e.channel in CHANNELS and (e.src_id in ix.nodes or e.src_id == fid):
                    self.by[e.channel].append((e, fid, lang))

    def edge(self, src, dst, precision, attrs) -> bool:
        ix = self.ix
        if src == dst or src not in ix.nodes or dst not in ix.nodes:
            return False
        ix.edges.append(Edge("communicates", src, dst, precision, attrs))
        return True

    def callable_named(self, lang: str, fid: str, src: str, expr: str) -> Optional[str]:
        """The function a name written at `src` refers to: handle, self.on_message, tasks.add, this.onMessage."""
        ix = self.ix
        if expr.startswith("type:"):
            return None
        parts = expr.split(".")
        name = parts[-1]
        receiver = None if len(parts) == 1 else ("this" if parts[0] in ("self", "this") and len(parts) == 2 else ".".join(parts[:-1]))
        n = ix.nodes.get(src)
        enclosing = None
        cur = n
        while cur is not None:
            if cur.kind == "type":
                enclosing = cur.id
                break
            cur = ix.nodes.get(cur.parent_id) if cur.parent_id else None
        before_edges, before_stats = len(ix.edges), {k: dict(v) for k, v in ix.stats.items()}
        found = None
        for argc in (1, 0, 2, 3):
            call = CallSite(src, name, receiver, None, argc, 0, enclosing)
            ix._guessed, ix._call = False, None
            try:
                got = ix._resolve_call(lang, fid, call) or []
            except Exception:
                got = []
            got = [g for g in got if g.kind in ("callable", "test")]
            if got and not ix._guessed:
                found = got[0].id
                break
        del ix.edges[before_edges:]
        ix.stats.clear()
        ix.stats.update({k: Counter(v) for k, v in before_stats.items()})
        if found is None and receiver is None:
            # A function declared in the same file under that name.
            own = [c for c in ix.by_name.get((lang, name), ()) if ix.file_of.get(c.id) == fid]
            if len(own) == 1:
                found = own[0].id
        return found

    def type_named(self, lang: str, name: str, src: str) -> Optional[str]:
        name = re.sub(r"<.*", "", name).strip().rsplit(".", 1)[-1]
        if not name:
            return None
        try:
            tid = self.ix._type(lang, name, src)
        except Exception:
            tid = None
        if tid is None:
            cands = self.ix.types_by_name.get((lang, name), [])
            tid = cands[0] if len(cands) == 1 else None
        return tid

    def in_tests(self, nid: str) -> bool:
        n = self.ix.nodes.get(nid)
        cur = n
        while cur is not None and cur.kind in ("callable", "test"):
            if cur.kind == "test" or cur.attrs.get("is_test") or cur.attrs.get("is_fixture"):
                return True
            cur = self.ix.nodes.get(cur.parent_id)
        return n is not None and bool(n.path) and _test_path(n.path)

    def members(self, tid: str) -> dict[str, list]:
        """Methods of a type and the types it builds on, nearest first."""
        out: dict[str, list] = {}
        for t in self.ix._chain(tid):
            for name, fns in self.ix.members.get(t, {}).items():
                out.setdefault(name, fns)
        return out

    # -- dependency injection ----------------------------------------------------------------------------

    def di(self) -> None:
        st = self.ix.channel_stats["di"]
        injects: dict[str, set] = defaultdict(set)   # token -> types declared where it is injected
        for e, fid, lang in self.by["di"]:
            if e.role == "inject":
                for typ in e.literals:
                    injects[e.address].add(typ)
        linked_pairs: set = set()
        for e, fid, lang in self.by["di"]:
            if e.role == "provide":
                st["registrations"] += 1
                if not e.literals:
                    st["registered_as_itself"] += 1
                    continue
                impl = self.type_named(lang, e.literals[0], e.src_id)
                services = [e.address]
                if not re.fullmatch(r"[\w.]+", e.address) or (e.address not in self._type_names(lang) and e.address in injects):
                    services = sorted(injects.get(e.address, ()))   # a token: the type is whatever is declared where it is injected
                svcs = [s for s in (self.type_named(lang, s, e.src_id) for s in services) if s]
                if impl is None or not svcs:
                    st["registrations_outside"] += 1
                    continue
                precision = "guess" if e.method == "factory" else "heuristic"
                linked = again = 0
                for svc in svcs:
                    if svc == impl:
                        continue
                    impl_members = self.members(impl)
                    for name, fns in self.ix.members.get(svc, {}).items():
                        for f in fns:
                            if f.kind != "callable" or name in (".ctor", "constructor", "__init__"):
                                continue
                            argc, params = f.attrs.get("argc_max"), f.id.rsplit("(", 1)[-1]
                            cands = [c for c in impl_members.get(name, []) if c.kind == "callable" and c.id != f.id]
                            same = [c for c in cands if c.id.rsplit("(", 1)[-1] == params] or \
                                [c for c in cands if c.attrs.get("argc_max") == argc] or cands
                            for c in same[:1]:
                                if (f.id, c.id) in linked_pairs:
                                    again += 1      # registered twice (two hosts, or a test host): one link
                                    continue
                                linked_pairs.add((f.id, c.id))
                                if self.edge(f.id, c.id, precision, {
                                        "channel": "di", "address": f"{self.ix.nodes[svc].name} -> {self.ix.nodes[impl].name}",
                                        "registered_in": e.src_id, "registered_at": e.line}):
                                    linked += 1
                st["registrations_linked"] += bool(linked or again)
                st["links"] += linked
            elif e.role == "start":
                tid = self.type_named(lang, e.address, e.src_id)
                ms = self.members(tid) if tid else {}
                for name in ("ExecuteAsync", "StartAsync", "Run", "RunAsync"):
                    fns = [f for f in ms.get(name, []) if f.kind == "callable"]
                    if fns and self.edge(e.src_id, fns[0].id, "heuristic", {
                            "channel": "di", "address": f"hosted {e.address}", "direction": "start", "line": e.line}):
                        st["hosted_services_linked"] += 1
                        st["links"] += 1
                        break
            elif e.role == "depend":
                st["dependencies"] += 1
                target = self.callable_named(lang, fid, e.src_id, e.address)
                if target is None:
                    tid = self.type_named(lang, e.address, e.src_id)
                    if tid:
                        ms = self.members(tid)
                        target = next((f.id for f in ms.get("__init__", []) + ms.get("__call__", []) if f.kind == "callable"), None)
                if target and self.edge(e.src_id, target, "heuristic", {"channel": "di", "address": f"Depends({e.address})", "line": e.line}):
                    st["links"] += 1
                    st["dependencies_linked"] += 1

    def _type_names(self, lang: str) -> set:
        if not hasattr(self, "_tn"):
            self._tn = {}
        if lang not in self._tn:
            self._tn[lang] = {k[1] for k in self.ix.types_by_name if k[0] == lang}
        return self._tn[lang]

    # -- queues, buses and pub/sub -----------------------------------------------------------------------

    def queues(self) -> None:
        st = self.ix.channel_stats["queue"]
        pubs, subs = [], defaultdict(list)    # family -> [(address, target, endpoint)]
        for e, fid, lang in self.by["queue"]:
            if e.role == "publish":
                pubs.append((e, fid, lang))
                continue
            target = e.src_id
            if e.handler and e.handler.startswith("type:"):
                tid = e.src_id if e.handler == "type:" else self.type_named(lang, e.handler[5:], e.src_id)
                ms = self.members(tid) if tid else {}
                target = next((f.id for m in HANDLE_METHODS for f in ms.get(m, []) if f.kind == "callable"), None)
                if target is None:
                    continue
            elif e.handler:
                target = self.callable_named(lang, fid, e.src_id, e.handler) or e.src_id
            if target not in self.ix.nodes or self.ix.nodes[target].kind not in ("callable", "test"):
                continue
            subs[e.method].append((e.address, target, e))
        # Messages only created come last, so a message also sent by name keeps its heuristic link.
        pubs.sort(key=lambda p: p[0].method == "message-new")
        st["publish_sites"] = sum(1 for e, _, _ in pubs if e.method not in ("signal", "message-new"))
        st["subscriptions"] = sum(len(v) for v in subs.values())
        exact: dict[tuple, list] = defaultdict(list)
        patterns = []
        for fam, lst in subs.items():
            for address, target, e in lst:
                if fam == "topic" and re.search(r"[*#>]|\+", address):
                    rx = re.escape(address).replace(r"\*", r"[^.]+").replace(r"\#", r".*").replace(r"\>", r".+").replace(r"\+", r"[^/]+")
                    patterns.append((fam, re.compile(rx + r"\Z"), address, target, e))
                else:
                    exact[(fam, address)].append((address, target, e))
        pairs = []
        for e, fid, lang in pubs:
            if e.method == "task" and e.handler:
                # The task is named in code (add.delay(...)): it is that function, wherever it is declared.
                target = self.callable_named(lang, fid, e.src_id, e.handler)
                if target:
                    pairs.append((e, target, e.address, "heuristic", None))
                    continue
            if e.method == "message-new":
                # Created, not visibly sent: a guess that it reaches the handler for its type.
                for a, t, s in exact.get(("message", e.address), []):
                    pairs.append((e, t, a, "guess", s))
                continue
            hits = [(a, t, s) for a, t, s in exact.get((e.method, e.address), [])]
            hits += [(a, t, s) for fam, rx, a, t, s in patterns if fam == e.method and rx.match(e.address)]
            if e.method == "event" and e.address in COMMON_EVENTS:
                st["too_common_to_link"] += bool(hits)
                continue
            if not hits:
                st["published_with_no_subscriber_here"] += e.method != "signal"
                continue
            for a, t, s in hits:
                pairs.append((e, t, a, "heuristic", s))
        fan = Counter((e.method, a) for e, _t, a, _p, _s in pairs)
        seen = set()
        for e, target, address, precision, sub in pairs:
            if fan[(e.method, address)] > 60:
                st["too_common_to_link"] += 1
                continue
            key = (e.src_id, target, address)
            if key in seen:
                continue
            seen.add(key)
            attrs = {"channel": "queue", "address": address, "kind": "message" if e.method == "message-new" else e.method,
                     "line": e.line}
            if e.method == "message-new":
                attrs["created_only"] = True
                st["linked_by_creation"] += 1
            if sub is not None and sub.src_id != target:
                attrs["subscriber"] = sub.src_id
            if self.edge(e.src_id, target, precision, attrs):
                st["links"] += 1

    # -- databases ---------------------------------------------------------------------------------------

    def db(self) -> None:
        ix = self.ix
        st = ix.channel_stats["db"]
        table_of: dict[str, str] = {}     # type id -> table name
        models: set[str] = set()          # type ids that are mapped to a table
        named: dict[str, str] = {}        # a model's name as code writes it (mongoose, sequelize) -> table
        dbsets: dict[str, Optional[str]] = {}   # DbSet property name -> its entity type
        for e, fid, lang in self.by["db"]:
            if e.role != "table":
                continue
            if e.method == "dbset":
                ent = self.type_named(lang, e.literals[0], e.src_id) if e.literals else None
                dbsets[e.address] = ent
                if ent:
                    models.add(ent)
                continue
            if e.method == "fluent":
                ent = self.type_named(lang, e.literals[0], e.src_id) if e.literals else None
                if ent:
                    table_of[ent] = e.address
                    models.add(ent)
                continue
            if e.method in ("mongoose", "sequelize"):
                for lit in e.literals:
                    named[lit] = e.address
                continue
            node = ix.nodes.get(e.src_id)
            if node is not None and node.kind == "type":
                models.add(node.id)
                if e.address:
                    table_of[node.id] = e.address
        for name, ent in dbsets.items():   # an entity's own [Table] or ToTable wins over its DbSet's name
            if ent:
                table_of.setdefault(ent, name)
        uses = []
        for e, fid, lang in self.by["db"]:
            if e.role not in ("read", "write"):
                continue
            node = ix.nodes.get(e.src_id)
            if node is not None and _migration_path(node.path or ""):
                st["in_migrations"] += 1      # a schema change, not data one side leaves for the other
                continue
            if self.in_tests(e.src_id):
                st["in_tests"] += 1           # a test sets up and reads back its own rows
                continue
            addr = e.address
            table = None
            if addr.startswith("model:"):
                name = addr[6:]
                tid = self.type_named(lang, name, e.src_id)
                if tid and (tid in models or any(t in models for t in ix._chain(tid)[1:])):
                    table = table_of.get(tid) or ix.nodes[tid].name
                elif name in named:
                    table = named[name]
                else:
                    st["not_a_model"] += 1
                    continue
            elif addr.startswith("dbset:"):
                name = addr[6:]
                if name not in dbsets:
                    continue
                table = table_of.get(dbsets[name] or "", name)
            elif addr.startswith("prisma:"):
                table = addr[7:]
            else:
                table = addr
            uses.append((e, table))
        st["read_sites"] = sum(1 for e, _ in uses if e.role == "read")
        st["write_sites"] = sum(1 for e, _ in uses if e.role == "write")
        st["tables"] = len({_norm(t) for _, t in uses})
        readers: dict[str, list] = defaultdict(list)
        stems: dict[str, list] = defaultdict(list)
        for e, table in uses:
            if e.role == "read":
                readers[_norm(table)].append((e, table))
                stems[_stem(_norm(table))].append((e, table))
        pairs = {}
        for e, table in uses:
            if e.role != "write":
                continue
            key = _norm(table)
            for r, rt in readers.get(key, []):
                pairs.setdefault((e.src_id, r.src_id), (table, "heuristic"))
            for r, rt in stems.get(_stem(key), []):
                if _norm(rt) != key:
                    pairs.setdefault((e.src_id, r.src_id), (f"{table} ~ {rt}", "guess"))
        fan = Counter(_stem(_norm(t.split(" ~ ")[0])) for t, _ in pairs.values())
        for (w, r), (table, precision) in sorted(pairs.items()):
            if w == r:
                continue
            if fan[_stem(_norm(table.split(" ~ ")[0]))] > 400:
                st["too_common_to_link"] += 1
                continue
            if self.edge(w, r, precision, {"channel": "db", "address": table}):
                st["links"] += 1

    # -- RPC ---------------------------------------------------------------------------------------------

    def rpc(self) -> None:
        ix = self.ix
        st = ix.channel_stats["rpc"]
        servers: dict[str, list] = defaultdict(list)   # service (lower case) -> [(method lower case, function id)]
        for e, fid, lang in self.by["rpc"]:
            if e.role == "serve":
                st["services"] += 1
                for name, fns in self.members(e.src_id).items():
                    for f in fns:
                        if f.kind == "callable":
                            servers[e.address.lower()].append((name.lower(), f.id))
            elif e.role == "serve-fn":
                st["services"] += 0
                svc, _, method = e.address.partition("/")
                target = self.callable_named(lang, fid, e.src_id, e.handler) if e.handler else e.src_id
                if target:
                    servers[svc.lower()].append((method.lower(), target))
        seen = set()
        for e, fid, lang in self.by["rpc"]:
            if e.role != "call":
                continue
            svc, _, method = e.address.partition("/")
            impls = servers.get(svc.lower(), [])
            if not impls:
                if e.method != "maybe":
                    st["calls_to_services_outside"] += 1
                continue
            st["call_sites"] += 1
            m = method.lower()
            hit = list(dict.fromkeys(f for name, f in impls if name == m or name == m + "async"))
            if not hit:
                st["calls_with_no_method_here"] += 1
                continue
            if len(hit) > 1:
                # Several servers of one service (examples, or one per language): the nearest by directory.
                here = ix.nodes[e.src_id].path or ""
                near = [_shared_dirs(here, ix.nodes[f].path or "") for f in hit]
                best = max(near)
                if near.count(best) < len(hit):
                    st["servers_further_away"] += len(hit) - near.count(best)
                    hit = [f for f, d in zip(hit, near) if d == best]
            for target in hit:
                if (e.src_id, target) in seen:
                    continue
                seen.add((e.src_id, target))
                if self.edge(e.src_id, target, "heuristic", {"channel": "rpc", "address": e.address, "line": e.line}):
                    st["links"] += 1


def resolve(ix) -> None:
    """Link the two ends of each channel across the workspace."""
    r = _Resolver(ix)
    r.di()
    r.queues()
    r.db()
    r.rpc()
