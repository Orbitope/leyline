## MODIFIED Requirements

### Requirement: The orientation tour describes a large module by its named parts
#### Scenario: The orientation tour lists the named parts of a module
- **WHEN** a module of 30 files or more has parts named with `name_part` and the map is built again
- **THEN** the module's stop names each named part with its summary, in the order `module_outline` lists them,
  marks a stale name as `module_outline` does, and does not list cluster groups

#### Scenario: The stop of an unnamed module says its parts are unnamed
- **WHEN** a module of 30 files or more has parts and none is named
- **THEN** its stop says how many parts are unnamed and names the leyline-explore-module skill

#### Scenario: The stop of a small module is unchanged
- **WHEN** a module has fewer than 30 files
- **THEN** its stop reads as before and says nothing about naming parts

#### Scenario: The parts are computed once per map
- **WHEN** a repository is mapped
- **THEN** `outline.map_parts` runs once, after the generation stamp is written, and the tour and the page read
  that one result

### Requirement: Map prompts the first orientation
#### Scenario: Map says when the parts of the main module are unnamed
- **WHEN** `leyline map` finishes on a repository whose largest product module of 30 files or more has unnamed parts
- **THEN** its `Next:` line ends with how many parts are unnamed and how to name them, whether or not change
  folders exist

#### Scenario: The next of the map tool offers to look around
- **WHEN** the `map` MCP tool finishes
- **THEN** `next` keeps the spec step first and then names the orientation tour, `find_flows`, `module_outline`
  and naming parts
