# Lock-discipline checker — `Objects/typeobject.c` — PASS 2

**Run:** informed-explore, slice `obj-typeobject`, pass 2
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), `Objects/typeobject.c`, 13,068 lines
**Scanner baseline:** `scan_lock_discipline` — **0 findings**, `vocabulary_resolved=66`, `critical_section_functions=23`, `mutex_functions=3`
**Build ref:** `a1d580430c8`. `Objects/typeobject.c` **and** `Objects/dictobject.c` are **byte-identical** between target and build ref (`git diff` empty for both), so every reproduction below exercises exactly the reviewed code.

Pass 1's report is not repeated. Its two findings (CPY-0082) are confirmed in one line in §2.3.

---

## 1. The zero is EARNED — certified by hand, with numbers

Pass 1 measured `critical_section_functions: 2` — the rule resolved 8% of this file's locking and its zero was **unearned**. Issue #28's blindness-fix A (`resolve_local_lock_macros`) landed, and the same file now reports **23**. I re-derived that number independently rather than trusting it.

**Method.** `scratchpad/audit_pass2_locks2.py` parses the file with the toolkit's own `tree_sitter_utils.extract_functions`, walks every acquire/release token with `#define` lines excluded, pairs them stack-wise **per release-token** (so a `PyMutex_Unlock` can never close a `BEGIN_TYPE_LOCK`), attributes each to its enclosing function, and reports every `return`/`goto` textually between an acquire and its release.

### 1.1 Denominator — exact match, independently derived

| quantity | scanner | my count | agree |
|---|---|---|---|
| `functions_analyzed` | 459 | 459 | ✅ |
| `critical_section_functions` | 23 | **23** | ✅ |
| `mutex_functions` | 3 | **3** | ✅ |

The 23 are **24 acquire sites in 23 distinct functions** — `type_setattro` holds two (`BEGIN_TYPE_LOCK:6817` and `BEGIN_TYPE_DICT_LOCK:6829`). Full list, all verified balanced:

`_PyType_GetBases:590` · `PyType_Watch:1110` · `PyType_Modified:1250` · `_PyType_SetVersion:1338` · `PyUnstable_Type_AssignVersionTag:1461` · `type_set_abstractmethods:1743` · `type_set_bases:2029` · `mro_implementation:3522` · `_PyObject_SetDict:4029` · `PyType_GetModuleByToken_DuringGC:5934` · `_PyType_LookupStackRefAndVersion:6358` · `_PyType_CacheInitForSpecialization:6415` · `_PyType_CacheGetItemForSpecialization:6434` · `_PyType_Validate:6468` · `_PyType_SetFlagsRecursive:6508` · `type_setattro:6817` **+ `:6829`** · `fini_static_type:6934` · `object_set_class_world_stopped:7778` · `PyType_Ready:9627` · `init_static_type:9669` · `hackcheck:10177` · `releasebuffer_maybe_call_super:11407` · `PyType_Freeze:12522`

The 3 `PyMutex` functions: `managed_static_type_state_init:323/326` · `managed_static_type_state_clear:416/422` · `type_add_common_descriptor:4630/4638`.

### 1.2 The leak audit — 27 regions, zero early exits

**Every one of the 24 critical-section regions and 3 mutex pairs is balanced, same-function, and contains not a single `return` or `goto` between acquire and release.** Not one region needs the release-then-exit idiom because not one region has an early exit.

The only control flow inside any region is loop-internal: `PyType_GetModuleByToken_DuringGC:5934–5961` uses `continue` (:5951) and `break` (:5958) inside a `for`, accumulates into the local `res`, and returns it at :5963 **after** `END_TYPE_LOCK()` at :5961. That is the canonical accumulate-then-release idiom, not an exit.

**Verdict: constructs present, all 27 verified clean. This is an earned zero on a real denominator, and the `resolve_local_lock_macros` fix is the reason it is now measurable.** It matches the sibling result that 17 of 18 `Objects/` files are an earned zero — with this file now joining them instead of being the one outlier.

---

## 2. Findings — what the rule structurally cannot see

### 2.1 [FIX] `_PyType_Modified_Unlocked` runs arbitrary Python while `dictobject.c` holds a non-recursive, non-suspendable `dk_mutex` → **reproduced permanent hang** (`Objects/typeobject.c:1222-1223` ⇄ `Objects/dictobject.c:1962-1971`)

