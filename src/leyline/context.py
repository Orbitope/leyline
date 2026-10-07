"""Context: a short outline of the code around a focus, ranked and cut to fit a token budget, for an agent that is
about to read or edit that code (after Aider's repository map).

The focus is what the agent is working on: node ids, names (`Owner.method`, `method`), file paths, a change
(`spec-<id>`, `pr-<id>`: the code its plan or review marked) or words to search for. Every function, type, field
and test on the map is ranked by personalized PageRank, seeded at the focus, over the links the map has:

    calls, overrides            1.0 each way
    channels (communicates)     1.0 each way: a request and the route that serves it, a writer and a reader
    extends, implements         0.8
    instantiates                0.6
    uses_type                   0.4
    reads, writes (a field)     0.3
    contains                    0.3 between a type and its members, 0.2 between a file and what sits at its top

Links run both ways: callers matter (they break when a signature changes) and so do callees (the agent calls
them). The rank is worked out by forward push (Andersen, Chung and Lang), which only visits code near the focus, so
it costs milliseconds however large the map. Code a test reaches, and code an entry point runs first, get a small
boost. The outline then shows, file by file, each chosen symbol's declaration line (not its body), with the focus
marked `>`, each channel end said in words ("answers GET /api/x", "writes table t"), and the symbols that call the
focus or that it calls said so, until the budget (tokens estimated as characters / 4) is spent. The last line says
what was left out.

The graph is read once per store and kept, keyed by the store's generation, so a server answers later calls
without reading it again.
"""

from __future__ import annotations

import json
import re
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from . import query, store

WEIGHTS = {"calls": 1.0, "overrides": 1.0, "communicates": 1.0, "extends": 0.8, "implements": 0.8,
           "instantiates": 0.6, "uses_type": 0.4, "reads": 0.3, "writes": 0.3}
IN_TYPE, IN_FILE = 0.3, 0.2      # containment: a member and its type; something at the top of a file and the file
MAX_PAIR = 1.5                   # two nodes linked several ways count a little more, not without bound
RESTART = 0.25                   # the walk's chance of going back to the focus at each step: higher keeps it nearer
EPS = 2e-6                       # forward push stops when what is left at a node is below this times its degree
TESTED_BOOST, ENTRY_BOOST = 1.15, 1.3
SHOWN = ("callable", "type", "field", "test")
LINE_MAX = 160                   # a declaration longer than this is cut
MIN_TOKENS, MAX_TOKENS = 200, 8000

# What a channel end does, from the sender's side and the receiver's; the address follows. A DI link names the other end.
CHANNEL_WORDS = {
    "http": ("requests", "answers"),
    "db": ("writes table", "reads table"),
    "file": ("writes file", "reads file"),
    "queue": ("publishes", "handles"),
    "event": ("raises", "handles event"),
    "process": ("launches", "is launched as"),
    "rpc": ("calls rpc", "serves rpc"),
    "di": ("is bound by DI to", "is registered by DI for"),
}


