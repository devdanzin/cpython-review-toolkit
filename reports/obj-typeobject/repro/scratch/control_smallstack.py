"""Control: in a 128 KB thread, does CPython's *guarded* machinery still raise
RecursionError rather than segfault? If yes, a segfault from an unguarded
typeobject.c descent in the same thread is a missing guard, not "stack too small".
"""
import sys
import threading

stack_kb = int(sys.argv[1]) if len(sys.argv) > 1 else 128


def go():
    # control A: plain Python recursion (guarded in the eval loop)
    def f(n):
        return f(n + 1)
    try:
        f(0)
    except RecursionError as e:
        print("A python-recursion  -> RecursionError:", e, flush=True)

    # control B: abstract_issubclass on a cyclic __bases__ (guarded, abstract.c:2571)
    class Fake:
        pass
    a, b = Fake(), Fake()
    a.__bases__ = (a, b)
    b.__bases__ = ()
    try:
        issubclass(a, int)
    except RecursionError as e:
        print("B abstract_issubclass -> RecursionError:", e, flush=True)

    # control C: repr of a deeply nested list (guarded by Py_ReprEnter)
    x = []
    for _ in range(2000):
        x = [x]
    try:
        repr(x)
        print("C nested repr -> ok", flush=True)
    except RecursionError as e:
        print("C nested repr -> RecursionError:", e, flush=True)


threading.stack_size(stack_kb * 1024)
t = threading.Thread(target=go)
t.start()
t.join()
print("controls done, no crash", flush=True)
