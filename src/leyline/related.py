"""Earlier changes to the same code: what was decided there is worth reading before changing it again.

`leyline plan` and `leyline pr` list up to five earlier changes that touched the code a change touches, newest first,
each with its id, title, date and the names they share. Two kinds of earlier change are indexed by the nodes they
touched:

- OpenSpec changes that are finished: archived (`openspec/changes/archive/<date>-<id>/`), or checked (`leyline check`
  ran on them). Their nodes come from the store (the tasks' code, `spec_items`; the code their check found edited,
  the review view's marks; the code they planned to change, the plan view's marks), from the anchors `check` keeps
  in `openspec/leyline-anchors.json`, and, for a folder the store knows nothing of, from the code its tasks and
  spec deltas name.
- Pull requests reviewed with `leyline pr`: the code each one edited and added (its view's marks).

A repository with neither falls back on git history, the cheap way: the commits before the change (at most 500, merges
and bulk commits left out) that changed the files the change touches, the most such files first. A commit's line
numbers are those of its own time, so placing its hunks in today's functions would mean parsing each file as it
was; files are what it can say for sure.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

from . import store

LIMIT = 5
MAX_COMMITS = 500
BULK = 50           # a commit that changed more files than this is a bulk change (a formatter, a rename), left out
CHANGED_ROLES = ("changed", "new", "edited as predicted", "new, as declared", "edited, not predicted")


def _near(a: str, b: str) -> bool:
    return a == b or b.startswith((a + ".", a + "(", a + "/")) or a.startswith((b + ".", b + "(", b + "/"))


def _marks(con, view_id: str) -> list[str]:
    row = con.execute("SELECT spec FROM views WHERE id = ?", (view_id,)).fetchone()
    if not row:
        return []
    try:
        return [m["id"] for m in json.loads(row[0] or "{}").get("marks", []) if m.get("role") in CHANGED_ROLES]
    except (ValueError, TypeError, KeyError):
        return []


def _view_date(con, view_id: str) -> str:
    row = con.execute("SELECT created FROM views WHERE id = ?", (view_id,)).fetchone()
    return (row[0] or "")[:10] if row else ""


def _stored_nodes(con, cid: str) -> set:
    """What the store says a spec change touched: its tasks' code, and the code its plan and check marked."""
    out = set()
    for r in con.execute("SELECT nodes, attrs FROM spec_items WHERE change_id = ? AND kind = 'task'", (cid,)):
        try:
            out |= set(json.loads(r[0] or "[]"))
            out |= set((json.loads(r[1] or "{}") or {}).get("into") or [])
        except ValueError:
            continue
    out |= set(_marks(con, "view-review-" + cid)) | set(_marks(con, "view-" + cid))
    return out


def _full(names, repos: list[str], local: str) -> Optional[str]:
    """An anchor's node id, written without the repository's id, as an id on this map."""
    if local in names.by_id:
        return local
    return next((f"{r}:{local}" for r in repos if f"{r}:{local}" in names.by_id), None)


def _named(names, folder: Path) -> set:
    """The code a change folder's tasks and spec deltas name, resolved on the map as it is now."""
    from . import drift
    out = set()
    try:
        for w in drift._change_names(folder):
            out |= set(names.resolve(w).get("ids") or [])
    except (OSError, ValueError):
        pass
    return out


def _verdict(folder: Path) -> Optional[str]:
    """The date `leyline check` last wrote a verdict on this folder's page, or None."""
    page = folder / "leyline.md"
    try:
        if "## 4. Was it done as agreed" in page.read_text(encoding="utf-8", errors="replace"):
            return datetime.date.fromtimestamp(page.stat().st_mtime).isoformat()
    except OSError:
        pass
    return None


