# Armed payload: drive type_new_impl through as many `goto error` points as
# possible -- __slots__, __qualname__, __doc__, __set_name__, __init_subclass__,
# a metaclass, and a multi-base MRO (PyType_Ready / mro_internal).
type("A", (Base,), {"__slots__": ("b", "c"), "__doc__": "docstring",
                    "__qualname__": "A.q", "x": Desc()})
type("B", (BaseInitSub,), {"__doc__": "d2"})
Meta("C", (Base, BaseInitSub), {"__slots__": ("z",), "__qualname__": "C.q"})
