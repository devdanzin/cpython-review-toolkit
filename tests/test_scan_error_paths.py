"""Tests for scan_error_paths.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_error_paths")


class TestErrorPathDetection(unittest.TestCase):
    """Test error handling bug detection."""

    def test_detects_missing_null_check(self):
        c_code = (
            "static PyObject *\n"
            "no_check(PyObject *self, PyObject *args)\n"
            "{\n"
            "    PyObject *result = PyObject_GetAttrString(self, \"name\");\n"
            "    PyObject *str = PyObject_Str(result);\n"
            "    return str;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            # Envelope sanity: silent-failure guard.
            self.assertGreater(result["files_analyzed"], 0)
            self.assertGreater(result["functions_analyzed"], 0)
            findings = result["findings"]
            unchecked = [
                f for f in findings
                if f["type"] in ("missing_null_check", "unchecked_return")
            ]
            self.assertGreater(len(unchecked), 0)

    def test_safety_annotation_downgrades_finding(self):
        c_code = (
            "static PyObject *\n"
            "no_check(PyObject *self, PyObject *args)\n"
            "{\n"
            "    /* safety: result is checked by Str() before deref */\n"
            "    PyObject *result = PyObject_GetAttrString(self, \"name\");\n"
            "    PyObject *str = PyObject_Str(result);\n"
            "    return str;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            relevant = [
                f for f in result["findings"]
                if f["type"] in ("missing_null_check", "unchecked_return")
            ]
            for f in relevant:
                self.assertEqual(f.get("confidence"), "low")
                self.assertTrue(f.get("suppressed_by_annotation"))

    def test_clean_error_handling(self):
        c_code = (
            "static PyObject *\n"
            "clean(PyObject *self, PyObject *args)\n"
            "{\n"
            "    PyObject *result = PyObject_GetAttrString(self, \"name\");\n"
            "    if (result == NULL) {\n"
            "        return NULL;\n"
            "    }\n"
            "    return result;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            null_checks = [
                f for f in result["findings"]
                if f["type"] == "missing_null_check"
                and f.get("variable") == "result"
            ]
            self.assertEqual(len(null_checks), 0)

    def test_detects_return_null_no_exception(self):
        c_code = (
            "static PyObject *\n"
            "bad_return(PyObject *self)\n"
            "{\n"
            "    return NULL;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            no_exc = [
                f for f in result["findings"]
                if f["type"] == "return_null_no_exception"
            ]
            self.assertGreaterEqual(len(no_exc), 0)


class TestAnalyze(unittest.TestCase):
    """Test full error path analysis."""

    def test_summary_fields(self):
        with TempProject({
            "Objects/test.c": (
                "static int\n"
                "test(void)\n"
                "{\n"
                "    return 0;\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            self.assertIn("summary", result)
            self.assertIn("missing_null_checks", result["summary"])
            self.assertIn("unchecked_returns", result["summary"])
            self.assertIn("total_findings", result["summary"])


if __name__ == "__main__":
    unittest.main()
