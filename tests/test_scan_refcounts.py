"""Tests for scan_refcounts.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_refcounts")


def _types(result, kind):
    return [f for f in result["findings"] if f["type"] == kind]


def _scan(c_code, path="Objects/test.c"):
    with TempProject({path: c_code}) as root:
        return mod.analyze(str(root))


# ---------------------------------------------------------------------------
# stale_slot_decref -- the Objects/iterobject.c crown jewel
# ---------------------------------------------------------------------------

# The real shape, transcribed from Objects/iterobject.c iter_iternext: a
# borrowed slot load, a call that runs arbitrary Python, then the slot cleared
# and the *stale local* dropped.
ITEROBJECT_SHAPE = (
    "static PyObject *\n"
    "iter_iternext(PyObject *iterator)\n"
    "{\n"
    "    seqiterobject *it;\n"
    "    PyObject *seq;\n"
    "    PyObject *result;\n"
    "\n"
    "    it = (seqiterobject *)iterator;\n"
    "    seq = it->it_seq;\n"
    "    if (seq == NULL)\n"
    "        return NULL;\n"
    "\n"
    "    result = PySequence_GetItem(seq, it->it_index);\n"
    "    if (result != NULL) {\n"
    "        it->it_index++;\n"
    "        return result;\n"
    "    }\n"
    "    if (PyErr_ExceptionMatches(PyExc_IndexError)) {\n"
    "        PyErr_Clear();\n"
    "        it->it_seq = NULL;\n"
    "        Py_DECREF(seq);\n"
    "    }\n"
    "    return NULL;\n"
    "}\n"
)

# The guarded twin, ~165 lines below the bug in the same real file:
# calliter_iternext uses Py_CLEAR, which re-reads the field.
CALLITER_SHAPE = (
    "static PyObject *\n"
    "calliter_iternext(PyObject *op)\n"
    "{\n"
    "    calliterobject *it = (calliterobject *)op;\n"
    "    PyObject *result;\n"
    "\n"
    "    if (it->it_callable == NULL) {\n"
    "        return NULL;\n"
    "    }\n"
    "    result = _PyObject_CallNoArgs(it->it_callable);\n"
    "    if (result != NULL && it->it_sentinel != NULL) {\n"
    "        int ok;\n"
    "        ok = PyObject_RichCompareBool(it->it_sentinel, result, Py_EQ);\n"
    "        if (ok == 0) {\n"
    "            return result;\n"
    "        }\n"
    "        if (ok > 0) {\n"
    "            Py_CLEAR(it->it_callable);\n"
    "            Py_CLEAR(it->it_sentinel);\n"
    "        }\n"
    "    }\n"
    "    Py_XDECREF(result);\n"
    "    return NULL;\n"
    "}\n"
)


class TestStaleSlotDecref(unittest.TestCase):
    """The Objects/iterobject.c:80 double-DECREF shape."""

    def test_true_positive_iterobject_shape(self):
        result = _scan(ITEROBJECT_SHAPE)
        found = _types(result, "stale_slot_decref")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["variable"], "seq")
        self.assertEqual(found[0]["function"], "iter_iternext")
        self.assertEqual(found[0]["confidence"], "high")
        self.assertIn("Py_CLEAR(it->it_seq)", found[0]["detail"])

    def test_true_negative_py_clear_guarded_twin(self):
        result = _scan(CALLITER_SHAPE)
        self.assertEqual(_types(result, "stale_slot_decref"), [])

    def test_both_in_one_file_flags_only_the_bug(self):
        """The real file holds both; only iter_iternext may be reported."""
        result = _scan(ITEROBJECT_SHAPE + "\n" + CALLITER_SHAPE)
        found = _types(result, "stale_slot_decref")
        self.assertEqual([f["function"] for f in found], ["iter_iternext"])

    def test_reports_the_decref_line_not_the_load_line(self):
        result = _scan(ITEROBJECT_SHAPE)
        found = _types(result, "stale_slot_decref")[0]
        source = ITEROBJECT_SHAPE.split("\n")
        self.assertIn("Py_DECREF(seq);", source[found["line"] - 1])

    def test_no_python_reaching_call_is_not_flagged(self):
        """Without an intervening call there is no re-entrancy window."""
        c_code = (
            "static void\n"
            "drop_slot(PyObject *op)\n"
            "{\n"
            "    myobj *it = (myobj *)op;\n"
            "    PyObject *seq;\n"
            "    seq = it->it_seq;\n"
            "    it->it_seq = NULL;\n"
            "    Py_DECREF(seq);\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "stale_slot_decref"), [])

    def test_refcount_only_window_is_low_confidence(self):
        """A bare Py_DECREF between load and clear is weaker evidence.

        Modules/_io/textio.c _textiowrapper_writeflush is exactly this and is
        the rule's one tree-wide false positive, so it is split out by
        confidence rather than reported as high.
        """
        c_code = (
            "static int\n"
            "writeflush(PyObject *op)\n"
            "{\n"
            "    textio *self = (textio *)op;\n"
            "    PyObject *pending = self->pending_bytes;\n"
            "    PyObject *b = PyBytes_FromStringAndSize(NULL, 4);\n"
            "    if (b == NULL) {\n"
            "        return -1;\n"
            "    }\n"
            "    Py_DECREF(b);\n"
            "    self->pending_bytes = NULL;\n"
            "    Py_DECREF(pending);\n"
            "    return 0;\n"
            "}\n"
        )
        found = _types(_scan(c_code), "stale_slot_decref")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["confidence"], "low")


# ---------------------------------------------------------------------------
# owner_freed_before_use -- the genericaliasobject.c alias shape
# ---------------------------------------------------------------------------


class TestOwnerFreedBeforeUse(unittest.TestCase):
    """The Objects/genericaliasobject.c:542 heap-use-after-free shape."""

    def test_true_positive_alias_chain(self):
        """`args = tuple_args = ...` makes the two names one object."""
        c_code = (
            "static PyObject *\n"
            "_Py_subs_parameters(PyObject *self, PyObject *args)\n"
            "{\n"
            "    PyObject *tuple_args = NULL;\n"
            "    if (is_args_list) {\n"
            "        args = tuple_args = PySequence_Tuple(args);\n"
            "    }\n"
            "    if (!PyTuple_Check(arg)) {\n"
            "        Py_XDECREF(tuple_args);\n"
            "        PyObject *original = PyTuple_GET_ITEM(args, iarg);\n"
            "        PyErr_Format(PyExc_TypeError, \"bad %T\", original);\n"
            "        return NULL;\n"
            "    }\n"
            "    return NULL;\n"
            "}\n"
        )
        found = _types(_scan(c_code), "owner_freed_before_use")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["variable"], "tuple_args")
        self.assertEqual(found[0]["confidence"], "high")
        self.assertIn("aliases", found[0]["detail"])
        lines = c_code.split("\n")
        self.assertIn("PyTuple_GET_ITEM(args, iarg)", lines[found[0]["line"] - 1])

    def test_true_negative_release_then_return(self):
        """Every other Py_XDECREF(tuple_args) in the real file returns."""
        c_code = (
            "static PyObject *\n"
            "subs_ok(PyObject *self, PyObject *args)\n"
            "{\n"
            "    PyObject *tuple_args = NULL;\n"
            "    args = tuple_args = PySequence_Tuple(args);\n"
            "    if (newargs == NULL) {\n"
            "        Py_XDECREF(tuple_args);\n"
            "        return NULL;\n"
            "    }\n"
            "    return newargs;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "owner_freed_before_use"), [])

    def test_preprocessor_branch_is_not_sequential(self):
        """`#else` puts the DECREF and the use in different builds.

        Objects/dictobject.c's Py_GIL_DISABLED lookups are this shape.
        """
        c_code = (
            "static Py_ssize_t\n"
            "lookup(PyDictObject *mp, PyObject *key)\n"
            "{\n"
            "    PyObject *value;\n"
            "    Py_ssize_t ix;\n"
            "#ifdef Py_GIL_DISABLED\n"
            "    ix = _Py_dict_lookup_threadsafe(mp, key, hash, &value);\n"
            "    Py_XDECREF(value);\n"
            "#else\n"
            "    ix = _Py_dict_lookup(mp, key, hash, &value);\n"
            "#endif\n"
            "    return ix;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "owner_freed_before_use"), [])

    def test_locally_increffed_reference_is_not_a_uaf(self):
        """Dropping a reference this function took leaves the caller's."""
        c_code = (
            "static int\n"
            "get_data(PyObject *obj)\n"
            "{\n"
            "    Py_INCREF(obj);\n"
            "    Py_DECREF(obj);\n"
            "    set_lookup_failure(tstate, obj, NULL);\n"
            "    return -1;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "owner_freed_before_use"), [])

    def test_py_clear_nulls_its_own_operand(self):
        """Py_CLEAR(v) leaves v == NULL, so a later read is not dangling."""
        c_code = (
            "static PyObject *\n"
            "loads(PyObject *exc)\n"
            "{\n"
            "    Py_CLEAR(exc);\n"
            "    if (exc != NULL) {\n"
            "        _PyErr_SetRaisedException(tstate, exc);\n"
            "    }\n"
            "    return NULL;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "owner_freed_before_use"), [])

    def test_member_access_is_not_a_use_of_a_local(self):
        """`self->last` must not read as a use of the local `last`."""
        c_code = (
            "static PyObject *\n"
            "handle_end(PyObject *op)\n"
            "{\n"
            "    treebuilderobject *self = (treebuilderobject *)op;\n"
            "    PyObject *last = self->last;\n"
            "    self->last = Py_NewRef(this);\n"
            "    Py_DECREF(last);\n"
            "    if (append_event(self, self->end_event_obj, self->last) < 0) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return Py_None;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "owner_freed_before_use"), [])


# ---------------------------------------------------------------------------
# borrowed_ref_across_call
# ---------------------------------------------------------------------------


class TestBorrowedRefAcrossCall(unittest.TestCase):
    """Ownership released through a pointer the function never owned."""

    def test_true_positive_container_item_released_after_call(self):
        c_code = (
            "static PyObject *\n"
            "zip_longest_next(lzobject *lz)\n"
            "{\n"
            "    PyObject *it;\n"
            "    PyObject *item;\n"
            "    it = PyTuple_GET_ITEM(lz->ittuple, i);\n"
            "    item = PyIter_Next(it);\n"
            "    if (item == NULL) {\n"
            "        PyTuple_SET_ITEM(lz->ittuple, i, NULL);\n"
            "        Py_DECREF(it);\n"
            "    }\n"
            "    return item;\n"
            "}\n"
        )
        found = _types(_scan(c_code), "borrowed_ref_across_call")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["variable"], "it")
        self.assertIn("PyTuple_GET_ITEM", found[0]["detail"])

    def test_true_negative_increfed_before_the_call(self):
        c_code = (
            "static PyObject *\n"
            "zip_longest_safe(lzobject *lz)\n"
            "{\n"
            "    PyObject *it;\n"
            "    PyObject *item;\n"
            "    it = PyTuple_GET_ITEM(lz->ittuple, i);\n"
            "    Py_INCREF(it);\n"
            "    item = PyIter_Next(it);\n"
            "    if (item == NULL) {\n"
            "        Py_DECREF(it);\n"
            "    }\n"
            "    return item;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "borrowed_ref_across_call"), [])

    def test_incref_written_against_the_source_expression(self):
        """`Py_INCREF(lz->lz_attr); x = lz->lz_attr;` makes x an owner.

        Python/import.c _PyImport_LoadLazyImportTstate is this shape.
        """
        c_code = (
            "static PyObject *\n"
            "load_lazy(lzobject *lz)\n"
            "{\n"
            "    PyObject *fromlist = NULL;\n"
            "    Py_INCREF(lz->lz_attr);\n"
            "    fromlist = lz->lz_attr;\n"
            "    PyObject *r = PyObject_CallMethod(mod, \"x\", NULL);\n"
            "    Py_XDECREF(fromlist);\n"
            "    return r;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "borrowed_ref_across_call"), [])

    def test_slot_overwritten_before_release_is_the_owner_swap(self):
        """defaultdict's `olddefault` swap is correct, not a stale drop."""
        c_code = (
            "static int\n"
            "defdict_init(PyObject *self, PyObject *args, PyObject *kwds)\n"
            "{\n"
            "    defdictobject *dd = (defdictobject *)self;\n"
            "    PyObject *olddefault = dd->default_factory;\n"
            "    dd->default_factory = Py_XNewRef(newdefault);\n"
            "    int result = PyDict_Type.tp_init(self, newargs, kwds);\n"
            "    Py_XDECREF(olddefault);\n"
            "    return result;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "borrowed_ref_across_call"), [])

    def test_no_double_report_with_stale_slot_decref(self):
        """The iterobject shape is reported once, by the specific rule."""
        result = _scan(ITEROBJECT_SHAPE)
        lines = {f["line"] for f in _types(result, "stale_slot_decref")}
        for f in _types(result, "borrowed_ref_across_call"):
            self.assertNotIn(f["line"], lines)


# ---------------------------------------------------------------------------
# Python-reaching call table
# ---------------------------------------------------------------------------


class TestPythonReachingApis(unittest.TestCase):
    """PYTHON_REACHING_APIS is what makes the borrowed-ref rules work."""

    def test_py_decref_is_python_reaching(self):
        """A __del__ is arbitrary Python; this is what finds finding 1."""
        self.assertIn("Py_DECREF", mod.PYTHON_REACHING_APIS)
        self.assertIn("Py_XDECREF", mod.PYTHON_REACHING_APIS)
        self.assertIn("Py_CLEAR", mod.PYTHON_REACHING_APIS)

    def test_families_match_by_prefix(self):
        text = "x = PySequence_GetItem(s, 0); y = PyNumber_Add(a, b);"
        names = mod.python_reaching_calls(text, 0, len(text))
        self.assertIn("PySequence_GetItem", names)
        self.assertIn("PyNumber_Add", names)

    def test_pure_macros_are_excluded(self):
        text = "o = PySequence_Fast_GET_ITEM(s, 0);"
        self.assertEqual(mod.python_reaching_calls(text, 0, len(text)), [])

    def test_converter_callbacks_and_warnings_reach_python(self):
        for api in ("PyUnicode_FSConverter", "PyErr_WarnEx", "PySys_Audit"):
            self.assertIn(api, mod.PYTHON_REACHING_APIS)


# ---------------------------------------------------------------------------
# Line-number fidelity (TK-15)
# ---------------------------------------------------------------------------


class TestLineNumbers(unittest.TestCase):
    """Block comments must not shift reported line numbers."""

    def test_strip_comments_preserves_newlines(self):
        source = "a\n/* one\n   two\n   three */\nb\n"
        self.assertEqual(
            mod.strip_comments_and_strings(source).count("\n"),
            source.count("\n"),
        )

    def test_line_survives_a_multiline_block_comment(self):
        c_code = (
            "static PyObject *\n"
            "iter_iternext(PyObject *iterator)\n"
            "{\n"
            "    seqiterobject *it = (seqiterobject *)iterator;\n"
            "    PyObject *seq;\n"
            "    /* A block comment that spans\n"
            "       several lines and used to\n"
            "       collapse into one space. */\n"
            "    seq = it->it_seq;\n"
            "    PyObject *result = PySequence_GetItem(seq, it->it_index);\n"
            "    if (result == NULL) {\n"
            "        it->it_seq = NULL;\n"
            "        Py_DECREF(seq);\n"
            "    }\n"
            "    return result;\n"
            "}\n"
        )
        found = _types(_scan(c_code), "stale_slot_decref")
        self.assertEqual(len(found), 1)
        lines = c_code.split("\n")
        self.assertIn("Py_DECREF(seq);", lines[found[0]["line"] - 1])

    def test_body_start_line_is_exact(self):
        c_code = "static int\nfoo(void)\n{\n    return 0;\n}\n"
        func = mod.find_functions(c_code)[0]
        self.assertEqual(func["body_start_line"], 4)
        self.assertEqual(c_code.split("\n")[3].strip(), "return 0;")


# ---------------------------------------------------------------------------
# Type-slot registration
# ---------------------------------------------------------------------------


class TestSlotRegistration(unittest.TestCase):
    """A name ending in `_new` is not a tp_new."""

    def test_positional_static_form_is_read_from_raw_source(self):
        """Objects/ uses `X, /* tp_new */` 42 times versus 2 designated.

        The marker lives in a comment, so it is only visible before
        strip_comments_and_strings() runs.
        """
        source = (
            "PyTypeObject PyList_Type = {\n"
            "    0,                          /* tp_init */\n"
            "    PyType_GenericAlloc,        /* tp_alloc */\n"
            "    (newfunc)list_new,          /* tp_new */\n"
            "};\n"
        )
        slots = mod.collect_slot_registrations(source)
        self.assertEqual(slots.get("tp_new"), {"list_new"})
        self.assertNotIn("tp_init", slots)

    def test_designated_and_spec_forms(self):
        source = (
            "static PyTypeObject T = { .tp_new = foo_new, .tp_init = foo_init };\n"
            "static PyType_Slot S[] = {{Py_tp_new, bar_new}, {0, NULL}};\n"
        )
        slots = mod.collect_slot_registrations(source)
        self.assertEqual(slots["tp_new"], {"foo_new", "bar_new"})
        self.assertEqual(slots["tp_init"], {"foo_init"})

    def test_capi_constructor_is_not_a_tp_new(self):
        """PyCell_New and friends are C-API constructors, not slots.

        object.__new__ allocates via tp_alloc (which zeroes) and refuses
        outright when tp_new is overridden, so the old rationale never applied.
        """
        c_code = (
            "PyObject *\n"
            "PyCell_New(PyObject *obj)\n"
            "{\n"
            "    PyCellObject *op = PyObject_GC_New(PyCellObject, &PyCell_Type);\n"
            "    if (op == NULL)\n"
            "        return NULL;\n"
            "    PyObject *tmp = PyObject_Repr(obj);\n"
            "    op->ob_ref = Py_XNewRef(obj);\n"
            "    return (PyObject *)op;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "new_missing_member_init"), [])

    def test_helper_named_init_is_not_a_tp_init(self):
        """unionbuilder_init initialises a stack struct, not an instance."""
        c_code = (
            "static int\n"
            "unionbuilder_init(unionbuilder *ub, bool is_builtin)\n"
            "{\n"
            "    ub->args = PyList_New(0);\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "init_not_reinit_safe"), [])

    def test_clinic_impl_suffix_matches_the_registered_wrapper(self):
        c_code = (
            "static PyTypeObject T = { .tp_init = MyObj_init };\n"
            "\n"
            "static int\n"
            "MyObj_init_impl(MyObj *self, int x)\n"
            "{\n"
            "    self->data = PyList_New(0);\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(len(_types(_scan(c_code), "init_not_reinit_safe")), 1)


# ---------------------------------------------------------------------------
# tp_init / tp_new safety
# ---------------------------------------------------------------------------


class TestInitReinitSafety(unittest.TestCase):
    """A registered tp_init that leaks on a second __init__()."""

    REGISTERED = "static PyTypeObject T = { .tp_init = MyObj_init };\n\n"

    def _init(self, body):
        return self.REGISTERED + (
            "static int\n"
            "MyObj_init(MyObj *self, PyObject *args, PyObject *kwds)\n"
            "{\n" + body + "}\n"
        )

    def test_true_positive_unguarded_alloc(self):
        code = self._init(
            "    self->data = PyList_New(0);\n"
            "    self->buffer = PyMem_Malloc(1024);\n"
            "    return 0;\n"
        )
        found = _types(_scan(code), "init_not_reinit_safe")
        self.assertEqual(len(found), 1)
        self.assertIn("MyObj_init", found[0]["detail"])

    def test_true_negative_flag_guard(self):
        code = self._init(
            "    if (self->initialized) {\n"
            '        PyErr_SetString(PyExc_RuntimeError, "already initialized");\n'
            "        return -1;\n"
            "    }\n"
            "    self->data = PyList_New(0);\n"
            "    return 0;\n"
        )
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])

    def test_guard_message_lives_in_a_string_literal(self):
        """_ctypes' StgInfo guard is only visible in the raw body.

        strip_comments_and_strings() blanks string literals, so the guard
        check has to run against the unstripped source too.
        """
        code = self._init(
            '    PyErr_Format(PyExc_SystemError, "StgInfo of %s is already '
            'initialized.", n);\n'
            "    self->data = PyList_New(0);\n"
            "    return 0;\n"
        )
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])

    def test_initialiser_helper_owns_the_reinit_decision(self):
        code = self._init(
            "    StgInfo *stginfo = PyStgInfo_Init(st, (PyTypeObject *)self);\n"
            "    if (stginfo == NULL) {\n"
            "        return -1;\n"
            "    }\n"
            "    stginfo->format = PyMem_Malloc(16);\n"
            "    return 0;\n"
        )
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])

    def test_save_old_then_release_is_reinit_safe(self):
        code = self._init(
            "    PyObject *olddefault = self->factory;\n"
            "    self->factory = PyList_New(0);\n"
            "    Py_XDECREF(olddefault);\n"
            "    return 0;\n"
        )
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])

    def test_no_alloc_no_finding(self):
        code = self._init("    self->count = 0;\n    return 0;\n")
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])


