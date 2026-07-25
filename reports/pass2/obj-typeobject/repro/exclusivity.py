"""Extract exclusivity regions from typeobject.c and report user-code-capable calls inside them."""
import re
import sys

PATH = "/home/danzin/projects/cpython/Objects/typeobject.c"
src = open(PATH, encoding="utf-8").read().splitlines()

OPEN_TOKENS = {
    "BEGIN_TYPE_LOCK()": ("TYPE_LOCK", "END_TYPE_LOCK()"),
    "BEGIN_TYPE_DICT_LOCK(": ("TYPE_DICT_LOCK", "END_TYPE_DICT_LOCK()"),
    "types_stop_world()": ("STW", "types_start_world()"),
    "_PyEval_StopTheWorld(": ("STW-raw", "_PyEval_StartTheWorld("),
}

# Calls that can execute arbitrary Python (directly or via a slot dispatch).
USER_CODE = re.compile(
    r"\b(PyObject_Call\w*|_PyObject_Call\w*|call_method\w*|call_unbound\w*|"
    r"vectorcall\w*|_PyObject_VectorCall\w*|PyObject_RichCompare\w*|"
    r"PyObject_GetAttr\w*|PyObject_SetAttr\w*|PyObject_GetOptionalAttr\w*|"
    r"_PyObject_LookupSpecial\w*|PyObject_Repr|PyObject_Str|PyObject_Hash|"
    r"PyErr_FormatUnraisable|PyErr_WriteUnraisable|PySequence_Tuple|PySequence_List|"
    r"PyIter_Next|PyObject_GetIter|PyMapping_\w+|PySequence_\w+|"
    r"PyDict_Merge|PyDict_Update|Py_DECREF|Py_XDECREF|Py_CLEAR|Py_SETREF|Py_XSETREF|"
    r"PyType_Modified|_PyType_Modified\w*|PyErr_Format|PyErr_SetObject|"
    r"PyList_Append|PyList_New|PyDict_New|PyTuple_New|PyMem_Malloc|PyObject_Malloc|"
    r"Py_BEGIN_CRITICAL_SECTION\w*|PyMutex_Lock|_PyDict_\w+|PyDict_SetItem\w*|"
    r"lookup_maybe_method|slot_\w+|mro_invoke|type_lock_prevent_release)\b"
)

# find function boundaries: lines that start at col 0 with an identifier and '('
funcs = []
for i, line in enumerate(src):
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(", line)
    if m and not line.startswith(("#", "}")):
        funcs.append((i + 1, m.group(1)))


def fname(lineno):
    best = ("?", 0)
    for ln, nm in funcs:
        if ln <= lineno:
            best = (nm, ln)
        else:
            break
    return f"{best[0]}@{best[1]}"


regions = []
for i, line in enumerate(src):
    for tok, (kind, close) in OPEN_TOKENS.items():
        if tok in line and not line.lstrip().startswith(("//", "*", "#define")):
            # find close within next 400 lines
            end = None
            for j in range(i + 1, min(i + 400, len(src))):
                if close in src[j]:
                    end = j
                    break
            regions.append((kind, i + 1, (end + 1) if end else None))

for kind, start, end in regions:
    if end is None:
        print(f"{kind} {start}: NO CLOSE FOUND ({fname(start)})")
        continue
    hits = []
    for k in range(start, end - 1):
        line = src[k]
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        for m in USER_CODE.finditer(line):
            hits.append((k + 1, m.group(1), stripped[:110]))
    if hits:
        print(f"\n=== {kind} {start}-{end}  in {fname(start)} ===")
        for ln, sym, txt in hits:
            print(f"  {ln:6d} {sym:34s} {txt}")
