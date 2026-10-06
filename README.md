# Leyline

Leyline maps a codebase into one SQLite graph that people and coding agents can both query.
Every record belongs to one of three layers:

- **fact**: extracted deterministically from the source, rebuilt on every index run
- **inferred**: written by an LLM, with evidence and a confidence
- **intent**: written by you, such as boundaries and rules

This version builds the fact layer, proposes systems by clustering, and accepts inferred and intent
annotations through `annotate`. Rules and change proposals have tables but no code yet.

## Use

```bash
pip install -e .
leyline index path/to/repo          # writes path/to/repo/.leyline/leyline.db
leyline --db path/to/repo/.leyline/leyline.db overview
leyline --db ... search "Simulation Step"
leyline --db ... expand "<node id>"
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

Without a compiler, a call is linked only when its receiver's type can be worked out from
declarations in the same file: fields, parameters, typed locals, `new` expressions, collection
element types and static type names. Three outcomes are counted per run:

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

Dependency injection, HTTP, queues, databases and shared files are recorded as `not_analyzed`.

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
