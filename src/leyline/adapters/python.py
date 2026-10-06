"""Python adapter: tree-sitter structure, imports, and call sites with simple receiver inference."""

from __future__ import annotations

import re
from typing import Optional

import tree_sitter_python
from tree_sitter import Language, Parser

from ..model import CallSite, Endpoint, FieldUse, Edge, FileResult, ImportRef, Node, Spawn, TypeRef

NAME = "tree-sitter-python"
VERSION = "0.1"
LANGUAGE = "python"
EXTENSIONS = (".py",)

_parser = Parser(Language(tree_sitter_python.language()))
LAUNCHERS = {"subprocess.Popen", "subprocess.run", "subprocess.check_output", "subprocess.check_call",
             "subprocess.call", "os.system", "os.popen", "Popen", "check_output", "check_call"}


HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}
ROUTE = re.compile(r"\.(route|get|post|put|delete|patch|websocket)\(\s*[rfbu]*['\"]([^'\"]*)['\"]")
FILE_WRITE = {"torch.save", "np.save", "np.savez", "np.savez_compressed", "numpy.save", "pickle.dump", "joblib.dump", "plt.savefig"}
FILE_READ = {"torch.load", "np.load", "numpy.load", "pickle.load", "joblib.load", "pd.read_csv", "np.loadtxt", "glob.glob",
             "os.listdir", "np.fromfile"}
PATH_BUILDERS = {"os.path.join", "Path", "pathlib.Path", "join", "glob.glob"}


