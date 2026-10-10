"""MCP server over a Leyline store. map, plan and check first; then reads of the graph, and writes of annotations,
views, proposals, rules and test runs.

Every tool goes through `_tool`, which turns a missing store, a bad argument or a crash into an error the agent
can act on, and keeps each answer small enough to read: lists are cut to a stated number of items and the whole
answer to about LIMIT characters, with `cut` saying what was cut and how to see the rest."""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Annotated, Literal, Optional

from pydantic import Field
from typing_extensions import NotRequired, TypedDict

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

from . import change as change_mod, coverage as measured, diff, loop, spec as spec_loop, patterns as pattern_labels, query, rules, store, tours as tour_store
from . import agent_skills

mcp = FastMCP(
    "leyline",
    instructions=(
        "Leyline is a map of a codebase (a graph in SQLite) for a loop in which a person decides the design of a change"
        " and agents write the code. The loop, in order:\n"
        "1. `map` with the repository directory, once, if a tool says nothing is mapped.\n"
        "2. Write the change as an OpenSpec folder, openspec/changes/<id>/ (proposal.md, specs/<area>/spec.md with"
        " scenarios, tasks.md naming code in backticks). Before writing, look around with `overview`, `search`,"
        " `expand` and `impact`.\n"
        "3. `plan` with the change and the test output from before any edit. It returns `status.blocking` (fix the spec"
        " and call `plan` again until it is empty) and the page `leyline.md`.\n"
        "4. Review: `spec_review_facts` once with reviewer=logic and once with reviewer=performance; file real problems"
        " with `spec_finding`. Only the person decides a finding; record their decision with `spec_resolve`. Then `plan`"
        " again.\n"
        "5. Implement the tasks.\n"
        "6. `check` with the change and the test output from after. Report its verdict (`done_as_agreed`, `why_not`),"
        " not your own account.\n"
        "Every answer from `map`, `plan` and `check` ends with `next`: do that. `plan` and `check` re-map changed code"
        " themselves. Node ids come from `search`, `overview` and `expand`; never guess one. Edges marked heuristic come"
        " from syntax and can be wrong. Long lists are cut: `cut` says what was cut and `more` how to see the rest.\n"
        "To answer how something works, outside the loop: `find_flows` with the question in words, then `explain_path`"
        " from the candidate that fits, reading the code (`source`) at the steps that matter."
    ) + agent_skills.start_here(),
)

LIMIT = 24_000   # characters; an answer much longer than this crowds out the rest of an agent's context

_local = threading.local()
_lock = threading.Lock()   # one re-index at a time
_generation = [0]          # bumped on every re-index, so each thread opens the store afresh


def _path() -> str:
    return os.environ.get("LEYLINE_DB") or ".leyline/leyline.db"


def _db() -> sqlite3.Connection:
    # The server may run each tool call on a different worker thread, and a SQLite connection
    # belongs to the thread that opened it. Keep one per thread, for the store the path names now.
    con = getattr(_local, "con", None)
    where = str(Path(_path()).resolve())
    if con is None or getattr(_local, "gen", None) != _generation[0] or getattr(_local, "where", None) != where:
        if con is not None:
            con.close()
        con = _local.con = store.connect(_path())
        _local.gen, _local.where = _generation[0], where
    return con


def _missing() -> Optional[str]:
    """Why there is nothing to read, or None. Checked before connecting, since connecting creates an empty store."""
    p = Path(_path())
    if not p.is_file():
        return (f"Nothing is mapped yet: there is no store at {p.resolve()}. Call `map` with the repository directory"
                f" (paths=[\".\"] for {Path.cwd()}), then try again.")
    if _db().execute("SELECT 1 FROM nodes WHERE kind = 'repo' LIMIT 1").fetchone() is None:
        return (f"The store at {p.resolve()} is empty, or is being written by `leyline map` right now. If a map is"
                " running, retry in a minute; otherwise call `map` with the repository directory.")
    return None


# -- keeping answers small --------------------------------------------------------------------------
def _dump(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str, separators=(",", ":"))


def _cap(x, n: int, keep: tuple = (), cut: Optional[dict] = None, path: str = "") -> dict:
    """Cut every list in `x` longer than `n` to its first `n` items, in place; a list under a key in `keep` stays
    whole (the lists inside it are still cut). Returns {path: "kept of total"} for what was cut."""
    cut = {} if cut is None else cut
    items = x.items() if isinstance(x, dict) else enumerate(x) if isinstance(x, list) else ()
    for k, v in list(items):
        p = f"{path}.{k}" if isinstance(k, str) and path else (k if isinstance(k, str) else f"{path}[]")
        if isinstance(v, list) and len(v) > n and k not in keep:
            cut.setdefault(p, f"{n} of {len(v)}")
            x[k] = v = v[:n]
        if isinstance(v, (dict, list)):
            _cap(v, n, keep, cut, p)
    return cut


CUT_MARK = re.compile(r"\n\[\.\.\. cut here: ([\d,]+) characters in all\]$")


def _uncut(s: str) -> tuple[str, int]:
    """A string as it was before `_fit` cut it, as far as it is kept: (what is left of it, its whole length)."""
    m = CUT_MARK.search(s)
    return (s[:m.start()], int(m[1].replace(",", ""))) if m else (s, len(s))


def _largest(x, path: str = "", best=None, floor: tuple = (3, 1000)):
    """The longest list (of more than floor[0] items) or string (over floor[1] characters, not counting the mark of
    an earlier cut) inside x, as (size, holder, key, path)."""
    items = x.items() if isinstance(x, dict) else enumerate(x) if isinstance(x, list) else ()
    for k, v in items:
        p = f"{path}.{k}" if isinstance(k, str) and path else (k if isinstance(k, str) else f"{path}[]")
        if (isinstance(v, list) and len(v) > floor[0]) or (isinstance(v, str) and len(_uncut(v)[0]) > floor[1]):
            size = len(_dump(v))
            if best is None or size > best[0]:
                best = (size, x, k, p)
        if isinstance(v, (dict, list)):
            best = _largest(v, p, best, floor)
    return best


MORE = ("Lists were cut to keep this answer short; `cut` says which (shown of total). To see the rest, narrow the call"
        " (a node id, `scope`, `kind` or `through`), raise `limit`, or page with `offset` where the tool has it.")


