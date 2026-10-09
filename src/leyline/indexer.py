"""Walk a repo, run the language adapters, resolve names across files, and write facts."""

from __future__ import annotations

import gc
import hashlib
import itertools
import posixpath
import json
import os
import pickle
import re
import subprocess
import time
import sys
from array import array
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from . import packing, store
from .adapters import BY_EXTENSION, BY_LANGUAGE
from .adapters.python import module_path
from .model import CallSite, Edge, Endpoint, FieldUse, FileResult, Node

SOURCE = "leyline-indexer/0.1"
MODULE_MARKERS = ("pyproject.toml", "setup.py", "package.json", "__init__.py")
# Languages where a file is a module and other files name what they take from it (import { a } from "./b").
FILE_MODULE = ("python", "typescript")
CTORS = (".ctor", "__init__", "constructor")
NODE_BUILTINS = frozenset("""assert async_hooks buffer child_process cluster console constants crypto dgram dns domain
events fs http http2 https inspector module net os path perf_hooks process punycode querystring readline repl stream
string_decoder timers tls tty url util v8 vm worker_threads zlib test""".split())
SKIP_DIRS = {".git", "node_modules", "bin", "obj", "__pycache__", ".venv", "venv", ".godot", ".leyline"}


# Directories of other people's code, left out even when committed: mapped, they bury the repository's own code.
DEPENDENCY_DIRS = {"node_modules", "bower_components", "site-packages", "__pycache__"}
# A vendor directory is only someone else's code when a package manager says so (Go modules, Composer).
VENDOR_MARKERS = ("vendor/modules.txt", "vendor/autoload.php")
MAX_FILE_MB = 5          # a source file larger than this is generated; parsing one costs seconds and hundreds of MB
LONG_LINES = 2000        # average characters per line above which a file is minified or generated, not written


def _git(root: Path, *args: str, raw: bool = False, timeout: float = 30):
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=timeout,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # git writes paths and names as UTF-8 bytes whatever the platform's locale is
    return out.stdout if raw else out.stdout.decode("utf-8", "replace").strip()


def max_file_bytes() -> int:
    env = os.environ.get("LEYLINE_MAX_FILE_MB", "")
    try:
        return int(float(env) * 2**20) if env else MAX_FILE_MB * 2**20
    except ValueError:
        return MAX_FILE_MB * 2**20


class Listing:
    """The files of a repository that can be read, and the ones left out with the reason for each. `how` is git or
    walk; `why_walk` says why git did not list them, when it is a git repository."""

    def __init__(self, files: list[str], skipped: list[tuple[str, str]], how: str, why_walk: Optional[str] = None):
        self.files, self.skipped, self.how, self.why_walk = files, skipped, how, why_walk


def scan(root: Path) -> Listing:
    """Every file to index: what git lists (tracked, and untracked but not ignored), or a walk of the directory when
    it is not a git repository. Anything that cannot be read as a source file is left out, with a reason, so that
    nothing later fails on it: entries git lists that are not files on disk (submodules, deleted files, symlinks to
    directories or to nothing), files outside the repository, other people's code, and source files that are too
    large, binary, minified or unreadable."""
    root = Path(root)
    skipped: list[tuple[str, str]] = []
    why_walk = None
    out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", raw=True, timeout=600)
    if out is not None:
        names, how = [], "git"
        for b in out.split(b"\0"):
            if not b:
                continue
            try:
                names.append(b.decode("utf-8"))
            except UnicodeDecodeError:
                skipped.append((b.decode("utf-8", "replace"), "file name is not UTF-8"))
        names = sorted(set(n.rstrip("/") for n in names))   # an untracked nested repository is listed as "dir/"
    else:
        names, how = _walk(root, skipped), "walk"
        if (root / ".git").exists():
            try:
                run = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=600,
                                     stdin=subprocess.DEVNULL)
                why_walk = run.stderr.decode("utf-8", "replace").strip().splitlines()
                why_walk = why_walk[0] if why_walk else f"git exited with {run.returncode}"
            except (OSError, subprocess.SubprocessError) as e:
                why_walk = f"git could not be run ({e})"
    vendored = {m[: -len(m.rsplit("/", 1)[-1])] for m in names
                if any(m == v or m.endswith("/" + v) for v in VENDOR_MARKERS)}
    try:
        real_root = root.resolve()
    except OSError:
        real_root = root
    limit = max_file_bytes()
    files, listed = [], set(names)
    for f in names:
        parts = f.split("/")
        if set(parts[:-1]) & DEPENDENCY_DIRS or any(f.startswith(v) for v in vendored):
            skipped.append((f, "dependency directory"))
            continue
        why = _unusable(root, real_root, f, BY_EXTENSION, limit, listed)
        if why:
            skipped.append((f, why))
        else:
            files.append(f)
    return Listing(files, skipped, how, why_walk)


def _walk(root: Path, skipped: list) -> list[str]:
    """The files under root when git cannot list them. Symlinked directories are not followed (a link back up the
    tree would never end), nested repositories and build or tool directories are left out, and .gitignore files
    are applied (the common patterns; see _ignore_rules)."""
    files = []

    def err(e: OSError) -> None:
        try:
            rel = Path(e.filename).relative_to(root).as_posix() if e.filename else "?"
        except ValueError:
            rel = str(e.filename)
        skipped.append((rel, f"cannot read directory: {e.strerror or e}"))
    rules: dict[str, list] = {}   # directory -> the ignore rules in force there, its own last
    for d, dirs, names in os.walk(root, onerror=err):
        rel = Path(d).relative_to(root).as_posix()
        rel = "" if rel == "." else rel + "/"
        here = rules.get(rel[:-1].rpartition("/")[0] + "/" if "/" in rel[:-1] else "", []) if rel else []
        if ".gitignore" in names:
            here = here + _ignore_rules(Path(d) / ".gitignore", rel)
        rules[rel] = here
        keep = []
        for x in dirs:
            p = os.path.join(d, x)
            if x in SKIP_DIRS or x in DEPENDENCY_DIRS or _ignored(here, rel + x, True):
                continue
            if os.path.islink(p):
                skipped.append((rel + x, "symlink to a directory"))
            elif os.path.exists(os.path.join(p, ".git")):
                skipped.append((rel + x, "nested repository (map it on its own, or with this one as a workspace)"))
            else:
                keep.append(x)
        dirs[:] = sorted(keep)
        for x in names:
            try:
                (rel + x).encode("utf-8")
            except UnicodeEncodeError:
                skipped.append(((rel + x).encode("utf-8", "surrogateescape").decode("utf-8", "replace"),
                                "file name is not UTF-8"))
                continue
            if not _ignored(here, rel + x, False):
                files.append(rel + x)
    return sorted(files)


def _ignore_rules(path: Path, base: str) -> list[tuple]:
    """A .gitignore's patterns as (regex, negated, directories only). Covers what ignore files mostly hold: names,
    globs with * ? [..] and **, a leading / or an inner / to anchor, a trailing / for directories, and ! to
    take a path back. Escapes and trailing-space rules are not handled."""
    import re as _re
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        if neg:
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/") if dir_only else line
        anchored = "/" in line   # a leading or inner slash; the trailing one only says "a directory"
        line = line.lstrip("/")
        if not line:
            continue
        rx, i = "", 0
        while i < len(line):
            c = line[i]
            if line.startswith("**/", i):
                rx, i = rx + "(?:.*/)?", i + 3
            elif line.startswith("/**", i) and i + 3 == len(line):
                rx, i = rx + "/.*", i + 3
            elif line.startswith("**", i):
                rx, i = rx + ".*", i + 2
            elif c == "*":
                rx, i = rx + "[^/]*", i + 1
            elif c == "?":
                rx, i = rx + "[^/]", i + 1
            elif c == "[" and "]" in line[i + 1:]:
                j = line.index("]", i + 1)
                body = line[i + 1:j]
                rx, i = rx + "[" + ("^" + body[1:] if body.startswith("!") else body) + "]", j + 1
            else:
                rx, i = rx + _re.escape(c), i + 1
        prefix = _re.escape(base) if anchored else _re.escape(base) + "(?:.*/)?"
        out.append((_re.compile(prefix + rx + "$"), neg, dir_only))
    return out


def _ignored(rules: list, path: str, is_dir: bool) -> bool:
    hit = False
    for rx, neg, dir_only in rules:   # the last rule that matches decides, as in git
        if (is_dir or not dir_only) and rx.match(path):
            hit = not neg
    return hit


def _unusable(root: Path, real_root: Path, f: str, sources: dict, limit: int, listed: set) -> Optional[str]:
    """Why a listed path cannot be indexed, or None. Only source files (those an adapter reads) are opened."""
    import errno
    import stat as st
    p = root / f
    try:
        info = os.lstat(p)
    except FileNotFoundError:
        return "deleted (git still lists it)"
    except OSError as e:
        return f"cannot read: {e.strerror or e}"
    except ValueError:   # UnicodeEncodeError: a name the file system's encoding (the locale's) cannot spell
        return "file name cannot be spelled in this system's encoding (use a UTF-8 locale)"
    if st.S_ISLNK(info.st_mode):
        try:
            target = p.resolve(strict=True)
            info = os.stat(p)
        except FileNotFoundError:
            return "broken symlink"
        except RuntimeError:   # a symlink loop, on Python before 3.13
            return "symlink loop"
        except OSError as e:
            return "symlink loop" if e.errno == errno.ELOOP else f"cannot read: {e.strerror or e}"
        if st.S_ISDIR(info.st_mode):
            return "symlink to a directory"
        try:
            inside = target.relative_to(real_root).as_posix()
        except ValueError:
            return "symlink to outside the repository"
        if inside in listed:
            return "symlink to another listed file (indexed there)"
    if st.S_ISDIR(info.st_mode):
        return "submodule or nested repository (map it on its own, or with this one as a workspace)"
    if not st.S_ISREG(info.st_mode):
        return "not a regular file"
    ext = "." + f.rsplit(".", 1)[-1] if "." in f.rsplit("/", 1)[-1] else ""
    if ext not in sources:
        return None
    if info.st_size > limit:
        return f"larger than {limit / 2**20:g} MB (generated?)"
    if f.endswith((".min.js", ".min.mjs", ".min.cjs")):
        return "minified"
    try:
        with open(p, "rb") as fh:
            head = fh.read(65536)
    except OSError as e:
        return f"cannot read: {e.strerror or e}"
    if _binary(head[:8192]):
        return "binary"
    if len(head) == 65536 and head.count(b"\n") < 65536 // LONG_LINES:
        return "minified or generated (very long lines)"
    return None


_CONTROL = bytes(b for b in range(32) if b not in (9, 10, 12, 13, 27)) + b"\x7f"


def _binary(head: bytes) -> bool:
    """Whether the start of a file is data rather than text. A NUL byte alone does not decide it (source can hold
    one inside a string literal); one byte in twenty being a control character does. UTF-16 text is half NULs, so a
    byte order mark lets it through."""
    if not head or head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    return len(head) - len(head.translate(None, _CONTROL)) > len(head) // 20


def list_files(root: Path) -> list[str]:
    return scan(root).files


def _report_skipped(repo: str, skipped: list) -> None:
    """One line per reason on stderr, with a few of the files, so a person can see what the map leaves out."""
    quiet = ("dependency directory", "deleted (git still lists it)", "symlink to another listed file (indexed there)")
    by = defaultdict(list)
    for f, why in skipped:
        by[why].append(f)
    for why, fs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if why in quiet:
            line = f"leyline: {repo}: left out {len(fs)} file{'s' * (len(fs) != 1)}: {why}"
        else:
            shown = ", ".join(fs[:3]) + (f" and {len(fs) - 3} more" if len(fs) > 3 else "")
            line = f"leyline: {repo}: left out {why}: {shown}"
        if REPORTED is None or line not in REPORTED:   # a command that maps twice (a pull request's base and head)
            if REPORTED is not None:                     # says it once
                REPORTED.add(line)
            print(line, file=sys.stderr)


REPORTED: Optional[set] = None   # the lines said so far by the command running (leyline.cli sets it)


def skipped_summary(skipped: list, failed: list) -> dict:
    """What a run left out and why, for the stats and the store: reason -> count and a few examples."""
    out: dict = {}
    for f, why in list(skipped) + [(f, "failed to parse: " + (w or "")) for f, w in failed]:
        key = why if not why.startswith("failed to parse") else "failed to parse"
        got = out.setdefault(key, {"count": 0, "examples": []})
        got["count"] += 1
        if len(got["examples"]) < 5:
            got["examples"].append(f if key != "failed to parse" else f"{f}: {why[len('failed to parse: '):]}")
    return out


# The placeholders a table-driven test's title is filled in from (it.each, test.each, pytest ids): printf-style
# %s %d %j ..., $name and ${name} from a table, and Jest's %# for the row number.
_TITLE_SLOT = re.compile(r"%[sdifjoOpc#]|\$\{?[A-Za-z_][\w.]*\}?")


def _test_title(n) -> str:
    """A test's name as a flow shows it. A title that is a template (`%s/%s` from it.each) says little on its own,
    so it is shown after its suite, with each placeholder as an ellipsis."""
    title = n.name
    if not _TITLE_SLOT.search(title):
        return title
    shown = _TITLE_SLOT.sub("…", title.replace("%%", "%")).strip()
    suite = _TITLE_SLOT.sub("…", str(n.attrs.get("suite") or "")).strip()
    return f"{suite} > {shown}" if suite else shown


def read_source(path: Path) -> bytes:
    """A source file's bytes as the parsers take them: UTF-8. A UTF-16 file (it starts with a byte order mark,
    as some Windows editors write C#) is converted; lines stay where they were."""
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16").encode("utf-8")
        except UnicodeDecodeError:
            return data
    return data


def source_lines(path: Path) -> list[str]:
    """A source file's lines, numbered as the parsers number them: only a newline ends a line (str.splitlines also
    breaks at form feeds and other separators, which put every later line out of step with the syntax tree)."""
    return text_lines(read_source(path))


def text_lines(data: bytes) -> list[str]:
    return [ln[:-1] if ln.endswith("\r") else ln for ln in data.decode("utf-8", "replace").split("\n")]


def _module_dirs(files: list[str]) -> set[str]:
    """Directories that are modules because they hold a project marker."""
    dirs, packages = set(), set()
    for f in files:
        d, _, base = f.rpartition("/")
        if base.endswith(".csproj") or base in MODULE_MARKERS:
            dirs.add(d)
            if base == "__init__.py":
                packages.add(d)
    # A nested __init__.py belongs to its top-most package, not to a module of its own. A project file
    # inside another project's folder (a workspace package) is a module of its own. Both are looked up by
    # directory: scanning every file per package, and every directory per directory, was quadratic.
    projects = {f.rpartition("/")[0] for f in files
                if f.endswith(".csproj") or f.rsplit("/", 1)[-1] in MODULE_MARKERS[:3]}
    own = {d for d in dirs if d not in packages or d in projects}

    def nested(d: str) -> bool:
        parts = d.split("/")
        return any("/".join(parts[:k]) in dirs for k in range(1, len(parts)))
    return {d for d in dirs if d in own or not nested(d)}


def _module_for(path: str, module_dirs: set[str]) -> str:
    d = path.rpartition("/")[0]
    cur = d
    while True:
        if cur in module_dirs and cur != "":
            return cur
        if "/" not in cur:
            break
        cur = cur.rpartition("/")[0]
    if cur in module_dirs and cur != "":
        return cur
    return d  # no marker: the file's own directory ("" is the repo root)


def _ext_clashes(work: list) -> set[str]:
    """The files whose ids keep their extension: files of one language in one directory that differ only in their
    extension (a.js beside a.ts, m.c beside m.h) would name their contents alike. The first by path keeps the usual
    ids, so a directory with no such pair keeps them all; the others keep the extension in theirs."""
    import inspect
    groups: dict[tuple, list] = defaultdict(list)
    for w in work:
        adapter = BY_EXTENSION[w[1]]
        if "keep_ext" in inspect.signature(adapter.parse).parameters:
            groups[(adapter.LANGUAGE, adapter.module_path(w[0]))].append(w[0])
    return {f for fs in groups.values() if len(fs) > 1 for f in sorted(fs)[1:]}


def _arity(type_id: str) -> int:
    tail = type_id.rsplit("`", 1)
    return int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else 0


NOT_TYPES = ("String", "Self", "None", "Some", "Ok", "Err")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _declared(text: list[str], start: Optional[int], end: Optional[int]) -> dict:
    """Variables a span of text declares with a type: name -> the type names written for it, in the order that
    decides (the first, then any later one), so that which of them are in the repo can be settled later."""
    body = "\n".join(text[(start or 1) - 1:end or start or 1])
    out: dict[str, list] = {}
    for pat, gn, gt in Indexer._DECL:
        for m in pat.finditer(body):
            name, tname = m.group(gn), m.group(gt)
            seen = out.setdefault(name, [])
            if tname not in seen:
                seen.append(tname)
    return out


def _parse_one(job):
    """Parse one file: (path, ext, module dir, module id, lines, sha1, pickled (result, declarations) or None,
    error or None, the names the result mentions). Runs in a worker process on large repositories, so it touches
    nothing but its arguments. The result comes back pickled because that is how it would cross from the worker
    anyway, and the same bytes are what the parse cache keeps (leyline.incremental); its calls are packed apart
    (leyline.packing)."""
    from .incremental import mentions
    try:
        out = _parse_file(job)
        res, decls = out[6], out[8]
        if res is None:
            return (*out[:6], None, out[7], b"")
        return (*out[:6], packing.pack(res, decls), None, mentions(res, decls))
    except Exception as exc:   # MemoryError, or a result that will not pickle: the file fails, not the run
        f, ext, mod_dir, mod_id = job[2]
        return f, ext, mod_dir, mod_id, 1, None, None, f"{type(exc).__name__}: {exc}"[:300], b""


DEEP_RECURSION = 25_000   # frames allowed when a file is parsed again on a large stack; past this, the work grows too fast


def _deep(fn, *args):
    """fn(*args) on a thread with a 1 GB stack and a recursion limit to match. The adapters walk the syntax tree
    recursively; a file nested deeper than the default limit allows (a long else-if chain, a generated expression)
    is read this way instead of being lost. The stack is only reserved address space until it is used."""
    import threading
    out: list = []

    def run():
        try:
            out.append((True, fn(*args)))
        except BaseException as exc:   # handed back to the caller's thread
            out.append((False, exc))
    if os.name == "nt" and sys.version_info < (3, 12):
        # Python before 3.12 on Windows does not stop a deep recursion before the thread's stack runs out: the whole
        # process dies. The file is reported as too deeply nested instead.
        raise RecursionError("nested too deeply to read")
    old_stack, old_limit = threading.stack_size(), sys.getrecursionlimit()
    try:
        threading.stack_size(2**30)
    except (ValueError, RuntimeError):   # a platform that will not give a thread a large stack
        raise RecursionError("nested too deeply to read")
    try:
        sys.setrecursionlimit(max(old_limit, DEEP_RECURSION))
        t = threading.Thread(target=run, name="leyline-deep-parse")
        t.start()
        t.join()
    finally:
        threading.stack_size(old_stack)
        sys.setrecursionlimit(old_limit)
    ok, value = out[0]
    if not ok:
        raise value
    return value


