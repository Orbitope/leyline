"""Read the rows of a store that an index writes, in a form two stores can be compared by: what an incremental run
must leave exactly as a full run of the same tree would."""

from __future__ import annotations

from leyline.incremental import differences as _stream

TABLES = {
    "nodes": "SELECT id, kind, name, parent_id, repo_id, language, path, span_start, span_end, content_hash, layer, source,"
             " attrs FROM nodes",
    "calls": "SELECT src_id, dst_id, dispatch, precision, site_start, site_end, hit_count FROM calls",
    "edges": "SELECT kind, src_id, dst_id, precision, layer, source, attrs FROM edges",
    "flows": "SELECT id, name, origin, entry_id, weight, group_id, layer, source, attrs FROM flows",
    "flow_steps": "SELECT flow_id, seq, depth, callable_id, via, site_line, parent_seq FROM flow_steps",
    "patterns": "SELECT id, pattern, matcher, rationale, confidence, evidence_hash, stale, attrs FROM pattern_instances",
    "pattern_roles": "SELECT instance_id, role, node_id FROM pattern_roles",
    "tour_stops": "SELECT tour_id, seq, ref_kind, ref_id, narrative, title FROM tour_stops",
    "search": "SELECT node_id, name, qualified, path, kind FROM search",
    "ancestry": "SELECT node_id, file_id, module_id FROM ancestry",
    "coverage": "SELECT repo_id, extractor, version, status, stats FROM extractor_coverage WHERE extractor != 'timing'",
}


def differences(a, b) -> dict:
    """table -> (rows in a, rows in b, the first rows that differ); empty when the stores agree."""
    return _stream(a, b, TABLES)
