#!/usr/bin/env python3
"""A NEGATIVE ``Py_SIZE(bytearray)`` defeats every bounds check in the slice
that is spelled ``i >= Py_SIZE(x)`` or ``slicelength <= 0``.

CPY-0186 established the *write* sink: ``bytearray_setslice_linear``'s stale
bpo-19568 recovery (`Objects/bytearrayobject.c:603`) executes
``Py_SET_SIZE(self, 0 + growth)`` on an object ``bytearray_resize_lock_held``
has already zeroed, so ``Py_SIZE(self)`` becomes negative and ``ob_start``
points at ``_PyRuntime.static_objects.singletons.bytes_empty.ob_sval``.

This script asks the *other* half of the question -- what happens when that
object is READ.  The primary answer, in `bytearray_subscript_lock_held`
(`Objects/bytearrayobject.c:502-527`, the `mp_subscript` slot -- a different
function and a different slot from CPY-0186's `mp_ass_subscript`), is an
out-of-bounds READ handed straight back to Python:

    505  slicelength = PySlice_AdjustIndices(PyByteArray_GET_SIZE(self),
    506                                       &start, &stop, step);
    508  if (slicelength <= 0)
    509      return PyByteArray_FromStringAndSize("", 0);
    510  else if (step == 1) {
    511      return PyByteArray_FromStringAndSize(
    512          PyByteArray_AS_STRING(self) + start, slicelength);

With ``length < 0``, `PySlice_AdjustIndices` (`Objects/sliceobject.c:257-296`)
clamps a non-negative ``start`` to ``length`` itself (:271-273) and a negative
``stop`` to ``0`` (:275-279), so ``start < stop`` holds with
``start == length < 0`` and the function returns a POSITIVE
``slicelength == -length``.  ``b[0:-1]`` therefore reads ``-length`` bytes
*before* ``ob_start`` and returns them as a bytearray.  The extended-slice
branch at :524-526 walks ``source_buf[cur]`` from the same negative ``cur``
(``cur`` is a ``size_t``, so the negative ``start`` wraps to the same address).

Scenarios
    slice_read   b[0:-1] after a 4 KB corruption -- ~3.9 KB of `_PyRuntime`
                 disclosed to Python.  rc=0 on every build: inspect the VALUE.
    ext_slice    b[0:-1:2] -- the extended-slice branch, same origin.
    big_read     b[0:-1] after a 1 MB corruption -- runs off the front of the
                 346 KB `_PyRuntime` global.  ASan global-buffer-overflow.
    read_matrix  52 further read paths across the four slice files, ONE PROBE
                 PER SUBPROCESS so a crash does not hide the probes after it.
    control      the same operations with NO injection.

Usage
    python nullsafe_negative_size_reads.py <python> [scenario ...]
    python nullsafe_negative_size_reads.py <python> --n <scenario> <index>
"""

import subprocess
import sys

# scenario -> (size, lo, bytes_len, probe)
SCENARIOS = {
    "slice_read":  (4_000,     100,     4, "b[0:-1]"),
    "ext_slice":   (4_000,     100,     4, "b[0:-1:2]"),
    "big_read":    (1_000_000, 200_000, 4, "b[0:-1]"),
}

# label -> expression evaluated against the corrupted bytearray `b`
PROBES = [
    ("b[0:-1]",     "b[0:-1]"),
    ("b[0:-1:2]",   "b[0:-1:2]"),
    ("b[:]",        "b[:]"),
    ("b[0]",        "b[0]"),
    ("b[-1]",       "b[-1]"),
    ("bytes",       "bytes(b)"),
    ("repr",        "repr(b)[:40]"),
    ("hex",         "b.hex()[:40]"),
    ("decode",      "b.decode('latin-1')[:40]"),
    ("find",        "b.find(b'x')"),
    ("count",       "b.count(b'x')"),
    ("index",       "b.index(b'x')"),
    ("in",          "b'x' in b"),
    ("startswith",  "b.startswith(b'x')"),
    ("endswith",    "b.endswith(b'x')"),
    ("split",       "len(b.split())"),
    ("split_sep",   "len(b.split(b'x'))"),
    ("rsplit",      "len(b.rsplit())"),
    ("splitlines",  "len(b.splitlines())"),
    ("strip",       "b.strip()"),
    ("lstrip",      "b.lstrip()"),
    ("rstrip",      "b.rstrip()"),
    ("partition",   "b.partition(b'x')[0]"),
    ("rpartition",  "b.rpartition(b'x')[0]"),
    ("replace",     "b.replace(b'x', b'y')"),
    ("translate",   "b.translate(None)"),
    ("center",      "b.center(4)"),
    ("ljust",       "b.ljust(4)"),
    ("zfill",       "b.zfill(4)"),
    ("expandtabs",  "b.expandtabs()"),
    ("upper",       "b.upper()"),
    ("title",       "b.title()"),
    ("isalpha",     "b.isalpha()"),
    ("copy",        "b.copy()"),
    ("join",        "b.join([b'a', b'b'])"),
    ("concat",      "b + b'z'"),
    ("iconcat",     "b.__iadd__(b'z')"),
    ("repeat",      "b * 2"),
    ("mod",         "b % ()"),
    ("eq",          "b == bytearray(b'x')"),
    ("lt",          "b < bytearray(b'x')"),
    ("reduce_ex",   "type(b.__reduce_ex__(2))"),
    ("sizeof",      "b.__sizeof__()"),
    ("iter_next",   "next(iter(b))"),
    ("list",        "len(list(b))"),
    ("memoryview",  "bytes(memoryview(b))"),
    ("take_bytes",  "b.take_bytes()"),
    ("int",         "int(b)"),
    ("float",       "float(b)"),
    ("compile",     "type(compile(b, '<s>', 'exec'))"),
    ("removeprefix", "b.removeprefix(b'x')"),
    ("maketrans",   "type(bytearray.maketrans(b'a', b'b'))"),
]
PROBE_MAP = dict(PROBES)

