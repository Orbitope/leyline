# Change: The orientation tour and `map` point at named parts, not cluster anchors

## Why
On this repository the orientation tour's module stop reads "Clustering splits it into 11 groups: Generic +
generic.py + __init__.py group; Indexer + indexer.py + Run group…". That orients no one. Names an agent gives with
`name_part` survive re-maps and are shown by `module_outline`, but the tour ignores them, nothing prompts the first
agent to name parts, and the `map` MCP tool's `next` says only to write a spec, while the command's `Next:` says to
look around first.

## What Changes
- `indexer.index` computes the map's parts once, with `outline.map_parts`, after `inc.finish` has written the
  generation stamp, and hands that one result to the tour and to the page, and returns it under `parts`. The tour's
  module stops are written from it, after `inc.finish`. The page builds no second model.
- The module stop of a module of 30 files or more (the size the map page splits) lists its named parts with their
  summaries, in the outline's order and with its stale marks, in place of the cluster groups; it says how many
  parts are unnamed and names the leyline-explore-module skill. Smaller modules keep their stop as it is.
- `map` says, by command and by tool, when the largest product module of 30 files or more has unnamed parts: the
  count comes from the `parts` the index returned, in `loop.counts`, and `loop.map_text` appends the sentence to
  whichever `Next:` line it writes. The tool's `next` keeps the spec step first and adds the orientation steps
  (`tour`, `find_flows`, `module_outline`, naming parts) after it.
- `name_part`'s `next` says the tour shows the name after the next map.
- The leyline-ask skill gets a row for `tours`, `tour` and `leyline tour`, and a route for "what is this codebase,
  how is it organized".

## Impact
`tours.generate` runs on every map; it reads the parts the index computed and builds no model of its own, so a
map does the parts work once instead of once for the page and again for the tour. `outline.named_parts` is not
changed, so `overview` pays nothing new. Two existing tests change with the text: the cluster-group assertion in
`test_two_systems_whose_anchors_share_a_name_are_named_apart` and the `next[0]` assertion in
`test_the_loop_over_mcp`.
