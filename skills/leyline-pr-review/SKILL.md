---
name: leyline-pr-review
description: Review a pull request or branch someone else wrote, with no spec, using a Leyline map. Maps what changed and what it reaches, runs an adversarial logic and performance review, and gives the person a short summary with the one finding to fix first. Use when asked to review a pull request, a branch or someone's changes, or to review one again after new commits.
---

# Review a pull request with Leyline

The code exists; what it was meant to do is a title and a paragraph. Leyline reads the change from the code.
Your job is to tell the person what it does, what it breaks or leaves undone, and what to fix first.

It works with the Leyline MCP server or with the `leyline` command alone:

| MCP tool | Command |
| --- | --- |
| `review_pr` (`base`, `about`) | `leyline pr <base> --about "<title and description>"` |
| `review_pr` (`github`) | `leyline pr --github <number>` |
| `spec_review_facts` | `leyline spec facts pr-<id> --reviewer logic` (or `performance`) |
| `spec_finding` | `leyline spec finding pr-<id> --reviewer logic --severity high --claim "..." --evidence <node id> --proposal "..."` |
| `spec_findings` | `leyline spec findings pr-<id>` |
| `affected_tests` | `leyline affected-tests pr-<id>` |
| `source`, `expand`, `impact` | `leyline source <id>`, `leyline expand <id>`, `leyline impact <name>` |

If neither is there, say so and stop.

## Steps

1. **Check the branch out** (`gh pr checkout <number>`, or `git switch <branch>`). Leyline reads the working
   tree, so review the checkout, not a diff pasted in.
2. **Read what it was meant to do.** `gh pr view <number> --json title,body,closingIssuesReferences`, then
   `gh issue view <m> --json title,body` for each linked issue. If `gh` is not there or no issue is linked, say so
   and go on with the title and description alone.
3. **Call `review_pr`** with `base` (the branch it merges into) and `about`: the title and description, then each
   linked issue's title and body (`--about-file` for long text). With `github`, still pass `about` when there are
   linked issues. Keep the `change_id` (`pr-<id>`). Read the page, and the files under
   `house_rules` before anything else: a pull request breaks the repository's own rules more often than the code.
4. **Run the leyline-adversarial-review skill** on `pr-<id>` twice: once as the logic reviewer, once as the
   performance reviewer. Fresh agents are best; if you cannot start one, do two separate passes and say so.
   Each files its findings with `spec_finding`.
5. **Hold the code against each linked issue.** Decide from the page's "What changed" and the code, not from the
   description, whether it addresses the issue. Where the code and an issue disagree, file a logic finding with
   `spec_finding`, the issue's number in the claim.
6. **Call `spec_findings`** with `pr-<id>`: everything filed, with ids.
7. **Summarize for the person**, in this order, on one screen:
   - **What changed**: two sentences, from the page's "What changed", not from the description.
   - **Linked issues**: one line each: addressed, partly or not, citing "What changed". Or say there were none, or
     that `gh` was not there.
   - **What it breaks or leaves**: callers of a changed signature left alone, removed code still called, other
     ends of a channel not edited, changed code no test runs. One line each, with the node.
   - **Findings**: high, then medium, then low; one line each, with its id and its evidence.
   - **Fix first**: the one finding to fix first, and why.
   - **Not checked**: what you could not check, and why.
8. Point to the page (`written`) for the whole review. Ask the person to decide each finding; record a decision
   they state with `spec_resolve`.

## Review it again after new commits

Check the new commits out and call `review_pr` again, with the same `review_id` or `github` number. The page
opens with "Since the last review", and the facts carry `since_last_review`:

1. Start there: the functions edited since the last head, and its `new_facts`.
2. Re-check each finding under `may_be_fixed` in the new code, and tell the person which the new commits fix.
3. Do not file again what is under `still_applies`.
4. Review only the new code adversarially. Open the summary with what changed since the last review.

## Rules

- Do not push to the author's branch or edit their code. Propose; the person decides.
- Do not resolve a finding on your own judgment.
- Describe the change from the code. Where the description, or a linked issue, and the code disagree, that is a finding.
