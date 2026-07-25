import _testcapi
_testcapi.set_nomemory(127, 128)
try:
    type('X', (), {})
except MemoryError:
    pass
