"""Probe: can bytesio_dealloc (Modules/_io/bytesio.c:1075) reach its
PyErr_SetString + PyErr_Print block at :1080-1084 with exports > 0?

That block mutates the thread's exception state from inside tp_dealloc with no
PyErr_GetRaisedException/PyErr_SetRaisedException bracket:

    if (FT_ATOMIC_LOAD_SSIZE_RELAXED(self->exports) > 0) {
        PyErr_SetString(PyExc_SystemError,
                        "deallocated BytesIO object has exported buffers");
        PyErr_Print();          <-- clears the error indicator, runs sys.excepthook
    }

If reachable, an in-flight exception is chained into the SystemError as
__context__ and then destroyed by PyErr_Print().

usage: python io_bytesio_dealloc_exports.py
"""

import gc
import io
import sys

print("build:", sys.version)


def shape(name, fn):
    print(f"--- {name}")
    try:
        fn()
    except BaseException as e:
        print(f"    raised {type(e).__name__}: {e}")
    n = gc.collect()
    print(f"    gc.collect() -> {n}")


def cycle_through_instance_dict():
    b = io.BytesIO(b"payload" * 16)
    mv = b.getbuffer()
    b.x = mv  # b -> __dict__ -> memoryview -> mbuf -> _BytesIOBuffer -> b
    del b, mv


def cycle_through_list():
    b = io.BytesIO(b"payload" * 16)
    mv = b.getbuffer()
    lst = []
    lst.append(lst)
    lst.append(b)
    lst.append(mv)
    del b, mv, lst


def cycle_via_inner_exporter():
    b = io.BytesIO(b"payload" * 16)
    mv = b.getbuffer()
    inner = mv.obj  # the _io._BytesIOBuffer intermediate
    holder = []
    holder.append(holder)
    holder.append(inner)
    holder.append(mv)
    holder.append(b)
    del b, mv, inner, holder


def subclass_with_del():
    class B(io.BytesIO):
        def __del__(self):
            pass

    b = B(b"payload" * 16)
    mv = b.getbuffer()
    b.x = mv
    del b, mv


shape("cycle through instance __dict__", cycle_through_instance_dict)
shape("cycle through list", cycle_through_list)
shape("cycle via inner _BytesIOBuffer", cycle_via_inner_exporter)
shape("subclass with __del__", subclass_with_del)

print("done; any 'SystemError: deallocated BytesIO object has exported buffers'")
print("printed above means the block is reachable.")
