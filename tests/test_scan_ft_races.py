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

    def test_t3_drop_after_the_critical_section_closes_is_flagged(self):
        """The setiter_iternext shape: a lock is present, the drop is outside it.

        Objects/setobject.c ends its critical section at :1127 and drops the
        owning reference at :1130-1131. Suppressing on "the body contains a
        lock" hid TSAN-0053, TSAN-0054 and TSAN-0062 — every instance of the
        class this rule exists for.
        """
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    PyObject *key = NULL;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    key = it->it_key;\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    if (key == NULL) {\n"
                    "        Py_CLEAR(it->it_seq);\n"
                    "    }\n"
                    "    return key;\n"
                    "}\n"
                )
            }
        )
        t3 = [f for f in result["findings"] if f["ft_class"] == "T3"]
        self.assertEqual(len(t3), 1, t3)
        self.assertEqual(t3[0]["type"], "iternext_double_decref")
        self.assertEqual(t3[0]["member"], "it_seq")

    def test_t3_opaque_lock_still_suppresses_the_whole_function(self):
        """A PyMutex has no delimitable region, so stay conservative there."""
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    PyMutex_Lock(&it->it_mutex);\n"
                    "    PyMutex_Unlock(&it->it_mutex);\n"
                    "    Py_CLEAR(it->it_seq);\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T3"], [])

    def test_t3_screaming_case_lock_macro_still_suppresses(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    LOCK_WEAKREFS(self);\n"
                    "    Py_CLEAR(it->it_seq);\n"
                    "    UNLOCK_WEAKREFS(self);\n"
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
                    "    FT_ATOMIC_STORE_SSIZE_RELAXED(lz->cnt, c + 1);\n"
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
        # The member is qualified by the receiver's declared struct type, so a
        # same-named field on an unrelated struct cannot pair with it.
        self.assertEqual(f["member"], "CountObject.cnt")
        self.assertEqual(f["function"], "count_repr")

    def test_t1_needs_a_synchronised_write_not_just_a_read(self):
        # Everything only ever *reads* the field under synchronisation: nothing
        # stores to it, so there is no race to report.
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
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T1"], [])

    # --- T1 retarget: guarded writer / unguarded reader ---------------------

    # Modules/itertoolsmodule.c count_nextlong / count_repr, the gh-153908
    # incomplete fix: the writer runs under a critical section its *caller*
    # takes, the reader takes nothing, and the field is a pointer handed to
    # PyObject_Repr — so the writer can free it under the reader.
    COUNT_SHAPE = (
        "typedef struct {\n"
        "    PyObject_HEAD\n"
        "    Py_ssize_t cnt;\n"
        "    PyObject *long_cnt;\n"
        "    PyObject *long_step;\n"
        "} countobject;\n"
        "\n"
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
        "\n"
        "static PyObject *\n"
        "count_next(PyObject *op)\n"
        "{\n"
        "    countobject *lz = countobject_CAST(op);\n"
        "    PyObject *returned;\n"
        "    Py_BEGIN_CRITICAL_SECTION(lz);\n"
        "    returned = count_nextlong(lz);\n"
        "    Py_END_CRITICAL_SECTION();\n"
        "    return returned;\n"
        "}\n"
        "\n"
        "static PyObject *\n"
        "count_repr(PyObject *op)\n"
        "{\n"
        "    countobject *lz = countobject_CAST(op);\n"
        '    return PyUnicode_FromFormat("%R", lz->long_cnt);\n'
        "}\n"
    )

    def test_t1_guarded_writer_unguarded_reader_is_flagged(self):
        result = self._findings({"Modules/itertoolsmodule.c": self.COUNT_SHAPE})
        hits = [
            f
            for f in result["findings"]
            if f["type"] == "guarded_writer_unguarded_reader"
        ]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "count_repr")
        self.assertEqual(hits[0]["member"], "countobject.long_cnt")
        # The writer holds no lock itself: only its caller does. Without the
        # one-hop caller check there is no guarded twin and no finding.
        self.assertTrue(hits[0]["guarded_twin"].startswith("count_nextlong:"))
        # A pointer field outranks a scalar one — a stale pointer is a UAF.
        self.assertEqual(hits[0]["confidence"], "medium")

    def test_t1_lock_held_reader_is_the_guarded_twin_not_a_finding(self):
        guarded = self.COUNT_SHAPE.replace(
            "count_repr(PyObject *op)", "count_repr_lock_held(PyObject *op)"
        )
        result = self._findings({"Modules/itertoolsmodule.c": guarded})
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T1"], [])

    def test_t1_emits_one_finding_per_site_not_per_field(self):
        # The collapse this replaces reported one finding per field name per
        # file, so the second and third reader sites vanished.
        three_reads = self.COUNT_SHAPE.replace(
            '    return PyUnicode_FromFormat("%R", lz->long_cnt);\n',
            "    if (lz->long_cnt == NULL)\n"
            '        return PyUnicode_FromFormat("count()");\n'
            '    return PyUnicode_FromFormat("%R", lz->long_cnt);\n',
        )
        result = self._findings({"Modules/itertoolsmodule.c": three_reads})
        lines = sorted(
            f["line"]
            for f in result["findings"]
            if f["type"] == "guarded_writer_unguarded_reader"
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(len(set(lines)), 2)

    def test_t1_pairs_by_receiver_type_not_by_member_name(self):
        # isliceobject.cnt and countobject.cnt are different fields that happen
        # to share a name; islice's is never touched atomically.
        islice = (
            "static PyObject *\n"
            "islice_next(PyObject *op)\n"
            "{\n"
            "    isliceobject *lz = (isliceobject *)op;\n"
            "    while (lz->cnt < lz->next) {\n"
            "        lz->cnt++;\n"
            "    }\n"
            "    return NULL;\n"
            "}\n"
        )
        result = self._findings(
            {"Modules/itertoolsmodule.c": islice + self.COUNT_SHAPE}
        )
        members = {f["member"] for f in result["findings"] if f["ft_class"] == "T1"}
        self.assertNotIn("isliceobject.cnt", members)

    def test_t1_clinic_critical_section_wrapper_counts_as_a_lock(self):
        # The lock lives in the generated wrapper, not in the _impl body.
        src = (
            "/*[clinic input]\n"
            "@critical_section\n"
            "_io.BytesIO.read\n"
            "[clinic start generated code]*/\n"
            "static PyObject *\n"
            "_io_BytesIO_read_impl(bytesio *self)\n"
            "{\n"
            "    self->pos += 1;\n"
            "    return PyLong_FromSsize_t(self->pos);\n"
            "}\n"
            "static PyObject *\n"
            "write_bytes_lock_held(bytesio *self)\n"
            "{\n"
            "    self->pos = 0;\n"
            "    return NULL;\n"
            "}\n"
        )
        result = self._findings({"Modules/_io/bytesio.c": src})
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T1"], [])

    def test_t1_pre_publication_write_in_a_constructor_is_not_a_race(self):
        src = (
            "static PyObject *\n"
            "dequeiter_new(PyTypeObject *type, dequeobject *deque)\n"
            "{\n"
            "    dequeiterobject *it = (dequeiterobject *)type->tp_alloc(type, 0);\n"
            "    it->counter = deque->state;\n"
            "    return (PyObject *)it;\n"
            "}\n"
            "static PyObject *\n"
            "dequeiter_next_lock_held(dequeiterobject *it)\n"
            "{\n"
            "    it->counter--;\n"
            "    return NULL;\n"
            "}\n"
        )
        result = self._findings({"Modules/_collectionsmodule.c": src})
        self.assertEqual(
            [
                f
                for f in result["findings"]
                if f["ft_class"] == "T1" and f["function"] == "dequeiter_new"
            ],
            [],
        )

    def test_t1_atomic_reader_outside_the_lock_races_the_plain_writer(self):
        # _collectionsmodule.c CON-1, reproduced under TSan: the two
        # synchronisation disciplines do not compose.
        src = (
            "static PyObject *\n"
            "dequeiter_next_lock_held(dequeiterobject *it)\n"
            "{\n"
            "    it->counter--;\n"
            "    return NULL;\n"
            "}\n"
            "static PyObject *\n"
            "dequeiter_len(PyObject *op)\n"
            "{\n"
            "    dequeiterobject *it = (dequeiterobject *)op;\n"
            "    return PyLong_FromSsize_t(FT_ATOMIC_LOAD_SSIZE(it->counter));\n"
            "}\n"
        )
        result = self._findings({"Modules/_collectionsmodule.c": src})
        hits = [
            f
            for f in result["findings"]
            if f["ft_class"] == "T1" and "do not compose" in f["detail"]
        ]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["function"], "dequeiter_next_lock_held")

    def test_t1_stack_local_aggregate_is_never_shared(self):
        src = (
            "static void\n"
            "w_reserve(WFILE *p)\n"
            "{\n"
            "    Py_BEGIN_CRITICAL_SECTION(p);\n"
            "    p->buf = grow(p->buf);\n"
            "    Py_END_CRITICAL_SECTION();\n"
            "}\n"
            "static void\n"
            "w_flush(WFILE *p)\n"
            "{\n"
            "    flush(p->buf);\n"
            "}\n"
            "static void\n"
            "marshal(void)\n"
            "{\n"
            "    WFILE wf;\n"
            "    w_reserve(&wf);\n"
            "}\n"
        )
        result = self._findings({"Python/marshal.c": src})
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T1"], [])

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


