-- Leyline store, schema v0. One SQLite file per workspace.
-- Layers: fact (deterministic), inferred (LLM), intent (human).

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS nodes (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,
  name          TEXT NOT NULL,
  parent_id     TEXT REFERENCES nodes(id),
  repo_id       TEXT,
  language      TEXT,
  path          TEXT,
  span_start    INTEGER,
  span_end      INTEGER,
  content_hash  TEXT,
  layer         TEXT NOT NULL DEFAULT 'fact',
  source        TEXT NOT NULL,
  commit_sha    TEXT,
  attrs         TEXT
);
CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS nodes_path ON nodes(path);

CREATE TABLE IF NOT EXISTS edges (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,
  src_id      TEXT NOT NULL REFERENCES nodes(id),
  dst_id      TEXT NOT NULL REFERENCES nodes(id),
  precision   TEXT NOT NULL,
  layer       TEXT NOT NULL DEFAULT 'fact',
  source      TEXT NOT NULL,
  commit_sha  TEXT,
  attrs       TEXT
);
CREATE INDEX IF NOT EXISTS edges_out ON edges(src_id, kind);
CREATE INDEX IF NOT EXISTS edges_in ON edges(dst_id, kind);

-- Call edges are the largest kind, so they get a narrow table of their own.
CREATE TABLE IF NOT EXISTS calls (
  src_id      TEXT NOT NULL REFERENCES nodes(id),
  dst_id      TEXT NOT NULL REFERENCES nodes(id),
  dispatch    TEXT NOT NULL,
  precision   TEXT NOT NULL,
  site_start  INTEGER,
  site_end    INTEGER,
  hit_count   INTEGER DEFAULT 0,
  commit_sha  TEXT
);
CREATE INDEX IF NOT EXISTS calls_out ON calls(src_id, dst_id);
CREATE INDEX IF NOT EXISTS calls_in ON calls(dst_id, src_id);

CREATE TABLE IF NOT EXISTS annotations (
  id             INTEGER PRIMARY KEY,
  node_id        TEXT NOT NULL REFERENCES nodes(id),
  key            TEXT NOT NULL,
  value          TEXT NOT NULL,
  layer          TEXT NOT NULL,
  source         TEXT NOT NULL,
  confidence     REAL,
  evidence       TEXT,
  evidence_hash  TEXT,
  stale          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS annotations_node ON annotations(node_id, key);

CREATE TABLE IF NOT EXISTS flows (
  id TEXT PRIMARY KEY, name TEXT, origin TEXT, entry_id TEXT, weight REAL,
  group_id TEXT, layer TEXT, source TEXT, attrs TEXT
);
CREATE TABLE IF NOT EXISTS flow_steps (
  flow_id TEXT, seq INTEGER, depth INTEGER, callable_id TEXT, edge_id INTEGER,
  via TEXT,            -- start | calls | runs | dispatch | event | process
  site_line INTEGER,   -- line of the call that led here
  parent_seq INTEGER   -- the step this one was reached from
);
CREATE INDEX IF NOT EXISTS flow_steps_flow ON flow_steps(flow_id, seq);
CREATE INDEX IF NOT EXISTS flow_steps_callable ON flow_steps(callable_id);
CREATE TABLE IF NOT EXISTS pattern_instances (
  id TEXT PRIMARY KEY, pattern TEXT, matcher TEXT, rationale TEXT,
  confidence REAL, evidence_hash TEXT, stale INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pattern_roles (instance_id TEXT, role TEXT, node_id TEXT);
CREATE TABLE IF NOT EXISTS tours (id TEXT PRIMARY KEY, title TEXT, audience TEXT, layer TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS tour_stops (tour_id TEXT, seq INTEGER, ref_kind TEXT, ref_id TEXT, narrative TEXT);
CREATE TABLE IF NOT EXISTS boundaries (id INTEGER PRIMARY KEY, system_id TEXT, selector TEXT);
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY, kind TEXT, selector_from TEXT, selector_to TEXT,
  edge_kinds TEXT, severity TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS change_proposals (
  id TEXT PRIMARY KEY, intent TEXT, status TEXT, base_commit TEXT, head_commit TEXT, attrs TEXT
);

-- What each extractor did per repo, so "not analyzed" is distinguishable from "none found".
CREATE TABLE IF NOT EXISTS extractor_coverage (
  repo_id TEXT, extractor TEXT, version TEXT, status TEXT, commit_sha TEXT, stats TEXT,
  PRIMARY KEY (repo_id, extractor)
);

-- Roll-up cache: the file and module each node sits in.
CREATE TABLE IF NOT EXISTS ancestry (
  node_id TEXT PRIMARY KEY, file_id TEXT, module_id TEXT
);
CREATE INDEX IF NOT EXISTS ancestry_module ON ancestry(module_id);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(node_id UNINDEXED, name, qualified, path, kind UNINDEXED);
