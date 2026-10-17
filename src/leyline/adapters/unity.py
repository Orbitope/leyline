"""Unity adapter: a scene, prefab or asset (`.unity`, `.prefab`, `.asset`) in Unity's text serialization, as a file
that names the scripts its components run (by GUID), the prefabs it includes and the methods its persistent event
calls name. Read line by line in a stream, not as YAML: a scene can be tens of MB and only a few keys matter. The
indexer ties the GUIDs to the `.meta` files and the names to code (Indexer._resolve_assets)."""

from __future__ import annotations

import io
import re

from ..model import AssetRef, FileResult

NAME = "unity-yaml"
VERSION = "0.1"
LANGUAGE = "unity-yaml"
EXTENSIONS = (".unity", ".prefab", ".asset")
ASSET = True

_DOC = re.compile(rb"--- !u!(\d+) &(-?\d+)")
_GUID = re.compile(rb"guid:\s*([0-9a-f]{32})")
_FILE_ID = re.compile(rb"fileID:\s*(-?\d+)")
_NULL_GUID = ("00000000000000000000000000000000", "0000000000000000e000000000000000", "0000000000000000f000000000000000")
BUILTIN_PREFIXES = ("UnityEngine.", "UnityEditor.", "TMPro.")
_PREFAB_KEYS = (b"m_SourcePrefab:", b"m_ParentPrefab:")


def parse(repo: str, rel_path: str, file_id: str, src: bytes, module: str = "") -> FileResult:
    res = FileResult()
    seen: set = set()
    doc = ""                               # fileID of the document being read
    script_of: dict[str, str] = {}         # fileID of a component -> the GUID of the script it runs
    calls: list[tuple] = []                # (target fileID, target GUID, assembly type, method, line)
    pending = None                         # the persistent call being read: [fileID, guid, type]

    def add(ref: AssetRef) -> None:
        key = (ref.what, ref.target, ref.owner)
        if key not in seen:
            seen.add(key)
            res.assets.append(ref)

    for n, raw in enumerate(io.BytesIO(src), 1):
        line = raw.strip()
        if line[:2] == b"- ":   # the first key of a list item
            line = line[2:]
        if line[:7] == b"--- !u!":
            m = _DOC.match(line)
            doc = m.group(2).decode() if m else ""
            pending = None
            continue
        if line[:9] == b"m_Script:":
            m = _GUID.search(line)
            if m and m.group(1).decode() not in _NULL_GUID:
                script_of[doc] = m.group(1).decode()
                add(AssetRef("script", script_of[doc], line=n))
        elif line.startswith(_PREFAB_KEYS):
            m = _GUID.search(line)
            if m and m.group(1).decode() not in _NULL_GUID:
                add(AssetRef("include", m.group(1).decode(), line=n))
        elif line[:9] == b"m_Target:":
            f, g = _FILE_ID.search(line), _GUID.search(line)
            pending = [f.group(1).decode() if f else "", g.group(1).decode() if g else "", ""]
        elif pending is not None:
            if line[:25] == b"m_TargetAssemblyTypeName:":
                pending[2] = line[25:].strip().decode("utf8", "replace")
            elif line[:13] == b"m_MethodName:":
                method = line[13:].strip().decode("utf8", "replace").strip("'\"")
                if method and pending[0] not in ("", "0"):
                    calls.append((pending[0], pending[1], pending[2], method, n))
                pending = None
    for fid, guid, typename, method, n in calls:
        if guid:   # a component of another file's prefab: any script that file runs may be the one
            add(AssetRef("method", method, owner=f"in:{guid}", line=n))
        elif fid in script_of:
            add(AssetRef("method", method, owner=script_of[fid], line=n))
        else:
            add(AssetRef("method", method, builtin=typename.startswith(BUILTIN_PREFIXES), line=n))
    return res
