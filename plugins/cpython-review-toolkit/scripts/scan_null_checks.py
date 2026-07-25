#!/usr/bin/env python3
"""Scan CPython C source for NULL pointer dereference risks.

Three rules, all reported with exact `file:line` coordinates:

``unchecked_alloc``
    A fallible allocation / ``PyObject *``-returning API result that is never
    NULL-checked *and* is then used in a way that dereferences it (``->``,
    subscript, a ``Py_TYPE``/``Py_SIZE``/``Py*_GET_ITEM``-style unsafe macro,
    or ``Py_INCREF``/``Py_DECREF``).  A result that is merely returned,
    ``Py_XDECREF``-ed or fed to ``Py_SETREF``/``Py_CLEAR`` is *not* reported:
    NULL propagation is the error-handling contract in CPython.

``deref_before_check``
    Same, except the NULL check does exist but the dereference reaches it
    first.  Gated on a dominator approximation: the dereference must sit at the
    same brace depth as the assignment, with no shallower point in between, so
    it is genuinely reached rather than merely textually earlier.

``decref_of_nulled_outparam``
    ``Py_DECREF(x)`` inside the failure branch of an API that NULLs its
    ``PyObject **`` out-parameter on every failure path (``_PyTuple_Resize`` and
    friends, plus auto-discovered local wrappers such as ``tuple_extend``).
    Guaranteed ``Py_DECREF(NULL)`` -> SIGSEGV.  This is the shape behind
    ``Objects/genericaliasobject.c:302``.

Usage:
    python scan_null_checks.py [path]

    path: directory, file, or omitted for current directory
"""

import json
import re
import sys
from pathlib import Path
from typing import Generator, Iterable


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def find_cpython_root(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    for _ in range(20):
        if (current / "Include" / "Python.h").is_file() and (
            current / "Objects" / "object.c"
        ).is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


_EXCLUDE_DIRS = frozenset({
    ".git", ".tox", ".venv", "venv", "__pycache__",
    "node_modules", "build", "dist", ".eggs",
})


def discover_c_files(
    root: Path, *, max_files: int = 0,
) -> Generator[Path, None, None]:
    count = 0
    if root.is_file():
        if root.suffix in (".c", ".h"):
            yield root
        return
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix not in (".c", ".h"):
            continue
        parts = set(p.relative_to(root).parts)
        if parts & _EXCLUDE_DIRS:
            continue
        yield p
        count += 1
        if max_files and count >= max_files:
            return


def _blank_keep_newlines(m: re.Match) -> str:
    """Replace a match with a space plus its own newlines.

    Preserving the newline count is what keeps every reported line number
    correct: a block comment must not shift the code that follows it.
    """
    return " " + "\n" * m.group(0).count("\n")


def strip_comments_and_strings(source: str) -> str:
    """Blank out comments and literals *without* changing any line number.

    Every substitution below is newline-preserving, so ``strip(source)`` has
    exactly as many lines as ``source`` and offsets map 1:1.  String and char
    literals are matched with newline excluded from the body so that a stray
    quote (``c == '"'``) can never make the regex swallow the rest of the file.
    """
    source = re.sub(r"/\*.*?\*/", _blank_keep_newlines, source, flags=re.DOTALL)
    source = re.sub(r"//[^\n]*", " ", source)
    source = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', source)
    source = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", source)
    return source


# ---------------------------------------------------------------------------
# Allocation APIs
# ---------------------------------------------------------------------------

ALLOC_APIS = frozenset({
    "malloc", "calloc", "realloc",
    "PyMem_Malloc", "PyMem_Calloc", "PyMem_Realloc",
    "PyMem_RawMalloc", "PyMem_RawCalloc", "PyMem_RawRealloc",
    "PyObject_Malloc", "PyObject_Calloc", "PyObject_Realloc",
    "PyMem_New", "PyMem_Resize",
    "PyObject_New", "PyObject_NewVar",
    "PyObject_GC_New", "PyObject_GC_NewVar",
})

# APIs that return PyObject* and can return NULL.
PYOBJ_APIS = frozenset({
    "PyObject_Call", "PyObject_CallObject", "PyObject_CallFunction",
    "PyObject_CallMethod", "PyObject_GetAttr", "PyObject_GetAttrString",
    "PyObject_GetItem", "PyObject_Str", "PyObject_Repr",
    "PyUnicode_FromString", "PyUnicode_FromFormat",
    "PyBytes_FromString", "PyBytes_FromStringAndSize",
    "PyLong_FromLong", "PyFloat_FromDouble",
    "PyList_New", "PyTuple_New", "PyDict_New",
    "Py_BuildValue",
    "PySequence_List", "PySequence_Tuple", "PySequence_GetItem",
    "PyNumber_Add", "PyNumber_Subtract", "PyNumber_Multiply",
    "PyIter_Next",
    "PyImport_ImportModule",
})

# APIs that write through a ``PyObject **`` out-parameter and set it to NULL on
# *every* failure path.  A Py_DECREF of that variable in the failure branch is
# therefore a guaranteed Py_DECREF(NULL).  Local wrappers (e.g. ``tuple_extend``
# in Objects/genericaliasobject.c) are discovered automatically; this is only
# the seed set.
NULLING_OUTPARAM_APIS = frozenset({
    "_PyTuple_Resize",
    "_PyBytes_Resize",
    "PyUnicode_Resize",
    "_PyUnicode_Resize",
})


# ---------------------------------------------------------------------------
# Function detection
# ---------------------------------------------------------------------------

# A CPython definition may spread its parameter list over several lines:
#     static PyObject *
#     subs_tvars(PyObject *obj, PyObject *params,
#                PyObject **argitems, Py_ssize_t nargs)
#     {
_MAX_SIG_LINES = 8

_C_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "do", "else", "sizeof", "return",
    "typedef", "struct", "union", "enum", "defined", "case", "default",
})


