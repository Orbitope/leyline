"""The spec loop: a change stated in an open spec format, tied to the map before and after it is made.

A change is an OpenSpec change folder (`openspec/changes/<id>/` with proposal.md, tasks.md and spec
deltas). Leyline reads it and writes one page back, `leyline.md`, that answers four questions:

    What code will be written?      tasks, each tied to nodes on the map
    What will it affect?            blast radius, and anything it reaches that no task covers
    How will I know it was done?    scenarios, each tied to a test of the same name
    Was it done as agreed?          after implementation: tasks, scenarios, drift, rules

Conventions a spec author follows, and nothing more:
- Name code in backticks in tasks: `Vehicle.Speed`, `Queue.Push`, `Core/Controls.cs`.
  A name that is not on the map yet is taken as new code; say where it goes: `Owner.NewName` for a member,
  `module.new_func`, `path/to/file.py: new_func` or `new_func` in `file.py` for a top-level function.
  Other words in backticks are noted, not checked; a task that names no code is the person's to check.
- Start a task with what it does: add, remove, rename, change. Anything else is read as a change in behavior.
  The code right after the verb is what the task changes; other code in the sentence is only mentioned.
- Give each scenario a test with the same name, written or made at run time.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from . import change, diff, rules, store
from . import verdicts

# A task's number ends at a dot, a colon, a bracket or a space: `2FA login` is text, not task 2.
# matched against a line with its trailing spaces cut: a lazy text before \s*$ is quadratic in a run of spaces inside it
TASK = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\[([^\]]*)\]\s*(?:(\d+(?:\.\d+)*)(?=[.:)\s])[.:)]?)?\s*(.+)$")
# A scenario's step: `- **WHEN** ...`, also with the colon inside or after the bold (`**WHEN:**`, `**WHEN**:`).
STEP = re.compile(r"^\s*[-*+]\s*\*\*(WHEN|GIVEN|AND|THEN)\s*:?\*\*:?\s*(.*)$")
CODE = re.compile(r"`([^`\n]+)`")
VERBS = (("add", ("add ", "create ", "introduce ", "new ", "implement ")), ("remove", ("remove ", "delete ", "drop ")),
         ("rename", ("rename ",)), ("signature", ("change the signature", "change signature", "add a parameter", "add parameter",
                                                  "remove a parameter", "change the return", "change return")))
BEGIN, END = "<!-- leyline:begin -->", "<!-- leyline:end -->"
REVIEWERS = ("logic", "performance")


def action_of(lower: str) -> str:
    """What a task (lower-cased) does to the code it names. "Add a parameter to `X`" changes X's signature: the longer
    phrase wins over the "add " it starts with."""
    if lower.startswith(dict(VERBS)["signature"]):
        return "signature"
    return next((a for a, starts in VERBS if lower.startswith(starts) or any(s in lower for s in starts if len(s) > 12)),
                "behavior")


def run_label(change_id: str, when: str) -> str:
    """The label a change's own test runs are stored under: before:spec-<id> and after:spec-<id>."""
    return f"{when}:{change_id}"


def record_review(con, change_id: str, reviewer: str) -> None:
    """Note that a review of this kind ran. A review that finds nothing files nothing, so this is its only trace."""
    row = con.execute("SELECT value FROM meta WHERE key = ?", ("review:" + change_id,)).fetchone()
    done = json.loads(row[0]) if row else {}
    done[reviewer] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", ("review:" + change_id, json.dumps(done)))


def reviews(con, change_id: str) -> list[str]:
    """The kinds of review that have run on a change: recorded ones, and any that filed a finding."""
    row = con.execute("SELECT value FROM meta WHERE key = ?", ("review:" + change_id,)).fetchone()
    done = set(json.loads(row[0])) if row else set()
    done |= {r[0] for r in con.execute("SELECT DISTINCT reviewer FROM findings WHERE change_id = ?", (change_id,))}
    return sorted(done)


# -- reading the folder ------------------------------------------------------------------------
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


def unfenced(text: str) -> list[str]:
    """The lines of Markdown outside fenced code (``` or ~~~): an example task or scenario in a fence is not one.
    A fence closes on a line of the same character, at least as long, with nothing after it."""
    out, fence = [], None
    for line in text.splitlines():
        m = FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)
        elif fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip().strip(fence[0]):
                fence = None
        else:
            out.append(line)
    return out


def parse(change_dir: str | Path) -> dict:
    """Read an OpenSpec change folder into its title, tasks, requirements and scenarios."""
    d = Path(change_dir)
    if not d.is_dir():
        return {"error": f"no change folder at {d}"}
    out = {"id": d.name, "dir": str(d), "title": d.name.replace("-", " "), "why": "", "what": "", "tasks": [], "scenarios": [],
           "requirements": [], "problems": []}
    # The repository the folder is in (above openspec/), else the folder: a file linked from outside it is not read.
    top = next((p.parent for p in d.resolve().parents if p.name == "openspec"), d)
    proposal = d / "proposal.md"
    if proposal.is_file() and store.inside(proposal, top):
        text = proposal.read_text(encoding="utf-8-sig", errors="replace")
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            out["title"] = re.sub(r"^(change|proposal)\s*:\s*", "", m.group(1).strip(), flags=re.I)
        for key, head in (("why", "Why"), ("what", "What Changes")):
            m = re.search(rf"^##\s+{head}\s*$(.*?)(?=^##\s|\Z)", text, re.M | re.S)
            if m:
                out[key] = " ".join(m.group(1).split())
    else:
        out["problems"].append("proposal.md is missing")
    tasks = d / "tasks.md"
    keys = Counter()   # numbering that restarts in each section (`1.` under two headings) still gives one key per task
    if tasks.is_file() and store.inside(tasks, top):
        n = 0
        for line in unfenced(tasks.read_text(encoding="utf-8-sig", errors="replace")):
            m = TASK.match(line.rstrip())
            if not m:
                continue
            n += 1
            text = m.group(3)
            lower = text.lower().lstrip("`* ")
            action = action_of(lower)
            key = m.group(2) or str(n)
            keys[key] += 1
            out["tasks"].append({"key": key if keys[key] == 1 else f"{key} ({keys[key]})", "text": text,
                                 "done": m.group(1).strip().lower() == "x", "action": action, "names": CODE.findall(text)})
    else:
        out["problems"].append("tasks.md is missing")
    for spec in sorted((d / "specs").rglob("spec.md")) if (d / "specs").is_dir() else []:
        if not store.inside(spec, top):
            continue
        capability = str(spec.parent.relative_to(d / "specs"))
        section = req = None
        cur = None
        for line in unfenced(spec.read_text(encoding="utf-8-sig", errors="replace")):
            if line.startswith("## "):
                section = (line[3:].split() or [""])[0].upper() or None   # `## ` alone names no section
            elif line.startswith("### Requirement:"):
                req = line.split(":", 1)[1].strip()
                out["requirements"].append({"capability": capability, "name": req, "kind": section or "ADDED"})
            elif line.startswith("#### Scenario:"):
                name = line.split(":", 1)[1].strip()
                key = f"{capability}/{name}"
                keys[key] += 1
                if keys[key] > 1:   # one test of that name would prove both
                    out["problems"].append(f"{spec.relative_to(d)}: two scenarios are named \"{name}\"; a scenario is"
                                           " proven by the test of its name, so give each its own")
                    key += f" ({keys[key]})"
                cur = {"key": key, "name": name,
                       "requirement": req, "capability": capability, "kind": section or "ADDED", "when": [], "then": []}
                out["scenarios"].append(cur)
            elif cur is not None and STEP.match(line):
                word = STEP.match(line)
                (cur["then"] if word.group(1) == "THEN" or (word.group(1) == "AND" and cur["then"]) else cur["when"]).append(word.group(2))
            elif re.match(r"^###\s+Scenario", line):
                out["problems"].append(f"{spec.relative_to(d)}: a scenario heading needs four #, found three: {line.strip()}")
    for r in out["requirements"]:
        if r["kind"] != "REMOVED" and not any(s["requirement"] == r["name"] and s["capability"] == r["capability"] for s in out["scenarios"]):
            out["problems"].append(f"requirement \"{r['name']}\" has no scenario")
    return out


# -- tying names to the map ---------------------------------------------------------------------
FILE_EXT = ("cs", "py", "pyi", "ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs", "gd", "go", "java", "kt", "rs", "rb", "php",
            "c", "cc", "cpp", "h", "hpp", "swift", "scala", "lua", "json", "md", "yaml", "yml", "toml", "txt", "csv", "html",
            "css", "sql", "sh", "xml", "proto")
FILE_NAME = re.compile(r"(?:[\w.@-]+/)*[\w.@-]+\.(?:" + "|".join(FILE_EXT) + r")")
# A path written in a task without backticks: `Add two mutants to tooling/scripts/mutation_probe.py`.
BARE_PATH = re.compile(r"(?<![\w/.`-])((?:[\w.@-]+/)+[\w.@-]+\.(?:" + "|".join(FILE_EXT) + r"))(?![\w/])")
# A file of tests: an edit in it is how a scenario gets proven, not a change to the product.
TEST_FILE = re.compile(r"(^|/)(tests?|spec|specs|__tests__)/|(^|/)test_[^/]*$|_test\.\w+$|\.(test|spec)\.\w+$|Tests?\.cs$")
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
# Extensions that are also common member names (`res.json`, `obj.go`): only a file when one is there.
WORDLIKE_EXT = ("go", "c", "h", "sh", "rs", "css", "html", "sql", "xml", "txt", "json", "lua", "rb", "kt", "csv")
HOMES = "`Owner.name` for a member, `module.name` or `path/to/file.py: name` for a top-level function"


class _Names:
    def __init__(self, con):
        self.con = con
        self.rows = con.execute("SELECT id, kind, name, parent_id, path FROM nodes WHERE layer = 'fact'"
                                " AND kind IN ('type', 'callable', 'field', 'file', 'module', 'test')").fetchall()
        self.by_id = {r["id"]: r for r in self.rows}
        self.by_name = defaultdict(list)
        self.by_stem = defaultdict(list)    # build_cases -> the file build_cases.py, for `build_cases.func`
        for r in self.rows:
            self.by_name[r["name"]].append(r)
            if r["kind"] == "file":
                self.by_stem[re.sub(r"\.[^./]+$", "", r["name"])].append(r)
        self.module = {r["node_id"]: r["module_id"] for r in con.execute("SELECT node_id, module_id FROM ancestry")}
        self.test_files, self.test_modules = store.test_places(con)
        self.roots = list(store.roots(con).values())

    def in_tests(self, i: str) -> bool:
        """Test code: a test, anything in a file that holds tests, or in a module that is mostly such files."""
        n = self.by_id.get(i)
        return bool(n) and (n["kind"] == "test" or n["path"] in self.test_files or self.module.get(i) in self.test_modules)

    def file(self, written: str) -> list:
        """File or module nodes a written path names: the whole path, or its end."""
        name = written.strip().lstrip("./")
        return [r for r in self.rows if r["kind"] in ("file", "module") and (r["path"] == name or (r["path"] or "").endswith("/" + name))]

    def on_disk(self, written: str) -> bool:
        name = written.strip().lstrip("./")
        return any((root / name).exists() for root in self.roots)

    def _module_file(self, dotted: list[str]) -> list:
        """The file a dotted module path names: `build_cases`, `validator.build_cases` or the full package path."""
        hits = self.by_stem.get(dotted[-1], [])
        if len(dotted) > 1:
            tail = "/".join(dotted)
            hits = [r for r in hits if re.sub(r"\.[^./]+$", "", r["path"] or "").endswith(tail)] or hits
        return hits

    def resolve(self, written: str) -> dict:
        """One backticked name -> {"ids": [...]} | {"new": name, "parent": id or None} | {"ambiguous": [...]}
        | {"note": why} (words and files that are not code on the map) | {"skip": True}"""
        w = written.strip()
        # `path/to/file.py: func`, `file.py:func`, `file.py::func`: a function at the top of a file.
        m = re.fullmatch(r"(" + FILE_NAME.pattern + r")\s*::?\s*([A-Za-z_$][\w$.]*)(?:\(.*\))?", w)
        if m:
            files = [f for f in self.file(m.group(1)) if f["kind"] == "file"]
            if len(files) != 1:
                return {"note": f"`{m.group(1)}` is not a file on the map"} if not files else {
                    "ambiguous": sorted(f["id"] for f in files)[:8]}
            return self._in_file(files[0], m.group(2))
        name = re.sub(r"\(.*\)$", "", w)
        if not name or " " in name or name[0].isdigit() or name.startswith(("-", "/")) and "." not in name:
            return {"skip": True}
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        if "/" in name or (FILE_NAME.fullmatch(name) and not w.endswith(")")):
            hits = self.file(name)
            if hits:
                return {"ids": [h["id"] for h in hits]}
            if self.on_disk(name):
                return {"note": f"`{name}` is a file the map does not read, so it is not checked as code", "doc": name}
            if "/" in name or ext not in WORDLIKE_EXT:
                return {"new": name, "parent": None, "file": True}
        parts = name.split(".")
        leaf = parts[-1]
        cands = [r for r in self.by_name.get(leaf, []) if r["kind"] != "file"]
        if len(parts) > 1:
            owner = parts[-2]
            cands = [r for r in cands if r["parent_id"] in self.by_id and self.by_id[r["parent_id"]]["name"] == owner]
            if not cands:   # `module.func`: a function at the top of the file build_cases.py
                files = self._module_file(parts[:-1])
                cands = [r for r in self.by_name.get(leaf, []) if r["kind"] != "file" and r["parent_id"] in {f["id"] for f in files}]
        if not cands:
            if len(parts) > 1:
                owners = [r for r in self.by_name.get(parts[-2], []) if r["kind"] in ("type", "module", "callable")]
                if len({o["id"] for o in owners}) == 1:
                    return {"new": leaf, "parent": owners[0]["id"]}
                files = self._module_file(parts[:-1])
                if not owners and len(files) == 1 and IDENT.fullmatch(leaf):
                    return self._in_file(files[0], leaf)
                if not owners and not IDENT.fullmatch(parts[-2]):
                    return {"skip": True}
            if not IDENT.fullmatch(leaf):
                return {"skip": True}
            return {"new": name, "parent": None}
        product = [r for r in cands if not self.in_tests(r["id"])] or cands
        owners = {(r["parent_id"], r["kind"]) for r in product}
        if len(owners) == 1:
            return {"ids": sorted(r["id"] for r in product)}      # overloads of one method are one target
        types = [r for r in product if r["kind"] == "type"]
        if len(types) == 1 and len(parts) == 1:
            return {"ids": [types[0]["id"]]}                     # a bare name that is a type means the type
        return {"ambiguous": sorted(r["id"] for r in product)[:8]}

    def _in_file(self, f, name: str) -> dict:
        """A name at the top of one file: the node if it is there, else new code whose home is that file."""
        parts = name.split(".")
        cur = f["id"]
        for part in parts:
            nxt = [r for r in self.by_name.get(part, []) if r["parent_id"] == cur and r["kind"] != "file"]
            if not nxt:
                if part is parts[-1] and cur in self.by_id:
                    return {"new": part, "parent": cur, "label": f"{f['name']}: {name}"}
                return {"note": f"`{name}` is not in {f['path']}"}
            cur = nxt[0]["id"]
        return {"ids": [cur]}


