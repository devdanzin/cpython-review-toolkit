"""Same bug through the builtin dir() rather than type.__dir__."""
class Fake:
    pass

a = Fake()
a.__bases__ = (a,)

class Meta(type):
    @property
    def __bases__(cls):
        return (a,)

class C(metaclass=Meta):
    pass

print("calling dir(C) ...", flush=True)
print(len(dir(C)), flush=True)
