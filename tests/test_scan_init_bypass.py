"""Tests for scan_init_bypass.py — __init__-bypass NULL dereferences.

Grounded in confirmed CPython crashes:
  - gh-152954: sqlite3.Connection.__new__ bypass -> NULL row_factory -> Py_INCREF
  - gh-152817: del cursor.row_factory -> NULL -> PyObject_Vectorcall
  - bytearray (main, 3.16.0a0): tp_init + PyType_GenericNew leaves
    ob_bytes_object NULL -> _PyBytes_Resize(&obj->ob_bytes_object, ...) derefs
    *pv unguarded -> SIGSEGV (exit 139, reproduced on a debug+ASan build).
"""

import unittest

from helpers import TempProject, import_script

# The bytearray shape, reduced: a positional static PyTypeObject whose tp_init
# establishes the "buffer is always non-NULL" invariant and whose tp_new is
# PyType_GenericNew, plus the unguarded _PyBytes_Resize(&self->field, n) sink.
_POSITIONAL_BYTEARRAY_SHAPE = """\
#include "Python.h"

typedef struct {
    PyObject_HEAD
    PyObject *ob_bytes_object;
    Py_ssize_t ob_alloc;
} ThingObject;

static int
thing___init___impl(ThingObject *self, PyObject *arg)
{
    /* First __init__; set ob_bytes_object so the buffer is always non-null. */
    if (self->ob_bytes_object == NULL) {
        self->ob_bytes_object = Py_GetConstant(Py_CONSTANT_EMPTY_BYTES);
    }
    return 0;
}

static int
thing_resize(PyObject *self, Py_ssize_t requested_size)
{
    ThingObject *obj = ((ThingObject *)self);
    size_t alloc = (size_t)obj->ob_alloc;
    int ret = _PyBytes_Resize(&obj->ob_bytes_object, alloc);
    return ret;
}

PyTypeObject Thing_Type = {
    PyVarObject_HEAD_INIT(NULL, 0)
    "thing",
    sizeof(ThingObject),
    0,
    0,                                  /* tp_dealloc */
    0,                                  /* tp_repr */
    thing___init__,                     /* tp_init */
    PyType_GenericAlloc,                /* tp_alloc */
    PyType_GenericNew,                  /* tp_new */
    PyObject_Free,                      /* tp_free */
};
"""

