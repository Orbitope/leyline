"""One line saying what a function or type is for.

Two sources, the one a person or an agent wrote first. An annotation `responsibility` (the intent layer over the
inferred one, and not one marked stale because the code behind its evidence changed) says what a reader who read the
code concluded. The doc is what the code's author wrote: the comment above a declaration, or a Python docstring
inside it, read from the file when it is mapped (`declared_doc`) and kept in the node's attributes (`doc`). Every tool
that shows a node can show this line, and `find_flows` ranks by it."""

from __future__ import annotations

import json
import re
from typing import Iterable, Optional

WIDTH = 200   # characters of a summary, its first sentence if that is shorter

COMMENT = re.compile(r"^\s*(//+|#+|/\*+|\*+/?|<!--|--)\s?")


def declared_doc(lines: list[str], start: int) -> str:
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


def first_sentence(text: str, width: int = WIDTH) -> str:
    """`text` cut to its first sentence, or to `width` characters when that is shorter."""
    text = " ".join((text or "").split())
    m = re.search(r"[.!?](\s|$)", text)
    if m:
        text = text[:m.start() + 1]
    return text if len(text) <= width else text[:width - 1].rstrip() + "…"


def for_nodes(con, ids: Iterable[str], width: int = WIDTH) -> dict[str, str]:
    """The summary of each of some nodes that has one, in two queries however many there are: an annotation
    `responsibility` that is not stale (the intent layer before the inferred one), else the node's `doc`."""
    ids = list(dict.fromkeys(i for i in ids if i))
    out: dict[str, str] = {}
    for at in range(0, len(ids), 500):
        chunk = ids[at:at + 500]
        marks = ",".join("?" * len(chunk))
        said = {}
        for nid, value, layer in con.execute(
                f"SELECT node_id, value, layer FROM annotations WHERE key = 'responsibility' AND stale = 0"
                f" AND node_id IN ({marks}) ORDER BY layer = 'intent'", chunk):   # intent last, so it wins
            if value and str(value).strip():
                said[nid] = str(value)
        for nid, raw in con.execute(f"SELECT id, attrs FROM nodes WHERE id IN ({marks}) AND attrs LIKE '%\"doc\"%'", chunk):
            if nid in said:
                continue
            try:
                doc = json.loads(raw or "{}").get("doc")
            except ValueError:
                doc = None
            if doc:
                said[nid] = doc
        out.update({nid: first_sentence(text, width) for nid, text in said.items() if first_sentence(text, width)})
    return out


def annotation_text(con) -> dict[str, str]:
    """Every `responsibility` annotation that is not stale, by node id: what `find_flows` reads on each call, so an
    annotation written after the map is found at once."""
    out: dict[str, str] = {}
    for nid, value in con.execute("SELECT node_id, value FROM annotations WHERE key = 'responsibility' AND stale = 0"
                                  " ORDER BY layer = 'intent'"):
        if value and str(value).strip():
            out[nid] = str(value)
    return out
