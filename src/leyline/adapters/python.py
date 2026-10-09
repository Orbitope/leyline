"""Python adapter: tree-sitter structure, imports, and call sites with simple receiver inference."""

from __future__ import annotations

import re
from typing import Optional

import tree_sitter_python
from tree_sitter import Language, Parser

from .. import channels
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


def _url_text(node) -> str:
    """A string literal's text, with each f-string hole written {}: f"/api/users/{uid}" -> /api/users/{}."""
    return "".join(_text(c) if c.type == "string_content" else "{}"
                   for c in node.children if c.type in ("string_content", "interpolation"))


def _kw_string(args, name: str):
    """The string a keyword argument is given (`method="POST"`), or None."""
    for a in args.named_children if args is not None else ():
        if a.type == "keyword_argument" and _text(a.child_by_field_name("name")) == name:
            v = a.child_by_field_name("value")
            return _url_text(v) if v is not None and v.type == "string" else None
    return None


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
    """Class names in an annotation, without the typing wrappers around them: Iterator[Flask] -> [Flask].
    What `Callable[..., Flask]` or `type[Flask]` holds is not an instance of Flask, so those are left out."""
    m = re.search(r"\b(Callable|type|Type)\[", text)
    while m:
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"[": 1, "]": -1}.get(text[i], 0)
            i += 1
        text = text[:m.start()] + text[i:]
        m = re.search(r"\b(Callable|type|Type)\[", text)
    names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    return [x for x in names if x.lstrip("_")[:1].isupper() and x not in WRAPPERS]   # _Private classes too


def _class_name(fn) -> Optional[str]:
    """The class a call constructs, read off the callee: `Flask` or `flask.Flask`. A capitalised last name is
    taken for a class; the module path in front is kept so the class can be found through the import."""
    text = _text(fn)
    if fn is None or fn.type not in ("identifier", "attribute") or not text.rsplit(".", 1)[-1].lstrip("_")[:1].isupper():
        return None
    head, _, last = text.rpartition(".")
    if head and (head.split(".")[0] in ("self", "cls") or not re.fullmatch(r"[a-z_][\w.]*", head)):
        return last
    return text


CONTAINER = re.compile(r"(?:[A-Za-z_]\w*\.)?(list|List|set|Set|frozenset|FrozenSet|Sequence|MutableSequence|Iterable|"
                       r"Iterator|Collection|deque|Deque|AbstractSet|tuple|Tuple)\[(.+)\]")


def _element_type(text: Optional[str]) -> Optional[str]:
    """The class of what iterating a value of this annotation gives: list[Tag] -> Tag. None for anything else,
    including a tuple of mixed types."""
    m = CONTAINER.fullmatch((text or "").replace(" ", ""))
    if not m:
        return None
    inner = m.group(2)
    if m.group(1) in ("tuple", "Tuple"):
        if not inner.endswith(",..."):
            return None
        inner = inner[:-4]
    if "," in inner or "[" in inner:
        return None
    return (_annotation_types(inner) or [None])[0]


def _narrowing(cond) -> list[tuple]:
    """(name, class) for each `isinstance(name, Class)` a condition requires: alone or joined by `and`."""
    if cond is None:
        return []
    if cond.type == "parenthesized_expression" and cond.named_children:
        return _narrowing(cond.named_children[0])
    if cond.type == "boolean_operator" and any(c.type == "and" for c in cond.children):
        return _narrowing(cond.child_by_field_name("left")) + _narrowing(cond.child_by_field_name("right"))
    if cond.type != "call" or _text(cond.child_by_field_name("function")) != "isinstance":
        return []
    args = [a for a in (cond.child_by_field_name("arguments") or cond).named_children if a.type != "comment"]
    if len(args) == 2 and args[0].type == "identifier" and args[1].type in ("identifier", "attribute"):
        cls = _class_name(args[1])
        return [(_text(args[0]), cls)] if cls else []
    return []


OVERLOAD = re.compile(r"@(typing\.|t\.)?overload$")


