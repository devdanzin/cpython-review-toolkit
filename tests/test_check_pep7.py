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

    def test_line_too_long_is_off_by_default(self):
        """TK-19: PEP 7's 79-col rule is soft and CPython breaks it ~3,000x."""
        long_line = "    int " + "x" * 80 + " = 0;\n"
        source = f"void foo(void) {{\n{long_line}}}\n"
        violations = mod.check_file(source)
        rules = {v["rule"] for v in violations}
        self.assertNotIn("line-too-long", rules)

    def test_line_too_long_when_explicitly_enabled(self):
        long_line = "    int " + "x" * 80 + " = 0;\n"
        source = f"void foo(void) {{\n{long_line}}}\n"
        violations = mod.check_file(
            source,
            rules=mod.ALL_RULES,
            line_limit=79,
        )
        rules = {v["rule"] for v in violations}
        self.assertIn("line-too-long", rules)

    def test_keyword_no_space(self):
        source = "void foo(void) {\n    if(x) { return; }\n}\n"
        violations = mod.check_file(source)
        rules = {v["rule"] for v in violations}
        self.assertIn("keyword-space", rules)

    def test_clean_code_no_violations(self):
        source = "void\nfoo(void)\n{\n    if (x) {\n        return;\n    }\n}\n"
        violations = mod.check_file(source)
        # Filter out rules that this clean code wouldn't trigger.
        meaningful = [
            v
            for v in violations
            if v["rule"] in ("tab-indent", "trailing-whitespace", "keyword-space")
        ]
        self.assertEqual(len(meaningful), 0)

    def test_ignores_multiline_comments(self):
        source = (
            "/*\n\tThis is a comment with tabs\n*/\nvoid foo(void) {\n    return;\n}\n"
        )
        violations = mod.check_file(source)
        tab_violations = [v for v in violations if v["rule"] == "tab-indent"]
        # Should not flag tabs inside comments.
        self.assertEqual(len(tab_violations), 0)


