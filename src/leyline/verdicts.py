"""One verdict for each task and scenario that `check` judges, and which verdicts hold up "done as agreed".

    proven          task: the code it names changed as it says.
                    scenario: its test passes after the change, and failed or did not exist before, or passed
                    both times while running the changed code.
    partial         task: some of the code it names changed, some did not.
                    scenario: some results that carry its name pass and some fail.
    contradicted    task: the code it names did not change (or what it adds is missing) while other code did.
                    scenario: its test fails after the change.
    inconclusive    task: the map cannot place the code it names, or nothing changed at all.
                    scenario: no pass or fail was recorded for it.
    needs a person  task: it names no code (docs, say). scenario: its test passes, but Leyline can tell it does not
                    run the changed code, or cannot tell whether it does.

By default partial, contradicted and inconclusive block; "needs a person" is listed and does not. A project can
change that in `openspec/leyline.toml`:

    [check]
    blocking = ["contradicted", "inconclusive", "partial"]
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

PROVEN, PARTIAL, CONTRADICTED, INCONCLUSIVE, PERSON = "proven", "partial", "contradicted", "inconclusive", "needs a person"
VERDICTS = (PROVEN, PARTIAL, CONTRADICTED, INCONCLUSIVE, PERSON)
DEFAULT_BLOCKING = (PARTIAL, CONTRADICTED, INCONCLUSIVE)
CONFIG = "leyline.toml"           # in the `openspec/` folder that holds the changes
# The order the summary line counts them in, and the words it uses.
ORDER = (PROVEN, PERSON, PARTIAL, CONTRADICTED, INCONCLUSIVE)
SHORT = {PERSON: "needs you"}
ALIASES = {"needs you": PERSON, "needs person": PERSON, "person": PERSON, "proved": PROVEN, "partly": PARTIAL}


# -- config ----------------------------------------------------------------------------------------
def _toml(text: str) -> dict:
    """Read TOML: tomllib where Python has it (3.11+), else a small reader for what this file needs: tables,
    and keys set to a string, a number, true or false, or a list of strings (across lines too)."""
    try:
        import tomllib
    except ImportError:
        return _tiny_toml(text)
    return tomllib.loads(text)


_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')


def _strip_comment(line: str) -> str:
    quote = None
    for k, ch in enumerate(line):
        if quote:
            if ch == quote and (quote == "'" or line[k - 1] != "\\"):
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:k]
    return line


def _tiny_toml(text: str) -> dict:
    out: dict = {}
    table = out
    pending = ""
    for n, raw in enumerate(text.splitlines(), 1):
        line = (pending + " " + _strip_comment(raw).strip()) if pending else _strip_comment(raw).strip()
        if not line:
            continue
        if not pending and line.startswith("[") and "=" not in line:
            if not line.endswith("]") or line.startswith("[["):
                raise ValueError(f"line {n}: cannot read the table name {line!r}")
            table = out
            for part in line[1:-1].strip().split("."):
                table = table.setdefault(part.strip().strip('"'), {})
            continue
        key, eq, value = line.partition("=")
        if not eq:
            raise ValueError(f"line {n}: expected `key = value`")
        value = value.strip()
        if value.startswith("[") and value.count("[") > value.count("]"):
            pending = line             # a list that goes on to the next line
            continue
        pending = ""
        table[key.strip().strip('"')] = _tiny_value(value, n)
    if pending:
        raise ValueError("a list is not closed with ]")
    return out


def _tiny_value(value: str, n: int):
    if value.startswith("["):
        if not value.endswith("]"):
            raise ValueError(f"line {n}: a list is not closed with ]")
        inner = value[1:-1]
        if _STRING.sub("", inner).replace(",", "").strip():
            raise ValueError(f"line {n}: only lists of quoted words are read here")
        return [_unquote(m) for m in _STRING.finditer(inner)]
    m = _STRING.fullmatch(value)
    if m:
        return _unquote(m)
    if value in ("true", "false"):
        return value == "true"
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"line {n}: cannot read the value {value!r}") from None


def _unquote(m) -> str:
    if m.group(1) is None:
        return m.group(2)                     # a 'literal string': no escapes
    try:
        return json.loads('"' + m.group(1) + '"')
    except ValueError:
        return m.group(1)


def _name(word) -> Optional[str]:
    w = re.sub(r"[-_\s]+", " ", str(word)).strip().lower()
    w = ALIASES.get(w, w)
    return w if w in VERDICTS else None


def config_path(change_dir: str | Path) -> Optional[Path]:
    """`openspec/leyline.toml` for the `openspec/` folder the change sits in, when there is one."""
    for p in Path(change_dir).resolve().parents:
        if p.name == "openspec":
            return p / CONFIG
    return None


def load_config(change_dir: str | Path) -> dict:
    """Which verdicts block, from the project's file or the default. Never raises: a file that cannot be read
    leaves the default in place and says why in `notes`."""
    out = {"blocking": list(DEFAULT_BLOCKING), "file": None, "notes": []}
    path = config_path(change_dir)
    if path is None or not path.is_file():
        return out
    shown = f"{path.parent.name}/{path.name}"
    try:
        data = _toml(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:   # tomllib.TOMLDecodeError is a ValueError
        out["notes"].append(f"{shown} could not be read ({e}), so the default applies.")
        return out
    section = data.get("check")
    if not isinstance(section, dict) or "blocking" not in section:
        return out
    listed = section["blocking"]
    if isinstance(listed, str):
        listed = [listed]
    if not isinstance(listed, list):
        out["notes"].append(f"{shown}: `blocking` should be a list of verdicts, so the default applies.")
        return out
    blocking = []
    for w in listed:
        v = _name(w)
        if v is None:
            out["notes"].append(f"{shown} names \"{w}\", which is not a verdict; it was left out."
                                f" The verdicts are {', '.join(VERDICTS)}.")
        elif v != PROVEN and v not in blocking:
            blocking.append(v)
    out["blocking"] = [v for v in VERDICTS if v in blocking]
    out["file"] = shown
    return out


# -- judging ---------------------------------------------------------------------------------------
def _task_verdict(t: dict, row, names, touched: set, removed: set, after_run: Optional[str]) -> tuple[str, str]:
    missing = ", ".join(t["missing"][:3])
    if t["state"] == "checked by you":
        return PERSON, "it names no code, so the map cannot see it done"
    if t.get("removal"):   # a removal or rename, judged by what is left (leyline.removal)
        return t["removal"]["verdict"], t["removal"]["why"]
    if t["state"] == "done":
        return PROVEN, "the code it names changed"
    if t["state"] == "partly":
        return PARTIAL, "some of the code it names changed" + (f"; not {missing}" if missing else "")
    if not (touched or removed):
        return INCONCLUSIVE, "nothing in the code changed since the plan; was the code mapped again after the edit?"
    extra = json.loads(row["attrs"] or "{}") if row is not None else {}
    ids = json.loads(row["nodes"] or "[]") if row is not None else []
    new = extra.get("new", [])
    # Inconclusive only when none of what it names can be placed; code that is there and unchanged contradicts it.
    gone = [i for i in ids if i not in names.by_id and i not in removed]
    homeless = [n for n in new if n.get("parent") and n["parent"] not in names.by_id]
    base = lambda xs: {i.split("(")[0] for i in xs}
    if (gone or homeless) and base(gone) == base(ids) and len(homeless) == len(new):
        what = ", ".join(([i.split("(")[0].split("/")[-1] for i in gone] + [n["name"] for n in homeless])[:3])
        return INCONCLUSIVE, f"the map no longer has {what}, so it cannot tell whether it changed"
    if extra.get("scenarios") and not ids and not new and not after_run:
        return INCONCLUSIVE, "no test results were recorded, so the test it adds cannot be seen"
    action = row["action"] if row is not None else ""
    if action == "remove":
        return CONTRADICTED, f"{missing or 'what it removes'} is still there, unchanged, though other code changed"
    if action == "add":
        return CONTRADICTED, f"{missing or 'what it adds'} is missing, though other code changed"
    return CONTRADICTED, f"{missing or 'the code it names'} did not change, though other code did"


def _get(r, key: str):
    """A field of a result row, whether a stored row or a plain dict that may leave it out."""
    try:
        return r[key]
    except (KeyError, IndexError):
        return None


def _scenario_verdict(s: dict, after: list, before: Optional[list]) -> tuple[str, str]:
    passed = [r for r in after if r["status"] == "pass"]
    failed = [r for r in after if r["status"] == "fail"]
    from .props import from_message   # a property test's smallest failing input, when it printed one
    ex = next((x for x in (from_message(_get(r, "message")) for r in failed) if x), None)
    if passed and failed:
        return PARTIAL, (f"{len(passed)} of {len(passed) + len(failed)} results that carry its name pass"
                         + (f"; one fails for {ex}" if ex else ""))
    if failed:
        if ex:
            return CONTRADICTED, f"fails for {ex}" + ("; its test passed before the change"
                                                    if before and all(r["status"] == "pass" for r in before) else "")
        if before and all(r["status"] == "pass" for r in before):
            return CONTRADICTED, "its test passed before the change and fails now"
        return CONTRADICTED, "its test fails after the change"
    if s["state"] != "passes":
        if s["state"] == "skipped":
            return INCONCLUSIVE, "its test was skipped, so no pass or fail was recorded"
        if s["state"] == "results older than the code":
            return INCONCLUSIVE, ("its test has results, but they are older than the code: run the tests again and"
                                  " pass their output")
        if s["state"] == "no test":
            return INCONCLUSIVE, "it has no test, and no result carries its name"
        return INCONCLUSIVE, "no result for its test was recorded"
    measured, static = s.get("measured_running_the_change"), s.get("reaches_the_change")
    ran = s.get("ran_changed_code")   # per-test coverage from the run after the change (leyline.affected)
    if ran is False:
        return PERSON, ("its test passes, but measured per-test coverage shows it never ran the changed code, so it"
                        " would pass whatever the change did")
    if before is not None and (not before or any(r["status"] == "fail" for r in before)):
        return PROVEN, ("its test failed before the change and passes now" if before
                        else "its test is new since the plan, and passes")
    if ran or measured or (measured is None and static):
        return PROVEN, "its test passes and runs the changed code" + (", as it did before" if before else "")
    if measured is False or (s.get("test") and not static):
        return PERSON, ("its test passes, but " + ("measured coverage shows it never runs the changed code"
                                                   if measured is False else "on the map it does not reach the changed code")
                        + ", so it may not test the change")
    if before is None:
        return PERSON, ("its test passes, but no run from before the change was recorded, and the test is not on the"
                        " map, so whether it tests the change cannot be told")
    return PERSON, ("its test passed before the change too, and it is not on the map, so whether it runs the changed"
                    " code cannot be told")


def blocks(x: dict, blocking) -> bool:
    """Whether an item holds up "done as agreed". A tick in tasks.md does not clear one: the agent ticks tasks as it
    goes, and the verdict comes from the code and the test results, not from the agent's account of its work."""
    return x.get("verdict") in blocking


