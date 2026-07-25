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
            for key in (
                "name",
                "tier",
                "deprecated_in",
                "removed_in",
                "replacement",
                "drop_in",
                "caveat",
                "notes",
            ):
                self.assertIn(key, api, msg=api.get("name"))
            self.assertIn(api["tier"], ("hard", "hard-internal", "soft"))
            self.assertIsInstance(api["drop_in"], bool, msg=api["name"])

    def test_a_non_drop_in_entry_must_explain_itself(self):
        """`drop_in: false` with no caveat is worse than no field at all."""
        for api in mod.load_deprecated_apis():
            if not api["drop_in"]:
                self.assertTrue(
                    api["caveat"].strip(),
                    msg=f"{api['name']} is not a drop-in but carries no caveat",
                )

    def test_writer_prepare_macros_are_in_the_vocabulary(self):
        """The macro forms are separate names from the *Internal functions.

        A word-boundary matcher cannot match one from the other, which is
        exactly why ~25 call sites were invisible.
        """
        names = {a["name"] for a in mod.load_deprecated_apis()}
        for pair in (
            ("_PyUnicodeWriter_Prepare", "_PyUnicodeWriter_PrepareInternal"),
            ("_PyUnicodeWriter_PrepareKind", "_PyUnicodeWriter_PrepareKindInternal"),
        ):
            for name in pair:
                self.assertIn(name, names)

    def test_global_config_flags_carry_the_nearest_removal_date(self):
        by_name = {a["name"]: a for a in mod.load_deprecated_apis()}
        for name in (
            "Py_DebugFlag",
            "Py_VerboseFlag",
            "Py_IsolatedFlag",
            "Py_NoSiteFlag",
            "Py_UTF8Mode",
            "Py_FileSystemDefaultEncoding",
            "Py_LegacyWindowsStdioFlag",
        ):
            self.assertIn(name, by_name)
            self.assertEqual(by_name[name]["removed_in"], "3.16")
            self.assertEqual(by_name[name]["deprecated_in"], "3.12")
            self.assertIn("Python/initconfig.c", by_name[name]["compat_shim_files"])

    def test_polarity_inverted_flags_are_not_drop_ins(self):
        """CPython's own bridge table marks these `not` -- a rename inverts them."""
        by_name = {a["name"]: a for a in mod.load_deprecated_apis()}
        for name in (
            "Py_NoSiteFlag",
            "Py_FrozenFlag",
            "Py_IgnoreEnvironmentFlag",
            "Py_DontWriteBytecodeFlag",
            "Py_NoUserSiteDirectory",
            "Py_UnbufferedStdioFlag",
        ):
            self.assertFalse(by_name[name]["drop_in"], msg=name)
            self.assertIn("INVERTED", by_name[name]["caveat"], msg=name)

    def test_pygen_family_is_a_regression_guard(self):
        by_name = {a["name"]: a for a in mod.load_deprecated_apis()}
        for name in (
            "PyGen_New",
            "PyGen_NewWithQualName",
            "PyCoro_New",
            "PyAsyncGen_New",
        ):
            self.assertIn(name, by_name)
            self.assertEqual(by_name[name]["deprecated_in"], "3.16")
            self.assertEqual(by_name[name]["removed_in"], "3.18")

    def test_no_duplicate_names(self):
        names = [a["name"] for a in mod.load_deprecated_apis()]
        self.assertEqual(len(names), len(set(names)))

    def test_apis_that_are_only_discouraged_are_excluded(self):
        """TK-20 correction: `PyDict_GetItem` is not actually deprecated."""
        names = {a["name"] for a in mod.load_deprecated_apis()}
        for not_deprecated in (
            "PyDict_GetItem",
            "PyMapping_HasKey",
            "PyMapping_HasKeyString",
            "PyOS_snprintf",
        ):
            self.assertNotIn(not_deprecated, names)

    def test_removed_apis_are_excluded(self):
        """Symbols already removed cannot be called; scanning for them is noise."""
        names = {a["name"] for a in mod.load_deprecated_apis()}
        for removed in (
            "PyWeakref_GetObject",
            "PyEval_CallObject",
            "PyUnicode_GetSize",
            "PyCFunction_Call",
            "Py_TRASHCAN_SAFE_BEGIN",
            "PyObject_AsCharBuffer",
        ):
            self.assertNotIn(removed, names)

    def test_documents_its_exclusions(self):
        with open(_DATA, encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("deliberately_excluded", data["_meta"])
        self.assertIn("PyDict_GetItem", data["_meta"]["deliberately_excluded"])

    def test_already_removed_names_are_excluded_with_the_trap_documented(self):
        """`PyImport_ImportModuleNoBlock` is a trap, not just a dead name.

        `Modules/_testlimitedcapi/import.c:116` re-declares the removed
        prototype locally so it can call it at :123; a word-matching scanner
        that carried the name would report that as a live call site.
        """
        with open(_DATA, encoding="utf-8") as f:
            excluded = json.load(f)["_meta"]["deliberately_excluded"]
        for removed in (
            "PyImport_ImportModuleNoBlock",
            "PyWeakref_GET_OBJECT",
            "PyUnicode_AsDecodedObject",
            "PyUnicode_AsDecodedUnicode",
            "PyUnicode_AsEncodedObject",
            "PyUnicode_AsEncodedUnicode",
            "Py_GetPath",
            "Py_GetPrefix",
            "Py_GetExecPrefix",
            "Py_GetProgramName",
            "Py_GetProgramFullPath",
            "Py_GetPythonHome",
        ):
            self.assertIn(removed, excluded)
        names = {a["name"] for a in mod.load_deprecated_apis()}
        self.assertNotIn("PyImport_ImportModuleNoBlock", names)
        self.assertIn("_testlimitedcapi", excluded["PyImport_ImportModuleNoBlock"])

    def test_schema_documents_the_new_fields(self):
        with open(_DATA, encoding="utf-8") as f:
            meta = json.load(f)["_meta"]
        for field in ("drop_in", "caveat", "compat_shim_files"):
            self.assertIn(field, meta["schema"])


class TestDropInAndCaveat(unittest.TestCase):
    """A scanner that confidently recommends a regression is worse than silent."""

    def test_drop_in_and_caveat_round_trip_into_the_finding(self):
        with TempProject(
            {
                "Modules/_json.c": (
                    "static int\n"
                    "write_escaped_unicode(PyUnicodeWriter *writer, PyObject *pystr)\n"
                    "{\n"
                    "    if (_PyUnicodeWriter_WriteStr((_PyUnicodeWriter*)writer, "
                    "pystr) < 0) {\n"
                    "        return -1;\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                ),
            }
        ) as root:
            findings = [
                f
                for f in mod.analyze(str(root))["findings"]
                if f["api"] == "_PyUnicodeWriter_WriteStr"
            ]
            self.assertEqual(len(findings), 1)
            f = findings[0]
            self.assertFalse(f["drop_in"])
            # The named replacement is the one the comment warns against.
            self.assertEqual(f["replacement"], "PyUnicodeWriter_WriteStr")
            self.assertIn("PyUnicodeWriter_WriteSubstring", f["caveat"])
            self.assertIn("gh-148241", f["caveat"])
            # And the detail must carry it, not just the raw field.
            self.assertIn("NOT a drop-in", f["detail"])
            self.assertIn("PyUnicodeWriter_WriteSubstring", f["detail"])

    def test_a_true_drop_in_says_so(self):
        with TempProject(
            {
                "Objects/x.c": ("void\nf(void)\n{\n    PyMem_NEW(PyObject *, n);\n}\n"),
            }
        ) as root:
            findings = [
                f for f in mod.analyze(str(root))["findings"] if f["api"] == "PyMem_NEW"
            ]
            self.assertEqual(len(findings), 1)
            self.assertTrue(findings[0]["drop_in"])
            self.assertEqual(findings[0]["caveat"], "")
            self.assertIn("drop-in", findings[0]["detail"])

    def test_summary_counts_findings_needing_a_caveat(self):
        with TempProject(
            {
                "Objects/x.c": (
                    "void\n"
                    "f(void)\n"
                    "{\n"
                    "    PyMem_NEW(PyObject *, n);\n"
                    '    PyModule_AddObject(m, "n", o);\n'
                    "}\n"
                ),
            }
        ) as root:
            summary = mod.analyze(str(root))["summary"]
            self.assertEqual(summary["findings_needing_a_caveat"], 1)


class TestCompatShimSuppression(unittest.TestCase):
    """The deprecated API's own backwards-compatibility bridge is not a use."""

    _INITCONFIG = (
        "int Py_VerboseFlag = 0;\n"
        "static const PyConfigSpec SPEC[] = {\n"
        "    SPEC(verbose, UINT, PUBLIC, SYS_FLAG(8), GLOBAL(&Py_VerboseFlag, 0)),\n"
        "};\n"
    )

    def test_shim_file_is_suppressed(self):
        with TempProject({"Python/initconfig.c": self._INITCONFIG}) as root:
            result = mod.analyze(str(root))
            self.assertEqual(
                [f for f in result["findings"] if f["api"] == "Py_VerboseFlag"], []
            )
            self.assertGreater(result["summary"]["suppressed_compat_shim"], 0)

    def test_the_real_consumer_read_still_fires(self):
        """`Python/sysmodule.c:4533` pairs a deprecated fn with a deprecated var."""
        with TempProject(
            {
                "Python/sysmodule.c": (
                    "void\n"
                    "PySys_SetArgv(int argc, wchar_t **argv)\n"
                    "{\n"
                    "    PySys_SetArgvEx(argc, argv, Py_IsolatedFlag == 0);\n"
                    "}\n"
                ),
            }
        ) as root:
            findings = mod.analyze(str(root))["findings"]
            apis = {f["api"] for f in findings}
            self.assertIn("Py_IsolatedFlag", apis)
            self.assertIn("PySys_SetArgvEx", apis)
            for f in findings:
                self.assertEqual(f["line"], 4)
                self.assertEqual(f["removed_in"], "3.16")

    def test_column_zero_variable_definition_is_not_a_use(self):
        with TempProject(
            {
                "Objects/x.c": (
                    "int Py_DebugFlag = 0;\n"
                    "const char *Py_FileSystemDefaultEncoding = NULL;\n"
                ),
            }
        ) as root:
            self.assertEqual(mod.analyze(str(root))["findings"], [])


class TestNegativeControls(unittest.TestCase):
    """Measured, not asserted: the guards actually fire."""

    def test_chain_exceptions1_successor_is_never_flagged(self):
        """`_PyErr_ChainExceptions` is in the vocabulary; the live successor
        `_PyErr_ChainExceptions1` appears 7x in the Modules/ sample and must
        stay unflagged. This is the `PyUnicode_AsUnicode` substring failure
        that sank the 2021 list, reproduced on a different pair.
        """
        with TempProject(
            {
                "Modules/_pickle.c": (
                    "static void\n"
                    "f(void)\n"
                    "{\n"
                    "    _PyErr_ChainExceptions1(exc);\n"
                    "    _PyErr_ChainExceptions1(exc);\n"
                    "}\n"
                ),
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertEqual(
                [f for f in result["findings"] if f["api"] == "_PyErr_ChainExceptions"],
                [],
            )

    def test_the_deprecated_chain_exceptions_itself_is_still_flagged(self):
        with TempProject(
            {
                "Modules/_pickle.c": (
                    "static void\n"
                    "f(void)\n"
                    "{\n"
                    "    _PyErr_ChainExceptions(t, v, tb);\n"
                    "}\n"
                ),
            }
        ) as root:
            findings = [
                f
                for f in mod.analyze(str(root))["findings"]
                if f["api"] == "_PyErr_ChainExceptions"
            ]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["line"], 4)


class TestWordBoundaries(unittest.TestCase):
    """TK-20: no more substring false positives."""

    def test_substring_is_not_a_match(self):
        with TempProject(
            {
                "Objects/x.c": (
                    "void\nf(void)\n{\n    PyEval_GetBuiltinsExtra(a);\n}\n"
                ),
            }
        ) as root:
            result = mod.analyze(str(root))
            apis = {f.get("api") for f in result["findings"]}
            self.assertNotIn("PyEval_GetBuiltins", apis)

    def test_exact_name_is_a_match(self):
        with TempProject(
            {
                "Objects/x.c": (
                    "void\nf(void)\n{\n    PyObject *b = PyEval_GetBuiltins();\n}\n"
                ),
            }
        ) as root:
            result = mod.analyze(str(root))
            hits = [
                f for f in result["findings"] if f.get("api") == "PyEval_GetBuiltins"
            ]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["line"], 4)
            self.assertEqual(hits[0]["replacement"], "PyEval_GetFrameBuiltins")


class TestDefinitionSiteSuppression(unittest.TestCase):
    """TK-20: every 2021-era hit was the API's own definition."""

    def test_definition_body_is_not_a_use(self):
        with TempProject(
            {
                "Objects/x.c": (
                    "PyObject *\n"
                    "PyEval_GetBuiltins(void)\n"
                    "{\n"
                    "    /* PyEval_GetBuiltins implementation */\n"
                    "    return do_it();\n"
                    "}\n"
                ),
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["findings"], [])

    def test_declaration_is_not_a_use(self):
        with TempProject(
            {
                "Objects/x.h": (
                    "PyAPI_FUNC(PyObject *) PyEval_GetBuiltins(void);\n"
                    "#define PyErr_Restore(a, b, c) legacy(a, b, c)\n"
                ),
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["findings"], [])

    def test_forward_declaration_is_not_a_use(self):
        with TempProject(
            {
                "Objects/x.c": "static void PyErr_Restore(a, b, c);\n",
            }
        ) as root:
            result = mod.analyze(str(root))
            self.assertEqual(result["findings"], [])

    def test_statement_form_call_is_still_a_use(self):
        """A call statement has the same tail as a forward declaration."""
        with TempProject(
            {
                "Objects/x.c": ("void\nf(void)\n{\n    PyErr_Restore(t, v, tb);\n}\n"),
            }
        ) as root:
            result = mod.analyze(str(root))
            hits = [f for f in result["findings"] if f.get("api") == "PyErr_Restore"]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]["line"], 4)


class TestCommentAndStringSuppression(unittest.TestCase):
    """TK-20: comment prose and Clinic docstrings are not usage."""

    def test_comment_prose_is_not_a_use(self):
        with TempProject(
            {
                "Objects/x.c": (
                    "void\n"
                    "f(void)\n"
                    "{\n"
                    "    /* Formerly used PyErr_Restore here. */\n"
                    "    return;\n"
                    "}\n"
                ),
            }
        ) as root:
            self.assertEqual(mod.analyze(str(root))["findings"], [])

    def test_docstring_text_is_not_a_use(self):
        with TempProject(
            {
                "Objects/x.c": ('PyDoc_STRVAR(doc, "takes a Py_UNICODE buffer");\n'),
            }
        ) as root:
            self.assertEqual(mod.analyze(str(root))["findings"], [])

    def test_line_numbers_survive_a_multiline_comment(self):
        with TempProject(
            {
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
            }
        ) as root:
            findings = mod.analyze(str(root))["findings"]
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["line"], 7)


class TestSeverity(unittest.TestCase):
    """removed_in set -> FIX; otherwise CONSIDER."""

    def test_scheduled_removal_is_fix(self):
        with TempProject(
            {
                "Objects/x.c": (
                    "void\nf(void)\n{\n    _PyUnicodeWriter_Init(&writer);\n}\n"
                ),
            }
        ) as root:
            findings = mod.analyze(str(root))["findings"]
            self.assertEqual(findings[0]["severity"], "FIX")
            self.assertEqual(findings[0]["tier"], "hard-internal")
            self.assertIn("Py_BUILD_CORE", findings[0]["detail"])

    def test_unscheduled_deprecation_is_consider(self):
        with TempProject(
            {
                "Objects/x.c": (
                    'void\nf(void)\n{\n    PyModule_AddObject(m, "n", o);\n}\n'
                ),
            }
        ) as root:
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
                f
                for f in mod.analyze(str(root))["findings"]
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
                f
                for f in mod.analyze(str(root))["findings"]
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
                f
                for f in mod.analyze(str(root))["findings"]
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
                f
                for f in mod.analyze(str(root))["findings"]
                if f["type"] == "gc-untrack-macro-form"
            ]
            self.assertEqual(findings, [])


