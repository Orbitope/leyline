# Leyline

## Start here

Leyline lets a person design a change to a codebase while a coding agent writes it. Before any code is
written it shows, on one page, what code will be written, what it will affect and how you will know it
was done; afterwards it says on the same page whether the change was done as agreed. Everything on that
page comes from the code and the test results, not from the agent's account of its own work.

```bash
pip install -e .        # Python 3.10 or later; installs the `leyline` command
```

Three commands, in this order. Each one ends with a `Next:` line saying what to do after it.

| Command | When | What you read |
| --- | --- | --- |
| `leyline map [repo ...]` | Once, before the first change | Ten lines on what was found, and a map page to browse |
| `leyline plan <change>` | After the change is written as a spec, and after every edit to it | The page, `leyline.md`, and what is still needed before implementation |
| `leyline check <change> --tests FILE` | After the code is written | The verdict, added to the same page |

A change is an [OpenSpec](https://openspec.dev) folder, `openspec/changes/<id>/`: a proposal, the
requirements with scenarios, and a task list. You describe the change in words and your agent writes the
folder; `skills/leyline-spec/SKILL.md` tells it how. `<change>` is the folder or just its id.

### A worked example

The repository `tests/fixture2` has a small Python package with an `Engine` class and its tests.

```bash
cp -r tests/fixture2 /tmp/engine-repo && cd /tmp/engine-repo
leyline map .
```
```
Mapped engine-repo in 1.2 s: 13 files, 323 lines, 4 modules.
Found 31 types, 64 functions, 5 tests and 1 entry point (where a program starts).
Modules: cs/Mod (7 files), py/src/pkg (2 files), py/tests (2 files), py/web (2 files)
Design patterns found: builder, composite, decorator, factory 2, singleton, strategy, template method
Store: .leyline/leyline.db
Map page: .leyline/map.html (open it in a browser)
Next: write the change you want as an OpenSpec folder, openspec/changes/<id>/ (ask your agent; ...
```

You tell your agent: "engine names should come back in upper case, and add a `shout`". It writes
`openspec/changes/loud-engine/` with this `tasks.md`

```
- [ ] 1.1 Change `Engine.start` to return the name in upper case
- [ ] 1.2 Add `Engine.shout`, the name with an exclamation mark
- [ ] 1.3 Add the test "Shout"
```

and two scenarios, `Start` and `Shout`, in `specs/engine/spec.md`. Then you plan it, passing the tests'
output from before any code changes so that `check` can tell a test the change breaks from one that
already failed (any runner that prints one PASS or FAIL line per test works; `pytest -rA` does):

```bash
(cd py && PYTHONPATH=src pytest -rA tests) | leyline plan loud-engine --tests -
```
```
# Loud engine
...
**State: ready to implement.** It has not been reviewed.

Names are hard to read in logs. The plan has 3 tasks: it changes Engine.start and adds Engine.shout.
Nothing else must be edited with it; 3 places in 2 modules run into the changed code and may behave
differently; 3 existing tests already run through it. It is done when 2 scenarios pass: 1 already has a
test, 1 needs a test written.
...
**Shares a caller or a field with the change, and no task names it.** Each line is either right to
leave alone or a missing task:
- Engine.name (used by Engine.start) is also used by Engine.child
...
Next: have the plan reviewed before code is written (ask your agent to run the
leyline-adversarial-review skill), then run `leyline plan loud-engine` again. Or, if you accept the plan
as it is, implement it.
```

The agent implements the tasks. Then:

```bash
(cd py && PYTHONPATH=src pytest -rA tests) | leyline check loud-engine --tests -
```
```
## 4. Was it done as agreed
**Yes.** Every task is done, every scenario is proven, and nothing outside the spec changed.

| Task | Result | Missing |
| 1.1 Change `Engine.start` to return the name in upper case | done |  |
...
| Scenario | Result | Evidence |
| Start | passes | its test reaches the changed code on the map |
| Shout | passes | its test reaches the changed code on the map |

Tests: 5 of 5 passed before, 6 of 6 after.
Next: nothing left to check; the change was done as agreed. Review the diff and commit it.
```

`check` exits 0 only when the change was done as agreed. Had the agent also edited `Engine.child`, the
verdict would read "Not yet: 1 edit is outside the spec", and `Next:` would ask you to add a task for it or
undo it.

### What you read

`openspec/changes/<id>/leyline.md` is the one page for a change. It opens with its state (not ready,
ready, done as agreed or not) and a paragraph in plain words, then:

1. **What code will be written**: each task and the code it touches.
2. **What it will affect**: what must be edited with it, what runs into it, the risks, and code that
   shares a caller or a field with the change that no task names.
3. **How you will know it was done**: each scenario and the test that proves it.
4. **Review findings** and **Before implementation**: what blocks implementation, and what is left to
   decide. Only you resolve a finding.
5. **Was it done as agreed**, added by `check`.

### With an agent

Register `leyline --db <repo>/.leyline/leyline.db serve` as an MCP server. The agent's path is the same
three tools, `map`, `plan` and `check`, each returning `next`; the skills in `skills/` say how to write
the spec (`leyline-spec`), review it (`leyline-adversarial-review`), assess any change
(`leyline-change-impact`) and explain the code (`leyline-tour`).

Everything below is the detail: the other commands, how the map is built, and how far to trust it.
`leyline --help` lists the other commands under "advanced".

## What Leyline is

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
leyline map path/to/repo            # writes path/to/repo/.leyline/leyline.db and .leyline/map.html
leyline index path/to/repo          # the same index, printing the full statistics instead
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

### Several repositories

```bash
leyline map flask/ werkzeug/        # one workspace; writes ./.leyline/leyline.db
leyline --db .leyline/leyline.db index werkzeug/   # re-indexes flask too
```

Repositories named together are indexed in one run, so a name in one resolves to its declaration in
the other: `import werkzeug` in Flask reaches Werkzeug's source (a package under `src/` by its package
name), a Flask class extends Werkzeug's, and a Werkzeug method that calls `self.open()` dispatches
into Flask's override. Ids keep their own repo's prefix, so a store indexed one repository at a time
reads the same. The store remembers its members: indexing any one of them later indexes all of them
again, which keeps the links between them. A member whose directory has gone is left as stored.

