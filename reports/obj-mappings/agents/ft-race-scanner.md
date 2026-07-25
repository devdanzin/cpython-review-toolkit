# ft-race-scanner — obj-mappings slice

Target `/home/danzin/projects/cpython` @ `4f3be1b5777`.
Scope: **exactly** `Objects/dictobject.c` (8,597 L) and `Objects/setobject.c` (3,228 L).

**Build identity check (lesson 4 prerequisite):** both slice files are **byte-identical**
between `4f3be1b5777` and the `python_build_matrix` tree at `a1d580430c8`
(`diff <(git show 4f3be1b5777:$f) builds/debug-ft-nojit/$f` → no output, both files).
Every line number below is quotable against both.

---

## 1. Denominator

| population | N | source |
|---|---|---|
| `scan_ft_races` slice findings triaged | **20 / 20** | `scanners/scan_ft_races.sample.json` `denominators.findings` |
| — of which `atomic_plain_asymmetry` | 17 | (all in dictobject.c) |
| — `guarded_writer_unguarded_reader` | 2 | (both in dictobject.c) |
| — `publish_before_init_complete` | 1 | (setobject.c) |
| scanner functions analysed | 404 | `denominators.functions_analyzed` |
| scanner `iternext_functions` | 8 | `denominators.iternext_functions` |
| scanner `lock_held_functions` (suppressed) | 33 | `denominators.lock_held_functions` |
| whole-`Objects/` comparison | 58 | 20/58 = 34 % of the tree's ft-race candidates sit in these 2 files |
| **`LOCK_KEYS` regions enumerated by hand** | **7 / 7** | the deliverable, §3 |
| **`tp_iternext` implementations enumerated** | **5 / 5** | §4 (scanner's 8 counts `_lock_held` delegates twice) |
| **two-armed `#ifdef Py_GIL_DISABLED` splits diffed** | **24 / 24** | §6 |
| plain `ma_keys` / `ma_values` assignments audited | **11 / 11** | §5.3 |
| `memcpy`/`memset` sites audited | **13 / 13** (dict 7, set 6) | §6 |
| `so->fill` accesses audited | **21 / 21** | §6 |
| lines actually read | ~2,600 of 11,825 (dict ~1,900, set ~700) | |

Scanner precision on this slice: **2 / 20 promoted, 18 dismissed → 10 %.**
Of the 5 FIX/CONSIDER findings below, **3 are net-new and were found by reading, not by the
scanner** (§8).

---

## 2. Confirmations (one line each, not re-litigated)

- **CPY-0096** `insert_split_key:1971` — `_PyType_Modified_Unlocked` under `LOCK_KEYS`.
  Still present at `4f3be1b5777`, and per the history agent this is **iteration four**
  (`efb2fffae1f` → `bef57062226` → revert `9626ef87f42` → re-land `20921922fa4`); the revert
  discarded the code but kept the comment, so `dictobject.c:224-226` states
  "*`PyType_Modified()` must not be called within `LOCK_KEYS()`*" **ten lines above** the call
  that does it. CONFIRMED.
- **CPY-0107** `_Py_dict_lookup:1385` — **UPGRADED from static-confirmed to REPRODUCED**, see §3.1.
- **CPY-0115** `delitem_common:2987` — live `PyDict_Next` cursor vs mutation. CONFIRMED and
  sharpened: `_PyDict_Next` (`:3187-3248`) has **no critical section, no lock assertion and no
  atomics at all**, and its own doc comment (`:3263-3266`) states only the *single-threaded*
  contract ("isn't safe … in a loop that mutates the dict"). Lesson 3: that comment's threat
  model is re-entrant mutation by the same thread; it says nothing about another thread, and
  the public `PyDict_Next` API gives a caller no way to hold the dict lock. Left as CPY-0115.
- **TSAN-0053** `dictiter_iternext_threadsafe:6158-6159` (dict fwd iterator, gh-154130 OPEN).
  CONFIRMED still live. Note the **second, un-catalogued site of the same bug**:
  `dictiter_iternextitem_lock_held:6027-6030` performs the identical
  `di->di_dict = NULL; Py_DECREF(d);`, is reached on the FT build via `try_locked:6162`, and
  its critical section is on the **dict** — which serialises the two threads but does **not**
  make the sequence atomic, because `d = di->di_dict` was latched at `:6185` *before* the
  section. Any fix for gh-154130 must cover `:6028` as well as `:6158`.
- **TSAN-0054** `setiter_iternext:1130-1131` (set iterator, gh-144357 stalled). CONFIRMED
  still live, verbatim.

---

## 3. THE ENUMERATION — every `LOCK_KEYS` region in the slice

`LOCK_KEYS(keys)` = `PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)`.
Two properties make this lock different from `Py_BEGIN_CRITICAL_SECTION`:

1. **non-reentrant** — `PyMutex` has no recursion count;
2. **non-detaching** (`_Py_LOCK_DONT_DETACH`, `pycore_lock.h:37`) — unlike a critical section,
   which `_PyCriticalSection_SuspendAll` silently drops when the thread detaches, this lock is
   *never* released across a nested wait.

Together: **any Python executed under `LOCK_KEYS` that re-enters a keys-locking path hangs
forever.** `dictobject.c:218-227` states the rule; it is violated twice.

Method: interprocedural walk (`scratchpad/lockwalk2.py`) over an intra-file call graph, with
the `do_lookup` **function-pointer** edges (`compare_unicode_generic` / `compare_unicode_unicode`
/ `compare_generic`) supplied by hand — the syntactic call graph cannot see them, and that is
exactly where CPY-0107 lives.

| # | region | function | reaches user Python? | verdict |
|---|---|---|---|---|
| 1 | `:738-794` | `_PyDict_CheckConsistency` | **No.** The only `PyObject_Hash` token at `:751` is inside a comment. Body is `unicode_get_hash` / `dictkeys_get_index` / `CHECK` (abort). | **ACCEPTABLE** |
| 2 | `:1283-1285` | `unicodekeys_lookup_split` | **No.** `unicodekeys_lookup_unicode` → `do_lookup(…, compare_unicode_unicode)`, which is `unicode_eq` — pure C. | **ACCEPTABLE** — *never audited before* |
| 3 | `:1317-1320` | `_PyDictKeys_StringLookupAndVersion` | **No.** Same `compare_unicode_unicode`, plus `_PyDictKeys_GetVersionForCurrentState` → `get_next_dict_keys_version`, which is a pure atomic CAS loop (`:8149-8158`) and takes no lock. | **ACCEPTABLE** — *never audited before* |
| 4 | `:1385-1389` | `_Py_dict_lookup` | **YES** — `unicodekeys_lookup_generic` → `do_lookup(…, compare_unicode_generic)` → `PyObject_RichCompareBool` (`:1168`) **and** `Py_DECREF(startkey)` (`:1169`), both explicitly forbidden by `:221-222`. | **FIX / CPY-0107 — REPRODUCED §3.1** |
| 5 | `:1962-1981` | `insert_split_key` | **YES** — `_PyType_Modified_Unlocked` (`:1971`) → type-watcher callback + `PyErr_FormatUnraisable` → `sys.unraisablehook`. | **FIX / CPY-0096 (recorded)** |
| 6 | `:2230-2261` | `dictresize` | **No.** Body is `get_index_from_order`, `Py_NewRef` (INCREF only — no DECREF), `unicode_get_hash`, `build_indices_*`. Nothing that can run Python and nothing that takes another lock. | **ACCEPTABLE** — *never audited before; #1-ranked function by 2023+ crash-fix commits (12)* |
| 7 | `:7317-7321` | `_PyObject_InitInlineValues` | **No.** Two `_Py_atomic_*` on `dk_usable`. | **ACCEPTABLE** — *never audited before* |

**Result: 7 regions, 2 violations (both already recorded), 5 clean.** The hunt for further
CPY-0096 siblings inside `LOCK_KEYS` **comes up empty** — a real negative on a denominator of 7,
and it means the fix surface for this class in dictobject.c is exactly two call sites.

`setobject.c` has **zero** `LOCK_KEYS`/`PyMutex` regions (no keys object); its only lock is the
per-object critical section. Structural zero, stated as such.

### 3.1 CPY-0107 — REPRODUCED (upgrade from `static-confirmed`)

Repro: `reports/obj-mappings/repro/cpy0107_lock_keys_richcompare_reentry.py`

```
d.pop(Evil())  on a SPLIT instance __dict__, Evil.__hash__ colliding with 'x'
  -> _Py_dict_lookup:1385   LOCK_KEYS_IF_SPLIT(dk, DICT_KEYS_SPLIT)
  -> compare_unicode_generic:1168  PyObject_RichCompareBool  == ARBITRARY PYTHON
  -> Evil.__eq__ does `victim.second_attribute = 2`
  -> insert_split_key:1962  LOCK_KEYS(keys)   same keys object -> HANG
```

| build | result |
|---|---|
| `debug-ft-nojit` (`PYTHON_GIL=0`) | **HUNG 12/12** |
| `release-ft-nojit` (`PYTHON_GIL=0`) | **HUNG 12/12** |
| `debug-gil-nojit` (default) | hung 0/12 |
| `release-gil-nojit` (default) | hung 0/12 |

*(Methodology note, lesson 4: a first sweep reported the GIL builds hanging 12/12. That was my
harness — `PYTHON_GIL=0` makes a GIL build die with `Fatal Python error: config_read_gil`, which
my grep scored as "did not complete". Re-run without the env var: 0/12. Reported here is the
corrected sweep.)*

gdb proof (`handle SIGALRM stop nopass`, `debug-ft-nojit`), abridged — one thread, self-deadlock:

```
#9  _PyMutex_LockTimed (m=0x2000286d223, timeout=-1, flags=_Py_LOCK_DONT_DETACH) lock.c:138
#11 insert_split_key (keys=0x2000286d218, ...)                    dictobject.c:1962
#12 store_instance_attr_lock_held                                 dictobject.c:7455
#24 slot_tp_richcompare                                           typeobject.c:11074
#27 PyObject_RichCompareBool                                      object.c:1135
#28 compare_unicode_generic (dk=<optimized out>, ...)             dictobject.c:1168
#29 do_lookup (check_lookup=0x... <compare_unicode_generic>)      dictobject.c:1124
#30 unicodekeys_lookup_generic (dk=0x2000286d218, ...)            dictobject.c:1188
#31 _Py_dict_lookup                                               dictobject.c:1387
```

`keys = 0x2000286d218` at frame 11 and `dk = 0x2000286d218` at frame 30 are the **same keys
object**; `m = 0x2000286d223` is `&keys->dk_mutex`. The thread is parked on a mutex it holds.

**Guarded twin, and what it actually defends (lesson 3).** The nearest sibling is
`insert_split_key:1955-1959`, which takes `types.mutex` *before* `LOCK_KEYS` with the comment
"*To avoid deadlocks we must always acquire the type lock first*". That guard addresses **lock
ordering between two locks**. It does **not** address running arbitrary Python under a
non-reentrant lock, which is why CPY-0096 survives it. The correct twin is the *rule* at
`:218-227`, which no site enforces mechanically.

**Fix.** Hoist the comparison out of the keys lock, as `_Py_dict_lookup_threadsafe` /
`compare_generic_threadsafe:1564` already does — that path uses `_Py_TryIncrefCompare` and
`DKIX_KEY_CHANGED` restart with **no keys lock held across `PyObject_RichCompareBool`**. It is a
complete, in-file, already-shipped model for the fix.

---

## 4. Every `tp_iternext` in the slice (reported separately, per spec)

| # | iternext | exhaustion drop | lock at the drop | verdict |
|---|---|---|---|---|
| 1 | `dictiter_iternextkey` / `…value` / `…item` → **`dictiter_iternext_threadsafe`** (FT arm) | `:6158-6159` | **none** | TSAN-0053 / gh-154130 — confirmed |
| 2 | …→ **`dictiter_iternextitem_lock_held`** (via `try_locked:6162`, and the GIL arm) | `:6027-6030` | `Py_BEGIN_CRITICAL_SECTION(d)` — **on the dict**, entered *after* `d` was latched at `:6185` | **second site of TSAN-0053, not in the catalog entry** |
| 3 | **`dictreviter_iternext`** (`reversed()` × 3 types) | `:6337-6340` | `Py_BEGIN_CRITICAL_SECTION(d)` — same defect as #2, and **no `#ifdef Py_GIL_DISABLED` variant exists at all** | **FIX — NET-NEW, REPRODUCED §4.1** |
| 4 | `dictiter_iternextkey_lock_held` / `dictiter_iternextvalue_lock_held` | `:5772-5775` / `:5895-5898` | whole function is `#ifndef Py_GIL_DISABLED` (`:5709`/`:5834` … `:5778`/`:5901`) | **ACCEPTABLE** (dead on FT) |
| 5 | **`setiter_iternext`** | `:1130-1131` | `Py_BEGIN_CRITICAL_SECTION(so)` **ends at `:1127`** — the drop is outside it | TSAN-0054 / gh-144357 — confirmed |

### 4.1 [FIX] `reversed(dict)` shared-iterator double-DECREF — NET-NEW

**`Objects/dictobject.c:6337-6340` (`dictreviter_iter_lock_held`), entered from
`dictreviter_iternext:6344`.**

**Mechanism.** `dictreviter_iternext` loads `PyDictObject *d = di->di_dict` at `:6347`,
**before** taking any lock, then enters `Py_BEGIN_CRITICAL_SECTION(d)` at `:6353` — a section
keyed on the **dict**, not on the **iterator**. `dictreviter_iter_lock_held`'s `fail:` label
then executes `di->di_dict = NULL; Py_DECREF(d);`, which drops the iterator's single owning
reference to the dict. N threads calling `next()` on one `reversed(d)` all latch the same
non-NULL `d` at `:6347` and queue on the dict's mutex; the winner NULLs `di_dict` and drops the
reference, and every queued thread then enters with its own stale `d` and drops it again.

The tombstone walk at `:6284-6289` (`while (entry_ptr->me_value == NULL) { if (--i < 0) goto
fail; entry_ptr--; }`) is the natural amplifier: emptying a dict with `del` leaves `dk_nentries`
high while every `me_value` is NULL, so the winner holds the critical section for `dk_nentries`
iterations — long enough for all other threads to have executed `:6347`.

**Why this is a separate fix from TSAN-0053.** Different function, different iterator types
(`dict_reversekeyiterator` `:6368`, `dict_reversevalueiterator` `:6410`,
`dict_reverseitemiterator` `:6422`). More importantly, the forward iterator at least *has* a
`#ifdef Py_GIL_DISABLED` path (`dictiter_iternext_threadsafe`); `dictreviter_iternext` has
**none** — the reverse iterator was skipped entirely by the free-threading hardening pass.

**Guarded twin, and what it defends (lesson 3).** The twin is `Py_BEGIN_CRITICAL_SECTION(d)`
itself, one line above the bug. Its threat model is *another thread mutating the dict during the
table walk* — and against that it works. It is **not** evidence of safety here, because the
racing state is the **iterator's** `di_dict` field, which that lock does not cover and which was
already read before the lock was taken. The correct twin is gh-144357's shape for the set
iterator: `Py_BEGIN_CRITICAL_SECTION2(self, so)` plus dropping the exhaustion `Py_DECREF`
entirely under FT.

**Reproduction** — `repro/dictreviter_shared_iter_double_decref.py` (8 threads, 60k-entry
emptied dict, 60 trials/run):

| build | result |
|---|---|
| `debug-ft-nojit` (`PYTHON_GIL=0`) | **CRASHED 6/6** — `_Py_NegativeRefcount: object has negative ref count`, `object type name: dict`, `object refcount : -1` (and `-2` on a longer run), `Fatal Python error: _PyObject_AssertFailed`, exit **134** |
| `release-ft-nojit` (`PYTHON_GIL=0`) | 0/6 (the refcount assertion is debug-only) |
| `release-ft-nojit-o0` (`PYTHON_GIL=0`) | 0/6 |
| `debug-gil-nojit` (default) | **0/6 — clean control** |

A sole-reference variant (iterator holds the only ref) did **not** produce a release-build SEGV
in 60 trials × 3 builds; FT's QSBR-delayed free plus the missing ASan shadow on the FT object
heap (run-context trap) keeps the freed dict readable. Reported as measured, not extrapolated.

**Prior art:** none. `gh api search/issues` for `reversed dict iterator free-threading race` and
`dictiter_iternext_threadsafe` returns gh-154130 (forward iterator), gh-148873 (closed),
gh-120496 (closed), gh-151627/gh-152537 (OrderedDict) — **nothing on `dictreviter`**.

---

## 5. Findings from reading (net-new)

### 5.1 [FIX] `get_index_from_order` re-reads `mp->ma_values` plainly, discarding the lock-free iterator's atomic snapshot → SIGSEGV

**`Objects/dictobject.c:676` (`get_index_from_order`), reached from
`dictiter_iternext_threadsafe:6100`.**

This is scanner finding #2 (`atomic_plain_asymmetry`, `PyDictObject.ma_values`,
`guarded_twin: set_values:215`) — the one T1 in the batch that is a real memory-safety bug.

