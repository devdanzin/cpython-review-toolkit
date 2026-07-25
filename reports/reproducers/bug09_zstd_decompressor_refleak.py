"""Bug: _zstd decompressor leaks 'value' ref when PyLong_AsInt(key) fails

_zstd/decompressor.c:109-114:
    Py_INCREF(key);
    Py_INCREF(value);
    int key_v = PyLong_AsInt(key);
    Py_DECREF(key);
    if (key_v == -1 && PyErr_Occurred()) {
        return -1;  // BUG: value still has extra refcount!
    }

The compressor (compressor.c:155-158) correctly does Py_DECREF(value)
before the early return. The decompressor omits it.

Expected: reference leak of the value object on each failed call.
"""

import sys

try:
    import _zstd
except ImportError:
    try:
        import compression._zstd as _zstd
    except ImportError:
        print("_zstd module not available (new in 3.15)")
        sys.exit(0)

# Trigger the leak by passing a non-int key in the options dict.
# PyLong_AsInt will fail with TypeError, and 'value' leaks.

class BadKey:
    """Key that fails PyLong_AsInt."""
    pass

value_object = object()
options = {BadKey(): value_object}

refcount_before = sys.getrefcount(value_object)

# Each failed call should leak one ref to value_object
ITERATIONS = 100
for _ in range(ITERATIONS):
    try:
        _zstd.ZstdDecompressor(options=options)
    except (TypeError, Exception):
        pass

refcount_after = sys.getrefcount(value_object)
leaked = refcount_after - refcount_before

print(f"Refcount before: {refcount_before}")
print(f"Refcount after {ITERATIONS} failed calls: {refcount_after}")
print(f"Leaked references: {leaked}")

if leaked > 0:
    print(f"\nBUG CONFIRMED: {leaked} references leaked!")
    print("The decompressor does not Py_DECREF(value) when")
    print("PyLong_AsInt(key) fails at decompressor.c:113-114.")
else:
    print("\nNo leak detected (bug may have been fixed).")
