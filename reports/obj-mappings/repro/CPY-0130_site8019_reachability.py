"""CPY-0130 site 2 -- is Objects/dictobject.c:8019 (`dict->ma_values = NULL;`)
reachable at all?

    Objects/dictobject.c:8006-8024  PyObject_ClearManagedDict
        Py_BEGIN_CRITICAL_SECTION(dict);
        err = detach_dict_from_object(dict, obj);
        Py_END_CRITICAL_SECTION();
        if (err) {
            /* Must be out of memory */
            assert(PyErr_Occurred() == PyExc_MemoryError);
            PyErr_FormatUnraisable(...);
            Py_BEGIN_CRITICAL_SECTION(dict);
            PyDictKeysObject *oldkeys = dict->ma_keys;
            set_keys(dict, Py_EMPTY_KEYS);      /* :8018  ATOMIC RELEASE STORE */
            dict->ma_values = NULL;             /* :8019  PLAIN STORE */
            ...

`detach_dict_from_object` fails on exactly one condition: `copy_values` returning
NULL, i.e. an allocation failure.  So :8019 is an OOM-only line.  This script
sweeps `_testcapi.set_nomemory` over the allocation index space of

    o = C(); o.a = 1
    d = o.__dict__      # materialise: ma_values == inline values, dict is LIVE
                        # and externally referenced by `d`
    del o               # -> subtype_dealloc -> PyObject_ClearManagedDict

and reports which index (if any) drives execution through :8019.  `d` is still
held, so if the line is reached it is reached on a dict with an external
reference -- which is the question the record asks.

Usage:  <python> CPY-0130_site8019_reachability.py <index>     # one probe
        (driven by CPY-0130_site8019_sweep.sh)
Exit 0 = clean, 3 = MemoryError raised at the probe, other = crash.
"""

import sys

import _testcapi


class C:
    pass


def main():
    idx = int(sys.argv[1])
    o = C()
    o.a = 1
    o.b = 2
    d = o.__dict__          # materialise; ma_values still == inline values
    assert isinstance(d, dict)
    _testcapi.set_nomemory(idx, idx + 1)
    try:
        del o               # -> PyObject_ClearManagedDict -> detach (may OOM)
    except MemoryError:
        _testcapi.remove_mem_hooks()
        return 3
    _testcapi.remove_mem_hooks()
    # touch the dict afterwards: if :8019 ran, the dict is now keys=EMPTY,
    # values=NULL, used=0
    return 0 if len(d) >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
