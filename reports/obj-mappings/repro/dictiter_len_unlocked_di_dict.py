"""dictiter_len (`it.__length_hint__()`) dereferences di->di_dict with no lock,
racing the exhaustion drop in the iternext family.

Objects/dictobject.c:5678

  static PyObject *
  dictiter_len(PyObject *self, PyObject *Py_UNUSED(ignored))
  {
      dictiterobject *di = (dictiterobject *)self;
      Py_ssize_t len = 0;
      if (di->di_dict != NULL && di->di_used == GET_USED(di->di_dict))   :5682
          len = FT_ATOMIC_LOAD_SSIZE_RELAXED(di->len);                   :5683
      return PyLong_FromSize_t(len);
  }

Line 5683 loads di->len atomically; line 5682 reads di->di_dict, di->di_used and
`di_dict->ma_used` with plain loads and no critical section anywhere in the
function.  The three iternext exhaustion paths --
dictiter_iternextitem_lock_held:6028, dictiter_iternext_threadsafe:6158 and
dictreviter_iter_lock_held:6338 -- all execute `di->di_dict = NULL;
Py_DECREF(d);`.  A concurrent __length_hint__() can therefore pass the
`!= NULL` test and then dereference a dict whose last reference has just been
dropped.

That mixed discipline in a two-line function is the scanner's
`guarded_writer_unguarded_reader` finding at dictobject.c:5682.
"""

import sys
import threading

TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
NPROBERS = 6
NENTRIES = 4000


def main():
    for trial in range(TRIALS):
        d = {i: i for i in range(NENTRIES)}
        for i in range(NENTRIES):
            del d[i]
        it = iter(d)
        del d  # the iterator now holds the ONLY reference to the dict

        barrier = threading.Barrier(NPROBERS + 1)

        def prober():
            barrier.wait()
            for _ in range(400):
                try:
                    it.__length_hint__()
                except Exception:
                    pass

        def drainer():
            barrier.wait()
            for _ in range(4):
                try:
                    next(it)
                except StopIteration:
                    pass
                except RuntimeError:
                    pass

        threads = [threading.Thread(target=prober) for _ in range(NPROBERS)]
        threads.append(threading.Thread(target=drainer))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        del it
        if trial % 2000 == 0:
            print("trial", trial, flush=True)
    print("survived all %d trials" % TRIALS, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
