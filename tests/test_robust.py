"""Indexing does not crash or hang on what real repositories hold: half-written and binary files, odd encodings,
symlinks that go nowhere or in circles, submodules, files git lists that are gone, unreadable files, code nested
too deep for the default recursion limit, and a parser process that dies or gets stuck. Each is either read or left
out with a reason, and the run finishes."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from leyline import cli, indexer
from leyline.indexer import index, read_source, scan, source_lines

HERE = Path(__file__).parent
HAVE_GIT = shutil.which("git") is not None


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "init.defaultBranch=main",
                    "-c", "protocol.file.allow=always", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


def write(root: Path, rel: str, data) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    return p


def symlink(target: str, link: Path) -> bool:
    try:
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError):   # Windows without the privilege to make one
        return False


def reasons(listing) -> dict:
    return {f: why for f, why in listing.skipped}


def ordinary(root: Path) -> None:
    write(root, "good.py", "def ok():\n    return helper()\n\n\ndef helper():\n    return 1\n")
    write(root, "half.py", "def broken(:\n    x = [1, 2,\n")
    write(root, "half.ts", "export class A {\n  m( { return \n")
    write(root, "bom.py", b"\xef\xbb\xbfdef bom():\n    return 1\n")
    write(root, "latin1.py", "# caf\xe9\ndef cafe():\n    return 1\n".encode("latin-1"))
    write(root, "utf16.py", "def sixteen():\n    return 1\n".encode("utf-16"))
    write(root, "crlf.py", b"def crlf():\r\n    return other()\r\n\r\ndef other():\r\n    return 2\r\n")
    write(root, "empty.py", "")
    write(root, "comments.go", "// nothing here\n")
    write(root, "dir with space/ünïcødé файл.py", "def unicode_name():\n    return 1\n")
    write(root, "nul.java", 'class K { String s = "\0a\0"; void m() {} }\n')   # a NUL in a literal is still text
    write(root, "blob.py", bytes(range(256)) * 40)
    write(root, "app.min.js", "function a(){return 1}\n")
    write(root, "long.js", "var a=[" + ",".join(["1"] * 40000) + "];\n")
    write(root, "big.py", "x = 1\n" * 4000)
    write(root, "node_modules/pkg/index.js", "module.exports = function vendored() {}\n")
    write(root, "vendor/modules.txt", "# example.com/y v1.0.0\n")
    write(root, "vendor/example.com/y/y.go", "package y\nfunc Vendored() {}\n")


@pytest.fixture
def small_files(monkeypatch):
    monkeypatch.setenv("LEYLINE_MAX_FILE_MB", "0.01")   # big.py (24 KB) is over this


@pytest.mark.skipif(not HAVE_GIT, reason="needs git")
def test_hostile_repository_is_indexed_and_says_what_it_left_out(tmp_path, small_files, capsys):
    root = tmp_path / "mess"
    ordinary(root)
    write(root, "tracked_then_deleted.py", "def gone():\n    pass\n")
    write(root, ".gitignore", "ignored/\n")
    write(root, "ignored/i.py", "def ignored():\n    pass\n")
    sub = tmp_path / "subsrc"
    write(sub, "s.py", "def s():\n    pass\n")
    git(sub, "init")
    git(sub, "add", ".")
    git(sub, "commit", "-m", "x")
    git(root, "init")
    git(root, "submodule", "add", sub.as_uri(), "libs/sub.js")   # a submodule whose name looks like a source file
    # Windows checks out a symlink loop or a link to "." as a file git cannot add: the links are left out there.
    links = os.name != "nt" and all([symlink("good.py", root / "link.py"), symlink("missing.py", root / "dangling.py"),
                 symlink("loop_b.py", root / "loop_a.py"), symlink("loop_a.py", root / "loop_b.py"),
                 symlink(".", root / "loopdir.ts")])
    git(root, "add", "-A")
    git(root, "commit", "-m", "x")
    (root / "tracked_then_deleted.py").unlink()

    listing = scan(root)
    why = reasons(listing)
    assert why["tracked_then_deleted.py"].startswith("deleted")
    assert why["libs/sub.js"].startswith("submodule")
    assert why["blob.py"] == "binary"
    assert why["app.min.js"] == "minified"
    assert why["big.py"].startswith("larger than")
    assert why["node_modules/pkg/index.js"] == why["vendor/example.com/y/y.go"] == "dependency directory"
    if links:
        assert why["dangling.py"] == "broken symlink"
        assert why["loop_a.py"] == why["loop_b.py"] == "symlink loop"
        assert why["loopdir.ts"] == "symlink to a directory"
        assert why["link.py"].startswith("symlink to another listed file")
    assert "ignored/i.py" not in listing.files and "ignored/i.py" not in why   # .gitignore is git's to apply
    for f in ("good.py", "half.py", "half.ts", "bom.py", "latin1.py", "utf16.py", "crlf.py", "empty.py", "comments.go",
              "nul.java", "dir with space/ünïcødé файл.py"):
        assert f in listing.files, f

    db = tmp_path / "s.db"
    stats = index(root, db, exact="off")
    left = stats["left_out"]
    assert left["binary"]["count"] == 1 and left["minified"]["count"] == 1
    err = capsys.readouterr().err
    assert "left out binary: blob.py" in err
    con = sqlite3.connect(db)
    names = {r[0] for r in con.execute("SELECT name FROM nodes WHERE kind = 'callable'")}
    assert {"ok", "helper", "bom", "cafe", "sixteen", "crlf", "other", "unicode_name"} <= names
    assert json.loads(con.execute("SELECT value FROM meta WHERE key = 'left_out:mess'").fetchone()[0])["binary"]
    con.close()


def test_walk_without_git_applies_gitignore_and_skips_nested_repositories(tmp_path):
    root = tmp_path / "plain"
    write(root, "a.py", "def a():\n    pass\n")
    write(root, ".gitignore", "build/\n*.log.py\n!keep.log.py\n/rooted.py\n**/gen/*.py\n# a comment\n")
    write(root, "build/b.py", "x = 1\n")
    write(root, "x.log.py", "x = 1\n")
    write(root, "keep.log.py", "x = 1\n")
    write(root, "rooted.py", "x = 1\n")
    write(root, "pkg/rooted.py", "x = 1\n")
    write(root, "pkg/gen/g.py", "x = 1\n")
    write(root, "pkg/.gitignore", "local.py\n")
    write(root, "pkg/local.py", "x = 1\n")
    write(root, "other/local.py", "x = 1\n")
    write(root, "node_modules/m.js", "x = 1\n")
    write(root, "inner/.git/HEAD", "ref: refs/heads/main\n")
    write(root, "inner/i.py", "x = 1\n")
    listing = scan(root)
    if listing.how == "git":   # inside a checkout (a temp dir under a repository): nothing to test here
        pytest.skip("the temporary directory is inside a git repository")
    assert sorted(f for f in listing.files if f.endswith(".py")) == [
        "a.py", "keep.log.py", "other/local.py", "pkg/rooted.py"]
    assert reasons(listing)["inner"].startswith("nested repository")


@pytest.mark.skipif(sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="permissions do not stop root, and Windows has no chmod 000")
def test_unreadable_files_are_left_out(tmp_path):
    root = tmp_path / "r"
    write(root, "a.py", "def a():\n    pass\n")
    secret = write(root, "secret.py", "def s():\n    pass\n")
    secret.chmod(0)
    try:
        listing = scan(root)
        assert reasons(listing)["secret.py"].startswith("cannot read")
        stats = index(root, tmp_path / "s.db", exact="off")
        assert stats["left_out"]["cannot read: Permission denied"]["count"] == 1
    finally:
        secret.chmod(0o644)


def test_utf16_and_line_numbering(tmp_path):
    p = write(tmp_path, "u.cs", "class A {\r\n  void M() {}\r\n}\r\n".encode("utf-16"))
    assert read_source(p).decode("utf-8").startswith("class A")
    # A form feed is not a line break to the parser; str.splitlines would make it one and shift every later line.
    q = write(tmp_path, "f.py", "# one\x0c two\ndef f():\n    return 1\n")
    assert source_lines(q)[1] == "def f():"


def parse_job(root: Path, f: str):
    return (str(root), "r", (f, "." + f.rsplit(".", 1)[1], "", "r:module:."))


def test_deep_nesting_is_read_on_a_large_stack_or_reported(tmp_path):
    write(tmp_path, "elseif.ts", "export function f(x: number) {\n" + "".join(
        f"  {'else ' if i else ''}if (x === {i}) {{ return g{i}(); }}\n" for i in range(3000)) + "}\n")
    write(tmp_path, "absurd.py", "y = " + "f(" * 20000 + ")" * 20000 + "\n")
    ok = indexer._parse_one(parse_job(tmp_path, "elseif.ts"))
    if os.name == "nt":   # Windows gives a thread less stack: it may be reported instead, which is the fallback
        assert (ok[7] is None and ok[6]) or ok[7] == "nested too deeply to read"
    else:
        assert ok[7] is None and ok[6]   # past the default recursion limit, read on the large stack
    bad = indexer._parse_one(parse_job(tmp_path, "absurd.py"))
    assert bad[6] is None and bad[7] == "nested too deeply to read"


def test_a_file_gone_since_listing_fails_alone(tmp_path):
    got = indexer._parse_one(parse_job(tmp_path, "gone.py"))
    assert got[6] is None and got[7].startswith("cannot read")


# -- the worker pool ---------------------------------------------------------------------------------------------
class Boom:
    """An adapter that kills or stalls the process parsing a file named for it."""
    NAME, LANGUAGE, EXTENSIONS, VERSION = "boom", "boom", (".boom",), "0"

    @staticmethod
    def parse(repo, rel_path, file_id, src, module=""):
        if b"die" in src:
            os._exit(7)
        if b"hang" in src:
            time.sleep(600)
        from leyline.model import FileResult
        return FileResult()


def many(root: Path, n: int, special: dict) -> list:
    work = []
    for i in range(n):
        name = f"f{i:03}.py"
        write(root, name, f"def f{i}():\n    return {i}\n")
        work.append((name, ".py", "", "r:module:."))
    for name, body in special.items():
        write(root, name, body)
        work.insert(len(work) // 2, (name, ".boom", "", "r:module:."))
    return work


def fork_only():
    # The stand-in adapter reaches the workers only through fork, which is only safe to rely on on Linux.
    if not sys.platform.startswith("linux"):
        pytest.skip("needs fork, on Linux")


@pytest.fixture
def boom(monkeypatch):
    fork_only()
    monkeypatch.setitem(indexer.BY_EXTENSION, ".boom", Boom)
    monkeypatch.setenv("LEYLINE_START_METHOD", "fork")
    monkeypatch.setenv("LEYLINE_JOBS", "2")
    monkeypatch.setattr(indexer, "PARALLEL_MIN_FILES", 10)


def test_a_dead_worker_loses_only_its_file(tmp_path, boom, capsys):
    work = many(tmp_path, 120, {"x.boom": "die"})
    died: list = []
    out = list(indexer._parse_all(tmp_path, "r", work, died))
    assert [o[0] for o in out] == [w[0] for w in work]   # every file, in order
    assert died == ["x.boom"]
    failed = {o[0]: o[7] for o in out if o[6] is None}
    assert failed == {"x.boom": "the parser process died on this file"}
    assert "x.boom" in capsys.readouterr().err


def test_a_stuck_worker_is_stopped(tmp_path, boom, monkeypatch):
    monkeypatch.setenv("LEYLINE_PARSE_TIMEOUT", "3")
    work = many(tmp_path, 40, {"y.boom": "hang"})
    began = time.monotonic()
    out = list(indexer._parse_all(tmp_path, "r", work, []))
    assert time.monotonic() - began < 120
    failed = {o[0]: o[7] for o in out if o[6] is None}
    assert list(failed) == ["y.boom"] and failed["y.boom"].startswith("parsing took longer")
    assert len(out) == len(work)


def _exit_at_start():
    os._exit(3)


def test_workers_that_cannot_start_fall_back_to_this_process(tmp_path, boom, monkeypatch, capsys):
    work = many(tmp_path, 60, {})
    monkeypatch.setattr(indexer, "_worker_init", _exit_at_start)
    died: list = []
    out = list(indexer._parse_all(tmp_path, "r", work, died))
    assert [o[0] for o in out] == [w[0] for w in work] and all(o[6] for o in out)
    assert died == [] and "parsing in this process instead" in capsys.readouterr().err


def test_spawned_workers_give_what_one_process_gives(tmp_path, monkeypatch):
    """spawn is how workers start on macOS and Windows; it must parse exactly as the single process does."""
    work = many(tmp_path, 40, {})
    monkeypatch.setattr(indexer, "PARALLEL_MIN_FILES", 10)
    monkeypatch.setenv("LEYLINE_JOBS", "1")
    serial = list(indexer._parse_all(tmp_path, "r", work))
    monkeypatch.setenv("LEYLINE_JOBS", "2")
    monkeypatch.setenv("LEYLINE_START_METHOD", "spawn")
    spawned = list(indexer._parse_all(tmp_path, "r", work))
    assert spawned == serial


def test_spawn_is_the_default_where_fork_is_unsafe(monkeypatch):
    monkeypatch.delenv("LEYLINE_START_METHOD", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert indexer._start_method() == "spawn"
    monkeypatch.setenv("LEYLINE_START_METHOD", "no-such-method")
    with pytest.raises(ValueError):
        indexer._start_method()


# -- the store ---------------------------------------------------------------------------------------------------
def test_a_damaged_store_gets_a_message_not_a_traceback(tmp_path, capsys):
    db = tmp_path / "leyline.db"
    db.write_text("this is not a database")
    assert cli.main(["--db", str(db), "overview"]) == 2
    err = capsys.readouterr().err
    assert "damaged or is not a Leyline store" in err and "Traceback" not in err


def test_a_store_being_written_can_still_be_read(tmp_path):
    root = HERE / "fixture2"
    db = tmp_path / "s.db"
    index(root, db, exact="off")
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")   # as a map in progress holds it
    try:
        from leyline import query, store
        con = store.connect(db)
        assert query.overview(con)["repos"]
        con.close()
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_empty_and_languageless_repositories_map(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    write(tmp_path, "docs/README.md", "# hi\n")
    for name in ("empty", "docs"):
        assert cli.main(["map", str(tmp_path / name), "--exact", "off"]) == 0
        assert "No source files were found" in capsys.readouterr().out


def test_layout_of_a_huge_level_stays_bounded(monkeypatch):
    """Long edges get a waypoint per row they cross; on a level of thousands of boxes that was tens of millions of
    waypoints and the map page of a large repository ran out of memory. Past a budget they are left out."""
    from leyline import export
    monkeypatch.setattr(export, "WAYPOINTS", 1000)
    n = 3000
    pairs = sorted({(i, (i * 7 + 13) % n) for i in range(n)} | {(i, i + 1) for i in range(n - 1)})
    pairs = [p for p in pairs if p[0] != p[1]]
    began = time.monotonic()
    placed = export._layered(list(range(n)), pairs, lambda i: 120)
    assert set(placed) == set(range(n))
    assert time.monotonic() - began < 60


def test_a_full_disk_gets_a_message(capsys):
    assert "free disk space" in cli._store_problem(sqlite3.OperationalError("database or disk is full"))
    assert "another leyline run" in cli._store_problem(sqlite3.OperationalError("database is locked"))


def test_a_moved_or_copied_checkout_maps_its_own_code(tmp_path):
    """The store records where the code is relative to itself, so a checkout that is copied or renamed, with its
    .leyline/ inside, re-maps and reads the copy, not the folder it was first indexed in."""
    import shutil
    from leyline import store
    from leyline.indexer import index
    src = tmp_path / "first"
    shutil.copytree(Path(__file__).parent / "fixture2" / "py", src)
    index(src, src / ".leyline" / "leyline.db", "app")
    moved = tmp_path / "second"
    shutil.copytree(src, moved)
    (moved / "src" / "pkg" / "core.py").write_text((moved / "src" / "pkg" / "core.py").read_text() + "\n\ndef only_here():\n    return 1\n")
    con = store.connect(moved / ".leyline" / "leyline.db")
    assert store.roots(con) == {"app": moved.resolve()}
    con.close()
    index(moved, moved / ".leyline" / "leyline.db", "app")
    con = store.connect(moved / ".leyline" / "leyline.db")
    assert con.execute("SELECT COUNT(*) FROM nodes WHERE name = 'only_here'").fetchone()[0] == 1
    con.close()
    # A store from before relative paths were kept: it belongs to the folder its .leyline/ is in.
    old = tmp_path / "third"
    shutil.copytree(src, old)
    con = store.connect(old / ".leyline" / "leyline.db")
    con.execute("DELETE FROM meta WHERE key LIKE 'rel:%'")
    con.commit()
    assert store.roots(con) == {"app": old.resolve()}
    con.close()


def test_the_map_summary_names_files_left_out_and_a_compiler_check_together():
    """Both lines at once used to crash the summary: the count of files left out shadowed the plural helper."""
    from leyline import loop
    m = {"repos": ["r"], "modules": [], "seconds": 1, "files": 3, "lines": 10, "types": 1, "functions": 2, "tests": 0,
         "entry_points": 0, "left_out": {"too large": 2}, "patterns": {}, "db": "x",
         "exact": {"exact:roslyn": {"status": "ok", "calls_confirmed": 5}}}
    text = loop.map_text(m)
    assert "Not mapped: 2 files" in text and "5 calls confirmed" in text


def test_a_map_made_by_another_version_of_leyline_is_made_again(tmp_path):
    """plan, check and pr re-map only changed files; a map an older Leyline made is made again even with none."""
    from leyline import loop, store
    from leyline.indexer import index
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    return 1\n")
    db = tmp_path / "s.db"
    index(root, db, "r", "off")
    assert loop.refresh(db) is None                      # nothing changed, same Leyline
    con = store.connect(db)
    with con:
        con.execute("UPDATE meta SET value = 'older' WHERE key = 'made_by'")
    con.close()
    assert loop.refresh(db) is not None and not loop.made_by_another_version(db)
