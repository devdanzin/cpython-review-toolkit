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
            '        PyErr_Format(PyExc_TypeError, "bad %T", original);\n'
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
            '    PyObject *r = PyObject_CallMethod(mod, "x", NULL);\n'
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
        for f in _types(result, "stale_slot_use"):
            self.assertNotIn(f["line"], lines)


# ---------------------------------------------------------------------------
# slot_transfer_across_call -- the escape hazard
# ---------------------------------------------------------------------------


# Modules/itertoolsmodule.c count_nextlong, an ASan-confirmed heap-use-after-
# free: the borrowed slot value is handed to PyNumber_Add (which dispatches to
# an arbitrary user __radd__ through the untyped step), the slot is then
# overwritten, and the stale local is *returned* -- so a re-entrant call that
# performed the same transfer leaves two owners for one reference.
COUNT_NEXTLONG_SHAPE = (
    "static PyObject *\n"
    "count_nextlong(countobject *lz)\n"
    "{\n"
    "    PyObject *result = lz->long_cnt;\n"
    "    PyObject *stepped_up = PyNumber_Add(result, lz->long_step);\n"
    "    if (stepped_up == NULL) {\n"
    "        return NULL;\n"
    "    }\n"
    "    lz->long_cnt = stepped_up;\n"
    "    return result;\n"
    "}\n"
)

# Objects/enumobject.c increment_longindex_lock_held: a structural clone of
# count_nextlong, comment text and all, that is SAFE because both PyNumber_Add
# operands are provably PyLong -- `en->one` is `_PyLong_GetOne()` -- so the
# dispatch resolves to long_add and no user code runs.
ENUM_LONGINDEX_SHAPE = (
    "static PyObject *\n"
    "enum_new_impl(PyTypeObject *type, PyObject *iterable, PyObject *start)\n"
    "{\n"
    "    enumobject *en = (enumobject *)type->tp_alloc(type, 0);\n"
    "    start = PyNumber_Index(start);\n"
    "    en->en_longindex = start;\n"
    "    en->one = _PyLong_GetOne();\n"
    "    return (PyObject *)en;\n"
    "}\n"
    "\n"
    "static PyObject *\n"
    "increment_longindex_lock_held(enumobject *en)\n"
    "{\n"
    "    PyObject *next_index = en->en_longindex;\n"
    "    PyObject *stepped_up = PyNumber_Add(next_index, en->one);\n"
    "    if (stepped_up == NULL) {\n"
    "        return NULL;\n"
    "    }\n"
    "    en->en_longindex = stepped_up;\n"
    "    return next_index;\n"
    "}\n"
)


