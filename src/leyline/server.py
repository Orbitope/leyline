"""MCP server over a Leyline store. Reads facts; writes annotations, views, proposals, rules and test runs."""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Optional

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP

from . import change, coverage as measured, diff, spec as spec_loop, patterns as pattern_labels, query, rules, store, tours as tour_store

mcp = FastMCP(
    "leyline",
    instructions=(
        "Leyline is a graph of a codebase. Start with `overview` for the module map, `search` to find a"
        " node id by name, then `expand` a node to see what it contains and how it connects."
        " Edges marked heuristic come from syntax alone and can be wrong; exact edges cannot."
        " To assess a change described in words: find the nodes it touches with `search` and `expand`,"
        " then call `propose_change`. To show the user any other slice of the code, call `save_view`."
    ),
)

_local = threading.local()


def _db() -> sqlite3.Connection:
    # The server may run each tool call on a different worker thread, and a SQLite connection
    # belongs to the thread that opened it. Keep one per thread.
    con = getattr(_local, "con", None)
    if con is None:
        con = _local.con = store.connect(os.environ.get("LEYLINE_DB", ".leyline/leyline.db"))
    return con


@mcp.tool()
def overview() -> dict:
    """The top-level map: repos, modules with sizes, module-to-module dependencies with counts per
    edge kind, external packages, and which extractors ran. Call this first."""
    return query.overview(_db())


@mcp.tool()
def expand(node_id: str, limit: int = 50) -> dict:
    """One node in detail. A module returns its files, public types, entry points and dependencies.
    A type returns its members, bases and users. A callable returns its signature, callers and callees.
    Ids come from `overview` or `search`."""
    return query.expand(_db(), node_id, limit)


@mcp.tool()
def search(text: str, kind: Optional[str] = None, limit: int = 20) -> dict:
    """Find nodes by name, qualified name or path. `kind` narrows to one of: module, file, type,
    callable, field, entry_point, external."""
    return query.search(_db(), text, kind, limit)


@mcp.tool()
def neighbors(node_id: str, direction: str = "both", kinds: Optional[list[str]] = None,
              limit: int = 100) -> dict:
    """Edges around a node. `direction` is out, in or both. `kinds` filters by edge kind: calls,
    imports, uses_type, extends, implements, instantiates, has_field, exposes, depends_on, reads, writes
    (reads and writes run from a function to a field)."""
    return query.neighbors(_db(), node_id, direction, kinds, limit)


@mcp.tool()
def source(node_id: str, max_lines: int = 200) -> dict:
    """The source text of a type, callable or field, read from the indexed working tree."""
    return query.source(_db(), node_id, max_lines)


@mcp.tool()
def flows(kind: Optional[str] = None, through: Optional[str] = None) -> dict:
    """List flows: paths walked from each entry point and each test. `kind` is entry or test.
    `through` keeps only flows that pass a given node id."""
    return query.flows(_db(), kind, through)


@mcp.tool()
def flow(flow_id: str, max_steps: int = 400) -> dict:
    """One flow step by step, in source order, with call depth. Ids come from `flows`."""
    return query.flow(_db(), flow_id, max_steps)


@mcp.tool()
def trace(from_id: str, to_id: str) -> dict:
    """The shortest chain of calls and channels from one function to another."""
    return query.trace(_db(), from_id, to_id)


@mcp.tool()
def impact(node_id: str, max_depth: int = 6) -> dict:
    """What can reach a node: callers grouped by module and the flows that pass through it.
    Call this before proposing a change to the node."""
    return query.impact(_db(), node_id, max_depth)


@mcp.tool()
def annotate(node_id: str, key: str, value: str, evidence: Optional[list[str]] = None,
             confidence: Optional[float] = None, layer: str = "inferred") -> dict:
    """Record a statement about a node: for example key `name` or `responsibility` on a system, or
    `summary` on a module. `layer` is `inferred` (yours; must list the node ids it is based on as
    `evidence`, with a confidence from 0 to 1) or `intent` (the user's own statement). The store flags
    an inferred annotation as stale when the files behind its evidence change. Facts cannot be written."""
    return query.annotate(_db(), node_id, key, value, evidence, confidence, layer, "mcp")


