"""Propose systems: groups of types inside a large module, found by Louvain community detection.

The proposal is deterministic. Naming and explaining each system is an inferred-layer job, done
through `annotate`.
"""

from __future__ import annotations

import json
from collections import defaultdict

SOURCE = "leyline-cluster/louvain-0.1"
WEIGHTS = {"calls": 1.0, "uses_type": 1.0, "instantiates": 1.0, "extends": 3.0, "implements": 3.0}


def _units(con, repo_id: str) -> tuple[dict, dict]:
    """Map every node to its unit (outermost type, or file for loose functions) and unit to module."""
    nodes = {r["id"]: r for r in con.execute(
        "SELECT id, kind, parent_id, name FROM nodes WHERE repo_id = ? AND layer = 'fact'", (repo_id,))}
    unit: dict[str, str] = {}
    for i, n in nodes.items():
        if n["kind"] in ("module", "repo", "external", "entry_point"):
            continue
        if n["kind"] == "file":
            unit[i] = i
            continue
        cur = i
        while nodes[cur]["parent_id"] in nodes and nodes[nodes[cur]["parent_id"]]["kind"] != "file":
            cur = nodes[cur]["parent_id"]
        unit[i] = cur if nodes[cur]["kind"] == "type" else nodes[cur]["parent_id"]
    module = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
    return unit, module


def propose(con, repo_id: str, min_units: int = 12, resolution: float = 1.0, seed: int = 7,
            min_modularity: float = 0.3) -> dict:
    """Replace the proposed systems of a repo. Returns counts per module."""
    try:
        import networkx as nx
    except ImportError:  # clustering is optional
        return {"skipped": "networkx is not installed"}
    unit, module = _units(con, repo_id)
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM nodes WHERE repo_id = ?", (repo_id,))}
    kinds = {r["id"]: r["kind"] for r in con.execute("SELECT id, kind FROM nodes WHERE repo_id = ?", (repo_id,))}
    has_code = {u for i, u in unit.items() if kinds.get(i) in ("callable", "type")}
    by_module: dict[str, set] = defaultdict(set)
    for u in set(unit.values()):
        if u in has_code and module.get(u):
            by_module[module[u]].add(u)
    weights: dict[tuple, float] = defaultdict(float)

    def add(src: str, dst: str, w: float) -> None:
        a, b = unit.get(src), unit.get(dst)
        if a and b and a != b and module.get(a) == module.get(b):
            weights[(a, b) if a < b else (b, a)] += w

    for r in con.execute("SELECT src_id, dst_id, COUNT(*) AS n FROM calls GROUP BY src_id, dst_id ORDER BY dst_id, src_id"):
        add(r["src_id"], r["dst_id"], WEIGHTS["calls"] * r["n"])
    for r in con.execute("SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('uses_type','instantiates','extends','implements')"):
        add(r["src_id"], r["dst_id"], WEIGHTS[r["kind"]])

    # Both ends of a weighted pair share a module, so each module's pairs can be found without scanning them all.
    pairs_of: dict[str, list] = defaultdict(list)
    for (a, b), w in weights.items():
        pairs_of[module.get(a)].append((a, b, w))

    old = [r[0] for r in con.execute("SELECT id FROM nodes WHERE repo_id = ? AND kind = 'system' AND source = ?",
                                     (repo_id, SOURCE))]
    with con:
        for sid in old:
            con.execute("DELETE FROM edges WHERE kind = 'groups' AND src_id = ?", (sid,))
        con.execute("DELETE FROM nodes WHERE repo_id = ? AND kind = 'system' AND source = ?", (repo_id, SOURCE))
        result = {}
        for mod, members in sorted(by_module.items()):
            if len(members) < min_units:
                continue
            g = nx.Graph()
            g.add_nodes_from(sorted(members))
            for a, b, w in pairs_of.get(mod, ()):
                if a in members and b in members:
                    g.add_edge(a, b, weight=w)
            communities = nx.community.louvain_communities(g, weight="weight", seed=seed, resolution=resolution)
            modularity = nx.community.modularity(g, communities, weight="weight") if g.number_of_edges() else 0.0
            made = 0
            if modularity < min_modularity:
                # The module does not split cleanly; proposing groups would be noise.
                result[mod.split(":module:")[-1]] = {"units": len(members), "systems": 0,
                                                     "modularity": round(modularity, 3), "note": "no clear split"}
                continue
            for community in sorted(communities, key=lambda c: (-len(c), sorted(c)[0])):
                if len(community) < 2:
                    continue  # a type with no links stays ungrouped
                ranked = sorted(community, key=lambda v: (-g.degree(v, weight="weight"), v))
                anchor = ranked[0]
                internal = sum(d["weight"] for a, b, d in g.subgraph(community).edges(data=True))
                boundary = sum(d["weight"] for a, b, d in g.edges(community, data=True)
                               if (a in community) != (b in community))
                sid = f"{mod}/system:{names[anchor]}"
                con.execute(
                    "INSERT OR REPLACE INTO nodes (id, kind, name, parent_id, repo_id, layer, source, attrs)"
                    " VALUES (?, 'system', ?, ?, ?, 'inferred', ?, ?)",
                    (sid, f"{names[anchor]} group", mod, repo_id, SOURCE, json.dumps({
                        "anchor": anchor, "members": len(community), "modularity": round(modularity, 3),
                        "cohesion": round(internal / (internal + boundary), 3) if internal + boundary else 0.0,
                        "top": [names[v] for v in ranked[:6]], "method": "louvain"})))
                con.executemany(
                    "INSERT INTO edges (kind, src_id, dst_id, precision, layer, source) VALUES"
                    " ('groups', ?, ?, 'heuristic', 'inferred', ?)", [(sid, u, SOURCE) for u in sorted(community)])
                made += 1
            result[mod.split(":module:")[-1]] = {"units": len(members), "systems": made,
                                                 "modularity": round(modularity, 3)}
        con.execute("INSERT OR REPLACE INTO extractor_coverage VALUES (?,?,?,?,?,?)",
                    (repo_id, "systems:louvain", "0.1", "ok", None, json.dumps(result)))
    return result
