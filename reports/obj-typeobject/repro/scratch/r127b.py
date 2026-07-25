import _testcapi, sys
ns = {}
exec("", ns)
_testcapi.set_nomemory(127, 128)
try:
    exec("type('X',(),{})", ns)
except MemoryError:
    pass
except Exception:
    pass
try:
    _testcapi.remove_mem_hooks()
except Exception:
    pass
sys.exit(0)
