# pyerr-clear-auditor — slice `obj-typeobject`

**Target:** `/home/danzin/projects/cpython/Objects/typeobject.c` @ `4f3be1b5777` (3.16.0a0), 13,068 lines
**Builds used:** `debug-gil-nojit`, `release-gil-nojit` @ `a1d580430c8`.
`Objects/typeobject.c`, `Python/errors.c`, `Python/gc.c` and `Modules/_testcapi/watchers.c` are **byte-identical**
between the target ref and the build ref (`git diff --stat 4f3be1b5777 a1d580430c8 --` is empty for all four), so
every citation and every repro below applies unmodified to the target.

**Deconfliction.** `error-path-analyzer` owns and has settled `type_repr:2405`, `object_repr:7490`,
`find_name_in_mro:6149/:6158/:6183`, `get_subclasses_key:9748`, and `has_dunder_getitem:11090` /
`slot_tp_iter:11108`. Those are confirmed in one line each below and not re-triaged. This report's own
contributions are: the **independent widened census** (§1), the **destructor-family sweep** (§2), the
**`slot_tp_finalize:11243` strengthening with its guarded twin** (§3), the **fetch/restore balance** (§4), the
**construction surface** (§5), and the **toolkit recall gap** (§8).

---

## 1. Census — verified independently, and widened

### 1a. The `PyErr_Clear` count: **11 confirmed**

`grep -n PyErr_Clear Objects/typeobject.c` → **11**. This matches `error-path-analyzer`'s count.
I checked the four ways that number could be an undercount; all four are negative:

| could hide a clear | check | result |
|---|---|---|
| private tstate alias | `grep -c _PyErr_Clear` | **0** — this file uses only the public spelling |
| in-file or header macro expanding to a clear | `grep "define.*PyErr_Clear\|define.*PyErr_Fetch"` over `typeobject.c`, `pycore_pyerrors.h`, `cpython/pyerrors.h`, `pyerrors.h` | **0 definitions** |
| the `SLOT0`/`SLOT1`/`SLOT1BIN` statement macros (which *do* hide the region from tree-sitter, see §8) | read the macro bodies | **no error handling at all** — each is a bare `return vectorcall_method(...)` |
| exception discarded by hand after a fetch | `grep "PyErr_SetRaisedException(NULL)\|PyErr_Restore(NULL\|Py_XDECREF(exc)\|Py_DECREF(exc)"` | **0** |

So **11 is the correct and complete `PyErr_Clear` census.**

### 1b. But the *exception-loss* census is 14, not 11

A `PyErr_Clear` grep is not the same as an accounting of where this file can destroy an exception. Three further
sites destroy a pending exception through APIs that contain no `PyErr_Clear` at all, and none would appear in an
11-site census:

| line | function | mechanism | verdict |
|---|---|---|---|
| 1223 | `_PyType_Modified` | `PyErr_FormatUnraisable` — an *implicit* fetch-without-restore (`format_unraisable_v` calls `_PyErr_Fetch` at `Python/errors.c:1671` and never restores) | ACCEPTABLE (§7) |
| 6998 | `type_dealloc` | same, in the type-watcher notification loop | ACCEPTABLE (§7) |
| **11243** | **`slot_tp_finalize`** | **`_PyErr_SetRaisedException` `Py_XDECREF`s whatever is pending — a restore *over* a live exception** | **CONSIDER (§3)** |

`_PyErr_SetRaisedException` (`Python/errors.c`) is unconditional:

```c
_PyErr_SetRaisedException(PyThreadState *tstate, PyObject *exc)
{
    PyObject *old_exc = tstate->current_exception;
    tstate->current_exception = exc;
    Py_XDECREF(old_exc);
}
```

A "restore" therefore destroys a pending exception exactly as effectively as a clear, silently and with no
diagnostic. That is the gap in an 11-site census, and §3 is the site where it bites.

### 1c. The 11 clears

