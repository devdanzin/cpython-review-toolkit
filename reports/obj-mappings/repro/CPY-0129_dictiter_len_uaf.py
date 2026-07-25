"""CPY-0129 -- force the `dictiter_len` use-after-free to a crash face.

    Objects/dictobject.c:5678-5685
      static PyObject *
      dictiter_len(PyObject *self, PyObject *Py_UNUSED(ignored))
      {
          dictiterobject *di = (dictiterobject *)self;
          Py_ssize_t len = 0;
          if (di->di_dict != NULL && di->di_used == GET_USED(di->di_dict))   /* :5682 */
              len = FT_ATOMIC_LOAD_SSIZE_RELAXED(di->len);                   /* :5683 */
          return PyLong_FromSize_t(len);
      }

    Objects/dictobject.c:6157-6160  (dictiter_iternext_threadsafe, `fail:`)
      fail:
          di->di_dict = NULL;      /* :6158 */
          Py_DECREF(d);            /* :6159 */

`GET_USED(x)` is `FT_ATOMIC_LOAD_SSIZE_RELAXED((x)->ma_used)`: :5682 plainly
loads `di->di_dict`, tests it for NULL, and then DEREFERENCES it -- with no
critical section anywhere in the function -- while :6158/:6159 clear that field
and drop what can be the container's LAST reference.

Why the previous attempt (`dictiter_len_unlocked_di_dict.py`, 0 crashes in 3,000
trials) found nothing, and what is different here:

  * Its diagnosis was "QSBR delays the free".  That is true of the dict's KEYS
    and VALUES arrays (`free_keys_object` / `free_values`, gated on
    `IS_DICT_SHARED`), and FALSE of the `PyDictObject` header -- and `ma_used`,
    the field :5682 dereferences, lives in the header.  `PyObject_GC_Del`
    (Python/gc_free_threading.c) ends in a plain `PyObject_Free`, with no QSBR
    deferral at all.
  * What DOES defer the free is biased reference counting: when a NON-OWNING
    thread drops the last reference, `_Py_MergeZeroLocalRefcount` hands the
    object to the owning thread's queue instead of freeing it.  So the drainer
    must be the thread that CREATED the dict.  Here each drainer builds and
    exhausts its own iterators, and only the probers are foreign threads --
    which is sound because `dictiter_len` never calls `ensure_shared_on_read`.
  * Even then a plain build reuses the mimalloc block silently, so the UAF has
    no crash face.  `release-ft-nojit-asan-mitrack` is a free-threaded ASan build
    with `MI_TRACK_ASAN`, so blocks ARE poisoned on free, and the disassembly
    confirms the dereference is instrumented there:

        <+52>: add $0x20,%rax          ; %rax = di->di_dict + offsetof(ma_used)
        <+56..70>: shadow check on (%rax)
        <+72>: mov (%rax),%rax

    On plain `release-ft-nojit` gcc emits ONE load of `di->di_dict`
    (`mov 0x20(%rdi),%rax` at <+0>, reused at <+52>), so the "compiler
    rematerialises the pointer and GET_USED(NULL) segfaults" escalation is not
    available on this build.

Usage:  <ft-python> CPY-0129_dictiter_len_uaf.py [seconds] [nprobers] [ndrainers]
Exit 0 = survived.  SIGSEGV / ASan `heap-use-after-free READ` at
dictobject.c:5682 = reproduced.
"""

import sys
import threading
import time

SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
# NPROBERS should OVERSUBSCRIBE the machine.  The window at :5682 is ~10
# instructions wide, and the drop at :6158/:6159 puts di_dict = NULL *before*
# Py_DECREF, so a prober that has already loaded a non-NULL di_dict must still
# be sitting between its two loads several hundred nanoseconds later, when the
# dealloc/free/poison completes.  The only thing that stretches a thread across
# that gap is an involuntary context switch -- which only happens if there are
# more runnable threads than cores.
NPROBERS = int(sys.argv[2]) if len(sys.argv) > 2 else 8
NDRAINERS = int(sys.argv[3]) if len(sys.argv) > 3 else 4
PERDRAINER = int(sys.argv[4]) if len(sys.argv) > 4 else 8

STOP = False
SLOT = [None] * (NDRAINERS * PERDRAINER)
DROPS = [0] * NDRAINERS

# Retired iterators are pinned in a ring so that replacing SLOT[i] frees the
# DICT but never the ITERATOR.  Without this the run is dominated by a
# different, off-target report: a prober doing `SLOT[i]` reaches
# list_get_item_ref -> _Py_TryXGetRef -> _Py_TryIncrefCompare, which reads
# ob_tid/ob_ref_local of a list element another thread has just replaced, and
# MI_TRACK_ASAN flags that optimistic-incref read as use-after-poison
# (Objects/listobject.c:373 / Include/cpython/object.h:580).  That is a
# different site with a different owner; pinning removes it from this run.
RINGBITS = int(sys.argv[5]) if len(sys.argv) > 5 else 16
RING = [None] * (1 << RINGBITS)
RINGMASK = (1 << RINGBITS) - 1


def make_iter():
    # dk_nentries high, ma_used 0: the first next() walks to the end of the
    # entry array and takes `fail:`, and the iterator is the dict's ONLY owner,
    # so :6159 is the last reference.
    d = {}
    for i in range(48):
        d["k%d" % i] = i
    for i in range(48):
        del d["k%d" % i]
    return iter(d)


def prober():
    n = len(SLOT)
    while not STOP:
        for i in range(n):
            it = SLOT[i]
            if it is None:
                continue
            try:
                it.__length_hint__()
                it.__length_hint__()
                it.__length_hint__()
                it.__length_hint__()
                it.__length_hint__()
                it.__length_hint__()
                it.__length_hint__()
                it.__length_hint__()
            except Exception:
                pass


def drainer(idx):
    base = idx * PERDRAINER
    # This thread OWNS every dict it creates, so the Py_DECREF at :6159 goes
    # through the local refcount and frees synchronously.
    for j in range(PERDRAINER):
        SLOT[base + j] = make_iter()
    count = idx << 20
    while not STOP:
        for j in range(PERDRAINER):
            it = SLOT[base + j]
            try:
                next(it)          # -> fail: -> di_dict = NULL; Py_DECREF(d)
            except (StopIteration, RuntimeError):
                pass
            RING[count & RINGMASK] = it     # pin the iterator, free only the dict
            count += 1
            SLOT[base + j] = make_iter()
    DROPS[idx] = count - (idx << 20)


def main():
    global STOP
    print("seconds=%.1f probers=%d drainers=%d gil=%s"
          % (SECONDS, NPROBERS, NDRAINERS,
             getattr(sys, "_is_gil_enabled", lambda: "n/a")()), flush=True)

    ts = [threading.Thread(target=drainer, args=(i,), daemon=True)
          for i in range(NDRAINERS)]
    ts += [threading.Thread(target=prober, daemon=True)
           for _ in range(NPROBERS)]
    for t in ts:
        t.start()

    end = time.monotonic() + SECONDS
    while time.monotonic() < end:
        time.sleep(0.25)
    STOP = True
    for t in ts:
        t.join(timeout=15.0)
    print("survived: %d exhaustion drops staged" % sum(DROPS), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
