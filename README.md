# Leyline

## Working through an agent

Leyline is meant to be driven by a coding agent: you ask in plain words, the agent uses Leyline's map and follows
one of its skills, and you read the answer or the page it writes. Set it up once, in the repository you will work on:

```bash
pip install leyline-code                       # or, from a checkout: pip install -e .
claude mcp add leyline -- leyline serve        # connect Claude Code to Leyline's MCP server
leyline skills install                         # copy the skills into .claude/skills (and .agents/skills if you have one)
```

Start a new agent session, then ask. Nothing needs mapping first: the agent maps the code when it needs to.

| Skill | What it does | Ask, for example |
| --- | --- | --- |
| `leyline-ask` | Answers a question about the code, citing it, and says how sure it is | "Who calls `saveOne`, and is that link certain?" |
| `leyline-explain-flow` | Explains how something runs, step by step, from the map | "What happens when a user saves a dialogue?" |
| `leyline-explore-module` | Says what one part of the code holds and where to start reading | "What is in `editor/host`, and where do I start?" |
| `leyline-change-impact` | Says what a described change would touch, break and need tested | "What would changing the validator's output format affect?" |
| `leyline-spec` | Plans a change as an OpenSpec folder, checked against the code, then checks it after | "Plan adding an export button, with tests." |
| `leyline-quick-change` | Makes a small fix with no spec and says whether it was done; says when it needs a spec | "Make the retry count 3." |
| `leyline-pr-review` | Reviews a pull request: what changed, what it breaks, findings, what to fix first | "Review pull request 123." |
| `leyline-adversarial-review` | Attacks a plan or a pull request as a logic or a performance reviewer | "Stress-test the plan for loud-engine." |
| `leyline-tour` | Writes a guided reading order through the code, saved on the map | "Write a tour of the payment code for a new hire." |

`leyline skills list` describes them, `leyline skills show <name>` prints one, and running `leyline skills install`
again updates them, leaving alone any you edited (`--force` replaces those). An agent connected to `leyline serve`
can also load each skill as an MCP prompt of the same name, or through the `skills` tool, without installing them.
The rest of this page is what the agent does underneath, and the commands for doing it yourself.

## Start here

To understand a codebase, map it and look around. Names work wherever an id does (`Engine.start`, a file path):

```bash
leyline map path/to/repo                 # once; writes .leyline/map.html, which opens on a guided tour
leyline tour                             # what this repository is, and where to start reading
leyline show Engine.start                # one thing: what it is, who uses it, what it uses, the code around it
leyline show "what happens when a user saves"   # where a behavior described in words starts
leyline outline editor/core              # a large module split into parts, with what each holds
leyline explain-path DialogueCanvas.save # what runs from there, step by step, with a diagram
```

