"""Tests for scan_memory_patterns.py — allocation-size overflow, GC-track, and
allocator-family mismatch checks."""

import unittest

from helpers import TempProject, import_script


class TestScanMemoryPatterns(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_memory_patterns")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _of_type(self, result, type_name):
        return [f for f in result["findings"] if f["type"] == type_name]

    # === Check 1: alloc_size_overflow =====================================

    def test_alloc_overflow_python_derived_size_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "make_buffer(PyObject *self, PyObject *arg)\n"
                    "{\n"
                    "    Py_ssize_t n = PyLong_AsSsize_t(arg);\n"
                    "    int *buf = PyMem_Malloc(n * sizeof(int));\n"
                    "    if (buf == NULL) return PyErr_NoMemory();\n"
                    "    return (PyObject *)buf;\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "alloc_size_overflow")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "make_buffer")
        self.assertEqual(hits[0]["confidence"], "medium")

    def test_alloc_overflow_with_max_guard_is_safe(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "make_buffer(PyObject *self, PyObject *arg)\n"
                    "{\n"
                    "    Py_ssize_t n = PyLong_AsSsize_t(arg);\n"
                    "    if (n < 0 || n > PY_SSIZE_T_MAX / sizeof(int))\n"
                    "        return NULL;\n"
                    "    int *buf = PyMem_Malloc(n * sizeof(int));\n"
                    "    return (PyObject *)buf;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "alloc_size_overflow"), [])

    def test_alloc_overflow_pymem_new_is_safe(self):
        # PyMem_New overflow-checks internally; count/size are separate args,
        # never a bare `a * b`, so it is never inspected.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "make_buffer(PyObject *self, PyObject *arg)\n"
                    "{\n"
                    "    Py_ssize_t n = PyLong_AsSsize_t(arg);\n"
                    "    int *buf = PyMem_New(int, n);\n"
                    "    return (PyObject *)buf;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "alloc_size_overflow"), [])

    def test_alloc_overflow_constant_multiply_is_safe(self):
        # No Python-derived operand -> a sizeof-only constant multiply is silent.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void *\n"
                    "make_fixed(void)\n"
                    "{\n"
                    "    return PyMem_Malloc(16 * sizeof(int));\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "alloc_size_overflow"), [])

    def test_alloc_overflow_comment_suppression(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "make_buffer(PyObject *self, PyObject *arg)\n"
                    "{\n"
                    "    Py_ssize_t n = PyLong_AsSsize_t(arg);\n"
                    "    /* safety: n is validated by the caller */\n"
                    "    int *buf = PyMem_Malloc(n * sizeof(int));\n"
                    "    return (PyObject *)buf;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "alloc_size_overflow"), [])

    # === Check 2: gc_untrack_without_track ================================

    def test_gc_free_before_track_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(FooObject *op)\n"
                    "{\n"
                    "    _PyObject_GC_UNTRACK(op);\n"
                    "    Py_XDECREF(op->attr);\n"
                    "    PyObject_GC_Del(op);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) {\n"
                    "        Py_DECREF(op);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    PyObject_GC_Track(op);\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "gc_untrack_without_track")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "foo_new")

    def test_gc_safe_function_untrack_is_not_gated_in(self):
        # File uses the safe *function* PyObject_GC_UnTrack, never the macro,
        # so the free-before-track idiom here cannot hit the O6 bug -> silent.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(FooObject *op)\n"
                    "{\n"
                    "    PyObject_GC_UnTrack(op);\n"
                    "    Py_XDECREF(op->attr);\n"
                    "    PyObject_GC_Del(op);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) {\n"
                    "        Py_DECREF(op);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    PyObject_GC_Track(op);\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "gc_untrack_without_track"), [])

    def test_gc_track_before_free_is_safe(self):
        # Object is tracked before the fallible step; the later free is safe.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "bar_dealloc(BarObject *op)\n"
                    "{\n"
                    "    _PyObject_GC_UNTRACK(op);\n"
                    "    PyObject_GC_Del(op);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "bar_new(PyTypeObject *type)\n"
                    "{\n"
                    "    BarObject *op = PyObject_GC_New(BarObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = NULL;\n"
                    "    PyObject_GC_Track(op);\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) {\n"
                    "        Py_DECREF(op);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "gc_untrack_without_track"), [])

    def test_gc_comment_suppression(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(FooObject *op)\n"
                    "{\n"
                    "    _PyObject_GC_UNTRACK(op);\n"
                    "    PyObject_GC_Del(op);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) {\n"
                    "        /* intentional: dealloc tolerates the untracked object */\n"
                    "        Py_DECREF(op);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    PyObject_GC_Track(op);\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "gc_untrack_without_track"), [])

    # === Check 3: mismatched_alloc_free ===================================

    def test_mismatch_pymem_alloc_raw_free_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "frob(void)\n"
                    "{\n"
                    "    char *buf = PyMem_Malloc(64);\n"
                    "    do_work(buf);\n"
                    "    free(buf);\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "mismatched_alloc_free")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "frob")
        self.assertEqual(hits[0]["confidence"], "high")

    def test_mismatch_pyobject_alloc_pymem_free_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "frob(void)\n"
                    "{\n"
                    "    void *p = PyObject_Malloc(128);\n"
                    "    PyMem_Free(p);\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "mismatched_alloc_free")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "frob")

    def test_mismatch_matching_family_is_safe(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "frob(void)\n"
                    "{\n"
                    "    char *buf = PyMem_Malloc(64);\n"
                    "    do_work(buf);\n"
                    "    PyMem_Free(buf);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "mismatched_alloc_free"), [])

    def test_mismatch_comment_suppression(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "frob(void)\n"
                    "{\n"
                    "    char *buf = PyMem_Malloc(64);\n"
                    "    /* nolint: ownership handed to a raw-free API */\n"
                    "    free(buf);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "mismatched_alloc_free"), [])

    # === Envelope =========================================================

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
        self.assertIn("by_type", result["summary"])
        self.assertIn("by_confidence", result["summary"])


if __name__ == "__main__":
    unittest.main()
