"""Learnings: what people decided when they rejected a review finding, kept so later reviews do not ask again.

When a person rejects a finding and says why, the reason is kept with the finding's claim, its kind of review and
where it points: the evidence nodes, and the type, file and module each sits in, so a decision about one function
also covers its neighbours a little. Learnings live in a file in the repository, committed like code, so the team
shares them and a fresh map keeps them:

    openspec/leyline-learnings.json     when the repository has an openspec folder
    .leyline-learnings.json             otherwise, at the repository's root

Node ids are kept without the repository's id (`python:app.use.count`, not `myrepo:python:app.use.count`), so a
clone in a folder of another name reads them the same.

A later finding of the same kind, on the same code, saying much the same thing (word overlap, see `similarity`) is
kept and marked: the reviewer and the person see which past decision it matches. If people then reject it too, the
learning held; if they accept findings it matched more often than they reject them (at least twice), it was wrong,
and it is retired.

A learning also keeps a fingerprint of the code it is about: the text hash the map keeps for each evidence node (the
node's own lines, trimmed, so moving or re-indenting it does not count; the file's hash for a node with none). Each
time a learning is used it is checked against the map: when a node's code was edited, or the node is gone, the
learning is `stale` and says which. A stale learning still applies and still marks findings; the person decides
whether the decision holds for the new code (`confirm` records the code as it is now) or not (`retire`). A learning
kept before Leyline recorded fingerprints has none: whether its code changed is unknown, not stale.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from . import store

FILE_IN_OPENSPEC = "openspec/leyline-learnings.json"
FILE_AT_ROOT = ".leyline-learnings.json"
ABOUT = ("Decisions people made on Leyline review findings: each rejected finding, with the reason in the person's"
         " words and the code it is about. Reviewers read the active ones first. Commit this file.")
# Word overlap (Jaccard) a finding's claim needs with a learning's claim to match it: closer code needs less.
SAME_CODE = 0.45   # the same node, a node in the same type, or the same file (tuned on test_learnings's claims)
SAME_MODULE = 0.6  # only the same module
RETIRE_AFTER = 2   # accepted findings a learning matched, more than it matched rejected ones, before it is retired
LEVELS = ("node", "type", "file", "module")

STOP = set("""a an the and or but nor of to in on at by for with from into onto over under is are was were be been being
it its it's this that these those as if then than so no not still can could will would should may might must do does
did done has have had having when which who whom what where there here also only any all each every more less very just
same other without about after before because while yet such some their them they he she we you your our his her
one ever never get gets got via per too instead""".split())
GENERIC = {"call", "caller", "function"}   # (as SYNONYMS leaves them) in nearly every claim about code: they say nothing


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


# -- the file ------------------------------------------------------------------------------------
def path_for(root: Path) -> Path:
    """Where a repository keeps its learnings: the file it has, else beside its OpenSpec changes when it has them,
    else at its root. A file stays where it was made when an openspec folder appears later."""
    for p in (root / FILE_IN_OPENSPEC, root / FILE_AT_ROOT):
        if p.is_file():
            return p
    return root / FILE_IN_OPENSPEC if (root / "openspec").is_dir() else root / FILE_AT_ROOT


class Unreadable(OSError):
    """A learnings file that is there but cannot be read (a merge left conflict markers in it): it is not written over."""


def _read(path: Path, strict: bool = False) -> list[dict]:
    """The learnings in a file. `strict`, for a read that will be written back: a file that is there and cannot be
    read raises Unreadable, so the decisions in it are not replaced by a file holding only the new one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RecursionError) as e:
        if strict:
            raise Unreadable(f"{path} could not be read ({e}); fix it by hand, then decide again")
        return []
    items = data.get("learnings") if isinstance(data, dict) else None
    if strict and not isinstance(items, list):
        raise Unreadable(f"{path} could not be read (no list of learnings in it); fix it by hand, then decide again")
    return [x for x in items if isinstance(x, dict) and x.get("id")] if isinstance(items, list) else []


