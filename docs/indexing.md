# What is indexed

Which files and languages Leyline reads, how calls, types and channels are resolved, systems, annotations, flows, and how well the result holds up against a compiler.

Part of Leyline's documentation: the [README](../README.md) is the start, and [quick-reference.md](quick-reference.md) is one page of what to ask.

### Which files

The files git lists: tracked ones, and untracked ones `.gitignore` does not exclude. A directory that is
not a git repository (or one git refuses to read, such as a checkout owned by another user) is walked
instead, leaving out what git would: the patterns of your global excludes file (`core.excludesFile`, else
`~/.config/git/ignore`), of `.git/info/exclude` and of each `.gitignore`, escapes and trailing spaces read as
git reads them, so a file kept out of git such as `.env` stays off the map and its page. Some files are left out, and `map` names them with
the reason: other people's code (`node_modules`, a Go or Composer `vendor`), submodules and nested
repositories, symlinks to directories or out of the repository, files git lists that are gone, and source
files that are binary, minified, unreadable or larger than 5 MB (`LEYLINE_MAX_FILE_MB` changes that). A
file that does not parse is kept on the map as a file, without its contents, and named too.

Large repositories are parsed in worker processes (`LEYLINE_JOBS` sets how many). A worker that crashes
or is still on one batch of files after ten minutes (`LEYLINE_PARSE_TIMEOUT`, in seconds) costs only
the file it was on. Workers start by spawn on macOS and Windows and by fork on Linux;
`LEYLINE_START_METHOD` picks one. The compiler step (`--exact`) gives up after half an hour
(`LEYLINE_EXACT_TIMEOUT`) and keeps the syntax-based links.

### Languages

Any language with a tree-sitter grammar is indexed by one **generic adapter** that knows no
language. It reads what grammars have in common: node names (`function_declaration`,
`class_definition`, `call_expression`), the grammar's own tags query where it ships one, and how
typed languages write a variable's type (`Foo x`, `x: Foo`, `x = new Foo(`). From that it gets
declarations, nesting, calls with what the text shows of the receiver, imports matched to files by
path, tests by naming convention, and `main`. Adding a language is one line in
`adapters/generic.py` and a `pip install tree-sitter-<language>`.

Installed by default: C#, Python, TypeScript and JavaScript. `pip install 'leyline-code[languages]'`
adds Go, Rust, Java, Kotlin, Swift, C, C++, Ruby, PHP, Scala, Lua, Bash and GDScript.

C#, Python and TypeScript also have **hand-written adapters** that see more: receiver types,
overloads, field reads and writes, events, routes. They are used for those languages unless
`LEYLINE_GENERIC=1`. For other languages, a **SCIP index** (from the language's own indexer,
`--scip FILE`) replaces the generic adapter's guesses with the compiler's links.

`leyline grade <repo> <index.scip | roslyn>` measures either adapter against a compiler. Precision
counts only call sites the compiler resolved; recall counts the compiler's links between functions
on the map.

| Language (repository) | Compiler | Hand-written: right / found | Generic: right / found |
| --- | --- | --- | --- |
| TypeScript (Parlance, 160k lines) | scip-typescript | 100% / 98% | 99% / 97% |
| Python (Flask) | scip-python | 99% / 68% | 96% / 87% |
| C# (a private project) | Roslyn | 99.7% / 97% | 98% / 83% |

Where the generic adapter loses, it is on calls made on a variable whose type it cannot read from
the text. The hand-written Python adapter misses calls inside decorators (`@app.route(...)`).

| Record | Precision | Notes |
| --- | --- | --- |
| Modules, files, types, callables, fields, entry points | exact | A module is a directory with a project file, or a source file's own directory |
| `contains`, `has_field`, `exposes` | exact | |
| `imports`, `depends_on` | exact | From `using` and `import` statements and from `.csproj` references |
| `extends`, `implements`, `uses_type`, `instantiates` | heuristic | Type names resolved by name, limited to what the project can reference |
| `calls` | heuristic or guess | See below |
| `overrides` | heuristic | A method to the interface or base method it implements, by name and arity |
| `communicates` | heuristic or guess | Events and process launches; see Channels |
| Tests | exact | xUnit, NUnit and MSTest attributes, pytest names, and inline runners of the form `Run("name", () => { ... })` |

### How calls are resolved

Without a compiler, a call is linked only when its receiver's type can be worked out from what is
written: fields, parameters, typed locals, `new` expressions, collection element types, static type
names, and the declared return type of the call a value came from (`a.Make().Run()`, `var x =
a.Make(); x.Run()`). In C#, extension methods are matched on the type of their `this` parameter,
and overloads of equal length are narrowed by the arguments: a lambda's parameter count, the type
of a literal, a `new` expression or a typed local, and explicit type arguments. In Python, packages
under a source root are imported by their own name, with or without an `__init__.py` (a namespace
package), names re-exported through `__init__.py` are followed, and a test parameter filled by a pytest
fixture takes the fixture's return type. In TypeScript and JavaScript, an import through the `paths` of
the nearest `tsconfig.json` or `jsconfig.json` (`@ui/*`) is the local file, not a package; `extends` is
not followed. Three outcomes are counted per run:

