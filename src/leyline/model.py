"""Language-neutral records that adapters emit and the indexer resolves."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Node:
    id: str
    kind: str  # repo, module, file, type, callable, field, entry_point, external
    name: str
    parent_id: Optional[str] = None
    language: Optional[str] = None
    path: Optional[str] = None
    span_start: Optional[int] = None  # 1-based line
    span_end: Optional[int] = None
    content_hash: Optional[str] = None
    attrs: dict = field(default_factory=dict)


@dataclass
class Edge:
    kind: str
    src_id: str
    dst_id: str
    precision: str = "exact"  # exact | heuristic | observed
    attrs: dict = field(default_factory=dict)


@dataclass
class TypeRef:
    """A use of a type by name, to be resolved against the workspace."""

    src_id: str
    names: list[str]  # every identifier in the type expression, e.g. List<Foo> -> [List, Foo]
    role: str  # param | return | field_type | local | base | instantiate
    line: Optional[int] = None


@dataclass
class CallSite:
    """An unresolved call, with whatever the adapter could tell about its receiver."""

    src_id: str  # enclosing callable
    name: str  # method or function name
    receiver: Optional[str]  # None (bare call), 'this', a type name, or a variable name
    receiver_type: Optional[str]  # declared type of the receiver if the adapter inferred it
    argc: int
    line: int
    enclosing_type: Optional[str] = None  # id of the type the caller lives in


@dataclass
class ImportRef:
    src_id: str  # file id
    target: str  # namespace or module path as written
    symbols: list[str] = field(default_factory=list)
    alias: Optional[str] = None
    is_static: bool = False


@dataclass
class FileResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    type_refs: list[TypeRef] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    # Namespaces (C#) or module paths (Python) this file declares.
    declares: list[str] = field(default_factory=list)
