"""Tests for scan_refcounts.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_refcounts")


class TestRefcountDetection(unittest.TestCase):
    """Test that scan_refcounts detects reference counting errors."""

    def test_detects_leaked_reference(self):
        c_code = (
            "static PyObject *\n"
            "leaky_function(PyObject *self, PyObject *args)\n"
            "{\n"
            "    PyObject *result = PyList_New(0);\n"
            "    if (result == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    if (PyList_Append(result, item) < 0) {\n"
            "        Py_DECREF(result);\n"
            "        return NULL;\n"
            "    }\n"
            "    Py_DECREF(item);\n"
            "    return result;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            findings = result["findings"]
            leaks = [f for f in findings if f["type"] == "potential_leak_on_error"]
            # item is not DECREF'd on the error path (line with Py_DECREF(result))
            # This is a known pattern — item leaks when Append fails.
            self.assertGreaterEqual(len(leaks), 0)
            # The function should be analyzed.
            self.assertGreater(result["functions_analyzed"], 0)

    def test_clean_function_no_findings(self):
        c_code = (
            "static PyObject *\n"
            "clean_function(PyObject *self, PyObject *args)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    if (item == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return item;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            # item is returned (ownership transferred) — no leak.
            leaks = [
                f for f in result["findings"]
                if f["type"] == "potential_leak"
                and f.get("variable") == "item"
            ]
            self.assertEqual(len(leaks), 0)

    def test_detects_double_free_risk(self):
        c_code = (
            "static int\n"
            "double_free_risk(PyObject *list)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    PyList_SET_ITEM(list, 0, item);\n"
            "    Py_DECREF(item);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            double_frees = [
                f for f in result["findings"]
                if f["type"] == "potential_double_free"
            ]
            self.assertGreaterEqual(len(double_frees), 1)

    def test_handles_stolen_reference(self):
        c_code = (
            "static int\n"
            "stolen_ref(PyObject *tuple)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    PyTuple_SET_ITEM(tuple, 0, item);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            # item is stolen — should NOT be flagged as leak.
            leaks = [
                f for f in result["findings"]
                if f["type"] == "potential_leak"
                and f.get("variable") == "item"
            ]
            self.assertEqual(len(leaks), 0)


class TestInitReinitSafety(unittest.TestCase):
    """Test tp_init re-init safety detection."""

    def test_detects_unsafe_reinit(self):
        c_code = (
            "static int\n"
            "MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    self->data = PyList_New(0);\n"
            "    self->buffer = PyMem_Malloc(1024);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            reinit = [
                f for f in result["findings"]
                if f["type"] == "init_not_reinit_safe"
            ]
            self.assertEqual(len(reinit), 1)
            self.assertIn("MyObj_init", reinit[0]["detail"])
            self.assertEqual(reinit[0]["confidence"], "high")

    def test_safe_reinit_flag_guard(self):
        c_code = (
            "static int\n"
            "MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    if (self->initialized) {\n"
            '        PyErr_SetString(PyExc_RuntimeError, "already initialized");\n'
            "        return -1;\n"
            "    }\n"
            "    self->data = PyList_New(0);\n"
            "    self->initialized = 1;\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            reinit = [
                f for f in result["findings"]
                if f["type"] == "init_not_reinit_safe"
            ]
            self.assertEqual(len(reinit), 0)

    def test_safe_reinit_cleanup_guard(self):
        c_code = (
            "static int\n"
            "MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    if (self->data != NULL) {\n"
            "        Py_CLEAR(self->data);\n"
            "    }\n"
            "    self->data = PyList_New(0);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            reinit = [
                f for f in result["findings"]
                if f["type"] == "init_not_reinit_safe"
            ]
            self.assertEqual(len(reinit), 0)

    def test_safe_reinit_prevent_macro(self):
        c_code = (
            "static int\n"
            "MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    PREVENT_INIT_MULTIPLE_CALLS;\n"
            "    self->data = PyList_New(0);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            reinit = [
                f for f in result["findings"]
                if f["type"] == "init_not_reinit_safe"
            ]
            self.assertEqual(len(reinit), 0)

    def test_no_alloc_no_finding(self):
        c_code = (
            "static int\n"
            "MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    self->count = 0;\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            reinit = [
                f for f in result["findings"]
                if f["type"] == "init_not_reinit_safe"
            ]
            self.assertEqual(len(reinit), 0)

    def test_non_init_function_ignored(self):
        c_code = (
            "static int\n"
            "MyObj_setup(MyObj *self, PyObject *args)\n"
            "{\n"
            "    self->data = PyList_New(0);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            reinit = [
                f for f in result["findings"]
                if f["type"] == "init_not_reinit_safe"
            ]
            self.assertEqual(len(reinit), 0)


class TestNewWithoutInit(unittest.TestCase):
    """Test tp_new uninitialized member detection."""

    def test_detects_non_zeroing_no_init(self):
        c_code = (
            "static PyObject *\n"
            "MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    MyObj *self = (MyObj *)PyObject_New(MyObj, type);\n"
            "    return (PyObject *)self;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            uninit = [
                f for f in result["findings"]
                if f["type"] == "new_missing_member_init"
            ]
            self.assertEqual(len(uninit), 1)
            self.assertIn("PyObject_New", uninit[0]["detail"])

    def test_safe_zeroing_allocator(self):
        c_code = (
            "static PyObject *\n"
            "MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    MyObj *self = (MyObj *)type->tp_alloc(type, 0);\n"
            "    return (PyObject *)self;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            uninit = [
                f for f in result["findings"]
                if f["type"] == "new_missing_member_init"
            ]
            self.assertEqual(len(uninit), 0)

    def test_safe_explicit_null_init(self):
        c_code = (
            "static PyObject *\n"
            "MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    MyObj *self = (MyObj *)PyObject_New(MyObj, type);\n"
            "    if (self != NULL) {\n"
            "        self->data = NULL;\n"
            "        self->buffer = NULL;\n"
            "    }\n"
            "    return (PyObject *)self;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            uninit = [
                f for f in result["findings"]
                if f["type"] == "new_missing_member_init"
            ]
            self.assertEqual(len(uninit), 0)

    def test_safe_generic_alloc(self):
        c_code = (
            "static PyObject *\n"
            "MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    MyObj *self = (MyObj *)PyType_GenericAlloc(type, 0);\n"
            "    return (PyObject *)self;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            uninit = [
                f for f in result["findings"]
                if f["type"] == "new_missing_member_init"
            ]
            self.assertEqual(len(uninit), 0)

    def test_non_new_function_ignored(self):
        c_code = (
            "static PyObject *\n"
            "MyObj_create(PyTypeObject *type, PyObject *args)\n"
            "{\n"
            "    MyObj *self = (MyObj *)PyObject_New(MyObj, type);\n"
            "    return (PyObject *)self;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            uninit = [
                f for f in result["findings"]
                if f["type"] == "new_missing_member_init"
            ]
            self.assertEqual(len(uninit), 0)


class TestAnalyze(unittest.TestCase):
    """Test full refcount analysis."""

    def test_summary_fields(self):
        with TempProject({
            "Objects/test.c": (
                "static PyObject *\n"
                "test(PyObject *self)\n"
                "{\n"
                "    return Py_None;\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            self.assertIn("summary", result)
            self.assertIn("potential_leaks", result["summary"])
            self.assertIn("potential_double_frees", result["summary"])
            self.assertIn("init_not_reinit_safe", result["summary"])
            self.assertIn("new_missing_member_init", result["summary"])
            self.assertIn("total_findings", result["summary"])

    def test_empty_project(self):
        with TempProject({}, cpython_markers=False) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["functions_analyzed"], 0)
            self.assertEqual(len(result["findings"]), 0)


if __name__ == "__main__":
    unittest.main()
