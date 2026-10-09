"""Change coupling from git history: files that usually change in the same commits.

The map links code that calls, imports or crosses a channel to other code. It cannot see a doc that has to be updated
with a rule, a JSON schema that has to agree with its reader, a fixture a test compares with, or the other side of a
protocol written in another language with no shared name. Git history can: if `schema/x.json` changed in 7 of the 9
commits that changed `validate.py`, a change to `validate.py` that leaves `schema/x.json` alone is worth a question.

The measure is CodeScene's. Over the commits read, for files A and B:
- changes(A): commits that changed A
- together(A, B): commits that changed both
- confidence(A -> B) = together / changes(A): how often a change to A came with a change to B
- support = together / commits read

The same is counted from a file to a folder (a commit that changed A and any file in the folder), because some
partners are a new file each time: a rule that comes with a new test case in `cases/<name>/` never couples to any one
case, but it couples to `cases/`. A folder is reported only when no deeper folder says the same, no single file in
it is reported for most of the same commits, and it is not at the top of the repository (`tests/`, `docs/`), which is
too broad to act on.

Which commits are read: at most MAX_COMMITS, from the two years before the commit read from, merges left out. A
commit that changed more than BULK files (a formatter run, a licence header, a vendored import) is left out: it says
those files were touched at once, not that they belong together. Renames are followed, so a file's older commits
count under its name now; files that no longer exist are not reported.

File level here. Function-level coupling maps each commit's diff hunks to the functions they fall in, and a hunk's
line numbers are those of its own commit, so it means parsing old versions of files: too slow for a whole history,
so leyline.fncoupling does it only for the few functions a plan names or a pull request edits (for_spec, for_pr).

The result is kept in the store (coupling_runs, coupling_files, coupling_pairs, coupling_dirs) under the commit it
was read from, so it is worked out once per commit.
"""

from __future__ import annotations

import itertools
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from . import store

MAX_COMMITS = 1000
YEARS = 2
BULK = 50             # a commit that changed more files than this is a bulk change, left out
MIN_TOGETHER = 3      # report a pair only when it changed together at least this often
MIN_CONFIDENCE = 0.5  # and at least this share of the first file's commits changed the second
KEEP = 2              # pairs are stored from this many shared commits up, so a lower threshold can still be asked
KEEP_RUNS = 3         # results kept per repository (HEAD, and the bases of pull requests reviewed)


def _git(root: Path, *args: str, timeout: float = 300) -> Optional[bytes]:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def head(root: Path) -> Optional[str]:
    out = _git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    return out.decode().strip() if out else None


def _commits(root: Path, sha: str, max_commits: int, years: float) -> Optional[list[list[tuple[str, ...]]]]:
    """Each commit's changes, newest first: (status, path) or, for a rename or copy, (status, old path, new path)."""
    when = _git(root, "log", "-1", "--format=%ct", sha)
    if when is None:
        return None
    since = int(when.decode().strip() or 0) - int(years * 365.25 * 86400)
    out = _git(root, "-c", "core.quotePath=false", "log", "--no-merges", "-M", "--name-status", "-z", "--format=%x01%H",
               f"-n{max_commits}", f"--since={since}", sha, "--", ".")   # . : only the folder mapped
    if out is None:
        return None
    commits = []
    for chunk in out.split(b"\x01")[1:]:
        tokens = chunk.split(b"\0")[1:]   # the first is the commit's sha
        changes, k = [], 0
        while k < len(tokens):
            status = tokens[k].decode("utf-8", "replace").strip()
            if not status:
                k += 1
                continue
            width = 3 if status[:1] in ("R", "C") else 2
            paths = [t.decode("utf-8", "replace") for t in tokens[k + 1:k + width]]
            if len(paths) == width - 1:
                changes.append((status[:1], *paths))
            k += width
        commits.append(changes)
    return commits


def _folders(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:k]) + "/" for k in range(1, len(parts) + 1)]