# The property shape: same positional bypass, but every read is truthiness- or
# NULL-guarded, so the scanner must stay silent *after* seeing the fields.
_POSITIONAL_PROPERTY_SHAPE = """\
#include "Python.h"

typedef struct {
    PyObject_HEAD
    PyObject *prop_get;
    PyObject *prop_set;
    PyObject *prop_del;
} PropObject;

static int
property_init_impl(PropObject *self, PyObject *get, PyObject *set, PyObject *del)
{
    self->prop_get = Py_XNewRef(get);
    self->prop_set = Py_XNewRef(set);
    self->prop_del = Py_XNewRef(del);
    return 0;
}

static PyObject *
property_descr_get(PyObject *op, PyObject *obj)
{
    PropObject *self = (PropObject *)op;
    if (self->prop_get == NULL) {
        PyErr_SetString(PyExc_AttributeError, "unreadable attribute");
        return NULL;
    }
    return PyObject_CallOneArg(self->prop_get, obj);
}

static PyObject *
property_copy(PyObject *op)
{
    PropObject *self = (PropObject *)op;
    PyObject *get = self->prop_get ? self->prop_get : Py_None;
    if (self->prop_set) {
        Py_INCREF(self->prop_set);
    }
    if (self->prop_del != NULL) {
        Py_INCREF(self->prop_del);
    }
    return Py_NewRef(get);
}

PyTypeObject Prop_Type = {
    PyVarObject_HEAD_INIT(NULL, 0)
    "property",
    sizeof(PropObject),
    0,
    0,                                  /* tp_dealloc */
    0,                                  /* tp_repr */
    property_init,                      /* tp_init */
    PyType_GenericAlloc,                /* tp_alloc */
    PyType_GenericNew,                  /* tp_new */
};
"""


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

    def test_pytype_spec_form_still_reports_both_reasons(self):
        # Modules/_sqlite/cursor.c (gh-152817): the field carries BOTH a
        # deletable member entry and a tp_init assignment, wired via PyType_Spec.
        # This is the calibration corpus — guard it against regression.
        result = self._findings(
            {
                "Modules/cur.c": (
                    "typedef struct { PyObject_HEAD PyObject *row_factory; } CurObject;\n"
                    "static int\n"
                    "cursor_init_impl(CurObject *self, PyObject *conn)\n"
                    "{\n"
                    "    self->row_factory = Py_NewRef(Py_None);\n"
                    "    return 0;\n"
                    "}\n"
                    "static PyObject *\n"
                    "cursor_iternext(PyObject *op)\n"
                    "{\n"
                    "    CurObject *self = (CurObject *)op;\n"
                    "    if (!Py_IsNone(self->row_factory)) {\n"
                    "        PyObject *args[] = { op };\n"
                    "        return PyObject_Vectorcall(self->row_factory, args, 1, NULL);\n"
                    "    }\n"
                    "    Py_RETURN_NONE;\n"
                    "}\n"
                    "static PyMemberDef cursor_members[] = {\n"
                    '    {"row_factory", _Py_T_OBJECT, offsetof(CurObject, row_factory), 0},\n'
                    "    {NULL}\n"
                    "};\n"
                    "static PyType_Slot cursor_slots[] = {\n"
                    "    {Py_tp_init, cursor_init_impl},\n"
                    "    {0, NULL},\n"
                    "};\n"
                )
            }
        )
        f = next((f for f in result["findings"] if f["field"] == "row_factory"), None)
        self.assertIsNotNone(f)
        self.assertEqual(f["reason"], "deletable_member,new_bypass")
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["sink"], "PyObject_Vectorcall")

    def test_init_that_downcasts_self_is_understood(self):
        # gh-144330's cm_init/sm_init: the tp_init takes `PyObject *self` and
        # re-casts it before storing, so receiver-anchored matching alone finds
        # no fields at all. This is the shape that provably had the bug.
        result = self._findings(
            {
                "Objects/funcy.c": (
                    "typedef struct { PyObject_HEAD PyObject *sm_callable; } staticmethod;\n"
                    "static int\n"
                    "sm_init(PyObject *self, PyObject *args, PyObject *kwds)\n"
                    "{\n"
                    "    staticmethod *sm = (staticmethod *)self;\n"
                    "    PyObject *callable;\n"
                    '    if (!PyArg_UnpackTuple(args, "staticmethod", 1, 1, &callable))\n'
                    "        return -1;\n"
                    "    Py_XSETREF(sm->sm_callable, Py_NewRef(callable));\n"
                    "    return 0;\n"
                    "}\n"
                    "static PyObject *\n"
                    "sm_call(PyObject *callable, PyObject *args, PyObject *kwargs)\n"
                    "{\n"
                    "    staticmethod *sm = (staticmethod *)callable;\n"
                    "    return PyObject_Call(sm->sm_callable, args, kwargs);\n"
                    "}\n"
                    "PyTypeObject PyStaticMethod_Type = {\n"
                    "    PyVarObject_HEAD_INIT(&PyType_Type, 0)\n"
                    '    "staticmethod",\n'
                    "    sm_init,                            /* tp_init */\n"
                    "    PyType_GenericAlloc,                /* tp_alloc */\n"
                    "    PyType_GenericNew,                  /* tp_new */\n"
                    "};\n"
                )
            }
        )
        f = next((f for f in result["findings"] if f["field"] == "sm_callable"), None)
        self.assertIsNotNone(f)
        self.assertEqual(f["function"], "sm_call")
        self.assertEqual(f["sink"], "PyObject_Call")
        self.assertEqual(f["reason"], "new_bypass")

    def test_field_read_off_a_later_parameter_is_not_flagged(self):
        # Field names collide across structs: Py_buffer also has an `obj` field,
        # so Py_TYPE(buffer->obj) must not read as the receiver's `obj`.
        result = self._findings(
            {
                "Objects/coll.c": (
                    "typedef struct { PyObject_HEAD PyObject *obj; } SuObject;\n"
                    "static int\n"
                    "su_init(PyObject *self, PyObject *args, PyObject *kwds)\n"
                    "{\n"
                    "    SuObject *su = (SuObject *)self;\n"
                    "    su->obj = Py_NewRef(args);\n"
                    "    return 0;\n"
                    "}\n"
                    "static void\n"
                    "releasebuffer_call_python(PyObject *self, Py_buffer *buffer)\n"
                    "{\n"
                    "    bool wrapped = Py_TYPE(buffer->obj) == &_PyBufferWrapper_Type;\n"
                    "    (void)wrapped;\n"
                    "}\n"
                    "PyTypeObject Su_Type = {\n"
                    "    PyVarObject_HEAD_INIT(&PyType_Type, 0)\n"
                    '    "su",\n'
                    "    su_init,                            /* tp_init */\n"
                    "    PyType_GenericAlloc,                /* tp_alloc */\n"
                    "    PyType_GenericNew,                  /* tp_new */\n"
                    "};\n"
                )
            }
        )
        # The field is still visible as nullable — only the collision is dropped.
        self.assertGreater(result["total_nullable_fields"], 0)
        self.assertEqual(result["findings"], [])

    # --- positional slot tables (the Objects/ form) ------------------------

    def test_positional_slot_table_is_parsed(self):
        # Objects/ declares types with the positional static PyTypeObject form,
        # whose ONLY marker is the trailing /* tp_init */ comment. Matching it
        # requires running against the raw source: strip_comments() deletes it.
        result = self._findings({"Objects/thing.c": _POSITIONAL_BYTEARRAY_SHAPE})
        self.assertGreater(result["total_nullable_fields"], 0)
        self.assertIn("new_bypass", result["nullable_fields_by_reason"])

    def test_bytearray_shape_addr_deref_is_flagged(self):
        # The live SIGSEGV: tp_init sets the field, tp_new is PyType_GenericNew,
        # and _PyBytes_Resize(&self->field, n) dereferences *pv unguarded.
        result = self._findings({"Objects/thing.c": _POSITIONAL_BYTEARRAY_SHAPE})
        f = next(
            (f for f in result["findings"] if f["field"] == "ob_bytes_object"), None
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["function"], "thing_resize")
        self.assertEqual(f["sink"], "_PyBytes_Resize")
        self.assertEqual(f["reason"], "new_bypass")
        self.assertIn("derefs *pv", f["detail"])

    def test_positional_real_tp_new_blocks_bypass(self):
        # Same shape, but the block wires a real tp_new that initializes the
        # field — T.__new__(T) can no longer produce a NULL.
        source = _POSITIONAL_BYTEARRAY_SHAPE.replace(
            "    PyType_GenericNew,                  /* tp_new */",
            "    thing_new,                          /* tp_new */",
        )
        result = self._findings({"Objects/thing.c": source})
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["total_nullable_fields"], 0)

    def test_positional_pairing_is_per_type_block(self):
        # A second type in the same file with a real tp_new must not hide the
        # first type's bypass (descrobject.c's mappingproxy_new vs property).
        source = _POSITIONAL_BYTEARRAY_SHAPE + (
            "static PyObject *other_new(PyTypeObject *t, PyObject *a, PyObject *k);\n"
            "PyTypeObject Other_Type = {\n"
            "    PyVarObject_HEAD_INIT(NULL, 0)\n"
            '    "other",\n'
            "    0,                                  /* tp_init */\n"
            "    other_new,                          /* tp_new */\n"
            "};\n"
        )
        result = self._findings({"Objects/thing.c": source})
        f = next(
            (f for f in result["findings"] if f["field"] == "ob_bytes_object"), None
        )
        self.assertIsNotNone(f)

    def test_macro_wrapped_member_offset_is_parsed(self):
        # funcobject.c / methodobject.c wrap offsetof in a file-local macro:
        #     #define OFF(x) offsetof(PyFunctionObject, x)
        result = self._findings(
            {
                "Objects/funcy.c": (
                    "typedef struct { PyObject_HEAD PyObject *func_doc; } FuncObject;\n"
                    "#define OFF(x) offsetof(FuncObject, x)\n"
                    "static void\n"
                    "funcy_use(PyObject *op)\n"
                    "{\n"
                    "    FuncObject *self = (FuncObject *)op;\n"
                    "    Py_INCREF(self->func_doc);\n"
                    "}\n"
                    "static PyMemberDef funcy_members[] = {\n"
                    '    {"__doc__", _Py_T_OBJECT, OFF(func_doc), 0},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        f = next((f for f in result["findings"] if f["field"] == "func_doc"), None)
        self.assertIsNotNone(f)
        self.assertEqual(f["reason"], "deletable_member")
        self.assertEqual(f["confidence"], "high")

    # --- getset setters as a nullability source ---------------------------

    def test_null_accepting_getset_setter_is_a_nullability_source(self):
        # typealias_set_module (Objects/typevarobject.c): stores Py_XNewRef(value)
        # with no rejection, so `del ta.__module__` leaves the field NULL.
        result = self._findings(
            {
                "Objects/alias.c": (
                    "typedef struct { PyObject_HEAD PyObject *module; } AliasObject;\n"
                    "static int\n"
                    "alias_set_module(PyObject *self, PyObject *value, void *unused)\n"
                    "{\n"
                    "    AliasObject *ta = (AliasObject *)self;\n"
                    "    Py_XSETREF(ta->module, Py_XNewRef(value));\n"
                    "    return 0;\n"
                    "}\n"
                    "static PyObject *\n"
                    "alias_use(PyObject *self)\n"
                    "{\n"
                    "    AliasObject *ta = (AliasObject *)self;\n"
                    "    return Py_NewRef(ta->module);\n"
                    "}\n"
                    "static PyGetSetDef alias_getset[] = {\n"
                    '    {"__module__", alias_module, alias_set_module, NULL, NULL},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        f = next((f for f in result["findings"] if f["field"] == "module"), None)
        self.assertIsNotNone(f)
        self.assertEqual(f["reason"], "deletable_getset")
        self.assertEqual(f["confidence"], "high")

    def test_delete_rejecting_getset_setter_is_not_a_source(self):
        result = self._findings(
            {
                "Objects/alias.c": (
                    "typedef struct { PyObject_HEAD PyObject *module; } AliasObject;\n"
                    "static int\n"
                    "alias_set_module(PyObject *self, PyObject *value, void *unused)\n"
                    "{\n"
                    "    AliasObject *ta = (AliasObject *)self;\n"
                    "    if (value == NULL) {\n"
                    '        PyErr_SetString(PyExc_AttributeError, "cannot delete");\n'
                    "        return -1;\n"
                    "    }\n"
                    "    Py_XSETREF(ta->module, Py_NewRef(value));\n"
                    "    return 0;\n"
                    "}\n"
                    "static PyObject *\n"
                    "alias_use(PyObject *self)\n"
                    "{\n"
                    "    AliasObject *ta = (AliasObject *)self;\n"
                    "    return Py_NewRef(ta->module);\n"
                    "}\n"
                    "static PyGetSetDef alias_getset[] = {\n"
                    '    {"__module__", alias_module, alias_set_module, NULL, NULL},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_getset_setter_validating_via_helper_is_not_a_source(self):
        # element_tag_setter (Modules/_elementtree.c) rejects deletion inside the
        # _VALIDATE_ATTR_VALUE macro; type_set_qualname does it inside
        # check_set_special_type_attr. Unresolved indirection over the value
        # parameter must be treated as a rejection.
        result = self._findings(
            {
                "Modules/et.c": (
                    "typedef struct { PyObject_HEAD PyObject *tag; } ElemObject;\n"
                    "static int\n"
                    "elem_tag_setter(PyObject *op, PyObject *value, void *closure)\n"
                    "{\n"
                    "    _VALIDATE_ATTR_VALUE(value);\n"
                    "    ElemObject *self = (ElemObject *)op;\n"
                    "    Py_SETREF(self->tag, Py_NewRef(value));\n"
                    "    return 0;\n"
                    "}\n"
                    "static PyObject *\n"
                    "elem_use(PyObject *op)\n"
                    "{\n"
                    "    ElemObject *self = (ElemObject *)op;\n"
                    "    return Py_NewRef(self->tag);\n"
                    "}\n"
                    "static PyGetSetDef elem_getset[] = {\n"
                    '    {"tag", elem_tag_getter, elem_tag_setter, NULL, NULL},\n'
                    "    {NULL}\n"
                    "};\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    # --- true negatives ----------------------------------------------------

    def test_positional_truthiness_guards_suppress(self):
        # property (Objects/descrobject.c) has exactly the bypass shape and is
        # fully defended: every read is behind `field == NULL` / `if (field)` /
        # `field ?`. P1 makes the fields visible; the zero must stay EARNED.
        result = self._findings({"Objects/prop.c": _POSITIONAL_PROPERTY_SHAPE})
        self.assertEqual(result["findings"], [])
        # ...but the rule DID fire — that is what makes the zero meaningful.
        self.assertGreaterEqual(result["total_nullable_fields"], 3)
        self.assertGreaterEqual(
            result["nullable_fields_by_reason"].get("new_bypass", 0), 3
        )

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
            # Recall canary — see the module docstring.
            "total_nullable_fields",
            "files_with_nullable_fields",
            "nullable_fields_by_reason",
        ):
            self.assertIn(key, result)

    def test_recall_canary_distinguishes_silence_from_safety(self):
        # A scope with nothing to see reports zero nullable fields; a scope with
        # a guarded candidate reports zero findings but NON-zero nullable
        # fields. Consumers must check the latter before calling a scope clean.
        silent = self._findings(
            {"Modules/foo.c": "static void foo(PyObject *self) { }\n"}
        )
        self.assertEqual(silent["total_nullable_fields"], 0)
        self.assertEqual(silent["findings"], [])

        earned = self._findings({"Objects/prop.c": _POSITIONAL_PROPERTY_SHAPE})
        self.assertGreater(earned["total_nullable_fields"], 0)
        self.assertEqual(earned["findings"], [])


if __name__ == "__main__":
    unittest.main()
