"""C# adapter: tree-sitter structure, with enough local type inference to resolve most calls."""

from __future__ import annotations

import re
from typing import Optional

import tree_sitter_c_sharp
from tree_sitter import Language, Parser

from .. import channels
from ..model import FieldUse, CallSite, Edge, Endpoint, EventUse, FileResult, ImportRef, Node, TypeRef

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
LAMBDAS = ("lambda_expression", "anonymous_method_expression")
# `this` and `base` as the grammar names them: this_expression in older releases, this from 0.23 on.
THIS = ("this_expression", "this")
BASE = ("base_expression", "base")


LITERALS = {"integer_literal": "int", "real_literal": "double", "boolean_literal": "bool", "character_literal": "char",
            "string_literal": "string", "verbatim_string_literal": "string", "raw_string_literal": "string",
            "interpolated_string_expression": "string"}


HTTP_CLIENT = {"GetAsync": "GET", "GetStringAsync": "GET", "GetFromJsonAsync": "GET", "GetStreamAsync": "GET",
               "GetByteArrayAsync": "GET", "PostAsync": "POST", "PostAsJsonAsync": "POST", "PutAsync": "PUT",
               "PutAsJsonAsync": "PUT", "DeleteAsync": "DELETE", "PatchAsync": "PATCH"}
HTTP_MAP = {"MapGet": "GET", "MapPost": "POST", "MapPut": "PUT", "MapDelete": "DELETE", "MapPatch": "PATCH"}
HTTP_ATTR = re.compile(r"\[\s*(Http(Get|Post|Put|Delete|Patch)|Route)\s*\(\s*\"([^\"]*)\"")
FILE_CALL = re.compile(r"(?:^|\.)(File|Directory|FileAccess)\.(\w+)$")


def _cs_strings(node) -> list[str]:
    """String literals under a node. An interpolated hole becomes {}."""
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type == "interpolated_string_expression":
            parts = []
            for c in n.children:
                if c.type == "interpolation":
                    parts.append("{}")
                elif c.is_named and not c.type.startswith("interpolation_"):   # not the $ that opens it
                    parts.append(_text(c))
            out.append("".join(parts))
        elif n.type in ("string_literal", "verbatim_string_literal", "raw_string_literal"):
            body = "".join(_text(c) for c in n.children if c.type == "string_literal_content" or c.type.endswith("content"))
            out.append(body or _text(n).lstrip("@").strip('"'))
        else:
            stack.extend(reversed(n.children))
    return [x for x in out if x]


# Calls whose first string is never a request: logging, string work, routing and parsing.
NOT_REQUESTS = frozenset("""WriteLine Write Log LogInformation LogDebug LogWarning LogError LogTrace LogCritical Format
Concat Join Split Replace StartsWith EndsWith Contains IndexOf Equals Compare Parse TryParse Match IsMatch Matches
Combine GetFullPath Exists Map MapGroup MapFallback UseRouting Route Redirect RedirectPermanent Throw Assert
Created CreatedAtRoute CreatedAtAction Accepted AcceptedAtRoute LocalRedirect AddRoute MapForwarder""".split())


def _relative_url(path: str):
    """A request path with no leading slash, said against a client's base address: "api/items/1" -> /api/items/1.
    None for anything that is not plainly a path of two or more parts."""
    path = path[2:] if path.startswith("{}/") else path
    path = path.split("?", 1)[0]
    if not path or path.startswith(("http:", "https:")) or any(ch.isspace() for ch in path):
        return None
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or "." in parts[0] or "." in parts[-1] or not re.fullmatch(r"[\w\-{}:]+", parts[0]):
        return None
    return "/" + "/".join(parts)


def _controller_prefix(method_node, method_name: str):
    """The route an ASP.NET controller's [Route("api/[controller]")] gives its actions, or None without one."""
    cls = method_node.parent
    while cls is not None and cls.type not in ("class_declaration", "record_declaration"):
        if cls.type in CALLABLE_DECLS or cls.type == "compilation_unit":
            return None
        cls = cls.parent
    if cls is None:
        return None
    text = " ".join(_text(a) for a in cls.children if a.type == "attribute_list")
    m = re.search(r"\bRoute\s*\(\s*@?\"([^\"]*)\"", text)
    if not m:
        return None
    cname = _text(cls.child_by_field_name("name"))
    cname = cname[:-len("Controller")] if cname.endswith("Controller") and len(cname) > len("Controller") else cname
    return m.group(1).replace("[controller]", cname).replace("[action]", method_name).strip("/")


