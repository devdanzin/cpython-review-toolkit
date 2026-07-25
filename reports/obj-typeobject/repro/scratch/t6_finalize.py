"""slot_tp_finalize: an exception from __del__.__get__ vanishes with no
unraisable report, while an exception from __del__() itself IS reported."""
import sys

reports = []
def hook(unraisable):
    reports.append((type(unraisable.exc_value).__name__, str(unraisable.exc_value)))
sys.unraisablehook = hook

class RaisingGet:
    def __get__(self, obj, objtype=None):
        raise KeyboardInterrupt("EXC-FROM-__del__.__get__")

class A:
    __del__ = RaisingGet()

class B:
    def __del__(self):
        raise KeyboardInterrupt("EXC-FROM-__del__-BODY")

a = A(); del a
print("after A teardown, unraisable reports:", reports)
reports.clear()
b = B(); del b
print("after B teardown, unraisable reports:", reports)
