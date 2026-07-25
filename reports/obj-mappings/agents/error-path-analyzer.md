# error-path-analyzer — obj-mappings slice

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777`
Scope: `Objects/dictobject.c` (8,597 lines), `Objects/setobject.c` (3,228 lines) — nothing else.

**Build/target drift check (RUN_CONTEXT requirement):**
`git diff a1d580430c8 4f3be1b5777 -- Objects/dictobject.c Objects/setobject.c` is **empty**.
The matrix builds (`a1d580430c8`) and the target ref are byte-identical for both slice files, so
every reproduction below is against the reviewed source.

---

## 1. Denominator line

| quantity | value |
|---|---|
| `scan_error_paths` slice population (`scanners/scan_error_paths.sample.json`) | **4** (`alloc_null_no_memerror` 2, `unconditional_pyerr_clear` 2) |
| scanner candidates triaged | 4 / 4 |
| scanner candidates **confirmed** | **0** (all 4 ACCEPTABLE — see §4, precision 0/4) |
| scanner denominators carried in the sample | `functions_analyzed` 312, `fallible_assignment_sites` 36, `int_status_callees_resolved` 112 |
| structural zeros in the sample | `missing_null_checks` 0, `unchecked_returns` 0, `pylong_sentinel_no_errcheck` 0, `unchecked_parse_calls` 0, `int_status_never_tested` 0 |
| lines read by hand | ≈ **3,900 / 11,825** (~33%), concentrated on the allocate/insert/resize/teardown spine of both files |
| OOM-sweep allocation-failure points driven | **48** (CPY-0079 payload) + **152** (broad dict/set payload) on `debug-gil-nojit`; 129–136 on the other three builds |
| findings from **reading** | 3 of 4 (1 FIX, 1 FIX, 1 CONSIDER) |
| findings from **scanner output** | 0 |

All three new findings and the CPY-0079 upgrade came from reading + dynamic probing.
The scanner's net-new contribution on this slice is **zero** — consistent with lesson 1.

---

## 2. Findings

### [FIX] `set_add_entry_takeref` inserts before resizing: a failed `set_table_resize` can leave the table with no virgin slot → uninterruptible infinite loop

* **Site:** `Objects/setobject.c:319-326` (`set_add_entry_takeref`, label `found_unused:`)
* **Supporting sites:** `:527-531` (`set_table_resize`, the `PyMem_NEW` failure branch);
  `:230-246` (`set_do_lookup`'s `while (1)`); `:271-306` (`set_add_entry_takeref`'s own `while (1)`)
* **Class:** error path leaves the object in a state that violates a documented invariant

**Mechanism.** `found_unused:` commits the insertion *first* — `so->fill++`, `so->used++`,
`entry->hash = hash`, `entry->key = key` — and only then evaluates the load factor and calls
`set_table_resize()`. If that resize's `PyMem_NEW(setentry, newsize)` fails, `set_table_resize`
correctly raises `MemoryError` and returns −1 **without mutating the set**, but the element is
already in. `set.add()` therefore reports failure while having succeeded, and `so->fill` has
advanced past the point the resize existed to prevent. The next `add()` re-evaluates
`(size_t)so->fill*5 < mask*3`, fails the resize again, and inserts again. Four consecutive
failures drive `fill` from 5 to `mask+1 == 8` on the `smalltable`, at which point there is no
`key == NULL` entry left and both `while (1)` probing loops — in `set_do_lookup` and in
`set_add_entry_takeref` itself — never terminate.

The file states this invariant explicitly, 200 lines above the defect, at
`Objects/setobject.c:517-520`:

```
   Subtle:  This is *necessary* if fill==size,
   as set_lookkey needs at least one virgin slot to
   terminate failing searches.
```

**Guarded twin — and what its guard actually defends against (lesson 3).**
`Objects/dictobject.c:1910-1915`, `insert_combined_dict`:

```c
    if (mp->ma_keys->dk_usable <= 0) {
        /* Need to resize. */
        if (insertion_resize(mp, 1) < 0) {
            return -1;
        }
    }
    /* ... only now is the entry written ... */
