"""The verdict must be right: real runner output read correctly, a scenario proven by a generated test, new code
at the top of a file given a home, words in backticks left alone, and nothing reported outside the spec that a
task explains. Each test names the rehearsal finding it guards."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from leyline import diff, loop, spec, store
from leyline.indexer import index

CHECKS = '''"""Checks over numbers."""


def check_a(x):
    return x > 0


CHECKS = {
    "a": (
        check_a,
        "positive",
    ),
}


def run(x):
    return [(name, fn(x)) for name, (fn, _) in CHECKS.items()]
'''

VALIDATOR = '''export function validateAll(items: number[]): string[] {
  const out: string[] = [];
  const walk = (n: number): void => {
    if (n < 0) out.push("negative");
  };
  for (const n of items) walk(n);
  return out;
}

export function other(items: number[]): number {
  return items.length;
}
'''

VALIDATOR_TEST = '''import { describe, it, expect } from "vitest";
import { validateAll } from "../tool/validator";

const cases = ["one", "two"];
const names = ["x"];

describe("validator", () => {
  for (const c of cases) {
    it(`agrees on ${c}`, () => {
      expect(validateAll([1])).toEqual([]);
    });
  }
  for (const v of names) {
    it(`${v}`, () => {
      expect(v).toBeTruthy();
    });
  }
  it("flags a negative", () => {
    expect(validateAll([-1])).toEqual(["negative"]);
  });
});
'''

CHECKS_TEST = '''import pytest

from tool.checks import CHECKS, run


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_check(name):
    assert CHECKS[name][0](1) is not None


def test_run():
    assert run(1)
'''


def make_repo(root: Path) -> Path:
    for path, text in {"tool/checks.py": CHECKS, "tool/validator.ts": VALIDATOR, "test/validator.test.ts": VALIDATOR_TEST,
                       "tests/test_checks.py": CHECKS_TEST, "tool/__init__.py": "", "tests/__init__.py": "",
                       "docs/GUIDE.md": "| code | meaning |\n| --- | --- |\n| NEG | a negative |\n"}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    ch = root / "openspec" / "changes" / "zero-warn"
    (ch / "specs" / "checks").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Flag zero\n\n## Why\nA zero slips through.\n\n## What Changes\n"
                                    "- Both checkers flag zero with `ZERO`.\n")
    (ch / "tasks.md").write_text(
        "## 1. Rule\n"
        "- [ ] 1.1 Change `validateAll.walk` to flag zero as `ZERO`\n"
        "- [ ] 1.2 Add `checks.check_zero` and register it in the CHECKS table\n"
        "## 2. Tests\n"
        "- [ ] 2.1 Add the test \"flags a zero\" in test/validator.test.ts\n"
        "- [ ] 2.2 Add the case \"agrees on zero\" (the harness makes the test from it)\n"
        "## 3. Docs\n"
        "- [ ] 3.1 Change the `ZERO` row of `docs/GUIDE.md`\n")
    (ch / "specs" / "checks" / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Zero is flagged\nBoth checkers SHALL flag zero.\n\n"
        "#### Scenario: flags a zero\n- **WHEN** a zero is validated\n- **THEN** it is flagged\n\n"
        "#### Scenario: agrees on zero\n- **WHEN** the zero case runs\n- **THEN** both agree\n\n"
        "#### Scenario: check zero\n- **WHEN** the zero check runs\n- **THEN** it answers\n")
    return ch


def implement(root: Path, ts_rule: bool = True, rest: bool = True) -> None:
    if ts_rule:
        v = root / "tool/validator.ts"
        v.write_text(v.read_text().replace('    if (n < 0) out.push("negative");\n',
                                           '    if (n < 0) out.push("negative");\n    if (n === 0) out.push("ZERO");\n'))
    if not rest:
        return
    c = root / "tool/checks.py"
    c.write_text(c.read_text().replace('    return x > 0\n\n\n', '    return x > 0\n\n\ndef check_zero(x):\n    return x == 0\n\n\n', 1)
                 .replace('        "positive",\n    ),\n', '        "positive",\n    ),\n    "zero": (\n        check_zero,\n'
                          '        "zero",\n    ),\n'))
    t = root / "test/validator.test.ts"
    t.write_text(t.read_text().replace('const cases = ["one", "two"];', 'const cases = ["one", "two", "zero"];')
                 .replace('  it("flags a negative"', '  it("flags a zero", () => {\n    expect(validateAll([0])).toEqual(["ZERO"]);\n'
                                                     '  });\n\n  it("flags a negative"'))
    (root / "docs/GUIDE.md").write_text((root / "docs/GUIDE.md").read_text() + "| ZERO | a zero |\n")


def tap(names: dict) -> str:
    """vitest's TAP: a block per file and per describe, `# time=` after each name, a YAML block under a failure."""
    lines = ["TAP version 13", "1..1", "ok 1 - test/validator.test.ts # time=12.00ms {", "    1..1",
             "    " + ("not ok" if "fail" in names.values() else "ok") + " 1 - validator # time=10.00ms {"]
    for n, (name, status) in enumerate(names.items(), 1):
        lines.append(f"        {'not ok' if status == 'fail' else 'ok'} {n} - {name} # time=1.00ms")
        if status == "fail":
            lines += ["            ---", "            error:", '                name: "AssertionError"',
                      '                message: "expected [] to deeply equal [ \'ZERO\' ]"', "            ..."]
    return "\n".join(lines + ["    }", "}"]) + "\n"


