"""What the loop got wrong on a real feature (a rehearsal on Parlance), each pinned on a small repository: impact
through nested functions, imports through sys.path, the left-alone list, the CLI's impact and spec finding, search
ranking, names that read the same, test titles that are templates, map refusing a path that is not a directory,
and long builder chains."""

import shutil
import time
from pathlib import Path

from leyline import change, query, spec, store
from leyline.cli import main
from leyline.indexer import index
from store_identity import differences

HERE = Path(__file__).parent


def write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


NESTED = {
    # The change is inside a closure that only its sibling closures call, three hops down inside one function.
    "web/src/local.ts": (
        "export function validateEntity(e: any): string[] {\n"
        "  const out: string[] = [];\n"
        "  const walkEffect = (x: any) => { if (x.delta === 0) out.push('zero'); };\n"
        "  const walkEffects = (xs: any[]) => { for (const x of xs) walkEffect(x); };\n"
        "  const validateNode = (n: any) => { walkEffects(n.effects); };\n"
        "  const validateDialogue = (d: any) => { for (const n of d.nodes) validateNode(n); };\n"
        "  validateDialogue(e);\n"
        "  return out;\n"
        "}\n"),
    "web/src/validator.ts": (
        'import { validateEntity } from "./local";\n\n'
        "export function validate(p: any): string[] {\n  return validateEntity(p);\n}\n"),
    "web/src/host.ts": (
        'import { validate } from "./validator";\n\n'
        "export function save(p: any): string[] {\n  return validate(p);\n}\n"),
    "py/checks.py": (
        "def outer(items):\n"
        "    def inner(x):\n"
        "        return x + 1\n"
        "    return [inner(i) for i in items]\n\n\n"
        "def run():\n"
        "    return outer([1, 2])\n"),
}


def test_a_change_inside_a_nested_function_reaches_the_callers_of_the_function_around_it(tmp_path):
    root = write(tmp_path / "repo", NESTED)
    db = tmp_path / "s.db"
    index(root, db, "n")
    c = store.connect(db)
    walk = "n:typescript:web.src.local.validateEntity.walkEffect"
    assert c.execute("SELECT 1 FROM nodes WHERE id = ?", (walk,)).fetchone()
    r = change.assess(c, "warn on a zero delta", [{"id": walk, "action": "behavior"}])
    marks = {m["id"].split(":", 2)[-1]: m for m in r["marks"]}
    # The function it is defined in, and that function's callers, however many closures sit in between.
    assert "web.src.local.validateEntity" in marks and "defines walkEffect" in marks["web.src.local.validateEntity"]["note"]
    assert "web.src.validator.validate" in marks and "web.src.host.save" in marks
    assert marks["web.src.host.save"]["role"] == "indirect"
    # The same for Python, and for the read-only impact query.
    inner = "n:python:py.checks.outer.inner"
    r = change.assess(c, "x", [{"id": inner, "action": "behavior"}])
    assert {"n:python:py.checks.outer", "n:python:py.checks.run"} <= {m["id"] for m in r["marks"]}
    reached = query.impact(c, walk)
    assert reached["reached_by"] >= 3
    assert any("host" in m["module"] or "web" in m["module"] for m in reached["by_module"])
    c.close()


SYS_PATH = {
    "tooling/validate.py": "def validate_project(p):\n    return walk_effect(p)\n\n\ndef walk_effect(e):\n    return e\n",
    "tooling/tests/test_validate.py": (
        "import sys\nfrom pathlib import Path\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n\n"
        "import validate  # noqa: E402\n\n\n"
        "def test_zero():\n    assert validate.validate_project({}) == {}\n"),
    # No sys.path change here: a bare `import validate` stays unresolved.
    "other/test_plain.py": "import validate\n\n\ndef test_plain():\n    assert validate.validate_project({}) == {}\n",
}