class TestNewMemberInit(unittest.TestCase):
    """A registered tp_new that can hand a half-built object to tp_dealloc."""

    REGISTERED = "static PyTypeObject T = { .tp_new = MyObj_new };\n\n"

    def _new(self, body):
        return self.REGISTERED + (
            "static PyObject *\n"
            "MyObj_new(PyTypeObject *type, PyObject *args, PyObject *kwds)\n"
            "{\n" + body + "}\n"
        )

    def test_true_positive_fallible_call_before_member_init(self):
        code = self._new(
            "    MyObj *self = (MyObj *)PyObject_New(MyObj, type);\n"
            "    PyObject *tmp = PyObject_CallNoArgs(factory);\n"
            "    if (tmp == NULL) {\n"
            "        Py_DECREF(self);\n"
            "        return NULL;\n"
            "    }\n"
            "    self->data = tmp;\n"
            "    return (PyObject *)self;\n"
        )
        found = _types(_scan(code), "new_missing_member_init")
        self.assertEqual(len(found), 1)
        self.assertIn("PyObject_New", found[0]["detail"])
        lines = code.split("\n")
        self.assertIn("PyObject_New", lines[found[0]["line"] - 1])

    def test_true_negative_zeroing_allocator(self):
        code = self._new(
            "    MyObj *self = (MyObj *)type->tp_alloc(type, 0);\n"
            "    PyObject *tmp = PyObject_CallNoArgs(factory);\n"
            "    self->data = tmp;\n"
            "    return (PyObject *)self;\n"
        )
        self.assertEqual(_types(_scan(code), "new_missing_member_init"), [])

    def test_memset_of_a_sub_object_counts_as_zeroing(self):
        """Modules/_testcapi/structmember.c zeroes ob->structmembers."""
        code = self._new(
            "    MyObj *ob = PyObject_New(MyObj, type);\n"
            "    if (ob == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    memset(&ob->structmembers, 0, sizeof(all_structmembers));\n"
            "    if (!PyArg_ParseTupleAndKeywords(args, kwargs, fmt, kw)) {\n"
            "        Py_DECREF(ob);\n"
            "        return NULL;\n"
            "    }\n"
            "    return (PyObject *)ob;\n"
        )
        self.assertEqual(_types(_scan(code), "new_missing_member_init"), [])

    def test_no_window_between_alloc_and_stores(self):
        """A bare Py_DECREF on the allocator's NULL branch is not a window."""
        code = self._new(
            "    MyObj *self = (MyObj *)PyObject_New(MyObj, type);\n"
            "    if (self == NULL) {\n"
            "        Py_DECREF(attr);\n"
            "        return NULL;\n"
            "    }\n"
            "    self->attr = attr;\n"
            "    return (PyObject *)self;\n"
        )
        self.assertEqual(_types(_scan(code), "new_missing_member_init"), [])