class Graph:
    """The map as the ranking reads it: nodes by number, weighted links both ways, and what each node is."""

    def __init__(self, con):
        self.ids: list[str] = []
        self.index: dict[str, int] = {}
        self.kind: list[str] = []
        self.name: list[str] = []
        self.parent: list[int] = []
        self.path: list[Optional[str]] = []
        self.line: list[int] = []
        self.raw: list[Optional[str]] = []
        for r in con.execute("SELECT id, kind, name, parent_id, path, span_start, attrs FROM nodes"
                             " WHERE kind IN ('file', 'type', 'callable', 'field', 'test', 'entry_point')"):
            self.index[r[0]] = len(self.ids)
            self.ids.append(r[0])
            self.kind.append(r[1])
            self.name.append(r[2])
            self.parent.append(r[3])
            self.path.append(r[4])
            self.line.append(r[5] or 0)
            self.raw.append(r[6])
        ix = self.index
        self.parent = [ix.get(p, -1) if p else -1 for p in self.parent]
        pair: dict[tuple, dict] = defaultdict(dict)

        def link(a, b, kind, w):
            if a is None or b is None or a == b:
                return
            key = (a, b) if a < b else (b, a)
            pair[key][kind] = max(pair[key].get(kind, 0.0), w)

        for s, d in con.execute("SELECT DISTINCT src_id, dst_id FROM calls"):
            link(ix.get(s), ix.get(d), "calls", WEIGHTS["calls"])
        kinds = [k for k in WEIGHTS if k not in ("calls", "communicates")]
        for k, s, d in con.execute(f"SELECT kind, src_id, dst_id FROM edges WHERE kind IN ({','.join('?' * len(kinds))})", kinds):
            link(ix.get(s), ix.get(d), k, WEIGHTS[k])
        # Channel ends, kept to be said in words: node -> [(side, channel, address, other end)].
        self.channels: dict[int, list] = defaultdict(list)
        for s, d, a in con.execute("SELECT src_id, dst_id, attrs FROM edges WHERE kind = 'communicates'"):
            si, di = ix.get(s), ix.get(d)
            link(si, di, "communicates", WEIGHTS["communicates"])
            try:
                attrs = json.loads(a) if a else {}
            except ValueError:
                attrs = {}
            ch, addr = attrs.get("channel") or "channel", str(attrs.get("address") or "")
            if si is not None:
                self.channels[si].append((0, ch, addr, di))
            if di is not None:
                self.channels[di].append((1, ch, addr, si))
        for i, p in enumerate(self.parent):
            if p < 0 or self.kind[i] == "entry_point":
                continue
            if self.kind[p] == "file":
                link(i, p, "contains", IN_FILE)
            elif self.kind[p] in ("type", "callable"):
                link(i, p, "contains", IN_TYPE)
        n = len(self.ids)
        self.adj: list[list] = [[] for _ in range(n)]
        for (a, b), ks in pair.items():
            w = min(sum(ks.values()), MAX_PAIR)
            self.adj[a].append((b, w))
            self.adj[b].append((a, w))
        self.deg = [sum(w for _, w in nb) for nb in self.adj]
        # Direct calls, to say "calls the focus" and "called by the focus".
        self.callees: dict[int, set] = defaultdict(set)
        self.callers: dict[int, set] = defaultdict(set)
        for s, d in con.execute("SELECT DISTINCT src_id, dst_id FROM calls"):
            si, di = ix.get(s), ix.get(d)
            if si is not None and di is not None and si != di:
                self.callees[si].add(di)
                self.callers[di].add(si)
        self.boost = [1.0] * n
        for (cid,) in con.execute(store.TESTED):
            if cid in ix:
                self.boost[ix[cid]] = TESTED_BOOST
        for (eid,) in con.execute("SELECT dst_id FROM edges WHERE kind = 'exposes'"):
            e = ix.get(eid)
            if e is None:
                continue
            # What a program runs first: the code an entry point exposes, and what that calls directly.
            for x in (e, *self.callees.get(e, ())):
                self.boost[x] = max(self.boost[x], ENTRY_BOOST)


_cache: dict = {}
_cache_lock = threading.Lock()


