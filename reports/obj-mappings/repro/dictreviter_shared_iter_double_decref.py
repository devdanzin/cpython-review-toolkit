"""Shared `reversed(dict)` iterator -> double-DECREF of the dict.

Objects/dictobject.c:

  dictreviter_iternext(self)                          :6344
      PyDictObject *d = di->di_dict;                  :6347  <-- READ, NO LOCK
      if (d == NULL) return NULL;                     :6349
      Py_BEGIN_CRITICAL_SECTION(d);                   :6353  <-- locks the DICT
      value = dictreviter_iter_lock_held(d, self);    :6354
      Py_END_CRITICAL_SECTION();

  dictreviter_iter_lock_held(d, self)                 :6254
      while (entry_ptr->me_value == NULL) {           :6284  <-- long walk over
          if (--i < 0) goto fail;                     :6285      tombstones
          entry_ptr--;
      }
    fail:
      di->di_dict = NULL;                             :6338
      Py_DECREF(d);                                   :6339  <-- the ONE owning ref

The critical section is keyed on the DICT, not on the ITERATOR, and the load of
di->di_dict happens BEFORE it.  N threads calling next() on the same reversed()
iterator all latch the same non-NULL `d` and queue on the dict's critical
section; the winner walks to `fail:`, NULLs di_dict and drops the reference,
and then every queued thread enters with its own stale `d` and drops it again.

The tombstone walk at :6284 is the amplifier: emptying the dict with `del`
leaves dk_nentries high while every me_value is NULL, so the winner holds the
critical section for dk_nentries iterations -- a window wide enough for all the
other threads to have already executed :6347.

Unlike the forward iterator (TSAN-0053 / gh-154130, dictiter_iternext_threadsafe)
this function has NO `#ifdef Py_GIL_DISABLED` variant at all: the reverse
iterator was skipped entirely by the free-threading hardening pass.  It backs
three types -- dict_reversekeyiterator (:6368), dict_reversevalueiterator
(:6410) and dict_reverseitemiterator (:6422).

Expected: _Py_NegativeRefcount / validate_refcounts abort on a free-threaded
build; clean on the GIL build.
"""

import sys
import threading

TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
NTHREADS = 8
NENTRIES = 60000


def main():
    for trial in range(TRIALS):
        # Big combined table, then emptied with del: dk_nentries stays at
        # NENTRIES while every me_value becomes NULL.
        d = {i: i for i in range(NENTRIES)}
        for i in range(NENTRIES):
            del d[i]

        it = reversed(d)
        barrier = threading.Barrier(NTHREADS)

        def worker():
            barrier.wait()
            for _ in range(3):
                try:
                    next(it)
                except StopIteration:
                    pass
                except RuntimeError:
                    pass

        threads = [threading.Thread(target=worker) for _ in range(NTHREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        del it
        del d
        if trial % 20 == 0:
            print("trial", trial, flush=True)
    print("survived all %d trials" % TRIALS, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
