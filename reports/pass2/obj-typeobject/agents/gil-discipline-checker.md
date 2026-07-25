# GIL discipline / exclusivity — `Objects/typeobject.c` (pass 2)

**Slice:** `obj-typeobject` pass 2 (informed-explore) · **Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c`, 13,068 lines
**Builds:** `~/projects/python_build_matrix/builds/{debug,release}-{gil,ft}-nojit{,-asan}` @ `a1d580430c8`.
`git diff 4f3be1b5777 a1d580430c8 -- Objects/typeobject.c Objects/dictobject.c Python/critical_section.c Python/lock.c Python/pystate.c Modules/_testcapi/watchers.c Python/optimizer_analysis.c` is **empty** — every file cited below is byte-identical between the target ref and the builds used to reproduce.

---

## 0. Corrected verdict on my own zero — `rule_not_applicable`, not "blind spot"

`scan_gil_usage` reports **0 findings** with `vocabulary_resolved=0`, `vocabulary_tokens_seen=4`.
Verified in one step:

```
$ grep -c 'Py_BEGIN_ALLOW_THREADS\|Py_END_ALLOW_THREADS\|PyGILState_Ensure\|PyGILState_Release\|Py_BLOCK_THREADS\|Py_UNBLOCK_THREADS' Objects/typeobject.c
0
```

| verdict | correct? |
|---|---|
| "GIL discipline clean" | **no** — nothing was resolved |
| "the one genuine blind spot left on this file" | **no** — a blind spot is a construct that is *present and invisible* (that is `scan_stw_safety`, 9 of 11 regions). Here the construct is *absent*. |
| **`rule_not_applicable` / N/A for this file** | **yes** |

`typeobject.c` never releases the GIL through the `ALLOW_THREADS` mechanism, because it is not
I/O code. Its concurrency story is stop-the-world and `TYPE_LOCK` (design comment `:65-77`), for
which this rule has no vocabulary at all. The scanner's own envelope already says so —
`"rule_not_applicable": true` with a note telling the reader to establish whether the constructs
are genuinely absent or merely spelled differently. **They are genuinely absent.** I checked for
the "spelled differently" case too: there is no file-local `#define` wrapping `ALLOW_THREADS` or
`PyGILState_*` anywhere in the file (the only file-local concurrency macros are
`BEGIN_TYPE_LOCK` / `types_stop_world` / `type_lock_prevent_release`, none of which touch the GIL).

**Record correction:** the pass-1 entry describing this as a blind spot should read
*N/A — vocabulary structurally absent; do not certify, do not chase*. The remaining sections are
the part of this agent's remit that **does** apply, and it produced one reproduced
heap-use-after-free.

---

## 1. What applies here: the exclusivity census

The GIL-adjacent hazard in this file is not *releasing* exclusivity, it is *running arbitrary
Python while holding it*. I built an intra-file call graph, seeded it from every
`TYPE_LOCK` / `TYPE_DICT_LOCK` / stop-the-world region plus every function carrying
`ASSERT_TYPE_LOCK_HELD()`, propagated transitively, and matched user-code-capable calls.
(Script: `scratchpad/exclusivity2.py`; 34 regions, 459 functions.)

**Every user-Python-capable call reachable while exclusivity is held — the complete list:**

| site | call | exclusivity held | state broken across it | verdict |
|---|---|---|---|---|
| `_PyType_Modified_Unlocked` **:1222** | `cb(type)` — watcher callback (C, third-party) | `TYPE_LOCK` (+ dict CS on the setattr path) | live `PyDict_Next` cursor + borrowed `subclasses` at `:1195/:1201`; subclasses invalidated, `type` not yet | **FIX** (§2.1) |
| `_PyType_Modified_Unlocked` **:1223** | `PyErr_FormatUnraisable(... %R ...)` → `sys.unraisablehook` **and** `PyObject_Repr(type)` | same | same | **FIX** (§2.1) |
| `mro_invoke` **:3603** | `call_method_noarg(type, "mro")` — custom metaclass `mro()` | `TYPE_LOCK` | `tp_mro` | ACCEPTABLE (§4.1) |
| `mro_invoke` **:3613** | `PySequence_Tuple(mro_result)` — user `__iter__` | `TYPE_LOCK` | as above | ACCEPTABLE (§4.1) |
| `class_name` **:3263/:3264** | `PyObject_GetOptionalAttr(cls,"__name__")`, `PyObject_Repr(cls)` | `TYPE_LOCK` | `to_merge[]` borrowed tuples; local `set` cursor | ACCEPTABLE (§4.2) |
| `same_slots_added` **:7609** | `PyObject_RichCompareBool(slots_a, slots_b, Py_EQ)` | **world stopped** + `TYPE_LOCK` | whole STW region | ACCEPTABLE — confirmed, pass 1 §2.4 (§4.3) |

