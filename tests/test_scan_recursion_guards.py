"""Tests for scan_recursion_guards.py — recursion-prone slots lacking a guard."""

import unittest

from helpers import TempProject, import_script


class TestScanRecursionGuards(unittest.TestCase):
    def setUp(self):
        self.mod = import_script("scan_recursion_guards")

    def _findings(self, files):
        with TempProject(files) as root:
            return self.mod.analyze(str(root))

    # --- true positives ----------------------------------------------------

    def test_container_hash_without_guard_is_flagged(self):
        # The tuple_hash / frozendict_hash class (gh-154318): loop over items
        # calling PyObject_Hash with no Py_EnterRecursiveCall.
        result = self._findings(
            {
                "Objects/mytuple.c": (
                    "static Py_hash_t\n"
                    "mytuple_hash(PyObject *self)\n"
                    "{\n"
                    "    Py_ssize_t len = Py_SIZE(self);\n"
                    "    Py_uhash_t acc = 0;\n"
                    "    for (Py_ssize_t i = 0; i < len; i++) {\n"
                    "        Py_hash_t y = PyObject_Hash(GET_ITEM(self, i));\n"
                    "        acc = (acc * 1000003) ^ (Py_uhash_t)y;\n"
                    "    }\n"
                    "    return (Py_hash_t)acc;\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["function"] == "mytuple_hash"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["type"], "missing_recursion_guard")
        self.assertEqual(f["slot"], "tp_hash")
        self.assertEqual(f["shape"], "container_element_descent")
        self.assertEqual(f["confidence"], "high")

    def test_self_recursive_parameter_walk_is_flagged(self):
        # The _Py_make_parameters class (gh-154275): self-recursion, no guard.
        result = self._findings(
            {
                "Objects/genericaliasobject.c": (
                    "static PyObject *\n"
                    "make_parameters(PyObject *args)\n"
                    "{\n"
                    "    PyObject *sub = make_parameters(inner(args));\n"
                    "    return sub;\n"
                    "}\n"
                )
            }
        )
        f = next(
            (f for f in result["findings"] if f["function"] == "make_parameters"),
            None,
        )
        self.assertIsNotNone(f)
        self.assertEqual(f["slot"], "parameter_walk")
        self.assertEqual(f["shape"], "self_recursion")
        self.assertEqual(f["confidence"], "high")

    def test_slot_designated_richcompare_descent_is_flagged(self):
        result = self._findings(
            {
                "Objects/myseq.c": (
                    "static PyObject *\n"
                    "myseq_cmp(PyObject *a, PyObject *b, int op)\n"
                    "{\n"
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        int r = PyObject_RichCompareBool("
                    "GET_ITEM(a, i), GET_ITEM(b, i), Py_EQ);\n"
                    "        if (r < 0) return NULL;\n"
                    "    }\n"
                    "    Py_RETURN_TRUE;\n"
                    "}\n"
                    "static PyTypeObject Seq_Type = {\n"
                    "    .tp_richcompare = myseq_cmp,\n"
                    "};\n"
                )
            }
        )
        f = next((f for f in result["findings"] if f["function"] == "myseq_cmp"), None)
        self.assertIsNotNone(f)
        self.assertEqual(f["slot"], "tp_richcompare")
        self.assertEqual(f["confidence"], "high")

    # --- true negatives ----------------------------------------------------

    def test_guarded_recursion_is_suppressed(self):
        result = self._findings(
            {
                "Objects/mytuple.c": (
                    "static Py_hash_t\n"
                    "mytuple_hash(PyObject *self)\n"
                    "{\n"
                    '    if (Py_EnterRecursiveCall(" while hashing")) return -1;\n'
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        PyObject_Hash(GET_ITEM(self, i));\n"
                    "    }\n"
                    "    Py_LeaveRecursiveCall();\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_repr_with_reprenter_is_suppressed(self):
        result = self._findings(
            {
                "Objects/mylist.c": (
                    "static PyObject *\n"
                    "mylist_repr(PyObject *self)\n"
                    "{\n"
                    "    if (Py_ReprEnter(self) != 0) return NULL;\n"
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        PyObject_Repr(GET_ITEM(self, i));\n"
                    "    }\n"
                    "    Py_ReprLeave(self);\n"
                    "    return NULL;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_scalar_hash_without_descent_is_not_flagged(self):
        # Hashes a single field, no loop, no self-call -> bounded, not flagged.
        result = self._findings(
            {
                "Objects/point.c": (
                    "static Py_hash_t\n"
                    "point_hash(PyObject *self)\n"
                    "{\n"
                    "    return PyObject_Hash(((PointObject *)self)->x);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_non_slot_self_recursion_is_not_flagged(self):
        # A plain helper that self-recurses but is not a recursion-prone slot
        # is out of scope (keeps false positives down).
        result = self._findings(
            {
                "Objects/util.c": (
                    "static int\n"
                    "count_down(int n)\n"
                    "{\n"
                    "    if (n <= 0) return 0;\n"
                    "    return count_down(n - 1);\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])

    def test_comment_suppression(self):
        result = self._findings(
            {
                "Objects/mytuple.c": (
                    "static Py_hash_t\n"
                    "mytuple_hash(PyObject *self)\n"
                    "{\n"
                    "    /* safe because elements are guaranteed non-container scalars */\n"
                    "    for (Py_ssize_t i = 0; i < n; i++) {\n"
                    "        PyObject_Hash(GET_ITEM(self, i));\n"
                    "    }\n"
                    "    return 0;\n"
                    "}\n"
                )
            }
        )
        self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
