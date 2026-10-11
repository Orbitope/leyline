"""Property-based tests: scenarios that state an invariant, and the counterexample a failing property test prints.

A scenario that says something holds for any input ("for any amount, the balance is never negative") is proven
better by a property test than by one example: the library generates inputs, and when one breaks the property it
shrinks it to the smallest that still does. Kiro writes specs this way; here it is two small things.

`plan`:   a scenario whose words state an invariant is marked, with the property-test library for the language
          of the code it is about (Hypothesis, fast-check, FsCheck).
`check`:  a failing property test's output carries its counterexample. Read from the runner's text, it is kept
          on the test's result, so the scenario's verdict says what input broke it: "fails for amount=-1".

Formats read:
    Hypothesis   Falsifying example: test_x(a=0, b=-1)       (or "Failing test case:", Hypothesis 6.130 and later)
    fast-check   Property failed after 1 tests ... Counterexample: [0,-1] ... Shrunk 32 time(s)
    FsCheck      Falsifiable, after 3 tests (1 shrink) (...): Original: ... Shrunk: ...
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Optional

# -- plan: which scenarios state an invariant ---------------------------------------------------------
# Words that say a statement holds over all inputs, not for one example. Kept narrow: "each" and "any" alone are
# too common in ordinary scenarios ("each request is logged") to mean an invariant.
INVARIANT = re.compile(r"\b(for (any|every|all|each possible)\b|always\b|never\b|no matter\b|regardless of\b|"
                       r"invariant\b|whatever (the|its)\b|under any\b|in any order\b)", re.I)

LIBRARIES = {
    "python": "Hypothesis",
    "typescript": "fast-check",
    "javascript": "fast-check",
    "csharp": "FsCheck",
    "fsharp": "FsCheck",
}
EXT = {"py": "python", "pyi": "python", "ts": "typescript", "tsx": "typescript", "mts": "typescript", "cts": "typescript",
       "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript", "cs": "csharp", "fs": "fsharp"}


def invariant(s: dict) -> bool:
    """Whether a scenario's name or its WHEN/THEN lines state an invariant: for any, for every, always, never..."""
    text = " ".join([s.get("name") or ""] + list(s.get("when") or []) + list(s.get("then") or []))
    return bool(INVARIANT.search(text))


def _language(paths: list) -> Optional[str]:
    langs = Counter(EXT.get((p or "").rsplit(".", 1)[-1].lower()) for p in paths if p and "." in p)
    langs.pop(None, None)
    return langs.most_common(1)[0][0] if langs else None


def language(con, ids: list[str], test_id: Optional[str] = None) -> Optional[str]:
    """The language a scenario's test is written in: its test's file, else the code the tasks change, else the
    repository's most common one."""
    def paths(where, args):
        return [r[0] for r in con.execute(f"SELECT path FROM nodes WHERE {where}", args)]
    if test_id:
        lang = _language(paths("id = ?", (test_id,)))
        if lang:
            return lang
    if ids:
        lang = _language(paths(f"id IN ({','.join('?' * len(ids))})", ids))
        if lang:
            return lang
    return _language(paths("kind = 'file' AND layer = 'fact'", ()))


def mark_scenarios(con, scenarios: list[dict], ids: list[str]) -> None:
    """Mark each scenario that states an invariant, in place: `invariant` and `property_library`."""
    for s in scenarios:
        if invariant(s):
            s["invariant"] = True
            s["property_library"] = LIBRARIES.get(language(con, ids, s.get("test")) or "")


def cell(s: dict) -> str:
    """What the plan's scenario table adds for an invariant scenario."""
    if not s.get("invariant"):
        return ""
    lib = s.get("property_library")
    return "; invariant: a property test fits" + (f" ({lib})" if lib else "")


# -- check: the counterexample a failing property test prints ------------------------------------------
_HYPOTHESIS = re.compile(r"(?:Falsifying(?: explicit)? example|Failing test case):\s*([A-Za-z_]\w*)\(")
_FASTCHECK = re.compile(r"Counterexample:\s*(.*\S)")   # greedy: a lazy one before \s*$ is quadratic in a run of spaces
_FC_RUNS = re.compile(r"Property failed after (\d+) tests?")
_FSCHECK = re.compile(r"Falsifiable, after (\d+) tests? \((\d+) shrinks?\)")
_PREFIX = re.compile(r"^(?:E\s{1,8}|\s*[|>]\s?)")         # pytest's `E   ` before an assertion's lines
MARK = re.compile(r"^fails for (.+) \((Hypothesis|fast-check|FsCheck)\)\.?$")
LIMIT = 200


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LIMIT else text[:LIMIT - 1].rstrip() + "…"


def _split_top(text: str) -> list[str]:
    """Split at commas that are not inside brackets or quotes."""
    out, depth, quote, cur = [], 0, None, []
    for k, ch in enumerate(text):
        if quote:
            if ch == quote and text[k - 1] != "\\":
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _hypothesis_args(lines: list[str], k: int, start: int) -> Optional[str]:
    """The arguments of `test_x(...)` from the line it opens on to its closing parenthesis."""
    buf, depth, quote, escaped = [], 1, None, False
    for j in range(k, min(len(lines), k + 60)):
        line = lines[j] if j > k else lines[j][start:]
        if j > k:
            line = _PREFIX.sub("", line)
        line = re.sub(r"\s#\s.*$", "", line)                     # `a=[],  # or any other generated value`
        for ch in line:
            if quote:
                if escaped:
                    escaped = False                               # 'a\'b"c': an escaped quote does not close it
                elif ch == "\\":
                    escaped = True
                else:
                    quote = None if ch == quote else quote
            elif ch in "\"'":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    args = [a for a in _split_top("".join(buf)) if not a.startswith("self=")]
                    return ", ".join(args)
            buf.append(ch)
        buf.append(" ")
    return None


