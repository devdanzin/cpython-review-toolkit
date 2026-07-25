# Lock-discipline checker — `Objects/typeobject.c`

**Run:** informed-explore, slice `obj-typeobject`
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), `Objects/typeobject.c`, 13,068 lines
**Scanner baseline:** `scan_lock_discipline` — 0 findings in slice, 0 across all of `Objects/`

---

## 1. Denominator verdict (primary deliverable)

**The rule was not inert here, but it was nearly so: it resolved 2 of the file's 25 lock regions — 8%.**

`scan_lock_discipline.sample.json` reports `critical_section_functions: 2`, `mutex_functions: 3`, and six `vocabulary_counts` hits. Reading those hits against the source shows **three of the six are the `#define` lines themselves, not uses**:

| vocabulary hit | count | what it actually is |
|---|---|---|
| `Py_BEGIN_CRITICAL_SECTION` | 2 | 2 real uses — `:4029`, `:7778` |
| `Py_END_CRITICAL_SECTION` | 3 | 2 real uses + **the `#define` at `:80`** |
| `Py_BEGIN_CRITICAL_SECTION_MUTEX` | 1 | **the `#define` at `:79`** — zero uses |
| `Py_END_CRITICAL_SECTION2` | 1 | **the `#define` at `:85`** — zero uses |
| `PyMutex_Lock` / `PyMutex_Unlock` | 3 / 3 | real, and correctly paired |

So the rule saw 2 critical sections and 3 mutex pairs. The file's *actual* locking protocol — 21 `BEGIN_TYPE_LOCK()`/`END_TYPE_LOCK()` pairs, 1 `BEGIN_TYPE_DICT_LOCK()`/`END_TYPE_DICT_LOCK()` pair, and 9 `types_stop_world()`/`types_start_world()` regions — is **entirely invisible**.

The cause is what the concurrently-run `ft-race-scanner` diagnosed for its own vocabulary, and it applies identically here. `Objects/typeobject.c:78-85` defines the whole protocol as file-local aliases:

```c
#define TYPE_LOCK &_PyInterpreterState_GET()->types.mutex
#define BEGIN_TYPE_LOCK() Py_BEGIN_CRITICAL_SECTION_MUTEX(TYPE_LOCK)
#define END_TYPE_LOCK() Py_END_CRITICAL_SECTION()

#define BEGIN_TYPE_DICT_LOCK(d) \
    Py_BEGIN_CRITICAL_SECTION2_MUTEX(TYPE_LOCK, &_PyObject_CAST(d)->ob_mutex)
#define END_TYPE_DICT_LOCK() Py_END_CRITICAL_SECTION2()
```

The scanner has no same-translation-unit define resolution, so it matches the `#define` line and none of the 22 call sites.

### Correction: this does *not* explain the `Objects/`-wide zero

The coordinator's hypothesis was that the directory-wide zero is the same macro-alias artifact. **My measurement contradicts that.** Raw token counts over `Objects/*.c`:

| token | occurrences in `Objects/` |
|---|---|
| `Py_BEGIN_CRITICAL_SECTION` | **156** |
| `Py_BEGIN_CRITICAL_SECTION2` | **30** |
| `Py_END_CRITICAL_SECTION` | 157 |
| `PyMutex_Lock` | 13 |
| `LOCK_WEAKREFS` | 6 |
| `Py_BEGIN_CRITICAL_SECTION_MUTEX` | 2 |
| `Py_BEGIN_CRITICAL_SECTION2_MUTEX` | **1** (only `typeobject.c:83`) |

186 raw begin-tokens across 18 files are spelled in the vocabulary the scanner *does* know. So the `Objects/` zero decomposes into two different results:

- **17 of 18 files** — constructs present, resolved, and verified clean. An earned zero.
- **`typeobject.c`** — constructs present and 92% invisible. An unearned zero.

`typeobject.c` is the outlier, not the rule. Reporting the directory zero as uniformly artifactual would overstate the defect.

### Function-count denominator

Scanner `functions_analyzed: 417`; a crude count of C-style definitions in the file gives **~480**. A ~13% gap, consistent with the known tree-sitter/preprocessor attribution issue. It cost nothing here because the entire audit below was done by hand.

### Toolkit defects (ranked)

