# Informed-exploration briefing — CPython C code review

You are running as part of an **informed** explore. Unlike a cold run, you have the catalog of recurring CPython C bug SHAPES (reusable templates — not file:line, since the scope differs every run), the cross-cutting triage rules, and the false-positive taxonomy. Use them: the goal is fix-propagation (find *every* instance of a known shape) and genuinely new territory, not re-discovering the catalog.

## Your three informed-mode rules

1. **Confirm, don't re-litigate.** If a candidate matches a previously-recorded finding (the catalog section below, if present), tally it in one line and move on — do not re-describe a known bug.
2. **Skip the known false-positive classes** (the taxonomy below). If you flag something that falls in one of those classes, you must say *why this instance is NOT that FP class*.
3. **Hunt siblings via the guarded twin.** For each bug SHAPE relevant to your scope, locate its **guarded twin** (the correctly-handled sibling in the same file/family = the fix), then search for the other sites that lack it — those un-found siblings are the point of an informed run.

## Cross-cutting triage rules (apply to every finding)

- The guarded twin is the strongest static-review signal: nearly every real CPython bug has a correctly-handled sibling in the same file — cite it as the fix and hunt for other unfixed siblings.
- Both-crash != acceptable: if the differential shows released CPython ALSO crashes on the input, that is not proof it is fine — search the CPython tracker (label:type-crash) and treat a pure-Python-reachable segfault as a bug in both.
- Reachability first: rank a candidate by whether its triggering value flows from Python-controlled input (a parsed arg, a user __index__/__hash__/__repr__, a user-supplied index/length) vs an internal invariant (assert / 'cannot fail').
- Confirm-don't-relitigate: a finding already recorded in the findings catalog is context, not a fresh discovery — confirm it still reproduces and move on to siblings.
- Class J (abort-vs-MemoryError) is out of scope; a wrong-size write or a swallowed error is in scope.

## Bug-shape catalog — sibling-hunt templates

6 recurring shapes. For each, the **guarded twin** is the correctly-handled sibling that both confirms the finding and *is* the fix.

### unguarded-recursion-in-slot — Recursion-prone slot without Py_EnterRecursiveCall
- **severity (default before triage):** FIX
- **pattern:** A tp_hash / tp_richcompare / tp_repr / tp_str slot (or a generic-alias parameter walk) that descends a user-controlled object graph — self-recursion, or a loop calling PyObject_Hash/Repr/RichCompare on items — with no Py_EnterRecursiveCall / Py_ReprEnter.
- **guarded twin (the fix):** A sibling slot in the same file that DOES bracket its descent with Py_EnterRecursiveCall()/Py_LeaveRecursiveCall() or Py_ReprEnter()/Py_ReprLeave().
- **hunt:** For every container/aggregate type, check tp_hash, tp_richcompare, tp_repr, tp_str for symmetry: if repr is guarded but hash is not, hash is suspect. Grep for copy-pasted hash algorithms across container types.
- **differential (how to confirm):** Build a deeply-nested or self-referential instance on a debug CPython and trigger the slot; a SIGSEGV (not RecursionError) confirms. A native stack overflow is a bug even if released CPython also crashes.
- **confirmed examples:** gh-154318 tuple_hash/frozendict_hash, gh-154275 _Py_make_parameters, gh-149146 tuple_dealloc
- **surfaced by:** `scan_recursion_guards.py`

### pyerr-clear-in-destructor — PyErr_Clear() clobbers an in-flight exception during teardown
- **severity (default before triage):** FIX
- **pattern:** A tp_dealloc / tp_clear / tp_finalize calls PyErr_Clear() (or drops a fallible result) with no surrounding save/restore of the exception state; the destructor can run while an exception is already pending.
- **guarded twin (the fix):** A destructor that captures with PyErr_GetRaisedException() at the top and restores with PyErr_SetRaisedException() at the bottom (or reports via PyErr_WriteUnraisable).
- **hunt:** Across a module family, check every dealloc/clear/finalize that calls a fallible API on a cleanup path; the ones that PyErr_Clear() without save/restore swallow MemoryError/KeyboardInterrupt.
- **differential (how to confirm):** Under set_nomemory OOM injection, raise MemoryError, then trigger the teardown; the swallowed error becomes a silent success/hang.
- **confirmed examples:** gh-152083 context_tp_dealloc, OOM-0039 deque_clear, gh-146102 PyErr_Clear-on-success sweep (upstream, in progress)
- **surfaced by:** `scan_pyerr_clear.py`

