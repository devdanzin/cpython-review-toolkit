# gil-discipline-checker — obj-mappings slice

**Scope:** `Objects/dictobject.c` (8,597 lines) + `Objects/setobject.c` (3,228 lines)
@ `4f3be1b5777`. Nothing else. Builds used are at `a1d580430c8`; **`git diff 4f3be1b5777
a1d580430c8 -- Objects/dictobject.c Objects/setobject.c` is empty — both files are
byte-identical between the target ref and every build in the matrix**, so the builds
are valid oracles for this slice.

## 1. Denominators

| quantity | value |
|---|---|
| `scan_gil_usage` findings in slice | **0** |
| `scan_gil_usage` functions analyzed in slice | 404 (2 files) |
| `scan_gil_usage` `vocabulary_counts` in slice | `Py_BEGIN_ALLOW_THREADS 0`, `Py_END_ALLOW_THREADS 0`, `PyGILState_Ensure 0`, `PyGILState_Release 0` — `vocabulary_resolved: 0` |
| independent `grep -c` of the same four tokens over both files | **0** (confirms the scanner, not a vocabulary gap) |
| `Py_GIL_DISABLED` conditional regions enumerated (**redirected remit denominator**) | **51** — 44 in `dictobject.c`, 7 in `setobject.c` |
| of those, two-arm (`#else`/`#elif` present) | 20 dict + 4 set = **24** |
| of those, one-arm | 24 dict + 3 set = **27** |
| regions read in full | **51 / 51** |
| FT-only lock assertions in slice (compile to nothing under the GIL) | **61** — dict: `ASSERT_DICT_LOCKED` 23, `ASSERT_WORLD_STOPPED_OR_DICT_LOCKED` 3, `ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED` 5, `ASSERT_KEYS_LOCKED` 3, `ASSERT_OWNED_OR_SHARED` 3, `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` 9; set: `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` 15 |
| Python-visible GIL-vs-FT behavioural probes run | **18**, × 4 builds |
| lines read (regions + enclosing functions + callees) | ~2,400 |

## 2. The classic class is STRUCTURALLY ABSENT — one section, as instructed

`Py_BEGIN_ALLOW_THREADS` / `Py_END_ALLOW_THREADS` / `PyGILState_Ensure` /
`PyGILState_Release` appear **zero times** in either file. `rule_not_applicable: true`
in the slice sample, and an independent `grep -c` agrees: this is **"no constructs
present"**, not "constructs present and verified clean". dict and set are core object
code that never releases the GIL and never re-acquires it from a foreign thread; the
rule cannot fire here.

For calibration: across all of `Objects/` the vocabulary resolves to **8 tokens**
(3 `Py_BEGIN_ALLOW_THREADS`, 3 `END`, 1 `Ensure`, 1 `Release`) over 123 files /
4,861 functions, and the scanner's 16 `Objects/`-wide findings are **all**
`blocking_with_gil` in the classes my own agent brief pre-declares ACCEPTABLE:
`PyObject_Dump`/`_PyObject_AssertFailed` crash-dump paths (10), the one-shot
`_pymalloc_system_hugepage_size` `/proc/meminfo` probe, `_PyObject_DebugDumpAddress`,
`PyObject_Print`, and vendored `Objects/mimalloc/`. None is in this slice.

Nothing further is owed to the classic class. The rest of the budget went to the
redirected remit.

---

## 3. Redirected remit — the GIL/no-GIL dual-build divergence audit

### 3.1 Full region enumeration (the denominator)

Produced by walking the `#if/#ifdef/#ifndef/#elif/#else/#endif` nesting with a stack
and keeping every conditional whose arms mention `Py_GIL_DISABLED`. Verdict column:
**OK** = the two arms discharge the same obligations; **NOTE** = a real asymmetry that
is nevertheless correct; **FIX/CONSIDER** = finding below.

#### `Objects/dictobject.c` — 44 regions

