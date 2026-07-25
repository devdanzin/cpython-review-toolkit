import sys
TEMPLATE = """
import faulthandler, _testcapi, sys
faulthandler.enable()
ns = {{}}
exec({setup!r}, ns)
_testcapi.set_nomemory({a}, {b})
try:
    exec({code!r}, ns)
except MemoryError:
    pass
except Exception as e:
    pass
try:
    _testcapi.remove_mem_hooks()
except Exception:
    pass
sys.exit(0)
"""
sys.stdout.write(TEMPLATE.format(setup="", code="type('X',(),{})", a=int(sys.argv[1]), b=int(sys.argv[1])+1))