| # | line | function | shape | verdict |
|---|------|----------|-------|---------|
| 1 | 2405 | `type_repr` | clears `type_module()`'s failure, unnarrowed | *error-path-analyzer: FIX, reproduced* |
| 2 | 3023 | `lookup_method_ex` | narrowed `PyErr_ExceptionMatches(PyExc_AttributeError)` | ACCEPTABLE |
| 3 | 6149 | `find_name_in_mro` | own `_PyObject_HashDictKey` failure | *error-path-analyzer: FIX, reproduced* |
| 4 | 6158 | `find_name_in_mro` | clears `PyType_Ready`'s exception | *error-path-analyzer: CONSIDER* — see §6 |
| 5 | 6183 | `find_name_in_mro` | own `DKIX_ERROR` from the MRO walk | *error-path-analyzer: FIX, reproduced* |
| 6 | 7490 | `object_repr` | same shape as #1 | *error-path-analyzer: FIX, reproduced* |
| 7 | 8619 | `type_add_method` | **guarded twin** — narrows to `AttributeError` first | ACCEPTABLE |
| 8 | 9748 | `get_subclasses_key` | early-return guard clause + manual fallback | ACCEPTABLE — named verbatim in the FP taxonomy |
| 9 | 9786 | `remove_subclass` | own `PyDict_DelItem` KeyError; caller brackets it | ACCEPTABLE (§2) |
| 10 | 10931 | `slot_tp_hash` | statically-known `PyLong_Type.tp_hash` after an OverflowError-only conversion | ACCEPTABLE — **scanner-invisible** |
| 11 | 11032 | `slot_tp_getattr_hook` | narrowed to `AttributeError` | ACCEPTABLE — **scanner-invisible** |

**This agent's verdicts: 14 exception-loss sites examined · FIX 0 · CONSIDER 1 (new, §3) · POLICY 3 · ACCEPTABLE 10.**
(Sites 1, 3, 5, 6 are counted under `error-path-analyzer`, not here.)

Narrowing rate: 3 of 11 clears carry an explicit `PyErr_ExceptionMatches`; 3 more are covered by a caller-side
`assert(!PyErr_Occurred())`.

---

## 2. Destructor family — swept, and clean apart from §3

Destructor-family functions in this file: **15**. `PyErr_Clear` calls lexically inside one: **0**. That zero is
earned; here is the reachability work behind it.

Enumerated: `type_dealloc` (:6978), `type_dealloc_common` (:6849), `type_traverse` (:7198), `type_clear` (:7229),
`subtype_dealloc` (:2719), `subtype_clear` (:2680), `subtype_traverse` (:2607), `object_dealloc` (:7476),
`slot_tp_finalize` (:11217), `bufferwrapper_dealloc` (:11264), `bufferwrapper_traverse` (:11255),
`super_dealloc` (:12556), `super_traverse` (:12973).

**Clears reachable from a destructor** (not lexically inside one): two, both via
`type_dealloc` → `type_dealloc_common` → `remove_all_subclasses` → `remove_subclass` → `get_subclasses_key`,
i.e. sites 9748 and 9786. **Both are already bracketed** — `type_dealloc_common` is the in-file guarded twin:

```c
static void
type_dealloc_common(PyTypeObject *type)
{
    PyObject *bases = lookup_tp_bases(type);
    if (bases != NULL) {
        PyObject *exc = PyErr_GetRaisedException();
        remove_all_subclasses(type, bases);
        PyErr_SetRaisedException(exc);
    }
}
```
— `:6848-6857`. Exactly the OOM-0039 / gh-152083 fix shape, applied prophylactically. `remove_all_subclasses`
additionally documents the hazard inline (`// remove_subclass() can clear the current exception`, `:9799`) and
brackets its body with `assert(!PyErr_Occurred())` at both ends (`:9800`, `:9808`).

**`subtype_dealloc`** — the brief's highest-value target, since it runs arbitrary user `__del__` and finalizers —
contains **no** `PyErr_Clear`, no fetch, and no fallible call whose result it drops. It delegates finalization to
`PyObject_CallFinalizerFromDealloc` and weakref clearing to `PyObject_ClearWeakRefs`, both of which own their
exception state in `Objects/`. Its resurrection checks (`Py_REFCNT(self) > 0` after `tp_del`) are refcount logic,
not exception logic. **Clean.**