- **resolved**: linked to a callable in the workspace
- **external**: the receiver's type is outside the workspace, or nothing in the workspace has that name
- **unresolved**: the name exists in the workspace but the receiver's type is unknown

A call on a receiver of unknown type is linked by name only when exactly one declaration of that
name is visible and the name was never seen on an outside type. In Python it is linked only to a method, and
never by a name the built-in types have (`add`, `get`, `close`, `split`). Those links are stored with
precision `guess`, not `heuristic`, and the map draws them differently.

### Channels

A `communicates` edge records code talking to code without a direct call. It points the way data
moves, and carries a `channel` and an `address`.

- **event** (C#): from the function that raises an event to the function that handles it. The
  address is the event. Subscriptions to events declared outside the workspace are counted, not linked.
- **process** (Python): from a `subprocess` call to the entry point of the program it launches, when
  the command names a project, a build output or a script in the workspace. If only the surrounding
  file names it, the edge is a `guess`.

- **http**: from a request to the route that serves it. Routes are read from decorators
  (`@app.route("/x")`, `@app.get`), ASP.NET attributes (`[HttpGet("x")]`) and `MapGet`-style calls;
  requests from `.get("/x")`-style calls and `HttpClient` methods with a literal path. A request is
  linked to a route declared inside the same test first, then to the only route in the repository
  that matches. When several match, in every language, the route that names more of the path outright
  wins (`/items/special` over `/items/{id}`, `/api/review/health` over `/api/review/:id`); if that leaves
  more than one, it is counted as ambiguous and left unlinked.
  In TypeScript (Fastify, Express, Hono, Koa routers), a route's inline handler
  (`server.get("/api/x", async (req) => ...)`, `server.route({ method, url, handler })`, or
  `server.get(path, { schema }, handler)`) is a function of its own, named for its route (`GET /api/x`)
  and nested in the function that registers it: the request lands on it, what it calls is its own, and
  the registrar registers it (kept as a call, so startup still reaches it). A handler given by name
  (`server.get("/x", listThings)`) is that function. When several handlers' routes still fit a request
  after that, a request with no method written is taken as a GET. In a pull request, an edit inside a handler touches
  its route and no other. An ASP.NET
  controller's `[Route("api/[controller]")]`, and a minimal API's `MapGroup("api/x")`, start the
  routes declared under them. A path handed to
  something the name does not give away is linked only when exactly one route serves it: a wrapper
  (`apiFetch("/api/x")`, `api_get("/api/x/1")`, `Get<T>("/api/x")`), `server.inject({ url })`, a
  session's `request("GET", "/x")`, a test client's `open("/x", method=...)`, a base address in a
  variable (`f"{BASE}/api/x"`, `BASE + "/api/x"`), `HttpClient` with a relative path
  (`GetAsync("api/x")`), RestSharp's `new RestRequest("api/x")` and `new HttpRequestMessage(...)`.
- **file**: from a function that writes a file to one that reads it, when the path fragments written
  in the two agree: the same file name, or the same directory and the same extension
  (`policies/*.bin`). These are always a `guess`, since paths are usually built at run time. Flows
  do not follow them: writing a file does not run its reader.

- **di**: from a method of an interface to the same method of the implementation a container
  registers for it (`services.AddScoped<IFoo, Foo>()` and `typeof` forms, Autofac's
  `RegisterType<Foo>().As<IFoo>()`, Python `container.register(IFoo, Foo)` and `bind(IFoo).to(Foo)`,
  Angular and NestJS `{ provide: X, useClass: Y }`, Inversify `bind<X>(…).to(Y)`), with
  `registered_in` and `registered_at` naming the registration. An implementation read off a factory
  (`sp => new Foo()`, `sp.GetRequiredService<Foo>()`) is a `guess`. Also from the registration to a
  hosted service's `ExecuteAsync`, and from a FastAPI endpoint to the function its `Depends()` names.
  Flows try the registered implementation before the other implementations.
- **queue**: from code that publishes to the handlers of the same topic, queue or message type:
  Kafka, RabbitMQ, Redis, NATS-style `publish`/`subscribe` with a literal or constant name, Node
  `emit`/`on`, NestJS `@EventPattern`/`@OnEvent`, graphql-subscriptions, Celery and RQ tasks
  (`send_receipt.delay()` reaches `send_receipt`), Django signals, and C# messages by type (MediatR
  `Send(new X())` and `AddDomainEvent` to `IRequestHandler<X>`/`INotificationHandler<X>`, and the
  like). A message type that is created with a handler in sight but not visibly sent (handed to an
  outbox or a wrapper) is linked as a `guess` with `created_only`. Event names that streams and
  sockets raise themselves (`error`, `data`, `exit`) are not linked.
