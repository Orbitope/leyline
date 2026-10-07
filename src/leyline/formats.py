"""Code joined by a string's shape, not by a call: one function builds a key such as `dialogue/<d>/nodes/<n>/text`,
and another takes keys of that shape apart. Nothing calls across, so a change to the shape on one side breaks
the other side silently.

`extract` finds both sides in a parsed file and records them as `format` endpoints:

- a writer is a template with holes and at least two fixed parts between separators (`/` or `:`): a template
  literal, an f-string, an interpolated string, `"%s/nodes/%s" % ...`, `"{}/nodes/{}".format(...)`,
  `String.Format("{0}/nodes/{1}", ...)` or a chain of `+`. A hole that is a call to another key builder
  (`${dialogueNodeKey(d, n)}/text`) is filled in with that builder's shape when the two are linked.
- a reader is a regular expression over such a shape (`/^dialogue\\/([^/]+)\\/nodes\\//`), a startsWith, endsWith
  or includes test of a literal with two or more fixed parts, or a function that compares the pieces of a split
  key by position (`parts[2] === "nodes"`, `rest[0] !== "text"`).

`resolve` links a writer to a reader when two or more fixed parts of the reader sit at the same places in the
writer's shape and no fixed part disagrees. Routes (`/api/...`) and file paths (a part with a dot, a leading `/`)
are left to the http and file channels. The link is data-like: the writer's end is the hub, and a reader must
read what the writer now writes."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Optional

from .model import Edge

WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*\Z")
DIGITS = re.compile(r"\d+\Z")
SEPS = ("/", ":")
HOLE = "*"
# First parts that make a shape a URL or a path, which other channels own.
NOT_KEYS = frozenset("api http https www v1 v2 v3 static assets public src dist build node_modules tmp var usr etc home".split()
                     # git's own names (refs/heads/x): both ends talk to git, not to each other
                     + ["refs"])
PREFIX_TESTS = {"startsWith": "start", "startswith": "start", "StartsWith": "start", "endsWith": "end", "endswith": "end",
                "EndsWith": "end", "includes": "any", "Contains": "any", "indexOf": "any", "IndexOf": "any"}
REGEX_FNS = {"compile", "match", "fullmatch", "search", "findall", "finditer", "sub", "subn", "split"}
CS_REGEX_FNS = {"Match", "IsMatch", "Matches", "Replace", "Split"}
TOO_MANY_WRITERS = 12   # one shape built in this many places is a convention, not one format's two ends
TOO_MANY_READERS = 25


def _t(node) -> str:
    return node.text.decode("utf8", "replace") if node is not None else ""


# -- shapes ------------------------------------------------------------------------------------------------

def shape_of(pieces: list) -> Optional[tuple[str, list[str]]]:
    """(separator, parts) for a template given as pieces: ("c", text) for fixed text, ("h", None) for a hole,
    ("call", name) for a hole that is a call, ("v", value) for a hole filled by a known constant. A part is fixed
    text, `*` for a hole, or `@name` for a call to fill in later. None when the template is not a key."""
    text = "".join(p[1] if p[0] in ("c", "v") else "\x00" for p in pieces)
    if not text or len(text) > 200 or "://" in text or text.startswith(("/", "http", "\\")) or "\n" in text:
        return None
    lead_call = bool(pieces) and pieces[0][0] == "call"
    sep = next((s for s in SEPS if text.count(s) >= 2 or (lead_call and text.count(s) >= 1)), None)
    if sep is None:
        return None
    segs: list[list] = [[]]
    for kind, val in pieces:
        if kind in ("c", "v"):
            bits = val.split(sep)
            for i, b in enumerate(bits):
                if i:
                    segs.append([])
                if b:
                    segs[-1].append(("c", b))
        else:
            segs[-1].append((kind, val))
    if segs and not segs[-1]:
        segs.pop()                      # a trailing separator
    out = []
    for seg in segs:
        if not seg:
            return None                 # a leading or doubled separator: a path
        if all(k == "c" for k, _ in seg):
            word = "".join(v for _, v in seg)
            if not (WORD.match(word) or DIGITS.match(word)):
                return None             # spaces, dots, quotes: prose, a file name, markup
            out.append(word)
        elif len(seg) == 1 and seg[0][0] == "call":
            out.append("@" + seg[0][1])
        else:
            fixed = "".join(v for k, v in seg if k == "c")
            if fixed and not re.fullmatch(r"[A-Za-z0-9_\-]+", fixed):
                return None
            out.append(HOLE)
    return sep, out


def is_key(parts: list[str]) -> bool:
    """A finished writer shape: fixed first part, two or more fixed words, at least one hole."""
    words = [p for p in parts if WORD.match(p)]
    return (len(parts) >= 3 and bool(WORD.match(parts[0])) and parts[0].lower() not in NOT_KEYS
            and len(words) >= 2 and HOLE in parts and not any(p.startswith("@") for p in parts))


def address(sep: str, parts: list[str]) -> str:
    return sep.join(parts)


def regex_shape(pattern: str) -> Optional[tuple[str, list[str], bool]]:
    """(separator, parts, anchored) for a regular expression over a key: ^dialogue\\/([^/]+)\\/nodes -> dialogue/*/nodes.
    Only a plain sequence of words and groups is read; alternation at the top, or a part with a dot, is not."""
    if len(pattern) > 300:
        return None
    anchored = pattern.startswith("^")
    body = pattern[1:] if anchored else pattern
    body = re.sub(r"\$$|\\z$|\\Z$", "", body)
    sep = "/" if body.count("/") >= 2 else ":" if body.count(":") >= 2 else None
    if sep is None:
        return None
    segs, cur, depth, cls, i = [], "", 0, False, 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt == sep and depth == 0 and not cls:
                segs.append(cur)
                cur, i = "", i + 2
                continue
            cur += ch + nxt
            i += 2
            continue
        if cls:
            cls = ch != "]"
        elif ch == "[":
            cls = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "|" and depth == 0:
            return None
        elif ch == sep and depth == 0:
            segs.append(cur)
            cur, i = "", i + 1
            continue
        cur += ch
        i += 1
    segs.append(cur)
    if segs and segs[-1] == "":
        segs.pop()
    if not segs or segs[0] == "":
        return None
    out = []
    for s in segs:
        plain = s.replace("\\-", "-").replace("\\_", "_")
        if WORD.match(plain):
            out.append(plain)
        elif "\\." in s or re.search(r"(?<!\\)\.\w", s) and not re.search(r"[(\[]", s):
            return None                 # a file name
        elif re.search(r"[(\[.\\+*?{]", s):
            out.append(HOLE)
        else:
            return None
    words = [p for p in out if WORD.match(p)]
    if len(words) < 2 or HOLE not in out:
        return None
    return sep, out, anchored


def literal_shape(text: str) -> Optional[tuple[str, list[str]]]:
    """(separator, fixed parts) of a literal a key is tested against: "dialogue/" -> no (one part); "nodes/x/" -> two."""
    sep = next((s for s in SEPS if s in text), None)
    if sep is None or len(text) > 120 or any(ch.isspace() for ch in text):
        return None
    parts = [p for p in text.split(sep)]
    inner = [p for p in parts if p]
    if len(inner) < 2 or not all(WORD.match(p) for p in inner) or text.startswith("/") and inner[0].lower() in NOT_KEYS:
        return None
    if parts[0] == "" and text.startswith("/") and len(parts) > 1 and parts[1].lower() in NOT_KEYS:
        return None
    return sep, inner


# -- extraction --------------------------------------------------------------------------------------------

class _Base:
    """Walks one file and records format ends. Subclasses say how each language writes the shapes."""

    def __init__(self, tree, where, out, consts: dict):
        self.tree, self.where, self.out = tree, where, out
        self.consts = {k: v[0] for k, v in consts.items() if len(v) == 1 and v[0] and WORD.match(v[0])}
        self.index: dict[tuple, dict] = defaultdict(lambda: defaultdict(set))   # (fn, array) -> position -> words
        self.index_line: dict[tuple, int] = {}
        self.alias: dict[tuple, tuple] = {}      # (fn, name) -> (array, position)
        self.origin: dict[tuple, Optional[int]] = {}   # (fn, array) -> offset into a split key, None when unknown
        self.splits: dict[str, set] = defaultdict(set)   # fn -> separators it splits keys on
        self.done: set = set()
        self.held: dict[str, tuple] = {}   # a module constant or field holding a pattern -> the pattern's shape

    def fn(self, node) -> Optional[str]:
        return self.where.fn(node.start_point[0] + 1)

    def writer(self, node, pieces) -> None:
        if (node.start_byte, node.end_byte) in self.done:
            return
        self.done.add((node.start_byte, node.end_byte))
        got = shape_of(pieces)
        src = self.fn(node)
        if got is None or src is None:
            return
        sep, parts = got
        parts = parts[:1] + [HOLE if p.startswith("@") else p for p in parts[1:]]   # only a leading builder is filled in
        if not (parts[0].startswith("@") and len(parts) >= 2) and not is_key(parts):
            return
        self.out.add("format", "write", src, address(sep, parts), node.start_point[0] + 1, "template")

    def reader(self, node, sep, parts, kind, anchored=False) -> None:
        src = self.fn(node)
        if src is None:
            return
        n = self.where.nodes.get(src)
        if n is not None and (n.kind == "type" or n.name in ("<module>", "<top-level>")):
            # A pattern kept in a module constant or a field is read where the code uses it.
            holder, cur = None, node
            for _ in range(4):
                cur = cur.parent
                if cur is None:
                    break
                if cur.type in ("assignment", "variable_declarator"):
                    name = cur.child_by_field_name("left") or cur.child_by_field_name("name") \
                        or next((c for c in cur.named_children if c.type == "identifier"), None)
                    holder = _t(name).rsplit(".", 1)[-1] if name is not None else None
                    break
            if holder and WORD.match(holder):
                self.held[holder] = (sep, parts, kind, anchored, src, node.start_point[0] + 1)
                return
        self.out.add("format", "read", src, address(sep, parts), node.start_point[0] + 1, kind,
                     ["anchored"] if anchored else [])

    def uses_of_held(self) -> None:
        if not self.held:
            return
        used = set()
        stack = [self.tree.root_node]
        while stack:
            n = stack.pop()
            if n.type in ("identifier", "property_identifier") and _t(n) in self.held:
                src = self.fn(n)
                node = self.where.nodes.get(src) if src else None
                if node is not None and node.kind in ("callable", "test") and node.name not in ("<module>", "<top-level>"):
                    sep, parts, kind, anchored, _s, _l = self.held[_t(n)]
                    used.add(_t(n))
                    self.out.add("format", "read", src, address(sep, parts), n.start_point[0] + 1, kind,
                                 ["anchored"] if anchored else [])
            stack.extend(n.children)
        for name, (sep, parts, kind, anchored, src, line) in self.held.items():
            if name not in used:   # used nowhere in this file: the constant itself is the reader
                self.out.add("format", "read", src, address(sep, parts), line, kind, ["anchored"] if anchored else [])

    def compare(self, node, sub_array: str, pos: int, words: list[str]) -> None:
        src = self.fn(node)
        if src is None:
            return
        key = (src, sub_array)
        if key in self.alias:
            sub_array, base = self.alias[key]
            pos += base
            key = (src, sub_array)
        for w in words:
            if WORD.match(w):
                self.index[key][pos].add(w)
        self.index_line.setdefault(key, node.start_point[0] + 1)

    def finish(self) -> None:
        self.uses_of_held()
        for (src, arr), at in self.index.items():
            if len(at) < 2:
                continue
            off = self.origin.get((src, arr), "?")
            seps = self.splits.get(src) or set(SEPS)
            lits = [f"{p}={'|'.join(sorted(ws))}" for p, ws in sorted(at.items())]
            if isinstance(off, int):
                lits.append(f"offset={off}")
            for sep in sorted(seps):
                shown = " ".join(f"[{p}]={'|'.join(sorted(ws))}" for p, ws in sorted(at.items()))
                self.out.add("format", "read", src, f"{arr}{sep} {shown}", self.index_line[(src, arr)], "index", lits)


class _TypeScript(_Base):
    def pieces(self, node) -> Optional[list]:
        t = node.type
        if t == "template_string":
            out = []
            for c in node.children:
                if c.type in ("string_fragment", "escape_sequence"):
                    out.append(("c", _t(c)))
                elif c.type == "template_substitution":
                    out.append(self.hole(c.named_children[0] if c.named_children else None))
            return out
        if t == "string":
            return [("c", "".join(_t(c) for c in node.children if c.type in ("string_fragment", "escape_sequence")))]
        if t == "parenthesized_expression" and node.named_children:
            return self.pieces(node.named_children[0])
        if t == "binary_expression" and _t(node.child_by_field_name("operator")) == "+":
            a, b = self.pieces(node.child_by_field_name("left")), self.pieces(node.child_by_field_name("right"))
            return (a or [("h", None)]) + (b or [("h", None)]) if (a or b) else None
        return None

    def hole(self, e):
        if e is None:
            return ("h", None)
        if e.type == "identifier" and _t(e) in self.consts:
            return ("v", self.consts[_t(e)])
        if e.type == "call_expression":
            f = e.child_by_field_name("function")
            if f is not None and f.type == "identifier":
                return ("call", _t(f))
        return ("h", None)

    def run(self) -> None:
        stack = [self.tree.root_node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "template_string" and any(c.type == "template_substitution" for c in n.children):
                self.writer(n, self.pieces(n) or [])
            elif t == "binary_expression":
                op = _t(n.child_by_field_name("operator"))
                if op == "+" and not (n.parent is not None and n.parent.type == "binary_expression"
                                      and _t(n.parent.child_by_field_name("operator")) == "+"):
                    p = self.pieces(n)
                    if p and any(k == "c" for k, _ in p) and any(k != "c" for k, _ in p):
                        self.writer(n, p)
                elif op in ("===", "!==", "==", "!="):
                    self._compare(n, n.child_by_field_name("left"), n.child_by_field_name("right"))
                    self._compare(n, n.child_by_field_name("right"), n.child_by_field_name("left"))
            elif t == "regex":
                got = regex_shape(_t(n.child_by_field_name("pattern")))
                if got:
                    self.reader(n, got[0], got[1], "regex", got[2])
            elif t == "call_expression":
                self._call(n)
            elif t == "variable_declarator":
                self._declare(n)
            elif t == "switch_statement":
                self._switch(n)
            stack.extend(reversed(n.children))
        self.finish()

    def _sub(self, node):
        """(array, position) for a[2]."""
        while node is not None and node.type in ("parenthesized_expression", "non_null_expression"):
            node = node.named_children[0] if node.named_children else None
        if node is not None and node.type == "subscript_expression":
            obj, idx = node.child_by_field_name("object"), node.child_by_field_name("index")
            if obj is not None and obj.type == "identifier" and idx is not None and idx.type == "number" and _t(idx).isdigit():
                return _t(obj), int(_t(idx))
        if node is not None and node.type == "identifier":
            key = (self.fn(node), _t(node))
            if key in self.alias:
                return self.alias[key][0], self.alias[key][1]
        return None

    def _compare(self, n, side, other) -> None:
        at = self._sub(side)
        if at is None or other is None or other.type != "string":
            return
        self.compare(n, at[0], at[1], [(self.pieces(other) or [("c", "")])[0][1]])

    def _switch(self, n) -> None:
        at = self._sub(n.child_by_field_name("value"))
        body = n.child_by_field_name("body")
        if at is None or body is None:
            return
        words = [_t(v)[1:-1] for c in body.named_children if c.type == "switch_case"
                 for v in [c.child_by_field_name("value")] if v is not None and v.type == "string"]
        self.compare(n, at[0], at[1], words)

    def _declare(self, n) -> None:
        name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
        if name is None or value is None:
            return
        src = self.fn(n)
        while value.type in ("non_null_expression", "parenthesized_expression", "as_expression") and value.named_children:
            value = value.named_children[0]
        if name.type == "identifier":
            at = self._sub(value)
            if at is not None:
                self.alias[(src, _t(name))] = at
                return
            if value.type == "call_expression":
                f = value.child_by_field_name("function")
                args = value.child_by_field_name("arguments")
                if f is not None and f.type == "member_expression":
                    prop = _t(f.child_by_field_name("property"))
                    obj = f.child_by_field_name("object")
                    if prop == "split":
                        self.origin[(src, _t(name))] = 0
                    elif prop == "slice" and obj is not None and obj.type == "identifier" and args is not None \
                            and args.named_children and args.named_children[0].type == "number" and _t(args.named_children[0]).isdigit():
                        self.alias[(src, _t(name))] = (_t(obj), int(_t(args.named_children[0])))   # rest = parts.slice(2)
        elif name.type == "array_pattern" and value.type == "call_expression":
            f = value.child_by_field_name("function")
            if f is not None and f.type == "member_expression" and _t(f.child_by_field_name("property")) == "split":
                arr = f"[{name.start_point[0]}]"
                self.origin[(src, arr)] = 0
                for i, c in enumerate(name.named_children):
                    if c.type == "identifier":
                        self.alias[(src, _t(c))] = (arr, i)
                    elif c.type == "rest_pattern" and c.named_children and c.named_children[0].type == "identifier":
                        self.alias[(src, _t(c.named_children[0]))] = (arr, i)   # [kind, id, ...rest]

    def _call(self, n) -> None:
        f = n.child_by_field_name("function")
        args = n.child_by_field_name("arguments")
        if f is None or f.type != "member_expression" or args is None or not args.named_children:
            return
        prop = _t(f.child_by_field_name("property"))
        first = args.named_children[0]
        if prop == "split" and first.type == "string":
            s = _t(first)[1:-1]
            if s in SEPS:
                src = self.fn(n)
                if src:
                    self.splits[src].add(s)
        elif prop in PREFIX_TESTS and first.type == "string":
            got = literal_shape(_t(first)[1:-1])
            if got:
                self.reader(n, got[0], got[1], PREFIX_TESTS[prop], PREFIX_TESTS[prop] == "start")


class _Python(_Base):
    def pieces(self, node) -> Optional[list]:
        t = node.type
        if t == "string":
            out = []
            for c in node.children:
                if c.type in ("string_content", "escape_sequence"):
                    out.append(("c", _t(c)))
                elif c.type == "interpolation":
                    e = c.named_children[0] if c.named_children else None
                    out.append(self.hole(e))
            return out
        if t == "concatenated_string":
            return [p for c in node.named_children for p in (self.pieces(c) or [])]
        if t == "parenthesized_expression" and node.named_children:
            return self.pieces(node.named_children[0])
        if t == "binary_operator" and _t(node.child_by_field_name("operator")) == "+":
            a, b = self.pieces(node.child_by_field_name("left")), self.pieces(node.child_by_field_name("right"))
            return (a or [("h", None)]) + (b or [("h", None)]) if (a or b) else None
        return None

    def hole(self, e):
        if e is None:
            return ("h", None)
        if e.type == "identifier" and _t(e) in self.consts:
            return ("v", self.consts[_t(e)])
        if e.type == "call":
            f = e.child_by_field_name("function")
            if f is not None and f.type == "identifier":
                return ("call", _t(f))
        return ("h", None)

    @staticmethod
    def holes(text: str) -> list:
        """A %-format or str.format template as pieces: "%s/nodes/%s" -> hole, "/nodes/", hole."""
        out, last = [], 0
        for m in re.finditer(r"%[-#0 +]*\d*(?:\.\d+)?[sdirf]|\{[^{}]*\}", text):
            out.append(("c", text[last:m.start()]))
            out.append(("h", None))
            last = m.end()
        out.append(("c", text[last:]))
        return [p for p in out if p != ("c", "")]

    def run(self) -> None:
        stack = [self.tree.root_node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "string" and any(c.type == "interpolation" for c in n.children):
                self.writer(n, self.pieces(n) or [])
            elif t == "binary_operator":
                op = _t(n.child_by_field_name("operator"))
                left = n.child_by_field_name("left")
                if op == "%" and left is not None and left.type == "string":
                    self.writer(n, self.holes("".join(v for _, v in (self.pieces(left) or []) if v)))
                elif op == "+" and not (n.parent is not None and n.parent.type == "binary_operator"
                                        and _t(n.parent.child_by_field_name("operator")) == "+"):
                    p = self.pieces(n)
                    if p and any(k == "c" for k, _ in p) and any(k != "c" for k, _ in p):
                        self.writer(n, p)
            elif t == "comparison_operator":
                self._compare(n)
            elif t == "call":
                self._call(n)
            elif t == "assignment":
                self._assign(n)
            stack.extend(reversed(n.children))
        self.finish()

    def _sub(self, node):
        if node is not None and node.type == "subscript":
            obj, idx = node.child_by_field_name("value"), node.child_by_field_name("subscript")
            if obj is not None and obj.type == "identifier" and idx is not None and idx.type == "integer":
                return _t(obj), int(_t(idx))
        if node is not None and node.type == "identifier":
            key = (self.fn(node), _t(node))
            if key in self.alias:
                return self.alias[key]
        return None

    def _compare(self, n) -> None:
        kids = n.children
        if len(kids) != 3:
            return
        a, op, b = kids
        if _t(op) not in ("==", "!=", "in", "not in"):
            return
        for side, other in ((a, b), (b, a)):
            at = self._sub(side)
            if at is None:
                continue
            if other.type == "string":
                words = ["".join(_t(c) for c in other.children if c.type == "string_content")]
            elif other.type in ("tuple", "list", "set") and _t(op) in ("in", "not in"):
                words = ["".join(_t(c) for c in s.children if c.type == "string_content") for s in other.named_children
                         if s.type == "string"]
            else:
                continue
            self.compare(n, at[0], at[1], words)
            return

    def _assign(self, n) -> None:
        left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
        if left is None or right is None:
            return
        src = self.fn(n)
        if left.type == "identifier":
            at = self._sub(right)
            if at is not None:
                self.alias[(src, _t(left))] = at
                return
            if right.type == "call":
                f = right.child_by_field_name("function")
                if f is not None and f.type == "attribute" and _t(f.child_by_field_name("attribute")) in ("split", "rsplit"):
                    self.origin[(src, _t(left))] = 0
            elif right.type == "subscript":
                obj, sl = right.child_by_field_name("value"), right.child_by_field_name("subscript")
                if obj is not None and obj.type == "identifier" and sl is not None and sl.type == "slice":
                    lo = sl.named_children[0] if sl.named_children else None
                    if lo is not None and lo.type == "integer" and _t(sl).startswith(_t(lo)):
                        self.alias[(src, _t(left))] = (_t(obj), int(_t(lo)))
        elif left.type in ("pattern_list", "tuple_pattern") and right.type == "call":
            f = right.child_by_field_name("function")
            if f is not None and f.type == "attribute" and _t(f.child_by_field_name("attribute")) == "split":
                arr = f"[{left.start_point[0]}]"
                self.origin[(src, arr)] = 0
                for i, c in enumerate(left.named_children):
                    if c.type == "identifier":
                        self.alias[(src, _t(c))] = (arr, i)

    def _call(self, n) -> None:
        f = n.child_by_field_name("function")
        args = n.child_by_field_name("arguments")
        if f is None or args is None or not args.named_children:
            return
        first = args.named_children[0]
        name = _t(f.child_by_field_name("attribute")) if f.type == "attribute" else _t(f)
        if f.type == "attribute" and name == "format" and f.child_by_field_name("object") is not None \
                and f.child_by_field_name("object").type == "string":
            obj = f.child_by_field_name("object")
            self.writer(n, self.holes("".join(v for _, v in (self.pieces(obj) or []) if v)))
            return
        if first.type != "string" or any(c.type == "interpolation" for c in first.children):
            return
        text = "".join(_t(c) for c in first.children if c.type == "string_content")
        if f.type == "attribute" and name in ("split", "rsplit") and text in SEPS:
            src = self.fn(n)
            if src:
                self.splits[src].add(text)
        elif f.type == "attribute" and name in PREFIX_TESTS:
            got = literal_shape(text)
            if got:
                self.reader(n, got[0], got[1], PREFIX_TESTS[name], PREFIX_TESTS[name] == "start")
        elif name in REGEX_FNS and f.type == "attribute" and _t(f.child_by_field_name("object")) in ("re", "regex"):
            got = regex_shape(text)
            if got:
                self.reader(n, got[0], got[1], "regex", got[2] or name in ("match", "fullmatch"))


class _CSharp(_Base):
    def pieces(self, node) -> Optional[list]:
        t = node.type
        if t == "interpolated_string_expression":
            out = []
            for c in node.children:
                if c.type == "interpolation":
                    e = next((x for x in c.named_children if not x.type.startswith("interpolation_")), None)
                    out.append(self.hole(e))
                elif c.is_named and not c.type.startswith("interpolation_"):
                    out.append(("c", _t(c)))
            return out
        if t in ("string_literal", "verbatim_string_literal", "raw_string_literal"):
            return [("c", self.text(node))]
        if t == "parenthesized_expression" and node.named_children:
            return self.pieces(node.named_children[0])
        if t == "binary_expression" and _t(node.child_by_field_name("operator")) == "+":
            a, b = self.pieces(node.child_by_field_name("left")), self.pieces(node.child_by_field_name("right"))
            return (a or [("h", None)]) + (b or [("h", None)]) if (a or b) else None
        return None

    @staticmethod
    def text(node) -> str:
        body = "".join(_t(c) for c in node.children if c.type.endswith("content"))
        return body or _t(node).lstrip("@").strip('"')

    def hole(self, e):
        if e is None:
            return ("h", None)
        if e.type == "identifier" and _t(e) in self.consts:
            return ("v", self.consts[_t(e)])
        if e.type == "invocation_expression":
            f = e.child_by_field_name("function")
            if f is not None and f.type == "identifier":
                return ("call", _t(f))
        return ("h", None)

    def run(self) -> None:
        stack = [self.tree.root_node]
        while stack:
            n = stack.pop()
            t = n.type
            if t == "interpolated_string_expression":
                self.writer(n, self.pieces(n) or [])
            elif t == "binary_expression":
                op = _t(n.child_by_field_name("operator"))
                if op == "+" and not (n.parent is not None and n.parent.type == "binary_expression"
                                      and _t(n.parent.child_by_field_name("operator")) == "+"):
                    p = self.pieces(n)
                    if p and any(k == "c" for k, _ in p) and any(k != "c" for k, _ in p):
                        self.writer(n, p)
                elif op in ("==", "!="):
                    for side, other in ((n.child_by_field_name("left"), n.child_by_field_name("right")),
                                        (n.child_by_field_name("right"), n.child_by_field_name("left"))):
                        at = self._sub(side)
                        if at is not None and other is not None and other.type == "string_literal":
                            self.compare(n, at[0], at[1], [self.text(other)])
                            break
            elif t == "invocation_expression":
                self._call(n)
            elif t == "object_creation_expression":
                ty = _t(n.child_by_field_name("type")).rsplit(".", 1)[-1]
                args = n.child_by_field_name("arguments")
                first = self._args(args)[:1]
                if ty == "Regex" and first and first[0].type in ("string_literal", "verbatim_string_literal", "raw_string_literal"):
                    got = regex_shape(self.text(first[0]))
                    if got:
                        self.reader(n, got[0], got[1], "regex", got[2])
            elif t == "variable_declarator":
                self._declare(n)
            elif t == "attribute" and _t(n.child_by_field_name("name")) in ("GeneratedRegex", "GeneratedRegexAttribute"):
                for a in n.named_children:
                    for s in a.named_children if a.type == "attribute_argument_list" else ():
                        lit = s.named_children[0] if s.named_children else None
                        if lit is not None and lit.type in ("string_literal", "verbatim_string_literal"):
                            got = regex_shape(self.text(lit))
                            if got:
                                self.reader(n, got[0], got[1], "regex", got[2])
                            break
            stack.extend(reversed(n.children))
        self.finish()

    @staticmethod
    def _args(args) -> list:
        return [a.named_children[-1] for a in args.children if a.type == "argument" and a.named_children] if args is not None else []

    def fn(self, node):
        return self.where.fn(node.start_point[0] + 1) or self.where.type(node.start_point[0] + 1)

    def _sub(self, node):
        if node is not None and node.type == "element_access_expression":
            obj = node.child_by_field_name("expression")
            sub = node.child_by_field_name("subscript")
            idx = _t(sub).strip("[] ") if sub is not None else ""
            if obj is not None and obj.type == "identifier" and idx.isdigit():
                return _t(obj), int(idx)
        if node is not None and node.type == "identifier":
            key = (self.fn(node), _t(node))
            if key in self.alias:
                return self.alias[key]
        return None

    def _declare(self, n) -> None:
        name = n.child_by_field_name("name") or next((c for c in n.named_children if c.type == "identifier"), None)
        value = n.named_children[-1] if n.named_child_count > 1 else None
        if value is not None and value.type == "equals_value_clause":
            value = value.named_children[-1] if value.named_children else None
        if name is None or value is None:
            return
        src = self.fn(n)
        at = self._sub(value)
        if at is not None:
            self.alias[(src, _t(name))] = at
        elif value.type == "invocation_expression":
            f = value.child_by_field_name("function")
            if f is not None and f.type == "member_access_expression" and _t(f.child_by_field_name("name")) == "Split":
                self.origin[(src, _t(name))] = 0

    def _call(self, n) -> None:
        f = n.child_by_field_name("function")
        args = self._args(n.child_by_field_name("arguments"))
        if f is None or not args:
            return
        name = _t(f.child_by_field_name("name")) if f.type == "member_access_expression" else _t(f)
        owner = _t(f.child_by_field_name("expression")) if f.type == "member_access_expression" else ""
        first = args[0]
        if name == "Format" and owner in ("string", "String") and first.type in ("string_literal", "verbatim_string_literal"):
            text = self.text(first)
            out, last = [], 0
            for m in re.finditer(r"\{\d+(?:[,:][^}]*)?\}", text):
                out += [("c", text[last:m.start()]), ("h", None)]
                last = m.end()
            out.append(("c", text[last:]))
            self.writer(n, [p for p in out if p != ("c", "")])
            return
        if name == "Split" and first.type in ("character_literal", "string_literal"):
            sep = _t(first)[1:-1]
            src = self.fn(n)
            if sep in SEPS and src:
                self.splits[src].add(sep)
        elif name in PREFIX_TESTS and first.type in ("string_literal", "verbatim_string_literal"):
            got = literal_shape(self.text(first))
            if got:
                self.reader(n, got[0], got[1], PREFIX_TESTS[name], PREFIX_TESTS[name] == "start")
        elif name in CS_REGEX_FNS and owner == "Regex" and len(args) > 1 \
                and args[1].type in ("string_literal", "verbatim_string_literal", "raw_string_literal"):
            got = regex_shape(self.text(args[1]))
            if got:
                self.reader(n, got[0], got[1], "regex", got[2])


def extract(lang: str, tree, where, out, consts: Optional[dict] = None) -> None:
    """Record the format ends in one parsed file (see the module docstring). A failure loses this file's formats."""
    cls = {"typescript": _TypeScript, "python": _Python, "csharp": _CSharp}.get(lang)
    if cls is None:
        return
    try:
        cls(tree, where, out, consts or {}).run()
    except RecursionError:
        pass


# -- linking -----------------------------------------------------------------------------------------------

def _parse_addr(addr: str) -> tuple[str, list[str]]:
    sep = next((s for s in SEPS if s in addr), "/")
    return sep, addr.split(sep)


def _fits(writer: list[str], reader: list[str], anchored: bool) -> int:
    """How many fixed parts of a reader's shape sit at the same places in a writer's shape, at the best alignment
    where no fixed part disagrees. 0 when none fits."""
    best = 0
    offsets = [0] if anchored else range(0, max(1, len(writer) - 1))
    for k in offsets:
        hits, bad = 0, False
        for i, r in enumerate(reader):
            if i + k >= len(writer):
                break
            w = writer[i + k]
            if r == HOLE or w == HOLE:
                continue
            if r == w:
                hits += WORD.match(r) is not None
            else:
                bad = True
                break
        if not bad:
            best = max(best, hits)
    return best


def _fits_index(writer: list[str], at: dict[int, set], offset: Optional[int]) -> tuple[int, int]:
    """(hits, offset) for a reader that compares a split key's pieces by position: the writer's part at position
    p + offset must be one of the words compared at p."""
    best = (0, -1)
    offsets = [offset] if offset is not None else range(0, len(writer))
    for k in offsets:
        hits, bad = 0, False
        for p, words in at.items():
            if p + k >= len(writer) or writer[p + k] == HOLE:
                continue
            if writer[p + k] in words:
                hits += 1
            else:
                bad = True
                break
        if not bad and hits > best[0]:
            best = (hits, k)
    return best


def _in_tests(ix, nid: str) -> bool:
    from .channels import _test_path
    n = ix.nodes.get(nid)
    cur = n
    while cur is not None and cur.kind in ("callable", "test"):
        if cur.kind == "test" or cur.attrs.get("is_test") or cur.attrs.get("is_fixture"):
            return True
        cur = ix.nodes.get(cur.parent_id)
    return n is not None and bool(n.path) and _test_path(n.path)


def resolve(ix) -> None:
    """Link each key writer to the readers of its shape, across the workspace (one repository at a time)."""
    st = ix.channel_stats["format"]
    writers, readers = [], []
    for fid, res in ix.results.items():
        for e in res.endpoints:
            if e.channel != "format" or e.src_id not in ix.nodes:
                continue
            (writers if e.role == "write" else readers).append(e)
    # A hole that calls another key builder takes that builder's shape: `${dialogueNodeKey(d, n)}/text`.
    by_name: dict[tuple, list] = defaultdict(list)
    for e in writers:
        n = ix.nodes[e.src_id]
        by_name[(e.src_id.split(":", 1)[0], n.name)].append(e)
    done: dict[int, Optional[tuple]] = {}

    def expand(e, depth=0):
        if id(e) in done:
            return done[id(e)]
        done[id(e)] = None
        sep, parts = _parse_addr(e.address)
        out = []
        for p in parts:
            if p.startswith("@"):
                cands = [w for w in by_name.get((e.src_id.split(":", 1)[0], p[1:]), []) if w is not e]
                same = [w for w in cands if ix.file_of.get(w.src_id) == ix.file_of.get(e.src_id)]
                cands = same or cands
                shapes = {expand(w, depth + 1) for w in cands} if depth < 4 else set()
                shapes.discard(None)
                if len(shapes) != 1 or len({w.src_id for w in cands}) != 1:
                    if not out:
                        return None       # what the key starts with is not known
                    out.append(HOLE)
                    continue
                s2, p2 = next(iter(shapes))
                if s2 != sep:
                    return None
                out.extend(p2)
            else:
                out.append(p)
        got = (sep, tuple(out)) if is_key(out) else None
        done[id(e)] = got
        return got

    shaped = []
    for e in writers:
        if _in_tests(ix, e.src_id):
            st["written_in_tests"] += 1   # a test writes a sample key to feed the reader; it does not set the format
            continue
        got = expand(e)
        if got is not None:
            shaped.append((e, got[0], list(got[1])))
    st["writers"] = len(shaped)
    st["readers"] = len(readers)
    per_shape = Counter((ix.nodes[e.src_id].id.split(":", 1)[0], sep, tuple(p)) for e, sep, p in shaped)
    pairs = {}
    for r in readers:
        rrepo = r.src_id.split(":", 1)[0]
        if r.method == "index":
            sep = r.address.split(" ", 1)[0][-1]
            at: dict[int, set] = {}
            offset = None
            for lit in r.literals:
                k, _, v = lit.partition("=")
                if k == "offset":
                    offset = int(v)
                else:
                    at[int(k)] = set(v.split("|"))
        else:
            sep, rparts = _parse_addr(r.address)
        found = []   # (writer, parts, hits, offset)
        for e, wsep, wparts in shaped:
            if wsep != sep or e.src_id.split(":", 1)[0] != rrepo or e.src_id == r.src_id:
                continue
            k = 0
            if r.method == "index":
                hits, k = _fits_index(wparts, at, offset)
            elif r.method == "end":
                hits = _fits(list(reversed(wparts)), list(reversed(rparts)), True)
            else:
                hits = _fits(wparts, rparts, "anchored" in r.literals)
            if hits >= 2:
                found.append((e, wparts, hits, k))
        if r.method == "index" and offset is None and len({k for *_, k in found}) > 1:
            # The pieces come from a parameter, so where they start in the key is not seen. Two formats can share a
            # tail (dialogue/<d>/nodes/<n>/text and nodes/<n>/text): the reader is taken to read the one its own
            # file writes, else the one written in the most files, and not both.
            here = ix.file_of.get(r.src_id)
            own = {k for e, _p, _h, k in found if ix.file_of.get(e.src_id) == here}
            votes = Counter(k for k, _f in {(k, ix.file_of.get(e.src_id)) for e, _p, _h, k in found})
            top = votes.most_common()
            if len(own) == 1:
                keep = own
            elif len(top) > 1 and top[0][1] == top[1][1]:
                st["readers_of_two_formats"] += 1
                keep = set()
            else:
                keep = {top[0][0]}
            found = [f for f in found if f[3] in keep]
        for e, wparts, hits, _k in found:
            key = (e.src_id, r.src_id)
            prev = pairs.get(key)
            if prev is None or hits > prev[1]:
                pairs[key] = (address(sep, wparts), hits, r.method, r.line, e.line)
    fan_in = Counter(r for (_w, r) in pairs)
    for (w, r), (addr, hits, how, rline, wline) in sorted(pairs.items()):
        sep, parts = _parse_addr(addr)
        if per_shape[(w.split(":", 1)[0], sep, tuple(parts))] > TOO_MANY_WRITERS or fan_in[r] > TOO_MANY_READERS:
            st["too_common_to_link"] += 1
            continue
        if w == r or w not in ix.nodes or r not in ix.nodes:
            continue
        ix.edges.append(Edge("communicates", w, r, "heuristic" if hits >= 3 else "guess",
                             {"channel": "format", "address": addr, "read_by": how, "line": wline, "read_at": rline}))
        st["links"] += 1
