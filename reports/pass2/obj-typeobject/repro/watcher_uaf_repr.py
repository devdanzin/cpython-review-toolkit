"""Same UAF, reached WITHOUT sys.unraisablehook.

PyErr_FormatUnraisable(":1223") formats "... for %R" -- %R is PyObject_Repr(type),
which for a type whose metaclass defines __repr__ in Python is arbitrary user code,
run inside _PyType_Modified_Unlocked's live PyDict_Next loop over tp_subclasses.
"""
import _testcapi

fired = []


class Meta(type):
    def __repr__(cls):
        if not fired:
            fired.append("repr-called")
            try:
                cls.__bases__ = (object,)
                fired.append("reparented")
            except Exception as exc:  # noqa: BLE001
                fired.append("reparent-failed: %r" % (exc,))
        return "<Meta instance>"


class C:
    pass


class D(C, metaclass=Meta):
    pass


C.probe = 1
D.probe2 = 1
assert C.probe == 1
assert D.probe2 == 1

wid = _testcapi.add_type_watcher(1)  # error watcher -> PyErr_FormatUnraisable
_testcapi.watch_type(wid, D)

C.trigger = 2

print("hook:", fired)
print("C subclasses now:", C.__subclasses__())
print("survived")