```

dict tests capacity and resizes **before** writing, so a failed `dictresize` returns −1 with the
dict byte-for-byte unchanged (`insertdict:2051-2053` → `Fail:` → DECREF both, return −1).
**Honest caveat:** dict's ordering is not a deliberate defence against this failure mode — it is
structural, because `dk_usable` *is* the capacity counter and there is nowhere to put the entry
until the table grows. It nevertheless produces exactly the right behaviour, and it is the
shape the fix should take. The set's *own* nearest guard, `set_insert_clean` (`:375-408`, whose
comment cites issue 1456209), defends against a **re-entrant callback during a resize** — a
completely different threat model. It says nothing about a resize that never happens.

**Reproduction.** `repro/set_resize_oom_hang.py` (bounded window: `set_nomemory(0, 10)`, so by
the time the loop ends the allocator is healthy again — this rules out an allocation-famine
livelock).

| build | result |
|---|---|
| `debug-gil-nojit` | **HANG 6/6** (5/5 + 1/1), 100% CPU, process state `R` |
| `release-gil-nojit` | **HANG 4/4** |
| `debug-ft-nojit` | **HANG 4/4** |
| `release-ft-nojit` | **HANG 4/4** |

`repro/set_resize_oom_hang_trace.py` localises it exactly (unbuffered `os.write` markers,
everything pre-built before arming):

```
i=00 i=01 i=02 i=03 i=04 M i=05 M i=06 M i=07 M i=08 <hangs>
```

i=04..07 are the four `MemoryError`s (fill 5→8 on an 8-slot table); the hang is inside the
**ninth `set.add()` itself**, not at a later membership test. The GIL is held with no bytecode
boundary, so the hang is not `Ctrl-C`-interruptible.
`repro/set_resize_probe.py` is the dense scan that located the window (widths 2/3/4/6 → 1/2/2/3
`MemoryError`s and no hang; width ≥ 10 → hang).

**Reachability / severity.** OOM-window only — `set_table_resize` has exactly one failure mode
and `PyMem_NEW`'s own overflow guard needs ~2^59 elements, so this is unreachable without memory
pressure. But the *consequence* is qualitatively worse than the `MemoryError` it replaces: an
uninterruptible spin rather than a raisable exception, and the set is left permanently unusable
even if memory later becomes available.

**Fix.** Move the load-factor test above the insertion, mirroring `insert_combined_dict`:
check `(size_t)(so->fill + 1)*5 >= mask*3` (or simply `so->fill == mask`) *before* writing the
entry and return −1 from `set_table_resize`'s failure without having inserted. A minimal
alternative that preserves the invariant is to refuse the insert whenever it would consume the
last virgin slot.

**Prior art:** none found. `gh api -X GET search/issues -f q='repo:python/cpython set_table_resize MemoryError'`
and `'... set add infinite loop resize failure'` return nothing relevant.

---

### [FIX] `set.remove()` / `set.discard()` clear a `TypeError` raised by a colliding element's `__eq__`

* **Sites:** `Objects/setobject.c:2656-2660` (`set_remove_impl`), `Objects/setobject.c:2696-2700`
  (`set_discard_impl`)
* **Class:** exception clobbering — an unnarrowed-*enough* `PyErr_Clear()` after a call that runs
  user Python

**Mechanism.**

```c
    rv = set_discard_key(so, key);
    if (rv < 0) {
        if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError))
            return NULL;
        PyErr_Clear();
        Py_hash_t hash;
        Py_BEGIN_CRITICAL_SECTION(key);
        hash = frozenset_hash_impl(key);
        Py_END_CRITICAL_SECTION();
        rv = set_discard_entry(so, key, hash);
