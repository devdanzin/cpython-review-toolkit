# Distinct from gh-151377's reproducer: no type_setattro at all.
# One thread creates subclasses; another thread grabs them out of
# Base.__subclasses__() -- where PyType_Ready() put them BEFORE
# fixup_slot_dispatchers() rewrote the slots -- and DISPATCHES through
# tp_new / tp_call / tp_repr while fixup is still writing them.
import threading

class Base:
    def __repr__(self): return "base"
    def __call__(self): return 1

STOP = False

def subclasser():
    for _ in range(20000):
        type('Sub', (Base,), {'__repr__': lambda s: 'sub',
                              '__call__': lambda s: 2,
                              '__init__': lambda s: None})

def dispatcher():
    while not STOP:
        for c in Base.__subclasses__():
            try:
                pass
                pass
                pass
            except Exception:
                pass

ts = [threading.Thread(target=subclasser) for _ in range(3)]
td = [threading.Thread(target=dispatcher) for _ in range(3)]
for t in td: t.daemon = True; t.start()
for t in ts: t.start()
for t in ts: t.join()
STOP = True
for t in td: t.join(timeout=5)
print("done")
