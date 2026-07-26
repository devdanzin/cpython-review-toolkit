#!/usr/bin/env python3
"""lock-discipline-checker, slice obj-sequences, task (c).

Does the slice's *nested* two-object critical-section locking deadlock?

Three sites in the slice take a second object's per-object lock while already
holding the first object's lock, using two SINGLE-object begins rather than
Py_BEGIN_CRITICAL_SECTION2:

  N1  bytearray_iconcat        Objects/bytearrayobject.c:382  CS(b1)
        -> bytearray_iconcat_lock_held:356  PyObject_GetBuffer(other)
        -> bytearray_getbuffer Objects/bytearrayobject.c:77    CS(b2)
      Python:  b1 += b2        (both bytearrays)

  N2  _list_extend `else` arm  Objects/listobject.c:1506       CS(l1)
        -> list_extend_iter_lock_held:1318  iternext(it)
        -> listiter_next -> list_get_item_ref:339               CS(l2)
      Python:  l1.extend(iter(l2))     (an ITERATOR, so the CS2 arms are skipped)

  N3  PyByteArray_Concat        Objects/bytearrayobject.c:304  -- no lock at all,
      two sequential PyObject_GetBuffer calls, each taking its own CS.

The guarded twins are in the same two files:
  bytearray_ass_subscript:893   Py_BEGIN_CRITICAL_SECTION2(op, values)
  bytearray_mod:2865            Py_BEGIN_CRITICAL_SECTION2(v, w)
  _PyList_Concat:813            Py_BEGIN_CRITICAL_SECTION2(a, b)
  list_richcompare:3504         Py_BEGIN_CRITICAL_SECTION2(v, w)
  list_ass_subscript:3901       Py_BEGIN_CRITICAL_SECTION2(self, value)
  _list_extend:1468..1501       Py_BEGIN_CRITICAL_SECTION2 x6

Run one scenario per subprocess.  A hang is a valid result: faulthandler dumps
every thread's Python frame after DUMP_AFTER seconds so the *location* is
evidence rather than the conclusion.

Usage:  python lock_order_inversion.py <scenario>
"""

import faulthandler
import os
import sys
import threading
import time

DURATION = float(os.environ.get("LOI_DURATION", "6.0"))
DUMP_AFTER = float(os.environ.get("LOI_DUMP_AFTER", "20.0"))
NTHREADS = int(os.environ.get("LOI_THREADS", "4"))

faulthandler.enable()
faulthandler.dump_traceback_later(DUMP_AFTER, exit=True)

stop = threading.Event()
counts = {}
errors = []


def _run(name, fn):
    n = 0
    try:
        while not stop.is_set():
            fn()
            n += 1
    except BaseException as exc:  # noqa: BLE001
        errors.append(f"{name}: {type(exc).__name__}: {exc}")
    counts[name] = counts.get(name, 0) + n


def drive(pairs):
    """pairs: list of (name, callable)."""
    threads = []
    for i in range(NTHREADS):
        name, fn = pairs[i % len(pairs)]
        t = threading.Thread(target=_run, args=(f"{name}-{i}", fn), name=f"{name}-{i}")
        threads.append(t)
    t0 = time.monotonic()
    for t in threads:
        t.start()
    time.sleep(DURATION)
    stop.set()
    for t in threads:
        t.join(timeout=30.0)
    alive = [t.name for t in threads if t.is_alive()]
    dt = time.monotonic() - t0
    print(f"PROBE:elapsed={dt:.3f}")
    print(f"PROBE:iterations={sum(counts.values())}")
    print(f"PROBE:threads_alive_after_join={alive}")
    for e in errors[:5]:
        print(f"PROBE:error={e}")
    if alive:
        print("PROBE:result=HUNG")
        faulthandler.dump_traceback()
        return 1
    print("PROBE:result=COMPLETED")
    return 0


# ---------------------------------------------------------------- scenarios


def trim(seq):
    """Bound growth.  Unconditional: `del seq[64:]` is itself a single-object
    critical section on `seq`, so it adds no new lock order."""
    del seq[64:]


def s_ba_iconcat_inversion():
    """N1 both directions: CS(b1)->CS(b2) racing CS(b2)->CS(b1).

    operator.iadd, not `b1 += b2`: the statement form rebinds the closure local
    and raises UnboundLocalError before reaching sq_inplace_concat at all.
    """
    import operator

    b1 = bytearray(b"A" * 64)
    b2 = bytearray(b"B" * 64)

    def fwd():
        operator.iadd(b1, b2)   # bytearray_iconcat -> CS(b1) -> getbuffer CS(b2)
        trim(b1)

    def rev():
        operator.iadd(b2, b1)
        trim(b2)

    return drive([("fwd", fwd), ("rev", rev)])


def s_list_extend_inversion():
    """N2 both directions: CS(l1)->CS(l2) racing CS(l2)->CS(l1)."""
    l1 = list(range(64))
    l2 = list(range(64, 128))

    def fwd():
        l1.extend(iter(l2))
        trim(l1)

    def rev():
        l2.extend(iter(l1))
        trim(l2)

    return drive([("fwd", fwd), ("rev", rev)])


