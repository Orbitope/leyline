"""Review a change someone else made: a branch or a pull request, with no spec behind it.

A spec says what a change will do before it is written, and `check` compares the code with it. A pull request
arrives the other way round: the code is written, and what it was meant to do is a title and a paragraph. This
module reads the change from the code itself, by mapping the commit the branch started from and comparing it with
the checkout, and then asks of the edits what `plan` asks of a spec's tasks: what they reach and did not change,
which channels they cross, which shared state they write, which tests run them. The result is a page a person can
read in a minute and the same facts arranged for the adversarial reviewers, whose findings are kept like a spec's.

    leyline pr main                         # the checkout against where it left main
    leyline pr main --about "Fix the retry"  # with what the change says it does
    leyline pr --github 123                 # base, title and description from GitHub (needs gh, and the PR checked out)
    leyline pr main --gate                  # exit 1 while something that blocks is left ([pr] in openspec/leyline.toml)

The base is mapped once per commit, from `git archive` into a temporary directory (the repository and its
worktrees are not touched), reusing the parse output of the checkout's map for every file that did not change.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import change, diff, rules, spec, store

HOUSE_RULES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", ".github/copilot-instructions.md", ".cursorrules",
               "docs/CONTRIBUTING.md", "ARCHITECTURE.md", "docs/ARCHITECTURE.md")


CONTEXT = 12   # lines above an edited line that still count as what the edit touches
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|specs?)(/|$)|[._-](test|spec)s?\.[A-Za-z]+$|(^|/)test_[^/]+$|Tests?\.cs$")


class GitError(RuntimeError):
    pass


def _git(root: Path, *args: str, binary: bool = False):
    try:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    except FileNotFoundError:
        raise GitError("git is not on the PATH")
    if out.returncode != 0:
        raise GitError(out.stderr.decode("utf-8", errors="replace").strip() or f"git {' '.join(args)} failed")
    return out.stdout if binary else out.stdout.decode("utf-8", errors="replace").strip()


def git_root(path: Path) -> Path:
    return Path(_git(path, "rev-parse", "--show-toplevel")).resolve()


def github_pr(root: Path, number: str) -> dict:
    """Base branch, title, description and head commit of a GitHub pull request, through the gh command."""
    try:
        out = subprocess.run(["gh", "pr", "view", str(number), "--json", "baseRefName,title,body,headRefOid,number,url"],
                             cwd=root, capture_output=True, check=False)
    except FileNotFoundError:
        raise GitError("--github needs the gh command (https://cli.github.com), signed in")
    if out.returncode != 0:
        raise GitError(out.stderr.decode("utf-8", errors="replace").strip() or "gh pr view failed")
    return json.loads(out.stdout)


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "change"


def change_id(root: Path, given: Optional[str] = None, number: Optional[str] = None) -> str:
    """pr-<given>, pr-<number>, or pr-<branch name>; on a detached checkout, pr-<commit>."""
    if given:
        return "pr-" + slug(given.removeprefix("pr-"))
    if number:
        return f"pr-{number}"
    try:
        branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    except GitError:
        branch = ""
    return "pr-" + slug(branch or _git(root, "rev-parse", "--short", "HEAD"))


# -- the two maps ----------------------------------------------------------------------------------------
def _repo_of(con, root: Path) -> Optional[str]:
    for rid, where in store.roots(con).items():
        try:
            if Path(where).resolve() == root:
                return rid
        except OSError:
            continue
    return None


def _safe_members(tar: tarfile.TarFile, into: Path):
    """The archive's entries that stay inside `into`: a branch under review is someone else's code, and an entry
    named ../x, or a link out of the tree followed by a file written through it, must not reach outside."""
    top = into.resolve()
    for m in tar.getmembers():
        dest = (top / m.name).resolve()
        if m.name.startswith("/") or (dest != top and top not in dest.parents):
            continue
        if m.issym() or m.islnk():
            target = (dest.parent / m.linkname).resolve() if m.issym() else (top / m.linkname).resolve()
            if m.linkname.startswith("/") or (target != top and top not in target.parents):
                continue
        if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
            continue
        yield m


def _archive(root: Path, sha: str) -> bytes:
    """`git archive` of a commit with every file in it as the checkout has it. The repository's `export-ignore` and
    `export-subst` attributes shape release tarballs (tests left out, a version string filled in), so the archive is
    made from an empty repository that borrows this one's objects and unsets both, at the highest precedence."""
    objects = (root / _git(root, "rev-parse", "--git-path", "objects")).resolve()
    with tempfile.TemporaryDirectory(prefix="leyline-archive-") as tmp:
        bare = Path(tmp)
        _git(bare, "init", "-q", "--bare")
        (bare / "objects" / "info").mkdir(parents=True, exist_ok=True)
        (bare / "objects" / "info" / "alternates").write_text(str(objects) + "\n")
        (bare / "info").mkdir(exist_ok=True)
        (bare / "info" / "attributes").write_text("* -export-ignore -export-subst\n")
        return _git(bare, "archive", "--format=tar", sha, binary=True)


def _export(root: Path, sha: str, into: Path) -> None:
    """The files of a commit, as git keeps them, written under `into`. Nothing in the repository changes."""
    data = _archive(root, sha)
    tmp = into.parent / (into.name + ".tar")
    tmp.write_bytes(data)
    try:
        with tarfile.open(tmp) as tar:
            members = list(_safe_members(tar, into))
            try:
                tar.extractall(into, members=members, filter="tar")
            except TypeError:   # no extraction filters before Python 3.12 (and 3.11.4, 3.10.12)
                tar.extractall(into, members=members)
            except tarfile.FilterError:   # a link the filter refuses: take the entries one by one, leaving those out
                for m in members:
                    try:
                        tar.extract(m, into, filter="tar")
                    except tarfile.FilterError:
                        pass
    finally:
        tmp.unlink()
    # A repository of its own holding exactly the commit's files, so the map lists them as git lists the checkout's
    # (a tracked file that a .gitignore pattern would match stays in), and not by walking the directory.
    _git(into, "init", "-q")
    _git(into, "add", "-A", "-f")


