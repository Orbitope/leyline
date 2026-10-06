"""Architecture rules: constraints over the graph that are checked without any inference.

A rule is the user's intent. An agent may suggest one, but a suggested rule stays marked as
suggested until the user confirms it.
"""

from __future__ import annotations

import datetime
import json
from collections import defaultdict
from typing import Optional

KINDS = ("forbid", "no_cycle", "must_be_tested")
EDGE_KINDS = ("calls", "imports", "uses_type", "instantiates", "extends", "implements", "depends_on", "communicates",
              "reads", "writes")
# What a forbid rule looks at unless told otherwise. A channel is left out: the sender of an event
# does not depend on whoever listens to it.
DEPENDENCY_KINDS = ("calls", "imports", "uses_type", "instantiates", "extends", "implements", "depends_on", "reads", "writes")


def _select(con, selector: str) -> set[str]:
    """Node ids a selector covers. Forms: module:Name, system:Name, external:Name, path:prefix, id:prefix, *"""
    selector = selector.strip()
    if selector == "*":
        return {r[0] for r in con.execute("SELECT id FROM nodes WHERE layer = 'fact'")}
    kind, _, value = selector.partition(":")
    if kind == "module":
        row = con.execute("SELECT id FROM nodes WHERE kind = 'module' AND (name = ? OR path = ?)", (value, value)).fetchone()
        if not row:
            return set()
        return {row[0]} | {r[0] for r in con.execute("SELECT node_id FROM ancestry WHERE module_id = ?", (row[0],))}
    if kind == "system":
        row = con.execute(
            "SELECT n.id FROM nodes n LEFT JOIN annotations a ON a.node_id = n.id AND a.key = 'name'"
            " WHERE n.kind = 'system' AND (a.value = ? OR n.name = ?)", (value, value)).fetchone()
        if not row:
            return set()
        out = set()
        for u in con.execute("SELECT dst_id FROM edges WHERE kind = 'groups' AND src_id = ?", (row[0],)):
            out |= {r[0] for r in con.execute(
                "WITH RECURSIVE d(id) AS (SELECT ? UNION SELECT n.id FROM nodes n JOIN d ON n.parent_id = d.id)"
                " SELECT id FROM d", (u[0],))}
        return out
    if kind == "external":
        return {r[0] for r in con.execute("SELECT id FROM nodes WHERE kind = 'external' AND (name = ? OR name LIKE ?)",
                                          (value, value + ".%"))}
    if kind == "path":
        return {r[0] for r in con.execute("SELECT id FROM nodes WHERE path = ? OR path LIKE ?", (value, value.rstrip("/") + "/%"))}
    if kind == "id":
        return {r[0] for r in con.execute("SELECT id FROM nodes WHERE id LIKE ?", (value + "%",))}
    return set()


