# obj-typeobject pass 2 — running findings ledger

Accumulated as agents report. Becomes `cpython-review-findings` records at synthesis.
Next free catalog ID at run start: **CPY-0091**.

## FIX — reproduced

### P2-F1 · `do_super_lookup:12656` — heap-UAF via re-entrant `super.__init__`
**Proposed record:** CPY-0091, category `refcount`.
**Source:** init-bypass-checker (found by READING, not by its scanner — and not even its own bug class).
**Shape:** briefing shape 4 (borrowed-ref-across-call) in its **re-init-in-place** form.

`super_getattro:12699` passes `su->type` / `su->obj` / `su->obj_type` as raw **borrowed** pointers.
`_PySuper_LookupDescr:12622` calls `PyDict_GetItemRef(dict, name, &res)` — and `name` arrives straight
from `getattr()`, so a `str` subclass with a Python `__hash__` runs arbitrary Python inside that lookup.
Calling `super.__init__(s, T, o)` on the same object from there executes the three `Py_XSETREF`s at
`:12950-12952`, freeing the old values. `do_super_lookup` then hands the dangling pointers to the
descriptor `__get__` at `:12656`.

**Reproduced:** exit 139 on `debug-gil-nojit`, `release-gil-nojit`, `debug-ft-nojit`, **and released
3.14.4** → longstanding, not a regression. ASan: `heap-use-after-free`, freed at `super_init_impl:12951`,
read at `do_super_lookup:12656`. An 8-probe crash/survive split isolates the sink exactly.

**Guarded twin — nine lines away.** `_PySuper_LookupDescr:12597-12601` pins `mro` with a `_PyCStackRef`,
commented *"can be replaced during PyDict_GetItemRef"*. Same window, one value pinned, two not.
**Second twin:** `_PySuper_Lookup:12771-12776` holds a **strong** `su_obj_type` — which is why the
zero-arg `super()` eval-loop path survives (verified).
**Lineage:** gh-58407 (2012, `type-crash`) and `bdf6de8c3f0` / gh-145685 both recognised this window —
for `mro` only. No prior art on the tracker for the other two values.
**Family:** seventh confirmed catalog member of this shape, after the six `_struct` records
CPY-0044/0045/0046/0048/0049/0050 — now in a second file.

**INDEPENDENTLY CONFIRMED by refcount-auditor**, arriving from the borrowed-ref lane rather than the
init-bypass lane, with the evidence sharpened: **SIGSEGV on all four configurations**
(debug/release × GIL/FT) — **deferred refcounting does NOT mask it**, unlike CPY-0068/0069. Sink chain
`func_descr_get → PyMethod_New → Py_INCREF` on freed memory. It also names a *different* guarded twin:
`super_descr_get:12806` re-reads `su->type` after its Python-reaching call instead of carrying a stale
local. Two agents, two lanes, same defect.

### P2-F5 · `type_mro_modified:1299` — borrowed MRO freed by a re-entrant `__bases__` assignment
**Source:** refcount-auditor. ASan-confirmed heap-use-after-free, ~40 lines of pure Python, no imports.
`mro_internal:3678` publishes the new MRO and hands the **same borrowed pointer** to
`type_mro_modified`, which runs `has_custom_mro` → a metaclass dict lookup → user `__eq__` →
re-entrant `T.__bases__ = …`, whose rollback-list release at `type_set_bases_unlocked:1952` drops the
last reference.
**Why the existing defence misses it:** `mro_internal`'s own identity-test at `:3667` sits **before**
`set_tp_mro`; everything after it is unprotected.
**Guarded twin:** `find_name_in_mro:6172` pins with a `_PyCStackRef`.

### P2-F2 · `type_update_dict:6714` — unnarrowed `AttributeError` destroys MemoryError
**Proposed record:** new, category `error-path`.
**Source:** pyerr-clear-auditor.
Every `_PyDict_SetItem_LockHeld` failure is converted to `PyErr_Format(PyExc_AttributeError, ...)`.
The conversion is right for one case (`del T.x` → `KeyError`) and applied to all.
**Reproduced on 3 builds:** under single-allocation OOM injection, `T.x = 1` raises
`AttributeError: type object 'T' has no attribute 'a15'` with `__context__=None` — the MemoryError is
destroyed. Same script, same injection index, the **instance** attribute path returns `MemoryError`
correctly.
**Guarded twin:** the identical two-call conversion at `Objects/object.c:2104`, gated on
`PyErr_ExceptionMatches(PyExc_KeyError)`. Sibling hunt: all 11 `_PyObject_SetAttributeErrorContext`
sites in the tree read — `typeobject.c:6718` is the **only** unguarded one. No prior art.

