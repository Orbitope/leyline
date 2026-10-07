"""`leyline pr`: review a branch someone else wrote, with no spec. The base is mapped from git's copy of the commit,
the repository is left as it was, and what the change reaches and did not change is reported."""

import io
import json
import os
import subprocess
import tarfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from leyline import cli, diff, pr, store

FILES = {
    "app/store.py": "def load(path):\n    with open(path) as f:\n        return f.read()\n\n\n"
                    "def helper(x):\n    return x * 2\n",
    "app/use.py": "from app.store import load, helper\n\n\ndef first(p):\n    return load(p).split(\"\\n\")[0]\n\n\n"
                  "def count(p):\n    return len(load(p))\n\n\ndef double(x):\n    return helper(x)\n",
    "web/server.ts": "import express from \"express\";\nconst app = express();\nexport function routes() {\n"
                     "  app.get(\"/api/items\", (req, res) => {\n    res.json({ items: [1, 2, 3] });\n  });\n}\n",
    "web/client.ts": "export async function items() {\n  const r = await fetch(\"/api/items\");\n"
                     "  return (await r.json()).items;\n}\n",
    "app/other.py": "def unrelated():\n    return 1\n",
    "tests/test_use.py": "from app.use import count\n\n\ndef test_count(tmp_path):\n    p = tmp_path / \"x\"\n"
                         "    p.write_text(\"ab\")\n    assert count(str(p)) == 2\n",
}


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def branch(tmp_path):
    """A repository with a base commit on main and, checked out, a branch that changes `load`'s signature and
    updates one of its two callers, removes `helper` while `double` still calls it, and changes what the
    /api/items route answers."""
    root = tmp_path / "repo"
    for f, text in FILES.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "app/store.py").write_text("def load(path, encoding):\n    with open(path, encoding=encoding) as f:\n"
                                       "        return f.read()\n")
    use = root / "app/use.py"
    use.write_text(use.read_text().replace("return load(p).split", "return load(p, \"utf-8\").split"))
    server = root / "web/server.ts"
    server.write_text(server.read_text().replace("{ items: [1, 2, 3] }", "{ rows: [1, 2, 3] }"))
    git(root, "commit", "-qam", "Read files with an encoding")
    return root


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


def test_a_branch_is_reviewed_with_no_spec(branch, monkeypatch):
    monkeypatch.chdir(branch)
    code, page = run("pr", "main")
    assert code == 0, page
    assert "Signature: `load` (path) -> (path, encoding)" in page
    assert "**Not edited, calls changed code:** `use.py.count`" in page      # the caller the branch forgot
    assert "use.py.first" not in page.split("## What it reaches")[1].split("Shares")[0]   # the one it updated
    assert "**Removed but still called:** `helper`, from `double`" in page
    assert "Crosses the http GET /api/items" in page and "`client.ts.items`" in page   # the route's answer changed
    assert "Read files with an encoding" in page          # no --about: the commits say what it does
    # the repository is as it was: no worktree, nothing new that git sees
    assert git(branch, "worktree", "list").count("\n") == 1
    assert git(branch, "status", "--porcelain") == ""
    assert (branch / ".leyline/reviews/pr-feature.md").read_text().startswith("# Review:")


def test_the_base_map_is_kept_and_the_review_takes_findings(branch, monkeypatch):
    monkeypatch.chdir(branch)
    run("pr", "main")
    con = store.connect(branch / ".leyline/leyline.db")
    snap = diff.snapshot_path(con, "pr-feature")
    made = snap.stat().st_mtime_ns
    assert run("pr", "main")[0] == 0 and snap.stat().st_mtime_ns == made   # same base commit: mapped once
    code, out = run("spec", "facts", "pr-feature", "--reviewer", "logic")
    facts = json.loads(out)
    assert code == 0 and [m["name"] for m in facts["logic"]["signature_changed_callers_not_edited"]] == ["use.py.count"]
    count = next(m["id"] for m in facts["logic"]["signature_changed_callers_not_edited"])
    code, out = run("spec", "finding", "pr-feature", "--reviewer", "logic", "--severity", "high",
                    "--claim", "count still calls load with one argument", "--evidence", count, "--proposal", "pass utf-8")
    assert code == 0 and "warning" not in json.loads(out)
    far = con.execute("SELECT id FROM nodes WHERE name = 'unrelated'").fetchone()[0]
    code, out = run("spec", "finding", "pr-feature", "--reviewer", "logic", "--severity", "low",
                    "--claim", "unrelated is wrong", "--evidence", far)
    assert "blast radius" in json.loads(out)["warning"]
    page = run("pr", "main")[1]
    assert "- Logic review: 2 findings, 2 still open." in page
    assert "count still calls load with one argument" in page and "not near the change" in page
    assert run("spec", "forget", "pr-feature")[0] == 0 and not snap.exists()


def test_uncommitted_edits_count_and_a_missing_base_is_said(branch, monkeypatch):
    monkeypatch.chdir(branch)
    use = branch / "app/use.py"
    use.write_text(use.read_text().replace("return len(load(p))", "return len(load(p, \"utf-8\"))"))
    page = run("pr", "main")[1]
    assert "with uncommitted edits" in page and "`use.py.count`" not in page.split("## What it reaches")[1].split("Shares")[0]
    code, _ = run("pr", "no-such-branch")
    assert code == 1


def test_a_hostile_archive_cannot_write_outside(tmp_path):
    """The base of someone else's branch is extracted from an archive: entries that climb out, and links that point
    out, are left out."""
    tar_path = tmp_path / "x.tar"
    with tarfile.open(tar_path, "w") as tar:
        for name, data in (("ok/a.py", b"x = 1\n"), ("../evil.py", b"bad\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (("out", "/etc"), ("up", "../../.."), ("in", "ok/a.py")):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    into = tmp_path / "into"
    into.mkdir()
    with tarfile.open(tar_path) as tar:
        kept = [m.name for m in pr._safe_members(tar, into)]
    assert kept == ["ok/a.py", "in"]


def test_a_moved_checkout_maps_without_parsing_again(tmp_path):
    """Parse output depends on a file's text, not on where the checkout is: a store mapped again from another place
    (as the base of a pull request is) reuses it."""
    from leyline.indexer import index
    root = tmp_path / "repo"
    for f, text in FILES.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    db = tmp_path / "s" / "leyline.db"
    first = index(root, db, "repo", "off")
    assert first["incremental"]["files_parsed"] == len(FILES)
    moved = tmp_path / "elsewhere" / "repo"
    moved.parent.mkdir()
    root.rename(moved)
    again = index(moved, db, "repo", "off")
    assert again["incremental"]["mode"] == "full" and again["incremental"]["files_parsed"] == 0
    (moved / "app/store.py").write_text(FILES["app/store.py"] + "\n\ndef more():\n    return 1\n")
    third = index(moved, db, "repo", "off", full=True)    # asked for: everything is parsed again
    assert third["incremental"]["files_parsed"] == len(FILES)
