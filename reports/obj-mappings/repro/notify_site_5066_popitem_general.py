"""notify site :5066 -- dict_popitem_impl, the GENERAL (non-unicode) keys branch.

Objects/dictobject.c:5057-5072 (twin of the already-reproduced :5051 unicode branch)

    5058:  PyDictKeyEntry *ep0 = DK_ENTRIES(self->ma_keys);   <-- raw entry pointer
    5059:  i = self->ma_keys->dk_nentries - 1;                <-- index
    ...
    5065:  key = ep0[i].me_key;
    5066:  _PyDict_NotifyEvent(PyDict_EVENT_DELETED, self, key, NULL);  <-- window
    5067:  hash  = ep0[i].me_hash;     <-- READ through a possibly-freed ep0
    5068:  value = ep0[i].me_value;    <-- READ
    5069:  STORE_KEY(&ep0[i], NULL);   <-- WRITE
    5070:  STORE_HASH(&ep0[i], -1);    <-- WRITE
    5071:  STORE_VALUE(&ep0[i], NULL); <-- WRITE
    5074:  j = lookdict_index(self->ma_keys, hash, i);  assert(j >= 0)
    5079:  PyTuple_SET_ITEM(res, 1, value);   <-- possibly a raw C NULL into a tuple

Reaching the general branch needs a non-str key, which forces
DICT_KEYS_GENERAL.  d.clear() in the hook drops the keys object's last
reference -> free_keys_object -> PyMem_Free.

Usage:  python notify_site_5066_popitem_general.py [clear|regrow] [N]
"""

import sys

import _testcapi

MODE = sys.argv[1] if len(sys.argv) > 1 else "clear"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 200


def main():
    d = {}
    # A non-str key at position 0 forces a DICT_KEYS_GENERAL table for the
    # whole dict, so popitem() takes the :5057 branch.
    d[(0,)] = ["seed"]
    for i in range(1, N):
        d["k%d" % i] = [i]

    fired = []

    def hook(unraisable):
        if fired:
            return
        fired.append(1)
        d.clear()
        if MODE == "regrow":
            for j in range(3):
                d[(j, j)] = [j]

    sys.unraisablehook = hook
    wid = _testcapi.add_dict_watcher(1)
    _testcapi.watch_dict(wid, d)
    print("[main] armed mode=%s N=%d" % (MODE, N), flush=True)

    item = d.popitem()

    print("[main] popitem returned", flush=True)
    _testcapi.unwatch_dict(wid, d)
    sys.unraisablehook = sys.__unraisablehook__
    print("[main] len=%d" % len(d), flush=True)
    print("[main] item[0]=%r" % (item[0],), flush=True)
    print("[main] item[1]=%r" % (item[1],), flush=True)
    print("[main] survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
