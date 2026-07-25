# lock-discipline-checker — obj-mappings slice

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777`
Scope: `Objects/dictobject.c` (8,597 lines), `Objects/setobject.c` (3,228 lines).
Build-matrix parity: `Objects/dictobject.c`, `Objects/setobject.c`, `Objects/typeobject.c`,
`Python/critical_section.c`, `Python/lock.c` are **byte-identical** between the matrix build
commit `a1d580430c8` and the target ref `4f3be1b5777` (`git diff --quiet` clean on all five).

---

## 1. Denominator

| quantity | value |
|---|---|
| `scan_lock_discipline` findings — slice | **0** (`scanners/scan_lock_discipline.sample.json`) |
| `scan_lock_discipline` findings — whole `Objects/` | **0** (113 files, 4,861 functions) |
| `scan_lock_discipline` findings — `Objects/`+`Modules/`+`Python/` (measured by me) | **0** (614 files, 19,521 functions, 818 CS-functions, 106 mutex-functions) |
| slice vocabulary resolved | 183 tokens over 15 distinct macros |
| lock windows I read by hand | **99** — 54 in `dictobject.c`, 30 in `setobject.c`, 2 in `clinic/dictobject.c.h`, 13 in `clinic/setobject.c.h` |
| windows containing a `return`/`goto` between BEGIN and END | **5** (all in `dictobject.c`) — all 5 verified correct |
| `_Py_LOCK_DONT_DETACH` regions in the slice | **9** (7 keys-mutex + 2 watcher-mutex) — all read individually |
| `Py_BEGIN_CRITICAL_SECTION2` sites | 6 dict + 11 set + 4 clinic-set = 21 |
| two-object nestings via two single-object begins | **2**, both cross-file (clinic) |
| lines read | ~2,600 in the two slice files, plus `Python/critical_section.c`, `Python/lock.c:80-165`, `Python/pystate.c:2316-2331`, `Include/internal/pycore_critical_section.h`, `Include/internal/pycore_lock.h:35-52`, `Objects/clinic/{set,dict}object.c.h` |

**Which zero is it?** Both, in different places, and the split is the whole story:

- For the shapes the rule **models** (leaked lock on an early `return`/`goto`; unpaired
  acquire; two nested single-object begins visible in one file), the zero is **evidential and
  earned**. 99 windows, 5 with an inner exit, all 5 correct. I re-derived it independently of
  the scanner and got the same answer.
- For the shapes that **actually bite this code** — arbitrary Python run under a
  non-reentrant `_Py_LOCK_DONT_DETACH` mutex — the zero is **structural**. There is no such
  rule. Both recorded lock findings in this slice (CPY-0096, CPY-0107) are of that shape, and
  neither is a leak nor a nesting, so neither could ever have been surfaced.

The briefing's hypothesis ("the rule only knows `Py_BEGIN_CRITICAL_SECTION` and dict uses
`LOCK_KEYS`") is **wrong** and I want that on record, because it would have sent the fix in the
wrong direction. `resolve_local_lock_macros` already resolves `LOCK_KEYS`,
`LOCK_KEYS_IF_SPLIT`, `UNLOCK_KEYS`, `UNLOCK_KEYS_IF_SPLIT` to `python_mutex` acquire/release,
and the sample envelope proves it (`local_lock_macros["Objects/dictobject.c"]`). The real
recall gaps are three different things, all measured in §5.

Attribution measured per token (my probe, `functions extracted: 292` for `dictobject.c`):

```
token                                   raw  in-fn  orphan
Py_BEGIN_CRITICAL_SECTION                40     40       0
Py_BEGIN_CRITICAL_SECTION2                6      6       0
Py_BEGIN_CRITICAL_SECTION_MUTEX           1      0       1  [1959]   <- insert_split_key
Py_END_CRITICAL_SECTION                  41     40       1  [1984]   <- insert_split_key
LOCK_KEYS                                11      4       7  [...,1962] <- insert_split_key
UNLOCK_KEYS                               8      4       4  [...,1981] <- insert_split_key
```

---

## 2. Findings

### [FIX — UPGRADED to `reproduced`] CPY-0107 is a deterministic *single-threaded* self-deadlock, not just a lock-order inversion
`Objects/dictobject.c:1385` (`_Py_dict_lookup`) → `:1168` (`compare_unicode_generic`)

**Confirmed, not re-litigated:** the site is unchanged. What is new is the mechanism and the
status.

**Mechanism.** `LOCK_KEYS(keys)` is `PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)`
(`:227`). `_Py_LOCK_DONT_DETACH == 0` (`Include/internal/pycore_lock.h:37`), so
`Python/lock.c:139` parks with `detach == false`, and `Python/pystate.c:2321`
(`_PyCriticalSection_SuspendAll`) never runs. A `PyMutex` is not reentrant. Therefore **one
thread** that re-enters split-keys dict code from inside the `PyObject_RichCompareBool` at
`:1168` parks on a mutex it already holds and can never be woken — no second thread, no race,
no timing window. The record said "lock-order inversion"; it is strictly worse than that.

**Reachability (this is the part that took the work).** On a free-threaded build the *read*
entry points do **not** take the keys lock: `dict_subscript`/`PyDict_GetItem` route through
`_Py_dict_lookup_threadsafe:1601`, whose `unicodekeys_lookup_generic_threadsafe:1522` →
`compare_unicode_generic_threadsafe:1485` runs the same `PyObject_RichCompareBool` **lock-free**.
My first reproducer used a lookup and correctly failed (0/4 builds) — recorded here because it
is exactly the wrong-model trap lesson 4 warns about. The trigger is a **mutation**:
`insertdict:2038`, `_PyDict_DelItem_KnownHash_LockHeld:3030`, `dict_setdefault:3291` all call
`_Py_dict_lookup` directly. So: a *store* into a split-keys dict (an instance `__dict__`) under
a non-`str` key whose `__hash__` collides with a real attribute name.

**Guarded twin, and what its guard addresses (lesson 3).** `compare_unicode_generic_threadsafe`
/`compare_generic_threadsafe:1577` are the twin: same comparison, same re-validation, **no keys
lock** — they defend against exactly this by not taking the lock at all, using
`_Py_TryIncrefCompare` + a `read_failed:` fallback instead. Note carefully what that twin does
*not* address: it is a twin for the *deadlock* threat model only. It still runs user `__eq__`
mid-lookup, so it is not evidence of safety against re-entrant mutation — the `DKIX_KEY_CHANGED`
restart is the separate guard for that.

**Reproduction.** `reports/obj-mappings/repro/CPY-0107_self_deadlock.py`

| build | result |
|---|---|
| `debug-ft-nojit` | **hang 5/5** (SIGKILL from `timeout -s KILL 10`) |
| `release-ft-nojit` | **hang 5/5** |
| `debug-gil-nojit` | clean 0/5 hang (completes, `LOCK_KEYS` is empty at `:257`) |
| `release-gil-nojit` | clean 0/5 hang |

Last output before the hang is the print inside `__eq__`, i.e. the interpreter is inside
`compare_unicode_generic` with `dk_mutex` held. Because the waiter does not detach, the process
also becomes un-stoppable: any subsequent stop-the-world request hangs behind it.

**Fix.** Same as the twin: either drop the keys lock around the comparison and re-validate
(`_Py_TryIncrefCompare` + `DKIX_KEY_CHANGED`), or hoist the comparison out of the locked region.

---

### [FIX — confirmed in one line, with one new fact] CPY-0096
`Objects/dictobject.c:1962` (`LOCK_KEYS(keys)`) → `:1971` (`_PyType_Modified_Unlocked(type)`)

Still present; the acquire is at **1962** and the violating call at **1971** (both line numbers
in circulation are correct, they name different things — see §4).

**New fact from `git blame`, which sharpens the "audit the file against its own comment" framing.**
The prohibition comment is `bef570622263` (Victor Stinner, **2026-06-25**), lines 218-226:

> `// We are not allowed to acquire other locks within LOCK_KEYS(). For example,`
> `// PyType_Modified() must not be called within LOCK_KEYS() since it acquires`
> `// the type lock.`

