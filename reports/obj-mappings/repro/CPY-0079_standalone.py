# CPY-0079 standalone reproducer -- no sweep harness needed.
#
#   ~/projects/python_build_matrix/builds/debug-gil-nojit/python CPY-0079_standalone.py
#   -> Segmentation fault (core dumped)     [40/40 runs]
#
# Mechanism (gdb-confirmed, see the report):
#   copy_lock_held_untracked (Objects/dictobject.c:4484) takes the ma_used == 0
#   arm, assigns `d = dict_new_untracked(&PyDict_Type)` at :4492 WITHOUT a NULL
#   check, and then executes `assert(!_PyObject_GC_IS_TRACKED(d))` at :4494 --
#   an unconditional dereference of d.  That dereference is UB when d is NULL,
#   so with assertions enabled the optimizer is entitled to assume the callee
#   cannot return NULL and DELETES the `if (self == NULL) return NULL;` guard
#   inside the inlined anydict_new_untracked (:5362).  The fault is therefore
#   not the assert reading NULL -- it is the *store* `d->_ma_watcher_tag = 0`
#   at :5368 writing through NULL.
#
# The empty dicts drain the PyDictObject freelist (_Py_FREELIST_POP at
# dictobject.c:974 / :988): a freelist pop is not an allocator call, so without
# the drain the injected failure never reaches _PyType_AllocNoTrack and the
# sweep reports a false clean.  They are built BEFORE arming, so index 0 is the
# allocation inside e.copy().
import _testcapi

e = {}
_drain = [{} for _ in range(40)]

_testcapi.set_nomemory(0, 1)
e.copy()
