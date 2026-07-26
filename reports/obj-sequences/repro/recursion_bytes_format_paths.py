"""recursion-guard-auditor, slice obj-sequences.

`bytes` and `bytearray` are not self-referential containers, so the classic
"nested container drives the slot into itself" shape is structurally absent.
This probe tests the paths that CAN re-enter through user Python:

  * `_PyBytes_FormatEx` (Objects/bytesobject.c:600-1170), the %-format engine
    shared by `bytes.__mod__` (:2847) and `bytearray.__mod__`
    (`bytearray_mod_lock_held`, Objects/bytearrayobject.c:2844):
        %b / %s -> format_obj  -> __bytes__          (:917)
        %d %i %u %x %X %o      -> __index__/__int__  (:965)
        %c      -> byte_converter -> __index__       (:1003)
        %f %e %g               -> __float__          (:986)
        %a                     -> PyObject_ASCII     (:904)
        %(key)s -> PyObject_GetItem(dict, key)       (:750)
  * `bytes.__contains__` / `bytearray.__contains__`
    (`_Py_bytes_contains`, Objects/bytes_methods.c:602) -> __index__ / __buffer__
  * `bytes(iterable)` (`_PyBytes_FromList/FromTuple/FromIterator`) -> __index__
  * the two C-level self-recursions in Objects/bytearrayobject.c
    (`bytearray_setslice:653`, `bytearray_ass_subscript_lock_held:806`)

Each scenario re-enters the SAME C entry point from the user hook, without
bound, so the only thing between it and a native stack overflow is the
eval loop's own `_Py_EnterRecursiveCallTstate` (Python/ceval.c:1255).

    RecursionError  -> bounded          (rc = 0)
    SIGSEGV / 139   -> the bug class

Usage:
    <python> recursion_bytes_format_paths.py            # every scenario
    <python> recursion_bytes_format_paths.py <name>     # one, in-process
"""

import os
import subprocess
import sys

# High enough that an unguarded C descent overflows well before it finishes.
N = int(os.environ.get("RECUR_N", "1000000"))


# --------------------------------------------------------------------------
# %-format: user hook re-enters the same format engine
# --------------------------------------------------------------------------


def s_mod_bytes_dunder():
    class R:
        def __bytes__(self):
            return b"%b" % R()  # re-enter _PyBytes_FormatEx

    print("VAL", b"%b" % R())


def s_mod_bytearray_dunder():
    class R:
        def __bytes__(self):
            return bytes(bytearray(b"%b") % R())

    print("VAL", bytearray(b"%b") % R())


def s_mod_index():
    class R:
        def __index__(self):
            return b"%d" % R() and 1  # re-enter through formatlong

    print("VAL", b"%d" % R())


def s_mod_char_converter():
    class R:
        def __index__(self):
            return b"%c" % R() and 65  # re-enter through byte_converter

    print("VAL", b"%c" % R())


def s_mod_float():
    class R:
        def __float__(self):
            return float(b"%f" % R() and 1.0)

    print("VAL", b"%f" % R())


def s_mod_ascii():
    class R:
        def __repr__(self):
            return repr(b"%a" % R())  # %a -> PyObject_ASCII -> PyObject_Repr

    print("VAL", b"%a" % R())


def s_mod_mapping():
    class M:
        def __getitem__(self, k):
            return b"%(k)s" % M()  # re-enter through PyObject_GetItem

    print("VAL", b"%(k)s" % M())


def s_mod_bytearray_mapping():
    class M:
        def __getitem__(self, k):
            return bytes(bytearray(b"%(k)s") % M())

    print("VAL", bytearray(b"%(k)s") % M())


# --------------------------------------------------------------------------
# sq_contains -> _Py_bytes_contains (bytes_methods.c, SHARED by both types)
# --------------------------------------------------------------------------


def s_contains_index_bytes():
    class R:
        def __index__(self):
            return (R() in b"hello") and 1

    print("VAL", R() in b"hello")


def s_contains_index_bytearray():
    class R:
        def __index__(self):
            return (R() in bytearray(b"hello")) and 1

    print("VAL", R() in bytearray(b"hello"))


