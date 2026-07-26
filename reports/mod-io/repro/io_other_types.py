"""Same post-guard shape applied to the other four _io types in the slice.

BytesIO / StringIO / FileIO / IOBase have no detach(), so the analogue is
close() (which frees self->buf in bytesio.c / stringio.c) running from inside a
user callback that the C method invokes mid-operation.

Usage: io_other_types.py <scenario> <backend>
"""

import sys
import tempfile

scenario = sys.argv[1]
backend = sys.argv[2] if len(sys.argv) > 2 else "io"
if backend == "_pyio":
    import _pyio as iomod
else:
    import io as iomod

state = {}


# BytesIO.write(obj) -- __buffer__ closes the BytesIO (frees self->buf)
if scenario == "bytesio_write_buffer_closes":

    class EvilBuf:
        def __buffer__(self, flags):
            state["b"].close()
            return memoryview(b"payload")

    b = iomod.BytesIO(b"seed")
    state["b"] = b
    print("before write", flush=True)
    b.write(EvilBuf())
    print("survived", flush=True)


# BytesIO.truncate(n) -- __index__ closes the BytesIO
elif scenario == "bytesio_truncate_index_closes":

    class EvilIndex:
        def __index__(self):
            state["b"].close()
            return 2

    b = iomod.BytesIO(b"seed data")
    state["b"] = b
    print("before truncate", flush=True)
    b.truncate(EvilIndex())
    print("survived", flush=True)


# BytesIO.seek(n) -- __index__ closes the BytesIO
elif scenario == "bytesio_seek_index_closes":

    class EvilIndex:
        def __index__(self):
            state["b"].close()
            return 2

    b = iomod.BytesIO(b"seed data")
    state["b"] = b
    print("before seek", flush=True)
    b.seek(EvilIndex())
    print("survived", flush=True)


# StringIO.truncate(n) -- __index__ closes the StringIO (frees the UCS4 buf)
elif scenario == "stringio_truncate_index_closes":

    class EvilIndex:
        def __index__(self):
            state["s"].close()
            return 2

    s = iomod.StringIO("seed data")
    state["s"] = s
    print("before truncate", flush=True)
    s.truncate(EvilIndex())
    print("survived", flush=True)


# StringIO.seek(n) -- __index__ closes the StringIO
elif scenario == "stringio_seek_index_closes":

    class EvilIndex:
        def __index__(self):
            state["s"].close()
            return 2

    s = iomod.StringIO("seed data")
    state["s"] = s
    print("before seek", flush=True)
    s.seek(EvilIndex())
    print("survived", flush=True)


# FileIO.truncate(n) -- __index__ closes the fd
elif scenario == "fileio_truncate_index_closes":

    class EvilIndex:
        def __index__(self):
            state["f"].close()
            return 2

    path = tempfile.mktemp()
    f = iomod.FileIO(path, "w+")
    f.write(b"seed data")
    state["f"] = f
    print("before truncate", flush=True)
    f.truncate(EvilIndex())
    print("survived", flush=True)


# BytesIO exports counter: resize while a memoryview is live
elif scenario == "bytesio_resize_with_export":

    class EvilIndex:
        def __index__(self):
            # try to force a resize while mv holds an export
            state["b"].write(b"x" * 4096)
            return 2

    b = iomod.BytesIO(b"seed data")
    state["b"] = b
    mv = memoryview(b.getbuffer())
    print("before truncate", flush=True)
    try:
        b.truncate(EvilIndex())
    except Exception as e:
        print("exception:", type(e).__name__, e, flush=True)
    print("mv[0] =", mv[0], flush=True)
    print("survived", flush=True)

else:
    print("unknown scenario", scenario, file=sys.stderr)
    sys.exit(99)
