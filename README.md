# Leyline

Leyline maps a codebase into one SQLite graph that people and coding agents can both query.
Every record belongs to one of three layers:

- **fact**: extracted deterministically from the source, rebuilt on every index run
- **inferred**: written by an LLM, with evidence and a confidence
- **intent**: written by you, such as boundaries and rules

This version builds the fact layer and serves it. The inferred and intent layers have tables but
nothing writes to them yet.

## Use

```bash
pip install -e .
leyline index path/to/repo          # writes path/to/repo/.leyline/leyline.db
leyline --db path/to/repo/.leyline/leyline.db overview
leyline --db ... search "Simulation Step"
leyline --db ... expand "<node id>"
leyline --db ... serve              # MCP server over stdio
```

To use it from an MCP client, register the command `leyline --db <path> serve`.

## MCP tools

| Tool | Returns |
| --- | --- |
| `overview` | Repos, modules with sizes, module-to-module dependencies by edge kind, external packages, extractor status |
| `expand(node_id)` | One node in detail: contents, dependencies, dependents, callers and callees |
| `search(text, kind?)` | Nodes matching a name, qualified name or path |
| `neighbors(node_id, direction?, kinds?)` | Raw edges around a node |
| `source(node_id)` | The node's source text |

## What is indexed

Languages: C# and Python, through tree-sitter.

| Record | Precision | Notes |
| --- | --- | --- |
| Modules, files, types, callables, fields, entry points | exact | A module is a directory with a project file, or a source file's own directory |
| `contains`, `has_field`, `exposes` | exact | |
| `imports`, `depends_on` | exact | From `using` and `import` statements and from `.csproj` references |
| `extends`, `implements`, `uses_type`, `instantiates` | heuristic | Type names resolved by name, limited to what the project can reference |
| `calls` | heuristic | See below |

### How calls are resolved

Without a compiler, a call is linked only when its receiver's type can be worked out from
declarations in the same file: fields, parameters, typed locals, `new` expressions, collection
element types and static type names. Three outcomes are counted per run:

- **resolved**: linked to a callable in the workspace
- **external**: the receiver's type is outside the workspace, or nothing in the workspace has that name
- **unresolved**: the name exists in the workspace but the receiver's type is unknown

A call on a receiver of unknown type is linked by name only when exactly one declaration of that
name is visible and the name was never seen on an outside type. The run reports how many links were
made this way (`calls_by_unique_name`).

SCIP indexers will replace these heuristics with exact references. That extractor, test coverage,
and every `communicates` channel are recorded as `not_analyzed` in `extractor_coverage`, so missing
analysis is never mistaken for an empty result.

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
