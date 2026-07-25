import re
import pathlib
from collections import Counter

# NULL-returning constructor/call APIs that are genuinely fallible but are
# absent from scan_null_checks.py's PYOBJ_APIS / ALLOC_APIS.
CANDIDATES = [
    "PyObject_GetIter", "PyObject_CallOneArg", "PyObject_CallNoArgs",
    "_PyObject_CallNoArgs", "PyObject_Vectorcall", "PyObject_CallFunctionObjArgs",
    "PyType_GenericAlloc", "PyUnicodeWriter_Create", "_PyEval_GetBuiltin",
    "PySet_New", "PyDict_Copy", "PyDict_Items", "PyDict_Keys", "PyDict_Values",
    "PyUnicode_Concat", "PyUnicode_FromFormatV", "PyUnicode_Join",
    "PyUnicode_Substring", "_PyObject_LookupSpecial", "PyMapping_Keys",
    "PyMapping_Items", "PyLong_FromSsize_t", "PyLong_FromSize_t",
    "PyUnicode_FromStringAndSize", "PyUnicode_InternFromString",
    "PyList_GetSlice", "PyTuple_GetSlice", "PySequence_Fast",
    "PyObject_GenericGetAttr", "PyObject_SelfIter", "PyWeakref_NewRef",
    "PyWeakref_NewProxy", "PyCapsule_New", "PyStructSequence_New",
    "_PyDict_NewPresized", "PyODict_New", "PyIter_Next",
]
ROOT = pathlib.Path("/home/danzin/projects/cpython/Objects")
counts: Counter = Counter()
for p in sorted(ROOT.glob("*.c")):
    txt = p.read_text(errors="replace")
    for api in CANDIDATES:
        n = len(re.findall(r"\w+\s*=\s*(?:\([^)]*\)\s*)?" + api + r"\s*\(", txt))
        if n:
            counts[api] += n
print("assignment sites in Objects/ from fallible APIs missing from the scanner list:")
print("TOTAL:", sum(counts.values()))
for api, n in counts.most_common():
    print(f"  {n:3d}  {api}")
