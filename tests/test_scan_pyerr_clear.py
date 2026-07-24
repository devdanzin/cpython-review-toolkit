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


if __name__ == "__main__":
    unittest.main()
