"""Tests for find_parity_pairs.py -- C-accelerator / pure-Python-twin discovery."""

import unittest

from helpers import TempProject, import_script


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
                "Modules/_decimal/_decimal.c": '#include "Python.h"\n',
                # Accelerator-import pair: heapq.py imports the _heapq C module.
                "Lib/heapq.py": (
                    "def heappush(heap, item):\n"
                    "    heap.append(item)\n"
                    "try:\n"
                    "    from _heapq import *\n"
                    "except ImportError:\n"
                    "    pass\n"
                ),
                "Modules/_heapqmodule.c": '#include "Python.h"\n',
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
                "Lib/_pydatetime.py": "def datetime():\n    pass\n",
                "Lib/datetime.py": (
                    "try:\n"
                    "    from _datetime import *\n"
                    "except ImportError:\n"
                    "    from _pydatetime import *\n"
                ),
                "Modules/_datetimemodule.c": '#include "Python.h"\n',
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

    def test_named_import_only_is_low_confidence(self):
        # csv.py selectively imports primitives from _csv (partial acceleration),
        # not a full `import *` replacement -> lower confidence than a twin.
        result = self._analyze(
            {
                "Lib/csv.py": "from _csv import Error, writer, reader\n",
                "Modules/_csv.c": '#include "Python.h"\n',
            }
        )
        csv = self._pair(result, "csv")
        self.assertIsNotNone(csv)
        self.assertEqual(csv["detection"], "accelerator_import")
        self.assertEqual(csv["import_style"], "named")
        self.assertEqual(csv["confidence"], "low")

    # --- CPython-specific edge: package-level accelerator import -----------

    def test_package_submodule_accelerator_import_is_discovered(self):
        # json's accelerator import lives in a submodule (json/scanner.py), not
        # in json/__init__.py -- the package must still be discovered.
        result = self._analyze(
            {
                "Lib/json/__init__.py": "from .scanner import make_scanner\n",
                "Lib/json/scanner.py": "from _json import make_scanner as c\n",
                "Modules/_json.c": '#include "Python.h"\n',
            }
        )
        json_pair = self._pair(result, "json")
        self.assertIsNotNone(json_pair)
        self.assertEqual(json_pair["detection"], "accelerator_import")
        self.assertEqual(json_pair["python_impl"], "Lib/json/__init__.py")
        self.assertIn("Lib/json/scanner.py", json_pair["import_sites"])
        self.assertIn("Modules/_json.c", json_pair["c_sources"])

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
        ):
            self.assertIn(key, result)
        for key in ("total_pairs", "by_confidence", "by_detection"):
            self.assertIn(key, result["summary"])


if __name__ == "__main__":
    unittest.main()
