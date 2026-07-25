"""Remaining typeobject.c hierarchy descents: update_subclasses/recurse_down_subclasses
and mro_hierarchy_for_complete_type.

argv: depth which [stack_kb]
"""
import sys
import threading

N = int(sys.argv[1])
which = sys.argv[2]
stack_kb = int(sys.argv[3]) if len(sys.argv) > 3 else 128

chain = [type("C0", (object,), {})]
for i in range(1, N):
    chain.append(type("C%d" % i, (chain[-1],), {}))
root, leaf = chain[0], chain[-1]
for c in chain:
    getattr(c, "zzz", None)          # prime version tags bottom-up
alt = type("ALT", (object,), {})     # a layout-compatible alternative base
print(f"built chain depth={N}", flush=True)


def go():
    try:
        if which == "slot":
            # type_setattro on a SLOT name -> update_slot -> update_subclasses
            #                              -> recurse_down_subclasses (mutual recursion)
            root.__len__ = lambda self: 0
        elif which == "mrohier":
            # __bases__ reassignment -> mro_hierarchy_for_complete_type down subclasses
            root.__bases__ = (alt,)
        print("survived", flush=True)
    except RecursionError as e:
        print("RecursionError:", e, flush=True)
    except TypeError as e:
        print("TypeError:", e, flush=True)


if stack_kb == 0:
    go()
else:
    threading.stack_size(stack_kb * 1024)
    t = threading.Thread(target=go)
    t.start()
    t.join()
print("done", flush=True)
