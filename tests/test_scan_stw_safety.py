"""Tests for scan_stw_safety.py — StopTheWorld-safety in CPython's own code.

The scanner reviews CPython's ``Python/`` / ``Objects/`` / ``Modules/`` code:
an unsafe call inside a real ``_PyEval_StopTheWorld()..._PyEval_StartTheWorld()``
region can deadlock the world or corrupt interpreter state on the free-threaded
build. Note the data-file revision (2026-04-04) reclassified object allocation
(``PyList_New`` etc.) and ``PyErr_NoMemory`` as *conditionally safe* on 3.14+, so
those are intentionally NOT flagged; the unambiguous violations below are
Python-invoking calls (``PyObject_Str``), the exception format machinery
(``PyErr_Format``), and container mutation.
"""

import unittest

from helpers import TempProject, import_script


class TestLockPolarityInStwVocabulary(unittest.TestCase):
    """The counter-intuitive half of the stop-the-world contract.

    Verified in CPython main @ 4f3be1b5:
      Python/lock.c:656              PyMutex_Lock blocks with _PY_LOCK_DETACH
      Python/pystate.c:2323          detach_thread -> _PyCriticalSection_SuspendAll
      Python/critical_section.c:113  SuspendAll unlocks CRITICAL-SECTION mutexes only

    So a critical section entered inside a stopped-world region is SAFE (the
    detach releases it), while a RAW PyMutex_Lock is the actual hazard (nothing
    releases it, and it can block on a mutex a stopped thread holds). Getting
    this backwards suppresses the real defect and invents a false one, so it is
    asserted rather than left to a comment.
    """

    def setUp(self):
        self.mod = import_script("scan_stw_safety")
        self.data = self.mod._load_stw_apis()

    def _flatten(self, section: dict) -> set[str]:
        names: set[str] = set()
        for key, value in section.items():
            if isinstance(value, list) and not key.endswith("note"):
                names.update(value)
        return names

    def test_critical_sections_are_classified_safe(self):
        safe = self._flatten(self.data["safe_during_stw"])
        for macro in (
            "Py_BEGIN_CRITICAL_SECTION",
            "Py_BEGIN_CRITICAL_SECTION2",
            "Py_BEGIN_CRITICAL_SECTION_MUTEX",
            "Py_BEGIN_CRITICAL_SECTION2_MUTEX",
        ):
            self.assertIn(macro, safe, msg=macro)

    def test_raw_mutex_acquisition_is_classified_unsafe(self):
        unsafe = self._flatten(self.data["unsafe_during_stw"])
        for name in ("PyMutex_Lock", "PyMutex_LockFlags", "PyThread_acquire_lock"):
            self.assertIn(name, unsafe, msg=name)

    def test_the_two_classifications_do_not_overlap(self):
        safe = self._flatten(self.data["safe_during_stw"])
        unsafe = self._flatten(self.data["unsafe_during_stw"])
        overlap = safe & unsafe
        self.assertEqual(overlap, set(), f"classified both ways: {sorted(overlap)}")

    def test_both_new_categories_carry_their_reasoning(self):
        self.assertIn("critical_sections_note", self.data["safe_during_stw"])
        self.assertIn("raw_lock_acquisition_note", self.data["unsafe_during_stw"])


