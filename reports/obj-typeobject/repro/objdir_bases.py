# merge_class_dict via object.__dir__ (:8526) -- no metaclass needed
class Fake: pass
a = Fake()
a.__bases__ = (a,)          # 1-node cycle

class C:
    @property
    def __class__(self):    # object.__dir__ reads self.__class__ -> arbitrary object
        return a

print("entering", flush=True)
dir(C())
print("survived", flush=True)
