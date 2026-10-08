"""`leyline outline` and `name_part`: a large module split into parts (folders, groups of files where a folder is
flat), each described, and names for the parts that outlive a re-map."""

import json
import shutil
from pathlib import Path

import pytest

from leyline import cli, explain, export, outline, query, store
from leyline.indexer import index

pytest.importorskip("networkx")

ORDERS = ["Order", "Cart", "Payment", "Receipt", "Invoice"]
REPORTS = ["Report", "Chart", "Table", "Axis", "Legend", "Series", "Export"]
PLUGINS = 14


def _cluster(names: list[str], prefix: str) -> dict:
    """One file per class; each class makes every other one of the group and calls it, so they form one tight group."""
    files = {}
    for n in names:
        others = [o for o in names if o != n]
        files[f"{n.lower()}.py"] = (
            "".join(f"from .{o.lower()} import {o}\n" for o in others) + "\n\n"
            f"class {n}:\n"
            f"    def run(self):\n"
            + "".join(f"        {o.lower()} = {o}()\n" for o in others)
            + "        return [" + ", ".join(f"{o.lower()}.step(1)" for o in others) + "]\n\n"
            f"    def step(self, n):\n        return {prefix}_helper(n)\n\n\n"
            f"def {prefix}_helper(n):\n    return n + 1\n")
    return files


def make_repo(root: Path, orders=ORDERS, reports=REPORTS) -> Path:
    """svc/ (a Python project): pkg/core/ is flat (two groups of classes that only call within their group), pkg/api/
    holds two route files, pkg/plugins/ more subfolders than a level shows, and tests/ tests the orders."""
    if root.exists():
        shutil.rmtree(root)
    svc = root / "svc"
    core = svc / "src" / "shop" / "core"
    core.mkdir(parents=True)
    (svc / "pyproject.toml").write_text("[project]\nname = 'svc'\n")
    (svc / "src" / "shop" / "__init__.py").write_text("")
    for name, text in {**_cluster(orders, "order"), **_cluster(reports, "report")}.items():
        (core / name).write_text(text)
    api = svc / "src" / "shop" / "api"
    api.mkdir()
    (api / "__init__.py").write_text("")
    (api / "orders.py").write_text(f"from shop.core.{orders[0].lower()} import {orders[0]}\n\n\n"
                                   f"def place_order():\n    o = {orders[0]}()\n    return o.run()\n\n\n"
                                   f"def cancel_order():\n    return place_order()\n")
    (api / "reports.py").write_text(f"from shop.core.{reports[0].lower()} import {reports[0]}\n\n\n"
                                    f"def show_report():\n    r = {reports[0]}()\n    return r.run()\n")
    for k in range(PLUGINS):
        p = svc / "src" / "shop" / "plugins" / f"p{k:02d}"
        p.mkdir(parents=True)
        (p / "plugin.py").write_text(f"def plugin_{k}():\n" + "    x = 1\n" * (k + 1) + "    return x\n")
    tests = svc / "tests"
    tests.mkdir()
    (tests / "test_orders.py").write_text("from shop.api.orders import place_order\n\n\n"
                                          "def test_place_order():\n    got = place_order()\n    assert got\n")
    return root


@pytest.fixture
def mapped(tmp_path):
    repo = make_repo(tmp_path / "repo")
    db = tmp_path / "s.db"
    index(repo, db, "r")
    con = store.connect(db)
    yield repo, db, con
    con.close()


def by_id(r: dict, pid: str) -> dict:
    return next(p for p in r["parts"] if p["id"] == pid)