Two entries in that table are the same call site as the preflight's lead
(`object_set_class_world_stopped` → `same_slots_added` :7609) and pass 1 already disposed of it;
I re-checked the one premise it rests on and confirmed it (§4.3), one line, no re-litigation.

**Why "exclusivity held" is the right frame and not "deadlock":** `_PyCriticalSection_BeginSlow`
(`Python/critical_section.c:20-60`) *skips* locking when the requested mutex is already held by the
**topmost** critical section, so re-entering `BEGIN_TYPE_LOCK()` from the same thread does not
deadlock. It also does not protect anything: `TYPE_LOCK` excludes other *threads*, never a
re-entrant call on *this* thread. Every finding below is a re-entrancy bug that the GIL build has
too, which is why `TYPE_LOCK`'s presence in the FT arm is irrelevant to it.

---

## 2. Findings

### 2.1 [FIX] Re-entrancy through the type-watcher notification frees the `tp_subclasses` dict that `_PyType_Modified_Unlocked` is iterating — reproduced heap-use-after-free

**Site:** `Objects/typeobject.c:1195-1208` (the loop) / `:1222-:1225` (the re-entrancy source).

```c
    PyObject *subclasses = lookup_tp_subclasses(type);      // :1195  BORROWED, no strong ref
    if (subclasses != NULL) {
        Py_ssize_t i = 0;
        PyObject *ref;
        while (PyDict_Next(subclasses, &i, NULL, &ref)) {   // :1201  live cursor
            PyTypeObject *subclass = type_from_ref(ref);
            if (subclass == NULL) { continue; }
            _PyType_Modified_Unlocked(subclass);            // :1206  fires SUBCLASS's watchers
            Py_DECREF(subclass);
        }
    }
    ...
                PyType_WatchCallback cb = interp->type_watchers[i];
                if (cb && (cb(type) < 0)) {                 // :1222  third-party C callback
                    PyErr_FormatUnraisable(                 // :1223  sys.unraisablehook + %R repr
                        "Exception ignored in type watcher callback #%d for %R", i, type);
                }
```

The recursive call at `:1206` fires the **subclass's** watchers. A watcher that returns `-1`
reaches `PyErr_FormatUnraisable` at `:1223`, which runs arbitrary Python **twice over**: the `%R`
conversion calls `PyObject_Repr(type)` (user code whenever the metaclass defines `__repr__` in
Python), and the message is then handed to `sys.unraisablehook` (user code whenever the program
sets one). Both execute while the **outer** frame's `subclasses` pointer is borrowed and its
`PyDict_Next` cursor `i` is live.

That Python can reparent the subclass. `D.__bases__ = (object,)` runs
`type_set_bases` `:2032` → `type_set_bases_unlocked` `:1965` → `remove_all_subclasses` `:9805`
→ `remove_subclass` `:9791`, and when the dict empties, `clear_tp_subclasses` →
`Py_CLEAR(self->tp_subclasses)` — **the dict the outer loop is iterating is deallocated.**

**Reproduced — `debug-gil-nojit-asan`, 3/3 runs, deterministic:**

