# `itertools._grouper.__next__` has the same re-entrant use-after-free that was fixed in `groupby.__next__`

## Bug description

gh-143543 / commit a91b5c3fb5a fixed a re-entrant use-after-free in `groupby_next` by snapshotting `gbo->tgtkey` and `gbo->currkey` with `Py_INCREF` before calling `PyObject_RichCompareBool`. However, the sibling function `_grouper_next` (the inner group iterator) has the exact same unprotected comparison at `Modules/itertoolsmodule.c:681`:

```c
rcmp = PyObject_RichCompareBool(igo->tgtkey, gbo->currkey, Py_EQ);
```

Neither `igo->tgtkey` nor `gbo->currkey` is held with a strong reference during the comparison. A user-defined `__eq__` that re-enters the `_grouper` iterator can trigger `groupby_step`, which calls `Py_XSETREF(gbo->currkey, newkey)` — freeing the old `currkey` while `PyObject_RichCompareBool` still holds a dangling pointer to it as local variable `w`. When `__eq__` returns `NotImplemented`, `do_richcompare` tries the reverse comparison `w.__eq__(v)` on the freed object.

## Reproducer

Crashes with a segfault (tested on 3.14 debug build with ASAN):

```python
import itertools

grouper_iter = None

class Key:
    __hash__ = None

    def __init__(self, do_advance):
        self.do_advance = do_advance
        self.payload = bytearray(256)

    def __eq__(self, other):
        if self.do_advance:
            self.do_advance = False
            if grouper_iter is not None:
                try:
                    next(grouper_iter)
                except StopIteration:
                    pass
            for _ in range(50):
                bytearray(256)
            return NotImplemented
        return True

def keyfunc(element):
    if element == 0:
        return Key(do_advance=True)
    return Key(do_advance=False)

g = itertools.groupby(range(4), keyfunc)
key, grouper_iter = next(g)
items = list(grouper_iter)  # segfault
```

## Root cause

`_grouper_next` at line 681 passes `igo->tgtkey` and `gbo->currkey` to `PyObject_RichCompareBool` without incrementing their refcounts. `PyObject_RichCompareBool` → `PyObject_RichCompare` → `do_richcompare` does **not** INCREF the arguments. When `__eq__` re-enters the `_grouper` iterator:

1. Inner `_grouper_next` calls `groupby_step`
2. `groupby_step` calls `Py_XSETREF(gbo->currkey, newkey)` — this decrefs the old `currkey` to refcount 0, freeing it
3. `__eq__` returns `NotImplemented`
4. `do_richcompare` tries the reverse: `w.__eq__(v)` where `w` is the freed `currkey` — **use-after-free**

## Suggested fix

Apply the same fix from `groupby_next` (commit a91b5c3fb5a) to `_grouper_next`:

```c
// Before (vulnerable):
rcmp = PyObject_RichCompareBool(igo->tgtkey, gbo->currkey, Py_EQ);

// After (safe):
PyObject *tgtkey = igo->tgtkey;
PyObject *currkey = gbo->currkey;
Py_INCREF(tgtkey);
Py_INCREF(currkey);
int rcmp = PyObject_RichCompareBool(tgtkey, currkey, Py_EQ);
Py_DECREF(tgtkey);
Py_DECREF(currkey);
```

## Versions affected

Same as gh-143543 — all versions with `itertools.groupby`. The fix for `groupby_next` was applied to 3.13 and 3.14; this sibling function needs the same treatment.
