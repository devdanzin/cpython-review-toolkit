import faulthandler, _testcapi
faulthandler.enable()
_testcapi.set_nomemory(1, 2)
try:
    type('X', (), {})
except MemoryError:
    print("clean MemoryError")
finally:
    try:
        _testcapi.remove_mem_hooks()
    except Exception:
        pass
print("survived")
