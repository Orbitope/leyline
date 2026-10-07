"""Outline: a large module split into parts a person can hold in their head, and names for those parts.

On a large repository a module is still huge (Parlance's `editor/client` holds 249 files) and the map draws it as one
box. `outline(con, part)` splits one level of it into at most MAX_PARTS parts:

    folders        when the folder tree means something: each subfolder is a part, and the files lying loose beside
                   them are one more ("files in src/"). A folder that holds nearly everything is shown instead of its
                   parent, so `editor/client` is shown from `editor/client/src`.
    groups         where a folder is flat (MIN_CLUSTER_FILES loose files or more), its files are grouped by Louvain
                   community detection over calls, type use and inheritance between their types and functions; each
                   file goes to the group that holds most of its code, so a group is a set of whole files. When few
                   of them link to each other (a folder of tests), they are grouped by the code they use instead. A
                   module that is one flat folder reuses the systems the map proposed (cluster.py).
    files          a small folder, a group or what is left over: one part per file.
    a leaf         a file, a type or a proposed system: its key types and functions.

A level of more than MAX_PARTS parts shows the largest and one `@more` part holding the rest. For each part it gives
its size, its entry points, the parts and modules it uses and that use it (counted links), the channels that cross
its edge, its busiest functions (most flows through them) and what makes it risky beside its siblings.

Part ids hold across maps where they can: a module is its node id, a folder `<repo>:dir:<path>`, the loose files of a
folder `<repo>:dir:<path>#files`, a group `<repo>:dir:<path>#system:<anchor>` named after its most linked type or
file, what no group took `<repo>:dir:<path>#other`, a file or a type its node id.

`name_part` stores a name and a one-line summary for a part (table `part_names`, inferred layer unless the person
said it), with the part's files at the time. Outlines, the map page, `overview` and `explain_path` show the names. A
name whose part kept less than half of those files, or most of whose evidence is gone, is shown as "may be stale"; a
group whose anchor changed (so its id did) takes the name of the old group of the same folder it shares most files
with.
"""

from __future__ import annotations

import json
import posixpath
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from . import cluster, store

MAX_PARTS = 12
MIN_CLUSTER_FILES = 8     # a folder with fewer loose files lists them as they are
MIN_CLUSTER_FILES_BESIDE = 15   # the same, for loose files beside subfolders: fewer are one part, "files in x/"
MIN_MODULARITY = 0.3      # as cluster.py: a weaker split is noise
NEARLY_ALL = 0.9          # a folder holding this share of a part's files, with at most SMALL_REST others, is shown instead
SMALL_REST = 5
KEPT_FOR_FRESH = 0.5      # a named part that kept less than this share of its files may be stale
TOP = ("<module>", "<top-level>")
TYPE_LINKS = ("uses_type", "instantiates", "extends", "implements")

ENTRY_WORDS = {   # how each kind of entry point is said, one and many
    "route": ("route", "routes"), "message": ("message handler", "message handlers"),
    "entry": ("program", "programs"), "command": ("command", "commands"), "ui": ("UI handler", "UI handlers"),
    "engine": ("engine callback", "engine callbacks"), "component": ("UI component", "UI components"),
}
ENGINE_CALLBACKS = {"Awake", "Start", "Update", "FixedUpdate", "LateUpdate", "OnEnable", "_ready", "_process",
                    "_physics_process", "_input"}


