# COLD payload: the pickle / __reduce_ex__ region (typeobject.c 7848-8406).
# reduce_newobj, _PyObject_GetState, object_getstate_default, _common_reduce and
# the copyreg._slotnames cache -- all reached cold, so the _slotnames build and
# the state-dict construction are inside the injection window.
for _i, (_a, _b) in enumerate(SLOT_PAIRS):
    _o = SLOT_OBJS[_i]
    _o.p = _i
    _o.q = _i
    _r = _o.__reduce_ex__(2)

for _i, (_a, _b) in enumerate(DICT_PAIRS):
    _o = DICT_OBJS[_i]
    _o.x = _i
    _r = _o.__reduce_ex__(2)
    _r = _o.__reduce_ex__(1)
    _r = _o.__reduce__()