The violating call at `:1971` is `20921922fa4d` (Dino Viehland, **2026-07-06**) — the "take 2"
re-land, **eleven days AFTER the comment was written**, ten lines below it in the same file.
So this is not the CPY-0107 situation of a rule written 26 months too late and never applied
backwards; here the rule already existed at HEAD and the new code was written through it. The
`LOCK_KEYS(keys)` acquire itself is `d5df25268b03` (2024-04-25), the same commit that introduced
CPY-0107's acquire.

The `Py_BEGIN_CRITICAL_SECTION_MUTEX(&…types.mutex)` added at `:1959` by that same commit fixes
the *ordering* half (type lock before keys lock) and is correct. It does not touch the
re-entrancy half: `_PyType_Modified_Unlocked` still runs a type-watcher callback and
`PyErr_FormatUnraisable` → `sys.unraisablehook` under a non-reentrant, non-detaching mutex.

---

### [CONSIDER] The slice's only two-object nesting is invisible from the `.c` file
`Objects/setobject.c:2662` (`set_remove_impl`), `Objects/setobject.c:2702` (`set_discard_impl`)

**What.** Both are `@critical_section`-annotated, so `Objects/clinic/setobject.c.h:474` / `:501`
wrap them in `Py_BEGIN_CRITICAL_SECTION(so)`. Each body then opens a *second* section on a
*different* object:

```c
PyErr_Clear();
Py_hash_t hash;
Py_BEGIN_CRITICAL_SECTION(key);      /* setobject.c:2662 / :2702 — `so` already held */
hash = frozenset_hash_impl(key);
Py_END_CRITICAL_SECTION();
rv = set_discard_entry(so, key, hash);
```

Acquisition order is `so → key`. `set_issubset`/`set_issuperset`/`set_isdisjoint`/
`set_symmetric_difference` take the same two objects via `Py_BEGIN_CRITICAL_SECTION2(so, other)`,
which is **address-ordered** — so when `key` sorts below `so`, the two orders are opposed.

**Classification: CONSIDER, not FIX — and the reason matters more than the finding.** In CPython
this is *not* a deadlock. `_PyCriticalSection_BeginSlow` (`Python/critical_section.c:57`) blocks
in `PyMutex_Lock`, i.e. `_PY_LOCK_DETACH`; the park detaches the thread state
(`Python/lock.c:139`), and `detach_thread` calls `_PyCriticalSection_SuspendAll`
(`Python/pystate.c:2321`), releasing every section the waiter holds. The cycle is broken by
construction. What is actually lost is **atomicity**, not liveness. Neither body caches anything
across the inner section (`set_discard_entry` re-reads `so->table`), so both are safe today.

**Verified empirically** (`repro/set_add_entry_lock_suspension.py`): while thread A sits inside a
user `__eq__` reached from `set.add` — i.e. nominally holding `so`'s critical section — thread B
adds 300 elements to the same set, forcing at least one `set_table_resize`. Result **3/3** on
`debug-ft-nojit`, `release-ft-nojit`, `debug-gil-nojit`: `so`'s section was suspended.

This is the answer to the coordinator's open question on `set_add_entry_takeref:290-295`: the
window is **not** merely single-thread re-entrancy. Under free-threading it is a genuine
concurrent-mutation window that includes a full table resize, because the "held" lock is
released the moment the comparison blocks. The `if (table != so->table || entry->key != startkey)
goto restart;` guard at `:294` exists *precisely because* the lock does not survive the compare —
which is exactly why placing `if (cmp > 0) goto found_active;` *above* it at `:290` is wrong. I
confirm it does not become a UAF: `found_active:` does `Py_DECREF(key); return 0;` and never
dereferences `entry`, and `set_swap_bodies`/`set_table_resize` cannot change that. So the
consequence remains the silent lost update the coordinator already reproduced 4/4.

