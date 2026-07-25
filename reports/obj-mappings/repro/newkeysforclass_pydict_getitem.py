"""dictobject.c:7280 -- _PyDict_NewKeysForClass calls the error-discarding
PyDict_GetItem() on a class body dict that can hold attacker-chosen keys.

    PyObject *attrs = PyDict_GetItem(cls->ht_type.tp_dict,
                                     &_Py_ID(__static_attributes__));

Reached from Objects/typeobject.c:9480 type_ready_managed_dict, for every
heap type with Py_TPFLAGS_MANAGED_DICT -- i.e. every ordinary Python class.

type('C', (), ns) copies `ns` verbatim into tp_dict, so tp_dict can be a
DICT_KEYS_GENERAL dict containing a non-str key.  If that key's hash collides
with hash('__static_attributes__'), the probe calls its __eq__.

At 3.16.0a0 dict_getitem (:2425) saves/restores the ambient exception and
reports any non-KeyError through PyErr_FormatUnraisable, so this is NOT a
silent swallow -- it is an unraisable emitted from a plain type() call, whose
text tells the user to "consider using PyDict_GetItemRef()".

The __eq__ is armed only AFTER the namespace dict is built, so the raise
happens during type creation and nowhere else.
"""

import sys

TARGET = hash("__static_attributes__")
print("hash('__static_attributes__') =", TARGET, file=sys.stderr)

ARMED = False
calls = []


class Colliding:
    def __hash__(self):
        return TARGET

    def __eq__(self, other):
        calls.append(other)
        if ARMED:
            raise RuntimeError("__eq__ from a class-body key")
        return False


ns = {}
ns[Colliding()] = 1
ns["__static_attributes__"] = ("a", "b")
print("namespace keys:", [type(k).__name__ for k in ns], file=sys.stderr)
print("__eq__ calls while building the namespace:", len(calls), file=sys.stderr)

calls.clear()
ARMED = True
print("--- creating the class ---", file=sys.stderr)
C = type("C", (), ns)
print("--- created:", C, file=sys.stderr)
print("__eq__ invocations during creation:", len(calls), file=sys.stderr)

ARMED = False
c = C()
c.a = 1
c.b = 2
print("instance attrs ok:", c.__dict__, file=sys.stderr)
print("DONE", file=sys.stderr)