1. **No same-TU object-like `#define` resolution.** Resolve an object-like `#define` whose body contains a known lock token, then treat the alias as that token. Recovers 21+1 pairs in this file. This is the single highest-value fix.
2. **`Py_BEGIN_CRITICAL_SECTION2_MUTEX` is absent from `data/lock_macros.json` entirely.** It is not in `lock_pairs`, not in `all_acquire_macros`. `typeobject.c:83` is its only use in `Objects/`, so the miss is currently cheap — but it is a silent vocabulary hole, and its partner `Py_END_CRITICAL_SECTION2` *is* listed, so the asymmetry would mis-pair if the alias fix above landed without it.
3. **The `stop_the_world` family is in the data file but not modelled.** `lock_macros.json` defines `_PyEval_StopTheWorld`/`_PyEval_StartTheWorld` with the note *"No Python code, GC, or exceptions allowed until StartTheWorld"*, but the agent contract models only the *scoped* and *PyMutex* families. All 9 STW regions here are unpaired and unchecked — **and both findings in §3 live in exactly that gap.**
4. **Treat `ASSERT_*_LOCK_HELD()` as a held-lock assertion**, the same way the `*_lock_held` callee naming convention is already treated. There are 16 `ASSERT_TYPE_LOCK_HELD()` sites in this file (`:1295, 1410, 1751, 1800, 1931, 3433, 3596, 3657, 6739, 9248, 9323, 9530, 10126, 12096, 12162, 12526`). Each is a free, authoritative, cross-function statement that TYPE_LOCK is held — the only practical way past the intra-function limitation in a file whose locking is this heavily wrapper-based.

---

## 2. Critical-section census — 25 regions, all balanced, zero early exits

Hand-audited every region with a purpose-built pairing script (`scratchpad/audit_typelock.py`), which skips `#define` lines and reports any `return` or `goto` textually between a begin and its end.

| family | use-site pairs | unbalanced | regions containing `return`/`goto` |
|---|---|---|---|
| `BEGIN_TYPE_LOCK` / `END_TYPE_LOCK` | 21 | 0 | **0** |
| `BEGIN_TYPE_DICT_LOCK` / `END_TYPE_DICT_LOCK` | 1 (`:6829..6838`) | 0 | **0** |
| `Py_BEGIN_CRITICAL_SECTION` / `Py_END_CRITICAL_SECTION` | 2 (`:4029..4033`, `:7778..7780`) | 0 | **0** |
| `types_stop_world` / `types_start_world` | 9 | 0 | **0** |
| `type_lock_prevent_release` / `type_lock_allow_release` | 5 | 0 | **0** |
| `PyMutex_Lock` / `PyMutex_Unlock` | 3 | 0 | **0** |

**Verdict: constructs present, all 25 verified clean on the leak shape.** Not a single lock region in this file contains an early `return` or a `goto`. Every region is straight-line; the largest is 27 lines (`:5934..5961`, `PyType_GetModuleByToken_DuringGC`) and even that one uses `break` out of a loop, never a jump past the END.

### The `type_ready` goto question — definitively answered

The complexity agent flagged 14 `goto`s in `type_ready`. They are **not** a leak risk:

- All 14 target a single intra-function label, `error:` at `:9605`, which does cleanup and `return -1`.
- **`type_ready` contains zero `BEGIN_TYPE_LOCK`/`END_TYPE_LOCK`.** Verified by grep over the function body.
- The lock lives in its two callers, both branch-free trivial wrappers: `PyType_Ready` (`:9627..9634`) and `init_static_type` (`:9669..9671`).

A `goto error` in `type_ready` returns normally to the caller, which then executes `END_TYPE_LOCK()`. This is the *correct* cross-function idiom — the scoped local never goes out of scope early — and it is precisely the shape the scanner's intra-function limitation would have to be careful not to false-positive on.

### Two-object locking — clean

The only two-object lock in the file is `BEGIN_TYPE_DICT_LOCK(d)` at `:82`, which already uses the deadlock-safe `Py_BEGIN_CRITICAL_SECTION2_MUTEX(TYPE_LOCK, &d->ob_mutex)` form (canonical address ordering, single `Py_END_CRITICAL_SECTION2()`). There is **no** site anywhere in the file that nests two single-object begins, and neither of the two plain `Py_BEGIN_CRITICAL_SECTION` sites (`:4029` in `_PyObject_SetDict`, `:7778` in `object_set_class_world_stopped`) is inside a TYPE_LOCK region. Nothing to recommend here.

