# Change: Every node carries a one-line summary, read by every understanding tool

## Why
The map knows structure (callers, callees, fields, flows) but not meaning. `show Indexer.run` lists 18 fields read
and never says what `run` does, though its author wrote a docstring. `find_flows` reads the leading comment or
docstring on the fly to rank candidates (`explain.leading_doc`, now `summaries.declared_doc`), then throws it away. Annotations an agent writes
with `annotate` are read back only by `expand` and the tour's module stop. An agent paying to read code should
leave a sentence the next one inherits, and the author's sentence should be there from the first map.

## What Changes
- At index time each callable and type gets `attrs.doc`: its leading comment or docstring, as `declared_doc` reads it
  from the file's text lines (decoded once per file in the worker). `doc` is left out of the incremental shape
  hash, like `loc` and `body_line`, so a comment-only edit re-parses its own file and nothing else.
- A new module `summaries.py` gives the summary of some nodes in one query: a `responsibility` annotation that is
  not stale (intent over inferred), else the node's `doc`, cut to its first sentence or 200 characters.
- `search` and `expand` add `summary` to their rows after `_brief`, with one summaries call per answer; the
  command line prints it. `context` prints it for the focus and its shown direct neighbours, within the budget.
  `module_outline` carries it on busiest functions, key types and key functions, and prints it. `explain_path`
  carries it on each step, cut to 120 characters, and prints it.
- `find_flows` reads `doc` from the store instead of the source, reads annotation text fresh on each call as a
  `note` field (weight 2.0, named "its note" in `why`), and multiplies a candidate's score by 1.5 when it matches
  two or more distinct description words in strong fields (name, route, owner, doc, note); the strong-field rule
  and the halving of tests and samples stay.

## Impact
The index writes one more attribute per callable and type (a full map on the next run, since `code_version`
changes). Answers grow by one short line per item; `context` keeps its budget, and `explain_path`'s server limit
is unchanged, so a long walk may show fewer steps. No tool's arguments change. `find_flows` reads the flows
table as before; `store.write_flows` is not touched. The cached explain index does not hold annotations, so
no cache is invalidated by `annotate`.
