"""Problems a verification run on Parlance found, each on a small fixture of the same shape."""

from __future__ import annotations

import json
from pathlib import Path

from leyline import store
from leyline.indexer import index


def _write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _map(tmp_path: Path, files: dict):
    root = _write(tmp_path / "ws", files)
    db = tmp_path / "m.db"
    index(root, db)
    return store.connect(db)


def _links(con, channel: str):
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in con.execute(
        "SELECT src_id, dst_id, precision, attrs FROM edges WHERE kind = 'communicates'")
            if json.loads(r[3]).get("channel") == channel]


# -- 1. the program a launch call starts ----------------------------------------------------------------
ELECTRON = {
    "editor/electron/src/main.ts": '''import { spawn } from "child_process";
import { join, resolve } from "path";

function hostEntry(): string {
  const packaged = join(process.resourcesPath ?? "", "dist", "index.js");
  return resolve(__dirname, "..", "..", "host", "dist", "index.js");
}

function guidePath(): string {
  return resolve(__dirname, "..", "..", "..", "tooling", "GUIDE.md");
}

export function startServer(project: string) {
  const child = spawn(process.execPath, [hostEntry(), project], { stdio: ["ignore", "pipe", "pipe"] });
  return child;
}

export function initProject(target: string) {
  const args = [hostEntry(), "init", target];
  return spawn(process.execPath, args, { stdio: "pipe" });
}

export function openGuide() {
  return guidePath();
}
''',
    "editor/host/src/index.ts": '''import { createServer } from "http";

function serve() {
  createServer(() => {}).listen(4000);
}

serve();
''',
    "tooling/validate.py": '''import sys


def main():
    print(len(sys.argv))


if __name__ == "__main__":
    main()
''',
}


def test_a_program_a_helper_builds_is_that_program_not_a_word_elsewhere_in_the_file(tmp_path):
    """main.ts starts `spawn(process.execPath, [hostEntry(), ...])`: the host's built entry, whose source is
    host/src/index.ts. It used to be linked to the `tooling` module, named by another function's path."""
    con = _map(tmp_path, ELECTRON)
    procs = _links(con, "process")
    assert procs, "the launches are linked"
    assert not [p for p in procs if "tooling" in p[1]]
    by_src = {p[0].rsplit(".", 1)[-1]: p for p in procs}
    assert set(by_src) == {"startServer", "initProject"}
    for _, dst, _, attrs in by_src.values():
        assert "editor.host.src.index" in dst
        assert attrs["address"] == "editor/host/dist/index.js"


def test_a_helper_whose_path_cannot_be_worked_out_leaves_the_program_unknown(tmp_path):
    files = dict(ELECTRON)
    files["editor/electron/src/main.ts"] = files["editor/electron/src/main.ts"].replace(
        'return resolve(__dirname, "..", "..", "host", "dist", "index.js");', "return packaged;")
    con = _map(tmp_path, files)
    assert _links(con, "process") == []


# -- 2. wildcard routes ---------------------------------------------------------------------------------
LORE = {
    "host/src/lore-routes.ts": '''import type { FastifyInstance } from "fastify";

export function registerLoreRoutes(server: FastifyInstance): void {
  server.get("/api/lore/*", async (req, reply) => {
    return reply.send({ one: 1 });
  });
  server.get("/api/lore-doc/*", async (req, reply) => {
    return reply.send({ doc: 1 });
  });
  server.get("/api/lore-files", async () => {
    return { files: [] };
  });
  server.put("/api/lore/*", async (req, reply) => {
    return reply.send({ saved: true });
  });
}
''',
    "client/src/loreApi.ts": '''const rel = (file: string) => encodeURIComponent(file);

export const loreApi = {
  getDoc: (file: string) => fetch(`/api/lore-doc/${rel(file)}`).then((r) => r.json()),
  put: (file: string, text: string) => fetch(`/api/lore/${rel(file)}`, { method: "PUT", body: text }).then((r) => r.json()),
  getNested: (dir: string, name: string) => fetch(`/api/lore/${dir}/${name}.md`).then((r) => r.json()),
  listFiles: () => fetch("/api/lore-files").then((r) => r.json()),
};
''',
}


