# Leyline quick reference

One page: what to ask, and the command or tool behind it. The [README](../README.md) has the detail.

## Set up once

```bash
pip install leyline-code                  # Python 3.10+; adds `leyline`
claude mcp add leyline -- leyline serve   # connect Claude Code (add -s user for every project)
leyline skills install                    # copy the skills into .claude/skills
```

Then ask your agent in plain words. It maps the code when it needs to. Every command also works by
hand, and each one ends with a `Next:` line.

Languages: C#, Python, TypeScript and JavaScript by default; `pip install 'leyline-code[languages]'`
adds Go, Rust, Java, Kotlin, Swift, C, C++, Ruby, PHP, Scala, Lua, Bash and GDScript.

## The main loop

| Step | Command | You read |
| --- | --- | --- |
| Map the code | `leyline map [repo ...]` | A ten-line summary and `.leyline/map.html` |
| Plan a change | `<tests> \| leyline plan <id> --tests -` | `openspec/changes/<id>/leyline.md`: what will be written, what it affects, how you'll know it's done |
| Check it | `<tests> \| leyline check <id> --tests -` | The verdict, added to the same page. Exits 0 only when done as agreed |

`<tests>` is your test run, printing one pass or fail line per test: `pytest -rA`, vitest or
`node --test` (TAP), or similar. `<id>` is an OpenSpec change folder, `openspec/changes/<id>/`.

## By job

### Understand

| Job | Ask, for example | Skill | Command | MCP tool |
| --- | --- | --- | --- | --- |
| Answer a question, citing code | "Who calls `saveOne`, and is that certain?" | `leyline-ask` | `search`, `expand`, `impact` | `search`, `expand`, `impact`, `trace` |
| Explain a flow | "What happens when a user saves?" | `leyline-explain-flow` | `find-flows "<words>"`, `explain-path <start>` | `find_flows`, `explain_path`, `flow` |
| Get around a large module | "What's in `editor/host`?" | `leyline-explore-module` | `outline [module]`, `name-part` | `module_outline`, `name_part` |
| Onboard someone | "Write a tour of the payment code" | `leyline-tour` | `tour [id]` | `tours`, `tour`, `save_tour` |
| Context before editing | (the agent asks for this itself) | | `context <focus> --tokens N` | `context` |
| Find structural risk | "Which fields have no single owner?" | | `state`, `patterns`, `coupling [file]` | `shared_state`, `patterns`, `coupling` |
| Browse the map | | | `view`, `export -o map.html` | |

### Plan

| Job | Ask, for example | Skill | Command | MCP tool |
| --- | --- | --- | --- | --- |
| Assess a change's impact | "What would changing the validator's output affect?" | `leyline-change-impact` | `impact <name>` | `propose_change`, `impact` |
| Design it as a spec | "Plan adding an export button, with tests" | `leyline-spec` | `plan <id>` | `plan` |
| Make a small change | "Make the retry count 3" | `leyline-quick-change` | `quick "<what>" --about NAME`, then `quick --done quick-<slug>` | `quick` |
| Move a quick change up to a spec | (when the page says it has grown) | | `quick --to-spec <id> quick-<slug>` | `quick` |
| Pick the tests to run | | | `affected-tests <id>` | `affected_tests` |

### Review

| Job | Ask, for example | Skill | Command | MCP tool |
| --- | --- | --- | --- | --- |
| Review a plan before code | "Stress-test the plan for loud-engine" | `leyline-adversarial-review` | `spec facts <id> --reviewer logic` | `spec_review_facts` |
| Review a PR (no spec) | "Review pull request 123" | `leyline-pr-review` | `pr main --about "<description>"` or `pr --github 123` | `review_pr` |
| Re-review after new commits | "Review it again" | `leyline-pr-review` | `pr` again, with the same `--id` or `--github` | `review_pr` |
| Gate a merge in CI | | | `pr <base> --gate` (exits 1 while something blocks; set the kinds under `[pr] blocking` in `openspec/leyline.toml`) | `review_pr` (`gate_passed`, `blocking`) |
| File a finding | (reviewers do this) | | `spec finding <id> --reviewer logic --severity high --claim "..." --evidence <node> --proposal "..."` | `spec_finding` |
| List findings | | | `spec findings <id>` | `spec_findings` |
| Decide a finding | "Reject f-7fdda7: it's on purpose" | | `spec resolve <finding> accepted\|rejected\|deferred "why"` | `spec_resolve` |

Only you resolve a finding. A rejection with a reason becomes a learning.

### Check

| Job | Command | MCP tool |
| --- | --- | --- |
| Was the spec done as agreed? | `check <id> --tests FILE [--coverage .coverage]` | `check` |
| Was the quick change done? | `quick --done quick-<slug> --tests FILE` | `quick` |
| Restart from the code as it is now | `plan <id> --new-baseline` | `plan` |

### Over time

| Job | Command | MCP tool |
| --- | --- | --- |
| Past review decisions | `learnings`, `learnings retire <id> "why"` | `learnings` |
| Re-confirm a decision after its code changed | `learnings confirm <id>` | `learnings` (`confirm`) |
| Specs that no longer match the code | `drift [path]` | `drift` |
| Architecture rules (runs in CI) | `rules`, `rules --confirm <id>` | `add_rule`, `check_rules` |
| Import measured coverage | `coverage <file>` | `coverage` |

## Verdicts

| Verdict | Task | Scenario | Blocks by default |
| --- | --- | --- | --- |
| proven | the code it names changed | its test passes, and failed or didn't exist before, or passed both times while running the changed code | |
| partial | some of the named code changed | some of its results pass, some fail | yes |
| contradicted | the named code didn't change, but other code did | its test fails | yes |
| inconclusive | the map can't place the code, or nothing changed | no result recorded | yes |
| needs a person | it names no code | it passes, but doesn't reach the changed code | no |

A tick in `tasks.md` never counts. Change which verdicts block in `openspec/leyline.toml`:

```toml
[check]
blocking = ["contradicted", "inconclusive", "partial", "needs a person"]
```

## Writing a spec Leyline can read

- Name code in backticks in `tasks.md`: `` `Engine.start` ``. A name that isn't on the map is new code.
- Start a task with add, remove or rename, or "change the signature", to have it read that way.
  Anything else is read as a change in behavior.
- Give each scenario the same name as the test that proves it.
- A scenario that says "for any", "always" or "never" is marked as one a property test fits.

## Where things live

| Path | What | In git? |
| --- | --- | --- |
| `.leyline/leyline.db` | The map | no (Leyline writes `.leyline/.gitignore`) |
| `.leyline/map.html` | The map page | no |
| `.leyline/snapshots/` | The code as it was when each change was planned | no |
| `.leyline/reviews/pr-<id>.md` | PR review pages | no |
| `openspec/changes/<id>/leyline.md` | The page for a change | yes |
| `openspec/leyline-learnings.json` | Past review decisions | yes |
| `openspec/leyline.toml` | Project settings (blocking verdicts) | yes |