# Where a name sits in a task decides what it is. The code right after the verb (and any joined to it by "and" or a
# comma, or named after a later "and add" or "and change") is what the task changes; code after "in", "into" or
# "to" is where it goes; any other code in the sentence is context the task only mentions ("slower than
# `SimConfig.QueueSpeed`", "one `Metrics.QueuedVehicles` per env"), shown in the plan but not counted as changed.
HOME_BEFORE = re.compile(r"\b(?:in|into|inside|to|under|from|of)\s+(?:the\s+)?$", re.I)
JOINED = re.compile(r"\s*(?:,\s*|/\s*)?(?:(?:and|or|&)\s+)?(?:(?:the|its)\s+)?", re.I)
CLAUSE = re.compile(r"(?:\band|[,;]|\bthen)\s+(?:then\s+|also\s+)?(?:add|change|update|remove|delete|rename|create|extend|move"
                    r"|register|introduce|implement|replace|modify)\b", re.I)


def _roles(text: str) -> list[tuple[str, str]]:
    """Each backticked name in a task with its part: lead (what the task changes), home (where it goes) or mention."""
    occ = [(m.start(), m.end(), m.group(1)) for m in CODE.finditer(text)]
    lead = set()
    for start in [0] + [m.end() for m in CLAUSE.finditer(text)]:
        k = next((k for k, (a, _, _) in enumerate(occ) if a >= start), None)
        while k is not None and k not in lead:
            lead.add(k)
            k = k + 1 if k + 1 < len(occ) and JOINED.fullmatch(text[occ[k][1]:occ[k + 1][0]]) else None
    return [(w, "lead" if k in lead else "home" if HOME_BEFORE.search(text[:a]) else "mention")
            for k, (a, _, w) in enumerate(occ)]


def _narrow(names: _Names, could_be: list[str], context: list[str]) -> list[str]:
    """Of the things an ambiguous name could be, those that sit with the code the task mentions (same owner or file)."""
    def file_of(i):
        row = names.con.execute("SELECT file_id FROM ancestry WHERE node_id = ?", (i,)).fetchone()
        return row[0] if row else None
    near = {x for i in context if i in names.by_id for x in (i, names.by_id[i]["parent_id"], file_of(i)) if x}
    return [c for c in could_be if c in names.by_id and (names.by_id[c]["parent_id"] in near or file_of(c) in near)]


def _targets(names: _Names, parsed: dict) -> tuple[list[dict], list[dict]]:
    """Turn tasks into change targets. Returns (targets, per-task links)."""
    targets, links, seen = [], [], set()
    for t in parsed["tasks"]:
        link = {"key": t["key"], "text": t["text"], "action": t["action"], "nodes": [], "new": [], "ambiguous": [], "unknown": [],
                "into": [], "notes": [], "mentions": [], "mention_ids": [],
                # A task covers a scenario's test when it quotes the scenario's name.
                "scenarios": [s["key"] for s in parsed["scenarios"]
                              if _norm(s["name"]) in {_norm(q) for q in re.findall(r'["\u201c]([^"\u201d]+)["\u201d]', t["text"])}]}
        roles = _roles(t["text"])
        mentioned = [(w, names.resolve(w)) for w, role in roles if role == "mention"]
        context = [i for _, r in mentioned for i in r.get("ids", [])]
        for written, r in mentioned:   # context: shown with the task, never counted as changed
            if r.get("skip"):
                continue
            if "note" in r:
                link["notes"].append(r["note"])
            elif "ids" in r:
                link["mention_ids"] += [i for i in r["ids"] if i not in link["mention_ids"]]
                link["mentions"] += [x for x in dict.fromkeys(_label(names, i) for i in r["ids"]) if x not in link["mentions"]]
            elif r.get("parent") in names.by_id:
                link["mentions"].append(f"{names.by_id[r['parent']]['name']}.{r['new']} (not on the map yet)")
            elif "ambiguous" in r:
                link["mentions"].append(f"{written} (could be {len(r['ambiguous'])} things)")
            else:
                link["notes"].append(f"`{written}` is not on the map; read as a word, not code")
        resolved = []
        for w, role in roles:
            if role == "mention":
                continue
            r = names.resolve(w)
            near = _narrow(names, r["ambiguous"], context) if "ambiguous" in r and context else []
            resolved.append((w, {"ids": near} if len(near) == 1 else r, False))   # the one beside what the task mentions
        # A path written without backticks counts when it is a file on the map; otherwise it is only prose.
        unquoted = re.sub(r"`[^`]*`", " ", t["text"])
        resolved += [(w, {"ids": [h["id"] for h in names.file(w)]}, True) for w in dict.fromkeys(BARE_PATH.findall(unquoted))
                     if names.file(w)]
        files = {i for _, r, _ in resolved for i in r.get("ids", []) if names.by_id[i]["kind"] == "file"}
        names_code = any("ids" in r or r.get("parent") or r.get("file") for _, r, _ in resolved)
        for written, r, bare in resolved:
            if r.get("skip"):
                continue
            if "note" in r:
                link["notes"].append(r["note"])
                continue
            if "new" in r and not r.get("parent") and not r.get("file") and len(files) == 1:
                f = names.by_id[next(iter(files))]   # "add `func` in `build_cases.py`": the file is its home
                r = {"new": r["new"], "parent": f["id"], "label": f"{f['name']}: {r['new']}"}
            if "ids" in r and (t["action"] == "add" or bare) and all(names.by_id[i]["kind"] in ("type", "file", "module") for i in r["ids"]):
                link["into"].extend(r["ids"])   # "add X to `Foo`": Foo is where it goes, not something whose behavior changes
            elif "ids" in r:
                link["nodes"].extend(r["ids"])
                for i in r["ids"]:
                    # Adding something to an existing type changes the type's behavior, not its contract.
                    action = "behavior" if t["action"] == "add" else t["action"]
                    if (i, action) not in seen and names.by_id[i]["kind"] != "module":
                        seen.add((i, action))
                        targets.append({"id": i, "action": action, "note": f"task {t['key']}: {t['text'][:120]}"})
            elif "ambiguous" in r:
                link["ambiguous"].append({"written": written, "could_be": r["ambiguous"]})
            elif r.get("parent") or r.get("file") or (t["action"] == "add" and not names_code and not re.fullmatch(r"[A-Z0-9_]+", r["new"])):
                owner = names.by_id[r["parent"]]["name"] + "." if r.get("parent") in names.by_id else ""
                link["new"].append({"name": r["new"], "parent": r.get("parent"), "label": r.get("label") or owner + r["new"],
                                    **({"file": True} if r.get("file") else {})})
                if r.get("parent"):
                    targets.append({"action": "add", "name": r["new"], "parent": r["parent"],
                                    "note": f"task {t['key']}", "used_by": [], "related": list(link["mention_ids"])})
            else:   # a word in backticks that is not on the map: an issue code, a value, a type of effect
                link["notes"].append(f"`{written}` is not on the map; read as a word, not code")
        links.append(link)
    return targets, links


def _label(names: _Names, i: str) -> str:
    r = names.by_id.get(i)
    if r is None:
        return i
    owner = names.by_id.get(r["parent_id"])
    if r["kind"] in ("callable", "field") and owner is not None and owner["kind"] == "type":
        return f"{owner['name']}.{r['name']}"
    return r["path"] if r["kind"] == "file" else r["name"]


def _tests(con) -> dict:
    """Test name (lower-cased, punctuation squeezed) -> node id."""
    out = {}
    for r in con.execute("SELECT n.id, n.name FROM nodes n WHERE n.kind = 'test'"
                         " OR json_extract(n.attrs, '$.is_test') = 1"):
        out[_norm(r["name"])] = r["id"]
    return out


def _norm(s: str) -> str:
    # `test_` only where a name starts: `test_shortest_path` is "shortest path", not "shorpath"
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"(?<![a-z0-9])test_", "", s.lower())).strip()


def _result_keys(name: str) -> tuple[set, set]:
    """The names a recorded result answers to. Strong: the test's own name (with its parameter, for pytest), or
    `suite > test`. Weak, used only when nothing answers strongly: a suite around it, a parametrized test's
    function (all its parameters), or a parameter id."""
    p = diff.result_parts(name)
    strong = {_norm(p["leaf"]), _norm(" > ".join(p["suites"] + [p["leaf"]])), _norm(name)}
    weak = {_norm(x) for x in p["suites"]}
    if p["func"]:
        weak |= {_norm(p["func"]), _norm(p["param"])}
    return strong - {""}, weak - strong - {""}


def _results_index(rows) -> tuple[dict, dict]:
    strong, weak = defaultdict(list), defaultdict(list)
    for r in rows:
        st, wk = _result_keys(r["name"])
        for k in st:
            strong[k].append(r)
        for k in wk:
            weak[k].append(r)
    return strong, weak


def _scenario_results(index: tuple[dict, dict], name: str) -> list:
    """The recorded results that carry a scenario's name. Every one must pass for the scenario to pass."""
    k = _norm(name)
    return index[0].get(k) or index[1].get(k) or []


def _generated(test_names, name: str) -> Optional[dict]:
    """A test on the map that makes a test of this name at run time: one named with a template ("agrees on {}"), or
    a parametrized pytest test whose name the scenario's starts with (`test_check` for "check zero")."""
    hits = [(size, t) for pattern, size, t in test_names.templates if re.compile(pattern.pattern, re.I).match(name)]
    key = _norm(name)
    hits += [(len(_norm(t["name"])), t) for t in test_names.parametrized
             if _norm(t["name"]) and key.startswith(_norm(t["name"]) + " ")]
    return max(hits, key=lambda h: h[0])[1] if hits else None


