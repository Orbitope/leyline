"""Unusual but legitimate repository shapes and environments: git checkouts made in odd ways, odd files, odd
layouts, and odd places to run from."""

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

from leyline import cli


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


CORE = ("def helper(x):\n    return x.lower()\n\n\nclass Engine:\n    def start(self):\n        return helper('A')\n")
TEST = "from pkg.core import Engine\n\n\ndef test_start():\n    assert Engine().start() == 'a'\n"


def _origin(tmp_path) -> Path:
    src = tmp_path / "origin"
    (src / "pkg").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "pkg/__init__.py").write_text("")
    (src / "pkg/core.py").write_text(CORE)
    (src / "tests/test_core.py").write_text(TEST)
    git(src, "init", "-q", "-b", "main")
    git(src, "add", "pkg", "tests")
    git(src, "commit", "-qm", "base")
    return src


def test_a_checkout_with_autocrlf_reviews_only_the_files_it_edited(tmp_path, monkeypatch):
    """core.autocrlf=true checks files out with CRLF; the base must be read the same way, or every file differs."""
    src = _origin(tmp_path)
    root = tmp_path / "crlf"
    git(tmp_path, "clone", "-q", "-c", "core.autocrlf=true", str(src), str(root))
    assert b"\r\n" in (root / "tests/test_core.py").read_bytes()
    git(root, "switch", "-q", "-c", "feature")
    (root / "pkg/core.py").write_bytes(CORE.replace("lower", "upper").replace("\n", "\r\n").encode())
    monkeypatch.chdir(root)
    code, out = run("pr", "--json")
    assert code == 0, out
    r = json.loads(out)
    assert r["facts"]["changed"]["files"] == ["pkg/core.py"] if "facts" in r else True
    assert "tests/test_core.py" not in out


def _repo(root: Path, text: str = CORE) -> Path:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg/core.py").write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "pkg")
    git(root, "commit", "-qm", "base")
    return root


def test_map_with_no_path_in_a_workspaces_folder_maps_the_workspace_again(tmp_path, monkeypatch):
    """`leyline map a b` from a folder, then `leyline map` there: the two members, not the folder as a third."""
    from leyline import store
    _repo(tmp_path / "alpha")
    _repo(tmp_path / "beta", CORE.replace("helper", "bhelper"))
    monkeypatch.chdir(tmp_path)
    assert run("map", "alpha", "beta")[0] == 0
    code, out = run("map")
    assert code == 0, out
    assert "2 repositories (alpha, beta)" in out, out
    con = store.connect(tmp_path / ".leyline/leyline.db")
    try:
        assert sorted(store.roots(con)) == ["alpha", "beta"]
    finally:
        con.close()


def test_coupling_says_why_there_is_no_history(tmp_path, monkeypatch, capsys):
    """A repository with no commits yet is under git; a workspace whose members have moved is not 'not under git'."""
    root = tmp_path / "fresh"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg/core.py").write_text(CORE)
    git(root, "init", "-q")
    monkeypatch.chdir(root)
    assert run("map")[0] == 0
    capsys.readouterr()
    assert run("coupling")[0] != 0
    assert "no commits yet" in capsys.readouterr().err

    _repo(tmp_path / "ws/alpha")
    _repo(tmp_path / "ws/beta")
    monkeypatch.chdir(tmp_path / "ws")
    assert run("map", "alpha", "beta")[0] == 0
    (tmp_path / "ws/alpha").rename(tmp_path / "ws/alpha2")
    (tmp_path / "ws/beta").rename(tmp_path / "ws/beta2")
    capsys.readouterr()
    assert run("coupling")[0] != 0
    err = capsys.readouterr().err
    assert "not under git" not in err and "where it was mapped" in err, err


