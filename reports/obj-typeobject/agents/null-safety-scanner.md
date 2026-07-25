# NULL Safety Analysis — `Objects/typeobject.c` (informed-explore, slice `obj-typeobject`)

**Target:** `/home/danzin/projects/cpython/Objects/typeobject.c` @ `4f3be1b5777` (3.16.0a0), 13,068 lines.
**Builds:** `~/projects/python_build_matrix/builds/*` @ `a1d580430c8`.
**File-identity check:** `git diff a1d580430c8 4f3be1b5777 -- Objects/typeobject.c Python/slots.c Include/internal/pycore_slots_generated.h` → **empty**. Every line number below is valid on both the target ref and the matrix builds.

---

## 1. THE DENOMINATOR — was the zero earned?

`scan_null_checks.sample.json`: `files_analyzed 1`, `functions_analyzed 423`, `findings []`, all three summary counters `0`.

I re-ran the scanner's own machinery with per-stage instrumentation
(`scratchpad/denom.py`, `scratchpad/stages.py`, importing `scan_null_checks` directly).

### 1a. Rule coverage — how much of the file the rule can even see

| measure | count |
|---|---|
| assignment-from-call sites in the file (`lval = SomeCall(`) | **760** |
| …resolved by the scanner's closed API enum (`ALLOC_APIS ∪ PYOBJ_APIS`) | **49 (6.4 %)** |
| …invisible to the enum | **711 (93.6 %)** |

The 16 enum members that actually occur here are `PyDict_New`(10), `PyList_New`(6),
`PyUnicode_FromFormat`(5), `PyUnicode_FromString`(5), `PyObject_GetAttr`(5),
`PyMem_Malloc`(4), `PyTuple_New`(3), `PyMem_New`(2), `PySequence_Tuple`(2), and eight singletons.

The 711 unseen sites are not noise. The top fallible constructors this file actually
uses are absent from the enum: `PyDict_SetItem`(14), `PyTuple_Pack`(8),
`PyDict_GetItemWithError`(6), `PyType_GetDict`(4), `_PyType_LookupRef`(4),
`_PyObject_CallNoArgs`(4), `PyObject_Vectorcall`(4), `PyStaticMethod_New`(2),
`PyDescr_NewGetSet`(2), `PyCFunction_NewEx`(2), `PyLong_FromVoidPtr`(2),
`PyObject_GetIter`(2), `PyDict_Copy`(2), `PyUnicode_AsUTF8`(3), plus ~120 in-file
static helpers that return `NULL` on failure (`lookup_tp_dict`, `lookup_tp_bases`,
`lookup_tp_mro`, `_PyStaticType_GetState`, `find_best_base`, `solid_base`,
`type_module`, `type_qualname`, `slotptr`, `vectorcall_method`, `lookup_maybe_method`, …).

### 1b. Pipeline stages on the 49 sites it *did* see

| stage | count |
|---|---|
| 0 — matched the enum assignment regex | 49 |
| 1 — dropped: assignment is a control-condition | 0 |
| 2 — dropped: no deref of the result inside the 12-line window | 22 |
| 3 — reached "has a deref in window" | 27 |
| 4 — dropped: deref not dominated by the assignment | 15 |
| 6 — dropped: NULL check precedes the deref | **12** |
| **findings** | **0** |

### 1c. Rule 3 (`decref_of_nulled_outparam`) — a true structural zero

`discover_outparam_wrappers()` found **0** local wrappers, and the file contains
**0** call sites of any `NULLING_OUTPARAM_API` (`_PyTuple_Resize`, `_PyBytes_Resize`,
`PyUnicode_Resize`, `_PyUnicode_Resize`). **Denominator = 0.** This rule was not
exercised at all here; its zero carries no information whatsoever.

### VERDICT

- **`decref_of_nulled_outparam`: the zero is structural silence.** Denominator zero. Do not
  count it as a clean bill for this file.
