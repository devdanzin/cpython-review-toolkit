# stw-safety-checker — obj-mappings slice

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** `Objects/dictobject.c` (8,597 lines), `Objects/setobject.c` (3,228 lines)
**Build used:** `~/projects/python_build_matrix/builds/debug-ft-nojit` @ `a1d580430c8`,
plus `debug-ft-nojit-tsan`.
**Build-vs-target diff:** every file cited below — `Objects/dictobject.c`,
`Objects/setobject.c`, `Python/critical_section.c`, `Python/pystate.c`,
`Python/gc_free_threading.c`, `Objects/typeobject.c`, `Objects/weakrefobject.c` —
is **byte-identical** between `a1d580430c8` and `4f3be1b5777`
(`git diff --stat a1d580430c8 4f3be1b5777 -- <file>` empty for all seven).
Dynamic results below are therefore valid evidence for the target ref.

---

## 1. Denominator

### Scanner population: **0 — structural, not evidential**

`scanners/scan_stw_safety.sample.json`:

| field | value |
|---|---|
| `files_analyzed` | 2 |
| `functions_analyzed` | 404 |
| `findings` | 0 |
| `summary.stw_function_count` | 0 |
| `summary.stw_wrapper_count` | **0** |
| `stw_functions` | `[]` |
| `stw_wrappers` | **`[]`** |

The zero is **structural**. The rule cannot fire on this code:

```
$ grep -c '_PyEval_StopTheWorld\|_PyEval_StartTheWorld\|_PyEval_StopTheWorldAll' \
      Objects/dictobject.c Objects/setobject.c
0   (both files)
```

Neither file contains the token. `scan_stw_safety.py:535`
(`if "_PyEval_StopTheWorld" not in source_text: continue`) skips both files at
the file gate before any function is classified. The `functions_analyzed: 404`
denominator is real but measures a different thing — functions parsed, not
candidates evaluated. **Candidates evaluated by the rule: 0 of 0 possible.**

I re-ran the scanner per-file to confirm the sample was not a stale artifact:

```
dict  files 1 funcs 292 findings 0 stw_functions [] stw_wrappers [] wrapper_count 0
set   files 1 funcs 112 findings 0 stw_functions [] stw_wrappers [] wrapper_count 0
```

### Wrapper-resolution fix (`stw_wrapper_kind` / `discover_stw_wrappers`)

Asked for explicitly. The fix is **present** in the scanner
(`scan_stw_safety.py:259`, `:276`, `:541`, `:602`, `:605`) and reports
`stw_wrapper_count: 0` / `stw_wrappers: []` on both slice files.

**This slice does not test the fix.** The typeobject.c defect it repaired was
`stw_functions` counting a trivial static wrapper's own definition as a region.
A wrapper can only be discovered in a file that contains a StopTheWorld call;
both slice files are rejected at the `:535` file gate, so `discover_stw_wrappers`
is never reached. The result here is a **no-regression** signal (the fix does not
manufacture phantom wrappers on STW-free code), not a positive test. The
positive test still wants a file that *does* open a region and *does* use a
static wrapper — `Objects/typeobject.c` remains the only known one
(`types_stop_world`, `typeobject.c:128`, 10 call sites).

### The denominator that actually matters here

dict and set open zero regions, so the outbound question is vacuous. The real
surface is **inbound**: functions in these two files that execute *while some
other file has stopped the world*. That population I built by hand:

| measure | count |
|---|---|
| STW regions tree-wide (`_PyEval_StopTheWorld(` call sites, excl. definition) | 46 |
| `_PyEval_StopTheWorldAll(` call sites (excl. definition) | 8 |
| regions audited for reachability into dict/set | 54 |
| regions that reach `dictobject.c` / `setobject.c` | **5** |
| slice functions proven to execute under STW | **7** |
| STW-contract violations found among those 7 | 0 live, 3 CONSIDER |

---

## 2. The inbound call graph (built by reading, not by the scanner)

