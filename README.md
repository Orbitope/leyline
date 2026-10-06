# Leyline

Leyline maps a codebase into one SQLite graph that people and coding agents can both query.
Every record belongs to one of three layers:

- **fact**: extracted deterministically from the source, rebuilt on every index run
- **inferred**: written by an LLM, with evidence and a confidence
- **intent**: written by you, such as boundaries and rules

This version builds the fact layer for C# and Python, proposes systems by clustering, labels design
patterns by their shape, generates an orientation tour, assesses a described change before it is made,
reviews it after, and checks architecture rules.

## Use

```bash
pip install -e .
leyline index path/to/repo          # writes path/to/repo/.leyline/leyline.db
leyline --db ... coverage .coverage # import measured test coverage
leyline --db path/to/repo/.leyline/leyline.db overview
leyline --db ... search "Simulation Step"
leyline --db ... expand "<node id>"
leyline --db ... tour               # a guided walk through the repository
leyline --db ... patterns           # design patterns found by their shape
leyline --db ... state              # fields assigned from outside their own type
leyline --db ... serve              # MCP server over stdio
leyline --db ... view               # the map, served on http://127.0.0.1:8765
leyline --db ... export -o map.html # the map as one self-contained page
```

To use it from an MCP client, register the command `leyline --db <path> serve`.

## How the pieces fit

Everything lives in one file: `.leyline/leyline.db` inside the repository you indexed. Three things
read and write it, and none of them runs unless you start it.

| Piece | Started by | Reads | Writes |
| --- | --- | --- | --- |
| `leyline index` | You, a git hook or CI | The working tree | Facts, flows and system proposals. Replaces the previous facts. |
| `leyline serve` (MCP) | Your coding agent, when it starts | The store | Annotations, change proposals and saved views |
| `leyline view` or `export` | You | The store | Nothing |

Annotations, proposals and views are not facts, so re-indexing keeps them. An annotation is flagged
stale when the code behind its evidence changes.

### Assessing a change

1. You describe a change to an agent that has the Leyline MCP server connected.
2. The agent finds the nodes the description refers to and calls `propose_change`.
3. Leyline walks callers, interface links, channels and flows from those nodes and stores the result
   as a draft proposal with a saved view.
4. You refresh the map and open the Views tab: the change, what must be edited with it, what it
   reaches, the tests to run and the risks.

`skills/leyline-change-impact/SKILL.md` tells an agent how to do steps 2 and 3 well. The same
`save_view` tool lets an agent save any other slice of the code as a view.

### Reviewing a change after it is made

`propose_change` copies the store to `.leyline/snapshots/<change id>.db` before it returns. Once the
change is implemented:

```
<run the tests> > before.txt        # before the change, on the old code
leyline record-tests before before.txt
... implement ...
<run the tests> > after.txt
leyline index .
leyline record-tests after after.txt
leyline review <change id> --before before --after after
```

`review` compares the snapshot with the graph as it is now and reports:

- which predicted edits were made, which edits were not in the proposal, and which predicted edits
  did not happen. A method whose parameters changed gets a new id; it is paired with the old one
  by owner and name, so it counts as one edited method.
- links between modules that are new or gone, and the flows whose path changed
- rules that fail now and held before
- tests that fail now and passed before

It saves the result as a view, which the map shows under Views as "Review: ...".
`record-tests` reads one `PASS name` or `FAIL name: message` line per test; an agent can pass
results in any other format through the `record_test_run` tool.

### Rules