```
==1336997==ERROR: AddressSanitizer: heap-use-after-free ... READ of size 8
    #0 _PyDict_Next                Objects/dictobject.c:3212
    #1 _PyType_Modified_Unlocked   Objects/typeobject.c:1201
    #2 type_update_dict            Objects/typeobject.c:6712
    #3 type_setattro               Objects/typeobject.c:6830
freed by thread T0 here:
    #1 dict_dealloc                Objects/dictobject.c
    #3 remove_subclass             Objects/typeobject.c:9791
    #4 remove_all_subclasses       Objects/typeobject.c:9805
    #5 type_set_bases_unlocked     Objects/typeobject.c:1965
    #6 type_set_bases              Objects/typeobject.c:2032
    #7 type_setattro               Objects/typeobject.c:6807
```

Reproducers: `scratchpad/watcher_uaf.py` (via `sys.unraisablehook` — the ASan hit above),
`scratchpad/watcher_uaf_repr.py` (via a metaclass `__repr__`, **no hook needed** — proves `%R`
alone reaches user code; reparents on 3/3 runs, ASan does not always observe the read because the
default unraisable-hook traceback machinery churns the allocator in between).

| build | result |
|---|---|
| `debug-gil-nojit-asan` | **heap-use-after-free**, 3/3 |
| `debug-gil-nojit`, `release-gil-nojit` | reparents, no visible fault (freed memory still readable) |
| `debug-ft-nojit-asan` (`PYTHON_GIL=0`) | reparents; no ASan report — the free-threaded build reclaims the dict through the deferred/freelist path, so ASan never sees the reuse. **The read is still of a dead object; FT masks it, it does not fix it.** |

**Reachability.** `PyType_AddWatcher` / `PyType_Watch` are public C API since 3.12
(`Include/cpython/object.h:457-460`), and returning `-1` from the callback is the documented way to
signal failure — `Modules/_testcapi/watchers.c:213` ships exactly such a callback. Any extension
that watches types and can fail (a profiler, a JIT, an ORM invalidation hook) plus either a
`sys.unraisablehook` or a Python-level metaclass `__repr__` reaches this. It is not FT-specific and
not debug-specific.

**Guarded twins — three, all in this file:**

1. `_PyType_GetSubclasses` `:776-807`. Same `PyDict_Next(subclasses, …)` walk, and it carries the
   explicit justification the buggy loop lacks: *"The loop cannot modify tp_subclasses, there is
   no need to hold a strong reference (use a borrowed reference)"* (`:788-789`).
2. `recurse_down_subclasses` `:12351-12353`. Same walk, same explicit justification: *"It is safe
   to use a borrowed reference because update_subclasses() is only used with
   update_slots_callback() which doesn't modify tp_subclasses."*
3. **`type_update_dict` `:6696-6702`** — the direct caller, stating the exact invariant that is
   violated: *"We don't want any re-entrancy between when we update the dict and call
   `_PyType_Modified_Unlocked`, including running the destructor of the current value as it can
   observe the cache in an inconsistent state. Because we have an exact unicode and our dict has
   exact unicodes we know that this will all complete without releasing the locks."*

Both walkers 1 and 2 state *why* borrowing is safe for them. The loop at `:1201` states nothing —
and it is the only one of the three that provably **does** run user code, because it is the only
one that fires watcher notifications. `type_update_dict` proves the author's model is
"nothing re-entrant runs in here"; `:1222-1225` breaks that model from two frames down.

**The acknowledgement is at the callee, the defence is missing at the caller.** The comment at
`:1219-1220` — *"Note that PyErr_FormatUnraisable is potentially re-entrant and the watcher
callback might be too"* — was added by `fbbbc10055e` (gh-127266, *"avoid data races when updating
type slots"*, 2025-05-27), i.e. the re-entrancy was noticed during free-threading hardening. The
`PyDict_Next` loop directly above it (`3a4c15bb981`, bpo-46417, 2022) was never revisited.

**Fix.** Snapshot the subclass set before the loop rather than iterating the live dict — exactly
what `_PyType_GetSubclasses` already does. Note the tension with **pass 1's FIX**
(`set_flags_recursive`, `:6494`), which is *"do not allocate inside the STW region"*: the
resolution that satisfies both is to materialise the snapshot **before** entering the exclusive
region and pass it down, which is the `queue_slot_update` / `apply_slot_updates` (`:3859`/`:3883`)
pattern this file already uses for slot writes. A minimum viable fix is
`Py_INCREF(subclasses)` around the loop plus `Py_DECREF` after — that converts the
use-after-free into a merely stale iteration (`PyDict_DelItem` leaves a dummy and does not
resize), which is a strictly smaller bug but still not correct if the re-entrant code *inserts*.

