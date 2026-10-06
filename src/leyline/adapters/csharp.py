"""C# adapter: tree-sitter structure, with enough local type inference to resolve most calls."""

from __future__ import annotations

import re
from typing import Optional

import tree_sitter_c_sharp
from tree_sitter import Language, Parser

from ..model import CallSite, Edge, FileResult, ImportRef, Node, TypeRef

NAME = "tree-sitter-c-sharp"
VERSION = "0.1"
LANGUAGE = "csharp"
EXTENSIONS = (".cs",)

_parser = Parser(Language(tree_sitter_c_sharp.language()))

TYPE_DECLS = {
    "class_declaration": "class",
    "struct_declaration": "struct",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "record_struct_declaration": "record",
    "delegate_declaration": "delegate",
}
CALLABLE_DECLS = {
    "method_declaration", "constructor_declaration", "destructor_declaration",
    "operator_declaration", "conversion_operator_declaration", "local_function_statement",
}
ACCESS = ("public", "private", "protected", "internal")
LIFECYCLE = {"_Ready", "_Process", "_PhysicsProcess", "_Input", "_UnhandledInput", "_EnterTree",
             "_ExitTree", "_Draw", "_Notification"}


def _text(node) -> str:
    return node.text.decode("utf8", "replace") if node is not None else ""


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _modifiers(node) -> list[str]:
    return [_text(c) for c in node.children if c.type == "modifier"]


def _visibility(mods: list[str], default: str) -> str:
    found = [m for m in mods if m in ACCESS]
    return " ".join(found) if found else default


def _child(node, *types):
    for c in node.children:
        if c.type in types:
            return c
    return None


def _type_names(node) -> list[str]:
    """Every identifier in a type expression: List<Foo> -> [List, Foo]."""
    out: list[str] = []
    if node is None:
        return out
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":
            out.append(_text(n))
        elif n.type == "qualified_name":
            # A.B.C: the last segment is the type; keep it only.
            ids = [c for c in n.children if c.type in ("identifier", "generic_name")]
            if ids:
                stack.append(ids[-1])
        else:
            stack.extend(reversed(n.children))
    return out


SEQUENCES = {"List", "IList", "IEnumerable", "IReadOnlyList", "ICollection", "IReadOnlyCollection",
             "HashSet", "ISet", "Queue", "Stack", "LinkedList", "SortedSet", "ImmutableArray",
             "ImmutableList", "Span", "ReadOnlySpan"}


def _elem_type(node) -> Optional[str]:
    """Element type of a sequence or array type expression: List<Foo> and Foo[] both give Foo."""
    if node is None:
        return None
    names = _type_names(node)
    if not names:
        return None
    text = _text(node)
    if text.rstrip("?").endswith("]"):
        return names[0]
    if len(names) == 2 and names[0] in SEQUENCES:
        return names[1]
    return None


def _outer_type(node) -> Optional[str]:
    names = _type_names(node)
    return names[0] if names else None