---

### [CONSIDER] Assertion asymmetry: the deadlock-fatal lock is the one that is never asserted
`Objects/dictobject.c:230` (definition), `:244` (the single use)

`ASSERT_DICT_LOCKED` is used **20** times. `ASSERT_KEYS_LOCKED` is used **once**, in
`split_keys_entry_added:244`. Every other function with a keys-lock obligation carries no
assertion at all: `unicodekeys_lookup_unicode:1207`, `unicodekeys_lookup_generic:1186`,
`find_empty_slot:1877`, `dictkeys_set_index:552`, `build_indices_generic:2137`,
`build_indices_unicode:2152`.

The asymmetry runs the wrong way. The dict lock is an ordinary critical section: violating it
costs atomicity. The keys lock is `DONT_DETACH`: violating it costs the process. 1 assertion of
~7 obligations on the fatal lock, 20 on the survivable one.

It also compounds with the mapper's point, which I verified myself
(`Include/internal/pycore_critical_section.h:62-73`): `ASSERT_DICT_LOCKED` →
`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` is skipped entirely when `Py_REFCNT(op) == 1` and
compiles to nothing without `Py_DEBUG`. `ASSERT_KEYS_LOCKED` has no such escape — it is a plain
`assert(PyMutex_IsLocked(&keys->dk_mutex))` — so it is the *more* trustworthy of the two and is
the one that is barely deployed. **Nowhere in this report do I use an `ASSERT_*_LOCKED` as proof
that a lock is held**; §2's suspension experiment shows why that inference is unsound in general,
since the assert only speaks about function entry.

---

### [ACCEPTABLE] `dictobject.c:4380` — dead `return -1;`
Inside `dict_merge`'s generic branch, directly after `goto slow_exit;`. Unreachable, so not a
leak; the scanner already models this (`_is_unreachable_statement`). Cosmetic residue of the
gh-112075 retrofit. Listed only because it is the single textual hit in the whole slice that
*looks* like `critical_section_end_on_error`.

---

## 3. Classes bounded (checked here, clean, with the denominator)

**Leaked lock on early return / goto — 99 windows, 0 leaks.** Audited textually (independent of
tree-sitter, so it covers the functions `extract_functions` drops). 5 windows contain an inner
exit; in all 5 the `goto` label sits *inside* the section immediately above the `END`:

| window | exits | label | verdict |
|---|---|---|---|
| `dict_merge` 4321-4392 | 6× `goto slow_exit`, 1 dead `return -1` | `slow_exit:` at 4386 | correct |
| `_PyObject_MaterializeManagedDict` 7386-7400 | `goto exit` | `exit:` at 7398 | correct |
| `try_set_dict_inline_only_or_other_dict` 7805-7830 | 2× `goto exit_lock` | `exit_lock:` at 7828 | correct |
| `ensure_managed_dict` 8051-8063 | `goto done` | `done:` at 8062 | correct |
| `ensure_nonmanaged_dict` 8078-8095 | `goto done` | `done:` at 8093 | correct |

`setobject.c`: 30 windows, **zero** with any inner exit — every one is the trivial
`begin; rv = *_lock_held(...); end;` wrapper. Both clinic headers: 15 windows, all trivial, all
paired (I checked the apparent 10-vs-9 `BEGIN`/`END` imbalance in `clinic/setobject.c.h` — it is
the `#include` comment on line 5, not a leak).

**`_Py_LOCK_DONT_DETACH` regions — 9, exhausted.** This resolves both discrepancies you flagged.
The history agent said six, the include-graph-mapper said seven; the true count is **nine**,
because two regions use a *third* macro spelling neither of us was looking for.