def compute(root: Path, sha: str, max_commits: int = MAX_COMMITS, years: float = YEARS, bulk: int = BULK) -> Optional[dict]:
    """Read the history behind `sha` and count, for each file, each pair of files (a < b) and each file and folder
    outside it, the commits that changed them. None outside git."""
    commits = _commits(root, sha, max_commits, years)
    if commits is None:
        return None
    # A repository mapped from a folder inside a larger git repository: git names files from the top, the map from
    # the folder. Only the folder's files count, under the map's names.
    prefix = (_git(root, "rev-parse", "--show-prefix") or b"").decode("utf-8", "replace").strip()
    listed = _git(root, "ls-tree", "-r", "-z", "--full-tree", "--name-only", sha)
    alive = {p.decode("utf-8", "replace") for p in (listed or b"").split(b"\0") if p}
    alive = {p[len(prefix):] for p in alive if p.startswith(prefix)}
    now: dict[str, str] = {}   # an older name -> the name the file has at `sha`
    changes: Counter = Counter()
    pairs: Counter = Counter()
    dirs: Counter = Counter()
    read = skipped = 0
    for c in commits:   # newest first, so a rename is seen before the commits that used the old name
        files = set()
        for ch in c:
            if ch[0] in ("R", "C"):
                old, new = ch[1], ch[2]
                cur = now.get(new, new)
                if ch[0] == "R":
                    now[old] = cur
                files.add(cur)
            else:
                files.add(now.get(ch[1], ch[1]))
        if not files:
            continue
        if len(files) > bulk:
            skipped += 1
            continue
        files = sorted(f[len(prefix):] for f in files if f.startswith(prefix) and f[len(prefix):] in alive
                       and not f[len(prefix):].startswith(".leyline/"))
        if not files:
            continue
        read += 1
        changes.update(files)
        pairs.update(itertools.combinations(files, 2))
        touched = {d for f in files for d in _folders(f)}
        for f in files:
            own = set(_folders(f))
            dirs.update((f, d) for d in touched if d not in own)
    return {"commits": read, "bulk": skipped, "changes": dict(changes), "pairs": dict(pairs), "dirs": dict(dirs)}


TABLES = ("coupling_runs", "coupling_files", "coupling_pairs", "coupling_dirs")


def ensure(con, repo: str, root: Path, sha: Optional[str] = None) -> Optional[dict]:
    """The coupling run for a repository at a commit (HEAD by default), worked out now if the store does not hold it.
    None when the repository is not under git or has no commits."""
    root = Path(root)
    sha = sha or head(root)
    if not sha:
        return None
    row = con.execute("SELECT commits, bulk, seconds FROM coupling_runs WHERE repo_id = ? AND sha = ?", (repo, sha)).fetchone()
    if row is not None:
        return {"repo": repo, "sha": sha, "commits": row[0], "bulk": row[1], "seconds": row[2], "cached": True}
    t0 = time.perf_counter()
    r = compute(root, sha)
    if r is None:
        return None
    seconds = round(time.perf_counter() - t0, 2)
    with con:
        old = [x[0] for x in con.execute("SELECT sha FROM coupling_runs WHERE repo_id = ? ORDER BY created DESC", (repo,))][KEEP_RUNS - 1:]
        for s in old + [sha]:
            for table in TABLES:
                con.execute(f"DELETE FROM {table} WHERE repo_id = ? AND sha = ?", (repo, s))
        con.execute("INSERT INTO coupling_runs VALUES (?,?,?,?,?,?)", (repo, sha, r["commits"], r["bulk"], seconds, time.time()))
        con.executemany("INSERT INTO coupling_files VALUES (?,?,?,?)", ((repo, sha, p, n) for p, n in r["changes"].items()))
        con.executemany("INSERT INTO coupling_pairs VALUES (?,?,?,?,?)",
                        ((repo, sha, a, b, n) for (a, b), n in r["pairs"].items() if n >= KEEP))
        con.executemany("INSERT INTO coupling_dirs VALUES (?,?,?,?,?)",
                        ((repo, sha, f, d, n) for (f, d), n in r["dirs"].items() if n >= KEEP))
    return {"repo": repo, "sha": sha, "commits": r["commits"], "bulk": r["bulk"], "seconds": seconds, "cached": False}


