"""Isolate update_subclasses/recurse_down_subclasses and
mro_hierarchy_for_complete_type from _PyType_Modified_Unlocked.

_PyType_Modified_Unlocked returns immediately when tp_version_tag == 0, so we
first drop every version tag in the chain (on the big main-thread stack), then
trigger the descent under test in a small-stack thread.

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
root = chain[0]
alt = type("ALT", (object,), {})

# Drop every version tag, leaf -> root, so no single call recurses deeply.
for c in reversed(chain):
    c.q0 = 1
print(f"built chain depth={N}, version tags cleared", flush=True)


def go():
    try:
        if which == "slot":
            root.__len__ = lambda self: 0        # -> update_slot -> update_subclasses
        elif which == "mrohier":
            root.__bases__ = (alt,)              # -> mro_hierarchy_for_complete_type
        print("survived", flush=True)
    except RecursionError as e:
        print("RecursionError:", e, flush=True)
    except TypeError as e:
        print("TypeError:", e, flush=True)


threading.stack_size(stack_kb * 1024)
t = threading.Thread(target=go)
t.start()
t.join()
print("done", flush=True)
