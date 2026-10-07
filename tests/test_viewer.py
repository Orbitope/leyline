"""The map page: what export puts in it, and, when Chromium is installed, the page itself in a real browser."""
import json
import re
import shutil
from pathlib import Path

import pytest

from leyline import export, spec, store
from leyline.indexer import index

FIXTURE2 = Path(__file__).parent / "fixture2"
FIXTURE_WS = Path(__file__).parent / "fixture_ws"


def placed(g: dict) -> dict:
    """{level: {node id: [row, x]}} from an exported graph."""
    return {lvl: {g["nodes"][int(i)]["i"]: v for i, v in at.items()} for lvl, at in g["layout"].items()}


def plan_a_change(work: Path, db: Path) -> None:
    """A change written as an OpenSpec folder and planned, without running any tests."""
    ch = work / "openspec" / "changes" / "loud-engine"
    (ch / "specs" / "engine").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Loud engine\n\n## Why\nNames are hard to read in logs.\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `Engine.start` to return the name in upper case\n"
                                 "- [ ] 1.2 Add `Engine.shout`, the name with an exclamation mark\n")
    (ch / "specs" / "engine" / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Loud names\nThe engine SHALL report its name loudly.\n\n"
        "#### Scenario: Start\n- **WHEN** an engine starts\n- **THEN** it returns its name in upper case\n")
    c = store.connect(db)
    assert "error" not in spec.brief(c, ch)
    c.close()


def test_layout_is_remembered_across_a_reindex(tmp_path):
    work, db = tmp_path / "repo", tmp_path / "s.db"
    shutil.copytree(FIXTURE2, work)
    index(work, db, "f2")
    c = store.connect(db)
    memory = export.memory_path(c)
    assert memory == db.with_suffix(".layout.json")
    first = placed(export.graph(c, False, memory))
    assert memory.is_file() and "repo:f2" in first and any(k.startswith("module:") for k in first)
    assert placed(export.graph(c, False, memory)) == first        # the same store gives the same map
    assert placed(export.graph(c, False)) == first                # and so does starting afresh
    c.close()

    # New code: a module that uses pkg, and a new class in pkg. Everything that was there stays put.
    (work / "py" / "extra").mkdir()
    (work / "py" / "extra" / "use.py").write_text("from pkg.core import Engine\n\n\ndef go():\n    return Engine('x').start()\n")
    (work / "py" / "src" / "pkg" / "more.py").write_text("class Loud:\n    def say(self):\n        return 'hi'\n")
    index(work, db, "f2")
    c = store.connect(db)
    second = placed(export.graph(c, False, memory))
    c.close()
    assert len(second["repo:f2"]) > len(first["repo:f2"])
    for lvl, at in first.items():
        for nid, where in at.items():
            assert second[lvl][nid] == where, (lvl, nid)
    for nid, (row, x) in second["repo:f2"].items():                   # and the new module found room of its own
        if nid not in first["repo:f2"]:
            assert all(abs(x - ox) >= 70 for oid, (orow, ox) in first["repo:f2"].items() if orow == row)


def test_page_carries_changes_and_workspaces(tmp_path):
    work, db = tmp_path / "repo", tmp_path / "s.db"
    shutil.copytree(FIXTURE2, work)
    index(work, db, "f2")
    plan_a_change(work, db)
    c = store.connect(db)
    g = export.graph(c, False)
    ch = g["changes"][0]
    assert ch["name"] == "loud-engine" and ch["folder"] == "openspec/changes/loud-engine/"
    assert "## 1. What code will be written" in ch["page"] and [t["key"] for t in ch["tasks"]] == ["1.1", "1.2"]
    assert g["nodes"][ch["tasks"][0]["nodes"][0]]["n"] == "start" and ch["tasks"][1]["new"][0]["name"] == "Engine.shout"
    assert any(v["id"] == ch["plan_view"] and v["change_id"] == ch["id"] for v in g["views"])
    page = export.page(c, open_change="loud-engine")
    assert '"start":{"change":"loud-engine"}' in page and "https://" not in page.split("<script")[0]
    c.close()

    ws = tmp_path / "ws.db"
    index([FIXTURE_WS / "wsapp", FIXTURE_WS / "wslib"], ws)
    c = store.connect(ws)
    g = export.graph(c)
    assert set(g["layout"]["ws"]) == {str(i) for i, n in enumerate(g["nodes"]) if n["k"] == "repo"}
    assert any(k.startswith("wsapp/") for k in g["sources"]) and any(k.startswith("wslib/") for k in g["sources"])
    assert "wsapp + wslib" in export.page(c)
    c.close()


