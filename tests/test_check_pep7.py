"""Tests for check_pep7.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("check_pep7")


class TestCheckFile(unittest.TestCase):
    """Test PEP 7 style checking on individual files."""

    def test_tab_indentation(self):
        source = "void foo(void) {\n\treturn;\n}\n"
        violations = mod.check_file(source)
        rules = {v["rule"] for v in violations}
        self.assertIn("tab-indent", rules)

    def test_trailing_whitespace(self):
        source = "void foo(void) {   \n    return;\n}\n"
        violations = mod.check_file(source)
        rules = {v["rule"] for v in violations}
        self.assertIn("trailing-whitespace", rules)

    def test_line_too_long(self):
        long_line = "    int " + "x" * 80 + " = 0;\n"
        source = f"void foo(void) {{\n{long_line}}}\n"
        violations = mod.check_file(source)
        rules = {v["rule"] for v in violations}
        self.assertIn("line-too-long", rules)

    def test_keyword_no_space(self):
        source = "void foo(void) {\n    if(x) { return; }\n}\n"
        violations = mod.check_file(source)
        rules = {v["rule"] for v in violations}
        self.assertIn("keyword-space", rules)

    def test_clean_code_no_violations(self):
        source = (
            "void\n"
            "foo(void)\n"
            "{\n"
            "    if (x) {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        violations = mod.check_file(source)
        # Filter out rules that this clean code wouldn't trigger.
        meaningful = [
            v for v in violations
            if v["rule"] in ("tab-indent", "trailing-whitespace",
                             "keyword-space")
        ]
        self.assertEqual(len(meaningful), 0)

    def test_ignores_multiline_comments(self):
        source = (
            "/*\n"
            "\tThis is a comment with tabs\n"
            "*/\n"
            "void foo(void) {\n"
            "    return;\n"
            "}\n"
        )
        violations = mod.check_file(source)
        tab_violations = [v for v in violations if v["rule"] == "tab-indent"]
        # Should not flag tabs inside comments.
        self.assertEqual(len(tab_violations), 0)


class TestCheckHeaderGuard(unittest.TestCase):
    """Test header guard detection."""

    def test_missing_guard(self):
        source = "typedef int MyType;\n"
        violations = mod.check_header_guard(source, "test.h")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["rule"], "header-guard")

    def test_has_guard(self):
        source = (
            "#ifndef TEST_H\n"
            "#define TEST_H\n"
            "typedef int MyType;\n"
            "#endif\n"
        )
        violations = mod.check_header_guard(source, "test.h")
        self.assertEqual(len(violations), 0)

    def test_pragma_once(self):
        source = "#pragma once\ntypedef int MyType;\n"
        violations = mod.check_header_guard(source, "test.h")
        self.assertEqual(len(violations), 0)

    def test_c_file_no_guard_needed(self):
        source = "void foo(void) {}\n"
        violations = mod.check_header_guard(source, "test.c")
        self.assertEqual(len(violations), 0)


class TestAnalyze(unittest.TestCase):
    """Test full PEP 7 analysis."""

    def test_basic_project(self):
        with TempProject({
            "Objects/test.c": (
                "void foo(void) {\n"
                "\treturn;\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            self.assertGreater(result["files_analyzed"], 0)
            self.assertIn("files", result)
            self.assertIn("summary", result)
            self.assertGreater(result["summary"]["total_violations"], 0)


if __name__ == "__main__":
    unittest.main()
