"""Confirm the `del obj.attr` route is structurally closed in typeobject.c."""
import sys

su = super.__new__(super)

# 1. super's three members are Py_READONLY -> del must be rejected.
for name in ("__thisclass__", "__self__", "__self_class__"):
    try:
        delattr(su, name)
        print("DEL-OK   super.%s  <-- UNEXPECTED" % name)
    except Exception as e:
        print("DEL-BLK  super.%-16s %s: %s" % (name, type(e).__name__, e))

# 2. type's PyMemberDef entries are all Py_READONLY.
class C: pass
for name in ("__basicsize__", "__itemsize__", "__flags__",
             "__weakrefoffset__", "__base__", "__dictoffset__"):
    try:
        delattr(C, name)
        print("DEL-OK   type.%s  <-- UNEXPECTED" % name)
    except Exception as e:
        print("DEL-BLK  type.%-17s %s: %s" % (name, type(e).__name__, e))

# 3. type's getset setters: which accept deletion?
for name in ("__name__", "__qualname__", "__bases__", "__module__",
             "__abstractmethods__", "__doc__", "__annotations__",
             "__annotate__", "__type_params__"):
    class D: pass
    try:
        delattr(D, name)
        print("DEL-OK   type.%-17s (setter accepts NULL)" % name)
    except Exception as e:
        print("DEL-BLK  type.%-17s %s: %s" % (name, type(e).__name__, e))

# 4. object.__class__ deletion
o = C()
try:
    del o.__class__
    print("DEL-OK   object.__class__  <-- UNEXPECTED")
except Exception as e:
    print("DEL-BLK  object.__class__     %s: %s" % (type(e).__name__, e))

# 5. after del C.__abstractmethods__ / __annotations__, exercise the type hard
class E: pass
E.__abstractmethods__ = frozenset({"z"})
del E.__abstractmethods__
print("post-del abstractmethods: ", repr(E), E.__mro__, E(), E.__flags__ & (1 << 20))
class F: pass
F.__annotations__ = {"a": int}
del F.__annotations__
print("post-del annotations:     ", repr(F), F.__annotations__, F())
