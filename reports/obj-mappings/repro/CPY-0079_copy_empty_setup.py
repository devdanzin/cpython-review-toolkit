# CPY-0079 setup -- runs UNARMED.
# `e.copy()` on an EMPTY dict takes the ma_used == 0 arm of
# copy_lock_held_untracked (Objects/dictobject.c:4484) and returns
# dict_new_untracked(&PyDict_Type) unchecked into
# `assert(!_PyObject_GC_IS_TRACKED(d))` at :4494.
e = {}
