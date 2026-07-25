# Setup for the CPY-0079 re-run OOM sweep (runs UNARMED, before set_nomemory).
# Purpose: build every dict/set shape the payload copies, and warm every code
# path once so that import / freelist / bytecode-specialisation allocations do
# not burn the injection budget.

class Inst:
    def __init__(self):
        self.a = 1
        self.b = 2
        self.c = 3


empty_dict = {}
empty_frozen = frozendict()
combined = {"k%d" % i: i for i in range(12)}
general = {i: i for i in range(12)}          # non-unicode keys -> DICT_KEYS_GENERAL
inst = Inst()                                 # split table via managed dict
sparse = {i: i for i in range(64)}
for i in range(0, 64, 2):                     # make it non-compact -> dict_merge path
    del sparse[i]
frozen_nonempty = frozendict(combined)
empty_set = set()
small_set = {1, 2, 3, 4, 5}
big_set = set(range(64))
frozen_set = frozenset(range(12))
empty_frozenset = frozenset()


def exercise():
    # --- dict copy family ---------------------------------------------------
    empty_dict.copy()               # ma_used == 0  -> dict_new_untracked  (CPY-0079)
    empty_frozen.copy()             # ma_used == 0  -> frozendict_new_untracked (CPY-0079)
    frozendict(empty_dict)
    combined.copy()                 # fast-copy -> clone_combined_dict_keys
    dict(combined)
    general.copy()
    inst.__dict__.copy()            # split table -> copy_values / new_values
    dict(inst.__dict__)
    sparse.copy()                   # non-compact -> dict_merge path
    frozen_nonempty.copy()
    frozendict(combined)
    dict.fromkeys(("x", "y", "z"))
    {**combined}
    combined | general
    # --- set copy family ----------------------------------------------------
    empty_set.copy()
    small_set.copy()
    big_set.copy()
    frozen_set.copy()
    empty_frozenset.copy()
    set(small_set)
    frozenset(small_set)
    small_set | big_set
    small_set & big_set
    small_set - big_set
    small_set ^ big_set
    set(range(8))
    # --- fresh instances: exercises _PyDict_NewKeysForClass / split keys -----
    Inst().__dict__.copy()


# Warm every path once, unarmed.
for _ in range(3):
    exercise()
