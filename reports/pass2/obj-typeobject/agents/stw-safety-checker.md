# StopTheWorld safety — `Objects/typeobject.c` PASS 2

**Slice:** `obj-typeobject` pass 2 (informed-explore) · **Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c`, all 11 stop-the-world regions
**Builds:** `~/projects/python_build_matrix/builds/{debug,release}-{gil,ft}-nojit{,-asan}` @ `a1d580430c8`.
`git diff 4f3be1b5777 a1d580430c8 -- Objects/typeobject.c Objects/dictobject.c Objects/weakrefobject.c Python/pystate.c Python/critical_section.c Include/internal/pycore_critical_section.h Include/internal/pycore_weakref.h` is **empty** — every file cited below is byte-identical between the target ref and the matrix builds.

---

## 0. The structural zero — stated plainly

`scan_stw_safety.sample.json` reports `findings: 0`, `stw_functions: 3`, `functions_analyzed: 459`.

**That zero is structural, not earned.** The rule keys on the literal token `_PyEval_StopTheWorld` appearing in a function body. This file has **11** stop-the-world regions; **9 of them go through the file-local wrapper `types_stop_world()` (`/home/danzin/projects/cpython/Objects/typeobject.c:123-130`)**, which hides the token. The 3 the scanner saw are the wrapper itself plus the two raw callers (`type_set_name:1546`, `type_set_qualname:1581`). Measured region recall: **27%** (3/11), and both of the two genuine violations in this file sit in regions the rule never opened.

I built the proposed fix as a prototype and ran it (§6). With file-local wrapper resolution the same scanner reports **11/11 regions and 8 findings on this file, 2 of them high-confidence — and both of those 2 are real bugs.** One is pass 1's FIX (CPY-0073); the other is finding **[FIX-1]** below, which pass 1 explicitly ruled ACCEPTABLE.

Everything in this file is `Py_GIL_DISABLED`-only *as an STW question* — `types_stop_world()` / `types_start_world()` are empty macros on the GIL build (`:193-194`). **[FIX-2]** is the exception: it reproduces on the default GIL build, because the thing that made it safe was deleted along with a comment explaining why it was needed.

---

## 1. Census of all 11 regions

| # | region (stop → start) | function | under `TYPE_LOCK` | `prevent_release` | what runs inside | verdict |
|---|---|---|---|---|---|---|
| 1 | `:669` → `:673` | `set_tp_mro` (650) | yes (caller's) | **yes** | `self->tp_mro = mro` | ACCEPTABLE |
| 2 | `:1572` → `:1576` | `type_set_name` (1546) — **raw** | no | n/a | `tp_name` + `ht_name` store; `Py_DECREF(old)` deferred past Start | ACCEPTABLE (but see CONSIDER-2) |
| 3 | `:1598` → `:1601` | `type_set_qualname` (1581) — **raw** | no | n/a | `ht_qualname` store; DECREF deferred | ACCEPTABLE (but see CONSIDER-2) |
| 4 | `:1745` → `:1750` | `type_set_abstractmethods` (1715) | yes 1743 | **no** | `type_add_flags` / `type_clear_flags` | CONSIDER-1 (= CPY-0082) |
| 5 | `:1939` → `:1942` | `type_set_bases_unlocked` (1928) | yes | **yes** | `set_tp_bases` + `tp_base` store, refs pre-INCREF'd by caller | ACCEPTABLE |
| 6 | `:2005` → `:2008` | `type_set_bases_unlocked` (bail) | yes | **yes** | same, rollback direction | ACCEPTABLE |
| 7 | `:3923` → `:3925` | `apply_type_slot_updates` (3901) | yes | **yes** | `apply_slot_updates` — pure stores, `assert(types_world_is_stopped())` | **ACCEPTABLE — the exemplar** |
| 8 | `:6457` → `:6460` | `_PyType_SetFlags` (6452) | no | n/a | one `tp_flags` store | ACCEPTABLE as STW |
| 9 | `:6522` → `:6524` | `_PyType_SetFlagsRecursive` (6505) | yes 6508 | **yes** | `set_flags_recursive` → `_PyType_GetSubclasses` → `PyList_New`/`PyList_Append`, failure discarded | **CPY-0073 (pass 1 FIX) — confirmed**; plus ACCEPTABLE-2 below |
| 10 | `:7823` → `:7828` | `object_set_class` (7799) | no | n/a | `object_set_class_world_stopped` → **arbitrary Python** | **[FIX-1]** + **[FIX-2]** |
| 11 | `:12523` → `:12525` | `PyType_Freeze` (12505) | yes 12522 | **no** | `type_add_flags` | CONSIDER-1 (= CPY-0082) |

No region has a `goto` or early `return` that skips its `StartTheWorld`. I walked all 11, including the `undo:`/`bail:` ladder at `:1984-2020` and the `if (!unique)` pairing at `:7821-7829` where `unique` is cached in a local so the stop and start cannot disagree. Regions 1, 5, 6, 7, 8 and 11 are pure field stores (`type_set_flags` at `:454-459` is `ASSERT_WORLD_STOPPED_OR_NEW_TYPE` + one assignment; `set_tp_bases` at `:598-619` is asserts + one assignment) and are unambiguously clean.

The exemplar is worth restating because it is *this file's own* statement of the contract: `queue_slot_update` (`:3859`) does the `PyMem_Malloc` + `PyErr_NoMemory` **before** the world stops, and `apply_slot_updates` (`:3883`) then does nothing but `*(item->slot_ptr) = item->slot_value;`. That is `Python/gc_free_threading.c:2223` expressed in `typeobject.c`.

---

## 2. Findings

### [FIX-1] Arbitrary Python executes inside `object_set_class`'s stop-the-world region — `same_slots_added` (`/home/danzin/projects/cpython/Objects/typeobject.c:7609`)

**What.** The region opened at `:7823` reaches user Python:

```
object_set_class:7823          types_stop_world()
  object_set_class_world_stopped:7700
    compatible_for_assignment:7763
      same_slots_added:7588
        PyObject_RichCompareBool(slots_a, slots_b, Py_EQ)   :7609   <-- runs Python