`Python/gc_free_threading.c` contains **zero** `PyDict_*` / `PySet_*` /
`PyDictObject` / `PySetObject` tokens. The GC's only route into this slice is
indirect `tp_traverse` dispatch. Every `tp_traverse` call in the free-threaded
GC is inside an STW region:

| dispatch site | enclosing fn | region |
|---|---|---|
| `gc_free_threading.c:1021` | `update_refs` | 2067–2141 |
| `:1043` | `mark_reachable` | 2067–2141 |
| `:1331` | `gc_propagate_alive_prefetch` | 2067–2141 |
| `:1352` | `gc_propagate_alive` | 2067–2141 |
| `:1877` | `handle_resurrected_objects` | 2151–2161 |
| `:2382` | `visit_get_referrers` (`gc.get_referrers()`) | 2398–2400 |
| `Modules/gcmodule.c:283` | `append_referrents` (`gc.get_referents()`) | `gcmodule.c:313–315` |

So the seven slice functions executing under STW are:

`dict_traverse` (dictobject.c:5089) · `dictiter_traverse` (:5669) ·
`dictview_traverse` (:6443) · `PyObject_VisitManagedDict` (:7748) ·
`set_traverse` (setobject.c:937) · `set_next` (:710) ·
`setiter_traverse` (:1050)

Plus four direct entry points from non-GC regions (subagent sweep, each
verified by me in source):

| region | slice entry point |
|---|---|
| `Python/pystate.c:2743` `_PyThread_CurrentFrames` (`StopTheWorldAll`) | `PyDict_SetItem` @ `pystate.c:2762` |
| `Python/pystate.c:2808` `_PyThread_CurrentExceptions` (`StopTheWorldAll`) | `PyDict_SetItem` @ `pystate.c:2826` |
| `Objects/typeobject.c:7823` `object_set_class` (`types_stop_world`) | `_PyObject_MaterializeManagedDict_LockHeld` (dictobject.c:7358), `_PyDict_DetachFromObject` (:8031) |
| `Objects/typeobject.c:6522` `_PyType_SetFlagsRecursive` | `PyDict_Next` (dictobject.c:3269) |

---

## 3. Findings

### [CONSIDER] STW-0001 — the critical-section STW bypass is per-interpreter, but `StopTheWorldAll` is runtime-wide

**Site (slice anchor):** `Objects/dictobject.c` `PyDict_SetItem`
(→ `_PyDict_SetItem_Take2` → `Py_BEGIN_CRITICAL_SECTION(mp)`), executed with the
world stopped from `Python/pystate.c:2762`.

**Mechanism.** CPython discharges the "never take a lock a stopped thread holds"
rule structurally, not by review. `Python/critical_section.c:47-52`:

```c
    // If the world is stopped, we don't need to acquire the lock because
    // there are no other threads that could be accessing the object.
    // Without this check, acquiring a critical section while the world is
    // stopped could lead to a deadlock.
    if (tstate->interp->stoptheworld.world_stopped) {
```

That test reads the **per-interpreter** flag (also at `:69` for
`_PyCriticalSection2_BeginSlow`). But `stop_the_world()` sets
`stw->world_stopped = 1` on whichever struct it was handed
(`Python/pystate.c:2473`, `:2495`), and:

- `_PyEval_StopTheWorld(interp)` → `stop_the_world(&interp->stoptheworld)` ✅ bypass fires
- `_PyEval_StopTheWorldAll(runtime)` → `stop_the_world(&runtime->stoptheworld)` ❌ **bypass does not fire**

So inside any of the 8 runtime-wide STW regions, `Py_BEGIN_CRITICAL_SECTION`
falls through to a real `PyMutex_Lock`. If the target mutex were held by a
thread parked at a safe point, the stopping thread blocks forever and can never
reach `_PyEval_StartTheWorldAll` — a permanent, unrecoverable runtime deadlock,
exactly the failure the comment says the check exists to prevent.

