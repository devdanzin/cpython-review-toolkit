"""Tests for scan_common.build_report — the shared JSON envelope.

The envelope is where a scanner's result becomes a claim someone acts on, so the
distinction between "clean" and "saw nothing" belongs here rather than in each
scanner's prose.
"""

import unittest
from pathlib import Path

from helpers import import_script

mod = import_script("scan_common")


def build(**kwargs) -> dict:
    defaults: dict = {
        "project_root": Path("/tmp/proj"),
        "scan_root": Path("/tmp/proj/Objects"),
        "files_analyzed": 1,
        "functions_analyzed": 10,
        "findings": [],
        "summary": {"total_findings": 0},
    }
    defaults.update(kwargs)
    return mod.build_report(**defaults)


class TestEnvelopeBasics(unittest.TestCase):
    def test_required_keys_are_present(self):
        report = build()
        for key in (
            "project_root",
            "scan_root",
            "files_analyzed",
            "functions_analyzed",
            "findings",
            "summary",
        ):
            self.assertIn(key, report)

    def test_extra_kwargs_are_passed_through(self):
        self.assertEqual(build(skipped_files=["a.c"])["skipped_files"], ["a.c"])

    def test_paths_are_stringified(self):
        report = build()
        self.assertIsInstance(report["project_root"], str)
        self.assertIsInstance(report["scan_root"], str)


class TestRuleNotApplicable(unittest.TestCase):
    """An empty findings list means two opposite things; the envelope must say which.

    Measured on the obj-typeobject review: scan_gil_usage resolved 0 constructs
    in a file containing 11 stop-the-world regions and 3 critical-section macro
    families, and reported an empty findings list indistinguishable from a clean
    scan of Modules/socketmodule.c (29 resolved ALLOW_THREADS pairs).
    """

    def test_an_all_zero_vocabulary_is_flagged_not_applicable(self):
        report = build(vocabulary_counts={"Py_BEGIN_ALLOW_THREADS": 0, "X": 0})
        self.assertIs(report["rule_not_applicable"], True)
        self.assertIn("silence, not safety", report["rule_not_applicable_note"])

    def test_a_resolved_vocabulary_is_not_flagged(self):
        report = build(vocabulary_counts={"Py_BEGIN_ALLOW_THREADS": 29, "X": 0})
        self.assertIs(report["rule_not_applicable"], False)
        self.assertNotIn("rule_not_applicable_note", report)

    def test_a_scanner_without_a_vocabulary_gets_no_verdict(self):
        """Absence of the field must not be reported as either answer."""
        self.assertNotIn("rule_not_applicable", build())

    def test_an_empty_vocabulary_dict_gets_no_verdict(self):
        self.assertNotIn("rule_not_applicable", build(vocabulary_counts={}))

    def test_non_numeric_vocabulary_values_do_not_crash(self):
        report = build(vocabulary_counts={"note": "some text", "X": 0})
        self.assertIs(report["rule_not_applicable"], True)

    def test_findings_present_still_reports_the_denominator(self):
        """The flag describes the vocabulary, not whether anything was found."""
        report = build(
            findings=[{"file": "a.c", "line": 1}],
            summary={"total_findings": 1},
            vocabulary_counts={"X": 5},
        )
        self.assertIs(report["rule_not_applicable"], False)


if __name__ == "__main__":
    unittest.main()