`overview` lists the links between repositories; the `cross_repo` tool adds the functions most called
across and the flows that cross and come back. An edge between repositories carries `to_repo`;
`impact` and a change assessment count what they reach per repo and flag a change that reaches into
another one. Not yet: the C# compiler pass (SCIP indexes are matched to their repository by the
directory they were made in), and values handed across that the syntax pass cannot follow, such as
the WSGI app a test client calls.

## How the pieces fit

Everything lives in one file: `.leyline/leyline.db` inside the repository you indexed. Three things
read and write it, and none of them runs unless you start it.

| Piece | Started by | Reads | Writes |
| --- | --- | --- | --- |
| `leyline map` or `index` | You, a git hook or CI; `plan` and `check` when the code changed | The working tree | Facts, flows and system proposals. Replaces the previous facts. |
| `leyline serve` (MCP) | Your coding agent, when it starts | The store | Annotations, change proposals and saved views |
| `leyline view` or `export` | You | The store | Nothing |

Annotations, proposals and views are not facts, so re-indexing keeps them. An annotation is flagged
stale when the code behind its evidence changes.

### The spec loop

The intended way to change a mapped codebase: the person drives the design, an agent writes the
code, and one page says what will be written, what it affects and how you will know it was done.

A change is an [OpenSpec](https://openspec.dev) change folder. Leyline reads it and writes
`leyline.md` back into it. `leyline plan` and `leyline check` (see Start here) run the loop; these are
its steps one at a time, for when you want one alone:

```
leyline spec brief  openspec/changes/<id>     # before any code: the one-page brief, and the gaps in the spec
leyline spec facts  openspec/changes/<id> --reviewer logic   # what the graph says, as questions for reviewers
leyline spec verify openspec/changes/<id> --before before --after after   # after: was it done as agreed
```

`plan` is `brief` after re-indexing changed code, and records the test output you pass it under
`before:spec-<id>`. `check` re-indexes, records its test output under `after:spec-<id>`, and runs `verify`
with both. Before re-indexing, both compare each source file's hash with the store, so an unchanged
repository is not indexed again.

The brief ties each task to code and each scenario to a test, by three conventions and no markup:

- Code named in backticks in `tasks.md` is looked up on the map (`` `Vehicle.Speed` ``). A name not on
  the map is new code; `` `Owner.NewName` `` says where it goes.
- A task that starts with add, remove, rename or "change the signature" is read that way. Anything
  else is a change in behavior.
- A scenario is proven by a test with the same name.

It then lists what the change reaches that no task covers, the patterns it sits in, the gaps that
block implementation, and the code that uses the same things and that no task names: other callers of
a changed function, other users of a field it uses, and users of an existing member that a new one is
named like (a new `EmergencyQueues` beside `EntryQueues`). Each of those lines is either right to
leave alone or a missing task. Reviewers (the `leyline-adversarial-review` skill, one run
for logic and one for performance) file findings with node ids as evidence; only the person
resolves them (`leyline spec resolve <finding> accepted|rejected|deferred "why"`). A reviewer that passes
its kind (`--reviewer`, or `reviewer` on the `spec_review_facts` tool) is recorded as having run, so the page
can say a review found nothing rather than that none ran.

After implementation and a re-index, `verify` (inside `check`) marks each task from the graph diff and each scenario
from its test's recorded result, lists edits outside the spec, new links between modules and rules
newly broken, and appends the result to `leyline.md`. It exits 0 only when the change was done as
agreed. A new function that only code named in the spec calls is listed as a helper, not as an edit
outside the spec.

A spec can change part-way. Once the code has moved on, `brief` keeps the picture of the code from
the first brief, so `verify` still compares with the code as it was; `--new-baseline` starts over.

`skills/leyline-spec/SKILL.md` tells an agent how to write the folder and run the loop.

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
- Properties count as fields. Enum members and events do not. In Python, reading an attribute that a
  getter computes (`@property`, or a decorator that is a descriptor class, such as Werkzeug's
  `cached_property`) is also a call to the getter; on `self`, so is a subclass's getter of that name.
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
| `map(paths?)` | Step 1: index the code (or map again what the store holds); counts, the map page, `next` |
| `plan(change, test_output?, test_results?)` | Step 2: the one page for a change, `status.blocking`, `next`; records the tests from before |
| `check(change, test_output?, test_results?)` | Step 3: re-index, record the tests from after, the verdict and `next` |
| `overview` | Repos, modules with sizes, module-to-module dependencies by edge kind, external packages, extractor status |
| `cross_repo` | In a workspace of several repositories: links between them, functions most called across, flows that cross and come back |
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
| `spec_brief(change_dir)`, `spec_verify(change_dir, before_run?, after_run?)` | The steps inside `plan` and `check`, one at a time |
| `spec_review_facts(change_dir, reviewer?)`, `spec_finding(...)`, `spec_findings(change_id)`, `spec_resolve(...)` | Adversarial review of a spec |
| `shared_state(scope?)` | Fields assigned from outside the type that declares them |
| `coverage(node_id?, flow_id?, import_path?)` | Measured coverage: what ran, set against the static paths |
| `patterns(pattern?, node_id?)` | Design patterns found by shape, with roles, rationale and confidence |
| `label_pattern(pattern, roles, rationale, confidence?)` | Record a pattern the matchers missed |
| `tours()`, `tour(tour_id)` | List tours, or read one stop by stop |
| `save_tour(title, stops, audience?)` | Save a tour written for the user |
| `views()`, `view(view_id)` | List saved views, or read one in full |

## What is indexed

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
| C# (Signal) | Roslyn | 99.7% / 97% | 98% / 83% |

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