class TestRuleTiers(unittest.TestCase):
    """TK-19: rules PEP 7 qualifies must not fire on untouched code."""

    _BRACELESS = "void\nfoo(void)\n{\n    if (x)\n        return;\n}\n"

    def test_missing_braces_silent_on_whole_tree(self):
        """PEP 7: 'do not add them to code you are not otherwise modifying'."""
        violations = mod.check_file(self._BRACELESS)
        self.assertNotIn("missing-braces", {v["rule"] for v in violations})

    def test_missing_braces_fires_inside_a_diff(self):
        violations = mod.check_file(
            self._BRACELESS,
            rules=mod.ALL_RULES,
            changed_lines={4},
        )
        self.assertIn("missing-braces", {v["rule"] for v in violations})

    def test_missing_braces_silent_outside_the_changed_lines(self):
        violations = mod.check_file(
            self._BRACELESS,
            rules=mod.ALL_RULES,
            changed_lines={99},
        )
        self.assertNotIn("missing-braces", {v["rule"] for v in violations})

    # D-1: the rule used to look ahead a FIXED 2 lines from the control keyword,
    # assuming the condition ended there. It does not when the condition spans
    # lines and the brace sits on its own -- this codebase's deliberate Allman
    # sub-convention for multi-line conditions (~60 sites in typeobject.c). The
    # first continuation line neither starts nor ends with `{`, so the rule
    # fired on a correctly braced block: 4 FPs of 153, all 2024-2026, because
    # multi-line Allman conditions are how new code here is written.

    # Note the exact shape, taken from Objects/typeobject.c:1675. The rule only
    # ever *sees* a multi-line condition when its first line happens to end in
    # `)` -- here from the inner call `PyUnicode_Check(module)` -- because
    # _CONTROL_NO_BRACE anchors on `\)\s*$`. A first line ending in `&&` is
    # invisible to the rule entirely; that is a separate, pre-existing recall
    # limit and not what this fix is about.
    _MULTILINE_BRACED = (
        "static void\n"
        "foo(void)\n"
        "{\n"
        "    if (PyUnicode_Check(module)\n"
        "        && !_PyUnicode_Equal(module, &_Py_ID(builtins))\n"
        "        && !_PyUnicode_Equal(module, &_Py_ID(__main__)))\n"
        "    {\n"
        "        return;\n"
        "    }\n"
        "}\n"
    )

    def test_multiline_condition_with_allman_brace_is_not_a_violation(self):
        violations = mod.check_file(
            self._MULTILINE_BRACED,
            rules=mod.ALL_RULES,
            changed_lines=set(range(1, 10)),
        )
        self.assertNotIn("missing-braces", {v["rule"] for v in violations})

    def test_a_genuinely_braceless_multiline_condition_still_fires(self):
        """The fix must not blind the rule to the real shape."""
        source = (
            "static void\n"
            "foo(void)\n"
            "{\n"
            "    if (PyUnicode_Check(module)\n"
            "        && other_condition(module))\n"
            "        return;\n"
            "}\n"
        )
        violations = mod.check_file(
            source, rules=mod.ALL_RULES, changed_lines=set(range(1, 8))
        )
        self.assertIn("missing-braces", {v["rule"] for v in violations})

    def test_three_line_condition_is_handled(self):
        """The old 2-line cap mis-fired on any 3+-line condition independently."""
        source = (
            "static void\n"
            "foo(void)\n"
            "{\n"
            "    if (a(x)\n"
            "        && b(y)\n"
            "        && c(z))\n"
            "    {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        violations = mod.check_file(
            source, rules=mod.ALL_RULES, changed_lines=set(range(1, 11))
        )
        self.assertNotIn("missing-braces", {v["rule"] for v in violations})

    def test_same_line_brace_after_multiline_condition(self):
        source = (
            "static void\n"
            "foo(void)\n"
            "{\n"
            "    if (a(x)\n"
            "        && b(y)) {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        violations = mod.check_file(
            source, rules=mod.ALL_RULES, changed_lines=set(range(1, 9))
        )
        self.assertNotIn("missing-braces", {v["rule"] for v in violations})

    def test_single_line_condition_unaffected(self):
        """The commonest form must behave exactly as before."""
        violations = mod.check_file(
            self._BRACELESS, rules=mod.ALL_RULES, changed_lines={4}
        )
        self.assertIn("missing-braces", {v["rule"] for v in violations})

    def test_func_call_space_rule_is_gone(self):
        """TK-19: the rule fired on `#define MAX (5 + ...)` and `assert (...)`."""
        self.assertNotIn("func-call-space", mod.ALL_RULES)
        source = "#define MAX_INTMAX_CHARS (5 + (SIZEOF_INTMAX_T * 53) / 22)\n"
        violations = mod.check_file(source, rules=mod.ALL_RULES, line_limit=79)
        self.assertEqual([v for v in violations if v["rule"] == "func-call-space"], [])

    def test_keyword_space_ignores_preprocessor_conditionals(self):
        source = "#if(defined SOMETHING)\nint x;\n#endif\n"
        violations = mod.check_file(source)
        self.assertNotIn("keyword-space", {v["rule"] for v in violations})

    def test_keyword_space_still_fires_on_real_code(self):
        source = "void f(void) {\n    switch(kind) {\n    }\n}\n"
        violations = mod.check_file(source)
        self.assertIn("keyword-space", {v["rule"] for v in violations})


class TestDiffParsing(unittest.TestCase):
    """--diff-only line-scope extraction."""

    def test_parse_unified_zero_diff(self):
        diff = (
            "diff --git a/Objects/x.c b/Objects/x.c\n"
            "--- a/Objects/x.c\n"
            "+++ b/Objects/x.c\n"
            "@@ -10,0 +11,3 @@\n"
            "+one\n+two\n+three\n"
            "@@ -40,1 +44 @@\n"
            "+four\n"
        )
        scope = mod.parse_diff(diff)
        self.assertEqual(scope, {"Objects/x.c": {11, 12, 13, 44}})

    def test_pure_deletion_hunks_are_dropped(self):
        diff = "--- a/Objects/y.c\n+++ b/Objects/y.c\n@@ -10,3 +9,0 @@\n-gone\n"
        self.assertEqual(mod.parse_diff(diff), {})