CHILD = r'''
import sys, faulthandler
faulthandler.enable()
import _testcapi

SIZE, LO, BL, N, EXPR = {size}, {lo}, {bl}, {n}, {expr!r}
src = bytes(b'\xAA' * BL)

# Warm the path once UNARMED so the injection budget lands on the slice
# assignment, not on the compiler or the freelists.
b = bytearray(b'x' * SIZE)
try:
    b[LO:SIZE] = src
except MemoryError:
    pass
b = bytearray(b'x' * SIZE)

if N >= 0:
    _testcapi.set_nomemory(N, N + 1)
try:
    b[LO:SIZE] = src
    r = "no-exception"
except MemoryError:
    r = "MemoryError"
except BaseException as e:
    r = "%s: %s" % (type(e).__name__, e)
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
print("OP:%s" % r)
sys.stdout.flush()

try:
    print("SIZE:len=%r" % len(b))
except BaseException as e:
    print("SIZE:len=EXC %s: %s" % (type(e).__name__, e))
sys.stdout.flush()

try:
    v = eval(EXPR)
except BaseException as e:
    print("R=EXC %s: %s" % (type(e).__name__, e))
else:
    if isinstance(v, (bytes, bytearray)):
        print("R=len(%d) %r%s" % (len(v), bytes(v[:24]),
                                  "..." if len(v) > 24 else ""))
    else:
        print("R=%r" % (v,))
sys.stdout.flush()
print("DONE")
'''


def run(python, size, lo, bl, n, expr):
    src = CHILD.format(size=size, lo=lo, bl=bl, n=n, expr=expr)
    return subprocess.run([python, "-c", src], capture_output=True,
                          text=True, timeout=600)


def fmt(p):
    out = " | ".join(x for x in p.stdout.splitlines() if x)
    san = "ASAN" if "Sanitizer" in p.stderr else ""
    return f"rc={p.returncode:5d} {san:5s} {out}"


def show(p, prefix=""):
    print(prefix + fmt(p))
    if "Sanitizer" in p.stderr or p.returncode not in (0, 1):
        print("        " + "\n        ".join(
            p.stderr.strip().splitlines()[:14]))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    python, rest = sys.argv[1], sys.argv[2:]

    if rest and rest[0] == "--n":
        size, lo, bl, expr = SCENARIOS[rest[1]]
        show(run(python, size, lo, bl, int(rest[2]), expr))
        return 0

    todo = rest or (list(SCENARIOS) + ["read_matrix"])
    for sc in todo:
        if sc == "read_matrix":
            print("### read_matrix   (size=4000 lo=100 bytes_len=4, n=2, "
                  "one subprocess per probe)")
            print(f"  {'probe':<14} {'CONTROL (no injection)':<52} CORRUPTED")
            for label, expr in PROBES:
                c = run(python, 4000, 100, 4, -1, expr)
                d = run(python, 4000, 100, 4, 2, expr)
                cl = [x for x in c.stdout.splitlines() if x.startswith("R")]
                dl = [x for x in d.stdout.splitlines() if x.startswith("R")]
                flag = ""
                if d.returncode != 0:
                    flag = f"  <<< rc={d.returncode}"
                if "Sanitizer" in d.stderr:
                    flag += "  <<< ASAN"
                print(f"  {label:<14} {(cl[0] if cl else 'rc=%d' % c.returncode)[:50]:<52}"
                      f"{(dl[0] if dl else 'rc=%d' % d.returncode)[:70]}{flag}")
                if d.returncode != 0 or "Sanitizer" in d.stderr:
                    head = [ln for ln in d.stderr.strip().splitlines()[:8]]
                    print("      " + "\n      ".join(head))
            print()
            continue
        size, lo, bl, expr = SCENARIOS[sc]
        print(f"### {sc}   (size={size} lo={lo} bytes_len={bl} expr={expr})")
        show(run(python, size, lo, bl, -1, expr),
             prefix="  control (no injection)  ")
        for n in range(0, 8):
            p = run(python, size, lo, bl, n, expr)
            if (p.returncode != 0 or "Sanitizer" in p.stderr
                    or "no-exception" not in p.stdout):
                show(p, prefix=f"  n={n:<3d}                   ")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
