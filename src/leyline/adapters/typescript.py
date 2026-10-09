"""TypeScript and JavaScript adapter: tree-sitter structure, imports, call sites, JSX, and inline tests.

A file is a module. Functions, classes, interfaces, type aliases and enums become nodes; so does a
`const` that holds a function, and a top-level `const` object whose properties are functions (it is
treated as a type, so `api.load()` finds `load`). Anonymous callbacks are not nodes: what they call is
attributed to the function they are written in. Two exceptions: an inline test (`it("x", () => ...)`), and the
inline handler of a server route (`server.get("/api/x", async (req) => ...)`), which becomes a function named for its
route (`GET /api/x`), nested in the function that registers it.
"""

from __future__ import annotations

import posixpath
import re
import os
import sys
from typing import Optional

import tree_sitter_typescript
from tree_sitter import Language, Parser

from .. import channels
from ..model import CallSite, Edge, Endpoint, FieldUse, FileResult, ImportRef, Node, Spawn, TypeRef
from .python import pathlike

NAME = "tree-sitter-typescript"
VERSION = "0.1"
LANGUAGE = "typescript"
EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
CTOR = "constructor"

# Deeply nested JSX and long call chains recurse deeply. Python before 3.12 on Windows does not stop a recursion
# before the main thread's stack runs out (the process dies), so there the default limit stays, and a file nested
# past it is reported (see indexer._parse_file).
if sys.getrecursionlimit() < 6000 and not (os.name == "nt" and sys.version_info < (3, 12)):
    sys.setrecursionlimit(6000)

_ts = Parser(Language(tree_sitter_typescript.language_typescript()))
_tsx = Parser(Language(tree_sitter_typescript.language_tsx()))

_EXT = re.compile(r"\.(d\.[cm]?ts|[cm]?[jt]sx?)$")
FUNCS = ("arrow_function", "function_expression", "generator_function", "function")
# Calls that hand back the function they are given, so `const x = useCallback(() => ..., [])` declares x.
PASS_THROUGH = {"memo", "forwardRef", "useCallback", "debounce", "throttle", "observer", "action", "cache"}
TEST_FNS = {"it", "test", "bench", "specify"}
SUITE_FNS = {"describe", "suite", "context"}
TEST_MODS = {"only", "skip", "concurrent", "sequential", "fails", "todo", "skipIf", "runIf", "each", "for"}
HTTP_VERBS = {"get", "post", "put", "delete", "patch", "head", "options"}
SERVERS = re.compile(r"(app|server|fastify|router|instance|api|route[rs]?)$", re.I)
CLIENTS = re.compile(r"(axios|http|client|request|ky|got|api)$", re.I)
FS_READ = {"readFile", "readFileSync", "readdir", "readdirSync", "createReadStream", "readJson", "readJsonSync", "opendir"}
FS_WRITE = {"writeFile", "writeFileSync", "appendFile", "appendFileSync", "createWriteStream", "writeJson",
            "writeJsonSync", "copyFile", "copyFileSync", "outputFile"}
LAUNCHERS = {"spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"}
MAIN_NAMES = {"main", "run", "start", "bootstrap", "cli", "serve"}
# Objects every JavaScript program is given. A call on one of them is never a call into this code.
GLOBALS = frozenset("""JSON Math Object Array Promise console Number String Boolean Date Reflect Symbol Intl Error RegExp
window document process Buffer globalThis navigator localStorage sessionStorage crypto performance history location
URL URLSearchParams Map Set WeakMap WeakSet Atomics BigInt Proxy structuredClone AbortSignal TextEncoder TextDecoder
expect vi jest React ReactDOM fs path os util http https url child_process zlib stream events assert""".split())
# Method names so common on built-in and library objects that sharing one says nothing about which function is meant.
COMMON_METHODS = frozenset("""push pop shift unshift splice slice map filter reduce forEach find findIndex some every includes
indexOf join keys values entries get set has add delete clear parse stringify log warn error info debug replace replaceAll
at split trim match test exec then catch finally toString call apply bind sort reverse concat flat flatMap fill from of
assign freeze click press type goto evaluate close open send status json text code header on off once emit write read end
render resolve reject all race now count first last nth focus blur hover check change wait run start stop next size append
remove insert update create load save pause play subscribe unsubscribe dispatch fetch search reset init request abort move
select reload route observe disconnect decode encode post put patch head options listen use register name value label
toJSON valueOf equals compare clone copy merge format print show hide toggle enable disable validate""".split())
# Methods that change the collection they are called on. A field used this way is written, not only read.
MUTATORS = frozenset("push pop shift unshift splice sort reverse set delete add clear fill copyWithin".split())
WRAPPERS = {"Promise", "Array", "ReadonlyArray", "Readonly", "Partial", "Required", "Pick", "Omit", "Record", "Map", "Set",
            "WeakMap", "WeakSet", "Awaited", "NonNullable", "ReturnType", "Parameters", "Iterable", "Iterator",
            "AsyncIterable", "AsyncIterator", "Generator", "AsyncGenerator", "Exclude", "Extract", "Uint8Array", "Buffer",
            "Date", "Error", "RegExp", "Function", "Object", "String", "Number", "Boolean", "Symbol", "JSX", "React",
            "ReactNode", "ReactElement", "Element", "FC", "PropsWithChildren", "Dispatch", "SetStateAction", "RefObject",
            "MutableRefObject", "T", "K", "V", "U"}


def _text(node) -> str:
    return node.text.decode("utf8", "replace") if node is not None else ""


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def module_path(rel_path: str) -> str:
    return _EXT.sub("", rel_path).replace("/", ".")


def _string(node) -> Optional[str]:
    """The text of a string or template literal; substitutions become {}."""
    if node is None:
        return None
    if node.type == "string":
        return "".join(_text(c) for c in node.children if c.type in ("string_fragment", "escape_sequence"))
    if node.type == "template_string":
        return "".join(_text(c) if c.type in ("string_fragment", "escape_sequence") else "{}"
                       for c in node.children if c.type in ("string_fragment", "escape_sequence", "template_substitution"))
    return None


def _strings(node) -> list[str]:
    out, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type in ("string", "template_string"):
            s = _string(n)
            if s:
                out.append(s)
        else:
            stack.extend(reversed(n.children))
    return out


def _type_names(node) -> list[str]:
    """Named types in a type expression, without the wrappers around them: Promise<Foo[]> -> [Foo]."""
    out, stack = [], [node] if node is not None else []
    while stack:
        n = stack.pop()
        if n.type == "type_identifier":
            if _text(n) not in WRAPPERS:
                out.append(_text(n))
        elif n.type == "nested_type_identifier":
            last = _text(n).rsplit(".", 1)[-1]
            if last not in WRAPPERS:
                out.append(last)
        elif n.type not in ("object_type", "function_type", "literal_type", "type_query"):
            stack.extend(reversed(n.children))
    return out


def _unwrap(node):
    while node is not None and node.type in ("parenthesized_expression", "non_null_expression", "as_expression",
                                              "satisfies_expression", "await_expression", "type_assertion"):
        inner = [c for c in node.named_children if c.type != "comment"]
        if not inner:
            break
        node = inner[-1] if node.type == "type_assertion" else inner[0]
    return node


IMPORT_TYPE = re.compile(r"typeof\s+import\(\s*['\"]([^'\"]+)['\"]\s*\)")


def _dynamic_import(node) -> Optional[str]:
    """The module named by `import("x")` or `await import("x")`, if that is what this expression is."""
    node = _unwrap(node)
    if node is not None and node.type == "call_expression":
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        if fn is not None and fn.type == "import" and args is not None and args.named_children:
            return _string(args.named_children[0])
    return None


def _pattern_names(pattern) -> list[tuple[str, str]]:
    """(name taken, local name) for each simple entry of an object pattern."""
    out = []
    for p in pattern.named_children:
        if p.type == "shorthand_property_identifier_pattern":
            out.append((_text(p), _text(p)))
        elif p.type == "pair_pattern":
            key, value = p.child_by_field_name("key"), p.child_by_field_name("value")
            if key is not None and value is not None and value.type == "identifier":
                out.append((_text(key), _text(value)))
    return out


def _arity(arg) -> Optional[int]:
    """How many parameters a callback argument takes; None when the argument is not a function."""
    fn = _unwrap(arg)
    if fn is None or fn.type not in FUNCS:
        return None
    params = fn.child_by_field_name("parameters")
    return len([p for p in params.named_children if p.type != "comment"]) if params is not None else 1


