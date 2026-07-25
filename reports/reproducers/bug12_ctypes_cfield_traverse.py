"""Bug: PyCField_traverse missing Py_VISIT(self->name)

_ctypes/cfield.c:405-411:
    PyCField_traverse:
        Py_VISIT(Py_TYPE(self));
        Py_VISIT(self->proto);
        return 0;  // MISSING: Py_VISIT(self->name)

    PyCField_clear:
        Py_CLEAR(self->proto);
        Py_CLEAR(self->name);  // clear handles it, traverse doesn't

CFieldObject.name is a PyObject* (PyUnicode) that is cleared by
tp_clear but not visited by tp_traverse. If name participates in
a reference cycle, GC won't find it.

In practice, field names are interned strings that don't participate
in cycles, so this is LOW risk but still a code correctness bug.

Expected: confirmed by code inspection, not triggerable from Python
(strings don't form reference cycles).
"""

import ctypes
import gc

class MyStruct(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int), ("y", ctypes.c_float)]

# Access the field descriptors
x_field = MyStruct.x
y_field = MyStruct.y

print(f"Field descriptor type: {type(x_field)}")
print(f"x_field: {x_field}")
print(f"y_field: {y_field}")

# The CFieldObject has a 'name' member (PyUnicode).
# traverse visits proto but not name.
# clear clears both proto and name.
# This is a traverse/clear mismatch.

# Force GC to exercise traverse
gc.collect()
gc.collect()

print()
print("Bug confirmed by code inspection:")
print("  PyCField_traverse: visits Py_TYPE(self), self->proto")
print("  PyCField_clear: clears self->proto, self->name")
print("  Missing: Py_VISIT(self->name) in traverse")
print()
print("Fix: add Py_VISIT(self->name) to PyCField_traverse")
print("at Modules/_ctypes/cfield.c:410")
print()
print("Risk: LOW — field names are interned strings that don't")
print("form reference cycles. But traverse/clear should be consistent.")