| # | site | function | verdict |
|---|---|---|---|
| 1 | 738-794 `LOCK_KEYS_IF_SPLIT` | `_PyDict_CheckConsistency` | conforms — atomics/asserts only. **Assert-only reachability**: called from `ASSERT_CONSISTENT` (`:666/668`), i.e. inside `assert(...)`, plus `Objects/object.c:64`. This is the region the history agent excluded to get six. |
| 2 | 1283-1285 `LOCK_KEYS` | `unicodekeys_lookup_split` | conforms — `unicodekeys_lookup_unicode` → `unicode_eq`, no Python, no DECREF |
| 3 | 1317-1320 `LOCK_KEYS` | `_PyDictKeys_StringLookupAndVersion` | conforms — `_PyDictKeys_GetVersionForCurrentState:8171` is atomics-only and takes no lock |
| 4 | 1385-1389 `LOCK_KEYS_IF_SPLIT` | `_Py_dict_lookup` | **VIOLATES** — `PyObject_RichCompareBool:1168` *and* `Py_DECREF:1169`. CPY-0107 |
| 5 | 1962-1981 `LOCK_KEYS` | `insert_split_key` | **VIOLATES** — `_PyType_Modified_Unlocked:1971`. CPY-0096 |
| 6 | 2230-2261 `LOCK_KEYS` | `dictresize` | conforms — `Py_NewRef` is an INCREF (permitted); `build_indices_*`, `get_index_from_order` are pure. Checked specifically because it is the #1 crash-fix function; no exits in the window |
| 7 | 7317-7321 `LOCK_KEYS` | `_PyObject_InitInlineValues` | conforms — `_Py_atomic_*` only |
| 8 | 8252-8264 `FT_MUTEX_LOCK_FLAGS(&…watcher_mutex, _Py_LOCK_DONT_DETACH)` | `PyDict_AddWatcher` | conforms structurally — `goto done` targets the label above `FT_MUTEX_UNLOCK`. One note: `PyErr_SetString:8261` allocates under the mutex, so a GC there could stop the world while another `AddWatcher` caller is parked un-detachably. Rare, C-API-only, short — ACCEPTABLE, but it is the same latent shape |
| 9 | 8272-8283 same | `PyDict_ClearWatcher` | conforms — same structure |

**So: 7 of 9 conform to the file's own specification at `:218-226`, 2 violate it, and both
violations are already recorded.** I found no *third* violation, in either file. Within this
slice the claim "CPY-0107 is the only lock-order inversion" holds — but I would restate it, since
it is not primarily an inversion (§2).

**Two-object locking — 21 `CS2` sites, 2 nested singles, 0 misuses.** Every two-set and
two-dict operation uses `Py_BEGIN_CRITICAL_SECTION2`: `set_update_internal:1291/1298`,
`set_intersection_multi:1826`, `set_and:1884`, `set_iand:1900`, `set_difference_update:2059`,
`set_difference_multi:2181`, `set_sub:2210`, `set_isub:2227`,
`set_symmetric_difference_update:2314/2324`, plus the 4 clinic ones; and
`dict_merge:4312`, `dict_equal:4728`, `dict_richcompare:6882`, `_PyObject_SetManagedDict:7893`,
`dict_init:3507/3523`. The single-lock cases are each provably correct, not oversights:
`set_update_local:1265/1272` locks only `other` because `assert(Py_REFCNT(so) == 1)` makes `so`
thread-local; the `PyFrozenDict_CheckExact` arms lock only `so` because a frozendict is
immutable; `set_symmetric_difference_update_impl:2334` locks only `so` because `otherset` is a
freshly built local. The only nestings are the two clinic ones in §2.

**Cross-family nesting.** `insert_split_key` holds `types.mutex` (scoped, `:1959`) across
`dk_mutex` (`PyMutex`, `:1962`). The ordering is now documented and correct (`:1955-1958`).
Bounded: it is the only cross-family nesting in the slice.

---

## 4. Resolving the two discrepancies you asked about