def test_a_wildcard_route_takes_the_rest_of_the_path(tmp_path):
    con = _map(tmp_path, LORE)
    got = {(s.rsplit(".", 1)[-1], a["address"]) for s, d, _, a in _links(con, "http")}
    assert ("getDoc", "GET /api/lore-doc/*") in got
    assert ("put", "PUT /api/lore/*") in got
    assert ("getNested", "GET /api/lore/*") in got
    assert ("listFiles", "GET /api/lore-files") in got
    assert not any(src == "listFiles" and addr != "GET /api/lore-files" for src, addr in got)
    assert not any(src == "getDoc" and addr != "GET /api/lore-doc/*" for src, addr in got)


def test_wildcard_segments_and_holes():
    from leyline.indexer import _fits_wildcard, _wildcard
    assert all(_wildcard(x) for x in ("*", "(.*)", "*splat", ":path*", ":path(.*)", "{*path}", "{**rest}"))
    assert not any(_wildcard(x) for x in ("lore", ":id", "{id}", "<int:id>"))
    assert _fits_wildcard(["api", "lore", "*"], ["api", "lore", "a", "b.md"])
    assert not _fits_wildcard(["api", "lore", "*"], ["api", "lore"])
    assert not _fits_wildcard(["api", "lore", "*"], ["api", "lore-files"])
    # a hole may be several segments: the route's parameters and the wildcard
    assert _fits_wildcard(["api", "lore", ":dir", "*"], ["api", "lore", "{}"])
    # but never a segment the route spells out
    assert not _fits_wildcard(["api", "lore", "*"], ["api", "{}"])


# -- 3. a diagram's note agrees with the drawing --------------------------------------------------------------
APP = {
    "app/__init__.py": "",
    "app/main.py": "from app.service import Service\n\n\ndef main():\n    Service().run(\"x\")\n\n\n"
                   "if __name__ == \"__main__\":\n    main()\n",
    "app/service.py": "class Service:\n    def run(self, name):\n        self.check(name)\n        return name\n\n"
                      "    def check(self, data):\n        return bool(data)\n\n"
                      "    def other(self):\n        return 1\n",
}


def test_changed_code_past_the_cap_that_the_drawing_reaches_is_shaded_not_left_out(tmp_path):
    from leyline import diagrams
    con = _map(tmp_path, APP)
    ids = {r[0]: r[1] for r in con.execute("SELECT name, id FROM nodes WHERE kind = 'callable'")}
    d = diagrams.sequence(con, [ids["run"], ids["check"], ids["other"]], max_focus=1)
    assert "check()" in d["mermaid"] and "changed: Service.check" in d["mermaid"]
    assert d["focus_left_out"] == ["Service.other"]
    legend = diagrams.legend(d)
    assert "Not drawn, to keep it readable: Service.other." in legend and "Service.check" not in legend


# -- 4. before and after pick callees the same way -------------------------------------------------------------
def _many(first: str = "") -> str:
    helpers = "".join(f"def h{i}():\n    return {i}\n\n\n" for i in range(30))
    calls = "".join(f"    h{i}()\n" for i in range(30))
    return helpers + f"def walk():\n{first}{calls}\n\nif __name__ == \"__main__\":\n    walk()\n"