**This is the history preflight's #1 pass-2 target (§4.2), and it is live.**

**What.** `Objects/dictobject.c:insert_split_key` acquires two locks and then calls into typeobject.c:

```c
    Py_BEGIN_CRITICAL_SECTION_MUTEX(&_PyInterpreterState_GET()->types.mutex);   // :1959 TYPE_LOCK
    LOCK_KEYS(keys);                                                            // :1962
    ...
        if (type) {
            // we acquired the type lock above
            _PyType_Modified_Unlocked(type);                                    // :1971
        }
```

`LOCK_KEYS` is **not** a critical section:

```c
#define LOCK_KEYS(keys) PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)   // dictobject.c:227
```

A raw `PyMutex`, **non-recursive**, taken with `_Py_LOCK_DONT_DETACH` — so `_PyCriticalSection_SuspendAll` will never release it and a thread blocked on it never detaches.

`_PyType_Modified_Unlocked` then runs **arbitrary Python** under it:

```c
                PyType_WatchCallback cb = interp->type_watchers[i];
                if (cb && (cb(type) < 0)) {                       // :1222  arbitrary C, may run Python
                    PyErr_FormatUnraisable(
                        "Exception ignored in type watcher callback #%d for %R",   // :1223 -> sys.unraisablehook
                        i, type);
```

`PyErr_FormatUnraisable` calls `sys.unraisablehook`, which is pure Python. Any attribute insertion performed there re-enters `insert_split_key` and blocks forever on the `dk_mutex` it already holds.

**Impact.** Permanent, uninterruptible hang of the interpreter. **Free-threaded build only** — `LOCK_KEYS` is an empty macro under the GIL (`dictobject.c:257`).

**Reproduced** — `repro/lock_insert_split_key_deadlock.py`:

| build | result | exit |
|---|---|---|
| `debug-ft-nojit`, `PYTHON_GIL=0` | **HANG** | 124 (killed) |
| `release-ft-nojit`, `PYTHON_GIL=0` | **HANG** | 124 (killed) |
| `debug-gil-nojit` (control) | completes cleanly | 0 |

The native backtrace (`repro/lock_insert_split_key_deadlock_gdb.py` under gdb) is conclusive — **the same `keys` object in both frames**:

```
#9  _PyMutex_LockTimed (m=0x2000286dca3, flags=_Py_LOCK_DONT_DETACH) at Python/lock.c:138
#11 insert_split_key (keys=0x2000286dc98, ...) at Objects/dictobject.c:1962      <-- blocks here
...
#21 format_unraisable_v (format="Exception ignored in type watcher callback #%d for %R")
#22 PyErr_FormatUnraisable at Python/errors.c:1777
#23 _PyType_Modified_Unlocked at Objects/typeobject.c:1223
#24 insert_split_key (keys=0x2000286dc98, ...) at Objects/dictobject.c:1971      <-- holds the mutex
```

`0x2000286dca3 − 0x2000286dc98 = 11` = the offset of `dk_mutex` in the same `PyDictKeysObject`.

**Guarded twin.** `Objects/typeobject.c:6694 type_update_dict`, which states the discipline this path violates:

> *"We don't want any re-entrancy between when we update the dict and call `_PyType_Modified_Unlocked`, **including running the destructor of the current value**… Because we have an exact unicode and our dict has exact unicodes we know that this will all complete without releasing the locks."*

`type_update_dict` earns that claim by interning `name` at `type_setattro:6768-6785` and by deferring the old value's `Py_XDECREF` to `:6843`, **after** `END_TYPE_DICT_LOCK()`. `insert_split_key` makes no equivalent argument — and cannot, because the watcher callback is an arbitrary function pointer.

**Not a re-litigation.** This is the fourth iteration of the same code path, and every prior issue is **closed**:

| | |
|---|---|
| gh-150490 | original change |
| gh-151593 | *"test_abc hangs on TSan Parallel Test on Free Threading"* — the same deadlock class |
| gh-152200 | attempted fix — introduced a data race |
| gh-152238 | **revert**: *"it violates locking discipline and results in deadlocks"* |
| gh-152914 | **"take 2"**, landed 2026-07-06, **19 days before HEAD** — the code reviewed here |

