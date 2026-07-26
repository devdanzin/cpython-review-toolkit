"""bufferedio.c re-reads `self->raw` after a call that runs user Python, with
no re-check.  The gh-143008 fix (db4b1948bc4) introduced `buffer_access_safe()`
for exactly this hazard -- in textio.c ONLY.  bufferedio.c has zero
`*_access_safe` helpers and the same shape at five sites:

  :625  _io__Buffered_detach_impl       (the run's seeded lead)
  :591  _io__Buffered_close_impl        after LEAVE_BUFFERED + _PyFile_Flush
  :1389 _io__Buffered_seek_impl         after CHECK_CLOSED -> raw.closed getter
  :1485 _io__Buffered_truncate_impl     after buffered_flush_and_rewind_unlocked
  :1713 / :1748 _bufferedreader_read_all after the flush / between read() calls

`detach()` takes no ENTER_BUFFERED, so it succeeds from inside any of those
user-code windows and leaves `self->raw == NULL`.

Usage:  python buffered_raw_recheck_siblings.py <close|seek|truncate|readall> [--pyio]
"""

import sys

if "--pyio" in sys.argv:
    import _pyio as io
else:
    import io

SITE = next((a for a in sys.argv[1:] if not a.startswith("-")), "close")
fired = False


def detonate(obj):
    global fired
    if not fired:
        fired = True
        try:
            obj.detach()
        except Exception as exc:  # noqa: BLE001
            print("  inner detach ->", type(exc).__name__, exc, flush=True)


class Raw(io.RawIOBase):
    """A Python raw stream whose property/method hooks re-enter."""

    def __init__(self, owner_box, hook):
        self._box = owner_box
        self._hook = hook
        self._data = bytearray(b"payload" * 64)
        self._pos = 0

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def readinto(self, b):
        n = min(len(b), len(self._data) - self._pos)
        b[:n] = self._data[self._pos : self._pos + n]
        self._pos += n
        return n

    def read(self, n=-1):
        if self._hook == "read" and self._box:
            detonate(self._box[0])
        chunk = bytes(self._data[self._pos :])
        self._pos = len(self._data)
        return chunk

    def write(self, b):
        if self._hook == "write" and self._box:
            detonate(self._box[0])
        return len(b)

    def seek(self, pos, whence=0):
        return 0

    def truncate(self, pos=None):
        return pos or 0

    def tell(self):
        return 0

    @property
    def closed(self):
        if self._hook == "closed" and self._box:
            detonate(self._box[0])
        return False


box = []
print("site:", SITE, flush=True)

if SITE == "close":

    class Reader(io.BufferedReader):
        def flush(self):
            detonate(self)
            return None

    obj = Reader(io.BytesIO(b"x" * 512))
    action = obj.close

elif SITE == "seek":
    obj = io.BufferedReader(Raw(box, "closed"))
    box.append(obj)
    action = lambda: obj.seek(0, 2)  # noqa: E731

elif SITE == "truncate":
    obj = io.BufferedWriter(Raw(box, "write"))
    box.append(obj)
    obj.write(b"A" * 2048)  # make the flush have something to push

    def action():
        obj.truncate(0)

elif SITE == "readall":
    obj = io.BufferedReader(Raw(box, "read"))
    box.append(obj)
    action = obj.read

else:
    raise SystemExit("unknown site")

try:
    action()
except Exception as exc:  # noqa: BLE001
    print("raised", type(exc).__name__, exc, flush=True)
else:
    print("returned", flush=True)
print("survived", flush=True)