def graph(con) -> Graph:
    """The store's graph, read once per index run (the store's generation) and kept."""
    path = str(store.store_file(con))
    gen = con.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
    stamp = (gen[0] if gen else None, con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    with _cache_lock:
        hit = _cache.get(path)
        if hit is not None and hit[0] == stamp and gen is not None:
            return hit[1]
    g = Graph(con)
    with _cache_lock:
        _cache[path] = (stamp, g)
    return g


def rank(g: Graph, seeds: dict[int, float]) -> dict[int, float]:
    """Personalized PageRank from `seeds` (node -> weight), by forward push: {node: score}, scores summing to
    about 1. Only nodes the walk reaches get a score."""
    total = sum(seeds.values()) or 1.0
    r: dict[int, float] = {u: w / total for u, w in seeds.items()}
    p: dict[int, float] = defaultdict(float)
    queue, queued = deque(r), set(r)
    adj, deg = g.adj, g.deg
    while queue:
        u = queue.popleft()
        queued.discard(u)
        ru = r.get(u, 0.0)
        du = deg[u]
        if ru <= 0.0 or (du and ru < EPS * du):
            continue
        r[u] = 0.0
        if not du:      # nothing leads on: what reaches it stays there
            p[u] += ru
            continue
        p[u] += RESTART * ru
        share = (1.0 - RESTART) * ru / du
        for v, w in adj[u]:
            rv = r.get(v, 0.0) + share * w
            r[v] = rv
            if v not in queued and rv >= EPS * deg[v]:
                queue.append(v)
                queued.add(v)
    return p


# -- what the focus names ------------------------------------------------------------------------
def _change_nodes(con, cid: str) -> list[str]:
    """The code a change's plan or review marked: what it changes, adds, must edit, and contracts it must keep."""
    ids: list[str] = []
    row = con.execute("SELECT spec FROM views WHERE id = ?", ("view-" + cid,)).fetchone()
    if row is not None:
        try:
            marks = json.loads(row[0] or "{}").get("marks", [])
        except ValueError:
            marks = []
        ids += [m["id"] for m in marks if isinstance(m, dict) and m.get("role") in ("changed", "new", "must_edit", "contract")]
    for (nodes,) in con.execute("SELECT nodes FROM spec_items WHERE change_id = ? AND kind = 'task'", (cid,)):
        try:
            ids += json.loads(nodes or "[]")
        except ValueError:
            pass
    return list(dict.fromkeys(ids))


def _is_change(con, cid: str) -> bool:
    return con.execute("SELECT 1 FROM views WHERE id = ? UNION SELECT 1 FROM spec_items WHERE change_id = ?",
                       ("view-" + cid, cid)).fetchone() is not None


WORD_STOP = set("a an the and or of to in on for with from by is are was be it its this that how what where when"
                " which who does do code function method class file files".split())


def _search(con, g: Graph, text: str, most: int = 6) -> list[str]:
    """Nodes for words: the whole text searched as one name, and each word alone. A node found scores by how many of
    the words its id and path hold, then by kind (functions and types before files, fields last), then by how near
    the top of a search it came."""
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9_]+", text) if len(w) > 2 and w.lower() not in WORD_STOP]
    near: dict[str, float] = defaultdict(float)
    for q in [text] + (words[:8] if len(words) > 1 else []):
        for k, h in enumerate(query.search(con, q, limit=30)["results"]):
            if h["id"] in g.index and _search_ok(g, h["id"]):
                near[h["id"]] = max(near[h["id"]], 1.0 / (k + 1))
    kind_rank = {"callable": 1.0, "type": 1.0, "test": 0.6, "file": 0.5, "field": 0.0}

    def score(i):
        u = g.index[i]
        hay = (i + " " + (g.path[u] or "")).lower()
        return (sum(w in hay for w in words), kind_rank.get(g.kind[u], 0.0) + near[i])
    return sorted(near, key=lambda i: (tuple(-x for x in score(i)), i))[:most]


def _search_ok(g: Graph, i: str) -> bool:
    u = g.index[i]
    return g.kind[u] == "file" or _shown(g, u)