# -- the model: everything an outline reads, loaded once per map run ----------------------------------------------------
class Model:
    def __init__(self, con):
        q = con.execute
        self.repos = [r[0] for r in q("SELECT id FROM nodes WHERE kind = 'repo' ORDER BY id")]
        self.modules: dict[str, dict] = {}
        for r in q("SELECT id, name, path, repo_id FROM nodes WHERE kind = 'module' AND layer = 'fact'"):
            self.modules[r[0]] = {"name": r[1], "path": "" if r[2] in (None, ".") else r[2], "repo": r[3]}
        self.files: dict[str, dict] = {}
        self.files_of_module: dict[str, list] = defaultdict(list)
        for r in q("SELECT id, path, parent_id, repo_id, span_end FROM nodes WHERE kind = 'file' AND layer = 'fact'"
                   " ORDER BY path"):
            mod = r[2] if r[2] in self.modules else None
            self.files[r[0]] = {"path": r[1] or "", "module": mod, "repo": r[3], "lines": r[4] or 0}
            if mod:
                self.files_of_module[mod].append(r[0])
        self.file_by_path = {(f["repo"], f["path"]): i for i, f in self.files.items()}
        nodes = {r[0]: r for r in q("SELECT id, kind, name, parent_id, attrs, span_start, span_end FROM nodes"
                                    " WHERE kind IN ('type', 'callable', 'test') AND layer = 'fact'")}
        self.name = {i: r[2] for i, r in nodes.items()}
        self.kind = {i: r[1] for i, r in nodes.items()}
        self.lines_of = {i: (r[6] or 0) - (r[5] or 0) + 1 for i, r in nodes.items()}
        self.file_of: dict[str, str] = {}
        for nid, fid in q("SELECT node_id, file_id FROM ancestry WHERE file_id IS NOT NULL"):
            if nid in nodes and fid in self.files:
                self.file_of[nid] = fid
        # The unit of a node: its outermost type, or its file when it sits loose in one (as cluster.py has it).
        self.unit: dict[str, str] = {}
        for i in nodes:
            cur = i
            while nodes.get(nodes[cur][3]) is not None:
                cur = nodes[cur][3]
            self.unit[i] = cur if nodes[cur][1] == "type" else self.file_of.get(i)
        self.units_of_file: dict[str, list] = defaultdict(list)
        self.code_units: set = set()
        for i, u in self.unit.items():
            if u and nodes[i][1] in ("type", "callable"):
                self.code_units.add(u)
        for u in sorted(self.code_units):
            f = self.file_of.get(u) or (u if u in self.files else None)
            if f:
                self.units_of_file[f].append(u)
        entry = {r[0]: json.loads(r[1] or "{}").get("trigger") for r in q(
            "SELECT e.dst_id, n.attrs FROM edges e JOIN nodes n ON n.id = e.src_id WHERE e.kind = 'exposes'")}
        channel_in: dict[str, set] = defaultdict(set)
        self.channels: dict[str, list] = defaultdict(list)   # file -> (src file, dst file, channel, address) touching it
        for s, d, a in q("SELECT src_id, dst_id, attrs FROM edges WHERE kind = 'communicates'"):
            try:
                at = json.loads(a or "{}")
            except ValueError:
                at = {}
            ch = at.get("channel") or "channel"
            channel_in[d].add(ch)
            fs, fd = self._file(s), self._file(d)
            if fs and fd and fs != fd:
                addr = str(at.get("address") or "")
                if re.match(r"^[\w.-]+:[\w#-]+:", addr):   # an event's address is its node id: say its name
                    addr = re.split(r"[.:]", addr)[-1]
                c = (fs, fd, ch, addr)
                self.channels[fs].append(c)
                self.channels[fd].append(c)
        # Functions and tests per file, and what kind of entry point each function is.
        self.functions: dict[str, list] = defaultdict(list)
        self.tests: dict[str, int] = Counter()
        self.entry_kind: dict[str, str] = {}
        self.route: dict[str, str] = {}
        for i, r in nodes.items():
            f = self.file_of.get(i)
            if f is None or r[1] == "type":
                continue
            a = _json(r[4])
            if r[1] == "test" or a.get("is_test"):
                self.tests[f] += 1
                continue
            if r[2] in TOP and i not in entry:
                continue
            self.functions[f].append(i)
            parent = nodes.get(r[3])
            k = _entry_kind(i, r[2], a, parent, entry, channel_in.get(i, set()))
            if k:
                self.entry_kind[i] = k
                if a.get("route"):
                    self.route[i] = a["route"]
        self.flows = {r[0]: r[1] for r in q("SELECT k.id, COUNT(*) FROM steps s JOIN keys k ON k.k = s.callable"
                                            " GROUP BY s.callable")}
        self.tested = {r[0] for r in q(store.TESTED)}
        # Links between files (for uses / used by) and between units of one module (for grouping).
        self.fout: dict[str, Counter] = defaultdict(Counter)
        self.fin: dict[str, Counter] = defaultdict(Counter)
        self.uadj: dict[str, Counter] = defaultdict(Counter)

        def link(s, d, n, w):
            fs, fd = self._file(s), self._file(d)
            if fs and fd and fs != fd:
                self.fout[fs][fd] += n
                self.fin[fd][fs] += n
            a, b = self.unit.get(s) or (s if s in self.files else None), self.unit.get(d) or (d if d in self.files else None)
            if a and b and a != b and fs and fd and self.files[fs]["module"] == self.files[fd]["module"]:
                self.uadj[a][b] += w
                self.uadj[b][a] += w
        for s, d, n in q("SELECT sk.id, dk.id, c.n FROM (SELECT src, dst, COUNT(*) AS n FROM call_sites GROUP BY src, dst) c"
                         " JOIN keys sk ON sk.k = c.src JOIN keys dk ON dk.k = c.dst"):
            link(s, d, n, cluster.WEIGHTS["calls"] * n)
        for k, s, d in q(f"SELECT kind, src_id, dst_id FROM edges WHERE kind IN ({','.join('?' * len(TYPE_LINKS))})",
                         TYPE_LINKS):
            link(s, d, 1, cluster.WEIGHTS[k])
        # Systems the map proposed (cluster.py), by module.
        self.systems: dict[str, list] = defaultdict(list)
        self.system_units: dict[str, list] = defaultdict(list)
        self.system_name: dict[str, str] = {}
        for sid, name, mod in q("SELECT id, name, parent_id FROM nodes WHERE kind = 'system' ORDER BY id"):
            self.systems[mod].append(sid)
            self.system_name[sid] = name
        for s, d in q("SELECT src_id, dst_id FROM edges WHERE kind = 'groups'"):
            self.system_units[s].append(d)
        self.splits: dict[str, tuple] = {}
        self.lock = threading.Lock()

    def _file(self, nid: str) -> Optional[str]:
        return nid if nid in self.files else self.file_of.get(nid)

    def lines(self, files) -> int:
        return sum(self.files[f]["lines"] for f in files)


def _json(raw) -> dict:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _entry_kind(nid, name, a, parent, entry, chans) -> Optional[str]:
    """What kind of entry point a function is, as explain.find_flows says it; None for an ordinary function."""
    if a.get("native_kind") == "route_handler" or a.get("route") or "http" in chans:
        return "route"
    if chans & {"queue", "event", "rpc"}:
        return "message"
    if nid in entry:
        return "entry"
    decorators = " ".join(map(str, a.get("decorators") or []))
    if re.search(r"\.command\b|\bcommand\(|\bcli\b", decorators):
        return "command"
    if re.match(r"(on|handle)[A-Z_]", name or ""):
        return "ui"
    if name in ENGINE_CALLBACKS:
        return "engine"
    if a.get("native_kind") == "component":
        return "component"
    if parent is not None and parent[1] == "callable" and "component" in (parent[4] or ""):
        return "ui"
    return None


_cache: dict = {}
_cache_lock = threading.Lock()


def model(con) -> Model:
    """The model of a store, built once per map run and kept."""
    gen = con.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()
    stamp = (gen[0] if gen else None, con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
             con.execute("SELECT COUNT(*) FROM call_sites").fetchone()[0])
    key = str(store.store_file(con))
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and hit[0] == stamp and stamp[0] is not None:
            return hit[1]
    m = Model(con)
    with _cache_lock:
        _cache[key] = (stamp, m)
    return m


# -- parts ---------------------------------------------------------------------------------------------------------------
@dataclass
class Part:
    id: str
    kind: str             # workspace, repo, module, folder, files, system, other, file, type, more
    label: str            # its name until someone gives it one
    files: list           # file ids, sorted
    path: str = ""        # the folder it stands for (a file's path for a file)
    module: str = ""
    repo: str = ""
    parent: str = ""      # the id of the part it was split from
    rest: list = field(default_factory=list)     # a `more` part: the parts it holds
    around: list = field(default_factory=list)   # a group made by what it uses: the names of that code
    node: str = ""        # a type: its node id


def _dir_id(repo: str, path: str) -> str:
    return f"{repo}:dir:{path}"


def _rel(m: Model, part: Part, path: str) -> str:
    """A folder's path as shown inside its module: relative to the module's own folder."""
    base = m.modules.get(part.module, {}).get("path", "")
    return path[len(base) + 1:] if base and path.startswith(base + "/") else path


def _under(top: str, path: str) -> str:
    """A folder's path as shown inside the part (of folder `top`) it was split from."""
    if path == top:
        return (posixpath.basename(path) or "(repository root)") + "/"
    return (path[len(top) + 1:] if top and path.startswith(top + "/") else path) + "/"


