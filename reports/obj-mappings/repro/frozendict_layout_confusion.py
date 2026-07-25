"""Probe the layout boundary between dict and frozendict.

PyFrozenDictObject == PyDictObject + a trailing Py_hash_t ma_hash.  If a
dict-layout instance could be retyped to a frozendict-layout type, frozendict_hash
(Objects/dictobject.c:8447) would read ma_hash past the end of the allocation.

Routes tried:
  A  __class__ assignment between heap subclasses of dict and frozendict
  B  multiple inheritance mixing the two solid bases
  C  frozendict.__hash__ invoked on a dict via the unbound slot
  D  dict.__init__ / dict.update invoked on a frozendict (mutating an immutable)
"""

import sys

_b = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
frozendict = _b.get("frozendict")
if frozendict is None:
    print("no frozendict in this build; nothing to probe")
    sys.exit(0)

print("build:", sys.version.replace("\n", " "))
print("dict basicsize      :", dict.__basicsize__)
print("frozendict basicsize:", frozendict.__basicsize__)


def check(label, fn):
    try:
        r = fn()
        print(f"  ALLOWED  {label}: {r!r}")
    except BaseException as exc:  # noqa: BLE001
        print(f"  refused  {label}: {type(exc).__name__}: {exc}")


class D(dict):
    pass


class F(frozendict):
    pass


print("\nA. __class__ assignment")


def a1():
    x = D(a=1)
    x.__class__ = F
    return hash(x)


def a2():
    x = F(a=1)
    x.__class__ = D
    return len(x)


check("D() -> F then hash()", a1)
check("F() -> D then len()", a2)

print("\nB. multiple inheritance across the two solid bases")
check("class M(dict, frozendict)", lambda: type("M", (dict, frozendict), {}))
check("class M(frozendict, dict)", lambda: type("M", (frozendict, dict), {}))

print("\nC. frozendict.__hash__ applied to a plain dict")
check("frozendict.__hash__({})", lambda: frozendict.__hash__({}))
check("frozendict.__hash__(D())", lambda: frozendict.__hash__(D(a=1)))
check("type(frozendict()).__hash__ via slot on dict subclass",
      lambda: frozendict.__dict__["__hash__"](D(a=1)))

print("\nD. dict mutators applied to a frozendict")
check("dict.__init__(fd, {'x': 1})", lambda: dict.__init__(frozendict(), {"x": 1}))
check("dict.update(fd, {'x': 1})", lambda: dict.update(frozendict(), {"x": 1}))
check("dict.__setitem__(fd, 'x', 1)",
      lambda: dict.__setitem__(frozendict(), "x", 1))
check("dict.clear(fd)", lambda: dict.clear(frozendict()))
check("dict.pop(fd, 'a')", lambda: dict.pop(frozendict(a=1), "a"))
check("dict.popitem(fd)", lambda: dict.popitem(frozendict(a=1)))
check("dict.setdefault(fd, 'x', 1)",
      lambda: dict.setdefault(frozendict(), "x", 1))
check("dict.__ior__(fd, {'x': 1})", lambda: dict.__ior__(frozendict(), {"x": 1}))

print("\nE. frozendict.__hash__ on a subclass built by __new__ only")
check("F.__new__(F) then hash()", lambda: hash(F.__new__(F)))
check("frozendict.__new__(frozendict) then hash()",
      lambda: hash(frozendict.__new__(frozendict)))

print("\ndone")