@mcp.tool()
def propose_change(intent: str, targets: list[dict], title: Optional[str] = None, depth: int = 4) -> dict:
    """Assess a change before any code is written, and save a blast-radius view the user can open.
    `intent` is the change in the user's words. `targets` lists what will change, each as
    {"id": node id, "action": ..., "note": why}. Actions: `behavior` (same contract, different result),
    `signature` (parameters or return type change), `rename`, `remove`, or `add` for something new:
    {"action": "add", "name": ..., "parent": id of the type, file or module it goes in,
     "uses": [ids it will call], "used_by": [ids that will call it]}.
    Returns what must be edited, what is reached, the tests to run, entry points affected, channel
    crossings, untested targets and risk flags. Target a function when you can; a type, file or system
    counts every function inside it."""
    return change.propose(_db(), intent, targets, title, depth, "mcp")


@mcp.tool()
def save_view(title: str, narrative: str, marks: list[dict], legend: Optional[dict] = None) -> dict:
    """Save a custom view for the user: any set of nodes worth looking at together, such as the parts
    of a design alternative or everything involved in one feature. Each mark is
    {"id": node id, "role": short label for its group, "note": one line on why it is here}.
    `narrative` explains the view in a few sentences. `legend` maps each role to a description.
    The view appears in the map's Views tab."""
    return change.save_view(_db(), title, narrative, marks, "custom", "mcp", legend=legend)


@mcp.tool()
def views() -> dict:
    """List saved views, including the blast-radius view of every proposed change."""
    return change.list_views(_db())


@mcp.tool()
def view(view_id: str) -> dict:
    """One saved view in full: its narrative, marks and, for a change, its impact report."""
    return change.get_view(_db(), view_id)


@mcp.tool()
def review_change(change_id: str, before_run: Optional[str] = None, after_run: Optional[str] = None) -> dict:
    """After a proposed change has been implemented and the repository re-indexed: compare the graph
    with the snapshot taken at proposal time. Returns which predicted edits happened, which edits were
    not predicted, new and removed dependencies between modules, flows whose path changed, rule
    results, and the test delta if two runs were recorded with `record_test_run`. Saves a review view."""
    return diff.review(_db(), change_id, before_run, after_run)


@mcp.tool()
def record_test_run(run: str, results: list[dict]) -> dict:
    """Store the outcome of one test run under a label such as `before` or `after`.
    Each result is {"name": test name, "status": "pass" | "fail" | "skip", "message": optional}."""
    return diff.record_tests(_db(), run, results)


@mcp.tool()
def add_rule(kind: str, selector_from: str, selector_to: str = "", edge_kinds: Optional[list[str]] = None,
             severity: str = "error", reason: str = "", confirmed: bool = False) -> dict:
    """Add an architecture rule. Kinds: `forbid` (nothing in selector_from may link to selector_to),
    `no_cycle` (selector_from is `modules` or `systems`), `must_be_tested` (every function in
    selector_from is on some test's path). Selectors: `module:Name`, `system:Name`, `external:Name`,
    `path:prefix`, `id:prefix`, `*`. A rule is the user's intent: pass confirmed=true only when the
    user stated it. Otherwise it is stored as suggested, with your `reason`."""
    return rules.add_rule(_db(), kind, selector_from, selector_to, edge_kinds, severity, reason,
                          "confirmed" if confirmed else "suggested", "mcp")


@mcp.tool()
def check_rules() -> dict:
    """Evaluate every architecture rule against the current graph, with examples of each violation."""
    return rules.check(_db())


@mcp.tool()
def patterns(pattern: Optional[str] = None, node_id: Optional[str] = None, include_tests: bool = False) -> dict:
    """Design patterns found by their shape in the graph: strategy, decorator, composite, template
    method, observer, factory, builder, singleton, process boundary. Each has the nodes playing each role,
    a rationale and a confidence. Filter by `pattern` name or by `node_id` (a type, module or function).
    A label says the code has the shape, not that the author intended the pattern."""
    return pattern_labels.listing(_db(), pattern, node_id, include_tests)


@mcp.tool()
def label_pattern(pattern: str, roles: dict, rationale: str, confidence: float = 0.6) -> dict:
    """Record a pattern the structural matchers missed, after reading the code. `roles` maps each role
    to the node ids that play it, for example {"adapter": [...], "adaptee": [...]}. `rationale` must
    say what in the code makes it this pattern. The label goes stale when that code changes."""
    return pattern_labels.label(_db(), pattern, roles, rationale, confidence, "mcp")


@mcp.tool()
def shared_state(scope: Optional[str] = None, limit: int = 40) -> dict:
    """Fields assigned from outside the type that declares them, most widely written first: the mutable
    state with no single owner. `scope` is a module id or an id prefix. To see every reader and writer of
    one field, or the fields one function touches, call `expand` on it and read `data`."""
    return query.shared_state(_db(), scope, limit)