```

The narrowing is written for one failure — "`PyObject_Hash(key)` said the key is an unhashable
set, so hash it as a frozenset instead". But `set_discard_key` (`:622-631`) is a *compound*:
`PyObject_Hash` **then** `set_discard_entry` → `set_lookkey` → `set_do_lookup` →
`PyObject_RichCompareBool` (`:185`). It returns −1 for either. `PyErr_ExceptionMatches(PyExc_TypeError)`
cannot tell "this key is unhashable" from "a *different* object's `__eq__` raised TypeError while
probing", so a user exception that has nothing to do with hashability is discarded.

Reaching the comparison needs a **set subclass** that supplies a `__hash__` (`PySet_Check`
accepts subclasses) colliding with an element whose `__eq__` raises.

**Guarded twins — in the same file, and their guard is the right one.**
`_PySet_Contains:2559-2565` and `frozenset___contains___impl:2619-2625` call
`PyObject_Hash(key)` **themselves** and enter the clear window only when *that* call returned −1:

```c
    Py_hash_t hash = PyObject_Hash(key);
    if (hash == -1) {
        if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError)) { ... }
        PyErr_Clear();
```

No comparison has run at that point, so the only exception in flight is the hash's. That is
exactly the discriminator `set_remove_impl` / `set_discard_impl` lose by delegating to the
compound helper. The general house pattern is also present and correct one file over:
`dictobject.c:2450-2463` (`dict_getitem`) saves the exception with `_PyErr_GetRaisedException`,
reports anything non-`KeyError` through `PyErr_FormatUnraisable`, and restores.

**Reproduction.** `repro/set_remove_discard_eq_clobber.py` — **3/3 runs on `debug-gil-nojit`,
1/1 on `release-gil-nojit`, 1/1 on `debug-ft-nojit`**:

```
baseline: a direct comparison must raise the user TypeError
  `Boom() in s`: OK -- TypeError: boom from __eq__
set.remove / set.discard with a colliding set-subclass key
  s.remove(HSet()):  CLOBBERED -- got KeyError: HSet()
  s.discard(HSet()): CLOBBERED -- no exception at all (silent success)
```

`set.discard()` is the worse half: the user's `TypeError` vanishes with no `__context__`, no
unraisable report, and a successful return.

**Second tier (POLICY, same code).** A set subclass whose *own* `__hash__` raises `TypeError` is
also silently reinterpreted as "unhashable set": `HashRaises() in s` returns `False`,
`s.discard(HashRaises())` returns `None`, `s.remove(HashRaises())` raises `KeyError`. Arguably
the intended trade-off for `x in s` where `x` is a set — but it means a deliberate
`raise TypeError` from a user `__hash__` is unobservable. Worth a decision, not necessarily a
fix.

**Bound on the damage (checked, not assumed).** The `PyErr_ExceptionMatches(PyExc_TypeError)`
narrowing does hold: a `__hash__` raising `KeyboardInterrupt` propagates untouched
(`s.remove(HashRaisesKBI())` → `KeyboardInterrupt`). So this is *not* the
`unionobject.c:172` "swallows KeyboardInterrupt/MemoryError" class — it is bounded to
`TypeError` and its subclasses. That is why it is FIX and not a five-alarm FIX.

**Fix.** Split `set_discard_key` at the call site: hash the key in `set_remove_impl` /
`set_discard_impl` (as the two `__contains__` implementations already do), take the
frozenset-hash fallback only on a hash failure, and pass the resulting hash to
`set_discard_entry` so no comparison error can enter the clear window.

**Prior art:** none found (`gh api -X GET search/issues -f q='repo:python/cpython set.discard swallows TypeError __eq__'`,
`'... set remove PyErr_Clear frozenset_hash'`).

---

### [FIX] `assert(new_dict == NULL)` states a false invariant on the `_PyDict_DetachFromObject` failure path

* **Sites:** `Objects/dictobject.c:7932` (`_PyObject_SetManagedDict`, the `#else` / GIL arm) and
  `Objects/dictobject.c:7848` (`replace_dict_probably_inline_materialized`, the
  `#ifdef Py_GIL_DISABLED` arm) — **both arms carry the same wrong assertion**
* **Class:** failable `assert` on an allocation-failure path (the CPY-0079 shape, different site)

