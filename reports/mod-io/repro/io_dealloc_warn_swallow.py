"""Differential: does `_dealloc_warn` swallow a KeyboardInterrupt raised by the
underlying raw object's `_dealloc_warn`?

Target: Modules/_io/bufferedio.c:495 (_io__Buffered__dealloc_warn_impl)
Guarded twin: Modules/_io/fileio.c:103 (fileio_dealloc_warn) -- saves with
PyErr_GetRaisedException, narrows to PyExc_Warning, reports via
PyErr_FormatUnraisable, restores.

Oracle: Lib/_pyio.py:863 _BufferedIOMixin._dealloc_warn -- plain call, no swallow.

usage: python io_dealloc_warn_swallow.py [io|_pyio]
"""

import sys

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "io":
    import io as mod
else:
    import _pyio as mod


class Raw(mod.RawIOBase):
    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return False

    def readinto(self, b):
        return 0

    def _dealloc_warn(self, source):
        raise KeyboardInterrupt("raised by raw._dealloc_warn")


r = Raw()
b = mod.BufferedReader(r)

print(f"backend={backend}")

# Case 1: direct public-method call, _io._Buffered._dealloc_warn (METH_O).
try:
    res = b._dealloc_warn(b)
except KeyboardInterrupt as e:
    print("  case1 direct  : PROPAGATED KeyboardInterrupt:", e)
except BaseException as e:
    print(f"  case1 direct  : {type(e).__name__}: {e}")
else:
    print(f"  case1 direct  : returned {res!r} -- KeyboardInterrupt SWALLOWED")

# Case 2: through close(), with the writable `_finalizing` member set from
# pure Python (bufferedio.c:2578 {"_finalizing", Py_T_BOOL, ..., 0}).
r2 = Raw()
b2 = mod.BufferedReader(r2)
try:
    b2._finalizing = True
except AttributeError as e:
    print("  case2 close() : cannot set _finalizing:", e)
else:
    try:
        b2.close()
    except KeyboardInterrupt as e:
        print("  case2 close() : PROPAGATED KeyboardInterrupt:", e)
    except BaseException as e:
        print(f"  case2 close() : {type(e).__name__}: {e}")
    else:
        print("  case2 close() : returned normally -- KeyboardInterrupt SWALLOWED")

print("  exc_info after:", sys.exc_info())
