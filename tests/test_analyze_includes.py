"""Tests for analyze_includes.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("analyze_includes")


class TestExtractIncludes(unittest.TestCase):
    """Test #include directive extraction."""

    def test_local_include(self):
        source = '#include "Python.h"\n'
        result = mod.extract_includes(source)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["header"], "Python.h")
        self.assertEqual(result[0]["kind"], "local")

    def test_system_include(self):
        source = "#include <stdio.h>\n"
        result = mod.extract_includes(source)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["header"], "stdio.h")
        self.assertEqual(result[0]["kind"], "system")

    def test_multiple_includes(self):
        source = (
            '#include "Python.h"\n'
            "#include <stdlib.h>\n"
            '#include "internal/pycore_object.h"\n'
        )
        result = mod.extract_includes(source)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["header"], "Python.h")
        self.assertEqual(result[1]["header"], "stdlib.h")
        self.assertEqual(result[2]["header"], "internal/pycore_object.h")

    def test_no_includes(self):
        source = "void foo(void) {}\n"
        result = mod.extract_includes(source)
        self.assertEqual(len(result), 0)

    def test_include_with_space(self):
        source = '#  include "Python.h"\n'
        result = mod.extract_includes(source)
        self.assertEqual(len(result), 1)


class TestClassifyApiTier(unittest.TestCase):
    """Test header API tier classification."""

    def test_public_header(self):
        self.assertEqual(mod.classify_api_tier("object.h"), "public")

    def test_cpython_header(self):
        self.assertEqual(mod.classify_api_tier("cpython/object.h"), "cpython")

    def test_internal_header(self):
        self.assertEqual(
            mod.classify_api_tier("internal/pycore_object.h"), "internal"
        )


class TestDetectCycles(unittest.TestCase):
    """Test cycle detection in include graphs."""

    def test_no_cycles(self):
        graph = {"a.c": ["b.h"], "b.h": ["c.h"], "c.h": []}
        cycles = mod.detect_cycles(graph)
        self.assertEqual(len(cycles), 0)

    def test_simple_cycle(self):
        graph = {"a.h": ["b.h"], "b.h": ["a.h"]}
        cycles = mod.detect_cycles(graph)
        self.assertGreaterEqual(len(cycles), 1)

    def test_self_cycle(self):
        graph = {"a.h": ["a.h"]}
        cycles = mod.detect_cycles(graph)
        self.assertGreaterEqual(len(cycles), 1)


class TestAnalyze(unittest.TestCase):
    """Test full include graph analysis."""

    def test_basic_project(self):
        with TempProject({
            "Objects/listobject.c": (
                '#include "Python.h"\n'
                '#include "internal/pycore_list.h"\n'
                "void list_init(void) {}\n"
            ),
            "Include/internal/pycore_list.h": (
                "#ifndef PYCORE_LIST_H\n"
                "#define PYCORE_LIST_H\n"
                '#include "Python.h"\n'
                "#endif\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            self.assertGreater(result["files_analyzed"], 0)
            self.assertIn("include_graph", result)
            self.assertIn("fan_in", result)
            self.assertIn("cycles", result)
            self.assertIn("api_tiers", result)

    def test_single_file(self):
        with TempProject({
            "test.c": '#include <stdio.h>\nvoid foo(void) {}\n',
        }, cpython_markers=True) as root:
            result = mod.analyze(str(root / "test.c"))
            self.assertGreater(result["files_analyzed"], 0)

    def test_empty_project(self):
        with TempProject({}, cpython_markers=False) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["files_analyzed"], 0)


class TestFindCpythonRoot(unittest.TestCase):
    """Test CPython root detection."""

    def test_finds_root(self):
        with TempProject({
            "Objects/foo.c": "void foo(void) {}\n",
        }) as root:
            found = mod.find_cpython_root(root / "Objects")
            self.assertEqual(found, root)

    def test_no_root(self):
        with TempProject({}, cpython_markers=False) as root:
            found = mod.find_cpython_root(root)
            self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
