# Planning, reviewing and checking a change

How Leyline takes a change from a spec to a verdict: the spec loop step by step, spec drift, assessing a change before it is written, reviewing a pull request, a quick change with no spec, learning from rejected findings, sequence diagrams, reviewing a change after it is made, and architecture rules.

Part of Leyline's documentation: the [README](../README.md) is the start, and [quick-reference.md](quick-reference.md) is one page of what to ask.

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

node --test's TAP writes a tab in a test's name and a backslash followed by `t` the same way (`\\t`, and so
for `\n`, `\r`, `\b`, `\f`, `\v`); Leyline reads it as the tab, so a name with a literal `\t` in it may not
match its test.

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
[Change coupling](understanding.md#change-coupling)). Reviewers (the `leyline-adversarial-review` skill, one run
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
the first brief, so `verify` still compares with the code as it was; `--new-baseline` starts over. The code has moved
on when any source file on the map has changed: a constant at the top of a module counts, and so, to be safe, does a
comment.
That picture is `.leyline/snapshots/<change id>.db`: one per change, taken once, holding only what the
comparison reads (nodes and their hashes, links, flows, a hash per source line) and what a diagram of the code
as it was needs (the line of each call, the order of each flow, each channel link's channel). It stays after the
change is checked done as agreed, so `check` can run again after a later edit, and is deleted with
`leyline spec forget <id>` or once the change folder is archived or removed (a folder that a local branch not checked
out still holds is not removed: it is back when that branch is). A baseline deleted any other way (by hand, or with
the snapshots folder) is lost, not forgotten: `plan` will not quietly take the code as it is now as the start, which
after the change is implemented would leave `check` nothing to find, and asks for `--new-baseline` to take a new one.
`leyline map` writes `.leyline/.gitignore`, so the store stays out of git without touching your own `.gitignore`.

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
  [Change coupling](understanding.md#change-coupling)).
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
"Checked" means its latest check found it done as agreed: a check that fails after one that passed makes it unfinished
again, and so does planning it again (it is then reopened; the store keeps each check's time and verdict).

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
ids in it leave out the repository's id, so a clone in a folder of another name reads them. In a workspace of
several repositories each keeps its own file, and a learning applies only to code in its own repository.

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

`leyline learnings` lists them, and exits 1, naming the file, when a learnings file cannot be read;
`leyline learnings retire <id> "why"` retires one by hand; `leyline learnings confirm <id>` says one still holds for
the code as it is now.

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
