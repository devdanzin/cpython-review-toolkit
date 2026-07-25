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


class TestSpecSlotTablePairing(unittest.TestCase):
    """The PyType_Spec form pairs tp_init with the tp_new of its OWN table.

    Regression for the whole-file kill switch: `_TP_NEW_TOKENS_RE` disabled the
    new_bypass signal for an entire file as soon as the token `Py_tp_new`
    appeared anywhere in it — even though `{Py_tp_new, PyType_GenericNew}` is
    the canonical *bypassable* wiring. 21 of the 58 slot tables carrying a
    Py_tp_init tree-wide (36%) were silenced, including
    Modules/_asynciomodule.c Task_slots, whose
    `_asyncio.Task.__new__(_asyncio.Task).get_context()` is a reproduced
    SIGSEGV (exit 139 on all four build variants).
    """

    def setUp(self):
        self.mod = import_script("scan_init_bypass")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    # The _asynciomodule.c Task shape, reduced.
    _TASK = """\
#include "Python.h"

typedef struct {
    PyObject_HEAD
    PyObject *task_context;
    PyObject *task_coro;
} TaskObj;

static int
_asyncio_Task___init___impl(TaskObj *self, PyObject *ctx, PyObject *coro)
{
    self->task_context = Py_NewRef(ctx);
    self->task_coro = Py_NewRef(coro);
    return 0;
}

static PyObject *
_asyncio_Task_get_context_impl(TaskObj *self)
{
    return Py_NewRef(self->task_context);
}

static PyObject *
_asyncio_Task_get_coro_impl(TaskObj *self)
{
    if (self->task_coro) {
        return Py_NewRef(self->task_coro);
    }
    Py_RETURN_NONE;
}

static PyType_Slot Task_slots[] = {
    {Py_tp_doc, (void *)_asyncio_Task___init____doc__},
    {Py_tp_init, _asyncio_Task___init__},
    {Py_tp_new, PyType_GenericNew},
    {0, 0},
};

static PyType_Spec Task_spec = {
    .name = "_asyncio.Task",
    .basicsize = sizeof(TaskObj),
    .flags = (Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE),
    .slots = Task_slots,
};
"""

    def test_true_positive_generic_new_does_not_protect(self):
        result = self._findings({"Modules/_asynciomodule.c": self._TASK})
        got = [
            (f["function"], f["field"], f["reason"]) for f in result["findings"]
        ]
        self.assertIn(
            ("_asyncio_Task_get_context_impl", "task_context", "new_bypass"), got
        )
        # The guarded twin two functions away must NOT be reported.
        self.assertNotIn(
            "_asyncio_Task_get_coro_impl", [f["function"] for f in result["findings"]]
        )

    def test_true_negative_real_tp_new_in_the_same_table(self):
        src = self._TASK.replace(
            "{Py_tp_new, PyType_GenericNew},", "{Py_tp_new, Task_new},"
        )
        result = self._findings({"Modules/_asynciomodule.c": src})
        self.assertEqual(
            [f for f in result["findings"] if f["reason"] == "new_bypass"], []
        )

    def test_sibling_type_with_a_real_tp_new_does_not_silence_this_one(self):
        """CPython edge: one file, two types. The kill switch keyed on the
        token, so a sibling's `{Py_tp_new, Other_new}` hid every other type in
        the file — descrobject.c's mappingproxy hid property, and the seven
        _ctypes metatypes hid each other."""
        sibling = """
static PyType_Slot Other_slots[] = {
    {Py_tp_new, Other_new},
    {0, 0},
};

static PyType_Spec Other_spec = {
    .name = "_asyncio.Other",
    .slots = Other_slots,
};
"""
        result = self._findings(
            {"Modules/_asynciomodule.c": self._TASK + sibling}
        )
        self.assertIn(
            "_asyncio_Task_get_context_impl",
            [f["function"] for f in result["findings"]],
        )

    def test_disallow_instantiation_on_the_referencing_spec_protects(self):
        src = self._TASK.replace(
            "Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE",
            "Py_TPFLAGS_DEFAULT | Py_TPFLAGS_DISALLOW_INSTANTIATION",
        )
        result = self._findings({"Modules/_asynciomodule.c": src})
        self.assertEqual(
            [f for f in result["findings"] if f["reason"] == "new_bypass"], []
        )


