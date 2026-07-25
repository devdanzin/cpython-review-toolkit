# tsan-stress-generator — obj-mappings slice

Target ref `4f3be1b5777`; build matrix at `a1d580430c8`.
**`Objects/dictobject.c` and `Objects/setobject.c` are byte-identical between the two**
(`diff -q` on the ASan build tree vs. the target checkout, and `git diff a1d580430c8 4f3be1b5777`
on both files is empty). Every line number below is valid on both.

Scripts: `reports/obj-mappings/repro/`. Raw sanitizer output: `repro/tsan/`, `repro/asan/`.

---

## 1. Denominator

| quantity | value |
|---|---|
| stress / repro scripts written | 8 (`CPY0096_gil_reentry.py`, `CPY0107_lookup_keys_lock_reentry.py`, `gh154130_iter_sole_owner.py`, `tsan_stress_dictset_iter.py`, `tsan_stress_iter_methods.py`, `tsan_stress_dictset_resize.py`, `tsan_stress_reentrant_hash_eq.py`, `tsan_stress_split_combined.py`) + 2 instrumentation probes |
| distinct scenario definitions | 45 |
| interpreter processes launched | ≈290 |
| builds exercised | 7 — `debug-ft-nojit`, `release-ft-nojit`, `release-ft-nojit-tsan`, `debug-gil-nojit`, `release-gil-nojit`, `debug-gil-nojit-asan`, `release-gil-nojit-asan` |
| `scan_ft_races` slice population consulted | 20 findings / 404 functions / **8 `iternext_functions`** / 33 `lock_held_functions` (`scanners/scan_ft_races.sample.json`) |
| `iternext_double_decref` findings in that population | **0** — and that zero is *wrong*; see §5 |
| TSan runs completed | 4 scripts, 24 scenarios, on `release-ft-nojit-tsan` |
| TSan warnings collected | 27 (`tsan_stress_dictset_iter.py`) + 73 (`tsan_stress_iter_methods.py`) + **0** (`tsan_stress_dictset_resize.py`) + **0** (`tsan_stress_reentrant_hash_eq.py`) + **0** (`tsan_stress_split_combined.py`) |
| unique races per `parse_tsan_report.py` | 6 (0 noise) + 14 (0 noise); every iterator-field pair lands in the two slice files |
| net-new TSan race signatures vs. `cpython-tsan-findings` | 4 (recorded as TSAN-0062) |

Everything below that says "reproduced" was run; every claim carries N/M and a build name.
Section §4 lists what did **not** reproduce, with attempt counts.

---

## 2. Findings

### F1 — CPY-0096's three GIL-build consequences: **all three reproduced**

`Objects/dictobject.c:1943 insert_split_key` — **FIX**

```c
LOCK_KEYS(keys);                                              /* :1962 */
ix = unicodekeys_lookup_unicode(keys, key, hash);             /* :1963 */
if (ix == DKIX_EMPTY && keys->dk_usable > 0) {                /* :1964  CHECK  */
    ...
    _PyType_Modified_Unlocked(type);                          /* :1971  RUNS PYTHON */
    Py_ssize_t hashpos = find_empty_slot(keys, hash);         /* :1973 */
    ix = keys->dk_nentries;                                   /* :1974  USE    */
    dictkeys_set_index(keys, hashpos, ix);                    /* :1975 */
    PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(keys)[ix];   /* :1976  WRITE  */
    STORE_SHARED_KEY(ep->me_key, Py_NewRef(key));             /* :1977 */
    split_keys_entry_added(keys);                             /* :1978  usable--, nentries++ */
}
assert (ix < SHARED_KEYS_MAX_SIZE);                           /* :1980 */
```

The `dk_usable > 0` test at :1964 is **not re-evaluated** after the re-entrancy window at
:1971. Every nested level tests the same pre-re-entry value and then consumes a slot on the
way out, so N nested re-entries starting from `dk_usable == 1` leave `dk_usable == 1 - (N+1)`
and `dk_nentries` past `SHARED_KEYS_MAX_SIZE` (30). Under the GIL `LOCK_KEYS` is a no-op, so
there is no deadlock — only the stale-check corruption.

Reproducer `repro/CPY0096_gil_reentry.py <mode> [depth]`. Setup: a fresh class, one instance
(`_PyObject_InitInlineValues`, dictobject.c:7324, takes `dk_usable` 30 → 29), then 28
attributes → `dk_nentries == 28, dk_usable == 1`. `_testcapi.add_type_watcher(1)` arms
`type_modified_callback_error`, whose `-1` forces `typeobject.c:1223 PyErr_FormatUnraisable`
→ `sys.unraisablehook` → user Python.

| consequence | build | mode | result |
|---|---|---|---|
| **`dk_usable` invariant break** | `debug-gil-nojit` | `usable` depth 1 | **5/5** survive, `dk_nentries` measured **30** against a 29-entry grant ⇒ `dk_usable == -1` |
| **`assert(ix < SHARED_KEYS_MAX_SIZE)`** | `debug-gil-nojit` | `usable` depth 2 | **5/5 SIGABRT** — `Objects/dictobject.c:1980: Assertion 'ix < SHARED_KEYS_MAX_SIZE' failed` |
| " | `debug-gil-nojit` | `usable` depth 3 | **5/5 SIGABRT**, same assert |
| **hard SIGSEGV** | `release-gil-nojit` | `usable` depth 1 | **5/5 SIGSEGV** (exit 139) |
| " | `release-gil-nojit` | `segv` depth 1 | **5/5 SIGSEGV** |
| **ASan heap-buffer-overflow** | `release-gil-nojit-asan` | `overflow` depth 8 | **5/5 heap-buffer-overflow READ of size 8**, see below |
| " | `debug-gil-nojit-asan` | `overflow` depth 8 | **5/5 SIGABRT** — the `:1980` assert fires before the overflow |
| " | both ASan builds | `usable` depth 1 | 0/10 — survives cleanly under ASan where the plain release build SIGSEGVs 5/5. The single-re-entry read lands *inside* the allocation (the uninitialised insertion-order array), so ASan does not flag it and whatever ASan's allocator leaves there is benign to `Py_DECREF`. Use `overflow`, not `usable`, on an ASan build |
| **unbounded recursion** | `release-gil-nojit` | `unbounded` | **3/3 SIGSEGV** |
| " | `debug-gil-nojit` | `unbounded` | **3/3 SIGABRT** |
| FT control (CPY-0096 proper) | `debug-ft-nojit` | `usable` depth 1 | **4/4 HANG** (25 s timeout) |
| " | `release-ft-nojit` | `usable` depth 1 | **4/4 HANG** |

