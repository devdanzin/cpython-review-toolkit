"""Tests for scan_uninit_dealloc.py — freeing a half-constructed object."""

import unittest

from helpers import TempProject, import_script

# A destructor that decrefs the members, so the pointer-member filter has
# in-file evidence. Reused by several fixtures.
TEMPLATE_DEALLOC = (
    "static int\n"
    "templateiter_clear(PyObject *op)\n"
    "{\n"
    "    templateiterobject *self = (templateiterobject *)op;\n"
    "    Py_CLEAR(self->stringsiter);\n"
    "    Py_CLEAR(self->interpolationsiter);\n"
    "    return 0;\n"
    "}\n"
)


class TestScanUninitDealloc(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_uninit_dealloc")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _uninit(self, files):
        return [
            f
            for f in self._findings(files)["findings"]
            if f["type"] == "dealloc_of_uninitialized_object"
        ]

    # --- true positives ----------------------------------------------------

    def test_template_iter_shape_is_flagged(self):
        """gh-151815: Objects/templateobject.c:225 template_iter.

        Two fallible PyObject_GetIter calls Py_DECREF the iterator before both
        member pointers are assigned; templateiter_clear then Py_CLEARs
        garbage. Still present at 3.16.0a0.
        """
        findings = self._uninit(
            {
                "Objects/templateobject.c": (
                    "static PyObject *\n"
                    "template_iter(PyObject *op)\n"
                    "{\n"
                    "    templateobject *self = (templateobject *)op;\n"
                    "    templateiterobject *iter =\n"
                    "        PyObject_GC_New(templateiterobject, &_PyTemplateIter_Type);\n"
                    "    if (iter == NULL) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    PyObject *stringsiter = PyObject_GetIter(self->strings);\n"
                    "    if (stringsiter == NULL) {\n"
                    "        Py_DECREF(iter);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    PyObject *isiter = PyObject_GetIter(self->interpolations);\n"
                    "    if (isiter == NULL) {\n"
                    "        Py_DECREF(iter);\n"
                    "        Py_DECREF(stringsiter);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    iter->stringsiter = stringsiter;\n"
                    "    iter->interpolationsiter = isiter;\n"
                    "    iter->from_strings = 1;\n"
                    "    PyObject_GC_Track(iter);\n"
                    "    return (PyObject *)iter;\n"
                    "}\n" + TEMPLATE_DEALLOC
                )
            }
        )
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["function"], "template_iter")
        self.assertEqual(f["variable"], "iter")
        self.assertEqual(f["allocator"], "PyObject_GC_New")
        self.assertEqual(f["confidence"], "medium")
        self.assertEqual(f["unset_members"], ["interpolationsiter", "stringsiter"])
        # The first early free is the one reported.
        self.assertLess(f["line"], f["free_line"])

    def test_odictiter_new_shape_is_flagged(self):
        """Objects/odictobject.c:1945 odictiter_new — reproduced (K=1 SIGABRT).

        The fallible _PyTuple_FromPairSteal runs *first*; kind / di_current /
        di_odict are written only afterwards, and odictiter_dealloc opens with
        the unchecked _PyObject_GC_UNTRACK macro.
        """
        findings = self._uninit(
            {
                "Objects/odictobject.c": (
                    "static void\n"
                    "odictiter_dealloc(PyObject *op)\n"
                    "{\n"
                    "    odictiterobject *di = (odictiterobject *)op;\n"
                    "    _PyObject_GC_UNTRACK(di);\n"
                    "    Py_XDECREF(di->di_odict);\n"
                    "    Py_XDECREF(di->di_current);\n"
                    "    if ((di->kind & _odict_ITER_ITEMS) == _odict_ITER_ITEMS) {\n"
                    "        Py_DECREF(di->di_result);\n"
                    "    }\n"
                    "    PyObject_GC_Del(di);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "odictiter_new(PyODictObject *od, int kind)\n"
                    "{\n"
                    "    odictiterobject *di;\n"
                    "    _ODictNode *node;\n"
                    "    int reversed = kind & _odict_ITER_REVERSED;\n"
                    "\n"
                    "    di = PyObject_GC_New(odictiterobject, &PyODictIter_Type);\n"
                    "    if (di == NULL)\n"
                    "        return NULL;\n"
                    "\n"
                    "    if ((kind & _odict_ITER_ITEMS) == _odict_ITER_ITEMS) {\n"
                    "        di->di_result = _PyTuple_FromPairSteal(Py_None, Py_None);\n"
                    "        if (di->di_result == NULL) {\n"
                    "            Py_DECREF(di);\n"
                    "            return NULL;\n"
                    "        }\n"
                    "    }\n"
                    "    else {\n"
                    "        di->di_result = NULL;\n"
                    "    }\n"
                    "\n"
                    "    di->kind = kind;\n"
                    "    node = reversed ? _odict_LAST(od) : _odict_FIRST(od);\n"
                    "    di->di_current = node ? Py_NewRef(_odictnode_KEY(node)) : NULL;\n"
                    "    di->di_odict = (PyODictObject*)Py_NewRef(od);\n"
                    "\n"
                    "    _PyObject_GC_TRACK(di);\n"
                    "    return (PyObject *)di;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["function"], "odictiter_new")
        # di_result is written before the free and must not be reported;
        # kind / di_current / di_odict are the garbage ones.
        self.assertNotIn("di_result", f["unset_members"])
        self.assertIn("di_odict", f["unset_members"])
        self.assertIn("di_current", f["unset_members"])

    def test_pylist_new_free_threaded_branch_is_flagged(self):
        """Objects/listobject.c:250 PyList_New — the branch-insensitivity bug.

        `op->ob_item = NULL` lives in the `size <= 0` arm; the Py_DECREF(op)
        lives in the sibling `else` arm under `#ifdef Py_GIL_DISABLED`. The old
        flat-text `= NULL before the free` gate dismissed the whole function.
        """
        findings = self._uninit(
            {
                "Objects/listobject.c": (
                    "PyObject *\n"
                    "PyList_New(Py_ssize_t size)\n"
                    "{\n"
                    "    PyListObject *op = _Py_FREELIST_POP(PyListObject, lists);\n"
                    "    if (op == NULL) {\n"
                    "        op = PyObject_GC_New(PyListObject, &PyList_Type);\n"
                    "        if (op == NULL) {\n"
                    "            return NULL;\n"
                    "        }\n"
                    "    }\n"
                    "    if (size <= 0) {\n"
                    "        op->ob_item = NULL;\n"
                    "    }\n"
                    "    else {\n"
                    "#ifdef Py_GIL_DISABLED\n"
                    "        _PyListArray *array = list_allocate_array(size);\n"
                    "        if (array == NULL) {\n"
                    "            Py_DECREF(op);\n"
                    "            return PyErr_NoMemory();\n"
                    "        }\n"
                    "        op->ob_item = array->ob_item;\n"
                    "#else\n"
                    "        op->ob_item = (PyObject **) PyMem_Calloc(size, 8);\n"
                    "#endif\n"
                    "        if (op->ob_item == NULL) {\n"
                    "            Py_DECREF(op);\n"
                    "            return PyErr_NoMemory();\n"
                    "        }\n"
                    "    }\n"
                    "    op->allocated = size;\n"
                    "    _PyObject_GC_TRACK(op);\n"
                    "    return (PyObject *) op;\n"
                    "}\n"
                    "\n"
                    "static void\n"
                    "list_dealloc(PyObject *self)\n"
                    "{\n"
                    "    PyListObject *op = (PyListObject *)self;\n"
                    "    Py_ssize_t i;\n"
                    "    PyObject_GC_UnTrack(op);\n"
                    "    if (op->ob_item != NULL) {\n"
                    "        i = Py_SIZE(op);\n"
                    "        while (--i >= 0) {\n"
                    "            Py_XDECREF(op->ob_item[i]);\n"
                    "        }\n"
                    "        free_list_items(op->ob_item, false);\n"
                    "    }\n"
                    "    PyObject_GC_Del(op);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["function"], "PyList_New")
        self.assertEqual(f["unset_members"], ["ob_item"])

    def test_destructor_read_scalar_is_flagged_low(self):
        """Modules/_elementtree.c:2367 — the blake2 (gh-152851) shape.

        The uninitialized member is a *scalar* loop bound, not a pointer, but
        the destructor reads it to drive Py_XDECREFs. Reported at `low`.
        """
        findings = self._uninit(
            {
                "Modules/_elementtree.c": (
                    "static void\n"
                    "elementiter_dealloc(PyObject *op)\n"
                    "{\n"
                    "    ElementIterObject *it = (ElementIterObject *)op;\n"
                    "    Py_ssize_t i = it->parent_stack_used;\n"
                    "    PyObject_GC_UnTrack(it);\n"
                    "    while (i--)\n"
                    "        Py_XDECREF(it->parent_stack[i].parent);\n"
                    "    PyMem_Free(it->parent_stack);\n"
                    "    Py_XDECREF(it->sought_tag);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "create_elementiter(ElementObject *self, PyObject *tag)\n"
                    "{\n"
                    "    ElementIterObject *it;\n"
                    "    it = PyObject_GC_New(ElementIterObject, &ElementIter_Type);\n"
                    "    if (!it)\n"
                    "        return NULL;\n"
                    "    it->sought_tag = Py_NewRef(tag);\n"
                    "    it->parent_stack = PyMem_New(ParentLocator, 8);\n"
                    "    if (it->parent_stack == NULL) {\n"
                    "        Py_DECREF(it);\n"
                    "        return PyErr_NoMemory();\n"
                    "    }\n"
                    "    it->parent_stack_used = 0;\n"
                    "    PyObject_GC_Track(it);\n"
                    "    return (PyObject *)it;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["confidence"], "low")
        self.assertEqual(f["unset_members"], ["parent_stack_used"])
        self.assertEqual(
            f["destructor_evidence"]["parent_stack_used"]["kind"],
            "destructor_read",
        )

    # --- true negatives: the guarded twins ---------------------------------

    def test_dictiter_new_guarded_twin_is_not_flagged(self):
        """Objects/dictobject.c:5617 dictiter_new — the function odictiter_new
        was copied from, with the fallible call placed *last*."""
        findings = self._uninit(
            {
                "Objects/dictobject.c": (
                    "static void\n"
                    "dictiter_dealloc(PyObject *self)\n"
                    "{\n"
                    "    dictiterobject *di = (dictiterobject *)self;\n"
                    "    _PyObject_GC_UNTRACK(di);\n"
                    "    Py_XDECREF(di->di_dict);\n"
                    "    Py_XDECREF(di->di_result);\n"
                    "    PyObject_GC_Del(di);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "dictiter_new(PyDictObject *dict, PyTypeObject *itertype)\n"
                    "{\n"
                    "    Py_ssize_t used;\n"
                    "    dictiterobject *di;\n"
                    "    di = PyObject_GC_New(dictiterobject, itertype);\n"
                    "    if (di == NULL) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    di->di_dict = (PyDictObject*)Py_NewRef(dict);\n"
                    "    used = GET_USED(dict);\n"
                    "    di->di_used = used;\n"
                    "    di->len = used;\n"
                    "    if (itertype == &PyDictIterItem_Type) {\n"
                    "        di->di_result = _PyTuple_FromPairSteal(Py_None, Py_None);\n"
                    "        if (di->di_result == NULL) {\n"
                    "            Py_DECREF(di);\n"
                    "            return NULL;\n"
                    "        }\n"
                    "    }\n"
                    "    else {\n"
                    "        di->di_result = NULL;\n"
                    "    }\n"
                    "    _PyObject_GC_TRACK(di);\n"
                    "    return (PyObject *)di;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_structseq_new_guarded_twin_is_not_flagged(self):
        """Objects/structseq.c:65 PyStructSequence_New NULLs every one of
        n_fields slots before anything fallible — a silent correct negative."""
        findings = self._uninit(
            {
                "Objects/structseq.c": (
                    "static void\n"
                    "structseq_dealloc(PyObject *op)\n"
                    "{\n"
                    "    PyStructSequence *obj = (PyStructSequence *)op;\n"
                    "    Py_ssize_t i, size = REAL_SIZE(op);\n"
                    "    for (i = 0; i < size; ++i) {\n"
                    "        Py_XDECREF(obj->ob_item[i]);\n"
                    "    }\n"
                    "    PyObject_GC_Del(obj);\n"
                    "}\n"
                    "\n"
                    "PyObject *\n"
                    "PyStructSequence_New(PyTypeObject *type)\n"
                    "{\n"
                    "    PyStructSequence *obj;\n"
                    "    Py_ssize_t size = REAL_SIZE_TP(type), i;\n"
                    "    if (size < 0) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    obj = PyObject_GC_NewVar(PyStructSequence, type, size);\n"
                    "    if (obj == NULL)\n"
                    "        return NULL;\n"
                    "    Py_SET_SIZE(obj, size);\n"
                    "    for (i = 0; i < size; i++)\n"
                    "        obj->ob_item[i] = NULL;\n"
                    "    return (PyObject*)obj;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_scalar_only_members_are_not_flagged(self):
        """Modules/_sre/sre.c:2955 pattern_new_match — the members left unset
        (pos / endpos / lastindex) are Py_ssize_t scalars the destructor never
        looks at."""
        findings = self._uninit(
            {
                "Modules/_sre/sre.c": (
                    "static int\n"
                    "match_clear(PyObject *op)\n"
                    "{\n"
                    "    MatchObject *self = (MatchObject *)op;\n"
                    "    Py_CLEAR(self->string);\n"
                    "    Py_CLEAR(self->regs);\n"
                    "    return 0;\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "pattern_new_match(PatternObject *pattern, SRE_STATE *state)\n"
                    "{\n"
                    "    MatchObject *match;\n"
                    "    match = PyObject_GC_NewVar(MatchObject, &Match_Type, 2);\n"
                    "    if (!match)\n"
                    "        return NULL;\n"
                    "    match->string = Py_NewRef(state->string);\n"
                    "    match->regs = PyTuple_New(2);\n"
                    "    if (match->regs == NULL) {\n"
                    "        Py_DECREF(match);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    match->pos = state->pos;\n"
                    "    match->endpos = state->endpos;\n"
                    "    match->lastindex = state->lastindex;\n"
                    "    return (PyObject *)match;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_shared_fail_label_is_not_flagged(self):
        """Objects/typeobject.c:11343 slot_bf_getbuffer — the Py_XDECREF sits on
        a shared `fail:` label with no member write after it."""
        findings = self._uninit(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *op)\n"
                    "{\n"
                    "    FooObject *f = (FooObject *)op;\n"
                    "    Py_XDECREF(f->attr);\n"
                    "    Py_XDECREF(f->other);\n"
                    "}\n"
                    "\n"
                    "static int\n"
                    "foo_new(PyTypeObject *type, PyObject *arg)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return -1;\n"
                    "    op->attr = PyObject_GetIter(arg);\n"
                    "    if (op->attr == NULL) goto fail;\n"
                    "    op->other = PyList_New(0);\n"
                    "    if (op->other == NULL) goto fail;\n"
                    "    return 0;\n"
                    "fail:\n"
                    "    Py_XDECREF(op);\n"
                    "    return -1;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_nonnull_sentinel_init_is_not_flagged(self):
        """Objects/bytearrayobject.c:164 — the guard is `new->ob_exports = 0;`,
        not `= NULL`; the old `= NULL`-only gate class."""
        findings = self._uninit(
            {
                "Objects/bytearrayobject.c": (
                    "static void\n"
                    "bytearray_dealloc(PyObject *op)\n"
                    "{\n"
                    "    PyByteArrayObject *self = (PyByteArrayObject *)op;\n"
                    "    Py_XDECREF(self->ob_bytes_object);\n"
                    "}\n"
                    "\n"
                    "PyObject *\n"
                    "PyByteArray_FromStringAndSize(const char *bytes, Py_ssize_t size)\n"
                    "{\n"
                    "    PyByteArrayObject *new;\n"
                    "    new = PyObject_New(PyByteArrayObject, &PyByteArray_Type);\n"
                    "    if (new == NULL)\n"
                    "        return NULL;\n"
                    "    new->ob_exports = 0;\n"
                    "    new->ob_bytes_object = PyBytes_FromStringAndSize(bytes, size);\n"
                    "    if (new->ob_bytes_object == NULL) {\n"
                    "        Py_DECREF(new);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    return (PyObject *)new;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_plain_ifdef_write_dominates_later_free(self):
        """CPython-specific: Objects/codeobject.c:736 _PyCode_New.

        `co->_co_unique_id = …` sits in a plain `#ifdef Py_GIL_DISABLED` block
        with no `#else`. Only one arm of a preprocessor conditional is ever
        compiled, so the write *does* dominate the later free — flagging it is
        a false positive.
        """
        findings = self._uninit(
            {
                "Objects/codeobject.c": (
                    "static void\n"
                    "code_dealloc(PyObject *self)\n"
                    "{\n"
                    "    PyCodeObject *co = (PyCodeObject *)self;\n"
                    "    Py_XDECREF(co->co_consts);\n"
                    "#ifdef Py_GIL_DISABLED\n"
                    "    assert(co->_co_unique_id == _Py_INVALID_UNIQUE_ID);\n"
                    "#endif\n"
                    "}\n"
                    "\n"
                    "static PyCodeObject *\n"
                    "_PyCode_New(struct _PyCodeConstructor *con)\n"
                    "{\n"
                    "    Py_ssize_t size = 4;\n"
                    "    PyCodeObject *co;\n"
                    "    co = PyObject_GC_NewVar(PyCodeObject, &PyCode_Type, size);\n"
                    "    if (co == NULL) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "#ifdef Py_GIL_DISABLED\n"
                    "    co->_co_unique_id = _Py_INVALID_UNIQUE_ID;\n"
                    "#endif\n"
                    "    if (init_code(co, con) < 0) {\n"
                    "        Py_DECREF(co);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "#ifdef Py_GIL_DISABLED\n"
                    "    co->_co_unique_id = _PyObject_AssignUniqueId((PyObject *)co);\n"
                    "#endif\n"
                    "    return co;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_memset_zeroed_is_safe(self):
        findings = self._uninit(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *op)\n"
                    "{\n"
                    "    Py_XDECREF(((FooObject *)op)->attr);\n"
                    "    Py_XDECREF(((FooObject *)op)->other);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    memset(op, 0, sizeof(*op));\n"
                    "    op->attr = PyList_New(0);\n"
                    "    if (op->attr == NULL) { Py_DECREF(op); return NULL; }\n"
                    "    op->other = PyList_New(0);\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_members_nulled_before_error_is_safe(self):
        findings = self._uninit(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *op)\n"
                    "{\n"
                    "    Py_XDECREF(((FooObject *)op)->attr);\n"
                    "    Py_XDECREF(((FooObject *)op)->other);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = NULL;\n"
                    "    op->other = NULL;\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) { Py_DECREF(op); return NULL; }\n"
                    "    op->other = PyList_New(0);\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_zeroing_allocator_is_safe(self):
        # tp_alloc / PyType_GenericAlloc zero the object; not flagged.
        findings = self._uninit(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *op)\n"
                    "{\n"
                    "    Py_XDECREF(((FooObject *)op)->attr);\n"
                    "    Py_XDECREF(((FooObject *)op)->other);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    FooObject *op = (FooObject *)type->tp_alloc(type, 0);\n"
                    "    if (op == NULL) return NULL;\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) { Py_DECREF(op); return NULL; }\n"
                    "    op->other = PyList_New(0);\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

    def test_no_early_free_is_safe(self):
        # Object never freed inside the constructor -> nothing to flag.
        findings = self._uninit(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *op)\n"
                    "{\n"
                    "    Py_XDECREF(((FooObject *)op)->attr);\n"
                    "}\n"
                    "\n"
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
        self.assertEqual(findings, [])

    def test_comment_suppression(self):
        findings = self._uninit(
            {
                "Objects/foo.c": (
                    "static void\n"
                    "foo_dealloc(PyObject *op)\n"
                    "{\n"
                    "    Py_XDECREF(((FooObject *)op)->attr);\n"
                    "    Py_XDECREF(((FooObject *)op)->other);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "foo_new(PyTypeObject *type)\n"
                    "{\n"
                    "    /* intentional: dealloc handles the uninitialized case */\n"
                    "    FooObject *op = PyObject_GC_New(FooObject, type);\n"
                    "    op->attr = PyObject_GetIter(type);\n"
                    "    if (op->attr == NULL) { Py_DECREF(op); return NULL; }\n"
                    "    op->other = PyList_New(0);\n"
                    "    return (PyObject *)op;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(findings, [])

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