**Prior art.** `gh api -X GET search/issues` finds **gh-151377** (open, 2026-06-11), *"Data races on
type slots and `tp_subclasses` between `type_setattro` and `type_new` / `type_dealloc`"* — the
**cross-thread** race on the *other* walker (`recurse_down_subclasses`) under free-threading. This
finding is the **single-threaded re-entrancy** sibling on the third walker, observable on the
default GIL build. Adjacent, not the same; the existing issue's reproducer does not reach `:1201`
and its proposed locking would not close this path. No issue found for the watcher/re-entrancy
angle (`gh api` for `_PyType_Modified_Unlocked watcher`, `type watcher reentrant` → 0 relevant).

**Sibling to hunt elsewhere:** the third instance of the shape is pass 2's NET-NEW candidate
**`_PyType_GetSubclasses` :793** (`scan_refcounts`, `borrowed_field_deref_across_call`, medium).
Its `PyList_Append` at `:799` can run a GC pass and therefore a `__del__` → `type_dealloc` →
`remove_subclass` → the same `Py_CLEAR`. Its `:788-789` comment asserts this cannot happen. That
belongs to the refcount agent; I flag it as confirmed-family, not confirmed-bug.

---

### 2.2 [CONSIDER] `type_set_abstractmethods` waits for stop-the-world without `type_lock_prevent_release()`, so `TYPE_LOCK` can be dropped between the invalidation and the flag write

**Site:** `Objects/typeobject.c:1743-1752`.

```c
    BEGIN_TYPE_LOCK();
    _PyType_Modified_Unlocked(type);   // :1744  zeroes tp_version_tag
    types_stop_world();                // :1745  NO type_lock_prevent_release()
    if (abstract) type_add_flags(type, Py_TPFLAGS_IS_ABSTRACT);
    else          type_clear_flags(type, Py_TPFLAGS_IS_ABSTRACT);
    types_start_world();               // :1750
    ASSERT_TYPE_LOCK_HELD();           // :1751
    END_TYPE_LOCK();
```

**Guarded twin, 4,760 lines later, spelling out why the missing call is needed:**

```c
    /* Keep TYPE_LOCK held while waiting for stop-the-world so no thread
       can reassign a version tag before the flag update. */
    type_lock_prevent_release();       // :6521   _PyType_SetFlagsRecursive
    types_stop_world();
    set_flags_recursive(self, mask, flags);
    types_start_world();
    type_lock_allow_release();         // :6525
```

The mechanism is real and I traced it end-to-end. `types_stop_world()` →
`_PyEval_StopTheWorld` → `stop_the_world()` (`Python/pystate.c:2441`) begins with
`_PyRWMutex_Lock/RLock(&runtime->stoptheworld_mutex)` and `PyMutex_Lock(&stw->mutex)`. Both park
with **detach enabled** — `rwmutex_set_parked_and_wait` calls
`_PyParkingLot_Park(..., /*detach=*/1)` (`Python/lock.c`), and `PyMutex_Lock` is
`_PyMutex_LockTimed(m, -1, _PY_LOCK_DETACH)`. Detaching runs
`_PyCriticalSection_SuspendAll` (`Python/critical_section.c:106`), which unlocks every mutex held
via a critical section — including `TYPE_LOCK`. `type_lock_prevent_release()` (`:144`) exists
solely to splice `TYPE_LOCK` out of that record so it survives the wait. (The wait on the stop
*event* itself passes `detach = 0`, so the window is the two mutex acquisitions, i.e. whenever
another STW is already in flight — a concurrent GC is the common case.)

**Consequence.** With `TYPE_LOCK` dropped, another thread can take it and run
`assign_version_tag(type)` (`:1407`, reachable from any attribute lookup via
`_PyType_LookupStackRefAndVersion` `:6358`). The type then holds a *fresh* version tag that was
assigned **before** `Py_TPFLAGS_IS_ABSTRACT` changed, and nothing invalidates it afterwards —
`_PyType_Modified_Unlocked` already ran, at `:1744`. Anything keyed on the version tag
(the method cache, `_spec_cache`, a JIT executor guarded on `tp_version_tag`) is then valid for a
flag state that no longer holds.

