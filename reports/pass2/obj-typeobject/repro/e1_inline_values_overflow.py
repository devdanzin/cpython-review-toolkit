#!/usr/bin/env python3
"""P2-F17 reproducer, used here as the E1 ASan-blindness probe.

`type_ready` links a new heap type into `Base.__subclasses__()` in
`type_ready_add_subclasses` BEFORE `type_ready_managed_dict` sets
`Py_TPFLAGS_INLINE_VALUES` (typeobject.c:9487).  A concurrent thread that
grabs the half-ready type out of `Base.__subclasses__()` and instantiates it
reads `tp_flags` twice inside `_PyType_AllocNoTrack`:

    :2524  if (type->tp_flags & Py_TPFLAGS_INLINE_VALUES)  -> sizes the alloc
    :2528  alloc = _PyObject_MallocWithType(type, size + presize)
    :2550  if (type->tp_flags & Py_TPFLAGS_INLINE_VALUES)  -> initializes
    :2551  _PyObject_InitInlineValues(obj, type)

If the flag flips between :2524 and :2550 the object is allocated WITHOUT the
inline-values region and then initialized WITH it -> heap-buffer-overflow
WRITE, landing 0 bytes after the region.

Run:
    PYTHON_GIL=0 <build>/python e1_inline_values_overflow.py [seconds]
"""
import sys
import threading
import time

DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0

stop = threading.Event()
made = [0]
inst = [0]
lock = threading.Lock()


def maker():
    """Repeatedly publish a fresh half-ready subclass."""
    n = 0
    while not stop.is_set():
        base = type("B%d" % n, (object,), {})
        holder.append(base)
        # The class statement is the publish-before-flag window.
        for i in range(8):
            type("S%d_%d" % (n, i), (base,), {"__init__": _init})
        n += 1
        with lock:
            made[0] += n
        if len(holder) > 64:
            del holder[:32]


def _init(self):
    self.a0 = 1
    self.a1 = 2
    self.a2 = 3
    self.a3 = 4
    self.a4 = 5


def racer():
    """Grab whatever subclasses exist and instantiate them."""
    k = 0
    while not stop.is_set():
        try:
            bases = holder[-4:]
        except Exception:
            continue
        for b in bases:
            try:
                for sub in b.__subclasses__():
                    o = sub()
                    o.b0 = 1
                    o.b1 = 2
                    o.b2 = 3
                    k += 1
            except Exception:
                pass
        if k > 4096:
            with lock:
                inst[0] += k
            k = 0


holder = []

if __name__ == "__main__":
    print("E1/P2-F17 probe: %s" % sys.executable, flush=True)
    threads = [threading.Thread(target=maker, daemon=True) for _ in range(2)]
    threads += [threading.Thread(target=racer, daemon=True) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(DURATION)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    print("survived: types=%d instances=%d" % (made[0], inst[0]), flush=True)
