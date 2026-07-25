"""Py_ReprEnter is taken INSIDE the per-object critical section.

set_repr (setobject.c:811) and dict_repr (dictobject.c:3777) both do

    Py_BEGIN_CRITICAL_SECTION(self);
    result = <..>_lock_held(self);      // <- Py_ReprEnter lives in here
    Py_END_CRITICAL_SECTION();

so a user __repr__ that re-enters repr(self) re-acquires the *same* object's
lock on the *same* thread before Py_ReprEnter gets a chance to short-circuit.
This probe asks whether that self-re-entry deadlocks on a free-threaded build.

Run:  <interp> repr_reentry.py {set|dict|frozendict|view}
"""

import sys

which = sys.argv[1] if len(sys.argv) > 1 else "set"


class R:
    def __repr__(self):
        return repr(container)


if which == "set":
    container = {R()}
elif which == "dict":
    container = {0: R()}
elif which == "frozendict":
    container = frozendict({0: R()})
elif which == "view":
    container = {0: R()}.items()
else:
    raise SystemExit("unknown: " + which)

print("built", which, flush=True)
try:
    print("repr ->", repr(container)[:80], flush=True)
except RecursionError as e:
    print("RecursionError:", e, flush=True)
print("DONE", flush=True)