def _same(a, b) -> bool:
    return a is not None and b is not None and a.start_byte == b.start_byte and a.end_byte == b.end_byte


class _Walker:
    def __init__(self, repo: str, rel_path: str, file_id: str, src: bytes):
        self.repo, self.path, self.file_id, self.src = repo, rel_path, file_id, src
        self.mod = module_path(rel_path)
        self.res = FileResult(declares=[_EXT.sub("", rel_path)])
        ext = rel_path.rsplit(".", 1)[-1]
        self.tree = (_ts if ext in ("ts", "mts", "cts") else _tsx).parse(src)
        self.top_id = f"{repo}:typescript:{self.mod}.<module>"
        self.top_used = False
        self.entry = False
        self.ids: set[str] = set()
        self.by_id: dict[str, Node] = {}
        self.self_types: dict[str, dict[str, str]] = {}
        self.done_calls: dict = {}
        self.redirect: dict = {}       # callback span -> (test id, qualified name)
        self.suites: list[str] = []
        self.consts: dict[str, list[str]] = {}
        self.all_strings: Optional[list[str]] = None
        self.default_name: Optional[str] = None
        self.exported: set[str] = set()
        self.module_aliases: set[str] = set()   # type X = typeof import("./y"): X stands for the module
        self.stores: set[str] = set()
        base = rel_path.rsplit("/", 1)[-1]
        self.is_test_file = bool(re.search(r"\.(test|spec|bench)\.[cm]?[jt]sx?$", base)) or "/__tests__/" in "/" + rel_path
        self.is_jsx = ext in ("tsx", "jsx")
        # Names that are functions somewhere in this file, or come from an import: a bare mention of one
        # as an argument (onClick={save}, items.map(render)) hands the function over to be called.
        self.fn_names: set[str] = set()
        self.fn_nodes: dict = {}       # name -> the function's node, for a program path a helper of the file builds
        self.imported: set[str] = set()
        self._prescan(self.tree.root_node)

    def _prescan(self, root) -> None:
        stack = [root]
        while stack:
            n = stack.pop()
            t = n.type
            if t in ("function_declaration", "generator_function_declaration"):
                self.fn_names.add(_text(n.child_by_field_name("name")))
                self.fn_nodes.setdefault(_text(n.child_by_field_name("name")), n)
            elif t == "variable_declarator":
                name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
                if name is not None and name.type == "identifier" and self._fn_of(value) is not None:
                    self.fn_names.add(_text(name))
                    self.fn_nodes.setdefault(_text(name), self._fn_of(value))
                elif name is not None and name.type == "identifier" and value is not None and n.parent is not None \
                        and n.parent.parent is not None and n.parent.parent.type in ("program", "export_statement"):
                    found = _strings(value) if value.type in ("string", "template_string", "array") else []
                    if found:
                        self.consts[_text(name)] = found
            elif t == "import_statement":
                for c in n.children:
                    if c.type == "import_clause":
                        for d in c.children:
                            if d.type == "identifier":
                                self.imported.add(_text(d))
                            elif d.type == "named_imports":
                                for s in d.named_children:
                                    if s.type == "import_specifier":
                                        self.imported.add(_text(s.child_by_field_name("alias") or s.child_by_field_name("name")))
                continue
            stack.extend(n.children)

    def _helper_paths(self, call, arg_nodes) -> Optional[list[str]]:
        """The paths a launch call's program comes from when a function of this file computes it: an argument (or a
        local variable it names) calls `hostEntry()`, which returns `resolve(__dirname, "..", "host", "index.js")`.
        None when no argument calls a function of this file; otherwise the paths those functions build, relative to
        the repository root (possibly none)."""
        scope = call.parent
        while scope is not None and scope.type not in FUNCS and scope.type not in ("function_declaration", "generator_function_declaration",
                                                                                   "method_definition", "program"):
            scope = scope.parent
        decls: dict = {}
        stack = [scope] if scope is not None else []
        while stack:
            n = stack.pop()
            if n.type == "variable_declarator" and n.child_by_field_name("name") is not None:
                decls.setdefault(_text(n.child_by_field_name("name")), n.child_by_field_name("value"))
            stack.extend(n.named_children)
        helpers, stack, looked = [], list(arg_nodes), set()
        while stack:
            n = stack.pop()
            if n is None:
                continue
            if n.type == "identifier" and _text(n) in decls and _text(n) not in looked:
                looked.add(_text(n))
                stack.append(decls[_text(n)])
                continue
            if n.type == "call_expression":
                f = _unwrap(n.child_by_field_name("function"))
                if f is not None and f.type == "identifier" and _text(f) in self.fn_nodes:
                    helpers.append(self.fn_nodes[_text(f)])
                    continue
            if n.type not in FUNCS:
                stack.extend(n.named_children)
        if not helpers:
            return None
        here = self.path.rsplit("/", 1)[0] if "/" in self.path else ""
        out = []
        for fn in helpers:
            body = fn.child_by_field_name("body")
            exprs = [body] if body is not None and body.type != "statement_block" else []   # () => resolve(...)
            stack = [body] if body is not None else []
            while stack:
                n = stack.pop()
                if n.type == "return_statement":
                    exprs.extend(n.named_children)
                elif n.type not in FUNCS:
                    stack.extend(n.named_children)
            for e in exprs:
                e = _unwrap(e)
                if e is None or e.type != "call_expression":
                    continue
                f, a = e.child_by_field_name("function"), e.child_by_field_name("arguments")
                if f is None or a is None or _text(f).rsplit(".", 1)[-1] not in ("resolve", "join"):
                    continue
                parts = [x for x in a.named_children if x.type != "comment"]
                if not parts or _text(parts[0]) not in ("__dirname", "import.meta.dirname"):
                    continue
                bits = [_string(x) for x in parts[1:]]
                if not bits or any(b is None for b in bits):
                    continue
                segs: Optional[list] = []
                for seg in "/".join([here] + bits).split("/"):
                    if seg in ("", "."):
                        continue
                    if seg != "..":
                        segs.append(seg)
                    elif segs:
                        segs.pop()
                    else:
                        segs = None    # above the repository root
                        break
                if segs:
                    out.append("/".join(segs))
        return list(dict.fromkeys(out))

    def _fn_of(self, value):
        """The function a declarator's value amounts to, if any."""
        value = _unwrap(value)
        if value is None:
            return None
        if value.type in FUNCS:
            return value
        if value.type == "call_expression":
            fn = value.child_by_field_name("function")
            args = value.child_by_field_name("arguments")
            if fn is not None and args is not None and _text(fn).rsplit(".", 1)[-1] in PASS_THROUGH:
                first = next((a for a in args.named_children if a.type != "comment"), None)
                first = _unwrap(first)
                if first is not None and first.type in FUNCS:
                    return first
        return None

    def run(self) -> FileResult:
        root = self.tree.root_node
        if self.src.startswith(b"#!"):
            self.entry = True
        self._walk_children(root, self.top_id, None, {}, self.mod, True)
        if self.default_name:
            for n in self.res.nodes:
                if n.name == self.default_name and n.parent_id == self.file_id:
                    n.attrs["is_default_export"] = True
                    break
        for n in self.res.nodes:
            if n.parent_id == self.file_id and n.name in self.exported:
                n.attrs["visibility"] = "public"
        if self.entry:
            self.top_used = True
            self.res.nodes.append(Node(
                id=self.top_id + "#entry", kind="entry_point", name=self.path, parent_id=self.file_id,
                language=LANGUAGE, path=self.path, span_start=1, span_end=root.end_point[0] + 1,
                attrs={"trigger": "ui" if self.is_jsx else "cli", "address": self.path}))
            self.res.edges.append(Edge("exposes", self.top_id + "#entry", self.top_id))
        if self.top_used:
            self.res.nodes.append(Node(
                id=self.top_id, kind="callable", name="<module>", parent_id=self.file_id,
                language=LANGUAGE, path=self.path, span_start=1, span_end=root.end_point[0] + 1,
                attrs={"signature": f"module {self.path}", "visibility": "public",
                       "native_kind": "module_body", "argc_min": 0, "argc_max": 0}))
        return self.res

    def _add(self, node: Node) -> bool:
        if node.id in self.ids:
            return False
        self.ids.add(node.id)
        self.by_id[node.id] = node
        self.res.nodes.append(node)
        if node.parent_id == self.top_id:
            self.top_used = True
        return True

    # -- walking ---------------------------------------------------------------
    def _walk_children(self, node, cid, class_id, scope, qual, top=False) -> None:
        for c in node.children:
            self._walk(c, cid, class_id, scope, qual, top)

    def _walk(self, node, cid, class_id, scope, qual, top=False, exported=False) -> None:
        t = node.type
        if t == "comment":
            return
        if t == "import_statement":
            self._import(node)
            return
        if t == "export_statement":
            self._export(node, cid, class_id, scope, qual, top)
            return
        if t in ("function_declaration", "generator_function_declaration"):
            name = _text(node.child_by_field_name("name")) or "default"
            self._function(name, node, node, self.file_id if top else cid, qual, None, scope, exported=exported)
            return
        if t in ("class_declaration", "abstract_class_declaration", "class"):
            name = _text(node.child_by_field_name("name"))
            if name:
                self._class(name, node, self.file_id if top else cid, qual, exported)
                return
        if t in ("interface_declaration", "type_alias_declaration", "enum_declaration"):
            self._typeish(node, self.file_id if top else cid, qual, exported)
            return
        if t in ("lexical_declaration", "variable_declaration"):
            for d in node.named_children:
                if d.type == "variable_declarator":
                    self._declarator(d, node, cid, class_id, scope, qual, top, exported)
            return
        if t in ("function_signature", "ambient_declaration", "internal_module", "module"):
            if t in ("ambient_declaration", "internal_module", "module"):
                self._walk_children(node, cid, class_id, scope, qual, top)
            return
        if t in FUNCS:
            self._callback(node, cid, class_id, scope, qual)
            return
        if t == "call_expression":
            self._call_node(node, cid, class_id, scope, qual, top)
            return
        if t == "new_expression":
            self._new(node, cid, class_id, scope)
        elif t == "member_expression":
            self._use(node, cid, class_id, scope)
        elif t in ("jsx_opening_element", "jsx_self_closing_element"):
            self._jsx(node, cid, class_id)
        elif t == "assignment_expression":
            self._assign(node, cid, class_id, scope)
        elif t in ("string", "template_string"):
            s = _string(node)
            if s and pathlike(s):
                self.res.path_strings.setdefault(cid, []).append(s)
            if t == "string":
                return
        elif t in ("jsx_expression", "pair", "shorthand_property_identifier"):
            self._reference(node, cid, class_id, scope)
        elif t == "object" and cid is not None:
            self._object_members(node, cid, class_id, scope, qual)
            return
        self._walk_children(node, cid, class_id, scope, qual, top and t in ("expression_statement", "await_expression"))

    def _callback(self, fn, cid, class_id, scope, qual) -> None:
        """An anonymous function: what it does is attributed to the function it is written in."""
        key = (fn.start_byte, fn.end_byte)
        if key in self.redirect:
            cid, qual = self.redirect[key]
        inner = dict(scope)
        self._params(fn, cid, inner, declare=False)
        body = fn.child_by_field_name("body")
        if body is not None:
            self._walk(body, cid, class_id, inner, qual)

    def _export(self, node, cid, class_id, scope, qual, top) -> None:
        source = node.child_by_field_name("source")
        is_default = any(c.type == "default" for c in node.children)
        if source is not None:
            target = self._spec(_string(source) or "")
            clause = next((c for c in node.children if c.type == "export_clause"), None)
            ns = next((c for c in node.children if c.type == "namespace_export"), None)
            if clause is not None:
                symbols = []
                for s in clause.named_children:
                    if s.type == "export_specifier":
                        name, alias = _text(s.child_by_field_name("name")), _text(s.child_by_field_name("alias"))
                        symbols.append(f"{name} as {alias}" if alias and alias != name else name)
                self.res.imports.append(ImportRef(self.file_id, target, symbols=symbols))
            elif ns is not None:
                self.res.imports.append(ImportRef(self.file_id, target, alias=_text(ns.named_children[0]) if ns.named_children else None))
            else:
                self.res.imports.append(ImportRef(self.file_id, target, symbols=["*"]))
            return
        decl = node.child_by_field_name("declaration")
        value = node.child_by_field_name("value")
        if decl is not None:
            if is_default:
                self.default_name = _text(decl.child_by_field_name("name")) or "default"
            self._walk(decl, cid, class_id, scope, qual, top, exported=True)
        elif value is not None:
            inner = _unwrap(value)
            if inner.type == "identifier":
                self.default_name = _text(inner)
                return
            fn = self._fn_of(inner)
            if fn is not None:
                self.default_name = _text(fn.child_by_field_name("name")) or "default"
                self._function(self.default_name, fn, node, self.file_id, qual, None, scope, exported=True)
            elif inner.type == "class":   # export default class { ... }: named default, as a function would be
                self.default_name = _text(inner.child_by_field_name("name")) or "default"
                self._class(self.default_name, inner, self.file_id, qual, True)
            else:
                self._walk(value, cid, class_id, scope, qual)
        else:
            for c in node.children:
                if c.type == "export_clause":
                    for s in c.named_children:
                        if s.type == "export_specifier":
                            name, alias = _text(s.child_by_field_name("name")), _text(s.child_by_field_name("alias"))
                            if alias == "default":
                                self.default_name = name
                            self.exported.add(name)

    # -- imports ---------------------------------------------------------------
    def _spec(self, spec: str) -> str:
        """A relative specifier becomes ./<path from the repo root, without extension>; a package name stays."""
        if spec.startswith("."):
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(self.path), spec))
            return "./" + _EXT.sub("", joined)
        return spec

    def _import(self, node) -> None:
        source = node.child_by_field_name("source")
        req = next((c for c in node.children if c.type == "import_require_clause"), None)
        if req is not None:
            src = next((c for c in req.children if c.type == "string"), None)
            name = next((c for c in req.children if c.type == "identifier"), None)
            self.res.imports.append(ImportRef(self.file_id, self._spec(_string(src) or ""), alias=_text(name) or None))
            return
        if source is None:
            return
        target = self._spec(_string(source) or "")
        symbols, alias = [], None
        for c in node.children:
            if c.type != "import_clause":
                continue
            for d in c.children:
                if d.type == "identifier":
                    symbols.append(f"default as {_text(d)}")
                elif d.type == "namespace_import":
                    alias = _text(next((x for x in d.children if x.type == "identifier"), None)) or None
                elif d.type == "named_imports":
                    for s in d.named_children:
                        if s.type == "import_specifier":
                            name, al = _text(s.child_by_field_name("name")), _text(s.child_by_field_name("alias"))
                            symbols.append(f"{name} as {al}" if al and al != name else name)
        if alias:
            self.res.imports.append(ImportRef(self.file_id, target, alias=alias))
        if symbols or not alias:
            self.res.imports.append(ImportRef(self.file_id, target, symbols=symbols))

    # -- declarations ----------------------------------------------------------
    def _declarator(self, d, stmt, cid, class_id, scope, qual, top, exported) -> None:
        name_node, value = d.child_by_field_name("name"), d.child_by_field_name("value")
        ann = d.child_by_field_name("type")
        inner = _unwrap(value)
        dyn = _dynamic_import(value)
        if dyn and name_node is not None and name_node.type in ("identifier", "object_pattern"):
            target = self._spec(dyn)   # const files = await import("./files.js")  /  const { a, b } = await import(...)
            if name_node.type == "identifier":
                self.res.imports.append(ImportRef(self.file_id, target, alias=_text(name_node)))
            else:
                self.res.imports.append(ImportRef(self.file_id, target, symbols=[
                    a if a == b else f"{a} as {b}" for a, b in _pattern_names(name_node)]))
            return
        if name_node is not None and name_node.type == "identifier" and ann is not None:
            m = IMPORT_TYPE.search(_text(ann))
            if m:   # let files: typeof import("./files.js")
                self.res.imports.append(ImportRef(self.file_id, self._spec(m.group(1)), alias=_text(name_node)))
                if value is not None:
                    self._walk(value, cid, class_id, scope, qual)
                return
        if name_node is not None and name_node.type == "object_pattern" and inner is not None and inner.type == "call_expression":
            # const { save, load } = useStore()  /  = useStore.getState()
            callee = _unwrap(inner.child_by_field_name("function"))
            owner = _text(callee) if callee is not None and callee.type == "identifier" else \
                _text(callee.child_by_field_name("object")) if callee is not None and callee.type == "member_expression" \
                and _text(callee.child_by_field_name("property")) == "getState" else ""
            if owner in self.imported or owner in self.stores:
                for taken, local in _pattern_names(name_node):
                    scope["@" + local] = (owner, taken)
        if name_node is None or name_node.type != "identifier":
            # const { a, b } = require("x")  /  const [x, setX] = useState()
            if inner is not None and inner.type == "call_expression" and _text(inner.child_by_field_name("function")) == "require":
                target = self._require_target(inner)
                if target and name_node is not None and name_node.type == "object_pattern":
                    syms = [_text(p.child_by_field_name("key") or p) for p in name_node.named_children
                            if p.type in ("shorthand_property_identifier_pattern", "pair_pattern")]
                    self.res.imports.append(ImportRef(self.file_id, target, symbols=syms))
                    return
            if value is not None:
                self._walk(value, cid, class_id, scope, qual)
            return
        name = _text(name_node)
        for mark in ("", "~", "=", "@"):
            scope.pop(mark + name, None)
        fn = self._fn_of(value)
        if fn is not None:
            self._function(name, fn, stmt, self.file_id if top else cid, qual, class_id if fn.type == "arrow_function" else None,
                           scope, exported=exported)
            if not _same(inner, fn):   # the wrapper call's other arguments
                args = inner.child_by_field_name("arguments")
                for a in (args.named_children if args is not None else []):
                    if not _same(_unwrap(a), fn):
                        self._walk(a, cid, class_id, scope, qual)
            return
        if inner is not None and inner.type == "call_expression" and _text(inner.child_by_field_name("function")) == "require":
            target = self._require_target(inner)
            if target:
                self.res.imports.append(ImportRef(self.file_id, target, alias=name))
                return
        if top and inner is not None and inner.type == "object" and self._object_type(name, inner, stmt, qual, exported):
            return
        if top and inner is not None and inner.type == "call_expression":
            # const useStore = create<State>((set, get) => ({ save() {...}, load: () => {...} })): a type with those methods
            made = self._made_object(inner)
            if made is not None and self._object_type(name, made, stmt, qual, exported, native="store"):
                self.stores.add(name)
                self._call(inner, cid, class_id, scope)
                return
        if inner is not None and inner.type == "call_expression":
            # const save = useStore((s) => s.save): calling save() calls the store's save
            callee = _unwrap(inner.child_by_field_name("function"))
            args = inner.child_by_field_name("arguments")
            first = _unwrap(args.named_children[0]) if args is not None and args.named_children else None
            if callee is not None and callee.type == "identifier" and first is not None and first.type == "arrow_function":
                body = _unwrap(first.child_by_field_name("body"))
                param = first.child_by_field_name("parameter") or (first.child_by_field_name("parameters").named_children[0]
                                                                    if first.child_by_field_name("parameters") is not None
                                                                    and first.child_by_field_name("parameters").named_children else None)
                pname = _text(param.child_by_field_name("pattern") or param) if param is not None else ""
                if body is not None and body.type == "member_expression" and _text(body.child_by_field_name("object")) == pname \
                        and pname and (_text(callee) in self.imported or _text(callee) in self.stores):
                    scope["@" + name] = (_text(callee), _text(body.child_by_field_name("property")))
        if inner is not None and inner.type in ("class", "class_expression"):
            self._class(name, inner, self.file_id if top else cid, qual, exported)
            return
        names = _type_names(ann) if ann is not None else []
        if names:
            scope[name] = names[0]
            self.res.type_refs.append(TypeRef(cid, names, "local", d.start_point[0] + 1))
        elif inner is not None and inner.type == "new_expression":
            ctor = _text(inner.child_by_field_name("constructor")).rsplit(".", 1)[-1]
            if ctor[:1].isupper():
                scope[name] = ctor
        if value is not None:
            if not names and inner is not None and inner.type == "call_expression":
                site = self._call(inner, cid, class_id, scope)
                if site is not None:
                    scope["~" + name] = site
            self._walk(value, cid, class_id, scope, qual)
        if name not in scope and ("~" + name) not in scope and ("@" + name) not in scope:
            scope["=" + name] = "local"   # a value of unknown type: calling it is not calling a function of that name

    def _made_object(self, call):
        """The object literal a callback argument hands back: create((set) => ({ ... })) or create()((set) => ({ ... }))."""
        args = call.child_by_field_name("arguments")
        for a in (args.named_children if args is not None and args.type == "arguments" else []):
            fn = _unwrap(a)
            if fn is not None and fn.type in FUNCS:
                body = _unwrap(fn.child_by_field_name("body"))
                if body is not None and body.type == "object":
                    pairs = [m for m in body.named_children if m.type == "method_definition"
                             or (m.type == "pair" and self._fn_of(m.child_by_field_name("value")) is not None)]
                    if len(pairs) >= 2:
                        return body
        return None

    def _require_target(self, call) -> Optional[str]:
        args = call.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        s = _string(first)
        return self._spec(s) if s else None

    def _params(self, fn, cid, scope, declare=True):
        """Read a function's parameters into scope. Returns (names, required, total, variadic)."""
        params = fn.child_by_field_name("parameters")
        single = fn.child_by_field_name("parameter")
        names, required, total, variadic = [], 0, 0, False
        items = [single] if single is not None else (params.named_children if params is not None else [])
        for p in items:
            if p.type == "comment":
                continue
            pat, ann, default, optional = p, None, None, False
            if p.type in ("required_parameter", "optional_parameter"):
                pat = p.child_by_field_name("pattern") or (p.named_children[0] if p.named_children else None)
                ann, default = p.child_by_field_name("type"), p.child_by_field_name("value")
                optional = p.type == "optional_parameter"
            elif p.type == "assignment_pattern":
                pat, default = p.child_by_field_name("left"), p.child_by_field_name("right")
            if pat is None or pat.type == "this":
                continue
            if pat.type == "rest_pattern":
                variadic = True
                continue
            total += 1
            if default is None and not optional:
                required += 1
            if pat.type == "identifier":
                pname = _text(pat)
                names.append(pname)
                scope.pop(pname, None)
                scope.pop("~" + pname, None)
                tn = _type_names(ann) if ann is not None else []
                if tn:
                    scope[pname] = tn[0]
                    if declare:
                        self.res.type_refs.append(TypeRef(cid, tn, "param", p.start_point[0] + 1))
                else:
                    scope["=" + pname] = "local"   # shadows a function of the same name
            elif ann is not None and declare:
                tn = _type_names(ann)
                if tn:
                    self.res.type_refs.append(TypeRef(cid, tn, "param", p.start_point[0] + 1))
        return names, required, total, variadic

    def _function(self, name, fn, outer, parent_id, qual, class_id, outer_scope, exported=False,
                  is_method=False, is_static=False, visibility=None, native=None, abstract=False) -> str:
        cid = f"{self.repo}:typescript:{qual}.{name}"
        scope: dict = dict(outer_scope or {}) if not is_method else {}
        pnames, required, total, variadic = self._params(fn, cid, scope)
        body = fn.child_by_field_name("body")
        head_end = body.start_byte if body is not None else fn.end_byte
        ret = fn.child_by_field_name("return_type")
        rnames = _type_names(ret) if ret is not None else []
        if rnames:
            self.res.type_refs.append(TypeRef(cid, rnames, "return", fn.start_point[0] + 1))
        sig = _squash(self.src[outer.start_byte:head_end].decode("utf8", "replace"))[:300].rstrip("{ ").rstrip("=>").rstrip()
        component = self.is_jsx and name[:1].isupper() and not is_method
        node = Node(
            id=cid, kind="callable", name=name, parent_id=parent_id, language=LANGUAGE, path=self.path,
            span_start=outer.start_point[0] + 1, span_end=outer.end_point[0] + 1,
            attrs={"signature": sig,
                   "visibility": visibility or ("public" if exported or is_method else "private"),
                   "is_static": is_static or not is_method, "is_async": any(c.type == "async" for c in fn.children),
                   "is_virtual": is_method and not is_static, "is_abstract": abstract or None,
                   "native_kind": native or ("method" if is_method else "component" if component else "function"),
                   "argc_min": required, "argc_max": 99 if variadic else total, "type_id": class_id if is_method else None,
                   "params": pnames, "returns": rnames[0] if rnames else None,
                   "body_line": body.start_point[0] + 1 if body is not None else None})
        fresh = self._add(node)
        if body is not None:
            self._walk(body, cid, class_id, scope, f"{qual}.{name}")
            if fresh and not rnames:
                made = self._returned_new(body)
                if made:
                    node.attrs["returns"] = made
        return cid

    def _returned_new(self, body) -> Optional[str]:
        """With no annotation, `return new Foo()` still says what comes back."""
        expr = _unwrap(body) if body.type != "statement_block" else None
        if expr is not None and expr.type == "new_expression":
            return _text(expr.child_by_field_name("constructor")).rsplit(".", 1)[-1]
        stack = list(body.children) if body.type == "statement_block" else []
        while stack:
            n = stack.pop()
            if n.type in FUNCS or n.type in ("function_declaration", "class_declaration", "method_definition"):
                continue
            if n.type == "return_statement":
                expr = _unwrap(n.named_children[0]) if n.named_children else None
                if expr is not None and expr.type == "new_expression":
                    return _text(expr.child_by_field_name("constructor")).rsplit(".", 1)[-1]
            stack.extend(n.children)
        return None

    def _member_name(self, node) -> str:
        n = node.child_by_field_name("name")
        if n is None:
            return ""
        if n.type in ("string", "template_string"):
            return _string(n) or ""
        return _text(n) if n.type != "computed_property_name" else ""

    def _class(self, name, node, parent_id, qual, exported) -> None:
        qname = f"{qual}.{name}"
        tid = f"{self.repo}:typescript:{qname}"
        body = node.child_by_field_name("body")
        head_end = body.start_byte if body is not None else node.end_byte
        self._add(Node(
            id=tid, kind="type", name=name, parent_id=parent_id, language=LANGUAGE, path=self.path,
            span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
            attrs={"native_kind": "class", "namespace": qual, "visibility": "public" if exported else "private",
                   "is_abstract": node.type == "abstract_class_declaration",
                   "signature": _squash(self.src[node.start_byte:head_end].decode("utf8", "replace"))[:300]}))
        for h in node.children:
            if h.type == "class_heritage":
                for clause in h.children:
                    if clause.type == "extends_clause":
                        value = clause.child_by_field_name("value")
                        base = _text(value).split("<")[0].rsplit(".", 1)[-1]
                        if base:
                            self.res.type_refs.append(TypeRef(tid, [base], "base", clause.start_point[0] + 1))
                    elif clause.type == "implements_clause":
                        for tn in _type_names(clause):
                            self.res.type_refs.append(TypeRef(tid, [tn], "base", clause.start_point[0] + 1))
        self.self_types.setdefault(tid, {})
        if body is None:
            return
        # Overload signatures: the method is its implementation, wherever the signatures sit.
        implemented = {self._member_name(m) for m in body.children if m.type == "method_definition"}
        for m in body.children:
            if m.type == "method_signature" and self._member_name(m) in implemented:
                continue
            mods = {c.type for c in m.children} | {_text(c) for c in m.children if c.type == "accessibility_modifier"}
            vis = "private" if "private" in mods else "protected" if "protected" in mods else "public"
            static = "static" in mods
            if m.type in ("method_definition", "abstract_method_signature", "method_signature"):
                mname = self._member_name(m)
                if not mname:
                    self._walk_children(m, None, tid, {}, qname)
                    continue
                if mname.startswith("#"):
                    vis = "private"
                kind = "getter" if "get" in mods else "setter" if "set" in mods else None
                if mname == CTOR:
                    params = m.child_by_field_name("parameters")
                    for p in (params.named_children if params is not None else []):
                        if any(c.type in ("accessibility_modifier", "readonly") for c in p.children):
                            pat, ann = p.child_by_field_name("pattern"), p.child_by_field_name("type")
                            if pat is not None and pat.type == "identifier":
                                self._field(tid, _text(pat), p, ann, None)
                if kind == "setter" and f"{tid}.{mname}" in self.ids:
                    self._walk_children(m, f"{tid}.{mname}", tid, {}, qname)
                    continue
                self._function(mname, m, m, tid, qname, tid, None, is_method=True, is_static=static, visibility=vis,
                               native=kind, abstract=m.type != "method_definition")
            elif m.type in ("public_field_definition", "field_definition", "property_signature"):
                fname = self._member_name(m)
                if not fname:
                    continue
                value = m.child_by_field_name("value")
                fn = self._fn_of(value)
                if fn is not None:
                    self._function(fname, fn, m, tid, qname, tid, None, is_method=True, is_static=static, visibility=vis)
                else:
                    self._field(tid, fname, m, m.child_by_field_name("type"), value, vis)
                    if value is not None:
                        self._walk(value, f"{tid}.{CTOR}" if f"{tid}.{CTOR}" in self.ids else None, tid, {}, qname)

    def _field(self, tid, name, node, ann, value, vis="public", native="property") -> None:
        fid = f"{tid}.{name}"
        names = _type_names(ann) if ann is not None else []
        declared = _squash(_text(ann)).lstrip(": ")[:120] if ann is not None else None
        inner = _unwrap(value)
        tname = names[0] if names else None
        if tname is None and inner is not None and inner.type == "new_expression":
            tname = _text(inner.child_by_field_name("constructor")).rsplit(".", 1)[-1]
        if tname:
            self.self_types.setdefault(tid, {})[name] = tname
        if self._add(Node(
                id=fid, kind="field", name=name, parent_id=tid, language=LANGUAGE, path=self.path,
                span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
                attrs={"native_kind": native, "declared_type": declared, "type_name": tname,
                       "visibility": "private" if name.startswith(("#", "_")) else vis, "is_mutable": True})):
            self.res.edges.append(Edge("has_field", tid, fid))
            if names:
                self.res.type_refs.append(TypeRef(tid, names, "field_type", node.start_point[0] + 1))

    def _typeish(self, node, parent_id, qual, exported) -> None:
        name = _text(node.child_by_field_name("name"))
        if not name:
            return
        if node.type == "type_alias_declaration":
            m = IMPORT_TYPE.search(_text(node.child_by_field_name("value")))
            if m:   # type FilesModule = typeof import("../src/files.js")
                self.module_aliases.add(name)
                self.res.imports.append(ImportRef(self.file_id, self._spec(m.group(1)), alias=name))
                return
        qname = f"{qual}.{name}"
        tid = f"{self.repo}:typescript:{qname}"
        native = {"interface_declaration": "interface", "type_alias_declaration": "alias", "enum_declaration": "enum"}[node.type]
        if not self._add(Node(
                id=tid, kind="type", name=name, parent_id=parent_id, language=LANGUAGE, path=self.path,
                span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
                attrs={"native_kind": native, "namespace": qual, "visibility": "public" if exported else "private",
                       "is_abstract": native == "interface",
                       "signature": _squash(_text(node))[:200]})):
            return   # a declaration merged with one of the same name
        if native == "enum":
            body = node.child_by_field_name("body")
            for m in (body.named_children if body is not None else []):
                mname = _text(m.child_by_field_name("name") or m) if m.type in ("enum_assignment", "property_identifier") else ""
                if mname:
                    self._field(tid, mname, m, None, None, native="enum_member")
            return
        if native == "interface":
            for c in node.children:
                if c.type == "extends_type_clause":
                    for tn in _type_names(c):
                        self.res.type_refs.append(TypeRef(tid, [tn], "base", c.start_point[0] + 1))
            bodies = [node.child_by_field_name("body")]
        else:
            # type X = A & { b: string }: A is a base, b is a field.
            value, bodies, parts = node.child_by_field_name("value"), [], []
            stack = [value] if value is not None else []
            while stack:
                n = stack.pop()
                if n.type in ("intersection_type", "parenthesized_type"):
                    stack.extend(n.named_children)
                elif n.type == "object_type":
                    bodies.append(n)
                else:
                    parts.append(n)
            for p in parts:
                if p.type in ("type_identifier", "generic_type", "nested_type_identifier") and len(parts) + len(bodies) > 1:
                    tn = _type_names(p)
                    if tn:
                        self.res.type_refs.append(TypeRef(tid, [tn[0]], "base", p.start_point[0] + 1))
                else:
                    tn = _type_names(p)
                    if tn:
                        self.res.type_refs.append(TypeRef(tid, tn, "field_type", p.start_point[0] + 1))
        for body in bodies:
            for m in (body.named_children if body is not None else []):
                if m.type == "property_signature":
                    fname = self._member_name(m)
                    if fname:
                        self._field(tid, fname, m, m.child_by_field_name("type"), None)
                elif m.type == "method_signature":
                    mname = self._member_name(m)
                    if mname:
                        self._function(mname, m, m, tid, qname, tid, None, is_method=True, abstract=True)

    def _object_type(self, name, obj, stmt, qual, exported, native="object") -> bool:
        """`const api = { load() {}, save: () => {} }` at the top of a file: a type with those methods."""
        members = []
        for m in obj.named_children:
            if m.type == "method_definition":
                members.append((self._member_name(m), m, m))
            elif m.type == "pair":
                fn = self._fn_of(m.child_by_field_name("value"))
                key = m.child_by_field_name("key")
                kname = (_string(key) if key is not None and key.type == "string" else _text(key)) if key is not None else ""
                if fn is not None and re.fullmatch(r"[A-Za-z_$][\w$]*", kname or ""):
                    members.append((kname, fn, m))
        if not members:
            return False
        qname = f"{qual}.{name}"
        tid = f"{self.repo}:typescript:{qname}"
        self._add(Node(
            id=tid, kind="type", name=name, parent_id=self.file_id, language=LANGUAGE, path=self.path,
            span_start=stmt.start_point[0] + 1, span_end=stmt.end_point[0] + 1,
            attrs={"native_kind": native, "namespace": qual, "visibility": "public" if exported else "private",
                   "is_abstract": False, "signature": f"const {name} = {{ ... }}"}))
        done = {(m.start_byte, m.end_byte) for _, _, m in members}
        for mname, fn, outer in members:
            if mname:
                self._function(mname, fn, outer, tid, qname, tid, None, is_method=True, is_static=True)
        for m in obj.named_children:
            if (m.start_byte, m.end_byte) not in done:
                self._walk(m, self.top_id, None, {}, qual)
        return True

    def _object_members(self, obj, cid, class_id, scope, qual) -> None:
        """Function-valued properties of an object written inside a function are that function's local functions."""
        for m in obj.children:
            if m.type == "method_definition":
                mname = self._member_name(m)
                if (m.start_byte, m.end_byte) in self.redirect:   # a route's handler: { handler(req) { ... } }
                    self._callback(m, cid, class_id, scope, qual)
                    continue
                if mname:
                    self._function(mname, m, m, cid, qual, class_id, scope)
                    continue
            elif m.type == "pair":
                key = m.child_by_field_name("key")
                fn = self._fn_of(m.child_by_field_name("value"))
                kname = _text(key) if key is not None and key.type == "property_identifier" else ""
                if fn is not None and kname and fn.end_point[0] - fn.start_point[0] >= 2 \
                        and (fn.start_byte, fn.end_byte) not in self.redirect:
                    self._function(kname, fn, m, cid, qual, class_id, scope)
                    continue
            self._walk(m, cid, class_id, scope, qual)

    # -- expressions -----------------------------------------------------------
    def _assign(self, node, cid, class_id, scope) -> None:
        left, right = _unwrap(node.child_by_field_name("left")), _unwrap(node.child_by_field_name("right"))
        if left is None:
            return
        dyn = _dynamic_import(node.child_by_field_name("right"))
        if dyn and left.type in ("identifier", "object_pattern", "object"):
            target = self._spec(dyn)
            if left.type == "identifier":
                self.res.imports.append(ImportRef(self.file_id, target, alias=_text(left)))
            else:
                names = _pattern_names(left) or [(_text(c), _text(c)) for c in left.named_children
                                                 if c.type in ("shorthand_property_identifier", "shorthand_property_identifier_pattern")]
                self.res.imports.append(ImportRef(self.file_id, target, symbols=[a if a == b else f"{a} as {b}" for a, b in names]))
        if left.type == "identifier" and right is not None and right.type == "new_expression":
            ctor = _text(right.child_by_field_name("constructor")).rsplit(".", 1)[-1]
            if ctor[:1].isupper():
                scope[_text(left)] = ctor
        if left.type == "member_expression" and class_id is not None and _text(left.child_by_field_name("object")) == "this":
            prop = left.child_by_field_name("property")
            if prop is not None and prop.type in ("property_identifier", "private_property_identifier") \
                    and self.by_id.get(class_id) is not None and self.by_id[class_id].attrs.get("native_kind") == "class" \
                    and f"{class_id}.{_text(prop)}" not in self.ids:
                self._field(class_id, _text(prop), node, None, right)   # a JavaScript class declares fields by assigning them

    def _receiver(self, obj, cid, class_id, scope):
        """(receiver, receiver type, chain) for the object a member is taken from."""
        obj = _unwrap(obj)
        if obj is None:
            return "?", None, None
        t = obj.type
        if t == "this":
            return "this", None, None
        if t == "super":
            return "base", None, None
        if t == "identifier":
            text = _text(obj)
            known = scope.get(text)
            if known in self.module_aliases:
                return known, None, None    # a variable typed as a module: calls on it are calls into that module
            return text, known, None if known else scope.get("~" + text)
        if t == "call_expression":
            inner = _unwrap(obj.child_by_field_name("function"))
            if inner is not None and inner.type == "member_expression" and _text(inner.child_by_field_name("property")) == "getState" \
                    and inner.child_by_field_name("object").type == "identifier":
                return _text(inner.child_by_field_name("object")), None, None   # useStore.getState().save()
        if t == "member_expression" and _text(obj.child_by_field_name("object")) == "this":
            inner = _text(obj.child_by_field_name("property"))
            return "." + inner, self.self_types.get(class_id or "", {}).get(inner), None
        if t == "call_expression":
            return "?", None, self._call(obj, cid, class_id, scope)
        if t == "new_expression":
            return "?", _text(obj.child_by_field_name("constructor")).rsplit(".", 1)[-1], None
        return "?", None, None

    def _call(self, node, cid, class_id, scope) -> Optional[CallSite]:
        """Record one call. A call used as the receiver of another is recorded once and shared."""
        key = (node.start_byte, node.end_byte)
        if key in self.done_calls:
            return self.done_calls[key]
        self.done_calls[key] = None
        fn = _unwrap(node.child_by_field_name("function"))
        args = node.child_by_field_name("arguments")
        if fn is None or cid is None:
            return None
        arg_nodes = [a for a in args.named_children if a.type != "comment"] if args is not None and args.type == "arguments" else []
        argc = len(arg_nodes)
        line, col = node.start_point[0] + 1, node.start_point[1]
        hints = tuple(_arity(a) for a in arg_nodes)
        site = None
        if fn.type == "identifier":
            name = _text(fn)
            if "@" + name in scope:
                owner, member = scope["@" + name]
                site = CallSite(cid, member, owner, None, argc, line, class_id, col, hints)
            elif name in scope or "=" + name in scope or "~" + name in scope:
                self.done_calls[key] = None
                return None   # a local value being called: a parameter, a callback, the result of another call
            else:
                site = CallSite(cid, name, None, None, argc, line, class_id, col, hints)
        elif fn.type == "super":
            site = CallSite(cid, CTOR, "base", None, argc, line, class_id, col, hints)
        elif fn.type == "member_expression":
            name = _text(fn.child_by_field_name("property"))
            receiver, rtype, chain = self._receiver(fn.child_by_field_name("object"), cid, class_id, scope)
            if name:
                site = CallSite(cid, name, receiver, rtype, argc, line, class_id, col, hints, chain=chain)
        if site is not None:
            self.res.calls.append(site)
            if cid == self.top_id:
                self.top_used = True
            self._endpoint(node, fn, site, arg_nodes, cid, line)
        self.done_calls[key] = site
        return site

    def _call_node(self, node, cid, class_id, scope, qual, top) -> None:
        fn = _unwrap(node.child_by_field_name("function"))
        args = node.child_by_field_name("arguments")
        # Not the callee's whole text: in a builder chain a.b().c().d()... that is the chain so far, read again at
        # every link, which made a long chain quadratic. Only the last name is needed, and the whole text only for
        # a call that could be a test (a title and a function among its arguments).
        if fn is None:
            last = ""
        elif fn.type == "member_expression":
            last = _text(fn.child_by_field_name("property"))
        elif fn.type in ("identifier", "import"):
            last = _text(fn)
        else:
            last = _text(fn).rsplit(".", 1)[-1]
        arg_nodes = [a for a in args.named_children if a.type != "comment"] if args is not None and args.type == "arguments" else []
        if fn is not None and fn.type == "import":
            s = _string(arg_nodes[0]) if arg_nodes else None
            if s:
                self.res.imports.append(ImportRef(self.file_id, self._spec(s)))
            return
        if fn is not None and fn.type == "identifier" and last == "require" and arg_nodes and _string(arg_nodes[0]):
            self.res.imports.append(ImportRef(self.file_id, self._spec(_string(arg_nodes[0]))))
            return
        title = _string(arg_nodes[0]) if arg_nodes else None
        body = next((_unwrap(a) for a in arg_nodes[1:] if _unwrap(a) is not None and _unwrap(a).type in FUNCS), None) \
            if title is not None else None
        head, mods = "", set()
        if body is not None:
            # describe("x", () => { it("does y", () => { ... }) })
            ftext = _text(fn)
            head = ftext.split(".")[0].split("(")[0]
            runner = fn
            while runner is not None and runner.type in ("call_expression", "member_expression"):   # it.each(rows)("...", fn)
                runner = runner.child_by_field_name("function") if runner.type == "call_expression" else runner.child_by_field_name("object")
            head = _text(runner) if runner is not None and runner.type == "identifier" else head
            mods = set(re.findall(r"\.(\w+)", ftext.split("(")[0]))
        if head in SUITE_FNS and mods <= TEST_MODS and title is not None and body is not None:
            self.suites.append(title)
            self._callback(body, cid, class_id, scope, qual)
            self.suites.pop()
            return
        if head in TEST_FNS and mods <= TEST_MODS and title is not None and body is not None and (self.is_test_file or cid == self.top_id):
            full = " > ".join(self.suites + [title])
            slug = re.sub(r"[^A-Za-z0-9]+", "-", full).strip("-").lower()[:120]
            tid = f"{self.top_id}/test:{slug}"
            n = 2
            while tid in self.ids:
                tid = f"{self.top_id}/test:{slug}-{n}"
                n += 1
            self.top_used = True
            self._add(Node(
                id=tid, kind="test", name=title, parent_id=self.top_id, language=LANGUAGE, path=self.path,
                span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
                attrs={"framework": "vitest", "runner": head, "suite": " > ".join(self.suites) or None, "full_name": full}))
            self.redirect[(body.start_byte, body.end_byte)] = (tid, f"{self.mod}.<module>/test:{tid.rsplit('/test:', 1)[1]}")
            self._callback(body, cid, class_id, scope, qual)
            return
        site = self._call(node, cid, class_id, scope)
        if top and cid == self.top_id and site is not None and not self.is_test_file:
            if (site.receiver is None and site.name in MAIN_NAMES and site.name in self.fn_names) or site.name in ("listen",) \
                    or (site.name == "render" and "createRoot" in _text(node)):
                self.entry = True
        if site is not None and fn is not None and last in LAUNCHERS \
                and (site.receiver is None or re.search(r"child_process|^cp$|^childProcess$", site.receiver or "")):
            found = list(_strings(args)) if args is not None else []
            for a in arg_nodes:
                if a.type == "identifier" and _text(a) in self.consts:
                    found.extend(self.consts[_text(a)])
            if self.all_strings is None:
                self.all_strings = _strings(self.tree.root_node)
            # A program named by a literal ("git") is that program; only a computed one is looked for among the file's strings.
            named = bool(arg_nodes) and _unwrap(arg_nodes[0]) is not None and _unwrap(arg_nodes[0]).type == "string"
            # A program a function of this file computes (spawn(node, [hostEntry(), ...])) is the path that function
            # builds, or nothing known: the file's other strings name other things.
            helped = self._helper_paths(node, arg_nodes)
            if helped is not None:
                found, named = helped + found, True
            self.res.spawns.append(Spawn(cid, found, [] if named else self.all_strings,
                                         "pipe" in _text(args) or "stdio" in _text(args), node.start_point[0] + 1))
        for a in arg_nodes:
            self._reference(a, cid, class_id, scope)
        # The callee's own sub-expressions (a.b().c(): the inner call; x.y.z(): reads of x.y), then the arguments.
        if fn is not None and fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            if obj is not None:
                self._walk(obj, cid, class_id, scope, qual)
        elif fn is not None and fn.type not in ("identifier", "super"):
            self._walk(fn, cid, class_id, scope, qual)
        if args is not None:
            self._walk_children(args, cid, class_id, scope, qual)

    def _reference(self, node, cid, class_id, scope) -> None:
        """A function handed over by name: `onClick={save}`, `items.map(render)`, `{ onSave: save }`."""
        if cid is None:
            return
        t = node.type
        target = node
        if t == "jsx_expression":
            target = node.named_children[0] if node.named_children else None
        elif t == "pair":
            target = node.child_by_field_name("value")
        target = _unwrap(target)
        if target is None:
            return
        line, col = target.start_point[0] + 1, target.start_point[1]
        if target.type in ("identifier", "shorthand_property_identifier"):
            name = _text(target)
            if (name in self.fn_names or name in self.imported) and name not in scope and ("=" + name) not in scope \
                    and ("~" + name) not in scope:
                self.res.calls.append(CallSite(cid, name, None, None, -1, line, class_id, col, ref=True))
                if cid == self.top_id:
                    self.top_used = True
        elif target.type == "member_expression" and _text(target.child_by_field_name("object")) == "this" and class_id is not None:
            name = _text(target.child_by_field_name("property"))
            if name:
                self.res.calls.append(CallSite(cid, name, "this", None, -1, line, class_id, col, ref=True))

    def _new(self, node, cid, class_id, scope) -> None:
        if cid is None:
            return
        key = (node.start_byte, node.end_byte)
        if key in self.done_calls:
            return
        self.done_calls[key] = None
        ctor = _unwrap(node.child_by_field_name("constructor"))
        args = node.child_by_field_name("arguments")
        argc = len([a for a in args.named_children if a.type != "comment"]) if args is not None else 0
        line, col = node.start_point[0] + 1, node.start_point[1]
        if ctor is None:
            return
        if ctor.type == "identifier":
            self.res.calls.append(CallSite(cid, _text(ctor), None, None, argc, line, class_id, col))
        elif ctor.type == "member_expression" and ctor.child_by_field_name("object").type == "identifier":
            self.res.calls.append(CallSite(cid, _text(ctor.child_by_field_name("property")),
                                           _text(ctor.child_by_field_name("object")), None, argc, line, class_id, col))
        if cid == self.top_id:
            self.top_used = True

    def _jsx(self, node, cid, class_id) -> None:
        name = node.child_by_field_name("name")
        if name is None or cid is None:
            return
        text = _text(name)
        line, col = node.start_point[0] + 1, node.start_point[1]
        if name.type == "identifier" and text[:1].isupper():
            self.res.calls.append(CallSite(cid, text, None, None, 1, line, class_id, col))
        elif name.type in ("member_expression", "nested_identifier") and "." in text:
            head, _, last = text.rpartition(".")
            if "." not in head and last[:1].isupper():
                self.res.calls.append(CallSite(cid, last, head, None, 1, line, class_id, col))
        if cid == self.top_id:
            self.top_used = True

    def _use(self, node, cid, class_id, scope) -> None:
        """Record obj.prop as a read or an assignment of a possible field."""
        parent = node.parent
        if cid is None or parent is None:
            return
        if parent.type == "call_expression" and _same(parent.child_by_field_name("function"), node):
            return
        prop = node.child_by_field_name("property")
        if prop is None or prop.type not in ("property_identifier", "private_property_identifier"):
            return
        receiver, rtype, chain = self._receiver(node.child_by_field_name("object"), cid, class_id, scope)
        if receiver == "?" and chain is None and rtype is None:
            return
        if receiver not in ("this", "base", "?") and not receiver.startswith(".") and rtype is None and chain is None:
            return   # an untyped local: nothing says what it is
        cur, up = node, parent
        while up is not None and up.type in ("parenthesized_expression", "non_null_expression", "as_expression"):
            cur, up = up, up.parent
        access = "r"
        if up is not None:
            if up.type == "assignment_expression" and _same(up.child_by_field_name("left"), cur):
                access = "w"
            elif up.type == "augmented_assignment_expression" and _same(up.child_by_field_name("left"), cur):
                access = "rw"
            elif up.type == "update_expression":
                access = "rw"
            elif up.type == "subscript_expression" and _same(up.child_by_field_name("object"), cur):
                outer = up.parent
                if outer is not None and outer.type in ("assignment_expression", "augmented_assignment_expression") \
                        and _same(outer.child_by_field_name("left"), up):
                    access = "rw"
        if access == "r" and up is not None:   # this.items.push(x), this.byId[k].add(x)
            c2, u2 = cur, up
            if u2.type == "subscript_expression" and _same(u2.child_by_field_name("object"), c2):
                c2, u2 = u2, u2.parent
            if u2 is not None and u2.type == "member_expression" and _same(u2.child_by_field_name("object"), c2) \
                    and u2.parent is not None and u2.parent.type == "call_expression" \
                    and _same(u2.parent.child_by_field_name("function"), u2) \
                    and _text(u2.child_by_field_name("property")) in MUTATORS:
                access = "rw"
        self.res.field_uses.append(FieldUse(cid, _text(prop), receiver, rtype, access, node.start_point[0] + 1, class_id, chain))

    def _endpoint(self, node, fn, site: CallSite, arg_nodes, cid, line) -> None:
        """Is this call one end of an HTTP or file channel?"""
        name = site.name
        first = _unwrap(arg_nodes[0]) if arg_nodes else None
        addr = _string(first)
        if site.receiver is None and name == "fetch" and addr and addr.startswith(("/", "http://", "https://", "{}/")):
            m = re.search(r"method\s*:\s*['\"`](\w+)['\"`]", _text(arg_nodes[1])) if len(arg_nodes) > 1 else None
            self.res.endpoints.append(Endpoint("http", "call", cid, addr.replace("{}/", "/", 1) if addr.startswith("{}/") else addr,
                                               line, m.group(1).upper() if m else None))
            return
        if fn.type == "member_expression" and name in HTTP_VERBS | {"all"} and addr and addr.startswith("/"):
            recv = _text(fn.child_by_field_name("object"))
            handler = any(_unwrap(a) is not None and (_unwrap(a).type in FUNCS or _unwrap(a).type == "identifier") for a in arg_nodes[1:])
            method = None if name == "all" else name.upper()
            last = _unwrap(arg_nodes[-1]) if len(arg_nodes) > 1 else None
            if SERVERS.search(recv) and (handler or (last is not None and last.type == "object" and self._handler_member(last))):
                src, written = self._route_handler(last, method, addr, cid, line)
                self.res.endpoints.append(Endpoint("http", "serve", src, addr, line, method, handler=written))
            elif CLIENTS.search(recv):
                self.res.endpoints.append(Endpoint("http", "call", cid, addr, line, method))
            return
        if fn.type == "member_expression" and name == "route" and first is not None and first.type == "object" \
                and SERVERS.search(_text(fn.child_by_field_name("object"))) and self._handler_member(first) is not None:
            # server.route({ method: "GET", url: "/api/x", handler: async (req) => { ... } })
            url, methods = None, []
            for m in first.named_children:
                key = m.child_by_field_name("key") if m.type == "pair" else None
                kname = (_string(key) if key is not None and key.type == "string" else _text(key)) if key is not None else ""
                value = _unwrap(m.child_by_field_name("value")) if m.type == "pair" else None
                if kname in ("url", "path"):
                    url = _string(value)
                elif kname == "method" and value is not None:
                    methods = [s.upper() for s in ([_string(value)] if value.type in ("string", "template_string") else _strings(value))
                               if s]
            if url and url.startswith("/"):
                method = methods[0] if len(methods) == 1 else None
                src, written = self._route_handler(first, method, url, cid, line, label=", ".join(methods) or None)
                self.res.endpoints.append(Endpoint("http", "serve", src, url, line, method, handler=written))
                return
        role = "read" if name in FS_READ else "write" if name in FS_WRITE else None
        if role:
            lits = [s for a in arg_nodes for s in _strings(a)]
            for a in arg_nodes:
                if a.type == "identifier" and _text(a) in self.consts:
                    lits.extend(self.consts[_text(a)])
            self.res.endpoints.append(Endpoint("file", role, cid, "", line, None, lits))
        if name in ("join", "resolve") and fn.type == "member_expression" and _text(fn.child_by_field_name("object")) in ("path", "posix", "nodePath"):
            self.res.path_strings.setdefault(cid, []).extend(s for a in arg_nodes for s in _strings(a))
            return
        if role:
            return
        # A request through a wrapper (apiFetch("/api/x"), api.get(...) on a client the name does not give away)
        # or a test's server.inject({ method, url }): a path that a route of this program serves, linked only when
        # one route fits it (see Indexer._resolve_endpoints).
        url, method = None, None
        if addr and addr.startswith("/") and addr.count("/") >= 2 and not any(ch.isspace() for ch in addr):
            url = addr
        else:
            for a in arg_nodes[:2]:
                a = _unwrap(a)
                if a is not None and a.type == "object":
                    text = _text(a)
                    m = re.search(r"\burl\s*:\s*['\"`](/[^'\"`\s]*)['\"`]", text)
                    if m:
                        url = m.group(1)
                        mm = re.search(r"\bmethod\s*:\s*['\"`](\w+)['\"`]", text)
                        method = mm.group(1).upper() if mm else None
                        break
        if url:
            if method is None and len(arg_nodes) > 1:
                mm = re.search(r"\bmethod\s*:\s*['\"`](\w+)['\"`]", _text(arg_nodes[1]))
                method = mm.group(1).upper() if mm else None
            self.res.endpoints.append(Endpoint("http", "maybe", cid, url.split("?", 1)[0], line, method))

    # -- route handlers ----------------------------------------------------------
    def _handler_member(self, obj):
        """The `handler` of a route's options object: { handler: async (req) => ... }, { handler(req) { ... } },
        { handler: listThings } or { handler }. None when it has none."""
        for m in obj.named_children:
            if m.type == "method_definition" and self._member_name(m) == "handler":
                return m
            if m.type == "shorthand_property_identifier" and _text(m) == "handler":
                return m
            if m.type == "pair":
                key = m.child_by_field_name("key")
                if key is None or (_string(key) if key.type == "string" else _text(key)) != "handler":
                    continue
                value = m.child_by_field_name("value")
                fn = self._fn_of(value)
                value = fn if fn is not None else _unwrap(value)
                if value is not None and value.type in FUNCS + ("identifier", "member_expression"):
                    return value
        return None

    def _route_handler(self, last, method, addr, cid, line, label=None):
        """What answers a route: (the id of the function that serves it, the handler's name as written when it is
        given by name). An inline handler becomes its own function, named for its route and nested in the function
        that registers it, so what it calls is its own and not the registrar's."""
        target = last
        if target is not None and target.type == "object":
            target = self._handler_member(target)
        if target is None or cid is None:
            return cid, None
        if target.type in FUNCS or target.type == "method_definition":
            route = f"{label or method or 'ALL'} {addr}"
            base = f"{cid}/route:{route}"
            hid, n = base, 2
            while hid in self.ids:
                hid = f"{base}-{n}"
                n += 1
            pnames, required, total, variadic = self._params(target, hid, {}, declare=False)
            body = target.child_by_field_name("body")
            head_end = body.start_byte if body is not None else target.end_byte
            head = _squash(self.src[target.start_byte:head_end].decode("utf8", "replace"))[:200].rstrip("{ ").rstrip("=>").rstrip()
            self._add(Node(
                id=hid, kind="callable", name=route, parent_id=cid, language=LANGUAGE, path=self.path,
                span_start=target.start_point[0] + 1, span_end=target.end_point[0] + 1,
                attrs={"signature": f"{route}: {head}", "visibility": "private", "is_static": True,
                       "is_async": any(c.type == "async" for c in target.children), "is_virtual": False,
                       "native_kind": "route_handler", "route": route, "registered_at": line,
                       "argc_min": required, "argc_max": 99 if variadic else total, "type_id": None,
                       "params": pnames, "returns": None,
                       "body_line": body.start_point[0] + 1 if body is not None else None}))
            self.redirect[(target.start_byte, target.end_byte)] = (hid, hid.split(":typescript:", 1)[1])
            return hid, None
        if target.type in ("identifier", "shorthand_property_identifier", "member_expression"):
            return cid, _text(target)
        return cid, None


def parse(repo: str, rel_path: str, file_id: str, src: bytes, module: str = "") -> FileResult:
    w = _Walker(repo, rel_path, file_id, src)
    res = w.run()
    channels.extract(LANGUAGE, w.tree, res, file_id, w.consts)
    return res