**Mechanism.** `dictiter_iternext_threadsafe` runs with **no critical section**. It carefully
snapshots the values array —

```c
PyDictValues *values = _Py_atomic_load_ptr_consume(&d->ma_values);   // :6086
if (values == NULL) goto concurrent_modification;                    // :6087
Py_ssize_t used = _Py_atomic_load_uint8(&values->size);              // :6091
int index = get_index_from_order(d, i);                              // :6100  <-- RE-READS THE FIELD
PyObject *value = _Py_atomic_load_ptr(&values->values[index]);       // :6101  <-- uses the snapshot
```

— and then at `:6100` calls a helper that goes back to the field:

```c
get_index_from_order(mp, i) {
    assert(i < mp->ma_values->size);                                 // :675  plain
    uint8_t *array = get_insertion_order_array(mp->ma_values);       // :676  plain
```

`get_insertion_order_array(values)` is `&values->values[values->capacity]`
(`pycore_dict.h:341`). Any thread that runs `dictresize()` split→combined in the
`:6087`→`:6100` window executes `set_values(mp, NULL)` (`:2264`), so the helper dereferences
NULL.

**Guarded twin, and what it defends.** The twin is **one line below**: `:6101` and `:6103` both
use the `values` snapshot, correctly. The whole function is written in the snapshot discipline;
`get_index_from_order` is the single hole. The field-level twin is `set_values:211-216`
(`ASSERT_OWNED_OR_SHARED` + `_Py_atomic_store_ptr_release`) — which tells you the field is
declared concurrent, so a plain read of it outside a lock is unsynchronised by the file's own
convention.

