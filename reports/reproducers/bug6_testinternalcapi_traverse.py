"""Bug 6: _testinternalcapi module_traverse/module_clear discards return value

_testinternalcapi.c:2662-2677:
    traverse_module_state(state, visit, arg);  // return value DISCARDED
    return 0;

Same pattern fixed in _interpqueuesmodule.c, _interpretersmodule.c,
_interpchannelsmodule.c (commits 6e5350d, 93a31be).

This is a test module (low severity), but the bug is real:
if traverse_module_state returns an error, module_traverse ignores it
and returns 0 (success), misleading the GC.

Not directly triggerable from Python (GC traverse errors are internal),
but we can verify the module loads and exercises the traverse path.
"""

import gc
import sys

try:
    import _testinternalcapi
    print(f"_testinternalcapi loaded: {_testinternalcapi}")

    # Force a GC collection which will traverse all modules
    gc.collect()
    gc.collect()

    print("GC traversal completed (traverse return value silently discarded)")
    print("The bug is that if traverse_module_state() returns an error,")
    print("module_traverse() at _testinternalcapi.c:2666 ignores it.")
    print("This is only observable by code inspection or under error injection.")

except ImportError:
    print("_testinternalcapi not available (test-only module)")
    print("This bug only affects CPython's test infrastructure.")
