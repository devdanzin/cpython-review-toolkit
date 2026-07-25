import dis, sys, types

class C:
    def m(self):
        def inner():
            return self          # makes `self` a CELL (CO_FAST_CELL on slot 0)
        return super()           # makes `__class__` a FREEVAR

orig = C.m.__code__
print("argcount", orig.co_argcount, "cellvars", orig.co_cellvars,
      "freevars", orig.co_freevars, file=sys.stderr)
code = bytearray(orig.co_code)

MAKE_CELL = dis.opmap["MAKE_CELL"]
NOP = dis.opmap["NOP"]
# locate `MAKE_CELL 0` in the prologue (it follows COPY_FREE_VARS)
idx = None
for i in range(0, 12, 2):
    print(f"  [{i}] {dis.opname[code[i]]} {code[i+1]}", file=sys.stderr)
    if code[i] == MAKE_CELL and code[i + 1] == 0:
        idx = i
assert idx is not None, "no MAKE_CELL 0 found"
print("patching MAKE_CELL 0 at offset", idx, "-> NOP", file=sys.stderr)

# co_localspluskinds[0] still says CO_FAST_CELL, but localsplus[0] now holds
# the RAW argument. _PyCode_CODE(co)[0] is still COPY_FREE_VARS, so the
# debug-only assert at typeobject.c:12837 still passes.
code[idx] = NOP
new = orig.replace(co_code=bytes(code))
f = types.FunctionType(new, globals(), "m", None, C.m.__closure__)

print("calling f(1.5): PyCell_GetRef will read the float's double bits "
      "(0x3FF8000000000000) as a PyObject*", file=sys.stderr)
sys.stderr.flush()
r = f(1.5)
print("returned", r, file=sys.stderr)
