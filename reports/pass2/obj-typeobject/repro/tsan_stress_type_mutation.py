#!/usr/bin/env python3
"""TSan stress test for CPython's TYPE-MUTATION surface -- new territory.

Nobody in pass 2 hammered these concurrently.  Every scenario shares ONE type
object (or one instance) across all threads.

Surface under test, all in Objects/typeobject.c:

  __class__ assignment   object_set_class:7825 / object_set_class_world_stopped:7763
                         compatible_for_assignment:7683 / same_slots_added:7609
                         (P2-F3 runs user Python inside the stopped world;
                          P2-F4 carries a stale `oldto` across the re-entry)
  __bases__ assignment   type_set_bases:1918 / type_set_bases_unlocked:1949
                         rollback log freed at :1952 before the :1968 exit (P2-F11)
  MRO recomputation      mro_internal:3678 / mro_invoke / mro_implementation_unlocked:3503
                         type_mro_modified:1299 takes a BORROWED mro (P2-F5)
                         mro_hierarchy_for_complete_type:1803
  subclass bookkeeping   add_all_subclasses / remove_subclass:9791 (Py_CLEAR of
                         the dict `_PyType_Modified_Unlocked:1201` is iterating)
  watcher callbacks      _PyType_Modified_Unlocked:1206/:1212/:1222/:1223 --
                         recursive subclass descent + arbitrary Python from a
                         watcher callback and from PyErr_FormatUnraisable("%R")

Run with a free-threaded TSan build of CPython:
    PYTHON_GIL=0 ./python tsan_stress_type_mutation.py 2> tsan_report.txt
"""
import os
import signal
import sys
import threading
import time

THREADS = 8
ITERATIONS = 8_000
SCENARIO_TIMEOUT = 120


def _is_tsan_build():
    try:
        import sysconfig
        return "fsanitize=thread" in (sysconfig.get_config_var("CFLAGS") or "").lower()
    except Exception:
        return False


IS_TSAN = _is_tsan_build()
if IS_TSAN:
    THREADS = min(THREADS, 4)
    ITERATIONS = min(ITERATIONS, 300)

import warnings
warnings.filterwarnings("ignore", ".*GIL.*")
# sys._clear_type_cache() is deprecated; its DeprecationWarning drags in a
# first-time lazy import (linecache -> tokenize -> io -> abc.ABCMeta.__new__),
# i.e. CLASS CREATION on a worker thread. Under continuous stop-the-world type
# mutation that import starves and the scenario appears to hang. Suppress the
# warning AND pre-import, so the harness measures type mutation, not import.
warnings.simplefilter("ignore", DeprecationWarning)
import linecache  # noqa: E402
import tokenize   # noqa: E402,F401
import io         # noqa: E402,F401

try:
    import _testcapi
except ImportError:
    _testcapi = None


def run_scenario(name, target_fns, thread_counts=None):
    print(f"  Running: {name}...", end=" ", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.environ.get("STRESS_NO_FORK"):
        # TSan + os.fork() deadlocks inside the sanitizer runtime; run inline
        # and rely on one-scenario-per-process isolation from the driver.
        try:
            _run_scenario_threads(target_fns, thread_counts)
            print("OK", flush=True)
        except SystemExit:
            print("FAIL", flush=True)
        except BaseException as e:
            print("ERROR (%r)" % (e,), flush=True)
        return
    pid = os.fork()
    if pid == 0:
        try:
            _run_scenario_threads(target_fns, thread_counts)
            os._exit(0)
        except SystemExit as e:
            os._exit(e.code if isinstance(e.code, int) else 1)
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)

    deadline = time.monotonic() + SCENARIO_TIMEOUT
    wait_status = None
    while time.monotonic() < deadline:
        r, status = os.waitpid(pid, os.WNOHANG)
        if r != 0:
            wait_status = status
            break
        time.sleep(0.1)

    if wait_status is None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        print(f"TIMEOUT ({SCENARIO_TIMEOUT}s)  <-- possible deadlock/hang")
    elif os.WIFSIGNALED(wait_status):
        sig = os.WTERMSIG(wait_status)
        nm = signal.Signals(sig).name if sig in signal.Signals._value2member_map_ else str(sig)
        print(f"CRASH ({nm})")
    elif os.WIFEXITED(wait_status) and os.WEXITSTATUS(wait_status) != 0:
        print(f"FAIL (exit {os.WEXITSTATUS(wait_status)})")
    else:
        print("OK")


