#!/usr/bin/env python3
"""`PyBytesWriter_Format` uses `bytes_fromformat`'s result without a NULL check.

    Objects/bytesobject.c:3910-3924   PyBytesWriter_Format
    Objects/bytesobject.c:196         bytes_fromformat  (static char *, returns NULL on error)
    Objects/bytesobject.c:383-387     PyBytes_FromFormatV -- the guarded twin

```c
3920     char *buf = bytes_fromformat(writer, pos, format, vargs);
3923     Py_ssize_t size = buf - byteswriter_data(writer);   /* buf may be NULL */
3924     return PyBytesWriter_Resize(writer, size);
```

`bytes_fromformat` returns NULL on every one of its error paths -- an
out-of-range `%c` argument (`:271-277`, `OverflowError`), an unsupported format
character, and any `PyBytesWriter_Grow` failure inside it (`MemoryError`).
`PyBytesWriter_Format` neither tests it nor propagates: it subtracts the
writer's data pointer from NULL -- undefined behaviour, and in practice a large
negative `Py_ssize_t` -- and hands that to `PyBytesWriter_Resize`, whose
`size < 0` guard raises `ValueError: size must be >= 0` *over* the exception
`bytes_fromformat` had already set.

The guarded twin is 3,537 lines up in the same file and calls the same helper:

```c
383      char *s = bytes_fromformat(writer, 0, format, vargs);
384      if (s == NULL) {
385          PyBytesWriter_Discard(writer);
386          return NULL;
387      }
```

`PyBytesWriter_Format` is public C API since 3.15; the only in-tree caller is
`Modules/_testcapi/bytes.c:179` (`writer.format_i`), which is what this script
drives.

Usage
    python nullsafe_byteswriter_format_unchecked.py <python> [...]
"""

import subprocess
import sys

CHILD = r'''
import sys, faulthandler
faulthandler.enable()
import _testcapi

CASE = {case!r}

def writer(alloc=0, string=b""):
    return _testcapi.PyBytesWriter(alloc, string, 0)

if CASE == "overflow_c":
    # bytes_fromformat's %c range check sets OverflowError and returns NULL.
    w = writer()
    try:
        w.format_i(b"x=%c", 300)
    except BaseException as e:
        print("R=%s: %s" % (type(e).__name__, e))
        print("CONTEXT=%s" % (type(e.__context__).__name__,))
    else:
        print("R=NO-EXCEPTION finish=%r" % (w.finish(),))

elif CASE == "control_ok":
    w = writer()
    w.format_i(b"x=%i", 123456)
    print("R=%r" % (w.finish(),))

elif CASE == "control_twin":
    # PyBytes_FromFormatV, the guarded twin, on the same %c overflow.
    try:
        _testcapi.bytes_fromformat_c(300)
    except BaseException as e:
        print("R=%s: %s" % (type(e).__name__, e))
    except SystemError as e:
        print("R=SystemError: %s" % e)

print("DONE")
'''


def run(python, case):
    return subprocess.run([python, "-c", CHILD.format(case=case)],
                          capture_output=True, text=True, timeout=120)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    python = sys.argv[1]
    for case in (sys.argv[2:] or ["control_ok", "overflow_c"]):
        p = run(python, case)
        out = " | ".join(x for x in p.stdout.splitlines() if x)
        print(f"  {case:<14} rc={p.returncode:<4} {out}")
        if p.returncode not in (0, 1):
            print("      " + "\n      ".join(p.stderr.strip().splitlines()[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