# ---------------------------------------------------------------------------
# One-hop interprocedural sink + vararg sentinel (issue #28 rules 5 and 6)
# ---------------------------------------------------------------------------
#
# The `super` shape, reduced from Objects/typeobject.c. Three things are being
# tested at once, and all three are properties of the *same* function:
#
#   * `supercheck(su->type, obj)` at :12797 -- the crash is one hop away, in the
#     callee's unguarded `type->tp_name`. The old rule could not see it.
#   * `Py_NewRef(su->type)` at :12806 -- what the old rule DID report, and which
#     control never reaches. CPY-0007's catalog entry has the right lines; the
#     scanner was right by luck.
#   * `PyObject_CallFunctionObjArgs(..., su->type, obj, NULL)` at :12793 -- a
#     NULL here does not crash, it truncates the call and drops `obj`
#     (CPY-0080). It is in the *other* arm of the same `if`, so it must not be
#     treated as dominating anything in the `else`.
SUPER_SHAPE = """\
#include "Python.h"

typedef struct {
    PyObject_HEAD
    PyTypeObject *type;
    PyObject *obj;
} superobject;

static PyTypeObject *
supercheck(PyTypeObject *type, PyObject *obj)
{
    if (PyType_IsSubtype(Py_TYPE(obj), type)) {
        return (PyTypeObject *)Py_NewRef(Py_TYPE(obj));
    }
    PyErr_Format(PyExc_TypeError, "not an instance of %.200s", type->tp_name);
    return NULL;
}

static PyObject *
do_super_lookup(superobject *su, PyTypeObject *su_obj_type, PyObject *name)
{
    if (su_obj_type == NULL) {
        goto skip;
    }
    return _PySuper_LookupDescr(su_obj_type, name);
  skip:
    return PyObject_GenericGetAttr((PyObject *)su, name);
}

static PyObject *
super_descr_get(PyObject *self, PyObject *obj, PyObject *type)
{
    superobject *su = (superobject *)self;
    superobject *newobj;

    if (obj == NULL || su->obj != NULL) {
        return Py_NewRef(self);
    }
    if (!Py_IS_TYPE(su, &PySuper_Type))
        return PyObject_CallFunctionObjArgs((PyObject *)Py_TYPE(su),
                                            su->type, obj, NULL);
    else {
        PyTypeObject *obj_type = supercheck(su->type, obj);
        if (obj_type == NULL)
            return NULL;
        newobj = (superobject *)PySuper_Type.tp_new(&PySuper_Type, NULL, NULL);
        newobj->type = (PyTypeObject *)Py_NewRef(su->type);
        return (PyObject *)newobj;
    }
}

static int
super_init(superobject *su, PyObject *args, PyObject *kwds)
{
    su->type = (PyTypeObject *)Py_NewRef(&PyBaseObject_Type);
    su->obj = NULL;
    return 0;
}

PyTypeObject PySuper_Type = {
    PyVarObject_HEAD_INIT(0, 0)
    "super",
    sizeof(superobject),
    0,
    0,                                          /* tp_dealloc */
    0,                                          /* tp_getattr */
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE,   /* tp_flags */
    super_init,                                 /* tp_init */
    PyType_GenericAlloc,                        /* tp_alloc */
    PyType_GenericNew,                          /* tp_new */
};
"""


