"""bytesio.c:740 CHECK_EXPORTS is stale for the same reason CHECK_CLOSED is.

_io_BytesIO_truncate_impl checks exports at :740, then converts the caller's
`size` object at :748 with PyLong_AsLong -- which runs a user __index__. If
that __index__ takes a memoryview export, :761 resize_buffer_lock_held runs
with exports > 0, i.e. it reallocates the buffer out from under a live export.

Usage: io_bytesio_stale_exports.py <backend>
"""

import sys

backend = sys.argv[1] if len(sys.argv) > 1 else "io"
if backend == "_pyio":
    import _pyio as iomod
else:
    import io as iomod

state = {}


class EvilIndex:
    def __index__(self):
        # take an export AFTER CHECK_EXPORTS(self) at bytesio.c:740 already ran
        state["mv"] = state["b"].getbuffer()
        return 2


b = iomod.BytesIO(b"A" * 4096)
state["b"] = b

print("before truncate", flush=True)
try:
    b.truncate(EvilIndex())
except Exception as e:
    print("exception:", type(e).__name__, e, flush=True)

mv = state.get("mv")
if mv is not None:
    # if the buffer was reallocated under us this reads freed memory
    print("len(mv) =", len(mv), flush=True)
    print("mv[:8] =", bytes(mv[:8]), flush=True)
print("survived", flush=True)