**Fix.** Pass the snapshot in: `get_index_from_order(PyDictValues *values, Py_ssize_t i)`. Nine
call sites (`:788, :2240, :2253, :3205, :5735, :5858, :5981, :6100, :6276`); the lock-held ones
are unaffected, `:6100` and `:3205` are the ones that need it.

**Reproduction** — `repro/dict_get_index_from_order_stale_ma_values.py`
(7 reader threads iterating a shared **split** instance `__dict__`, 1 thread doing
`d[object()] = 1` to force `dictresize` split→combined; 6000 trials/run):

| build | result |
|---|---|
| `release-ft-nojit-o0` (`PYTHON_GIL=0`) | **SIGSEGV 6/6** (rc 139) |
| `release-ft-nojit` (`PYTHON_GIL=0`) | **SIGSEGV 6/6** |
| `debug-ft-nojit` (`PYTHON_GIL=0`) | **SIGSEGV 6/6** |
| `debug-gil-nojit` (default) | **0/6 — clean control** |
| `release-gil-nojit` (default) | **0/6 — clean control** |

gdb (`release-ft-nojit-o0`), exact predicted frame:

```
Thread 3532 received signal SIGSEGV
#0 get_insertion_order_array (values=0x0)         ./Include/internal/pycore_dict.h:341
#1 get_index_from_order (mp=0x394d667f180, i=0)   Objects/dictobject.c:676
#2 dictiter_iternext_threadsafe (d=..., self=...) Objects/dictobject.c:6100
#3 dictiter_iternextkey (self=...)                Objects/dictobject.c:5791
#4 _PyForIter_VirtualIteratorNext                 Python/ceval.c:3775
```

