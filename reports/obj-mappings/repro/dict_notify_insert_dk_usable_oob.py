"""obj-mappings / refcount-auditor

insert_combined_dict (Objects/dictobject.c:1899-1939) validates
`mp->ma_keys->dk_usable <= 0` at :1910 and resizes if needed, THEN calls
_PyDict_NotifyEvent(PyDict_EVENT_ADDED, ...) at :1917, and only afterwards
writes `DK_UNICODE_ENTRIES(mp->ma_keys)[mp->ma_keys->dk_nentries]` at :1925.

_PyDict_SendEvent runs PyErr_FormatUnraisable() -> sys.unraisablehook when the
watcher callback returns -1, so arbitrary Python executes between the check and
the write.  If that Python consumes the last usable slot, dk_nentries equals the
allocated entry count and the store at :1926-1927 runs one entry past the end of
the PyDictKeysObject block.

Strategy: grow `d` until exactly one usable slot remains (detected via the
sys.getsizeof step), then let the hook consume it.

Run on a GIL ASan build:
  ~/projects/python_build_matrix/builds/debug-gil-nojit-asan/python <thisfile>
"""

import sys

import _testcapi


def fill_to_one_usable_slot():
    """Return a dict with exactly one usable entry slot left."""
    d = {}
    d["seed"] = 0
    size = sys.getsizeof(d)
    i = 0
    # grow until the table resizes, then stop one insert before the next resize
    while True:
        d["k%d" % i] = i
        i += 1
        new = sys.getsizeof(d)
        if new != size:
            size = new
            break
    # now the table has just been resized; fill it until one more insert
    # would resize again, using a probe dict to learn the boundary
    probe = dict(d)
    j = i
    while sys.getsizeof(probe) == size:
        probe["k%d" % j] = j
        j += 1
    # probe resized on the insert of key j-1; so d can take (j-1-i) more keys
    # before dk_usable reaches 0.  Leave exactly one slot.
    for n in range(i, j - 2):
        d["k%d" % n] = n
    assert sys.getsizeof(d) == size
    return d


d = fill_to_one_usable_slot()
print("prepared dict: len=%d sizeof=%d" % (len(d), sys.getsizeof(d)), flush=True)

WATCHER = _testcapi.add_dict_watcher(1)  # error callback: RuntimeError, return -1
_testcapi.watch_dict(WATCHER, d)

_fired = False


def hook(unraisable):
    global _fired
    if _fired:
        return
    _fired = True
    # consume the last usable slot from inside the ADDED notification
    d["consumed_by_hook"] = object()


sys.unraisablehook = hook

print("before outer insert", flush=True)
d["outer_key"] = object()
print("after outer insert (no crash); len=%d" % len(d), flush=True)
