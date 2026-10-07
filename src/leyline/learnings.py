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
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from . import store

FILE_IN_OPENSPEC = "openspec/leyline-learnings.json"
FILE_AT_ROOT = ".leyline-learnings.json"
ABOUT = ("Decisions people made on Leyline review findings: each rejected finding, with the reason in the person's"
         " words and the code it is about. Reviewers read the active ones first. Commit this file.")
# Word overlap (Jaccard) a finding's claim needs with a learning's claim to match it: closer code needs less.
SAME_CODE = 0.4    # the same node, a node in the same type, or the same file
SAME_MODULE = 0.6  # only the same module
RETIRE_AFTER = 2   # accepted findings a learning matched, more than it matched rejected ones, before it is retired
LEVELS = ("node", "type", "file", "module")

STOP = set("""a an the and or but nor of to in on at by for with from into onto over under is are was were be been being
it its it's this that these those as if then than so no not still can could will would should may might must do does
did done has have had having when which who whom what where there here also only any all each every more less very just
same other without about after before because while yet such some their them they he she we you your our his her
one ever never get gets got via per""".split())
GENERIC = {"call", "caller", "function", "method"}   # in nearly every claim about code, so they say nothing


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


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("learnings") if isinstance(data, dict) else None
    return [x for x in items if isinstance(x, dict) and x.get("id")] if isinstance(items, list) else []


def _write(path: Path, items: list[dict]) -> None:
    """Canonical JSON (sorted keys, two-space indent, UTF-8 as is, one newline at the end), written whole and then
    moved into place, so a reader never sees half a file."""
    items = sorted(({k: v for k, v in x.items() if not k.startswith("_")} for x in items),
                   key=lambda x: (x.get("created", ""), x["id"]))
    body = json.dumps({"about": ABOUT, "learnings": items}, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)   # a temp file is private; this is not
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _roots(con) -> dict:
    return {repo: root for repo, root in store.roots(con).items() if root.is_dir()}


def _all(con) -> dict:
    """{file: its learnings} for every mapped repository that has a learnings file."""
    out = {}
    for root in _roots(con).values():
        p = path_for(root)
        if p.is_file():
            out[p] = _read(p)
    return out


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


# -- what a claim says ---------------------------------------------------------------------------
def words(text: str) -> set:
    """A claim's words, compared loosely: code names split (`EntryQueues` is entry and queue), lower case, common
    words dropped, and plural and tense endings cut."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP or len(w) < 2:
            continue
        w = _stem(w)
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


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of two claims' words: 0 shares nothing, 1 is the same words."""
    x, y = words(a), words(b)
    return len(x & y) / len(x | y) if x and y else 0.0


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


def best_match(con, reviewer: str, claim: str, evidence: list[str]) -> Optional[tuple]:
    """The active learning a new finding repeats: the same kind of review, close code, and a claim saying much the
    same thing. (file, learning, level, similarity), or None."""
    here = scope_of(con, evidence)
    best = None
    for path, items in _all(con).items():
        for l in items:
            if l.get("status", "active") != "active" or l.get("reviewer") != reviewer:
                continue
            level = overlap(l.get("scope") or {}, here)
            if level is None:
                continue
            sim = similarity(claim, l.get("claim", ""))
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
        m = best_match(con, reviewer, claim, evidence)
        if m is None:
            return {}
        path, l, level, sim = m
        items = _read(path)
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
        return {"learned": {"id": mine["id"], "reason": mine.get("reason", ""), "claim": mine.get("claim", ""),
                            "close_on": level, "similarity": round(sim, 2),
                            "note": f"This matches a past decision ({mine['id']}): {mine.get('reason', '')} Kept, and"
                                    " marked on the page; refile only if the code changed in a way that decision"
                                    " did not cover."}}
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
        items = _read(path)
        lid = "l-" + hashlib.sha1(f"{finding_id}|{row['claim']}".encode()).hexdigest()[:6]
        items = [x for x in items if x["id"] != lid]
        items.append({"id": lid, "status": "active", "created": now(), "reviewer": row["reviewer"],
                      "claim": row["claim"], "reason": resolution.strip(), "scope": scope_of(con, evidence),
                      "source": {"change": row["change_id"], "finding": finding_id},
                      "hits": 0, "dismissals": 0, "accepted": 0, "findings": {}})
        _write(path, items)
        return {"learning": lid, "learning_file": str(path)}
    except OSError as e:
        return {"learning_note": f"No learning was kept: could not write the learnings file ({e})."}


