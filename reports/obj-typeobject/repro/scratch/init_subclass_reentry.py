"""type_new re-entered from __init_subclass__ / __set_name__ — is it bounded?"""
import sys
import threading

mode = sys.argv[1]
stack_kb = int(sys.argv[2]) if len(sys.argv) > 2 else 128


def go():
    if mode == "init_subclass":
        class Base:
            def __init_subclass__(cls, **kw):
                type(cls.__name__ + "x", (cls,), {})
        try:
            type("A", (Base,), {})
            print("survived", flush=True)
        except RecursionError as e:
            print("RecursionError:", e, flush=True)
    elif mode == "set_name":
        class D:
            def __set_name__(self, owner, name):
                type("Q", (object,), {"d": D()})
        try:
            type("A", (object,), {"d": D()})
            print("survived", flush=True)
        except RecursionError as e:
            print("RecursionError:", e, flush=True)


threading.stack_size(stack_kb * 1024)
t = threading.Thread(target=go)
t.start()
t.join()
print("done", flush=True)
