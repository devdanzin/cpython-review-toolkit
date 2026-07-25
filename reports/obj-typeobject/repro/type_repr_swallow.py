"""type_repr / object_repr swallow an exception raised by a user __eq__.

Objects/typeobject.c:2403-2405

    PyObject *mod = type_module(type);
    if (mod == NULL) {
        PyErr_Clear();          // <-- bare clear, no PyErr_ExceptionMatches
    }

type_module() does a PyDict_GetItemRef(tp_dict, &_Py_ID(__module__)). A class
dict may hold non-string keys -- type_new_impl:4960 only warns -- so that lookup
can dispatch a user __eq__, and whatever it raises is discarded here.

Guarded twin: type_add_method:8614-8620 narrows the IDENTICAL type_module()
failure with PyErr_ExceptionMatches(PyExc_AttributeError). Two more callers
propagate (type_get_module:1637, _PyType_GetFullyQualifiedName:1669) -- 3 right,
2 wrong. Blame shows the twin (2024, gh-115231) postdates the outlier
(gh-111696).

Run on any build. Exit code is 0 either way: the defect is a lost exception, not
a crash, so read the RESULT lines.
"""

import warnings

warnings.simplefilter("ignore")

CALLS = []
ARMED = False


class Evil:
    """A non-string dict key whose __eq__ raises once armed.

    It must hash into the same bucket as '__module__' to be compared against it,
    so it borrows that string's hash. Arming is deferred because type creation
    itself looks up '__module__' -- an always-raising key aborts the class
    statement before the interesting call is ever reached.
    """

    def __hash__(self):
        return hash("__module__")

    def __eq__(self, other):
        CALLS.append(other)
        if ARMED:
            raise KeyboardInterrupt("EXC-FROM-USER-__eq__")
        return NotImplemented


def probe(label, fn):
    before = len(CALLS)
    try:
        value = fn()
    except KeyboardInterrupt as exc:
        print(f"RESULT {label}: PROPAGATED {exc}  (__eq__ calls: {len(CALLS) - before})")
        return "propagated"
    except BaseException as exc:  # noqa: BLE001 -- any other type is also a loss
        print(
            f"RESULT {label}: REPLACED by {type(exc).__name__}: {exc}"
            f"  (__eq__ calls: {len(CALLS) - before})"
        )
        return "replaced"
    print(f"RESULT {label}: SWALLOWED -> {value!r}  (__eq__ calls: {len(CALLS) - before})")
    return "swallowed"


X = type("X", (), {Evil(): 1})


class Meta(type):
    pass


Y = Meta("Y", (), {Evil(): 1})
inst = Y()

ARMED = True  # every __eq__ from here on raises

print("--- sanity: the key really does raise on comparison ---")
probe("dict['__module__']", lambda: X.__dict__["__module__"])

print("\n--- the defect: type_repr (typeobject.c:2405) ---")
type_repr_verdict = probe("repr(X)", lambda: repr(X))

print("\n--- the same shape in object_repr (typeobject.c:7490) ---")
object_repr_verdict = probe("repr(instance-of-Y)", lambda: repr(inst))

print("\n--- guarded twin: type_get_module propagates (typeobject.c:1637) ---")
probe("X.__module__", lambda: X.__module__)

print(
    f"\nSUMMARY type_repr={type_repr_verdict} object_repr={object_repr_verdict}"
    f" total_eq_calls={len(CALLS)}"
)