**Mechanism.**

```c
        if (_PyDict_DetachFromObject(dict, obj) == 0) {
            _PyObject_ManagedDictPointer(obj)->dict = (PyDictObject *)Py_XNewRef(new_dict);
            Py_DECREF(dict);
            return 0;
        }
        assert(new_dict == NULL);
        return -1;
```

`_PyDict_DetachFromObject` (`:8031`) → `detach_dict_from_object` (`:7954`) has exactly one
failure mode: `copy_values()` at `:7970` returns NULL and `:7972-7974` raises `MemoryError`.
Whether the *caller* passed a new dict is unrelated to whether that copy succeeded, so the
assertion asserts something the code does not establish. `obj.__dict__ = {...}` on an object
whose managed dict was materialised onto its inline values reaches it with `new_dict != NULL`.

The control flow itself is correct — `return -1` with `MemoryError` set, which the caller
propagates — so on a release build the behaviour is right. The assertion is the bug.

**Guarded twin.** `PyObject_ClearManagedDict:8010-8024`, the *other* consumer of the same
`detach_dict_from_object` failure, handles it properly: it reports through
`PyErr_FormatUnraisable("Exception ignored while clearing an object managed dict")` and then does
the manual teardown. Its guard addresses precisely this threat model — "the detach can fail under
OOM and there is nothing the caller can do about it" — which is why it is a real twin and not a
coincidence.

**Reproduction.** `repro/CPY-setmanageddict_assert.py` (standalone, `set_nomemory(0,1)`):

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT 10/10** — `Objects/dictobject.c:7932: int _PyObject_SetManagedDict(PyObject *, PyObject *): Assertion 'new_dict == NULL' failed.` |
| `debug-ft-nojit` | **SIGABRT** at the `:7848` twin (`replace_dict_probably_inline_materialized`), n=1 |
| `release-gil-nojit` | 0/10 — assertion compiled out, clean `MemoryError` |
| `release-ft-nojit` | 0 crashes over 129 allocation-failure points |

Also surfaced independently by the broad sweep (`repro/oom_dictset_*.json`) at n=96 on
`debug-gil-nojit` (152 points) and n=86 on `debug-ft-nojit` (129 points), from the payload line
`inst.__dict__ = {"a": 1}`.

**Fix.** Delete both assertions, or replace them with the true invariant
(`assert(PyErr_Occurred())`).

---

### [CONFIRMED — re-run, do not re-litigate] CPY-0079 upgraded from `static-confirmed` to **reproduced**, denominator **48**

`Objects/dictobject.c:4494` (`copy_lock_held_untracked`) — `assert(!_PyObject_GC_IS_TRACKED(d))`
on a `d` that neither arm of the preceding `if`/`else` NULL-checked.

The record's clean sweep had a denominator of **2** allocation-failure points, which D-17 calls
"too thin to certify". I rebuilt the payload (`repro/CPY-0079_setup.py` +
`repro/CPY-0079_payload.py`: 27 dict/set copy/construct operations across empty dict, empty
frozendict, combined, generic-keys, split-table, non-compact, frozendict, and the whole set copy
family, warmed three times unarmed).

| build | allocation-failure points | outcome |
|---|---|---|
| `debug-gil-nojit` | **48** | **REPRODUCED** — SIGSEGV at n=1 |
| `debug-ft-nojit` | **40** | **REPRODUCED** — SIGSEGV |
| `release-gil-nojit` | **46** | clean (assert compiled out; `return d` with `d == NULL` is correct) |

48 > the `THIN_EVIDENCE_POINTS = 20` floor, so the verdict is now certifiable — and it is
positive, not clean. `repro/CPY-0079_repro.py` reproduces standalone: **SIGSEGV 10/10** on
`debug-gil-nojit`, 0/4 on `release-gil-nojit`.

gdb confirms the record's stated mechanism exactly:

