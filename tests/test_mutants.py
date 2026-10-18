"""A mutation plan for a spec change, and what its results say about the scenarios' own tests. Each test builds a small git
project, plans and implements a change in it, and uses the real `mutants`, `check` and, where the scenario is about it,
the generated run.py."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leyline import cli, diff, loop, mutants, store, verdicts

CALC_BEFORE = '''def discount(total):
    if total > 100:
        return total - 10
    return total
'''
CALC_AFTER = '''def discount(total):
    if total > 200:
        return total - 20
    if total > 100:
        return total - 10
    return total
'''
TESTS_BEFORE = '''from app.calc import discount


def test_big_orders_get_ten_off():
    assert discount(150) == 140
'''
TESTS_AFTER = TESTS_BEFORE + '''

def test_big_orders_get_twenty_off():
    assert discount(250) == 230


def test_discount_runs_for_any_total():
    discount(150)
    discount(250)
'''
TASKS = '''- [ ] 1.1 Change `discount` to take twenty off over 200
- [ ] 2.1 Add the test "Big orders get twenty off"
- [ ] 2.2 Add the test "Discount runs for any total"
'''
SPEC = '''## ADDED Requirements

### Requirement: Tiers
#### Scenario: Big orders get twenty off
- **WHEN** the total is 250
- **THEN** it is 230

#### Scenario: Discount runs for any total
- **WHEN** a total is given
- **THEN** it runs
'''
STRONG = "tests/test_calc.py::test_big_orders_get_twenty_off"
WEAK = "tests/test_calc.py::test_discount_runs_for_any_total"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=root, check=True, capture_output=True)


def run_tests(root: Path) -> str:
    return subprocess.run([sys.executable, "-m", "pytest", "-rA", "-q", "-p", "no:cacheprovider"], cwd=root,
                          capture_output=True, text=True).stdout


class Project:
    """A git project with a change planned before the code is written, and written after."""

    def __init__(self, root: Path, before: dict, after: dict, tasks: str = TASKS, spec: str = SPEC, change: str = "more-discount"):
        self.root, self.change = root, change
        self.db = root / ".leyline" / "leyline.db"
        self.dir = root / "openspec" / "changes" / change
        self.before_files, self.after_files = before, after
        root.mkdir(parents=True)
        self.write(before)
        (self.dir / "specs" / "pricing").mkdir(parents=True)
        (self.dir / "proposal.md").write_text(f"# Change: {change}\n\n## Why\nIt must.\n\n## What Changes\n- `discount`\n")
        (self.dir / "tasks.md").write_text(tasks)
        (self.dir / "specs" / "pricing" / "spec.md").write_text(spec)
        git(root, "init", "-q")
        git(root, "add", "app", "tests", "openspec")
        git(root, "commit", "-q", "-m", "base")
        loop.map_repos([str(root)], self.db, repo_id="p")
        before_run = diff.parse_test_output(run_tests(root))
        planned = loop.plan(self.db, self.dir, before_run)
        assert "error" not in planned, planned

    def write(self, files: dict) -> None:
        for rel, text in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)

    def implement(self) -> None:
        self.write(self.after_files)
        self.after = diff.parse_test_output(run_tests(self.root))

    def plan(self, limit: int = 30) -> dict:
        loop.refresh(self.db)
        con = store.connect(self.db)
        try:
            return mutants.plan(con, self.dir, limit)
        finally:
            con.close()

    def plan_dir(self) -> Path:
        return self.root / ".leyline" / "mutants" / self.change

    def results(self, rows: list[dict], plan: str = None) -> Path:
        """A results.jsonl as a run of run.py writes it: the unmutated run, then one line for each row."""
        d = self.plan_dir()
        pid = plan or json.loads((d / "plan.json").read_text())["plan"]
        lines = [{"plan": pid, "baseline_failed": [], "baseline_seconds": 1.0}] + [{"plan": pid, **r} for r in rows]
        (d / "results.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        return d / "results.jsonl"

    def check(self, results: Path = None) -> dict:
        return loop.check(self.db, self.dir, self.after, None, results)


@pytest.fixture
def proj(tmp_path):
    p = Project(tmp_path / "proj", {"app/__init__.py": "", "app/calc.py": CALC_BEFORE, "tests/test_calc.py": TESTS_BEFORE},
                {"app/calc.py": CALC_AFTER, "tests/test_calc.py": TESTS_AFTER})
    p.implement()
    return p


def by_operator(plan: dict) -> dict:
    return {m["operator"]: m["id"] for m in plan["mutants"]}


def scenario(v: dict, name: str) -> dict:
    return next(s for s in v["scenarios"] if s["name"] == name)


def test_the_mutation_plan_covers_only_the_lines_the_change_edited(proj):
    plan = proj.plan()
    assert plan["count"] == 5 and {m["at"].split(":")[1] for m in plan["mutants"]} == {"2", "3"}   # not the unedited tier
    doc = json.loads((proj.plan_dir() / "plan.json").read_text())
    for m in doc["mutants"]:
        assert m["function_name"] == "discount" and m["before"] and m["after"] != m["before"]
        assert STRONG in m["stage1"] and WEAK in m["stage1"]                       # the scenarios' tests come first
        assert "tests/test_calc.py::test_big_orders_get_ten_off" in m["stage2"]    # then the other tests that reach it


def test_a_moved_function_gets_no_mutants(tmp_path):
    p = Project(tmp_path / "moved", {"app/__init__.py": "", "app/calc.py": CALC_AFTER, "tests/test_calc.py": TESTS_AFTER},
                {"app/calc.py": "from app.pricing import discount\n", "app/pricing.py": CALC_AFTER,
                 "tests/test_calc.py": TESTS_AFTER})
    p.implement()
    assert p.plan()["count"] == 0                                                   # its lines stood in the baseline


def test_each_mutant_is_a_patch_that_applies(proj, tmp_path):
    before = (proj.root / "app" / "calc.py").read_bytes()
    plan = proj.plan()
    for m in plan["mutants"]:
        done = subprocess.run(["git", "apply", "--check", str(proj.plan_dir() / f"{m['id']}.patch")], cwd=proj.root,
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    assert (proj.root / "app" / "calc.py").read_bytes() == before                  # the working tree is unchanged
    crlf = Project(tmp_path / "crlf", {"app/__init__.py": "", "app/calc.py": CALC_BEFORE.replace("\n", "\r\n").encode(),
                                       "tests/test_calc.py": TESTS_BEFORE},
                   {"app/calc.py": CALC_AFTER.replace("\n", "\r\n").encode(), "tests/test_calc.py": TESTS_AFTER})
    crlf.implement()
    for m in crlf.plan()["mutants"]:
        done = subprocess.run(["git", "apply", "--check", str(crlf.plan_dir() / f"{m['id']}.patch")], cwd=crlf.root,
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


def test_a_mutant_is_made_at_the_operator(tmp_path):
    before = "def pick(a, b, c, d):\n    return None\n"
    after = 'def pick(a, b, c, d):\n    flag = ("é", a > b and c > d)\n    return flag\n'
    tasks = "- [ ] 1.1 Change `pick` to return a word\n- [ ] 2.1 Add the test \"Pick words\"\n"
    spec = "## ADDED Requirements\n\n### Requirement: Pick\n#### Scenario: Pick words\n- **WHEN** a\n- **THEN** b\n"
    p = Project(tmp_path / "ops", {"app/__init__.py": "", "app/calc.py": before,
                                   "tests/test_calc.py": "from app.calc import pick\n\n\ndef test_pick_words():\n    assert pick(0, 0, 0, 0) is None\n"},
                {"app/calc.py": after, "tests/test_calc.py": "from app.calc import pick\n\n\ndef test_pick_words():\n    assert pick(2, 1, 4, 3)[1] is True\n"},
                tasks=tasks, spec=spec)
    p.implement()
    assert p.plan()["count"] > 0
    doc = json.loads((p.plan_dir() / "plan.json").read_text())
    afters = {m["after"] for m in doc["mutants"] if m["operator"] == "> to <="}
    assert 'flag = ("é", a > b and c <= d)' in afters and 'flag = ("é", a <= b and c > d)' in afters
    for m in doc["mutants"]:                                                       # each mutated file still parses
        src = (p.root / m["file"]).read_text().split("\n")
        src[m["line"] - 1] = "    " + m["after"]                                    # the plan keeps the line stripped
        compile("\n".join(src), m["file"], "exec")


def test_code_of_the_tests_gets_no_mutants(proj):
    plan = proj.plan()
    assert plan["mutants"] and all(not m["at"].startswith("tests/") for m in plan["mutants"])


def test_the_cap_gives_each_edited_function_a_mutant_first():
    site = lambda f, line: {"file": "a.py", "line": line, "c0": 0, "c1": 1, "new": "x", "operator": "o", "function": f,
                            "function_name": f}
    many = {f"f{k}": [site(f"f{k}", 10 * k + j) for j in range(10)] for k in range(5)}
    picked, left = mutants._choose(many, 30)
    assert left == [] and [m["function"] for m in picked[:5]] == [f"f{k}" for k in range(5)]
    counts = [sum(m["function"] == f for m in picked) for f in many]
    assert max(counts) - min(counts) <= 1 and [m["id"] for m in picked[:3]] == ["m-1", "m-2", "m-3"]
    wide = {f"g{k}": [site(f"g{k}", k)] for k in range(40)}
    picked, left = mutants._choose(wide, 30)
    assert len(picked) == 30 and len(left) == 10                                    # the ten the cap left out are named


def test_a_language_with_no_mutation_operators_is_named(tmp_path):
    p = Project(tmp_path / "ts", {"app/__init__.py": "", "app/calc.py": CALC_BEFORE, "tests/test_calc.py": TESTS_BEFORE,
                                  "app/util.ts": "export function bump(a: number): number {\n  return a + 1;\n}\n"},
                {"app/util.ts": "export function bump(a: number): number {\n  return a + 2;\n}\n"}, tasks="- [ ] 1.1 Change `bump` to add two\n- [ ] 2.1 Add the test \"Bump adds two\"\n",
                spec="## ADDED Requirements\n\n### Requirement: Bump\n#### Scenario: Bump adds two\n- **WHEN** a\n- **THEN** b\n")
    p.implement()
    plan = p.plan()
    assert plan["count"] == 0 and any(x["file"].endswith("util.ts") and "Python only" in x["why"] for x in plan["no_mutants"])


def test_a_plan_is_for_spec_changes_only(proj, capsys):
    for name in ("pr-12", "quick-make-it-three"):
        assert cli.main(["--db", str(proj.db), "mutants", name]) == 2
        assert "for a spec change" in capsys.readouterr().err


def test_planning_reads_the_line_hashes_of_the_edited_files_only(tmp_path, monkeypatch):
    files = {"app/__init__.py": "", "app/calc.py": CALC_BEFORE, "tests/test_calc.py": TESTS_BEFORE,
             "app/other.py": "def untouched(a):\n    return a == 1\n"}
    p = Project(tmp_path / "rows", files, {"app/calc.py": CALC_AFTER, "tests/test_calc.py": TESTS_AFTER})
    p.implement()
    seen = []
    real = diff._open

    def spy(path):
        con = real(path)
        con.set_trace_callback(lambda sql: seen.append(sql) if "source_lines" in sql else None)
        return con
    monkeypatch.setattr(diff, "_open", spy)
    assert p.plan()["count"] > 0
    reads = [sql for sql in seen if "hashes" in sql]
    assert reads and all("app/calc.py" in sql for sql in reads)                      # no other file's rows are read


def test_the_runner_restores_the_file_after_a_hang(tmp_path, monkeypatch):
    before = "def wait(n):\n    return n\n"
    after = "def wait(n):\n    while n > 0:\n        n = n - 1\n    return n\n"
    tasks = "- [ ] 1.1 Change `wait` to count down\n- [ ] 2.1 Add the test \"Wait counts down\"\n"
    spec = "## ADDED Requirements\n\n### Requirement: Wait\n#### Scenario: Wait counts down\n- **WHEN** a\n- **THEN** b\n"
    p = Project(tmp_path / "hang", {"app/__init__.py": "", "app/calc.py": before,
                                    "tests/test_calc.py": "from app.calc import wait\n\n\ndef test_wait_counts_down():\n    assert wait(0) == 0\n"},
                {"app/calc.py": after, "tests/test_calc.py": "from app.calc import wait\n\n\ndef test_wait_counts_down():\n    assert wait(3) == 0\n"},
                tasks=tasks, spec=spec)
    p.implement()
    plan = p.plan()
    hang = [m for m in plan["mutants"] if m["operator"] == "- to +"]
    assert hang
    original = (p.root / "app" / "calc.py").read_bytes()
    done = subprocess.run([sys.executable, str(p.plan_dir() / "run.py")], cwd=p.root, capture_output=True, text=True,
                          env={**os.environ, "LEYLINE_MUTANT_TIMEOUT": "4"}, timeout=240)
    assert done.returncode == 0, done.stdout + done.stderr
    got = {json.loads(x)["id"]: json.loads(x) for x in (p.plan_dir() / "results.jsonl").read_text().splitlines() if '"id"' in x}
    assert got[hang[0]["id"]]["status"] == "timeout"
    assert (p.root / "app" / "calc.py").read_bytes() == original and not (p.plan_dir() / "applied.json").exists()


def test_a_mutant_is_not_run_from_the_compiled_file_of_the_original(tmp_path):
    """Python keeps a compiled file while its source has the same size and the same modified time in whole seconds. A
    mutant such as `-` to `+` is the same size, and a quick test run writes it within a second of the last compile, so
    the mutant would run as the original and be counted as one that survived. Each run gets its own place for them."""
    import importlib.util
    p = Project(tmp_path / "pyc", {"app/__init__.py": "", "app/calc.py": CALC_BEFORE, "tests/test_calc.py": TESTS_BEFORE},
                {"app/calc.py": CALC_AFTER, "tests/test_calc.py": TESTS_AFTER})
    p.implement()
    assert p.plan()["count"] > 0
    spec = importlib.util.spec_from_file_location("leyline_mutant_run", p.plan_dir() / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    calc = p.root / "app" / "calc.py"
    original = calc.read_bytes()
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    subprocess.run([sys.executable, "-c", "import app.calc"], cwd=p.root, env=env, check=True)   # the compiled original
    stat = calc.stat()
    mutant = original.replace(b"total - 20", b"total + 20")
    assert mutant != original and len(mutant) == len(original)
    calc.write_bytes(mutant)
    os.utime(calc, ns=(stat.st_atime_ns, stat.st_mtime_ns))   # the second the compiled file recorded
    failed, _ = run.tests(str(p.root), [STRONG], False, 120)
    assert failed == [STRONG], failed                         # the mutant ran: the test that wants 230 sees 270


def test_a_baseline_failure_kills_nothing(tmp_path):
    p = Project(tmp_path / "base", {"app/__init__.py": "", "app/calc.py": CALC_BEFORE, "tests/test_calc.py": TESTS_BEFORE},
                {"app/calc.py": CALC_AFTER, "tests/test_calc.py": TESTS_AFTER + "\n\ndef test_always_fails():\n    discount(250)\n    assert False\n"})
    p.implement()
    p.plan()
    done = subprocess.run([sys.executable, str(p.plan_dir() / "run.py")], cwd=p.root, capture_output=True, text=True, timeout=240)
    assert done.returncode == 0, done.stdout + done.stderr
    rows = [json.loads(x) for x in (p.plan_dir() / "results.jsonl").read_text().splitlines()]
    assert "tests/test_calc.py::test_always_fails" in rows[0]["baseline_failed"]
    assert all("test_always_fails" not in " ".join(r.get("killed_by", [])) for r in rows[1:])


def test_a_mutant_that_survived_is_listed(proj):
    plan = proj.plan()
    boundary = by_operator(plan)["200 to 201"]
    rows = [{"id": m["id"], "status": "survived" if m["id"] == boundary else "killed",
             "killed_by": [] if m["id"] == boundary else [STRONG], "ran": [3, 3]} for m in plan["mutants"]]
    v = proj.check(proj.results(rows))
    assert "error" not in v
    page = loop.spec.verify_text(v)
    assert "Covered, but not asserted" in page and "app/calc.py:2" in page and "`if total > 201:`" in page


def test_a_scenario_with_no_mutant_caught_by_its_own_tests_is_unasserted(proj):
    plan = proj.plan()
    rows = [{"id": m["id"], "status": "killed", "killed_by": [STRONG], "ran": [3, 3]} for m in plan["mutants"]]
    v = proj.check(proj.results(rows))
    weak = scenario(v, "Discount runs for any total")
    assert weak["verdict"] == verdicts.UNASSERTED and STRONG in weak["verdict_why"]    # another test failed them, not its own
    assert "other tests did" in weak["verdict_why"]
    assert scenario(v, "Big orders get twenty off")["verdict"] == "proven"


def test_a_scenario_with_a_mutant_caught_by_its_own_tests_keeps_its_verdict(proj):
    plan = proj.plan()
    rows = [{"id": m["id"], "status": "killed", "killed_by": [WEAK], "ran": [3, 3]} for m in plan["mutants"][:1]] + \
           [{"id": m["id"], "status": "survived", "killed_by": [], "ran": [3, 3]} for m in plan["mutants"][1:]]
    v = proj.check(proj.results(rows))
    assert scenario(v, "Discount runs for any total")["verdict"] == "proven"


def test_a_scenario_whose_tests_are_new_is_judged_too(proj):
    plan = proj.plan()
    rows = [{"id": m["id"], "status": "survived", "killed_by": [], "ran": [3, 3]} for m in plan["mutants"]]
    v = proj.check(proj.results(rows))
    for name in ("Big orders get twenty off", "Discount runs for any total"):          # both tests are new since the plan
        s = scenario(v, name)
        assert s["test_new"] and s["verdict"] == "unasserted"


def test_a_mutant_that_was_never_run_leaves_the_scenario_unjudged(proj):
    plan = proj.plan()
    rows = [{"id": m["id"], "status": "survived", "killed_by": [], "ran": [3, 3]} for m in plan["mutants"][:-1]]
    v = proj.check(proj.results(rows))
    assert scenario(v, "Discount runs for any total")["verdict"] == "proven"
    assert scenario(v, "Discount runs for any total")["mutants_note"].startswith("1 mutant")
    assert "not run" in loop.spec.verify_text(v)


def test_an_equivalent_mutant_counts_for_nothing(proj):
    plan = proj.plan()
    rows = [{"id": m["id"], "status": "equivalent", "why": f"same result for {m['id']}"} for m in plan["mutants"]]
    v = proj.check(proj.results(rows))
    assert scenario(v, "Discount runs for any total")["verdict"] == "proven"
    assert "equivalent, counted for nothing" in loop.spec.verify_text(v) and "same result for m-1" in loop.spec.verify_text(v)


def test_a_scenario_with_no_mutants_is_not_unasserted(tmp_path):
    before = "def tag(a):\n    return a\n"
    after = "def tag(a):\n    label = a\n    return a\n"
    tasks = "- [ ] 1.1 Change `tag` to name its value\n- [ ] 2.1 Add the test \"Tag names its value\"\n"
    spec = "## ADDED Requirements\n\n### Requirement: Tag\n#### Scenario: Tag names its value\n- **WHEN** a\n- **THEN** b\n"
    p = Project(tmp_path / "none", {"app/__init__.py": "", "app/calc.py": before,
                                    "tests/test_calc.py": "from app.calc import tag\n\n\ndef test_tag_names_its_value():\n    tag(1)\n"},
                {"app/calc.py": after}, tasks=tasks, spec=spec)
    p.implement()
    plan = p.plan()
    assert plan["count"] == 0 and plan["no_mutants"]
    v = p.check(p.results([]))
    assert scenario(v, "Tag names its value")["verdict"] != "unasserted"
    assert "no mutants for app/calc.py" in loop.spec.verify_text(v)


def test_results_made_for_other_code_are_refused(proj):
    plan = proj.plan()
    rows = [{"id": m["id"], "status": "killed", "killed_by": [STRONG], "ran": [3, 3]} for m in plan["mutants"]]
    path = proj.results(rows, plan="aaaaaaaaaaaa")
    assert "another plan" in proj.check(path)["error"]
    path = proj.results(rows)
    (proj.root / "app" / "calc.py").write_text(CALC_AFTER + "# edited after the plan\n")      # or a patch left applied
    assert "has changed since the plan" in proj.check(path)["error"]


def test_unasserted_does_not_block_by_default(proj):
    plan = proj.plan()
    rows = [{"id": m["id"], "status": "killed", "killed_by": [STRONG], "ran": [3, 3]} for m in plan["mutants"]]
    v = proj.check(proj.results(rows))
    assert scenario(v, "Discount runs for any total")["verdict"] == "unasserted"
    assert v["done_as_agreed"] and "unasserted" not in " ".join(v["why_not"])
    assert "Not blocking: needs you, unasserted." in loop.spec.verify_text(v)


def test_a_project_can_make_unasserted_block(proj):
    plan = proj.plan()
    (proj.root / "openspec" / "leyline.toml").write_text('[check]\nblocking = ["partial", "contradicted", "inconclusive", "unasserted"]\n')
    rows = [{"id": m["id"], "status": "killed", "killed_by": [STRONG], "ran": [3, 3]} for m in plan["mutants"]]
    v = proj.check(proj.results(rows))
    assert not v["done_as_agreed"] and "some scenarios are not proven" in v["why_not"]
    assert "strengthen" in " ".join(loop.next_after_check(v, "more-discount", for_agent=True))
    assert "Blocking: unasserted, partial, contradicted, inconclusive." in loop.spec.verify_text(v)


def test_without_results_check_is_as_it_was(proj):
    v = proj.check()
    page = loop.spec.verify_text(v)
    assert "unasserted" not in page and "Covered, but not asserted" not in page and "mutants" not in v
    assert all(s.get("verdict") != "unasserted" for s in v["scenarios"])
    assert "Blocking: partial, contradicted, inconclusive." in page


# -- a link the spec names ---------------------------------------------------------------------------------
CALC_MATH = CALC_AFTER.replace("def discount", "import math\n\n\ndef discount").replace("total - 20", "math.floor(total - 20)")


def with_math(tmp_path, tasks):
    p = Project(tmp_path / "math", {"app/__init__.py": "", "app/calc.py": CALC_BEFORE, "tests/test_calc.py": TESTS_BEFORE},
                {"app/calc.py": CALC_MATH, "tests/test_calc.py": TESTS_AFTER}, tasks=tasks)
    p.implement()
    return p


def test_a_link_the_spec_names_is_not_a_hold_up(tmp_path):
    p = with_math(tmp_path, TASKS.replace("take twenty off over 200", "take twenty off over 200, rounded down with `math`"))
    v = p.check()
    assert "error" not in v and v["new_dependencies"] == [] and [d["to"] for d in v["declared_dependencies"]] == ["math"]
    assert "new links between modules" not in v["why_not"] and v["done_as_agreed"]
    assert "named in the spec" in loop.spec.verify_text(v)


def test_a_link_the_spec_does_not_name_holds_the_change_up(tmp_path):
    p = with_math(tmp_path, TASKS)
    v = p.check()
    assert [d["to"] for d in v["new_dependencies"]] == ["math"] and not v["done_as_agreed"]
    assert "new links between modules" in v["why_not"]