```

`slots_a`/`slots_b` are the two types' `ht_slots` tuples. **Pass 1 ruled this ACCEPTABLE** on the grounds that "`ht_slots` is a tuple of strings … the compare resolves to `tuple_richcompare` → `unicode_richcompare`, all C". That is wrong on one word. `ht_slots` is populated at `:4923` from `ctx->slots`, which `type_new_copy_slots` (`:4250`) builds by passing each entry through `valid_identifier` (`PyUnicode_Check`, *not* `CheckExact`) and then `_Py_Mangle`, which returns `Py_NewRef(ident)` unchanged for any name not starting with `__`. **A `str` subclass survives into `ht_slots` intact**, and `tuple_richcompare` then dispatches its `__eq__` through `slot_tp_richcompare` → the eval loop.

CPython asserts this exact invariant, and the assertion names this file:

```c
/* Python/ceval.h:141-150 */
static void check_invalid_reentrancy(void) {
#if defined(Py_DEBUG) && defined(Py_GIL_DISABLED)
    // In the free-threaded build, the interpreter must not be re-entered if
    // the world-is-stopped.  If so, that's a bug somewhere (quite likely in
    // the painfully complex typeobject code).
    PyInterpreterState *interp = _PyInterpreterState_GET();
    assert(!interp->stoptheworld.world_stopped);
#endif
}
```
called from `Python/ceval.c:1232`.

**Trigger (pure Python, no C API, no crafted objects):**

```python
class MyStr(str):
    def __eq__(self, other): ...; return str.__eq__(self, other)
    def __ne__(self, o): return not self.__eq__(o)
    def __hash__(self): return str.__hash__(self)