# Methods that change the collection they are called on. A field used this way is written, not only read.
MUTATORS = frozenset("""append extend insert remove pop clear add update discard sort reverse setdefault popitem
popleft appendleft extendleft put put_nowait write writelines""".split())


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
        self.self_elems: dict[str, dict[str, str]] = {}  # class id -> attribute -> class of its items (list[Tag])
        self.done_calls: dict = {}  # call span -> its call site
        self.fn_nodes: dict[str, Node] = {}  # function id -> its node, for facts found in its body
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
                if left is not None and left.type == "identifier":
                    ann = st.named_children[0].child_by_field_name("type")
                    named = _annotation_types(_text(ann)) if ann is not None else []
                    made = _class_name(right.child_by_field_name("function")) if right is not None and right.type == "call" else None
                    if named or made:
                        self.res.var_types[_text(left)] = named[0] if named else made

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
        calls_before = len(self.res.calls)
        for c in node.children:
            t = c.type
            target = c
            decorators: list[str] = []
            if t == "decorated_definition":
                decorators = [_squash(_text(d)) for d in c.children if d.type == "decorator"]
                target = c.child_by_field_name("definition") or c.children[-1]
                t = target.type
                if any(OVERLOAD.match(d) for d in decorators):
                    continue   # a signature for type checkers; the implementation follows under the same name
            if top and t == "if_statement" and "TYPE_CHECKING" in _text(c.child_by_field_name("condition")):
                # Declarations only type checkers read: imports and classes that annotations elsewhere name.
                self._block(c.child_by_field_name("consequence"), parent_id, qual, class_id, cid, scope, top)
                continue
            if t in ("function_definition", "class_definition"):
                self._head(c, target, cid, scope)
            if t == "function_definition":
                self._function(target, c, parent_id, qual, class_id, decorators)
            elif t == "class_definition":
                self._class(target, c, parent_id, qual, decorators, cid)
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
                self._body(c, cid, None if cid == self.top_id else class_id, scope)
        if cid == self.top_id and any(c.src_id == cid for c in self.res.calls[calls_before:]):
            self.top_used = True

    def _class(self, node, outer, parent_id, qual, decorators, run_cid=None) -> None:
        """run_cid: the function whose code runs the class statement (the module body, at the top level).
        The class body runs there too, so its calls are made by that function."""
        name = _text(node.child_by_field_name("name"))
        qname = f"{qual}.{name}"
        tid = f"{self.repo}:python:{qname}"
        if tid in self.self_types:
            return   # defined again under the same name (in another branch): the first one stands
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
                if s.type == "call":
                    # class Manager(BaseManager.from_queryset(QuerySet)): a base made at run time, members unknown.
                    self.res.nodes[-1].attrs["open_base"] = True
                if s.type == "subscript":
                    s = s.child_by_field_name("value")   # a generic base: Mixin[T] is Mixin
                if s is not None and s.type in ("identifier", "attribute"):
                    # flask.Flask stays whole: the module in front says which Flask, when it was imported as a module.
                    self.res.type_refs.append(TypeRef(tid, [_text(s)], "base", s.start_point[0] + 1))
        self.self_types.setdefault(tid, {})
        if body is None:
            return
        # Class-level assignments are fields.
        for st in body.children:
            if st.type == "expression_statement" and st.named_child_count and st.named_children[0].type == "assignment":
                left = st.named_children[0].child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    self._field(tid, _text(left), st, st.named_children[0])
        self._block(body, tid, qname, tid, run_cid, {})

    def _field(self, tid, name, node, assignment=None, scope=None, cid=None) -> None:
        fid = f"{tid}.{name}"
        declared, cls, via, via_param = None, None, None, None
        if assignment is not None:
            ann = assignment.child_by_field_name("type")
            declared = _squash(_text(ann)) if ann is not None else None
            cls = (_annotation_types(declared) or [None])[0] if declared else None
            elem = _element_type(declared)
            if elem:
                self.self_elems.setdefault(tid, {}).setdefault(name, elem)
            right = assignment.child_by_field_name("right")
            if right is not None and right.type == "call":
                fn = right.child_by_field_name("function")
                made = _class_name(fn)
                if made and not cls:
                    cls = made
                    declared = declared or made
                elif fn is not None and fn.type == "attribute" and _text(fn.child_by_field_name("object")) == "self":
                    via = _text(fn.child_by_field_name("attribute"))   # self.config = self.make_config(): what it returns
            elif right is not None and right.type == "identifier" and scope:
                cls = cls or scope.get(_text(right))   # self.serializer = serializer, a parameter of a declared type
                if scope.get("^" + _text(right)) and cid:
                    via_param = [cid, _text(right)]   # and whatever callers pass for it
            if cls and name not in self.self_types.get(tid, {}):
                self.self_types.setdefault(tid, {})[name] = cls
        seen = next((n for n in self.res.nodes[-200:] if n.id == fid), None)
        if seen is not None:
            # Declared again (often `None` in one place, an instance in another): keep the first type found.
            if cls and not seen.attrs.get("type_name"):
                seen.attrs["type_name"], seen.attrs["declared_type"] = cls, seen.attrs.get("declared_type") or declared
            if via and not seen.attrs.get("type_call"):
                seen.attrs["type_call"] = via
            if via_param:
                seen.attrs["from_params"] = (seen.attrs.get("from_params") or []) + [via_param]
            return
        self.res.nodes.append(Node(
            id=fid, kind="field", name=name, parent_id=tid, language=LANGUAGE, path=self.path,
            span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
            attrs={"native_kind": "attribute", "declared_type": declared,
                   "type_name": self.self_types.get(tid, {}).get(name), "type_call": via,
                   "from_params": [via_param] if via_param else None,
                   "visibility": "private" if name.startswith("_") else "public", "is_mutable": True}))
        self.res.edges.append(Edge("has_field", tid, fid))

    def _function(self, node, outer, parent_id, qual, class_id, decorators, outer_scope=None) -> None:
        name = _text(node.child_by_field_name("name"))
        cid = f"{self.repo}:python:{qual}.{name}"
        params = node.child_by_field_name("parameters")
        body = node.child_by_field_name("body")
        scope: dict[str, str] = dict(outer_scope or {})  # a nested function sees the enclosing locals
        pnames: list[str] = []
        ptypes: dict[str, str] = {}
        required = total = 0
        variadic = False
        star_at = None   # how many named parameters come before *args
        is_method = class_id is not None and parent_id == class_id
        is_static = any("staticmethod" in d for d in decorators)
        if params is not None:
            first = True
            for p in params.named_children:
                pname, ptype, has_default = None, None, False
                if p.type == "typed_parameter" and p.named_children and p.named_children[0].type in (
                        "list_splat_pattern", "dictionary_splat_pattern"):
                    p = p.named_children[0]   # *args: Any is still *args
                if p.type == "identifier":
                    pname = _text(p)
                elif p.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
                    nm = p.child_by_field_name("name") or (p.named_children[0] if p.named_children else None)
                    pname = _text(nm)
                    ptype = p.child_by_field_name("type")
                    has_default = "default" in p.type
                elif p.type in ("list_splat_pattern", "dictionary_splat_pattern"):
                    variadic = True
                    if p.type == "list_splat_pattern" and star_at is None:
                        star_at = len(pnames)
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
                    scope.pop("[" + pname, None)
                    scope["^" + pname] = "1"
                if not has_default:
                    required += 1
                if ptype is not None:
                    elem = _element_type(_text(ptype))
                    if elem and pname:
                        scope["[" + pname] = elem
                    tn = _annotation_types(_text(ptype))
                    if tn:
                        scope[pname] = ptypes[pname] = tn[0]
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
                   "params": pnames, "returns": returns, "ptypes": ptypes or None, "star_at": star_at,
                   "body_line": body.start_point[0] + 1 if body is not None else None,
                   "is_fixture": any(re.search(r"\bfixture\b", d) for d in decorators) or None})
        self.res.nodes.append(fn_node)
        self.fn_nodes[cid] = fn_node
        for d in decorators:
            m = ROUTE.search(d)
            if m and m.group(2).startswith("/"):
                self.res.endpoints.append(Endpoint("http", "serve", cid, m.group(2), outer.start_point[0] + 1,
                                                   None if m.group(1) in ("route", "websocket") else m.group(1).upper()))
        if body is not None:
            self._body(body, cid, class_id if is_method else None, scope, qual=f"{qual}.{name}")
            inner = {_text(d.child_by_field_name("name")) for c in body.children
                     for d in [c.child_by_field_name("definition") if c.type == "decorated_definition" else c]
                     if d is not None and d.type == "function_definition"}
            made = next((x for x in self._return_names(body) if x in inner), None)
            if made:
                fn_node.attrs["returns_fn"] = made   # a decorator factory: `return decorator`, defined just above
            if returns is None:
                # No annotation: read the type off what is returned or yielded.
                kind, value = self._returned(body, scope)
                if kind == "type":
                    fn_node.attrs["returns"] = value
                elif kind == "call":
                    fn_node.attrs["returns_call"] = value

    def _return_names(self, body) -> list[str]:
        """Names returned as they are (`return decorator`), not looking into nested functions."""
        out, stack = [], list(body.children)
        while stack:
            n = stack.pop()
            if n.type in ("function_definition", "class_definition", "decorated_definition", "lambda"):
                continue
            if n.type == "return_statement" and n.named_children and n.named_children[0].type == "identifier":
                out.append(_text(n.named_children[0]))
            stack.extend(n.children)
        return out

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
                    if _class_name(fn):
                        return "type", _class_name(fn)
                    if fn is not None and fn.type == "attribute" and fn.child_by_field_name("object").type == "identifier":
                        return "call", [_text(fn.child_by_field_name("object")), last]
            stack.extend(n.children)
        return None, None

    def _import(self, node, src=None) -> None:
        """src: the function an import inside a function belongs to. The names it binds are that function's."""
        src = src or self.file_id
        if node.type == "import_statement":
            for c in node.named_children:
                if c.type == "dotted_name":
                    self.res.imports.append(ImportRef(src, _text(c), alias=_text(c).split(".")[0]))
                elif c.type == "aliased_import":
                    self.res.imports.append(ImportRef(
                        src, _text(c.child_by_field_name("name")),
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
            self.res.imports.append(ImportRef(src, target, symbols=symbols))

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
        if access == "r" and up is not None:   # self.x.append(v), self.x[k].add(v): a call that changes what the field holds
            c2, u2 = cur, up
            if u2.type == "subscript" and same(u2.child_by_field_name("value"), c2):
                c2, u2 = u2, u2.parent
            if u2 is not None and u2.type == "attribute" and same(u2.child_by_field_name("object"), c2) \
                    and u2.parent is not None and u2.parent.type == "call" and same(u2.parent.child_by_field_name("function"), u2) \
                    and _text(u2.child_by_field_name("attribute")) in MUTATORS:
                access = "rw"
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
        argc = sum(a.type != "comment" for a in args.named_children) if args is not None else 0
        site = self._callee(fn, argc, node.start_point[0] + 1, node.start_point[1], cid, class_id, scope)
        if site is not None and args is not None and args.type == "argument_list":
            site.args = self._hints(args, scope, class_id)
        if site is not None:
            self.res.calls.append(site)
            self._endpoint(node, fn, args, cid, site.line)
        self.done_calls[key] = site
        return site

    def _hints(self, args, scope, class_id) -> tuple:
        """What each positional argument is, for working out later what a parameter receives: a class name,
        `@self`, `@p:name` (a parameter of the caller), `@f:name` (a field of self), `@*` (*args passed on)."""
        out = []
        for a in args.named_children:
            if a.type == "comment":
                continue
            if a.type in ("keyword_argument", "dictionary_splat"):
                break
            text = _text(a)
            if a.type == "list_splat":
                out.append("@*")
            elif a.type == "identifier":
                out.append("@self" if text == "self" and class_id else "@p:" + text if scope.get("^" + text) else scope.get(text))
            elif a.type == "attribute" and _text(a.child_by_field_name("object")) == "self":
                out.append("@f:" + _text(a.child_by_field_name("attribute")))
            elif a.type == "call":
                out.append(_class_name(a.child_by_field_name("function")))
            else:
                out.append(None)
        return tuple(out)

    def _callee(self, fn, argc, line, col, cid, class_id, scope) -> Optional[CallSite]:
        """The call site for a callee expression: `f`, `self.f`, `x.f`, `a.make().f`."""
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
            elif obj is not None and obj.type == "attribute":
                # app.config.load(), flask.g.get(): typed by the attribute read in front, once that is resolved.
                inner = self._callee(obj, -1, line, col, cid, class_id, scope)
                if inner is not None:
                    inner.attr = True
                site = CallSite(cid, name, "?", None, argc, line, class_id, col, chain=inner)
            else:
                site = CallSite(cid, name, "?", None, argc, line, class_id, col)
        return site

    def _head(self, outer, fn, cid, scope, class_id=None) -> None:
        """Decorators and default values run where a function or class is defined, not inside it: their
        calls are made by the code around the definition (the module body, for a method)."""
        if cid is None:
            return
        for d in outer.children if outer.type == "decorated_definition" else ():
            expr = d.named_children[0] if d.type == "decorator" and d.named_children else None
            if expr is None:
                continue
            if expr.type in ("identifier", "attribute"):
                # `@cache`, `@app.before_request`: the decorator itself is called with the function.
                site = self._callee(expr, 1, expr.start_point[0] + 1, expr.start_point[1], cid, class_id, scope)
                if site is not None:
                    self.res.calls.append(site)
            else:
                self._body(expr, cid, class_id, scope)
                if expr.type == "call":
                    # `@app.route("/")`: what route returns is then called with the function.
                    site = self._site(expr, cid, class_id, scope)
                    if site is not None:
                        self.res.calls.append(CallSite(cid, "__call__", "?", None, 1, site.line, class_id, site.col, chain=site))
        params = fn.child_by_field_name("parameters") if fn.type == "function_definition" else None
        for p in params.named_children if params is not None else ():
            value = p.child_by_field_name("value") if p.type in ("default_parameter", "typed_default_parameter") else None
            if value is not None:
                self._body(value, cid, class_id, scope)

    def _endpoint(self, node, fn, args, cid, line) -> None:
        """Is this call one end of an HTTP or file channel?"""
        full = _text(fn)
        last = full.rsplit(".", 1)[-1]
        if node.parent is not None and node.parent.type == "decorator" and cid not in self.fn_nodes:
            # @app.post("/x") at the top of a module declares a route and requests nothing. Inside a test it is kept:
            # the test declares the route to call it, often as client.get() with the path left out.
            return
        first = args.named_children[0] if args is not None and args.named_children else None
        if fn.type == "attribute" and last in HTTP_VERBS | {"open"} and first is not None and first.type == "string":
            path = _url_text(first)
            if path.startswith(("/", "http://", "https://")):
                method = None if last == "open" else last.upper()
                if last == "open":   # a test client's open("/x", method="POST")
                    method = _kw_string(args, "method")
                    method = method.upper() if method else None
                self.res.endpoints.append(Endpoint("http", "call", cid, path, line, method))
                return
            if path.startswith("{}/") and len(path) > 3:   # f"{BASE}/api/x": the host is in a variable
                self.res.endpoints.append(Endpoint("http", "maybe", cid, path[2:].split("?", 1)[0], line,
                                                   None if last == "open" else last.upper()))
                return
        if fn.type == "attribute" and last in HTTP_VERBS and first is not None and first.type == "binary_operator" \
                and _text(first.child_by_field_name("operator")) == "+":
            right = first.child_by_field_name("right")
            path = _url_text(right) if right is not None and right.type == "string" else ""
            if path.startswith("/") and path.count("/") >= 2:   # BASE_URL + "/api/x"
                self.res.endpoints.append(Endpoint("http", "maybe", cid, path.split("?", 1)[0], line, last.upper()))
                return
        if self._wrapped_request(fn, full, last, args, cid, line):
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

    def _wrapped_request(self, fn, full, last, args, cid, line) -> bool:
        """A request through something the name does not give away: a session helper's request("GET", "/x"), an
        api_get("/api/x/1") of the project's own, a url= keyword. Recorded as a path a route may serve; the indexer
        links it only when exactly one route fits (see Indexer._resolve_endpoints)."""
        if args is None or full in FILE_WRITE | FILE_READ | PATH_BUILDERS | LAUNCHERS or full == "open" \
                or last in ("join", "open", "exists", "glob", "rglob", "joinpath", "startswith", "endswith", "split",
                            "replace", "format", "match", "search", "sub", "compile", "fullmatch", "findall", "strip",
                            "rstrip", "lstrip", "removeprefix", "removesuffix", "print", "info", "debug", "warning", "error",
                            "route", "websocket", "add_url_rule", "add_api_route", "api_route", "url_for", "redirect",
                            "Blueprint", "APIRouter", "Rule", "path", "re_path", "mount", "include_router"):
            return False
        named = args.named_children
        method, url = None, None
        if last == "request" and len(named) > 1 and named[0].type == "string" and named[1].type == "string":
            verb = _url_text(named[0])
            if verb.lower() in HTTP_VERBS:
                method, url = verb.upper(), _url_text(named[1])
        if url is None and named and named[0].type == "string":
            url = _url_text(named[0])
        if url is None:
            url = _kw_string(args, "url") or _kw_string(args, "path")
        if not url:
            return False
        url = url[2:] if url.startswith("{}/") else url
        if not url.startswith("/") or url.count("/") < 2 or any(ch.isspace() for ch in url) or "." in url.rsplit("/", 1)[-1]:
            return False
        if method is None:
            m = _kw_string(args, "method")
            method = m.upper() if m and m.lower() in HTTP_VERBS else None
        self.res.endpoints.append(Endpoint("http", "maybe", cid, url.split("?", 1)[0], line, method))
        return True

    # -- bodies --------------------------------------------------------------
    def _body(self, node, cid, class_id, scope, qual=None) -> None:
        t = node.type
        if t == "function_definition" or t == "class_definition" or t == "decorated_definition":
            if cid is not None and qual is not None:
                target = node.child_by_field_name("definition") if t == "decorated_definition" else node
                nested = [_squash(_text(d)) for d in node.children if d.type == "decorator"] if t == "decorated_definition" else []
                if target is not None and target.type == "function_definition":
                    if any(OVERLOAD.match(d) for d in nested):
                        return
                    self._head(node, target, cid, scope, class_id)
                    self._function(target, node, cid, qual, None, nested, scope)
                elif target is not None and target.type == "class_definition":
                    # A class defined inside a function (common in tests: a Flask subclass for one case).
                    self._head(node, target, cid, scope, class_id)
                    self._class(target, node, cid, qual, nested, cid)
            return
        if t in ("import_statement", "import_from_statement"):
            self._import(node, None if cid in (None, self.top_id) else cid)   # in a try block, or inside a function
        elif t == "assignment":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if right is not None:
                # The value is worked out before the name is bound: in `app = Wrap(app)` the argument is the old app.
                self._body(right, cid, class_id, scope, qual)
            if left is not None and left.type == "identifier":
                scope.pop("^" + _text(left), None)   # no longer the parameter's value
            if left is not None and right is not None and right.type == "call":
                cls = _class_name(right.child_by_field_name("function"))
                if left.type == "identifier":
                    scope.pop("~" + _text(left), None)
                if cls:
                    if left.type == "identifier":
                        scope[_text(left)] = cls
                elif left.type == "identifier" and cid is not None:
                    # `x = a.make()`: typed by what make returns, once calls are resolved.
                    site = self._site(right, cid, class_id, scope)
                    if site is not None:
                        scope.pop(_text(left), None)
                        scope["~" + _text(left)] = site
            elif left is not None and left.type == "identifier" and right is not None and right.type == "attribute" and cid is not None:
                # `app = ctx.app`: typed by the attribute read, once that is resolved.
                inner = self._callee(right, -1, right.start_point[0] + 1, right.start_point[1], cid, class_id, scope)
                scope.pop(_text(left), None)
                scope.pop("~" + _text(left), None)
                if inner is not None:
                    inner.attr = True
                    scope["~" + _text(left)] = inner
            if left is not None and left.type == "attribute" and class_id is not None:
                obj = left.child_by_field_name("object")
                attr = _text(left.child_by_field_name("attribute"))
                if _text(obj) == "self" and attr:
                    self._field(class_id, attr, node, node, scope, cid)
            elif left is not None and left.type in ("pattern_list", "tuple_pattern", "list_pattern") and class_id is not None:
                # self.w, self.h = w, h: each is a field, of no type the right side says outright.
                for el in left.named_children:
                    if el.type == "attribute" and _text(el.child_by_field_name("object")) == "self":
                        self._field(class_id, _text(el.child_by_field_name("attribute")), node)
            for c in node.children:
                if right is None or c.start_byte != right.start_byte or c.end_byte != right.end_byte:
                    self._body(c, cid, class_id, scope, qual)
            return
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
            if fn is not None and _text(fn) in ("isinstance", "callable") and args is not None and args.named_children \
                    and args.named_children[0].type == "identifier" and cid in self.fn_nodes:
                # `isinstance(x, Foo)`: the code branches on what x is, so what callers pass says little about
                # which branch a call on x sits in.
                narrowed = self.fn_nodes[cid].attrs.setdefault("narrowed", [])
                if _text(args.named_children[0]) not in narrowed:
                    narrowed.append(_text(args.named_children[0]))
        elif t == "attribute" and cid is not None:
            self._use(node, cid, class_id, scope)
        elif t == "string" and cid is not None:
            text = "".join(_text(c) for c in node.children if c.type == "string_content")
            if pathlike(text):
                self.res.path_strings.setdefault(cid, []).append(text)
        elif t == "if_statement" and cid is not None and _narrowing(node.child_by_field_name("condition")):
            # `if isinstance(rhs, Query): rhs.add_q(...)`: inside the branch, rhs is a Query.
            cond, body = node.child_by_field_name("condition"), node.child_by_field_name("consequence")
            self._body(cond, cid, class_id, scope, qual)
            saved = {}
            for name, cls in _narrowing(cond):
                for k in (name, "~" + name):
                    saved[k] = scope.pop(k, None)
                scope[name] = cls
            if body is not None:
                self._body(body, cid, class_id, scope, qual)
            for k, v in saved.items():
                scope.pop(k, None)
                if v is not None:
                    scope[k] = v
            for c in node.children:
                if c.type in ("elif_clause", "else_clause"):
                    self._body(c, cid, class_id, scope, qual)
            return
        elif t == "for_statement" and cid is not None:
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and left.type == "identifier" and right is not None:
                # `for tag in self.order`, with `order: list[JSONTag]`: each tag is a JSONTag.
                elem = None
                if right.type == "identifier":
                    elem = scope.get("[" + _text(right))
                elif right.type == "attribute" and _text(right.child_by_field_name("object")) == "self":
                    elem = self.self_elems.get(class_id or "", {}).get(_text(right.child_by_field_name("attribute")))
                name = _text(left)
                for k in (name, "~" + name, "^" + name, "[" + name):
                    scope.pop(k, None)
                if elem:
                    scope[name] = elem
        elif t == "with_item" and cid is not None and node.named_children:
            # `with app.app_context():` runs the context manager's __enter__ and __exit__.
            value = node.named_children[0]
            if value.type == "as_pattern" and value.named_children:
                value = value.named_children[0]
            recv, rtype, chain = "?", None, None
            if value.type == "call":
                chain = self._site(value, cid, class_id, scope)
            elif value.type == "identifier":
                recv, rtype = _text(value), scope.get(_text(value))
                chain = None if rtype else scope.get("~" + recv)
            elif value.type == "attribute" and _text(value.child_by_field_name("object")) == "self":
                attr = _text(value.child_by_field_name("attribute"))
                recv, rtype = "." + attr, self.self_types.get(class_id or "", {}).get(attr)
            if recv != "?" or chain is not None:
                line, col = value.start_point[0] + 1, value.start_point[1]
                for name, argc in (("__enter__", 0), ("__exit__", 3)):
                    self.res.calls.append(CallSite(cid, name, recv, rtype, argc, line, class_id, col, chain=chain))
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
    w = _Walker(repo, rel_path, file_id, src)
    res = w.run()
    channels.extract(LANGUAGE, w.tree, res, file_id, w.consts)
    return res
