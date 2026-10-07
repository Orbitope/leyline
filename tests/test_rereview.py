"""Reviewing a pull request again after new commits: what changed since the last review, which facts are new and
which are gone, and which open findings its new code may have fixed. And earlier changes to the same code."""

import io
import json
import subprocess
from contextlib import redirect_stdout

import pytest

from leyline import cli, diff, rereview, store

FILES = {
    "app/store.py": "def load(path):\n    with open(path) as f:\n        return f.read()\n\n\n"
                    "def helper(x):\n    return x * 2\n",
    "app/use.py": "from app.store import load, helper\n\n\ndef first(p):\n    return load(p).split(\"\\n\")[0]\n\n\n"
                  "def count(p):\n    return len(load(p))\n\n\ndef double(x):\n    return helper(x)\n",
    "app/other.py": "def unrelated():\n    return 1\n",
    "tests/test_use.py": "from app.use import count\n\n\ndef test_count(tmp_path):\n    p = tmp_path / \"x\"\n"
                         "    p.write_text(\"ab\")\n    assert count(str(p)) == 2\n",
}


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


@pytest.fixture
def branch(tmp_path):
    """main has two earlier commits to app/use.py; the branch changes `load`'s signature and updates only `first`."""
    root = tmp_path / "repo"
    for f, text in FILES.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Start")
    use = root / "app/use.py"
    use.write_text(use.read_text() + "\n\ndef twice(p):\n    return count(p) * 2\n")
    git(root, "commit", "-qam", "Add twice to the use module")
    (root / "app/other.py").write_text("def unrelated():\n    return 2\n")
    git(root, "commit", "-qam", "Touch only other")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "app/store.py").write_text("def load(path, encoding):\n    with open(path, encoding=encoding) as f:\n"
                                       "        return f.read()\n\n\ndef helper(x):\n    return x * 2\n")
    use.write_text(use.read_text().replace("return load(p).split", "return load(p, \"utf-8\").split"))
    git(root, "commit", "-qam", "Read files with an encoding")
    return root


def test_a_second_review_says_what_changed_since_and_which_findings_may_be_fixed(branch, monkeypatch):
    monkeypatch.chdir(branch)
    code, page = run("pr", "main")
    assert code == 0 and "## Since the last review" not in page   # a first review has nothing to compare with
    facts = json.loads(run("spec", "facts", "pr-feature", "--reviewer", "logic")[1])
    assert facts["since_last_review"] == {"first_review": True}
    count = next(m["id"] for m in facts["logic"]["signature_changed_callers_not_edited"] if m["name"] == "use.py.count")
    other = next(e["id"] for e in facts["changed"]["edited"] if e["name"] == "load")   # not edited again
    fixed = json.loads(run("spec", "finding", "pr-feature", "--reviewer", "logic", "--severity", "high",
                           "--claim", "count still calls load with one argument", "--evidence", count)[1])["id"]
    stays = json.loads(run("spec", "finding", "pr-feature", "--reviewer", "logic", "--severity", "low",
                           "--claim", "load does not say which encodings it takes", "--evidence", other)[1])["id"]
    head1 = git(branch, "rev-parse", "HEAD").strip()

    use = branch / "app/use.py"   # the author fixes count, adds a function, and breaks double by changing helper
    use.write_text(use.read_text().replace("return len(load(p))", "return len(load(p, \"utf-8\"))")
                   + "\n\ndef lines(p):\n    return load(p, \"utf-8\").count(\"\\n\")\n")
    st = branch / "app/store.py"
    st.write_text(st.read_text().replace("def helper(x):\n    return x * 2", "def helper(x, scale):\n    return x * scale"))
    git(branch, "commit", "-qam", "Pass the encoding in count")
    code, page = run("pr", "main")
    assert code == 0
    section = page.split(f"## Since the last review (`{head1[:7]}`, ")[1].split("\n## ")[0]
    assert "2 functions edited (`helper`, `count`)" in section and "1 added (`lines`)" in section
    assert "**New:** `use.py.double` calls code whose signature changed, and was not edited." in section
    assert "Gone (fixed, or no longer true): `use.py.count` calls code whose signature changed" in section
    assert f"{fixed} (high, logic): count still calls load with one argument. **May be fixed:** its code changed" in section
    assert f"{stays} (low, logic): load does not say which encodings it takes. **Still applies:**" in section
    assert "(Its code changed since it was filed: re-check it.)" in page.split("## Review")[1]

    facts = json.loads(run("spec", "facts", "pr-feature", "--reviewer", "logic")[1])
    s = facts["since_last_review"]
    assert [x["id"] for x in s["findings"]["may_be_fixed"]] == [fixed]
    assert [x["id"] for x in s["findings"]["still_applies"]] == [stays]
    assert sorted(x["name"] for x in s["code"]["edited"]) == ["count", "helper"] and "ask" in s
    assert "Pass the encoding in count" in facts["what_it_says_it_does"]   # the new commits' messages count too

    # Running it again on the same code says the same thing: the record is updated, not added to.
    con = store.connect(branch / ".leyline/leyline.db")
    assert [r["seq"] for r in rereview.runs(con, "pr-feature")] == [1, 2]
    again = run("pr", "main")[1]
    assert f"## Since the last review (`{head1[:7]}`, " in again and "1 added (`lines`)" in again
    assert all(diff.snapshot_path(con, r["snapshot"]).exists() for r in rereview.runs(con, "pr-feature"))
    assert run("spec", "forget", "pr-feature")[0] == 0 and rereview.runs(con, "pr-feature") == []


