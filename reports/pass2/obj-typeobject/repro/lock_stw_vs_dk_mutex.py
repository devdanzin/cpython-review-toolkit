"""Variant 2: the LOCK-ORDERING form of the same defect -- no re-entrancy needed.

T1: insert_split_key holds keys->dk_mutex (_Py_LOCK_DONT_DETACH) and runs
    arbitrary Python from _PyType_Modified_Unlocked's unraisable path.
    That Python stops the world (abc.register -> _PyType_SetFlagsRecursive
    -> types_stop_world, Objects/typeobject.c:6522).

T2: hammers new-attribute insertion on instances of the SAME class, so it
    blocks in LOCK_KEYS on the same dk_mutex. Because that mutex is taken
    with _Py_LOCK_DONT_DETACH, T2 does NOT detach and therefore never parks
    for stop-the-world.

=> T1's stop-the-world waits for T2 forever. Deadlock, no re-entrancy.

This is the shape of gh-151593 ("test_abc hangs on TSan Parallel Test on
Free Threading"), whose fix was reverted (gh-152238) and re-landed as
gh-152914 "take 2" on 2026-07-06.
"""

import abc
import sys
import threading
import time

import _testcapi


class C:
    pass


class Target(abc.ABC):
    pass


stop = threading.Event()
progress = {"t2": 0}


def t2_worker():
    i = 0
    while not stop.is_set():
        o = C()
        # each iteration inserts a brand-new split key -> insert_split_key
        setattr(o, f"attr_{i}", i)
        i += 1
        progress["t2"] = i


def main():
    wid = _testcapi.add_type_watcher(1)  # error-returning callback
    _testcapi.watch_type(wid, C)

    fired = []

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        print("  [hook] dk_mutex held; now stopping the world via abc.register",
              flush=True)
        # _PyType_SetFlagsRecursive -> type_lock_prevent_release + types_stop_world
        Target.register(int)
        print("  [hook] stop-the-world completed (NO deadlock)", flush=True)

    sys.unraisablehook = hook

    t = threading.Thread(target=t2_worker, daemon=True)
    t.start()
    time.sleep(0.3)
    print(f"[main] T2 running (progress={progress['t2']}); triggering T1", flush=True)

    o = C()
    o.trigger_attribute = 1

    print("[main] completed without deadlock", flush=True)
    stop.set()
    t.join(timeout=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