def _folder_label(m: Model, mod: str, path: str) -> str:
    base = m.modules.get(mod, {}).get("path", "")
    if path == base:
        return (base or "(repository root)") + "/"
    return (path[len(base) + 1:] if base and path.startswith(base + "/") else path) + "/"


def module_part(m: Model, mod: str) -> Part:
    info = m.modules[mod]
    return Part(mod, "module", (info["path"] or "(repository root)") + "/", list(m.files_of_module.get(mod, [])),
                info["path"], mod, info["repo"])


def folder_part(m: Model, repo: str, path: str, loose_only: bool = False) -> Optional[Part]:
    """The files under a folder (or lying directly in it), in the module that holds the folder."""
    path = path.strip("/")
    mods = [k for k, v in m.modules.items() if v["repo"] == repo and (not v["path"] or path == v["path"]
                                                                       or path.startswith(v["path"] + "/"))]
    if not mods:
        return None
    mod = max(mods, key=lambda k: len(m.modules[k]["path"]))
    pre = path + "/" if path else ""
    files = [f for f in m.files_of_module.get(mod, []) if m.files[f]["path"].startswith(pre)
             and (not loose_only or "/" not in m.files[f]["path"][len(pre):])]
    if not files:
        return None
    if loose_only:
        name = "files in " + _folder_label(m, mod, path)
        return Part(_dir_id(repo, path) + "#files", "files", name, files, path, mod, repo)
    return Part(_dir_id(repo, path), "folder", _folder_label(m, mod, path), files, path, mod, repo)


def file_part(m: Model, f: str, parent: str = "") -> Part:
    info = m.files[f]
    return Part(f, "file", info["path"].rsplit("/", 1)[-1], [f], info["path"], info["module"] or "", info["repo"], parent)


def _sort(m: Model, parts: list) -> list:
    return sorted(parts, key=lambda p: (-m.lines(p.files), -len(p.files), p.id))


def _pack(m: Model, owner: Part, kids: list) -> list:
    """At most MAX_PARTS, largest first; the rest in one `more` part."""
    kids = _sort(m, kids)
    if len(kids) <= MAX_PARTS:
        return kids
    keep, rest = kids[:MAX_PARTS - 1], kids[MAX_PARTS - 1:]
    files = sorted({f for k in rest for f in k.files})
    more = Part(owner.id + "@more", "more", f"{len(rest)} more parts", files, owner.path, owner.module, owner.repo,
                owner.id, rest=rest)
    return _sort(m, keep) + [more]


def split(m: Model, part: Part) -> tuple[list, str, Optional[str]]:
    """The parts one level down: (parts, how they were split, the folder shown from when it is not the part's own)."""
    with m.lock:
        hit = m.splits.get(part.id)
    if hit is not None:
        return hit
    out = _split(m, part)
    with m.lock:
        m.splits[part.id] = out
    return out


def _split(m: Model, part: Part) -> tuple[list, str, Optional[str]]:
    if part.kind == "workspace":
        return [Part(r, "repo", r, sorted(f for f, v in m.files.items() if v["repo"] == r), "", "", r, part.id)
                for r in m.repos], "repositories", None
    if part.kind == "repo":
        mods = [module_part(m, k) for k, v in m.modules.items() if v["repo"] == part.repo and m.files_of_module.get(k)]
        for x in mods:
            x.parent = part.id
        return _pack(m, part, mods), "modules", None
    if part.kind == "more":
        return part.rest, "the rest of the level", None
    if part.kind in ("file", "type"):
        return [], "", None
    if part.kind in ("system", "files", "other"):
        if len(part.files) < 2:
            return [], "", None
        return _pack(m, part, [file_part(m, f, part.id) for f in part.files]), "files", None
    return _folders(m, part)


def _folders(m: Model, part: Part) -> tuple[list, str, Optional[str]]:
    base, files, extra, shown_from = part.path, list(part.files), [], None
    while True:
        pre = base + "/" if base else ""
        subs: dict[str, list] = defaultdict(list)
        loose = []
        for f in files:
            rest = m.files[f]["path"][len(pre):]
            (subs[rest.split("/", 1)[0]].append(f) if "/" in rest else loose.append(f))
        if not subs:
            break
        big = max(subs, key=lambda k: (len(subs[k]), k))
        others = len(files) - len(subs[big])
        if len(subs[big]) < NEARLY_ALL * len(files) or others > SMALL_REST:
            break
        # One folder holds nearly everything: show it, and keep the few files beside it as parts of their own.
        for k, fs in sorted(subs.items()):
            if k != big:
                extra.append(Part(_dir_id(part.repo, pre + k), "folder", _under(part.path, pre + k),
                                  fs, pre + k, part.module, part.repo, part.id))
        if len(loose) == 1:
            extra.append(file_part(m, loose[0], part.id))
        elif loose:
            extra.append(Part(_dir_id(part.repo, base) + "#files", "files", "files in " + _under(part.path, base),
                              loose, base, part.module, part.repo, part.id))
        base, files = pre + big, subs[big]
        shown_from = base
    kids = list(extra)
    for k, fs in sorted(subs.items()):
        kids.append(Part(_dir_id(part.repo, pre + k), "folder", _under(part.path, pre + k), fs, pre + k,
                         part.module, part.repo, part.id))
    how = "folders" if subs else "files"
    # Beside folders, a few loose files are one part; only many of them are worth grouping.
    if len(loose) >= (MIN_CLUSTER_FILES_BESIDE if subs else MIN_CLUSTER_FILES):
        groups = _groups(m, part, base, loose)
        if groups:
            kids += groups
            how = "folders and groups of files" if subs else "groups of files"
        elif subs:
            kids.append(Part(_dir_id(part.repo, base) + "#files", "files", "files in " + _under(part.path, base),
                             loose, base, part.module, part.repo, part.id))
        else:
            kids += [file_part(m, f, part.id) for f in loose]
    elif subs and len(loose) > 1:
        kids.append(Part(_dir_id(part.repo, base) + "#files", "files", "files in " + _under(part.path, base),
                         loose, base, part.module, part.repo, part.id))
    else:
        kids += [file_part(m, f, part.id) for f in loose]
    if len(kids) == 1 and kids[0].kind in ("folder", "files") and set(kids[0].files) == set(part.files):
        return split(m, kids[0])
    return _pack(m, part, kids), how, shown_from


