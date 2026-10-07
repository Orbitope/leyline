"""Change coupling by function, for a few functions at a time.

leyline.coupling counts files that change in the same commits. Two functions in two files can be the pair that
really moves together (a validator rule and its twin in another language; a reader and the writer of one format)
while the rest of each file does not. Telling that needs, for each commit, which functions its diff touched, and a
commit's line numbers say that only against the file as it was at that commit. So this parses old versions of
files, which is too slow to do for a whole history, and is done only for the functions a plan or a pull request is
about:

1. the last MAX_COMMITS commits that changed the function's lines (`git log -L`, which follows the lines back through
   each diff), merges and bulk commits left out;
2. at each, the function's file as it was, parsed with the same adapter the map uses, and the commit's hunks (`-U0`,
   line ranges of the new version) placed in the innermost function around them, so a commit counts for the function
   only where it changed the function's own lines (not only a function nested in it); a hunk that only deletes lines
   is placed at the line it deleted after;
3. the MAX_FILES other files changed in most of those commits (each in at least MIN_TOGETHER of them and in at least
   half) are read the same way at those commits, and nothing else is read.

`partner` changed in `together` of the `changes` commits that changed `function`: it is reported when that is at
least MIN_TOGETHER commits and at least MIN_CONFIDENCE of them. Which functions one commit changed in one file is
kept in the store (coupling_functions), so a second plan reads no old file again.

What it cannot see: a function renamed or moved to another file has a new id, so its older commits count under the
old one; a file renamed counts only from its new name (no --follow).
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from .coupling import BULK, MIN_CONFIDENCE, MIN_TOGETHER, _git

MAX_COMMITS = 20      # commits read per function
MAX_FILES = 4         # other files read per function: those changed in most of its commits
POOL_FROM = 6         # parses that are worth a second process
KINDS = ("callable", "test")
TOP = ("<module>", "<top-level>")
HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

_TABLE = ("CREATE TABLE IF NOT EXISTS coupling_functions (repo_id TEXT, path TEXT, sha TEXT, version TEXT, ids TEXT,"
          " PRIMARY KEY (repo_id, path, sha, version))")


def _adapter(path: str):
    from .adapters import BY_EXTENSION
    return BY_EXTENSION.get("." + path.rsplit(".", 1)[-1] if "." in path else "")


def _range_commits(root: Path, sha: str, path: str, start: int, end: int, n: int) -> list[str]:
    """The last `n` commits before `sha` that changed lines `start`..`end` of `path` as it is at `sha`, newest first.
    git follows the range back through each diff (`log -L`), so no older version is parsed to find them."""
    out = _git(root, "log", "--no-merges", "-s", "--format=%x01%H", f"-n{n}", f"-L{start},{end}:{path}", sha)
    return [c.strip().decode() for c in (out or b"").split(b"\x01")[1:] if c.strip()]


def _commit_files(root: Path, commits: list[str]) -> dict[str, set]:
    """commit -> every file it changed, for the commits that changed no more than BULK files."""
    if not commits:
        return {}
    out = _git(root, "-c", "core.quotePath=false", "log", "--no-walk=unsorted", "--no-renames", "--relative",
               "--name-only", "-z", "--format=%x01%H", *commits)
    found = {}
    for chunk in (out or b"").split(b"\x01")[1:]:
        tokens = chunk.split(b"\0")   # the sha, then each file
        files = {t.strip(b"\n").decode("utf-8", "replace") for t in tokens[1:] if t.strip(b"\n")}
        if 0 < len(files) <= BULK:
            found[tokens[0].decode().strip()] = files
    return found


def _hunks(root: Path, commits: list[str], paths: list[str]) -> dict:
    """(commit, path) -> the changed lines of the path as that commit left it, from one `git show -U0`."""
    out = _git(root, "-c", "core.quotePath=false", "show", "--no-renames", "--relative", "-U0", "--no-color",
               "--format=%x01%H", *commits, "--", *paths) or b""
    found: dict = {}
    cur = path = None
    for ln in out.split(b"\n"):
        if ln.startswith(b"\x01"):
            cur, path = ln[1:].decode().strip(), None
        elif ln.startswith(b"+++ "):
            p = ln[4:].decode("utf-8", "replace")
            path = p[2:] if p.startswith("b/") else None   # /dev/null: the file was deleted
        elif ln.startswith(b"@@") and cur and path:
            m = HUNK.match(ln)
            if m:
                start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
                lines = found.setdefault((cur, path), set())
                lines.update(range(start, start + count) if count else (max(start, 1),))
    return found


def _blobs(root: Path, wanted: list[tuple[str, str]]) -> dict:
    """(commit, path) -> the file's bytes at that commit, from one `git cat-file --batch`."""
    import subprocess
    if not wanted:
        return {}
    try:
        proc = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"], capture_output=True, timeout=120,
                              input=b"".join(f"{c}:./{p}\n".encode() for c, p in wanted))
    except (OSError, subprocess.SubprocessError):
        return {}
    data, pos, out = proc.stdout, 0, {}
    for key in wanted:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        head = data[pos:nl].split()
        pos = nl + 1
        if len(head) == 3 and head[1] == b"blob":
            size = int(head[2])
            out[key] = data[pos:pos + size]
            pos += size + 1
    return out


