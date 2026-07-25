#!/usr/bin/env python3
"""Re-run the IDENTICAL pipeline with the alphabet widened three ways.

W1: field-forwarding accessors (return self->FIELD where FIELD is nullable)
W2: ~70 common fallible CPython APIs absent from the closed enum
W3: both

Purpose: separate "the file is clean" from "the rule cannot see the file".
"""
import sys
import re
from pathlib import Path

SCRIPTS = "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts"
sys.path.insert(0, SCRIPTS)
import scan_null_checks as S  # noqa: E402

TARGET = Path("/home/danzin/projects/cpython/Objects/typeobject.c")
src = TARGET.read_text(encoding="utf-8", errors="replace")
stripped = S.strip_comments_and_strings(src)
funcs = S.find_functions(stripped)
base_extra = S.nullable_source_calls(funcs)

# ---- W1: field-forwarding accessors ------------------------------------
# `static PyObject *lookup_tp_mro(t) { ... return self->tp_mro; }` has no
# literal `return NULL` and is not a call-forwarder, so nullable_source_calls
# cannot see it.  Nullability comes from the FIELD.
FIELD_RETURN_RE = re.compile(r"\breturn\s+[A-Za-z_]\w*\s*->\s*(\w+)\s*;")
W1 = set()
field_by_fn = {}
for f in funcs:
    if "*" not in (f.get("return_type") or ""):
        continue
    if f["name"] in base_extra:
        continue
    ms = FIELD_RETURN_RE.findall(f["body"])
    if ms:
        W1.add(f["name"])
        field_by_fn[f["name"]] = sorted(set(ms))

# ---- W2: common fallible APIs missing from the closed enum --------------
W2 = {
    "PyTuple_Pack", "PyDict_GetItemWithError", "PyDict_GetItem",
    "PyType_GetDict", "_PyType_LookupRef", "_PyType_Lookup",
    "_PyObject_CallNoArgs", "PyObject_Vectorcall", "PyObject_VectorcallMethod",
    "PyStaticMethod_New", "PyClassMethod_New", "PyDescr_NewGetSet",
    "PyDescr_NewMethod", "PyDescr_NewMember", "PyDescr_NewWrapper",
    "PyDescr_NewClassMethod", "PyCFunction_NewEx", "PyCFunction_New",
    "PyLong_FromVoidPtr", "PyLong_FromSsize_t", "PyLong_FromSize_t",
    "PyObject_GetIter", "PyDict_Copy", "PyUnicode_AsUTF8",
    "PyUnicode_AsUTF8AndSize", "PyUnicode_InternFromString",
    "PyUnicode_FromStringAndSize", "PyUnicode_Substring", "PyUnicode_Concat",
    "PyUnicode_Join", "PyUnicode_Replace", "PyUnicode_Split",
    "PyMapping_Keys", "PyMapping_Values", "PyMapping_Items",
    "PySequence_Fast", "PySequence_Concat", "PySequence_Repeat",
    "PyObject_GenericGetAttr", "PyObject_GetAttrId",
    "PyObject_CallOneArg", "PyObject_CallNoArgs", "PyObject_CallFunctionObjArgs",
    "PyObject_CallMethodObjArgs", "PyObject_CallMethodOneArg",
    "PyObject_Vectorcall", "_PyObject_FastCall", "_PyObject_Call",
    "PyWeakref_NewRef", "PyType_GenericNew", "PyType_FromSpec",
    "PyType_FromMetaclass", "PyType_FromSpecWithBases",
    "PySlice_New", "PyMethod_New", "PyCell_New",
    "PyImport_ImportModuleLevel", "PyImport_Import", "PyImport_GetModule",
    "PyErr_Format", "PyEval_GetFrame",
    "PyDict_SetDefault", "PyDict_SetDefaultRef",
    "_PyDict_GetItemStringWithError", "PyObject_SelfIter",
    "PyFrozenSet_New", "PySet_New", "PyByteArray_FromStringAndSize",
    "PyBytes_FromFormat", "PyNumber_Index", "PyNumber_Long",
    "_PyObject_LookupSpecial", "_PyObject_GetAttrId",
}

REGIONS = [
    ("R3 managed-static", 228, 522), ("R4 accessors", 524, 810),
    ("R6 watchers/versions", 971, 1481), ("R11 MRO C3", 3217, 3702),
    ("R18 PyType_Get*", 5834, 6139), ("R19 lookup cache", 6140, 6452),
    ("R20 setflags", 6453, 6528), ("R21 getattro/setattro", 6529, 6848),
    ("R25 __class__ assign", 7482, 7846), ("R26 pickle", 7848, 8406),
    ("R37 super", 12534, 13068),
]


def region_of(line):
    for name, lo, hi in REGIONS:
        if lo <= line <= hi:
            return name
    return "pass-1"


def run(extra_set, label):
    alloc_re = S._alloc_re_for(extra_set)
    matched = 0
    out = []
    for f in funcs:
        body = f["body"]
        depths = S._depth_profile(body)
        for m in alloc_re.finditer(body):
            api = m.group("api")
            matched += 1
            targets = S._assignment_targets(body, m)
            primary = targets[0]
            line = f["body_line"] + body[:m.start()].count("\n")
            if S._in_control_condition(body, m.start()):
                continue
            call_end = S._matching_paren(body, m.end() - 1)
            ws = m.end() if call_end == -1 else call_end + 1
            window = S._truncate_at_reassignment(S._window(body, ws), targets)
            checked_at = None
            for t in targets:
                c = re.search(S._NULL_CHECK_TEMPLATE.format(var=S._lvalue_regex(t)),
                              window)
                if c is not None and (checked_at is None or c.start() < checked_at):
                    checked_at = c.start()
            deref = re.search(S._DEREF_TEMPLATE.format(var=S._lvalue_regex(primary)),
                              window)
            if deref is None:
                continue
            deref_abs = ws + deref.start()
            if not S._dominates(body, depths, m.start(), deref_abs):
                join = S._join_after_full_ifelse(body, m.start(), targets)
                if join is None or not S._dominates(body, depths, join, deref_abs):
                    continue
            if checked_at is not None and deref.start() >= checked_at:
                continue
            kind = "unchecked_alloc" if checked_at is None else "deref_before_check"
            out.append((line, region_of(line), f["name"], primary, api,
                        deref.group(0).strip(), kind))
    print(f"\n### {label}: alphabet={len(S.ALLOC_APIS | S.PYOBJ_APIS | extra_set)}, "
          f"matched sites={matched}, candidates={len(out)}")
    for o in sorted(out):
        print(f"  typeobject.c:{o[0]:6d} [{o[1]:22s}] {o[2]:32s} "
              f"{o[3]} = {o[4]}(...)  deref={o[5]!r}  {o[6]}")
    return out


print(f"W1 field-forwarding accessors discovered: {len(W1)}")
for fn in sorted(W1):
    print(f"   {fn}  -> returns ->{'/'.join(field_by_fn[fn])}")

run(base_extra, "SHIPPED (base)")
run(base_extra | W1, "W1 = base + field-forwarding accessors")
run(base_extra | W2, "W2 = base + common fallible APIs")
run(base_extra | W1 | W2, "W3 = base + W1 + W2")
