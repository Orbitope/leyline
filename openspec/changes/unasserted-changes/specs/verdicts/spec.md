## ADDED Requirements

### Requirement: A plan of small breaks for the code a change edited
#### Scenario: The mutation plan covers only the lines the change edited
- **WHEN** a change edited two lines of a Python function that has comparisons on other lines
- **THEN** the plan holds mutants of the two lines only, each with its file, line, what it changes, its function and its
  tests in two stages

#### Scenario: A moved function gets no mutants
- **WHEN** a change moved a function to another file, or reformatted it, with the same lines
- **THEN** the plan holds no mutant of it

#### Scenario: Each mutant is a patch that applies
- **WHEN** the plan is written for a file with LF line endings and for one with CRLF
- **THEN** each patch is accepted by `git apply --check` from its repository's root, and the working tree is unchanged

#### Scenario: A mutant is made at the operator
- **WHEN** a changed line has two comparisons and a non-ASCII character before them
- **THEN** the mutant that flips the second changes only that operator, and the mutated file parses

#### Scenario: Code of the tests gets no mutants
- **WHEN** a change edited a function and the test that covers it
- **THEN** the plan holds mutants of the function and none of the test

#### Scenario: The cap gives each edited function a mutant first
- **WHEN** a change edited five functions with ten mutable lines each and the cap is 30
- **THEN** every function has a mutant before any has three, the mutants are numbered in that order, and a function the
  cap left without one is named

#### Scenario: A language with no mutation operators is named
- **WHEN** a change edited a TypeScript file
- **THEN** the plan says there are no mutants for that file and why

#### Scenario: A plan is for spec changes only
- **WHEN** `mutants` is called on a `pr-<id>` or a quick change
- **THEN** it is refused, and says that plans are for spec changes

#### Scenario: Planning reads the line hashes of the edited files only
- **WHEN** the plan is made on a repository of many files, two of them edited
- **THEN** the snapshot rows of those two files are read

### Requirement: The runner leaves the working tree as it found it
#### Scenario: The runner restores the file after a hang
- **WHEN** a mutant makes a test run past its timeout
- **THEN** `run.py` stops it, undoes the patch, records `timeout` and goes on, and the file is as it was

#### Scenario: A baseline failure kills nothing
- **WHEN** a test fails in the unmutated run
- **THEN** it is listed as a baseline failure and no mutant counts as killed by it

### Requirement: Results say whether the scenario's own test asserts what the change did
#### Scenario: A mutant that survived is listed
- **WHEN** a results file marks a mutant as survived
- **THEN** the page lists it under "Covered, but not asserted" with its file, line, what was changed and the tests that ran it

#### Scenario: A scenario with no mutant caught by its own tests is unasserted
- **WHEN** a scenario passes, its test reaches a changed function, and every mutant of that function was run and none
  failed the scenario's test, even if another test failed one
- **THEN** its verdict is `unasserted`, saying how many mutants and which tests did fail them

#### Scenario: A scenario with a mutant caught by its own tests keeps its verdict
- **WHEN** a mutant of a function the scenario reaches failed the scenario's own test
- **THEN** the scenario keeps the verdict it had

#### Scenario: A scenario whose tests are new is judged too
- **WHEN** the scenario's test is new since the plan, and caught no mutant
- **THEN** its verdict is `unasserted`, not `proven`

#### Scenario: A mutant that was never run leaves the scenario unjudged
- **WHEN** the results file leaves a mutant of the function out, or marks it as an error
- **THEN** the page says how many were not run, and the scenario keeps its verdict

#### Scenario: An equivalent mutant counts for nothing
- **WHEN** a person marks a mutant equivalent with a reason
- **THEN** it is left out of the judgment and the page shows the reason

#### Scenario: A scenario with no mutants is not unasserted
- **WHEN** the changed functions the scenario reaches have no mutants
- **THEN** it keeps its verdict and the page says why there were none

#### Scenario: Results made for other code are refused
- **WHEN** the results file names another plan, or a mutated file has changed since the plan
- **THEN** `check` refuses it and says which

### Requirement: A change can name the links it adds between modules
#### Scenario: A link the spec names is not a hold-up
- **WHEN** a change adds an import of a module and its tasks name that module in backticks
- **THEN** the link is listed as named in the spec and the change can still be done as agreed

#### Scenario: A link the spec does not name holds the change up
- **WHEN** a change adds an import of a module that no file of the change names
- **THEN** the link is listed as a new link between modules and the change is not done as agreed

### Requirement: The new verdict does not block unless the project says so
#### Scenario: Unasserted does not block by default
- **WHEN** a scenario is unasserted and `[check] blocking` is the default
- **THEN** `check` reports the change as done as agreed, exits 0, and says what is unasserted

#### Scenario: A project can make unasserted block
- **WHEN** `[check] blocking` lists `unasserted` and a scenario has it
- **THEN** `check` exits 1, and `next` says to strengthen the test and run the plan again

#### Scenario: Without results check is as it was
- **WHEN** `check` is called without `--mutants`
- **THEN** its page is what it was, the blocking line does not name `unasserted`, and no scenario has it
