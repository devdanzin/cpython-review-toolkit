"""Tests for check_known_issues.py — the known-issues regression command."""

import unittest
from pathlib import Path

from helpers import TempProject, import_script

# A tp_dealloc with an unguarded PyErr_Clear() — the pyerr-clear scanner flags
# this (exception-clobbering in the destructor family).
_PRESENT_DEALLOC = (
    '#include "Python.h"\n'
    "static void\n"
    "present_dealloc(PyObject *self)\n"
    "{\n"
    '    if (PyObject_CallMethod(self, "close", NULL) == NULL) {\n'
    "        PyErr_Clear();\n"
    "    }\n"
    "    Py_TYPE(self)->tp_free(self);\n"
    "}\n"
)

# A clean dealloc — no PyErr_Clear, so the scanner finds nothing here.
_CLEAN_DEALLOC = (
    '#include "Python.h"\n'
    "static void\n"
    "clean_dealloc(PyObject *self)\n"
    "{\n"
    "    Py_XDECREF(((FooObject *)self)->attr);\n"
    "    Py_TYPE(self)->tp_free(self);\n"
    "}\n"
)


def _tsv(rows: list[tuple]) -> str:
    """Build a TAB-separated catalog body from (bug_id, path, line, cat, fn, note)."""
    header = "# bug_id\tpath\tline\tcategory\tfunction\tnote\n"
    return header + "".join("\t".join(str(c) for c in row) + "\n" for row in rows)


