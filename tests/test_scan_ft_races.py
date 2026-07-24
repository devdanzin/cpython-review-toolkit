"""Tests for scan_ft_races.py — free-threading data races (T1/T2/T3)."""

import unittest

from helpers import TempProject, import_script


class TestScanFtRaces(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_ft_races")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    # --- T3: iterator-exhaustion double-DECREF -----------------------------

    def test_t3_iternext_pyclear_without_lock_is_flagged(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    if (it->it_seq == NULL) return NULL;\n"
                    "    if (exhausted(it)) {\n"
                    "        Py_CLEAR(it->it_seq);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    return next_item(it);\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["type"] == "iternext_double_decref"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["ft_class"], "T3")
        self.assertEqual(f["confidence"], "high")

    def test_t3_setnull_decref_without_lock_is_flagged(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    PyObject *seq = it->it_seq;\n"
                    "    if (done(it)) {\n"
                    "        it->it_seq = NULL;\n"
                    "        Py_DECREF(seq);\n"
                    "        return NULL;\n"
                    "    }\n"
                    "    return next_item(it);\n"
                    "}\n"
                )
            }
        )
        self.assertTrue(
            any(f["type"] == "iternext_double_decref" for f in result["findings"])
        )

    def test_t3_iternext_with_critical_section_is_suppressed(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static PyObject *\n"
                    "myiter_iternext(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    Py_CLEAR(it->it_seq);\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T3"], [])

    def test_t3_non_iternext_is_not_flagged(self):
        result = self._findings(
            {
                "Objects/myiter.c": (
                    "static void\n"
                    "myiter_dealloc(PyObject *self)\n"
                    "{\n"
                    "    MyIter *it = (MyIter *)self;\n"
                    "    Py_CLEAR(it->it_seq);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T3"], [])

    # --- T2: lazy-init without a critical section --------------------------

    def test_t2_lazy_init_without_lock_is_flagged(self):
        result = self._findings(
            {
                "Objects/descr.c": (
                    "static PyObject *\n"
                    "descr_get_qualname(PyObject *self)\n"
                    "{\n"
                    "    MyDescr *descr = (MyDescr *)self;\n"
                    "    if (descr->d_qualname == NULL)\n"
                    "        descr->d_qualname = calculate_qualname(descr);\n"
                    "    return Py_XNewRef(descr->d_qualname);\n"
                    "}\n"
                )
            }
        )
        f = next(
            (
                f
                for f in result["findings"]
                if f["type"] == "lazy_init_no_critical_section"
            ),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["ft_class"], "T2")
        self.assertEqual(f["member"], "descr->d_qualname")

    def test_t2_bang_form_is_flagged(self):
        result = self._findings(
            {
                "Objects/x.c": (
                    "static PyObject *\n"
                    "get_cache(PyObject *self)\n"
                    "{\n"
                    "    Foo *f = (Foo *)self;\n"
                    "    if (!f->cache) {\n"
                    "        f->cache = build_cache(f);\n"
                    "    }\n"
                    "    return f->cache;\n"
                    "}\n"
                )
            }
        )
        self.assertTrue(
            any(
                f["type"] == "lazy_init_no_critical_section" for f in result["findings"]
            )
        )

    def test_t2_with_critical_section_is_suppressed(self):
        result = self._findings(
            {
                "Objects/x.c": (
                    "static PyObject *\n"
                    "get_cache(PyObject *self)\n"
                    "{\n"
                    "    Foo *f = (Foo *)self;\n"
                    "    Py_BEGIN_CRITICAL_SECTION(self);\n"
                    "    if (f->cache == NULL)\n"
                    "        f->cache = build_cache(f);\n"
                    "    Py_END_CRITICAL_SECTION();\n"
                    "    return f->cache;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T2"], [])

    def test_t2_null_check_without_assignment_is_not_flagged(self):
        result = self._findings(
            {
                "Objects/x.c": (
                    "static PyObject *\n"
                    "get_thing(PyObject *self)\n"
                    "{\n"
                    "    Foo *f = (Foo *)self;\n"
                    "    if (f->thing == NULL)\n"
                    "        return NULL;\n"
                    "    return f->thing;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T2"], [])

    # --- T1: atomic/plain access asymmetry ---------------------------------

    def test_t1_atomic_plain_asymmetry_is_flagged(self):
        result = self._findings(
            {
                "Modules/counter.c": (
                    "static PyObject *\n"
                    "count_next(CountObject *lz)\n"
                    "{\n"
                    "    Py_ssize_t c = FT_ATOMIC_LOAD_SSIZE_RELAXED(lz->cnt);\n"
                    "    return PyLong_FromSsize_t(c);\n"
                    "}\n"
                    "static PyObject *\n"
                    "count_repr(CountObject *lz)\n"
                    "{\n"
                    '    return PyUnicode_FromFormat("count(%zd)", lz->cnt);\n'
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["type"] == "atomic_plain_asymmetry"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["ft_class"], "T1")
        self.assertEqual(f["member"], "cnt")

    def test_t1_all_atomic_is_not_flagged(self):
        result = self._findings(
            {
                "Modules/counter.c": (
                    "static void bump(CountObject *lz)\n"
                    "{\n"
                    "    FT_ATOMIC_STORE_SSIZE_RELAXED(lz->cnt, "
                    "FT_ATOMIC_LOAD_SSIZE_RELAXED(lz->cnt) + 1);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual([f for f in result["findings"] if f["ft_class"] == "T1"], [])

    def test_envelope_shape(self):
        result = self._findings({"Objects/x.c": "static void f(void) { }\n"})
        for key in (
            "project_root",
            "scan_root",
            "files_analyzed",
            "functions_analyzed",
            "findings",
            "summary",
        ):
            self.assertIn(key, result)
        self.assertIn("by_class", result["summary"])


if __name__ == "__main__":
    unittest.main()
