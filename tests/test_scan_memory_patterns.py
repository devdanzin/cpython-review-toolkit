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

    def test_alloc_overflow_bounded_by_existing_allocation_is_safe(self):
        """Objects/call.c:491 and Objects/listobject.c:2985 — the single FP
        class of this rule. An n-element container already occupies n*8 live
        bytes, so `n * sizeof(ptr)` cannot wrap."""
        result = self._findings(
            {
                "Objects/call.c": (
                    "static PyObject *\n"
                    "_PyObject_Call_Prepend(PyObject *callable, PyObject *args)\n"
                    "{\n"
                    "    Py_ssize_t argcount = PyTuple_GET_SIZE(args);\n"
                    "    PyObject **stack = PyMem_Malloc((argcount + 1) * sizeof(PyObject *));\n"
                    "    if (stack == NULL) return PyErr_NoMemory();\n"
                    "    return NULL;\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "list_sort_impl(PyListObject *self)\n"
                    "{\n"
                    "    Py_ssize_t saved_ob_size = Py_SIZE(self);\n"
                    "    PyObject **keys = PyMem_Malloc(sizeof(PyObject *) * saved_ob_size);\n"
                    "    if (keys == NULL) return PyErr_NoMemory();\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "alloc_size_overflow"), [])

    def test_alloc_overflow_two_nonconstant_factors_still_flagged(self):
        """A bounded length multiplied by a *second* non-constant factor can
        still wrap — only `length * <element size>` is dismissed."""
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static int\n"
                    "frob(PyObject *code, int bytes_per_entry)\n"
                    "{\n"
                    "    Py_ssize_t code_len = PyBytes_GET_SIZE(code);\n"
                    "    void *lines = PyMem_Malloc(1 + code_len * bytes_per_entry);\n"
                    "    if (lines == NULL) return -1;\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "alloc_size_overflow")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["confidence"], "low")

    # === Check 1b: varobject_nitems_unguarded =============================

    def test_varobject_nitems_from_type_dict_is_flagged(self):
        """Objects/structseq.c:77 PyStructSequence_New — CONFIRMED, reproduced.

        `size` is read from the type's own (Python-writable) `n_fields` dict
        entry; the multiply happens inside `_PyObject_VAR_SIZE`, so it never
        appears in source and `alloc_size_overflow` cannot see it. Note the
        `size < 0` sign check must NOT count as an overflow guard — `2**62`
        passes it.
        """
        result = self._findings(
            {
                "Objects/structseq.c": (
                    "PyObject *\n"
                    "PyStructSequence_New(PyTypeObject *type)\n"
                    "{\n"
                    "    PyStructSequence *obj;\n"
                    "    Py_ssize_t size = REAL_SIZE_TP(type), i;\n"
                    "    if (size < 0) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    Py_ssize_t vsize = VISIBLE_SIZE_TP(type);\n"
                    "    if (vsize < 0) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    obj = PyObject_GC_NewVar(PyStructSequence, type, size);\n"
                    "    if (obj == NULL)\n"
                    "        return NULL;\n"
                    "    Py_SET_SIZE(obj, vsize);\n"
                    "    for (i = 0; i < size; i++)\n"
                    "        obj->ob_item[i] = NULL;\n"
                    "    return (PyObject*)obj;\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "varobject_nitems_unguarded")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "PyStructSequence_New")
        self.assertEqual(hits[0]["nitems"], "size")

    def test_varobject_tuple_alloc_guarded_twin_is_safe(self):
        """Objects/tupleobject.c:52 tuple_alloc — the `n > MAX/elem` guard, in
        structseq's own base type."""
        result = self._findings(
            {
                "Objects/tupleobject.c": (
                    "static PyTupleObject *\n"
                    "tuple_alloc(Py_ssize_t size)\n"
                    "{\n"
                    "    if ((size_t)size > ((size_t)PY_SSIZE_T_MAX - (sizeof(PyTupleObject) -\n"
                    "                sizeof(PyObject *))) / sizeof(PyObject *)) {\n"
                    "        return (PyTupleObject *)PyErr_NoMemory();\n"
                    "    }\n"
                    "    PyTupleObject *result =\n"
                    "        PyObject_GC_NewVar(PyTupleObject, &PyTuple_Type, size);\n"
                    "    return result;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "varobject_nitems_unguarded"), [])

    def test_varobject_narrow_int_operand_is_safe(self):
        """CPython-specific: Objects/genobject.c / memoryobject.c pass an `int`.

        On LP64 an `int` count cannot make `nitems * tp_itemsize` wrap a 64-bit
        size_t, so these are silent by construction (7 of the 9 var-object
        sites in Objects/ fall to this or to a bound/guard).
        """
        result = self._findings(
            {
                "Objects/genobject.c": (
                    "static PyObject *\n"
                    "make_gen(PyTypeObject *type, PyFunctionObject *func)\n"
                    "{\n"
                    "    PyCodeObject *code = (PyCodeObject *)func->func_code;\n"
                    "    int slots = code->co_nlocalsplus + code->co_stacksize;\n"
                    "    PyGenObject *gen = PyObject_GC_NewVar(PyGenObject, type, slots);\n"
                    "    return (PyObject *)gen;\n"
                    "}\n"
                    "\n"
                    "static PyMemoryViewObject *\n"
                    "memory_alloc(int ndim)\n"
                    "{\n"
                    "    PyMemoryViewObject *mv;\n"
                    "    mv = (PyMemoryViewObject *)\n"
                    "        PyObject_GC_NewVar(PyMemoryViewObject, &PyMemoryView_Type, 3*ndim);\n"
                    "    return mv;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "varobject_nitems_unguarded"), [])

    def test_varobject_bounded_operand_is_safe(self):
        """Objects/codeobject.c:736 — nitems derives from PyBytes_GET_SIZE of an
        object already in memory."""
        result = self._findings(
            {
                "Objects/codeobject.c": (
                    "static PyCodeObject *\n"
                    "_PyCode_New(struct _PyCodeConstructor *con)\n"
                    "{\n"
                    "    Py_ssize_t size = PyBytes_GET_SIZE(con->code) / sizeof(_Py_CODEUNIT);\n"
                    "    PyCodeObject *co;\n"
                    "    co = PyObject_GC_NewVar(PyCodeObject, &PyCode_Type, size);\n"
                    "    return co;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "varobject_nitems_unguarded"), [])

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

    def test_gc_gate_is_type_level_not_file_level(self):
        """Objects/listobject.c:262 PyList_New — the file-level gate's FP.

        `list_dealloc` uses the untracked-tolerant *function*, but
        `listiter_dealloc` — a different type in the same file — uses the
        macro. A file-level gate lets PyList_New through; a type-level gate
        does not.
        """
        result = self._findings(
            {
                "Objects/listobject.c": (
                    "static void\n"
                    "list_dealloc(PyObject *self)\n"
                    "{\n"
                    "    PyListObject *op = (PyListObject *)self;\n"
                    "    PyObject_GC_UnTrack(op);\n"
                    "    PyObject_GC_Del(op);\n"
                    "}\n"
                    "\n"
                    "static void\n"
                    "listiter_dealloc(PyObject *self)\n"
                    "{\n"
                    "    _PyObject_GC_UNTRACK(self);\n"
                    "    PyObject_GC_Del(self);\n"
                    "}\n"
                    "\n"
                    "PyTypeObject PyList_Type = {\n"
                    "    PyVarObject_HEAD_INIT(&PyType_Type, 0)\n"
                    '    "list",\n'
                    "    sizeof(PyListObject),\n"
                    "    0,\n"
                    "    list_dealloc,                               /* tp_dealloc */\n"
                    "};\n"
                    "\n"
                    "PyTypeObject PyListIter_Type = {\n"
                    "    PyVarObject_HEAD_INIT(&PyType_Type, 0)\n"
                    '    "list_iterator",\n'
                    "    sizeof(listiterobject),\n"
                    "    0,\n"
                    "    listiter_dealloc,                           /* tp_dealloc */\n"
                    "};\n"
                    "\n"
                    "PyObject *\n"
                    "PyList_New(Py_ssize_t size)\n"
                    "{\n"
                    "    PyListObject *op = PyObject_GC_New(PyListObject, &PyList_Type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->ob_item = PyMem_Calloc(size, 8);\n"
                    "    if (op->ob_item == NULL) {\n"
                    "        Py_DECREF(op);\n"
                    "        return PyErr_NoMemory();\n"
                    "    }\n"
                    "    _PyObject_GC_TRACK(op);\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "gc_untrack_without_track"), [])

    def test_gc_gate_type_macro_resolution_is_flagged(self):
        """Objects/odictobject.c:1952 odictiter_new — the scanner's one true
        positive, resolved through the positional `/* tp_dealloc */` slot."""
        result = self._findings(
            {
                "Objects/odictobject.c": (
                    "static void\n"
                    "odictiter_dealloc(PyObject *op)\n"
                    "{\n"
                    "    odictiterobject *di = (odictiterobject *)op;\n"
                    "    _PyObject_GC_UNTRACK(di);\n"
                    "    Py_XDECREF(di->di_odict);\n"
                    "    PyObject_GC_Del(di);\n"
                    "}\n"
                    "\n"
                    "PyTypeObject PyODictIter_Type = {\n"
                    "    PyVarObject_HEAD_INIT(&PyType_Type, 0)\n"
                    '    "odict_iterator",\n'
                    "    sizeof(odictiterobject),\n"
                    "    0,\n"
                    "    odictiter_dealloc,                        /* tp_dealloc */\n"
                    "};\n"
                    "\n"
                    "static PyObject *\n"
                    "odictiter_new(PyODictObject *od, int kind)\n"
                    "{\n"
                    "    odictiterobject *di;\n"
                    "    di = PyObject_GC_New(odictiterobject, &PyODictIter_Type);\n"
                    "    if (di == NULL)\n"
                    "        return NULL;\n"
                    "    di->di_result = _PyTuple_FromPairSteal(Py_None, Py_None);\n"
                    "    if (di->di_result == NULL) {\n"
                    "        Py_DECREF(di);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    di->di_odict = (PyODictObject*)Py_NewRef(od);\n"
                    "    _PyObject_GC_TRACK(di);\n"
                    "    return (PyObject *)di;\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "gc_untrack_without_track")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "odictiter_new")
        self.assertEqual(hits[0]["gate"], "type:macro")
        self.assertEqual(hits[0]["tp_dealloc"], "odictiter_dealloc")
        self.assertEqual(hits[0]["confidence"], "medium")

    def test_gc_gate_designated_initializer_resolution(self):
        """Objects/templateobject.c uses `.tp_dealloc = …` — a designated
        initializer — and the untracked-tolerant function, so it is silent even
        though a sibling type in the file uses the macro."""
        result = self._findings(
            {
                "Objects/templateobject.c": (
                    "static void\n"
                    "templateiter_dealloc(PyObject *op)\n"
                    "{\n"
                    "    PyObject_GC_UnTrack(op);\n"
                    "    Py_TYPE(op)->tp_free(op);\n"
                    "}\n"
                    "\n"
                    "static void\n"
                    "other_dealloc(PyObject *op)\n"
                    "{\n"
                    "    _PyObject_GC_UNTRACK(op);\n"
                    "}\n"
                    "\n"
                    "PyTypeObject _PyTemplateIter_Type = {\n"
                    "    PyVarObject_HEAD_INIT(&PyType_Type, 0)\n"
                    '    .tp_name = "string.templatelib.TemplateIter",\n'
                    "    .tp_dealloc = templateiter_dealloc,\n"
                    "};\n"
                    "\n"
                    "static PyObject *\n"
                    "template_iter(PyObject *op)\n"
                    "{\n"
                    "    templateiterobject *iter =\n"
                    "        PyObject_GC_New(templateiterobject, &_PyTemplateIter_Type);\n"
                    "    if (iter == NULL) return NULL;\n"
                    "    iter->stringsiter = PyObject_GetIter(op);\n"
                    "    if (iter->stringsiter == NULL) {\n"
                    "        Py_DECREF(iter);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    PyObject_GC_Track(iter);\n"
                    "    return (PyObject *)iter;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "gc_untrack_without_track"), [])

    def test_gc_gate_unresolvable_type_falls_back_to_file(self):
        """Objects/dictobject.c:5646 dictiter_new — the un-found sibling.

        The type comes in as an `itertype` *parameter*, so type-level
        resolution fails; the file-level fallback keeps it because
        `dictiter_dealloc` does use the macro.
        """
        result = self._findings(
            {
                "Objects/dictobject.c": (
                    "static void\n"
                    "dictiter_dealloc(PyObject *self)\n"
                    "{\n"
                    "    dictiterobject *di = (dictiterobject *)self;\n"
                    "    _PyObject_GC_UNTRACK(di);\n"
                    "    PyObject_GC_Del(di);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "dictiter_new(PyDictObject *dict, PyTypeObject *itertype)\n"
                    "{\n"
                    "    dictiterobject *di;\n"
                    "    di = PyObject_GC_New(dictiterobject, itertype);\n"
                    "    if (di == NULL) return NULL;\n"
                    "    di->di_result = _PyTuple_FromPairSteal(Py_None, Py_None);\n"
                    "    if (di->di_result == NULL) {\n"
                    "        Py_DECREF(di);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    _PyObject_GC_TRACK(di);\n"
                    "    return (PyObject *)di;\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "gc_untrack_without_track")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "dictiter_new")
        self.assertEqual(hits[0]["gate"], "file")
        self.assertEqual(hits[0]["confidence"], "low")

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


# ---------------------------------------------------------------------------
# The tp_alloc slot pointer (issue #28 rule 7)
# ---------------------------------------------------------------------------
#
# `_VAROBJ_NITEMS_INDEX` keyed on the callee's exact name, so every allocation
# written as a virtual dispatch through the slot -- `type->tp_alloc(type, n)` --
# was invisible. That is why the scanner reported zero on
# Objects/typeobject.c, whose var-object sites both go through the slot.
#
# The bottom of that chain has no guard: `_PyType_AllocNoTrack:2521` computes
# `_PyObject_VAR_SIZE(type, nitems+1)` with no PY_SSIZE_T_MAX/itemsize division
# check and no __builtin_mul_overflow, so every caller passing a non-constant
# count really does owe an overflow check.


class TestTpAllocSlotDispatch(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_memory_patterns")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _of_type(self, result, type_name):
        return [f for f in result["findings"] if f["type"] == type_name]

    def test_index_resolution(self):
        f = self.mod._varobj_nitems_index
        self.assertEqual(f("type->tp_alloc"), 1)
        self.assertEqual(f("(*type->tp_alloc)"), 1)
        self.assertEqual(f("base.tp_alloc"), 1)
        self.assertEqual(f("PyType_GenericAlloc"), 1)
        self.assertEqual(f("_PyType_AllocNoTrack"), 1)
        self.assertEqual(f("PyObject_GC_NewVar"), 2)
        self.assertIsNone(f("tp_alloc_helper"))
        self.assertIsNone(f("PyMem_Malloc"))

    def test_slot_pointer_call_is_flagged(self):
        result = self._findings(
            {
                "Objects/typeobject.c": (
                    "static PyObject *\n"
                    "type_from_spec(PyTypeObject *type, PyType_Spec *spec)\n"
                    "{\n"
                    "    Py_ssize_t nmembers = count_members(spec);\n"
                    "    PyObject *res = type->tp_alloc(type, nmembers);\n"
                    "    return res;\n"
                    "}\n"
                )
            }
        )
        hits = self._of_type(result, "varobject_nitems_unguarded")
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["nitems"], "nmembers")
        self.assertEqual(hits[0]["dispatch"], "slot_pointer")

    def test_constant_count_is_still_discharged(self):
        """117 of the 132 slot-pointer sites in the tree pass a literal 0, so
        modelling the slot costs almost nothing."""
        result = self._findings(
            {
                "Objects/enumobject.c": (
                    "static PyObject *\n"
                    "enum_new(PyTypeObject *type)\n"
                    "{\n"
                    "    enumobject *en = (enumobject *)type->tp_alloc(type, 0);\n"
                    "    return (PyObject *)en;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "varobject_nitems_unguarded"), [])

    def test_guard_before_the_slot_call_suppresses(self):
        result = self._findings(
            {
                "Objects/x.c": (
                    "static PyObject *\n"
                    "safe_new(PyTypeObject *type, Py_ssize_t n)\n"
                    "{\n"
                    "    if (n > PY_SSIZE_T_MAX / type->tp_itemsize) {\n"
                    "        return PyErr_NoMemory();\n"
                    "    }\n"
                    "    return type->tp_alloc(type, n);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(self._of_type(result, "varobject_nitems_unguarded"), [])

    def test_census_reports_the_denominator(self):
        """A zero finding count with a zero site count is silence, not safety."""
        result = self._findings(
            {
                "Objects/x.c": (
                    "static PyObject *\n"
                    "a(PyTypeObject *t, Py_ssize_t n)\n"
                    "{\n"
                    "    return t->tp_alloc(t, n);\n"
                    "}\n"
                    "static PyObject *\n"
                    "b(PyTypeObject *t)\n"
                    "{\n"
                    "    return t->tp_alloc(t, 0);\n"
                    "}\n"
                    "static PyObject *\n"
                    "c(PyTypeObject *t, Py_ssize_t n)\n"
                    "{\n"
                    "    return PyType_GenericAlloc(t, n);\n"
                    "}\n"
                )
            }
        )
        census = result["varobject_allocation_census"]
        self.assertEqual(census["sites"], 3)
        self.assertEqual(census["via_slot_pointer"], 2)
        self.assertEqual(census["non_constant_nitems"], 2)

    def test_a_corpus_with_no_allocation_sites_reports_zero_sites(self):
        result = self._findings(
            {"Objects/x.c": "static int f(void)\n{\n    return 0;\n}\n"}
        )
        self.assertEqual(result["varobject_allocation_census"]["sites"], 0)


if __name__ == "__main__":
    unittest.main()
