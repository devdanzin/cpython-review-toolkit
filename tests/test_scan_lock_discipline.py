"""Tests for scan_lock_discipline.py — critical-section / lock discipline."""

import unittest

from helpers import TempProject, import_script


class TestScanLockDiscipline(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_lock_discipline")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _types(self, result):
        return [f["type"] for f in result["findings"]]

    # --- true positives ----------------------------------------------------

    def test_early_return_before_end_is_flagged(self):
        # Py_BEGIN_CRITICAL_SECTION with an early return before the END: the
        # per-object lock leaks on the error path.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_method(PyObject *self)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    if (bad(self)) {\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return self;\n"
                    "}\n"
                )
            }
        )
        self.assertIn("critical_section_end_on_error", self._types(result))
        f = next(
            f
            for f in result["findings"]
            if f["type"] == "critical_section_end_on_error"
        )
        self.assertEqual(f["function"], "foo_method")
        self.assertEqual(f["classification"], "FIX")
        self.assertEqual(f["line"], 6)  # the `return NULL;` line

    def test_missing_end_is_flagged(self):
        # A begin with no matching END anywhere: never released.
        result = self._findings(
            {
                "Objects/bar.c": (
                    "static PyObject *\n"
                    "bar_method(PyObject *self)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    PyObject *r = compute(self);\n"
                    "    return r;\n"
                    "}\n"
                )
            }
        )
        self.assertIn("critical_section_missing_end", self._types(result))
        # The unbalanced begin must not *also* be double-reported as an
        # end-on-error for the same return.
        self.assertNotIn("critical_section_end_on_error", self._types(result))

    def test_goto_out_of_section_is_flagged(self):
        # goto to a label that lives *after* the END skips the release.
        result = self._findings(
            {
                "Objects/baz.c": (
                    "static int\n"
                    "baz_method(PyObject *self)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    if (err(self)) goto cleanup;\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "cleanup:\n"
                    "    return -1;\n"
                    "}\n"
                )
            }
        )
        self.assertIn("critical_section_end_on_error", self._types(result))

    def test_nested_different_objects_is_consider(self):
        result = self._findings(
            {
                "Objects/qux.c": (
                    "static PyObject *\n"
                    "qux_merge(PyObject *a, PyObject *b)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION(a);\n"
                    "    Py_BEGIN_CRITICAL_SECTION(b);\n"
                    "    do_merge(a, b);\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return a;\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["type"] == "nested_critical_sections"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["classification"], "CONSIDER")

    # --- true negatives ----------------------------------------------------

    def test_properly_paired_is_clean(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_get(PyObject *self)\n"
                    "{\n"
                    "    PyObject *r;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    r = load(self);\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return r;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_end_before_early_return_is_clean(self):
        # Releasing before the early return is the correct idiom — no finding.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_get(PyObject *self)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    if (bad(self)) {\n"
                    "        Py_END_CRITICAL_SECTION();\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return self;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_retry_goto_within_section_is_clean(self):
        # A goto whose target label is *inside* the section is an internal
        # jump (retry loop), not an exit — must not be flagged.
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_spin(PyObject *self)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "retry:\n"
                    "    if (again(self)) goto retry;\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return self;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_no_critical_section_is_clean(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "plain(PyObject *self)\n"
                    "{\n"
                    "    if (bad(self)) return NULL;\n"
                    "    return self;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_comment_suppression(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo_method(PyObject *self)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    if (bad(self)) {\n"
                    "        /* intentional: lock released by caller */\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return self;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    # --- CPython-specific edge: the _MUTEX spelling ------------------------

    def test_mutex_begin_spelling_is_handled(self):
        # Py_BEGIN_CRITICAL_SECTION_MUTEX(&m) must be recognized as a begin
        # (paired with the ordinary Py_END_CRITICAL_SECTION()).
        result = self._findings(
            {
                "Modules/m.c": (
                    "static int\n"
                    "m_op(state *st)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION_MUTEX(&st->mutex);\n"
                    "    if (fail(st)) {\n"
                    "        return -1;\n"
                    "    }\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertIn("critical_section_end_on_error", self._types(result))

    def test_mutex_begin_properly_paired_is_clean(self):
        result = self._findings(
            {
                "Modules/m.c": (
                    "static int\n"
                    "m_op(state *st)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION_MUTEX(&st->mutex);\n"
                    "    touch(st);\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    # --- envelope shape ----------------------------------------------------

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
        self.assertIn("total_findings", result["summary"])
        self.assertIn("by_type", result["summary"])
        self.assertIn("by_classification", result["summary"])
        self.assertIn("critical_section_functions", result)
        self.assertIn("mutex_functions", result)
        self.assertIn("vocabulary_counts", result)


class TestPyMutexFamily(unittest.TestCase):
    """The ``python_mutex`` half of ``data/lock_macros.json``, which used to be
    loaded and then discarded by a ``type == "critical_section"`` filter."""

    def setUp(self):
        self.mod = import_script("scan_lock_discipline")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _types(self, result):
        return [f["type"] for f in result["findings"]]

    def test_both_families_are_loaded_from_the_data_file(self):
        families = self.mod._get_lock_families()
        self.assertIn("critical_section", families)
        self.assertIn("python_mutex", families)
        acquires, releases = families["python_mutex"]
        self.assertIn("PyMutex_Lock", acquires)
        self.assertIn("PyMutex_Unlock", releases)
        # CPython's PyMutex-backed weakref macros, added for this scanner.
        self.assertIn("LOCK_WEAKREFS", acquires)
        self.assertIn("UNLOCK_WEAKREFS", releases)
        self.assertIn("LOCK_WEAKREFS_FOR_WR", acquires)
        self.assertIn("UNLOCK_WEAKREFS_FOR_WR", releases)

    # --- true positives ----------------------------------------------------

    def test_pymutex_leaked_on_error_path_is_flagged(self):
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static int\n"
                    "foo_copy(PyObject *op)\n"
                    "{\n"
                    "    PyMutex_Lock(&op->mutex);\n"
                    "    if (bad(op)) {\n"
                    "        return -1;\n"
                    "    }\n"
                    "    PyMutex_Unlock(&op->mutex);\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertIn("mutex_leak_on_error", self._types(result))
        f = next(f for f in result["findings"] if f["type"] == "mutex_leak_on_error")
        self.assertEqual(f["line"], 6)
        self.assertEqual(f["classification"], "FIX")

    def test_weakref_lock_macros_are_visible(self):
        """Objects/weakrefobject.c's whole scheme used to be invisible."""
        result = self._findings(
            {
                "Objects/weakref.c": (
                    "static int\n"
                    "new_ref(PyObject *obj)\n"
                    "{\n"
                    "    LOCK_WEAKREFS(obj);\n"
                    "    if (bad(obj)) {\n"
                    "        return -1;\n"
                    "    }\n"
                    "    UNLOCK_WEAKREFS(obj);\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertIn("mutex_leak_on_error", self._types(result))

    # --- true negatives ----------------------------------------------------

    def test_release_before_every_exit_is_clean(self):
        """_PyWeakref_NewRef's four-exit body: each return unlocks first."""
        result = self._findings(
            {
                "Objects/weakref.c": (
                    "static PyObject *\n"
                    "get_or_create_weakref(PyObject *obj)\n"
                    "{\n"
                    "    LOCK_WEAKREFS(obj);\n"
                    "    PyObject *basic = try_reuse(obj);\n"
                    "    if (basic != NULL) {\n"
                    "        UNLOCK_WEAKREFS(obj);\n"
                    "        return basic;\n"
                    "    }\n"
                    "    PyObject *newref = allocate(obj);\n"
                    "    if (newref == NULL) {\n"
                    "        UNLOCK_WEAKREFS(obj);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    insert(newref);\n"
                    "    UNLOCK_WEAKREFS(obj);\n"
                    "    return newref;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_goto_cleanup_label_that_unlocks_is_clean(self):
        """Modules/zlibmodule.c:1093 — `goto error;` where error: unlocks."""
        result = self._findings(
            {
                "Modules/zlib.c": (
                    "static PyObject *\n"
                    "zlib_copy(PyObject *self)\n"
                    "{\n"
                    "    PyMutex_Lock(&self->mutex);\n"
                    "    int err = deflateCopy(self);\n"
                    "    if (err != 0) {\n"
                    "        goto error;\n"
                    "    }\n"
                    "    PyMutex_Unlock(&self->mutex);\n"
                    "    return self;\n"
                    "error:\n"
                    "    PyMutex_Unlock(&self->mutex);\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_lock_helper_that_never_unlocks_is_silent(self):
        """extensions_lock_acquire / stop_the_world: the caller releases."""
        result = self._findings(
            {
                "Python/import.c": (
                    "static void\n"
                    "extensions_lock_acquire(void)\n"
                    "{\n"
                    "    PyMutex_Lock(&_PyRuntime.imports.extensions.mutex);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_families_do_not_cross_pair(self):
        """A PyMutex_Unlock must not close a Py_BEGIN_CRITICAL_SECTION."""
        result = self._findings(
            {
                "Objects/foo.c": (
                    "static PyObject *\n"
                    "foo(PyObject *self)\n"
                    "{\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    PyMutex_Unlock(&self->mutex);\n"
                    "    return self;\n"
                    "}\n"
                )
            }
        )
        self.assertIn("critical_section_missing_end", self._types(result))
        self.assertNotIn("mutex_leak_on_error", self._types(result))

    # --- CPython-specific edge case ----------------------------------------

    def test_dead_return_after_unconditional_goto_is_not_a_leak(self):
        """Objects/dictobject.c:4380 — gh-112075 left a dead `return -1;`."""
        result = self._findings(
            {
                "Objects/dict.c": (
                    "static int\n"
                    "dict_set(PyObject *op)\n"
                    "{\n"
                    "    int res;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(op);\n"
                    "    if (needs_slow_path(op)) {\n"
                    "        res = -1;\n"
                    "        goto slow_exit;\n"
                    "        return -1;\n"
                    "    }\n"
                    "    res = 0;\n"
                    "slow_exit:\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return res;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_a_reachable_return_in_the_same_spot_is_still_flagged(self):
        """The dead-code gate must not swallow the live shape it resembles."""
        result = self._findings(
            {
                "Objects/dict.c": (
                    "static int\n"
                    "dict_set(PyObject *op)\n"
                    "{\n"
                    "    int res;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(op);\n"
                    "    if (needs_slow_path(op)) {\n"
                    "        res = -1;\n"
                    "        return -1;\n"
                    "    }\n"
                    "    res = 0;\n"
                    "slow_exit:\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return res;\n"
                    "}\n"
                )
            }
        )
        self.assertIn("critical_section_end_on_error", self._types(result))


# ---------------------------------------------------------------------------
# Same-TU #define resolution (issue #28, blindness fix A)
# ---------------------------------------------------------------------------
#
# Objects/typeobject.c wraps the vocabulary in its own macros at :79/:80 and
# then uses them 25 times. The scanner resolved 2 of those 25 regions -- the two
# written in the canonical spelling.

TYPEOBJECT_MACROS = """\
#define TYPE_LOCK &_PyInterpreterState_GET()->types.mutex
#define BEGIN_TYPE_LOCK() Py_BEGIN_CRITICAL_SECTION_MUTEX(TYPE_LOCK)
#define END_TYPE_LOCK() Py_END_CRITICAL_SECTION()

#define BEGIN_TYPE_DICT_LOCK(d) \\
    Py_BEGIN_CRITICAL_SECTION2_MUTEX(TYPE_LOCK, &_PyObject_CAST(d)->ob_mutex)

#define END_TYPE_DICT_LOCK() Py_END_CRITICAL_SECTION2()

#define ASSERT_TYPE_LOCK_HELD() \\
    _Py_CRITICAL_SECTION_ASSERT_MUTEX_LOCKED(TYPE_LOCK)
"""


class TestLocalLockMacroResolution(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_lock_discipline")

    def _resolve(self, source):
        return self.mod.resolve_local_lock_macros(
            source, self.mod._get_lock_families()
        )

    def _analyze(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def test_single_line_define_resolves(self):
        aliases, _ = self._resolve(TYPEOBJECT_MACROS)
        self.assertEqual(aliases["BEGIN_TYPE_LOCK"], ("critical_section", "acquire"))
        self.assertEqual(aliases["END_TYPE_LOCK"], ("critical_section", "release"))

    def test_continued_line_define_resolves(self):
        """BEGIN_TYPE_DICT_LOCK's body is on the next line after a backslash."""
        aliases, _ = self._resolve(TYPEOBJECT_MACROS)
        self.assertEqual(
            aliases["BEGIN_TYPE_DICT_LOCK"], ("critical_section", "acquire")
        )
        self.assertEqual(
            aliases["END_TYPE_DICT_LOCK"], ("critical_section", "release")
        )

    def test_lock_held_assertion_is_not_an_acquire(self):
        """Counting it as one turns every lock-held helper into an unpaired
        begin and manufactures a missing-END on correct code."""
        aliases, asserts = self._resolve(TYPEOBJECT_MACROS)
        self.assertNotIn("ASSERT_TYPE_LOCK_HELD", aliases)
        self.assertIn("ASSERT_TYPE_LOCK_HELD", asserts)

    def test_an_unrelated_define_is_not_a_lock_macro(self):
        aliases, asserts = self._resolve(
            "#define MAX_VERSIONS_PER_CLASS 1000\n"
            "#define TYPE_IS_REVEALED(tp) ((tp)->ob_flags & FLAG)\n"
        )
        self.assertEqual(aliases, {})
        self.assertEqual(asserts, set())

    def test_a_wrapped_region_becomes_visible(self):
        result = self._analyze(
            {
                "Objects/typeobject.c": TYPEOBJECT_MACROS
                + "\n"
                "static PyObject *\n"
                "lookup(PyTypeObject *type)\n"
                "{\n"
                "    PyObject *res;\n"
                "    BEGIN_TYPE_LOCK();\n"
                "    res = type->tp_dict;\n"
                "    END_TYPE_LOCK();\n"
                "    return res;\n"
                "}\n"
            }
        )
        self.assertEqual(result["critical_section_functions"], 1)
        self.assertIn("Objects/typeobject.c", result["local_lock_macros"])
        # Correctly paired, so still no finding -- an *earned* zero.
        self.assertEqual(result["summary"]["total_findings"], 0)

    def test_a_wrapped_leak_is_now_caught(self):
        result = self._analyze(
            {
                "Objects/typeobject.c": TYPEOBJECT_MACROS
                + "\n"
                "static PyObject *\n"
                "leaky(PyTypeObject *type)\n"
                "{\n"
                "    BEGIN_TYPE_LOCK();\n"
                "    if (type->tp_dict == NULL) {\n"
                "        return NULL;\n"
                "    }\n"
                "    END_TYPE_LOCK();\n"
                "    return type->tp_dict;\n"
                "}\n"
            }
        )
        self.assertGreater(result["summary"]["total_findings"], 0)

    def test_a_file_with_no_wrappers_reports_none(self):
        """17 of the 18 Objects/ files that lock at all use the canonical
        spellings; their empty entry is the expected answer, not a gap."""
        result = self._analyze(
            {
                "Objects/plain.c": (
                    "static PyObject *\n"
                    "get(PyObject *self)\n"
                    "{\n"
                    "    PyObject *r;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    r = self->ob_type->tp_dict;\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return r;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["local_lock_macros"], {})
        self.assertEqual(result["critical_section_functions"], 1)


if __name__ == "__main__":
    unittest.main()


class TestFlagsTakingAcquireMatchesItsRelease(unittest.TestCase):
    """A lock's identity is its FIRST argument, not the whole argument text.

    `FT_MUTEX_LOCK_FLAGS(m, _Py_LOCK_DONT_DETACH)` releases through
    `FT_MUTEX_UNLOCK(m)` — two arguments against one. Comparing whole argument
    text made them look like different locks, so the correct `goto done; ...
    done: FT_MUTEX_UNLOCK(...)` ladder in Objects/dictobject.c's
    PyDict_AddWatcher and PyDict_ClearWatcher read as four leaks. The same
    applies to PyMutex_LockFlags, and therefore to LOCK_KEYS, which expands to
    it.
    """

    def setUp(self):
        self.mod = import_script("scan_lock_discipline")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))["findings"]

    def test_first_top_level_arg_ignores_the_flags(self):
        self.assertEqual(
            self.mod._first_top_level_arg(
                "&interp->dict_state.watcher_mutex, _Py_LOCK_DONT_DETACH"
            ).strip(),
            "&interp->dict_state.watcher_mutex",
        )

    def test_first_top_level_arg_ignores_commas_inside_parens(self):
        self.assertEqual(
            self.mod._first_top_level_arg("FOO(a, b), _Py_LOCK_DONT_DETACH").strip(),
            "FOO(a, b)",
        )

    def test_goto_to_a_label_that_unlocks_is_not_a_leak(self):
        # The PyDict_AddWatcher shape, verbatim in structure.
        src = (
            "int\n"
            "add_watcher(PyInterpreterState *interp, void *callback)\n"
            "{\n"
            "    int watcher_id = -1;\n"
            "    FT_MUTEX_LOCK_FLAGS(&interp->dict_state.watcher_mutex,\n"
            "                        _Py_LOCK_DONT_DETACH);\n"
            "    for (int i = 0; i < 8; i++) {\n"
            "        if (!interp->dict_state.watchers[i]) {\n"
            "            watcher_id = i;\n"
            "            goto done;\n"
            "        }\n"
            "    }\n"
            "    PyErr_SetString(PyExc_RuntimeError, \"no more\");\n"
            "done:\n"
            "    FT_MUTEX_UNLOCK(&interp->dict_state.watcher_mutex);\n"
            "    return watcher_id;\n"
            "}\n"
        )
        self.assertEqual(self._findings({"Objects/w.c": src}), [])

    def test_a_genuine_leak_past_a_flags_acquire_is_still_reported(self):
        src = (
            "int\n"
            "leaky(PyInterpreterState *interp, int bad)\n"
            "{\n"
            "    FT_MUTEX_LOCK_FLAGS(&interp->dict_state.watcher_mutex,\n"
            "                        _Py_LOCK_DONT_DETACH);\n"
            "    if (bad) {\n"
            "        return -1;\n"
            "    }\n"
            "    FT_MUTEX_UNLOCK(&interp->dict_state.watcher_mutex);\n"
            "    return 0;\n"
            "}\n"
        )
        found = self._findings({"Objects/w.c": src})
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["type"], "mutex_leak_on_error")