## Widenings of existing records (do NOT open new records)

### CPY-0074 / CPY-0077 — reachability corrected, impact escalated
**Source:** pyerr-clear-auditor.
- **Reachability:** the trigger does NOT need the construct CPython warns about at class creation.
  `_PyDict_HasOnlyStringKeys` uses `PyUnicode_Check`, so a **`str` subclass** key passes the gh-55664
  check with **no warning at all**, while still forcing `DICT_KEYS_GENERAL` and a user `__eq__`.
- **Impact:** not merely a wrong `AttributeError`. `pickle.dumps` returns a valid pickle that
  round-trips to the **wrong value** with the `KeyboardInterrupt` gone; `copy.copy` ignores `__copy__`;
  `__reduce__` is ignored. The consumers' defensive `else if (PyErr_Occurred())` checks are dead by design.
- **Two in-file guarded twins**, both in pass-2 regions, both doing the same lookup against the same
  hostile key and propagating correctly: `_PySuper_LookupDescr:12622` and `type_update_dict:6703`.

### P2-F3 · `o.__class__ = B` runs user Python **inside the stopped world** — `typeobject.c:7609`
**Source:** null-safety-scanner (found by reading; not its own bug class). Routed to stw-safety-checker.
**Reproduced:** 12 lines of pure Python — `__slots__` holding a `str` subclass; `_Py_Mangle` returns the
same object, so the subclass survives into `ht_slots`, and its `__eq__` is then invoked with the world
stopped. **SIGABRT on debug-ft-nojit** (`Assertion '!interp->stoptheworld.world_stopped' failed`),
**silent on release-ft** — i.e. the Python really does run with the world stopped; the debug build is
the only thing that says so.
**Full gdb chain:** `object_set_class:7826 → object_set_class_world_stopped:7763 →
compatible_for_assignment:7683 → same_slots_added:7609 → slot_tp_richcompare:11074`.
**Guarded twin:** `_PyType_SetFlagsRecursive:6506-6527` does its Python-invoking work *before* stopping
the world. No prior art (3 tracker searches, 0 hits).

**INDEPENDENTLY CONFIRMED by memory-pattern-analyzer**, which reached the same site from the CPY-0078
sibling hunt and added two outcomes: **`release-ft-nojit` HANGS** (nested `_PyEval_StopTheWorld`), and
`debug-ft-nojit` hard-aborts on `Python/ceval.h:148 check_invalid_reentrancy` — whose own comment reads
*"quite likely in the painfully complex typeobject code"*. Two agents, two lanes, same defect.
**Fix direction:** hoist the pure predicate `compatible_for_assignment` out of the stopped region.

### P2-F4 · `object_set_class:7825` — stale `oldto` across a re-entrant assignment → over-DECREF of a type
**Source:** memory-pattern-analyzer, via the informed sibling hunt on recorded CPY-0078.
`oldto = Py_TYPE(self)` is captured **before** `object_set_class_world_stopped`, which reaches
`same_slots_added:7609`'s `PyObject_RichCompareBool` on the two `ht_slots` tuples — a user `__eq__`
(str subclasses survive into `ht_slots`, per CPY-0078). If that `__eq__` reassigns `self.__class__`, the
outer call `Py_SET_TYPE`s over the intermediate type (leak) and `Py_DECREF`s `oldto` a **second** time
at `:7832`.
**Measured:** `A 5→3` (should be `5→4`), `C 4→5`.
**Outcomes:** SIGSEGV on `release-gil-nojit`; `_Py_NegativeRefcount` on `debug-gil-nojit`;
`gc_decref: refcount is too small` from a **single** round; ASan **heap-use-after-free READ** on the
1040-byte type object on `release-gil-nojit-asan`.
**Guarded twins, same file:** `subtype_dealloc:2860` — `/* Extract the type again; tp_del may have
changed it */` — and `mro_internal:3667`'s `reent` comparison.
**No prior art** (four `gh api search/issues` query forms).
**Note:** one ~20-line pure-Python script drives both P2-F3 and P2-F4.

