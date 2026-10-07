"""Renames, for spec drift: an anchored name that is gone may have been renamed rather than deleted.

A gone anchor is taken to be renamed when exactly one node that no other anchor accounts for, of the same kind,
under the same owner (for a member) and in the same file or module, matches it by one of, in order:

    its text with the name taken out (`shape_hash`, kept on anchors recorded by this version),
    its content hash (a node whose name is not in its own text),
    its declaration with the name taken out, when no other node there has that declaration (a type's members too).

A candidate must also be new: one that was there beside the anchored node is a sibling, not its new name. Git
says so when the repository has it (the candidate's name was not in the file while the old name was); else the
baseline of the change that recorded the anchor (`.leyline/snapshots/spec-<id>.db`) does. The first two tests are
strong enough to stand when neither can tell; a declaration counts only for a node known to be new, since
`def (self)` fits many methods.

Git also says where a file went (`git log -M`, and the working tree against HEAD): that is where to look, and for
an anchor that names a file, the rename itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

TIMEOUT = 30


def _word(name: str) -> re.Pattern:
    return re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout if p.returncode == 0 else None


class Git:
    """What git says about the mapped repositories, asked once each."""

    def __init__(self, roots: list[Path]):
        self.repos = []
        for root in roots:
            prefix = _git(root, "rev-parse", "--show-prefix")
            if prefix is not None:
                self.repos.append((root, prefix.strip()))
        self._renames: Optional[dict] = None
        self._shown: dict = {}

    def show(self, root: Path, spec: str) -> Optional[str]:
        if (root, spec) not in self._shown:
            self._shown[(root, spec)] = _git(root, "show", spec)
        return self._shown[(root, spec)]

    def renames(self) -> dict[str, str]:
        """old path -> the path it has now, from renames in history and in the working tree, followed in order.
        Paths are relative to the mapped directory, as the map writes them."""
        if self._renames is not None:
            return self._renames
        out: dict[str, str] = {}
        for root, prefix in self.repos:
            pairs = []
            for text in (_git(root, "log", "-M", "--diff-filter=R", "--name-status", "--format=", "--reverse",
                              "--max-count=2000", "HEAD"),
                         _git(root, "diff", "-M", "--diff-filter=R", "--name-status", "HEAD")):
                for line in (text or "").splitlines():
                    bits = line.split("\t")
                    if len(bits) == 3 and bits[0].startswith("R") and bits[1].startswith(prefix) and bits[2].startswith(prefix):
                        pairs.append((bits[1][len(prefix):], bits[2][len(prefix):]))
            for old, new in pairs:
                for k, v in list(out.items()):
                    if v == old:
                        out[k] = new
                out[old] = new
        self._renames = {k: v for k, v in out.items() if k != v}
        return self._renames

    def was_new(self, path: str, old: str, new: str) -> Optional[bool]:
        """Whether `new` came into the file while `old` was in it. True when HEAD does not have it yet (the rename
        is not committed); else read from the parent of the commit that first wrote it, following the file across
        renames: the old name there and the new one not. None when git cannot tell."""
        back = {v: k for k, v in self.renames().items()}
        for root, prefix in self.repos:
            head = self.show(root, f"HEAD:{prefix}{path}")
            if head is None and path in back:
                head = self.show(root, f"HEAD:{prefix}{back[path]}")
            if head is None:
                continue
            if not _word(new).search(head):
                return True
            log = _git(root, "log", "--follow", "-M", "-S", new, "--format=@%H", "--name-status", "HEAD", "--",
                       f"{prefix}{path}") or ""
            blocks = [b.strip().splitlines() for b in log.split("@") if b.strip()]
            if not blocks:
                return None
            first = blocks[-1]                    # the oldest commit that changed how often it is written
            status = next((ln.split("\t") for ln in first[1:] if "\t" in ln), None)
            if status is None:
                return None
            was = status[1] if status[0].startswith("R") and len(status) == 3 else status[-1]
            before = self.show(root, f"{first[0]}^:{was}")
            if before is None:
                return None
            return bool(_word(old).search(before)) and not _word(new).search(before)
        return None


def _minus_name(text: Optional[str], name: str) -> str:
    return " ".join(_word(name).sub("", text or "", count=1).split()) if text else ""


CTORS = ("__init__", "__new__", "constructor", ".ctor", ".cctor", "new", "init", "setUp", "tearDown")


def _baseline_has(m, key: Optional[str], node_id: str) -> Optional[bool]:
    """Whether the baseline of the change that recorded the anchor (the code before that change) holds a node. None
    when there is no such baseline (a living spec's anchors, or a change that was forgotten)."""
    if not key or key.startswith("specs/"):
        return None
    snaps = m.__dict__.setdefault("_snaps", {})
    if key not in snaps:
        from . import diff
        p = diff.snapshot_path(m.con, "spec-" + key)
        snaps[key] = diff._open(p) if p.exists() else None
    db = snaps[key]
    if db is None:
        return None
    try:
        return db.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone() is not None
    except Exception:
        return None


def _is_new(m, key: Optional[str], a: dict, r) -> Optional[bool]:
    """Whether a candidate is new since the anchor was right: git first, else the recording change's baseline (a node
    there existed before that change, so after it too, beside the anchored one). None when neither can tell."""
    git = m.git()
    if git and a.get("name"):
        v = git.was_new(r["path"], a["name"], r["name"])
        if v is not None:
            return v
    had = _baseline_has(m, key, r["id"])
    return None if had is None else not had


def find(m, a: dict, key: Optional[str] = None) -> Optional[dict]:
    """What a gone anchor was most likely renamed to: {"id", "to", "to_path", "how"}, or None. `key` is the change
    (or specs/<capability>) the anchor belongs to."""
    from . import drift
    names = m.names
    git = m.git()
    moved = git.renames().get(a.get("path") or "") if git else None
    if a["kind"] in ("file", "module"):
        if moved and names.file(moved):
            f = names.file(moved)[0]
            return {"id": f["id"], "to": moved, "to_path": moved, "how": "git records the file as renamed"}
        return None
    paths = {p for p in (a.get("path"), moved) if p}
    files = [r for p in paths for r in names.file(p) if r["kind"] == "file" and r["path"] == p]
    modules = {names.module.get(f["id"]) for f in files} - {None}
    if not modules:   # the file is gone: the module is the folder it was in
        folder = str(Path(a.get("path") or "").parent).replace("\\", "/")
        modules = {r["id"] for r in names.rows if r["kind"] == "module" and (r["path"] or ".") == folder}
    label = a.get("label") or ""
    owner = label.rsplit(".", 1)[0] if "." in label and a["kind"] in ("callable", "field", "test") and label != a.get("path") else None

    def placed(r) -> bool:
        parent = names.by_id.get(r["parent_id"])
        if owner is not None:
            if parent is None or parent["kind"] != "type" or parent["name"] != owner.rsplit(".", 1)[-1]:
                return False
        elif a["kind"] in ("callable", "field") and parent is not None and parent["kind"] == "type":
            return False                     # a top-level function is not renamed into a method
        return r["path"] in paths or names.module.get(r["id"]) in modules

    region = [r for r in names.rows if r["kind"] == a["kind"] and r["name"] != a.get("name") and r["name"] not in CTORS
              and placed(r)]
    if not region:
        return None
    fps = {r["id"]: drift.fingerprint(m.con, names, r["id"]) for r in region}
    fresh = [r for r in region if r["id"] not in m.claimed and fps[r["id"]]]

    def new(rs, sure=False):   # leave out a candidate that was there before; `sure`: keep only one known to be new
        out = []
        for r in rs:
            v = _is_new(m, key, a, r)
            if v is True or (v is None and not sure):
                out.append(r)
        return out
    tiers = [("has the same body", lambda fp: a.get("shape_hash") and fp.get("shape_hash") == a["shape_hash"]),
             ("has the same body", lambda fp: a.get("body_hash") and fp["body_hash"] == a["body_hash"])]
    for how, same in tiers:
        hits = new([r for r in fresh if same(fps[r["id"]])])
        if len(hits) == 1:
            return _found(m, hits[0], how)
        if len(hits) > 1:
            return None
    shape = _minus_name(a.get("decl"), a.get("name") or "")
    if not shape or (a["kind"] == "type" and not a.get("members")):
        return None

    def same_decl(r) -> bool:
        fp = fps[r["id"]]
        if not fp or _minus_name(fp.get("decl"), r["name"]) != shape:
            return False
        return a["kind"] != "type" or sorted(fp.get("members") or []) == sorted(a.get("members") or [])
    every = [r for r in region if same_decl(r)]   # the declaration must say which one: no other node there has it
    # A declaration is weak evidence (`def (self)` fits many methods): it counts only for a node known to be new.
    if len(every) == 1 and every[0] in fresh and new(every, sure=True):
        return _found(m, every[0], "has the same declaration")
    return None


def _found(m, r, how: str) -> dict:
    return {"id": r["id"], "to": m.label(r["id"]), "to_path": r["path"], "how": how}


def for_file(m, written: str) -> Optional[dict]:
    """A file path a spec names that is gone: where git says it went, when that file is on the map or on disk."""
    git = m.git()
    if not git:
        return None
    moved = git.renames().get(written.strip().lstrip("./"))
    if moved and (m.names.file(moved) or m.names.on_disk(moved)):
        return {"id": None, "to": moved, "to_path": moved, "how": "git records the file as renamed"}
    return None
