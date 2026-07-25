"""Does `so`'s critical section actually hold across set_add_entry_takeref's
PyObject_RichCompareBool?  (setobject.c:288)

set_add_entry:335 asserts `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(so)` and
set.add is `@critical_section`-wrapped in Objects/clinic/setobject.c.h:404, so
the section IS entered.  But a critical section is released whenever the thread
detaches: Python/pystate.c:2321 `detach_thread` -> `_PyCriticalSection_SuspendAll`.
`PyObject_RichCompareBool` runs arbitrary Python, and arbitrary Python detaches
on any blocking call.

So: thread A calls s.add(Collide()); its __eq__ blocks on a threading.Event.
If `so`'s lock were really held for the duration, thread B could not mutate `s`.

Prints whether B got in.  Run on a free-threaded build.
"""

import threading
import time

s = set()


class Anchor:
    def __hash__(self):
        return 12345

    def __eq__(self, other):
        return self is other


anchor = Anchor()
s.add(anchor)

in_eq = threading.Event()
release = threading.Event()
b_mutated = threading.Event()


class Collide:
    def __hash__(self):
        return 12345  # collides with anchor -> forces RichCompareBool

    def __eq__(self, other):
        in_eq.set()
        release.wait(10)  # blocking call -> detach -> SuspendAll
        return False


def thread_a():
    s.add(Collide())


def thread_b():
    in_eq.wait(10)
    time.sleep(0.2)
    try:
        for i in range(300):  # enough to force set_table_resize
            s.add(i)
        b_mutated.set()
    except Exception as e:  # noqa: BLE001
        print("   B raised:", e)


ta = threading.Thread(target=thread_a)
tb = threading.Thread(target=thread_b)
ta.start()
tb.start()
in_eq.wait(10)
time.sleep(1.0)
mutated = b_mutated.is_set()
print("thread B mutated the set while A sits inside __eq__ under set.add:", mutated)
print("  -> so's critical section was", "SUSPENDED" if mutated else "held")
release.set()
ta.join(10)
tb.join(10)
print("final len(s) =", len(s))
