"""Change coupling by function: for the functions a plan names or a pull request edits, the functions that changed in
most of the same commits, read from each commit's diff against that commit's own version of the file."""

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from leyline import cli, coupling, fncoupling, spec, store
from leyline.adapters import csharp, python, typescript
from leyline.indexer import index


def git(root, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


def fn(name: str, body: str) -> str:
    return f"def {name}(x):\n    {body}\n"


def rules(v: dict) -> str:
    """src/rules.py at one point in time: `check` with a function nested in it, `helper`, and padding between them, so
    a change to one never touches the lines of another."""
    pad = "\n\n".join(f"# {k}" for k in range(3))
    return (f"def check(x):\n    def inner(y):\n        return y + {v['inner']}\n    return inner(x) + {v['check']}\n\n\n"
            f"{pad}\n\n\n" + fn("helper", f"return x * {v['helper']}") + f"\n\n{pad}\n\n\n" + fn("other", f"return {v['other']}"))


def reader_py(v: dict) -> str:
    return fn("read", f"return {v['read']}") + "\n\n# --\n\n\n" + fn("write", f"return {v['write']}")


@pytest.fixture
def repo(tmp_path):
    """check and read are made together and change together in 4 commits; check alone in 1 more; inner (nested in check) alone in 3;
    helper and write made together and changed together once more (2: too few to report); other alone in 3."""
    return make_repo(tmp_path, "reader.py")


def make_repo(tmp_path, reader: str):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    v = {"check": 0, "inner": 0, "helper": 0, "other": 0, "read": 0, "write": 0}

    def step(msg, *keys):
        for k in keys:
            v[k] += 1
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "rules.py").write_text(rules(v))
        (root / "src" / reader).write_text(reader_py(v))
        git(root, "add", "-A")
        git(root, "commit", "-qm", msg)
    step("start")
    for i in range(4):
        step(f"rule {i}", "check", "read")
    step("check alone", "check")
    for i in range(3):
        step(f"inner {i}", "inner")
    step("helper", "helper", "write")
    for i in range(3):
        step(f"other {i}", "other")
    db = tmp_path / "s.db"
    index(root, db, "r")
    return root, db


def ids(con, *names):
    return [con.execute("SELECT id FROM nodes WHERE name = ? AND kind = 'callable' ORDER BY length(id)", (n,)).fetchone()[0]
            for n in names]


def test_a_function_couples_to_the_function_that_changed_with_it_and_to_nothing_else(repo):
    root, db = repo
    con = store.connect(db)
    try:
        check, read, inner, helper, other = ids(con, "check", "read", "inner", "helper", "other")
        sha = coupling.head(root)
        r = fncoupling.compute(con, "r", root, sha, [check])
        assert [(x["partner_id"], x["together"], x["changes"]) for x in r["functions"]] == [(read, 5, 6)]
        # inner changed alone three times: those are not changes of check, though git's line range holds them
        assert r["functions"][0]["changes"] == 6
        r = fncoupling.compute(con, "r", root, sha, [read])
        assert [(x["partner_id"], x["together"], x["changes"]) for x in r["functions"]] == [(check, 5, 5)]
        assert fncoupling.compute(con, "r", root, sha, [helper])["functions"] == []    # 2 together: too few
        assert fncoupling.compute(con, "r", root, sha, [other])["functions"] == []
        assert fncoupling.compute(con, "r", root, sha, [inner])["functions"] == []
        # read once per file and commit, then kept
        again = fncoupling.compute(con, "r", root, sha, [check, read])
        assert again["parsed"] == 0
        m = fncoupling.missed(con, "r", root, sha, [check], lambda g: False)
        assert fncoupling.line(m["functions"][0]) == "`reader.py.read` changed in 5 of the 6 commits that changed `rules.py.check`"
        assert fncoupling.missed(con, "r", root, sha, [check], lambda g: g == read)["functions"] == []
    finally:
        con.close()


