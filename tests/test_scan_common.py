"""Tests for scan_common helpers (stdlib-only)."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import import_script  # noqa: E402


class TestExtractNearbyComments(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_common")

    def test_no_comments(self):
        src = "int x = 1;\nint y = 2;\nint z = 3;\n"
        self.assertEqual(self.mod.extract_nearby_comments(src, 2), [])

    def test_line_comment_within_radius(self):
        src = "int x = 1;\n// a note here\nint z = 3;\n"
        comments = self.mod.extract_nearby_comments(src, 2, radius=1)
        self.assertIn("a note here", comments)

    def test_block_comment_within_radius(self):
        src = "int x = 1;\n/* block comment */\nint z = 3;\n"
        comments = self.mod.extract_nearby_comments(src, 2, radius=1)
        self.assertTrue(any("block comment" in c for c in comments))

    def test_multiline_block_comment(self):
        src = (
            "int x = 1;\n"
            "/* line 2 of comment\n"
            " * line 3 of comment\n"
            " * line 4 of comment */\n"
            "int y = 5;\n"
            "int z = 6;\n"
        )
        # Finding on line 5 should see the multi-line block comment.
        comments = self.mod.extract_nearby_comments(src, 5, radius=1)
        self.assertTrue(any("line 2 of comment" in c for c in comments))

    def test_comment_outside_radius(self):
        src = "// far away\n" + "int x = 0;\n" * 20
        # Line 15 is far from the comment on line 1 with default radius.
        comments = self.mod.extract_nearby_comments(src, 15, radius=5)
        self.assertEqual(comments, [])

    def test_empty_source(self):
        self.assertEqual(self.mod.extract_nearby_comments("", 1), [])


class TestHasSafetyAnnotation(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_common")

    def test_positive_safety_colon(self):
        self.assertTrue(
            self.mod.has_safety_annotation(["safety: reviewed by bob"])
        )

    def test_positive_intentional(self):
        self.assertTrue(
            self.mod.has_safety_annotation(["this is intentional behavior"])
        )

    def test_positive_case_insensitive(self):
        self.assertTrue(
            self.mod.has_safety_annotation(["This Is Safe to ignore"])
        )

    def test_positive_gil_held(self):
        self.assertTrue(
            self.mod.has_safety_annotation(["gil-held by caller"])
        )

    def test_negative_no_keyword(self):
        self.assertFalse(
            self.mod.has_safety_annotation(["just a regular comment"])
        )

    def test_negative_empty(self):
        self.assertFalse(self.mod.has_safety_annotation([]))


class TestMakeFinding(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_common")

    def test_required_keys_present(self):
        f = self.mod.make_finding(
            "my_type",
            classification="FIX",
            severity="high",
            detail="boom",
        )
        for k in (
            "type", "file", "line", "function",
            "classification", "severity", "confidence", "detail",
        ):
            self.assertIn(k, f)
        self.assertEqual(f["type"], "my_type")
        self.assertEqual(f["confidence"], "high")  # default

    def test_extra_kwargs_merged(self):
        f = self.mod.make_finding(
            "t",
            classification="CONSIDER",
            severity="low",
            detail="d",
            api_call="PyList_New",
            variable="obj",
        )
        self.assertEqual(f["api_call"], "PyList_New")
        self.assertEqual(f["variable"], "obj")


class TestLoadJsonData(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_common")

    def test_missing_file_warns_and_returns_empty(self):
        missing = Path(tempfile.gettempdir()) / "cpyrt_does_not_exist_xyz.json"
        if missing.exists():
            missing.unlink()
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = self.mod.load_json_data(missing)
        self.assertEqual(result, {})
        self.assertIn("WARNING", buf.getvalue())
        self.assertIn("Failed to load", buf.getvalue())

    def test_invalid_json_warns_and_returns_empty(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        ) as f:
            f.write("not { valid json")
            path = Path(f.name)
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                result = self.mod.load_json_data(path)
            self.assertEqual(result, {})
            self.assertIn("WARNING", buf.getvalue())
        finally:
            path.unlink()

    def test_valid_json_round_trip(self):
        data = {"foo": "bar", "n": 3}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        ) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            result = self.mod.load_json_data(path)
            self.assertEqual(result, data)
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
