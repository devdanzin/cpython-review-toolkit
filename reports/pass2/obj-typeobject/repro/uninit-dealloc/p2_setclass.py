# PASS-2 payload B: __class__ assignment -> object_set_class_world_stopped,
# including the INLINE_VALUES materialize + _PyDict_DetachFromObject step whose
# failure returns -1 with the object mid-reassignment.
dobj.__class__ = DOther
dobj.__class__ = DBase
sobj.__class__ = SOther
sobj.__class__ = SBase
dobj2.y = 5
dobj2.__class__ = DOther
