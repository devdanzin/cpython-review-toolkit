"""Sibling sweep for the __init__-RE-ENTRY class on list / bytes / bytearray.

The bytearray instance of this class (bytearrayobject.c:927, `self->ob_exports = 0`
performed by __init__ on an object the caller already holds) is known and is fixed
upstream by PR #153498.  This probe asks the two questions Group A2 was handed:

  1. Does `list` or `bytes` carry a constructor-only write in its re-callable
     initialisation, or an invariant that __init__ re-entry can break?
  2. Does `bytearray` have any OTHER re-initialisation path (__reduce__ /
     __setstate__ / extend / += / slice assignment) that reaches the same
     `ob_exports` invariant?

Usage:  python initbypass_reinit_siblings.py <probe>
        python initbypass_reinit_siblings.py --list      (names)

One probe per process; a probe that dies leaves no PROBE: line.
"""

import sys


def p(name, val):
    print("PROBE:%s=%s" % (name, val))
    sys.stdout.flush()


def guarded(name, fn):
    try:
        p(name, fn())
    except BaseException as exc:  # noqa: BLE001
        p(name, "RAISED %s: %s" % (type(exc).__name__, exc))


# ---------------------------------------------------------------- list ------

def l_init_during_sort_key():
    """__init__ re-entry from a sort key function."""
    lst = [3, 1, 2]

    def key(x):
        lst.__init__([9, 9, 9, 9, 9])
        return x
    guarded("l_init_during_sort_key.sort", lambda: lst.sort(key=key))
    return "after=%r len=%d sizeof_ok=%s" % (
        lst, len(lst), sys.getsizeof(lst) < 10_000)


def l_init_during_sort_lt():
    """__init__ re-entry from a user __lt__ during sort."""
    lst = []

    class C:
        def __init__(self, v):
            self.v = v

        def __lt__(self, other):
            lst.__init__([1, 2, 3, 4, 5, 6, 7, 8])
            return self.v < other.v
    lst.extend(C(i) for i in (3, 1, 2))
    guarded("l_init_during_sort_lt.sort", lambda: lst.sort())
    return "after_len=%d" % len(lst)


def l_getsizeof_during_sort():
    """sys.getsizeof() while list_sort_impl has allocated == -1."""
    lst = []
    seen = []

    def key(x):
        seen.append(sys.getsizeof(lst))
        return x
    lst.extend([3, 1, 2])
    lst.sort(key=key)
    return "sizeof_during_sort=%r  normal=%r" % (seen, sys.getsizeof(lst))


def l_init_from_generator():
    """__init__ re-entering __init__ through the iterable argument."""
    lst = []

    def gen():
        yield 1
        lst.__init__([7, 7, 7, 7, 7, 7, 7, 7])
        yield 2
        yield 3
    guarded("l_init_from_generator.init", lambda: lst.__init__(gen()))
    return "after=%r" % (lst,)


def l_extend_from_generator_init():
    lst = [0]

    def gen():
        yield 1
        lst.__init__([7] * 64)
        yield 2
    guarded("l_extend_from_generator_init.extend", lambda: lst.extend(gen()))
    return "after_len=%d" % len(lst)


def l_init_self():
    lst = [1, 2, 3]
    lst.__init__(lst)
    return "after=%r" % (lst,)


def l_init_during_iteration():
    lst = [1, 2, 3, 4, 5]
    it = iter(lst)
    next(it)
    lst.__init__([9] * 100)
    return "rest=%r len=%d" % ([next(it) for _ in range(3)], len(lst))


def l_init_during_eq():
    """__init__ re-entry from __eq__ inside index/remove/count/__contains__."""
    out = {}
    for opname in ("index", "remove", "count", "contains"):
        lst = []

        class E:
            def __eq__(self, other):
                lst.__init__([0] * 200)
                return False
            __hash__ = None
        lst.extend([E() for _ in range(6)])
        probe = E()
        try:
            if opname == "index":
                r = lst.index(probe)
            elif opname == "remove":
                r = lst.remove(probe)
            elif opname == "count":
                r = lst.count(probe)
            else:
                r = probe in lst
            out[opname] = ("ok", r, len(lst))
        except BaseException as exc:  # noqa: BLE001
            out[opname] = (type(exc).__name__, str(exc), len(lst))
    return repr(out)


def l_init_during_repr():
    lst = []

    class R:
        def __repr__(self):
            lst.__init__([0] * 128)
            return "R"
    lst.extend([R(), R(), R()])
    guarded("l_init_during_repr.repr", lambda: repr(lst)[:40])
    return "after_len=%d" % len(lst)


def l_init_from_del_in_slice_assign():
    """__init__ re-entry from a __del__ run by list_ass_slice's recycle DECREF."""
    lst = []

    class D:
        def __del__(self):
            lst.__init__([5] * 64)
    lst.extend([D() for _ in range(4)])
    guarded("l_init_from_del.slice", lambda: lst.__setitem__(slice(0, 4), [1, 2]))
    return "after_len=%d" % len(lst)


def l_init_reentrant_from_init():
    """__init__ called from __init__'s own iterable, nested three deep."""
    lst = []
    depth = [0]

    def gen(d):
        depth[0] = max(depth[0], d)
        yield d
        if d < 3:
            lst.__init__(gen(d + 1))
        yield d
    guarded("l_init_reentrant.init", lambda: lst.__init__(gen(1)))
    return "after=%r depth=%d" % (lst, depth[0])