`gh api -X GET search/issues` over `repo:python/cpython` for *insert_split_key deadlock*, *\_PyType_Modified_Unlocked deadlock*, *dk_mutex deadlock* and *150490 deadlock* returns **only these five, all closed**. **No open issue covers the take-2 state.**

**Fix shape.** Take-2 fixed the lock *order* (TYPE_LOCK before the keys mutex) but not the fact that a Python-reaching call sits under a `DONT_DETACH` mutex. The invalidation must move out of the `LOCK_KEYS` region — collect `type` inside, call `_PyType_Modified_Unlocked` after `UNLOCK_KEYS(keys)` — which is exactly the ordering `_PyType_SetFlagsRecursive:6509-6513` already documents for the analogous case: *"`_PyType_Modified_Unlocked()` is re-entrant and cannot run with the world stopped, so we must invalidate first."*

**Honest scoping.** The trigger requires a registered type watcher. That is **not** test-only: `Python/optimizer_analysis.c:177` and `optimizer_bytecodes.c` arm `PyType_Watch` for the tier-2 JIT, and `PyType_AddWatcher` is public C API. My reproduction uses `_testcapi`'s error-returning callback to force the `PyErr_FormatUnraisable` path deterministically; a third-party watcher that itself runs Python deadlocks without needing the error path. I did **not** reproduce the two-thread stop-the-world variant (`repro/lock_stw_vs_dk_mutex.py`) — my T2 exhausts the shared keys within ~90k iterations and stops contending the mutex, so the design is flawed, not the hypothesis. The single-threaded form above is conclusive on its own.

---

### 2.2 [CONSIDER] `tp_watched` is written under `TYPE_LOCK` in one place and bare in the other → **reproduced data race** (`Objects/typeobject.c:1112` vs `:1129`)

**What.** The preflight's asymmetry lead, confirmed and characterised. `unsigned char tp_watched` (`Include/cpython/object.h:236`) has exactly two writers, both plain non-atomic read-modify-write on one byte:

```c
PyType_Watch:    BEGIN_TYPE_LOCK();
                 assign_version_tag(interp, type);
                 type->tp_watched |= (1 << watcher_id);      // :1112  UNDER TYPE_LOCK
                 END_TYPE_LOCK();

PyType_Unwatch:  type->tp_watched &= ~(1 << watcher_id);     // :1129  NO LOCK AT ALL
```

and two readers, `_PyType_Modified_Unlocked:1212-1214` (under `TYPE_LOCK`) and `type_dealloc:6988-6991`. No comment anywhere explains the difference.

**Reproduced** — `repro/tsan_tp_watched_race.py` on `debug-ft-nojit-tsan`, `PYTHON_GIL=0`: **6 ThreadSanitizer data races**, log at `repro/tsan_tp_watched_race.log`:

```
WARNING: ThreadSanitizer: data race (pid=1346074)
  Write of size 1 at 0x7acd3a7df1b8 by thread T2:
    #0 PyType_Watch   Objects/typeobject.c:1112:22
  ...
    #0 PyType_Unwatch Objects/typeobject.c:1129:22
```

`Write of size 1` is the `unsigned char` — a genuine byte-granularity lost-update race, both write/write and write/read against the locked reader.

**Impact.** A lost `|=` means a watcher believes it is armed while `tp_watched` has the bit clear, so `_PyType_Modified_Unlocked` never invokes its callback. This is not cosmetic: `Python/optimizer_analysis.c:177` arms this bit for the **tier-2 JIT's type-guard invalidation** and `:156` disarms it. A lost SET means a modified type never invalidates its executor. A lost CLEAR is benign — `:1222` checks `cb &&` before calling.

**Guarded twin.** `PyType_Watch:1110-1113` itself, 16 lines above: same field, same operation, correctly bracketed by `BEGIN_TYPE_LOCK()`/`END_TYPE_LOCK()`.

**Fix.** Bracket `:1129` in `BEGIN_TYPE_LOCK()`/`END_TYPE_LOCK()`, matching `PyType_Watch`. (`PyType_Unwatch` does not need `assign_version_tag`; only the store needs the lock.)

**Why CONSIDER and not FIX.** Reaching the JIT consequence needs two threads concurrently optimizing the same type on a free-threaded **JIT** build, and no such build exists in the matrix (`*-ft-*` are all `-nojit`), so I could reproduce the race but not a wrong-code outcome. Per the toolkit's calibration, an FT finding without a demonstrated user-visible consequence stays CONSIDER.

