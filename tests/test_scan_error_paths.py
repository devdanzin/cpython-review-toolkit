"""Tests for scan_error_paths.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_error_paths")


def _types(result, kind):
    return [f for f in result["findings"] if f["type"] == kind]


class TestStripComments(unittest.TestCase):
    """Comment stripping must not destroy line structure (TK-15)."""

    def test_block_comment_newlines_preserved(self):
        src = "a\n/* one\ntwo\nthree */\nb\n"
        out = mod.strip_comments_and_strings(src)
        self.assertEqual(src.count("\n"), out.count("\n"))
        self.assertEqual(out.split("\n")[4], "b")

    def test_line_comment_preserved(self):
        src = "a // note\nb\n"
        out = mod.strip_comments_and_strings(src)
        self.assertEqual(src.count("\n"), out.count("\n"))


class TestFindFunctions(unittest.TestCase):
    """Regression tests for the return-type off-by-one (TK-9)."""

    def test_two_line_signature_return_type(self):
        c_code = (
            "\n"
            "PyObject *\n"
            "_PyLazyImport_New(PyObject *name)\n"
            "{\n"
            "    return NULL;\n"
            "}\n"
        )
        funcs = mod.find_functions(c_code)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["return_type"], "PyObject *")

    def test_static_two_line_signature_return_type(self):
        c_code = (
            "}\n"
            "\n"
            "static bool\n"
            "helper(PyObject *arg)\n"
            "{\n"
            "    return true;\n"
            "}\n"
        )
        funcs = mod.find_functions(c_code)
        self.assertEqual(funcs[0]["return_type"], "static bool")

    def test_comment_above_is_not_a_return_type(self):
        """The line above the signature must never be read as the type."""
        c_code = (
            "/* a comment that is not a return type */\n"
            "static PyObject *\n"
            "thing(void)\n"
            "{\n"
            "    return NULL;\n"
            "}\n"
        )
        funcs = mod.find_functions(c_code)
        self.assertEqual(funcs[0]["return_type"], "static PyObject *")

    def test_body_start_line_is_first_body_line(self):
        c_code = (
            "static int\n"
            "f(void)\n"
            "{\n"
            "    return 0;\n"
            "}\n"
        )
        funcs = mod.find_functions(c_code)
        # `{` is line 3, so the first body line is line 4.
        self.assertEqual(funcs[0]["body_start_line"], 4)


class TestLineAccuracy(unittest.TestCase):
    """Reported lines must land on the construct (TK-15)."""

    def test_line_survives_block_comment_in_body(self):
        c_code = (
            "static int\n"                                     # 1
            "f(PyObject *o)\n"                                 # 2
            "{\n"                                              # 3
            "    /* a\n"                                       # 4
            "       multi-line\n"                              # 5
            "       comment */\n"                              # 6
            "    int rc = PyObject_Hash(o);\n"                 # 7
            "    if (rc == -1) {\n"                            # 8
            "        PyErr_Clear();\n"                         # 9
            "        return 0;\n"                              # 10
            "    }\n"
            "    return rc;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            clears = _types(result, "unconditional_pyerr_clear")
            self.assertEqual(len(clears), 1)
            self.assertEqual(clears[0]["line"], 9)


class TestAllocNullNoMemError(unittest.TestCase):
    """Rule alloc_null_no_memerror (replaces return_null_no_exception)."""

    def test_true_positive_raw_allocator(self):
        c_code = (
            "static PyObject *\n"
            "leaky(Py_ssize_t n)\n"
            "{\n"
            "    char *buf = PyMem_Malloc(n);\n"
            "    if (buf == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return PyBytes_FromStringAndSize(buf, n);\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            found = _types(result, "alloc_null_no_memerror")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["api_call"], "PyMem_Malloc")
            self.assertEqual(found[0]["line"], 4)
            self.assertEqual(found[0]["guard_line"], 5)

    def test_true_negative_pyerr_nomemory_present(self):
        c_code = (
            "static int\n"
            "clean(Py_ssize_t n)\n"
            "{\n"
            "    char *buf = PyMem_Malloc(n);\n"
            "    if (buf == NULL) {\n"
            "        PyErr_NoMemory();\n"
            "        return -1;\n"
            "    }\n"
            "    PyMem_Free(buf);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "alloc_null_no_memerror"), [])

    def test_exception_setting_allocator_is_exempt(self):
        """PyObject_GC_New raises MemoryError itself — never flagged."""
        c_code = (
            "static PyObject *\n"
            "make(void)\n"
            "{\n"
            "    thing *t = PyObject_GC_New(thing, &Thing_Type);\n"
            "    if (t == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return (PyObject *)t;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "alloc_null_no_memerror"), [])

    def test_pymem_new_is_a_raw_allocator(self):
        """PyMem_New is a macro over PyMem_Malloc, so it does *not* raise."""
        self.assertIn("PyMem_New", mod.RAW_ALLOCATORS)
        self.assertNotIn("PyMem_New", mod.EXCEPTION_SETTING_ALLOCATORS)
        c_code = (
            "static int *\n"
            "marks(int len)\n"
            "{\n"
            "    int *starts = PyMem_New(int, len);\n"
            "    if (starts == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return starts;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(len(_types(result, "alloc_null_no_memerror")), 1)

    def test_caller_discharges_the_obligation(self):
        """A thin helper whose in-file callers all raise is not a bug."""
        c_code = (
            "static void *\n"
            "alloc_array(size_t n)\n"
            "{\n"
            "    void *array = PyMem_Malloc(n);\n"
            "    if (array == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return array;\n"
            "}\n"
            "\n"
            "static int\n"
            "user(size_t n)\n"
            "{\n"
            "    void *a = alloc_array(n);\n"
            "    if (a == NULL) {\n"
            "        PyErr_NoMemory();\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "alloc_null_no_memerror"), [])


class TestUnconditionalPyErrClear(unittest.TestCase):
    """Rule unconditional_pyerr_clear (Objects/unionobject.c:172)."""

    def test_true_positive_unguarded_clear(self):
        c_code = (
            "static bool\n"
            "add_single(builder *ub, PyObject *arg)\n"
            "{\n"
            "    Py_hash_t hash = PyObject_Hash(arg);\n"
            "    if (hash == -1) {\n"
            "        PyErr_Clear();\n"
            "        return true;\n"
            "    }\n"
            "    return false;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            found = _types(result, "unconditional_pyerr_clear")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["line"], 6)

    def test_true_negative_narrowed_clear(self):
        c_code = (
            "static bool\n"
            "add_single(builder *ub, PyObject *arg)\n"
            "{\n"
            "    Py_hash_t hash = PyObject_Hash(arg);\n"
            "    if (hash == -1) {\n"
            "        if (!PyErr_ExceptionMatches(PyExc_TypeError)) {\n"
            "            return false;\n"
            "        }\n"
            "        PyErr_Clear();\n"
            "        return true;\n"
            "    }\n"
            "    return false;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "unconditional_pyerr_clear"), [])

    def test_destructor_family_is_left_to_scan_pyerr_clear(self):
        c_code = (
            "static void\n"
            "thing_dealloc(PyObject *self)\n"
            "{\n"
            "    PyObject *cb = PyObject_CallNoArgs(hook);\n"
            "    if (cb == NULL) {\n"
            "        PyErr_Clear();\n"
            "    }\n"
            "    Py_XDECREF(cb);\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "unconditional_pyerr_clear"), [])

    def test_underscore_spelling_is_matched(self):
        """CPython's internal `_PyErr_Clear(tstate)` spelling."""
        c_code = (
            "static int\n"
            "f(PyThreadState *tstate, PyObject *o)\n"
            "{\n"
            "    PyObject *r = PyObject_Str(o);\n"
            "    if (r == NULL) {\n"
            "        _PyErr_Clear(tstate);\n"
            "        return 0;\n"
            "    }\n"
            "    Py_DECREF(r);\n"
            "    return 1;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(len(_types(result, "unconditional_pyerr_clear")), 1)

    def test_clear_outside_a_failure_branch_is_ignored(self):
        c_code = (
            "static int\n"
            "f(void)\n"
            "{\n"
            "    PyErr_Clear();\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "unconditional_pyerr_clear"), [])

    def test_pyerr_occurred_is_the_failure_test_not_a_narrowing(self):
        """Modules/itertoolsmodule.c islice_new — the archetype spelling.

        `if (x == -1 && PyErr_Occurred()) PyErr_Clear();` was silently
        suppressed because PyErr_Occurred sat in the guard alternation. It
        answers "is *something* pending", never "is it the exception I
        expected".
        """
        c_code = (
            "static PyObject *\n"
            "islice_new(PyObject *a3)\n"
            "{\n"
            "    Py_ssize_t step = PyNumber_AsSsize_t(a3, PyExc_OverflowError);\n"
            "    if (step == -1 && PyErr_Occurred())\n"
            "        PyErr_Clear();\n"
            '    PyErr_SetString(PyExc_ValueError, "Step must be positive.");\n'
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Modules/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            found = _types(result, "unconditional_pyerr_clear")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["line"], 6)

    def test_checked_long_conversion_is_suppressed(self):
        """The guarded twin: Modules/itertoolsmodule.c count_repr.

        PyLong_AsLong short-circuits on PyLong_Check, so not even an int
        subclass runs user code — only OverflowError can be pending, and that
        is exactly the case the branch handles.
        """
        c_code = (
            "static PyObject *\n"
            "count_repr(countobject *lz)\n"
            "{\n"
            "    if (PyLong_Check(lz->long_step)) {\n"
            "        long step = PyLong_AsLong(lz->long_step);\n"
            "        if (step == -1 && PyErr_Occurred()) {\n"
            "            PyErr_Clear();\n"
            "            return NULL;\n"
            "        }\n"
            "    }\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Modules/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "unconditional_pyerr_clear"), [])


class TestPyLongSentinelNoErrCheck(unittest.TestCase):
    """Rule pylong_sentinel_no_errcheck (Modules/_zoneinfo.c:2314)."""

    def test_true_positive_bare_sentinel_comparison(self):
        c_code = (
            "static int\n"
            "get_local_timestamp(PyObject *dt, int64_t *out)\n"
            "{\n"
            '    PyObject *num = PyObject_GetAttrString(dt, "hour");\n'
            "    long hour = PyLong_AsLong(num);\n"
            "    Py_DECREF(num);\n"
            "    if (hour == -1) {\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            found = _types(result, "pylong_sentinel_no_errcheck")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["line"], 7)
            self.assertEqual(found[0]["assign_line"], 5)
            self.assertEqual(found[0]["variable"], "hour")
            self.assertEqual(found[0]["confidence"], "high")

    def test_true_negative_guarded_twin(self):
        """Modules/_zoneinfo.c:2304, ten lines above the bug, gets it right."""
        c_code = (
            "static int\n"
            "get_local_timestamp(PyObject *dt, int64_t *out)\n"
            "{\n"
            '    PyObject *num = PyObject_CallMethod(dt, "toordinal", NULL);\n'
            "    long ord = PyLong_AsLong(num);\n"
            "    Py_DECREF(num);\n"
            "    if (ord == -1 && PyErr_Occurred()) {\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "pylong_sentinel_no_errcheck"), [])

    def test_subscripted_lvalue_and_goto_branch(self):
        """Modules/_zoneinfo.c load_data: `self->trans[i]` + `goto error`."""
        c_code = (
            "static int\n"
            "load_data(PyZoneInfo *self, PyObject *num)\n"
            "{\n"
            "    Py_ssize_t cur = PyLong_AsSsize_t(num);\n"
            "    if (cur == -1) {\n"
            "        goto error;\n"
            "    }\n"
            "    return 0;\n"
            "error:\n"
            "    return -1;\n"
            "}\n"
        )
        with TempProject({"Modules/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            found = _types(result, "pylong_sentinel_no_errcheck")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["confidence"], "high")

    def test_checked_long_operand_is_not_a_finding(self):
        """Python/flowgraph.c const_folding_safe_power — NULL is a
        'do not fold' sentinel, not an error sentinel, and the operand is
        PyLong_Check-ed so no user code runs."""
        c_code = (
            "static PyObject *\n"
            "const_folding_safe_power(PyObject *v, PyObject *w)\n"
            "{\n"
            "    if (PyLong_Check(v) && PyLong_Check(w)) {\n"
            "        size_t wbits = PyLong_AsSize_t(w);\n"
            "        if (wbits == (size_t)-1) {\n"
            "            return NULL;\n"
            "        }\n"
            "    }\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Python/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "pylong_sentinel_no_errcheck"), [])


class TestUncheckedReturnFalsePositives(unittest.TestCase):
    """The five mechanical FP classes measured at 28/28 on Objects/."""

    def _analyze(self, body):
        c_code = (
            "static PyObject *\n"
            "f(PyObject *self, PyObject *args)\n"
            "{\n"
            + body
            + "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            return mod.analyze(str(root))

    def test_class_a_value_returned_directly(self):
        result = self._analyze(
            "    PyObject *repr = PyObject_Repr(self);\n"
            "    return repr;\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_class_a_value_returned_wrapped(self):
        result = self._analyze(
            "    PyObject *obj = PyObject_Call(self, args, NULL);\n"
            "    return set_orig_class(obj, self);\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_class_b_positive_form_check(self):
        result = self._analyze(
            "    PyObject *v = PyObject_Str(self);\n"
            "    if (v) {\n"
            "        Py_DECREF(v);\n"
            "    }\n"
            "    return NULL;\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_class_b_loop_condition_assignment(self):
        result = self._analyze(
            "    PyObject *key;\n"
            "    while ((key = PyIter_Next(it)) != NULL) {\n"
            "        Py_DECREF(key);\n"
            "    }\n"
            "    return NULL;\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_class_b_for_header_assignment(self):
        result = self._analyze(
            "    PyObject *key;\n"
            "    for (key = PyIter_Next(it); key; key = PyIter_Next(it)) {\n"
            "        Py_DECREF(key);\n"
            "    }\n"
            "    return NULL;\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_class_c_py_setref_alias(self):
        result = self._analyze(
            "    PyObject *tmp = PyObject_Str(self);\n"
            "    Py_SETREF(item, tmp);\n"
            "    if (item == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return item;\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_class_d_multi_assign_alias(self):
        result = self._analyze(
            "    args = tuple_args = PySequence_Tuple(args);\n"
            "    if (args == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return do_thing(tuple_args);\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_class_f_struct_member_lhs(self):
        result = self._analyze(
            "    ub->args = PyList_New(0);\n"
            "    if (ub->args == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return do_thing(ub);\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_null_tolerant_consumer(self):
        """PyModule_Add* reject NULL themselves (Python/modsupport.c:602)."""
        result = self._analyze(
            "    PyObject *r = Py_BuildValue(\"(ii)\", 1, 2);\n"
            "    if (PyModule_Add(m, \"pair\", r) < 0) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return NULL;\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_out_parameter_store(self):
        result = self._analyze(
            "    *method = PyObject_GetAttr(obj, name);\n"
            "    return NULL;\n"
        )
        self.assertEqual(_types(result, "unchecked_return"), [])

    def test_true_positive_still_fires(self):
        result = self._analyze(
            "    PyObject *v = PyObject_Str(self);\n"
            "    PyList_Append(lst, v);\n"
            "    Py_DECREF(v);\n"
            "    return NULL;\n"
        )
        found = _types(result, "unchecked_return")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["variable"], "v")
        self.assertEqual(found[0]["line"], 4)


class TestMissingNullCheck(unittest.TestCase):
    """Rule missing_null_check (deref before test)."""

    def test_true_positive_deref_before_check(self):
        c_code = (
            "static PyObject *\n"
            "f(PyObject *self)\n"
            "{\n"
            "    PyObject *r = PyObject_GetAttrString(self, \"x\");\n"
            "    Py_ssize_t n = r->ob_refcnt;\n"
            "    return PyLong_FromSsize_t(n);\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            found = _types(result, "missing_null_check")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["line"], 4)

    def test_self_reference_in_call_args_is_not_a_deref(self):
        """`f = PyMem_Malloc(sizeof *f->a)` derefs nothing yet."""
        c_code = (
            "static int\n"
            "f(struct src *src)\n"
            "{\n"
            "    fb = PyMem_Malloc(sizeof *fb + 3 * (sizeof *fb->array));\n"
            "    if (fb == NULL) {\n"
            "        PyErr_NoMemory();\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "missing_null_check"), [])


class TestRetiredRules(unittest.TestCase):
    """Rules removed in the 0/29-precision cleanup must stay removed."""

    def test_no_return_null_no_exception(self):
        c_code = (
            "static PyObject *\n"
            "f(void)\n"
            "{\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "return_null_no_exception"), [])
            self.assertNotIn("return_null_no_exception", result["summary"])

    def test_no_sparse_error_cleanup(self):
        c_code = (
            "static PyObject *\n"
            "f(void)\n"
            "{\n"
            "    PyObject *t = Py_BuildValue(\"(s)\", \"a\");\n"
            "    if (!t) {\n"
            "        goto error;\n"
            "    }\n"
            "    return t;\n"
            "error:\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Objects/t.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(_types(result, "sparse_error_cleanup"), [])
            self.assertNotIn("sparse_error_cleanup", result["summary"])


class TestAnalyze(unittest.TestCase):
    """Test full error path analysis."""

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
            self.assertIn("summary", result)
            for key in (
                "missing_null_checks",
                "unchecked_returns",
                "alloc_null_no_memerror",
                "unconditional_pyerr_clear",
                "pylong_sentinel_no_errcheck",
                "unchecked_parse_calls",
                "int_status_never_tested",
                "total_findings",
            ):
                self.assertIn(key, result["summary"])


# ---------------------------------------------------------------------------
# int_status_never_tested (issue #28 rule 4)
# ---------------------------------------------------------------------------
#
# The "value returned directly" suppression is correct for a *pointer* -- the
# callee's exception propagates untouched -- and wrong for an **int status**,
# because `return res` at the *end* of the function stops nothing in between.

# Objects/typeobject.c type_set_bases_unlocked:1966, reproduced as CPY-0070:
# SIGABRT on debug, silent permanent corruption on release.
TYPE_SET_BASES = (
    "static int\n"
    "add_all_subclasses(PyTypeObject *type, PyObject *bases)\n"
    "{\n"
    "    int res = 0;\n"
    "    if (add_subclass(type, type) < 0) {\n"
    "        res = -1;\n"
    "    }\n"
    "    return res;\n"
    "}\n"
    "\n"
    "static int\n"
    "type_set_bases_unlocked(PyTypeObject *type, PyObject *new_bases)\n"
    "{\n"
    "    PyObject *old_bases = lookup_tp_bases(type);\n"
    "    int res;\n"
    "    if (lookup_tp_bases(type) == new_bases) {\n"
    "        remove_all_subclasses(type, old_bases);\n"
    "        res = add_all_subclasses(type, new_bases);\n"
    "        if (update_all_slots(type) < 0) {\n"
    "            goto bail;\n"
    "        }\n"
    "        _PyType_Modified_Unlocked(type);\n"
    "    }\n"
    "    else {\n"
    "        res = 0;\n"
    "    }\n"
    "    Py_DECREF(old_bases);\n"
    "    return res;\n"
    "\n"
    "  bail:\n"
    "    return -1;\n"
    "}\n"
)


class TestIntStatusNeverTested(unittest.TestCase):
    KIND = "int_status_never_tested"

    def _scan(self, code, path="Objects/typeobject.c"):
        with TempProject({path: code}) as root:
            return _types(mod.analyze(str(root)), self.KIND)

    def test_the_reproduced_shape(self):
        found = self._scan(TYPE_SET_BASES)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["function"], "type_set_bases_unlocked")
        self.assertEqual(found[0]["api_call"], "add_all_subclasses")
        self.assertEqual(found[0]["variable"], "res")
        self.assertEqual(found[0]["confidence"], "high")

    def test_a_sibling_branch_assignment_is_not_a_rebind(self):
        """`else { res = 0; }` is mutually exclusive with the assignment, so it
        does not end the flagged value's life."""
        self.assertEqual(len(self._scan(TYPE_SET_BASES)), 1)

    def test_testing_the_status_suppresses(self):
        fixed = TYPE_SET_BASES.replace(
            "        res = add_all_subclasses(type, new_bases);\n",
            "        res = add_all_subclasses(type, new_bases);\n"
            "        if (res < 0) {\n"
            "            goto bail;\n"
            "        }\n",
        )
        self.assertEqual(self._scan(fixed), [])

    def test_assignment_inside_its_own_condition_is_a_test(self):
        code = (
            "static int\n"
            "ok(PyObject *d, PyObject *k, PyObject *v)\n"
            "{\n"
            "    int res;\n"
            "    if ((res = PyDict_SetItem(d, k, v)) < 0) {\n"
            "        return -1;\n"
            "    }\n"
            "    commit(d);\n"
            "    return res;\n"
            "}\n"
        )
        self.assertEqual(self._scan(code), [])

    def test_accumulate_then_return_is_correct_code(self):
        """Only cleanup between the assignment and the read."""
        code = (
            "static int\n"
            "tidy(PyObject *d, PyObject *k, PyObject *v, PyObject *tmp)\n"
            "{\n"
            "    int res = PyDict_SetItem(d, k, v);\n"
            "    Py_DECREF(tmp);\n"
            "    return res;\n"
            "}\n"
        )
        self.assertEqual(self._scan(code), [])

    def test_pointer_result_is_left_to_the_pointer_rule(self):
        """For a pointer, `return p` really does propagate the exception."""
        code = (
            "static PyObject *\n"
            "fwd(PyObject *d, PyObject *k)\n"
            "{\n"
            "    PyObject *res = PyDict_GetItemWithError(d, k);\n"
            "    commit(d);\n"
            "    return res;\n"
            "}\n"
        )
        self.assertEqual(self._scan(code), [])

    def test_int_status_callees_are_discovered_not_tabulated(self):
        """add_all_subclasses signals failure through a `res` it sets to -1,
        never with a literal `return -1;`."""
        callees = mod.int_status_callees(mod.find_functions(TYPE_SET_BASES))
        self.assertIn("add_all_subclasses", callees)
        self.assertIn("PyDict_SetItem", callees)

    def test_void_helper_is_not_an_int_status_callee(self):
        code = (
            "static void\n"
            "remove_all_subclasses(PyTypeObject *type, PyObject *bases)\n"
            "{\n"
            "    int x = -1;\n"
            "    (void)x;\n"
            "}\n"
        )
        callees = mod.int_status_callees(mod.find_functions(code))
        self.assertNotIn("remove_all_subclasses", callees)


if __name__ == "__main__":
    unittest.main()