# -- the brief -------------------------------------------------------------------------------------
def brief(con, change_dir: str | Path, write: bool = True, new_baseline: bool = False) -> dict:
    """Assess a spec before it is implemented, store the result, and write `leyline.md` into the folder.
    Once the code has moved on from the first brief, later briefs keep that first picture of the code to
    compare against, so a spec can be amended part-way through. `new_baseline` starts over from the code as it is."""
    parsed = parse(change_dir)
    if "error" in parsed:
        return parsed
    names = _Names(con)
    targets, links = _targets(names, parsed)
    cid = "spec-" + parsed["id"]
    intent = parsed["what"] or parsed["why"] or parsed["title"]
    self_tests = _self_tests(con, [run_label(cid, "before"), run_label(cid, "after")])
    report = change.propose(con, intent, targets, parsed["title"], source="spec", change_id=cid,
                            keep_baseline=not new_baseline, test_entries={i: x["name"] for i, x in self_tests.items()}) \
        if targets or parsed["tasks"] else {"error": "tasks.md has no tasks (`- [ ] 1.1 ...`)."}
    # Tasks that name no code (docs, say) are for a person to check; the baseline still shows what else changed.
    tests = _tests(con)
    test_names = diff.TestNames(con)
    ran = _results_index(con.execute("SELECT name, status, message FROM test_results WHERE run = ?",
                                     (run_label(cid, "before"),)).fetchall())
    scenarios = []
    for s in parsed["scenarios"]:
        tid = tests.get(_norm(s["name"]))
        gen = None if tid else _generated(test_names, s["name"])
        in_run = bool(_scenario_results(ran, s["name"]))
        scenarios.append({**s, "test": tid, "test_exists": bool(tid or gen or in_run),
                          **({"generated_by": gen["name"]} if gen else {"in_run": True} if in_run and not tid else {})})
    from . import props   # a scenario that states an invariant is proven better by a property test
    props.mark_scenarios(con, scenarios, [i for l in links for i in l["nodes"]])
    tasked = {i for l in links for i in l["nodes"]}
    planned = {k for l in links for k in l["scenarios"]}   # scenarios some task says it will write the test for

    def covered(i):  # a node is covered by a task that names it, its type or its file
        cur = i
        while cur:
            if cur in tasked:
                return True
            cur = names.by_id[cur]["parent_id"] if cur in names.by_id else None
        row = con.execute("SELECT file_id FROM ancestry WHERE node_id = ?", (i,)).fetchone()
        return bool(row and row[0] in tasked)
    uncovered = [m for m in report.get("must_edit", []) if not covered(m["id"])] if "error" not in report else []
    if "error" not in report:
        _remember_folder(con, cid, Path(parsed["dir"]))
    crossings, agree = _crossings(con, names, links) if "error" not in report else ([], [])
    if "error" not in report:
        report["channels"] = crossings
        # The channels themselves say this, end by end (see brief_text).
        report["risks"] = [r for r in report.get("risks") or [] if "far side of a channel" not in r["what"]]
        _say_what_tests_are_known(con, cid, report, self_tests)
    state = _shared_state_touched(con, tasked)
    others = _left_alone(con, names, links, cid)
    patterns = _patterns_touched(con, tasked)
    from . import coupling   # files that usually change with what the tasks touch, from git history
    history = coupling.for_spec(con, names, links)
    rule_state = rules.check(con)
    gaps = list(parsed["problems"])
    for l in links:
        gaps += [f"task {l['key']}: `{a['written']}` could be {len(a['could_be'])} things; write it as `Owner.Name` or"
                 f" `path/to/file: Name`" for a in l["ambiguous"]]
        gaps += [f"task {l['key']}: new `{n['name']}` has no stated home; write it as {HOMES}"
                 for n in l["new"] if not n["parent"] and not n.get("file")]
        # A task that names no code (docs, a mutant list in a file the map does not read) is for the person to check.
        l["by_you"] = not (l["nodes"] or l["new"] or l["into"] or l["scenarios"])
    gaps += [f"must be edited but no task covers it: {m['name']} ({m.get('note', '')})" for m in uncovered]
    if report.get("snapshot_error"):
        gaps.append(f"the baseline of the code as it is now could not be kept ({report['snapshot_error']}), so `check`"
                    " would have nothing to compare the change with: fix that, then plan again before changing the code")
    gaps += [f"scenario \"{s['name']}\" has no test of that name, and no task says it will add one"
             for s in scenarios if not s["test_exists"] and s["key"] not in planned]
    result = {"change_id": cid, "title": parsed["title"], "why": parsed["why"], "what": parsed["what"], "dir": parsed["dir"],
              "tasks": [{**l, "labels": [_label(names, i) for i in l["nodes"]], "into_labels": [_label(names, i) for i in l["into"]]} for l in links],
              "notes": [f"task {l['key']}: {n}" for l in links for n in dict.fromkeys(l["notes"])],
              "scenarios": scenarios, "impact": {k: report.get(k) for k in ("summary", "risks", "by_module", "tests_to_run", "channels", "untested")}
              if "error" not in report else {"error": report["error"]},
              "must_edit_uncovered": uncovered, "must_agree": agree, "self_tests": self_tests,
              "shared_state": state, "left_alone": others, "patterns": patterns, "usually_changes_with": history,
              "rules_failing_now": [r for r in rule_state["rules"] if not r["passes"]],
              "findings": findings(con, cid)["findings"], "gaps": gaps,
              "baseline": report.get("snapshot"), "reviews": reviews(con, cid),
              "baseline_tests": con.execute("SELECT 1 FROM test_results WHERE run = ? LIMIT 1",
                                            (run_label(cid, "before"),)).fetchone() is not None,
              "ready": not gaps and not any(f["status"] == "open" and f["severity"] == "high" for f in findings(con, cid)["findings"])}
    from . import drift   # specs, living or of finished changes, that no longer match code this change touches
    result["drifted_specs"] = drift.touching(con, parsed["dir"], sorted(tasked | {i for l in links for i in l["into"]}))
    from . import diagrams   # the planned path through the changed code, drawn from the map as it is now
    result["how_it_runs"] = diagrams.safe(diagrams.sequence, con, [i for l in links for i in l["nodes"]])
    from . import related   # finished changes, earlier reviews (or, with neither, commits) that touched the same code
    result["related_changes"] = related.find(con, names, sorted(tasked | {i for l in links for i in l["into"]}), exclude=cid)
    with con:
        con.execute("DELETE FROM spec_items WHERE change_id = ?", (cid,))
        con.executemany("INSERT INTO spec_items VALUES (?,?,?,?,?,?,?)",
                        [(cid, "task", l["key"], l["text"], l["action"], json.dumps(l["nodes"]),
                          json.dumps({"new": l["new"], "into": l["into"], "scenarios": l["scenarios"], "by_you": l["by_you"],
                                      "mentions": l["mention_ids"]}))
                         for l in links]
                        + [(cid, "scenario", s["key"], s["name"], s["kind"], json.dumps([s["test"]] if s["test"] else []),
                            json.dumps({"when": s["when"], "then": s["then"], "requirement": s["requirement"]})) for s in scenarios])
    if write:
        _write(Path(parsed["dir"]) / "leyline.md", brief_text(result))
        result["written"] = str(Path(parsed["dir"]) / "leyline.md")
    return result


def _remember_folder(con, cid: str, folder: Path) -> None:
    """Note where a change's folder is, relative to its repository when it is inside one (a store finds its
    repository after a move), so its baseline can go once the folder is archived or removed."""
    from . import store
    where = {"dir": str(folder.resolve())}
    for repo, root in store.roots(con).items():
        try:
            where = {"dir_repo": repo, "dir_rel": folder.resolve().relative_to(root.resolve()).as_posix()}
            break
        except ValueError:
            continue
    row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
    attrs = {k: v for k, v in json.loads(row[0] or "{}").items() if k not in ("dir", "dir_repo", "dir_rel")} if row else {}
    with con:
        con.execute("UPDATE change_proposals SET attrs = ? WHERE id = ?", (json.dumps({**attrs, **where}), cid))


def folder_gone(con, cid: str) -> bool:
    """True when the folder a change was planned from is no longer where it was: archived, or removed."""
    from . import store
    row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
    attrs = json.loads(row[0] or "{}") if row else {}
    if attrs.get("dir_repo"):
        root = store.roots(con).get(attrs["dir_repo"])
        if root is None or not root.is_dir() or (root / attrs["dir_rel"]).is_dir():
            return False
        folder = root / attrs["dir_rel"]
        archived = folder.parent / "archive"
        if archived.is_dir() and any(f.is_dir() and f.name.endswith("-" + folder.name) for f in archived.iterdir()):
            return True
        # A folder committed on a branch that is not checked out is out of the working tree only until it is again.
        return not _on_a_branch(root, attrs["dir_rel"])
    return bool(attrs.get("dir")) and not Path(attrs["dir"]).is_dir()


def _on_a_branch(root: Path, rel: str) -> bool:
    """True when the head of some local branch of the repository at `root` holds the folder `rel` (relative to root)."""
    import subprocess
    try:
        git = lambda *a, **k: subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, check=True,
                                             **k).stdout
        heads = git("for-each-ref", "--format=%(refname)", "refs/heads").split()
        if not heads:
            return False
        path = git("rev-parse", "--show-prefix").strip() + rel   # as the commit names it: from the top of the repository
        out = git("cat-file", "--batch-check", input="".join(f"{h}:{path}\n" for h in heads))
    except (OSError, subprocess.CalledProcessError):   # not a git repository, or no git
        return False
    return any(line.split()[1:2] == ["tree"] for line in out.splitlines())   # "<oid> tree <size>", or "<name> missing"


# -- channels and tests the map does not see as such -------------------------------------------------
CHANNEL_SKIP = ("event",)   # inside one program, where the compiler joins the two sides


def _crossing_line(c: dict) -> str:
    """One channel the change crosses, as the plan says it."""
    at = f" ({c.get('program') or c.get('address')})" if c.get("program") or c.get("address") else ""
    if "hub" not in c:   # a brief stored before channels had ends
        return f"Crosses a {c['channel']} boundary{at}: {c['from_name']} to {c['to_name']}."
    spokes = [x["name"] for x in sorted(c["spokes"], key=lambda x: not x["changed"])]   # the ends that change first
    one = len(spokes) == 1
    if c["channel"] == "di":
        return (f"Crosses a di boundary{at}: calls to {_and(spokes)} reach {c['hub_name']} because a container registers it."
                " The calling code never names the implementation.")
    who, s_ = _and(spokes), "s" if one else ""
    how = (f"{who} start{s_} {c['hub_name']} and talk{s_} to it over its stdio pipe." if c["channel"] == "process" else
           f"{who} read{s_} what {c['hub_name']} writes." if c.get("data") else f"{who} call{s_} {c['hub_name']}.")
    changed = list(dict.fromkeys(c["hub_changed"] + [x for s in c["spokes"] for x in s["changed"]]))
    if c["hub_changed"] and any(x["changed"] for x in c["spokes"]):
        return (f"Crosses a {c['channel']} boundary{at}: {how} Both ends change ({_and(changed)}); nothing checks one against"
                " the other, so a mismatch fails only at run time.")
    return (f"Crosses a {c['channel']} boundary{at}: {how} One end changes ({_and(changed)}); the other has no"
            " compile-time link to it.")


def _crossings(con, names: _Names, links: list[dict]) -> tuple[list[dict], list[dict]]:
    """Channels the change crosses, and their other ends that no task names. A channel is crossed when the code
    a task changes sits at one of its ends. For a process, the launched end is the type (or script) around the
    program's entry, which reads its input and writes its output, and the launcher's end is the type that holds the
    process, whose methods talk over the pipe. For a request or a message it is the two functions linked. Every
    other launcher or caller of the same end must agree with what changed there."""
    changed = [(i, _label(names, i)) for l in links for i in l["nodes"]]
    changed += [(n["parent"], n.get("label") or n["name"]) for l in links for n in l["new"] if n.get("parent") in names.by_id]
    named = {i for l in links for i in l["nodes"] + l["into"] + l.get("mention_ids", [])} | {p for p, _ in changed}

    def within(i, end):
        while i:
            if i == end:
                return True
            i = names.by_id[i]["parent_id"] if i in names.by_id else None
        return False

    def at(end):
        return list(dict.fromkeys(lab for i, lab in changed if within(i, end)))

    def unit(i, keep_function):   # the type around a node; with none, the function itself or its file
        cur = i
        while cur in names.by_id and names.by_id[cur]["kind"] not in ("file", "module"):
            if names.by_id[cur]["kind"] == "type":
                return cur
            cur = names.by_id[cur]["parent_id"]
        return i if keep_function or cur not in names.by_id else cur
    groups: dict = defaultdict(list)
    for r in con.execute("SELECT src_id, dst_id, precision, attrs FROM edges WHERE kind = 'communicates' ORDER BY src_id, dst_id"):
        a = json.loads(r["attrs"] or "{}")
        ch = a.get("channel", "channel")
        if ch in CHANNEL_SKIP or r["src_id"] not in names.by_id or r["dst_id"] not in names.by_id:
            continue
        if ch == "process" and not (a.get("pipes") or a.get("direction") == "both"):
            continue    # started and left to run: it reads nothing the program sends
        data = ch in change.DATA_CHANNELS
        # The hub is the end others connect to: the launched program, the handler, or the writer of the data.
        hub, spoke = (r["src_id"], r["dst_id"]) if data else (r["dst_id"], r["src_id"])
        hub_end = unit(hub, False) if ch == "process" else hub
        spoke_end = unit(spoke, True) if ch == "process" else spoke
        # One registrar of many routes (or a writer of many tables) is many channels: one per address.
        groups[(ch, hub_end, "" if ch == "process" else a.get("address") or "")].append({"hub": hub, "spoke": spoke, "end": spoke_end, "address": a.get("address") or "",
                                      "guessed": r["precision"] == "guess"})
    crossings, agree, seen = [], [], set()

    def label(i):   # `Owner.name`, or `file.name` at the top of a file
        return change._label(names.by_id, i)
    for (ch, hub_end, _), members in groups.items():
        there = at(hub_end)
        moved = [m for m in members if at(m["end"])]
        if not there and not moved:
            continue
        hub = members[0]["hub"]
        script = names.by_id[hub]["name"] in ("<module>", "<top-level>")   # a script is named by its file
        module = names.by_id.get(names.module.get(hub))
        program = (names.by_id[hub]["path"] if script else module["name"] if module else "") if ch == "process" else ""
        ends = list({m["end"]: m for m in members}.values())
        crossings.append({"channel": ch, "address": members[0]["address"], "data": ch in change.DATA_CHANNELS,
                          "hub": hub, "hub_name": names.by_id[hub]["path"] if script else label(hub), "program": program,
                          "hub_changed": there, "spokes": [{"id": m["spoke"], "name": label(m["end"]), "changed": at(m["end"])}
                                                           for m in ends],
                          "guessed": all(m["guessed"] for m in moved or members),
                          # kept for older readers of the brief: one end and the other
                          "from": (moved or members)[0]["spoke"], "to": hub,
                          "from_name": label((moved or members)[0]["end"]), "to_name": label(hub)})
        if ch == "di":   # the container picks the implementation; its callers name only the interface
            continue
        what = program or label(hub)
        if there:   # what the hub sends or answers changed: everyone who talks to it must agree
            mine = {lab.rsplit(".", 1)[-1] for m in moved for lab in at(m["end"])}
            for m in members:
                if m in moved or m["end"] in seen or any(within(n, m["end"]) or within(m["end"], n) for n in named):
                    continue
                seen.add(m["end"])
                twins = [r["id"] for r in names.rows if r["kind"] == "callable" and r["name"] in mine and within(r["id"], m["end"])]
                agree.append({"id": m["end"], "name": label(m["end"]), "path": (names.by_id.get(m["end"]) or {})["path"],
                              "reads": [label(t) for t in twins], "channel": ch,
                              "why": (f"also starts {what} and reads what it sends" if ch == "process" else
                                      f"also reads what {what} writes" if ch in change.DATA_CHANNELS else
                                      f"also calls {what} over {ch}")})
        elif hub_end not in seen and not any(within(n, hub_end) or within(hub_end, n) for n in named):
            seen.add(hub_end)   # one side changed what it sends or expects: the hub must agree
            agree.append({"id": hub_end, "name": label(hub_end), "path": (names.by_id.get(hub_end) or {})["path"],
                          "reads": [], "channel": ch,
                          "why": (f"the program {what}, at the other end of the pipe" if ch == "process" else
                                  f"writes what {_some([x['name'] for x in crossings[-1]['spokes'] if x['changed']])} reads"
                                  if ch in change.DATA_CHANNELS else
                                  f"answers {_some([x['name'] for x in crossings[-1]['spokes'] if x['changed']])}")})
    return crossings, agree


