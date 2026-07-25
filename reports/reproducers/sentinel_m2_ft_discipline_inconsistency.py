"""M2: Inconsistent FT discipline on self->name within the same object.

Two read paths exist for sentinel.name:

1. Member descriptor s.__name__:
   PyMember_GetOne (Python/structmember.c:117-131) for Py_T_OBJECT_EX uses
   FT_ATOMIC_LOAD_PTR + _Py_TryIncrefCompare + critical-section fallback.
   Result on cleared sentinel: clean AttributeError.

2. tp_repr (Objects/sentinelobject.c:129-134) and tp_reduce (line 148):
       return Py_NewRef(self->name);
   Bare load, no atomic, no critical section.
   Result on cleared sentinel: NULL deref / SIGSEGV.

The asymmetry is observable from a single thread by exploiting the brief
window during sentinel_clear (after Py_CLEAR(self->name) sets name=NULL,
before tp_free runs). A str-subclass `__del__` runs during that window and
can observe the inconsistency directly.

Tested on: ~/projects/ft_cpython/python (3.15a8 FT), PYTHON_GIL=1.
Expected:
  - s.__name__ raises AttributeError (FT-correct path)
  - repr(s) crashes  (FT-incorrect path)
"""
import builtins
import ctypes
import sys
import traceback


sentinel_addr = [None]
results = {"name_attr": None, "repr": None}


class EvilName(str):
    """During this str's __del__ (which runs inside Py_CLEAR(self->name) of
    the sentinel), self->name has already been set to NULL. We resurrect the
    sentinel via ctypes and probe both access paths."""

    def __del__(self):
        addr = sentinel_addr[0]
        if addr is None:
            return
        sentinel_addr[0] = None
        try:
            s = ctypes.cast(addr, ctypes.py_object).value
        except Exception as e:
            results["name_attr"] = f"resurrect failed: {e}"
            return

        # Probe 1: s.__name__ via member descriptor (FT-correct path).
        # self->name is NULL right now; PyMember_GetOne should raise AttributeError.
        try:
            name = s.__name__
            results["name_attr"] = f"unexpected: got {name!r}"
        except AttributeError as e:
            results["name_attr"] = f"AttributeError (clean): {e}"
        except SystemError as e:
            results["name_attr"] = f"SystemError: {e}"
        except Exception as e:
            results["name_attr"] = f"{type(e).__name__}: {e}"

        # Probe 2: repr(s) via tp_repr (FT-incorrect path).
        # tp_repr does `Py_NewRef(self->name)` with NULL → crash.
        try:
            r = repr(s)
            results["repr"] = f"unexpected: got {r!r}"
        except SystemError as e:
            results["repr"] = f"SystemError: {e}"
        except Exception as e:
            results["repr"] = f"{type(e).__name__}: {e}"


def trigger():
    s = builtins.sentinel(EvilName("victim"))
    sentinel_addr[0] = id(s)
    del s  # triggers sentinel_dealloc → sentinel_clear → name's __del__


print("Triggering the inconsistency...")
try:
    trigger()
except SystemError as e:
    print(f"SystemError during trigger: {e}")

print()
print("Probe results from inside EvilName.__del__:")
print(f"  s.__name__ (atomic load path):  {results['name_attr']}")
print(f"  repr(s)    (bare Py_NewRef):    {results['repr']}")
print()
print("If the first is 'AttributeError' and we never see the second probe's")
print("result (because the script crashed in repr()), that demonstrates the")
print("FT-discipline inconsistency: same field, different safety levels.")
print()
print("On non-crashing runs, both probes return SystemError or AttributeError,")
print("but the asymmetry in the access patterns is real and triggers under TSan.")
