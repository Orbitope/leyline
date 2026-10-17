# Change: Say when a scenario's own test would pass whatever the change did

## Why
`check` proves a scenario by its test passing and reaching the changed code, and measured per-test coverage tightens
that to "the test ran the changed code". A test can run every changed line and assert nothing about them: it passes
whatever the change did. Agents write such tests readily, and coverage cannot see it. A mutation can: break a changed
line on purpose, run the tests, and see whether the scenario's own test fails.

## What Changes
- `leyline mutants <change>` (the `mutants` tool) writes a plan of small breaks for a spec change, and for nothing
  else (a `pr-<id>` or a quick change is refused, with the reason). It makes mutants of the lines whose text is new in
  each function the change edited, in Python product code: a moved or reformatted function, whose lines already stood
  in the baseline, gets none, and tests get none. The breaks are a comparison flipped, `and` turned into `or`, a `not`
  dropped, a returned value replaced with None, an integer one more, `+` turned into `-`. Each is made at the operator's
  own place in the source and must still parse. At most 30 per change and 3 to a line, chosen one to each edited
  function first, then the next to each, so no function is left out while another has three; functions left without one
  are named. Other languages are named in the plan as having no operators.
- The plan is a folder, `.leyline/mutants/<change>/`: `plan.json` (the mutants, each with its file, line, operator,
  before and after, function, and its tests in two stages: the tests of the scenarios that reach the function, and at
  most 40 other tests that reach it; and a plan id that is a hash of the mutants and of the content of each file they
  mutate), a patch per mutant (in the file's own line endings, relative to its repository's root, for `git apply`), and
  `run.py`, a small program using only the standard library. Leyline writes nothing else and never runs any of it.
- `python .leyline/mutants/<change>/run.py` is what the person or the agent runs. It runs the tests unmutated first
  (what fails there is listed and never counts as a kill), then for each mutant applies its patch, runs the first stage
  in full (so that every scenario's own tests are recorded) and, only if nothing failed, the second with the runner's
  fail-fast option, under a timeout of twice the unmutated run (at least 30 seconds; `LEYLINE_MUTANT_TIMEOUT` sets it),
  and always undoes the patch, on an error, a timeout, an interrupt or the end. It appends one
  line per mutant to `results.jsonl`: the plan id, the mutant, `killed`, `survived`, `timeout` (counted as killed) or
  `error`, which tests failed, and how many of the tests it ran. It may be stopped at any time: a partial file is read.
- `leyline check <change> --mutants results.jsonl` reads it. A file made for another plan, or for code that has changed
  since the plan (so a patch left applied is caught too), is refused, with the reason. For each scenario that passes and
  whose test reaches a changed function with mutants, every one run: the scenario is `unasserted` when none of those
  mutants failed the scenario's own test; a test that fails them keeps its verdict, whoever else failed them. A mutant
  not run leaves the scenario unjudged, and says so. A person may mark a mutant `equivalent`, with a reason, in the file:
  it counts for nothing. Where there are no mutants, the scenario is not unasserted.
- `unasserted` is not blocking by default. It is shown in the verdict counts, on the page under "Covered, but not
  asserted" with each survivor's file, line, change and tests, and in `next` ("strengthen the test, run the plan again").
  A project may list it under `[check] blocking`, and then `check` exits 1 while one is left. Without `--mutants`, the
  page is what it was.

- A change can name the links it adds between modules. `check` listed any new link between modules (even to the
  standard library's `ast`, which this change cannot do without) as holding the verdict up, and told the author to "keep
  them in the spec" with nothing that did. A new link whose target the change names in backticks, in its proposal,
  design, tasks or specs, was planned: it is listed apart, and does not hold the verdict up. One it does not name does.

## Impact
New module for the plan, the runner and the results. `check` gains one optional argument and the verdicts one name. The
plan reads the snapshot's rows of the edited files only. `run.py` runs the repository's tests, as running them
by hand would. `docs/mcp-tools.md` lists every tool and its arguments, and a test checks it.
