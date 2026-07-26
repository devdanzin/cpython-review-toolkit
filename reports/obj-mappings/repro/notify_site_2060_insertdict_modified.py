"""notify site :2060 -- insertdict(), MODIFIED branch.

Objects/dictobject.c:

    2038:  ix = _Py_dict_lookup(mp, key, hash, &old_value);   <-- ix + old_value
    ...
    2060:  _PyDict_NotifyEvent(PyDict_EVENT_MODIFIED, mp, key, value);  <-- window
    2067:  PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[ix];
    2068:  STORE_VALUE(ep, value);          <-- stale ix into a re-read ma_keys
    2076:  Py_XDECREF(old_value);           <-- stale borrowed ref

Two independent consequences, so two independent hooks:

  clear         d.clear() frees the keys object, which DECREFs every value.
                old_value hits refcount 0 and is freed.  :2076 then DECREFs
                freed memory.  NOTE: the earlier failed attempt stored small
                ints (immortal, DECREF is a no-op) -- the values here are
                heap objects with refcount 1.

  regrow        d.clear() then re-insert a handful of keys.  ma_keys is now a
                *heap* 8-slot table, so the stale ix (199) write at :2067 lands
                past the end of a live heap allocation instead of inside the
                static empty_keys_struct.  This is the fix for the diagnosis
                that the previous attempt's write went into BSS.

  resize        no clear at all: the hook forces a split->combined + resize by
                bulk insert, so ma_keys is replaced by a *larger* heap table.
                stale ix then indexes a valid-but-wrong entry.

Usage:  python notify_site_2060_insertdict_modified.py [clear|regrow|resize] [N]
"""

import sys

import _testcapi

MODE = sys.argv[1] if len(sys.argv) > 1 else "clear"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 200

TARGET = "k%d" % (N - 1)


def build():
    d = {}
    for i in range(N):
        # heap objects, refcount exactly 1 (held only by the dict) --
        # NOT small ints, which are immortal and make :2076 a no-op.
        d["k%d" % i] = [i]
    return d


def main():
    d = build()
    fired = []

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        if MODE == "clear":
            d.clear()
        elif MODE == "regrow":
            d.clear()
            for j in range(3):
                d["r%d" % j] = [j]
        elif MODE == "resize":
            for j in range(N * 8):
                d["g%d" % j] = [j]
        else:
            raise SystemExit("unknown mode %r" % MODE)

    sys.unraisablehook = hook
    wid = _testcapi.add_dict_watcher(1)  # dict_watch_callback_error
    _testcapi.watch_dict(wid, d)
    print("[main] armed mode=%s N=%d" % (MODE, N), flush=True)

    # MODIFIED: key already present, so insertdict takes the :2060 branch.
    d[TARGET] = ["replacement"]

    print("[main] returned from insertdict", flush=True)
    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__
    print("[main] len=%d" % len(d), flush=True)
    print("[main] real entries=%d" % len(list(d.keys())), flush=True)
    if len(d) != len(list(d.keys())):
        print("[main] *** ma_used disagrees with entry count ***", flush=True)
    # touch every value: a dangling entry shows up here
    tot = 0
    for k, v in d.items():
        tot += len(repr(v))
    print("[main] survived, checksum=%d" % tot, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