def test_before_and_after_draw_the_same_callees_when_none_was_added_or_removed(tmp_path):
    from leyline import diagrams
    root, db = tmp_path / "ws", tmp_path / "m.db"
    _write(root, {"walker.py": _many()})
    index(root, db)
    con = store.connect(db)
    full = tmp_path / "before.db"
    con.execute("VACUUM INTO ?", (str(full),))
    con.close()
    (root / "walker.py").write_text(_many(first="    h29()\n"))   # h29 is now also called first: no call added
    index(root, db)
    con, before = store.connect(db), store.connect(full)
    walk = con.execute("SELECT id FROM nodes WHERE name = 'walk'").fetchone()[0]
    view = diagrams.for_change(before, con, [walk])
    assert diagrams.change_lines(view["changes"]) == [
        "No call or channel link into or out of the changed code was added or removed."]

    def callees(d):
        return [a["to"].rsplit(".", 1)[-1] for a in d["arrows"] if a["kind"] == "call" and a["from"] == walk]
    assert callees(view["before"]) == callees(view["after"])
    assert callees(view["after"])[0] == "h29" and len(callees(view["after"])) < 30
    assert diagrams.unbacked(before, view["before"]) == [] and diagrams.unbacked(con, view["after"]) == []


# -- 5h. a diagram starts at the product's entry point, not a benchmark -----------------------------------------
def test_a_diagram_starts_at_the_product_not_a_benchmark_script(tmp_path):
    from leyline import diagrams
    con = _map(tmp_path, {
        "app/__init__.py": "",
        "app/core.py": "def work(x):\n    return x + 1\n",
        "app/main.py": "from app.core import work\n\n\ndef serve():\n    return handle(1)\n\n\ndef handle(x):\n"
                       "    return work(x)\n\n\nif __name__ == \"__main__\":\n    serve()\n",
        "scripts/bench_work.py": "from app.core import work\n\n\nif __name__ == \"__main__\":\n    work(2)\n",
    })
    work = con.execute("SELECT id FROM nodes WHERE name = 'work'").fetchone()[0]
    d = diagrams.sequence(con, [work])
    assert "app/main.py" in d["mermaid"] and "bench_work" not in d["mermaid"]


# -- 5c. a removal is not an edit of the file around it; a constant is named ----------------------------------
def _quick_repo(tmp_path, monkeypatch):
    import test_quick as q
    root = tmp_path / "repo"
    _write(root, q.FILES)
    q.git(root, "init", "-q", "-b", "main")
    q.git(root, "add", "-A")
    q.git(root, "commit", "-qm", "base")
    monkeypatch.chdir(root)
    q.run("map", ".")
    return root, q


def test_a_constant_edited_outside_the_named_code_is_named(tmp_path, monkeypatch):
    root, q = _quick_repo(tmp_path, monkeypatch)
    code, page = q.run("quick", "make retries 4", "--about", "RETRIES")
    # 5j: what "first from" meant, said plainly
    assert "Runs into it: 2 places in 1 module; the nearest are `get` and `head`, which call the changed code." in page, page
    q.run("quick", "make head shorter", "--about", "head", "--tests", "-", stdin=q.PASSING)
    q.edit(root, "app/net.py", "    return fetch(url)[:4]", "    return fetch(url)[:3]")
    q.edit(root, "app/net.py", "RETRIES = 5", "RETRIES = 3")
    code, page = q.run("quick", "--done", "quick-make-head-shorter", "--tests", "-", stdin=q.PASSING)
    assert "- `RETRIES` (edited, app/net.py)" in page and "the top level of" not in page
    assert "--about RETRIES" in page


KEYS_TS = '''export const A = 1;
const LORE = /^[a-z]+$/;

/** "lore/x.md" to "x". */
export function stem(file: string): string {
  return file.slice(5);
}

/** The key for a paragraph. */
export function paragraphKey(file: string, n: number): string {
  const s = stem(file);
  return `${s}/${n}`;
}
'''


