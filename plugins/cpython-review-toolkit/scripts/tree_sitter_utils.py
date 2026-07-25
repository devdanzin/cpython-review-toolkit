#!/usr/bin/env python3
"""Tree-sitter parsing utilities for C/C++ extension analysis.

This is the core parsing module used by all analysis scripts in
cext-review-toolkit. It provides structured access to C/C++ source code
via Tree-sitter, replacing fragile regex-based parsing.

Requires: pip install tree-sitter tree-sitter-c
Optional: pip install tree-sitter-cpp (for C++ file support)
"""

import json
import re
import sys
from pathlib import Path

try:
    import tree_sitter
    import tree_sitter_c
except ImportError:
    print(
        json.dumps(
            {
                "error": "tree-sitter not installed",
                "install": "pip install tree-sitter tree-sitter-c",
            }
        )
    )
    sys.exit(1)

# Initialize the C parser once at module level.
C_LANGUAGE = tree_sitter.Language(tree_sitter_c.language())
_parser = tree_sitter.Parser(C_LANGUAGE)

# C++ support (optional).
_CPP_AVAILABLE = False
_cpp_parser: tree_sitter.Parser | None = None

try:
    import tree_sitter_cpp

    _CPP_AVAILABLE = True
except ImportError:
    pass

C_EXTENSIONS = frozenset({".c", ".h"})
CPP_EXTENSIONS = frozenset({".cpp", ".cxx", ".cc", ".hpp"})
ALL_SOURCE_EXTENSIONS = C_EXTENSIONS | CPP_EXTENSIONS


# ---------------------------------------------------------------------------
# Macro pre-substitution ("scrubbing")
# ---------------------------------------------------------------------------
# CPython -- and extensions that mimic its idioms -- uses macros that expand to
# unbalanced braces, bare declarations, or _Pragma() directives. Tree-sitter has
# no preprocessor, so these desynchronize the parse. Three failure modes, all
# measured against CPython main (Objects/, 50 files, 118k lines):
#
#   PRAGMA  _Py_COMP_DIAG_PUSH / _POP / _IGNORE_DEPR_DECLS expand to _Pragma().
#           This is the worst mode because it is SILENT: it produces no
#           top-level ERROR node, so a has_error check misses it. Instead the
#           parse nests every following function inside one record. At
#           Objects/object.c:1271 this swallowed 87 functions into a single
#           2,254-line "_PyObject_GetAttrId" record. Misattribution is worse
#           than omission -- a finding at line 3412 gets reported against a
#           function starting at line 1267, and that survives review as
#           plausible.
#   PUNCT   PyObject_HEAD and friends supply their own ';' or ',', so the use
#           site is a bare identifier where a declaration is expected.
#   BRACE   Py_BEGIN/END_ALLOW_THREADS, Py_BEGIN/END_CRITICAL_SECTION* and
#           _Py_BEGIN/END_SUPPRESS_IPH carry an unbalanced '{' or '}'.
#
# Substituting these three classes is a measured win: +192 functions, -28%
# ERROR nodes, the largest single record drops from 2,254 lines to 127, and
# Objects/dictobject.c goes from 187 functions (extraction dying at line 5239
# of 8598) to 292 with the whole frozendict family recovered.
#
# Two things that look like they belong here do NOT, and were rejected on
# measurement rather than taste:
#
#   * Argument Clinic "*_METHODDEF" element substitution makes things WORSE:
#     dictobject.c drops from 292 functions back to 72, and critical-section
#     sites falling outside any function go from 3 to 47.
#   * Recovering function_definition nodes from inside ERROR nodes yields only
#     garbage -- 5 candidates tree-wide, every one an "else if" fragment.
#
# Substitution is byte-length- AND line-count-preserving, so node.start_byte,
# node.end_byte and node.start_point still index the ORIGINAL source. Callers
# keep passing the original bytes to get_node_text(), so extracted text and
# reported line numbers still show real source -- including the macro names
# themselves, which is why the text-based macro searches in scan_gil_usage.py
# and scan_lock_discipline.py keep working unchanged.

# Macros carrying an unbalanced '{'.
_BRACE_OPEN_MACROS = frozenset(
    {
        "Py_BEGIN_ALLOW_THREADS",
        "Py_BEGIN_CRITICAL_SECTION",
        "Py_BEGIN_CRITICAL_SECTION2",
        "Py_BEGIN_CRITICAL_SECTION_MUTEX",
        "Py_BEGIN_CRITICAL_SECTION2_MUTEX",
        "Py_BEGIN_CRITICAL_SECTION_SEQUENCE_FAST",
        "_Py_BEGIN_SUPPRESS_IPH",
    }
)

# Macros carrying an unbalanced '}'. Note there is no Py_END_CRITICAL_SECTION_MUTEX:
# the _MUTEX and non-_MUTEX openers share the same closer.
#
# Py_TRASHCAN_BEGIN / Py_TRASHCAN_END are deliberately absent. They LOOK like a
# brace pair but on CPython main (Include/cpython/object.h:446-447) both expand
# to nothing at all -- adding them here would inject a spurious brace pair.
_BRACE_CLOSE_MACROS = frozenset(
    {
        "Py_END_ALLOW_THREADS",
        "Py_END_CRITICAL_SECTION",
        "Py_END_CRITICAL_SECTION2",
        "Py_END_CRITICAL_SECTION_SEQUENCE_FAST",
        "_Py_END_SUPPRESS_IPH",
    }
)

