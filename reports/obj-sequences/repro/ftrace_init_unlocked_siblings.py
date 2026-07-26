"""Task (e): the CPY-0187 shape swept beyond the slice.

CPY-0187 = `bytearray.__init__` is the ONE clinic entry point of its type with no
`Py_BEGIN_CRITICAL_SECTION` while 33 siblings have one, and it writes self's
fields directly (heap buffer overflow on debug-ft-nojit, Group B G2).

Measuring Group B's `clinic_critical_section_coverage` proposal tree-wide
(scratchpad/clinic_cs_coverage.py) turns up 17 gated candidates in 9 files, of
which `bytearray___init___impl` is one.  The rest are the same shape in other
modules -- an unlocked `__init__` / `__exit__` / `close` among locked siblings:

  Modules/_io/bufferedio.c  25/34 locked
      _io_BufferedReader___init___impl:1591   writes ok/readable/writable/
                                              buffer_size/detached/fast_closed_checks
      _io_BufferedWriter___init___impl:1943   + pos
      _io_BufferedRandom___init___impl:2484   + pos
      _io_BufferedRWPair___init___impl:2273   writes reader/writer
  Modules/_io/stringio.c    16/17 locked
      _io_StringIO___init___impl:683          writes buf/pos/string_size/state/decoder/...
  Modules/_io/textio.c      26/34 locked
      _io_IncrementalNewlineDecoder___init___impl:247
  Modules/_asynciomodule.c  36/57 locked
      _asyncio_Task___init___impl:2299
  Modules/_lsprof.c          9/10 locked
      profiler_init_impl:1008

Each scenario re-`__init__`s ONE shared, already-published object from several
threads while other threads use it through the type's LOCKED methods, so any
crash is the unlocked entry point racing a locked sibling -- the CPY-0187
mechanism exactly.

Controls: `*_locked` scenarios drive only the locked sibling; `solo_*` runs the
same call volume single-threaded.

Usage (one scenario per subprocess):
    PYTHON_GIL=0 .../debug-ft-nojit/python ftrace_init_unlocked_siblings.py <scenario> [rounds] [threads]
    ftrace_init_unlocked_siblings.py --list
"""

import io
import sys
import threading

ROUNDS_DEFAULT = 3000
NTHREADS_DEFAULT = 4


# --- _io.StringIO ----------------------------------------------------------
def sio_init_vs_write(obj, i):
    if i % 2:
        obj.__init__("x" * 512)
    else:
        obj.write("y" * 512)


def sio_init_vs_init(obj, i):
    obj.__init__("x" * (256 + (i % 512)))


def sio_init_vs_read(obj, i):
    if i % 2:
        obj.__init__("x" * 512)
    else:
        obj.getvalue()


def sio_write_only(obj, i):
    obj.write("y" * 512)


def mk_sio():
    return io.StringIO("seed")


# --- _io.BufferedReader ----------------------------------------------------
def mk_br():
    return io.BufferedReader(io.BytesIO(b"a" * 65536))


def br_init_vs_read(obj, i):
    if i % 2:
        obj.__init__(io.BytesIO(b"b" * 65536), buffer_size=8192)
    else:
        obj.read(64)


def br_init_vs_init(obj, i):
    obj.__init__(io.BytesIO(b"b" * 65536), buffer_size=1024 * (1 + i % 8))


def br_read_only(obj, i):
    obj.read(64)


# --- _io.BufferedWriter ----------------------------------------------------
def mk_bw():
    return io.BufferedWriter(io.BytesIO())


def bw_init_vs_write(obj, i):
    if i % 2:
        obj.__init__(io.BytesIO(), buffer_size=8192)
    else:
        obj.write(b"z" * 256)


def bw_write_only(obj, i):
    obj.write(b"z" * 256)


# --- _io.IncrementalNewlineDecoder -----------------------------------------
def mk_nld():
    return io.IncrementalNewlineDecoder(None, True)


def nld_init_vs_decode(obj, i):
    if i % 2:
        obj.__init__(None, bool(i % 3))
    else:
        obj.decode("a\r\nb\rc\n", i % 2 == 0)


def nld_decode_only(obj, i):
    obj.decode("a\r\nb\rc\n", True)


SCENARIOS = {
    "sio_init_vs_write": (mk_sio, sio_init_vs_write, "_io_StringIO___init___impl:683 vs locked write"),
    "sio_init_vs_init": (mk_sio, sio_init_vs_init, "two unlocked __init__"),
    "sio_init_vs_read": (mk_sio, sio_init_vs_read, "__init__ vs locked getvalue"),
    "sio_write_only": (mk_sio, sio_write_only, "CONTROL -- locked sibling only"),
    "br_init_vs_read": (mk_br, br_init_vs_read, "_io_BufferedReader___init___impl:1591 vs locked read"),
    "br_init_vs_init": (mk_br, br_init_vs_init, "two unlocked __init__"),
    "br_read_only": (mk_br, br_read_only, "CONTROL -- locked sibling only"),
    "bw_init_vs_write": (mk_bw, bw_init_vs_write, "_io_BufferedWriter___init___impl:1943 vs locked write"),
    "bw_write_only": (mk_bw, bw_write_only, "CONTROL -- locked sibling only"),
    "nld_init_vs_decode": (mk_nld, nld_init_vs_decode, "_io_IncrementalNewlineDecoder___init___impl:247"),
    "nld_decode_only": (mk_nld, nld_decode_only, "CONTROL -- locked sibling only"),
    "solo_sio_init": (mk_sio, sio_init_vs_write, "SOLO -- no concurrency"),
    "solo_br_init": (mk_br, br_init_vs_read, "SOLO -- no concurrency"),
}


def run(name, rounds, nthreads):
    factory, op, _note = SCENARIOS[name]
    solo = name.startswith("solo_")
    for r in range(rounds):
        obj = factory()
        if solo:
            for i in range(16):
                try:
                    op(obj, i)
                except (ValueError, OSError, TypeError):
                    pass
            continue
        barrier = threading.Barrier(nthreads)

        def worker(tid):
            barrier.wait()
            for i in range(16):
                try:
                    op(obj, tid + i)
                except (ValueError, OSError, TypeError):
                    pass

        ts = [threading.Thread(target=worker, args=(t,)) for t in range(nthreads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--list", "-l"):
        for k, (_f, _o, note) in SCENARIOS.items():
            print(f"{k:22s} {note}")
        return 0
    name = sys.argv[1]
    if name not in SCENARIOS:
        print(f"unknown scenario {name!r}", file=sys.stderr)
        return 2
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else ROUNDS_DEFAULT
    nthreads = int(sys.argv[3]) if len(sys.argv) > 3 else NTHREADS_DEFAULT
    run(name, rounds, nthreads)
    print(f"survived {name} rounds={rounds} threads={nthreads}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
