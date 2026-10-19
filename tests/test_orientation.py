"""The orientation tour and `map` point at the named parts of a large module, not at the clusters the map proposed,
and say how many parts still have no name; the parts are worked out once per map."""

import shutil
from pathlib import Path

import pytest

pytest.importorskip("networkx")

from leyline import loop, outline, store, tours  # noqa: E402
from leyline.indexer import index  # noqa: E402

from test_outline import make_repo  # noqa: E402

HERE = Path(__file__).parent


@pytest.fixture
def big(tmp_path):
    """svc: a module of more than 30 files, split into five parts, none named."""
    repo = make_repo(tmp_path / "repo")
    db = tmp_path / "s.db"
    index(repo, db, "r")
    return repo, db


def module_stop(db, name="svc") -> str:
    """The narrative of the stop that describes a module (not the one that says where to start)."""
    con = store.connect(db)
    try:
        t = tours.get(con, "tour:orientation:r")
        return next(s["narrative"] for s in t["stops"] if s["title"].endswith(f": {name}"))
    finally:
        con.close()


def part_ids(db) -> list:
    con = store.connect(db)
    try:
        return [p["id"] for p in outline.outline(con, "svc")["parts"]]
    finally:
        con.close()


def test_the_orientation_tour_lists_the_named_parts_of_a_module(big):
    repo, db = big
    core, api = "r:dir:svc/src/shop/core", "r:dir:svc/src/shop/api"
    assert core in part_ids(db) and api in part_ids(db)
    con = store.connect(db)
    outline.name_part(con, core, "Domain core", "Orders and reports.", layer="intent")
    outline.name_part(con, api, "HTTP routes", "What clients call.", layer="intent")
    con.close()
    index(repo, db, "r")                                       # the same code again: the tour is written again
    said = module_stop(db)
    assert "Domain core (Orders and reports.)" in said and "HTTP routes (What clients call.)" in said
    assert said.index("Domain core") < said.index("HTTP routes")          # the outline's order: largest first
    assert "3 of its 5 parts have no name" in said and "leyline-explore-module" in said
    assert "Clustering splits it" not in said
    stale = tours._parts_sentence([{"name": "Old", "named": True, "summary": "x", "stale": "may be stale: it kept 10%"}])
    assert "Old (x) [its name may be stale]" in stale


def test_the_stop_of_an_unnamed_module_says_its_parts_are_unnamed(big):
    repo, db = big
    said = module_stop(db)
    assert "It splits into 5 parts and none has a name yet" in said and "leyline-explore-module" in said
    assert "Clustering splits it" not in said


def test_the_stop_of_a_small_module_is_unchanged(tmp_path):
    repo = tmp_path / "small"
    shutil.copytree(HERE / "fixture2", repo)
    db = tmp_path / "s.db"
    index(repo, db, "f2")
    con = store.connect(db)
    try:
        stops = [s["narrative"] for s in tours.get(con, "tour:orientation:f2")["stops"] if s["title"].startswith("Module")
                 or s["title"].startswith("The foundation")]
    finally:
        con.close()
    assert stops and not any("leyline-explore-module" in x for x in stops)
    assert any("Clustering splits it into" in x for x in stops)


def test_the_parts_are_computed_once_per_map(big, tmp_path, monkeypatch):
    repo, _ = big
    calls = []
    real = outline.map_parts
    monkeypatch.setattr(outline, "map_parts", lambda con: calls.append(1) or real(con))
    m = loop.map_repos([str(repo)], tmp_path / "again.db", repo_id="r")
    assert len(calls) == 1                                      # the tour and the page read the one result
    assert "unnamed_parts" in m and "parts" not in m
    assert '"part:r:dir:svc/src/shop/core"' in Path(m["page"]).read_text(encoding="utf-8")


def test_map_says_when_the_parts_of_the_main_module_are_unnamed(big, tmp_path):
    repo, _ = big
    db = tmp_path / "next.db"
    m = loop.map_repos([str(repo)], db, repo_id="r")
    last = loop.map_text(m).splitlines()[-1]
    assert last.startswith("Next: to look around")
    assert "5 of the 5 parts of svc have no name" in last and "leyline-explore-module" in last
    (repo / "openspec" / "changes" / "x").mkdir(parents=True)   # the other Next line: a change folder exists
    m = loop.map_repos([str(repo)], db, repo_id="r")
    last = loop.map_text(m).splitlines()[-1]
    assert last.startswith("Next: `leyline plan <id>`") and "5 of the 5 parts of svc have no name" in last
    con = store.connect(db)
    for k, pid in enumerate(part_ids(db)):
        outline.name_part(con, pid, f"Part {k}", layer="intent")
    con.close()
    m = loop.map_repos([str(repo)], db, repo_id="r")
    assert m["unnamed_parts"] is None and "have no name" not in loop.map_text(m)