def _groups(m: Model, part: Part, base: str, loose: list) -> list:
    """The loose files of a flat folder in groups (see the module's docstring); [] when they do not split cleanly."""
    try:
        import networkx as nx
    except ImportError:
        return []
    U = {u for f in loose for u in m.units_of_file.get(f, ())}
    if len(U) < 4:
        return []
    mod = part.module
    stored = [s for s in m.systems.get(mod, []) if m.system_units.get(s)]
    every = {u for f in m.files_of_module.get(mod, []) for u in m.units_of_file.get(f, ())}
    found: list = []    # (units of U, stored system id or None, the code around it it uses)
    if stored and base == m.modules[mod]["path"] and len(U) >= NEARLY_ALL * len(every):
        found = [(set(m.system_units[s]) & U, s, []) for s in stored]
        found = [x for x in found if len(x[0]) >= 2]
    if not found:
        found = _louvain(nx, m, U, set())
        placed = set().union(*(x[0] for x in found)) if found else set()
        if len(placed) < 0.5 * len(U):
            # Few of them link to each other (a folder of tests, say): group them by the code they use instead, with
            # that code in the graph and left out of the groups.
            ctx = {v for u in U for v in m.uadj.get(u, ()) if v not in U}
            again = _louvain(nx, m, U, ctx)
            if again and len(set().union(*(x[0] for x in again))) > len(placed):
                found = again
    if not found:
        return []
    votes: dict = defaultdict(Counter)
    for k, x in enumerate(found):
        for u in x[0]:
            votes[m.file_of.get(u) or u][k] += 1
    by_group: dict = defaultdict(list)
    left = []
    for f in sorted(loose):
        if votes.get(f):
            by_group[sorted(votes[f].items(), key=lambda kv: (-kv[1], -len(found[kv[0]][0]), kv[0]))[0][0]].append(f)
        else:
            left.append(f)
    if len(by_group) < 2 and not left:
        return []
    out, used = [], Counter()
    folder = _dir_id(part.repo, base)
    for k, fs in sorted(by_group.items()):
        units, sid, around = found[k]
        if len(fs) == 1:   # a file and its own types: that is the file, not a group
            out.append(file_part(m, fs[0], folder))
            continue
        # Named after its most linked type or file among the files it holds (a stored system keeps its id, but its
        # anchor may sit in a file that went to another group).
        own = {u for f in fs for u in m.units_of_file.get(f, ())} & units
        anchor = sorted(own or units, key=lambda u: (-sum(w for v, w in m.uadj.get(u, {}).items() if v in own), u))[0]
        nm = m.name.get(anchor) or m.files.get(anchor, {}).get("path", anchor).rsplit("/", 1)[-1]
        label = f"{nm} group"
        if sid is None:
            used[nm] += 1
            sid = f"{folder}#system:{nm}" + (f"~{used[nm]}" if used[nm] > 1 else "")
        out.append(Part(sid, "system", label, fs, base, mod, part.repo, folder,
                        around=[m.name.get(v) or m.files.get(v, {}).get("path", v).rsplit("/", 1)[-1] for v in around]))
    if len(left) == 1:
        out.append(file_part(m, left[0], folder))
    elif left:
        out.append(Part(f"{folder}#other", "other", "other files in " + _under(part.path, base), left, base, mod,
                        part.repo, folder))
    return out


def _louvain(nx, m: Model, U: set, ctx: set) -> list:
    """Louvain communities of U (with ctx in the graph but not in the result) holding two or more units of U, as
    [(units of U, None, the ctx units it is most linked to)]. [] when the split is weak."""
    nodes = U | ctx
    g = nx.Graph()
    g.add_nodes_from(sorted(nodes))
    for a in sorted(nodes):
        for b, w in sorted(m.uadj.get(a, {}).items()):
            if a < b and b in nodes and (a in U or b in U):
                g.add_edge(a, b, weight=w)
    if not g.number_of_edges():
        return []
    comms = nx.community.louvain_communities(g, weight="weight", seed=7)
    if nx.community.modularity(g, comms, weight="weight") < MIN_MODULARITY:
        return []
    out = []
    for c in sorted((c for c in comms if len(c & U) >= 2), key=lambda c: (-len(c & U), sorted(c & U)[0])):
        around = sorted(c - U, key=lambda v: (-sum(g[v][w]["weight"] for w in g[v] if w in U), v))[:3]
        out.append((c & U, None, around))
    return out


# -- finding a part by its id ------------------------------------------------------------------------------------------
def find(m: Model, pid: Optional[str]) -> Optional[Part]:
    """The part an id names: a module, a folder, a group, a file, a type, a repository, or a module's path."""
    if not pid or not pid.strip():
        if len(m.repos) == 1:
            return Part(m.repos[0], "repo", m.repos[0], sorted(m.files), "", "", m.repos[0])
        return Part("workspace", "workspace", "the workspace", sorted(m.files))
    pid = pid.strip()
    if pid in m.repos:
        return Part(pid, "repo", pid, sorted(f for f, v in m.files.items() if v["repo"] == pid), "", "", pid)
    if pid in m.modules:
        return module_part(m, pid)
    if pid in m.files:
        return file_part(m, pid)
    if pid in m.system_units:
        mod = next((k for k, v in m.systems.items() if pid in v), "")
        files = sorted({m.file_of.get(u) or u for u in m.system_units[pid]} & set(m.files))
        return Part(pid, "system", m.system_name.get(pid, pid), files, m.modules.get(mod, {}).get("path", ""), mod,
                    m.modules.get(mod, {}).get("repo", ""), mod)
    if m.kind.get(pid) == "type":
        f = m.file_of.get(pid)
        return Part(pid, "type", m.name[pid], [f] if f else [], m.files[f]["path"] if f else "",
                    m.files[f]["module"] if f else "", m.files[f]["repo"] if f else "", f or "", node=pid)
    if pid.endswith("@more"):
        owner = find(m, pid[:-len("@more")])
        if owner is None:
            return None
        return next((k for k in split(m, owner)[0] if k.id == pid), None)
    if ":dir:" in pid:
        repo, _, rest = pid.partition(":dir:")
        path, _, frag = rest.partition("#")
        if frag == "files":
            return folder_part(m, repo, path, loose_only=True)
        folder = folder_part(m, repo, path)
        if folder is None or not frag:
            return folder
        return _search(m, folder, pid)
    # A path: a module's, or a folder's in one of the repositories.
    path = pid.strip("/")
    for k, v in sorted(m.modules.items()):
        if v["path"] == path or (path in (".", "") and not v["path"]):
            return module_part(m, k)
    for r in m.repos:
        p = folder_part(m, r, path)
        if p is not None:
            return p
    f = next((i for i, v in m.files.items() if v["path"] == path), None)
    return file_part(m, f) if f else None