def test_the_repository_then_a_module_then_a_flat_folder(mapped):
    repo, db, con = mapped
    top = outline.outline(con)
    assert top["kind"] == "repo" and top["split"] == "modules"
    svc = next(p for p in top["parts"] if p["id"] == "r:module:svc")
    assert svc["size"]["files"] >= 2 + len(ORDERS) + len(REPORTS) + PLUGINS

    # A module by its path. src/shop holds all but the test file: the module is shown from there, the test beside it.
    mod = outline.outline(con, "svc")
    assert mod["id"] == "r:module:svc" and mod["split"] == "folders" and mod["shown_from"] == "svc/src/shop/"
    assert [p["id"] for p in mod["parts"]] == ["r:dir:svc/src/shop/core", "r:dir:svc/src/shop/plugins",
                                              "r:dir:svc/src/shop/api", "r:dir:svc/tests",
                                              "r:file:svc/src/shop/__init__.py"]   # largest first
    core = by_id(mod, "r:dir:svc/src/shop/core")
    assert core["name"] == "src/shop/core/" and core["drill"] == 'module_outline("r:dir:svc/src/shop/core")'
    assert sorted(x["files"] for x in core["parts"]) == [len(ORDERS), len(REPORTS)]   # depth 2: the parts inside
    assert by_id(mod, "r:dir:svc/tests")["entry_points"]["tests"] == 1
    assert "shown from svc/src/shop/" in outline.text(mod)

    inside = outline.outline(con, "r:dir:svc/src/shop", depth=1)   # the folder itself, by its id
    api = by_id(inside, "r:dir:svc/src/shop/api")
    assert api["size"] == {"files": 3, "functions": 3, "lines": api["size"]["lines"]}
    assert {u["name"] for u in api["uses"]} == {"core/"} and api["used_by"][0]["name"].startswith("tests")
    assert "parts" not in api   # depth 1
    core = by_id(inside, "r:dir:svc/src/shop/core")
    assert core["used_by"][0]["id"] == "r:dir:svc/src/shop/api" and core["busiest"][0]["flows"] >= 1

    flat = outline.outline(con, "r:dir:svc/src/shop/core")
    assert flat["split"] == "groups of files"
    groups = [p for p in flat["parts"] if p["kind"] == "system"]
    assert len(groups) == 2 and len(flat["parts"]) == 2
    orders = next(g for g in groups if g["size"]["files"] == len(ORDERS))
    assert orders["id"].startswith("r:dir:svc/src/shop/core#system:") and orders["name"].endswith(" group")
    # Order.run, and the step and helper of each other class it calls
    assert orders["on_a_test_path"] == f"{1 + 2 * (len(ORDERS) - 1)} of {3 * len(ORDERS)} functions"
    reports = next(g for g in groups if g is not orders)
    assert reports["on_a_test_path"].startswith("0 of")
    assert outline.outline(con, orders["id"])["parts"][0]["kind"] == "file"   # a group drills into its files

    leaf = outline.outline(con, "r:file:svc/src/shop/api/orders.py")
    assert "parts" not in leaf and [k["name"] for k in leaf["key"]][0] == "place_order"
    assert "name-part r:file:svc/src/shop/api/orders.py" in outline.text(leaf)


def test_a_level_shows_twelve_parts_and_the_rest_behind_more(mapped):
    repo, db, con = mapped
    r = outline.outline(con, "r:dir:svc/src/shop/plugins", depth=1)
    assert len(r["parts"]) == outline.MAX_PARTS
    more = r["parts"][-1]
    assert more["kind"] == "more" and more["id"] == "r:dir:svc/src/shop/plugins@more"
    assert len(more["holds"]) == PLUGINS - outline.MAX_PARTS + 1
    # Largest first, so the smallest plugins are the ones behind `more`, and drilling into it shows them.
    assert r["parts"][0]["id"] == f"r:dir:svc/src/shop/plugins/p{PLUGINS - 1:02d}"
    rest = outline.outline(con, more["id"])
    assert {p["id"] for p in rest["parts"]} == {"r:dir:svc/src/shop/plugins/p00", "r:dir:svc/src/shop/plugins/p01",
                                               "r:dir:svc/src/shop/plugins/p02"}
    assert "error" in outline.name_part(con, more["id"], "Rest", "x", ["r:module:svc"])
    assert "no module or part" in outline.outline(con, "nowhere/at/all")["error"]