def _write(path: Path, items: list[dict]) -> None:
    """Canonical JSON (sorted keys, two-space indent, UTF-8 as is, one newline at the end), written whole and then
    moved into place, so a reader never sees half a file."""
    items = sorted(({k: v for k, v in x.items() if not k.startswith("_")} for x in items),
                   key=lambda x: (x.get("created", ""), x["id"]))
    body = json.dumps({"about": ABOUT, "learnings": items}, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if (why := store.escapes(path.parent)):
        raise store.UntrustedStore(why)
    store.write_file(path, body)


def _roots(con) -> dict:
    return {repo: root for repo, root in store.roots(con).items() if root.is_dir()}


def _repos(con) -> dict:
    """{learnings file: the repository id it belongs to}."""
    return {path_for(root): repo for repo, root in _roots(con).items()}


def _all(con) -> dict:
    """{file: its learnings} for every mapped repository that has a learnings file."""
    out = {}
    for root in _roots(con).values():
        p = path_for(root)
        if p.is_file():
            out[p] = _read(p)
    return out


def _unreadable(con) -> list[str]:
    """Why each learnings file that is there cannot be read: its learnings are missing from every list until it is fixed."""
    out = []
    for root in _roots(con).values():
        p = path_for(root)
        if p.is_file():
            try:
                _read(p, strict=True)
            except Unreadable as e:
                out.append(str(e).replace("; fix it by hand, then decide again", "; fix it by hand"))
    return out


# -- learnings a pull request brings with it ---------------------------------------------------------
def _items_at(root: Path, rev: str, rel: str) -> dict:
    """{id: learning} in a learnings file as a commit has it; {} when it has none or it cannot be read."""
    import subprocess
    try:
        run = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{rel}"], capture_output=True, timeout=60,
                             stdin=subprocess.DEVNULL)
        data = json.loads(run.stdout.decode("utf-8", "replace")) if run.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
        return {}
    items = data.get("learnings") if isinstance(data, dict) else None
    return {x["id"]: x for x in items if isinstance(x, dict) and x.get("id")} if isinstance(items, list) else {}


def from_the_change(con, change_id: Optional[str]) -> dict:
    """{learning id: why} for the learnings a pull request's own commits add or change. A learning says a reviewer's
    worry was rejected before, so a pull request that commits one ("f may return anything, do not flag it") would be
    steering its own review: those are not applied to it, and its page lists them for the person to judge."""
    if not change_id or not change_id.startswith("pr-"):
        return {}
    row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (change_id,)).fetchone()
    a = json.loads(row[0] or "{}") if row else {}
    if not a.get("root") or not a.get("base_sha") or not Path(a["root"]).is_dir():
        return {}
    out = {}
    for rel in (FILE_IN_OPENSPEC, FILE_AT_ROOT):
        before, after = _items_at(Path(a["root"]), a["base_sha"], rel), _items_at(Path(a["root"]), "HEAD", rel)
        for lid, item in after.items():
            if before.get(lid) != item:
                out[lid] = "changed by this pull request" if lid in before else "added by this pull request"
    return out


def not_applied(con, change_id: str) -> list[dict]:
    """The learnings a pull request adds or changes, as the page lists them: not applied to its review."""
    skip = from_the_change(con, change_id)
    if not skip:
        return []
    items = {l["id"]: l for ls in _all(con).values() for l in ls}
    return [{"id": lid, "why": why, "claim": (items.get(lid) or {}).get("claim"), "reason": (items.get(lid) or {}).get("reason")}
            for lid, why in sorted(skip.items())]


# -- where a finding points ----------------------------------------------------------------------
def _bare(nid: str, repo: Optional[str]) -> str:
    return nid[len(repo) + 1:] if repo and nid.startswith(repo + ":") else nid