### P2-F6 · `insert_split_key` → `_PyType_Modified_Unlocked` — reproduced PERMANENT HANG (FT)
**Source:** lock-discipline-checker. This is the history preflight's **#1 target**, live.
`Objects/dictobject.c:insert_split_key` holds `keys->dk_mutex` — a **non-recursive** raw `PyMutex`
taken `_Py_LOCK_DONT_DETACH`, so `SuspendAll` never releases it — and calls
`_PyType_Modified_Unlocked`, which runs **arbitrary Python** via a watcher callback
(`typeobject.c:1222`) and `sys.unraisablehook` (`:1223`).
**Reproduced:** hangs on `debug-ft-nojit` and `release-ft-nojit`; completes cleanly under the GIL.
gdb proves it: frames #24 and #11 hold the **same** `keys=0x2000286dc98`, blocked on
`0x2000286dca3` = `&keys->dk_mutex`.
**Provenance:** the **fourth** iteration of a path already reverted once for deadlock (gh-152238).
Take-2 landed **19 days before HEAD**, and **every related issue is closed** — nothing open covers it.
**Guarded twin:** `type_update_dict:6694`.

### P2-C2 · `tp_watched` written under `TYPE_LOCK` at `:1112`, bare at `:1129` — reproduced data race
**Source:** lock-discipline-checker. **6 TSan races, `Write of size 1`.**
**Not test-only:** `Python/optimizer_analysis.c:177` arms this bit for the tier-2 JIT, so a lost SET
means a modified type never invalidates its executor.
**Class bounded:** a 35-field sweep found this is the **only** truly asymmetric field in the file.

### THIRD CONVERGENCE on P2-F3 / P2-F4 — stw-safety-checker, with the strongest evidence
Three agents in three lanes (null-safety, memory-pattern, stw-safety) independently reached the same
two defects. The STW lane adds the decisive material:

**On P2-F3 (Python inside the stopped world) — it corrects a pass-1 ACCEPTABLE ruling.**
Pass 1 dismissed `same_slots_added:7609` because *"`ht_slots` is a tuple of strings"*. That is wrong on
one word: `valid_identifier` uses **`PyUnicode_Check`, not `CheckExact`**, and `_Py_Mangle` returns the
object unchanged — so a `str` subclass with `__eq__` survives into `ht_slots` and dispatches through the
eval loop. **Permanent interpreter hang on `release-ft-nojit` at BOTH `PYTHON_GIL=0` and `PYTHON_GIL=1`,
from four independent pure-Python triggers — including a bare `gc.collect()`.**

**On P2-F4 (stale `oldto`) — it is NOT free-threading-only, and it is a named regression.**
**Reproduced on the default GIL build, single-threaded:** ASan heap-use-after-free; SIGSEGV on
`release-gil-nojit` after 2 iterations; Fatal Python error on `debug-gil-nojit`.
**The guarded twin is literally the three lines that gh-120198 (`3bfc9c831ad9`, 2024-07-11) DELETED** —
an explicit `oldto = Py_TYPE(self)` re-fetch immediately before `Py_SET_TYPE`, with a comment saying
why it was there. Same shape as CPY-0069 being a regression of gh-30775.

### P2-F7 · `solid_base:3776` — unguarded self-recursion; trigger is `class X(Deep): pass`
**Source:** recursion-guard-auditor, found by READING the MRO region (its scanner missed it — see D-13).
**Reproduced: exit 139 on debug AND release @4000/100 KB**, with a shallow-base control that survives.
The trigger is the most ordinary construct in the whole family: `class X(Deep): pass`.
**Pass 1 dismissed this on a compiler claim that `objdump` disproves** — the recursive call is **not** in
tail position, and both debug and release binaries contain a literal `call solid_base` with a 32-byte
frame.
**Method lesson (propose for the FP taxonomy):** never dismiss a self-recursion on an *assumed* tail
call without checking `objdump`. A surviving small-stack probe is evidence about frame **size**, not
frame **count**.