- **`1962` vs `1971` for CPY-0096.** Both are right and they are different things. `:1962` is
  the `LOCK_KEYS(keys)` **acquire** (blame `d5df25268b03`, 2024-04-25); `:1971` is the
  `_PyType_Modified_Unlocked(type)` **violating call** (blame `20921922fa4d`, 2026-07-06). The
  mapper was enumerating acquires, the finding record names the offending statement. No conflict;
  a lock-scope finding needs both, and I have cited both.
- **six vs seven `LOCK_KEYS` regions.** Seven lexical regions exist. Six are on ordinary runtime
  paths; the seventh (`_PyDict_CheckConsistency:738`) is reachable only through `assert()`, which
  is presumably why the history agent excluded it. Neither count is the population my class
  needs, which is *`DONT_DETACH` regions*: that is **nine**, adding
  `PyDict_AddWatcher:8252` and `PyDict_ClearWatcher:8272`, which spell the acquire
  `FT_MUTEX_LOCK_FLAGS(..., _Py_LOCK_DONT_DETACH)`.

---

## 5. Toolkit feedback

### 5.1 Recall gap A — Argument Clinic sections (MEASURED, patch validated then reverted)

Verified independently. `Objects/clinic/setobject.c.h` contains **13** lock scopes — 9
`Py_BEGIN_CRITICAL_SECTION(so)` (lines 26, 77, 101, 125, 404, 474, 501, 525, 549) and 4
`Py_BEGIN_CRITICAL_SECTION2(so, other)` (230, 330, 354, 378) — matching the 13
`@critical_section` directives in `setobject.c`. `clinic/dictobject.c.h` has 2
(`dict.setdefault`, `dict.popitem`). So `set.add/remove/discard/pop/copy/clear` read as
*unlocked* from the `.c` file, and `set.issubset/issuperset/isdisjoint/symmetric_difference` read
as *unlocked* when they in fact hold a `CS2`.

One correction to the framing: the scanner **does** read the clinic headers — they are ordinary
`.h` files and `discover_c_files` picks them up (`files_analyzed: 113` for `Objects/` against
only 50 `.c` files). The generated wrappers are trivially correct, so scanning them yields
nothing. The defect is that the wrapper's lock is never **associated with the `_impl` it wraps**,
so no cross-file reasoning is possible.

**Patch measured** (`resolve_clinic_sections()`: read `<dir>/clinic/<file>.c.h`, map each
`*_impl` called inside a BEGIN/END window to an implicit outer section, seed the nesting stack
with it, and count it in the denominators). Before/after over `Objects/`+`Modules/`+`Python/`:

| | findings | by type | `critical_section_functions` | `mutex_functions` |
|---|---|---|---|---|
| before | 0 | — | 818 | 106 |
| after | **3** | `nested_critical_sections` 3 | **1281** (+463, +57%) | **142** (+36, +34%) |

468 clinic-locked `_impl`s recovered across 34 files. The 3 findings are
`setobject.c:2662 set_remove_impl`, `setobject.c:2702 set_discard_impl` (exactly the two I found
by reading — 2/2 recall on my manual result) and `Python/traceback.c:211
traceback_tb_next_set_impl`, which I read and confirmed is the same real shape
(`traceback_tb_next_set` is clinic-locked on `self` at `clinic/traceback.c.h:121`, and the body
opens `Py_BEGIN_CRITICAL_SECTION(cursor)` on a different traceback in a loop). **Precision 3/3
for the shape**; all three are ACCEPTABLE once triaged, for the reason in §5.4.

**Does it recover CPY-0096?** **No.** Nor CPY-0107. Neither is a leak or a nesting; they need
the new rule in §5.5. This patch's value is the +57% denominator and the two-object-nesting
recall, not the recorded findings.

Patch applied, measured, then reverted: `git checkout -- plugins/.../scan_lock_discipline.py`;
`git status --porcelain` shows exactly one line, `?? reports/obj-mappings/` (this run's own
output). No toolkit file is modified.

### 5.2 Recall gap B — `FT_MUTEX_*` is not in the vocabulary (one-line data fix)

