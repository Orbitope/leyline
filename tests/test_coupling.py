"""Change coupling from git history: files that usually change together, which the map cannot link (docs, schemas,
fixtures). Read once per commit, listed in a plan for files no task names, and in a pull request review for files
the branch did not change."""

import io
import json
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from leyline import cli, coupling, spec, store
from leyline.indexer import index


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def write(root: Path, path: str, text: str) -> None:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(text)


def commit(root: Path, message: str, files: dict) -> None:
    for path, text in files.items():
        write(root, path, text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


RULES = "def check(rule):\n    return rule.get(\"ok\", False)\n\n\ndef helper(x):\n    return x\n"
OTHER = "from src.rules import check\n\n\ndef run(r):\n    return check(r)\n"
DOC = "# Rules\n\nEach rule is checked by `check`.\n" + "".join(f"\nLine {i} of the rules guide.\n" for i in range(20))


@pytest.fixture
def repo(tmp_path):
    """A history in which every change to `src/rules.py` came with its doc (renamed part-way from docs/old.md), most
    with the schema, and each with a new test case in a folder of its own. One bulk commit (more than 50 files)
    changed rules.py and other.py together, and does not count; one ordinary commit did, once."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    commit(root, "Start", {"src/rules.py": RULES, "src/other.py": OTHER, "docs/old.md": DOC,
                           "schema/rule.json": json.dumps({"type": "object"}), "README.md": "# Repo\n"})
    for i in range(4):
        if i == 2:   # the doc is renamed, with an edit, in the same commit as a rule change
            git(root, "mv", "docs/old.md", "docs/rules.md")
        doc = "docs/rules.md" if i >= 2 else "docs/old.md"
        files = {"src/rules.py": RULES + f"\n\ndef rule_{i}(x):\n    return x + {i}\n",
                 doc: (root / doc).read_text() + f"\n## Rule {i}\n",
                 f"tests/cases/case-{i}/case.json": json.dumps({"rule": i})}
        if i != 1:
            files["schema/rule.json"] = json.dumps({"type": "object", "rules": i})
        RULES_NOW = files["src/rules.py"]
        commit(root, f"Add rule {i}", files)
    commit(root, "Tidy other", {"src/other.py": OTHER + "\n# tidy\n"})
    commit(root, "Reformat everything", {**{f"gen/f{i}.txt": "x\n" for i in range(60)},
                                         "src/rules.py": RULES_NOW + "\n", "src/other.py": OTHER + "\n# fmt\n"})
    commit(root, "Rules and other once", {"src/rules.py": RULES_NOW + "\n\n# once\n", "src/other.py": OTHER + "\n# once\n"})
    return root


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


def test_history_is_counted_with_renames_followed_and_bulk_commits_left_out(repo):
    r = coupling.compute(repo, coupling.head(repo))
    assert r["bulk"] == 1 and r["commits"] == 7
    assert r["changes"]["src/rules.py"] == 6                      # start, four rules, once; not the reformat
    assert r["pairs"][("docs/rules.md", "src/rules.py")] == 5     # its commits as docs/old.md count under its name now
    assert "docs/old.md" not in r["changes"]                      # a name the file no longer has is not reported
    assert r["pairs"][("src/other.py", "src/rules.py")] == 2
    assert r["dirs"][("src/rules.py", "tests/cases/")] == 4      # a new case each time: coupled to the folder


def test_partners_are_files_and_a_folder_no_single_file_explains(repo):
    con = store.connect(repo / "s.db")
    store.set_root(con, "r", repo)
    con.commit()
    run_ = coupling.ensure(con, "r", repo)
    assert run_ and not run_["cached"]
    assert coupling.ensure(con, "r", repo)["cached"]              # once per commit
    ps = {x["path"]: x for x in coupling.partners(con, run_, ["src/rules.py"], folders=True)["src/rules.py"]}
    assert ps["docs/rules.md"]["together"] == 5 and ps["docs/rules.md"]["confidence"] == 0.83
    assert ps["schema/rule.json"]["together"] == 4
    assert ps["tests/cases/"]["folder"] and ps["tests/cases/"]["together"] == 4
    assert "src/other.py" not in ps                                # 2 of 6: below both thresholds
    assert "docs/" not in ps and "tests/" not in ps                # a file, or a deeper folder, already says it
    # a new commit is read again, under its own sha
    commit(repo, "More", {"README.md": "# Repo\n\nMore.\n"})
    again = coupling.ensure(con, "r", repo)
    assert not again["cached"] and again["sha"] != run_["sha"]
    con.close()


def spec_folder(root: Path, tasks: str) -> Path:
    ch = root / "openspec" / "changes" / "strict-rules"
    (ch / "specs" / "rules").mkdir(parents=True, exist_ok=True)
    (ch / "proposal.md").write_text("# Change: Strict rules\n\n## Why\nA rule with no ok key passes today.\n")
    (ch / "tasks.md").write_text(tasks)
    (ch / "specs" / "rules" / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Strict\nIt SHALL fail a rule with no ok key.\n\n"
        "#### Scenario: Missing ok\n- **WHEN** a rule has no ok key\n- **THEN** it fails\n")
    return ch


def test_a_plan_lists_what_usually_changes_with_the_tasks_files_and_no_task_names(repo):
    db = repo / ".leyline" / "leyline.db"
    index(repo, db, "r")
    ch = spec_folder(repo, "- [ ] 1.1 Change `check` to fail a rule with no ok key\n")
    con = store.connect(db)
    b = spec.brief(con, ch)
    found = {x["path"]: x for x in b["usually_changes_with"]["files"]}
    assert set(found) == {"docs/rules.md", "schema/rule.json", "tests/cases/"}
    page = (ch / "leyline.md").read_text()
    assert ("**Usually changes with the files the tasks touch, and no task names it** (from the last 7 commits, leaving out"
            " 1 that changed more than 50 files)") in page
    assert "- `docs/rules.md` changed in 5 of the 6 commits that changed `src/rules.py`; no task names it." in page
    assert "- Files in `tests/cases/` changed in 4 of the 6 commits that changed `src/rules.py`; no task names it." in page
    facts = spec.review_facts(con, ch)
    assert len(facts["logic"]["usually_changes_with_no_task"]["files"]) == 3

    # a task that names the doc and the schema (by the end of its path, without backticks) and adds a case
    spec_folder(repo, "- [ ] 1.1 Change `check` to fail a rule with no ok key\n"
                      "- [ ] 1.2 Describe it in `docs/rules.md` and in rule.json\n"
                      "- [ ] 1.3 Add a case in tests/cases/missing-ok/\n")
    b = spec.brief(con, ch)
    assert b["usually_changes_with"]["files"] == []
    assert "Usually changes with" not in (ch / "leyline.md").read_text()
    con.close()


def test_a_plan_outside_git_says_nothing_about_history(tmp_path):
    root = tmp_path / "plain"
    write(root, "src/rules.py", RULES)
    db = tmp_path / "s.db"
    index(root, db, "p")
    ch = spec_folder(root, "- [ ] 1.1 Change `check` to fail a rule with no ok key\n")
    con = store.connect(db)
    b = spec.brief(con, ch)
    assert b["usually_changes_with"] == {} and "Usually changes with" not in (ch / "leyline.md").read_text()
    con.close()


def test_a_pull_request_lists_what_usually_changed_with_its_files_and_did_not(repo, monkeypatch):
    git(repo, "checkout", "-q", "-b", "feature")
    commit(repo, "Strict rules", {"src/rules.py": RULES.replace("False", "None"), "schema/rule.json": "{}"})
    monkeypatch.chdir(repo)
    code, page = run("pr", "main")
    assert code == 0, page
    block = page.split("Usually changes with what it changed, and it did not change")[1].split("##")[0]
    assert "(from the last 7 commits, leaving out 1 that changed more than 50 files before the branch)" in block
    # each once, with the changed file it went with most often
    assert ("- `docs/rules.md` changed in 4 of the 4 commits that changed `schema/rule.json` (and often with"
            " `src/rules.py`).") in block
    assert "tests/cases/" in block and "- `schema/rule.json` changed" not in block   # the branch changed the schema
    con = store.connect(repo / ".leyline" / "leyline.db")
    from leyline import pr
    facts = pr.review_facts(con, "pr-feature")
    assert {x["path"] for x in facts["logic"]["usually_changes_with_not_changed"]["files"]} == {"docs/rules.md", "tests/cases/"}
    con.close()


def test_the_coupling_command(repo, monkeypatch, capsys):
    db = repo / ".leyline" / "leyline.db"
    index(repo, db, "r")
    monkeypatch.chdir(repo / "src")
    code, out = run("--db", str(db), "coupling", "rules.py")     # relative to here
    assert code == 0
    assert out.startswith("`src/rules.py` changed in 6 commits (from the last 7 commits")
    assert "- `docs/rules.md`: 5 of 6 (83%)" in out and "- files in `tests/cases/`: 4 of 6 (67%)" in out
    code, out = run("--db", str(db), "coupling", "--json", "src/other.py", "--min-together", "2", "--min-confidence", "0.6")
    r = json.loads(out)
    assert r["path"] == "src/other.py" and [x["path"] for x in r["partners"]][:1] == ["src/rules.py"]
    code, out = run("--db", str(db), "coupling")
    # each pair said from the file that changed less often
    assert code == 0 and "- `src/rules.py` changed in 5 of the 5 commits that changed `docs/rules.md`" in out
    code, _ = run("--db", str(db), "coupling", "nothing.py")
    assert code == 1 and "no file 'nothing.py'" in capsys.readouterr().err
