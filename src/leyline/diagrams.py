"""Sequence diagrams of how execution reaches and leaves some code, drawn from the map.

A diagram a language model draws for a pull request reads well and is often wrong: it invents a participant, or
turns a message on a queue into a call that waits for its answer. Here every arrow is an edge on the map: a call,
a channel link (http, a queue, a table, a launched program), or a call through an interface into one of its
implementations. The path in comes from the stored flows, which walk the calls from an entry point in source order;
the calls out of the changed code are listed in the order they appear in its text.

    sequence(con, ids)                    the diagram, as Mermaid text, and the arrows it draws
    unbacked(con, d)                      arrows of a diagram with no edge behind them on the map (should be none)
    edge_changes(before, after, ids)      calls and channel links into or out of changed code, added and removed
    markdown(d) / change_lines(c)         the lines a page shows

GitHub renders a ```mermaid block in Markdown, which is where leyline.md and the pull request page are read.
"""

from __future__ import annotations

import json
import re
from typing import Optional

CHANNEL_VIA = ("event", "process", "http", "channel", "di", "queue", "rpc", "file", "db")
SHADE = "rgba(255, 196, 0, 0.18)"
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|specs?)(/|$)|[._-](test|spec)s?\.[A-Za-z]+$|(^|/)test_[^/]+$|Tests?\.cs$")
TOP = ("<module>", "<top-level>")


def _cols(con, table: str) -> set:
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except Exception:
        return set()


def drawable(con) -> bool:
    """Whether a store keeps what a diagram needs: where each call sits, how sure each link is, which channel a link
    crosses, and the order of each flow. A comparison's slim baseline keeps only which pairs are linked, so a
    diagram of the code as it was cannot be drawn from it honestly."""
    return ({"site_start", "precision"} <= _cols(con, "calls") and {"attrs", "precision"} <= _cols(con, "edges")
            and {"parent_seq", "seq"} <= _cols(con, "flow_steps"))


class _Nodes:
    def __init__(self, con):
        self.con, self.rows = con, {}

    def get(self, i: Optional[str]) -> Optional[dict]:
        if i is None:
            return None
        if i not in self.rows:
            r = self.con.execute("SELECT id, kind, name, parent_id, path, span_start FROM nodes WHERE id = ?", (i,)).fetchone()
            self.rows[i] = dict(zip(("id", "kind", "name", "parent_id", "path", "span_start"), r)) if r else None
        return self.rows[i]

    def unit(self, i: str) -> str:
        """The participant a function belongs to: its type, else its file."""
        cur, seen = i, set()
        while cur and cur not in seen:
            seen.add(cur)
            n = self.get(cur)
            if n is None:
                break
            if n["kind"] in ("type", "file", "module"):
                return cur
            cur = n["parent_id"]
        return i

    def fn(self, i: str) -> str:
        n = self.get(i) or {"name": i.rsplit(".", 1)[-1], "kind": "callable"}
        if n["name"] in TOP:
            return "top level"
        name = n["name"]
        return name if len(name) <= 48 else name[:45] + "..."

    def label(self, i: str) -> str:
        """`Owner.name`, or `name` for a function at the top of a file."""
        n = self.get(i)
        if n is None:
            return i.rsplit(":", 1)[-1]
        if n["kind"] in ("callable", "field"):
            u = self.get(self.unit(i))
            if n["name"] in TOP:
                return f"the top level of {n['path']}" if n.get("path") else "a file's top level"
            if u is not None and u["kind"] in ("type", "file"):   # Owner.name, or file.name, as the other pages say it
                return f"{u['name']}.{self.fn(i)}"
        return self.fn(i)

    def is_test(self, i: str) -> bool:
        n = self.get(i)
        return bool(n) and (n["kind"] == "test" or bool(TEST_PATH.search(n["path"] or "")))


def _text(s: str) -> str:
    """Text Mermaid shows as written: no statement breaks, entity codes, comments or tags."""
    s = s.replace(";", ",").replace("#", "").replace("%%", "%").replace("<", "[").replace(">", "]")
    return " ".join(s.split())


