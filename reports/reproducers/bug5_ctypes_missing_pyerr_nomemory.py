"""Bug 5: Missing PyErr_NoMemory in ctypes StructUnionType_paramfunc

_ctypes/_ctypes.c:670-672:
    ptr = PyMem_Malloc(self->b_size);
    if (ptr == NULL) {
        return NULL;  // No PyErr_NoMemory()!
    }

Status: NOT REPRODUCIBLE from Python.

_testcapi.set_nomemory() doesn't work on non-debug builds (the
allocator hooks aren't installed), and ASAN builds replace the
allocator entirely. The paramfunc path requires calling a C function
with a struct argument by value, which triggers the malloc during
argument marshalling — but set_nomemory can't intercept it.

The bug is confirmed by code inspection: PyMem_Malloc (unlike
PyObject_Malloc) does NOT set PyErr_NoMemory on failure. The caller
must do it. StructUnionType_paramfunc returns NULL without setting
any exception, which would cause a SystemError up the call chain.

The fix is a one-liner at _ctypes.c:672:
    if (ptr == NULL) {
        PyErr_NoMemory();
        return NULL;
    }
"""

print("Bug 5: Missing PyErr_NoMemory in ctypes StructUnionType_paramfunc")
print()
print("NOT REPRODUCIBLE from Python — requires OOM injection into")
print("PyMem_Malloc during ctypes argument marshalling, which")
print("_testcapi.set_nomemory() cannot reach on standard builds.")
print()
print("The bug is confirmed by code inspection at _ctypes.c:670-672.")
print("When PyMem_Malloc(self->b_size) fails for a struct > sizeof(void*),")
print("the function returns NULL without calling PyErr_NoMemory().")
