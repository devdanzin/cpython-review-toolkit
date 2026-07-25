import sys

armed = [False]
calls = [0]

class Evil:
    def __hash__(self):
        return hash('__module__')
    def __eq__(self, other):
        calls[0] += 1
        if armed[0]:
            raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return False

X = type('X', (), {Evil(): 1})
print("type created ok; __eq__ calls during creation:", calls[0])
print("tp_dict keys:", list(X.__dict__.keys()))

armed[0] = True

# sanity: does a plain dict lookup of '__module__' on the type dict raise?
try:
    d = dict(X.__dict__)
    d['__module__']
    print("SANITY: plain dict lookup did NOT raise")
except KeyboardInterrupt as e:
    print("SANITY: plain dict lookup raised", e)
except BaseException as e:
    print("SANITY: plain dict lookup raised", type(e).__name__, e)

print("--- calling repr(X)  [type_repr -> type_module] ---")
try:
    r = repr(X)
    print("RESULT: repr(X) returned %r  -> EXCEPTION SWALLOWED" % (r,))
except BaseException as e:
    print("RESULT: repr(X) propagated", type(e).__name__, e)

print("--- calling repr(X())  [object_repr -> type_module] ---")
try:
    r = repr(X())
    print("RESULT: repr(X()) returned %r  -> EXCEPTION SWALLOWED" % (r,))
except BaseException as e:
    print("RESULT: repr(X()) propagated", type(e).__name__, e)

print("--- control: X.__module__ attribute access (type_get_module) ---")
try:
    m = X.__module__
    print("RESULT: X.__module__ ->", m)
except BaseException as e:
    print("RESULT: X.__module__ propagated", type(e).__name__, e)

print("--- control: PyType_GetFullyQualifiedName via %T / __qualname__ path ---")
try:
    print("RESULT: f-string %T:", "{}".format(X))
except BaseException as e:
    print("RESULT:", type(e).__name__, e)

print("total __eq__ calls:", calls[0])
