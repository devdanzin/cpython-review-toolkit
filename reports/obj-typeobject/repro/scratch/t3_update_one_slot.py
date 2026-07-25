"""find_name_in_mro's swallow reaches update_one_slot -> a slot is silently NULLed."""
armed = [False]
calls = [0]

class Evil:
    def __hash__(self):
        return hash('__init__')
    def __eq__(self, other):
        calls[0] += 1
        if armed[0]:
            raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return False

Base = type('Base', (), {Evil(): 1})
print("Base dict keys:", list(Base.__dict__.keys()))

class C(Base):
    def __init__(self):
        print("   C.__init__ ran")

print("before:  C() ->", end=" ")
C()
print("before:  C(1,2,3) ->", end=" ")
try:
    C(1, 2, 3)
    print("   accepted extra args (WRONG)")
except TypeError as e:
    print("   TypeError:", e)

armed[0] = True
print("--- del C.__init__  (triggers update_slot -> find_name_in_mro walks Base) ---")
try:
    del C.__init__
    print("del succeeded (no exception propagated)")
except BaseException as e:
    print("del propagated", type(e).__name__, e)

armed[0] = False
print("after:   C() ->", end=" ")
try:
    C()
    print("   ok (no __init__)")
except BaseException as e:
    print("  ", type(e).__name__, e)

print("after:   C(1,2,3) ->", end=" ")
try:
    C(1, 2, 3)
    print("   ACCEPTED EXTRA ARGS -> tp_init was silently NULLed")
except TypeError as e:
    print("   TypeError (correct):", e)
except BaseException as e:
    print("  ", type(e).__name__, e)

print("C.__init__ is object.__init__?", C.__init__ is object.__init__)
print("total Evil.__eq__ calls:", calls[0])
