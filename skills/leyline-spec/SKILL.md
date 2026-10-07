---
name: leyline-spec
description: Turn a change someone wants into an OpenSpec change folder checked against a Leyline map, and verify it after it is implemented. Use when asked to plan, spec, scope or design a change to a mapped codebase, or to check that an implemented change matches its spec.
---

# Spec a change with Leyline

The person decides the design. You write it down in a form that can be checked, and Leyline checks
it against the code. The person should finish able to say three things in a sentence each: what
code will be written, what it will affect, and how they will know it was done. It needs the Leyline
MCP server connected. If `plan` is missing, say so and stop.

The path has three Leyline calls, the same three commands the person can type:

1. `map`, only if the code was never mapped (`overview` returns no repos).
2. You write the spec, then `plan`; review; the person decides; `plan` again until nothing blocks.
3. Someone implements the tasks, then `check`.

`plan` and `check` re-map changed code on their own and return `next`. Follow it, or tell the person
why not.

## Before any code: write the spec

1. **Hear the change.** Restate it in two sentences. If it could mean two designs, ask which.
2. **Look before writing.** `overview`, `search`, `expand`, `impact` on what the change touches.
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
   - Name code in backticks in tasks: `` `Vehicle.Speed` ``, `` `SignalController.Tick` ``. New code is
     `` `Owner.NewName` `` so its home is stated.
   - Start each task with what it does: add, remove, rename, change the signature of. One task, one
     thing a person could tick.
   - Name each scenario so a test can carry the same name. Add a task to write that test, quoting
     the scenario's name: `- [ ] 3.1 Add the test "<scenario name>"`.
5. **Record the tests as they pass now**, before anyone edits code: run the test suite and pass its
   output to `plan` as `test_output` (one PASS or FAIL line per test; `pytest -rA` prints that), or the
   results as `test_results`. Without it, `check` cannot tell a test the change broke from one that
   already failed. If you cannot run the tests, say so and give the person the command
   (`<test command> | leyline plan <id> --tests -`).
6. **Call `plan`** with the folder. Read `status.blocking`. Fix the spec, not the tool: an ambiguous name
   gets its owner, a must-edit with no task gets a task or a sentence in the proposal saying why not,
   a scenario with no test gets a task. Call it again until `status.blocking` is empty or each
   remaining item is a choice the person made.
   Then read the lines under "Uses the same things, and no task names it". Each is code that shares a
   caller, a field or a look-alike new member with the change. For each, either add a task or be able
   to say why it is right to leave alone. Do not pass this list to the person undigested.
7. **Show the person `leyline.md`** (also returned as `page`). It is one page. Do not summarise it
   into something longer. Say which lines you are least sure of.
8. **Get it reviewed** (the `leyline-adversarial-review` skill, once for logic and once for
   performance) before anyone implements. The person decides each finding; then call `plan` again.

## After implementation: check

1. Run the tests and call `check` with the folder and their output (`test_output` or `test_results`).
   It re-maps the code itself.
2. Report `done_as_agreed` first. Then each task not done, each scenario not proven, and each edit
   outside the spec. For every edit outside the spec, read its source and say whether the spec was
   incomplete or the implementation wandered. The person decides which; do not decide for them.
3. Pass on `next`.

`spec_brief`, `spec_verify` and `record_test_run` are the steps inside `plan` and `check`, for when one
is needed alone. `plan` stores its test run as `before:spec-<id>` and `check` as `after:spec-<id>`.

## When the spec changes part-way

Edit the spec and call `plan` again. Once the code has changed it answers `baseline: kept`: the
picture of the code from the first plan stays, so `check` still compares with the code as it was
before any edit, and test output passed to `plan` then is refused as a baseline. Use `new_baseline`
only to abandon what was done and start over.

A new function that only code named in the spec calls is reported as a helper, not as an edit outside
the spec. Read it anyway.

## Rules

- Never tick a task or call a change done from the agent's own report. `check` reads the code.
- A scenario with no test is unproven, however obvious it looks.
- Keep the spec short. If the brief does not fit on a page, the change is two changes.
- You do not resolve review findings. The person does.