def _parse_file(job):
    root, repo, (f, ext, mod_dir, mod_id) = job[:3]
    try:
        data = read_source(Path(root) / f)
    except OSError as exc:   # gone or made unreadable since it was listed
        return f, ext, mod_dir, mod_id, 1, None, None, f"cannot read: {exc.strerror or exc}", None
    loc, sha = data.count(b"\n") + 1, hashlib.sha1(data).hexdigest()
    adapter = BY_EXTENSION[ext]
    args = (repo, f, f"{repo}:file:{f}", data, mod_dir or ".")
    parse = adapter.parse
    if len(job) > 3 and job[3]:   # see _ext_clashes
        import functools
        parse = functools.partial(adapter.parse, keep_ext=True)
    try:
        try:
            res = parse(*args)
        except RecursionError:
            res = _deep(parse, *args)
    except RecursionError:
        return f, ext, mod_dir, mod_id, loc, sha, None, "nested too deeply to read", None
    except Exception as exc:
        return f, ext, mod_dir, mod_id, loc, sha, None, f"{type(exc).__name__}: {exc}"[:300], None
    lines = data.split(b"\n")
    for n in res.nodes:
        if n.span_start and n.kind in ("type", "callable", "test", "field"):
            # A hash of the node's own text, so a later index can tell which nodes were edited.
            n.content_hash = hashlib.sha1(b"\n".join(ln.strip() for ln in lines[n.span_start - 1:n.span_end])).hexdigest()[:16]
    decls = None
    if getattr(adapter, "GENERIC", False):
        # The generic resolver reads variable types out of function and type text. Reading it here puts that
        # work in the parallel part; the text is read the way Indexer._file_text reads it, so the answer is the same.
        text = text_lines(data)
        if text is not None:
            # Only where a call names a variable receiver is the type looked for; any other place falls back to
            # reading the text in the main process.
            holders, todo = set(), list(res.calls)
            while todo:
                c = todo.pop()
                if c.receiver not in (None, "this", "base", "?") and _IDENT.fullmatch(c.receiver):
                    holders.update((c.src_id, c.enclosing_type))
                if c.chain is not None:
                    todo.append(c.chain)
            decls = {n.id: (n.span_start, n.span_end, _declared(text, n.span_start, n.span_end))
                     for n in res.nodes if n.id in holders}
    return f, ext, mod_dir, mod_id, loc, sha, res, None, decls


PARALLEL_MIN_FILES = 300   # below this, starting worker processes costs more than it saves


def _jobs() -> int:
    env = os.environ.get("LEYLINE_JOBS")
    if env and env.isdigit():
        return max(1, int(env))
    return max(1, min(os.cpu_count() or 1, 16))


def _start_method() -> Optional[str]:
    """How worker processes start. fork is quickest, but it is only safe in a process with one thread (a fork
    taken while another thread holds a lock deadlocks the child), and macOS and Windows do not have it safely or
    at all; spawn works everywhere. LEYLINE_START_METHOD picks one (fork, forkserver or spawn)."""
    import multiprocessing
    import threading
    env = os.environ.get("LEYLINE_START_METHOD")
    methods = multiprocessing.get_all_start_methods()
    if env:
        if env not in methods:
            raise ValueError(f"LEYLINE_START_METHOD={env!r}: this platform has {', '.join(methods)}")
        return env
    if sys.platform.startswith("linux") and "fork" in methods and threading.active_count() == 1:
        return "fork"
    return "spawn"


CHUNK = 16   # files a worker takes at a time; small, so that few files are in flight when a worker dies


def _timeout() -> float:
    """Seconds a worker may spend on one batch of files (or, retried alone, one file) before it is taken to be
    stuck and stopped. LEYLINE_PARSE_TIMEOUT changes it."""
    try:
        return max(1.0, float(os.environ.get("LEYLINE_PARSE_TIMEOUT", "") or 600))
    except ValueError:
        return 600.0


def _parse_all(root: Path, repo: str, work: list, died: Optional[list] = None, keep_ext: frozenset = frozenset()):
    """Parse every file, across processes when there are enough files to pay for starting them.
    Results come back in the order given, so the index is the same however many processes ran.
    A worker that dies (a crash inside a parser, or the system killing it for memory) or gets stuck loses only the
    file it was on: the files that were in flight are parsed again one at a time, each in a fresh process, and the
    one that kills or stalls its process is reported as failed (and named in `died`)."""
    jobs = _jobs()
    items = [(str(root), repo, w, w[0] in keep_ext) for w in work]
    if jobs == 1 or len(items) < PARALLEL_MIN_FILES:
        yield from map(_parse_one, items)
        return
    import multiprocessing
    from collections import deque
    from concurrent.futures import ProcessPoolExecutor, TimeoutError as Stuck
    from concurrent.futures.process import BrokenProcessPool
    ctx = multiprocessing.get_context(_start_method())
    limit = _timeout()

    def pool(n):
        return ProcessPoolExecutor(max_workers=n, mp_context=ctx, initializer=_worker_init)

    chunks = [items[i:i + CHUNK] for i in range(0, len(items), CHUNK)]
    pos = 0
    worked = {"any": False}   # whether a worker has ever returned a result in this run
    while pos < len(chunks):
        ahead: deque = deque()
        nxt = pos
        ex = pool(jobs)
        try:
            while pos < len(chunks):
                # A few chunks per worker are handed out ahead of the one awaited: enough to keep every worker
                # busy, few enough that when one dies the file that killed it is among them.
                while nxt < len(chunks) and len(ahead) < 4 * jobs:
                    ahead.append(ex.submit(_parse_chunk, chunks[nxt]))
                    nxt += 1
                try:
                    done = ahead[0].result(timeout=limit)
                except Stuck:
                    _kill(ex)
                    raise BrokenProcessPool("stuck") from None
                ahead.popleft()
                pos += 1
                worked["any"] = True
                yield from done
        except BrokenProcessPool:
            suspects = [it for c in chunks[pos:nxt] for it in c]
            ex.shutdown(wait=True)
            yield from _parse_alone(suspects, pool, died, limit, worked)
            if worked.get("broken"):   # workers cannot run here at all: the rest is parsed in this process
                yield from map(_parse_one, [it for c in chunks[nxt:] for it in c])
                return
            pos = nxt
        finally:
            ex.shutdown(wait=True)


def _worker_init() -> None:
    # Ctrl+C reaches every process in the group; the main process stops the run, and the workers stay quiet
    # instead of each printing a traceback.
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _kill(ex) -> None:
    for proc in list(getattr(ex, "_processes", {}).values()):   # the pool has no public way to stop a busy worker
        try:
            proc.kill()
        except Exception:
            pass


def _parse_chunk(items):
    return [_parse_one(it) for it in items]


def _parse_alone(items, pool, died, limit, worked):
    """Parse each file in a one-worker pool, starting a new pool after a file kills or stalls one."""
    from concurrent.futures import TimeoutError as Stuck
    from concurrent.futures.process import BrokenProcessPool

    def failed(it, why):
        f, ext, mod_dir, mod_id = it[2]
        print(f"leyline: {why}: {f}; it is left out", file=sys.stderr)
        if died is not None:
            died.append(f)
        try:
            data = read_source(Path(it[0]) / f)
            loc, sha = data.count(b"\n") + 1, hashlib.sha1(data).hexdigest()
        except OSError:
            loc, sha = 1, None
        return f, ext, mod_dir, mod_id, loc, sha, None, why, b""
    ex = pool(1)
    # Files whose worker died before any worker in this run had finished a file: until one does, it is not known
    # whether the files kill the workers or the workers cannot run here at all.
    lost: list = []
    try:
        for k, it in enumerate(items):
            if len(lost) >= 3:
                print("leyline: worker processes are not working here; parsing in this process instead",
                      file=sys.stderr)
                worked["broken"] = True
                yield from map(_parse_one, [x for x, _ in lost] + list(items[k:]))
                return
            why = None
            try:
                got = ex.submit(_parse_one, it).result(timeout=limit)
            except Stuck:
                why = f"parsing took longer than {limit:g} s"
                _kill(ex)
            except BrokenProcessPool:
                why = "the parser process died on this file"
            if why is not None:
                ex.shutdown(wait=True)
                ex = pool(1)
                if not worked["any"]:
                    lost.append((it, why))
                    continue
                got = failed(it, why)
            else:
                worked["any"] = True
                for x, w in lost:   # a worker can run, so those files did kill theirs
                    yield failed(x, w)
                lost = []
            yield got
        for x, w in lost:
            yield failed(x, w)
    finally:
        ex.shutdown(wait=True)


class FlowSteps:
    """The steps of every flow, packed into arrays: as tuples they were the largest thing in memory on a large
    repository (millions of them). Iterates as (flow id, seq, depth, callable id, via, site line, parent seq)."""

    def __init__(self):
        self.flows: list[tuple] = []          # (flow id, index of its first step)
        self.callable: list[str] = []         # the node ids themselves, shared with the nodes
        # depth is at most the walk's max_depth, a parent is a step of the same flow, and a line is under 10**9
        self.depth, self.via, self.line, self.parent = array("h"), array("b"), array("i"), array("i")
        self.via_names: list[str] = []
        self._via: dict[str, int] = {}

    def add(self, flow_id: str, steps: list[tuple]) -> None:
        """steps: (seq, depth, callable id, via, site line or None, parent seq or None), seq counting from 0."""
        self.flows.append((flow_id, len(self.callable)))
        for _seq, depth, c, via, line, parent in steps:
            v = self._via.get(via)
            if v is None:
                v = self._via[via] = len(self.via_names)
                self.via_names.append(via)
            self.callable.append(c)
            self.depth.append(depth)
            self.via.append(v)
            self.line.append(-1 if line is None else line)
            self.parent.append(-1 if parent is None else parent)

    def __len__(self) -> int:
        return len(self.callable)

    def ids(self):
        """Every flow id, then every callable id, in step order: the ids the rows name, as store._keys takes them."""
        return itertools.chain((fid for fid, _ in self.flows), self.callable)

    def __iter__(self):
        ends = [start for _, start in self.flows[1:]] + [len(self.callable)]
        for (fid, start), end in zip(self.flows, ends):
            for i in range(start, end):
                line, parent = self.line[i], self.parent[i]
                yield (fid, i - start, self.depth[i], self.callable[i], self.via_names[self.via[i]],
                       None if line < 0 else line, None if parent < 0 else parent)


def _repo_of(node_id: str) -> str:
    """Every node id starts with its repo id: flask:python:..., flask:file:..., flask:module:..."""
    return node_id.split(":", 1)[0]


_WILDCARD = re.compile(r"\*\w*|\(\.\*\)|:\w+(\*|\+|\(\.\*\))|\{\*\*?\w*\}")


def _wildcard(segment: str) -> bool:
    """A route segment that takes the rest of the path: Fastify's and Express's `*`, Express's `(.*)`, `:path*` and
    `*name`, ASP.NET's `{*path}`."""
    return bool(_WILDCARD.fullmatch(segment))


def _segment_fits(route: str, request: str) -> bool:
    """One segment of a route against one of a request: equal, or a parameter of the route (ASP.NET's [controller]
    is the class's name, which clients write in lower case)."""
    return route == request or route.startswith(("<", "{", ":")) or route.lower() == request.lower()


def _fits_wildcard(route: list, request: list) -> bool:
    """Does a request's path fit a route that ends in a wildcard? The wildcard takes one segment or more; a hole in
    the request (`/api/lore/${rel}`, written {}) may itself be several segments, so it can cover the route's
    parameters up to and including the wildcard. A hole never stands in for a segment the route spells out."""
    k = len(route) - 1

    def go(i: int, j: int) -> bool:
        if i == k:
            return j < len(request)
        if j == len(request) or not _segment_fits(route[i], request[j]):
            return False
        if go(i + 1, j + 1):
            return True
        if request[j] != "{}":
            return False
        i2 = i + 1
        while i2 < k and route[i2].startswith(("<", "{", ":")):
            if go(i2 + 1, j + 1):
                return True
            i2 += 1
        return i2 == k and j == len(request) - 1
    return go(0, 0)