- **`unchecked_alloc` / `deref_before_check`: the zero is *weakly* earned, over 6.4 % of the
  surface.** The 12 "checked-first" drops prove the check-recognition logic runs and works,
  so within its 49-site window the rule is behaving. But a rule that resolves 49 of 760
  fallible-assignment sites and then reports zero is reporting on a sample, not on the file.
  Stating "NULL safety: clean" from this output would be the exact v0.8 failure mode.

**Confirmation that the silence is a coverage artifact, not safety:** re-running the *identical*
pipeline (same window, same check regexes, same dominance test) with the API set widened to
`enum ∪ in-file static functions ∪ ~60 common fallible APIs` (`scratchpad/widened.py`) moves the
denominator from 49 → **351 candidates / 162 deref-in-window / 84 dominated**, and produces
**26 candidates** to triage — including three of the findings below. Same detector, different
alphabet.

---

## 2. Findings

### [FIX] `type_set_bases_unlocked` ignores `add_all_subclasses`'s failure — `Objects/typeobject.c:1966`

```c
1965        remove_all_subclasses(type, old_bases);
1966        res = add_all_subclasses(type, new_bases);      /* -1 + live exception, NEVER TESTED */
1967        if (update_all_slots(type) < 0) {               /* <-- guarded twin, one line below */
1968            goto bail;
1969        }
1970        /* Clear the VALID_VERSION flag of 'type' and all its subclasses. */
1971        _PyType_Modified_Unlocked(type);
```

**What.** `add_all_subclasses` (`typeobject.c:9727`) returns `int` and sets a live exception
when `add_subclass` fails (`PyLong_FromVoidPtr` / `PyWeakref_NewRef` / `init_tp_subclasses` /
`PyDict_SetItem`, `typeobject.c:9693-9722`). Line 1966 stores that `-1` in `res` and **takes no
branch on it**. Execution falls into `update_all_slots(type)` with a pending `MemoryError`, and
`update_one_slot`'s first statement is

```c
11937    /* We may end up clearing live exceptions below, so make sure it's ours. */
11938    assert(!PyErr_Occurred());
```

Worse, the `goto bail` rollback (which restores `old_bases` / `old_base`) is skipped, while
`remove_all_subclasses` at line 1965 has *already* unlinked the type from its old bases.

**Impact.**
- **Debug builds — SIGABRT.** `python: Objects/typeobject.c:11938: update_one_slot: Assertion '!PyErr_Occurred()' failed.` Reproduced on `debug-gil-nojit` and `debug-ft-nojit`.
- **Release builds — silent, permanent type-graph corruption.** The assignment is *committed*
  (`C.__bases__ == (B,)`) while `MemoryError` is raised to the caller, and `C` is in
  **neither** `A.__subclasses__()` nor `B.__subclasses__()`. `C` is now invisible to
  slot-update propagation from `B` forever — which is precisely the invariant
  `update_one_slot` exists to maintain. Reproduced on `release-gil-nojit` and `release-ft-nojit`.

**Differential (all four builds, `scratchpad/bases_probe2.py`).**

```
setup:  class A: pass / class B: pass / class C(A): pass
inject: _testcapi.set_nomemory(N, N+1)
code:   C.__bases__ = (B,)

N=1,2  (failure inside type_check_new_bases / PyList_New — return value IS checked)
       all builds: MemoryError, rollback correct, bases=(A,), C in A.__subclasses__()  ← CORRECT

N=4,5,6 (failure inside add_all_subclasses — return value NOT checked)
       debug-gil-nojit  rc=134  Assertion `!PyErr_Occurred()' failed, typeobject.c:11938
       debug-ft-nojit   rc=134  same
       release-gil-nojit rc=0   MemoryError, bases=(B,), C in B.__subclasses__()=False,
                                             C in A.__subclasses__()=False
       release-ft-nojit  rc=0   same