def scope_of(con, ids: list[str]) -> dict:
    """The evidence nodes, and the type, file and module each sits in, as ids without the repository's id; with
    the files' paths, for a person reading the file."""
    s = {k: set() for k in ("nodes", "types", "files", "modules", "paths")}
    for i in ids:
        row = con.execute("SELECT n.kind, n.parent_id, n.repo_id, n.path, a.file_id, a.module_id FROM nodes n"
                          " LEFT JOIN ancestry a ON a.node_id = n.id WHERE n.id = ?", (i,)).fetchone()
        if row is None:
            continue
        repo = row["repo_id"]
        s["nodes"].add(_bare(i, repo))
        cur, kind, parent, seen = i, row["kind"], row["parent_id"], 0
        while cur and seen < 20:   # the nearest type holding it, or itself when it is one
            if kind == "type":
                s["types"].add(_bare(cur, repo))
                break
            cur = parent
            r = con.execute("SELECT kind, parent_id FROM nodes WHERE id = ?", (cur,)).fetchone() if cur else None
            kind, parent = (r["kind"], r["parent_id"]) if r else (None, None)
            seen += 1
        if row["file_id"]:
            s["files"].add(_bare(row["file_id"], repo))
        if row["module_id"]:
            s["modules"].add(_bare(row["module_id"], repo))
        if row["path"]:
            s["paths"].add(row["path"])
    return {k: sorted(v) for k, v in s.items()}


def _in_repo(con, ids: list[str], repo: Optional[str]) -> list[str]:
    """The ids of nodes in one repository. A learnings file's ids leave the repository's id out, so in a workspace
    another repository can hold a node of the same id: a learning is compared only with code in its own."""
    if repo is None:
        return list(ids)
    return [i for i in ids if (con.execute("SELECT repo_id FROM nodes WHERE id = ?", (i,)).fetchone() or [None])[0] == repo]


def _nested(a: str, b: str) -> bool:
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def overlap(learned: dict, here: dict) -> Optional[str]:
    """How close two scopes are: node (the same node, or one inside the other), type, file, module, or None."""
    if any(_nested(a, b) for a in learned.get("nodes", []) for b in here.get("nodes", [])):
        return "node"
    if set(learned.get("types", [])) & (set(here.get("types", [])) | set(here.get("nodes", []))) \
            or set(here.get("types", [])) & set(learned.get("nodes", [])):
        return "type"
    for level in ("files", "modules"):
        if set(learned.get(level, [])) & set(here.get(level, [])):
            return level[:-1]
    return None


# -- whether the code changed since ----------------------------------------------------------------
def _code_hash(con, nid: str) -> Optional[str]:
    """The text hash the map keeps for a node (its own lines; the file's for a node with none), '' when it has
    neither, None when the node is not in the map."""
    row = con.execute("SELECT COALESCE(n.content_hash, f.content_hash, '') FROM nodes n LEFT JOIN ancestry a"
                      " ON a.node_id = n.id LEFT JOIN nodes f ON f.id = a.file_id WHERE n.id = ?", (nid,)).fetchone()
    return row[0] if row else None


def _full(con, repo: Optional[str], bare: str) -> Optional[str]:
    """The id in this map of a node kept without its repository's id, or None when the map has no such node."""
    for nid in ([f"{repo}:{bare}"] if repo else []) + [bare]:
        if con.execute("SELECT 1 FROM nodes WHERE id = ?", (nid,)).fetchone():
            return nid
    return None


def fingerprint(con, repo: Optional[str], nodes) -> dict:
    """{node id without the repository's id: its text hash now}; None for a node the map does not have."""
    out = {}
    for bare in sorted(set(nodes or ())):
        nid = _full(con, repo, bare)
        out[bare] = _code_hash(con, nid) if nid else None
    return out


def changed_text(edited, gone) -> str:
    """`a` edited, `b` gone."""
    return ", ".join([f"`{n}` edited" for n in edited] + [f"`{n}` gone" for n in gone])


