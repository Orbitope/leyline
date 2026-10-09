"""The skills that ship with Leyline: one folder each, with a SKILL.md that tells a coding agent how to do one job
with the map (answer a question, plan a change, review a pull request).

The folders under `skills/` at the top of the repository are the only copy kept by hand. A wheel carries them as
`leyline/skills/` (pyproject.toml's force-include), so an installed Leyline finds them beside this module; a checkout
finds the top-level folder. A new folder there is picked up by the CLI, the MCP server and the wheel with no other
edit.

`install` copies them into a repository's `.claude/skills/` or `.agents/skills/` and records each file's hash in
`.leyline-skills.json` there, so a later install updates a copy nobody edited and leaves an edited one alone."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__, store

HERE = Path(__file__).resolve().parent
MANIFEST = ".leyline-skills.json"
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
TARGETS = {"claude": ".claude/skills", "agents": ".agents/skills"}


def skills_dir() -> Optional[Path]:
    """Where the skills are: beside this module in an installed wheel, else `skills/` at the top of a checkout."""
    for d in (HERE / "skills", HERE.parents[1] / "skills"):
        if d.is_dir() and any(d.glob("*/SKILL.md")):
            return d
    return None


def frontmatter(text: str) -> tuple[dict, str]:
    """The `key: value` lines of a SKILL.md's frontmatter, and the text after it. Only the flat form skills use."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 3)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            meta[key.strip()] = value
    return meta, text[end + 5:].lstrip("\n")


