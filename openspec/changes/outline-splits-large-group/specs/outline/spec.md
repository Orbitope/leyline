## MODIFIED Requirements

### Requirement: A level's parts are a real split
#### Scenario: A group holding most of a flat module is split again
- **WHEN** a flat module's files group so that one group holds more than 40% of the loose files' lines and at
  least 6 files, and that group separates with the code it uses in the graph
- **THEN** the outline shows the group's communities as separate parts of the level, each smaller than the group,
  with the files no community took kept under the group's own id, and community detection ran at most twice
  more for that group

#### Scenario: A group that does not separate stays whole
- **WHEN** such a group gives fewer than two communities either way
- **THEN** the outline shows it unchanged

#### Scenario: A name given to a split group is marked stale, not lost
- **WHEN** a group was named with `name_part` and a later outline splits it
- **THEN** the piece sharing most of its files shows the name marked "may be stale" when it kept less than half of
  them, and the name is not shown on the others

#### Scenario: The label of a group counts the files it does not name
- **WHEN** a group holds more than three files
- **THEN** its label names its three busiest files, says how many more it holds, and ends with "group"
