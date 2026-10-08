"""`leyline quick`: a small change with no spec folder. Before: what it touches, what must change with it, the tests
that run it. After: one verdict from the code and the test results, and the way into a spec when it grew."""

import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from leyline import cli, diff, quick, store

FILES = {
    "app/__init__.py": "",
    "app/net.py": "RETRIES = 5\n\n\ndef fetch(url, tries=RETRIES):\n    for _ in range(tries):\n        pass\n    return url\n\n\n"
                  "def get(url):\n    return fetch(url)\n\n\ndef head(url):\n    return fetch(url)[:4]\n",
    "app/store.py": "def load(path):\n    with open(path) as f:\n        return f.read()\n\n\n"
                    "def helper(x):\n    return x * 2\n",
    "app/use.py": "from app.store import load, helper\n\n\ndef first(p):\n    return load(p).split(\"\\n\")[0]\n\n\n"
                  "def count(p):\n    return len(load(p))\n\n\ndef double(x):\n    return helper(x)\n",
    "app/other.py": "def unrelated():\n    return 1\n",
    "tests/test_net.py": "from app.net import get\n\n\ndef test_get():\n    assert get(\"x\") == \"x\"\n",
    "tests/test_use.py": "from app.use import count\n\n\ndef test_count(tmp_path):\n    p = tmp_path / \"x\"\n"
                         "    p.write_text(\"ab\")\n    assert count(str(p)) == 2\n",
}
PASSING = "PASS test_get\nPASS test_count\n"


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    for f, text in FILES.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    monkeypatch.chdir(root)
    run("map", ".")
    return root


def run(*argv, stdin: str = None, monkeypatch=None) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    if stdin is not None:
        import sys
        old = sys.stdin
        sys.stdin = io.StringIO(stdin)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
    finally:
        if stdin is not None:
            sys.stdin = old
    return code, out.getvalue() + err.getvalue()


def edit(root: Path, path: str, old: str, new: str) -> None:
    f = root / path
    assert old in f.read_text()
    f.write_text(f.read_text().replace(old, new))


def test_a_constant_and_its_caller(repo):
    """The one-line fix the spec route is too heavy for: change a constant, see what reads it, check it was done."""
    code, page = run("quick", "make the retry count 3", "--about", "RETRIES", "--tests", "-", stdin=PASSING)
    assert code == 0, page
    assert "Will touch: the value `RETRIES` (app/net.py, line 1), read by `fetch`." in page
    assert "Must edit with it: nothing" in page and "Channels: none touched." in page
    assert "Tests that run it:" in page and "test_get" in page
    assert "Recorded the tests as they are now: 2 pass, 0 fail." in page
    assert "leyline quick --done quick-make-the-retry-count-3 --tests -" in page
    assert 10 <= len(page.strip().splitlines()) <= 20
    row = store.connect(repo / ".leyline/leyline.db").execute(
        "SELECT attrs FROM change_proposals WHERE id = 'quick-make-the-retry-count-3'").fetchone()
    assert json.loads(row[0])["kind"] == "quick"

    edit(repo, "app/net.py", "RETRIES = 5", "RETRIES = 3")
    code, page = run("quick", "--done", "quick-make-the-retry-count-3", "--tests", "-", stdin=PASSING)
    assert code == 0, page
    assert "**Done.**" in page
    assert "Edits stayed in the named code: proven" in page and "A test ran the change: proven" in page
    assert "`test_get` passed" in page and "grown past" not in page


def test_an_edit_outside_the_named_code_is_partial_until_named(repo):
    run("quick", "make the retry count 3", "--about", "RETRIES", "--tests", "-", stdin=PASSING)
    edit(repo, "app/net.py", "RETRIES = 5", "RETRIES = 3")
    edit(repo, "app/net.py", "    return fetch(url)[:4]", "    return fetch(url)[:5]")
    code, page = run("quick", "--done", "quick-make-the-retry-count-3", "--tests", "-", stdin=PASSING)
    assert code == 1
    assert "**Not done**" in page and "Edits stayed in the named code: partial" in page
    assert "- `head` (edited, app/net.py)" in page
    assert "--about head" in page                    # how to name it, keeping the baseline
    code, page = run("quick", "--done", "quick-make-the-retry-count-3", "--about", "head", "--tests", "-", stdin=PASSING)
    assert code == 0 and "**Done.**" in page, page


