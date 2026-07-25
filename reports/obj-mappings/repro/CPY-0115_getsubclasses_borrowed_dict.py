"""CPY-0115 -- `_PyType_GetSubclasses` walks a BORROWED `tp_subclasses` dict with
a live `PyDict_Next` cursor while another thread deletes from it, and -- when the
delete empties it -- FREES it.

    Objects/typeobject.c:775-807  _PyType_GetSubclasses          (no lock at all)
        PyObject *subclasses = lookup_tp_subclasses(self);  // borrowed ref  :783
        // The loop cannot modify tp_subclasses, there is no need
        // to hold a strong reference (use a borrowed reference).   :788-789
        while (PyDict_Next(subclasses, &i, NULL, &ref)) {           :793
            PyTypeObject *subclass = type_from_ref(ref);            :794   (ref borrowed)
            if (PyList_Append(list, ...) < 0) { ... }               :799   (allocates)

    Objects/typeobject.c:9773  remove_subclass
        PyDict_DelItem(subclasses, key)        -> Objects/dictobject.c:2987 delitem_common
        if (PyDict_Size(subclasses) == 0)
            clear_tp_subclasses(base);         -> Py_CLEAR(self->tp_subclasses)   :728

    Objects/typeobject.c:699-714  init_tp_subclasses -- installs a NEW dict at :712

`type___subclasses___impl` (:7055) and its clinic wrapper take NO critical
section and no type lock, so the borrowed dict at :783 is unprotected for the
whole loop.  The comment at :788-789 is a RE-ENTRANCY argument ("the loop cannot
modify tp_subclasses"); it says nothing about a second thread, which is exactly
the guarded-twin discipline this campaign records.

Trigger is pure Python: `X.__bases__ = (B,)` calls `remove_all_subclasses(X,
old_bases)` -> `remove_subclass(A, X)`.  When X is A's only subclass the delete
empties A's dict and `clear_tp_subclasses(A)` frees it out from under a
concurrent `A.__subclasses__()`.

Usage:  <python> CPY-0115_getsubclasses_borrowed_dict.py [rounds] [nreaders]
Exit 0 = survived.  SIGSEGV / SIGABRT / ASan report = reproduced.
Run on a GIL build as the control: there the two threads cannot interleave
inside the C loop, so it must be clean.
"""

import sys
import threading

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 200000
NREADERS = int(sys.argv[2]) if len(sys.argv) > 2 else 6
NFLIPPERS = 2
NFAMILIES = 8

STOP = False


class A0:
    pass


class B0:
    pass


FAMILIES = []
for _n in range(NFAMILIES):
    A = type("A%d" % _n, (), {})
    B = type("B%d" % _n, (), {})
    X = type("X%d" % _n, (A,), {})
    FAMILIES.append((A, B, X))


def reader(idx):
    while not STOP:
        for (A, B, X) in FAMILIES:
            try:
                A.__subclasses__()
                B.__subclasses__()
            except Exception:
                pass


def flipper(idx):
    i = idx
    while not STOP:
        A, B, X = FAMILIES[i % NFAMILIES]
        try:
            X.__bases__ = (B,)      # remove_subclass(A, X) -> A's dict empties -> FREED
            X.__bases__ = (A,)      # init_tp_subclasses(A) installs a NEW dict
        except TypeError:
            pass
        i += 1


def main():
    global STOP
    print("rounds=%d readers=%d gil=%s"
          % (ROUNDS, NREADERS,
             getattr(sys, "_is_gil_enabled", lambda: "n/a")()), flush=True)
    ts = [threading.Thread(target=reader, args=(i,), daemon=True)
          for i in range(NREADERS)]
    ts += [threading.Thread(target=flipper, args=(i,), daemon=True)
           for i in range(NFLIPPERS)]
    for t in ts:
        t.start()

    for r in range(ROUNDS):
        for (A, B, X) in FAMILIES:
            A.__subclasses__()
        if r % 50000 == 0:
            print("round", r, flush=True)

    STOP = True
    for t in ts:
        t.join(timeout=10.0)
    print("survived %d rounds" % ROUNDS, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
