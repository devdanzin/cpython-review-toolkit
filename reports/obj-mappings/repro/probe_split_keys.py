"""Probe: how dk_nentries / capacity behave for a plain class's shared keys.

Not a repro -- an instrument. `_testinternalcapi.get_object_dict_values(obj)`
returns a tuple of length `ht_cached_keys->dk_nentries`, so its length is a
direct read-out of dk_nentries.
"""

import sys

import _testinternalcapi as tic


def nentries(obj):
    v = tic.get_object_dict_values(obj)
    return None if v is None else len(v)


class C:
    pass


def main():
    print("SHARED_KEYS_MAX_SIZE =", tic.SHARED_KEYS_MAX_SIZE)
    o = C()
    print("fresh instance nentries =", nentries(o))
    for i in range(35):
        setattr(o, "a%02d" % i, i)
        n = nentries(o)
        print("  after a%02d -> nentries=%r  inline=%r" % (i, n, n is not None))
        if n is None:
            print("  (inline values gone -- dict materialised at attr #%d)" % i)
            break
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