### P2-F8 · CPY-0087 widened — `_PyType_Modified_Unlocked:1206` trigger drops to `obj.newattr = value`
**Source:** recursion-guard-auditor. Confirms and **widens** a recorded finding.
`Objects/dictobject.c:1971` passes `dsk_owning_type`; nothing bounds the subclass-tree descent, and the
call is **not** inside `#ifdef Py_GIL_DISABLED`. So the trigger falls from *class mutation* to an
ordinary **instance attribute store**.
**Reproduced:** exit 139 on debug-gil, debug-ft and release-gil. The byte-identical control that drains
version tags on the main thread first **survives**, isolating the crash to `:1206`.
**POLICY hand-off (to lock-discipline / ft-race):** that descent, plus watcher callbacks and
`PyErr_FormatUnraisable("%R")` (→ metaclass `__repr__`), now runs under `LOCK_KEYS` =
`PyMutex_LockFlags(..., _Py_LOCK_DONT_DETACH)` *and* the types-mutex critical section. This is the same
lock the P2-F6 deadlock hangs on.

### P2-F9 · `_PyType_Modified_Unlocked:1195-1208` — cursor invalidation UAF, ASan 3/3 deterministic
**Source:** gil-discipline-checker (from the "runs user Python under exclusivity" remit, not the GIL rule).
The loop iterates `tp_subclasses` with a **borrowed** pointer and a **live `PyDict_Next` cursor**. The
recursive call at `:1206` fires the subclass's watcher notification, which reaches
`PyErr_FormatUnraisable` at `:1223` — running arbitrary Python **twice**: `sys.unraisablehook`, and
`PyObject_Repr(type)` via the `%R`. That Python can reparent the subclass →
`remove_subclass:9791` → `Py_CLEAR(tp_subclasses)` → **frees the dict being iterated**.
**Reproduced:** ASan on `debug-gil-nojit-asan`, **3/3 deterministic**. Chain: `_PyDict_Next` ←
`_PyType_Modified_Unlocked:1201` ← `type_update_dict:6712`; freed by `dict_dealloc` ←
`remove_subclass:9791` ← `type_set_bases_unlocked:1965`.
**Not FT-specific, not debug-specific** — it is a re-entrancy bug, so `TYPE_LOCK` is irrelevant to it.
The FT build actually **masks** it from ASan via deferred reclamation.
**Three guarded twins, all in-file:** `_PyType_GetSubclasses:788-789` and
`recurse_down_subclasses:12351-12353` both carry an explicit comment justifying the borrowed reference;
**the buggy loop carries none, and is the only one of the three that provably runs user code.** Third
twin is the direct caller `type_update_dict:6696-6702`, whose comment states the exact violated
invariant: *"We don't want any re-entrancy between when we update the dict and call
`_PyType_Modified_Unlocked`"*.
**Provenance:** the re-entrancy comment at `:1219-1220` was added by gh-127266 during FT hardening — the
hazard was noticed **at the callee**, the defence was never added **at the caller**.
**Prior art checked and distinguished:** open issue gh-151377 covers the *cross-thread* race on a
**different** walker. This is the single-threaded sibling on the GIL build, and gh-151377's proposed
locking would **not** close it.

### P2-F10 · `update_slot_after_setattr:6744` + `update_all_slots:12177` — discarded `-1` in the `#else` arm
**Source:** error-path-analyzer. **This also invalidates part of CPY-0070's write-up — see below.**
Both discard `update_slot`'s `-1` **in the `#else` arm of an `#ifdef` whose other arm tests it**.
**Reproduced:** `T.__len__ = f` / `T.__bases__ = (B,)` → **SIGABRT on debug-gil**, and on release-gil a
`SystemError` **misattributed to `TextIOWrapper.write`**. Correct on FT.
The in-tree comment *"update_slot returns int but can't actually fail in this case"* is **false**.
**⚠ Consequence for CPY-0070:** its cited guarded twin at `:1967` is **dead code on the default build**.
The record's "the twin is the next line, added 2025" claim holds only for the FT arm. Fix the record.