**Why `PyType_Freeze` (`:12522-12528`) is *not* the same bug** even though it also omits the call:
it does the flag write first and `_PyType_Modified_Unlocked(type)` **after** (`:12527`), so a
version tag assigned during the lock-drop window is invalidated by the subsequent call. The
ordering saves it. `type_set_abstractmethods` invalidates first and therefore has no such
backstop — it is the only one of the three that both omits `type_lock_prevent_release()` and
invalidates before the STW.

**Why CONSIDER, not FIX.** Free-threaded-build only (the macros are no-ops otherwise, `:184-194`),
requires a concurrent stop-the-world to open the window, and the observable is a stale
specialization rather than a crash. I did not reproduce it. **The fix is two lines** and is
already written 4,760 lines away: bracket `types_stop_world()`/`types_start_world()` at
`:1745`/`:1750` with `type_lock_prevent_release()` / `type_lock_allow_release()`. The
`ASSERT_TYPE_LOCK_HELD()` the author put at `:1751` — immediately after `types_start_world()`,
where it can only be checking that the lock survived — suggests the concern was already felt here.

---

### 2.3 [POLICY] The watcher notification observes a type in a state no watcher contract describes

Independent of the use-after-free, `:1222` fires the callback at a point where
`_PyType_Modified_Unlocked` has invalidated **every subclass** (`:1201-1208`) but has not yet
invalidated **itself** (`set_version_unlocked(type, 0)` is at `:1233`, `_spec_cache.getitem = NULL`
at `:1237`). So at callback time:

- `type->tp_version_tag` is still the **pre-modification** value, and the method cache entries
  keyed on it are still considered valid by `_PyType_LookupStackRefAndVersion` (`:6313-6345`);
- `type`'s `_spec_cache` still points at the stale specialization;
- on the `type_setattro` path the dict has **not yet been written** — `type_update_dict` runs
  `_PyType_Modified_Unlocked` at `:6712` *before* `_PyDict_SetItem_LockHeld` at `:6715`.

I measured the third one directly: with a watcher on `C` and a hook that reads the type,
`C.x = 2` produces `C.__dict__['x'] == 1` **inside the callback**
(`scratchpad/watcher_stale2.py`, identical on `debug-gil-nojit` and `release-ft-nojit`
`PYTHON_GIL=0`). The callback is told "this type was modified" and, if it looks, sees the type
before the modification.

I tried to turn this into a persistent stale-cache result — hook reads `D.x` on a subclass in that
window, hoping the fresh version tag assigned to `D` would pin the pre-update value permanently —
and **could not**: `D.x` read 2 immediately after in every build. Reporting the negative honestly:
the window is real and demonstrated, the durable-corruption consequence is not.

Classified POLICY because the watcher API has no documented statement of what state the callback
may observe, and "you are notified before the modification is visible, with a still-valid version
tag" is a contract decision for maintainers rather than a defect I can name a correct behaviour
for. Note the in-tree consumer is safe by construction: the JIT's `type_watcher_callback`
(`Python/optimizer_analysis.c:152-158`) only invalidates executors and unwatches — it never reads
the type.

---

## 3. Free-threading readiness of the pass-2 regions

