"""Re-indexing after an edit takes the incremental path, and the store it leaves is the one a full run of the same
tree writes. Each fixture gets the same kinds of edit, one after another: a function body changed, a function
added, one deleted, a type renamed, a file added and one removed, an import changed."""

import shutil
import sqlite3
from pathlib import Path

import pytest

from leyline.incremental import cache_path
from leyline.indexer import index
from store_identity import differences

HERE = Path(__file__).parent


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1))


def rename(paths, old: str, new: str) -> None:
    import re
    for p in paths:
        p.write_text(re.sub(rf"\b{old}\b", new, p.read_text()))


def check(tmp_path: Path, roots, repo_id, edits) -> list[dict]:
    """Index once in full, then after each edit: incrementally into the same store, and in full into a new one."""
    inc = tmp_path / "inc.db"
    first = index(roots, inc, repo_id)
    assert first["incremental"]["mode"] == "full"
    runs = []
    for k, (name, fn) in enumerate(edits):
        fn()
        stats = index(roots, inc, repo_id)
        assert stats["incremental"]["mode"] == "incremental", (name, stats["incremental"])
        full = tmp_path / f"full{k}.db"
        index(roots, full, repo_id, full=True)
        assert differences(inc, full) == {}, name
        runs.append(stats["incremental"])
    return runs