### P2-F11 · `type_set_bases_unlocked` `bail:` is a PARTIAL rollback — and it gates the CPY-0070 fix
**Source:** error-path-analyzer.
The rollback log `temp` is freed at `:1952`, **before** the `:1968` exit. So a *failed* `__bases__`
assignment leaves the class inconsistent: `__bases__ == (A,)` but `__mro__ == (X, B, object)`,
`issubclass(X, B) == True`, `B.__subclasses__() == [X]`.
**⚠ This gates CPY-0070.** Fixing CPY-0070 naively — i.e. just checking `add_all_subclasses` — would
make **this** reachable on the GIL build too. The two must be fixed together; say so when filing.

### P2-F12 · `PyType_Watch:1111` discards `assign_version_tag` — watcher permanently silent
**Source:** error-path-analyzer. **Three lines of Python** via a custom metaclass `mro()`.
Returns success while the watcher will never fire again.
**Guarded twin:** `_PyType_Validate:6471` — same callee, same file, result tested.

### P2-F13 · `set_mro_error:3320` — heap-use-after-free from a plain `class` statement
**Source:** error-path-analyzer (re-verified first-hand); **handed off to refcount-auditor** as not an
error-path shape. Freed one line down at `:3322`. Trigger is an ordinary `class` statement.

### P2-F14 · `object_getstate_default:7990` — SIGSEGV, `pickle.dumps` racing `list.append`
**Source:** error-path-analyzer (re-verified first-hand); **handed off to ft-race-scanner**.

### P2-F15 · the composite: `find_name_in_mro` breaks both halves of the error protocol in sequence
**Source:** error-path-analyzer, from the error-return view of the recorded clears.
`find_name_in_mro` swallows the exception → returns `-1` with **nothing set** →
`_PyType_LookupRefAndVersion:6369` converts that back into **success with a wrong answer**.
Neither half is new on its own (CPY-0076/0077); the *composition* is, and it is what makes the class
user-visible rather than merely untidy.

### P2-F16 · `mro_implementation_unlocked:3503` — CPY-0014 reached from two first-class Python routes
**Source:** uninitialized-dealloc-auditor. **Widens a recorded finding's reachability, not a new record.**
`PyList_New(1)` on a free-threaded build frees a list whose `ob_item`/`ob_size` were never written —
that is **CPY-0014**, catalogued with `marshal.loads` as its **only** reachability. Pass 2 gives it two
ordinary Python routes:
- `class C(A, B): pass` → `type_ready_mro:9259` → `mro_implementation_unlocked:3503`
- `C.__bases__ = (...)` → `type_set_bases_unlocked:1949` → `mro_hierarchy_for_complete_type:1803` → same
**Reproduced:** faulting on `_PyMem_DebugFree(0xcdcdcdcdcdcdcdc5)`, **3/3 runs on `debug-ft-nojit`**,
same sweep indices on `release-ft-nojit` (asserts compiled out) and `release-ft-nojit-asan`. **Both GIL
builds clean at every index.** Preconditions: ≥2 bases (the single-base MRO fast path builds a tuple)
and a drained list freelist.
**Action:** widen CPY-0014's `sites` and `cpython_behavior`.

### P2-F17 · `type_ready` publishes before setting `Py_TPFLAGS_INLINE_VALUES` — heap overflow
**Source:** ft-race-scanner. **Resolves an open question from pass 1** — it was never a slot store.
`type_ready` publishes the type at `:9581`, **then** sets `Py_TPFLAGS_INLINE_VALUES` at `:9487`;
`_PyType_AllocNoTrack` reads that flag at `:2524` to **size** the allocation and at `:2550` to
**initialize** it.
**Reproduced:** ASan **3/3** — allocation at `:2528`, overflow at `:2551`, **on the same thread**,
landing 0 bytes after the region. **20/20 crash on plain FT** from `class Sub(Base)`; **0/10 on GIL**.
**gh-151394 does NOT fix this** (it closes CPY-0072, a different site).

### P2-F18 · `PyType_Unwatch:1129` — observable LOST UPDATE, no sanitizer needed
**Source:** ft-race-scanner. Strengthens P2-C2 from CONSIDER to FIX.
Writes `tp_watched` unlocked; its twin **17 lines up** writes it under `TYPE_LOCK`.
**Three TSan pairs, plus an observable lost update with no sanitizer at all:** 5/40 FT rounds where a
watcher bit **survived its own `PyType_Unwatch`**; **0/40 on GIL**.

