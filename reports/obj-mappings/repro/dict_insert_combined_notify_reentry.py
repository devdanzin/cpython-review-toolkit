"""
Objects/dictobject.c:1917 (insert_combined_dict)

insert_combined_dict() secures capacity first:

    if (mp->ma_keys->dk_usable <= 0) {         /* :1910 */
        if (insertion_resize(mp, 1) < 0) ...
    }
    _PyDict_NotifyEvent(PyDict_EVENT_ADDED, mp, key, value);   /* :1917  <-- runs Python */
    Py_ssize_t hashpos = find_empty_slot(mp->ma_keys, hash);   /* :1920 */
    ...
    ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[mp->ma_keys->dk_nentries];  /* :1925 */
    STORE_KEY(ep, key); STORE_VALUE(ep, value);
    STORE_KEYS_USABLE(mp->ma_keys, mp->ma_keys->dk_usable - 1); /* :1936 */
    assert(mp->ma_keys->dk_usable >= 0);                        /* :1938 */

The notify at :1917 sits BETWEEN the capacity check and the write, and it can run
arbitrary Python: the stock _testcapi dict watcher formats the event with
PyUnicode_FromFormat("new:%S:%S", key, new_value) (Modules/_testcapi/watchers.c:49),
and %S calls PyObject_Str(new_value).

If that __str__ inserts into the same dict and consumes the last usable slot, the
outer frame resumes with dk_usable == 0 and dk_nentries == capacity, and writes
one entry past the end of the entries array.

Run: <python> dict_insert_combined_notify_reentry.py [prefill]
With no argument it sweeps prefill 0..12 in separate subprocesses.
"""

import os
import subprocess
import sys


def run_one(prefill: int) -> int:
    import _testcapi

    wid = _testcapi.add_dict_watcher(0)  # the formatting watcher (uses %S)

    d = {}
    for i in range(prefill):
        d[f"k{i}"] = 0

    class Reenter:
        def __str__(self):
            # Re-enter the same dict from inside the ADDED notification, i.e.
            # after insert_combined_dict() already decided no resize was needed.
            d["reentrant"] = 1
            return "x"

    _testcapi.watch_dict(wid, d)
    d["outer"] = Reenter()
    _testcapi.unwatch_dict(wid, d)
    _testcapi.clear_dict_watcher(wid)

    # Touch every entry -- an out-of-bounds slot shows up here too.
    n = len(d)
    items = list(d.items())
    assert len(items) == n, (len(items), n)
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        return run_one(int(sys.argv[1]))

    print(sys.version)
    bad = []
    for prefill in range(0, 13):
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), str(prefill)],
            capture_output=True,
            text=True,
        )
        tag = "ok"
        if proc.returncode != 0:
            tag = f"rc={proc.returncode}"
            bad.append((prefill, proc.returncode))
        tail = (proc.stderr or "").strip().splitlines()
        note = tail[-1] if tail else ""
        print(f"  prefill={prefill:2d}  {tag:12s} {note[:140]}")
    print()
    if bad:
        print(f"RESULT: crashed/aborted at prefill {[p for p, _ in bad]}")
        return 1
    print("RESULT: no crash observed in this sweep")
    return 0


if __name__ == "__main__":
    sys.exit(main())