| region | FT arm | GIL arm | divergence shape |
|---|---|---|---|
| watchers / versions `971-1481` | `TYPE_LOCK` + atomics on `tp_version_tag`/`tp_versions_used` | all lock macros are no-ops (`:184-194`) | **the bug is in the shared arm.** §2.1 is a re-entrancy UAF, not a race; `TYPE_LOCK` neither causes nor prevents it, and the GIL build is where it is *observable* because FT's deferred reclamation hides the free from ASan. This is the **inverse** of CPY-0068 (FT arm ships the fix, GIL path broken): here the FT arm ships more machinery that is simply orthogonal. |
| MRO C3 `3217-3702` | `TYPE_LOCK` around `mro_implementation`/`mro_internal`; `ASSERT_TYPE_LOCK_HELD` ×4 | no-op | **no divergence.** The only defence against a re-entrant custom `mro()` is the `reent` check in `mro_internal` (`:3667-3676`), which is build-independent. Correct on both. |
| lookup cache `6140-6452` | seqlock + acquire/release, `update_cache_gil_disabled` `:6237` | plain compare, `update_cache` `:6216` | **deliberate, self-consistent divergence.** Additionally `_PyType_LookupByVersion` returns `NULL` unconditionally under `Py_GIL_DISABLED` (`:1346-1347`) and `set_version_unlocked`'s `type_version_cache` maintenance is `#ifndef Py_GIL_DISABLED` (`:1137-1162`) — an entire cache that exists only on the GIL build. Anything reasoning about it is GIL-only. |
| getattro / setattro `6529-6848` | `BEGIN_TYPE_DICT_LOCK` (two-mutex CS) | no-op | **shared-arm defect again.** The invariant `type_update_dict` `:6696` relies on ("this will all complete without releasing the locks") is broken on both builds by `:1222-1225`, which is inside the region on both. |
| `__class__` assignment `7482-7846` | `_PyObject_IsUniquelyReferenced` fast path + STW (`:7821-7829`) | STW macros are no-ops | **FT arm strictly stronger**; nothing to flag. `Py_BEGIN_CRITICAL_SECTION(dict)` inside the stopped world (`:7778`) is safe — pass 1 §2.4 established that a stopped thread runs `_PyCriticalSection_SuspendAll` before parking. |
| `type_set_abstractmethods` `1716-1755` | `TYPE_LOCK` + STW, **missing** `type_lock_prevent_release` | no-op | **FT-only defect** (§2.2). The GIL build is unaffected. |
| pickle / `__reduce__` `7848-8406` | none | none | **no exclusivity at all**, and it is the region where the most user Python runs per call. Nothing for this agent; it is pure refcount/error-path territory. |

**Cross-cutting FT observation.** Three of the four debug assertions that encode this file's
concurrency invariants (`ASSERT_TYPE_LOCK_HELD`, `ASSERT_WORLD_STOPPED_OR_NEW_TYPE`,
`ASSERT_NEW_TYPE_OR_LOCKED`, `:108-121`) are simultaneously `Py_DEBUG`-only **and** FT-only, and
`TYPE_IS_REVEALED` is hard-`0` on 32-bit (`:104`). An invariant enforced only by these is checked
on exactly one of four build configurations. That is a POLICY-grade observation about how much of
this file's correctness is machine-checked, and it is why §2.1 — which no assertion covers on any
build — went unnoticed for four years.

---

## 4. Dispositions (checked, not re-litigated)

### 4.1 `mro_invoke :3603` — custom `mro()` under `TYPE_LOCK` — ACCEPTABLE
`call_method_noarg((PyObject *)type, &_Py_ID(mro))` is arbitrary Python with `TYPE_LOCK` held, and
the comment at `:3599-3601` says so (*"This is potentially re-entrant"*). The caller
`mro_internal` `:3663-3676` snapshots `old_mro = Py_XNewRef(lookup_tp_mro(type))` **before** the
call and tests `reent = (lookup_tp_mro(type) != old_mro)` after, bailing with return code 0 —
with a 20-line comment documenting the three-way return contract (`:3625-3655`). This is the
correct handling of the shape, and it is what §2.1's loop should have.

### 4.2 `class_name :3263-3264` under `TYPE_LOCK` — ACCEPTABLE
Both calls are user Python for a metaclass with `__getattr__`, a `__name__` property, or
`__repr__`. Two call sites, both checked:
- `set_mro_error :3334` calls it inside a live `PyDict_Next(set, &i, &k, &v)` loop — but `set` is a
  function-local dict (`:3315`) that user code cannot reach, so the cursor is safe; and all
  `to_merge[]` borrowed derefs happen in the loop at `:3318-3327`, before any user code runs.
- `check_duplicates :3281` receives `o` borrowed from `tp_bases`, and returns `-1` on the very
  next statement without touching the tuple again.
No state survives the call in either. Recording it so nobody re-flags it.