def _parse_signature(lines: list[str], brace_idx: int) -> dict | None:
    """Walk back from a column-0 ``{`` and recover the function signature."""
    collected: list[str] = []
    for j in range(brace_idx - 1, max(-1, brace_idx - 1 - _MAX_SIG_LINES), -1):
        line = lines[j]
        if ";" in line or "}" in line:
            return None
        collected.insert(0, line.strip())
        text = " ".join(collected).strip()
        if not text.endswith(")"):
            continue
        if text.count("(") == 0 or text.count("(") != text.count(")"):
            continue
        open_paren = text.index("(")
        head = text[:open_paren]
        if "=" in head:
            return None
        m = re.search(r"(\w+)\s*$", head)
        if m is None:
            return None
        name = m.group(1)
        if name in _C_KEYWORDS:
            return None
        sig_start = j
        ret_type = head[:m.start()].strip()
        # A bare return type on its own line above the name.
        if sig_start > 0 and re.match(
            r"^[\w\s\*]+$", lines[sig_start - 1].strip()
        ):
            sig_start -= 1
            ret_type = (lines[sig_start].strip() + " " + ret_type).strip()
        return {
            "name": name,
            "params": text[open_paren + 1:text.rindex(")")],
            "sig_start": sig_start,
            # Needed to tell a pointer-returning (hence NULL-capable) helper
            # from an int-returning one when widening the fallible-source set.
            "return_type": ret_type,
        }
    return None


def find_functions(source: str) -> list[dict]:
    """Locate top-level function definitions.

    ``source`` should already have been passed through
    :func:`strip_comments_and_strings` so that brace counting is not confused
    by braces inside comments or string literals.  Because that transform is
    newline-preserving, every line number recorded here is a real line number
    in the original file.

    Each entry records ``body_line``: the 1-based line of the first line of the
    body.  ``line = body_line + line_offset - 1`` is the only correct way to
    turn an in-body offset into a file line.
    """
    lines = source.split("\n")
    functions: list[dict] = []
    for i, line in enumerate(lines):
        if not line.startswith("{"):
            continue
        if i < 1:
            continue
        sig = _parse_signature(lines, i)
        if sig is None:
            continue

        depth = 1
        body_start = i + 1
        body_end = body_start
        for j in range(body_start, len(lines)):
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        body_end = j
                        break
            if depth == 0:
                break

        functions.append({
            "name": sig["name"],
            "params": sig["params"],
            "body": "\n".join(lines[body_start:body_end]),
            # 1-based line number of the first body line.
            "body_line": body_start + 1,
            "start_line": sig["sig_start"] + 1,
            "end_line": body_end + 1,
            "return_type": sig.get("return_type", ""),
        })
    return functions


# ---------------------------------------------------------------------------
# NULL safety analysis
# ---------------------------------------------------------------------------

_LVALUE = r"(?:[A-Za-z_]\w*\s*(?:->|\.)\s*)*[A-Za-z_]\w*"

_ALL_ALLOC_RE = re.compile(
    r"(?P<lval>" + _LVALUE + r")"
    r"\s*(?<![=!<>+\-*/%&|^])=(?!=)\s*"
    r"(?:\([^()]*\)\s*)?"
    r"(?P<api>" + "|".join(
        re.escape(api) for api in sorted(
            ALLOC_APIS | PYOBJ_APIS, key=len, reverse=True,
        )
    ) + r")\s*\("
)

_EXTRA_TARGET_RE = re.compile(
    r"(" + _LVALUE + r")\s*(?<![=!<>+\-*/%&|^])=(?!=)"
)