def code_check(con, repo: Optional[str], l: dict) -> dict:
    """Whether the code a learning is about changed since it was kept or last confirmed. `code` is unchanged,
    changed or unknown (no fingerprint: kept before Leyline recorded them); `stale` is true only for changed, with the nodes `edited`
    and `gone`; `code_note` says it in a sentence when the code is not unchanged."""
    kept = l.get("fingerprint")
    if not isinstance(kept, dict):
        return {"code": "unknown", "stale": False,
                "code_note": "Kept before Leyline recorded the code a learning is about, so whether that code changed"
                             f" since is not known. If the decision still holds, `leyline learnings confirm {l['id']}`"
                             " records the code as it is now."}
    current = fingerprint(con, repo, kept)
    edited = sorted(n for n, h in kept.items() if current[n] is not None and current[n] != h)
    gone = sorted(n for n, h in kept.items() if current[n] is None and h is not None)
    if not edited and not gone:
        return {"code": "unchanged", "stale": False}
    return {"code": "changed", "stale": True, "edited": edited, "gone": gone,
            "code_note": f"The code it was about has changed since: {changed_text(edited, gone)}. The decision may"
                         f" not hold for the new code: ask the person, then `leyline learnings confirm {l['id']}` if"
                         " it does, or retire it if not."}


def _checked(con, repos: dict, path: Path, l: dict) -> dict:
    try:
        return code_check(con, repos.get(path), l)
    except Exception as e:   # a map that cannot be read says nothing about the code; the learning still applies
        return {"code": "unknown", "stale": False, "code_note": f"Could not compare with the map: {e}"}


# -- what a claim says ---------------------------------------------------------------------------
# Words reviewers use for the same thing, one group per line: every word in a group counts as the group's first. Kept
# short on purpose: each group is a way for two different worries to look alike, so a group goes in only when its words
# are interchangeable in a claim about code. No word is in two groups (`test_learnings` checks).
SYNONYMS = """
argument parameter param arg args argv
remove delete drop erase
null none nil undefined nullptr nullish
caller callsite
call invoke invocation
function method func routine procedure callable
error exception
throw raise rethrow
crash panic abort kill
empty blank
missing absent omit omitted lack lacks
check verify guard
wrong incorrect invalid bad broken
ignore skip bypass overlook
change update modify mutate edit alter
create construct instantiate allocate
read load fetch retrieve
write save persist store
lock mutex synchronize
race concurrent concurrently
slow expensive costly inefficient
memory ram heap
loop iterate iteration
cache memoize memoization
config configuration setting option
field property attribute attr prop member
type class struct
list array vector sequence
map dict dictionary hashmap
number integer int float numeric
boolean bool
length size len
duplicate copy clone dup repeated
unused dead
async asynchronous await
encoding charset codec
init initialize initialise setup
close dispose release cleanup
default fallback
timeout deadline
"""
# Phrases that say one thing in several words, rewritten before the words are split.
PHRASES = ((r"\bcall[\s-]sites?\b", "callsite"), (r"\bcalling code\b", "caller"), (r"\bout of (?:range|bounds)\b", "outofbounds"),
           (r"\brace conditions?\b", "race"), (r"\bnull pointers?\b", "null"), (r"\btime[\s-]?outs?\b", "timeout"),
           (r"\bset[\s-]?up\b", "setup"), (r"\bclean[\s-]?up\b", "cleanup"), (r"\bgives? back\b", "returns"), (r"\bas well\b", " "))
# Words in nearly every claim about some code: shared, they say little, so they weigh half (made into the form `words`
# gives them below `_canon`).
LIGHT_WORDS = """file read write return value data use used list name type string new change set get run path line test
code item result output input object field number request response"""
EXTENSIONS = set("py pyi ts tsx js jsx mjs cjs cs go rs java kt swift c h cc cpp hpp rb php scala lua sh gd json md yml"
                 " yaml toml".split())


def _qualified(m) -> str:
    """`app.use.count`, `Engine::start()`, `pkg/core.py`: the short name, which is how another claim will say it."""
    parts = re.split(r"\.|::|#|/", m.group(0).rstrip("()"))
    if len(parts) > 1 and parts[-1].lower() in EXTENSIONS:
        parts = parts[:-1]
    return parts[-1]


