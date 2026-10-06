---
name: leyline-spec
description: Turn a change someone wants into an OpenSpec change folder checked against a Leyline map, and verify it after it is implemented. Use when asked to plan, spec, scope or design a change to a mapped codebase, or to check that an implemented change matches its spec.
---

# Spec a change with Leyline

The person decides the design. You write it down in a form that can be checked, and Leyline checks
it against the code. The person should finish able to say three things in a sentence each: what
code will be written, what it will affect, and how they will know it was done. It needs the Leyline
MCP server connected. If `spec_brief` is missing, say so and stop.

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
5. **Call `spec_brief`** with the folder. Read `gaps`. Fix the spec, not the tool: an ambiguous name
   gets its owner, a must-edit with no task gets a task or a sentence in the proposal saying why not,
   a scenario with no test gets a task. Call it again until `gaps` is empty or each remaining gap is
   a choice the person made.
6. **Show the person `leyline.md`.** It is one page. Do not summarise it into something longer. Say
   which lines you are least sure of.
7. **Get it reviewed** (the `leyline-adversarial-review` skill) before anyone implements.

## After implementation: verify

1. Record the tests: `record_test_run` with run `before` (before the first edit) and `after`.
2. Ask for a re-index.
3. Call `spec_verify` with the folder and the two run labels.
4. Report `done_as_agreed` first. Then each task not done, each scenario not proven, and each edit
   outside the spec. For every edit outside the spec, read its source and say whether the spec was
   incomplete or the implementation wandered. The person decides which; do not decide for them.

## Rules

- Never tick a task or call a change done from the agent's own report. `spec_verify` reads the code.
- A scenario with no test is unproven, however obvious it looks.
- Keep the spec short. If the brief does not fit on a page, the change is two changes.
- You do not resolve review findings. The person does.
