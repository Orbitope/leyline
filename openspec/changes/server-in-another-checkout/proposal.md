# Change: An agent working in another checkout of the server's repository can map it

## Why
The desktop app starts `leyline serve` in the main checkout while the agent works in a worktree under
`.claude/worktrees/`. `serve` sets `LEYLINE_DB` from its own directory, and `map` folds any directory under the
main checkout onto the main repository, so `map` with the worktree's path maps the main tree, and the "nothing is
mapped" error tells the agent to do just that. The server cannot see the agent's working directory; it can say
where it is, stop folding a checkout onto the one that contains it, and read the store of the checkout it mapped.

## What Changes
- A directory that is itself a git top level (a linked worktree) is not folded onto the repository that contains
  it: `map_code`'s fold skips it.
- The server keeps one current store per process. It starts as `LEYLINE_DB` or the default. When `map` maps a
  checkout that is not the server's own, the store is `<checkout>/.leyline/leyline.db`, and `_path` returns it
  only after `map_repos` has returned without error, inside the lock beside the generation bump; a refused store
  (a tracked `.leyline`) leaves the old store current. A later `map` of another checkout moves it again, and the
  answer names the current store; one server process has one current store, and after a restart `serve` finds
  the store from its own directory as before, which the `map` answer says.
- `_outside` allows the store's roots, the server's directory and its repository, and that repository's linked
  worktrees, listed once at start by `loop.worktrees_of` and again on each `map`, never per tool call.
- The default paths of `review_pr` and `drift`, and the search order of `loop.find_change`, use the store's
  first root before the server's directory, so after a move they read the mapped checkout.
- The "nothing is mapped" error and `map`'s answer say that an agent working in another checkout passes that
  checkout's absolute path to `map`, in those words.
- The leyline-ask and leyline-explain-flow skills say the same.

## Impact
`_path` becomes a value `map` can set for the process. `_db` already reopens a thread's connection when the store
path changes. No git subprocess runs per tool call.