def s_cs2_vs_nested():
    """A CS2 acquirer racing a nested single-lock acquirer on the same pair."""
    l1 = list(range(64))
    l2 = list(range(64, 128))

    def cs2():
        l1[0:0] = l2          # list_ass_subscript -> CS2(l1, l2)
        trim(l1)

    def nested():
        l2.extend(iter(l1))   # CS(l2) -> CS(l1)
        trim(l2)

    return drive([("cs2", cs2), ("nested", nested)])


def s_ba_cs2_vs_nested():
    """bytearray CS2 (ass_subscript) racing nested iconcat."""
    b1 = bytearray(b"A" * 64)
    b2 = bytearray(b"B" * 64)

    def cs2():
        b1[0:0] = b2          # bytearray_ass_subscript -> CS2(b1, b2)
        trim(b1)

    def nested():
        b2 += b1              # CS(b2) -> CS(b1) via getbuffer
        trim(b2)

    return drive([("cs2", cs2), ("nested", nested)])


def s_self_alias():
    """Same object on both sides: recursive acquire of one mutex.

    Every op here DOUBLES its target, so the reset is a hard truncation, not a
    conditional trim -- the first version of this probe grew without bound and
    was OOM-killed (rc=-9) on release-gil, which is a harness artefact and not
    a lock result.
    """
    import operator

    b = bytearray(b"A" * 64)
    l = list(range(64))

    def ba():
        # CS(b) -> CS(b) via getbuffer.  Raises BufferError: the getbuffer
        # bumps ob_exports on the SAME object, so _canresize refuses.  That is
        # pre-existing CPython behaviour (RustPython mirrors it) -- swallowed
        # here because the point of the probe is the lock, not the exception.
        try:
            operator.iadd(b, b)
        except BufferError:
            pass
        del b[64:]

    def ba_slice():
        b[0:0] = b             # CS2(b, b) -- the m1 == m2 aliasing path
        del b[64:]

    def ba_extend():
        b.extend(iter(b))      # CS(b) held across PyIter_Next on b's own iter
        del b[64:]

    def li_slice():
        l[0:0] = l             # CS2(l, l)
        del l[64:]

    def li_extend_list():
        l.extend(l)            # CS2(l, l) -- exact-list arm
        del l[64:]

    # NOT included: l.extend(iter(l)).  Measured single-threaded in
    # lock_self_alias_isolate.py: it does not terminate on release-ft OR
    # release-gil, because list_extend_iter_lock_held appends to the same list
    # the iterator is walking.  That is a non-termination, not a deadlock, and
    # including it made every cell of this scenario read as a hang.
    return drive([("ba", ba), ("ba_slice", ba_slice), ("ba_extend", ba_extend),
                  ("li_slice", li_slice), ("li_extend_list", li_extend_list)])


def s_list_usercode_inversion():
    """The general form, and the one that always nests.

    list_extend_iter_lock_held (listobject.c:1318) runs an arbitrary __next__
    while holding CS(self).  If that __next__ touches ANOTHER list, the second
    object's clinic critical section is taken inside the first.  Two threads
    doing it in opposite directions is a textbook lock-order inversion, and it
    needs no C-level trick -- four lines of Python.
    """
    l1 = list(range(8))
    l2 = list(range(8))

    def gen(other):
        for i in range(8):
            other.append(i)       # CS(other) taken INSIDE CS(self)
            del other[8:]
            yield i

    def fwd():
        l1.extend(gen(l2))
        trim(l1)

    def rev():
        l2.extend(gen(l1))
        trim(l2)

    return drive([("fwd", fwd), ("rev", rev)])


def s_ba_usercode_inversion():
    """Same shape for bytearray: bytearray_extend_impl:2216 PyIter_Next."""
    b1 = bytearray(b"A" * 8)
    b2 = bytearray(b"B" * 8)

    def gen(other):
        for i in range(8):
            other.append(65)      # CS(other) inside CS(self)
            del other[8:]
            yield i

    def fwd():
        b1.extend(gen(b2))
        trim(b1)

    def rev():
        b2.extend(gen(b1))
        trim(b2)

    return drive([("fwd", fwd), ("rev", rev)])


def s_chain3():
    """A three-object cycle: 1->2, 2->3, 3->1, each via the nested path."""
    ls = [list(range(64)) for _ in range(3)]

    def mk(i):
        a, b = ls[i], ls[(i + 1) % 3]

        def f():
            a.extend(iter(b))
            trim(a)

        return f

    return drive([(f"c{i}", mk(i)) for i in range(3)])


def s_solo_control():
    """Workload control: the same call volume, one thread."""
    global NTHREADS
    NTHREADS = 1
    return s_ba_iconcat_inversion()


def s_solo_control_list():
    global NTHREADS
    NTHREADS = 1
    return s_list_extend_inversion()


SCENARIOS = {
    name[2:]: fn for name, fn in sorted(globals().items()) if name.startswith("s_")
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        print("scenarios:", " ".join(sorted(SCENARIOS)))
        return 2
    name = sys.argv[1]
    print(f"PROBE:scenario={name}")
    print(f"PROBE:gil_disabled={not sys._is_gil_enabled() if hasattr(sys, '_is_gil_enabled') else 'n/a'}")
    rc = SCENARIOS[name]()
    faulthandler.cancel_dump_traceback_later()
    return rc


if __name__ == "__main__":
    sys.exit(main())