### dealloc-of-uninitialized-object — Half-constructed object freed on an error path
- **severity (default before triage):** FIX
- **pattern:** A constructor allocates via a non-zeroing allocator (PyObject_New/PyObject_GC_New/...), then on a fallible-step failure Py_DECREFs the object before its members are NULL-initialized; tp_dealloc reads garbage member pointers.
- **guarded twin (the fix):** A constructor that NULL-initializes all members (or memsets the object) immediately after allocation, before any fallible call.
- **hunt:** For each non-zeroing allocation with an error path that frees the object, read the tp_dealloc: if it Py_XDECREFs / switches-on-enum a member the constructor sets late, it crashes on that path.
- **differential (how to confirm):** set_nomemory to fail the exact allocation on the error path, then construct from Python; a crash in tp_dealloc confirms (OOM class O5).
- **confirmed examples:** gh-151815 template_iter, gh-152851 blake2 .copy()
- **surfaced by:** `scan_uninit_dealloc.py`

### borrowed-ref-across-call — Borrowed reference used across a call that can free it (crown jewel)
- **severity (default before triage):** FIX
- **pattern:** A borrowed pointer loaded from a slot field or container (PyList/Tuple/Dict_GET_ITEM) is used after an intervening call that can run arbitrary Python (PyObject_Call*, PyObject_Repr/Str, converter callbacks like PyUnicode_FSConverter, warnings, GC) with no intervening Py_INCREF.
- **guarded twin (the fix):** The same access pattern elsewhere that Py_INCREFs the borrowed ref before the call and Py_DECREFs after.
- **hunt:** Trace every borrowed load feeding a call that can execute Python; the free may come from a __del__, __fspath__, setcontext, or a re-entrant callback.
- **differential (how to confirm):** Supply an adversarial object whose callback drops the last strong ref (mutating __fspath__/__eq__/__index__), then observe UAF on ASan.
- **confirmed examples:** gh-148382 _decimal CURRENT_CONTEXT, gh-151403 _posixsubprocess __fspath__, gh-154527 defaultdict default_factory (FT)
- **surfaced by:** `scan_refcounts.py (widen the Python-reaching call set)`

### return-null-without-exception — Return NULL without setting an exception / stale-exception desync
- **severity (default before triage):** FIX
- **pattern:** A PyObject*-returning function returns NULL with no prior PyErr_Set*, or an entry point trusts res!=NULL while PyErr_Occurred() is set.
- **guarded twin (the fix):** Sibling error paths in the same function that set an exception before returning NULL.
- **hunt:** OOM paths are the usual offenders — a constructor whose allocation-failure branch returns NULL but forgets PyErr_NoMemory().
- **differential (how to confirm):** set_nomemory to force the failure; a SystemError('returned NULL without setting an exception') confirms.
- **confirmed examples:** gh-151968, gh-151126 (missing PyErr_NoMemory cluster)
- **surfaced by:** `scan_error_paths.py (return_null_no_exception)`

### integer-overflow-in-allocation — Allocation size from a Python-controlled multiply with no overflow guard
- **severity (default before triage):** CONSIDER
- **pattern:** PyMem_*/malloc(n * size) where n derives from a Py_ssize_t parsed from Python args, with no `n > MAX/size` guard and no safe-multiply helper.
- **guarded twin (the fix):** Sibling allocations that use PyMem_New / a checked multiply / an explicit bound.
- **hunt:** Every length/count argument that reaches an allocation multiply is a candidate; abort-vs-MemoryError is out of scope, wrong-size-then-write is the bug.
- **differential (how to confirm):** Pass a length near SIZE_MAX/elem; a segfault (vs a clean MemoryError) confirms the overflow.
- **confirmed examples:** gh-3493, gh-1779
- **surfaced by:** `memory-pattern-analyzer (promote to a real scanner in a later slice)`

## Previously-recorded findings (confirm, don't re-litigate; hunt siblings)

From the `cpython-review-findings` catalog (2 recorded). These are context, not fresh discoveries: confirm each still reproduces in one line, then spend your effort on **un-found siblings** of the same shape (via its guarded twin).

| id | category | site | title | status |
|----|----------|------|-------|--------|
| CPY-0001 | recursion | `Objects/tupleobject.c (tuple_hash); Objects/unionobject.c (union_hash)` | tuple_hash has no recursion guard: a deeply-nested / cyclic tuple overflows the native C stack (SIGSEGV) instead of raising RecursionError | static-confirmed |
| CPY-0002 | recursion | `Objects/genericaliasobject.c:231 (_Py_make_parameters)` | _Py_make_parameters walks nested type arguments self-recursively with no Py_EnterRecursiveCall: `list[x]` on deeply-nested args overflows the C stack (SIGSEGV) | static-confirmed |

## Known false-positive classes — DO NOT re-report (justify if you flag one)

The full taxonomy lives in `data/cpython_non_bugs.md`; it is reproduced here so every agent sees it inline.

# CPython false-positive taxonomy (cpython-review-toolkit)

The precision-decay guard for the informed-explore loop. Each entry is a pattern
the scanners *can* surface but that is usually **not** a bug in CPython's own
code. During triage, skip these classes — or explicitly justify why this instance
is different. Add to this file whenever a review confirms a new FP class.

---

## PyErr_Clear / exception state

