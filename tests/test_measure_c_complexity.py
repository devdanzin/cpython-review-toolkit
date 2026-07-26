"""Tests for measure_c_complexity.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("measure_c_complexity")


class TestFindFunctions(unittest.TestCase):
    """Test C function detection."""

    def test_simple_function(self):
        source = "static int\nmy_func(int x)\n{\n    return x + 1;\n}\n"
        funcs, _ = mod.find_functions(source)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["name"], "my_func")

    def test_pyobject_function(self):
        source = (
            "static PyObject *\n"
            "list_append(PyObject *self, PyObject *args)\n"
            "{\n"
            "    return Py_None;\n"
            "}\n"
        )
        funcs, _ = mod.find_functions(source)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["name"], "list_append")

    def test_no_functions(self):
        source = "#define FOO 42\nint x = 0;\n"
        funcs, _ = mod.find_functions(source)
        self.assertEqual(len(funcs), 0)

    def test_multiple_functions(self):
        source = (
            "void\n"
            "foo(void)\n"
            "{\n"
            "    return;\n"
            "}\n"
            "\n"
            "int\n"
            "bar(int x)\n"
            "{\n"
            "    return x;\n"
            "}\n"
        )
        funcs, _ = mod.find_functions(source)
        self.assertEqual(len(funcs), 2)
        names = {f["name"] for f in funcs}
        self.assertEqual(names, {"foo", "bar"})

    def test_skips_control_keywords(self):
        source = "void\ntest(void)\n{\n    if (x)\n    {\n        return;\n    }\n}\n"
        funcs, _ = mod.find_functions(source)
        # Should find test() but not treat 'if' as a function.
        for f in funcs:
            self.assertNotEqual(f["name"], "if")


class TestMeasureFunction(unittest.TestCase):
    """Test complexity metric computation."""

    def test_simple_function_metrics(self):
        func = {
            "name": "simple",
            "params": "int x",
            "body": "    return x + 1;",
            "start_line": 1,
            "end_line": 3,
        }
        metrics = mod.measure_function(func)
        self.assertEqual(metrics["name"], "simple")
        self.assertEqual(metrics["parameter_count"], 1)
        self.assertGreaterEqual(metrics["cyclomatic_complexity"], 1)
        self.assertGreaterEqual(metrics["score"], 1.0)

    def test_void_params(self):
        func = {
            "name": "no_args",
            "params": "void",
            "body": "    return;",
            "start_line": 1,
            "end_line": 3,
        }
        metrics = mod.measure_function(func)
        self.assertEqual(metrics["parameter_count"], 0)

    def test_complex_function(self):
        body = "\n".join(
            [
                "    if (x > 0) {",
                "        if (y > 0) {",
                "            if (z > 0) {",
                "                if (w > 0) {",
                "                    if (v > 0) {",
                "                        if (u > 0) {",
                "                            return 1;",
                "                        }",
                "                    }",
                "                }",
                "            }",
                "        }",
                "    }",
                "    return 0;",
            ]
        )
        func = {
            "name": "deep",
            "params": "int x, int y, int z, int w, int v, int u",
            "body": body,
            "start_line": 1,
            "end_line": 16,
        }
        metrics = mod.measure_function(func)
        self.assertGreater(metrics["nesting_depth"], 5)
        self.assertGreater(metrics["cyclomatic_complexity"], 5)

    def test_goto_counting(self):
        func = {
            "name": "with_goto",
            "params": "void",
            "body": (
                "    goto error;\n"
                "    goto done;\n"
                "error:\n"
                "    return -1;\n"
                "done:\n"
                "    return 0;\n"
            ),
            "start_line": 1,
            "end_line": 8,
        }
        metrics = mod.measure_function(func)
        self.assertEqual(metrics["goto_count"], 2)


class TestAnalyze(unittest.TestCase):
    """Test full complexity analysis."""

    def test_basic_project(self):
        with TempProject(
            {
                "Objects/test.c": ("static int\nsimple(int x)\n{\n    return x;\n}\n"),
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertGreater(result["functions_analyzed"], 0)
            self.assertIn("files", result)
            self.assertIn("hotspots", result)
            self.assertIn("summary", result)


class TestStripCommentsAndStrings(unittest.TestCase):
    """Test comment and string stripping."""

    def test_line_comment(self):
        result = mod.strip_comments_and_strings("x = 1; // comment\n")
        self.assertNotIn("comment", result)

    def test_block_comment(self):
        result = mod.strip_comments_and_strings("x = 1; /* block */ y = 2;")
        self.assertNotIn("block", result)
        self.assertIn("y = 2", result)

    def test_string_literal(self):
        result = mod.strip_comments_and_strings('x = "hello world";')
        self.assertNotIn("hello", result)

    def test_escaped_quote(self):
        result = mod.strip_comments_and_strings(r'x = "hello \"world\"";')
        self.assertNotIn("world", result)

    def test_multiline_comment_preserves_line_numbers(self):
        """A block comment must not shift the lines that follow it (TK-21)."""
        source = "a;\n/* one\n   two\n   three */\nb;\n"
        result = mod.strip_comments_and_strings(source)
        self.assertEqual(source.count("\n"), result.count("\n"))
        # `b;` must still be on line 5.
        self.assertEqual(result.split("\n")[4].strip(), "b;")

    def test_char_literal_containing_quote_does_not_run_away(self):
        """`'"'` is legal C and must not swallow the rest of the file."""
        source = "if (c == '\"') {\n    x = 1;\n}\nint keep_me;\n"
        result = mod.strip_comments_and_strings(source)
        self.assertEqual(source.count("\n"), result.count("\n"))
        self.assertIn("keep_me", result)


class TestMultiLineSignatures(unittest.TestCase):
    """TK-22: parameter lists spanning several lines must not be dropped."""

    def test_multiline_parameter_list(self):
        source = (
            "static PyObject *\n"
            "dict_subscript(PyObject *self,\n"
            "               PyObject *key)\n"
            "{\n"
            "    return NULL;\n"
            "}\n"
        )
        funcs, coverage = mod.find_functions(source)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["name"], "dict_subscript")
        self.assertEqual(funcs[0]["signature_lines"], 2)
        self.assertEqual(coverage["multiline_signatures"], 1)
        # start_line points at the return type, 1-indexed.
        self.assertEqual(funcs[0]["start_line"], 1)

    def test_four_line_parameter_list(self):
        source = (
            "static int\n"
            "wide(PyObject *a,\n"
            "     PyObject *b,\n"
            "     PyObject *c,\n"
            "     PyObject *d)\n"
            "{\n"
            "    return 0;\n"
            "}\n"
        )
        funcs, _ = mod.find_functions(source)
        self.assertEqual([f["name"] for f in funcs], ["wide"])
        self.assertEqual(funcs[0]["params"].count(","), 3)

    def test_macro_wrapped_return_type_picks_the_real_name(self):
        source = (
            "Py_LOCAL_INLINE(int) helper(PyObject *a,\n"
            "                            PyObject *b)\n"
            "{\n"
            "    return 0;\n"
            "}\n"
        )
        funcs, _ = mod.find_functions(source)
        self.assertEqual([f["name"] for f in funcs], ["helper"])

    def test_argument_clinic_impl_is_found(self):
        """The clinic marker sits between the signature and the brace."""
        source = (
            "/*[clinic input]\n"
            "bytearray.clear\n"
            "[clinic start generated code]*/\n"
            "\n"
            "static PyObject *\n"
            "bytearray_clear_impl(PyByteArrayObject *self)\n"
            "/*[clinic end generated code: output=abc input=def]*/\n"
            "{\n"
            "    return NULL;\n"
            "}\n"
        )
        funcs, _ = mod.find_functions(source)
        self.assertIn("bytearray_clear_impl", [f["name"] for f in funcs])

    def test_coverage_is_reported(self):
        source = "static int\nok(void)\n{\n    return 0;\n}\n"
        _, coverage = mod.find_functions(source)
        self.assertEqual(coverage["brace_blocks_seen"], 1)
        self.assertEqual(coverage["functions_parsed"], 1)
        self.assertEqual(coverage["coverage_pct"], 100.0)

    def test_initializer_is_not_a_function(self):
        source = "static PyType_Slot slots[] =\n{\n    {0, NULL},\n};\n"
        funcs, _ = mod.find_functions(source)
        self.assertEqual(funcs, [])


class TestCleanupLadder(unittest.TestCase):
    """TK-21: the goto-free manual-cleanup counter-metric."""

    def test_ladder_counts_repeated_cleanup(self):
        body = "\n".join(
            [
                "    PyObject *a = make_a();",
                "    PyObject *b = make_b();",
                "    if (x) {",
                "        Py_DECREF(a);",
                "        return NULL;",
                "    }",
                "    if (y) {",
                "        Py_DECREF(b);",
                "        return NULL;",
                "    }",
                "    return a;",
            ]
        )
        result = mod.measure_cleanup_ladder(body)
        self.assertEqual(result["owned_locals"], 2)
        self.assertEqual(result["returns_with_cleanup"], 2)
        self.assertEqual(result["manual_cleanup_ladder"], 4)

    def test_clean_function_scores_zero(self):
        body = "    int x = compute();\n    return x;\n"
        result = mod.measure_cleanup_ladder(body)
        self.assertEqual(result["manual_cleanup_ladder"], 0)

    def test_ladder_suppressed_when_goto_present(self):
        func = {
            "name": "with_goto",
            "params": "void",
            "body": "\n".join(
                [
                    "    PyObject *a = make_a();",
                    "    if (x)",
                    "        goto error;",
                    "    Py_DECREF(a);",
                    "    return 0;",
                    "error:",
                    "    Py_DECREF(a);",
                    "    return -1;",
                ]
            ),
            "start_line": 1,
            "end_line": 10,
        }
        metrics = mod.measure_function(func)
        self.assertGreater(metrics["goto_count"], 0)
        self.assertEqual(metrics["manual_cleanup_ladder"], 0)


class TestHotspotSelection(unittest.TestCase):
    """TK-21: relative rather than absolute hotspot threshold."""

    def _funcs(self, scores):
        return [{"name": f"f{i}", "score": s} for i, s in enumerate(scores)]

    def test_relative_threshold_fires_on_low_scores(self):
        """The old absolute 5.0 cutoff fired 3 times on all of Objects/."""
        funcs = self._funcs([2.5] + [1.0] * 99)
        hotspots, threshold = mod.select_hotspots(funcs, top_percent=2.0)
        self.assertEqual(threshold, 2.5)
        self.assertEqual([f["name"] for f in hotspots], ["f0"])

    def test_ties_at_the_cut_are_kept(self):
        funcs = self._funcs([3.0, 3.0, 3.0] + [1.0] * 97)
        hotspots, threshold = mod.select_hotspots(funcs, top_percent=1.0)
        self.assertEqual(threshold, 3.0)
        self.assertEqual(len(hotspots), 3)

    def test_absolute_floor_can_be_layered_on(self):
        funcs = self._funcs([2.5] + [1.0] * 99)
        hotspots, threshold = mod.select_hotspots(funcs, top_percent=2.0, min_score=5.0)
        self.assertEqual(threshold, 5.0)
        self.assertEqual(hotspots, [])

    def test_empty_corpus(self):
        hotspots, threshold = mod.select_hotspots([])
        self.assertEqual(hotspots, [])
        self.assertEqual(threshold, 0.0)

    def test_analyze_reports_threshold_and_coverage(self):
        with TempProject(
            {
                "Objects/test.c": ("static int\nsimple(int x)\n{\n    return x;\n}\n"),
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertIn("coverage", result)
            self.assertIn("hotspot_threshold", result["summary"])
            self.assertIn("signal_caveat", result["summary"])
            self.assertIn("cleanup_ladders", result)


if __name__ == "__main__":
    unittest.main()


class TestFunctionPointerParameterCount(unittest.TestCase):
    """D-19: commas inside a function-pointer parameter were counted.

    `do_lookup` (Objects/dictobject.c) takes 5 parameters and was reported as
    having 10 — the 4 scalars plus the 6 types inside its `compare` callback's
    own argument list. That pushed it past the `param_count > 6` scoring
    threshold and put it in dictobject.c's hotspot list purely as an artifact;
    corrected, it scores 1.0 and drops off. 2 of the 8 hotspots reported for
    that file were noise.
    """

    def test_commas_inside_a_function_pointer_are_not_parameters(self):
        self.assertEqual(
            mod._count_top_level_params(
                "PyDictObject *mp, PyDictKeysObject *dk, PyObject *key, "
                "Py_hash_t hash, "
                "int (*compare)(PyDictObject *, PyDictKeysObject *, void *, "
                "Py_ssize_t, PyObject *, Py_hash_t)"
            ),
            5,
        )

    def test_plain_parameters_are_unaffected(self):
        self.assertEqual(mod._count_top_level_params("int a, char *b, long c"), 3)
        self.assertEqual(mod._count_top_level_params("int a"), 1)

    def test_array_parameter_brackets_do_not_confuse_the_count(self):
        self.assertEqual(
            mod._count_top_level_params("int argc, char *argv[], void *ctx"), 3
        )

    def test_measure_function_uses_the_top_level_count(self):
        func = {
            "name": "do_lookup",
            "body": "{\n    return 0;\n}\n",
            "params": (
                "PyDictObject *mp, PyDictKeysObject *dk, PyObject *key, "
                "Py_hash_t hash, "
                "int (*compare)(PyDictObject *, PyDictKeysObject *, void *, "
                "Py_ssize_t, PyObject *, Py_hash_t)"
            ),
            "start_line": 1,
            "end_line": 3,
        }
        self.assertEqual(mod.measure_function(func)["parameter_count"], 5)


class TestMergedRunTruncationIsReported(unittest.TestCase):
    """D-20: a smaller file squeezed out of a merged run said nothing at all.

    The percentile is computed over the merged corpus, so running over
    Objects/dictobject.c + setobject.c together gave setobject.c zero hotspots
    — which reads as "clean here" when it means "outranked elsewhere". Silent
    truncation is the same failure mode as a zero denominator.
    """

    def test_a_file_with_functions_but_no_hotspots_is_named(self):
        big = "".join(
            "static int\n"
            f"big_{i}(int a, int b, int c, int d, int e, int f, int g, int h)\n"
            "{\n"
            + "".join(
                f"    if (a == {j}) {{ if (b == {j}) {{ if (c == {j}) return {j}; }} }}\n"
                for j in range(12)
            )
            + "    return 0;\n"
            "}\n"
            for i in range(10)
        )
        small = "static int\nsmall(int x)\n{\n    return x;\n}\n"
        with TempProject({"Objects/big.c": big, "Objects/small.c": small}) as root:
            result = mod.analyze(str(root))
        starved = result["summary"]["files_without_hotspots"]
        self.assertIn("Objects/small.c", starved)
        self.assertTrue(result["summary"]["files_without_hotspots_note"])

    def test_no_note_when_every_file_contributes(self):
        src = "static int\nf(int x)\n{\n    return x;\n}\n"
        with TempProject({"Objects/only.c": src}) as root:
            result = mod.analyze(str(root))
        self.assertEqual(result["summary"]["files_without_hotspots"], [])
        self.assertEqual(result["summary"]["files_without_hotspots_note"], "")


class TestPreprocessorBraceBalance(unittest.TestCase):
    """A #ifdef whose branches share a closing brace must not eat the function.

    CPython varies a condition across platforms by opening a brace in both
    branches of a #ifdef/#else and closing it once after the #endif --
    Modules/_io/fileio.c:483-491 is the live instance. Counting braces
    character-wise leaves the depth permanently above zero, the search runs off
    the end of the file, and the old code silently kept body_end at its
    body_start initialization: line_count 0, cyclomatic 1, score 1.00, while
    coverage_pct still counted the function as parsed.

    That hid 31 functions across Objects/ Modules/ Python/, including
    _io_FileIO___init___impl (254 lines, cyclomatic 57, the highest-churn
    function in _io) and dictobject.c's dictiter_iternextitem.
    """

    UNBALANCED = (
        "static int\n"
        "platform_forked(int a)\n"
        "{\n"
        "    int ret = 0;\n"
        "#ifdef MS_WINDOWS\n"
        "    if (GetLastError() == ERROR_INVALID_HANDLE) {\n"
        "#else\n"
        "    if (errno == EBADF) {\n"
        "#endif\n"
        "        goto error;\n"
        "    }\n"
        "    return ret;\n"
        "error:\n"
        "    return -1;\n"
        "}\n"
    )

    def test_shared_closing_brace_does_not_truncate(self):
        funcs, coverage = mod.find_functions(self.UNBALANCED)
        self.assertEqual(len(funcs), 1)
        fn = funcs[0]
        self.assertEqual(fn["name"], "platform_forked")
        # The real closing brace is the last line (1-indexed 15).
        self.assertEqual(fn["end_line"], 15)
        self.assertIn("goto error", fn["body"])
        self.assertEqual(coverage["extents_unresolved"], 0)

    def test_truncated_function_is_no_longer_zero_length(self):
        funcs, _ = mod.find_functions(self.UNBALANCED)
        measured = mod.measure_function(funcs[0])
        self.assertGreater(measured["line_count"], 0)
        self.assertGreater(measured["cyclomatic_complexity"], 1)

    def test_balanced_ifdef_still_resolves(self):
        source = (
            "static int\n"
            "both_balanced(int a)\n"
            "{\n"
            "#ifdef MS_WINDOWS\n"
            "    if (a) { return 1; }\n"
            "#else\n"
            "    if (a) { return 2; }\n"
            "#endif\n"
            "    return 0;\n"
            "}\n"
        )
        funcs, coverage = mod.find_functions(source)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["end_line"], 10)
        self.assertEqual(coverage["extents_unresolved"], 0)

    def test_nested_conditionals_track_independently(self):
        source = (
            "static int\n"
            "nested(int a)\n"
            "{\n"
            "#ifdef OUTER\n"
            "#  ifdef INNER\n"
            "    if (a) {\n"
            "#  else\n"
            "    if (!a) {\n"
            "#  endif\n"
            "        return 1;\n"
            "    }\n"
            "#endif\n"
            "    return 0;\n"
            "}\n"
        )
        funcs, coverage = mod.find_functions(source)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["end_line"], 14)
        self.assertEqual(coverage["extents_unresolved"], 0)

    def test_genuinely_empty_function_is_still_zero_and_not_flagged(self):
        """_Py_BreakPoint and friends really are empty -- do not manufacture a body."""
        source = "void\n_Py_BreakPoint(void)\n{\n}\n"
        funcs, coverage = mod.find_functions(source)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(mod.measure_function(funcs[0])["line_count"], 0)
        self.assertEqual(coverage["extents_unresolved"], 0)

    def test_unresolvable_extent_falls_back_and_is_reported(self):
        """No closing brace at all: fall back to a column-0 '}', never to nothing."""
        source = "static int\nrunaway(int a)\n{\n    if (a) {\n        return 1;\n}\n"
        funcs, coverage = mod.find_functions(source)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(coverage["extents_unresolved"], 1)
        # It fell back to the column-0 brace rather than collapsing to zero.
        self.assertGreater(funcs[0]["end_line"], funcs[0]["start_line"] + 1)

    def test_extents_unresolved_is_in_the_analyze_envelope(self):
        with TempProject({"Objects/x.c": self.UNBALANCED}) as root:
            result = mod.analyze(str(root))
        self.assertIn("extents_unresolved", result["coverage"])
