"""Label design patterns by their shape in the graph.

Each matcher looks for a structure, not a name: a strategy is an abstraction with several
implementations that some other type holds and calls. A match says the code has that shape; it does
not say the author meant the pattern. Matches are written to the inferred layer with the nodes that
play each role, a sentence saying why, and a fixed confidence per matcher. An agent can add labels the
matchers cannot see through `label`.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Optional

from . import store

MATCHER = "leyline-patterns/0.1"
SIDE_DIRS = ("sample", "samples", "example", "examples", "bench", "benchmarks", "docs", "doc", "demo", "demos", "snippets")

# How far each shape is from proof of intent. Shapes that also occur by accident score lower.
CONFIDENCE = {"strategy": 0.8, "decorator": 0.75, "composite": 0.75, "template method": 0.7, "observer": 0.85,
              "factory": 0.6, "builder": 0.6, "singleton": 0.7, "process boundary": 0.9}

ABOUT = {
    "strategy": "An abstraction with interchangeable implementations, chosen and held by another type.",
    "decorator": "A type that implements an abstraction and wraps another object of the same abstraction.",
    "composite": "A type that implements an abstraction and holds a collection of that same abstraction.",
    "template method": "A base method that fixes the steps and leaves some of them to subclasses.",
    "observer": "A type raises an event; other code subscribes and is called back.",
    "factory": "One function decides which of several related types to create.",
    "builder": "A type that collects settings step by step and then produces another object.",
    "singleton": "A type that keeps one shared instance of itself.",
    "process boundary": "Two programs that share no code and talk over a pipe.",
}


class _Graph:
    def __init__(self, con):
        self.nodes = {r["id"]: r for r in con.execute(
            "SELECT id, kind, name, parent_id, language, path, attrs FROM nodes WHERE layer = 'fact'")}
        self.attrs = {i: json.loads(r["attrs"] or "{}") for i, r in self.nodes.items()}
        self.children = defaultdict(list)
        for i, r in self.nodes.items():
            self.children[r["parent_id"]].append(i)
        self.out = defaultdict(lambda: defaultdict(set))
        self.inn = defaultdict(lambda: defaultdict(set))
        self.edge_attrs = {}
        for e in con.execute("SELECT kind, src_id, dst_id, attrs FROM edges WHERE kind != 'contains'"):
            self.out[e["kind"]][e["src_id"]].add(e["dst_id"])
            self.inn[e["kind"]][e["dst_id"]].add(e["src_id"])
            self.edge_attrs[(e["kind"], e["src_id"], e["dst_id"])] = json.loads(e["attrs"] or "{}")
        self.calls = defaultdict(set)
        for c in con.execute("SELECT DISTINCT src_id, dst_id FROM calls"):
            self.calls[c["src_id"]].add(c["dst_id"])
        self.module = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
        tests = {r[0] for r in con.execute("SELECT id FROM nodes WHERE kind = 'test'")}
        self.test_modules = {self.module.get(t) for t in tests}

    def kind(self, i):
        return self.nodes[i]["kind"] if i in self.nodes else None

    def name(self, i):
        if i not in self.nodes:
            return i
        n = self.nodes[i]
        if n["kind"] == "type" and "`" in i.rsplit(".", 1)[-1]:
            return n["name"] + "<T>"  # tell Foo<T> from Foo
        return n["name"]

    def qual(self, i):
        """A member with its owner: Simulation.Step."""
        owner = self.owner(i) if self.kind(i) in ("callable", "field") else None
        return f"{self.name(owner)}.{self.name(i)}" if owner else self.name(i)

    def subtypes(self, t) -> set:
        return self.inn["implements"][t] | self.inn["extends"][t]

    def supertypes(self, t, seen=None) -> set:
        seen = seen if seen is not None else set()
        for b in self.out["implements"][t] | self.out["extends"][t]:
            if b not in seen:
                seen.add(b)
                self.supertypes(b, seen)
        return seen

    def owner(self, i) -> Optional[str]:
        """The type a member belongs to."""
        cur = self.nodes[i]["parent_id"] if i in self.nodes else None
        while cur in self.nodes and self.nodes[cur]["kind"] != "type":
            cur = self.nodes[cur]["parent_id"]
        return cur if cur in self.nodes else None

    def fields(self, t):
        return [c for c in self.children[t] if self.kind(c) == "field"]

    def methods(self, t):
        return [c for c in self.children[t] if self.kind(c) == "callable"]

    def is_abstraction(self, t) -> bool:
        a = self.attrs.get(t, {})
        return a.get("native_kind") == "interface" or bool(a.get("is_abstract")) or (
            self.nodes[t]["language"] == "python" and len(self.subtypes(t)) >= 2)

    def in_tests(self, i) -> bool:
        """Test code, and code beside the product: samples, benchmarks, docs."""
        path = (self.nodes[i]["path"] or "") if i in self.nodes else ""
        parts = [p.lower() for p in path.split("/")[:-1]]
        return self.module.get(i) in self.test_modules or any(p.startswith("test") or p in SIDE_DIRS for p in parts)


def _held(g: _Graph, abstraction: str):
    """Fields whose type is the abstraction itself (not a collection of it), by owning type."""
    out = defaultdict(list)
    for f in g.inn["uses_type"][abstraction]:
        if g.kind(f) == "field" and g.edge_attrs.get(("uses_type", f, abstraction), {}).get("role") == "field_type":
            owner = g.owner(f)
            if owner:
                out[owner].append(f)
    return out


def _collections(g: _Graph, abstraction: str):
    """Fields that hold many of the abstraction (List<I>, I[], dict of I), by owning type."""
    out = defaultdict(list)
    name = g.name(abstraction)
    for f in g.inn["uses_type"][abstraction]:
        if g.kind(f) != "field":
            continue
        role = g.edge_attrs.get(("uses_type", f, abstraction), {}).get("role")
        declared = g.attrs[f].get("declared_type") or ""
        if role == "generic_arg" or declared.replace(" ", "").endswith(name + "[]"):
            owner = g.owner(f)
            if owner:
                out[owner].append(f)
    return out


def detect(con) -> list[dict]:
    g = _Graph(con)
    found: list[dict] = []

    def add(pattern, anchor, roles, rationale, confidence=None, peripheral=None):
        roles = {k: sorted(set(v)) for k, v in roles.items() if v}
        found.append({"id": "pat:" + hashlib.sha1(f"{pattern}|{anchor}".encode()).hexdigest()[:10],
                      "pattern": pattern, "anchor": anchor, "roles": roles, "rationale": rationale,
                      "confidence": confidence or CONFIDENCE[pattern],
                      "in_tests": g.in_tests(anchor.split("|")[0]) if peripheral is None else peripheral})

    types = [i for i, r in g.nodes.items() if r["kind"] == "type"]
    wrappers: dict[str, set] = defaultdict(set)   # abstraction -> types already explained as decorator/composite

    # Decorator and composite first: a wrapper also holds the abstraction, and should not count as a strategy's context.
    for t in types:
        supers = g.supertypes(t)
        for a in supers:
            if not g.is_abstraction(a):
                continue
            many = _collections(g, a).get(t)
            one = _held(g, a).get(t)
            if many:
                wrappers[a].add(t)
                add("composite", f"{t}|{a}", {"component": [a], "composite": [t], "children": many},
                    f"{g.name(t)} implements {g.name(a)} and holds a collection of {g.name(a)} in "
                    f"{', '.join(g.name(f) for f in many)}, so one object can stand for many.")
            elif one:
                wrappers[a].add(t)
                add("decorator", f"{t}|{a}", {"component": [a], "decorator": [t], "wrapped": one},
                    f"{g.name(t)} implements {g.name(a)} and keeps another {g.name(a)} in "
                    f"{', '.join(g.name(f) for f in one)}, so it can add behavior around the one it wraps.")

    for a in types:
        if not g.is_abstraction(a):
            continue
        impls = sorted(s for s in g.subtypes(a) if g.kind(s) == "type" and not g.attrs[s].get("native_kind") == "interface")
        real = [s for s in impls if not g.in_tests(s)]
        if len(real) < 2:
            continue
        holders = {t: fs for t, fs in _held(g, a).items() if t not in wrappers[a] and t not in impls and t != a}
        abstract_methods = set(g.methods(a))
        using = {}
        for t, fs in holders.items():
            callers = [m for m in g.methods(t) if g.calls[m] & abstract_methods]
            if callers:
                using[t] = (fs, callers)
        if any(not g.in_tests(t) for t in using):
            using = {t: v for t, v in using.items() if not g.in_tests(t)}
        if using:
            contexts = sorted(using)
            add("strategy", a, {"strategy": [a], "implementation": real, "context": contexts,
                                "held in": [f for t in contexts for f in using[t][0]]},
                f"{g.name(a)} has {len(real)} implementations ({', '.join(g.name(s) for s in real[:4])}"
                f"{', ...' if len(real) > 4 else ''}). {', '.join(g.name(t) for t in contexts[:3])} "
                f"{'holds' if len(contexts) == 1 else 'each hold'} one and "
                f"{'calls' if len(contexts) == 1 else 'call'} it without knowing which.",
                peripheral=all(g.in_tests(t) for t in contexts))

    # Template method: a concrete method on a base type calls that type's own abstract or virtual methods,
    # and at least one subclass overrides one of them.
    for t in types:
        subs = g.subtypes(t)
        if not subs or g.attrs[t].get("native_kind") == "interface":
            continue
        own = g.methods(t)
        overridden = {m for m in own if g.inn["overrides"][m]}
        hooks_all = {m for m in overridden if g.attrs[m].get("is_virtual") or g.nodes[m]["language"] == "python"}
        if not hooks_all:
            continue
        for m in own:
            if m in hooks_all or g.name(m) in (".ctor", "__init__"):
                continue
            hooks = g.calls[m] & hooks_all
            sig = g.attrs[m].get("signature") or ""
            if hooks and " abstract " not in f" {sig} ":
                overriders = {g.owner(o) for h in hooks for o in g.inn["overrides"][h]} - {None}
                add("template method", m, {"template": [m], "step": list(hooks), "base": [t], "subclass": list(overriders)},
                    f"{g.name(t)}.{g.name(m)} is written once on the base and calls {', '.join(g.name(h) for h in sorted(hooks)[:4])}, "
                    f"which {len(overriders)} subclass{'es supply' if len(overriders) != 1 else ' supplies'}.")

    # Observer: event channels found by the indexer.
    by_subject = defaultdict(lambda: {"raiser": set(), "observer": set(), "event": set()})
    for src, dsts in g.out["communicates"].items():
        for dst in dsts:
            a = g.edge_attrs.get(("communicates", src, dst), {})
            if a.get("channel") != "event":
                continue
            subject = g.owner(src) or src
            key = (subject, a.get("address") or "")
            by_subject[key]["raiser"].add(src)
            by_subject[key]["observer"].add(dst)
    for (subject, address), r in by_subject.items():
        add("observer", f"{subject}|{address}", {"subject": [subject], "raised in": list(r["raiser"]), "observer": list(r["observer"])},
            f"{g.name(subject)} raises {address.split('.')[-1] or 'an event'}; {len(r['observer'])} handler"
            f"{'s elsewhere subscribe' if len(r['observer']) != 1 else ' elsewhere subscribes'} to it, so the raiser does not know who listens.")

    # Process boundary.
    for src, dsts in g.out["communicates"].items():
        for dst in dsts:
            a = g.edge_attrs.get(("communicates", src, dst), {})
            if a.get("channel") == "process":
                add("process boundary", f"{src}|{dst}", {"launcher": [src], "program": [dst]},
                    f"{g.qual(src)} starts {g.name(g.module.get(dst) or dst)} as a separate program"
                    f"{' and talks to it over its standard input and output' if a.get('pipes') else ''}. "
                    f"Nothing in the type system ties the two sides together.")

    # Factory: one function creates two or more types that share a supertype.
    for f, made in g.out["instantiates"].items():
        if g.kind(f) != "callable" or g.name(f) in (".ctor", "__init__") or g.in_tests(f):
            continue
        made = {m for m in made if g.kind(m) == "type"}
        if len(made) < 2:
            continue
        common = defaultdict(set)
        for m in made:
            for s in g.supertypes(m):
                common[s].add(m)
        best = max(common.items(), key=lambda kv: (len(kv[1]), kv[0]), default=None)
        if best and len(best[1]) >= 2:
            base, products = best
            if not g.methods(base):
                continue  # a marker type with no behavior: creating several of them is not a choice between implementations
            a = g.attrs[f]
            rets = [n.split("`")[0] for n in (a.get("returns_names") or ([a["returns"]] if a.get("returns") else []))]
            returns_base = bool(rets) and rets[0] == g.nodes[base]["name"]
            kinds = f"{len(products)} kinds of {g.name(base)} ({', '.join(g.name(p) for p in sorted(products)[:4])})"
            if returns_base:
                add("factory", f, {"factory": [f], "product": list(products), "product type": [base]},
                    f"{g.qual(f)} creates {kinds} and returns it as {g.name(base)}, so callers do not name the concrete type.", 0.8)
            else:
                add("factory", f, {"factory": [f], "product": list(products), "product type": [base]},
                    f"{g.qual(f)} decides which of {kinds} to create. It does not return one, so this is where the "
                    f"choice is wired in, not a factory others call.", 0.5)

    # Builder: several methods return the type itself (chaining) and one produces a different type.
    for t in types:
        if g.in_tests(t):
            continue
        tname = g.name(t)
        fluent, produce = [], []
        for m in g.methods(t):
            a = g.attrs[m]
            rets = [n.split("`")[0] for n in (a.get("returns_names") or ([a["returns"]] if a.get("returns") else []))]
            if g.name(m) in (".ctor", "__init__") or a.get("visibility") == "private":
                continue
            if rets and rets[0] == tname:
                fluent.append(m)
            elif g.name(m).lower().startswith(("build", "create")) and rets:
                produce.append((m, rets[0]))
        if len(fluent) >= 2 and produce:
            add("builder", t, {"builder": [t], "step": fluent, "build": [m for m, _ in produce]},
                f"{tname} has {len(fluent)} methods that return the {tname} itself, so calls chain, and "
                f"{g.name(produce[0][0])} turns what was collected into a {produce[0][1]}.")

    # Singleton: a static field of the type's own type, and no public constructor.
    for t in types:
        own_static = [f for f in g.fields(t) if g.attrs[f].get("is_static") and (g.attrs[f].get("type_name") or "").split("`")[0] == g.name(t)]
        ctors = [m for m in g.methods(t) if g.name(m) == ".ctor"]
        if own_static and ctors and all(g.attrs[c].get("visibility") in ("private", "protected") for c in ctors):
            add("singleton", t, {"singleton": [t], "instance": own_static},
                f"{g.name(t)} keeps an instance of itself in the static {g.name(own_static[0])} and its constructors "
                f"are not public, so every caller shares that one object.")
    return found


def run(con, repo_id: Optional[str] = None) -> dict:
    """Replace the matcher's labels with a fresh pass. Labels written by others are kept and checked for staleness."""
    found = detect(con)
    with con:
        old = [r[0] for r in con.execute("SELECT id FROM pattern_instances WHERE matcher = ?", (MATCHER,))]
        con.executemany("DELETE FROM pattern_roles WHERE instance_id = ?", [(i,) for i in old])
        con.execute("DELETE FROM pattern_instances WHERE matcher = ?", (MATCHER,))
        for p in found:
            evidence = [i for ids in p["roles"].values() for i in ids]
            con.execute("INSERT OR REPLACE INTO pattern_instances (id, pattern, matcher, rationale, confidence, evidence_hash, stale, attrs)"
                        " VALUES (?,?,?,?,?,?,0,?)",
                        (p["id"], p["pattern"], MATCHER, p["rationale"], p["confidence"], store.evidence_hash(con, evidence),
                         json.dumps({"peripheral": bool(p["in_tests"])})))
            con.executemany("INSERT INTO pattern_roles VALUES (?,?,?)",
                            [(p["id"], role, i) for role, ids in p["roles"].items() for i in ids])
        # Labels from agents: stale when the code behind them changed, dropped roles when a node is gone.
        for row in con.execute("SELECT id, evidence_hash FROM pattern_instances WHERE matcher != ?", (MATCHER,)).fetchall():
            ids = [r[0] for r in con.execute("SELECT node_id FROM pattern_roles WHERE instance_id = ?", (row["id"],))]
            con.execute("UPDATE pattern_instances SET stale = ? WHERE id = ?",
                        (int(store.evidence_hash(con, ids) != row["evidence_hash"]), row["id"]))
        counts = defaultdict(int)
        for p in found:
            counts[p["pattern"]] += 1
        if repo_id:
            con.execute("INSERT OR REPLACE INTO extractor_coverage VALUES (?,?,?,?,?,?)",
                        (repo_id, "patterns:structural", "0.1", "ok", None, json.dumps(dict(counts))))
    return dict(counts)


