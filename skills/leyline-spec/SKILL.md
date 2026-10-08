---
name: leyline-spec
description: Turn a change someone wants into an OpenSpec change folder checked against a Leyline map, and verify it after it is implemented. Use when asked to plan, spec, scope or design a change to a mapped codebase, or to check that an implemented change matches its spec.
---

# Spec a change with Leyline

The person decides the design. You write it down in a form that can be checked, and Leyline checks
it against the code. The person should finish able to say three things in a sentence each: what
code will be written, what it will affect, and how they will know it was done.

For a change of a function or two and a constant, with no design to agree (a value, a one-line fix), skip the folder:
use `quick` (`leyline quick "<what>" --about <names>` before, `--done quick-<slug>` after), and write a spec only if its
answer says the change grew. The leyline-quick-change skill says how. Code someone else already wrote, with no spec, is
reviewed with the leyline-pr-review skill.

It works with the Leyline MCP server or with the `leyline` command alone. Without the server, use the
command for each tool this skill names:

| MCP tool | Command |
| --- | --- |
| `map` | `leyline map <repo>` |
| `plan` (with `test_output`) | `<test command> \| leyline plan <id> --tests -` (`--new-baseline` for `new_baseline`) |
| `check` (with `test_output`) | `<test command> \| leyline check <id> --tests -` |
| `overview`, `search`, `expand`, `impact`, `source` | `leyline overview`, `leyline search <text>`, `leyline expand <id>`, `leyline impact <name>`, `leyline source <id>` |
| `spec_review_facts` | `leyline spec facts <id> --reviewer logic` (or `performance`) |
| `spec_finding` | `leyline spec finding <id> --reviewer logic --severity medium --claim "..." --evidence <node id> --proposal "..."` |
| `spec_findings`, `spec_resolve` | `leyline spec findings <id>`, `leyline spec resolve <finding id> accepted\|rejected\|deferred "why"` |
| `check` (with `coverage_path`) | `... \| leyline check <id> --tests - --coverage .coverage` (per-test coverage: `pytest --cov --cov-context=test`) |
| `affected_tests` | `leyline affected-tests <id>`: the tests to run for the change, as a command |
| `drift` (`accept`) | `leyline drift` (`--accept`) |

Where this skill says a result field (`next`, `status.blocking`, `page`), the command prints the same
thing: the `Next:` line, the "Before implementation" list, and the plan itself. If neither the server nor
the command is there, say so and stop.

The path has three Leyline calls, the same three commands the person can type:

1. `map`, only if the code was never mapped (`overview` returns no repos).
2. You write the spec, then `plan`; review; the person decides; `plan` again until nothing blocks.
3. Someone implements the tasks, then `check`.

`plan` and `check` re-map changed code on their own and return `next`. Follow it, or tell the person
why not.

## Before any code: write the spec

1. **Hear the change.** Restate it in two sentences. If it could mean two designs, ask which.
2. **Look before writing.** `overview`, `search`, `expand`, `impact` on what the change touches (the
   leyline-ask skill, for a question about how the code works now).
   Tell the person anything that makes the change bigger or different than they said. Do this before
   drafting: a spec written first and checked second anchors on the wrong shape.
3. **Write the OpenSpec folder** at `openspec/changes/<kebab-id>/`:
   - `proposal.md`: `# Change: <title>`, then `## Why`, `## What Changes`, `## Impact`.
   - `specs/<capability>/spec.md`: `## ADDED Requirements` (or MODIFIED, REMOVED), each
     `### Requirement: <name>` with at least one `#### Scenario: <name>` (four `#`) and
     `- **WHEN** ...` / `- **THEN** ...` lines.
   - `tasks.md`: `- [ ] 1.1 <task>` lines.
   - `design.md` only when there is a real decision to record.
4. **Follow three conventions**, because the check depends on them:
   - Name code in backticks in tasks: `` `Vehicle.Speed` ``, `` `Queue.Push` ``. New code
     states its home: `` `Owner.NewName` `` for a member; `` `module.new_func` ``,
     `` `path/to/file.py: new_func` `` or `` `new_func` in `file.py` `` for a top-level function. A task
     that names no code (docs) is left for the person to check; it does not block.
   - Start each task with what it does: add, remove, rename, change the signature of. One task, one
     thing a person could tick. The code right after the verb (and any joined to it by "and" or a
     comma) is what the task changes; other code in the sentence, such as `` `SimConfig.QueueSpeed` `` in
     "counting vehicles slower than ...", is context the plan shows as "mentions" and does not count.
   - Name each scenario so a test can carry the same name. Add a task to write that test, quoting
     the scenario's name: `- [ ] 3.1 Add the test "<scenario name>"`.
   - A scenario that states an invariant ("for any amount, the balance is never negative"; always,
     never, for every) gets a property test, not one example: the plan marks it "invariant: a property
     test fits" and names the library. Name the test like the scenario. The shape, for "Balance is
     never negative":
     ```python
     @given(st.integers(min_value=0), st.integers())               # Hypothesis
     def test_balance_is_never_negative(balance, amount): assert apply(balance, amount) >= 0
     ```
     ```ts
     it("Balance is never negative", () =>                          // fast-check
       fc.assert(fc.property(fc.nat(), fc.integer(), (b, a) => apply(b, a) >= 0)));
     ```
     ```csharp
     [Property] public bool Balance_is_never_negative(NonNegativeInt b, int a) => Apply(b.Get, a) >= 0;   // FsCheck
     ```
     When it fails, `check` shows the shrunk counterexample: "contradicted: fails for amount=-1".
   - "Remove `X`" is done when `X` is gone and nothing still calls it; "Rename `X` to `Y`" when `X`
     is gone, `Y` is there and nothing still calls `X`. Name the callers' edits in tasks too.