```
Program received signal SIGSEGV
0x000055555570556b in anydict_new_untracked (type=<optimized out>) at Objects/dictobject.c:5368
5368        d->_ma_watcher_tag = 0;
#1  dict_new_untracked (...) at Objects/dictobject.c:5382
#2  copy_lock_held_untracked (o=..., as_frozendict=0) at Objects/dictobject.c:4492
#3  PyDict_Copy (...) at Objects/dictobject.c:4599
rax = 0x0                  # the NULL allocation result
$1 = (void *) 0x10         # si_addr == offsetof(_ma_watcher_tag)
```

The fault is **inside the inlined callee at `:5368`, past its own `if (self == NULL) return NULL;`
at `:5362`** — i.e. the caller's UB-bearing `assert` licensed the optimizer to delete the
callee's NULL check, which is what the record predicted. Trigger: `{}.copy()`.

**Records to update:** `CPY-0079` `status` → `reproduced`; add the 48-point denominator, the
gdb frame, and `repro/CPY-0079_repro.py`.

---

### [CONFIRMED, one line each — already recorded]

* **CPY-0096** (`insert_split_key:1971` calls `_PyType_Modified_Unlocked` under the non-reentrant
  `LOCK_KEYS`) — still present at `:1962-1981`, unchanged. Reached from *inside this slice* by
  `_PyDict_NewKeysForClass:7285`, which calls `insert_split_key` in a loop.
* **CPY-0107** (`compare_unicode_generic` under `LOCK_KEYS`) — `_Py_dict_lookup:1385` still takes
  `LOCK_KEYS_IF_SPLIT` around `unicodekeys_lookup_generic`. Unchanged.
* **CPY-0015** (`dictiter_new:5646`) and **CPY-0019** (`frozendict_pair_hash:8427`) — sites intact.

---

## 3. Classes bounded (checked here, with the denominator)

| class | denominator examined | result |
|---|---|---|
| Raw allocators (`PyMem_*`/`PyObject_*Malloc`) in the slice | **5 sites**: `dictobject.c:860, 917, 1036, 7265`; `setobject.c:527` | 3 raise `PyErr_NoMemory()` in place (`:864`, `:1038`, `setobject:529`); `:917` (`new_values`) defers to both callers, **both verified** (`:1012` and `:4502` → `return PyErr_NoMemory()`, plus `:7973` via `copy_values`); `:7265` defers to its single tree-wide caller `typeobject.c:9482`, verified. **Clean, 5/5.** |
| `PyErr_Clear()` sites in the slice | **6 total** (`dictobject.c:1336`, `:7269`; `setobject.c:2565`, `:2625`, `:2660`, `:2700`) | 4 of 6 narrow with `PyErr_ExceptionMatches(PyExc_TypeError)`; 2 of those 4 (`:2660`, `:2700`) narrow on the **wrong** predicate — see finding 2. `dictobject.c:1336` and `:7269` are ACCEPTABLE (§4). |
| Half-built object freed on an error path (OOM-dominant class) | **6 constructors** read: `new_dict_impl:941`, `anydict_new_untracked:5355`, `insert_to_emptydict:2089`, `copy_lock_held_untracked`'s split arm `:4498-4529`, `make_new_set_untracked:1350`, `set_iter:1171` | **Clean.** `new_dict_impl` handles `mp == NULL` before touching a field; `make_new_set_untracked` writes all seven members before the first fallible call; `set_iter` writes all four before `_PyObject_GC_TRACK`; the split-copy arm has no fallible call between `PyObject_GC_New` and the last member write. Corroborated dynamically: 152 allocation-failure points over the broad dict/set payload produced **exactly one** crash, and it was the `:7932` assert, not a dealloc. |
| Resize family leaves a consistent state on failure | `dictresize:2192` (allocates at `:2220` and returns −1 before any mutation), all **3** `dictresize` callers, `set_table_resize:484` and all **4** call sites (`setobject.c:326`, `:850`, `:1204`, `:2038`) | dict: **clean**. set: 3 of 4 call sites resize *up front* and propagate −1 with the set untouched; the 4th (`:326`, in `set_add_entry_takeref`) is finding 1. |
| Allocation failure inside a lock / critical section | every allocation under `LOCK_KEYS` / `Py_BEGIN_CRITICAL_SECTION*` in both files | **Clean.** `dictresize` allocates *before* `LOCK_KEYS(oldkeys)` at `:2230`, and nothing inside that region is fallible (`Py_NewRef`, `build_indices_*`). `set_table_resize` allocates under the caller's critical section but the failure branch mutates nothing and the macro-scoped unwind is the caller's. No path returns out of a `Py_BEGIN_CRITICAL_SECTION` region without its `END`. |
| Fallible/Python-running call on an error path clobbering the in-flight exception | all **30** `PyErr_Set*` / `PyErr_Format` / `_PyErr_SetKeyError` sites in the slice | **Clean except finding 2.** The house ordering is correct everywhere checked: `dict_dict_merge:4285-4291` DECREFs key and value *before* `PyErr_SetString(RuntimeError, ...)`; `dict_popitem_impl:5026-5027` DECREFs the fresh empty tuple first. `set_unhashable_type:345` and `dict_unhashable_type:2477` both use `PyErr_GetRaisedException`/`SetRaisedException` and only reformat an *exact* `TypeError`. |
| `unchecked_return`, `missing_null_check`, `unchecked_parse`, `pylong_sentinel_no_errcheck`, `int_status_never_tested` | scanner reports **0** against 36 `fallible_assignment_sites` and 112 resolved int-status callees | **Evidential zero, not structural** — the rules fired on real populations in this slice and found nothing. Both files are Argument-Clinic-generated at the boundary, which explains `unchecked_parse` = 0 structurally. |

