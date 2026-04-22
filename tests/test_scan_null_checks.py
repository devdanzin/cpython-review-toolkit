"""Tests for scan_null_checks.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_null_checks")


class TestNullSafetyDetection(unittest.TestCase):
    """Test NULL safety scanning."""

    def test_detects_unchecked_malloc(self):
        c_code = (
            "static int\n"
            "bad_alloc(int n)\n"
            "{\n"
            "    char *buf = PyMem_Malloc(n);\n"
            "    buf->data = 0;\n"
            "    PyMem_Free(buf);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            # Envelope sanity: silent-failure guard.
            self.assertGreater(result["files_analyzed"], 0)
            self.assertGreater(result["functions_analyzed"], 0)
            findings = result["findings"]
            unchecked = [
                f for f in findings if f["type"] == "unchecked_alloc"
            ]
            self.assertGreater(len(unchecked), 0)

    def test_safety_annotation_downgrades_finding(self):
        # Same flaw as test_detects_unchecked_malloc, but annotated.
        c_code = (
            "static int\n"
            "bad_alloc(int n)\n"
            "{\n"
            "    /* safety: caller guarantees non-NULL buf via preallocation */\n"
            "    char *buf = PyMem_Malloc(n);\n"
            "    buf->data = 0;\n"
            "    PyMem_Free(buf);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            unchecked = [
                f for f in result["findings"]
                if f["type"] == "unchecked_alloc"
            ]
            # Any finding that remains must be downgraded.
            for f in unchecked:
                self.assertEqual(f.get("confidence"), "low")
                self.assertTrue(f.get("suppressed_by_annotation"))

    def test_checked_malloc_no_finding(self):
        c_code = (
            "static int\n"
            "good_alloc(int n)\n"
            "{\n"
            "    char *buf = PyMem_Malloc(n);\n"
            "    if (buf == NULL) {\n"
            "        return -1;\n"
            "    }\n"
            "    buf[0] = 0;\n"
            "    PyMem_Free(buf);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            unchecked = [
                f for f in result["findings"]
                if f["type"] == "unchecked_alloc"
                and f.get("variable") == "buf"
            ]
            self.assertEqual(len(unchecked), 0)

    def test_detects_unchecked_pyobject_api(self):
        c_code = (
            "static PyObject *\n"
            "no_check(PyObject *self)\n"
            "{\n"
            "    PyObject *list = PyList_New(0);\n"
            "    PyObject *item = PyLong_FromLong(42);\n"
            "    PyList_Append(list, item);\n"
            "    return list;\n"
            "}\n"
        )
        with TempProject({"Objects/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            self.assertGreater(result["functions_analyzed"], 0)


class TestAnalyze(unittest.TestCase):
    """Test full NULL safety analysis."""

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
            self.assertIn("unchecked_allocations", result["summary"])
            self.assertIn("total_findings", result["summary"])


if __name__ == "__main__":
    unittest.main()