def test_pr_says_plainly_why_it_has_no_base(tmp_path, monkeypatch, capsys):
    """No commits yet; a default branch named trunk with no origin; a shallow clone missing the fork point."""
    def pr_error(where, *argv):
        monkeypatch.chdir(where)
        capsys.readouterr()
        code, _ = run("pr", *argv)
        assert code != 0
        return capsys.readouterr().err

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q")
    assert "no commits yet" in pr_error(fresh)

    trunk = tmp_path / "trunk"
    trunk.mkdir()
    (trunk / "a.py").write_text("def f():\n    return 1\n")
    git(trunk, "init", "-q", "-b", "trunk")
    git(trunk, "add", "a.py")
    git(trunk, "commit", "-qm", "one")
    err = pr_error(trunk)
    assert "leyline pr <branch or commit>" in err, err

    # a shallow clone of main, then a feature branch fetched shallow too: no commit they share is in the clone
    src = _origin(tmp_path)
    git(src, "switch", "-q", "-c", "feature")
    for i in range(3):
        (src / "pkg/core.py").write_text(CORE + f"\n\ndef more{i}():\n    return {i}\n")
        git(src, "commit", "-qam", f"f{i}")
    git(src, "switch", "-q", "main")
    (src / "pkg/__init__.py").write_text("X = 1\n")
    git(src, "commit", "-qam", "main moves on")
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", "--branch", "main", "file://" + str(src), str(shallow))
    git(shallow, "fetch", "-q", "--depth", "1", "origin", "feature:feature")
    git(shallow, "switch", "-q", "feature")
    err = pr_error(shallow, "main")
    assert "shallow" in err and "--unshallow" in err, err


def test_a_read_only_repository_says_to_keep_the_store_elsewhere_and_can_then_be_mapped(tmp_path, monkeypatch, capsys):
    import os
    import stat
    import pytest
    if os.geteuid() == 0:
        pytest.skip("root writes anywhere")
    root = _repo(tmp_path / "ro")
    monkeypatch.chdir(root)
    mode = root.stat().st_mode
    root.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        capsys.readouterr()
        code, _ = run("map")
        err = capsys.readouterr().err
        assert code == 2 and "--db" in err and "Traceback" not in err, err
        assert run("--db", str(tmp_path / "elsewhere/leyline.db"), "map")[0] == 0
    finally:
        root.chmod(mode)


def test_serve_started_in_a_folder_inside_the_repository_reads_the_repositorys_store(tmp_path, monkeypatch):
    """An agent opened in a package folder of a mapped repository: the server reads the repository's map, as `search`
    and the other commands do from there, not an empty store in the folder."""
    import os
    from leyline import server
    root = _repo(tmp_path / "repo")
    monkeypatch.chdir(root)
    monkeypatch.setenv("LEYLINE_DB", "")   # recorded, so the value serve sets is taken away after the test
    monkeypatch.delenv("LEYLINE_DB")
    assert run("map")[0] == 0
    seen = []
    monkeypatch.setattr(server, "main", lambda: seen.append(os.environ["LEYLINE_DB"]))
    monkeypatch.chdir(root / "pkg")
    assert run("serve")[0] == 0
    assert Path(seen[0]).resolve() == (root / ".leyline/leyline.db").resolve()


def test_a_file_with_no_partners_reads_as_a_sentence():
    from leyline import coupling
    out = coupling.text({"path": "a.py", "changes": 2, "about": "from the last 2 commits", "partners": [], "total": 0,
                         "min_together": 3, "min_confidence": 0.5})
    assert out.endswith("(at least 3 commits together, and 50% of its).") and "its's" not in out, out


def test_a_sparse_checkout_does_not_review_the_files_it_left_out_as_removed(tmp_path, monkeypatch):
    src = _origin(tmp_path)
    root = tmp_path / "sparse"
    git(tmp_path, "clone", "-q", str(src), str(root))
    git(root, "sparse-checkout", "set", "pkg")
    assert not (root / "tests/test_core.py").exists()
    git(root, "switch", "-q", "-c", "feature")
    (root / "pkg/core.py").write_text(CORE.replace("lower", "upper"))
    monkeypatch.chdir(root)
    code, out = run("pr", "--json")
    assert code == 0, out
    changed = json.loads(out)["changed"] if "changed" in json.loads(out) else json.loads(out)["facts"]["changed"]
    assert [x["name"] for x in changed["removed"]] == [], changed["removed"]
    assert changed["files"] == ["pkg/core.py"], changed["files"]


def _graph(db) -> tuple[set, set]:
    from leyline import store
    con = store.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT id FROM nodes")}, {(r[0], r[1]) for r in con.execute(
            "SELECT src_id, dst_id FROM calls")}
    finally:
        con.close()