def pathlike(s: str) -> bool:
    if not s or len(s) > 120 or any(ch.isspace() for ch in s) or s.startswith(("http:", "https:")):
        return False
    return bool(re.fullmatch(r"(\{\}|\*)?(\.[A-Za-z0-9_]{1,10}){1,2}", s)) or "/" in s.strip("/")


def _identifiers(node) -> list[str]:
    if node is None:
        return []
    if node.type == "identifier":
        return [_text(node)]
    return [x for c in node.children for x in _identifiers(c)]


def _declared_names(node) -> list[str]:
    """Names a lambda, pattern, catch clause or out-declaration introduces."""
    if node.type == "lambda_expression":
        first = node.child_by_field_name("parameters") or (node.named_children[0] if node.named_children else None)
        if first is None or first.type not in ("implicit_parameter", "parameter_list", "identifier"):
            return []
        if first.type == "parameter_list":
            return [_text(p.child_by_field_name("name") or p.named_children[-1]) for p in first.children if p.type == "parameter"]
        return [_text(first)]
    name = node.child_by_field_name("name")
    if name is not None:
        return _identifiers(name)
    ids = [c for c in node.named_children
           if c.type in ("identifier", "single_variable_designation", "parenthesized_variable_designation")]
    return _identifiers(ids[-1]) if ids else []


def _predefined(tnode) -> str:
    """int, float, string: a parameter of one of these is matched against a literal or a variable of it."""
    return _text(tnode) if tnode is not None and tnode.type == "predefined_type" else ""


def _product(node, scope: dict) -> Optional[tuple]:
    """`total * share` as an argument parses as a declaration of a pointer variable. When the "type" is a local
    variable, it is a multiplication: the two names."""
    if node.type != "declaration_expression":
        return None
    tnode, name = node.child_by_field_name("type"), node.child_by_field_name("name")
    inner = tnode.named_children[0] if tnode is not None and tnode.type == "pointer_type" and tnode.named_children else None
    if inner is None or name is None or inner.type != "identifier" or ("#" + _text(inner)) not in scope:
        return None
    return _text(inner), _text(name)


def _arg_hint(arg, scope: dict):
    """What is known about one argument: a lambda's parameter count (int), a type name (str), or None."""
    if arg.type == "lambda_expression":
        params = arg.child_by_field_name("parameters")
        if params is None:
            return None
        if params.type in ("identifier", "implicit_parameter"):
            return 1
        return len([c for c in params.children if c.type in ("parameter", "identifier", "implicit_parameter")])
    if arg.type == "real_literal":
        return {"f": "float", "m": "decimal"}.get(_text(arg)[-1:].lower(), "double")   # 9f is a float, 9.0 a double
    if arg.type in LITERALS:
        return LITERALS[arg.type]
    if arg.type == "parenthesized_expression" and arg.named_children:
        return _arg_hint(arg.named_children[0], scope)
    if _product(arg, scope):
        sides = [scope.get("%" + x) for x in _product(arg, scope)]
        return next((wide for wide in NUMERIC if wide in sides), None) if all(x in NUMERIC for x in sides) else None
    if arg.type == "binary_expression":
        # total * share: arithmetic on numbers has the widest operand's type; on a string, + gives a string.
        op = arg.child_by_field_name("operator")
        left, right = arg.child_by_field_name("left"), arg.child_by_field_name("right")
        if op is None or left is None or right is None or _text(op) not in ("+", "-", "*", "/", "%"):
            return None
        sides = [_arg_hint(left, scope), _arg_hint(right, scope)]
        if _text(op) == "+" and "string" in sides:
            return "string"
        for wide in NUMERIC:
            if wide in sides:
                return wide if all(x in NUMERIC for x in sides) else None
        return None
    if arg.type == "object_creation_expression":
        names = _type_names(arg.child_by_field_name("type"))
        return names[0].split("`")[0] if names else None
    if arg.type == "identifier":
        known = scope.get(_text(arg)) or scope.get("%" + _text(arg))
        return known.split("`")[0] if known else None
    return None