class TestCheckHeaderGuard(unittest.TestCase):
    """Test header guard detection."""

    def test_missing_guard(self):
        source = "typedef int MyType;\n"
        violations = mod.check_header_guard(source, "test.h")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["rule"], "header-guard")

    def test_has_guard(self):
        source = "#ifndef TEST_H\n#define TEST_H\ntypedef int MyType;\n#endif\n"
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

    def test_argument_clinic_output_is_exempt(self):
        """TK-19: `clinic/*.c.h` is #included once mid-file; a guard breaks it."""
        source = "/*[clinic input]\npreserve\n[clinic start generated code]*/\n"
        violations = mod.check_header_guard(source, "Objects/clinic/bytesobject.c.h")
        self.assertEqual(violations, [])

    def test_stringlib_template_is_exempt(self):
        """A guard here would break the per-width re-inclusion."""
        source = "/* stringlib: codec implementations */\nvoid f(void) {}\n"
        violations = mod.check_header_guard(source, "Objects/stringlib/codecs.h")
        self.assertEqual(violations, [])

    def test_generated_header_is_exempt(self):
        source = "/* File generated by Tools/ssl/make_ssl_data.py */\nint x;\n"
        violations = mod.check_header_guard(source, "Modules/_ssl_data_36.h")
        self.assertEqual(violations, [])

    def test_vendored_header_is_exempt(self):
        source = "/* upstream expat */\nint x;\n"
        violations = mod.check_header_guard(source, "Modules/expat/ascii.h")
        self.assertEqual(violations, [])

    def test_hand_written_header_still_flagged(self):
        source = "/* Declarations shared by the io module */\nint x;\n"
        violations = mod.check_header_guard(source, "Modules/_io/_iomodule.h")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["rule"], "header-guard")


class TestAnalyze(unittest.TestCase):
    """Test full PEP 7 analysis."""

    def test_basic_project(self):
        with TempProject(
            {
                "Objects/test.c": ("void foo(void) {\n\treturn;\n}\n"),
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertGreater(result["files_analyzed"], 0)
            self.assertIn("files", result)
            self.assertIn("summary", result)
            self.assertGreater(result["summary"]["total_violations"], 0)

    def test_standard_envelope(self):
        """TK-18: every scanner must expose `findings`."""
        with TempProject(
            {
                "Objects/test.c": ("void foo(void) {\n\treturn;\n}\n"),
            }
        ) as root:
            result = mod.analyze(str(root))
            for key in (
                "project_root",
                "scan_root",
                "files_analyzed",
                "functions_analyzed",
                "findings",
                "summary",
            ):
                self.assertIn(key, result)
            self.assertTrue(result["findings"])
            finding = result["findings"][0]
            for key in ("type", "rule", "file", "line", "severity", "detail"):
                self.assertIn(key, finding)
            self.assertEqual(
                len(result["findings"]), result["summary"]["total_violations"]
            )
            # Back-compat grouping retained.
            self.assertIn("files", result)

    def test_whole_tree_run_skips_diff_gated_rules(self):
        with TempProject(
            {
                "Objects/test.c": (
                    "void\nfoo(void)\n{\n    if (x)\n        return;\n}\n"
                ),
            }
        ) as root:
            result = mod.analyze(str(root), line_limit=79)
            self.assertEqual(result["summary"]["total_findings"], 0)
            self.assertIn("missing-braces", result["summary"]["skipped_rules"])
            self.assertEqual(result["summary"]["diff_scope"], "whole-tree")

    def test_changed_files_enables_diff_gated_rules(self):
        with TempProject(
            {
                "Objects/test.c": (
                    "void\nfoo(void)\n{\n    if (x)\n        return;\n}\n"
                ),
            }
        ) as root:
            result = mod.analyze(
                str(root),
                changed_files=["Objects/test.c"],
                line_limit=79,
            )
            rules = {f["rule"] for f in result["findings"]}
            self.assertIn("missing-braces", rules)
            self.assertEqual(result["files_analyzed"], 1)

    def test_enable_rule_forces_a_diff_gated_rule_on(self):
        with TempProject(
            {
                "Objects/test.c": (
                    "void\nfoo(void)\n{\n    if (x)\n        return;\n}\n"
                ),
            }
        ) as root:
            result = mod.analyze(
                str(root),
                enable_rules=frozenset({"missing-braces"}),
            )
            rules = {f["rule"] for f in result["findings"]}
            self.assertIn("missing-braces", rules)

    def test_bad_git_ref_reports_an_error_not_silence(self):
        with TempProject({"Objects/test.c": "int x;\n"}) as root:
            result = mod.analyze(str(root), diff_only=True, diff_ref="nope")
            self.assertIn("diff_error", result["summary"])


if __name__ == "__main__":
    unittest.main()