def test_a_file_whose_name_has_a_space_is_read_like_any_other(tmp_path):
    """git ends a `+++ b/<path>` line with a tab when the path has a space in it: the hunks must still be the file's."""
    root, db = make_repo(tmp_path, "my reader.py")
    con = store.connect(db)
    try:
        check, read = ids(con, "check", "read")
        sha = coupling.head(root)
        assert [(x["partner_id"], x["together"]) for x in fncoupling.compute(con, "r", root, sha, [check])["functions"]] == [(read, 5)]
        assert [(x["partner_id"], x["together"]) for x in fncoupling.compute(con, "r", root, sha, [read])["functions"]] == [(check, 5)]
    finally:
        con.close()


def spec_folder(root: Path, tasks: str) -> Path:
    ch = root / "openspec" / "changes" / "strict"
    (ch / "specs" / "rules").mkdir(parents=True, exist_ok=True)
    (ch / "proposal.md").write_text("# Change: Strict rules\n\n## Why\nRules pass too easily.\n")
    (ch / "tasks.md").write_text(tasks)
    (ch / "specs" / "rules" / "spec.md").write_text(
        "## MODIFIED Requirements\n### Requirement: Rules\nA rule SHALL fail without ok.\n\n"
        "#### Scenario: No ok\n- **WHEN** a rule has no ok key\n- **THEN** it fails\n")
    return ch


def test_a_plan_names_the_function_that_usually_changes_with_a_task_and_no_task_names(repo):
    root, db = repo
    ch = spec_folder(root, "- [ ] 1.1 Change `check` to fail a rule with no ok key\n")
    con = store.connect(db)
    try:
        b = spec.brief(con, ch)
        assert [x["partner"] for x in b["usually_changes_with"]["functions"]] == ["reader.py.read"]
        page = (ch / "leyline.md").read_text()
        assert "**Usually changes with the functions the tasks name, and no task names it.**" in page
        assert "- `reader.py.read` changed in 5 of the 6 commits that changed `rules.py.check`; no task names it." in page
        spec_folder(root, "- [ ] 1.1 Change `check` to fail a rule with no ok key\n- [ ] 1.2 Change `read` to match\n")
        b = spec.brief(con, ch)
        assert b["usually_changes_with"]["functions"] == []
    finally:
        con.close()


def test_a_pull_request_names_the_function_that_usually_changed_with_what_it_edited(repo, monkeypatch):
    root, _ = repo
    git(root, "checkout", "-q", "-b", "feature")
    text = (root / "src" / "rules.py").read_text().replace("return inner(x) + 5", "return inner(x) + 50")
    assert "+ 50" in text
    (root / "src" / "rules.py").write_text(text)
    git(root, "commit", "-qam", "Stricter")
    monkeypatch.chdir(root)
    out = io.StringIO()
    with redirect_stdout(out):
        assert cli.main(["pr", "main"]) == 0
    page = out.getvalue()
    assert "Functions that usually changed with the functions it edited, and it did not change:" in page
    assert "- `reader.py.read` changed in 5 of the 6 commits that changed `rules.py.check`." in page


FIXTURES = Path(__file__).parent


@pytest.mark.parametrize("adapter,path", [
    (python, "fixture2/py/src/pkg/core.py"),
    (typescript, "fixture5/ts/nest.ts"),
    (csharp, "fixture2/cs/Mod/Runner.cs"),
])
def test_the_quick_parse_finds_the_functions_the_full_parse_finds(adapter, path):
    """The parse leaves out the adapter's channel pass, which reads the functions and adds none."""
    full = FIXTURES / path
    if not full.exists():
        pytest.skip(f"no {path}")
    data = full.read_bytes()
    quick = fncoupling._nodes(adapter, "r", path, data, ".")
    whole = adapter.parse("r", path, f"r:file:{path}", data, ".")
    spans = lambda res: sorted((n.id, n.kind, n.span_start, n.span_end) for n in res.nodes)
    assert spans(quick) == spans(whole) and any(n.kind == "callable" for n in whole.nodes)
