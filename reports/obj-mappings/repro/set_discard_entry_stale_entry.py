"""CPY-0131 probe: set_discard_entry (setobject.c:580-597) writes through a raw
`setentry *` that set_lookkey (:414) returned AFTER releasing its own
Py_BEGIN_CRITICAL_SECTION(so).

    static int
    set_discard_entry(PySetObject *so, PyObject *key, Py_hash_t hash)
    {
        setentry *entry;
        int status = set_lookkey(so, key, hash, &entry);   /* :583  CS opened+closed inside */
        ...
        old_key = entry->key;                              /* :590  raw pointer, post-CS */
        FT_ATOMIC_STORE_SSIZE_RELAXED(entry->hash, -1);    /* :591 */
        FT_ATOMIC_STORE_SSIZE_RELAXED(so->used, so->used - 1);
        FT_ATOMIC_STORE_PTR_RELEASE(entry->key, dummy);    /* :593 */
        Py_DECREF(old_key);                                /* :594 */

If another thread runs set_table_resize(so) in the window between the END of
set_lookkey's critical section and the writes, `entry` points into the freed old
table -> heap-use-after-free WRITE + a Py_DECREF of a stale key.

Strategy: many threads hammering discard/remove/difference_update/
symmetric_difference_update on ONE shared set, while other threads add/remove to
drive set_table_resize.  A colliding-hash key class puts a user __eq__ in the
probe sequence so the lookup can detach (suspending the caller's critical
section) inside set_do_lookup.

Run on a free-threaded build (ideally -asan or -tsan).
"""

import sys
import threading

NTHREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 4000

BUCKETS = 4


class Collide:
    """Hash collides in a few buckets so linear probing runs __eq__ a lot."""

    __slots__ = ("n",)

    def __init__(self, n):
        self.n = n

    def __hash__(self):
        return self.n % BUCKETS

    def __eq__(self, other):
        # Runs arbitrary Python inside set_do_lookup, i.e. inside
        # set_lookkey's critical section.  A bytecode boundary here is a
        # detach opportunity -> _PyCriticalSection_SuspendAll.
        if type(other) is not Collide:
            return NotImplemented
        for _ in range(2):
            pass
        return self.n == other.n


shared = set()
stop = threading.Event()


def churn():
    """Drive set_table_resize on the shared set."""
    while not stop.is_set():
        for i in range(200, 900):
            shared.add(Collide(i))
        shared.clear()


def discarder(seed):
    for i in range(ROUNDS):
        k = Collide((seed * 31 + i) % 400)
        shared.add(k)
        shared.discard(k)
        try:
            shared.remove(Collide(i % 400))
        except KeyError:
            pass


def differ(seed):
    other = {Collide(i) for i in range(seed % 7, 200, 3)}
    for _ in range(ROUNDS // 8):
        shared.difference_update(other)
        shared.symmetric_difference_update(other)
        shared.difference_update(other)
        # set_copy_and_difference_untracked:2077 -> set_difference_update_internal
        # on a FRESH untracked copy whose lock the caller does NOT hold: the one
        # caller shape where set_lookkey's inner CS is a real acquire/release.
        _ = shared - other


threads = []
for t in range(2):
    threads.append(threading.Thread(target=churn, daemon=True))
for t in range(NTHREADS):
    threads.append(threading.Thread(target=discarder, args=(t,)))
for t in range(NTHREADS // 2):
    threads.append(threading.Thread(target=differ, args=(t,)))

for th in threads:
    th.start()
for th in threads[2:]:
    th.join()
stop.set()

print("SURVIVED", len(shared))
