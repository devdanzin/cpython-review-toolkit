"""Same descents, triggered from a thread with a small stack.

CPython's c_stack_soft_limit is derived from the ACTUAL thread stack, so a
*guarded* descent raises RecursionError here and an *unguarded* one segfaults.

argv: depth  which  stack_kb
"""
import sys
import threading

N = int(sys.argv[1])
which = sys.argv[2]
stack_kb = int(sys.argv[3]) if len(sys.argv) > 3 else 256

C = type("C0", (object,), {})
root = C
for i in range(1, N):
    C = type("C%d" % i, (C,), {})
leaf = C
print(f"built chain depth={N} leaf_mro={len(leaf.__mro__)}", flush=True)


def go():
    try:
        if which == "modified":
            root.x = 1
        elif which == "flags":
            root.__abstractmethods__ = frozenset()
        elif which == "dir":
            dir(leaf)
        elif which == "solid":
            type("X", (leaf,), {})
        elif which == "bases":
            root.__bases__ = (dict,)
        elif which == "issubclass":
            # guarded twin control: abstract_issubclass / type_is_subtype
            issubclass(leaf, dict)
        elif which == "repr":
            repr(leaf)
        elif which == "super":
            leaf.m = lambda self: None
            super(leaf, leaf()).m
        print("survived", flush=True)
    except RecursionError as e:
        print("RecursionError:", e, flush=True)


threading.stack_size(stack_kb * 1024)
t = threading.Thread(target=go)
t.start()
t.join()
print("done", flush=True)