### 4.3 `same_slots_added :7609` `PyObject_RichCompareBool` while the world is stopped — ACCEPTABLE, **confirmed**
Pass 1 disposed of this (report §2.4 item 2) on the premise that `ht_slots` is always a tuple of
strings. I re-checked the one thing that premise rests on: `ht_slots` has exactly **two**
assignments in the file, `:4923` (`et->ht_slots = ctx->slots`, every element already through
`valid_identifier` → `PyUnicode_Check` + `PyUnicode_IsIdentifier`) and `:4927` (`NULL`). There is
no `PyType_FromSpec`/`PyType_FromMetaclass` path that sets it. So the compare resolves to
`tuple_richcompare` → `unicode_richcompare`, all C, and cannot re-enter Python. Premise holds;
one line, moving on.

---

## 5. Summary

| # | class | site | status |
|---|---|---|---|
| 0 | **N/A** | `scan_gil_usage` on this file | vocabulary structurally absent (`grep -c` = 0); not clean, not a blind spot |
| 1 | **FIX** | `Objects/typeobject.c:1195-1208` / `:1222-1225` | **reproduced heap-use-after-free**, 3/3 on `debug-gil-nojit-asan`; 3 guarded twins in-file |
| 2 | **CONSIDER** | `Objects/typeobject.c:1745` (`type_set_abstractmethods`) | mechanism traced through `pystate.c:2441` + `lock.c` + `critical_section.c:106`; twin at `:6521`; not reproduced |
| 3 | **POLICY** | `Objects/typeobject.c:1222` (watcher contract) | pre-modification visibility **measured**; durable-corruption consequence **not** demonstrated |
| 4 | **ACCEPTABLE** | `:3603`, `:3613` (`mro_invoke`) | guarded by the `reent` check at `:3667` |
| 5 | **ACCEPTABLE** | `:3263`, `:3264` (`class_name`) | no state survives the call at either call site |
| 6 | **ACCEPTABLE** | `:7609` (`same_slots_added`) | pass-1 disposition confirmed against the `ht_slots` writer census |

Lanes I deliberately did **not** enter: `borrowed_field_deref_across_call` triage (18 candidates,
refcount agent), the `_PyType_GetSubclasses :793` candidate (same family as §2.1 but its
discriminator is GC-reentrancy, not exclusivity), and pass 1's `set_flags_recursive` FIX (confirmed
still present, cross-linked in §2.1's fix discussion).

---

## 6. TOOLKIT ASSESSMENT

### 6.1 Precision / recall of `scan_gil_usage` on this file

| metric | value |
|---|---|
| findings | 0 |
| false positives | 0 (nothing emitted) |
| **true recall** | **undefined** — 0 of 0 constructs resolved |
| findings this agent produced *outside* the scanner | 1 FIX (reproduced UAF), 1 CONSIDER, 1 POLICY |

The `rule_not_applicable` flag from issue #28 / PR #29 **worked exactly as designed**: it is what
stopped this from being logged as a clean pass, and it is what let me discharge the zero in one
grep instead of a session. Keep it. The remaining defect is that the flag is per-*scanner*, and
this scanner is about to need two vocabularies.

### 6.2 The vocabulary extension — concrete proposal

`scan_gil_usage`'s premise is "the GIL is the exclusivity primitive, and the interesting event is
releasing it". In post-PEP-703 CPython there are **three** exclusivity primitives, and in
`Objects/` and `Python/` the GIL one is the rare one. The rule should be reframed as
**exclusivity discipline**, with two vocabulary families reported separately.

**(a) Two vocabulary families, separately counted.**

```
family "gil_release"   : Py_BEGIN_ALLOW_THREADS / Py_END_ALLOW_THREADS /
                         PyGILState_Ensure / PyGILState_Release /
                         Py_BLOCK_THREADS / Py_UNBLOCK_THREADS
family "exclusivity"   : _PyEval_StopTheWorld / _PyEval_StartTheWorld
                         Py_BEGIN_CRITICAL_SECTION{,2}{,_MUTEX} / Py_END_CRITICAL_SECTION{,2}
                         PyMutex_Lock / PyMutex_Unlock
                         + file-local #define aliases and one-line static wrappers
```

