"""Tests for scan_null_checks.py.

The headline test class is :class:`TestLineNumberAccuracy`.  Every reported
``file:line`` must land on the construct the finding describes -- that is the
mechanical property the scanner used to violate in 111 of 113 ``Objects/``
findings.  It is checked here the same way ``check_lines.py`` checked it during
the review: rebuild a regex from the finding's own fields and require it to
match the reported source line.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_null_checks")


def line_pattern(finding: dict) -> re.Pattern:
    """Regex the reported source line must match, per finding type."""
    var = re.escape(finding["variable"].lstrip("*").strip())
    api = re.escape(finding["api_call"])
    if finding["type"] == "unchecked_alloc":
        return re.compile(
            r"\*?\s*" + var + r"\s*=\s*(?:\([^)]*\)\s*)?" + api + r"\s*\("
        )
    if finding["type"] == "deref_before_check":
        return re.compile(var)
    if finding["type"] == "decref_of_nulled_outparam":
        return re.compile(r"Py_DECREF\s*\(\s*" + var + r"\s*\)")
    raise AssertionError(f"unknown finding type {finding['type']!r}")


def assert_lines_exact(testcase: unittest.TestCase, root: Path,
                       result: dict) -> None:
    """Every finding's reported line must contain what the finding describes."""
    for finding in result["findings"]:
        src = (root / finding["file"]).read_text().splitlines()
        line = finding["line"]
        testcase.assertTrue(
            1 <= line <= len(src),
            f"{finding['file']}:{line} out of range (file has {len(src)})",
        )
        testcase.assertRegex(
            src[line - 1],
            line_pattern(finding),
            f"{finding['file']}:{line} does not contain the reported "
            f"{finding['type']} for {finding['variable']!r}",
        )


# ---------------------------------------------------------------------------
# Line-number accuracy -- the top acceptance criterion
# ---------------------------------------------------------------------------

# Exercises every construct that used to shift line numbers: a leading licence
# block comment, the `static PyObject *\nname(...)\n{` CPython style, a
# multi-line signature, a block comment *inside* a function body, a `//`
# comment, and a string literal containing braces.
_LINE_FIXTURE = """\
/* Multi-line
   licence header
   spanning four
   lines. */
#include "Python.h"

static PyObject *
first_function(PyObject *self)
{
    /* A block comment
       inside the body,
       three lines long. */
    PyObject *a = PyList_New(0);
    Py_INCREF(a);
    return a;
}

static PyObject *
second_function(PyObject *self, PyObject *args,
                PyObject *kwargs)
{
    // A line comment mentioning { and } braces.
    const char *s = "a string with { and } in it";
    PyObject *b = PyDict_New();
    Py_DECREF(b);
    return NULL;
}
"""

# 1-based line numbers of the two assignments above.
_LINE_FIXTURE_A = 13
_LINE_FIXTURE_B = 24


class TestLineNumberAccuracy(unittest.TestCase):
    """Line numbers must be exact -- no drift from comments or signatures."""

    def test_strip_preserves_line_count(self):
        stripped = mod.strip_comments_and_strings(_LINE_FIXTURE)
        self.assertEqual(
            stripped.count("\n"), _LINE_FIXTURE.count("\n"),
            "stripping comments/strings must not change the line count",
        )

    def test_reported_lines_are_exact(self):
        with TempProject({"Objects/lines.c": _LINE_FIXTURE}) as root:
            result = mod.analyze(str(root))
            findings = [
                f for f in result["findings"] if f["file"].endswith("lines.c")
            ]
            self.assertEqual(len(findings), 2, findings)
            by_var = {f["variable"]: f["line"] for f in findings}
            self.assertEqual(by_var["a"], _LINE_FIXTURE_A)
            self.assertEqual(by_var["b"], _LINE_FIXTURE_B)
            assert_lines_exact(self, root, result)

    def test_multiline_signature_is_analyzed(self):
        """`name(a,\\n b)` over two lines is a function, not a skipped blob."""
        with TempProject({"Objects/lines.c": _LINE_FIXTURE}) as root:
            result = mod.analyze(str(root))
            names = {
                f["function"] for f in result["findings"]
                if f["file"].endswith("lines.c")
            }
            self.assertIn("second_function", names)


# ---------------------------------------------------------------------------
# Rule 1: unchecked_alloc
# ---------------------------------------------------------------------------

