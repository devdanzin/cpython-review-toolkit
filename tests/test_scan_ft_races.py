"""Tests for scan_ft_races.py — free-threading data races (T1/T2/T3)."""

import unittest

from helpers import TempProject, import_script


class TestScanFtRaces(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_ft_races")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    # --- T3: iterator-exhaustion double-DECREF -----------------------------

    def test_t3_iternext_pyclear_without_lock_is_flagged(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    if (it->it_seq == NULL) return NULL;\n"
                    "    if (exhausted(it)) {\n"
                    "        Py_CLEAR(it->it_seq);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    return next_item(it);\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["type"] == "iternext_double_decref"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["ft_class"], "T3")
        self.assertEqual(f["confidence"], "high")

    def test_t3_setnull_decref_without_lock_is_flagged(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    PyObject *seq = it->it_seq;\n"
                    "    if (done(it)) {\n"
                    "        it->it_seq = NULL;\n"
                    "        Py_DECREF(seq);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    return next_item(it);\n"
                    "}\n"
                )
            }
        )
        self.assertTrue(
            any(f["type"] == "iternext_double_decref" for f in result["findings"])
        )

    def test_t3_iternext_with_critical_section_is_suppressed(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    Py_CLEAR(it->it_seq);\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T3"], [])

    def test_t3_non_iternext_is_not_flagged(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static void\n"
                    "myiter_dealloc(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    Py_CLEAR(it->it_seq);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T3"], [])

    # --- T2: lazy-init without a critical section --------------------------

    def test_t2_lazy_init_without_lock_is_flagged(self):
        result = self._findings(
            {
                "Objects/descr.c": (
                    "static PyObject *\n"
                    "descr_get_qualname(PyObject *self)\n"
                    "{\n"
                    "    MyDescr *descr = (MyDescr *)self;\n"
                    "    if (descr->d_qualname == NULL)\n"
                    "        descr->d_qualname = calculate_qualname(descr);\n"
                    "    return Py_XNewRef(descr->d_qualname);\n"
                    "}\n"
                )
            }
        )
        f = next(
            (
                f
                for f in result["findings"]
                if f["type"] == "lazy_init_no_critical_section"
            ),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["ft_class"], "T2")
        self.assertEqual(f["member"], "descr->d_qualname")

    def test_t2_bang_form_is_flagged(self):
        result = self._findings(
            {
                "Objects/x.c": (
                    "static PyObject *\n"
                    "get_cache(PyObject *self)\n"
                    "{\n"
                    "    Foo *f = (Foo *)self;\n"
                    "    if (!f->cache) {\n"
                    "        f->cache = build_cache(f);\n"
                    "    }\n"
                    "    return f->cache;\n"
                    "}\n"
                )
            }
        )
        self.assertTrue(
            any(
                f["type"] == "lazy_init_no_critical_section" for f in result["findings"]
            )
        )

    def test_t2_with_critical_section_is_suppressed(self):
        result = self._findings(
            {
                "Objects/x.c": (
                    "static PyObject *\n"
                    "get_cache(PyObject *self)\n"
                    "{\n"
                    "    Foo *f = (Foo *)self;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    if (f->cache == NULL)\n"
                    "        f->cache = build_cache(f);\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return f->cache;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T2"], [])

    def test_t2_null_check_without_assignment_is_not_flagged(self):
        result = self._findings(
            {
                "Objects/x.c": (
                    "static PyObject *\n"
                    "get_thing(PyObject *self)\n"
                    "{\n"
                    "    Foo *f = (Foo *)self;\n"
                    "    if (f->thing == NULL)\n"
                    "        return NULL;\n"
                    "    return f->thing;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T2"], [])

    # --- T1: atomic/plain access asymmetry ---------------------------------

    def test_t1_atomic_plain_asymmetry_is_flagged(self):
        result = self._findings(
            {
                "Modules/counter.c": (
                    "static PyObject *\n"
                    "count_next(CountObject *lz)\n"
                    "{\n"
                    "    Py_ssize_t c = FT_ATOMIC_LOAD_SSIZE_RELAXED(lz->cnt);\n"
                    "    return PyLong_FromSsize_t(c);\n"
                    "}\n"
                    "static PyObject *\n"
                    "count_repr(CountObject *lz)\n"
                    "{\n"
                    '    return PyUnicode_FromFormat("count(%zd)", lz->cnt);\n'
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["type"] == "atomic_plain_asymmetry"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["ft_class"], "T1")
        self.assertEqual(f["member"], "cnt")

    def test_t1_all_atomic_is_not_flagged(self):
        result = self._findings(
            {
                "Modules/counter.c": (
                    "static void bump(CountObject *lz)\n"
                    "{\n"
                    "    FT_ATOMIC_STORE_SSIZE_RELAXED(lz->cnt, "
                    "FT_ATOMIC_LOAD_SSIZE_RELAXED(lz->cnt) + 1);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T1"], [])

    def test_envelope_shape(self):
        result = self._findings({"Objects/x.c": "static void f(void) { }\n"})
        for key in (
            "project_root",
            "scan_root",
            "files_analyzed",
            "functions_analyzed",
            "findings",
            "summary",
        ):
            self.assertIn(key, result)
        self.assertIn("by_class", result["summary"])

    # --- T3: Py_SETREF(x->f, NULL), the ga_iternext SIGSEGV shape ----------

    def test_t3_setref_null_is_its_own_high_confidence_type(self):
        """The real Objects/genericaliasobject.c:952 shape (ASan SIGSEGV)."""
        result = self._findings(
            {
                "Objects/genericalias.c": (
                    "static PyObject *\n"
                    "ga_iternext(PyObject *op)\n"
                    "{\n"
                    "    gaiterobject *gi = (gaiterobject*)op;\n"
                    "    if (gi->obj == NULL) {\n"
                    "        PyErr_SetNone(PyExc_StopIteration);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    gaobject *alias = (gaobject *)gi->obj;\n"
                    "    PyObject *starred = Py_GenericAlias(alias->origin,"
                    " alias->args);\n"
                    "    if (starred == NULL) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    Py_SETREF(gi->obj, NULL);\n"
                    "    return starred;\n"
                    "}\n"
                )
            }
        )
        f = next(
            (
                f
                for f in result["findings"]
                if f["type"] == "iternext_setref_null_decref"
            ),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["ft_class"], "T3")
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["member"], "obj")
        # Exact line of the Py_SETREF, not the enclosing signature.
        self.assertEqual(f["line"], 14)
        self.assertIn("Py_DECREF(NULL)", f["detail"])

    def test_t3_xsetref_null_is_flagged(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    Py_XSETREF(it->it_seq, NULL);\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertTrue(
            any(f["type"] == "iternext_setref_null_decref" for f in result["findings"])
        )

    def test_t3_setref_to_a_real_value_is_not_a_drop(self):
        """Py_SETREF(x->f, new_value) replaces the member; it is not a drop."""
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    Py_SETREF(it->it_seq, PyList_New(0));\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T3"], [])

    # --- T3: preprocessor modelling ---------------------------------------

    def test_t3_drop_elided_under_ifndef_gil_disabled_is_clean(self):
        """The tupleiter_next / listiter_next fix strategy, already applied."""
        result = self._findings(
            {
                "Objects/tupleiter.c": (
                    "static PyObject *\n"
                    "tupleiter_next(PyObject *self)\n"
                    "{\n"
                    "    _PyTupleIterObject *it = (void *)self;\n"
                    "    PyObject *seq = it->it_seq;\n"
                    "    if (seq == NULL) return NULL;\n"
                    "    if (in_range(it, seq)) return item(it, seq);\n"
                    "#ifndef Py_GIL_DISABLED\n"
                    "    it->it_seq = NULL;\n"
                    "    Py_DECREF(seq);\n"
                    "#endif\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T3"], [])

    def test_t1_plain_access_in_gil_only_arm_is_clean(self):
        """Objects/tupleobject.c:1165 — the #else arm of #ifdef Py_GIL_DISABLED."""
        result = self._findings(
            {
                "Objects/tupleiter.c": (
                    "static PyObject *\n"
                    "tupleiter_len(PyObject *self)\n"
                    "{\n"
                    "    _PyTupleIterObject *it = (void *)self;\n"
                    "    Py_ssize_t len = 0;\n"
                    "#ifdef Py_GIL_DISABLED\n"
                    "    Py_ssize_t idx = FT_ATOMIC_LOAD_SSIZE_RELAXED(it->it_index);\n"
                    "    len = size(it) - idx;\n"
                    "#else\n"
                    "    len = size(it) - it->it_index;\n"
                    "#endif\n"
                    "    return PyLong_FromSsize_t(len);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T1"], [])

    def test_gil_disabled_region_split(self):
        gil_only, ft_only = self.mod._gil_disabled_regions(
            "a\n#ifdef Py_GIL_DISABLED\nb\n#else\nc\n#endif\nd\n"
        )
        self.assertTrue(self.mod._in_ranges(3, ft_only))
        self.assertTrue(self.mod._in_ranges(5, gil_only))
        self.assertFalse(self.mod._in_ranges(1, gil_only))
        self.assertFalse(self.mod._in_ranges(7, ft_only))

    # --- T2: the guarded twin discriminator --------------------------------

    def test_t2_partial_guard_is_high_confidence(self):
        """Objects/genericaliasobject.c: gh-153298 guarded one of two accessors."""
        result = self._findings(
            {
                "Objects/genericalias.c": (
                    "static PyObject *\n"
                    "ga_getitem(PyObject *self, PyObject *item)\n"
                    "{\n"
                    "    gaobject *alias = (gaobject *)self;\n"
                    "    if (alias->parameters == NULL) {\n"
                    "        alias->parameters = _Py_make_parameters(alias->args);\n"
                    "    }\n"
                    "    return subs(self, alias->parameters, item);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "ga_parameters_lock_held(PyObject *self)\n"
                    "{\n"
                    "    gaobject *alias = (gaobject *)self;\n"
                    "    if (alias->parameters == NULL) {\n"
                    "        alias->parameters = _Py_make_parameters(alias->args);\n"
                    "    }\n"
                    "    return Py_NewRef(alias->parameters);\n"
                    "}\n"
                )
            }
        )
        partial = [
            f for f in result["findings"] if f["type"] == "lazy_init_partial_guard"
        ]
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0]["confidence"], "high")
        self.assertEqual(partial[0]["function"], "ga_getitem")
        self.assertEqual(partial[0]["line"], 5)
        self.assertIn("ga_parameters_lock_held", partial[0]["guarded_twin"])
        # The guarded accessor itself is never a finding.
        self.assertFalse(
            any(f.get("function") == "ga_parameters_lock_held" for f in partial)
        )

    def test_t2_single_unguarded_accessor_stays_medium(self):
        """No twin -> a bare lazy init is often a single-threaded init path."""
        result = self._findings(
            {
                "Objects/descr.c": (
                    "static PyObject *\n"
                    "descr_get_qualname(PyObject *self)\n"
                    "{\n"
                    "    PyDescrObject *descr = (PyDescrObject *)self;\n"
                    "    if (descr->d_qualname == NULL)\n"
                    "        descr->d_qualname = calculate_qualname(descr);\n"
                    "    return Py_XNewRef(descr->d_qualname);\n"
                    "}\n"
                )
            }
        )
        t2 = [f for f in result["findings"] if f["ft_class"] == "T2"]
        self.assertEqual(len(t2), 1)
        self.assertEqual(t2[0]["type"], "lazy_init_no_critical_section")
        self.assertEqual(t2[0]["confidence"], "medium")
        self.assertEqual(t2[0]["line"], 5)

    def test_t2_one_helper_two_callers_is_clean(self):
        """Objects/unionobject.c union_init_parameters — the correct pattern."""
        result = self._findings(
            {
                "Objects/union.c": (
                    "static int\n"
                    "union_init_parameters(unionobject *alias)\n"
                    "{\n"
                    "    int result = 0;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(alias);\n"
                    "    if (alias->parameters == NULL) {\n"
                    "        alias->parameters = _Py_make_parameters(alias->args);\n"
                    "    }\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return result;\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "union_getitem(PyObject *self, PyObject *item)\n"
                    "{\n"
                    "    unionobject *alias = (unionobject *)self;\n"
                    "    if (union_init_parameters(alias) < 0) return NULL;\n"
                    "    return subs(self, alias->parameters, item);\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "union_parameters(PyObject *self, void *unused)\n"
                    "{\n"
                    "    unionobject *alias = (unionobject *)self;\n"
                    "    if (union_init_parameters(alias) < 0) return NULL;\n"
                    "    return Py_NewRef(alias->parameters);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T2"], [])

    def test_t2_stack_local_aggregate_is_clean(self):
        """Objects/unionobject.c:173 — `unionbuilder ub;` is never shared."""
        result = self._findings(
            {
                "Objects/union.c": (
                    "static int\n"
                    "unionbuilder_add_single_unchecked(unionbuilder *ub, PyObject *a)\n"
                    "{\n"
                    "    if (ub->unhashable_args == NULL) {\n"
                    "        ub->unhashable_args = PyList_New(0);\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                    "\n"
                    "static PyObject *\n"
                    "make_union(PyObject *args)\n"
                    "{\n"
                    "    unionbuilder ub;\n"
                    "    unionbuilder_add_single_unchecked(&ub, args);\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T2"], [])

    # --- the *_lock_held convention ----------------------------------------

    def test_lock_held_callee_is_suppressed_but_counted(self):
        result = self._findings(
            {
                "Objects/od.c": (
                    "static PyObject *\n"
                    "odictiter_iternext_lock_held(PyObject *op)\n"
                    "{\n"
                    "    odictiterobject *di = (odictiterobject *)op;\n"
                    "    Py_CLEAR(di->di_odict);\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T3"], [])
        self.assertEqual(result["lock_held_functions"], 1)

    def test_lock_held_suffix_does_not_suppress_a_lookalike(self):
        """`*_locked_ref` is not the convention; only the exact suffixes are."""
        result = self._findings(
            {
                "Objects/od.c": (
                    "static PyObject *\n"
                    "myiter_iternext_locked_ref(PyObject *op)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)op;\n"
                    "    Py_CLEAR(it->it_seq);\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertTrue(
            any(f["ft_class"] == "T3" for f in result["findings"]),
        )
        self.assertEqual(result["lock_held_functions"], 0)

    # --- CPython-specific: positional tp_iternext slots + comment text -----

    def test_positional_tp_iternext_slot_is_recognised(self):
        """Objects/ wires iternext positionally with a /* tp_iternext */ marker."""
        result = self._findings(
            {
                "Objects/bytes.c": (
                    "static PyObject *\n"
                    "striter_next(PyObject *op)\n"
                    "{\n"
                    "    striterobject *it = (striterobject *)op;\n"
                    "    PyObject *seq = it->it_seq;\n"
                    "    if (in_range(it, seq)) return item(it, seq);\n"
                    "    it->it_seq = NULL;\n"
                    "    Py_DECREF(seq);\n"
                    "    return NULL;\n"
                    "}\n"
                    "\n"
                    "PyTypeObject PyBytesIter_Type = {\n"
                    "    PyVarObject_HEAD_INIT(&PyType_Type, 0)\n"
                    '    "bytes_iterator",                    /* tp_name */\n'
                    "    PyObject_SelfIter,                   /* tp_iter */\n"
                    "    striter_next,                        /* tp_iternext */\n"
                    "};\n"
                )
            }
        )
        self.assertTrue(
            any(f["type"] == "iternext_double_decref" for f in result["findings"])
        )
        self.assertEqual(result["iternext_functions"], 1)

    def test_field_access_inside_a_comment_is_not_an_access(self):
        """Objects/weakrefobject.c:112 mentions self->wr_object in prose."""
        result = self._findings(
            {
                "Objects/weakref.c": (
                    "static void\n"
                    "store_it(PyWeakReference *self, PyObject *ob)\n"
                    "{\n"
                    "    FT_ATOMIC_STORE_PTR(self->wr_object, ob);\n"
                    "}\n"
                    "\n"
                    "static void\n"
                    "clear_it(PyObject *op)\n"
                    "{\n"
                    "    PyWeakReference *self = (PyWeakReference *)op;\n"
                    "    // self->wr_object may be Py_None if the GC cleared it\n"
                    "    LOCK_WEAKREFS_FOR_WR(self);\n"
                    "    UNLOCK_WEAKREFS_FOR_WR(self);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T1"], [])

    def test_strip_comments_preserves_line_numbers(self):
        stripped = self.mod.strip_comments("a\n/* one\ntwo\nthree */\nb\n")
        self.assertEqual(len(stripped.split("\n")), 6)
        self.assertNotIn("two", stripped)
        self.assertEqual(stripped.split("\n")[4], "b")

    def test_strip_comments_keeps_string_literals_intact(self):
        stripped = self.mod.strip_comments('char *u = "http://example";\n')
        self.assertIn("http://example", stripped)


if __name__ == "__main__":
    unittest.main()