NUMERIC = ("decimal", "double", "float", "ulong", "long", "uint", "int")   # widest first


PREDEFINED = {"int", "long", "short", "byte", "uint", "ulong", "float", "double", "decimal", "bool", "string",
              "char", "TimeSpan", "DateTime", "DateTimeOffset", "Guid", "CancellationToken"}


def _delegate_arity(ptype: str) -> int:
    """How many arguments a delegate-typed parameter takes; -1 when the type is not a known delegate."""
    head, _, rest = ptype.rstrip("?").partition("<")
    head = head.rsplit(".", 1)[-1]
    if head in PREDEFINED:
        return -2  # certainly not a delegate: a lambda cannot be passed here
    if head not in ("Action", "Func", "Predicate", "Comparison"):
        return -1
    if not rest:
        return 0
    depth, n = 0, 1
    for ch in rest[:-1]:
        depth += ch == "<"
        depth -= ch == ">"
        n += ch == "," and depth == 0
    return {"Action": n, "Func": n - 1, "Predicate": 1, "Comparison": 2}[head]
STRINGS = ("string_literal", "verbatim_string_literal", "raw_string_literal", "interpolated_string_expression")
# Calls of the shape Run("name", () => { ... }) declare a test inline.
TEST_RUNNERS = {"Run", "Test", "It", "Case", "Scenario", "Fact", "Check", "Spec"}
TEST_ATTRIBUTES = {"Fact", "Theory", "Test", "TestCase", "TestMethod", "DataTestMethod"}
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
        elif n.type == "generic_name":
            # Foo<A, B> is written Foo`2, so it is told apart from a non-generic Foo.
            targs = _child(n, "type_argument_list")
            count = len([c for c in targs.named_children]) if targs is not None else 0
            out.append(f"{_text(_child(n, 'identifier'))}`{count}" if count else _text(_child(n, "identifier")))
            if targs is not None:
                stack.extend(reversed(targs.children))
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
    if len(names) == 2 and names[0].split("`")[0] in SEQUENCES:
        return names[1]
    return None


def _outer_type(node) -> Optional[str]:
    names = _type_names(node)
    return names[0] if names else None


# Methods that change the collection they are called on. A field used this way is written, not only read.
MUTATORS = frozenset("""Add AddRange AddFirst AddLast Insert InsertRange Remove RemoveAt RemoveAll RemoveRange RemoveFirst
RemoveLast RemoveWhere Clear Enqueue Dequeue TryDequeue Push Pop TryPop TryAdd TryRemove TryUpdate TryTake Sort Reverse
UnionWith ExceptWith IntersectWith SymmetricExceptWith Append AppendLine AppendFormat Set SetValue Fill EnsureCapacity
TrimExcess GetOrAdd AddOrUpdate""".split())


