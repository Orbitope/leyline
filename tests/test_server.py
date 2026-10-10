"""The MCP server driven end to end the way an agent drives it: `leyline serve` as a subprocess, spoken to over
stdio by the official MCP client. Each test starts its own server, so nothing is shared between them."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

import leyline  # noqa: E402

SRC = str(Path(leyline.__file__).resolve().parents[1])
TESTS = Path(__file__).parent
CALL_TIMEOUT = 300   # seconds; a tool that takes longer than this is hung
CALLED: set = set()  # every tool any test here called, to check none was left out
LISTED: set = set()  # every tool the server lists


class Agent:
    """One MCP session, with the two ways an agent reads an answer: a result, or an error to act on."""

    def __init__(self, session):
        self.s = session

    async def raw(self, name, args):
        CALLED.add(name)
        res = await asyncio.wait_for(self.s.call_tool(name, args), CALL_TIMEOUT)
        text = "".join(getattr(c, "text", "") for c in res.content)
        error = getattr(res, "is_error", None)
        return (res.isError if error is None else error), text

    async def call(self, name, **args) -> dict:
        error, text = await self.raw(name, args)
        assert not error, f"{name}({args}) failed: {text}"
        return json.loads(text)

    async def fail(self, name, **args) -> str:
        error, text = await self.raw(name, args)
        assert error, f"{name}({args}) should have failed, but returned: {text[:300]}"
        assert "Traceback" not in text and len(text) < 2000, text
        assert text != f"Error executing tool {name}", "the reason was swallowed"
        return text


def serve(cwd: Path, script, env: dict = None, log: Path = None):
    """Start `leyline serve` in `cwd`, run `script(agent, tools)` against it, and stop it."""
    async def main():
        params = StdioServerParameters(command=sys.executable, args=["-m", "leyline.cli", "serve"],
                                       env={"PYTHONPATH": SRC, **(env or {})}, cwd=str(cwd))
        with open(log or cwd.parent / "server.log", "a") as errlog:
            async with stdio_client(params, errlog=errlog) as (r, w):
                async with ClientSession(r, w) as s:
                    init = await s.initialize()
                    tools = {t.name: t for t in (await s.list_tools()).tools}
                    await script(Agent(s), tools, init)
    asyncio.run(asyncio.wait_for(main(), 900))


def change_folder(repo: Path) -> Path:
    """The loud-engine change of the CLI test, written as an OpenSpec folder."""
    ch = repo / "openspec" / "changes" / "loud-engine"
    (ch / "specs" / "engine").mkdir(parents=True)
    (ch / "proposal.md").write_text("# Change: Loud engine\n\n## Why\nNames are hard to read in logs.\n\n"
                                    "## What Changes\n- `Engine.start` returns the name in upper case\n- A new `Engine.shout`\n")
    (ch / "tasks.md").write_text("- [ ] 1.1 Change `Engine.start` to return the name in upper case\n"
                                 "- [ ] 1.2 Add `Engine.shout`, the name with an exclamation mark\n"
                                 "- [ ] 1.3 Add the test \"Shout\"\n")
    (ch / "specs" / "engine" / "spec.md").write_text(
        "## ADDED Requirements\n### Requirement: Loud names\nThe engine SHALL report its name loudly.\n\n"
        "#### Scenario: Start\n- **WHEN** an engine starts\n- **THEN** it returns its name in upper case\n\n"
        "#### Scenario: Shout\n- **WHEN** an engine shouts\n- **THEN** the name ends with an exclamation mark\n")
    return ch


def run_tests(repo: Path) -> str:
    return subprocess.run([sys.executable, "-m", "pytest", "-rA", "-q", "-p", "no:cacheprovider", "tests"],
                          cwd=repo / "py", env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True).stdout


def implement(repo: Path, ch: Path) -> None:
    core = repo / "py/src/pkg/core.py"
    core.write_text(core.read_text().replace("        return self.name\n", "        return self.name.upper()\n\n"
                                             "    def shout(self):\n        return self.name + \"!\"\n", 1))
    tests = repo / "py/tests/test_engine.py"
    tests.write_text(tests.read_text().replace('engine.start() == "fixture"', 'engine.start() == "FIXTURE"')
                     + "\n\ndef test_shout(engine):\n    assert engine.shout().endswith(\"!\")\n")
    (ch / "tasks.md").write_text((ch / "tasks.md").read_text().replace("- [ ]", "- [x]"))


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "repo"
    shutil.copytree(TESTS / "fixture2", work)
    return work


def test_the_loop_over_mcp(repo):
    """map -> look around -> plan -> two reviews and a decision -> implement -> check, as an agent does it."""
    ch = change_folder(repo)
    before = run_tests(repo)

    async def script(a: Agent, tools, init):
        assert "`map`" in init.instructions and "`plan`" in init.instructions and "`check`" in init.instructions
        # Nothing is mapped yet: every read says to call map, and nothing is created by asking.
        assert "Call `map`" in await a.fail("overview")
        assert "Call `map`" in await a.fail("plan", change="loud-engine")
        assert not (repo / ".leyline").exists()

        m = await a.call("map", paths=["."])
        assert m["repos"] == ["repo"] and m["functions"] > 0 and "Mapped repo" in m["summary"]
        assert "`plan`" in m["next"][0] and Path(m["map_page"]).is_file()

        o = await a.call("overview")
        assert any(x["id"] == "repo:module:py/src/pkg" for x in o["repos"][0]["modules"])
        found = await a.call("search", text="Engine start")
        start = next(r["id"] for r in found["results"] if r["name"] == "start" and "Engine" in r["id"])
        engine = start.rsplit(".", 1)[0]
        e = await a.call("expand", node_id=engine)
        assert {c["name"] for c in e["contains"]["callable"]["items"]} >= {"start", "child"}
        hit = await a.call("impact", node_id=start)
        assert hit["reached_by"] >= 1 and hit["flows_through"]["total"] >= 1

        # Plan, with the tests as they run before any edit.
        p = await a.call("plan", change="loud-engine", test_output=before)
        assert p["status"]["blocking"] == [] and not p["status"]["reviewed"]
        assert p["tests_recorded"]["pass"] == 5 and "**State: ready to implement.**" in p["page"]
        assert "spec_review_facts" in p["next"][0] and "leyline plan" not in " ".join(p["next"])
        assert Path(p["written"]) == ch / "leyline.md" and "**State: ready to implement.**" in Path(p["written"]).read_text()

        # Two reviews; the logic reviewer files a high finding, which blocks until the person decides it.
        facts = await a.call("spec_review_facts", change="loud-engine", reviewer="logic")
        assert "logic" in facts and "performance" in facts and "spec_finding" in facts["how_to_file"]
        await a.call("spec_review_facts", change=str(ch), reviewer="performance")
        child = engine + ".child"
        f1 = await a.call("spec_finding", change="loud-engine", reviewer="logic", severity="high",
                          claim="Engine.child copies the name, so a child of a loud engine is loud too.",
                          evidence=[child], proposal="Say whether a child keeps the name as given.")
        f2 = await a.call("spec_finding", change="spec-loud-engine", reviewer="performance", severity="low",
                          claim="start is on every test's path.", evidence=[start, "no:such:node"])
        assert f1["change_id"] == "spec-loud-engine" and f2["ignored_evidence"] == ["no:such:node"]
        assert "evidence" in await a.fail("spec_finding", change="loud-engine", reviewer="logic", severity="low",
                                             claim="x", evidence=["no:such:node"])
        p = await a.call("plan", change="loud-engine")
        assert p["status"]["reviewed"] and any(b.startswith("decide") for b in p["status"]["blocking"])
        assert "spec_resolve" in p["next"][0]
        listed = await a.call("spec_findings", change="loud-engine")
        assert listed["open"] == 2 and {x["severity"] for x in listed["findings"]} == {"high", "low"}
        r = await a.call("spec_resolve", finding_id=f1["id"], status="rejected", resolution="A child is a copy; fine.")
        assert r["status"] == "rejected"
        learned = await a.call("learnings")   # the rejection, with its reason, is kept for later reviews
        assert learned["active"] == 1 and learned["learnings"][0]["id"] == r["learning"]
        assert learned["stale"] == 0 and learned["learnings"][0]["code"] == "unchanged"
        confirmed = await a.call("learnings", confirm=r["learning"])   # the person says it holds for the code now
        assert confirmed["id"] == r["learning"] and confirmed["was"] == "unchanged"
        p = await a.call("plan", change="loud-engine")
        # The low finding is still open: next names it and the call that records the decision (Signal item 9).
        assert p["status"]["blocking"] == [] and f2["id"] in p["next"][0] and "spec_resolve" in p["next"][0]
        assert p["next"][1].startswith("Then: implement the tasks")

        # Implement. Without test output check says what is missing; with it, the verdict.
        implement(repo, ch)
        v = await a.call("check", change="loud-engine")
        assert v["done_as_agreed"] is False and "test_output" in v["next"][0] and "--tests" not in v["next"][0]
        v = await a.call("check", change="loud-engine", test_output=run_tests(repo))
        assert v["done_as_agreed"] is True and v["why_not"] == [] and "**Yes.**" in v["page"]
        assert v["next"] == ["Next: nothing left to check; the change was done as agreed. Show the person the verdict"
                             " and the diff. The baseline is kept, so `check` can run again after later edits."]
        assert "**State: done as agreed.**" in (ch / "leyline.md").read_text()

        # The check recorded what the spec names; drift stays quiet until the code moves on.
        d = await a.call("drift")
        assert d["fails"] is False and d["page"].startswith("# Spec drift")
        core = repo / "py/src/pkg/core.py"
        core.write_text(core.read_text().replace("    def start(self):", "    def start(self, loud):"))
        d = await a.call("drift")
        assert d["fails"] is True and "has changed signature" in d["page"] and "accept=true" in d["next"][0]

    serve(repo, script)


def test_every_other_tool_with_real_arguments(repo):
    """The tools off the main path, each called the way an agent would, on a mapped fixture."""
    ch = change_folder(repo)

    async def script(a: Agent, tools, init):
        await a.call("map", paths=["."])
        start = "repo:python:py.src.pkg.core.Engine.start"
        assert (await a.call("cross_repo"))["repos"] == ["repo"]
        n = await a.call("neighbors", node_id=start, direction="in", kinds=["calls"])
        assert n["in"]["calls"]["total"] >= 1
        assert "return self.name" in (await a.call("source", node_id=start))["text"]
        ctx = await a.call("context", focus=["Engine.start"], budget_tokens=500)
        assert "def start(self)" in ctx["text"] and ctx["tokens"] <= 500 and "Left out:" in ctx["text"]
        top = await a.call("module_outline")
        assert top["kind"] == "repo" and any(p["id"] == "repo:module:cs/Mod" for p in top["parts"])
        mod = await a.call("module_outline", module="cs/Mod", depth=1)
        part = mod["parts"][0]
        assert part["size"]["functions"] > 0 and part["drill"] == f'module_outline("{part["id"]}")'
        error, text = await a.raw("name_part", {"part_id": part["id"], "name": "Shapes", "summary": "The shape types.",
                                                "evidence": [start]})   # `name` is also call()'s own argument
        assert not error and json.loads(text)["part_id"] == part["id"]
        again = await a.call("module_outline", module="cs/Mod", depth=1)
        assert next(p for p in again["parts"] if p["id"] == part["id"])["summary"] == "The shape types."
        assert "module_outline() with no module" in await a.fail("module_outline", module="nowhere")
        error, text = await a.raw("name_part", {"part_id": part["id"], "name": "Shapes"})
        assert error and "evidence" in text
        fl = await a.call("flows", through=start)
        assert fl["total"] >= 1
        page = await a.call("flows", limit=2)
        assert len(page["flows"]) == 2 and page["next_offset"] == 2 and page["flows"][0]["kind"] == "entry"
        assert (await a.call("flows", limit=2, offset=2))["flows"][0] not in page["flows"]
        steps = await a.call("flow", flow_id=fl["flows"][0]["id"])
        assert any(s["id"] == start for s in steps["steps"])
        t = await a.call("trace", from_id=steps["steps"][0]["id"], to_id=start)
        assert t["found"]
        ff = await a.call("find_flows", description="how an engine starts", limit=5)
        assert start in [c["id"] for c in ff["candidates"]] and ff["candidates"][0]["why"]
        walk = await a.call("explain_path", start=steps["steps"][0]["id"], to="Engine.start")
        assert walk["steps"][-1]["id"] == start and walk["mermaid"].startswith("sequenceDiagram") and "text" not in walk
        assert (await a.call("explain_path", start=fl["flows"][0]["id"], max_steps=5))["steps"]
        assert (await a.call("diagram", ids=["Engine.start"]))["mermaid"].startswith("sequenceDiagram")
        assert (await a.call("annotate", node_id="repo:module:py/src/pkg", key="summary", value="The engine.",
                             evidence=[start], confidence=0.7))
        prop = await a.call("propose_change", intent="start returns upper case", title="Loud start",
                            targets=[{"id": start, "action": "behavior", "note": "upper case"},
                                     {"action": "add", "name": "shout", "parent": "repo:python:py.src.pkg.core.Engine"}])
        assert prop["tests_to_run"] and isinstance(prop["marks"], int)
        sv = await a.call("save_view", title="Engine", narrative="The engine and its users.",
                          marks=[{"id": start, "role": "core", "note": "the method"}], legend={"core": "what changes"})
        listed = await a.call("views")
        assert {prop["view_id"], sv.get("id", sv.get("view_id"))} <= {v["id"] for v in listed["views"]}
        assert (await a.call("view", view_id=prop["view_id"]))["kind"] == "change"
        rec = await a.call("record_test_run", run="before", results=[{"name": "test_start", "status": "pass"}])
        assert rec["pass"] == 1
        assert "predicted" in json.dumps(await a.call("review_change", change_id=prop["change_id"], before_run="before"))
        rule = await a.call("add_rule", kind="forbid", selector_from="module:py/tests", selector_to="module:cs/Mod",
                            reason="tests stay in their language")
        assert rule
        assert (await a.call("check_rules"))["total"] == 1
        pats = await a.call("patterns")
        assert pats["total"] >= 1
        lab = await a.call("label_pattern", pattern="facade", roles={"facade": ["repo:python:py.src.pkg.core.make_engine"]},
                           rationale="make_engine hides the constructor.")
        assert lab["pattern"] == "facade"
        assert "fields" in await a.call("shared_state")
        assert (await a.call("coverage"))["imported"] is False
        assert (await a.call("spec_brief", change="loud-engine"))["change_id"] == "spec-loud-engine"
        picked = await a.call("affected_tests", change="loud-engine")
        assert picked["basis"] == "the map" and picked["commands"][0]["command"].startswith("pytest ")
        assert "tasks" in await a.call("spec_verify", change=str(ch))
        tours = await a.call("tours")
        assert (await a.call("tour", tour_id=tours["tours"][0]["id"]))["stops"]
        st = await a.call("save_tour", title="Start", audience="new to the engine",
                          stops=[{"title": "start", "kind": "node", "ref": start, "narrative": "Returns the name."}])
        assert st

    serve(repo, script)


def test_bad_arguments_give_errors_an_agent_can_act_on(repo):
    """Wrong ids, missing things and malformed arguments: each is an error that says what to do, not a traceback."""
    change_folder(repo)

    async def script(a: Agent, tools, init):
        await a.call("map", paths=["."])
        start = "repo:python:py.src.pkg.core.Engine.start"
        for name in ("expand", "neighbors", "source", "impact"):
            assert "`search`" in await a.fail(name, node_id="repo:python:Engine.strat")
        assert "did_you_mean" in await a.fail("expand", node_id="repo:python:py.src.pkg.core.Engine.strat")
        assert "text" in await a.fail("search", text="")
        assert "kind" in await a.fail("search", text="Engine", kind="class")
        assert "limit" in await a.fail("search", text="Engine", limit=0)
        assert "direction" in await a.fail("neighbors", node_id=start, direction="sideways")
        assert "kinds" in await a.fail("neighbors", node_id=start, kinds=["calls_into"])
        assert "no source" in await a.fail("source", node_id="repo:module:py/src/pkg")
        assert "kind" in await a.fail("flows", kind="unit")
        assert "No node" in await a.fail("flows", through="nope")
        assert "`flows`" in await a.fail("flow", flow_id="nope")
        assert "No node" in await a.fail("trace", from_id=start, to_id="nope")
        assert "evidence" in await a.fail("annotate", node_id=start, key="k", value="v")
        assert "layer" in await a.fail("annotate", node_id=start, key="k", value="v", layer="fact")
        assert "targets" in await a.fail("propose_change", intent="x", targets=[])
        assert "action" in await a.fail("propose_change", intent="x", targets=[{"id": start, "action": "explode"}])
        assert "needs a name" in await a.fail("propose_change", intent="x", targets=[{"action": "add"}])
        assert "No node" in await a.fail("propose_change", intent="x", targets=[{"id": "nope", "action": "behavior"}])
        assert "mark" in await a.fail("save_view", title="t", narrative="n", marks=[{"id": "nope"}])
        assert "`views`" in await a.fail("view", view_id="nope")
        assert "No change" in await a.fail("review_change", change_id="nope")
        assert "status" in await a.fail("record_test_run", run="x", results=[{"name": "t", "status": "passed"}])
        assert "kind" in await a.fail("add_rule", kind="never", selector_from="*")
        assert "matches nothing" in await a.fail("add_rule", kind="forbid", selector_from="module:nope", selector_to="*")
        assert "No node" in await a.fail("patterns", node_id="nope")
        assert "Patterns found" in (await a.call("patterns", pattern="visitor"))["note"]
        assert "roles" in await a.fail("label_pattern", pattern="p", roles={"a": "not-a-list"}, rationale="r")
        assert "rationale" in await a.fail("label_pattern", pattern="p", roles={"a": [start]}, rationale=" ")
        assert "No file" in await a.fail("coverage", import_path="no/such/.coverage")
        assert "No node" in await a.fail("coverage", node_id="nope")
        assert "No flow" in await a.fail("coverage", flow_id="nope")
        for name in ("plan", "check", "spec_brief", "spec_review_facts", "spec_verify"):
            assert "openspec/changes/<id>" in await a.fail(name, change="no-such-change")
        assert "Call `plan`" in await a.fail("spec_findings", change="no-such-change")
        assert "Call `plan`" in await a.fail("spec_finding", change="loud-engine", reviewer="logic", severity="high",
                                             claim="c", evidence=[start])   # not planned yet
        assert "reviewer" in await a.fail("spec_review_facts", change="loud-engine", reviewer="security")
        assert "PASS or FAIL" in await a.fail("plan", change="loud-engine", test_output="all good")
        assert "status" in await a.fail("spec_resolve", finding_id="f-1", status="maybe")
        assert "no finding" in await a.fail("spec_resolve", finding_id="f-1", status="accepted")
        assert "`tours`" in await a.fail("tour", tour_id="nope")
        assert "kind" in await a.fail("save_tour", title="t", stops=[{"title": "s", "kind": "file", "ref": start,
                                                                       "narrative": "n"}])
        assert "Not a directory" in await a.fail("map", paths=["no/such/dir"])
        assert "Not a directory" in await a.fail("map", paths=["py/src/pkg/core.py"])
        assert (await a.call("overview"))["repos"][0]["id"] == "repo"   # the refused maps added nothing
        assert "No module path" in await a.fail("overview", scope="nowhere")

    serve(repo, script)


def test_paths_an_agent_passes_stay_inside_the_mapped_repositories(repo, tmp_path):
    """An agent may have read untrusted text that tells it which path to pass: a tool that takes a path reads or
    writes only inside the repositories the store maps (or the directory the server started in, before the first
    map). The person, at the command line, is not limited."""
    outside = tmp_path / "outside"
    other = outside / "openspec" / "changes" / "x"
    other.mkdir(parents=True)
    (other / "proposal.md").write_text("# Change x\n")
    (other / "tasks.md").write_text("- [ ] 1.1 Change `start`\n")
    (outside / ".coverage").write_bytes(b"SQLite format 3\0")
    subprocess.run(["git", "init", "-q", str(outside)], check=True)
    ch = change_folder(repo)

    async def script(a: Agent, tools, init):
        assert "outside" in await a.fail("map", paths=[str(outside)])
        await a.call("map", paths=["."])
        assert "outside" in await a.fail("map", paths=[str(outside)])
        for name in ("plan", "check", "spec_brief", "spec_review_facts", "spec_verify", "affected_tests"):
            assert "outside" in await a.fail(name, change=str(other)), name
        assert "outside" in await a.fail("check", change=str(ch), coverage_path=str(outside / ".coverage"))
        assert "outside" in await a.fail("coverage", import_path=str(outside / ".coverage"))
        assert "outside" in await a.fail("quick", done="quick-x", coverage_path=str(outside / ".coverage"))
        assert "outside" in await a.fail("drift", path=str(outside))
        assert "outside" in await a.fail("review_pr", path=str(outside))
        assert "outside" in await a.fail("map", paths=[".."])
        assert not (other / "leyline.md").exists() and not (outside / ".leyline").exists()
        assert (await a.call("plan", change=str(ch)))["written"]   # inside: as before

    serve(repo, script)


def test_no_store_says_to_map(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    async def script(a: Agent, tools, init):
        for name, args in (("overview", {}), ("search", {"text": "x"}), ("flows", {}), ("views", {}), ("tours", {}),
                           ("check_rules", {}), ("expand", {"node_id": "x"}), ("check", {"change": "x"})):
            assert "Call `map`" in await a.fail(name, **args)
        assert "pass the repository directory" in await a.fail("map")
        # A store named by LEYLINE_DB that does not exist yet gets the same answer.
    serve(empty, script)
    assert not (empty / ".leyline").exists()

    async def named(a: Agent, tools, init):
        assert str(tmp_path / "elsewhere" / "s.db") in await a.fail("overview")
    serve(empty, named, env={"LEYLINE_DB": str(tmp_path / "elsewhere" / "s.db")})


def test_tool_descriptions_name_every_argument(repo):
    """What a model reads before calling anything: every tool and argument described, the loop in order."""
    async def script(a: Agent, tools, init):
        order = [init.instructions.index(f"`{t}`") for t in ("map", "plan", "spec_review_facts", "check")]
        assert order == sorted(order)
        assert len(tools) >= 35
        for t in tools.values():
            assert t.description and len(t.description) > 40, t.name
            for arg, schema in (t.input_schema.get("properties") or {}).items():
                assert schema.get("description"), f"{t.name}.{arg}"
        assert "Loop step 1" in tools["map"].description and "Loop step 3" in tools["check"].description
        LISTED.update(tools)
    serve(repo, script)


def test_a_map_from_the_command_line_is_seen_by_a_running_server(repo):
    """The person re-maps with the CLI while the agent's server runs: the server answers from the new map."""
    async def script(a: Agent, tools, init):
        await a.call("map", paths=["."])
        assert not (await a.call("search", text="shout"))["results"]
        await a.call("overview")   # every worker thread now holds an open connection
        core = repo / "py/src/pkg/core.py"
        core.write_text(core.read_text() + "\n\ndef shout(x):\n    return x + '!'\n")
        out = subprocess.run([sys.executable, "-m", "leyline.cli", "map", "."], cwd=repo, capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": SRC})
        assert out.returncode == 0, out.stderr
        for _ in range(5):   # whichever worker thread takes the call
            assert any(r["name"] == "shout" for r in (await a.call("search", text="shout"))["results"])

        # A full re-map running beside the server: every answer is either right or says to retry.
        proc = subprocess.Popen([sys.executable, "-m", "leyline.cli", "map", ".", "--full"], cwd=repo,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "PYTHONPATH": SRC})
        try:
            while proc.poll() is None:
                error, text = await a.raw("search", {"text": "Engine"})
                assert (not error and json.loads(text)["results"]) or "retry" in text, text
                await asyncio.sleep(0.05)
        finally:
            proc.wait(60)
        assert (await a.call("search", text="Engine"))["results"]
    serve(repo, script)


