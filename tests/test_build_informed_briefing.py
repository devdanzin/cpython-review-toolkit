"""Tests for build_informed_briefing.py — the informed-explore briefing generator.

The generator emits Markdown (not JSON) assembled from the CPython bug-shape
catalog, the cross-cutting triage rules, the FP taxonomy, and — optionally — a
``cpython-review-findings`` catalog dir. These tests use the real shipped
catalogs for the default-path assertions and a ``tempfile`` fixture for the
``--catalog-dir`` behavior.
"""

import json
import tempfile
import unittest
from pathlib import Path

from helpers import import_script

_DATA = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "cpython-review-toolkit"
    / "data"
)


class TestBuildInformedBriefing(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("build_informed_briefing")

    def _shapes(self) -> list[dict]:
        data = json.loads(
            (_DATA / "cpython_bug_shapes.json").read_text(encoding="utf-8")
        )
        return data["shapes"]

    # --- default briefing --------------------------------------------------

    def test_briefing_has_a_section_for_each_shape_id(self):
        briefing = self.mod.build_briefing()
        for shape in self._shapes():
            self.assertIn(
                shape["id"],
                briefing,
                f"shape id {shape['id']!r} missing from briefing",
            )

    def test_briefing_contains_the_guarded_twin_text(self):
        briefing = self.mod.build_briefing()
        shapes = self._shapes()
        # The literal guarded-twin string of every shape must appear.
        for shape in shapes:
            self.assertIn(shape["guarded_twin"], briefing)
        # And the "guarded twin (the fix)" label frames it.
        self.assertIn("guarded twin (the fix)", briefing)

    def test_briefing_contains_the_three_agent_rules(self):
        briefing = self.mod.build_briefing()
        self.assertIn("Confirm, don't re-litigate", briefing)
        self.assertIn("Skip the known false-positive classes", briefing)
        self.assertIn("Hunt siblings via the guarded twin", briefing)

    def test_briefing_includes_triage_rules_and_fp_taxonomy(self):
        briefing = self.mod.build_briefing()
        # A distinctive phrase from triage_rules.
        self.assertIn("guarded twin is the strongest static-review signal", briefing)
        # A distinctive heading from the FP taxonomy markdown.
        self.assertIn("CPython false-positive taxonomy", briefing)
        self.assertIn("false-positive", briefing.lower())

    def test_briefing_lists_the_scanner_per_shape(self):
        briefing = self.mod.build_briefing()
        # Each shape names the scanner that surfaces it.
        self.assertIn("scan_recursion_guards.py", briefing)
        self.assertIn("surfaced by", briefing)

    # --- catalog-dir behavior ----------------------------------------------

    def test_catalog_dir_adds_findings_section(self):
        with tempfile.TemporaryDirectory(prefix="cpyrt_catalog_") as tmp:
            report = Path(tmp) / "reports" / "CRF-0001-tuple-hash-overflow"
            report.mkdir(parents=True)
            (report / "meta.json").write_text(
                json.dumps(
                    {
                        "id": "CRF-0001",
                        "title": "tuple_hash native-stack overflow on deep nesting",
                        "status": "reported",
                        "sites": ["Objects/tupleobject.c tuple_hash:412"],
                        "category": "unguarded-recursion-in-slot",
                    }
                ),
                encoding="utf-8",
            )
            briefing = self.mod.build_briefing(catalog_dir=tmp)

        self.assertIn("Previously-recorded findings", briefing)
        self.assertIn("confirm, don't re-litigate", briefing.lower())
        self.assertIn("CRF-0001", briefing)
        self.assertIn("tuple_hash native-stack overflow on deep nesting", briefing)
        self.assertIn("Objects/tupleobject.c tuple_hash:412", briefing)
        self.assertIn("reported", briefing)

    def test_catalog_dir_tolerates_signature_dict_layout(self):
        # meta.json using the dict-shaped `signature` (OOM-findings layout).
        with tempfile.TemporaryDirectory(prefix="cpyrt_catalog_") as tmp:
            report = Path(tmp) / "reports" / "CRF-0002-double-free"
            report.mkdir(parents=True)
            (report / "meta.json").write_text(
                json.dumps(
                    {
                        "id": "CRF-0002",
                        "title": "double-free on error path",
                        "signature": {"site_frame": "foo@Parser/pegen_errors.c:363"},
                    }
                ),
                encoding="utf-8",
            )
            briefing = self.mod.build_briefing(catalog_dir=tmp)

        self.assertIn("CRF-0002", briefing)
        self.assertIn("foo@Parser/pegen_errors.c:363", briefing)
        # Missing status defaults to "confirmed".
        self.assertIn("confirmed", briefing)

    def test_missing_catalog_dir_omits_section_gracefully(self):
        briefing = self.mod.build_briefing(catalog_dir="/nonexistent/path/xyz")
        self.assertNotIn("Previously-recorded findings", briefing)
        # The rest of the briefing is still well-formed.
        self.assertIn("Bug-shape catalog", briefing)

    def test_empty_catalog_dir_omits_section(self):
        with tempfile.TemporaryDirectory(prefix="cpyrt_catalog_") as tmp:
            briefing = self.mod.build_briefing(catalog_dir=tmp)
        self.assertNotIn("Previously-recorded findings", briefing)

    def test_no_catalog_dir_omits_section(self):
        briefing = self.mod.build_briefing()
        self.assertNotIn("Previously-recorded findings", briefing)

    # --- overrides / degradation -------------------------------------------

    def test_custom_shapes_path_is_used(self):
        with tempfile.TemporaryDirectory(prefix="cpyrt_shapes_") as tmp:
            shapes_file = Path(tmp) / "shapes.json"
            shapes_file.write_text(
                json.dumps(
                    {
                        "shapes": [
                            {
                                "id": "planted-shape-xyz",
                                "title": "a planted shape",
                                "pattern": "some pattern",
                                "guarded_twin": "the planted guarded twin",
                                "hunt": "hunt directive",
                                "severity": "FIX",
                                "scanner": "planted_scanner.py",
                            }
                        ],
                        "triage_rules": ["a planted triage rule"],
                    }
                ),
                encoding="utf-8",
            )
            briefing = self.mod.build_briefing(shapes_path=str(shapes_file))

        self.assertIn("planted-shape-xyz", briefing)
        self.assertIn("the planted guarded twin", briefing)
        self.assertIn("a planted triage rule", briefing)

    def test_missing_shapes_file_degrades_gracefully(self):
        briefing = self.mod.build_briefing(
            shapes_path="/nonexistent/shapes.json",
            non_bugs_path="/nonexistent/nonbugs.md",
        )
        # No crash; the three rules and headings still render.
        self.assertIn("Your three informed-mode rules", briefing)
        self.assertIn("no shapes loaded", briefing)
        self.assertIn("taxonomy file missing", briefing)

    def test_output_is_markdown_not_json(self):
        briefing = self.mod.build_briefing()
        self.assertTrue(briefing.lstrip().startswith("#"))
        self.assertTrue(briefing.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
