"""obj-mappings / refcount-auditor -- evidence script for the gdb check.

Shows that `frozenset - frozenset` reaches set_discard_entry() with an EXACT
frozenset receiver, i.e. CPython mutates an exact frozenset's table in place and
therefore reaches set_lookkey()'s PyFrozenSet_CheckExact branch --
set_compare_frozenset -- on a set whose table IS changing.

Driven under gdb:
  break set_discard_entry
  run <thisfile>
  p ((PyObject*)so)->ob_type->tp_name
"""

BIG = frozenset(range(200))
SMALL = frozenset([7])
print("start", flush=True)
OUT = BIG - SMALL
print("len(OUT) =", len(OUT), flush=True)
