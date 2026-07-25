"""has_dunder_getitem() discards lookup_maybe_method()'s -1; slot_tp_iter then
clobbers the still-pending exception with a TypeError."""

class RaisingGet:
    def __init__(self, exc):
        self.exc = exc
    def __get__(self, obj, objtype=None):
        raise self.exc

class C:
    __iter__ = RaisingGet(AttributeError("no __iter__ here"))
    __getitem__ = RaisingGet(KeyboardInterrupt("EXC-FROM-__getitem__.__get__"))

print("tp_iter installed?", type(C).__iter__ if False else "n/a")
try:
    it = iter(C())
    print("RESULT: iter() returned", it)
except BaseException as e:
    print("RESULT:", type(e).__name__, ":", e)
    ctx = e.__context__
    print("        __context__:", type(ctx).__name__ if ctx else None,
          ":", ctx)

print()
print("--- control: plain attribute access propagates ---")
try:
    C().__getitem__
except BaseException as e:
    print("        ", type(e).__name__, ":", e)