def test_removing_a_function_is_not_an_edit_of_its_files_top_level(tmp_path, monkeypatch):
    import test_quick as q
    root = tmp_path / "repo"
    _write(root, {"src/keys.ts": KEYS_TS, "src/keys.test.ts": 'import { it } from "vitest";\nimport { stem } from "./keys";\n\n'
                  'it("stems a lore file", () => {\n  stem("lore/a.md");\n});\n'})
    import os
    os.symlink("src", root / "linked")    # 5l: said once per command, though a pull request maps twice
    q.git(root, "init", "-q", "-b", "main")
    q.git(root, "add", "-A")
    q.git(root, "commit", "-qm", "base")
    monkeypatch.chdir(root)
    q.run("map", ".")
    # the function goes, its comment stays, and its one caller inlines it
    q.edit(root, "src/keys.ts", "export function stem(file: string): string {\n  return file.slice(5);\n}\n", "")
    q.edit(root, "src/keys.ts", "const s = stem(file);", "const s = file.slice(5);")
    code, page = q.run("pr", "main", "--id", "drop")
    assert "Removed: `stem`" in page and "Edited: `paragraphKey`" in page, page
    assert "(top level)" not in page and "keys.ts.<module>" not in page and "Outside any function" not in page
    assert page.count("left out symlink to a directory: linked") == 1, page
    # 5i: one test, said in the singular
    assert "**Likely to fail:** `stems a lore file`: it calls code whose signature changed, or that was removed, and was" \
           " not edited." in page, page


# -- 5d. a registrar is not edited when only its handler is --------------------------------------------------
ROUTES_TS = '''export function registerRoutes(server: any): void {
  server.get("/api/files", async () => {
    return { files: [] };
  });
  server.get("/api/other", async () => {
    return { other: 1 };
  });
}
'''


def test_a_second_review_does_not_call_a_registrar_edited_when_only_its_handler_is(tmp_path, monkeypatch):
    import test_quick as q
    root = tmp_path / "repo"
    _write(root, {"host/routes.ts": ROUTES_TS})
    q.git(root, "init", "-q", "-b", "main")
    q.git(root, "add", "-A")
    q.git(root, "commit", "-qm", "base")
    q.git(root, "checkout", "-q", "-b", "feature")
    q.edit(root, "host/routes.ts", "return { other: 1 };", "return { other: 2 };")
    q.git(root, "commit", "-qam", "other is 2")
    monkeypatch.chdir(root)
    code, page = q.run("pr", "main")
    q.edit(root, "host/routes.ts", "return { files: [] };", "return { paths: [] };")
    q.git(root, "commit", "-qam", "files are paths")
    code, page = q.run("pr", "main")
    since = next(ln for ln in page.splitlines() if ln.startswith("Between "))
    assert "GET /api/files" in since and "registerRoutes" not in since, since


# -- 5e. skipped tests are said apart ---------------------------------------------------------------------------
def test_skipped_tests_are_said_apart_from_passed_and_failed(tmp_path):
    from leyline import diff
    con = store.connect(tmp_path / "t.db")
    diff.record_tests(con, "b", [{"name": "a", "status": "pass"}, {"name": "s", "status": "skip"}])
    t = diff.record_tests(con, "a", [{"name": "a", "status": "pass"}, {"name": "n", "status": "pass"},
                                     {"name": "s", "status": "skip"}])
    d = diff.test_delta(con, "b", "a")
    assert diff.passed_text(d["before"]) == "1 of 1 passed (1 skipped)"
    assert diff.passed_text(d["after"]) == "2 of 2 passed (1 skipped)"
    assert diff.passed_pair_text(d["before"], d["after"]) == "1 of 1 passed before, 2 of 2 after (1 skipped in each run)"
    assert diff.recorded_text(t) == "2 pass, 0 fail, 1 skipped"
    assert diff.recorded_text({"pass": 3}) == "3 pass, 0 fail"


# -- 5g. a new function at the top of a file, said in a sentence --------------------------------------------
def test_the_plan_summary_says_a_new_function_in_its_file():
    from leyline import spec
    b = {"why": "", "impact": {"error": "x"}, "scenarios": [],
         "tasks": [{"labels": ["walkEffect"], "new": [{"name": "case_x", "label": "build_cases.py: case_x"}]}]}
    assert "it changes walkEffect and adds `case_x` in build_cases.py." in spec._plain_summary(b)