def _attrs(raw) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _channel_text(channel: str, address: str, nodes: _Nodes, dst: str) -> str:
    if channel in ("db", "file"):   # nothing runs across: the reader reads what was written, later
        what = ("table " if channel == "db" else "file ") + address if address else "a " + ("table" if channel == "db" else "file")
        return f"writes {what}, which {nodes.fn(dst)}() reads later"
    if channel == "process":
        return f"starts {address or 'a program'}"
    if channel == "di":
        return f"{nodes.fn(dst)}(), the registered implementation"
    return f"{channel} {address}".strip()


def _backing(con, nodes: _Nodes, src: str, dst: str, via: str) -> Optional[dict]:
    """The edge on the map behind one step of a flow, or None."""
    if via == "calls":
        r = con.execute("SELECT COUNT(*), MIN(site_start), MIN(precision = 'guess') FROM calls WHERE src_id = ? AND dst_id = ?",
                        (src, dst)).fetchone()
        return {"kind": "call", "guess": bool(r[2]), "line": r[1]} if r[0] else None
    if via == "dispatch":
        r = con.execute("SELECT precision FROM edges WHERE kind = 'overrides' AND src_id = ? AND dst_id = ?", (dst, src)).fetchone()
        return {"kind": "dispatch", "guess": r[0] == "guess"} if r else None
    if via == "runs":
        n = nodes.get(dst)
        return {"kind": "runs", "guess": False} if n and n["parent_id"] == src else None
    rows = con.execute("SELECT precision, attrs FROM edges WHERE kind = 'communicates' AND src_id = ? AND dst_id = ?",
                       (src, dst)).fetchall()
    rows = sorted(rows, key=lambda r: _attrs(r[1]).get("channel") != via)
    if not rows:
        return None
    a = _attrs(rows[0][1])
    return {"kind": "channel", "channel": a.get("channel", via), "address": a.get("address") or "",
            "guess": rows[0][0] == "guess", "line": a.get("launched_at") or a.get("line")}


def _chain(con, nodes: _Nodes, fid: str) -> Optional[dict]:
    """The path a stored flow takes from its start to `fid`: from an entry point if one reaches it, else from a
    test; the one that reaches it in the fewest calls."""
    has_layer = "layer" in _cols(con, "flows")
    rows = con.execute("SELECT s.flow_id, s.seq, s.depth, f.entry_id, f.name, f.attrs FROM flow_steps s JOIN flows f ON f.id = s.flow_id"
                       " WHERE s.callable_id = ?" + (" AND f.layer = 'fact'" if has_layer else ""), (fid,)).fetchall()
    if not rows:
        return None
    best = min(rows, key=lambda r: (_attrs(r[5]).get("kind") != "entry", r[2] or 0, r[4] or "", r[0]))
    flow, seq = best[0], best[1]
    links, guard = [], 0
    while seq is not None and guard < 64:
        guard += 1
        s = con.execute("SELECT seq, callable_id, via, site_line, parent_seq FROM flow_steps WHERE flow_id = ? AND seq = ?",
                        (flow, seq)).fetchone()
        if s is None or s[4] is None or s[2] == "start":
            break
        p = con.execute("SELECT callable_id FROM flow_steps WHERE flow_id = ? AND seq = ?", (flow, s[4])).fetchone()
        if p is None:
            break
        links.append((p[0], s[1], s[2], s[3]))
        seq = s[4]
    links.reverse()
    return {"flow": best[4], "kind": _attrs(best[5]).get("kind") or "entry", "entry": best[3], "links": links}


