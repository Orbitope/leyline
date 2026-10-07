---
name: leyline-quick-change
description: Make a small code change (a constant, one function and its caller) on a mapped codebase with no spec folder, and have Leyline say whether it was done. Use when asked for a one-line fix, a changed value or a small tweak, and to decide when a change has grown and needs a spec.
---

# Make a quick change with Leyline

`quick` gives a small change the same answers as a spec, with no folder: what it will touch, what it reaches,
and whether it was done. Three steps: `quick` before, the edit, `quick` with `done` after.

It works with the Leyline MCP server or with the `leyline` command alone:

| MCP tool | Command |
| --- | --- |
| `quick` (`what`, `names`, `test_output`) | `<test command> \| leyline quick "<what>" --about <names> --tests -` |
| `quick` (`done`, `test_output`) | `<test command> \| leyline quick --done quick-<slug> --tests -` |
| `quick` (`done`, `names`) | `leyline quick --done quick-<slug> --about <name>` |
| `search`, `expand`, `source` | `leyline search <text>`, `leyline expand <id>`, `leyline source <id>` |
| `affected_tests` | `leyline affected-tests quick-<slug>` |
| none | `leyline quick --to-spec <id> quick-<slug>` |

If neither is there, say so and stop.

## Is it quick?

Quick fits a value or one or two functions, with no design to agree. Use the leyline-spec skill instead when:

- the person has to choose between designs, or agree what "done" means;
- it adds a feature, a public API or a data format;
- it changes what crosses a channel (a route, an event, a queue, a table, a file, a program's input): both ends
  must agree.

## Steps

1. **Find the code.** `search`, then `expand` to confirm. Use names as the code writes them: `Owner.method`,
   `module.func`, a constant.
2. **Before the edit.** Run the tests. Call `quick` with `what` (one sentence), `names` and `test_output`. If
   you cannot run the tests, say so: without them `done` cannot tell a test the change broke from one that
   already failed.
3. **Read the answer:** what it will touch, what must be edited with it, the channels, the tests that run it.
   Tell the person anything bigger than they said. If `grown` is not empty, stop and escalate (below).
4. **Edit** the named code, and what the answer says must be edited with it. Nothing else.
5. **After the edit.** Run the tests (`affected_tests` names the ones to run), then call `quick` with `done` (the
   `change_id`) and `test_output`.
6. **Report the verdict as it comes:** `done`, then each item with its verdict and why. An edit that belonged
   but was not named: call `quick` again with `done` and `names` to add it. Do not leave it out of the report.
7. If `grown` is not empty now, escalate.

## Escalate to a spec

`grown` says why: too many pieces of code, callers that must change, or a channel crossed. Then:

1. Tell the person why the change grew, in a sentence.
2. Write `openspec/changes/<id>/` with the leyline-spec skill.
3. Run `leyline quick --to-spec <id> quick-<slug>`: the spec starts from this baseline and the tests from before.
4. Go on with `plan` and `check`, as leyline-spec says.

## Rules

- Never call the change done from your own account. `quick` with `done` reads the code.
- Do not widen the edit to tidy nearby code. That is a second change.
- For a review, use the change id as a spec's: `spec_review_facts` with `quick-<slug>`. The person resolves
  findings, not you.
