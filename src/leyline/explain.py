"""Asking how something works: find where a described behavior starts, and walk it step by step.

Leyline holds no language model. An agent (or a person at the command line) asks in words, and these two functions
give it what to reason over:

    find_flows(con, description)        where the behavior could start: entry points, route handlers, UI handlers,
                                        message handlers, tests named for the behavior, and other functions, ranked
                                        by the words they share with the description, each with why it matched
    explain_path(con, frm, to, through) an ordered walk from one of them, across calls and channels (a request, a
                                        message, a launched program), each step with the line that reaches it and the
                                        callee's declaration; data written for a reader that runs later is marked so

Words are matched loosely: code names are split (`saveDialogue` is save and dialogue), endings are cut (saves, saved
and saving are save), and a small table makes words that mean the same thing in code one (save, write, persist,
store, put). A word rare on the map counts for more than one found everywhere. A candidate that starts something (an
entry point, a route, an event handler) or a test whose name states the behavior is ranked up.

The words index is built once per map run and kept, so later questions cost milliseconds.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import diagrams, query, store

# -- words ---------------------------------------------------------------------------------------------------------
STOP = set("""a an the and or of to in on for with from by is are was were be been being it its this that these those
how what where when which who whom whose why does do did done doing happen happens happened there their then than
into as at via after before through about our my your we you i me can could will would should shall may might must us
them they he she his her if so not no yes also just one any all some each every way thing things work works get gets
got going go goes like use used using let lets please show tell explain trace find walk step-by-step""".split())
WEAK = set("user users writer writers author authors person people someone somebody customer customers client"
           " flow code function functions method methods class classes".split())

SYNONYMS = [
    "save write persist store put commit flush upsert",
    "delete remove destroy drop erase unlink purge",
    "create add new make spawn instantiate insert build construct generate init",
    "load read fetch get open retrieve hydrate",
    "run execute exec play start launch invoke perform",
    "validate validation check verify lint audit",
    "step tick advance update iterate",
    "send emit publish post dispatch broadcast notify",
    "receive handle consume listen subscribe",
    "render draw display show paint",
    "train learn fit",
    "search find query lookup filter",
    "login signin auth authenticate",
    "error fail failure exception crash",
    "close stop shutdown quit exit",
    "rename move relocate",
    "copy duplicate clone",
    "import ingest parse",
    "export serialize dump",
    "edit change modify update",
    "reset clear",
    "vehicle car",
    "test spec",
]

WEIGHT = {"name": 3.0, "route": 3.0, "test": 2.0, "owner": 1.5, "file": 1.2, "doc": 1.0, "language": 1.0, "path": 0.5}
LANGUAGE_WORDS = {"csharp": "csharp cs c# dotnet", "typescript": "typescript ts", "javascript": "javascript js",
                  "python": "python py", "gdscript": "gdscript godot", "cpp": "cpp c++"}
EXACT, NEAR, SYNONYM = 1.0, 0.8, 0.6

# What a candidate is, how to say it, and how much it is ranked up.
KINDS = {
    "route": ("route handler", 1.6),
    "message": ("message handler", 1.5),
    "entry": ("program entry", 1.5),
    "command": ("command", 1.5),
    "ui": ("UI event handler", 1.4),
    "engine": ("engine callback", 1.3),
    "test": ("test", 1.1),
    "component": ("UI component", 1.1),
    "function": ("function", 1.0),
}
ENGINE_CALLBACKS = {"Awake", "Start", "Update", "FixedUpdate", "LateUpdate", "OnEnable", "_ready", "_process",
                    "_physics_process", "_input"}
TOP = diagrams.TOP


def split_words(text: str) -> list[str]:
    """Words of text or of a code name, lower case: `saveDialogue` and `save_dialogue` are save, dialogue."""
    out = []
    for part in re.findall(r"[A-Za-z0-9]+", text or ""):
        for w in re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", part):
            out.append(w.lower())
    return out


def stem(w: str) -> str:
    """A word with its ending cut, so saves, saved and saving, or validate, validation and validator, meet."""
    w = w.lower()
    if not w.endswith("ss"):
        for suf in ("ations", "ions", "ion", "ings", "ing", "ers", "er", "ors", "or", "ies", "ied", "sses", "es", "ed", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[:-len(suf)] + ("y" if suf in ("ies", "ied") else "ss" if suf == "sses" else "")
                if suf in ("ing", "ed", "er", "ers") and len(w) > 3 and w[-1] == w[-2] and w[-1] not in "ls":
                    w = w[:-1]   # stepping, stepped: step
                break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    return w


GROUPS: dict[str, set] = defaultdict(set)
for _k, _line in enumerate(SYNONYMS):
    for _w in _line.split():
        GROUPS[stem(_w)].add(_k)


# -- the index of candidates ------------------------------------------------------------------------------------------
class _Lines:
    """Source lines read from the working tree, one file at a time, kept."""

    def __init__(self, con):
        self.roots = store.roots(con)
        self.files: dict = {}

    def get(self, node_id: str, path: Optional[str]) -> list[str]:
        repo = node_id.split(":", 1)[0]
        root = self.roots.get(repo)
        if root is None or not path:
            return []
        key = (repo, path)
        if key not in self.files:
            from .indexer import source_lines
            try:
                self.files[key] = source_lines(Path(root) / path)
            except OSError:
                self.files[key] = []
        return self.files[key]

    def line(self, node_id: str, path: Optional[str], n: Optional[int]) -> Optional[str]:
        lines = self.get(node_id, path)
        if not n or not (0 < n <= len(lines)):
            return None
        text = " ".join(lines[n - 1].split())
        return text if len(text) <= 160 else text[:157] + "..."

    def declaration(self, node_id: str, path: Optional[str], n: Optional[int]) -> Optional[str]:
        """The line that declares a node whose span starts at line n: past its decorators or attributes
        (`@app.route(...)`, `[HttpGet]`), which may run over several lines."""
        lines = self.get(node_id, path)
        if not n or not (0 < n <= len(lines)):
            return None
        k, depth = n, 0
        while k <= min(len(lines), n + 30):
            s = lines[k - 1].strip()
            if not (depth > 0 or s.startswith(("@", "["))):
                return self.line(node_id, path, k)
            depth = max(0, depth + s.count("(") + s.count("[") - s.count(")") - s.count("]"))
            k += 1
        return self.line(node_id, path, n)


COMMENT = re.compile(r"^\s*(//+|#+|/\*+|\*+/?|<!--|--)\s?")


def leading_doc(lines: list[str], start: int) -> str:
    """The comment just above a declaration, and a Python docstring just below it: what its author said it does."""
    if not lines or not start:
        return ""
    out: list[str] = []
    k = start - 2
    while k >= 0 and len(out) < 10:
        s = lines[k].strip()
        if s.startswith("@") or s.startswith("["):    # a decorator or an attribute sits between the comment and the code
            k -= 1
            continue
        if not COMMENT.match(s) or not s:
            break
        out.append(re.sub(r"\s*(\*/|-->)$", "", COMMENT.sub("", s)))
        k -= 1
    out.reverse()
    for k in range(start, min(start + 3, len(lines))):   # the docstring, on the lines after `def ...:`
        s = lines[k].strip()
        if s.startswith(('"""', "'''", 'r"""')):
            q = s.lstrip("r")[:3]
            body = [s.lstrip("r")[3:]]
            j = k
            while q not in body[-1] and j + 1 < len(lines) and len(body) < 8:
                j += 1
                body.append(lines[j].strip())
            out.append(" ".join(body).split(q)[0])
            break
        if s and not s.endswith((":", "(", ",")):
            break
    text = re.sub(r"<[^>]+>", " ", " ".join(out))     # C# /// <summary>
    return " ".join(text.split())[:400]


