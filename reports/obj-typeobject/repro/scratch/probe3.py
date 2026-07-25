import gc, sys, _testcapi
# Does type_dealloc's watcher loop ever see a pending exception?
# Force the GC to run at maximal pressure while an exception unwinds.
unr = []
sys.unraisablehook = lambda a: unr.append((type(a.exc_value).__name__, str(a.exc_value),
                                           type(getattr(a.exc_value,'__context__',None)).__name__))
wid = _testcapi.add_type_watcher(1)
gc.set_threshold(1, 1, 1)          # collect as often as possible

def mk():
    T = type('T', (), {})
    _testcapi.watch_type(wid, T)
    return T

def sink(*a): pass

lost = clob = ok = 0
for i in range(3000):
    mk()                            # watched garbage type awaiting collection
    try:
        # arg evaluation allocates (GC pressure) then raises mid-expression,
        # so the value stack is unwound with ZeroDivisionError pending
        sink([object() for _ in range(20)], 1 // 0)
    except ZeroDivisionError:
        ok += 1
    except BaseException:
        clob += 1
    else:
        lost += 1
gc.collect()
ctxs = set(c for _, _, c in unr)
print(f"survived={ok} clobbered={clob} lost={lost} unraisable_events={len(unr)} contexts={ctxs}")