def _focus(con, nodes: _Nodes, ids: list[str]) -> list[str]:
    """Functions to draw for the ids given: a type stands for its methods. Tests only when nothing else changed."""
    out = []
    for i in dict.fromkeys(ids):
        n = nodes.get(i)
        if n is None:
            continue
        if n["kind"] in ("callable", "test"):
            out.append(i)
        elif n["kind"] == "type":
            out += [r[0] for r in con.execute("SELECT id FROM nodes WHERE parent_id = ? AND kind = 'callable' ORDER BY span_start, id", (i,))]
    out = list(dict.fromkeys(out))
    product = [i for i in out if not nodes.is_test(i)] or out
    # A file's top level whose text changed only because a function in it did is not drawn when that function is.
    return [i for i in product if (nodes.get(i) or {}).get("name") not in TOP] or product


def sequence(con, focus_ids: list[str], max_participants: int = 8, max_messages: int = 25, max_focus: int = 4,
             max_hops_in: int = 3, max_channels_in: int = 3) -> dict:
    """A Mermaid sequence diagram of how execution reaches the functions in `focus_ids` (types stand for their
    methods) and what they call. Participants are the types around the functions, or the file for a function at the
    top of a file. Returns {"mermaid", "arrows", "participants", "focus", "focus_left_out", "left_out", ...}; "arrows"
    lists each drawn arrow with the edge behind it, so the drawing can be checked against the map (`unbacked`)."""
    nodes = _Nodes(con)
    every = _focus(con, nodes, focus_ids)
    focus = every[:max_focus]
    fset = set(focus)
    children: dict[str, list] = {}
    keys: set = set()
    roots: list[tuple] = []          # (node, note)
    must: list[str] = []

    def add(src, dst, e, order):
        key = (src, dst, e["kind"])
        if key in keys:
            return
        keys.add(key)
        children.setdefault(src, []).append((order if order is not None else 10 ** 9, len(keys), dst, e))

    for f in focus:
        must.append(nodes.unit(f))
        ch = _chain(con, nodes, f)
        if ch and ch["links"]:
            backed = []
            for src, dst, via, line in ch["links"]:
                e = _backing(con, nodes, src, dst, via)
                if e is None:    # a step the map cannot show as an edge: start the drawing below it
                    backed = []
                    continue
                backed.append((src, dst, e, line))
            hidden = len(ch["links"]) - len(backed)
            kept = backed[-max_hops_in:]
            hidden += len(backed) - len(kept)
            if kept:
                start = kept[0][0]
                e_row = nodes.get(ch["entry"])
                entry = (e_row["path"] if e_row and e_row["name"] in TOP and e_row["path"] else
                         nodes.label(ch["entry"]) if e_row else ch["flow"])
                what = "the test" if ch["kind"] == "test" else "the entry point"
                note = (f"starts at {what} {entry}" if not hidden else
                        f"reached from {what} {entry} through {hidden} more call{'s' if hidden != 1 else ''}")
                roots.append((start, note))
                for src, dst, e, line in kept:
                    add(src, dst, e, line)
                    must += [nodes.unit(src), nodes.unit(dst)]
        # Code reached over a channel (a request, a message): the senders, outside tests first.
        ins = con.execute("SELECT src_id, precision, attrs FROM edges WHERE kind = 'communicates' AND dst_id = ?"
                          " ORDER BY src_id", (f,)).fetchall()
        ins = sorted(ins, key=lambda r: (nodes.is_test(r[0]), r[0]))
        for src, prec, raw in ins[:max_channels_in]:
            a = _attrs(raw)
            add(src, f, {"kind": "channel", "channel": a.get("channel", "channel"), "address": a.get("address") or "",
                         "guess": prec == "guess"}, None)
            if not any(r[0] == src for r in roots):
                roots.append((src, None))
        for k in range(max_channels_in, len(ins)):
            keys.add(("more-in", k, f))
        if not any(r[0] == f for r in roots) and not any(f == c[2] for cs in children.values() for c in cs):
            roots.append((f, None))
        # What it calls, in the order its text calls it, and the channels it sends on.
        for dst, line, guess in con.execute("SELECT dst_id, MIN(site_start) AS line, MIN(precision = 'guess') FROM calls"
                                            " WHERE src_id = ? AND dst_id != src_id GROUP BY dst_id ORDER BY line, dst_id", (f,)):
            if nodes.get(dst) is not None:
                add(f, dst, {"kind": "call", "guess": bool(guess), "line": line}, line)
        for dst, prec, raw in con.execute("SELECT dst_id, precision, attrs FROM edges WHERE kind = 'communicates' AND src_id = ?"
                                          " ORDER BY dst_id", (f,)):
            a = _attrs(raw)
            line = a.get("launched_at") or a.get("line")
            add(f, dst, {"kind": "channel", "channel": a.get("channel", "channel"), "address": a.get("address") or "",
                         "guess": prec == "guess", "line": line}, line)
    for cs in children.values():
        cs.sort(key=lambda c: (c[0], c[1]))
    must = list(dict.fromkeys(must))

    pid: dict[str, str] = {}
    lines: list[str] = []
    arrows: list[dict] = []
    expanded: set = set()

    def fits(units):
        new = [u for u in dict.fromkeys(units) if u not in pid]
        return not [u for u in new if u not in must] or len(pid) + len(new) <= max_participants

    def part(u):
        if u not in pid:
            pid[u] = f"P{len(pid) + 1}"
        return pid[u]

    def arrow(src, dst, e):
        a, b = part(nodes.unit(src)), part(nodes.unit(dst))
        if e["kind"] == "channel":
            head = "--)" if e["guess"] else "-)"
            label = _channel_text(e["channel"], e.get("address") or "", nodes, dst)
        else:
            head = "-->>" if e["guess"] else "->>"
            label = f"{nodes.fn(dst)}()" + (" (implementation)" if e["kind"] == "dispatch" else "")
            if e["kind"] == "call" and "/route:" in dst:   # a route's inline handler, which its registrar hands over
                label = f"registers {nodes.fn(dst)}"
            if e["kind"] == "runs":
                label = f"runs {nodes.fn(dst)}"
        lines.append(f"    {a}{head}{b}: {_text(label)}")
        arrows.append({"from": src, "to": dst, "kind": e["kind"], "guess": e["guess"],
                       **({"channel": e["channel"], "address": e.get("address") or ""} if e["kind"] == "channel" else {})})

    def shaded(node, draw_in):
        """The changed function's run, shaded: the arrow into it (if any), a note, and what it calls."""
        lines.append(f"    rect {SHADE}")
        draw_in()
        lines.append(f"    Note over {part(nodes.unit(node))}: changed: {_text(nodes.label(node))}")
        emit(node)
        lines.append("    end")

    def emit(node):
        if node in expanded:
            return
        expanded.add(node)
        for _order, _k, dst, e in children.get(node, ()):
            if len(arrows) >= max_messages or not fits([nodes.unit(node), nodes.unit(dst)]):
                continue
            if dst in fset and dst not in expanded:
                shaded(dst, lambda: arrow(node, dst, e))
            else:
                arrow(node, dst, e)
                emit(dst)

    for node, note in roots:
        if len(arrows) >= max_messages:
            break
        if note and node not in expanded:
            lines.append(f"    Note over {part(nodes.unit(node))}: {_text(note)}")
        if node in fset and node not in expanded:
            shaded(node, lambda: None)
        else:
            emit(node)
    for f in focus:
        if f not in expanded and fits([nodes.unit(f)]):
            shaded(f, lambda: None)
    left = len(keys) - len(arrows)
    order = sorted(pid, key=lambda u: int(pid[u][1:]))
    if left > 0 and order:
        span = pid[order[0]] + ("," + pid[order[-1]] if len(order) > 1 else "")
        lines.append(f"    Note over {span}: and {left} more call{'s' if left != 1 else ''} not drawn")

    labels: dict[str, str] = {}
    for u in order:
        n = nodes.get(u) or {"name": u, "kind": "", "path": ""}
        labels[u] = n["name"] if n["kind"] != "module" else (n.get("path") or n["name"])
    seen: dict[str, int] = {}
    for u in order:
        seen[labels[u]] = seen.get(labels[u], 0) + 1
    for u in order:     # two files named index.ts: say which folder
        n = nodes.get(u)
        if seen[labels[u]] > 1 and n and n.get("path"):
            labels[u] = "/".join(n["path"].split("/")[-2:])
    head = ["sequenceDiagram"] + [f"    participant {pid[u]} as {_text(labels[u])}" for u in order]
    return {"mermaid": "\n".join(head + lines) if order else "",
            "arrows": arrows, "participants": [labels[u] for u in order],
            "focus": [nodes.label(f) for f in focus], "focus_left_out": [nodes.label(f) for f in every[max_focus:]],
            # no call or channel on the map leads in, and no flow starts there: nothing in the repository runs it
            "nothing_reaches": [nodes.label(f) for f in focus if not any(k[1] == f for k in keys)
                                and con.execute("SELECT 1 FROM calls WHERE dst_id = ? LIMIT 1", (f,)).fetchone() is None
                                and con.execute("SELECT 1 FROM flows WHERE entry_id = ? LIMIT 1", (f,)).fetchone() is None],
            "left_out": max(left, 0),
            "guessed": any(a["guess"] for a in arrows), "channels": any(a["kind"] == "channel" for a in arrows)}