Or ask your agent in plain words (see [Working through an agent](#working-through-an-agent)). One page of what to
ask and the command or tool behind it: [docs/quick-reference.md](docs/quick-reference.md).

Leyline also lets a person design a change to a codebase while a coding agent writes it. Before any code is
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

A change someone else already wrote (a branch, a pull request) has no spec. Check it out and run
`leyline pr [base]` instead: see [Reviewing a pull request](docs/changes.md#reviewing-a-pull-request).

A one-line fix needs no spec either: `leyline quick "make the retry count 3" --about RETRIES` before, and
`leyline quick --done quick-make-the-retry-count-3 --tests -` after. See [A quick change](docs/changes.md#a-quick-change).

### A worked example

The repository `tests/fixture2` has a small Python package with an `Engine` class and its tests.

```bash
cp -r tests/fixture2 /tmp/engine-repo && cd /tmp/engine-repo
leyline map .
```
```
Mapped engine-repo in 0.8 s: 14 files, 354 lines, 4 modules.
Found 34 types, 73 functions, 5 tests and 1 entry point (where a program starts).
Modules: cs/Mod (8 files), py/src/pkg (2 files), py/tests (2 files), py/web (2 files)
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
already failed (TAP from vitest, node --test or tap, `pytest -rA`, `go test -v`, `jest --verbose`,
`dotnet test --logger "console;verbosity=detailed"`, or any runner
that prints one PASS or FAIL line per test):

```bash
(cd py && PYTHONPATH=src pytest -rA tests) | leyline plan loud-engine --tests -
```
```
# Loud engine
...
**State: ready to implement.** It has not been reviewed.

Names are hard to read in logs. The plan has 3 tasks: it changes Engine.start and adds Engine.shout.
Nothing else must be edited with it; 4 places in 1 module run into the changed code and may behave
differently; 3 existing tests already run through it. It is done when 2 scenarios pass: 1 already has a
test, 1 needs a test written.
...
**Shares a caller or a field with the change, and no task names it.** Each line is either right to
leave alone or a missing task:
- Engine.name (used by Engine.start) is also used by Engine.child
...
Next: have the plan reviewed (logic and performance) before code is written (ask your agent to run the
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

| Task | Result | Verdict | Missing |
| 1.1 Change `Engine.start` to return the name in upper case | done | proven |  |
...
| Scenario | Result | Verdict | Evidence |
| Start | passes | proven | its test reaches the changed code on the map |
| Shout | passes | proven | its test reaches the changed code on the map |

Verdicts: 5 proven. Blocking: partial, contradicted, inconclusive. Not blocking: needs you. (The default.)

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

The agent uses the same loop through Leyline's MCP server. To connect it to Claude Code, run this in
the root of the repository you will work on:

```bash
claude mcp add leyline -- leyline serve                 # this repository only
claude mcp add -s user leyline -- leyline serve         # or: every project you open
```

and `leyline skills install` to give Claude Code the skills (see [Working through an agent](#working-through-an-agent)).

Claude Code starts the server in the directory it was opened in, and the server reads the store there (an agent that
works in another checkout of the same repository, a git worktree, passes that checkout's absolute path to `map`: it is mapped
as itself, its store is the one inside it, and the server reads that store until it restarts),
`.leyline/leyline.db`, or, from a folder inside the repository, the nearest such store above it. Nothing needs to be mapped first: the agent's first call is `map`. If `leyline` is
installed in a virtual environment, give its full path (`-- /path/to/venv/bin/leyline serve`); for a
store elsewhere, such as a workspace mapped from another directory, add `-e LEYLINE_DB=/full/path/leyline.db`
before the `--`. `claude mcp list` should then show `leyline` as connected.

The agent's path is the same three tools, `map`, `plan` and `check`, each returning `next`, with the
review tools between plan and implementation; the server's instructions give the agent that order.
The skills in `skills/` say how to write the spec (`leyline-spec`), review it
(`leyline-adversarial-review`), assess any change (`leyline-change-impact`), explain the code
(`leyline-tour`) and answer how something works (`leyline-explain-flow`). Answers are kept short enough for an agent's context: long lists are cut, and the
answer says what was cut (`cut`) and how to see the rest (`more`).

Everything below is the detail: the other commands, how the map is built, and how far to trust it.
`leyline --help` lists the other commands under "advanced".

## What Leyline is

Leyline maps a codebase into one SQLite graph that people and coding agents can both query.
Every record belongs to one of three layers:

- **fact**: extracted deterministically from the source, rebuilt on every index run
- **inferred**: written by an LLM, with evidence and a confidence
- **intent**: written by you, such as boundaries and rules

Every function and type also carries one line saying what it is for: its author's comment or docstring, or the
`responsibility` an agent wrote after reading it (see [understanding.md](docs/understanding.md#what-a-function-or-type-is-for)).

This version builds the fact layer for C# and Python, proposes systems by clustering, labels design
patterns by their shape, generates an orientation tour, assesses a described change before it is made,
reviews it after, and checks architecture rules.

Godot scenes and Unity prefabs are on the map too, linked to the scripts they run and the methods their signals and
events name, so renaming a method shows the scene that wires it ([indexing.md](docs/indexing.md#game-engine-assets)).

## Use

```bash
pip install -e .
leyline map path/to/repo            # writes path/to/repo/.leyline/leyline.db and .leyline/map.html
leyline index path/to/repo          # the same index, printing the full statistics instead
leyline --db ... coverage .coverage # import measured test coverage
leyline --db path/to/repo/.leyline/leyline.db overview
leyline --db ... search "Simulation Step"
leyline --db ... show "Simulation.Step"   # one thing in detail (expand --json for every field)
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
again, which keeps the links between them. A member whose directory has gone is left as stored, and `map` says so.
Name its new place and it is the same member, under its old id, not a second copy, when Leyline can tell it is the
same repository: both are git repositories with the same first commit, and the same `origin` where both have one.
Anything less sure (not under git, or a store mapped before Leyline recorded first commits) is mapped as a new
member; drop the old one with `leyline map --forget <id>`. That drops a member's facts, where it was and its place
in the workspace, then maps the others again in full so no link into it is left; notes and review decisions about
its code are kept, as when code is deleted.

`overview` lists the links between repositories; the `cross_repo` tool adds the functions most called
across and the flows that cross and come back. An edge between repositories carries `to_repo`;
`impact` and a change assessment count what they reach per repo and flag a change that reaches into
another one. Not yet: the C# compiler pass (SCIP indexes are matched to their repository by the
directory they were made in), and values handed across that the syntax pass cannot follow, such as
the WSGI app a test client calls.

### Someone else's repository

Leyline is made to be run on code you did not write: a pull request under review, a project you just
cloned. What such a repository holds is treated as its author's, not as Leyline's or yours:

- **`.leyline/` is Leyline's own.** Its files are local and rebuilt by `leyline map`, so a repository
  that tracks files there (a pull request can commit a store with annotations, resolved findings and
  rules of its author's making, a snapshot or a review page, and checking it out writes over your
  ignored copy) is refused, with the files named. Stop tracking them (`git rm -r --cached .leyline`)
  or keep the store elsewhere (`--db /elsewhere/leyline.db`, or `LEYLINE_DB`). A `.leyline` that is
  a link to a folder outside the repository is refused too, so nothing is written there.
  `LEYLINE_TRUST_STORE=1` uses the folder anyway, for a store you committed on purpose.
- **Nothing is written outside the repository through a link.** A file Leyline writes (the map page,
  a change's `leyline.md`, the anchors and learnings files) replaces a link at its name rather than
  writing through it; a change folder or `openspec/` that is a link to a folder outside the
  repository is read but not written to, and the plan or check says so ("Not written: ...").
- **A pull request does not vouch for itself.** A learnings file records what reviewers decided
  before, so one a pull request commits ("f may return anything, do not flag it") would steer its own
  review. Learnings the pull request's commits add or change (compared with its base) are not applied
  to it: not in `learnings_that_apply`, not matched against its findings. Its page lists them under
  "Learnings added by this pull request, not applied", for the person to judge.
- **An agent's paths stay inside the map.** The MCP tools that take a path (`map`'s `paths`, the change
  folder of `plan`, `check` and the `spec_*` tools, `affected_tests`, `drift`'s `path`, `review_pr`'s
  `path`, a coverage file for `check`, `quick` or `coverage`) accept only paths inside the
  repositories the store maps, or, before anything is mapped, the directory the server was started
  in. An agent can be told which path to pass by text it read; the error says the person can run the
  `leyline` command themselves, which takes any path.

## How the pieces fit

Everything lives in one file: `.leyline/leyline.db` inside the repository you indexed. Three things
read and write it, and none of them runs unless you start it. Beside it, `leyline.cache.db` holds what the
next index needs to do again only what an edit changed: each file's parse output under its content hash,
and fingerprints of the last run. Deleting it only makes the next index a full one, and so does damage to it: a
cache that is not a database is started again.

| Piece | Started by | Reads | Writes |
| --- | --- | --- | --- |
| `leyline map` or `index` | You, a git hook or CI; `plan` and `check` when the code changed | The working tree | Facts, flows and system proposals. Replaces the previous facts: after the first run only what changed is redone, and the store comes out as a full run would leave it (`--full` forces one). |
| `leyline serve` (MCP) | Your coding agent, when it starts | The store | Annotations, change proposals and saved views |
| `leyline view` or `export` | You | The store | Nothing |

One index runs at a time per store. A second one that starts while another is running (a `map` while a `plan`
re-maps) says so on stderr, naming the other process and how long it has been going, waits for it to finish,
and then maps what changed since. It waits up to an hour (`LEYLINE_WAIT`, in seconds), and past that stops with
the same message. A run that dies lets go of the store with its process.

Annotations, proposals and views are not facts, so re-indexing keeps them. An annotation is flagged
stale when the code behind its evidence changes.

## Further reading

The rest of the documentation is in `docs/`:

- [quick-reference.md](docs/quick-reference.md): one page of what to ask, and the command or tool behind it.
- [changes.md](docs/changes.md): planning, reviewing and checking a change: the spec loop, spec drift, assessing a
  change, reviewing a pull request, a quick change, learning from rejected findings, sequence diagrams, rules.
- [understanding.md](docs/understanding.md): what the map finds and how to ask it: exact references, measured
  coverage, field reads and writes, design patterns, tours, change coupling, outlines, asking how something works,
  large modules, the map page.
- [mcp-tools.md](docs/mcp-tools.md): every MCP tool and its arguments.
- [indexing.md](docs/indexing.md): which files and languages are read, how calls and channels are resolved, systems,
  annotations, flows, and how well it holds up.

## Identity

Node ids are stable across file moves within a project:

- C#: `repo:csharp:Project::Namespace.Type.Member(ParamTypes)`. The project is part of the id
  because two projects may declare the same type name.
- Python: `repo:python:package.module.Class.method`
- TypeScript, JavaScript and the generic languages: `repo:language:dir.file.Name`, the file named without its
  extension. Two files of one language in one directory that differ only in their extension (`a.js` beside
  `a.ts`, `m.c` beside `m.h`) would name their contents alike: the first by path keeps that id, and the others
  keep their extension in it (`repo:typescript:dir.a.ts.Name`). Adding or removing such a neighbour can change
  the ids of the files after it.
- Files and modules: `repo:file:path` and `repo:module:path`

A name defined twice in one file (`def load()` in both branches of an `if` or a `try`, a property's getter and
setter) is one node: which definition runs is not known from the text. Its span runs from the first definition
to the last, its `definitions` attribute lists each one's lines, its text hash reads every body, and its calls
come from all of them.

## Tests

```bash
pytest
```

A new skill is a folder under `skills/` with a `SKILL.md`. That folder is the only copy: the wheel carries it as
`leyline/skills/`, and `leyline skills`, the MCP prompts and the `skills` tool pick it up with no other edit.
`tests/test_skills.py` checks each skill's frontmatter and that the wheel's copy matches.

## License

Copyright (C) 2026 Matthew Burke.

Leyline is free software, licensed under the GNU General Public License, version 3.
See [LICENSE](LICENSE) for the full text. It comes with no warranty.