def _self_tests(con, runs: list[str]) -> dict:
    """Entry points that are tests of their own: a script whose checks print one PASS or FAIL line each, which the
    map has no test node for (`check(ok, "queued reported per env")` in a file run as a script). Found by the
    recorded results whose names are written in the entry's file. entry id -> {name, path, results}."""
    names = set()
    for run in runs:
        for r in con.execute("SELECT name FROM test_results WHERE run = ? AND test_id IS NULL", (run,)):
            leaf = diff.result_parts(r["name"])["leaf"].strip()
            if len(leaf) >= 8 and " " in leaf:   # a phrase, not a word that could be written anywhere
                names.add(leaf)
    if not names:
        return {}
    by_file = defaultdict(list)
    for r in con.execute("SELECT f.entry_id, n.repo_id, n.path FROM flows f JOIN nodes n ON n.id = f.entry_id"
                         " WHERE COALESCE(json_extract(f.attrs, '$.kind'), '') != 'test' AND n.path IS NOT NULL"):
        by_file[(r["repo_id"], r["path"])].append(r["entry_id"])
    root_of = diff.roots(con)
    out = {}
    for (repo, path), ids in sorted(by_file.items()):
        data = diff.source(con, repo, path, root_of)
        text = data.decode("utf-8", errors="replace") if data else ""
        found = sorted(n for n in names if n in text and re.search(r"[\"']" + re.escape(n) + r"[\"']", text))
        if len(found) >= 2:
            for i in ids:
                out[i] = {"name": f"{path}, run as a script (its own checks)", "path": path, "results": found}
    return out


def _say_what_tests_are_known(con, cid: str, report: dict, self_tests: dict) -> None:
    """A function on "no test's path" is on none the map knows. When the recorded run has passing results the map
    cannot place, one of them may run the change after all: say what is known instead."""
    placed = {n for x in self_tests.values() for n in x["results"]}
    loose = {r[0] for run in (run_label(cid, "before"), run_label(cid, "after")) for r in con.execute(
        "SELECT name FROM test_results WHERE run = ? AND test_id IS NULL AND status = 'pass'", (run,))
        if diff.result_parts(r[0])["leaf"].strip() not in placed}
    if not loose:
        return
    for r in report.get("risks") or []:
        if r["what"].endswith("changed functions are on no test's path."):
            r["level"] = "medium"
            r["what"] = (r["what"][:-1] + " on the map. The recorded test run has " + _n(len(loose), "passing result")
                         + " the map cannot place, so one of them may run it.")


def _shared_state_touched(con, tasked: set) -> list[dict]:
    """Fields the named code assigns that other types also assign."""
    from . import query
    shared = {f["id"]: f for f in query.shared_state(con, limit=100000, guesses=False)["fields"]}
    out = {}
    for i in tasked:
        for r in con.execute(f"SELECT dst_id FROM edges WHERE kind = 'writes' AND (src_id = ? OR src_id LIKE ?) AND {SURE}",
                             (i, i + ".%")):
            if r[0] in shared and len(shared[r[0]]["written_from"]) >= 2:
                out[r[0]] = {"name": shared[r[0]]["name"], "also_written_from": shared[r[0]]["written_from"][:5]}
        if i in shared:
            out[i] = {"name": shared[i]["name"], "also_written_from": shared[i]["written_from"][:5]}
    return sorted(out.values(), key=lambda x: x["name"])


def _words(name: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+", name)]


def _task_functions(names: _Names, links: list[dict]) -> list[str]:
    """Existing functions the tasks change. A named type stands for its functions."""
    fns = []
    for l in links:
        for i in l["nodes"]:
            kind = names.by_id[i]["kind"]
            if kind in ("callable", "test"):
                fns.append(i)
            elif kind == "type":
                fns += [r["id"] for r in names.rows if r["parent_id"] == i and r["kind"] == "callable"]
    return list(dict.fromkeys(fns))


SURE = "COALESCE(precision, '') != 'guess'"   # an edge found by more than a name that happens to be unique


def _in_baseline(con, cid: str, i: str) -> bool:
    """Whether the change's baseline (the code as it was when first planned) holds this node. True with no baseline."""
    snap = diff.snapshot_path(con, cid)
    if not cid or not snap.exists():
        return True
    before = diff._open(snap)
    try:
        return before.execute("SELECT 1 FROM nodes WHERE id = ?", (i,)).fetchone() is not None
    except sqlite3.Error:
        return True
    finally:
        before.close()


def _left_alone(con, names: _Names, links: list[dict], cid: str = "") -> dict:
    """Code that shares something with the change and that no task names: other callers of a changed function,
    and other users of a field a changed function uses. A parallel edit is most often forgotten here."""
    fns = _task_functions(names, links)
    inside = set(fns)

    def product(i):
        return i in names.by_id and names.by_id[i]["kind"] != "test" and not names.in_tests(i)
    def owner(i):
        return names.by_id[i]["parent_id"] if i in names.by_id else None

    def is_ctor(i):
        return names.by_id[i]["name"] in (".ctor", "__init__", "constructor")

    counted: dict = {}

    def fields_of(t):
        if t not in counted:
            counted[t] = con.execute("SELECT COUNT(*) FROM nodes WHERE parent_id = ? AND kind = 'field'", (t,)).fetchone()[0]
        return counted[t]
    reads: dict = {}

    def walker(fn, t):
        """A function that uses most of a type's fields (at least five): it walks them all, not one in particular."""
        if (fn, t) not in reads:
            reads[(fn, t)] = con.execute(
                "SELECT COUNT(DISTINCT e.dst_id) FROM edges e JOIN nodes n ON n.id = e.dst_id WHERE e.kind IN ('reads', 'writes')"
                " AND e.src_id = ? AND n.parent_id = ?", (fn, t)).fetchone()[0]
        n = fields_of(t) if t else 0
        return n >= 5 and reads[(fn, t)] * 2 >= n
    callers, state, seen = [], [], set()
    for i in fns:
        # A function that registers a route's handler gets nothing back from it: its requesters are the other ends.
        who = sorted({r[0] for r in con.execute("SELECT DISTINCT src_id FROM calls WHERE dst_id = ? AND dispatch != 'registers'", (i,))
                      if r[0] not in inside and product(r[0])})
        if who:
            callers.append({"id": i, "changed": _label(names, i), "callers": [_label(names, w) for w in who], "caller_ids": who,
                            "far": len({names.module.get(w) for w in who} - {names.module.get(i)}),
                            "other_types": len({owner(w) for w in who} - {owner(i)})})
        about = _task_words(names, links, i)
        # A field link guessed by name alone (`.Size` read on some other type) is not evidence: only sure links count.
        for f in con.execute(f"SELECT dst_id, MAX(kind = 'writes') FROM edges WHERE kind IN ('reads', 'writes') AND src_id = ?"
                             f" AND {SURE} GROUP BY dst_id ORDER BY 2 DESC", (i,)):
            if f[0] in seen or f[0] not in names.by_id:
                continue
            # A constructor setting a field up is not a second user of it.
            users = sorted({r[0] for r in con.execute(
                f"SELECT DISTINCT src_id FROM edges WHERE kind IN ('reads', 'writes') AND dst_id = ? AND {SURE}", (f[0],))
                if r[0] not in inside and product(r[0]) and not is_ctor(r[0])})
            if not 0 < len(users) <= 8:   # a field half the program uses says nothing about this change
                continue
            # A reader that walks every field of its type (a serializer, a copy, a dump) reads this one too
            # whatever it holds, so a field only such readers share is not shared in any way that matters.
            plain = [u for u in users if not walker(u, owner(f[0]))]
            if not plain:
                continue
            seen.add(f[0])
            close = bool(about & _field_words(names.by_id[f[0]]["name"]))
            quiet = "" if close else (
                f"{_label(names, i)} reads most fields of its type, so using this one says little" if walker(i, owner(f[0]))
                else f"{names.by_id[owner(f[0])]['name']} has {fields_of(owner(f[0]))} fields and this one's name shares"
                     " no word with the tasks" if fields_of(owner(f[0])) > BIG_TYPE else "")
            state.append({"field": _label(names, f[0]), "used_by_changed": _label(names, i),
                          "also_used_by_unchanged": [_label(names, u) for u in users], "user_ids": users,
                          # State the changed type owns is where a parallel edit gets forgotten; a field of
                          # some other type that the change only reads rarely is. The page lists only these
                          # (`own`), and of them only the ones the change is close to: on a large type, sharing
                          # a field the tasks never mention is common and mostly unrelated.
                          "own": owner(f[0]) == owner(i) and not quiet, "close": close,
                          **({"quiet": quiet} if quiet and owner(f[0]) == owner(i) else {}),
                          "changed_writes_it": bool(f[1]),
                          "other_types": len({owner(u) for u in users} - {owner(f[0])})})
    # A new member named like one its type already has (EmergencyQueues beside EntryQueues) is usually a second
    # one of the same thing, and whoever uses the first is a candidate to need the second.
    beside = []
    fresh = [(n["name"].split(".")[-1], n.get("parent")) for l in links for n in l["new"] if n.get("parent")]
    # Code an add task names that is on the map already is new only when the baseline does not hold it (a plan made
    # again after the code was written); otherwise the task changes it, and it is not new.
    fresh += [(names.by_id[i]["name"], owner(i)) for l in links if l["action"] == "add" for i in l["nodes"]
              if names.by_id[i]["kind"] in ("field", "callable") and not _in_baseline(con, cid, i)]
    fresh_names = {(n, p) for n, p in fresh}
    for name, parent in dict.fromkeys(fresh):
        last = _words(name)[-1:]
        if not last or len(_words(name)) < 2:
            continue
        for r in names.rows:
            if r["parent_id"] != parent or r["kind"] != "field" or (r["name"], parent) in fresh_names or _words(r["name"])[-1:] != last:
                continue
            users = sorted({u[0] for u in con.execute(
                f"SELECT DISTINCT src_id FROM edges WHERE kind IN ('reads', 'writes') AND dst_id = ? AND {SURE}", (r["id"],))
                if u[0] not in inside and product(u[0]) and not is_ctor(u[0])})
            if users:
                beside.append({"new": _label(names, parent) + "." + name, "existing": _label(names, r["id"]),
                               "existing_used_by_unchanged": [_label(names, u) for u in users], "user_ids": users})
    callers.sort(key=lambda c: (-c["far"], -c["other_types"], len(c["callers"])))
    # Closest first: a field the tasks' words name, then state of the changed type, then the rest. The page shows
    # the first few and counts the others; `spec facts` lists them all.
    state.sort(key=lambda x: (not x["close"], not x["own"], not x["changed_writes_it"], -x["other_types"],
                              len(x["also_used_by_unchanged"])))
    quiet = [x for x in state if x.get("quiet")]
    return {"beside": beside, "callers": callers, "state": state,
            # What the page leaves off as weak, for it to count: "and 3 more ...".
            "left_out": {"count": len(quiet), "fields": [x["field"] for x in quiet],
                         "why": "they share only a large type, or a reader of every field, with the change"}}


BIG_TYPE = 12   # fields; on a type this large, two methods sharing one is weak evidence that they change together
_PLAIN = set("the a an and or not for with where when then each same its it is are be to of in on at by as from into "
             "that this than any all one two three new add change make use set get call run test case cases code "
             "remove rename write read return value values number true false none null so".split())


def _field_words(name: str) -> set:
    return {w.rstrip("s") for w in _words(name) if len(w) > 2} - _PLAIN


def _task_words(names: _Names, links: list[dict], fn: str) -> set:
    """What the tasks that change a function are about, as words: the text of each task naming it (all tasks when
    none names it directly), less the words of the function's own name and its type's, which every such task has."""
    texts = [l["text"] for l in links if fn in l["nodes"]] or [l["text"] for l in links]
    own = set(_words(names.by_id[fn]["name"]))
    parent = names.by_id.get(names.by_id[fn]["parent_id"])
    if parent is not None:
        own |= set(_words(parent["name"]))
    words = set()
    for t in texts:
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9_]*", t):
            words |= _field_words(tok)
    return words - {w.rstrip("s") for w in own}