def s_contains_index_depth_counter():
    """Prove the `__contains__` -> __index__ recursion really is bounded.

    The plain `contains_index_*` scenarios come back as TypeError rather than
    RecursionError, because `_Py_bytes_contains` (bytes_methods.c:608) does an
    UNNARROWED `PyErr_Clear()` on the `PyNumber_AsSsize_t` failure path and then
    retries the buffer protocol -- so the RecursionError the guard raised is
    swallowed and replaced (this is the error-path agent's F2, seen from the
    recursion side).  The counter shows the descent still terminated at the
    guard rather than for some other reason.
    """
    depth = [0]
    peak = [0]

    class R:
        def __index__(self):
            depth[0] += 1
            peak[0] = max(peak[0], depth[0])
            try:
                return (R() in b"hello") and 1
            finally:
                depth[0] -= 1

    try:
        print("VAL", R() in b"hello")
    finally:
        print("VAL peak __index__ nesting reached:", peak[0])


def s_contains_buffer():
    class R:
        def __buffer__(self, flags):
            return memoryview(bytes([R() in b"hello"]))

    print("VAL", R() in b"hello")


# --------------------------------------------------------------------------
# bytes(iterable) constructors -- _PyBytes_FromList / FromTuple / FromIterator
# --------------------------------------------------------------------------


def s_bytes_from_list_index():
    class R:
        def __index__(self):
            return bytes([R()]) and 1

    print("VAL", bytes([R()]))


def s_bytes_from_tuple_index():
    class R:
        def __index__(self):
            return bytes((R(),)) and 1

    print("VAL", bytes((R(),)))


def s_bytes_from_iterator_index():
    class R:
        def __index__(self):
            return bytes(iter([R()])) and 1

    print("VAL", bytes(iter([R()])))


def s_bytearray_setitem_index():
    class R:
        def __index__(self):
            b = bytearray(b"x")
            b[0] = R()
            return 1

    b = bytearray(b"x")
    b[0] = R()
    print("VAL", b)


# --------------------------------------------------------------------------
# the two C-level self-recursions in bytearrayobject.c -- structural bound 1
# --------------------------------------------------------------------------


def s_setslice_self_alias():
    # bytearray_setslice:648-655 -- `values == self` copies and recurses once
    b = bytearray(b"abcdef")
    for _ in range(10000):
        b[1:3] = b
        del b[8:]
    print("VAL setslice self-alias ok, len=", len(b))


def s_ass_subscript_self_alias():
    # bytearray_ass_subscript_lock_held:794-808 -- same shape, extended slice
    b = bytearray(b"abcdef")
    for _ in range(10000):
        b[::2] = bytearray(b"xyz")
    print("VAL ass_subscript self-alias ok, len=", len(b))


def s_ass_subscript_nonbytearray():
    # non-bytearray `values` takes the copy-and-recurse arm; a __iter__ that
    # re-enters gives Python-level depth on top of the C recursion.
    class It:
        def __iter__(self):
            b = bytearray(b"abcdef")
            b[::2] = It()
            return iter([1, 2, 3])

    b = bytearray(b"abcdef")
    b[::2] = It()
    print("VAL", b)


# --------------------------------------------------------------------------
# deep-nesting controls: bytes/bytearray simply cannot nest
# --------------------------------------------------------------------------


def s_no_nesting_possible():
    # A bytearray can only hold ints 0..255; it can never contain another
    # bytes/bytearray, so no element descent exists to be unguarded.
    b = bytearray(b"abc")
    try:
        b[0] = bytearray(b"x")
    except TypeError as exc:
        print("VAL bytearray element must be int:", exc)
    try:
        bytes([bytes(1)])
    except TypeError as exc:
        print("VAL bytes element must be int:", exc)
    print("VAL bytes hash is flat:", hash(b"abc") == hash(b"abc"))
    try:
        hash(bytearray(b"abc"))
    except TypeError as exc:
        print("VAL bytearray unhashable:", exc)


SCENARIOS = {k[2:]: v for k, v in sorted(globals().items()) if k.startswith("s_")}


def main(argv):
    if len(argv) > 1:
        name = argv[1]
        try:
            SCENARIOS[name]()
        except RecursionError as exc:
            print(f"PROBE:{name}=RecursionError ({exc})", flush=True)
            return 0
        except BaseException as exc:  # noqa: BLE001 - probe
            print(f"PROBE:{name}={type(exc).__name__}: {str(exc)[:70]}", flush=True)
            return 0
        print(f"PROBE:{name}=completed", flush=True)
        return 0

    for name in SCENARIOS:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), name],
            capture_output=True,
            text=True,
            timeout=600,
        )
        out = " | ".join(
            ln[:110]
            for ln in proc.stdout.splitlines()
            if ln.startswith(("PROBE", "VAL"))
        )
        tail = proc.stderr.strip().splitlines()[-1:] if proc.stderr.strip() else []
        print(f"{name:28s} rc={proc.returncode:<5d} {out}  {' '.join(tail)[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