# ---------------------------------------------------------------------------
# T4 — publish_before_init_complete (issue #28 rule 9)
# ---------------------------------------------------------------------------
#
# `type_new_impl` calls `PyType_Ready(type)`, which links the type into every
# base's `tp_subclasses` and every subclass's MRO — reachable from other threads
# from that instant — and *then* calls `fixup_slot_dispatchers(type)`, which
# rewrites the slot table with plain stores (gh-151377, CPY-0072). The stores
# are in the callee, so a rule that only reads the publishing function's own
# body sees nothing.

PUBLISH_SHAPE = """\
#include "Python.h"

static void
update_one_slot(PyTypeObject *type, slotdef *p)
{
    void **ptr = slotptr(type, p->offset);
    *ptr = p->function;
}

static void
fixup_slot_dispatchers(PyTypeObject *type)
{
    for (slotdef *p = slotdefs; p->name; p++) {
        update_one_slot(type, p);
    }
}

static PyObject *
type_new_impl(type_new_ctx *ctx)
{
    PyTypeObject *type = type_new_init(ctx);
    if (PyType_Ready(type) < 0) {
        return NULL;
    }
    fixup_slot_dispatchers(type);
    return (PyObject *)type;
}
"""


class TestPublishBeforeInitComplete(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_ft_races")

    def _findings(self, files):
        with TempProject(files) as root:
            result = self.mod.analyze(str(root))
        return [
            f
            for f in result["findings"]
            if f["type"] == "publish_before_init_complete"
        ]

    def test_the_reproduced_shape(self):
        found = self._findings({"Objects/typeobject.c": PUBLISH_SHAPE})
        hits = [f for f in found if f["function"] == "type_new_impl"]
        self.assertEqual(len(hits), 1, found)
        self.assertEqual(hits[0]["publish_api"], "PyType_Ready")
        self.assertLess(hits[0]["publish_line"], hits[0]["line"])
        self.assertIn("fixup_slot_dispatchers", hits[0]["written_in"])
        self.assertEqual(hits[0]["ft_class"], "T4")

    def test_direct_store_after_publish(self):
        code = (
            "static int\n"
            "register_it(PyObject *d, PyObject *k, MyObject *self)\n"
            "{\n"
            "    if (PyDict_SetItem(d, k, (PyObject *)self) < 0) {\n"
            "        return -1;\n"
            "    }\n"
            "    self->ready = 1;\n"
            "    return 0;\n"
            "}\n"
        )
        found = self._findings({"Objects/x.c": code})
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["call_hops"], 0)
        self.assertEqual(found[0]["confidence"], "medium")

    def test_stores_before_the_publish_are_not_flagged(self):
        code = (
            "static int\n"
            "register_it(PyObject *d, PyObject *k, MyObject *self)\n"
            "{\n"
            "    self->ready = 1;\n"
            "    return PyDict_SetItem(d, k, (PyObject *)self);\n"
            "}\n"
        )
        self.assertEqual(self._findings({"Objects/x.c": code}), [])

    def test_a_critical_section_suppresses(self):
        code = (
            "static int\n"
            "register_it(PyObject *d, PyObject *k, MyObject *self)\n"
            "{\n"
            "    Py_BEGIN_CRITICAL_SECTION(self);\n"
            "    if (PyDict_SetItem(d, k, (PyObject *)self) < 0) {\n"
            "        return -1;\n"
            "    }\n"
            "    self->ready = 1;\n"
            "    Py_END_CRITICAL_SECTION();\n"
            "    return 0;\n"
            "}\n"
        )
        self.assertEqual(self._findings({"Objects/x.c": code}), [])

    def test_confidence_degrades_with_call_depth(self):
        """One hop is checkable by opening the callee; four is a chain."""
        found = self._findings({"Objects/typeobject.c": PUBLISH_SHAPE})
        for f in found:
            expected = "medium" if f["call_hops"] <= 1 else "low"
            self.assertEqual(f["confidence"], expected, f)

    def test_balanced_paren_argument_extraction(self):
        """`if (PyType_Ready(type) < 0)` -- a greedy `\\(([^;]*)\\)` captures
        `type) < 0` and the argument stops being an identifier."""
        text = "if (PyType_Ready(type) < 0) { return NULL; }"
        open_paren = text.index("(", text.index("PyType_Ready"))
        args, end = self.mod._call_args_at(text, open_paren)
        self.assertEqual(args, "type")
        self.assertEqual(text[end - 1], ")")