**Guarded twin — and what its guard actually addresses (lesson 3).** The twin is
the *same code path* under the per-interpreter stop: `object_set_class`
(`typeobject.c:7823`) takes `Py_BEGIN_CRITICAL_SECTION(dict)` at `:7778` inside
`types_stop_world()`, which is `_PyEval_StopTheWorld(interp)`
(`typeobject.c:128`) — there the bypass fires and the lock is skipped. The guard
at `critical_section.c:50` genuinely addresses *the stopping thread re-entering a
per-object lock*. It does **not** address *which* stoptheworld state machine was
used to stop the world; the flag it reads and the flag `StopTheWorldAll` writes
are different fields of different structs. Citing the bypass as blanket proof
that "critical sections are STW-safe in CPython" would be exactly the lesson-3
error.

**Why this is CONSIDER and not FIX.** At the one slice-anchored site the mutex is
provably uncontested: `result` is created by `PyDict_New()` at `pystate.c:2731`,
before the stop, and is never published — no parked thread can hold its lock.
The mechanism is proven; the *live* deadlock is not. I did not find a
runtime-wide region that locks a genuinely shared object.

**Reproduction:** `repro/stw_all_dict_critical_section.py` —
8 dict-hammering threads + 4 deep-frame threads, 4,000 `sys._current_frames()` /
`sys._current_exceptions()` calls per run.
**5/5 runs completed with no deadlock** on `debug-ft-nojit` (~0.15 s each).
Negative confirmed, as predicted. The finding is latent, not live.

**Owner note:** the per-interpreter/runtime flag mismatch is a *lock-discipline*
defect, not a dict defect. It is reported here because dict is the only CPython
object whose per-object lock is demonstrably reachable from a runtime-wide STW
region. Recommend handing the mechanism to the lock-discipline checker.

---

### [CONSIDER] STW-0002 — `set_clear_internal` is registered raw as `tp_clear`; its dict twin is not

**Site:** `Objects/setobject.c:646` `set_clear_internal`, wired at
`setobject.c:2916` (`PySet_Type.tp_clear`) and `setobject.c:3008`
(`PyFrozenSet_Type.tp_clear`).

**Mechanism.** `set_clear_internal` takes no critical section and does
`Py_DECREF(entry->key)` in a loop (`setobject.c:686-691`), which can run a user
`__del__`. Every *other* route into it holds the per-object lock — I checked all
of them:

| caller | lock |
|---|---|
| `set.clear()` → clinic wrapper `set_clear` | `Py_BEGIN_CRITICAL_SECTION(so)` — `Objects/clinic/setobject.c.h:125` (`@critical_section` at `setobject.c:1632`) |
| `set_symmetric_difference_update_impl` self-case `:2309` | calls the clinic wrapper |
| `PySet_Clear` `:3065` | calls the clinic wrapper |
| `set_difference_update_internal` `:1987` | `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(so)` at `:1983` |
| `set_init` `:2782` | `Py_BEGIN_CRITICAL_SECTION(self)` at `:2780` |
| **`PySet_Type.tp_clear` `:2916`** | **none** |
| **`PyFrozenSet_Type.tp_clear` `:3008`** | **none** |
| `_PySet_ClearInternal` `:3072` | none (internal contract) |

The file uses `@critical_section` 13 times. The `tp_clear` registration is the
outlier.

**Guarded twin — and what its guard actually addresses (lesson 3).** The twin is
the same slot on the sibling type: `dict_tp_clear` (`dictobject.c:5121`) does
**not** call `clear_lock_held` directly; it calls `PyDict_Clear`, which is
exactly `Py_BEGIN_CRITICAL_SECTION(op); clear_lock_held(op); Py_END_CRITICAL_SECTION();`.
Same GC caller, same slot, opposite discipline. That guard addresses *a second
thread touching the same dict* — the live threat here.

The trap: `set_clear_internal` **does** carry a prominent safety comment
(`setobject.c:659-664`) — *"This is delicate. During the process of clearing the
set, decrefs can cause the set to mutate… we have to make the set empty before
clearing the slots"* — and it implements that correctly (empty-to-minsize, then
clear a private copy of the table). But that guard's threat model is
**single-threaded re-entrancy from a `__del__` on the same thread**. It provides
**zero** protection against a second thread inside `set_add_entry` holding the
critical section this path never takes. Reading the comment as evidence of
thread-safety is the precise error lesson 3 describes.