# Macros expanding to one or more ';'-terminated struct members or declarations.
# The replacement must be no longer than the text it replaces, since the
# substitution is length-preserving.
#
# The stand-ins are deliberately opaque "char" arrays rather than the truthful
# expansion ("PyObject ob_base;" for PyObject_HEAD). A truthful stand-in makes
# the synthetic member VISIBLE to find_struct_members(), and scan_type_slots.py
# then reports the object header itself as a PyObject* member that tp_traverse
# forgot to visit. These macro-hidden fields were invisible to the scanners
# before this substitution existed, and they must stay invisible; the point of
# the stand-in is only to keep the struct parseable.
_DECLARATION_MACROS = {
    "PyObject_HEAD": "char oh_[8];",
    "PyObject_VAR_HEAD": "char ovh_[16];",
    "PyException_HEAD": "char eh_[8];",
    "_PyGenObject_HEAD": "char gh_[8];",
    "_PyTZINFO_HEAD": "char th_[8];",
    "_PyDateTime_TIMEHEAD": "char dth_[8];",
    "_PyDateTime_DATETIMEHEAD": "char ddh_[8];",
    "_Py_COMMON_FIELDS": "char cf_[8];",
    "_ASDL_SEQ_HEAD": "char sh_[8];",
    "PyABIInfo_VAR": "char av_[8];",
    "STRUCT_FOR_ID": "char si_[8];",
}

# Macros expanding to a ','-terminated braced initializer element.
_ELEMENT_MACROS = frozenset({"PyObject_HEAD_INIT", "PyVarObject_HEAD_INIT"})

# Macros expanding to a _Pragma() directive (or, on unsupported compilers,
# to nothing). Blanked out entirely.
_ERASE_MACROS = frozenset(
    {
        "_Py_COMP_DIAG_PUSH",
        "_Py_COMP_DIAG_POP",
        "_Py_COMP_DIAG_IGNORE_DEPR_DECLS",
    }
)

# Macros used as a BARE identifier, with no argument list. Never consume a
# following "(...)" for these. This matters: CPython writes
#
#     Py_BEGIN_ALLOW_THREADS
#     (void)closesocket(fd);
#
# in three places (signalmodule.c, socketmodule.c, _sqlite/connection.c). A
# naive paren-matcher skips the newline and swallows the "(void)" cast, which
# both corrupts real code and destroys a line.
_ARGLESS_MACROS = frozenset(
    {
        "Py_BEGIN_ALLOW_THREADS",
        "Py_END_ALLOW_THREADS",
        "_Py_BEGIN_SUPPRESS_IPH",
        "_Py_END_SUPPRESS_IPH",
        "PyObject_HEAD",
        "PyObject_VAR_HEAD",
        "PyException_HEAD",
        "_PyTZINFO_HEAD",
        "_PyDateTime_TIMEHEAD",
        "_PyDateTime_DATETIMEHEAD",
        "_ASDL_SEQ_HEAD",
    }
    | _ERASE_MACROS
)

_MACRO_IDENT_RE = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
# Matches the text between the start of a line and a macro name, when that name
# is the one being #define'd. Used to leave a macro's own definition alone.
_DEFINE_PREFIX_RE = re.compile(rb"^[ \t]*#[ \t]*define[ \t]+$")

# Macros whose expansion is an ENTIRE FUNCTION DEFINITION, invoked at file
# scope. The value is the index of the argument holding the generated function's
# name.
#
# These are the most destructive macros in the tree, and not for the obvious
# reason. The generated function is of course invisible -- tree-sitter cannot
# expand a macro. The real damage is that the unparseable invocation corrupts the
# parse of the REAL, hand-written functions that follow it. Measured on
# Objects/typeobject.c: 35 invocations produced 47 ERROR nodes, and the wreckage
# swallowed `slot_tp_hash`, `slot_tp_call`, `_Py_slot_tp_getattro`,
# `slot_tp_repr`, `slot_tp_str` and `call_attribute` -- ordinary functions that
# dispatch into user Python, i.e. exactly the population every crash-class rule
# is meant to police. Substituting a byte-length-preserving `int NAME(){}` stub
# takes that file from 417 functions / 47 errors to 459 / 5, losing nothing.
#
# The stub deliberately carries no body: the body lives in the #define, not
# here, so a rule that inspects it correctly finds nothing rather than something
# wrong. What the stub restores is the NAME and the LINE, and the parse of
# everything after it.
_FUNCTION_MACROS = {
    "SLOT0": 0,
    "SLOT1": 0,
    "SLOT1BIN": 0,
    "SLOT1BINFULL": 0,
}


def _split_macro_args(text: bytes) -> list[bytes]:
    """Split a macro argument list's interior on top-level commas."""
    args: list[bytes] = []
    depth = 0
    current = bytearray()
    for byte in text:
        char = bytes([byte])
        if char in b"([":
            depth += 1
        elif char in b")]":
            depth -= 1
        if char == b"," and depth == 0:
            args.append(bytes(current).strip())
            current = bytearray()
        else:
            current += char
    args.append(bytes(current).strip())
    return args


