"""Tests for analyze_includes.py.

The invariant these tests protect: every include directive is resolved to a
real path before it is classified or graphed. CPython includes its internal
headers by bare name (``#include "pycore_object.h"``), so text-based
classification buckets them as "public" and text-keyed graph edges never match
a path-keyed node, which makes ``cycles`` a tautology.
"""

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
        self.assertEqual(mod.classify_api_tier("internal/pycore_object.h"), "internal")

    def test_resolved_paths(self):
        self.assertEqual(mod.classify_api_tier("Include/object.h"), "public")
        self.assertEqual(mod.classify_api_tier("Include/cpython/object.h"), "cpython")
        self.assertEqual(
            mod.classify_api_tier("Include/internal/pycore_object.h"), "internal"
        )

    def test_bare_pycore_name_is_internal(self):
        # CPython writes `#include "pycore_object.h"` with no path component.
        # Text classification called that "public"; 148 headers tree-wide.
        self.assertEqual(mod.classify_api_tier("pycore_object.h"), "internal")

    def test_generated_and_vendored_are_not_public(self):
        self.assertEqual(mod.classify_api_tier("Modules/clinic/_ssl.c.h"), "generated")
        self.assertEqual(
            mod.classify_api_tier("Modules/_hacl/Hacl_Hash_SHA2.h"), "vendored"
        )
        self.assertEqual(mod.classify_api_tier("Objects/mimalloc/alloc.c"), "vendored")
        self.assertEqual(
            mod.classify_api_tier("Objects/stringlib/find.h"), "other-local"
        )