def base_snapshot(db: Path, root: Path, rid: str, base_sha: str, cid: str) -> Path:
    """The map of the base commit, kept as the change's baseline (the snapshot `check` compares with). Made once
    per base commit: a copy of the checkout's store is mapped again from the base's files, so every file that did
    not change keeps its parse output, and the result is cut down to what a comparison reads."""
    con = store.connect(db)
    try:
        target = diff.snapshot_path(con, cid)
    finally:
        con.close()
    from .incremental import code_version
    marker = target.with_suffix(".base")
    stamp = f"{base_sha} {code_version()}"   # a base mapped by another version of Leyline is mapped again
    if target.exists() and marker.exists() and marker.read_text().strip() == stamp:
        return target
    from .incremental import cache_path
    from .indexer import index
    work = Path(tempfile.mkdtemp(prefix="leyline-pr-"))
    try:
        src = work / rid           # the directory name is the repository's id in a workspace
        src.mkdir()
        _export(root, base_sha, src)
        mapped = work / "map" / "leyline.db"
        mapped.parent.mkdir()
        live = store.connect(db)   # a consistent copy of the store, even while something else reads it
        try:
            live.execute("VACUUM INTO ?", (str(mapped),))
            exact = (live.execute("SELECT value FROM meta WHERE key = 'exact'").fetchone() or ["auto"])[0]
        finally:
            live.close()
        if cache_path(db).exists():
            shutil.copyfile(cache_path(db), cache_path(mapped))
        con = store.connect(mapped)
        try:
            with con:   # paths kept relative to the store would now point near the copy
                con.execute("DELETE FROM meta WHERE key LIKE 'rel:%'")
        finally:
            con.close()
        index(src, mapped, rid, exact)
        con = store.connect(mapped)
        try:
            snap = diff.snapshot(con, "base")
        finally:
            con.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(snap), str(target))
        marker.write_text(stamp + "\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return target


# -- what changed, and what it reaches --------------------------------------------------------------------
def _inside(i: str, others: set) -> bool:
    """i is one of `others`, or nested in one (a method of a changed type)."""
    if i in others:
        return True
    return any(i.startswith(o + ".") or i.startswith(o + "/") for o in others)


def _house_rules(root: Path, paths: list[str]) -> list[str]:
    """Files a reviewer should read first: the repository's own rules for agents and contributors, at the top and
    in the directories the change touches."""
    out = [f for f in HOUSE_RULES if (root / f).is_file()]
    for p in sorted({str(Path(p).parent) for p in paths if p}):
        cur = Path(p)
        while str(cur) not in ("", "."):
            for name in ("AGENTS.md", "CLAUDE.md"):
                f = cur / name
                if (root / f).is_file() and str(f) not in out:
                    out.append(str(f))
            cur = cur.parent
    return out


def _address_words(address: str) -> set:
    """What an edited line says when it touches a channel: the address, a route without its method, the route up to
    its first parameter (`/api/entities` of `GET /api/entities/:type`), a file's name. A table, event or program
    name is matched whole."""
    words = {address}
    if "/" in address:
        path = address.split(" ", 1)[-1]
        words |= {path, re.split(r"/[:{<*]", path, maxsplit=1)[0].rstrip("/")}
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        if "." in leaf:
            words.add(leaf)
    return {w for w in words if len(w) >= 3}


def _names_format(address: str, text: str) -> bool:
    """An edited line touches a key format when it writes one of the format's fixed parts as a piece of a key
    (`/text`, `nodes/`) or as a word the reader compares (`"text"`)."""
    for w in re.split(r"[/:]", address):
        if len(w) < 2 or w == "*":
            continue
        e = re.escape(w)
        if re.search(rf"(?<![\w-]){e}(?=[/:])|[/:]{e}(?![\w-])|[\"'`]{e}[\"'`]", text):
            return True
    return False


def _params(text: str, start: int, name: str) -> Optional[str]:
    """The parameter list of a function whose text starts at line `start` (from 1): the brackets after its name,
    past any decorators, with the spacing made plain. None when it cannot be found."""
    lines = text.split("\n")[start - 1:start + 40]
    body = "\n".join(lines)
    name = (name or "").rsplit(".", 1)[-1]   # a method is named `Owner.name` on the map, and `name` in its text
    at = body.find(name) if name and name not in ("<module>", "<top-level>") else -1
    if at < 0:
        return None
    open_ = body.find("(", at)
    if open_ < 0 or "\n" in body[at:open_].strip("\n ") and body[at:open_].count("\n") > 2:
        return None
    depth = 0
    for k in range(open_, len(body)):
        ch = body[k]
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
            if depth == 0:
                return re.sub(r"\s+", " ", body[open_ + 1:k]).strip().rstrip(",")
    return None


def _touching(con, names, crossings: list, changed: set, own: Optional[dict], added: set):
    """A pull request, unlike a spec, shows the edited lines, so a channel counts as crossed only when the edit
    touches it: an edited line (or a new function's text) inside one of its ends names its address (the table, the
    route, the event, the program). A route whose handler is a function of its own is touched by any edit inside
    that function, and by no other. Of the other ends that must agree, a reader of data whose own read changed is
    told apart: the writer of that data need not change, but the reader must read what it writes.
    Returns the crossings kept, the other ends that must agree, and the cross-channel reads to check."""
    root_of = diff.roots(con)
    text_of: dict = {}

    def text(i):
        if i not in text_of:
            if i in added:
                row = con.execute("SELECT repo_id, path, span_start, span_end FROM nodes WHERE id = ?", (i,)).fetchone()
                data = diff.source(con, row["repo_id"], row["path"], root_of) if row and row["path"] else None
                lines = data.decode("utf-8", errors="replace").split("\n") if data else []
                text_of[i] = "\n".join(lines[(row["span_start"] or 1) - 1:row["span_end"] or len(lines)]) if lines else ""
            else:   # the edited lines, with the few above each: a route's handler is edited below its path
                changed_at = [ln for ln, _ in (own or {}).get(i) or []]
                row = con.execute("SELECT repo_id, path, span_start, span_end FROM nodes WHERE id = ?", (i,)).fetchone()
                data = diff.source(con, row["repo_id"], row["path"], root_of) if changed_at and row and row["path"] else None
                lines = data.decode("utf-8", errors="replace").split("\n") if data else []
                lo = (row["span_start"] or 1) if row else 1
                keep = sorted({k for ln in changed_at for k in range(max(lo, ln - CONTEXT), ln + 1)})
                text_of[i] = "\n".join(lines[k - 1] for k in keep if 0 < k <= len(lines))
        return text_of[i]

    def within(i, end):
        while i:
            if i == end:
                return True
            i = names.by_id[i]["parent_id"] if i in names.by_id else None
        return False

    def answers(end, address):
        """`end` is the handler of this route: its own function (an inline handler, or one given by name), so every
        line of it is what the route answers."""
        return any(a.get("channel") == "http" and a.get("handler") is True and a.get("address") == address
                   for a in (json.loads(r[0] or "{}") for r in con.execute(
                       "SELECT attrs FROM edges WHERE kind = 'communicates' AND dst_id = ?", (end,))))

    def touches(end, address, channel=""):
        inside = [i for i in changed if within(i, end)]
        if not inside:
            return False
        if not address or own is None or answers(end, address):
            return True
        if channel == "format":
            return any(_names_format(address, text(i)) for i in inside)
        words = _address_words(address)
        return any(w and w in text(i) for i in inside for w in words)
    kept, must, across = [], [], []
    for c in crossings:
        address = c.get("address") or ""
        hub = touches(c["hub"], address, c["channel"])
        moved = [sp for sp in c.get("spokes", []) if touches(sp["id"], address, c["channel"])]
        if not hub and not moved:
            continue
        what = f"{c['channel']} {address}".strip()
        guessed = bool(c.get("guessed"))
        kept.append({**c, "hub_touched": hub, "spokes_touched": [sp["name"] for sp in moved]})
        if hub:   # what the hub writes, sends or answers changed: every other end must agree
            for sp in c.get("spokes", []):
                if sp["changed"]:
                    continue
                must.append({"id": sp["id"], "name": sp["name"], "channel": c["channel"], "address": address,
                             "hub": c["hub_name"], "guessed": guessed, "why": (f"starts {c.get('program') or c['hub_name']}, whose input or output the edit changed"
                                                           if c["channel"] == "process"
                                                           else f"takes apart keys of the form {address}, which the edit to {c['hub_name']} changed"
                                                           if c["channel"] == "format"
                                                           else f"reads the {what} data, which the edit to {c['hub_name']} changed" if c["data"]
                                                           else f"calls {what}, whose answer the edit to {c['hub_name']} changed")})
        elif c["data"]:   # a read changed: the writer need not change, but the read must match what it writes
            across.append({"id": c["hub"], "name": c["hub_name"], "channel": c["channel"], "address": address,
                           "why": f"writes the {what} data the change now reads in {', '.join(sp['name'] for sp in moved)}:"
                                  " check it reads what is written"})
        else:             # a caller changed what it sends: the other end must accept it
            must.append({"id": c["hub"], "name": c["hub_name"], "channel": c["channel"], "address": address,
                         "hub": c["hub_name"], "guessed": guessed, "why": f"answers {', '.join(sp['name'] for sp in moved)} over {what}"})
    dedupe = lambda xs: list({x["id"]: x for x in xs}.values())
    return kept, dedupe(must), dedupe(across)


def analyse(con, snap: Path, about: str = "", old_source=None) -> dict:
    """The change between the base snapshot and the store as it is now, as facts a reviewer works from.
    `old_source(path)` gives a file's text at the base, to read what a changed declaration was."""
    before = diff._open(snap)
    try:
        d = diff.compare(before, con)
        old_spans = {}
        if old_source is not None:
            for n in d["nodes"]["edited"]:
                if n["kind"] == "callable":
                    r = before.execute("SELECT path, span_start FROM nodes WHERE id = ?", (n["id"],)).fetchone()
                    if r is not None:
                        old_spans[n["id"]] = (r[0], r[1])
        changed_ids = [n["id"] for key in ("resigned", "edited", "types_edited") for n in d["nodes"][key]]
        fb = {r[0]: r[1] for r in before.execute("SELECT id, content_hash FROM nodes WHERE kind = 'file' AND layer = 'fact'")}
        files = [r for r in con.execute("SELECT id, name, path, content_hash, span_end FROM nodes WHERE kind = 'file' AND layer = 'fact'")
                 if fb.get(r["id"]) != r["content_hash"]]
        gone_files = sorted({r[0] for r in before.execute("SELECT path FROM nodes WHERE kind = 'file' AND layer = 'fact'")}
                            - {r[0] for r in con.execute("SELECT path FROM nodes WHERE kind = 'file' AND layer = 'fact'")})
        own = diff.own_changes(before, con, changed_ids + [f["id"] for f in files])
        rules_before = rules.check(before, rules_from=con)
        removed_callers = {}
        for n in d["nodes"]["removed"]:
            if n["kind"] in ("callable", "test", "type"):
                # caller -> whether every call it made was a guess by name
                removed_callers[n["id"]] = dict(sorted((r[0], bool(r[1])) for r in before.execute(
                    "SELECT src_id, MIN(precision = 'guess') FROM calls WHERE dst_id = ? OR dst_id LIKE ? GROUP BY src_id",
                    (n["id"], n["id"] + ".%"))))
    finally:
        before.close()
    names = spec._Names(con)

    def lines_of(i):
        return len(own.get(i) or []) if own is not None else None
    # A container whose text changed only inside something nested in it (a class around an edited method) is not
    # an edit of its own.
    edited = [n for n in d["nodes"]["edited"] + d["nodes"]["resigned"] if own is None or own.get(n["id"])]
    resigned = {n["id"] for n in d["nodes"]["resigned"]}
    # Where ids carry no parameter list (Python, TypeScript, Go ...), a changed signature is an edit like any other:
    # read the declaration as it was and as it is.
    root_of = diff.roots(con)
    old_files: dict = {}
    for n in edited:
        i = n["id"]
        if i in resigned or i not in old_spans or not n.get("line"):
            continue
        first = min((ln for ln, _ in (own or {}).get(i) or []), default=None)
        if first is None or first > n["line"] + 40:
            continue
        path, old_line = old_spans[i]
        if path not in old_files:
            old_files[path] = old_source(path)
        new_data = diff.source(con, i.split(":", 1)[0], n["path"], root_of)
        was = _params(old_files[path], old_line or 1, n["name"]) if old_files[path] and old_line else None
        now = _params(new_data.decode("utf-8", errors="replace"), n["line"], n["name"]) if new_data else None
        if was is not None and now is not None and was != now:
            n.update({"was": was, "now": now})
            resigned.add(i)
    types = [n for n in d["nodes"]["types_edited"] if own is None or own.get(n["id"])]
    added = d["nodes"]["added"]
    outer_added = [n for n in added if not any(n["id"].startswith(o["id"] + ".") for o in added if o is not n)]
    from . import diagrams   # the changed code as it runs now, and the calls and channel links it gained and lost
    how_it_runs = diagrams.safe(diagrams.for_snapshot, snap, con, [n["id"] for n in edited + added] or [n["id"] for n in types],
                                [n["id"] for n in d["nodes"]["removed"]])
    top = [{"id": f["id"], "path": f["path"], "lines": len(own[f["id"]])} for f in files
           if own is not None and any(t.strip() for _, t in own.get(f["id"]) or [])]
    changed = {n["id"] for n in edited + types} | {n["id"] for n in added}
    removed = d["nodes"]["removed"]

    targets = ([{"id": n["id"], "action": "signature" if n["id"] in resigned else "behavior"} for n in edited + types]
               + [{"id": n["id"], "action": "behavior"} for n in outer_added if n["id"] in names.by_id])
    report = change.assess(con, about or "a pull request", targets) if targets else {}
    if "error" in report:
        report = {}
    must = [m for m in report.get("must_edit") or [] if not _inside(m["id"], changed)]
    sig_targets = [i for i in resigned if i in names.by_id]
    for m in must:   # how many calls each must fix: a caller can call the changed code more than once
        sure = 0
        if sig_targets:
            m["call_sites"], sure = con.execute(
                f"SELECT COUNT(*), COALESCE(SUM(precision != 'guess'), 0) FROM calls WHERE src_id = ?"
                f" AND dst_id IN ({','.join('?' * len(sig_targets))})", (m["id"], *sig_targets)).fetchone()
        m["test"] = bool(names.in_tests(m["id"]))
        # the map's only link to the changed code is a guess by name: worth a read, never a reason to block
        m["guessed"] = "a guess" in (m.get("note") or "") and not sure

    still_called = []
    for rid_, callers in removed_callers.items():
        live = [c for c in callers if c in names.by_id and not _inside(c, changed) and not _inside(c, {rid_})]
        if live:
            still_called.append({"id": rid_, "removed": next(n["name"] for n in removed if n["id"] == rid_),
                                 "callers": [spec._label(names, c) for c in live], "caller_ids": live,
                                 "test_callers": [spec._label(names, c) for c in live if names.in_tests(c)],
                                 "guessed": all(callers[c] for c in live)})

    flagged = {m["id"] for m in must} | {c for x in still_called for c in x["caller_ids"]}
    existing = [i for i in dict.fromkeys([n["id"] for n in edited + types]) if i in names.by_id]
    new_existing = [n["id"] for n in outer_added if n["id"] in names.by_id]
    links = [{"key": "edits", "action": "behavior", "text": "", "nodes": existing, "new": [], "into": [], "mention_ids": [],
              "scenarios": [], "notes": [], "ambiguous": []},
             {"key": "added", "action": "add", "text": "", "nodes": new_existing, "new": [], "into": [], "mention_ids": [],
              "scenarios": [], "notes": [], "ambiguous": []}]
    crossings, agree = spec._crossings(con, names, links)
    crossings, agree, across = _touching(con, names, crossings, changed, own, {n["id"] for n in added})
    alone = spec._left_alone(con, names, links)
    state = spec._shared_state_touched(con, set(existing) | set(new_existing))
    now_rules = rules.check(con)
    was_failing = {r["id"] for r in rules_before.get("rules", []) if not r["passes"]}
    new_violations = [r for r in now_rules["rules"] if not r["passes"] and r["id"] not in was_failing]
    if any(r["status"] == "confirmed" for r in new_violations):   # does it fail on links the map is sure of?
        sure_fail = {r["id"] for r in rules.check(con, sure_only=True)["rules"] if not r["passes"]}
        for r in new_violations:
            r["guessed"] = r["id"] not in sure_fail

    def is_test(i):
        n = names.by_id.get(i)
        return bool(n) and (n["kind"] == "test" or bool(TEST_PATH.search(n["path"] or "")))
    fns = [i for i in existing + new_existing if names.by_id[i]["kind"] in ("callable", "test") and not is_test(i)]
    tests_touched = [spec._label(names, i) for i in existing + new_existing
                     if is_test(i) and names.by_id[i]["name"] not in ("<module>", "<top-level>")]
    test_files = sorted({f["path"] for f in files if TEST_PATH.search(f["path"] or "")})
    product = [n for n in edited + types + outer_added if not is_test(n["id"])]
    lines = sum(lines_of(n["id"]) or 0 for n in edited + types) + sum(t["lines"] for t in top)
    modules = sorted({names.module.get(n["id"]) for n in product if names.module.get(n["id"])})
    guessed = sum(1 for r in report.get("risks") or [] if "guesses by name" in r["what"])

    def item(n, **extra):
        name = f"{n['path']} (top level)" if n["name"] in ("<module>", "<top-level>") else n["name"]
        return {"id": n["id"], "name": name, "kind": n["kind"], "path": n["path"], "line": n.get("line"),
                **({"test": True} if is_test(n["id"]) or TEST_PATH.search(n["path"] or "") else {}), **extra}
    bodies = {n["path"] for n in edited if n["name"] in ("<module>", "<top-level>")}
    top = [t for t in top if t["path"] not in bodies]
    return {
        "about": about,
        "changed": {
            "edited": [item(n, lines=lines_of(n["id"]), **({"signature": f"({n['was']}) -> ({n['now']})"} if n["id"] in resigned else {}))
                       for n in edited],
            "types": [item(n, lines=lines_of(n["id"])) for n in types],
            "added": [item(n) for n in outer_added],
            "removed": [item(n) for n in removed if not any(n["id"].startswith(o["id"] + ".") for o in removed if o is not n)],
            "top_level": top, "files": sorted(f["path"] for f in files), "files_removed": gone_files,
        },
        "size": {"functions": len([n for n in product if n["kind"] in ("callable", "test")]), "types": len(types),
                 "added": len(outer_added), "removed": len(removed), "lines": lines if own is not None else None,
                 "modules": len(modules), "files": len(files) + len(gone_files)},
        "reaches": {
            "signature_changed_callers_not_edited": must,
            "removed_but_still_called": still_called,
            "channels_crossed": crossings,
            "other_ends_not_edited": agree,
            "reads_or_calls_across_a_channel": across,
            "callers_left_alone": [x for x in ({**c, "callers": [n for n, i in zip(c["callers"], c["caller_ids"]) if i not in flagged],
                                                 "caller_ids": [i for i in c["caller_ids"] if i not in flagged]}
                                                for c in alone["callers"]) if x["caller_ids"]][:30],
            "state_shared_with_unchanged_code": [x for x in alone["state"] if not x.get("quiet")][:30],
            "fields_written_from_elsewhere_too": state,
            "new_members_named_like_existing_ones": alone["beside"][:20],
            "entry_points_affected": (report.get("entry_points_affected") or [])[:20],
        },
        "tests": {
            "likely_to_fail_unedited": sorted({m["name"] for m in must if m.get("test")}
                                              | {t for x in still_called for t in x["test_callers"]}),
            "touched_by_the_change": tests_touched, "test_files_changed": test_files,
            "changed_code_no_test_reaches": [u for u in report.get("untested") or [] if not u["name"].endswith(("<module>", "<top-level>"))],
            "tests_to_run": (report.get("tests_to_run") or [])[:30],
            **_measured(con, [n["id"] for n in edited + types + added]),
        },
        "structure": {"new_dependencies": d["structure"]["new_dependencies"],
                      "removed_dependencies": d["structure"]["removed_dependencies"],
                      "flows_changed": d["flows"]["changed"], "rules_now_failing": new_violations},
        "performance": {"changed_functions_by_how_much_runs_through_them": spec.hot_functions(con, names, fns)[:25],
                        "tests_that_measure_speed": spec._speed_tests(con, set(fns))[:20]},
        "how_it_runs": how_it_runs,
        "how_sure": {"guessed_caller_links": guessed > 0,
                     "note": "Caller and channel links come from the map. Where the map guessed a link by name it says so;"
                             " read the code before filing anything on one."},
        "marks": (
            [{"id": n["id"], "role": "changed", "note": "signature changed" if n["id"] in resigned else "edited"} for n in edited + types
             if n["id"] in names.by_id]
            + [{"id": i, "role": "new", "note": "added"} for i in new_existing]
            + [{"id": m["id"], "role": "must_edit", "note": "calls code whose signature changed; not edited"} for m in must]
            + [{"id": c, "role": "must_edit", "note": f"calls {x['removed']}, which was removed"} for x in still_called for c in x["caller_ids"]]
            + [{"id": a["id"], "role": "contract", "note": a["why"]} for a in agree if a["id"] in names.by_id]
            + [{"id": a["id"], "role": "direct", "note": a["why"]} for a in across if a["id"] in names.by_id]
            + [{"id": w, "role": "direct", "note": f"also calls {c['changed']}"} for c in alone["callers"][:30] for w in c["caller_ids"]]),
    }


def _measured(con, ids: list[str]) -> dict:
    """With per-test coverage imported: the tests measured running the changed code, and a command that runs them."""
    from . import affected
    ran = affected.measured_tests(con, ids)
    return {"measured_running_the_change": ran[:40], "run_them": affected.commands(con, ran)} if ran else {}


# -- the whole step ---------------------------------------------------------------------------------------
def review(db: str | Path, path: str | Path = ".", base: Optional[str] = None, about: str = "", github: Optional[str] = None,
           given_id: Optional[str] = None) -> dict:
    """Map the checkout (again, if it changed) and the commit it branched from, compare them, store the change
    under pr-<id> so reviewers can file findings on it, and write its page."""
    from . import loop
    root = git_root(Path(path).resolve())
    title, url = "", ""
    if github:
        gh = github_pr(root, github)
        base = base or gh["baseRefName"]
        title, url = gh.get("title") or "", gh.get("url") or ""
        about = about or ((gh.get("title") or "") + "\n\n" + (gh.get("body") or "")).strip()
        head = _git(root, "rev-parse", "HEAD")
        if gh.get("headRefOid") and gh["headRefOid"] != head:
            return {"error": f"the checkout is at {head[:7]}, not at pull request {github}'s head"
                             f" ({gh['headRefOid'][:7]}): run `gh pr checkout {github}` first"}
    if not base:
        base = _default_base(root)
    for name in (base, "origin/" + base):   # a branch this clone has only as origin's
        try:
            _git(root, "rev-parse", "--verify", "-q", name + "^{commit}")
            base = name
            break
        except GitError:
            continue
    else:
        return {"error": f"no commit or branch {base!r} in {root}"}
    base_sha = _git(root, "merge-base", base, "HEAD")
    head_sha = _git(root, "rev-parse", "HEAD")
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
    given = about.strip() or None   # said by the person (or the pull request); a description made here is not kept
    if not given:
        about = _described(root, base_sha, db, given_id, github)
    db = Path(db)
    if not db.exists():
        loop.map_repos([str(root)], db, page=False)
    else:
        con = store.connect(db)
        try:
            known = _repo_of(con, root)
        finally:
            con.close()
        if known is None:
            return {"error": f"the store {db} does not hold {root}: map it first (`leyline map {root}`)"}
        loop.refresh(db)
    con = store.connect(db)
    try:
        rid = _repo_of(con, root)
        cid = change_id(root, given_id, github)
    finally:
        con.close()
    snap = base_snapshot(db, root, rid, base_sha, cid)
    con = store.connect(db)
    try:
        facts = analyse(con, snap, about, _old_source(root, base_sha))
        title = title or _title(about) or _untitled(root)
        marks = facts.pop("marks")
        with con:
            row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
            attrs = json.loads(row[0] or "{}") if row else {}
            if given:
                attrs["about_given"] = given
            attrs.update({"title": title, "kind": "pr", "base": base, "base_sha": base_sha, "url": url, "dirty": dirty,
                          "root": str(root), "report": {k: facts[k] for k in ("size",)}})
            con.execute("INSERT OR REPLACE INTO change_proposals (id, intent, status, base_commit, head_commit, attrs)"
                        " VALUES (?,?,?,?,?,?)", (cid, attrs.get("about_given") or about or title, "pr", base_sha, head_sha,
                                                  json.dumps(attrs)))
        seen, kept = set(), []
        for m in marks:
            if m["id"] not in seen:
                seen.add(m["id"])
                kept.append(m)
        if kept:
            change.save_view(con, "Pull request: " + title, about or title, kept, kind="change", source="leyline-pr",
                             change_id=cid, view_id="view-" + cid,
                             legend={"changed": "edited by the change", "new": "added by the change",
                                     "must_edit": "should have changed with it, and did not",
                                     "contract": "the other end of a channel the change crosses",
                                     "direct": "also calls what the change edited"})
        out = {"change_id": cid, "title": title, "url": url, "base": base, "base_sha": base_sha, "head_sha": head_sha,
               "dirty": dirty, "root": str(root), **facts,
               "house_rules": _house_rules(root, facts["changed"]["files"]),
               "other_files": _other_files(root, base_sha, facts["changed"]["files"] + facts["changed"]["files_removed"]),
               "findings": spec.findings(con, cid)["findings"], "reviews": spec.reviews(con, cid)}
        from . import coupling   # files that usually changed with what the branch changed, in the history before it
        ch = facts["changed"]
        out["usually_changes_with"] = coupling.for_pr(
            con, rid, root, base_sha, functions=[x["id"] for x in ch["edited"] if not x.get("test")],
            changed_ids=[x["id"] for k in ("edited", "types", "added", "removed") for x in ch[k]])
        from . import related, rereview   # earlier changes to this code; and, on a re-review, what changed since the last
        out["related_changes"] = related.find(con, spec._Names(con), _touched(facts), exclude=cid, since=base_sha)
        summary, fp = rereview.summarize(facts), diff._fingerprint(con)
        out["since_last_review"] = rereview.since(con, cid, summary, fp)
        rereview.mark(out["findings"], out["since_last_review"])
        rereview.record(con, cid, head_sha, dirty, summary, fp)
        out["gate"] = gate(out, gate_config(root))   # judged from this run's facts and the findings as they are now
        page =Path(db).parent / "reviews" / f"{cid}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text(out), encoding="utf-8")
        out["page"] = str(page)
        return out
    finally:
        con.close()


def _touched(f: dict) -> list[str]:
    """The code a review's change edited or added, by id."""
    c = f["changed"]
    return [x["id"] for k in ("edited", "types", "added") for x in c[k]]


def _title(about: str) -> str:
    """A title from a description: its first line, or the first commit's subject (the oldest: commits added later
    leave it as it was), cut at a word near 80 characters."""
    line = about.strip().split("\n")[0]
    if line.startswith("From its commit messages: "):
        line = line.removeprefix("From its commit messages: ").split(" / ")[-1]
    line = line.strip()
    if len(line) <= 80:
        return line
    cut = line[:80].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "..."


def _untitled(root: Path) -> str:
    """A title for a branch with no commits of its own and no description: its name, and that it is uncommitted."""
    try:
        branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
    except GitError:
        branch = ""
    return f"Uncommitted edits on {branch}" if branch else "Uncommitted edits"


def _described(root: Path, base_sha: str, db, given_id, github) -> str:
    """What the change says it does when no one said: what an earlier review of it was told, else its commits'
    messages."""
    if Path(db).exists():   # what the person said at an earlier review of this change still holds
        con = store.connect(db)
        try:
            row = con.execute("SELECT attrs FROM change_proposals WHERE id = ?", (change_id(root, given_id, github),)).fetchone()
        finally:
            con.close()
        said = (json.loads(row[0] or "{}").get("about_given") if row else None)
        if said:
            return said
    try:
        log = _git(root, "log", "--no-merges", "--format=%s%n%n%b%x00", f"{base_sha}..HEAD")
    except GitError:
        return ""
    msgs = [m.strip() for m in log.split("\0") if m.strip()][:20]
    if not msgs:
        return ""
    subjects = " / ".join(m.split("\n")[0] for m in msgs)
    # the whole messages only when one says more than its subject line: else the page reads each subject twice
    return "From its commit messages: " + subjects + ("\n\n" + "\n\n".join(msgs) if any("\n" in m for m in msgs) else "")


def _other_files(root: Path, base_sha: str, mapped: list[str]) -> list[str]:
    """Files the change touches that the map does not read (docs, styles, data, config): the page names them so a
    reviewer knows the map's view of the change stops short of them."""
    try:
        listed = _git(root, "diff", "--name-only", base_sha).splitlines()
        listed += _git(root, "ls-files", "--others", "--exclude-standard").splitlines()
    except GitError:
        return []
    seen = set(mapped)
    return sorted({f for f in listed if f and f not in seen and not f.startswith(".leyline/")})


def _old_source(root: Path, sha: str):
    """path -> the file's text at a commit, or None."""
    def read(path: str) -> Optional[str]:
        try:
            return _git(root, "show", f"{sha}:{path}", binary=True).decode("utf-8", errors="replace")
        except GitError:
            return None
    return read


def _default_base(root: Path) -> str:
    """origin's default branch when it is known, else main, else master."""
    try:
        ref = _git(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD")
        if ref:
            return ref
    except GitError:
        pass
    for b in ("main", "master", "origin/main", "origin/master"):
        try:
            _git(root, "rev-parse", "--verify", "-q", b + "^{commit}")
            return b
        except GitError:
            continue
    return "main"


def stored(con, cid: str) -> Optional[dict]:
    row = con.execute("SELECT intent, base_commit, attrs FROM change_proposals WHERE id = ?", (cid,)).fetchone()
    if row is None:
        return None
    a = json.loads(row["attrs"] or "{}")
    return a if a.get("kind") == "pr" else None


def review_facts(con, cid: str, reviewer: Optional[str] = None) -> dict:
    """The facts behind a pull request's page, arranged as the questions each reviewer answers, as
    spec.review_facts does for a spec. Run `leyline pr` first: it makes the baseline these compare with."""
    a = stored(con, cid)
    snap = diff.snapshot_path(con, cid)
    if a is None or not snap.exists():
        return {"error": f"no pull request {cid!r} reviewed here; run `leyline pr` in its checkout first"}
    about = con.execute("SELECT intent FROM change_proposals WHERE id = ?", (cid,)).fetchone()[0] or ""
    root = Path(a.get("root") or ".")
    f = analyse(con, snap, about, _old_source(root, a["base_sha"]) if a.get("base_sha") and root.is_dir() else None)
    if reviewer:
        spec.record_review(con, cid, reviewer)
    r, t = f["reaches"], f["tests"]
    from . import learnings, related, rereview
    return {
        "change_id": cid, "title": a.get("title"), "what_it_says_it_does": about or "(no description: judge it by the code)",
        "since_last_review": rereview.for_facts(rereview.since(con, cid, rereview.summarize(f))),   # a re-review starts here
        "related_changes": related.find(con, spec._Names(con), _touched(f), exclude=cid, since=a.get("base_sha")),
        "changed": f["changed"], "size": f["size"],
        "house_rules_to_read_first": _house_rules(Path(a.get("root") or "."), f["changed"]["files"]),
        "learnings_that_apply": learnings.applying(con, cid),   # past decisions on this code: read these first
        "logic": {
            "signature_changed_callers_not_edited": r["signature_changed_callers_not_edited"],
            "removed_but_still_called": r["removed_but_still_called"],
            "channels_crossed": r["channels_crossed"],
            "other_ends_of_those_channels_not_edited": r["other_ends_not_edited"],
            "callers_of_changed_functions_left_alone": r["callers_left_alone"],
            "state_shared_with_unchanged_code": r["state_shared_with_unchanged_code"],
            "fields_written_from_elsewhere_too": r["fields_written_from_elsewhere_too"],
            "new_members_named_like_existing_ones": r["new_members_named_like_existing_ones"],
            "usually_changes_with_not_changed": _usually(con, cid, a, root),
            "changed_code_no_test_reaches": t["changed_code_no_test_reaches"],
            "tests_touched": t["touched_by_the_change"], "test_files_changed": t["test_files_changed"],
            "new_dependencies_between_modules": f["structure"]["new_dependencies"],
            "rules_now_failing": f["structure"]["rules_now_failing"],
            "ask": "First: does the code do what the description says, all of it and nothing else? Name each edit the"
                   " description does not explain. Then, for each list: is the change wrong, or is the map? Read the code"
                   " before filing. Then ask what the change leaves out: error paths, empty inputs, ordering, the second"
                   " caller, the other end of each channel, data written before the change.",
        },
        "performance": {**f["performance"],
                        "ask": "For each changed function on a hot path: does the change add work per call, allocation, I/O,"
                               " a lock or a process hop? Name the test that would show a regression, or say none exists."},
        "how_sure": f["how_sure"],
        "how_to_file": f"leyline spec finding {cid} --reviewer <logic|performance> --severity <high|medium|low>"
                       " --claim \"...\" --evidence <node id> --proposal \"...\" (or the spec_finding tool with this id).",
    }


def _usually(con, cid: str, a: dict, root: Path) -> dict:
    """What usually changed with the files the branch changed, and was not changed, from git history before it."""
    from . import coupling
    rid = _repo_of(con, root) if root.is_dir() else None
    return coupling.for_pr(con, rid, root, a["base_sha"]) if rid and a.get("base_sha") else {}


# -- the gate ---------------------------------------------------------------------------------------------
# What can hold up a pull request under `leyline pr --gate`, by the name a project lists in openspec/leyline.toml:
#
#     [pr]
#     blocking = ["unedited-callers", "still-called", "failing-rules", "open-high-findings"]
#
# The default blocks only what the map shows is broken. A link the map guessed by name never blocks on its own.
GATE_KINDS = {
    "unedited-callers": "callers of a changed signature that were not edited",
    "still-called": "removed code that is still called",
    "failing-rules": "confirmed error-level rules that now fail",
    "open-high-findings": "open high findings",
    "open-medium-findings": "open findings of medium severity or higher",
    "open-findings": "open findings of any severity",
    "other-ends": "other ends of a channel the edit changed, not edited",
    "untested": "changed code no test reaches",
}
DEFAULT_GATE = ("unedited-callers", "still-called", "failing-rules", "open-high-findings")
_GATE_ALIASES = {"callers": "unedited-callers", "removed": "still-called", "rules": "failing-rules",
                 "high-findings": "open-high-findings", "medium-findings": "open-medium-findings",
                 "findings": "open-findings", "channels": "other-ends", "no-test": "untested"}
_AT_LEAST = {"open-high-findings": ("high",), "open-medium-findings": ("high", "medium"), "open-findings": None}


def gate_config(root: Path) -> dict:
    """Which kinds block, from `[pr] blocking` in the repository's openspec/leyline.toml, else the default. Never
    raises: a file that cannot be read leaves the default in place and says why in `notes`."""
    from . import verdicts
    out = {"blocking": list(DEFAULT_GATE), "file": None, "notes": []}
    path = Path(root) / "openspec" / verdicts.CONFIG
    if not path.is_file():
        return out
    shown = f"openspec/{verdicts.CONFIG}"
    try:
        data = verdicts._toml(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        out["notes"].append(f"{shown} could not be read ({e}), so the default gate applies.")
        return out
    section = data.get("pr")
    if not isinstance(section, dict) or "blocking" not in section:
        return out
    listed = section["blocking"]
    if isinstance(listed, str):
        listed = [listed]
    if not isinstance(listed, list):
        out["notes"].append(f"{shown}: `[pr] blocking` should be a list of kinds, so the default gate applies.")
        return out
    kinds = []
    for w in listed:
        k = re.sub(r"[_\s]+", "-", str(w).strip().lower())
        k = _GATE_ALIASES.get(k, k)
        if k not in GATE_KINDS:
            out["notes"].append(f"{shown} names \"{w}\" under [pr], which is not a kind; it was left out."
                                f" The kinds are {', '.join(GATE_KINDS)}.")
        elif k not in kinds:
            kinds.append(k)
    out["blocking"] = [k for k in GATE_KINDS if k in kinds]
    out["file"] = shown
    return out


def gate(r: dict, config: dict) -> dict:
    """What is left that blocks, from the facts of this run and the findings as they stand now: one line each,
    with what to do about it. Running `leyline pr` again after new commits judges it again from scratch."""
    on = set(config["blocking"])
    reach, t = r["reaches"], r["tests"]
    out = []   # (reason, what to do)
    guessed = 0   # left out: the map's only link behind them is a guess by name
    if "unedited-callers" in on:
        for m in reach["signature_changed_callers_not_edited"]:
            guessed += bool(m.get("guessed"))
            if not m.get("guessed"):
                out.append((f"`{m['name']}` calls code whose signature changed, and was not edited",
                            f"update the call in `{m['name']}` ({m.get('path') or 'see the page'}) to the new signature"))
    if "still-called" in on:
        for x in reach["removed_but_still_called"]:
            guessed += bool(x.get("guessed"))
            if not x.get("guessed"):
                out.append((f"`{x['removed']}` was removed and is still called by {_names(x['callers'], 3)}",
                            f"remove the calls to `{x['removed']}` from {_names(x['callers'], 3)}, or keep `{x['removed']}`"))
    if "failing-rules" in on:
        for rule in r["structure"]["rules_now_failing"]:
            if rule.get("status") != "confirmed" or rule.get("severity") != "error":
                continue
            guessed += bool(rule.get("guessed"))
            if not rule.get("guessed"):
                what = f"{rule['kind']} {rule['from']}" + (f" -> {rule['to']}" if rule.get("to") else "")
                out.append((f"rule {rule['id']} ({what}) now fails",
                            f"remove what breaks rule {rule['id']} ({what}); `leyline rules` lists it"))
    sev = next((_AT_LEAST[k] for k in ("open-findings", "open-medium-findings", "open-high-findings") if k in on), False)
    if sev is not False:
        order = {"high": 0, "medium": 1, "low": 2}
        for f in sorted((f for f in r.get("findings") or [] if f["status"] == "open"),
                        key=lambda f: order.get(f["severity"], 3)):
            if sev is None or f["severity"] in sev:
                out.append((f"open {f['severity']} finding {f['id']}: {spec._first_sentence(f['claim'], 120)}",
                            f"fix what finding {f['id']} says, or have the person resolve it"
                            f" (`leyline spec resolve {f['id']} accepted|rejected|deferred \"why\"`)"))
    if "other-ends" in on:
        for a in reach["other_ends_not_edited"]:
            guessed += bool(a.get("guessed"))
            if not a.get("guessed"):
                out.append((f"`{a['name']}` {a['why']}, and was not edited",
                            f"check that `{a['name']}` agrees with the change, and edit it if not"))
    if "untested" in on:
        for u in t["changed_code_no_test_reaches"]:
            out.append((f"no test reaches `{u['name']}`", f"add a test that runs `{u['name']}`"))
    return {"blocking": [why for why, _ in out], "passed": not out, "kinds": config["blocking"],
            "config": config["file"], "notes": config["notes"], "next": out[0][1] if out else None,
            "guessed": guessed}


def gate_lines(g: Optional[dict]) -> list[str]:
    """The page's Gate section: whether it passes, what blocks and under which config, and the first thing to do."""
    if not g:
        return []
    under = (f"set in {g['config']}" if g.get("config") else "the default") + "; `leyline pr --gate` exits 1 when blocked"
    kinds = ", ".join(g["kinds"]) or "nothing"
    L = ["", "## Gate", ""]
    if g["passed"]:
        L.append(f"**Passes.** Nothing that blocks is left. Blocking: {kinds} ({under}).")
    else:
        L.append(f"**Blocked** by {_n(len(g['blocking']), 'thing')}. Blocking: {kinds} ({under}).")
        L += [f"- {b.rstrip('.')}." for b in g["blocking"][:10]]
        if len(g["blocking"]) > 10:
            L.append(f"- and {len(g['blocking']) - 10} more")
        L += ["", f"Next: {g['next']}."]
    if g.get("guessed"):
        L += ["", f"Left out of the gate: {_n(g['guessed'], 'item')} below that rest only on links the map guessed by name."
                  " Read the code behind them."]
    L += g.get("notes") or []
    return L


# -- the page ---------------------------------------------------------------------------------------------
def _n(n: int, word: str, plural: str = "") -> str:
    return f"{n} {word if n == 1 else plural or word + 's'}"


def _names(xs: list[str], k: int = 4) -> str:
    xs = list(dict.fromkeys(xs))
    return ", ".join(f"`{x}`" for x in xs[:k]) + (f" and {len(xs) - k} more" if len(xs) > k else "")


def _by_folder(paths: list[str]) -> list[str]:
    """Paths, with a folder that holds three or more of them said once: `cases/adjust-delta-zero/ (7 files)`."""
    by = defaultdict(list)
    for p in paths:
        parts = p.split("/")
        by["/".join(parts[:-1]) if len(parts) > 1 else ""].append(p)
    # the deepest folder that holds three or more, rolled up one level at a time
    out = []
    for folder, ps in sorted(by.items()):
        out += [f"{folder}/ ({len(ps)} files)"] if folder and len(ps) >= 3 else ps
    if len(out) > 8:    # still long: by top-two folders
        top = defaultdict(int)
        for p in paths:
            top["/".join(p.split("/")[:2])] += 1
        out = [f"{t}/ ({n} files)" if n > 1 else next(p for p in paths if p.startswith(t)) for t, n in sorted(top.items())]
    return out


def _crossing_line(c: dict) -> str:
    """A channel the edit touches, as the page says it: which end's edit, and how many others it reaches."""
    what = f"{'key format' if c['channel'] == 'format' else c['channel']} {c.get('program') or c.get('address') or ''}".strip()
    others = [sp["name"] for sp in c.get("spokes", []) if not sp["changed"]]
    also = f", and the edit to {_names(c['spokes_touched'], 3)} changes how it is used" if c.get("spokes_touched") else ""
    if c["hub_touched"]:
        verb = "starts as" if c["channel"] == "process" else "writes" if c["data"] else "answers"
        return (f"Crosses the {what}: the edit changes what `{c['hub_name']}` {verb}{also}. {_n(len(others), 'other end')}"
                f" depend{'s' if len(others) == 1 else ''} on it with no compile-time link, so a mismatch fails only at run time.")
    return (f"Crosses the {what}: the edit to {_names(c['spokes_touched'], 3)} changes what is sent to `{c['hub_name']}`,"
            " with no compile-time link between them.")


def text(r: dict) -> str:
    c, s, reach, t = r["changed"], r["size"], r["reaches"], r["tests"]
    head = f"`{r['head_sha'][:7]}`" + (" with uncommitted edits" if r["dirty"] else "")
    L = [f"# Review: {r['title']}", "",
         (f"Base `{r['base_sha'][:7]}`" if r["base_sha"].startswith(r["base"]) or r["base"].startswith(r["base_sha"][:7])
          else f"Base `{r['base']}` (`{r['base_sha'][:7]}`, where this branch left it)") + f" compared with {head}."
         + (f" {r['url']}" if r.get("url") else "")]
    L += ["", "**What it says it does:** " + (spec._first_sentence(r["about"].replace("\n", " "), 400) if r["about"].strip()
                                              else "no description given (`--about`, or `--github <number>`). Judge it by the code.")]
    from . import related, rereview
    L += rereview.lines(r.get("since_last_review"), head)
    L += gate_lines(r.get("gate"))
    # what changed
    parts = [_n(s["functions"], "function") + " edited or added" if s["functions"] else "",
             _n(s["types"], "type") + " changed" if s["types"] else "",
             _n(s["removed"], "thing") + " removed" if s["removed"] else ""]
    where = f" in {_n(s['modules'], 'module')}" if s["modules"] else ""
    lines = f", {_n(s['lines'], 'line')} inside them" if s.get("lines") else ""
    L += ["", "## What changed", "",
          (", ".join(p for p in parts if p) or "Nothing on the map changed") + where + lines + f" ({_n(s['files'], 'file')})."]
    sig = [e for e in c["edited"] if e.get("signature")]
    for e in sig[:8]:
        L.append(f"- Signature: `{e['name']}` {e['signature']}")
    plain = [e for e in c["edited"] if not e.get("signature") and not e.get("test")]
    if plain:
        L.append(f"- Edited: {_names([e['name'] for e in plain], 8)}")
    if [e for e in c["added"] if not e.get("test")]:
        L.append(f"- Added: {_names([e['name'] for e in c['added'] if not e.get('test')], 8)}")
    if c["removed"]:
        L.append(f"- Removed: {_names([e['name'] for e in c['removed']], 8)}")
    if [x for x in c["top_level"] if not TEST_PATH.search(x["path"])]:
        L.append(f"- Outside any function: {_names([x['path'] for x in c['top_level'] if not TEST_PATH.search(x['path'])], 6)}")
    if r.get("other_files"):
        L.append(f"- Files the map does not read: {_names(_by_folder(r['other_files']), 8)}")
    big = s["functions"] + s["types"] > 40 or s["modules"] > 8
    if big:
        L.append(f"- This is large for one review ({_n(s['functions'] + s['types'], 'unit')} across {_n(s['modules'], 'module')})."
                 " Ask whether it is two changes.")
    from . import diagrams
    L += diagrams.section(r.get("how_it_runs"), "## How it runs")
    # what it reaches and did not change
    risky = []
    for m in reach["signature_changed_callers_not_edited"][:10]:
        sites = f", {_n(m['call_sites'], 'call')}" if m.get("call_sites", 0) > 1 else ""
        risky.append(f"- **Not edited, calls changed code:** `{m['name']}` ({m.get('note') or 'its callee changed signature'}{sites}).")
    for x in reach["removed_but_still_called"][:10]:
        risky.append(f"- **Removed but still called:** `{x['removed']}`, from {_names(x['callers'])}.")
    for ch in [c for c in reach["channels_crossed"] if c["hub_touched"] or not c["data"]][:6]:
        risky.append("- " + _crossing_line(ch))
    groups: dict = defaultdict(list)
    for a in reach["other_ends_not_edited"]:
        groups[(a["channel"], a["address"], a["hub"])].append(a)
    for (ch, address, hub), xs in list(groups.items())[:8]:
        if len(xs) <= 3:
            risky += [f"- **Must agree with the change:** `{a['name']}`: {a['why']}." for a in xs]
        else:   # a table or a route half the program uses: one line, the reviewer reads what was written
            risky.append(f"- **Must agree with the change:** {_n(len(xs), 'other end')} of the {ch} {address} that {hub}"
                         f" writes or answers ({_names([a['name'] for a in xs], 3)}): check what the edit changed there.")
    L += ["", "## What it reaches and did not change", ""]
    L += risky or ["Nothing that must change with it was left alone, as far as the map sees."]
    soft = []
    for x in reach["callers_left_alone"][:6]:
        soft.append(f"- `{x['changed']}` is also called by {_names(x['callers'])}: does what they get still hold?")
    for x in reach["state_shared_with_unchanged_code"][:4]:
        soft.append(f"- `{x['field']}`, which `{x['used_by_changed']}` uses, is also used by {_names(x['also_used_by_unchanged'])}.")
    for x in reach["new_members_named_like_existing_ones"][:4]:
        soft.append(f"- New `{x['new']}` sits beside `{x['existing']}`, used by {_names(x['existing_used_by_unchanged'])}: do they need the new one too?")
    for x in reach["reads_or_calls_across_a_channel"][:4]:
        soft.append(f"- `{x['name']}` {x['why']}.")
    if soft:
        L += ["", "Shares a caller, a field or data with the change:", ""] + soft
    hist = r.get("usually_changes_with") or {}
    if hist.get("files"):   # from git history: docs, schemas, config and fixtures the map has no link to
        from .coupling import line as coupling_line
        L += ["", f"Usually changes with what it changed, and it did not change ({hist['about']}):", ""]
        L += [f"- {coupling_line(x)}." for x in hist["files"][:5]]
        if hist["total"] > 5:
            L.append(f"- and {hist['total'] - 5} more: `leyline spec facts {r['change_id']}`")
    if hist.get("functions"):   # the same, by function, for the functions the branch edited
        from .fncoupling import line as fn_line
        L += ["", "Functions that usually changed with the functions it edited, and it did not change:", ""]
        L += [f"- {fn_line(x)}." for x in hist["functions"][:5]]
    L += related.lines(r.get("related_changes"))
    if reach["entry_points_affected"]:
        L += ["", "Reached from: " + _names([e["name"] for e in reach["entry_points_affected"]], 6) + "."]
    # tests
    L += ["", "## Tests", ""]
    if t.get("likely_to_fail_unedited"):
        one = len(set(t["likely_to_fail_unedited"])) == 1
        L.append("- **Likely to fail:** " + _names(t["likely_to_fail_unedited"], 6)
                 + (": it calls code whose signature changed, or that was removed, and was not edited." if one else
                    ": they call code whose signature changed, or that was removed, and were not edited."))
    if t["touched_by_the_change"] or t["test_files_changed"]:
        L.append("- The change edits or adds tests: " + _names(t["touched_by_the_change"] or t["test_files_changed"], 6) + ".")
    else:
        L.append("- **The change edits no tests.**")
    if t["changed_code_no_test_reaches"]:
        L.append(f"- No test on the map reaches {_names([u['name'] for u in t['changed_code_no_test_reaches']], 6)}.")
    if t["tests_to_run"]:
        L.append("- Tests that run the changed code: " + _names([x["name"] for x in t["tests_to_run"]], 6) + ".")
    if t.get("measured_running_the_change"):
        L.append("- Measured running the changed code (per-test coverage): "
                 + _names([x.get("pytest") or x["name"] for x in t["measured_running_the_change"]], 6) + ".")
        L += [f"  Run them: `{c['command']}`" for c in t.get("run_them") or [] if c["command"]][:3]
    st = r["structure"]
    if st["new_dependencies"] or st["rules_now_failing"]:
        L += ["", "## Structure", ""]
        for dep in st["new_dependencies"][:8]:
            L.append(f"- New dependency: {dep['from']} now uses {dep['to']} ({_n(dep['links'], 'link')}).")
        for rule in st["rules_now_failing"][:8]:
            L.append(f"- **A rule now fails:** {rule['kind']} {rule['from']}" + (f" -> {rule['to']}" if rule.get("to") else "") + ".")
    if r["how_sure"]["guessed_caller_links"]:
        L += ["", "Some caller links are guesses by name: read the code behind any line above before acting on it."]
    if r.get("house_rules"):
        L += ["", "House rules to review against: " + _names(r["house_rules"], 6) + "."]
    found = r.get("findings") or []
    L += ["", "## Review", ""]
    L += spec.review_lines(found, r.get("reviews") or [], full=True) if found or r.get("reviews") else [
        f"Not reviewed yet. `leyline spec facts {r['change_id']} --reviewer logic` (then `performance`) gives a reviewer"
        " its facts; the leyline-adversarial-review skill runs it."]
    return "\n".join(L) + "\n"