@mcp.tool()
def coverage(node_id: Optional[str] = None, flow_id: Optional[str] = None, import_path: Optional[str] = None) -> dict:
    """Measured test coverage, as opposed to the static paths in `flows`. With no argument: per module,
    how many functions ran, how many are on a test's path but never ran, and how many ran through links
    the map does not have. `node_id`: the tests under which that function ran. `flow_id` (a test's flow):
    its static path against what ran. `import_path`: read a coverage.py data file or a Cobertura XML
    report into the store first."""
    out = {}
    if import_path:
        out["imported"] = measured.import_file(_db(), import_path)
    if flow_id:
        return {**out, **measured.compare_flow(_db(), flow_id)}
    if node_id:
        return {**out, "node": node_id, "tests": measured.tests_for(_db(), node_id)}
    return {**out, **measured.summary(_db())}


@mcp.tool()
def spec_brief(change_dir: str, new_baseline: bool = False) -> dict:
    """Assess a change written as an OpenSpec folder (proposal.md, tasks.md, specs/) before it is
    implemented. Ties each task to code named in backticks and each scenario to a test of the same name,
    computes the blast radius, and writes `leyline.md` into the folder: what will be written, what it
    affects, how the person will know it was done. Returns the same, with `gaps` to fix in the spec.
    Run it again after every edit to the spec. Once the code has changed, later briefs keep the first
    picture of the code (`baseline: kept`) so a spec can be amended part-way; `new_baseline` starts over."""
    return spec_loop.brief(_db(), change_dir, new_baseline=new_baseline)


@mcp.tool()
def spec_review_facts(change_dir: str) -> dict:
    """What the graph says about a spec, arranged as the questions a logic reviewer and a performance
    reviewer must answer. Read it, read the code behind anything suspicious, then file findings."""
    return spec_loop.review_facts(_db(), change_dir)


@mcp.tool()
def spec_finding(change_id: str, reviewer: str, severity: str, claim: str, evidence: list[str], proposal: str = "") -> dict:
    """File a review finding against a spec: `reviewer` is logic or performance, `severity` high, medium
    or low, `claim` one sentence a person can check, `evidence` the node ids that show it, `proposal` the
    change to the spec. A finding with no node behind it is refused. Only the person resolves findings."""
    return spec_loop.add_finding(_db(), change_id, reviewer, severity, claim, evidence, proposal)


@mcp.tool()
def spec_findings(change_id: str) -> dict:
    """The findings filed against a spec, and whether the person accepted, rejected or deferred each."""
    return spec_loop.findings(_db(), change_id)


@mcp.tool()
def spec_resolve(finding_id: str, status: str, resolution: str = "") -> dict:
    """Record the person's decision on a finding: accepted, rejected or deferred, with their reason.
    Call this only with a decision the person stated. Never resolve a finding on your own judgment."""
    return spec_loop.resolve_finding(_db(), finding_id, status, resolution)


@mcp.tool()
def spec_verify(change_dir: str, before_run: Optional[str] = None, after_run: Optional[str] = None) -> dict:
    """After the spec is implemented and the repository re-indexed: was it done as agreed? Marks each
    task done or not from the graph diff, each scenario from its test's result, lists edits outside the
    spec, new links between modules and rules newly broken, and appends the result to `leyline.md`."""
    return spec_loop.verify(_db(), change_dir, before_run, after_run)


@mcp.tool()
def tours() -> dict:
    """List tours: ordered walks through the codebase. The orientation tour is generated from the graph
    on every index; others were written by an agent or the user."""
    return tour_store.listing(_db())


@mcp.tool()
def tour(tour_id: str) -> dict:
    """One tour, stop by stop: what each stop points at and what to notice there."""
    return tour_store.get(_db(), tour_id)


@mcp.tool()
def save_tour(title: str, stops: list[dict], audience: str = "") -> dict:
    """Save a tour you wrote for the user: for example one feature end to end, or what someone needs
    before changing one module. Each stop is {"title": ..., "kind": "node" | "flow" | "pattern" | "view" | "repo",
    "ref": the id, "narrative": what to notice here and why it matters}. Order the stops the way you would
    explain the code aloud. Only point at things you looked at; say in the narrative what you inferred."""
    return tour_store.save(_db(), title, stops, audience, "mcp")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
