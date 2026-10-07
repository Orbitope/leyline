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
        " from syntax and can be wrong. Long lists are cut: `cut` says what was cut and `more` how to see the rest."
    ),
)

LIMIT = 24_000   # characters; an answer much longer than this crowds out the rest of an agent's context

_local = threading.local()
_lock = threading.Lock()   # one re-index at a time
_generation = [0]          # bumped on every re-index, so each thread opens the store afresh


def _path() -> str:
    return os.environ.get("LEYLINE_DB") or ".leyline/leyline.db"


def _db() -> sqlite3.Connection:
    # The server may run each tool call on a different worker thread, and a SQLite connection
    # belongs to the thread that opened it. Keep one per thread.
    con = getattr(_local, "con", None)
    if con is None or getattr(_local, "gen", None) != _generation[0]:
        con = _local.con = store.connect(_path())
        _local.gen = _generation[0]
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


def _largest(x, path: str = "", best=None):
    """The longest list (of more than 3 items) or string (over 1,000 characters) inside x, as
    (size, holder, key, path)."""
    items = x.items() if isinstance(x, dict) else enumerate(x) if isinstance(x, list) else ()
    for k, v in items:
        p = f"{path}.{k}" if isinstance(k, str) and path else (k if isinstance(k, str) else f"{path}[]")
        if (isinstance(v, list) and len(v) > 3) or (isinstance(v, str) and len(v) > 1000):
            size = len(_dump(v))
            if best is None or size > best[0]:
                best = (size, x, k, p)
        if isinstance(v, (dict, list)):
            best = _largest(v, p, best)
    return best


MORE = ("Lists were cut to keep this answer short; `cut` says which (shown of total). To see the rest, narrow the call"
        " (a node id, `scope`, `kind` or `through`), raise `limit`, or page with `offset` where the tool has it.")


def _fit(out: dict, cut: dict, limit: int = LIMIT, more: str = MORE) -> str:
    """The answer as compact JSON of at most about `limit` characters: the longest lists and strings are
    shortened until it fits, and `cut` says what was cut."""
    text = _dump(out)
    for _ in range(200):
        if len(text) <= limit:
            break
        best = _largest(out)
        if best is None:
            break
        size, holder, key, p = best
        v, excess = holder[key], len(text) - limit + 200
        if isinstance(v, list):
            keep = max(3, min(len(v) - 1, len(v) - -(-excess * len(v) // max(size, 1))))
            cut[p] = f"{keep} of {cut[p].split(' of ')[1] if p in cut else len(v)}"
            holder[key] = v[:keep]
        else:
            keep = max(1000, len(v) - excess)
            holder[key] = v[:keep] + f"\n[... cut here: {len(v):,} characters in all]"
            cut[p] = f"{keep:,} of {len(v):,} characters"
        text = _dump({**out, "cut": cut} if cut else out)
    if cut:
        out["cut"], out["more"] = cut, more
        text = _dump(out)
    return text


def _tool(fn=None, *, name: Optional[str] = None, needs_store: bool = True, items: int = 0, keep: tuple = (),
          more: str = MORE):
    """Register `fn` as a tool. Its answer is a dict; one with "error" is returned as a tool error. `items` caps
    every list in the answer (0 leaves them to the size limit alone); lists under keys in `keep` stay whole.
    `more` tells the agent how to see what was cut."""
    if fn is None:
        return lambda f: _tool(f, name=name, needs_store=needs_store, items=items, keep=keep, more=more)

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
        return _fit(out, cut, more=more)

    # No return annotation: the answer is text (compact JSON), not a structured result to validate.
    run.__signature__ = inspect.signature(fn, eval_str=True).replace(return_annotation=inspect.Signature.empty)
    del run.__wrapped__
    return mcp.tool(name=name or fn.__name__)(run)


# -- argument types ---------------------------------------------------------------------------------
NodeId = Annotated[str, Field(description="A node id exactly as `search`, `overview` or `expand` returned it.")]
ChangeArg = Annotated[str, Field(description="The change: its folder (openspec/changes/<id>, absolute or relative to the"
                                             " server's directory) or just its id.")]
TestOutput = Annotated[Optional[str], Field(description="The test runner's output as text: TAP (vitest --reporter=tap,"
                                                        " node --test), `pytest -rA`, or one PASS or FAIL line per test."
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
        return None, ("found no test results in test_output: it reads TAP (vitest --reporter=tap, node --test), pytest -rA,"
                      " or one PASS or FAIL line per test. Pass other formats as test_results.")
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
def check(change: ChangeArg, test_output: TestOutput = None, test_results: TestResults = None) -> dict:
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
            v = loop.check(_path(), folder, results)
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
    r = query.impact(_db(), node_id, max_depth)
    mods = r["by_module"]
    for m in mods:
        if len(m["direct"]) > 5:
            m["direct"], m["direct_more"] = m["direct"][:5], len(m["direct"]) - 5
    r["by_module"] = {"total": len(mods), "items": mods[:limit]}
    r["flows_through"]["items"] = r["flows_through"]["items"][:limit]
    return r


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


@_tool(items=50)
def coverage(node_id: Annotated[Optional[str], Field(description="A function: the tests under which it ran.")] = None,
             flow_id: Annotated[Optional[str], Field(description="A test's flow: its static path against what ran.")] = None,
             import_path: Annotated[Optional[str], Field(description="A coverage.py data file or Cobertura XML report to"
                                                                     " read into the store first.")] = None) -> dict:
    """Measured test coverage, as opposed to the static paths in `flows`. With no argument: per module, how many
    functions ran, how many are on a test's path but never ran, and how many ran through links the map does not
    have."""
    out = {}
    if import_path:
        if not Path(import_path).is_file():
            return {"error": f"No file at {import_path} (relative paths are from {Path.cwd()})."}
        imported = measured.import_file(_db(), import_path)
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
    folder, err = _change(change)
    if err:
        return {"error": err}
    return spec_loop.review_facts(_db(), folder, reviewer)


@_tool(needs_store=False, items=20, keep=("next",),
       more="Lists were cut to keep this answer short; `page` has the whole review page and `spec_review_facts` the facts.")
def review_pr(base: Annotated[Optional[str], Field(description="The branch the change will merge into, or a commit."
                                                             " Default: origin's default branch, else main.")] = None,
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
    called), and which tests run it. Then run the adversarial review on the returned `change_id` with
    `spec_review_facts` and `spec_finding`, as for a spec."""
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
    return {"change_id": r["change_id"], "page": pr.text(r), "written": r["page"], "size": r["size"],
            "reaches": r["reaches"], "tests": r["tests"], "other_files": r["other_files"], "house_rules": r["house_rules"],
            "next": [f"Run the leyline-adversarial-review skill on {r['change_id']}: spec_review_facts with reviewer logic,"
                     " then performance; file findings with spec_finding. Show the person the page."]}


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
              why: Annotated[str, Field(description="With retire: the person's reason.")] = "") -> dict:
    """Past decisions on review findings: each finding a person rejected, the reason in their words, and the code it
    is about. `spec_review_facts` lists the ones that apply to a change as `learnings_that_apply`."""
    from . import learnings as learned
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


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
