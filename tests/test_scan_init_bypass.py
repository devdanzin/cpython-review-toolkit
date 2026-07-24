"""Tests for scan_init_bypass.py — __init__-bypass NULL dereferences.

Grounded in two confirmed CPython crashes:
  - gh-152954: sqlite3.Connection.__new__ bypass -> NULL row_factory -> Py_INCREF
  - gh-152817: del cursor.row_factory -> NULL -> PyObject_Vectorcall
"""

import unittest

from helpers import TempProject, import_script


class TestScanInitBypass(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_init_bypass")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    # --- true positives ----------------------------------------------------

    def test_deletable_member_incref_is_flagged(self):
        # A T_OBJECT_EX member (deletable via `del obj.cb`) is Py_INCREF'd with
        # no NULL guard.
        result = self._findings(
            {
                "Modules/foo.c": (
                    '#include "Python.h"\n'
                    "typedef struct { PyObject_HEAD PyObject *cb; } FooObject;\n"
                    "static void\n"
                    "foo_use(PyObject *op)\n"
                    "{\n"
                    "    FooObject *self = (FooObject *)op;\n"
                    "    Py_INCREF(self->cb);\n"
                    "}\n"
                    "static PyMemberDef foo_members[] = {\n"
                    '    {"cb", T_OBJECT_EX, offsetof(FooObject, cb), 0},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["type"] == "init_bypass_null_deref"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["function"], "foo_use")
        self.assertEqual(f["field"], "cb")
        self.assertEqual(f["sink"], "Py_INCREF")
        self.assertEqual(f["confidence"], "high")
        self.assertIn("deletable_member", f["reason"])
        # Findings carry the documented envelope keys.
        for key in ("type", "function", "line", "confidence", "detail", "file"):
            self.assertIn(key, f)

    def test_deletable_field_vectorcall_via_alias_is_flagged(self):
        # Mirrors gh-152817: `!Py_IsNone(self->factory)` is NOT a NULL guard, and
        # the factory is aliased into a local before the call.
        result = self._findings(
            {
                "Modules/bar.c": (
                    "typedef struct { PyObject_HEAD PyObject *factory; } BarObject;\n"
                    "static PyObject *\n"
                    "bar_call(PyObject *op)\n"
                    "{\n"
                    "    BarObject *self = (BarObject *)op;\n"
                    "    if (!Py_IsNone(self->factory)) {\n"
                    "        PyObject *f = self->factory;\n"
                    "        PyObject *args[] = { op };\n"
                    "        return PyObject_Vectorcall(f, args, 1, NULL);\n"
                    "    }\n"
                    "    Py_RETURN_NONE;\n"
                    "}\n"
                    "static PyMemberDef bar_members[] = {\n"
                    '    {"factory", Py_T_OBJECT_EX, offsetof(BarObject, factory), 0},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        f = next((f for f in result["findings"] if f["field"] == "factory"), None)
        self.assertIsNotNone(f)
        self.assertEqual(f["sink"], "PyObject_Vectorcall")
        self.assertEqual(f["confidence"], "high")

    def test_new_bypass_field_incref_is_flagged(self):
        # Mirrors gh-152954: field set only in tp_init, no tp_new -> NULL after
        # T.__new__(T). `!= Py_None` is not a NULL guard.
        result = self._findings(
            {
                "Modules/conn.c": (
                    "typedef struct { PyObject_HEAD PyObject *row_factory; } ConnObject;\n"
                    "static int\n"
                    "conn_init_impl(ConnObject *self, PyObject *args)\n"
                    "{\n"
                    "    self->row_factory = Py_NewRef(Py_None);\n"
                    "    return 0;\n"
                    "}\n"
                    "static PyObject *\n"
                    "conn_make_cursor(PyObject *op)\n"
                    "{\n"
                    "    ConnObject *self = (ConnObject *)op;\n"
                    "    if (self->row_factory != Py_None) {\n"
                    "        Py_INCREF(self->row_factory);\n"
                    "    }\n"
                    "    Py_RETURN_NONE;\n"
                    "}\n"
                    "static PyType_Slot conn_slots[] = {\n"
                    "    {Py_tp_init, conn_init_impl},\n"
                    "    {0, NULL},\n"
                    "};\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["function"] == "conn_make_cursor"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["field"], "row_factory")
        self.assertEqual(f["reason"], "new_bypass")
        self.assertEqual(f["confidence"], "medium")

    # --- true negatives ----------------------------------------------------

    def test_explicit_null_guard_is_suppressed(self):
        result = self._findings(
            {
                "Modules/baz.c": (
                    "typedef struct { PyObject_HEAD PyObject *cb; } BazObject;\n"
                    "static void\n"
                    "baz_use(PyObject *op)\n"
                    "{\n"
                    "    BazObject *self = (BazObject *)op;\n"
                    "    if (self->cb == NULL) {\n"
                    "        return;\n"
                    "    }\n"
                    "    Py_INCREF(self->cb);\n"
                    "}\n"
                    "static PyMemberDef baz_members[] = {\n"
                    '    {"cb", T_OBJECT_EX, offsetof(BazObject, cb), 0},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_truthiness_guard_is_suppressed(self):
        # `if (self->msg && ...)` IS a NULL guard (the ImportError_str idiom).
        result = self._findings(
            {
                "Modules/exc.c": (
                    "typedef struct { PyObject_HEAD PyObject *msg; } ExcObject;\n"
                    "static PyObject *\n"
                    "exc_str(PyObject *op)\n"
                    "{\n"
                    "    ExcObject *self = (ExcObject *)op;\n"
                    "    if (self->msg && PyUnicode_Check(self->msg)) {\n"
                    "        return Py_NewRef(self->msg);\n"
                    "    }\n"
                    "    Py_RETURN_NONE;\n"
                    "}\n"
                    "static PyMemberDef exc_members[] = {\n"
                    '    {"msg", Py_T_OBJECT_EX, offsetof(ExcObject, msg), 0},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_readonly_member_is_not_nullable(self):
        # A READONLY member cannot be deleted, so it is not treated as nullable.
        result = self._findings(
            {
                "Modules/qux.c": (
                    "typedef struct { PyObject_HEAD PyObject *owner; } QuxObject;\n"
                    "static void\n"
                    "qux_use(PyObject *op)\n"
                    "{\n"
                    "    QuxObject *self = (QuxObject *)op;\n"
                    "    Py_INCREF(self->owner);\n"
                    "}\n"
                    "static PyMemberDef qux_members[] = {\n"
                    '    {"owner", Py_T_OBJECT_EX, offsetof(QuxObject, owner), Py_READONLY},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_tp_new_present_disables_bypass(self):
        # When the type wires a tp_new (controls instantiation), the
        # __new__-bypass reasoning does not apply.
        result = self._findings(
            {
                "Modules/conn.c": (
                    "typedef struct { PyObject_HEAD PyObject *row_factory; } ConnObject;\n"
                    "static int\n"
                    "conn_init_impl(ConnObject *self, PyObject *args)\n"
                    "{\n"
                    "    self->row_factory = Py_NewRef(Py_None);\n"
                    "    return 0;\n"
                    "}\n"
                    "static PyObject *\n"
                    "conn_make_cursor(PyObject *op)\n"
                    "{\n"
                    "    ConnObject *self = (ConnObject *)op;\n"
                    "    if (self->row_factory != Py_None) {\n"
                    "        Py_INCREF(self->row_factory);\n"
                    "    }\n"
                    "    Py_RETURN_NONE;\n"
                    "}\n"
                    "static PyType_Slot conn_slots[] = {\n"
                    "    {Py_tp_new, conn_new},\n"
                    "    {Py_tp_init, conn_init_impl},\n"
                    "    {0, NULL},\n"
                    "};\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_comment_suppression(self):
        result = self._findings(
            {
                "Modules/foo.c": (
                    "typedef struct { PyObject_HEAD PyObject *cb; } FooObject;\n"
                    "static void\n"
                    "foo_use(PyObject *op)\n"
                    "{\n"
                    "    FooObject *self = (FooObject *)op;\n"
                    "    /* intentional: cb cannot be NULL on this path */\n"
                    "    Py_INCREF(self->cb);\n"
                    "}\n"
                    "static PyMemberDef foo_members[] = {\n"
                    '    {"cb", T_OBJECT_EX, offsetof(FooObject, cb), 0},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    # --- envelope ----------------------------------------------------------

    def test_envelope_shape(self):
        result = self._findings(
            {"Modules/foo.c": "static void foo(PyObject *self) { }\n"}
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
