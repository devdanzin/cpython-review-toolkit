"""Concurrency smoke over every stop-the-world path in Objects/typeobject.c.
Looking for deadlocks / hangs / aborts, not for correctness."""
import collections.abc as cabc
import sys, threading, time, faulthandler

faulthandler.dump_traceback_later(40, exit=True)
STOP = False

def make_tree(tag, n=8):
    class R:
        def __len__(self): return 0
        def __getitem__(self, i): raise IndexError
    R.__name__ = f"R{tag}"
    kids = [type(f"K{tag}_{i}", (R,), {}) for i in range(n)]
    return R, kids

def w_setname():                       # type_set_name  (typeobject.c:1572)
    class C: pass
    i = 0
    while not STOP:
        i += 1
        C.__name__ = f"n{i}"
        C.__qualname__ = f"q{i}"       # type_set_qualname (1598)

def w_setbases():                      # type_set_bases_unlocked (1939 / 2005 / 669)
    class A: pass
    class B: pass
    class C(A): pass
    while not STOP:
        C.__bases__ = (B,)
        C.__bases__ = (A,)

def w_abstract():                      # type_set_abstractmethods (1745)
    class C: pass
    while not STOP:
        C.__abstractmethods__ = frozenset()
        del C.__abstractmethods__

def w_slotupdate():                    # apply_type_slot_updates (3923)
    class C: pass
    while not STOP:
        C.__len__ = lambda self: 1
        del C.__len__

def w_setflags():                      # _PyType_SetFlagsRecursive (6522)
    t = 0
    while not STOP:
        t += 1
        R, kids = make_tree(f"{threading.get_ident()}_{t}")
        cabc.Sequence.register(R)

def w_setclass():                      # object_set_class (7823)
    class A: pass
    class B: pass
    o = A()
    holder = [o]                       # keep refcount > 1 so STW is taken
    while not STOP:
        o.__class__ = B
        o.__class__ = A

workers = [w_setname, w_setbases, w_abstract, w_slotupdate, w_setflags,
           w_setclass]
threads = [threading.Thread(target=w, daemon=True)
           for w in workers for _ in range(3)]
for t in threads:
    t.start()
time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 6.0)
STOP = True
for t in threads:
    t.join(timeout=15)
alive = [t for t in threads if t.is_alive()]
print("still alive (hung):", len(alive), file=sys.stderr)
sys.exit(1 if alive else 0)
