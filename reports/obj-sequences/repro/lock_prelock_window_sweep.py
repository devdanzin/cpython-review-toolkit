#!/usr/bin/env python3
"""lock-discipline-checker, slice obj-sequences, task (a).

The 18 Argument-Clinic wrappers in Objects/clinic/{listobject,bytearrayobject}.c.h
that run arbitrary user Python in a CONVERTER *before* Py_BEGIN_CRITICAL_SECTION.
The question is not whether the converter touches `self` -- it does not, it
operates on an argument -- but whether the arbitrary Python it runs can mutate
the state the critical section is supposed to protect, and whether the impl
then re-derives from the CURRENT state under the lock or trusts the value the
converter produced against a state that no longer exists.

Each probe passes a converter argument whose __index__ / __buffer__ empties or
resizes the receiver, then checks the call for a crash or a wrong answer.

  converter family        wrappers
  _PyNumber_Index         list.insert, list.pop, bytearray.resize,
                          bytearray.replace(count), bytearray.split(maxsplit),
                          bytearray.rsplit(maxsplit), bytearray.insert,
                          bytearray.pop, bytearray.hex(bytes_per_sep)
  _PyEval_SliceIndex      bytearray.{find,count,index,rfind,rindex,
                          startswith,endswith} x {start, end}
  PyObject_GetBuffer      bytearray.removeprefix, bytearray.removesuffix,
                          bytearray.replace(old, new)

Usage: python lock_prelock_window_sweep.py [all|<probe>]
"""

import os
import sys
import traceback

PAY = b"PAYLOAD-" * 512          # 4096 bytes, forces a real allocation
SMALL = b"abcdefgh"

# clear  -> receiver reallocated down to the empty-bytes constant
# grow   -> receiver reallocated UP, so the data pointer moves
# shrink -> receiver truncated in place
MODE = os.environ.get("LOCK_PRELOCK_MODE", "clear")


class Idx:
    """__index__ that mutates the receiver, then returns `val`."""

    def __init__(self, target, val, mode=None):
        self.t, self.v, self.mode = target, val, mode or MODE

    def __index__(self):
        _mutate(self.t, self.mode)
        return self.v


class Buf:
    """__buffer__ that mutates the receiver, then exports `data`."""

    def __init__(self, target, data=b"P", mode=None):
        self.t, self.data, self.mode = target, data, mode or MODE

    def __buffer__(self, flags):
        _mutate(self.t, self.mode)
        return memoryview(self.data)


def _mutate(t, mode):
    try:
        if mode == "clear":
            t.clear() if hasattr(t, "clear") else t.__init__()
        elif mode == "grow":
            t.extend(b"Z" * 8192) if isinstance(t, bytearray) else t.extend(range(8192))
        elif mode == "shrink":
            del t[1:]
    except BaseException:
        pass


RESULTS = []


def probe(name, fn):
    try:
        r = fn()
        RESULTS.append((name, "OK", repr(r)[:70]))
    except BaseException as e:  # noqa: BLE001
        RESULTS.append((name, type(e).__name__, str(e)[:60]))


# --------------------------------------------------- _PyNumber_Index family

def p_list_insert():
    l = list(range(64))
    l.insert(Idx(l, 60), "X")
    return len(l), l[:2]


def p_list_pop():
    l = list(range(64))
    return l.pop(Idx(l, 60)), len(l)


def p_ba_resize():
    b = bytearray(PAY)
    b.resize(Idx(b, 4096))
    return len(b)


def p_ba_insert():
    b = bytearray(PAY)
    b.insert(Idx(b, 4000), 65)
    return len(b)


def p_ba_pop():
    b = bytearray(PAY)
    return b.pop(Idx(b, 4000)), len(b)


def p_ba_replace_count():
    b = bytearray(PAY)
    return len(b.replace(b"P", b"QQ", Idx(b, 1000)))


def p_ba_split_maxsplit():
    b = bytearray(PAY)
    return len(b.split(b"-", Idx(b, 1000)))


def p_ba_rsplit_maxsplit():
    b = bytearray(PAY)
    return len(b.rsplit(b"-", Idx(b, 1000)))


def p_ba_hex_sep():
    b = bytearray(PAY)
    return len(b.hex("_", Idx(b, 4)))


# ------------------------------------------------ _PyEval_SliceIndex family

def _slicefn(meth, which):
    def f():
        b = bytearray(PAY)
        m = getattr(b, meth)
        arg = b"P"
        if which == "start":
            return m(arg, Idx(b, 4000), 4096)
        return m(arg, 0, Idx(b, 4000))

    return f


# ------------------------------------------------ PyObject_GetBuffer family

def p_ba_removeprefix():
    b = bytearray(PAY)
    return len(b.removeprefix(Buf(b, b"PAY")))


def p_ba_removesuffix():
    b = bytearray(PAY)
    return len(b.removesuffix(Buf(b, b"AD-")))


def p_ba_replace_old():
    b = bytearray(PAY)
    return len(b.replace(Buf(b, b"P"), b"QQ"))


def p_ba_replace_new():
    b = bytearray(PAY)
    return len(b.replace(b"P", Buf(b, b"QQ")))


# ------------------------------------------------------------------ driver

PROBES = {k[2:]: v for k, v in sorted(globals().items()) if k.startswith("p_")}
for _m in ("find", "count", "index", "rfind", "rindex", "startswith", "endswith"):
    for _w in ("start", "end"):
        PROBES[f"ba_{_m}_{_w}"] = _slicefn(_m, _w)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = sorted(PROBES) if which == "all" else [which]
    for n in names:
        if n not in PROBES:
            print("probes:", " ".join(sorted(PROBES)))
            return 2
        probe(n, PROBES[n])
    for n, kind, detail in RESULTS:
        print(f"PROBE:{n}={kind} :: {detail}")
    print(f"PROBE:total={len(RESULTS)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:
        traceback.print_exc()
        sys.exit(3)
