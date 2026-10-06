"""Python adapter: tree-sitter structure, imports, and call sites with simple receiver inference."""

from __future__ import annotations

import re
from typing import Optional

import tree_sitter_python
from tree_sitter import Language, Parser

from ..model import CallSite, Edge, FileResult, ImportRef, Node, Spawn, TypeRef

NAME = "tree-sitter-python"
VERSION = "0.1"
LANGUAGE = "python"
EXTENSIONS = (".py",)

_parser = Parser(Language(tree_sitter_python.language()))
LAUNCHERS = {"subprocess.Popen", "subprocess.run", "subprocess.check_output", "subprocess.check_call",
             "subprocess.call", "os.system", "os.popen", "Popen", "check_output", "check_call"}


def _strings(node) -> list[str]:
    """Every string literal under a node, without quotes."""
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type == "string":
            body = "".join(_text(c) for c in n.children if c.type == "string_content")
            if body:
                out.append(body)
        else:
            stack.extend(reversed(n.children))
    return out


def _text(node) -> str:
    return node.text.decode("utf8", "replace") if node is not None else ""


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def module_path(rel_path: str) -> str:
    p = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


WRAPPERS = {"Optional", "Union", "Iterator", "Iterable", "Generator", "AsyncIterator", "AsyncGenerator", "Awaitable",
            "Callable", "Any", "Type", "List", "Dict", "Set", "Tuple", "Sequence", "Mapping", "None", "Annotated",
            "ClassVar", "Final", "Literal", "Self", "TypeVar", "Collection", "NoReturn", "IO"}


def _annotation_types(text: str) -> list[str]:
    """Class names in an annotation, without the typing wrappers around them: Iterator[Flask] -> [Flask]."""
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    return [x for x in names if x[:1].isupper() and x not in WRAPPERS]