**Sibling sweep — bounded, and this is the only one.** `scratchpad/asym_field_sweep.py` classified every write to a `tp_*`/`_spec_cache` field in the file by whether it sits inside a `TYPE_LOCK` region, a stop-the-world region, or neither. 35 fields, 4 asymmetric, 3 explained:

| field | verdict |
|---|---|
| `tp_flags` :458 bare | **not a finding** — `type_set_flags` carries `ASSERT_WORLD_STOPPED_OR_NEW_TYPE(tp)` (:457); the STW is in its callers |
| `tp_base` :4433/:5644/:9060/:9063 bare | **not a finding** — all type-construction paths, permitted by the design comment at :65-77 (type not yet revealed) |
| `tp_dict` :562 bare | **not a finding** — `set_tp_dict` runs only from `PyType_Ready`/`type_dealloc`, never on a live ready type |
| **`tp_watched` :1129 bare** | **the finding above** |

---

### 2.3 [CONSIDER] CPY-0082 — confirmed unchanged, not re-litigated

`type_set_abstractmethods:1745` and `PyType_Freeze:12523` still call `types_stop_world()` inside `BEGIN_TYPE_LOCK()` **without** `type_lock_prevent_release()`. My STW pairing pass confirms the partition is still exactly 5-of-7 guarded (`set_tp_mro:668`, `type_set_bases_unlocked:1938` and `:2004`, `apply_type_slot_updates:3922`, `_PyType_SetFlagsRecursive:6521`), with these two the only unguarded sites that hold `TYPE_LOCK`. Guarded twin remains `_PyType_SetFlagsRecursive:6506` (rationale at :6519-6520). No change since pass 1.

---

## 3. Clean negatives — audited, and NOT findings

Recorded so pass 3 does not re-open them.

**The preflight's #1 hunt target is safe, and the runtime says so explicitly.** `object_set_class_world_stopped:7778` takes `Py_BEGIN_CRITICAL_SECTION(dict)` inside the stop-the-world window opened at `object_set_class:7823`. The preflight called this *"the textbook STW deadlock shape… the single highest-value hunt target in the pass-2 set."* **It is not a deadlock**, for two independent, verified reasons:

1. Every thread that parks for stop-the-world goes through `_PyThreadState_Suspend` → `detach_thread` (`Python/pystate.c:2365`) → `_PyCriticalSection_SuspendAll` (`:2323`), which **unlocks every critical-section mutex the thread holds** (`Python/critical_section.c:109-127`). So `dict->ob_mutex` is guaranteed uncontended while the world is stopped.
2. `_PyCriticalSection_BeginSlow` **no-ops entirely when the world is stopped**, with a comment naming this exact hazard (`Python/critical_section.c:47-54`): *"If the world is stopped, we don't need to acquire the lock… Without this check, acquiring a critical section while the world is stopped could lead to a deadlock."*

I checked the two ways the invariant could be broken and both are absent tree-wide: there is **no** raw `PyMutex_Lock(&…ob_mutex)` anywhere, and `type_lock_prevent_release` (the one mechanism that makes a lock un-suspendable) splices **only** `TYPE_LOCK`, asserted at `:150`, `:155`, `:159`. **ACCEPTABLE.**

**The `unique` fast path does not break the `_LockHeld` contract.** `object_set_class:7821` skips stop-the-world when `_PyObject_IsUniquelyReferenced(self)`, and `object_set_class_world_stopped:7769` then calls `_PyObject_MaterializeManagedDict_LockHeld`, whose first statement is `ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED(obj)` — neither of which holds on that path. It is still correct: the assertion has a refcount escape, `if (Py_REFCNT(op) != 1)` (`Include/internal/pycore_critical_section.h:64-67`), and `_PyObject_IsUniquelyReferenced` implies `Py_REFCNT == 1`, so the escape covers exactly the fast path. gh-145566 also **added** the `:7778` critical section in the same commit, precisely because STW no longer always covers the detach. Coherent design. **ACCEPTABLE.**