---

## 4. Scanner triage — precision 0/4 on this slice

| # | site | type | verdict | FP class |
|---|---|---|---|---|
| 1 | `dictobject.c:917` `new_values` | `alloc_null_no_memerror` | **ACCEPTABLE** | *Obligation deferred to the caller* — the taxonomy names `new_values` explicitly. Verified rather than assumed: both call sites (`new_dict_with_shared_keys:1010-1012` and `copy_values:4458` → `copy_lock_held_untracked:4500-4502` / `detach_dict_from_object:7970-7974`) call `PyErr_NoMemory()`. |
| 2 | `dictobject.c:1336` `_PyDictKeys_StringLookupSplit` | `unconditional_pyerr_clear` | **ACCEPTABLE** | *Statically-known type slot* — `PyUnicode_Type.tp_hash(key)` is a fixed C function on a `PyUnicode_CheckExact`-asserted key; no user code can run, and the call is in fact infallible, so the clear is dead code. This exact site is already the taxonomy's exemplar. |
| 3 | `dictobject.c:7265` `_PyDict_NewKeysForClass` | `alloc_null_no_memerror` | **ACCEPTABLE** | *Cross-file deferral* — grepped tree-wide: one caller, `typeobject.c:9480-9484`, which raises `PyErr_NoMemory()` on NULL. The function's own doc comment (`:7256-7257`) declares the sentinel-NULL convention. |
| 4 | `dictobject.c:7269` `_PyDict_NewKeysForClass` | `unconditional_pyerr_clear` | **ACCEPTABLE (POLICY note)** | Not in an existing FP class, but not a bug either: it sits on the `PyMem_Malloc == NULL` branch, and `PyMem_Malloc` sets nothing, so the clear can only destroy an exception that was *already* pending on entry. `type_ready_managed_dict` never calls it with one. The clear is unnecessary; if it ever did fire it would silently downgrade a real exception to the caller's `MemoryError`. One-line comment or deletion. |

