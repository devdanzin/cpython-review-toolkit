"""Descents in Objects/typeobject.c over a deep single-inheritance chain.

argv: depth  which  [stack_kb]   (stack_kb=0 => run on the main thread)

Version tags are primed BOTTOM-UP so that assign_version_tag's own recursion
does not fire, isolating whichever descent we are testing.
"""
import sys
import threading

N = int(sys.argv[1])
which = sys.argv[2]
stack_kb = int(sys.argv[3]) if len(sys.argv) > 3 else 256

chain = [type("C0", (object,), {})]
for i in range(1, N):
    chain.append(type("C%d" % i, (chain[-1],), {}))
root, leaf = chain[0], chain[-1]

if which != "version":
    # bottom-up priming: each assign_version_tag call recurses only one level
    for c in chain:
        getattr(c, "zzz", None)
print(f"built chain depth={N} leaf_mro={len(leaf.__mro__)} primed={which != 'version'}",
      flush=True)


def go():
    try:
        if which == "modified":
            # _PyType_Modified_Unlocked: down tp_subclasses
            root.qq = 1
        elif which == "version":
            # assign_version_tag: up tp_bases
            getattr(leaf, "zzz", None)
        elif which == "flags":
            set(chain)  # keep alive
            root.__abstractmethods__ = frozenset()
        elif which == "dir":
            dir(leaf)
        elif which == "solid":
            type("X", (leaf,), {})
        elif which == "issubclass":
            issubclass(leaf, dict)   # guarded-twin control
        elif which == "isinstance":
            isinstance(leaf(), dict)  # guarded-twin control
        print("survived", flush=True)
    except RecursionError as e:
        print("RecursionError:", e, flush=True)


if stack_kb == 0:
    go()
else:
    threading.stack_size(stack_kb * 1024)
    t = threading.Thread(target=go)
    t.start()
    t.join()
print("done", flush=True)
