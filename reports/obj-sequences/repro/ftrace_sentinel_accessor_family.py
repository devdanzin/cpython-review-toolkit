"""Evidence for the proposed `iterator_sentinel_field_asymmetry` rule (task (c)).

The rule flags every *method* of an iterator type that tests the exhaustion
sentinel field and dereferences it in a SEPARATE expression, when the iternext
NULLs that field on a free-threaded build.  Group B measured it on the
obj-sequences slice only (4/4, 0 FP).  This script tests the candidates the
prototype produced OUTSIDE the slice, so the proposal can be judged tree-wide.

Each scenario shares ONE iterator between a drain thread (which calls next()
until exhaustion, i.e. drives the sentinel store) and N probe threads that call
ONLY the accessor under test.  A crash in a `probe` frame is attributable to the
accessor, not to the iternext.

Sites under test (all read `X->F` after a separate `if (X->F)` test):

  bytes_*    Objects/bytesobject.c striter_len:3461 / striter_reduce:3478 /
             striter_setstate:3494        <- CPY-0182, in-slice, confirmation only
  ga_*       Objects/genericaliasobject.c ga_iter_reduce:991
             <- un-found sibling of CPY-0026 (ga_iternext Py_SETREF(obj, NULL):952)
  calliter_* Objects/iterobject.c calliter_reduce:266 / calliter_iternext:229
             <- `it_callable` Py_CLEAR'd at :243; no catalogue entry anywhere
  seqiter_*  Objects/iterobject.c iter_len:91 / iter_reduce:119
             <- it_seq face; TSAN-0044 records only the it_index cursor face
  str_*      Objects/unicodeobject.c unicodeiter_len:14996 / _reduce:15013 /
             _setstate:15034  <- TSAN-0038 records `len` (it_index) only
  array_*    Modules/arraymodule.c array_arrayiterator___reduce___impl:3289
             <- un-found sibling of CPY-0067 (arrayiter_next it->ao=NULL:3247)

Guarded-twin controls -- the same thread mix on iterators whose iternext elides
the drop under `#ifndef Py_GIL_DISABLED`:

  list_*, tuple_*, bytearray_*

Usage (one scenario per subprocess; never run two in one process):
    PYTHON_GIL=0 .../debug-ft-nojit/python ftrace_sentinel_accessor_family.py <scenario> [rounds] [probes]
    ftrace_sentinel_accessor_family.py --list
"""

import array
import operator
import sys
import threading

NPROBE_DEFAULT = 7
ROUNDS_DEFAULT = 40000


def _mk_bytes():
    return iter(b"abcdefgh")


def _mk_str():
    return iter("abcdefgh")


def _mk_list():
    return iter([1, 2, 3, 4, 5, 6, 7, 8])


def _mk_tuple():
    return iter((1, 2, 3, 4, 5, 6, 7, 8))


def _mk_bytearray():
    return iter(bytearray(b"abcdefgh"))


def _mk_array():
    return iter(array.array("i", [1, 2, 3, 4, 5, 6, 7, 8]))


def _mk_ga():
    return iter(list[int, str, float, bytes])


class _Seq:
    """A __getitem__-only sequence -> Objects/iterobject.c seqiterobject."""

    __slots__ = ()

    def __getitem__(self, i):
        if i > 7:
            raise IndexError(i)
        return i


def _mk_seqiter():
    return iter(_Seq())


def _mk_calliter():
    box = [0]

    def step():
        box[0] += 1
        return box[0] if box[0] <= 8 else None

    return iter(step, None)


def _p_len(it):
    operator.length_hint(it)


def _p_reduce(it):
    it.__reduce__()


def _p_setstate(it):
    it.__setstate__(3)


def _p_next(it):
    try:
        next(it)
    except StopIteration:
        pass