class TestEnvelope(unittest.TestCase):
    """Standard report envelope (design 4.2)."""

    def test_envelope_keys(self):
        with TempProject({"Objects/x.c": "int x;\n"}) as root:
            result = mod.analyze(str(root))
            for key in (
                "project_root",
                "scan_root",
                "files_analyzed",
                "functions_analyzed",
                "findings",
                "summary",
            ):
                self.assertIn(key, result)
            for key in (
                "total_findings",
                "by_api",
                "by_tier",
                "by_severity",
                "by_removal",
                "findings_needing_a_caveat",
                "suppressed_compat_shim",
                "apis_in_vocabulary",
            ):
                self.assertIn(key, result["summary"])

    def test_by_removal_orders_the_worklist_by_deadline(self):
        """Prioritise by removal date, not by the age of the deprecation."""
        with TempProject(
            {
                "Objects/x.c": (
                    "void\n"
                    "f(void)\n"
                    "{\n"
                    "    _PyUnicodeWriter_Init(&w);\n"
                    "    PySys_SetArgvEx(argc, argv, 0);\n"
                    "    PyMem_NEW(PyObject *, n);\n"
                    "}\n"
                ),
            }
        ) as root:
            by_removal = mod.analyze(str(root))["summary"]["by_removal"]
            self.assertEqual(by_removal.get("3.16"), 1)
            self.assertEqual(by_removal.get("3.18"), 1)
            self.assertEqual(by_removal.get("none"), 1)
            # Sorted soonest-first so the report can follow the key order.
            self.assertEqual(list(by_removal), ["3.16", "3.18", "none"])

    def test_max_files_is_honoured(self):
        with TempProject(
            {
                "Objects/a.c": "int a;\n",
                "Objects/b.c": "int b;\n",
                "Objects/c.c": "int c;\n",
            }
        ) as root:
            result = mod.analyze(str(root), max_files=1)
            self.assertEqual(result["files_analyzed"], 1)


if __name__ == "__main__":
    unittest.main()