**Why this is in the STW class.** The only remaining safety argument for omitting
the lock is "the GC runs with the world stopped." **That premise is false**, and
CPython writes it down explicitly at `Objects/weakrefobject.c:163`:

> `// The world is stopped during GC in free-threaded builds. It's safe to`
> `// call this without holding the lock.`

— in `gc_clear`, i.e. a `tp_clear`. But `tp_clear` is dispatched by
`delete_garbage()` at `Python/gc_free_threading.c:1761` (`(void) clear(op);`),
and `delete_garbage` is called at **`:2176`** — *fifteen lines after*
`_PyEval_StartTheWorld(interp)` at **`:2161`**. `finalize_garbage` (`:2149`) and
`call_weakref_callbacks` (`:2148`) are likewise outside. The world is running
during every `tp_clear`. The premise holds only for `tp_traverse`.

**Why CONSIDER, not FIX.** Reaching it needs a second thread holding a set that
is in the GC's unreachable worklist. `handle_resurrected_objects`
(`gc_free_threading.c:1841`, inside region B) removes objects republished
*before* `delete_garbage`; a republication from inside `delete_garbage`'s own
loop is not covered, which is the window I targeted. I could not make it fire.

**Reproduction:** `repro/stw_set_tp_clear_unlocked.py` — a `__del__` that
republishes a still-unreachable set to 6 hammering threads, 300 collect rounds ×
40 cycles per run.
- **8/8 runs clean, exit 0**, `debug-ft-nojit`.
- **0 ThreadSanitizer warnings** in 1 run on `debug-ft-nojit-tsan`
  (`TSAN_OPTIONS=halt_on_error=0`), which is the correct detector for this shape.

Static-confirmed; **not reproduced**. Recorded honestly as such.

---

### [ACCEPTABLE] STW-0003 — `detach_dict_from_object` raises `MemoryError` with the world stopped

**Site:** `Objects/dictobject.c:7972-7975`, reached under `types_stop_world()`
via `object_set_class` (`typeobject.c:7823`) → `object_set_class_world_stopped`
(`:7779`) → `_PyDict_DetachFromObject` (`dictobject.c:8031`).

```c
    PyDictValues *values = copy_values(mp->ma_values);
    if (values == NULL) {
        PyErr_NoMemory();
        return -1;
    }
```

CPython's ground-truth pattern inverts this: `gc_free_threading.c:2111-2113` and
`:2121-2123` both do `_PyEval_StartTheWorld(interp); PyErr_NoMemory();`.

**Verdict ACCEPTABLE**, per the 2026-04-04 data-file revision: `PyErr_NoMemory`
is conditionally safe, and both preconditions hold here. The type is built-in
and the `MemoryError` instance is preallocated, so no allocation occurs while
stopped; and there is no pending exception — `object_set_class` is an attribute
setter entered from Python with a clean error state. The error is propagated out
by return value and observed after `types_start_world()`. I am recording it
rather than staying silent because it is the one place in the slice where the
literal ground-truth ordering is inverted, and because a future edit that
replaces `PyErr_NoMemory()` with anything format-bearing would silently become a
FIX.