def _nodes(adapter, repo: str, path: str, data: bytes, mod_dir: str):
    """The adapter's parse of one file, without the channel pass where the adapter has a walker of its own (C#,
    Python, TypeScript): the channel pass reads the nodes and adds none, and is about half the time."""
    walker = getattr(adapter, "_Walker", None)
    if walker is not None and callable(getattr(walker, "run", None)):
        import inspect
        args = [repo, path, f"{repo}:file:{path}", data]
        if len(inspect.signature(walker).parameters) >= 5:
            args.append(mod_dir)
        return walker(*args).run()
    return adapter.parse(repo, path, f"{repo}:file:{path}", data, mod_dir)


def _spans(data: bytes, path: str, repo: str, mod_dir: str) -> dict:
    """id -> (first line, last line) of each function in one version of a file; empty when it cannot be read."""
    adapter = _adapter(path)
    if adapter is None:
        return {}
    try:
        res = _nodes(adapter, repo, path, data, mod_dir or ".")
    except Exception:   # RecursionError, a grammar that fails on an old version: that version is not read
        return {}
    return {n.id: (n.span_start, n.span_end or n.span_start) for n in res.nodes
            if n.kind in KINDS and n.span_start and n.name not in TOP}


def _changed(data: bytes, lines: set, path: str, repo: str, mod_dir: str) -> Optional[list[str]]:
    """The functions of one version of a file that hold a changed line (the innermost one around each line)."""
    spans = [(a, b, i) for i, (a, b) in _spans(data, path, repo, mod_dir).items()]
    if not spans:
        return None
    out = set()
    for ln in lines:
        around = [s for s in spans if s[0] <= ln <= s[1]]
        if around:
            out.add(min(around, key=lambda s: (s[1] - s[0], -s[0]))[2])
    return sorted(out)


def _job(args):
    return _changed(*args)


