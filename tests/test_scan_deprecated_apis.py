"""Tests for scan_deprecated_apis.py."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import TempProject, import_script

mod = import_script("scan_deprecated_apis")

_DATA = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "cpython-review-toolkit"
    / "data"
    / "deprecated_c_apis.json"
)


class TestDataFile(unittest.TestCase):
    """The vocabulary must load and be well-formed."""

    def test_loads(self):
        apis = mod.load_deprecated_apis()
        self.assertGreater(len(apis), 30)

    def test_every_entry_has_required_keys(self):
        for api in mod.load_deprecated_apis():
            for key in ("name", "tier", "deprecated_in", "removed_in",
                        "replacement", "notes"):
                self.assertIn(key, api, msg=api.get("name"))
            self.assertIn(api["tier"], ("hard", "hard-internal", "soft"))

    def test_no_duplicate_names(self):
        names = [a["name"] for a in mod.load_deprecated_apis()]
        self.assertEqual(len(names), len(set(names)))

    def test_apis_that_are_only_discouraged_are_excluded(self):
        """TK-20 correction: `PyDict_GetItem` is not actually deprecated."""
        names = {a["name"] for a in mod.load_deprecated_apis()}
        for not_deprecated in (
            "PyDict_GetItem", "PyMapping_HasKey", "PyMapping_HasKeyString",
            "PyOS_snprintf",
        ):
            self.assertNotIn(not_deprecated, names)

    def test_removed_apis_are_excluded(self):
        """Symbols already removed cannot be called; scanning for them is noise."""
        names = {a["name"] for a in mod.load_deprecated_apis()}
        for removed in (
            "PyWeakref_GetObject", "PyEval_CallObject", "PyUnicode_GetSize",
            "PyCFunction_Call", "Py_TRASHCAN_SAFE_BEGIN",
            "PyObject_AsCharBuffer",
        ):
            self.assertNotIn(removed, names)

    def test_documents_its_exclusions(self):
        with open(_DATA, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("deliberately_excluded", data["_meta"])
        self.assertIn("PyDict_GetItem", data["_meta"]["deliberately_excluded"])


class TestWordBoundaries(unittest.TestCase):
    """TK-20: no more substring false positives."""

    def test_substring_is_not_a_match(self):
        with TempProject({
            "Objects/x.c": (
                "void\n"
                "f(void)\n"
                "{\n"
                "    PyEval_GetBuiltinsExtra(a);\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            apis = {f.get("api") for f in result["findings"]}
            self.assertNotIn("PyEval_GetBuiltins", apis)

    def test_exact_name_is_a_match(self):
        with TempProject({
            "Objects/x.c": (
                "void\n"
                "f(void)\n"
                "{\n"
                "    PyObject *b = PyEval_GetBuiltins();\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            hits = [f for f in result["findings"]
                    if f.get("api") == "PyEval_GetBuiltins"]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["line"], 4)
            self.assertEqual(hits[0]["replacement"], "PyEval_GetFrameBuiltins")


class TestDefinitionSiteSuppression(unittest.TestCase):
    """TK-20: every 2021-era hit was the API's own definition."""

    def test_definition_body_is_not_a_use(self):
        with TempProject({
            "Objects/x.c": (
                "PyObject *\n"
                "PyEval_GetBuiltins(void)\n"
                "{\n"
                "    /* PyEval_GetBuiltins implementation */\n"
                "    return do_it();\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["findings"], [])

    def test_declaration_is_not_a_use(self):
        with TempProject({
            "Objects/x.h": (
                "PyAPI_FUNC(PyObject *) PyEval_GetBuiltins(void);\n"
                "#define PyErr_Restore(a, b, c) legacy(a, b, c)\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["findings"], [])

    def test_forward_declaration_is_not_a_use(self):
        with TempProject({
            "Objects/x.c": "static void PyErr_Restore(a, b, c);\n",
        }) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["findings"], [])

    def test_statement_form_call_is_still_a_use(self):
        """A call statement has the same tail as a forward declaration."""
        with TempProject({
            "Objects/x.c": (
                "void\n"
                "f(void)\n"
                "{\n"
                "    PyErr_Restore(t, v, tb);\n"
                "}\n"
            ),
        }) as root:
            result = mod.analyze(str(root))
            hits = [f for f in result["findings"] if f.get("api") == "PyErr_Restore"]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["line"], 4)


class TestCommentAndStringSuppression(unittest.TestCase):
    """TK-20: comment prose and Clinic docstrings are not usage."""

    def test_comment_prose_is_not_a_use(self):
        with TempProject({
            "Objects/x.c": (
                "void\n"
                "f(void)\n"
                "{\n"
                "    /* Formerly used PyErr_Restore here. */\n"
                "    return;\n"
                "}\n"
            ),
        }) as root:
            self.assertEqual(mod.analyze(str(root))["findings"], [])

    def test_docstring_text_is_not_a_use(self):
        with TempProject({
            "Objects/x.c": (
                'PyDoc_STRVAR(doc, "takes a Py_UNICODE buffer");\n'
            ),
        }) as root:
            self.assertEqual(mod.analyze(str(root))["findings"], [])

    def test_line_numbers_survive_a_multiline_comment(self):
        with TempProject({
            "Objects/x.c": (
                "/* a long\n"
                "   banner\n"
                "   comment */\n"
                "void\n"
                "f(void)\n"
                "{\n"
                "    PyErr_Restore(t, v, tb);\n"
                "}\n"
            ),
        }) as root:
            findings = mod.analyze(str(root))["findings"]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["line"], 7)


class TestSeverity(unittest.TestCase):
    """removed_in set -> FIX; otherwise CONSIDER."""

    def test_scheduled_removal_is_fix(self):
        with TempProject({
            "Objects/x.c": (
                "void\n"
                "f(void)\n"
                "{\n"
                "    _PyUnicodeWriter_Init(&writer);\n"
                "}\n"
            ),
        }) as root:
            findings = mod.analyze(str(root))["findings"]
            self.assertEqual(findings[0]["severity"], "FIX")
            self.assertEqual(findings[0]["tier"], "hard-internal")
            self.assertIn("Py_BUILD_CORE", findings[0]["detail"])

    def test_unscheduled_deprecation_is_consider(self):
        with TempProject({
            "Objects/x.c": (
                "void\n"
                "f(void)\n"
                "{\n"
                "    PyModule_AddObject(m, \"n\", o);\n"
                "}\n"
            ),
        }) as root:
            findings = mod.analyze(str(root))["findings"]
            self.assertEqual(findings[0]["severity"], "CONSIDER")


class TestGcUntrackMacroForm(unittest.TestCase):
    """The `_PyObject_GC_UNTRACK` vs `PyObject_GC_UnTrack` safety rule."""

    _BUGGY = (
        "static PyObject *\n"
        "myiter_new(PyObject *od, int kind)\n"
        "{\n"
        "    myiterobject *di;\n"
        "    di = PyObject_GC_New(myiterobject, &MyIter_Type);\n"
        "    if (di == NULL)\n"
        "        return NULL;\n"
        "    di->di_result = make_pair();\n"
        "    if (di->di_result == NULL) {\n"
        "        Py_DECREF(di);\n"
        "        return NULL;\n"
        "    }\n"
        "    _PyObject_GC_TRACK(di);\n"
        "    return (PyObject *)di;\n"
        "}\n"
        "\n"
        "static void\n"
        "myiter_dealloc(PyObject *op)\n"
        "{\n"
        "    myiterobject *di = (myiterobject *)op;\n"
        "    _PyObject_GC_UNTRACK(di);\n"
        "    Py_XDECREF(di->di_result);\n"
        "    PyObject_GC_Del(di);\n"
        "}\n"
    )

    def test_pretrack_free_path_is_flagged(self):
        with TempProject({"Objects/x.c": self._BUGGY}) as root:
            findings = [
                f for f in mod.analyze(str(root))["findings"]
                if f["type"] == "gc-untrack-macro-form"
            ]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["severity"], "FIX")
            self.assertEqual(findings[0]["function"], "myiter_dealloc")
            self.assertEqual(findings[0]["line"], 21)
            self.assertEqual(findings[0]["replacement"], "PyObject_GC_UnTrack")

    def test_detail_does_not_call_it_null_safe(self):
        """PyObject_GC_UnTrack is untracked-tolerant, NOT NULL-safe."""
        with TempProject({"Objects/x.c": self._BUGGY}) as root:
            findings = [
                f for f in mod.analyze(str(root))["findings"]
                if f["type"] == "gc-untrack-macro-form"
            ]
            detail = findings[0]["detail"]
            self.assertNotIn("NULL-safe", detail)
            self.assertIn("dereferences", detail)

    def test_no_pretrack_free_path_is_clean(self):
        """The macro is correct when the object is tracked before any free."""
        source = (
            "static PyObject *\n"
            "myiter_new(PyObject *od, int kind)\n"
            "{\n"
            "    myiterobject *di;\n"
            "    di = PyObject_GC_New(myiterobject, &MyIter_Type);\n"
            "    if (di == NULL)\n"
            "        return NULL;\n"
            "    di->di_result = NULL;\n"
            "    _PyObject_GC_TRACK(di);\n"
            "    return (PyObject *)di;\n"
            "}\n"
            "\n"
            "static void\n"
            "myiter_dealloc(PyObject *op)\n"
            "{\n"
            "    myiterobject *di = (myiterobject *)op;\n"
            "    _PyObject_GC_UNTRACK(di);\n"
            "    PyObject_GC_Del(di);\n"
            "}\n"
        )
        with TempProject({"Objects/x.c": source}) as root:
            findings = [
                f for f in mod.analyze(str(root))["findings"]
                if f["type"] == "gc-untrack-macro-form"
            ]
            self.assertEqual(findings, [])

    def test_tolerant_form_is_clean(self):
        """Using the public function on the same shape is the correct fix."""
        source = self._BUGGY.replace(
            "_PyObject_GC_UNTRACK(di);", "PyObject_GC_UnTrack(di);"
        )
        with TempProject({"Objects/x.c": source}) as root:
            findings = [
                f for f in mod.analyze(str(root))["findings"]
                if f["type"] == "gc-untrack-macro-form"
            ]
            self.assertEqual(findings, [])


class TestEnvelope(unittest.TestCase):
    """Standard report envelope (design 4.2)."""

    def test_envelope_keys(self):
        with TempProject({"Objects/x.c": "int x;\n"}) as root:
            result = mod.analyze(str(root))
            for key in (
                "project_root", "scan_root", "files_analyzed",
                "functions_analyzed", "findings", "summary",
            ):
                self.assertIn(key, result)
            for key in ("total_findings", "by_api", "by_tier", "by_severity"):
                self.assertIn(key, result["summary"])

    def test_max_files_is_honoured(self):
        with TempProject({
            "Objects/a.c": "int a;\n",
            "Objects/b.c": "int b;\n",
            "Objects/c.c": "int c;\n",
        }) as root:
            result = mod.analyze(str(root), max_files=1)
            self.assertEqual(result["files_analyzed"], 1)


if __name__ == "__main__":
    unittest.main()
