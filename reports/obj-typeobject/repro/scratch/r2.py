import faulthandler, _testcapi, sys
faulthandler.enable()
_testcapi.set_nomemory(1)
try:
    type('X', (), {})
except MemoryError:
    sys.stdout.write("clean MemoryError\n")
