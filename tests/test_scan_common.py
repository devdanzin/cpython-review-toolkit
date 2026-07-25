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


class TestDenominators(unittest.TestCase):
    """Every envelope carries a `denominators` block (issue #28).

    `rule_not_applicable` answers "did the rule see anything" only for scanners
    that ship a vocabulary. The rest express their reach as bespoke keys that an
    agent has to know about in advance. One name means the standing rule --
    *report the denominator before calling a zero clean* -- can be followed
    against any scanner's output.
    """

    def _report(self, **extra):
        return mod.build_report(
            project_root=Path("/x"),
            scan_root=Path("/x/y"),
            files_analyzed=2,
            functions_analyzed=40,
            findings=[],
            summary={"total_findings": 0},
            **extra,
        )

    def test_always_present(self):
        den = self._report()["denominators"]
        self.assertEqual(den["files_analyzed"], 2)
        self.assertEqual(den["functions_analyzed"], 40)
        self.assertEqual(den["findings"], 0)

    def test_suffix_named_counters_are_collected(self):
        den = self._report(total_nullable_fields=7, allocation_sites=12)["denominators"]
        self.assertEqual(den["total_nullable_fields"], 7)
        self.assertEqual(den["allocation_sites"], 12)

    def test_summary_counters_are_collected_too(self):
        report = mod.build_report(
            project_root=Path("/x"),
            scan_root=Path("/x"),
            files_analyzed=1,
            functions_analyzed=3,
            findings=[],
            summary={"total_findings": 0, "allocation_sites": 5},
        )
        self.assertEqual(report["denominators"]["allocation_sites"], 5)

    def test_a_census_dict_is_flattened_not_counted(self):
        """Reporting a three-key census's *length* would say 3 for numbers that
        are 161/127/27 -- worse than saying nothing."""
        den = self._report(
            varobject_allocation_census={"sites": 161, "via_slot_pointer": 127}
        )["denominators"]
        self.assertEqual(den["varobject_allocation_census.sites"], 161)
        self.assertEqual(den["varobject_allocation_census.via_slot_pointer"], 127)
        self.assertNotIn("varobject_allocation_census", den)

    def test_a_non_numeric_collection_is_counted(self):
        den = self._report(outparam_wrappers=["a", "b"])["denominators"]
        self.assertEqual(den["outparam_wrappers"], 2)

    def test_vocabulary_is_summed(self):
        den = self._report(vocabulary_counts={"A": 3, "B": 0})["denominators"]
        self.assertEqual(den["vocabulary_resolved"], 3)
        self.assertEqual(den["vocabulary_tokens_seen"], 2)

    def test_all_zero_denominators_get_a_note(self):
        report = mod.build_report(
            project_root=Path("/x"),
            scan_root=Path("/x"),
            files_analyzed=0,
            functions_analyzed=0,
            findings=[],
            summary={"total_findings": 0},
            allocation_sites=0,
        )
        self.assertIn("note", report["denominators"])
        self.assertIn("silence, not safety", report["denominators"]["note"])

    def test_a_real_denominator_gets_no_note(self):
        den = self._report(allocation_sites=4)["denominators"]
        self.assertNotIn("note", den)

    def test_booleans_are_not_denominators(self):
        den = self._report(some_flag_resolved=True)["denominators"]
        self.assertNotIn("some_flag_resolved", den)


if __name__ == "__main__":
    unittest.main()
