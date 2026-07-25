# Unarmed setup for the managed-static-types region (typeobject.c 228-522):
# _PyStaticType_GetState / managed_static_type_state_init / _PyStaticType_InitBuiltin
# and the per-interpreter static-type state array.
import _testcapi

# Warm the subinterpreter machinery and the static-type paths unarmed.
_ = int.__mro__
_ = type.__subclasses__(object)


class SubOfStatic(int):
    pass


class SubOfStr(str):
    pass


_ = SubOfStatic(3) + 1
_ = SubOfStr("x").upper()
_ = _testcapi.__name__