def test_names_survive_a_remap_follow_a_moved_group_and_go_stale(mapped, tmp_path):
    repo, db, con = mapped
    flat = outline.outline(con, "r:dir:svc/src/shop/core")
    reports = next(p for p in flat["parts"] if p["size"]["files"] == len(REPORTS))
    chart = con.execute("SELECT id FROM nodes WHERE kind = 'type' AND name = 'Chart'").fetchone()[0]
    assert "evidence" in outline.name_part(con, reports["id"], "Reporting", "Draws reports.")["error"]
    assert outline.name_part(con, reports["id"], "Reporting", "Draws reports.", [chart])["files"] == len(REPORTS)
    assert outline.name_part(con, "r:dir:svc/src/shop/api", "HTTP routes", "What clients call.", layer="intent")["layer"] == "intent"
    con.close()

    index(make_repo(repo), db, "r")   # the same code again
    con = store.connect(db)
    flat = outline.outline(con, "r:dir:svc/src/shop/core")
    got = by_id(flat, reports["id"])
    assert got["name"] == "Reporting" and got["summary"] == "Draws reports." and "stale" not in got
    assert got["label"] == reports["name"] and flat["names"] == "1 of 2 parts has a name"
    pkg = outline.outline(con, "r:dir:svc/src/shop")
    api = by_id(pkg, "r:dir:svc/src/shop/api")
    assert api["name"] == "HTTP routes" and api["named_by"] == "the person"
    assert {u["name"] for u in by_id(pkg, "r:dir:svc/src/shop/core")["used_by"]} >= {"HTTP routes"}   # others say it too
    assert "Reporting" in outline.text(flat) and "HTTP routes" in outline.text(pkg)
    con.close()

    # The group's anchor is renamed, so its id changes; the name follows the files it shares with the old group.
    anchor = reports["id"].split("#system:")[1]
    moved = [("Zed" if n == anchor else n) for n in REPORTS]
    index(make_repo(repo, reports=moved), db, "r")
    con = store.connect(db)
    flat = outline.outline(con, "r:dir:svc/src/shop/core")
    now = next(p for p in flat["parts"] if p["size"]["files"] == len(REPORTS))
    assert now["id"] != reports["id"] and now["name"] == "Reporting" and now["named_as"] == reports["id"]
    con.close()

    # Back to the old anchor, but five of the seven files are new: the name may be stale.
    fresh = [anchor] + [n for n in REPORTS if n != anchor][:1] + ["Plot", "Scale", "Key", "Line", "Grid"]
    index(make_repo(repo, reports=fresh), db, "r")
    con = store.connect(db)
    again = by_id(outline.outline(con, "r:dir:svc/src/shop/core"), reports["id"])
    assert again["name"] == "Reporting" and again["stale"].startswith("may be stale: it kept 29%")
    con.close()


def test_names_show_in_overview_explain_path_and_the_map_page(mapped, monkeypatch):
    repo, db, con = mapped
    place = con.execute("SELECT id FROM nodes WHERE name = 'place_order'").fetchone()[0]
    assert outline.name_part(con, "r:module:svc", "Shop service", "Takes orders and draws reports.", [place])["name"]
    assert outline.name_part(con, "r:dir:svc/src/shop/api", "HTTP routes", "What clients call.", [place])["name"]
    o = query.overview(con)
    m = next(x for x in o["repos"][0]["modules"] if x["id"] == "r:module:svc")
    assert m["title"] == "Shop service" and m["summary"] == "Takes orders and draws reports."
    assert [x["name"] for x in o["named_parts"]] == ["HTTP routes"]

    walk = explain.explain_path(con, place)
    assert walk["steps"][0]["part"] == "HTTP routes"           # the most specific named part
    assert any(s.get("part") == "Shop service" for s in walk["steps"][1:])
    assert "[in HTTP routes]" in walk["text"]

    monkeypatch.setattr(outline, "MAP_MIN_FILES", 10)
    g = export.graph(con, False)
    parts = [n for n in g["nodes"] if n["k"] == "part"]
    top = [n for n in parts if g["nodes"][n["p"]]["k"] == "module"]
    assert {n["n"] for n in top} >= {"src/shop/core/", "tests/", "HTTP routes"}
    api = next(n for n in parts if n["i"] == "part:r:dir:svc/src/shop/api")
    assert api["n"] == "HTTP routes" and api["x"]["summary"] == "What clients call." and api["x"]["files"] == 3
    listed = {p["id"]: p for p in g["parts"]}
    assert len(listed["r:dir:svc/src/shop/api"]["files"]) == 3 and g["nodes"][listed["r:dir:svc/src/shop/api"]["i"]] is api


