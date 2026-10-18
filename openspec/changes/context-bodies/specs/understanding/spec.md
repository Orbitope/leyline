## ADDED Requirements

### Requirement: An outline can carry the code to read, not only the declarations
#### Scenario: The context outline carries the bodies of the focus and its callers
- **WHEN** `context` is called with `bodies` on a function that other functions call
- **THEN** the text lists the whole body of the function and the lines around the call in each direct caller, each with
  its file and lines, after an outline cut to a third of the budget, and the whole stays within the budget

#### Scenario: A body does not bring its sibling methods
- **WHEN** the focus is a method of a type that has other methods
- **THEN** no other method of that type or of its file appears in the bodies

#### Scenario: A hub does not crowd out the types
- **WHEN** the focus has over a hundred callers and takes a parameter of a type on the map
- **THEN** the type is shown, and the callers that do not fit are left out

#### Scenario: The types a focus takes or returns show their declaration and fields
- **WHEN** the focus takes a parameter of a type on the map, and returns another
- **THEN** each type appears as its declaration line and its field lines, at most 12 and then a count, and not its
  methods

#### Scenario: A body that does not fit is named
- **WHEN** the budget is too small for every body
- **THEN** the focus comes first, whole or cut at a line it names, a caller that does not fit is listed under "Bodies
  left out" with its length, and the answer stays within the budget

#### Scenario: A long caller is cut around the call
- **WHEN** a caller is longer than 40 lines and calls the focus past its fortieth line
- **THEN** the lines shown are those around the call, and the answer says which lines of the caller they are

#### Scenario: The top level of a file is a caller with a short window
- **WHEN** the focus is called from the top level of a file
- **THEN** at most 16 lines around the call are shown, not the first lines of the file

#### Scenario: A file as the focus shows at most three bodies
- **WHEN** `context` is called with `bodies` and a file with many functions as the focus
- **THEN** at most three bodies of its functions are shown, the rest are listed as left out, and the answer stays within
  the budget

#### Scenario: A body is shown once
- **WHEN** a caller contains a body already shown, or is itself a focus function
- **THEN** its text appears once

#### Scenario: A function defined twice shows each definition
- **WHEN** a function is defined in both branches of an if
- **THEN** each definition is shown by its own lines, and not the lines between them

#### Scenario: A hub reads only the files of the bodies it shows
- **WHEN** the focus has over a hundred callers in many files
- **THEN** no more files are opened than the bodies shown and ten that were refused for size

#### Scenario: A file that links outside the repository is not read
- **WHEN** a mapped file has become a link to a file outside the repository
- **THEN** its body is not shown and the answer says it was not read

#### Scenario: A body changed since the map is not shown
- **WHEN** a focus function was edited after the map was made
- **THEN** its body is not shown and is listed as changed since the map, with a line telling the agent to map again

#### Scenario: A large budget through the tool keeps the lines that say what was left out
- **WHEN** the `context` tool is called with `bodies` and a budget of 8,000 tokens on a focus with many callers
- **THEN** the answer is not cut, and still ends with the lines that name what was left out

#### Scenario: A small budget is raised and says so
- **WHEN** `context` is called with `bodies` and a budget under 600 tokens
- **THEN** the answer uses 600 and says that it did

#### Scenario: Without bodies the outline is as before
- **WHEN** `context` is called without `bodies`
- **THEN** the answer has no bodies and its text is what it was before this change
