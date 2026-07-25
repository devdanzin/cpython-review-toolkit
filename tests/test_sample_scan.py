"""Tests for tools/sample_scan.py -- sample-scoped scanner runs.

The bug this tool fixes is measured: the `Modules/` run's pre-filtered
`scan_init_bypass.sample.json` shipped `findings: []` next to
`total_nullable_fields: 103` -- a corpus-wide denominator beside sample-scoped
findings, which inverts the canary that tells an agent whether a zero is
earned.
"""

import importlib.util
import json
import unittest
from pathlib import Path

from helpers import TempProject

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "sample_scan.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("sample_scan", _TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMergePolicy(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_counts_are_summed(self):
        merged, policy = self.tool.merge_reports(
            [
                {"files_analyzed": 1, "total_nullable_fields": 4, "findings": []},
                {"files_analyzed": 1, "total_nullable_fields": 9, "findings": []},
            ]
        )
        self.assertEqual(merged["files_analyzed"], 2)
        self.assertEqual(merged["total_nullable_fields"], 13)
        self.assertEqual(policy["total_nullable_fields"], "summed")

    def test_nested_summary_dicts_are_merged_key_wise(self):
        merged, _ = self.tool.merge_reports(
            [
                {"summary": {"total_findings": 2, "by_api": {"a": 2}}, "findings": []},
                {
                    "summary": {"total_findings": 1, "by_api": {"a": 1, "b": 1}},
                    "findings": [],
                },
            ]
        )
        self.assertEqual(merged["summary"]["total_findings"], 3)
        self.assertEqual(merged["summary"]["by_api"], {"a": 3, "b": 1})

    def test_a_constant_is_carried_not_summed(self):
        """`apis_in_vocabulary` describes the scanner, not the corpus."""
        merged, policy = self.tool.merge_reports(
            [
                {"summary": {"apis_in_vocabulary": 93}, "findings": []},
                {"summary": {"apis_in_vocabulary": 93}, "findings": []},
                {"summary": {"apis_in_vocabulary": 93}, "findings": []},
            ]
        )
        self.assertEqual(merged["summary"]["apis_in_vocabulary"], 93)
        self.assertIn("carried", policy["summary.apis_in_vocabulary"])

    def test_findings_are_concatenated_and_sorted(self):
        merged, _ = self.tool.merge_reports(
            [
                {"findings": [{"file": "b.c", "line": 2}]},
                {"findings": [{"file": "a.c", "line": 9}, {"file": "a.c", "line": 1}]},
            ]
        )
        self.assertEqual(
            [(f["file"], f["line"]) for f in merged["findings"]],
            [("a.c", 1), ("a.c", 9), ("b.c", 2)],
        )


class TestSampleScan(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_denominators_describe_the_sample_not_the_corpus(self):
        scanner = self.tool.load_scanner("scan_deprecated_apis")
        with TempProject(
            {
                "Modules/in_sample.c": (
                    "void\nf(void)\n{\n    PyMem_NEW(PyObject *, n);\n}\n"
                ),
                "Modules/out_of_sample.c": (
                    "void\ng(void)\n{\n"
                    "    PyMem_RESIZE(p, PyObject *, n);\n"
                    '    PyModule_AddObject(m, "n", o);\n'
                    "}\n"
                ),
            }
        ) as root:
            corpus = scanner.analyze(str(root))
            self.assertEqual(corpus["summary"]["total_findings"], 3)
            # TempProject also lays down the CPython root markers.
            self.assertGreater(corpus["files_analyzed"], 2)

            sample = self.tool.sample_scan(scanner, root, ["Modules/in_sample.c"])
            # Findings AND every denominator are sample-scoped.
            self.assertEqual(len(sample["findings"]), 1)
            self.assertEqual(sample["summary"]["total_findings"], 1)
            self.assertEqual(sample["files_analyzed"], 1)
            self.assertEqual(sample["summary"]["by_api"], {"PyMem_NEW": 1})

    def test_missing_sample_files_are_reported_not_silently_dropped(self):
        scanner = self.tool.load_scanner("scan_deprecated_apis")
        with TempProject({"Modules/a.c": "int x;\n"}) as root:
            sample = self.tool.sample_scan(
                scanner, root, ["Modules/a.c", "Modules/gone.c"]
            )
            self.assertEqual(sample["_sample"]["files_scanned"], ["Modules/a.c"])
            self.assertEqual(sample["_sample"]["files_missing"], ["Modules/gone.c"])

    def test_output_records_how_it_was_produced(self):
        scanner = self.tool.load_scanner("scan_deprecated_apis")
        with TempProject({"Modules/a.c": "int x;\n"}) as root:
            sample = self.tool.sample_scan(scanner, root, ["Modules/a.c"])
            self.assertIn("RE-RUN", sample["_sample"]["method"])
            self.assertIn("merge_policy", sample["_sample"])
            # Must be serialisable -- it is written to disk for agents to read.
            json.dumps(sample, default=str)


if __name__ == "__main__":
    unittest.main()