def judge(con, cid: str, change_dir: str | Path, tasks: list[dict], scenarios: list[dict], names, touched: set,
          removed: set, before_run: Optional[str], after_run: Optional[str]) -> dict:
    """Give each task and scenario its `verdict` and `verdict_why`, in place. Returns the summary: counts, which
    verdicts block, and the reasons (in the words `why_not` already uses) that hold up "done as agreed"."""
    from . import spec   # spec imports this module
    rows = {r["key"]: r for r in con.execute("SELECT * FROM spec_items WHERE change_id = ? AND kind = 'task'", (cid,))}
    for t in tasks:
        t["verdict"], t["verdict_why"] = _task_verdict(t, rows.get(t["key"]), names, touched, removed, after_run)

    def index(run):
        if not run:
            return None
        return spec._results_index(con.execute("SELECT name, status, message FROM test_results WHERE run = ?", (run,)).fetchall())
    after_ix, before_ix = index(after_run), index(before_run)
    for s in scenarios:
        after = spec._scenario_results(after_ix, s["name"]) if after_ix else []
        before = spec._scenario_results(before_ix, s["name"]) if before_ix else None
        s["verdict"], s["verdict_why"] = _scenario_verdict(s, after, before)

    config = load_config(change_dir)
    holds = []
    if any(blocks(t, config["blocking"]) and t["verdict"] != PERSON for t in tasks):
        holds.append("some tasks are not done")
    if any(blocks(s, config["blocking"]) and s["verdict"] != PERSON for s in scenarios):
        holds.append("some scenarios are not proven")
    if any(blocks(x, config["blocking"]) and x["verdict"] == PERSON for x in tasks + scenarios):
        holds.append(f"some items need a person, and {config['file'] or 'the project'} makes that block")
    counts = {v: sum(x["verdict"] == v for x in tasks + scenarios) for v in VERDICTS}
    return {"counts": counts, "blocking": config["blocking"], "config": config["file"], "notes": config["notes"],
            "holds_up": holds}