def _alloc_re_for(extra: Iterable[str]) -> re.Pattern:
    """``_ALL_ALLOC_RE`` widened with same-file NULL-returning helpers."""
    names = ALLOC_APIS | PYOBJ_APIS | set(extra)
    return re.compile(
        r"(?P<lval>" + _LVALUE + r")"
        r"\s*(?<![=!<>+\-*/%&|^])=(?!=)\s*"
        r"(?:\([^()]*\)\s*)?"
        r"(?P<api>"
        + "|".join(re.escape(api) for api in sorted(names, key=len, reverse=True))
        + r")\s*\("
    )


# A pointer-returning function is fallible if it can hand back NULL.  Needed
# because the closed API enum resolved only **49 of 760** assignment-from-call
# sites tree-wide (6.4%): `Objects/dictobject.c:4491` assigns
# `d = frozendict_new_untracked(&PyFrozenDict_Type);`, a same-file `static`
# helper, so there was no candidate to check in the first place.
_RETURNS_NULL_RE = re.compile(r"\breturn\s+(?:NULL|PyErr_NoMemory\s*\(\s*\))\s*;")


_FORWARD_RETURN_RE = re.compile(r"\breturn\s+([A-Za-z_]\w*)\s*\(")


def nullable_source_calls(functions: Iterable[dict]) -> set[str]:
    """Same-file functions that return a pointer and can return NULL.

    Includes thin forwarders: ``dict_new_untracked`` is
    ``return anydict_new_untracked(type);`` and has no literal ``return NULL``
    of its own, so a one-pass test misses half the producers of any value it
    feeds.
    """
    pointer_returning = [
        f for f in functions if "*" in (f.get("return_type", "") or "")
    ]
    out = {
        f["name"]
        for f in pointer_returning
        if _RETURNS_NULL_RE.search(f.get("body", ""))
    }
    for _ in range(4):
        grown = False
        for f in pointer_returning:
            if f["name"] in out:
                continue
            for m in _FORWARD_RETURN_RE.finditer(f.get("body", "")):
                if m.group(1) in out or m.group(1) in ALLOC_APIS | PYOBJ_APIS:
                    out.add(f["name"])
                    grown = True
                    break
        if not grown:
            break
    return out

# How many lines after the assignment to look for the NULL check / the use.
# A line window, not a byte window: a single CPython error branch routinely
# exceeds 300 bytes, which used to hide checks that were plainly present.
_WINDOW_LINES = 12

# Ways of establishing that the value cannot be NULL from here on, or that
# NULL is handled.  ``{var}`` is substituted with a regex for the full lvalue.
_NULL_CHECK_TEMPLATE = (
    # The cast is CPython's `if (list == (PyObject*)NULL)` spelling.
    r"{var}\s*==\s*(?:\([^()]*\)\s*)?NULL|"
    r"{var}\s*!=\s*(?:\([^()]*\)\s*)?NULL|"
    r"NULL\s*==\s*{var}|"
    r"NULL\s*!=\s*{var}|"
    r"!\s*{var}\s*[)&|]|"
    r"if\s*\(\s*{var}\s*\)|"
    r"while\s*\(\s*{var}\s*\)|"
    # `assert(x)` / `assert(x != NULL)` -- the operand must be the *whole*
    # assertion, not buried inside a call. `assert(MACRO(x))` is the opposite of
    # a check: see _ASSERT_CALL_DEREF below.
    r"assert\s*\(\s*!?\s*{var}\s*(?:\)|&&|\|\||[!=]=)"
)

# `assert(EXPR(var))` -- the operand sits inside a *call* under the assert, so
# the macro dereferences it (issue #28 rule 8).
#
# `Objects/dictobject.c:4493` is `assert(!_PyObject_GC_IS_TRACKED(d))` on a `d`
# that was never NULL-checked. This is worse than an ordinary unchecked deref:
# the dereference is undefined behaviour, and the UB licenses the optimizer to
# conclude `d` cannot be NULL and **delete the NULL check inside the inlined
# callee** -- disassembly of the release build shows no `test %rax,%rax` before
# the store. Recorded as CPY-0079.
#
# It is also invisible on a release build, where NDEBUG compiles the assert out
# entirely, so the reproduction and the miscompilation live on opposite builds.
_ASSERT_CALL_DEREF = (
    r"assert\s*\([^;]*?[A-Za-z_]\w*\s*\(\s*"
    r"(?:\(\s*[\w\s*]+\)\s*)?{var}\s*[,)]"
)