`Include/internal/pycore_pyatomic_ft_wrappers.h:140-142` defines
`FT_MUTEX_LOCK` / `FT_MUTEX_LOCK_FLAGS` / `FT_MUTEX_UNLOCK` as thin wrappers over
`PyMutex_Lock` / `PyMutex_LockFlags` / `PyMutex_Unlock`. None of the three is in
`data/lock_macros.json`, and `resolve_local_lock_macros` cannot help because the `#define`s live
in a *header*, not in the scanned `.c`. **27 uses tree-wide**, 4 of them in the slice
(`dictobject.c:8252, 8261-ish, 8272, 8281`) — and those 4 are `DONT_DETACH` acquires, i.e.
exactly the highest-risk family. Fix: add
`{"type": "python_mutex", "acquire": ["FT_MUTEX_LOCK", "FT_MUTEX_LOCK_FLAGS"], "release": ["FT_MUTEX_UNLOCK"]}`
to `lock_macros.json`. Zero code change.

### 5.3 Recall gap C — functions tree-sitter drops (measured, small but precisely targeted)

`extract_functions` returns **286** functions for `dictobject.c` and misses three:
`slot` (`:158`), `insert_split_key` (`:1943`), `dictiter_iternextitem` (`:6182`). All three
contain `#ifdef` in statement position; the whole-file `root_node.type` is `ERROR`. I bisected
`insert_split_key`: blanking *any one* of line 1951 (`if (ix >= 0) {`), line 1959
(`Py_BEGIN_CRITICAL_SECTION_MUTEX(...)`) or line 1983 (`#ifdef Py_GIL_DISABLED`) restores the
parse — it is an interaction between a preproc block containing a braced `if` and a later preproc
block, not any single construct. `dictiter_iternextitem` is genuinely unparseable (the `#ifdef`
splits an `if (...) {`).

Tree-wide scale: **76 of 2,070** lock call sites (3.7%, and ~26 of the 76 are `#include`
trailing comments I did not filter, so ~2.4% real) land outside every extracted function,
concentrated in `Modules/mmapmodule.c` (16) and `Objects/dictobject.c` (6). Small — but the 6 in
`dictobject.c` are *all four* lock macros of `insert_split_key`, i.e. the gap swallows precisely
the one function in this slice carrying a recorded lock finding. Suggested fix (surgical, not a
grammar change): when a regex finds a column-0 function definition that `extract_functions` did
not return, blank the `#`-directive lines within that function's textual extent and re-parse just
that snippet. I verified this recovers `insert_split_key` (`blank preproc lines` →
`fns=['insert_split_key']`). Note `tree_sitter_utils.py` is shared and **cext-review-toolkit is
upstream** for it, so this belongs there first.

### 5.4 Precision defect — the `nested_critical_sections` rationale is wrong for CPython

The finding text says: *"If another thread locks them in the opposite order this deadlocks."*
For CPython's own `Py_BEGIN_CRITICAL_SECTION` that is **false**, and stating it will get true
findings dismissed by maintainers. A critical section that blocks detaches
(`Python/lock.c:139`), and detaching runs `_PyCriticalSection_SuspendAll`
(`Python/pystate.c:2321`), releasing everything the waiter holds. The cycle cannot form. I
demonstrated the suspension directly, 3/3 builds (`repro/set_add_entry_lock_suspension.py`).

The rule is still worth keeping — the real consequence is **loss of atomicity across the inner
section**, which is a genuine and much more common bug source. Proposed replacement wording:

