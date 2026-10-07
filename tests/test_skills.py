"""The skills that ship with Leyline: each is valid, the wheel carries the one copy kept in skills/, and
`leyline skills install` puts them in a repository without overwriting a copy someone edited."""

import importlib.util
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from leyline import agent_skills, cli

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SHIPPED = {"leyline-ask", "leyline-quick-change", "leyline-pr-review", "leyline-spec", "leyline-adversarial-review",
           "leyline-change-impact", "leyline-tour"}
# Being written on other branches at the same time as these; once merged, every name below must exist.
PARALLEL = {"leyline-explain-flow", "leyline-explore-module"}


def test_every_skill_has_valid_frontmatter():
    found = agent_skills.available()
    assert SHIPPED <= {s.name for s in found}
    assert {s.folder.name for s in found} == {p.parent.name for p in SKILLS.glob("*/SKILL.md")}
    for s in found:
        assert agent_skills.problems(s) == [], (s.name, agent_skills.problems(s))


def test_frontmatter_problems_are_named(tmp_path):
    bad = tmp_path / "Bad_Name"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: uses <b>markup</b>\n---\nno heading\n")
    found = agent_skills.problems(agent_skills.read(bad))
    assert any("hyphens" in p for p in found) and any("markup" in p for p in found) and any("heading" in p for p in found)
    (bad / "SKILL.md").write_text("# No frontmatter\n")
    assert "frontmatter" in agent_skills.problems(agent_skills.read(bad))[0]


def test_skills_name_only_skills_that_exist():
    names = {s.name for s in agent_skills.available()}
    for s in agent_skills.available():
        for ref in set(re.findall(r"\bleyline-[a-z]+(?:-[a-z]+)*\b(?!\.\w)", s.text)) - {"leyline-code"}:
            assert ref in names or (ref in PARALLEL and not (SKILLS / ref).exists()), f"{s.name} names {ref}"


def test_new_skills_give_the_command_for_each_tool():
    for name in ("leyline-ask", "leyline-quick-change", "leyline-pr-review"):
        assert "| MCP tool | Command |" in agent_skills.find(name).text, name


# -- one copy, in skills/; the wheel carries it --------------------------------------------------
def test_a_checkout_reads_the_top_level_folder_and_any_copy_in_the_package_matches_it():
    assert agent_skills.skills_dir() in (SKILLS, ROOT / "src/leyline/skills")
    copy = ROOT / "src/leyline/skills"
    if copy.exists():   # nobody should keep a second copy by hand; if one appears, it must not drift
        files = lambda d: {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*") if p.is_file()}
        assert files(copy) == files(SKILLS)
    text = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'\[tool\.hatch\.build\.targets\.wheel\.force-include\]\s*\n(#.*\n)*"skills" = "leyline/skills"', text)


@pytest.mark.skipif(importlib.util.find_spec("hatchling") is None, reason="hatchling is not installed")
def test_the_wheel_carries_every_skill_as_it_is_in_skills(tmp_path):
    out = subprocess.run([sys.executable, "-c", "import sys; from hatchling.build import build_wheel;"
                          " print(build_wheel(sys.argv[1]))", str(tmp_path)], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    wheel = tmp_path / out.stdout.strip().splitlines()[-1]
    with zipfile.ZipFile(wheel) as z:
        inside = {n[len("leyline/skills/"):]: z.read(n) for n in z.namelist() if n.startswith("leyline/skills/")}
    assert inside == {p.relative_to(SKILLS).as_posix(): p.read_bytes() for p in SKILLS.rglob("*") if p.is_file()}
    assert "leyline/agent_skills.py" in zipfile.ZipFile(wheel).namelist()


# -- installing ----------------------------------------------------------------------------------
@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".git").mkdir(parents=True)
    return r


def states(result) -> dict:
    return {t["folder"]: {x["skill"]: x["state"] for x in t["skills"]} for t in result["targets"]}


def test_install_into_claude_by_default_then_leave_an_edited_copy_alone(repo):
    r = agent_skills.install(repo)
    assert set(states(r)) == {".claude/skills"} and set(states(r)[".claude/skills"].values()) == {"installed"}
    ask = repo / ".claude/skills/leyline-ask/SKILL.md"
    assert ask.read_text() == (SKILLS / "leyline-ask/SKILL.md").read_text()
    manifest = json.loads((repo / ".claude/skills" / agent_skills.MANIFEST).read_text())
    assert set(manifest["skills"]) >= SHIPPED and "SKILL.md" in manifest["skills"]["leyline-ask"]["files"]

    assert set(states(agent_skills.install(repo))[".claude/skills"].values()) == {"unchanged"}

    ask.write_text(ask.read_text() + "\nOur own rule: always cite the file.\n")
    again = agent_skills.install(repo)
    kept = next(x for x in again["targets"][0]["skills"] if x["skill"] == "leyline-ask")
    assert kept["state"] == "kept" and "SKILL.md" in kept["why"] and "--force" in kept["why"]
    assert "Our own rule" in ask.read_text()
    assert "Our own rule" in ask.read_text() and states(agent_skills.install(repo))[".claude/skills"]["leyline-ask"] == "kept"

    forced = agent_skills.install(repo, force=True)
    assert states(forced)[".claude/skills"]["leyline-ask"] == "replaced"
    assert ask.read_text() == (SKILLS / "leyline-ask/SKILL.md").read_text()
    assert states(agent_skills.install(repo))[".claude/skills"]["leyline-ask"] == "unchanged"