```

Full gdb chain (debug-gil-nojit):
`update_one_slot` (typeobject.c:11938) ← `update_slots_callback` (:12080) ←
`update_subclasses` (:12356) ← `update_slot` (:12124) ← `update_all_slots` ←
`type_set_bases_unlocked` (:1967) ← `PyObject_SetAttr`.

**Guarded twins.**
- `typeobject.c:1967` — the **immediately following line** tests its fallible call and `goto bail`s.
- `typeobject.c:1949` — `if (mro_hierarchy_for_complete_type(type, temp) < 0) goto undo;`.
- `typeobject.c:9400-9405` (`type_ready_add_subclasses`) — the *other* call site of the same helper
  family: `if (PyType_Check(b) && add_subclass((PyTypeObject *)b, type) < 0) return -1;`.

**Blame / prior art — an abandoned fix, not an unknown bug.**
- `git blame`: line 1966 is `104b9e0ccac4` (Benjamin Peterson, 2015-02-05); the *checked*
  neighbour at 1967-1969 is `fbbbc10055e0` (Neil Schemenauer, **2025-05-27**) — someone
  hardened the adjacent call and left this one.
- **bpo-38554 / python/cpython#16879** (ZackerySpytz, 2019) proposed exactly this fix:
  ```diff
  -        res = add_all_subclasses(type, new_bases);
  +        if (add_all_subclasses(type, new_bases) < 0) {
  +            goto undo;
  +        }
  ```
  The PR was **closed unmerged** on 2023-06-07T23:24:45Z, at the same timestamp as the
  bulk BPO-migration issue closure — i.e. auto-closed as stale, never applied. The defect
  is live on main.

**Fix.** Apply #16879: branch on `add_all_subclasses`'s result and `goto undo` (or `bail`) so the
`old_bases`/`old_base` restore runs and `update_all_slots` is never entered with a live exception.

---

### [FIX — cross-slice referral to `obj-dict`] `assert` dereferences an unchecked allocation, and the UB deletes the NULL check — `Objects/dictobject.c:4493`

Surfaced by the OOM sweep of *this* slice (the crash is reached from `typeobject.c:4900`,
`type_new_init`), but the defect is in `dictobject.c`. Recorded here, not claimed as an
in-slice finding.

```c
4485        if (mp->ma_used == 0) {
4486            /* The dict is empty; just return a new dict. */
4487            PyObject *d;
...
4492                d = dict_new_untracked(&PyDict_Type);
4493            }
4494            assert(!_PyObject_GC_IS_TRACKED(d));   /* <-- derefs d; d can be NULL */
4495            return d;
```

`dict_new_untracked` → `anydict_new_untracked` (`dictobject.c:5355`) returns `NULL` when
`_PyType_AllocNoTrack` (`typeobject.c:2512`) fails. `_PyObject_GC_IS_TRACKED(NULL)` reads the
GC pre-header of NULL. Because the caller dereferences `d` on *both* the NULL-return and the
success path after inlining, the optimizer concludes `d` is never NULL and **deletes the
`if (self == NULL) return NULL;` inside the inlined `anydict_new_untracked`**:

```
copy_lock_held_untracked+375: lea  PyDict_Type,%rdi
copy_lock_held_untracked+387: call _PyType_AllocNoTrack
copy_lock_held_untracked+392: xorps %xmm0,%xmm0                <-- no `test %rax,%rax`
=>              +395:         movups %xmm0,0x10(%rax)          <-- rax=0, si_addr=0x10
```

**Impact.** SIGSEGV on debug/`-Og` builds; `NDEBUG` builds are clean (the assert vanishes, the
NULL check survives, `NULL` propagates correctly). Reproduced on `debug-gil-nojit`, absent on
`release-gil-nojit` across all 200 sweep indices.

**Differential (`scratchpad/sweep1.py`, single-allocation failure `set_nomemory(n, n+1)`).**

| payload | debug-gil-nojit | release-gil-nojit |
|---|---|---|
| `type('X',(),{})` | **n=127 SIGSEGV** | no crash 1..200 |
| `type('X',(int,),{})` | **n=135 SIGSEGV** | — |
| `d={}; d.copy()` | **n=97 SIGSEGV** | — |
| `import copy; copy.copy({})` | **n=109 SIGSEGV** | — |
| `class C: pass`, `type('X',(),{'__slots__':(...)})` | no crash (non-empty dict → different branch) | — |

**Guarded twin.** `dictobject.c:4499-4512` in the *same function*: `copy_values(...)` and
`PyObject_GC_New(...)` are both NULL-tested before use.
**Fix.** `if (d == NULL) return NULL;` before line 4494 (or `assert(d == NULL || !_PyObject_GC_IS_TRACKED(d))`).

---

### [CONSIDER] `_PyType_GetBases` does `Py_INCREF` on an unchecked `tp_bases`; the check lives one frame too late — `Objects/typeobject.c:592`

```c
585   PyObject *
586   _PyType_GetBases(PyTypeObject *self)
587   {
588       PyObject *res;
589
590       BEGIN_TYPE_LOCK();
591       res = lookup_tp_bases(self);       /* returns self->tp_bases — NULL before type_ready_set_bases */
592       Py_INCREF(res);                    /* Py_INCREF(NULL) — SIGSEGV */
593       END_TYPE_LOCK();
594
595       return res;
596   }
```

Its only caller in the tree does check — but afterwards:

```c
1758  type_get_bases(PyObject *tp, void *closure)
1761      PyObject *bases = _PyType_GetBases(type);
1762      if (bases == NULL) {          /* dead: the crash already happened at :592 */
1763          Py_RETURN_NONE;
```

This is `deref_before_check` split across a function boundary — invisible to a
single-function detector by construction.

**Guarded twins (three, all in this file).**
- `type_get_mro` at `:1772-1776` — the *sibling getset*, ten lines below: `if (mro == NULL) Py_RETURN_NONE; return Py_NewRef(mro);` (check **before** the incref).
- `PyType_GetDict` at `:548-549` — `_Py_XNewRef(dict)`.
- `_PyType_GetSubclasses` at `:783-786` — `if (subclasses == NULL) return list;`.

**Why CONSIDER, not FIX.** `tp_bases == NULL` requires observing a type before
`type_ready_set_bases` (`typeobject.c:9558`) or after `clear_tp_bases`. `type_ready` leaves a
failed *static* type permanently half-built (`:9605-9607`, no `Py_TPFLAGS_READY`), but such a type
is not yet linked into any base's `tp_subclasses` (that happens later, at `:9581`), so I could
not construct a Python-level route. The existence of the (currently dead) NULL branch at
`:1762` shows the author considered NULL reachable.
**Fix.** Move the check into `_PyType_GetBases`, or use `_Py_XNewRef` as `PyType_GetDict` does.

---

### [CONSIDER] `PyType_GetModuleByToken_DuringGC` guards `tp_mro` with only an `assert` — `Objects/typeobject.c:5936-5946`

```c
5936      PyObject *mro = lookup_tp_mro(type);
5937      // The type must be ready
5938      assert(mro != NULL);
5939      assert(PyTuple_Check(mro));
...
5946      Py_ssize_t n = PyTuple_GET_SIZE(mro);      /* Py_SIZE(NULL) with NDEBUG */
```

`tp_mro` is *not* an always-set field: `type_clear` (tp_clear) explicitly
`Py_CLEAR(type->tp_mro)` at **`typeobject.c:7273`**, and `set_tp_mro` accepts `NULL`
(`:653`). This is the CPY-0031 shape (`lazyimportobject.c` reading a tp_clear'd member)
applied to the type object itself. Public reach: `PyType_GetModuleByDef` (`:5973`) →
`PyType_GetModuleByToken_DuringGC`, used by essentially every multi-phase-init extension.

**Guarded twins.**
- **`PyType_GetBaseByToken_DuringGC` at `:6037-6048`, 100 lines below** — same field, same
  situation, and it *handles* it: `PyObject *mro = type->tp_mro; if (mro == NULL) { ...
  get_base_by_token_recursive(lookup_tp_bases(type), token); ... }`.
- `hackcheck_unlocked` at `:10128-10131` — `if (!mro) { /* Probably ok not to check ... */ return 1; }`.
- `is_subtype_with_mro` at `:2905-2925` — the whole function is an explicit `a_mro != NULL` /
  `else follow tp_base` split.

**Why CONSIDER.** Since PEP 442 the GC runs finalizers *before* `tp_clear` and re-checks
reachability, so a resurrected-after-clear type is not constructible from pure Python; and on
a debug build the assert converts the SIGSEGV into a clean abort. I could not build a
Python-level path. The fix is one line and matches the sibling exactly.

---

### [CONSIDER] `init_tp_subclasses` / `clear_tp_subclasses` dereference `_PyStaticType_GetState` with no guard at all — `Objects/typeobject.c:709`, `:725`

```c
708       managed_static_type_state *state = _PyStaticType_GetState(interp, self);
709       state->tp_subclasses = subclasses;        /* no assert, no check */
...
724       managed_static_type_state *state = _PyStaticType_GetState(interp, self);
725       Py_CLEAR(state->tp_subclasses);           /* no assert, no check */
```

Every *other* accessor in the same block guards it: `lookup_tp_dict:531-532`,
`set_tp_dict:557-558`, `clear_tp_dict:570-571`, `lookup_tp_subclasses:736-737` all
`assert(state != NULL)`, and `_PyType_HasSubclasses:763-768` performs a **real runtime test**
with the in-tree comment `// XXX _PyStaticType_GetState() should never return NULL.` — i.e.
the invariant is documented as *not fully trusted*.
**Classification.** CONSIDER / low. Only reachable if a `_Py_TPFLAGS_STATIC_BUILTIN` type is
touched from an interpreter that never registered it. Fix is to add the same `assert` for
consistency.

---

### [ACCEPTABLE / verified clean] — checked, not reported

- **CPY-0007 confirmed, one line, not re-litigated.** `super_descr_get:12797` passes the
  `super.__new__(super)`-uninitialized `su->type` into `supercheck`, which reads
  `type->tp_name` at `:12763` → SIGSEGV. Still present verbatim.
  *Sibling hunt on the same field:* `super_repr:12575` is the guarded twin
  (`su->type ? su->type->tp_name : "NULL"`); `super_getattro:12699` is safe because
  `do_super_lookup` short-circuits on `su_obj_type == NULL` (`:12643`);
  `super_descr_get:12793` passes a possibly-NULL `su->type` to
  `PyObject_CallFunctionObjArgs`, which merely terminates the varargs list early — a
  semantic oddity, not a deref. **No new sibling found.**
- **`PyType_Freeze:12509`** — `mro = type_get_mro(...)` then `PyTuple_Check(mro)`. Flagged by
  the widened rule; **false positive**: `type_get_mro` (`:1769-1777`) returns `Py_None`, never
  `NULL`.
- **`Py_tp_members` with a `NULL` pointer in a `PyType_Spec` / `PySlot` array**
  (`typeobject.c:5374`, `for (memb = it.current.sl_ptr; memb->name != NULL; memb++)`) —
  **not reachable**: `_PySlot_get_null_handling(Py_tp_members)` falls to `default:
  _PySlot_PROBLEM_REJECT` (`pycore_slots_generated.h:942`), and `handle_first_run`
  (`Python/slots.c:326-345`) raises `SystemError: NULL not allowed for slot Py_tp_members`
  before the loop is entered. Verified `Py_tp_members` is `_PySlot_DTYPE_PTR` so the `is_null`
  probe fires. A truncated array missing its `Py_slot_end` sentinel is out of contract and
  undetectable by construction.
- **`bases = lookup_tp_bases(...)` then `PyTuple_GET_SIZE(bases)`** at `assign_version_tag:1427`,
  `mro_implementation_unlocked:3440`, `type_ready_add_subclasses:9400` — all dominated by a
  `Py_TPFLAGS_READY` / `_PyType_IsReady` / in-`type_ready`-pipeline guard. Not bugs.
- **`dict = lookup_tp_dict(...)` / `PyType_GetDict(...)` then `PyDict_*(dict, ...)`** — 11 of
  the 26 widened candidates (`:1611 :1649 :1707 :2057 :2098 :2148 :2177 :2249 :2333 :7882
  :9149 :9379 :10502 :12278`). All are getsets/readiness-pipeline steps on a type whose
  `tp_dict` is set; `type_clear` (`:7267-7270`) `PyDict_Clear`s but never NULLs `tp_dict`.
- **`_PyType_Lookup` returning NULL without an exception** — this is the documented contract
  (`:6388-6390`, `:6278-6282`), enforced by `find_name_in_mro` `PyErr_Clear()`ing its own
  errors at `:6149`, `:6158`, `:6183`. The single in-file caller, `get_dict_descriptor:3973-3977`,
  checks. Taxonomy class "correct by construction". *(The `PyErr_Clear()` of a `PyType_Ready`
  MemoryError at `:6158` is real but belongs to `pyerr-clear-auditor`, and is deliberate.)*
- **`super_init_without_args:12817`** (`co = _PyFrame_GetCode(cframe); co->co_argcount`) —
  `_PyFrame_GetCode` is infallible on a live frame.

### Routed elsewhere (not double-reported)

- **`check_immutable_bases` runs before bases are validated as types** —
  `typeobject.c:5535-5545`: `check_immutable_bases(it.name, bases, 0)` reads
  `_PyType_HasFeature(b, ...)` (i.e. `b->tp_flags`, offset ~0xa8) on every element of a
  caller-supplied `bases` tuple, *before* `find_best_base(bases)` at `:5542` establishes that
  the elements are types. `PyType_FromMetaclass(m, mod, spec_with_IMMUTABLETYPE, (1,2,3))`
  → out-of-bounds read on a small object. Type-confusion OOB read, not a NULL deref →
  **`memory-pattern-analyzer`**.
- The `add_all_subclasses` finding's *stale-exception* aspect (`assert(!PyErr_Occurred())`
  precondition violation) overlaps **`error-path-analyzer`**; the finding is filed here because
  the reproduction and the release-build corruption came out of this pass.

