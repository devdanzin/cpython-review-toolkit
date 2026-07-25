"""CPY-0143 probe v2 -- sharper: can the un-restarted `set_next` cursor in
set_intersection (setobject.c:1743-1761) / set_difference_untracked
(setobject.c:2103, :2136) SKIP an element?

v1 mutated the iterated set by growing then shrinking it back, and observed no
divergence.  v2 tries the two shapes v1 did not:
  (a) a mutation that leaves the table SMALLER than it was (forces the cursor's
      index past the new end -> the tail of the set is never visited);
  (b) a mutation that only reinserts an element at a LOWER probe index (the
      cursor has already passed it -> the element is skipped).

Every round compares the C answer against the pure-Python answer computed from
the operands' final contents.
"""

import sys

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60

skips = []
extras = []


class Elem:
    __slots__ = ("n",)

    target = None
    mode = None
    armed = False

    def __init__(self, n):
        self.n = n

    def __hash__(self):
        return self.n % 5

    def __eq__(self, other):
        if type(other) is not Elem:
            return NotImplemented
        if Elem.armed and Elem.target is not None:
            Elem.armed = False
            try:
                Elem.mode(Elem.target)
            finally:
                Elem.armed = True
        return self.n == other.n

    def __repr__(self):
        return f"E{self.n}"


def grow_then_shrink(s):
    """Leave `s` with a table full of dummies, then force a shrink-resize."""
    tmp = [Elem(5000 + i) for i in range(400)]
    for e in tmp:
        s.add(e)
    for e in tmp:
        s.discard(e)
    # set_add_entry resizes (possibly downward) when fill*5 >= mask*3
    s.add(Elem(9999))
    s.discard(Elem(9999))


def reinsert_low(s):
    """Remove and re-add an element so it lands at a fresh probe index."""
    for n in (0, 1, 2):
        e = Elem(n)
        if e in s:
            s.discard(e)
            s.add(Elem(n))


def run(mode, op):
    global skips, extras
    for r in range(ROUNDS):
        small = {Elem(i) for i in range(6)}
        big = {Elem(i) for i in range(3, 40)}
        Elem.target, Elem.mode, Elem.armed = small if op == "and" else big, mode, True
        try:
            if op == "and":
                res = big & small
            else:
                res = big - small
        except RuntimeError:
            Elem.armed = False
            continue
        Elem.armed = False
        Elem.target = Elem.mode = None
        sn = {e.n for e in small}
        bn = {e.n for e in big}
        want = (bn & sn) if op == "and" else (bn - sn)
        got = {e.n for e in res}
        if got - want:
            extras.append((op, mode.__name__, r, sorted(got - want)))
        if want - got:
            skips.append((op, mode.__name__, r, sorted(want - got)))


for op in ("and", "sub"):
    for mode in (grow_then_shrink, reinsert_low):
        run(mode, op)

print(f"rounds per (op,mode): {ROUNDS}; combinations: 4")
print("SKIPPED elements (want-got):", skips[:10], f"... total {len(skips)}")
print("EXTRA   elements (got-want):", extras[:10], f"... total {len(extras)}")
print("DONE")
