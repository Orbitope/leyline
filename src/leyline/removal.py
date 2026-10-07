"""Tasks that take code away: "Remove `X`" and "Rename `X` to `Y`".

`check` counts a task done when the code it names changed. For a removal that is too weak: `X` edited is not `X`
removed, and `X` deleted while something still calls it is half a removal (the build or the run breaks there).
So these two tasks are judged by what is left:

    Remove `X`            proven when `X` is gone and nothing that called it still names it;
                          partial when `X` is gone but a caller still calls it; contradicted when `X` still exists.
    Rename `X` to `Y`     the same for `X`, and `Y` must be there too.

Callers are those the baseline (the code as it was planned) linked to `X`: calls, and for a type or a file its
uses and imports. A caller still calls `X` when it is still on the map and its text still has `X`'s name outside a
comment. Only tasks written that way are judged so: "Remove a parameter from `X`" removes no `X`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from . import diff, verdicts

KIND_WORDS = (r"(?:the|an?|unused|dead|old|obsolete|legacy|deprecated|stale|now[- ]unused|function|method|class|type|"
              r"field|property|constant|helper|file|module|interface|enum|struct|variable|member|attribute|route|"
              r"endpoint|test)")
REMOVE = re.compile(r"^\s*(?:remove|delete|drop)\s+(?:" + KIND_WORDS + r"\s+)*`", re.I)
RENAME = re.compile(r"^\s*rename\s+(?:" + KIND_WORDS + r"\s+)*`([^`]+)`\s+(?:to|as|into)\s+(?:the\s+)?`([^`]+)`", re.I)
USES = ("instantiates", "uses_type", "extends", "implements", "imports", "reads", "writes", "overrides")
COMMENT = re.compile(r"^\s*(#|//|/\*|\*|--|')")


def _is_path(written: str) -> bool:
    from . import spec
    return "/" in written or bool(spec.FILE_NAME.fullmatch(written))


def _leaf(written: str) -> str:
    """The name a node of this written name has: `Engine.start` -> start; a file's path stays whole."""
    w = re.sub(r"\(.*\)$", "", written.strip())
    return w if _is_path(w) else w.split(".")[-1]