def resolve_focus(con, g: Graph, focus: list[str]) -> tuple[dict, list[dict]]:
    """Each focus item turned into nodes on the map. ({node: weight}, [{"focus", "as", "ids"} or {"focus", "error"}])."""
    seeds: dict[int, float] = defaultdict(float)
    found: list[dict] = []
    for raw in focus:
        text = (raw or "").strip()
        if not text:
            continue
        ids, how, cid = [], "", None
        if text in g.index:
            ids, how = [text], "node"
        elif re.fullmatch(r"(spec|pr)-\S+", text) and _is_change(con, text):
            cid = text
        if not ids and cid is None and " " not in text:
            r = query.resolve(con, text)
            if "id" in r:
                ids, how = [r["id"]], "name"
            elif r.get("candidates") and "could be" in r.get("error", ""):
                ids, how = [c["id"] for c in r["candidates"][:8]], "name, several things"
            elif "/" not in text and _is_change(con, "spec-" + text):   # a change folder's id, as plan takes it
                cid = "spec-" + text
        if cid is not None:
            ids, how = _change_nodes(con, cid), f"change {cid}"
            if not ids:
                found.append({"focus": text, "error": f"the change {cid} marks no code yet: run `leyline plan` on it"})
                continue
        if not ids:
            ids, how = _search(con, g, text), "search"
        ids = [i for i in ids if i in g.index]
        if not ids:
            found.append({"focus": text, "error": "nothing on the map matches it"})
            continue
        # Each focus item weighs the same, shared among its nodes; search hits nearer the top weigh more.
        ws = [1.0 / (k + 1) if how == "search" else 1.0 for k in range(len(ids))]
        for i, w in zip(ids, ws):
            u = g.index[i]
            seeds[u] += w / sum(ws)
            if g.kind[u] == "file":   # a file: what sits at its top, as well as the file
                kids = [k for k, p in enumerate(g.parent) if p == u and g.kind[k] in SHOWN]
                for k in kids:
                    seeds[k] += w / sum(ws) / max(len(kids), 1)
        found.append({"focus": text, "as": how, "ids": ids})
    return dict(seeds), found


# -- the outline ---------------------------------------------------------------------------------
def _short(g: Graph, u: int) -> str:
    if g.kind[u] == "test":
        n = g.name[u]
        return "test " + (n if len(n) <= 40 else n[:37].rstrip() + "...")
    p = g.parent[u]
    if p >= 0 and g.kind[p] == "type":
        return f"{g.name[p]}.{g.name[u]}"
    return g.name[u]


def _shown(g: Graph, u: int) -> bool:
    return g.kind[u] in SHOWN and g.name[u] != "<module>" and bool(g.path[u])


def _ancestors(g: Graph, u: int) -> list[int]:
    """The types and functions a node sits in, outermost first, up to its file."""
    out, p, seen = [], g.parent[u], 0
    while p >= 0 and g.kind[p] != "file" and seen < 20:
        if _shown(g, p):
            out.append(p)
        p, seen = g.parent[p], seen + 1
    return out[::-1]


class _Sources:
    """Declaration lines read from the working tree, for nodes the map keeps no signature for."""

    def __init__(self, con):
        self.roots = store.roots(con)
        self.files: dict = {}

    def line(self, node_id: str, path: str, line: int) -> Optional[str]:
        repo = node_id.split(":", 1)[0]
        root = self.roots.get(repo)
        if root is None or not path or not line:
            return None
        key = (repo, path)
        if key not in self.files:
            from .indexer import source_lines
            try:
                self.files[key] = source_lines(Path(root) / path)
            except OSError:
                self.files[key] = []
        lines = self.files[key]
        return lines[line - 1].strip() if 0 < line <= len(lines) else None


def _declaration(g: Graph, u: int, sources: _Sources) -> str:
    try:
        a = json.loads(g.raw[u]) if g.raw[u] else {}
    except ValueError:
        a = {}
    kind, name = g.kind[u], g.name[u]
    if kind == "test":
        text = "test " + json.dumps(a.get("full_name") or a.get("signature") or name, ensure_ascii=False)
    elif kind == "field":
        text = f"{name}: {a['declared_type']}" if a.get("declared_type") else name
    else:
        text = a.get("signature") or sources.line(g.ids[u], g.path[u], g.line[u]) or f"{kind} {name}"
    text = re.sub(r"\s+", " ", text).strip().rstrip("{").rstrip()
    # Some signatures are kept cut just before the body, losing the last `>` of a generic return type.
    opened = text.count("<") - text.count(">") + text.count("=>")
    if 0 < opened <= 2 and re.search(r"<[\w\s,.|\[\]<>]*$", text):
        text += ">" * opened
    return text if len(text) <= LINE_MAX else text[:LINE_MAX - 3].rstrip() + "..."