_MACRO_NAME_RE = re.compile(rb"^[A-Za-z_]\w*$")


def _macro_arg_end(source_bytes: bytes, pos: int) -> int:
    """Return the index just past the "(...)" beginning at/after ``pos``.

    Returns ``pos`` unchanged when there is no argument list. Bails out on a
    ';' inside the parentheses, which means we mis-detected the construct.
    """
    i = pos
    n = len(source_bytes)
    while i < n and source_bytes[i : i + 1] in (b" ", b"\t", b"\n", b"\r"):
        i += 1
    if i >= n or source_bytes[i : i + 1] != b"(":
        return pos
    depth = 0
    while i < n:
        char = source_bytes[i : i + 1]
        if char == b"(":
            depth += 1
        elif char == b")":
            depth -= 1
            if depth == 0:
                return i + 1
        elif char == b";":
            return pos
        i += 1
    return pos


def _pad_replacement(replacement: bytes, original_span: bytes) -> bytes | None:
    """Pad ``replacement`` to the exact byte length AND newline count of the span.

    Newlines are re-emitted at the end of the padding. Byte offsets after the
    span are therefore unchanged, and so is every line number after it. Returns
    None if the replacement does not fit, in which case the caller must skip the
    substitution rather than corrupt offsets.
    """
    newlines = original_span.count(b"\n")
    fill = len(original_span) - len(replacement) - newlines
    if fill < 0:
        return None
    return replacement + b" " * fill + b"\n" * newlines


def _follows_label(source_bytes: bytes, pos: int) -> bool:
    """True if the nearest non-whitespace byte before ``pos`` ends a goto label.

    The free-threading idiom ``goto exit; ... exit: Py_END_CRITICAL_SECTION();``
    leaves a label immediately before the closing macro, and ``label: }`` is not
    a valid statement. Emitting ``;}`` there rather than ``}`` is worth 62
    functions in Objects/listobject.c alone; emitting it unconditionally
    regresses other files, so it is applied only in this position.
    """
    i = pos - 1
    while i >= 0 and source_bytes[i : i + 1].isspace():
        i -= 1
    if i < 0 or source_bytes[i : i + 1] != b":":
        return False
    # Exclude C++ "::" and the ternary "? :".
    return source_bytes[i - 1 : i] not in (b":", b"?")


def scrub_macros(source_bytes: bytes) -> bytes:
    """Neutralize parse-breaking CPython macros, preserving bytes and lines.

    Returns source of the exact same byte length and the exact same newline
    count, with the macros listed in the tables above replaced by parseable
    stand-ins padded with spaces. Every ``node.start_byte`` / ``node.end_byte``
    / ``node.start_point`` derived from the result therefore still indexes the
    ORIGINAL source, and callers should keep passing the ORIGINAL bytes to
    ``get_node_text``.

    The tables are CPython-specific by design but harmless elsewhere: a project
    that never uses these macro names gets its source back unchanged. Extensions
    with their own brace-carrying macros can extend the frozensets above.
    """
    out = bytearray(source_bytes)
    changed = False

    for match in _MACRO_IDENT_RE.finditer(source_bytes):
        name = match.group(0).decode("ascii", errors="replace")
        if name in _BRACE_OPEN_MACROS:
            kind = "open"
        elif name in _BRACE_CLOSE_MACROS:
            kind = "close"
        elif name in _DECLARATION_MACROS:
            kind = "declaration"
        elif name in _ELEMENT_MACROS:
            kind = "element"
        elif name in _ERASE_MACROS:
            kind = "erase"
        elif name in _FUNCTION_MACROS:
            kind = "function"
        else:
            continue

        start = match.start()
        end = match.end()
        if name not in _ARGLESS_MACROS:
            end = _macro_arg_end(source_bytes, end)

        # Leave the macro's own "#define NAME ..." line intact.
        line_start = source_bytes.rfind(b"\n", 0, start) + 1
        if _DEFINE_PREFIX_RE.match(source_bytes[line_start:start]):
            continue

        if kind == "function":
            # Only a FILE-SCOPE invocation defines a function. The same name
            # appearing mid-expression is something else and must be left alone.
            if source_bytes[line_start:start].strip():
                continue
            inner = source_bytes[match.end() : end].strip()
            if not (inner.startswith(b"(") and inner.endswith(b")")):
                continue
            args = _split_macro_args(inner[1:-1])
            index = _FUNCTION_MACROS[name]
            if index >= len(args) or not _MACRO_NAME_RE.match(args[index]):
                continue
            region = source_bytes[start:end]
            # The generated name must stay at its ORIGINAL byte offset. Callers
            # pass the unscrubbed source to get_node_text, so a stub that shifts
            # the identifier (e.g. a plain "int NAME(){}" prefix) makes every
            # consumer read the wrong bytes and yields a garbled function name.
            # Overlay in place instead: "int" + filler, the untouched name, then
            # "(){}".
            name_at = region.find(args[index])
            if name_at < 3:
                continue
            stub = (
                b"int"
                + b" " * (name_at - 3)
                + args[index]
                + b"(){}"
            )
            padded = _pad_replacement(stub, region)
            if padded is None:
                continue
            out[start:end] = padded
            changed = True
            continue

        if kind == "open":
            replacement = b"{"
        elif kind == "close":
            replacement = b";}" if _follows_label(source_bytes, start) else b"}"
        elif kind == "declaration":
            replacement = _DECLARATION_MACROS[name].encode("ascii")
        elif kind == "element":
            replacement = b"{0},"
        else:
            replacement = b""

        padded = _pad_replacement(replacement, source_bytes[start:end])
        if padded is None:
            continue
        out[start:end] = padded
        changed = True

    return bytes(out) if changed else source_bytes


