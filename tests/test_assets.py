"""Godot scenes and Unity prefabs as files on the map: linked to the scripts they run, the scenes they include and the
methods their connections and events name, with what cannot be tied to code counted by reason and not guessed."""

import json
import os
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

import pytest

from leyline import export, loop, query, store
from leyline.adapters import unity
from leyline.indexer import index, scan
from store_identity import differences

GUID = {"player": "a" * 32, "base": "b" * 32, "hero": "c" * 32, "ghost": "d" * 32}

BASE = """public class Base : UnityEngine.MonoBehaviour {
    public void Shared() {}
}
"""
PLAYER = """public class Player : Base {
    public int speed;
    public void OnHit() {}
    public void Jump() {}
}
"""
HERO = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &100
GameObject:
  m_Name: Hero
--- !u!114 &200
MonoBehaviour:
  m_GameObject: {fileID: 100}
  m_Script: {fileID: 11500000, guid: GUID_PLAYER, type: 3}
  onHit:
    m_PersistentCalls:
      m_Calls:
      - m_Target: {fileID: 200}
        m_TargetAssemblyTypeName: Player, Assembly-CSharp
        m_MethodName: OnHit
        m_Mode: 1
      - m_Target: {fileID: 200}
        m_TargetAssemblyTypeName: Player, Assembly-CSharp
        m_MethodName: Shared
        m_Mode: 1
      - m_Target: {fileID: 200}
        m_TargetAssemblyTypeName: Player, Assembly-CSharp
        m_MethodName: Gone
        m_Mode: 1
      - m_Target: {fileID: 300}
        m_TargetAssemblyTypeName: UnityEngine.GameObject, UnityEngine.CoreModule
        m_MethodName: SetActive
        m_Mode: 1
"""
MAIN = """%YAML 1.1
--- !u!1001 &900
PrefabInstance:
  m_SourcePrefab: {fileID: 100100000, guid: GUID_HERO, type: 3}
--- !u!114 &901
MonoBehaviour:
  m_Script: {fileID: 11500000, guid: GUID_GHOST, type: 3}
"""


def put(root: Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def meta(guid: str) -> str:
    return f"fileFormatVersion: 2\nguid: {guid}\n"


def unity_files(prefab_dir: str = "Assets/Prefabs") -> dict:
    fill = lambda t: t.replace("GUID_PLAYER", GUID["player"]).replace("GUID_HERO", GUID["hero"]).replace("GUID_GHOST", GUID["ghost"])
    return {"Assets/Scripts/Base.cs": BASE, "Assets/Scripts/Player.cs": PLAYER,
            "Assets/Scripts/Base.cs.meta": meta(GUID["base"]), "Assets/Scripts/Player.cs.meta": meta(GUID["player"]),
            f"{prefab_dir}/Hero.prefab": fill(HERO), f"{prefab_dir}/Hero.prefab.meta": meta(GUID["hero"]),
            "Assets/Scenes/Main.unity": fill(MAIN)}


PLAYER_GD = """extends Node2D

func _on_timer_timeout():
    pass

func jump():
    pass
"""
MAIN_TSCN = """[gd_scene load_steps=3 format=3 uid="uid://abc"]

[ext_resource type="Script" path="res://player.gd" id="1_abc"]
[ext_resource type="PackedScene" uid="uid://x" path="res://enemy.tscn" id="2_x"]
[ext_resource type="Script" path="res://missing.gd" id="3_y"]

[node name="Main" type="Node2D"]
script = ExtResource("1_abc")

[node name="Timer" type="Timer" parent="."]

[node name="Enemy" parent="." instance=ExtResource("2_x")]

[connection signal="timeout" from="Timer" to="." method="_on_timer_timeout"]
[connection signal="timeout" from="Timer" to="." method="nope"]
[connection signal="timeout" from="Timer" to="Timer" method="queue_free"]
"""
ENEMY_TSCN = """[gd_scene format=3]