Trigger is pure Python: `for k in obj.__dict__:` in one thread, `obj.__dict__[object()] = 1`
in another. **Prior art: none** (`gh api search/issues … get_index_from_order` → empty).

### 5.2 [FIX] `dictiter_len` / `setiter_len` dereference the iterator's dict/set with no lock — TSan-confirmed

**`Objects/dictobject.c:5682` (`dictiter_len`)** — scanner finding #10
(`guarded_writer_unguarded_reader`, twin `dictiter_iternextitem_lock_held:5971`) — and its
**fully unguarded set twin `Objects/setobject.c:1062` (`setiter_len`)**.

```c
// dictobject.c:5678
if (di->di_dict != NULL && di->di_used == GET_USED(di->di_dict))   // :5682  ALL PLAIN
    len = FT_ATOMIC_LOAD_SSIZE_RELAXED(di->len);                   // :5683  ATOMIC
```

The mixed discipline **inside a two-line function** is the tell: `di->len` is loaded atomically,
`di->di_dict` is read plainly and then **dereferenced**, with no critical section anywhere in
the function. All three exhaustion paths (`:6028`, `:6158`, `:6338`) write
`di->di_dict = NULL; Py_DECREF(d);`. `it.__length_hint__()` is a public Python-reachable method.

`setiter_len:1060-1063` is worse: `si->si_set`, `si->si_used`, `si->si_set->used` and `si->len`
are **all** plain, while `setiter_iternext:1107-1108` reads `so->used` and `si->si_used` with
`FT_ATOMIC_LOAD_SSIZE_RELAXED` — the guarded twin is 45 lines away in the same file.