def counterexamples(text: str) -> list[dict]:
    """Every counterexample in a test run's text: {line, library, example, func (Hypothesis: the test function),
    tests, shrinks}. The example is the smallest failing input the library found, as it printed it."""
    lines = (text or "").splitlines()
    out = []
    for k, line in enumerate(lines):
        m = _HYPOTHESIS.search(line)
        if m:
            args = _hypothesis_args(lines, k, m.end())
            if args is not None:
                out.append({"line": k, "library": "Hypothesis", "func": m.group(1), "example": _clip(args) or "no arguments"})
            continue
        m = _FASTCHECK.search(line)
        head = next((x for x in reversed(lines[max(0, k - 6):k]) if _FC_RUNS.search(x)), None)
        if m and head is not None:
            ex = m.group(1)
            if re.search(r"message:\s*\"", head):   # TAP's YAML: the message is a quoted string, its quotes escaped
                ex = ex.replace('\\"', '"').replace("\\\\", "\\")
            if ex.startswith("[") and ex.endswith("]"):
                ex = ", ".join(_split_top(ex[1:-1]))   # the property's arguments, as a tuple
            out.append({"line": k, "library": "fast-check", "example": _clip(ex) or "no arguments",
                        "tests": int(_FC_RUNS.search(head).group(1))})
            continue
        m = _FSCHECK.search(line)
        if m:
            blocks: dict = {}
            cur = None
            for x in lines[k + 1:k + 80]:
                x = x.strip()
                if x in ("Original:", "Shrunk:"):       # each argument on a line of its own
                    cur = blocks.setdefault(x[:-1], [])
                elif not x or x.startswith(("with exception:", "Last step was invoked")) or _FSCHECK.search(x):
                    if "Shrunk" in blocks or x.startswith("with exception:") or _FSCHECK.search(x):
                        break
                    cur = None
                elif cur is not None:
                    cur.append(x)
            args = blocks.get("Shrunk") or blocks.get("Original")
            if args:
                out.append({"line": k, "library": "FsCheck", "example": _clip(", ".join(args)), "tests": int(m.group(1)),
                            "shrinks": int(m.group(2))})
    return out


def from_message(message: Optional[str]) -> Optional[str]:
    """The counterexample `annotate` put on a result's message: "amount=-1"."""
    first = (message or "").split("\n", 1)[0]
    m = MARK.match(first)
    return m.group(1) if m else None


def _keys(name: str) -> set[str]:
    """What a result's name can be found by in a runner's text: the test's own name, without a parameter."""
    from .diff import result_parts
    p = result_parts(name)
    leaf = p["leaf"].strip()
    out = {leaf, re.sub(r"\[.*\]$", "", leaf)}
    if "." in leaf and " " not in leaf:            # Namespace.Class.Method, as dotnet prints it
        out.add(leaf.rsplit(".", 1)[-1])
    return {k for k in out if len(k) >= 3}


def _put(r: dict, ce: dict) -> None:
    rest = (r.get("message") or "").strip()
    if rest.startswith('"') and rest.count('"') == 1:   # a quoted YAML message that went on past its first line
        rest = rest[1:]
    r["message"] = f"fails for {ce['example']} ({ce['library']})." + (f"\n{rest}" if rest else "")


def annotate(results: list[dict], text: Optional[str] = None) -> list[dict]:
    """Put each failing property test's counterexample at the head of its result's message, in place, and return
    the results. A counterexample is found in the result's own message, or in the run's text: Hypothesis names its
    test; for the others, the nearest line above that names a failing test is taken to be its header."""
    failing = [r for r in results if r.get("status") == "fail" and not from_message(r.get("message"))]
    if not failing:
        return results
    for r in failing:
        found = counterexamples(r.get("message") or "")
        if found:
            _put(r, found[0])
    left = [r for r in failing if not from_message(r.get("message"))]
    if not left or not text:
        return results
    lines = text.splitlines()
    keys = {id(r): _keys(r["name"]) for r in left}
    def nearest(cands: list[dict], at: int) -> Optional[dict]:
        """The failing result named on the closest line above, by its longest name; a tie looks further up."""
        for j in range(at, max(-1, at - 400), -1):
            score = {id(x): max((len(k) for k in keys[id(x)] if k in lines[j]), default=0) for x in cands}
            best = max(score.values(), default=0)
            top = [x for x in cands if best and score[id(x)] == best]
            if len(top) == 1:
                return top[0]
        return None
    for ce in counterexamples(text):
        cands = [r for r in left if ce["func"] in keys[id(r)]] if ce.get("func") else left
        r = cands[0] if ce.get("func") and len(cands) == 1 else nearest(cands, ce["line"] - (0 if ce.get("func") else 1))
        if r is None:
            continue
        _put(r, ce)
        left.remove(r)
        if not left:
            break
    return results