# -- words -----------------------------------------------------------------------------------------
def summary_line(counts: dict) -> str:
    """"7 proven, 1 needs you, 1 inconclusive"."""
    return ", ".join(f"{counts[v]} {SHORT.get(v, v)}" for v in ORDER if counts.get(v))


def blocking_line(info: dict) -> str:
    """"Blocking: partial, contradicted, inconclusive. Not blocking: needs you (the default)."."""
    b = [SHORT.get(v, v) for v in ORDER if v in info["blocking"]]
    free = [SHORT.get(v, v) for v in ORDER if v not in info["blocking"] and v != PROVEN]
    return (f"Blocking: {', '.join(b) or 'nothing'}." + (f" Not blocking: {', '.join(free)}." if free else "")
            + (f" (Set in {info['config']}.)" if info.get("config") else " (The default.)"))


def _item(x: dict, kind: str) -> str:
    return f"task {x['key']}" if kind == "task" else f"scenario \"{x['name']}\""


def page_lines(v: dict) -> list[str]:
    """The lines under the tables: the count, what blocks, and why each item that is not proven got its verdict."""
    info = v.get("verdicts")
    if not info:
        return []
    L = ["", f"Verdicts: {summary_line(info['counts']) or 'nothing to judge'}. " + blocking_line(info)]
    L += [n for n in info.get("notes", [])]
    rest = [(x, k) for k, xs in (("task", v["tasks"]), ("scenario", v["scenarios"])) for x in xs if x.get("verdict") != PROVEN]
    if rest:
        L += ["", "**Not proven, and why:**"]
        L += [f"- {_item(x, k)}, {x['verdict']}: {x['verdict_why']}"
              + ("" if blocks(x, info["blocking"]) else " (does not block)") for x, k in rest[:20]]
        if len(rest) > 20:
            L.append(f"- and {len(rest) - 20} more")
    return L


