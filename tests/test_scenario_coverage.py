"""Scenarios proven by execution, not only by name, and the tests a change needs, from per-test coverage.

The coverage files here are written in the formats the tools write (coverage.py's SQLite data file with
`--cov-context=test` contexts, Istanbul's coverage-final.json), so the tests need neither tool installed."""

import io
import json
import sqlite3
from contextlib import redirect_stdout
from pathlib import Path

from leyline import cli, coverage, loop, pr, store
from leyline import affected

OPS = "def add(a, b):\n    return a + b\n\n\ndef scale(x, k):\n    return x * k\n\n\ndef describe(x):\n    return f\"value {x}\"\n"
TESTS = ("from calc.ops import add, describe, scale\n\n\ndef test_add_two_numbers():\n    assert add(2, 3) == 5\n\n\n"
         "def test_scale_by_a_factor():\n    assert scale(2, 3) == 6\n\n\ndef test_describe_a_value():\n"
         "    assert describe(1) == \"value 1\"\n")
WEAK = "\n\ndef test_negative_factor_gives_zero():\n    assert 2 * max(-3, 0) == 0   # never calls scale\n"


def run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


def write(root: Path, files: dict) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def line(path: Path, text: str) -> int:
    return next(i for i, s in enumerate(path.read_text().splitlines(), 1) if text in s)


