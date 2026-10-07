---
name: leyline-change-impact
description: Assess a described code change against a Leyline map and save a blast-radius view, review an implemented change against its proposal, or save any custom view of the code. Use when asked what a change will touch, break or need tested, or whether a change went as planned.
---

# Assess a change with Leyline

Use this when the user describes a change (a spec, a ticket, a sentence) and wants to know what it
touches before any code is written, or asks for a picture of some slice of the codebase. It needs the
Leyline MCP server connected and the repository indexed. If the `overview` tool is missing, say so and
stop: do not guess the impact from memory.

A question about the code with no change in mind goes to the leyline-ask skill. A change to plan and check with a
spec is the leyline-spec skill; a one-line fix, leyline-quick-change.

Leyline computes reach; you decide which nodes the words refer to. Keep those two jobs separate and
tell the user which is which.

## Steps

1. **Read the change.** Restate it in one sentence. If it could mean two different things in this
   codebase, ask before going on.
2. **Orient.** Call `overview` once. Note the modules, the systems and which extractors did not run:
   anything marked not analyzed is a blind spot for this assessment.
3. **Find the targets.** For each thing the change mentions, use `search`, then `expand` to confirm
   it is the right node. Read `source` when the name alone does not settle it. Prefer functions over
   whole types: a type target counts every function inside it and inflates the result.
4. **Choose an action for each target.**
   - `behavior`: same parameters and return type, different result or side effect
   - `signature`: parameters or return type change
   - `rename`, `remove`
   - `add`: something new. Give its `name`, the `parent` it goes in, what it `uses` and what is `used_by` it.
5. **Look across channels yourself.** If a target sends or receives data over a process pipe, an
   event, a file or the network, the code on the other side has no link to it in the graph. Find the
   reader or writer on the far side (`search`, `source`) and add it as a target with a note that says
   what it must do. Leyline flags that a channel is involved; it cannot tell you which function
   parses the bytes.
6. **Call `propose_change`** with the user's intent in their words, a short title and the targets.
   Give every target a `note` saying what changes there.
7. **Read the result before reporting.** Check that `must_edit` makes sense. If it is empty for a
   breaking change, or huge for a small one, a target is probably wrong: fix it and call again.
   Follow up with `impact` or `flow` on anything surprising.
8. **Report** in this order: what must be edited, the risks as flagged, the tests to run, what no
   test covers, and what Leyline could not see. Tell the user the view is saved under the title you
   gave it and appears in the map's Views tab after a refresh.

## After the change is implemented

Do this when the user asks whether a change went as planned, or when you implemented it yourself.

1. Before editing, run the tests and call `record_test_run` with run `before`. If the edits are
   already made, skip this and say the review has no test baseline.
2. After the edits, run the tests again and record run `after`. Ask for the repository to be
   re-indexed (`leyline index`); the review compares stores, not files.
3. Call `review_change` with the change id and the two run labels.
4. Report the findings first. Then, for each edit that was not predicted, read its `source` and say
   whether it was a needed follow-on the proposal missed or drift outside the change. For each
   predicted edit that did not happen, say whether the prediction was wrong or the work is unfinished.
5. A new link between modules or a newly broken rule is a design decision: put it to the user, do
   not wave it through.

To record a design constraint, call `add_rule`. Pass `confirmed=true` only when the user stated the
rule themselves; a rule you inferred from the code or the docs is a suggestion, with your `reason`.

## Custom views

When the user wants to see something that is not a change (how a feature works end to end, two
design options side by side, everything that touches one concept), collect the node ids and call
`save_view`. Group marks with short `role` labels, give each a one-line `note`, describe the roles in
`legend`, and explain the view in `narrative`. To compare design options, save one view per option
with the same role names so they read alike.

## Rules

- Never present the reach as proof the change is safe. It is static: it shows what can be affected,
  not whether behavior stays correct.
- Say when a link in the result is marked guessed.
- Do not write annotations or views about code you did not look at.
- After the change is implemented, ask for the repository to be re-indexed before assessing again.
