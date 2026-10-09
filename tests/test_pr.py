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
    assert "use.py.first" not in page.split("## What it reaches")[1].split("## Tests")[0]   # the one it updated
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
    assert "with uncommitted edits" in page and "`use.py.count`" not in page.split("## What it reaches")[1].split("## Tests")[0]
    code, _ = run("pr", "no-such-branch")
    assert code == 1


def test_a_new_file_not_yet_added_counts_as_an_uncommitted_edit(branch, monkeypatch):
    """The map reads the files git lists, untracked ones included, so a new file not yet added is in the review: the
    page must say the checkout has uncommitted edits, not that it is the head commit."""
    monkeypatch.chdir(branch)
    (branch / "app/extra.py").write_text("def extra():\n    return 3\n")
    r = pr.review(branch / ".leyline/leyline.db", branch, "main")
    assert "extra" in [x["name"] for x in r["changed"]["added"]]
    assert r["dirty"] is True and "with uncommitted edits" in Path(r["page"]).read_text()


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


def test_the_base_is_every_file_of_the_commit_whatever_its_export_attributes_say(tmp_path, monkeypatch):
    """`export-ignore` and `export-subst` shape release tarballs, not the code: a base mapped from an archive that
    applied them would lack the ignored tests (so the branch seems to add them all) and hold a substituted version
    file (so the branch seems to edit it)."""
    root = tmp_path / "repo"
    files = {**FILES, "app/_version.py": "def version():\n    return \"$Format:%H$\"\n",
             ".gitattributes": "tests export-ignore\napp/_version.py export-subst\n"}
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    other = root / "app/other.py"
    other.write_text(other.read_text().replace("return 1", "return 2"))
    git(root, "commit", "-qam", "Return two")
    monkeypatch.chdir(root)
    r = pr.review(root / ".leyline/leyline.db", root, "main")
    c = r["changed"]
    assert [x["name"] for x in c["edited"]] == ["unrelated"]
    assert c["added"] == [] and c["files"] == ["app/other.py"]
    assert git(root, "status", "--porcelain") == ""


def test_files_the_map_does_not_read_are_named_as_they_are(branch):
    """git quotes a path with a byte outside ASCII (`"caf\\303\\251.py"`) unless told not to: a mapped file of that
    name must not be listed as one the map does not read, and a doc must be listed by its own name."""
    for f in ("app/café.py", "docs/résumé.md", "docs/my notes.md"):
        (branch / f).parent.mkdir(parents=True, exist_ok=True)
        (branch / f).write_text("x = 1\n")
    git(branch, "add", "app/café.py", "docs/résumé.md")
    git(branch, "commit", "-qm", "More files")
    base = git(branch, "merge-base", "main", "HEAD").strip()
    assert pr._other_files(branch, base, ["app/café.py", "app/store.py", "app/use.py", "web/server.ts"]) == ["docs/my notes.md", "docs/résumé.md"]


def test_a_github_pull_request_is_read_against_origins_base_not_a_stale_local_one(tmp_path, monkeypatch):
    """GitHub compares a pull request with its base branch as GitHub has it. A clone whose own `main` was left behind
    (fetched, never pulled) must not count what `main` gained since as part of the pull request."""
    up = tmp_path / "upstream"
    for f, text in FILES.items():
        (up / f).parent.mkdir(parents=True, exist_ok=True)
        (up / f).write_text(text)
    git(up, "init", "-q", "-b", "main")
    git(up, "add", "-A")
    git(up, "commit", "-qm", "base")
    git(tmp_path, "clone", "-q", str(up), "clone")
    root = tmp_path / "clone"
    other = up / "app/other.py"
    other.write_text(other.read_text().replace("return 1", "return 2"))
    git(up, "commit", "-qam", "Upstream work, merged before the pull request")
    git(root, "fetch", "-q")
    git(root, "checkout", "-q", "-b", "feature", "origin/main")
    use = root / "app/use.py"
    use.write_text(use.read_text().replace("return len(load(p))", "return len(load(p)) + 0"))
    git(root, "commit", "-qam", "Count one more")
    head = git(root, "rev-parse", "HEAD").strip()
    monkeypatch.setattr(pr, "github_pr", lambda root, n: {"baseRefName": "main", "title": "Count one more", "body": "",
                                                          "headRefOid": head, "number": 7, "url": ""})
    monkeypatch.chdir(root)
    r = pr.review(root / ".leyline/leyline.db", root, github="7")
    assert r["base_sha"] == git(root, "rev-parse", "origin/main").strip()
    assert [x["name"] for x in r["changed"]["edited"]] == ["count"]