**Near miss worth naming.** The sibling failure handler for the *same* callee —
`PyObject_ClearManagedDict` at `dictobject.c:8011-8012` — reacts to the identical
`detach_dict_from_object` failure with `PyErr_FormatUnraisable(...)`, which runs
the exception format machinery **and** `sys.unraisablehook`, i.e. arbitrary
Python. That is squarely forbidden under STW. I traced its callers: it runs only
from dealloc/clear paths, which (per STW-0002's proof) execute world-started.
**Not currently STW-reachable** — but it is one call-graph edge away, and it is
the same shape as CPY-0096 / CPY-0084 (`_PyType_Modified_Unlocked` →
watcher + `PyErr_FormatUnraisable("%R")`).

---

### [ACCEPTABLE] STW-0004 — `PyObject_VisitManagedDict` is the file's only non-atomic read of the managed-dict pointer

**Site:** `Objects/dictobject.c:7754`

```c
    PyDictObject *dict = _PyObject_ManagedDictPointer(obj)->dict;
```

Every other read of that field in the file goes through `_PyObject_GetManagedDict()`,
which is `FT_ATOMIC_LOAD_PTR_ACQUIRE` (`Include/internal/pycore_object.h:941-944`);
every write uses `FT_ATOMIC_STORE_PTR*` (`:7373`, `:7486`, `:7791`, `:7818`,
`:7853`, `:7943`, `:8058`).

**ACCEPTABLE for this class**: `PyObject_VisitManagedDict` is a `tp_traverse`
helper, and all 7 `tp_traverse` dispatch sites in the free-threaded build are
inside STW regions (table in §2), so no concurrent writer exists and the plain
read is sound. Flagged only as a cross-reference for `scan_ft_races`
(`atomic_plain_asymmetry`, 17 in this slice) — the asymmetry is real, its safety
argument is STW, and that argument should be written down, because
`PyObject_VisitManagedDict` is **public API** (`Include/cpython/object.h:453`,
no leading underscore, 8 callers incl. `Modules/_asynciomodule.c` and
`Objects/typevarobject.c`) whose STW precondition is nowhere documented.

---

## 4. Classes bounded — clean *here*, with denominators

| class | denominator | result |
|---|---|---|
| dict/set opening an STW region | 404 functions / 2 files / 11,825 lines | **0** — structural; neither file contains the token |
| Nested `_PyEval_StopTheWorld` inside a region | 0 regions in slice | vacuous |
| `tp_traverse` under STW invoking Python / allocating / raising / locking | 7 functions (`dict_traverse`, `dictiter_traverse`, `dictview_traverse`, `PyObject_VisitManagedDict`, `set_traverse`, `set_next`, `setiter_traverse`) | **0 violations.** All are pure struct reads + `Py_VISIT`. The closure calls no `PyObject_*`, no allocator, no `PyErr_*`, and takes no lock |
| `tp_clear` running under STW (the `__del__`-in-clear shape the brief asked for) | 3 slots (`dict_tp_clear`, `set_clear_internal` ×2) | **0** — `delete_garbage` is called at `gc_free_threading.c:2176`, after `StartTheWorld` at `:2161`. `Py_DECREF`→`__del__` during clear is therefore *not* an STW violation. (The unlocked-clear consequence is STW-0002.) |
| Deallocation reaching `dict_dealloc`'s `PyDict_EVENT_DEALLOCATED` (`dictobject.c:3652`) under STW | 2 GC regions | **0** — deliberate deferral. `queue_untracked_obj_decref` (`gc_free_threading.c:~880`) comments *"so that we can decref and deallocate the object once we start the world again"*; `process_delayed_frees` uses `_PyMem_ProcessDelayedNoDealloc`. No DECREF-to-zero occurs while stopped |
| Dict watcher machinery (`_PyDict_SendEvent`, `dictobject.c:8298`) reachable under STW | 5 dict-reaching regions | **0** — it is the slice's real arbitrary-code gateway (user `PyDict_WatchCallback` at `:8309`, plus `PyErr_FormatUnraisable` at `:8315`), but it fires only from `_PyDict_NotifyEvent` on mutation, and the only dict mutated under STW is `_PyThread_CurrentFrames`' fresh unwatched local (`_ma_watcher_tag == 0`) |
| `_PyDict_MaybeUntrack` under STW | tree-wide grep | **0** — the symbol no longer exists in the tree. The FT GC's `gc_maybe_untrack` (`gc_free_threading.c:482`) handles `PyTuple_CheckExact` only and never enters dict/set |
| `_PyObject_LookupSpecial` / `PyObject_Hash` / `PyObject_RichCompareBool` reachable from an STW region into dict/set | 7-function traverse closure + 4 direct entry points | **0 from traverse.** From `PyDict_SetItem` under `StopTheWorldAll`: hash/compare do run, but both operands are `PyLong` (`PyLong_FromUnsignedLong`, `pystate.c:2754`), so dispatch resolves to `long_hash` / C richcompare — the briefing's "statically-known type slot" class, no user code |
| Split-table OOB in `dict_traverse` (`ma_values->values[i]` bounded by `dk_nentries`, not `values->capacity`) | 1 site | **safe.** `capacity = shared_keys_usable_size(keys) = dk_nentries + dk_usable` at values-creation (`dictobject.c:1009-1010`, `:7328-7331`); `dk_usable` is monotonically non-increasing and floored at 0 (`:1910`, `:1938`), so `dk_nentries` can never exceed a previously-snapshotted capacity |
| `uint8_t` truncation of `values->capacity` (`dictobject.c:924`, `:7331`) | 2 casts | **impossible.** `SHARED_KEYS_MAX_SIZE == 30` (`pycore_dict.h:255`), far below 255 |
| `Py_BEGIN_CRITICAL_SECTION` deadlock under *per-interpreter* STW | 46 regions | **0** — structurally discharged by the `critical_section.c:50` bypass. The runtime-wide case is STW-0001 |

**Confirmed, not re-litigated** (one line each, per informed-mode rule 1):
CPY-0096 (`insert_split_key`, dictobject.c:1971) and CPY-0107
(`compare_unicode_generic` under `LOCK_KEYS`, :1385) are lock-order/re-entrancy
defects under `LOCK_KEYS`/`dk_mutex`, not StopTheWorld regions — neither is in
this class and both remain as recorded. CPY-0015, CPY-0019, CPY-0079, CPY-0115
are likewise outside the STW class.

---

## 5. Toolkit feedback

### Recall gap (the highest-value output): the scanner models only the *outbound* direction

`scan_stw_safety.py` answers "which functions open a StopTheWorld region and call
something unsafe inside it." On this slice the correct answer is *none*, and the
scanner returns it correctly and quickly. But **100% of the slice's actual STW
risk is inbound** — functions defined here that execute while another file has
stopped the world. The scanner surfaced **0 of the 7** such functions, and it
cannot: `scan_stw_safety.py:535` short-circuits any file lacking the literal
`_PyEval_StopTheWorld` token, so `dictobject.c` and `setobject.c` are discarded
before function classification begins.

This is the same structural blind spot as the documented intra-file limitation,
but one level worse: the intra-file note says "a helper called during STW but
defined in another translation unit is reported as `stw_unknown_call`". Here the
helper is reached through a **function pointer** (`tp_traverse`, `tp_clear`), so
it is not even a named call — no `stw_unknown_call` is emitted, and nothing
signals that anything was missed. Four of the five dict/set-reaching regions
tree-wide reach the slice indirectly.

**Proposal 1 — `stw_reachable_functions` (a second pass).** Add an inverse mode:
build a tree-wide inventory of STW regions once (54 sites; cheap — one grep pass),
resolve `tp_traverse` / `tp_clear` / `tp_dealloc` slot registrations to their
functions, then classify *those* against the same safe/unsafe vocabulary. Emit
them in a new envelope field so a slice with zero regions still gets a non-zero
denominator. This would have surfaced all 7 slice functions automatically and
would have flagged STW-0003's `PyErr_NoMemory` mechanically.

**Proposal 2 — a `stw_premise_check` rule.** The load-bearing error in this slice
is not a call inside a region; it is code *outside* a region that claims to be
inside one. `Objects/weakrefobject.c:163` states "The world is stopped during GC
in free-threaded builds. It's safe to call this without holding the lock." inside
a `tp_clear` — and `delete_garbage` runs `tp_clear` 15 lines after
`StartTheWorld`. A grep-grade rule ("comment asserting world-stopped inside a
function registered as `tp_clear`/`tp_dealloc`/`tp_finalize`") is cheap, and this
is the second such stale-premise defect the campaign has hit (CPY-0107's
prohibiting comment postdated the violating code by 26 months).

**Proposal 3 — model the runtime/interpreter flag split.** The scanner already
knows both primitives (`_STOP_PRIMITIVES` at `scan_stw_safety.py:255` includes
`_PyEval_StopTheWorldAll` — *no gap there, credit where due*). But it treats them
as equivalent. They are not: `Py_BEGIN_CRITICAL_SECTION` is a no-op under
`StopTheWorld` and a real `PyMutex_Lock` under `StopTheWorldAll`
(`critical_section.c:50` reads `interp->…`, `pystate.c:2531` writes `runtime->…`).
A lock acquisition inside a `StopTheWorldAll` region deserves its own finding
type; inside a `StopTheWorld` region it deserves silence. That single distinction
turns STW-0001 from hand analysis into a scanner rule.

### Precision

No precision measurement is possible: the rule produced zero findings on zero
candidates. The three CONSIDERs and one ACCEPTABLE above came entirely from
reading, consistent with lesson 1 (pass-2's scanners contributed 1 net-new live
finding out of 24 FIX).

### Wrapper-resolution fix

Inert on this slice — see §1. Reports `stw_wrapper_count: 0` / `stw_wrappers: []`
correctly and invents nothing. A positive test needs a file that opens a region.

---

## 6. Noticed outside slice (one line each)

- `Objects/weakrefobject.c:163` — `gc_clear` (a `tp_clear`) justifies skipping the lock with "The world is stopped during GC in free-threaded builds"; `delete_garbage` dispatches `tp_clear` at `gc_free_threading.c:2176`, after `StartTheWorld` at `:2161`. Same false premise as STW-0002, stated explicitly. Recommend routing to the lock-discipline or ft-races owner.
- `Python/critical_section.c:50` and `:69` — read `tstate->interp->stoptheworld.world_stopped`; `_PyEval_StopTheWorldAll` (`pystate.c:2531`) sets only `runtime->stoptheworld.world_stopped`. The bypass silently does not apply to the 8 runtime-wide regions. This is the root of STW-0001 and belongs to lock-discipline.
- `Python/instrumentation.c:2130`, `:2022`, `:2050`, `:2070` — `PyErr_Format` reachable inside the `:2122` / `:2368` / `:2453` STW regions, while `instrumentation.c:2483` and `codeobject.c:3562` deliberately moved the same call *after* `StartTheWorld`. That asymmetry is unexplained and is the `python-instrumentation` slice's to resolve.
- `Objects/object.c:3410` `PyRefTracer_SetTracer` — invokes a user-supplied `PyRefTracer` C callback at `:3412` inside a `StopTheWorldAll` region; a tracer that touches a shared dict/set would hit STW-0001's real-lock path.
- `Objects/dictobject.c:8587-8588` — `PyFrozenDict_Type` wires `dict_tp_clear`, so the GC can mutate a "frozen" dict whose `frozendict_hash` result is cached. Outside the STW class (runs world-started) and the object is garbage, but worth a line for whoever owns frozendict.
- `Python/gc_free_threading.c:2674-2676` — `PyObject_GC_Track` runs `tp_traverse` under `#ifdef Py_DEBUG` with the world **running**, the only such dispatch in the FT build. Harmless for this slice (every track site in both files uses the `_PyObject_GC_TRACK` macro, which skips the validation), but it breaks the otherwise-clean "all `tp_traverse` runs under STW" invariant that STW-0004's safety argument rests on.

---

## 7. Artifacts

- `reports/obj-mappings/repro/stw_all_dict_critical_section.py` — STW-0001, 5/5 no deadlock, `debug-ft-nojit`.
- `reports/obj-mappings/repro/stw_set_tp_clear_unlocked.py` — STW-0002, 8/8 clean on `debug-ft-nojit`, 0 TSan warnings on `debug-ft-nojit-tsan`.

**Summary line:** FIX=0 CONSIDER=2 POLICY=0 ACCEPTABLE=2 — no live StopTheWorld
contract violation in `Objects/dictobject.c` or `Objects/setobject.c`.