# ---------------------------------------------------------------------------
# New-reference balance
# ---------------------------------------------------------------------------


class TestLeakDetection(unittest.TestCase):
    """potential_leak and its precision gates."""

    def test_true_positive_plain_leak(self):
        c_code = (
            "static PyObject *\n"
            "leaky(PyObject *self)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    if (item == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return Py_None;\n"
            "}\n"
        )
        found = _types(_scan(c_code), "potential_leak")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["variable"], "item")

    def test_returned_reference_is_not_a_leak(self):
        c_code = (
            "static PyObject *\n"
            "clean(PyObject *self)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    return item;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak"), [])

    def test_member_assignment_transfers_ownership(self):
        """`ub->args = PyList_New(0)` gives the struct's finalizer the ref."""
        c_code = (
            "static int\n"
            "setup(unionobject *ub)\n"
            "{\n"
            "    ub->args = PyList_New(0);\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak"), [])

    def test_out_parameter_transfers_ownership(self):
        """`*pv = PyTuple_New(n)` hands the reference to the caller."""
        c_code = (
            "static int\n"
            "resize(PyObject **pv, Py_ssize_t newsize)\n"
            "{\n"
            "    *pv = PyTuple_New(newsize);\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak"), [])

    def test_declaration_star_is_not_an_out_parameter(self):
        """`PyObject *item = ...` is a declaration, not `*item = ...`."""
        clean = "    PyObject *item = PyLong_FromLong(42);"
        pos = clean.index("item")
        self.assertTrue(mod._is_local_assignment(clean, pos))
        deref = "    *pv = PyTuple_New(4);"
        self.assertFalse(mod._is_local_assignment(deref, deref.index("pv")))

    def test_py_setref_consumes_its_second_argument(self):
        c_code = (
            "static int\n"
            "swap(PyObject *item)\n"
            "{\n"
            "    PyObject *tmp = PyTuple_New(1);\n"
            "    Py_SETREF(item, tmp);\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak"), [])

    def test_py_buildvalue_n_code_consumes_arguments(self):
        c_code = (
            "static PyObject *\n"
            "reduce(PyObject *self)\n"
            "{\n"
            "    PyObject *list = PyList_New(0);\n"
            "    return Py_BuildValue(\"N(N)\", iter, list);\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak"), [])

    def test_module_global_is_process_lifetime(self):
        """CPython's module-init code parks objects in statics on purpose."""
        c_code = (
            "static PyObject *Struct = NULL;\n"
            "\n"
            "static int\n"
            "exec_module(PyObject *m)\n"
            "{\n"
            "    Struct = PyUnicode_FromString(\"Struct\");\n"
            "    if (Struct == NULL) {\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak"), [])


