import gc, sys, faulthandler
faulthandler.dump_traceback_later(6, exit=True)
class MyStr(str):
    def __eq__(self, other):
        print("  [in __eq__, world stopped] calling gc.collect()", flush=True)
        gc.collect()
        print("  [gc.collect() returned]", flush=True)
        return str.__eq__(self, other)
    def __ne__(self, o): return not self.__eq__(o)
    def __hash__(self): return str.__hash__(self)
    def __lt__(self, o): return str.__lt__(self, o)
class Base: pass
class A(Base): __slots__ = (MyStr("x"),)
class B(Base): __slots__ = (MyStr("x"),)
a = A(); keep = a
print("before", flush=True)
a.__class__ = B
print("AFTER -> no hang", flush=True)
faulthandler.cancel_dump_traceback_later()