def _search(m: Model, start: Part, pid: str, depth: int = 3) -> Optional[Part]:
    """A part made by splitting `start` (a group, what no group took): looked for a few levels down."""
    level = [start]
    for _ in range(depth):
        nxt = []
        for p in level:
            for k in split(m, p)[0]:
                if k.id == pid:
                    return k
                if k.kind in ("folder", "more", "module") and pid.startswith(k.id.split("@")[0].split("#")[0]):
                    nxt.append(k)
        level = nxt
    return None


# -- names -----------------------------------------------------------------------------------------------------------------
def ensure(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS part_names (part_id TEXT PRIMARY KEY, name TEXT NOT NULL, summary TEXT,"
                " layer TEXT NOT NULL, source TEXT, kind TEXT, parent TEXT, members TEXT, evidence TEXT, created TEXT)")


def _rows(con) -> dict:
    try:
        return {r[0]: {"id": r[0], "name": r[1], "summary": r[2] or "", "layer": r[3], "kind": r[4], "parent": r[5],
                       "members": json.loads(r[6] or "[]"), "evidence": json.loads(r[7] or "[]")}
                for r in con.execute("SELECT part_id, name, summary, layer, kind, parent, members, evidence FROM part_names")}
    except Exception:   # a store with no names yet
        return {}


def names(con, parts: list) -> dict:
    """{part id: {"name", "summary", "by"?, "stale"?, "from"?}} for the parts that have a name, or inherit one."""
    rows = _rows(con)
    if not rows:
        return {}
    out = {}
    ids = {p.id for p in parts}
    for p in parts:
        r = rows.get(p.id)
        if r is None and p.kind == "system":
            # Its anchor (and so its id) changed: the name of the old group of this folder it shares most files with.
            best, score = None, 0.0
            for x in rows.values():
                if x["kind"] == "system" and x["parent"] == p.parent and x["id"] not in ids and x["members"]:
                    a, b = set(x["members"]), set(p.files)
                    j = len(a & b) / len(a | b)
                    if j > score:
                        best, score = x, j
            if best is not None and score >= KEPT_FOR_FRESH:
                r = {**best, "from": best["id"]}
        if r is None:
            continue
        item = {"name": r["name"], "summary": r["summary"]}
        if r["layer"] == "intent":
            item["by"] = "the person"
        if r.get("from"):
            item["from"] = r["from"]
        stale = _stale(con, p, r)
        if stale:
            item["stale"] = stale
        out[p.id] = item
    return out


def _stale(con, p: Part, r: dict) -> Optional[str]:
    if p.kind not in ("folder", "module", "repo", "file", "type") and r["members"]:
        had = set(r["members"])
        kept = len(had & set(p.files)) / len(had)
        if kept < KEPT_FOR_FRESH or len(set(p.files) - had) > len(had):
            return (f"may be stale: it kept {round(100 * kept)}% of the {len(had)} files it had when named, and has"
                    f" {len(p.files)} now")
    ev = r["evidence"]
    if ev:
        gone = [e for e in ev if con.execute("SELECT 1 FROM nodes WHERE id = ?", (e,)).fetchone() is None]
        if len(gone) * 2 > len(ev):
            return "may be stale: most of the code it was named from is gone"
    return None


def name_part(con, part_id: str, name: str, summary: str = "", evidence: Optional[list] = None,
              layer: str = "inferred", source: str = "mcp") -> dict:
    """Store a name and a one-line summary for a part, replacing an earlier one."""
    name, summary = (name or "").strip(), " ".join((summary or "").split())
    if not name:
        return {"error": "name is empty: give the part a short name, such as \"Validation rules\"."}
    if layer not in ("inferred", "intent"):
        return {"error": "layer must be inferred (your reading of the code) or intent (the person said it)"}
    m = model(con)
    p = find(m, part_id)
    if p is None:
        return {"error": f"no part {part_id!r}. Part ids come from module_outline (`leyline outline`)."}
    if p.kind in ("more", "workspace"):
        return {"error": f"{part_id!r} only holds the rest of a level; name the parts inside it."}
    evidence = [e for e in (evidence or []) if e]
    if layer == "inferred":
        if not evidence:
            return {"error": "an inferred name needs evidence: the node ids of the code you read to name it"
                             " (from the outline's busiest and key items, or `search`)."}
        missing = [e for e in evidence if con.execute("SELECT 1 FROM nodes WHERE id = ?", (e,)).fetchone() is None]
        if missing:
            return {"error": f"evidence ids not on the map: {missing[:3]}. Use node ids from module_outline or `search`."}
    ensure(con)
    with con:
        con.execute("INSERT OR REPLACE INTO part_names VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (p.id, name, summary, layer, source, p.kind, p.parent, json.dumps(sorted(p.files)),
                     json.dumps(evidence), time.strftime("%Y-%m-%dT%H:%M:%S")))
    if p.id in m.system_units:   # a system the map proposed: `overview` and the map's system boxes show it too
        store.annotate(con, p.id, "name", name, layer, source, None, evidence)
        if summary:
            store.annotate(con, p.id, "responsibility", summary, layer, source, None, evidence)
    return {"part_id": p.id, "name": name, "summary": summary, "layer": layer, "files": len(p.files),
            "next": "Name the other parts the same way; module_outline shows the names, and the map page after `map`."}


def labels(con, node_ids: list) -> dict:
    """{node id: the name of the most specific named part it sits in} for the ids that sit in one."""
    rows = _rows(con)
    if not rows or not node_ids:
        return {}
    m = model(con)
    out = {}
    for nid in node_ids:
        f = m._file(nid)
        if f is None:
            continue
        info = m.files[f]
        best = None   # (specificity, name): fewer files is more specific
        for r in rows.values():
            pid = r["id"]
            if pid == f or (r["kind"] not in ("folder", "module", "repo") and f in r["members"]):
                size = len(r["members"]) or 1
            elif r["kind"] == "folder" and pid.startswith(info["repo"] + ":dir:") and \
                    info["path"].startswith(pid.split(":dir:", 1)[1].rstrip("/") + "/"):
                size = 10_000 - pid.count("/")
            elif r["kind"] == "module" and pid == info["module"]:
                size = 100_000
            else:
                continue
            if best is None or size < best[0]:
                best = (size, r["name"])
        if best:
            out[nid] = best[1]
    return out