### P2-F6 (deadlock) — escalated: forbidden in-tree, and it has GIL-build consequences
**Source:** ft-race-scanner, converging with lock-discipline-checker.
**24/24 FT deadlock vs 0/12 GIL**, gdb stack showing both frames and the same `keys` pointer.
**`Objects/dictobject.c:218-227` explicitly forbids exactly this, naming `PyType_Modified`.**
Four entry points run arbitrary Python inside the raw non-reentrant keys mutex, including the
**user-assignable `sys.unraisablehook`**.
**Three further consequences reproduce on the DEFAULT GIL build:** an ASan heap-buffer-overflow, a
`dk_usable` invariant break **5/5 from a single re-entry**, and unbounded recursion → SIGSEGV.
**Scoping:** those three sub-findings live in `Objects/dictobject.c` and are memory-pattern / recursion
class — flagged for the **`obj-dictobject` slice** so they do not fall between slice boundaries.

### P2-F19 · `type_ready`'s three flag writes are neither STW nor pre-publication
**Source:** ft-race-scanner, **reversing pass 1's dismissal**. Static only, no reproducer.
Pass 1 quoted the "flags are read without locks" sanction. That sanction is **conditional** on writes
being stop-the-world or pre-publication; `type_ready`'s three flag writes are **neither**.

### P2-F20 (H-1) · `recurse_down_subclasses:12386` — user `__eq__` frees the dict being iterated
**Source:** git-history-analyzer, converged on by two independent sweep lanes. **Verified live.**
`PyDict_Contains` at `:12386` runs a user `__eq__` that synchronously frees the dict being iterated, via
`clear_tp_subclasses`. The in-file comment at `:12363` justifies only the **callback** and never covers
this call.
**Guarded twin:** `mro_hierarchy_for_complete_type:1835`, carrying the comment that names the hazard verbatim.
**⚠ CORRECTS THE CATALOG:** CPY-0069 is recorded as *"a regression of gh-30775"*. **PR gh-30775 was
closed UNMERGED** (`merged_at: null`) — so nothing it would have fixed was ever fixed. Its own body
proposes exactly the right fix ("iterate on a **copy** … because its loop can modify subclasses"). What
landed instead was `3a4c15bb9815` "Cleanup typeobject.c code" — **the blame origin of three pass-2
findings plus this one**. Fix the record's framing: not a regression, a fix that never landed.

### P2-F21 (H-2) · `_Py_dict_lookup:1385` — P2-F6's unswept sibling, same lock, same file
**Source:** git-history-analyzer. **The only lock-order inversion found tree-wide.**
`compare_unicode_generic:1168` runs `PyObject_RichCompareBool` under `LOCK_KEYS`.
**Why it was missed:** the prohibiting comment (`bef570622263`, 2026-06-25) was written **26 months
after** the violating code (`d5df25268b03`, 2024-04-25) and was never applied backwards.

### P2-F22..F24 (H-3/H-4/H-5) · siblings outside this slice
- **H-3** tracemalloc `TABLES_LOCK`.
- **H-4** `HEAD_LOCK` — **gh-84139 was closed `not_planned` explicitly for lack of a reproducer.** The
  ask there is a **repro**, not a report. File accordingly.
- **H-5** four unguarded `Objects/object.c` siblings whose twin is `#ifdef Py_DEBUG`-only.

## ⚠ LATE FINDING — and it corrects one of our own guarded-twin citations

### P2-F25 (PENDING a GIL control) · `_PyType_GetSubclasses:793` — cursor invalidation, cross-thread
**Source:** tsan-stress-generator, Part-3 scenarios. Confirmed by **two independent scenarios**.
`T.__subclasses__()` racing `X.__bases__ = (...)`: the reader walks `tp_subclasses` with a **live
`PyDict_Next` cursor**, against two writers — `init_tp_subclasses:702` **replacing** the dict and
`delitem_common:2987` **deleting** from it.