> `'F' opens CS(b) at L2 while CS(a) from L1 is held. If the inner acquire contends, the thread
> detaches and `_PyCriticalSection_SuspendAll` releases `a` too — so any state read before L2 and
> used after L3 may have been mutated by another thread. Use `Py_BEGIN_CRITICAL_SECTION2(a, b)`,
> or re-validate after the inner section.

That rewording also makes the rule's triage tractable: the question becomes "is anything cached
across the inner section?", which is answerable, instead of "can two threads invert?", which is
always no.

### 5.5 The missing rule — `python_under_non_detaching_mutex` (the one that matters)

This is the rule that would have found **both** CPY-0096 and CPY-0107, and it has an unusually
tractable scope.

*Trigger.* An acquire whose flag argument is `_Py_LOCK_DONT_DETACH`, resolved through the
existing `#define` machinery, with any of the following inside the held region:
(a) a `PYTHON_REACHING_APIS` call (`PyObject_RichCompareBool`, `PyObject_Call*`,
`PyObject_Hash`, `_PyType_Modified*`, `PyErr_FormatUnraisable`, …);
(b) `Py_DECREF` / `Py_XDECREF` / `Py_CLEAR` / `Py_SETREF` on anything (a `__del__` runs Python);
(c) another lock acquire of any family.
Classification FIX; the file's own comment at `dictobject.c:218-226` is the specification, and
`Objects/weakrefobject.c:26` states the same rule for the weakref striped lock.

*Scope, measured.* Only **8** `_Py_LOCK_DONT_DETACH` acquire macros exist tree-wide:
`dictobject.c:227` (`LOCK_KEYS`), `dictobject.c:8253`, `dictobject.c:8274`,
`pycore_weakref.h:24` (`LOCK_WEAKREFS`), `pycore_weakref.h:29` (`LOCK_WEAKREFS_FOR_WR`),
`uniqueid.c:20`, `index_pool.c:148`, `tracemalloc.c:39`, `exceptions.c:4100`
(`MEMERRORS_LOCK`), `pycore_pystate.h:266` (`HEAD_LOCK`). Under ten definitions — the rule can be
exhaustive rather than heuristic. It also gives `mutex_missing_unlock`'s currently-unearned zero
a real job.

Two of the ten already have a known cross-file consumer: `LOCK_WEAKREFS` is in
`lock_macros.json` and `Objects/weakrefobject.c:199` (`weakref_hash_lock_held`, CPY-0021) hashes a
referent *inside* that striped lock — which is the same shape as CPY-0107 in a different file, and
is already a recorded finding. That is a free cross-file validation for the rule before it ships.

### 5.6 Denominator defect — `mutex_functions` ignores resolved aliases

`_has_mutex_lock()` (`scan_lock_discipline.py:707`) consults the **global** families, not the
per-file `file_families`, so locally-`#define`d mutex acquires never count. The slice envelope
therefore reports `mutex_functions: 0` for a file with 6 mutex-locking functions
(`_PyDict_CheckConsistency`, `unicodekeys_lookup_split`, `_PyDictKeys_StringLookupAndVersion`,
`_Py_dict_lookup`, `dictresize`, `_PyObject_InitInlineValues`). A zero next to a zero denominator
is the exact failure mode this run is built to catch, and here the denominator was wrong by
construction. One-line fix (included in my §5.1 patch measurement): 106 → 142 tree-wide.

---

## 6. Noticed outside slice

- `Python/traceback.c:211` `traceback_tb_next_set_impl` — same clinic-outer + inner-`CS(cursor)`
  nesting as `set_remove_impl`; surfaced by my patch, ACCEPTABLE per §5.4. Belongs to whichever
  slice owns `Python/`.
- `Modules/mmapmodule.c` — 16 lock call sites orphaned by the tree-sitter parse gap (§5.3), the
  largest single concentration tree-wide. Worth a re-scan once the parse recovery lands.
- `Objects/weakrefobject.c:199` `weakref_hash_lock_held` (CPY-0021) is, structurally, CPY-0107 in
  another file: `PyObject_Hash` on a referent inside a `_Py_LOCK_DONT_DETACH` striped lock. Only
  noted; it is another slice's and already recorded.

## 7. Files written

- `reports/obj-mappings/agents/lock-discipline-checker.md` (this report)
- `reports/obj-mappings/repro/CPY-0107_self_deadlock.py` — 5/5 hang FT, 0/5 GIL
- `reports/obj-mappings/repro/set_add_entry_lock_suspension.py` — 3/3 shows section suspension