def test_a_workspace_of_two_repositories(tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(TESTS / "fixture_ws", ws)

    async def script(a: Agent, tools, init):
        m = await a.call("map", paths=["wsapp", "wslib"])
        assert m["repos"] == ["wsapp", "wslib"]
        o = await a.call("overview")
        assert {r["id"] for r in o["repos"]} == {"wsapp", "wslib"} and o["workspace"]["links"]
        x = await a.call("cross_repo")
        assert any(p["from"] == "wsapp" and p["to"] == "wslib" for p in x["pairs"])
        assert any(c["name"] == "helper" for c in x["most_called_across"])
        hit = await a.call("impact", node_id=next(c["id"] for c in x["most_called_across"] if c["name"] == "helper"))
        assert hit["crosses_repo_boundary"]
        assert (await a.call("map"))["repos"] == ["wsapp", "wslib"]   # map again what the store holds
    serve(ws, script)


def test_a_pull_request_is_reviewed_over_mcp(tmp_path):
    """review_pr on a checked-out branch, then the reviewers' facts and a finding on the id it returns."""
    from test_pr import FILES, git
    root = tmp_path / "repo"
    for f, text in FILES.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "checkout", "-q", "-b", "feature")
    (root / "app/store.py").write_text("def load(path, encoding):\n    with open(path, encoding=encoding) as f:\n"
                                       "        return f.read()\n\n\ndef helper(x):\n    return x * 2\n")
    git(root, "commit", "-qam", "Add an encoding")

    async def script(a: Agent, tools, init):
        LISTED.update(tools)
        r = await a.call("review_pr", base="main", about="Read files with an encoding")
        assert r["change_id"] == "pr-feature" and "Signature: `load`" in r["page"]
        assert {m["name"] for m in r["reaches"]["signature_changed_callers_not_edited"]} == {"use.py.first", "use.py.count"}
        assert r["gate_passed"] is False and r["gate"]["config"] == "the default"
        assert "`use.py.count` calls code whose signature changed, and was not edited" in r["blocking"]
        assert r["next"][0].startswith("Blocked: update the call in")
        f = await a.call("spec_review_facts", change="pr-feature", reviewer="logic")
        assert f["what_it_says_it_does"] == "Read files with an encoding"
        assert f["since_last_review"] == {"first_review": True} and "related_changes" in f
        ev = f["logic"]["signature_changed_callers_not_edited"][0]["id"]
        filed = await a.call("spec_finding", change="pr-feature", reviewer="logic", severity="high",
                             claim="first and count still pass one argument", evidence=[ev])
        assert filed["change_id"] == "pr-feature"
        assert (await a.call("spec_findings", change="pr-feature"))["open"] == 1
        again = await a.call("review_pr", base="main")   # the gate is judged again, with the open high finding
        assert not again["gate_passed"] and any(b.startswith(f"open high finding {filed['id']}") for b in again["blocking"])
        assert await a.fail("review_pr", base="main", github="1")   # no gh, no sign-in, or no such pull request
        c = await a.call("coupling", path="store.py", min_together=2)    # git history: the base and the branch
        assert c["path"] == "app/store.py" and c["changes"] == 2 and c["partners"] == []
    serve(root, script)


