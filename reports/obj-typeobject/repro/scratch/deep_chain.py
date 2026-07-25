"""Probe: how deep a single-inheritance chain can we build, and does the
tp_subclasses / tp_bases descent in typeobject.c overflow at that depth?

argv[1] = depth, argv[2] = which descent to trigger
"""
import sys
import resource

N = int(sys.argv[1])
which = sys.argv[2] if len(sys.argv) > 2 else "modified"

sys.setrecursionlimit(100000)

C = type("C0", (object,), {})
root = C
for i in range(1, N):
    C = type("C%d" % i, (C,), {})
leaf = C
mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
print(f"built chain depth={N} leaf_mro={len(leaf.__mro__)} rss={mem:.0f}MB", flush=True)

if which == "modified":
    # setattr on the root -> _PyType_Modified_Unlocked recurses down tp_subclasses
    print("trigger: setattr on root -> _PyType_Modified_Unlocked", flush=True)
    root.x = 1
elif which == "flags":
    # _PyType_SetFlagsRecursive -> set_flags_recursive down tp_subclasses
    import abc
    print("trigger: set_flags_recursive", flush=True)
    root.__abstractmethods__ = frozenset()
elif which == "version":
    # assign_version_tag recurses UP tp_bases from the leaf
    print("trigger: assign_version_tag (up tp_bases)", flush=True)
    import _testinternalcapi
    print(_testinternalcapi.type_assign_version(leaf))
elif which == "solid":
    print("trigger: solid_base via new subclass", flush=True)
    type("X", (leaf,), {})
elif which == "dir":
    print("trigger: type.__dir__ -> merge_class_dict up __bases__", flush=True)
    print(len(dir(leaf)))
elif which == "bases":
    print("trigger: __bases__ reassign -> mro_hierarchy_for_complete_type", flush=True)
    root.__bases__ = (dict,)
elif which == "token":
    print("trigger: PyType_GetBaseByToken", flush=True)
    import _testcapi
    print(_testcapi.pytype_getbasebytoken(leaf, 0, 1, 1))

print("survived", flush=True)