# Unsafe macros/uses that dereference their operand without a NULL check.
# Uses that are *not* here -- `return var;`, `Py_XDECREF(var)`,
# `Py_SETREF(x, var)`, `Py_CLEAR(var)`, passing var to a NULL-tolerant callee --
# are deliberately absent: NULL propagation is CPython's error-reporting
# contract, and those uses are the documented FP classes (9 of the 21
# false positives in the `Objects/` sample were "result returned directly").
# Deliberately *not* including ``*var``: in C that spelling is far more often a
# declaration (``PyObject *tup = ...``) or a ``sizeof *var`` inside the
# allocation's own argument list than a real dereference.
_DEREF_TEMPLATE = (
    r"{var}\s*->|"
    r"{var}\s*\[|"
    r"Py_TYPE\s*\(\s*{var}\s*\)|"
    r"Py_SIZE\s*\(\s*{var}\s*\)|"
    r"Py_REFCNT\s*\(\s*{var}\s*\)|"
    r"Py_SET_(?:TYPE|SIZE|REFCNT)\s*\(\s*{var}\b|"
    r"Py_INCREF\s*\(\s*{var}\s*\)|"
    r"Py_DECREF\s*\(\s*{var}\s*\)|"
    r"Py(?:Tuple|List|Bytes|ByteArray|Unicode|Dict|Set|Frame|Function|Cell|"
    r"Method|Slice|Weakref)_[A-Z][A-Za-z_]*\s*\(\s*{var}\s*[,)]|"
    r"_?_?Py\w*_(?:GC_)?IS_[A-Z_]+\s*\(\s*{var}\s*\)|" + _ASSERT_CALL_DEREF
)


def _lvalue_regex(lval: str) -> str:
    """Build a regex that matches ``lval`` tolerating whitespace in ``->``/``.``."""
    lval = lval.strip()
    prefix = ""
    if lval.startswith("*"):
        prefix = r"\*\s*"
        lval = lval[1:].strip()
    out: list[str] = []
    for tok in re.split(r"(->|\.)", lval):
        tok = tok.strip()
        if tok in ("->", "."):
            out.append(r"\s*" + re.escape(tok) + r"\s*")
        elif tok:
            out.append(re.escape(tok))
    return prefix + r"\b" + "".join(out)