def _fit(out: dict, cut: dict, limit: int = LIMIT, more: str = MORE) -> str:
    """The answer as compact JSON of at most about `limit` characters: the longest lists and strings are
    shortened until it fits (to 3 items and 1,000 characters, then, when that is not enough, to 1 item and 200
    characters), and `cut` says what was cut."""
    text = _dump(out)
    for floor in ((3, 1000), (1, 200)):
        for _ in range(200):
            if len(text) <= limit:
                break
            best = _largest(out, floor=floor)
            if best is None:
                break
            size, holder, key, p = best
            v, excess = holder[key], len(text) - limit + 200
            if isinstance(v, list):
                keep = max(floor[0], min(len(v) - 1, len(v) - -(-excess * len(v) // max(size, 1))))
                cut[p] = f"{keep} of {cut[p].split(' of ')[1] if p in cut else len(v)}"
                holder[key] = v[:keep]
            else:
                body, whole = _uncut(v)   # a string cut before is cut again from what is left, and keeps its length
                keep = max(floor[1], len(body) - excess)
                holder[key] = body[:keep] + f"\n[... cut here: {whole:,} characters in all]"
                cut[p] = f"{keep:,} of {whole:,} characters"
            text = _dump({**out, "cut": cut, "more": more} if cut else out)
    if cut:
        out["cut"], out["more"] = cut, more
        text = _dump(out)
    return text


def _tool(fn=None, *, name: Optional[str] = None, needs_store: bool = True, items: int = 0, keep: tuple = (),
          more: str = MORE, limit: int = LIMIT):
    """Register `fn` as a tool. Its answer is a dict; one with "error" is returned as a tool error. `items` caps
    every list in the answer (0 leaves them to the size limit alone); lists under keys in `keep` stay whole.
    `more` tells the agent how to see what was cut; `limit` is the answer's size in characters."""
    if fn is None:
        return lambda f: _tool(f, name=name, needs_store=needs_store, items=items, keep=keep, more=more, limit=limit)

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            why = _missing() if needs_store else None
            out = {"error": why} if why else fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            # A map running in another process holds the write lock, or changed the tables under this connection.
            _generation[0] += 1
            out = {"error": f"The store could not be read ({e}). A map may be running; retry in a minute."}
        except Exception as e:   # the agent gets the reason, not a traceback
            out = {"error": f"{fn.__name__} failed: {type(e).__name__}: {e}"}
        if not isinstance(out, dict):
            out = {"result": out}
        if "error" in out:
            rest = {k: v for k, v in out.items() if k != "error"}
            raise ToolError(_for_agent(str(out["error"])) + (" " + _fit(rest, _cap(rest, 10)) if rest else ""))
        cut = out.pop("cut") if isinstance(out.get("cut"), dict) else {}   # what the tool itself cut
        cut.update(_cap(out, items, keep) if items else {})
        return _fit(out, cut, limit=limit, more=more)

    # No return annotation: the answer is text (compact JSON), not a structured result to validate.
    run.__signature__ = inspect.signature(fn, eval_str=True).replace(return_annotation=inspect.Signature.empty)
    del run.__wrapped__
    return mcp.tool(name=name or fn.__name__)(run)


# -- argument types ---------------------------------------------------------------------------------
NodeId = Annotated[str, Field(description="A node id exactly as `search`, `overview` or `expand` returned it.")]
ChangeArg = Annotated[str, Field(description="The change: its folder (openspec/changes/<id>, absolute or relative to the"
                                             " server's directory) or just its id.")]
TestOutput = Annotated[Optional[str], Field(description="The test runner's output as text: " + diff.READS + "."
                                                        " Use this or test_results.")]


class TestResult(TypedDict):
    name: Annotated[str, Field(description="The test's name as the runner prints it")]
    status: Literal["pass", "fail", "skip"]
    message: NotRequired[str]


TestResults = Annotated[Optional[list[TestResult]], Field(description="The results as a list, for runner output that"
                                                                      " test_output cannot read.")]
NodeKind = Literal["module", "file", "type", "callable", "field", "test", "entry_point", "external", "system", "repo"]
EdgeKind = Literal["calls", "imports", "uses_type", "extends", "implements", "instantiates", "has_field", "exposes",
                   "depends_on", "reads", "writes", "communicates", "overrides", "groups"]
Limit = Annotated[int, Field(ge=1, le=500, description="Items to show in each list (each list also gives its total).")]
Offset = Annotated[int, Field(ge=0, description="Items to skip, to page through a long list.")]


class Target(TypedDict):
    id: NotRequired[Annotated[str, Field(description="The node id of what changes (not needed for action add)")]]
    action: Literal["behavior", "signature", "rename", "remove", "add"]
    note: NotRequired[Annotated[str, Field(description="Why it changes")]]
    name: NotRequired[Annotated[str, Field(description="add only: the new member's name")]]
    parent: NotRequired[Annotated[str, Field(description="add only: id of the type, file or module it goes in")]]
    uses: NotRequired[Annotated[list[str], Field(description="add only: ids it will call")]]
    used_by: NotRequired[Annotated[list[str], Field(description="add only: ids that will call it")]]


class Mark(TypedDict):
    id: Annotated[str, Field(description="A node id")]
    role: NotRequired[Annotated[str, Field(description="A short label for its group")]]
    note: NotRequired[Annotated[str, Field(description="One line on why it is here")]]


class Stop(TypedDict):
    title: str
    kind: Literal["node", "flow", "pattern", "view", "repo"]
    ref: Annotated[str, Field(description="The id of what the stop points at")]
    narrative: Annotated[str, Field(description="What to notice here and why it matters")]


def _change(arg: str) -> tuple[Optional[Path], Optional[str]]:
    """A change folder from a folder path, an id or a spec id (spec-<id>), with an error when there is none."""
    folder = loop.find_change(arg, _path())
    if folder is None and arg.startswith("spec-"):
        folder = loop.find_change(arg[5:], _path())
    if folder is None or not (folder / "tasks.md").is_file() and not (folder / "proposal.md").is_file():
        return None, (f"No change folder {arg!r}: looked for it as a path from {Path.cwd()} and under openspec/changes/"
                      " in the mapped repositories. Write the change first, as openspec/changes/<id>/ with proposal.md,"
                      " specs/ and tasks.md, or pass the folder's full path.")
    return folder, None


def _change_id(arg: str) -> tuple[Optional[str], Optional[str]]:
    """The stored id of a planned change (spec-<id>), from a folder, an id or the stored id itself."""
    con = _db()
    for cid in (arg, "spec-" + arg, "spec-" + Path(arg).name):
        if con.execute("SELECT 1 FROM change_proposals WHERE id = ?", (cid,)).fetchone():
            return cid, None
    return None, f"No planned change {arg!r}. Call `plan` with the change folder first."


def _results(test_output: Optional[str], test_results: Optional[list[dict]]):
    """Test results from runner output or from a list: (results or None, error or None)."""
    if test_results is not None:
        return [dict(r) for r in test_results], None
    if test_output is None:
        return None, None
    parsed = diff.parse_test_output(test_output)
    if not parsed:
        return None, (f"found no test results in test_output: it reads {diff.READS}. Pass other formats as test_results.")
    return parsed, None


CLI_TO_TOOL = (("`leyline plan`", "`plan`"), ("`leyline check`", "`check`"), ("`leyline spec findings`", "`spec_findings`"),
               ("`leyline spec facts`", "`spec_review_facts`"))


def _for_agent(text: str) -> str:
    """Text written for the command line, naming the tools instead of the commands."""
    for cli, tool in CLI_TO_TOOL:
        text = text.replace(cli, tool)
    return text


def _known(node_id: str) -> Optional[dict]:
    """An error for a node id that is not in the store, with near matches; None when it is there."""
    if query._node(_db(), node_id) is not None:
        return None
    hits = query.search(_db(), node_id.split(":")[-1].split(".")[-1], limit=5)["results"] if node_id.strip() else []
    return {"error": f"No node with id {node_id!r}. Find ids with `search`.",
            "did_you_mean": [{"id": h["id"], "kind": h["kind"]} for h in hits]}


# -- the loop ---------------------------------------------------------------------------------------
@_tool(name="map", needs_store=False, items=30)
def map_code(paths: Annotated[Optional[list[str]], Field(description="Repository directories, absolute or relative to the"
                                                                     " server's directory. Several are mapped together as one"
                                                                     " workspace. Leave out to map again what the store holds.")] = None) -> dict:
    """Loop step 1: index the code into the store this server reads. Call it once, when a tool says nothing is
    mapped, or to add a repository. `plan` and `check` re-map changed code on their own. Returns what was found,
    the map page a person can open, and `next`."""
    bad = [p for p in paths or [] if not Path(p).is_dir()]
    if bad:
        return {"error": f"Not a directory: {', '.join(map(str, bad))}. Pass repository directories (relative paths are"
                         f" from {Path.cwd()})."}
    if not paths and not Path(_path()).is_file():
        return {"error": f"Nothing is mapped in {Path(_path()).resolve()} yet: pass the repository directory as paths"
                         f" (for example [\".\"] for {Path.cwd()})."}
    with _lock:
        try:
            m = loop.map_repos(paths or [], _path())
        finally:
            _generation[0] += 1
    if "error" in m:
        return m
    m["db"] = str(Path(m["db"]).resolve())
    if m.get("page"):
        m["page"] = str(Path(m["page"]).resolve())
    summary = loop.map_text(m).replace("Map page:", "Map page (for the person to open):").rsplit("\nNext:", 1)[0]
    m["map_page"] = m.pop("page", None)
    return {**m, "summary": summary, "next": [
        "Write the change as an OpenSpec folder, openspec/changes/<id>/ (proposal.md, specs/<area>/spec.md, tasks.md;"
        " the leyline-spec skill says how), then call `plan` with it and the current test output."]}


@_tool(items=15, keep=("blocking", "decide", "notes", "tasks", "scenarios", "findings", "gaps", "next"),
       more="Lists were cut to keep this answer short; `cut` says which. `page` and the leyline.md it was written to"
            " (`written`) have the whole plan.")
def plan(change: ChangeArg, test_output: TestOutput = None, test_results: TestResults = None,
         new_baseline: Annotated[bool, Field(description="Forget the picture of the code kept from the first plan and"
                                                         " compare from the code as it is now. Only to start over.")] = False) -> dict:
    """Loop step 2, before any code is written; call again after every edit to the spec. Re-maps changed code,
    ties each task to code and each scenario to a test, and writes the plan page `leyline.md` into the change
    folder (its text is `page`). `status.blocking` lists what must be fixed in the spec, or decided by the person,
    before implementation; `next` says what to do. Pass the tests as they run now, before any edit, so `check`
    can tell a test the change broke from one that already failed."""
    folder, err = _change(change)
    if err:
        return {"error": err}
    results, err = _results(test_output, test_results)
    if err:
        return {"error": err}
    with _lock:
        try:
            b = loop.plan(_path(), folder, results, new_baseline)
        finally:
            _generation[0] += 1
    if "error" in b:
        return b
    b["map_page"] = str(Path(b.pop("page")).resolve()) if b.get("page") else None
    status = spec_loop.brief_status(b)
    status["notes"] = [_for_agent(n) for n in status["notes"]]
    return {"status": status, "next": loop.next_after_plan(b, change, for_agent=True),
            "page": spec_loop.brief_text(b), **b}


@_tool(items=25, keep=("tasks", "scenarios", "why_not", "next"),
       more="Lists were cut to keep this answer short; `cut` says which. leyline.md (`written`) has the whole verdict.")
def check(change: ChangeArg, test_output: TestOutput = None, test_results: TestResults = None,
          coverage_path: Annotated[Optional[str], Field(description="Coverage measured on the same test run (pytest"
                                                                    " --cov=<package> --cov-context=test writes .coverage):"
                                                                    " each scenario then says whether its test ran the"
                                                                    " changed code (`ran_changed_code`).")] = None) -> dict:
    """Loop step 3, after the tasks are implemented. Re-maps the code, records the test run, and says whether the
    change was done as agreed: each task from what changed in the code, each scenario from its test's result,
    edits outside the spec, new links between modules, rules newly broken. Appends the verdict to `leyline.md`.
    `done_as_agreed` is the verdict and `why_not` the reasons; `next` says what to do. Report this verdict to the
    person, never your own account of the change."""
    folder, err = _change(change)
    if err:
        return {"error": err}
    results, err = _results(test_output, test_results)
    if err:
        return {"error": err}
    with _lock:
        try:
            v = loop.check(_path(), folder, results, coverage_path)
        finally:
            _generation[0] += 1
    if "error" in v:
        return v
    v["map_page"] = str(Path(v.pop("page")).resolve()) if v.get("page") else None
    return {"done_as_agreed": v["done_as_agreed"], "why_not": v.get("why_not"),
            "next": loop.next_after_check(v, change, for_agent=True), "page": spec_loop.verify_text(v), **v}


# -- looking around ---------------------------------------------------------------------------------
@_tool
def overview(scope: Annotated[Optional[str], Field(description="A module path prefix (such as `src/core`) to see one part"
                                                               " of a large repository.")] = None,
             limit: Limit = 20) -> dict:
    """The top-level map: repositories, modules with sizes, which modules depend on which (counts per edge kind),
    proposed systems (groups of types inside a module), external packages, and which extractors ran. Call it
    first to get oriented. Lists are largest first; `expand` a module or system id for its contents."""
    o = query.overview(_db())
    inside = lambda mid: scope is None or (mid or "").split(":module:")[-1].startswith(scope.strip("/"))
    out: dict = {}
    if scope:
        out["scope"] = scope
    repos, all_mods = [], set()
    for r in o["repos"]:
        mods = [m for m in r["modules"] if inside(m["id"])]
        all_mods |= {m["id"] for m in mods}
        mods.sort(key=lambda m: -m["loc"])
        repos.append({**{k: v for k, v in r.items() if k != "modules" and v is not None},
                      "modules_total": len(mods), "lines": sum(m["loc"] for m in mods),
                      "modules": [{k: v for k, v in m.items() if k not in ("name", "path")} for m in mods[:limit]]})
    if scope and not all_mods:
        return {"error": f"No module path starts with {scope!r}. Call `overview` without scope to see the module paths."}
    out["repos"] = repos
    if "workspace" in o:
        out["workspace"] = o["workspace"]
    edges = [e for e in o["module_edges"] if e["from"] in all_mods or e["to"] in all_mods]
    out["module_edges"] = {"total": len(edges), "items": edges[:limit]}
    systems = [s for s in o["systems"] if s["module"] and inside(":module:" + s["module"])]
    systems.sort(key=lambda s: -len(s["members"]))
    out["systems"] = {"total": len(systems), "items": [
        {"id": s["id"], "name": s["name"], "members": len(s["members"]),
         **({"responsibility": s["responsibility"]} if s["responsibility"] else {})} for s in systems[:limit]]}
    ext = [x for x in o["externals"] if any(u in all_mods for u in x["used_by"])]
    ext.sort(key=lambda x: (-len(x["used_by"]), x["name"]))
    named = [x for x in o.get("named_parts", []) if x["module"] in all_mods]
    if named:   # parts of modules that were given names (module_outline, name_part)
        out["named_parts"] = {"total": len(named), "items": [
            {k: x[k] for k in ("id", "name", "summary") if x.get(k)} for x in named[:limit]]}
    out["externals"] = {"total": len(ext), "items": [
        {"id": x["id"], "used_by_modules": len(x["used_by"])}
        for x in ext[:limit]]}
    ran: dict = {}
    for c in o["coverage"]:
        if c["extractor"] != "timing" and c["status"] != "no_files":
            ran.setdefault(c["status"], []).append(c["extractor"])
    out["extractors"] = ran
    out["notes"] = o["notes"]
    return out


@_tool(items=20)
def cross_repo(limit: Limit = 20) -> dict:
    """For a workspace of several repositories mapped together: the links between them by kind, the functions
    most called from another repository, and how many flows cross into another repository and come back."""
    return query.cross_repo(_db(), limit)


@_tool(items=40, keep=("did_you_mean",))
def search(text: Annotated[str, Field(min_length=1, description="Words of a name, qualified name or path, such as"
                                                                 " `Engine start` or `core/engine.py`.")],
           kind: Annotated[Optional[NodeKind], Field(description="Only nodes of this kind.")] = None,
           limit: Annotated[int, Field(ge=1, le=100, description="Most results to return.")] = 20) -> dict:
    """Find node ids by name, qualified name or path, best match first. Start here to get the id any other tool
    needs."""
    if not text.strip():
        return {"error": "text is empty: pass words of the name you are looking for."}
    r = query.search(_db(), text, kind, limit)
    if not r["results"]:
        r["note"] = ("Nothing matches." + (f" Try without kind={kind}." if kind else "")
                     + " Search matches whole-word prefixes of names and paths: try one distinctive word.")
    return r


@_tool(items=40)
def expand(node_id: NodeId, limit: Limit = 20) -> dict:
    """One node in detail. A module: its files, public types, entry points and module dependencies. A type: its
    members, bases and the modules that use it. A function: its signature, callers and callees, and the fields
    it reads and writes (`data`). Each list shows `limit` items and its total."""
    if (err := _known(node_id)):
        return err
    out = query.expand(_db(), node_id, limit)
    for k in ("depends_on", "depended_on_by", "externals"):
        if isinstance(out.get(k), list) and len(out[k]) > limit:
            out[k] = {"total": len(out[k]), "items": out[k][:limit]}
    return out


@_tool
def neighbors(node_id: NodeId,
              direction: Annotated[Literal["out", "in", "both"], Field(description="out: edges from the node; in: edges"
                                                                                    " to it.")] = "both",
              kinds: Annotated[Optional[list[EdgeKind]], Field(description="Only these edge kinds. reads and writes run"
                                                                           " from a function to a field.")] = None,
              limit: Limit = 25) -> dict:
    """The raw edges around a node, grouped by edge kind, each group with its total. Use `expand` for a summary
    of a node; use this to list one kind of edge, such as everything that calls it (direction=in, kinds=[calls])."""
    if (err := _known(node_id)):
        return err
    return query.neighbors(_db(), node_id, direction, kinds, limit)


@_tool
def source(node_id: NodeId,
           max_lines: Annotated[int, Field(ge=1, le=2000, description="Most lines to return from the start.")] = 200) -> dict:
    """The source text of a type, function or field, read from the working tree that was mapped."""
    if (err := _known(node_id)):
        return err
    return query.source(_db(), node_id, max_lines)


@_tool
def flows(kind: Annotated[Optional[Literal["entry", "test"]], Field(description="entry: walked from a program's entry"
                                                                              " point; test: walked from a test.")] = None,
          through: Annotated[Optional[str], Field(description="Only flows that pass this node id.")] = None,
          limit: Limit = 25, offset: Offset = 0) -> dict:
    """List flows: the call paths walked from each entry point and each test. Entry points come first. Pass
    `through` to find the flows that reach one function; read one with `flow`."""
    if through and (err := _known(through)):
        return err
    r = query.flows(_db(), kind, through)
    rows = sorted(r["flows"], key=lambda f: f["kind"] != "entry")
    page = rows[offset:offset + limit]
    for f in page:   # a test's flow can pass through dozens of modules
        if len(f["modules"]) > 5:
            f["modules"] = f["modules"][:5] + [f"... {len(f['modules']) - 5} more"]
    out = {"total": r["total"], "offset": offset, "flows": page, "note": r["note"]}
    if offset + limit < r["total"]:
        out["next_offset"] = offset + limit
    return out


@_tool
def flow(flow_id: Annotated[str, Field(description="A flow id from `flows`.")],
         max_steps: Annotated[int, Field(ge=1, le=1000, description="Most steps to return.")] = 40,
         offset: Offset = 0) -> dict:
    """One flow step by step, in source order: each step's function, call depth and the step it was reached
    from. Page through a long flow with `offset`."""
    r = query.flow(_db(), flow_id, offset + max_steps)
    if "error" in r:
        return r
    r["steps"] = r["steps"][offset:]
    r["offset"] = offset
    if offset + max_steps < (r.get("total_steps") or 0):
        r["next_offset"] = offset + max_steps
    return r


@_tool
def trace(from_id: Annotated[str, Field(description="Node id of the function to start from.")],
          to_id: Annotated[str, Field(description="Node id of the function to reach.")]) -> dict:
    """The shortest chain of calls and channels from one function to another."""
    for i in (from_id, to_id):
        if (err := _known(i)):
            return err
    return query.trace(_db(), from_id, to_id)


@_tool
def impact(node_id: NodeId,
           max_depth: Annotated[int, Field(ge=1, le=20, description="How many calls back to follow.")] = 6,
           limit: Limit = 15) -> dict:
    """What can reach a node: its callers, near and far, grouped by module (largest first), and the flows that
    pass through it. Call it on what a change will touch before writing the spec. Read-only; `propose_change`
    gives a fuller assessment and saves it as a view."""
    if (err := _known(node_id)):
        return err
    r = query.impact(_db(), node_id, max_depth, limit)
    mods = r["by_module"]
    for m in mods:
        total = m.pop("direct_total", len(m["direct"]))
        if total > 5:
            m["direct"], m["direct_more"] = m["direct"][:5], total - 5
    r["by_module"] = {"total": len(mods), "items": mods[:limit]}
    r["flows_through"]["items"] = r["flows_through"]["items"][:limit]
    return r


@_tool(items=10, limit=36_000)
def context(focus: Annotated[list[str], Field(min_length=1, description="What you are working on: node ids, names"
                                                                       " (`Owner.method`), file paths, a change id"
                                                                       " (spec-<id>, pr-<id>) or words to search for.")],
            budget_tokens: Annotated[int, Field(ge=200, le=8000, description="How long the outline may be, in tokens"
                                                                           " (characters / 4).")] = 2000) -> dict:
    """A short outline of the code around what you are working on, to read before editing it: the related
    functions, types and tests file by file, as declaration lines (no bodies), most related first, cut to the
    budget. The focus is marked `>`; channel ends are said in words ("answers GET /api/x"); the last lines say what
    was left out. Ranked by personalized PageRank over calls, channels, type use and containment. Read a body
    with `source`."""
    from . import context as ctx
    r = ctx.build(_db(), focus, budget_tokens)
    if "error" in r:
        return r
    return {"text": r["text"], "tokens": r["tokens"], "focus": r["focus"], "shown": r["shown"], "left_out": r["left_out"]}


@_tool(limit=30_000, more="Lists were cut to keep this answer short; `cut` says which. Lower depth, or drill into one"
                          " part by its id, to see the rest.")
def module_outline(module: Annotated[Optional[str], Field(description="A module's path (such as `editor/core`) or id, or"
                                                                     " a part id from an earlier outline. Leave out to"
                                                                     " list the modules.")] = None,
                   depth: Annotated[int, Field(ge=1, le=3, description="1: the parts; 2: and the parts inside each; 3:"
                                                                       " one more level of names.")] = 2) -> dict:
    """A large module split into at most 12 parts: its folders, or groups of files (found from calls and type use)
    where a folder is flat, largest first. For each part: size (files, functions, lines), entry points (routes, UI
    handlers and components, programs, commands; tests), what it uses and what uses it (other parts and modules,
    counted links), channels crossing its edge, its busiest functions (most flows through them), key types, what makes
    it risky beside its siblings, and its name and summary if one was given. Drill in with a part's id; a file or a
    group of one file lists its key functions. Name parts with `name_part` after reading their code."""
    from . import outline
    return outline.outline(_db(), module, depth)


@_tool
def name_part(part_id: Annotated[str, Field(min_length=1, description="A part id from module_outline.")],
              name: Annotated[str, Field(min_length=1, description="A short name a person would use, such as"
                                                                   " \"Validation rules\".")],
              summary: Annotated[str, Field(description="One line on what the part does.")] = "",
              evidence: Annotated[Optional[list[str]], Field(description="Node ids of the code you read to name it;"
                                                                         " needed unless layer is intent.")] = None,
              layer: Annotated[Literal["inferred", "intent"], Field(description="inferred: your reading of the code."
                                                                                " intent: the person said it.")] = "inferred") -> dict:
    """Give a part from module_outline a name and a one-line summary. Outlines, `overview`, explain_path's steps and
    the map page show it. It is kept across maps; when the part later keeps less than half of its files, it is shown
    as "may be stale"."""
    from . import outline
    return outline.name_part(_db(), part_id, name, summary, evidence, layer, "mcp")


# -- asking how something works -----------------------------------------------------------------------
@_tool(keep=("words",))
def find_flows(description: Annotated[str, Field(min_length=1, description="What happens, in plain words, such as"
                                                                          " \"what happens when a writer saves a dialogue\""
                                                                          " or \"how a vehicle is spawned\".")],
               limit: Annotated[int, Field(ge=1, le=30, description="Most candidates to return.")] = 10) -> dict:
    """Where a described behavior could start, best first: entry points, route handlers, UI event handlers, message
    handlers, commands, tests whose names state the behavior, and other functions, ranked by the words they share
    with the description (code names split, endings cut, a few synonyms such as save, write, persist). Each comes
    with why it matched, its kind, file:line and `flows`, the flows that start there; one where none starts has
    `reached_from`, the entry points' flows that reach it, and `tests_reaching`, the tests' flows that reach it. When
    `ambiguous` is true, show the person the top few and ask which they mean. Then walk one with `explain_path`."""
    from . import explain
    return explain.find_flows(_db(), description, limit)


@_tool(limit=30_000, more="Lists were cut to keep this answer short; `cut` says which. Lower max_steps, or walk from a"
                          " later step, to see the rest.")
def explain_path(start: Annotated[str, Field(min_length=1, description="Where to start: a node id, a name"
                                                                      " (`Owner.method`), a route (`PUT /api/x`) or a"
                                                                      " flow id, such as a candidate from find_flows.")],
                 to: Annotated[Optional[str], Field(description="Where to end: the shortest path from start to it.")] = None,
                 through: Annotated[Optional[str], Field(description="A node the path must pass: the path to it, then on"
                                                                     " from it.")] = None,
                 max_steps: Annotated[int, Field(ge=2, le=200, description="Most steps to show.")] = 40) -> dict:
    """An ordered walk across calls and channels (http, messages, launched programs) from `start`: with nothing
    else, the main flow from it (its shallow steps first, helpers called from many places counted, not shown); with
    `to`, the shortest path; with `through`, the path through that node and on. Each step has its id, name,
    file:line, how it was reached (call, http GET /x, starts a program ...), the calling line and the callee's
    declaration. Data written for a reader that runs later (a file, a table) is under `later_elsewhere`; `not_seen`
    says what the map could not follow. `mermaid` draws the walk. Read the code of a step (`source`) before saying
    what it does."""
    from . import explain
    r = explain.explain_path(_db(), start, to, through, max_steps)
    if "error" not in r:
        r.pop("text", None)   # the same walk as `steps`, as lines for the command line
    return r


@_tool
def diagram(ids: Annotated[list[str], Field(min_length=1, description="Functions or types (a type stands for its"
                                                                     " methods): node ids or names.")]) -> dict:
    """A Mermaid sequence diagram of how execution reaches the named code and what it calls. Every arrow is a call
    or a channel link on the map; a dotted arrow is a link the map guessed by name."""
    from . import explain
    return explain.diagram(_db(), ids)


# -- writing to the map -------------------------------------------------------------------------------
@_tool
def annotate(node_id: NodeId, key: Annotated[str, Field(description="What is stated, such as `name` or `responsibility`"
                                                                    " on a system, or `summary` on a module.")],
             value: Annotated[str, Field(description="The statement itself.")],
             evidence: Annotated[Optional[list[str]], Field(description="Node ids the statement is based on; needed when"
                                                                        " layer is inferred.")] = None,
             confidence: Annotated[Optional[float], Field(ge=0, le=1, description="From 0 to 1; for layer inferred.")] = None,
             layer: Annotated[Literal["inferred", "intent"], Field(description="inferred: your reading of the code."
                                                                               " intent: a statement the person made.")] = "inferred") -> dict:
    """Record a statement about a node. An inferred statement goes stale when the files behind its evidence
    change. Facts from the code cannot be written."""
    if (err := _known(node_id)):
        return err
    return query.annotate(_db(), node_id, key, value, evidence, confidence, layer, "mcp")


@_tool(items=15, keep=("must_edit", "risks"),
       more="Lists were cut to keep this answer short; `cut` says which. The whole report is in the saved view:"
            " call `view` with view_id and a higher limit.")
def propose_change(intent: Annotated[str, Field(description="The change in the person's words.")],
                   targets: Annotated[list[Target], Field(min_length=1, description="What will change. Target a function"
                                                                                    " when you can; a type, file or system"
                                                                                    " counts every function inside it.")],
                   title: Annotated[Optional[str], Field(description="A short name for the view; the intent if left out.")] = None,
                   depth: Annotated[int, Field(ge=1, le=10, description="How many calls back to follow.")] = 4) -> dict:
    """Assess a change before any code is written, without an OpenSpec folder, and save its blast radius as a
    view the person can open. For a change in the plan/check loop use `plan` instead. Actions: behavior (same
    contract, different result), signature, rename, remove, or add (with name, parent, uses, used_by). Returns
    what must be edited with it, what it reaches, the tests to run, entry points affected, channels crossed,
    untested targets and risks; `view` shows the saved view, `review_change` compares after implementation."""
    targets = [dict(t) for t in targets]
    for t in targets:
        if t["action"] == "add" and not t.get("name"):
            return {"error": "a target with action add needs a name (and a parent: the type, file or module it goes in)"}
        if t["action"] != "add" and not t.get("id"):
            return {"error": f"a target with action {t['action']} needs the id of the node that changes"}
        if t.get("id") and (err := _known(t["id"])):
            return err
    r = change_mod.propose(_db(), intent, targets, title, depth, "mcp")
    if isinstance(r.get("marks"), list):   # the marks are the saved view; `view` shows them
        r["marks"] = len(r["marks"])
    return r


@_tool
def save_view(title: Annotated[str, Field(description="A short name for the view.")], narrative: Annotated[str, Field(description="A few sentences explaining the view.")],
              marks: Annotated[list[Mark], Field(min_length=1, description="The nodes to show.")],
              legend: Annotated[Optional[dict[str, str]], Field(description="Each role mapped to a description.")] = None) -> dict:
    """Save a custom view for the person: any set of nodes worth looking at together, such as the parts of a
    design alternative or everything involved in one feature. It appears in the map page's Views tab."""
    return change_mod.save_view(_db(), title, narrative, [dict(m) for m in marks], "custom", "mcp", legend=legend)


@_tool(items=50)
def views() -> dict:
    """List saved views, newest first, including the blast-radius view of every proposed change."""
    return change_mod.list_views(_db())


@_tool
def view(view_id: Annotated[str, Field(description="A view id from `views`, `propose_change` or `save_view`.")],
         limit: Limit = 15) -> dict:
    """One saved view: its narrative, its marked nodes and, for a proposed change, its impact report. Each list
    shows `limit` items and its total."""
    r = change_mod.get_view(_db(), view_id)
    if "error" not in r:
        r["cut"] = _cap(r, limit, ("risks",))
    return r


@_tool(items=25)
def review_change(change_id: Annotated[str, Field(description="The change_id `propose_change` returned.")],
                  before_run: Annotated[Optional[str], Field(description="Label of the test run recorded before the edit.")] = None,
                  after_run: Annotated[Optional[str], Field(description="Label of the test run recorded after it.")] = None) -> dict:
    """For a change assessed with `propose_change` (not one in the plan/check loop, which `check` covers), after
    it is implemented: compare the code with the snapshot taken at proposal time. Re-map first with `map`.
    Returns which predicted edits happened, edits not predicted, new and removed module dependencies, flows whose
    path changed, rule results, and the test delta between two runs recorded with `record_test_run`."""
    return diff.review(_db(), change_id, before_run, after_run)


@_tool
def record_test_run(run: Annotated[str, Field(min_length=1, description="A label, such as `before` or `after`.")],
                    results: Annotated[list[TestResult], Field(min_length=1, description="One entry per test.")]) -> dict:
    """Store the outcome of one test run under a label, for `review_change`. `plan` and `check` record their own
    test runs from test_output; this is for the propose_change path."""
    return diff.record_tests(_db(), run, [dict(r) for r in results])


@_tool
def add_rule(kind: Annotated[Literal["forbid", "no_cycle", "must_be_tested"],
                             Field(description="forbid: nothing in selector_from may link to selector_to. no_cycle:"
                                               " selector_from is `modules` or `systems`. must_be_tested: every function"
                                               " in selector_from is on some test's path.")],
             selector_from: Annotated[str, Field(description="`module:Name`, `system:Name`, `external:Name`, `path:prefix`,"
                                                             " `id:prefix` or `*`.")],
             selector_to: Annotated[str, Field(description="For forbid: a selector of the same forms.")] = "",
             edge_kinds: Annotated[Optional[list[EdgeKind]], Field(description="For forbid: only these links count.")] = None,
             severity: Annotated[Literal["error", "warning"], Field(description="How a violation is reported.")] = "error",
             reason: Annotated[str, Field(description="Why the rule should hold.")] = "",
             confirmed: Annotated[bool, Field(description="True only when the person stated this rule; otherwise it is"
                                                          " stored as a suggestion.")] = False) -> dict:
    """Add an architecture rule. A rule is the person's intent: unless they stated it, it is stored as
    suggested, with your reason."""
    return rules.add_rule(_db(), kind, selector_from, selector_to, edge_kinds, severity, reason,
                          "confirmed" if confirmed else "suggested", "mcp")


@_tool(items=20)
def check_rules() -> dict:
    """Evaluate every architecture rule against the current map, with examples of each violation."""
    return rules.check(_db())


@_tool(items=10, keep=("patterns",))
def patterns(pattern: Annotated[Optional[str], Field(description="Only this pattern, such as `strategy`.")] = None,
             node_id: Annotated[Optional[str], Field(description="Only patterns this node (or anything inside it)"
                                                                 " plays a role in.")] = None,
             include_tests: Annotated[bool, Field(description="Also patterns inside test code.")] = False,
             limit: Limit = 15) -> dict:
    """Design patterns found by their shape: strategy, decorator, composite, template method, observer, factory,
    builder, singleton, process boundary. Each has the nodes playing each role, a rationale and a confidence. A
    label says the code has the shape, not that the author intended the pattern."""
    if node_id and (err := _known(node_id)):
        return err
    r = pattern_labels.listing(_db(), pattern.strip().lower() if pattern else None, node_id, include_tests, limit)
    if pattern and not r["patterns"]:
        found = pattern_labels.listing(_db(), None, None, include_tests, 1)["by_pattern"]
        r["note"] = f"No {pattern!r} found. Patterns found: {', '.join(sorted(found)) or 'none'}."
    return r


@_tool
def label_pattern(pattern: Annotated[str, Field(description="The pattern's name, such as `adapter`.")],
                  roles: Annotated[dict[str, list[str]], Field(description="Each role mapped to the node ids that play it,"
                                                                           " such as {\"adapter\": [...], \"adaptee\": [...]}.")],
                  rationale: Annotated[str, Field(description="What in the code makes it this pattern.")],
                  confidence: Annotated[float, Field(ge=0, le=1, description="How sure you are, from 0 to 1.")] = 0.6) -> dict:
    """Record a pattern the structural matchers missed, after reading the code. The label goes stale when that
    code changes."""
    return pattern_labels.label(_db(), pattern, roles, rationale, confidence, "mcp")


@_tool
def shared_state(scope: Annotated[Optional[str], Field(description="A module id or an id prefix.")] = None,
                 limit: Limit = 40) -> dict:
    """Fields assigned from outside the type that declares them, most widely written first: the mutable state with
    no single owner. For every reader and writer of one field, or the fields one function touches, `expand` it
    and read `data`."""
    return query.shared_state(_db(), scope, limit)


@_tool(name="coupling")
def change_coupling(path: Annotated[Optional[str], Field(description="A file: its path in the repository, or the end of"
                                                                     " it. Left out: the most coupled pairs.")] = None,
                    min_together: Annotated[int, Field(ge=2, description="Commits the two changed in together.")] = 3,
                    min_confidence: Annotated[float, Field(ge=0, le=1, description="Share of the file's commits that"
                                                                                   " changed the other too.")] = 0.5,
                    limit: Limit = 20) -> dict:
    """Files (and folders) that usually change in the same commits as a file, from git history: links the map
    cannot see, such as docs, schemas, config, fixtures and the other side of a protocol. Worked out once per
    commit. `plan` and `review_pr` already list those a change leaves alone."""
    from . import coupling as history
    return history.query(_db(), path, min_together, min_confidence, limit)


@_tool(items=50)
def coverage(node_id: Annotated[Optional[str], Field(description="A function: the tests under which it ran.")] = None,
             flow_id: Annotated[Optional[str], Field(description="A test's flow: its static path against what ran.")] = None,
             import_path: Annotated[Optional[str], Field(description="A coverage.py data file, a Cobertura XML report or"
                                                                     " Istanbul's coverage-final.json to read into the"
                                                                     " store first.")] = None,
             test: Annotated[Optional[str], Field(description="With import_path: the one test file that ran, for a"
                                                              " report with no per-test detail (Istanbul, Cobertura);"
                                                              " what ran is tied to that file.")] = None) -> dict:
    """Measured test coverage, as opposed to the static paths in `flows`. With no argument: per module, how many
    functions ran, how many are on a test's path but never ran, and how many ran through links the map does not
    have. An Istanbul or Cobertura report covers a whole run: run one test file at a time and import each with
    `test` naming that file."""
    out = {}
    if import_path:
        if not Path(import_path).is_file():
            return {"error": f"No file at {import_path} (relative paths are from {Path.cwd()})."}
        imported = measured.import_file(_db(), import_path, test=test)
        if isinstance(imported, dict) and "error" in imported:
            return imported
        out["imported"] = imported
    if flow_id:
        return {**out, **measured.compare_flow(_db(), flow_id)}
    if node_id:
        if (err := _known(node_id)):
            return err
        return {**out, "node": node_id, "tests": measured.tests_for(_db(), node_id)}
    return {**measured.summary(_db()), **out}


@_tool(items=60, keep=("commands",))
def affected_tests(change: Annotated[str, Field(description="The change: its folder or id (a planned spec), or a review"
                                                            " id such as pr-123.")]) -> dict:
    """The tests to run for a change, each with why, and `commands` that run them (pytest node ids,
    `npx vitest run <files>`, `npx jest --runTestsByPath <files>`, `go test -run`). With per-test coverage imported, the tests
    measured running the changed or must-edit code; otherwise, and for changed code no measured test ran, the tests
    whose path on the map passes through it. Run these, then pass their output to `check`."""
    from . import affected
    folder = loop.find_change(change, _path())
    with _lock:
        try:
            loop.refresh(_path())
        finally:
            _generation[0] += 1
    con = _db()
    return affected.select(con, affected.change_id_for(con, change, folder))


# -- the spec steps: the parts of plan and check, and review --------------------------------------
@_tool(items=25, keep=("tasks", "scenarios", "findings", "gaps"))
def spec_brief(change: ChangeArg,
               new_baseline: Annotated[bool, Field(description="Compare from the code as it is now. Only to start over.")] = False) -> dict:
    """Rarely needed: the brief step of `plan` alone, with no re-map and no test run. Use `plan`."""
    folder, err = _change(change)
    if err:
        return {"error": err}
    return spec_loop.brief(_db(), folder, new_baseline=new_baseline)


@_tool(items=30)
def spec_review_facts(change: ChangeArg,
                      reviewer: Annotated[Optional[Literal["logic", "performance"]],
                                          Field(description="Which review you are doing; recorded so the plan shows it"
                                                            " ran, even if you file nothing.")] = None) -> dict:
    """Review step, after `plan` and before code is written: what the map says about the change, arranged as the
    questions a logic reviewer and a performance reviewer must answer. Read the code behind anything suspicious
    (`source`, `expand`), then file each real problem with `spec_finding`."""
    if change.startswith("pr-") and not Path(change).is_dir():   # a pull request reviewed with review_pr
        from . import pr
        return pr.review_facts(_db(), change, reviewer)
    if change.startswith("quick-") and not Path(change).is_dir():   # a small change started with `quick`
        from . import quick as quick_mod
        return quick_mod.review_facts(_db(), change, reviewer)
    folder, err = _change(change)
    if err:
        return {"error": err}
    return spec_loop.review_facts(_db(), folder, reviewer)


@_tool(needs_store=False, items=20, keep=("next", "blocking"),
       more="Lists were cut to keep this answer short; `page` has the whole review page and `spec_review_facts` the facts.")
def review_pr(base: Annotated[Optional[str], Field(description="The branch the change will merge into, or a commit."
                                                             " Default: origin's default branch, else main, else master.")] = None,
              about: Annotated[Optional[str], Field(description="What the change says it does: its title and"
                                                              " description. Default: its commit messages.")] = None,
              github: Annotated[Optional[str], Field(description="A GitHub pull request number: base, title and"
                                                               " description come from it (needs gh, and the pull"
                                                               " request checked out).")] = None,
              review_id: Annotated[Optional[str], Field(description="Name it pr-<review_id>. Default: the pull request"
                                                                  " number, else the branch name.")] = None,
              path: Annotated[str, Field(description="The checkout, absolute or relative to the server's directory.")] = ".") -> dict:
    """Review a change someone else wrote, with no spec: a branch or a pull request, checked out here. Maps the commit
    it left its base at, compares it with the checkout, and returns a page saying what changed, what it reaches and
    did not change (callers of a changed signature, the other ends of channels its edits touch, removed code still
    called), and which tests run it. `blocking` lists, one line each, what holds up the merge under the project's
    gate (`[pr] blocking` in openspec/leyline.toml; by default unedited callers of a changed signature, removed code
    still called, confirmed error rules newly failing, and open high findings), and `gate_passed` is true when
    nothing does. Then run the adversarial review on the returned `change_id` with `spec_review_facts` and
    `spec_finding`, as for a spec."""
    from . import pr
    try:
        with _lock:
            try:
                r = pr.review(_path(), Path(path), base, about or "", github, review_id)
            finally:
                _generation[0] += 1
    except pr.GitError as e:
        return {"error": str(e)}
    if "error" in r:
        return r
    g = r["gate"]
    return {"change_id": r["change_id"], "gate_passed": g["passed"], "blocking": g["blocking"],
            "gate": {"kinds": g["kinds"], "config": g["config"] or "the default", "notes": g["notes"]},
            "page": pr.text(r), "written": r["page"], "size": r["size"],
            "reaches": r["reaches"], "tests": r["tests"], "other_files": r["other_files"], "house_rules": r["house_rules"],
            "next": ([f"Blocked: {g['next']}."] if not g["passed"] else [])
            + [f"Run the leyline-adversarial-review skill on {r['change_id']}: spec_review_facts with reviewer logic,"
               " then performance; file findings with spec_finding. Show the person the page."]}


@_tool(needs_store=False, items=15, keep=("items", "next"),
       more="Lists were cut to keep this answer short; `page` has the whole answer.")
def quick(what: Annotated[Optional[str], Field(description="Before the edit: the change in one sentence, such as"
                                                           " \"make the retry count 3\". Code in backticks in it counts as"
                                                           " named.")] = None,
          names: Annotated[Optional[list[str]], Field(description="The code it touches, as written in the code:"
                                                                  " `Owner.name`, `module.func`, a constant's name. With"
                                                                  " done: more code the change turned out to touch.")] = None,
          done: Annotated[Optional[str], Field(description="After the edit: the change_id (quick-<slug>) the first call"
                                                           " returned.")] = None,
          test_output: TestOutput = None, test_results: TestResults = None,
          coverage_path: Annotated[Optional[str], Field(description="With done: coverage measured on the same test run.")] = None,
          change_id: Annotated[Optional[str], Field(description="Name it quick-<change_id>. Default: from the sentence.")] = None) -> dict:
    """A small change (a constant, one function and its caller) with no spec folder. Before editing, call it with
    `what`, `names` and the current test output: it says what the change touches, what must be edited with it, the
    channels it touches and the tests that run it, and keeps a baseline. After editing, call it with `done` and the
    new test output: one verdict (`done`), each item proven, partial, contradicted, inconclusive or needs a person, the
    edits outside the named code and the callers left broken. When `grown` is not empty, write a spec and use `plan`."""
    from . import quick as quick_mod
    results, err = _results(test_output, test_results)
    if err:
        return {"error": err}
    with _lock:
        try:
            if done:
                if not Path(_path()).is_file():
                    return {"error": "Nothing is mapped here yet: call `quick` with `what` and `names` before editing."}
                v = quick_mod.done(_path(), done if done.startswith("quick-") else "quick-" + done, results, coverage_path, names)
                if "error" in v:
                    return v
                return {"done": v["done"], "page": quick_mod.done_text(v), "next": quick_mod.next_after_done(v, for_agent=True),
                        **{k: v[k] for k in ("change_id", "items", "outside", "broken", "tests_broke", "ran", "grown")}}
            if not what:
                return {"error": "Pass `what` (the change in a sentence) and `names` before editing, or `done` after."}
            b = quick_mod.start(_path(), what, names, results, ".", change_id)
        finally:
            _generation[0] += 1
    if "error" in b:
        return b
    return {"change_id": b["change_id"], "page": quick_mod.start_text(b), "next": quick_mod.next_after_start(b, for_agent=True),
            **{k: b[k] for k in ("named", "values", "unplaced", "must_edit", "channels", "tests_to_run", "grown")}}


@_tool
def spec_finding(change: ChangeArg,
                 reviewer: Annotated[Literal["logic", "performance"], Field(description="Which review found it.")],
                 severity: Annotated[Literal["high", "medium", "low"], Field(description="high blocks implementation"
                                                                                         " until the person decides it.")],
                 claim: Annotated[str, Field(min_length=1, description="One sentence a person can check.")],
                 evidence: Annotated[list[str], Field(min_length=1, description="Node ids that show it.")],
                 proposal: Annotated[str, Field(description="The change to the spec you propose.")] = "") -> dict:
    """File a review finding against a planned change. A finding with no node behind it is refused. Only the
    person resolves findings."""
    cid, err = _change_id(change)
    if err:
        return {"error": err}
    known = [e for e in evidence if query._node(_db(), e) is not None]
    if not known:
        return {"error": f"None of the evidence ids is on the map: {', '.join(evidence[:5])}. Find ids with `search`."}
    r = spec_loop.add_finding(_db(), cid, reviewer, severity, claim, evidence, proposal)
    if "error" not in r:
        r["change_id"] = cid
        if len(known) < len(evidence):
            r["ignored_evidence"] = [e for e in evidence if e not in known]
    return r


@_tool(items=50)
def spec_findings(change: ChangeArg) -> dict:
    """The findings filed against a planned change, and the person's decision on each: open, accepted, rejected or
    deferred."""
    cid, err = _change_id(change)
    if err:
        return {"error": err}
    return spec_loop.findings(_db(), cid)


@_tool
def spec_resolve(finding_id: Annotated[str, Field(description="A finding id from `spec_finding` or `spec_findings`.")],
                 status: Annotated[Literal["accepted", "rejected", "deferred", "open"],
                                   Field(description="accepted means the spec changes to follow it.")],
                 resolution: Annotated[str, Field(description="The person's reason, in their words.")] = "") -> dict:
    """Record the person's decision on a finding. Call it only with a decision the person stated; never resolve a
    finding on your own judgment."""
    return spec_loop.resolve_finding(_db(), finding_id, status, resolution)


@_tool(items=50)
def learnings(retire: Annotated[Optional[str], Field(description="A learning id to retire. Only when the person says"
                                                               " the decision no longer holds.")] = None,
              why: Annotated[str, Field(description="With retire: the person's reason.")] = "",
              confirm: Annotated[Optional[str], Field(description="A learning id to confirm against the code as it is"
                                                                " now, so it is no longer stale. Only when the person"
                                                                " says the decision still holds.")] = None) -> dict:
    """Past decisions on review findings: each finding a person rejected, the reason in their words, and the code it
    is about. `spec_review_facts` lists the ones that apply to a change as `learnings_that_apply`. A learning whose
    code has changed since is `stale`, naming the nodes `edited` and `gone`: it still applies, and whether it holds
    is the person's call. One kept before Leyline recorded its code has `code` unknown."""
    from . import learnings as learned
    if confirm:
        return learned.confirm(_db(), confirm)
    return learned.retire(_db(), retire, why) if retire else learned.listing(_db())


@_tool(items=25, keep=("tasks", "scenarios", "why_not"))
def spec_verify(change: ChangeArg,
                before_run: Annotated[Optional[str], Field(description="Label of a recorded test run from before.")] = None,
                after_run: Annotated[Optional[str], Field(description="Label of a recorded test run from after.")] = None) -> dict:
    """Rarely needed: the verify step of `check` alone, with test runs named by label and no re-map. Use `check`."""
    folder, err = _change(change)
    if err:
        return {"error": err}
    return spec_loop.verify(_db(), folder, before_run, after_run)


@_tool(items=30, keep=("next",), more="Lists were cut to keep this answer short; `page` says what drifted, spec by spec.")
def drift(path: Annotated[Optional[str], Field(description="The repository or its openspec/ folder. Default: the server's"
                                                           " directory and every mapped repository.")] = None,
          accept: Annotated[bool, Field(description="Record the code as it is now as what the specs mean. Only when the"
                                                    " person says the specs and the code agree.")] = False) -> dict:
    """Specs that no longer match the code: every backticked code name in the living specs (openspec/specs/) and in
    finished changes, compared with the map and with what it meant when the change was checked done. Each name is
    gone, renamed (to the name in `renamed.to`), moved, signature (changed), ambiguous (could now be several things),
    body (changed inside; not drift by itself) or ok. Re-maps changed code first. `fails` is true when something is
    gone, renamed or changed signature."""
    from . import drift as drift_mod
    with _lock:
        try:
            r = drift_mod.run(_path(), path if path is not None else Path.cwd(), accept)
        finally:
            _generation[0] += 1
    groups = [{**g, "items": [{k: v for k, v in it.items() if k not in ("ids", "was_ids")} for it in g["items"]
                              if it["state"] != "ok"]} for g in r["groups"]]
    return {"fails": r["fails"], "counts": r["counts"], "page": drift_mod.text(r),
            "next": drift_mod.next_steps(r, for_agent=True), "groups": [g for g in groups if g["items"]],
            **({"accepted": r["accepted"]} if "accepted" in r else {}), "problems": r["problems"]}


# -- tours ----------------------------------------------------------------------------------------
@_tool(items=50)
def tours() -> dict:
    """List tours: ordered walks through the code. The orientation tour is generated from the map on every
    index; others were written by an agent or the person."""
    return tour_store.listing(_db())


@_tool(items=60)
def tour(tour_id: Annotated[str, Field(description="A tour id from `tours`.")]) -> dict:
    """One tour, stop by stop: what each stop points at and what to notice there."""
    return tour_store.get(_db(), tour_id)


@_tool
def save_tour(title: Annotated[str, Field(description="The tour's name.")], stops: Annotated[list[Stop], Field(min_length=1, description="In the order you would explain"
                                                                                      " the code aloud.")],
              audience: Annotated[str, Field(description="Who it is for, such as `new to the payment code`.")] = "") -> dict:
    """Save a tour you wrote for the person: one feature end to end, or what someone needs before changing one
    module. Only point at things you looked at; say in each narrative what you inferred."""
    return tour_store.save(_db(), title, [dict(s) for s in stops], audience, "mcp")


# -- skills: step-by-step instructions for whole jobs, as a tool and as prompts ----------------------
@_tool(needs_store=False, items=0)
def skills(skill: Annotated[Optional[str], Field(description="A skill's name from the list, such as leyline-ask: returns"
                                                            " its text, to follow step by step.")] = None) -> dict:
    """The skills that ship with Leyline: instructions for whole jobs done with these tools (answer a question about
    the code, plan a change, make a small one, review a pull request). With no name, each one's name and when to use
    it; with `skill`, its text. Each is also an MCP prompt of the same name."""
    if skill:
        s = agent_skills.find(skill)
        if s is None:
            return {"error": f"No skill named {skill!r}. Call `skills` with no skill to list them."}
        return {"name": s.name, "description": s.description, "text": s.body}
    return {"skills": [{"name": s.name, "description": s.description} for s in agent_skills.available()],
            "how": "Pick the one whose description fits what the person asked, then follow its text: call `skills` with"
                   " skill set to its name, or load the MCP prompt of the same name. The person can install them for"
                   " their agent with `leyline skills install` in the repository."}


def _skill_prompt(s: agent_skills.Skill) -> None:
    def load(request: Annotated[str, Field(description="What the person asked, if anything; it is added after the"
                                                       " skill's text.")] = "") -> str:
        body = agent_skills.read(s.folder).body   # read on each call, so an edited skill is served as it is now
        return body + (f"\n\nThe person asked: {request.strip()}" if request.strip() else "")
    mcp.prompt(name=s.name, description=s.description)(load)


for _s in agent_skills.available():
    _skill_prompt(_s)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
