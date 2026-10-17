# MCP tools

Every tool the `leyline serve` MCP server offers, with its arguments.

Part of Leyline's documentation: the [README](../README.md) is the start, and [quick-reference.md](quick-reference.md) is one page of what to ask.

| Tool | Returns |
| --- | --- |
| `map(paths?)` | Step 1: index the code (or map again what the store holds); counts, the map page, `next`. One checkout that is not the server's own (a git worktree) gets the store inside it, which the server reads from then on until it restarts (`store_note`) |
| `plan(change, test_output?, test_results?, new_baseline?)` | Step 2: the one page for a change, `status.blocking`, `next`; records the tests from before; `new_baseline` compares from the code as it is now |
| `check(change, test_output?, test_results?, coverage_path?, mutants_path?)` | Step 3: re-index, record the tests from after, the verdict and `next`; with the run's coverage, whether each scenario's test ran the changed code; with the results of the change's mutation plan, whether its own tests failed a mutant of it |
| `mutants(change, max_mutants?)` | A plan of small breaks of the code a spec change edited, written under `.leyline/mutants/<change>/` (a patch for each, `plan.json`, and `run.py`, which someone runs; Leyline never does): the mutants, the tests that reach each, and how to run it. Spec changes only |
| `affected_tests(change)` | The tests to run for a change or a `pr-<id>` review, and the command that runs them |
| `overview(scope?, limit?, budget_tokens?)` | Repos, modules with sizes, module-to-module dependencies by edge kind, systems, external packages, extractors that ran |
| `cross_repo(limit?)` | In a workspace of several repositories: links between them, functions most called across, flows that cross and come back |
| `search(text, kind?, limit?)` | Node ids matching a name, qualified name or path |
| `expand(node_id, limit?, budget_tokens?)` | One node in detail: contents, dependencies, dependents, callers and callees |
| `neighbors(node_id, direction?, kinds?, limit?, budget_tokens?)` | Raw edges around a node, by kind |
| `source(node_id, max_lines?)` | The node's source text |
| `context(focus, budget_tokens?, bodies?)` | A short outline of the code around a focus (ids, names, paths, a change, or words), most related first, cut to a token budget; with `bodies`, the code too: the focus's body, the types it takes, and the lines around each call to it |
| `module_outline(module?, depth?, budget_tokens?)` | A large module split into at most 12 parts (folders, or groups of files where a folder is flat): each part's size, entry points, links in and out, channels, busiest functions, risks and name; drill in with a part's id |
| `name_part(part_id, name, summary?, evidence?, layer?)` | Name a part from the outline with a one-line summary; kept across maps, shown as "may be stale" when the part changes a lot |
| `flows(kind?, through?, limit?, offset?)` | Flows walked from each entry point and test; `through` keeps flows that pass a node |
| `flow(flow_id, max_steps?, offset?)` | One flow step by step, in source order, with call depth |
| `trace(from_id, to_id)` | The shortest chain of calls and channels between two functions |
| `find_flows(description, limit?, budget_tokens?)` | Where a behavior described in words could start: entry points, route, UI and message handlers, tests named for it, functions; each with why it matched, its kind, file:line and the flows that start there, or else the entry points' flows (`reached_from`) and the tests' (`tests_reaching`) that reach it |
| `explain_path(start, to?, through?, max_steps?, budget_tokens?)` | An ordered walk across calls and channels from `start` (its main flow, the shortest path `to` a node, or the path `through` one), each step with how it was reached, the calling line and the declaration; data written for later marked; a Mermaid diagram of the walk |
| `diagram(ids)` | A Mermaid sequence diagram of how execution reaches some functions or types and what they call |
| `impact(node_id, max_depth?, limit?, budget_tokens?)` | What can reach a node: callers by module and the flows through it |
| `annotate(node_id, key, value, evidence?, confidence?, layer?)` | Write an inferred or intent statement about a node |
| `propose_change(intent, targets, title?, depth?)` | Assess a change without an OpenSpec folder and save its blast-radius view |
| `save_view(title, narrative, marks, legend?)` | Save any set of marked nodes as a view |
| `review_change(change_id, before_run?, after_run?)` | Compare a change assessed with `propose_change` with what was done, and save a review view |
| `record_test_run(run, results)` | Store one test run under a label, for `review_change` |
| `add_rule(kind, selector_from, selector_to?, edge_kinds?, severity?, reason?, confirmed?)` | Add an architecture rule, suggested unless the user stated it |
| `check_rules()` | Evaluate every rule against the graph |
| `review_pr(base?, about?, github?, review_id?, path?)` | Review a checked-out branch or pull request with no spec: what changed, what it reaches and did not change, tests; returns `change_id` (`pr-<id>`), `blocking` (one line for each thing that holds up the merge) and `gate_passed` |
| `quick(what?, names?, done?, test_output?, test_results?, coverage_path?, change_id?)` | A small change with no spec folder: before (`what`, `names`), what it touches and the tests that run it; after (`done`), one verdict, and `grown` when it needs a spec |
| `spec_review_facts(change, reviewer?)`, `spec_finding(change, reviewer, severity, claim, evidence, proposal?)`, `spec_findings(change)`, `spec_resolve(finding_id, status, resolution?)` | Adversarial review of a planned change, or of a pull request by its `pr-<id>` |
| `learnings(retire?, why?, confirm?)` | Past decisions on review findings, kept from rejections with a reason, each marked `stale` when its code changed since; `retire` one the person says no longer holds, `confirm` one they say still holds for the code as it is now |
| `spec_brief(change, new_baseline?)`, `spec_verify(change, before_run?, after_run?)` | The steps inside `plan` and `check`, one at a time; rarely needed |
| `drift(path?, accept?)` | Code the living specs and finished changes name that is gone, moved, changed signature or ambiguous; `fails`, the page, `next` |
| `shared_state(scope?, limit?)` | Fields assigned from outside the type that declares them |
| `coupling(path?, min_together?, min_confidence?, limit?)` | Files (and folders) that usually change in the same commits as a file, from git history; with no path, the most coupled pairs |
| `coverage(node_id?, flow_id?, import_path?, test?)` | Measured coverage: what ran, set against the static paths; `import_path` reads a report in first, and `test` names the one test file that ran for a report with no per-test detail |
| `patterns(pattern?, node_id?, include_tests?, limit?)` | Design patterns found by shape, with roles, rationale and confidence; `include_tests` adds those inside test code |
| `label_pattern(pattern, roles, rationale, confidence?)` | Record a pattern the matchers missed |
| `tours()`, `tour(tour_id)` | List tours, or read one stop by stop |
| `save_tour(title, stops, audience?)` | Save a tour written for the user |
| `views()`, `view(view_id, limit?)` | List saved views, or read one |
| `skills(skill?)` | The skills that ship with Leyline, each with when to use it, or one skill's text. Each is also an MCP prompt of the same name |

