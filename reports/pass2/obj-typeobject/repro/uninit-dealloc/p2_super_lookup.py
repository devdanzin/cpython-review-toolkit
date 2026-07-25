# PASS-2 payload D: super beyond construction + the type-attribute lookup cache
# and getattro/setattro (typeobject.c 6140-6848, 12700-13068).
s1 = super(SuperMid, sleaf)
v1 = s1.m()
v2 = sleaf.m()
s2 = super(SuperLeaf, SuperLeaf)
v3 = repr(s1)
LEAF.newattr = 1
del LEAF.newattr
v4 = getattr(LEAF, "v")
v5 = type.__getattribute__(LEAF, "__mro__")
setattr(SuperLeaf, "zz", 3)
del SuperLeaf.zz
