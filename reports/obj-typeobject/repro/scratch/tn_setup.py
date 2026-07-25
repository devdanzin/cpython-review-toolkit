# Unarmed setup: warm every import / freelist the payload would otherwise burn.
import _testcapi  # noqa: F401


class Meta(type):
    pass


class Base:
    __slots__ = ("a",)


class BaseInitSub:
    def __init_subclass__(cls, **kw):
        pass


class Desc:
    def __set_name__(self, owner, name):
        pass


# warm up the machinery once, unarmed, so caches/freelists are populated
_warm = type("W", (Base,), {"__slots__": ("b", "c"), "__doc__": "d",
                            "__qualname__": "W.q", "x": Desc()})
del _warm
