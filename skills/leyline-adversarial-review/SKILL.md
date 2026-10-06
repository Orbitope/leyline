---
name: leyline-adversarial-review
description: Attack a spec before it is implemented, as a logic reviewer or a performance reviewer, using a Leyline map as evidence. Use after a change brief exists and before any code is written, or when asked to review, challenge or stress-test a proposed change.
---

# Review a spec adversarially with Leyline

Your job is to find what is wrong with a change before it costs anything. You are not the author
and you are not trying to help it pass. Run as a fresh agent that has not seen the spec being
written. Do one review per run: logic or performance. It needs the Leyline MCP server connected.

## Steps

1. Read the change folder: `proposal.md`, the spec deltas, `tasks.md`, `leyline.md`.
2. Call `spec_review_facts` with the folder. It returns what the graph knows, arranged as the
   questions below.
3. For every item in your section, read the code (`source`, `expand`, `flow`) before deciding. A
   fact from the graph is a lead, not a finding.
4. File each real problem with `spec_finding`: one sentence a person can check, a severity, the node
   ids that show it, and the change to the spec you propose. No node, no finding.
5. Report the count by severity and the one finding you would fix first. Stop there.

## Logic reviewer: answer each of these

- Which callers, implementers or overriders must change and have no task?
- Which channel does the change cross (process, HTTP, file, event), and does the spec say what the
  other end must do?
- Which shared field gains a writer, or changes meaning for its existing readers?
- Which state do the changed functions use that other, unchanged functions also use? If the change
  adds a parallel structure (a second queue, a second flag), does every reader of the first one learn
  about the second?
- For each scenario: would its test still pass if the task it is meant to prove were left out?
- Which scenario has no test? Which requirement has no scenario?
- What do the scenarios leave out: the error path, the empty case, ordering, the second caller,
  what happens to data written before the change?
- Which existing rule or pattern does the design break, and is that said?
- Does any task contradict another, or the proposal?

## Performance reviewer: answer each of these

- Which changed functions do the most flows pass through, and which sit inside the main loop? Read
  the callers to confirm: the count of flows is a proxy.
- Does the change add work per call, per item or per tick on those functions?
- Does it add allocation, I/O, serialisation, a lock or a process hop on a hot path?
- Does it grow a message or a structure that crosses a channel on every step?
- What is the measured baseline, and which test would show a regression? If none exists, that is a
  finding.

## Severity

- **high**: the change will be wrong, or will break something the spec does not mention, if
  implemented as written.
- **medium**: a gap that will probably cost a second round.
- **low**: worth a sentence in the spec.

## Rules

- Do not rewrite the spec. Propose the change in the finding and let the person decide.
- Do not file style or naming preferences.
- If the facts are empty and you found nothing after reading the code, say so in one line. A review
  that finds nothing is a valid result.
- Say what you could not check and why.