def _patterns_touched(con, tasked: set) -> list[dict]:
    from . import patterns
    out = []
    owner = {r["id"]: r["parent_id"] for r in con.execute("SELECT id, parent_id FROM nodes WHERE kind IN ('callable', 'field')")}
    near = set(tasked) | {owner[i] for i in tasked if i in owner}   # a method stands for its type here
    for p in patterns.listing(con, limit=2000)["patterns"]:
        ids = {n["id"] for ns in p["roles"].values() for n in ns}
        if p["pattern"] in ("factory", "process boundary") and not (ids & set(tasked)):
            continue
        if any(i in ids for i in near):
            out.append({"pattern": p["pattern"], "rationale": p["rationale"]})
    return out[:12]


def _write(path: Path, body: str) -> None:
    """Replace the generated block of a file, keeping anything a person wrote around it. A block whose end marker
    is missing (a write cut off, a merge gone wrong) runs to the end of the file. Written whole or not at all."""
    block = f"{BEGIN}\n{body.rstrip()}\n{END}\n"
    old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    start = old.find(BEGIN)
    end = old.find(END, start) if start >= 0 else -1
    if start < 0:
        store.write_file(path, block)
    elif end < 0:
        store.write_file(path, old[:start] + block)
    else:
        store.write_file(path, old[:start] + block + old[end + len(END):].lstrip("\n"))


def _some(xs: list[str], n: int = 4) -> str:
    return ", ".join(xs[:n]) + (f" and {len(xs) - n} more" if len(xs) > n else "")


def _and(xs: list[str], n: int = 4) -> str:
    """A list as a sentence says it: A, B and C."""
    return _some(xs, n) if len(xs) > n else ", ".join(xs[:-1]) + " and " + xs[-1] if len(xs) > 1 else "".join(xs)


def _first_sentence(text: str, limit: int = 220) -> str:
    m = re.match(r"(.+?[.!?])(\s|$)", text.strip(), re.S)
    out = (m.group(1) if m else text.strip()).replace("\n", " ")
    return out if len(out) <= limit else out[:limit - 3].rstrip() + "..."