def unbacked(con, d: dict) -> list[dict]:
    """The arrows of a diagram with no edge behind them on the map. Every arrow should have one."""
    nodes = _Nodes(con)
    bad = []
    for a in d.get("arrows") or []:
        if a["kind"] == "call":
            ok = con.execute("SELECT 1 FROM calls WHERE src_id = ? AND dst_id = ? LIMIT 1", (a["from"], a["to"])).fetchone()
        elif a["kind"] == "dispatch":
            ok = con.execute("SELECT 1 FROM edges WHERE kind = 'overrides' AND src_id = ? AND dst_id = ?", (a["to"], a["from"])).fetchone()
        elif a["kind"] == "runs":
            ok = (nodes.get(a["to"]) or {}).get("parent_id") == a["from"]
        else:
            ok = any(_attrs(r[0]).get("channel", "channel") == a["channel"] and (_attrs(r[0]).get("address") or "") == a["address"]
                     for r in con.execute("SELECT attrs FROM edges WHERE kind = 'communicates' AND src_id = ? AND dst_id = ?",
                                          (a["from"], a["to"])))
        if not ok:
            bad.append(a)
    return bad


# -- before and after ---------------------------------------------------------------------------------------
def _base(i: str) -> str:
    head, sep, _ = i.rpartition("(")
    return head if sep and i.endswith(")") else i


