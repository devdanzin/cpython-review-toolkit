import gc, sys, _testcapi

unraisables = []
sys.unraisablehook = lambda a: unraisables.append((type(a.exc_value).__name__,
                                                   str(a.exc_value),
                                                   type(getattr(a.exc_value,'__context__',None)).__name__))

wid = _testcapi.add_type_watcher(1)   # type_modified_callback_error -> RuntimeError("boom!")

def mk():
    T = type('T', (), {})
    _testcapi.watch_type(wid, T)
    return T

# Scenario C: gc.collect() inside __del__ that runs during unwinding
class Trigger:
    def __del__(self):
        gc.collect()

def scenC():
    unraisables.clear()
    mk()                     # becomes garbage, watched
    t = Trigger()
    try:
        del t
        raise ValueError("OUTER-C")
    except ValueError as e:
        return ("survived", str(e), list(unraisables))
    except BaseException as e:
        return ("CLOBBERED", repr(e), list(unraisables))
    return ("LOST", None, list(unraisables))

print("C:", scenC())

# Scenario D: gc.collect() while exception pending via weakref callback
import weakref
def scenD():
    unraisables.clear()
    mk()
    holder = [1]
    def cb(ref):
        gc.collect()
    class Obj: pass
    o = Obj()
    r = weakref.ref(o, cb)
    try:
        del o
        raise ValueError("OUTER-D")
    except ValueError as e:
        return ("survived", str(e), list(unraisables))
    except BaseException as e:
        return ("CLOBBERED", repr(e), list(unraisables))
print("D:", scenD())

# Scenario E: plain gc.collect(), no pending exception -- baseline for the watcher firing at all
unraisables.clear()
mk()
gc.collect()
print("E baseline unraisables:", unraisables)