**Precision: 0 confirmed / 4 candidates.** Two dismissals (#1, #2) were pre-suppressible from
the existing taxonomy; #3 needed a tree-wide grep; #4 is a new, narrow shape.

---

## 5. Toolkit feedback

### 5a. Recall gaps — the highest-value output

**Gap 1 (new rule proposal): `insert-before-capacity-check`.** The set hang is invisible to every
rule in the suite. The shape is mechanical and greppable: *a mutation that writes into a hash
table and bumps its fill counter, followed in the same basic block by a capacity/resize call whose
failure is returned to the caller.* Detection sketch — inside a function, find a statement
sequence `<field>++ ; … ; return <resize_fn>(...)` (or `if (cond) return 0; return <resize_fn>(...)`)
where `<resize_fn>` is a same-file `static int` whose body contains a raw allocator + `PyErr_NoMemory`.
The dict twin (`insert_combined_dict`) would be the negative control. Population in `Objects/`
should be small (hash-table types only: set, dict, odict, and the `_collections`/`_json` caches),
so precision can be high.

**Gap 2 (widen `unconditional_pyerr_clear`): the *compound-callee* narrowing.** The scanner asks
"is there a `PyErr_ExceptionMatches` within 3 lines?" — `setobject.c:2658`/`:2698` answer yes and
are suppressed, yet they are the finding. The missing question is **how many distinct failure
modes the tested call has**. Proposed rule `narrowing_over_compound_callee`: flag a
`PyErr_ExceptionMatches`-narrowed `PyErr_Clear()` whose guarded call is a *same-file static
helper* that (a) itself calls a Python-reaching API (`PyObject_Hash`, `PyObject_RichCompareBool`,
`PyObject_Call*`) on more than one line **and** (b) has a sibling in the same file where the
caller performs step (a) inline before the same clear. Condition (b) *is* the guarded twin, and it
is what makes this cheap: `_PySet_Contains`/`frozenset___contains__` vs
`set_remove_impl`/`set_discard_impl` is a 2-vs-2 split inside one file. I would expect this rule
to be near-silent tree-wide, which is the point.

**Gap 3 (new rule proposal): `assert-on-a-caller-supplied-value-in-a-callee-failure-branch`.**
Both `assert(new_dict == NULL)` sites assert a relationship between a *parameter* and an
*unrelated callee's* return status. `scan_null_checks` found `dictobject.c:4494` (CPY-0079)
because that assert *dereferences*; these two do not dereference, so nothing fires — yet they
are SIGABRTs on the default debug build. Rule sketch: inside `if (<call>(...) != 0) { … }` or after
a `== 0` early-return, flag `assert(<param> == NULL)` / `assert(<param> != NULL)` where `<param>`
is a function parameter that the guarded call does not take. Cross-check against the callee's
error contract (here: "fails only on OOM").

**Gap 4 (methodology, for `run_oom_sweep.py` documentation):** a payload's `keys[:40]` executed
*inside* the armed window is itself an allocation, and its `MemoryError` escapes the `try` and
looks like a harness failure (`lost sys.stderr`, exit 1). The dry-run guard does not catch it
because unarmed it succeeds. Worth one line in the docstring next to the existing "setup runs
unarmed" note: **slicing, f-strings, `range()` objects and `%`-formatting in the payload allocate;
hoist them into setup.** This cost me two iterations.

### 5b. Precision per rule (this slice)

| rule | candidates | confirmed | precision |
|---|---|---|---|
| `alloc_null_no_memerror` | 2 | 0 | 0% (both are documented deferral classes) |
| `unconditional_pyerr_clear` | 2 | 0 | 0% (one is the taxonomy's own exemplar) |
| all other `scan_error_paths` rules | 0 | 0 | evidential zero on live denominators |

The two `alloc_null_no_memerror` hits are worth keeping — the cross-caller verification they force
is cheap and the rule is the only thing that would catch a *new* raw allocator added without a
raise. The `unconditional_pyerr_clear` hit at `dictobject.c:1336` should be **suppressed by name**:
it is already in `data/cpython_non_bugs.md` verbatim as the "statically-known type slot" exemplar,
so re-emitting it every run is pure noise. Suggest a `_STATIC_TYPE_SLOT_HASH` gate on
`<PyType>_Type.tp_hash(` / `.tp_richcompare(` receivers.

### 5c. Concrete tuning proposals

1. Add `narrowing_over_compound_callee` (gap 2) to `scan_error_paths.py`.
2. Add `assert_on_unrelated_param_in_failure_branch` (gap 3) to `scan_null_checks.py` — it is the
   natural sibling of the rule that already found CPY-0079.
3. Suppress `unconditional_pyerr_clear` when the guarded call is `<Identifier>_Type.tp_*(`.
4. Document the payload-allocation trap in `run_oom_sweep.py`'s docstring (gap 4).
5. `insert-before-capacity-check` (gap 1) probably belongs in `scan_memory_patterns.py`.

---

## 6. Noticed outside slice / outside my class

* **`Objects/listobject.c` — CPY-0014 confirmed, 3/3.** The FT OOM sweep segfaults in
  `PyList_New` → `_Py_Dealloc` (`repro/CPY-setmanageddict_assert.py 6` on `debug-ft-nojit`,
  SIGSEGV 3/3). Already recorded; outside this slice.
* **`dictobject.c:4321-4346` `dict_merge` slow path** — runs `PyMapping_Keys(b)`,
  `PyObject_GetIter`, `PyIter_Next` and `dict_contains(a, …)` (all arbitrary Python) inside
  `Py_BEGIN_CRITICAL_SECTION(a)`. Same family as CPY-0096. **lock-discipline-checker's territory**;
  note that `_PyCriticalSection_BeginSlow`'s top-most-mutex recursion optimisation covers immediate
  re-entry but not a nested-then-re-entered acquisition.
* **`dictobject.c:5050-5055` `dict_popitem_impl`** — `_PyDict_NotifyEvent(PyDict_EVENT_DELETED, …)`
  at `:5051` runs a watcher callback (arbitrary Python) between reading `ep0[i].me_key` at `:5050`
  and `STORE_KEY(&ep0[i], NULL)` at `:5054`; a callback that mutates the dict invalidates `ep0`.
  **refcount-auditor's territory.**
* **`setobject.c:1743-1761` `set_intersection`** — iterates `other` with a live `set_next` cursor
  across `set_contains_entry`, which runs a user `__eq__`. `set_next` re-reads `so->table` per call
  and the key is INCREF'd first, so this is index-based and not a UAF — but it can silently skip or
  duplicate elements after a re-entrant mutation. **ft/refcount territory; recording the negative
  so it is not re-derived.**
* **`dictobject.c:6392` `dictiter_reduce`** — copies `dictiterobject tmp = *di` (the CPY-0028
  shape). Not a hang here: no critical section is held on `di` at the copy, and
  `dictiter_iternext*` locks `di_dict`, never the iterator. **Clean negative, stated so the shape
  is not re-hunted.**
* **`dictobject.c:2213` `ensure_shared_on_resize(mp)`** runs *before* the `new_keys_object`
  allocation at `:2220`, so a failed `dictresize` leaves the dict permanently marked shared. A
  one-way flag with no correctness consequence — recorded as a deliberate non-finding.

---

## 7. Artifacts

All under `/home/danzin/projects/cpython-review-toolkit/reports/obj-mappings/repro/`:

| file | what it is |
|---|---|
| `set_resize_oom_hang.py` | finding 1, bounded-window hang (4 builds) |
| `set_resize_oom_hang_trace.py` | finding 1, per-iteration marker trace localising the hang to the 9th `add()` |
| `set_resize_probe.py` | finding 1, dense width scan that located the window |
| `set_remove_discard_eq_clobber.py` | finding 2, both tiers + the KeyboardInterrupt control |
| `CPY-setmanageddict_assert.py` | finding 3, standalone SIGABRT 10/10 |
| `CPY-0079_setup.py`, `CPY-0079_payload.py` | CPY-0079 re-run, 48-point payload |
| `CPY-0079_repro.py` | CPY-0079 standalone SIGSEGV 10/10 |
| `CPY-0079_sweep_*.json` | CPY-0079 sweep results, 3 builds |
| `oom_dictset_setup.py`, `oom_dictset_payload.py` | broad 152-point dict/set OOM payload |
| `oom_dictset_*.json` | broad sweep results, 4 builds |
