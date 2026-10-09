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

A change someone else already wrote (a branch, a pull request) has no spec. Check it out and run
`leyline pr [base]` instead: see [Reviewing a pull request](#reviewing-a-pull-request).

A one-line fix needs no spec either: `leyline quick "make the retry count 3" --about RETRIES` before, and
`leyline quick --done quick-make-the-retry-count-3 --tests -` after. See [A quick change](#a-quick-change).

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
already failed (TAP from vitest, node --test or tap, `pytest -rA`, `go test -v`, `jest --verbose`, or any runner
that prints one PASS or FAIL line per test):

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

Claude Code starts the server in the directory it was opened in, and the server reads the store there,
`.leyline/leyline.db`. Nothing needs to be mapped first: the agent's first call is `map`. If `leyline` is
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
read and write it, and none of them runs unless you start it. Beside it, `leyline.cache.db` holds what the
next index needs to do again only what an edit changed: each file's parse output under its content hash,
and fingerprints of the last run. Deleting it only makes the next index a full one.

| Piece | Started by | Reads | Writes |
| --- | --- | --- | --- |
| `leyline map` or `index` | You, a git hook or CI; `plan` and `check` when the code changed | The working tree | Facts, flows and system proposals. Replaces the previous facts: after the first run only what changed is redone, and the store comes out as a full run would leave it (`--full` forces one). |
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
  the map is new code: `` `Owner.NewName` `` says where a member goes, and `` `module.new_func` ``,
  `` `path/to/file.py: new_func` `` or `` `new_func` in `file.py` `` where a top-level function goes. A
  path that is on the map counts with or without backticks. Other words in backticks (an issue code, a
  value, a doc file) are noted, not checked. A task that names no code is left for the person to check;
  a change whose tasks all name no code (docs only) is planned and checked all the same, and `check` says
  whether any code changed with it.
- A task that starts with add, remove, rename or "change the signature" is read that way. Anything
  else is a change in behavior.
- A scenario is proven by a test with the same name: one on the map, or one made at run time (a name built
  from a template, a pytest parameter) whose result in the test run carries that name. A scenario that states an
  invariant ("for any", "for every", "always", "never") is marked "invariant: a property test fits", with the
  library for its language (Hypothesis, fast-check, FsCheck). When such a test fails, `check` reads the shrunk
  counterexample from the runner's output and gives it as the reason: "contradicted: fails for amount=-1".

It then lists what the change reaches that no task covers, the patterns it sits in, the gaps that
block implementation, and the code that uses the same things and that no task names: other callers of
a changed function, other users of a field it uses, and users of an existing member that a new one is
named like (a new `EmergencyQueues` beside `EntryQueues`). Each of those lines is either right to
leave alone or a missing task. So is the last such list, which comes from git history rather than the map:
files that usually change with the files the tasks touch and that no task names (see
[Change coupling](#change-coupling)). Reviewers (the `leyline-adversarial-review` skill, one run
for logic and one for performance) file findings with node ids as evidence; only the person
resolves them (`leyline spec resolve <finding> accepted|rejected|deferred "why"`). A reviewer that passes
its kind (`--reviewer`, or `reviewer` on the `spec_review_facts` tool) is recorded as having run, so the page
can say a review found nothing rather than that none ran.

After implementation and a re-index, `verify` (inside `check`) marks each task from the graph diff and each scenario
from its test's recorded result, lists edits outside the spec, new links between modules and rules
newly broken, and appends the result to `leyline.md`. It exits 0 only when the change was done as
agreed. A new function that only code named in the spec calls is listed as a helper, not as an edit
outside the spec.

Each task and scenario gets one verdict, shown in the tables, counted under them ("7 proven, 1 needs
you, 1 inconclusive"), and given as `verdict` and `verdict_why` in the MCP `check` answer:

| Verdict | Task | Scenario |
| --- | --- | --- |
| proven | the code it names changed | its test passes, and failed or did not exist before, or passed both times while running the changed code |
| partial | some of the code it names changed, some did not | some results that carry its name pass, some fail |
| contradicted | the code it names did not change (or what it adds is missing) while other code did | its test fails after the change |
| inconclusive | the map cannot place the code it names, or nothing changed at all | no pass or fail was recorded for it |
| needs a person | it names no code (docs, say) | its test passes, but does not reach the changed code, or Leyline cannot tell whether it does |

A task that removes code is judged by what is left, since edited is not removed: "Remove `X`" is contradicted while
`X` still exists, partial when `X` is gone but code that called it still does ("`X` is gone but `caller` still
calls it"), and proven only when it is gone and uncalled. "Rename `X` to `Y`" also needs `Y` there.

Partial, contradicted and inconclusive hold up "done as agreed"; an item that needs a person is listed
and does not. A project can change that in `openspec/leyline.toml`, committed beside the changes:

```toml
[check]
blocking = ["contradicted", "inconclusive", "partial", "needs a person"]   # every item proven by the code
```

A tick in `tasks.md` never clears an item: the agent ticks tasks as it goes.

A spec can change part-way. Once the code has moved on, `brief` keeps the picture of the code from
the first brief, so `verify` still compares with the code as it was; `--new-baseline` starts over.
That picture is `.leyline/snapshots/<change id>.db`: one per change, taken once, holding only what the
comparison reads (nodes and their hashes, links, flows, a hash per source line) and what a diagram of the code
as it was needs (the line of each call, the order of each flow, each channel link's channel). It stays after the
change is checked done as agreed, so `check` can run again after a later edit, and is deleted with
`leyline spec forget <id>` or once the change folder is archived or removed. `leyline map` writes
`.leyline/.gitignore`, so the store stays out of git without touching your own `.gitignore`.

`skills/leyline-spec/SKILL.md` tells an agent how to write the folder and run the loop.

### Spec drift

Specs name code in backticks, and the code moves on without them: a function is renamed, a method gets
a parameter, a class moves to another file. `leyline drift` reads every backticked name in the living
specs (`openspec/specs/<capability>/spec.md`) and in finished changes (`openspec/changes/archive/<date>-<id>/`),
compares each with the map, and prints one page, spec by spec:

```
## openspec/specs/engine/spec.md

- `Engine.start` has changed signature since the spec was written: was `def start(self)`, now `def start(self, loud=True)`.
- `core.make_engine` is gone: py/src/pkg/core.py has no `make_engine` now.
- `Journal.note` has moved: it is now in py/src/pkg/journal.py (was in py/src/pkg/core.py).
```

A name is *gone*, *renamed*, *moved* (another file or owner), *signature changed*, *ambiguous* (it now names
several things) or *changed inside* (same signature; worth a read, not drift by itself). It exits 1 when something
is gone, renamed or changed signature. *Renamed* is a gone name with one new node beside it (same owner, same file
or module) that has its body without the name, or its declaration without the name when git or the change's
baseline shows the node is new; or a file git records as renamed: "`Engine.shout` has been renamed to
`Engine.yell`". Names that are not code (`true`, `GET`) are counted and left alone, and so is
code a task removes or renames.

To see a changed signature, Leyline must know what a name meant when it was right. When `check` finds a change
done as agreed, it records an *anchor* for each code name in the change's tasks and spec deltas: the node,
its kind and file, and fingerprints of its declaration (a function's signature line, a type's declaration and
member names, a field's type) and of its body. Anchors are kept in the store (table `spec_anchors`) and in
`openspec/leyline-anchors.json`: canonical JSON, sorted keys, keyed by change id (or `specs/<capability>`),
with node ids written without the repository's id so another clone reads them. Commit it with the specs; it
survives a fresh map, and wins over the store. A living spec's name is read against its own anchors, or those
of the change that wrote it. A name with no anchor is checked by name only. When the specs and the code agree,
`leyline drift --accept` records the code as it is now; code that is gone stays reported until the spec stops
naming it.

`leyline plan` reads the same anchors: a new change whose tasks touch drifted code lists, under "Specs that no
longer match code this change touches", lines such as "The living spec engine/spec.md names `Engine.start`,
which has changed signature since it was written."

### Assessing a change

1. You describe a change to an agent that has the Leyline MCP server connected.
2. The agent finds the nodes the description refers to and calls `propose_change`.
3. Leyline walks callers, interface links, channels and flows from those nodes and stores the result
   as a draft proposal with a saved view.
4. You refresh the map and open the Views tab: the change, what must be edited with it, what it
   reaches, the tests to run and the risks.

`skills/leyline-change-impact/SKILL.md` tells an agent how to do steps 2 and 3 well. The same
`save_view` tool lets an agent save any other slice of the code as a view.

### Reviewing a pull request

A spec says what a change will do before it is written. A pull request comes the other way round: the code
exists, and what it was meant to do is a title and a paragraph. `leyline pr` reads the change from the code:

```
gh pr checkout 123
leyline pr main --about "what the pull request says it does"    # or: leyline pr --github 123
```

It maps the commit the branch left `main` at (from `git archive` into a temporary folder, so the repository
and its worktrees are not touched; files that did not change keep their parse output, so this costs a few
seconds), compares it with the checkout, uncommitted edits included, and writes
`.leyline/reviews/pr-<id>.md`:

- **What changed**: functions edited, added and removed, a changed parameter list as it was and as it is,
  and the files the map does not read (docs, styles, data), so you know where its view stops.
- **What it reaches and did not change**: callers of a changed signature that were not edited, removed code
  that is still called, and each channel the edit touches (an edited line within a few lines of the route,
  table, event or program it names) with every other end that must agree. A channel the changed function
  sits on but the edit does not touch is left out.
- **Shares a caller, a field or data with the change**: weaker leads, for a reviewer to read.
- **Usually changes with what it changed, and it did not change**: from the history before the branch (see
  [Change coupling](#change-coupling)).
- **Tests**: whether it edits any, which changed code no test reaches, and which tests run it.
- New dependencies between modules, rules that now fail, and the repository's own rules for reviewers
  (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` ...) to review against.

With no `--about`, the commits' messages stand for the description. The review is stored as `pr-<id>` (the
pull request's number, else the branch name), and everything that takes a change id takes it: `leyline spec
facts pr-123 --reviewer logic` gives the adversarial reviewers their facts, `leyline spec finding pr-123 ...`
files one, and the page lists them. A finding whose evidence is nowhere near the change (not on its blast
radius, nor one call from it) is kept but marked, so the person questions it first. `leyline spec forget
pr-123` deletes the base's map.

`leyline pr` exits 0 whatever it finds. With `--gate` it exits 1 while something that blocks is left, so it
can hold up a merge in CI. The page opens with a **Gate** section: whether it passes, what blocks under which
config, one line for each thing that blocks, and a `Next:` line saying what to do about the first. The MCP
`review_pr` answer carries the same lines as `blocking`, and `gate_passed`. Each run judges the gate again from
the code at the checkout and the findings as they stand, so a commit that fixes a caller, or a finding the person
resolves, clears it on the next run.

By default four things block, the ones the map shows are broken: a caller of a changed signature that was not
edited, removed code that is still called, a confirmed error-level rule (see [Rules](#rules)) that now fails and
did not at the base, and an open high finding. A line that rests only on a link the map guessed by name never
blocks; the Gate section counts those so you can read them. A project picks its own set in
`openspec/leyline.toml`, as it does for `check`:

```toml
[pr]
blocking = ["unedited-callers", "still-called", "failing-rules", "open-high-findings"]   # the default
```

The kinds are `unedited-callers`, `still-called`, `failing-rules`, `open-high-findings`, `open-medium-findings`
(medium or high), `open-findings` (any severity), `other-ends` (the other end of a channel the edit changed, not
edited) and `untested` (changed code no test on the map reaches). `blocking = []` lets everything through.

In CI, check the pull request's head out with enough history to find where it left the base:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- run: pip install leyline-code
- run: leyline pr origin/${{ github.base_ref }} --gate
```

### A quick change

A spec folder is too much for "make the retry count 3". `leyline quick` gives the same three answers with no folder:

```
pytest -rA | leyline quick "make the retry count 3" --about RETRIES fetch --tests -
... edit ...
pytest -rA | leyline quick --done quick-make-the-retry-count-3 --tests -
```

Before the edit it looks up the named code as a task's names are looked up (`--about`, or names in backticks in the
sentence; a constant the map has no node for is found where it is set, with the functions that read it), assesses what
it reaches, records the test run, keeps a baseline, and prints a short page: what it will touch, what must be edited
with it, the channels it touches or is reached across, the tests that run it, and the command to run when done.

After the edit, `--done` maps the code again and compares it with the baseline as `leyline pr` compares a branch with its
base. One line gives the verdict, then four items, each proven, partial, contradicted, inconclusive or needs a person:
the edits stayed in the named code (a new helper only it calls, and a caller updated to match a changed signature,
count as part of it), no caller was left broken, no test broke against the run from before, and a test that passed ran
the changed code. It exits 0 when nothing that blocks is left (by default partial, contradicted and inconclusive; the
project's `openspec/leyline.toml` applies). An edit that belongs but was not named is named afterwards, keeping the
baseline: `leyline quick --done <id> --about <name>`.

When the change has grown past quick (more than three functions edited, a channel crossed, or a caller that must
change left as it was), the page says so and gives the way into a spec: write `openspec/changes/<id>/`, run
`leyline quick --to-spec <id> quick-<slug>` to hand it this baseline and the test run from before, then `leyline plan
<id>` and `leyline check <id>`.

The change is stored as `quick-<slug>`, and the review steps take it as they take `pr-<id>`: `leyline spec facts
quick-<slug> --reviewer logic`, `leyline spec finding`, `leyline spec findings`, `leyline affected-tests` and
`leyline spec forget quick-<slug>` (which deletes its baseline). The MCP tool is `quick`.

Run it again after new commits and the page opens with **Since the last review (`<sha>`, `<when>`)**: the functions
edited, added and removed between the two heads (not since the base), the facts that are new (a caller newly broken)
and those that are gone, and each open finding marked "may be fixed" (the code its evidence names changed since it
was filed: re-check it) or "still applies" (do not file it again). Each run is kept in the store with its head, its
time and its facts, and a slim map of the code at that head (`.leyline/snapshots/pr-<id>.head-<n>.db`, the last
three and any an open finding was filed against); running it again on the same code changes nothing. The reviewers'
facts carry the same as `since_last_review`.

Both `leyline pr` and `leyline plan` list **Earlier changes to this code**: up to five finished OpenSpec changes
(archived, or checked) and earlier pull request reviews that touched the same functions or types, newest first, with
the names they share; the facts carry them as `related_changes`. A repository with no such history gets the commits
before the change that changed the same files instead (of the last 500 that touched them), most overlap first.

### Learning from rejected findings

Many review findings that people reject are correct about the code but miss a choice made on purpose. So
when you reject a finding and say why (`leyline spec resolve <finding> rejected "why"`), Leyline keeps a
learning: your reason in your words, the finding's claim, its kind of review, and the code it is about (the
evidence nodes, and the type, file and module each is in). A rejection with no reason keeps nothing.

Learnings go in a file in the repository, so you commit them, the team shares them, and a fresh map keeps
them: `openspec/leyline-learnings.json` when the repository has an `openspec` folder, else
`.leyline-learnings.json` at its root. A file stays where it was first made. It is JSON with sorted keys
and a two-space indent, one entry per learning: `id`, `status` (active or retired), `reviewer`, `claim`,
`reason`, `scope`, `fingerprint` (the code it is about, see below), `source` (the change and finding it
came from), `created`, `confirmed` (when a person last said it holds for the code as it is), `hits`,
`dismissals`, `accepted`, and `findings` (the later findings it matched, with what people decided). Node
ids in it leave out the repository's id, so a clone in a folder of another name reads them.

- **Reviewers read them first.** The facts for a spec or a pull request (`leyline spec facts`, or
  `spec_review_facts`) start with `learnings_that_apply`: active learnings about code the change touches or
  reaches, closest first.
- **A repeat is marked, not dropped.** A new finding of the same kind of review, on the same node, type or
  file, whose claim shares at least 45% of its words with a learning's claim (60% when only the module is
  shared), comes back with `learned`, naming the learning and its reason. The page shows it as "Matches a
  past decision: ..." and the person still decides it. Words are compared loosely: lower case, code names
  split (`parseArgs` and `parse_args` are one name, `app.use.count` is `count`), the names of the code both
  claims are about left out (they say where, not what), common words dropped, endings cut, words a review
  uses for the same thing made one (about forty groups, such as argument and parameter, remove and delete,
  null, None and undefined, caller and call site; `SYNONYMS` in `learnings.py`), and words found in nearly
  every claim (file, read, value) counted half.
- **A wrong learning retires itself.** Each later decision on a finding it matched is counted. Once people
  have accepted at least two of them, and more than they rejected, the learning is retired and the file
  says why. It is also retired if the finding it came from is later marked anything but rejected.
- **A learning about code that has changed says so.** A learning keeps a fingerprint: for each evidence
  node, the hash the map keeps of that node's own lines (trimmed, so moving or re-indenting it does not
  count; the file's hash for a node with none). Each time a learning is used, in `learnings_that_apply`,
  in `learned` on a new finding, on the page and in `leyline learnings`, it is compared with the map. If a
  node was edited or is gone, the learning is `stale` and names them in `edited` and `gone`. It still
  applies and still marks findings, and is not retired. The page says "Matches a past decision, but the
  code it was about has changed since: ..." and asks whether it still holds. That is the person's call:
  `leyline learnings confirm <id>` takes the fingerprint again for the code as it is now, and
  `leyline learnings retire <id>` ends it. A learning kept before Leyline recorded fingerprints has none.
  Whether its code changed is unknown, not stale, and it says so; confirming it gives it one.

`leyline learnings` lists them; `leyline learnings retire <id> "why"` retires one by hand;
`leyline learnings confirm <id>` says one still holds for the code as it is now.

### How it runs: sequence diagrams

`leyline.md` (under "What code will be written", and again after `check`) and the pull request page carry a
Mermaid sequence diagram of the changed code, which GitHub draws from the ```` ```mermaid ```` block: how execution
reaches it from the nearest entry point (along the stored flow, in source order), and what it calls. Participants
are types, or the file for a function at the top of a file. Every arrow is an edge on the map: a call, a call through
an interface into an implementation, or a channel link, drawn with an open arrowhead (`-)`) and named (`http GET
/api/x`, `writes table orders, which load() reads later`); a link the map guessed by name is dotted. The changed
code is shaded. A diagram keeps to about 8 participants and 25 arrows, and says how many calls it left out.

After a change, the check page and the pull request page show two diagrams, **Before** and **After**, then list what
changed in how it runs: calls and channel links into or out of the changed code, added and removed since the
baseline. The Before diagram is drawn from the baseline alone, never from the map as it is now, and is shown only
when every one of its arrows is a link the baseline holds (`diagrams.unbacked(baseline, d)`; given the map, it
checks a diagram of the code as it is). A baseline taken by an older Leyline kept which pairs were linked but not in
what order: it still compares, and the code as it was is listed, not drawn. Keeping the order made a baseline of
Parlance about 22% larger (8.2 MB to 10.0 MB).

### Reviewing a change after it is made

The first `propose_change` of a change keeps a baseline in `.leyline/snapshots/<change id>.db` (what the
comparison reads, not a copy of the store). Once the change is implemented:

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
- Istanbul's `coverage-final.json` (vitest `--coverage.reporter=json`, jest `--coverageReporters=json`). It covers a
  whole run, so run one test file at a time and import each with `--test <that file>`: each function is then tied
  to the test file that ran it, not to the one test inside it.

#### Scenarios proven by what ran

A scenario's test passing says the behavior holds; it does not say the test ran the code the change edited. Pass the
coverage of the same run to `check`, and each scenario also says whether its test ran the changed code:

```
pytest -rA --cov=src --cov-context=test > after.txt
leyline check <change> --tests after.txt --coverage .coverage
```

Each scenario result gets `ran_changed_code` (true, false, or null when it cannot be told) and a one-line
`ran_changed_code_note`. A test that passed without running the changed code is listed under **Weaker proof**: it
would pass whatever the change did. With no per-test coverage, `check` reads as it always has.

#### The tests a change needs

`leyline affected-tests <change>` (the `affected_tests` tool) lists the tests to run for a planned change or a
`pr-<id>` review, each with why, and prints a command that runs them (`pytest path::test ...`, `npx vitest run
<files>`, `npx jest <files>`, `go test -run`). With per-test coverage it takes the tests measured running the changed
or must-edit code, the change's own new tests, and, from the map, tests that reach changed code no measured test ran
or that the measured run left out; without it, the tests whose path on the map passes through the change. Feed that
smaller run to `check`. `leyline pr` lists the tests measured running the changed code the same way.

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
- A call on the field to a method that changes a collection in place (`list.Add(x)`, `items.append(x)`,
  `queue.push(x)`) is a read and a write. The methods are named in a short list per language (`MUTATORS` in
  each adapter); any other method call on a field is a read, since the map cannot tell what it does.
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

### Change coupling

Some files have to change together with nothing in the code to say so: a doc and the rule it describes, a JSON
schema and its reader, a test fixture, the other side of a protocol. Git history shows them. `leyline coupling
<file>` lists what usually changed in the same commits as a file, and `leyline coupling` alone the most coupled
pairs:

```
$ leyline coupling tooling/validate.py
`tooling/validate.py` changed in 12 commits (from the last 206 commits, leaving out 17 that changed more than 50
files). What usually changed with it:

- files in `editor/core/src/validation/`: 7 of 12 (58%)
- `tooling/conformance/validator/build_cases.py`: 7 of 12 (58%)
- files in `tooling/conformance/validator/cases/`: 7 of 12 (58%)
- `tooling/scripts/mutation_probe.py`: 6 of 12 (50%)
```

That is the rule a contributor's guide states in words (a validator rule changes both validators, adds a
conformance case and a mutant), found from history alone.

A file is listed when it changed together with the first in at least 3 commits and in at least half of the first
file's commits (`--min-together`, `--min-confidence`). A folder is listed when its files did, though no one file
did (a rule that comes with a new test case each time), unless it is at the top of the repository. Read: the last
1,000 commits within two years, merges and commits that changed more than 50 files left out, renames followed.
It is worked out once per commit and kept in the store. `plan` lists the files that usually change with the files
the tasks touch and that no task names, and `pr` those that usually changed with what the branch changed and that
it did not; `leyline spec facts` has the whole list.

The same is worked out by function, for the functions the tasks name (in `plan`) or the branch edited (in `pr`), up
to 6 of them: "`spec.py.verify` changed in 8 of the 9 commits that changed `spec.py.brief`". A past commit's line
numbers say which function they were in only against the file as it was then, so this reads, for each function, the
last 20 commits that changed its lines (`git log -L`), parses its file as it was at each with the same parser the map
uses, and puts each changed line in the innermost function around it; then reads the same way the 4 other files that
changed in most of those commits. The same thresholds apply (3 commits, half of them). Which functions a commit
changed in a file is kept in the store, so only the first plan pays for it (about a second on this repository; a
function renamed or moved counts only from then).

### An outline for an agent

`leyline context <focus...> [--tokens N]` (the `context` tool) gives an agent about to edit some code a short outline of
the code around it, cut to a token budget (2,000 by default, 200 to 8,000; a token is counted as four characters):

```
$ leyline context registerLoreRoutes --tokens 1000
Code around registerLoreRoutes: declarations, the most related files first; > marks the focus.

editor/host/src/lore-routes.ts
  const relOf = (params: unknown): string
      -- called by registerLoreRoutes
> function registerLoreRoutes(server: FastifyInstance, opts: { index: () => ReferenceIndex }): void
      -- answers GET /api/lore-files, GET /api/usages/:type/:id; called from 1 place, calls 12

editor/host/src/server.ts
  async function buildServer()
      -- answers GET /api/types, ... and 29 more; calls registerLoreRoutes
...
editor/client/src/lib/loreApi.ts
  const loreApi = { ... }
    listFiles: ()
        -- requests GET /api/lore-files
Shown: 30 symbols in 17 files.
Left out: 1 more symbol linked directly to the focus and 422 two links away, in 120 files; the nearest: ...
```

The focus is node ids, names (`Owner.method`), file paths, a change (`spec-<id>`, `pr-<id>`: the code its plan or review
marked) or words to search for. Every function, type, field and test is ranked by personalized PageRank from the focus
over calls, channel links, type use and containment, each way, with a small boost for code a test reaches and code an
entry point runs first. The outline shows declaration lines, not bodies, file by file, most related file first: the focus
marked `>`, each channel end in words ("answers GET /api/x", "writes table t"), and what calls the focus or what it calls
said so. The last lines say how much was left out and name the nearest of it. The ranking visits only code near the focus,
and the graph is read once per map run: on Parlance (160,000 lines) the first call takes 0.3 s and later ones 0.05 s.

### Asking how something works

Leyline holds no language model; an agent asks it in words and reasons over what comes back. Two commands (and the
tools of the same names) make "how does a vehicle get spawned" or "what happens when a writer saves a dialogue" a
question with an answer on the map, and the `leyline-explain-flow` skill drives them:

```
$ leyline find-flows "what happens when a writer saves a dialogue in the editor"
 1. DialogueScriptEditor.save  [UI event handler]  editor/client/src/surfaces/DialogueEditor/DialogueScriptEditor.tsx:338
      matched: saves in its name; dialogue in the name of what it is in; editor in the name of what it is in
 2. DialogueCanvas.onInspectorSave  [UI event handler]  editor/client/src/surfaces/DialogueEditor/DialogueCanvas.tsx:764
 ...
$ leyline explain-path DialogueCanvas.save
Walk from DialogueCanvas.save: 40 of 154 steps shown.
  1. DialogueCanvas.save  (editor/client/src/surfaces/DialogueEditor/DialogueCanvas.tsx:230)
  2.    call -> useAppStore.saveEntity  (editor/client/src/store.ts:754)
           at editor/client/src/surfaces/DialogueEditor/DialogueCanvas.tsx:233: return saveEntity(updated, label, ...
 ...
 17.             http PUT /api/entities/:type/:id -> the PUT /api/entities/:type/:id handler  (editor/host/src/server.ts:361)
                    crosses to another process or service
 ...
 31.                call -> scheduleValidation  (editor/host/src/validation.ts:161)
```

`find-flows` ranks every function and test by the words it shares with the description: in its name, its route, the
test's name, the name of the type or component it sits in, its file's name and folders, its language, and the comment
just above it (or a Python docstring). Code names are split (`saveDialogue` is save and dialogue), endings are cut
(saves, saved, saving), a short table makes words that mean the same in code one (save, write, persist, store, put;
create, add, spawn; run, execute, play, start; about twenty groups, `SYNONYMS` in `explain.py`), a word rare on the
map counts for more, and a word for who does it (a user, a writer) counts little. Route handlers, UI event handlers,
program entries, message handlers and commands are ranked up, then tests. Each candidate says why it matched and which
flows start there or reach it; `ambiguous` is set when the first ones score close in different files, so the agent
asks the person which they mean. The words index is built once per map run (about a second on Parlance) and kept: a
later question takes a few milliseconds.

`explain-path <start>` walks from a function, a route (`"PUT /api/x"`), a test or a flow id. Alone, it follows the
stored flow that starts there, or walks the same way now (depth first, in the order the code makes its calls, 8 calls
deep at most), and shows the shallow steps first and every hop across a channel, up to `--steps` (40); leaves called
from 8 or more places are counted as helpers, not shown. `--to` gives the shortest path (without file, table and key
hops when there is one), `--through` the path through a node and on from it. Each step says how it was reached (a
call, `http PUT /x`, `starts <program>`, a message, an interface's implementation), the calling line and the callee's
declaration line, and `later, elsewhere` for a table, file or key it writes that other code reads later. The walk ends
with what the map could not see: guessed links, interfaces, the depth limit, and a program a type in the walk started
and talks to over its pipes (the messages are not calls the map can follow, so it names the program to walk next).
Its diagram has one arrow per step, each a link on the map. `leyline diagram <ids>` draws the usual sequence diagram
for any functions or types.

### Finding your way around a large module

On a large repository a module is still huge: Parlance's `editor/client` holds 249 files and the map draws it as one
box. `leyline outline <module>` (the `module_outline` tool) splits it into at most twelve parts, largest first, and
says for each what it holds and how it connects. With no module it lists the repository's modules. Here is
`editor/core/src` after an agent named its parts:

```
$ leyline outline parlance:dir:editor/core/src
Core library (folder, editor/core/src/): 67 files, 669 functions, 20,380 lines.
The project model and every engine on it: storage, validation, explore, play, prose, review, export.
Split by folders and groups of files. 11 of 12 parts have a name.

1. Storage and rename (projectStorage.ts group)  [system]: 7 files, 104 functions, 3,098 lines
   id: parlance:dir:editor/core/src#system:projectStorage.ts
   Canonical JSON in and out, the one atomic write path, loading a project, and entity-id renames.
   entry points: none; 103 of 104 functions on a test's path
   uses: editor/runtime (25), Validation rules (8), Validator (8), Review threads and localization (6), ...
   used by: Core tests (496), editor/host (175), editor/mcp (64), editor (16), editor/client (10), projectDiff.ts (6)
   channel: file from editor/host, 5 links (e.g. threads/*.json)
   busiest: stringify (887 flows), parse (753 flows), ownValue (738 flows)
   key types: StorageContext, RenamePlan, EntityType, RenameInput
   risky: busy: its functions are on the most flows
2. Validation rules (validation/): 9 files, 78 functions, 2,850 lines
   ...
12. 12 more parts  [more]: 18 files, 85 functions, 2,565 lines
```

A part is a folder when the folder tree means something; a folder that holds nearly all of a module is shown instead
of it (`editor/client` is shown from `editor/client/src`), with the few files beside it as parts of their own. Where a
folder is flat, its files are grouped by Louvain community detection over calls, type use and inheritance between
them, each file going to the group that holds most of its code; when few of them link to each other (a folder of
tests), they are grouped by the code they use instead. A module that is one flat folder reuses the systems the map
proposed. A level of more than twelve parts shows the eleven largest and a `@more` part holding the rest. A file or a
group of one file is a leaf: its key functions, most flows through them first.

For each part: its size; its entry points (routes, UI handlers and components, programs, commands, message handlers)
and tests, and how many of its functions are on a test's path; what it uses and what uses it (its siblings, the
folder next to it, other modules), as counted calls and type links; the channels that cross its edge; its busiest
functions (most flows through them) and the types other code leans on most; and `risky` beside its siblings: the
busiest, the most depended on, and those few tests reach. `--depth 2` (the default) also lists the parts inside each
part; drill in with a part's id. Ids hold across maps: `<repo>:dir:<path>` for a folder, `<path>#files` for the loose
files beside its folders, `<path>#system:<anchor>` for a group (named after its most linked type or file), a file's or
a module's node id for those.

An agent names a part after reading its code: `name_part(part_id, name, summary, evidence)`, or `leyline name-part
<part id> "Validation rules" --summary "..." --evidence <node ids>` (`--intent` when the person said it). Names are
kept in the store (table `part_names`) with the part's files at the time, so they survive a re-map: a group whose
anchor changed takes the name of the old group of its folder it shares most files with, and a name whose part kept
less than half its files, or most of whose evidence is gone, is shown as "may be stale". Outlines, `overview` (module
titles and `named_parts`), `explain_path` (each step says the named part it enters) and the map page show the names.
Naming a system the map proposed also sets its `name` and `responsibility`.

The `leyline-explore-module` skill walks an agent through it: outline the module, read each part's key code, name
it, and give the person a one-screen guide (the parts, how they connect, where to start reading for common tasks,
which parts are risky), offered as a tour.

## The map

`view` and `export` open the same page. It has up to four zoom levels:

1. **Modules.** One box per module, with an arrow for each dependency and a count of the links behind it.
2. **Inside a module.** A module of 30 files or more is drawn as its parts, as `leyline outline` splits it (see
   [Finding your way around a large module](#finding-your-way-around-a-large-module)), with their names and sizes;
   open a part for the parts inside it, then its types and files. A smaller module shows one box per system where
   it was split, otherwise one per type or file.
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
| `check(change, test_output?, test_results?, coverage_path?)` | Step 3: re-index, record the tests from after, the verdict and `next`; with the run's coverage, whether each scenario's test ran the changed code |
| `affected_tests(change)` | The tests to run for a change or a `pr-<id>` review, and the command that runs them |
| `overview(scope?, limit?)` | Repos, modules with sizes, module-to-module dependencies by edge kind, systems, external packages, extractors that ran |
| `cross_repo` | In a workspace of several repositories: links between them, functions most called across, flows that cross and come back |
| `search(text, kind?, limit?)` | Node ids matching a name, qualified name or path |
| `expand(node_id, limit?)` | One node in detail: contents, dependencies, dependents, callers and callees |
| `neighbors(node_id, direction?, kinds?, limit?)` | Raw edges around a node, by kind |
| `source(node_id, max_lines?)` | The node's source text |
| `context(focus, budget_tokens?)` | A short outline of the code around a focus (ids, names, paths, a change, or words), most related first, cut to a token budget |
| `module_outline(module?, depth?)` | A large module split into at most 12 parts (folders, or groups of files where a folder is flat): each part's size, entry points, links in and out, channels, busiest functions, risks and name; drill in with a part's id |
| `name_part(part_id, name, summary?, evidence, layer?)` | Name a part from the outline with a one-line summary; kept across maps, shown as "may be stale" when the part changes a lot |
| `flows(kind?, through?, limit?, offset?)` | Flows walked from each entry point and test; `through` keeps flows that pass a node |
| `flow(flow_id, max_steps?, offset?)` | One flow step by step, in source order, with call depth |
| `trace(from_id, to_id)` | The shortest chain of calls and channels between two functions |
| `find_flows(description, limit?)` | Where a behavior described in words could start: entry points, route, UI and message handlers, tests named for it, functions; each with why it matched, its kind, file:line and the flows that start there |
| `explain_path(start, to?, through?, max_steps?)` | An ordered walk across calls and channels from `start` (its main flow, the shortest path `to` a node, or the path `through` one), each step with how it was reached, the calling line and the declaration; data written for later marked; a Mermaid diagram of the walk |
| `diagram(ids)` | A Mermaid sequence diagram of how execution reaches some functions or types and what they call |
| `impact(node_id, max_depth?, limit?)` | What can reach a node: callers by module and the flows through it |
| `annotate(node_id, key, value, evidence, confidence, layer)` | Write an inferred or intent statement about a node |
| `propose_change(intent, targets, title?)` | Assess a change without an OpenSpec folder and save its blast-radius view |
| `save_view(title, narrative, marks, legend?)` | Save any set of marked nodes as a view |
| `review_change(change_id, before_run?, after_run?)` | Compare a change assessed with `propose_change` with what was done, and save a review view |
| `record_test_run(run, results)` | Store one test run under a label, for `review_change` |
| `add_rule(kind, selector_from, selector_to?, ...)` | Add an architecture rule, suggested unless the user stated it |
| `check_rules()` | Evaluate every rule against the graph |
| `review_pr(base?, about?, github?, review_id?, path?)` | Review a checked-out branch or pull request with no spec: what changed, what it reaches and did not change, tests; returns `change_id` (`pr-<id>`), `blocking` (one line for each thing that holds up the merge) and `gate_passed` |
| `quick(what?, names?, done?, test_output?, test_results?, coverage_path?, change_id?)` | A small change with no spec folder: before (`what`, `names`), what it touches and the tests that run it; after (`done`), one verdict, and `grown` when it needs a spec |
| `spec_review_facts(change, reviewer?)`, `spec_finding(change, ...)`, `spec_findings(change)`, `spec_resolve(finding_id, status, resolution?)` | Adversarial review of a planned change, or of a pull request by its `pr-<id>` |
| `learnings(retire?, why?, confirm?)` | Past decisions on review findings, kept from rejections with a reason, each marked `stale` when its code changed since; `retire` one the person says no longer holds, `confirm` one they say still holds for the code as it is now |
| `spec_brief(change)`, `spec_verify(change, before_run?, after_run?)` | The steps inside `plan` and `check`, one at a time; rarely needed |
| `drift(path?, accept?)` | Code the living specs and finished changes name that is gone, moved, changed signature or ambiguous; `fails`, the page, `next` |
| `shared_state(scope?)` | Fields assigned from outside the type that declares them |
| `coupling(path?, min_together?, min_confidence?, limit?)` | Files (and folders) that usually change in the same commits as a file, from git history; with no path, the most coupled pairs |
| `coverage(node_id?, flow_id?, import_path?)` | Measured coverage: what ran, set against the static paths |
| `patterns(pattern?, node_id?, limit?)` | Design patterns found by shape, with roles, rationale and confidence |
| `label_pattern(pattern, roles, rationale, confidence?)` | Record a pattern the matchers missed |
| `tours()`, `tour(tour_id)` | List tours, or read one stop by stop |
| `save_tour(title, stops, audience?)` | Save a tour written for the user |
| `views()`, `view(view_id, limit?)` | List saved views, or read one |
| `skills(skill?)` | The skills that ship with Leyline, each with when to use it, or one skill's text. Each is also an MCP prompt of the same name |

`change` is a change folder or its id. A tool that cannot answer returns an error saying what to do
instead (no store yet: call `map`; an unknown id: find it with `search`). Each answer is at most about
24,000 characters: lists show their first items and their total, `cut` names any list that was
shortened, and `more` says how to see the rest (`limit`, `offset`, `scope`, or a narrower id).

## What is indexed

### Which files

The files git lists: tracked ones, and untracked ones `.gitignore` does not exclude. A directory that is
not a git repository (or one git refuses to read, such as a checkout owned by another user) is walked
instead, with the common `.gitignore` patterns applied. Some files are left out, and `map` names them with
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
  In TypeScript (Fastify, Express, Hono, Koa routers), a route's inline handler
  (`server.get("/api/x", async (req) => ...)`, `server.route({ method, url, handler })`, or
  `server.get(path, { schema }, handler)`) is a function of its own, named for its route (`GET /api/x`)
  and nested in the function that registers it: the request lands on it, what it calls is its own, and
  the registrar registers it (kept as a call, so startup still reaches it). A handler given by name
  (`server.get("/x", listThings)`) is that function. When several handlers' routes fit a request, the
  route that names more of the path outright wins (`/api/review/health` over `/api/review/:id`), and a
  request with no method written is taken as a GET. In a pull request, an edit inside a handler touches
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
listing each function once. Flows follow calls, calls through an interface into its implementations
(the registered one first), process launches, HTTP requests, messages to their handlers, remote calls,
and events whose handler was subscribed earlier in the same flow. They do not follow files or tables:
writing one does not run its reader. They stop at
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

A new skill is a folder under `skills/` with a `SKILL.md`. That folder is the only copy: the wheel carries it as
`leyline/skills/`, and `leyline skills`, the MCP prompts and the `skills` tool pick it up with no other edit.
`tests/test_skills.py` checks each skill's frontmatter and that the wheel's copy matches.

## License

Copyright (C) 2026 Matthew Burke.

Leyline is free software, licensed under the GNU General Public License, version 3.
See [LICENSE](LICENSE) for the full text. It comes with no warranty.