def index(con, names) -> list[dict]:
    """Every finished OpenSpec change and every reviewed pull request, with the nodes each touched."""
    from . import drift, spec
    repos = list(store.roots(con))
    out: dict[str, dict] = {}
    for osp in drift.places(con):
        anchors, _ = drift.load(con, osp)
        changes = osp / "changes"
        folders = []
        if changes.is_dir():
            folders += [(f, "checked") for f in sorted(changes.iterdir()) if f.is_dir() and f.name != "archive"]
        if (changes / "archive").is_dir():
            folders += [(f, "archived") for f in sorted((changes / "archive").iterdir()) if f.is_dir()]
        for folder, state in folders:
            key = drift.DATED.sub("", folder.name)
            cid = "spec-" + key
            row = con.execute("SELECT attrs, intent FROM change_proposals WHERE id = ?", (cid,)).fetchone()
            attrs = json.loads(row[0] or "{}") if row else {}
            if state == "archived":
                date = folder.name[:10] if drift.DATED.match(folder.name) else (attrs.get("verified") or "")[:10]
            else:
                date = (attrs.get("verified") or "")[:10] or _verdict(folder)
                if not date:
                    continue   # still being worked on: not an earlier change
            nodes = _stored_nodes(con, cid)
            nodes |= {i for a in (anchors.get(key) or {}).get("anchors", []) if (i := _full(names, repos, a.get("node", "")))}
            if not nodes:
                nodes = _named(names, folder)
            try:
                title = spec.parse(folder).get("title") or key
            except OSError:
                title = attrs.get("title") or key
            out[cid] = {"id": key, "change_id": cid, "kind": state, "title": title, "date": date or "", "nodes": nodes,
                        "where": str(folder)}
    for r in con.execute("SELECT id, intent, attrs FROM change_proposals"):
        attrs = json.loads(r["attrs"] or "{}")
        if attrs.get("kind") != "pr" or r["id"] in out:
            continue
        from . import rereview
        try:
            date = (rereview.last_reviewed(con, r["id"]) or "")[:10]
        except Exception:
            date = ""
        out[r["id"]] = {"id": r["id"], "change_id": r["id"], "kind": "review", "title": attrs.get("title") or (r["intent"] or "")[:80],
                        "date": date or _view_date(con, "view-" + r["id"]), "nodes": set(_marks(con, "view-" + r["id"]))}
    return [x for x in out.values() if x["nodes"]]


def _label(names, i: str) -> str:
    from . import spec
    return spec._label(names, i) if i in names.by_id else i.rsplit(":", 1)[-1]


def overlap(names, current: list[str], past: set) -> list[str]:
    """The names of the code a change touches that an earlier change touched too: the same node, one inside the
    other (a method of a type it changed), or a function in a file it changed."""
    files = {p for p in past if (names.by_id.get(p) or {"kind": ""})["kind"] == "file"}
    hits = []
    for c in current:
        row = names.by_id.get(c)
        if any(_near(c, p) for p in past) or (row is not None and files and
                                              any(f for f in files if names.by_id[f]["path"] == row["path"])):
            hits.append(_label(names, c))
    return list(dict.fromkeys(hits))


def find(con, names, current: list[str], exclude: Optional[str] = None, since: Optional[str] = None,
         limit: int = LIMIT) -> dict:
    """Up to `limit` earlier changes to the code a change touches. {"source", "about", "items", "total"}, or {}."""
    current = [c for c in dict.fromkeys(current) if c]
    if not current:
        return {}
    try:
        found = []
        for x in index(con, names):
            if x["change_id"] == exclude:
                continue
            shared = overlap(names, current, x["nodes"])
            if shared:
                found.append({k: v for k, v in x.items() if k not in ("nodes", "change_id")} | {"overlap": shared})
        if found:
            found.sort(key=lambda x: (x["date"], len(x["overlap"])), reverse=True)
            return {"source": "openspec", "about": "finished OpenSpec changes and earlier reviews that touched the same code,"
                                                   " newest first", "items": found[:limit], "total": len(found)}
        return from_git(con, names, current, since, limit)
    except Exception:   # a lead, never a reason for the plan or the review to fail
        return {}


def _ere(name: str) -> str:
    """A name as a whole word in a POSIX extended regular expression (what `git log -G` reads)."""
    body = "".join("\\" + ch if ch in ".^$*+?()[]{}|\\" else ch for ch in name)
    return f"(^|[^A-Za-z0-9_$]){body}([^A-Za-z0-9_$]|$)"