def by_finding(con) -> dict:
    """{finding id: the learning it matched}, for the pages."""
    out = {}
    for items in _all(con).values():
        for l in items:
            for fid in l.get("findings") or {}:
                out[fid] = {"id": l["id"], "reason": l.get("reason", ""), "status": l.get("status", "active")}
    return out


def _marks(con, change_id: str) -> list[str]:
    row = con.execute("SELECT spec FROM views WHERE id = ?", ("view-" + change_id,)).fetchone()
    return [m["id"] for m in json.loads(row[0] or "{}").get("marks", [])] if row else []


def applying(con, change_id: str, nodes: Optional[list[str]] = None, limit: int = 20) -> list[dict]:
    """Active learnings about code the change touches or reaches, closest first: what reviewers read before filing."""
    try:
        here = scope_of(con, list(dict.fromkeys([*(nodes or []), *_marks(con, change_id)])))
        found = []
        for items in _all(con).values():
            for l in items:
                if l.get("status", "active") != "active":
                    continue
                level = overlap(l.get("scope") or {}, here)
                if level:
                    found.append({"id": l["id"], "reviewer": l.get("reviewer"), "claim": l.get("claim"),
                                  "reason": l.get("reason"), "close_on": level,
                                  "where": (l.get("scope") or {}).get("paths", []),
                                  "matched_since": l.get("hits", 0), "rejected_again": l.get("dismissals", 0)})
        found.sort(key=lambda x: (LEVELS.index(x["close_on"]), x["id"]))
        return found[:limit]
    except OSError:
        return []


# -- listing and retiring ------------------------------------------------------------------------
def listing(con) -> dict:
    files = _all(con)
    items = [{**l, "file": str(p)} for p, xs in files.items() for l in xs]
    items.sort(key=lambda l: (l.get("status", "active") != "active", l.get("created", ""), l["id"]))
    where = [str(path_for(r)) for r in _roots(con).values()]
    return {"active": sum(l.get("status", "active") == "active" for l in items), "learnings": items,
            "files": [str(p) for p in files] or where}


def retire(con, lid: str, why: str = "") -> dict:
    """A person's call that a learning no longer holds. It stays in the file, marked retired, and stops applying."""
    for path, items in _all(con).items():
        for l in items:
            if l["id"] == lid:
                l["status"] = "retired"
                l["retired"] = f"{now()[:10]}: retired by hand" + (f": {why.strip()}" if why.strip() else ".")
                _write(path, items)
                return {"id": lid, "status": "retired", "file": str(path)}
    return {"error": f"no learning {lid!r}; `leyline learnings` lists them"}


def text(r: dict) -> str:
    if not r["learnings"]:
        return ("No learnings yet. One is kept when a person rejects a review finding with a reason"
                " (`leyline spec resolve <finding> rejected \"why\"`). They go in " + " or ".join(r["files"] or [FILE_AT_ROOT]) + ".")
    L = [f"{r['active']} active of {len(r['learnings'])}, in " + ", ".join(r["files"]) + ".", ""]
    for l in r["learnings"]:
        where = ", ".join((l.get("scope") or {}).get("paths", [])[:3]) or "?"
        L.append(f"{l['id']}  {l.get('status', 'active'):<7} {l.get('reviewer', '')}, {where}")
        L.append(f"    Claim: {l.get('claim', '')}")
        L.append(f"    Decided: {l.get('reason', '')}")
        L.append(f"    Matched {l.get('hits', 0)} later findings: {l.get('dismissals', 0)} rejected, {l.get('accepted', 0)} accepted.")
        if l.get("retired"):
            L.append(f"    Retired {l['retired']}")
    return "\n".join(L)


def cli(con, args) -> int:
    """`leyline learnings` lists them; `leyline learnings retire <id> ["why"]` retires one."""
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
    return 0
