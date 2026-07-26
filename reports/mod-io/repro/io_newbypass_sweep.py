"""Sweep: every public method/attr of the _io C types on a __new__-bypassed
instance (tp_init skipped), plus the pure-Python-subclass-forgets-super variant.

Usage:  python io_newbypass_sweep.py [c|py]
   c  -> import io      (the C accelerator)
   py -> import _pyio   (the differential oracle)

Prints one line per probe: OK / <ExcName> / (a crash kills the process, so the
last line printed before death names the culprit).
"""

import sys

backend = sys.argv[1] if len(sys.argv) > 1 else "c"
if backend == "py":
    import _pyio as iomod
else:
    import io as iomod

TYPES = [
    "BufferedReader",
    "BufferedWriter",
    "BufferedRandom",
    "BufferedRWPair",
    "TextIOWrapper",
    "IncrementalNewlineDecoder",
    "BytesIO",
    "StringIO",
    "FileIO",
]

# name -> callable(obj)
PROBES = [
    ("flush", lambda o: o.flush()),
    ("close", lambda o: o.close()),
    ("detach", lambda o: o.detach()),
    ("seekable", lambda o: o.seekable()),
    ("readable", lambda o: o.readable()),
    ("writable", lambda o: o.writable()),
    ("fileno", lambda o: o.fileno()),
    ("isatty", lambda o: o.isatty()),
    ("tell", lambda o: o.tell()),
    ("seek0", lambda o: o.seek(0)),
    ("truncate", lambda o: o.truncate()),
    ("read", lambda o: o.read()),
    ("read1", lambda o: o.read1()),
    ("readline", lambda o: o.readline()),
    ("readlines", lambda o: o.readlines()),
    ("peek", lambda o: o.peek()),
    ("readinto", lambda o: o.readinto(bytearray(4))),
    ("write", lambda o: o.write(b"x")),
    ("writestr", lambda o: o.write("x")),
    ("writelines", lambda o: o.writelines([b"x"])),
    ("iter", lambda o: next(iter(o))),
    ("repr", lambda o: repr(o)),
    ("name", lambda o: o.name),
    ("mode", lambda o: o.mode),
    ("closed", lambda o: o.closed),
    ("raw", lambda o: o.raw),
    ("buffer", lambda o: o.buffer),
    ("encoding", lambda o: o.encoding),
    ("errors", lambda o: o.errors),
    ("newlines", lambda o: o.newlines),
    ("line_buffering", lambda o: o.line_buffering),
    ("chunk_size", lambda o: o._CHUNK_SIZE),
    ("chunk_size_set", lambda o: setattr(o, "_CHUNK_SIZE", 100)),
    ("reconfigure_nl", lambda o: o.reconfigure(newline="\n")),
    ("reconfigure_enc", lambda o: o.reconfigure(encoding="utf-8")),
    ("getvalue", lambda o: o.getvalue()),
    ("getbuffer", lambda o: o.getbuffer()),
    ("decode", lambda o: o.decode(b"abc")),
    ("getstate", lambda o: o.getstate()),
    ("setstate", lambda o: o.setstate((b"", 0))),
    ("reset", lambda o: o.reset()),
    ("getstate_nl", lambda o: o.newlines),
    ("reduce", lambda o: o.__reduce__()),
    ("sizeof", lambda o: o.__sizeof__()),
    ("del", lambda o: None),
]


def run(label, factory):
    for pname, fn in PROBES:
        try:
            obj = factory()
        except Exception as exc:  # construction itself failed
            print("%-28s %-16s CONSTRUCT-%s" % (label, pname, type(exc).__name__))
            continue
        sys.stdout.flush()
        sys.stderr.write("PROBE %s %s\n" % (label, pname))
        sys.stderr.flush()
        try:
            fn(obj)
            print("%-28s %-16s OK" % (label, pname))
        except BaseException as exc:
            print("%-28s %-16s %s" % (label, pname, type(exc).__name__))
        sys.stdout.flush()


for tname in TYPES:
    T = getattr(iomod, tname, None)
    if T is None:
        print("%-28s -- not present" % tname)
        continue
    # direct bypass
    try:
        T.__new__(T)
    except BaseException as exc:
        print("%-28s __new__ rejected: %s" % (tname, type(exc).__name__))
        continue
    run(tname + ".__new__", lambda T=T: T.__new__(T))

    # subclass that forgets super().__init__()
    ns = {"__init__": lambda self, *a, **k: None}
    try:
        S = type("S_" + tname, (T,), ns)
    except BaseException as exc:
        print("%-28s subclass rejected: %s" % (tname, type(exc).__name__))
        continue
    run(tname + ".subclass", lambda S=S: S())

print("SWEEP-DONE")