def _log(root: Path, since: Optional[str], paths: list[str], *extra: str) -> list[tuple[str, str, str, list[str]]]:
    """(sha, date, subject, files) of the commits before `since` that changed `paths`, newest first; bulk commits
    left out."""
    from .coupling import _git
    out = _git(root, "-c", "core.quotePath=false", "log", "--no-merges", "--full-diff", "--relative", "--name-only",
               f"-n{MAX_COMMITS}", "--format=%x01%H%x02%cs%x02%s", *extra, since or "HEAD", "--", *paths, timeout=60)
    found = []
    for chunk in (out or b"").decode("utf-8", "replace").split("\x01")[1:]:
        head, _, body = chunk.partition("\n")
        sha, date, subject = (head.split("\x02") + ["", ""])[:3]
        files = [f for f in body.splitlines() if f.strip()]
        if len(files) <= BULK:
            found.append((sha, date, subject.strip(), files))
    return found


def from_git(con, names, current: list[str], since: Optional[str] = None, limit: int = LIMIT) -> dict:
    """The commits before the change that changed the code it touches: first those whose diff, in a changed
    function's file, adds or removes a line naming that function (`git log -G`), then those that changed its files."""
    by_repo: dict[str, set] = {}
    fns: dict[str, list] = {}
    for c in current:
        row = names.by_id.get(c)
        if row is None or not row["path"]:
            continue
        repo = c.split(":", 1)[0]
        by_repo.setdefault(repo, set()).add(row["path"])
        if row["kind"] in ("callable", "test", "type") and row["name"] not in ("<module>", "<top-level>") and len(row["name"]) >= 3:
            fns.setdefault(repo, []).append((row["path"], row["name"], _label(names, c)))
    commits: dict = {}
    for repo, root in store.roots(con).items():
        paths = sorted(by_repo.get(repo, set()))[:100]
        if not paths or not Path(root).is_dir():
            continue
        mine = set(paths)
        for sha, date, subject, files in _log(Path(root), since, paths):
            shared = [f for f in files if f in mine]
            if shared:
                commits[(repo, sha)] = {"id": sha[:7], "kind": "commit", "title": subject, "date": date, "files": shared,
                                        "names": [], **({"repo": repo} if len(by_repo) > 1 else {})}
        for path, name, label in list(dict.fromkeys(fns.get(repo, [])))[:20]:   # a few calls, each on one file's history
            for sha, *_ in _log(Path(root), since, [path], "-G", _ere(name)):
                if (repo, sha) in commits and label not in commits[(repo, sha)]["names"]:
                    commits[(repo, sha)]["names"].append(label)
    if not commits:
        return {}
    ranked = sorted(commits.values(), key=lambda x: (len(x["names"]), len(x["files"]), x["date"]), reverse=True)
    found = [{k: v for k, v in x.items() if k not in ("files", "names")}
             | {"overlap": x["names"] or x["files"], "level": "function" if x["names"] else "file"} for x in ranked]
    return {"source": "git", "about": "no OpenSpec history on this code; from git history: commits before it whose diff"
                                      " names the same functions, then those that changed the same files (of the last"
                                      f" {MAX_COMMITS} that touched them)",
            "items": found[:limit], "total": len(found)}


def lines(r: Optional[dict], header: str = "Earlier changes to this code") -> list[str]:
    """The page's list, empty when there is nothing to say."""
    if not r or not r.get("items"):
        return []
    L = ["", f"{header} ({r['about']}):", ""]
    for x in r["items"]:
        when = {"archived": "archived ", "checked": "checked ", "review": "reviewed "}.get(x["kind"], "") + (x["date"] or "undated")
        what = ("Changed the same file:" if len(x["overlap"]) == 1 else "Changed the same files:") if x.get("level") == "file" \
            else "Changed" if x["kind"] == "commit" else "Touched"
        shared = ", ".join(f"`{n}`" for n in x["overlap"][:4]) + (f" and {len(x['overlap']) - 4} more" if len(x["overlap"]) > 4 else "")
        title = x["title"].rstrip(".") + "." if x["title"] else ""
        L.append(f"- `{x['id']}` ({when}): {title} {what} {shared}.".replace("  ", " "))
    if r["total"] > len(r["items"]):
        L.append(f"- and {r['total'] - len(r['items'])} more.")
    return L