class TestCheckKnownIssues(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("check_known_issues")

    def _run(self, files: dict, rows: list[tuple]) -> dict:
        files = dict(files)
        files["known.tsv"] = _tsv(rows)
        with TempProject(files) as root:
            catalog = str(Path(root) / "known.tsv")
            return self.mod.analyze(str(root), catalog_path=catalog)

    def _result_for(self, report: dict, bug_id: str) -> dict:
        return next(r for r in report["catalog_results"] if r["bug_id"] == bug_id)

    # --- present -----------------------------------------------------------

    def test_present_matches_by_function_when_line_unknown(self):
        report = self._run(
            {"Objects/present.c": _PRESENT_DEALLOC},
            [
                (
                    "PRESENT-1",
                    "Objects/present.c",
                    0,
                    "pyerr-clear",
                    "present_dealloc",
                    "planted",
                )
            ],
        )
        entry = self._result_for(report, "PRESENT-1")
        self.assertEqual(entry["status"], "present")
        self.assertEqual(report["bug_rollup"]["PRESENT-1"], "present")
        # And it surfaces as an actionable finding.
        f = next(f for f in report["findings"] if f["bug_id"] == "PRESENT-1")
        self.assertEqual(f["type"], "known_bug_present")
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["file"], "Objects/present.c")

    def test_present_matches_within_drift_window(self):
        # The PyErr_Clear sits on line 6; a catalog line a couple lines off
        # still counts as present (within the ±drift window).
        report = self._run(
            {"Objects/present.c": _PRESENT_DEALLOC},
            [
                (
                    "PRESENT-2",
                    "Objects/present.c",
                    7,
                    "pyerr-clear",
                    "present_dealloc",
                    "near",
                )
            ],
        )
        self.assertEqual(self._result_for(report, "PRESENT-2")["status"], "present")

    # --- absent ------------------------------------------------------------

    def test_absent_when_file_scanned_but_clean(self):
        report = self._run(
            {"Objects/clean.c": _CLEAN_DEALLOC},
            [
                (
                    "ABSENT-1",
                    "Objects/clean.c",
                    0,
                    "pyerr-clear",
                    "clean_dealloc",
                    "should be gone",
                )
            ],
        )
        entry = self._result_for(report, "ABSENT-1")
        self.assertEqual(entry["status"], "absent")
        self.assertEqual(report["bug_rollup"]["ABSENT-1"], "likely_fixed")
        # Absent entries never become actionable findings.
        self.assertFalse(any(f["bug_id"] == "ABSENT-1" for f in report["findings"]))

    # --- line_drifted ------------------------------------------------------

    def test_line_drifted_when_findings_exist_but_off_site(self):
        # Known line far from the real finding, and an unknown function ("0"),
        # so neither the line window nor the function name matches.
        report = self._run(
            {"Objects/present.c": _PRESENT_DEALLOC},
            [("DRIFT-1", "Objects/present.c", 400, "pyerr-clear", "0", "moved")],
        )
        entry = self._result_for(report, "DRIFT-1")
        self.assertEqual(entry["status"], "line_drifted")
        self.assertIsNotNone(entry["nearest_line"])

    # --- file_missing ------------------------------------------------------

    def test_file_missing_when_path_absent(self):
        report = self._run(
            {"Objects/present.c": _PRESENT_DEALLOC},
            [
                (
                    "MISSING-1",
                    "Objects/ghost.c",
                    0,
                    "pyerr-clear",
                    "ghost_dealloc",
                    "gone",
                )
            ],
        )
        entry = self._result_for(report, "MISSING-1")
        self.assertEqual(entry["status"], "file_missing")
        self.assertEqual(report["bug_rollup"]["MISSING-1"], "likely_fixed")

    # --- no_scanner passthrough --------------------------------------------

    def test_no_scanner_category_passes_through(self):
        # `init-bypass` has no scanner yet -> carried through as no_scanner.
        # (`tsan` IS scanned now, by scan_ft_races.)
        report = self._run(
            {"Objects/present.c": _PRESENT_DEALLOC},
            [
                (
                    "INIT-1",
                    "Objects/present.c",
                    0,
                    "init-bypass",
                    "0",
                    "__new__ bypass",
                ),
            ],
        )
        self.assertEqual(self._result_for(report, "INIT-1")["status"], "no_scanner")
        self.assertEqual(report["bug_rollup"]["INIT-1"], "no_scanner")
        # no_scanner entries are never scanned and never actionable findings.
        self.assertFalse(any(f["bug_id"] == "INIT-1" for f in report["findings"]))

    # --- bug_rollup collapsing multi-site bugs -----------------------------

    def test_multi_site_rollup_present_wins(self):
        report = self._run(
            {
                "Objects/present.c": _PRESENT_DEALLOC,
                "Objects/clean.c": _CLEAN_DEALLOC,
            },
            [
                (
                    "MULTI-1",
                    "Objects/clean.c",
                    0,
                    "pyerr-clear",
                    "clean_dealloc",
                    "site a",
                ),
                (
                    "MULTI-1",
                    "Objects/present.c",
                    0,
                    "pyerr-clear",
                    "present_dealloc",
                    "site b",
                ),
            ],
        )
        # One site absent, one present -> the bug rolls up to present.
        self.assertEqual(report["bug_rollup"]["MULTI-1"], "present")

    # --- envelope shape ----------------------------------------------------

    def test_envelope_shape(self):
        report = self._run(
            {"Objects/present.c": _PRESENT_DEALLOC},
            [
                (
                    "PRESENT-1",
                    "Objects/present.c",
                    0,
                    "pyerr-clear",
                    "present_dealloc",
                    "x",
                )
            ],
        )
        for key in (
            "project_root",
            "scan_root",
            "files_analyzed",
            "functions_analyzed",
            "findings",
            "summary",
            "catalog_summary",
            "bug_rollup",
            "catalog_results",
            "notes",
        ):
            self.assertIn(key, report)
        cs = report["catalog_summary"]
        self.assertEqual(cs["catalog_entries"], 1)
        self.assertIn("status_counts", cs)
        # The absent-is-not-a-fix caveat must be baked into the JSON notes.
        self.assertTrue(any("absent" in n.lower() for n in report["notes"]))


if __name__ == "__main__":
    unittest.main()
