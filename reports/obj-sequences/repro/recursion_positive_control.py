"""recursion-guard-auditor, slice obj-sequences: POSITIVE CONTROL.

Every scenario in `recursion_list_slot_matrix.py` and
`recursion_bytes_format_paths.py` came back rc=0 / RecursionError.  A negative
result is only worth something if the harness can produce a positive one, so
this reproduces two *known* members of the class on the same builds:

  * CPY-0001 / gh-154318 -- `tuple_hash` (Objects/tupleobject.c) descends
    through `PyObject_Hash`, which has NO recursion guard
    (Objects/object.c:1158).  A deeply-nested tuple overflows the native C
    stack.
  * CPY-0020 -- `ga_hash` (Objects/genericaliasobject.c:615/:619), same shape
    through both alias fields.

Expected:  rc = -11 / 139  (SIGSEGV)  -- an uncatchable native stack overflow.

Contrast with the slice's own hash story: `list` and `bytearray` are
unhashable (`PyObject_HashNotImplemented` / tp_hash == 0) and `bytes_hash`
(Objects/bytesobject.c:1719) is `Py_HashBuffer` over a flat byte range, so the
four slice files contribute ZERO nodes to the unguarded hash graph.

Usage:
    <python> recursion_positive_control.py            # all
    <python> recursion_positive_control.py <name>     # one, in-process
"""

import os
import subprocess
import sys

N = int(os.environ.get("RECUR_DEPTH", "400000"))


def s_tuple_hash_deep():
    """CPY-0001: unguarded PyObject_Hash descent. Expect SIGSEGV."""
    x = ()
    for _ in range(N):
        x = (x,)
    print("VAL built, hashing...", flush=True)
    print("VAL", hash(x))


def s_ga_hash_deep():
    """CPY-0020: types.GenericAlias.__hash__. Expect SIGSEGV."""
    x = int
    for _ in range(N):
        x = list[x]
    print("VAL built, hashing...", flush=True)
    print("VAL", hash(x))


def s_slice_types_are_not_hash_nodes():
    """The slice's own types cannot appear in an unguarded hash descent."""
    a = []
    for _ in range(1000):
        a = [a]
    try:
        hash((a,))
    except TypeError as exc:
        print("VAL tuple containing a list is unhashable:", exc)
    try:
        hash(bytearray(b"x"))
    except TypeError as exc:
        print("VAL bytearray unhashable:", exc)
    print("VAL bytes hash is a flat Py_HashBuffer:", hash(b"a" * 100000) != 0)
    while isinstance(a, list) and a:
        a = a.pop()


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
            timeout=900,
        )
        out = " | ".join(
            ln[:100]
            for ln in proc.stdout.splitlines()
            if ln.startswith(("PROBE", "VAL"))
        )
        print(f"{name:34s} rc={proc.returncode:<6d} {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
