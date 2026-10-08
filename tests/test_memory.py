"""Mapping a large repository keeps each file's calls packed until a pass reaches the file (leyline.packing), and
the steps after the parse hold less. None of it may change what is mapped: a store made with the calls packed is
the store made without, for a full run and for an incremental one."""

import shutil
from pathlib import Path

import pytest

from leyline import packing
from leyline.indexer import index
from leyline.model import CallSite, FieldUse, FileResult, Node
from store_identity import differences

HERE = Path(__file__).parent


@pytest.mark.parametrize("name", ["fixture", "fixture2", "fixture3", "fixture4", "fixture5", "fixture6"])
def test_packed_calls_map_the_same(tmp_path, monkeypatch, name):
    root = tmp_path / name
    shutil.copytree(HERE / name, root)
    monkeypatch.setenv("LEYLINE_PACKED", "0")
    index(root, tmp_path / "plain.db", "r")
    monkeypatch.setenv("LEYLINE_PACKED", "1")
    index(root, tmp_path / "packed.db", "r")
    assert differences(tmp_path / "plain.db", tmp_path / "packed.db") == {}


def test_packed_incremental_run_matches_a_full_one(tmp_path, monkeypatch):
    monkeypatch.setenv("LEYLINE_PACKED", "1")
    root = tmp_path / "f4"
    shutil.copytree(HERE / "fixture4", root)
    inc = tmp_path / "inc.db"
    assert index(root, inc, "f4")["incremental"]["mode"] == "full"
    counter = root / "java/com/acme/Counter.java"
    for k, (old, new) in enumerate([("public void add(int n) { count += n; }", "public void add(int n) { count += n; get(); }"),
                                    ("public int get()", "public int twice() { return get() * 2; }\n    public int get()")]):
        counter.write_text(counter.read_text().replace(old, new, 1))
        assert index(root, inc, "f4")["incremental"]["mode"] == "incremental"
        full = tmp_path / f"full{k}.db"
        index(root, full, "f4", full=True)
        assert differences(inc, full) == {}


def test_pack_keeps_a_receiver_shared_and_its_keys_stable():
    inner = CallSite("m", "make", None, None, 0, 3)
    outer = CallSite("m", "run", "x", None, 1, 3, chain=inner, args=(CallSite("m", "arg", None, None, 0, 3), 2))
    use = FieldUse("m", "size", "x", None, "r", 4, chain=inner)
    res = FileResult(nodes=[Node("m", "callable", "m")], calls=[outer], field_uses=[use])
    blob = packing.pack(res, {"m": (1, 2, {})})
    assert res.calls == [outer] and res.field_uses == [use]   # the caller's result is left as it was
    got, heavy, decls = packing.unpack(blob)
    assert got.calls is None and got.field_uses is None and packing.unpack_decls(decls) == {"m": (1, 2, {})}
    keys = []
    for _ in range(2):
        got.calls, got.field_uses = packing.unpack_heavy(heavy)
        assert got.field_uses[0].chain is got.calls[0].chain   # one object, as the adapter made it
        k = packing.call_keys(got, 5)
        keys.append([k[id(c)] for c in (got.calls[0], got.calls[0].chain, got.calls[0].args[0])])
        assert all(v < 0 for v in k.values()) and len(set(k.values())) == len(k)
    assert keys[0] == keys[1]


def test_large_repositories_pack_by_default(monkeypatch):
    monkeypatch.delenv("LEYLINE_PACKED", raising=False)
    assert not packing.packs(10) and packing.packs(packing.PACKED_MIN_FILES)
    monkeypatch.setenv("LEYLINE_PACKED", "1")
    assert packing.packs(1)