def _notes(g: Graph, u: int, focus: set, names: dict) -> str:
    """What to know about a node besides its declaration: its channel ends in words, and how it meets the focus."""
    said: dict[str, list] = {}
    for side, ch, addr, end in g.channels.get(u, ()):
        verb = CHANNEL_WORDS.get(ch, ("sends on " + ch, "receives on " + ch))[side]
        what = (_short(g, end) if end is not None else "?") if ch == "di" else (addr or "?")
        if what not in said.setdefault(verb, []):
            said[verb].append(what)
    phrases = [verb + " " + ", ".join(xs[:3]) + (f" and {len(xs) - 3} more" if len(xs) > 3 else "")
               for verb, xs in said.items()]
    if len(phrases) > 3:
        phrases = phrases[:3] + [f"{len(phrases) - 3} more kinds of link"]
    said, other = phrases, []
    notes = ["; ".join(said)] if said else []
    if u in focus:
        n_in, n_out = len(g.callers.get(u, ())), len(g.callees.get(u, ()))
        counts = ([f"called from {n_in} place{'s' * (n_in != 1)}"] if n_in else []) + ([f"calls {n_out}"] if n_out else [])
        if counts:
            notes.append(", ".join(counts))
    else:
        calls = [names[f] for f in focus if f in g.callees.get(u, ())]
        called = [names[f] for f in focus if f in g.callers.get(u, ())]
        for verb, xs in (("calls", calls), ("called by", called)):
            if xs:
                other.append(f"{verb} " + ", ".join(sorted(xs)[:2]) + (f" and {len(xs) - 2} more" if len(xs) > 2 else ""))
    return "; ".join(notes + other)