**Reproduction status.** Not crash-reproduced: `repro/dictiter_len_unlocked_di_dict.py`
survived 3000 trials on `debug-ft-nojit`, `release-ft-nojit-o0` and `debug-gil-nojit`
(FT's QSBR-delayed free keeps the stale dict readable). **TSan does confirm the exact pair** on
`release-ft-nojit-tsan` (`PYTHON_GIL=0`, upstream suppressions applied), 2 reports, 150 trials:

```
WARNING: ThreadSanitizer: data race
  #0 dictiter_len                    Objects/dictobject.c:5682:13
  #0 dictiter_iternext_threadsafe    Objects/dictobject.c:6158:17
  #1 dictiter_iternextkey            Objects/dictobject.c:5791:9
SUMMARY: data race … dictobject.c:6158:17 in dictiter_iternext_threadsafe
```

That is precisely the predicted read/write pair. Classification **FIX** on the strength of the
TSan pair plus the dereference; severity is bounded to "read of a possibly-freed dict" until a
crash face is found.

### 5.3 [FIX] Two plain `ma_values` stores to a **live, published** dict, bypassing `set_values()`

I enumerated all 11 plain `->ma_keys =` / `->ma_values =` assignments in `dictobject.c`:

| line | function | verdict |
|---|---|---|
| `:283`, `:289` | GIL arm of `set_keys`/`set_values` | ACCEPTABLE (not compiled on FT) |
| `:955`, `:956` | `new_dict` | ACCEPTABLE (pre-publication) |
| `:4518`, `:4519` | `copy_lock_held_untracked` (`split_copy`) | ACCEPTABLE (pre-publication, untracked) |
| `:5371`, `:5372` | `anydict_new_untracked` | ACCEPTABLE (pre-publication) |
| **`:7976`** | `detach_dict_from_object` — `mp->ma_values = values;` | **FIX** |
| **`:8019`** | `PyObject_ClearManagedDict` — `dict->ma_values = NULL;` | **FIX** (scanner finding #18) |

**`:8019` is the cleanest guarded twin in the slice** — the line immediately above it is
`set_keys(dict, Py_EMPTY_KEYS);` (`:8018`). The same author used the atomic helper for `ma_keys`
and the raw field for `ma_values`, one line apart.

**`:7976` is the more dangerous of the two.** It publishes a **freshly filled** values array
(`copy_values` at `:7970`) into a live dict with a plain store — no release barrier. The
lock-free reader pairs `_Py_atomic_load_ptr_consume(&d->ma_values)` (`:6086`) /
`_Py_atomic_load_ptr(&mp->ma_values)` (`:1626`) against `set_values`'s
`_Py_atomic_store_ptr_release` (`:215`). A plain store breaks that pairing, so on a weakly
ordered target a reader can observe the new pointer before the `copy_values` writes are visible.
`ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED(mp)` at `:7965` does **not** save it: the obj lock excludes
other writers, not the lock-free readers, which take nothing.

**Fix:** `set_values(mp, values)` / `set_values(dict, NULL)`. Two one-line changes; the helper
already exists and is used at `:2264` and `:3155`. Not reproduced (weak-memory / narrow window);
static, and mechanical.

---

## 6. Classes bounded — checked here, clean, with denominators

- **`LOCK_KEYS` held across user code — 7/7 regions.** 2 known violations, 5 clean. No un-found
  siblings. (§3)
- **`#ifdef Py_GIL_DISABLED` arm divergence (the CPY-0099 shape) — 24/24 two-armed splits
  diffed** (dict 20, set 4). **Clean.** Every split is "atomic/threadsafe variant vs plain
  variant"; not one has a status check or assertion live in one arm and dead in the other. The
  nearest thing is `:2451/:2537/:2669`, where the FT arm does
  `_Py_dict_lookup_threadsafe(...); Py_XDECREF(value); return value; // borrowed reference` —
  that is the documented `PyDict_GetItem` borrowed-reference contract, **POLICY**, not a
  divergence defect.
- **`memcpy`/`memset` vs the setobject gh-143100 fix — 13/13 sites** (dict `:843, :844, :1042,
  :2281, :2298, :4465`; set `:205, :522, :674, :1541, :1543, :1544`). **Clean in dictobject.c.**
  The gh-143100 bug was a plain **write** into a *live* table that lock-free readers load
  atomically. All six dict sites write into **freshly allocated, not-yet-published** memory
  (`new_keys_object`, `clone_combined_dict_keys`, `dictresize`'s `newkeys`, `copy_values`'s
  `newvalues`) and read from memory only CS-holders write. The un-propagated-fix hypothesis from
  the history agent is **disproven for dictobject.c** — the two `dictresize` memcpys at `:2281`
  and `:2298` target `newentries`, a private allocation. The residual plain
  `memcpy(tab, a->smalltable, …)` at `setobject.c:1541` is a read into a stack local after
  `a->table` was already NULLed at `:1521`, so lock-free readers bail — deliberately left by
  `e8e044eda34`. ACCEPTABLE.
- **`so->fill` — 21/21 accesses.** `fill` is never read by a lock-free path (`set_len` reads
  `used`; `setiter_iternext` reads `used`/`si_used`; `set_lookkey_threadsafe` reads
  `table`/`mask`/`key`/`hash`). It is a lock-only field, so the plain accesses at `:320`,
  `:1524` etc. are correct despite sitting beside `FT_ATOMIC` neighbours. **ACCEPTABLE** — this
  is the answer to the history agent's `set_pop_impl` item as well: `set.pop` is
  clinic-guarded (`Objects/clinic/setobject.c.h:26 Py_BEGIN_CRITICAL_SECTION(so)`), so its plain
  reads of `so->table/finger/mask/used` are under the lock and race no one.
- **`_PySet_AddTakeRef` (`setobject.c:362`)** — the one set entry point that calls
  `set_add_entry_takeref` **without** a critical section and without asserting one; reached from
  the eval loop (`Python/bytecodes.c:1383` SET_ADD, `:2565` BUILD_SET). In both opcodes the set
  is one the interpreter just built and holds uniquely, so it is not shared —
  **ACCEPTABLE, but it is the file's only unguarded mutator and worth a comment upstream.**
- **`set_add_entry_takeref:290-295` revalidation ordering** — confirmed as already found by the
  history agent (lost update, not UAF). I checked the **concurrent** variant it asked about: it
  is the same lost update reached by a concurrent `set_table_resize`/`set_clear` instead of a
  re-entrant `__eq__` (`cmp > 0 → found_active` at `:328` does not dereference `entry`, so still
  no memory-unsafety). **Not a new finding** — same defect, second trigger.
- **`set_intersection:1780` `publish_before_init_complete` (scanner finding #19)** — **FALSE
  POSITIVE.** The rule is not branch-aware: `_PyObject_GC_TRACK(result)` at `:1762` is inside
  the `if (PyAnySet_Check(other))` arm and is immediately followed by `return` (`:1763`); the
  `set_add_entry(result, …)` at `:1780` is in the mutually exclusive iterator arm, whose own
  `_PyObject_GC_TRACK(result)` is at `:1794`, **after** every insert. Both arms are correctly
  ordered.

### Scanner triage, all 20

| # | site | verdict | why |
|---|---|---|---|
| 0,1 | `dictkeys_decref:508/516` `dk_nentries` | ACCEPTABLE | reached only when `DECREF_KEYS(dk)==1` — this thread owns the keys exclusively |
| **2** | `get_index_from_order:676` `ma_values` | **FIX** | §5.1 — reproduced SIGSEGV |
| 3,4 | `insert_to_emptydict:2122/2123` `dk_usable`/`dk_nentries` | ACCEPTABLE | `newkeys` is pre-publication; the code comment at `:2124-2128` says exactly that |
| 5,6 | `delete_index_from_values:2951`, `clear_embedded_values:3117` `values->size` | CONSIDER | plain write vs `_Py_atomic_load_uint8` at `:6091`; TSan-reportable, but bounded by `capacity` and `:6096-6099` documents acceptance of the sibling order-array race |
| 7 | `_PyDict_Next:3206` `ma_values` | CONSIDER | folds into CPY-0115; same helper defect as §5.1 but with no lock at all |
| 8 | `_PyDict_Next:3236` `me_hash` | CONSIDER | ditto (CPY-0115) |
| 9 | `copy_values:4462` `newvalues->size` | ACCEPTABLE | fresh allocation, pre-publication |
| **10** | `dictiter_len:5682` `di_used`/`di_dict` | **FIX** | §5.2 — TSan-confirmed |
| 11,12 | `dictiter_iternextitem_lock_held:6017/6018` `di_pos`/`len` | CONSIDER | plain writes under the **dict** CS vs atomic reads at `:5975` (same function!) and `:5683` (no lock) — the iterator's own fields are not covered by the dict's lock |
| **13,14** | `dictreviter_iter_lock_held:6305/6306` `di_pos`/`len` | **FIX** | subsumed by §4.1 |
| 15 | `_PyObject_IsInstanceDictEmpty:7725` `dk_nentries` | CONSIDER | plain read of a shared split-keys field grown atomically by `split_keys_entry_added:248`, in a loop whose two neighbours (`:7723`, `:7726`) *are* atomic; `LOAD_KEYS_NENTRIES` (`:237`) is the ready-made fix. No OOB (`dk_nentries + dk_usable` is invariant, so `i < capacity`) |
| 16,17 | `PyObject_VisitManagedDict:7763/7765` `values->valid`/`values` | ACCEPTABLE | `tp_traverse`; the FT GC runs world-stopped and `gc.get_referents`/`get_referrers` bracket with `_PyEval_StopTheWorld` (`Modules/gcmodule.c:313`) |
| **18** | `PyObject_ClearManagedDict:8019` `ma_values` | **FIX** | §5.3 |
| 19 | `set_intersection:1780` publish-before-init | ACCEPTABLE | FP, branch-unaware rule (above) |

---

## 7. Noticed outside slice

- `Objects/odictobject.c` — `_odict_FAST_SIZE`/`odictiter` share dict's iterator shape;
  gh-151627/gh-152537 already cover `odictiter_new`. Another slice.
- `Modules/gcmodule.c:313` — `gc.get_referents` correctly stop-the-worlds; that is what makes
  `PyObject_VisitManagedDict`'s plain reads acceptable. Worth a one-line comment there.
- `Include/internal/pycore_dict.h:341` `get_insertion_order_array` has no NULL contract; §5.1's
  fix could add one.

---

## 8. Toolkit feedback

### 8.1 Recall gaps — the highest-value output

1. **The call graph does not follow C function pointers, and that is where the flagship bug
   lives.** `_Py_dict_lookup` → `unicodekeys_lookup_generic` → `do_lookup(mp, dk, key, hash,
   compare_unicode_generic)` — the risky callee is passed as the 5th argument. My first
   interprocedural pass produced **zero** hits for CPY-0107 and only found it after I hand-added
   three edges. `Objects/` is full of this idiom (`do_lookup`, `set_do_lookup`,
   `compare_func`). **Proposal:** in `scan_ft_races.py` (and any call-graph consumer), when a
   call argument is a bare identifier that names a same-file function, add a call edge from the
   *callee* to it. That one rule recovers `do_lookup`→`compare_*` and `set_do_lookup`→
   `set_compare_*` mechanically.

2. **There is no rule for "lock held across a call that can run Python".** This is the slice's
   single highest-value class (CPY-0096, CPY-0107) and the scanner has no ft_class for it. The
   three ingredients are all mechanical: (a) a `LOCK_KEYS`/`PyMutex_LockFlags` region, (b) a
   transitive callee set, (c) a small table of Python-invoking APIs. **Proposal:** new
   `ft_class` **T5 `lock_held_across_python`**, with severity keyed on the lock flavour —
   `_Py_LOCK_DONT_DETACH`/raw `PyMutex` ⇒ **high** (hang: non-reentrant *and* non-detaching),
   `Py_BEGIN_CRITICAL_SECTION` ⇒ **medium** (the section is silently dropped on detach, so
   invariants break but no deadlock). That severity split is the whole triage and it is a
   one-line lookup on the flags argument.

3. **A `*_lock_held` callee is suppressed, but the lock may be on the wrong object.** The
   envelope reports 33 suppressed `lock_held_functions`. `dictiter_iternextitem_lock_held` and
   `dictreviter_iter_lock_held` are both suppressed by that convention — and both contain a
   real double-DECREF, because the lock they run under is the **dict's**, while the field they
   mutate belongs to the **iterator**. **Proposal:** do not suppress a `*_lock_held` function
   whose body writes `self->…`/`it->…` on a struct that is *not* the locked object. Cheap
   approximation: if the function takes ≥2 object parameters and the assertion names one of
   them (`ASSERT_DICT_LOCKED(d)`) while the writes target the other (`di->…`), keep the finding.
   This is exactly the gap that let both TSAN-0053's second site and the whole of §4.1 through.

4. **`iternext_functions: 8` over-counts and hides the real gap.** The scanner counted 8, but
   there are 5 distinct `tp_iternext` implementations in the slice; the `_lock_held` delegates
   are counted separately. Worse, the count gave no signal that `dictreviter_iternext` — three
   public iterator types — has **no FT arm at all**. **Proposal:** report
   `iternext_functions_without_ft_arm` alongside the raw count. A `tp_iternext` in a file that
   *has* `#ifdef Py_GIL_DISABLED` regions but whose own body has none is a high-signal
   one-liner, and it would have flagged §4.1 directly.

5. **The T4 `publish_before_init_complete` rule is not branch-aware** — finding #19 paired a
   `_PyObject_GC_TRACK` in one `if` arm with a write in the mutually exclusive `else` arm.
   **Proposal:** require the TRACK site to dominate the write in the CFG, or at minimum reject
   the pair when a `return` lies between them at the same brace depth. Precision on this rule
   in the slice was 0/1.

### 8.2 Precision per rule (this slice)

| rule | fired | promoted | precision | note |
|---|---|---|---|---|
| `atomic_plain_asymmetry` | 17 | 4 FIX + 6 CONSIDER | 24 % FIX / 59 % actionable | the low-confidence rule carried the slice's best static lead (#2 → a reproduced SIGSEGV) — **do not down-weight T1 here** |
| `guarded_writer_unguarded_reader` | 2 | 1 FIX (#10), 1 CONSIDER (#8) | 50 % | best precision of the three |
| `publish_before_init_complete` | 1 | 0 | 0 % | branch-unaware FP |

### 8.3 What worked

The `guarded_twin` field earned its keep three times: `set_values:215` pointed straight at §5.1
and §5.3, and `dictiter_iternextitem_lock_held:5971` at §5.2. Keep emitting it even on `low`
confidence — for `atomic_plain_asymmetry` the twin *is* the finding.
