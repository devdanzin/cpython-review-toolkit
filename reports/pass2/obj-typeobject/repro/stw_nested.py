"""Probe 2: what runs inside object_set_class's stop-the-world region.

`obj.__class__ = Other` -> object_set_class (typeobject.c:7800) stops the world
(:7823) -> object_set_class_world_stopped (:7700) -> compatible_for_assignment
(:7763) -> same_slots_added (:7588) -> PyObject_RichCompareBool (:7609) -> the
__eq__ of a str subclass stored in __slots__.  That __eq__ is arbitrary Python,
executed with every other thread suspended.

variant "none"  -> control: __eq__ does nothing extra                (expect: OK)
variant "name"  -> __eq__ assigns type.__name__ (type_set_name, RAW _PyEval_StopTheWorld)
variant "class" -> __eq__ assigns another object's __class__ (types_stop_world wrapper)
variant "lock"  -> __eq__ waits 0.5s for a helper thread to release a lock
"""
import faulthandler
import sys
import threading
import time

variant = sys.argv[1] if len(sys.argv) > 1 else "none"
faulthandler.dump_traceback_later(6, exit=True)


class Victim:
    pass


class Base:
    pass


held = threading.Lock()
started = threading.Event()


def holder():
    held.acquire()
    started.set()
    time.sleep(0.5)  # short: without STW the main thread waits 0.5s and proceeds
    held.release()


class MyStr(str):
    def __eq__(self, other):
        print("  [entered __eq__ -- this is Python code, world is stopped]", flush=True)
        if variant == "name":
            Victim.__name__ = "renamed"
        elif variant == "class":
            o = C1()
            k = o  # keep refcount > 1 so the STW path is taken
            o.__class__ = C2
            del k
        elif variant == "lock":
            with held:
                pass
        print("  [__eq__ body completed]", flush=True)
        return str.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return str.__hash__(self)

    def __lt__(self, other):
        return str.__lt__(self, other)


class A(Base):
    __slots__ = (MyStr("x"),)


class B(Base):
    __slots__ = (MyStr("x"),)


class CB:
    pass


class C1(CB):
    __slots__ = ("y",)


class C2(CB):
    __slots__ = ("y",)


if variant == "lock":
    t = threading.Thread(target=holder, daemon=True)
    t.start()
    started.wait()
    time.sleep(0.1)

a = A()
keep = a
print("before assignment", flush=True)
a.__class__ = B
print("AFTER assignment -> no hang, class =", type(a).__name__, flush=True)
del keep
faulthandler.cancel_dump_traceback_later()