class _Walker:
    def __init__(self, repo: str, rel_path: str, file_id: str, src: bytes, module: str):
        self.repo = repo
        self.path = rel_path
        self.file_id = file_id
        self.module = module
        self.res = FileResult()
        self.tree = _parser.parse(src)
        self.field_types: dict[str, dict[str, str]] = {}  # type id -> member name -> type name

    # -- ids -----------------------------------------------------------------
    def _qid(self, qualified: str) -> str:
        # The assembly is part of a .NET type's identity: two projects may both declare `Program`.
        return f"{self.repo}:csharp:{self.module}::{qualified}"

    # -- top level -----------------------------------------------------------
    def run(self) -> FileResult:
        root = self.tree.root_node
        globals_ = [c for c in root.children if c.type == "global_statement"]
        self._members(root, ns="", parent_id=self.file_id, type_id=None, qual="")
        if globals_:
            cid = self._qid(f"{self.path}::<top-level>")
            self.res.nodes.append(Node(
                id=cid, kind="callable", name="<top-level>", parent_id=self.file_id,
                language=LANGUAGE, path=self.path, span_start=globals_[0].start_point[0] + 1,
                span_end=globals_[-1].end_point[0] + 1,
                attrs={"signature": "top-level statements", "visibility": "internal",
                       "native_kind": "top_level"}))
            self.res.nodes.append(Node(
                id=cid + "#entry", kind="entry_point", name=f"{self.path} (top-level)",
                parent_id=self.file_id, language=LANGUAGE, path=self.path,
                attrs={"trigger": "cli", "address": self.path}))
            self.res.edges.append(Edge("exposes", cid + "#entry", cid))
            scope: dict[str, str] = {}
            for g in globals_:
                self._body(g, cid, None, scope)
        return self.res

    def _members(self, node, ns: str, parent_id: str, type_id: Optional[str], qual: str) -> None:
        for c in node.children:
            t = c.type
            if t == "using_directive":
                self._using(c)
            elif t in ("namespace_declaration", "file_scoped_namespace_declaration"):
                name = _text(c.child_by_field_name("name"))
                full = f"{ns}.{name}" if ns else name
                if full not in self.res.declares:
                    self.res.declares.append(full)
                body = c.child_by_field_name("body") or c
                self._members(body, full, parent_id, type_id, full)
            elif t in TYPE_DECLS:
                self._type(c, ns, parent_id, qual)
            elif t == "declaration_list":
                self._members(c, ns, parent_id, type_id, qual)

    def _using(self, node) -> None:
        raw = _squash(_text(node)).rstrip(";")
        raw = re.sub(r"^(global\s+)?using\s+", "", raw)
        is_static = raw.startswith("static ")
        raw = raw[7:] if is_static else raw
        alias = None
        if "=" in raw:
            alias, raw = [p.strip() for p in raw.split("=", 1)]
        self.res.imports.append(ImportRef(self.file_id, raw, alias=alias, is_static=is_static))

    # -- types ---------------------------------------------------------------
    def _type(self, node, ns: str, parent_id: str, qual: str) -> None:
        native = TYPE_DECLS[node.type]
        name = _text(node.child_by_field_name("name"))
        tparams = _child(node, "type_parameter_list")
        arity = len([c for c in tparams.children if c.type == "type_parameter"]) if tparams else 0
        qname = f"{qual}.{name}" if qual else name
        if arity:
            qname += f"`{arity}"
        tid = self._qid(qname)
        mods = _modifiers(node)
        body = node.child_by_field_name("body") or _child(node, "declaration_list", "enum_member_declaration_list")
        head_end = body.start_byte if body is not None else node.end_byte
        self.res.nodes.append(Node(
            id=tid, kind="type", name=name, parent_id=parent_id, language=LANGUAGE, path=self.path,
            span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
            attrs={"native_kind": native, "namespace": ns,
                   "visibility": _visibility(mods, "internal" if parent_id == self.file_id else "private"),
                   "is_abstract": "abstract" in mods or native == "interface",
                   "is_static": "static" in mods, "is_partial": "partial" in mods,
                   "signature": _squash(self.tree.root_node.text[node.start_byte:head_end].decode("utf8", "replace"))}))
        base_list = _child(node, "base_list")
        if base_list is not None:
            for b in base_list.children:
                if b.type in (",", ":"):
                    continue
                names = _type_names(b)
                if names:
                    self.res.type_refs.append(TypeRef(tid, names, "base", b.start_point[0] + 1))
        self.field_types.setdefault(tid, {})
        if native == "delegate":
            return
        if body is None:
            return
        # Record primary-constructor parameters as fields (records).
        plist = _child(node, "parameter_list")
        if plist is not None:
            for p in plist.children:
                if p.type == "parameter":
                    self._field_node(tid, _text(p.child_by_field_name("name")), p.child_by_field_name("type"),
                                     p, ["public"], "property")
        for m in body.children:
            mt = m.type
            if mt in TYPE_DECLS:
                self._type(m, ns, tid, qname)
            elif mt == "field_declaration" or mt == "event_field_declaration":
                decl = _child(m, "variable_declaration")
                if decl is None:
                    continue
                tnode = decl.child_by_field_name("type")
                for v in decl.children:
                    if v.type == "variable_declarator":
                        nm = v.child_by_field_name("name") or _child(v, "identifier")
                        self._field_node(tid, _text(nm), tnode, m, _modifiers(m),
                                         "event" if mt.startswith("event") else "field")
            elif mt in ("property_declaration", "indexer_declaration", "event_declaration"):
                nm = m.child_by_field_name("name")
                self._field_node(tid, _text(nm) if nm else "this[]", m.child_by_field_name("type"), m,
                                 _modifiers(m), mt.split("_")[0])
            elif mt == "enum_member_declaration":
                nm = m.child_by_field_name("name") or _child(m, "identifier")
                self.res.nodes.append(Node(
                    id=f"{tid}.{_text(nm)}", kind="field", name=_text(nm), parent_id=tid,
                    language=LANGUAGE, path=self.path, span_start=m.start_point[0] + 1,
                    span_end=m.end_point[0] + 1,
                    attrs={"native_kind": "enum_member", "visibility": "public", "is_mutable": False}))
        # Callables second, so every field type of this type is known to the body walk.
        for m in body.children:
            if m.type in CALLABLE_DECLS:
                self._callable(m, tid, tid, native == "interface")
            elif m.type in ("property_declaration", "indexer_declaration"):
                # Property bodies: attribute their calls to the property itself.
                nm = m.child_by_field_name("name")
                pid = f"{tid}.{_text(nm) if nm else 'this[]'}"
                self._body(m, pid, tid, {})

    def _field_node(self, tid, name, tnode, node, mods, native) -> None:
        if not name:
            return
        default_vis = "private"
        self.res.nodes.append(Node(
            id=f"{tid}.{name}", kind="field", name=name, parent_id=tid, language=LANGUAGE,
            path=self.path, span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
            attrs={"native_kind": native, "declared_type": _squash(_text(tnode)),
                   "type_name": _outer_type(tnode),
                   "visibility": _visibility(mods, default_vis), "is_static": "static" in mods or "const" in mods,
                   "is_mutable": not ("readonly" in mods or "const" in mods)}))
        self.res.edges.append(Edge("has_field", tid, f"{tid}.{name}"))
        names = _type_names(tnode)
        if names:
            self.res.type_refs.append(TypeRef(f"{tid}.{name}", names, "field_type", node.start_point[0] + 1))
            self.field_types.setdefault(tid, {})[name] = names[0]
            elem = _elem_type(tnode)
            if elem:
                self.field_types[tid][name + "[]"] = elem

    # -- callables -----------------------------------------------------------
    def _callable(self, node, parent_id: str, type_id: Optional[str], in_interface: bool,
                  outer_scope: Optional[dict] = None) -> None:
        t = node.type
        if t == "constructor_declaration":
            name = ".ctor"
        elif t == "destructor_declaration":
            name = ".dtor"
        elif t in ("operator_declaration", "conversion_operator_declaration"):
            op = node.child_by_field_name("operator")
            name = "operator " + (_text(op) if op else _squash(_text(node.child_by_field_name("type"))))
        else:
            name = _text(node.child_by_field_name("name"))
        params = node.child_by_field_name("parameters") or _child(node, "parameter_list")
        scope: dict[str, str] = dict(outer_scope or {})
        ptypes: list[str] = []
        required = 0
        plist = [p for p in params.children if p.type == "parameter"] if params is not None else []
        for p in plist:
            ptype = p.child_by_field_name("type")
            ptypes.append(_squash(_text(ptype)))
            pname = _text(p.child_by_field_name("name"))
            outer = _outer_type(ptype)
            if pname and outer:
                scope[pname] = outer
                elem = _elem_type(ptype)
                if elem:
                    scope[pname + "[]"] = elem
            has_default = any(c.type in ("equals_value_clause", "=") for c in p.children)
            is_params = any(_text(c) == "params" for c in p.children)
            if not has_default and not is_params:
                required += 1
        sep = "/" if t == "local_function_statement" else "."
        cid = f"{parent_id}{sep}{name}({','.join(ptypes)})"
        mods = _modifiers(node)
        body = node.child_by_field_name("body") or _child(node, "block", "arrow_expression_clause")
        head_end = body.start_byte if body is not None else node.end_byte
        ret = node.child_by_field_name("returns") or node.child_by_field_name("type")
        is_variadic = any(any(_text(c) == "params" for c in p.children) for p in plist)
        self.res.nodes.append(Node(
            id=cid, kind="callable", name=name, parent_id=parent_id, language=LANGUAGE, path=self.path,
            span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
            attrs={"signature": _squash(self.tree.root_node.text[node.start_byte:head_end].decode("utf8", "replace")).rstrip(";"),
                   "visibility": "public" if in_interface else _visibility(mods, "private"),
                   "is_static": "static" in mods, "is_async": "async" in mods,
                   "is_virtual": in_interface or any(m in mods for m in ("virtual", "abstract", "override")),
                   "native_kind": t.replace("_declaration", "").replace("_statement", ""),
                   "argc_min": required, "argc_max": 99 if is_variadic else len(plist),
                   "type_id": type_id}))
        for p, ptext in zip(plist, ptypes):
            names = _type_names(p.child_by_field_name("type"))
            if names:
                self.res.type_refs.append(TypeRef(cid, names, "param", p.start_point[0] + 1))
        if ret is not None:
            names = _type_names(ret)
            if names:
                self.res.type_refs.append(TypeRef(cid, names, "return", ret.start_point[0] + 1))
        if name == "Main" and "static" in mods:
            self._entry(cid, "cli", self.path)
        elif name in LIFECYCLE:
            self._entry(cid, "lifecycle", name)
        if body is not None:
            self._body(body, cid, type_id, scope)
        init = _child(node, "constructor_initializer")
        if init is not None:
            self._body(init, cid, type_id, scope)

    def _entry(self, cid: str, trigger: str, address: str) -> None:
        self.res.nodes.append(Node(
            id=cid + "#entry", kind="entry_point", name=cid.split(":", 2)[-1], parent_id=self.file_id,
            language=LANGUAGE, path=self.path, attrs={"trigger": trigger, "address": address}))
        self.res.edges.append(Edge("exposes", cid + "#entry", cid))

    # -- bodies --------------------------------------------------------------
    def _body(self, node, cid: str, type_id: Optional[str], scope: dict[str, str]) -> None:
        """Walk a body in source order, tracking local variable types and emitting call sites."""
        t = node.type
        if t == "local_function_statement":
            self._callable(node, cid, type_id, False, scope)
            return
        if t in TYPE_DECLS:
            return
        if t == "variable_declaration":
            tnode = node.child_by_field_name("type")
            declared = _outer_type(tnode) if tnode is not None and _text(tnode) != "var" else None
            if declared:
                self.res.type_refs.append(TypeRef(cid, _type_names(tnode), "local", node.start_point[0] + 1))
            for v in node.children:
                if v.type != "variable_declarator":
                    continue
                nm = _text(v.child_by_field_name("name") or _child(v, "identifier"))
                vt = declared or self._infer(v, scope, type_id)
                if nm and vt:
                    scope[nm] = vt
                elem = _elem_type(tnode) if declared else self._infer(v, scope, type_id, elem=True)
                if nm and elem:
                    scope[nm + "[]"] = elem
        elif t in ("foreach_statement",):
            tnode = node.child_by_field_name("type")
            nm = _text(node.child_by_field_name("left"))
            if tnode is not None and _text(tnode) != "var" and nm:
                outer = _outer_type(tnode)
                if outer:
                    scope[nm] = outer
            elif nm:
                elem = self._elem_of(node.child_by_field_name("right"), scope, type_id)
                if elem:
                    scope[nm] = elem
        elif t == "invocation_expression":
            self._invocation(node, cid, type_id, scope)
        elif t == "object_creation_expression":
            tnode = node.child_by_field_name("type")
            names = _type_names(tnode)
            args = node.child_by_field_name("arguments")
            argc = len([a for a in args.children if a.type == "argument"]) if args is not None else 0
            if names:
                self.res.type_refs.append(TypeRef(cid, names, "instantiate", node.start_point[0] + 1))
                self.res.calls.append(CallSite(cid, ".ctor", names[0], names[0], argc,
                                               node.start_point[0] + 1, type_id))
        for c in node.children:
            self._body(c, cid, type_id, scope)

    def _elem_of(self, expr, scope: dict[str, str], type_id: Optional[str]) -> Optional[str]:
        """Element type of a collection expression that is a local, a parameter or a field."""
        if expr is None:
            return None
        own = self.field_types.get(type_id or "", {})
        if expr.type == "identifier":
            return scope.get(_text(expr) + "[]") or own.get(_text(expr) + "[]")
        if expr.type == "member_access_expression":
            inner = expr.child_by_field_name("expression")
            if inner is not None and inner.type == "this_expression":
                return own.get(_text(expr.child_by_field_name("name")) + "[]")
        return None

    def _infer(self, declarator, scope: dict[str, str], type_id: Optional[str], elem: bool = False) -> Optional[str]:
        """Type of `var x = <expr>` for the easy cases (or its element type when elem=True)."""
        if elem:
            for i, c in enumerate(declarator.children):
                n = c.children[-1] if c.type == "equals_value_clause" and c.children else c
                if i and n.type in ("object_creation_expression", "array_creation_expression"):
                    return _elem_type(n.child_by_field_name("type"))
                if i and n.type == "identifier":
                    return self._elem_of(n, scope, type_id)
            return None
        for i, c in enumerate(declarator.children):
            n = c
            if i == 0:
                continue
            if n.type == "equals_value_clause":
                n = n.children[-1] if n.children else n
            if n.type == "object_creation_expression":
                return _outer_type(n.child_by_field_name("type"))
            if n.type == "cast_expression":
                return _outer_type(n.child_by_field_name("type"))
            if n.type == "as_expression":
                return _outer_type(n.child_by_field_name("right"))
            if n.type == "identifier":
                return scope.get(_text(n)) or (self.field_types.get(type_id or "", {}).get(_text(n)))
            if n.type == "member_access_expression":
                expr = n.child_by_field_name("expression")
                name = _text(n.child_by_field_name("name"))
                if expr is not None and expr.type == "this_expression":
                    return self.field_types.get(type_id or "", {}).get(name)
        return None

    def _invocation(self, node, cid: str, type_id: Optional[str], scope: dict[str, str]) -> None:
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        argc = len([a for a in args.children if a.type == "argument"]) if args is not None else 0
        line = node.start_point[0] + 1
        if fn is None:
            return
        name = None
        receiver = None
        rtype = None
        if fn.type in ("identifier", "generic_name"):
            name = _text(_child(fn, "identifier")) if fn.type == "generic_name" else _text(fn)
        elif fn.type == "member_access_expression":
            nm = fn.child_by_field_name("name")
            name = _text(_child(nm, "identifier")) if nm is not None and nm.type == "generic_name" else _text(nm)
            expr = fn.child_by_field_name("expression")
            receiver, rtype = self._receiver(expr, scope, type_id)
        elif fn.type == "conditional_access_expression":
            binding = None
            for c in fn.children:
                if c.type == "member_binding_expression":
                    binding = c
            if binding is not None:
                name = _text(binding.child_by_field_name("name") or _child(binding, "identifier"))
                receiver, rtype = self._receiver(fn.children[0], scope, type_id)
        if not name:
            return
        if name == "nameof":
            return
        self.res.calls.append(CallSite(cid, name, receiver, rtype, argc, line, type_id))

    def _receiver(self, expr, scope: dict[str, str], type_id: Optional[str]):
        if expr is None:
            return None, None
        t = expr.type
        own = self.field_types.get(type_id or "", {})
        if t == "this_expression":
            return "this", None
        if t == "base_expression":
            return "base", None
        if t == "identifier":
            nm = _text(expr)
            return nm, scope.get(nm) or own.get(nm)
        if t == "generic_name":
            return _text(_child(expr, "identifier")), None
        if t == "member_access_expression":
            inner = expr.child_by_field_name("expression")
            nm = _text(expr.child_by_field_name("name"))
            if inner is not None and inner.type == "this_expression":
                return nm, own.get(nm)
            # a.b.Call(): the receiver is member b of something; mark it as chained.
            return "." + nm, None
        if t == "object_creation_expression":
            tn = _outer_type(expr.child_by_field_name("type"))
            return tn, tn
        if t == "parenthesized_expression" and expr.named_child_count == 1:
            return self._receiver(expr.named_children[0], scope, type_id)
        if t == "cast_expression":
            tn = _outer_type(expr.child_by_field_name("type"))
            return tn, tn
        if t == "element_access_expression":
            elem = self._elem_of(expr.child_by_field_name("expression"), scope, type_id)
            return "[]", elem
        return "?", None


def parse(repo: str, rel_path: str, file_id: str, src: bytes, module: str = "") -> FileResult:
    return _Walker(repo, rel_path, file_id, src, module).run()