def partners(con, run: dict, paths: Iterable[str], min_together: int = MIN_TOGETHER,
             min_confidence: float = MIN_CONFIDENCE, folders: bool = False) -> dict[str, list[dict]]:
    """path -> the files that usually change with it, strongest first: {path, together, changes (of the first file),
    confidence, partner_changes, support}. With `folders`, also folders whose files often change with it and that no
    deeper folder or single file in them explains ({..., "folder": True}, path ending in /)."""
    repo, sha, total = run["repo"], run["sha"], max(run["commits"], 1)
    out = {}
    for p in dict.fromkeys(paths):
        row = con.execute("SELECT changes FROM coupling_files WHERE repo_id = ? AND sha = ? AND path = ?", (repo, sha, p)).fetchone()
        if row is None or row[0] < min_together:
            out[p] = []
            continue
        n = row[0]
        rows = con.execute(
            "SELECT CASE WHEN c.a = ? THEN c.b ELSE c.a END AS other, c.together, f.changes FROM coupling_pairs c"
            " JOIN coupling_files f ON f.repo_id = c.repo_id AND f.sha = c.sha AND f.path = (CASE WHEN c.a = ? THEN c.b ELSE c.a END)"
            " WHERE c.repo_id = ? AND c.sha = ? AND (c.a = ? OR c.b = ?) AND c.together >= ?",
            (p, p, repo, sha, p, p, min_together)).fetchall()
        found = [{"path": r[0], "together": r[1], "changes": n, "confidence": round(r[1] / n, 2), "partner_changes": r[2],
                  "support": round(r[1] / total, 3)} for r in rows if r[1] / n >= min_confidence]
        if folders:
            ds = {r[0]: r[1] for r in con.execute("SELECT dir, together FROM coupling_dirs WHERE repo_id = ? AND sha = ?"
                                                  " AND path = ? AND together >= ?", (repo, sha, p, min_together))
                  if r[1] / n >= min_confidence}
            for d, k in ds.items():
                if d.count("/") < 2:   # a folder at the top (tests/, docs/) is too broad to act on
                    continue
                deeper = any(o != d and o.startswith(d) for o in ds)
                # one file in it already reported for most of the folder's commits: the folder adds nothing
                one_file = any(x["path"].startswith(d) and x["together"] >= 0.75 * k for x in found)
                if not deeper and not one_file:
                    found.append({"path": d, "together": k, "changes": n, "confidence": round(k / n, 2), "folder": True,
                                  "support": round(k / total, 3)})
        out[p] = sorted(found, key=lambda x: (-x["confidence"], -x["together"], x["path"]))
    return out


def strongest(con, run: dict, min_together: int = MIN_TOGETHER, min_confidence: float = MIN_CONFIDENCE, limit: int = 20) -> list[dict]:
    """The most coupled pairs of files in a repository, each in its stronger direction (from the file that changed
    less often, so the share is of its commits)."""
    repo, sha, total = run["repo"], run["sha"], max(run["commits"], 1)
    rows = con.execute(
        "SELECT c.a, c.b, c.together, fa.changes, fb.changes FROM coupling_pairs c"
        " JOIN coupling_files fa ON fa.repo_id = c.repo_id AND fa.sha = c.sha AND fa.path = c.a"
        " JOIN coupling_files fb ON fb.repo_id = c.repo_id AND fb.sha = c.sha AND fb.path = c.b"
        " WHERE c.repo_id = ? AND c.sha = ? AND c.together >= ?", (repo, sha, min_together)).fetchall()
    out = []
    for a, b, n, ca, cb in rows:
        first, other, cf, co = (a, b, ca, cb) if ca <= cb else (b, a, cb, ca)
        if n / cf >= min_confidence:
            out.append({"file": first, "path": other, "together": n, "changes": cf, "confidence": round(n / cf, 2),
                        "partner_changes": co, "support": round(n / total, 3)})
    out.sort(key=lambda x: (-x["together"], -x["confidence"], x["file"], x["path"]))
    return out[:limit]