`search` and `expand` rows, the steps of `explain_path`, the busiest functions, key types and key functions of
`module_outline`, and the `--` lines of `context` carry `summary`: what the function or type is for, from a
`responsibility` annotation (the intent layer first, stale ones passed over) or else the comment or docstring its
author wrote (`doc`, kept when the file is mapped). See [What a function or type is for](understanding.md#what-a-function-or-type-is-for).

`overview`, `expand`, `impact`, `module_outline`, `explain_path`, `find_flows` and `neighbors` also take
`budget_tokens` (600 to 6,000; a token is four characters): the answer is cut to the smaller of that and the tool's
usual size, `cut` says what was cut, and `more` names the budget. The budget is applied after the answer is worked
out, so it saves the agent's context, not time; `explain_path` and `module_outline` also walk fewer steps or levels
to fit it. `context` sizes its own outline with its `budget_tokens` and is not cut again; with `bodies` the outline takes a third of the budget, which is at least 600 tokens, and the code follows in the same text, with `bodies` describing each piece without repeating it.

`change` is a change folder or its id. A tool that cannot answer returns an error saying what to do
instead (no store yet: call `map`; an unknown id: find it with `search`). Each answer is at most about
24,000 characters (30,000 for `module_outline` and `explain_path`, 44,000 for `context`): lists show their
first items and their total, `cut` names any list that was shortened, and `more` says how to see the rest
(`limit`, `offset`, `scope`, or a narrower id).