---

## 3. Findings

### [CONSIDER] `type_set_abstractmethods` stops the world under TYPE_LOCK without `type_lock_prevent_release()` (`Objects/typeobject.c:1743-1752`)

```c
BEGIN_TYPE_LOCK();
_PyType_Modified_Unlocked(type);
types_stop_world();                                  // <-- may DROP TYPE_LOCK
if (abstract)
    type_add_flags(type, Py_TPFLAGS_IS_ABSTRACT);
else
    type_clear_flags(type, Py_TPFLAGS_IS_ABSTRACT);
types_start_world();
ASSERT_TYPE_LOCK_HELD();
END_TYPE_LOCK();
```

**What.** `types_stop_world()` → `_PyEval_StopTheWorld` → `stop_the_world()` → `PyMutex_Lock(&stw->mutex)` (`Python/pystate.c:2453`). `PyMutex_Lock` passes `_PY_LOCK_DETACH` (`Python/lock.c:654-657`), so on contention the thread parks and detaches; `detach_thread` calls `_PyCriticalSection_SuspendAll` (`Python/pystate.c:2322-2324`), which **unlocks every critical-section mutex the thread holds** (`Python/critical_section.c:113-124`) — including TYPE_LOCK.

CPython documents this exact hazard, in this exact file, at `:3910-3921`:

> *"If we block on the stop-the-world mutex then we could release TYPE_LOCK mutex and potentially allow other threads to update the dict. That's because TYPE_LOCK was acquired using a critical section. The `type_lock_prevent_release()` call prevents the TYPE_LOCK mutex from being released even if we block on the STM mutex."*

`type_lock_prevent_release()` (`:143-163`) defeats the suspend by setting `c->_cs_mutex = NULL`, which is exactly the field `_PyCriticalSection_SuspendAll` tests before unlocking.

**Impact.** The invalidate-then-flip pair is not atomic. In the drop window another thread can take TYPE_LOCK and run `assign_version_tag()`, re-validating the version tag `_PyType_Modified_Unlocked` just invalidated for this type and all its subclasses. The flag then changes under a live, valid tag. gh-148450 (*"`abc.register` needs to update `type_version` when `tp_flags` is changed"*) establishes that the maintainers treat tag/flag atomicity as load-bearing.

Note the `ASSERT_TYPE_LOCK_HELD()` on the next line **cannot detect this**: suspend-and-resume re-acquires the mutex, so the assertion passes even when the lock was dropped mid-region.

**Guarded twin.** The 5 sites that do it correctly — `set_tp_mro` (`:668-674`), `type_set_bases_unlocked` (`:1938-1943`), its bail path (`:2004-2009`), `apply_type_slot_updates` (`:3922-3926`, with the explanatory comment), and `_PyType_SetFlagsRecursive` (`:6521-6525`, *"Keep TYPE_LOCK held while waiting for stop-the-world so no thread can reassign a version tag before the flag update"*).

**Fix.** Wrap as the twins do:
```c
type_lock_prevent_release();
types_stop_world();
...
types_start_world();
type_lock_allow_release();
```

---

### [CONSIDER] `PyType_Freeze` stops the world under TYPE_LOCK without `type_lock_prevent_release()` (`Objects/typeobject.c:12522-12528`)

```c
BEGIN_TYPE_LOCK();
types_stop_world();                                  // <-- may DROP TYPE_LOCK
type_add_flags(type, Py_TPFLAGS_IMMUTABLETYPE);
types_start_world();
ASSERT_TYPE_LOCK_HELD();
_PyType_Modified_Unlocked(type);
END_TYPE_LOCK();
```

Same mechanism, same guarded twin, same fix. Here the flag flip precedes the invalidation, so the exposed window is between `type_add_flags` and `_PyType_Modified_Unlocked` — during which TYPE_LOCK may already have been dropped and re-taken, and TYPE_LOCK is exactly the mutex that serializes this against `type_setattro`'s `BEGIN_TYPE_DICT_LOCK` (`:6829`).

*Scoping honesty:* `type_setattro` reads `Py_TPFLAGS_IMMUTABLETYPE` lock-free at `:6754`, before taking any lock, so a freeze/setattr race window exists independently of this defect. The lock drop widens it; it is not its sole cause. I am not claiming `PyType_Freeze` is exploitable — only that its locked region does not hold its lock.

---

