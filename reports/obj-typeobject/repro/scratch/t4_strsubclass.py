"""Same swallows, reached with a str SUBCLASS key -> no RuntimeWarning at all."""
import warnings
warnings.simplefilter("error", RuntimeWarning)   # prove no non-string-key warning fires

armed = [False]

class S(str):
    def __eq__(self, other):
        if armed[0]:
            raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return str.__eq__(self, other)
    __hash__ = str.__hash__

X = type('X', (), {S('__module__'): 'modname'})
print("created with RuntimeWarning-as-error: OK")
print("dict keys:", [type(k).__name__ + ':' + str(k) for k in X.__dict__.keys()])

armed[0] = True
print("--- repr(X) [type_repr:2405] ---")
try:
    print("  RESULT:", repr(repr(X)), "-> SWALLOWED")
except BaseException as e:
    print("  RESULT: propagated", type(e).__name__, e)

print("--- repr(X()) [object_repr:7490] ---")
try:
    print("  RESULT:", repr(repr(X())), "-> SWALLOWED")
except BaseException as e:
    print("  RESULT: propagated", type(e).__name__, e)

print("--- control X.__module__ [type_get_module, no clear] ---")
try:
    print("  RESULT:", X.__module__)
except BaseException as e:
    print("  RESULT: propagated", type(e).__name__, e)

print()
print("=== slot desync via find_name_in_mro:6183, str-subclass key ===")
armed2 = [False]

class S2(str):
    def __eq__(self, other):
        if armed2[0]:
            raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return str.__eq__(self, other)
    __hash__ = str.__hash__

Base = type('Base', (), {S2('__init__'): 'decoy'})

class C(Base):
    def __init__(self):
        pass

armed2[0] = True
try:
    del C.__init__
    print("del C.__init__ succeeded silently")
except BaseException as e:
    print("del propagated", type(e).__name__, e)
armed2[0] = False
try:
    C(1, 2, 3)
    print("RESULT: C(1,2,3) ACCEPTED -> tp_init NULLed;  C.__init__ is object.__init__ ->",
          C.__init__ is object.__init__)
except TypeError as e:
    print("RESULT: TypeError (correct):", e)