def _links(con, ids: set) -> tuple[set, dict]:
    """Calls and channel links with an end in `ids`: {(kind, src, dst)} and the channel of each channel link, when the
    store keeps it."""
    if not ids:
        return set(), {}
    marks = ",".join("?" * len(ids))
    args = list(ids) * 2
    out, info = set(), {}
    for s, d in con.execute(f"SELECT DISTINCT src_id, dst_id FROM calls WHERE src_id IN ({marks}) OR dst_id IN ({marks})", args):
        if s != d:
            out.add(("calls", s, d))
    with_attrs = "attrs" in _cols(con, "edges")
    for r in con.execute(f"SELECT src_id, dst_id{', attrs' if with_attrs else ''} FROM edges WHERE kind = 'communicates'"
                         f" AND (src_id IN ({marks}) OR dst_id IN ({marks}))", args):
        out.add(("channel", r[0], r[1]))
        if with_attrs:
            a = _attrs(r[2])
            info[(r[0], r[1])] = {"channel": a.get("channel", "channel"), "address": a.get("address") or ""}
    return out, info


def edge_changes(before, after, changed: list[str], removed: list[str] = (), limit: int = 400) -> dict:
    """Calls and channel links into or out of the changed code, added and removed between the baseline (before) and
    the store (after). `changed` are ids in the store (edited, added, re-signed); `removed` ids only the baseline has.
    A method whose parameter list changed is matched to its old id."""
    a_nodes, b_nodes = _Nodes(after), _Nodes(before)
    now = {i for i in list(changed)[:limit] if a_nodes.get(i) is not None}
    was = {i for i in now if b_nodes.get(i) is not None} | {i for i in list(removed)[:limit] if b_nodes.get(i) is not None}
    remap = {}
    for i in now:
        base = _base(i)
        if base != i and i not in was:
            for (old,) in before.execute("SELECT id FROM nodes WHERE substr(id, 1, ?) = ? AND substr(id, ?, 1) = '('",
                                         (len(base), base, len(base) + 1)):
                if a_nodes.get(old) is None:
                    remap[old] = i
                    was.add(old)
    lb, info_b = _links(before, was)
    la, info_a = _links(after, now)
    lb = {(k, remap.get(s, s), remap.get(d, d)) for k, s, d in lb}
    touched = now | {remap.get(i, i) for i in was}
    la = {x for x in la if x[1] in touched or x[2] in touched}
    lb = {x for x in lb if x[1] in touched or x[2] in touched}

    def item(con_nodes, k, s, d, info):
        x = {"from": con_nodes.get(s) and con_nodes.label(s) or s, "to": con_nodes.get(d) and con_nodes.label(d) or d,
             "from_id": s, "to_id": d}
        if k == "channel":
            x.update(info.get((s, d)) or {"channel": None, "address": ""})
        return x

    def guessed(s, d):
        r = after.execute("SELECT MIN(precision = 'guess') FROM calls WHERE src_id = ? AND dst_id = ?", (s, d)).fetchone()
        return bool(r and r[0])
    old_label = lambda i: i if b_nodes.get(i) is not None else next((o for o, n in remap.items() if n == i), i)
    return {
        "calls_added": [{**item(a_nodes, k, s, d, info_a), "guess": guessed(s, d)} for k, s, d in sorted(la - lb) if k == "calls"],
        "calls_removed": [{**item(_Either(b_nodes, a_nodes, old_label), k, s, d, info_b),
                           # the callee is gone but the caller is not: the call is still written, and now breaks
                           "callee_removed": d in set(removed) and a_nodes.get(s) is not None,
                           "caller_edited": s in now or remap.get(s, s) in now}
                          for k, s, d in sorted(lb - la) if k == "calls"],
        "channels_added": [item(a_nodes, k, s, d, info_a) for k, s, d in sorted(la - lb) if k == "channel"],
        "channels_removed": [item(_Either(b_nodes, a_nodes, old_label), k, s, d,
                                  {(remap.get(s2, s2), remap.get(d2, d2)): v for (s2, d2), v in info_b.items()})
                             for k, s, d in sorted(lb - la) if k == "channel"],
    }


