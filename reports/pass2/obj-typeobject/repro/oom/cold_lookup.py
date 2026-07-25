# COLD payload: type-attribute lookup cache + getattro/setattro
# (typeobject.c 6140-6452 and 6529-6848), plus super beyond construction.
# Every name is new, so each setattr is a real type-dict insertion and each
# getattr a real cache miss -> _PyType_LookupStackRefAndVersion fill.
for _nm in LOOKUP_NAMES:
    setattr(LLeaf, _nm, 1)
for _nm in LOOKUP_NAMES:
    _ = getattr(LLeaf, _nm)
    _ = getattr(LOBJ, _nm)
for _nm in LOOKUP_NAMES:
    delattr(LLeaf, _nm)

_s1 = super(LMid, LOBJ)
_v1 = _s1.m()
_v2 = LOBJ.m()
_v3 = repr(_s1)
_v4 = type.__getattribute__(LLeaf, "__mro__")
_v5 = LLeaf.__mro__