def test_a_checkout_in_a_folder_named_like_a_url_is_reviewed(tmp_path, monkeypatch):
    """The base map and each run's map are opened read-only by URI: `#` and `%` in a folder's name are characters."""
    root = tmp_path / "C# work" / "repo"
    root.parent.mkdir()
    for f, text in FILES.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    use = root / "app/use.py"
    use.write_text(use.read_text().replace("return len(load(p))", "return len(load(p)) + 0"))
    git(root, "commit", "-qam", "Count one more")
    monkeypatch.chdir(root)
    r = pr.review(root / ".leyline/leyline.db", root, "main")
    assert [x["name"] for x in r["changed"]["edited"]] == ["count"]
    use.write_text(use.read_text().replace("+ 0", "+ 1"))
    git(root, "commit", "-qam", "Count two more")
    r = pr.review(root / ".leyline/leyline.db", root, "main")
    assert [x["name"] for x in r["since_last_review"]["code"]["edited"]] == ["count"]


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


def test_the_description_follows_the_commits_and_keeps_what_a_person_said(tmp_path, monkeypatch):
    """A description made from commit messages is not stored as if someone said it: each review reads the commits
    again, so a new commit shows; one given with --about is kept for the reviews after it."""
    root = tmp_path / "repo"
    for f, text in FILES.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(root)

    def said(page):
        return next(ln for ln in page.splitlines() if ln.startswith("**What it says it does:**"))
    use = root / "app/use.py"
    use.write_text(use.read_text().replace("return len(load(p))", "return len(load(p)) + 0"))
    page = run("pr", "main")[1]
    assert page.startswith("# Review: Uncommitted edits on feature") and "no description given" in said(page)

    git(root, "commit", "-qam", "Count one more way")
    page = run("pr", "main")[1]
    assert said(page) == "**What it says it does:** From its commit messages: Count one more way"
    assert page.startswith("# Review: Count one more way")

    use.write_text(use.read_text().replace("return helper(x)", "return helper(x) + 0"))
    git(root, "commit", "-qam", "Double one more way")
    page = run("pr", "main")[1]
    assert "Double one more way" in said(page) and "Count one more way" in said(page)
    assert said(page).count("Double one more way") == 1                       # each subject said once
    assert page.startswith("# Review: Count one more way")                     # the title stays the first commit's

    page = run("pr", "main", "--about", "Make counting and doubling agree")[1]
    assert "Make counting and doubling agree" in said(page)
    page = run("pr", "main")[1]
    assert "Make counting and doubling agree" in said(page) and "commit messages" not in said(page)


# -- the gate: `leyline pr --gate` exits 1 while something that blocks is left ----------------------------
def gate_of(page: str) -> str:
    return page.split("## Gate")[1].split("\n## ")[0]


def fix_the_branch(root):
    """Update the caller `count` that the branch forgot, and stop `double` calling the removed `helper`."""
    use = root / "app/use.py"
    use.write_text(use.read_text().replace("return len(load(p))", "return len(load(p, \"utf-8\"))")
                   .replace("return helper(x)", "return x * 2"))
    git(root, "commit", "-qam", "Update the callers")