def pathlike(s: str) -> bool:
    """A literal that looks like part of a file path: `.bin`, `runs/out.json`, `*.parity.json`."""
    if not s or len(s) > 120 or any(ch.isspace() for ch in s) or s.startswith(("http:", "https:")):
        return False
    return bool(re.fullmatch(r"\*?(\.[A-Za-z0-9_]{1,10}){1,2}", s)) or ("/" in s.strip("/") and "<" not in s)


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
        self.done_calls: dict = {}  # call span -> its call site
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
                   "body_line": body.start_point[0] + 1 if body is not None else None,
                   "is_fixture": any(re.search(r"\bfixture\b", d) for d in decorators) or None})
        self.res.nodes.append(fn_node)
        for d in decorators:
            m = ROUTE.search(d)
            if m and m.group(2).startswith("/"):
                self.res.endpoints.append(Endpoint("http", "serve", cid, m.group(2), outer.start_point[0] + 1,
                                                   None if m.group(1) in ("route", "websocket") else m.group(1).upper()))
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

    def _use(self, node, cid, class_id, scope) -> None:
        """Record obj.attr as a read or an assignment of a possible field."""
        parent = node.parent
        same = lambda a, b: a is not None and b is not None and a.start_byte == b.start_byte and a.end_byte == b.end_byte
        if parent is None or (parent.type == "call" and same(parent.child_by_field_name("function"), node)):
            return  # a method call
        obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
        if obj is None or attr is None:
            return
        name, otext = _text(attr), _text(obj)
        receiver = rtype = chain = None
        if otext == "self":
            receiver = "this"
        elif obj.type == "identifier":
            receiver, rtype = otext, scope.get(otext)
            chain = None if rtype else scope.get("~" + otext)
        elif obj.type == "attribute" and _text(obj.child_by_field_name("object")) == "self":
            inner = _text(obj.child_by_field_name("attribute"))
            receiver, rtype = "." + inner, self.self_types.get(class_id or "", {}).get(inner)
        elif obj.type == "call":
            receiver, chain = "?", self._site(obj, cid, class_id, scope)
        else:
            receiver = "?"
        cur, up = node, parent
        while up is not None and up.type in ("pattern_list", "tuple_pattern", "tuple", "parenthesized_expression"):
            cur, up = up, up.parent
        access = "r"
        if up is not None:
            if up.type == "assignment" and same(up.child_by_field_name("left"), cur):
                access = "w"
            elif up.type == "augmented_assignment" and same(up.child_by_field_name("left"), cur):
                access = "rw"
            elif up.type == "subscript" and same(up.child_by_field_name("value"), cur):
                outer = up.parent
                if outer is not None and outer.type in ("assignment", "augmented_assignment") and same(outer.child_by_field_name("left"), up):
                    access = "rw"  # self.x[i] = v changes what the field holds
        self.res.field_uses.append(FieldUse(cid, name, receiver, rtype, access, node.start_point[0] + 1, class_id, chain))

    def _site(self, node, cid, class_id, scope) -> Optional[CallSite]:
        """Record one call. A call used as the receiver of another is recorded once and shared."""
        key = (node.start_byte, node.end_byte)
        if key in self.done_calls:
            return self.done_calls[key]
        self.done_calls[key] = None
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is None:
            return None
        argc = len(args.named_children) if args is not None else 0
        line, col = node.start_point[0] + 1, node.start_point[1]
        site = None
        if fn.type == "identifier":
            site = CallSite(cid, _text(fn), None, None, argc, line, class_id, col)
        elif fn.type == "attribute":
            obj = fn.child_by_field_name("object")
            name = _text(fn.child_by_field_name("attribute"))
            otext = _text(obj)
            if obj is not None and obj.type == "call" and _text(obj.child_by_field_name("function")) == "super":
                site = CallSite(cid, name, "base", None, argc, line, class_id, col)
            elif otext == "self":
                site = CallSite(cid, name, "this", None, argc, line, class_id, col)
            elif obj is not None and obj.type == "identifier":
                known = scope.get(otext)
                site = CallSite(cid, name, otext, known, argc, line, class_id, col,
                                chain=None if known else scope.get("~" + otext))
            elif obj is not None and obj.type == "attribute" and _text(obj.child_by_field_name("object")) == "self":
                attr = _text(obj.child_by_field_name("attribute"))
                rtype = self.self_types.get(class_id or "", {}).get(attr)
                site = CallSite(cid, name, "." + attr, rtype, argc, line, class_id, col)
            elif obj is not None and obj.type == "call":
                site = CallSite(cid, name, "?", None, argc, line, class_id, col,
                                chain=self._site(obj, cid, class_id, scope))  # a.make().run()
            else:
                site = CallSite(cid, name, "?", None, argc, line, class_id, col)
        if site is not None:
            self.res.calls.append(site)
            self._endpoint(node, fn, args, cid, line)
        self.done_calls[key] = site
        return site

    def _endpoint(self, node, fn, args, cid, line) -> None:
        """Is this call one end of an HTTP or file channel?"""
        full = _text(fn)
        last = full.rsplit(".", 1)[-1]
        first = args.named_children[0] if args is not None and args.named_children else None
        if fn.type == "attribute" and last in HTTP_VERBS | {"open"} and first is not None and first.type == "string":
            path = "".join(_text(c) for c in first.children if c.type == "string_content")
            if path.startswith(("/", "http://", "https://")):
                self.res.endpoints.append(Endpoint("http", "call", cid, path, line, None if last == "open" else last.upper()))
                return
        role = None
        if full == "open" or full.endswith((".open",)) and last == "open" and fn.type == "attribute" and _text(fn.child_by_field_name("object")) in ("io", "codecs", "gzip"):
            mode = ""
            if args is not None:
                named = args.named_children
                if len(named) > 1 and named[1].type == "string":
                    mode = "".join(_text(c) for c in named[1].children if c.type == "string_content")
                for a in named:
                    if a.type == "keyword_argument" and _text(a.child_by_field_name("name")) == "mode":
                        mode = _text(a.child_by_field_name("value")).strip("'\"")
            role = "write" if any(ch in mode for ch in "wax+") else "read"
        elif full in FILE_WRITE or last in ("write_text", "write_bytes", "to_csv", "savefig", "to_json", "to_parquet"):
            role = "write"
        elif full in FILE_READ or last in ("read_text", "read_bytes", "read_csv", "read_json", "read_parquet"):
            role = "read"
        if role:
            self.res.endpoints.append(Endpoint("file", role, cid, "", line, None, _strings(args) if args is not None else []))
        if full in PATH_BUILDERS and args is not None:
            self.res.path_strings.setdefault(cid, []).extend(_strings(args))

    # -- bodies --------------------------------------------------------------
    def _body(self, node, cid, class_id, scope, qual=None) -> None:
        t = node.type
        if t == "function_definition" or t == "class_definition" or t == "decorated_definition":
            if cid is not None and qual is not None and t != "class_definition":
                target = node.child_by_field_name("definition") if t == "decorated_definition" else node
                if target is not None and target.type == "function_definition":
                    nested = [_squash(_text(d)) for d in node.children if d.type == "decorator"] if t == "decorated_definition" else []
                    self._function(target, node, cid, qual, None, nested, scope)
            return
        if t == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and right.type == "call":
                cls = _text(right.child_by_field_name("function")).split(".")[-1]
                if left.type == "identifier":
                    scope.pop("~" + _text(left), None)
                if cls[:1].isupper():
                    if left.type == "identifier":
                        scope[_text(left)] = cls
                elif left.type == "identifier" and cid is not None:
                    # `x = a.make()`: typed by what make returns, once calls are resolved.
                    site = self._site(right, cid, class_id, scope)
                    if site is not None:
                        scope.pop(_text(left), None)
                        scope["~" + _text(left)] = site
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
                self._site(node, cid, class_id, scope)
        elif t == "attribute" and cid is not None:
            self._use(node, cid, class_id, scope)
        elif t == "string" and cid is not None:
            text = "".join(_text(c) for c in node.children if c.type == "string_content")
            if pathlike(text):
                self.res.path_strings.setdefault(cid, []).append(text)
        elif t == "as_pattern" and cid is not None and node.named_child_count >= 2:
            # `with app.test_client() as c:` binds c to what the call returns.
            value, alias = node.named_children[0], node.named_children[-1]
            target = alias.named_children[0] if alias.type == "as_pattern_target" and alias.named_children else alias
            if value.type == "call" and target.type == "identifier":
                site = self._site(value, cid, class_id, scope)
                if site is not None:
                    scope.pop(_text(target), None)
                    scope["~" + _text(target)] = site
        for c in node.children:
            self._body(c, cid, class_id, scope, qual)


def parse(repo: str, rel_path: str, file_id: str, src: bytes, module: str = "") -> FileResult:
    return _Walker(repo, rel_path, file_id, src).run()
