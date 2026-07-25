"""Bug: int i loop counter overflow in _pickle.c for data > 2GB

Same pattern as the CSV fix (97b0ef0) which changed int i to
Py_ssize_t i. The pickle module has 5 locations using int i to
iterate over potentially large data.

This requires >2GB of data to trigger, so we just demonstrate
the pattern and note the locations.

Expected: integer overflow → wrong behavior or crash on >2GB data.
"""

import pickle
import sys

print("Bug: _pickle.c uses 'int i' loop counters at 5 locations:")
print("  Line 2313: iteration in batch_list")
print("  Line 3457: iteration in batch_dict")
print("  Line 3598: iteration in batch_setitems")
print("  Line 5456: iteration in load_counted_tuple")
print("  Line 7027: iteration in load_setitems")
print()
print("When processing >2GB data (>2^31 elements), the int counter")
print("overflows to negative, causing wrong loop termination or")
print("out-of-bounds access.")
print()

# We can demonstrate the limit exists without actually allocating 2GB
# by checking that pickle handles large collections
print("=== Quick sanity check ===")
try:
    # A large-ish list to verify pickle works normally
    big = list(range(100_000))
    data = pickle.dumps(big, protocol=5)
    result = pickle.loads(data)
    assert len(result) == 100_000
    print(f"  pickle.dumps/loads of 100K list: OK ({len(data):,} bytes)")
except Exception as e:
    print(f"  Error: {e}")

print()
print("To trigger the overflow, you would need:")
print(f"  - A list with > {2**31:,} elements")
print(f"  - Or a dict with > {2**31:,} items")
print(f"  - This requires ~16-32GB of RAM minimum")
print()
print("The fix is mechanical: change 'int i' to 'Py_ssize_t i'")
print("at each of the 5 locations, same as the CSV fix.")
