"""Tests for the shared tree-sitter chassis: scrub_macros and parse_health.

This module is vendored into five sibling toolkits, so a regression here is a
regression everywhere. It had no direct test coverage before the
function-generating-macro fix.

The invariant every scrub must hold: byte length and newline count are
preserved, so every ``start_byte``/``start_point`` derived from the scrubbed
tree still indexes the ORIGINAL source. Each test below asserts that alongside
whatever behaviour it is checking.
"""

import unittest

from helpers import import_script

tsu = import_script("tree_sitter_utils")


def assert_layout_preserved(test, original: bytes, scrubbed: bytes) -> None:
    test.assertEqual(len(scrubbed), len(original), "byte length changed")
    test.assertEqual(
        scrubbed.count(b"\n"), original.count(b"\n"), "newline count changed"
    )


class TestScrubLayoutInvariant(unittest.TestCase):
    def test_unrelated_source_is_returned_unchanged(self):
        src = b"static int f(void) { return 1; }\n"
        self.assertEqual(tsu.scrub_macros(src), src)

    def test_layout_preserved_for_every_macro_category(self):
        src = (
            b"typedef struct {\n"
            b"    PyObject_HEAD\n"
            b"    int x;\n"
            b"} Foo;\n"
            b"SLOT0(slot_nb_negative, __neg__)\n"
            b"static PyTypeObject T = { PyVarObject_HEAD_INIT(NULL, 0) };\n"
        )
        assert_layout_preserved(self, src, tsu.scrub_macros(src))


class TestFunctionGeneratingMacros(unittest.TestCase):
    """SLOT0/SLOT1 expand to whole functions; the invocation breaks the parse.

    The damage is not the missing generated function -- it is that the
    unparseable invocation swallows the REAL functions that follow it. Measured
    on Objects/typeobject.c: 35 invocations, 47 ERROR nodes, and `slot_tp_hash`,
    `slot_tp_call` and `_Py_slot_tp_getattro` invisible.
    """

    SOURCE = (
        b"#define SLOT0(FUNCNAME, DUNDER) \\\n"
        b"static PyObject * \\\n"
        b"FUNCNAME(PyObject *self) \\\n"
        b"{ \\\n"
        b"    return call_it(&_Py_ID(DUNDER)); \\\n"
        b"}\n"
        b"\n"
        b"SLOT0(slot_nb_negative, __neg__)\n"
        b"SLOT0(slot_nb_positive, __pos__)\n"
        b"\n"
        b"static PyObject *\n"
        b"real_function_after_macros(PyObject *self)\n"
        b"{\n"
        b"    PyErr_Clear();\n"
        b"    return NULL;\n"
        b"}\n"
    )

    def _functions(self, src: bytes) -> dict[str, int]:
        tree = tsu.parse_bytes(src)
        return {f["name"]: f["start_line"] for f in tsu.extract_functions(tree, src)}

    def test_the_real_function_after_the_macros_is_recovered(self):
        """The headline fix: an unparseable invocation must not eat its neighbours."""
        found = self._functions(self.SOURCE)
        self.assertIn("real_function_after_macros", found)

    def test_generated_function_names_are_visible_at_the_right_line(self):
        found = self._functions(self.SOURCE)
        self.assertEqual(found.get("slot_nb_negative"), 8)
        self.assertEqual(found.get("slot_nb_positive"), 9)

    def test_generated_names_are_not_garbled(self):
        """The stub must keep the identifier at its ORIGINAL byte offset.

        Callers pass the unscrubbed source to get_node_text, so a stub that
        shifts the name (a plain "int NAME(){}" prefix) makes every consumer
        read the wrong bytes.
        """
        for name in self._functions(self.SOURCE):
            self.assertRegex(name, r"^[A-Za-z_]\w*$", f"garbled name: {name!r}")

    def test_the_macro_definition_line_is_left_alone(self):
        scrubbed = tsu.scrub_macros(self.SOURCE)
        self.assertIn(b"#define SLOT0(FUNCNAME, DUNDER)", scrubbed)

    def test_layout_is_preserved(self):
        assert_layout_preserved(self, self.SOURCE, tsu.scrub_macros(self.SOURCE))

    def test_a_non_file_scope_use_is_not_substituted(self):
        """Only a file-scope invocation defines a function."""
        src = b"static int f(void) { return SLOT0(a, b); }\n"
        self.assertNotIn(b"int a(){}", tsu.scrub_macros(src))

    def test_a_non_identifier_first_argument_is_skipped(self):
        src = b"SLOT0(3 + 4, __neg__)\n"
        scrubbed = tsu.scrub_macros(src)
        assert_layout_preserved(self, src, scrubbed)
        self.assertNotIn(b"int", scrubbed)

    def test_a_stub_that_does_not_fit_is_skipped_not_truncated(self):
        """A short invocation must be left alone rather than corrupt offsets."""
        src = b"SLOT0(x)\n"
        scrubbed = tsu.scrub_macros(src)
        assert_layout_preserved(self, src, scrubbed)


class TestSplitMacroArgs(unittest.TestCase):
    def test_splits_on_top_level_commas_only(self):
        self.assertEqual(
            tsu._split_macro_args(b"a, f(b, c), d"), [b"a", b"f(b, c)", b"d"]
        )

    def test_single_argument(self):
        self.assertEqual(tsu._split_macro_args(b"only"), [b"only"])

    def test_bracketed_commas_are_not_separators(self):
        self.assertEqual(tsu._split_macro_args(b"x[1, 2], y"), [b"x[1, 2]", b"y"])


class TestParseHealth(unittest.TestCase):
    def test_reports_coverage_and_function_count(self):
        src = (
            b"static int a(void)\n{\n    return 1;\n}\n"
            b"static int b(void)\n{\n    return 2;\n}\n"
        )
        health = tsu.parse_health(tsu.parse_bytes(src), src)
        self.assertEqual(health["functions"], 2)
        self.assertEqual(health["error_nodes"], 0)
        # 8 code lines are attributed; lines_total is 9 because the trailing
        # newline opens a final empty line. Assert the count, not a threshold.
        self.assertEqual(health["lines_attributed"], 8)
        self.assertEqual(health["lines_total"], 9)

    def test_a_file_of_only_tables_has_low_coverage_and_no_functions(self):
        """Coverage is the canary: zero functions must not read as a clean scan."""
        src = b"static PyMethodDef m[] = {\n" + b"    {NULL},\n" * 20 + b"};\n"
        health = tsu.parse_health(tsu.parse_bytes(src), src)
        self.assertEqual(health["functions"], 0)
        self.assertEqual(health["coverage"], 0.0)

    def test_empty_source_does_not_divide_by_zero(self):
        health = tsu.parse_health(tsu.parse_bytes(b""), b"")
        self.assertEqual(health["functions"], 0)
        self.assertEqual(health["coverage"], 0.0)


if __name__ == "__main__":
    unittest.main()
