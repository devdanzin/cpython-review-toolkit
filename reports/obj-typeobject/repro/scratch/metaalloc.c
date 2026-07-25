/* Reachability probe for the coordinator's lead:
 *
 * type_from_slots_or_spec (Objects/typeobject.c:5623) allocates the new type
 * with `metaclass->tp_alloc(metaclass, nmembers)` and then, on any of the
 * post-ownership-transfer `goto finally` paths, hands the half-built object to
 * Py_CLEAR(res) -> the metatype's tp_dealloc (type_dealloc).
 *
 * The function rejects a custom metaclass tp_new at :5562 but places NO
 * constraint on tp_alloc.  Py_tp_alloc IS an accepted spec slot
 * (Include/internal/pycore_slots_generated.h:509 writes ht->ht_type.tp_alloc;
 * Modules/arraymodule.c:3163 uses it in-tree), so an extension can install a
 * metatype allocator that does not zero.
 *
 * `dirty_meta_alloc` below models the _datetimemodule.c time_alloc /
 * datetime_alloc shape ("All data members remain uninitialized trash"):
 * it produces a correctly-linked object whose ht_slots field holds allocator
 * garbage rather than NULL.  type_from_slots_or_spec never writes ht_slots on
 * ANY path, and type_dealloc (Objects/typeobject.c:7034) does
 * Py_XDECREF(et->ht_slots).
 */
#include <Python.h>
#include <stdint.h>

#define TRASH ((PyObject *)(uintptr_t)0xdddddddddddd0001ULL)

static PyObject *
dirty_meta_alloc(PyTypeObject *metatype, Py_ssize_t nitems)
{
    PyObject *obj = PyType_GenericAlloc(metatype, nitems);
    if (obj == NULL) {
        return NULL;
    }
    /* Re-dirty the fields a non-zeroing allocfunc would have left as trash and
     * that type_from_slots_or_spec never assigns. */
    ((PyHeapTypeObject *)obj)->ht_slots = TRASH;
    return obj;
}

static int
meta_traverse(PyObject *self, visitproc visit, void *arg)
{
    return PyType_Type.tp_traverse(self, visit, arg);
}

static void
meta_dealloc(PyObject *self)
{
    fprintf(stderr, "[probe] meta_dealloc self=%p ht_slots=%p -> type_dealloc\n",
            (void *)self, (void *)((PyHeapTypeObject *)self)->ht_slots);
    fflush(stderr);
    PyType_Type.tp_dealloc(self);
}

static PyType_Slot meta_slots[] = {
    {Py_tp_alloc, (void *)dirty_meta_alloc},
    {Py_tp_traverse, (void *)meta_traverse},
    {Py_tp_dealloc, (void *)meta_dealloc},
    {0, NULL},
};

static PyType_Spec meta_spec = {
    "metaalloc.DirtyMeta",
    sizeof(PyHeapTypeObject),
    sizeof(PyMemberDef),
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_BASETYPE | Py_TPFLAGS_HAVE_GC,
    meta_slots,
};

/* Deterministic POST-transfer failure: Py_TPFLAGS_HAVE_GC with no
 * Py_tp_traverse makes type_ready_post_checks (Objects/typeobject.c:9492)
 * return -1, so PyType_Ready fails at :5724 -> `goto finally` -> Py_CLEAR(res).
 * No OOM injection needed. */
static PyType_Slot bad_slots[] = {
    {0, NULL},
};

static PyType_Spec bad_spec = {
    "metaalloc.Bad",
    sizeof(PyObject),
    0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC,
    bad_slots,
};

/* Control: the same metaclass driving a spec that succeeds, to show the
 * metaclass itself is constructible and usable. */
static PyType_Slot ok_slots[] = {
    {0, NULL},
};

static PyType_Spec ok_spec = {
    "metaalloc.Ok",
    sizeof(PyObject),
    0,
    Py_TPFLAGS_DEFAULT,
    ok_slots,
};

static PyObject *
make_meta(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    return PyType_FromSpecWithBases(&meta_spec, (PyObject *)&PyType_Type);
}

static PyObject *
drive_error_path(PyObject *self, PyObject *meta)
{
    if (!PyType_Check(meta)) {
        PyErr_SetString(PyExc_TypeError, "need a metaclass");
        return NULL;
    }
    fprintf(stderr, "[probe] calling PyType_FromMetaclass on the failing spec\n");
    fflush(stderr);
    PyObject *t = PyType_FromMetaclass((PyTypeObject *)meta, NULL, &bad_spec, NULL);
    fprintf(stderr, "[probe] returned %p (survived type_dealloc)\n", (void *)t);
    fflush(stderr);
    if (t == NULL) {
        PyObject *e = PyErr_GetRaisedException();
        fprintf(stderr, "[probe] failure exception: %s\n",
                e ? Py_TYPE(e)->tp_name : "(none!)");
        if (e) {
            PyObject *s = PyObject_Str(e);
            if (s) {
                fprintf(stderr, "[probe]   %s\n", PyUnicode_AsUTF8(s));
                Py_DECREF(s);
            }
            Py_DECREF(e);
        }
        fflush(stderr);
        Py_RETURN_NONE;
    }
    return t;
}

static PyObject *
drive_success_path(PyObject *self, PyObject *meta)
{
    if (!PyType_Check(meta)) {
        PyErr_SetString(PyExc_TypeError, "need a metaclass");
        return NULL;
    }
    return PyType_FromMetaclass((PyTypeObject *)meta, NULL, &ok_spec, NULL);
}

/* Quiet variant for the OOM sweep: no allocation of its own beyond the
 * PyType_FromMetaclass call itself. */
static PyObject *
probe(PyObject *self, PyObject *meta)
{
    PyObject *t = PyType_FromMetaclass((PyTypeObject *)meta, NULL, &bad_spec, NULL);
    Py_XDECREF(t);
    PyErr_Clear();
    Py_RETURN_NONE;
}

static PyMethodDef methods[] = {
    {"probe", probe, METH_O, NULL},
    {"make_meta", make_meta, METH_NOARGS, NULL},
    {"drive_error_path", drive_error_path, METH_O, NULL},
    {"drive_success_path", drive_success_path, METH_O, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef moddef = {
    PyModuleDef_HEAD_INIT, "metaalloc", NULL, -1, methods,
};

PyMODINIT_FUNC
PyInit_metaalloc(void)
{
    return PyModule_Create(&moddef);
}