def _clip(text: str, limit: int) -> str:
    """Text cut at a word boundary to fit, with an ellipsis when cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] if " " in text[:limit] else text[:limit]
    if cut.count("`") % 2:   # not inside a code name: it would leave a backtick open
        cut = cut[:cut.rindex("`")]
    return cut.rstrip(" ,;:") + "…"


def _n(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def brief_status(b: dict) -> dict:
    """Where a plan stands: what blocks implementation, what is left to decide, and what is worth knowing."""
    blocking = ([b["impact"]["error"]] if b["impact"].get("error") else []) + list(b["gaps"])
    opened = [f for f in b["findings"] if f["status"] == "open"]
    blocking += [f"decide the open high finding {f['id']}: {_first_sentence(f['claim'], 120)}" for f in opened if f["severity"] == "high"]
    decide = [f"decide the open {f['severity']} finding {f['id']}: {_first_sentence(f['claim'], 120)}"
              for f in opened if f["severity"] != "high"]
    done = b.get("reviews") or sorted({f["reviewer"] for f in b["findings"]})
    notes = []
    if not done:
        notes.append("No review has run. A logic and a performance review find what the plan misses before code exists.")
    elif missing := [r for r in REVIEWERS if r not in done]:
        notes.append(f"No {' or '.join(missing)} review has run.")
    mine = [t["key"] for t in b["tasks"] if t.get("by_you")]
    if mine and len(mine) == len(b["tasks"]):
        notes.append("No task names code that is on the map, so only you can check the tasks; `check` still says what"
                     " else changed. Put code names in backticks in tasks.md if the change is to code.")
    elif mine:
        notes.append(f"{'Task' if len(mine) == 1 else 'Tasks'} {', '.join(mine)} {'names' if len(mine) == 1 else 'name'} no code,"
                     " so you check {} yourself after the change; {} not hold up the verdict.".format(
                         "it" if len(mine) == 1 else "them", "it does" if len(mine) == 1 else "they do"))
    if b.get("baseline_tests") is False and b.get("baseline") != "kept":
        notes.append("The tests have not been recorded as they pass now, so `leyline check` will not be able to tell a test "
                     "the change breaks from one that already failed.")
    return {"ready": not blocking, "blocking": blocking, "decide": decide, "notes": notes, "reviewed": bool(done)}


def _state_line(b: dict) -> str:
    st = brief_status(b)
    if st["blocking"]:
        return (f"**State: not ready to implement.** {_n(len(st['blocking']), 'thing')} to settle first, listed under "
                f"\"Before implementation\" at the end.")
    return "**State: ready to implement.**" + ("" if st["reviewed"] else " It has not been reviewed.")


def _plain_summary(b: dict) -> str:
    """One paragraph a newcomer can read without knowing Leyline."""
    S = [_first_sentence(b["why"], 300)] if b["why"] else []
    existing = sorted({x for t in b["tasks"] for x in t["labels"]})
    new = list(dict.fromkeys(n.get("label") or n["name"] for t in b["tasks"] for n in t["new"]))
    # a new function at the top of a file is labelled `file: name`; a sentence says it as `name` in file
    new = [f"`{x.split(': ', 1)[1]}` in {x.split(': ', 1)[0]}" if ": " in x else x for x in new]
    did = ([f"changes {_some(existing)}"] if existing else []) + ([f"adds {_some(new)}"] if new else [])
    S.append(f"The plan has {_n(len(b['tasks']), 'task')}" + (f": it {' and '.join(did)}." if did else "."))
    imp = b["impact"]
    if not imp.get("error"):
        s = imp["summary"]
        must = f"{_n(s['must_edit'], 'other place')} must be edited along with it" if s["must_edit"] else "Nothing else must be edited with it"
        # the modules the places that run into it are in, not every module the change touches
        mods = sum(1 for g in imp.get("by_module") or [] if g.get("reached")) or s["modules"]
        reach = (f"{_n(s['reached'], 'place')} in {_n(mods, 'module')} {'runs' if s['reached'] == 1 else 'run'} into the "
                 "changed code and may behave differently") if s["reached"] else "no other code runs into it"
        tests = (f"{_n(s['tests_to_run'], 'existing test')} already {'runs' if s['tests_to_run'] == 1 else 'run'} through it"
                 if s["tests_to_run"] else "no existing test runs through it")
        S.append(f"{must}; {reach}; {tests}.")
    sc = b["scenarios"]
    if sc:
        made = sum(1 for x in sc if x.get("generated_by") or x.get("in_run"))
        have = sum(1 for x in sc if x["test_exists"]) - made
        need = len(sc) - have - made
        S.append(f"It is done when {_n(len(sc), 'scenario passes', 'scenarios pass')}: " + ", ".join(
            x for x in ((f"{have} already {'has' if have == 1 else 'have'} a test" if have else ""),
                        (f"{made} {'gets its test' if made == 1 else 'get their tests'} made at run time" if made else ""),
                        (f"{need} {'needs' if need == 1 else 'need'} a test written" if need else "")) if x) + ".")
    else:
        S.append("No scenario says yet what done means.")
    return " ".join(S)


def _past_decision(l: dict) -> str:
    """How an open finding that repeats a learning says so: with a warning when the code the learning was about has
    changed since, which the person weighs; the finding stays either way."""
    if l.get("stale"):
        from .learnings import changed_text
        return (f"Matches a past decision, but the code it was about has changed since:"
                f" {changed_text(l.get('edited') or [], l.get('gone') or [])}. Decided then: {l['reason']}"
                " Does it still hold?")
    if l.get("code") == "unknown":
        return (f"Matches a past decision: {l['reason']} Whether its code changed since is not known: it was kept"
                " before Leyline recorded that.")
    return f"Matches a past decision: {l['reason']}"


def review_lines(found: list[dict], kinds: list[str], full: bool = True) -> list[str]:
    """Each kind of review: whether it ran, and what it filed. Open findings in full; settled ones as the claim,
    then the decision."""
    L = []
    order = {"high": 0, "medium": 1, "low": 2}
    ran = list(dict.fromkeys([*REVIEWERS, *kinds, *(f["reviewer"] for f in found)]))
    for kind in ran:
        mine = [f for f in found if f["reviewer"] == kind]
        if kind not in kinds and not mine:
            L.append(f"- {kind.capitalize()} review: not run.")
        elif not mine:
            L.append(f"- {kind.capitalize()} review: ran, and filed nothing.")
        else:
            left = sum(f["status"] == "open" for f in mine)
            L.append(f"- {kind.capitalize()} review: {_n(len(mine), 'finding')}, "
                     + (f"{left} still open." if left else "all settled."))
    if not full:
        return L
    opened = sorted((f for f in found if f["status"] == "open"), key=lambda f: order.get(f["severity"], 3))
    if opened:
        L += ["", "Open:"] + [f"- **{f['severity']}** ({f['reviewer']}, {f['id']}): {f['claim']}"
                              + (f" Proposed: {f['proposal']}" if f["proposal"] else "")
                              + (" (Its evidence is not near the change: question it first.)" if f.get("evidence_far_from_change") else "")
                              + (f" ({_past_decision(f['learned'])})" if f.get("learned") else "")
                              + (" (Its code changed since it was filed: re-check it.)" if f.get("code_changed_since") else "")
                              for f in opened]
    closed = sorted((f for f in found if f["status"] != "open"), key=lambda f: order.get(f["severity"], 3))
    if closed:   # a settled finding is one line: the full text stays in `leyline spec findings`
        L += ["", "Settled (full text: `leyline spec findings`):"]
        def said(text):
            text = _first_sentence(text, 160)
            return text if text.endswith((".", "!", "?")) else text + "."
        L += [f"- {f['severity']}: {said(f['claim'])} **{f['status'].capitalize()}**"
              + (f": {said(f['resolution'])}" if f["resolution"] else ".")
              + (f" (Matched a past decision: {said(f['learned']['reason'])})" if f.get("learned") else "") for f in closed]
    return L


def brief_text(b: dict) -> str:
    d = Path(b["dir"])
    rel = "/".join(d.parts[-3:]) if d.parent.name == "changes" else d.name
    L = [f"# {b['title']}", "",
         f"The one-page plan for the change in `{rel}/`, written by Leyline from the spec files beside it. "
         "`leyline plan` rewrites it; edit the spec, not this page.", "",
         _state_line(b), "", _plain_summary(b), "",
         "## 1. What code will be written", ""]
    if b["tasks"]:
        L += ["| Task | Does | Code |", "| --- | --- | --- |"]
        for t in b["tasks"]:
            code = ", ".join(sorted(set(t["labels"]))[:6]) + (f" and {len(set(t['labels'])) - 6} more" if len(set(t["labels"])) > 6 else "")
            new = ", ".join(f"{n.get('label') or n['name']} (new)" for n in t["new"])
            into = ", ".join(f"in {x}" for x in t.get("into_labels", []))
            test = "a test for the scenario" if t.get("scenarios") else ""
            does = ", ".join(x for x in (code, new, test, into) if x) or "no code: checked by you"
            if t.get("mentions"):   # context the task reads or compares with: not changed by it
                does += "; mentions " + _some(t["mentions"], 3)
            L.append(f"| {t['key']} | {t['text'].replace('|', '/')} | {does} |")
        if b.get("notes"):
            L += ["", "Not read as code (nothing to check on the map): " + "; ".join(b["notes"][:8])
                  + (f"; and {len(b['notes']) - 8} more" if len(b["notes"]) > 8 else "") + "."]
    else:
        L.append("No tasks yet.")
    from . import diagrams
    drawn = diagrams.markdown(b.get("how_it_runs"), "How execution reaches the code the tasks change, and what that code calls,"
                              " as the code is now:", "The code the tasks change is shaded.")
    if drawn:
        L += ["", "### How it runs", ""] + drawn
    imp = b["impact"]
    L += ["", "## 2. What it will affect", ""]
    if imp.get("error"):
        L.append(imp["error"])
    else:
        s = imp["summary"]
        n = s["changed"] + s["added"]
        L.append(f"{_n(n, 'thing changes', 'things change')}, {s['must_edit']} more must be edited with {'it' if n == 1 else 'them'},"
                 f" and {s['reached']} {'is' if s['reached'] == 1 else 'are'} reached without needing an edit, across"
                 f" {_n(s['modules'], 'module')}. {_n(s['tests_to_run'], 'existing test runs', 'existing tests run')}"
                 " through the change.")
        L += ["", "*Must be edited*: code that breaks unless it changes too, such as the callers of a function whose parameters "
                  "change. *Reached*: code that runs into the change, directly or through other calls; it needs no edit but may "
                  "behave differently.", ""]
        for m in (imp.get("by_module") or [])[:8]:
            bits = [f"{m[k]} {label}" for k, label in (("changed", "changed"), ("must_edit", "to edit"), ("reached", "reached")) if m.get(k)]
            L.append(f"- {m['module']}: {', '.join(bits)}")
        for r in imp.get("risks") or []:
            L.append(f"- **{r['level']} risk:** {r['what']}")
        for c in (imp.get("channels") or [])[:6]:
            # A channel is the risk, said once: high when a mismatch can only show at run time.
            loud = c.get("hub") and c["channel"] != "di" and (c["hub_changed"] and any(x["changed"] for x in c["spokes"])
                                                               or any(x["channel"] == c["channel"] for x in b.get("must_agree") or []))
            L.append("- " + ("**high risk:** " if loud else "") + _crossing_line(c))
    if b["must_edit_uncovered"]:
        L += ["", "**Must be edited, and no task covers it:**"] + [f"- {m['name']}: {m.get('note', '')}" for m in b["must_edit_uncovered"][:20]]
    if b.get("must_agree"):
        L += ["", "**Must agree with the change, and no task names it** (another end of a channel it crosses):"] + [
            f"- {_and(x['reads']) if x.get('reads') else x['name']}" + (f" ({x['path']})" if x.get("path") else "")
            + f": {x['name'] + ' ' if x.get('reads') else ''}{x['why']}" for x in b["must_agree"][:10]]
    la = {"beside": [], "callers": [], "state": [], **(b.get("left_alone") or {})}
    # One page: new members that double an existing one, callers outside the changed function's own type,
    # and state the changed type owns.
    cal = [c for c in la["callers"] if c.get("other_types", 1)]
    own = [x for x in la["state"] if x.get("own", True) and not x.get("quiet")]
    rows = [f"- {x['new']} (new) sits beside {x['existing']}, which is used by {_some(x['existing_used_by_unchanged'], 6)}" for x in la["beside"][:3]]
    rows += [f"- {c['changed']} is also called by {_some(c['callers'])}" for c in cal[:max(2, 5 - len(rows))]]
    rows += [f"- {x['field']} (used by {x['used_by_changed']}) is also used by {_some(x['also_used_by_unchanged'])}" for x in own[:max(2, 8 - len(rows))]]
    quiet = (la.get("left_out") or {}).get("count", 0)
    more = len(la["beside"]) + len(la["callers"]) + len(la["state"]) - quiet - len(rows)
    if rows:
        L += ["", "**Shares a caller or a field with the change, and no task names it.** Each line is either right to leave "
                  "alone or a missing task:"] + rows
        if more > 0:
            L.append(f"- and {more} more: `leyline spec facts`")
    if quiet:   # said, so a short list is not read as the whole list
        L += ["", f"{quiet} more field{'s' if quiet != 1 else ''} shared with the change left off this page: "
                  f"{(la.get('left_out') or {}).get('why', 'weak links')}. `leyline spec facts` lists them."]
    hist = b.get("usually_changes_with") or {}
    if hist.get("files"):   # from git history: docs, schemas, config and fixtures the map has no link to
        from .coupling import line as coupling_line
        L += ["", f"**Usually changes with the files the tasks touch, and no task names it** ({hist['about']}). Each line "
                  "is either right to leave alone or a missing task:"]
        L += [f"- {coupling_line(x, 'no task names it')}." for x in hist["files"][:4]]
        if hist["total"] > 4:
            L.append(f"- and {hist['total'] - 4} more: `leyline spec facts`")
    if hist.get("functions"):   # the same, by function, for the functions the tasks name
        from .fncoupling import line as fn_line
        L += ["", "**Usually changes with the functions the tasks name, and no task names it.** Each line is either right"
                  " to leave alone or a missing task:"]
        L += [f"- {fn_line(x, 'no task names it')}." for x in hist["functions"][:4]]
    if b["patterns"]:
        L += ["", "**Design patterns the change sits in** (found from the shape of the code):"] + [
            f"- {p['pattern']}: {p['rationale']}" for p in b["patterns"][:5]]
    if b.get("drifted_specs"):
        L += ["", "**Specs that no longer match code this change touches** (update them with it):"] + [
            f"- {x}" for x in b["drifted_specs"][:8]] + ([f"- and {len(b['drifted_specs']) - 8} more"] if len(b["drifted_specs"]) > 8 else [])
    from . import related
    L += [x.replace("Earlier changes to this code (", "**Earlier changes to this code** (", 1) for x in related.lines(b.get("related_changes"))]
    L += ["", "## 3. How you will know it was done", "",
          "Each scenario is proven by a test with the same name. After the change, `leyline check` marks each one from "
          "the test results.", ""]
    if b["scenarios"]:
        L += ["| Scenario | When | Then | Test |", "| --- | --- | --- | --- |"]
        for s in b["scenarios"]:
            test = (f"made at run time by `{s['generated_by']}`" if s.get("generated_by") else
                    "in the test run (made at run time)" if s.get("in_run") else
                    "exists" if s["test_exists"] else "to be written, with this name")
            from .props import cell
            test += cell(s)
            L.append(f"| {s['name']} | {'; '.join(s['when']).replace('|', '/')} | {'; '.join(s['then']).replace('|', '/')} | {test} |")
    else:
        L.append("No scenarios yet: nothing says what done means.")
    L += ["", "## Review findings", ""] + review_lines(b["findings"], b.get("reviews") or [])
    st = brief_status(b)
    L += ["", "## Before implementation", ""]
    if st["blocking"]:
        L += [f"- {g}" for g in st["blocking"]]
    else:
        L.append("Nothing blocks implementation: every task that names code is tied to it, everything that must be edited has"
                 " a task, and every scenario has a test or a task to write one.")
    if st["decide"]:
        L += ["", "Still to decide, not blocking:"] + [f"- {d}" for d in st["decide"]]
    if st["notes"]:
        L += [""] + [f"- {x}" for x in st["notes"]]
    return "\n".join(L) + "\n"


# -- findings ------------------------------------------------------------------------------------
def add_finding(con, change_id: str, reviewer: str, severity: str, claim: str, evidence: Optional[list[str]] = None,
                proposal: str = "") -> dict:
    """A reviewer's finding against a change: a claim, the nodes behind it, and what to change in the spec."""
    if severity not in ("high", "medium", "low"):
        return {"error": "severity must be high, medium or low"}
    if not con.execute("SELECT 1 FROM change_proposals WHERE id = ?", (change_id,)).fetchone():
        return {"error": f"no change {change_id!r}; run the brief first"}
    known = {r[0] for r in con.execute("SELECT id FROM nodes")}
    kept = [e for e in (evidence or []) if e in known]
    if not kept:
        return {"error": "a finding needs evidence: at least one node id on the map that shows the problem"}
    fid = "f-" + hashlib.sha1(f"{change_id}|{reviewer}|{claim}".encode()).hexdigest()[:6]
    with con:
        con.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,COALESCE((SELECT status FROM findings WHERE id = ?), 'open'),"
                    "(SELECT resolution FROM findings WHERE id = ?),?)",
                    (fid, change_id, reviewer, severity, claim.strip(), json.dumps(kept), proposal.strip(), fid, fid,
                     datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")))
    out = {"id": fid, "status": "open"}
    if near_change(con, change_id, kept) is False:
        out["warning"] = ("None of the evidence is on the change's blast radius (what it changes, what must change with"
                          " it, the other ends of its channels, or one call from those). Kept, and marked so the person"
                          " questions it first; if the link is real, add evidence that shows it.")
    from . import learnings
    out.update(learnings.on_finding(con, fid, reviewer, claim.strip(), kept))   # a repeat of a past decision is marked
    return out


def resolve_finding(con, finding_id: str, status: str, resolution: str = "") -> dict:
    """The person's call on a finding: accepted (the spec changes), rejected or deferred, with the reason."""
    if status not in ("accepted", "rejected", "deferred", "open"):
        return {"error": "status must be accepted, rejected, deferred or open"}
    with con:
        n = con.execute("UPDATE findings SET status = ?, resolution = ? WHERE id = ?", (status, resolution, finding_id)).rowcount
    if not n:
        return {"error": f"no finding {finding_id!r}"}
    from . import learnings   # a rejection with a reason is kept, so later reviews do not ask again
    return {"id": finding_id, "status": status, **learnings.on_resolve(con, finding_id, status, resolution)}


def near_change(con, change_id: str, evidence: list[str]) -> Optional[bool]:
    """Whether any of a finding's evidence lies on the change's blast radius: a node the change's view marks (changed,
    must edit, the other end of a channel, a caller...), something nested in one or holding one, or a node one call
    from one. A finding whose evidence is elsewhere is not wrong for that, but it is the first a person should
    question: the reviewer may have wandered. None when the change has no view to judge by."""
    row = con.execute("SELECT spec FROM views WHERE id = ?", ("view-" + change_id,)).fetchone()
    marks = {m["id"] for m in json.loads(row[0] or "{}").get("marks", [])} if row else set()
    if not marks:
        return None
    for e in evidence:
        if e in marks or any(e.startswith(m + ".") or m.startswith(e + ".") for m in marks):
            return True
        for r in con.execute("SELECT src_id, dst_id FROM calls WHERE src_id = ? OR dst_id = ?", (e, e)):
            if r[0] in marks or r[1] in marks:
                return True
    return False


def findings(con, change_id: str) -> dict:
    from . import learnings
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM nodes")}
    learned = learnings.by_finding(con)
    out = []
    for r in con.execute("SELECT * FROM findings WHERE change_id = ? ORDER BY created", (change_id,)):
        ev = json.loads(r["evidence"] or "[]")
        near = near_change(con, change_id, ev)
        out.append({"id": r["id"], "reviewer": r["reviewer"], "severity": r["severity"], "claim": r["claim"],
                    "proposal": r["proposal"], "status": r["status"], "resolution": r["resolution"] or "",
                    "evidence": [{"id": e, "name": names.get(e, e)} for e in ev],
                    **({"evidence_far_from_change": True} if near is False else {}),
                    **({"learned": learned[r["id"]]} if r["id"] in learned else {})})
    return {"change_id": change_id, "open": sum(1 for f in out if f["status"] == "open"), "findings": out}


# -- verification --------------------------------------------------------------------------------
def verify(con, change_dir: str | Path, before_run: Optional[str] = None, after_run: Optional[str] = None, write: bool = True,
           old_run: Optional[str] = None) -> dict:
    """After implementation and a re-index: was the change made as the spec says? `old_run` names results left out
    because the code changed after they ran: a scenario they carry has a test, whose results are out of date."""
    parsed = parse(change_dir)
    if "error" in parsed:
        return parsed
    cid = "spec-" + parsed["id"]
    planned = con.execute("SELECT 1 FROM change_proposals WHERE id = ?", (cid,)).fetchone()
    if planned and parsed["tasks"] and not con.execute("SELECT 1 FROM spec_items WHERE change_id = ? LIMIT 1", (cid,)).fetchone():
        # The plan stored the change, and stopped before its tasks and scenarios: there is nothing to judge.
        kept = diff.snapshot_path(con, cid).exists()
        return {"error": f"the last plan of {parsed['id']} did not finish, so its tasks were not stored.",
                "next": [f"Next: run `leyline plan {parsed['id']}` again" + (" (the baseline it took is kept)" if kept else
                         " on the code as it was before the change") + ", then check it again."]}
    review = diff.review(con, cid, before_run, after_run)
    if "error" in review:
        if review["error"].startswith("No change"):   # never planned, or the plan stopped at an error
            return {"error": f"{parsed['id']} has not been planned, so there is nothing to compare the code with.",
                    "next": [f"Next: run `leyline plan {parsed['id']}` (the `plan` tool) on the code as it was before the"
                             " change (undo the edits, or start from the commit before them), then check it again."]}
        return {"error": review["error"] + (f" Run `leyline plan {parsed['id']}` on the change before it is implemented."
                                            if review["error"].startswith("No snapshot") else "")}
    names = _Names(con)
    g = review["graph"]["nodes"]
    touched = {n["id"] for key in ("added", "resigned", "edited", "types_edited") for n in g[key]}
    removed = {n["id"] for n in g["removed"]}
    added = {n["id"] for n in g["added"]}
    home = {}   # node -> the file and module it sits in, for tasks that name a file
    for i in touched | removed:
        row = con.execute("SELECT file_id, module_id FROM ancestry WHERE node_id = ?", (i,)).fetchone()
        home[i] = set(row) - {None} if row else set()
    items = con.execute("SELECT * FROM spec_items WHERE change_id = ? AND kind = 'task' ORDER BY key", (cid,)).fetchall()
    tests = _tests(con)
    test_names = diff.TestNames(con)
    results = {r["name"]: r for r in con.execute("SELECT * FROM test_results WHERE run = ?", (after_run,))} if after_run else {}
    index = _results_index(results.values())
    older = _results_index(con.execute("SELECT * FROM test_results WHERE run = ?", (old_run,)).fetchall()) if old_run else None
    from . import coverage as measured
    own_checks = _self_tests(con, [r for r in (before_run, after_run) if r])

    scenarios, scenario_ran = [], {}
    scenario_name = {s["key"]: s["name"] for s in parsed["scenarios"]}
    for s in parsed["scenarios"]:
        tid = tests.get(_norm(s["name"]))
        gen = None if tid else _generated(test_names, s["name"])
        rows = _scenario_results(index, s["name"])
        scenario_ran[s["key"]] = bool(rows)
        failed = [r for r in rows if r["status"] == "fail"]
        state = ("fails" if failed else "passes" if any(r["status"] == "pass" for r in rows) else "skipped" if rows
                 else "results older than the code" if older and _scenario_results(older, s["name"])
                 else "test exists, not run" if tid or gen else "no test")
        ran_change = None
        if tid and measured.has(con):
            ran = {r[0] for r in con.execute("SELECT node_id FROM covered WHERE test_id = ?", (tid,))}
            ran_change = bool(ran & touched) if ran else None
        static = bool(tid) and bool({r[0] for r in con.execute(
            "SELECT s.callable_id FROM flow_steps s JOIN flows f ON f.id = s.flow_id WHERE f.entry_id = ?", (tid,))} & touched)
        script = next((x["path"] for x in own_checks.values() if s["name"] in x["results"]), None) if not tid else None
        from .props import from_message   # a failing property test's smallest failing input
        ex = next((x for x in (from_message(r["message"]) for r in failed) if x), None)
        scenarios.append({"name": s["name"], "state": state, "test": tid, "generated": not tid and bool(gen),
                          **({"counterexample": ex} if ex else {}),
                          "off_map": not tid and not gen and bool(rows), **({"script": script} if script else {}),
                          "results": len(rows), "reaches_the_change": static, "measured_running_the_change": ran_change,
                          "message": (failed[0]["message"] or "") if failed else ""})

    tasks, declared, named = [], set(), set()
    for row in items:
        extra = json.loads(row["attrs"] or "{}")
        ids, new = json.loads(row["nodes"] or "[]"), extra.get("new", [])
        named |= set(ids)

        def did(i):
            if names.by_id.get(i, {"kind": ""})["kind"] in ("file", "module"):   # "change `file.py`": anything in it
                return any(i in home.get(t, ()) for t in touched | removed)
            return i in touched or i in removed or any(t.startswith((i + ".", i + "/", i + "(")) or t.split("(")[0] == i.split("(")[0]
                                                       for t in touched | removed)
        hit = [i for i in ids if did(i)]
        made = []
        for n in new:
            if n.get("file"):
                found = bool(names.file(n["name"]))
            else:
                leaf = n["name"].split(".")[-1]
                found_ids = [a for a in added if names.by_id.get(a) is not None and names.by_id[a]["name"] == leaf
                             and (not n.get("parent") or names.by_id[a]["parent_id"] == n["parent"]
                                  or n["parent"] in home.get(a, ()) or a.startswith(n["parent"] + "."))]
                declared |= set(found_ids)
                found = bool(found_ids)
            if found:
                made.append(n["name"])
        want = len({i.split("(")[0] for i in ids}) + len(new)
        got = len({i.split("(")[0] for i in hit}) + len(made)
        unproven = []
        for key in extra.get("scenarios", []):       # a task to write a scenario's test is done when that test exists or ran
            want += 1
            title = scenario_name.get(key, key.split("/", 1)[-1])
            there = _norm(title) in tests or scenario_ran.get(key, False)
            got += there
            if not there:
                unproven.append(f"a result for the test \"{title}\"")
        if not want and extra.get("into"):           # "add something to `Foo`": done when something inside Foo is new or edited
            want = 1
            files = {r[0] for i in extra["into"] for r in con.execute("SELECT node_id FROM ancestry WHERE file_id = ? OR module_id = ?", (i, i))}
            got = int(any(t in files or any(t.startswith(i + ".") for i in extra["into"]) for t in touched))
        by_you = extra.get("by_you", not want)
        state = ("checked by you" if by_you else "done" if want and got >= want else "partly" if got else "not done")
        from . import removal   # "Remove `X`" is done when X is gone and nothing still calls it, not when X is edited
        gone = None if by_you else removal.check(con, cid, row, names, touched, removed)
        state = gone["state"] if gone else state
        declared |= set(gone["made"]) if gone else set()   # a rename's new name is the task's, not an edit of its own
        checked = next((t["done"] for t in parsed["tasks"] if t["key"] == row["key"]), False)
        tasks.append({"key": row["key"], "text": row["text"], "state": state, "ticked": checked,
                      "missing": [_label(names, i) for i in ids if i not in hit][:6] + [n["name"] for n in new if n["name"] not in made][:6]
                      + unproven})
        if gone:
            tasks[-1].update(missing=gone["missing"], removal=gone)

    all_findings = findings(con, cid)["findings"]
    open_high = [f for f in all_findings if f["status"] == "open" and f["severity"] == "high"]
    test_ids = set(tests.values())
    # A new or edited test is how a scenario gets proven, not an edit outside the spec.
    tests_touched = [n for n in review["not_predicted"] if n["id"] in test_ids or n["id"].split("/test:")[0] in test_ids and "/test:" in n["id"]
                     or TEST_FILE.search(n.get("path") or "")]
    # "Add X to `Foo`" covers whatever is new or edited inside Foo.
    into = {i for row in items for i in json.loads(row["attrs"] or "{}").get("into", [])}
    inside = {r[0] for i in into for r in con.execute("SELECT node_id FROM ancestry WHERE file_id = ? OR module_id = ?", (i, i))}

    def in_container(i):
        return i in into or i in inside or any(i.startswith(c + ".") or i.startswith(c + "/") for c in into)
    # New code a task named (`build_cases.case_x`, `func` in `file.py`) is the task's, found the way the plan found it.
    drift = [n for n in review["not_predicted"] if n not in tests_touched and not in_container(n["id"])
             and n["id"] not in declared and n["id"] not in named]
    # Named code that is gone took what was inside it along (a class's members, a file's functions): not edits of their own.
    gone_named = sorted(i for i in named if i not in names.by_id)
    if gone_named and drift:
        was = diff._open(diff.snapshot_path(con, cid))
        try:
            marks = ",".join("?" * len(gone_named))
            went = {r[0] for r in was.execute(f"SELECT node_id FROM ancestry WHERE file_id IN ({marks}) OR module_id IN ({marks})",
                                              gone_named + gone_named)}
        finally:
            was.close()
        drift = [n for n in drift if not (n["id"] in removed and (n["id"] in went or any(
            n["id"].startswith((i + ".", i + "/", i + "(")) for i in gone_named)))]
    # A new function that only code named in the spec calls is how a task got done, not a change of its own.
    in_spec = {t for t in touched if t not in {n["id"] for n in drift}} | named
    helpers = []
    for n in list(drift):
        if n["id"] not in added or n.get("kind") != "callable":
            continue
        callers = {r[0] for r in con.execute("SELECT DISTINCT src_id FROM calls WHERE dst_id = ?", (n["id"],))} - {n["id"]}
        if callers and all(c in in_spec or in_container(c) or any(c.split("(")[0] == i.split("(")[0] for i in named) for c in callers):
            helpers.append({"name": n["name"], "id": n["id"], "called_by": sorted(_label(names, c) for c in callers)})
            drift.remove(n)
    # What is left of a body around the task's code (a module's top level, a class around a new method) changed
    # only next to that code, or to import or register it.
    spans, code_names = diff._spans(con, sorted(in_spec | declared | {h["id"] for h in helpers}))
    drift = [n for n in drift if not (n.get("own") and diff.explained(n, spans.get(n.get("path"), []), code_names))]
    from . import affected   # with per-test coverage: did each passing scenario's test run the code its tasks changed?
    affected.mark_scenarios(con, scenarios, ((in_spec | declared | {h["id"] for h in helpers}) & touched) or touched,
                            after_run, test_names, cid)
    # One verdict per task and scenario; which of them hold up "done as agreed" is the project's to set.
    judged = verdicts.judge(con, cid, parsed["dir"], tasks, scenarios, names, touched, removed, before_run, after_run)
    verdict = list(judged.pop("holds_up"))
    if drift:
        verdict.append(f"{_n(len(drift), 'edit is', 'edits are')} outside the spec")
    if review["rules"]["new_violations"]:
        verdict.append("a rule that held now fails")
    if review["graph"]["structure"]["new_dependencies"]:
        verdict.append("new links between modules")
    if review["tests"] and review["tests"]["newly_failing"]:
        verdict.append("tests that passed now fail")
    if review["tests"] and review["tests"].get("new_failing"):
        verdict.append("tests added since the plan fail")
    after_fails = [r["name"] for r in results.values() if r["status"] == "fail"]
    if not review["tests"] and after_fails:
        verdict.append(f"{_n(len(after_fails), 'test fails', 'tests fail')}, and with no run from before the change it cannot"
                       " be told whether the change broke them")
    if open_high:
        verdict.append("a high review finding is still open")
    for n in drift:
        n.pop("own", None)
    out = {"change_id": cid, "title": parsed["title"], "tasks": tasks, "scenarios": scenarios, "drift": drift,
           "predicted_not_edited": [{**n, "name": _label(names, n["id"]) if n["id"] in names.by_id else n["name"]}
                                    for n in review["predicted_untouched"]], "new_dependencies": review["graph"]["structure"]["new_dependencies"],
           "rules_newly_failing": review["rules"]["new_violations"], "tests": review["tests"], "open_high_findings": open_high,
           "tests_added_or_changed": [n["name"] for n in tests_touched], "helpers_added": helpers,
           "review": {"findings": len(all_findings), "open": sum(f["status"] == "open" for f in all_findings),
                      "kinds": reviews(con, cid), "all": all_findings},
           "baseline": review.get("baseline"),
           "baseline_other_version": review.get("baseline_other_version", False),
           "after_tests": {"passed": sum(r["status"] == "pass" for r in results.values()), "total": len(results),
                           "skipped": sum(r["status"] == "skip" for r in results.values())} if results else None,
           "after_failing": [{"name": n} for n in after_fails],
           "done_as_agreed": not verdict, "why_not": verdict, "verdicts": judged, "view_id": review.get("view_id")}
    from . import diagrams   # the changed code as it runs now, and the calls and channel links it gained and lost
    out["how_it_runs"] = diagrams.safe(diagrams.for_snapshot, diff.snapshot_path(con, cid), con,
                                       [n["id"] for k in ("edited", "resigned", "added") for n in g[k]]
                                       or [n["id"] for n in g["types_edited"]], [n["id"] for n in g["removed"]])
    if write:
        path = Path(parsed["dir"]) / "leyline.md"
        body = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        head = body[body.index(BEGIN) + len(BEGIN):body.index(END)].strip() if BEGIN in body and END in body else ""
        head = head.split("\n## 4. Was it done as agreed")[0].rstrip()
        # The state at the top of the page is now the verdict.
        head = re.sub(r"^\*\*State:.*$", lambda _: check_state(out), head, count=1, flags=re.M)
        _write(path, head + "\n\n" + verify_text(out))
        out["written"] = str(path)
    # The latest check decides: the change is finished (verified) only while its last check found it done. Each check
    # is kept in `checks`. Done, the baseline stays (it is small), so a later edit can be checked against the same
    # start; it goes with `leyline spec forget`, or once the change folder is archived or removed.
    row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
    attrs = json.loads(row[0] or "{}") if row else {}
    at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    attrs["checks"] = (attrs.get("checks") or [])[-19:] + [{"at": at, "done": bool(out["done_as_agreed"])}]
    if out["done_as_agreed"]:
        attrs["verified"] = at
    else:
        attrs.pop("verified", None)
    with con:
        con.execute("UPDATE change_proposals SET status = ?, attrs = ? WHERE id = ?",
                    ("verified" if out["done_as_agreed"] else "implemented", json.dumps(attrs), cid))
    if out["done_as_agreed"]:
        from . import drift   # what each code name meant now that it is agreed: `leyline drift` compares later code with it
        out["anchors"] = drift.record(con, change_dir, write_file=write)
    return out


def _yes(v: dict) -> str:
    other = verdicts.yes_text(v)   # something other than proven was let through: say so
    if other:
        return other
    mine = [t["key"] for t in v["tasks"] if t["state"] == "checked by you"]
    return ("Every task " + ("that names code " if mine else "") + "is done, every scenario is proven, and nothing outside"
            " the spec changed." + (f" Task{'s' if len(mine) > 1 else ''} {', '.join(mine)} {'are' if len(mine) > 1 else 'is'}"
                                    " yours to check." if mine else ""))


def check_state(v: dict) -> str:
    if v["done_as_agreed"]:
        return "**State: done as agreed.** " + _yes(v)
    return "**State: not done as agreed yet:** " + "; ".join(v["why_not"]) + ". Details under \"Was it done as agreed\"."


def _drift_line(n: dict) -> str:
    if n["name"] in ("<module>", "<top-level>"):
        return f"- the top level of {n.get('path') or 'a file'} (code outside any function)"
    return f"- {n['name']} ({n.get('why') or n['kind']}) {n.get('path') or ''}".rstrip()


def verify_text(v: dict) -> str:
    L = ["## 4. Was it done as agreed", "",
         "Leyline marks each task from what changed in the code since the plan, whether or not it is ticked in tasks.md,"
         " and each scenario from the test results.", "",
         "**Yes.** " + _yes(v) if v["done_as_agreed"]
         else "**Not yet:** " + "; ".join(v["why_not"]) + ".", "",
         *(["*The baseline was taken by another version of Leyline (or one that did not record its version), which may"
            " have read the same code differently: a change below that the diff does not show comes from that, not from"
            " the edit.*", ""] if v.get("baseline_other_version") else []),
         "| Task | Result | Verdict | Missing |", "| --- | --- | --- | --- |"]
    for t in v["tasks"]:
        L.append(f"| {t['key']} {_clip(t['text'], 70).replace('|', '/')} | {t['state']} | {t.get('verdict', '')} | {', '.join(t['missing'])} |")
    if any(t["state"] == "checked by you" for t in v["tasks"]):
        L += ["", "*Checked by you*: the task names no code, so the map cannot see it done; "
              + ("this project makes that hold up the verdict." if verdicts.PERSON in (v.get("verdicts") or {}).get("blocking", ())
                 else "it does not hold up the verdict.")]
    L += ["", "| Scenario | Result | Verdict | Evidence |", "| --- | --- | --- | --- |"]
    for s in v["scenarios"]:
        ev = ("measured running the changed code" if s["measured_running_the_change"] else
              "passed without running the changed code" if s.get("ran_changed_code") is False else
              "proven by the test run (the test is generated, so it is not on the map)"
              if s["generated"] and s["state"] == "passes" else
              f"proven by the test run (a check in {s['script']}, which runs as a script; the test is not on the map)"
              if s.get("script") and s["state"] == "passes" else
              "proven by the test run (the test is not on the map)" if s.get("off_map") and s["state"] == "passes" else
              "its test reaches the changed code on the map" if s["reaches_the_change"] else
              "its test does not reach the changed code" if s["test"] else "")
        if s["state"] == "passes" and s.get("results", 0) > 1:
            ev += f" ({s['results']} results carry its name; all pass)"
        msg = " ".join((s["message"] or "").split()).replace("|", "/")
        L.append(f"| {s['name']} | {s['state']} | {s.get('verdict', '')} | {(msg[:200] + '...' if len(msg) > 200 else msg) or ev} |")
    L += verdicts.page_lines(v)
    if v["drift"]:
        L += ["", "**Changed, but not in the spec:**"] + [_drift_line(n) for n in v["drift"][:25]]
    if v.get("helpers_added"):
        L += ["", "**Helpers added** (new, and called only by code the spec names):"] + [
            f"- {h['name']}, called by {_some(h['called_by'])}" for h in v["helpers_added"][:15]]
    if v["predicted_not_edited"]:
        L += ["", "**Expected to change, and did not:**"] + [f"- {n['name']}" for n in v["predicted_not_edited"][:25]]
    if v["new_dependencies"]:
        L += ["", "**New links between modules:**"] + [f"- {d['from']} to {d['to']}" for d in v["new_dependencies"]]
    if v["rules_newly_failing"]:
        L += ["", "**Rules that held before and fail now:**"] + [f"- {r['kind']} {r['from']} {r['to']}" for r in v["rules_newly_failing"]]
    t = v["tests"]
    if t:
        L += ["", f"Tests: {diff.passed_pair_text(t['before'], t['after'])}."]
        L += [f"- now fails: {x['name']}" + (f": {x['message']}" if x["message"] else "") for x in t["newly_failing"]]
        L += [f"- new, and fails: {x['name']}" + (f": {x['message']}" if x["message"] else "") for x in t.get("new_failing", [])]
    elif v.get("after_tests"):
        a = v["after_tests"]
        L += ["", f"Tests: {diff.passed_text(a)} after the change. No run from before it was recorded, so a test "
                  "the change broke cannot be told from one that already failed."]
        L += [f"- fails: {r['name']}" for r in v.get("after_failing", [])[:10]]
    else:
        L += ["", "No test results were recorded after the change, so scenarios cannot be marked as passing."]
    from . import diagrams
    L += diagrams.section(v.get("how_it_runs"), "### How it runs now")
    r = v.get("review") or {}
    L += ["", "Review before implementation:"] + review_lines(r.get("all") or [], r.get("kinds") or [], full=False)
    if v.get("baseline"):
        L += ["", f"Compared with the code as it was at {v['baseline']}."]
    return "\n".join(L) + "\n"


# -- facts for reviewers ---------------------------------------------------------------------------
SPEED_PLACE = re.compile(r"perf|bench|budget|latency|throughput|load[-_ ]?test|stress", re.I)
CLOCK = re.compile(r"performance\.now|process\.hrtime|perf_counter|time\.time\(|time\.monotonic|timeit|Stopwatch|"
                   r"System\.nanoTime|System\.currentTimeMillis|Instant::now|time\.Now\(|Date\.now\(\)|console\.time|"
                   r"benchmark|\bbench\(", re.I)


def _speed_tests(con, changed: set, self_tests: Optional[dict] = None) -> list[dict]:
    """Tests that measure speed: those in a file or suite named for it, and those whose own text reads a clock.
    A test named "adds 5s of slow_down" does not measure speed; perf-budget.test.ts does. A script's own checks
    (see _self_tests) count too, by their names and by a clock read near where each is named."""
    tests = con.execute("SELECT id, name, path, repo_id, span_start, span_end, attrs FROM nodes WHERE kind = 'test'"
                        " OR json_extract(attrs, '$.is_test') = 1").fetchall()
    root_of = diff.roots(con)
    texts: dict = {}
    out = []
    for r in tests:
        a = json.loads(r["attrs"] or "{}")
        place = bool(SPEED_PLACE.search(r["path"] or "") or SPEED_PLACE.search(a.get("suite") or "")
                     or SPEED_PLACE.search(r["name"] or ""))
        key = (r["repo_id"], r["path"])
        if key not in texts:
            data = diff.source(con, *key, root_of=root_of) if r["path"] else None
            texts[key] = data.decode("utf-8", errors="replace").split("\n") if data else []
        body = "\n".join(texts[key][(r["span_start"] or 1) - 1:r["span_end"] or 0])
        clock = bool(body and CLOCK.search(body))
        if place or clock:
            out.append({"id": r["id"], "name": r["name"], "path": r["path"], "why": " and ".join(
                x for x in ("named for speed" if place else "", "reads a clock" if clock else "") if x)})
    done = set()
    for entry, x in (self_tests or {}).items():
        if x["path"] in done:
            continue
        done.add(x["path"])
        repo = con.execute("SELECT repo_id FROM nodes WHERE id = ?", (entry,)).fetchone()
        data = diff.source(con, repo[0], x["path"], root_of) if repo else None
        lines = data.decode("utf-8", errors="replace").split("\n") if data else []
        for name in x["results"]:
            at = [k for k, ln in enumerate(lines) if name in ln]
            near = "\n".join(lines[max(0, at[0] - 15):at[0] + 3]) if at else ""
            place, clock = bool(SPEED_PLACE.search(name)), bool(near and CLOCK.search(near))
            if place or clock:
                out.append({"id": entry, "name": name, "path": x["path"], "why": " and ".join(
                    w for w in ("named for speed" if place else "", "reads a clock" if clock else "") if w)
                    + " (a check the script runs and prints)"})
    if out and changed:
        reach = {r[0] for r in con.execute(
            f"SELECT DISTINCT f.entry_id FROM flow_steps s JOIN flows f ON f.id = s.flow_id"
            f" WHERE s.callable_id IN ({','.join('?' * len(changed))})", sorted(changed))} if len(changed) < 900 else set()
        for x in out:
            x["runs_the_change"] = x["id"] in reach
    out.sort(key=lambda x: (not x.get("runs_the_change"), "named" not in x["why"], "clock" not in x["why"], x["path"] or "", x["name"]))
    return out


def hot_functions(con, names, fns: list[str]) -> list[dict]:
    """Changed functions by how much runs through them: flows that pass through, program entries (not tests) that
    reach them, and call sites. A function most flows pass through is on a hot path."""
    n_flows = con.execute("SELECT COUNT(*) FROM flows").fetchone()[0] or 1
    hot = []
    for i in dict.fromkeys(fns):
        through = con.execute("SELECT COUNT(DISTINCT flow_id) FROM flow_steps WHERE callable_id = ?", (i,)).fetchone()[0]
        entry = con.execute("SELECT COUNT(DISTINCT s.flow_id) FROM flow_steps s JOIN flows f ON f.id = s.flow_id"
                            " WHERE s.callable_id = ? AND json_extract(f.attrs, '$.kind') != 'test'", (i,)).fetchone()[0]
        sites = con.execute("SELECT COUNT(*) FROM calls WHERE dst_id = ?", (i,)).fetchone()[0]
        hot.append({"id": i, "name": _label(names, i), "flows_through": through, "share_of_flows": round(through / n_flows, 2),
                    "program_entries_that_reach_it": entry, "call_sites": sites})
    hot.sort(key=lambda h: (-h["flows_through"], -h["call_sites"]))
    return hot


def review_facts(con, change_dir: str | Path, reviewer: Optional[str] = None) -> dict:
    """What the graph says about a change, arranged as the questions each reviewer must answer.
    A reviewer reads this, reads the code behind anything suspicious, and files findings. Naming the
    `reviewer` (logic or performance) records that this review ran, so the plan can say so."""
    b = brief(con, change_dir, write=False)
    if "error" in b:
        return b
    if reviewer:
        record_review(con, b["change_id"], reviewer)
    names = _Names(con)
    tasked = sorted({i for t in b["tasks"] for i in t["nodes"]})
    fns = [i for i in tasked if names.by_id[i]["kind"] in ("callable", "test")]
    for i in tasked:   # a named type stands for its functions
        if names.by_id[i]["kind"] == "type":
            fns += [r["id"] for r in names.rows if r["parent_id"] == i and r["kind"] == "callable"]
    hot = hot_functions(con, names, fns)
    perf_tests = _speed_tests(con, set(fns), b.get("self_tests"))
    la = b["left_alone"]
    imp = b["impact"] if "error" not in b["impact"] else {}
    from . import learnings
    return {
        "change_id": b["change_id"], "title": b["title"],
        "learnings_that_apply": learnings.applying(con, b["change_id"], tasked),   # past decisions: read these first
        "related_changes": b.get("related_changes") or {},   # earlier changes to the same code
        "logic": {
            "must_edit_with_no_task": b["must_edit_uncovered"],
            "channels_crossed": imp.get("channels") or [],
            "other_ends_of_those_channels_no_task_names": b.get("must_agree") or [],
            "shared_state_written": b["shared_state"],
            "scenarios_with_no_test": [s["name"] for s in b["scenarios"] if not s["test_exists"]],
            "new_members_named_like_existing_ones": la["beside"][:20],
            "callers_of_changed_functions_the_spec_leaves_alone": la["callers"][:30],
            "state_shared_with_functions_the_spec_leaves_alone": la["state"][:30],
            "usually_changes_with_no_task": b.get("usually_changes_with") or {},
            "changed_code_no_test_reaches": imp.get("untested") or [],
            "patterns_involved": b["patterns"],
            "rules_failing_before_the_change": b["rules_failing_now"],
            "spec_problems": b["gaps"],
            "ask": "For each list: is the spec wrong, or is the map? Read the code before filing. Then ask what the scenarios "
                   "leave out: error paths, empty inputs, ordering, the second caller, the other end of each channel.",
        },
        "performance": {
            "changed_functions_by_how_much_runs_through_them": hot[:25],
            "tests_that_measure_speed": perf_tests[:20],
            "speed_tests_note": "Tests in files or suites named for speed (perf, bench, budget, latency, throughput), and"
                                " tests that read a clock. Those that run the changed functions come first."
                                if perf_tests else "No test on the map measures speed.",
            "ask": "A function most flows pass through is on a hot path. For each one the spec changes: does the change add work "
                   "per call, allocation, I/O or a process hop? Name the test that would show a regression, or say none exists.",
        },
        "how_to_file": "Call spec_finding with a claim, a severity, node ids as evidence, and the change to the spec you propose.",
    }
