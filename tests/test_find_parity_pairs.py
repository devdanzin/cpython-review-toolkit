"""Tests for find_parity_pairs.py -- C-accelerator / pure-Python-twin discovery."""

import ast
import unittest

from helpers import TempProject, import_script


def _c_source(module: str) -> str:
    """A minimal C accelerator that a `PyInit_` sweep can verify."""
    return (
        '#include "Python.h"\n'
        "PyMODINIT_FUNC\n"
        f"PyInit_{module}(void)\n"
        "{\n"
        "    return NULL;\n"
        "}\n"
    )


class TestFindParityPairs(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("find_parity_pairs")

    def _analyze(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    def _pair(self, result, module):
        return next((f for f in result["findings"] if f["module"] == module), None)

    # --- both detection methods --------------------------------------------

    def test_discovers_explicit_py_twin_and_accelerator_import(self):
        result = self._analyze(
            {
                # Explicit _py* twin pair: _pydecimal.py <-> Modules/_decimal/.
                "Lib/_pydecimal.py": "def Decimal(x):\n    return x\n",
                "Modules/_decimal/_decimal.c": _c_source("_decimal"),
                # Accelerator-import pair: heapq.py imports the _heapq C module.
                "Lib/heapq.py": (
                    "def heappush(heap, item):\n"
                    "    heap.append(item)\n"
                    "try:\n"
                    "    from _heapq import *\n"
                    "except ImportError:\n"
                    "    pass\n"
                ),
                "Modules/_heapqmodule.c": _c_source("_heapq"),
            }
        )

        decimal = self._pair(result, "decimal")
        self.assertIsNotNone(decimal, "explicit _py* twin not discovered")
        self.assertEqual(decimal["detection"], "explicit_py_twin")
        self.assertEqual(decimal["python_impl"], "Lib/_pydecimal.py")
        self.assertEqual(decimal["python_twin_module"], "_pydecimal")
        self.assertEqual(decimal["c_module"], "_decimal")
        self.assertIn("Modules/_decimal/_decimal.c", decimal["c_sources"])
        self.assertEqual(decimal["confidence"], "high")

        heapq = self._pair(result, "heapq")
        self.assertIsNotNone(heapq, "accelerator-import pair not discovered")
        self.assertEqual(heapq["detection"], "accelerator_import")
        self.assertEqual(heapq["python_impl"], "Lib/heapq.py")
        self.assertIsNone(heapq["python_twin_module"])
        self.assertEqual(heapq["c_module"], "_heapq")
        self.assertIn("Modules/_heapqmodule.c", heapq["c_sources"])
        self.assertEqual(heapq["import_style"], "star")

    # --- merging: a pair found by BOTH methods is reported once ------------

    def test_twin_plus_accelerator_import_merges_to_both(self):
        result = self._analyze(
            {
                "Lib/_pydatetime.py": "class datetime:\n    def replace(self):\n        pass\n",
                "Lib/datetime.py": (
                    "try:\n"
                    "    from _datetime import *\n"
                    "except ImportError:\n"
                    "    from _pydatetime import *\n"
                ),
                "Modules/_datetimemodule.c": _c_source("_datetime"),
            }
        )
        datetime_pairs = [f for f in result["findings"] if f["module"] == "datetime"]
        self.assertEqual(len(datetime_pairs), 1, "pair should be merged, not doubled")
        pair = datetime_pairs[0]
        self.assertEqual(pair["detection"], "both")
        self.assertEqual(pair["python_impl"], "Lib/_pydatetime.py")
        self.assertEqual(pair["import_style"], "star")
        self.assertEqual(pair["python_dispatcher"], "Lib/datetime.py")
        self.assertEqual(pair["confidence"], "high")

    # --- the Lib/<pkg>/_<pkg>.py discovery rule (recovers zoneinfo) --------

    def test_package_twin_is_discovered_at_high_confidence(self):
        """`Lib/zoneinfo/_zoneinfo.py` is a twin; the `Lib/_py*` rule misses it.

        Missing this rule is measured, not hypothetical: the scanner reported
        `python_twin_module: null, confidence: low` for `zoneinfo`, and CPY-0033
        exists only because the twin was identified by hand afterwards.
        """
        result = self._analyze(
            {
                "Lib/zoneinfo/__init__.py": (
                    "try:\n"
                    "    from _zoneinfo import ZoneInfo\n"
                    "except ImportError:\n"
                    "    from ._zoneinfo import ZoneInfo\n"
                ),
                "Lib/zoneinfo/_zoneinfo.py": (
                    "class ZoneInfo:\n"
                    "    def utcoffset(self, dt):\n"
                    "        return None\n"
                    "    def tzname(self, dt):\n"
                    "        return None\n"
                ),
                "Modules/_zoneinfo.c": _c_source("_zoneinfo"),
            }
        )
        pair = self._pair(result, "zoneinfo")
        self.assertIsNotNone(pair, "Lib/<pkg>/_<pkg>.py twin not discovered")
        self.assertEqual(pair["confidence"], "high")
        self.assertEqual(pair["python_twin_module"], "zoneinfo._zoneinfo")
        self.assertEqual(pair["python_impl"], "Lib/zoneinfo/_zoneinfo.py")
        self.assertIn("Modules/_zoneinfo.c", pair["c_sources"])
        # Merged with the accelerator import found in __init__.py.
        self.assertEqual(pair["detection"], "both")
        # And it must be importable side by side with the C one.
        self.assertEqual(pair["force_python_hint"], "import zoneinfo._zoneinfo as m")
        self.assertEqual(pair["force_c_hint"], "import zoneinfo as m")

    def test_package_twin_probes_go_through_a_method_not_the_class(self):
        """`type(SomeClass)` is `type` on both sides -- probe a method."""
        result = self._analyze(
            {
                "Lib/zoneinfo/__init__.py": (
                    "try:\n"
                    "    from _zoneinfo import ZoneInfo\n"
                    "except ImportError:\n"
                    "    from ._zoneinfo import ZoneInfo\n"
                ),
                "Lib/zoneinfo/_zoneinfo.py": (
                    "class ZoneInfo:\n"
                    "    def utcoffset(self, dt):\n"
                    "        return None\n"
                ),
                "Modules/_zoneinfo.c": _c_source("_zoneinfo"),
            }
        )
        probes = self._pair(result, "zoneinfo")["backend_assertion"]["probes"]
        self.assertIn("ZoneInfo.utcoffset", probes)
        self.assertNotIn("ZoneInfo", probes)

    # --- the `long` false pair ---------------------------------------------

    def test_pylong_is_rejected_because_there_is_no_long_c_module(self):
        """`_pylong` is an algorithm helper, not an API twin: `import _long` fails.

        This was a *high*-confidence false pair -- the worst kind, because
        `high` is the tier an agent is told to start with.
        """
        result = self._analyze(
            {
                "Lib/_pylong.py": "def int_to_decimal_string(n):\n    return str(n)\n",
                # Objects/longobject.c is the last-resort fallback the old
                # heuristic latched onto. It defines no module.
                "Objects/longobject.c": '#include "Python.h"\nint x;\n',
            }
        )
        self.assertIsNone(self._pair(result, "long"))
        rejected = result["rejected_pairs"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["module"], "long")
        self.assertEqual(rejected[0]["c_module"], "_long")
        self.assertIn("_long", rejected[0]["reason"])
        self.assertEqual(result["summary"]["rejected_pairs"], 1)

    def test_c_module_verified_via_module_name_literal(self):
        """`_warnings` is a builtin: no PyInit_, but it names itself."""
        result = self._analyze(
            {
                "Lib/_py_warnings.py": "def warn(msg):\n    pass\n",
                "Lib/warnings.py": (
                    "try:\n"
                    "    from _warnings import warn\n"
                    "except ImportError:\n"
                    "    from _py_warnings import warn\n"
                ),
                "Python/_warnings.c": (
                    '#include "Python.h"\n'
                    '#define MODULE_NAME "_warnings"\n'
                    "static struct PyModuleDef warningsmodule = {MODULE_NAME};\n"
                ),
            }
        )
        pair = self._pair(result, "warnings")
        self.assertIsNotNone(pair)
        self.assertTrue(pair["c_module_verified"])
        self.assertIn("module-name literal", pair["c_module_evidence"])

    # --- confidence ladder --------------------------------------------------

    def test_dual_binding_promotes_a_named_import_off_low(self):
        """`py_make_scanner`/`c_make_scanner` is a real side-by-side dual path."""
        result = self._analyze(
            {
                "Lib/json/__init__.py": "from .scanner import make_scanner\n",
                "Lib/json/scanner.py": (
                    "try:\n"
                    "    from _json import make_scanner as c_make_scanner\n"
                    "except ImportError:\n"
                    "    c_make_scanner = None\n"
                    "\n"
                    "def py_make_scanner(context):\n"
                    "    return context\n"
                    "\n"
                    "make_scanner = c_make_scanner or py_make_scanner\n"
                ),
                "Modules/_json.c": _c_source("_json"),
            }
        )
        pair = self._pair(result, "json")
        self.assertIsNotNone(pair)
        self.assertEqual(pair["import_style"], "named")
        self.assertIn("make_scanner", pair["dual_bindings"])
        self.assertEqual(pair["confidence"], "medium")
        self.assertIn("scanner.make_scanner", pair["backend_assertion"]["probes"])

    def test_named_import_without_a_dual_binding_stays_low(self):
        result = self._analyze(
            {
                "Lib/csv.py": (
                    "from _csv import Error, writer, reader\n"
                    "\n"
                    "class DictReader:\n"
                    "    def fieldnames(self):\n"
                    "        return []\n"
                ),
                "Modules/_csv.c": _c_source("_csv"),
            }
        )
        csv = self._pair(result, "csv")
        self.assertIsNotNone(csv)
        self.assertEqual(csv["detection"], "accelerator_import")
        self.assertEqual(csv["import_style"], "named")
        self.assertEqual(csv["confidence"], "low")

    def test_unconditional_accelerator_import_is_not_differentiable(self):
        """`from _csv import ...` at column 0 means there is no fallback at all.

        Blocking the accelerator breaks `import csv` outright, so the "twin"
        cannot be loaded and a differential is impossible. Saying so beats
        ranking the pair above ones that can actually be driven.
        """
        result = self._analyze(
            {
                "Lib/struct.py": "from _struct import *\n",
                "Modules/_struct.c": _c_source("_struct"),
            }
        )
        pair = self._pair(result, "struct")
        self.assertIsNotNone(pair)
        self.assertFalse(pair["accelerator_import_guarded"])
        self.assertFalse(pair["differentiable"])
        self.assertEqual(pair["confidence"], "low")
        self.assertIn("no pure-Python fallback", pair["notes"])

    # --- backend assertion --------------------------------------------------

    def test_backend_assertion_warns_against_dunder_module(self):
        """`datetime.datetime.__module__` is 'datetime' for BOTH backends."""
        result = self._analyze(
            {
                "Lib/_pydatetime.py": "class datetime:\n    def replace(self):\n        pass\n",
                "Lib/datetime.py": (
                    "try:\n"
                    "    from _datetime import *\n"
                    "except ImportError:\n"
                    "    from _pydatetime import *\n"
                ),
                "Modules/_datetimemodule.c": _c_source("_datetime"),
            }
        )
        assertion = self._pair(result, "datetime")["backend_assertion"]
        self.assertIn("__module__", assertion["trap"])
        self.assertIn("method_descriptor", assertion["method"])
        self.assertIn("datetime.replace", assertion["probes"])

    # --- true negatives ----------------------------------------------------

    def test_pure_python_module_without_c_counterpart_not_reported(self):
        result = self._analyze(
            {
                "Lib/foo.py": "def foo():\n    return 42\n",
                # A pure-Python helper that imports an UNRELATED C module must
                # not turn foo into a parity pair.
                "Lib/bar.py": "from _thread import RLock\n",
            }
        )
        self.assertIsNone(self._pair(result, "foo"))
        self.assertIsNone(self._pair(result, "bar"))
        self.assertIsNone(self._pair(result, "thread"))
        self.assertEqual(result["findings"], [])

    # --- CPython-specific edge: package-level accelerator import -----------

    def test_package_submodule_accelerator_import_is_discovered(self):
        # json's accelerator import lives in a submodule (json/scanner.py), not
        # in json/__init__.py -- the package must still be discovered.
        result = self._analyze(
            {
                "Lib/json/__init__.py": "from .scanner import make_scanner\n",
                "Lib/json/scanner.py": "from _json import make_scanner as c\n",
                "Modules/_json.c": _c_source("_json"),
            }
        )
        json_pair = self._pair(result, "json")
        self.assertIsNotNone(json_pair)
        self.assertEqual(json_pair["detection"], "accelerator_import")
        self.assertEqual(json_pair["python_impl"], "Lib/json/__init__.py")
        self.assertIn("Lib/json/scanner.py", json_pair["import_sites"])
        self.assertIn("Modules/_json.c", json_pair["c_sources"])

    # --- parse fallback: the target tree is newer than this interpreter ----

    def test_structure_falls_back_to_regex_on_unparseable_source(self):
        """The venv is 3.12 while CPython main is 3.16; ast.parse *will* fail.

        Treating an unparseable stdlib module as structureless silently drops
        its dual bindings (that is how `collections`/OrderedDict was lost).
        """
        with TempProject(
            {
                "Lib/collections/__init__.py": (
                    "from __future__ import some_future_syntax\n"
                    "def f(x: int) ->> int: ...\n"  # deliberate syntax error
                    "class OrderedDict(dict):\n"
                    "    def popitem(self, last=True):\n"
                    "        pass\n"
                    "try:\n"
                    "    from _collections import OrderedDict\n"
                    "except ImportError:\n"
                    "    pass\n"
                ),
                "Modules/_collectionsmodule.c": _c_source("_collections"),
            }
        ) as root:
            path = root / "Lib" / "collections" / "__init__.py"
            with self.assertRaises(SyntaxError):
                ast.parse(path.read_text())
            struct = self.mod.module_structure(path, "_collections")
            self.assertEqual(struct["parsed_with"], "regex")
            self.assertIn("OrderedDict", struct["classes"])
            self.assertIn("popitem", struct["classes"]["OrderedDict"])
            self.assertIn("OrderedDict", struct["imported"]["_collections"])

            result = self.mod.analyze(str(root))
            pair = self._pair(result, "collections")
            self.assertIn("OrderedDict", pair["dual_bindings"])
            self.assertEqual(pair["confidence"], "medium")
            self.assertEqual(result["parse_health"].get("regex"), 1)

    # --- harness emission ---------------------------------------------------

    def _datetime_pair(self):
        result = self._analyze(
            {
                "Lib/_pydatetime.py": (
                    "class date:\n    def ctime(self):\n        return ''\n"
                ),
                "Lib/datetime.py": (
                    "try:\n"
                    "    from _datetime import *\n"
                    "except ImportError:\n"
                    "    from _pydatetime import *\n"
                ),
                "Modules/_datetimemodule.c": _c_source("_datetime"),
            }
        )
        return self._pair(result, "datetime")

    def test_emitted_harness_is_valid_python_and_self_contained(self):
        harness = self.mod.emit_harness(self._datetime_pair())
        ast.parse(harness)  # must not raise
        self.assertIn("subprocess", harness)
        self.assertIn("import _pydatetime as m", harness)
        self.assertIn("import datetime as m", harness)
        self.assertIn("date.ctime", harness)

    def test_emitted_harness_runs_both_sides_in_subprocesses(self):
        harness = self.mod.emit_harness(self._datetime_pair())
        # Separate subprocesses are the whole point: a SIGSEGV cannot be caught.
        self.assertIn("subprocess.run", harness)
        self.assertIn("SIGSEGV", harness)
        self.assertIn("SIGABRT", harness)
        self.assertIn("timeout", harness)

    def test_emitted_harness_asserts_the_backend_and_refuses_when_it_cannot(self):
        harness = self.mod.emit_harness(self._datetime_pair())
        self.assertIn("PARITY-BACKEND-ASSERTION-FAILED", harness)
        self.assertIn("method_descriptor", harness)
        self.assertIn("NO PROBE DISTINGUISHES THE BACKENDS", harness)
        # And it must not tell anyone to trust __module__.
        self.assertIn("Do NOT use __module__", harness)

    def test_emitted_harness_compares_exit_codes_and_exception_types(self):
        harness = self.mod.emit_harness(self._datetime_pair())
        self.assertIn("PARITY-EXC:", harness)
        self.assertIn("different exception", harness)
        self.assertIn("returncode", harness)

    def test_emit_harness_filename(self):
        self.assertEqual(
            self.mod.harness_filename("zoneinfo"), "parity_harness_zoneinfo.py"
        )

    # --- envelope shape ----------------------------------------------------

    def test_envelope_shape(self):
        result = self._analyze({"Lib/foo.py": "x = 1\n"})
        for key in (
            "project_root",
            "scan_root",
            "files_analyzed",
            "functions_analyzed",
            "findings",
            "summary",
            "rejected_pairs",
            "parse_health",
        ):
            self.assertIn(key, result)
        for key in (
            "total_pairs",
            "by_confidence",
            "by_detection",
            "rejected_pairs",
            "differentiable_pairs",
        ):
            self.assertIn(key, result["summary"])


if __name__ == "__main__":
    unittest.main()
