"""Tests for scan_recursion_guards.py — recursion-prone descents lacking a guard.

The organising fact these tests encode (verified against CPython main @
3.16.0a0): of the four element-descent dispatchers only ``PyObject_Hash``
(``Objects/object.c:1158``) lacks ``_Py_EnterRecursiveCallTstate``. So a
``tp_hash`` descent is unguarded at every level and segfaults, while a
repr/str/richcompare descent is bounded by its dispatcher and raises
``RecursionError``.
"""

import unittest

from helpers import TempProject, import_script


class TestScanRecursionGuards(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_recursion_guards")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _get(self, result, name):
        return next((f for f in result["findings"] if f["function"] == name), None)

    # --- true positives ----------------------------------------------------

    def test_container_hash_without_guard_is_flagged(self):
        # The tuple_hash class (gh-154318, Objects/tupleobject.c:385): loop over
        # items calling PyObject_Hash with no Py_EnterRecursiveCall.
        result = self._findings(
            {
                "Objects/mytuple.c": (
                    "static Py_hash_t\n"
                    "mytuple_hash(PyObject *op)\n"
                    "{\n"
                    "    MyTupleObject *v = (MyTupleObject *)op;\n"
                    "    Py_ssize_t len = Py_SIZE(v);\n"
                    "    Py_uhash_t acc = 0;\n"
                    "    for (Py_ssize_t i = 0; i < len; i++) {\n"
                    "        Py_hash_t y = PyObject_Hash(v->ob_item[i]);\n"
                    "        acc = (acc * 1000003) ^ (Py_uhash_t)y;\n"
                    "    }\n"
                    "    return (Py_hash_t)acc;\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "mytuple_hash")
        self.assertIsNotNone(f)
        self.assertEqual(f["type"], "missing_recursion_guard")
        self.assertEqual(f["slot"], "tp_hash")
        self.assertEqual(f["shape"], "container_element_descent")
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["element_op"], "PyObject_Hash")

    def test_fixed_arity_field_hash_is_flagged(self):
        # Regression for the has_container gate (ga_hash,
        # Objects/genericaliasobject.c:615/619). No loop, no *_GET_ITEM — the
        # old rule could never reach this, yet it is a reproduced SIGSEGV.
        result = self._findings(
            {
                "Objects/genericalias.c": (
                    "static Py_hash_t\n"
                    "ga_hash(PyObject *self)\n"
                    "{\n"
                    "    gaobject *alias = (gaobject *)self;\n"
                    "    Py_hash_t h0 = PyObject_Hash(alias->origin);\n"
                    "    if (h0 == -1) { return -1; }\n"
                    "    Py_hash_t h1 = PyObject_Hash(alias->args);\n"
                    "    if (h1 == -1) { return -1; }\n"
                    "    return h0 ^ h1;\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "ga_hash")
        self.assertIsNotNone(f)
        self.assertEqual(f["shape"], "field_element_descent")
        self.assertEqual(f["confidence"], "high")
        self.assertFalse(f["tail_call"])
        # Both descent sites are reported, not just the first.
        self.assertEqual([s["line"] for s in f["sites"]], [5, 7])

    def test_lock_held_helper_is_classified_by_its_slot(self):
        # The weakref_hash / weakref_hash_lock_held split
        # (Objects/weakrefobject.c:199). The registered slot is a critical-
        # section wrapper; the descent lives in the _lock_held helper, whose
        # name matches no slot suffix.
        result = self._findings(
            {
                "Objects/myweakref.c": (
                    "static Py_hash_t\n"
                    "weakref_hash_lock_held(PyWeakReference *self)\n"
                    "{\n"
                    "    PyObject *obj = _PyWeakref_GET_REF((PyObject *)self);\n"
                    "    self->hash = PyObject_Hash(obj);\n"
                    "    Py_DECREF(obj);\n"
                    "    return self->hash;\n"
                    "}\n"
                    "static Py_hash_t\n"
                    "weakref_hash(PyObject *op)\n"
                    "{\n"
                    "    PyWeakReference *self = _PyWeakref_CAST(op);\n"
                    "    Py_hash_t hash;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    hash = weakref_hash_lock_held(self);\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return hash;\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "weakref_hash_lock_held")
        self.assertIsNotNone(f)
        self.assertEqual(f["slot"], "tp_hash")
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["line"], 5)

    def test_slot_helper_hashing_a_parameter_is_promoted(self):
        # frozendict_hash -> frozendict_pair_hash (Objects/dictobject.c:8427),
        # the formalized tuple_hash copy-paste. The helper hashes a *parameter*,
        # so it is only additive because a recursion-prone slot drives it in a
        # loop — that promotion is what makes it high rather than an entry point.
        result = self._findings(
            {
                "Objects/mydict.c": (
                    "static Py_hash_t\n"
                    "frozendict_pair_hash(Py_hash_t key_hash, PyObject *value)\n"
                    "{\n"
                    "    Py_hash_t lane = PyObject_Hash(value);\n"
                    "    return lane ^ key_hash;\n"
                    "}\n"
                    "static Py_hash_t\n"
                    "frozendict_hash(PyObject *op)\n"
                    "{\n"
                    "    PyDictObject *mp = (PyDictObject *)op;\n"
                    "    Py_uhash_t acc = 0;\n"
                    "    while (_PyDict_Next(op, &pos, &key, &value, &key_hash)) {\n"
                    "        acc ^= frozendict_pair_hash(key_hash, value);\n"
                    "    }\n"
                    "    return (Py_hash_t)acc;\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "frozendict_pair_hash")
        self.assertIsNotNone(f)
        self.assertEqual(f["shape"], "slot_helper_descent")
        self.assertEqual(f["confidence"], "high")

    def test_self_recursive_parameter_walk_is_flagged(self):
        # The _Py_make_parameters class (gh-154275): self-recursion, no guard.
        result = self._findings(
            {
                "Objects/genericaliasobject.c": (
                    "static PyObject *\n"
                    "make_parameters(PyObject *args)\n"
                    "{\n"
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        PyObject *t = PyTuple_GET_ITEM(args, i);\n"
                    "        PyObject *sub = make_parameters(t);\n"
                    "    }\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "make_parameters")
        self.assertIsNotNone(f)
        self.assertEqual(f["slot"], "parameter_walk")
        self.assertEqual(f["shape"], "self_recursion")
        self.assertEqual(f["confidence"], "high")

    def test_two_self_recursions_in_one_file_are_both_reported(self):
        # _Py_make_parameters (:231) and _Py_subs_parameters (:482) are two
        # distinct unguarded walks in genericaliasobject.c. The shared
        # deduplicate_findings() normalises quoted identifiers out of the
        # detail text and collapsed them into one; this scanner must not.
        result = self._findings(
            {
                "Objects/genericaliasobject.c": (
                    "static PyObject *\n"
                    "_Py_make_parameters(PyObject *args)\n"
                    "{\n"
                    "    PyObject *t = PyTuple_GET_ITEM(args, i);\n"
                    "    return _Py_make_parameters(t);\n"
                    "}\n"
                    "static PyObject *\n"
                    "_Py_subs_parameters(PyObject *self, PyObject *args,\n"
                    "                    PyObject *parameters, PyObject *item)\n"
                    "{\n"
                    "    PyObject *arg = PyTuple_GET_ITEM(args, i);\n"
                    "    return _Py_subs_parameters(self, arg, parameters, item);\n"
                    "}\n"
                )
            }
        )
        names = {f["function"] for f in result["findings"]}
        self.assertIn("_Py_make_parameters", names)
        self.assertIn("_Py_subs_parameters", names)

    def test_hash_of_a_parameter_is_surfaced_as_an_entry_point(self):
        # unionbuilder_add_single_unchecked (Objects/unionobject.c:170) crashes
        # `int | <deep alias>` at construction, but the recursive frames belong
        # to ga_hash/tuple_hash: it adds exactly one. Surfaced, at low.
        result = self._findings(
            {
                "Objects/unionobject.c": (
                    "static bool\n"
                    "unionbuilder_add_single_unchecked(unionbuilder *ub, PyObject *arg)\n"
                    "{\n"
                    "    Py_hash_t hash = PyObject_Hash(arg);\n"
                    "    if (hash == -1) { return false; }\n"
                    "    return true;\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "unionbuilder_add_single_unchecked")
        self.assertIsNotNone(f)
        self.assertEqual(f["shape"], "hash_entry_point")
        self.assertEqual(f["confidence"], "low")

    # --- true negatives ----------------------------------------------------

    def test_guarded_recursion_is_suppressed(self):
        result = self._findings(
            {
                "Objects/mytuple.c": (
                    "static Py_hash_t\n"
                    "mytuple_hash(PyObject *self)\n"
                    "{\n"
                    '    if (Py_EnterRecursiveCall(" while hashing")) return -1;\n'
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        PyObject_Hash(PyTuple_GET_ITEM(self, i));\n"
                    "    }\n"
                    "    Py_LeaveRecursiveCall();\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_repr_with_reprenter_is_suppressed(self):
        result = self._findings(
            {
                "Objects/mylist.c": (
                    "static PyObject *\n"
                    "mylist_repr(PyObject *self)\n"
                    "{\n"
                    "    if (Py_ReprEnter(self) != 0) return NULL;\n"
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        PyObject_Repr(PyList_GET_ITEM(self, i));\n"
                    "    }\n"
                    "    Py_ReprLeave(self);\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_richcompare_descent_is_the_dispatcher_guarded_twin(self):
        # tuple_richcompare (Objects/tupleobject.c:755) is the guarded twin of
        # the whole class: PyObject_RichCompare (Objects/object.c:1099) wraps
        # _Py_EnterRecursiveCallTstate, so 1M-deep `a == b` raises a clean
        # RecursionError. It must not count as a missing guard.
        result = self._findings(
            {
                "Objects/myseq.c": (
                    "static PyObject *\n"
                    "myseq_cmp(PyObject *a, PyObject *b, int op)\n"
                    "{\n"
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        int r = PyObject_RichCompareBool("
                    "PyTuple_GET_ITEM(a, i), PyTuple_GET_ITEM(b, i), Py_EQ);\n"
                    "        if (r < 0) return NULL;\n"
                    "    }\n"
                    "    Py_RETURN_TRUE;\n"
                    "}\n"
                    "static PyTypeObject Seq_Type = {\n"
                    "    .tp_richcompare = myseq_cmp,\n"
                    "};\n"
                )
            }
        )
        f = self._get(result, "myseq_cmp")
        self.assertIsNotNone(f)
        self.assertEqual(f["type"], "recursion_descent_guarded_by_dispatcher")
        self.assertEqual(f["confidence"], "low")
        self.assertTrue(
            any("Objects/object.c:1099" in site for site in f["guarded_by"]),
            f["guarded_by"],
        )
        self.assertEqual(result["summary"]["missing_recursion_guard"], 0)

    def test_opcode_delegating_self_call_is_not_a_descent(self):
        # ga_richcompare / set_richcompare re-enter with the *same* operands
        # and a different opcode for Py_NE: bounded at one extra frame.
        result = self._findings(
            {
                "Objects/mygeneric.c": (
                    "static PyObject *\n"
                    "ga_richcompare(PyObject *a, PyObject *b, int op)\n"
                    "{\n"
                    "    if (op == Py_NE) {\n"
                    "        PyObject *eq = ga_richcompare(a, b, Py_EQ);\n"
                    "        if (eq == NULL) return NULL;\n"
                    "        Py_DECREF(eq);\n"
                    "        Py_RETURN_TRUE;\n"
                    "    }\n"
                    "    Py_RETURN_FALSE;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_hashing_a_freshly_built_scalar_is_not_flagged(self):
        # channelid_hash (Modules/_interpchannelsmodule.c:2495) hashes a fresh
        # int built from a receiver field: the descent bound is exactly 0.
        result = self._findings(
            {
                "Objects/mychannel.c": (
                    "static Py_hash_t\n"
                    "channelid_hash(PyObject *self)\n"
                    "{\n"
                    "    channelid *cidobj = channelid_CAST(self);\n"
                    "    PyObject *pyid = PyLong_FromLongLong(cidobj->cid);\n"
                    "    if (pyid == NULL) { return -1; }\n"
                    "    Py_hash_t hash = PyObject_Hash(pyid);\n"
                    "    Py_DECREF(pyid);\n"
                    "    return hash;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_non_slot_self_recursion_is_not_flagged(self):
        # A plain helper that self-recurses but is not a recursion-prone slot
        # is out of scope (keeps false positives down).
        result = self._findings(
            {
                "Objects/util.c": (
                    "static int\n"
                    "count_down(int n)\n"
                    "{\n"
                    "    if (n <= 0) return 0;\n"
                    "    return count_down(n - 1);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_comment_suppression(self):
        result = self._findings(
            {
                "Objects/mytuple.c": (
                    "static Py_hash_t\n"
                    "mytuple_hash(PyObject *self)\n"
                    "{\n"
                    "    /* safe because elements are guaranteed non-container scalars */\n"
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        PyObject_Hash(PyTuple_GET_ITEM(self, i));\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    # --- CPython-specific edge cases ---------------------------------------

    def test_bare_tail_call_descent_is_capped_at_medium(self):
        # mappingproxy_hash (Objects/descrobject.c:1207) is source-identical to
        # the FIX class, but its single descent is a bare tail call that clang
        # -O turns into a jump: 3,000,000 levels deep does not crash. Report it
        # honestly rather than as a reproducible SIGSEGV.
        result = self._findings(
            {
                "Objects/descrobject.c": (
                    "static Py_hash_t\n"
                    "mappingproxy_hash(PyObject *self)\n"
                    "{\n"
                    "    mappingproxyobject *pp = (mappingproxyobject *)self;\n"
                    "    return PyObject_Hash(pp->mapping);\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "mappingproxy_hash")
        self.assertIsNotNone(f)
        self.assertTrue(f["tail_call"])
        self.assertEqual(f["confidence"], "medium")

    def test_locally_built_container_is_medium(self):
        # range_hash (Objects/rangeobject.c:624) hashes a tuple it packs from
        # receiver fields: one level, bounded by the nestability of the fields.
        result = self._findings(
            {
                "Objects/rangeobject.c": (
                    "static Py_hash_t\n"
                    "range_hash(PyObject *op)\n"
                    "{\n"
                    "    rangeobject *r = (rangeobject *)op;\n"
                    "    PyObject *t = PyTuple_New(3);\n"
                    "    PyTuple_SET_ITEM(t, 0, Py_NewRef(r->length));\n"
                    "    Py_hash_t result = PyObject_Hash(t);\n"
                    "    Py_DECREF(t);\n"
                    "    return result;\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "range_hash")
        self.assertIsNotNone(f)
        self.assertEqual(f["shape"], "temporary_container_descent")
        self.assertEqual(f["confidence"], "medium")

    def test_dispatcher_guard_model_is_reported(self):
        result = self._findings({"Objects/empty.c": "void f(void) {}\n"})
        model = result["dispatcher_guard_model"]
        self.assertIn("PyObject_Hash", model["unguarded"])
        self.assertNotIn("PyObject_Hash", model["guarded"])
        self.assertIn("PyObject_RichCompare", model["guarded"])


class TestHelpers(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_recursion_guards")

    def test_strip_delegation_suffix(self):
        self.assertEqual(
            self.mod.strip_delegation_suffix("weakref_hash_lock_held"), "weakref_hash"
        )
        self.assertEqual(
            self.mod.strip_delegation_suffix("PyODict_SetItem_LockHeld"),
            "PyODict_SetItem",
        )
        self.assertEqual(self.mod.strip_delegation_suffix("tuple_hash"), "tuple_hash")

    def test_parameter_names(self):
        self.assertEqual(
            self.mod.parameter_names("Py_hash_t key_hash, PyObject *value"),
            ["key_hash", "value"],
        )
        self.assertEqual(self.mod.parameter_names("void"), [])

    def test_classify_hash_argument(self):
        classify = self.mod.classify_hash_argument
        params = ["self", "key"]
        roots = {"self", "alias"}
        self.assertEqual(classify("alias->origin", params, roots, {}), "receiver")
        self.assertEqual(
            classify("PyTuple_GET_ITEM(alias->args, i)", params, roots, {}), "container"
        )
        self.assertEqual(classify("key", params, roots, {}), "parameter")
        # Hashing the whole receiver is a pass-through, not a descent.
        self.assertEqual(classify("self", params, roots, {}), "parameter")
        self.assertEqual(classify("PyLong_FromLong(1)", params, roots, {}), "scalar")
        self.assertEqual(classify("PyTuple_New(3)", params, roots, {}), "temporary")

    def test_self_call_descends(self):
        descends = self.mod.self_call_descends
        self.assertFalse(descends("a, b, Py_EQ", ["a", "b", "op"], set()))
        self.assertFalse(descends("(PyObject *)v, w, Py_EQ", ["w", "op"], {"v"}))
        self.assertTrue(descends("t", ["args"], set()))
        self.assertTrue(
            descends("self, arg, parameters, item", ["self", "args"], set())
        )


class TestHashAliasAndOneHop(unittest.TestCase):
    """`_PyObject_HashDictKey` is PyObject_Hash under another name, and a
    `*_getstate` helper hides the fresh container one call away."""

    def setUp(self):
        self.mod = import_script("scan_recursion_guards")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _get(self, result, name):
        return next((f for f in result["findings"] if f["function"] == name), None)

    def test_hash_dict_key_alias_is_in_the_vocabulary(self):
        """Modules/_collectionsmodule.c:2592 _count_elements — confirmed stack
        overflow under ASan via collections.Counter. The alias is a
        Py_ALWAYS_INLINE wrapper whose tail is `return PyObject_Hash(op);`, so
        27 sites tree-wide (8+ in Objects/dictobject.c) were invisible."""
        result = self._findings(
            {
                "Modules/_collectionsmodule.c": (
                    "static PyObject *\n"
                    "_collections__count_elements_impl(PyObject *m, PyObject *k)\n"
                    "{\n"
                    "    Py_hash_t hash = _PyObject_HashDictKey(k);\n"
                    "    if (hash == -1) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        f = self._get(result, "_collections__count_elements_impl")
        self.assertIsNotNone(f)
        self.assertEqual(f["element_op"], "_PyObject_HashDictKey")
        # A caller-supplied value adds exactly one frame: correctly `low`.
        self.assertEqual(f["confidence"], "low")
        self.assertEqual(f["shape"], "hash_entry_point")

    def test_bound_zero_hash_spellings_stay_excluded(self):
        """Modules/_decimal/_decimal.c:5846 PyObject_GenericHash — an identity
        hash never descends. Excluded deliberately, and the envelope says so."""
        result = self._findings(
            {
                "Modules/d.c": (
                    "static Py_hash_t\n"
                    "dec_hash(PyObject *op)\n"
                    "{\n"
                    "    return PyObject_GenericHash(op);\n"
                    "}\n"
                )
            }
        )
        self.assertIsNone(self._get(result, "dec_hash"))
        self.assertIn(
            "PyObject_GenericHash",
            result["dispatcher_guard_model"]["bound_zero_excluded"],
        )

    def test_getstate_helper_bounds_the_descent(self):
        """Modules/_datetimemodule.c:2568 delta_hash — the worst-rated finding
        in the informed Modules/ sample, and a bound-1 false positive.
        `delta_getstate` is `return Py_BuildValue("iii", ...)`, i.e. a tuple of
        three C ints: nothing in it is a user object."""
        result = self._findings(
            {
                "Modules/_datetimemodule.c": (
                    "static PyObject *\n"
                    "delta_getstate(PyDateTime_Delta *self)\n"
                    "{\n"
                    '    return Py_BuildValue("iii", GET_TD_DAYS(self),\n'
                    "                         GET_TD_SECONDS(self),\n"
                    "                         GET_TD_MICROSECONDS(self));\n"
                    "}\n"
                    "\n"
                    "static Py_hash_t\n"
                    "delta_hash(PyObject *op)\n"
                    "{\n"
                    "    PyDateTime_Delta *self = PyDelta_CAST(op);\n"
                    "    if (self->hashcode == -1) {\n"
                    "        PyObject *temp = delta_getstate(self);\n"
                    "        if (temp != NULL) {\n"
                    "            self->hashcode = PyObject_Hash(temp);\n"
                    "            Py_DECREF(temp);\n"
                    "        }\n"
                    "    }\n"
                    "    return self->hashcode;\n"
                    "}\n"
                    "\n"
                    "static PyTypeObject D = { .tp_hash = delta_hash };\n"
                )
            }
        )
        self.assertIsNone(self._get(result, "delta_hash"))

    def test_getstate_helper_returning_a_field_tuple_still_reports(self):
        """The guarded twin of the previous test: a helper that packs receiver
        *fields* (not C scalars) is a real one-level descent, so the site
        survives — at `medium`, as temporary_container_descent."""
        result = self._findings(
            {
                "Modules/r.c": (
                    "static PyObject *\n"
                    "range_getstate(rangeobject *self)\n"
                    "{\n"
                    "    return PyTuple_Pack(3, self->start, self->stop,\n"
                    "                        self->step);\n"
                    "}\n"
                    "\n"
                    "static Py_hash_t\n"
                    "range_hash(PyObject *op)\n"
                    "{\n"
                    "    rangeobject *self = (rangeobject *)op;\n"
                    "    PyObject *temp = range_getstate(self);\n"
                    "    Py_hash_t h = PyObject_Hash(temp);\n"
                    "    Py_DECREF(temp);\n"
                    "    return h;\n"
                    "}\n"
                    "\n"
                    "static PyTypeObject R = { .tp_hash = range_hash };\n"
                )
            }
        )
        f = self._get(result, "range_hash")
        self.assertIsNotNone(f)
        self.assertEqual(f["shape"], "temporary_container_descent")
        self.assertEqual(f["confidence"], "medium")

    def test_one_hop_never_crosses_a_fat_helper(self):
        """Conservative by construction: a helper that does real work is not
        followed, so its receiver scope can never be misread in the caller."""
        result = self._findings(
            {
                "Modules/f.c": (
                    "static PyObject *\n"
                    "fat_getstate(FatObject *self)\n"
                    "{\n"
                    "    PyObject *a = build_a(self);\n"
                    "    PyObject *b = build_b(self);\n"
                    "    PyObject *c = build_c(self);\n"
                    "    PyObject *d = build_d(self);\n"
                    "    PyObject *e = build_e(self);\n"
                    "    PyObject *g = build_g(self);\n"
                    "    return PyTuple_Pack(2, a, b);\n"
                    "}\n"
                    "\n"
                    "static Py_hash_t\n"
                    "fat_hash(PyObject *op)\n"
                    "{\n"
                    "    FatObject *self = (FatObject *)op;\n"
                    "    PyObject *temp = fat_getstate(self);\n"
                    "    return PyObject_Hash(temp);\n"
                    "}\n"
                    "\n"
                    "static PyTypeObject F = { .tp_hash = fat_hash };\n"
                )
            }
        )
        f = self._get(result, "fat_hash")
        self.assertIsNotNone(f)
        self.assertNotEqual(f["shape"], "temporary_container_descent")


if __name__ == "__main__":
    unittest.main()