def is_cpp_available() -> bool:
    """Check if tree-sitter-cpp is installed."""
    return _CPP_AVAILABLE


def _get_cpp_parser() -> tree_sitter.Parser:
    """Lazily initialize and return the C++ parser."""
    global _cpp_parser
    if _cpp_parser is None:
        if not _CPP_AVAILABLE:
            raise ImportError(
                "tree-sitter-cpp not installed: pip install tree-sitter-cpp"
            )
        cpp_language = tree_sitter.Language(tree_sitter_cpp.language())
        _cpp_parser = tree_sitter.Parser(cpp_language)
    return _cpp_parser


def get_parser_for_file(filepath: Path) -> tree_sitter.Parser:
    """Return the appropriate parser for a file based on its extension."""
    if filepath.suffix in CPP_EXTENSIONS and _CPP_AVAILABLE:
        return _get_cpp_parser()
    return _parser


def parse_bytes_for_file(
    source_bytes: bytes, filepath: Path, *, scrub: bool = True
) -> tree_sitter.Tree:
    """Parse source bytes using the parser appropriate for the file type.

    ``scrub`` applies :func:`scrub_macros` first. It is on by default and is
    offset-transparent -- keep passing the ORIGINAL ``source_bytes`` to
    ``get_node_text`` and friends.
    """
    parser = get_parser_for_file(filepath)
    if scrub:
        source_bytes = scrub_macros(source_bytes)
    return parser.parse(source_bytes)


def parse_file(path: Path, *, scrub: bool = True) -> tree_sitter.Tree:
    """Parse a C source file and return the Tree-sitter syntax tree."""
    return parse_bytes(path.read_bytes(), scrub=scrub)


def parse_string(source: str, *, scrub: bool = True) -> tree_sitter.Tree:
    """Parse a C source string and return the Tree-sitter syntax tree."""
    return parse_bytes(source.encode("utf-8"), scrub=scrub)


def parse_bytes(source_bytes: bytes, *, scrub: bool = True) -> tree_sitter.Tree:
    """Parse C source from bytes already in memory.

    ``scrub`` applies :func:`scrub_macros` first, neutralizing CPython macros
    that would otherwise desynchronize the parse. Because scrubbing preserves
    byte length and line count, every offset in the returned tree still indexes
    the ORIGINAL ``source_bytes``; pass those originals to ``get_node_text``.
    """
    if scrub:
        source_bytes = scrub_macros(source_bytes)
    return _parser.parse(source_bytes)


def get_node_text(node: tree_sitter.Node, source_bytes: bytes) -> str:
    """Get the source text for a tree-sitter node."""
    return source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )


def walk_descendants(node: tree_sitter.Node, type_filter: str | None = None):
    """Yield all descendant nodes, optionally filtered by node type.

    Common type names: 'call_expression', 'return_statement',
    'if_statement', 'goto_statement', 'declaration', 'assignment_expression',
    'binary_expression', 'identifier', 'string_literal'
    """
    cursor = node.walk()
    visited = False
    while True:
        if not visited:
            current = cursor.node
            if type_filter is None or current.type == type_filter:
                yield current
            if cursor.goto_first_child():
                visited = False
                continue
        if cursor.goto_next_sibling():
            visited = False
            continue
        if cursor.goto_parent():
            visited = True
            continue
        break


def get_declarator_name(node: tree_sitter.Node, source_bytes: bytes) -> str | None:
    """Extract the identifier name from a declarator, handling pointers and arrays."""
    if node.type in ("identifier", "field_identifier"):
        return get_node_text(node, source_bytes)
    if node.type == "pointer_declarator":
        decl = node.child_by_field_name("declarator")
        if decl:
            return get_declarator_name(decl, source_bytes)
    if node.type == "array_declarator":
        decl = node.child_by_field_name("declarator")
        if decl:
            return get_declarator_name(decl, source_bytes)
    if node.type == "parenthesized_declarator":
        for child in node.children:
            name = get_declarator_name(child, source_bytes)
            if name:
                return name
    if node.type == "function_declarator":
        decl = node.child_by_field_name("declarator")
        if decl:
            return get_declarator_name(decl, source_bytes)
    for child in node.children:
        if child.type in ("identifier", "field_identifier"):
            return get_node_text(child, source_bytes)
    return None


def _get_function_declarator(node: tree_sitter.Node) -> tree_sitter.Node | None:
    """Find the function_declarator within a declarator tree."""
    if node.type == "function_declarator":
        return node
    for child in node.children:
        result = _get_function_declarator(child)
        if result:
            return result
    return None


