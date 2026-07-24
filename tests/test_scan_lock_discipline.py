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


if __name__ == "__main__":
    unittest.main()