def test_a_copy_leyline_did_not_install_is_kept(repo):
    mine = repo / ".claude/skills/leyline-tour"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("my own tour skill\n")
    r = agent_skills.install(repo, names=["tour"])
    assert r["targets"][0]["skills"] == [{"skill": "leyline-tour", "state": "kept",
                                          "why": "a different copy is here that Leyline did not install; --force replaces it"}]
    assert (mine / "SKILL.md").read_text() == "my own tour skill\n"


def test_a_new_version_updates_an_unedited_copy(repo, tmp_path, monkeypatch):
    src = tmp_path / "shipped"
    (src / "leyline-x").mkdir(parents=True)
    (src / "leyline-x/SKILL.md").write_text("---\nname: leyline-x\ndescription: one\n---\n# X\n")
    (src / "leyline-x/old.md").write_text("an old reference\n")
    monkeypatch.setattr(agent_skills, "skills_dir", lambda: src)
    assert states(agent_skills.install(repo))[".claude/skills"] == {"leyline-x": "installed"}
    (src / "leyline-x/SKILL.md").write_text("---\nname: leyline-x\ndescription: two\n---\n# X, better\n")
    (src / "leyline-x/old.md").unlink()
    assert states(agent_skills.install(repo))[".claude/skills"] == {"leyline-x": "updated"}
    dest = repo / ".claude/skills/leyline-x"
    assert "better" in (dest / "SKILL.md").read_text() and not (dest / "old.md").exists()


def test_both_folders_and_a_link_between_them_written_once(repo):
    (repo / ".agents").mkdir()
    r = agent_skills.install(repo)
    assert set(states(r)) == {".claude/skills", ".agents/skills"}
    assert (repo / ".agents/skills/leyline-pr-review/SKILL.md").is_file()

    linked = repo.parent / "linked"
    (linked / ".git").mkdir(parents=True)
    (linked / ".agents/skills").mkdir(parents=True)
    (linked / ".claude").mkdir()
    (linked / ".claude/skills").symlink_to("../.agents/skills")
    r = agent_skills.install(linked)
    assert [(t["folder"], t["also"]) for t in r["targets"]] == [(".agents/skills", [".claude/skills"])]
    assert set(states(r)[".agents/skills"].values()) == {"installed"}

    only = agent_skills.install(repo.parent / "linked", which="agents")
    assert set(states(only)) == {".agents/skills"}


def test_the_skills_command(repo, capsys, monkeypatch):
    assert cli.main(["skills", "list"]) == 0
    out = capsys.readouterr().out
    assert "leyline-ask\n  Answer a question" in out and "leyline skills install" in out

    assert cli.main(["skills", "show", "quick-change"]) == 0
    assert capsys.readouterr().out == (SKILLS / "leyline-quick-change/SKILL.md").read_text()
    assert cli.main(["skills", "show", "nope"]) == 2
    assert "no skill named nope" in capsys.readouterr().err

    monkeypatch.chdir(repo)
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")   # the repository around the current directory
    assert cli.main(["skills", "install"]) == 0
    out = capsys.readouterr().out
    assert "installed   leyline-ask" in out and "claude mcp add leyline -- leyline serve" in out
    assert (repo / ".claude/skills/leyline-ask/SKILL.md").is_file() and not (repo / "sub/.claude").exists()

    (repo / ".claude/skills/leyline-ask/SKILL.md").write_text("edited\n")
    assert cli.main(["skills", "install", "--to", str(repo), "--claude"]) == 0
    out = capsys.readouterr().out
    assert "kept        leyline-ask: edited here since it was installed (SKILL.md)" in out and "--force" in out
    assert cli.main(["skills", "install", "--to", str(repo), "--force", "ask"]) == 0
    assert "replaced    leyline-ask" in capsys.readouterr().out
    assert cli.main(["skills", "install", "--to", str(repo / "nowhere")]) == 2
    assert cli.main(["skills", "install", "--to", str(repo), "nope"]) == 2
