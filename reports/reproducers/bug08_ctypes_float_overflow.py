"""Bug: ctypes c_float silent overflow — same pattern fixed in struct.pack('f')

_ctypes/cfield.c:884 does:
    x = (float)PyFloat_AsDouble(value);
This silently truncates overflow to infinity instead of raising
OverflowError. The _struct.c fix (gh-145633) replaced the cast with
PyFloat_Pack4 which properly raises.

Expected: c_float(huge) silently produces inf (BUG)
         struct.pack('f', huge) raises OverflowError (FIXED)
"""

import ctypes
import struct
import sys

# Value that overflows float32 but fits in float64
huge = 3.5e+38  # FLT_MAX is ~3.4028235e+38

print(f"Test value: {huge} (> FLT_MAX ~3.4028235e+38)")
print()

# struct.pack was FIXED to raise OverflowError
print("=== struct.pack('f', huge) ===")
try:
    result = struct.pack('f', huge)
    val = struct.unpack('f', result)[0]
    print(f"  Packed successfully: {val}")
    if val == float('inf'):
        print(f"  BUG: silently produced infinity")
    else:
        print(f"  OK: value preserved")
except OverflowError as e:
    print(f"  OverflowError (CORRECT): {e}")

# ctypes c_float has the SAME bug, UNFIXED
print()
print("=== ctypes.c_float(huge) ===")
try:
    f = ctypes.c_float(huge)
    print(f"  c_float.value = {f.value}")
    if f.value == float('inf'):
        print(f"  BUG CONFIRMED: silently produced infinity!")
        print(f"  Should raise OverflowError like struct.pack('f')")
    else:
        print(f"  Value preserved (unexpected)")
except OverflowError as e:
    print(f"  OverflowError (would mean bug is fixed): {e}")

# Also test negative overflow
print()
print("=== ctypes.c_float(-huge) ===")
try:
    f = ctypes.c_float(-huge)
    print(f"  c_float.value = {f.value}")
    if f.value == float('-inf'):
        print(f"  BUG CONFIRMED: silently produced -infinity!")
except OverflowError as e:
    print(f"  OverflowError (would mean bug is fixed): {e}")

# Test via Structure field assignment too
print()
print("=== Structure with c_float field ===")
class S(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float)]

try:
    s = S()
    s.x = huge
    print(f"  s.x = {s.x}")
    if s.x == float('inf'):
        print(f"  BUG CONFIRMED in struct field assignment too")
except OverflowError as e:
    print(f"  OverflowError: {e}")
