## ADDED Requirements

### Requirement: An agent can say how much of an answer it can afford
#### Scenario: A read tool answers within the budget it was given
- **WHEN** `expand`, `overview`, `impact`, `module_outline`, `explain_path`, `find_flows` or `neighbors` is called
  with `budget_tokens` of 600 or more
- **THEN** the answer is at most four characters per token of that budget plus the `cut` and `more` fields, and
  never longer than the tool's default limit

#### Scenario: A budget below the default cuts lists and says so
- **WHEN** a read tool is called with a budget smaller than its answer
- **THEN** the answer's `cut` names each list or string that was shortened and `more` names `budget_tokens` and
  the tool's own knob

#### Scenario: Context keeps its own budget
- **WHEN** `context` is called with `budget_tokens`
- **THEN** its text is sized as before and its "Left out" line is kept

### Requirement: The tour command prints JSON when asked
#### Scenario: Tour prints JSON when asked
- **WHEN** `leyline tour --json` runs
- **THEN** it prints the whole tour as `tours.get` returns it