- **db**: from a function that writes a table to one that reads it: SQL in string literals and
  constants, SQLAlchemy and SQLModel, Django managers, EF `DbSet`s and generic repositories
  (`IRepository<Order>`), Prisma, Knex, TypeORM, Mongoose, Sequelize and document stores
  (`db.users.find`). Table names come from `__tablename__`, `[Table]`, `ToTable`, `@Entity` and the
  like, or the class name. A match only after folding singular and plural (`user ~ users`) is a
  `guess`. Sites in tests and migrations are counted, not linked. Flows do not follow these.
- **rpc**: from a call on a gRPC or Thrift stub (`GreeterStub`, `Greeter.GreeterClient`,
  `getService('X')`) to the method of the same name on a class that implements the service
  (`GreeterServicer`, `Greeter.GreeterBase`, `@GrpcMethod`). When several servers implement it, the
  ones nearest the caller by directory are linked.
- **format** (TypeScript, Python, C#): from a function that builds keys of one shape to a function
  that takes them apart, where no call joins the two. The address is the shape
  (`dialogue/*/nodes/*/text`). A builder is a template with holes and two or more fixed parts
  between `/` or `:` separators, starting with a fixed part: a template literal, an f-string, an
  interpolated string, `"%s/x/%s" %`, `String.Format` or a chain of `+`. A hole that calls another
  builder (`${nodeKey(d, n)}/text`) takes that builder's shape. A reader is a regular expression over
  the shape (where a constant holding it is used), a `startsWith` test of a literal with two fixed
  parts, or code that compares a split key's pieces by position (`parts[2] === "nodes"`, through
  `slice`, aliases and destructuring). They are linked when two or more of the reader's fixed parts
  sit at the same places in the builder's shape and none disagrees; when the pieces come from a
  parameter and two shapes share a tail, the reader is taken to read the shape its own file builds,
  else the one most files build. Routes, file paths, git's `refs/...` and keys built in tests are
  left out. A `guess` unless three fixed parts agree. Like files and tables, flows do not follow
  these, and a pull request that edits the builder's template lists the readers as "must agree".

All five meet by a name or a shape written in code, so each link is `heuristic` at best. Each change brief
lists the channels a change crosses, with their address.

### Systems

A module is split into systems by Louvain community detection over calls, type use and inheritance
between its units: each top-level type, and each file for the functions that sit outside any type. A
module with fewer than 12 units holding code, or one that does not split cleanly (modularity under
0.3), is left whole. Each proposed system is named after its most connected unit (a type, or a file)
until something better is written with `annotate`, using the keys `name` and `responsibility`.

The grouping is deterministic. The names are not: they belong to the inferred layer.

### Annotations

`annotate` is the only write. An inferred annotation must list the node ids it is based on; the
store hashes the files behind them and marks the annotation stale when any of those files changes.
An intent annotation is the user's own statement and needs no evidence. Facts cannot be written.

### Flows

A flow is a walk of the call graph from one entry point or one test, depth-first, in source order,
listing each function once. Flows follow calls, calls through an interface into its implementations
(the registered one first), process launches, HTTP requests, messages to their handlers, remote calls,
and events whose handler was subscribed earlier in the same flow. They do not follow files or tables:
writing one does not run its reader. They stop at
8 calls deep or 300 steps.

A flow's id is `flow:<start>`. A start that is two kinds of start at once (an entry point that is also a test)
has a flow for each: the entry point's keeps that id, and the other's is `flow:<start>#test`.

Flows are static: they show what can run, not what did run. Per-test coverage will replace them with
observed paths where it is available.

SCIP indexers will replace these heuristics with exact references. That extractor and test coverage
are recorded as `not_analyzed` in `extractor_coverage`, so missing analysis is never mistaken for an
empty result.

### How well this holds up

Measured on three repositories, counting call sites whose target is inside the repository:

| Repository | Files | Index time | Call sites linked | Of those, by guess | Left open |
|---|---|---|---|---|---|
| A private project (C# and Python) | 59 | 1 s | 99% | 4% | 18 |
| Flask (Python) | 83 | 1 s | 91% | 9% | 101 |
| Polly (C#) | 801 | 7 s | 94% | 2% | 915 |

With the compiler pass on, the syntax links can be scored. On the private project the compiler confirmed 2,081
of them, removed 16 and added 38 it had missed. On Polly, where packages could not be restored and
only part of the code binds, it confirmed 4,844 and removed 1,286, nearly all extra overloads of
the right method.

Without that pass, "linked" is not "correct". In a random sample of 42 links across the
three, read against the source by hand, all 42 pointed at the right method; 4 of the 22 from Polly
pointed at the wrong overload of it. Polly's fluent API has up to twenty overloads per name, which
is the hard case for this approach. A call the indexer classes
as leaving the repository is not in these counts, and some of those are misses: a call on a value
whose type comes from outside (Flask's test client) cannot be followed back in.
