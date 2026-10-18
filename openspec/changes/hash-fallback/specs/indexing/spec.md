## ADDED Requirements

### Requirement: The index does not depend on an optional hash
#### Scenario: A Python without blake2b still maps
- **WHEN** `hashlib` has no `blake2b` (removed with `raising=False`, after the module is imported) and a
  repository is mapped twice without an edit
- **THEN** both maps complete, the second run reports no groups written and its patterns kept, and the
  fingerprint of a group equals the first 8 bytes of its sha256
