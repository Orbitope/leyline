# Change: An outline splits a group that holds most of a level

## Why
`leyline outline src/leyline` puts 25 of 47 files and 16,115 of 28,708 loose-file lines into one group named
"spec.py + diff.py + quick.py group". Half the module in one part is not a split, and the label suggests three files.

## What Changes
- A group holding more than 40% of the lines of the level's loose files and at least 6 files is split once more,
  with the same two-step detection `_groups` uses (community detection over its own units, then over its units
  with the code they use) in a new `outline._resplit`. The pieces take the group's place at the level; files no
  community takes stay together under the group's own id and label, so a name given to the group stays with
  them. A re-split accepts a weaker split than the first (modularity 0.2, not 0.3): a group the first split
  chose is a weaker split by nature, and on this repository its best split scores 0.26. A group that gives fewer
  than two pieces stays whole. One round only: at most two more community runs per group, and a piece above 40%
  is left as it is.
- `outline.names` gives the name of an old group whose id is gone to the group sharing most of its files (at least
  a quarter of them), by one rule for a moved anchor and a split, and `_stale` marks it when less than half was kept.
- A group's label counts the files beyond the three it names and keeps its ending: "spec.py + diff.py + quick.py
  + 22 more group".
- The level's cap of 12 parts still applies; the pieces count toward it and the smallest parts may move behind
  `@more` as today.

## Impact
`outline._groups` may run community detection up to two more times per flat level, bounded by the one-round rule.
Part ids of the pieces are new; the leftover piece keeps the old id. The tests that assert a label ends with
" group" still pass.