def configure(root, text):
    (root / "openspec").mkdir(exist_ok=True)
    (root / "openspec/leyline.toml").write_text(text)


def test_a_review_exits_0_without_gate_even_with_blockers(branch, monkeypatch):
    monkeypatch.chdir(branch)
    code, out = run("pr", "main")
    assert code == 0
    gate = gate_of(out)
    assert "**Blocked** by 2 things" in gate and "(the default; `leyline pr --gate` exits 1 when blocked)" in gate
    assert "`use.py.count` calls code whose signature changed, and was not edited" in gate
    assert "`helper` was removed and is still called by `double`" in gate
    assert out.rstrip().splitlines()[-1].startswith("Next: update the call in `use.py.count`")
    page = (branch / ".leyline/reviews/pr-feature.md").read_text()
    assert "## Gate" in page and "Next: update the call in `use.py.count`" in page
    assert run("pr", "main", "--json")[0] == 0


def test_the_gate_fails_on_an_unedited_caller_and_passes_once_fixed(branch, monkeypatch):
    monkeypatch.chdir(branch)
    configure(branch, '[pr]\nblocking = ["unedited-callers"]\n')
    code, out = run("pr", "main", "--gate")
    assert code == 1
    assert "**Blocked** by 1 thing" in gate_of(out) and "set in openspec/leyline.toml" in gate_of(out)
    code, out = run("pr", "main", "--gate", "--json")
    r = json.loads(out)
    assert code == 1 and r["gate"]["passed"] is False
    assert r["gate"]["blocking"] == ["`use.py.count` calls code whose signature changed, and was not edited"]
    fix_the_branch(branch)   # a re-review after a new commit judges the gate again from what is there now
    code, out = run("pr", "main", "--gate")
    assert code == 0 and "**Passes.** Nothing that blocks is left." in gate_of(out)
    assert "Next: have it reviewed" in out


def test_the_project_narrows_and_widens_what_blocks(branch, monkeypatch):
    monkeypatch.chdir(branch)
    configure(branch, '[pr]\nblocking = ["still-called"]\n')
    code, out = run("pr", "main", "--gate")
    assert code == 1 and "**Blocked** by 1 thing" in gate_of(out) and "`helper` was removed" in gate_of(out)
    configure(branch, "[pr]\nblocking = []\n")
    code, out = run("pr", "main", "--gate")
    assert code == 0 and "Blocking: nothing (set in openspec/leyline.toml" in gate_of(out)

    fix_the_branch(branch)
    configure(branch, "")   # the default: nothing the map shows broken is left
    assert run("pr", "main", "--gate")[0] == 0
    # wider: the client that reads what the edited route answers, which the branch did not edit
    configure(branch, '[pr]\nblocking = ["unedited-callers", "other_ends", "no such kind"]\n')
    code, out = run("pr", "main", "--gate")
    gate = gate_of(out)
    assert code == 1 and "`client.ts.items`" in gate and "Blocking: unedited-callers, other-ends" in gate
    assert "names \"no such kind\" under [pr], which is not a kind" in gate


def test_an_open_high_finding_blocks_and_a_resolved_one_does_not(branch, monkeypatch):
    monkeypatch.chdir(branch)
    fix_the_branch(branch)
    assert run("pr", "main", "--gate")[0] == 0
    con = store.connect(branch / ".leyline/leyline.db")
    count = con.execute("SELECT id FROM nodes WHERE name = 'count'").fetchone()[0]
    con.close()
    run("spec", "finding", "pr-feature", "--reviewer", "logic", "--severity", "low",
        "--claim", "count could say what it counts", "--evidence", count)
    assert run("pr", "main", "--gate")[0] == 0     # a low finding is not in the default set
    out = run("spec", "finding", "pr-feature", "--reviewer", "logic", "--severity", "high",
              "--claim", "count reads the whole file into memory.", "--evidence", count)[1]
    high = json.loads(out)["id"]
    code, out = run("pr", "main", "--gate")
    assert code == 1
    assert f"- open high finding {high}: count reads the whole file into memory." in gate_of(out)
    assert f"Next: fix what finding {high} says, or have the person resolve it" in out
    assert run("spec", "resolve", high, "rejected", "it is small")[0] == 0
    assert run("pr", "main", "--gate")[0] == 0
    configure(branch, '[pr]\nblocking = ["open-findings"]\n')   # wider: any open finding, the low one too
    code, out = run("pr", "main", "--gate")
    assert code == 1 and "open low finding" in gate_of(out)


