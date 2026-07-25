"""Confirm the field-expression recall gap in the new VarObject nitems rule."""
import json
import pathlib
import subprocess
import sys
import tempfile

SCAN = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts/scan_memory_patterns.py"
PY = sys.executable

CASES = {
    "bare_identifier": "PyTypeObject *t = f(); x = (PyTypeObject *)mt->tp_alloc(mt, n);",
    "field_expression": "PyTypeObject *t = f(); x = (PyTypeObject *)mt->tp_alloc(mt, ctx->nslot);",
    "array_subscript": "PyTypeObject *t = f(); x = (PyTypeObject *)mt->tp_alloc(mt, sizes[i]);",
    "field_times_two": "PyTypeObject *t = f(); x = (PyTypeObject *)mt->tp_alloc(mt, ctx->nslot * 2);",
    "literal": "PyTypeObject *t = f(); x = (PyTypeObject *)mt->tp_alloc(mt, 0);",
}

TEMPLATE = """#include "Python.h"
static PyObject *
probe(PyTypeObject *mt, Py_ssize_t n, struct C *ctx, Py_ssize_t *sizes, int i)
{
    PyObject *x;
    %s
    return x;
}
"""

d = pathlib.Path(tempfile.mkdtemp())
for name, body in CASES.items():
    (d / (name + ".c")).write_text(TEMPLATE % body)

out = subprocess.run([PY, SCAN, str(d)], capture_output=True, text=True)
data = json.loads(out.stdout)
print("census:", data["varobject_allocation_census"])
hit = {f["file"] for f in data["findings"]}
for name in CASES:
    print(f"  {name:20s} reported={name + '.c' in hit}")
