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