---

## 3. RECALL — the concrete rule shapes that would have caught this

### R1 (highest value) — int-sentinel fallible call: *assigned but never branched on*

The FIX finding is invisible to **both** scanners, for two independent reasons:

1. `scan_null_checks` models only **pointer** results. `add_all_subclasses` returns `int`.
   Out of model entirely — no widening of the API enum would ever reach it.
2. `scan_error_paths::_check_unchecked_returns` (`scan_error_paths.py:507-580`) *would* have a
   shot, but its **FP class A suppression** kills it:
   ```python
   # FP class A: the value flows straight back to the caller (`return res;`) ...
   returned = _check_re([r'return\s+[^;]{0,200}?' + _lvalue_pattern(a) for a in aliases])
   if returned.search(after): continue
   ```
   `res = add_all_subclasses(...)` … `return res;` (line 1982) matches, so it is suppressed.

**The suppression is correct for pointers and wrong for ints.** Returning a `NULL` pointer
*is* CPython's error-reporting contract — the value stops flowing. An `int` sentinel stops
nothing: `res = f(); /* no branch */ g(); h(); return res;` keeps executing `g()` and `h()`
with a live exception and a half-mutated data structure. That is exactly the shape here.

Proposed rule, `int_sentinel_unbranched`:

> For `res = CALL(...)` where the callee's return type is `int`/`Py_ssize_t` (recoverable from
> an in-file `static int NAME(` definition, or a known-int-API table), flag when **no branch
> whose controlling expression mentions `res`** occurs between the assignment and the next
> call statement in the same basic block. `return res;` **does not** discharge the obligation
> for int returns — only a branch does.
>
> Confidence **high** when the enclosing function contains at least one *sibling* fallible call
> that **is** branched on (the guarded twin is in-function): `typeobject.c:1949` and `:1967`
> here. That co-occurrence is what makes this a two-line-window, near-zero-FP rule.