def test_a_confirmed_error_rule_newly_failing_blocks(branch, monkeypatch):
    from leyline import rules
    monkeypatch.chdir(branch)
    fix_the_branch(branch)
    run("pr", "main")
    con = store.connect(branch / ".leyline/leyline.db")
    rules.add_rule(con, "forbid", "path:app/use.py", "path:app/other.py", status="confirmed")
    rules.add_rule(con, "forbid", "path:app/use.py", "path:app/store.py", status="suggested")   # never blocks
    con.close()
    assert run("pr", "main", "--gate")[0] == 0
    use = branch / "app/use.py"
    use.write_text("from app.other import unrelated\n" + use.read_text().replace("return x * 2", "return unrelated() * x"))
    git(branch, "commit", "-qam", "Use other")
    code, out = run("pr", "main", "--gate")
    assert code == 1 and "forbid path:app/use.py -> path:app/other.py) now fails" in gate_of(out)
    assert "path:app/store.py" not in gate_of(out)


def guess_repo(root, param: str):
    """`Repo.fetch_rows` gains a parameter and its one caller, `run`, is not edited. When `run`'s parameter has no
    type the map links `r.fetch_rows(1)` by name only, as a guess; typed `r: Repo`, the link is certain."""
    files = {"app/repo.py": "class Repo:\n    def fetch_rows(self, q):\n        return [q]\n",
             "app/use.py": f"from app.repo import Repo\n\n\ndef run({param}):\n    return r.fetch_rows(1)\n"}
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "app/repo.py").write_text("class Repo:\n    def fetch_rows(self, q, limit):\n        return [q][:limit]\n")
    git(root, "commit", "-qam", "Limit the rows")
    return root


def test_a_caller_linked_only_by_a_guess_does_not_block(tmp_path, monkeypatch):
    root = guess_repo(tmp_path / "guess", "r")
    monkeypatch.chdir(root)
    code, out = run("pr", "main", "--gate")
    assert "Signature: `Repo.fetch_rows` (self, q) -> (self, q, limit)" in out
    assert "**Not edited, calls changed code:** `use.py.run` (its call must change (link is a guess))" in out
    assert code == 0 and "**Passes.**" in gate_of(out)
    assert "Left out of the gate: 1 item below that rest only on links the map guessed by name" in gate_of(out)

    sure = guess_repo(tmp_path / "sure", "r: Repo")   # the same caller, linked for certain
    monkeypatch.chdir(sure)
    code, out = run("pr", "main", "--gate")
    assert code == 1 and "`use.py.run` calls code whose signature changed" in gate_of(out)


def test_the_tests_that_run_the_change_are_counted_past_the_thirty_listed(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app/core.py").write_text("def helper(x):\n    return x\n")
    (root / "tests/test_many.py").write_text("from app.core import helper\n\n" + "".join(
        f"\ndef test_n{i}():\n    assert helper({i}) == {i}\n" for i in range(40)))
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "app/core.py").write_text("def helper(x):\n    return x + 0\n")
    git(root, "commit", "-qam", "Add zero")
    monkeypatch.chdir(root)
    code, out = run("pr", "main")
    line = next(x for x in out.splitlines() if x.startswith("- Tests that run the changed code:"))
    assert line.endswith(" and 34 more."), line
