"""Spec drift: code a spec names that has since moved, changed or gone.

OpenSpec keeps living specs in openspec/specs/<capability>/spec.md and archives finished changes under
openspec/changes/archive/<date>-<id>/. Both name code in backticks, and nothing tells a reader when that code
is renamed, moved, given new parameters or deleted. This module keeps a record of what each name meant when it
was last known to be right, and compares the map with it.

An anchor is that record for one name: the name as written, the node it resolved to (id, kind, path), and two
fingerprints: of its declaration (a function's signature line, a type's declaration line and member names, a
field's declared type) and of its body (the node's content hash). `check` records one for every code name of a
change it finds done as agreed; `leyline drift --accept` records them for the living specs, after a person says
the specs and the code agree.

Anchors are kept twice: in the store (table `spec_anchors`), and in `openspec/leyline-anchors.json` beside the
specs, to be committed with them, so they outlive a fresh map and travel with the repository. That file is
canonical JSON (sorted keys, two-space indent, a newline at the end), keyed by the change id, or by
`specs/<capability>` for a living spec, and its node ids are written without the repository's id (the name of
the directory it was mapped from), so another clone reads them the same. The file wins over the store.

Drift is reported per name:
    gone        nothing on the map answers to the name or its anchor any more
    renamed     gone, but one new node beside where it was has its body or declaration under another name, or git
                records its file as renamed (leyline.renames)
    moved       found, but in another file or under another owner
    signature   its declaration differs from the anchor (or a type lost members)
    ambiguous   the name now resolves to several things
    body        changed inside, same declaration: worth a read, not drift by itself
A name with no anchor is checked by name only: gone when its owner or file is there without it, unclear when it
could be several things (it never named one, so that is not drift). Without an anchor, a changed signature
cannot be seen. Only gone, renamed and signature make `leyline drift` exit 1.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from . import spec, store

ANCHOR_FILE = "leyline-anchors.json"
DATED = re.compile(r"^\d{4}-\d{2}-\d{2}-")
ORDER = ("gone", "renamed", "signature", "moved", "ambiguous", "unclear", "body", "ok")
DRIFTED = ("gone", "renamed", "signature", "moved", "ambiguous")   # what a plan warns about
FAILS = ("gone", "renamed", "signature")                          # what makes `leyline drift` exit 1
SHOWN = DRIFTED + ("unclear",)                         # a name with no anchor that could be several things


# -- where the specs are --------------------------------------------------------------------------
def openspec_of(path: str | Path) -> Optional[Path]:
    """The openspec/ folder a path is in, or the one at or above it."""
    p = Path(path).resolve()
    for d in [p, *p.parents]:
        if d.name == "openspec" and d.is_dir():
            return d
        if (d / "openspec").is_dir():
            return d / "openspec"
    return None


def places(con, path: Optional[str | Path] = None) -> list[Path]:
    """Every openspec/ folder to check: the one at or above `path`, and one in each mapped repository."""
    found = [openspec_of(path)] if path is not None else []
    found += [root / "openspec" for root in store.roots(con).values() if (root / "openspec").is_dir()]
    return list(dict.fromkeys(p.resolve() for p in found if p is not None))


def _code_names(text: str) -> list[str]:
    """Backticked names in Markdown, leaving out fenced code and REMOVED sections (those name what is meant to go)."""
    out, fenced, removed = [], False, False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        if line.startswith("## "):
            removed = line[3:].strip().upper().startswith("REMOVED")
        if not removed:
            out += [w.strip() for w in spec.CODE.findall(line)]
    return list(dict.fromkeys(w for w in out if w))


def _change_names(folder: Path) -> dict[str, list[str]]:
    """written name -> where in a change folder it is named: tasks.md, or specs/<capability>/spec.md. A task that
    removes or renames code names something meant to be gone, so the code right after its verb is left out."""
    out: dict[str, list[str]] = defaultdict(list)
    parsed = spec.parse(folder)
    for t in parsed.get("tasks", []):
        for w, role in spec._roles(t["text"]):
            if not (role == "lead" and t["action"] in ("remove", "rename")) and "tasks.md" not in out[w.strip()]:
                out[w.strip()].append("tasks.md")
    specs = folder / "specs"
    for f in sorted(specs.rglob("spec.md")) if specs.is_dir() else []:
        rel = f.relative_to(folder).as_posix()
        for w in _code_names(f.read_text(encoding="utf-8", errors="replace")):
            if rel not in out[w]:
                out[w].append(rel)
    return dict(out)


# -- fingerprints -----------------------------------------------------------------------------------
def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _local(node_id: str, repo: Optional[str]) -> str:
    """A node id without its repository's id, which is only the name of the directory it was mapped from."""
    if repo and node_id.startswith(repo + ":"):
        return node_id[len(repo) + 1:]
    return node_id.split(":", 1)[1] if ":" in node_id else node_id


