"""Language-neutral records that adapters emit and the indexer resolves.

The records have slots: a large repository holds millions of them at once, and without a dict each one is
about half the size."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class Node:
    id: str
    kind: str  # repo, module, file, type, callable, field, entry_point, external, test
    name: str
    parent_id: Optional[str] = None
    language: Optional[str] = None
    path: Optional[str] = None
    span_start: Optional[int] = None  # 1-based line
    span_end: Optional[int] = None
    content_hash: Optional[str] = None
    attrs: dict = field(default_factory=dict)


@dataclass(slots=True)
class Edge:
    kind: str
    src_id: str
    dst_id: str
    precision: str = "exact"  # exact | heuristic | observed
    attrs: dict = field(default_factory=dict)


@dataclass(slots=True)
class TypeRef:
    """A use of a type by name, to be resolved against the workspace."""

    src_id: str
    names: list[str]  # every identifier in the type expression, e.g. List<Foo> -> [List, Foo]
    role: str  # param | return | field_type | local | base | instantiate
    line: Optional[int] = None


@dataclass(slots=True)
class CallSite:
    """An unresolved call, with whatever the adapter could tell about its receiver."""

    src_id: str  # enclosing callable
    name: str  # method or function name
    receiver: Optional[str]  # None (bare call), 'this', a type name, or a variable name
    receiver_type: Optional[str]  # declared type of the receiver if the adapter inferred it
    argc: int
    line: int
    enclosing_type: Optional[str] = None  # id of the type the caller lives in
    col: int = 0
    args: tuple = ()  # per argument: a lambda's parameter count (int), a known type name (str), or None
    targs: int = 0  # explicit type arguments on the call: M<int>() has 1
    chain: Optional["CallSite"] = None  # the call whose result this one is made on: a.Make().Run()
    ref: bool = False  # not a call: the function is handed over by name (onClick={save}) for someone else to call


@dataclass(slots=True)
class EventUse:
    """A place where an event is raised or subscribed to."""

    kind: str  # raise | subscribe
    src_id: str  # enclosing callable
    event: str  # event member name
    receiver: Optional[str]  # as in CallSite; None means the enclosing type's own event
    receiver_type: Optional[str]
    handler: Optional[str]  # method name for a method-group handler, None for a lambda
    line: int
    enclosing_type: Optional[str] = None


@dataclass(slots=True)
class FieldUse:
    """A place where a member that may be a field is read or assigned."""

    src_id: str  # enclosing callable
    name: str
    receiver: Optional[str]  # as in CallSite; None means a bare name
    receiver_type: Optional[str]
    access: str  # r | w | rw | i (set while the object is being created: new Foo { a = 1 })
    line: int
    enclosing_type: Optional[str] = None
    chain: Optional[CallSite] = None


@dataclass(slots=True)
class Endpoint:
    """One end of a channel that is not a call: an HTTP route or request, a file read or written, a registration
    in a dependency-injection container, a message published or handled, a table read or written, an RPC."""

    channel: str  # http | file | di | queue | db | rpc
    role: str  # serve | call | write | read; di: provide | inject | depend | start; queue: publish | subscribe; db: table
    src_id: str
    address: str  # the route or request path (empty for files), the topic, table, service, or service/method
    line: int
    method: Optional[str] = None  # the HTTP method; for the other channels, the shape that was recognised
    literals: list[str] = field(default_factory=list)  # string literals in the call; di: the implementation
    handler: Optional[str] = None  # the function a subscription or task names, as written


@dataclass(slots=True)
class Spawn:
    """A place where another program is launched."""

    src_id: str
    strings: list[str]  # string literals in the launch call, constants resolved
    file_strings: list[str]  # every other string literal in the file, as a weaker hint
    pipes: bool  # the launcher wires up stdin or stdout
    line: int


@dataclass(slots=True)
class ImportRef:
    src_id: str  # file id
    target: str  # namespace or module path as written
    symbols: list[str] = field(default_factory=list)
    alias: Optional[str] = None
    is_static: bool = False


@dataclass(slots=True)
class FileResult:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    type_refs: list[TypeRef] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    events: list[EventUse] = field(default_factory=list)
    field_uses: list[FieldUse] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    path_strings: dict[str, list[str]] = field(default_factory=dict)  # function -> path-like literals in its body
    spawns: list[Spawn] = field(default_factory=list)
    # Namespaces (C#) or module paths (Python) this file declares.
    declares: list[str] = field(default_factory=list)