| lines | arms | what the FT arm does / what the GIL arm does | verdict |
|---|---|---|---|
| 163–299 | 2 | the whole lock/atomic macro vocabulary vs. its no-op twin; `split_keys_entry_added`, `set_keys`, `set_values`, `load_keys_nentries` defined twice | NOTE — see 3.4 (a) and (b) |
| 650–652 | 1 | `{0}, /* dk_mutex */` in `empty_keys_struct` | OK |
| 837–839 | 1 | `dk->dk_mutex = (PyMutex){0}` in `init_keys_object` | OK |
| 878–880, 883–885, 887–892 | 1 | `free_keys_object` QSBR-delayed free | OK |
| 932–937 | 1 | `free_values` QSBR-delayed free | OK |
| 1264–1268 | 1 | forward decl of `unicodekeys_lookup_unicode_threadsafe` | OK |
| 1277–1289 | 2 | `unicodekeys_lookup_split`: lock-free probe + `LOCK_KEYS` retry vs. plain probe. Both assign `ix`; neither can return `DKIX_KEY_CHANGED` to the caller | OK |
| 1372–1381 | 2 | `_Py_dict_lookup`: split keys routed through `unicodekeys_lookup_split` vs. plain `unicodekeys_lookup_unicode` | OK |
| 1424–1446 | 1 | `ensure_shared_on_read` + `_PyDict_EnsureSharedOnRead`. FT-only **definition**; the prototype (`pycore_dict.h:179`) and the only call site (`Python/specialize.c:672`) are both `#ifdef`-guarded — checked, links cleanly | OK |
| 1451–1465 | 1 | `ensure_shared_on_resize` body (empty function under GIL) | OK |
| 1472–1479 | 1 | `ensure_shared_on_keys_version_assignment` body | OK |
| 1482–1778 | 2 | the whole threadsafe-lookup family vs. two thin `_Py_dict_lookup` wrappers. FT returns a **new** ref, GIL returns borrowed-then-`Py_XNewRef`'d — both callers handle it (regions 2592, 5327) | OK |
| 1788–1811 | 1 | `_PyDict_GetMethodStackRef` FT fast path; GIL always falls through to the shared slow path | OK |
| 1847–1860 | 1 | `_PyDict_EnablePerThreadRefcounting` — entire body FT-only; a no-op function under the GIL. Prototype unguarded but the body is a stub, so `Objects/moduleobject.c:230` links | OK |
| 1866–1870 | 2 | `is_unusable_slot`: `ix >= 0 \|\| ix == DKIX_DUMMY` vs. `ix >= 0` — FT refuses to reuse `DKIX_DUMMY` index slots, GIL reuses them | NOTE — probed, no Python-visible difference (3.3) |
| **1949–1960 + 1983–1985** | 1 | `insert_split_key`: FT takes `types.mutex` then `LOCK_KEYS`; under the GIL `LOCK_KEYS` expands to **nothing** and there is no barrier at all around the `_PyType_Modified_Unlocked` call | **FIX — finding B** |
| 2451–2456, 2537–2542, 2669–2674 | 2 | `_Py_dict_lookup_threadsafe` + `Py_XDECREF` vs. `_Py_dict_lookup` — both end up returning a borrowed ref, as the legacy API requires | OK |
| 2578–2582 / 2592–2596 | 2 | `*result = value` (FT already owns) vs. `*result = Py_NewRef(value)` | OK |
| 5319–5323 / 5327–5329 | 2 / 1 | `_PyDict_Contains_KnownHash`: FT's extra `Py_DECREF(value)` is exactly the ref FT acquired. On `DKIX_ERROR` FT's `value` is provably NULL, so the early `return -1` leaks nothing | OK |
| 5703–5778 | 2 | forward decl of `dictiter_iternext_threadsafe` vs. the body of `dictiter_iternextkey_lock_held` | see finding A |
| **5790–5796** | 2 | `dictiter_iternextkey` dispatch | **FIX — finding A** |
| 5834–5901 | 1 (`#ifndef`) | `dictiter_iternextvalue_lock_held`, GIL-only | see finding A |
| **5913–5919** | 2 | `dictiter_iternextvalue` dispatch | **FIX — finding A** |
| **6033–6169** | 1 | `acquire_key_value` + `dictiter_iternext_threadsafe`, FT-only | **FIX — finding A (the site)** |
| **6191–6196** | 2 | `dictiter_iternextitem` dispatch — `#ifdef` split *inside* an `if (` condition; both arms test `== 0` | **FIX — finding A** |
| 7314–7327 | 2 | `_PyObject_InitInlineValues` `dk_usable--`: CAS-under-`LOCK_KEYS` vs. plain decrement. Same post-condition | OK |
| 7388–7394 / 7397–7399 | 1 | `_PyObject_MaterializeManagedDict` double-checked locking + FT-only `exit:` label | OK |
| 7569–7598 | 2 | `_PyObject_StoreInstanceAttribute`: FT re-checks for a raced materialisation. `int res` is written iff `dict == NULL` and read iff `dict == NULL`, with `dict` unmodified between — always initialised | OK (fragile, see 3.5) |
| 7651–7710 | 2 | `_PyObject_TryGetInstanceAttribute`: FT try-incref then two lock retries vs. a bare `Py_XNewRef` | OK |
| 7796–7858 | 1 | `try_set_dict_inline_only_or_other_dict` + `replace_dict_probably_inline_materialized`, FT-only | OK |
| 7883–7934 | 2 | `_PyObject_SetManagedDict`. **Both** arms test the failure: FT `if (err != 0) return err;`, GIL `if (_PyDict_DetachFromObject(...) == 0)` / `return -1`. The GIL arm's unconditional `_PyDict_DetachFromObject` is equivalent to FT's `cur_dict == inline_dict` guard because `detach_dict_from_object` (`:7953`) early-returns 0 when `ma_values != inline values` | OK — **explicitly checked for the CPY-0099 shape; not present** |
| 8049–8056 / 8061–8064 | 1 | `ensure_managed_dict` double-check + `done:` label | OK |
| 8077–8083 / 8093–8096 | 1 | `ensure_nonmanaged_dict` double-check + `done:` label | OK |
| 8149–8164 | 2 | `get_next_dict_keys_version`: CAS loop vs. `v = next_keys_version++`. Both return the **old** value and both bail on 0 | OK |