def test_an_import_through_sys_path_resolves_to_the_one_module_of_that_name(tmp_path):
    root = write(tmp_path / "repo", SYS_PATH)
    db = tmp_path / "s.db"
    index(root, db, "sp")
    c = store.connect(db)
    edge = c.execute("SELECT precision, attrs FROM edges WHERE kind = 'imports' AND src_id = ? AND dst_id = ?",
                     ("sp:file:tooling/tests/test_validate.py", "sp:file:tooling/validate.py")).fetchone()
    assert edge is not None and edge["precision"] == "heuristic" and '"found_by": "sys.path"' in edge["attrs"]
    calls = {(r[0].split(":", 2)[-1], r[1].split(":", 2)[-1]) for r in c.execute("SELECT src_id, dst_id FROM calls")}
    assert any(s.endswith("test_zero") and d == "tooling.validate.validate_project" for s, d in calls)
    assert not any("test_plain" in s and d.startswith("tooling.validate") for s, d in calls)
    # So the changed function is on a test's path, and the plan says no untested risk.
    r = change.assess(c, "x", [{"id": "sp:python:tooling.validate.walk_effect", "action": "behavior"}])
    assert r["untested"] == [] and r["tests_to_run"]
    c.close()


def test_sys_path_imports_stay_identical_when_mapped_incrementally(tmp_path):
    root = write(tmp_path / "repo", {**SYS_PATH, "tooling/tests/test_more.py": (
        "import validate\n\n\ndef test_more():\n    assert validate.walk_effect(1) == 1\n")})
    inc = tmp_path / "inc.db"
    index(root, inc, "sp")
    # A conftest.py that puts the directory on the path changes how a file that did not change resolves.
    (root / "tooling/tests/conftest.py").write_text("import sys\nsys.path.insert(0, '..')\n")
    assert index(root, inc, "sp")["incremental"]["mode"] == "incremental"
    full = tmp_path / "full.db"
    index(root, full, "sp", full=True)
    assert differences(inc, full) == {}
    c = store.connect(inc)
    assert c.execute("SELECT 1 FROM calls WHERE src_id LIKE '%test_more%' AND dst_id = 'sp:python:tooling.validate.walk_effect'"
                     ).fetchone()
    c.close()


BIG = "".join(f"        self.f{i} = 0\n" for i in range(14))
LEFT_ALONE = {
    "v/validator.py": (
        "class Validator:\n"
        "    def __init__(self):\n" + BIG +
        "        self.xp_grants = []\n        self.delta_seen = []\n        self.cutscenes = []\n\n"
        "    def walk_effect(self, e):\n"
        "        self.xp_grants.append(e)\n        self.delta_seen.append(e)\n        self.cutscenes.append(e)\n\n"
        "    def check_progression(self):\n        return len(self.xp_grants)\n\n"
        "    def check_delta(self):\n        return len(self.delta_seen)\n\n"
        "    def check_cutscenes(self):\n        return len(self.cutscenes)\n\n"
        "    def dump(self):\n        return [" + ", ".join(f"self.f{i}" for i in range(14)) + ", self.cutscenes]\n"),
}


def test_left_alone_lists_only_state_close_to_what_the_tasks_change(tmp_path):
    root = write(tmp_path / "repo", LEFT_ALONE)
    ch = root / "openspec" / "changes" / "zero-delta"
    (ch / "specs" / "v").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Zero delta\n\n## Why\nA zero delta does nothing.\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `Validator.walk_effect` to warn on an adjust effect whose delta is 0\n")
    (ch / "specs" / "v" / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Warn\nIt SHALL warn.\n\n"
        "#### Scenario: Zero\n- **WHEN** a delta is 0\n- **THEN** it warns\n")
    db = tmp_path / "s.db"
    index(root, db, "la")
    c = store.connect(db)
    b = spec.brief(c, ch)
    state = b["left_alone"]["state"]
    shown = [x["field"] for x in state if x["own"]]
    # delta_seen shares a word with the task; xp_grants does not, on a type of 17 fields; cutscenes is also read by
    # dump, a reader of every field, but check_cutscenes is a plain reader, so it stays and is only quiet.
    assert shown == ["Validator.delta_seen"]
    assert state[0]["field"] == "Validator.delta_seen" and state[0]["close"]
    quiet = {x["field"]: x["quiet"] for x in state if x.get("quiet")}
    assert set(quiet) == {"Validator.xp_grants", "Validator.cutscenes"} and "17 fields" in quiet["Validator.xp_grants"]
    assert b["left_alone"]["left_out"]["count"] == 2
    page = (ch / "leyline.md").read_text()
    assert "Validator.delta_seen (used by Validator.walk_effect) is also used by Validator.check_delta" in page
    assert "xp_grants" not in page.split("## 3.")[0]
    c.close()


