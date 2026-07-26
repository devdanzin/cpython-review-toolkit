"""list_remove_impl reads self->ob_item and Py_SIZE(self) PLAINLY inside its own
critical section, across a user __eq__ that can detach the thread.

Objects/listobject.c:3409   for (i = 0; i < Py_SIZE(self); i++)      <- plain Py_SIZE
Objects/listobject.c:3410       PyObject *obj = self->ob_item[i];    <- plain ob_item load
Objects/listobject.c:3411       Py_INCREF(obj);
Objects/listobject.c:3412       int cmp = PyObject_RichCompareBool(obj, value, Py_EQ);

Its three siblings (list_contains:660, list_index_impl:3340, list_count_impl:3371)
call list_get_item_ref(), whose Py_GIL_DISABLED body (listobject.c:354-377) uses
PyList_GET_SIZE (an atomic relaxed load) and _Py_atomic_load_ptr(&op->ob_item).

The reason remove differs is real: only list.remove takes the clinic critical
section (Objects/clinic/listobject.c.h:391); index/count/__contains__ do not.
But Python/pystate.c:2323 releases every held critical section on detach, and a
user __eq__ that sleeps detaches -- so another thread can run list_resize's
_Py_atomic_store_ptr_release(&self->ob_item, ...) between two plain loads here.

Run on a free-threaded TSan build:
    PYTHON_GIL=0 .../release-ft-nojit-tsan/python list_remove_ft_plain_read.py
"""

import sys
import threading
import time

N = int(sys.argv[1]) if len(sys.argv) > 1 else 400


class Slow:
    """__eq__ that detaches the thread, releasing list.remove's critical section."""

    def __eq__(self, other):
        time.sleep(0)
        return False

    __hash__ = None


stop = threading.Event()
lst = [Slow() for _ in range(64)]


def remover():
    while not stop.is_set():
        try:
            lst.remove(Slow())
        except ValueError:
            pass
        except Exception:
            pass


def resizer():
    while not stop.is_set():
        try:
            lst.append(Slow())
            lst.append(Slow())
            del lst[:2]
        except Exception:
            pass


def main():
    threads = [threading.Thread(target=remover) for _ in range(3)]
    threads += [threading.Thread(target=resizer) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(6)
    stop.set()
    for t in threads:
        t.join()
    print("survived; len(lst) =", len(lst))


if __name__ == "__main__":
    main()
    print("completed")
