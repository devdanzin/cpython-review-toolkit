# memory-pattern-analyzer — obj-mappings slice

Target `/home/danzin/projects/cpython` @ `4f3be1b5777`.
Scope: exactly `Objects/dictobject.c` (8,597 lines) and `Objects/setobject.c` (3,228 lines).

**Build-vs-ref check:** `git diff a1d580430c8 4f3be1b5777 -- Objects/dictobject.c
Objects/setobject.c Include/internal/pycore_dict.h Include/internal/pycore_setobject.h`
is **empty**. The `python_build_matrix` builds (all at `a1d580430c8`) and the target ref
carry byte-identical sources for every file cited below, so every reproduction and every
line number is valid at the reviewed ref.

---

## 1. Denominator line

**Scanner population for my class in this slice: 1 finding.**
`scanners/scan_memory_patterns.sample.json` `denominators`: `files_analyzed: 2`,
`functions_analyzed: 404`, `findings: 1`, `varobject_allocation_census.sites: 2`,
`.via_slot_pointer: 0`, `.non_constant_nitems: 0`. Against **7** in `Objects/` overall.

Per rule, with the kind of zero stated:

| rule | slice count | denominator | kind of zero |
|---|---|---|---|
| `gc_untrack_without_track` | **1** (low, `gate: "file"`, `tp_dealloc: null`) | 4 `PyObject_GC_New*` constructors across both files | — |
| `varobject_nitems_unguarded` | 0 | census `sites: 2`, `non_constant_nitems: 0`; **zero `*_NewVar` / `GC_Resize` call sites in either file** (grep) | **structural** — every var-object allocation in the slice passes a literal `0` (`_PyType_AllocNoTrack(type, 0)`, `setobject.c:1356`); the rule cannot fire here |
| `alloc_size_overflow` | 0 | **5** raw allocation sites (`dictobject.c:860, :917, :1036, :7265`; `setobject.c:527`), 4 of which carry a multiply | **evidential** — I read all 5 by hand, see §3 |
| `mismatched_alloc_free` | 0 | 5 allocation sites / 7 free sites, all `PyMem_*` | **evidential** |

**Read by hand:** all of `setobject.c` (3,228 lines) and roughly 4,600 lines of
`dictobject.c` — the allocator/sizing layer (`:440–1070`), the insert/resize/delete core
(`:1860–2400`, `:2940–3200`, `:3400–3660`), the keys/values/items builders (`:3850–4010`),
`popitem` and iterators (`:5010–5220`, `:5600–5680`, `:6420–6490`), and the shared-keys /
instance-attribute layer (`:7250–7550`, `:8290–8330`), plus
`Include/internal/pycore_dict.h:240–390`.

**Net-new findings that came from reading, not from the scanner: 6 of 6.** The scanner's
one finding is a previously-recorded bug. See §4 for the recall gap and the proposed rule.

---

## 2. Findings

Six of the seven findings below are one bug shape. Naming it once:

> **`stale-capacity-across-notify`** — a bound, an index, or a raw pointer into a dict's
> keys/values allocation is validated or captured, then a **CPython-initiated** call runs
> arbitrary Python, then the value is used to write with no re-validation.

The Python-running call is always the same two lines of CPython's own code:

* `Objects/dictobject.c:8314` — `_PyDict_SendEvent` calls `PyErr_FormatUnraisable` when a
  **dict watcher** callback returns `-1`;
* `Objects/typeobject.c:1223` — `_PyType_Modified_Unlocked` calls `PyErr_FormatUnraisable`
  when a **type watcher** callback returns `-1`.

`PyErr_FormatUnraisable` → `Python/errors.c:1737` `PyObject_CallOneArg` → `sys.unraisablehook`,
which is pure Python and user-settable.

