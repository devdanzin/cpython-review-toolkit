"""GIL-vs-FT differential probe for Objects/dictobject.c and Objects/setobject.c.

Single-threaded only. Every case exercises a code path that dictobject.c or
setobject.c implements differently in the `#ifdef Py_GIL_DISABLED` arm and the
`#else` arm. Prints a stable, diffable line per case so the two builds can be
compared with `diff`.

Run:  <build>/python dual_build_differential.py > out.txt
"""

import gc
import sys


def show(name, fn):
    try:
        r = fn()
    except BaseException as exc:  # noqa: BLE001 - we are recording the exception
        r = f"{type(exc).__name__}: {exc}"
    print(f"{name} :: {r!r}")


# ---------------------------------------------------------------- dict iterators


def iter_mutate_keys():
    """dictiter_iternextkey_lock_held (GIL) vs dictiter_iternext_threadsafe (FT)."""
    d = {"a": 1, "b": 2}
    it = iter(d)
    out = [next(it)]
    del d["a"]
    d["c"] = 3
    try:
        while True:
            out.append(next(it))
    except StopIteration:
        return ("StopIteration", out)
    except RuntimeError as exc:
        return (str(exc), out)


def iter_mutate_items():
    d = {"a": 1, "b": 2}
    it = iter(d.items())
    out = [next(it)]
    del d["a"]
    d["c"] = 3
    try:
        while True:
            out.append(next(it))
    except StopIteration:
        return ("StopIteration", out)
    except RuntimeError as exc:
        return (str(exc), out)


def iter_mutate_values():
    d = {"a": 1, "b": 2}
    it = iter(d.values())
    out = [next(it)]
    del d["a"]
    d["c"] = 3
    try:
        while True:
            out.append(next(it))
    except StopIteration:
        return ("StopIteration", out)
    except RuntimeError as exc:
        return (str(exc), out)


def iter_size_change():
    d = {"a": 1, "b": 2}
    it = iter(d)
    next(it)
    d["c"] = 3
    try:
        next(it)
    except RuntimeError as exc:
        return str(exc)


def iter_sticky_after_error():
    """di_used = -1 stickiness."""
    d = {"a": 1, "b": 2}
    it = iter(d)
    next(it)
    d["c"] = 3
    errs = []
    for _ in range(3):
        try:
            next(it)
        except BaseException as exc:  # noqa: BLE001
            errs.append(type(exc).__name__)
        else:
            errs.append("ok")
    return errs


def iter_len_hint_after_error():
    d = {"a": 1, "b": 2}
    it = iter(d)
    next(it)
    d["c"] = 3
    try:
        next(it)
    except RuntimeError:
        pass
    try:
        return it.__length_hint__()
    except BaseException as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def iter_reduce_after_error():
    d = {"a": 1, "b": 2}
    it = iter(d)
    next(it)
    d["c"] = 3
    try:
        next(it)
    except RuntimeError:
        pass
    return repr(it.__reduce__())


# ------------------------------------------------------- split tables / instances


class Inst:
    pass


def split_dict_order():
    """Instance __dict__ uses shared (split) keys; probe insertion order."""
    a = Inst()
    a.x = 1
    a.y = 2
    a.z = 3
    del a.y
    a.w = 4
    b = Inst()
    b.q = 9
    return (list(a.__dict__), list(b.__dict__))


def split_dict_iter_mutate():
    a = Inst()
    a.x = 1
    a.y = 2
    it = iter(a.__dict__)
    out = [next(it)]
    del a.x
    a.z = 3
    try:
        while True:
            out.append(next(it))
    except StopIteration:
        return ("StopIteration", out)
    except RuntimeError as exc:
        return (str(exc), out)


def split_keys_exhaustion():
    """Fill the shared-keys table past SHARED_KEYS_MAX_SIZE."""
    class T:
        pass

    objs = []
    for n in range(40):
        o = T()
        for i in range(n + 1):
            setattr(o, f"a{i}", i)
        objs.append(o)
    return (len(objs[-1].__dict__), sorted(objs[-1].__dict__) == sorted(f"a{i}" for i in range(40)))


