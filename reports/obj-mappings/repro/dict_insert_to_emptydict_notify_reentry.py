"""
Objects/dictobject.c:2103 (insert_to_emptydict)

    PyDictKeysObject *newkeys = new_keys_object(PyDict_LOG_MINSIZE, unicode); /* :2097 */
    ...
    _PyDict_NotifyEvent(PyDict_EVENT_ADDED, mp, key, value);   /* :2103  <-- runs Python */
    ...
    STORE_USED(mp, mp->ma_used + 1);                           /* :2121 */
    newkeys->dk_usable--; newkeys->dk_nentries++;              /* :2122-2123 */
    FT_ATOMIC_STORE_PTR_RELEASE(mp->ma_keys, newkeys);         /* :2129 */

The comment at :2124 explains that the keys are published LAST "so no one can see
them in a partially inconsistent state".  That reasoning holds for another
*thread*.  It does not hold for RE-ENTRANCY on this thread: because ma_keys is
still Py_EMPTY_KEYS while the notify at :2103 runs Python, a re-entrant insert
takes the insert_to_emptydict path too, builds and publishes ITS OWN keys
object, and sets ma_used = 1.  The outer frame then does ma_used += 1 (-> 2) and
overwrites ma_keys with its own single-entry table at :2129.

Result: ma_used == 2 with dk_nentries == 1.  len(d) lies, the inner keys object
and its strong reference to the inner key/value leak, and every consumer that
trusts ma_used (iteration, dictresize, dict_dealloc) walks past the end.

The notify runs Python because the stock _testcapi dict watcher formats the
event with PyUnicode_FromFormat("new:%S:%S", key, new_value)
(Modules/_testcapi/watchers.c:49) and %S calls PyObject_Str(new_value).
"""

import sys

import _testcapi


def main() -> int:
    print(sys.version)

    wid = _testcapi.add_dict_watcher(0)
    d = {}

    class Reenter:
        def __str__(self):
            d["inner"] = "I"
            return "x"

    _testcapi.watch_dict(wid, d)
    d["outer"] = Reenter()
    _testcapi.unwatch_dict(wid, d)
    _testcapi.clear_dict_watcher(wid)

    reported = len(d)
    walked = list(d.keys())
    print(f"  len(d)            = {reported}   <- ma_used")
    print(f"  list(d.keys())    = {walked}   (len {len(walked)})")
    print(f"  d == dict(d)      = {d == dict(d)}")

    if reported != len(walked):
        print(f"\nRESULT: ma_used ({reported}) disagrees with the published "
              f"table ({len(walked)} entries) -- inconsistent dict")
        # Now make a consumer trust ma_used.
        print("  forcing a resize through the inconsistent dict ...")
        for i in range(64):
            d[f"pad{i}"] = i
        print(f"  survived; len(d) = {len(d)}")
        return 1

    print("\nRESULT: consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