class _Either:
    """Labels from the baseline, else from the store (a re-signed method's new id)."""
    def __init__(self, first: _Nodes, second: _Nodes, old):
        self.first, self.second, self.old = first, second, old

    def get(self, i):
        return self.first.get(self.old(i)) or self.second.get(i)

    def label(self, i):
        o = self.old(i)
        return self.first.label(o) if self.first.get(o) is not None else self.second.label(i)


def for_change(before, after, changed: list[str], removed: list[str] = ()) -> dict:
    """What a page shows for a change after it is made: the diagram of the changed code as it is now, the calls and
    channel links it gained and lost, and a diagram of it as it was when the baseline keeps enough to draw one."""
    nodes = _Nodes(after)
    changed = [i for i in changed if not nodes.is_test(i)] or list(changed)
    out = {"after": sequence(after, changed), "changes": edge_changes(before, after, changed, removed)}
    if drawable(before):
        nodes = _Nodes(before)
        was = [i for i in list(changed) + list(removed) if nodes.get(i) is not None]
        if was:
            out["before"] = sequence(before, was)
    return out


# -- the lines a page shows ---------------------------------------------------------------------------------------
def legend(d: dict, shaded: str = "The changed code is shaded.") -> str:
    out = ("Drawn from the map: every arrow is a call or a channel link found in the code, in the order the code makes"
           " them where that order is known. " + shaded)
    if d.get("channels"):
        out += (" An open arrowhead crosses a channel (a request, a message, a table, another program): the far side runs"
                " later, or in another process, and nothing checks the two sides against each other.")
    if d.get("guessed"):
        out += " A dotted arrow is a link the map guessed by name: read the code before relying on it."
    if d.get("focus_left_out"):
        out += f" Not drawn, to keep it readable: {', '.join(d['focus_left_out'][:6])}" + (
            f" and {len(d['focus_left_out']) - 6} more" if len(d["focus_left_out"]) > 6 else "") + "."
    if d.get("nothing_reaches"):
        out += f" Nothing on the map reaches {', '.join(d['nothing_reaches'][:4])}."
    return out