def dummy_slot_reuse():
    """is_unusable_slot(): FT refuses DKIX_DUMMY slots, GIL reuses them."""
    d = {}
    for i in range(5):
        d[f"k{i}"] = i
    for i in range(5):
        del d[f"k{i}"]
    for i in range(5):
        d[f"n{i}"] = i
    return (list(d), len(d))


# ---------------------------------------------------------------- adversarial eq


class Evil:
    """__eq__ mutates the container being probed."""

    def __init__(self, target, action):
        self.target = target
        self.action = action
        self.fired = False

    def __hash__(self):
        return 42

    def __eq__(self, other):
        if not self.fired:
            self.fired = True
            try:
                self.action()
            except BaseException:  # noqa: BLE001
                pass
        return False


def dict_lookup_clears_during_eq():
    d = {}
    probe = Evil(d, lambda: d.clear())
    other = Evil(d, lambda: None)
    d[other] = 1
    try:
        return (probe in d, len(d))
    except BaseException as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def dict_lookup_raises_during_eq():
    class Boom:
        def __hash__(self):
            return 7

        def __eq__(self, other):
            raise ValueError("boom")

    d = {Boom(): 1}
    try:
        return Boom() in d
    except BaseException as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------------- sets


def set_iter_mutate():
    s = {1, 2, 3}
    it = iter(s)
    out = [next(it)]
    s.discard(next(iter(s - {out[0]})))
    s.add(99)
    try:
        while True:
            out.append(next(it))
    except StopIteration:
        return ("StopIteration", len(out))
    except RuntimeError as exc:
        return (str(exc), len(out))


def set_contains_during_eq():
    s = set()
    fired = []

    class E:
        def __hash__(self):
            return 5

        def __eq__(self, other):
            if not fired:
                fired.append(1)
                s.clear()
            return False

    s.add(E())
    try:
        return (E() in s, len(s))
    except BaseException as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def frozenset_contains_during_eq():
    """set_lookkey_threadsafe takes the PyFrozenSet_CheckExact fast path in FT."""
    fired = []
    holder = []

    class E:
        def __hash__(self):
            return 5

        def __eq__(self, other):
            if not fired:
                fired.append(1)
                if holder:
                    holder[0].clear()
            return False

    fs = frozenset([E()])
    s = set(fs)
    holder.append(s)
    return (E() in fs, len(fs))


def set_swap_bodies():
    a = {1, 2, 3}
    b = {4, 5}
    a ^= b
    return sorted(a)


def frozenset_hash_stability():
    fs = frozenset(range(10))
    h1 = hash(fs)
    h2 = hash(fs)
    return h1 == h2


# ----------------------------------------------------------------------- driver

CASES = [
    ("dict.iter_mutate_keys", iter_mutate_keys),
    ("dict.iter_mutate_items", iter_mutate_items),
    ("dict.iter_mutate_values", iter_mutate_values),
    ("dict.iter_size_change", iter_size_change),
    ("dict.iter_sticky_after_error", iter_sticky_after_error),
    ("dict.iter_len_hint_after_error", iter_len_hint_after_error),
    ("dict.iter_reduce_after_error", iter_reduce_after_error),
    ("dict.split_order", split_dict_order),
    ("dict.split_iter_mutate", split_dict_iter_mutate),
    ("dict.split_keys_exhaustion", split_keys_exhaustion),
    ("dict.dummy_slot_reuse", dummy_slot_reuse),
    ("dict.lookup_clears_during_eq", dict_lookup_clears_during_eq),
    ("dict.lookup_raises_during_eq", dict_lookup_raises_during_eq),
    ("set.iter_mutate", set_iter_mutate),
    ("set.contains_during_eq", set_contains_during_eq),
    ("set.frozenset_contains_during_eq", frozenset_contains_during_eq),
    ("set.swap_bodies", set_swap_bodies),
    ("set.frozenset_hash_stability", frozenset_hash_stability),
]


def main():
    for name, fn in CASES:
        show(name, fn)
        gc.collect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