# Node types that WRAP function definitions rather than being them. When
# collecting top-level definitions we descend into these so nested functions
# are still found. The preproc_* entries matter for header-only C extensions:
# an entire header body wrapped in an "#ifndef FOO_H ... #endif" include guard
# parses as a single preproc_ifdef node, so a root-children-only walk would
# find ZERO functions in it.
_WRAPPER_NODE_TYPES = frozenset(
    {
        "linkage_specification",  # extern "C" { ... }
        "namespace_definition",  # namespace X { ... }
        "preproc_ifdef",  # #ifndef / #ifdef ... #endif  (include guards)
        "preproc_if",  # #if ... #endif
        "preproc_elif",  # #elif ...
        "preproc_else",  # #else ...
    }
)


def _collect_top_level_nodes(nodes: list) -> list:
    """Flatten a node list, descending into extern-C / namespace / preprocessor
    wrapper nodes so function definitions nested inside them are still found.

    extern "C" and namespace hold their members under a ``body`` field;
    preprocessor conditionals hold them as direct children. Recurses so nested
    wrappers (e.g. an ``#if PY_VERSION_HEX`` inside an ``#ifndef`` guard) work.
    """
    result = []
    for node in nodes:
        if node.type in ("linkage_specification", "namespace_definition"):
            body = node.child_by_field_name("body")
            result.extend(
                _collect_top_level_nodes(body.children if body else node.children)
            )
        elif node.type in _WRAPPER_NODE_TYPES:
            result.extend(_collect_top_level_nodes(node.children))
        else:
            result.append(node)
    return result


def extract_functions(tree: tree_sitter.Tree, source_bytes: bytes) -> list[dict]:
    """Extract all function definitions from a parse tree.

    Returns list of dicts with keys:
      - name: str (function name)
      - return_type: str
      - parameters: str (raw parameter text)
      - body: str (function body text, excluding braces)
      - body_node: tree_sitter.Node (the compound_statement node)
      - start_line: int (1-indexed)
      - end_line: int (1-indexed)
      - start_byte: int
      - end_byte: int
    """
    functions = []
    root = tree.root_node

    # Collect top-level nodes, descending into extern "C" {} / namespace {}
    # blocks and #ifndef/#ifdef/#if include guards + conditionals, all of which
    # wrap function definitions. Without the preproc descent, a header-only
    # extension whose entire body sits inside an "#ifndef FOO_H" guard yields
    # zero functions.
    top_nodes = _collect_top_level_nodes(root.children)

    for node in top_nodes:
        if node.type != "function_definition":
            continue

        declarator = node.child_by_field_name("declarator")
        body_node = node.child_by_field_name("body")
        if not declarator or not body_node:
            continue

        # Get the return type: everything before the declarator.
        return_type_parts = []
        for child in node.children:
            if child == declarator:
                break
            if child.type not in ("comment",):
                return_type_parts.append(get_node_text(child, source_bytes))
        return_type = " ".join(return_type_parts).strip()

        # Find the function_declarator to get name and params.
        func_decl = _get_function_declarator(declarator)
        if not func_decl:
            continue

        name_node = func_decl.child_by_field_name("declarator")
        params_node = func_decl.child_by_field_name("parameters")

        if not name_node:
            continue

        func_name = get_declarator_name(name_node, source_bytes)
        if not func_name:
            continue

        params_text = ""
        if params_node:
            params_text = get_node_text(params_node, source_bytes)
            # Strip outer parentheses.
            if params_text.startswith("(") and params_text.endswith(")"):
                params_text = params_text[1:-1].strip()

        # Body text: strip outer braces.
        body_text = get_node_text(body_node, source_bytes)
        if body_text.startswith("{") and body_text.endswith("}"):
            body_text = body_text[1:-1]

        functions.append(
            {
                "name": func_name,
                "return_type": return_type,
                "parameters": params_text,
                "body": body_text,
                "body_node": body_node,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
            }
        )

    return functions


def extract_struct_initializers(
    tree: tree_sitter.Tree, source_bytes: bytes, type_name: str
) -> list[dict]:
    """Find static struct initializers for a given type name.

    e.g., extract_struct_initializers(tree, source, "PyMethodDef") finds:
      static PyMethodDef module_methods[] = { ... };

    Returns list of dicts with keys:
      - variable_name: str
      - type_name: str
      - is_array: bool
      - initializer_text: str (the { ... } content)
      - initializer_node: tree_sitter.Node
      - start_line: int
      - end_line: int
    """
    results = []
    root = tree.root_node

    for node in root.children:
        if node.type != "declaration":
            continue

        decl_text = get_node_text(node, source_bytes)
        # Check if the type name appears in the declaration.
        if type_name not in decl_text:
            continue

        # Look for the type specifier.
        type_node = node.child_by_field_name("type")
        if type_node:
            type_text = get_node_text(type_node, source_bytes)
            if type_name not in type_text:
                # Also check if it's "struct type_name"
                found = False
                for desc in walk_descendants(type_node):
                    if (
                        desc.type == "type_identifier"
                        and get_node_text(desc, source_bytes) == type_name
                    ):
                        found = True
                        break
                if not found:
                    continue

        # Find init_declarator children to get variable name and initializer.
        for child in node.children:
            if child.type != "init_declarator":
                continue

            declarator = child.child_by_field_name("declarator")
            value = child.child_by_field_name("value")
            if not declarator or not value:
                continue

            var_name = get_declarator_name(declarator, source_bytes)
            if not var_name:
                continue

            is_array = "array_declarator" in get_node_text(
                declarator, source_bytes
            ) or "[" in get_node_text(declarator, source_bytes)

            init_text = get_node_text(value, source_bytes)

            results.append(
                {
                    "variable_name": var_name,
                    "type_name": type_name,
                    "is_array": is_array,
                    "initializer_text": init_text,
                    "initializer_node": value,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                }
            )

    return results


