"""_PyType_Modified_Unlocked holds a borrowed tp_subclasses across the type-watcher
callback / PyErr_FormatUnraisable(%R) at Objects/typeobject.c:1201-1225."""
import _testcapi

armed = [False]


class M(type):
    def __repr__(cls):
        if armed[0]:
            armed[0] = False
            print("  M.__repr__ fired; detaching", flush=True)
            for s in subs:
                for _ in range(3):
                    try:
                        s.__bases__ = (object,)
                        break
                    except Exception as e:
                        last = e
                else:
                    print("   detach failed %r" % (last,), flush=True)
            print("  Base.__subclasses__() =", Base.__subclasses__(), flush=True)
        return "<M>"


class Base(metaclass=M):
    pass


subs = [M('S%d' % i, (Base,), {}) for i in range(8)]

wid = _testcapi.add_type_watcher(1)          # error-returning callback
for s in subs:
    _testcapi.watch_type(wid, s)

# make sure every type has a version tag so _PyType_Modified_Unlocked recurses
for s in subs:
    s.__mro__
Base.__mro__

armed[0] = True
print("touching Base ...", flush=True)
Base.attr = 1
print("done", flush=True)