@dataclass
class Skill:
    folder: Path
    meta: dict
    body: str

    @property
    def name(self) -> str:
        return self.meta.get("name") or self.folder.name

    @property
    def description(self) -> str:
        return self.meta.get("description", "")

    @property
    def text(self) -> str:
        return (self.folder / "SKILL.md").read_text(encoding="utf-8")

    def files(self) -> dict[str, bytes]:
        """Every file of the skill, by its path inside the folder."""
        return {p.relative_to(self.folder).as_posix(): p.read_bytes() for p in sorted(self.folder.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts and p.name != MANIFEST}


def read(folder: Path) -> Skill:
    meta, body = frontmatter((folder / "SKILL.md").read_text(encoding="utf-8"))
    return Skill(folder, meta, body)


def available() -> list[Skill]:
    d = skills_dir()
    return [read(f.parent) for f in sorted(d.glob("*/SKILL.md"))] if d else []


def find(name: str) -> Optional[Skill]:
    """A skill by its name, with or without the `leyline-` in front."""
    by = {s.name: s for s in available()}
    return by.get(name) or by.get("leyline-" + name)


def problems(s: Skill) -> list[str]:
    """What is wrong with a skill's frontmatter, as an agent reads it: none when it is right."""
    out = []
    if not s.meta:
        return ["SKILL.md does not start with frontmatter (--- name: ... description: ... ---)"]
    name, desc = s.meta.get("name", ""), s.meta.get("description", "")
    if not NAME.match(name) or len(name) > 64:
        out.append(f"name {name!r} is not lower case words joined by hyphens, 64 characters at most")
    if name != s.folder.name:
        out.append(f"name {name!r} is not the folder's name, {s.folder.name!r}")
    if not desc or len(desc) > 1024:
        out.append("description is missing or longer than 1,024 characters")
    if "<" in desc or ">" in desc:
        out.append("description has < or >, which agents may read as markup")
    if not s.body.startswith("# "):
        out.append("the text after the frontmatter does not open with a # heading")
    return out


# -- installing into a repository ------------------------------------------------------------------
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def repo_root(start: Path) -> Path:
    """The repository around `start` (the nearest folder with .git), else `start` itself."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return start


def targets(root: Path, which: Optional[str]) -> list[dict]:
    """The skills folders to install into: `claude`, `agents` or `both`; by default both when the repository has an
    .agents folder, else .claude. A folder that is a link to another one listed is written once."""
    which = which or ("both" if (root / ".agents").is_dir() else "claude")
    out: list[dict] = []
    for key in (("claude", "agents") if which == "both" else (which,)):
        path = root / TARGETS[key]
        same = next((t for t in out if t["path"].resolve() == path.resolve()), None)
        if same and same["path"].is_symlink() and not path.is_symlink():   # name the real folder, and the link to it
            same["also"].append(same["label"])
            same["path"], same["label"] = path, TARGETS[key]
        elif same:
            same["also"].append(TARGETS[key])
        else:
            out.append({"path": path, "label": TARGETS[key], "also": []})
    return out


def _manifest(folder: Path) -> dict:
    try:
        m = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        return m if isinstance(m, dict) and isinstance(m.get("skills"), dict) else {"skills": {}}
    except (OSError, ValueError):
        return {"skills": {}}


def _copy(s: Skill, dest: Path, shipped: dict[str, bytes], drop: list[str]) -> None:
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    for rel in drop:   # files an earlier version shipped and this one does not, left as installed
        (dest / rel).unlink(missing_ok=True)
    for rel, data in shipped.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        store.write_file(dest / rel, data)
        shutil.copymode(s.folder / rel, dest / rel)


def install_one(s: Skill, folder: Path, manifest: dict, force: bool) -> dict:
    """Install one skill into one skills folder: installed, updated, unchanged, replaced (--force) or kept (edited
    here since it was installed, or not installed by Leyline)."""
    dest = folder / s.name
    shipped = s.files()
    want = {rel: _sha(b) for rel, b in shipped.items()}
    record = (manifest["skills"].get(s.name) or {}).get("files") or {}
    if not dest.exists() and not dest.is_symlink():
        state, drop = "installed", []
    elif not dest.is_dir() or dest.is_symlink():
        if not force:
            return {"skill": s.name, "state": "kept", "why": f"{dest.name} here is not a folder; --force replaces it"}
        state, drop = "replaced", []
    else:
        now = {rel: _sha((dest / rel).read_bytes()) if (dest / rel).is_file() else None for rel in {*want, *record}}
        gone = [rel for rel in record if rel not in want and now.get(rel) is not None]
        if all(now[rel] == want[rel] for rel in want) and not gone:
            state, drop = "unchanged", []
        else:
            edited = sorted(rel for rel in {*want, *record} if now.get(rel) != record.get(rel)) if record else []
            if record and not edited:
                state, drop = "updated", gone
            elif force:
                state, drop = "replaced", gone
            else:
                why = (f"edited here since it was installed ({', '.join(edited)})" if record else
                       "a different copy is here that Leyline did not install")
                return {"skill": s.name, "state": "kept", "why": why + "; --force replaces it"}
    if state != "unchanged":
        _copy(s, dest, shipped, drop)
    manifest["skills"][s.name] = {"files": want, "version": __version__}
    return {"skill": s.name, "state": state}


def install(root: Path, which: Optional[str] = None, force: bool = False, names: Optional[list[str]] = None) -> dict:
    """Copy the skills into the repository at `root`. Returns, per skills folder, what happened to each skill."""
    skills = available()
    if names:
        picked = [find(n) for n in names]
        missing = [n for n, s in zip(names, picked) if s is None]
        if missing:
            return {"error": f"no skill named {', '.join(missing)}. `leyline skills list` names them."}
        skills = [s for s in picked if s is not None]
    if not skills:
        return {"error": "this Leyline has no skills to install: its skills folder is missing."}
    out = []
    for t in targets(root, which):
        t["path"].mkdir(parents=True, exist_ok=True)
        manifest = _manifest(t["path"])
        results = [install_one(s, t["path"], manifest, force) for s in skills]
        manifest["leyline"] = __version__
        store.write_file(t["path"] / MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        out.append({"folder": t["label"], "path": str(t["path"]), "also": t["also"], "skills": results})
    return {"root": str(root), "targets": out}


# -- text for a person -----------------------------------------------------------------------------
EXAMPLES = {   # what a person might ask that each skill answers; the first four installed are shown after an install
    "leyline-ask": "who calls the save function, and how sure is that?",
    "leyline-explain-flow": "what happens when a user saves a file?",
    "leyline-explore-module": "what is in editor/host, and where do I start?",
    "leyline-quick-change": "make the retry count 3",
    "leyline-pr-review": "review this pull request",
    "leyline-spec": "plan adding an export button, with tests",
    "leyline-change-impact": "what would changing the validator's output format affect?",
    "leyline-adversarial-review": "stress-test the plan for loud-engine",
    "leyline-tour": "write a tour of the payment code for a new hire",
}


def list_text() -> str:
    skills = available()
    if not skills:
        return "This Leyline has no skills: its skills folder is missing."
    L = []
    for s in skills:
        L.append(s.name)
        L += textwrap.wrap(s.description, 100, initial_indent="  ", subsequent_indent="  ")
    L += ["", "Read one: `leyline skills show <name>`. Give them to your coding agent: `leyline skills install` in the"
              " repository."]
    return "\n".join(L)


def install_text(r: dict) -> str:
    words = {"installed": "installed", "updated": "updated", "unchanged": "up to date", "replaced": "replaced",
             "kept": "kept"}
    L = []
    for t in r["targets"]:
        also = f" ({', '.join(t['also'])} {'links' if len(t['also']) == 1 else 'link'} to it)" if t["also"] else ""
        L.append(f"{t['folder']}{also}:")
        for x in t["skills"]:
            L.append(f"  {words[x['state']]:<11} {x['skill']}" + (f": {x['why']}" if x.get("why") else ""))
    kept = [x["skill"] for t in r["targets"] for x in t["skills"] if x["state"] == "kept"]
    names = {x["skill"] for t in r["targets"] for x in t["skills"]}
    ask = [f"  \"{q}\" ({n})" for n, q in EXAMPLES.items() if n in names][:4]
    folders = " and ".join(f for t in r["targets"] for f in (t["folder"], *t["also"]))
    wrap = lambda s: textwrap.wrap(s, 100)
    L += ["", *wrap(f"Your agent reads skills from {folders} when a session starts: start a new one if one is open."
                    " Then ask in plain words, and it picks the skill. For example:"), *ask,
          "The skills use Leyline's MCP server. Connect it once, in the repository:",
          "  claude mcp add leyline -- leyline serve",
          "Without it, they fall back to the leyline command.",
          "Commit the folder if the rest of the team should have them too."]
    if kept:
        L.append(f"{len(kept)} kept as edited here; `leyline skills install --force` replaces them with Leyline's.")
    return "\n".join(L)


def start_here() -> str:
    """Where an agent connected to the server starts, for the server's instructions."""
    names = [s.name for s in available()]
    return ("\n\nStart here, by what the person wants: `map` once if nothing is mapped. To understand code, follow"
            " the leyline-ask skill (how something runs: leyline-explain-flow; what a part holds: leyline-explore-module)."
            " To change code: leyline-spec (`plan`) when there is a design to agree, leyline-quick-change (`quick`) for"
            " a small fix, leyline-pr-review (`review_pr`) for code someone else wrote. Then leyline-adversarial-review"
            " (`spec_review_facts`), the person decides each finding, the code is written, and `check` (or `quick` with"
            " done) gives the verdict. Each skill is an MCP prompt of the same name; the `skills` tool lists them and"
            " returns one's text." + (f" Skills: {', '.join(names)}." if names else ""))


def cli(args) -> int:
    """`leyline skills list | show <name> | install [names] [--to DIR] [--claude|--agents|--both] [--force]`."""
    import sys
    if args.action == "list":
        print(list_text())
        return 0 if available() else 1
    if args.action == "show":
        s = find(args.names[0]) if args.names else None
        if s is None:
            print(f"leyline: no skill named {args.names[0] if args.names else '(none given)'}. `leyline skills list`"
                  " names them.", file=sys.stderr)
            return 2
        print(s.text, end="")
        return 0
    root = Path(args.to) if args.to else repo_root(Path.cwd())
    if not root.is_dir():
        print(f"leyline: {root} is not a directory. Name the repository with --to.", file=sys.stderr)
        return 2
    r = install(root, args.where, args.force, args.names or None)
    if "error" in r:
        print(f"leyline: {r['error']}", file=sys.stderr)
        return 2
    print(f"Leyline's skills, into {root}:")
    print(install_text(r))
    return 0