### Both findings are a three-generation fix-propagation gap

`type_lock_prevent_release()` was introduced by **`fbbbc10055e`** (gh-127266 / PR gh-133177). That same commit added `types_stop_world()` *inside the already-existing `BEGIN_TYPE_LOCK()` region* of both `type_set_abstractmethods` and `PyType_Freeze`, adding `ASSERT_TYPE_LOCK_HELD()` after each — while adding `type_lock_prevent_release()` plus the full explanatory comment to `apply_type_slot_updates`. The hazard was reasoned about in one hunk and not applied in two others in the same commit.

The guard has since been propagated to newly-found sites **three separate times**:

| commit | issue | site given the guard |
|---|---|---|
| `fbbbc10055e` | gh-127266 | `apply_type_slot_updates` |
| `b83f379a972` | gh-140549 | `set_tp_mro`, `type_set_bases_unlocked` |
| `e7eaed56149` | gh-148450 | `_PyType_SetFlagsRecursive` |

`type_set_abstractmethods` and `PyType_Freeze` were missed by all three.

**The partition is exact.** Of 9 `types_stop_world()` use-sites, 5 call `type_lock_prevent_release()` first. Of the remaining 4, two hold no TYPE_LOCK at all and are therefore correct as written — `_PyType_SetFlags` (`:6457`) and `object_set_class` (`:7823`) — and the other two are the findings above. There is no third category.

**Prior art:** `gh api search/issues` over `repo:python/cpython` for `type_lock_prevent_release`, `PyType_Freeze free-threading lock`, `abstractmethods stop the world free-threaded`, and `TYPE_LOCK critical section released stop-the-world` returns only the four commits above (all closed). **No open issue covers these two sites.**

**Reproduction: NOT REPRODUCED — reported honestly as CONSIDER.**
`scratchpad/stress_typelock.py` hammers both paths (`__abstractmethods__` churn over a 12-deep subclass chain, `__class__` reassignment, `gc.collect`, and concurrent type-attribute traffic to drive `assign_version_tag`) on `debug-ft-nojit` (asserts on) and `debug-ft-nojit-tsan`, `PYTHON_GIL=0`.

| build | duration | iterations | result | exit |
|---|---|---|---|---|
| `debug-ft-nojit` | 10 s | abstract=191, gc=927, setclass=53, lookup=1358 | no crash / no assert / no hang | 0 |
| `debug-ft-nojit-tsan` | 25 s | abstract=212, gc=950, setclass=36, lookup=482 | no crash, **no TSan report** | 0 |

This is the expected negative and it does not weaken the static case. Stop-the-world makes the flag stores themselves race-free by construction, so there is no data race for TSan to see; what is broken is the *pairing* of the version-tag invalidation with the flag flip, a logical atomicity property no sanitizer models. `ASSERT_TYPE_LOCK_HELD()` cannot catch it either, for the reason given above. Confirming this would need instrumentation inside `_PyCriticalSection_SuspendAll`, which is out of scope for a static slice.

`Objects/typeobject.c` is **byte-identical** between the target ref `4f3be1b5777` and the build ref `a1d580430c8` (`git diff` over the file is empty), so the builds exercise exactly the reviewed code.

---

## 4. Clean negatives (confident, with reasons)

These were audited and are **not** findings. Each is recorded so the next run does not re-litigate.

**No lock is held across arbitrary Python — except one deliberate, documented case.**

- **`__set_name__` (`:12276`) and `__init_subclass__` (`:12322`)** are called from `type_new_impl` at `:4971` and `:4975`. No `BEGIN_TYPE_LOCK` region spans either line — the nearest pairs are `:3522..3524` and `:5934..5961`. Both user hooks run fully unlocked, by design, at the end of type construction. Clean.
- **A user `__set__` on a metatype descriptor** (`type_setattro`, `f(descr, (PyObject *)type, value)` at `:6807`) runs before any lock is taken; the first acquisition in that function is at `:6817`. Clean.
- **A user `__eq__`/`__hash__` during the type-dict update is structurally impossible.** `type_update_dict` (`:6694-6723`) documents it: *"Because we have an exact unicode and our dict has exact unicodes we know that this will all complete without releasing the locks."* The invariant is enforced at the call site — `type_setattro` interns `name` and hard-fails if interning does not produce an interned exact-unicode (`:6768-6784`). Clean, and well-reasoned.
- **A user `mro()` *does* run under TYPE_LOCK** — `mro_invoke` at `:3603` calls `call_method_noarg((PyObject *)type, &_Py_ID(mro))` with `ASSERT_TYPE_LOCK_HELD()` asserted four lines above. This is **deliberate and documented**, not an oversight: `:3601` says *"This is potentially re-entrant"* and `:3636-3652` spells out the re-entrancy protocol. TYPE_LOCK is designed to be suspendable precisely so this is safe — which is *why* `type_lock_prevent_release()` had to be invented for the cases where suspension is not acceptable. **ACCEPTABLE — reporting it as a defect would relitigate a known design.** It does sharpen §3: the droppable form is the default, so any region needing atomicity must opt out explicitly.