def _row(con, node_id: str):
    return con.execute("SELECT id, kind, name, parent_id, repo_id, path, content_hash, attrs, span_start, span_end"
                       " FROM nodes WHERE id = ?", (node_id,)).fetchone()


def _shape(con, r) -> Optional[str]:
    """A hash of a node's text with its name taken out where it is declared (lines stripped, as the content hash
    strips them), so a function renamed and otherwise untouched hashes the same. None for a file or module, or
    text not on disk."""
    if r["kind"] in ("file", "module", "repo") or not r["span_start"] or not r["path"]:
        return None
    root = store.roots(con).get(r["repo_id"])
    if root is None:
        return None
    f = root / r["path"]
    try:
        st = f.stat()
        key = (str(f), st.st_mtime_ns, st.st_size)
        if _TEXT.get(str(f), (None,))[0] != key:
            if len(_TEXT) > 4000:
                _TEXT.clear()
            _TEXT[str(f)] = (key, f.read_bytes().decode("utf-8", errors="replace").split("\n"))
    except OSError:
        return None
    # Only where it is declared: the same word elsewhere (`JSON.parse` in `parse`) is other code, and stays.
    word = re.compile(r"(?<![\w$])" + re.escape(r["name"]) + r"(?![\w$])")
    lines = _TEXT[str(f)][1][r["span_start"] - 1:r["span_end"] or r["span_start"]]
    return _h(word.sub("\0", "\n".join(ln.strip() for ln in lines), count=1))


_TEXT: dict = {}   # path -> ((path, mtime, size), lines): a file read once while it stays the same


def _declaration(con, r) -> tuple[Optional[str], Optional[list[str]]]:
    """What a node promises to the code that uses it: its declaration line, and for a type its member names."""
    if r["kind"] in ("file", "module", "repo"):
        return None, None
    a = json.loads(r["attrs"] or "{}")
    text = a.get("signature")
    if not text and r["kind"] in ("callable", "test"):
        text = json.dumps([a.get("params"), a.get("param_types"), a.get("returns")])
    if not text and r["kind"] == "field":
        text = a.get("declared_type") or ""
    members = None
    if r["kind"] == "type":
        members = sorted({m[0] for m in con.execute(
            "SELECT name FROM nodes WHERE parent_id = ? AND kind IN ('callable', 'field')", (r["id"],))})
    return " ".join((text or r["name"]).split()), members


def fingerprint(con, names, node_id: str) -> Optional[dict]:
    r = _row(con, node_id)
    if r is None:
        return None
    decl, members = _declaration(con, r)
    out = {"id": node_id, "node": _local(node_id, r["repo_id"]), "kind": r["kind"], "name": r["name"],
           "label": spec._label(names, node_id), "path": r["path"], "decl": decl,
           "decl_hash": _h(decl) if decl is not None else None, "body_hash": r["content_hash"]}
    if members is not None:
        out["members"] = members
    shape = _shape(con, r)   # the body without the name: a rename keeps it (see renamed())
    if shape is not None:
        out["shape_hash"] = shape
    return out


def _anchor(written: str, sources: list[str], fp: dict) -> dict:
    return {"written": written, "from": sorted(sources), **{k: v for k, v in fp.items() if k != "id"}}


def _anchors_for(con, names, written_from: dict[str, list[str]]) -> list[dict]:
    """An anchor for each name that resolves to code on the map. A name that could be several things, or is not
    code, gets none."""
    out = []
    for w, sources in sorted(written_from.items()):
        r = names.resolve(w)
        for i in r.get("ids", []):
            fp = fingerprint(con, names, i)
            if fp is not None:
                out.append(_anchor(w, sources, fp))
    out.sort(key=lambda a: (a["written"], a["node"]))
    return out


# -- keeping anchors ---------------------------------------------------------------------------------
def _today() -> str:
    return datetime.date.today().isoformat()


