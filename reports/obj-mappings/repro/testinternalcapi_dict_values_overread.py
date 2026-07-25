"""_testinternalcapi.get_object_dict_values reads values->values[i] for
i < ht_cached_keys->dk_nentries, without bounding by THIS instance's
values->capacity.

Modules/_testinternalcapi.c:2093-2125
    PyDictValues *values = _PyObject_InlineValues(obj);   // :2100
    if (!values->valid) { Py_RETURN_NONE; }               // :2101
    PyDictKeysObject *keys = ((PyHeapTypeObject *)type)->ht_cached_keys;
    int size = (int)keys->dk_nentries;                    // :2106  CLASS-wide
    ...
    for (int i = 0; i < size; i++) {
        PyObject *item = values->values[i];               // :2114  INSTANCE-wide

`values->capacity` is fixed when the instance is allocated; `dk_nentries` of
the shared keys is a property of the CLASS and keeps growing as any instance
adds attributes.
"""

import sys

import _testinternalcapi


class C:
    pass


a = C()
a.x = 1

# Grow the class's shared ht_cached_keys via OTHER instances, leaving `a`'s
# inline values array at its original capacity.
holders = []
for n in range(1, 400):
    b = C()
    for i in range(n):
        setattr(b, "a%d" % i, i)
    holders.append(b)
    try:
        vals = _testinternalcapi.get_object_dict_values(a)
    except Exception as exc:
        print("n=%d raised %r" % (n, exc))
        break
    if vals is None:
        print("n=%d -> None (values->valid == 0), stopping" % n)
        break
    if len(vals) > 8:
        print("n=%d -> tuple of %d entries read out of `a`" % (n, len(vals)))
        print("       %r" % (vals[:12],))
        if len(vals) > 40:
            break

print("SURVIVED", flush=True)
sys.exit(0)