def markdown(d: Optional[dict], intro: str = "", shaded: str = "The changed code is shaded.") -> list[str]:
    """A diagram as a page shows it: a sentence, the ```mermaid block, and what its arrows mean."""
    if not d or not d.get("mermaid"):
        return []
    return ([intro, ""] if intro else []) + ["```mermaid", d["mermaid"], "```", "", legend(d, shaded)]


def _cap(xs: list[str], n: int = 12) -> list[str]:
    return xs[:n] + ([f"- and {len(xs) - n} more"] if len(xs) > n else [])


def change_lines(c: Optional[dict]) -> list[str]:
    """What changed in how the code runs, one link a line."""
    if not c:
        return []

    def over(x):
        if not x.get("channel"):
            return "over a channel (the baseline does not keep which)"
        return "over " + (f"{x['channel']} {x['address']}".strip())
    L = _cap([f"- `{x['from']}` now calls `{x['to']}`" + (" (a link guessed by name)" if x.get("guess") else "") for x in c["calls_added"]])
    L += _cap([(f"- `{x['from']}` called `{x['to']}`, which was removed; `{x['from']}` was edited too"
                if x.get("caller_edited") else f"- `{x['from']}` still calls `{x['to']}`, which was removed")
               if x.get("callee_removed") else f"- `{x['from']}` no longer calls `{x['to']}`" for x in c["calls_removed"]])
    L += _cap([f"- `{x['from']}` now reaches `{x['to']}` {over(x)}" for x in c["channels_added"]], 8)
    L += _cap([f"- `{x['from']}` no longer reaches `{x['to']}` {over(x)}" for x in c["channels_removed"]], 8)
    return L or ["No call or channel link into or out of the changed code was added or removed."]


def section(view: Optional[dict], heading: str) -> list[str]:
    """A change's diagrams and its list of changed links, under a heading (for the check page and the pull request
    page)."""
    if not view or view.get("error"):
        return []
    L = ["", heading, ""]
    L += markdown(view.get("after"), "The changed code as it runs now:") or ["Nothing in the changed code is on the map as a function to draw."]
    if view.get("before"):
        L += [""] + markdown(view["before"], "As it ran before the change:", "The code the change edits is shaded.")
    L += ["", "**What changed in how it runs** (calls and channel links into or out of the changed code, compared with"
              " the baseline):", ""] + change_lines(view.get("changes"))
    return L


def safe(fn, *args, **kw) -> Optional[dict]:
    """A diagram is an aid to reading: one that cannot be drawn must not stop the page it sits on."""
    try:
        return fn(*args, **kw)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def for_snapshot(snap, after, changed: list[str], removed: list[str] = ()) -> Optional[dict]:
    """for_change() against a change's baseline file. None when there is no baseline."""
    from pathlib import Path
    from . import diff
    if not Path(snap).exists():
        return None
    before = diff._open(Path(snap))
    try:
        return for_change(before, after, changed, removed)
    finally:
        before.close()