`rule_not_applicable` becomes **per family**. On this file the honest envelope is
`gil_release: 0 resolved → N/A` and `exclusivity: 34 regions resolved → analysed`. Today's single
boolean cannot express that, and "0 and 0" reads identically to "0 and 34" in any downstream
synthesis.

**Do not re-implement the resolution.** `scan_lock_discipline` already resolves this file's
`#define` aliases correctly (`vocabulary_resolved=66`, `critical_section_functions=23`, per the
include-map's measured table) — the `BEGIN_TYPE_LOCK`/`END_TYPE_LOCK` case is its own motivating
example. Lift that resolver into the shared chassis and have both scanners consume it. Add pass 1's
one-line-static-wrapper rule (`types_stop_world` `:124`) there too, so `scan_stw_safety`,
`scan_lock_discipline` and this rule all stop missing the same nine regions.

**(b) New finding type: `user_code_under_exclusivity`.**

Trigger: a user-code-capable call reachable — **transitively, intra-file** — from an open
exclusivity region. Transitivity is not optional: in every one of my findings the region opener and
the risky call are in different functions, 1–3 hops apart
(`type_setattro:6830` → `type_update_dict:6712` → `_PyType_Modified_Unlocked:1223`). A textual
scan of region bodies finds **none** of them; I verified that by running one first
(`scratchpad/exclusivity.py`, 5 hits, all benign) before writing the call-graph version
(`scratchpad/exclusivity2.py`, which found all six real ones).

Seed the region set from **three** sources, not one: the textual `BEGIN…/END…` span, the callees
named inside it, and every function containing an `ASSERT_*_LOCK_HELD()`-family assertion. That
third source is free precision — the file is telling you its own contract — and it is what pulled
`mro_invoke`, `mro_implementation_unlocked` and `class_name` into scope, none of which appear
inside any textual region.

**(c) The user-code-capable list must be led by the *indirect* entries.** The direct ones
(`PyObject_Call*`, `_PyObject_LookupSpecial`, `call_method*`) are the easy half. Every finding
above came from the indirect half:

| entry | why | evidence |
|---|---|---|
| `PyErr_FormatUnraisable` / `PyErr_WriteUnraisable` | runs `sys.unraisablehook` **and** the `%R` repr | §2.1, both reproduced |
| `PyErr_Format` / `PyUnicode_FromFormat*` **with a `%R`/`%S`/`%N`/`%T` conversion** | those conversions call `PyObject_Repr`/`Str`; a `%s`-only format does not | pass 1's ACCEPTABLE #1 turns on exactly this distinction — **the discriminator is already in the catalog, it just is not in a scanner** |
| a C callback read from interpreter state (`interp->type_watchers[i]`, `interp->dict_state.watchers[…]`) | third-party code by definition | §2.1 |
| `Py_DECREF`/`Py_CLEAR`/`Py_SETREF` of a value that can hold a finalizer | `__del__` | `:1207`, `:1965` |

The `%R`-vs-`%s` rule is the single highest-value addition: it is a pure-syntax check on the format
string, it has near-zero false-positive cost, and it separates pass 1's ACCEPTABLE from pass 2's
FIX at the same class of call site.

**(d) Cross-rule join: cursor invalidation.** The finding shape in §2.1 is
*"a `PyDict_Next` / index loop over a container obtained through a **borrowed** accessor, with a
user-code-capable call in the loop body"*. Neither existing rule sees it: `scan_refcounts` finds
the borrowed container but has no notion of "user code can run here", and an exclusivity rule finds
the user code but has no notion of "a cursor is live". Emit the join. There are **three** instances
of this exact shape in this one file (`:1201` = confirmed UAF, `:793` = pass-2 NET-NEW candidate,
`:12360` = the subject of open CPython issue gh-151377), which is the strongest evidence in this
slice that the shape deserves its own rule.

**(e) Do not report `functions_analyzed` as this rule's denominator.** `459` next to
`vocabulary_resolved=0` is the exact shape the campaign has been burned by. The `denominators`
block from PR #29 already carries `vocabulary_resolved`; the remaining fix is to make the *summary
line* quote the vocabulary count, never the parse count, whenever the two disagree.
