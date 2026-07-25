import sys

print("=== 6149: _PyObject_HashDictKey(name) failure -> PyErr_Clear ===")

class S(str):
    def __hash__(self):
        raise KeyboardInterrupt("EXC-FROM-USER-__hash__")

try:
    r = getattr(int, S('bit_length'))
    print("RESULT: getattr returned %r -> EXCEPTION SWALLOWED" % (r,))
except AttributeError as e:
    print("RESULT: AttributeError %s  -> original exception REPLACED/SWALLOWED" % (e,))
except BaseException as e:
    print("RESULT: propagated", type(e).__name__, e)

print()
print("=== 6183: dict-lookup DKIX_ERROR in a type's tp_dict -> PyErr_Clear ===")

armed = [False]
calls = [0]

class Evil:
    def __hash__(self):
        return hash('zzz')
    def __eq__(self, other):
        calls[0] += 1
        if armed[0]:
            raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return False

Y = type('Y', (), {Evil(): 1})
print("Y dict keys:", list(Y.__dict__.keys()))
armed[0] = True

try:
    d = dict(Y.__dict__)
    d['zzz']
except KeyError:
    print("SANITY: plain dict lookup gave KeyError (no collision reached!)")
except BaseException as e:
    print("SANITY: plain dict lookup raised", type(e).__name__, e)

try:
    r = getattr(Y, 'zzz')
    print("RESULT: getattr(Y,'zzz') returned %r -> SWALLOWED" % (r,))
except AttributeError as e:
    print("RESULT: AttributeError %r -> original exception SWALLOWED/REPLACED" % (str(e),))
except BaseException as e:
    print("RESULT: propagated", type(e).__name__, e)

try:
    r = hasattr(Y, 'zzz')
    print("RESULT: hasattr(Y,'zzz') ->", r)
except BaseException as e:
    print("RESULT: hasattr propagated", type(e).__name__, e)

print("total Evil.__eq__ calls:", calls[0])