class TestOneHopParamSink(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_init_bypass")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _of_kind(self, result, kind):
        return [f for f in result["findings"] if f.get("sink_kind") == kind]

    def test_unguarded_param_sinks_finds_the_callee(self):
        src = SUPER_SHAPE.encode()
        tree = self.mod.parse_bytes(src)
        funcs = self.mod.extract_functions(tree, src)
        sinks = self.mod.unguarded_param_sinks(funcs)
        self.assertIn("supercheck", sinks)
        self.assertIn(0, sinks["supercheck"])

    def test_do_super_lookup_is_the_negative_control(self):
        """It opens with `if (su_obj_type == NULL) goto skip;`, so its
        parameter is guarded and it must not become a sink."""
        src = SUPER_SHAPE.encode()
        tree = self.mod.parse_bytes(src)
        funcs = self.mod.extract_functions(tree, src)
        sinks = self.mod.unguarded_param_sinks(funcs)
        self.assertNotIn(1, sinks.get("do_super_lookup", {}))

    def test_call_site_is_reported_with_the_callee_deref_line(self):
        result = self._findings({"Objects/typeobject.c": SUPER_SHAPE})
        hops = self._of_kind(result, "one_hop_param_deref")
        self.assertEqual(len(hops), 1, hops)
        f = hops[0]
        self.assertEqual(f["function"], "super_descr_get")
        self.assertEqual(f["callee"], "supercheck")
        self.assertEqual(f["callee_param_index"], 0)
        self.assertEqual(f["field"], "type")
        # The callee's deref line, not the call site's.
        self.assertGreater(f["callee_deref_line"], 0)
        self.assertLess(f["callee_deref_line"], f["line"])

    def test_the_callee_deref_line_lands_on_the_deref(self):
        src = SUPER_SHAPE.encode()
        tree = self.mod.parse_bytes(src)
        funcs = self.mod.extract_functions(tree, src)
        line = self.mod.unguarded_param_sinks(funcs)["supercheck"][0]
        # Body offsets are relative to the `{`, not to the signature's first
        # line -- CPython puts the return type on its own line, so using
        # start_line reports one or two lines early.
        self.assertIn("type->tp_name", SUPER_SHAPE.split("\n")[line - 1])

    def test_the_later_sink_is_marked_dominated(self):
        """`Py_NewRef(su->type)` is downstream of the supercheck call on the
        same path, so control never reaches it."""
        result = self._findings({"Objects/typeobject.c": SUPER_SHAPE})
        increfs = self._of_kind(result, "incref")
        self.assertEqual(len(increfs), 1, increfs)
        self.assertIn("dominated_by", increfs[0])
        hop = self._of_kind(result, "one_hop_param_deref")[0]
        self.assertEqual(increfs[0]["dominated_by"], hop["line"])
        self.assertIn(increfs[0]["line"], hop["dominates"])

    def test_a_sibling_branch_sink_is_not_dominated(self):
        """The ObjArgs call is in the other arm of the same `if`. Text-order
        brace counting cannot tell -- CPython writes the braceless
        `if (c) return f(x); else { ... }`."""
        result = self._findings({"Objects/typeobject.c": SUPER_SHAPE})
        varargs = self._of_kind(result, "vararg_sentinel")
        self.assertEqual(len(varargs), 1, varargs)
        self.assertNotIn("dominated_by", varargs[0])
        hop = self._of_kind(result, "one_hop_param_deref")[0]
        self.assertNotIn("dominated_by", hop)


class TestVarargNullTruncation(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_init_bypass")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def test_non_final_null_truncates_the_call(self):
        result = self._findings({"Objects/typeobject.c": SUPER_SHAPE})
        found = [
            f for f in result["findings"] if f["type"] == "vararg_null_truncation"
        ]
        self.assertEqual(len(found), 1, found)
        f = found[0]
        self.assertEqual(f["sink"], "PyObject_CallFunctionObjArgs")
        self.assertEqual(f["argument_index"], 1)
        self.assertEqual(f["arguments_dropped"], ["obj"])
        self.assertEqual(f["confidence"], "medium")

    def test_the_sentinel_position_itself_is_not_flagged(self):
        """A NULL in the *final* slot is the terminator and is correct."""
        src = SUPER_SHAPE.replace(
            "                                            su->type, obj, NULL);",
            "                                            su->type);",
        )
        found = [
            f
            for f in self._findings({"Objects/typeobject.c": src})["findings"]
            if f["type"] == "vararg_null_truncation"
        ]
        self.assertEqual(found, [])

    def test_argument_zero_is_left_to_the_call_sink(self):
        """The callable itself is not a truncation; a NULL there raises."""
        src = SUPER_SHAPE.replace(
            "return PyObject_CallFunctionObjArgs((PyObject *)Py_TYPE(su),\n"
            "                                            su->type, obj, NULL);",
            "return PyObject_CallFunctionObjArgs(su->type, obj, NULL);",
        )
        found = [
            f
            for f in self._findings({"Objects/typeobject.c": src})["findings"]
            if f["type"] == "vararg_null_truncation"
        ]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
