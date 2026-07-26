"""Error-path agent, slice obj-sequences.

bytearrayobject.c:1609-1612 -- `bytearray_take_bytes_impl`:

    if (_PyBytes_Resize(&self->ob_bytes_object, to_take) == -1) {
        Py_DECREF(remaining);
        return NULL;                 <- self->ob_bytes_object is now NULL
    }

Every failure path of `_PyBytes_Resize` (bytesobject.c:3344-3400) stores NULL
into `*pv` after releasing the old object.  The bytearray is therefore left with
`ob_bytes_object == NULL` while `ob_bytes`, `ob_start` and `ob_size` still
describe the freed buffer -- and unlike the `bytearray.__new__` case the object
is fully constructed, so nothing downstream expects a NULL.

Guarded twin, same file, same call, 1329 lines up:
    bytearray_resize_lock_held:280-284 handles the identical failure by
    installing Py_CONSTANT_EMPTY_BYTES and zeroing size/alloc before returning.

Dense OOM injection via _testcapi.set_nomemory, sweeping the allocation index.

Usage:  <python> errpath_take_bytes_resize_fail.py [max_index]
"""

import subprocess
import sys

CHILD = r"""
import _testcapi, sys
i = int(sys.argv[1])
b = bytearray(b"X" * 4096)
b.take_bytes(4000)          # warm the code path once, no injection
b2 = bytearray(b"Y" * 4096)
_testcapi.set_nomemory(i, i + 1)
try:
    b2.take_bytes(4000)
except MemoryError:
    pass
except BaseException as exc:
    print("OTHER", type(exc).__name__)
_testcapi.remove_mem_hooks()
# now touch the possibly-dangling bytearray
print("LEN", len(b2))
print("VAL", bytes(b2)[:8])
b2.append(1)
print("OK")
"""


def main(argv):
    top = int(argv[1]) if len(argv) > 1 else 60
    exe = "/home/danzin/projects/python_build_matrix/builds/release-gil-nojit/python"
    if len(argv) > 2:
        exe = argv[2]
    interesting = 0
    for i in range(top):
        p = subprocess.run([exe, "-c", CHILD, str(i)],
                           capture_output=True, text=True, timeout=60)
        tag = "ok" if p.returncode == 0 else f"rc={p.returncode}"
        if p.returncode != 0 or "OTHER" in p.stdout:
            interesting += 1
            print(f"PROBE:idx={i} {tag}\n  out={p.stdout.strip()[:200]}"
                  f"\n  err={p.stderr.strip()[-400:]}", flush=True)
    print(f"PROBE:swept={top} interesting={interesting}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