Measured on this file: **1 candidate, 1 true positive** (`typeobject.c:1966`).

### R2 — `assert(EXPR(x))` where `x` is an unchecked fallible result

`dictobject.c:4493` (`assert(!_PyObject_GC_IS_TRACKED(d));`) is a `deref_before_check` whose
deref is *inside an `assert`*. `_DEREF_TEMPLATE` in `scan_null_checks.py:307-318` does not
model `assert(...)` as a dereference site — and worse, `_NULL_CHECK_TEMPLATE:295` treats
`assert\s*\(\s*{var}\b` as a **check**, so an assert on the value silences the rule.

> Rule: treat `assert( <anything mentioning x other than a bare `x` / `x != NULL` / `x == NULL`> )`
> as a **dereference**, not as a check. Confidence high; severity note "debug builds only —
> and the UB additionally licenses the optimizer to delete the caller's NULL check", which is
> what turns it from a failed assert into a SIGSEGV at `0x10`.

### R3 — cross-function `deref_before_check` on a 1-caller helper

`_PyType_GetBases:592` / `type_get_bases:1762` is a real `deref_before_check` split across a
call boundary. A cheap, bounded version:

> For a `static`/internal function whose body is `res = <fallible>; DEREF(res); return res;`
> with **no** NULL test, and which has ≤ 3 call sites in the same file, check whether **any**
> caller NULL-tests the returned value. If yes → report `deref_before_check` at the callee's
> deref line and cite the caller's (dead) check as evidence of intent.