class _Walker:
    def __init__(self, repo: str, rel_path: str, file_id: str, src: bytes):
        self.repo = repo
        self.path = rel_path
        self.file_id = file_id
        self.mod = module_path(rel_path)
        self.src = src
        self.res = FileResult(declares=[self.mod])
        self.tree = _parser.parse(src)
        self.top_id = f"{repo}:python:{self.mod}.<module>"
        self.top_used = False
        self.self_types: dict[str, dict[str, str]] = {}  # class id -> attribute -> class name
        self.consts: dict[str, list[str]] = {}  # module-level NAME = "..." or [...] -> its strings
        self.all_strings: Optional[list[str]] = None
        base = rel_path.rsplit("/", 1)[-1]
        self.is_test_file = base.startswith("test_") or base.endswith("_test.py")
        for st in self.tree.root_node.children:
            if st.type == "expression_statement" and st.named_child_count and st.named_children[0].type == "assignment":
                left = st.named_children[0].child_by_field_name("left")
                right = st.named_children[0].child_by_field_name("right")
                if left is not None and left.type == "identifier" and right is not None:
                    found = _strings(right)
                    if found:
                        self.consts[_text(left)] = found

    def run(self) -> FileResult:
        root = self.tree.root_node
        scope: dict[str, str] = {}
        self._block(root, parent_id=self.file_id, qual=self.mod, class_id=None,
                    cid=self.top_id, scope=scope, top=True)
        if self.top_used:
            self.res.nodes.append(Node(
                id=self.top_id, kind="callable", name="<module>", parent_id=self.file_id,
                language=LANGUAGE, path=self.path, span_start=1, span_end=root.end_point[0] + 1,
                attrs={"signature": f"module {self.mod}", "visibility": "public",
                       "native_kind": "module_body", "argc_min": 0, "argc_max": 0}))
        return self.res

    # -- declarations --------------------------------------------------------
    def _block(self, node, parent_id, qual, class_id, cid, scope, top=False) -> None:
        for c in node.children:
            t = c.type
            target = c
            decorators: list[str] = []
            if t == "decorated_definition":
                decorators = [_squash(_text(d)) for d in c.children if d.type == "decorator"]
                target = c.child_by_field_name("definition") or c.children[-1]
                t = target.type
            if t == "function_definition":
                self._function(target, c, parent_id, qual, class_id, decorators)
            elif t == "class_definition":
                self._class(target, c, parent_id, qual, decorators)
            elif t in ("import_statement", "import_from_statement"):
                self._import(c)
            elif top and t == "if_statement" and "__name__" in _text(c.child_by_field_name("condition")):
                self.top_used = True
                self.res.nodes.append(Node(
                    id=self.top_id + "#entry", kind="entry_point", name=f"{self.path} (__main__)",
                    parent_id=self.file_id, language=LANGUAGE, path=self.path,
                    span_start=c.start_point[0] + 1, span_end=c.end_point[0] + 1,
                    attrs={"trigger": "cli", "address": self.path}))
                self.res.edges.append(Edge("exposes", self.top_id + "#entry", self.top_id))
                self._body(c, cid, class_id, scope)
            else:
                before = len(self.res.calls)
                self._body(c, cid, class_id, scope)
                if top and len(self.res.calls) > before:
                    self.top_used = True

    def _class(self, node, outer, parent_id, qual, decorators) -> None:
        name = _text(node.child_by_field_name("name"))
        qname = f"{qual}.{name}"
        tid = f"{self.repo}:python:{qname}"
        body = node.child_by_field_name("body")
        head_end = body.start_byte if body is not None else node.end_byte
        self.res.nodes.append(Node(
            id=tid, kind="type", name=name, parent_id=parent_id, language=LANGUAGE, path=self.path,
            span_start=outer.start_point[0] + 1, span_end=outer.end_point[0] + 1,
            attrs={"native_kind": "class", "namespace": qual,
                   "visibility": "private" if name.startswith("_") else "public",
                   "decorators": decorators, "is_abstract": False,
                   "signature": _squash(self.src[node.start_byte:head_end].decode("utf8", "replace")).rstrip(":")}))
        supers = node.child_by_field_name("superclasses")
        if supers is not None:
            for s in supers.named_children:
                if s.type in ("identifier", "attribute"):
                    self.res.type_refs.append(TypeRef(tid, [_text(s).split(".")[-1]], "base", s.start_point[0] + 1))
        self.self_types.setdefault(tid, {})
        if body is None:
            return
        # Class-level assignments are fields.
        for st in body.children:
            if st.type == "expression_statement" and st.named_child_count and st.named_children[0].type == "assignment":
                left = st.named_children[0].child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    self._field(tid, _text(left), st, st.named_children[0])
        self._block(body, tid, qname, tid, None, {})

    def _field(self, tid, name, node, assignment=None) -> None:
        fid = f"{tid}.{name}"
        if any(n.id == fid for n in self.res.nodes[-200:]):
            return
        declared = None
        if assignment is not None:
            ann = assignment.child_by_field_name("type")
            declared = _squash(_text(ann)) if ann is not None else None
            right = assignment.child_by_field_name("right")
            if right is not None and right.type == "call":
                fn = right.child_by_field_name("function")
                cls = _text(fn).split(".")[-1]
                if cls[:1].isupper():
                    self.self_types.setdefault(tid, {})[name] = cls
                    declared = declared or cls
        self.res.nodes.append(Node(
            id=fid, kind="field", name=name, parent_id=tid, language=LANGUAGE, path=self.path,
            span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
            attrs={"native_kind": "attribute", "declared_type": declared,
                   "type_name": self.self_types.get(tid, {}).get(name),
                   "visibility": "private" if name.startswith("_") else "public", "is_mutable": True}))
        self.res.edges.append(Edge("has_field", tid, fid))

    def _function(self, node, outer, parent_id, qual, class_id, decorators, outer_scope=None) -> None:
        name = _text(node.child_by_field_name("name"))
        cid = f"{self.repo}:python:{qual}.{name}"
        params = node.child_by_field_name("parameters")
        body = node.child_by_field_name("body")
        scope: dict[str, str] = dict(outer_scope or {})  # a nested function sees the enclosing locals
        pnames: list[str] = []
        required = total = 0
        variadic = False
        is_method = class_id is not None and parent_id == class_id
        is_static = any("staticmethod" in d for d in decorators)
        if params is not None:
            first = True
            for p in params.named_children:
                pname, ptype, has_default = None, None, False
                if p.type == "identifier":
                    pname = _text(p)
                elif p.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
                    nm = p.child_by_field_name("name") or (p.named_children[0] if p.named_children else None)
                    pname = _text(nm)
                    ptype = p.child_by_field_name("type")
                    has_default = "default" in p.type
                elif p.type in ("list_splat_pattern", "dictionary_splat_pattern"):
                    variadic = True
                    continue
                else:
                    continue
                if first and is_method and not is_static:
                    first = False
                    continue
                first = False
                total += 1
                if pname:
                    pnames.append(pname)
                    scope.pop(pname, None)  # a parameter hides an outer local of the same name
                if not has_default:
                    required += 1
                if ptype is not None:
                    tn = _annotation_types(_text(ptype))
                    if tn:
                        scope[pname] = tn[0]
                        self.res.type_refs.append(TypeRef(cid, tn, "param", p.start_point[0] + 1))
        head_end = body.start_byte if body is not None else node.end_byte
        ret = node.child_by_field_name("return_type")
        returns = (_annotation_types(_text(ret)) or [None])[0] if ret is not None else None
        fn_node = Node(
            id=cid, kind="callable", name=name, parent_id=parent_id, language=LANGUAGE, path=self.path,
            span_start=outer.start_point[0] + 1, span_end=outer.end_point[0] + 1,
            attrs={"signature": _squash(self.src[node.start_byte:head_end].decode("utf8", "replace")).rstrip(":"),
                   "visibility": "private" if name.startswith("_") and not name.startswith("__") else "public",
                   "is_static": is_static or not is_method, "is_async": _text(node).startswith("async"),
                   "is_virtual": is_method, "decorators": decorators,
                   "is_test": bool(self.is_test_file and name.startswith("test")) or None,
                   "framework": "pytest" if self.is_test_file and name.startswith("test") else None,
                   "native_kind": "method" if is_method else "function",
                   "argc_min": required, "argc_max": 99 if variadic else total, "type_id": class_id if is_method else None,
                   "params": pnames, "returns": returns,
                   "is_fixture": any(re.search(r"\bfixture\b", d) for d in decorators) or None})
        self.res.nodes.append(fn_node)
        if body is not None:
            self._body(body, cid, class_id if is_method else None, scope, qual=f"{qual}.{name}")
            if returns is None:
                # No annotation: read the type off what is returned or yielded.
                kind, value = self._returned(body, scope)
                if kind == "type":
                    fn_node.attrs["returns"] = value
                elif kind == "call":
                    fn_node.attrs["returns_call"] = value

    def _returned(self, body, scope):
        stack = list(body.children)
        while stack:
            n = stack.pop()
            if n.type in ("function_definition", "class_definition", "decorated_definition", "lambda"):
                continue
            if n.type in ("return_statement", "yield"):
                expr = n.named_children[0] if n.named_children else None
                if expr is not None and expr.type == "identifier" and _text(expr) in scope:
                    return "type", scope[_text(expr)]
                if expr is not None and expr.type == "call":
                    fn = expr.child_by_field_name("function")
                    last = _text(fn).split(".")[-1]
                    if last[:1].isupper():
                        return "type", last
                    if fn is not None and fn.type == "attribute" and fn.child_by_field_name("object").type == "identifier":
                        return "call", [_text(fn.child_by_field_name("object")), last]
            stack.extend(n.children)
        return None, None

    def _import(self, node) -> None:
        if node.type == "import_statement":
            for c in node.named_children:
                if c.type == "dotted_name":
                    self.res.imports.append(ImportRef(self.file_id, _text(c), alias=_text(c).split(".")[0]))
                elif c.type == "aliased_import":
                    self.res.imports.append(ImportRef(
                        self.file_id, _text(c.child_by_field_name("name")),
                        alias=_text(c.child_by_field_name("alias"))))
        else:
            mod = node.child_by_field_name("module_name")
            target = _text(mod)
            if target.startswith("."):
                if self.path.endswith("__init__.py"):
                    base = self.mod  # inside a package's __init__, `.` is the package itself
                else:
                    base = self.mod.rsplit(".", 1)[0] if "." in self.mod else ""
                dots = len(target) - len(target.lstrip("."))
                for _ in range(dots - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                rest = target.lstrip(".")
                target = ".".join(x for x in (base, rest) if x)
            symbols = []
            for c in node.named_children:
                if c is mod or (mod is not None and c.start_byte == mod.start_byte):
                    continue
                if c.type == "dotted_name":
                    symbols.append(_text(c))
                elif c.type == "aliased_import":
                    symbols.append(_text(c.child_by_field_name("name")) + " as " + _text(c.child_by_field_name("alias")))
                elif c.type == "wildcard_import":
                    symbols.append("*")
            self.res.imports.append(ImportRef(self.file_id, target, symbols=symbols))

    # -- bodies --------------------------------------------------------------
    def _body(self, node, cid, class_id, scope, qual=None) -> None:
        t = node.type
        if t == "function_definition" or t == "class_definition" or t == "decorated_definition":
            if cid is not None and qual is not None and t != "class_definition":
                target = node.child_by_field_name("definition") if t == "decorated_definition" else node
                if target is not None and target.type == "function_definition":
                    self._function(target, node, cid, qual, None, [], scope)
            return
        if t == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and right.type == "call":
                cls = _text(right.child_by_field_name("function")).split(".")[-1]
                if cls[:1].isupper():
                    if left.type == "identifier":
                        scope[_text(left)] = cls
            if left is not None and left.type == "attribute" and class_id is not None:
                obj = left.child_by_field_name("object")
                attr = _text(left.child_by_field_name("attribute"))
                if _text(obj) == "self" and attr:
                    self._field(class_id, attr, node, node)
        elif t == "call":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            argc = len(args.named_children) if args is not None else 0
            line = node.start_point[0] + 1
            if fn is not None and cid is not None and _text(fn) in LAUNCHERS and args is not None:
                found = _strings(args)
                stack = [args]
                while stack:
                    n = stack.pop()
                    if n.type == "identifier" and _text(n) in self.consts:
                        found.extend(self.consts[_text(n)])
                    stack.extend(n.children)
                if self.all_strings is None:
                    self.all_strings = _strings(self.tree.root_node)
                self.res.spawns.append(Spawn(cid, found, self.all_strings, "PIPE" in _text(args), line))
                if cid == self.top_id:
                    self.top_used = True
            if fn is not None and cid is not None:
                if fn.type == "identifier":
                    self.res.calls.append(CallSite(cid, _text(fn), None, None, argc, line, class_id, node.start_point[1]))
                elif fn.type == "attribute":
                    obj = fn.child_by_field_name("object")
                    name = _text(fn.child_by_field_name("attribute"))
                    otext = _text(obj)
                    if obj is not None and obj.type == "call" and _text(obj.child_by_field_name("function")) == "super":
                        self.res.calls.append(CallSite(cid, name, "base", None, argc, line, class_id, node.start_point[1]))
                    elif otext == "self":
                        self.res.calls.append(CallSite(cid, name, "this", None, argc, line, class_id, node.start_point[1]))
                    elif obj is not None and obj.type == "identifier":
                        self.res.calls.append(CallSite(cid, name, otext, scope.get(otext), argc, line, class_id, node.start_point[1]))
                    elif obj is not None and obj.type == "attribute" and _text(obj.child_by_field_name("object")) == "self":
                        attr = _text(obj.child_by_field_name("attribute"))
                        rtype = self.self_types.get(class_id or "", {}).get(attr)
                        self.res.calls.append(CallSite(cid, name, "." + attr, rtype, argc, line, class_id, node.start_point[1]))
                    else:
                        self.res.calls.append(CallSite(cid, name, "?", None, argc, line, class_id, node.start_point[1]))
        for c in node.children:
            self._body(c, cid, class_id, scope, qual)


def parse(repo: str, rel_path: str, file_id: str, src: bytes, module: str = "") -> FileResult:
    return _Walker(repo, rel_path, file_id, src).run()
