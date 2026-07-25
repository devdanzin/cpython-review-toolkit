"""Sweep every super() method against an __init__-bypassed super object.

Each probe runs in-process; we print PROBE/OK/EXC lines so a segfault shows up
as a truncated log with the last PROBE naming the crashing operation.
Run one probe per process with `python super_sweep.py <n>` to isolate crashes.
"""
import sys

class S(super):
    pass

def mk_direct():
    return super.__new__(super)

def mk_subclass_new():
    return S.__new__(S)

class S2(super):
    def __init__(self, *a, **k):
        pass  # forgets super().__init__()

def mk_subclass_init():
    return S2(int, 1)

PROBES = []
def probe(name):
    def deco(f):
        PROBES.append((name, f))
        return f
    return deco

# ---- direct bypass: super.__new__(super) --------------------------------
@probe("direct: repr()")
def _(): return repr(mk_direct())

@probe("direct: str()")
def _(): return str(mk_direct())

@probe("direct: .__thisclass__")
def _(): return mk_direct().__thisclass__

@probe("direct: .__self__")
def _(): return mk_direct().__self__

@probe("direct: .__self_class__")
def _(): return mk_direct().__self_class__

@probe("direct: .anything (getattro)")
def _():
    try:
        return mk_direct().nosuchattr
    except AttributeError as e:
        return "AttributeError: %s" % e

@probe("direct: .__class__")
def _(): return mk_direct().__class__

@probe("direct: __get__(1)  [CPY-0007]")
def _(): return mk_direct().__get__(1)

@probe("direct: __get__(None)")
def _(): return mk_direct().__get__(None)

@probe("direct: __get__(1, int)")
def _(): return mk_direct().__get__(1, int)

@probe("direct: as class attribute descriptor")
def _():
    class C:
        d = mk_direct()
    return C().d

@probe("direct: gc.collect after alloc")
def _():
    import gc
    x = mk_direct()
    gc.collect()
    del x
    gc.collect()
    return "ok"

@probe("direct: dir()")
def _(): return len(dir(mk_direct()))

@probe("direct: hash()")
def _(): return hash(mk_direct()) is not None

@probe("direct: == ")
def _(): return mk_direct() == mk_direct()

@probe("direct: copy.copy")
def _():
    import copy
    try:
        return copy.copy(mk_direct())
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)

@probe("direct: pickle")
def _():
    import pickle
    try:
        return pickle.dumps(mk_direct())
    except Exception as e:
        return "%s: %s" % (type(e).__name__, e)

# ---- subclass bypass: S.__new__(S) --------------------------------------
@probe("subclass-new: repr()")
def _(): return repr(mk_subclass_new())

@probe("subclass-new: __get__(1)")
def _(): return mk_subclass_new().__get__(1)

@probe("subclass-new: .attr")
def _():
    try:
        return mk_subclass_new().nosuchattr
    except AttributeError as e:
        return "AttributeError: %s" % e

# ---- subclass whose __init__ skips super().__init__() -------------------
@probe("subclass-init: repr()")
def _(): return repr(mk_subclass_init())

@probe("subclass-init: __get__(1)")
def _(): return mk_subclass_init().__get__(1)

@probe("subclass-init: .attr")
def _():
    try:
        return mk_subclass_init().nosuchattr
    except AttributeError as e:
        return "AttributeError: %s" % e

@probe("subclass-init: as descriptor on a class")
def _():
    class C:
        d = mk_subclass_init()
    return C().d


def main():
    if len(sys.argv) > 1:
        idx = int(sys.argv[1])
        name, f = PROBES[idx]
        print("PROBE %d %s" % (idx, name), flush=True)
        try:
            r = f()
            print("  OK -> %.120r" % (r,), flush=True)
        except BaseException as e:
            print("  EXC %s: %s" % (type(e).__name__, e), flush=True)
        return
    print("n_probes=%d" % len(PROBES))
    for i, (name, _f) in enumerate(PROBES):
        print("%d\t%s" % (i, name))

main()
