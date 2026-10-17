## ADDED Requirements

### Requirement: Each function and type says what it is for
The store holds, for each callable and type, the comment or docstring its author wrote above or inside it, and a
summary that prefers an annotation written by a person or an agent.

#### Scenario: The docstring of a function is on the map
- **WHEN** a file with a function that has a docstring is mapped
- **THEN** the function's node has `doc` in its attributes, holding the docstring's text

#### Scenario: A comment edited above a function re-parses only its own file
- **WHEN** only the comment above a function named like functions in other files is edited and the map is run again
- **THEN** the incremental run parses that file and resolves no other file again

#### Scenario: An annotation overrides the docstring in a summary
- **WHEN** a function has a docstring and a `responsibility` annotation
- **THEN** its summary is the annotation, the intent layer wins over the inferred one, and an annotation marked
  stale loses to the docstring

### Requirement: Every understanding tool shows the summary
#### Scenario: Search and expand say what a function does
- **WHEN** `search` or `expand` returns a function that has a docstring and a `responsibility` annotation
- **THEN** each result row carries `summary` holding the annotation, from one summaries query per answer, and the
  `search` and `show` commands print it

#### Scenario: The context outline says what the focus does
- **WHEN** `context` is built around a function with a summary
- **THEN** the focus line and the lines of its shown direct neighbours carry the summary as a `--` line, looked up
  once for the focus and its direct neighbours only, and the outline stays within its budget (a budget too small
  for more than the focus's own lines leaves the summaries out)

#### Scenario: The busiest functions of an outline carry their summary
- **WHEN** `module_outline` lists a part's busiest functions, key types or key functions
- **THEN** each one that has a summary carries it, and the `outline` command prints it

#### Scenario: Each step of a walk says what it does
- **WHEN** `explain_path` walks through a function with a summary
- **THEN** the step carries `summary`, cut to 120 characters, and the text form prints it after the step's name

### Requirement: Finding a flow uses the words the author wrote
#### Scenario: A match on several words outranks a match on one word in a name
- **WHEN** `find_flows` is asked "how the server answers a question", one function is named `answers` with no doc
  and another is named `find_flows` with the doc "Answer a question about the server from the map"
- **THEN** the function with the doc ranks first, and a test or sample code matching the same words still ranks
  below product code

#### Scenario: An annotation written after the map is found at once
- **WHEN** `annotate` writes a `responsibility` on a function and `find_flows` is asked with words from it
- **THEN** the function is a candidate matched on "its note", without a new map
