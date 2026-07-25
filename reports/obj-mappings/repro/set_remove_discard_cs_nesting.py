"""CPY-0133: the slice's only two-object critical-section NESTING, and it is
invisible from the .c file.

Objects/clinic/setobject.c.h wraps set.remove / set.discard in
Py_BEGIN_CRITICAL_SECTION(so) (clinic `@critical_section`).  Inside that
section, setobject.c:2662 and :2702 open a SECOND critical section on a
DIFFERENT object:

    rv = set_discard_key(so, key);
    if (rv < 0) {
        if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError))
            return NULL;
        PyErr_Clear();
        Py_hash_t hash;
        Py_BEGIN_CRITICAL_SECTION(key);        /* :2662  (set.remove)  */
        hash = frozenset_hash_impl(key);       /* :2702  (set.discard) */
        Py_END_CRITICAL_SECTION();
        rv = set_discard_entry(so, key, hash);

Acquisition order is so -> key: NOT address-ordered.  Every other two-object
lock in the file uses the address-ordered Py_BEGIN_CRITICAL_SECTION2
(set_issubset / set_issuperset / set_isdisjoint / set_symmetric_difference).

It is not a deadlock -- Py_BEGIN_CRITICAL_SECTION detaches the thread when the
inner mutex is contended, which SUSPENDS the outer section on `so`.  What is
lost is ATOMICITY: `so` becomes unlocked in the middle of set.remove().

This probe shows a third thread mutating `so` while thread A is parked
acquiring `key`'s lock inside set.remove(so, key).  Free-threaded build.
"""

import sys
import threading
import time

if not hasattr(sys, "_is_gil_enabled") or sys._is_gil_enabled():
    print("NOTE: GIL build -- critical sections are no-ops; run on a FT build")

s = {frozenset({1, 2, 3})}
key = {1, 2, 3}          # a *set*: unhashable, so set_discard_key raises
                         # TypeError and we take the :2662/:2702 branch

holder_in = threading.Event()
holder_release = threading.Event()
a_started = threading.Event()
mutated_during = threading.Event()


class SlowHash:
    """Its __hash__ parks while `key`'s critical section is held."""

    def __hash__(self):
        holder_in.set()
        holder_release.wait(5.0)
        return 424242

    def __eq__(self, other):
        return self is other


def holder():
    # set.add is @critical_section(so) -> holds key's per-object lock across
    # PyObject_Hash(SlowHash()), which parks.
    key.add(SlowHash())


def remover():
    a_started.set()
    try:
        s.remove(key)          # -> setobject.c:2656 fails, then :2662 blocks
    except KeyError:
        pass
    except TypeError:
        pass


def mutator():
    # If so's critical section really held for the duration of set.remove,
    # this could not get in while A is parked on key's lock.
    deadline = time.time() + 4.0
    while time.time() < deadline:
        if holder_in.is_set() and a_started.is_set():
            before = len(s)
            for i in range(500, 700):
                s.add(frozenset({i}))
            if len(s) != before:
                mutated_during.set()
            return
        time.sleep(0.001)


th = threading.Thread(target=holder)
tr = threading.Thread(target=remover)
tm = threading.Thread(target=mutator)

th.start()
holder_in.wait(5.0)
tr.start()
time.sleep(0.2)
tm.start()
tm.join()
holder_release.set()
th.join()
tr.join()

print("thread A parked acquiring key's lock inside set.remove:", a_started.is_set())
print("thread C mutated `so` during that window:", mutated_during.is_set())
print("  -> so's critical section was SUSPENDED" if mutated_during.is_set()
      else "  -> so stayed locked (or the window was missed)")
print("final len(s) =", len(s))
print("DONE")
