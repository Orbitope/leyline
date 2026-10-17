"""Godot adapter: a scene or resource (`.tscn`, `.tres`) as a file that names the scripts it runs, the scenes it
includes and the methods its signal connections call. The format is lines of `[section key=value ...]` followed by
`property = value` lines, read here line by line in a stream, never as a whole; the indexer ties the names to code
(Indexer._resolve_assets). Nothing is executed and no engine is needed."""

from __future__ import annotations

import io
import re

from ..model import AssetRef, FileResult

NAME = "godot-scene"
VERSION = "0.1"
LANGUAGE = "godot-scene"
EXTENSIONS = (".tscn", ".tres")
ASSET = True

_SECTION = re.compile(rb"\[(\w+)(.*)\]\s*$")
_ATTR = re.compile(rb'(\w+)=(?:"((?:[^"\\]|\\.)*)"|(\w+\([^)]*\))|([^\s\]]+))')
_RESOURCE_ID = re.compile(rb'ExtResource\(\s*"?([^")\s]+)"?\s*\)')
_SCRIPT_LINE = re.compile(rb"script\s*=\s*(.*)")


def _attrs(text: bytes) -> dict:
    out = {}
    for m in _ATTR.finditer(text):
        out[m.group(1).decode()] = (m.group(2) if m.group(2) is not None else m.group(3) or m.group(4)).decode("utf8", "replace")
    return out


def _ext_id(value: str) -> str:
    m = _RESOURCE_ID.search(value.encode())
    return m.group(1).decode() if m else ""


def parse(repo: str, rel_path: str, file_id: str, src: bytes, module: str = "") -> FileResult:
    res = FileResult()
    ext: dict[str, tuple[str, str]] = {}   # resource id -> (type, path)
    node_script: dict[str, str] = {}        # node path within the scene -> resource id of its script
    node_kind: dict[str, str] = {}          # node path -> "instance" or "built-in"
    connections: list[tuple[str, str, int]] = []
    seen: set = set()
    section, node = "", None
    root = None

    def add(ref: AssetRef) -> None:
        key = (ref.what, ref.target, ref.owner)
        if key not in seen:
            seen.add(key)
            res.assets.append(ref)

    for n, raw in enumerate(io.BytesIO(src), 1):
        line = raw.strip()
        if not line or line[:1] == b";":
            continue
        if line[:1] == b"[":
            m = _SECTION.match(line)
            if not m:
                continue   # a value that spans lines, or something that is not a header
            section, a = m.group(1).decode(), _attrs(m.group(2))
            node = None
            if section == "ext_resource":
                ext[a.get("id", "")] = (a.get("type", ""), a.get("path", ""))
                kind, path = ext[a.get("id", "")]
                if path.startswith("res://") and kind == "Script":
                    add(AssetRef("script", path, line=n))
                elif path.startswith("res://") and (kind == "PackedScene" or path.endswith(".tscn")):
                    add(AssetRef("include", path, line=n))
            elif section == "node":
                name, parent = a.get("name", ""), a.get("parent")
                if parent is None:
                    node = root = "."
                else:
                    node = name if parent == "." else f"{parent}/{name}"
                node_kind[node] = "instance" if "instance" in a else "built-in"
            elif section == "connection":
                to, method = a.get("to", ""), a.get("method", "")
                if method:
                    connections.append((to, method, n))
            continue
        if node is not None and line.startswith(b"script"):
            m = _SCRIPT_LINE.match(line)
            if m:
                rid = _ext_id(m.group(1).decode("utf8", "replace"))
                if rid:
                    node_script[node] = rid
    for to, method, n in connections:
        script = ext.get(node_script.get(to, ""), ("", ""))[1]
        if script:
            add(AssetRef("method", method, owner=script, line=n))
        else:   # a node with no script of its own: a method of its engine class, unless it is another scene's
            add(AssetRef("method", method, builtin=node_kind.get(to) == "built-in", line=n))
    return res
