"""Enumerate every type defined in dictobject.c / setobject.c and report whether
T.__new__(T) succeeds (i.e. an instance can be built with no C-level constructor).

Prints one line per type.  Runs no methods -- construction only.
"""

import sys

d = {"a": 1}
s = {1, 2}
fs = frozenset({1, 2})

TYPES = {
    "dict": dict,
    "dict_keys": type(d.keys()),
    "dict_values": type(d.values()),
    "dict_items": type(d.items()),
    "dict_keyiterator": type(iter(d.keys())),
    "dict_valueiterator": type(iter(d.values())),
    "dict_itemiterator": type(iter(d.items())),
    "dict_reversekeyiterator": type(reversed(d.keys())),
    "dict_reversevalueiterator": type(reversed(d.values())),
    "dict_reverseitemiterator": type(reversed(d.items())),
    "set": set,
    "frozenset": frozenset,
    "set_iterator": type(iter(s)),
}

# frozendict is new in 3.16 and may not be exposed under a stable name.
for name in ("frozendict",):
    t = getattr(__builtins__, name, None) if not isinstance(__builtins__, dict) \
        else __builtins__.get(name)
    if t is not None:
        TYPES[name] = t

try:
    import _testcapi  # noqa: F401
except ImportError:
    pass

print("build:", sys.version.split()[0], "|", sys.executable)
for name, T in TYPES.items():
    try:
        obj = T.__new__(T)
        print(f"CONSTRUCTED  {name:28s} -> {object.__repr__(obj)}")
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSED      {name:28s} -> {type(exc).__name__}: {exc}")