- **`PyErr_Clear()` after a sentinel-returning lookup is idiomatic.** After
  `PyObject_GetAttr` / `PyDict_GetItemWithError` / `PyMapping_GetOptionalItem`
  where a missing key is expected, clearing an `AttributeError`/`KeyError` is
  correct — *outside* the destructor family. The `pyerr-clear-auditor` is scoped
  to dealloc/clear/finalize precisely to avoid this; a hit there is real.
- **A destructor that already saves/restores** (`PyErr_GetRaisedException` /
  `PyErr_Fetch` / `PyErr_WriteUnraisable`) is fine — but verify the save/restore
  actually brackets the flagged clear in a large function (whole-function
  suppression can hide a second, unguarded clear).

## Recursion guards

- **Guarded by the dispatcher — but NOT for hash.** A leaf slot reached only
  through `PyObject_Repr` (`Objects/object.c:759`), `PyObject_Str` (`:800`) or
  `PyObject_RichCompare` (`:1099`) is safe *if it is never reached directly*,
  because those three do wrap `_Py_EnterRecursiveCallTstate`. Confirm the call
  graph before dismissing.

  **`PyObject_Hash` (`Objects/object.c:1158`) has NO recursion guard.** Verified
  against main @ 3.16.0a0. A `tp_hash` slot that descends into element hashes is
  therefore unguarded at *every* level and overflows the native C stack (SIGSEGV,
  not a catchable `RecursionError`). Never dismiss a hash-descent finding as
  "dispatcher-guarded": that asymmetry between the four dispatchers is exactly
  what makes `tuple_hash` (gh-154318 / CPY-0001), `union_hash`, `ga_hash` and
  `frozendict_pair_hash` real bugs. This entry previously listed `PyObject_Hash`
  among the guarded dispatchers, which was factually wrong and would have
  suppressed the entire true-positive class.
- **Non-nestable receiver.** A `*_hash`/`*_repr` on a type whose elements can
  never be the same or another container (e.g. a code object's fixed fields) can
  descend without a guard because the depth is bounded. ACCEPTABLE — but state
  the bound.
- **Iterative deallocation bounds a `tp_dealloc` — but do NOT look for
  `Py_TRASHCAN_BEGIN` to decide that.** On main (verified @ 3.16.0a0) the old
  macros are **empty backwards-compat shims** — `Include/cpython/object.h:446-447`,
  literally `#define Py_TRASHCAN_BEGIN(op, dealloc)` with an empty body — and
  **zero call sites remain in `Objects/` or `Modules/`**. The live mechanism is
  automatic inside `_Py_Dealloc`, via `_PyTrash_thread_deposit_object()` /
  `_PyTrash_thread_destroy_chain()`.

  So the correct test is *not* "is this dealloc trashcan-protected?" (nothing is,
  by that marker — an agent applying the old test finds no marker anywhere and
  wrongly promotes every dealloc finding). The test is **what the descent goes
  through**: a `tp_dealloc` that recurses only by `Py_DECREF`-ing contained
  objects is bounded by the automatic chain and is a FP; a dealloc that recurses
  some *other* way is not covered and can still overflow the C stack — see
  `gh-149146 tuple_dealloc` (recursion during MemoryError unwind), which is a
  real catalogued bug.

## Uninitialized dealloc

- **Zeroing allocator.** `type->tp_alloc(type, n)` / `PyType_GenericAlloc` /
  `*_GC_Calloc` zero the object; a following early free is safe. The scanner
  excludes these, but a wrapper macro may hide one.
- **`tp_dealloc` guards each member with `Py_XDECREF`** *and* the members were
  NULL-initialized before the failing step — Py_XDECREF(NULL) is a no-op, so no
  crash. Only a member left as *garbage* (not NULL) at the free point is a bug.

## NULL checks / error paths

- **Infallible-by-construction returns.** `Py_None`/`Py_True`/`Py_False`, interned
  singletons, and `_Py_ID(...)` never return NULL; a missing check is not a bug.
- **Checked via a macro the scanner doesn't model.** `Py_SETREF` / `Py_XSETREF`
  and `Py_CLEAR` handle NULL internally; assignments through them are safe.

## Refcounts

- **Borrowed ref under a known-live owner.** A borrowed item is safe across a call
  if a strong reference is provably held elsewhere for the duration (e.g. the
  container is a local the callee cannot reach). Establish the owner before
  dismissing a borrowed-across-call finding.
- **Stolen-ref APIs used correctly.** `PyList_SET_ITEM`/`PyTuple_SET_ITEM` on a
  freshly-created, not-yet-published container are the normal fast path.

## Free-threading (when the FT detectors land)

- **Immortal objects** (`_Py_IMMORTAL_REFCNT`) are not raced by refcount ops.
- **Access under `Py_BEGIN_CRITICAL_SECTION`** for the relevant object is
  protected; a plain read is only a race if a *concurrent* writer exists without
  the same critical section.