def test_a_large_page_is_compressed(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    index(FIXTURE2, db, "f2")
    c = store.connect(db)
    monkeypatch.setattr(export, "COMPRESS_OVER", 1000)
    page = export.page(c)
    c.close()
    assert '<script id="leyline-data" type="application/gzip+base64">' in page
    import base64
    import gzip
    data = re.search(r'type="application/gzip\+base64">(.*?)</script>', page, re.S).group(1)
    assert json.loads(gzip.decompress(base64.b64decode(data)))["nodes"][0]["k"] == "repo"


# -- in a browser ---------------------------------------------------------------------------------
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


def open_page(browser, path: Path, width=1280, scheme="light"):
    ctx = browser.new_context(viewport={"width": width, "height": 860}, color_scheme=scheme)
    pg = ctx.new_page()
    problems = []
    pg.on("console", lambda m: problems.append(m.text) if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: problems.append(str(e)))
    pg.on("request", lambda r: problems.append("fetched " + r.url) if not r.url.startswith(("file:", "data:", "blob:")) else None)
    pg.goto(path.as_uri())
    pg.wait_for_selector("#nodes .node", timeout=20000)
    pg.wait_for_timeout(200)
    return ctx, pg, problems


def positions(pg) -> dict:
    return pg.evaluate("""Object.fromEntries([...document.querySelectorAll('#nodes .node')]
        .map((g) => [g.getAttribute('aria-label'), g.getAttribute('transform')]))""")


def test_page_in_a_browser(browser, tmp_path):
    work, db = tmp_path / "repo", tmp_path / "s.db"
    shutil.copytree(FIXTURE2, work)
    index(work, db, "f2")
    plan_a_change(work, db)
    c = store.connect(db)
    a, b = tmp_path / "a.html", tmp_path / "b.html"
    a.write_text(export.page(c))
    b.write_text(export.page(c))
    plan = tmp_path / "plan.html"
    plan.write_text(export.page(c, open_change="loud-engine"))
    c.close()

    seen = []
    for f in (a, b):
        ctx, pg, problems = open_page(browser, f)
        assert pg.locator("#tab-map").is_visible() and pg.locator("#tab-change").is_visible()
        assert pg.locator("#tab-flows").is_hidden()                     # behind More
        pg.click("#tab-more")
        assert pg.locator("#tab-flows").is_visible()
        pg.keyboard.press("Escape")
        assert pg.locator("#side h2").inner_text() == "f2"
        seen.append(positions(pg))
        assert not problems, problems
        ctx.close()
    assert seen[0] == seen[1] and len(seen[0]) >= 3                     # two exports, one map

    ctx, pg, problems = open_page(browser, plan, width=390, scheme="dark")
    assert "ready to implement" in pg.locator(".state").inner_text()
    assert pg.locator("#tab-change").get_attribute("aria-pressed") == "true"
    pg.click('[data-task="1.1"]')
    assert pg.locator(".node.picked").count() == 1
    assert pg.locator(".node.picked").get_attribute("aria-label") == "Engine"
    pg.click("#tab-map")                                                # the change stays drawn over the map
    assert pg.locator(".banner").is_visible() and pg.locator("#nodes .node[class*=role-]").count() >= 1
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390
    assert not problems, problems
    ctx.close()