def test_the_commands(mapped, capsys, monkeypatch):
    repo, db, con = mapped
    assert cli.main(["--db", str(db), "outline", "svc"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("svc/ (module") and "Split by folders" in out and "leyline outline <part id>" in out
    assert cli.main(["--db", str(db), "outline", "r:dir:svc/src/shop/core", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["split"] == "groups of files"
    assert cli.main(["--db", str(db), "name-part", "r:dir:svc/src/shop/api", "HTTP routes", "--summary", "What clients call.",
                     "--intent"]) == 0
    assert "Named r:dir:svc/src/shop/api: HTTP routes. What clients call." in capsys.readouterr().out
    assert cli.main(["--db", str(db), "name-part", "r:dir:svc/src/shop/api", "HTTP routes"]) == 1
    assert "evidence" in capsys.readouterr().err
    assert cli.main(["--db", str(db), "outline", "nowhere"]) == 1
    assert cli.main(["--db", str(db), "overview"]) == 0


@pytest.fixture(scope="module")
def browser():
    sync = pytest.importorskip("playwright.sync_api")
    try:
        p = sync.sync_playwright().start()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"playwright did not start: {e}")
    try:
        b = p.chromium.launch()
    except Exception as e:
        p.stop()
        pytest.skip(f"no Chromium to drive: {e}")
    yield b
    b.close()
    p.stop()


def test_the_map_page_opens_a_large_module_into_its_parts(mapped, browser, tmp_path, monkeypatch):
    repo, db, con = mapped
    place = con.execute("SELECT id FROM nodes WHERE name = 'place_order'").fetchone()[0]
    outline.name_part(con, "r:dir:svc/src/shop/api", "HTTP routes", "What clients call.", [place])
    monkeypatch.setattr(outline, "MAP_MIN_FILES", 10)
    page = tmp_path / "map.html"
    page.write_text(export.page(con))
    pg = browser.new_page(viewport={"width": 1280, "height": 860})
    problems = []
    pg.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: problems.append(str(e)))
    pg.goto(page.as_uri())
    pg.wait_for_selector("#nodes .node", timeout=20000)
    boxes = lambda: pg.evaluate("[...document.querySelectorAll('#nodes .node')].map((g) => g.getAttribute('aria-label'))")
    pg.dblclick('#nodes .node[aria-label="svc"]')
    assert set(boxes()) == {"src/shop/core/", "src/shop/plugins/", "HTTP routes", "tests/", "__init__.py"}
    assert "3 files" in pg.locator('#nodes .node[aria-label="HTTP routes"]').text_content()
    assert "PARTS" in pg.locator("#side").inner_text().upper()
    pg.click('#nodes .node[aria-label="HTTP routes"]')
    side = pg.locator("#side").inner_text()
    assert "What clients call." in side and "DEPENDS ON" in side.upper()
    pg.dblclick('#nodes .node[aria-label="src/shop/core/"]')            # its parts: the two groups
    shown = boxes()
    assert sum(1 for b in shown if b.endswith(" group")) == 2
    assert pg.locator("#crumbs").inner_text().split() == ["r", "/", "svc", "/", "src/shop/core/"]
    group = next(b for b in shown if b.endswith(" group"))
    pg.dblclick(f'#nodes .node[aria-label="{group}"]')                    # a group of files: its types
    assert len(boxes()) >= len(ORDERS) and pg.locator("#crumbs").inner_text().split("\n")[-1] == group
    assert not problems, problems
    pg.close()