def test_a_field_only_a_reader_of_every_field_shares_is_left_out():
    names = type("N", (), {})()
    names.by_id = {"t": {"name": "T", "kind": "type", "parent_id": None}, "fn": {"name": "walk_effect", "kind": "callable",
                                                                               "parent_id": "t"}}
    links = [{"text": "Change `T.walk_effect` to warn on a zero adjust delta", "nodes": ["fn"]}]
    words = spec._task_words(names, links, "fn")
    assert {"zero", "adjust", "delta", "warn"} <= words and not {"walk", "effect", "change"} & words


def test_impact_and_spec_finding_from_the_command_line(tmp_path, capsys):
    root = write(tmp_path / "repo", NESTED)
    db = str(tmp_path / "s.db")
    index(root, db, "n")
    assert main(["--db", db, "impact", "validateEntity.walkEffect"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("n:typescript:web.src.local.validateEntity.walkEffect") and "reached by" in out
    assert main(["--db", db, "impact", "web/src/host.ts"]) == 0
    assert main(["--db", db, "impact", "nothing_like_this"]) == 1
    assert "nothing on the map" in capsys.readouterr().err

    ch = root / "openspec" / "changes" / "c1"
    ch.mkdir(parents=True)
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `validateEntity` to warn\n")
    c = store.connect(db)
    spec.brief(c, ch)
    c.close()
    ev = ["--evidence", "n:typescript:web.src.host.save"]
    assert main(["--db", db, "spec", "finding", str(ch), "--reviewer", "logic", "--severity", "low",
                 "--claim", "save is reached", *ev]) == 0
    assert main(["--db", db, "spec", "file", str(ch), "--reviewer", "logic", "--severity", "low",
                 "--claim", "the older name still files one", *ev]) == 0
    c = store.connect(db)
    assert len(spec.findings(c, "spec-c1")["findings"]) == 2
    c.close()


def test_search_puts_what_the_text_names_exactly_first(tmp_path):
    files = {f"src/{n}.ts": f"export function {n}Thing(): number {{ return 1; }}\n"
             for n in ("localization", "localeCatalog", "localStore", "localCache")}
    files["src/validation/local.ts"] = "export function check(): number { return 2; }\n"
    root = write(tmp_path / "repo", files)
    db = tmp_path / "s.db"
    index(root, db, "s")
    c = store.connect(db)
    assert query.search(c, "local.ts")["results"][0]["id"] == "s:file:src/validation/local.ts"
    assert query.search(c, "src/validation/local.ts")["results"][0]["id"] == "s:file:src/validation/local.ts"
    assert query.search(c, "check")["results"][0]["name"] == "check"
    c.close()


def test_fields_that_read_the_same_name_their_file(tmp_path):
    builder = ("class Builder:\n    def __init__(self):\n        self.notes = []\n\n"
               "def use(b: Builder):\n    b.notes = [1]\n")
    root = write(tmp_path / "repo", {"a/build_ink.py": builder, "a/build_yarn.py": builder})
    db = tmp_path / "s.db"
    index(root, db, "d")
    c = store.connect(db)
    names = sorted(f["name"] for f in query.shared_state(c)["fields"])
    assert names == ["Builder.notes (a/build_ink.py)", "Builder.notes (a/build_yarn.py)"]
    c.close()


def test_a_test_title_that_is_a_template_is_shown_after_its_suite(tmp_path):
    root = write(tmp_path / "repo", {"t/rename.test.ts": (
        'import { describe, it, expect } from "vitest";\n\n'
        'describe("rename sweep", () => {\n'
        '  it.each([["a", "b"]])("%s/%s", (x: string, y: string) => { expect(x).not.toBe(y); });\n'
        '  it("plain title", () => { expect(1).toBe(1); });\n'
        "});\n")})
    db = tmp_path / "s.db"
    index(root, db, "tt")
    c = store.connect(db)
    flows = {r[0] for r in c.execute("SELECT name FROM flows")}
    assert "rename sweep > …/…" in flows and "plain title" in flows
    # The test itself keeps its own name, so a scenario or a test run still finds it.
    assert c.execute("SELECT 1 FROM nodes WHERE kind = 'test' AND name = '%s/%s'").fetchone()
    c.close()


def test_the_map_page_counts_functions_as_the_command_line_does(tmp_path):
    from leyline import export, loop
    shutil.copytree(HERE / "fixture2", tmp_path / "f3")
    db = tmp_path / "s.db"
    index(tmp_path / "f3", db, "f3")
    c = store.connect(db)
    g = export.graph(c, False)
    marked = [n for n in g["nodes"] if n["k"] == "callable" and (n.get("x") or {}).get("is_test")]
    unmarked = [n for n in g["nodes"] if n["k"] == "callable" and not (n.get("x") or {}).get("is_test")]
    assert marked and len(unmarked) == loop.counts(c)["functions"]   # pytest functions are callables marked as tests
    c.close()


def test_map_refuses_a_path_that_is_not_a_directory(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "one.py").write_text("x = 1\n")
    assert main(["map", str(tmp_path / "nope")]) == 2
    assert "does not exist" in capsys.readouterr().err
    assert main(["map", str(tmp_path / "one.py")]) == 2
    assert "is a file" in capsys.readouterr().err
    assert main(["index", str(tmp_path / "nope")]) == 2
    assert not (tmp_path / ".leyline").exists() and not (tmp_path / "nope").exists()


def test_mapping_a_repository_again_keeps_the_id_it_was_mapped_under(tmp_path, monkeypatch):
    root = write(tmp_path / "checkout", {"a.py": "def f():\n    return 1\n"})
    db = root / ".leyline" / "leyline.db"
    assert main(["map", str(root), "--repo", "parlance", "--exact", "off"]) == 0
    monkeypatch.chdir(root)
    assert main(["map", ".", "--exact", "off"]) == 0
    c = store.connect(db)
    assert [r[0] for r in c.execute("SELECT id FROM nodes WHERE kind = 'repo'")] == ["parlance"]
    c.close()


def test_a_long_builder_chain_is_mapped_in_linear_time(tmp_path):
    """a.m0().m1()...: reading the chain so far again at every link made 3000 links take 14 s."""
    def run(n: int) -> float:
        root = write(tmp_path / f"c{n}", {"chain.ts": "export function f(a: any) {\n  return a"
                                          + "".join(f"\n    .m{i}()" for i in range(n)) + ";\n}\n"})
        began = time.perf_counter()
        index(root, tmp_path / f"c{n}.db", "c", exact="off")
        return time.perf_counter() - began
    run(200)   # warm up the parsers
    small, large = run(1000), run(4000)
    assert large < 8 * small + 1.0, (small, large)   # quadratic would be about 16 times


def test_a_typescript_import_inside_a_dot_directory(tmp_path):
    """Files under a directory whose name starts with a dot (.storybook, .vitepress) import each other like any other."""
    root = write(tmp_path / "dots", {
        ".storybook/helper.ts": "export function helper(): number {\n  return 1;\n}\n",
        ".storybook/main.ts": 'import { helper } from "./helper";\n\nexport function main(): number {\n  return helper();\n}\n',
    })
    db = tmp_path / "d.db"
    index(root, db, "d")
    con = store.connect(db)
    assert {r[0] for r in con.execute("SELECT dst_id FROM edges WHERE kind = 'imports' AND src_id = 'd:file:.storybook/main.ts'")} \
        == {"d:file:.storybook/helper.ts"}
    assert {r[0] for r in con.execute("SELECT dst_id FROM calls WHERE src_id LIKE 'd:typescript:%main.main'")} \
        == {"d:typescript:.storybook.helper.helper"}
    con.close()
