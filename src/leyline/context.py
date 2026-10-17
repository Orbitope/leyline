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
them). A broad hub (an error class every script raises, a route most of the client requests) links code that shares
nothing else, so a link through a node with more than HUB links counts for less, and such a node, unless it is
linked to the focus, is shown in proportion to how few links it has. The rank is worked out by forward push (Andersen, Chung and Lang), which only visits code near the focus, so
it costs milliseconds however large the map. Code a test reaches, and code an entry point runs first, get a small
boost. The outline then shows, file by file, each chosen symbol's declaration line (not its body), with the focus
marked `>`, each channel end said in words ("answers GET /api/x", "writes table t"), and the symbols that call the
focus or that it calls said so, until the budget (tokens estimated as characters / 4) is spent. The last line says
what was left out.

The graph is read once per store and kept, keyed by the store's generation, so a server answers later calls
without reading it again.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from . import diff, query, store, summaries
from .diagrams import cut as diagrams_cut

WEIGHTS = {"calls": 1.0, "overrides": 1.0, "communicates": 1.0, "extends": 0.8, "implements": 0.8,
           "instantiates": 0.6, "uses_type": 0.4, "reads": 0.3, "writes": 0.3}
IN_TYPE, IN_FILE = 0.3, 0.2      # containment: a member and its type; something at the top of a file and the file
MAX_PAIR = 1.5                   # two nodes linked several ways count a little more, not without bound
HUB = 12                         # a node with more links than this is a hub: each of its links counts sqrt(HUB/links)
RESTART = 0.25                   # the walk's chance of going back to the focus at each step: higher keeps it nearer
EPS = 2e-6                       # forward push stops when what is left at a node is below this times its degree
TESTED_BOOST, ENTRY_BOOST = 1.15, 1.3
SHOWN = ("callable", "type", "field", "test")
LINE_MAX = 160                   # a declaration longer than this is cut
MIN_TOKENS, MAX_TOKENS = 200, 8000
SUMMARY_WIDTH = 120              # characters of the summary under the focus and the code linked directly to it
# With `bodies`: what is shown of the code itself.
BODY_LINES = 80                  # a focus function's body is cut after this many lines
CALLER_LINES = 40                # lines of a caller shown around the call
TOP_LEVEL_LINES = 16             # lines shown around a call made from the top level of a file
FOCUS_BODIES = 3                 # focus functions that get a body, the highest ranked
FIELD_LINES = 12                 # field lines of a type
BODY_MISSES = 10                 # bodies refused for size before packing stops
BODIES_MIN_TOKENS = 600          # the least budget bodies work with
AVG_LINE_CHARS = 40              # what a line of code is taken to cost before it is read
LEFT_OUT_LISTED = 10             # bodies named under "Bodies left out"; the rest are counted
BLANKS = " \t\r\n\x0b\x0c"        # what bytes.strip takes off a line, as the index hashed it

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
        self.end: list[int] = []
        self.hash: list[Optional[str]] = []
        self.raw: list[Optional[str]] = []
        for r in con.execute("SELECT id, kind, name, parent_id, path, span_start, attrs, span_end, content_hash FROM nodes"
                             " WHERE kind IN ('file', 'type', 'callable', 'field', 'test', 'entry_point')"):
            self.index[r[0]] = len(self.ids)
            self.ids.append(r[0])
            self.kind.append(r[1])
            self.name.append(r[2])
            self.parent.append(r[3])
            self.path.append(r[4])
            self.line.append(r[5] or 0)
            self.raw.append(r[6])
            self.end.append(r[7] or 0)
            self.hash.append(r[8])
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
        # A broad hub (an error class every scenario raises, a route half the client requests, a registrar of every
        # route) links things that share nothing else: a link through it counts for less, the more it links.
        links = [0] * n
        for a, b in pair:
            links[a] += 1
            links[b] += 1
        damp = [min(1.0, (HUB / c) ** 0.5) if c else 1.0 for c in links]
        for (a, b), ks in pair.items():
            w = min(sum(ks.values()), MAX_PAIR) * damp[a] * damp[b]
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
        # A walk piles up at a hub because so much leads to it, not because it is near: past HUB links, a hub's score
        # is taken in proportion to how many it has, as a reason to show it (unless it is linked to the focus itself).
        self.hub = [min(1.0, HUB / c) if c else 1.0 for c in links]
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
    out = []
    for i in dict.fromkeys(ids):
        if con.execute("SELECT 1 FROM nodes WHERE id = ?", (i,)).fetchone() is None and i.endswith(")"):
            # A method marked when the change was planned whose parameters have changed since: the same owner and name
            base = diff._base(i)
            out += [r[0] for r in con.execute("SELECT id FROM nodes WHERE id > ? AND id < ? AND kind = 'callable'",
                                              (base + "(", base + "*"))
                    if diff._base(r[0]) == base]
        else:
            out.append(i)
    return list(dict.fromkeys(out))


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
        self.refused: dict = {}

    def file(self, node_id: str, path: Optional[str]) -> Optional[list]:
        """The lines of a mapped file, read from the working tree; None when it cannot be (`why` says why). A file that
        has become a link to one outside the repository is not read, as `source` does not read it."""
        repo = node_id.split(":", 1)[0]
        root = self.roots.get(repo)
        if root is None or not path:
            return None
        key = (repo, path)
        if key not in self.files:
            from .indexer import source_lines
            full = Path(root) / path
            if not store.inside(full, root):
                self.files[key], self.refused[key] = None, "a link out of the repository: not read"
            else:
                try:
                    self.files[key] = source_lines(full)
                except OSError:
                    self.files[key], self.refused[key] = None, "the file cannot be read"
        return self.files[key]

    def why(self, node_id: str, path: Optional[str]) -> str:
        return self.refused.get((node_id.split(":", 1)[0], path or ""), "the file is not on disk")

    def line(self, node_id: str, path: str, line: int) -> Optional[str]:
        if not line:
            return None
        lines = self.file(node_id, path) or []
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