def copy(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    shutil.copytree(HERE / name, root)
    return root


def test_python_and_csharp(tmp_path):
    root = copy(tmp_path, "fixture2")
    core, app = root / "py/src/pkg/core.py", root / "py/web/app.py"
    runs = check(tmp_path, root, "f2", [
        ("body", lambda: edit(core, "    def start(self):\n", "    def start(self):\n        make_engine()\n")),
        ("local", lambda: edit(core, "    def count(self):\n", "    def count(self):\n        n = 1\n")),
        ("add function", lambda: edit(core, "def poke(thing):", "def added():\n    return make_engine().start()\n\n\ndef poke(thing):")),
        ("delete function", lambda: edit(core, "def poke(thing):\n    handler = thing.child\n    return thing.child()\n", "")),
        ("rename type", lambda: rename([core], "Journal", "Ledger")),
        ("add file", lambda: (root / "py/src/pkg/extra.py").write_text(
            "from .core import make_engine\n\n\ndef extra():\n    return make_engine().child()\n")),
        ("remove file", lambda: (root / "py/web/client.py").unlink()),
        ("change import", lambda: edit(app, "import os\n", "import os\nfrom pkg.core import added\n")),
        ("csharp body", lambda: edit(root / "cs/Mod/State.cs", "public class Journal", "public class Journal2")),
    ])
    assert runs[0]["files_resolved"] < 10 and runs[0]["files_parsed"] == 1


def test_csharp_projects(tmp_path):
    root = copy(tmp_path, "fixture")
    shapes, prog = root / "Lib/Shapes.cs", root / "App/Program.cs"
    check(tmp_path, root, "fx", [
        ("body", lambda: edit(shapes, "double t = 0;", "double t = 0; Add(null);")),
        ("add method", lambda: edit(shapes, "public void Add(IShape s)", "public int Count() => _shapes.Count;\n        public void Add(IShape s)")),
        ("delete method", lambda: edit(shapes, "public double Total(double scale) => Total() * scale;", "")),
        ("rename type", lambda: rename([shapes, prog], "Circle", "Disk")),
        ("add file", lambda: (root / "Lib/Extra.cs").write_text(
            "namespace Lib\n{\n    public class Extra\n    {\n        public double Run() => new Canvas().Total();\n    }\n}\n")),
        ("remove file", lambda: (root / "Lib/Bus.cs").unlink()),
        ("change using", lambda: edit(prog, "using System.IO;\n", "")),
    ])


def test_typescript_packages(tmp_path):
    root = copy(tmp_path, "fixture3")
    ids, index_ts, graph = (root / "pkg/core/src" / f for f in ("ids.ts", "index.ts", "graph.ts"))
    check(tmp_path, root, "f3", [
        ("body", lambda: edit(ids, "  counter += 1;\n", "  counter += 1;\n  store.save(prefix, prefix);\n")),
        ("add function", lambda: edit(ids, "export const store", "export function plain(x: string): string {\n  return makeId(x);\n}\n\nexport const store")),
        ("delete method", lambda: edit(graph, "  touch(): void {}\n", "")),
        ("rename type", lambda: rename([graph, index_ts, root / "pkg/app/src/main.tsx"], "Graph", "Board")),
        ("add file", lambda: (root / "pkg/core/src/extra.ts").write_text(
            'import { plain } from "./ids.js";\n\nexport function extra(): string {\n  return plain("x");\n}\n')),
        ("change import", lambda: edit(index_ts, 'export * from "./graph.js";\n', 'export * from "./graph.js";\nexport * from "./extra.js";\n')),
        ("remove file", lambda: (root / "pkg/app/src/server.ts").unlink()),
    ])


def test_go_java_rust(tmp_path):
    root = copy(tmp_path, "fixture4")
    counter, main_go, shape = root / "java/com/acme/Counter.java", root / "go/main.go", root / "go/shapes/shape.go"
    check(tmp_path, root, "f4", [
        ("body", lambda: edit(counter, "public void add(int n) { count += n; }", "public void add(int n) { count += n; get(); }")),
        ("add method", lambda: edit(counter, "public int get()", "public int twice() { return get() * 2; }\n    public int get()")),
        ("delete method", lambda: edit(counter, "    public Counter plus(int n) { add(n); return this; }\n", "")),
        ("rename type", lambda: rename([main_go], "Options", "Settings")),
        ("add file", lambda: (root / "go/shapes/extra.go").write_text(
            "package shapes\n\nfunc Double(s *Square) *Square { return Scale(s, 2) }\n")),
        ("remove file", lambda: (root / "rs/src/geo.rs").unlink()),
        ("change import", lambda: edit(main_go, '\t"fmt"\n', "")),
        ("go body", lambda: edit(shape, "return NewSquare(s.Side * f)", "return Double(NewSquare(s.Side * f))")),
    ])


def test_workspace(tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(HERE / "fixture_ws", ws)
    lib, app = ws / "wslib/src/libpkg/core.py", ws / "wsapp/src/apppkg/main.py"
    check(tmp_path, [ws / "wsapp", ws / "wslib"], None, [
        ("body", lambda: edit(lib, "    return x * 2\n", "    print(x)\n    return x * 2\n")),
        ("add function", lambda: edit(lib, "def helper(x):", "def triple(x):\n    return helper(x) * 3\n\n\ndef helper(x):")),
        ("rename type", lambda: rename([lib, app], "Base", "Root")),
        ("change import", lambda: edit(app, "from libpkg import helper\n", "from libpkg import helper\nfrom libpkg.core import triple\n")),
    ])


def test_full_when_asked_or_when_the_cache_is_not_the_stores(tmp_path):
    root = copy(tmp_path, "fixture4")
    db = tmp_path / "s.db"
    index(root, db, "f4")
    assert index(root, db, "f4")["incremental"]["mode"] == "incremental"
    assert index(root, db, "f4", full=True)["incremental"]["mode"] == "full"
    # a store written by something else (here: its token changed) is not trusted to match the cache
    con = sqlite3.connect(str(db))
    con.execute("UPDATE meta SET value = 'other' WHERE key = 'generation'")
    con.commit()
    con.close()
    assert index(root, db, "f4")["incremental"]["mode"] == "full"
    cache_path(db).unlink()
    assert index(root, db, "f4")["incremental"]["mode"] == "full"
    assert index(root, db, "f4")["incremental"]["mode"] == "incremental"


def test_verify_mode(tmp_path, monkeypatch):
    """LEYLINE_VERIFY=1 follows an incremental run with a full one and reports any row that differs."""
    from leyline import incremental
    root = copy(tmp_path, "fixture4")
    db = tmp_path / "v.db"
    index(root, db, "f4")
    edit(root / "java/com/acme/Counter.java", "public int get()", "public int twice() { return get() * 2; }\n    public int get()")
    monkeypatch.setenv("LEYLINE_VERIFY", "1")
    stats = index(root, db, "f4")
    assert stats["incremental"]["mode"] == "incremental" and stats["incremental"]["differs"] == {}
    con = sqlite3.connect(str(db))
    con.execute("UPDATE call_sites SET site_start = site_start + 1 WHERE rowid = (SELECT MIN(rowid) FROM call_sites)")
    con.commit()
    con.close()
    other = tmp_path / "w.db"
    index(root, other, "f4", full=True)
    assert list(incremental.differences(db, other)) == ["calls"]


def test_values_passed_in(tmp_path):
    """The Python rule that types a value by what callers pass reads calls from every file. An edit in one file can
    change what it finds in another that did not change, and the incremental run must find the same."""
    root = copy(tmp_path, "fixture6")
    app, extra = root / "web/app.py", root / "web/extra.py"
    check(tmp_path, root, "f6", [
        # a new caller elsewhere passes a Middleware to run_app: run_app's app(...) now reaches it too
        ("new caller in another file", lambda: extra.write_text(
            "from .app import run_app, Middleware\n\n\ndef other():\n    return run_app(Middleware(None), {})\n")),
        ("caller removed", lambda: edit(app, '    return run_app(App("plain"), {})\n', "    return None\n")),
        ("unrelated body", lambda: edit(app, "    def wsgi(self, environ, start):\n", "    def wsgi(self, environ, start):\n        n = 1\n")),
    ])
    con = sqlite3.connect(tmp_path / "inc.db")
    got = {r[0].split(":", 2)[2] for r in con.execute("SELECT dst_id FROM calls WHERE src_id = 'f6:python:web.app.run_app'")}
    assert "web.app.Middleware.__call__" in got


def test_constructor_added_or_removed(tmp_path):
    """A call that makes an object (`pkg.Engine(...)`, `new Shape()`) is written with the class's name and links to
    its constructor, inherited or not. Adding or removing a constructor must resolve again the files that make one,
    though they never mention `__init__` or `constructor`."""
    root = tmp_path / "ctor"
    (root / "pkg").mkdir(parents=True)
    (root / "ts").mkdir()
    core = root / "pkg/core.py"
    (root / "pkg/__init__.py").write_text("from .core import Engine, Sub\n")
    core.write_text("class Engine:\n    def __init__(self, name):\n        self.name = name\n\n\nclass Sub(Engine):\n    pass\n")
    (root / "use.py").write_text("import pkg\n\n\ndef make():\n    return pkg.Engine('x')\n\n\ndef sub():\n    return pkg.Sub('y')\n")
    shape = root / "ts/shape.ts"
    shape.write_text("export class Shape {\n  constructor(public n: number) {}\n}\n")
    (root / "ts/main.ts").write_text('import { Shape } from "./shape";\n\nexport function make(): Shape {\n  return new Shape(1);\n}\n')
    check(tmp_path, root, "c", [
        ("python constructor removed", lambda: edit(core, "    def __init__(self, name):\n", "    def setup(self, name):\n")),
        ("python constructor added", lambda: edit(core, "    def setup(self, name):\n", "    def __init__(self, name):\n")),
        ("typescript constructor removed", lambda: edit(shape, "  constructor(public n: number) {}\n", "  n = 1;\n")),
        ("typescript constructor added", lambda: edit(shape, "  n = 1;\n", "  constructor(public n: number) {}\n")),
    ])


def test_tour_with_tied_modules(tmp_path):
    """The tour's library stop picks the module most used by others; between modules used as much, the choice must
    not depend on the order their rows were written in (a module added by an incremental run is written last)."""
    root = tmp_path / "tie"
    (root / "b").mkdir(parents=True)
    (root / "b/one.py").write_text("def one():\n    return 1\n")
    check(tmp_path, root, "t", [
        ("module added", lambda: ((root / "a").mkdir(), (root / "a/two.py").write_text("def two():\n    return 2\n"))),
    ])


def test_counts_written_in_one_order(tmp_path):
    """An incremental run adds up the counts of the files it did not resolve again in another order than a full run;
    the coverage rows must still read the same."""
    root = copy(tmp_path, "fixture")
    check(tmp_path, root, "fx", [("remove file", lambda: (root / "scripts/run.py").unlink())])


def test_module_variable_type_changed(tmp_path):
    """A call on a module-level variable (`current.render()`, with `current: App = App()` in another file) takes the
    variable's declared type. Removing the variable, or changing its type, must resolve its users again."""
    root = copy(tmp_path, "fixture6")
    glob = root / "web/globals.py"
    check(tmp_path, root, "f6", [
        ("variable removed", lambda: edit(glob, 'current: App = App("current")\n', "")),
        ("variable added", lambda: glob.write_text(glob.read_text() + 'current: App = App("current")\n')),
    ])


def test_waits_for_another_run_writing_the_cache(tmp_path):
    """A second run that starts while another is writing the cache (a map while a plan starts) waits for it, as it
    waits for the store, rather than failing at once with "database is locked"."""
    import threading
    root = tmp_path / "w"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    db = tmp_path / "w.db"
    index(root, db, "w")
    (root / "a.py").write_text("def a():\n    return 2\n")
    other = sqlite3.connect(str(cache_path(db)), check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")   # as a run holds it from its first parse until it finishes
    done = threading.Timer(1.5, other.rollback)
    done.start()
    try:
        assert index(root, db, "w")["incremental"]["mode"] in ("full", "incremental")
    finally:
        done.join()
        other.close()


def test_tour_goes_when_a_repository_has_no_modules_left(tmp_path):
    """A repository whose last source file goes has no modules and no tour: the tour of the earlier map, which names
    a module that is gone, must not be left behind."""
    root = tmp_path / "e"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    db = tmp_path / "e.db"
    index(root, db, "e")
    (root / "a.py").unlink()
    index(root, db, "e")
    full = tmp_path / "full.db"
    index(root, full, "e", full=True)
    assert differences(db, full) == {}


def test_file_differing_only_in_extension_added_and_removed(tmp_path):
    """Whether a file's ids keep its extension depends on the files beside it (a.js beside a.ts): adding or removing
    one changes the other's ids though its content is the same, so its kept parse output must not be used."""
    root = tmp_path / "x"
    (root / "src").mkdir(parents=True)
    ts, js = root / "src/a.ts", root / "src/a.js"
    ts.write_text("export function one(): number {\n  return 1;\n}\n")
    (root / "src/main.ts").write_text('import { one } from "./a";\n\nexport function main(): number {\n  return one();\n}\n')
    check(tmp_path, root, "x", [
        ("js added beside", lambda: js.write_text("export function two() {\n  return 2;\n}\n")),
        ("js removed", lambda: js.unlink()),
    ])


def test_start_that_is_both_an_entry_and_a_test(tmp_path):
    """Both flows of a start that is an entry and a test are walked again, kept and dropped as a full run would."""
    root = tmp_path / "b"
    root.mkdir()
    p = root / "P.cs"
    p.write_text("public static class P\n{\n    [Fact]\n    public static void Main() { Run(); }\n"
                 "    static void Run() { }\n    static void Step() { }\n}\n")
    check(tmp_path, root, "b", [
        ("callee body", lambda: edit(p, "static void Run() { }", "static void Run() { Step(); }")),
        ("no longer a test", lambda: edit(p, "    [Fact]\n", "")),
        ("a test again", lambda: edit(p, "    public static void Main()", "    [Fact]\n    public static void Main()")),
    ])


def _holding_run(tmp_path):
    """A repository mapped once, and a second run of it that has started and holds the map (as one does from its
    first parse until it finishes)."""
    from leyline import incremental, store
    from leyline.indexer import Indexer
    root = tmp_path / "w"
    root.mkdir()
    (root / "a.py").write_text("def a():\n    return 1\n")
    db = tmp_path / "w.db"
    index(root, db, "w")
    (root / "a.py").write_text("def a():\n    return 2\n")
    con = store.connect(db)
    holder = incremental.Run(con, db, Indexer(root, "w"))
    return root, db, con, holder


def test_a_run_waits_for_a_long_run_to_finish(tmp_path, monkeypatch):
    """A run that finds another one in progress waits for it to finish, however long it takes (here longer than
    the time SQLite is told to wait for a lock), then maps as usual."""
    import threading
    from leyline import store
    monkeypatch.setattr(store, "BUSY_SECONDS", 0.5)
    root, db, con, holder = _holding_run(tmp_path)

    got = {}

    def second():
        try:
            got["mode"] = index(root, db, "w")["incremental"]["mode"]
        except Exception as exc:
            got["error"] = exc
    t = threading.Thread(target=second)
    t.start()
    t.join(2.0)   # still waiting, past the lock's own time limit
    assert t.is_alive() and not got
    holder.abandon()
    con.close()
    t.join(60)
    assert got.get("mode") in ("full", "incremental"), got


def test_a_run_that_waits_too_long_says_who_holds_the_map(tmp_path, monkeypatch):
    """LEYLINE_WAIT limits the wait; past it the run stops with a message naming the other run and how long it has
    been going, not with "database is locked"."""
    import os
    root, db, con, holder = _holding_run(tmp_path)
    monkeypatch.setenv("LEYLINE_WAIT", "1")
    try:
        with pytest.raises(RuntimeError) as err:
            index(root, db, "w")
        assert "another leyline index" in str(err.value) and f"pid {os.getpid()}" in str(err.value)
    finally:
        holder.abandon()
        con.close()
    assert index(root, db, "w")["incremental"]["mode"] in ("full", "incremental")


def test_a_map_that_dies_after_writing_facts_is_mapped_again(tmp_path, monkeypatch):
    """A run killed after its facts are written but before the tour, patterns and stale marks are: the next `plan` or
    `check` must not take the store as up to date because no file changed since."""
    from leyline import loop, tours
    root = copy(tmp_path, "fixture2")
    db = tmp_path / "s.db"
    index(root, db, "f2")
    edit(root / "py/src/pkg/core.py", "    def start(self):\n", "    def start(self):\n        make_engine()\n")

    def dies(*a, **k):
        raise KeyboardInterrupt
    with monkeypatch.context() as m:
        m.setattr(tours, "generate", dies)
        with pytest.raises(KeyboardInterrupt):
            index(root, db, "f2")
    assert loop.refresh(db) is not None   # mapped again
    full = tmp_path / "full.db"
    index(root, full, "f2", "auto", full=True)   # as refresh maps a store `map` did not note the mode of
    assert differences(db, full) == {}


def test_a_damaged_cache_makes_a_full_run_not_a_failed_one(tmp_path):
    """The cache only saves work: deleting it makes the next run a full one. A damaged one (a disk error, a copy cut
    off) stopped every index with "the store is damaged ... delete it", which would lose the annotations, findings
    and test runs kept in a store that was fine."""
    root = copy(tmp_path, "fixture2")
    db = tmp_path / "s.db"
    index(root, db, "f2")
    cache = cache_path(db)
    for p in (Path(str(cache) + "-wal"), Path(str(cache) + "-shm")):
        p.unlink(missing_ok=True)
    cache.write_bytes(b"this is not a database, it was cut off" * 10)
    stats = index(root, db, "f2")
    assert stats["incremental"]["mode"] == "full"
    assert index(root, db, "f2")["incremental"]["mode"] == "incremental"   # and the cache is good again
