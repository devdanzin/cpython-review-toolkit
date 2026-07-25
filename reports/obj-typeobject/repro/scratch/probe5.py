# type_new_set_names / type_new_init_subclass: is the USER exception preserved on every path?
def mk(exc, where):
    try:
        if where == "set_name":
            class D:
                def __set_name__(self, owner, name): raise exc
            class C: x = D()
        elif where == "set_name_lookup":
            class Meta(type):
                @property
                def __set_name__(cls): raise exc
            class D(metaclass=Meta): pass
            class C: x = D()
        elif where == "init_subclass":
            class B:
                def __init_subclass__(cls, **kw): raise exc
            class C(B): pass
    except BaseException as e:
        return (where, type(e).__name__, str(e),
                getattr(e, "__notes__", None),
                type(e.__cause__).__name__ if e.__cause__ else None)
    return (where, "NO EXCEPTION RAISED", None, None, None)

for w in ("set_name", "set_name_lookup", "init_subclass"):
    for exc in (KeyboardInterrupt("ctrl-c"), MemoryError("oom"), SystemExit(7)):
        print(mk(exc, w))
