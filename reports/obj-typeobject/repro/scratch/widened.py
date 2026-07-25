import re, sys, collections
sys.path.insert(0, "/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts")
import scan_null_checks as S

PATH="/home/danzin/projects/cpython/Objects/typeobject.c"
src=open(PATH,encoding="utf-8",errors="replace").read()
clean=S.strip_comments_and_strings(src)
funcs=S.find_functions(clean)

# Every static function in this file whose return type is a pointer (can return NULL),
# plus a widened set of tree-wide fallible APIs.
localfns = set()
for f in funcs:
    localfns.add(f["name"])

WIDE = set(S.ALLOC_APIS) | set(S.PYOBJ_APIS) | localfns | {
 "PyObject_GetIter","PyObject_CallOneArg","_PyObject_CallNoArgs","_PyObject_LookupSpecial",
 "PyLong_FromSsize_t","PyType_GenericAlloc","PyTuple_Pack","PyDict_GetItemWithError",
 "PyDict_Copy","PyDict_Keys","PyDict_Values","PyDict_Items","PyType_GetDict",
 "_PyType_Lookup","_PyType_LookupRef","_PyDict_GetItemStringWithError","PyMapping_Keys",
 "PyUnicode_AsUTF8","PyUnicode_AsUTF8AndSize","PyUnicode_FromStringAndSize","PyUnicode_Substring",
 "PyObject_CallMethodNoArgs","PyObject_Vectorcall","_PyObject_Call_Prepend","PyStaticMethod_New",
 "PyClassMethod_New","PyDescr_NewGetSet","PyDescr_NewMember","PyDescr_NewMethod","PyDescr_NewWrapper",
 "PyCFunction_NewEx","PyLong_FromVoidPtr","_PyTuple_FromPair","_PyType_GetSubclasses",
 "PyWeakref_NewRef","PyList_GetItem","PyDict_GetItem","PyObject_GenericGetDict",
 "PyMem_RawMalloc","PyMem_RawCalloc","PyMem_RawRealloc","strrchr","strchr","PyType_GetModule",
 "PyType_GetModuleByDef","PyType_GetSlot","_PyModule_GetState","PyObject_GetAttrString",
 "_PyDict_NewPresized","PySlice_New","PyImport_ImportModule","_PyEval_GetFrame",
 "_PyThreadState_GetFrame","_PyFrame_GetCode","PyCell_New","PyUnicode_Concat","PyUnicode_Join",
 "PyUnicode_Replace","PyUnicode_Format","PyObject_Format","_PyUnicode_Copy","PySequence_Fast",
}
WIDE -= {"f"}

alt = re.compile(
    r"(?P<lval>" + S._LVALUE + r")\s*(?<![=!<>+\-*/%&|^])=(?!=)\s*(?:\([^()]*\)\s*)?"
    r"(?P<api>" + "|".join(re.escape(a) for a in sorted(WIDE, key=len, reverse=True)) + r")\s*\(")

out=[]
stage=collections.Counter()
for f in funcs:
    body=f["body"]; depths=S._depth_profile(body)
    for m in alt.finditer(body):
        stage["cand"]+=1
        api=m.group("api"); targets=S._assignment_targets(body,m); primary=targets[0]
        if S._in_control_condition(body,m.start()): continue
        ce=S._matching_paren(body,m.end()-1); ws=m.end() if ce==-1 else ce+1
        window=S._truncate_at_reassignment(S._window(body,ws),targets)
        checked=None
        for t in targets:
            c=re.search(S._NULL_CHECK_TEMPLATE.format(var=S._lvalue_regex(t)),window)
            if c and (checked is None or c.start()<checked): checked=c.start()
        deref=re.search(S._DEREF_TEMPLATE.format(var=S._lvalue_regex(primary)),window)
        if deref is None: continue
        stage["deref"]+=1
        dabs=ws+deref.start()
        if not S._dominates(body,depths,m.start(),dabs): continue
        stage["dominated"]+=1
        line=f["body_line"]+body[:m.start()].count("\n")
        if checked is None:
            out.append(("unchecked_alloc",line,f["name"],api,primary,deref.group(0).strip()))
        elif deref.start()<checked:
            dl=f["body_line"]+body[:dabs].count("\n")
            out.append(("deref_before_check",dl,f["name"],api,primary,deref.group(0).strip()))

print("candidates:",stage["cand"],"deref-in-window:",stage["deref"],"dominated:",stage["dominated"],"findings:",len(out))
for t,l,fn,api,v,d in sorted(out,key=lambda x:x[1]):
    print(f"{t:20s} typeobject.c:{l:<6d} {fn:34s} {v} = {api}(...)   deref: {d}")
