"""Free-threaded consequence: nested types_stop_world() from inside the
user __eq__ that object_set_class runs while the world is already stopped.

`o.__class__ = B` (o refcount > 1) does types_stop_world(), then reaches
same_slots_added -> PyObject_RichCompareBool -> S.__eq__, and S.__eq__ does
another `o2.__class__ = ...`, whose object_set_class calls types_stop_world()
a SECOND time with the world already stopped.
"""
import sys, threading, time

depth = 0


class S(str):
    def __eq__(self, other):
        global depth
        if depth == 0:
            depth = 1
            print("  [nested __class__ assignment -> second types_stop_world()]", flush=True)
            o2.__class__ = C
            print("  [nested returned]", flush=True)
        return True

    def __hash__(self):
        return str.__hash__(self)


A = type("A", (), {"__slots__": (S("x"),)})
B = type("B", (), {"__slots__": (S("x"),)})
C = type("C", (), {"__slots__": (S("x"),)})

o = A()
keep_o = o
o2 = A()
keep_o2 = o2

stop = False


def spin():
    n = 0
    while not stop:
        n += 1
    return n


t = threading.Thread(target=spin, daemon=True)
t.start()
time.sleep(0.2)

print("gil disabled:", getattr(sys, "_is_gil_enabled", lambda: True)() is False, flush=True)
print("assigning ...", flush=True)
o.__class__ = B
print("done, types =", type(o).__name__, type(o2).__name__, flush=True)
stop = True
t.join(5)
print("OK-END", flush=True)
