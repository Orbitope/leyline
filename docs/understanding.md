# Understanding a mapped codebase

What the map finds and how to ask it: exact references from a compiler, measured coverage, field reads and writes, design patterns, tours, change coupling, outlines for an agent, asking how something works, finding your way around a large module, and the map page.

Part of Leyline's documentation: the [README](../README.md) is the start, and [quick-reference.md](quick-reference.md) is one page of what to ask.

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
  whole run, so run one test file at a time and import each with `--test <that file>` (`test` on the `coverage`
  tool): each function is then tied to the test file that ran it, not to the one test inside it.
- lcov (`lcov.info`, from c8, vitest or jest with the `lcov` reporter, and most other tools). Lines (`DA:`) and
  functions (`FN:`, `FNDA:`) that ran; records under a `TN:` test name are tied to that test, and a file with no test
  names is a whole run, imported with `--test` as Istanbul's is. Paths may be absolute, relative or Windows-style.

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
<files>`, `npx jest --runTestsByPath <files>`, `go test -run`). With per-test coverage it takes the tests measured running the changed
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
flows start there; one where none starts names the entry points' flows that reach it (`reached_from`) and the tests'
(`tests_reaching`). `ambiguous` is set when the first ones score close in different files, so the agent
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

The page opens on the first stop of the orientation tour (what the repository is, where to start reading,
the patterns and the paths worth tracing), until you end the tour once; the Tour tab brings it back, and
any tour an agent saved. Under More, the Matrix tab shows the same level as a dependency matrix, ordered
so that cycles appear above the diagonal; the Flows tab steps through one flow at a time, with the source
of the selected step beside it; the Saved views tab shows saved views: for a proposed change, the marked
code colored by role beside the impact report.

The exported page embeds the graph and the source text of every indexed file, so share it with the
same care as the repository. Pass `--no-sources` to leave source text out.