class Base: pass
class A(Base): __slots__ = (MyStr("x"),)
class B(Base): __slots__ = (MyStr("x"),)
a = A(); keep = a          # refcount > 1 -> !unique -> the world IS stopped
a.__class__ = B            # -> MyStr.__eq__ runs with every other thread suspended
```

**Reproduced.**

| build | `__eq__` body | result |
|---|---|---|
| `debug-ft-nojit` (`PYTHON_GIL=0`) | *anything* | **SIGABRT** — `Python/ceval.h:148: check_invalid_reentrancy: Assertion '!interp->stoptheworld.world_stopped' failed` |
| `release-ft-nojit` (`PYTHON_GIL=0` **and** `PYTHON_GIL=1`) | `gc.collect()` | **permanent hang** |
| `release-ft-nojit` (both GIL settings) | `with some_lock:` held by a helper thread | **permanent hang** (the holder is suspended and can never release) |
| `release-ft-nojit` (both GIL settings) | `SomeClass.__name__ = "x"` | **permanent hang** — nested `_PyEval_StopTheWorld` self-deadlocks on `stw->mutex` |
| `release-ft-nojit` (both GIL settings) | `other.__class__ = C` | **permanent hang** — same, via the wrapper |
| `release-gil-nojit` | any of the above | clean (STW macros are no-ops) |

`gc.collect()` is the important one: it needs no adversarial intent at all, and it hangs the interpreter forever. Note the hang also occurs with `PYTHON_GIL=1`, i.e. on a stock `python3.16t` in its **default** configuration — `types_stop_world()` is compiled in for every `Py_GIL_DISABLED` build regardless of the runtime GIL setting.

**Why the region deadlocks rather than merely misbehaving.** `stop_the_world` (`/home/danzin/projects/cpython/Python/pystate.c:2453`) takes `PyMutex_Lock(&stw->mutex)`, which is not recursive; a nested stop from the same thread blocks on a mutex it already holds. And any other thread the Python code needs is `_Py_THREAD_SUSPENDED` until `start_the_world` runs, which is exactly what the Python code is preventing.

**Guarded twin.** The *same helper*, called from the *other* caller, outside any STW region: `type_set_bases` (`:2021`) → `type_check_new_bases` → `compatible_for_assignment(type, base, "__bases__", 0)` → `same_slots_added`. `__bases__` assignment holds only `TYPE_LOCK`; `__class__` assignment wraps the identical call in stop-the-world. One caller is safe, the other is not, and the difference is a decision made in `object_set_class`, not in the helper.

**Fix.** Two options, in preference order:
1. Make `same_slots_added` not run Python: compare the two `ht_slots` tuples element-wise with `_PyUnicode_Equal` (they are validated identifiers by construction). **This also discharges CPY-0078**, which is the tri-state-collapse half of the same line.
2. Hoist `compatible_for_assignment` out of the stop-the-world window in `object_set_class` — do the compatibility check with the world running, then stop the world only for the `Py_SET_TYPE` + dict-detach, mirroring `queue_slot_update`/`apply_slot_updates` (`:3859`/`:3883`).

**Prior art:** none. `gh api -X GET search/issues -f q='repo:python/cpython same_slots_added stop the world'` → 0; `…'check_invalid_reentrancy'` → 0; `…'__class__ assignment free-threading hang'` → 0.

**Reproducers:** `reports/pass2/obj-typeobject/repro/stw_slots_probe.py`, `reports/pass2/obj-typeobject/repro/stw_nested.py` (`none|name|class|lock` variants), `reports/pass2/obj-typeobject/repro/stw_gc.py`.

---

### [FIX-2] `object_set_class` over-decrements a borrowed `oldto` across a re-entrant call — heap use-after-free on the **default GIL build** (`/home/danzin/projects/cpython/Objects/typeobject.c:7825` + `:7832`)

This is the answer to the brief's item 5 (*"audit whether every operation is safe under the weaker condition"*). The answer is no — and the operation that breaks is not inside `object_set_class_world_stopped` at all, it is the borrowed pointer the caller holds *across* it.

```c
    int unique = _PyObject_IsUniquelyReferenced(self);      // :7821
    if (!unique) { types_stop_world(); }                    // :7822-7824
    PyTypeObject *oldto = Py_TYPE(self);                    // :7825   BORROWED, no INCREF
    int res = object_set_class_world_stopped(self, newto);  // :7826   can run Python (FIX-1)
    if (!unique) { types_start_world(); }                   // :7827-7829
    if (res == 0) {
        if (oldto->tp_flags & Py_TPFLAGS_HEAPTYPE) {
            Py_DECREF(oldto);                               // :7832
        }
```

Via **[FIX-1]**'s edge, `:7826` can execute Python that re-enters `object_set_class` on the same object and retypes it `A → C`, releasing the reference `oldto` still names. The outer frame then does `Py_SET_TYPE(self, B)` (leaking `C`'s reference) and `Py_DECREF(oldto)` on `A` a **second** time.

**Guarded twin — the deleted lines.** Before `3bfc9c831ad9` (gh-120198, 2024-07-11) the code read:

```c
        Py_BEGIN_CRITICAL_SECTION(self);
        // The real Py_TYPE(self) (`oldto`) may have changed from
        // underneath us in another thread, so we re-fetch it here.
        oldto = Py_TYPE(self);
        Py_SET_TYPE(self, newto);
        Py_END_CRITICAL_SECTION();
```

The re-fetch immediately before `Py_SET_TYPE` closed the window on **every** build. gh-120198 deleted it and its comment, on the reasoning that stop-the-world makes it unnecessary. That reasoning covers *another thread*; it does not cover *re-entrancy on this thread*, and on the GIL build `types_stop_world()` is an empty macro so it covers nothing at all. `1d091a336e60` (gh-145566, 2026-03-06) then added the `unique` fast path, removing the STW protection on the free-threaded build too. **The guarded twin is literally the three deleted lines.**

**Reproduced — pure Python, single-threaded, default GIL build.**

| build | result |
|---|---|
| `release-gil-nojit` | refcount delta on `A` is **−2** where −1 is correct; `C` leaked +1. With 2 iterations of the retype loop: **SIGSEGV (rc=139)** |
| `release-gil-jit` | identical refcount corruption |
| `debug-gil-nojit` | **Fatal Python error** — `Python/gc.c:96: gc_decref: Assertion "gc_get_refs(g) > 0" failed: refcount is too small`, `object repr: <class '__main__.A'>`, `object refcount: 2` |
| `release-gil-nojit-asan` | **`AddressSanitizer: heap-use-after-free`**, READ of size 4 in `Py_INCREF` ← `PyDict_GetItemRef` ← `_PyEval_LoadName` (loading the global `A`); freed by `_Py_Dealloc` ← `Py_DECREF` ← `PyObject_SetAttr` (the `__class__` assignment) |
| `debug-ft-nojit` (`PYTHON_GIL=0`) | masked — aborts earlier on **[FIX-1]**'s `check_invalid_reentrancy` |
| `release-ft-nojit` (`PYTHON_GIL=0`) | masked — hangs on **[FIX-1]**'s nested STW |

**Fix.** `Py_INCREF(oldto)` at `:7825` and `Py_DECREF` it unconditionally after `:7833`, **or** restore the re-fetch immediately before `Py_SET_TYPE` inside `object_set_class_world_stopped:7790`. Fixing **[FIX-1]** (option 1 above) also closes this, since the only Python-running edge disappears — but the borrowed-`oldto` shape should be fixed on its own merits: it is one re-entrant call away from returning.

**Classification note.** This is `borrowed-ref-across-call` (the crown-jewel shape) and belongs to `refcount-auditor`'s class, not mine. `scan_refcounts` did not flag it because it keys `borrowed_field_deref_across_call` on the four `lookup_tp_*` accessors and `Py_TYPE(self)` is a plain struct read — the exact recall gap `include_map.md` §4.3 predicted. I own the report because the STW audit of item 5 is what exposed it.

**Reproducers:** `reports/pass2/obj-typeobject/repro/setclass_oldto_uaf.py` (minimal, ~35 lines), `reports/pass2/obj-typeobject/repro/setclass_oldto_uaf_free.py` (drives it to the free), `reports/pass2/obj-typeobject/repro/asan.txt` (ASan report).

---

### [CONSIDER-1] The `type_lock_prevent_release` asymmetry — confirming CPY-0082, with the mechanism nailed down and a per-site decision

CPY-0082 is recorded static-confirmed; per informed-mode rules I confirm rather than re-litigate, and add the two things the brief asked for: *why* the guard is required, and a decision for each of the two omitting sites.

**Mechanism (this was not previously pinned down).** `stop_the_world` (`/home/danzin/projects/cpython/Python/pystate.c:2441`) acquires two locks *before* it stops anything:

```c
    _PyRWMutex_RLock(&runtime->stoptheworld_mutex);   // :2451
    PyMutex_Lock(&stw->mutex);                        // :2453
```

`PyMutex_Lock` is `_PyMutex_LockTimed(m, -1, _PY_LOCK_DETACH)` (`/home/danzin/projects/cpython/Python/lock.c:654-657`). On contention the caller **detaches**, and `detach_thread` (`Python/pystate.c:2322-2324`) calls `_PyCriticalSection_SuspendAll`, which unlocks every mutex held through a critical section — including `TYPE_LOCK`. When the thread re-attaches, `_PyCriticalSection_Resume` (`pystate.c:2303`) silently re-acquires it. So the caller believes it held `TYPE_LOCK` continuously across `types_stop_world()` and did not. The wait loop itself is safe (`PyEvent_WaitTimed(..., detach=0)` at `:2488`); the window is entirely at the two acquisitions. `type_lock_prevent_release()` (`typeobject.c:143-163`) splices `TYPE_LOCK` out of the critical-section record so `SuspendAll` cannot release it — which is why the file's own comment at `:3906-3921` insists on it.

5 of the 7 STW-inside-`TYPE_LOCK` sites use it. Decision on the two that do not:

- **`type_set_abstractmethods:1745` — the worse of the two.** It calls `_PyType_Modified_Unlocked(type)` at `:1744` and *then* enters the unguarded stop. That is precisely the ordering `_PyType_SetFlagsRecursive`'s comment (`:6519-6520`) says requires the guard: *"Keep TYPE_LOCK held while waiting for stop-the-world so no thread can reassign a version tag before the flag update."* In the gap, another thread can take `TYPE_LOCK` and run `assign_version_tag` on the type we just invalidated, so `Py_TPFLAGS_IS_ABSTRACT` lands on a type carrying a version tag minted before the flag change. **The guarded twin is `_PyType_SetFlagsRecursive:6506-6527`, 4,760 lines away, which does the identical invalidate-then-stop sequence *with* the guard.** Adding `type_lock_prevent_release()`/`type_lock_allow_release()` around `:1745`/`:1750` is a two-line fix. CONSIDER (no repro; the window is a contended-mutex race).
- **`PyType_Freeze:12523` — lower severity.** It stops the world, sets `Py_TPFLAGS_IMMUTABLETYPE`, restarts, and only then invalidates (`:12527`). Losing `TYPE_LOCK` during the wait lets a concurrent `type_setattro` land a mutation on a type that is being frozen; the version-tag ordering itself survives because the invalidation happens after the lock is re-acquired. Independently, `check_immutable_bases` (`:12515`) runs entirely outside the lock, so `PyType_Freeze` already has a wider TOCTOU than this one. CONSIDER.

Both sites re-assert `ASSERT_TYPE_LOCK_HELD()` right after `types_start_world()` (`:1751`, `:12526`). That assertion cannot detect the gap — the critical section has already been resumed by then — and it is `Py_DEBUG`+FT+64-bit-only anyway (`:107-121`, `:188-191`, `:104`).

---

### [CONSIDER-2] The two raw `_PyEval_StopTheWorld` callers forgo the wrapper's nesting assertion (`:1572`, `:1598`)

`types_stop_world()` brackets the primitive with `assert(!types_world_is_stopped())` / `assert(types_world_is_stopped())` (`:126`, `:129`); `types_start_world()` mirrors it (`:135`, `:138`). `type_set_name` (`:1572`) and `type_set_qualname` (`:1598`) call `_PyEval_StopTheWorld(interp)` directly and are the only two sites in the file that do.

The regions themselves are clean (two pointer stores each, with `Py_DECREF(old_name)` deliberately deferred past `_PyEval_StartTheWorld`). The cost is the lost assertion. **Measured consequence:** in the `name` variant of `reports/pass2/obj-typeobject/repro/stw_nested.py`, a nested stop reached through `type_set_name` produces a *silent* permanent hang on `release-ft-nojit`. In that particular repro the debug build still catches it, but only because the path went through the eval loop and tripped `check_invalid_reentrancy` first. A nested stop reached without entering the eval loop — a C extension calling `PyType_Freeze` or `_PyType_SetFlags` from a `tp_dealloc` that fires inside an STW region — would have *no* debug-build diagnostic at these two sites, whereas at the other nine it would abort on `:126`.

Fix: route both through `types_stop_world()` / `types_start_world()`. Zero behaviour change on release, restores the debug canary. (CPY-0090 already covers the *performance* half of these two functions; this is the diagnostics half.)

---

### [ACCEPTABLE-1] `Py_BEGIN_CRITICAL_SECTION(dict)` at `:7778` inside the stopped world — the headline candidate is **killed**, with a stronger citation than pass 1's

The preflight nominated `Py_BEGIN_CRITICAL_SECTION(dict)` at `:7778`, inside the `!unique` window opened at `:7823`, as "the textbook STW-deadlock shape". It is not, and the reason is a dedicated early-out in the critical-section implementation:

```c
/* Python/critical_section.c:46-54, _PyCriticalSection_BeginSlow */
    // If the world is stopped, we don't need to acquire the lock because
    // there are no other threads that could be accessing the object.
    // Without this check, acquiring a critical section while the world is
    // stopped could lead to a deadlock.
    if (tstate->interp->stoptheworld.world_stopped) {
        c->_cs_mutex = NULL;
        c->_cs_prev = 0;
        return;
    }
```

The fast path (`_PyCriticalSection_BeginMutex`, `Include/internal/pycore_critical_section.h:107-117`) is `PyMutex_LockFast` — an uncontended CAS that cannot block. If it fails, control reaches `BeginSlow`, which returns without acquiring when the world is stopped. **A critical section can never block during STW.** Pass 1 reached the right verdict via `_PyCriticalSection_SuspendAll` (`Python/pystate.c:2323`); that is a true supporting fact but it is not the primary defence, because it only covers mutexes a suspended thread held *through a critical section*. The `world_stopped` early-out covers the case unconditionally.

The corollary stands and is the shape the scanner should key on: **a raw `PyMutex_Lock` outside a critical section during STW is a genuine deadlock** — nothing suspends it and nothing early-outs. See ACCEPTABLE-2.

Also verified in the same region: `_PyObject_MaterializeManagedDict_LockHeld` (`:7769`) and `_PyDict_DetachFromObject` (`:7779`) both open with `ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED` (`Objects/dictobject.c:7360`, `:8033`, macro at `:175`), which is discharged on the STW path by the world stop and on the `unique` path by the macro's `Py_REFCNT(op) != 1` escape hatch (`Include/internal/pycore_critical_section.h:64-67`) — `_PyObject_IsUniquelyReferenced` guarantees exactly that. The three `PyErr_Format` calls in the region (`:7757`, `:7635`, `:7688`) use `%s` on `char *` only, never `%R`/`%S`, so the repr machinery is never entered; `PyExc_TypeError` is a built-in with no `__del__`, and `object_set_class` has returned on every earlier error so no exception is pending — the conditionally-safe precondition holds.

### [ACCEPTABLE-2] A raw `PyMutex_Lock` **is** reachable from an STW region — correcting pass 1's census

Pass 1 concluded: *"This file has exactly three such sites: `:323`, `:416`, `:4630`. None is reachable from an STW region."* That census counted only the literal `PyMutex_Lock` spellings in `typeobject.c`. There is a fourth acquisition, hidden behind a macro in another header, and it **is** inside a stop-the-world region:

```
_PyType_SetFlagsRecursive:6522   types_stop_world()
  set_flags_recursive:6493         _PyType_GetSubclasses(self)
    _PyType_GetSubclasses:797        type_from_ref(ref)            typeobject.c:218
      _PyWeakref_GET_REF                LOCK_WEAKREFS(obj)         pycore_weakref.h:23-24
        == PyMutex_LockFlags(&interp->weakref_locks[hash(obj)], _Py_LOCK_DONT_DETACH)
```

This is a raw `PyMutex`, not a critical section, so neither the `world_stopped` early-out nor `_PyCriticalSection_SuspendAll` applies. If a suspended thread held that lock, `_PyType_SetFlagsRecursive` would hang the interpreter.

**It discharges, and the reason is worth recording because it is fragile.** A thread can only be suspended for STW at a safe point (eval breaker) or while detached. I read every `LOCK_WEAKREFS` window in the tree — `Objects/weakrefobject.c:48-55`, `:434-446`, `:455-457`, `:1037-1044`, `:1066-1079`, `:1132-1136`, `Modules/_weakref.c:87-107` — and none contains a safe point or a detaching call. The widest is `:434-446`, which calls `allocate_weakref` under the lock; on the free-threaded build allocation does not run GC synchronously and does not detach. So no thread can be parked while holding a weakref list lock, and the acquisition during STW always succeeds immediately.

This is one refactor away from being a hang, it is invisible to every current rule, and it is exactly the pattern pass 1 said the scanner *should* be flagging. See §6, proposal 2.

### [ACCEPTABLE-3] Regions 1, 5, 6, 7, 8, 11 — pure field stores

`self->tp_mro = mro`, `self->tp_bases = bases` + `tp_base`, `tp_flags` via `type_set_flags` (`:454-459`), and `apply_slot_updates`'s `*(item->slot_ptr) = item->slot_value`. All refcount work is either `Py_NewRef` on an already-owned object (atomic incref, permitted) or deferred past `StartTheWorld` (`:1577`, `:1602`). Nothing allocates, nothing can raise, nothing takes a lock.

### Confirmed, not re-litigated

- **CPY-0073** (`set_flags_recursive` allocates inside region 9 and discards the failure) — still present at `:6493-6496` / `:776-807`, unchanged. The prototype scanner fix (§6) recovers it automatically as `stw_unsafe_call` at `:6523`.
- **CPY-0090** (`type_set_name`/`type_set_qualname` stop the world unconditionally; `object_set_class` got the fast path and they did not) — still present. Pass 1's POLICY; unchanged. Note that **[FIX-2]** shows the fast path they were denied is itself unsound as written, which is an argument for fixing `object_set_class` before propagating its pattern.
- **CPY-0082** — see CONSIDER-1.
- **CPY-0078** (`same_slots_added` collapses `PyObject_RichCompareBool`'s tri-state at `:7609`) — same line as **[FIX-1]**. Preferred fix option 1 discharges both.

---

## 3. Item 5 in full: what `object_set_class_world_stopped` still relies on when the world is *not* stopped

`1d091a336e60` (2026-03-06) made a function whose name asserts an invariant run without that invariant on the `_PyObject_IsUniquelyReferenced(self)` fast path. Per-operation audit:

| operation | line | needs | holds on the `unique` path? |
|---|---|---|---|
| `Py_TYPE(self)` read | 7702 | no concurrent retype of `self` | yes — only this thread has a reference |
| `PyType_IsSubtype(newto, …)` | 7753-7754 | stable `newto->tp_mro` | yes — `set_tp_mro(…, initial=0)` is the only live writer and it stops the world (`:667-675`), so it cannot land while we run C code without a safe point |
| `compatible_for_assignment` field reads | 7763 | stable `tp_free`/`tp_flags`/`tp_dictoffset`/`tp_base` | yes, same argument |
| **`same_slots_added` → `PyObject_RichCompareBool`** | 7609 | — | **runs Python, and here there is no STW to prevent re-entrancy → [FIX-2]** |
| `_PyObject_MaterializeManagedDict_LockHeld` | 7769 | world stopped **or** `self` locked | yes — via the assertion's `Py_REFCNT(op) != 1` escape hatch |
| `Py_BEGIN_CRITICAL_SECTION(dict)` + `_PyDict_DetachFromObject` | 7778-7780 | `dict` locked | yes — the lock is real here (the world is running), and `dict` is the object that needs it |
| `Py_SET_TYPE(self, newto)` | 7790 | no concurrent observer of `ob_type` | yes, modulo the TOCTOU below |

**One residual, reported as an observation rather than a finding.** `_PyObject_IsUniquelyReferenced` (`Include/internal/pycore_object.h`) reads `ob_ref_local == 1 && ob_ref_shared == 0` with relaxed atomics. Between that read at `:7821` and `Py_SET_TYPE` at `:7790`, another thread can obtain the first shared reference to `self` through a weakref (`_PyWeakref_GET_REF` → `_Py_TryIncref`), since a weakref holds no strong reference and therefore does not disturb either counter. The window is small and the same idiom is used at ~20 sites tree-wide, but `object_set_class` is the most exposed of them: its operand is a heap-type instance (weakref-able, unlike the `int`/`tuple`/`bytes` operands of the other sites) and the mutation is a type change plus a dict materialisation published without the object's critical section. I did not attempt a stress reproduction — the two confirmed bugs above were the better use of the budget — and I would not report it as a finding without one. It is recorded here so the next reviewer does not have to re-derive it.

---

## 4. Item 4 in full: can the raw callers nest, and what happens?

Yes, and I reproduced it. `type_set_name:1572` and `type_set_qualname:1598` bypass `types_stop_world`'s `assert(!types_world_is_stopped())`. Reached from inside another STW region — which **[FIX-1]** makes possible from pure Python — `_PyEval_StopTheWorld` → `stop_the_world` → `PyMutex_Lock(&stw->mutex)` on the mutex the outer stop already holds. `PyMutex` is not recursive. Result on `release-ft-nojit` (both `PYTHON_GIL=0` and `PYTHON_GIL=1`): permanent hang, `faulthandler` traceback pointing at the `Victim.__name__ = "renamed"` line inside `__eq__`. See CONSIDER-2.

---

## 5. Summary table

| # | class | site | status |
|---|---|---|---|
| 1 | **FIX** | `Objects/typeobject.c:7609` (`same_slots_added`), region opened `:7823` | reproduced — SIGABRT on `debug-ft-nojit` (`check_invalid_reentrancy`); permanent interpreter hang on `release-ft-nojit` at both GIL settings, from 4 independent pure-Python triggers incl. `gc.collect()` |
| 2 | **FIX** | `Objects/typeobject.c:7825` + `:7832` (`object_set_class`) | reproduced — ASan heap-use-after-free and SIGSEGV on `release-gil-nojit`, Fatal Python error on `debug-gil-nojit`; **default GIL build, single-threaded**; regression from gh-120198 (2024-07-11) |
| 3 | **CONSIDER** | `Objects/typeobject.c:1745` (`type_set_abstractmethods`), `:12523` (`PyType_Freeze`) | confirms CPY-0082; mechanism pinned to `Python/pystate.c:2453` + `Python/lock.c:656`; `:1745` is the worse site |
| 4 | **CONSIDER** | `Objects/typeobject.c:1572`, `:1598` | raw stop bypasses the wrapper's nesting assert; measured silent hang where the other nine sites would abort |
| 5 | **ACCEPTABLE** | `Objects/typeobject.c:7778` (`Py_BEGIN_CRITICAL_SECTION(dict)` during STW) | headline candidate killed — `Python/critical_section.c:46-54` |
| 6 | **ACCEPTABLE** | `LOCK_WEAKREFS` via `:6522 → :797 → :218` | a raw `PyMutex` **is** reachable from an STW region; discharges only because no `LOCK_WEAKREFS` window contains a safe point. Corrects pass 1's census |
| 7 | **ACCEPTABLE** | regions 1, 5, 6, 7, 8, 11 | pure field stores; no region skips its `StartTheWorld` on any path |
| — | confirmed | CPY-0073, CPY-0078, CPY-0082, CPY-0090 | all still present, not re-litigated |

---

## 6. TOOLKIT ASSESSMENT

### 6.1 The structural-zero verdict, first

**`scan_stw_safety` reported 0 findings on this file over a 27% denominator, and both real violations were inside the 73% it never opened.** The rule resolved 3 of 11 regions because it matches the literal token `_PyEval_StopTheWorld` in a function body, and this file routes 9 of its 11 regions through the file-local wrapper `types_stop_world()` (`Objects/typeobject.c:123`). Pass 1 correctly diagnosed this and proposed the fix; pass 2 confirms the diagnosis, quantifies the cost (**two reproduced bugs, one of which is a use-after-free on the default build**), and ships a measured prototype of the fix.

A zero from this rule on any file containing an STW wrapper must be read as *not analysed*, not as *clean*.

### 6.2 The scanner fix — built, run, measured

**Proposal 1 (high value, ~40 lines): resolve file-local trivial wrappers around the STW primitives and treat them as region delimiters.** This is the same class of fix that took `scan_lock_discipline` from 2 to 23 critical-section functions on this file via `resolve_local_lock_macros` — except the construct here is a `static void` *function*, not a `#define`, so `#define`-aliasing cannot reach it.

**What it should key on.** For each function in the file, strip `assert(...)` statements from the body, then collect the remaining called identifiers minus control-flow keywords and the thread/interpreter-state getters (`_PyInterpreterState_GET`, `_PyRuntimeState_GET`, `_PyThreadState_GET`). If the remaining set is non-empty and is a subset of `{_PyEval_StopTheWorld, _PyEval_StopTheWorldAll}`, the function is a **stop alias**; likewise for the Start primitives. Add the discovered aliases to the per-file `_STOP_RE`/`_START_RE` used by `_find_stw_regions`, and to `_STW_CONTROL` so the alias call itself is not flagged as a finding. Nothing else in the scanner needs to change — the existing `_propagate_stw_safety` call graph already crosses function boundaries, so once the region is opened, `object_set_class` → `object_set_class_world_stopped` → `compatible_for_assignment` → `same_slots_added` → `PyObject_RichCompareBool` resolves to `unsafe` on its own.

**Prototype:** `reports/pass2/obj-typeobject/repro/stw_wrapper_prototype.py` (monkeypatches the shipped module per file; prints a before/after envelope).

**Measured recall change.**

| scope | STW regions before → after | findings before → after | high-confidence before → after |
|---|---|---|---|
| `Objects/typeobject.c` | **3 → 11** (27% → **100%**) | 0 → 8 | 0 → **2** |
| `Objects/` | 8 → 16 | 13 → 21 | 0 → 2 |
| `Python/` | 35 → 35 | 68 → 68 | 5 → 5 |
| `Modules/` | 7 → 7 | 5 → 5 | 0 → 0 |
| **tree-wide** | **50 → 58 (+16%)** | **86 → 94** | **5 → 7 (+40%)** |

The wrapper detector fired on exactly **2 functions tree-wide** — `types_stop_world` and `types_start_world`, both in `typeobject.c` — and produced **zero** spurious wrapper detections across `Python/` and `Modules/`. So the tree-wide recall gain is modest in absolute terms and entirely concentrated in one file; but that file is the campaign's tier-A slice, its recall there goes from 27% to 100%, and the two recovered high-confidence findings are the two real bugs (`_PyType_SetFlagsRecursive:6523` = CPY-0073, and `object_set_class:7826` = **[FIX-1]**/**[FIX-2]**).

**Precision after the fix, on this file:** 8 findings — 2 `stw_unsafe_call` (high), both true positives; 6 `stw_unknown_call` (medium), all ACCEPTABLE (`type_add_flags` ×3, `set_tp_bases` ×2, `apply_slot_updates`, all pure `tp_flags`/pointer stores). Medium confidence is the correct label for those: they are file-local `static` helpers the propagation left unclassified, and reading each takes under a minute. No precision regression.

**Residual blind spot to state in the envelope:** a wrapper defined in a *different* translation unit is still invisible, because `analyze()` gates on `if "_PyEval_StopTheWorld" not in source_text` before doing any work (`scan_stw_safety.py:437`). Detecting cross-file wrappers needs a two-pass design (collect aliases tree-wide, then scan). No such wrapper exists in CPython today, so this is a documentation item, not a code item.

### 6.3 Two further rules, both earned by this pass

**Proposal 2 — flag raw `PyMutex_Lock` inside an STW region; do *not* flag `Py_BEGIN_CRITICAL_SECTION`.** The rule's intuition is currently inverted for FT-era CPython. `Python/critical_section.c:46-54` makes a critical section a documented no-op while the world is stopped; a raw `PyMutex_Lock` has no such early-out and is the real deadlock. The vocabulary must include the macro spellings, not just the literal call: `LOCK_WEAKREFS` / `LOCK_WEAKREFS_FOR_WR` (`Include/internal/pycore_weakref.h:23-30`) expand to `PyMutex_LockFlags` and are the only instance in the tree that is actually reachable from an STW region (ACCEPTABLE-2). Pass 1 proposed the inversion; pass 2 supplies the live instance that makes it non-hypothetical.

**Proposal 3 — flag "the world is stopped and we entered the eval loop".** `Python/ceval.h:141-150` is CPython's own runtime check for exactly the property this agent audits, and its comment blames `typeobject.c` by name. A static approximation is cheap and would have caught **[FIX-1]** directly: inside an STW region, any transitive call that reaches a `tp_richcompare`/`tp_hash`/`tp_repr`/`tp_str`/`tp_call` dispatcher (`PyObject_RichCompare*`, `PyObject_Hash`, `PyObject_Repr`, `PyObject_Str`, `PyObject_Call*`, `PyIter_Next`) is a violation *unless* every operand is provably a built-in exact type. The data file already classifies these as unsafe; what is missing is the region that contains them, which Proposal 1 supplies. **A useful corollary check for the agent, not the scanner:** when a comment justifies such a call by asserting a type invariant (`":7221  ht_slots is a tuple of strings"`), verify the validator is `PyUnicode_CheckExact` and not `PyUnicode_Check`. That single word is the whole of **[FIX-1]**, and pass 1 accepted the comment at face value.

**Proposal 4 (carried forward from pass 1, still unimplemented and still worth it)** — "error discarded inside STW": a fallible call inside an STW region whose enclosing function returns `void`. That is CPY-0073's exact shape and is the inverse of the `gc_free_threading.c:2223` pattern the checker is built around.

### 6.4 Denominator honesty

After Proposal 1 this file's STW denominator is 11/11 and the zero would be earned. Before it, `stw_functions: 3` was the canary and it was reported without comment next to `functions_analyzed: 459`, which reads like a large denominator and is not one. **`stw_functions` is the only denominator that matters for this rule; the envelope should surface it as such**, and a run where `stw_functions` is small relative to the file's `grep -c 'stop_world\|StopTheWorld'` count should emit a `possible_wrapper_undercount` note.
