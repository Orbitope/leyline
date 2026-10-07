"""Propose systems: groups of types inside a large module, found by Louvain community detection.

The proposal is deterministic. Naming and explaining each system is an inferred-layer job, done
through `annotate`.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Optional

from . import store

SOURCE = "leyline-cluster/louvain-0.1"
WEIGHTS = {"calls": 1.0, "uses_type": 1.0, "instantiates": 1.0, "extends": 3.0, "implements": 3.0}


def _units(con, repo_id: str, only: str = "") -> tuple[dict, dict]:
    """Map every node to its unit (outermost type, or file for loose functions) and unit to module. `only` limits
    it to the nodes in some modules (SQL naming their ids)."""
    nodes = {r["id"]: r for r in con.execute(
        f"SELECT id, kind, parent_id, name FROM nodes WHERE repo_id = ? AND layer = 'fact'{only}", (repo_id,))}
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
    module = {r["node_id"]: r["module_id"] for r in con.execute(
        "SELECT node_id, module_id FROM ancestry" + (f" WHERE node_id IN (SELECT id FROM nodes WHERE 1{only})" if only else ""))}
    return unit, module


def propose(con, repo_id: str, min_units: int = 12, resolution: float = 1.0, seed: int = 7,
            min_modularity: float = 0.3, modules: Optional[set] = None) -> dict:
    """Replace the proposed systems of a repo. Returns counts per module. With `modules`, only those modules are
    clustered again (each module is clustered on its own, so the others would come out as they are) and the
    counts kept for the rest are carried over."""
    try:
        import networkx as nx
    except ImportError:  # clustering is optional
        return {"skipped": "networkx is not installed"}
    only, src_only = "", ""
    if modules is not None:
        con.execute("CREATE TEMP TABLE IF NOT EXISTS cluster_mods (id TEXT PRIMARY KEY)")
        con.execute("DELETE FROM cluster_mods")
        con.executemany("INSERT INTO cluster_mods VALUES (?)", [(m,) for m in sorted(modules)])
        only = " AND id IN (SELECT node_id FROM ancestry WHERE module_id IN (SELECT id FROM cluster_mods))"
        src_only = " AND src_id IN (SELECT node_id FROM ancestry WHERE module_id IN (SELECT id FROM cluster_mods))"
    unit, module = _units(con, repo_id, only)
    names = {r["id"]: r["name"] for r in con.execute(f"SELECT id, name FROM nodes WHERE repo_id = ?{only}", (repo_id,))}
    kinds = {r["id"]: r["kind"] for r in con.execute(f"SELECT id, kind FROM nodes WHERE repo_id = ?{only}", (repo_id,))}
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

    for r in con.execute(f"SELECT src_id, dst_id, COUNT(*) AS n FROM calls WHERE 1{src_only} GROUP BY src_id, dst_id ORDER BY dst_id, src_id"):
        add(r["src_id"], r["dst_id"], WEIGHTS["calls"] * r["n"])
    for r in con.execute("SELECT src_id, dst_id, kind FROM edges WHERE kind IN ('uses_type','instantiates','extends','implements')"
                         + src_only):
        add(r["src_id"], r["dst_id"], WEIGHTS[r["kind"]])

    # Both ends of a weighted pair share a module, so each module's pairs can be found without scanning them all.
    pairs_of: dict[str, list] = defaultdict(list)
    for (a, b), w in weights.items():
        pairs_of[module.get(a)].append((a, b, w))

    mine = " AND parent_id IN (SELECT id FROM cluster_mods)" if modules is not None else ""
    old = [r[0] for r in con.execute(f"SELECT id FROM nodes WHERE repo_id = ? AND kind = 'system' AND source = ?{mine}",
                                     (repo_id, SOURCE))]
    with con:
        for sid in old:
            con.execute("DELETE FROM links WHERE kind = 'groups' AND src IN (SELECT k FROM keys WHERE id = ?)", (sid,))
        con.execute(f"DELETE FROM nodes WHERE repo_id = ? AND kind = 'system' AND source = ?{mine}", (repo_id, SOURCE))
        result = {}
        for mod, members in sorted(by_module.items()):
            if len(members) < min_units:
                continue
            g = nx.Graph()
            g.add_nodes_from(sorted(members))
            for a, b, w in sorted(pairs_of.get(mod, ())):   # sorted: the communities found depend on the order edges are added
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
                store.insert_edges(con, [(None, "groups", sid, u, "heuristic", "inferred", SOURCE, None, None)
                                         for u in sorted(community)])
                made += 1
            result[mod.split(":module:")[-1]] = {"units": len(members), "systems": made,
                                                 "modularity": round(modularity, 3)}
        if modules is not None:
            row = con.execute("SELECT stats FROM extractor_coverage WHERE repo_id = ? AND extractor = 'systems:louvain'",
                              (repo_id,)).fetchone()
            kept = json.loads(row[0]) if row and row[0] else {}
            redone = {m.split(":module:")[-1] for m in modules}
            result = dict(sorted({**{k: v for k, v in kept.items() if k not in redone}, **result}.items()))
        con.execute("INSERT OR REPLACE INTO extractor_coverage VALUES (?,?,?,?,?,?)",
                    (repo_id, "systems:louvain", "0.1", "ok", None, json.dumps(result)))
    return result