class TestInteriorPointerAlias(unittest.TestCase):
    """`PyTypeObject *type = &res->ht_type;` (Objects/typeobject.c:5628)."""

    def setUp(self):
        self.mod = import_script("scan_ft_races")

    def test_interior_alias_of_a_fresh_allocation(self):
        body = (
            "    PyHeapTypeObject *res = PyType_GenericAlloc(meta, nmembers);\n"
            "    PyTypeObject *type = &res->ht_type;\n"
            "    type->tp_flags = flags;\n"
        )
        self.assertTrue(
            self.mod._is_freshly_allocated(body, "type", body.index("type->tp_flags"))
        )

    def test_cast_alias_of_a_fresh_allocation(self):
        body = (
            "    PyObject *self = PyObject_GC_New(MyObject, tp);\n"
            "    MyObject *obj = (MyObject *)self;\n"
            "    obj->field = NULL;\n"
        )
        self.assertTrue(
            self.mod._is_freshly_allocated(body, "obj", body.index("obj->field"))
        )

    def test_an_unrelated_local_is_not_fresh(self):
        body = (
            "    PyObject *self = PyObject_GC_New(MyObject, tp);\n"
            "    MyObject *other = get_shared();\n"
            "    other->field = NULL;\n"
        )
        self.assertFalse(
            self.mod._is_freshly_allocated(body, "other", body.index("other->field"))
        )

    def test_the_alias_must_precede_the_use(self):
        body = (
            "    type->tp_flags = flags;\n"
            "    PyHeapTypeObject *res = PyType_GenericAlloc(meta, 0);\n"
            "    PyTypeObject *type = &res->ht_type;\n"
        )
        self.assertFalse(
            self.mod._is_freshly_allocated(body, "type", body.index("type->tp_flags"))
        )


if __name__ == "__main__":
    unittest.main()
