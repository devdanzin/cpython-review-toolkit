"""CPY-0182's siblings: the whole `striter_*` accessor family is GIL-dependent.

`Objects/bytesobject.c`'s iterator uses `it_seq == NULL` as the exhaustion
sentinel and drops the reference *unconditionally*:

    3451:    it->it_seq = NULL;          /* plain store, no #ifndef Py_GIL_DISABLED */
    3452:    Py_DECREF(seq);             /* the only owning reference */

So every other accessor does a two-step test-then-dereference of `it_seq`:

    striter_len:3461-3462      if (it->it_seq) len = PyBytes_GET_SIZE(it->it_seq) - it->it_index;
    striter_reduce:3478-3479   if (it->it_seq != NULL) Py_BuildValue("N(O)n", iter, it->it_seq, ...)
    striter_setstate:3494-3498 if (it->it_seq != NULL) ... PyBytes_GET_SIZE(it->it_seq)

Under the GIL there is no window between the test and the dereference.  Under
free threading a concurrent next() can NULL the field and free the object
inside it.

Guarded twins -- all gate on an ATOMIC `it_index` and never NULL `it_seq` on a
free-threaded build:
    Objects/bytearrayobject.c:2979/2996-2999 (next), :3011 (len), :3033 (reduce)
    Objects/listobject.c:4069/4076-4082 (next), :4093 (len)

Usage:  python gil_striter_family_race.py <scenario> [nprobe] [rounds]

Roles are fixed: exactly ONE "drainer" thread calls next(); `nprobe` threads
call only the probe under test.  A SIGSEGV/SIGABRT reported in a `probe-*`
thread is attributable to the probe accessor, not to striter_next.

Scenarios (kind_probe):
  bytes_len / bytes_reduce / bytes_setstate      -- bytesobject.c, expect FT crash
  ba_len    / ba_reduce    / ba_setstate         -- bytearray, guarded twin
  list_len  / list_reduce  / list_setstate       -- list, guarded twin
  bytes_none                                     -- drain only (CPY-0182 control)
  solo_bytes_len                                 -- single thread, same call volume
"""

from __future__ import annotations

import faulthandler
import operator
import sys
import threading

faulthandler.enable()

# Short payload: exhaustion (the only moment it_seq is NULLed) happens often.
N = 24


def make_iter(kind: str):
    if kind == "bytes":
        return iter(bytes(N))
    if kind == "ba":
        return iter(bytearray(N))
    if kind == "list":
        return iter([0] * N)
    raise SystemExit(f"unknown kind {kind}")


def make_probe(name: str):
    if name == "len":
        return operator.length_hint
    if name == "reduce":
        return lambda it: it.__reduce__()
    if name == "setstate":
        return lambda it: it.__setstate__(0)
    if name == "none":
        return None
    raise SystemExit(f"unknown probe {name}")


def run(kind: str, probe_name: str, nprobe: int, rounds: int) -> None:
    probe = make_probe(probe_name)
    box: list = [None]
    go = threading.Event()
    done = threading.Event()

    def drainer():
        while not done.is_set():
            go.wait(0.5)
            it = box[0]
            if it is None:
                continue
            try:
                for _ in it:
                    pass
            except Exception:
                pass

    def prober():
        while not done.is_set():
            it = box[0]
            if it is None or probe is None:
                continue
            try:
                probe(it)
            except Exception:
                pass

    threads = [threading.Thread(target=drainer, name="drain-0", daemon=True)]
    for i in range(nprobe):
        threads.append(
            threading.Thread(target=prober, name=f"probe-{i}", daemon=True)
        )
    for t in threads:
        t.start()

    for r in range(rounds):
        box[0] = make_iter(kind)
        go.set()
        # let the drainer exhaust it while probers hammer
        for _ in range(400):
            pass
        go.clear()
        if r % 20000 == 0:
            print(f"round {r} ok", flush=True)
    done.set()
    go.set()
    for t in threads:
        t.join(5)
    print("PROBE:completed", flush=True)


def solo(kind: str, probe_name: str, rounds: int) -> None:
    probe = make_probe(probe_name)
    for _ in range(rounds):
        it = make_iter(kind)
        for _ in range(N + 4):
            probe(it)
            try:
                next(it)
            except StopIteration:
                pass
        probe(it)
    print("PROBE:completed", flush=True)


if __name__ == "__main__":
    sc = sys.argv[1] if len(sys.argv) > 1 else "bytes_len"
    nprobe = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    rounds = int(sys.argv[3]) if len(sys.argv) > 3 else 200000
    if sc.startswith("solo_"):
        _, k, p = sc.split("_", 2)
        solo(k, p, rounds)
    else:
        k, p = sc.split("_", 1)
        run(k, p, nprobe, rounds)
