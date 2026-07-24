"""Tests for scan_uninit_dealloc.py — freeing a half-constructed object."""

import unittest

from helpers import TempProject, import_script


class TestScanUninitDealloc(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_uninit_dealloc")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    # --- true positives ----------------------------------------------------

    def test_free_before_member_init_is_flagged(self):
        result = self._findings(
            {
                "Objects/templateobject.c": (
                    "static PyObject *\n"
                    "template_iter_new(PyTypeObject *type, PyObject *tmpl)\n"
                    "{\n"
                    "    TemplateIter *it = PyObject_GC_New(TemplateIter, type);\n"
                    "    if (it == NULL) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    it->stringsiter = PyObject_GetIter(tmpl);\n"
                    "    if (it->stringsiter == NULL) {\n"
                    "        Py_DECREF(it);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    it->index = 0;\n"
                    "    return (PyObject *)it;\n"
                    "}\n"
                )
            }
        )
        f = next(
            (
                f
                for f in result["findings"]
                if f["type"] == "dealloc_of_uninitialized_object"
            ),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["variable"], "it")
        self.assertEqual(f["allocator"], "PyObject_GC_New")
        self.assertEqual(f["confidence"], "medium")

    # --- true negatives ----------------------------------------------------

    def test_memset_zeroed_is_safe(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    memset(op->fields, 0, sizeof(op->fields));\n"
                    "    op->attr = PyList_New(0);\n"
                    "    if (op->attr == NULL) { Py_DECREF(op); return NULL; }\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_members_nulled_before_error_is_safe(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = NULL;\n"
                    "    op->other = NULL;\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) { Py_DECREF(op); return NULL; }\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_zeroing_allocator_is_safe(self):
        # tp_alloc / PyType_GenericAlloc zero the object; not flagged.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = (FooObject *)type->tp_alloc(type, 0);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) { Py_DECREF(op); return NULL; }\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_no_early_free_is_safe(self):
        # Object never freed inside the constructor -> nothing to flag.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = NULL;\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_comment_suppression(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    /* intentional: dealloc handles the uninitialized case */\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) { Py_DECREF(op); return NULL; }\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_envelope_shape(self):
        result = self._findings({"Objects/foo.c": "static void foo(void) { }\n"})
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
