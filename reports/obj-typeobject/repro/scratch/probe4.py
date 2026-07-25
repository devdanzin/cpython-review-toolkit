import sys
unr = []
sys.unraisablehook = lambda a: unr.append((type(a.exc_value).__name__, str(a.exc_value)))

# A descriptor used as __del__ whose __get__ raises a NON-AttributeError.
class RaisingGet:
    def __init__(self, exc): self.exc = exc
    def __get__(self, obj, objtype=None):
        raise self.exc

def trial(exc, label):
    unr.clear()
    class C:
        __del__ = RaisingGet(exc)
    c = C()
    del c                       # -> slot_tp_finalize -> lookup fails -> restore-away
    print(f"{label:18} unraisable_reports={len(unr)} {unr}")

trial(KeyboardInterrupt("CTRL-C"), "KeyboardInterrupt")
trial(MemoryError("oom"),          "MemoryError")
trial(SystemExit(3),               "SystemExit")

# Control: the BODY of __del__ raising IS reported (the guarded five-lines-above case)
class D:
    def __del__(self): raise KeyboardInterrupt("from-body")
unr.clear()
d = D(); del d
print(f"{'body-raise CONTROL':18} unraisable_reports={len(unr)} {unr}")

# Does the OUTER saved exception still survive? (the save/restore itself is fine)
def outer():
    class E:
        __del__ = RaisingGet(MemoryError("swallowed"))
    e = E()
    try:
        del e
        raise ValueError("OUTER")
    except ValueError as ex:
        return "outer survived: " + str(ex)
print(outer())