def test_long_answers_are_cut_to_fit_and_say_so():
    """Answers stay under the limit however large the map; what was cut is named, with how to see the rest."""
    from leyline import server

    big = {"total": 5000, "items": [{"id": f"repo:python:mod{i}.f", "name": f"f{i}", "path": f"src/mod{i}.py"}
                                    for i in range(5000)], "text": "x" * 50_000, "small": [1, 2, 3, 4, 5]}
    text = server._fit(big, {})
    out = json.loads(text)
    assert len(text) <= server.LIMIT + 300 and out["total"] == 5000 and out["small"] == [1, 2, 3, 4, 5]
    assert out["cut"]["items"].endswith("of 5000") and "cut here" in out["text"] and "offset" in out["more"]
    capped = {"a": list(range(30)), "keep": list(range(30)), "inner": [{"b": list(range(30))}]}
    assert server._cap(capped, 10, ("keep",)) == {"a": "10 of 30", "inner[].b": "10 of 30"}
    assert len(capped["keep"]) == 30 and len(capped["a"]) == 10


def test_a_quick_change_over_mcp(repo):
    """A one-line change with no spec folder: quick before, edit, quick with done after."""
    before = run_tests(repo)

    async def script(a: Agent, tools, init):
        assert "`what`" in await a.fail("quick")
        b = await a.call("quick", what="engine names in upper case", names=["Engine.start"], test_output=before)
        assert b["change_id"] == "quick-engine-names-in-upper-case" and "Will touch: `Engine.start`" in b["page"]
        assert b["tests_to_run"] and "done=" in b["next"][0]
        core = repo / "py/src/pkg/core.py"
        core.write_text(core.read_text().replace("        return self.name\n", "        return self.name.upper()\n", 1))
        tests = repo / "py/tests/test_engine.py"
        tests.write_text(tests.read_text().replace('engine.start() == "fixture"', 'engine.start() == "FIXTURE"'))
        v = await a.call("quick", done=b["change_id"], test_output=run_tests(repo))
        assert v["done"] is True and "**Done.**" in v["page"], v["page"]
        assert {x["key"]: x["verdict"] for x in v["items"]} == {"scope": "proven", "callers": "proven", "tests": "proven",
                                                               "ran": "proven"}
        facts = await a.call("spec_review_facts", change=b["change_id"], reviewer="logic")
        assert "Engine.start" in facts["named_code"]
        assert "no quick change 'quick-nope'" in await a.fail("quick", done="quick-nope")

    serve(repo, script)


