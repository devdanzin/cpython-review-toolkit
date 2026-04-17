"""Tests for scan_gil_usage.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_gil_usage")


class TestGilDetection(unittest.TestCase):
    """Test GIL discipline issue detection."""

    def test_detects_mismatched_threads(self):
        c_code = (
            "static int\n"
            "bad_threads(int fd)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    read(fd, buf, n);\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            # Envelope sanity: silent-failure guard.
            self.assertGreater(result["files_analyzed"], 0)
            self.assertGreater(result["functions_analyzed"], 0)
            mismatched = [
                f for f in result["findings"]
                if f["type"] == "mismatched_allow_threads"
            ]
            self.assertGreater(len(mismatched), 0)

    def test_safety_annotation_downgrades_finding(self):
        c_code = (
            "static int\n"
            "api_no_gil(PyObject *self)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    /* safety: gil-held by callee via internal mutex */\n"
            "    PyObject_CallMethod(self, \"method\", NULL);\n"
            "    Py_END_ALLOW_THREADS\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            api_findings = [
                f for f in result["findings"]
                if f["type"] == "api_without_gil"
            ]
            for f in api_findings:
                self.assertEqual(f.get("confidence"), "low")
                self.assertTrue(f.get("suppressed_by_annotation"))

    def test_balanced_threads_no_finding(self):
        c_code = (
            "static int\n"
            "good_threads(int fd)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    read(fd, buf, n);\n"
            "    Py_END_ALLOW_THREADS\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            mismatched = [
                f for f in result["findings"]
                if f["type"] == "mismatched_allow_threads"
            ]
            self.assertEqual(len(mismatched), 0)

    def test_detects_api_without_gil(self):
        c_code = (
            "static int\n"
            "api_no_gil(PyObject *self)\n"
            "{\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    PyObject_CallMethod(self, \"method\", NULL);\n"
            "    Py_END_ALLOW_THREADS\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            api_findings = [
                f for f in result["findings"]
                if f["type"] == "api_without_gil"
            ]
            self.assertGreater(len(api_findings), 0)

    def test_detects_blocking_with_gil(self):
        c_code = (
            "static int\n"
            "blocking_gil(int fd)\n"
            "{\n"
            "    char buf[1024];\n"
            "    read(fd, buf, sizeof(buf));\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            blocking = [
                f for f in result["findings"]
                if f["type"] == "blocking_with_gil"
            ]
            self.assertGreater(len(blocking), 0)

    def test_blocking_in_released_region_no_finding(self):
        c_code = (
            "static int\n"
            "good_blocking(int fd)\n"
            "{\n"
            "    char buf[1024];\n"
            "    Py_BEGIN_ALLOW_THREADS\n"
            "    read(fd, buf, sizeof(buf));\n"
            "    Py_END_ALLOW_THREADS\n"
            "    return 0;\n"
            "}\n"
        )
        with TempProject({"Modules/test.c": c_code}) as root:
            result = mod.analyze(str(root))
            blocking = [
                f for f in result["findings"]
                if f["type"] == "blocking_with_gil"
            ]
            self.assertEqual(len(blocking), 0)


class TestAnalyze(unittest.TestCase):
    """Test full GIL analysis."""

    def test_summary_fields(self):
        with TempProject({
            "Modules/test.c": (
                "static int\n"
                "test(void)\n"
                "{\n"
                "    return 0;\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            self.assertIn("summary", result)
            self.assertIn("mismatched_pairs", result["summary"])
            self.assertIn("api_without_gil", result["summary"])
            self.assertIn("blocking_with_gil", result["summary"])


if __name__ == "__main__":
    unittest.main()