def _run(jobs: list) -> list:
    """_changed() for each job, on two processes when there are enough of them (the parse is the cost)."""
    import os
    workers = min(4, os.cpu_count() or 1)
    if len(jobs) < POOL_FROM or workers < 2:
        return [_job(j) for j in jobs]
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    try:
        ctx = multiprocessing.get_context("fork")
    except ValueError:
        return [_job(j) for j in jobs]
    try:
        with ProcessPoolExecutor(workers, mp_context=ctx) as pool:
            return list(pool.map(_job, jobs, chunksize=max(1, len(jobs) // (workers * 4))))
    except Exception:   # a pool that will not start (a sandbox, a broken worker): one process
        return [_job(j) for j in jobs]


class _Reader:
    """Which functions each commit changed in a file, read once and kept in the store."""

    def __init__(self, con, repo: str, root: Path):
        self.con, self.repo, self.root = con, repo, Path(root)
        con.execute(_TABLE)
        self.mods: dict = {}
        self.parsed = 0

    def mod_dir(self, path: str) -> Optional[str]:
        if path not in self.mods:
            r = self.con.execute("SELECT m.path FROM ancestry a JOIN nodes m ON m.id = a.module_id WHERE a.node_id = ?",
                                 (f"{self.repo}:file:{path}",)).fetchone()
            self.mods[path] = r[0] if r else None
        return self.mods[path]

    def spans_at(self, path: str, data: bytes) -> dict:
        """The functions of one version of a file and their lines: from the map when it read these same bytes, else
        parsed."""
        import hashlib
        r = self.con.execute("SELECT content_hash FROM nodes WHERE id = ?", (f"{self.repo}:file:{path}",)).fetchone()
        if r is not None and r[0] == hashlib.sha1(data).hexdigest():
            return {i: (a, b or a) for i, a, b, name in self.con.execute(
                f"SELECT id, span_start, span_end, name FROM nodes WHERE repo_id = ? AND path = ? AND layer = 'fact'"
                f" AND kind IN ({','.join('?' * len(KINDS))}) AND span_start IS NOT NULL", (self.repo, path, *KINDS))
                if name not in TOP}
        self.parsed += 1
        return _spans(data, path, self.repo, self.mod_dir(path))

    def read(self, wanted: dict[str, set]) -> dict:
        """path -> commit -> the ids of the functions it changed there (None: not readable)."""
        out: dict = {p: {} for p in wanted}
        todo: list = []
        for path, commits in wanted.items():
            adapter = _adapter(path)
            if adapter is None or self.mod_dir(path) is None:   # not code the map reads
                continue
            version = f"{adapter.NAME}/{adapter.VERSION}"
            for c in commits:
                r = self.con.execute("SELECT ids FROM coupling_functions WHERE repo_id = ? AND path = ? AND sha = ? AND version = ?",
                                     (self.repo, path, c, version)).fetchone()
                if r is not None:
                    out[path][c] = json.loads(r[0]) if r[0] is not None else None
                else:
                    todo.append((c, path, version))
        if not todo:
            return out
        hunks = _hunks(self.root, sorted({c for c, _, _ in todo}), sorted({p for _, p, _ in todo}))
        blobs = _blobs(self.root, [(c, p) for c, p, _ in todo if (c, p) in hunks])
        jobs = [(blobs[(c, p)], hunks[(c, p)], p, self.repo, self.mod_dir(p)) for c, p, _ in todo if (c, p) in blobs]
        self.parsed += len(jobs)
        done = iter(_run(jobs))
        rows = []
        for c, path, version in todo:
            # no hunk: the file was deleted at that commit, so no function of it changed there
            ids = next(done) if (c, path) in blobs else []
            out[path][c] = ids
            rows.append((self.repo, path, c, version, json.dumps(ids) if ids is not None else None))
        with self.con:
            self.con.executemany("INSERT OR REPLACE INTO coupling_functions VALUES (?,?,?,?,?)", rows)
        return out


def _inside(a: str, b: str) -> bool:
    return a.startswith(b + ".") or a.startswith(b + "/") or b.startswith(a + ".") or b.startswith(a + "/")


def compute(con, repo: str, root: Path, sha: str, ids: Iterable[str], max_commits: int = MAX_COMMITS,
            min_together: int = MIN_TOGETHER, min_confidence: float = MIN_CONFIDENCE) -> dict:
    """For each function in `ids` (ids on the map now), the functions that changed in most of the commits that changed
    it: {"functions": [{function_id, partner_id, path, together, changes, confidence}], "commits", "parsed", "seconds"}."""
    t0 = time.perf_counter()
    reader = _Reader(con, repo, root)
    targets: dict[str, list] = {}
    for i in dict.fromkeys(ids):
        r = con.execute("SELECT kind, path, name FROM nodes WHERE id = ?", (i,)).fetchone()
        if r is not None and r[0] in KINDS and r[1] and r[2] not in TOP:
            targets.setdefault(r[1], []).append(i)
    found, read = [], 0
    for path, fids in targets.items():
        # Where each function is at `sha` (that version parsed), then the commits that changed those lines.
        mod = reader.mod_dir(path)
        now = _blobs(reader.root, [(sha, path)]).get((sha, path)) if mod is not None else None
        spans = reader.spans_at(path, now) if now is not None else {}
        cands = {f: _range_commits(reader.root, sha, path, *spans[f], max_commits) for f in fids if f in spans}
        files_of = _commit_files(reader.root, sorted({c for cs in cands.values() for c in cs}))
        read = max([read] + [len(cs) for cs in cands.values()])
        # git's range takes in the functions nested in a function; a commit counts for it only where the parse of
        # that version puts a changed line in the function itself, as it does for every partner.
        own = reader.read({path: set(files_of)})[path]
        need: dict[str, set] = {}
        mine: dict[str, list] = {}
        for f, cs in cands.items():
            cf = [c for c in cs if c in files_of and f in (own.get(c) or [])]
            if len(cf) < min_together:
                continue
            mine[f] = cf
            seen = Counter(x for c in cf for x in files_of[c] if x != path and reader.mod_dir(x) is not None
                           and _adapter(x) is not None)
            for x, k in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_FILES]:
                if k >= min_together and k / len(cf) >= min_confidence:
                    need.setdefault(x, set()).update(c for c in cf if x in files_of[c])
        others = reader.read(need) if need else {}
        for f, cf in mine.items():
            together: Counter = Counter()
            for c in cf:
                ids_at = set(own.get(c) or [])
                for x in files_of[c]:
                    if x in others:
                        ids_at |= set(others[x].get(c) or [])
                together.update(ids_at)
            for g, k in together.items():
                if g == f or _inside(f, g) or k < min_together or k / len(cf) < min_confidence:
                    continue
                r = con.execute("SELECT path FROM nodes WHERE id = ?", (g,)).fetchone()
                if r is None:   # gone from the code now
                    continue
                found.append({"function_id": f, "partner_id": g, "path": r[0], "together": k, "changes": len(cf),
                              "confidence": round(k / len(cf), 2)})
    found.sort(key=lambda x: (-x["confidence"], -x["together"], x["function_id"], x["partner_id"]))
    return {"functions": found, "commits": read, "parsed": reader.parsed, "seconds": round(time.perf_counter() - t0, 2)}


def missed(con, repo: str, root: Path, sha: str, ids: Iterable[str], covered, limit: int = 0) -> dict:
    """compute(), keeping the partners `covered(id)` says nothing covers (no task names them; the branch did not change
    them), each partner once (with the function it most often changed with), labelled as the pages say them."""
    from .diagrams import _Nodes
    try:
        r = compute(con, repo, root, sha, ids)
    except Exception:   # history is a lead, never a reason for a page to fail
        return {}
    nodes = _Nodes(con)
    best: dict = {}
    for x in r["functions"]:
        g = x["partner_id"]
        if covered(g) or g in best:
            continue
        best[g] = {**x, "function": nodes.label(x["function_id"]), "partner": nodes.label(g)}
    out = list(best.values())
    return {"functions": out[:limit] if limit else out, "total": len(out), "commits": r["commits"], "seconds": r["seconds"]}


def line(x: dict, tail: str = "") -> str:
    """`B.g` changed in 5 of the 6 commits that changed `A.f`; <tail>."""
    return (f"`{x['partner']}` changed in {x['together']} of the {x['changes']} commits that changed `{x['function']}`"
            + (f"; {tail}" if tail else ""))
