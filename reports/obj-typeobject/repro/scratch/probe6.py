import sys
unr = []
sys.unraisablehook = lambda a: unr.append((type(a.exc_value).__name__, str(a.exc_value)))

class RaisingGet:
    def __init__(self, exc): self.exc = exc
    def __get__(self, obj, objtype=None): raise self.exc

# A: special-method LOOKUP fails inside type_new_set_names (__set_name__ is a raising descriptor)
def A(exc):
    unr.clear()
    try:
        class D:
            __set_name__ = RaisingGet(exc)
        class C: x = D()
    except BaseException as e:
        return f"A set_name-lookup : PROPAGATED {type(e).__name__}({e})  unraisable={len(unr)}"
    return f"A set_name-lookup : *** SWALLOWED ***  unraisable={len(unr)}"

# B: special-method LOOKUP fails inside slot_tp_finalize (__del__ is a raising descriptor)
def B(exc):
    unr.clear()
    class C:
        __del__ = RaisingGet(exc)
    c = C()
    try:
        del c
    except BaseException as e:
        return f"B del-lookup      : PROPAGATED {type(e).__name__}({e})  unraisable={len(unr)}"
    return f"B del-lookup      : *** SWALLOWED ***  unraisable={len(unr)}"

for exc in (KeyboardInterrupt("ctrl-c"), MemoryError("oom")):
    print(A(exc)); print(B(exc))
