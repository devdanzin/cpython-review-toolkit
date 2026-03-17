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
        source = (
            "static int\n"
            "my_func(int x)\n"
            "{\n"
            "    return x + 1;\n"
            "}\n"
        )
        funcs = mod.find_functions(source)
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
        funcs = mod.find_functions(source)
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0]["name"], "list_append")

    def test_no_functions(self):
        source = "#define FOO 42\nint x = 0;\n"
        funcs = mod.find_functions(source)
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
        funcs = mod.find_functions(source)
        self.assertEqual(len(funcs), 2)
        names = {f["name"] for f in funcs}
        self.assertEqual(names, {"foo", "bar"})

    def test_skips_control_keywords(self):
        source = (
            "void\n"
            "test(void)\n"
            "{\n"
            "    if (x)\n"
            "    {\n"
            "        return;\n"
            "    }\n"
            "}\n"
        )
        funcs = mod.find_functions(source)
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
        body = "\n".join([
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
        ])
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
        with TempProject({
            "Objects/test.c": (
                "static int\n"
                "simple(int x)\n"
                "{\n"
                "    return x;\n"
                "}\n"
            ),
        }) as root:
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


if __name__ == "__main__":
    unittest.main()
