"""striter_next (the `bytes` iterator) drops its single owning it_seq reference
with a plain store + plain Py_DECREF and no critical section.

Objects/bytesobject.c:3434  striter_next
Objects/bytesobject.c:3441      seq = it->it_seq;            <- plain load
Objects/bytesobject.c:3444      if (it->it_index < PyBytes_GET_SIZE(seq)) {
Objects/bytesobject.c:3445          return _PyLong_FromUnsignedChar(seq->ob_sval[it->it_index++]);
Objects/bytesobject.c:3450      it->it_seq = NULL;           <- plain store
Objects/bytesobject.c:3451      Py_DECREF(seq);              <- the ONLY owning reference

Two threads that both take the exhaustion branch both drop it.

Guarded twins, both in this slice:
  * Objects/bytearrayobject.c:2996  bytearrayiter_next -- FT_ATOMIC_LOAD_SSIZE_RELAXED
    for it_index, Py_BEGIN_CRITICAL_SECTION(seq) for the data read, and the drop
    itself is `#ifndef Py_GIL_DISABLED Py_CLEAR(it->it_seq) #endif` -- i.e. under
    free-threading the reference is deliberately NOT dropped here at all.
  * Objects/listobject.c:4076 listiter_next and :4226 listreviter_next -- identical
    FT_ATOMIC index + `#ifndef Py_GIL_DISABLED` drop.

striter_next has none of the three: no FT_ATOMIC, no critical section, no
#ifndef guard.  Same shape as CPY-0067 (arrayiter_next) and CPY-0062
(elementiter_next).

Run on a free-threaded build:
    PYTHON_GIL=0 .../debug-ft-nojit/python bytes_iterator_ft_double_decref.py
"""

import sys
import threading

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 4000
NTHREADS = int(sys.argv[2]) if len(sys.argv) > 2 else 8

DATA = bytes(range(256)) * 4


def drain(it, barrier):
    barrier.wait()
    for _ in it:
        pass


def main():
    for r in range(ROUNDS):
        it = iter(DATA)
        barrier = threading.Barrier(NTHREADS)
        threads = [
            threading.Thread(target=drain, args=(it, barrier))
            for _ in range(NTHREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    print("survived", ROUNDS, "rounds")


if __name__ == "__main__":
    main()
    print("completed")
