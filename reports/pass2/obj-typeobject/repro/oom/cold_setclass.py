# COLD payload: __class__ assignment (typeobject.c 7482-7846).
# object_set_class -> object_set_class_world_stopped -> compatible_for_assignment,
# plus the managed-dict materialize + _PyDict_DetachFromObject step whose failure
# returns -1 with the object mid-reassignment. Each pair is touched for the
# FIRST time here, so the allocation footprint is the real one.
for _i, (_a, _b) in enumerate(SLOT_PAIRS):
    _o = SLOT_OBJS[_i]
    _o.p = 1
    _o.q = 2
    _o.__class__ = _b
    _o.__class__ = _a

for _i, (_a, _b) in enumerate(DICT_PAIRS):
    _o = DICT_OBJS[_i]
    _o.x = 1
    _o.y = 2
    _o.__class__ = _b
    _o.__class__ = _a
