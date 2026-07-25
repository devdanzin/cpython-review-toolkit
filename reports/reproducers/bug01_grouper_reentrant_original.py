import itertools

outer_grouper = None

class Key:
    def __init__(self, val, do_advance):
        self.val = val
        self.do_advance = do_advance

    def __eq__(self, other):
        if self.do_advance:
            self.do_advance = False
            try:
                next(outer_grouper)
            except StopIteration:
                pass
            return NotImplemented
        return self.val == other.val

    def __hash__(self):
        return hash(self.val)

values = [1, 1, 2]
keys_iter = iter([Key(1, True), Key(1, False), Key(2, False)])
g = itertools.groupby(values, lambda _: next(keys_iter))
outer_grouper = g
k, grp = next(g)
list(grp)  # use-after-free / crash under ASAN