**⚠ THIS SITE WAS CITED IN THIS RUN AS A GUARDED TWIN OF P2-F9.** The gil-discipline agent named
`_PyType_GetSubclasses:788-789` as one of the two correctly-handled siblings, because it carries an
explicit comment justifying its borrowed reference. **That comment justifies it against RE-ENTRANCY, not
against CONCURRENT MUTATION.** Under the second threat model the "twin" is itself a defect.
**Lesson for the informed-explore method:** a guarded twin is only a twin *for the threat model its
comment addresses*. Check which one before citing it as the fix.

**Also note:** `:793` is the same line the refcount auditor showed the baseline had mis-joined
(recorded as CPY-0083 at its *load* line 783 — see D-12). Third time this line has surfaced.

**BEFORE IT BECOMES A RECORD:** run the GIL control, and check gh-151377's coverage (it names a
cross-thread race on a *different* walker).

## CONSIDER

### P2-C7 · `_PyCStackRef` migration incomplete — 9 candidates remain, 4 net new
**Source:** git-history-analyzer, deliverable 4.
14 pinning sites against 201 candidate accesses; 80 functions triaged. `tp_dict`'s structural immunity
clears **31 at a stroke** — upgrading `RUN_CONTEXT.md`'s prediction to a verdict.
**⚠ Filing note:** the agreed **P2-F3 fix direction does not, on its own, fix the third defect on that
same `RichCompareBool`.** Do not present it as closing all three.
**Contrast — the `lookup_tp_*` migration IS complete:** 79/84 (94.0%), zero wrong-for-a-static-builtin
sites; gh-105020 **deliberately** reverted the `tp_bases`/`tp_mro` indirection, so those pass-throughs
are by design, not stragglers.

### P2-C6 · `init_static_type` failure path leaves `_Py_TPFLAGS_STATIC_BUILTIN` set
**Source:** uninitialized-dealloc-auditor.
The failure path clears the managed-static index (`tp_subclasses = NULL`) but leaves the flag set, so
every later `lookup_tp_dict` / `_PyStaticType_GetState` computes `index = SIZE_MAX`.
**Guarded twin:** `fini_static_type:6923`.
**Reachability currently closed** — both in-tree callers fail fatally inside interpreter init. Recorded
so the next reader does not re-derive it.

### P2-C5 · gh-148450 sibling `_PyType_SetFlags:6453` — invariant break reproduced, harm latent
**Source:** error-path-analyzer. The history preflight's §4.3 lead, decided.
**Reproduced single-threaded:** `tp_flags` changed with version tag 131160 still live. But the harm is
**latent** — the tier-2 optimizer does not constant-fold `_MATCH_MAPPING`. Hence CONSIDER, not FIX.
The sharper half is the **FT exposure**: no `TYPE_LOCK`, no `type_lock_prevent_release()`.

### P2-C4 · `type_set_abstractmethods:1745` omits `type_lock_prevent_release()` — two-line fix
**Source:** gil-discipline-checker, traced end-to-end.
`stop_the_world` (`Python/pystate.c:2441`) takes two mutexes that park **with detach**, and detach runs
`_PyCriticalSection_SuspendAll`, **dropping `TYPE_LOCK`**. So state protected by the type lock is
unprotected across the window even though the acquire/release pair is balanced.
**Guarded twin:** `_PyType_SetFlagsRecursive:6521`, whose comment states exactly why the call is needed.
**`PyType_Freeze` also omits it but is saved** by invalidating *after* the flag write —
`type_set_abstractmethods` is the **only** site that both omits the call and invalidates first.

### P2-C3 · `get_base_by_token_recursive:6002` — overturns pass 1's ACCEPTABLE
**Source:** recursion-guard-auditor. The one NET NEW candidate from the baseline that survived.
Pass 1 ruled it ACCEPTABLE: *"bound 0 for any Python-constructible type"*, because the descent only runs
on the `tp_mro == NULL` branch. **That precondition is not impossible — it is advertised.**
`Modules/_testcapi/heaptype.c:482-488` documents and exercises it, and `type_clear`
(`Objects/typeobject.c:7276`) manufactures it on **every GC'd heap type**, while its own 25-line comment
*deliberately* preserves `tp_bases`.
**Reproduced:** exit 139 on debug @1500/100 KB and release @3000/100 KB — **debug crashes earlier**, so
no CPY-0049-style debug-hiding — plus ASan `stack-overflow` with uniform `typeobject.c:6002` frames.
**Held at CONSIDER** only because the non-`_testcapi` GC route was not driven end to end.