class Indexer:
    def __init__(self, root: str | Path, repo_id: Optional[str] = None,
                 others: Optional[list[tuple[str | Path, str]]] = None):
        """`others` are more (root, repo id) pairs indexed in the same run, a workspace: a name in one
        repository resolves to its declaration in another. Node ids keep their own repo's prefix."""
        self.root = Path(root).resolve()
        self.repo = repo_id or self.root.name
        self.repos: dict[str, Path] = {self.repo: self.root}
        for r, rid in others or ():
            if rid in self.repos or ":" in rid:
                raise ValueError(f"repo id {rid!r} is used twice or contains ':'")
            self.repos[rid] = Path(r).resolve()
        self.commit = _git(self.root, "rev-parse", "HEAD")
        self.commits = {rid: _git(r, "rev-parse", "HEAD") for rid, r in self.repos.items()}
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.calls: list[tuple] = []
        self.results: dict[str, FileResult] = {}  # file id -> adapter output
        # On a large repository, file id -> its calls and field uses, packed (leyline.packing); see _open
        self._packed: dict[str, bytes] = {}
        self._packed_decls: dict[str, bytes] = {}
        self._opened: Optional[list] = None      # [file id, result, changed] of the one file unpacked now
        self._ckeys: Optional[dict] = None
        self._file_index: dict[str, int] = {}
        self.file_lang: dict[str, str] = {}
        self.file_of_path: dict[str, str] = {}
        self.keep_ext: set[str] = set()          # file ids whose node ids keep the file's extension (_ext_clashes)
        self._decl_cache: dict[str, dict] = {}
        self._read_decls: dict[str, dict] = {}   # file id -> node id -> (span, declarations read in the parse)
        self._text_cache: dict[str, list] = {}
        self.stats: dict[str, Counter] = defaultdict(Counter)
        self.flows: list[tuple] = []
        self.call_col: dict[tuple, int] = {}
        self.flow_steps = FlowSteps()
        self.channel_stats: dict[str, Counter] = defaultdict(Counter)
        self.project_refs: dict[str, set] = defaultdict(set)
        self._vis_cache: dict[str, set] = {}
        self._sets: dict[tuple, set] = {}        # see _file_set
        self._name_ix: dict[tuple, dict] = {}
        self._loose_reach: dict[str, set] = {}
        self.exact_mode = "off"          # off | auto | roslyn | scip
        self.scip_paths: list[str] = []
        self.exact_stats: dict[str, dict] = {}
        self.keep_results = True         # False: run() lets go of the adapters' output and the resolvers' caches before writing
        self.parse_cache = None          # leyline.incremental: parse output kept from the last run, by content hash

    def _apply_exact(self) -> None:
        """Let a compiler overrule the syntax resolvers where one is available."""
        if self.exact_mode == "off":
            return
        from . import exact
        multi = len(self.repos) > 1
        if self.exact_mode in ("auto", "roslyn") and any(v == "csharp" for v in self.file_lang.values()):
            if multi:
                records, info = [], {"status": "skipped", "reason": "not yet run for a workspace of several repositories"}
            else:
                try:
                    records, info = exact.roslyn(self)
                except RuntimeError as exc:
                    records, info = [], {"status": "failed", "reason": str(exc)[-600:]}
            if records:
                info.update(exact.apply(self, records, "roslyn"))
            self.exact_stats["exact:roslyn"] = info
        paths = [(p, None) for p in self.scip_paths]
        if self.exact_mode in ("auto", "scip") and not paths:
            paths = [(str(p), repo) for repo, root in self.repos.items()
                     for p in (root / "index.scip", root / ".leyline" / "index.scip") if p.is_file()]
        by_repo = {}
        for path, repo in paths:
            try:
                if repo is None:   # a SCIP index names the directory it was made in; that says whose it is
                    repo = next((r for r, root in self.repos.items() if exact.scip_root(path) == root), self.repo) if multi else self.repo
                info = {"status": "ok", "file": path, **exact.apply(self, exact.scip(path, self.repos[repo]), "scip",
                                                                    repo if multi else None)}
            except Exception as exc:
                info = {"status": "failed", "reason": str(exc)[-600:]}
            self.exact_stats["exact:scip"] = info
            by_repo[repo] = info
        if multi and by_repo:
            ok = any(i["status"] == "ok" for i in by_repo.values())
            self.exact_stats["exact:scip"] = {"status": "ok" if ok else "failed", "by_repo": by_repo}

    # -- public --------------------------------------------------------------
    def _timed(self, name: str, fn, *args) -> None:
        start = time.perf_counter()
        fn(*args)
        self.timing[name] = round(self.timing.get(name, 0) + time.perf_counter() - start, 3)

    def run(self, con) -> dict:
        self.timing: dict[str, float] = {}
        started = time.perf_counter()
        self.files_of, self.skipped, self.failed = {}, {}, {}
        for rid, root in self.repos.items():
            listing = scan(root)
            self.files_of[rid], self.skipped[rid] = listing.files, listing.skipped
            if listing.why_walk:
                print(f"leyline: git would not list the files of {root} ({listing.why_walk}); reading the directory"
                      " instead, with the common .gitignore patterns applied", file=sys.stderr)
            elif listing.how == "walk":
                print(f"leyline: {root} is not a git repository: reading every file under it, with the common"
                      " .gitignore patterns applied", file=sys.stderr)
            _report_skipped(rid, listing.skipped)
        for repo, root in self.repos.items():
            self._parse_repo(repo, root, self.files_of[repo])
        self.timing["parse"] = round(time.perf_counter() - started, 3)

        def resolve():
            for repo, files in self.files_of.items():
                self._projects(files, repo)
            self._attach_methods()
            self._build_indexes()
            self._resolve_imports()
            self._script_entries()
            self._resolve_types()
            self._resolve_overrides()
            self._resolve_calls()
            self._resolve_events()
            self._resolve_fields()
        self._timed("resolve", resolve)
        self._timed("compiler", self._apply_exact)

        def channels():
            self._resolve_spawns()
            self._resolve_endpoints()
            from . import channels as more
            more.resolve(self)   # dependency injection, queues, databases, RPC
        self._timed("channels", channels)
        if not self.keep_results:
            # Flows and the write are left, which read nodes, edges, calls (with their columns) and flows. What the
            # parse and the resolvers held (GBs on a large repository) is let go first, so the flows' lists and the
            # write's reuse that memory instead of adding to it.
            for held in (self.results, self._read_decls, self._decl_cache, self._name_ix, self._sets, self._text_cache,
                         getattr(self, "_chain_memo", {}), self._packed, self._packed_decls,
                         getattr(self, "_decls_unpacked", {})):
                held.clear()
        self._timed("flows", self._build_flows)
        if not self.keep_results:
            for held in (self._vis_cache, self.call_col, self._loose_reach):
                held.clear()
        self._timed("write", self._write, con)
        return {k: dict(v) for k, v in self.stats.items()}

    def _parse_repo(self, repo: str, root: Path, files: list[str]) -> None:
        module_dirs = _module_dirs(files)
        self._add(Node(id=repo, kind="repo", name=repo, path="",
                       attrs={"url": _git(root, "remote", "get-url", "origin"),
                              "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD")}))
        work = []
        for f in files:
            ext = "." + f.rsplit(".", 1)[-1] if "." in f else ""
            adapter = BY_EXTENSION.get(ext)
            if adapter is None:
                continue
            mod_dir = _module_for(f, module_dirs)
            mod_id = self._module(mod_dir, files, repo)
            work.append((f, ext, mod_dir, mod_id))
        # With a parse cache (leyline.incremental), a file whose content and module are as they were last time is
        # not parsed again. Either way the files are taken in the listed order, so the index is the same.
        cache = self.parse_cache
        keep_ext = _ext_clashes(work)
        self.keep_ext.update(f"{repo}:file:{f}" for f in keep_ext)
        cached = cache.lookup(repo, root, work, keep_ext) if cache is not None else {}
        fresh = _parse_all(root, repo, [w for w in work if w[0] not in cached], keep_ext=keep_ext)
        failed_here = self.failed.setdefault(repo, [])
        packs = packing.packs(len(work))
        for w in work:
            got = cached.pop(w[0], None)   # let go as it is used: on a large repository the blobs add up
            fresh_one = got is None
            if fresh_one:
                got = next(fresh)
            f, ext, mod_dir, mod_id, loc, sha, blob, failed, toks = got
            if cache is not None:
                cache.keep(f"{repo}:file:{f}", got, fresh_one, f in keep_ext)
            del got
            res, heavy, decls = packing.unpack(blob) if blob is not None else (None, None, None)
            blob = None
            adapter = BY_EXTENSION[ext]
            file_id = f"{repo}:file:{f}"
            self._add(Node(id=file_id, kind="file", name=f.rsplit("/", 1)[-1], parent_id=mod_id,
                           language=adapter.LANGUAGE, path=f, span_start=1, span_end=loc,
                           content_hash=sha, attrs={"loc": loc}))
            if res is None:   # one bad file must not sink the run
                self.stats[adapter.NAME]["files_failed"] += 1
                failed_here.append((f, failed))
                print(f"leyline: failed to parse {f}: {failed}", file=sys.stderr)
                continue
            self.results[file_id] = res
            # Python's calls are kept unpacked: the argument-flow rule holds on to them after their file's turn.
            if packs and adapter.LANGUAGE != "python":
                self._packed[file_id] = heavy
                if decls is not None:
                    self._packed_decls[file_id] = decls
            else:
                res.calls, res.field_uses = packing.unpack_heavy(heavy)
                if decls is not None:
                    self._read_decls[file_id] = packing.unpack_decls(decls)
            self.file_of_path[f] = file_id
            self.file_lang[file_id] = adapter.LANGUAGE
            self.stats[adapter.NAME]["files"] += 1
            for n in res.nodes:
                self._add(n)
            if res.calls is not None:
                self._share_ids(res)
            self.edges.extend(res.edges)
        next(fresh, None)   # lets the worker pool, if one was started, shut down

    # -- packed calls (leyline.packing) -----------------------------------------
    def _open(self, fid: str, res: FileResult) -> FileResult:
        """The file's result with its calls and field uses in place. Packed ones are unpacked here, one file at a
        time: opening a file lets go of the one opened before (packing it again if it was changed)."""
        if res.calls is not None:
            return res
        self._shut()
        res.calls, res.field_uses = packing.unpack_heavy(self._packed[fid])
        self._share_ids(res)
        self._opened = [fid, res, False]
        return res

    def _share_ids(self, res: FileResult) -> None:
        """Point each call's and field use's caller at its node's own id string. Unpacked apart from the nodes,
        each was a copy of it, and every call row made from the call kept that copy."""
        nodes = self.nodes
        for group in (res.calls, res.field_uses):
            for c in group:
                n = nodes.get(c.src_id)
                if n is not None:
                    c.src_id = n.id

    def _shut(self) -> None:
        if self._opened is None:
            return
        fid, res, changed = self._opened
        if changed:
            self._packed[fid] = packing.pack_heavy(res.calls, res.field_uses)
        res.calls = res.field_uses = None
        self._opened = self._ckeys = None

    def _ckey(self, call: CallSite):
        """What _chain_memo keeps a call's results under: the call itself while it lives, which for a packed file
        is only while the file is open, so there a key that names the same call each time it is unpacked."""
        if self._opened is None:
            return id(call)
        if self._ckeys is None:
            fid = self._opened[0]
            self._ckeys = packing.call_keys(self._opened[1], self._file_index.setdefault(fid, len(self._file_index)))
        return self._ckeys.get(id(call), id(call))

    def _decls_read(self, fid: Optional[str]) -> dict:
        """node id -> (span, declarations) the parse read in a file, unpacked for the last file asked about."""
        got = self._read_decls.get(fid)
        if got is None and fid in self._packed_decls:
            cache = self.__dict__.setdefault("_decls_unpacked", {})
            got = cache.get(fid)
            if got is None:
                if len(cache) >= 2:
                    cache.clear()
                got = cache[fid] = packing.unpack_decls(self._packed_decls[fid])
        return got or {}

    # -- structure -----------------------------------------------------------
    def _add(self, n: Node) -> None:
        if n.id in self.nodes:
            # Partial classes: keep the first declaration, note the extra file.
            first = self.nodes[n.id]
            if n.kind == "type" and n.path != first.path:
                first.attrs.setdefault("also_in", []).append(n.path)
            return
        self.nodes[n.id] = n

    def _module(self, mod_dir: str, files: list[str], repo: Optional[str] = None) -> str:
        repo = repo or self.repo
        mid = f"{repo}:module:{mod_dir or '.'}"
        if mid not in self.nodes:
            marker = self._markers(files).get(mod_dir)
            self._add(Node(id=mid, kind="module", name=mod_dir.rsplit("/", 1)[-1] or repo,
                           parent_id=repo, path=mod_dir, attrs={"marker": marker}))
        return mid

    def _markers(self, files: list[str]) -> dict:
        """directory -> the first project marker in it, for a list of files. Made once per list: looking through
        every file for each new module was quadratic (minutes on a repository of tens of thousands of files)."""
        got = getattr(self, "_marker_memo", None)
        if got is None or got[0] is not files:
            out: dict[str, str] = {}
            for f in files:
                d, _, base = f.rpartition("/")
                if d not in out and (f.endswith(".csproj") or base in MODULE_MARKERS):
                    out[d] = base
            got = self._marker_memo = (files, out)
        return got[1]

    def _projects(self, files: list[str], repo: Optional[str] = None) -> None:
        """Project files give exact module-to-module and module-to-package edges."""
        repo = repo or self.repo
        root = self.repos[repo]
        for f in files:
            if not f.endswith(".csproj"):
                continue
            d = f.rpartition("/")[0]
            mid = f"{repo}:module:{d or '.'}"
            if mid not in self.nodes:
                continue
            try:
                text = (root / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for ref in re.findall(r'<ProjectReference\s+Include="([^"]+)"', text):
                target = (Path(d) / ref.replace("\\", "/")).parent
                tdir = Path(*_normalize(target.parts)).as_posix() if target.parts else ""
                tid = f"{repo}:module:{tdir or '.'}"
                if target.parts[:1] == ("..",) or tid not in self.nodes:
                    tid = self._module_elsewhere(root / target) or tid   # a project in another repo of the workspace
                if tid in self.nodes:
                    self.edges.append(Edge("imports", mid, tid, "exact", {"via": "ProjectReference"}))
                    self.project_refs[mid].add(tid)
            for name, ver in re.findall(r'<PackageReference\s+Include="([^"]+)"(?:\s+Version="([^"]+)")?', text):
                xid = self._external("nuget", name, {"category": "package", "version": ver or None}, repo)
                self.edges.append(Edge("depends_on", mid, xid, "exact", {"version_range": ver or None}))
            sdk = re.search(r'<Project\s+Sdk="([^"/]+)(?:/([^"]+))?"', text)
            if sdk and sdk.group(1) != "Microsoft.NET.Sdk":
                xid = self._external("nuget", sdk.group(1), {"category": "sdk", "version": sdk.group(2)}, repo)
                self.edges.append(Edge("depends_on", mid, xid, "exact", {"version_range": sdk.group(2)}))
            tf = re.search(r"<TargetFramework>([^<]+)<", text)
            if tf:
                self.nodes[mid].attrs["target_framework"] = tf.group(1)

    def _module_elsewhere(self, path: Path) -> Optional[str]:
        """The module of another workspace repo that an absolute directory is, if any."""
        if len(self.repos) < 2:
            return None
        path = Path(os.path.normpath(path))
        for repo, root in self.repos.items():
            if path == root or root in path.parents:
                mid = f"{repo}:module:{path.relative_to(root).as_posix()}"
                return mid if mid in self.nodes else None
        return None

    def _external(self, eco: str, name: str, attrs: Optional[dict] = None, repo: Optional[str] = None) -> str:
        repo = repo or self.repo
        xid = f"{repo}:ext:{eco}:{name}"
        if xid not in self.nodes:
            self._add(Node(id=xid, kind="external", name=name, parent_id=repo,
                           attrs={"ecosystem": eco, **(attrs or {})}))
        return xid

    # -- indexes -------------------------------------------------------------
    def _build_indexes(self) -> None:
        self.types_by_name: dict[tuple, list[str]] = defaultdict(list)   # (lang, name) -> type ids
        self.members: dict[str, dict[str, list[Node]]] = defaultdict(lambda: defaultdict(list))
        self.by_name: dict[tuple, list[Node]] = defaultdict(list)        # (lang, name) -> callables
        self.field_type: dict[str, dict[str, str]] = defaultdict(dict)   # type id -> field -> type name
        self.field_types_global: dict[tuple, set] = defaultdict(set)     # (lang, field) -> type names
        self.bases: dict[str, list[str]] = defaultdict(list)
        self.field_names: dict[str, set] = defaultdict(set)
        self.ns_modules: dict[str, set] = defaultdict(set)               # C# namespace -> module ids
        self.py_modules: dict[str, dict] = defaultdict(dict)             # repo -> python module path -> file id
        self.path_modules: dict[str, dict] = defaultdict(dict)           # repo -> path without extension -> file id (TypeScript)
        self.py_importable: dict[str, str] = {}                          # python import name -> file id, across the workspace
        self.star_exports: dict[str, list] = defaultdict(list)           # file -> files it re-exports everything from
        self.default_export: dict[str, str] = {}                         # file -> the node it exports as default
        self.packages: dict[str, dict] = {}                              # npm package name -> {"dir", "exports", "main"}
        for n in self.nodes.values():
            if n.attrs.get("is_default_export") and n.kind in ("callable", "type"):
                self.default_export[n.parent_id] = n.id
        self.file_of: dict[str, str] = {}
        self.field_calls: dict[tuple, str] = {}                          # (type id, field) -> the method whose result it holds
        self.py_vars: dict[str, dict] = {fid: res.var_types for fid, res in self.results.items() if res.var_types}
        for n in self.nodes.values():
            if n.kind == "type":
                self.types_by_name[(n.language, n.name)].append(n.id)
            elif n.kind == "callable":
                self.members[n.parent_id][n.name].append(n)
                self.by_name[(n.language, n.name)].append(n)
            elif n.kind == "field":
                self.field_names[n.parent_id].add(n.name)
                if n.attrs.get("type_call"):
                    self.field_calls[(n.parent_id, n.name)] = n.attrs["type_call"]
                tn = n.attrs.get("type_name")
                if tn:
                    self.field_type[n.parent_id][n.name] = tn
                    self.field_types_global[(n.language, n.name)].add(tn)
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            mod_id = self.nodes[fid].parent_id
            for d in res.declares:
                if lang == "csharp":
                    self.ns_modules[d].add(mod_id)
                elif lang == "python":
                    self.py_modules[_repo_of(fid)][d] = fid
                else:
                    self.path_modules[_repo_of(fid)][d] = fid
            for n in res.nodes:
                self.file_of[n.id] = fid
        # A package under a source root (src/flask) is imported by its own name (flask), not by its path.
        for repo in self.repos:
            py_files = {self.nodes[f].path: f for f in self.results if self.file_lang[f] == "python" and _repo_of(f) == repo}
            dirs_with_init = {p.rsplit("/", 1)[0] if "/" in p else "" for p in py_files if p.endswith("__init__.py")}
            for path, fid in sorted(py_files.items()):
                parts = path.split("/")
                start = len(parts) - 1
                while start > 0 and "/".join(parts[:start]) in dirs_with_init:
                    start -= 1
                if 0 < start < len(parts) - 1:
                    self.py_modules[repo].setdefault(module_path("/".join(parts[start:])), fid)
                if len(self.repos) > 1 and start < len(parts) - 1:
                    # Inside a package: another repository of the workspace can import it by this name.
                    self.py_importable.setdefault(module_path("/".join(parts[start:])), fid)

    # -- imports -------------------------------------------------------------
    def _resolve_imports(self) -> None:
        self.cs_usings: dict[str, set] = defaultdict(set)      # file -> namespaces
        self.cs_static: dict[str, list] = defaultdict(list)    # file -> type names from `using static`
        self.cs_alias: dict[str, dict] = defaultdict(dict)     # file -> alias -> type name
        self.py_names: dict[str, dict] = defaultdict(dict)     # file -> local name -> (file id, symbol|None)
        self.outside_imports: dict[str, set] = defaultdict(set)  # file -> local names imported from outside the workspace
        self.import_targets: dict[str, set] = defaultdict(set)   # file -> files it imports, by any form of import
        for repo, files in self.files_of.items():
            for f in files:
                if f.rsplit("/", 1)[-1] == "package.json":
                    try:
                        data = json.loads((self.repos[repo] / f).read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    if isinstance(data, dict) and data.get("name"):
                        self.packages[data["name"]] = {"dir": f.rpartition("/")[0], "exports": data.get("exports"), "repo": repo,
                                                       "main": data.get("source") or data.get("module") or data.get("main") or data.get("types")}
        seen = set()
        stdlib = getattr(sys, "stdlib_module_names", frozenset())
        self.py_local: dict[tuple, tuple] = {}   # (function, name) -> what an import inside that function binds

        def bind(fid: str, src: str, name: str, value: tuple) -> None:
            if src == fid:
                self.py_names[fid][name] = value
                return
            # An import inside a function binds the name there only: elsewhere in the file the same name may be a
            # local variable (`query = Query()` beside another function's `from .sql import query`).
            self.py_local[(src, name)] = value
            if value[0]:
                self.import_targets[fid].add(value[0])
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            for imp in res.imports:
                if lang == "csharp":
                    if imp.alias:
                        self.cs_alias[fid][imp.alias] = imp.target.rsplit(".", 1)[-1]
                    if imp.is_static:
                        self.cs_static[fid].append(imp.target.rsplit(".", 1)[-1])
                    ns = imp.target
                    mods = self.ns_modules.get(ns)
                    if not mods and (imp.is_static or imp.alias):
                        ns = imp.target.rsplit(".", 1)[0]
                        mods = self.ns_modules.get(ns)
                    self.cs_usings[fid].add(ns)
                    vis = self._visible(fid)
                    declared = bool(mods)
                    if mods and vis is not None:
                        # A namespace can be declared by several projects; only the referenced ones count.
                        mods = {m for m in mods if m in vis or m == self.nodes[fid].parent_id}
                    if mods:
                        for m in sorted(mods):
                            key = (fid, m, ns)
                            if key not in seen and m != self.nodes[fid].parent_id:
                                seen.add(key)
                                self.edges.append(Edge("imports", fid, m, "exact", {"namespace": ns}))
                    elif not declared:
                        xid = self._external("dotnet", imp.target if not (imp.is_static or imp.alias) else ns,
                                             {"category": "namespace"}, _repo_of(fid))
                        if (fid, xid) not in seen:
                            seen.add((fid, xid))
                            self.edges.append(Edge("imports", fid, xid, "exact"))
                else:
                    target = self._find_module(fid, imp.target)
                    on_path = False
                    if not target and lang == "python" and imp.target.split(".")[0] not in stdlib:
                        target = self._py_on_path(fid, imp.target)
                        on_path = bool(target)
                    if target:
                        if imp.symbols:
                            for s in imp.symbols:
                                name, _, alias = s.partition(" as ")
                                if name == "*":
                                    self.star_exports[fid].append(target)
                                    continue
                                sub = self._py_module(fid, f"{imp.target}.{name}") if lang == "python" else None
                                bind(fid, imp.src_id, alias or name, (sub, None) if sub else (target, name))
                        elif imp.alias or lang == "python":
                            bind(fid, imp.src_id, imp.alias or imp.target, (target, None))
                        self.import_targets[fid].add(target)
                        if (fid, target) not in seen and target != fid:
                            seen.add((fid, target))
                            self.edges.append(Edge("imports", fid, target, "heuristic" if on_path else "exact",
                                                   {"symbols": imp.symbols, **({"found_by": "sys.path"} if on_path else {})}))
                    elif lang == "python":
                        top = imp.target.split(".")[0] or imp.target
                        xid = self._external("python", top,
                                             {"category": "stdlib" if top in stdlib else "package"}, _repo_of(fid))
                        if (fid, xid) not in seen:
                            seen.add((fid, xid))
                            self.edges.append(Edge("imports", fid, xid, "exact", {"symbols": imp.symbols}))
                    elif not imp.target.startswith("."):
                        spec = imp.target[5:] if imp.target.startswith("node:") else imp.target
                        parts = spec.split("/")
                        top = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
                        builtin = imp.target.startswith("node:") or top in NODE_BUILTINS
                        xid = self._external("npm", top, {"category": "stdlib" if builtin else "package"}, _repo_of(fid))
                        for s in imp.symbols:   # a name from outside: calls to it are not ours to resolve
                            self.outside_imports[fid].add(s.partition(" as ")[2] or s)
                        if imp.alias:
                            self.outside_imports[fid].add(imp.alias)
                        if (fid, xid) not in seen:
                            seen.add((fid, xid))
                            self.edges.append(Edge("imports", fid, xid, "exact", {"symbols": imp.symbols}))

    def _script_entries(self) -> None:
        """A TypeScript file that does work at its top level and that nothing imports is a program:
        a server's start file, a build script. Tests and config files are not."""
        imported = {t for targets in self.import_targets.values() for t in targets}
        for fid, res in self.results.items():
            if self.file_lang[fid] != "typescript" or fid in imported:
                continue
            path = self.nodes[fid].path
            base = path.rsplit("/", 1)[-1]
            top = f"{_repo_of(fid)}:typescript:{self._modpath(fid)}.<module>"
            if top not in self.nodes or top + "#entry" in self.nodes or re.search(r"\.(test|spec|bench|config|setup|d)\.", base) \
                    or "/test/" in "/" + path or "/__tests__/" in "/" + path or any(n.kind == "test" for n in res.nodes):
                continue
            calls = [c for c in self._open(fid, res).calls if c.src_id == top and not c.ref]
            if len(calls) < 1:
                continue
            ep = Node(id=top + "#entry", kind="entry_point", name=path, parent_id=fid, language="typescript", path=path,
                      span_start=1, span_end=self.nodes[fid].span_end, attrs={"trigger": "cli", "address": path, "found_by": "nothing imports it"})
            self._add(ep)
            self.file_of[ep.id] = fid
            self.edges.append(Edge("exposes", ep.id, top))

    def _attach_methods(self) -> None:
        """A method written outside its type's body (Go's func (t *T) Run(), Rust's impl T { ... } in another
        file) belongs to the type of that name, looked for in the same directory first."""
        types: dict[tuple, list[str]] = defaultdict(list)
        for n in self.nodes.values():
            if n.kind == "type":
                types[(n.language, n.name)].append(n.id)
        for n in list(self.nodes.values()):
            owner = n.attrs.get("owner_name") if n.kind == "callable" else None
            if not owner or (n.parent_id in self.nodes and self.nodes[n.parent_id].kind == "type"):
                continue
            cands = types.get((n.language, owner), [])
            here = (_repo_of(n.id), (n.path or "").rpartition("/")[0])
            near = [t for t in cands if (_repo_of(t), (self.nodes[t].path or "").rpartition("/")[0]) == here] or cands
            if len(near) == 1:
                n.parent_id = near[0]
                n.attrs["type_id"] = near[0]
                fid = f"{_repo_of(n.id)}:file:{n.path}"
                for res in (self.results.get(fid),):
                    for c in (self._open(fid, res).calls if res else ()):
                        if c.src_id == n.id and c.enclosing_type is None:
                            c.enclosing_type = near[0]
                            if self._opened is not None:
                                self._opened[2] = True   # packed again when it is let go

    def _find_module(self, fid: str, target: str) -> Optional[str]:
        if getattr(BY_LANGUAGE.get(self.file_lang[fid]), "GENERIC", False):
            hits = self._generic_import(fid, target)
            for h in hits[1:]:
                self.import_targets[fid].add(h)
            return hits[0] if hits else None
        if self.file_lang[fid] == "python":
            return self._py_module(fid, target)
        return self._path_module(target, _repo_of(fid))

    def _generic_import(self, fid: str, target: str) -> list[str]:
        """Files an import names, whatever the language writes: a relative path (./x, ../x.h), a dotted or ::
        path (com.foo.Bar, crate::a::b), or a package path ("github.com/x/y/pkg" names a directory)."""
        if not hasattr(self, "_suffixes"):
            self._suffixes: dict[str, list[str]] = defaultdict(list)
            self._dirs: dict[str, list[str]] = defaultdict(list)
            for f in self.results:
                path = self.nodes[f].path
                stem = re.sub(r"\.[A-Za-z0-9]+$", "", path)
                parts = stem.split("/")
                for i in range(len(parts)):
                    self._suffixes["/".join(parts[i:])].append(f)
                    if parts[-1] in ("mod", "index", "__init__", "lib") and i < len(parts) - 1:
                        self._suffixes["/".join(parts[i:-1])].append(f)
                d = parts[:-1]
                for i in range(len(d)):
                    self._dirs["/".join(d[i:])].append(f)
        lang, repo = self.file_lang[fid], _repo_of(fid)
        here = self.nodes[fid].path.rpartition("/")[0]
        t = target.strip().strip("\"'<>`")
        if not t:
            return []
        m = re.fullmatch(r"(\.+)([\w.]*)", t)
        if m:   # Python's relative import: one dot is this package, each further dot one level up
            base = here
            for _ in range(len(m.group(1)) - 1):
                base = base.rpartition("/")[0]
            t = "./" + posixpath.join(posixpath.relpath(base or ".", here or "."), m.group(2).replace(".", "/")) if m.group(2) else "./"
            if not m.group(2):
                return [f for f in self._suffixes.get(posixpath.join(base, "__init__") if base else "__init__", []) if _repo_of(f) == repo][:1]
        if t.startswith("."):
            joined = posixpath.normpath(posixpath.join(here, t))
            key = re.sub(r"\.[A-Za-z0-9]+$", "", joined)
            return [f for f in self._suffixes.get(key, []) if self.nodes[f].path.startswith(key) and _repo_of(f) == repo] or self._dirs.get(key, [])[:0]
        t = re.sub(r"^(crate|self|super)::", "", t)
        key = re.sub(r"(::|\.|\\)", "/", t) if not "/" in t else re.sub(r"\.[A-Za-z0-9]+$", "", t)
        key = key.rstrip("/*").strip("/")
        def find(accept):
            same_lang = lambda fs, k: [f for f in fs if self.file_lang[f] == lang and accept(f, k)]
            for k in (key, key.rsplit("/", 1)[0] if "/" in key else None):   # import a.b.C names a file a/b/C, or a symbol C in a/b
                if not k:
                    continue
                hits = same_lang(self._suffixes.get(k, []), k)
                if hits:
                    return hits[:8] if len(hits) <= 8 else []
                hits = same_lang(self._dirs.get(k, []), k)        # a package or module directory
                if hits and len({self.nodes[h].path.rpartition("/")[0] for h in hits}) == 1:
                    return hits[:40]
            return []
        own = find(lambda f, k: _repo_of(f) == repo)
        if own or len(self.repos) == 1:
            return own
        # Another repository of the workspace: only a name that starts at its root or at a source root there
        # (werkzeug/routing in src/werkzeug/routing.py), not any file that happens to end the same way (json).
        return find(lambda f, k: _repo_of(f) != repo and self._rooted(self.nodes[f].path, k))

    SOURCE_ROOTS = {"src", "lib", "source", "sources", "main", "java", "kotlin", "scala", "pkg", "include", "app"}

    def _rooted(self, path: str, key: str) -> bool:
        """Whether `key` names `path` from the top of its repository or of a source root in it."""
        stem = re.sub(r"\.[A-Za-z0-9]+$", "", path)
        for tail in ("", "/mod", "/index", "/__init__", "/lib"):
            for cand in (stem, stem.rpartition("/")[0]):   # the file itself, or its directory (a package)
                if tail and not cand.endswith(tail):
                    continue
                cand = cand[: len(cand) - len(tail)] if tail else cand
                if cand == key or cand.endswith("/" + key):
                    prefix = cand[: len(cand) - len(key)].strip("/")
                    if not prefix or all(p in self.SOURCE_ROOTS for p in prefix.split("/")):
                        return True
        return False

    def _path_module(self, target: str, repo: Optional[str] = None) -> Optional[str]:
        """A TypeScript import: ./path from the repo root (the adapter resolved it), or a workspace package,
        which may live in another repository of the workspace."""
        def at(path, repo=repo or self.repo):
            # only a leading ./ goes: lstrip("./") also took the dot off a directory such as .storybook
            path = re.sub(r"\.(d\.[cm]?ts|[cm]?[jt]sx?)$", "", path[2:] if path.startswith("./") else path)
            return self.path_modules[repo].get(path) or self.path_modules[repo].get(path + "/index")
        if target.startswith("."):
            return at(target[2:] if target.startswith("./") else target)
        name = max((p for p in self.packages if target == p or target.startswith(p + "/")), key=len, default=None)
        if name is None:
            return None
        pkg, sub = self.packages[name], target[len(name):].lstrip("/")
        exports = pkg["exports"]
        entry = exports.get("./" + sub if sub else ".") if isinstance(exports, dict) else (exports if not sub else None)
        while isinstance(entry, dict):   # {"import": ..., "types": ..., "default": ...}
            entry = next((entry[k] for k in ("source", "import", "default", "types", "require") if k in entry), None)
        tries = [entry] if isinstance(entry, str) else []
        tries += [f"{sub}", f"src/{sub}"] if sub else [pkg["main"], "src/index", "index"]
        for t in tries:
            if t:
                hit = at(posixpath.normpath(posixpath.join(pkg["dir"], t)), pkg["repo"])
                if hit:
                    return hit
        return None

    def _py_module(self, fid: str, target: str) -> Optional[str]:
        repo = _repo_of(fid)
        mods = self.py_modules[repo]
        if target in mods:
            return mods[target]
        own = module_path(self.nodes[fid].path)
        pkg = own.rsplit(".", 1)[0] if "." in own else ""
        sibling = f"{pkg}.{target}" if pkg else target
        if sibling in mods:  # script-style import of a file in the same directory
            return mods[sibling]
        hit = self.py_importable.get(target)   # a package of another repository in the workspace
        return hit if hit and _repo_of(hit) != repo else None

    def _py_on_path(self, fid: str, target: str) -> Optional[str]:
        """`sys.path.insert(0, <dir>)` then `import validate`: a script or a test that puts a directory on the path
        imports a file there by its bare name. When the importing file, or a conftest.py above it, changes sys.path,
        an import nothing else resolves is taken to be the repository's one module of that name (or the one in a
        directory above the importer, when there are several). A heuristic: which directory is added is not worked
        out, so the import edge says so."""
        if target.startswith(".") or not self._edits_sys_path(fid):
            return None
        repo = _repo_of(fid)
        tail = "." + target
        cands = sorted({f for m, f in self.py_modules[repo].items() if (m == target or m.endswith(tail)) and f != fid})
        if len(cands) > 1:
            here = self.nodes[fid].path.rpartition("/")[0]
            cands = [f for f in cands if (here + "/").startswith(self.nodes[f].path.rpartition("/")[0].rstrip("/") + "/")
                     or "/" not in self.nodes[f].path]
        return cands[0] if len(cands) == 1 else None

    def _edits_sys_path(self, fid: str) -> bool:
        cache = self.__dict__.setdefault("_sys_path_files", {})
        if fid not in cache:
            repo, path = _repo_of(fid), self.nodes[fid].path
            parts = path.split("/")
            files = [path] + ["/".join(parts[:i] + ["conftest.py"]) for i in range(len(parts) - 1, -1, -1)]
            hit = False
            for p in dict.fromkeys(files):
                if p != path and module_path(p) not in self.py_modules[repo]:
                    continue
                try:
                    hit = b"sys.path" in read_source(self.repos[repo] / p)
                except OSError:
                    hit = False
                if hit:
                    break
            cache[fid] = hit
        return cache[fid]

    def _modpath(self, fid: str) -> str:
        adapter = BY_LANGUAGE[self.file_lang[fid]]
        if fid in self.keep_ext:
            return adapter.module_path(self.nodes[fid].path, True)
        return adapter.module_path(self.nodes[fid].path)

    def _py_export(self, target_fid: str, symbol: str, depth: int = 0) -> Optional[str]:
        """The node a module exposes under a name, following re-exports (`from .app import Flask`,
        `export { a } from "./b"`, `export * from "./c"`)."""
        if target_fid not in self.file_lang:
            return None
        if symbol == "default" and target_fid in self.default_export:
            return self.default_export[target_fid]
        cand = f"{_repo_of(target_fid)}:{self.file_lang[target_fid]}:{self._modpath(target_fid)}.{symbol}"
        if cand in self.nodes:
            return cand
        if depth < 6 and symbol in self.py_names.get(target_fid, {}):
            nxt, sub = self.py_names[target_fid][symbol]
            if sub is not None:
                return self._py_export(nxt, sub, depth + 1)
        if depth < 6:
            for nxt in self.star_exports.get(target_fid, ()):
                hit = self._py_export(nxt, symbol, depth + 1)
                if hit:
                    return hit
        return None

    def _py_name(self, fid: str, src: str, name: str) -> tuple:
        """What a name imported in a file means inside one of its functions: an import in the function or an
        enclosing one first, then the file's. (None, None) when it cannot be told."""
        cur = src
        while cur in self.nodes and self.nodes[cur].kind == "callable":
            if (cur, name) in self.py_local:
                return self.py_local[(cur, name)]
            cur = self.nodes[cur].parent_id
        return self.py_names[fid].get(name, (None, None))

    def _py_bound(self, fid: str, src: Optional[str], name: str) -> bool:
        """Does an import bind this name where src runs: in src, a function around it, or the file?"""
        cur = src
        while cur in self.nodes and self.nodes[cur].kind == "callable":
            if (cur, name) in self.py_local:
                return True
            cur = self.nodes[cur].parent_id
        return name in self.py_names[fid]

    def _py_var(self, fid: str, name: str, depth: int = 0) -> Optional[str]:
        """The type of a module-level variable (`g: Globals = ...`), found through imports and re-exports."""
        tn = self.py_vars.get(fid, {}).get(name)
        if tn:
            return self._type("python", tn, None, file=fid)
        if depth < 6 and name in self.py_names.get(fid, {}):
            nxt, sub = self.py_names[fid][name]
            if sub is not None:
                return self._py_var(nxt, sub, depth + 1)
        if depth < 6:
            for nxt in self.star_exports.get(fid, ()):
                hit = self._py_var(nxt, name, depth + 1)
                if hit:
                    return hit
        return None

    def _attr_type(self, lang: str, inner: CallSite) -> Optional[tuple]:
        """The type of an attribute read (`app.config`, `flask.g`) used as a receiver: the declared type of the
        field, of a property's result, or of a module's variable. None when it cannot be told."""
        fid = self.file_of.get(inner.src_id)
        if lang == "python" and fid and inner.receiver_type is None and inner.chain is None and self._py_bound(fid, inner.src_id, inner.receiver or ""):
            target, symbol = self._py_name(fid, inner.src_id, inner.receiver)
            if target and symbol is None:
                tid = self._py_var(target, inner.name)
                return (tid, True) if tid else None
        tid, _ = self._receiver_type(lang, inner)
        for t in self._chain(tid):
            tn = self.field_type.get(t, {}).get(inner.name)
            if tn:
                got = self._type(lang, tn, f"{t}.{inner.name}", file=self.file_of.get(t))
                return (got, True) if got else None
            getters = [m for m in self.members.get(t, {}).get(inner.name, ()) if m.attrs.get("returns")
                       and any("property" in d for d in m.attrs.get("decorators") or [])]
            if getters:
                got = self._type(lang, getters[0].attrs["returns"], getters[0].id)
                return (got, True) if got else None
        return None

    def _py_fixtures(self) -> None:
        """pytest passes a test each fixture named by its parameters. Link them, and give the
        parameter the fixture's return type so calls on it can be resolved."""
        self.py_param_type: dict[tuple, str] = {}
        by_file: dict[str, dict[str, Node]] = defaultdict(dict)
        for n in self.nodes.values():
            if n.language == "python" and n.kind == "callable" and n.attrs.get("is_fixture"):
                by_file[self.file_of[n.id]][n.name] = n
        if not by_file:
            return
        conftests = {(_repo_of(f), self.nodes[f].path.rsplit("/", 1)[0] if "/" in self.nodes[f].path else ""): f
                     for f in by_file if self.nodes[f].path.rsplit("/", 1)[-1] == "conftest.py"}

        def find(fid: str, name: str, skip: Optional[str] = None) -> Optional[Node]:
            hit = by_file.get(fid, {}).get(name)
            if hit is not None and hit.id != skip:
                return hit
            d = self.nodes[fid].path.rsplit("/", 1)[0] if "/" in self.nodes[fid].path else ""
            while True:
                hit = by_file.get(conftests.get((_repo_of(fid), d), ""), {}).get(name)
                if hit is not None and hit.id != skip:
                    return hit
                if not d:
                    return None
                d = d.rsplit("/", 1)[0] if "/" in d else ""
        memo: dict[str, Optional[str]] = {}

        def returns(fx: Node, depth: int = 0) -> Optional[str]:
            if fx.id in memo or depth > 4:
                return memo.get(fx.id)
            memo[fx.id] = None
            out = None
            if fx.attrs.get("returns"):
                out = self._type("python", fx.attrs["returns"], fx.id)
            elif fx.attrs.get("returns_call"):
                recv, method = fx.attrs["returns_call"]
                dep = find(self.file_of[fx.id], recv, fx.id) if recv in (fx.attrs.get("params") or []) else None
                owner = returns(dep, depth + 1) if dep is not None else None
                for m in self._methods(owner, method, 0) if owner else []:
                    if m.attrs.get("returns"):
                        out = self._type("python", m.attrs["returns"], m.id)
            memo[fx.id] = out
            return out
        for n in list(self.nodes.values()):
            if n.language != "python" or n.kind != "callable" or not (n.attrs.get("is_test") or n.attrs.get("is_fixture")):
                continue
            for pname in n.attrs.get("params") or []:
                fx = find(self.file_of[n.id], pname, n.id)
                if fx is None:
                    continue
                self.calls.append((n.id, fx.id, "fixture", "heuristic", n.span_start))
                tid = returns(fx)
                if tid:
                    self.py_param_type[(n.id, pname)] = tid

    # -- visibility ----------------------------------------------------------
    def _visible(self, fid: str) -> Optional[set]:
        """Modules whose symbols a file can reference, or None when that is not knowable."""
        mod = self.nodes[fid].parent_id
        if self.file_lang[fid] in FILE_MODULE:
            if fid not in self._vis_cache:
                out, queue = {fid}, [t for t, _ in self.py_names[fid].values() if t] + list(self.star_exports.get(fid, ()))
                if self.file_lang[fid] == "python":
                    queue += list(self.import_targets.get(fid, ()))   # imports inside functions count too
                while queue:   # a file re-exported with `export *` is seen through the file that re-exports it
                    cur = queue.pop()
                    if cur not in out:
                        out.add(cur)
                        queue.extend(self.star_exports.get(cur, ()))
                self._vis_cache[fid] = out
            return self._vis_cache[fid]
        if not (self.nodes[mod].attrs.get("marker") or "").endswith(".csproj"):
            return None  # a loose .cs file: no project file says what it can see
        if mod not in self._vis_cache:
            seen, queue = {mod}, [mod]
            while queue:
                cur = queue.pop()
                for nxt in self.project_refs.get(cur, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append(nxt)
            self._vis_cache[mod] = seen
        return self._vis_cache[mod]

    def _file_set(self, key: tuple) -> Optional[set]:
        """A set kept for the file whose calls are being resolved. Calls are resolved a file at a time, so only the
        last few files' sets are kept: keeping one per file held millions of members on a large repository (every
        Go file sees each file of every package it imports). Import targets are settled before any of these is
        asked for, so a set made again is the same set."""
        return self._sets.get(key)

    def _keep_set(self, key: tuple, value: set) -> set:
        if len(self._sets) >= 16:
            self._sets.clear()
        self._sets[key] = value
        return value

    def _reach(self, fid: str) -> set:
        """Files reachable through imports. A name found nowhere else in that set is a fair guess."""
        key = ("reach", fid)
        got = self._file_set(key)
        if got is None:
            out, queue = set(), [fid]
            while queue:
                cur = queue.pop()
                if cur in out:
                    continue
                out.add(cur)
                queue.extend(t for t in self.import_targets.get(cur, ()) if t not in out)
            got = self._keep_set(key, out)
        return got

    def _can_see(self, fid: str, node_id: str) -> bool:
        vis = self._visible(fid)
        if vis is None:
            return True
        target_file = self.file_of.get(node_id)
        if self.file_lang[fid] in FILE_MODULE:
            return target_file in vis
        return target_file is not None and self.nodes[target_file].parent_id in vis

    # -- types ---------------------------------------------------------------
    def _type(self, lang: str, name: str, from_id: Optional[str], file: Optional[str] = None) -> Optional[str]:
        fid = file or self.file_of.get(from_id or "")
        arity = None
        if lang == "python" and "." in name:
            # flask.Flask: the class a module imported under that name exposes.
            head, _, rest = name.rpartition(".")
            mod = self._py_name(fid, from_id, head) if fid and "." not in head else None
            cand = self._py_export(mod[0], rest) if mod and mod[0] and mod[1] is None else None
            if cand and self.nodes[cand].kind == "type":
                return cand
            name = rest
        if lang == "csharp":
            name, tick, count = name.partition("`")
            arity = int(count) if tick and count.isdigit() else 0
            if fid:
                name = self.cs_alias[fid].get(name, name)
        if lang == "python" and from_id:
            cur = from_id   # a class defined in the function itself or one around it
            while cur in self.nodes and self.nodes[cur].kind in ("callable", "test", "type"):
                hit = self.nodes.get(f"{cur}.{name}")
                if hit is not None and hit.kind == "type" and hit.id != from_id:   # `class App(web.App)` is not its own base
                    return hit.id
                cur = self.nodes[cur].parent_id
        if lang in FILE_MODULE and fid and (self._py_bound(fid, from_id, name) if lang == "python" else name in self.py_names[fid]):
            target, symbol = self._py_name(fid, from_id, name) if lang == "python" else self.py_names[fid][name]
            cand = self._py_export(target, symbol or name)
            if cand and self.nodes[cand].kind == "type":
                return cand
        if lang == "typescript" and fid:
            if name in self.outside_imports[fid]:
                return None
            own = f"{_repo_of(fid)}:{lang}:{self._modpath(fid)}.{name}"
            if own in self.nodes and self.nodes[own].kind == "type":
                return own
        cands = self.types_by_name.get((lang, name), [])
        if lang == "python":
            # A class defined inside a function is seen only there (found above), not by name from elsewhere.
            cands = [c for c in cands if self.nodes[self.nodes[c].parent_id].kind not in ("callable", "test")]
        if lang == "csharp" and fid:
            cands = [c for c in cands if self._can_see(fid, c)]
        if arity is not None and cands:
            # Foo and Foo<T> are different types. A plain name falls back to the generic one, because
            # some callers (a receiver's inferred type) do not carry the type arguments.
            exact = [c for c in cands if _arity(c) == arity]
            cands = exact or ([] if arity else cands)
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        if fid:
            same_file = [c for c in cands if self.file_of.get(c) == fid]
            if same_file:
                return same_file[0]
            if lang == "csharp":
                here = set(self.cs_usings[fid])
                for ns in self.results[fid].declares:  # a namespace sees its parents
                    parts = ns.split(".")
                    here.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
                here.add("")
                visible = [c for c in cands if self.nodes[c].attrs.get("namespace") in here]
                if len(visible) >= 1:
                    return visible[0]
        return None

    def _resolve_types(self) -> None:
        seen = set()
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            adapter = BY_LANGUAGE[lang].NAME
            for ref in res.type_refs:
                for i, name in enumerate(ref.names):
                    tid = self._type(lang, name, ref.src_id)
                    if tid is None:
                        self.stats[adapter]["type_refs_external"] += 1
                        continue
                    self.stats[adapter]["type_refs_resolved"] += 1
                    if ref.role == "base":
                        if i > 0:
                            kind, attrs = "uses_type", {"role": "generic_arg"}
                        else:
                            target_iface = self.nodes[tid].attrs.get("native_kind") == "interface"
                            src_iface = self.nodes[ref.src_id].attrs.get("native_kind") == "interface"
                            kind, attrs = ("implements" if target_iface and not src_iface else "extends"), {}
                            self.bases[ref.src_id].append(tid)
                    elif ref.role == "instantiate":
                        kind, attrs = ("instantiates" if i == 0 else "uses_type"), ({} if i == 0 else {"role": "generic_arg"})
                    else:
                        kind, attrs = "uses_type", {"role": ref.role if i == 0 else "generic_arg"}
                    key = (kind, ref.src_id, tid, attrs.get("role"))
                    if key in seen or ref.src_id == tid:
                        continue
                    seen.add(key)
                    self.edges.append(Edge(kind, ref.src_id, tid, "heuristic", attrs))

    # -- calls ---------------------------------------------------------------
    def _chain(self, type_id: Optional[str]) -> list[str]:
        """A type, its bases, and its enclosing types, nearest first."""
        out, queue = [], [type_id] if type_id else []
        while queue:
            t = queue.pop(0)
            if t in out or t not in self.nodes:
                continue
            out.append(t)
            queue.extend(self.bases.get(t, []))
            parent = self.nodes[t].parent_id
            if parent in self.nodes and self.nodes[parent].kind == "type":
                queue.append(parent)
        return out

    def _pick(self, cands: list[Node], argc: int, skip: int = 0) -> list[Node]:
        """Overloads that fit a call. `skip` is 1 for an extension method, whose first parameter is the receiver."""
        if argc < 0:
            return list(cands)   # a function named, not called: nothing to match arguments against
        argc += skip
        fit = [c for c in cands if c.attrs.get("argc_min", 0) <= argc <= c.attrs.get("argc_max", 99)]
        call = getattr(self, "_call", None)
        if len(fit) > 1 and call is not None:
            # Several overloads take this many arguments. Narrow by what the call site shows.
            if call.targs:
                fit = [c for c in fit if c.attrs.get("generic_arity") == call.targs] or fit
            hints = call.args

            def possible(c: Node) -> bool:
                want = c.attrs.get("delegate_arity") or []
                for i, h in enumerate(hints, skip):
                    if isinstance(h, int) and not isinstance(h, bool) and i < len(want):
                        if want[i] == -2 or (want[i] >= 0 and want[i] != h):
                            return False  # a lambda passed where no delegate of that shape is taken
                    elif isinstance(h, str) and i < len(want) and want[i] >= 0 and h in ("int", "double", "bool", "char", "string"):
                        return False  # a literal passed where a delegate is taken
                return True

            def score(c: Node) -> int:
                want, types = c.attrs.get("delegate_arity") or [], c.attrs.get("param_types") or []
                n = 0
                for i, h in enumerate(hints, skip):
                    if isinstance(h, int) and i < len(want) and want[i] == h:
                        n += 1
                    elif isinstance(h, str) and i < len(types) and types[i].split("`")[0] == h:
                        n += 1
                return n
            fit = [c for c in fit if possible(c)] or fit
            best = max(score(c) for c in fit)
            fit = [c for c in fit if score(c) == best]
        return fit or cands

    def _methods(self, type_id: Optional[str], name: str, argc: int) -> list[Node]:
        for t in self._chain(type_id):
            cands = self.members.get(t, {}).get(name)
            if cands:
                return self._pick(cands, argc)
        return []

    def _receiver_type(self, lang: str, call: CallSite) -> tuple[Optional[str], bool]:
        """Returns (type id, known). known=True with None means an external type."""
        r = call.receiver
        if r in (None, "this"):
            return call.enclosing_type, call.enclosing_type is not None
        if r == "base":
            bases = self.bases.get(call.enclosing_type or "", [])
            return (bases[0] if bases else None), True
        if call.receiver_type:
            return self._type(lang, call.receiver_type, call.src_id), True
        if call.chain is not None and not call.receiver_type:
            got = self._returned_type(lang, call.chain)
            if got is not None:
                return got
        if r in ("?", "[]"):
            return None, False
        if r in getattr(BY_LANGUAGE[lang], "GLOBALS", ()) and not self._type(lang, r, call.src_id):
            return None, True   # JSON.stringify, Math.max: the platform's, not ours
        if r.startswith("."):
            names = self.field_types_global.get((lang, r[1:]), set())
            ids = {self._type(lang, n, call.src_id) for n in names}
            ids.discard(None)
            if len(ids) == 1:
                return ids.pop(), True
            if names and not ids:
                return None, True  # every field of that name has an external type
            return None, False
        if lang == "python":
            # A parameter filled by a pytest fixture, seen from the test or a function nested in it.
            cur, shadowed = call.src_id, False
            while cur in self.nodes and self.nodes[cur].kind == "callable":
                if (cur, r) in self.py_param_type:
                    return self.py_param_type[(cur, r)], True
                if r in (self.nodes[cur].attrs.get("params") or []):
                    shadowed = True
                    break
                cur = self.nodes[cur].parent_id
            fid = self.file_of.get(call.src_id)
            if not shadowed and call.chain is None and fid and (r in self.py_vars.get(fid, {}) or self._py_bound(fid, call.src_id, r)):
                tid = self._py_var(fid, r)   # a module-level variable, of this file or imported
                if tid:
                    return tid, True
        # A bare identifier: a field of the enclosing type chain, or a type name (static call).
        for t in self._chain(call.enclosing_type):
            tn = self.field_type.get(t, {}).get(r)
            if tn:
                return self._type(lang, tn, call.src_id), True
        tid = self._type(lang, r, call.src_id)
        if tid:
            return tid, True
        if lang == "csharp" and r[:1].isupper() and not any(
                r in self.field_names.get(t, ()) for t in self._chain(call.enclosing_type)):
            return None, True  # PascalCase, not a member, not ours: a type from outside (File.Open)
        return None, False

    WRAPPERS = {"Task", "ValueTask", "Task`1", "ValueTask`1", "Nullable`1"}

    def _returned_type(self, lang: str, inner: CallSite, depth: int = 0) -> Optional[tuple]:
        """The type of a call's result, from the declared return type of what it resolves to.
        Returns (type id or None, known) like _receiver_type, or None when nothing can be said."""
        key = self._ckey(inner)
        if key in self._chain_memo:
            return self._chain_memo[key]
        self._chain_memo[key] = None
        if depth > 6:
            return None
        if inner.attr:
            out = self._attr_type(lang, inner)
            self._chain_memo[key] = out
            return out
        saved = (getattr(self, "_guessed", False), getattr(self, "_call", None), getattr(self, "_last_src", None))
        self._guessed = False
        stats_before = {k: dict(v) for k, v in self.stats.items()}
        self._call = inner
        fid = self.file_of.get(inner.src_id)
        targets = self._resolve_call(lang, fid, inner) if fid else None
        guessed = self._guessed
        self._guessed, self._call, self._last_src = saved[0], saved[1], saved[2]
        for k, v in stats_before.items():  # the inner call is counted when its own turn comes
            self.stats[k].clear()
            self.stats[k].update(v)
        out = None
        if targets and not guessed:
            t = targets[0]
            if t.name in CTORS:
                out = (t.parent_id, True)
            elif lang == "python" and t.attrs.get("returns_fn") and f"{t.id}.{t.attrs['returns_fn']}" in self.nodes:
                out = (f"{t.id}.{t.attrs['returns_fn']}", True)   # a function value: calling it runs that function
            elif lang in FILE_MODULE:
                if t.attrs.get("returns"):
                    tid = self._type(lang, t.attrs["returns"], t.id)
                    out = (tid, True) if tid else None
            else:
                names = [n for n in t.attrs.get("returns_names") or [] if n not in self.WRAPPERS]
                if names and names[0] not in (t.attrs.get("type_params") or []):
                    tid = self._type(lang, names[0], t.id)
                    owner_params = re.findall(r"[A-Za-z_]\w*", (self.nodes[t.parent_id].attrs.get("signature") or "").split(":")[0].partition("<")[2]) \
                        if t.parent_id in self.nodes else []
                    if tid:
                        out = (tid, True)
                    elif names[0].split("`")[0] not in owner_params and names[0] not in ("void", "var", "dynamic", "object"):
                        out = (None, True)  # a declared type from outside the workspace
        self._chain_memo[key] = out
        return out

    def _extensions(self, lang: str, fid: str, tid: str, name: str, argc: int) -> list[Node]:
        """Extension methods called on a receiver of a known type: static methods whose `this` parameter is that type."""
        cands = [c for c in self.by_name.get((lang, name), ()) if c.attrs.get("is_extension") and self._can_see(fid, c.id)]
        if not cands:
            return []
        # Foo and Foo<T> are different receivers: compare the name together with its generic arity.
        names = {self.nodes[t].name.split("`")[0] + (f"`{_arity(t)}" if _arity(t) else "")
                 for t in self._chain(tid) if t in self.nodes}
        exact = [c for c in cands if (c.attrs.get("param_types") or [""])[0] in names]
        loose = [c for c in cands if (c.attrs.get("param_types") or [""])[0] in (c.attrs.get("type_params") or [])]
        pool = exact or loose
        if not pool:
            return []
        if not exact:
            self._guessed = True  # `this TBuilder builder`: the constraint is not checked
        return self._pick(pool, argc, skip=1)

    def _resolve_calls(self) -> None:
        self._chain_memo: dict = {}
        self._call = None
        self._arg_log: list = []    # Python: (function, a call that reaches it), for _py_argument_flow
        self._pending: list = []    # Python: calls nothing resolved, for _py_argument_flow
        for (tid, fname), method in self.field_calls.items():
            # self.config = self.make_config(): the field holds what that method is declared to return.
            if fname in self.field_type.get(tid, {}):
                continue
            for m in self._methods(tid, method, -1):
                if m.attrs.get("returns"):
                    self.field_type[tid][fname] = m.attrs["returns"]
                    self.field_types_global[(m.language, fname)].add(m.attrs["returns"])
                    break
        # Names seen on receivers of a known outside type (List.Add, dict.get). A call to such a
        # name on a receiver of unknown type is never guessed.
        self.outside_names: set = set()
        for a in BY_LANGUAGE.values():
            self.outside_names.update((a.LANGUAGE, n) for n in getattr(a, "COMMON_METHODS", ()))
        self._py_fixtures()
        for fid, res in self._calls_pass(1):
            lang = self.file_lang[fid]
            for call in self._open(fid, res).calls:
                if call.receiver not in (None, "this", "base") and call.name != ".ctor":
                    if lang in FILE_MODULE and (self._py_bound(fid, call.src_id, call.receiver) if lang == "python" else call.receiver in self.py_names[fid]):
                        continue
                    if call.ref:
                        continue
                    tid, known = self._receiver_type(lang, call)
                    if known and tid is None:
                        self.outside_names.add((lang, call.name))
        for fid, res in self._calls_pass(2):
            lang = self.file_lang[fid]
            adapter = BY_LANGUAGE[lang].NAME
            st = self.stats[adapter]
            for call in self._open(fid, res).calls:
                if call.ref:
                    # A function handed over by name. Linked when the name is one of ours; otherwise it was a value.
                    self._guessed, self._call = False, None
                    before = dict(st)
                    targets = [t for t in (self._resolve_call(lang, fid, call) or []) if t.kind == "callable"]
                    st.clear()
                    st.update(before)
                    if targets and not self._guessed:
                        st["references_linked"] += 1
                        for t in targets:
                            self.calls.append((call.src_id, t.id, "reference", "heuristic", call.line))
                            self.call_col[(call.src_id, t.id, call.line)] = call.col
                    continue
                st["calls_total"] += 1
                self._guessed = False
                self._call = call
                targets = self._resolve_call(lang, fid, call)
                if targets is None:
                    st["calls_external"] += 1
                elif not targets:
                    st["calls_unresolved"] += 1
                else:
                    st["calls_resolved"] += 1
                    for t in targets:
                        dispatch = "virtual" if t.attrs.get("is_virtual") and lang == "csharp" else "static"
                        self.calls.append((call.src_id, t.id, dispatch,
                                           "guess" if self._guessed else "heuristic", call.line))
                        self.call_col[(call.src_id, t.id, call.line)] = call.col
                if lang == "python":
                    if targets and not self._guessed:
                        for t in targets:
                            self._arg_log.append((t.id, call))
                    elif not targets:
                        self._pending.append(call)
        self._shut()
        # Kept as lists on the indexer, so an incremental run records each file's share with its turn and replays it.
        incoming: dict[str, list] = defaultdict(list)   # Python: function -> the calls that reach it
        for tid, call in self._arg_log:
            incoming[tid].append(call)
        self._py_argument_flow(incoming, self._pending)

    def _py_argument_flow(self, incoming: dict, pending: list) -> None:
        """Calls made on a value handed in from outside: `app(environ, start)` on a parameter, `self.repo.save()` on
        a field set from one. The value's class is whatever the callers pass, followed back through parameters,
        fields set from parameters and `*args` passed on. Linked to that class's method (`__call__` when the value
        itself is called), as a guess, since a caller the map does not see may pass something else."""
        if not pending:
            return
        memo: dict = {}
        active: set = set()
        cut = [0, 0]   # answers left incomplete by a cycle or a limit; questions asked for the current call

        def param_owner(src: str, name: str) -> Optional[tuple]:
            cur = src
            while cur in self.nodes and self.nodes[cur].kind == "callable":
                params = self.nodes[cur].attrs.get("params") or []
                if name in params:
                    return cur, params.index(name)
                cur = self.nodes[cur].parent_id
            return None

        def narrowed(src: str, owner: str, name: str) -> bool:
            """Is the parameter tested with isinstance() between the call and the function that takes it?"""
            cur = src
            while cur in self.nodes and self.nodes[cur].kind == "callable":
                if name in (self.nodes[cur].attrs.get("narrowed") or ()):
                    return True
                if cur == owner:
                    return False
                cur = self.nodes[cur].parent_id
            return False

        def field_types(type_id: Optional[str], name: str, depth: int) -> set:
            for t in self._chain(type_id):
                f = self.nodes.get(f"{t}.{name}")
                if f is None or f.kind != "field":
                    continue
                tn = self.field_type.get(t, {}).get(name)
                tid = self._type("python", tn, f.id) if tn else None
                if tid:
                    return {tid}
                out = set()
                for fn, pname in f.attrs.get("from_params") or []:   # self.app = app, in the constructor
                    owner = param_owner(fn, pname)
                    out |= arg_types(owner[0], owner[1], depth + 1) if owner else set()
                return out
            return set()

        def arg_types(fn_id: str, pos: int, depth: int = 0) -> set:
            """Classes of ours passed as the pos-th positional argument of fn_id."""
            key = (fn_id, pos)
            if key in memo:
                return memo[key]
            fn = self.nodes.get(fn_id)
            cut[1] += 1
            if fn is None or key in active or depth > 8 or cut[1] > 5000:
                cut[0] += 1   # not cached: the same question asked from higher up may get further
                return set()
            params, star = fn.attrs.get("params") or [], fn.attrs.get("star_at")
            if pos < len(params) and (star is None or pos < star):
                declared = (fn.attrs.get("ptypes") or {}).get(params[pos])
                tid = self._type("python", declared, fn_id) if declared else None
                if tid:
                    memo[key] = {tid}
                    return memo[key]
            active.add(key)
            before, out = cut[0], set()
            for c in incoming.get(fn_id, ()):
                hints = c.args or ()
                spread = next((k for k, h in enumerate(hints[:pos + 1]) if h == "@*"), None)
                if spread is not None:
                    caller = self.nodes.get(c.src_id)
                    if caller is not None and caller.attrs.get("star_at") is not None:
                        out |= arg_types(c.src_id, caller.attrs["star_at"] + pos - spread, depth + 1)
                    continue
                h = hints[pos] if pos < len(hints) else None
                if h is None:
                    continue
                if h == "@self":
                    out |= {c.enclosing_type} if c.enclosing_type else set()
                elif h.startswith("@p:"):
                    owner = param_owner(c.src_id, h[3:])
                    out |= arg_types(owner[0], owner[1], depth + 1) if owner else set()
                elif h.startswith("@f:"):
                    out |= field_types(c.enclosing_type, h[3:], depth)
                else:
                    tid = self._type("python", h, c.src_id)
                    out |= {tid} if tid else set()
            active.discard(key)
            if cut[0] == before:
                memo[key] = out
            return out

        st = self.stats[BY_LANGUAGE["python"].NAME]
        for call in pending:
            r, method = call.receiver, call.name
            types: set = set()
            cut[1] = 0
            if r is None or r not in ("this", "base", "?"):
                owner = param_owner(call.src_id, r or call.name)
                if owner is not None and narrowed(call.src_id, owner[0], r or call.name):
                    owner = None
                if owner is not None and (r is None or call.receiver_type is None or not self._type("python", call.receiver_type, call.src_id)):
                    types = arg_types(*owner)
                    method = "__call__" if r is None else method
            elif r == "this" and call.enclosing_type and call.name in self.field_names.get(call.enclosing_type, ()):
                types, method = field_types(call.enclosing_type, call.name, 0), "__call__"   # self.handler(x)
            if r is not None and r.startswith(".") and call.enclosing_type:
                types = field_types(call.enclosing_type, r[1:], 0)
            for tid in sorted(types):
                self._call = call
                for t in self._methods(tid, method, call.argc):
                    st["calls_by_argument_flow"] += 1
                    self.calls.append((call.src_id, t.id, "static", "guess", call.line))
                    self.call_col[(call.src_id, t.id, call.line)] = call.col

    def _calls_pass(self, phase: int):
        """The files whose calls _resolve_calls goes through, in its first pass (names seen on outside types) and
        its second (the calls themselves). Every file, in order; leyline.incremental narrows it to the files an
        edit can have changed and fills in the rest from the last run."""
        return self.results.items()

    _DECL = [  # (pattern, group of the name, group of the type): how typed languages write a variable's type
        (re.compile(r"\b([A-Z]\w*)(?:<[^<>;=()]*>)?(?:\[\])?\??\s*[*&]?\s+(\w+)\s*(?=[=;,)]|$)"), 2, 1),      # Foo x  (C#, Java, C++)
        (re.compile(r"\b(\w+)\s*:\s*&?(?:mut\s+|readonly\s+)?\(?(?:[a-z_]\w*(?:::|\.))*([A-Z]\w*)"), 1, 2),                          # x: Foo, x: &mod::Foo  (TS, Kotlin, Swift, Rust, Python)
        (re.compile(r"\b(\w+)\s*(?::=|=)\s*(?:new\s+|&|await\s+)?([A-Z]\w*)(?:<[^<>]*>)?(?:::new)?\s*[({]"), 1, 2),  # x = new Foo( / Foo{ / Foo::new(
        (re.compile(r"\b(\w+)\s+\*?([A-Z]\w*)\s*[,)]"), 1, 2),                                                     # (x *Foo)  (Go parameters)
        (re.compile(r"\bvar\s+(\w+)\s+\*?([A-Z]\w*)"), 1, 2),                                                      # var x Foo  (Go)
        # built-in types, which tell an overload apart: int x, boolean b[], x int, err error
        (re.compile(r"\b(byte|short|int|long|float|double|boolean|bool|char|string|str|rune)(?:\[\])?\s+(\w+)\s*(?=[=;,)])"), 2, 1),
        (re.compile(r"\b(\w+)\s+(byte|int|int32|int64|uint|float32|float64|bool|string|rune|error)\s*[,)]"), 1, 2),
    ]

    def _generic_var_type(self, lang: str, src_id: str, var: str, enclosing_type: Optional[str]) -> Optional[str]:
        """The type a variable or field is declared with, read from the text of the function and of its type."""
        if not re.fullmatch(r"[A-Za-z_]\w*", var):
            return None
        for holder in (src_id, enclosing_type):
            if not holder or holder not in self.nodes:
                continue
            decls = self._generic_decls(lang, holder)[0]
            if var in decls:
                if decls[var] is None:
                    return "external"     # declared with a type that is not in the repo
                return self._type(lang, decls[var], src_id) or (self.types_by_name.get((lang, decls[var])) or [None])[0]
        return None

    def _generic_decls(self, lang: str, holder: str) -> tuple:
        """({variable: repo type name, or None for a type from outside}, {variable: type name as written})."""
        got = self._decl_cache.get(holder)
        if got is None:
            n = self.nodes[holder]
            fid = self.file_of.get(holder)
            read = self._decls_read(fid).get(holder)
            if read is not None and read[:2] == (n.span_start, n.span_end):
                found = read[2]   # read by the parse worker from the same text
            else:
                found = _declared(self._file_text(fid) if fid else [], n.span_start, n.span_end)
            decls, raw = {}, {}
            for name, tnames in found.items():
                raw[name] = tnames[0]
                typed = [t for t in tnames if t not in NOT_TYPES]
                if typed:
                    # the first type written for it, or, if that one is not in the repo, the first after it that is
                    decls[name] = next((t for t in typed if self.types_by_name.get((lang, t))), None)
            if len(self._decl_cache) >= 4096:
                # Asked about the functions of the file being resolved; made again the same when asked later.
                # Kept for every function, it was hundreds of MB on a large repository.
                self._decl_cache.clear()
            got = self._decl_cache[holder] = (decls, raw)
        return got

    _INTS = re.compile(r"(?i)^(int|long|short|byte|integer|bigint|biginteger|[iu](8|16|32|64|128|size)|u?int(8|16|32|64)?|rune)$")
    _FLOATS = re.compile(r"(?i)^(float|double|f32|f64|float32|float64|decimal|bigdecimal|number)$")
    _STRS = re.compile(r"(?i)^(string|str|charsequence)$")
    _BOOLS = re.compile(r"(?i)^(bool|boolean)$")
    _PRIMS = {"int", "long", "short", "byte", "float", "double", "boolean", "char", "bool"}
    _ANY = {"Object", "object", "any", "Any", "AnyObject", "dynamic", "interface", "unknown"}

    def _supers(self, tid: str) -> set:
        out, queue = set(), [tid]
        while queue:
            t = queue.pop()
            if t not in out:
                out.add(t)
                queue.extend(self.bases.get(t, []))
        return out

    def _arg_fits(self, lang: str, hint: str, ptype: str) -> int:
        """How well an argument fits a declared parameter type: 2 the same, 1 compatible, 0 cannot tell, -1 cannot be."""
        p = ptype.rstrip(".")
        if not p or not hint or hint == "fn":
            return 0
        repo_p = self.types_by_name.get((lang, p))
        kinds = {"string": self._STRS, "int": self._INTS, "float": self._FLOATS, "bool": self._BOOLS}
        boxed, named = False, hint
        if hint not in kinds and hint not in ("char", "null") and not self.types_by_name.get((lang, hint)):
            # a built-in type by name (int, Integer, String): the same as the literal of that kind
            kind = "char" if hint.lower() in ("char", "character") else \
                next((k for k, pat in kinds.items() if pat.match(hint) and hint.lower() not in ("number", "decimal")), None)
            if kind:
                boxed, hint = hint[:1].isupper() and kind != "string", kind
        if hint in kinds or hint == "char":
            # a literal is the built-in type exactly (int, bool); its boxed or wider form takes it too
            exact = 2 if p[:1].islower() != boxed or hint == "string" else 1
            if hint == "char" and p.lower() in ("char", "character", "rune"):
                return exact
            if hint in kinds and kinds[hint].match(p):
                if named.lower() == p.lower() and named != hint:
                    return 3    # the very type named: Float.NaN to float rather than double
                if named == "float" and re.fullmatch(r"(?i)float|f32|float32", p):
                    return 1    # a decimal literal is a double unless marked otherwise
                return exact if p.lower() not in ("number", "decimal", "bigdecimal", "biginteger", "bigint") else 1
            if hint in ("int", "char") and self._FLOATS.match(p) or hint == "char" and self._INTS.match(p):
                return 1   # widened
            other = any(k.match(p) for k in kinds.values()) or p in self._PRIMS
            return -1 if other or repo_p else 0
        if hint == "null":
            return -1 if p in self._PRIMS else 0
        if any(k.match(p) for k in kinds.values()) or p in self._PRIMS:
            return -1      # a type that is not a built-in one passed where a built-in one is taken
        if hint == p:
            return 2
        repo_h = self.types_by_name.get((lang, hint))
        if repo_h and repo_p:
            return 1 if any(t in self._supers(h) for h in repo_h for t in repo_p) else -1
        if repo_h and (p in self._PRIMS or any(k.match(p) for k in kinds.values())):
            return -1
        if repo_p and not repo_h and (len(hint) > 2 or not hint.isupper()):
            return -1   # a type from outside cannot be one of ours (a one-letter name is a type parameter)
        if repo_h and not repo_p and p not in self._ANY and (len(p) > 2 or not p.isupper()):
            # one of ours passed where a type from outside is taken: only if it says it extends that type
            said = " ".join(self.nodes[t].attrs.get("signature") or "" for h in repo_h for t in self._supers(h) if t in self.nodes)
            return 1 if re.search(rf"\b{re.escape(p)}\b", said) else -1
        return 0

    def _generic_returns(self, lang: str, inner: CallSite) -> Optional[tuple]:
        """_returned_type for the generic resolver, without the edges resolving the inner call would record twice."""
        if inner.receiver is None and self.types_by_name.get((lang, inner.name)):
            return (self.types_by_name[(lang, inner.name)][0], True)    # Foo(): a constructor called by the type's name
        n_edges = len(self.edges)
        out = self._returned_type(lang, inner)
        del self.edges[n_edges:]
        return out

    def _generic_return_name(self, lang: str, inner: CallSite) -> Optional[str]:
        """The type name a call's target declares it returns, from outside the repo or not (None when the call
        does not resolve, or when what it returns is a type parameter)."""
        key = ("ret", self._ckey(inner))
        if key in self._chain_memo:
            return self._chain_memo[key]
        self._chain_memo[key] = None
        if inner.receiver is None and self.types_by_name.get((lang, inner.name)):
            out = inner.name
        else:
            saved = (getattr(self, "_guessed", False), getattr(self, "_call", None))
            stats_before = {k: dict(v) for k, v in self.stats.items()}
            n_edges = len(self.edges)
            self._guessed = False
            fid = self.file_of.get(inner.src_id)
            targets = self._resolve_call(lang, fid, inner) if fid else None
            guessed = self._guessed
            self._guessed, self._call = saved
            for k, v in stats_before.items():
                self.stats[k].clear()
                self.stats[k].update(v)
            del self.edges[n_edges:]
            names = {(t.attrs.get("returns_names") or [None])[0] for t in targets or ()
                     if (t.attrs.get("returns_names") or [None])[0] not in (t.attrs.get("type_params") or ())}
            out = names.pop() if targets and not guessed and len(names) == 1 else None
        self._chain_memo[key] = out
        return out

    def _hint_type(self, lang: str, call: CallSite, h) -> Optional[str]:
        """The type name an argument hint stands for: `$x` a variable, `$a.b` a field, a call its return type."""
        if isinstance(h, CallSite):
            return self._generic_return_name(lang, h)
        if not isinstance(h, str) or not h.startswith("$"):
            return h if isinstance(h, str) else None
        var, _, field = h[1:].partition(".")
        if field:
            if not self.types_by_name.get((lang, var)) and any(k.match(var) for k in (self._INTS, self._FLOATS, self._BOOLS)):
                return var      # Integer.MAX_VALUE, Double.NaN: a constant of that built-in type
            if var in ("this", "self"):
                tids = [call.enclosing_type] if call.enclosing_type else []
            else:
                tids = self.types_by_name.get((lang, var)) or []
                if not tids:
                    tid = self._generic_var_type(lang, call.src_id, var, call.enclosing_type)
                    tids = [tid] if tid and tid != "external" else []
            for tid in tids:
                for t in self._chain(tid):
                    if field in self.field_type.get(t, {}):
                        return self.field_type[t][field]
            return None
        for holder in (call.src_id, call.enclosing_type):
            if holder and holder in self.nodes:
                raw = self._generic_decls(lang, holder)[1]
                if var in raw:
                    return raw[var]
        return None

    def _by_arg_types(self, lang: str, call: CallSite, fns: list) -> list:
        """Overloads with as many parameters: keep those whose parameter types fit what the arguments show."""
        hints = [self._hint_type(lang, call, h) for h in call.args]
        if not any(hints):
            return fns
        scored = []
        for f in fns:
            types, score = f.attrs.get("param_types") or [], 0
            for i, h in enumerate(hints):
                p = types[i] if i < len(types) else (types[-1] if types and types[-1].endswith("...") else "")
                v = self._arg_fits(lang, h, p) if h else 0
                if v < 0:
                    score = -1
                    break
                score += v
            scored.append((score, f))
        best = max(sc for sc, _ in scored)
        return [f for sc, f in scored if sc == best] if best >= 0 else fns

    def _file_text(self, fid: str) -> list[str]:
        if fid not in self._text_cache:
            if len(self._text_cache) >= 8:
                # Calls are resolved a file at a time, so a few files are enough; keeping every file's text
                # cost hundreds of MB on a large repo.
                self._text_cache.clear()
            try:
                self._text_cache[fid] = source_lines(self.repos[_repo_of(fid)] / self.nodes[fid].path)
            except OSError:
                self._text_cache[fid] = []
        return self._text_cache[fid]

    def _near(self, fid: str) -> set:
        """Files this one imports, and the files those import in turn (a package's index re-exporting its parts)."""
        key = ("near", fid)
        got = self._file_set(key)
        if got is None:
            first = set(self.import_targets.get(fid, ()))
            got = self._keep_set(key, first | {t for f in first for t in self.import_targets.get(f, ())})
        return got

    def _name_index(self, lang: str, name: str) -> dict:
        """Where the callables of one name are declared, looked up by file and directory. A common name (get, run)
        has thousands of declarations in a large repo, and scanning them for every call was quadratic."""
        key = (lang, name)
        ix = self._name_ix.get(key)
        if ix is None:
            ix = {"free": [], "typed": [], "free_file": defaultdict(list), "typed_file": defaultdict(list),
                  "free_dir": {}, "free_dir_base": {}, "stem": {}, "stem_free": {}, "owners": set(), "typed_owners": set()}
            for i, c in enumerate(self.by_name.get(key, ())):
                f = self.file_of.get(c.id)
                typed = bool(c.attrs.get("type_id"))
                ix["owners"].add(c.parent_id)
                if typed:
                    ix["typed"].append(c)
                    ix["typed_file"][f].append((i, c))
                    ix["typed_owners"].add(c.parent_id)
                else:
                    ix["free"].append(c)
                    ix["free_file"][f].append((i, c))
                if f is None:
                    continue
                d = self.nodes[f].path.rpartition("/")[0]
                if not typed:
                    ix["free_dir"].setdefault(d, c)
                    ix["free_dir_base"].setdefault(d.rsplit("/", 1)[-1], c)
                ix["stem"].setdefault(self.nodes[f].name.rsplit(".", 1)[0], c)
                if not typed:
                    ix["stem_free"].setdefault(self.nodes[f].name.rsplit(".", 1)[0], c)
            self._name_ix[key] = ix
        return ix

    @staticmethod
    def _in_files(by_file: dict, files, also: Optional[str] = None) -> list:
        """The declarations in any of these files (or in `also`), in declaration order. Walks whichever side is
        smaller: a Go package's files all import each other, so `files` can be thousands long for every call."""
        if len(by_file) < len(files):
            hits = [ic for f, ics in by_file.items() if f in files or f == also for ic in ics]
        else:
            hits = [ic for f in files for ic in by_file.get(f, ())]
            if also is not None and also not in files:
                hits.extend(by_file.get(also, ()))
        hits.sort(key=lambda ic: ic[0])
        return [c for _, c in hits]

    def _generic_call(self, lang: str, fid: str, call: CallSite) -> Optional[list[Node]]:
        """A call read by the generic adapter: only the name and what the text shows of the receiver are known.
        Nearest first: the caller's own type, its file, its directory, a type or package named by the receiver,
        then a name declared once in the whole repo, which is a guess."""
        name = call.name
        cands = self.by_name.get((lang, name), [])
        types = self.types_by_name.get((lang, name), [])
        if len(self.repos) > 1:   # another repository's names are seen only through what this file imports
            reach = self._reach(fid)
            mine = lambda i: _repo_of(i) == _repo_of(fid) or self.file_of.get(i) in reach
            cands, types = [c for c in cands if mine(c.id)], [t for t in types if mine(t)]
        if not cands and not types:
            return None
        ix = self._name_index(lang, name)
        r = call.receiver

        def by_args(fns):
            """Overloads: keep those declared with as many parameters as the call passes, when that narrows it."""
            fns = list(fns)
            if len(fns) > 1:
                fns = [f for f in fns if not f.attrs.get("via_base")] or fns
            if len(fns) > 1 and call.argc >= 0:
                # A declaration with fewer parameters than the call passes cannot take it; of the rest, the one
                # with the fewest is likeliest (the others' extra parameters would need defaults).
                fit = [f for f in fns if f.attrs.get("params_seen") is None or f.attrs["params_seen"] >= call.argc]
                if fit and all(f.attrs.get("params_seen") is not None for f in fit):
                    least = min(f.attrs["params_seen"] for f in fit)
                    fit = [f for f in fit if f.attrs["params_seen"] == least]
                if len(fit) > 1 and call.args:
                    fit = self._by_arg_types(lang, call, fit)
                return fit or fns
            return fns

        def ctor_of(tids):
            out = []
            for t in tids:
                out += self.members.get(t, {}).get(name, []) or [m for c in CTORS for m in self.members.get(t, {}).get(c, [])]
                self.edges.append(Edge("instantiates", call.src_id, t, "heuristic"))
            return by_args(out) or None
        near_files = self._near(fid)
        here = self.nodes[fid].path.rpartition("/")[0]
        if r in (None, "this", "base"):
            chain = self._chain(call.enclosing_type)
            caller = self.nodes.get(call.src_id)
            if r is None and caller is not None and caller.attrs.get("explicit_self"):
                chain = []      # Go, Rust, Python: a member is reached only through the receiver
            for t in chain[1:] if r == "base" else chain:
                found = self.members.get(t, {}).get(name)
                if found:
                    return by_args(found)
            if r is not None:
                return [] if cands else None
            local = ix["free_file"].get(fid)   # declared in this file, then in this directory
            if local:
                return [local[0][1]]
            if here in ix["free_dir"]:
                return [ix["free_dir"][here]]
            if types and r is None:
                local_t = [t for t in types if self.file_of.get(t) == fid] or \
                    [t for t in types if self.file_of.get(t) in near_files] or \
                    [t for t in types if self.nodes[self.file_of.get(t, fid)].path.rpartition("/")[0] == here] or types
                if len(local_t) == 1:
                    return ctor_of(local_t)
            free = ix["free"]
            imported = self._in_files(ix["free_file"], self.import_targets.get(fid, ())) or \
                self._in_files(ix["free_file"], near_files)
            if imported:
                return by_args(imported) if len({c.parent_id for c in imported}) == 1 else []
            if len(free) == 1 and (lang, name) not in self.outside_names:
                self._guessed = True
                return list(free)
            return [] if free else None
        if r not in ("?", None, "this", "base"):
            tid = self._generic_var_type(lang, call.src_id, r, call.enclosing_type)
            if tid == "external":
                return None
            if tid:
                for t in self._chain(tid):
                    found = self.members.get(t, {}).get(name)
                    if found:
                        return by_args(found)
        if r == "?" and (call.receiver_type or call.chain is not None):
            # made on `new Foo()` or on what another call returns
            tid = (self.types_by_name.get((lang, call.receiver_type)) or [None])[0] if call.receiver_type else None
            if call.chain is not None and not call.receiver_type:
                rt = self._generic_returns(lang, call.chain)
                if rt and rt[0] is None and rt[1]:
                    return None     # a type from outside the repo
                tid = rt[0] if rt else None
            if tid:
                for t in self._chain(tid):
                    found = self.members.get(t, {}).get(name)
                    if found:
                        return by_args(found)
        if r == "?":
            pool, owners = ix["typed"], ix["typed_owners"]
            if not pool:
                return None
            near = self._in_files(ix["typed_file"], near_files, fid)
            if near and len({c.parent_id for c in near}) == 1:
                self._guessed = True
                return by_args(near)
        else:
            # The receiver names a type (static call) or a package / module directory (Go's pkg.Func, Rust's mod::f).
            owned = [c for t in self.types_by_name.get((lang, r), []) for c in self.members.get(t, {}).get(name, [])]
            if owned:
                return by_args(owned)
            if r in ix["free_dir_base"]:
                return [ix["free_dir_base"][r]]
            # module.f() reaches the module's own functions before any method of that name in it
            if r in ix["stem_free"]:
                return [ix["stem_free"][r]]
            if r in ix["stem"]:
                return [ix["stem"][r]]
            if types and r not in self.types_by_name.get((lang, r), ()) and self._generic_var_type(lang, call.src_id, r, call.enclosing_type) is None:
                # module.Type(...): a type reached through the module or package the receiver names
                via = [t for t in types if self.file_of.get(t) in near_files
                       or (self.nodes[self.file_of[t]].path.rpartition("/")[0].rsplit("/", 1)[-1] == r if t in self.file_of else False)]
                if len(via) == 1:
                    return ctor_of(via)
            if r[:1].isupper() and not self.types_by_name.get((lang, r)):
                return None      # a type from outside: Foo.bar()
            pool, owners = cands, ix["owners"]
        if (lang, name) in self.outside_names or len(name) <= 2:
            self.stats[BY_LANGUAGE[lang].NAME]["calls_guess_declined"] += 1
            return None
        if len(owners) == 1:
            self._guessed = True
            self.stats[BY_LANGUAGE[lang].NAME]["calls_by_unique_name"] += 1
            return list(pool)
        return []

    def _resolve_call(self, lang: str, fid: str, call: CallSite) -> Optional[list[Node]]:
        """None = defined outside the workspace; [] = defined here but not pinned down."""
        if getattr(BY_LANGUAGE.get(lang), "GENERIC", False):
            return self._generic_call(lang, fid, call)
        name, argc = call.name, call.argc
        self._last_src = call.src_id
        if lang == "typescript":   # one hop is too strict where a store or a module object sits between caller and function
            reach = self._reach(fid)
            defined_here = any(self.file_of.get(c.id) in reach for c in self.by_name.get((lang, name), ())) or bool(
                self.types_by_name.get((lang, name)))
        else:
            defined_here = any(self._can_see(fid, c.id) for c in self.by_name.get((lang, name), ())) or (
                lang in FILE_MODULE and bool(self.types_by_name.get((lang, name))))
        if name == ".ctor":
            tid = self._type(lang, call.receiver_type or "", call.src_id)
            if tid is None:
                return None
            ctors = self.members.get(tid, {}).get(".ctor")
            return self._pick(ctors, argc) if ctors else None
        if call.receiver is None:
            # Local functions of the caller, innermost first.
            cur = call.src_id
            while cur in self.nodes and self.nodes[cur].kind in ("callable", "test", "type"):
                if self.nodes[cur].kind == "type":
                    if lang != "python":
                        break
                    cur = self.nodes[cur].parent_id   # a method does not see its class's names, but does see the function around the class
                    continue
                local = self.members.get(cur, {}).get(name)
                if local:
                    return self._pick(local, argc)
                if lang == "python" and self.nodes.get(f"{cur}.{name}") is not None and self.nodes[f"{cur}.{name}"].kind == "type":
                    return self._py_symbol(f"{cur}.{name}", argc) or []   # a class defined in the function
                cur = self.nodes[cur].parent_id
            if lang in FILE_MODULE:
                return self._py_bare(fid, call, defined_here)
            found = self._methods(call.enclosing_type, name, argc)
            if found:
                return found
            for tn in self.cs_static[fid]:
                found = self._methods(self._type(lang, tn, call.src_id), name, argc)
                if found:
                    return found
            return [] if defined_here else None
        if lang == "typescript" and call.receiver in self.outside_imports[fid]:
            return None   # React.useState(), path.join(): a namespace from outside
        inner = call.chain
        if lang == "python" and inner is not None and inner.attr and inner.chain is None and inner.receiver_type is None \
                and self._py_bound(fid, inner.src_id, inner.receiver or ""):
            target, symbol = self._py_name(fid, inner.src_id, inner.receiver)
            sub = self.py_modules[_repo_of(target)].get(f"{self._modpath(target)}.{inner.name}") if target and symbol is None else None
            if sub:
                return self._py_symbol(self._py_export(sub, name) or "", argc)   # flask.json.dumps(): a submodule's function
        imported = (call.receiver_type is None and self._py_bound(fid, call.src_id, call.receiver or "")) if lang == "python" \
            else call.receiver in self.py_names[fid]   # a typed local of the same name hides the import
        if lang in FILE_MODULE and call.receiver not in ("this",) and imported:
            target, symbol = self._py_name(fid, call.src_id, call.receiver) if lang == "python" else self.py_names[fid][call.receiver]
            if target is None:
                return [] if defined_here else None
            if symbol is None:
                return self._py_symbol(self._py_export(target, name) or "", argc)
            tid = self._py_export(target, symbol)
            if tid is None and lang == "python":
                tid = self._py_var(target, symbol)   # `from .globals import g`: a variable of a known type
            found = self._methods(tid, name, argc)
            return found or ([] if defined_here else None)
        tid, known = self._receiver_type(lang, call)
        if tid and lang == "python" and tid in self.nodes and self.nodes[tid].kind == "callable":
            return [self.nodes[tid]] if name == "__call__" else None   # a function value: only calling it means anything
        if tid:
            found = self._methods(tid, name, argc)
            if found:
                return found
            if lang == "csharp":
                found = self._extensions(lang, fid, tid, name, argc)
                if found:
                    return found
            if not (lang == "typescript" and any(name in self.field_names.get(t, ()) for t in self._chain(tid))) and not (
                    lang == "python" and any(self.nodes[t].attrs.get("open_base") for t in self._chain(tid) if t in self.nodes)):
                return None  # the type is ours but the method is inherited from outside
            # A property that holds a function (`save: (x) => void` in an interface), or a Python class whose base
            # is made at run time: what is there is not known from the type, so fall through to the unique-name guess.
            tid, known = None, False
        if known:
            return None  # receiver has a type that is not in the workspace
        if not defined_here:
            return None
        if lang == "python" and name.startswith("__") and name.endswith("__"):
            return []   # __enter__, __call__: run implicitly on all kinds of objects, never guessed by name
        if (lang, name) in self.outside_names:
            self.stats[BY_LANGUAGE[lang].NAME]["calls_guess_declined"] += 1
            # In TypeScript most receivers are untyped, and a name seen on built-in types (push, get, keys) is
            # far more often the built-in than the one function here that shares it.
            return None if lang == "typescript" else []
        self.stats[BY_LANGUAGE[lang].NAME]["calls_by_unique_name"] += 1
        self._guessed = True
        # Receiver type unknown: accept only a name that is defined exactly once.
        cands = self._pick([c for c in self.by_name[(lang, name)] if (
            self.file_of.get(c.id) in self._reach(fid) if lang == "typescript" else self._can_see(fid, c.id))], argc)
        owners = {c.parent_id for c in cands}
        if len(owners) > 1:
            # Several declarations: if all but one are overrides or implementations of the same
            # root declaration, the call is to that root (virtual dispatch).
            roots = {o for o in owners if not any(b in owners for b in self._chain(o)[1:])}
            if len(roots) == 1 and all(next(iter(roots)) in self._chain(o) for o in owners):
                root = roots.pop()
                return [c for c in cands if c.parent_id == root]
            return []
        return cands

    def _resolve_overrides(self) -> None:
        """Link each method to the base or interface method it implements, by name and arity."""
        self.implementers: dict[str, list[str]] = defaultdict(list)
        for tid, bases in list(self.bases.items()):
            if not bases:
                continue
            for name, impls in self.members.get(tid, {}).items():
                if name in (".ctor", ".dtor"):
                    continue
                for impl in impls:
                    if impl.kind != "callable":
                        continue
                    for base in self._chain(tid)[1:]:
                        if self.nodes[base].kind != "type" or base == self.nodes[tid].parent_id:
                            continue
                        # Python and TypeScript have no overloads at run time: a method replaces the base's of that name,
                        # whatever it takes
                        cands = [c for c in self.members.get(base, {}).get(name, [])
                                 if impl.language in ("python", "typescript") or c.attrs.get("argc_max") == impl.attrs.get("argc_max")]
                        if cands:
                            self.edges.append(Edge("overrides", impl.id, cands[0].id, "heuristic"))
                            self.implementers[cands[0].id].append(impl.id)
                            break

    # -- channels ------------------------------------------------------------
    def _event_field(self, type_id: Optional[str], name: str) -> Optional[str]:
        for t in self._chain(type_id):
            fid = f"{t}.{name}"
            n = self.nodes.get(fid)
            if n is not None and n.kind == "field" and n.attrs.get("native_kind") == "event":
                return fid
        return None

    def _resolve_fields(self) -> None:
        """Link each function to the fields it reads and assigns."""
        field_id: dict[str, dict[str, str]] = defaultdict(dict)
        by_name: dict[tuple, list[str]] = defaultdict(list)
        for n in self.nodes.values():
            if n.kind == "field" and n.attrs.get("native_kind") not in ("enum_member", "event"):
                field_id[n.parent_id][n.name] = n.id
                by_name[(n.language, n.name)].append(n.id)
        found: dict[tuple, list] = {}
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            st = self.stats[BY_LANGUAGE[lang].NAME]
            for u in self._open(fid, res).field_uses:
                if u.src_id not in self.nodes:
                    continue
                target, guessed = None, False
                if u.receiver in (None, "this"):
                    if u.receiver is None and lang in FILE_MODULE:
                        continue  # a bare name in Python is a local or a global, never an attribute
                    owners = self._chain(u.enclosing_type)
                elif u.receiver == "base":
                    owners = self._chain(u.enclosing_type)[1:]
                else:
                    probe = CallSite(u.src_id, u.name, u.receiver, u.receiver_type, 0, u.line, u.enclosing_type, chain=u.chain)
                    tid, known = self._receiver_type(lang, probe)
                    owners = self._chain(tid) if tid else []
                    if lang == "csharp" and not tid and not known and len(u.name) > 3:
                        # Receiver type unknown: accept only a field name declared exactly once, and say it is a guess.
                        cands = [c for c in by_name.get((lang, u.name), ()) if self._can_see(fid, c)]
                        if len(cands) == 1 and (lang, u.name) not in self.outside_names:
                            target, guessed = cands[0], True
                for t in owners:
                    if u.name in field_id.get(t, ()):
                        target = field_id[t][u.name]
                        break
                if lang == "python" and u.access != "w" and owners:
                    self._read_getters(u, owners, target)
                if target is None or target == u.src_id:
                    continue
                st["field_uses"] += 1
                for kind in (("reads",) if u.access == "r" else ("writes",) if u.access in ("w", "i") else ("reads", "writes")):
                    slot = found.setdefault((kind, u.src_id, target), [0, u.line, False, 0])
                    slot[0] += 1
                    slot[1] = min(slot[1], u.line)
                    slot[2] = slot[2] or guessed
                    slot[3] += u.access == "i"
        self._shut()
        for (kind, src, dst), (n, line, guessed, init) in found.items():
            attrs = {"n": n, "line": line}
            if init == n:
                attrs["init"] = True  # only ever set while creating the object, never changed afterwards
            self.edges.append(Edge(kind, src, dst, "guess" if guessed else "heuristic", attrs))

    def _is_getter(self, fn: Node) -> bool:
        """A Python method that runs when its attribute is read: @property, or a decorator that is a descriptor
        class (one defining __get__, such as werkzeug's cached_property) declared somewhere in the workspace."""
        if fn.kind != "callable" or not fn.attrs.get("decorators"):
            return False
        if fn.id not in self._getter_memo:
            hit = False
            for d in fn.attrs["decorators"]:
                d = d.lstrip("@").split("(", 1)[0].strip()
                if d in ("property", "functools.cached_property", "abc.abstractproperty"):
                    hit = True
                elif re.fullmatch(r"[A-Za-z_][\w.]*", d) and not d.endswith((".setter", ".deleter")):
                    tid = self._type("python", d.rsplit(".", 1)[-1], fn.id)
                    hit = bool(tid and any("__get__" in self.members.get(t, {}) for t in self._chain(tid)))
                if hit:
                    break
            self._getter_memo[fn.id] = hit
        return self._getter_memo[fn.id]

    def _read_getters(self, u: FieldUse, owners: list[str], field: Optional[str]) -> None:
        """Reading an attribute that a getter computes runs the getter, so the read is a call. On `self`, a
        subclass may compute an attribute the class itself holds as a plain value or a getter of its own
        (Flask's Request.max_content_length over Werkzeug's): those getters are reached too, as dispatch."""
        if not hasattr(self, "_getter_memo"):
            self._getter_memo: dict[str, bool] = {}
            self._subtypes: dict[str, set] = defaultdict(set)
            for sub, bases in self.bases.items():
                for b in bases:
                    self._subtypes[b].add(sub)
        hits = []
        for t in owners:
            if field is not None and self.nodes[field].parent_id == t:
                break   # a plain value nearer than any getter
            found = [m for m in self.members.get(t, {}).get(u.name, []) if self._is_getter(m)]
            if found:
                hits.append((found[0], "property"))
                break
        if u.receiver == "this" and (hits or field is not None):
            seen, queue = set(), [owners[0]]
            while queue:
                for sub in self._subtypes.get(queue.pop(), ()):
                    if sub not in seen:
                        seen.add(sub)
                        queue.append(sub)
                        hits += [(m, "virtual") for m in self.members.get(sub, {}).get(u.name, [])[:1] if self._is_getter(m)]
        for fn, dispatch in hits:
            if fn.id != u.src_id:
                self.calls.append((u.src_id, fn.id, dispatch, "heuristic", u.line))

    def _resolve_events(self) -> None:
        """Link the code that raises an event to the code that handles it."""
        raisers: dict[str, list] = defaultdict(list)
        handlers: dict[str, list] = defaultdict(list)
        st = self.channel_stats["event"]
        for fid, res in self.results.items():
            lang = self.file_lang[fid]
            for ev in res.events:
                if ev.kind == "raise":
                    field = self._event_field(ev.enclosing_type, ev.event)
                    if field:
                        raisers[field].append((ev.src_id, ev.line))
                    continue
                if ev.receiver is None:
                    field = self._event_field(ev.enclosing_type, ev.event)
                else:
                    probe = CallSite(ev.src_id, ev.event, ev.receiver, ev.receiver_type, 0, ev.line, ev.enclosing_type)
                    tid, _ = self._receiver_type(lang, probe)
                    field = self._event_field(tid, ev.event) if tid else None
                if not field:
                    if ev.handler is None or ev.receiver is not None:
                        st["subscriptions_to_outside_events"] += 1
                    continue
                target, via = ev.src_id, "lambda"
                if ev.handler:
                    found = self._methods(ev.enclosing_type, ev.handler, 1) or self._methods(ev.enclosing_type, ev.handler, 0)
                    if found:
                        target, via = found[0].id, "method"
                handlers[field].append((target, via, ev.line, ev.src_id))
        seen = set()
        for field, subs in handlers.items():
            st["events_with_handlers"] += 1
            for raiser, _line in raisers.get(field, []):
                for target, via, line, subscriber in subs:
                    key = (raiser, target, field)
                    if key in seen or raiser not in self.nodes or target not in self.nodes:
                        continue
                    seen.add(key)
                    st["links"] += 1
                    self.edges.append(Edge("communicates", raiser, target, "heuristic", {
                        "channel": "event", "address": field, "direction": "push", "handler": via,
                        "subscriber": subscriber, "subscribed_at": line}))
            if field not in raisers:
                st["events_never_raised_here"] += 1
        st["events_declared"] = sum(1 for n in self.nodes.values()
                                    if n.kind == "field" and n.attrs.get("native_kind") == "event")

    GENERIC_DIRS = {"data", "temp", "file", "files", "json", "path", "output", "outputs", "input", "home", "user", "users",
                    "test", "tests", "docs", "static", "assets", "resources", "config", "build", "dist", "local"}

    def _route_handlers(self, routes: list) -> tuple[list, dict]:
        """The functions that answer routes. An inline handler is its own function (an adapter made it, nested in
        the function that registers the route): the registrar registers it, which the map keeps as a call so that
        whatever runs the registrar still reaches the handler. A handler given by name (server.get("/x", list))
        is the function that name refers to, when the registrar's reference to it was resolved.
        Returns the routes with their serving end moved to the handler, and each handler's registrar."""
        handlers: dict[str, str] = {}
        for n in list(self.nodes.values()):
            if n.kind == "callable" and n.attrs.get("native_kind") == "route_handler" and n.parent_id in self.nodes:
                handlers[n.id] = n.parent_id
                self.calls.append((n.parent_id, n.id, "registers", "heuristic", n.attrs.get("registered_at") or n.span_start))
        named = [i for i, (e, _) in enumerate(routes) if e.handler]
        if named:
            refs: dict[str, set] = defaultdict(set)
            wanted = {routes[i][0].src_id for i in named}
            for src, dst, dispatch, _p, _l in self.calls:
                if dispatch == "reference" and src in wanted:
                    refs[src].add(dst)
            routes = list(routes)
            for i in named:
                e, res = routes[i]
                name = e.handler.rsplit(".", 1)[-1]
                hits = {d for d in refs.get(e.src_id, ()) if d in self.nodes and self.nodes[d].name == name}
                if len(hits) == 1:
                    target = hits.pop()
                    handlers.setdefault(target, e.src_id)
                    routes[i] = (Endpoint(e.channel, e.role, target, e.address, e.line, e.method, e.literals, e.handler), res)
        return routes, handlers

    @staticmethod
    def _one_handler(cands: list, handlers: dict, method: Optional[str], literal) -> list:
        """A request that several handlers' routes fit. A route that names more of the path outright wins
        (/api/review/health over /api/review/:id), as routers pick it; then, for a request whose method is not
        written, a GET, which is what a request sent with no method is; failing both, the function that registers
        them all answers it, as it did before handlers had functions of their own. Empty when none settles it."""
        most = max(literal(r) for r in cands)
        cands = [r for r in cands if literal(r) == most]
        if len({r.src_id for r in cands}) == 1:
            return cands
        gets = [r for r in cands if r.method in (None, "GET")] if method is None else []
        if gets and len({r.src_id for r in gets}) == 1:
            return gets
        owners = {handlers.get(r.src_id) for r in cands}
        if len(owners) == 1 and None not in owners:
            r, same = cands[0], {c.method for c in cands}
            return [Endpoint(r.channel, r.role, owners.pop(), r.address, r.line, same.pop() if len(same) == 1 else None,
                             r.literals)]
        return []

    def _resolve_endpoints(self) -> None:
        """Link the two ends of channels that are not calls: an HTTP request to its route, and code that
        writes a file to code that reads it."""
        http, files = self.channel_stats["http"], self.channel_stats["file"]

        def root_of(i):  # the outermost function a nested function or inline test sits in
            cur = i
            while self.nodes[cur].parent_id in self.nodes and self.nodes[self.nodes[cur].parent_id].kind in ("callable", "test"):
                cur = self.nodes[cur].parent_id
            return cur

        def segments(path):
            path = re.sub(r"^[a-z]+://[^/]+", "", path).split("?", 1)[0].split("#", 1)[0]
            return [x for x in path.split("/") if x]
        routes, requests, io = [], [], []
        for res in self.results.values():
            for e in res.endpoints:
                if e.src_id not in self.nodes or e.channel not in ("http", "file"):
                    continue   # the other channels are linked in leyline.channels
                (routes if (e.channel, e.role) == ("http", "serve") else requests if e.channel == "http" else io).append((e, res))
        http["routes"], http["requests"] = len(routes), sum(1 for e, _ in requests if e.role != "maybe")
        routes, handlers = self._route_handlers(routes)

        def fits(route, request):
            a, b = segments(route.address), segments(request.address)
            if route.method and request.method and route.method != request.method:
                return False
            if a and _wildcard(a[-1]):
                return _fits_wildcard(a, b)
            if len(a) != len(b):
                return False
            # ASP.NET's [controller] is the class's name, which clients write in lower case
            return all(_segment_fits(x, y) for x, y in zip(a, b))
        seen = set()
        for req, _ in requests:
            cands = [r for r, _ in routes if fits(r, req)]
            # In a workspace, a route in the request's own repository wins over one in another.
            cands = [r for r in cands if _repo_of(r.src_id) == _repo_of(req.src_id)] or cands
            near = [r for r in cands if root_of(r.src_id) == root_of(req.src_id)]   # a route declared inside the same test
            chosen = near or (cands if len({r.src_id for r in cands}) == 1 else [])
            if handlers and len({r.src_id for r in chosen or cands}) > 1:   # several handlers, perhaps of one registrar
                chosen = self._one_handler(chosen or cands, handlers, req.method,
                                           lambda r: sum(1 for x in segments(r.address)
                                                         if not x.startswith(("<", "{", ":")) and not _wildcard(x))) or chosen
            if req.role == "maybe":   # a path handed to a wrapper: a request only when exactly one route serves it
                chosen = chosen if len(segments(req.address)) >= 2 and len({r.src_id for r in chosen}) == 1 else []
            elif cands and not chosen:
                http["ambiguous"] += 1
            for r in chosen:
                key = (req.src_id, r.src_id)
                if key not in seen and req.src_id != r.src_id:
                    seen.add(key)
                    http["links"] += 1
                    self.edges.append(Edge("communicates", req.src_id, r.src_id, "heuristic",
                                           {"channel": "http", "address": f"{req.method or r.method or 'ANY'} {r.address}", "line": req.line,
                                            **({"handler": True} if r.src_id in handlers else {})}))

        # Files: a writer and a reader are linked when the path fragments written in each agree.
        def tokens(strings):
            dirs, exts, names = set(), set(), set()
            for s in strings:
                s = s.replace("\\", "/")
                if len(s) > 160 or any(ch.isspace() for ch in s):
                    continue
                parts = [x for x in s.split("/") if x not in ("", ".", "..")]
                for i, seg in enumerate(parts):
                    clean = re.sub(r"\{[^}]*\}|\*", "", seg)
                    if i == len(parts) - 1 and "." in seg:
                        stem, _, ext = clean.partition(".")
                        if re.fullmatch(r"[A-Za-z0-9_]{1,10}(\.[A-Za-z0-9_]{1,10})?", ext or ""):
                            exts.add("." + ext.lower())
                            if stem and clean == seg and len(stem) > 2:
                                names.add(seg.lower())
                    elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]{3,}", seg) and seg.lower() not in self.GENERIC_DIRS:
                        dirs.add(seg.lower())
            return dirs, exts, names
        by_fn: dict[tuple, list] = defaultdict(list)
        for e, res in io:
            by_fn[(e.src_id, e.role)].extend(e.literals + res.path_strings.get(e.src_id, []) + res.path_strings.get(root_of(e.src_id), []))
        files["read_sites"] = sum(1 for e, _ in io if e.role == "read")
        files["write_sites"] = sum(1 for e, _ in io if e.role == "write")
        toks = {k: tokens(v) for k, v in by_fn.items()}
        pairs = []
        for (w, wr), (wd, we, wn) in toks.items():
            if wr != "write":
                continue
            for (r, rr), (rd, re_, rn) in toks.items():
                if rr != "read" or root_of(r) == root_of(w) or _repo_of(r) != _repo_of(w):
                    continue   # file paths are matched within one repository; across two they say too little
                shared_name = wn & rn
                shared = (wd & rd, we & re_)
                if shared_name:
                    pairs.append((w, r, sorted(shared_name)[0]))
                elif shared[0] and shared[1]:
                    pairs.append((w, r, f"{sorted(shared[0], key=lambda x: (-len(x), x))[0]}/*{sorted(shared[1], key=lambda x: (-len(x), x))[0]}"))
        fan = Counter(p[2] for p in pairs)
        for w, r, address in pairs:
            if fan[address] > 8:
                files["too_common_to_link"] += 1
                continue   # a fragment this common says nothing about who reads whose file
            files["links"] += 1
            self.edges.append(Edge("communicates", w, r, "guess", {"channel": "file", "address": address}))

    def _resolve_spawns(self) -> None:
        """Link code that launches a program to that program's entry point, when it is in the workspace."""
        st = self.channel_stats["process"]
        entries: dict[str, list[str]] = defaultdict(list)  # module -> cli entry callables
        for e in self.edges:
            if e.kind == "exposes" and self.nodes[e.src_id].attrs.get("trigger") == "cli":
                mod = self.nodes[self.file_of[e.dst_id]].parent_id if e.dst_id in self.file_of else None
                if mod:
                    entries[mod].append(e.dst_id)
        files_by_path = {(_repo_of(n.id), n.path): n.id for n in self.nodes.values() if n.kind == "file"}
        modules = [(n.path, n.id) for n in self.nodes.values() if n.kind == "module" and n.path]

        def match(text: str, repo: str):
            text = text.strip().replace("\\", "/")
            if (repo, text) in files_by_path:  # a script path, in the launching file's repository
                fid = files_by_path[(repo, text)]
                tops = [e.dst_id for e in self.edges if e.kind == "exposes" and self.file_of.get(e.dst_id) == fid]
                return tops[0] if tops else fid
            built = re.match(r"^(.*?/)?(?:dist|build|out|lib)/(.+)\.[cm]?js$", text)
            if built:   # a built script (host/dist/index.js) runs the source it was built from (host/src/index.ts)
                for src in ("src/", ""):
                    for ext in (".ts", ".mts", ".cts", ".tsx", ".js", ".mjs"):
                        source = f"{built.group(1) or ''}{src}{built.group(2)}{ext}"
                        if (repo, source) in files_by_path:
                            return match(source, repo)
            for path, mid in sorted(modules, key=lambda m: _repo_of(m[1]) != repo):   # own repository first
                stem = text.rsplit("/", 1)[-1].rsplit(".", 1)[0] if "." in text.rsplit("/", 1)[-1] else None
                if text == path or text.startswith(path + "/") or (stem and stem == path.rsplit("/", 1)[-1] and text.endswith((".dll", ".exe", ".csproj"))):
                    if not entries.get(mid):
                        continue
                    return entries[mid][0] if len(entries[mid]) == 1 else mid
            return None

        seen = set()
        for fid, res in self.results.items():
            for sp in res.spawns:
                st["launch_sites"] += 1
                hits = [(match(s, _repo_of(fid)), s, "heuristic") for s in sp.strings]
                hits = [h for h in hits if h[0]]
                if not hits:
                    hits = [(match(s, _repo_of(fid)), s, "guess") for s in dict.fromkeys(sp.file_strings)]
                    hits = [h for h in hits if h[0]]
                if not hits:
                    st["launches_of_outside_programs"] += 1
                    continue
                for target, text, precision in dict.fromkeys(hits):
                    if (sp.src_id, target) in seen or target == sp.src_id:
                        continue
                    seen.add((sp.src_id, target))
                    st["links"] += 1
                    self.edges.append(Edge("communicates", sp.src_id, target, precision, {
                        "channel": "process", "address": text,
                        "direction": "both" if sp.pipes else "start", "pipes": sp.pipes, "launched_at": sp.line}))

    # -- flows ---------------------------------------------------------------
    def _dispatch_reaches(self, home: str, impl: str) -> bool:
        """Can a program whose entry is in `home` be running this implementation of an interface?"""
        if self._visible(home) is not None:
            return self._can_see(home, impl)
        # A loose file with no project: it can only be composed of the modules its own module imports.
        mod = self.nodes[home].parent_id
        if mod not in self._loose_reach:
            files = {f for f in self.file_lang if self.nodes[f].parent_id == mod}
            self._loose_reach[mod] = {mod} | {e.dst_id for e in self.edges if e.kind == "imports" and e.src_id in files}
        target = self.file_of.get(impl)
        return target is not None and self.nodes[target].parent_id in self._loose_reach[mod]

    def _build_flows(self, max_depth: int = 8, max_steps: int = 300) -> None:
        """Walk the call graph from every entry point and test, in source order."""
        out: dict[str, list] = defaultdict(list)
        for src, dst, _disp, _prec, line in self.calls:
            out[src].append(((line or 0) + self.call_col.get((src, dst, line), 0) / 10000, dst, "calls", None))
        if not self.keep_results:
            self.call_col.clear()   # read only here (an entry per call): the walk's steps reuse its memory
        for e in self.edges:
            if e.kind == "communicates":
                a = e.attrs or {}
                if a.get("channel") in ("file", "db", "format"):
                    continue  # writing a file, a row or a key does not run whoever reads it later
                # A registered implementation is listed ahead of the other implementations of its interface.
                order = 10 ** 9 - 2 if a.get("channel") == "di" and "registered_in" in a else 10 ** 9
                out[e.src_id].append((a.get("launched_at") or a.get("line") or order, e.dst_id, a.get("channel", "channel"),
                                      a.get("subscriber")))
        # A call to an interface or base method may land in any implementation.
        for base, impls in self.implementers.items():
            for impl in impls:
                out[base].append((10 ** 9 - 1, impl, "dispatch", None))
        # A test declared inline runs where its runner call sits.
        for n in self.nodes.values():
            if n.kind == "test" and n.parent_id:
                out[n.parent_id].append((n.span_start or 0, n.id, "runs", None))
        for lst in out.values():
            lst.sort(key=lambda x: (x[0], x[1]))
        starts = []
        for e in self.edges:
            if e.kind == "exposes":
                ep = self.nodes[e.src_id]
                starts.append((e.dst_id, "entry", ep.attrs.get("trigger")))
        for n in self.nodes.values():
            if n.kind == "test" or n.attrs.get("is_test"):
                starts.append((n.id, "test", n.attrs.get("framework")))
        keep = self._flow_select(out, starts)
        for start, kind, detail in starts:
            if start not in self.nodes or (keep is not None and start not in keep):
                continue
            fid = f"flow:{start}"
            entry_file = self.file_of.get(start)
            seen, steps, truncated = {start}, [(0, 0, start, "start", None, None)], False
            stack = [(start, 0, 0)]
            # Depth-first, pre-order, each callable listed once per flow.
            def walk(node, depth, parent_seq, home):
                nonlocal truncated
                if depth >= max_depth:
                    if out.get(node):
                        truncated = True
                    return
                for line, dst, via, subscriber in out.get(node, ()):
                    if dst in seen or dst not in self.nodes:
                        continue
                    if via == "event" and subscriber not in seen:
                        continue  # nobody in this flow subscribed, so the handler does not run here
                    if via in ("dispatch", "di") and home and self.file_lang.get(home) == "csharp" \
                            and not self._dispatch_reaches(home, dst):
                        continue  # an implementation in a project this program does not reference
                    if kind == "test" and via == "runs":
                        continue
                    if len(steps) >= max_steps:
                        truncated = True
                        return
                    seen.add(dst)
                    seq = len(steps)
                    steps.append((seq, depth + 1, dst, via, int(line) if line < 10 ** 9 - 1 else None, parent_seq))
                    # A launched program is its own composition: what it can reach is judged from there.
                    walk(dst, depth + 1, seq, self.file_of.get(dst, home) if via == "process" else home)
            walk(start, 0, 0, entry_file)
            n = self.nodes[start]
            name = _test_title(n) if n.kind == "test" else start.split(":", 2)[-1].split("::")[-1]
            mods = {self.nodes[self.file_of[s[2]]].parent_id for s in steps if s[2] in self.file_of}
            self.flows.append((fid, name, "static", start, 0.0, None, "fact", SOURCE, {
                "kind": kind, "detail": detail, "steps": len(steps), "truncated": truncated,
                "modules": sorted(m for m in mods if m)}))
            self.flow_steps.add(fid, steps)

    def _flow_select(self, out: dict, starts: list) -> Optional[set]:
        """The starts to walk, given the call graph the walk reads (`out`); None for all of them.
        leyline.incremental walks again only the flows an edit can have changed."""
        return None

    def _py_symbol(self, node_id: str, argc: int) -> Optional[list[Node]]:
        n = self.nodes.get(node_id)
        if n is None:
            return None
        if n.kind == "callable":
            return [n] if argc < 0 else (self._pick([n], argc) or [n])
        if n.kind == "type":
            init = next((found for c in CTORS for found in [self._methods(n.id, c, argc)] if found), None)
            self.edges.append(Edge("instantiates", self._last_src, n.id, "heuristic"))
            return init or None
        return None

    def _py_bare(self, fid: str, call: CallSite, defined_here: bool) -> Optional[list[Node]]:
        self._last_src = call.src_id
        lang = self.file_lang[fid]
        own = f"{_repo_of(fid)}:{lang}:{self._modpath(fid)}.{call.name}"
        hit = self._py_symbol(own, call.argc)
        if hit is not None:
            return hit
        if self._py_bound(fid, call.src_id, call.name):
            target, symbol = self._py_name(fid, call.src_id, call.name)
            return self._py_symbol(self._py_export(target, symbol or call.name) or "", call.argc) if target else None
        if lang == "typescript" and own not in self.nodes and call.name not in self.outside_imports[fid]:
            # Not declared here and not imported by name: a local taken out of something (`const { save } = await
            # load()`). If exactly one exported function reachable from this file has the name, say so as a guess.
            reach = self._reach(fid)
            cands = [c for c in self.by_name.get((lang, call.name), ())
                     if self.file_of.get(c.id) in reach and c.parent_id == self.file_of.get(c.id) and c.attrs.get("visibility") == "public"]
            if len(cands) == 1 and len(call.name) > 3:
                self._guessed = True
                self.stats[BY_LANGUAGE[lang].NAME]["calls_by_unique_name"] += 1
                return cands
        return None  # builtins, star imports, and classes with no constructor

    # -- write ---------------------------------------------------------------
    def _owner(self, node_id: str) -> str:
        """The repository a node's rows are written under."""
        r = _repo_of(node_id)
        return r if r in self.repos else self.repo

    def _final(self) -> tuple[list, list]:
        """The edges and calls as they are written: edges whose ends both exist, each once, links between
        repositories marked, and a contains edge per parent; calls whose caller exists, each once, sorted.
        Made once per run."""
        got = getattr(self, "_final_rows", None)
        if got is not None:
            return got
        multi, owner = len(self.repos) > 1, self._owner
        # Drop edges whose endpoints did not survive (defensive) and exact duplicates.
        seen, edges = set(), []
        for e in self.edges:
            key = (e.kind, e.src_id, e.dst_id, tuple(sorted((e.attrs or {}).items(), key=str)).__repr__())
            if e.src_id in self.nodes and e.dst_id in self.nodes and key not in seen:
                seen.add(key)
                if multi and owner(e.dst_id) != owner(e.src_id):
                    e.attrs = {**(e.attrs or {}), "to_repo": owner(e.dst_id)}   # a link between repositories
                edges.append(e)
        for n in self.nodes.values():
            if n.parent_id:
                edges.append(Edge("contains", n.parent_id, n.id, "exact", None))   # no attrs: not an empty dict per node
        calls = sorted({c for c in self.calls if c[0] in self.nodes})
        self._final_rows = (edges, calls)
        return self._final_rows

    def _write(self, con) -> None:
        owner = self._owner
        with con:
            for repo, root in self.repos.items():
                store.clear_facts(con, repo)
                store.write_nodes(con, [n for n in self.nodes.values() if owner(n.id) == repo], repo, SOURCE, self.commits[repo])
                store.set_root(con, repo, root)
            edges, calls = self._final()
            for repo in self.repos:   # each row carries the commit of the repository its source is in
                store.write_edges(con, [e for e in edges if owner(e.src_id) == repo], SOURCE, self.commits[repo])
                store.write_calls(con, [c for c in calls if owner(c[0]) == repo], self.commits[repo])
            store.write_flows(con, self.repo, self.flows, self.flow_steps)
            self._write_coverage(con, len(self.flows), len(self.flow_steps))
            store.rebuild_derived(con)

    def _write_coverage(self, con, flows: int, steps: int) -> None:
        """What each extractor did, per repo, with the run's counts."""
        multi = len(self.repos) > 1
        from .adapters import ADAPTERS
        # Counts are for the whole run; in a workspace every member repo gets the same rows, marked as such.
        ws = {"workspace": sorted(self.repos)} if multi else {}
        for repo in self.repos:
            commit = self.commits[repo]
            for a in ADAPTERS:
                st = dict(self.stats.get(a.NAME, {}))
                status = "ok" if st.get("files") else "no_files"
                store.write_coverage(con, repo, a.NAME, a.VERSION, status, commit, {**st, **ws} if st else st)
            for channel in ("event", "process", "di", "http", "rpc", "queue", "db", "file", "format"):
                store.write_coverage(con, repo, f"communicates:{channel}", "0.1", "ok", commit,
                                     dict(self.channel_stats.get(channel, {})))
            store.write_coverage(con, repo, "flows:static", "0.1", "ok", commit, {"flows": flows, "steps": steps, **ws})
            for name in ("exact:roslyn", "exact:scip"):
                info = dict(self.exact_stats.get(name, {}))
                status = info.pop("status", "not_analyzed")
                store.write_coverage(con, repo, name, "1" if status == "ok" else "-",
                                     status if status in ("ok", "failed") else "not_analyzed", commit, info)
            kept = con.execute("SELECT format, stats FROM coverage_runs ORDER BY created DESC LIMIT 1").fetchone()
            if kept:   # an imported coverage file outlives a re-index
                store.write_coverage(con, repo, "coverage", kept["format"], "ok", commit, json.loads(kept["stats"] or "{}"))
            else:
                store.write_coverage(con, repo, "coverage", "-", "not_analyzed", commit, {})
        if multi:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('workspace', ?)", (json.dumps(list(self.repos)),))


def _normalize(parts: tuple) -> list[str]:
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        elif p not in (".", ""):
            out.append(p)
    return out


def workspace(con, roots: list[str | Path], repo_id: Optional[str] = None) -> list[tuple[Path, str]]:
    """The (root, repo id) pairs to index together. Repositories named together make the store a workspace,
    and it remembers them: indexing any one of them later indexes all of them again, so the links between
    them are kept. A member whose directory is gone is left as it was stored."""
    given = [(Path(r).resolve(), repo_id if repo_id and len(roots) == 1 else Path(r).resolve().name) for r in roots]
    row = con.execute("SELECT value FROM meta WHERE key = 'workspace'").fetchone()
    stored = json.loads(row[0]) if row else []
    if len(given) == 1 and not stored:
        return given
    out = list(given)
    for rid in stored:
        here = store.roots(con).get(rid)
        root = (str(here),) if here is not None else None
        if any(rid == g[1] or (root and Path(root[0]) == g[0]) for g in given):
            continue
        if root and Path(root[0]).is_dir():
            out.append((Path(root[0]), rid))
        else:
            print(f"leyline: workspace member {rid} is not at {root[0] if root else '?'}; its facts are kept as they were",
                  file=sys.stderr)
    ids = [rid for _, rid in out]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"two repositories would have the id {dupes[0]!r}; index them from directories with different names")
    return out


def index(root: str | Path | list, db_path: str | Path, repo_id: Optional[str] = None, exact: str = "off",
          scip: Optional[list[str]] = None, full: bool = False, _verify: bool = True) -> dict:
    """Index a repository, or several as one workspace (`root` a list). `exact` is off, auto, roslyn or scip:
    whether a compiler's view of the references replaces the syntax-based one (see leyline.exact). `scip`
    lists index.scip files. When the store was made by an earlier run, only what changed since is done again
    (leyline.incremental), and the store comes out as a full run would leave it; `full` makes it a full run."""
    from . import incremental, tours

    con = store.connect(db_path)
    inc = None
    try:
        members = workspace(con, list(root) if isinstance(root, (list, tuple)) else [root], repo_id)
        ix = Indexer(members[0][0], members[0][1], members[1:])
        ix.exact_mode, ix.scip_paths = ("scip" if scip and exact == "off" else exact), list(scip or [])
        ix.keep_results = False
        inc = incremental.Run(con, db_path, ix, full=full)
        began = time.perf_counter()
        # The run makes millions of objects that live until it ends; the cycle collector would scan them over and
        # over (a third of loading a large repository's cached parse output) and find nothing to free.
        gc.disable()
        try:
            stats = ix.run(con)
        finally:
            gc.enable()
        stats.update(ix.exact_stats)
        left = {r: skipped_summary(ix.skipped.get(r, []), ix.failed.get(r, [])) for r in ix.repos}
        stats["left_out"] = left if len(ix.repos) > 1 else left[ix.repo]
        with con:   # so the overview and the map page can say what is not in the map
            for r, v in left.items():
                con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"left_out:{r}", json.dumps(v)))
            # Which Leyline made this map: a newer one maps again even where no file changed (see loop.refresh).
            con.execute("INSERT OR REPLACE INTO meta VALUES ('made_by', ?)", (incremental.code_version(),))
        # Everything from here reads the store. Letting the indexer go first keeps its memory (most of a GB on a
        # large repo) from adding to what clustering and the pattern matchers hold.
        repo, timing, repos = ix.repo, ix.timing, dict(ix.repos)
        inc.release()
        del ix
        mark = time.perf_counter()
        multi = len(repos) > 1
        systems = {r: inc.systems(con, r) for r in repos}
        stats["systems"] = systems if multi else systems[repo]
        if inc.full:
            with con:   # a workspace re-clusters every repo, so it rebuilds everything
                store.rebuild_derived(con, systems_of=None if multi else repo)
        timing["systems"] = round(time.perf_counter() - mark, 3)
        mark = time.perf_counter()
        stats["patterns"] = inc.patterns(con, repo)
        for other in list(repos)[1:]:
            with con:
                store.write_coverage(con, other, "patterns:structural", "0.1", "ok", None, stats["patterns"])
        tour = {r: tours.generate(con, r) for r in repos}
        stats["tour"] = tour if multi else tour[repo]
        timing["patterns_and_tour"] = round(time.perf_counter() - mark, 3)
        stats["stale_annotations"] = store.refresh_stale(con)
        if multi:
            stats["workspace"] = {"repos": {r: str(p) for r, p in repos.items()}, "cross_repo": _cross_counts(con)}
        files = sum(v.get("files", 0) for k, v in stats.items() if k.startswith(("tree-sitter", "generic")))
        lines = con.execute(f"SELECT COALESCE(SUM(span_end), 0) FROM nodes WHERE kind = 'file' AND repo_id IN ({','.join('?' * len(repos))})",
                            list(repos)).fetchone()[0]
        total = round(time.perf_counter() - began, 3)
        stats["timing"] = {"total_seconds": total, **timing, "files": files, "lines": lines,
                           "lines_per_second": round(lines / total) if total else 0}
        stats["incremental"] = inc.summary()
        with con:  # kept in the store so a later reader can see what the map cost to build
            for r in repos:
                con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"timing:{r}", json.dumps(stats["timing"])))
                con.execute("INSERT OR REPLACE INTO extractor_coverage VALUES (?,?,?,?,?,?)",
                            (r, "timing", "-", "ok", None, json.dumps(stats["timing"])))
        inc.finish(con)
        if _verify and stats["incremental"]["mode"] == "incremental" and os.environ.get("LEYLINE_VERIFY"):
            stats["incremental"]["differs"] = incremental.verify(db_path, members, exact, scip)
        inc = None
        return stats
    finally:
        if inc is not None:
            inc.abandon()
        con.close()


def _cross_counts(con) -> list[dict]:
    from .query import cross_repo
    return cross_repo(con)["pairs"]