**The lookup-cache narrowing (R19) did not leave a store unprotected.** `_PyType_LookupStackRefAndVersion:6357` now takes `TYPE_LOCK` only when `should_assign_version_tag()` is true (born 2026-03-12, gh-145685 perf, zero fix history), calling `find_name_in_mro` **unlocked** at `:6365`. Sound on two counts: `find_name_in_mro:6145` carries no lock-held assertion and defends itself by pinning the MRO with a `_PyCStackRef` (`:6172-6174`, *"because `type->tp_mro` can be replaced during dict lookup"*); and a stale `update_cache(entry, name, version_tag, …)` written after a concurrent invalidation can never be read back, because `next_global_version_tag` is monotonic and never reuses a tag, so `entry_version == type_version` simply stops matching. The documented store order (value → name → **version release**, `:6218/6224/6230`, rationale at `:6225-6229`) is intact and mirrored by the reader at `:6313-6337`. **ACCEPTABLE.**

**`find_name_in_mro`'s `goto done` is the correct idiom, not a leak.** `:6185` jumps to `:6192`, where `_PyThreadState_PopCStackRef` runs before the return. Release-at-the-label — silent by design, and it is a CStackRef, not a lock.

**The raw `types.mutex` sites never nest inside a `TYPE_LOCK` critical section.** `managed_static_type_state_init:323/326` and `_clear:416/422` take `&interp->types.mutex` with a bare `PyMutex_Lock` — the *same mutex* `TYPE_LOCK` names (`:78`), and non-recursive, so nesting would self-deadlock. Verified they cannot nest: `init_static_type` calls `state_init` at `:9666`, **before** `BEGIN_TYPE_LOCK()` at `:9669`, and `state_clear` at `:9674`, **after** `END_TYPE_LOCK()` at `:9671`; `fini_static_type` calls `state_clear` at `:6941`, after `END_TYPE_LOCK()` at `:6937`. Correct by ordering — but by ordering alone, with no assertion enforcing it. **ACCEPTABLE, fragile.**

**Two-object locking — nothing to recommend.** The only two-mutex site is `BEGIN_TYPE_DICT_LOCK(d)` (`:82-83`), already the deadlock-safe `Py_BEGIN_CRITICAL_SECTION2_MUTEX(TYPE_LOCK, &d->ob_mutex)`. No site nests two single-object begins.

**The lock-order graph has no cycle among the modelled locks.** Order is documented twice and consistently: `typeobject.c:3920-3921` (*"we always acquire locks in the same order: first the TYPE_LOCK mutex and then the STM mutex"*) and `dictobject.c:1955-1958` (*"we must always acquire the type lock first"*, TYPE_LOCK before the keys mutex). Every site obeys it. The §2.1 defect is **not** an ordering violation — it is a Python-reaching call under a non-suspendable lock, which no ordering rule prevents.

---

## 4. Preflight corrections

- **`include_map.md` §2.1 over-counts `BEGIN_TYPE_DICT_LOCK`.** It reports 3 call sites, *"6829 (plus 2 in pass-1 territory)"*. There is exactly **one** use site (`:6829`); the other two grep hits are the `#define` at `:82` and the GIL no-op at `:186`. Pass 1 had this right. The scanner's `vocabulary_counts` of 3 is a raw token count and is correct as such.

---

## TOOLKIT ASSESSMENT

### The certified-zero statement

**`scan_lock_discipline` reports 0 findings on `Objects/typeobject.c` over `functions_analyzed=459`, `vocabulary_resolved=66`, `vocabulary_tokens_seen=11`, `critical_section_functions=23`, `mutex_functions=3`. I independently re-derived 459 / 23 / 3 with tree-sitter and hand-verified all 24 critical-section regions and all 3 `PyMutex` pairs: every one is balanced, same-function, and contains zero `return`/`goto` between acquire and release. This is an EARNED zero.**

That 23 was **2** in pass 1. Issue #28's blindness-fix A (`resolve_local_lock_macros`) is the difference: it resolves `BEGIN_TYPE_LOCK` / `END_TYPE_LOCK` / `BEGIN_TYPE_DICT_LOCK` / `END_TYPE_DICT_LOCK` as file-local aliases and picks up `ASSERT_TYPE_LOCK_HELD` as a lock-held assertion. This file was the fix's motivating example and the fix works exactly as specified — **8% → 100% vocabulary resolution, and the file moves from being the one unearned zero in `Objects/` into the earned-zero majority.**

**Precision: 0 false positives on 0 findings. Recall on the leak/nesting shapes: complete. Recall on lock *semantics*: 0 of 3 — all three findings in §2 are invisible to the rule.**

