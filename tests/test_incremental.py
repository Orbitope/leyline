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
