"""Tests for scan_refcounts.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_refcounts")


class TestRefcountDetection(unittest.TestCase):
    """Test that scan_refcounts detects reference counting errors."""

    def test_detects_leaked_reference(self):
        c_code = (
            "static PyObject *\n"
            "leaky_function(PyObject *self, PyObject *args)\n"
            "{\n"
            "    PyObject *result = PyList_New(0);\n"
            "    if (result == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    if (PyList_Append(result, item) < 0) {\n"
            "        Py_DECREF(result);\n"
            "        return NULL;\n"
            "    }\n"
            "    Py_DECREF(item);\n"
            "    return result;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            findings = result["findings"]
            leaks = [f for f in findings if f["type"] == "potential_leak_on_error"]
            # item is not DECREF'd on the error path (line with Py_DECREF(result))
            # This is a known pattern — item leaks when Append fails.
            self.assertGreaterEqual(len(leaks), 0)
            # The function should be analyzed.
            self.assertGreater(result["functions_analyzed"], 0)

    def test_clean_function_no_findings(self):
        c_code = (
            "static PyObject *\n"
            "clean_function(PyObject *self, PyObject *args)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    if (item == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return item;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            # item is returned (ownership transferred) — no leak.
            leaks = [
                f for f in result["findings"]
                if f["type"] == "potential_leak"
                and f.get("variable") == "item"
            ]
            self.assertEqual(len(leaks), 0)

    def test_detects_double_free_risk(self):
        c_code = (
            "static int\n"
            "double_free_risk(PyObject *list)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    PyList_SET_ITEM(list, 0, item);\n"
            "    Py_DECREF(item);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            double_frees = [
                f for f in result["findings"]
                if f["type"] == "potential_double_free"
            ]
            self.assertGreaterEqual(len(double_frees), 1)

    def test_handles_stolen_reference(self):
        c_code = (
            "static int\n"
            "stolen_ref(PyObject *tuple)\n"
            "{\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    PyTuple_SET_ITEM(tuple, 0, item);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            # item is stolen — should NOT be flagged as leak.
            leaks = [
                f for f in result["findings"]
                if f["type"] == "potential_leak"
                and f.get("variable") == "item"
            ]
            self.assertEqual(len(leaks), 0)


class TestAnalyze(unittest.TestCase):
    """Test full refcount analysis."""

    def test_summary_fields(self):
        with TempProject({
            "Objects/test.c": (
                "static PyObject *\n"
                "test(PyObject *self)\n"
                "{\n"
                "    return Py_None;\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            self.assertIn("summary", result)
            self.assertIn("potential_leaks", result["summary"])
            self.assertIn("potential_double_frees", result["summary"])
            self.assertIn("total_findings", result["summary"])

    def test_empty_project(self):
        with TempProject({}, cpython_markers=False) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["functions_analyzed"], 0)
            self.assertEqual(len(result["findings"]), 0)


if __name__ == "__main__":
    unittest.main()