def extract_static_declarations(
    tree: tree_sitter.Tree, source_bytes: bytes
) -> list[dict]:
    """Find all file-scope static variable declarations.

    Returns list of dicts with keys:
      - name: str (variable name)
      - type: str (full type including qualifiers, e.g., "static PyObject *")
      - is_const: bool
      - is_pointer: bool
      - is_pyobject: bool (type contains "PyObject")
      - initializer: str | None
      - start_line: int
    """
    results = []
    root = tree.root_node

    for node in root.children:
        if node.type != "declaration":
            continue

        decl_text = get_node_text(node, source_bytes)
        # Must be static.
        if not decl_text.lstrip().startswith("static"):
            has_static = False
            for child in node.children:
                if (
                    child.type == "storage_class_specifier"
                    and get_node_text(child, source_bytes) == "static"
                ):
                    has_static = True
                    break
            if not has_static:
                continue

        # Skip function declarations (those have function_declarator).
        is_func_decl = False
        for desc in walk_descendants(node, "function_declarator"):
            is_func_decl = True
            break
        if is_func_decl:
            continue

        # Get type info.
        is_const = "const" in decl_text.split("=")[0]

        # Find each declared variable.
        for child in node.children:
            if child.type == "init_declarator":
                declarator = child.child_by_field_name("declarator")
                value = child.child_by_field_name("value")
                if not declarator:
                    continue
                var_name = get_declarator_name(declarator, source_bytes)
                if not var_name:
                    continue
                decl_part = decl_text.split("=")[0].strip().rstrip(";").strip()
                is_pointer = "*" in decl_part
                is_pyobject = "PyObject" in decl_part
                init_text = get_node_text(value, source_bytes) if value else None
                results.append(
                    {
                        "name": var_name,
                        "type": decl_part.rsplit(var_name, 1)[0].strip()
                        if var_name in decl_part
                        else decl_part,
                        "is_const": is_const,
                        "is_pointer": is_pointer,
                        "is_pyobject": is_pyobject,
                        "initializer": init_text,
                        "start_line": node.start_point[0] + 1,
                    }
                )
            elif child.type in ("identifier", "pointer_declarator", "array_declarator"):
                # Declaration without initializer.
                var_name = get_declarator_name(child, source_bytes)
                if not var_name:
                    continue
                decl_part = decl_text.strip().rstrip(";").strip()
                is_pointer = "*" in decl_part
                is_pyobject = "PyObject" in decl_part
                results.append(
                    {
                        "name": var_name,
                        "type": decl_part.rsplit(var_name, 1)[0].strip()
                        if var_name in decl_part
                        else decl_part,
                        "is_const": is_const,
                        "is_pointer": is_pointer,
                        "is_pyobject": is_pyobject,
                        "initializer": None,
                        "start_line": node.start_point[0] + 1,
                    }
                )

    return results


# Macros that scrub_macros() removes from the tree AND that carry an argument
# list -- i.e. the ones that tree-sitter parsed as a call_expression before the
# substitution existed. find_calls_in_scope() re-surfaces these on request so a
# scanner asking for them by name still sees them.
_CALL_LIKE_SCRUBBED_MACROS = frozenset(
    (
        _BRACE_OPEN_MACROS
        | _BRACE_CLOSE_MACROS
        | _ELEMENT_MACROS
        | frozenset(_DECLARATION_MACROS)
    )
    - _ARGLESS_MACROS
)


def _find_scrubbed_macro_calls(
    node: tree_sitter.Node, source_bytes: bytes, api_names: set[str]
) -> list[dict]:
    """Re-surface macro invocations that scrub_macros() erased from the tree.

    Scrubbing replaces ``Py_BEGIN_CRITICAL_SECTION(self)`` with ``{``, so it is
    no longer a call_expression. scan_lock_discipline.py finds critical sections
    through the AST rather than by text search, and would otherwise report ZERO
    findings on exactly the unbalanced code it exists to catch.

    Only macros the caller named explicitly in ``api_names`` are returned, so
    broad ``api_names=None`` walks keep their existing behaviour. The scanners
    doing those broad walks either skip Py*/_Py* names outright or match against
    refcount/nullable API tables that contain none of these macros.
    """
    wanted = api_names & _CALL_LIKE_SCRUBBED_MACROS
    if not wanted:
        return []

    results = []
    region = source_bytes[node.start_byte : node.end_byte]
    for match in _MACRO_IDENT_RE.finditer(region):
        name = match.group(0).decode("ascii", errors="replace")
        if name not in wanted:
            continue
        start = node.start_byte + match.start()
        ident_end = node.start_byte + match.end()
        end = _macro_arg_end(source_bytes, ident_end)
        if end == ident_end:
            # No argument list, so it was never a call_expression.
            continue
        args_text = source_bytes[ident_end:end].decode("utf-8", errors="replace").strip()
        if args_text.startswith("(") and args_text.endswith(")"):
            args_text = args_text[1:-1].strip()
        results.append(
            {
                "function_name": name,
                "arguments_text": args_text,
                # A real node so callers can use .parent / .end_byte safely. It
                # is the substituted construct (usually the '{' or '}'), not a
                # call_expression -- the macro call no longer exists in the tree.
                "node": node.descendant_for_byte_range(start, start),
                "start_line": source_bytes.count(b"\n", 0, start) + 1,
                "start_byte": start,
            }
        )
    return results


