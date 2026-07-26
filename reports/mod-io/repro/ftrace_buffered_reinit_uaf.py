"""ft-race-scanner repro A: BufferedReader.__init__ has no critical section.

Modules/_io/bufferedio.c
  _io_BufferedReader___init___impl  (:1592)  -- clinic block has NO @critical_section,
                                                and the body never takes ENTER_BUFFERED
    -> _buffered_init (:838)
         :846-847   if (self->buffer) PyMem_Free(self->buffer);
         :848       self->buffer = PyMem_Malloc(self->buffer_size);
         :851-852   if (self->lock) PyThread_free_lock(self->lock);
         :853       self->lock = PyThread_allocate_lock();

Every *other* entry point of the type is @critical_section (25 of them) AND takes
ENTER_BUFFERED.  So a second __init__ on a live shared object frees, from outside
both locks:
  (1) self->buffer, which a concurrent read()/readline() is memcpy'ing out of; and
  (2) self->lock, the PyThread_type_lock a concurrent ENTER_BUFFERED is *holding*.

Guarded twin: _io.TextIOWrapper.__init__ (textio.c:1237) IS @critical_section.

Usage:  python ftrace_buffered_reinit_uaf.py [scenario] [seconds]
scenarios: reinit_read (default) | reinit_close | reinit_reinit
"""

import io
import sys
import threading

SCEN = sys.argv[1] if len(sys.argv) > 1 else "reinit_read"
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0

NTHREAD = 8
PAYLOAD = b"line of text for the buffered reader to chew on\n" * 400

stop = threading.Event()
errors = []


def make():
    return io.BufferedReader(io.BytesIO(PAYLOAD), buffer_size=8192)


def reinit(f):
    while not stop.is_set():
        try:
            # Re-__init__ a *live* object: legal Python, no _testcapi.
            f.__init__(io.BytesIO(PAYLOAD), buffer_size=8192)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))


def reader(f):
    while not stop.is_set():
        try:
            f.read(64)
            f.readline()
            f.peek(32)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))


def closer(f):
    while not stop.is_set():
        try:
            f.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))


def main():
    f = make()
    if SCEN == "reinit_read":
        fns = [reinit, reader]
    elif SCEN == "reinit_close":
        fns = [reinit, closer]
    elif SCEN == "reinit_reinit":
        fns = [reinit, reinit]
    else:
        raise SystemExit("unknown scenario " + SCEN)

    ts = []
    for i in range(NTHREAD):
        t = threading.Thread(target=fns[i % len(fns)], args=(f,), daemon=True)
        ts.append(t)
    for t in ts:
        t.start()
    stop.wait(DUR)
    stop.set()
    for t in ts:
        t.join(5.0)
    print("survived; %d benign exceptions" % len(errors))


main()