def label(con, pattern: str, roles: dict, rationale: str, confidence: float = 0.6, source: str = "mcp") -> dict:
    """Record a pattern the matchers did not find. `roles` maps a role name to node ids."""
    if not rationale.strip():
        return {"error": "a pattern label needs a rationale: say what in the code makes it this pattern"}
    known = {r[0] for r in con.execute("SELECT id FROM nodes")}
    clean = {role: [i for i in ids if i in known] for role, ids in roles.items()}
    missing = [i for ids in roles.values() for i in ids if i not in known]
    clean = {k: v for k, v in clean.items() if v}
    if not clean:
        return {"error": "none of the node ids exist in the store", "missing": missing[:5]}
    evidence = [i for ids in clean.values() for i in ids]
    pid = "pat:" + hashlib.sha1(f"{pattern}|{sorted(evidence)}|{source}".encode()).hexdigest()[:10]
    with con:
        con.execute("DELETE FROM pattern_roles WHERE instance_id = ?", (pid,))
        con.execute("INSERT OR REPLACE INTO pattern_instances (id, pattern, matcher, rationale, confidence, evidence_hash, stale)"
                    " VALUES (?,?,?,?,?,?,0)",
                    (pid, pattern.strip().lower(), source, rationale, max(0.0, min(1.0, confidence)), store.evidence_hash(con, evidence)))
        con.executemany("INSERT INTO pattern_roles VALUES (?,?,?)", [(pid, role, i) for role, ids in clean.items() for i in ids])
    return {"id": pid, "pattern": pattern.strip().lower(), "missing": missing[:10]}