5. **Record the tests as they pass now**, before anyone edits code: run the test suite and pass its
   output to `plan` as `test_output` (TAP from vitest or node --test, `pytest -rA`, or one PASS or FAIL
   line per test; several runners' output can go in one text), or the
   results as `test_results`. Without it, `check` cannot tell a test the change broke from one that
   already failed. If you cannot run the tests, say so and give the person the command
   (`<test command> | leyline plan <id> --tests -`).
6. **Call `plan`** with the folder. Read `status.blocking`. Fix the spec, not the tool: an ambiguous name
   gets its owner, a must-edit with no task gets a task or a sentence in the proposal saying why not,
   a scenario with no test gets a task. Call it again until `status.blocking` is empty or each
   remaining item is a choice the person made.
   Then read the lines under "Must agree with the change" (other ends of a channel the change crosses,
   such as a second program that reads the same pipe) and "Shares a caller or a field with the change,
   and no task names it". Each is code that shares a channel, a caller, a field or a look-alike new
   member with the change. For each, either propose a task (see "Proposing a missing task") or be able
   to say why it is right to leave alone. Do not pass this list to the person undigested.
   "Usually changes with the files the tasks touch" comes from git history, not the map: a doc, schema,
   fixture or test-case folder that changed in most past commits to a file the tasks touch. Treat it the
   same way: a task, or a reason it is not needed this time.
7. **Show the person `leyline.md`** (also returned as `page`). It is one page. Do not summarise it
   into something longer. Say which lines you are least sure of.
8. **Get it reviewed** (the `leyline-adversarial-review` skill, once for logic and once for
   performance) before anyone implements. The person decides each finding; then call `plan` again.

## After implementation: check

1. Run the tests and call `check` with the folder and their output (`test_output` or `test_results`).
   It re-maps the code itself.
2. Report `done_as_agreed` first. Then each task not done, each scenario not proven, and each edit
   outside the spec. For every edit outside the spec, read its source and say whether the spec was
   incomplete or the implementation wandered. The person decides which; do not decide for them. Propose
   the task, or the undo, as in "Proposing a missing task".
   Each task and scenario carries a `verdict` (proven, partial, contradicted, inconclusive, needs a
   person) and `verdict_why`; quote them as they are, and name every item that needs a person.
3. Pass on `next`.

A check that finds the change done as agreed records what each code name in it means now, in
`openspec/leyline-anchors.json`. Commit that file with the spec. Later, `drift` says which names in the
living specs and finished changes have gone, moved or changed signature; `plan` lists those the new
change touches under "Specs that no longer match code this change touches". Update each such spec with
the change. Call `drift` with `accept=true` only when the person says the specs and the code agree.

`spec_brief`, `spec_verify` and `record_test_run` are the steps inside `plan` and `check`, for when one
is needed alone. `plan` stores its test run as `before:spec-<id>` and `check` as `after:spec-<id>`.

## Proposing a missing task

When `check` lists an edit outside the spec, or `plan` lists a line under "Shares a caller or a field with the
change, and no task names it" that needs a task:

1. Draft the exact line to add to `tasks.md`, numbered after the tasks around it, in the conventions above: it
   starts with add, remove, rename or "change the signature of" when that is what it does, and names the code in
   backticks with its owner: `` - [ ] 2.4 Change the signature of `Queue.push` to take a priority ``.
2. Show the person the line and its evidence: the node, what was edited or what it shares with the change, and
   the source you read. For an edit outside the spec, also propose the other way out: undo the edit, and say what
   undoing it loses.
3. Add the line only when the person agrees. If they choose the undo, undo that edit and nothing else.
4. Call `plan` again, and `check` again once the code is written.

## OpenSpec's verify

If the project uses OpenSpec's expanded profile and `/opsx:verify` is there, you may run it after `check`. Its
findings are an LLM's opinion, not a verdict. File each CRITICAL or WARNING as a finding with `spec_finding`, so
it is tracked and the person resolves it like any other:

- reviewer `logic` (Leyline has only logic and performance);
- severity high for CRITICAL, medium for WARNING;
- the claim in one sentence, starting "/opsx:verify:";
- as evidence, the node ids it is about (`search`, `expand`). A finding needs at least one node on the map; one
  you cannot tie to a node goes in your report to the person instead, saying so.

The two verdicts are separate. Report `check`'s `done_as_agreed` as it is, whatever `/opsx:verify` says, and do
not drop a `/opsx:verify` finding because `check` passed.

## When the spec changes part-way

Edit the spec and call `plan` again. Once the code has changed it answers `baseline: kept`: the
picture of the code from the first plan stays, so `check` still compares with the code as it was
before any edit, and test output passed to `plan` then is refused as a baseline. Use `new_baseline`
only to abandon what was done and start over.

A new function that only code named in the spec calls is reported as a helper, not as an edit outside
the spec. Read it anyway.

## Rules

- Never tick a task or call a change done from the agent's own report. `check` reads the code.
- Never add a task or tick one without the person.
- A scenario with no test is unproven, however obvious it looks.
- Keep the spec short. If the brief does not fit on a page, the change is two changes.
- You do not resolve review findings. The person does.