class _Walker:
    def __init__(self, repo: str, rel_path: str, file_id: str, src: bytes, module: str):
        self.repo = repo
        self.path = rel_path
        self.file_id = file_id
        self.module = module
        self.res = FileResult()
        self.tree = _parser.parse(src)
        self.field_types: dict[str, dict[str, str]] = {}  # type id -> member name -> type name
        self.redirect: dict[tuple, str] = {}  # lambda span -> node its body's calls belong to
        self.done_calls: dict[tuple, Optional[CallSite]] = {}  # invocation span -> its call site

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
                if t == "file_scoped_namespace_declaration":
                    # `namespace X;` covers the rest of the file: what follows is a sibling, not a child.
                    ns = qual = full
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
            ptypes.append(_squash(_text(ptype)).replace(", ", ","))
            pname = _text(p.child_by_field_name("name"))
            outer = _outer_type(ptype)
            if pname:
                scope["#" + pname] = "1"  # a local name: never a field of the enclosing type
                scope.pop("%" + pname, None)
                if ptype is not None and ptype.type == "predefined_type":
                    scope["%" + pname] = _text(ptype)   # float, int: not a receiver, but tells overloads apart
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
        tparams = _child(node, "type_parameter_list")
        generic_arity = len([c for c in tparams.children if c.type == "type_parameter"]) if tparams is not None else 0
        # M(int) and M<T>(int) are different methods, so the count of type parameters is part of the id.
        cid = f"{parent_id}{sep}{name}{'`' + str(generic_arity) if generic_arity else ''}({','.join(ptypes)})"
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
                   "body_line": body.start_point[0] + 1 if body is not None else None,
                   "delegate_arity": [_delegate_arity(x) for x in ptypes], "generic_arity": generic_arity,
                   "returns_names": _type_names(ret) if ret is not None and t != "constructor_declaration" else [],
                   "type_params": [_text(_child(c, "identifier") or c) for c in tparams.children
                                   if c.type == "type_parameter"] if tparams is not None else [],
                   "is_extension": bool(plist) and any(_text(c) == "this" for c in plist[0].children) or None,
                   "param_types": [(_type_names(p.child_by_field_name("type")) or [_predefined(p.child_by_field_name("type"))])[0]
                                   for p in plist],
                   "type_id": type_id}))
        for p, ptext in zip(plist, ptypes):
            names = _type_names(p.child_by_field_name("type"))
            if names:
                self.res.type_refs.append(TypeRef(cid, names, "param", p.start_point[0] + 1))
        if ret is not None:
            names = _type_names(ret)
            if names:
                self.res.type_refs.append(TypeRef(cid, names, "return", ret.start_point[0] + 1))
        attrs_text = " ".join(_text(a) for a in node.children if a.type == "attribute_list")
        prefix = _controller_prefix(node, name)
        for m in HTTP_ATTR.finditer(attrs_text):
            path = m.group(3)
            if prefix is not None and not path.startswith(("/", "~")):   # [Route("api/[controller]")] on the class
                path = f"{prefix}/{path}" if path else prefix
            path = path.lstrip("~")
            self.res.endpoints.append(Endpoint("http", "serve", cid, "/" + path.lstrip("/"), node.start_point[0] + 1,
                                               m.group(2).upper() if m.group(2) else None))
        if prefix is not None:   # [HttpGet] with no template: the action answers at the controller's own route
            for m in re.finditer(r"\[\s*(?:[^\]]*,\s*)?Http(Get|Post|Put|Delete|Patch)\s*(?:\(\s*\)\s*)?[\],]", attrs_text):
                self.res.endpoints.append(Endpoint("http", "serve", cid, "/" + prefix.lstrip("/"), node.start_point[0] + 1,
                                                   m.group(1).upper()))
        found = set(re.findall(r"[A-Za-z_]+", attrs_text)) & TEST_ATTRIBUTES
        if found:
            self.res.nodes[-1].attrs["is_test"] = True
            self.res.nodes[-1].attrs["framework"] = sorted(found)[0]
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
        if t in LAMBDAS and self.redirect:
            cid = self.redirect.pop((node.start_byte, node.end_byte), cid)
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
                if nm:
                    scope.pop("~" + nm, None)
                    scope.pop("%" + nm, None)
                    scope["#" + nm] = "1"
                    if tnode is not None and tnode.type == "predefined_type":
                        scope["%" + nm] = _text(tnode)
                if nm and vt:
                    scope[nm] = vt
                elif nm:
                    # `var x = a.Make();`: the type is whatever Make returns, known once calls are resolved.
                    init = self._call_in(v)
                    if init is not None:
                        site = self._invocation(init, cid, type_id, scope)
                        if site is not None:
                            scope["~" + nm] = site
                elem = _elem_type(tnode) if declared else self._infer(v, scope, type_id, elem=True)
                if nm and elem:
                    scope[nm + "[]"] = elem
        elif t in ("foreach_statement",):
            tnode = node.child_by_field_name("type")
            nm = _text(node.child_by_field_name("left"))
            for ident in _identifiers(node.child_by_field_name("left")):
                scope["#" + ident] = "1"
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
        elif t in ("lambda_expression", "catch_declaration", "declaration_expression", "declaration_pattern", "from_clause") \
                and not _product(node, scope):
            tnode = node.child_by_field_name("type") if t != "lambda_expression" else None
            declared = _outer_type(tnode) if tnode is not None and _text(tnode) != "var" else None
            for ident in _declared_names(node):  # names these introduce are locals
                scope["#" + ident] = "1"
                if declared:   # `x is Sandbox s`, `out Foo f`, `catch (IOException e)`: typed where introduced
                    scope.pop("~" + ident, None)
                    scope[ident] = declared
        if t in ("identifier", "member_access_expression"):
            self._use(node, cid, type_id, scope)
        elif t in STRINGS:
            for text in _cs_strings(node):
                if pathlike(text):
                    self.res.path_strings.setdefault(cid, []).append(text)
        if t == "assignment_expression" and any(_text(c) == "+=" for c in node.children if not c.is_named):
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and right is not None and right.type in LAMBDAS + ("identifier", "member_access_expression"):
                handler = None
                if right.type == "identifier":
                    handler = _text(right)
                elif right.type == "member_access_expression":
                    handler = _text(right.child_by_field_name("name"))
                if left.type == "identifier":
                    self.res.events.append(EventUse("subscribe", cid, _text(left), None, None, handler,
                                                    node.start_point[0] + 1, type_id))
                elif left.type == "member_access_expression":
                    recv, rtype = self._receiver(left.child_by_field_name("expression"), scope, type_id)
                    self.res.events.append(EventUse("subscribe", cid, _text(left.child_by_field_name("name")),
                                                    recv, rtype, handler, node.start_point[0] + 1, type_id))
        elif t == "object_creation_expression":
            tnode = node.child_by_field_name("type")
            names = _type_names(tnode)
            args = node.child_by_field_name("arguments")
            argc = len([a for a in args.children if a.type == "argument"]) if args is not None else 0
            if names:
                self.res.type_refs.append(TypeRef(cid, names, "instantiate", node.start_point[0] + 1))
                if names[0] in ("StreamWriter", "StreamReader"):
                    self.res.endpoints.append(Endpoint("file", "write" if names[0] == "StreamWriter" else "read", cid, "",
                                                       node.start_point[0] + 1, None, _cs_strings(args) if args is not None else []))
                ctor_args = [a.named_children[-1] for a in args.children
                             if a.type == "argument" and a.named_children] if args is not None else []
                if names[0] in ("RestRequest", "HttpRequestMessage"):
                    self._request_object(names[0], ctor_args, cid, node.start_point[0] + 1)
                self.res.calls.append(CallSite(cid, ".ctor", names[0], names[0], argc,
                                               node.start_point[0] + 1, type_id, node.start_point[1],
                                               tuple(_arg_hint(a, scope) for a in ctor_args) if len(ctor_args) == argc else ()))
        for c in node.children:
            self._body(c, cid, type_id, scope)

    def _use(self, node, cid: str, type_id: Optional[str], scope: dict[str, str]) -> None:
        """Record a name that may be a field, and whether this use reads or assigns it."""
        parent = node.parent
        if parent is None:
            return
        pt = parent.type
        same = lambda a, b: a is not None and b is not None and a.start_byte == b.start_byte and a.end_byte == b.end_byte
        receiver = rtype = chain = None
        if node.type == "member_access_expression":
            if pt == "invocation_expression" and same(parent.child_by_field_name("function"), node):
                return  # a method call, recorded as a call site
            name_node = node.child_by_field_name("name")
            if name_node is None or name_node.type != "identifier":
                return
            name = _text(name_node)
            expr = node.child_by_field_name("expression")
            receiver, rtype = self._receiver(expr, scope, type_id)
            inner = expr
            while inner is not None and inner.type in ("parenthesized_expression", "await_expression") and inner.named_children:
                inner = inner.named_children[-1]
            if inner is not None and inner.type == "invocation_expression":
                chain, receiver = self._invocation(inner, cid, type_id, scope), "?"
            elif rtype is None and receiver and "~" + receiver in scope:
                chain = scope["~" + receiver]
        else:
            name = _text(node)
            if "#" + name in scope:
                return  # a local or a parameter
            if pt == "member_access_expression" and same(parent.child_by_field_name("name"), node):
                return  # the member half of a.b, handled with its parent
            if pt in ("invocation_expression", "generic_name", "qualified_name", "type_argument_list", "base_list",
                      "array_type", "nullable_type", "name_colon", "name_equals", "attribute", "variable_declarator",
                      "parameter", "type_parameter", "using_directive", "labeled_statement", "member_binding_expression",
                      "implicit_parameter", "tuple_element", "pointer_type", "type_parameter_constraint", "typeof_expression",
                      "alias_qualified_name", "enum_member_declaration", "local_function_statement", "catch_declaration",
                      "single_variable_designation", "declaration_pattern", "constant_pattern", "from_clause"):
                if not (pt == "variable_declarator" and not same(parent.child_by_field_name("name") or _child(parent, "identifier"), node)):
                    return
            if same(parent.child_by_field_name("type"), node) or same(parent.child_by_field_name("name"), node) and pt != "argument":
                return  # a type name, or the name being declared
            if pt == "assignment_expression" and parent.parent is not None and parent.parent.type == "initializer_expression" \
                    and same(parent.child_by_field_name("left"), node):
                made = parent.parent.parent
                if made is not None and made.type == "object_creation_expression":
                    tn = _outer_type(made.child_by_field_name("type"))
                    if tn:  # new Foo { a = 1 } assigns Foo.a
                        self.res.field_uses.append(FieldUse(cid, name, tn, tn, "i", node.start_point[0] + 1, type_id))
                return
        # Read or write: look at what the expression sits in.
        cur, up = node, parent
        while up is not None and up.type == "parenthesized_expression":
            cur, up = up, up.parent
        access = "r"
        if up is not None:
            ops = [_text(c) for c in up.children if not c.is_named]
            if up.type == "assignment_expression" and same(up.child_by_field_name("left"), cur):
                access = "w" if "=" in ops else "rw"
            elif up.type in ("postfix_unary_expression", "prefix_unary_expression") and ("++" in ops or "--" in ops):
                access = "rw"
            elif up.type == "argument" and any(o in ("out", "ref") for o in ops):
                access = "w" if "out" in ops else "rw"
            elif up.type == "element_access_expression" and same(up.child_by_field_name("expression") or up.named_children[0], cur):
                outer = up.parent
                if outer is not None and outer.type == "assignment_expression" and same(outer.child_by_field_name("left"), up):
                    access = "rw"  # field[i] = x changes what the field holds
        if access == "r" and up is not None:   # field.Add(x), field[k].Enqueue(x): a call that changes what the field holds
            c2, u2 = cur, up
            if u2.type == "element_access_expression" and same(u2.child_by_field_name("expression") or u2.named_children[0], c2):
                c2, u2 = u2, u2.parent
            if u2 is not None and u2.type == "member_access_expression" and same(u2.child_by_field_name("expression"), c2) \
                    and u2.parent is not None and u2.parent.type == "invocation_expression" \
                    and _text(u2.child_by_field_name("name")).split("<")[0] in MUTATORS:
                access = "rw"
        self.res.field_uses.append(FieldUse(cid, name, receiver, rtype, access, node.start_point[0] + 1, type_id, chain))

    def _elem_of(self, expr, scope: dict[str, str], type_id: Optional[str]) -> Optional[str]:
        """Element type of a collection expression that is a local, a parameter or a field."""
        if expr is None:
            return None
        own = self.field_types.get(type_id or "", {})
        if expr.type == "identifier":
            return scope.get(_text(expr) + "[]") or own.get(_text(expr) + "[]")
        if expr.type == "member_access_expression":
            inner = expr.child_by_field_name("expression")
            if inner is not None and inner.type in THIS:
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
                if expr is not None and expr.type in THIS:
                    return self.field_types.get(type_id or "", {}).get(name)
        return None

    def _map_group(self, receiver: str) -> str:
        """The route prefix of a minimal-API group a MapGet is called on: `app.MapGroup("api/x")` written in
        place, or a variable this file sets to one (`var api = app.MapGroup("api/x")`, groups of groups too)."""
        if not hasattr(self, "_groups"):
            text = self.tree.root_node.text.decode("utf8", "replace")
            found: dict[str, set] = {}
            for m in re.finditer(r"\b(\w+)\s*=\s*([\w.]+?)\s*\.\s*MapGroup\s*\(\s*@?\"([^\"]*)\"", text):
                found.setdefault(m.group(1), set()).add((m.group(2).rsplit(".", 1)[-1], m.group(3)))
            self._groups = {k: next(iter(v)) for k, v in found.items() if len(v) == 1}
        m = re.search(r"MapGroup\s*\(\s*@?\"([^\"]*)\"\s*\)[^\"]*$", receiver)
        if m:
            base = receiver[:m.start()].rstrip(". ")
            return (self._map_group(base) + "/" + m.group(1).strip("/")).strip("/") if base else m.group(1).strip("/")
        prefix, seen = [], set()
        while receiver in self._groups and receiver not in seen:
            seen.add(receiver)
            receiver, part = self._groups[receiver]
            prefix.insert(0, part.strip("/"))
        return "/".join(p for p in prefix if p)

    def _request_object(self, kind: str, ctor_args, cid: str, line: int) -> None:
        """new RestRequest("api/items", Method.Post) or new HttpRequestMessage(HttpMethod.Get, "/api/items"): a
        request built as an object and sent later. A path a route may serve (see Indexer._resolve_endpoints)."""
        strings = [a for a in ctor_args if a.type in STRINGS]
        if not strings:
            return
        path = (_cs_strings(strings[0]) or [""])[0]
        url = path if path.startswith("/") and path.count("/") >= 2 else _relative_url(path)
        if not url:
            return
        m = re.search(r"\b(?:Http)?Method\.(Get|Post|Put|Delete|Patch)\b", " ".join(_text(a) for a in ctor_args), re.I)
        self.res.endpoints.append(Endpoint("http", "maybe", cid, url.split("?", 1)[0], line, m.group(1).upper() if m else None))

    def _endpoint(self, full: str, name: str, args, arg_nodes, cid: str, line: int) -> None:
        """Is this call one end of an HTTP or file channel?"""
        first = arg_nodes[0] if arg_nodes else None
        if first is not None and first.type in STRINGS and name in HTTP_CLIENT | HTTP_MAP:
            path = (_cs_strings(first) or [""])[0]
            if path.startswith(("/", "http://", "https://")) or name in HTTP_MAP:
                role, method = ("serve", HTTP_MAP[name]) if name in HTTP_MAP else ("call", HTTP_CLIENT[name])
                if role == "serve":
                    group = self._map_group(full.rsplit(".", 1)[0])
                    if group:   # api = app.MapGroup("api/catalog"); api.MapGet("/items/{id}", ...)
                        path = group.rstrip("/") + "/" + path.lstrip("/") if path.strip("/") else group
                self.res.endpoints.append(Endpoint("http", role, cid, path if path.startswith(("/", "http")) else "/" + path, line, method))
                return
            rel = _relative_url(path)
            if rel:   # client.GetAsync("api/items/1") against a base address, or $"{baseUrl}/api/items"
                self.res.endpoints.append(Endpoint("http", "maybe", cid, rel, line, HTTP_CLIENT[name]))
                return
        if first is not None and first.type in STRINGS and name not in HTTP_CLIENT | HTTP_MAP \
                and not FILE_CALL.search(full.replace("?", "")) and not full.endswith(("Path.Combine", "Path.Join")) \
                and name not in NOT_REQUESTS:
            # A request through the project's own wrapper (Get<T>("/api/items/1")): a path a route may serve.
            path = (_cs_strings(first) or [""])[0]
            path = path[2:] if path.startswith("{}/") else path
            if path.startswith("/") and path.count("/") >= 2 and not any(ch.isspace() for ch in path) \
                    and "." not in path.rsplit("/", 1)[-1]:
                self.res.endpoints.append(Endpoint("http", "maybe", cid, path.split("?", 1)[0], line, None))
                return
        m = FILE_CALL.search(full.replace("?", ""))
        if m:
            owner, op = m.group(1), m.group(2)
            text = _text(args) if args is not None else ""
            if owner == "FileAccess":
                role = "write" if "Write" in text else "read" if op == "Open" else None
            elif owner == "Directory":
                role = "read" if op in ("GetFiles", "EnumerateFiles", "GetDirectories") else None
            else:
                role = ("write" if op.startswith(("Write", "Append", "Create", "OpenWrite", "Copy", "Move")) else
                        "read" if op.startswith(("Read", "OpenRead", "OpenText")) else None)
            if role:
                self.res.endpoints.append(Endpoint("file", role, cid, "", line, None, _cs_strings(args) if args is not None else []))
        if full.endswith(("Path.Combine", "Path.Join")) and args is not None:
            self.res.path_strings.setdefault(cid, []).extend(_cs_strings(args))

    def _call_in(self, declarator):
        """The call a variable is initialised from, looking through `await` and parentheses."""
        cur = declarator.named_children[-1] if declarator.named_child_count > 1 else None
        if cur is not None and cur.type == "equals_value_clause":
            cur = cur.named_children[-1] if cur.named_children else None
        while cur is not None and cur.type in ("await_expression", "parenthesized_expression") and cur.named_children:
            cur = cur.named_children[-1]
        return cur if cur is not None and cur.type == "invocation_expression" else None

    def _invocation(self, node, cid: str, type_id: Optional[str], scope: dict[str, str]):
        key = (node.start_byte, node.end_byte)
        if key in self.done_calls:
            return self.done_calls[key]  # already recorded as the receiver or initialiser of something
        self.done_calls[key] = None
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        argc = len([a for a in args.children if a.type == "argument"]) if args is not None else 0
        line = node.start_point[0] + 1
        if fn is None:
            return
        name = None
        receiver = None
        rtype = None
        chain = None
        targs = 0
        generic = fn if fn.type == "generic_name" else (
            fn.child_by_field_name("name") if fn.type == "member_access_expression" else None)
        if generic is not None and generic.type == "generic_name":
            tl = _child(generic, "type_argument_list")
            targs = len(tl.named_children) if tl is not None else 0
        if fn.type in ("identifier", "generic_name"):
            name = _text(_child(fn, "identifier")) if fn.type == "generic_name" else _text(fn)
        elif fn.type == "member_access_expression":
            nm = fn.child_by_field_name("name")
            name = _text(_child(nm, "identifier")) if nm is not None and nm.type == "generic_name" else _text(nm)
            expr = fn.child_by_field_name("expression")
            receiver, rtype = self._receiver(expr, scope, type_id)
            inner = expr
            while inner is not None and inner.type in ("parenthesized_expression", "await_expression") and inner.named_children:
                inner = inner.named_children[-1]
            if inner is not None and inner.type == "invocation_expression":
                chain = self._invocation(inner, cid, type_id, scope)  # a.Make().Run(): typed by what Make returns
                receiver = "?"
            elif rtype is None and receiver and "~" + receiver in scope:
                chain = scope["~" + receiver]
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
        if name == "Invoke" and receiver and receiver[:1].isalpha() and receiver not in ("this", "base"):
            self.res.events.append(EventUse("raise", cid, receiver, None, None, None, line, type_id))
        arg_nodes = [a.named_children[-1] for a in args.children if a.type == "argument" and a.named_children] if args is not None else []
        if (name in TEST_RUNNERS and len(arg_nodes) == 2 and arg_nodes[0].type in STRINGS
                and arg_nodes[1].type in LAMBDAS):
            title = _text(arg_nodes[0]).strip('@$"')
            tid = f"{cid}/test:{re.sub(r'[^A-Za-z0-9]+', '-', title).strip('-').lower()[:80]}"
            lam = arg_nodes[1]
            self.res.nodes.append(Node(
                id=tid, kind="test", name=title, parent_id=cid, language=LANGUAGE, path=self.path,
                span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
                attrs={"framework": "inline runner", "runner": name}))
            self.redirect[(lam.start_byte, lam.end_byte)] = tid
        self._endpoint(_text(fn), name, args, arg_nodes, cid, line)
        site = CallSite(cid, name, receiver, rtype, argc, line, type_id, node.start_point[1],
                        tuple(_arg_hint(a, scope) for a in arg_nodes) if len(arg_nodes) == argc else (),
                        targs, chain)
        self.res.calls.append(site)
        self.done_calls[key] = site
        return site

    def _receiver(self, expr, scope: dict[str, str], type_id: Optional[str]):
        if expr is None:
            return None, None
        t = expr.type
        own = self.field_types.get(type_id or "", {})
        if t in THIS:
            return "this", None
        if t in BASE:
            return "base", None
        if t == "identifier":
            nm = _text(expr)
            return nm, scope.get(nm) or own.get(nm)
        if t == "generic_name":
            names = _type_names(expr)  # Policy<int>.Handle(): the generic Policy, not the plain one
            return _text(_child(expr, "identifier")), (names[0] if names and "`" in names[0] else None)
        if t == "member_access_expression":
            inner = expr.child_by_field_name("expression")
            nm = _text(expr.child_by_field_name("name"))
            if inner is not None and inner.type in THIS:
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
    w = _Walker(repo, rel_path, file_id, src, module)
    res = w.run()
    channels.extract(LANGUAGE, w.tree, res, file_id)
    return res