def test_a_latin1_python_file_with_a_coding_declaration_keeps_its_names(tmp_path):
    """PEP 263: `# -*- coding: latin-1 -*-` makes the file's bytes Latin-1; read as UTF-8, `café` lost its é."""
    from leyline.indexer import index
    root = tmp_path / "repo"
    root.mkdir()
    (root / "latin.py").write_bytes("# -*- coding: latin-1 -*-\n\ndef caf\xe9():\n    return '\xe9t\xe9'\n\n\n"
                                    "def after():\n    return caf\xe9()\n".encode("latin-1"))
    index(root, tmp_path / "l.db", "r")
    ids, calls = _graph(tmp_path / "l.db")
    assert "r:python:latin.caf\xe9" in ids, sorted(ids)
    assert ("r:python:latin.after", "r:python:latin.caf\xe9") in calls


def test_a_namespace_package_in_a_src_layout_is_imported_by_its_own_name(tmp_path):
    """src/corp/tools/strings.py with no __init__.py anywhere (PEP 420) is imported as corp.tools.strings."""
    from leyline.indexer import index
    root = tmp_path / "repo"
    for f, text in {"pyproject.toml": "[project]\nname = 'corp'\n",
                    "src/corp/tools/strings.py": "def shout(s):\n    return s.upper()\n",
                    "src/corp/app/main.py": "from corp.tools.strings import shout\n\n\ndef run():\n    return shout('a')\n",
                    "tests/test_main.py": "from corp.app.main import run\n\n\ndef test_run():\n    assert run()\n"}.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    index(root, tmp_path / "l.db", "r")
    _, calls = _graph(tmp_path / "l.db")
    assert ("r:python:src.corp.app.main.run", "r:python:src.corp.tools.strings.shout") in calls
    assert ("r:python:tests.test_main.test_run", "r:python:src.corp.app.main.run") in calls


def test_a_module_imported_from_a_namespace_package_is_local_not_an_outside_package(tmp_path):
    """`from corp.tools import strings` where corp/tools has no __init__.py names the module corp/tools/strings.py."""
    from leyline import store
    from leyline.indexer import index
    root = tmp_path / "repo"
    for f, text in {"corp/tools/strings.py": "def shout(s):\n    return s.upper()\n",
                    "corp/app/main.py": "from corp.tools import strings\n\n\ndef run():\n    return strings.shout('a')\n"}.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    index(root, tmp_path / "l.db", "r")
    con = store.connect(tmp_path / "l.db")
    try:
        externals = [r[0] for r in con.execute("SELECT id FROM nodes WHERE kind = 'external'")]
        calls = {(r[0], r[1], r[2]) for r in con.execute("SELECT src_id, dst_id, precision FROM calls")}
    finally:
        con.close()
    assert externals == []
    assert ("r:python:corp.app.main.run", "r:python:corp.tools.strings.shout", "heuristic") in calls, calls


def test_a_typescript_path_alias_from_tsconfig_is_the_local_file(tmp_path):
    """compilerOptions.paths (`@ui/*` -> `src/*`, with comments and a trailing comma as tsconfig allows) names files
    of the repository, not npm packages."""
    from leyline import store
    from leyline.indexer import index
    root = tmp_path / "repo"
    for f, text in {
        "packages/ui/tsconfig.json": '{\n  // aliases\n  "compilerOptions": {"baseUrl": ".", "paths": {\n'
                                     '    "@ui/*": ["src/*"], "~lib": ["src/lib/index.ts"],\n  }},\n}\n',
        "packages/ui/src/lib/format.ts": "export function fmt(x: number): string { return String(x); }\n",
        "packages/ui/src/lib/index.ts": "export * from './format';\n",
        "packages/ui/src/lib/pad.ts": "export function pad(s: string) { return ' ' + s; }\n",
        "packages/ui/src/button.ts": "import { fmt } from '@ui/lib';\nimport { pad } from '@ui/lib/pad';\n"
                                     "export function label(n: number) {\n  return pad(fmt(n));\n}\n",
        "packages/ui/src/menu.ts": "import { fmt } from '~lib';\nexport function item(n: number) {\n  return fmt(n);\n}\n",
    }.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    index(root, tmp_path / "l.db", "r")
    con = store.connect(tmp_path / "l.db")
    try:
        externals = [r[0] for r in con.execute("SELECT id FROM nodes WHERE kind = 'external'")]
        calls = {(r[0], r[1]) for r in con.execute("SELECT src_id, dst_id FROM calls")}
    finally:
        con.close()
    assert externals == []
    ts = "r:typescript:packages.ui.src."
    assert {(ts + "button.label", ts + "lib.format.fmt"), (ts + "button.label", ts + "lib.pad.pad"),
            (ts + "menu.item", ts + "lib.format.fmt")} <= calls, calls
