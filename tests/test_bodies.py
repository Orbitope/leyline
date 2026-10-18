"""`context` with `bodies`: the code itself after the outline. The whole body of the focus, the lines around the call in
each caller, and the types it takes or returns as declarations: no sibling methods, each text once, within the budget,
and never through a link that leaves the repository or a file edited since the map."""

import json
import os
from pathlib import Path

import pytest

from leyline import context, store
from leyline.indexer import index

MODELS = '''class Order:
    id: int
    total: float
    note: str

    def pay(self):
        return self.total

    def refund(self):
        return -self.total


class Receipt:
    order: Order
    amount: float
'''
SERVICE = '''from pkg.models import Order, Receipt


def settle(order: Order) -> Receipt:
    total = order.total
    if total > 100:
        total -= 5
    return Receipt(order, total)


def helper_one():
    return 1


def helper_two():
    return 2
'''
API = '''from pkg.service import settle


def checkout(order):
    return settle(order)
'''
TWICE = '''import sys

if sys.version_info >= (3,):
    def pick():
        return "new"
else:
    def pick():
        return "old"
'''
NEST = '''from pkg.service import settle


def outer(order):
    def inner():
        return settle(order)
    return inner()
'''
RET = '''export interface Receipt {
  amount: number;
  note: string;
}

export interface Order {
  id: number;
}

export function make(order: Order): Receipt {
  return { amount: order.id, note: "x" };
}
'''
TEST = '''from pkg.service import settle
from pkg.models import Order


def test_settle():
    assert settle(Order())
'''


def long_caller() -> str:
    """60 lines, the first marked, calling settle on the 50th."""
    body = ["def long_caller(order):", "    FIRST_LINE_MARK = 0"]
    body += [f"    x{k} = {k}" for k in range(47)]
    body += ["    return settle(order)  # THE_CALL"]
    body += [f"    y{k} = {k}" for k in range(10)]
    return "from pkg.service import settle\n\n\n" + "\n".join(body) + "\n"


def top_level() -> str:
    lines = ["from pkg.service import settle", ""]
    lines += [f"TOP_MARK_{k:02d} = {k}" for k in range(30)]
    lines += ["RESULT = settle(None)  # TOP_CALL"]
    lines += [f"AFTER_MARK_{k:02d} = {k}" for k in range(20)]
    return "\n".join(lines) + "\n"


HUB = """from pkg.models import Order


def hub(order: Order):
    return order.total
"""


def many() -> str:
    """Sixty callers of hub of forty lines each, for a focus with more callers than any budget holds."""
    out = ["from pkg.hub import hub", ""]
    for k in range(60):
        out += ["", f"def caller_{k:02d}(order):"] + [f"    v{j} = {j}" for j in range(36)] + ["    return hub(order)"]
    return "\n".join(out) + "\n"


def fan(k: int) -> str:
    return f"from pkg.hub import hub\n\n\ndef use_{k:02d}(order):\n    return hub(order)\n"


def write(root: Path) -> None:
    files = {"pkg/__init__.py": "", "pkg/models.py": MODELS, "pkg/service.py": SERVICE, "pkg/api.py": API,
             "pkg/twice.py": TWICE, "pkg/nest.py": NEST, "pkg/ret.ts": RET, "pkg/long.py": long_caller(),
             "pkg/script.py": top_level(), "pkg/many.py": many(), "pkg/hub.py": HUB, "tests/test_service.py": TEST}
    files.update({f"fan/f{k:02d}.py": fan(k) for k in range(30)})
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


@pytest.fixture
def mapped(tmp_path):
    root = tmp_path / "repo"
    write(root)
    db = tmp_path / "s.db"
    index(root, db, "b")
    con = store.connect(db)
    yield root, db, con
    con.close()


def bodies_of(r: dict) -> str:
    """The part of the text that is the bodies."""
    return r["text"].split("Bodies (", 1)[1]