def find_calls_in_scope(
    node: tree_sitter.Node, source_bytes: bytes, api_names: set[str] | None = None
) -> list[dict]:
    """Find all function calls within a given AST node (typically a function body).

    If api_names is provided, only return calls to those functions. Naming a
    macro that :func:`scrub_macros` neutralizes (for example
    ``Py_BEGIN_CRITICAL_SECTION``) also returns that macro's invocations, which
    scrubbing removed from the tree.

    Returns list of dicts with keys:
      - function_name: str
      - arguments_text: str
      - node: tree_sitter.Node
      - start_line: int
      - start_byte: int
    """
    results = []
    for call_node in walk_descendants(node, "call_expression"):
        func_node = call_node.child_by_field_name("function")
        args_node = call_node.child_by_field_name("arguments")
        if not func_node:
            continue

        func_name = get_node_text(func_node, source_bytes)
        if api_names is not None and func_name not in api_names:
            continue

        args_text = ""
        if args_node:
            args_text = get_node_text(args_node, source_bytes)
            if args_text.startswith("(") and args_text.endswith(")"):
                args_text = args_text[1:-1].strip()

        results.append(
            {
                "function_name": func_name,
                "arguments_text": args_text,
                "node": call_node,
                "start_line": call_node.start_point[0] + 1,
                "start_byte": call_node.start_byte,
            }
        )

    if api_names is not None:
        macro_calls = _find_scrubbed_macro_calls(node, source_bytes, api_names)
        if macro_calls:
            results.extend(macro_calls)
            results.sort(key=lambda call: call["start_byte"])

    return results


def find_assignments_in_scope(
    node: tree_sitter.Node, source_bytes: bytes, var_name: str | None = None
) -> list[dict]:
    """Find variable assignments within a scope.

    If var_name is provided, only return assignments to that variable.

    Returns list of dicts with keys:
      - variable: str
      - value_text: str
      - value_node: tree_sitter.Node
      - is_declaration: bool (part of a declaration vs standalone assignment)
      - start_line: int
    """
    results = []

    # Find standalone assignments (assignment_expression).
    for assign_node in walk_descendants(node, "assignment_expression"):
        left = assign_node.child_by_field_name("left")
        right = assign_node.child_by_field_name("right")
        if not left or not right:
            continue
        assigned_var = get_node_text(left, source_bytes)
        if var_name is not None and assigned_var != var_name:
            continue
        results.append(
            {
                "variable": assigned_var,
                "value_text": get_node_text(right, source_bytes),
                "value_node": right,
                "is_declaration": False,
                "start_line": assign_node.start_point[0] + 1,
            }
        )

    # Find declaration-initializations (init_declarator inside declarations).
    for decl_node in walk_descendants(node, "init_declarator"):
        declarator = decl_node.child_by_field_name("declarator")
        value = decl_node.child_by_field_name("value")
        if not declarator or not value:
            continue
        declared_var = get_declarator_name(declarator, source_bytes)
        if not declared_var:
            continue
        if var_name is not None and declared_var != var_name:
            continue
        results.append(
            {
                "variable": declared_var,
                "value_text": get_node_text(value, source_bytes),
                "value_node": value,
                "is_declaration": True,
                "start_line": decl_node.start_point[0] + 1,
            }
        )

    return results


def find_return_statements(node: tree_sitter.Node, source_bytes: bytes) -> list[dict]:
    """Find all return statements within a scope.

    Returns list of dicts with keys:
      - value_text: str | None (None for bare 'return;')
      - node: tree_sitter.Node
      - start_line: int
    """
    results = []
    for ret_node in walk_descendants(node, "return_statement"):
        # A return statement's children: 'return' keyword, optional expression, ';'
        value_text = None
        for child in ret_node.children:
            if child.type not in ("return", ";", "comment"):
                value_text = get_node_text(child, source_bytes)
                break
        results.append(
            {
                "value_text": value_text,
                "node": ret_node,
                "start_line": ret_node.start_point[0] + 1,
            }
        )
    return results