# ---------------------------------------------------------------- bytes -----

def by_init_is_noop():
    b = b"abcd"
    out = []
    for args in ((), (b"zzzz",), ("x", "ascii"), (1, 2, 3)):
        try:
            b.__init__(*args)
            out.append(("ok", args, b))
        except BaseException as exc:  # noqa: BLE001
            out.append((type(exc).__name__, args, str(exc)[:60]))
    return repr(out)


def by_init_slot_identity():
    return "bytes.__init__ is object.__init__: %s ; bytes.__new__ is object.__new__: %s" % (
        bytes.__init__ is object.__init__, bytes.__new__ is object.__new__)


def by_subclass_skips_super():
    class B(bytes):
        def __init__(self, *a, **k):
            pass
    b = B(b"payload")
    return "value=%r len=%d hash_ok=%s" % (b, len(b), hash(b) == hash(b"payload"))


# ------------------------------------------------------------ bytearray -----

def ba_ob_exports_writers():
    """Enumerate every Python-reachable path that could reset ob_exports.

    Each entry runs an operation on a bytearray that has a LIVE memoryview and
    reports whether the export counter survived (BufferError == survived).
    """
    out = {}
    ops = {
        "append": lambda b: b.append(1),
        "extend": lambda b: b.extend(b"AB"),
        "iadd": lambda b: b.__iadd__(b"AB"),
        "imul": lambda b: b.__imul__(3),
        "setslice_grow": lambda b: b.__setitem__(slice(0, 0), b"AB"),
        "setslice_shrink": lambda b: b.__setitem__(slice(0, 2), b""),
        "delslice": lambda b: b.__delitem__(slice(0, 2)),
        "delslice_ext": lambda b: b.__delitem__(slice(None, None, 2)),
        "clear": lambda b: b.clear(),
        "pop": lambda b: b.pop(),
        "remove": lambda b: b.remove(65),
        "resize": lambda b: b.resize(16),
        "take_bytes": lambda b: b.take_bytes(2),
        "init": lambda b: b.__init__(b"zz"),
        "init_empty": lambda b: b.__init__(),
        "setstate_reduce": lambda b: b.__reduce_ex__(2),
    }
    for name, fn in ops.items():
        b = bytearray(b"AAAABBBB")
        mv = memoryview(b)
        try:
            fn(b)
            res = "NO_BUFFERERROR"
        except BufferError:
            res = "BufferError"
        except BaseException as exc:  # noqa: BLE001
            res = "%s: %s" % (type(exc).__name__, str(exc)[:40])
        # After releasing the view the counter must be back to 0: a second
        # export-then-resize must still raise.
        mv.release()
        try:
            b.extend(b"C")
            second = "resize_allowed_after_release(OK)"
        except BufferError:
            second = "STILL_PINNED(counter leaked +)"
        mv2 = memoryview(b)
        try:
            b.extend(b"D")
            third = "COUNTER_BROKEN(negative)"
        except BufferError:
            third = "counter_ok"
        except BaseException as exc:  # noqa: BLE001
            third = type(exc).__name__
        mv2.release()
        out[name] = (res, second, third)
    return "\n        ".join("%-16s %s" % (k, v) for k, v in out.items())


def ba_reduce_roundtrip_reinit():
    """Does the pickle round-trip re-initialise a LIVE bytearray in place?"""
    import pickle
    b = bytearray(b"AAAA")
    mv = memoryview(b)
    red = b.__reduce_ex__(2)
    out = ["reduce_ex2=%r" % (red,)]
    try:
        out.append("loads=%r" % (pickle.loads(pickle.dumps(b, 2)),))
    except BaseException as exc:  # noqa: BLE001
        out.append("loads RAISED %s" % type(exc).__name__)
    out.append("has_setstate=%s" % hasattr(b, "__setstate__"))
    mv.release()
    return " | ".join(out)


def ba_new_then_memoryview_then_append():
    """The accidental guard: an export on a bypassed bytearray blocks the SEGV."""
    b = bytearray.__new__(bytearray)
    mv = memoryview(b)
    try:
        b.append(1)
        r = "NO_EXCEPTION (would have segfaulted without the export)"
    except BufferError as exc:
        r = "BufferError: %s" % exc
    mv.release()
    return r


def ba_delattr():
    """Is any attribute of these three types deletable at all?"""
    out = []
    for obj, names in (
        (bytearray(b"x"), ["ob_exports", "__class__", "__doc__"]),
        ([1], ["__class__", "__doc__"]),
        (b"x", ["__class__", "__doc__"]),
    ):
        for n in names:
            try:
                delattr(obj, n)
                out.append("%s.%s DELETED" % (type(obj).__name__, n))
            except BaseException as exc:  # noqa: BLE001
                out.append("%s.%s -> %s" % (type(obj).__name__, n, type(exc).__name__))
    return "; ".join(out)


PROBES = {k: v for k, v in sorted(globals().items())
          if callable(v) and (k.startswith("l_") or k.startswith("by_")
                              or k.startswith("ba_"))}


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        for k in PROBES:
            print(k)
        return
    name = sys.argv[1]
    guarded(name, PROBES[name])


if __name__ == "__main__":
    main()