class TestLeakOnError(unittest.TestCase):
    """potential_leak_on_error and its flow gates."""

    def test_true_positive_missing_from_cleanup(self):
        c_code = (
            "static PyObject *\n"
            "build(PyObject *self)\n"
            "{\n"
            "    PyObject *key = PyTuple_Pack(1, self);\n"
            "    PyObject *other = PyList_New(0);\n"
            "    if (other == NULL) {\n"
            "        goto error;\n"
            "    }\n"
            "    Py_DECREF(other);\n"
            "    return key;\n"
            "error:\n"
            "    Py_XDECREF(other);\n"
            "    return NULL;\n"
            "}\n"
        )
        found = _types(_scan(c_code), "potential_leak_on_error")
        self.assertEqual([f["variable"] for f in found], ["key"])

    def test_assigned_after_the_last_goto_cannot_leak(self):
        c_code = (
            "static PyObject *\n"
            "late(PyObject *self)\n"
            "{\n"
            "    if (bad) {\n"
            "        goto error;\n"
            "    }\n"
            "    PyObject *result = PyList_New(0);\n"
            "    return result;\n"
            "error:\n"
            "    return NULL;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak_on_error"), [])

    def test_label_that_returns_the_variable_is_not_a_leak(self):
        """math_fsum's `_fsum_error: ... return sum;` -- indented label."""
        c_code = (
            "static PyObject *\n"
            "fsum(PyObject *self, PyObject *seq)\n"
            "{\n"
            "    PyObject *sum = PyFloat_FromDouble(0.0);\n"
            "    if (bad) {\n"
            "        goto _fsum_error;\n"
            "    }\n"
            "    return sum;\n"
            "\n"
            "  _fsum_error:\n"
            "    Py_DECREF(iter);\n"
            "    return sum;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak_on_error"), [])

    def test_goto_inside_the_null_check_branch_is_not_a_live_path(self):
        c_code = (
            "static PyObject *\n"
            "make(PyObject *self)\n"
            "{\n"
            "    PyObject *capsule = PyCapsule_New(p, NULL, d);\n"
            "    if (capsule == NULL) {\n"
            "        goto error;\n"
            "    }\n"
            "    return capsule;\n"
            "error:\n"
            "    return NULL;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_leak_on_error"), [])