def _path_fields(path: Optional[str]) -> dict:
    """A file's own name, which says what it is about, and the folders it is in, which say less."""
    folders, _, base = (path or "").rpartition("/")
    return {"file": base.split(".")[0], "path": folders}


class Index:
    """Everything find_flows ranks: one row per callable and test, with its words by field."""

    def __init__(self, con):
        rows = con.execute("SELECT id, kind, name, parent_id, path, span_start, attrs, language FROM nodes"
                           " WHERE kind IN ('callable', 'test', 'type', 'file')").fetchall()
        info = {r[0]: r for r in rows}
        self.entry = {r[0]: json.loads(r[1] or "{}").get("trigger") for r in con.execute(
            "SELECT e.dst_id, n.attrs FROM edges e JOIN nodes n ON n.id = e.src_id WHERE e.kind = 'exposes'")}
        self.channel_in: dict[str, list] = defaultdict(list)
        for d, a in con.execute("SELECT dst_id, attrs FROM edges WHERE kind = 'communicates'"):
            try:
                at = json.loads(a or "{}")
            except ValueError:
                at = {}
            self.channel_in[d].append((at.get("channel") or "channel", str(at.get("address") or "")))
        self.flows_from: dict[str, list] = defaultdict(list)
        for fid, eid in con.execute("SELECT id, entry_id FROM flows"):
            self.flows_from[eid].append(fid)
        lines = _Lines(con)
        self.ids: list[str] = []
        self.rows: list[dict] = []
        self.fields: list[dict] = []
        self.postings: dict[str, list] = defaultdict(list)   # stem -> [(row, field)]
        self.surface: dict[str, str] = {}                    # stem -> the shortest word on the map it came from
        for r in rows:
            nid, kind, name, parent, path, line, raw, lang = r
            if kind not in ("callable", "test"):
                continue
            try:
                a = json.loads(raw or "{}")
            except ValueError:
                a = {}
            p = info.get(parent)
            top = name in TOP
            if top and nid not in self.entry:
                continue
            what = self._kind(nid, kind, name, a, p, path)
            owner = p[2] if p is not None and p[1] in ("type", "callable") and p[2] not in TOP else ""
            label = (f"top level of {path}" if top else f"test \"{name}\"" if what == "test" else
                     f"{owner}.{name}" if owner and what != "route" else name)
            route = a.get("route") or next((addr for ch, addr in self.channel_in.get(nid, ()) if ch == "http"), "")
            if what == "test":
                fields = {"test": a.get("full_name") or name, **_path_fields(path)}
            else:
                # A program's top level is named by its file: train.py is the trainer.
                fields = {"name": "" if name == route else name if not top else _path_fields(path)["file"],
                          "owner": owner, "route": route,
                          **_path_fields(path)}
                if not top:
                    fields["doc"] = leading_doc(lines.get(nid, path), line or 0)
            fields["language"] = LANGUAGE_WORDS.get(lang or "", lang or "")
            k = len(self.ids)
            self.ids.append(nid)
            self.rows.append({"id": nid, "kind": what, "label": label, "path": path, "line": line, "route": route,
                              "address": (self.entry.get(nid) or "") if what == "entry" else ""})
            stems_by_field = {}
            for f, text in fields.items():
                ws = set()
                for w in split_words(text):
                    if len(w) > 1 and w not in STOP:
                        st = stem(w)
                        ws.add(st)
                        if len(w) < len(self.surface.get(st, w + "_")):
                            self.surface[st] = w
                stems_by_field[f] = ws
                for s in ws:
                    self.postings[s].append((k, f))
            self.fields.append(stems_by_field)
        self.vocab = sorted(self.postings)
        self.df = {s: len({k for k, _ in ps}) for s, ps in self.postings.items()}
        self.n = max(len(self.ids), 1)

    def _kind(self, nid, kind, name, a, parent, path) -> str:
        if kind == "test" or a.get("is_test"):
            return "test"
        if a.get("native_kind") == "route_handler" or a.get("route"):
            return "route"
        chans = {ch for ch, _ in self.channel_in.get(nid, ())}
        if "http" in chans:
            return "route"
        if chans & {"queue", "event", "rpc"}:
            return "message"
        if nid in self.entry:
            return "entry"
        decorators = " ".join(map(str, a.get("decorators") or []))
        if re.search(r"\.command\b|\bcommand\(|\bcli\b", decorators):
            return "command"
        if re.match(r"(on|handle)[A-Z_]", name):
            return "ui"
        if name in ENGINE_CALLBACKS:
            return "engine"
        if a.get("native_kind") == "component":
            return "component"
        if parent is not None and parent[1] == "callable" and "component" in (parent[6] or ""):
            return "ui"     # a function declared inside a UI component: a handler or a callback it hands out
        return "function"


