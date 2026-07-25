"""Bug: _pyrepl os.environ.get() not protected — same crash as _colorize fix

_colorize.py was fixed (8ef7735c) to use _safe_getenv() when
os.environ is replaced with a broken object. But 7+ locations in
_pyrepl still use bare os.environ.get() and will crash.

Expected: AttributeError or TypeError when os.environ is replaced.
"""

import os
import sys

# Save the real environ
real_environ = os.environ

class BrokenEnviron:
    """Replacement for os.environ that crashes on .get()"""
    def get(self, key, default=None):
        raise TypeError(f"BrokenEnviron.get({key!r})")
    def __getitem__(self, key):
        raise TypeError(f"BrokenEnviron[{key!r}]")
    def __contains__(self, key):
        raise TypeError(f"key in BrokenEnviron")

print("=== Testing _colorize (FIXED) ===")
os.environ = BrokenEnviron()
try:
    import _colorize
    result = _colorize.can_colorize()
    print(f"  can_colorize() = {result} (handled gracefully)")
except Exception as e:
    print(f"  CRASH: {type(e).__name__}: {e}")
finally:
    os.environ = real_environ

# Reimport to test _pyrepl modules
print()
print("=== Testing _pyrepl.pager (UNFIXED) ===")
os.environ = BrokenEnviron()
try:
    # Force reimport
    for mod_name in list(sys.modules):
        if '_pyrepl' in mod_name:
            del sys.modules[mod_name]

    from _pyrepl import pager
    # pager module accesses os.environ.get('MANPAGER'), etc.
    # at module level or in get_pager()
    try:
        p = pager.get_pager()
        print(f"  get_pager() = {p}")
    except TypeError as e:
        print(f"  BUG CONFIRMED — TypeError: {e}")
    except Exception as e:
        print(f"  Error: {type(e).__name__}: {e}")
except ImportError:
    print("  _pyrepl.pager not available")
except TypeError as e:
    print(f"  BUG CONFIRMED at import time — TypeError: {e}")
except Exception as e:
    print(f"  Error during import: {type(e).__name__}: {e}")
finally:
    os.environ = real_environ

print()
print("=== Testing _pyrepl.simple_interact (UNFIXED) ===")
os.environ = BrokenEnviron()
try:
    for mod_name in list(sys.modules):
        if '_pyrepl' in mod_name:
            del sys.modules[mod_name]

    from _pyrepl import simple_interact
    # simple_interact accesses os.environ.get("TERM", "")
    print(f"  Import succeeded (TERM accessed lazily)")
except TypeError as e:
    print(f"  BUG CONFIRMED at import — TypeError: {e}")
except Exception as e:
    print(f"  Error: {type(e).__name__}: {e}")
finally:
    os.environ = real_environ

print()
print("Locations that need _safe_getenv() or try/except:")
print("  _pyrepl/pager.py:27,31,35,92 — MANPAGER, PAGER, TERM, LINES")
print("  _pyrepl/simple_interact.py:49 — TERM")
print("  _pyrepl/terminfo.py:85,96,339 — TERMINFO, TERMINFO_DIRS, TERM")
print("  _pyrepl/trace.py:12 — PYREPL_TRACE")