def _run_scenario_threads(target_fns, thread_counts=None):
    if thread_counts is None:
        thread_counts = [THREADS] * len(target_fns)
    barrier = threading.Barrier(sum(thread_counts))
    errors = []

    def wrapper(fn):
        def wrapped():
            barrier.wait()
            try:
                fn()
            except Exception as e:
                errors.append(e)
        return wrapped

    threads = []
    for fn, count in zip(target_fns, thread_counts):
        for _ in range(count):
            threads.append(threading.Thread(target=wrapper(fn)))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    if errors:
        print("thread errors:", errors[:3], file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------- scenario 1
def scenario_setclass_pingpong():
    """`__class__` assignment on ONE shared instance from many threads.

    object_set_class:7825 captures `oldto = Py_TYPE(self)` BEFORE
    object_set_class_world_stopped, then DECREFs it at :7832 (P2-F4).  Two
    threads reassigning the same instance's __class__ is the concurrent form
    of the re-entrancy P2-F4 drives single-threaded.
    """
    class A:
        __slots__ = ()

    class B:
        __slots__ = ()

    class C:
        __slots__ = ()

    obj = A()
    pool = [A, B, C]

    def flipper(k):
        def go():
            for i in range(ITERATIONS):
                try:
                    obj.__class__ = pool[(i + k) % 3]
                except TypeError:
                    pass
        return go

    def reader():
        for _ in range(ITERATIONS):
            type(obj)
            obj.__class__
            isinstance(obj, A)

    run_scenario(
        "__class__ assignment ping-pong on one shared instance (+ readers)",
        [flipper(0), flipper(1), reader],
        [max(1, THREADS // 2), max(1, THREADS // 2), THREADS // 2],
    )


# ---------------------------------------------------------------- scenario 2
def scenario_setbases_shared_type():
    """`__bases__` assignment on ONE shared type from many threads.

    Drives type_set_bases_unlocked:1949 -> mro_hierarchy_for_complete_type:1803
    -> mro_implementation_unlocked:3503, plus add_all_subclasses /
    remove_subclass:9791 on the shared parents' tp_subclasses dicts.
    """
    class P1:
        pass

    class P2:
        pass

    class P3:
        pass

    class X(P1):
        pass

    pool = [(P1,), (P2,), (P3,), (P1,), (P2,)]

    def rebaser(k):
        def go():
            for i in range(ITERATIONS):
                try:
                    X.__bases__ = pool[(i + k) % len(pool)]
                except TypeError:
                    pass
        return go

    def reader():
        for _ in range(ITERATIONS):
            X.__mro__
            X.__bases__
            P1.__subclasses__()
            P2.__subclasses__()
            issubclass(X, P1)

    run_scenario(
        "__bases__ reassignment on one shared type (+ MRO/subclass readers)",
        [rebaser(0), rebaser(2), reader],
        [max(1, THREADS // 2), max(1, THREADS // 2), THREADS // 2],
    )


# ---------------------------------------------------------------- scenario 3
def scenario_mro_recompute_vs_lookup():
    """MRO recomputation racing attribute lookup THROUGH the MRO.

    type_mro_modified:1299 receives the mro as a BORROWED pointer from
    mro_internal:3678 (P2-F5).  Readers here walk the same mro via
    _PyType_LookupStackRefAndVersion / find_name_in_mro on every getattr.
    """
    class Base:
        a = 1

    class Mid1(Base):
        b = 2

    class Mid2(Base):
        c = 3

    class Leaf(Mid1, Mid2):
        d = 4

    grand = [type("G%d" % i, (Leaf,), {"e": i}) for i in range(6)]

    def recomputer():
        for i in range(ITERATIONS):
            try:
                Leaf.__bases__ = (Mid1, Mid2) if i & 1 else (Mid2, Mid1)
            except TypeError:
                pass

    def mutator():
        for i in range(ITERATIONS):
            Base.a = i
            Mid1.b = i

    def looker():
        for _ in range(ITERATIONS):
            for g in grand:
                getattr(g, "a", None)
                getattr(g, "b", None)
                getattr(g, "c", None)
                getattr(g, "e", None)
                g.__mro__

    run_scenario(
        "MRO recomputation vs MRO-walking attribute lookup on a shared lattice",
        [recomputer, mutator, looker],
        [max(1, THREADS // 4), max(1, THREADS // 4), THREADS],
    )


# ---------------------------------------------------------------- scenario 4
def scenario_watcher_callbacks_concurrent():
    """Watcher callbacks firing on many threads against ONE shared hierarchy.

    _PyType_Modified_Unlocked:1201 iterates tp_subclasses with a live
    PyDict_Next cursor and a BORROWED pointer, recursing at :1206 and firing
    watcher callbacks at :1222 -- while another thread reparents subclasses,
    which can reach remove_subclass:9791 -> Py_CLEAR(tp_subclasses) on the very
    dict being walked (P2-F9).
    """
    if _testcapi is None:
        print("  SKIP: _testcapi unavailable")
        return

    wid = _testcapi.add_type_watcher(3)
    events = _testcapi.get_type_modified_events()

    class Root:
        pass

    class Alt:
        pass

    kids = [type("K%d" % i, (Root,), {}) for i in range(8)]
    grandkids = [type("GK%d" % i, (kids[i % 8],), {}) for i in range(16)]

    for t in [Root, Alt] + kids + grandkids:
        _testcapi.watch_type(wid, t)

    def modifier():
        for i in range(ITERATIONS):
            getattr(Root, "v", None)
            _testcapi.watch_type(wid, Root)
            Root.v = i          # recursive descent through 24 subclasses
            if len(events) > 1024:
                del events[:]

    def reparenter():
        for i in range(ITERATIONS):
            k = kids[i % 8]
            try:
                k.__bases__ = (Alt,) if i & 1 else (Root,)
            except TypeError:
                pass

    def reader():
        for _ in range(ITERATIONS):
            Root.__subclasses__()
            for g in grandkids:
                getattr(g, "v", None)

    run_scenario(
        "watcher callbacks + recursive subclass descent vs live reparenting",
        [modifier, reparenter, reader],
        [max(1, THREADS // 2), max(1, THREADS // 4), THREADS // 2],
    )
    _testcapi.clear_type_watcher(wid)


# ---------------------------------------------------------------- scenario 5
def scenario_mixed():
    """Everything at once on one shared lattice -- the realistic shape."""
    class Base:
        x = 0

    class Alt:
        x = 1

    class Obj(Base):
        __slots__ = ()

    class Obj2(Alt):
        __slots__ = ()

    inst = Obj()

    def rebase():
        for i in range(ITERATIONS):
            try:
                Obj.__bases__ = (Alt,) if i & 1 else (Base,)
            except TypeError:
                pass

    def setcls():
        for i in range(ITERATIONS):
            try:
                inst.__class__ = Obj2 if i & 1 else Obj
            except TypeError:
                pass

    def setattr_():
        for i in range(ITERATIONS):
            Base.x = i
            Alt.x = i

    def clearcache():
        for _ in range(ITERATIONS):
            sys._clear_type_cache()

    def read():
        for _ in range(ITERATIONS):
            getattr(inst, "x", None)
            type(inst).__mro__
            Base.__subclasses__()

    run_scenario(
        "mixed: __bases__ + __class__ + setattr + cache clear + readers",
        [rebase, setcls, setattr_, clearcache, read],
        [2, 2, 2, 1, max(2, THREADS // 2)],
    )


def _select(all_scenarios):
    """Allow one-scenario-per-process driving: `script.py <substring>`."""
    if len(sys.argv) > 1:
        want = sys.argv[1]
        return [f for f in all_scenarios if want in f.__name__]
    return all_scenarios


if __name__ == "__main__":
    print("TSan stress test for CPython type-mutation surface (pass-2 new territory)")
    print(f"  Python: {sys.version.splitlines()[0]}")
    print(f"  TSan build: {IS_TSAN}")
    print(f"  Threads: {THREADS}, Iterations: {ITERATIONS}")
    print()
    for _sc in _select([scenario_setclass_pingpong, scenario_setbases_shared_type, scenario_mro_recompute_vs_lookup, scenario_watcher_callbacks_concurrent, scenario_mixed]):
        _sc()

    print("\nDone. Check stderr for TSan warnings.")