def named_parts(con) -> dict:
    """{part id: {"id", "name", "summary", "kind", "module"}} for every part given a name, for `overview`."""
    out = {}
    for k, r in sorted(_rows(con).items()):
        mod = k if r["kind"] == "module" else ""
        if not mod and r["members"]:
            row = con.execute("SELECT parent_id FROM nodes WHERE id = ?", (r["members"][0],)).fetchone()
            mod = row[0] if row else ""
        out[k] = {"id": k, "name": r["name"], "summary": r["summary"], "kind": r["kind"], "module": mod or ""}
    return out


# -- describing a part --------------------------------------------------------------------------------------------------
def _outer(m: Model, g: str, within: Part) -> tuple[str, str]:
    """(id, label) for a file outside `within`: the folder next to it in the same module, or its module."""
    info = m.files[g]
    if within.kind in ("workspace", "repo") or info["module"] != within.module:
        mod = info["module"]
        if mod is None:
            return info["repo"], info["repo"]
        lab = m.modules[mod]["path"] or "(repository root)"
        return mod, (lab if info["repo"] == within.repo or not within.repo else f"{info['repo']}/{lab}")
    folder = posixpath.dirname(info["path"])
    base = within.path
    common = posixpath.commonpath([base, folder]) if base and folder else ""
    rest = info["path"][len(common) + 1:] if common else info["path"]
    if "/" not in rest:
        return _dir_id(info["repo"], common) + "#files", "files in " + _folder_label(m, within.module, common)
    sub = (common + "/" if common else "") + rest.split("/", 1)[0]
    return _dir_id(info["repo"], sub), _folder_label(m, within.module, sub)


def _entries(m: Model, fns: list, tests: int) -> dict:
    kinds = Counter(m.entry_kind[f] for f in fns if f in m.entry_kind)
    out: dict = {}
    if kinds:
        out["kinds"] = dict(kinds.most_common())
        shown = [f for f in fns if m.entry_kind.get(f) in ("route", "command", "entry", "message")]
        shown.sort(key=lambda f: (-m.flows.get(f, 0), f))
        if shown:
            out["examples"] = [{"id": f, "name": m.route.get(f) or (m.files[m.file_of[f]]["path"] if m.name.get(f) in TOP
                                                                    else _qual(m, f)), "kind": m.entry_kind[f]}
                               for f in shown[:3]]
    out["tests"] = tests
    return out


def _entry_text(e: dict) -> str:
    bits = [f"{n:,} {ENTRY_WORDS[k][n != 1]}" for k, n in e.get("kinds", {}).items()]
    if e.get("tests"):
        bits.append(f"{e['tests']:,} test{'s' if e['tests'] != 1 else ''}")
    return ", ".join(bits) or "none"


def describe(con, m: Model, p: Part, owner: dict, within: Part, given: dict, depth: int) -> dict:
    """One part as the outline shows it. `owner` maps each file of the level to its sibling part (id, label)."""
    files = set(p.files)
    fns = [f for x in p.files for f in m.functions.get(x, ())]
    tests = sum(m.tests.get(x, 0) for x in p.files)
    g = given.get(p.id)
    out: dict = {"id": p.id, "kind": p.kind, "name": g["name"] if g else p.label}
    if g:
        out["named"] = True
        if g["name"] != p.label:
            out["label"] = p.label
        if g.get("summary"):
            out["summary"] = g["summary"]
        for k, to in (("by", "named_by"), ("from", "named_as"), ("stale", "stale")):
            if g.get(k):
                out[to] = g[k]
    if p.path and p.kind in ("folder", "files", "system", "other", "module"):
        out["path"] = p.path + "/"
    out["size"] = {"files": len(files), "functions": len(fns), "lines": m.lines(files)}
    out["entry_points"] = _entries(m, fns, tests)
    if fns:
        out["on_a_test_path"] = f"{sum(1 for f in fns if f in m.tested):,} of {len(fns):,} functions"
    uses, used_by = Counter(), Counter()
    label: dict = {}
    for x in files:
        for side, acc in ((m.fout.get(x, {}), uses), (m.fin.get(x, {}), used_by)):
            for y, n in side.items():
                if y in files:
                    continue
                k, lab = owner.get(y) or _outer(m, y, within)
                label[k] = lab
                acc[k] += n
    for k in label:   # a part elsewhere that has a name is called by it
        if k in given:
            label[k] = given[k]["name"]
    out["uses"] = [{"id": k, "name": label[k], "links": n} for k, n in uses.most_common(6)]
    out["used_by"] = [{"id": k, "name": label[k], "links": n} for k, n in used_by.most_common(6)]
    if len(uses) > 6 or len(used_by) > 6:
        out["links_total"] = {"uses": sum(uses.values()), "used_by": sum(used_by.values())}
    chans: dict = {}
    for fs, fd, ch, addr in {c for x in files for c in m.channels.get(x, ())}:
        a, b = fs in files, fd in files
        if a == b:
            continue
        other = fd if a else fs
        k, lab = owner.get(other) or _outer(m, other, within)
        c = chans.setdefault((ch, "to" if a else "from", k), {"channel": ch, "direction": "to" if a else "from",
                                                              "other": given.get(k, {}).get("name", lab), "links": 0,
                                                              "example": addr})
        c["links"] += 1
    if chans:
        out["channels"] = sorted(chans.values(), key=lambda c: (-c["links"], c["channel"], c["other"]))[:6]
    busy = sorted((f for f in fns if m.flows.get(f)), key=lambda f: (-m.flows[f], f))[:4]
    if busy:
        out["busiest"] = [{"id": f, "name": _qual(m, f), "flows": m.flows[f]} for f in busy]
    # The types other code leans on most, one of each name (every component has its own Props).
    types, seen = [], set()
    for u in sorted({u for x in p.files for u in m.units_of_file.get(x, ()) if m.kind.get(u) == "type"},
                    key=lambda u: (-sum(m.uadj.get(u, {}).values()), -m.lines_of.get(u, 0), u)):
        if m.name[u] not in seen and len(types) < 4:
            seen.add(m.name[u])
            types.append(u)
    if types:
        out["key_types"] = [{"id": t, "name": m.name[t]} for t in types]
    if p.around:
        out["around"] = p.around
    if p.kind == "more":
        out["holds"] = [k.label for k in p.rest]
    if depth > 1:
        kids, _, _ = split(m, p)
        if kids:
            kg = names(con, kids)
            out["parts"] = [{"id": k.id, "name": kg[k.id]["name"] if k.id in kg else k.label, "files": len(k.files)}
                            for k in kids]
            if depth > 2:
                for item, k in zip(out["parts"], kids):
                    sub, _, _ = split(m, k)
                    if sub:
                        sg = names(con, sub)
                        item["parts"] = [{"id": s.id, "name": sg[s.id]["name"] if s.id in sg else s.label,
                                          "files": len(s.files)} for s in sub]
    out["drill"] = f'module_outline("{p.id}")'
    return out