A rule is a constraint on the graph, checked with no inference: `forbid` (nothing in A may use
anything in B), `no_cycle` (between modules or systems), `must_be_tested` (every function in A is on
some test's path). Selectors are `module:Name`, `system:Name`, `external:Name`, `path:prefix`,
`id:prefix` and `*`. A rule an agent adds is stored as suggested until you confirm it with
`leyline rules --confirm ID`. `leyline rules` checks all of them and exits 1 when a confirmed
error-level rule fails, so it can run in CI. A `forbid` rule on an external package sees imports
(`using`, `import`), not fully qualified names used inline.

### Exact references

The syntax resolvers below work without a compiler and are sometimes wrong. Where a compiler's view
is available, `leyline index` lets it overrule them (`--exact auto`, the default; `--exact off` to
skip):

- **C#**: if `dotnet` is on the PATH, a small program built against the Roslyn that ships inside
  the .NET SDK binds every module and reports each call and field access it resolved. It needs no
  package restore. A module whose packages are missing still binds whatever refers to source in the
  repository; the rest stays as the syntax pass left it. The first run builds the program into
  `~/.cache/leyline` (about ten seconds).
- **Other languages**: a SCIP index, from `--scip FILE` or `index.scip` in the repository root.
  Tested with scip-python. Run the indexer in an environment where the package resolves to the
  source tree (an editable install), or references through the installed copy are lost.

A link the compiler confirms is stored as `exact`. A link it contradicts is removed, but only where
it bound every call of that name in the function (C#) or bound that name to something else on the
same line (SCIP, which does not say what it failed to bind). Links it found and the syntax pass
missed are added. `overview` reports the counts under `exact:roslyn` and `exact:scip`.

### Measured coverage

Flows are static: what a test can reach. `leyline coverage FILE` imports what did run:

- coverage.py's data file. With `pytest --cov=<package> --cov-context=test`, each function is tied
  to the tests that ran it.
- Cobertura XML (coverlet, `coverage xml`). A function ran or did not; no per-test detail.

With coverage imported, a test's flow dims the steps that did not run and lists what ran without
being on its path (reached through a link the map does not have, such as a framework calling back
into the code). A change assessment adds the tests that were measured running the changed code, and
`must_be_tested` counts measured functions as tested. Only a function's body counts: the `def` line
runs when the file loads. An import outlives a re-index; `leyline coverage` flags one taken at
another commit.

### Field reads and writes

Each function is linked to the fields it reads and the fields it assigns (`reads` and `writes` edges,
with a count and the first line). `expand` on a field lists its readers and writers; on a function,
the fields it touches; on a type, each field with how many functions set and read it and which
other types set it. `leyline state` (the `shared_state` tool) ranks the fields assigned from outside
the type that declares them: the mutable state with no single owner.

- An assignment is `x.f = v`, `x.f += v`, `x.f++`, `out`/`ref x.f` and `x.f[i] = v`. A value set
  while creating an object (`new Foo { f = 1 }`) is recorded as construction and left out of the
  shared-state ranking, as are constructors, subclasses and test code.
- A change made by calling a method on the field (`list.Add(x)`) is a read of the field, not an
  assignment: the map cannot tell a mutating method from a query.
- Properties count as fields. Enum members and events do not.
- In C#, a field on a receiver of unknown type is linked by name only when one field has that name,
  and marked as a guess. In Python there is no such guess: attribute names repeat too often.
- A change assessment whose target is a field marks every reader and writer, and `review` reports
  field access that is new.

### Design patterns

Every index run looks for nine shapes in the graph and labels what it finds, with the nodes that
play each role, a sentence saying why, and a confidence:

| Pattern | The shape that is matched |
|---|---|
| strategy | an abstraction with two or more implementations, held in a field by another type that calls it |
| decorator | a type that implements an abstraction and holds one more of it |
| composite | a type that implements an abstraction and holds a collection of it |
| template method | a concrete method on a base type that calls methods its subclasses override |
| observer | an event with a raiser and subscribers in other code |
| factory | a function that creates two or more types sharing a supertype (higher confidence when it returns that supertype) |
| builder | a type with chaining methods that return itself and a `Build` or `Create` that returns something else |
| singleton | a static field of the type's own type and no public constructor |
| process boundary | one program launching another and talking over its pipes |

A label says the code has the shape. It does not say the author meant the pattern, and a pattern
built another way is not found. Labels in tests, samples and benchmarks are kept apart. An agent that
has read the code can add a label the matchers missed with `label_pattern`; it goes stale when the
code behind it changes.

### Tours

A tour is an ordered list of stops, each pointing at something on the map (a module, a function, a
flow, a pattern, a saved view) with a few sentences on what to notice. `leyline index` writes an
orientation tour from the graph alone: what the repository is, where it starts, the modules from the
most depended-on outward, the main abstractions and boundaries, one test path worth tracing, how it
is tested, and what the map cannot see. Every sentence in it is a count or a name the store can
back. An agent can write further tours with `save_tour`; `skills/leyline-tour/SKILL.md` says how.

## The map

`view` and `export` open the same page. It has up to four zoom levels:

1. **Modules.** One box per module, with an arrow for each dependency and a count of the links behind it.
2. **Inside a module.** One box per system where the module was split, otherwise one per type or file.
3. **Inside a system.** One box per type, or per file where functions sit outside any type.
4. **Inside a type or file.** One box per function, with the outside callers and callees around it.

Select a box or an arrow to see detail in the side panel: members, callers, callees, the links that
make up an arrow, and source text. Double-click a box to open it. Solid arrows are exact, dashed
arrows were worked out from syntax, dotted arrows are guesses, and blue dash-dot arrows are events and
process launches.

The Matrix tab shows the same level as a dependency matrix, ordered so that cycles appear above the
diagonal. The Flows tab steps through one flow at a time, with the source of the selected step beside it.
The Views tab shows saved views: for a proposed change, the marked code colored by role beside the
impact report.

The exported page embeds the graph and the source text of every indexed file, so share it with the
same care as the repository. Pass `--no-sources` to leave source text out.

## MCP tools

| Tool | Returns |
| --- | --- |
| `overview` | Repos, modules with sizes, module-to-module dependencies by edge kind, external packages, extractor status |
| `expand(node_id)` | One node in detail: contents, dependencies, dependents, callers and callees |
| `search(text, kind?)` | Nodes matching a name, qualified name or path |
| `neighbors(node_id, direction?, kinds?)` | Raw edges around a node |
| `source(node_id)` | The node's source text |
| `flows(kind?, through?)` | Flows walked from each entry point and test; `through` keeps flows that pass a node |
| `flow(flow_id)` | One flow step by step, in source order, with call depth |
| `trace(from_id, to_id)` | The shortest chain of calls and channels between two functions |
| `impact(node_id)` | What can reach a node: callers by module and the flows through it |
| `annotate(node_id, key, value, evidence, confidence, layer)` | Write an inferred or intent statement about a node |
| `propose_change(intent, targets, title?)` | Assess a change before it is made and save its blast-radius view |
| `save_view(title, narrative, marks, legend?)` | Save any set of marked nodes as a view |
| `review_change(change_id, before_run?, after_run?)` | Compare an implemented change with its proposal and save a review view |
| `record_test_run(run, results)` | Store one test run under a label |
| `add_rule(kind, selector_from, selector_to?, ...)` | Add an architecture rule, suggested unless the user stated it |
| `check_rules()` | Evaluate every rule against the graph |
| `shared_state(scope?)` | Fields assigned from outside the type that declares them |
| `coverage(node_id?, flow_id?, import_path?)` | Measured coverage: what ran, set against the static paths |
| `patterns(pattern?, node_id?)` | Design patterns found by shape, with roles, rationale and confidence |
| `label_pattern(pattern, roles, rationale, confidence?)` | Record a pattern the matchers missed |
| `tours()`, `tour(tour_id)` | List tours, or read one stop by stop |
| `save_tour(title, stops, audience?)` | Save a tour written for the user |
| `views()`, `view(view_id)` | List saved views, or read one in full |

## What is indexed

Languages: C# and Python, through tree-sitter.

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
under a source root are imported by their own name, names re-exported through `__init__.py` are
followed, and a test parameter filled by a pytest fixture takes the fixture's return type. Three
outcomes are counted per run:

- **resolved**: linked to a callable in the workspace
- **external**: the receiver's type is outside the workspace, or nothing in the workspace has that name
- **unresolved**: the name exists in the workspace but the receiver's type is unknown

A call on a receiver of unknown type is linked by name only when exactly one declaration of that
name is visible and the name was never seen on an outside type. Those links are stored with
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
  that matches; if several match, it is counted as ambiguous and left unlinked.
- **file**: from a function that writes a file to one that reads it, when the path fragments written
  in the two agree: the same file name, or the same directory and the same extension
  (`policies/*.bin`). These are always a `guess`, since paths are usually built at run time. Flows
  do not follow them: writing a file does not run its reader.

Dependency injection, queues, RPC and databases are recorded as `not_analyzed`.

### Systems

A module with at least 12 types is split into systems by Louvain community detection over calls,
type use and inheritance between its types. A module that does not split cleanly (modularity under
0.3) is left whole. Each proposed system is named after its most connected type until something
better is written with `annotate`, using the keys `name` and `responsibility`.

The grouping is deterministic. The names are not: they belong to the inferred layer.

### Annotations

`annotate` is the only write. An inferred annotation must list the node ids it is based on; the
store hashes the files behind them and marks the annotation stale when any of those files changes.
An intent annotation is the user's own statement and needs no evidence. Facts cannot be written.

### Flows

A flow is a walk of the call graph from one entry point or one test, depth-first, in source order,
listing each function once. Flows follow calls, calls through an interface into its implementations,
process launches, and events whose handler was subscribed earlier in the same flow. They stop at
8 calls deep or 300 steps.

Flows are static: they show what can run, not what did run. Per-test coverage will replace them with
observed paths where it is available.

SCIP indexers will replace these heuristics with exact references. That extractor and test coverage
are recorded as `not_analyzed` in `extractor_coverage`, so missing analysis is never mistaken for an
empty result.

### How well this holds up

Measured on three repositories, counting call sites whose target is inside the repository:

| Repository | Files | Index time | Call sites linked | Of those, by guess | Left open |
|---|---|---|---|---|---|
| Signal (C# and Python) | 59 | 1 s | 99% | 4% | 18 |
| Flask (Python) | 83 | 1 s | 91% | 9% | 101 |
| Polly (C#) | 801 | 7 s | 94% | 2% | 915 |

With the compiler pass on, the syntax links can be scored. On Signal the compiler confirmed 2,081
of them, removed 16 and added 38 it had missed. On Polly, where packages could not be restored and
only part of the code binds, it confirmed 4,844 and removed 1,286, nearly all extra overloads of
the right method.

Without that pass, "linked" is not "correct". In a random sample of 42 links across the
three, read against the source by hand, all 42 pointed at the right method; 4 of the 22 from Polly
pointed at the wrong overload of it. Polly's fluent API has up to twenty overloads per name, which
is the hard case for this approach. A call the indexer classes
as leaving the repository is not in these counts, and some of those are misses: a call on a value
whose type comes from outside (Flask's test client) cannot be followed back in.

## Identity

Node ids are stable across file moves within a project:

- C#: `repo:csharp:Project::Namespace.Type.Member(ParamTypes)`. The project is part of the id
  because two projects may declare the same type name.
- Python: `repo:python:package.module.Class.method`
- Files and modules: `repo:file:path` and `repo:module:path`

## Tests

```bash
pytest
```

## License

Copyright (C) 2026 Matthew Burke.

Leyline is free software, licensed under the GNU General Public License, version 3.
See [LICENSE](LICENSE) for the full text. It comes with no warranty.