class TestScanStwSafety(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_stw_safety")

    def _analyze(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    # --- true positives ----------------------------------------------------

    def test_unsafe_call_in_stw_region_is_flagged(self):
        result = self._analyze(
            {
                "Modules/worker.c": (
                    '#include "Python.h"\n'
                    "static void\n"
                    "stw_worker(PyInterpreterState *interp, PyObject *obj)\n"
                    "{\n"
                    "    _PyEval_StopTheWorld(interp);\n"
                    "    PyObject_Str(obj);\n"
                    "    _PyEval_StartTheWorld(interp);\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["api_call"] == "PyObject_Str"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["type"], "stw_unsafe_call")
        self.assertEqual(f["function"], "stw_worker")
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["unsafe_reason"], "invokes_python_code")
        self.assertEqual(f["file"], "Modules/worker.c")

    def test_exception_format_in_stw_region_is_exception_type(self):
        result = self._analyze(
            {
                "Python/thing.c": (
                    "static void\n"
                    "stw_err(PyInterpreterState *interp)\n"
                    "{\n"
                    "    _PyEval_StopTheWorld(interp);\n"
                    '    PyErr_Format(PyExc_RuntimeError, "boom");\n'
                    "    _PyEval_StartTheWorld(interp);\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["api_call"] == "PyErr_Format"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["type"], "stw_exception_during_stw")
        self.assertEqual(f["unsafe_reason"], "exception_setting")

    def test_transitive_unsafe_helper_in_region_is_flagged(self):
        # The STW region calls a local helper that is itself unsafe (it invokes
        # Python). Intra-file propagation must mark the helper unsafe and flag
        # the call to it during STW.
        result = self._analyze(
            {
                "Objects/graph.c": (
                    '#include "Python.h"\n'
                    "static void\n"
                    "unsafe_helper(PyObject *obj)\n"
                    "{\n"
                    '    PyObject_GetAttrString(obj, "x");\n'
                    "}\n"
                    "static void\n"
                    "stw_caller(PyInterpreterState *interp, PyObject *obj)\n"
                    "{\n"
                    "    _PyEval_StopTheWorld(interp);\n"
                    "    unsafe_helper(obj);\n"
                    "    _PyEval_StartTheWorld(interp);\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["api_call"] == "unsafe_helper"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["type"], "stw_unsafe_call")
        self.assertEqual(f["function"], "stw_caller")
        self.assertEqual(f["unsafe_reason"], "transitively_invokes_python")
        # And the helper is recorded as unsafe in the propagated classifications.
        self.assertEqual(
            result["function_classifications"]["Objects/graph.c"]["unsafe_helper"],
            "unsafe",
        )

    # --- true negatives ----------------------------------------------------

    def test_safe_ops_in_region_not_flagged(self):
        result = self._analyze(
            {
                "Objects/safe.c": (
                    '#include "Python.h"\n'
                    "static void\n"
                    "stw_safe(PyInterpreterState *interp, PyObject *lst, char *dst,\n"
                    "         char *src, Py_ssize_t n)\n"
                    "{\n"
                    "    _PyEval_StopTheWorld(interp);\n"
                    "    PyObject *item = PyList_GET_ITEM(lst, 0);\n"
                    "    Py_INCREF(item);\n"
                    "    memcpy(dst, src, n);\n"
                    "    _PyEval_StartTheWorld(interp);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])
        # It is still recorded as an STW-bearing function.
        self.assertEqual(result["summary"]["stw_function_count"], 1)

    def test_builtin_allocation_in_region_is_safe_on_314(self):
        # PyTuple_New/PyList_New during STW is SAFE on 3.14+ free-threading
        # builds (GC runs only on the eval breaker, not during allocation).
        # This is exactly what CPython's own _PyEval_SetProfileAllThreads does:
        # PyTuple_New inside the StopTheWorld region. The data-file revision
        # (safe_allocation_on_314) must suppress it — regression guard against
        # the nested-key lookup silently missing that carve-out.
        result = self._analyze(
            {
                "Python/prof.c": (
                    '#include "Python.h"\n'
                    "static void\n"
                    "stw_alloc(PyInterpreterState *interp, Py_ssize_t n)\n"
                    "{\n"
                    "    _PyEval_StopTheWorld(interp);\n"
                    "    PyObject *t = PyTuple_New(n);\n"
                    "    (void)t;\n"
                    "    _PyEval_StartTheWorld(interp);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_unsafe_call_without_stw_region_not_flagged(self):
        result = self._analyze(
            {
                "Objects/plain.c": (
                    '#include "Python.h"\n'
                    "static void\n"
                    "plain(PyObject *obj)\n"
                    "{\n"
                    "    PyObject_Str(obj);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["summary"]["stw_function_count"], 0)

    def test_unsafe_call_after_start_the_world_not_flagged(self):
        # The correct pattern: do the unsafe work AFTER StartTheWorld.
        result = self._analyze(
            {
                "Python/correct.c": (
                    '#include "Python.h"\n'
                    "static void\n"
                    "stw_correct(PyInterpreterState *interp, PyObject *obj)\n"
                    "{\n"
                    "    _PyEval_StopTheWorld(interp);\n"
                    "    Py_INCREF(obj);\n"
                    "    _PyEval_StartTheWorld(interp);\n"
                    "    PyObject_Str(obj);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    # --- suppression + envelope --------------------------------------------

    def test_comment_suppression(self):
        result = self._analyze(
            {
                "Modules/sup.c": (
                    '#include "Python.h"\n'
                    "static void\n"
                    "stw_sup(PyInterpreterState *interp, PyObject *obj)\n"
                    "{\n"
                    "    _PyEval_StopTheWorld(interp);\n"
                    "    /* intentional: type is a builtin, no exception is set */\n"
                    "    PyObject_Str(obj);\n"
                    "    _PyEval_StartTheWorld(interp);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_envelope_shape(self):
        result = self._analyze(
            {"Objects/foo.c": "static void foo(PyObject *self) { }\n"}
        )
        for key in (
            "project_root",
            "scan_root",
            "files_analyzed",
            "functions_analyzed",
            "findings",
            "summary",
            "stw_functions",
            "function_classifications",
        ):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
