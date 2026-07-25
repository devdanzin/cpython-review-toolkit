/* Recall canary for scan_init_bypass.
 *
 * Case A: the bytearray/super shape -- tp_init present, tp_new = PyType_GenericNew.
 *         This IS bypassable and MUST be reported.
 * Case B: the dict/set shape -- tp_init present, tp_new = a real constructor.
 *         Not bypassable; must stay silent.
 * Case C: the dict_keyiterator shape -- static PyTypeObject, tp_new omitted
 *         (== 0) and tp_base omitted (== object).  type_ready_set_new() adds
 *         Py_TPFLAGS_DISALLOW_INSTANTIATION implicitly, so this is NOT
 *         bypassable -- but the scanner has no tp_base model, so it should
 *         (wrongly) fire here.
 */

typedef struct {
    PyObject_HEAD
    PyObject *ca_field;
} canaryAObject;

static int
canarya_init(PyObject *self, PyObject *args, PyObject *kwds)
{
    canaryAObject *a = (canaryAObject *)self;
    a->ca_field = Py_None;
    return 0;
}

static PyObject *
canarya_get(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    canaryAObject *a = (canaryAObject *)self;
    Py_INCREF(a->ca_field);
    return a->ca_field;
}

PyTypeObject CanaryA_Type = {
    PyVarObject_HEAD_INIT(&PyType_Type, 0)
    "canaryA",                                  /* tp_name */
    sizeof(canaryAObject),                      /* tp_basicsize */
    0,                                          /* tp_itemsize */
    0,                                          /* tp_dealloc */
    0,                                          /* tp_vectorcall_offset */
    0,                                          /* tp_getattr */
    0,                                          /* tp_setattr */
    0,                                          /* tp_as_async */
    0,                                          /* tp_repr */
    0,                                          /* tp_as_number */
    0,                                          /* tp_as_sequence */
    0,                                          /* tp_as_mapping */
    0,                                          /* tp_hash */
    0,                                          /* tp_call */
    0,                                          /* tp_str */
    0,                                          /* tp_getattro */
    0,                                          /* tp_setattro */
    0,                                          /* tp_as_buffer */
    Py_TPFLAGS_DEFAULT,                         /* tp_flags */
    0,                                          /* tp_doc */
    0,                                          /* tp_traverse */
    0,                                          /* tp_clear */
    0,                                          /* tp_richcompare */
    0,                                          /* tp_weaklistoffset */
    0,                                          /* tp_iter */
    0,                                          /* tp_iternext */
    0,                                          /* tp_methods */
    0,                                          /* tp_members */
    0,                                          /* tp_getset */
    0,                                          /* tp_base */
    0,                                          /* tp_dict */
    0,                                          /* tp_descr_get */
    0,                                          /* tp_descr_set */
    0,                                          /* tp_dictoffset */
    canarya_init,                               /* tp_init */
    PyType_GenericAlloc,                        /* tp_alloc */
    PyType_GenericNew,                          /* tp_new */
    PyObject_Del,                               /* tp_free */
};

typedef struct {
    PyObject_HEAD
    PyObject *cb_field;
} canaryBObject;

static int
canaryb_init(PyObject *self, PyObject *args, PyObject *kwds)
{
    canaryBObject *b = (canaryBObject *)self;
    b->cb_field = Py_None;
    return 0;
}

static PyObject *
canaryb_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    canaryBObject *b = (canaryBObject *)type->tp_alloc(type, 0);
    if (b == NULL) {
        return NULL;
    }
    b->cb_field = Py_None;
    return (PyObject *)b;
}

static PyObject *
canaryb_get(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    canaryBObject *b = (canaryBObject *)self;
    Py_INCREF(b->cb_field);
    return b->cb_field;
}