def test_the_context_outline_carries_the_bodies_of_the_focus_and_its_callers(mapped):
    root, db, con = mapped
    r = context.build(con, ["settle"], 2000, True)
    assert "error" not in r and r["tokens"] <= r["budget_tokens"] == 2000
    got = bodies_of(r)
    assert "def settle(order: Order) -> Receipt:" in got and "total -= 5" in got          # the whole body
    assert "return settle(order)" in got and "def checkout(order):" in got                 # a caller
    outline_part = r["text"].split("Bodies (", 1)[0]
    assert len(outline_part) <= 2000 * 4 // 3 + 400                                       # about a third of the budget
    roles = {x["name"]: x["role"] for x in r["bodies"]}
    assert roles["settle"] == "focus" and roles["checkout"] == "caller"
    assert all("text" not in x for x in r["bodies"])                                      # described, not repeated


def test_a_body_does_not_bring_its_sibling_methods(mapped):
    root, db, con = mapped
    got = bodies_of(context.build(con, ["settle"], 2000, True))
    assert "helper_one" not in got and "helper_two" not in got
    got = bodies_of(context.build(con, ["Order.pay"], 2000, True))
    assert "return self.total" in got and "def refund" not in got and "-self.total" not in got


def test_the_types_a_focus_takes_or_returns_show_their_declaration_and_fields(mapped):
    root, db, con = mapped
    got = bodies_of(context.build(con, ["settle"], 3000, True))
    assert "class Order" in got and "total: float" in got and "note: str" in got
    assert "def pay" not in got and "def refund" not in got                              # fields, not methods
    ts = bodies_of(context.build(con, ["make"], 3000, True))                              # a return type, in TypeScript
    assert "interface Order" in ts and "interface Receipt" in ts and "amount: number" in ts


def test_a_body_that_does_not_fit_is_named(mapped):
    root, db, con = mapped
    r = context.build(con, ["hub"], 600, True)
    assert r["tokens"] <= 600
    got = bodies_of(r)
    assert got.index("def hub") < got.index("Bodies left out:")                         # the focus first
    left = [x for x in r["bodies"] if x["shown"] == "left out"]
    assert left and all(x.get("why") for x in left)
    assert "Bodies left out:" in r["text"].splitlines()[-1]


def test_a_hub_does_not_crowd_out_the_types(mapped):
    root, db, con = mapped
    r = context.build(con, ["hub"], 1200, True)
    got = bodies_of(r)
    assert "class Order" in got and "total: float" in got                               # the type, before any caller
    assert any(x["role"] == "caller" and x["shown"] == "left out" for x in r["bodies"])


def test_a_long_caller_is_cut_around_the_call(mapped):
    root, db, con = mapped
    r = context.build(con, ["settle"], 8000, True)
    mine = next(x for x in r["bodies"] if x["name"] == "long_caller")
    assert mine["call_line"] and mine["shown"] == "cut"
    got = bodies_of(r)
    assert "THE_CALL" in got and "FIRST_LINE_MARK" not in got
    block = got.split("long_caller", 1)[1].split("\n\n", 1)[0]
    assert len([x for x in block.splitlines() if x.startswith("    ")]) <= context.CALLER_LINES


def test_the_top_level_of_a_file_is_a_caller_with_a_short_window(mapped):
    root, db, con = mapped
    r = context.build(con, ["settle"], 8000, True)
    got = bodies_of(r)
    assert "TOP_CALL" in got and "TOP_MARK_00" not in got
    top = next(x for x in r["bodies"] if x["path"] == "pkg/script.py")
    block = got.split("pkg/script.py", 1)[1].split("\n\n", 1)[0]
    assert len([x for x in block.splitlines() if x.startswith("    ")]) <= context.TOP_LEVEL_LINES and top["shown"] == "cut"


def test_a_file_as_the_focus_shows_at_most_three_bodies(mapped):
    root, db, con = mapped
    r = context.build(con, ["pkg/many.py"], 3000, True)
    assert r["tokens"] <= 3000
    assert sum(1 for x in r["bodies"] if x["role"] == "focus" and x["shown"] != "left out") <= context.FOCUS_BODIES
    assert any("highest ranked focus functions" in x.get("why", "") for x in r["bodies"])