def add_rule(con, kind: str, selector_from: str, selector_to: str = "", edge_kinds: Optional[list[str]] = None,
             severity: str = "error", reason: str = "", status: str = "suggested", source: str = "mcp") -> dict:
    if kind not in KINDS:
        return {"error": f"kind must be one of {KINDS}"}
    if status not in ("suggested", "confirmed"):
        return {"error": "status must be suggested or confirmed"}
    if kind == "forbid" and not selector_to:
        return {"error": "a forbid rule needs selector_to"}
    if kind != "no_cycle" and not _select(con, selector_from):
        return {"error": f"selector {selector_from!r} matches nothing"}
    if kind == "forbid" and not _select(con, selector_to):
        return {"error": f"selector {selector_to!r} matches nothing"}
    with con:
        cur = con.execute(
            "INSERT INTO rules (kind, selector_from, selector_to, edge_kinds, severity, reason, status, source, created)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, selector_from, selector_to, json.dumps(edge_kinds or []), severity, reason, status, source,
             datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
    return {"id": cur.lastrowid, "kind": kind, "status": status}


def confirm_rule(con, rule_id: int) -> dict:
    with con:
        n = con.execute("UPDATE rules SET status = 'confirmed' WHERE id = ?", (rule_id,)).rowcount
    return {"id": rule_id, "status": "confirmed"} if n else {"error": f"no rule {rule_id}"}


def _links(con, kinds: list[str]):
    kinds = kinds or list(DEPENDENCY_KINDS)
    if "calls" in kinds:
        for r in con.execute("SELECT DISTINCT src_id, dst_id FROM calls"):
            yield r[0], r[1], "calls"
    rest = [k for k in kinds if k != "calls"]
    if rest:
        for r in con.execute(f"SELECT src_id, dst_id, kind FROM edges WHERE kind IN ({','.join('?' * len(rest))})", rest):
            yield r[0], r[1], r[2]


def check(con, rules_from=None) -> dict:
    """Evaluate every rule against the graph in `con`. `rules_from` reads the rules from another store."""
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM nodes")}
    out = []
    for rule in (rules_from or con).execute("SELECT * FROM rules ORDER BY id").fetchall():
        kinds = json.loads(rule["edge_kinds"] or "[]")
        violations = []
        if rule["kind"] == "forbid":
            a, b = _select(con, rule["selector_from"]), _select(con, rule["selector_to"])
            for s, d, k in _links(con, kinds):
                if s in a and d in b:
                    violations.append({"from": s, "to": d, "kind": k})
        elif rule["kind"] == "no_cycle":
            # Scope: "modules", or "systems" for systems and ungrouped types across the repo.
            scope = (rule["selector_from"] or "modules").strip()
            group = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
            if scope.startswith("system"):
                unit_sys = {r["dst_id"]: r["src_id"] for r in con.execute("SELECT src_id, dst_id FROM edges WHERE kind = 'groups'")}
                parent = {r["id"]: r["parent_id"] for r in con.execute("SELECT id, parent_id FROM nodes")}
                def owner(i):
                    cur = i
                    while cur:
                        if cur in unit_sys:
                            return unit_sys[cur]
                        cur = parent.get(cur)
                    return None
                group = {i: owner(i) for i in parent}
            graph = defaultdict(set)
            for s, d, k in _links(con, kinds or ["calls", "uses_type", "instantiates", "extends", "implements", "imports"]):
                gs, gd = group.get(s), group.get(d)
                if gs and gd and gs != gd:
                    graph[gs].add(gd)
            seen, done = set(), set()

            def visit(v, path):
                seen.add(v)
                for w in sorted(graph[v]):
                    if w in path:
                        cyc = path[path.index(w):] + [w]
                        key = tuple(sorted(cyc[:-1]))
                        if key not in done:
                            done.add(key)
                            violations.append({"cycle": [names.get(c, c) for c in cyc], "ids": cyc})
                    elif w not in seen:
                        visit(w, path + [w])
            for v in sorted(graph):
                if v not in seen:
                    visit(v, [v])
        elif rule["kind"] == "must_be_tested":
            scope = _select(con, rule["selector_from"])
            tested = {r[0] for r in con.execute(
                "SELECT DISTINCT s.callable_id FROM flow_steps s JOIN flows f ON f.id = s.flow_id"
                " WHERE json_extract(f.attrs, '$.kind') = 'test'")}
            for r in con.execute("SELECT id FROM nodes WHERE kind = 'callable'"):
                if r[0] in scope and r[0] not in tested:
                    violations.append({"untested": r[0]})
        for v in violations:
            for key in ("from", "to", "untested"):
                if key in v:
                    v[key + "_name"] = names.get(v[key], v[key])
        out.append({"id": rule["id"], "kind": rule["kind"], "from": rule["selector_from"], "to": rule["selector_to"],
                    "edge_kinds": kinds, "severity": rule["severity"], "reason": rule["reason"],
                    "status": rule["status"] or "confirmed", "passes": not violations,
                    "violations": len(violations), "examples": violations[:25]})
    return {"total": len(out), "failing": sum(1 for r in out if not r["passes"]), "rules": out}