def find_struct_members(
    tree: tree_sitter.Tree, source_bytes: bytes, struct_name: str
) -> list[dict]:
    """Find members of a named struct definition.

    Returns list of dicts with keys:
      - name: str
      - type: str
      - is_pyobject: bool
      - start_line: int
    """
    results = []

    # Look for struct definitions in type_definition or struct_specifier nodes.
    for node in walk_descendants(tree.root_node, "struct_specifier"):
        # Check if this struct has a name matching or a typedef name matching.
        name_node = node.child_by_field_name("name")
        struct_ident = get_node_text(name_node, source_bytes) if name_node else None

        # Check if this struct is inside a typedef with the target name.
        parent = node.parent
        is_match = False
        if struct_ident == struct_name:
            is_match = True
        elif parent and parent.type == "type_definition":
            # Check the typedef name.
            type_def_text = get_node_text(parent, source_bytes)
            if struct_name in type_def_text:
                # Find the declarator of the typedef.
                for child in parent.children:
                    if (
                        child.type == "type_identifier"
                        and get_node_text(child, source_bytes) == struct_name
                    ):
                        is_match = True
                        break

        if not is_match:
            continue

        # Find the field_declaration_list (body).
        body = node.child_by_field_name("body")
        if not body:
            continue

        for field in body.children:
            if field.type != "field_declaration":
                continue
            # Find field name from the declarator.
            declarator = field.child_by_field_name("declarator")
            if not declarator:
                continue
            field_name = get_declarator_name(declarator, source_bytes)
            if not field_name:
                continue

            # Get the type.
            type_parts = []
            for child in field.children:
                if child == declarator or child.type == ";":
                    break
                type_parts.append(get_node_text(child, source_bytes))
            field_type = " ".join(type_parts).strip()
            if "*" in get_node_text(declarator, source_bytes):
                field_type += " *"

            is_pointer = "*" in field_type
            is_pyobject = "PyObject" in field_type

            results.append(
                {
                    "name": field_name,
                    "type": field_type,
                    "is_pyobject": is_pyobject,
                    "is_pointer": is_pointer,
                    "start_line": field.start_point[0] + 1,
                }
            )

    return results


def strip_comments(source: str) -> str:
    """Remove C comments (/* */ and //) from source text, preserving line numbers.

    Simpler than tree-sitter for cases where we just need clean text.

    A multi-line block comment is replaced by a space plus the same number of
    newlines it contained, so ``strip_comments(src).count("\\n")`` always equals
    ``src.count("\\n")``. Without that, any caller computing a line number from
    the stripped text reports the wrong line: on Objects/genericaliasobject.c
    the naive version drifted 14 lines, and tree-wide it misplaced 111 of 113
    scan_null_checks findings in Objects/.

    Line comments are already line-preserving -- ``[^\\n]*`` stops at the
    newline.

    Known limitation (unchanged): string and character literals are not
    respected, so a "/*" inside a string literal is still treated as a comment
    opener.
    """
    # Remove block comments, keeping one newline per newline consumed.
    source = re.sub(
        r"/\*.*?\*/",
        lambda m: " " + "\n" * m.group(0).count("\n"),
        source,
        flags=re.DOTALL,
    )
    # Remove line comments (already line-preserving).
    source = re.sub(r"//[^\n]*", " ", source)
    return source


def parse_health(tree: tree_sitter.Tree, source_bytes: bytes) -> dict:
    """Report how well a file actually parsed, so a zero result is auditable.

    A scanner that returns zero findings is indistinguishable from a scanner
    whose parse silently collapsed -- the PRAGMA failure mode described above
    produces no top-level ERROR node at all, so ``tree.root_node.has_error`` is
    False while 87 functions sit merged inside one record. Surfacing these
    numbers alongside a result turns "zero findings" into a claim a reviewer can
    check.

    Returns a dict with keys:
      - error_nodes: int         ERROR nodes anywhere in the tree
      - missing_nodes: int       nodes tree-sitter inserted to recover
      - functions: int           functions extract_functions() found
      - lines_total: int         lines in the file
      - lines_attributed: int    distinct lines covered by some function
      - coverage: float          lines_attributed / lines_total, 0.0-1.0
      - max_function_span: int   longest function in lines
      - max_function_name: str   the function with that span

    Interpretation: a low ``coverage`` means whole regions are invisible to
    every per-function scanner. A ``max_function_span`` in the high hundreds or
    thousands almost always means several functions were merged into one record,
    so findings inside it will be attributed to the wrong function and the wrong
    line.
    """
    functions = extract_functions(tree, source_bytes)

    error_nodes = 0
    missing_nodes = 0
    for node in walk_descendants(tree.root_node):
        if node.type == "ERROR":
            error_nodes += 1
        if node.is_missing:
            missing_nodes += 1

    lines_total = source_bytes.count(b"\n") + 1 if source_bytes else 0

    attributed: set[int] = set()
    max_span = 0
    max_name = ""
    for func in functions:
        attributed.update(range(func["start_line"], func["end_line"] + 1))
        span = func["end_line"] - func["start_line"] + 1
        if span > max_span:
            max_span = span
            max_name = func["name"]

    return {
        "error_nodes": error_nodes,
        "missing_nodes": missing_nodes,
        "functions": len(functions),
        "lines_total": lines_total,
        "lines_attributed": len(attributed),
        "coverage": round(len(attributed) / lines_total, 4) if lines_total else 0.0,
        "max_function_span": max_span,
        "max_function_name": max_name,
    }