def test_a_signature_change_that_leaves_a_caller_broken(repo):
    """Backticks in the sentence name the code; a caller left as it was, and a test that broke, are contradicted, and
    a change that left a caller says to write a spec."""
    code, page = run("quick", "read `load` files with an encoding", "--tests", "-", stdin=PASSING)
    assert code == 0, page
    assert "Will touch: `load`" in page and "`first`" in page and "`count`" in page
    cid = "quick-read-load-files-with-an-encoding"
    edit(repo, "app/store.py", "def load(path):\n    with open(path) as f:",
         "def load(path, encoding):\n    with open(path, encoding=encoding) as f:")
    edit(repo, "app/use.py", "return load(p).split", "return load(p, \"utf-8\").split")
    code, page = run("quick", "--done", cid, "--tests", "-", stdin="PASS test_get\nFAIL test_count: TypeError\n")
    assert code == 1
    assert "No caller left broken: contradicted" in page and "- `count`" in page and "`first`" not in page.split("Callers left")[1]
    assert "No test broke: contradicted" in page and "Tests that broke: `test_count`" in page
    assert "Callers edited to match the named code: `first`" in page and "Edits outside" not in page
    assert "grown past a quick change" in page and "leyline quick --to-spec <id> " + cid in page

    # it grew: hand it to a spec, which then keeps the quick change's baseline and test run
    code, out = run("quick", "--to-spec", "read-with-encoding", cid)
    assert code == 0, out
    con = store.connect(repo / ".leyline/leyline.db")
    assert diff.snapshot_path(con, "spec-read-with-encoding").exists()
    assert con.execute("SELECT COUNT(*) FROM test_results WHERE run = 'before:spec-read-with-encoding'").fetchone()[0] == 2
    code, out = run("quick", "--to-spec", "read-with-encoding", cid)
    assert code == 1 and "already has a baseline" in out
    ch = repo / "openspec/changes/read-with-encoding"
    (ch / "specs/store").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Read with an encoding\n\n## Why\nFiles are not all UTF-8.\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change the signature of `load` to take an encoding\n"
                                 "- [ ] 1.2 Change `first` and `count` to pass utf-8\n")
    (ch / "specs/store/spec.md").write_text("## ADDED Requirements\n### Requirement: Encoding\nIt SHALL.\n\n"
                                            "#### Scenario: Count\n- **WHEN** x\n- **THEN** y\n")
    code, out = run("plan", "read-with-encoding")
    assert "still compares with the code as it was" in out, out
    edit(repo, "app/use.py", "return len(load(p))", "return len(load(p, \"utf-8\"))")
    code, out = run("check", "read-with-encoding", "--tests", "-", stdin=PASSING)
    assert "| 1.1 " in out and "| 1.2 " in out and "not done" not in out.split("## 4.")[1].split("| Scenario")[0], out


def test_adding_a_parameter_is_a_change_of_signature(repo):
    """"add a parameter to `load`" was read as adding code, so the callers that must change with it were not named."""
    assert quick._action("add a parameter `encoding` to `load`") == "signature"
    code, page = run("quick", "add a parameter `encoding` to `load`", "--tests", "-", stdin=PASSING)
    must = next(ln for ln in page.splitlines() if ln.startswith("Must edit with it"))
    assert "first` (its call must change)" in must and "count` (its call must change)" in must, page


def test_the_review_steps_take_a_quick_id(repo):
    run("quick", "make the retry count 3", "--about", "RETRIES", "fetch")
    edit(repo, "app/net.py", "RETRIES = 5", "RETRIES = 3")
    cid = "quick-make-the-retry-count-3"
    code, out = run("spec", "facts", cid, "--reviewer", "logic")
    assert code == 0, out
    facts = json.loads(out)
    assert facts["change_id"] == cid and "fetch" in facts["named_code"] and "RETRIES" in facts["named_code"]
    code, out = run("spec", "finding", cid, "--reviewer", "logic", "--severity", "low", "--claim", "three may be too few",
                    "--evidence", "repo:python:app.net.fetch")
    assert code == 0 and json.loads(out)["status"] == "open"
    code, out = run("spec", "findings", cid)
    assert "three may be too few" in out
    code, out = run("quick", "--done", cid, "--tests", "-", stdin=PASSING)
    assert "Open review findings" in out
    code, out = run("spec", "forget", cid)
    assert "Deleted the baseline" in out
    con = store.connect(repo / ".leyline/leyline.db")
    assert not diff.snapshot_path(con, cid).exists() and not quick._src_path(con, cid).exists()


def test_what_it_cannot_place(repo):
    code, out = run("quick", "make it faster")
    assert code == 1 and "name the code" in out
    code, out = run("quick", "make it faster", "--about", "Nowhere.nothing_here")
    assert code == 1 and "none of the names is code on the map" in out
    code, out = run("quick", "--done", "quick-never-started", "--tests", "-", stdin=PASSING)
    assert code == 1 and "no quick change" in out
    code, out = run("quick", "--done", "quick-make-the-retry-count-3")
    assert code == 1
    assert quick.slug("Make the `RETRIES` count 3, please, and quickly for everyone") == "quick-make-the-retries-count-3-please-and"


def test_nothing_changed_is_inconclusive(repo):
    run("quick", "make the retry count 3", "--about", "RETRIES", "--tests", "-", stdin=PASSING)
    code, page = run("quick", "--done", "quick-make-the-retry-count-3", "--tests", "-", stdin=PASSING)
    assert code == 1 and "Edits stayed in the named code: inconclusive" in page