class TestResolveInclude(unittest.TestCase):
    """Test directive -> on-disk path resolution."""

    def test_bare_internal_header_resolves(self):
        with TempProject(
            {
                "Objects/tupleobject.c": '#include "pycore_tuple.h"\n',
                "Include/internal/pycore_tuple.h": "#define X 1\n",
            }
        ) as root:
            resolved = mod.resolve_include(
                "pycore_tuple.h", root / "Objects" / "tupleobject.c", root
            )
            self.assertIsNotNone(resolved)
            self.assertEqual(
                resolved.relative_to(root).as_posix(),
                "Include/internal/pycore_tuple.h",
            )

    def test_own_directory_wins(self):
        with TempProject(
            {
                "Objects/stringlib/find.h": "#define F 1\n",
                "Objects/stringlib/split.h": '#include "find.h"\n',
            }
        ) as root:
            resolved = mod.resolve_include(
                "find.h", root / "Objects" / "stringlib" / "split.h", root
            )
            self.assertEqual(
                resolved.relative_to(root).as_posix(), "Objects/stringlib/find.h"
            )

    def test_unresolvable_returns_none(self):
        with TempProject({"Objects/foo.c": "\n"}) as root:
            self.assertIsNone(
                mod.resolve_include("windows.h", root / "Objects" / "foo.c", root)
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

    def test_deep_chain_does_not_recurse(self):
        # The resolved graph carries header->header edges and real chain depth;
        # a recursive DFS blows the Python stack on a full CPython tree.
        graph = {f"h{i}.h": [f"h{i + 1}.h"] for i in range(5000)}
        graph["h5000.h"] = []
        self.assertEqual(mod.detect_cycles(graph), [])


class TestAnalyze(unittest.TestCase):
    """Test full include graph analysis."""

    def test_basic_project(self):
        with TempProject(
            {
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
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertGreater(result["files_analyzed"], 0)
            self.assertIn("include_graph", result)
            self.assertIn("fan_in", result)
            self.assertIn("cycles", result)
            self.assertIn("api_tiers", result)
            self.assertIn("reverse_graph", result)
            self.assertIn("symbol_fan_in", result)

    def test_bare_internal_include_is_tiered_internal(self):
        # The blocking defect: Objects/ reported internal: 0 for a tree where
        # every file includes pycore_object.h.
        with TempProject(
            {
                "Objects/tupleobject.c": (
                    '#include "Python.h"\n'
                    '#include "pycore_object.h"\n'
                    '#include "pycore_tuple.h"\n'
                ),
                "Include/internal/pycore_object.h": "#define A 1\n",
                "Include/internal/pycore_tuple.h": "#define B 1\n",
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["summary"]["internal_headers"], 2)
            tiers = {
                e["resolved"]: e["tier"]
                for e in result["include_graph"]["Objects/tupleobject.c"]
            }
            self.assertEqual(tiers["Include/internal/pycore_object.h"], "internal")
            self.assertEqual(tiers["Include/internal/pycore_tuple.h"], "internal")

    def test_header_cycle_is_detected_end_to_end(self):
        # pycore_structs.h:55 <-> pycore_context.h:8 is the tree's only real
        # cycle; the text-keyed graph could never see it because no edge target
        # matched a node key.
        with TempProject(
            {
                "Include/internal/pycore_structs.h": (
                    "#ifndef Py_INTERNAL_STRUCTS_H\n"
                    "#define Py_INTERNAL_STRUCTS_H\n"
                    '#include "pycore_context.h"\n'
                    "#endif\n"
                ),
                "Include/internal/pycore_context.h": (
                    "#ifndef Py_INTERNAL_CONTEXT_H\n"
                    "#define Py_INTERNAL_CONTEXT_H\n"
                    '#include "pycore_structs.h"\n'
                    "#endif\n"
                ),
                "Objects/object.c": '#include "pycore_structs.h"\n',
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["summary"]["cycles_found"], 1)
            members = set(result["cycles"][0])
            self.assertEqual(
                members,
                {
                    "Include/internal/pycore_structs.h",
                    "Include/internal/pycore_context.h",
                },
            )

    def test_cycle_is_found_from_a_narrowed_scan(self):
        # A header<->header cycle is invisible from a subdirectory scan unless
        # cycles are computed tree-wide.
        with TempProject(
            {
                "Include/internal/pycore_a.h": '#include "pycore_b.h"\n',
                "Include/internal/pycore_b.h": '#include "pycore_a.h"\n',
                "Objects/foo.c": '#include "pycore_a.h"\n',
            }
        ) as root:
            result = mod.analyze(str(root / "Objects"))
            self.assertEqual(result["files_analyzed"], 2)  # foo.c + object.c marker
            self.assertEqual(result["summary"]["cycles_found"], 1)

    def test_fan_in_is_tree_wide_with_a_scoped_count_alongside(self):
        with TempProject(
            {
                "Include/internal/pycore_tuple.h": "#define B 1\n",
                "Objects/a.c": '#include "pycore_tuple.h"\n',
                "Objects/b.c": '#include "pycore_tuple.h"\n',
                "Python/c.c": '#include "pycore_tuple.h"\n',
            }
        ) as root:
            result = mod.analyze(str(root / "Objects"))
            row = next(
                r
                for r in result["fan_in"]
                if r["header"] == "Include/internal/pycore_tuple.h"
            )
            self.assertEqual(row["count"], 3)
            self.assertEqual(row["within_scope"], 2)
            self.assertEqual(row["tier"], "internal")
            self.assertIn("fan_in_scope", result["summary"])

    def test_symbol_fan_in_beats_include_fan_in_for_public_headers(self):
        # Python.h is a mega-include, so tupleobject.h has include fan-in 1
        # while PyTuple_* is referenced everywhere.
        with TempProject(
            {
                "Include/Python.h": (
                    "#ifndef Py_PYTHON_H\n"
                    "#define Py_PYTHON_H\n"
                    '#include "tupleobject.h"\n'
                    "#endif\n"
                ),
                "Include/tupleobject.h": (
                    "PyAPI_FUNC(PyObject *) PyTuple_New(Py_ssize_t size);\n"
                    "#define PyTuple_GET_ITEM(op, i) 0\n"
                ),
                "Objects/a.c": '#include "Python.h"\nPyTuple_New(1);\n',
                "Objects/b.c": '#include "Python.h"\nPyTuple_GET_ITEM(x, 0);\n',
                "Python/c.c": '#include "Python.h"\nPyTuple_New(2);\n',
            }
        ) as root:
            result = mod.analyze(str(root))
            row = next(
                r
                for r in result["symbol_fan_in"]
                if r["header"] == "Include/tupleobject.h"
            )
            self.assertEqual(row["include_fan_in"], 1)
            self.assertEqual(row["referencing_files"], 3)
            self.assertEqual(row["tier"], "public")

    def test_reverse_graph_answers_who_includes_me(self):
        with TempProject(
            {
                "Include/internal/pycore_tuple.h": "#define B 1\n",
                "Objects/a.c": '#include "pycore_tuple.h"\n',
                "Objects/b.c": '#include "pycore_tuple.h"\n',
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertEqual(
                result["reverse_graph"]["Include/internal/pycore_tuple.h"],
                ["Objects/a.c", "Objects/b.c"],
            )

    def test_unresolved_directives_are_reported_not_called_public(self):
        with TempProject({"Modules/winmod.c": '#include "windows.h"\n'}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["summary"]["local_directives"], 1)
            self.assertEqual(result["summary"]["local_directives_unresolved"], 1)
            self.assertIn("windows.h", result["api_tiers"]["unresolved"])
            self.assertNotIn("windows.h", result["api_tiers"]["public"])

    def test_single_file(self):
        with TempProject(
            {"test.c": "#include <stdio.h>\nvoid foo(void) {}\n"},
            cpython_markers=True,
        ) as root:
            result = mod.analyze(str(root / "test.c"))
            self.assertGreater(result["files_analyzed"], 0)

    def test_empty_project(self):
        with TempProject({}, cpython_markers=False) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["files_analyzed"], 0)


class TestExtractExportedSymbols(unittest.TestCase):
    def test_api_func_data_typeobject_and_macros(self):
        source = (
            "#ifndef Py_TUPLEOBJECT_H\n"
            "#define Py_TUPLEOBJECT_H\n"
            "PyAPI_DATA(PyTypeObject) PyTuple_Type;\n"
            "PyAPI_FUNC(PyObject *) PyTuple_New(Py_ssize_t size);\n"
            "#define PyTuple_GET_ITEM(op, i) 0\n"
            "#endif\n"
        )
        symbols = mod.extract_exported_symbols(source)
        self.assertIn("PyTuple_Type", symbols)
        self.assertIn("PyTuple_New", symbols)
        self.assertIn("PyTuple_GET_ITEM", symbols)
        # The include guard is boilerplate, not API.
        self.assertNotIn("Py_TUPLEOBJECT_H", symbols)


class TestFindCpythonRoot(unittest.TestCase):
    """Test CPython root detection."""

    def test_finds_root(self):
        with TempProject(
            {
                "Objects/foo.c": "void foo(void) {}\n",
            }
        ) as root:
            found = mod.find_cpython_root(root / "Objects")
            self.assertEqual(found, root)

    def test_no_root(self):
        with TempProject({}, cpython_markers=False) as root:
            found = mod.find_cpython_root(root)
            self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
