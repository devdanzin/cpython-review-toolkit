"""Tests for scan_pyerr_clear.py — exception-clobbering in the destructor family."""

import unittest

from helpers import TempProject, import_script


class TestScanPyErrClear(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_pyerr_clear")

    def _findings(self, files):
        with TempProject(files) as root:
            result = self.mod.analyze(str(root))
        return result

    # --- true positives ----------------------------------------------------

    def test_unguarded_clear_in_dealloc_is_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    '#include "Python.h"\n'
                    "static void\n"
                    "foo_dealloc(PyObject *self)\n"
                    "{\n"
                    '    if (PyObject_CallMethod(self, "close", NULL) == NULL) {\n'
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "    Py_TYPE(self)->tp_free(self);\n"
                    "}\n"
                )
            }
        )
        types = [f["type"] for f in result["findings"]]
        self.assertIn("pyerr_clear_in_dealloc", types)
        f = next(f for f in result["findings"] if f["type"] == "pyerr_clear_in_dealloc")
        self.assertEqual(f["function"], "foo_dealloc")
        self.assertEqual(f["slot"], "tp_dealloc")
        self.assertEqual(f["confidence"], "high")

    def test_clear_in_finalize_is_flagged(self):
        result = self._findings(
            {
                "Objects/bar.c": (
                    "static void\n"
                    "bar_finalize(PyObject *self)\n"
                    "{\n"
                    "    PyErr_Clear();\n"
                    "}\n"
                )
            }
        )
        self.assertTrue(any(f["slot"] == "tp_finalize" for f in result["findings"]))

    def test_slot_designated_function_without_suffix_is_flagged(self):
        # A teardown function whose name does NOT end in _dealloc, but which is
        # wired to tp_dealloc via a static PyTypeObject initializer.
        result = self._findings(
            {
                "Objects/baz.c": (
                    "static void\n"
                    "baz_teardown(PyObject *self)\n"
                    "{\n"
                    "    PyErr_Clear();\n"
                    "    Py_TYPE(self)->tp_free(self);\n"
                    "}\n"
                    "static PyTypeObject Baz_Type = {\n"
                    "    PyVarObject_HEAD_INIT(NULL, 0)\n"
                    '    .tp_name = "baz",\n'
                    "    .tp_dealloc = baz_teardown,\n"
                    "};\n"
                )
            }
        )
        funcs = [f["function"] for f in result["findings"]]
        self.assertIn("baz_teardown", funcs)

    def test_slot_spec_designated_function_is_flagged(self):
        result = self._findings(
            {
                "Modules/qux.c": (
                    "static void\n"
                    "qux_free(PyObject *self)\n"
                    "{\n"
                    "    PyErr_Clear();\n"
                    "}\n"
                    "static PyType_Slot qux_slots[] = {\n"
                    "    {Py_tp_dealloc, qux_free},\n"
                    "    {0, NULL},\n"
                    "};\n"
                )
            }
        )
        self.assertIn("qux_free", [f["function"] for f in result["findings"]])

    # --- true negatives ----------------------------------------------------

    def test_clear_outside_destructor_is_not_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_getattr(PyObject *self, PyObject *name)\n"
                    "{\n"
                    "    PyObject *r = PyObject_GenericGetAttr(self, name);\n"
                    "    if (r == NULL) {\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "    return r;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_dealloc_with_save_restore_is_suppressed(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *self)\n"
                    "{\n"
                    "    PyObject *exc = PyErr_GetRaisedException();\n"
                    '    if (PyObject_CallMethod(self, "close", NULL) == NULL) {\n'
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "    PyErr_SetRaisedException(exc);\n"
                    "    Py_TYPE(self)->tp_free(self);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_dealloc_with_writeunraisable_is_suppressed(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *self)\n"
                    "{\n"
                    "    if (something_failed()) {\n"
                    "        PyErr_WriteUnraisable(self);\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_dealloc_without_clear_is_not_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *self)\n"
                    "{\n"
                    "    Py_XDECREF(((FooObject *)self)->attr);\n"
                    "    Py_TYPE(self)->tp_free(self);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_comment_suppression(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *self)\n"
                    "{\n"
                    "    /* intentional: no exception can be live here */\n"
                    "    PyErr_Clear();\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    # --- CPython-specific edge ---------------------------------------------

    def test_traverse_clear_is_medium_confidence(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static int\n"
                    "foo_traverse(PyObject *self, visitproc visit, void *arg)\n"
                    "{\n"
                    "    PyErr_Clear();\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["function"] == "foo_traverse"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["slot"], "tp_traverse")
        self.assertEqual(f["confidence"], "medium")

    def test_envelope_shape(self):
        result = self._findings(
            {"Objects/foo.c": "static void foo_dealloc(PyObject *self) { }\n"}
        )
        for key in (
            "project_root",
            "scan_root",
            "files_analyzed",
            "functions_analyzed",
            "findings",
            "summary",
        ):
            self.assertIn(key, result)
        self.assertIn("by_type", result["summary"])


class TestPrivateAlias(unittest.TestCase):
    """TK-10: CPython internals spell the call ``_PyErr_Clear(tstate)``."""

    def setUp(self):
        self.mod = import_script("scan_pyerr_clear")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def test_private_alias_in_destructor_is_flagged(self):
        result = self._findings(
            {
                "Python/pystate.c": (
                    "static void\n"
                    "interpreter_clear(PyInterpreterState *interp, "
                    "PyThreadState *tstate)\n"
                    "{\n"
                    '    if (_PySys_Audit(tstate, "cpython.Clear", NULL) < 0) {\n'
                    "        _PyErr_Clear(tstate);\n"
                    "    }\n"
                    "}\n"
                )
            }
        )
        f = next(f for f in result["findings"] if f["type"] == "pyerr_clear_in_dealloc")
        self.assertEqual(f["function"], "interpreter_clear")
        self.assertEqual(f["slot"], "tp_clear")
        self.assertIn("_PyErr_Clear()", f["detail"])

    def test_private_save_restore_alias_suppresses(self):
        result = self._findings(
            {
                "Python/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *self)\n"
                    "{\n"
                    "    PyObject *exc = _PyErr_GetRaisedException(tstate);\n"
                    "    _PyErr_Clear(tstate);\n"
                    "    _PyErr_SetRaisedException(tstate, exc);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])


class TestPositionalGuard(unittest.TestCase):
    """Defect 3: the save/restore pair must actually bracket the flagged clear."""

    def setUp(self):
        self.mod = import_script("scan_pyerr_clear")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def test_second_clear_outside_the_bracket_is_still_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *self)\n"
                    "{\n"
                    "    PyObject *exc = PyErr_GetRaisedException();\n"  # 4
                    "    if (cleanup_one(self) < 0) {\n"  # 5
                    "        PyErr_Clear();\n"  # 6  bracketed
                    "    }\n"
                    "    PyErr_SetRaisedException(exc);\n"  # 8
                    "    if (cleanup_two(self) < 0) {\n"  # 9
                    "        PyErr_Clear();\n"  # 10 NOT bracketed
                    "    }\n"
                    "}\n"
                )
            }
        )
        lines = sorted(
            f["line"]
            for f in result["findings"]
            if f["type"] == "pyerr_clear_in_dealloc"
        )
        self.assertEqual(lines, [10])

    def test_bracketed_clear_is_suppressed(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_finalize(PyObject *self)\n"
                    "{\n"
                    "    PyObject *exc = PyErr_GetRaisedException();\n"
                    "    PyErr_Clear();\n"
                    "    PyErr_Clear();\n"
                    "    PyErr_SetRaisedException(exc);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])


class TestSuccessPathRule(unittest.TestCase):
    """``pyerr_clear_on_success_path`` — the gh-146102 class."""

    def setUp(self):
        self.mod = import_script("scan_pyerr_clear")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _of_type(self, result, type_):
        return [f for f in result["findings"] if f["type"] == type_]

    def test_unconditional_clear_is_flagged_high(self):
        result = self._findings(
            {
                "Objects/odictobject.c": (
                    "static int\n"
                    "mutablemapping_add_pairs(PyObject *self, PyObject *pairs)\n"
                    "{\n"
                    "    PyObject *it = PyObject_GetIter(pairs);\n"
                    "    PyErr_Clear();\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        found = self._of_type(result, "pyerr_clear_on_success_path")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "high")
        self.assertEqual(found[0]["enclosing_conditions"], [])

    def test_clear_under_a_pure_predicate_is_flagged(self):
        # memoryobject.c:3262 — equiv_shape() compares ints and sets nothing.
        result = self._findings(
            {
                "Objects/memoryobject.c": (
                    "static PyObject *\n"
                    "memory_richcompare(PyObject *v, PyObject *w, int op)\n"
                    "{\n"
                    "    if (!equiv_shape(vv, ww)) {\n"
                    "        PyErr_Clear();\n"
                    "        equal = 0;\n"
                    "        goto result;\n"
                    "    }\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        found = self._of_type(result, "pyerr_clear_on_success_path")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "medium")

    def test_clear_on_a_call_failure_path_is_not_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_get(PyObject *self, PyObject *name)\n"
                    "{\n"
                    "    PyObject *r = PyDict_GetItemWithError(d, name);\n"
                    "    if (r == NULL) {\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "    return r;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "pyerr_clear_on_success_path"), [])

    def test_exception_matches_guard_clause_suppresses(self):
        # abstract.c:223 — the narrowing is an early-return guard clause, not
        # an enclosing if. Without dominance over guard clauses this is a FP.
        result = self._findings(
            {
                "Objects/abstract.c": (
                    "int\n"
                    "PyMapping_GetOptionalItem(PyObject *obj, PyObject *key, "
                    "PyObject **result)\n"
                    "{\n"
                    "    *result = PyObject_GetItem(obj, key);\n"
                    "    if (*result) {\n"
                    "        return 1;\n"
                    "    }\n"
                    "    if (!PyErr_ExceptionMatches(PyExc_KeyError)) {\n"
                    "        return -1;\n"
                    "    }\n"
                    "    PyErr_Clear();\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_macro_return_guard_clause_suppresses(self):
        # CPython edge: Py_RETURN_TRUE is an expression_statement to
        # tree-sitter, not a return_statement.
        result = self._findings(
            {
                "Modules/_interpretersmodule.c": (
                    "static PyObject *\n"
                    "_interpreters_is_shareable_impl(PyObject *module, "
                    "PyObject *obj)\n"
                    "{\n"
                    "    if (_PyObject_CheckXIData(tstate, obj) == 0) {\n"
                    "        Py_RETURN_TRUE;\n"
                    "    }\n"
                    "    PyErr_Clear();\n"
                    "    Py_RETURN_FALSE;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "pyerr_clear_on_success_path"), [])

    def test_struct_field_lvalue_failure_path_suppresses(self):
        # CPython edge: the tested lvalue is a struct member, not an identifier.
        result = self._findings(
            {
                "Python/pystate.c": (
                    "PyObject *\n"
                    "PyInterpreterState_GetDict(PyInterpreterState *interp)\n"
                    "{\n"
                    "    if (interp->dict == NULL) {\n"
                    "        interp->dict = PyDict_New();\n"
                    "        if (interp->dict == NULL) {\n"
                    "            PyErr_Clear();\n"
                    "        }\n"
                    "    }\n"
                    "    return interp->dict;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "pyerr_clear_on_success_path"), [])

    def test_local_helper_status_check_suppresses(self):
        # A file-local static helper is not Py-prefixed, but comparing its
        # result against an error sentinel is still an error test.
        result = self._findings(
            {
                "Modules/_randommodule.c": (
                    "static int\n"
                    "random_seed(RandomObject *self, PyObject *arg)\n"
                    "{\n"
                    "    if (random_seed_urandom(self) < 0) {\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "pyerr_clear_on_success_path"), [])

    def test_sibling_branch_report_api_suppresses(self):
        result = self._findings(
            {
                "Modules/_sqlite/connection.c": (
                    "static void\n"
                    "print_or_clear_traceback(callback_context *ctx)\n"
                    "{\n"
                    "    if (ctx->state->enable_callback_tracebacks) {\n"
                    '        PyErr_FormatUnraisable("Exception ignored %R", '
                    "ctx->callable);\n"
                    "    }\n"
                    "    else {\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])


class TestUnfilteredAfterPythonCallRule(unittest.TestCase):
    """``pyerr_clear_unfiltered_after_python_call``."""

    def setUp(self):
        self.mod = import_script("scan_pyerr_clear")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _of_type(self, result, type_):
        return [f for f in result["findings"] if f["type"] == type_]

    def test_hash_failure_cleared_unfiltered_is_flagged(self):
        # unionobject.c:172 — PyObject_Hash dispatches to a user __hash__.
        result = self._findings(
            {
                "Objects/unionobject.c": (
                    "static bool\n"
                    "unionbuilder_add_single_unchecked(unionbuilder *ub, "
                    "PyObject *arg)\n"
                    "{\n"
                    "    Py_hash_t hash = PyObject_Hash(arg);\n"
                    "    if (hash == -1) {\n"
                    "        PyErr_Clear();\n"
                    "        return true;\n"
                    "    }\n"
                    "    return false;\n"
                    "}\n"
                )
            }
        )
        found = self._of_type(result, "pyerr_clear_unfiltered_after_python_call")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["failing_call"], "PyObject_Hash")
        self.assertEqual(found[0]["confidence"], "medium")

    def test_indirect_slot_call_is_flagged(self):
        # abstract.c:350 — a call through bf_getbuffer is arbitrary Python.
        result = self._findings(
            {
                "Objects/abstract.c": (
                    "int\n"
                    "PyObject_CheckReadBuffer(PyObject *obj)\n"
                    "{\n"
                    "    PyBufferProcs *pb = Py_TYPE(obj)->tp_as_buffer;\n"
                    "    Py_buffer view;\n"
                    "    if ((*pb->bf_getbuffer)(obj, &view, PyBUF_SIMPLE) == -1) {\n"
                    "        PyErr_Clear();\n"
                    "        return 0;\n"
                    "    }\n"
                    "    return 1;\n"
                    "}\n"
                )
            }
        )
        found = self._of_type(result, "pyerr_clear_unfiltered_after_python_call")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["failing_call"], "bf_getbuffer")

    def test_narrowed_clear_is_suppressed(self):
        # The load-bearing gate: this is the idiomatic 43/86 class in Objects/.
        result = self._findings(
            {
                "Objects/genericaliasobject.c": (
                    "static int\n"
                    "set_orig_class(PyObject *obj, PyObject *origin)\n"
                    "{\n"
                    "    PyObject *r = PyObject_GetAttr(obj, origin);\n"
                    "    if (r == NULL) {\n"
                    "        if (!PyErr_ExceptionMatches(PyExc_AttributeError) &&\n"
                    "            !PyErr_ExceptionMatches(PyExc_TypeError))\n"
                    "        {\n"
                    "            return -1;\n"
                    "        }\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_bounded_numeric_conversion_is_not_flagged(self):
        # rangeobject.c-style: PyLong_AsLong on a known PyLong can only raise
        # OverflowError, so it is not in the Python-reaching set.
        result = self._findings(
            {
                "Objects/rangeobject.c": (
                    "static long\n"
                    "range_get(PyObject *v)\n"
                    "{\n"
                    "    long x = PyLong_AsLong(v);\n"
                    "    if (x == -1) {\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "    return x;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_clear_then_bare_substitute_is_flagged(self):
        # Regression for the inverted `_reraises_after` gate: substituting a
        # fixed, less specific exception for whatever the user's __getitem__
        # raised IS the bug, not a mitigation. Suppressing on any PyErr_Set*
        # lost 3 of the 4 true positives in itertoolsmodule.c islice_new.
        result = self._findings(
            {
                "Objects/funcobject.c": (
                    "static int\n"
                    "descriptor_set_wrapped_attribute(PyObject *o, PyObject *k)\n"
                    "{\n"
                    "    PyObject *r = PyObject_GetItem(o, k);\n"
                    "    if (r == NULL) {\n"
                    "        PyErr_Clear();\n"
                    '        PyErr_Format(PyExc_AttributeError, "%R", k);\n'
                    "        return -1;\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(
            [f["type"] for f in result["findings"]],
            ["pyerr_clear_unfiltered_after_python_call"],
        )

    def test_clear_then_information_preserving_reraise_is_not_flagged(self):
        # The other half of the same gate: a re-raise that carries the
        # discarded exception forward (errno-derived, chained, restored) is the
        # genuine convert-the-exception idiom and must stay suppressed.
        result = self._findings(
            {
                "Objects/funcobject.c": (
                    "static int\n"
                    "descriptor_set_wrapped_attribute(PyObject *o, PyObject *k)\n"
                    "{\n"
                    "    PyObject *r = PyObject_GetItem(o, k);\n"
                    "    if (r == NULL) {\n"
                    "        PyErr_Clear();\n"
                    "        PyErr_SetFromErrno(PyExc_OSError);\n"
                    "        return -1;\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_static_type_slot_is_not_arbitrary_python(self):
        # CPython edge: PyUnicode_Type.tp_hash is a fixed C function; no user
        # code runs, unlike (*pb->bf_getbuffer)() on a runtime object.
        result = self._findings(
            {
                "Objects/dictobject.c": (
                    "Py_ssize_t\n"
                    "_PyDictKeys_StringLookupSplit(PyDictKeysObject *dk, "
                    "PyObject *key)\n"
                    "{\n"
                    "    Py_hash_t hash = unicode_get_hash(key);\n"
                    "    if (hash == -1) {\n"
                    "        hash = PyUnicode_Type.tp_hash(key);\n"
                    "        if (hash == -1) {\n"
                    "            PyErr_Clear();\n"
                    "            return DKIX_ERROR;\n"
                    "        }\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_clear_in_the_success_branch_is_not_attributed_to_the_call(self):
        # CPython edge: _testcapimodule.c:815 — the clear sits in the branch
        # taken when the import *succeeded*, so it is not clearing that failure.
        result = self._findings(
            {
                "Modules/_testcapimodule.c": (
                    "static PyObject *\n"
                    "test_capsule(PyObject *self, PyObject *args)\n"
                    "{\n"
                    "    PyObject *module = PyImport_ImportModule(name);\n"
                    "    if (module) {\n"
                    "        if (!PyErr_Occurred()) {\n"
                    "            return NULL;\n"
                    "        }\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_destructor_rule_owns_clears_in_destructors(self):
        # A Python-reaching failure cleared inside a dealloc is reported once,
        # by the destructor rule, not twice.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *self)\n"
                    "{\n"
                    "    Py_hash_t h = PyObject_Hash(self);\n"
                    "    if (h == -1) {\n"
                    "        PyErr_Clear();\n"
                    "    }\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(
            [f["type"] for f in result["findings"]], ["pyerr_clear_in_dealloc"]
        )


if __name__ == "__main__":
    unittest.main()