def _qual(m: Model, f: str) -> str:
    if m.name.get(f) in TOP and f in m.file_of:
        return "top level of " + m.files[m.file_of[f]]["path"].rsplit("/", 1)[-1]
    u = m.unit.get(f)
    if u and u != f and m.kind.get(u) == "type" and m.name.get(f) != m.name.get(u):
        return f"{m.name[u]}.{m.name.get(f, f)}"
    return m.name.get(f, f)


def _risks(parts: list) -> Optional[str]:
    """What makes a part risky beside its siblings: the busiest, the most depended on, those few tests reach."""
    if len(parts) < 3:
        return None
    load = [sum(b["flows"] for b in p.get("busiest", ())) for p in parts]
    deps = [sum(u["links"] for u in p.get("used_by", ())) for p in parts]
    for p, l, d in zip(parts, load, deps):
        r = []
        if l and l == max(load):
            r.append("busy: its functions are on the most flows")
        if d and d == max(deps):
            r.append("many dependents: the most links into it")
        fn = p["size"]["functions"]
        if fn >= 5:
            on = int(p["on_a_test_path"].split(" of ")[0].replace(",", ""))
            if on < 0.3 * fn:
                r.append(f"few tests: {on} of {fn} functions are on a test's path")
        if r:
            p["risky"] = r
    few = [p for p in parts if any(x.startswith("few tests") for x in p.get("risky", ()))]
    if len(few) >= 0.8 * len(parts):
        for p in few:   # said once for the level rather than on every part
            p["risky"] = [x for x in p["risky"] if not x.startswith("few tests")]
            if not p["risky"]:
                del p["risky"]
        return "Few tests reach any of these parts: most of their functions are on no test's path."
    return None


def _leaf(con, m: Model, p: Part, given: dict) -> dict:
    """A file, a type or a group with one file: its key types and functions, most flows through them first."""
    within = Part(p.id, p.kind, p.label, p.files, posixpath.dirname(p.path) if p.kind in ("file", "type") else p.path,
                  p.module, p.repo)
    out = describe(con, m, p, {}, within, given, 1)
    out.pop("drill", None)
    if p.kind == "type":
        members = [i for i in m.functions.get(p.files[0], ()) if m.unit.get(i) == p.node] if p.files else []
    else:
        members = [i for f in p.files for i in m.functions.get(f, ())]
    key = sorted(members, key=lambda i: (-m.flows.get(i, 0), -m.lines_of.get(i, 0), i))[:12]
    out["key"] = [{"id": i, "name": _qual(m, i), "flows": m.flows.get(i, 0), "lines": m.lines_of.get(i, 0),
                   **({"entry": m.entry_kind[i]} if i in m.entry_kind else {}),
                   **({"tested": True} if i in m.tested else {})} for i in key]
    if len(p.files) > 1:
        out["files"] = {"total": len(p.files), "items": [m.files[f]["path"] for f in p.files[:15]]}
    return out


def outline(con, part_id: Optional[str] = None, depth: int = 2) -> dict:
    """One level of a module (or of the repository, with no part): its parts, described."""
    depth = max(1, min(int(depth or 2), 3))
    m = model(con)
    p = find(m, part_id)
    if p is None:
        return {"error": f"no module or part {part_id!r} on the map. Call module_outline() with no module to see the"
                         " modules, or give a module's path (such as editor/core) or a part id from an outline."}
    given = names(con, [p])
    kids, how, shown_from = split(m, p)
    head = {"id": p.id, "kind": p.kind, "name": given[p.id]["name"] if p.id in given else p.label}
    if p.id in given:
        head["named"] = True
        if given[p.id].get("summary"):
            head["summary"] = given[p.id]["summary"]
        if given[p.id].get("stale"):
            head["stale"] = given[p.id]["stale"]
    if p.path:
        head["path"] = p.path + ("/" if p.kind not in ("file", "type") else "")
    fns = sum(len(m.functions.get(f, ())) for f in p.files)
    head["size"] = {"files": len(p.files), "functions": fns, "lines": m.lines(p.files)}
    if not kids:
        out = {**head, **{k: v for k, v in _leaf(con, m, p, given).items() if k not in head}}
        out["next"] = ("Read the key code (`source` with an id from key) before saying what it does; name this part"
                       f" with name_part(\"{p.id}\", name, summary, evidence).")
        return out
    owner = {}
    for k in kids:
        for x in (k.rest if k.kind == "more" else [k]):   # past the first page, each part is still itself
            for f in x.files:
                owner[f] = (x.id, x.label)
    given = {**{r["id"]: {"name": r["name"]} for r in _rows(con).values()}, **names(con, kids), **given}
    for f, (k, lab) in list(owner.items()):
        if k in given:
            owner[f] = (k, given[k]["name"])
    within = Part(p.id, p.kind, p.label, p.files, shown_from or p.path, p.module, p.repo)
    parts = [describe(con, m, k, owner, within, given, depth) for k in kids]
    note = _risks(parts)
    named = sum(1 for x in parts if x.get("named"))
    out = {**head, "split": how}
    if shown_from:
        out["shown_from"] = shown_from + "/"
    out["names"] = f"{named} of {len(parts)} parts {'has' if named == 1 else 'have'} a name"
    if note:
        out["note"] = note
    out["parts"] = parts
    out["next"] = ("Drill into a part with module_outline(\"<part id>\") (CLI: leyline outline <part id>). Read a part's"
                   " key code (`source`, `context`), then name it with name_part(part_id, name, summary, evidence).")
    return out