def pytest_out(params: list[str], fail: str = "") -> str:
    rows = [f"{'FAILED' if p == fail else 'PASSED'} tests/test_checks.py::test_check[{p}]" + (" - assert False" if p == fail else "")
            for p in params]
    return "\n".join(["........", "=== short test summary info ===", *rows, "PASSED tests/test_checks.py::test_run",
                      f"{len(params) + 1} passed"]) + "\n"


BEFORE = {"agrees on one": "pass", "agrees on two": "pass", "x": "pass", "flags a negative": "pass"}
AFTER = {"agrees on one": "pass", "agrees on two": "pass", "agrees on zero": "pass", "x": "pass", "flags a zero": "pass",
         "flags a negative": "pass"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    work = tmp_path / "repo"
    work.mkdir()
    ch = make_repo(work)
    monkeypatch.chdir(work)
    db = work / ".leyline" / "leyline.db"
    loop.map_repos([str(work)], db, "r", exact="off", page=False)
    return work, ch, db


def test_runner_output_is_read_as_tests_not_suites():
    """Finding 1: vitest TAP names kept the `N - ` prefix and `# time=`, suites counted as failures."""
    out = {r["name"]: r for r in diff.parse_test_output(tap({"a": "pass", "b \\#2": "fail"}))}
    assert set(out) == {"test/validator.test.ts > validator > a", "test/validator.test.ts > validator > b #2"}
    assert out["test/validator.test.ts > validator > b #2"]["message"] == "expected [] to deeply equal [ 'ZERO' ]"

    node_test = """TAP version 13
# Subtest: math
    # Subtest: adds
    ok 1 - adds
      ---
      duration_ms: 0.4
      ...
    # Subtest: divides
    not ok 2 - divides
      ---
      duration_ms: 0.2
      failureType: 'testCodeFailure'
      error: 'boom'
      ...
    # Subtest: later
    ok 3 - later # SKIP not yet
    1..3
not ok 1 - math
  ---
  type: 'suite'
  ...
# Subtest: setup fails
    # Subtest: never runs
    ok 1 - never runs
    1..1
not ok 2 - setup fails
  ---
  error: 'before hook failed'
  ...
1..2
"""
    got = {r["name"]: r["status"] for r in diff.parse_test_output(node_test)}
    # One failing test is one failure; a suite that failed with no failing test inside (a hook) is one too.
    assert got == {"math > adds": "pass", "math > divides": "fail", "math > later": "skip",
                   "setup fails > never runs": "pass", "setup fails": "fail"}

    tap14 = "TAP version 14\n# Subtest: group\n    ok 1 - inner one\n    not ok 2 - inner two # TODO later\n    1..2\nok 1 - group # time=3ms\n1..1\n"
    assert {r["name"]: r["status"] for r in diff.parse_test_output(tap14)} == {"group > inner one": "pass", "group > inner two": "skip"}


def test_pytest_names_end_where_the_message_starts():
    """A parameter id with ` - ` in it was cut there, and XFAIL and XPASS kept their reason in the test's name."""
    out = {r["name"]: r for r in diff.parse_test_output(
        "PASSED t.py::test_p[a - b]\nFAILED t.py::test_f[p - q] - AssertionError: bad - thing\n"
        "XFAIL t.py::TestK::test_xf - known - bug\nXPASS t.py::TestK::test_xp - flaky\nFAILED t.py::test_g[c: d]\n")}
    assert {n: r["status"] for n, r in out.items()} == {
        "t.py::test_p[a - b]": "pass", "t.py::test_f[p - q]": "fail", "t.py::TestK::test_xf": "skip",
        "t.py::TestK::test_xp": "pass", "t.py::test_g[c: d]": "fail"}
    assert out["t.py::test_f[p - q]"]["message"] == "AssertionError: bad - thing"
    assert spec._result_keys("t.py::TestK::test_xp")[0] >= {"xp"}


def test_a_pytest_collection_error_is_recorded_under_its_file():
    """`ERROR tests/x.py - ModuleNotFoundError: ...` (pytest 7) was a test named `tests/x.py - ModuleNotFoundError`."""
    text = ("PASSED tests/test_ok.py::test_fine\n"
            "ERROR tests/test_bad.py - ModuleNotFoundError: No module named 'nosuchmodule_xyz'\n"
            "ERROR tests/sub/test_worse.py - ImportError: cannot import name 'a' - b\n"
            "ERROR tests/test_plain.py\n")   # pytest 8 and 9 print no message here
    out = {r["name"]: r for r in diff.parse_test_output(text)}
    assert set(out) == {"tests/test_ok.py::test_fine", "tests/test_bad.py", "tests/sub/test_worse.py", "tests/test_plain.py"}
    assert out["tests/test_bad.py"]["status"] == "fail"
    assert out["tests/test_bad.py"]["message"] == "ModuleNotFoundError: No module named 'nosuchmodule_xyz'"
    assert out["tests/sub/test_worse.py"]["message"] == "ImportError: cannot import name 'a' - b"


# `go test -v ./...` and `go test ./...` on a package with a passing, a failing, a table and a skipped test, and a
# second package that passes, as go 1.2x prints them.
GO_V = """=== RUN   TestStart
--- PASS: TestStart (0.00s)
=== RUN   TestShout
    engine_test.go:8: want "X!", got "X"
--- FAIL: TestShout (0.00s)
=== RUN   TestTable
=== RUN   TestTable/upper_case
=== RUN   TestTable/empty
    engine_test.go:15: empty name
--- FAIL: TestTable (0.00s)
    --- PASS: TestTable/upper_case (0.00s)
    --- FAIL: TestTable/empty (0.00s)
=== RUN   TestSkipped
    engine_test.go:21: later
--- SKIP: TestSkipped (0.00s)
FAIL
FAIL\texample.com/engine\t0.252s
=== RUN   TestFine
--- PASS: TestFine (0.00s)
PASS
ok  \texample.com/engine/ok\t0.377s
?   \texample.com/engine/cmd\t[no test files]
FAIL
"""
GO_PLAIN = """--- FAIL: TestShout (0.00s)
    engine_test.go:8: want "X!", got "X"
--- FAIL: TestTable (0.00s)
    --- FAIL: TestTable/empty (0.00s)
        engine_test.go:15: empty name
FAIL
FAIL\texample.com/engine\t0.136s
ok  \texample.com/engine/ok\t0.263s
FAIL\texample.com/engine/broken [build failed]
FAIL
"""


def test_go_test_output_is_read_test_by_test():
    """`--- PASS: TestX` lines were not read, and the package lines `ok  <pkg>` and `FAIL <pkg>` were read as tests."""
    out = {r["name"]: r for r in diff.parse_test_output(GO_V)}
    assert {n: r["status"] for n, r in out.items()} == {
        "TestStart": "pass", "TestShout": "fail", "TestTable > upper_case": "pass", "TestTable > empty": "fail",
        "TestSkipped": "skip", "TestFine": "pass"}
    assert out["TestShout"]["message"] == 'engine_test.go:8: want "X!", got "X"'
    assert out["TestTable > empty"]["message"] == "engine_test.go:15: empty name"
    out = {r["name"]: r for r in diff.parse_test_output(GO_PLAIN)}
    assert {n: r["status"] for n, r in out.items()} == {
        "TestShout": "fail", "TestTable > empty": "fail", "example.com/engine/broken": "fail"}
    assert out["TestShout"]["message"] == 'engine_test.go:8: want "X!", got "X"'
    assert out["TestTable > empty"]["message"] == "engine_test.go:15: empty name"
    assert out["example.com/engine/broken"]["message"] == "build failed"
    assert spec._result_keys("TestTable > upper_case")[0] >= {"upper case"}


# Jest's default reporter, as `jest --verbose` prints it for two files (one failing) and a file that cannot load;
# without --verbose only the file lines and the `●` sections are printed.
JEST = """ PASS  src/engine.test.js
  Engine
    start
      ✓ returns upper case (2 ms)
      ○ skipped is quiet
      ✎ todo whispers
 FAIL  src/shout.test.js (5.123 s)
  Engine
    shout
      ✓ is loud (1 ms)
      ✕ adds a bang (3 ms)
  ✓ top level (1 ms)

  ● Engine › shout › adds a bang

    expect(received).toBe(expected) // Object.is equality

    Expected: "X!"
    Received: "X"

      3 | describe("Engine", () => {
    > 5 |   expect(shout("x")).toBe("X!");

      at Object.<anonymous> (src/shout.test.js:5:20)

 FAIL  src/broken.test.js
  ● Test suite failed to run

    Cannot find module './nope' from 'src/broken.test.js'

Test Suites: 2 failed, 1 passed, 3 total
Tests:       1 failed, 1 skipped, 1 todo, 3 passed, 6 total
Snapshots:   0 total
Time:        6.2 s
Ran all test suites.
"""
JEST_SHORT = """ PASS  src/engine.test.js
 FAIL  src/shout.test.js
  ● Engine › shout › adds a bang

    expect(received).toBe(expected) // Object.is equality

Test Suites: 1 failed, 1 passed, 2 total
"""


def test_jest_output_is_read_test_by_test():
    """Jest's ` PASS  file` lines were read as tests named for the file, and its ✓ and ✕ lines not at all."""
    out = {r["name"]: r for r in diff.parse_test_output(JEST)}
    assert {n: r["status"] for n, r in out.items()} == {
        "src/engine.test.js > Engine > start > returns upper case": "pass",
        "src/engine.test.js > Engine > start > is quiet": "skip",
        "src/engine.test.js > Engine > start > whispers": "skip",
        "src/shout.test.js > Engine > shout > is loud": "pass",
        "src/shout.test.js > Engine > shout > adds a bang": "fail",
        "src/shout.test.js > top level": "pass",
        "src/broken.test.js": "fail"}
    assert out["src/shout.test.js > Engine > shout > adds a bang"]["message"] == \
        "expect(received).toBe(expected) // Object.is equality"
    assert out["src/broken.test.js"]["message"] == "Cannot find module './nope' from 'src/broken.test.js'"
    out = {r["name"]: r["status"] for r in diff.parse_test_output(JEST_SHORT)}
    assert out == {"src/shout.test.js > Engine > shout > adds a bang": "fail"}


# node --test --test-reporter=tap (node 26) on test("tab\there"), test("back\\slash t"), test("new\nline"),
# test("parses {"), describe("suite {", () => test("inside")) and test("hash # here").
NODE_ESCAPED = """TAP version 13
# Subtest: tab\\\\there
ok 1 - tab\\\\there
  ---
  duration_ms: 0.4
  ...
# Subtest: back\\\\slash t
ok 2 - back\\\\slash t
# Subtest: new\\\\nline
ok 3 - new\\\\nline
# Subtest: parses {
ok 4 - parses {
  ---
  duration_ms: 0.1
  ...
# Subtest: suite {
    # Subtest: inside
    ok 1 - inside
    1..1
ok 5 - suite {
# Subtest: hash \\# here
ok 6 - hash \\# here
1..6
"""


def test_node_test_names_are_read_as_the_runner_meant(tmp_path):
    """node:test prints a tab as `\\\\t` and a newline as `\\\\n`, kept as a backslash and a letter; and a name ending in
    ` {` lost it, read as vitest's brace that opens a block."""
    got = [r["name"] for r in diff.parse_test_output(NODE_ESCAPED)]
    assert got == ["tab\there", "back\\slash t", "new\nline", "parses {", "suite { > inside", "hash # here"]
    # The map keeps a test's name as written in the source (`"tab\\there"`): the result still finds it.
    work = tmp_path / "js"
    (work / "test").mkdir(parents=True)
    (work / "test/a.test.mjs").write_text('import { test } from "node:test";\ntest("tab\\there", () => {});\n'
                                          'test("parses {", () => {});\n')
    index(work, tmp_path / "s.db", "r")
    names = diff.TestNames(store.connect(tmp_path / "s.db"))
    assert names.node("tab\there") and names.node("parses {")


def test_a_suite_named_for_a_method_is_not_a_file(tmp_path):
    """node:test prints no file, so `Engine.start > returns upper case` read `Engine.start` as the test's file and
    tied the result to no test."""
    work = tmp_path / "js"
    (work / "src").mkdir(parents=True)
    (work / "test").mkdir()
    (work / "src/engine.js").write_text("export class Engine {\n  start() { return 'X'; }\n}\n")
    (work / "test/engine.test.js").write_text(
        "import { describe, it } from 'vitest';\nimport { Engine } from '../src/engine.js';\n"
        "describe('Engine.start', () => {\n  it('returns upper case', () => { new Engine().start(); });\n});\n")
    index(work, tmp_path / "s.db", "r")
    names = diff.TestNames(store.connect(tmp_path / "s.db"))
    assert diff.result_parts("Engine.start > returns upper case")["file"] is None
    want = names.node("test/engine.test.js > Engine.start > returns upper case")
    assert want and names.node("Engine.start > returns upper case") == want


def test_node_test_names_and_messages_as_printed():
    """node:test escapes `#` in a `# Subtest:` line too, and prints a failure's message as `error:`."""
    text = ("TAP version 13\n# Subtest: Engine \\#start\n    # Subtest: fails\n    not ok 1 - fails\n      ---\n"
            "      error: '1 == 2'\n      stack: |-\n        at x\n      ...\n    1..1\nnot ok 1 - Engine \\#start\n"
            "# Subtest: block\nnot ok 2 - block\n  ---\n  error: |-\n    multi\n  ...\n1..2\n")
    out = {r["name"]: r for r in diff.parse_test_output(text)}
    assert set(out) == {"Engine #start > fails", "block"}
    assert out["Engine #start > fails"]["message"] == "1 == 2" and not out["block"]["message"]


def test_pytest_parameters_are_separate_results():
    """Finding 2: `test_x[a]` was stored as `test_x`, so a failing parameter hid behind a passing one."""
    out = {r["name"]: r["status"] for r in diff.parse_test_output(pytest_out(["a", "zero"], fail="zero"))}
    assert out["tests/test_checks.py::test_check[zero]"] == "fail" and out["tests/test_checks.py::test_check[a]"] == "pass"
    assert spec._result_keys("tests/test_checks.py::test_check[zero]")[0] >= {"check zero"}


def test_results_tie_to_their_own_test_only(repo):
    """Finding 1: every name that matched nothing was tied to one test named `{}`."""
    work, ch, db = repo
    con = store.connect(db)
    results = diff.parse_test_output(tap({**AFTER, "something else": "pass"}) + pytest_out(["a", "zero"]))
    out = diff.record_tests(con, "probe", results)
    tied = dict(con.execute("SELECT name, test_id FROM test_results WHERE run = 'probe'").fetchall())
    assert tied["test/validator.test.ts > validator > something else"] is None      # not the `{}` test
    assert tied["test/validator.test.ts > validator > agrees on zero"].endswith("agrees-on")   # the template it came from
    assert tied["test/validator.test.ts > validator > flags a negative"].endswith("flags-a-negative")
    assert tied["tests/test_checks.py::test_check[zero]"].endswith("test_checks.test_check")
    # Not tied: `something else`; `flags a zero`, not written yet; and `x`, whose test is named all hole (`{}`).
    assert out["matched_to_test_nodes"] == len(results) - 3


def test_plan_reads_new_top_level_functions_and_words(repo):
    """Findings 4 and 5: `module.func` had no home; `ZERO` and a doc path blocked the plan; a docs task blocked forever."""
    work, ch, db = repo
    b = loop.plan(db, ch, diff.parse_test_output(tap(BEFORE) + pytest_out(["a"])))
    t = {x["key"]: x for x in b["tasks"]}
    assert t["1.2"]["new"][0]["parent"].endswith("file:tool/checks.py")
    assert t["2.1"]["into"] and t["2.1"]["scenarios"]           # the test file, written without backticks
    assert t["3.1"]["by_you"] and any("GUIDE.md" in n for n in t["3.1"]["notes"])
    assert any("`ZERO`" in n for n in t["1.1"]["notes"])
    sc = {s["name"]: s for s in b["scenarios"]}
    assert sc["agrees on zero"]["generated_by"] == "agrees on {}"   # a template test makes it at run time
    assert b["gaps"] == [] and spec.brief_status(b)["ready"], b["gaps"]
    page = (ch / "leyline.md").read_text()
    assert "made at run time by `agrees on {}`" in page and "no code: checked by you" in page

    # The spellings a gap offers for a new function with no home.
    (ch / "tasks.md").write_text((ch / "tasks.md").read_text() + "- [ ] 4.1 Add `check_more`\n")
    con = store.connect(db)
    gap = next(g for g in spec.brief(con, ch, write=False)["gaps"] if "check_more" in g)
    assert "`module.name`" in gap and "`path/to/file.py: name`" in gap
    for written in ("`checks.check_more`", "`tool/checks.py: check_more`", "`check_more` in `tool/checks.py`"):
        (ch / "tasks.md").write_text((ch / "tasks.md").read_text().rsplit("- [ ] 4.1", 1)[0] + f"- [ ] 4.1 Add {written}\n")
        new = {x["key"]: x for x in spec.brief(con, ch, write=False)["tasks"]}["4.1"]["new"]
        assert new and new[0]["parent"].endswith("file:tool/checks.py"), written


def test_check_says_done_when_done_and_not_yet_when_broken(repo):
    """Findings 3, 11, 12, 13 and 16: the whole loop on a change made exactly as agreed, then on a broken one."""
    work, ch, db = repo
    b = loop.plan(db, ch, diff.parse_test_output(tap(BEFORE) + pytest_out(["a"])))
    snap = diff.snapshot_path(store.connect(db), b["change_id"])
    size = snap.stat().st_size
    assert b["baseline"] == "new" and snap.exists()
    assert {r[0] for r in sqlite3.connect(snap).execute("SELECT name FROM sqlite_master WHERE type = 'table'")} >= {"source_lines", "node_rows"}
    assert loop.plan(db, ch)["baseline"] == "same" and snap.stat().st_mtime_ns and snap.stat().st_size == size   # no second copy

    # Broken: the TypeScript rule is missing and its scenario fails.
    implement(work, ts_rule=False)
    v = loop.check(db, ch, diff.parse_test_output(tap({**AFTER, "flags a zero": "fail"}) + pytest_out(["a", "zero"])))
    assert not v["done_as_agreed"] and {t["key"]: t["state"] for t in v["tasks"]}["1.1"] == "not done"
    assert {s["name"]: s["state"] for s in v["scenarios"]}["flags a zero"] == "fails"
    assert "tests added since the plan fail" in v["why_not"]          # new, so not "passed before, fails now"
    nxt = " ".join(loop.next_after_check(v, "zero-warn"))
    assert "finish task 1.1" in nxt and '"flags a zero"' in nxt and "fix what fails" not in nxt
    assert snap.exists()

    # A failing parameter is not hidden by the passing ones.
    implement(work, rest=False)
    v = loop.check(db, ch, diff.parse_test_output(tap(AFTER) + pytest_out(["a", "zero"], fail="zero")))
    assert {s["name"]: s["state"] for s in v["scenarios"]}["check zero"] == "fails" and not v["done_as_agreed"]

    v = loop.check(db, ch, diff.parse_test_output(tap(AFTER) + pytest_out(["a", "zero"])))
    assert v["drift"] == [], v["drift"]       # not the module bodies, not validateAll, not check_zero
    assert {t["key"]: t["state"] for t in v["tasks"]} == {"1.1": "done", "1.2": "done", "2.1": "done", "2.2": "done",
                                                         "3.1": "checked by you"}
    sc = {s["name"]: s for s in v["scenarios"]}
    assert sc["agrees on zero"]["state"] == "passes" and sc["agrees on zero"]["generated"]
    assert v["done_as_agreed"], v["why_not"]
    page = (ch / "leyline.md").read_text()
    assert "proven by the test run (the test is generated, so it is not on the map)" in page
    assert "not ticked" not in page and "**Yes.**" in page
    assert "Check task 3.1 by hand" in " ".join(loop.next_after_check(v, "zero-warn"))
    # Done: the baseline stays, so a regression after "done" is still caught, and check runs again (Signal item 10).
    assert snap.exists()
    v = work / "tool/validator.ts"
    v.write_text(v.read_text().replace('    if (n === 0) out.push("ZERO");\n', ""))
    again = loop.check(db, ch, diff.parse_test_output(tap({**AFTER, "flags a zero": "fail"}) + pytest_out(["a", "zero"])))
    assert "error" not in again and not again["done_as_agreed"]
    assert {t["key"]: t["state"] for t in again["tasks"]}["1.1"] == "not done"


def test_drift_is_still_reported(repo):
    """The other side of finding 11: an edit no task explains is still outside the spec."""
    work, ch, db = repo
    loop.plan(db, ch, diff.parse_test_output(tap(BEFORE) + pytest_out(["a"])))
    implement(work)
    v = work / "tool/validator.ts"
    v.write_text(v.read_text().replace("return items.length;", "return items.length + 1;"))
    c = work / "tool/checks.py"
    c.write_text(c.read_text().replace('"""Checks over numbers."""', '"""Checks over numbers."""\n\nLIMIT = 3'))
    out = loop.check(db, ch, diff.parse_test_output(tap(AFTER) + pytest_out(["a", "zero"])))
    assert sorted(n["name"] for n in out["drift"]) == ["<top-level>", "other"] and not out["done_as_agreed"]
    assert "the top level of tool/checks.py" in (ch / "leyline.md").read_text()


def test_findings_show_the_claim_then_the_decision_and_reviews_that_found_nothing(repo):
    """Findings 9 and 10."""
    work, ch, db = repo
    con = store.connect(db)
    b = spec.brief(con, ch)
    f = spec.add_finding(con, b["change_id"], "logic", "medium", "Zero and false are equal in Python. More words here.",
                         [b["tasks"][1]["new"][0]["parent"]], "Exclude bool.")
    spec.resolve_finding(con, f["id"], "accepted", "Exclude bool in Python")
    spec.record_review(con, b["change_id"], "performance")
    page = spec.brief_text(spec.brief(con, ch, write=False))
    assert "- medium: Zero and false are equal in Python. **Accepted**: Exclude bool in Python." in page
    assert "- Performance review: ran, and filed nothing." in page and "- Logic review: 1 finding, all settled." in page


def test_speed_tests_are_found_by_place_and_clock(tmp_path):
    """Finding 9: a keyword in a test's name ("slow_down") is not a speed test; perf-budget.test.ts is."""
    (tmp_path / "test").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/core.ts").write_text("export function work(n: number): number { return n * 2; }\n")
    (tmp_path / "test/perf-budget.test.ts").write_text(
        'import { it, expect } from "vitest";\nimport { work } from "../src/core";\n'
        'it("scales linearly", () => { expect(work(2)).toBe(4); });\n')
    (tmp_path / "test/core.test.ts").write_text(
        'import { it, expect } from "vitest";\nimport { work } from "../src/core";\n'
        'it("adds 5s of slow_down", () => { expect(work(1)).toBe(2); });\n'
        'it("is quick enough", () => { const t = performance.now(); work(1); expect(performance.now() - t).toBeLessThan(5); });\n')
    db = tmp_path / ".leyline" / "leyline.db"
    index(tmp_path, db, "s")
    con = store.connect(db)
    found = {t["name"]: t["why"] for t in spec._speed_tests(con, set())}
    assert found == {"scales linearly": "named for speed", "is quick enough": "reads a clock"}


def test_map_keeps_its_store_out_of_git(tmp_path):
    """Finding 17: `.leyline/` is ignored without touching the repository's own .gitignore."""
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    (tmp_path / ".gitignore").write_text("node_modules\n")
    loop.map_repos([str(tmp_path)], tmp_path / ".leyline" / "leyline.db", "g", exact="off", page=False)
    assert (tmp_path / ".leyline" / ".gitignore").read_text().splitlines()[-1] == "*"
    assert (tmp_path / ".gitignore").read_text() == "node_modules\n"


def test_forget_drops_a_baseline(repo, capsys):
    """Finding 16: a baseline can be deleted on request."""
    from leyline.cli import main
    work, ch, db = repo
    loop.plan(db, ch)
    assert main(["--db", str(db), "spec", "forget", str(ch)]) == 0
    assert "Deleted the baseline of spec-zero-warn." in capsys.readouterr().out
    assert not diff.snapshot_path(store.connect(db), "spec-zero-warn").exists()
