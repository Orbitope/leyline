## ADDED Requirements

### Requirement: One function's detail says who exercises it
#### Scenario: Expand says which tests reach a function
- **WHEN** `expand` is called on a callable that tests reach through calls, some through flows the walk cut short
- **THEN** the result has `tests_reaching` with the total from the reverse call graph and the nearest tests, each
  with the test's node id, and `entries_reaching` with the entry points that reach it

#### Scenario: A function no tests reach says so
- **WHEN** `expand` is called on a callable no test reaches
- **THEN** `tests_reaching` is present with a total of 0

#### Scenario: Measured tests are shown apart from static ones
- **WHEN** a coverage run was imported and `expand` is called on a callable it ran
- **THEN** the result also has `tests_measured`, and `show` prints it under its own heading

#### Scenario: Show prints the tests that reach a function
- **WHEN** `leyline show <function>` runs on a function some tests reach
- **THEN** it prints "tests that reach it on the map (N): …" and "reached from (N): …"

#### Scenario: Expand reads the flow steps once
- **WHEN** `expand` is called on the callable the most flows pass
- **THEN** exactly one statement reads the flow steps