# -- text for the command line ----------------------------------------------------------------------------------------
def _n(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _size(s: dict) -> str:
    return (f"{s['files']:,} file{'s' if s['files'] != 1 else ''}, {s['functions']:,} function"
            f"{'s' if s['functions'] != 1 else ''}, {s['lines']:,} line{'s' if s['lines'] != 1 else ''}")


def _links(xs: list) -> str:
    return ", ".join(f"{x['name']} ({x['links']:,})" for x in xs)


def _part_lines(p: dict, n: Optional[int], pad: str = "   ") -> list:
    name = p["name"] + (f" ({p['label']})" if p.get("label") else "")
    tag = f"  [{p['kind']}]" if p["kind"] not in ("folder", "module", "repo") else ""
    L = [(f"{n}. " if n else "") + f"{name}{tag}: {_size(p['size'])}", f"{pad}id: {p['id']}"]
    if p.get("summary"):
        L.append(f"{pad}{p['summary']}")
    if p.get("stale"):
        L.append(f"{pad}name {p['stale']}")
    if p.get("named_as"):
        L.append(f"{pad}named as {p['named_as']}, which shared most of its files")
    e = p.get("entry_points") or {}
    if e:
        L.append(f"{pad}entry points: {_entry_text(e)}" + (f"; {p['on_a_test_path']} on a test's path"
                                                          if p.get("on_a_test_path") else ""))
        if e.get("examples"):
            L.append(f"{pad}  e.g. " + ", ".join(x["name"] for x in e["examples"]))
    if p.get("around"):
        L.append(f"{pad}around: {', '.join(p['around'])}")
    if p.get("uses"):
        L.append(f"{pad}uses: {_links(p['uses'])}")
    if p.get("used_by"):
        L.append(f"{pad}used by: {_links(p['used_by'])}")
    for c in p.get("channels", ()):
        L.append(f"{pad}channel: {c['channel']} {c['direction']} {c['other']}, {c['links']} link{'s' if c['links'] != 1 else ''}"
                 + (f" (e.g. {c['example']})" if c.get("example") else ""))
    if p.get("busiest"):
        L.append(f"{pad}busiest: " + ", ".join(f"{b['name']} ({_n(b['flows'], 'flow')})" for b in p["busiest"][:3]))
    if p.get("key_types"):
        L.append(f"{pad}key types: " + ", ".join(t["name"] for t in p["key_types"]))
    if p.get("risky"):
        L.append(f"{pad}risky: " + "; ".join(p["risky"]))
    if p.get("holds"):
        L.append(f"{pad}holds: " + ", ".join(p["holds"][:10]) + (" and more" if len(p["holds"]) > 10 else ""))
    if p.get("parts"):
        L.append(f"{pad}parts: " + ", ".join(f"{x['name']} ({x['files']} file{'s' if x['files'] != 1 else ''})"
                                              for x in p["parts"][:8])
                 + (f" and {len(p['parts']) - 8} more" if len(p["parts"]) > 8 else ""))
    return L


def text(r: dict) -> str:
    if "error" in r:
        return r["error"]
    L = [f"{r['name']} ({r['kind']}" + (f", {r['path']}" if r.get("path") and r["path"] != r["name"] else "")
         + f"): {_size(r['size'])}."]
    if r.get("summary"):
        L.append(r["summary"])
    if r.get("stale"):
        L.append(f"Its name {r['stale']}.")
    if "parts" in r and "split" in r:
        L.append(f"Split by {r['split']}" + (f" (shown from {r['shown_from']}, the folder that holds nearly all of it)"
                                             if r.get("shown_from") else "") + f". {r['names']}."
                 + (f" {r['note']}" if r.get("note") else ""))
        L.append("")
        for k, p in enumerate(r["parts"], 1):
            L += _part_lines(p, k)
    else:
        L += _part_lines({**r, "name": "This part"}, None, "")[2:]
        if r.get("files"):
            f = r["files"]
            L.append("Files: " + ", ".join(x.rsplit("/", 1)[-1] for x in f["items"])
                     + (f" and {f['total'] - len(f['items'])} more" if f["total"] > len(f["items"]) else ""))
        if r.get("key"):
            L.append("Key types and functions:")
            for x in r["key"]:
                L.append(f"  {x['name']}  {_n(x['flows'], 'flow')}, {_n(x['lines'], 'line')}" + (f", {x['entry']}" if x.get("entry") else "")
                         + ("" if x.get("tested") else ", on no test's path") + f"  [{x['id']}]")
    name = f"`leyline name-part {r['id'] if 'key' in r else '<part id>'} \"<name>\" --summary \"<one line>\" --evidence <node ids>`"
    if "key" in r:
        L += ["", f"Read the key code (`leyline source <id>`) before saying what it does; then name this part: {name}."]
    else:
        L += ["", "Drill into a part: `leyline outline <part id>`. Read a part's key code (`leyline source <id>`, `leyline"
                  f" context <name>`; `--json` gives the ids), then name it: {name}."]
    return "\n".join(L)


# -- for the map page ---------------------------------------------------------------------------------------------------
MAP_MIN_FILES = 30   # a module this large is drawn as its parts
MAP_MOST = 40        # the largest this many such modules


def map_parts(con) -> list[dict]:
    """For each large module: its parts two levels down, each with its files, name and size, for export.py."""
    m = model(con)
    big = sorted((k for k in m.modules if len(m.files_of_module.get(k, ())) >= MAP_MIN_FILES),
                 key=lambda k: (-len(m.files_of_module[k]), k))[:MAP_MOST]
    out = []
    for mod in big:
        kids, _, _ = split(m, module_part(m, mod))
        if len(kids) < 2:
            continue
        given = names(con, kids)

        def item(k, g):
            fns = sum(len(m.functions.get(f, ())) for f in k.files)
            x = {"id": k.id, "kind": k.kind, "name": g[k.id]["name"] if k.id in g else k.label, "label": k.label,
                 "files": k.files, "functions": fns, "lines": m.lines(k.files)}
            if k.id in g:
                x["named"] = True
                for key in ("summary", "stale"):
                    if g[k.id].get(key):
                        x[key] = g[k.id][key]
            return x
        parts = []
        for k in kids:
            x = item(k, given)
            sub, _, _ = split(m, k)
            if len(sub) >= 2 and len(k.files) >= 2:
                sg = names(con, sub)
                x["parts"] = [item(s, sg) for s in sub]
            parts.append(x)
        out.append({"module": mod, "parts": parts})
    return out
