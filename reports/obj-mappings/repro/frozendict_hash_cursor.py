"""CPY-0144 probe: frozendict_hash (dictobject.c:8446).

    PyObject *value;  // borrowed ref                       /* :8458 */
    Py_ssize_t pos = 0;
    Py_hash_t key_hash;
    while (_PyDict_Next(op, &pos, NULL, &value, &key_hash)) {   /* :8461 */
        Py_hash_t pair_hash = frozendict_pair_hash(key_hash, value);  /* :8462 */

`frozendict_pair_hash` calls PyObject_Hash(value) -> arbitrary user __hash__,
with a LIVE _PyDict_Next cursor (`pos`) and a BORROWED `value` outstanding.
That is CPY-0115's shape at a distinct site.

Also compares the hash-cache memory ordering against frozenset's:
    dictobject.c:8450  FT_ATOMIC_LOAD_SSIZE_RELAXED(self->ma_hash)   <- relaxed
    dictobject.c:8481  FT_ATOMIC_STORE_SSIZE_RELAXED(self->ma_hash)  <- relaxed
    setobject.c:1020   FT_ATOMIC_LOAD_SSIZE_RELAXED   (guard)
    setobject.c:1021   FT_ATOMIC_LOAD_SSIZE_ACQUIRE   (value)  <- acquire
    setobject.c:1025   FT_ATOMIC_STORE_SSIZE_RELEASE  (value)  <- release
"""

import gc
import sys

try:
    frozendict
except NameError:
    try:
        from builtins import frozendict  # type: ignore[attr-defined]
    except ImportError:
        print("NO frozendict BUILTIN -- probing via _testcapi/_testinternalcapi")
        frozendict = None

print("frozendict available:", frozendict is not None)
if frozendict is None:
    sys.exit(0)

fired = []


class Evil:
    """Value whose __hash__ runs while frozendict_hash holds a live cursor."""

    def __init__(self, n):
        self.n = n

    def __hash__(self):
        fired.append(self.n)
        # 1. force a GC while the cursor is live (frozendict has
        #    tp_clear = dict_tp_clear, dictobject.c:8588)
        gc.collect()
        # 2. build and drop a lot of dicts to churn the allocator
        for _ in range(20):
            d = {f"k{i}": i for i in range(40)}
            del d
        return self.n


# --- 1. plain: does the cursor survive a re-entrant GC? ------------------
fd = frozendict({f"k{i}": Evil(i) for i in range(64)})
h1 = hash(fd)
print("hash #1 =", h1, " __hash__ fired", len(fired), "times")
fired.clear()
h2 = hash(fd)
print("hash #2 =", h2, " __hash__ fired", len(fired), "times (0 => cached)")
print("cache consistent:", h1 == h2)

# --- 2. cycle: frozendict reachable only from a cycle, collected mid-hash?
fired.clear()


class Holder:
    pass


def cycle_probe():
    h = Holder()
    fd2 = frozendict({f"c{i}": Evil(1000 + i) for i in range(40)})
    h.fd = fd2
    fd2_self = h  # noqa: F841  (cycle: Holder -> frozendict -> ... )
    return hash(fd2)


print("cycle hash =", cycle_probe(), "fired", len(fired))

# --- 3. can a user __hash__ observe / mutate the frozendict? -------------
mutated = []


class Probe:
    def __init__(self, owner_box):
        self.box = owner_box

    def __hash__(self):
        fd3 = self.box[0]
        if fd3 is not None:
            try:
                fd3.clear()          # frozendict exposes no mutator
            except AttributeError as exc:
                mutated.append(("clear", type(exc).__name__))
            try:
                dict.__setitem__(fd3, "x", 1)
            except Exception as exc:  # noqa: BLE001
                mutated.append(("dict.__setitem__", type(exc).__name__))
            try:
                dict.clear(fd3)
            except Exception as exc:  # noqa: BLE001
                mutated.append(("dict.clear", type(exc).__name__))
        return 7


box = [None]
fd3 = frozendict({f"p{i}": Probe(box) for i in range(8)})
box[0] = fd3
print("mutation attempts from inside the cursor:", hash(fd3), mutated)

print("SURVIVED")
print("DONE")