`type_clear`, `type_traverse`, `subtype_clear`, `subtype_traverse`, `super_dealloc`, `super_traverse`,
`object_dealloc`, `bufferwrapper_*`: no exception-state manipulation of any kind.

The one destructor-family defect is `slot_tp_finalize`, next.

---

## 3. **[CONSIDER] `slot_tp_finalize:11243` restores over a live exception — a `__del__` *lookup* failure is destroyed with zero diagnostics**

`error-path-analyzer` flagged this site. Rather than restate it, this section supplies three things it did not
have: **the exact in-file guarded twin, a minimal-pair differential that isolates the defect to one branch, and
the 1-of-3 framing that makes the fix obvious.**

**What.** `slot_tp_finalize` correctly saves the ambient exception at `:11221` and restores it at `:11243`. But
between them, `lookup_maybe_method(self, &_Py_ID(__del__), &cref.ref)` at `:11227` can return **-1 with an
exception pending**, and that return value is tested only as `if (unbound >= 0)`. On the `-1` path the body is
skipped and control falls straight to `:11243`, where `_PyErr_SetRaisedException` `Py_XDECREF`s the pending
exception on its way to reinstalling the saved one.

`lookup_maybe_method` → `lookup_method_ex(..., raise_attribute_error=0)` clears only `AttributeError` (`:3020-3024`).
Anything else — `KeyboardInterrupt`, `MemoryError`, `SystemExit` — survives to `:11243` and is destroyed there.

**Trigger.** `__del__` bound to a non-`METHOD_DESCRIPTOR` descriptor whose `__get__` raises. A plain
`def __del__` is safe: functions carry `Py_TPFLAGS_METHOD_DESCRIPTOR`, so `lookup_method_ex` returns 1 at
`:3010` before ever reaching `tp_descr_get`. That narrowness is why CONSIDER, not FIX, is the right severity.

**The asymmetry is five lines wide.** A failure of the `__del__` *call* is reported via
`PyErr_FormatUnraisable` at `:11232`. A failure of the `__del__` *lookup* gets nothing.

**Guarded twin #1 — the same operation, same file, handled correctly.** `type_new_set_names:12288-12292` does the
identical thing (a special-method lookup that can fail through a user `__get__`) and tests for it:

```c
PyObject *set_name = _PyObject_LookupSpecial(value, &_Py_ID(__set_name__));
if (set_name == NULL) {
    if (PyErr_Occurred()) {
        goto error;          /* <-- the check slot_tp_finalize:11227 is missing */
    }
    continue;
}
```

**Minimal-pair differential** (`<scratchpad>/probe6.py`), identical trigger, both builds agree:

```
A set_name-lookup : PROPAGATED KeyboardInterrupt(ctrl-c)  unraisable=0
B del-lookup      : *** SWALLOWED ***                     unraisable=0
A set_name-lookup : PROPAGATED MemoryError(oom)           unraisable=0
B del-lookup      : *** SWALLOWED ***                     unraisable=0
```

**Control differential** (`<scratchpad>/probe4.py`) isolating it to the lookup branch — same exception, same
class, only the failure point moves:

```
KeyboardInterrupt  unraisable_reports=0 []                                  <- __del__ = raising descriptor
MemoryError        unraisable_reports=0 []
SystemExit         unraisable_reports=0 []
body-raise CONTROL unraisable_reports=1 [('KeyboardInterrupt','from-body')] <- def __del__: raise
outer survived: OUTER                                                       <- the save/restore itself is correct
```

Zero unraisable reports on the lookup path, one on the call path. The save/restore bracket is doing its job — the
*outer* exception is correctly preserved — so the defect is purely the undiagnosed destruction of the *inner* one.

**Guarded twin #2 — 1 of 3 restore sites lacks the guard.** This file has exactly three
`(Get|Set)RaisedException` pairs (3 gets, 3 sets, verified by count; `PyErr_Fetch`/`PyErr_Restore` appear 0 times):