### Scanner extension proposal — ranked by tractability

**1. `python_call_under_nonsuspendable_lock` — NEW RULE, highest value, fully tractable.**
This would have found §2.1 without me. The insight the current model is missing is that the two lock families differ by **suspendability**, not just by pairing style:

- *scoped critical section* — the runtime auto-releases it when the holder blocks or parks (`_PyCriticalSection_SuspendAll`). Running Python under it is **re-entrancy**, and CPython does it deliberately (`mro_invoke:3603` calls a user `mro()` under `TYPE_LOCK`, documented at `:3601`). **Not a finding.**
- *`PyMutex` family* — **not** released on detach, and `_Py_LOCK_DONT_DETACH` additionally stops the blocker from parking. Running Python under it is a **deadlock**. **A finding.**

Key on: an acquire from the `PyMutex` family (extend the vocabulary with `PyMutex_LockFlags(..., _Py_LOCK_DONT_DETACH)` and the `LOCK_KEYS`/`UNLOCK_KEYS` file-local alias pair, both already reachable via `resolve_local_lock_macros`), with a Python-reaching call between it and its release. Reuse `scan_refcounts`'s existing Python-reaching call set, plus (a) the unraisable/warning family `PyErr_FormatUnraisable` / `PyErr_WriteUnraisable` / `PyErr_WarnEx`, and (b) **any indirect call through a function pointer** — `cb(type)` at `:1222` is the actual entry point and no name-based list can catch it. Emitting "indirect call under a non-suspendable lock" alone would be a high-signal, low-volume rule.

**2. `asymmetric_field_guard` — NEW RULE, tractable today, ~120 lines.**
`scratchpad/asym_field_sweep.py` is a working prototype: it found §2.2 and bounded the class to exactly one true positive out of 35 fields in a single pass. It needs only what `resolve_local_lock_macros` already provides (acquire/release aliases → a locked line-set) plus the STW pair. Two FP suppressions make it publishable, and both are nearly free:
- suppress a bare write in a helper carrying a **lock-held assertion** — the scanner already extracts `lock_held_assertions` (it found `ASSERT_TYPE_LOCK_HELD`); just widen that vocabulary to `ASSERT_WORLD_STOPPED_OR_NEW_TYPE` and `ASSERT_NEW_TYPE_OR_LOCKED`. This alone kills the `tp_flags` and `tp_dict`/`tp_bases` FPs.
- suppress writes in construction paths (`*_new*|*_ready*|*_init*|*from_spec*`) — kills the `tp_base` FPs.

**3. `stw_under_lock_without_suspend_guard` — recovers CPY-0082 mechanically, ~20 lines.**
Key on an STW-opener token appearing between an acquire and its matching release where no suspend-guard call precedes it in the same region. Needs the `stop_the_world` family actually **modelled** (pass 1's defect #3: it sits unused in `data/lock_macros.json`), plus trivial-static-wrapper resolution so `types_stop_world` → `_PyEval_StopTheWorld` is seen — the include map's §4.2 blind spot #1. Detect the suspend-guard generically by body pattern (`_cs_mutex = NULL`), not by name.

**4. Lock-order cycle detection — NOT tractable, and not needed.**
Full ordering requires a whole-program call graph plus lock-identity resolution across files (`TYPE_LOCK` ≡ `&interp->types.mutex` ≡ the raw `PyMutex_Lock` at `:323`). Out of reach, and it would not have found anything here: the file's documented order is obeyed everywhere and the real defect is orthogonal to ordering. **Recommend not building it**; build rule 1 instead, which catches the deadlock class that ordering rules miss.

### Artifacts

- `scratchpad/audit_pass2_locks2.py` — tree-sitter pairing + denominator certification
- `scratchpad/asym_field_sweep.py` — asymmetric-field-guard prototype (proposal 2)
- `repro/lock_insert_split_key_deadlock.py` — §2.1, reproduces the hang
- `repro/lock_insert_split_key_deadlock_gdb.py` — §2.1, native backtrace under gdb
- `repro/lock_stw_vs_dk_mutex.py` — §2.1 two-thread variant, **NOT reproduced** (design flaw noted)
- `repro/tsan_tp_watched_race.py` + `.log` — §2.2, 6 TSan races