`dk_usable` is measured, not assumed: `_testinternalcapi.get_object_dict_values(obj)` returns
a tuple of length `ht_cached_keys->dk_nentries` (`Modules/_testinternalcapi.c:2106`), and one
instance + 28 attributes can only ever be granted 29 entries.

**The ASan report** (`repro/asan/overflow_asan.txt`), on the **GIL** ASan build as the brief
requires:

```
ERROR: AddressSanitizer: heap-buffer-overflow ... READ of size 8
    #0 store_instance_attr_lock_held Objects/dictobject.c:7497:27
    #1 _PyObject_GenericSetAttrWithDict Objects/object.c:2058:19
0x71d23f412700 is located 0 bytes after 320-byte region [0x71d23f4125c0,0x71d23f412700)
allocated by thread T0 here:
    #2 _PyType_AllocNoTrack Objects/typeobject.c:2528:19
    #3 PyType_GenericAlloc  Objects/typeobject.c:2559:21
    #4 object_new           Objects/typeobject.c:7468:21
```

`dictobject.c:7497` is `PyObject *old_value = values->values[ix];` — `ix` came back from
`insert_split_key` already past the instance's inline-values capacity, so the read runs off
the end of the object allocation (`tp_basicsize + _PyInlineValuesSize`). The very next thing
done with that value is `Py_DECREF(old_value)` at :7530, which is the release-build SIGSEGV:

```
#0 _Py_IsImmortal Include/refcount.h:132:12
#1 Py_DECREF      Include/refcount.h:421:9
#2 store_instance_attr_lock_held Objects/dictobject.c:7530:9
   ... rdi = 0x00000c24bbf3e92d      (r13 = 0x0706050403020100 — the insertion-order array)
```

The garbage it dereferences is not random: `_PyObject_InitInlineValues` (dictobject.c:7335-7337)
NULLs only `values[0 .. capacity-1]`; the insertion-order array that follows at
`&values->values[capacity]` is **left uninitialized**, and it is exactly what the out-of-capacity
slot reads. So the failure is "read uninitialized heap as a `PyObject *`, then DECREF it".

Two arithmetic bounds worth recording, both from `_PyDict_NewKeysForClass` (dictobject.c:7259):
the entry array is sized `USABLE_FRACTION(1<<6) == 42` while `init_keys_object` sets
`dk_usable = SHARED_KEYS_MAX_SIZE == 30`, so `DK_UNICODE_ENTRIES(keys)[ix]` at :1976 needs
`ix >= 42` to leave the *keys* allocation; the *instance* inline-values array (capacity ≤ 30)
is breached first, at `ix >= 34`. That ordering is why the observed ASan report is in
`store_instance_attr_lock_held` and not in `insert_split_key`.

