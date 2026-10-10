"""Learnings in a workspace: each repository keeps its own, and a learning applies only to code in its repository,
though node ids in the file leave the repository's id out (so two repositories can share one)."""

import json

from leyline import learnings, loop, spec, store

CLAIM = "count still calls load with one argument, so it reads the file without an encoding"
USE = "from app.store import load\n\n\ndef count(p):\n    return len(load(p))\n"
STORE = "def load(path):\n    with open(path) as f:\n        return f.read()\n"


def workspace(tmp_path):
    roots = []
    for name in ("one", "two"):
        root = tmp_path / name
        for f, text in {"app/use.py": USE, "app/store.py": STORE}.items():
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text(text)
        roots.append(root)
    db = tmp_path / "ws" / "leyline.db"
    loop.map_repos([str(r) for r in roots], db, exact="off", page=False)
    con = store.connect(db)
    with con:
        con.execute("INSERT INTO change_proposals (id, intent, status, attrs) VALUES ('pr-x', 'x', 'pr', '{}')")
    return con, roots


def test_a_learning_applies_only_in_its_own_repository(tmp_path):
    con, (one, two) = workspace(tmp_path)
    counts = [r[0] for r in con.execute("SELECT id FROM nodes WHERE name = 'count' ORDER BY id")]
    in_one = next(i for i in counts if con.execute("SELECT repo_id FROM nodes WHERE id = ?", (i,)).fetchone()[0]
                  == next(r for r, p in store.roots(con).items() if p == one.resolve()))
    in_two = next(i for i in counts if i != in_one)
    f = spec.add_finding(con, "pr-x", "logic", "high", CLAIM, [in_one])
    r = spec.resolve_finding(con, f["id"], "rejected", "load keeps a default encoding on purpose.")
    assert r["learning"].startswith("l-") and (one / ".leyline-learnings.json").is_file()
    assert not (two / ".leyline-learnings.json").exists()

    # the same claim on the other repository's `count`: a different function, which the decision is not about
    other = spec.add_finding(con, "pr-x", "logic", "high", CLAIM + ".", [in_two])
    assert "learned" not in other
    assert learnings.applying(con, "pr-x", [in_two]) == []
    # on its own repository's code it still applies and still marks a repeat
    assert [x["id"] for x in learnings.applying(con, "pr-x", [in_one])] == [r["learning"]]
    again = spec.add_finding(con, "pr-x", "logic", "medium", CLAIM + " at all", [in_one])
    assert again["learned"]["id"] == r["learning"]
    [l] = json.loads((one / ".leyline-learnings.json").read_text())["learnings"]
    assert list(l["findings"]) == [again["id"]]