def missed(con, run: dict, touched: Iterable[str], named, limit: int = 0, **thresholds) -> list[dict]:
    """Files (and folders) that usually change with one of `touched` and that `named(path)` says nothing covers,
    strongest first, each once, with the touched file it most often changed with. `named` is a set of paths or a
    function; a folder is covered when anything touched or named is in it."""
    is_named = named if callable(named) else (lambda p, s=set(named): p in s)
    touched = list(dict.fromkeys(touched))
    best: dict[str, dict] = {}
    also: dict[str, list] = {}
    for f, ps in partners(con, run, touched, folders=True, **thresholds).items():
        for x in ps:
            p = x["path"]
            if p in touched or is_named(p):
                continue
            if x.get("folder") and any(t.startswith(p) for t in touched):
                continue
            item = {"file": f, **x}
            also.setdefault(p, []).append(f)
            if p not in best or (x["confidence"], x["together"]) > (best[p]["confidence"], best[p]["together"]):
                best[p] = item
    out = [{**x, "also_with": [f for f in also[p] if f != x["file"]]} for p, x in best.items()
           if not (x.get("folder") and any(o != p and o.startswith(p) for o in best))]   # said by what is in it
    out.sort(key=lambda x: (-x["confidence"], -x["together"], bool(x.get("folder")), x["path"]))
    return out[:limit] if limit else out


def line(x: dict, tail: str = "") -> str:
    """`B` changed in 7 of the 9 commits that changed `A`; <tail>."""
    also = f" (and often with {_list(x['also_with'], 2)})" if x.get("also_with") else ""
    what = f"Files in `{x['path']}` changed" if x.get("folder") else f"`{x['path']}` changed"
    return (f"{what} in {x['together']} of the {x['changes']} commits that changed `{x['file']}`{also}"
            + (f"; {tail}" if tail else ""))


def _list(xs: list[str], k: int) -> str:
    return ", ".join(f"`{x}`" for x in xs[:k]) + (f" and {len(xs) - k} more" if len(xs) > k else "")


def about(run: dict) -> str:
    """Where the numbers come from, said once above the lines."""
    bulk = f", leaving out {run['bulk']} that changed more than {BULK} files" if run.get("bulk") else ""
    return f"from the last {run['commits']} commits{bulk}"


# -- the spec loop and pull requests -------------------------------------------------------------------
PATHISH = re.compile(r"[\w.@+-]+(?:/[\w.@+-]+)*\.[A-Za-z0-9]+|[\w.@+-]+(?:/[\w.@+-]+)+/?")
# PATHISH tries every place a dot could end a run, from every place it could start: quadratic in a run's length, so a
# spec or pull request description with one long word would hold the plan for minutes. It only ever matches inside a
# run of these characters, so it is tried run by run, and a run longer than any path is not one.
_RUN = re.compile(r"[\w.@+/-]+")
LONGEST_PATH = 400


def written_paths(texts: Iterable[str], known: Iterable[str]) -> set:
    """The known paths that some text names: the whole path or its end (`validation/local.ts`, `EDITOR_GUIDE.md`),
    in backticks or not, or a folder they are in (`cases/`). A name that ends several paths names them all."""
    words = _words(texts)
    if not words:
        return set()
    folders = {w for w in words if "/" in w}
    out = set()
    for p in known:
        parts = p.split("/")
        if p in words or any("/".join(parts[k:]) in words for k in range(1, len(parts))):
            out.add(p)
        elif any(("/" + "/".join(parts[:k])).endswith("/" + w) for w in folders for k in range(1, len(parts))):
            out.add(p)   # a folder the text names covers what is in it
    return out


def _words(texts: Iterable[str]) -> set:
    words = {w.strip("`'\"()[],;:").lstrip("./").rstrip("/") for t in texts for run in _RUN.findall(t or "")
             if len(run) <= LONGEST_PATH for w in PATHISH.findall(run)}
    return {w for w in words if w}


def known_paths(con, run: dict) -> list[str]:
    return [r[0] for r in con.execute("SELECT path FROM coupling_files WHERE repo_id = ? AND sha = ?", (run["repo"], run["sha"]))]


def _covered(named: set):
    """A path is covered when it is named, and a folder when something named is in it (a new file or folder too:
    `tests/cases/missing-ok/` covers `tests/cases/`)."""
    return lambda p: p in named or (p.endswith("/") and any(n.startswith(p) for n in named))