def read_file(openspec: Optional[Path]) -> tuple[dict, Optional[str]]:
    """key -> {"recorded", "anchors"} from openspec/leyline-anchors.json, and a problem reading it, if any."""
    f = openspec / ANCHOR_FILE if openspec else None
    if f is None or not f.is_file():
        return {}, None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return dict(data.get("anchors") or {}), None
    except (ValueError, AttributeError) as e:
        return {}, f"{f} could not be read ({e}); its anchors were left out"


def _write_file(openspec: Path, anchors: dict) -> Path:
    f = openspec / ANCHOR_FILE
    data = {"about": "Written by Leyline (`leyline check`, `leyline drift --accept`): what each code name in these specs"
                     " meant when it was last known to be right. `leyline drift` compares the code with it. Commit it"
                     " with the specs.",
            "anchors": anchors, "version": 1}
    text = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if not f.is_file() or f.read_text(encoding="utf-8", errors="replace") != text:
        f.write_text(text, encoding="utf-8")
    return f


def _place(openspec: Optional[Path]) -> str:
    """Which openspec/ folder a row in the store belongs to: a workspace of several repositories has one each."""
    return str(openspec.resolve()) if openspec else ""


def _store(con, place: str, key: str, entry: dict) -> None:
    roots = store.roots(con)
    rows = []
    for a in entry["anchors"]:
        full = next((f"{r}:{a['node']}" for r in roots if _row(con, f"{r}:{a['node']}") is not None), a["node"])
        rest = {k: v for k, v in a.items() if k not in ("written", "kind", "path", "decl", "decl_hash", "body_hash")}
        rows.append((place, key, a["written"], full, a["kind"], a["path"], a["decl"], a["decl_hash"], a["body_hash"],
                     json.dumps(rest, sort_keys=True), entry["recorded"]))
    with con:
        con.execute("DELETE FROM spec_anchors WHERE place = ? AND change_id = ?", (place, key))
        con.executemany("INSERT OR REPLACE INTO spec_anchors VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)


def _from_store(con, place: str) -> dict:
    out: dict = {}
    for r in con.execute("SELECT * FROM spec_anchors WHERE place = ? ORDER BY change_id, written, node_id", (place,)):
        e = out.setdefault(r["change_id"], {"recorded": r["recorded"], "anchors": []})
        e["anchors"].append({"written": r["written"], "kind": r["kind"], "path": r["path"], "decl": r["decl"],
                             "decl_hash": r["decl_hash"], "body_hash": r["body_hash"], **json.loads(r["attrs"] or "{}")})
    return out


def _put(con, openspec: Optional[Path], key: str, anchors: list[dict], write_file: bool = True) -> dict:
    """Keep a key's anchors in the store and, when there is an openspec/ folder, in its file. The date stays when
    nothing changed, so checking again does not touch a committed file. A file that cannot be read is left as it
    is: writing it would lose what it held."""
    held, problem = read_file(openspec)
    old = held.get(key) or _from_store(con, _place(openspec)).get(key)
    entry = {"recorded": old["recorded"] if old and old.get("anchors") == anchors else _today(), "anchors": anchors}
    _store(con, _place(openspec), key, entry)
    out = {"key": key, "count": len(anchors), **({"problem": problem} if problem else {})}
    if openspec is not None and write_file and not problem:
        held[key] = entry
        if not anchors:
            held.pop(key)
        out["file"] = str(_write_file(openspec, held))
    return out


def load(con, openspec: Optional[Path]) -> tuple[dict, list[str]]:
    """Every anchor for one openspec/ folder: the file's, then the store's for keys the file does not have."""
    held, problem = read_file(openspec)
    for key, e in _from_store(con, _place(openspec)).items():
        held.setdefault(key, e)
    return held, [problem] if problem else []


def record(con, change_dir: str | Path, write_file: bool = True) -> dict:
    """After `check` finds a change done as agreed: anchor every code name its tasks and spec deltas resolve to."""
    folder = Path(change_dir)
    names = spec._Names(con)
    anchors = _anchors_for(con, names, _change_names(folder))
    return _put(con, openspec_of(folder), DATED.sub("", folder.name), anchors, write_file)


# -- comparing -----------------------------------------------------------------------------------------
class _Map:
    """The map as drift reads it: names, rows by id, and the repositories ids are made from."""

    def __init__(self, con):
        self.con = con
        self.names = spec._Names(con)
        self.repos = list(store.roots(con)) or sorted({r["id"].split(":", 1)[0] for r in self.names.rows})
        self.claimed: set = set()   # nodes some anchor still finds: not what another name was renamed to
        self._git = None

    def git(self):
        """What git says about the mapped repositories (leyline.renames.Git), or None when none is a git repository."""
        if self._git is None:
            from .renames import Git
            self._git = Git(list(store.roots(self.con).values()))
        return self._git if self._git.repos else None

    def find(self, a: dict) -> Optional[str]:
        """The node an anchor was made from, if it is still on the map under the same id."""
        for i in [a.get("id")] + [f"{r}:{a['node']}" for r in self.repos]:
            if i and i in self.names.by_id:
                return i
        return None

    def label(self, i: str) -> str:
        return spec._label(self.names, i)

    def labels(self, ids: list[str]) -> list[str]:
        """Names for several things one name could be, with the file when two would read the same."""
        plain = [self.label(i) for i in ids]
        return [f"{x} in {self.names.by_id[i]['path']}" if plain.count(x) > 1 and i in self.names.by_id else x
                for i, x in zip(ids, plain)]


def _same_thing(m: _Map, a: dict, taken: set) -> Optional[str]:
    """Where an anchored node went when its id is gone: what the written name resolves to now, when it is the same
    owner and name (its id changed with its parameters, or its file moved) or keeps its declaration or body; or
    else the one node of the same name and kind with the same body or declaration."""
    r = m.names.resolve(a["written"])
    cands = [i for i in r.get("ids", []) if i not in taken]
    fps = {i: fingerprint(m.con, m.names, i) for i in cands}
    same_kind = [i for i in cands if fps[i] and fps[i]["kind"] == a["kind"]]
    for test in ("label", "decl_hash", "body_hash"):
        hit = [i for i in same_kind if fps[i][test] == a.get(test)]
        if hit:
            return hit[0]
    if "ambiguous" in r:
        return None
    hits = []
    for row in m.names.by_name.get(a.get("name") or a["written"].split(".")[-1], []):
        if row["kind"] != a["kind"] or row["id"] in taken:
            continue
        fp = fingerprint(m.con, m.names, row["id"])
        if fp and (fp["body_hash"] == a.get("body_hash") or fp["decl_hash"] == a.get("decl_hash")):
            hits.append(row["id"])
    return hits[0] if len(hits) == 1 else None


def judge(m: _Map, written: str, anchors: list[dict], key: Optional[str] = None) -> dict:
    """One name against its anchors (those of `key`, a change or specs/<capability>): what became of it."""
    item = {"written": written, "anchored": True, "was": [a["label"] for a in anchors], "ids": [],
            "was_ids": [f"{r}:{a['node']}" for a in anchors for r in m.repos]}
    found = {id(a): m.find(a) for a in anchors}
    taken = {i for i in found.values() if i}
    gone = 0
    for a in anchors:
        i = found[id(a)] or _same_thing(m, a, taken)
        if i is None:
            gone += 1
            continue
        taken.add(i)
        item["ids"].append(i)
        fp = fingerprint(m.con, m.names, i)
        if fp["path"] != a["path"] or fp["label"] != a["label"]:
            item.setdefault("moved", {"to": fp["label"], "to_path": fp["path"], "was": a["label"], "was_path": a["path"]})
        lost = sorted(set(a.get("members") or []) - set(fp.get("members") or [])) if a.get("members") is not None else []
        if a.get("decl_hash") and fp["decl_hash"] and (fp["decl_hash"] != a["decl_hash"] or lost):
            item.setdefault("signature", {"was": a["decl"], "now": fp["decl"], "lost": lost})
        elif fp["body_hash"] != a.get("body_hash"):
            item["body"] = True
    if gone and gone == len(anchors):
        from . import renames   # gone under this name, but likely there under another
        to = renames.find(m, anchors[0], key) if len(anchors) == 1 or len({a["label"] for a in anchors}) == 1 else None
        if to is not None:
            item["renamed"] = {**to, "was": anchors[0]["label"], "was_path": anchors[0]["path"]}
            item["ids"] += [to["id"]] if to.get("id") else []
        else:
            item["gone"] = {"was": anchors[0]["label"], "was_path": anchors[0]["path"]}
    elif gone and "signature" not in item:   # one of several overloads went
        item["signature"] = {"was": f"{len(anchors)} overloads", "now": f"{len(anchors) - gone}", "lost": []}
    r = m.names.resolve(written)
    if "ambiguous" in r and "gone" not in item:
        item["ambiguous"] = m.labels(r["ambiguous"])
    item["state"] = next(s for s in ORDER if s == "ok" or item.get(s))
    return item


def _owners(m: _Map, written: str) -> list[str]:
    """For `Owner.member`, the types called Owner (when there are several, the name alone cannot pick one)."""
    parts = written.split(".")
    if len(parts) != 2 or not all(spec.IDENT.fullmatch(x) for x in parts):
        return []
    return [r["id"] for r in m.names.by_name.get(parts[0], []) if r["kind"] == "type"]


def judge_name(m: _Map, written: str) -> Optional[dict]:
    """A name with no anchor, by name alone. None when it is a word, not code on the map."""
    r = m.names.resolve(written)
    item = {"written": written, "anchored": False, "ids": r.get("ids", [])}
    if "ids" in r:
        item["state"] = "ok"
    elif "ambiguous" in r:   # never pinned down, so not drift: the spec should say which
        item.update(state="unclear", ambiguous=m.labels(r["ambiguous"]))
    elif "new" in r and r.get("parent") in m.names.by_id:
        owner = m.names.by_id[r["parent"]]
        item.update(state="gone", gone={"owner": owner["path"] if owner["kind"] == "file" else owner["name"],
                                        "leaf": r["new"]}, ids=[r["parent"]])
    elif "new" in r and _owners(m, written):   # `Journal.forget` where every type called Journal lacks `forget`
        owners = _owners(m, written)
        item.update(state="gone", gone={"owner": m.names.by_id[owners[0]]["name"], "leaf": written.rsplit(".", 1)[1]},
                    ids=owners)
    elif "new" in r and r.get("file") and "/" in written:
        from . import renames   # a file git records as renamed
        to = renames.for_file(m, written)
        if to:
            item.update(state="renamed", renamed={**to, "was": written, "was_path": written})
        else:
            item.update(state="gone", gone={"file": True})
    else:
        return None
    return item


def _pick(index: dict, written: str, cap: Optional[str]) -> Optional[tuple[str, list[dict]]]:
    """The anchors a living spec's name is read against: its own (`specs/<capability>`), else those of the change
    that wrote this spec's delta, else the latest change that named it."""
    held = index.get(written) or {}
    if not held:
        return None
    if cap and f"specs/{cap}" in held:
        return f"specs/{cap}", held[f"specs/{cap}"][1]
    mine = [k for k, (_, xs) in held.items() if cap and any(f"specs/{cap}/spec.md" in x.get("from", []) for x in xs)]
    keys = mine or [k for k in held if not k.startswith("specs/")]
    if not keys:
        return None
    key = max(keys, key=lambda k: (held[k][0] or "", k))
    return key, held[key][1]


def _index(anchors: dict) -> dict:
    """written -> key -> (recorded, anchors). A spec may write a name another way than the change did
    (`make_engine` for `core.make_engine`), so an anchor's label and bare name find it too."""
    index: dict = {}
    for how in ("written", "label", "name"):
        by: dict = defaultdict(dict)
        for key, e in anchors.items():
            for a in e.get("anchors") or []:
                if a.get(how):
                    by[a[how]].setdefault(key, (e.get("recorded"), []))[1].append(a)
        for k, v in by.items():
            index.setdefault(k, v)
    return index


def report(con, dirs: list[Path]) -> dict:
    """Every living spec, archived change and anchored change in these openspec/ folders, name by name."""
    m = _Map(con)
    groups, problems, used = [], [], set()
    m.claimed = {i for osd in dirs for e in load(con, osd)[0].values() for a in e.get("anchors") or [] for i in [m.find(a)] if i}
    for osd in dirs:
        anchors, trouble = load(con, osd)
        problems += trouble
        index = _index(anchors)
        root = osd.parent
        rel = lambda p: Path(p).resolve().relative_to(root.resolve()).as_posix()
        for f in sorted((osd / "specs").rglob("spec.md")) if (osd / "specs").is_dir() else []:
            cap = f.parent.relative_to(osd / "specs").as_posix()
            items = []
            for w in _code_names(f.read_text(encoding="utf-8", errors="replace")):
                hit = _pick(index, w, cap)
                if hit:   # reported here, so not again under the change that anchored it
                    used |= {(hit[0], a["written"]) for a in hit[1]} | {a["node"] for a in hit[1]}
                it = judge(m, w, hit[1], hit[0]) if hit else judge_name(m, w)
                if it is not None and hit:
                    it["anchor_key"] = hit[0]
                items.append(it)
            groups.append({"kind": "spec", "source": rel(f), "capability": cap, "items": items})
        archived = {}
        arch = osd / "changes" / "archive"
        for d in sorted(arch.iterdir()) if arch.is_dir() else []:
            if d.is_dir():
                archived[DATED.sub("", d.name)] = d
        for key in sorted(set(archived) | {k for k in anchors if not k.startswith("specs/")}):
            mine = {a["written"]: [] for a in anchors.get(key, {}).get("anchors") or []}
            for a in anchors.get(key, {}).get("anchors") or []:
                mine[a["written"]].append(a)
            items = []
            for w, xs in sorted(mine.items()):
                if (key, w) not in used and not all(a["node"] in used for a in xs):
                    items.append({**judge(m, w, xs, key), "anchor_key": key})
            if key in archived:
                for w in _change_names(archived[key]):
                    if w not in mine:
                        items.append(judge_name(m, w))
            groups.append({"kind": "change", "source": key, "change": key,
                           **({"archived_as": rel(archived[key])} if key in archived else {}), "items": items})
    for g in groups:
        g["words"] = sum(1 for it in g["items"] if it is None)
        g["items"] = [it for it in g["items"] if it is not None]
    counts = {s: sum(1 for g in groups for it in g["items"] if it["state"] == s) for s in ORDER}
    counts["words"] = sum(g["words"] for g in groups)
    counts["unanchored"] = sum(1 for g in groups for it in g["items"] if it["state"] == "ok" and not it["anchored"])
    return {"places": [str(p) for p in dirs], "groups": groups, "counts": counts, "problems": problems,
            "specs": sum(g["kind"] == "spec" for g in groups), "changes": sum(g["kind"] == "change" for g in groups),
            "names": sum(len(g["items"]) for g in groups), "fails": any(counts[s] for s in FAILS)}


def accept(con, dirs: list[Path]) -> dict:
    """The person says the specs and the code agree as they are now: anchor each living spec's names afresh, and
    bring each change's anchors up to the code they found. Accepting cannot make code that is gone agree, so an
    anchor whose code is gone is kept as it was, and goes on being reported until the spec stops naming it."""
    m = _Map(con)
    done = {}
    for osd in dirs:
        anchors, _ = load(con, osd)
        index = _index(anchors)
        for key, e in sorted(anchors.items()):
            if key.startswith("specs/"):
                continue
            fresh = []
            for w in sorted({a["written"] for a in e.get("anchors") or []}):
                xs = [a for a in e["anchors"] if a["written"] == w]
                taken: set = set()
                for a in xs:
                    i = m.find(a) or _same_thing(m, a, taken)
                    if i and i not in taken:
                        taken.add(i)
                        fresh.append(_anchor(w, a.get("from") or [], fingerprint(con, m.names, i)))
                    elif i is None:
                        fresh.append(a)
            fresh.sort(key=lambda a: (a["written"], a["node"]))
            done[key] = _put(con, osd, key, fresh)["count"]
        for f in sorted((osd / "specs").rglob("spec.md")) if (osd / "specs").is_dir() else []:
            cap = f.parent.relative_to(osd / "specs").as_posix()
            src = f"specs/{cap}/spec.md"
            written = _code_names(f.read_text(encoding="utf-8", errors="replace"))
            fresh = _anchors_for(con, m.names, {w: [src] for w in written})
            have = {a["written"] for a in fresh}
            for w in written:   # gone now: keep what it meant, so it is still reported
                hit = None if w in have or "ambiguous" in m.names.resolve(w) else _pick(index, w, cap)
                fresh += [{**a, "written": w, "from": [src]} for a in hit[1] if not m.find(a)] if hit else []
            fresh.sort(key=lambda a: (a["written"], a["node"]))
            done[f"specs/{cap}"] = _put(con, osd, f"specs/{cap}", fresh)["count"]
    return done


def run(db: str | Path, path: Optional[str | Path] = None, do_accept: bool = False) -> dict:
    """`leyline drift`: bring the map up to date, then compare every spec's code names with it."""
    from . import loop
    reindexed = loop.refresh(db)
    con = store.connect(db)
    try:
        dirs = places(con, path if path is not None else Path.cwd())
        accepted = accept(con, dirs) if do_accept else None
        out = report(con, dirs)
    finally:
        con.close()
    out["reindexed"] = bool(reindexed)
    if accepted is not None:
        out["accepted"] = accepted
    return out


# -- saying it --------------------------------------------------------------------------------------
def _where(path: Optional[str]) -> str:
    return f" in {path}" if path else ""


def line(it: dict) -> str:
    """One name, as a sentence."""
    w = f"`{it['written']}`"
    if it.get("gone"):
        g = it["gone"]
        if g.get("file"):
            return f"{w} is gone: there is no such file now."
        if "owner" in g:
            return f"{w} is gone: {g['owner']} has no `{g['leaf']}` now."
        return f"{w} is gone: nothing on the map answers to it now (it was {g['was']}{_where(g['was_path'])})."
    if it.get("renamed"):
        rn = it["renamed"]
        if rn["how"].startswith("git"):
            return f"{w} has been renamed to `{rn['to']}`: {rn['how']}."
        return (f"{w} has been renamed to `{rn['to']}`{_where(rn['to_path'])}: nothing answers to the old name now, and"
                f" `{rn['to']}` {rn['how']}.")
    bits = []
    if it.get("signature"):
        s = it["signature"]
        if s["lost"]:
            bits.append(f"{w} has lost {spec._and([f'`{x}`' for x in s['lost']])} since the spec was written")
        if s["was"] != s["now"]:
            bits.append(f"{w} has changed signature since the spec was written: was `{s['was']}`, now `{s['now']}`")
    if it.get("moved"):
        mv = it["moved"]
        same = mv["to"] == mv["was"]
        where = f"it is now in {mv['to_path']}" if same else f"it is now {mv['to']}{_where(mv['to_path'])}"
        was = f"was in {mv['was_path']}" if same else f"was {mv['was']}{_where(mv['was_path'])}"
        bits.append(f"{w} has moved: {where} ({was})" if not bits else f"it has also moved: {where}")
    if it.get("ambiguous"):
        could = it["ambiguous"]
        now = " now" if it["anchored"] else ""
        bits.append(f"{w} could{now} be {len(could)} things ({spec._some(could, 3)}); write it as `Owner.name` or"
                    " `path/to/file: name`" if not bits else f"the name could{now} be {len(could)} things")
    if not bits and it.get("body"):
        return f"{w} has changed inside since the spec was written; its signature is the same."
    return ". ".join(b[0].upper() + b[1:] if k else b for k, b in enumerate(bits)) + "." if bits else f"{w} matches the code."


def _title(g: dict) -> str:
    if g["kind"] == "spec":
        return g["source"]
    return f"Change {g['change']}" + (f" (archived in {g['archived_as']})" if g.get("archived_as") else "")


def text(r: dict) -> str:
    c = r["counts"]
    bad = sum(c[s] for s in DRIFTED)
    L = ["# Spec drift", ""]
    if not r["places"]:
        return ("# Spec drift\n\nNo openspec/ folder here or in the mapped repositories, so there are no specs to check.\n")
    L.append(f"Checked {spec._n(r['names'], 'code name')} in {spec._n(r['specs'], 'living spec')} and"
             f" {spec._n(r['changes'], 'change')} against the map.")
    if bad:
        parts = [f"{c['gone']} {'is' if c['gone'] == 1 else 'are'} gone" if c["gone"] else "",
                 f"{c['renamed']} {'was' if c['renamed'] == 1 else 'were'} renamed" if c["renamed"] else "",
                 f"{c['signature']} changed signature" if c["signature"] else "",
                 f"{c['moved']} moved" if c["moved"] else "",
                 f"{c['ambiguous']} could now be several things" if c["ambiguous"] else ""]
        L.append(f"{spec._n(bad, 'name no longer matches', 'names no longer match')} the code: "
                 f"{spec._and([p for p in parts if p])}.")
    else:
        L.append("Every name that is code still matches it.")
    if c["unclear"]:
        L[-1] += (f" {spec._n(c['unclear'], 'name')} could be several things, so what {'it names' if c['unclear'] == 1 else 'they name'}"
                  " cannot be followed; write each more fully.")
    for g in r["groups"]:
        shown = [it for it in g["items"] if it["state"] in SHOWN]
        inside = [it for it in g["items"] if it["state"] == "body"]
        if not shown and not inside:
            continue
        L += ["", f"## {_title(g)}", ""] + [f"- {line(it)}" for it in sorted(shown, key=lambda x: ORDER.index(x["state"]))]
        if inside:
            quoted = ["`" + it["written"] + "`" for it in inside]
            L.append(f"- Changed inside since the spec was written, same signature (read them to be sure the spec still"
                     f" holds): {spec._some(quoted, 6)}.")
    L.append("")
    tail = [f"Up to date: {spec._n(c['ok'], 'name')}."]
    if c["unanchored"]:
        one = c["unanchored"] == 1
        tail.append(f"{c['unanchored']} of them {'has' if one else 'have'} no record of what {'it' if one else 'they'}"
                    f" meant, so only the name is checked and a changed signature would not show; `leyline drift --accept`"
                    f" records {'it' if one else 'them'}.")
    if c["words"]:
        tail.append(f"{spec._n(c['words'], 'other word')} in backticks {'is' if c['words'] == 1 else 'are'} not code on the map"
                    " and not checked.")
    L.append(" ".join(tail))
    for p in r.get("problems") or []:
        L.append(f"Note: {p}.")
    if r.get("accepted") is not None:
        L.append(f"Recorded the code as it is now for {spec._n(len(r['accepted']), 'spec or change', 'specs and changes')}"
                 f" ({sum(r['accepted'].values())} names) in openspec/{ANCHOR_FILE}.")
    return "\n".join(L) + "\n"


def next_steps(r: dict, for_agent: bool = False) -> list[str]:
    c = r["counts"]
    if not r["places"]:
        return []
    if any(c[s] for s in SHOWN):
        return ["Next: for each name above, update the spec to name the code as it is (or change the code back, if the"
                " spec is right), then " + ("ask the person to confirm and call `drift` with accept=true" if for_agent else
                                            "run `leyline drift --accept`") + " to record the code as it is now."]
    if c["unanchored"] and not r.get("accepted"):
        return ["Next: nothing has drifted. When the specs and the code agree, " + (
            "call `drift` with accept=true" if for_agent else "`leyline drift --accept`")
                + " records what each name means now, so a later change of signature shows."]
    return ["Next: nothing to do; the specs match the code."]


# -- the plan's warning ----------------------------------------------------------------------------------
def _near(a: str, b: str) -> bool:
    return a == b or b.startswith((a + ".", a + "(", a + "/")) or a.startswith((b + ".", b + "(", b + "/"))


def touching(con, change_dir: str | Path, ids: list[str]) -> list[str]:
    """Specs that have drifted from code a planned change touches, one sentence each: "The living spec
    auth/spec.md names `Session.renew`, which has changed signature since it was written." """
    if not ids:
        return []
    dirs = places(con, change_dir)
    if not dirs:
        return []
    r = report(con, dirs)
    names = spec._Names(con)

    def ancestors(i):
        out = []
        while i in names.by_id:
            out.append(i)
            i = names.by_id[i]["parent_id"]
        return out
    own = DATED.sub("", Path(change_dir).name)   # the planned change's own anchors, from an earlier check
    out = []
    for g in r["groups"]:
        for it in g["items"]:
            if it["state"] not in DRIFTED or it.get("anchor_key") == own or g.get("change") == own:
                continue
            mine = set(it.get("ids", [])) | set(it.get("was_ids", []))
            if not any(_near(t, x) or t in ancestors(x) for t in ids for x in mine):
                continue
            who = (f"The living spec {g['capability']}/spec.md" if g["kind"] == "spec" else f"The change {g['change']}")
            what = ("which is gone" if it["state"] == "gone" else
                    f"which has been renamed to `{it['renamed']['to']}`" if it["state"] == "renamed" else
                    "which has changed signature since it was written" if it["state"] == "signature" else
                    f"which has moved to {it['moved']['to_path']} since it was written" if it["state"] == "moved" else
                    f"which could now be {len(it['ambiguous'])} things")
            out.append(f"{who} names `{it['written']}`, {what}. `leyline drift` says more.")
    return list(dict.fromkeys(out))
