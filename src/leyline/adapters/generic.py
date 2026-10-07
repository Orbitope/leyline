"""One adapter for every language that has a tree-sitter grammar.

Nothing here knows a language. A language is one row of LANGUAGES: its name, its file extensions and
the grammar package to load. What the adapter reads comes from two language-neutral sources:

- the grammar's own *tags query* where it ships one (the queries code hosts use for "go to
  definition"): which nodes define a function, a method or a type, and which are calls;
- otherwise the *shape* of the syntax tree: grammars name their nodes alike (`function_declaration`,
  `class_definition`, `call_expression`), so a node whose type reads like a definition and that has a
  name is taken as one.

That gives declarations, nesting, call sites with whatever the text shows of the receiver, type
mentions, imports, tests by naming convention, and `main` as an entry point. It does not give what
needs a language's rules: the type of a receiver, which overload, field reads and writes. Calls are
therefore linked by name and scope, and the indexer says which links are guesses. A SCIP index, when
there is one, replaces the guesses with the compiler's answer.
"""

from __future__ import annotations

import importlib
import re
from typing import Optional

from ..model import CallSite, Edge, FileResult, ImportRef, Node, TypeRef

VERSION = "0.1"

# language, extensions, grammar package, function in that package that returns the grammar
LANGUAGES = [
    ("go", (".go",), "tree_sitter_go", "language"),
    ("rust", (".rs",), "tree_sitter_rust", "language"),
    ("java", (".java",), "tree_sitter_java", "language"),
    ("kotlin", (".kt", ".kts"), "tree_sitter_kotlin", "language"),
    ("swift", (".swift",), "tree_sitter_swift", "language"),
    ("c", (".c", ".h"), "tree_sitter_c", "language"),
    ("cpp", (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"), "tree_sitter_cpp", "language"),
    ("ruby", (".rb",), "tree_sitter_ruby", "language"),
    ("php", (".php",), "tree_sitter_php", "language_php"),
    ("scala", (".scala", ".sc"), "tree_sitter_scala", "language"),
    ("lua", (".lua",), "tree_sitter_lua", "language"),
    ("elixir", (".ex", ".exs"), "tree_sitter_elixir", "language"),
    ("dart", (".dart",), "tree_sitter_dart", "language"),
    ("zig", (".zig",), "tree_sitter_zig", "language"),
    ("haskell", (".hs",), "tree_sitter_haskell", "language"),
    ("ocaml", (".ml",), "tree_sitter_ocaml", "language_ocaml"),
    ("julia", (".jl",), "tree_sitter_julia", "language"),
    ("bash", (".sh", ".bash"), "tree_sitter_bash", "language"),
    ("gdscript", (".gd",), "tree_sitter_gdscript", "language"),
    # These three also have hand-written adapters, which are used unless the generic one is asked for.
    ("python", (".py",), "tree_sitter_python", "language"),
    ("csharp", (".cs",), "tree_sitter_c_sharp", "language"),
    ("typescript", (".ts", ".mts", ".cts", ".js", ".mjs", ".cjs"), "tree_sitter_typescript", "language_typescript"),
    ("typescript", (".tsx", ".jsx"), "tree_sitter_typescript", "language_tsx"),
]

# -- the shape of a syntax tree ------------------------------------------------------------------
_CALLABLE = re.compile(
    r"^(local_|generator_|abstract_|anonymous_)?(function|method|constructor|destructor|func|fun|fn|def|macro|subroutine|"
    r"procedure|init|deinit|getter|setter|operator|secondary_constructor)"
    r"(_definition|_declaration|_item|_statement|_signature|_declarator)?$")
_NOT_CALLABLE = {"function_declarator", "method_signature", "function_signature", "anonymous_function", "function_type",
                 "abstract_function_declaration" if False else "function_pointer_declarator", "method_elem"}
_TYPE = re.compile(
    r"^(abstract_)?(class|interface|struct|enum|trait|protocol|object|record|union|type_alias|typealias|module|"
    r"mixin|actor|annotation_type|type|data_type|newtype|companion_object)"
    r"(_definition|_declaration|_item|_specifier|_statement|_spec)?$")
_NOT_TYPE = {"type", "module", "class", "object", "interface", "struct", "union", "enum", "record", "type_declaration", "trait"}
_KEEP_TYPE = {("ruby", "class"), ("ruby", "module"), ("elixir", "module")}
# A block that adds members to a type declared elsewhere: `impl T { }`, `extension T { }`.
_CONTAINER = {"impl_item", "extension_declaration", "extension", "implementation", "class_implementation"}
# `const f = () => ...`, `let f = |x| ...`, `local f = function() ... end`, `val f = { x -> ... }`: a name given a function.
_BINDING = re.compile(r"^(variable_declarator|let_declaration|const_item|static_item|short_var_declaration|init_declarator|"
                      r"val_definition|var_definition|property_declaration|variable_declaration|assignment|assignment_expression|"
                      r"assignment_statement|local_variable_declaration|pair|public_field_definition|field_definition|"
                      r"variable_assignment|binding|value_definition|let_binding|local_function|lexical_binding)$")
_FUNC_VALUE = re.compile(r"^(arrow_function|function_expression|function|lambda|lambda_expression|closure_expression|"
                         r"anonymous_function|func_literal|function_definition|fun|fn|block_lambda|annotated_lambda|"
                         r"closure|generator_function|anonymous_method_expression|function_literal)$")
# A call that declares a test: it("does x", () => ...), test("x") { ... }, it "x" do ... end, t.Run("x", func ...).
TEST_CALLS = {"it", "test", "specify", "scenario", "Run", "should", "xit", "fit", "bench", "Describe" if False else "it_behaves_like"}
SUITE_CALLS = {"describe", "context", "suite", "feature", "Describe", "Context", "When"}
_CALL = re.compile(r"^(call|call_expression|method_invocation|invocation_expression|function_call|function_call_expression|"
                   r"member_call_expression|scoped_call_expression|macro_invocation|method_call|command|new_expression|"
                   r"object_creation_expression|instance_expression|navigation_call|constructor_invocation|"
                   r"jsx_self_closing_element|jsx_opening_element)$")
_NEW = {"new_expression", "object_creation_expression", "instance_expression", "composite_literal", "struct_expression"}
_IMPORT = re.compile(r"^(import|use|using|include|require|preproc_include|import_from|namespace_use|extern_crate|open)"
                     r"(_statement|_declaration|_directive|_header|_clause|_spec|_list)?$")
_BASES = re.compile(r"superclass|super_class|super_interfaces|base_list|base_class_clause|class_heritage|extends|implements|"
                    r"delegation_specifier|inheritance|type_list|trait_bounds|supertype|argument_list")
_FIELD = re.compile(r"^(field_declaration|property_declaration|field_definition|public_field_definition|struct_field|"
                    r"field|variable_declaration|val_definition|var_definition|instance_variable_declaration)$")
_NAME_TYPES = ("identifier", "type_identifier", "field_identifier", "simple_identifier", "constant", "name",
               "property_identifier", "namespace_identifier", "scoped_identifier", "qualified_identifier", "word", "variable_name")
_KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "typeof", "catch", "match", "defined", "await", "assert"}
TEST_PATH = re.compile(r"(^|/)(tests?|spec|specs|__tests__|testing)(/|$)|(_test|_spec|Test|Tests|Spec|\.test|\.spec)\.[A-Za-z]+$"
                       r"|(^|/)test_[^/]+$")
TEST_MARK = re.compile(r"#\[(tokio::)?test|@Test\b|@ParameterizedTest|\[Test\]|\[Fact\]|\[Theory\]|@pytest|func Test|\btest\s+\"")
# Method names so common on built-in and library objects that sharing one says nothing about which function is meant.
COMMON_METHODS = frozenset("""new get set add remove push pop put clear size len length next close open read write run start stop
init main string to_string toString equals hash clone copy map filter each find contains insert delete update append
format print println printf error errorf log debug info warn ok err unwrap expect iter into from as_ref as_str
is_empty isEmpty keys values join split trim send recv lock unlock wait name value index count first last sort parse""".split())


def _text(node) -> str:
    return node.text.decode("utf8", "replace") if node is not None else ""


def _name_of(node):
    """The node that names a definition."""
    n = node.child_by_field_name("name")
    if n is not None:
        return n
    decl = node.child_by_field_name("declarator")
    hops = 0
    while decl is not None and hops < 6:      # C: function_definition > function_declarator > identifier
        inner = decl.child_by_field_name("declarator") or decl.child_by_field_name("name")
        if inner is None:
            return decl if decl.type in _NAME_TYPES else next((c for c in decl.named_children if c.type in _NAME_TYPES), None)
        decl, hops = inner, hops + 1
    return next((c for c in node.named_children if c.type in _NAME_TYPES), None)


def _last_name(node):
    """The rightmost name in a callee expression: `a.b.c` -> c."""
    if node is None:
        return None
    if node.type in _NAME_TYPES and not node.named_child_count:
        return node
    for field in ("name", "field", "property", "method", "attribute", "function", "member"):
        c = node.child_by_field_name(field)
        if c is not None and c is not node:
            hit = _last_name(c)
            if hit is not None:
                return hit
    for c in reversed(node.named_children):
        if c.type in _NAME_TYPES or c.named_child_count:
            hit = _last_name(c)
            if hit is not None:
                return hit
    return None


class Generic:
    GENERIC = True

    def __init__(self, language: str, extensions: tuple, package: str, fn: str):
        self.LANGUAGE, self.EXTENSIONS, self.package, self.fn = language, extensions, package, fn
        self.NAME = f"generic-{language}"
        self.VERSION = VERSION
        self.COMMON_METHODS = COMMON_METHODS
        self._parser = self._query = None
        self._shape: dict = {}   # node type -> (definition kind, is a call, is a base list), see _collect
        self._io_shape: dict = {}   # node type -> (is an import, is a field), see parse
        self.mode = None      # "tags" or "shape", once loaded
        self._loaded = False

    def available(self) -> bool:
        try:
            importlib.import_module(self.package)
            return True
        except ImportError:
            return False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        from tree_sitter import Language, Parser, Query
        mod = importlib.import_module(self.package)
        lang = Language(getattr(mod, self.fn)())
        self._parser = Parser(lang)
        self.mode = "shape"
        tags = getattr(mod, "TAGS_QUERY", None)
        if tags is None and hasattr(mod, "_get_query"):
            try:
                tags = mod._get_query("TAGS_QUERY", "tags.scm")
            except Exception:
                tags = None
        if tags:
            try:
                self._query = Query(lang, tags)
                self.mode = "tags"
            except Exception:
                self._query = None

    @staticmethod
    def module_path(rel_path: str) -> str:
        return re.sub(r"\.[A-Za-z0-9]+$", "", rel_path).replace("/", ".")

    # -- reading one file ---------------------------------------------------------------------------
    def _collect(self, root, use_tags: bool):
        """(definitions, calls, type mentions, containers), each a list of tuples over tree nodes."""
        defs, calls, refs, containers = [], [], [], []
        if use_tags and self._query is not None:
            from tree_sitter import QueryCursor
            for _, caps in QueryCursor(self._query).matches(root):
                name = (caps.get("name") or [None])[0]
                if name is None:
                    continue
                for key, nodes in caps.items():
                    node = nodes[0]
                    if key.startswith("definition."):
                        kind = key.split(".", 1)[1]
                        if kind in ("function", "method", "macro", "constructor"):
                            defs.append(("callable", kind, name, node))
                        elif kind in ("class", "interface", "type", "module", "struct", "enum", "trait", "namespace", "object"):
                            if kind == "module" and node.child_by_field_name("body") is None and node.named_child_count <= 1:
                                continue   # `mod geo;` names a file, it is not a type
                            defs.append(("type", kind, name, node))
                    elif key == "reference.call":
                        # some queries tag a call's argument list; the call is the node around it
                        calls.append((name, node.parent if node.type in ("argument_list", "arguments") and node.parent is not None else node))
                    elif key == "reference.implementation" and node.type in _CONTAINER:
                        # `impl Trait for Type`: the block's methods belong to Type; the query names the trait
                        own = _last_name(node.child_by_field_name("type"))
                        containers.append((node, _text(own if own is not None else name)))
                        refs.append((name, node, "base") if node.child_by_field_name("trait") is not None
                                    and name.start_byte == node.child_by_field_name("trait").start_byte else (name, node, "skip"))
                    elif key.startswith("reference."):
                        role = "base" if key == "reference.implementation" or _BASES.search(node.type) or (
                            node.parent is not None and _BASES.search(node.parent.type)) else \
                            "instantiate" if node.type in _NEW or (node.parent is not None and node.parent.type in _NEW) else "local"
                        refs.append((name, node, role))
        lang = self.LANGUAGE
        shape = self._shape
        stack = [root]
        while stack:
            n = stack.pop()
            t = n.type
            # What the rules below make of a node type, worked out once per type rather than by four regular
            # expressions on every node of every file.
            got = shape.get(t)
            if got is None:
                got = shape[t] = (1 if _CALLABLE.match(t) and t not in _NOT_CALLABLE else
                                  2 if (_TYPE.match(t) and t not in _NOT_TYPE) or (lang, t) in _KEEP_TYPE else
                                  3 if _BINDING.match(t) else 0,
                                  _CALL.match(t) is not None, bool(_BASES.search(t)) and t != "argument_list")
            decl, is_call, is_bases = got
            if True:   # the shape rules run alongside the tags query: many queries cover only part of a language
                if decl == 1:
                    nm = _name_of(n)
                    if nm is not None and _text(nm) not in _KEYWORDS:
                        defs.append(("callable", t.split("_")[0], nm, n))
                elif decl == 2:
                    nm = _name_of(n)
                    if nm is not None:
                        defs.append(("type", t.split("_")[0], nm, n))
                elif decl == 3:
                    value = n.child_by_field_name("value") or n.child_by_field_name("right") or \
                        (n.named_children[-1] if n.named_child_count >= 2 else None)
                    while value is not None and value.type in ("parenthesized_expression", "expression_list") and value.named_children:
                        value = value.named_children[0]
                    target = n.child_by_field_name("name") or n.child_by_field_name("left") or n.child_by_field_name("pattern") \
                        or n.child_by_field_name("key") or n.child_by_field_name("declarator") or (n.named_children[0] if n.named_children else None)
                    if value is not None and _FUNC_VALUE.match(value.type) and target is not None and target is not value:
                        nm = target if target.type in _NAME_TYPES and not target.named_child_count else _last_name(target)
                        if nm is not None and re.fullmatch(r"[A-Za-z_$][\w$]*", _text(nm)):
                            defs.append(("callable", "function", nm, n))
                if is_call:
                    callee = n.child_by_field_name("function") or n.child_by_field_name("name") or n.child_by_field_name("method") \
                        or n.child_by_field_name("constructor") or n.child_by_field_name("type") or n.child_by_field_name("macro") \
                        or (n.named_children[0] if n.named_children else None)
                    nm = _last_name(callee)
                    if t.startswith("jsx_") and (nm is None or not _text(nm)[:1].isupper()):
                        nm = None     # <div> is markup; <Panel> renders a component
                    if nm is not None and _text(nm) not in _KEYWORDS:
                        calls.append((nm, n))
                        word = _text(nm)
                        if word in TEST_CALLS or word in SUITE_CALLS:
                            title = _first_string(n)
                            if title is not None and _has_function(n):
                                defs.append(("test" if word in TEST_CALLS else "suite", word, title, n))
                        if t in _NEW:
                            refs.append((nm, n, "instantiate"))
                elif is_bases and n.parent is not None and (
                        _TYPE.match(n.parent.type) or (lang, n.parent.type) in _KEEP_TYPE or n.parent.type in ("class", "type_spec")):
                    inner = [n]
                    while inner:
                        m = inner.pop()
                        if m.type in ("type_identifier", "identifier", "simple_identifier", "constant", "user_type") and not m.named_child_count:
                            refs.append((m, n, "base"))
                        else:
                            inner.extend(m.named_children)
            if t == "token_tree":
                # A macro's arguments, which the grammar leaves as raw tokens: a name directly followed by a
                # parenthesized group (or by `!` and one) is still a call.
                kids = n.children
                for i, c in enumerate(kids[:-1]):
                    nxt = kids[i + 1] if kids[i + 1].type != "!" or i + 2 >= len(kids) else kids[i + 2]
                    if c.type == "identifier" and nxt.type == "token_tree" and _text(nxt)[:1] == "(" and \
                            (i == 0 or _text(kids[i - 1]) not in ("fn", "struct", "enum", "macro_rules", "mod")):
                        calls.append((c, nxt))
            if t in _CONTAINER and not any(c[0].start_byte == n.start_byte for c in containers):
                target = n.child_by_field_name("type") or n.child_by_field_name("name") or _name_of(n)
                nm = _last_name(target)
                if nm is not None:
                    containers.append((n, _text(nm)))
            # Only named nodes are read; unnamed ones (punctuation, keywords) are not put on the stack at all.
            stack.extend(n.named_children)
        return defs, calls, refs, containers

    def parse(self, repo: str, rel_path: str, file_id: str, src: bytes, module: str = "") -> FileResult:
        self._load()
        res = FileResult()
        tree = self._parser.parse(src)
        root = tree.root_node
        mod = self.module_path(rel_path)
        lang = self.LANGUAGE
        prefix = f"{repo}:{lang}:{mod}"
        top_id = f"{prefix}.<module>"
        test_file = bool(TEST_PATH.search(rel_path))
        defs, calls, refs, containers = self._collect(root, True)

        # One definition per span; a method wins over a function when a grammar tags the node as both.
        best: dict = {}
        for kind, native, name, node in defs:
            key = (node.start_byte, node.end_byte)
            if key not in best or native == "method":
                best[key] = (kind, native, name, node)
        ordered = sorted(best.values(), key=lambda d: (d[3].start_byte, -d[3].end_byte))
        containers.sort(key=lambda c: (c[0].start_byte, -c[0].end_byte))
        made: list = []            # (start, end, id, kind, qualified name)
        force_top = [False]
        open_defs: list = []
        ids: dict[str, int] = {}
        type_ids: dict[str, str] = {}

        def enclosing(start, end, kinds=("callable", "type", "test")):
            for s, e, i, k, q in reversed(open_defs):
                if s <= start and end <= e and k in kinds:
                    return s, e, i, k, q
            return None

        suites: list = []
        for kind, native, name_node, node in ordered:
            if kind in ("test", "suite"):
                title = re.sub(r"^[\"'`]+|[\"'`]+$", "", _text(name_node)).strip()
                while suites and suites[-1][1] <= node.start_byte:
                    suites.pop()
                if kind == "suite":
                    suites.append((node.start_byte, node.end_byte, title))
                    continue
                full = " > ".join([x[2] for x in suites] + [title])
                while open_defs and open_defs[-1][1] <= node.start_byte:
                    open_defs.pop()
                outer = enclosing(node.start_byte, node.end_byte, ("callable",))
                parent = outer[2] if outer else top_id
                tid = f"{parent}/test:{re.sub(r'[^A-Za-z0-9]+', '-', full).strip('-').lower()[:120]}"
                ids[tid] = ids.get(tid, 0) + 1
                if ids[tid] > 1:
                    tid = f"{tid}-{ids[tid]}"
                test_parent_top = outer is None
                res.nodes.append(Node(id=tid, kind="test", name=title[:200], parent_id=parent, language=lang, path=rel_path,
                                      span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1,
                                      attrs={"framework": "by convention", "runner": native, "full_name": full}))
                entry = (node.start_byte, node.end_byte, tid, "test", full)
                made.append(entry)
                open_defs.append(entry)
                if test_parent_top:
                    force_top[0] = True
                continue
            name = _text(name_node).strip()
            if not name or len(name) > 120 or "\n" in name:
                continue
            name = re.split(r"[.:]+", name)[-1] if kind == "callable" else name.split("::")[-1].split(".")[-1]
            if native == "macro":
                name += "!"
            while open_defs and open_defs[-1][1] <= node.start_byte:
                open_defs.pop()
            outer = enclosing(node.start_byte, node.end_byte)
            owner_name = None
            cont = next((c for c in reversed(containers) if c[0].start_byte <= node.start_byte and node.end_byte <= c[0].end_byte
                         and not _same(c[0], node)), None)
            recv = node.child_by_field_name("receiver")      # Go: func (t *T) Run()
            if kind == "callable" and recv is not None:
                owner_name = next((_text(x) for x in _walk(recv) if x.type == "type_identifier"), None)
            elif kind == "callable" and cont is not None and (outer is None or outer[0] <= cont[0].start_byte):
                owner_name = cont[1]
            parent_id, qual = (outer[2], outer[4]) if outer else (file_id, "")
            if owner_name and (outer is None or outer[3] != "type"):
                qual = f"{qual}.{owner_name}" if qual and not qual.endswith(owner_name) else owner_name
                if owner_name in type_ids:
                    parent_id = type_ids[owner_name]
            qname = f"{qual}.{name}" if qual else name
            nid = f"{prefix}.{qname}"
            ids[nid] = ids.get(nid, 0) + 1
            if ids[nid] > 1:
                nid = f"{nid}~{ids[nid]}"       # an overload, or the same name declared again
            body = node.child_by_field_name("body")
            head = src[node.start_byte:body.start_byte if body is not None else node.end_byte].decode("utf8", "replace")
            head = re.sub(r"\s+", " ", head.split("\n\n")[0]).strip()[:240]
            before = src[max(0, node.start_byte - 160):node.start_byte].decode("utf8", "replace")
            is_method = kind == "callable" and (native == "method" or bool(owner_name) or (outer is not None and outer[3] == "type"))
            anonymous = kind == "callable" and not owner_name and outer is not None and _in_anonymous(node, outer[0])
            if anonymous:
                is_method = False   # `new Base() { void run() {} }`: a member of a class with no name, not of the outer type
            type_id = parent_id if is_method and parent_id != file_id and (outer is None or outer[3] == "type" or owner_name) else None
            attrs = {"signature": head, "native_kind": native,
                     "visibility": "private" if re.search(r"\b(private|fileprivate)\b", head) or name.startswith("_") else "public"}
            if kind == "callable":
                params = node.child_by_field_name("parameters")
                plist = [p for p in params.named_children if p.type != "comment"] if params is not None else []
                # The receiver written out (Go's func (c *T), Rust's &self, Python's self): a bare call in the
                # body is then never a call on the receiver.
                explicit_self = recv is not None or bool(plist and ("self" in plist[0].type or
                                                                    re.fullmatch(r"&?(mut )?(self|cls)", _text(plist[0]).strip())))
                is_test = (test_file and re.match(r"(?i)test", name) is not None) or bool(TEST_MARK.search(before[-90:] + head[:40]))
                attrs.update({"argc_min": 0, "argc_max": 99, "is_static": not is_method, "is_virtual": is_method,
                              "type_id": type_id, "owner_name": owner_name, "is_test": is_test or None,
                              "framework": "by convention" if is_test else None,
                              "params_seen": len(plist) if params is not None else None, "explicit_self": explicit_self or None,
                              "param_types": _param_types(plist[1:] if explicit_self and recv is None else plist) or None,
                              # declared in `impl Trait for T` or in an anonymous class: it implements something declared
                              # elsewhere, and a method of the type's own of the same name is the one called
                              "via_base": True if anonymous or (cont is not None and owner_name and
                                                                cont[0].child_by_field_name("trait") is not None) else None,
                              "body_line": body.start_point[0] + 1 if body is not None else None})
                ret = node.child_by_field_name("return_type") or node.child_by_field_name("result") or \
                    node.child_by_field_name("returns") or node.child_by_field_name("type")
                if ret is not None:
                    names = _type_names(_text(ret))
                    own = owner_name or (outer[4].rsplit(".", 1)[-1] if outer is not None and outer[3] == "type" else None)
                    attrs["returns_names"] = [own if x == "Self" and own else x for x in names][:4] or None
                tparams = node.child_by_field_name("type_parameters")
                if tparams is not None:
                    attrs["type_params"] = re.findall(r"[A-Za-z_]\w*", re.sub(r"(:|extends|super)[^,>]*", "", _text(tparams)))
            else:
                attrs.update({"namespace": qual, "is_abstract": native in ("interface", "trait", "protocol")})
                type_ids.setdefault(name, nid)
            res.nodes.append(Node(id=nid, kind=kind, name=name, parent_id=parent_id, language=lang, path=rel_path,
                                  span_start=node.start_point[0] + 1, span_end=node.end_point[0] + 1, attrs=attrs))
            entry = (node.start_byte, node.end_byte, nid, kind, qname)
            made.append(entry)
            open_defs.append(entry)
            if kind == "callable" and name == "main" and (parent_id == file_id or "static" in head) and not test_file:
                res.nodes.append(Node(id=nid + "#entry", kind="entry_point", name=f"{rel_path} (main)", parent_id=file_id,
                                      language=lang, path=rel_path, span_start=node.start_point[0] + 1,
                                      span_end=node.end_point[0] + 1, attrs={"trigger": "cli", "address": rel_path}))
                res.edges.append(Edge("exposes", nid + "#entry", nid))

        made.sort()
        starts = [m[0] for m in made]

        def inside(pos, kinds):
            """The innermost definition of one of these kinds around a byte position."""
            import bisect
            i = bisect.bisect_right(starts, pos) - 1
            hit = None
            while i >= 0:
                s, e, nid, k, _ = made[i]
                if s <= pos < e and k in kinds:
                    return made[i]
                if hit is None and pos - s > 400000:
                    break
                i -= 1
            return None

        top_used = False
        seen_calls = set()
        sites, site_at = [], {}
        for name_node, node in calls:
            name = _text(name_node).strip()
            if not name or not re.fullmatch(r"[A-Za-z_$@][\w$?]*", name) or name in _KEYWORDS:
                continue
            key = (name_node.start_byte, name)
            if key in seen_calls:
                continue
            seen_calls.add(key)
            home = inside(name_node.start_byte, ("callable", "test"))
            if home is not None and home[0] == node.start_byte and home[1] == node.end_byte:
                if home[3] == "test":   # the call that declares a test is made by what surrounds it
                    outer = [m for m in made if m[0] <= node.start_byte and node.end_byte <= m[1] and m is not home and m[3] in ("callable", "test")]
                    home = max(outer, key=lambda m: m[0]) if outer else None
                else:
                    continue
            src_id = home[2] if home else top_id
            top_used = top_used or home is None
            owner = inside(name_node.start_byte, ("type",))
            enclosing_type = owner[2] if owner else None
            if home is not None:
                n = next((x for x in res.nodes if x.id == home[2]), None) if False else None
            receiver = _receiver(src, name_node.start_byte)
            if src[name_node.end_byte:name_node.end_byte + 1] == b"!" and lang in ("rust",):
                name += "!"     # a macro: only a macro of that name can answer it
            args = node.child_by_field_name("arguments") or (node if node.type in ("argument_list", "arguments") else None)
            argc = len([a for a in args.named_children if a.type != "comment"]) if args is not None else 0
            if node.type == "token_tree":
                inner = node.children[1:-1]
                argc = sum(1 for x in inner if x.type == ",") + 1 if inner else 0
            site = CallSite(src_id, name, receiver, None, argc, name_node.start_point[0] + 1, enclosing_type,
                            name_node.start_point[1])
            res.calls.append(site)
            sites.append((site, node, args))
            site_at[(node.start_byte, node.end_byte)] = site
        for site, node, args in sites:
            # What the text shows of an argument's type, and of the value a call is made on when it is
            # itself an expression: `new Foo().run()`, `a.make().run()` (resolved by the indexer).
            if args is not None and args.type != "token_tree":
                hints = tuple(_arg_hint(a, site_at) for a in args.named_children if a.type != "comment")
                site.args = hints if any(h is not None for h in hints) else ()
            if site.receiver == "?":
                obj = _callee_object(node)
                if obj is not None and obj.type in _NEW:
                    tnode = obj.child_by_field_name("type") or obj.child_by_field_name("constructor") or \
                        (obj.named_children[0] if obj.named_children else None)
                    site.receiver_type = _type_head(_text(tnode)) if tnode is not None else None
                elif obj is not None and (obj.start_byte, obj.end_byte) in site_at:
                    site.chain = site_at[(obj.start_byte, obj.end_byte)]
        by_id = {n.id: n for n in res.nodes}
        for c in res.calls:        # a method written outside its type's block still belongs to the type
            if c.enclosing_type is None and c.src_id in by_id and by_id[c.src_id].attrs.get("type_id"):
                c.enclosing_type = by_id[c.src_id].attrs["type_id"]

        seen_refs = set()
        for name_node, node, role in refs:
            name = _text(name_node).split("::")[-1].split(".")[-1].strip()
            if role == "skip" or not re.fullmatch(r"[A-Za-z_][\w]*", name):
                continue
            home = inside(name_node.start_byte, ("callable", "type"))
            if home is None or (home[0] <= name_node.start_byte and _named_here(by_id.get(home[2]), name, name_node, role)):
                continue
            if (home[2], name, role) not in seen_refs:
                seen_refs.add((home[2], name, role))
                res.type_refs.append(TypeRef(home[2], [name], role, name_node.start_point[0] + 1))

        # Imports and fields come from the tree's shape in either mode.
        stack = [root]
        kinds = self._io_shape
        while stack:
            n = stack.pop()
            t = n.type
            got = kinds.get(t)
            if got is None:
                got = kinds[t] = (_IMPORT.match(t) is not None, _FIELD.match(t) is not None)
            if got[0]:
                for target in _import_targets(n):
                    res.imports.append(ImportRef(file_id, target))
                continue
            if got[1]:
                home = inside(n.start_byte, ("callable", "type"))
                if home is not None and home[3] == "type":
                    nm = _name_of(n)
                    nm = _last_name(nm) if nm is not None else None
                    fname = _text(nm).strip() if nm is not None else ""
                    fid = f"{home[2]}.{fname}"
                    if re.fullmatch(r"[A-Za-z_@$][\w$]*", fname) and fid not in by_id:
                        tnode = n.child_by_field_name("type")
                        field = Node(id=fid, kind="field", name=fname, parent_id=home[2], language=lang, path=rel_path,
                                     span_start=n.start_point[0] + 1, span_end=n.end_point[0] + 1,
                                     attrs={"native_kind": "field", "declared_type": _text(tnode)[:80] or None,
                                            "type_name": (re.findall(r"[A-Z]\w*", _text(tnode)) or [None])[-1] if tnode is not None else None,
                                            "visibility": "private" if "private" in _text(n)[:40] else "public", "is_mutable": True})
                        by_id[fid] = field
                        res.nodes.append(field)
                        res.edges.append(Edge("has_field", home[2], fid))
                    continue
            stack.extend(n.named_children)

        if top_used or force_top[0]:
            res.nodes.append(Node(id=top_id, kind="callable", name="<module>", parent_id=file_id, language=lang, path=rel_path,
                                  span_start=1, span_end=root.end_point[0] + 1,
                                  attrs={"signature": f"module {rel_path}", "visibility": "public", "native_kind": "module_body",
                                         "argc_min": 0, "argc_max": 0}))
        return res


def _type_head(text: str) -> Optional[str]:
    """The type a declaration names, without what decorates it: `Class<T>` -> Class, `&'a mut io::Read` -> Read,
    `java.io.Reader` -> Reader, `[]string` -> string."""
    t = text
    while True:
        u = re.sub(r"<[^<>]*>|\[[^\[\]]*\]", "", t)
        if u == t:
            break
        t = u
    t = re.sub(r"'\w+|\b(mut|dyn|impl|const|final|in|out|ref)\b|\.\.\.|[*&?]", " ", t)
    words = re.findall(r"[A-Za-z_]\w*", t)
    return words[-1] if words else None


def _param_types(plist) -> list:
    """Each parameter's type head, in order (Go's `a, b int` is two of them); None where none is written.
    A trailing `...` marks a parameter that takes the rest of the arguments."""
    out = []
    for p in plist:
        tnode = p.child_by_field_name("type")
        if tnode is None and ("spread" in p.type or "variadic" in p.type):
            tnode = next((c for c in p.named_children if "type" in c.type), None)
        head = _type_head(_text(tnode)) if tnode is not None else None
        names = p.children_by_field_name("name") if tnode is not None else []
        rest = "..." in _text(p) or "spread" in p.type or "variadic" in p.type
        for _ in range(max(1, len(names))):
            out.append((head or "") + ("..." if rest else ""))
    return out if any(out) else []


_LITERALS = [(re.compile(r"char"), "char"), (re.compile(r"string|template"), "string"),
             (re.compile(r"^(true|false)$|bool"), "bool"), (re.compile(r"float|double|decimal_floating"), "float"),
             (re.compile(r"int|number|decimal|hex|octal|binary"), "int"), (re.compile(r"^(null|nil|none|null_literal|nil_literal)$"), "null")]


def _type_names(text: str) -> list:
    """The names in a written type, outer first, without package qualifiers: `*pkg.Command` -> [Command]."""
    text = re.sub(r"\b[a-z_]\w*\s*(::|\.)\s*", "", text)
    return [w for w in re.findall(r"[A-Za-z_]\w*", text) if w not in ("mut", "dyn", "impl", "const", "final", "ref", "in", "out")]


def _in_anonymous(node, outer_start: int) -> bool:
    """A definition sits in an object-creation expression's body (Java's anonymous class) below its outer type."""
    p = node.parent
    while p is not None and p.start_byte > outer_start:
        if p.type in _NEW or p.type in ("enum_constant", "enum_entry"):    # new Base() { }, an enum constant's own body
            return True
        p = p.parent
    return False


def _callee_object(call):
    """The expression a method call is made on: `a.b()` -> a, `x.f().g()` -> x.f()."""
    callee = call.child_by_field_name("function") or call
    if callee.type in ("generic_function", "generic_name"):
        callee = callee.child_by_field_name("function") or callee
    obj = None
    for f in ("object", "operand", "value", "receiver", "expression"):
        obj = callee.child_by_field_name(f)
        if obj is not None:
            break
    while obj is not None and obj.type == "parenthesized_expression" and obj.named_children:
        obj = obj.named_children[0]
    return obj


def _arg_hint(a, calls=None):
    """What an argument shows of its type: a literal's kind, a constructed or cast-to type, a class literal, a
    lambda, `$name` for a variable or `$a.b` for a field whose declared type the indexer looks up, or the
    call whose declared return type it is."""
    while a.type in ("unary_expression", "reference_expression", "parenthesized_expression", "argument", "value_argument") \
            and a.named_children and not (a.type == "unary_expression" and _text(a)[:1] in "-+!~"):
        a = a.named_children[-1]
    t = a.type
    if calls and (a.start_byte, a.end_byte) in calls and t not in _NEW:
        return calls[(a.start_byte, a.end_byte)]
    if t in _NAME_TYPES and not a.named_child_count:
        return "$" + _text(a)
    if t in ("field_access", "selector_expression", "field_expression", "member_access_expression", "member_expression") \
            and re.fullmatch(r"[A-Za-z_]\w*\s*\.\s*[A-Za-z_]\w*", _text(a)):
        return "$" + re.sub(r"\s+", "", _text(a))
    if "cast" in t or t == "as_expression":
        tnode = a.child_by_field_name("type")
        return _type_head(_text(tnode)) if tnode is not None else None
    if t in ("ternary_expression", "conditional_expression"):
        for f in ("consequence", "alternative"):
            branch = a.child_by_field_name(f)
            h = _arg_hint(branch, calls) if branch is not None else None
            if h is not None and h != "null":
                return h
        return None
    if t == "unary_expression" and _text(a)[:1] == "!":
        return "bool"
    if t == "unary_expression" and _text(a)[:1] in "-+" and a.named_children:
        return _arg_hint(a.named_children[-1], calls)
    if t in ("binary_expression", "binary_operator"):
        op = a.child_by_field_name("operator")
        op = _text(op) if op is not None else next((_text(c) for c in a.children if not c.is_named), "")
        if op in ("==", "!=", "<", ">", "<=", ">=", "&&", "||", "and", "or", "===", "!=="):
            return "bool"
        if op == "+" and any(_arg_hint(c) == "string" for c in a.named_children):
            return "string"     # "a" + b, a + " " + b
        return None
    if t == "class_literal":
        return "Class"
    if t in _NEW or t == "composite_literal":
        tnode = a.child_by_field_name("type") or a.child_by_field_name("name") or (a.named_children[0] if a.named_children else None)
        return _type_head(_text(tnode)) if tnode is not None else None
    if _FUNC_VALUE.match(t):
        return "fn"
    if t.endswith(("literal", "string", "integer", "float", "number")) or t in ("true", "false", "null", "nil", "none", "string_literal"):
        return next((kind for pat, kind in _LITERALS if pat.search(t)), None)
    return None


def _first_string(call):
    """The first argument when it is a string literal (the test's title)."""
    args = call.child_by_field_name("arguments") or next((c for c in call.named_children if "argument" in c.type), None)
    first = None
    for c in (args.named_children if args is not None else call.named_children[1:]):
        if c.type != "comment":
            first = c
            break
    while first is not None and first.type in ("argument", "value_argument", "string_content") and first.named_children:
        first = first.named_children[0]
    if first is not None and ("string" in first.type or first.type in ("template_string",)):
        return first
    return None


def _has_function(call) -> bool:
    for c in _walk(call):
        if c is not call and (_FUNC_VALUE.match(c.type) or c.type in ("do_block", "block", "lambda_literal", "statement_block")):
            if c.parent is not None and (c.parent is call or "argument" in c.parent.type or c.parent.parent is call):
                return True
    return False


def _same(a, b) -> bool:
    return a.start_byte == b.start_byte and a.end_byte == b.end_byte


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def _named_here(node: Optional[Node], name: str, name_node, role: str) -> bool:
    """A definition's own name is not a mention of some other type."""
    return node is not None and node.name == name and role != "base" and name_node.start_point[0] + 1 == node.span_start


_RECV = re.compile(rb"([A-Za-z_@$][\w$]*)?\s*(\)|\])?\s*(\?\.|\.|->|::|:|&\.)\s*$")


_WS = b" \t\n\r\x0b\x0c"
_WORD = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$@"


def _receiver(src: bytes, pos: int) -> Optional[str]:
    """What the text shows the call is made on: None for a bare call, `this`, `base`, a name, or ? for an expression."""
    text = src[max(0, pos - 80):pos]
    # _RECV tried at every position of the 80 bytes was a fifth of all parsing time. A match ends in one of the
    # operators and can only cover the name, bracket and spaces just before it, so the search starts there:
    # it finds the same match, since no match can start earlier.
    s = text.rstrip(_WS)
    if s[-1:] not in (b".", b">", b":"):
        return None
    s = s[:-2] if s[-2:-1] and s[-2:-1] in b"?.->:&" else s[:-1]
    s = s.rstrip(_WS)
    if s[-1:] in (b")", b"]"):
        s = s[:-1]
    m = _RECV.search(text, len(s.rstrip(_WS).rstrip(_WORD)))
    if not m:
        return None
    if m.group(3) == b":" and not src[max(0, pos - 80):pos].rstrip().endswith(b":"):
        return None
    if m.group(3) == b":":      # Lua's obj:method(); elsewhere a lone colon is a label or a type
        if not m.group(1) or m.group(2) or src[pos - 1:pos] != b":":
            return None         # `Args: ExactArgs(2)`, `{key: f()}`: a key, with space before the value
    if m.group(2) or not m.group(1):
        return "?"
    name = m.group(1).decode("utf8", "replace")
    if name in ("this", "self", "Self", "@", "me"):
        return "this"
    if name in ("super", "base", "parent"):
        return "base"
    return name


def _import_targets(node) -> list[str]:
    """What an import names, as written: quoted paths (JS, Go, C), or dotted / ::-separated ones.
    `from a.b import c, d` names a.b, a.b.c and a.b.d; `use a::b::{c, d}` names a::b, a::b::c and a::b::d."""
    strings, stack = [], [node]
    while stack:
        n = stack.pop()
        if n.type in ("string", "string_literal", "interpreted_string_literal", "system_lib_string", "raw_string_literal",
                      "string_content", "string_fragment") and not any(c.type.startswith("string") for c in n.named_children):
            strings.append(_text(n).strip("\"'<>`"))
        else:
            stack.extend(reversed(n.children))
    if strings:
        return [s for s in strings if s and len(s) < 200]
    text = re.sub(r"/\*.*?\*/|//[^\n]*|#[^\n]*", " ", _text(node))
    text = re.sub(r"\s+", " ", text).strip().rstrip(";")
    path = r"\.*[A-Za-z_][\w]*(?:(?:\.|::)[A-Za-z_*][\w]*)*|\.+"
    m = re.match(rf"from ({path}) import \(?(.+?)\)?$", text)
    if m:
        base, names = m.group(1), [x.strip().split(" as ")[0] for x in m.group(2).split(",")]
        sep = "" if base.endswith(".") else "."
        return [base] + [base + sep + n for n in names if re.fullmatch(r"\w+", n)][:20]
    m = re.match(rf"(?:pub(?:\([\w ]+\))? )?use ({path})(?:::)?\{{(.+)\}}", text)
    if m:
        base = m.group(1).rstrip(":")
        return [base] + [f"{base}::{n.strip().split(' as ')[0]}" for n in m.group(2).split(",") if re.fullmatch(r"\s*\w+.*", n)][:20]
    text = re.sub(r"^(?:pub(?:\([\w ]+\))? )?(?:import|use|using|include|require|open|extern crate|static|namespace)\b ?", "", text)
    text = re.sub(r"^(?:static|type|typeof|func|function|const|global::) ?", "", text)
    out = []
    for part in text.split(","):
        m = re.search(path, part.strip())
        if m and m.group(0) not in ("as", "from"):
            out.append(m.group(0))
    return out[:12]


def adapters(skip: tuple = ()) -> list:
    """One adapter per language whose grammar package is installed. `skip`: languages someone else handles."""
    out, seen = [], set()
    for language, exts, package, fn in LANGUAGES:
        if language in skip:
            continue
        a = Generic(language, exts, package, fn)
        if a.available():
            out.append(a)
        seen.add(language)
    return out


def missing(extensions: set) -> dict:
    """Extensions in a repo that a grammar exists for but is not installed: {package: [extensions]}."""
    out: dict = {}
    for language, exts, package, fn in LANGUAGES:
        hit = sorted(set(exts) & extensions)
        if hit and not Generic(language, exts, package, fn).available():
            out.setdefault(package.replace("_", "-"), []).extend(hit)
    return out