def yes_text(v: dict) -> Optional[str]:
    """The "Yes." sentence when something other than proven (or a task that names no code) is let through.
    None leaves the usual sentence, which is right when every item is proven or is a task that names no code."""
    info = v.get("verdicts")
    if not info:
        return None
    odd = [x for x in v["tasks"] if x.get("verdict") not in (PROVEN, PERSON)] + \
          [x for x in v["scenarios"] if x.get("verdict") != PROVEN]
    if not odd:
        return None
    mine = [t["key"] for t in v["tasks"] if t.get("verdict") == PERSON]
    yours = [f"\"{s['name']}\"" for s in v["scenarios"] if s.get("verdict") == PERSON]
    let = [x for x in odd if x.get("verdict") != PERSON]
    head = ("Nothing that blocks is left" + (f" under {info['config']}" if info.get("config") else "")
            if let else "Every task " + ("that names code " if mine else "") + "is done, every scenario's test passes")
    out = head + ", and nothing outside the spec changed."
    if mine:
        out += f" Task{'s' if len(mine) > 1 else ''} {', '.join(mine)} {'are' if len(mine) > 1 else 'is'} yours to check."
    if yours:
        out += (f" Scenario{'s' if len(yours) > 1 else ''} {', '.join(yours[:3])}{' and more' if len(yours) > 3 else ''}"
                f" {'need' if len(yours) > 1 else 'needs'} you to confirm {'they test' if len(yours) > 1 else 'it tests'}"
                " the change.")
    if let:
        out += f" Not proven, and let through: {summary_line({k: sum(x['verdict'] == k for x in let) for k in VERDICTS})}."
    return out


def next_note(v: dict) -> str:
    """For `Next:` after a check that passed: the scenarios left for the person to confirm."""
    yours = [f"\"{s['name']}\"" for s in v["scenarios"] if s.get("verdict") == PERSON]
    if not yours:
        return ""
    many = len(yours) > 1
    return (f" Scenario{'s' if many else ''} {', '.join(yours[:3])}{' and more' if len(yours) > 3 else ''}"
            f" {'need' if many else 'needs'} a person to confirm {'they test' if many else 'it tests'} the change:"
            f" {'their tests pass' if many else 'its test passes'} but may not run the changed code.")


def waiting_lines(v: dict) -> list[str]:
    """For `Next:` when the project makes a person's check block: what waits on the person, and how to clear it."""
    info = v.get("verdicts") or {}
    waiting = [x for x in v["tasks"] + v["scenarios"] if x.get("verdict") == PERSON and blocks(x, info.get("blocking", ()))]
    mine = [x["key"] for x in waiting if "key" in x]              # tasks have a key; scenarios only a name
    yours = [f"\"{x['name']}\"" for x in waiting if "key" not in x]
    out = []
    if mine:
        out.append(f"task{'s' if len(mine) > 1 else ''} {', '.join(mine)} {'name' if len(mine) > 1 else 'names'} no code,"
                   f" and {info.get('config') or 'the project'} makes an item that needs a person block: name the code"
                   f" {'they change' if len(mine) > 1 else 'it changes'} in backticks (then plan again), or take"
                   " \"needs a person\" out of `blocking`.")
    if yours:
        out.append(f"make the test for scenario{'s' if len(yours) > 1 else ''} {', '.join(yours[:3])} run the changed"
                   " code; it passes, but Leyline cannot see it test the change.")
    return out
