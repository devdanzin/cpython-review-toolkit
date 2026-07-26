"""Measure what `list.remove`'s clinic critical section actually guarantees.

Task (d) tie-breaker.  Group A ruled `list_remove_impl:3410`'s raw
`self->ob_item[i]` read ACCEPTABLE on the grounds that
(i) the clinic wrapper holds `Py_BEGIN_CRITICAL_SECTION(self)`
    (Objects/clinic/listobject.c.h:391) and
(ii) `_PyCriticalSection_Resume` re-acquires before the plain loads.

Neither claim was measured directly.  These probes measure both, plus the
re-entrancy question nobody asked.

Usage: python gil_critical_section_semantics.py <probe>

Probes
  detach_window   Thread A runs L.remove(x) where x.__eq__ blocks on an Event.
                  Thread B tries L.append() with a timeout while A is parked.
                  B PROCEEDS  -> the per-object lock IS dropped on detach
                  B BLOCKS    -> the lock is held across the whole impl
                  This is the load-bearing fact behind Group A's verdict.

  reentrant_same  L.remove(x) where x.__eq__ mutates L on the SAME thread.
                  Under the GIL this returns.  Under free threading the
                  clinic critical section is already held on `self`.

  reentrant_read  Same, but __eq__ only reads L (len / index).

  contains_detach The unlocked sibling: `x in L` (list_contains, no critical
                  section) with the same blocking __eq__.  Control that
                  isolates "the lock" from "the blocking callback".
"""

from __future__ import annotations

import faulthandler
import sys
import threading
import time

faulthandler.enable()

RESULT = []


def p(msg: str) -> None:
    print(f"PROBE:{msg}", flush=True)


def detach_window(op: str = "remove") -> None:
    entered = threading.Event()
    release = threading.Event()

    class Blocker:
        def __eq__(self, other):
            entered.set()
            # A real block: this detaches the thread state.
            release.wait(10.0)
            return False

        __hash__ = None

    L = [1, 2, 3, Blocker(), 5, 6, 7, 8]
    target = Blocker()

    def worker():
        try:
            if op == "remove":
                L.remove(target)
            else:
                _ = target in L
        except ValueError:
            pass
        except Exception as e:
            p(f"worker_exc={type(e).__name__}")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    if not entered.wait(5.0):
        p("never_entered_eq")
        return

    # A is now parked inside __eq__, called from inside the impl.
    t0 = time.monotonic()
    done = threading.Event()

    def mutator():
        try:
            L.append("FROM_B")
            done.set()
        except Exception as e:
            p(f"mutator_exc={type(e).__name__}")

    m = threading.Thread(target=mutator, daemon=True)
    m.start()
    proceeded = done.wait(2.0)
    dt = time.monotonic() - t0
    p(f"{op}_mutator_proceeded_while_eq_blocked={proceeded} after={dt:.3f}s")
    release.set()
    t.join(10)
    m.join(10)
    p(f"{op}_final_len={len(L)}")
    p("completed")


def wrong_element(op: str = "remove") -> None:
    """Cross-thread version of the residual Group A named but did not measure.

    L = [A, BLOCKER, B, C, D].  Thread A runs L.remove(target) where target
    matches D.  The BLOCKER's __eq__ parks, releasing the per-object lock.
    While parked, thread B does `del L[0]`, shifting every element left one.
    On resume the loop index `i` is stale.
    """
    entered = threading.Event()
    release = threading.Event()

    class Blocker:
        def __eq__(self, other):
            entered.set()
            release.wait(10.0)
            return False

        __hash__ = None

    class Match:
        def __eq__(self, other):
            return isinstance(other, Match)

        __hash__ = None

    tail_marker = Match()
    L = ["A", Blocker(), "B", "C", tail_marker, "E"]
    before = [type(x).__name__ if not isinstance(x, str) else x for x in L]

    def worker():
        try:
            L.remove(Match())
        except ValueError:
            p("remove_raised_ValueError")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    if not entered.wait(5.0):
        p("never_entered_eq")
        return
    del L[0]
    p(f"deleted_index0_while_parked len={len(L)}")
    release.set()
    t.join(10)
    after = [type(x).__name__ if not isinstance(x, str) else x for x in L]
    p(f"before={before}")
    p(f"after={after}")
    p(f"match_still_present={any(isinstance(x, Match) for x in L)}")
    p("completed")


def reentrant(kind: str) -> None:
    L = [1, 2, 3, 4, 5, 6, 7, 8]
    seen = []
    fired = []

    class Reenter:
        def __eq__(self, other):
            try:
                if kind == "same":
                    # BOUNDED: mutate exactly once, so a hang here is a lock
                    # problem and not the well-known "grow the list forever"
                    # non-termination of remove().
                    if not fired:
                        fired.append(1)
                        L.append(99)
                        seen.append("appended")
                else:
                    seen.append(len(L))
            except Exception as e:
                seen.append(f"exc:{type(e).__name__}")
            return False

        __hash__ = None

    watchdog = threading.Timer(
        8.0, lambda: (p(f"reentrant_{kind}=HUNG"), faulthandler.dump_traceback(), __import__("os")._exit(124))
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        L.remove(Reenter())
    except ValueError:
        pass
    watchdog.cancel()
    p(f"reentrant_{kind}=returned len={len(L)} seen={seen[:3]}")
    p("completed")


if __name__ == "__main__":
    probe = sys.argv[1] if len(sys.argv) > 1 else "detach_window"
    if probe == "detach_window":
        detach_window("remove")
    elif probe == "contains_detach":
        detach_window("contains")
    elif probe == "reentrant_same":
        reentrant("same")
    elif probe == "reentrant_read":
        reentrant("read")
    elif probe == "wrong_element":
        wrong_element("remove")
    else:
        raise SystemExit(f"unknown probe {probe}")