The "the caller has a dead NULL branch" signal is what makes this high-confidence rather than
speculative — it is the author telling you NULL is reachable.

### R4 — `assert(field != NULL)` on a field that a `tp_clear` in the same file `Py_CLEAR`s

Mechanical and cheap: for each `Py_CLEAR(x->FIELD)` inside a function registered as `tp_clear`
(or named `*_clear`), find every *other* read of `->FIELD` in the file guarded only by
`assert`. Fires on `typeobject.c:5938` vs `typeobject.c:7273` here, and would have fired on
CPY-0031 (`lazyimportobject.c`). Emit CONSIDER; name the `if (FIELD == NULL)` twin if one exists
(here `:6038`).

### R5 — denominator reporting (process, not detection)

Every `scan_null_checks` run should emit, next to `summary`, the two numbers this report had to
be reverse-engineered to produce:
`enum_resolved_assignments` / `total_assignment_from_call_sites`, and per rule
`candidate_sites_examined`. `decref_of_nulled_outparam: 0` out of `0 call sites` must be
rendered differently from `0` out of `40`. Without this, a 6.4 %-coverage zero and a
100 %-coverage zero are indistinguishable in the JSON — which is how four v0.8 rules were
certified clean.

---

## 4. Summary

| | |
|---|---|
| `decref_of_nulled_outparam` | 0 (**denominator 0 — structural silence**) |
| `deref_before_check` | 0 reported by the rule; **2 found by reading** (`typeobject.c:592` cross-function, `dictobject.c:4493` inside an `assert`) |
| `unchecked_alloc` | 0 reported by the rule; **26 candidates** from the same pipeline with a widened API alphabet; 4 survive triage |
| Confirmed by OOM injection | **2** (`typeobject.c:1966` on 4/4 builds; `dictobject.c:4493` on debug builds, absent on release) |
| Confirmed prior findings | 1 (CPY-0007, one line) |
| Routed to other agents | 2 (`memory-pattern-analyzer`, `error-path-analyzer`) |

Artifacts: `scratchpad/denom.py`, `scratchpad/stages.py`, `scratchpad/widened.py`,
`scratchpad/sweep1.py` (single-allocation-failure sweep), `scratchpad/bases_probe2.py`.

**Harness note.** `run_oom_sweep.py` arms `set_nomemory(n)` *unbounded* (fail from `n` onward
forever). On any payload that allocates, `n=1` then cascades — the interpreter cannot build the
`MemoryError` traceback and dies with `lost sys.stderr`. That produced a `first_crash: n=1`
false positive for `type('X',(),{})` that is **not** a bug. The bounded form
`set_nomemory(n, n+1)` (one failing allocation) is what isolates a real single-failure defect,
and it moved the same payload's crash from the meaningless `n=1` to the meaningful `n=127`.
Recommend adding `--width` (default 1) to `run_oom_sweep.py`.
