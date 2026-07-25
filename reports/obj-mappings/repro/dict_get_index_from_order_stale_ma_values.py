"""get_index_from_order() re-reads mp->ma_values plainly, discarding the
lock-free iterator's atomic snapshot.

Objects/dictobject.c:

  get_index_from_order(mp, i)                                    :672
      assert(i < mp->ma_values->size);                           :675  plain
      uint8_t *array = get_insertion_order_array(mp->ma_values);  :676  plain
      return array[i];

  dictiter_iternext_threadsafe(d, self, ...)   -- NO critical section
      PyDictValues *values = _Py_atomic_load_ptr_consume(&d->ma_values);  :6086
      if (values == NULL) goto concurrent_modification;                   :6087
      Py_ssize_t used = _Py_atomic_load_uint8(&values->size);             :6091
      if (i >= used) goto fail;                                           :6092
      int index = get_index_from_order(d, i);                             :6100  <-- RE-READS
      PyObject *value = _Py_atomic_load_ptr(&values->values[index]);      :6101

The function snapshots ma_values atomically at :6086 and uses that snapshot at
:6101 -- but the helper it calls at :6100 goes back to the field.  Any thread
that runs dictresize() split->combined in that window executes
set_values(mp, NULL) (:2264), so get_insertion_order_array(NULL) dereferences
&((PyDictValues *)NULL)->values[NULL->capacity] -> SIGSEGV.

The guarded twin is one line below: :6101/:6103 correctly use the snapshot
`values`.  The fix is to pass the snapshot into the helper.

Reproduction strategy: threads iterate a shared SPLIT instance __dict__ while
one thread inserts a non-str key, which forces dictresize() split->combined and
NULLs ma_values.
"""

import sys
import threading

TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 40000
NREADERS = 7


class C:
    pass


def main():
    for trial in range(TRIALS):
        obj = C()
        for n in range(8):
            setattr(obj, "attr%d" % n, n)
        d = obj.__dict__  # split table, ma_values != NULL

        barrier = threading.Barrier(NREADERS + 1)
        stop = False

        def reader():
            barrier.wait()
            for _ in range(200):
                try:
                    for _k in d:
                        pass
                except RuntimeError:
                    pass

        def flipper():
            barrier.wait()
            try:
                d[object()] = 1  # non-str key -> dictresize -> set_values(mp, NULL)
            except Exception:
                pass

        threads = [threading.Thread(target=reader) for _ in range(NREADERS)]
        threads.append(threading.Thread(target=flipper))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if trial % 5000 == 0:
            print("trial", trial, flush=True)
    print("survived all %d trials" % TRIALS, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