SCENARIOS = {
    # name:            (factory, probe, note)
    "bytes_len": (_mk_bytes, _p_len, "striter_len:3461 (in-slice, CPY-0182)"),
    "bytes_reduce": (_mk_bytes, _p_reduce, "striter_reduce:3478 (CPY-0182)"),
    "bytes_setstate": (_mk_bytes, _p_setstate, "striter_setstate:3494 (CPY-0182)"),
    "ga_reduce": (_mk_ga, _p_reduce, "ga_iter_reduce:991 -- NEW sibling of CPY-0026"),
    "ga_next": (_mk_ga, _p_next, "ga_iternext:942/952 -- CPY-0026 itself"),
    "calliter_reduce": (_mk_calliter, _p_reduce, "calliter_reduce:266 -- NEW"),
    "calliter_next": (_mk_calliter, _p_next, "calliter_iternext:229 -- NEW"),
    "seqiter_len": (_mk_seqiter, _p_len, "iter_len:91 -- it_seq face, NEW"),
    "seqiter_reduce": (_mk_seqiter, _p_reduce, "iter_reduce:119 -- it_seq face, NEW"),
    "str_len": (_mk_str, _p_len, "unicodeiter_len:14996 (TSAN-0038 it_index face)"),
    "str_reduce": (_mk_str, _p_reduce, "unicodeiter_reduce:15013 -- NEW face"),
    "str_setstate": (_mk_str, _p_setstate, "unicodeiter_setstate:15034 -- NEW face"),
    "array_reduce": (
        _mk_array,
        _p_reduce,
        "array_arrayiterator___reduce___impl:3289 -- NEW sibling of CPY-0067",
    ),
    # guarded-twin controls: iternext elides the drop under #ifndef Py_GIL_DISABLED
    "ctl_list_len": (_mk_list, _p_len, "CONTROL listiter_len"),
    "ctl_list_reduce": (_mk_list, _p_reduce, "CONTROL listiter_reduce"),
    "ctl_list_setstate": (_mk_list, _p_setstate, "CONTROL listiter_setstate"),
    "ctl_tuple_len": (_mk_tuple, _p_len, "CONTROL tupleiter_len"),
    "ctl_tuple_reduce": (_mk_tuple, _p_reduce, "CONTROL tupleiter_reduce"),
    "ctl_bytearray_len": (_mk_bytearray, _p_len, "CONTROL bytearrayiter_length_hint"),
    "ctl_bytearray_reduce": (_mk_bytearray, _p_reduce, "CONTROL bytearrayiter_reduce"),
    # workload controls: same call volume, one thread
    "solo_bytes_reduce": (_mk_bytes, _p_reduce, "SOLO (no concurrency)"),
    "solo_ga_reduce": (_mk_ga, _p_reduce, "SOLO (no concurrency)"),
    "solo_str_reduce": (_mk_str, _p_reduce, "SOLO (no concurrency)"),
}


def run(name, rounds, nprobe):
    factory, probe, _note = SCENARIOS[name]
    solo = name.startswith("solo_")
    if solo:
        for _ in range(rounds):
            it = factory()
            for _ in range(8):
                probe(it)
                _p_next(it)
            probe(it)
        return

    for _ in range(rounds):
        it = factory()
        barrier = threading.Barrier(nprobe + 1)

        def drain():
            barrier.wait()
            for _ in range(12):
                _p_next(it)

        def prober():
            barrier.wait()
            for _ in range(12):
                try:
                    probe(it)
                except (TypeError, ValueError, AttributeError, StopIteration):
                    pass

        threads = [threading.Thread(target=drain, name="drain-0")]
        threads += [
            threading.Thread(target=prober, name=f"probe-{i}") for i in range(nprobe)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--list", "-l"):
        for k, (_f, _p, note) in SCENARIOS.items():
            print(f"{k:24s} {note}")
        return 0
    name = sys.argv[1]
    if name not in SCENARIOS:
        print(f"unknown scenario {name!r}", file=sys.stderr)
        return 2
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else ROUNDS_DEFAULT
    nprobe = int(sys.argv[3]) if len(sys.argv) > 3 else NPROBE_DEFAULT
    run(name, rounds, nprobe)
    print(f"survived {name} rounds={rounds} probes={nprobe}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