**Why the watcher documentation does not discharge this.** `Doc/c-api/dict.rst:582` says
*"The callback may inspect but must not modify dict … Do not trigger Python code execution
in the callback."* My trigger does **neither**: `_testcapi`'s `dict_watch_callback_error`
only returns `-1` with an exception set — the behaviour `Doc/c-api/dict.rst:595`
**explicitly prescribes** (*"If the callback sets an exception, it must return -1; this
exception will be printed as an unraisable exception"*). It is CPython, not the callback,
that then runs Python. The obligation is documented onto the callback author and violated
by the runtime on the callback's behalf. `typeobject.c:1219-1220` already says so out loud:
*"Note that PyErr_FormatUnraisable is potentially re-entrant and the watcher callback might
be too."* — the comment exists; no caller acts on it.

Reachability is C-API, not pure Python: `PyDict_AddWatcher` / `PyType_AddWatcher` are public
since 3.12 and are exactly what JIT/tracing/profiling extensions use. `_testcapi` makes them
reachable from a plain script, which is how every reproduction below runs.

---

### [FIX] 2.1 `insert_split_key` writes at an index approved by a stale `dk_usable`
`Objects/dictobject.c:1964` (`insert_split_key`)

```c
    LOCK_KEYS(keys);
    ix = unicodekeys_lookup_unicode(keys, key, hash);
    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {           // :1964  capacity checked
        ...
        if (type) { _PyType_Modified_Unlocked(type); }       // :1971  RUNS PYTHON
        Py_ssize_t hashpos = find_empty_slot(keys, hash);    // :1973
        ix = keys->dk_nentries;                              // :1974  re-read AFTER
        dictkeys_set_index(keys, hashpos, ix);
        PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(keys)[ix];  // :1976  WRITE
        STORE_SHARED_KEY(ep->me_key, Py_NewRef(key));
        split_keys_entry_added(keys);                        // :1978  usable--, nentries++
    }
    assert (ix < SHARED_KEYS_MAX_SIZE);                      // :1980  assert-only
```

**Mechanism.** `dk_usable > 0` is the entries-array bound for the type's shared keys.
`_PyType_Modified_Unlocked` runs the unraisable hook, which sets an attribute and re-enters
`insert_split_key` on the same `keys`. Nested frames all pass the check against the *same*
pre-consumption `dk_usable`, then each writes a distinct, ever-higher `dk_nentries` on the
way out. `dk_usable` goes negative, `dk_nentries` runs past `SHARED_KEYS_MAX_SIZE` (30) and
past the physical entries array, and `store_instance_attr_lock_held:7497/:7513` then
reads/writes `values->values[ix]` past the object's inline-values region.

This is the GIL-build half of **CPY-0096** that the brief assigned to this slice; CPY-0096
itself is the free-threaded self-deadlock on `dk_mutex`. Under the GIL `LOCK_KEYS` is a
no-op, so there is nothing to deadlock on and the same re-entry silently corrupts instead.

**Guarded twin — and what its guard actually covers (lesson 3).**
`insert_combined_dict:1910` re-tests `dk_usable <= 0` and calls `insertion_resize`, and its
`assert(mp->ma_keys->dk_usable >= 0)` at `:1938` is the post-condition `insert_split_key`
never states. **But that twin's guard defends only against a *statically* full table, not
against consumption during the notify window** — §2.2 shows the twin is a live bug under the
same threat model. The only real twin is `dictobject.c:218-227`, the comment that forbids
calling `PyType_Modified()` under `LOCK_KEYS`; it is a comment, not a check, and it is about
lock order rather than about capacity.

**Reproduction** — `repro/insert_split_key_stale_usable_overflow.py [depth]`

| build | depth | result | runs |
|---|---|---|---|
| `release-gil-nojit-asan` | 40 | `heap-buffer-overflow` READ at `store_instance_attr_lock_held dictobject.c:7497` | **5/5** |
| `debug-gil-nojit` | 20 | SIGABRT, `dictobject.c:719: _PyDict_CheckConsistency: Assertion failed: 0 <= dk_usable && dk_usable <= usable` | **5/5** |
| `release-gil-nojit` | 20 and 40 | SIGSEGV (139) | **5/5** each |
| any | ≤ 12 | clean exit — the corruption needs depth to exceed the remaining `dk_usable` | 0/5 |

```
==1801184==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x7a6505e75058 ...
READ of size 8 at 0x7a6505e75058 thread T0
    #0 ... in store_instance_attr_lock_held Objects/dictobject.c:7497:27
    #14 ... in insert_split_key Objects/dictobject.c:1971:13
    #13 ... in _PyType_Modified_Unlocked Objects/typeobject.c:1223:21
    #12 ... in PyErr_FormatUnraisable Python/errors.c:1777:5
    #11 ... in format_unraisable_v Python/errors.c:1737:21
    #10 ... in PyObject_CallOneArg Objects/call.c:395:12
0x7a6505e75058 is located 0 bytes after 72-byte region [0x7a6505e75010,0x7a6505e75058)
SUMMARY: AddressSanitizer: heap-buffer-overflow Objects/dictobject.c:7497:27
```

**Fix.** Re-test `keys->dk_usable > 0` (and re-run `unicodekeys_lookup_unicode`, since the
key may now be present) after `_PyType_Modified_Unlocked` returns; or hoist
`_PyType_Modified_Unlocked` out of the mutated region entirely, which also fixes CPY-0096.

---

### [FIX] 2.2 `insert_combined_dict` — the same shape on ordinary dicts, one re-entry, no nesting — **NET NEW**
`Objects/dictobject.c:1910` (`insert_combined_dict`)

```c
    if (mp->ma_keys->dk_usable <= 0) {                                   // :1910
        if (insertion_resize(mp, 1) < 0) { return -1; }
    }
    _PyDict_NotifyEvent(PyDict_EVENT_ADDED, mp, key, value);             // :1917 RUNS PYTHON
    ...
    Py_ssize_t hashpos = find_empty_slot(mp->ma_keys, hash);             // :1920
    dictkeys_set_index(mp->ma_keys, hashpos, mp->ma_keys->dk_nentries);  // :1921
    ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[mp->ma_keys->dk_nentries];     // :1925
    STORE_KEY(ep, key);                                                  // :1926 WRITE
    ...
    assert(mp->ma_keys->dk_usable >= 0);                                 // :1938 assert-only
```

**Mechanism.** For a **combined** table the invariant is exact: `dk_usable == 0` ⟺
`dk_nentries == USABLE_FRACTION(DK_SIZE(keys))`, i.e. every entry slot is taken. So a single
re-entrant burst from the unraisable hook that consumes exactly the remaining slots leaves
the suspended outer frame writing at `DK_UNICODE_ENTRIES(keys)[usable_total]` — **exactly
one element past the end of the `new_keys_object` allocation**. No nesting, no split tables,
no instance attributes: a plain `dict` and a plain `d[k] = v`.

This is strictly worse than §2.1: it needs one re-entry rather than ~20, it produces a
clean one-past-the-end **WRITE** rather than a depth-dependent read, and the affected object
is the ordinary combined dict rather than a type's shared keys.

**Guarded twin.** None in this file for the notify window. The nearest correct model is
`keys_lock_held:3872` — `n = mp->ma_used; v = PyList_New(n); if (n != mp->ma_used) goto again;`
— which re-validates a cached count against the live field and *restarts*. That guard was
written for a different threat model (allocation-triggered re-entry) but its shape is
exactly the missing one here.

**Reproduction** — `repro/insert_combined_dict_stale_usable_overflow.py [burst]`
`burst` must equal the `dk_usable` in force at the check; sweeping 1..130 on
`debug-gil-nojit` found **burst=21 and burst=106** (the two resize boundaries after a
64-key seed).

| build | burst | result | runs |
|---|---|---|---|
| `release-gil-nojit-asan` | 21 | `heap-buffer-overflow` **WRITE** at `insert_combined_dict dictobject.c:1926` | **5/5** |
| `debug-gil-nojit` | 21 (and 106) | SIGABRT, `Assertion 'mp->ma_keys->dk_usable >= 0' failed`, `dictobject.c:1938` | **5/5** |
| `release-gil-nojit` | 21 | glibc `double free or corruption (!prev)` → SIGABRT | **5/5** |
| any | 4096 | clean — the burst crosses a resize boundary and the outer write lands in the fresh table | 0/1 |

```
==2670105==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x7581fe9ff070 ...
WRITE of size 8 at 0x7581fe9ff070 thread T0
    #0 ... in insert_combined_dict Objects/dictobject.c:1926:9
    #1 ... in insertdict Objects/dictobject.c:2051:13
    #2 ... in _PyEval_EvalFrameDefault Python/generated_cases.c.h:12634:27

0x7581fe9ff070 is located 0 bytes after 1520-byte region [0x7581fe9fea80,0x7581fe9ff070)
allocated by thread T0 here:
    #1 ... in new_keys_object Objects/dictobject.c:860:14
    #2 ... in dictresize Objects/dictobject.c:2220:15
```

**Fix.** Move `_PyDict_NotifyEvent` **before** the `dk_usable` test, or re-test
`dk_usable <= 0` (and re-resize) after it returns.

---

### [FIX] 2.3 `dict_popitem_impl` — raw `ep0`, borrowed `key` and index `i` all cached across the notify — **NET NEW**
`Objects/dictobject.c:5043` (`dict_popitem_impl`)

```c
    PyDictUnicodeEntry *ep0 = DK_UNICODE_ENTRIES(self->ma_keys);  // :5043 raw ptr cached
    i = self->ma_keys->dk_nentries - 1;                           // :5044 index cached
    while (i >= 0 && ep0[i].me_value == NULL) { i--; }
    key = ep0[i].me_key;                                          // :5050 BORROWED
    _PyDict_NotifyEvent(PyDict_EVENT_DELETED, self, key, NULL);   // :5051 RUNS PYTHON
    hash = unicode_get_hash(key);                                 // :5052 stale key READ
    value = ep0[i].me_value;                                      // :5053 stale ep0 READ
    STORE_KEY(&ep0[i], NULL);                                     // :5054 stale ep0 WRITE
    STORE_VALUE(&ep0[i], NULL);                                   // :5055 stale ep0 WRITE
    ...
    j = lookdict_index(self->ma_keys, hash, i);                   // :5074 NEW keys, OLD i
    assert(j >= 0);                                               // :5075 assert-only
    dictkeys_set_index(self->ma_keys, j, DKIX_DUMMY);             // :5077 j may be -1
    PyTuple_SET_ITEM(res, 0, key);                                // :5079 stale key ESCAPES
    STORE_KEYS_NENTRIES(self->ma_keys, i);                        // :5082 stale nentries
```

Four independent defects across one call. `d.clear()` from the hook frees the whole keys
object (`clear_lock_held:3149` → `dictkeys_decref` → `free_keys_object`) and the key strings
with it; a re-entrant insert merely *replaces* it, which is enough to make `i` index the
wrong table and to hand a `-1` index to `dictkeys_set_index`.

**Guarded twin.** `set_add_entry_takeref` (`setobject.c:264-306`) is the exemplar in this
slice: it caches `table = so->table` immediately before `PyObject_RichCompareBool` and, on
return, tests `if (table != so->table || entry->key != startkey) goto restart;` — plus a
second `if (freeslot->hash != -1) goto restart;` for the dummy-slot case. `dict_popitem_impl`
does none of that. **What that twin's guard actually addresses:** single-threaded re-entrancy
from a user `__eq__` (issue 1456209, cited at `setobject.c:379`) — the same threat model as
here, so it transfers directly. It does *not* address a concurrent thread; that is the
FT agents' question, not this one.

**Reproduction** — `repro/dict_popitem_stale_ep0_uaf.py {clear|resize}`

| build | mode | result | runs |
|---|---|---|---|
| `release-gil-nojit-asan` | clear | `heap-use-after-free` READ at `dict_popitem_impl dictobject.c:5052` | **5/5** |
| `debug-gil-nojit` | clear | SIGSEGV | **5/5** |
| `release-gil-nojit` | clear | SIGSEGV | **5/5** |
| `debug-gil-nojit` | resize | SIGSEGV | **5/5** |
| `release-gil-nojit` | resize | clean (survives; the replaced table happens to be compatible) | 0/1 |

```
==2683781==ERROR: AddressSanitizer: heap-use-after-free on address 0x6c9bf8236768 ...
READ of size 8 at 0x6c9bf8236768 thread T0
    #0 ... in dict_popitem_impl Objects/dictobject.c:5052:16
    #1 ... in dict_popitem Objects/clinic/dictobject.c.h:231:20
0x6c9bf8236768 is located 24 bytes inside of 45-byte region [0x6c9bf8236750,0x6c9bf823677d)
freed by thread T0 here:
    #1 ... in unicode_dealloc Objects/unicodeobject.c:1714:5
    #2 ... in _Py_Dealloc Objects/object.c:3319:5
previously allocated by thread T0 here:
    #1 ... in _PyUnicode_ResizeCompact Objects/unicodeobject.c:1114:31
SUMMARY: AddressSanitizer: heap-use-after-free Objects/dictobject.c:5052:16 in dict_popitem_impl
```

ASan reports the **borrowed-key** UAF first (`key` was freed by the hook's `d.clear()`);
the stale-`ep0` WRITEs at `:5054/:5055` are the second live consequence on the same run.

**Fix.** Issue the `PyDict_EVENT_DELETED` notify *before* caching `ep0`/`i`/`key`, and
re-derive all three afterwards; or `Py_INCREF(key)` across the notify and re-run the
`dk_nentries - 1` scan.

**Overlap note:** the borrowed-`key`-across-call half is also the refcount-auditor's
crown-jewel shape. The `ep0`/`i` half — a raw pointer *into a refcount-less allocation*
hanging off a live object — is the carve-out the briefing spells out under
"A raw `PyMem_Malloc` buffer hanging off a live object is NOT protected by its owner".

---

### [FIX] 2.4 `delitem_common` reached with an `ix` that indexes a freed table — **NET NEW**
`Objects/dictobject.c:3038` and `:3307` → `:2963`, `:2969-2990`

Both `_PyDict_DelItem_KnownHash_LockHeld` (`:3038`) and the `pop` helper (`:3307`) compute
`ix = _Py_dict_lookup(...)`, then call `_PyDict_NotifyEvent(PyDict_EVENT_DELETED, ...)`, then
pass the *pre-notify* `ix` to `delitem_common`, which does:

```c
    Py_ssize_t hashpos = lookdict_index(mp->ma_keys, hash, ix);   // :2962 NEW keys, OLD ix
    assert(hashpos >= 0);                                         // :2963 assert-only
    ...
    dictkeys_set_index(mp->ma_keys, hashpos, DKIX_DUMMY);         // :2976 hashpos may be -1
    PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[ix]; // :2978 OOB
    old_key = ep->me_key;  STORE_KEY(ep, NULL);  STORE_VALUE(ep, NULL);
    Py_DECREF(old_key);                                           // :2990 garbage/NULL
```

After a hook-driven `d.clear()`, `mp->ma_keys == Py_EMPTY_KEYS` — a **zero-entry, static**
keys object — and `ix` is 199. `lookdict_index` returns `-1`, the assert is compiled out on
release, and the code writes `DKIX_DUMMY` at a negative index and reads an entry 199 slots
into an 8-byte static array.

**Guarded twin.** Same as §2.3 (`set_add_entry_takeref`'s restart). Note this is *adjacent
to but distinct from* **CPY-0115** (`delitem_common:2987`, a live `PyDict_Next` cursor vs.
mutation): that lead is about an iteration cursor across threads, this is about a lookup
index across a synchronous, single-threaded notify.

**Reproduction** — `repro/notify_event_stale_ix_family.py {delitem|pop}`

| build | scenario | result | runs |
|---|---|---|---|
| `debug-gil-nojit` | delitem, pop | SIGABRT, `dictobject.c:2963: delitem_common: Assertion 'hashpos >= 0' failed` | **5/5** each |
| `release-gil-nojit-asan` | delitem | `SEGV on unknown address 0x0` at `delitem_common dictobject.c:2990` (`Py_DECREF(old_key)`) | **3/3** |
| `release-gil-nojit` | delitem, pop | dict left corrupt; `len(d)` → `SystemError: <built-in function len> returned NULL without setting an exception` (exit 1) | **5/5** each |

```
==2688135==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000 ...
    #0 ... in _Py_IsImmortal ./Include/refcount.h:132:12
    #1 ... in Py_DECREF ./Include/refcount.h:421:9
    #2 ... in delitem_common Objects/dictobject.c:2990:9
SUMMARY: AddressSanitizer: SEGV ./Include/refcount.h:132:12 in _Py_IsImmortal
```

**Fix.** Re-run `_Py_dict_lookup` after the notify, or move the notify to after
`delitem_common`.

---

### [FIX] 2.5 `clear_lock_held` frees keys it captured before its own notify — **NET NEW**
`Objects/dictobject.c:3136`

```c
    oldkeys   = mp->ma_keys;                                       // :3136 captured
    oldvalues = mp->ma_values;                                     // :3137 captured
    if (oldkeys == Py_EMPTY_KEYS) { return; }
    _PyDict_NotifyEvent(PyDict_EVENT_CLEARED, mp, NULL, NULL);     // :3142 RUNS PYTHON
    ensure_shared_on_resize(mp);
    STORE_USED(mp, 0);
    if (oldvalues == NULL) {
        set_keys(mp, Py_EMPTY_KEYS);
        assert(oldkeys->dk_refcnt == 1);                           // :3148 assert-only
        dictkeys_decref(oldkeys, IS_DICT_SHARED(mp));              // :3149 UAF
    }
```

A re-entrant insert from the hook triggers `insertion_resize` → `dictresize` →
`free_keys_object(oldkeys)`. `oldkeys` is then dereferenced and decref'd through freed
memory. The `PyDict_EVENT_CLEARED` event is *by design* delivered before the clear
(`Doc/c-api/dict.rst:593`: *"Callbacks occur before the notified modification to dict takes
place"*), which is precisely what opens the window.

**Guarded twin.** `dict_dealloc:3651-3655` — it brackets its own
`_PyDict_NotifyEvent(PyDict_EVENT_DEALLOCATED, ...)` with `_PyObject_ResurrectStart` /
`_PyObject_ResurrectEnd` and **re-reads `mp->ma_values` and `mp->ma_keys` at `:3656-3657`,
after the notify returns.** That is the correct model, in the same file, 500 lines away.
What its guard addresses is resurrection specifically — but the *ordering* it establishes
(notify first, capture second) is exactly the fix here.

**Reproduction** — `repro/notify_event_stale_ix_family.py doubleclear`

| build | result | runs |
|---|---|---|
| `release-gil-nojit-asan` | `heap-use-after-free` READ at `dictkeys_decref dictobject.c:496`, from `clear_lock_held` | **3/3** |
| `debug-gil-nojit` | SIGABRT, `dictobject.c:3148: clear_lock_held: Assertion 'oldkeys->dk_refcnt == 1' failed` | **5/5** |
| `release-gil-nojit` | SIGABRT | **5/5** |

```
==2693593==ERROR: AddressSanitizer: heap-use-after-free on address 0x7730361ee100 ...
READ of size 8 at 0x7730361ee100 thread T0
    #0 ... in dictkeys_decref Objects/dictobject.c:496:9
    #1 ... in clear_lock_held Objects/dictobject.c
    #2 ... in PyDict_Clear Objects/dictobject.c:3178:5
freed by thread T0 here:
    #1 ... in dictresize Objects/dictobject.c
    #2 ... in insertion_resize Objects/dictobject.c:1895:12
previously allocated by thread T0 here:
    #1 ... in new_keys_object Objects/dictobject.c:860:14
    #2 ... in dictresize Objects/dictobject.c:2220:15
SUMMARY: AddressSanitizer: heap-use-after-free Objects/dictobject.c:496:9 in dictkeys_decref
```

**Fix.** Capture `oldkeys`/`oldvalues` **after** the notify, as `dict_dealloc` does.

---

### [FIX] 2.6 `insert_to_emptydict` publishes over a keys object a re-entrant insert already published — **NET NEW**
`Objects/dictobject.c:2097`

```c
    PyDictKeysObject *newkeys = new_keys_object(PyDict_LOG_MINSIZE, unicode);  // :2097
    if (newkeys == NULL) { ...; return -1; }
    _PyDict_NotifyEvent(PyDict_EVENT_ADDED, mp, key, value);       // :2103 RUNS PYTHON
    ...                                                            // fills `newkeys`
    STORE_USED(mp, mp->ma_used + 1);                               // :2121
    newkeys->dk_usable--;  newkeys->dk_nentries++;                 // :2122-2123
    FT_ATOMIC_STORE_PTR_RELEASE(mp->ma_keys, newkeys);             // :2129 publishes
```

`mp->ma_keys` is still `Py_EMPTY_KEYS` during the notify, so a re-entrant `mp[k] = v` runs
`insert_to_emptydict` **again**, allocates and publishes its *own* keys object with its own
entry, and bumps `ma_used` to 1. The outer frame then publishes its keys object over that
one (`:2129`) — **leaking the inner keys object and the key/value references it owns** — and
`ma_used` ends at **2 with one real entry**.

The desync is not contained. `keys_lock_held:3868` presizes `PyList_New(mp->ma_used)` and
fills from `_PyDict_Next`, which walks real entries; the surplus slots stay **NULL** and the
only thing standing between that and Python is `assert(j == n)` at `:3888` (and the same
pattern at `:3937`, `:4000` for values/items). On a release build the NULL escapes into a
Python-visible `list`.

**Guarded twin.** `keys_lock_held:3872`'s `if (n != mp->ma_used) { Py_DECREF(v); goto again; }`
— the file already knows that a cached count can go stale and restarts. That guard covers
the count going stale *during the list allocation*; it does not, and cannot, repair a dict
whose `ma_used` is permanently wrong.

**Reproduction** — `repro/notify_event_stale_ix_family.py empty`

| build | result | runs |
|---|---|---|
| `debug-gil-nojit` | SIGSEGV | **5/5** |
| `release-gil-nojit` | SIGSEGV | **5/5** |
| `release-gil-nojit-asan` | SEGV | **3/3** |

gdb on `debug-gil-nojit` names the victim exactly:

```
Program received signal SIGSEGV, Segmentation fault.
list_sort_impl (self=0x7ffff74f3e30, ...) at Objects/listobject.c:3042
3042	            if (!Py_IS_TYPE(key, key_type)) {
#0  list_sort_impl (...) at Objects/listobject.c:3042
#1  list_sort (...) at Objects/clinic/listobject.c.h:273
#4  builtin_sorted (...) at Python/bltinmodule.c:2765
```

i.e. `sorted(d)` on the corrupted dict yields a `list` containing a NULL element.

**Fix.** Move the `_PyDict_NotifyEvent` after the `FT_ATOMIC_STORE_PTR_RELEASE`, or re-test
`mp->ma_keys == Py_EMPTY_KEYS` after the notify and fall through to `insertdict` if not.
Independently, `keys_lock_held` / `values_lock_held` / `items_lock_held` should truncate to
`j` rather than `assert(j == n)`, so an `ma_used` desync from *any* cause cannot put a NULL
into a Python list.

---

### [CONSIDER] 2.7 `insertdict`'s MODIFIED path — same shape, **not reproduced**
`Objects/dictobject.c:2060` (`insertdict`)

`ix` and `old_value` come from `_Py_dict_lookup` at the top of the function; `:2060` runs
`_PyDict_NotifyEvent(PyDict_EVENT_MODIFIED, ...)`; `:2067` then writes
`&DK_UNICODE_ENTRIES(mp->ma_keys)[ix]` with the stale `ix`. Structurally identical to §2.4.

**I could not reproduce it: 0/5 on `debug-gil-nojit`, 0/5 on `release-gil-nojit`, 0/3 on
`release-gil-nojit-asan`** (`repro/notify_event_stale_ix_family.py modify`). After the hook's
`d.clear()` the stale write lands inside `empty_keys_struct`'s static entries region rather
than on the heap, and neither ASan's global instrumentation nor the plain builds flagged it
in the configurations I tried. Reported as CONSIDER, not FIX, on that evidence — the shape
is certain, the exploit path is not.

---

### [CONSIDER] 2.8 Amplifiers of §2.1/§2.2 — bounds that are `assert()`-only

Once `dk_nentries` exceeds the entries array (§2.1) or `dk_usable` goes negative (§2.2),
four in-file walks lose their bound. None is an independent bug; each turns the corruption
into a *different* class of memory error, and all four are release-build-live:

| site | what it does with `dk_nentries` | consequence |
|---|---|---|
| `dictobject.c:3111` `clear_embedded_values` | `PyObject *refs[SHARED_KEYS_MAX_SIZE]` (30) with only `assert(nentries <= SHARED_KEYS_MAX_SIZE)` at `:3112`, indexed `refs[i]` for `i < nentries` | **STACK** buffer overflow |
| `dictobject.c:505-517` `dictkeys_decref` | `for (i = 0, n = dk->dk_nentries; i < n; i++) Py_XDECREF(entries[i].me_key/me_value)` | OOB read + `Py_XDECREF` of garbage pointers, at teardown |
| `dictobject.c:1062` `clone_combined_dict_keys` | `n = keys->dk_nentries` walk with `Py_INCREF` | OOB read + INCREF of garbage |
| `dictobject.c:5093` `dict_traverse` | `n = keys->dk_nentries` walk with `Py_VISIT` | OOB read during GC |

I attempted an isolated reproduction of the stack overflow
(`repro/clear_embedded_values_stack_bound.py`, depths 31/34/40 on three builds): every
configuration crashed, but the inline-values overflow at `store_instance_attr_lock_held:7497`
trips **first** in all of them, so I did **not** obtain an isolated
`clear_embedded_values` stack-smash frame — **0/6 isolated**. Recorded honestly as an
amplifier, not as a separate reproduced finding. The fix for §2.1 removes all four.

---

### [POLICY] 2.9 `set_repr_lock_held` fills a presized list with no bound check at all
`Objects/setobject.c:776-785`

```c
    keys = PyList_New(so->used);                                    // :776
    if (keys == NULL) { goto done; }
    Py_ssize_t pos = 0, idx = 0;
    while (set_next(so, &pos, &entry)) {
        PyList_SET_ITEM(keys, idx++, Py_NewRef(entry->key));        // :784  unbounded idx
    }
```

`PyList_SET_ITEM` is unchecked by definition, `idx` is bounded by nothing, and there is not
even an `assert(idx == so->used)` — where the dict twin `keys_lock_held:3872` re-tests
`n != mp->ma_used` and restarts, and `:3884/:3888` assert both `j < n` and `j == n`.

**Not currently live**, and the reason is worth recording because it is exactly a
lesson-3 case. The comment at `:774-775` says the code avoids `PySequence_List` because
*"it might re-lock the object lock or the GIL and allow something to clear the set from
underneath us"* (gh-129967). The remaining candidate for running Python between `:776` and
`:784` is an allocation-triggered GC inside `PyList_New` — and that vector is closed:
`Python/gc.c` `_PyObject_GC_Link` calls `_Py_ScheduleGC(tstate)`, which only sets the eval
breaker, so no collection runs inside the C call. So the dict twin's `goto again` guards a
threat model that no longer exists, and the set's *absence* of that guard is currently
harmless. It is still the weaker of the two and should get the assert.

---

### [CONSIDER] 2.10 `dictresize`'s size guard is four bits too permissive
`Objects/dictobject.c:2200`

```c
    if (log2_newsize >= SIZEOF_SIZE_T*8) { PyErr_NoMemory(); return -1; }   // >= 64
```

`new_keys_object(63, ...)` would compute `USABLE_FRACTION((size_t)1<<63)` =
`((1<<63) << 1)/3` = `0` (the shift wraps `size_t`), and `get_log2_bytes(63)` returns `66`,
so `(size_t)1 << 66` at `:861` is undefined behaviour. The true safe ceiling is
`log2_newsize <= 59` (the `entry_size * usable` term is ≈ `2^log2 * 10.67`).

**Not reachable from Python:** every caller derives `log2_newsize` from
`calculate_log2_keysize(GROWTH_RATE(mp))` or `estimate_log2_keysize(ma_used + other->ma_used)`,
and reaching 63 needs `ma_used ≳ 1.5·10^18` entries, each of which occupies ≥ 16 live bytes.
`dict_new_presized:2346` independently clamps the only externally-fed path to
`log2_max_presize = 17`. Latent; tighten the constant.

---

### [CONFIRM] Previously recorded — one line each

* **CPY-0015** `dictobject.c:5646 dictiter_new` — still exactly as recorded: `Py_DECREF(di)`
  at `:5646` on the `_PyTuple_FromPairSteal` failure path, before `_PyObject_GC_TRACK(di)` at
  `:5653`, and `dictiter_dealloc:5662` runs the unconditional macro `_PyObject_GC_UNTRACK`.
  **This is the scanner's single finding for this slice.** It arrived as
  `confidence: low, gate: "file", tp_dealloc: null` because `itertype` is a *parameter*;
  I resolved it by hand — all six iterator types (`dictobject.c:5807, :5930, :6223, :6364,
  :6406, :6418`) install `dictiter_dealloc`, so the file-level gate is correct here and the
  low confidence is understated.
* **CPY-0079** `dictobject.c:4494 copy_lock_held_untracked` — unchanged; assert derefs an
  unchecked allocation. Not re-litigated (its OOM re-sweep is the oom-reproducer's job).
* **CPY-0096** `dictobject.c:1971 insert_split_key` — confirmed; §2.1 above is the GIL-build
  consequence set the brief assigned to this slice.
* **CPY-0019**, **CPY-0107**, **CPY-0115** — outside my class; not re-examined.

---

## 3. Classes bounded clean (with denominators)

**Allocation-size arithmetic — 5/5 raw allocation sites read, 0 findings.**

| site | size expression | bound |
|---|---|---|
| `dictobject.c:860` `new_keys_object` | `sizeof(PyDictKeysObject) + (1 << log2_bytes) + entry_size * usable` | `log2_size` is `uint8_t`; `dictresize:2200` rejects ≥ 64; every caller derives it from an existing dict's `ma_used`. Latent gap at §2.10. |
| `dictobject.c:917` `new_values` | `values_size_from_count(size)` = `(count+1)*8 + round_up(count,8)` | `count = shared_keys_usable_size(keys)` ≤ 30; `assert(size < 256)` |
| `dictobject.c:1036` `clone_combined_dict_keys` | `_PyDict_KeysSize(orig->ma_keys)` | **bounded-by-an-existing-allocation** |
| `dictobject.c:7265` `_PyDict_NewKeysForClass` | `sizeof(_instancekeysobject) + (1<<log2_bytes) + sizeof(PyDictUnicodeEntry)*usable` | all three terms are compile-time constants (`NEXT_LOG2_SHARED_KEYS_MAX_SIZE = 6`) |
| `setobject.c:527` `set_table_resize` | `PyMem_NEW(setentry, newsize)` | `PyMem_New` carries the `(size_t)(n) > PY_SSIZE_T_MAX / sizeof(type)` check (`Include/pymem.h:63`); the `while (newsize <= (size_t)minused) newsize <<= 1;` loop at `:498` terminates at `2^63 > PY_SSIZE_T_MAX` — no infinite loop, no wrap |

**The lying-`__len__` / `__length_hint__` path the brief asked about does not exist in this
slice.** Grepping `PyObject_Length` / `PySequence_Size` / `_PyObject_LengthHint` /
`PyObject_Size` over both files: the only hits are `dictobject.c:6547, :6550, :6709, :6947`,
all in dict-view set operations feeding *comparisons*, never an allocation. The presize
paths are `dict_dict_fromkeys:3416` (`PyDict_GET_SIZE`), `dict_set_fromkeys:3441`
(`PySET_GET_SIZE`), `dict_merge:4253` (`ma_used + other->ma_used`) and
`_PyDict_FromItems:2393` (a C-caller `length`) — all bounded-by-an-existing-allocation — and
`_PyDict_NewPresized` → `dict_new_presized:2346`, which **clamps to `log2_max_presize = 17`**
with an explicit comment. `dict.fromkeys(huge_iterable)` reaches `dict_fromkeys_impl:4027`,
which never presizes from a hint. `set(...)` never presizes at all: `make_new_set_untracked`
always starts at `PySet_MINSIZE` and grows through `set_add_entry`. **Clean, and the reason
is structural.**

**Allocator families — 5 allocations / 7 frees, 0 mismatches.** Every raw allocation in
both files is `PyMem_*` and every raw free is `PyMem_Free` / `_PyMem_FreeDelayed` /
`_Py_FREELIST_FREE(dictkeys, keys, PyMem_Free)`. Objects use `PyObject_GC_New` /
`_PyType_AllocNoTrack` with `PyObject_GC_Del` as `tp_free`. One near-miss checked and
cleared: `free_keys_object:893` pushes `keys` (not the `ptr` that was adjusted for the split
case at `:882`) onto the freelist, but the branch is gated on
`keys->dk_kind == DICT_KEYS_UNICODE`, which excludes `DICT_KEYS_SPLIT` — the only kind for
which `ptr != keys`. Correct.

**GC track/untrack — 4 `PyObject_GC_New*` constructors, 1 finding (CPY-0015).**
`_PyDictView_New:6475`, `set_iter:1175` and `new_dict/new_dict_impl:976/:990/:1002` have no
fallible step between the allocation and the track. `dict_dealloc:3661` and `set_dealloc:742`
use the untracked-*tolerant* **function** `PyObject_GC_UnTrack` (not NULL-safe —
`_PyObject_GC_IS_TRACKED` dereferences unconditionally), so their constructors are
true negatives. `_PyFrozenSet_MaybeUntrack:1427` uses the unconditional **macro**, and I
checked both callers (`make_new_frozenset:1443`, `PyFrozenSet_New:3043`): each receives a
`make_new_set(&PyFrozenSet_Type, …)` result tracked at `:1383`, and the
`PyFrozenSet_CheckExact` guard at `:1416` rejects subclasses. No path reaches it untracked
**in-tree** — but it is a `static` function, so the guarantee is local and complete.
ACCEPTABLE.

**Double-free — 0.** No pointer is freed twice on any path in either file; every `goto`
ladder I read either NULLs or transfers before the second exit. The §2.5 finding is a
use-after-free, not a double-free (the second `dictkeys_decref` operates on already-freed
memory rather than re-freeing a live block).

**Classic buffer-overflow greps — 0.** `sprintf`, `strcpy`, `strcat`, `alloca`: zero hits in
either file.

**`set_add_entry_takeref` restart loop — audited, correct.** Both the
`table != so->table || entry->key != startkey` restart at `:294` and the
`freeslot->hash != -1` restart at `:311` are present and cover the resize-during-`__eq__`
and the dummy-slot-reuse cases respectively. This is the one place in the slice where the
"cache, run Python, re-validate" discipline is applied properly — which is what makes its
absence in six `dictobject.c` functions a finding rather than a house style.

---

## 4. Toolkit feedback

### 4.1 Recall gap (the highest-value output): a missing rule, 6/6 of my findings

`scan_memory_patterns.py` has four rules — `alloc_size_overflow`,
`varobject_nitems_unguarded`, `gc_untrack_without_track`, `mismatched_alloc_free`. **None of
them can see any of §2.1–§2.6.** Three of the four produced a zero here; the fourth found a
bug already in the catalog. The net-new yield of the scanner on this slice was **0**, and the
net-new yield of reading was **6 reproduced crashes**.

The shape they all share is not an arithmetic error and not an allocator mismatch. It is:

> a **capacity test, index, or raw interior pointer** is established, an intervening call can
> **run arbitrary Python**, and the value is then used to **write** with no re-validation.

**Proposed rule `stale_bound_across_python_call`.** It is mechanically detectable with the
primitives the scanner already has:

1. Collect, per function, "bound establishers": a comparison against a capacity-ish field
   (`->dk_usable`, `->ma_used`, `->dk_nentries`, `->mask`, `->capacity`, `->allocated`,
   `->size`, `->used`, `->fill`), an assignment from a lookup returning an index
   (`_Py_dict_lookup`, `lookdict_index`, `set_lookkey`), or an assignment of a pointer
   derived from a container macro (`DK_ENTRIES`, `DK_UNICODE_ENTRIES`, `->table`,
   `->ma_values`, `->ma_keys`, `->values`).
2. Between establisher and use, look for a call in a **Python-reaching** set. Crucially this
   set must be **widened beyond `PyObject_Call*`** to include CPython's own indirect
   dispatchers, which is what the briefing's borrowed-ref entry already asks for:
   `_PyDict_NotifyEvent`, `_PyDict_SendEvent`, `_PyType_Modified`, `_PyType_Modified_Unlocked`,
   `PyErr_FormatUnraisable`, `PyErr_WriteUnraisable`, `Py_DECREF`/`Py_XDECREF`/`Py_CLEAR` of a
   non-provably-builtin operand, `FT_CLEAR_WEAKREFS`, `PyObject_ClearWeakRefs`.
   `PyErr_FormatUnraisable` alone is worth adding on its own: it is the runtime's *sanctioned*
   error-reporting path out of every watcher callback, and it calls `sys.unraisablehook`.
3. Report if the establisher's value is then used as an index, a pointer deref, or a store
   target, **with no re-read of the field it came from** in between.

Confidence tiers that would have ranked my findings correctly:
* `high` — the Python-reaching call is a watcher/unraisable dispatcher **and** the use is a
  write (`insert_split_key`, `insert_combined_dict`, `dict_popitem`, `clear_lock_held`);
* `medium` — the use is a read, or the establisher is a plain field compare;
* suppress when the establisher is re-read after the call (`dict_dealloc:3656-3657` is the
  in-file negative; `set_add_entry_takeref:294` is the restart form; `pairwise_next:364`,
  already in the taxonomy, is the third).

**A cheap high-yield sub-rule, orthogonal to the above:** flag any function where an
`assert(X)` on a **capacity or index bound** is the only thing standing between a
Python-reachable value and an array write. `dictobject.c:1938`, `:1980`, `:2963`, `:3112`,
`:3148`, `:3888`, `:5075` are all in this slice; five of the seven fired as SIGABRT in my
debug-build reproductions and are silent memory corruption on release. Grep-level rule:
`assert(` whose expression names a field in the capacity set **and** which is followed
within the same function by an indexing expression using the asserted variable.

### 4.2 Precision per rule, this slice

* `gc_untrack_without_track` — 1 candidate, **1 true positive** (CPY-0015). Precision 1/1.
  But note the gate did its documented failure mode: `gate: "file"` / `tp_dealloc: null`
  because the type arrives as the `itertype` *parameter*. Here it was right. **Proposal:**
  when the type is a parameter, resolve it from the `tp_dealloc` slot of every static
  `PyTypeObject` in the file whose `tp_iternext` or `tp_new` is in the same family; in this
  file that resolves unambiguously (six types, one `dictiter_dealloc`) and would promote the
  finding from `low` to `medium`.
* `varobject_nitems_unguarded` — **structural zero, correctly reported as such** by the
  `varobject_allocation_census` block. The census is doing exactly its job: it let me say
  "0 out of 2 sites, both with a literal count" instead of "0". Keep it; consider adding the
  same census block to `alloc_size_overflow` (an `alloc_site_census` with
  `constant_only` / `bounded_by_existing` / `tainted` buckets), which would have let me
  report that rule's zero as evidential without hand-grepping five call sites.
* `alloc_size_overflow`, `mismatched_alloc_free` — evidential zeros, both correct. The
  `bounded-by-an-existing-allocation` suppression is doing real work here: `:1036` and the
  four `fromkeys`/`merge` presize sites would all have been noise.

### 4.3 One factual correction for the FP taxonomy

The taxonomy's *Memory patterns* section says `PyMem_New` / `PyMem_Resize` overflow-check
internally, and the *Allocators* section says they do **not** raise. Both are right and they
are easy to conflate — `setobject.c:527` is the exemplar that needs both halves
(`PyMem_NEW` is checked *and* the caller must `PyErr_NoMemory()`, which it does at `:529`).
Worth one cross-reference line between the two sections; I had to read `Include/pymem.h:63`
to be sure.

---

## 5. Noticed outside slice

* `Objects/typeobject.c:1219-1223` — the comment *"Note that PyErr_FormatUnraisable is
  potentially re-entrant and the watcher callback might be too"* documents the hazard that
  §2.1 exploits, at the call site that causes it. The type-watcher docs should carry the same
  "do not trigger Python" language `Doc/c-api/dict.rst:582` already has for dict watchers —
  and both should note that returning `-1` *does* trigger Python via `sys.unraisablehook`.
* `Python/errors.c:1737` `format_unraisable_v` — `PyObject_CallOneArg` on `sys.unraisablehook`
  is the single choke point through which all six of my findings run. A `Py_EnterRecursiveCall`
  there, or a documented "runs arbitrary Python" annotation, would be a one-line systemic
  mitigation. It is also the reason `PyErr_FormatUnraisable` belongs in every toolkit's
  `PYTHON_REACHING_APIS` table.
* `Objects/listobject.c:3042` `list_sort_impl` — dereferences list elements with no NULL
  check; §2.6's NULL-in-a-list is what turns a dict bookkeeping error into a `sorted()`
  SIGSEGV. Belongs to the `obj-listobject` slice.
* `Include/pymem.h:63` — `PyMem_New`'s check is `> PY_SSIZE_T_MAX / sizeof(type)`, i.e. it
  bounds the product by `PY_SSIZE_T_MAX` rather than `SIZE_MAX`; correct but stricter than
  the raw allocator, worth one line in the toolkit's allocator table.

---

## 6. Reproducers

All in `reports/obj-mappings/repro/`, all `.py` files, all run against
`~/projects/python_build_matrix/builds/<name>/python`:

| file | finding | invocation |
|---|---|---|
| `insert_split_key_stale_usable_overflow.py` | §2.1 | `<py> … 40` (ASan) / `… 20` (debug, release) |
| `insert_combined_dict_stale_usable_overflow.py` | §2.2 | `<py> … 21` |
| `dict_popitem_stale_ep0_uaf.py` | §2.3 | `<py> … clear` (also `resize`) |
| `notify_event_stale_ix_family.py` | §2.4 / §2.5 / §2.6 / §2.7 | `<py> … {delitem\|pop\|doubleclear\|empty\|modify}` |
| `clear_embedded_values_stack_bound.py` | §2.8 (not isolated) | `<py> … 34` |

Run ASan builds with `ASAN_OPTIONS=detect_leaks=0`; add `symbolize=0` for the N/M sweeps
(symbolization dominates the wall clock and can exceed a 60 s timeout).
