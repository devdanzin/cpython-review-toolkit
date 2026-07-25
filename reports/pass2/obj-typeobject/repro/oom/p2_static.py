# PASS-2 payload: managed static types (typeobject.c 228-522).
# Subclassing a static builtin drives _PyStaticType_* state and the
# tp_subclasses linkage for a type whose state lives in the interpreter's
# managed-static array rather than on the heap.
class S1(int):
    pass


class S2(str):
    __slots__ = ()


class S3(tuple):
    pass


class S4(BaseException):
    pass


_ = S1(1) + S1(2)
_ = S2("ab") + "c"
_ = S3((1, 2))[0]
_ = int.__subclasses__()
_ = str.__subclasses__()
S1.attr = 1
del S1.attr
del S1, S2, S3, S4
