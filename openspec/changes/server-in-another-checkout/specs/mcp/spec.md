## ADDED Requirements

### Requirement: The server maps the checkout the agent works in
#### Scenario: A linked worktree under the main checkout is mapped as itself
- **WHEN** the server starts in a git repository with a linked worktree under it, and `map` is called with the
  worktree's absolute path
- **THEN** the store's root is the worktree, not the repository that contains it

#### Scenario: Mapping another checkout moves the store to it
- **WHEN** `map` maps a checkout that is not the directory the server started in and the map succeeds
- **THEN** the store is at `<checkout>/.leyline/leyline.db`, later tools read it, the answer names it and says the
  server reads it until it restarts

#### Scenario: A refused store leaves the old one current
- **WHEN** `map` maps a checkout whose `.leyline` is tracked by git
- **THEN** the map is refused with the files named and later tools still read the store they read before

#### Scenario: After the move the paths the server defaults to follow the store
- **WHEN** the store has moved and `review_pr` or `drift` is called with no path, or a change id exists in both
  checkouts
- **THEN** each reads the mapped checkout

#### Scenario: Accepting a worktree runs no git per call
- **WHEN** a tool is called with a path inside a mapped repository
- **THEN** `loop.worktrees_of` is not called

#### Scenario: The nothing-mapped error names the directory the server started in
- **WHEN** a tool is called before anything is mapped
- **THEN** the error names the server's directory and says "an agent working in another checkout passes that
  checkout's absolute path to `map`"