[node name="Enemy" type="Node2D"]
"""


def godot_files(base: str = "game") -> dict:
    return {f"{base}/project.godot": "config_version=5\n", f"{base}/player.gd": PLAYER_GD,
            f"{base}/enemy.tscn": ENEMY_TSCN, f"{base}/scenes/main.tscn": MAIN_TSCN}


def mapped(tmp_path, files: dict, name: str = "repo"):
    root = tmp_path / name
    put(root, files)
    db = tmp_path / f"{name}.db"
    index(root, db, "r")
    return root, db


def edges(db, kind: str, src: str = "") -> list:
    con = store.connect(db)
    try:
        return [(r["src_id"].removeprefix("r:"), r["dst_id"].removeprefix("r:"), r["precision"], json.loads(r["attrs"] or "{}"))
                for r in con.execute("SELECT * FROM edges WHERE kind = ? AND src_id LIKE ? ORDER BY src_id, dst_id", (kind, f"%{src}%"))]
    finally:
        con.close()


def not_linked(db, extractor: str) -> dict:
    con = store.connect(db)
    try:
        row = con.execute("SELECT stats FROM extractor_coverage WHERE extractor = ?", (extractor,)).fetchone()
    finally:
        con.close()
    return json.loads(row[0]).get("not_linked", {}) if row else {}


needs_gd = pytest.mark.skipif(__import__("importlib").util.find_spec("tree_sitter_gdscript") is None, reason="no GDScript grammar")


@needs_gd
def test_a_godot_scene_is_linked_to_the_script_it_runs(tmp_path):
    root, db = mapped(tmp_path, godot_files())
    got = edges(db, "imports", "main.tscn")
    assert ("file:game/scenes/main.tscn", "file:game/player.gd", "exact", {"symbols": [], "found_by": "asset"}) in got


@needs_gd
def test_a_godot_project_in_a_subfolder_is_resolved_from_its_own_root(tmp_path):
    files = godot_files()
    files["player.gd"] = "extends Node\n"   # a decoy at the repository root: res:// is relative to the project, not to the repository
    root, db = mapped(tmp_path, files)
    to = [d for s, d, p, a in edges(db, "imports", "main.tscn")]
    assert "file:game/player.gd" in to and "file:player.gd" not in to


@needs_gd
def test_a_godot_signal_connection_is_linked_to_its_method(tmp_path):
    root, db = mapped(tmp_path, godot_files())
    got = edges(db, "communicates", "main.tscn")
    assert [(a["channel"], a["address"], p) for s, d, p, a in got] == [("asset", "_on_timer_timeout", "heuristic")]
    con = store.connect(db)
    try:
        method = con.execute("SELECT id FROM nodes WHERE name = '_on_timer_timeout'").fetchone()[0]
        direct = query.impact(con, method)["by_module"][0]["direct"]
    finally:
        con.close()
    assert [d for d in direct if d.endswith("main.tscn")]


@needs_gd
def test_a_godot_scene_that_includes_another_scene_is_linked_to_it(tmp_path):
    root, db = mapped(tmp_path, godot_files())
    assert "file:game/enemy.tscn" in [d for s, d, p, a in edges(db, "imports", "main.tscn")]


def test_a_godot_script_missing_its_grammar_is_said_so(tmp_path, monkeypatch):
    import leyline.indexer as ix
    monkeypatch.setattr(ix, "BY_EXTENSION", {k: v for k, v in ix.BY_EXTENSION.items() if k != ".gd"})   # the grammar is not installed
    root, db = mapped(tmp_path, godot_files())
    assert not [1 for s, d, p, a in edges(db, "imports", "main.tscn") if d.endswith("player.gd")]
    got = not_linked(db, "godot-scene")
    assert got["script in a language that is not read"]["count"] == 1
    assert "res://player.gd" in got["script in a language that is not read"]["first"][0]
    assert got["outside the repository"]["count"] == 1 and "missing.gd" in got["outside the repository"]["first"][0]


def test_a_unity_prefab_is_linked_to_the_script_a_component_runs(tmp_path):
    root, db = mapped(tmp_path, unity_files())
    assert ("file:Assets/Prefabs/Hero.prefab", "file:Assets/Scripts/Player.cs") in [(s, d) for s, d, p, a in edges(db, "imports", "Hero.prefab")]


def test_a_unity_event_is_linked_to_the_method_it_calls(tmp_path):
    root, db = mapped(tmp_path, unity_files())
    got = {d: (p, a) for s, d, p, a in edges(db, "communicates", "Hero.prefab")}
    assert got["csharp:Assets/Scripts::Player.OnHit()"] == ("heuristic", {"channel": "asset", "address": "OnHit"})


def test_a_method_of_a_base_class_counts_as_a_method_of_the_script(tmp_path):
    root, db = mapped(tmp_path, unity_files())
    assert "csharp:Assets/Scripts::Base.Shared()" in [d for s, d, p, a in edges(db, "communicates", "Hero.prefab")]


def test_a_unity_scene_that_includes_a_prefab_is_linked_to_it(tmp_path):
    root, db = mapped(tmp_path, unity_files())
    assert ("file:Assets/Scenes/Main.unity", "file:Assets/Prefabs/Hero.prefab") in [(s, d) for s, d, p, a in edges(db, "imports", "Main.unity")]


def test_impact_of_a_script_class_lists_the_prefab_that_runs_it(tmp_path):
    root, db = mapped(tmp_path, unity_files())
    con = store.connect(db)
    try:
        ids = {n: con.execute("SELECT id FROM nodes WHERE name = ?", (n,)).fetchone()[0] for n in ("Player", "speed", "Jump")}
        for name, nid in ids.items():
            got = query.impact(con, nid)
            direct = [d for g in got["by_module"] for d in g["direct"]]
            assert "r:file:Assets/Prefabs/Hero.prefab" in direct, name
            assert got["reached_by"] >= 2, name   # the scene that includes the prefab too
    finally:
        con.close()


def test_a_reference_to_a_script_outside_the_repository_is_counted(tmp_path):
    root, db = mapped(tmp_path, unity_files())
    got = not_linked(db, "unity-yaml")
    gone = got["outside the repository (no .meta file claims the GUID)"]
    assert gone["count"] == 1 and GUID["ghost"] in gone["first"][0] and "Main.unity" in gone["first"][0]
    assert got["method the script does not have"]["count"] == 1 and "Gone" in got["method the script does not have"]["first"][0]


def test_a_guid_that_two_files_claim_is_not_linked(tmp_path):
    files = unity_files()
    files["Assets/Scripts/Base.cs.meta"] = meta(GUID["player"])   # both scripts claim the one GUID
    root, db = mapped(tmp_path, files)
    assert not [1 for s, d, p, a in edges(db, "imports", "Hero.prefab") if d.endswith(".cs")]
    assert not edges(db, "communicates", "Hero.prefab")
    assert not_linked(db, "unity-yaml")["GUID claimed by two .meta files"]["count"] == 1


def test_a_call_on_a_built_in_component_is_not_counted_as_missing(tmp_path):
    root, db = mapped(tmp_path, unity_files())
    got = not_linked(db, "unity-yaml")
    assert got["call on a built-in component"]["count"] == 1 and "SetActive" in got["call on a built-in component"]["first"][0]
    assert not any("SetActive" in x for x in got["method the script does not have"]["first"])


def test_a_folder_of_assets_alone_is_not_a_module(tmp_path):
    root, db = mapped(tmp_path, unity_files(prefab_dir="Assets/Scripts/Prefabs"))
    con = store.connect(db)
    try:
        mods = {r["path"] for r in con.execute("SELECT path FROM nodes WHERE kind = 'module'")}
        parent = con.execute("SELECT parent_id FROM nodes WHERE id = 'r:file:Assets/Scripts/Prefabs/Hero.prefab'").fetchone()[0]
        scene = con.execute("SELECT parent_id FROM nodes WHERE id = 'r:file:Assets/Scenes/Main.unity'").fetchone()[0]
    finally:
        con.close()
    assert mods == {"", "Assets/Scripts"} or mods == {"Assets/Scripts"}
    assert parent == "r:module:Assets/Scripts"      # under the nearest folder above that holds code
    assert scene != "r:module:Assets/Scenes"        # nothing above holds code: the repository's own


def test_the_page_leaves_the_text_of_assets_out(tmp_path):
    root, db = mapped(tmp_path, unity_files())
    con = store.connect(db)
    try:
        page = export.graph(con)
    finally:
        con.close()
    assert "Assets/Scripts/Player.cs" in page["sources"]
    assert not [p for p in page["sources"] if p.endswith((".prefab", ".unity"))]
    assert any(n.get("f") == "Assets/Prefabs/Hero.prefab" for n in page["nodes"])


def test_a_large_asset_is_read_in_a_stream(tmp_path, monkeypatch):
    import leyline.indexer as ix
    body = ("--- !u!114 &{n}\nMonoBehaviour:\n  m_Script: {{fileID: 11500000, guid: %s, type: 3}}\n  m_Name: piece_{n}\n"
            "  m_Enabled: 1\n") % GUID["player"]
    big = "%YAML 1.1\n" + "".join(body.format(n=n) for n in range(1, 1000)) * 150     # about 20 MB with the repeat
    data = big.encode()
    assert len(data) > 5 * 2**20
    tracemalloc.start()
    res = unity.parse("r", "Assets/Big.unity", "r:file:Assets/Big.unity", data)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert [(r.what, r.target) for r in res.assets] == [("script", GUID["player"])]
    assert peak < len(data) // 20                                                      # no list of its lines was made
    called = []
    real = ix.text_lines
    monkeypatch.setattr(ix, "text_lines", lambda d: (called.append(len(d)), real(d))[1])
    files = unity_files()
    root = tmp_path / "repo"
    put(root, files)
    (root / "Assets/Scenes/Big.unity").write_bytes(data)
    db = tmp_path / "s.db"
    index(root, db, "r")
    assert max(called) < 1000                                                          # the code is, the scene is not decoded as text
    assert ("file:Assets/Scenes/Big.unity", "file:Assets/Scripts/Player.cs") in [(s, d) for s, d, p, a in edges(db, "imports", "Big.unity")]


def test_a_binary_asset_is_left_out_and_named(tmp_path):
    root = tmp_path / "repo"
    put(root, unity_files())
    (root / "Assets/Scenes/Baked.asset").write_bytes(b"UnityFS\x00\x00\x00\x06" + bytes(range(256)) * 4)
    skipped = dict(scan(root).skipped)
    assert skipped["Assets/Scenes/Baked.asset"] == "binary"
    db = tmp_path / "s.db"
    index(root, db, "r")
    con = store.connect(db)
    try:
        assert con.execute("SELECT 1 FROM nodes WHERE path = 'Assets/Scenes/Baked.asset'").fetchone() is None
    finally:
        con.close()


def test_a_changed_meta_file_links_the_assets_again(tmp_path):
    root = tmp_path / "repo"
    put(root, unity_files())
    db = tmp_path / "inc.db"
    index(root, db, "r")
    assert loop.changed_files(db) == []
    (root / "Assets/Scripts/Player.cs.meta").write_text(meta("e" * 32))   # the script's GUID changed
    assert loop.changed_files(db) == ["Assets/Scripts/Player.cs"]
    stats = index(root, db, "r")
    assert stats["incremental"]["mode"] == "incremental"
    assert not [1 for s, d, p, a in edges(db, "imports", "Hero.prefab") if d.endswith("Player.cs")]
    full = tmp_path / "full.db"
    index(root, full, "r", full=True)
    assert differences(db, full) == {}
    (root / "Assets/Scripts/Player.cs.meta").write_text(meta(GUID["player"]))   # and back
    index(root, db, "r")
    assert [1 for s, d, p, a in edges(db, "imports", "Hero.prefab") if d.endswith("Player.cs")]
    index(root, full, "r", full=True)
    assert differences(db, full) == {}


def test_an_unchanged_asset_is_not_read_again(tmp_path, monkeypatch):
    import leyline.indexer as ix
    root = tmp_path / "repo"
    put(root, unity_files())
    old = time.time() - 3600
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (old, old))   # long settled: a file edited as it was read could not hide in the same tick
    db = tmp_path / "inc.db"
    index(root, db, "r")
    (root / "Assets/Scripts/Base.cs").write_text(BASE.replace("Shared", "Common"))
    opened, parsed = [], []
    real_read, real_parse = ix.read_source, unity.parse
    monkeypatch.setattr(ix, "read_source", lambda p: (opened.append(Path(p).name), real_read(p))[1])
    monkeypatch.setattr(unity, "parse", lambda *a, **k: (parsed.append(a[1]), real_parse(*a, **k))[1])
    stats = index(root, db, "r")
    assert stats["incremental"]["mode"] == "incremental"
    assert "Base.cs" in opened and not [n for n in opened if n.endswith((".prefab", ".unity"))]
    assert not parsed
    full = tmp_path / "full.db"
    index(root, full, "r", full=True)
    assert differences(db, full) == {}
    assert not [1 for s, d, p, a in edges(db, "communicates", "Hero.prefab") if d.endswith("Base.Shared()")]   # renamed: no edge


def test_both_engines_map_in_one_repository(tmp_path):
    files = {**unity_files(), **{f"godot/{k}": v for k, v in godot_files("proj").items()}}
    root, db = mapped(tmp_path, files)
    assert not_linked(db, "unity-yaml") and edges(db, "imports", "Hero.prefab")
    script = (
        "import sys; from leyline.indexer import index; from leyline import store\n"
        "index(sys.argv[1], sys.argv[2], 'r')\n"
        "con = store.connect(sys.argv[2])\n"
        "print(sorted(r[0] for r in con.execute(\"SELECT DISTINCT language FROM nodes WHERE kind = 'file'\")))\n"
        "print(con.execute(\"SELECT COUNT(*) FROM edges WHERE kind = 'imports' AND src_id LIKE '%Hero.prefab'\").fetchone()[0])")
    out = subprocess.run([sys.executable, "-c", script, str(root), str(tmp_path / "g.db")], capture_output=True, text=True,
                         env={**os.environ, "LEYLINE_GENERIC": "1"})
    assert out.returncode == 0, out.stderr[-800:]
    langs, imports = out.stdout.strip().splitlines()[-2:]
    assert "godot-scene" in langs and "unity-yaml" in langs and int(imports) >= 1


def test_a_repository_without_assets_maps_as_before(tmp_path, monkeypatch):
    import leyline.indexer as ix
    monkeypatch.setattr(ix, "read_meta", lambda p: pytest.fail("a .meta file was read"))
    root, db = mapped(tmp_path, {"pkg/a.py": "def f():\n    return 1\n", "pkg/a.py.meta": "guid: " + "f" * 32 + "\n"})
    con = store.connect(db)
    try:
        names = {r[0] for r in con.execute("SELECT extractor FROM extractor_coverage")}
        key = con.execute("SELECT 1 FROM meta WHERE key LIKE 'asset_meta:%'").fetchone()
    finally:
        con.close()
    assert not names & {"godot-scene", "unity-yaml"} and key is None
