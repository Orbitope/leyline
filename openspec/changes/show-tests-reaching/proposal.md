# Change: `show` and `expand` say which tests reach a function

## Why
Which test exercises a function is the strongest thing the map gives that grep cannot. `find_flows` and `impact`
show it; `show` and `expand`, the tools the leyline-ask skill reaches for first, do not. `show Indexer.run` says
"called by (2): grade, index" and nothing about the tests that reach it. Stored flows stop at depth 8 or 300
steps, so counting flow steps alone would say "no tests" for 82 functions in this repository that tests do reach.

## What Changes
- `query.reaching` walks the reverse call graph from a callable, as `impact` does, and gives the tests and the
  entry points that reach it: totals from the walk, the nearest first by their flow's depth where a stored flow
  passes the callable and by distance otherwise, each item with the test's or entry point's node id and its flow
  id when one exists. The flow steps table is read once.
- `expand` on a callable always carries `tests_reaching` and `entries_reaching`, with a total of 0 when none;
  when a measured run was imported it also carries `tests_measured`, from `coverage.tests_for`. Types are out of
  scope: `impact` answers for a type.
- `show` prints "tests that reach it on the map (N): …", "reached from (N): …" and, when present, "tests measured
  running it (N): …".
- The `expand` tool's description and the leyline-ask skill name the new fields.

## Impact
One reverse adjacency read per `expand` of a callable, the cost `impact` pays today, plus one read of the flow
steps for the callable. `export` and the viewer do not call `expand`.