PyTypeObject CanaryB_Type = {
    PyVarObject_HEAD_INIT(&PyType_Type, 0)
    "canaryB",                                  /* tp_name */
    sizeof(canaryBObject),                      /* tp_basicsize */
    0,                                          /* tp_itemsize */
    0,                                          /* tp_dealloc */
    0,                                          /* tp_vectorcall_offset */
    0,                                          /* tp_getattr */
    0,                                          /* tp_setattr */
    0,                                          /* tp_as_async */
    0,                                          /* tp_repr */
    0,                                          /* tp_as_number */
    0,                                          /* tp_as_sequence */
    0,                                          /* tp_as_mapping */
    0,                                          /* tp_hash */
    0,                                          /* tp_call */
    0,                                          /* tp_str */
    0,                                          /* tp_getattro */
    0,                                          /* tp_setattro */
    0,                                          /* tp_as_buffer */
    Py_TPFLAGS_DEFAULT,                         /* tp_flags */
    0,                                          /* tp_doc */
    0,                                          /* tp_traverse */
    0,                                          /* tp_clear */
    0,                                          /* tp_richcompare */
    0,                                          /* tp_weaklistoffset */
    0,                                          /* tp_iter */
    0,                                          /* tp_iternext */
    0,                                          /* tp_methods */
    0,                                          /* tp_members */
    0,                                          /* tp_getset */
    0,                                          /* tp_base */
    0,                                          /* tp_dict */
    0,                                          /* tp_descr_get */
    0,                                          /* tp_descr_set */
    0,                                          /* tp_dictoffset */
    canaryb_init,                               /* tp_init */
    PyType_GenericAlloc,                        /* tp_alloc */
    canaryb_new,                                /* tp_new */
    PyObject_Del,                               /* tp_free */
};

typedef struct {
    PyObject_HEAD
    PyObject *cc_field;
} canaryCObject;

static int
canaryc_init(PyObject *self, PyObject *args, PyObject *kwds)
{
    canaryCObject *c = (canaryCObject *)self;
    c->cc_field = Py_None;
    return 0;
}

static PyObject *
canaryc_get(PyObject *self, PyObject *Py_UNUSED(ignored))
{
    canaryCObject *c = (canaryCObject *)self;
    Py_INCREF(c->cc_field);
    return c->cc_field;
}

PyTypeObject CanaryC_Type = {
    PyVarObject_HEAD_INIT(&PyType_Type, 0)
    "canaryC",                                  /* tp_name */
    sizeof(canaryCObject),                      /* tp_basicsize */
    0,                                          /* tp_itemsize */
    0,                                          /* tp_dealloc */
    0,                                          /* tp_vectorcall_offset */
    0,                                          /* tp_getattr */
    0,                                          /* tp_setattr */
    0,                                          /* tp_as_async */
    0,                                          /* tp_repr */
    0,                                          /* tp_as_number */
    0,                                          /* tp_as_sequence */
    0,                                          /* tp_as_mapping */
    0,                                          /* tp_hash */
    0,                                          /* tp_call */
    0,                                          /* tp_str */
    0,                                          /* tp_getattro */
    0,                                          /* tp_setattro */
    0,                                          /* tp_as_buffer */
    Py_TPFLAGS_DEFAULT,                         /* tp_flags */
    0,                                          /* tp_doc */
    0,                                          /* tp_traverse */
    0,                                          /* tp_clear */
    0,                                          /* tp_richcompare */
    0,                                          /* tp_weaklistoffset */
    0,                                          /* tp_iter */
    0,                                          /* tp_iternext */
    0,                                          /* tp_methods */
    0,                                          /* tp_members */
    0,                                          /* tp_getset */
    0,                                          /* tp_base */
    0,                                          /* tp_dict */
    0,                                          /* tp_descr_get */
    0,                                          /* tp_descr_set */
    0,                                          /* tp_dictoffset */
    canaryc_init,                               /* tp_init */
    PyType_GenericAlloc,                        /* tp_alloc */
    0,                                          /* tp_new */
    PyObject_Del,                               /* tp_free */
};