def test_earlier_commits_to_the_same_code_are_listed_from_git(branch, monkeypatch):
    monkeypatch.chdir(branch)
    page = run("pr", "main")[1]
    rel = page.split("Earlier changes to this code (")[1].split("\n\n")[1]
    first, second = rel.split("\n")[:2]
    assert "Start. Changed `load`, `first`." in first   # its diff names the functions the branch changed
    assert "Add twice to the use module. Changed the same file: `app/use.py`." in second   # only their file
    assert "Touch only other" not in rel and "Read files with an encoding" not in rel   # other files; the branch itself
    facts = json.loads(run("spec", "facts", "pr-feature")[1])
    assert facts["related_changes"]["source"] == "git" and len(facts["related_changes"]["items"]) == 2


def test_an_archived_change_and_an_earlier_review_are_found_by_the_code_they_touched(branch, monkeypatch):
    monkeypatch.chdir(branch)
    run("pr", "main")   # reviewed once as pr-feature: it edited load and first
    arch = branch / "openspec/changes/archive/2026-09-01-fast-count"
    arch.mkdir(parents=True)
    (arch / "proposal.md").write_text("# Count faster\n\n## Why\n\nSpeed.\n")
    (arch / "tasks.md").write_text("- [x] 1.1 Change `count` to read less\n")
    live = branch / "openspec/changes/count-lines"
    live.mkdir(parents=True)
    (live / "proposal.md").write_text("# Count lines\n\n## Why\n\nNeeded.\n\n## What Changes\n\nCount lines.\n")
    (live / "tasks.md").write_text("- [ ] 1.1 Change `count` to count lines\n- [ ] 1.2 Change `load` to strip\n")
    code, out = run("plan", "count-lines")
    rel = out.split("**Earlier changes to this code** (")[1].split("\n\n")[1]
    first, second = rel.split("\n")[:2]
    assert first.startswith("- `pr-feature` (reviewed 20") and "Touched `load`." in first
    assert second == "- `fast-count` (archived 2026-09-01): Count faster. Touched `count`."
    con = store.connect(branch / ".leyline/leyline.db")
    from leyline import spec
    facts = spec.review_facts(con, str(live))
    assert [x["id"] for x in facts["related_changes"]["items"]] == ["pr-feature", "fast-count"]