# -- 5k. a broad hub ranks below code that is specific to the focus --------------------------------------------
def test_context_ranks_a_hub_below_code_specific_to_the_focus(tmp_path):
    from leyline import context
    others = "".join(f"def other{i}():\n    raise Failure(\"{i}\")\n\n\n" for i in range(30))
    con = _map(tmp_path, {
        "app/__init__.py": "",
        "app/errors.py": "class Failure(Exception):\n    pass\n",
        "app/flow.py": "from app.errors import Failure\n\n\ndef focus():\n    step1()\n    step2()\n    step3()\n\n\n"
                       "def step1():\n    specific()\n    raise Failure(\"1\")\n\n\ndef step2():\n    raise Failure(\"2\")\n\n\n"
                       "def step3():\n    raise Failure(\"3\")\n\n\ndef specific():\n    return 1\n",
        "app/others.py": "from app.errors import Failure\n\n\n" + others,
    })
    g = context.graph(con)
    ids = {g.name[k]: k for k in range(len(g.ids)) if g.kind[k] in ("callable", "type")}
    p = context.rank(g, {ids["focus"]: 1.0})
    score = lambda name: p.get(ids[name], 0) * g.boost[ids[name]] * g.hub[ids[name]]   # what the outline orders by
    assert score("specific") > score("Failure")
    text = context.build(con, "focus", 200)["text"]
    assert text.index("def specific") < text.index("class Failure") if "class Failure" in text else "def specific" in text


# -- 5f. the module counts add up to what changes -------------------------------------------------------------
def test_new_code_counts_in_its_module(tmp_path):
    from leyline import change
    con = _map(tmp_path, APP)
    run = con.execute("SELECT id FROM nodes WHERE name = 'run'").fetchone()[0]
    main_file = con.execute("SELECT id FROM nodes WHERE kind = 'file' AND path = 'app/main.py'").fetchone()[0]
    r = change.assess(con, "x", [{"id": run, "action": "behavior"},
                                 {"action": "add", "name": "audit", "parent": main_file}])
    assert r["summary"]["changed"] + r["summary"]["added"] == sum(m["changed"] for m in r["by_module"]) == 2


# -- 5a. a test's name, cut short, is quoted and cut at a word ---------------------------------------------------
def test_a_long_test_name_is_quoted_and_cut_at_a_word(tmp_path):
    from leyline import diagrams
    assert diagrams.cut("attributes every row exactly as the .xlsx lines say", 40) == "attributes every row exactly as the…"
    assert diagrams.cut("short", 40) == "short"
    con = _map(tmp_path, {
        "src/sheet.ts": "export function attribute(rows: string[]): number {\n  return rows.length;\n}\n",
        "src/sheet.test.ts": 'import { it } from "vitest";\nimport { attribute } from "./sheet";\n\n'
                             'it("attributes every row exactly as the .xlsx lines say, even when a row is blank", () => {\n'
                             '  attribute([]);\n});\n'})
    f = con.execute("SELECT id FROM nodes WHERE name = 'attribute'").fetchone()[0]
    m = diagrams.sequence(con, [f])["mermaid"]
    assert 'starts at the test "attributes every row exactly as the .xlsx…"' in m, m
    assert "..." not in m


# -- 5b. a script is named by its file, and the product's entry points come first --------------------------------
def test_entry_points_are_named_by_file_and_the_product_comes_first(tmp_path):
    from leyline import change
    con = _map(tmp_path, {
        "src/sheet.ts": "export function attribute(rows: string[]): number {\n  return rows.length;\n}\n",
        "scripts/bench-sheet.ts": 'import { attribute } from "../src/sheet";\nattribute(["a"]);\n',
        "src/index.ts": 'import { attribute } from "./sheet";\nattribute(["b"]);\n'})
    f = con.execute("SELECT id FROM nodes WHERE name = 'attribute'").fetchone()[0]
    names = [e["name"] for e in change.assess(con, "x", [{"id": f, "action": "behavior"}])["entry_points_affected"]]
    assert names == ["src/index.ts", "scripts/bench-sheet.ts"], names