# -- the code itself, when asked for (`bodies`) ----------------------------------------------------
def _span(g: Graph, u: int) -> list[tuple[int, int]]:
    """The line ranges of a node: one for each definition, since a function defined in both branches of an if is one
    node over both, and what sits between them is not its code."""
    try:
        a = json.loads(g.raw[u]) if g.raw[u] else {}
    except ValueError:
        a = {}
    defs = a.get("definitions")
    if defs:
        return [(int(s), int(e or s)) for s, e in defs]
    start = g.line[u]
    return [(start, max(g.end[u], start))] if start else []


def _hash_of(lines: list[str], spans: list[tuple[int, int]]) -> str:
    """The content hash the index made of a node, from its lines as they are now (see Indexer._parse_file and _add)."""
    h = None
    for a, b in spans:
        one = hashlib.sha1(b"\n".join(ln.strip(BLANKS).encode("utf-8") for ln in lines[a - 1:b])).hexdigest()[:16]
        h = one if h is None else hashlib.sha1(f"{h}|{one}".encode()).hexdigest()[:16]
    return h or ""


def _body(g: Graph, u: int, sources: "_Sources", around: Optional[int] = None, limit: int = BODY_LINES) -> dict:
    """The lines of a node, read from the working tree: {"text": [lines], "ranges": [(first, last)], "cut": how it was
    cut or None}, or {"why": ...} when they cannot be shown. A file that is a link out of the repository is not read; one
    edited since the map (its lines no longer hash to what the index saw) is not shown, since the map's line numbers
    would pick the wrong lines. A definition longer than `limit` is cut around the line `around` (a call), or after its
    first `limit` lines."""
    spans = _span(g, u)
    if not spans:
        return {"why": "it has no source span on the map"}
    lines = sources.file(g.ids[u], g.path[u])
    if lines is None:
        return {"why": sources.why(g.ids[u], g.path[u])}
    if g.hash[u] and g.end[u] and _hash_of(lines, spans) != g.hash[u]:
        return {"why": "changed since the map: map again"}
    room, ranges, text, cut = limit, [], [], None
    for k, (a, b) in enumerate(spans):
        if room <= 0:
            cut = cut or f"the other {len(spans) - k} definition(s) are not shown"
            break
        n = b - a + 1
        if n <= room:
            s, e = a, b
        elif around is not None and a <= around <= b:
            s = max(a, min(around - room // 2, b - room + 1))
            e = s + room - 1
            cut = f"lines {s}-{e} of {a}-{b}, around the call at line {around}"
        else:
            s, e = a, a + room - 1
            cut = f"cut after line {e} of {a}-{b}"
        if text:
            text.append("...")
        text += [ln.rstrip() for ln in lines[s - 1:e]]
        ranges.append((s, e))
        room -= e - s + 1
    return {"text": text, "ranges": ranges, "cut": cut}


def _type_lines(con, g: Graph, u: int, sources: "_Sources") -> list[str]:
    """A type as a declaration: its first line and at most FIELD_LINES lines declaring its fields, and how many more
    there are. Its methods are not shown."""
    fields = [g.index[r[0]] for r in con.execute(
        "SELECT id FROM nodes WHERE parent_id = ? AND kind = 'field' ORDER BY span_start, id", (g.ids[u],)) if r[0] in g.index]
    out = [_declaration(g, u, sources)] + ["    " + _declaration(g, k, sources) for k in fields[:FIELD_LINES]]
    if len(fields) > FIELD_LINES:
        out.append(f"    and {len(fields) - FIELD_LINES} more fields")
    return out


def _is_test_code(g: Graph, u: int) -> bool:
    return g.kind[u] == "test" or bool(re.search(r"(^|/)(tests?|spec)/|(^|/)test_[^/]*$|_test\.\w+$|\.test\.\w+$", g.path[u] or ""))


def _callers_of(con, g: Graph, funcs: list[int], weight) -> list[dict]:
    """The direct callers of some functions, with the line of the first call: those that are not test code first, then
    the nearest in the rank. A caller found by a guess says so, and so does one of the method a focus function overrides
    (it calls that method, which this one answers)."""
    found: dict[int, dict] = {}

    def add(rows, through):
        for src, line, precision in rows:
            u = g.index.get(src)
            if u is not None and u not in funcs and u not in found:
                found[u] = {"u": u, "line": line or 0, "guessed": precision == "guess", "through": through}
    for f in funcs:
        add(con.execute("SELECT src_id, site_start, precision FROM calls WHERE dst_id = ? ORDER BY site_start", (g.ids[f],)), None)
        for (base,) in con.execute("SELECT dst_id FROM edges WHERE kind = 'overrides' AND src_id = ?", (g.ids[f],)):
            add(con.execute("SELECT src_id, site_start, precision FROM calls WHERE dst_id = ? ORDER BY site_start", (base,)),
                _short(g, g.index[base]) if base in g.index else base)
    return sorted(found.values(), key=lambda c: (_is_test_code(g, c["u"]), -weight(c["u"]), g.ids[c["u"]]))


def _focus_types(con, g: Graph, funcs: list[int], own: list[int]) -> list[int]:
    """The types the focus is about: those given as the focus, and those its functions take as a parameter or return, by
    the role the map keeps on each type link."""
    out, seen = [], set()
    for u in own:
        if u not in seen:
            seen.add(u)
            out.append(u)
    for f in funcs:
        for dst, attrs in con.execute("SELECT dst_id, attrs FROM edges WHERE kind = 'uses_type' AND src_id = ?", (g.ids[f],)):
            try:
                role = (json.loads(attrs) if attrs else {}).get("role")
            except ValueError:
                role = None
            u = g.index.get(dst)
            if role in ("param", "return") and u is not None and g.kind[u] == "type" and u not in seen:
                seen.add(u)
                out.append(u)
    return sorted(out, key=lambda u: (g.name[u], g.ids[u]))


def _bodies(con, g: Graph, sources: "_Sources", fset: set, seeds: dict, weight, room: int) -> tuple[list[str], list[dict], int]:
    """The code to read, packed into `room` characters: the focus functions (at most FOCUS_BODIES, the highest ranked),
    the types the focus takes or returns, as declarations, and the lines around each call in their direct callers. What
    fits is decided from the lengths the map holds before any file is read, packing stops after BODY_MISSES bodies
    refused for size, and a text that is already shown is not shown again. Returns (the lines of the text, what each
    body is: id, name, role, path, lines, shown whole, cut or left out, and why, with at most LEFT_OUT_LISTED of those
    left out, and how many more were left out)."""
    funcs = sorted((u for u in fset if g.kind[u] in ("callable", "test")), key=lambda u: (-seeds.get(u, 0.0), g.ids[u]))
    shown_funcs, more_funcs = funcs[:FOCUS_BODIES], funcs[FOCUS_BODIES:]
    cands = [{"role": "focus", "u": u, "limit": BODY_LINES} for u in shown_funcs]
    # The types come before the callers: they are short, a function cannot be read without them, and a hub's many
    # callers would otherwise use the room up first.
    for u in _focus_types(con, g, shown_funcs, [u for u in fset if g.kind[u] == "type"]):
        cands.append({"role": "type", "u": u})
    for c in _callers_of(con, g, shown_funcs, weight):
        top = g.name[c["u"]].startswith("<")
        cands.append({"role": "caller", "u": c["u"], "limit": TOP_LEVEL_LINES if top else CALLER_LINES,
                      "around": c["line"] or None, "note": ("guessed" if c["guessed"] else "")
                      + (f"through {c['through']}" if c["through"] else "")})
    reserve = min(1200, room // 3)             # what the lines naming what was left out may take
    room -= reserve
    blocks: list[list[str]] = []
    meta: list[dict] = []
    left: list[dict] = []
    covered: dict[str, list] = defaultdict(list)   # path -> line ranges already shown
    misses, first = 0, True

    def refuse(c, why, count=True):
        u = c["u"]
        spans = _span(g, u)
        left.append({"id": g.ids[u], "name": _short(g, u), "role": c["role"], "path": g.path[u] or "",
                     **({"lines": [spans[0][0], spans[-1][1]]} if spans else {}), "shown": "left out", "why": why})

    for k, c in enumerate(cands):
        u, role = c["u"], c["role"]
        path = g.path[u] or ""
        head = f"{'>' if role == 'focus' else ' '} {path}:"
        if role == "type":
            text = _type_lines(con, g, u, sources)
            size = sum(len(x) + 5 for x in text) + len(head) + 40
            if size > room:
                refuse(c, "too long for what is left")
                misses += 1
            else:
                spans = _span(g, u)
                room -= size
                blocks.append([f"{head}{spans[0][0] if spans else ''}  {_short(g, u)}  [a type the focus takes or returns]"]
                              + ["    " + x for x in text])
                meta.append({"id": g.ids[u], "name": _short(g, u), "role": role, "path": path, "shown": "whole",
                             **({"lines": [spans[0][0], spans[-1][1]]} if spans else {})})
        else:
            spans = _span(g, u)
            wanted = sum(min(b - a + 1, c["limit"]) for a, b in spans)
            est = wanted * AVG_LINE_CHARS + 120
            if est > room and not (role == "focus" and first):
                refuse(c, f"too long for what is left ({sum(b - a + 1 for a, b in spans)} lines)")
                misses += 1
            else:
                limit = c["limit"]
                if est > room:        # the first focus function is always shown, cut to what there is
                    limit = max(6, (room - 120) // AVG_LINE_CHARS)
                got = _body(g, u, sources, c.get("around"), limit)
                if "why" in got:
                    refuse(c, got["why"])
                elif any(not (e < a or s > b) for s, e in got["ranges"] for a, b in covered[path]):
                    pass              # it is, or contains, text already shown
                else:
                    text, cut = got["text"], got["cut"]
                    while text and sum(len(x) + 5 for x in text) + 160 > room and len(text) > 3:
                        text = text[:-1]
                        cut = cut or "cut to fit"
                    if sum(len(x) + 5 for x in text) + 160 > room:
                        refuse(c, "too long for what is left")
                        misses += 1
                    else:
                        if len(text) < len(got["text"]):
                            cut = f"cut after line {got['ranges'][0][0] + len(text) - 1}"
                        room -= sum(len(x) + 5 for x in text) + 160
                        covered[path] += got["ranges"]
                        a0, b0 = got["ranges"][0][0], got["ranges"][-1][1]
                        why = {"focus": "the focus", "caller": "calls the focus"
                               + (f" at line {c['around']}" if c.get("around") else "")}[role]
                        if c.get("note"):
                            why += f" ({c['note']})"
                        blocks.append([f"{head}{a0}-{b0}  {_short(g, u)}  [{why}" + (f"; {cut}" if cut else "") + "]"]
                                      + ["    " + x for x in text])
                        meta.append({"id": g.ids[u], "name": _short(g, u), "role": role, "path": path,
                                     "lines": [a0, b0], "shown": "cut" if cut else "whole", **({"why": cut} if cut else {}),
                                     **({"call_line": c["around"]} if c.get("around") else {}),
                                     **({"note": c["note"]} if c.get("note") else {})})
        first = False
        if misses >= BODY_MISSES:
            for rest in cands[k + 1:]:
                refuse(rest, "packing stopped: ten bodies were already too long for what is left")
            break
    for u in more_funcs:
        refuse({"role": "focus", "u": u}, f"only the {FOCUS_BODIES} highest ranked focus functions get a body")
    out = ["Bodies (the focus, the types it takes, then what calls it):", ""]
    for b in blocks:
        out += b + [""]
    if not blocks:
        out += ["Nothing to show: the focus holds no function, and no type it takes is on the map.", ""]
    if left:
        text, used = "Bodies left out: ", len("Bodies left out: ")
        names = []
        for x in left[:LEFT_OUT_LISTED]:
            piece = f"{x['name']} ({x['path']}" + (f", {x['lines'][1] - x['lines'][0] + 1} lines" if x.get("lines") else "") + f"): {x['why']}"
            if used + len(piece) + 2 > reserve and names:
                break
            names.append(piece)
            used += len(piece) + 2
        rest = len(left) - len(names)
        out.append(text + "; ".join(names) + (f"; and {rest} more." if rest else "."))
    return out, meta + left[:LEFT_OUT_LISTED], max(0, len(left) - LEFT_OUT_LISTED)


def build(con, focus: list[str] | str, budget_tokens: int = 2000, bodies: bool = False) -> dict:
    """The outline for a focus within a token budget: {"text", "tokens", "focus", "shown", "left_out", ...}. With
    `bodies`, the code itself follows the outline (see `_bodies`), the outline taking a third of the budget, which is
    at least BODIES_MIN_TOKENS; the answer then also has `bodies`, what each is."""
    import time
    t0 = time.perf_counter()
    focus = [focus] if isinstance(focus, str) else list(focus or [])
    budget = max(MIN_TOKENS, min(int(budget_tokens or 2000), MAX_TOKENS))
    raised = bool(bodies and budget < BODIES_MIN_TOKENS)
    if raised:
        budget = BODIES_MIN_TOKENS
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
    near = {v for u in seeds for v, _w in g.adj[u]}

    def weight(u):   # the walk's score with its boosts, and a hub's share of it unless it is linked to the focus
        return scores.get(u, 0.0) * g.boost[u] * (1.0 if u in near else g.hub[u])
    ranked = sorted((u for u in scores if _shown(g, u) and u not in fset), key=lambda u: (-weight(u), g.ids[u]))
    order = sorted(fset, key=lambda u: -seeds.get(u, 0.0)) + ranked
    t_rank = time.perf_counter() - t0 - t_load

    sources = _Sources(con)
    total_chars = budget * 4
    budget_chars = total_chars // 3 if bodies else total_chars   # the outline's share
    head_names = [f["focus"] for f in found if "ids" in f]
    header = f"Code around {', '.join(head_names)[:200]}: declarations, the most related files first; > marks the focus."
    # What the focus named that is not on the map is said at the end, and counts against the budget too.
    missing = [f"Not found: {diagrams_cut(f['focus'], 60)!r} ({f['error']})." for f in found if "error" in f]
    reserve = len(header) + 520 + sum(len(m) + 1 for m in missing)   # the header, the summary and left-out lines
    chosen: set[int] = set()
    lines_of: dict[int, tuple] = {}      # node -> (declaration, notes)
    files: dict[str, float] = {}         # path -> best score in it, for the order of files
    used, misses, left = reserve, 0, []
    shown_path: dict[str, str] = {}      # a path shortened to fit the focus into a small budget

    # What the focus, and the code linked directly to it, is for: one lookup for those, not for every ranked candidate.
    near_ids = [u for u in (fset | near) if _shown(g, u)]
    got = summaries.for_nodes(con, [g.ids[u] for u in near_ids], width=SUMMARY_WIDTH)
    said = {u: got[g.ids[u]] for u in near_ids if g.ids[u] in got}

    def cost(u):
        decl, note = lines_of.get(u) or (_declaration(g, u, sources), _notes(g, u, fset, names))
        lines_of[u] = (decl, note)
        depth = len(_ancestors(g, u))
        return (2 + 2 * depth + len(decl) + 1 + ((6 + 2 * depth + len(note) + 1) if note else 0)
                + ((6 + 2 * depth + len(said[u]) + 1) if u in said else 0))

    for pos, u in enumerate(order):
        need = [a for a in _ancestors(g, u) if a not in chosen] + [u]
        c = sum(cost(x) for x in need) + (0 if g.path[u] in files else len(g.path[u]) + 2)
        if used + c > budget_chars and u in fset and not chosen:
            # The focus is always shown: when it does not fit, shorten its file's path (keeping the end, which names
            # the file) and its lines to what the budget has left.
            for x in need:   # a budget this small shows the focus's own lines only
                said.pop(x, None)
            room = budget_chars - used - sum(cost(x) - len(lines_of[x][0]) - len(lines_of[x][1]) for x in need) - 2
            plen = min(len(g.path[u]), max(24, room // 3))
            shown_path[g.path[u]] = g.path[u] if plen >= len(g.path[u]) else "..." + g.path[u][-(plen - 3):]
            per = max(16, (room - plen) // sum(1 + bool(lines_of[x][1]) for x in need))
            for x in need:
                d, note = lines_of[x]
                lines_of[x] = (diagrams_cut(d, per), diagrams_cut(note, per) if note else note)
            c = sum(cost(x) for x in need) + len(shown_path[g.path[u]]) + 2
        if used + c > budget_chars and not (u in fset and not chosen):
            left.append(u)
            misses += 1
            if misses > 40:   # nothing small enough is coming: stop looking
                left += [x for x in order[pos + 1:]]
                break
            continue
        used += c
        chosen.update(need)
        files[g.path[u]] = max(files.get(g.path[u], 0.0), 10.0 if u in fset else weight(u))

    # Render: files by their best score, inside each by line, members under their type.
    out = [header, ""]
    for path in sorted(files, key=lambda p: (-files[p], p)):
        out.append(shown_path.get(path, path))
        for u in sorted((x for x in chosen if g.path[x] == path), key=lambda x: (g.line[x], len(_ancestors(g, x)))):
            depth = len(_ancestors(g, u))
            decl, note = lines_of.get(u) or (_declaration(g, u, sources), _notes(g, u, fset, names))
            out.append(("> " if u in fset else "  ") + "  " * depth + decl)
            if u in said:
                out.append("  " + "  " * depth + "    -- " + said[u])
            if note:
                out.append("  " + "  " * depth + "    -- " + note)
        out.append("")
    near = _hops(g, set(seeds), 2)
    related = [u for u in left if u in near]
    direct = sum(near[u] == 1 for u in related)
    left_files = sorted({g.path[u] for u in related})
    out.append(f"Shown: {len(chosen)} symbol{'s' * (len(chosen) != 1)} in {len(files)} file{'s' * (len(files) != 1)}.")
    if related:
        top = diagrams_cut(", ".join(f"{_short(g, u)} ({g.path[u]})" for u in related[:3]), 260)
        out.append(f"Left out: {direct} more symbol{'s' * (direct != 1)} linked directly to the focus and"
                   f" {len(related) - direct} two links away, in {len(left_files)} file{'s' * (len(left_files) != 1)};"
                   f" the nearest: {top}. Raise the budget to see more.")
    else:
        out.append("Left out: nothing within two links of the focus.")
    out += missing
    text = "\n".join(out).rstrip() + "\n"
    described, unlisted = None, 0
    if bodies:
        lines, described, unlisted = _bodies(con, g, sources, fset, seeds, weight, max(0, total_chars - len(text) - 60))
        if raised:
            lines.insert(0, f"The budget was raised to {BODIES_MIN_TOKENS} tokens, the least that bodies work with.")
        text += "\n" + "\n".join(lines).rstrip() + "\n"
    return {"focus": found, "budget_tokens": budget, "tokens": len(text) // 4, "text": text,
            "shown": {"symbols": len(chosen), "files": len(files)},
            "left_out": {"direct": direct, "two_links_away": len(related) - direct, "files": len(left_files),
                         "nearest": [{"id": g.ids[u], "name": _short(g, u), "path": g.path[u]} for u in related[:10]]},
            **({"bodies": described} if described is not None else {}),
            **({"bodies_left_out_not_listed": unlisted} if unlisted else {}),
            **({"note": f"The budget was raised to {BODIES_MIN_TOKENS} tokens, the least that bodies work with."}
               if raised else {}),
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