#### `Objects/setobject.c` — 7 regions

| lines | arms | FT / GIL | verdict |
|---|---|---|---|
| 75–135 | 2 | `SET_IS_SHARED`/`SET_MARK_SHARED` + `ensure_shared_on_read` + `set_compare_threadsafe` vs. `SET_IS_SHARED(so) 0` / empty | OK |
| 198–206 | 2 | `set_zero_table`: per-entry atomic stores vs. `memset`. Same post-state | OK |
| 435–465 | 1 | `set_lookkey_threadsafe`, FT-only | NOTE — 3.4 (c) |
| 469–474 | 1 | `free_entries` QSBR-delayed free | OK |
| 568–573 | 2 | `set_contains_entry`: `set_lookkey_threadsafe` vs. `set_lookkey`. Both return `FOUND(1)/NO_MATCH(0)/ERROR(-1)`; the FT arm funnels `CHANGED` back into `set_lookkey`, so the caller-visible contract is identical | OK |
| 1489–1498 | 1 | `copy_small_table`, FT-only | OK |
| 1542–1548 | 2 | `set_swap_bodies`: two `memcpy`s vs. two `copy_small_table`s over the same `PySet_MINSIZE` entries | OK |

### 3.2 Findings

---

#### [FIX] A — `dictiter_iternext_threadsafe` leaks key+value on the "keys changed" path; the GIL twin does not

**Site:** `/home/danzin/projects/cpython/Objects/dictobject.c:6145` in
`dictiter_iternext_threadsafe` (region 6033–6169), reached via
`dictiter_iternextkey` (`:5791`), `dictiter_iternextvalue` (`:5914`),
`dictiter_iternextitem` (`:6192`).
**Class:** the CPY-0099 family — an obligation that is discharged in one arm of a
`Py_GIL_DISABLED` split and silently dropped in the other.

**Mechanism.** The two arms do the same two things in the **opposite order**.

The FT arm increfs first and checks second:

```c
        if (acquire_key_value(&entry_ptr->me_key, value,
                               &entry_ptr->me_value, out_key, out_value) < 0) {   /* :6120 */
            goto try_locked;                     /* acquire_key_value INCREF'd *out_key/*out_value */
        }
    }
    // We found an element (key), but did not expect it
    Py_ssize_t len;
    if ((len = _Py_atomic_load_ssize_relaxed(&di->len)) == 0) {
        goto concurrent_modification;            /* :6146 */
    }
    ...
concurrent_modification:
    PyErr_SetString(PyExc_RuntimeError,
                    "dictionary keys changed during iteration");   /* :6154 */
fail:
    di->di_dict = NULL;
    Py_DECREF(d);
    return -1;                                   /* :6160  -- out_key/out_value never released */
```

The GIL arm checks first and increfs second:

```c
    // We found an element, but did not expect it
    if (di->len == 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "dictionary keys changed during iteration");
        goto fail;                               /* :6015 -- nothing has been INCREF'd yet */
    }
    di->di_pos = i+1;
    di->len--;
    if (out_key != NULL) {
        *out_key = Py_NewRef(key);               /* :6020 */
    }
    if (out_value != NULL) {
        *out_value = Py_NewRef(value);           /* :6023 */
    }
    return 0;
```

`acquire_key_value` (`:6038`, whose own comment promises *"If either one is
unsuccessful nothing is incref'd"*) has already taken one strong reference per
non-NULL out-parameter by the time `di->len` is tested. On the
`concurrent_modification` path those references are abandoned. Every caller treats
`< 0` / `!= 0` as "no value produced" and never touches the out-parameters, so
nothing downstream can release them either. The **behaviour** is identical on both
builds — same exception, same message — which is precisely why this is invisible
without reading the two arms side by side.

**Guarded twin (lesson 3).** `dictiter_iternextitem_lock_held`
(`Objects/dictobject.c:5957`), the GIL arm's implementation and also the function the
FT arm falls back to at `try_locked:` (`:6164`). Its guard is the **statement
ordering** — `di->len == 0` is tested at `:6012`, thirteen lines *above* the
`Py_NewRef` pair at `:6020`/`:6023`. That ordering defends against exactly this
threat (an abandoned reference on the "keys changed" exit) and nothing else; it is
not a lock and says nothing about concurrency. The FT arm inverted it, presumably
because `acquire_key_value` must run while the entry pointer is still believed valid.

**Reachability.** Pure single-threaded Python. `di->len` reaches 0 while an entry is
still visible whenever a mutation restores `ma_used` (defeating the `di_used` size
check at `:6073`) while growing `dk_nentries`: `next(it)`, then
`del d[k_old]; d[k_new] = v`, then two more `next(it)`.

**Reproduction.** `reports/obj-mappings/repro/ft_dictiter_len0_refleak.py` (weakref
based; exit 1 = leak).

| build | result |
|---|---|
| `debug-gil-nojit` | clean **10/10** |
| `release-gil-nojit` | clean **10/10** |
| `debug-ft-nojit` | **leaked 10/10** — 4 objects per run (items: key+value, keys: key, values: value) |
| `release-ft-nojit` | **leaked 10/10** — 4 objects per run |

Deterministic, no threads, no timing dependence.

**Fix.** Move the `di->len == 0` test above the `acquire_key_value` calls (matching
the GIL twin's ordering), or release the acquired references on the
`concurrent_modification` path before `goto fail`.

---

#### [FIX] B — `insert_split_key`'s re-entrancy window: `LOCK_KEYS` is a no-op in the `#else` arm, so the GIL build corrupts memory where the FT build hangs

**Site:** `/home/danzin/projects/cpython/Objects/dictobject.c:1942` `insert_split_key`,
regions 1949–1960 and 1983–1985.
**Status:** this is **CPY-0096** — *confirmed, not re-litigated*. Recorded as
`kind: deadlock`, `status: reproduced`, 24/24 FT hang. What follows is the pickup the
brief assigned to this slice ("its three further consequences reproduce on the default
GIL build and were deliberately left for you"), framed through my remit: this is
remit bullet 3 — *a lock acquisition that is a no-op under the GIL, where the code
afterwards relies on mutual exclusion*.

**Mechanism (the dual-build part).**

```c
#ifdef Py_GIL_DISABLED
    ix = unicodekeys_lookup_unicode_threadsafe(keys, key, hash);      /* :1950 */
    if (ix >= 0) { return ix; }
    Py_BEGIN_CRITICAL_SECTION_MUTEX(&_PyInterpreterState_GET()->types.mutex);  /* :1959 */
#endif
    LOCK_KEYS(keys);                                                  /* :1962 -- NOTHING under the GIL */
    ix = unicodekeys_lookup_unicode(keys, key, hash);
    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {                    /* :1964  READ dk_usable */
        ...
        if (type) { _PyType_Modified_Unlocked(type); }                /* :1971  RUNS ARBITRARY PYTHON */
        Py_ssize_t hashpos = find_empty_slot(keys, hash);
        ix = keys->dk_nentries;                                       /* :1974 */
        PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(keys)[ix];       /* :1976  WRITE at ix */
        STORE_SHARED_KEY(ep->me_key, Py_NewRef(key));                 /* :1977 */
        split_keys_entry_added(keys);                                 /* :1978  dk_usable-- */
    }
```

The `dk_usable > 0` test at `:1964` is separated from the write at `:1976`–`:1978`
by a call that runs arbitrary Python. Under the GIL, `LOCK_KEYS`/`UNLOCK_KEYS` expand
to nothing (`:257`/`:258`) and `Py_BEGIN_CRITICAL_SECTION_MUTEX` is not compiled at
all, so **nothing whatsoever** stands between the stale read and the write. Under FT
the keys mutex *is* taken — and because it is `_Py_LOCK_DONT_DETACH` and
non-reentrant, the same input deadlocks instead. Neither arm is defended; only the
failure mode differs. The returned `ix` then flows straight into
`store_instance_attr_lock_held` (`:7455` → `:7497` → `:7513`) as an index into the
instance's inline-values array.

**Guarded twin, and what its guard actually addresses (lesson 3).** The comment at
`:218-227` — *"We are not allowed to acquire other locks within LOCK_KEYS()… PyType_Modified()
must not be called within LOCK_KEYS() since it acquires the type lock"* — and the
`types.mutex` acquisition at `:1959` that implements it. That guard addresses
**lock-order inversion between two threads**. It says nothing about **re-entrancy on
this thread**, which is the mechanism here, and it is compiled out entirely in the arm
where the corruption actually lands. Citing `:1959` as "the fix" would be exactly the
mistake lesson 3 warns about.

**Why a single re-entry is silent on release.** `_PyDict_NewKeysForClass` (`:7258`)
allocates `USABLE_FRACTION(1 << NEXT_LOG2_SHARED_KEYS_MAX_SIZE)` = **42** entries
(`:7262`, `:7267`) but initialises the object with `SHARED_KEYS_MAX_SIZE` = **30**
(`:7275`). The 12-entry slack absorbs a single out-of-budget write, so ASan stays
quiet and only the debug assertion fires. Reaching the end of the *allocation* needs
> 42 nested frames, all of which pass the `:1964` test before any of them runs
`split_keys_entry_added`. That is why the two reproducers below differ.

**Reproduction.** Path to arbitrary Python inside `_PyType_Modified_Unlocked`: a type
watcher callback that returns −1 makes CPython call `PyErr_FormatUnraisable`
(`Objects/typeobject.c:1223`), which dispatches to `sys.unraisablehook` — a plain
Python callable. `_testcapi.add_type_watcher(1)` installs such a callback;
`_testinternalcapi.type_assign_specific_version_unsafe` re-arms it each level (needed
because `_PyType_Modified_Unlocked` early-returns once `tp_version_tag == 0`,
`typeobject.c:1189`).

*`repro/gil_arm_insert_split_key_reentry.py`* — single re-entry:

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT 5/5** — `Objects/dictobject.c:719: _PyDict_CheckConsistency: Assertion failed: 0 <= dk_usable && dk_usable <= usable`, exit 134 |
| `debug-gil-nojit-asan` | same assertion, exit 134 |
| `release-gil-nojit` | exit 0 **0/5** — silent; the write lands in the 42-vs-30 slack |
| `release-gil-nojit-asan` (`PYTHONMALLOC=malloc`) | exit 0 — same reason |

*`repro/gil_arm_insert_split_key_nested.py`* — 60 nested frames:

| build | result |
|---|---|
| `release-gil-nojit` | **SIGSEGV 5/5** (exit 139) |
| `debug-gil-nojit` | **SIGSEGV 5/5** (exit 139) |
| `release-gil-nojit-asan` (`PYTHONMALLOC=malloc`) | **heap-buffer-overflow**, `SUMMARY: AddressSanitizer: heap-buffer-overflow Objects/dictobject.c:7497 in store_instance_attr_lock_held`; `0x…ed8 is located 0 bytes after 72-byte region`, allocated by `_PyObject_MallocWithType`. Stack: `store_instance_attr_lock_held:7497` ← `_PyObject_GenericSetAttrWithDict` … ← `format_unraisable_v` ← `PyErr_FormatUnraisable` ← `_PyType_Modified_Unlocked:1223` ← `insert_split_key:1971` ← `store_instance_attr_lock_held:7455` |
| `release-ft-nojit` | **hang 3/3** (timeout 30 s, exit 124) — CPY-0096's documented deadlock |

All three inherited consequences are now anchored: **dk_usable invariant break 5/5**,
**ASan heap-buffer-overflow**, **unbounded recursion → SIGSEGV 5/5**, with the FT arm
hanging on the identical input. The overflow is a **read** of the instance's inline
values at `:7497` (`values->values[ix]`) driven by an `ix` that `insert_split_key`
returned past the array's capacity; the corresponding write at `:7513` uses the same
index.

**Fix direction.** Re-validate `keys->dk_usable > 0` (and re-derive `ix`) *after*
`_PyType_Modified_Unlocked` returns, in both arms — or hoist the
`_PyType_Modified_Unlocked` call out of the `LOCK_KEYS` region entirely, which is what
`:218-227` already demands and which would fix the FT deadlock at the same time.

---

### 3.3 Python-visible behaviour: probed, and clean

Remit bullet 4 asks for *different Python-visible behaviour between the two builds*.
I wrote `repro/dual_build_differential.py` — **18 probes**, each aimed at a code path
these two files implement differently across the split — and diffed the output across
**four** builds.

Probes: `dict` key/item/value iterator × mutation-during-iteration; `di_used`
size-change detection; error stickiness after the first `RuntimeError`;
`__length_hint__` and `__reduce__` after the error; split-table insertion order;
split-dict iteration under mutation; shared-keys exhaustion past
`SHARED_KEYS_MAX_SIZE`; `DKIX_DUMMY` slot reuse (the `is_unusable_slot` asymmetry at
`:1866`); dict lookup whose `__eq__` clears the dict; dict lookup whose `__eq__`
raises; `set` iteration under mutation; `set.__contains__` whose `__eq__` clears the
set; `frozenset.__contains__` (the FT-only `PyFrozenSet_CheckExact` fast path at
`setobject.c:441`); `set_swap_bodies` via `^=`; `frozenset` hash stability.

```
diff release-gil-nojit release-ft-nojit  -> identical
diff debug-gil-nojit   debug-ft-nojit    -> identical
```

**Result: 0 Python-visible divergences out of 18 probes across 4 builds.** Notably,
`dict.iter_mutate_keys/items/values` *do* land on the exact
`"dictionary keys changed during iteration"` path that finding A leaks on, and produce
byte-identical output — the leak is the only observable difference, which is what makes
it worth reporting.

This is a genuine clean negative with a real denominator, not silence.

### 3.4 Asymmetries examined and deliberately NOT reported

**(a) `split_keys_entry_added` writes its two fields in opposite orders.**
`:242-250` (FT) does `dk_nentries++` then `dk_usable--`, with the comment *"We increase
before we decrease so we never get too small of a value when we're racing with reads"*.
`:274-278` (GIL) does the reverse. The ordering is load-bearing only for a concurrent
lock-free reader, which cannot exist under the GIL. Correct. **ACCEPTABLE.**

**(b) `LOAD_INDEX` / `STORE_INDEX` carry a trailing `;` inside the FT macro body
(`:182`, `:183`) and not in the GIL body (`:271`, `:272`).** All eight call sites
(`:533`, `:536`, `:540`, `:544`, `:561`, `:565`, `:569`, `:574`) are in statement
position, so the FT expansion merely emits a null statement. It would break
compilation if either macro were ever used in an expression context, and only in one
arm. **POLICY** — latent, cost-free to fix, not a bug today.

**(c) `set_lookkey_threadsafe`'s `PyFrozenSet_CheckExact` fast path
(`setobject.c:441-448`) takes no lock and uses `set_compare_frozenset`, which cannot
return `SET_LOOKKEY_CHANGED`.** It assumes frozensets are immutable after
construction. Whether `set_swap_bodies` can violate that is a **ft-race-scanner**
question, not a dual-build divergence — both arms make the same immutability
assumption. Probed behaviourally in 3.3 (`set.frozenset_contains_during_eq`): identical
on all four builds. **Noted, not claimed.**

**(d) 61 FT-only lock assertions compile to nothing under the GIL.** This is remit
bullet 2 and the answer is: every one of them asserts *a lock is held*, and under the
GIL there is no lock to hold. `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` is defined
empty in the GIL arm (`pycore_critical_section.h:71`, `:80`). I found **no** FT-only
assertion of a *data* invariant. The one that comes closest is
`can_modify_dict` (`:301-323`, `#ifndef NDEBUG`): on a debug FT build it enforces
"locked or uniquely-referenced"; on a debug GIL build the non-frozendict branch
reduces to `assert(1)`. Its frozendict branch — `assert(!_PyObject_GC_IS_TRACKED(mp))`
and `assert(PyUnstable_Object_IsUniquelyReferenced(mp))` — is a real data invariant and
**is** live in both arms. **ACCEPTABLE.**

**(e) Split-iterator bound: FT reads `values->size` (`:6091`), the GIL twin reads
`d->ma_used` (`:5979`).** Traced: `_PyDict_InsertSplitValue` (`:1998-2001`) increments
`values->size` (via `_PyDictValues_AddToInsertionOrder`) and `ma_used` in lock-step,
and `delete_index_from_values` (`:2937-2951`) / `:2971` decrement them in lock-step.
The two bounds are equal by construction. **Checked and clean.**

### 3.5 Style note, no finding

`_PyObject_StoreInstanceAttribute` (`:7582-7593`) declares `int res;` uninitialised,
writes it under `if (dict == NULL)` inside the critical section and reads it under an
identical `if (dict == NULL)` after. `dict` is not modified between, so `res` is always
initialised when read. Correct, but it depends on two textually separate conditions
staying in sync and would be a live uninitialised read the moment either changes.
**POLICY**, FT arm only.

---

## 4. Classes bounded (clean, with denominators)

| class | denominator | result |
|---|---|---|
| `Py_BEGIN/END_ALLOW_THREADS` balance | 0 occurrences / 404 functions | **structurally absent** |
| `PyGILState_Ensure`/`Release` balance | 0 occurrences | **structurally absent** |
| Python C-API call in a GIL-released region | 0 GIL-released regions exist | **structurally absent** |
| Blocking call with the GIL held | 0 in slice (16 in `Objects/`, all ACCEPTABLE classes, none here) | **clean** |
| CPY-0099 shape (status tested in one arm, discarded in the other) | 24 two-arm regions, all read | **1 found — finding A** (an *obligation*, not a status value); the closest true status-value candidate, `_PyObject_SetManagedDict` (7883–7934), tests `err` in both arms |
| FT-only assertion of a **data** invariant | 61 FT-only assertion sites | **0 found** — all 61 are lock assertions |
| GIL-no-op lock with post-lock reliance on mutual exclusion | 3 `LOCK_KEYS` regions (`:1317`, `:1962`, `:7317`) | **1 found — finding B** (`:1962`). `:1317` `_PyDictKeys_StringLookupAndVersion` and `:7317` `_PyObject_InitInlineValues` run no Python between lock and unlock — clean |
| Python-visible GIL-vs-FT behaviour difference | 18 probes × 4 builds | **0 found** |
| Linkage: FT-only definition with an unguarded prototype/call site | 2 candidates (`_PyDict_EnsureSharedOnRead`, `_PyDict_EnablePerThreadRefcounting`) | **0 found** — both correctly guarded / stubbed |

Previously-recorded findings in these files, confirmed in one line each and not
re-litigated: **CPY-0015** (`dictiter_new:5646`), **CPY-0019**
(`frozendict_pair_hash:8427`), **CPY-0079** (`copy_lock_held_untracked:4494`),
**CPY-0096** (`insert_split_key:1971` — picked up per the brief, see finding B),
**CPY-0107** (`_Py_dict_lookup:1385`, `compare_unicode_generic` under `LOCK_KEYS` —
still present at `:1499`, the *third* `LOCK_KEYS` reliance and out of my remit only
because it is already recorded), **CPY-0115** (`delitem_common:2987`).

## 5. Toolkit feedback

**Recall gap (the high-value output).** `scan_gil_usage` returned **0 findings on a
0 denominator** for this slice and was, correctly, honest about it
(`rule_not_applicable: true` — that flag did its job and is worth keeping). But both
findings in this report came from **reading `Py_GIL_DISABLED` regions**, a construct
no scanner in the toolkit enumerates. `scan_gil_usage` models the *old* GIL vocabulary
(`Py_BEGIN_ALLOW_THREADS`, `PyGILState_*`), which lives almost entirely in `Modules/`;
the *modern* GIL question in `Objects/` is dual-build arm divergence, and there is no
rule for it.

**Concrete proposal — a new rule, `ft_arm_divergence`, in `scan_gil_usage.py`
(or a new `scan_dual_build.py`):**

1. Walk `#if/#ifdef/#ifndef/#elif/#else/#endif` with a stack; keep every conditional
   whose arms mention `Py_GIL_DISABLED`. Emit the count as a **denominator** even
   when there are no findings — that alone would have turned this slice's "0/0" into
   "0 findings / 51 regions", which is the difference between silence and safety.
   *(My enumerator is ~60 lines; happy to contribute it.)*
2. `divergent_refcount_obligation` (**finding A's shape**): within a two-arm region
   or a pair of `*_threadsafe` / `*_lock_held` twins, flag when one arm's
   incref-producing call (`Py_NewRef`, `Py_XNewRef`, `_Py_TryIncrefCompare`,
   `_Py_TryXGetRef`, `acquire_*`) **precedes** a guard/`goto`-to-error, while the
   twin's equivalent guard **precedes** the increfs. Statement-order inversion across
   a `#ifdef` split is mechanically checkable and is exactly what produced A.
3. `noop_lock_with_python_call` (**finding B's shape**): flag a lock macro that has an
   empty definition in the `#else` arm (`LOCK_KEYS`, `UNLOCK_KEYS` — discoverable by
   parsing the region at `dictobject.c:163-299` itself) whose region contains a call
   that can run Python. This overlaps `lock-discipline-checker`, but that scanner
   reported **0/0** on this slice (`scan_lock_discipline` fired 0 in `Objects/` *and*
   0 here) — because it looks for `Py_BEGIN_CRITICAL_SECTION`, not for file-local lock
   macros. Feeding it the file's own macro vocabulary would have surfaced `:1962`.
4. `divergent_status_discard` (the literal CPY-0099 shape): in a two-arm region, flag
   when the same callee's result is bound to a variable in one arm and used as a bare
   statement in the other. Zero hits here, but it is the cheapest rule of the four and
   it is the one that caught CPY-0099.

**Precision note.** `scan_gil_usage`'s 16 `Objects/`-wide findings are 16/16 in the
ACCEPTABLE classes the agent prompt already enumerates (crash-dump paths, one-shot
init probes, vendored mimalloc). The agent prompt is doing the suppression work the
scanner could do itself; hard-coding `Objects/mimalloc/**`, `PyObject_Dump`,
`_PyObject_AssertFailed`, `_PyObject_DebugDumpAddress` and
`_pymalloc_system_hugepage_size` as scanner-side suppressions would take `Objects/`
from 16 findings to 0 with no loss.

**Small correction to the run context.** RUN_CONTEXT's table lists `scan_gil_usage`
as "16" under `Objects/` next to "0" for the slice. Those 16 are **findings**, not
vocabulary occurrences; the `Objects/`-wide vocabulary is 8 resolved tokens. A reader
comparing the columns would infer the slice has a vocabulary the scanner missed. The
distinction matters for exactly the reason lesson 2 exists.

## 6. Noticed outside slice

- `Objects/typeobject.c:1223` `PyErr_FormatUnraisable` inside `_PyType_Modified_Unlocked`
  is a general-purpose "run arbitrary Python from anywhere that modifies a type" gadget,
  reachable from pure Python via `_testcapi.add_type_watcher(1)` + `sys.unraisablehook`;
  finding B is one consumer of it (recorded as CPY-0084/CPY-0098's site).
- `Objects/dictobject.c:7262-7275` `_PyDict_NewKeysForClass` allocates
  `USABLE_FRACTION(64) = 42` entries but sets `dk_usable = SHARED_KEYS_MAX_SIZE = 30`
  — 12 entries of permanent slack per heap type. In-slice but not a defect; it is the
  reason a single re-entry in finding B is silent on release builds, and worth stating
  so nobody mistakes that silence for safety.
- `Python/specialize.c:672` is the sole caller of the FT-only
  `_PyDict_EnsureSharedOnRead`; correctly `#ifdef`-guarded. Verified only because the
  divergence audit required it.

## 7. Artifacts

All under `/home/danzin/projects/cpython-review-toolkit/reports/obj-mappings/repro/`:

- `ft_dictiter_len0_refleak.py` — finding A. Exit 1 = leak.
- `gil_arm_insert_split_key_reentry.py` — finding B, single re-entry (`dk_usable` assertion).
- `gil_arm_insert_split_key_nested.py` — finding B, 60 nested frames (SIGSEGV / ASan).
- `dual_build_differential.py` — the 18-probe Python-visible behaviour differential.