class TestSlotTransferAcrossCall(unittest.TestCase):
    """The transfer idiom performed across a re-entrancy window."""

    def test_true_positive_count_nextlong(self):
        findings = _types(_scan(COUNT_NEXTLONG_SHAPE), "slot_transfer_across_call")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["variable"], "result")
        self.assertEqual(findings[0]["source"], "lz->long_cnt")
        self.assertEqual(findings[0]["api_call"], "PyNumber_Add")
        # Reported on the borrowed *load*, which is the line to change.
        self.assertEqual(findings[0]["line"], 4)
        self.assertEqual(findings[0]["escape_line"], 10)

    def test_true_negative_type_constrained_operand(self):
        """Objects/enumobject.c:196 -- the guarded twin, and it must stay silent."""
        self.assertEqual(
            _types(_scan(ENUM_LONGINDEX_SHAPE), "slot_transfer_across_call"), []
        )

    def test_both_shapes_in_one_tree_flags_only_the_bug(self):
        with TempProject(
            {
                "Modules/itertoolsmodule.c": COUNT_NEXTLONG_SHAPE,
                "Objects/enumobject.c": ENUM_LONGINDEX_SHAPE,
            }
        ) as root:
            result = mod.analyze(str(root))
        findings = _types(result, "slot_transfer_across_call")
        self.assertEqual([f["function"] for f in findings], ["count_nextlong"])

    def test_overwrite_before_the_call_is_a_completed_transfer(self):
        """Modules/_tkinter.c TimerHandler: the slot is cleared while we are
        still alone, so the local is the legitimate sole owner."""
        c_code = (
            "static void\n"
            "TimerHandler(ClientData clientData)\n"
            "{\n"
            "    PyObject *func = v->func;\n"
            "    v->func = NULL;\n"
            "    PyObject *res = PyObject_CallNoArgs(func);\n"
            "    Py_DECREF(func);\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "slot_transfer_across_call"), [])

    def test_incref_on_the_slot_makes_the_local_an_owner(self):
        owned = COUNT_NEXTLONG_SHAPE.replace(
            "    PyObject *result = lz->long_cnt;\n",
            "    PyObject *result = lz->long_cnt;\n    Py_INCREF(result);\n",
        )
        self.assertEqual(_types(_scan(owned), "slot_transfer_across_call"), [])


# ---------------------------------------------------------------------------
# stale_slot_use -- the deref / call hazard
# ---------------------------------------------------------------------------


# Modules/itertoolsmodule.c batched_next, an ASan-confirmed heap-use-after-free.
# Three things have to line up for this to be visible: the borrowed slot load,
# a Python-reaching call spelled as a *runtime slot dispatch* through a cached
# function pointer, and loop-carried exposure -- the local is used exactly once
# textually, and the danger is that iteration N+1's use follows iteration N's
# call.
BATCHED_NEXT_SHAPE = (
    "static PyObject *\n"
    "batched_next(PyObject *op)\n"
    "{\n"
    "    batchedobject *bo = batchedobject_CAST(op);\n"
    "    PyObject *it = bo->it;\n"
    "    Py_ssize_t i;\n"
    "    iternextfunc iternext = *Py_TYPE(it)->tp_iternext;\n"
    "    for (i = 0; i < n; i++) {\n"
    "        PyObject *item = iternext(it);\n"
    "        if (item == NULL) {\n"
    "            goto null_item;\n"
    "        }\n"
    "    }\n"
    "    return result;\n"
    " null_item:\n"
    "    Py_CLEAR(bo->it);\n"
    "    return NULL;\n"
    "}\n"
)


class TestStaleSlotUse(unittest.TestCase):
    """A cached slot value dereferenced or called after a re-entrant clear."""

    def test_true_positive_batched_next(self):
        findings = _types(_scan(BATCHED_NEXT_SHAPE), "stale_slot_use")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["variable"], "it")
        self.assertEqual(findings[0]["source"], "bo->it")
        self.assertEqual(findings[0]["confidence"], "high")
        # The use is the indirect call, not the load.
        self.assertEqual(findings[0]["line"], 9)
        self.assertEqual(findings[0]["load_line"], 5)

    def test_runtime_slot_dispatch_is_python_reaching(self):
        findings = _types(_scan(BATCHED_NEXT_SHAPE), "stale_slot_use")
        self.assertIn("tp_iternext", findings[0]["api_call"])

    def test_statically_named_type_slot_is_not_python_reaching(self):
        """PyUnicode_Type.tp_hash is known at compile time -- not user code."""
        self.assertEqual(
            mod.reaching_calls_with_slots("x = PyUnicode_Type.tp_hash(s);", 0, 30),
            [],
        )

    def test_true_negative_slot_is_re_read_after_the_call(self):
        """Modules/itertoolsmodule.c pairwise_next -- the guarded twin."""
        c_code = (
            "static PyObject *\n"
            "pairwise_next(PyObject *op)\n"
            "{\n"
            "    pairwiseobject *po = pairwiseobject_CAST(op);\n"
            "    PyObject *it = po->it;\n"
            "    PyObject *new_item = (*Py_TYPE(it)->tp_iternext)(it);\n"
            "    it = po->it;\n"
            "    if (it == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    Py_CLEAR(po->it);\n"
            "    return new_item;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "stale_slot_use"), [])

    def test_true_negative_clear_before_any_python_reaching_call(self):
        c_code = (
            "static PyObject *\n"
            "elementiter_next(PyObject *op)\n"
            "{\n"
            "    ElementIterObject *it = (ElementIterObject *)op;\n"
            "    PyObject *elem = it->root_element;\n"
            "    it->root_element = NULL;\n"
            "    PyObject *res = PyObject_CallOneArg(cb, elem);\n"
            "    return res;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(c_code), "stale_slot_use"), [])

    def test_incref_on_the_local_is_not_a_stale_use(self):
        owned = BATCHED_NEXT_SHAPE.replace(
            "    PyObject *it = bo->it;\n",
            "    PyObject *it = bo->it;\n    Py_INCREF(it);\n",
        )
        self.assertEqual(_types(_scan(owned), "stale_slot_use"), [])


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
        slots = mod.collect_slot_registrations(
            "static PyTypeObject T = { .tp_init = MyObj_init };\n"
        )
        self.assertTrue(
            mod._registered_as({"name": "MyObj_init_impl"}, slots, "tp_init")
        )
        self.assertFalse(
            mod._registered_as({"name": "Other_init_impl"}, slots, "tp_init")
        )

    def test_clinic_dunder_init_name_is_proof_of_tp_init(self):
        """Argument Clinic emits `<Type>___init___impl` for `Type.__init__`,
        and the registered slot is the generated wrapper in `clinic/*.c.h` —
        often with a further hand-written wrapper in between
        (Modules/_struct.c registers `s_init` -> `Struct___init__` ->
        `Struct___init___impl`). Requiring registration of the *impl* made the
        rule fire zero times over Objects/, Modules/ and Python/."""
        self.assertTrue(mod._is_tp_init({"name": "Struct___init___impl"}, {}))
        self.assertFalse(mod._is_tp_init({"name": "unionbuilder_init"}, {}))
        self.assertFalse(mod._is_tp_init({"name": "Struct_impl"}, {}))


# ---------------------------------------------------------------------------
# tp_init / tp_new safety
# ---------------------------------------------------------------------------


class TestInitReinitSafety(unittest.TestCase):
    """A re-callable __init__ that invalidates an outstanding view.

    Exemplar, live at 3.16.0a0 and reproduced on both a release and a debug
    build (Modules/_struct.c)::

        s = struct.Struct("i"); it = s.iter_unpack(b"\\0" * 8); next(it)
        s.__init__("100i"); next(it)

    prepare_s frees s_codes and resets s_size, and unpackiter_iternext keeps
    reading through its stored ``self->so`` — 100 ints out of an 8-byte
    buffer on release (73 of 100 words were live heap), and the
    ``assert(self->index + self->so->s_size <= self->buf.len)`` at
    _struct.c:2274 on debug.

    Note the polarity flip: Py_XSETREF / Py_CLEAR / free-then-assign used to
    *suppress* this rule as evidence of re-init safety. They are the opposite
    — they prove the second call destroys what the first one published.
    """

    # The _struct.c chain, reduced: clinic __init__ -> set_format -> prepare_s,
    # where only prepare_s frees and replaces, and set_format never
    # dereferences its receiver at all.
    STRUCT_SHAPE = """\
static int
prepare_s(PyStructObject *self, PyObject *format)
{
    formatcode *codes0 = PyMem_Malloc(64);
    if (self->s_codes != NULL) {
        PyMem_Free(self->s_codes);
    }
    self->s_codes = codes0;
    self->s_size = size;
    self->s_len = len;
    return 0;
}

static int
set_format(PyStructObject *self, PyObject *format)
{
    if (prepare_s(self, format)) {
        return -1;
    }
    return 0;
}

static int
Struct___init___impl(PyStructObject *self, PyObject *format)
{
    if (set_format(self, format) < 0) {
        return -1;
    }
    return 0;
}

static PyObject *
unpackiter_iternext(PyObject *op)
{
    unpackiterobject *self = (unpackiterobject *)op;
    assert(self->index + self->so->s_size <= self->buf.len);
    self->index += self->so->s_size;
    return NULL;
}
"""

    def test_true_positive_reinit_invalidates_a_view(self):
        found = _types(_scan(self.STRUCT_SHAPE), "init_not_reinit_safe")
        self.assertEqual(len(found), 1)
        f = found[0]
        self.assertEqual(f["function"], "Struct___init___impl")
        self.assertEqual(f["replaced_members"], ["s_codes"])
        self.assertEqual(f["stale_members"], ["s_size"])
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["readers"][0]["function"], "unpackiter_iternext")
        self.assertEqual(f["readers"][0]["expression"], "self->so->s_size")

    def test_true_negative_second_call_is_rejected(self):
        """The guarded twin: an __init__ that raises on re-entry."""
        code = self.STRUCT_SHAPE.replace(
            "    if (set_format(self, format) < 0) {",
            "    if (self->initialized) {\n"
            '        PyErr_SetString(PyExc_RuntimeError, "already initialized");\n'
            "        return -1;\n"
            "    }\n"
            "    if (set_format(self, format) < 0) {",
        )
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])

    def test_true_negative_no_outstanding_view(self):
        """Only the owner's own methods read the state, one level deep — they
        see the new values, which is correct."""
        code = self.STRUCT_SHAPE.replace(
            "    assert(self->index + self->so->s_size <= self->buf.len);\n"
            "    self->index += self->so->s_size;\n",
            "    assert(self->s_size > 0);\n    self->index += self->s_size;\n",
        )
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])

    def test_true_negative_init_only_fills_never_replaces(self):
        """An __init__ that assigns without releasing prior state publishes
        nothing a view could already be holding."""
        code = self.STRUCT_SHAPE.replace(
            "    if (self->s_codes != NULL) {\n"
            "        PyMem_Free(self->s_codes);\n"
            "    }\n",
            "",
        )
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])

    def test_initialiser_helper_owns_the_reinit_decision(self):
        """Modules/_ctypes/_ctypes.c PyCPointerType_init: PyStgInfo_Init raises
        "StgInfo of '%s' is already initialized." on the second call
        (Modules/_ctypes/ctypes.h:639), so the type is protected in a callee
        the scanner cannot see."""
        code = (
            "static int\n"
            "PyCPointerType_init(PyObject *self, PyObject *args, PyObject *k)\n"
            "{\n"
            "    StgInfo *stginfo = PyStgInfo_Init(st, (PyTypeObject *)self);\n"
            "    if (!stginfo) {\n"
            "        return -1;\n"
            "    }\n"
            "    PyMem_Free(stginfo->format);\n"
            "    stginfo->format = PyMem_Malloc(16);\n"
            "    return 0;\n"
            "}\n"
            "\n"
            "static PyTypeObject T = { .tp_init = PyCPointerType_init };\n"
            "\n"
            "static PyObject *\n"
            "reader_iternext(PyObject *op)\n"
            "{\n"
            "    view *self = (view *)op;\n"
            "    return use(self->owner->format);\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(code), "init_not_reinit_safe"), [])

    def test_helper_named_init_is_still_not_a_tp_init(self):
        """unionbuilder_init initialises a stack struct, not an instance."""
        code = (
            "static int\n"
            "unionbuilder_init(unionbuilder *ub, bool is_builtin)\n"
            "{\n"
            "    Py_CLEAR(ub->args);\n"
            "    ub->args = PyList_New(0);\n"
            "    return 0;\n"
            "}\n"
            "\n"
            "static PyObject *\n"
            "reader_iternext(PyObject *op)\n"
            "{\n"
            "    view *self = (view *)op;\n"
            "    return use(self->owner->args);\n"
            "}\n"
        )
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
            '    return Py_BuildValue("N(N)", iter, list);\n'
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
            '    Struct = PyUnicode_FromString("Struct");\n'
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
            '    PyObject *ErrorObject = PyErr_NewException("xx.error", NULL, NULL);\n'
            "    Py_INCREF(ErrorObject);\n"
            '    if (PyModule_AddObject(m, "error", ErrorObject) < 0) {\n'
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
            "potential_leaks",
            "potential_double_frees",
            "stale_slot_decref",
            "owner_freed_before_use",
            "borrowed_ref_across_call",
            "init_not_reinit_safe",
            "new_missing_member_init",
            "total_findings",
            "high_confidence",
            "medium_confidence",
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


# ---------------------------------------------------------------------------
# borrowed_field_deref_across_call -- the read-only hazard (issue #28 rule 1)
# ---------------------------------------------------------------------------
#
# All the shapes below are transcribed from Objects/typeobject.c, which
# contains two reproduced ASan heap-use-after-frees that every release-anchored
# rule in this file reported as clean (0 findings over 403 functions).

# The four accessors, verbatim in shape.  Two have a static-builtin branch and
# therefore two field returns; both must still be discovered.
ACCESSORS = (
    "static inline PyObject *\n"
    "lookup_tp_mro(PyTypeObject *self)\n"
    "{\n"
    "    return self->tp_mro;\n"
    "}\n"
    "\n"
    "static inline PyObject *\n"
    "lookup_tp_subclasses(PyTypeObject *self)\n"
    "{\n"
    "    if (self->tp_flags & _Py_TPFLAGS_STATIC_BUILTIN) {\n"
    "        managed_static_type_state *state = _PyStaticType_GetState(interp, self);\n"
    "        return state->tp_subclasses;\n"
    "    }\n"
    "    return (PyObject *)self->tp_subclasses;\n"
    "}\n"
)

# CPY-0068: the borrowed tp_mro is only ever READ, and the free happens inside
# inherit_slots -> overrides_hash -> PyDict_Contains -> a user __eq__.  Both the
# interprocedural hop and the loop-carried ordering are required to see it.
TYPE_READY_INHERIT = (
    "static int\n"
    "overrides_hash(PyTypeObject *type)\n"
    "{\n"
    "    PyObject *dict = type->tp_dict;\n"
    "    return PyDict_Contains(dict, &_Py_ID(__eq__));\n"
    "}\n"
    "\n"
    "static int\n"
    "inherit_slots(PyTypeObject *type, PyTypeObject *base)\n"
    "{\n"
    "    if (overrides_hash(base)) {\n"
    "        return -1;\n"
    "    }\n"
    "    return 0;\n"
    "}\n"
    "\n"
    "static int\n"
    "type_ready_inherit(PyTypeObject *type)\n"
    "{\n"
    "    PyObject *mro = lookup_tp_mro(type);\n"
    "    assert(mro != NULL);\n"
    "    Py_ssize_t n = PyTuple_GET_SIZE(mro);\n"
    "    for (Py_ssize_t i = 1; i < n; i++) {\n"
    "        PyObject *b = PyTuple_GET_ITEM(mro, i);\n"
    "        if (inherit_slots(type, (PyTypeObject *)b) < 0) {\n"
    "            return -1;\n"
    "        }\n"
    "    }\n"
    "    return 0;\n"
    "}\n"
)

# CPY-0069: the use is in the *controlling expression* of the loop and the
# invalidating call sits after it in text order, before it in iteration order.
RECURSE_DOWN_SUBCLASSES = (
    "static int\n"
    "recurse_down_subclasses(PyTypeObject *type, PyObject *attr_name)\n"
    "{\n"
    "    PyObject *subclasses = lookup_tp_subclasses(type);\n"
    "    if (subclasses == NULL) {\n"
    "        return 0;\n"
    "    }\n"
    "    Py_ssize_t i = 0;\n"
    "    PyObject *ref;\n"
    "    while (PyDict_Next(subclasses, &i, NULL, &ref)) {\n"
    "        PyObject *dict = ref;\n"
    "        int r = PyDict_Contains(dict, attr_name);\n"
    "        if (r < 0) {\n"
    "            return -1;\n"
    "        }\n"
    "    }\n"
    "    return 0;\n"
    "}\n"
)


class TestFieldAccessorDiscovery(unittest.TestCase):
    """The accessor set is discovered, not tabulated."""

    def _discover(self, code):
        funcs = mod.find_functions(code)
        return mod.discover_field_accessors(code, funcs)

    def test_discovers_single_return_accessor(self):
        found = self._discover(ACCESSORS)
        self.assertEqual(found.get("lookup_tp_mro"), "tp_mro")

    def test_discovers_two_branch_accessor(self):
        """Both returns forward the same field, through different owners."""
        found = self._discover(ACCESSORS)
        self.assertEqual(found.get("lookup_tp_subclasses"), "tp_subclasses")

    def test_accessor_returning_a_strong_reference_is_not_borrowing(self):
        code = (
            "PyObject *\n"
            "_PyType_GetBases(PyTypeObject *self)\n"
            "{\n"
            "    PyObject *res = self->tp_bases;\n"
            "    Py_INCREF(res);\n"
            "    return res;\n"
            "}\n"
        )
        self.assertEqual(self._discover(code), {})

    def test_accessor_that_computes_is_not_vouched_for(self):
        """One return this rule cannot follow disqualifies the whole function."""
        code = (
            "static PyObject *\n"
            "pick(PyTypeObject *self, int which)\n"
            "{\n"
            "    if (which) {\n"
            "        return self->tp_mro;\n"
            "    }\n"
            "    return build_something(self);\n"
            "}\n"
        )
        self.assertEqual(self._discover(code), {})

    def test_null_return_does_not_disqualify(self):
        code = (
            "static PyObject *\n"
            "lookup_tp_dict(PyTypeObject *self)\n"
            "{\n"
            "    if (self == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return self->tp_dict;\n"
            "}\n"
        )
        self.assertEqual(self._discover(code), {"lookup_tp_dict": "tp_dict"})


class TestBorrowedFieldDerefAcrossCall(unittest.TestCase):
    KIND = "borrowed_field_deref_across_call"

    def test_reaches_python_through_a_same_file_helper(self):
        """CPY-0068. inherit_slots looks local; it dispatches a user __eq__."""
        found = _types(_scan(ACCESSORS + TYPE_READY_INHERIT), self.KIND)
        hits = [f for f in found if f["function"] == "type_ready_inherit"]
        self.assertEqual(len(hits), 1, found)
        self.assertEqual(hits[0]["source"], "lookup_tp_mro() -> tp_mro")
        self.assertIn("inherit_slots", hits[0]["api_call"])

    def test_use_in_a_loop_controlling_expression(self):
        """CPY-0069. The invalidating call is textually after the use."""
        found = _types(_scan(ACCESSORS + RECURSE_DOWN_SUBCLASSES), self.KIND)
        hits = [f for f in found if f["function"] == "recurse_down_subclasses"]
        self.assertEqual(len(hits), 1, found)
        self.assertIn("PyDict_Contains", hits[0]["api_call"])

    def test_pointer_comparison_is_not_a_dereference(self):
        """typeobject.c:1957/:1993/:3667 are re-entrancy checks, correct code."""
        code = ACCESSORS + (
            "static int\n"
            "type_set_bases_unlocked(PyTypeObject *type, PyObject *new_bases)\n"
            "{\n"
            "    PyObject *mro = lookup_tp_mro(type);\n"
            "    if (PyDict_Contains(type->tp_dict, new_bases) < 0) {\n"
            "        return -1;\n"
            "    }\n"
            "    if (lookup_tp_mro(type) != mro) {\n"
            "        return -1;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(code), self.KIND), [])

    def test_strong_reference_suppresses(self):
        code = ACCESSORS + (
            "static int\n"
            "safe(PyTypeObject *type)\n"
            "{\n"
            "    PyObject *mro = lookup_tp_mro(type);\n"
            "    Py_INCREF(mro);\n"
            "    if (PyDict_Contains(type->tp_dict, mro) < 0) {\n"
            "        return -1;\n"
            "    }\n"
            "    Py_ssize_t n = PyTuple_GET_SIZE(mro);\n"
            "    Py_DECREF(mro);\n"
            "    return (int)n;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(code), self.KIND), [])

    def test_no_reaching_call_in_the_window_is_silent(self):
        """typeobject.c:8771/:10128/:11368 -- borrowed, read, but nothing runs."""
        code = ACCESSORS + (
            "static int\n"
            "hackcheck_unlocked(PyTypeObject *type)\n"
            "{\n"
            "    PyObject *mro = lookup_tp_mro(type);\n"
            "    if (!mro) {\n"
            "        return 1;\n"
            "    }\n"
            "    for (Py_ssize_t i = PyTuple_GET_SIZE(mro) - 1; i >= 0; i--) {\n"
            "        PyObject *base = PyTuple_GET_ITEM(mro, i);\n"
            "        if (base == NULL) {\n"
            "            break;\n"
            "        }\n"
            "    }\n"
            "    return 1;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(code), self.KIND), [])

    def test_a_load_inside_the_loop_is_not_loop_carried(self):
        """It is refreshed every iteration, so iteration N's call cannot reach it."""
        code = ACCESSORS + (
            "static int\n"
            "per_iteration(PyTypeObject *type, PyObject *name)\n"
            "{\n"
            "    for (Py_ssize_t i = 0; i < 4; i++) {\n"
            "        PyObject *mro = lookup_tp_mro(type);\n"
            "        int r = PyDict_Contains(mro, name);\n"
            "        if (r < 0) {\n"
            "            return -1;\n"
            "        }\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(code), self.KIND), [])

    def test_guarded_twin_raises_confidence_to_high(self):
        twin = (
            "PyObject *\n"
            "_PyType_GetMro(PyTypeObject *self)\n"
            "{\n"
            "    PyObject *res = lookup_tp_mro(self);\n"
            "    Py_INCREF(res);\n"
            "    return res;\n"
            "}\n"
        )
        without = _types(_scan(ACCESSORS + TYPE_READY_INHERIT), self.KIND)
        withtwin = _types(_scan(ACCESSORS + twin + TYPE_READY_INHERIT), self.KIND)
        self.assertEqual([f["confidence"] for f in without], ["medium"])
        self.assertEqual([f["confidence"] for f in withtwin], ["high"])
        self.assertIsNotNone(withtwin[0]["guarded_twin_line"])

    def test_wrapped_newref_also_counts_as_a_twin(self):
        """typeobject.c:3665 spells it Py_XNewRef(lookup_tp_mro(type))."""
        twin = (
            "static int\n"
            "mro_hierarchy(PyTypeObject *type)\n"
            "{\n"
            "    PyObject *old_mro = Py_XNewRef(lookup_tp_mro(type));\n"
            "    Py_XDECREF(old_mro);\n"
            "    return 0;\n"
            "}\n"
        )
        found = _types(_scan(ACCESSORS + twin + TYPE_READY_INHERIT), self.KIND)
        self.assertEqual([f["confidence"] for f in found], ["high"])

    def test_pydict_next_is_not_a_python_reaching_call(self):
        """It walks the entry table by index: no hashing, no comparison."""
        self.assertNotIn("PyDict_Next", mod.PYTHON_REACHING_APIS)
        code = ACCESSORS + (
            "static int\n"
            "walk_only(PyTypeObject *type)\n"
            "{\n"
            "    PyObject *subclasses = lookup_tp_subclasses(type);\n"
            "    Py_ssize_t i = 0;\n"
            "    PyObject *ref;\n"
            "    while (PyDict_Next(subclasses, &i, NULL, &ref)) {\n"
            "        i++;\n"
            "    }\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(code), self.KIND), [])

    def test_external_call_does_not_count_as_reaching(self):
        """An unresolved callee stays unknown; assuming would stop the gate gating."""
        code = ACCESSORS + (
            "static int\n"
            "calls_out(PyTypeObject *type)\n"
            "{\n"
            "    PyObject *mro = lookup_tp_mro(type);\n"
            "    some_external_helper(type);\n"
            "    return (int)PyTuple_GET_SIZE(mro);\n"
            "}\n"
        )
        self.assertEqual(_types(_scan(code), self.KIND), [])


class TestLoopScopeEnd(unittest.TestCase):
    """_block_end reports where the block *enclosing* its start closes."""

    def test_block_end_needs_the_index_after_the_brace(self):
        text = "a { b } c } d"
        brace = text.index("{")
        # Handed the brace itself it starts one level deep and overshoots to
        # the *next* closing brace -- how hackcheck_unlocked's first loop
        # window came to include the second loop's PyErr_Format.
        self.assertEqual(text[mod._block_end(text, brace)], "}")
        self.assertEqual(mod._block_end(text, brace), text.rindex("}"))
        self.assertEqual(mod._block_end(text, brace + 1), text.index("}"))

    def test_two_sibling_loops_get_distinct_windows(self):
        body = "for (i = 0; i < n; i++) { A(); }\nfor (j = 0; j < n; j++) { B(); }\n"
        first = body.index("A()")
        end = mod._loop_scope_end(body, first, load_end=0)
        self.assertIsNotNone(end)
        self.assertNotIn("B()", body[:end])


if __name__ == "__main__":
    unittest.main()