class TestUncheckedAlloc(unittest.TestCase):

    def _findings(self, code: str, root_out: list | None = None):
        with TempProject({"Objects/test.c": code}) as root:
            result = mod.analyze(str(root))
            assert_lines_exact(self, root, result)
            return [
                f for f in result["findings"]
                if f["type"] == "unchecked_alloc"
                and f["file"].endswith("test.c")
            ]

    def test_true_positive_unchecked_malloc_dereferenced(self):
        code = (
            "static int\n"
            "bad_alloc(int n)\n"
            "{\n"
            "    char *buf = PyMem_Malloc(n);\n"
            "    buf[0] = 0;\n"
            "    PyMem_Free(buf);\n"
            "    return 0;\n"
            "}\n"
        )
        findings = self._findings(code)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0]["variable"], "buf")
        self.assertEqual(findings[0]["line"], 4)

    def test_true_negative_checked_malloc(self):
        code = (
            "static int\n"
            "good_alloc(int n)\n"
            "{\n"
            "    char *buf = PyMem_Malloc(n);\n"
            "    if (buf == NULL) {\n"
            "        return -1;\n"
            "    }\n"
            "    buf[0] = 0;\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_sizeof_star_var_in_own_arguments_is_not_a_deref(self):
        """`x = PyMem_Malloc(sizeof *x)` -- the CPython allocation idiom.

        The `*x` lives in the call's own argument list, so it cannot be a use
        of the result.  This alone accounted for 10 of the 37 `Modules/` FPs.
        """
        code = (
            "static int\n"
            "alloc_sizeof(void)\n"
            "{\n"
            "    struct unpacker *x = PyMem_Malloc(sizeof *x);\n"
            "    if (x == NULL) {\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_result_returned_directly(self):
        """NULL propagation *is* the error handling -- the `*_repr` shape."""
        code = (
            "static PyObject *\n"
            "thing_repr(PyObject *self)\n"
            "{\n"
            '    PyObject *res = PyUnicode_FromFormat("<thing %p>", self);\n'
            "    return res;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_py_setref(self):
        """Py_SETREF/Py_XSETREF/Py_CLEAR handle NULL themselves."""
        code = (
            "static PyObject *\n"
            "setref_user(PyObject *item, PyObject *self)\n"
            "{\n"
            "    PyObject *tmp = PyObject_Str(self);\n"
            "    Py_SETREF(item, tmp);\n"
            "    if (item == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return item;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_null_check_is_the_loop_condition(self):
        code = (
            "static int\n"
            "loop_check(PyObject *iterator)\n"
            "{\n"
            "    PyObject *pair;\n"
            "    while ((pair = PyIter_Next(iterator)) != NULL) {\n"
            "        PyObject *k = PyTuple_GET_ITEM(pair, 0);\n"
            "        Py_DECREF(pair);\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_null_check_is_the_for_clause(self):
        """`for (k = PyIter_Next(i); k; k = PyIter_Next(i))` -- dict_merge."""
        code = (
            "static int\n"
            "for_check(PyObject *iter)\n"
            "{\n"
            "    PyObject *key;\n"
            "    for (key = PyIter_Next(iter); key; key = PyIter_Next(iter)) {\n"
            "        Py_DECREF(key);\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_check_on_struct_field_lvalue(self):
        code = (
            "static int\n"
            "field_check(unionobject *ub)\n"
            "{\n"
            "    ub->args = PyList_New(0);\n"
            "    if (ub->args == NULL) {\n"
            "        return -1;\n"
            "    }\n"
            "    Py_DECREF(ub->args);\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_check_on_aliased_lvalue(self):
        """`args = tuple_args = PySequence_Tuple(args);` checks the outer name."""
        code = (
            "static PyObject *\n"
            "alias_check(PyObject *args)\n"
            "{\n"
            "    PyObject *tuple_args;\n"
            "    args = tuple_args = PySequence_Tuple(args);\n"
            "    if (args == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    Py_DECREF(tuple_args);\n"
            "    return args;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_cast_before_null(self):
        """CPython writes `if (list == (PyObject*)NULL)` in older modules."""
        code = (
            "static PyObject *\n"
            "cast_check(void)\n"
            "{\n"
            "    PyObject *list = PyList_New(4);\n"
            "    if (list == (PyObject*)NULL)\n"
            "        return (PyObject*)NULL;\n"
            "    Py_DECREF(list);\n"
            "    return NULL;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_out_parameter_fill_checked_through_the_pointer(self):
        """`*result = ...; if (*result)` -- PyMapping_GetOptionalItem."""
        code = (
            "int\n"
            "get_optional(PyObject *obj, PyObject *key, PyObject **result)\n"
            "{\n"
            "    *result = PyObject_GetItem(obj, key);\n"
            "    if (*result) {\n"
            "        return 1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_fp_deref_is_in_the_sibling_else_arm(self):
        """Brace-less `if (...) x = alloc(); else Py_INCREF(x);`."""
        code = (
            "static PyObject *\n"
            "else_arm(PyObject *fnargs)\n"
            "{\n"
            "    if (!PyTuple_CheckExact(fnargs))\n"
            "        fnargs = PySequence_Tuple(fnargs);\n"
            "    else\n"
            "        Py_INCREF(fnargs);\n"
            "    if (fnargs == NULL)\n"
            "        return NULL;\n"
            "    return fnargs;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])


# ---------------------------------------------------------------------------
# Rule 2: deref_before_check
# ---------------------------------------------------------------------------

class TestDerefBeforeCheck(unittest.TestCase):
    """The rule that used to build state and append nothing.

    Measured tree-wide on CPython main @ 3.16.0a0: 2 candidates, both correctly
    suppressed by the dominator gate, 0 reported.  These tests pin the
    behaviour in both directions so the zero stays a *measurement* rather than
    reverting to dead code.
    """

    def _findings(self, code: str):
        with TempProject({"Objects/test.c": code}) as root:
            result = mod.analyze(str(root))
            assert_lines_exact(self, root, result)
            return [
                f for f in result["findings"]
                if f["type"] == "deref_before_check"
                and f["file"].endswith("test.c")
            ]

    def test_true_positive_deref_then_check(self):
        code = (
            "static PyObject *\n"
            "late_check(PyObject *self)\n"
            "{\n"
            "    PyObject *obj = PyList_New(0);\n"
            "    Py_ssize_t n = Py_SIZE(obj);\n"
            "    if (obj == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return obj;\n"
            "}\n"
        )
        findings = self._findings(code)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0]["variable"], "obj")
        self.assertEqual(findings[0]["confidence"], "high")
        # Reported at the dereference, not at the assignment.
        self.assertEqual(findings[0]["line"], 5)

    def test_true_negative_check_comes_first(self):
        code = (
            "static PyObject *\n"
            "early_check(PyObject *self)\n"
            "{\n"
            "    PyObject *obj = PyList_New(0);\n"
            "    if (obj == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    Py_ssize_t n = Py_SIZE(obj);\n"
            "    return obj;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_deref_nested_in_a_conditional_is_not_dominated(self):
        """Textually earlier is not the same as actually reached first."""
        code = (
            "static PyObject *\n"
            "guarded_deref(PyObject *self, int flag)\n"
            "{\n"
            "    PyObject *obj = PyList_New(0);\n"
            "    if (flag && obj != NULL) {\n"
            "        Py_ssize_t n = Py_SIZE(obj);\n"
            "    }\n"
            "    if (obj == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return obj;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_balanced_braced_block_in_between_still_dominates(self):
        """A fully braced `if` between the two returns to the same depth."""
        code = (
            "static PyObject *\n"
            "braced_between(PyObject *self, int flag)\n"
            "{\n"
            "    PyObject *obj = PyList_New(0);\n"
            "    if (flag) {\n"
            "        PySys_WriteStdout(\"x\");\n"
            "    }\n"
            "    Py_ssize_t n = Py_SIZE(obj);\n"
            "    if (obj == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return obj;\n"
            "}\n"
        )
        findings = self._findings(code)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0]["line"], 8)


# ---------------------------------------------------------------------------
# Rule 3: decref_of_nulled_outparam
# ---------------------------------------------------------------------------

_TUPLE_EXTEND_WRAPPER = (
    "static Py_ssize_t\n"
    "tuple_extend(PyObject **dst, Py_ssize_t dstindex,\n"
    "             PyObject **src, Py_ssize_t count)\n"
    "{\n"
    "    if (_PyTuple_Resize(dst, PyTuple_GET_SIZE(*dst) + count - 1) != 0) {\n"
    "        return -1;\n"
    "    }\n"
    "    return dstindex + count;\n"
    "}\n"
)


class TestDecrefOfNulledOutparam(unittest.TestCase):
    """The shape behind Objects/genericaliasobject.c:302."""

    def _findings(self, code: str):
        with TempProject({"Objects/test.c": code}) as root:
            result = mod.analyze(str(root))
            assert_lines_exact(self, root, result)
            return [
                f for f in result["findings"]
                if f["type"] == "decref_of_nulled_outparam"
                and f["file"].endswith("test.c")
            ]

    def test_true_positive_direct_call(self):
        code = (
            "static int\n"
            "resize_then_decref(PyObject *keys, Py_ssize_t k)\n"
            "{\n"
            "    if (_PyTuple_Resize(&keys, k) == -1) {\n"
            "        Py_DECREF(keys);\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        findings = self._findings(code)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0]["variable"], "keys")
        self.assertEqual(findings[0]["api_call"], "_PyTuple_Resize")
        self.assertEqual(findings[0]["confidence"], "high")
        self.assertEqual(findings[0]["line"], 5)

    def test_true_negative_guarded_by_assert(self):
        """Objects/structseq.c:523 -- the in-tree correct form."""
        code = (
            "static int\n"
            "resize_guarded(PyObject *keys, Py_ssize_t k)\n"
            "{\n"
            "    if (_PyTuple_Resize(&keys, k) == -1) {\n"
            "        assert(keys == NULL);\n"
            "        return -1;\n"
            "    }\n"
            "    Py_DECREF(keys);\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_true_negative_decref_on_a_later_sibling_branch(self):
        """Modules/_pickle.c:1417 -- the DECREF is in a *different* branch."""
        code = (
            "static int\n"
            "resize_sibling(PyObject *data, Py_ssize_t n)\n"
            "{\n"
            "    if (_PyBytes_Resize(&data, n) < 0) {\n"
            "        return -1;\n"
            "    }\n"
            "    if (read_into(data) < 0) {\n"
            "        Py_DECREF(data);\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_py_xdecref_is_not_flagged(self):
        """Py_XDECREF(NULL) is a documented no-op -- dead code, not a crash."""
        code = (
            "static int\n"
            "resize_xdecref(PyObject *keys, Py_ssize_t k)\n"
            "{\n"
            "    if (_PyTuple_Resize(&keys, k) == -1) {\n"
            "        Py_XDECREF(keys);\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings(code), [])

    def test_local_wrapper_is_discovered(self):
        """genericaliasobject.c's `tuple_extend` inherits the NULLing contract."""
        code = (
            _TUPLE_EXTEND_WRAPPER
            + "\n"
            "static PyObject *\n"
            "subs_tvars(PyObject *subargs, PyObject *subparams, Py_ssize_t j)\n"
            "{\n"
            "    j = tuple_extend(&subargs, j, &subparams, 2);\n"
            "    if (j < 0) {\n"
            "        Py_DECREF(subparams);\n"
            "        Py_DECREF(subargs);\n"
            "        return NULL;\n"
            "    }\n"
            "    return subargs;\n"
            "}\n"
        )
        findings = self._findings(code)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0]["variable"], "subargs")
        self.assertEqual(findings[0]["api_call"], "tuple_extend")
        self.assertEqual(findings[0]["line"], 17)

    def test_wrapper_discovery_is_reported(self):
        with TempProject({"Objects/test.c": _TUPLE_EXTEND_WRAPPER}) as root:
            result = mod.analyze(str(root))
            self.assertIn("tuple_extend", result["outparam_wrappers"])


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

class TestAnalyze(unittest.TestCase):

    def test_summary_fields(self):
        with TempProject({
            "Objects/test.c": (
                "static int\n"
                "test(void)\n"
                "{\n"
                "    return 0;\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            summary = result["summary"]
            for key in ("unchecked_allocations", "deref_before_check",
                        "decref_of_nulled_outparam", "total_findings",
                        "high_confidence"):
                self.assertIn(key, summary)

    def test_max_files_is_honoured(self):
        files = {f"Objects/f{i}.c": "int x;\n" for i in range(5)}
        with TempProject(files) as root:
            result = mod.analyze(str(root), max_files=2)
            self.assertEqual(result["files_analyzed"], 2)


# ---------------------------------------------------------------------------
# Optional: mechanical line check against a real CPython tree
# ---------------------------------------------------------------------------

_CPYTHON_SRC = os.environ.get("CPYTHON_SRC")


@unittest.skipUnless(
    _CPYTHON_SRC and (Path(_CPYTHON_SRC) / "Objects" / "object.c").is_file(),
    "set CPYTHON_SRC to a CPython checkout to run the real-tree line check",
)
class TestRealTreeLineAccuracy(unittest.TestCase):
    """The `check_lines.py` harness, as a test: 100% or it fails."""

    def test_objects_line_numbers_are_all_exact(self):
        root = Path(_CPYTHON_SRC)
        result = mod.analyze(str(root / "Objects"))
        self.assertGreater(len(result["findings"]), 0)
        assert_lines_exact(self, Path(result["project_root"]), result)

    def test_finds_the_genericalias_decref_of_nulled_outparam(self):
        root = Path(_CPYTHON_SRC)
        result = mod.analyze(str(root / "Objects"))
        hits = [
            f for f in result["findings"]
            if f["type"] == "decref_of_nulled_outparam"
        ]
        self.assertEqual(
            [(f["file"], f["line"]) for f in hits],
            [("Objects/genericaliasobject.c", 302)],
        )


if __name__ == "__main__":
    unittest.main()
