"""FT-build structural probes (mapper handoff)."""
import sys
print(f"PROBE:interpreter={sys.version.splitlines()[0]}", flush=True)
print(f"PROBE:gil_enabled={sys._is_gil_enabled()}", flush=True)
b = bytearray()
b.extend(iter([1, 2, 3] * 200))          # bytearray_extend_impl -> bytearray_resize_lock_held(bytearray_obj)
print(f"PROBE:extend_private_resize=ok len={len(b)}", flush=True)
c = bytearray(b"AB"); c.clear(); mv = memoryview(c)
try:
    c.__init__("x", "ascii")
    print("PROBE:init_assert=NO_ABORT", flush=True)
except BaseException as e:
    print(f"PROBE:init_assert={type(e).__name__}", flush=True)
mv.release()