def test_a_body_is_shown_once(mapped):
    root, db, con = mapped
    got = bodies_of(context.build(con, ["inner", "outer"], 3000, True))
    assert got.count("return settle(order)") == 1                                         # the enclosing caller adds nothing
    only = bodies_of(context.build(con, ["inner"], 3000, True))
    assert only.count("return settle(order)") == 1 and "def outer" not in only.split("Bodies left out")[0]


def test_a_function_defined_twice_shows_each_definition(mapped):
    root, db, con = mapped
    got = bodies_of(context.build(con, ["pick"], 2000, True))
    assert 'return "new"' in got and 'return "old"' in got
    assert "else:" not in got                                                              # not what sits between them


def test_a_hub_reads_only_the_files_of_the_bodies_it_shows(mapped, monkeypatch):
    import leyline.indexer as ix
    root, db, con = mapped
    opened = []
    real = ix.source_lines
    monkeypatch.setattr(ix, "source_lines", lambda p: (opened.append(str(p)), real(p))[1])
    r = context.build(con, ["hub"], 700, True)
    shown = sum(1 for x in r["bodies"] if x["shown"] != "left out")
    assert len(set(opened)) <= shown + 10
    assert sum(1 for x in r["bodies"] if x["shown"] == "left out") >= 1


def test_a_file_that_links_outside_the_repository_is_not_read(mapped, tmp_path):
    root, db, con = mapped
    secret = tmp_path / "outside" / "api.py"
    secret.parent.mkdir()
    secret.write_text("# SECRET_OUTSIDE\n" + API)
    (root / "pkg" / "api.py").unlink()
    os.symlink(secret, root / "pkg" / "api.py")                                           # the mapped file became a link out
    r = context.build(con, ["settle", "checkout"], 3000, True)
    assert "SECRET_OUTSIDE" not in r["text"]
    gone = next(x for x in r["bodies"] if x["name"] == "checkout")
    assert gone["shown"] == "left out" and "link out of the repository" in gone["why"]


def test_a_body_changed_since_the_map_is_not_shown(mapped):
    root, db, con = mapped
    svc = root / "pkg" / "service.py"
    svc.write_text(SERVICE.replace("total -= 5", "total -= 7"))                          # edited, not mapped again
    r = context.build(con, ["settle"], 2000, True)
    assert "total -= 7" not in r["text"] and "total -= 5" not in r["text"]
    assert any(x["name"] == "settle" and "changed since the map" in x["why"] for x in r["bodies"])


def test_a_large_budget_through_the_tool_keeps_the_lines_that_say_what_was_left_out(mapped, monkeypatch):
    pytest.importorskip("mcp")
    from leyline import server
    root, db, con = mapped
    monkeypatch.setenv("LEYLINE_DB", str(db))
    text = server.context(focus=["hub"], budget_tokens=8000, bodies=True)
    d = json.loads(text)
    assert "cut" not in d and len(text) <= 44_000 and d["tokens"] <= 8000
    assert d["text"].rstrip().splitlines()[-1].startswith("Bodies left out:")
    assert d["bodies"] and all("text" not in x for x in d["bodies"])


def test_a_small_budget_is_raised_and_says_so(mapped):
    root, db, con = mapped
    r = context.build(con, ["settle"], 200, True)
    assert r["budget_tokens"] == context.BODIES_MIN_TOKENS == 600 and "raised to 600" in r["note"]
    assert "raised to 600" in r["text"] and r["tokens"] <= 600


def test_without_bodies_the_outline_is_as_before(mapped):
    root, db, con = mapped
    plain = context.build(con, ["settle"], 2000)
    assert "bodies" not in plain and "Bodies" not in plain["text"] and "note" not in plain
    assert plain["text"] == context.build(con, ["settle"], 2000, False)["text"]
    assert context.build(con, ["settle"], 200)["budget_tokens"] == 200                    # no raising without bodies