def build(con, focus: list[str] | str, budget_tokens: int = 2000) -> dict:
    """The outline for a focus within a token budget: {"text", "tokens", "focus", "shown", "left_out", ...}."""
    import time
    t0 = time.perf_counter()
    focus = [focus] if isinstance(focus, str) else list(focus or [])
    budget = max(MIN_TOKENS, min(int(budget_tokens or 2000), MAX_TOKENS))
    g = graph(con)
    t_load = time.perf_counter() - t0
    seeds, found = resolve_focus(con, g, focus)
    if not seeds:
        why = "; ".join(f"{f['focus']!r}: {f['error']}" for f in found) or "no focus given"
        return {"error": f"Nothing to start from ({why}). Give node ids, names such as Owner.method, file paths, a"
                         " change id (spec-<id>, pr-<id>) or words to search for.", "focus": found}
    scores = rank(g, seeds)
    fset = {u for u in seeds if _shown(g, u)}
    for u in seeds:   # a file named as the focus: what sits at its top is the focus
        if g.kind[u] == "file":
            fset |= {k for k, p in enumerate(g.parent) if p == u and _shown(g, k) and k in seeds}
    names = {u: _short(g, u) for u in fset}
    ranked = sorted((u for u in scores if _shown(g, u) and u not in fset),
                    key=lambda u: (-scores[u] * g.boost[u], g.ids[u]))
    order = sorted(fset, key=lambda u: -seeds.get(u, 0.0)) + ranked
    t_rank = time.perf_counter() - t0 - t_load

    sources = _Sources(con)
    budget_chars = budget * 4
    head_names = [f["focus"] for f in found if "ids" in f]
    header = f"Code around {', '.join(head_names)[:200]}: declarations, the most related files first; > marks the focus."
    reserve = len(header) + 520          # the header, the summary line and the left-out line
    chosen: set[int] = set()
    lines_of: dict[int, tuple] = {}      # node -> (declaration, notes)
    files: dict[str, float] = {}         # path -> best score in it, for the order of files
    used, misses, left = reserve, 0, []

    def cost(u):
        decl, note = lines_of.get(u) or (_declaration(g, u, sources), _notes(g, u, fset, names))
        lines_of[u] = (decl, note)
        depth = len(_ancestors(g, u))
        return 2 + 2 * depth + len(decl) + 1 + ((6 + 2 * depth + len(note) + 1) if note else 0)

    for pos, u in enumerate(order):
        need = [a for a in _ancestors(g, u) if a not in chosen] + [u]
        c = sum(cost(x) for x in need) + (0 if g.path[u] in files else len(g.path[u]) + 2)
        if used + c > budget_chars and not (u in fset and not chosen):
            left.append(u)
            misses += 1
            if misses > 40:   # nothing small enough is coming: stop looking
                left += [x for x in order[pos + 1:]]
                break
            continue
        used += c
        chosen.update(need)
        files[g.path[u]] = max(files.get(g.path[u], 0.0), 10.0 if u in fset else scores.get(u, 0.0) * g.boost[u])

    # Render: files by their best score, inside each by line, members under their type.
    out = [header, ""]
    for path in sorted(files, key=lambda p: (-files[p], p)):
        out.append(path)
        for u in sorted((x for x in chosen if g.path[x] == path), key=lambda x: (g.line[x], len(_ancestors(g, x)))):
            depth = len(_ancestors(g, u))
            decl, note = lines_of.get(u) or (_declaration(g, u, sources), _notes(g, u, fset, names))
            out.append(("> " if u in fset else "  ") + "  " * depth + decl)
            if note:
                out.append("  " + "  " * depth + "    -- " + note)
        out.append("")
    near = _hops(g, set(seeds), 2)
    related = [u for u in left if u in near]
    direct = sum(near[u] == 1 for u in related)
    left_files = sorted({g.path[u] for u in related})
    out.append(f"Shown: {len(chosen)} symbol{'s' * (len(chosen) != 1)} in {len(files)} file{'s' * (len(files) != 1)}.")
    if related:
        top = ", ".join(f"{_short(g, u)} ({g.path[u]})" for u in related[:3])
        out.append(f"Left out: {direct} more symbol{'s' * (direct != 1)} linked directly to the focus and"
                   f" {len(related) - direct} two links away, in {len(left_files)} file{'s' * (len(left_files) != 1)};"
                   f" the nearest: {top}. Raise the budget to see more.")
    else:
        out.append("Left out: nothing within two links of the focus.")
    for f in found:
        if "error" in f:
            out.append(f"Not found: {f['focus']!r} ({f['error']}).")
    text = "\n".join(out).rstrip() + "\n"
    return {"focus": found, "budget_tokens": budget, "tokens": len(text) // 4, "text": text,
            "shown": {"symbols": len(chosen), "files": len(files)},
            "left_out": {"direct": direct, "two_links_away": len(related) - direct, "files": len(left_files),
                         "nearest": [{"id": g.ids[u], "name": _short(g, u), "path": g.path[u]} for u in related[:10]]},
            "seconds": {"load": round(t_load, 3), "rank": round(t_rank, 3),
                        "total": round(time.perf_counter() - t0, 3)}}


def _hops(g: Graph, start: set, most: int) -> dict[int, int]:
    """How many links each node is from the nearest of `start`, up to `most`."""
    dist = {u: 0 for u in start}
    frontier = list(start)
    for d in range(1, most + 1):
        nxt = []
        for u in frontier:
            for v, _w in g.adj[u]:
                if v not in dist:
                    dist[v] = d
                    nxt.append(v)
        frontier = nxt
    return dist
