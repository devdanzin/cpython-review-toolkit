"""CPY-0143 probe: two live `set_next` cursors held across calls that run user
__eq__/__hash__, with NO restart loop, in the same file as
set_add_entry_takeref (:253) which HAS one.

  * set_intersection            setobject.c:1719, loop at :1743-1761
        while (set_next(other, &pos, &entry)) {
            key = entry->key; hash = entry->hash; Py_INCREF(key);
            rv = set_contains_entry(so, key, hash);      <-- runs user __eq__
            ... set_add_entry(result, key, hash) ...     <-- runs user __eq__

  * set_difference_untracked    setobject.c:2084, loops at :2103 and :2136
        while (set_next(so, &pos, &entry)) {
            key = entry->key; hash = entry->hash; Py_INCREF(key);
            rv = _PyDict_Contains_KnownHash(other, key, hash);  /  set_contains_entry
            ... set_add_entry(result, key, hash) ...

error-path-analyzer bounded set_intersection as NOT a UAF (index-based cursor,
key is INCREF'd) but flagged that it "can silently skip or duplicate elements".
set_difference_untracked was never adjudicated.  This probe answers both by
comparing the C result against the pure-Python semantics.
"""

import sys

BAD = []


def report(name, got, want, note=""):
    ok = got == want
    if not ok:
        BAD.append(name)
    print(f"  {name}: {'ok' if ok else 'DIVERGES'}  got={got}  want={want} {note}")


# --------------------------------------------------------------- helpers
class Mutator:
    """Hashable element whose __eq__ mutates a target set."""

    target = None
    action = None
    fired = 0

    __slots__ = ("n",)

    def __init__(self, n):
        self.n = n

    def __hash__(self):
        return self.n % 3          # dense collisions -> __eq__ runs often

    def __eq__(self, other):
        if type(other) is not Mutator:
            return NotImplemented
        if Mutator.target is not None and Mutator.action is not None:
            Mutator.fired += 1
            act, Mutator.action = Mutator.action, None
            act(Mutator.target)
            Mutator.action = act
        return self.n == other.n

    def __repr__(self):
        return f"M{self.n}"


def blow_up(s):
    """Force set_table_resize on `s` and move every entry."""
    for i in range(300, 460):
        s.add(Mutator(i))
    for i in range(300, 460):
        s.discard(Mutator(i))


# ------------------------------------------------- 1. set_intersection
def probe_intersection():
    small = {Mutator(i) for i in range(6)}
    big = {Mutator(i) for i in range(40)}
    baseline = {m.n for m in small} & {m.n for m in big}

    Mutator.target, Mutator.action, Mutator.fired = small, blow_up, 0
    try:
        res = big & small          # so=big (larger), other=small (iterated)
    except RuntimeError as exc:
        print("  probe_intersection raised RuntimeError:", exc)
        Mutator.target = Mutator.action = None
        return
    Mutator.target = Mutator.action = None

    got = sorted(m.n for m in res)
    report(
        "set_intersection element set",
        got,
        sorted(baseline),
        f"(__eq__ fired {Mutator.fired}x, |res|={len(res)})",
    )


# --------------------------------------- 2. set_difference_untracked
def probe_difference():
    # need (len(so) >> 2) <= len(other) so we take set_difference_untracked
    # and NOT set_copy_and_difference_untracked
    so = {Mutator(i) for i in range(20)}
    other = {Mutator(i) for i in range(10, 30)}
    baseline = {m.n for m in so} - {m.n for m in other}

    Mutator.target, Mutator.action, Mutator.fired = so, blow_up, 0
    try:
        res = so - other
    except RuntimeError as exc:
        print("  probe_difference raised RuntimeError:", exc)
        Mutator.target = Mutator.action = None
        return
    Mutator.target = Mutator.action = None

    got = sorted(m.n for m in res)
    report(
        "set_difference_untracked element set",
        got,
        sorted(baseline),
        f"(__eq__ fired {Mutator.fired}x, |res|={len(res)})",
    )


# ---------------------- 3. duplicates?  a set cannot hold duplicates, but
# the cursor CAN revisit an entry, which shows up as extra __eq__ traffic and
# as a result whose length disagrees with the pure-Python answer.  Also probe
# whether the result ends up holding an element that is NOT in the operands.
def probe_alien_element():
    small = {Mutator(i) for i in range(5)}
    big = {Mutator(i) for i in range(30)}
    operand_ns = {m.n for m in small} | {m.n for m in big}

    Mutator.target, Mutator.action = small, blow_up
    try:
        res = big & small
    except RuntimeError:
        Mutator.target = Mutator.action = None
        return
    Mutator.target = Mutator.action = None
    alien = sorted(m.n for m in res if m.n not in operand_ns)
    report("no element from outside the operands", alien, [])


for fn in (probe_intersection, probe_difference, probe_alien_element):
    print(fn.__name__)
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        print("  raised", type(exc).__name__, exc)

print("DIVERGENCES:", BAD)
print("DONE")
sys.exit(0)
