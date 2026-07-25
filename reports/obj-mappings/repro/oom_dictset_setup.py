# Broad OOM sweep over Objects/dictobject.c + Objects/setobject.c.
# Setup runs UNARMED: it builds every object the payload touches and warms every
# path once so imports / freelists / specialisation do not burn the budget.


class Inst:
    def __init__(self):
        self.a = 1
        self.b = 2
        self.c = 3


class Slotted:
    __slots__ = ("x",)


base = {"k%d" % i: i for i in range(10)}
gen = {i: i for i in range(10)}
seq2 = [("s%d" % i, i) for i in range(10)]
pool = [Inst() for _ in range(64)]
pool_i = [0]
sset = set(range(10))
fset = frozenset(range(10))
lst = list(range(10))


def exercise():
    # --- dict construction / conversion ------------------------------------
    dict(base)
    dict(seq2)
    dict(**base)
    dict.fromkeys(lst)
    dict.fromkeys(lst, 0)
    {**base, **gen}
    base | gen
    frozendict(base)
    # --- dict mutation / resize --------------------------------------------
    d = {}
    for k, v in seq2:
        d[k] = v
    d.update(base)
    d.setdefault("zz", 1)
    d.pop("zz", None)
    d.popitem()
    d.copy()
    d.clear()
    # --- dict views + set algebra on views ---------------------------------
    base.keys() | gen.keys()
    base.keys() & gen.keys()
    base.keys() - gen.keys()
    base.keys() ^ gen.keys()
    base.items() | gen.items()
    list(base.items())
    list(reversed(base))
    repr(base)
    # --- managed dict / inline values / split keys -------------------------
    inst = pool[pool_i[0] % len(pool)]
    pool_i[0] += 1
    inst.d = 4                       # split-table insert past __init__ keys
    inst.__dict__                    # materialise
    inst.__dict__.copy()
    inst.__dict__ = {"a": 1}         # _PyObject_SetManagedDict, non-NULL dict
    del inst.a                       # detach / delete on a materialised dict
    s = Slotted()
    s.x = 1
    # --- set construction / mutation / resize ------------------------------
    t = set()
    for i in lst:
        t.add(i)
    t.update(sset)
    t.discard(1)
    t.pop()
    t.copy()
    t.clear()
    set(lst)
    frozenset(lst)
    sset | fset
    sset & fset
    sset - fset
    sset ^ fset
    sset.union(lst)
    sset.intersection(lst)
    sset.symmetric_difference(lst)
    sset.difference(lst)
    repr(sset)
    hash(fset)


for _ in range(4):
    exercise()