def test_skills_are_prompts_and_a_tool_with_nothing_mapped(tmp_path):
    """An agent connected to the server finds the skills without installing them: as prompts, and with `skills`."""
    from leyline import agent_skills
    shipped = {s.name: s for s in agent_skills.available()}
    empty = tmp_path / "empty"
    empty.mkdir()

    async def script(a: Agent, tools, init):
        start = init.instructions.index("Start here")
        assert [init.instructions.index(f"`{t}`", start) for t in ("map", "plan", "quick", "review_pr", "check")] == \
            sorted(init.instructions.index(f"`{t}`", start) for t in ("map", "plan", "quick", "review_pr", "check"))
        assert "leyline-ask" in init.instructions[start:]
        prompts = {p.name: p for p in (await a.s.list_prompts()).prompts}
        assert set(prompts) == set(shipped) and {"leyline-ask", "leyline-pr-review"} <= set(prompts)
        assert prompts["leyline-ask"].description == shipped["leyline-ask"].description
        got = await a.s.get_prompt("leyline-quick-change", {"request": "make the retry count 3"})
        text = got.messages[0].content.text
        assert text.startswith("# Make a quick change with Leyline") and text.endswith("The person asked: make the retry count 3")
        listed = await a.call("skills")
        assert {s["name"] for s in listed["skills"]} == set(shipped) and "prompt" in listed["how"]
        one = await a.call("skills", skill="ask")
        assert one["name"] == "leyline-ask" and one["text"] == shipped["leyline-ask"].body
        assert "No skill named 'nope'" in await a.fail("skills", skill="nope")
    serve(empty, script)
    assert not (empty / ".leyline").exists()


def test_every_tool_was_called():
    """Runs last in this file: each tool the server lists was called by some test above."""
    if not LISTED or len(CALLED) < 10:
        pytest.skip("run with the rest of this file")
    assert LISTED - CALLED == set()