**No `__del__` can run under a lock (CPY-0028 shape absent).** A sweep of every locked region for `Py_DECREF` / `Py_XDECREF` / `Py_CLEAR` / `Py_SETREF` (`scratchpad/decref_under_lock.py`) returns **zero hits across all 25 regions**. `type_setattro` demonstrates the discipline positively: `type_update_dict` deliberately *hands `old_value` back to the caller* rather than dropping it, and the `Py_XDECREF(old_value)` happens at `:6845`, after `END_TYPE_DICT_LOCK()`. CPY-0028's shape (an `ob_mutex` copied or held while a destructor runs) does not transfer to this file.
*Limitation:* the sweep is textual over each region, so it does not follow callees. I hand-checked the significant ones (`type_update_dict`, `update_slot_after_setattr`, `type_ready`); a deep transitive sweep is out of scope for an intra-function pass.

**`PyMutex` usage is correct.** All 3 pairs are tight and straight-line with no branch between acquire and release: `managed_static_type_state_init` (`:323/:326`), `managed_static_type_state_clear` (`:416/:422`), and `type_add_common_descriptor` (`:4630/:4638`, where the `if (!descr)` error branch is correctly placed *after* the unlock). No `PyMutex` can leak — which matters more than a critical section, since a `PyMutex` is not released by scope exit.

**Deferred slot-update queue (`:3809-3941`) — correct, not reported.** Confirming the `ft-race-scanner`'s conclusion in one line: the design answer is stop-the-world rather than a lock, rationale at `:65-77` and `:3790-3806`, and `apply_type_slot_updates` is one of the 5 correctly-guarded sites. Its adjacent genuine defect (`fixup_slot_dispatchers` plain stores, gh-151377, PR gh-151394 unmerged) is owned by that agent and not duplicated here.

**`managed_static_type_state` lock protocol (`:228-522`).** The `is_readying:518` race itself is owned by the concurrent FT-race agent and is not re-litigated. The surrounding *lock protocol* is sound from this rule's perspective: `_PyStaticType_GetState`/`managed_static_type_state_get` are pure reads, and the two mutating functions take `interp->types.mutex` directly and correctly (above). The builtin path deliberately takes no mutex because it runs single-threaded during interpreter init.

---

## 5. Summary

- **Vocabulary in scope:** 2 `Py_BEGIN_CRITICAL_SECTION`, 3 `PyMutex_Lock`, 3 `PyMutex_Unlock`, 0 `LOCK_WEAKREFS` — plus **21 `BEGIN_TYPE_LOCK`, 1 `BEGIN_TYPE_DICT_LOCK`, 9 `types_stop_world`, 5 `type_lock_prevent_release`** that the scanner cannot see.
- **Functions with critical sections:** 2 resolved by the scanner; **~22 actual**.
- **FIX (leaked lock):** **0** — and this is an *earned* zero, hand-verified across all 25 regions.
- **CONSIDER:** **2** — `types_stop_world()` under TYPE_LOCK without `type_lock_prevent_release()` (`:1745`, `:12523`).
- **Verdict:** constructs present; all verified clean on the leak and two-object-nesting shapes; 2 lock-atomicity defects found by reading that the rule is structurally incapable of detecting.
- **Toolkit:** 4 defects, ranked in §1. The highest-value fix is same-TU `#define` alias resolution; the second is modelling the `stop_the_world` family that is already sitting unused in `data/lock_macros.json`, since that is where both findings live.

**Artifacts:** `scratchpad/audit_typelock.py` (region pairing), `scratchpad/decref_under_lock.py` (destructor-under-lock sweep), `scratchpad/stress_typelock.py` (reproduction attempt).