def _norm_lvalue(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _statement_start(body: str, pos: int) -> int:
    """Index just after the nearest preceding statement/block boundary."""
    return max(
        body.rfind(";", 0, pos),
        body.rfind("{", 0, pos),
        body.rfind("}", 0, pos),
        -1,
    ) + 1


_CONTROL_KEYWORD_RE = re.compile(r"\b(?:if|while|for|switch)\s*$")

_ENCLOSING_SCAN_LIMIT = 600


def _matching_paren(text: str, open_idx: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``open_idx``, or -1."""
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _enclosing_paren(body: str, pos: int) -> int:
    """Index of the innermost unclosed ``(`` to the left of ``pos``."""
    depth = 0
    start = max(0, pos - _ENCLOSING_SCAN_LIMIT)
    for i in range(pos - 1, start - 1, -1):
        c = body[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                return i
            depth -= 1
        elif c in "{}" and depth == 0:
            return -1
    return -1


def _in_control_condition(body: str, match_start: int) -> bool:
    """True when the assignment sits inside an if/while/for controlling test.

    ``while ((pair = PyIter_Next(it)) != NULL)`` self-discharges: the NULL test
    *is* the loop condition.  Walking the enclosing parentheses outward (rather
    than scanning the line, or the statement) catches the three real spellings:
    a nested group (``if (n > 0 && ((iovs = PyMem_New(...)) == NULL || ...))``),
    a condition split over several lines, and the third clause of a ``for``
    header, where the nearest ``;`` is *inside* the parentheses.
    """
    pos = match_start
    for _ in range(6):
        paren = _enclosing_paren(body, pos)
        if paren == -1:
            return False
        if _CONTROL_KEYWORD_RE.search(body[max(0, paren - 24):paren]):
            return True
        pos = paren
    return False


def _window(body: str, start: int) -> str:
    """``_WINDOW_LINES`` lines of body text starting at ``start``."""
    end = start
    for _ in range(_WINDOW_LINES + 1):
        nxt = body.find("\n", end)
        if nxt == -1:
            return body[start:]
        end = nxt + 1
    return body[start:end]


def _assignment_targets(body: str, m: re.Match) -> list[str]:
    """All lvalues the call's result lands in, innermost first.

    Handles chained assignment: ``args = tuple_args = PySequence_Tuple(args);``
    assigns to both, and the NULL check may be written against either.  Also
    recognises a write *through* a pointer (``*result = PyObject_GetItem(...)``,
    the out-parameter idiom), whose NULL check is spelled ``if (*result)``.
    """
    lval = m.group("lval").strip()
    prefix = body[_statement_start(body, m.start()):m.start()]
    if prefix.strip() == "*":
        lval = "*" + lval
    targets = [lval]
    for extra in _EXTRA_TARGET_RE.finditer(prefix):
        cand = extra.group(1).strip()
        if _norm_lvalue(cand) not in {_norm_lvalue(t) for t in targets}:
            targets.append(cand)
    return targets


def _leaves_scope(text: str, end: int) -> bool:
    """True if reaching ``end`` requires closing a block open at offset 0."""
    depth = 0
    for ch in text[:end]:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return True
    return False


def _truncate_at_reassignment(window: str, targets: list[str]) -> str:
    """Cut ``window`` at the first re-assignment of any tracked lvalue.

    A re-assignment in a *sibling branch* does not end this value's life --
    exactly one of the two arms runs.  ``Objects/dictobject.c:4489`` assigns
    ``d`` in the ``if`` arm and ``:4492`` assigns it in the ``else``; treating
    the second as a re-binding cut the window three lines short and hid the
    ``assert`` that dereferences it.
    """
    cut = len(window)
    for target in targets:
        pat = re.compile(
            _lvalue_regex(target) + r"\s*(?<![=!<>+\-*/%&|^])=(?!=)"
        )
        for rea in pat.finditer(window):
            if _leaves_scope(window, rea.start()):
                continue
            cut = min(cut, rea.start())
            break
    return window[:cut]


_IF_KEYWORD_RE = re.compile(r"\bif\s*\(")


def _join_after_full_ifelse(
    body: str, assign_pos: int, targets: list[str]
) -> int | None:
    """Offset just past an ``if/else`` in which **every** arm assigns a target.

    ``_dominates`` requires the use to sit at the assignment's own brace depth,
    which is right for the sibling-arm false positive it was built for and
    wrong when both arms assign: the value is live at the join whichever arm
    ran.  Returns the join offset so the caller can re-test dominance from
    there.
    """
    # Innermost `if (` whose consequent block contains the assignment.
    best: tuple[int, int] | None = None
    for m in _IF_KEYWORD_RE.finditer(body, 0, assign_pos):
        close = _matching_paren(body, m.end() - 1)
        if close == -1:
            continue
        i = close + 1
        while i < len(body) and body[i] in " \t\n":
            i += 1
        if i >= len(body) or body[i] != "{":
            continue
        end = _block_end(body, i + 1)
        if not (i < assign_pos < end):
            continue
        if best is None or i > best[0]:
            best = (i, end)
    if best is None:
        return None

    _, cons_end = best
    j = cons_end + 1
    while j < len(body) and body[j] in " \t\n":
        j += 1
    if not body.startswith("else", j):
        return None
    j += 4
    while j < len(body) and body[j] in " \t\n":
        j += 1
    if j >= len(body) or body[j] != "{":
        return None
    alt_end = _block_end(body, j + 1)
    alt = body[j:alt_end]
    for target in targets:
        if re.search(
            _lvalue_regex(target) + r"\s*(?<![=!<>+\-*/%&|^])=(?!=)", alt
        ):
            return alt_end + 1
    return None


def _block_end(text: str, start: int) -> int:
    """Offset of the ``}`` closing the block whose body begins at ``start``."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            if depth == 0:
                return i
            depth -= 1
    return len(text) - 1


def _depth_profile(text: str) -> list[int]:
    """Brace depth *before* each character of ``text``."""
    depths = []
    depth = 0
    for ch in text:
        depths.append(depth)
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    depths.append(depth)
    return depths


_BRACELESS_KEYWORD_RE = re.compile(r"\b(?:if|else|while|for|switch|do)\b")
_ALT_PATH_RE = re.compile(r"\b(?:goto|return|case|default)\b")


def _dominates(body: str, depths: list[int], start: int, end: int) -> bool:
    """True when ``end`` is on the straight-line path from ``start``.

    Two conditions, both needed:

    1. Same brace depth, with no point in between dropping below it.  That
       rules out a use nested inside a conditional opened after the assignment
       and a use after the assignment's own block closed.
    2. No *brace-less* control flow at that depth in between.  CPython writes
       ``if (!PyTuple_CheckExact(fnargs)) fnargs = PySequence_Tuple(fnargs);
       else Py_INCREF(fnargs);`` -- both arms sit at the same brace depth, so
       depth alone would wrongly call the ``else`` arm dominated.  A structured
       ``if (...) { ... }`` whose braces balance out is fine and is allowed
       through.
    """
    base = depths[start]
    if depths[end] != base:
        return False
    if min(depths[start:end + 1]) < base:
        return False
    between = body[start:end]
    offset = start
    for m in _BRACELESS_KEYWORD_RE.finditer(between):
        pos = offset + m.start()
        if depths[pos] != base:
            continue
        if m.group(0) in ("else", "do"):
            return False
        paren = body.find("(", pos)
        if paren == -1 or paren > end:
            return False
        close = _matching_paren(body, paren)
        if close == -1:
            return False
        nxt = close + 1
        while nxt < len(body) and body[nxt] in " \t\n":
            nxt += 1
        if nxt >= len(body) or body[nxt] != "{":
            return False
    for m in _ALT_PATH_RE.finditer(between):
        if depths[offset + m.start()] == base:
            return False
    return True


def analyze_function_null_safety(
    func: dict, alloc_re: re.Pattern | None = None
) -> list[dict]:
    """Analyze NULL safety in a single function.

    ``func["body"]`` must already be comment/string-stripped; offsets in it map
    1:1 onto lines of the original file.  ``alloc_re`` lets the caller widen the
    fallible-source set with the file's own NULL-returning helpers; it defaults
    to the closed API enum.
    """
    body = func["body"]
    findings: list[dict] = []
    depths = _depth_profile(body)

    for m in (alloc_re or _ALL_ALLOC_RE).finditer(body):
        api = m.group("api")
        targets = _assignment_targets(body, m)
        primary = targets[0]
        line_offset = body[:m.start()].count("\n") + 1

        if _in_control_condition(body, m.start()):
            continue

        # The window starts after the call's own argument list: `sizeof *x`
        # inside `x = PyMem_Malloc(sizeof *x)` is not a use of the result.
        call_end = _matching_paren(body, m.end() - 1)
        window_start = m.end() if call_end == -1 else call_end + 1
        window = _window(body, window_start)
        # Stop at a re-assignment of the value: a NULL check past that point
        # belongs to the *next* value, not this one.
        window = _truncate_at_reassignment(window, targets)

        checked_at = None
        for target in targets:
            var_re = _lvalue_regex(target)
            check = re.search(_NULL_CHECK_TEMPLATE.format(var=var_re), window)
            if check is not None and (
                checked_at is None or check.start() < checked_at
            ):
                checked_at = check.start()

        deref = re.search(
            _DEREF_TEMPLATE.format(var=_lvalue_regex(primary)), window
        )

        if deref is None:
            # Never dereferenced in the window: NULL just propagates, which is
            # how CPython reports errors.  Not a finding.
            continue

        # The dereference must be genuinely reached from the assignment, not
        # merely textually later -- otherwise the sibling arm of an
        # `if (...) x = alloc(); else Py_INCREF(x);` reads as a dereference.
        deref_abs = window_start + deref.start()
        if not _dominates(body, depths, m.start(), deref_abs):
            # The value may still be live at the join of an if/else whose arms
            # *all* assign it, which is where CPython puts the shared use.
            join = _join_after_full_ifelse(body, m.start(), targets)
            if join is None or not _dominates(body, depths, join, deref_abs):
                continue

        deref_text = deref.group(0).strip()
        via_assert = deref_text.startswith("assert")

        if checked_at is None:
            findings.append({
                "type": "unchecked_alloc",
                "api_call": api,
                "variable": primary,
                "line_offset": line_offset,
                "deref_line_offset": body[:deref_abs].count("\n") + 1,
                "deref_is_assert": via_assert,
                "detail": (
                    f"Result of {api} assigned to '{primary}' is never checked "
                    f"for NULL and is dereferenced "
                    f"({deref_text})"
                    + (
                        ". The dereference is inside an assert(), which is NOT a "
                        "NULL check -- it is the opposite. On a debug build it "
                        "dereferences NULL; on a release build NDEBUG compiles "
                        "it out, so the crash and the real damage live on "
                        "different builds. The damage is that the dereference is "
                        "undefined behaviour, which licenses the optimizer to "
                        "conclude the pointer cannot be NULL and delete NULL "
                        "checks downstream, including inside an inlined callee "
                        "(CPY-0079: no `test %rax,%rax` before the store in the "
                        "release disassembly)."
                        if via_assert
                        else ""
                    )
                ),
                "confidence": "medium",
            })
            continue

        if deref.start() >= checked_at:
            continue

        findings.append({
            "type": "deref_before_check",
            "api_call": api,
            "variable": primary,
            "line_offset": body[:deref_abs].count("\n") + 1,
            "detail": (
                f"'{primary}' (from {api}) is dereferenced "
                f"({deref.group(0).strip()}) before the NULL check that "
                f"follows it"
            ),
            "confidence": "high",
        })

    return findings


# ---------------------------------------------------------------------------
# Rule 3: Py_DECREF of an out-parameter the callee already NULLed
# ---------------------------------------------------------------------------

_WRAPPER_DEF_RE = re.compile(
    r"^(?:[A-Za-z_][\w \t\*]*?[\s\*])?(\w+)\s*\(([^;{]*?)\)\s*\n\{",
    re.MULTILINE,
)

_OUTPARAM_RE = re.compile(r"PyObject\s*\*\s*\*\s*(\w+)")

# ``r < 0`` / ``r == -1`` / ``r != 0`` -- the CPython int failure sentinels.
_FAILURE_TEST = r"(?:<\s*0|==\s*-\s*1|!=\s*0)"

_DECREF_RE_TEMPLATE = r"\bPy_DECREF\s*\(\s*{var}\s*\)"


def discover_outparam_wrappers(
    sources: Iterable[tuple[str, str]],
) -> dict[str, str]:
    """Find local functions that forward a ``PyObject **`` to a NULLing API.

    ``Objects/genericaliasobject.c:tuple_extend`` is the motivating case: it
    takes ``PyObject **dst`` and hands it straight to ``_PyTuple_Resize``, so it
    inherits the "out-param is NULL on failure" contract.
    """
    seed_re = re.compile(
        r"\b(" + "|".join(re.escape(a) for a in sorted(NULLING_OUTPARAM_APIS))
        + r")\s*\(\s*(\w+)\s*[,)]"
    )
    wrappers: dict[str, str] = {}
    for rel, source in sources:
        for m in _WRAPPER_DEF_RE.finditer(source):
            name, params = m.group(1), m.group(2)
            outparams = _OUTPARAM_RE.findall(params)
            if not outparams:
                continue
            body = source[m.end():m.end() + 2500]
            for sm in seed_re.finditer(body):
                if sm.group(2) in outparams:
                    wrappers[name] = rel
                    break
    return wrappers


def _controlled_block(text: str, close_paren: int) -> tuple[int, int] | None:
    """Span of the statement/block controlled by a condition ending at ``)``."""
    i = close_paren + 1
    while i < len(text) and text[i] in " \t\n":
        i += 1
    if i >= len(text):
        return None
    if text[i] != "{":
        end = text.find(";", i)
        return (i, len(text) if end == -1 else end + 1)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return (i, j + 1)
    return None


def _failure_branch(
    body: str, call_start: int, call_open: int,
) -> tuple[int, int] | None:
    """Locate the failure branch guarding an out-param call.

    Form A -- the call is the ``if`` condition::

        if (_PyTuple_Resize(&keys, k) == -1) { ... }

    Form B -- the result is assigned, then tested::

        j = tuple_extend(&subargs, j, ...);
        if (j < 0) { ... }

    Only the *failure* arm counts.  ``if (!API(&x, ...))`` selects the success
    arm and is rejected, as is any comparison that is not one of CPython's
    int failure sentinels.
    """
    stmt_start = _statement_start(body, call_start)
    stmt_prefix = body[stmt_start:call_start]
    call_close = _matching_paren(body, call_open)
    if call_close == -1:
        return None

    # Form A: an unbalanced "if (" before the call on this statement.
    if stmt_prefix.count("(") > stmt_prefix.count(")"):
        if_m = None
        for cand in re.finditer(r"\b(?:if|while)\s*\(", stmt_prefix):
            if_m = cand
        if if_m is None:
            return None
        open_idx = stmt_start + if_m.end() - 1
        close_idx = _matching_paren(body, open_idx)
        if close_idx == -1 or call_close > close_idx:
            return None
        if body[open_idx + 1:call_start].rstrip().endswith("!"):
            return None
        tail = body[call_close + 1:close_idx]
        if tail.strip() and not re.search(_FAILURE_TEST, tail):
            return None
        return _controlled_block(body, close_idx)

    # Form B: "lhs = call(...);" followed by a test of lhs.
    m = re.match(r"\s*(\w+)\s*(?<![=!<>+\-*/%&|^])=(?!=)\s*$", stmt_prefix)
    if m is None:
        return None
    lhs = m.group(1)
    semi = body.find(";", call_close)
    if semi == -1:
        return None
    nxt = re.compile(
        r"\s*if\s*\(\s*" + re.escape(lhs) + r"\s*" + _FAILURE_TEST
    ).match(body, semi + 1)
    if nxt is None:
        return None
    open_idx = body.index("(", semi + 1)
    close_idx = _matching_paren(body, open_idx)
    if close_idx == -1:
        return None
    return _controlled_block(body, close_idx)


def analyze_function_outparams(func: dict, apis: Iterable[str]) -> list[dict]:
    """Find ``Py_DECREF`` of a variable the callee provably NULLed."""
    body = func["body"]
    findings: list[dict] = []
    api_list = sorted(apis, key=len, reverse=True)
    if not api_list:
        return findings
    call_re = re.compile(
        r"\b(" + "|".join(re.escape(a) for a in api_list)
        + r")\s*(\()\s*&\s*(" + _LVALUE + r")"
    )
    for m in call_re.finditer(body):
        api, var = m.group(1), m.group(3).strip()
        branch = _failure_branch(body, m.start(), m.start(2))
        if branch is None:
            continue
        start, end = branch
        block = body[start:end]
        dm = re.search(
            _DECREF_RE_TEMPLATE.format(var=_lvalue_regex(var)), block
        )
        if dm is None:
            continue
        abs_pos = start + dm.start()
        findings.append({
            "type": "decref_of_nulled_outparam",
            "api_call": api,
            "variable": var,
            "line_offset": body[:abs_pos].count("\n") + 1,
            "detail": (
                f"Py_DECREF({var}) runs in the failure branch of "
                f"{api}(&{var}, ...), which NULLs its out-parameter on every "
                f"failure path -- this is a guaranteed Py_DECREF(NULL). "
                f"The correct forms in-tree assert it instead "
                f"(Objects/structseq.c:523, "
                f"Objects/genericaliasobject.c:555)."
            ),
            "confidence": "high",
        })
    return findings


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(target: str, *, max_files: int = 0) -> dict:
    """Analyze NULL safety for the given target path."""
    target_path = Path(target).resolve()
    project_root = find_cpython_root(target_path)
    if project_root is None:
        project_root = target_path if target_path.is_dir() else target_path.parent
    # scan_root is the target itself: discover_c_files() yields just that file
    # for a file root. Using .parent here silently widened a single-file scan
    # to the entire directory.
    scan_root = target_path

    # Read and strip each file exactly once.
    sources: list[tuple[str, str]] = []
    for filepath in discover_c_files(scan_root, max_files=max_files):
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)
        sources.append((rel, strip_comments_and_strings(source)))

    wrappers = discover_outparam_wrappers(sources)
    outparam_apis = set(NULLING_OUTPARAM_APIS) | set(wrappers)

    all_findings: list[dict] = []
    functions_analyzed = 0

    assignment_sites = 0
    local_helpers: set[str] = set()

    for rel, source in sources:
        functions = find_functions(source)
        # Widen the fallible-source set with this file's own NULL-returning
        # helpers. The closed API enum resolved 49 of 760 assignment-from-call
        # sites tree-wide (6.4%), which is why `d = frozendict_new_untracked(...)`
        # at Objects/dictobject.c:4491 had no candidate to check at all.
        extra = nullable_source_calls(functions)
        local_helpers |= extra
        alloc_re = _alloc_re_for(extra)
        for func in functions:
            functions_analyzed += 1
            assignment_sites += len(_EXTRA_TARGET_RE.findall(func["body"]))
            func_findings = analyze_function_null_safety(func, alloc_re)
            func_findings += analyze_function_outparams(func, outparam_apis)
            for finding in func_findings:
                finding["file"] = rel
                finding["function"] = func["name"]
                finding["line"] = (
                    func["body_line"] + finding.pop("line_offset") - 1
                )
                if "deref_line_offset" in finding:
                    finding["deref_line"] = (
                        func["body_line"] + finding.pop("deref_line_offset") - 1
                    )
                all_findings.append(finding)

    by_type: dict[str, int] = {}
    for f in all_findings:
        by_type[f["type"]] = by_type.get(f["type"], 0) + 1

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "files_analyzed": len(sources),
        "functions_analyzed": functions_analyzed,
        "outparam_wrappers": sorted(wrappers),
        # Denominators, so a zero is readable. `assignment_sites` is every
        # `lvalue = ...` in scope; `fallible_sources_resolved` is how many
        # distinct callees the rule recognised as able to return NULL, of which
        # `local_nullable_helpers` were discovered from this corpus rather than
        # tabulated. A zero finding count against a large `assignment_sites` and
        # a tiny resolved set is a coverage statement, not a safety one.
        "assignment_sites": assignment_sites,
        "fallible_sources_resolved": len(ALLOC_APIS | PYOBJ_APIS | local_helpers),
        "local_nullable_helpers": len(local_helpers),
        "findings": all_findings,
        "summary": {
            "unchecked_allocations": by_type.get("unchecked_alloc", 0),
            "deref_before_check": by_type.get("deref_before_check", 0),
            "decref_of_nulled_outparam": by_type.get(
                "decref_of_nulled_outparam", 0
            ),
            # Standing note: this rule has a denominator of literally zero on
            # CPython -- no call sites and no discovered wrappers -- so its zero
            # is structural. Do not read it as a clean result.
            "decref_of_nulled_outparam_call_sites": len(wrappers)
            + len(NULLING_OUTPARAM_APIS),
            "assert_only_derefs": len(
                [f for f in all_findings if f.get("deref_is_assert")]
            ),
            "total_findings": len(all_findings),
            "high_confidence": len(
                [f for f in all_findings if f.get("confidence") == "high"]
            ),
        },
    }


def main() -> None:
    max_files = 0
    positional: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--max-files" and i + 1 < len(argv):
            max_files = int(argv[i + 1])
            i += 2
        elif argv[i].startswith("--"):
            i += 1
        else:
            positional.append(argv[i])
            i += 1
    target = positional[0] if positional else "."
    result = analyze(target, max_files=max_files)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