**Reachability.** All of this needs a registered type watcher (`PyType_AddWatcher`), reached
from Python only through `_testcapi`. It is C-API reachable — any extension that installs a
type watcher (profilers, JIT caches, Cython's type-version machinery) plus any failing watcher
callback gets there. Not pure-Python reachable.

**Guarded twin.** `dictobject.c:218-227` is the fix, written down and not applied:

> *"it will be important that `LOCK_KEYS()` is essentially the 'inner-most' code and that we
> don't call `Py_DECREF()` or similar while holding the keys lock. We are not allowed to
> acquire other locks within `LOCK_KEYS()`. For example, `PyType_Modified()` must not be
> called within `LOCK_KEYS()` since it acquires the type lock."*

That comment addresses **lock ordering**. It does *not* address the stale-check: even with
`LOCK_KEYS` removed entirely (i.e. the GIL build) the `dk_usable > 0` test at :1964 is still
read before user Python runs and used after. Lesson 3 applied: the comment's threat model and
this bug's threat model are different, and the GIL-build results prove it.

---

### F2 — CPY-0107 reproduced (independent corroboration + gdb proof)

`Objects/dictobject.c:1385 _Py_dict_lookup` / `:1168 compare_unicode_generic` — **FIX**

*Attribution:* another agent in this same slice run had already moved CPY-0107 from
`static-confirmed` to `reproduced` and reached the same mechanism (single-threaded
self-deadlock, store path, not a two-thread lock-order inversion). I reproduced it
independently before reading that update; what follows is corroboration, a gdb backtrace
proving the two frames hold the *same keys pointer*, a wider run matrix, and a narrower
trigger. Recorded as an appended note on the existing record, not as a new finding.

The recorded finding says `compare_unicode_generic` runs `PyObject_RichCompareBool` under
`LOCK_KEYS`. It does, and the consequence is a **permanent single-threaded hang on any
free-threaded build, from pure Python, with no `_testcapi`**.

`repro/CPY0107_lookup_keys_lock_reentry.py`:

```python
class C: pass
owner = C(); owner.a = 1;  d = owner.__dict__      # SPLIT table
other = C(); other.a = 1                           # same shared keys, different object lock

class SubStr(str):                                  # NOT exact-unicode
    def __hash__(self): return str.__hash__(self)
    def __eq__(self, k):
        other.brand_new_attribute = 2               # -> insert_split_key -> LOCK_KEYS(same keys)
        return str.__eq__(self, k)

d[SubStr("a")] = 99                                 # insertdict -> _Py_dict_lookup:1385
```

| build | runs | result |
|---|---|---|
| `debug-ft-nojit` | 12 | **12/12 HANG** (SIGKILL at 12–20 s) |
| `release-ft-nojit` | 6 | **6/6 HANG** |
| `debug-gil-nojit` | 6 | **0/6** — clean exit 0 |
| `release-gil-nojit` | 6 | **0/6** — clean exit 0 |

gdb proof of the self-deadlock (same keys pointer in both frames):

```
#11 insert_split_key (keys=0x2000286d218, ...)      Objects/dictobject.c:1962
#10 PyMutex_LockFlags (m=0x2000286d223, flags=_Py_LOCK_DONT_DETACH)
 #9 _PyMutex_LockTimed  Python/lock.c:138
 #8 _PyParkingLot_Park  Python/parking_lot.c:340        <-- parked forever
...
#28 compare_unicode_generic (mp=0x2000266e470, ep0=0x2000286d278, ix=0)  dictobject.c:1168
#29 do_lookup            (dk=0x2000286d218, ...)                        dictobject.c:1124
#30 unicodekeys_lookup_generic (dk=0x2000286d218, ...)                  dictobject.c:1188
#31 _Py_dict_lookup      (mp=0x2000266e470, ...)                        dictobject.c:1387
#32 insertdict                                                          dictobject.c:2038
```

`0x2000286d223 == 0x2000286d218 + 0x0b == &keys->dk_mutex`, and `dk = 0x2000286d218` is the
keys object `LOCK_KEYS_IF_SPLIT` locked at :1385. One thread, one mutex, no contention needed.

**Entry point matters and is worth recording.** `d.get(SubStr("a"))` does **not** hang (0/2 on
`debug-ft-nojit`): reads go through `_Py_dict_lookup_threadsafe` (dictobject.c:1601), which
uses `unicodekeys_lookup_generic_threadsafe` and takes **no** keys lock. Only the write paths
reach `_Py_dict_lookup` proper — `insertdict` (dictobject.c:2036, the
`!PyUnicode_CheckExact(key)` fall-through) and `delitem_common`. Any triage that tests the
read path will wrongly conclude the site is unreachable.

**Guarded twin.** Same as F1 — `dictobject.c:218-227`. Here the comment is *exactly* on
point (it forbids taking another lock under `LOCK_KEYS`), which makes this the cleaner
violation of the two: `PyObject_RichCompareBool` can take *any* lock, including this one.

---

### F3 — Shared-iterator exhaustion double-DECREF: **confirmed live**, plus 3 net-new sites

`Objects/dictobject.c:6157` (`fail:` in `dictiter_iternext_threadsafe`) and
`Objects/setobject.c:1129` (`setiter_iternext`) — **FIX**

```c
/* dictobject.c */
fail:
    di->di_dict = NULL;      /* :6158 */
    Py_DECREF(d);            /* :6159 */

/* setobject.c */
    si->si_pos = i+1;        /* :1128  -- and the critical section ended at :1127 */
    if (key == NULL) {
        si->si_set = NULL;   /* :1130 */
        Py_DECREF(so);       /* :1131 */
```

`d` / `so` is a borrowed read of the field the drop clears (`dictiter_iternextkey:5784`,
`setiter_iternext:1101`) behind nothing but a `== NULL` test, and the drop is not conditional
on having *won* the NULL store. Two threads that both observe non-NULL both drop the same
single reference.

This is **already recorded**: `TSAN-0053` (dict, filed as python/cpython#154130) and
`TSAN-0054` (set, corroborating python/cpython#144356 / PR #144357). Confirming in one line,
per informed-mode rule 1 — **both still reproduce at the reviewed ref**.

`repro/gh154130_iter_sole_owner.py` — one shared iterator over a container whose **only**
reference is the iterator's, so the second DECREF reaches zero instead of merely perturbing a
refcount. 8 threads, 8000 barrier-synchronised rounds, 5 runs per cell:

| build | dict | dict1 | values | items | set | set1 | total |
|---|---|---|---|---|---|---|---|
| `debug-ft-nojit` | 2/5 | 4/5 | 1/5 | 2/5 | 2/5 | 2/5 | **13/30 SIGABRT** |
| `release-ft-nojit` | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/30 |
| `debug-gil-nojit` | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/30 |
| `release-gil-nojit` | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 | 0/30 |

```
Python/gc_free_threading.c:1083: validate_refcounts: Assertion "_Py_REFCNT(op) > 0" failed
object refcount : -1
object type name: dict        (and, separately, set)
```

The release-FT zero is *absence of a detector*, not absence of the bug — release builds have
no `validate_refcounts`; the same over-DECREF there is a silent UAF.

**TSan** (`repro/tsan_stress_dictset_iter.py` on `release-ft-nojit-tsan`, 4 threads × 300
rounds × 10 scenarios): **all 10 scenarios exit 66**, 27 warnings, **6 unique races, 6 in
CPython source, 0 noise** (`parse_tsan_report.py`; raw at `repro/tsan/iter_tsan.txt`):

| # | signature (unordered `file:func` pair) | catalogued? |
|---|---|---|
| 1 | `dictobject.c:dictiter_iternextkey` ↔ `dictobject.c:dictiter_iternext_threadsafe` | TSAN-0026 / TSAN-0053 |
| 2 | `dictobject.c:dictiter_iternext_threadsafe` ↔ `dictobject.c:dictiter_iternext_threadsafe` | TSAN-0026 |
| 3 | `dictobject.c:dictiter_iternextvalue` ↔ `dictobject.c:dictiter_iternext_threadsafe` | **NET NEW** |
| 4 | `dictobject.c:dictiter_iternextitem` ↔ `dictobject.c:dictiter_iternext_threadsafe` | **NET NEW** |
| 5 | `dictobject.c:dictreviter_iternext` ↔ `dictobject.c:dictreviter_iter_lock_held` | **NET NEW** |
| 6 | `setobject.c:setiter_iternext` ↔ `setobject.c:setiter_iternext` | TSAN-0040 / TSAN-0054 |

(Checked against `cpython-tsan-findings/catalog/known_races.tsv`: `dictiter_iternextvalue`,
`dictreviter_iternext`, `dictreviter_iter_lock_held` appear **zero** times; the two
`dictiter_iternextitem` hits are both `dictiter_iternextitem_lock_held`, a different function.)

Why 3 and 4 matter beyond bookkeeping: gh-154130's reproducer and TSAN-0053's `sites` name
only `dictiter_iternextkey:5791`. `dict.values()` and `dict.items()` iterators reach the
identical `fail:` label through `dictiter_iternextvalue:5907` and `dictiter_iternextitem:6185`.
A fix scoped to `dictiter_iternextkey` would be incomplete for two of the three dict iterators.

Finding 5 is the one I would file separately. `dictreviter_iternext` (dictobject.c:6342):

```c
    dictiterobject *di = (dictiterobject *)self;
    PyDictObject *d = di->di_dict;          /* :6347  borrowed read, OUTSIDE the CS */
    if (d == NULL)
        return NULL;
    Py_BEGIN_CRITICAL_SECTION(d);           /* :6352 */
    value = dictreviter_iter_lock_held(d, self);
    Py_END_CRITICAL_SECTION();
```

The critical section is on `d`, taken *after* `d` was captured, and `dictreviter_iter_lock_held`'s
`fail:` (:6338-6339) does the same unconditional `di->di_dict = NULL; Py_DECREF(d);` inside it.
Two threads capture the same `d` at :6347, serialise on the CS, and *both* run `fail:`. The
lock makes the drop look protected while protecting nothing that matters here — the missing
guard is on `di`, not on `d`. This is lesson 3 in C: a real lock, for the wrong threat model.

**Recorded** as `TSAN-0062` in `cpython-tsan-findings`
(`reports/TSAN-0062-dict-reversed-iter-double-decref/`, four signatures — the fourth is F4's
`dictiter_reduce` pair, added later in the session; `catalog/known_races.tsv` regenerated to
225 signatures / 59 races, `INDEX.md` row added).
Status `confirmed`, `upstream_issue: null` — my read is that the right move is a comment on
the **open** python/cpython#154130 rather than a new issue, since it is the same bug reached
through more doors; the `reversed()` decoy critical section probably deserves its own
paragraph there. Not posted anywhere.

---

### F4 — Iterator-method reads of the field the exhaustion path frees

`Objects/dictobject.c:5682 dictiter_len`, `:6392 dictiter_reduce`,
`Objects/setobject.c:1062 setiter_len`, `:1071 setiter_reduce` — **CONSIDER**
(FIX if the F3 drop is fixed by anything other than "stop dropping in iternext")

```c
/* dictobject.c:5682 */
if (di->di_dict != NULL && di->di_used == GET_USED(di->di_dict))
    len = FT_ATOMIC_LOAD_SSIZE_RELAXED(di->len);      /* :5683 */
```

The NULL check and the dereference are two separate plain loads of a field another thread
NULLs *and frees* between them, so `GET_USED(di->di_dict)` can read freed memory.
`__length_hint__` is on the hot path of `list(it)` / `tuple(it)` / `set(it)`.
`dictiter_reduce:6392-6393` is worse in kind: `dictiterobject tmp = *di; Py_XINCREF(tmp.di_dict);`
— a struct copy plus an unconditional INCREF of a pointer another thread may already have
taken to zero. `setiter_len:1062` / `setiter_reduce:1071` are literal twins.

**Guarded twin, and it is in the same statement:** `dictobject.c:5683` loads `di->len` with
`FT_ATOMIC_LOAD_SSIZE_RELAXED` on the line right after the plain, unguarded load of
`di->di_dict` on :5682. The *counter* was given atomics; the *pointer* guarding it was not.
Same shape as CPY-0061 (`dequeiter_len`, reproduced).

`scan_ft_races` **did** fire on `dictiter_len:5682` — but on the wrong member, with a verdict
that would suppress escalation. Its finding is on `dictiterobject.di_used`
(guarded twin `dictiter_iternextitem_lock_held:5971`, confidence `low`) and its `detail` says:

> *"`Py_ssize_t` does not tear on LP64, so the visible effect is a stale value rather than
> memory unsafety — one `FT_ATOMIC_*` on each access is the fix."*

That is true of `di_used` and false of `di_dict`, which is the *other* member read on the
same line and is a pointer that another thread frees. A triager following the scanner's own
reasoning marks the line value-benign and moves on. Right line, wrong member, misleading
severity. It did not fire at all on `setiter_len:1062`, `dictiter_reduce:6392` or
`setiter_reduce:1071`.

Reproduction status: **both races observed under TSan, on both dict and set; no hard crash.**
`repro/tsan_stress_iter_methods.py` on `release-ft-nojit-tsan`, all 8 scenarios (4 threads ×
400 rounds; half the threads call `next()`, half call the method, on ONE iterator whose
container is a temporary) — **73 warnings, 14 unique races, 0 noise**
(`parse_tsan_report.py`; raw at `repro/tsan/tsan_stress_iter_methods_tsan.txt`, parsed at
`repro/tsan/iter_methods_parsed.json`). Pairs by innermost slice-file frame:

| n | pair | catalogued? |
|---|---|---|
| 9 | `setiter_iternext:1117` ↔ `setiter_iternext:1128` (`si_pos`) | TSAN-0040 |
| 6 | `dictiter_iternextkey:5784` ↔ `dictiter_iternext_threadsafe:6158` | TSAN-0026/0053 |
| 6 | `setiter_len:1062` ↔ `setiter_iternext:1130` | TSAN-0040 |
| 6 | `setiter_iternext:1128` ↔ itself | TSAN-0040 |
| 5 | `setiter_len:1063` ↔ `setiter_iternext:1134` | TSAN-0040 |
| 5 | `setiter_iternext:1130` ↔ itself (the write/write on `si_set`) | TSAN-0054 |
| 4 | `dictiter_len:5682` ↔ `dictiter_iternext_threadsafe:6158` | TSAN-0026 |
| 3 | `setiter_iternext:1101` ↔ `setiter_iternext:1130` (borrowed read vs. drop) | TSAN-0054 |
| 3+2+2 | `dictiter_reduce:6392` (memcpy) ↔ `dictiter_iternext_threadsafe:6158/:6149/:6150` | **NET NEW** |
| 2 | `dictiter_iternext_threadsafe:6158` ↔ itself | TSAN-0026 |
| 2 | `dictiter_iternextitem:6185` ↔ `dictiter_iternext_threadsafe:6158` | **NET NEW** (also F3) |

Rows 8 and 5 are worth calling out: `setiter_iternext:1101` ↔ `:1130` is the *borrowed read
vs. the drop* and `:1130` ↔ `:1130` is the *two threads both storing NULL* — i.e. TSan
independently reconstructs both halves of TSAN-0054's double-DECREF, not just the benign
`si_pos` cursor race that gh-144356 describes.

**Not counted as findings:** four of the 14 unique races pair a slice-file frame with a
frame in another subsystem (`marshal.c r_object` → `PyTuple_SET_ITEM`, `tuple_alloc`,
`__tsan_memset`, `object.c:477 _Py_ExplicitMergeRefcount`) at addresses in the `0x7fffb8…`
range — thread-stack territory, so these are most likely TSan stack-slot reuse across
threads rather than real cross-subsystem aliasing. Flagged as unverified, not reported.

*`__length_hint__` side* — already catalogued as one of TSAN-0026's signatures on the dict
side and TSAN-0040 (python/cpython#144356) on the set side, so: confirmed still live, not a
new finding.

```
Read  dictobject.c:5682 dictiter_len  <->  Previous write dictobject.c:6158 dictiter_iternext_threadsafe
Read  setobject.c:1062  setiter_len   <->  Previous write setobject.c:1130  setiter_iternext
```

*`__reduce__` side* — **net new**, and the more interesting of the two, because TSan catches
the struct copy itself:

```
Write of size 8 by thread T1:
  #0 dictiter_iternext_threadsafe   Objects/dictobject.c:6158:17     di->di_dict = NULL;
  #1 dictiter_iternextkey           Objects/dictobject.c:5791:9
Previous read of size 8 by thread T2:
  #0 __tsan_memcpy
  #1 dictiter_reduce                Objects/dictobject.c:6392:26     dictiterobject tmp = *di;
```

The compiler lowers `dictiterobject tmp = *di;` to a `memcpy`, so the read of the field the
other thread is clearing is the *whole struct*; `Py_XINCREF(tmp.di_dict)` on the next line
then INCREFs a pointer whose refcount the other thread may already have taken to zero. That
is the mirror image of the double-DECREF in F3 — same field, opposite direction.
`Objects/dictobject.c:dictiter_reduce` appears **zero** times in `known_races.tsv`; added as a
fourth signature on TSAN-0062.

The same script produced **no hard crash** on `debug-ft-nojit` (0/8 scenarios, 8 threads ×
20 000 rounds) — the window between the NULL check and the deref/INCREF is a handful of
instructions. CONSIDER on that basis, not FIX.

`setiter_reduce:1071` is the one predicted site that produced **no** race of its own, despite
being a textual twin of `dictiter_reduce:6392`. Not a claim that it is safe — its scenario
did produce the `setiter_iternext` self-races above, so the threads were interleaving; the
struct-copy read simply never landed in the window. Static-only.

One out-of-slice pair fell out of the same run and is worth a line for whoever owns
`listobject.c`: `dictobject.c:6392 dictiter_reduce` ↔ `listobject.c:373 list_get_item_ref`
(6 occurrences) — `dictiter_reduce`'s `PySequence_List(&tmp)` builds a list from a stack copy
of the iterator while another thread advances the original.

---

## 3. Classes bounded (checked here, clean, with denominator)

All three classes below were run on the four plain builds **and** under
`release-ft-nojit-tsan`, where each reported **zero** TSan warnings. That is the strong form
of the negative: TSan flags a race on first occurrence regardless of whether it materialises
as a crash, and the same harness reported 27 and 73 warnings on the iterator scripts, so the
detector is demonstrably armed.

**Resize under contention — clean, 5 scenarios × 4 builds × 2 runs = 40 runs, 0 crashes,
0 hangs, plus 5 scenarios under TSan with 0 warnings.**
`repro/tsan_stress_dictset_resize.py`: dict and set grow/shrink storms (2 growers + 1 bulk
updater) against 3 iterating/`len`/`get`/`in` readers and 2 `copy()`/`list(items())`/`==`
threads; `clear()` vs `list(d.items())`; `clear()` vs `list(s)`; dict-view set-ops vs mutation.
20 000 iterations per thread. `debug-ft-nojit`, `release-ft-nojit`, `debug-gil-nojit`,
`release-gil-nojit` — **no crash, no hang** on any build. The only cross-build difference was
`RuntimeError: dictionary/Set changed size during iteration`, raised on FT and not under the
GIL, which is the documented `di_used`/`si_used` version check doing its job, not a defect.
`dictresize`/`set_table_resize` are properly serialised against these readers.

**User `__hash__` / `__eq__` re-entering the same container — clean, 4 scenarios × 4 builds
× 2 runs = 32 runs, 0 crashes, 0 hangs, plus 4 scenarios under TSan with 0 warnings.**
`repro/tsan_stress_reentrant_hash_eq.py`:
(1) a bucket-colliding key class whose `__eq__` mutates the same dict, against 3 concurrent
plain mutators and 2 iterating readers; (2) the same for a set, against a bulk
resize/difference_update thread; (3) a key whose `__hash__` mutates both a dict and a set;
(4) `str`-subclass keys stored into eight *different* instances' split dicts, so all eight
threads contend on the one shared `dk_mutex` through `_Py_dict_lookup:1385` without any single
thread self-deadlocking. 2 000 iterations × 8 threads per scenario. `debug-ft-nojit`,
`release-ft-nojit`, `debug-gil-nojit`, `release-gil-nojit` — all `ok`.

**`set_add_entry`'s restart loop — holds.** `setobject.c:264-312` re-checks
`table != so->table || entry->key != startkey` after `PyObject_RichCompareBool` and
`goto restart`s; `set_lookkey_entry_threadsafe` (setobject.c:117-124) does the same with
`FT_ATOMIC_LOAD_PTR_ACQUIRE`. Scenario 2 above targets it directly and produced no failure —
the restart loop is a real guard, correctly scoped for the re-entrancy it addresses.

**`compare_unicode_generic`'s mutation re-check — holds for its own threat model.**
`dictobject.c:1171` `if (dk == mp->ma_keys && ep->me_key == startkey)` correctly returns
`DKIX_KEY_CHANGED` on re-entrant mutation. What it does *not* address is the lock it is
holding while the user compare runs — that is F2, and it is a different threat model from the
one this re-check defends against.

**Iterator `di_used` / `si_used` version checks — present on every iterator in both files,
denominator 6.** Enumerated by grep for `di_used !=` / `si_used !=`:
`dictiter_iternextkey_lock_held:5722`, `dictiter_iternextvalue_lock_held:5846`,
`dictiter_iternextitem_lock_held:5968`, `dictiter_iternext_threadsafe:6073` (the only one that
loads `ma_used` atomically), `dictreviter_iter_lock_held:6261`, `setiter_iternext:1109`.
Six iterators, six checks — no asymmetry. (The brief's hunt list flags "present in some
iterators and absent in others"; in *these two files* it is present in all of them, so that
asymmetry lives elsewhere in the tree.) Note the atomicity asymmetry that remains: five of the
six read `d->ma_used` plainly, one uses `_Py_atomic_load_ssize_relaxed` — but the five plain
ones all run under the dict's critical section (`*_lock_held`), so that is correct, not a gap.

**Split→combined transition under contention — clean, 5 scenarios × 4 builds × 2 runs =
40 runs, 0 crashes, 0 hangs, plus 5 scenarios under TSan with 0 warnings.**
`repro/tsan_stress_split_combined.py`: new-attribute storms
past `SHARED_KEYS_MAX_SIZE` across 16 freshly-minted classes; `setattr` vs `__dict__`
materialisation / `vars()`; `setattr` vs `delattr` vs `__dict__ =`; instantiate-vs-insert
`dk_usable` contention (`_PyObject_InitInlineValues:7315-7326` racing
`split_keys_entry_added:248-249`); iterate an instance `__dict__` while other threads push it
across the split→combined boundary. The `insert_split_key` keys mutex serialises the writers
correctly **as long as nothing re-enters under it** — which is exactly what F1 and F2 do.

---

## 4. Did not reproduce

| hypothesis | attempts | outcome |
|---|---|---|
| Shared-iterator double-DECREF as a hard crash on **release** FT | 30 runs (`gh154130_iter_sole_owner.py`, 6 kinds × 5, 8 threads × 8000 rounds) | 0/30. Expected: release has no `validate_refcounts`; TSan reports the race and debug-FT aborts 13/30, so the bug is confirmed by other means — but the release-build crash itself was not observed. |
| Shared-iterator double-DECREF with the container held in a **local** (refcount ≥ 2) | 8 scenarios × 4000 rounds × 8 threads on `release-ft-nojit`, then 7 refcount-delta probes × 200 rounds | 0/1 crash, and `sys.getrefcount` delta was **+0 in all 7 probes on both FT and GIL** — the over-DECREF did not materialise at all in that configuration. Only the sole-owner form (above) and TSan detect it. Recording this because it is the configuration a naive stress script produces, and it reads as a clean pass. |
| `dictiter_len` / `setiter_len` / `*_reduce` UAF as a **crash** | 8 scenarios × 20 000 rounds × 8 threads, `debug-ft-nojit` | 0/8. Static reasoning is in F4; no crash observed. |
| `dict.get(SubStr(...))` reaching `_Py_dict_lookup:1385` | 2 runs, `debug-ft-nojit` | 0/2 — reads use `_Py_dict_lookup_threadsafe`, which takes no keys lock. Documented in F2 as an entry-point requirement, not as a negative result about the site. |
| Resize/`clear()` under contention producing memory corruption or a race | 5 scenarios × 4 builds × 2 runs, plus 5 under TSan | 0/40 crashes, **0 TSan warnings** |
| Re-entrant `__hash__`/`__eq__` producing memory corruption or a race | 4 scenarios × 4 builds × 2 runs, plus 4 under TSan | 0/32 crashes, **0 TSan warnings** |
| Split→combined transition producing memory corruption or a race | 5 scenarios × 4 builds × 2 runs, plus 5 under TSan | 0/40 crashes, **0 TSan warnings** |
| `setiter_reduce:1071` struct-copy race (twin of the `dictiter_reduce` one that *did* fire) | 1 TSan scenario, 4 threads × 400 rounds, which produced 20 other `setiter_iternext` warnings | 0 — the threads interleaved, this particular read never landed in the window |
| Heap-buffer-overflow in the **keys** entry array (`DK_UNICODE_ENTRIES(keys)[ix]`, dictobject.c:1976, needs `ix ≥ 42`) | reached `ix` up to 36 | Not observed. The instance inline-values array (capacity ≤ 30, allocation breached at `ix ≥ 34`) is always breached first, so the keys-array overflow is shadowed. Static bound only. |

**A second self-inflicted timeout, also retracted.** In the batched TSan chain, the
`set iter: next vs __reduce__` scenario reported `TIMEOUT/HANG (180s)` while its dict twin
completed — again the FT-only-hang signature. Re-run **standalone** on the same
`release-ft-nojit-tsan` build, the identical workload (4 threads × 400 rounds) **completes in
90.6 s** (`repro/probe_setiter_reduce_timeout.py`, with per-round progress printed so a
stalled round would be visible). The 180 s breach was CPU contention — four `fusil --tsan`
fuzzer instances plus the rest of this matrix were sharing 16 cores. **Not a hang.**

**One retraction.** An earlier run of `tsan_stress_reentrant_hash_eq.py` came back
`exits 124 124` on `debug-ft-nojit` — a timeout that looks exactly like an FT-only hang, and
would have been reported as one. It was **my harness, not CPython**: with an unbounded
re-entrant `__eq__`, the re-entrant insert probes the same hash bucket and calls `__eq__`
again, so the *setup loop alone* never finished. Bounding the re-entrancy to one level per
thread (`_once()` in the script) makes the same scenario complete in seconds and pass 8/8.
Recorded here because "FT times out, GIL does not" is the exact signature this slice is
hunting, and this one was self-inflicted.

---

## 5. Toolkit feedback

### 5.1 Recall gap (highest-value item): `scan_ft_races` T3 suppresses on *any* lock in the function

`scan_ft_races.py:373`

```python
body = strip_comments(func["body"])
if _has_lock(body):
    return None                     # <-- whole-function suppression
```

`_has_lock` (`:219`) is a substring/regex test over the entire function body. It has no notion
of whether the flagged drop is *inside* the critical section it found. Both confirmed instances
of the exact class the rule targets are therefore invisible:

* `setobject.c:1095 setiter_iternext` has `Py_BEGIN_CRITICAL_SECTION(so)` at :1116 — which
  **ends at :1127, two lines before** `si->si_set = NULL; Py_DECREF(so)` at :1130-1131.
* `dictobject.c:6063 dictiter_iternext_threadsafe` has `Py_BEGIN_CRITICAL_SECTION(d)` at the
  `try_locked:` label (:6162) — **after** the `fail:` drop at :6158-6159.
* `dictobject.c:6342 dictreviter_iternext` takes the CS at :6352 *after* the borrowed read at
  :6347.

Measured, not inferred. A 22-line reduction of `setiter_iternext` (kept in
`scratchpad/ftgap/a.c`), scanned as-is and with the two `Py_*_CRITICAL_SECTION` lines deleted:

```
WITH    critical section : findings 0  []                          iternext_functions: 1
WITHOUT critical section : findings 1  ['iternext_double_decref']  iternext_functions: 1
```

So in this slice the rule had a real denominator (`iternext_functions: 8`) and a **false**
zero: it missed `TSAN-0053`/gh-154130 and `TSAN-0054`/gh-144356 — both already filed upstream,
both in the rule's own docstring as its motivating examples (`scan_ft_races.py:9-20` cites
"gh-154130 dict, gh-144357 set").

**Proposed fix.** Replace the boolean with a range test:

1. Collect `(begin_line, end_line)` for every `Py_BEGIN_CRITICAL_SECTION*` /
   `Py_END_CRITICAL_SECTION*` pair in the body (the file already has `_in_ranges()` for the
   `gil_only` `#ifdef` ranges — reuse it).
2. Suppress a drop candidate only when its line falls **inside** one of those ranges.
3. Where the drop is outside, emit with a `detail` that names the section it is outside of —
   that is the sentence a maintainer needs, and it is the difference between "you forgot a
   lock" and "your lock ends two lines too early".

A second, smaller variant is worth handling: `dictreviter_iternext` reads the member outside
the CS and the *drop* happens in a callee (`dictreviter_iter_lock_held`) that `_caller_holds_lock`
correctly exempts. The pair is only visible if the rule follows the one-call-deep
`*_lock_held` delegation that CPython's FT code uses everywhere. Suggest a T3b: *tp_iternext
that reads an owning self-member before entering its critical section and delegates to a
`*_lock_held` callee that drops that member.*

### 5.2 Precision, per rule, on what I touched

* `guarded_writer_unguarded_reader` (2 findings in slice) — 1 of the 2 lands on a line I
  independently reached by reading (`dictiter_len:5682`, F4), but on the **wrong member**
  (`di_used`, not `di_dict`) and with a `detail` that argues it is value-benign. Keep the rule,
  but when a flagged line reads *two* members, check the others: a `Py_ssize_t` counter next to
  a `PyObject *` is exactly where "stale value, not memory unsafety" stops being true. Concrete
  tuning: when the flagged statement also dereferences or NULL-tests a pointer member of the
  same struct, emit a second candidate for that pointer instead of inheriting the scalar's
  benign verdict.
* `atomic_plain_asymmetry` (17 findings) — I did not triage these; the two I looked at
  (`dictreviter_iter_lock_held:6305-6306`, `dictiter_iternextitem_lock_held:6017-6018`) are in
  `*_lock_held` functions where the caller's critical section is the ordering, so they read as
  low-yield here. No verdict offered beyond that.
* `iternext_double_decref` / `iternext_setref_null_decref` — 0 findings on a denominator of 8;
  see 5.1. This is the four-rules-certified-clean-on-a-zero-denominator failure mode in its
  more dangerous form: the denominator was *non*-zero, so nothing looked suspicious.

### 5.3 Neither `insert_split_key` nor `compare_unicode_generic` is in any scanner's slice output

Mechanically checked across all 13 `scanners/*.sample.json` (substring match over the full
finding JSON, so a mention in a `detail` string counts as a hit):

| site | flagged? |
|---|---|
| `insert_split_key` (F1, CPY-0096) | **not flagged by any scanner** |
| `compare_unicode_generic` (F2, CPY-0107) | **not flagged by any scanner** |
| `setiter_iternext` (F3) | **not flagged by any scanner** |
| `dictreviter_iternext` (F3) | **not flagged by any scanner** |
| `setiter_len` (F4) | **not flagged by any scanner** |
| `store_instance_attr_lock_held` (F1's crash site) | **not flagged by any scanner** |
| `dictiter_len` (F4) | flagged at the right line, wrong member — see 5.2 |
| `_Py_dict_lookup`, `dictiter_iternext_threadsafe` | appear only inside `detail` prose of findings at unrelated lines |

The two FIX findings above (`dictobject.c:1971`, `dictobject.c:1385`) appear in **none** of the
13 scanners' slice samples. Both are the same shape — *a lock held across a call that can run
arbitrary Python* — and both were found by `lock-discipline-checker`/`ft-race-scanner` only in
the sense that a human read the code after those agents pointed at the file.
`scan_lock_discipline` has **0 findings in the whole `Objects/` tree** (RUN_CONTEXT table),
which for a directory containing `LOCK_KEYS` used with `_Py_LOCK_DONT_DETACH` is a structural
zero worth investigating: the scanner models `Py_BEGIN/END_CRITICAL_SECTION` pairing but not
the raw `PyMutex_LockFlags`/`PyMutex_Unlock` pair that `LOCK_KEYS`/`UNLOCK_KEYS` expand to.

**Proposed rule** (`scan_lock_discipline` or a new `scan_lock_across_python`):
between a lock acquisition and its release, flag any call in a set of
"can-run-arbitrary-Python" functions — `PyObject_RichCompare*`, `PyObject_Call*`,
`PyObject_Hash`, `PyErr_FormatUnraisable`, `PyErr_WriteUnraisable`, `_PyType_Modified*`,
`PyObject_Repr/Str`. Seed the lock set with the macro expansions actually used in `Objects/`:
`LOCK_KEYS`, `Py_BEGIN_CRITICAL_SECTION*`, `PyMutex_Lock*`. On this slice that rule would
have produced exactly the two FIX findings, from a population of 2. The shape already has
three catalogue entries (CPY-0093, CPY-0096, CPY-0107) and is not scanner-backed.

---

## 6. What was written to the findings repos

Nothing was committed (other agents are active in `cpython-review-findings` this session);
working-tree changes only.

`cpython-tsan-findings/`
* **new** `reports/TSAN-0062-dict-reversed-iter-double-decref/` — `meta.json` (4 signatures),
  `repro.py`, `repro_sole_owner.py`, `repro_iter_methods.py`,
  `probe_setiter_reduce_timeout.py`, `tsan_report.txt`, `tsan_report_iter_methods.txt`.
  The `notes` field records the three zero-warning negative controls from the same build, so
  a later reader can see the detector was armed.
* `catalog/known_races.tsv` regenerated by `scripts/gen_known_races.py` → 225 signatures /
  59 races
* `INDEX.md` — one new section + row, including the `scan_ft_races` defect note

`cpython-review-findings/`
* `CPY-0096.../meta.json` — appended the measured N/M for all three GIL-build consequences,
  the ASan frame, the release-build SIGSEGV, the uninitialised-order-array mechanism, the
  `42` vs `34` size bounds, and the reachability caveat; added `CPY0096_gil_reentry.py` and
  `asan_heap_buffer_overflow.txt`
* `CPY-0107.../meta.json` — appended an independent-corroboration note with the gdb
  same-keys-pointer backtrace, the 12/12 + 6/6 vs 0/6 + 0/6 matrix, and the narrowed trigger
  (a `str` subclass suffices; no hash collision needed); added
  `CPY0107_lookup_keys_lock_reentry.py`

---

## 7. Noticed outside slice

* `Objects/typeobject.c:1222-1223` — `_PyType_Modified_Unlocked` calls a watcher and then
  `PyErr_FormatUnraisable("... %R", i, type)`; the `%R` alone is a second arbitrary-Python
  call site under whatever lock the caller holds. Already CPY-0084/CPY-0098 territory.
* `Modules/_testinternalcapi.c:2106 get_object_dict_values` reads `values->values[i]` for
  `i < keys->dk_nentries` without bounding by the *instance's* `values->capacity`. Once F1
  drives `dk_nentries` past capacity this helper itself over-reads. Test-only, but it will
  produce an ASan report that names `_testinternalcapi.c` and mislead triage.