class TestDoubleFree(unittest.TestCase):
    """potential_double_free and its branch gate."""

    def test_true_positive_steal_then_decref(self):
        c_code = (
            "static int\n"
            "double_free_risk(PyObject *list)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    PyList_SET_ITEM(list, 0, item);\n"
            "    Py_DECREF(item);\n"
            "    return 0;\n"
            "}\n"
        )
        found = _types(_scan(c_code), "potential_double_free")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["variable"], "item")

    def test_true_negative_steal_without_decref(self):
        c_code = (
            "static int\n"
            "stolen_ref(PyObject *tuple)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    PyTuple_SET_ITEM(tuple, 0, item);\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_double_free"), [])

    def test_decref_on_the_steal_failure_branch_is_correct(self):
        """PyModule_AddObject steals only on success; the drop is required."""
        c_code = (
            "static int\n"
            "xx_modexec(PyObject *m)\n"
            "{\n"
            "    PyObject *ErrorObject = PyErr_NewException(\"xx.error\", NULL, NULL);\n"
            "    Py_INCREF(ErrorObject);\n"
            "    if (PyModule_AddObject(m, \"error\", ErrorObject) < 0) {\n"
            "        Py_DECREF(ErrorObject);\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "potential_double_free"), [])

    def test_pyset_discard_does_not_steal(self):
        """PySet_Discard removes an element; it takes no reference."""
        self.assertNotIn("PySet_Discard", mod.STEAL_REF_APIS)


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class TestAnalyze(unittest.TestCase):
    """Full analysis envelope."""

    def test_summary_fields(self):
        result = _scan(
            "static PyObject *\ntest(PyObject *self)\n{\n    return Py_None;\n}\n"
        )
        for key in (
            "potential_leaks", "potential_double_frees", "stale_slot_decref",
            "owner_freed_before_use", "borrowed_ref_across_call",
            "init_not_reinit_safe", "new_missing_member_init",
            "total_findings", "high_confidence", "medium_confidence",
            "low_confidence",
        ):
            self.assertIn(key, result["summary"])

    def test_every_finding_carries_file_function_and_line(self):
        result = _scan(ITEROBJECT_SHAPE)
        self.assertTrue(result["findings"])
        for f in result["findings"]:
            self.assertIn("file", f)
            self.assertIn("function", f)
            self.assertIsInstance(f["line"], int)
            self.assertGreater(f["line"], 0)
            self.assertNotIn("line_offset", f)

    def test_empty_project(self):
        with TempProject({}, cpython_markers=False) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["functions_analyzed"], 0)
            self.assertEqual(len(result["findings"]), 0)


class TestFindFunctions(unittest.TestCase):
    """C function detection including multi-line signatures."""

    def test_multiline_signature(self):
        c_code = (
            "static int\n"
            "init_sockobject(socket_state *state, PySocketSockObject *s,\n"
            "                int family, int type, int proto)\n"
            "{\n"
            "    s->sock_family = family;\n"
            "    return 0;\n"
            "}\n"
        )
        names = [f["name"] for f in mod.find_functions(c_code)]
        self.assertIn("init_sockobject", names)

    def test_clinic_comment_before_brace(self):
        c_code = (
            "static int\n"
            "sock_initobj_impl(PySocketSockObject *self, int family)\n"
            "/*[clinic end generated code: output=abc123 input=def456]*/\n"
            "{\n"
            "    self->sock_family = family;\n"
            "    return 0;\n"
            "}\n"
        )
        funcs = mod.find_functions(c_code)
        names = [f["name"] for f in funcs]
        self.assertIn("sock_initobj_impl", names)
        func = next(f for f in funcs if f["name"] == "sock_initobj_impl")
        self.assertIn("sock_family", func["body"])


if __name__ == "__main__":
    unittest.main()