| restore | function | what proves the slot is empty at restore time |
|---|---|---|
| `:6855` | `type_dealloc_common` | `remove_all_subclasses`'s own `assert(!PyErr_Occurred())` at `:9800` and `:9808` |
| `:11472` | `releasebuffer_call_python` | **`assert(!PyErr_Occurred());` at `:11470`, immediately before the restore** |
| **`:11243`** | **`slot_tp_finalize`** | **nothing** |

`releasebuffer_call_python` is the same idiom (save at the top of a `void` teardown function, restore at the
bottom) and it *asserts* the precondition that `slot_tp_finalize` merely assumes. It also states the reasoning
out loud at `:11418-11421`: `// bf_releasebuffer may be called while an exception is already active. ... we
simply stash away the active exception and restore it after the call to Python returns.`

**Fix.** Two lines, matching what the file already does elsewhere — report instead of discarding, and assert the
invariant so the next regression is caught:

```c
    int unbound = lookup_maybe_method(self, &_Py_ID(__del__), &cref.ref);
    if (unbound < 0) {
        PyErr_FormatUnraisable("Exception ignored while "
                               "looking up deallocator of %R", self);
    }
    else {
        ...existing body...
    }

    _PyThreadState_PopCStackRef(tstate, &cref);
    assert(!PyErr_Occurred());          /* as at :11470 */
    _PyErr_SetRaisedException(tstate, exc);
```

---

## 4. Fetch/restore and chaining balance — **3 pairs, all numerically balanced**

`PyErr_Fetch` / `PyErr_Restore`: **0 occurrences**. `_PyErr_ChainExceptions*`: **0 occurrences**.
`(_)PyErr_GetRaisedException`: 3. `(_)PyErr_SetRaisedException`: 3. No unbalanced fetch exists.

| pair | function | balanced? |
|---|---|---|
| `:6853` / `:6855` | `type_dealloc_common` | yes — straight-line, no branch or early return between |
| `:11221` / `:11243` | `slot_tp_finalize` | yes numerically — but see §3, the restore lands on a live exception |
| `:11423` / `:11472` | `releasebuffer_call_python` | yes — both `goto end` targets (`:11433`, `:11443`) land above the restore |

The §3 finding is precisely the case that a balance count cannot detect: the pair is balanced, and the exception
is destroyed anyway.

---

## 5. Construction surface — `type_new_set_names` / `type_new_init_subclass` — **correct on every path**