### P2-C1 · `managed_static_type_state_get:294` — off-by-one bound test
**Source:** null-safety-scanner. Routed to memory-pattern-analyzer.
Uses `index > _Py_MAX_MANAGED_STATIC_EXT_TYPES` where **every sibling bound test in the region uses
strict `<`**. At `index == 10` it returns `&for_extensions.initialized[10]` — one past the end, landing
on `type_version_cache[]`, so `state->tp_dict` yields a borrowed `PyTypeObject *`.
**No Python route found**, hence CONSIDER not FIX. The guarded twins are the siblings, in the same region.

## POLICY — for maintainers, not bugs

### P2-P1 · `PyType_FromSpec` family soft-deprecated in 3.16; CPython has migrated 0 of its own 173 sites
**Source:** api-deprecation-tracker.
Four `.. soft-deprecated:: next` directives at `Doc/c-api/type.rst:827/858/884/909`; replacement
`PyType_FromSlots` available since 3.15. Internal adoption is three lines of
`Modules/_testlimitedcapi/slots.c`. **Two of the four are stable ABI (3.2/3.3)**, so the compat arm of
`type_from_slots_or_spec` is permanent code — the migration burden is CPython's own choice, forever.

### P2-P2 · the new slot API's deprecations are enforced only against the new API
**Source:** api-deprecation-tracker. **Reproduced on `debug-gil-nojit`: 8/8 new-API paths warn, 0/8
legacy paths warn.**
`Python/slots.toml` classifies 81 of 111 slots `nulls = 'deprecated'` / `duplicates = 'deprecated'`, but
`Python/slots.c:350` and `:383` gate the `DeprecationWarning` on
`states[0].slot_struct_kind == _PySlot_KIND_SLOT` — true only for `PyType_FromSlots`. Legacy spec
callers take the `else` arm, which is `MSG(...)`, compiled out at `Python/slots.c:13-18`.
Deliberate and tested, but `Lib/test/test_capi/test_slots.py:168` uses `contextlib.nullcontext()` for
the legacy arm — exercised, not pinned. Undocumented, and the two sources imply opposite intents:
`Doc/c-api/slots.rst:38` says "no plans to remove"; `Python/slots.toml:19-20` says "Don't use for new slots".

### P2-P3 · `Py_TPFLAGS_HAVE_FINALIZE` on a production type spec — outside this slice
`Modules/posixmodule.c:17075` — no-op since 3.8, pending removal, and *not* stable ABI, so genuinely
removable. CONSIDER. (Cited line moved to `:17078` in the build-matrix ref; unchanged.)

## Corrections to pass 1

### `_PyBufferWrapper_Type` is NOT instantiable
Pass 1 recorded it as instantiable. `type_ready_set_new:9428-9437` auto-applies
`Py_TPFLAGS_DISALLOW_INSTANTIATION` to a *static* type with `tp_new == 0` whose base is `object`.
Verified: `__flags__` is `0x5182`, and both `BW()` and `BW.__new__(BW)` raise
`TypeError: cannot create '_buffer_wrapper' instances`.

## Bounded negatives (state these; a cold run never does)

- **`super` field enumeration is complete** — 23 reads walked; only the already-recorded `:12793` /
  `:12797` are unguarded.
- `__class__` assignment rejects deletion and enforces layout compatibility.
- Managed static types have no Python surface.
- `__slots__` members *are* deletable but have no C reader in this file (probes 9–12 clean).
- **PyErr_Clear destructor family: 0 of 11**, earned — including 5 family members pass 1's
  slot-oriented list never named (`managed_static_type_index_clear`, `managed_static_type_state_clear`,
  `type_cache_clear`, `super_dealloc`, `super_traverse`).
- **PyErr_Clear success-path (gh-146102) class: 0 of 11**, every clear dominated by a failure test it caused.
- **PEP 7 bug-adjacent shapes: 0 confirmed** across 5 shapes, 149 braceless bodies walked with
  paren-balance tracking.