def for_spec(con, names, links: list[dict], limit: int = 30) -> dict:
    """For the files a spec's tasks touch (the files of the code they name, and files they name by path), the files
    that usually change with them that no task names. {"about", "files", "total"}, or {} with no history to read."""
    texts = [l.get("text") or "" for l in links]
    by_repo: dict[str, set] = {}
    for l in links:
        ids = list(l.get("nodes") or []) + list(l.get("into") or []) + [n["parent"] for n in l.get("new") or [] if n.get("parent")]
        for i in ids:
            r = names.by_id.get(i)
            if r is not None and r["path"]:
                by_repo.setdefault(i.split(":", 1)[0], set()).add(r["path"])
    out, runs, fns = [], [], []
    for repo, root in store.roots(con).items():
        if not Path(root).is_dir():
            continue
        try:
            run = ensure(con, repo, root)
        except Exception:   # history is a lead, never a reason for the plan to fail
            run = None
        if run is None:
            continue
        named = written_paths(texts, known_paths(con, run))
        touched = sorted(by_repo.get(repo, set()) | named)
        if not touched:
            continue
        runs.append(run)
        written = {w + "/" for w in _words(texts) if "/" in w}   # paths not in the history yet, such as a new folder
        out += [{**x, "repo": repo} for x in missed(con, run, touched, _covered(named | set(touched) | written))]
        # Functions that usually changed with the functions the tasks name, and that no task names.
        tasked = [i for l in links for i in l.get("nodes") or [] if i.split(":", 1)[0] == repo]
        fns += _functions(con, repo, root, run["sha"], tasked, tasked)
    if not runs:
        return {}
    out.sort(key=lambda x: (-x["confidence"], -x["together"], bool(x.get("folder")), x["path"]))
    return {"about": about(_merged(runs)), "files": out[:limit], "total": len(out), "functions": fns[:limit]}


MAX_FUNCTIONS = 6   # functions whose own history is read, per plan or pull request (each costs a few parses)


def _functions(con, repo: str, root, sha: str, targets: list[str], named: Iterable[str]) -> list[dict]:
    """leyline.fncoupling for up to MAX_FUNCTIONS of `targets`, leaving out partners `named` covers (the function
    itself, or something it sits in or that sits in it)."""
    from . import fncoupling
    named = set(named)

    def covered(g):
        return any(g == n or g.startswith(n + ".") or g.startswith(n + "/") or n.startswith(g + ".") for n in named)
    targets = [i for i in dict.fromkeys(targets) if (con.execute("SELECT kind FROM nodes WHERE id = ?", (i,)).fetchone()
                                                      or [None])[0] in fncoupling.KINDS][:MAX_FUNCTIONS]
    if not targets:
        return []
    r = fncoupling.missed(con, repo, Path(root), sha, targets, covered)
    return [{**x, "repo": repo} for x in r.get("functions") or []]


def changed_since(root: Path, base: str) -> list[str]:
    """Files the checkout changed since `base`: committed, uncommitted and new."""
    out = set()
    for args in (("diff", "--relative", "--name-only", "-z", "--no-renames", base),
                 ("ls-files", "-z", "--others", "--exclude-standard")):
        listed = _git(root, "-c", "core.quotePath=false", *args)
        out |= {p.decode("utf-8", "replace") for p in (listed or b"").split(b"\0") if p}
    return sorted(p for p in out if not p.startswith(".leyline/"))


def for_pr(con, repo: str, root: Path, base_sha: str, limit: int = 30, functions: Iterable[str] = (),
           changed_ids: Iterable[str] = ()) -> dict:
    """For the files a branch changed, the files that usually changed with them in the history before the branch and
    that the branch did not change. {"about", "files", "total", "functions"}, or {} with no history to read. With
    `functions` (functions the branch edited), also the functions that usually changed with them and that are not in
    `changed_ids`."""
    try:
        run = ensure(con, repo, root, base_sha)
    except Exception:
        run = None
    if run is None:
        return {}
    changed = changed_since(Path(root), base_sha)
    found = missed(con, run, changed, _covered(set(changed)))
    fns = _functions(con, repo, root, base_sha, list(functions), set(functions) | set(changed_ids)) if functions else []
    return {"about": about(run) + " before the branch", "files": found[:limit], "total": len(found), "functions": fns[:limit]}


