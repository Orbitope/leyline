---
name: leyline-adversarial-review
description: Attack a change as a logic reviewer or a performance reviewer, using a Leyline map as evidence. Use on a spec after its brief exists and before any code is written, or on a pull request or branch someone else wrote (no spec needed), or when asked to review, challenge or stress-test a change.
---

# Review a change adversarially with Leyline

Two kinds of change can be reviewed: a **spec** (an OpenSpec folder, before any code is written) and a **pull
request** (code someone already wrote, with no spec; see the section below). Your job is to find what is
wrong with a change before it costs anything. You are not the author
and you are not trying to help it pass. Run as a fresh agent that has not seen the spec being
written. If you cannot start one, do the review as a separate pass: put aside what you know of how
the spec was written, re-read only the change folder and `leyline.md`, and say in your report that
the review was not done by a fresh agent. Do one review per run: logic or performance.

It works with the Leyline MCP server or with the `leyline` command alone. Without the server:
`spec_review_facts` is `leyline spec facts <id> --reviewer logic` (or `performance`), which prints
the same facts as JSON and records that the review ran; `spec_finding` is `leyline spec finding <id>
--reviewer logic --severity medium --claim "..." --evidence <node id> ... --proposal "..."`;
`source`, `expand`, `search` and `impact` are `leyline source <id>`, `leyline expand <id>`,
`leyline search <text>` and `leyline impact <name>`. If neither is there, say so and stop.

## Steps

1. Read the change folder: `proposal.md`, the spec deltas, `tasks.md`, `leyline.md`. For a pull request,
   read its page (`.leyline/reviews/pr-<id>.md`) and the diff.
2. Call `spec_review_facts` with the folder and `reviewer` (logic or performance). It returns what
   the graph knows, arranged as the questions below, and records that your review ran, so a review
   that files nothing still shows on the plan.
   - Read `learnings_that_apply` first: past findings on this code that a person rejected, with their reason.
     A finding you file that repeats one comes back with `learned`; it is kept and marked on the page.
     A learning marked `stale` is about code that has changed since the decision (`edited` and `gone` name
     the nodes). It is a question for the person, not an answer: read the new code, and file what you find
     as usual. Do not drop a finding because a stale learning matches it, and do not refile one to get
     around it. Say in your report which stale learnings you met, so the person can confirm or retire them.
     A learning whose `code` is unknown was kept before Leyline recorded its code: treat it as current.
3. For every item in your section, read the code (`source`, `expand`, `flow`, or the file itself)
   before deciding. A fact from the graph is a lead, not a finding.
4. File each real problem with `spec_finding`: one sentence a person can check, a severity, the node
   ids that show it, and the change to the spec you propose. No node, no finding.
5. Report the count by severity and the one finding you would fix first. Stop there.

## A pull request: set it up first

When the person asked for the whole review of a pull request, follow the leyline-pr-review skill: it runs this
one twice and writes the summary. Alone, this skill is the attack step.

1. Check the branch out (`gh pr checkout <number>`), then call `review_pr` with `base` (the branch it merges
   into) and `about` (its title and description, and each linked issue's title and body), or `github` with the
   number; without the server, `leyline pr <base> --about "..."` or `leyline pr --github <number>`. It returns the review's id,
   `pr-<id>`, and a page: what changed, what it reaches and did not change, its tests.
2. Use `pr-<id>` wherever the steps below say the change folder: `spec_review_facts` with it gives the facts
   (`leyline spec facts pr-<id> --reviewer logic`), and `spec_finding` files against it. Read the files under
   `house_rules_to_read_first` before anything else: a pull request breaks the repository's own rules more
   often than it breaks the code.
3. In the questions below, read "the spec" as "the description". The first logic question for a pull request
   is whether the code does what the description says, all of it and nothing else: name each edit the
   description does not explain, and each thing it promises that no edit does. `proposal` in a finding is the
   change to the code (or the description) you propose.
4. The facts for a pull request use the same questions under names without "task": `other_ends_of_those_channels_not_edited`,
   `callers_of_changed_functions_left_alone`, `state_shared_with_unchanged_code`; and add
   `signature_changed_callers_not_edited`, `removed_but_still_called` and `tests.likely_to_fail_unedited`.
5. A finding whose evidence is not on the change's blast radius comes back with a warning. Either add the node
   that links it to the change, or ask yourself whether you wandered.
6. On a re-review (new commits since the last one; the facts' `since_last_review` names the previous head), start
   from `since_last_review`: read the code changed since then and its `new_facts` first, re-check each finding under
   `may_be_fixed` and tell the person which ones the new code fixed, and do not refile findings under `still_applies`.

## Logic reviewer: answer each of these

- Which callers, implementers or overriders must change and have no task? (A pull request:
  `signature_changed_callers_not_edited` and `removed_but_still_called`. Each is a likely break; confirm it
  in the code.)
- Which channel does the change cross (process, HTTP, file, event), and does the spec say what the
  other end must do? `other_ends_of_those_channels_no_task_names` lists the other launchers, callers or
  readers of the same end: does each one need the change too?
- Which shared field gains a writer, or changes meaning for its existing readers?
- Which new member is named like one its type already has (`new_members_named_like_existing_ones`)?
  Read every user of the existing one: does it need the new one too?
- Who else calls each changed function (`callers_of_changed_functions_the_spec_leaves_alone`)? Does
  the change alter what those callers get or can assume?
- Which state do the changed functions use that other, unchanged functions also use?
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

- Do not rewrite the spec, or push to someone else's branch. Propose the change in the finding and let the
  person decide.
- Do not file style or naming preferences.
- Do not refile what a learning already settled, unless the code changed in a way its reason did not cover.
  Then say in the claim what changed.
- A finding whose claim starts "/opsx:verify:" was filed from OpenSpec's verify (the leyline-spec skill says how).
  It is an LLM's opinion: read the code behind it like any other lead, and do not file it again. It does not
  change `check`'s verdict, and `check` passing does not settle it.
- A stale learning settles nothing on its own. Keep the finding it matches; the page shows the person that
  the code changed, and they decide (`learnings` with `confirm` or `retire`, only on their word).
- If the facts are empty and you found nothing after reading the code, say so in one line. A review
  that finds nothing is a valid result.
- Say what you could not check and why.