Checked against the brief's concern about swallowed or mis-chained `__set_name__` exceptions, and against
gh-77757 (`55c99d97e14`, PR #103402), which replaced `_PyErr_FormatFromCause(PyExc_RuntimeError, ...)` with
`_PyErr_FormatNote(...)` at this site. (gh-112453 is a later issue in the same area; the note-based behaviour
below is what ships at the target ref.)

`type_new_set_names` (`:12276-12317`) has four exit paths; all four are correct:

1. `PyDict_Copy` fails → `return -1`, exception propagates untouched.
2. `_PyObject_LookupSpecial` returns NULL **with** an exception → `if (PyErr_Occurred()) goto error;`
   propagates it. This is guarded twin #1 from §3.
3. `_PyObject_LookupSpecial` returns NULL **without** one (no `__set_name__`; the helper clears `AttributeError`
   itself) → `continue`. Correctly distinguished from case 2.
4. `__set_name__` raises → `_PyErr_FormatNote(...)` **annotates** the live user exception rather than replacing
   it, then `goto error`.

`_PyErr_FormatNote` is itself safe on its own failure path: if `PyUnicode_FromFormatV` or `_PyException_AddNote`
fails, it takes `error: _PyErr_ChainExceptions1(exc)`, which makes the original the `__context__` of the new
failure rather than dropping it.

`type_new_init_subclass` (`:12322-12344`) propagates all three failure points (`super` construction, the
`__init_subclass__` attribute fetch, the call) by plain `return -1`. Nothing swallowed, nothing wrapped.

Verified end-to-end (`<scratchpad>/probe5.py`) — user exception type, value and note all preserved, `__cause__`
correctly `None`:

```
('set_name',      'KeyboardInterrupt', 'ctrl-c', ["Error calling __set_name__ on 'D' instance 'x' in 'C'"], None)
('set_name',      'MemoryError',       'oom',    ["Error calling __set_name__ on 'D' instance 'x' in 'C'"], None)
('set_name',      'SystemExit',        '7',      ["Error calling __set_name__ on 'D' instance 'x' in 'C'"], None)
('init_subclass', 'KeyboardInterrupt', 'ctrl-c', None, None)
('init_subclass', 'MemoryError',       'oom',    None, None)
('init_subclass', 'SystemExit',        '7',      None, None)
```

**Clean.** No finding.

---

## 6. `find_name_in_mro:6158` (the scan_pyerr_clear candidate) — one line, as agreed

Confirmed as `error-path-analyzer` has it. Adding only the evidence that bears on severity: the clear is covered
by a *declared* contract (`It never set an exception.`, `:6142`) **and** by a caller-side invariant — both call
sites are preceded by the identical comment and assertion,

```c
/* We may end up clearing live exceptions below, so make sure it's ours. */
assert(!PyErr_Occurred());
```
— `:6350-6351` (`_PyType_LookupRefAndVersion`) and `:11936-11937` (`update_one_slot`). So the clear provably
cannot destroy a *caller's* exception; the residual is only that `PyType_Ready`'s own error is flattened into
"not found" (`:6368-6371`) or a skipped slot (`:11943-11947`). On this agent's rubric that is **POLICY**.

---

## 7. Investigated and cleared: the two unbracketed unraisable reporters

Recorded because it is the strongest-*looking* non-finding in the file, and because the safety argument is
**non-local** — a future refactor could make it live.

`PyErr_FormatUnraisable` **consumes** the pending exception: `format_unraisable_v` (`Python/errors.c:1662`) calls
`_PyErr_Fetch` at `:1671` and never restores. Two of its five uses here are unbracketed — `:6998` in
**`type_dealloc`**'s type-watcher loop (added 2026-05-05 by `f6d16a0d708`, gh-149216 / GH-149236) and `:1223` in
`_PyType_Modified`'s. The asymmetry is striking: `type_dealloc` runs an arbitrary C watcher callback at `:6997`
unprotected, then ~50 lines later calls `type_dealloc_common`, which *does* save and restore.

**Both are ACCEPTABLE — provably unreachable with a pending exception**, for two independent reasons:

1. **Heap types are only ever freed by the GC.** `tp_mro` holds a strong self-reference, so the refcount never
   reaches zero by DECREF. Verified: a `weakref` to `type('T',(),{})` survives `del` and dies only on `gc.collect()`.
2. **The GC provably runs with an empty exception slot.** `_PyObject_GC_Link` schedules a collection only when
   `!_PyErr_Occurred(tstate)` (`Python/gc.c:1989` — a *runtime* condition present in release builds, not an
   assert), and `gc_collect_main` asserts `!_PyErr_Occurred(tstate)` on entry (`Python/gc.c:1436`).

The one non-GC route to `type_dealloc` — `type_new_impl`'s error paths, which `Py_DECREF` a half-built type *with
an exception pending*, before `tp_mro` creates the cycle — cannot reach the loop, because `if (type->tp_watched)`
(`:6986`) is false for a type nobody has had the chance to watch yet.

**Differential** (`<scratchpad>/probe3.py`): `_testcapi.add_type_watcher(1)` installs a callback that raises
`RuntimeError("boom!")` and returns -1; a watched heap type is left as garbage; `gc.set_threshold(1,1,1)`
maximizes collection frequency; the value stack is unwound with a pending `ZeroDivisionError` raised
mid-expression. 3000 iterations, both builds:

```
debug-gil-nojit:   survived=3000 clobbered=0 lost=0 unraisable_events=6000 contexts={'NoneType'}
release-gil-nojit: survived=3000 clobbered=0 lost=0 unraisable_events=6000 contexts={'NoneType'}
```

6000 watcher failures fired and `__context__` was `NoneType` every time — implicit chaining never occurred, so
nothing was ever pending. All 3000 outer exceptions reached their handler.

**Hardening note, not a bug:** `:6998`'s correctness rests entirely on a global GC invariant rather than a local
guard, unlike its three file-siblings. If a heap type ever becomes reachable by `type_dealloc` outside
`gc_collect_main`, this becomes a live CPY-0009. A two-line `PyErr_GetRaisedException()` /
`PyErr_SetRaisedException()` bracket around the loop would make it locally safe and match the file's convention.

---

## 8. Toolkit feedback

**Precision: 1/1 mechanically correct, 0/1 actionable.** The single `pyerr_clear_unfiltered_after_python_call`
finding fired on a genuine unfiltered clear after a Python-reaching call; it is downgraded only by evidence the
scanner cannot see. No rule change proposed — `find_name_in_mro` is already named in the agent spec's
documented-contract carve-out.

**Recall gap — the important result. `extract_functions` loses 686 lines of `Objects/typeobject.c` (5.3%), and
the lost region is the worst possible one.** Two of eleven clears (10931, 11032) were invisible to the scanner:
`total_pyerr_clear_calls: 9` against a true count of 11. Measuring gaps between consecutively parsed functions:

```
GAP  7283.. 7378  ( 95 lines)  type_is_gc          -> excess_args
GAP  8535.. 8600  ( 65 lines)  object___dir___impl -> type_add_method
GAP 10852..11041  (189 lines)  slot_nb_index       -> slot_tp_setattro
GAP 11521..11794  (273 lines)  slot_am_anext       -> slotptr
GAP 11842..11906  ( 64 lines)  has_slotdef         -> update_one_slot
```

Cause: the `SLOT0` / `SLOT1` / `SLOT1BIN` **statement-macro invocations** (e.g. `SLOT0(slot_tp_str, __str__)` at
`:10900`), which tree-sitter reads as declarations and which desynchronize the surrounding function definitions.

Why this matters beyond a count: those gaps contain `slot_tp_hash`, `slot_tp_call`, `slot_tp_getattro`,
`slot_tp_getattr_hook` and the async/buffer slots — **precisely the functions that dispatch into arbitrary user
Python**, which is the population rule 3 exists to police. The blind spot is anti-correlated with where the bugs
are. It is also silent: nothing in the envelope says "9 of 11". Both hidden clears happened to be fine here, so it
cost no finding on this slice — but it would not have announced itself if it had.

Two proposals:

1. **Emit a reconciliation counter.** Regex-count `PyErr_Clear` / `_PyErr_Clear` over the raw bytes and report
   `total_pyerr_clear_calls_unattributed` beside `total_pyerr_clear_calls`. A non-zero value is the canary that a
   clean result is partly a parse artifact. This generalizes to every scanner that attributes sites to functions,
   and mechanizes the agent spec's own Phase 4 step 2 instead of leaving it as manual work.
2. **Recover the region.** Either preprocess away known statement-macro invocations before parsing, or attribute
   an unmatched site to the nearest preceding `function_definition` and mark it `attribution: "approximate"`.
   Given the `Modules/multidict` precedent (`extract_functions` skipping `preproc_ifdef`, confirmed by 5 agents),
   this is the same defect family in `tree_sitter_utils.py` and should be fixed once, upstream, for all siblings.

**A rule the scanner does not have, and should.** §3 is invisible to every current rule: the pair is *balanced*,
so a fetch/restore-balance check passes, and there is no `PyErr_Clear` to match. The detectable shape is
**"`(_)PyErr_SetRaisedException` reached on a path where a fallible call's failure return was not tested"** —
i.e. a restore that can land on a live exception. In this file it is 1-of-3 restore sites, and the other two both
carry the `assert(!PyErr_Occurred())` that would be the natural fix, which makes it a cheap, high-precision rule:
flag a restore whose function contains a fallible call whose error branch does not report or return, and that is
not immediately preceded by `assert(!PyErr_Occurred())`.

**Suppression working as designed:** the taxonomy's early-return-guard-clause class correctly kept 9748 out of
this scanner's output (it names that exact line), and the `PyErr_ExceptionMatches` gate correctly suppressed 3023,
8619 and 11032. Those four suppressions are why this slice cost 14 reads instead of 40.