# -- the tour reads code with its tests beside it as code ------------------------------------------------------
def test_the_tour_reads_code_with_its_tests_beside_it_as_code(tmp_path):
    from leyline import tours
    con = _map(tmp_path, {
        "src/lib/grocery.ts": "export function weeksSince(d: number): number {\n  return d / 7;\n}\n"
                              "export function isDue(d: number): boolean {\n  return weeksSince(d) >= 1;\n}\n",
        "src/lib/units.ts": "export function toBase(x: number): number {\n  return x * 1000;\n}\n",
        "src/lib/grocery.test.ts": 'import { it, expect } from "vitest";\nimport { isDue } from "./grocery";\n\n'
                                   'it("is due after a week", () => {\n  expect(isDue(8)).toBe(true);\n});\n',
        "src/screens/List.ts": 'import { isDue } from "../lib/grocery";\nimport { toBase } from "../lib/units";\n\n'
                               "export function show(d: number) {\n  return isDue(d) ? toBase(d) : 0;\n}\n",
    })
    repo = con.execute("SELECT id FROM nodes WHERE kind = 'repo'").fetchone()[0]
    stops = tours.get(con, f"tour:orientation:{repo}")["stops"]
    titles = [s["title"] for s in stops]
    assert "The foundation: lib" in titles, titles
    tested = next(s["narrative"] for s in stops if s["title"] == "How it is tested")
    assert "2 of the 4 functions outside test code" in tested, tested


def test_the_tour_tells_two_modules_of_the_same_name_apart(tmp_path):
    from leyline import tours
    con = _map(tmp_path, {
        "src/lib/units.ts": "export function toBase(x: number): number {\n  return x * 1000;\n}\n",
        "src/app/main.ts": 'import { toBase } from "../lib/units";\n\nexport function run() {\n  return toBase(1);\n}\n',
        "scripts/lib/args.ts": "export function parse(a: string[]): string {\n  return a[0];\n}\n",
        "scripts/tool/migrate.ts": 'import { parse } from "../lib/args";\n\nexport function go() {\n  return parse([]);\n}\n',
    })
    repo = con.execute("SELECT id FROM nodes WHERE kind = 'repo'").fetchone()[0]
    text = " ".join(s["title"] + " " + s["narrative"] for s in tours.get(con, f"tour:orientation:{repo}")["stops"])
    assert "src/lib" in text and "scripts/lib" in text, text
    assert "Module: lib " not in text and "The foundation: lib " not in text, text


def test_the_tour_writes_each_language_by_its_own_name(tmp_path):
    from leyline import tours
    con = _map(tmp_path, {
        "a/one.py": "def one():\n    return 1\n", "a/two.py": "def two():\n    return 2\n",
        "b/three.ts": "export function three(): number {\n  return 3;\n}\n",
        "c/four.go": "package c\n\nfunc Four() int {\n\treturn 4\n}\n",
    })
    repo = con.execute("SELECT id FROM nodes WHERE kind = 'repo'").fetchone()[0]
    first = tours.get(con, f"tour:orientation:{repo}")["stops"][0]["narrative"]
    assert "written in Python, " in first and "TypeScript" in first and "Go" in first, first
    assert "typescript" not in first and " go" not in first, first


def test_state_counts_writers_in_code_with_its_tests_beside_it(tmp_path):
    from leyline import query
    con = _map(tmp_path, {
        "src/lib/types.ts": "export interface Ingredient {\n  name: string;\n  grams: number;\n}\n",
        "src/screens/Edit.ts": 'import { Ingredient } from "../lib/types";\n\n'
                               "export function save(i: Ingredient, g: number) {\n  i.grams = g;\n}\n",
        "src/screens/Edit.test.ts": 'import { it } from "vitest";\nimport { save } from "./Edit";\n\n'
                                    'it("saves", () => {\n  const i = { name: "a", grams: 0 };\n  save(i, 2);\n'
                                    '  i.name = "b";\n});\n',
    })
    got = query.shared_state(con)
    assert [f["name"] for f in got.get("fields", [])] == ["Ingredient.grams"], got