_cache: dict = {}
_cache_lock = threading.Lock()


def _stamp(con):
    gen = con.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
    return (gen[0] if gen else None, con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM flows").fetchone()[0])


def index(con) -> Index:
    """The words index of a store, built once per map run and kept."""
    path, stamp = str(store.store_file(con)), _stamp(con)
    with _cache_lock:
        hit = _cache.get(("index", path))
        if hit is not None and hit[0] == stamp and stamp[0] is not None:
            return hit[1]
    ix = Index(con)
    with _cache_lock:
        _cache[("index", path)] = (stamp, ix)
    return ix


# -- find_flows -------------------------------------------------------------------------------------------------------
def terms(description: str) -> list[dict]:
    """The words of a description that count, each with its stem, its weight and the words that mean the same. A word
    for who does it (a user, a writer) counts little and has no synonyms; nor does a noun made from a verb (editor is
    not edit)."""
    out, seen = [], set()
    raw = re.findall(r"[A-Za-z0-9]+", description or "")
    # A word written with a capital inside the sentence names something (Play mode, the Python trainer).
    named = {w.lower() for k, w in enumerate(raw) if k > 0 and w[:1].isupper() and not w.isupper()}
    for w in split_words(description):
        if w in STOP or len(w) < 2:
            continue
        s = stem(w)
        if s in seen:
            continue
        seen.add(s)
        listed = any(w in line.split() for line in SYNONYMS)
        agent = re.search(r"(er|or|ers|ors)$", w) and not listed
        same = [] if (w in WEAK or agent) else sorted(
            {x for line_k in GROUPS.get(s, ()) for x in SYNONYMS[line_k].split()} - {w})
        out.append({"word": w, "stem": s, "weak": w in WEAK, "same": same, **({"named": True} if w in named else {})})
    return out


def _matches(ix: Index, t: dict) -> dict[str, float]:
    """The stems on the map a term matches, and how well: the same word, a longer or shorter form, a synonym."""
    s = t["stem"]
    if t["weak"]:
        return {s: EXACT} if s in ix.postings else {}
    out = {s: EXACT} if s in ix.postings else {}
    if len(s) >= 4:
        import bisect
        k = bisect.bisect_left(ix.vocab, s)
        while k < len(ix.vocab) and ix.vocab[k].startswith(s):
            v = ix.vocab[k]
            if v != s and len(v) - len(s) <= 4:
                out.setdefault(v, NEAR)
            k += 1
    for w in t["same"]:
        for v in (stem(w),):
            if v != s and v in ix.postings:
                out[v] = max(out.get(v, 0.0), SYNONYM)
    return out


def _flows_through(con, nid: str, most: int = 2) -> list[dict]:
    """Entry-point flows that pass a function, shortest way in first."""
    rows = con.execute("SELECT f.id, f.name, s.depth FROM flow_steps s JOIN flows f ON f.id = s.flow_id WHERE"
                       " s.callable_id = ? AND json_extract(f.attrs, '$.kind') = 'entry' AND f.entry_id != ?"
                       " ORDER BY s.depth, f.id LIMIT ?", (nid, nid, most)).fetchall()
    return [{"flow": r[0], "depth": r[2]} for r in rows]


def find_flows(con, description: str, limit: int = 10) -> dict:
    """Where a described behavior could start, best first: {"terms", "candidates", "ambiguous", "note"}."""
    import time
    t0 = time.perf_counter()
    ts = terms(description)
    if not ts:
        return {"error": "the description has no words to look for: say what happens, such as \"a writer saves a"
                         " dialogue\" or \"how a vehicle is spawned\"."}
    ix = index(con)
    t_index = time.perf_counter() - t0
    own = {t["stem"] for t in ts}
    # A word another word of the description already is (play and run, both asked for) is not matched twice.
    matches = {t["stem"]: {v: q for v, q in _matches(ix, t).items() if v == t["stem"] or v not in own} for t in ts}
    weight = {}
    for t in ts:   # a word rare on the map counts for more; one not on it at all, by its rarest match
        df = ix.df.get(t["stem"]) or min([ix.df[v] for v in matches[t["stem"]]] or [0])
        weight[t["stem"]] = (0.3 if t["weak"] else 1.5 if t.get("named") else 1.0) * math.log(1 + ix.n / (1 + df))
    weak = {t["stem"]: t["weak"] for t in ts}
    total = sum(weight.values()) or 1.0
    found: dict[int, dict] = defaultdict(lambda: defaultdict(list))   # row -> term stem -> [(score, field, stem, quality)]
    for t in ts:
        for v, q in matches[t["stem"]].items():
            for k, f in ix.postings[v]:
                found[k][t["stem"]].append((WEIGHT[f] * q, f, v, q))
    best: dict[int, dict] = {}   # row -> term stem -> (score, field, matched stem, quality)
    scored = []
    for k, per_term in found.items():
        row = ix.rows[k]
        # Each word of the code answers one word of the description: play and run both matching `start` count once.
        # The words that count most choose first. A word found in its best place counts whole, and a little more for
        # each other place it is found (its name and its file).
        # The same word first, then the words that mean the same: play finds PlayPanel before run takes `start`.
        hits, term, used = {}, {}, set()
        for exact_only in (True, False):
            for s in sorted(per_term, key=lambda s: -weight[s] * max(x[0] for x in per_term[s])):
                if s in hits:
                    continue
                free = [x for x in per_term[s] if x[2] not in used and (x[3] == EXACT or not exact_only)]
                if not free:
                    continue
                top = max(free)
                used.add(top[2])
                hits[s] = top
                places = {}
                for x in free:
                    places[x[1]] = max(places.get(x[1], 0.0), x[0])
                term[s] = top[0] + 0.3 * (sum(places.values()) - top[0])
        best[k] = hits
        covered = sum(weight[s] * min(1.0, term[s] / 2.0) for s in hits)
        raw = sum(weight[s] * term[s] for s in hits)
        strong = any(h[1] in ("name", "route", "test") and not weak[s] for s, h in hits.items())
        score = raw * (0.4 + covered / total) * KINDS[row["kind"]][1] * (1.0 if strong else 0.6)
        scored.append((score, k))
    scored.sort(key=lambda x: (-x[0], ix.ids[x[1]]))
    picked, per_file, tests = [], defaultdict(int), 0
    test_cap = max(2, limit // 3)
    for score, k in scored:
        row = ix.rows[k]
        if per_file[row["path"]] >= 3 or (row["kind"] == "test" and tests >= test_cap):
            continue
        per_file[row["path"]] += 1
        tests += row["kind"] == "test"
        picked.append((score, k))
        if len(picked) >= limit:
            break
    words = {t["stem"]: t["word"] for t in ts}
    out = []
    for score, k in picked:
        row, nid = ix.rows[k], ix.ids[k]
        why = []
        for s, (sc, f, v, q) in sorted(best[k].items(), key=lambda x: -x[1][0]):
            where = {"name": "its name", "route": "its route", "test": "the test's name", "owner": "the name of what"
                     " it is in", "doc": "its comment", "file": "its file's name", "path": "its folder", "language": "its language"}[f]
            said = ix.surface.get(v, v)
            how = "" if q == EXACT else f" (as {said})" if q == NEAR else f" (as {said}, which means the same here)"
            why.append(f"{words[s]}{how} in {where}")
        kind_word = KINDS[row["kind"]][0]
        if row["kind"] == "route" and row["route"]:
            kind_word += f" for {row['route']}"
        elif row["kind"] == "entry" and row["address"]:
            kind_word += f" ({row['address']})"
        item = {"id": nid, "name": row["label"], "kind": kind_word, "at": f"{row['path']}:{row['line']}",
                "score": round(score, 2), "why": why, "flows": ix.flows_from.get(nid, [])[:3]}
        if not item["flows"]:
            item["reached_from"] = _flows_through(con, nid)
        out.append(item)
    # Close: another of the first four, in another file, scores near the first.
    ambiguous = any(sc >= 0.7 * picked[0][0] and ix.rows[k]["path"] != ix.rows[picked[0][1]]["path"]
                    for sc, k in picked[1:4]) if picked else False
    note = ("The first candidates score close together and sit in different files: show the person the top few and"
            " ask which they mean, or pick one and say why." if ambiguous else
            "Read the top candidate's code (`source`) before relying on it; the ranking matches words, not meaning.")
    if not out:
        note = "Nothing on the map shares a word with the description. Try the words the code would use."
    return {"description": description,
            "words": [{"word": t["word"], **({"also": t["same"][:8]} if t["same"] else {}),
                       **({"weak": True} if t["weak"] else {})} for t in ts],
            "candidates": out, "ambiguous": ambiguous, "note": note,
            "next": (f"explain_path with start={out[0]['id']!r} walks it step by step." if out else ""),
            "seconds": {"index": round(t_index, 3), "total": round(time.perf_counter() - t0, 3)}}


def find_text(r: dict) -> str:
    """find_flows as a person reads it at the command line."""
    if "error" in r:
        return r["error"]
    ws = ", ".join(w["word"] + (f" (or {', '.join(w['also'][:4])})" if w.get("also") else "")
                   + (" (counts little)" if w.get("weak") else "") for w in r["words"])
    L = [f"Where \"{r['description']}\" could start. Words looked for: {ws}.", ""]
    for k, c in enumerate(r["candidates"], 1):
        L.append(f"{k:>2}. {c['name']}  [{c['kind']}]  {c['at']}")
        L.append(f"      {c['id']}")
        L.append(f"      matched: {'; '.join(c['why'])}")
        if c["flows"]:
            L.append(f"      flows that start here: {', '.join(c['flows'])}")
        elif c.get("reached_from"):
            L.append(f"      reached from: {', '.join(x['flow'] for x in c['reached_from'])}")
    L += ["", r["note"]]
    if r.get("next"):
        L.append("Next: leyline explain-path " + json.dumps(r["candidates"][0]["id"]))
    return "\n".join(L)


# -- explain_path -----------------------------------------------------------------------------------------------------
DATA = ("db", "file", "format")                     # nothing runs across these: a reader reads it later
ELSEWHERE = ("http", "process", "rpc", "queue")      # the far side runs in another process, or later
HELPER_CALLERS = 8        # a leaf called from this many places is a helper: counted, not shown
MAX_DEPTH, MAX_WALK = 8, 300


class _Graph:
    """Calls and channel links out of each function, in the order the code makes them (read once per map run)."""

    def __init__(self, con):
        self.out: dict[str, list] = defaultdict(list)    # id -> [(order, dst, via, guess)]
        self.callers: dict[str, int] = defaultdict(int)   # callers outside test code
        tests = {r[0] for r in con.execute("SELECT id, path, kind FROM nodes WHERE kind IN ('callable', 'test')")
                 if r[2] == "test" or diagrams.TEST_PATH.search(r[1] or "")}
        for s, d, line, guess in con.execute("SELECT src_id, dst_id, MIN(site_start), MIN(precision = 'guess') FROM calls"
                                             " WHERE src_id != dst_id GROUP BY src_id, dst_id"):
            self.out[s].append((line or 0, d, "calls", bool(guess)))
            if s not in tests:
                self.callers[d] += 1
        self.data: dict[str, list] = defaultdict(list)   # id -> [(channel, address, reader, guess)]
        for s, d, prec, a in con.execute("SELECT src_id, dst_id, precision, attrs FROM edges WHERE kind = 'communicates'"):
            try:
                at = json.loads(a or "{}")
            except ValueError:
                at = {}
            ch = at.get("channel") or "channel"
            if ch in DATA:
                self.data[s].append((ch, str(at.get("address") or ""), d, prec == "guess"))
                continue
            order = at.get("launched_at") or at.get("line") or 10 ** 9
            self.out[s].append((order, d, ch, prec == "guess"))
        for impl, base, prec in con.execute("SELECT src_id, dst_id, precision FROM edges WHERE kind = 'overrides'"):
            self.out[base].append((10 ** 9 - 1, impl, "dispatch", prec == "guess"))
        for lst in self.out.values():
            lst.sort(key=lambda x: (x[0], x[1]))


def graph(con) -> _Graph:
    path, stamp = str(store.store_file(con)), _stamp(con)
    with _cache_lock:
        hit = _cache.get(("graph", path))
        if hit is not None and hit[0] == stamp and stamp[0] is not None:
            return hit[1]
    g = _Graph(con)
    with _cache_lock:
        _cache[("graph", path)] = (stamp, g)
    return g


def _resolve(con, text: str) -> dict:
    """{"id"} for a node id, a flow id, an entry point, a route (`PUT /api/x`) or a name; else an error."""
    text = (text or "").strip()
    if not text:
        return {"error": "nothing given to start from"}
    f = con.execute("SELECT entry_id FROM flows WHERE id = ?", (text,)).fetchone()
    if f is not None:
        return {"id": f[0], "flow": text}
    row = query._node(con, text)
    if row is not None:
        if row["kind"] == "entry_point":
            t = con.execute("SELECT dst_id FROM edges WHERE kind = 'exposes' AND src_id = ?", (text,)).fetchone()
            if t is not None:
                return {"id": t[0]}
        return {"id": text}
    if re.match(r"[A-Z]+ /", text):   # a route, as its handler is named
        rows = con.execute("SELECT id FROM nodes WHERE kind = 'callable' AND name = ?", (text,)).fetchall()
        if len(rows) == 1:
            return {"id": rows[0][0]}
    return query.resolve(con, text)


def _walk(con, g: _Graph, start: str) -> tuple[list[dict], bool, Optional[str]]:
    """The steps from `start`: the stored flow that starts there if there is one, else a walk made now the same way
    (depth first, in the order the code makes its calls, each function once). (steps, truncated, flow id)."""
    f = con.execute("SELECT id, attrs FROM flows WHERE entry_id = ? ORDER BY id LIMIT 1", (start,)).fetchone()
    if f is not None:
        steps = [{"seq": r[0], "depth": r[1], "id": r[2], "via": r[3], "line": r[4], "parent": r[5]} for r in con.execute(
            "SELECT seq, depth, callable_id, via, site_line, parent_seq FROM flow_steps WHERE flow_id = ? ORDER BY seq",
            (f[0],))]
        return steps, bool(json.loads(f[1] or "{}").get("truncated")), f[0]
    steps = [{"seq": 0, "depth": 0, "id": start, "via": "start", "line": None, "parent": None}]
    seen, truncated = {start}, False

    def walk(node, depth, parent):
        nonlocal truncated
        if depth >= MAX_DEPTH:
            truncated = truncated or bool(g.out.get(node))
            return
        for order, dst, via, _guess in g.out.get(node, ()):
            if dst in seen:
                continue
            if len(steps) >= MAX_WALK:
                truncated = True
                return
            seen.add(dst)
            seq = len(steps)
            steps.append({"seq": seq, "depth": depth + 1, "id": dst, "via": via,
                          "line": order if isinstance(order, int) and order < 10 ** 9 - 1 else None, "parent": parent})
            walk(dst, depth + 1, seq)
    walk(start, 0, 0)
    return steps, truncated, None


def _shortest(con, g: _Graph, a: str, b: str, max_depth: int = 16) -> Optional[list[dict]]:
    """The shortest chain of calls and channels from a to b, as steps; first without data hops, then with them."""
    for with_data in (False, True):
        prev = {a: None}
        frontier = [a]
        for _ in range(max_depth):
            if b in prev or not frontier:
                break
            nxt = []
            for cur in frontier:
                outs = list(g.out.get(cur, ()))
                if with_data:
                    outs += [(10 ** 9, d, ch, guess) for ch, _addr, d, guess in g.data.get(cur, ())]
                for order, dst, via, _guess in outs:
                    if dst not in prev:
                        prev[dst] = (cur, via, order)
                        nxt.append(dst)
            frontier = nxt
        if b in prev:
            chain, cur = [], b
            while prev[cur] is not None:
                p, via, order = prev[cur]
                chain.append((cur, via, order))
                cur = p
            chain.reverse()
            steps = [{"seq": 0, "depth": 0, "id": a, "via": "start", "line": None, "parent": None}]
            for k, (nid, via, order) in enumerate(chain, 1):
                steps.append({"seq": k, "depth": k, "id": nid, "via": via, "parent": k - 1,
                              "line": order if isinstance(order, int) and order < 10 ** 9 - 1 else None})
            return steps
    return None


def _select(g: _Graph, steps: list[dict], most: int) -> tuple[list[dict], list[dict]]:
    """The steps to show, at most `most`: every hop across a channel and what leads to it, then the shallowest steps,
    those that lead to more first. Leaves called from many places (helpers) are counted, not shown."""
    kids: dict = defaultdict(list)
    for s in steps:
        if s["parent"] is not None:
            kids[s["parent"]].append(s["seq"])
    size: dict = {}
    for s in reversed(steps):
        size[s["seq"]] = 1 + sum(size.get(c, 0) for c in kids[s["seq"]])
    by_seq = {s["seq"]: s for s in steps}
    # A helper: called from many places outside tests, and calling next to nothing itself (not cut off by the depth).
    helpers = [s for s in steps if s["via"] == "calls" and not kids[s["seq"]] and len(g.out.get(s["id"], ())) <= 1
               and g.callers.get(s["id"], 0) >= HELPER_CALLERS]
    helper_seqs = {s["seq"] for s in helpers}
    keep: set = set()

    def with_parents(seq):
        chain = []
        while seq is not None and seq not in keep:
            chain.append(seq)
            seq = by_seq[seq]["parent"]
        return chain
    for s in steps:
        if s["via"] not in ("start", "calls", "runs", "dispatch"):
            chain = with_parents(s["seq"])
            if len(keep) + len(chain) <= most:
                keep.update(chain)
    for s in sorted(steps, key=lambda s: (s["depth"], -size[s["seq"]], s["seq"])):
        if len(keep) >= most:
            break
        if s["seq"] in helper_seqs or s["seq"] in keep:
            continue
        chain = with_parents(s["seq"])
        if len(keep) + len(chain) <= most:
            keep.update(chain)
    return [s for s in steps if s["seq"] in keep], helpers


def _label(con, nid: str, nodes: diagrams._Nodes) -> str:
    n = nodes.get(nid)
    if n is None:
        return nid.rsplit(":", 1)[-1]
    if n["name"] in TOP:
        return f"the top level of {n['path']}" if n.get("path") else "a file's top level"
    p = nodes.get(n["parent_id"])
    if n["kind"] == "callable" and re.match(r"[A-Z]+ /", n["name"]):
        return f"the {n['name']} handler"
    if n["kind"] == "test":
        return f"test \"{n['name']}\""
    if p is not None and p["kind"] in ("type", "callable") and p["name"] not in TOP:
        return f"{p['name']}.{n['name']}"
    return n["name"]


def _how(via: str, address: str) -> str:
    return {"start": "start", "calls": "call", "runs": "run by the test runner",
            "dispatch": "call through an interface or base method, landing in this implementation",
            "di": "the implementation a container registers"}.get(via) or (
        f"starts {address or 'another program'}" if via == "process" else f"{via} {address}".strip())


def explain_path(con, frm: str, to: Optional[str] = None, through: Optional[str] = None, max_steps: int = 40) -> dict:
    """An ordered walk from `frm`: the main flow from it, the shortest path to `to`, or the paths through `through`."""
    max_steps = max(2, min(int(max_steps or 40), 200))
    ends = {}
    for key, text in (("start", frm), ("to", to), ("through", through)):
        if text is None or not str(text).strip():
            continue
        r = _resolve(con, str(text))
        if "id" not in r:
            return {"error": f"{key}: {r.get('error')}", "candidates": r.get("candidates", [])[:8]}
        ends[key] = r
    if "start" not in ends:
        return {"error": "start is needed: a node id, a name, a route (`PUT /api/x`) or a flow id. find_flows finds one."}
    g = graph(con)
    start = ends["start"]["id"]
    truncated, flow_id, mode = False, None, "walk"
    if "through" in ends or "to" in ends:
        mid = ends.get("through", {}).get("id")
        end = ends.get("to", {}).get("id")
        first = _shortest(con, g, start, mid or end)
        if first is None:
            what = mid or end
            return {"from": start, "found": False, "to" if end and not mid else "through": what,
                    "note": "No path on the map from one to the other. The link may run through something the map cannot"
                            " follow: a callback, a framework, a value whose type is not written, or a channel not analyzed.",
                    "next": "Try explain_path from the second one alone, or impact on it to see what reaches it."}
        steps, mode = first, "path"
        if mid and end:
            second = _shortest(con, g, mid, end)
            if second is None:
                return {"from": start, "through": mid, "to": end, "found": False,
                        "note": "The map reaches the middle point but finds no path on from it to the end."}
            base = len(steps) - 1
            for s in second[1:]:
                steps.append({**s, "seq": base + s["seq"], "depth": base + s["depth"], "parent": base + s["parent"]})
        elif mid:   # and on from the middle point, along its own walk
            mode = "through"
            rest, truncated, _ = _walk(con, g, mid)
            base, depth0, have = len(steps) - 1, steps[-1]["depth"], {s["id"] for s in steps}
            remap = {0: base}
            for s in rest[1:]:
                if s["id"] in have or s["parent"] not in remap:
                    continue
                remap[s["seq"]] = len(steps)
                steps.append({**s, "seq": len(steps), "depth": depth0 + s["depth"], "parent": remap[s["parent"]]})
    else:
        steps, truncated, flow_id = _walk(con, g, start)
    shown, helpers = _select(g, steps, max_steps) if mode != "path" else (steps[:max_steps], [])
    nodes, lines = diagrams._Nodes(con), _Lines(con)
    by_seq = {s["seq"]: s for s in steps}
    out_steps, notes, guessed, dispatch = [], [], [], []
    for k, s in enumerate(shown, 1):
        n = nodes.get(s["id"]) or {"path": None, "span_start": None, "kind": "?", "name": s["id"]}
        parent = by_seq.get(s["parent"]) if s["parent"] is not None else None
        e = diagrams._backing(con, nodes, parent["id"], s["id"], s["via"]) if parent else None
        address = (e or {}).get("address") or ""
        line = s.get("line") or (e or {}).get("line")
        step = {"n": k, "id": s["id"], "name": _label(con, s["id"], nodes), "at": f"{n['path']}:{n['span_start']}",
                "depth": s["depth"], "how": _how(s["via"], address)}
        if parent is not None:
            step["from"] = _label(con, parent["id"], nodes)
            pn = nodes.get(parent["id"]) or {}
            call = lines.line(parent["id"], pn.get("path"), line) if line else None
            if line:
                step["call_line"] = f"{pn.get('path')}:{line}"
            if call:
                step["call"] = call
        decl = lines.declaration(s["id"], n["path"], n["span_start"]) if n.get("name") not in TOP else None
        if decl:
            step["declaration"] = decl
        if s["via"] in ELSEWHERE:
            step["crosses"] = ("into another program" if s["via"] == "process" else
                               "to another process or service: the caller does not wait on this code's stack"
                               if s["via"] == "queue" else "to another process or service")
        if e is not None and e.get("guess"):
            step["guessed"] = True
            guessed.append(k)
        if s["via"] == "dispatch":
            dispatch.append(k)
        later = [{"what": diagrams._channel_text(ch, addr, nodes, reader), "reader": _label(con, reader, nodes),
                  "reader_id": reader, **({"guessed": True} if guess else {})}
                 for ch, addr, reader, guess in g.data.get(s["id"], ())[:3]]
        if later:
            step["later_elsewhere"] = later
        out_steps.append(step)
    from .outline import labels   # the named part (module_outline, name_part) each step sits in
    named = labels(con, [s["id"] for s in out_steps])
    for step in out_steps:
        if step["id"] in named:
            step["part"] = named[step["id"]]
    if guessed:
        notes.append(f"Step{'s' if len(guessed) > 1 else ''} {_nums(guessed)} follow{'s' if len(guessed) == 1 else ''}"
                     " a link the map guessed by name: read the code before relying on it.")
    if dispatch:
        notes.append(f"Step{'s' if len(dispatch) > 1 else ''} {_nums(dispatch)} go{'es' if len(dispatch) == 1 else ''}"
                     " through an interface or base method: which implementation runs is decided at run time, and the map"
                     " lists every one it knows.")
    if any("later_elsewhere" in s for s in out_steps):
        notes.append("A step marked later_elsewhere writes data (a file, a table, a key) that other code reads later; the"
                     " reader does not run as part of this walk.")
    pipes = _pipes(con, nodes, shown, {s["id"] for s in steps})
    for p in pipes:
        notes.append(f"{p['type']} started {p['starts']} in {p['in']} ({p['at']}) and talks to it over its pipes: what"
                     f" it writes there is read by {p['program']}, and the walk does not follow those messages as calls."
                     f" Walk from {p['program']} to see the other side.")
    if truncated:
        notes.append(f"The walk stops at {MAX_DEPTH} calls deep or {MAX_WALK} steps; code past that is not in it.")
    if len(steps) == 1:
        notes.append("Nothing on the map is called from here. It may call through something the map cannot follow: a"
                     " callback it is handed, a framework, a dynamic import or a value whose type is not written.")
    notes.append("The map follows calls whose target it can work out from the text, and channels it can match by name"
                 " or address. Calls through callbacks, framework hooks, reflection or values of unknown type are not in"
                 " the walk.")
    d = walk_diagram(con, nodes, [s for s in shown], by_seq, g)
    left = len(steps) - len(shown) - len(helpers)
    out = {"from": start, **({"to": ends["to"]["id"]} if "to" in ends else {}),
           **({"through": ends["through"]["id"]} if "through" in ends else {}),
           "mode": mode, **({"flow": flow_id} if flow_id else {}), "found": True,
           "total_steps": len(steps), "shown": len(shown),
           "steps": out_steps,
           **({"helpers": {"count": len(helpers), "names": sorted({_label(con, h["id"], nodes) for h in helpers})[:12],
                           "why": f"functions called from {HELPER_CALLERS} or more places outside tests that call next"
                                  " to nothing themselves: counted, not shown"}} if helpers else {}),
           **({"left_out": left} if left > 0 else {}),
           **({"talks_over_pipes": pipes} if pipes else {}),
           "not_seen": notes, "mermaid": d.get("mermaid", ""), "diagram_legend": diagrams.legend(d, "") if d.get("mermaid") else ""}
    out["text"] = walk_text(out)
    out["next"] = ("Read the code of the steps that matter (`source` with the step's id) before stating what they do."
                   " To keep this walk, save it as a tour (`save_tour`, one stop per step that matters).")
    return out


def _pipes(con, nodes: diagrams._Nodes, shown: list[dict], walked: set) -> list[dict]:
    """Programs that a type in the walk started with pipes and that the walk does not enter: the walk's messages to
    them (a `send`, a `write` to stdin) are not calls the map can follow, so the other side is said in words."""
    out, seen = [], set()
    for unit in dict.fromkeys(nodes.unit(s["id"]) for s in shown):
        u = nodes.get(unit)
        if u is None or u["kind"] != "type":
            continue
        for src, dst, raw in con.execute("SELECT e.src_id, e.dst_id, e.attrs FROM edges e JOIN nodes n ON n.id = e.src_id"
                                         " WHERE e.kind = 'communicates' AND n.parent_id = ?", (unit,)):
            a = diagrams._attrs(raw)
            if a.get("channel") != "process" or not a.get("pipes") or dst in walked or (unit, dst) in seen:
                continue
            seen.add((unit, dst))
            sn = nodes.get(src) or {}
            out.append({"type": u["name"], "starts": a.get("address") or "a program", "in": _label(con, src, nodes),
                        "at": f"{sn.get('path')}:{a.get('launched_at') or sn.get('span_start')}",
                        "program": _label(con, dst, nodes), "program_id": dst})
    return out[:4]


def _nums(ks: list[int]) -> str:
    ks = [str(k) for k in ks[:8]]
    return ", ".join(ks[:-1]) + " and " + ks[-1] if len(ks) > 1 else ks[0]


def walk_diagram(con, nodes: diagrams._Nodes, shown: list[dict], by_seq: dict, g: _Graph,
                 max_participants: int = 8, max_messages: int = 25) -> dict:
    """A Mermaid sequence diagram of a walk: one arrow per step, in the walk's order, each an edge on the map."""
    pid: dict[str, str] = {}
    lines, arrows, left = [], [], 0

    def part(u):
        if u not in pid:
            pid[u] = f"P{len(pid) + 1}"
        return pid[u]
    if shown:
        part(nodes.unit(shown[0]["id"]))
    for s in shown:
        parent = by_seq.get(s["parent"]) if s["parent"] is not None else None
        if parent is None:
            continue
        e = diagrams._backing(con, nodes, parent["id"], s["id"], s["via"])
        a, b = nodes.unit(parent["id"]), nodes.unit(s["id"])
        if e is None or len(arrows) >= max_messages or len(set(pid) | {a, b}) > max_participants:
            left += 1
            continue
        if e["kind"] == "channel":
            head = "--)" if e["guess"] else "-)"
            label = diagrams._channel_text(e["channel"], e.get("address") or "", nodes, s["id"])
        else:
            head = "-->>" if e["guess"] else "->>"
            label = f"{nodes.fn(s['id'])}()" + (" (implementation)" if e["kind"] == "dispatch" else "")
            if e["kind"] == "call" and "/route:" in s["id"]:
                label = f"registers {nodes.fn(s['id'])}"
            if e["kind"] == "runs":
                label = f"runs {nodes.fn(s['id'])}"
        lines.append(f"    {part(a)}{head}{part(b)}: {diagrams._text(label)}")
        arrows.append({"from": parent["id"], "to": s["id"], "kind": e["kind"], "guess": e["guess"],
                       **({"channel": e["channel"], "address": e.get("address") or ""} if e["kind"] == "channel" else {})})
    if left and pid:
        order = sorted(pid, key=lambda u: int(pid[u][1:]))
        span = pid[order[0]] + ("," + pid[order[-1]] if len(order) > 1 else "")
        lines.append(f"    Note over {span}: and {left} more step{'s' if left != 1 else ''} not drawn")
    labels = {}
    for u in pid:
        n = nodes.get(u) or {"name": u, "kind": "", "path": ""}
        labels[u] = n["name"] if n["kind"] != "module" else (n.get("path") or n["name"])
    head = ["sequenceDiagram"] + [f"    participant {pid[u]} as {diagrams._text(labels[u])}"
                                  for u in sorted(pid, key=lambda u: int(pid[u][1:]))]
    return {"mermaid": "\n".join(head + lines) if arrows else "", "arrows": arrows, "left_out": left,
            "guessed": any(a["guess"] for a in arrows), "channels": any(a["kind"] == "channel" for a in arrows)}


def walk_text(r: dict) -> str:
    """The walk as numbered lines, indented by call depth."""
    if not r.get("found", True):
        return r.get("note", "")
    first = r["steps"][0] if r["steps"] else None
    head = {"walk": "Walk from", "path": "Shortest path from", "through": "Path from"}[r["mode"]]
    L = [f"{head} {first['name'] if first else r['from']}"
         + (f" to {r['steps'][-1]['name']}" if r["mode"] == "path" and r["steps"] else "")
         + (f" through {r['through']}" if r["mode"] == "through" else "")
         + f": {r['shown']} of {r['total_steps']} steps shown" + (f" (along the flow {r['flow']})" if r.get("flow") else "")
         + "."]
    base = min((s["depth"] for s in r["steps"]), default=0)
    part = None
    for s in r["steps"]:
        pad = "   " * min(s["depth"] - base, 8)
        # A named part of the code (module_outline, name_part), said where the walk enters it.
        into = f"  [in {s['part']}]" if s.get("part") and s["part"] != part else ""
        part = s.get("part")
        if s["how"] == "start":
            L.append(f"{s['n']:>3}. {pad}{s['name']}  ({s['at']}){into}")
        else:
            L.append(f"{s['n']:>3}. {pad}{s['how']} -> {s['name']}  ({s['at']})" + ("  [guessed link]" if s.get("guessed") else "")
                     + into)
        if s.get("call"):
            L.append(f"     {pad}   at {s.get('call_line', '')}: {s['call']}")
        if s.get("declaration") and s["how"] != "call":
            L.append(f"     {pad}   declared: {s['declaration']}")
        if s.get("crosses"):
            L.append(f"     {pad}   crosses {s['crosses']}")
        for x in s.get("later_elsewhere", ()):
            L.append(f"     {pad}   later, elsewhere: {x['what']} ({x['reader']})")
    if r.get("helpers"):
        L.append(f"Helpers counted, not shown ({r['helpers']['count']}): {', '.join(r['helpers']['names'])}.")
    if r.get("left_out"):
        L.append(f"Left out: {r['left_out']} deeper steps; raise max_steps to see more.")
    L += ["", "What the map could not see:"] + [f"- {n}" for n in r["not_seen"]]
    return "\n".join(L)


def diagram(con, ids: list[str]) -> dict:
    """diagrams.sequence for any functions or types, named by id or by name."""
    got, missing = [], []
    for i in ids:
        r = _resolve(con, i)
        (got.append(r["id"]) if "id" in r else missing.append({"name": i, "error": r.get("error"),
                                                                "candidates": r.get("candidates", [])[:5]}))
    if not got:
        return {"error": "none of the ids is on the map; find them with `search` or `find_flows`.", "missing": missing}
    d = diagrams.sequence(con, got, marked="focus")
    if not d.get("mermaid"):
        return {"error": "nothing to draw: give functions or types (a type stands for its methods).", "missing": missing}
    return {"mermaid": d["mermaid"], "participants": d["participants"], "focus": d["focus"],
            "legend": diagrams.legend(d, "The code you named is shaded."), "left_out": d["left_out"],
            "arrows": len(d["arrows"]), **({"missing": missing} if missing else {}),
            **({"not_drawn": d["focus_left_out"]} if d["focus_left_out"] else {})}