def coverage_db(path: Path, measured: dict) -> Path:
    """A coverage.py data file: {source file: {pytest context: [lines]}}, as `--cov-context=test` records it."""
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE file (id INTEGER PRIMARY KEY, path TEXT); CREATE TABLE context (id INTEGER PRIMARY KEY,"
                     " context TEXT); CREATE TABLE line_bits (file_id INTEGER, context_id INTEGER, numbits BLOB);"
                     " CREATE TABLE arc (file_id INTEGER, context_id INTEGER, fromno INTEGER, tono INTEGER);")
    contexts: dict = {}
    bits = lambda ls: bytes(sum(1 << (n % 8) for n in ls if n // 8 == i) for i in range(max(ls) // 8 + 1))
    for fid, (src, by_test) in enumerate(measured.items(), 1):
        db.execute("INSERT INTO file VALUES (?, ?)", (fid, str(src)))
        for ctx, lines in by_test.items():
            cid = contexts.setdefault(ctx, len(contexts) + 1)
            db.execute("INSERT OR IGNORE INTO context VALUES (?, ?)", (cid, ctx))
            db.execute("INSERT INTO line_bits VALUES (?, ?, ?)", (fid, cid, bits(lines)))
    db.commit()
    db.close()
    return path


def change(root: Path) -> Path:
    ch = root / "openspec/changes/clamp-scale"
    write(ch, {
        "proposal.md": "# Change: Clamp scale\n\n## Why\nA negative factor flips the sign.\n\n## What Changes\n- `scale` clamps\n",
        "tasks.md": "- [ ] 1.1 Change `scale` to treat a negative factor as zero\n"
                    "- [ ] 1.2 Add the test \"Negative factor gives zero\"\n",
        "specs/calc/spec.md": "## MODIFIED Requirements\n### Requirement: Scaling\nScaling SHALL keep the sign.\n\n"
                              "#### Scenario: Scale by a factor\n- **WHEN** scaled by 3\n- **THEN** multiplied by 3\n\n"
                              "#### Scenario: Negative factor gives zero\n- **WHEN** scaled by -3\n- **THEN** zero\n"})
    return ch


def results(*names) -> list[dict]:
    return [{"name": f"tests/test_ops.py::{n}", "status": "pass"} for n in names]


def planned(tmp_path) -> tuple[Path, Path, Path]:
    """A mapped repository with the change planned, then implemented with a test that never calls `scale`."""
    root = tmp_path / "calc"
    write(root, {"src/calc/__init__.py": "", "src/calc/ops.py": OPS, "tests/test_ops.py": TESTS})
    ch = change(root)
    db = root / ".leyline/leyline.db"
    loop.map_repos([str(root)], db, exact="off", page=False)
    b = loop.plan(db, ch, results("test_add_two_numbers", "test_scale_by_a_factor", "test_describe_a_value"))
    assert "error" not in b, b
    ops = root / "src/calc/ops.py"
    ops.write_text(OPS.replace("return x * k", "return x * max(k, 0)"))
    (root / "tests/test_ops.py").write_text(TESTS + WEAK)
    return root, ch, db


def measured_after(root: Path, path: Path, weak_lines: bool = False) -> Path:
    ops, tests = root / "src/calc/ops.py", root / "tests/test_ops.py"
    m = {ops: {"tests/test_ops.py::test_scale_by_a_factor|run": [line(ops, "max(k, 0)")],
               "tests/test_ops.py::test_add_two_numbers|run": [line(ops, "return a + b")],
               "tests/test_ops.py::test_describe_a_value|run": [line(ops, "value {x}")]}}
    if weak_lines:   # `--cov=.`: the test's own body is measured too
        m[tests] = {"tests/test_ops.py::test_negative_factor_gives_zero|run": [line(tests, "max(-3, 0)")]}
    return coverage_db(path, m)


AFTER = ("test_add_two_numbers", "test_scale_by_a_factor", "test_negative_factor_gives_zero", "test_describe_a_value")


def by_name(v: dict) -> dict:
    return {s["name"]: s for s in v["scenarios"]}


def test_a_scenario_test_that_ran_the_changed_code_and_one_that_did_not(tmp_path):
    root, ch, db = planned(tmp_path)
    v = loop.check(db, ch, results(*AFTER), measured_after(root, tmp_path / ".coverage"))
    s = by_name(v)
    assert s["Scale by a factor"]["ran_changed_code"] is True
    assert s["Scale by a factor"]["ran_changed_code_note"] == "ran the changed code: scale"
    # Measured on the same run, and no record of it running anything: it passed without running the change.
    weak = s["Negative factor gives zero"]
    assert weak["state"] == "passes" and weak["ran_changed_code"] is False
    assert weak["ran_changed_code_note"].startswith("passed without running the changed code")
    page = (ch / "leyline.md").read_text()
    assert "| Negative factor gives zero | passes | needs a person | passed without running the changed code |" in page
    assert "| Scale by a factor | passes | proven | measured running the changed code |" in page
    assert weak["verdict"] == "needs a person" and "never ran the changed code" in weak["verdict_why"]
    assert "never ran the changed code" in page.split("**Not proven, and why:**")[1]
    assert "\"Negative factor gives zero\" needs a person" in loop.next_after_check(v, "clamp-scale")[0]


def test_a_test_measured_running_only_itself_did_not_run_the_change(tmp_path):
    root, ch, db = planned(tmp_path)
    loop.refresh(db)   # imported on its own after a map of the new code, not with check: still a record of the test
    coverage.import_file(store.connect(db), measured_after(root, tmp_path / ".coverage", weak_lines=True))
    v = loop.check(db, ch, results(*AFTER))
    weak = by_name(v)["Negative factor gives zero"]
    assert weak["ran_changed_code"] is False and "(scale)" in weak["ran_changed_code_note"]


def test_no_record_of_a_test_from_another_run_is_not_called_a_weak_proof(tmp_path):
    root, ch, db = planned(tmp_path)
    loop.refresh(db)
    coverage.import_file(store.connect(db), measured_after(root, tmp_path / ".coverage"))
    v = loop.check(db, ch, results(*AFTER))
    weak = by_name(v)["Negative factor gives zero"]
    assert weak["ran_changed_code"] is None and "--coverage" in weak["ran_changed_code_note"]
    assert by_name(v)["Scale by a factor"]["ran_changed_code"] is True


def test_without_per_test_coverage_check_is_as_before(tmp_path):
    root, ch, db = planned(tmp_path)
    v = loop.check(db, ch, results(*AFTER))
    assert all(s["ran_changed_code"] is None and "ran_changed_code_note" not in s for s in v["scenarios"])
    page = (ch / "leyline.md").read_text()
    assert "Weaker proof" not in page and "without running" not in page
    # Cobertura has no per-test detail: still as before.
    ops = root / "src/calc/ops.py"
    xml = tmp_path / "cov.xml"
    xml.write_text(f'<?xml version="1.0"?><coverage><packages><package><classes><class filename="{ops}"><lines>'
                   f'<line number="{line(ops, "max(k, 0)")}" hits="1"/></lines></class></classes></package></packages></coverage>')
    assert coverage.import_file(store.connect(db), xml)["per_test"] is False
    assert all(s["ran_changed_code"] is None for s in loop.check(db, ch, results(*AFTER))["scenarios"])


def test_coverage_taken_before_the_plan_does_not_prove_the_change(tmp_path):
    root = tmp_path / "calc"
    write(root, {"src/calc/__init__.py": "", "src/calc/ops.py": OPS, "tests/test_ops.py": TESTS})
    db = root / ".leyline/leyline.db"
    loop.map_repos([str(root)], db, exact="off", page=False)
    ops = root / "src/calc/ops.py"
    old = coverage_db(tmp_path / ".coverage", {ops: {"tests/test_ops.py::test_scale_by_a_factor|run": [line(ops, "x * k")]}})
    con = store.connect(db)
    coverage.import_file(con, old)
    con.execute("UPDATE coverage_runs SET created = '2000-01-01T00:00:00+00:00'")
    con.commit()
    ch = change(root)
    loop.plan(db, ch, results("test_scale_by_a_factor"))
    ops.write_text(OPS.replace("return x * k", "return x * max(k, 0)"))
    s = by_name(loop.check(db, ch, results("test_scale_by_a_factor")))["Scale by a factor"]
    assert s["ran_changed_code"] is None and "before the change was planned" in s["ran_changed_code_note"]


def test_affected_tests_from_the_map_then_from_what_ran(tmp_path, monkeypatch):
    root, ch, db = planned(tmp_path)
    monkeypatch.chdir(root)
    code, out = run("affected-tests", "clamp-scale")
    assert code == 0, out
    assert "chosen by the map" in out and f"cd {root} && pytest tests/test_ops.py::" in out
    assert "its path on the map passes through the change: tests/test_ops.py::test_scale_by_a_factor" in out
    assert "is itself new or changed: tests/test_ops.py::test_negative_factor_gives_zero" in out
    con = store.connect(db)
    coverage.import_file(con, measured_after(root, tmp_path / ".coverage"))
    r = affected.select(con, "spec-clamp-scale")
    assert r["basis"] == "measured coverage"
    why = {t["pytest"]: t["why"] for t in r["tests"]}
    assert why == {"tests/test_ops.py::test_scale_by_a_factor": "ran the changed code when coverage was measured",
                   "tests/test_ops.py::test_negative_factor_gives_zero": "is itself new or changed"}
    assert r["commands"] == [{"runner": "pytest", "cwd": str(root), "tests": 2, "command":
                              "pytest tests/test_ops.py::test_scale_by_a_factor tests/test_ops.py::test_negative_factor_gives_zero"}]
    # A measured run that left a test out: the map's path fills in for it. One that measured it running none of
    # the changed code leaves it out.
    ops = root / "src/calc/ops.py"
    coverage.import_file(con, coverage_db(tmp_path / "partial.coverage", {ops: {
        "tests/test_ops.py::test_add_two_numbers|run": [line(ops, "return a + b")]}}))   # replaces the import above
    r =affected.select(con, "spec-clamp-scale")
    assert r["basis"] == "measured coverage and the map"
    why = {t["name"]: t["why"] for t in r["tests"]}
    assert why["test_scale_by_a_factor"].endswith("the measured run did not include it")
    assert "test_add_two_numbers" not in why
    assert "No change" in affected.select(con, "spec-nothing")["error"]


def test_a_command_runs_the_tests_from_where_it_says_when_pytest_ran_from_a_folder_below(tmp_path):
    """pytest run from `backend/` names its tests `tests/test_ops.py::...`; the command is run from the repository's
    root, so it must name them by their path there."""
    root = tmp_path / "mono"
    write(root, {"backend/src/calc/__init__.py": "", "backend/src/calc/ops.py": OPS, "backend/tests/test_ops.py": TESTS})
    ch = change(root)
    db = root / ".leyline/leyline.db"
    loop.map_repos([str(root)], db, exact="off", page=False)
    assert "error" not in loop.plan(db, ch)
    ops = root / "backend/src/calc/ops.py"
    con = store.connect(db)
    coverage.import_file(con, coverage_db(tmp_path / ".coverage", {ops: {
        "tests/test_ops.py::test_scale_by_a_factor|run": [line(ops, "return x * k")]}}))
    r = affected.select(con, "spec-clamp-scale")
    assert [t["pytest"] for t in r["tests"]] == ["backend/tests/test_ops.py::test_scale_by_a_factor"]
    assert r["commands"][0]["cwd"] == str(root)
    assert r["commands"][0]["command"] == "pytest backend/tests/test_ops.py::test_scale_by_a_factor"
    assert [t["pytest"] for t in affected.measured_tests(con, [n for n in affected.change_code(con, "spec-clamp-scale")[0]])] \
        == ["backend/tests/test_ops.py::test_scale_by_a_factor"]


def test_a_coverage_file_in_a_folder_named_like_a_url_is_read(tmp_path):
    """The data file is opened read-only by URI: `#`, `?` and `%` in its folder's name are characters, not parts of
    a URI (a `C#` folder)."""
    root, ch, db = planned(tmp_path)
    where = tmp_path / "C# work" / "50%25 done"
    where.mkdir(parents=True)
    con = store.connect(db)
    r = coverage.import_file(con, measured_after(root, where / ".coverage"))
    assert r["format"] == "coverage.py" and r["tests_matched_to_the_map"] == 3, r


def test_a_jest_command_runs_each_test_file_by_its_path(tmp_path):
    """`jest <args>` reads each argument as a regular expression matched against test paths (testPathPattern), so
    `app/[id]/page.test.tsx` (a Next.js route) is a character class and matches nothing of that name, and `.` and `+`
    match more than they say. `--runTestsByPath` takes each argument as the exact path."""
    import re
    import shlex
    root = tmp_path / "web"
    write(root, {"package.json": '{"devDependencies": {"jest": "29.7.0"}}'})
    con = store.connect(tmp_path / "s.db")
    with con:
        con.execute("INSERT INTO meta (key, value) VALUES ('root:w', ?)", (str(root),))
    paths = ["app/[id]/page.test.tsx", "app/(auth)/a+b.test.ts"]
    [cmd] = affected.commands(con, [{"name": "t", "repo": "w", "path": p} for p in paths])
    args = shlex.split(cmd["command"])
    assert args[:3] == ["npx", "jest", "--runTestsByPath"] and args[3:] == paths, cmd
    # what the arguments would have meant as patterns: neither file's own path matches its pattern
    assert not any(re.search(p, str(root / p), re.I) for p in paths)


def test_csharp_tests_run_by_dotnet_test_with_a_filter_per_test_project(tmp_path):
    import shlex
    root = tmp_path / "game"
    write(root, {"Shop.Tests/Shop.Tests.csproj": "<Project/>", "Other.Tests/Other.Tests.csproj": "<Project/>"})
    con = store.connect(tmp_path / "s.db")
    with con:
        con.execute("INSERT INTO meta (key, value) VALUES ('root:g', ?)", (str(root),))
    tests = [{"name": "Total_adds", "repo": "g", "path": "Shop.Tests/Cart/CartTests.cs",
              "id": "g:csharp:Shop.Tests::Shop.Tests.CartTests.Total_adds()"},
             {"name": "Parses", "repo": "g", "path": "Shop.Tests/Cart/CartTests.cs",
              "id": "g:csharp:Shop.Tests::Shop.Tests.CartTests.Parses(string,int)"},
             {"name": "Works", "repo": "g", "path": "Other.Tests/OtherTests.cs",
              "id": "g:csharp:Other.Tests::Other.Tests.OtherTests.Works()"}]
    [cmd] = affected.commands(con, tests)
    assert cmd["runner"] == "dotnet" and cmd["cwd"] == str(root) and cmd["tests"] == 3
    other, shop = [shlex.split(c) for c in cmd["command"].split(" && ")]   # one per test project
    assert shop == ["dotnet", "test", "Shop.Tests/Shop.Tests.csproj", "--filter",
                    "FullyQualifiedName=Shop.Tests.CartTests.Parses|FullyQualifiedName=Shop.Tests.CartTests.Total_adds"], shop
    assert other == ["dotnet", "test", "Other.Tests/Other.Tests.csproj", "--filter",
                     "FullyQualifiedName=Other.Tests.OtherTests.Works"], other


def test_tests_with_no_runner_are_counted_past_the_forty_kept(tmp_path):
    con = store.connect(tmp_path / "s.db")
    tests = [{"name": f"Test_{i:03}", "repo": "w", "path": "Shop.Tests/CartTests.cs"} for i in range(119)]
    [cmd] = affected.commands(con, tests)
    assert cmd["command"] is None and cmd["tests"] == 119
    page = affected.text({"change_id": "c", "basis": "the map", "note": "",
                          "tests": [{**t, "why": "its path on the map passes through the change"} for t in tests],
                          "commands": [cmd]})
    assert "No runner recognized for: Test_000, Test_001, Test_002, Test_003, Test_004, Test_005 and 113 more." in page, page


def test_istanbul_reports_tie_what_ran_to_a_test_file(tmp_path, monkeypatch):
    root = tmp_path / "js"
    ops = ("export function add(a: number, b: number): number {\n  return a + b;\n}\n\n"
           "export function scale(x: number, k: number): number {\n  return x * k;\n}\n")
    write(root, {"package.json": '{"devDependencies": {"vitest": "1.6.1"}}', "src/ops.ts": ops,
                 "src/scale.test.ts": 'import { it, expect } from "vitest";\nimport { scale } from "./ops";\n\n'
                                      'it("scales by a factor", () => {\n  expect(scale(2, 3)).toBe(6);\n});\n',
                 "src/add.test.ts": 'import { it, expect } from "vitest";\nimport { add } from "./ops";\n\n'
                                    'it("adds two numbers", () => {\n  expect(add(2, 3)).toBe(5);\n});\n'})
    db = root / ".leyline/leyline.db"
    loop.map_repos([str(root)], db, exact="off", page=False)

    def report(ran: str) -> dict:   # v8's conversion: every line a statement, declaration and brace lines counted on load
        lines = (root / "src/ops.ts").read_text().splitlines()
        body = {"add": 2, "scale": 6}
        smap = {str(i): {"start": {"line": i + 1, "column": 0}, "end": {"line": i + 1, "column": len(t)}} for i, t in enumerate(lines)}
        s = {str(i): (0 if i + 1 in body.values() and i + 1 != body[ran] else 1) for i in range(len(lines))}
        fmap = {"0": {"name": "add", "loc": {"start": {"line": 1}, "end": {"line": 3}}},
                "1": {"name": "scale", "loc": {"start": {"line": 5}, "end": {"line": 7}}}}
        return {str(root / "src/ops.ts"): {"path": str(root / "src/ops.ts"), "statementMap": smap, "s": s, "fnMap": fmap,
                                           "f": {"0": int(ran == "add"), "1": int(ran == "scale")}, "branchMap": {}, "b": {}}}
    con = store.connect(db)
    for name in ("scale", "add"):
        f = tmp_path / f"{name}.json"
        f.write_text(json.dumps(report(name)))
        r = coverage.import_file(con, f, test=str(root / f"src/{name}.test.ts"))
        assert r["format"] == "istanbul" and r["per"] == "test file" and r["tests_matched_to_the_map"] == 1, r
    ran = {r["test"]: r["node_id"].rsplit(".", 1)[-1] for r in con.execute("SELECT test, node_id FROM covered")}
    assert {Path(k).name: v for k, v in ran.items()} == {"scale.test.ts": "scale", "add.test.ts": "add"}

    write(root, {"openspec/changes/clamp/tasks.md": "- [ ] 1.1 Change `scale` in `src/ops.ts` to clamp the factor\n",
                 "openspec/changes/clamp/proposal.md": "# Change: Clamp\n\n## Why\nSigns.\n\n## What Changes\n- clamp\n"})
    loop.plan(db, root / "openspec/changes/clamp")
    r = affected.select(store.connect(db), "spec-clamp")
    assert [t["path"] for t in r["tests"]] == ["src/scale.test.ts"] and "per test file" in r["tests"][0]["why"]
    assert r["commands"][0]["command"] == "npx vitest run src/scale.test.ts" and r["commands"][0]["cwd"] == str(root)
    bad = tmp_path / "bad.json"
    bad.write_text('{"result": []}')
    assert "Istanbul" in coverage.import_file(con, bad)["error"]


def test_the_coverage_tool_ties_an_istanbul_report_to_its_test_file(tmp_path, monkeypatch):
    """The MCP tool takes `test` as the command takes `--test`: what a run-wide report saw ran under that file."""
    from leyline import server
    root = tmp_path / "js"
    write(root, {"package.json": '{"devDependencies": {"vitest": "1.6.1"}}',
                 "src/ops.ts": "export function scale(x: number, k: number): number {\n  return x * k;\n}\n",
                 "src/scale.test.ts": 'import { it, expect } from "vitest";\nimport { scale } from "./ops";\n\n'
                                      'it("scales by a factor", () => {\n  expect(scale(2, 3)).toBe(6);\n});\n'})
    db = root / ".leyline/leyline.db"
    loop.map_repos([str(root)], db, exact="off", page=False)
    ops = str(root / "src/ops.ts")
    smap = {str(i): {"start": {"line": i + 1, "column": 0}, "end": {"line": i + 1, "column": 9}} for i in range(3)}
    f = root / "coverage" / "coverage-final.json"   # inside the repository: the server reads only there
    f.parent.mkdir()
    f.write_text(json.dumps({ops: {"path": ops, "statementMap": smap, "s": {"0": 1, "1": 1, "2": 1},
                                   "fnMap": {"0": {"name": "scale", "loc": {"start": {"line": 1}, "end": {"line": 3}}}},
                                   "f": {"0": 1}, "branchMap": {}, "b": {}}}))
    monkeypatch.setenv("LEYLINE_DB", str(db))
    r = json.loads(server.coverage(import_path=str(f), test=str(root / "src/scale.test.ts")))
    assert r["imported"]["per"] == "test file" and r["imported"]["tests_matched_to_the_map"] == 1, r
    tests = {t for (t,) in store.connect(db).execute("SELECT test FROM covered")}
    assert {Path(t).name for t in tests} == {"scale.test.ts"}, tests


def test_a_measured_test_that_is_also_an_entry_point_is_named_by_its_test_flow(tmp_path):
    """Main is a program's entry point and a test ([Fact]): its test flow is flow:<Main>#test. A change it was
    measured running, with no static path to it, names that flow, not the entry point's."""
    from leyline import change
    from leyline.indexer import index
    root = tmp_path / "b"
    write(root, {"P.cs": "public static class P\n{\n    [Fact]\n    public static void Main() { Run(); }\n"
                         "    static void Run() { }\n    public static void Other() { }\n}\n"})
    db = tmp_path / "b.db"
    index(root, db, "b", exact="off")
    con = store.connect(db)
    main, other = (con.execute("SELECT id FROM nodes WHERE name = ? AND kind = 'callable'", (n,)).fetchone()[0]
                   for n in ("Main", "Other"))
    assert {r[0] for r in con.execute("SELECT id FROM flows")} >= {f"flow:{main}", f"flow:{main}#test"}
    with con:
        con.execute("INSERT INTO covered VALUES ('default', 'P.Main', ?, ?, 1)", (main, other))
    r = change.assess(con, "touch Other", [{"id": other, "action": "behavior"}])
    assert [(t["id"], t.get("flow")) for t in r["tests_to_run"] if t.get("measured")] == [(main, f"flow:{main}#test")], \
        r["tests_to_run"]
    con.close()


def test_a_pull_request_lists_the_tests_measured_running_its_change(tmp_path, monkeypatch):
    from test_pr import FILES, git
    root = tmp_path / "repo"
    write(root, FILES)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "app/store.py").write_text("def load(path, encoding):\n    with open(path, encoding=encoding) as f:\n"
                                       "        return f.read()\n")
    git(root, "commit", "-qam", "Read files with an encoding")
    monkeypatch.chdir(root)
    code, page = run("pr", "main")
    assert code == 0 and "Measured running" not in page
    store_py, use_py = root / "app/store.py", root / "app/use.py"
    data = coverage_db(tmp_path / ".coverage", {
        store_py: {"tests/test_use.py::test_count|run": [line(store_py, "with open"), line(store_py, "return f.read()")]},
        use_py: {"tests/test_use.py::test_count|run": [line(use_py, "return len(load(p))")]}})
    imported = coverage.import_file(store.connect(root / ".leyline/leyline.db"), data)
    assert imported["tests_matched_to_the_map"] == 1 and imported["functions_ran"] == 2, imported
    code, page = run("pr", "main")
    assert code == 0, page
    assert "- Measured running the changed code (per-test coverage): `tests/test_use.py::test_count`." in page
    assert "Run them: `pytest tests/test_use.py::test_count`" in page
    r = pr.review(root / ".leyline/leyline.db", root, "main")
    assert [t["pytest"] for t in r["tests"]["measured_running_the_change"]] == ["tests/test_use.py::test_count"]
