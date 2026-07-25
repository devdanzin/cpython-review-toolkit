# CPY-0079 payload -- runs ARMED.
# new_dict / new_dict_untracked pop PyDictObject from the dict freelist
# (_Py_FREELIST_POP at dictobject.c:974 / :988) before ever calling
# PyObject_GC_New, and a freelist pop is not an allocator call -- so the
# injected failure never fires unless the freelist is drained first, from
# INSIDE the armed region and with the dicts kept alive.  Empty dict
# displays are used because a formatted key would add many unrelated
# allocations and push the interesting index past any practical sweep range.
_hold = [{}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}]
c = e.copy()