def _merged(runs: list[dict]) -> dict:
    return {"commits": sum(r["commits"] for r in runs), "bulk": sum(r["bulk"] for r in runs)}


# -- the coupling command and tool ------------------------------------------------------------------------------
def query(con, path: Optional[str] = None, min_together: int = MIN_TOGETHER, min_confidence: float = MIN_CONFIDENCE,
          limit: int = 20, cwd: Optional[Path] = None) -> dict:
    """A file's partners (files, and folders no single file explains), or with no path the most coupled pairs."""
    roots = {r: Path(p) for r, p in store.roots(con).items() if Path(p).is_dir()}
    if not roots:
        return {"error": "the store does not say where any repository is; run `leyline map` first"}
    runs = {}
    for repo, root in roots.items():
        run = ensure(con, repo, root)
        if run is not None:
            runs[repo] = run
    if not runs:
        return {"error": "no repository in the store is under git, so there is no history to read"}
    thresholds = {"min_together": min_together, "min_confidence": min_confidence}
    if not path:
        pairs = [{**x, "repo": repo} for repo, run in runs.items() for x in strongest(con, run, limit=limit, **thresholds)]
        pairs.sort(key=lambda x: (-x["together"], -x["confidence"]))
        return {"about": about(_merged(list(runs.values()))), "pairs": pairs[:limit], **thresholds}
    hits = _find(con, runs, roots, path, cwd or Path.cwd())
    if not hits:
        return {"error": f"no file {path!r} in the history read ({about(_merged(list(runs.values())))}); give its path"
                         " in the repository"}
    if len(hits) > 1:
        return {"error": f"{path!r} could be {len(hits)} files: " + ", ".join(p for _, p in hits[:8]) + "; give more of the path"}
    repo, p = hits[0]
    run = runs[repo]
    row = con.execute("SELECT changes FROM coupling_files WHERE repo_id = ? AND sha = ? AND path = ?", (repo, run["sha"], p)).fetchone()
    ps = partners(con, run, [p], folders=True, **thresholds)[p]
    return {"repo": repo, "path": p, "changes": row[0] if row else 0, "about": about(run), **thresholds,
            "partners": ps[:limit], "total": len(ps)}


def _find(con, runs: dict, roots: dict, path: str, cwd: Path) -> list[tuple[str, str]]:
    """(repo, path) pairs a written path can be: relative to here, to a repository, or the end of a path."""
    given = Path(path)
    full = given if given.is_absolute() else cwd / given
    for repo, root in roots.items():
        try:
            rel = full.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        if repo in runs and rel in set(known_paths(con, runs[repo])):
            return [(repo, rel)]
    name = path.strip().lstrip("./")
    exact = [(repo, name) for repo, run in runs.items() if name in set(known_paths(con, run))]
    if exact:
        return exact
    return [(repo, p) for repo, run in runs.items() for p in known_paths(con, run) if p.endswith("/" + name)]


def text(r: dict) -> str:
    """The coupling command's answer, for a person."""
    if "error" in r:
        return "leyline: " + r["error"]
    rule = f"at least {r['min_together']} commits together, and {round(r['min_confidence'] * 100)}% of the first file's"
    if "pairs" in r:
        if not r["pairs"]:
            return f"No two files usually change together ({r['about']}; {rule})."
        L = [f"Files that usually change together ({r['about']}; {rule}):", ""]
        L += [f"- {line(x)}" for x in r["pairs"]]
        return "\n".join(L)
    if not r["partners"]:
        return (f"`{r['path']}` changed in {r['changes']} commits ({r['about']}); nothing usually changed with it"
                f" ({rule.replace('first file', 'its')}).")
    L = [f"`{r['path']}` changed in {r['changes']} commits ({r['about']}). What usually changed with it:", ""]
    L += [f"- {'files in ' if x.get('folder') else ''}`{x['path']}`: {x['together']} of {x['changes']}"
          f" ({round(x['confidence'] * 100)}%)" for x in r["partners"]]
    if r["total"] > len(r["partners"]):
        L.append(f"- and {r['total'] - len(r['partners'])} more (--limit)")
    return "\n".join(L)