def _word(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")


def _targets(text: str) -> Optional[tuple[str, list[str], Optional[str]]]:
    """("remove", [X, ...], None) or ("rename", [X], Y) for a task written as a removal or a rename, else None."""
    m = RENAME.match(text)
    if m:
        return "rename", [m.group(1).strip()], m.group(2).strip()
    if not REMOVE.match(text):
        return None
    from . import spec
    cut = next((c.start() for c in spec.CLAUSE.finditer(text)), len(text))
    head = text[:cut]
    first = head.index("`")
    close = head.find("`", first + 1)
    if close < 0 or re.match(r"\s*['’]s\b", head[close + 1:]):   # "Remove `X`'s parameter": not X
        return None
    leads = [w for w, role in spec._roles(head) if role == "lead"]
    return ("remove", leads, None) if leads else None


class _Baseline:
    """The code as it was planned, as kept in the change's snapshot."""

    def __init__(self, con, cid: str):
        snap = diff.snapshot_path(con, cid)
        self.db = diff._open(snap) if snap.exists() else None

    def row(self, i: str):
        return self.db.execute("SELECT id, kind, name, parent_id, path FROM nodes WHERE id = ?", (i,)).fetchone()

    def users(self, i: str) -> list[tuple[str, str]]:
        """(user, how) for everything the baseline links to `i` or to something inside it."""
        n = len(i) + 1
        out = [(r[0], "calls") for r in self.db.execute(
            "SELECT DISTINCT src_id FROM calls WHERE dst_id = ? OR substr(dst_id, 1, ?) IN (?, ?)", (i, n, i + ".", i + "("))]
        out += [(r[0], "imports" if r[1] == "imports" else "uses") for r in self.db.execute(
            f"SELECT DISTINCT src_id, kind FROM edges WHERE kind IN ({','.join('?' * len(USES))})"
            f" AND (dst_id = ? OR substr(dst_id, 1, ?) IN (?, ?))", (*USES, i, n, i + ".", i + "("))]
        return list(dict.fromkeys(out))

    def close(self):
        if self.db is not None:
            self.db.close()


def _text(con, i: str, cache: dict) -> Optional[str]:
    """A node's own text as it is now, comment lines left out."""
    r = con.execute("SELECT repo_id, path, kind, span_start, span_end FROM nodes WHERE id = ?", (i,)).fetchone()
    if r is None or not r["path"]:
        return None
    key = (r["repo_id"], r["path"])
    if key not in cache:
        data = diff.source(con, *key)
        cache[key] = data.decode("utf-8", errors="replace").split("\n") if data else None
    lines = cache[key]
    if lines is None:
        return None
    s, e = (1, len(lines)) if r["kind"] == "file" or not r["span_start"] else (r["span_start"], r["span_end"] or r["span_start"])
    return "\n".join(ln for ln in lines[s - 1:e] if not COMMENT.match(ln))


def _still_there(names, i: str, kind: str, bases: dict) -> bool:
    """Whether the node is on the map now: by id, or by owner and name when only its parameters changed."""
    if i in names.by_id:
        return True
    if kind != "callable":
        return False
    if "set" not in bases:
        bases["set"] = {diff._base(x) for x, r in names.by_id.items() if r["kind"] == "callable"}
    return diff._base(i) in bases["set"]


def _callers(con, names, base: _Baseline, i: str, leaf: str, removed: set, cache: dict) -> list[tuple[str, str, bool]]:
    """Code that used `i` before the change and still names it: (label, how, is a test), code first."""
    from . import spec
    name = Path(leaf).stem if _is_path(leaf) else leaf   # a file is named by its stem where it is imported
    word = _word(name)
    # A caller of the same name (a pytest fixture `child` that calls `engine.child()`) declares it: not a use.
    own = re.compile(r"\b(?:def|function|fn|func|class|interface|struct|enum|sub)\s+" + re.escape(name) + r"(?![\w$])")
    out = []
    for src, how in base.users(i):
        if src == i or src.startswith((i + ".", i + "/", i + "(")) or src in removed or src not in names.by_id:
            continue
        text = _text(con, src, cache)
        if text and word.search(own.sub(" ", text)):
            out.append((spec._label(names, src), how, names.in_tests(src)))
    return sorted(dict.fromkeys(out), key=lambda c: c[2])


def _present(con, names, x_row, y: str) -> list[str]:
    """The new name of a rename on the map, beside where the old one was: the node ids that answer to it."""
    from . import spec
    w = re.sub(r"\(.*\)$", "", y.strip())
    if "/" in w or spec.FILE_NAME.fullmatch(w) and x_row["kind"] == "file":
        return [f["id"] for f in names.file(w)]
    parts = w.split(".")
    owner = parts[-2] if len(parts) > 1 else None
    out = []
    for r in names.by_name.get(parts[-1], []):
        if r["kind"] == "file" or (x_row["kind"] in ("callable", "field", "type") and r["kind"] != x_row["kind"]):
            continue
        parent = names.by_id.get(r["parent_id"])
        if r["parent_id"] == x_row["parent_id"] or r["path"] == x_row["path"] or (owner and parent is not None and parent["name"] == owner):
            out.append(r["id"])
    return out


def _who(callers: list[tuple[str, str, bool]]) -> str:
    """"parse still calls it", "documentXml, OfferChanges and 3 tests still call it"."""
    from . import spec
    code = [c for c, _, test in callers if not test]
    tests = [c for c, _, test in callers if test]
    if not code and len(tests) <= 3:   # only tests: name them
        code, tests = tests, []
    items = code[:3] + ([f"{len(code) - 3} more"] if len(code) > 3 else []) + ([spec._n(len(tests), "test")] if tests else [])
    hows = {h for _, h, _ in callers}
    verb = "calls" if hows == {"calls"} else "imports" if hows == {"imports"} else "uses"
    one = len(callers) == 1
    return f"{spec._and(items)} still {verb if one else verb[:-1]} it"


def check(con, cid: str, row, names, touched: set, removed: set) -> Optional[dict]:
    """A removal or rename task judged by what is left: {state, verdict, why, missing, made}, or None when the task is not
    one, or when nothing changed at all (the usual reading says so)."""
    if row is None or row["action"] not in ("remove", "rename") or not (touched or removed):
        return None
    found = _targets(row["text"])
    ids = json.loads(row["nodes"] or "[]")
    if not found or not ids:
        return None
    kind, written, new = found
    base = _Baseline(con, cid)
    if base.db is None:
        return None
    try:
        rows = {i: base.row(i) for i in ids}
        cache, bases, judged, made = {}, {}, [], []
        for w in written:
            leaf = _leaf(w)
            mine = [i for i in ids if rows[i] is not None and (rows[i]["name"] == leaf or rows[i]["kind"] == "file"
                                                                and (rows[i]["path"] or "").endswith(leaf.lstrip("./")))]
            if not mine:
                continue
            here = [i for i in mine if _still_there(names, i, rows[i]["kind"], bases)]
            if here:
                edited = any(diff._base(t) == diff._base(i) for i in here for t in touched)
                judged.append((verdicts.CONTRADICTED, f"`{w}` still exists" + ("; it was edited, not removed" if edited and kind == "remove"
                                                                                 else ""), f"{w} still exists"))
                continue
            if new is not None:
                there = _present(con, names, rows[mine[0]], new)
                made += there
                if not there:
                    judged.append((verdicts.CONTRADICTED, f"`{w}` is gone, but `{new}` is not on the map", f"{new}"))
                    continue
            callers = [c for i in mine for c in _callers(con, names, base, i, leaf, removed, cache)]
            callers = list(dict.fromkeys(callers))
            if callers:
                judged.append((verdicts.PARTIAL, f"`{w}` is gone but {_who(callers)}",
                               f"{callers[0][0]} still {'calls' if callers[0][1] == 'calls' else 'uses'} {w}"))
            else:
                judged.append((verdicts.PROVEN, f"`{w}` is gone" + (f" and `{new}` is there" if new else "")
                               + ", and nothing still calls it", ""))
    finally:
        base.close()
    if not judged:
        return None
    rank = (verdicts.CONTRADICTED, verdicts.PARTIAL, verdicts.PROVEN)
    worst = min((v for v, _, _ in judged), key=rank.index)
    if worst == verdicts.PROVEN:
        why = "; ".join(w for _, w, _ in judged)
    else:
        why = "; ".join(w for v, w, _ in judged if v != verdicts.PROVEN)
    return {"state": {verdicts.CONTRADICTED: "not done", verdicts.PARTIAL: "partly"}.get(worst, "done"),
            "verdict": worst, "why": why, "missing": [m for _, _, m in judged if m],
            "made": made}   # the new name of a rename: what the task added, not an edit outside the spec