def listing(con, pattern: Optional[str] = None, node_id: Optional[str] = None, include_tests: bool = False,
            limit: int = 100) -> dict:
    """Pattern labels, optionally of one kind or touching one node (or anything inside it)."""
    names = {r["id"]: (r["name"], r["kind"], r["path"]) for r in con.execute("SELECT id, name, kind, path FROM nodes")}
    test_mods = {r[0] for r in con.execute(
        "SELECT DISTINCT a.module_id FROM nodes n JOIN ancestry a ON a.node_id = n.id WHERE n.kind = 'test'")}
    module = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
    out, counts = [], defaultdict(int)
    for p in con.execute("SELECT * FROM pattern_instances ORDER BY confidence DESC, pattern, id"):
        roles = defaultdict(list)
        for r in con.execute("SELECT role, node_id FROM pattern_roles WHERE instance_id = ? ORDER BY role, node_id", (p["id"],)):
            roles[r["role"]].append(r["node_id"])
        ids = [i for v in roles.values() for i in v]
        if not ids:
            continue
        stored = json.loads(p["attrs"] or "{}").get("peripheral")
        in_tests = stored if stored is not None else sum(1 for i in ids if module.get(i) in test_mods) * 2 > len(ids)
        if in_tests and not include_tests and not node_id:
            continue
        if pattern and p["pattern"] != pattern.strip().lower():
            continue
        if node_id and not any(i == node_id or i.startswith(node_id + ".") or i.startswith(node_id + "/") for i in ids):
            continue
        counts[p["pattern"]] += 1
        if len(out) < limit:
            out.append({"id": p["id"], "pattern": p["pattern"], "about": ABOUT.get(p["pattern"], ""),
                        "rationale": p["rationale"], "confidence": p["confidence"],
                        "source": "structural matcher" if p["matcher"] == MATCHER else p["matcher"],
                        "stale": bool(p["stale"]), "in_tests": in_tests,
                        "roles": {role: [{"id": i, "name": names.get(i, (i,))[0]} for i in v] for role, v in roles.items()}})
    return {"total": sum(counts.values()), "by_pattern": dict(counts), "patterns": out,
            "note": "A label says the code has this shape. It does not say the author intended the pattern."}
