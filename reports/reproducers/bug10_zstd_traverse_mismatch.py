"""Bug: ZstdDecompressor_traverse missing Py_VISIT(self->unused_data)

decompressor.c:684-689:
    ZstdDecompressor_traverse:
        Py_VISIT(self->dict);
        return 0;  // MISSING: Py_VISIT(self->unused_data)

    ZstdDecompressor_clear:
        Py_CLEAR(self->dict);
        Py_CLEAR(self->unused_data);  // clear visits it, traverse doesn't

If unused_data participates in a reference cycle, the GC cycle
detector will never find it because traverse doesn't visit it.

Expected: reference cycle involving unused_data is never collected.
"""

import gc
import sys

try:
    import _zstd
except ImportError:
    try:
        import compression._zstd as _zstd
    except ImportError:
        print("_zstd module not available (new in 3.15)")
        sys.exit(0)

# Create a simple compressed payload to decompress
try:
    import zlib
    # Use a simple zstd-compressed payload
    data = b"Hello " * 1000
    compressed = _zstd.ZstdCompressor().compress(data)
except Exception as e:
    print(f"Cannot create test data: {e}")
    sys.exit(0)

# Create a decompressor and decompress partially to get unused_data
decompressor = _zstd.ZstdDecompressor()

# Feed compressed data + extra garbage to produce unused_data
try:
    result = decompressor.decompress(compressed + b"extra garbage data")
except Exception:
    pass

# Check if unused_data exists
unused = getattr(decompressor, 'unused_data', None)
print(f"unused_data: {unused!r}")
print(f"unused_data type: {type(unused)}")

# The traverse/clear mismatch means:
# - If unused_data is part of a reference cycle, GC won't find it
# - This is a GC-level leak, not directly observable from Python
# - The bug is confirmed by code inspection:
#   traverse visits: dict (only)
#   clear clears: dict, unused_data (both)

print()
print("The bug is confirmed by code inspection:")
print("  ZstdDecompressor_traverse: Py_VISIT(self->dict) only")
print("  ZstdDecompressor_clear: Py_CLEAR(self->dict) + Py_CLEAR(self->unused_data)")
print()
print("Fix: add Py_VISIT(self->unused_data) to ZstdDecompressor_traverse")
print("This is a one-line fix at Modules/_zstd/decompressor.c:687")
