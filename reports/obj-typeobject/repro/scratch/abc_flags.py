"""set_flags_recursive (Objects/typeobject.c:6500) via
collections.abc.Sequence.register() -> _abc_register -> _PyType_SetFlagsRecursive.
"""
import sys
import threading
import collections.abc

N = int(sys.argv[1])
stack_kb = int(sys.argv[2]) if len(sys.argv) > 2 else 128

chain = [type("C0", (object,), {})]
for i in range(1, N):
    chain.append(type("C%d" % i, (chain[-1],), {}))
root = chain[0]
print(f"built chain depth={N}", flush=True)


def go():
    try:
        collections.abc.Sequence.register(root)
        print("survived; leaf flags propagated:",
              bool(chain[-1].__flags__ & (1 << 5)), flush=True)
    except RecursionError as e:
        print("RecursionError:", e, flush=True)


threading.stack_size(stack_kb * 1024)
t = threading.Thread(target=go)
t.start()
t.join()
print("done", flush=True)