def short_names(ids) -> set:
    """The names of nodes as a claim writes them: `python:app.use.count` is count, `csharp:P::N.T.M(int)` is M."""
    out = set()
    for i in ids or ():
        tail = re.sub(r"\(.*$", "", i).split("/")[-1]
        name = re.split(r"\.|::|:", tail)[-1]
        if name and not name.startswith("<"):
            out.add(name)
    return out


def words(text: str, names=()) -> set:
    """A claim's words, compared loosely: a qualified name cut to its short name (`app.use.count` is count), the
    names in `names` (the code both claims are about, which says nothing about the worry) left out, other code names
    split (`EntryQueues` is entry and queue, `parse_args` parse and args), lower case, common words dropped, plural
    and tense endings cut, and words a review uses for the same thing (`SYNONYMS`, `PHRASES`) made one."""
    text = text or ""
    for pat, rep in PHRASES:
        text = re.sub(pat, rep, text, flags=re.I)
    text = re.sub(r"[A-Za-z_]\w*(?:(?:\.|::|#|/)[A-Za-z_]\w*)+(?:\(\))?", _qualified, text)
    skip = {n.lower().replace("_", "") for n in names}   # parse_args and parseArgs are one name
    if skip:
        text = re.sub(r"[A-Za-z_]\w*", lambda m: " " if m.group(0).lower().replace("_", "") in skip else m.group(0), text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP or len(w) < 2:
            continue
        w = _stem(w)
        w = CANON.get(w, w)
        if w not in GENERIC:
            out.add(w)
    return out


def _stem(w: str) -> str:
    """Crude, but the same on both sides: files, file; reads, reading, read; encoded, encoding, encode."""
    if w.isdigit() or len(w) < 4:
        return w
    if w.endswith("ies"):
        w = w[:-3] + "y"
    elif re.search(r"(ss|x|ch|sh|zz)es$", w):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for end in ("ing", "ed"):
        if w.endswith(end) and len(w) - len(end) >= 3:
            w = w[: -len(end)]
            break
    if w.endswith("e") and len(w) >= 4:
        w = w[:-1]
    return w


def _canon() -> dict:
    out = {}
    for line in SYNONYMS.split("\n"):
        group = line.split()
        for w in group:
            out[_stem(w)] = _stem(group[0])
    return out


CANON = _canon()
LIGHT = {CANON.get(_stem(w), _stem(w)) for w in LIGHT_WORDS.split()}


def similarity(a: str, b: str, names=()) -> float:
    """How much two claims say the same: the overlap of their words (Jaccard), each word weighing 1 but the common
    ones in `LIGHT`, which weigh half. 0 shares nothing, 1 is the same words. `names` are the code both claims are
    about, left out of both."""
    x, y = words(a, names), words(b, names)
    if not x or not y:
        return 0.0
    weight = lambda ws: sum(0.5 if w in LIGHT else 1.0 for w in ws)
    return weight(x & y) / weight(x | y)


# -- the steps that record and use learnings ------------------------------------------------------
def _finding(con, fid: str):
    return con.execute("SELECT * FROM findings WHERE id = ?", (fid,)).fetchone()


def _recount(l: dict) -> None:
    seen = l.get("findings") or {}
    l["hits"] = len(seen)
    l["dismissals"] = sum(s == "rejected" for s in seen.values())
    l["accepted"] = sum(s == "accepted" for s in seen.values())
    if l.get("status", "active") == "active" and l["accepted"] >= RETIRE_AFTER and l["accepted"] > l["dismissals"]:
        l["status"] = "retired"
        l["retired"] = (f"{now()[:10]}: people accepted {l['accepted']} findings it matched and rejected"
                        f" {l['dismissals']}, so the decision no longer holds.")


def best_match(con, reviewer: str, claim: str, evidence: list[str], skip=()) -> Optional[tuple]:
    """The active learning a new finding repeats: the same kind of review, close code, and a claim saying much the
    same thing. (file, learning, level, similarity), or None. Learnings in `skip` are not compared."""
    repos = _repos(con)
    best = None
    for path, items in _all(con).items():
        here = scope_of(con, _in_repo(con, evidence, repos.get(path)))
        for l in items:
            if l.get("status", "active") != "active" or l.get("reviewer") != reviewer or l["id"] in skip:
                continue
            level = overlap(l.get("scope") or {}, here)
            if level is None:
                continue
            # The code both are about is why they were compared at all; what is left is the worry itself.
            scope = l.get("scope") or {}
            names = short_names([*here["nodes"], *here["types"], *scope.get("nodes", []), *scope.get("types", [])])
            sim = similarity(claim, l.get("claim", ""), names)
            if sim < (SAME_MODULE if level == "module" else SAME_CODE):
                continue
            key = (-LEVELS.index(level), sim)
            if best is None or key > best[0]:
                best = (key, path, l, level, sim)
    return best[1:] if best else None


def on_finding(con, fid: str, reviewer: str, claim: str, evidence: list[str]) -> dict:
    """After a finding is filed: when it repeats a past decision, count the hit and say which decision. The finding
    is kept either way; the person decides whether the decision still holds."""
    try:
        row = _finding(con, fid)
        m = best_match(con, reviewer, claim, evidence, from_the_change(con, row["change_id"] if row is not None else None))
        if m is None:
            return {}
        path, l, level, sim = m
        items = _read(path, strict=True)
        mine = next((x for x in items if x["id"] == l["id"]), None)
        if mine is None:
            return {}
        if (mine.get("source") or {}).get("finding") == fid:
            return {}   # the finding the learning came from, filed again
        seen = mine.setdefault("findings", {})
        if fid not in seen:
            row = _finding(con, fid)
            seen[fid] = row["status"] if row is not None else "open"
            _recount(mine)
            _write(path, items)
        code = _checked(con, _repos(con), path, mine)
        note = f"This matches a past decision ({mine['id']}): {mine.get('reason', '')} Kept, and marked on the page"
        if code["stale"]:
            note += (f", with a warning: the code it was about has changed since"
                     f" ({changed_text(code['edited'], code['gone'])}). Whether the decision still holds is a question"
                     " for the person: do not drop or refile the finding for it.")
        else:
            note += "; refile only if the code changed in a way that decision did not cover."
            if code["code"] == "unknown":
                note += " Whether its code changed since is not known: it was kept before Leyline recorded that."
        return {"learned": {"id": mine["id"], "reason": mine.get("reason", ""), "claim": mine.get("claim", ""),
                            "close_on": level, "similarity": round(sim, 2), **code, "note": note}}
    except OSError as e:
        return {"learned_error": f"could not read or write the learnings file: {e}"}


def on_resolve(con, finding_id: str, status: str, resolution: str) -> dict:
    """After a person decides a finding: count the decision against any learning the finding matched, and keep a
    new learning when it is rejected with a reason and repeats none."""
    row = _finding(con, finding_id)
    if row is None:
        return {}
    out = {}
    matched = False
    try:
        for path, items in _all(con).items():
            changed = False
            for l in items:
                if finding_id in (l.get("findings") or {}):
                    l["findings"][finding_id] = status
                    _recount(l)
                    changed = matched = True
                    out["learning"] = l["id"]
                elif (l.get("source") or {}).get("finding") == finding_id:
                    matched = changed = True
                    out["learning"] = l["id"]
                    if status == "rejected" and resolution.strip():
                        l["reason"] = resolution.strip()
                    elif status != "rejected" and l.get("status", "active") == "active":
                        l["status"] = "retired"
                        l["retired"] = f"{now()[:10]}: the finding it came from was marked {status} instead."
            if changed:
                _write(path, items)
        if matched or status != "rejected":
            return out
        if not resolution.strip():
            return {"learning_note": "No learning was kept: give a reason with the rejection, so later reviews can"
                                     " use it."}
        evidence = json.loads(row["evidence"] or "[]")
        repos = {r["repo_id"] for r in con.execute(
            f"SELECT repo_id FROM nodes WHERE id IN ({','.join('?' * len(evidence))})", evidence)} if evidence else set()
        roots = _roots(con)
        root = next((roots[r] for r in sorted(repos) if r in roots), None)
        if root is None:
            return {"learning_note": "No learning was kept: the repository the evidence is in was not found."}
        path = path_for(root)
        items = _read(path, strict=True)
        lid = "l-" + hashlib.sha1(f"{finding_id}|{row['claim']}".encode()).hexdigest()[:6]
        items = [x for x in items if x["id"] != lid]
        scope = scope_of(con, evidence)
        repo = next((r for r in sorted(repos) if roots.get(r) == root), None)
        items.append({"id": lid, "status": "active", "created": now(), "reviewer": row["reviewer"],
                      "claim": row["claim"], "reason": resolution.strip(), "scope": scope,
                      "fingerprint": fingerprint(con, repo, scope["nodes"]),
                      "source": {"change": row["change_id"], "finding": finding_id},
                      "hits": 0, "dismissals": 0, "accepted": 0, "findings": {}})
        _write(path, items)
        return {"learning": lid, "learning_file": str(path)}
    except OSError as e:
        return {"learning_note": f"No learning was kept: could not write the learnings file ({e})."}


def by_finding(con, change_id: Optional[str] = None) -> dict:
    """{finding id: the learning it matched}, for the pages; for a pull request, not the learnings it brings."""
    out, repos = {}, _repos(con)
    skip = from_the_change(con, change_id)
    for path, items in _all(con).items():
        for l in items:
            if not l.get("findings") or l["id"] in skip:
                continue
            code = _checked(con, repos, path, l)
            for fid in l["findings"]:
                out[fid] = {"id": l["id"], "reason": l.get("reason", ""), "status": l.get("status", "active"), **code}
    return out


def _marks(con, change_id: str) -> list[str]:
    row = con.execute("SELECT spec FROM views WHERE id = ?", ("view-" + change_id,)).fetchone()
    return [m["id"] for m in json.loads(row[0] or "{}").get("marks", [])] if row else []


def applying(con, change_id: str, nodes: Optional[list[str]] = None, limit: int = 20) -> list[dict]:
    """Active learnings about code the change touches or reaches, closest first: what reviewers read before filing."""
    try:
        ids = list(dict.fromkeys([*(nodes or []), *_marks(con, change_id)]))
        found, repos = [], _repos(con)
        skip = from_the_change(con, change_id)   # a pull request's own learnings: listed apart, not applied
        for path, items in _all(con).items():
            here = scope_of(con, _in_repo(con, ids, repos.get(path)))
            for l in items:
                if l.get("status", "active") != "active" or l["id"] in skip:
                    continue
                level = overlap(l.get("scope") or {}, here)
                if level:
                    found.append({"id": l["id"], "reviewer": l.get("reviewer"), "claim": l.get("claim"),
                                  "reason": l.get("reason"), "close_on": level,
                                  "where": (l.get("scope") or {}).get("paths", []),
                                  "matched_since": l.get("hits", 0), "rejected_again": l.get("dismissals", 0),
                                  **_checked(con, repos, path, l)})
        found.sort(key=lambda x: (LEVELS.index(x["close_on"]), x["id"]))
        return found[:limit]
    except OSError:
        return []


# -- listing and retiring ------------------------------------------------------------------------
def listing(con) -> dict:
    files, repos = _all(con), _repos(con)
    items = [{**l, "file": str(p), **_checked(con, repos, p, l)} for p, xs in files.items() for l in xs]
    items.sort(key=lambda l: (l.get("status", "active") != "active", l.get("created", ""), l["id"]))
    where = [str(path_for(r)) for r in _roots(con).values()]
    active = [l for l in items if l.get("status", "active") == "active"]
    return {"active": len(active), "stale": sum(l["stale"] for l in active), "learnings": items,
            "files": [str(p) for p in files] or where, "problems": _unreadable(con)}


def confirm(con, lid: str) -> dict:
    """A person's call that a learning still holds for the code as it is now: its fingerprint is taken again, so it
    is no longer stale. Its status is left as it is."""
    repos = _repos(con)
    for path, items in _all(con).items():
        for l in items:
            if l["id"] == lid:
                was = _checked(con, repos, path, l)["code"]
                l["fingerprint"] = fingerprint(con, repos.get(path), (l.get("scope") or {}).get("nodes", []))
                l["confirmed"] = now()
                _write(path, items)
                gone = sorted(n for n, h in l["fingerprint"].items() if h is None)
                return {"id": lid, "status": l.get("status", "active"), "file": str(path), "was": was,
                        **({"not_in_map": gone} if gone else {})}
    return _not_found(con, lid)


def _not_found(con, lid: str) -> dict:
    problems = _unreadable(con)
    return {"error": f"no learning {lid!r}" + (f" in the files that can be read: {'; '.join(problems)}" if problems else
                                               "; `leyline learnings` lists them")}


def retire(con, lid: str, why: str = "") -> dict:
    """A person's call that a learning no longer holds. It stays in the file, marked retired, and stops applying."""
    for path, items in _all(con).items():
        for l in items:
            if l["id"] == lid:
                l["status"] = "retired"
                l["retired"] = f"{now()[:10]}: retired by hand" + (f": {why.strip()}" if why.strip() else ".")
                _write(path, items)
                return {"id": lid, "status": "retired", "file": str(path)}
    return _not_found(con, lid)


def text(r: dict) -> str:
    problems = [f"Left out: {p}." for p in r.get("problems") or []]
    if problems and not r["learnings"]:
        return "\n".join(problems)
    if not r["learnings"]:
        return ("No learnings yet. One is kept when a person rejects a review finding with a reason"
                " (`leyline spec resolve <finding> rejected \"why\"`). They go in " + " or ".join(r["files"] or [FILE_AT_ROOT]) + ".")
    L = [f"{r['active']} active of {len(r['learnings'])}"
         + (f" ({r['stale']} about code that has changed since)" if r.get("stale") else "")
         + ", in " + ", ".join(r["files"]) + ".", *problems, ""]
    for l in r["learnings"]:
        where = ", ".join((l.get("scope") or {}).get("paths", [])[:3]) or "?"
        L.append(f"{l['id']}  {l.get('status', 'active'):<7} {l.get('reviewer', '')}, {where}")
        L.append(f"    Claim: {l.get('claim', '')}")
        L.append(f"    Decided: {l.get('reason', '')}")
        L.append(f"    Matched {l.get('hits', 0)} later findings: {l.get('dismissals', 0)} rejected, {l.get('accepted', 0)} accepted.")
        if l.get("retired"):
            L.append(f"    Retired {l['retired']}")
        elif l.get("code") == "changed":
            L.append(f"    Stale: the code it was about has changed since: {changed_text(l['edited'], l['gone'])}."
                     f" Ask whether it still holds: `leyline learnings confirm {l['id']}` if it does, retire it if not.")
        elif l.get("code") == "unknown":
            L.append("    Code: not recorded (kept before Leyline did that), so whether it changed is not known."
                     f" `leyline learnings confirm {l['id']}` records it now.")
    return "\n".join(L)


def cli(con, args) -> int:
    """`leyline learnings` lists them; `leyline learnings retire <id> ["why"]` retires one; `leyline learnings
    confirm <id>` says one still holds for the code as it is now."""
    if args.action == "confirm":
        if not args.id:
            print("leyline: say which learning: leyline learnings confirm <id>")
            return 2
        r = confirm(con, args.id)
        if "error" in r:
            print(r["error"])
            return 1
        print(f"Confirmed {r['id']} against the code as it is now, in {r['file']}."
              + (f" Not in the map now: {', '.join(r['not_in_map'])}." if r.get("not_in_map") else ""))
        return 0
    if args.action == "retire":
        if not args.id:
            print("leyline: say which learning: leyline learnings retire <id> \"why\"")
            return 2
        r = retire(con, args.id, args.why or "")
        print(r.get("error") or f"Retired {r['id']} in {r['file']}.")
        return 1 if "error" in r else 0
    r = listing(con)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        print(text(r))
    return 1 if r["problems"] else 0   # a file left out is a failure to read, not an empty list
