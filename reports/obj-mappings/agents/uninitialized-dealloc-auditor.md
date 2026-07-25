# uninitialized-dealloc-auditor — slice `obj-mappings`

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777`
Scope: `Objects/dictobject.c` (8,597 lines) + `Objects/setobject.c` (3,228 lines) — nothing else.
Build matrix commit `a1d580430c8`: **both slice files are byte-identical to the target ref**
(`git diff --stat a1d580430c8 4f3be1b5777 -- Objects/dictobject.c Objects/setobject.c` is empty),
so every crash below is evidence about the ref under review.

---

## 1. Denominator

| quantity | value | source |
|---|---|---|
| scanner findings | **0** | `scanners/scan_uninit_dealloc.sample.json` |
| scanner non-zeroing allocation sites seen | **8** | same, `summary.allocation_sites` |
| scanner functions analyzed | **404** | same, `denominators.functions_analyzed` |
| scanner files analyzed | **2** | same |
| **object-construction sites enumerated by hand** | **13** (10 dict + 3 set) | §3 |
| of those, sites that build a `PyObject` | **8** | §3 |
| **sites that free a never-GC-tracked object** | **~20** | §3.3 |
| lines read | 11,825 (slice) + ~450 (`typeobject.c` `subtype_dealloc`, `pycore_gc.h`, `gc.c`, `gc_free_threading.c`) | |
| findings | **1 FIX** (confirming CPY-0015, mechanism refined), **1 cross-class FIX** (CPY-0079 upgraded to *reproduced*) | |

**Classification of the zero: EVIDENTIAL, not structural.**
The rule ran, saw 8 real non-zeroing allocations in these two files, and rejected all 8 — and it
was *right* to: no constructor in either file leaves a member holding allocator garbage at an
early free. What the zero does **not** mean is "the class is clean here". CPY-0015 is a live,
reproducing instance of the `dealloc-of-uninitialized-object` family in this exact slice, and it
crashes through a mechanism this rule does not model. §4 is that diagnosis.

---

## 2. Findings

### 2.1 [FIX] `dictiter_new` frees a never-tracked iterator — `Objects/dictobject.c:5646` (= CPY-0015, **confirmed**; release-build mechanism corrected)

Recorded finding, not re-litigated. Confirmed still present and still reproducing at `4f3be1b5777`.
What is **new** here is the mechanism on non-assert builds and the free-threaded split.

**Mechanism.** `dictiter_new` (`:5617`) allocates at `:5621` with `PyObject_GC_New`, writes *every*
member (`di_dict` `:5625`, `di_used` `:5627`, `len` `:5628`, `di_pos` `:5632`/`:5635`/`:5639`,
`di_result` `:5644`), runs the fallible `_PyTuple_FromPairSteal(Py_None, Py_None)` at `:5644`, and on
failure executes `Py_DECREF(di)` at `:5646`. `_PyObject_GC_TRACK(di)` is not reached until `:5653`.
`dictiter_dealloc` at `:5662` therefore runs the **unchecked macro** `_PyObject_GC_UNTRACK(di)` on an
object the GC never saw. No member is garbage — the crash is *upstream of the member reads*.

**Reproduced** (`repro/CPY-0015_standalone.py`, and via `run_oom_sweep.py` with
`repro/CPY-0015_dictiter_new_{setup,payload}.py`). Sweep envelope quoted:
`allocation_failure_points: 25`, `thin_evidence: false`,
`verdict: "REPRODUCED — allocation failure crashes the interpreter"`.

| build | sweep points | outcome | standalone rate |
|---|---|---|---|
| `debug-gil-nojit` | 25 | **SIGABRT** n=23 — `Objects/dictobject.c:5662: _PyObject_GC_UNTRACK: Assertion "_PyObject_GC_IS_TRACKED(...)" failed`, `object type name: dict_itemiterator` | 20/20 |
| `debug-gil-nojit-asan` | 25 | **SIGABRT** n=23, same assertion | — |
| `release-gil-nojit` | 25 | **SIGSEGV** n=23 | 20/20 |
| `debug-ft-nojit` | 27 | **SIGABRT** n=25, same assertion | 20/20 |
| `release-ft-nojit` | 27 | **clean**, 27/27 `memory_error`, `thin_evidence: false` | 0/20 |

**Correction to the recorded entry.** CPY-0015's `meta.json` says *"GC-list corruption with NDEBUG"*.
It is not corruption — it is a hard **SIGSEGV writing through NULL**. `addr2line -f -i` on the
release-GIL frame resolves the faulting PC to
`_PyGCHead_SET_NEXT` / `Include/internal/pycore_gc.h:151`, reached from `dictitems_iter` →
`dictiter_new` → `_Py_Dealloc` → `dictiter_dealloc`. `gc_alloc` (`Python/gc.c`) zeroes the
`PyGC_Head` (`((PyObject **)mem)[0] = NULL; [1] = NULL;`), so on a never-tracked object
`_PyObject_GC_UNTRACK` computes `prev = NULL`, `next = NULL` and then stores
`prev->_gc_next = next` — a write to address 0.

**Second correction: the free-threaded release build is benign.** `#ifdef Py_GIL_DISABLED` the
untrack is `_PyObject_CLEAR_GC_BITS(op, _PyGC_BITS_TRACKED)` — clearing an already-clear bit, not a
list unlink. So the severity ladder is *debug: SIGABRT (both) / release-GIL: SIGSEGV /
release-FT: nothing*. Anyone reproducing this on a free-threaded release build will get a clean
`MemoryError` and wrongly conclude it is fixed.

**Guarded twin — and what its guard actually defends (lesson 3).**
The twin is `frozendict_new` (`Objects/dictobject.c:8502`), in the same file. It has *structurally
the identical shape*: build untracked (`:8505`), run the fallible `dict_update_common` (`:8511`,
which runs arbitrary Python — user `keys()`/`__iter__`/`__hash__`, so it needs no OOM at all),
`Py_DECREF(d)` at `:8514` on a never-tracked object, `_PyObject_GC_TRACK(d)` only at `:8520`.
It is safe for exactly one reason: `PyFrozenDict_Type.tp_dealloc` is `dict_dealloc`, which at
`:3661` calls the **checked function** `PyObject_GC_UnTrack(mp)`, whose body is
`if (_PyObject_GC_IS_TRACKED(op)) { _PyObject_GC_UNTRACK(op); }`.

That guard is **incidental, not intentional**, and this is the load-bearing observation. Five
deallocs across the slice carry the *identical* comment:

```
/* bpo-31095: UnTrack is needed before calling any callbacks */
```
— `setobject.c:741` (`set_dealloc`, function form), `setobject.c:1044` (`setiter_dealloc`, **macro**),
`dictobject.c:3661` (`dict_dealloc`, function form), `dictobject.c:5662` (`dictiter_dealloc`,
**macro**), `dictobject.c:6437` (`dictview_dealloc`, **macro**).

bpo-31095 is about *ordering* — untrack before callbacks run — and says nothing whatsoever about
untracked-tolerance. So the comment is orthogonal to the threat model that actually separates the
safe sites from the crashing one. The safety of `frozendict_new`, `set_copy`, `set_union` and the
whole untracked-builder family is an accident of which spelling the author happened to reach for.

**Fix.** Either move `_PyObject_GC_TRACK(di)` above the `_PyTuple_FromPairSteal` call (the object is
fully initialized by `:5639`, so tracking early is safe), or change `:5662` to
`PyObject_GC_UnTrack(di)`. The first is preferable — it also fixes `dictview_dealloc`'s and
`setiter_dealloc`'s macro use by making the invariant true rather than tolerating its violation.

**Sibling hunt result: no un-found siblings in this slice.** Three types in the slice untrack with
the unchecked macro (`dict_itemiterator`/`dict_keyiterator`/… via `dictiter_dealloc`, the three
view types via `dictview_dealloc`, `set_iterator` via `setiter_dealloc`). Their constructors are
`dictiter_new` (**the bug**), `_PyDictView_New` (`:6475` — allocate, `dv->dv_dict = Py_NewRef(dict)`,
track; *no fallible step in between, no early free*), and `set_iter` (`setobject.c:1173` — allocate,
four member writes, track; same). `set_iter` is the cleanest possible twin for `dictiter_new`: same
iterator-construction shape, and it simply has nothing fallible to fail.

### 2.2 [FIX] `copy_lock_held_untracked` — `Objects/dictobject.c:4492` (= CPY-0079, upgraded `static-confirmed` → **reproduced**, and the predicted optimizer consequence *observed*)

Out of my declared class (`null-deref`), picked up because the run context flags its clean sweep as
having had a denominator of 2, and I had the OOM rig warmed for the same file. **Re-run with a real
denominator.**

*Convergence note:* another agent in this slice independently re-swept the same site
(`repro/CPY-0079_sweep_*.json`: 48 / 40 / 46 failure points, `thin_evidence: false`, same
debug-reproduces / release-clean split). Do not double-count — that is one finding, confirmed
twice. My distinct contribution is the gdb evidence below that the *predicted optimizer
consequence* actually occurs, which is what upgrades the record's central claim from inference to
observation.

`copy_lock_held_untracked` takes the `mp->ma_used == 0` arm at `:4484`, assigns
`d = dict_new_untracked(&PyDict_Type)` at `:4492` with **no NULL check**, and then executes
`assert(!_PyObject_GC_IS_TRACKED(d))` at `:4494` — an unconditional dereference of `d`.

Sweep envelope (`repro/CPY-0079_copy_empty_{setup,payload}.py`), all four builds:
`allocation_failure_points: 40`, `thin_evidence: false`.

| build | outcome | verdict string |
|---|---|---|
| `debug-gil-nojit` | **SIGSEGV** n=39 | `REPRODUCED — allocation failure crashes the interpreter` |
| `debug-ft-nojit` | **SIGSEGV** n=39 | `REPRODUCED — allocation failure crashes the interpreter` |
| `release-gil-nojit` | clean | `no crash over N=40 allocation-failure points (all handled cleanly)` |
| `release-ft-nojit` | clean | `no crash over N=40 allocation-failure points (all handled cleanly)` |

Standalone (`repro/CPY-0079_standalone.py`): **SIGSEGV 20/20** on `debug-gil-nojit` and
`debug-ft-nojit`, **0/20** on `release-gil-nojit`.

**The finding's central claim is now directly observed, not inferred.** CPY-0079 asserts *"the UB
lets the optimizer delete the inlined NULL check"*. gdb on `debug-gil-nojit` (`OPT = -g -Og -Wall`,
`--with-pydebug`):

```
Program received signal SIGSEGV, Segmentation fault.
0x000055555570556b in anydict_new_untracked (type=<optimized out>) at Objects/dictobject.c:5368
5368	    d->_ma_watcher_tag = 0;
#0  anydict_new_untracked (...) at Objects/dictobject.c:5368
#1  dict_new_untracked (...) at Objects/dictobject.c:5382
#2  copy_lock_held_untracked (o={}, as_frozendict=0) at Objects/dictobject.c:4492
#3  PyDict_Copy (o={}) at Objects/dictobject.c:4599
#4  dict_copy_impl (...) at Objects/dictobject.c:4449
#5  dict_copy (...) at Objects/clinic/dictobject.c.h:57
```

The fault is **inside the inlined callee**, at the member store `d->_ma_watcher_tag = 0` — meaning
`anydict_new_untracked`'s own guard `if (self == NULL) return NULL;` at `:5362` was **elided**.
So the crash is not "an assert read NULL" (a debug-only artifact that a maintainer can wave away);
it is "the assert's UB deleted a real NULL check that would otherwise have returned cleanly, and the
faulting instruction is a *write*". Release is clean only because `NDEBUG` removes the assert and
with it the UB, which restores the check.

**Guarded twin.** The very next arm of the *same function*, `:4499-4511`: `split_copy = PyObject_GC_New(...)`
followed by `if (split_copy == NULL) { free_values(newvalues, false); return NULL; }`. Same function,
same author, ten lines apart — one arm checks, the other asserts.

**Fix.** `if (d == NULL) { return NULL; }` before the assert at `:4494`.

---

## 3. Classes bounded — the full construction enumeration

Every object-construction site in both files, and why each is or is not this class. This table *is*
the denominator; the scanner's `allocation_sites: 8` covers only rows marked with a non-zeroing
`PyObject` allocator.

### 3.1 `Objects/dictobject.c` — 10 allocation sites

| line | function | allocator | member-garbage at an early free? | verdict |
|---|---|---|---|---|
| 857 / 860 | `new_keys_object` | `_Py_FREELIST_POP_MEM` / `PyMem_Malloc` | `PyDictKeysObject` is not a `PyObject`; no `tp_dealloc`. `init_keys_object` fully initializes, no early free | **out of class** |
| 917 | `new_values` | `PyMem_Malloc` | raw `PyDictValues` struct, three field writes, no free | **out of class** |
| 974 / 976 | `new_dict` | freelist / `PyObject_GC_New` | **no early free exists.** `new_dict_impl` handles `mp == NULL` by freeing its *inputs* (`dictkeys_decref` / `free_values`) and returning NULL — it never touches `mp` | **ACCEPTABLE** |
| 988 / 990 | `new_dict_untracked` | same | same | **ACCEPTABLE** |
| 1002 | `new_frozendict_untracked` | `PyObject_GC_New` | same; `frozendict=1` also sets `ma_hash = -1` in `new_dict_impl` | **ACCEPTABLE** |
| 1036 | `clone_combined_dict_keys` | `PyMem_Malloc` | keys array, `memcpy`'d whole; not a `PyObject` | **out of class** |
| 4505 / 4509 | `copy_lock_held_untracked` (split path) | `PyObject_GC_New` | five member writes with **no fallible call between them**; the only failure path frees `newvalues`, never `split_copy` | **ACCEPTABLE** (for *this* class — see 2.2 for the sibling arm) |
| **5621** | **`dictiter_new`** | `PyObject_GC_New` | all members set — but freed at `:5646` **before** `_PyObject_GC_TRACK` at `:5653`, into a macro-untracking dealloc | **FIX (2.1)** |
| 6475 | `_PyDictView_New` | `PyObject_GC_New` | `dv->dv_dict = Py_NewRef(dict)` then track; nothing fallible between, no early free | **ACCEPTABLE** |
| 7265 | `_PyDict_NewKeysForClass` | `PyMem_Malloc` | `struct _instancekeysobject`, not a `PyObject` | **out of class** |
| (5361) | `anydict_new_untracked` | `_PyType_AllocNoTrack` | **zeroing** (memsets past the `PyObject` header, `typeobject.c:2542`); four explicit member writes; no early free | **ACCEPTABLE** |

### 3.2 `Objects/setobject.c` — 3 allocation sites

| line | function | allocator | verdict |
|---|---|---|---|
| 527 | `set_table_resize` | `PyMem_NEW` | `setentry` table, not a `PyObject` | **out of class** |
| 1175 | `set_iter` | `PyObject_GC_New` | four member writes, **no fallible step**, then track. No early free exists. **This is `dictiter_new`'s guarded twin** | **ACCEPTABLE** |
| 1356 | `make_new_set_untracked` | `_PyType_AllocNoTrack` (**zeroing**) | seven explicit member writes, *then* the fallible `set_update_local` at `:1369` and `Py_DECREF(so)` at `:1370`. Every member is set; the object is never tracked at that point, but `set_dealloc:742` uses the **checked** `PyObject_GC_UnTrack` | **ACCEPTABLE** |

`make_new_set_untracked` is reachable with a heap subclass (`make_new_frozenset` → `make_new_set(type, …)`
with `type != &PyFrozenSet_Type`), so the dealloc can be `subtype_dealloc`. Checked:
`Objects/typeobject.c:2785` — the GC arm of `subtype_dealloc` also uses the checked function
`PyObject_GC_UnTrack(self)`. Safe on that path too.

### 3.3 The "free a never-GC-tracked object" family — ~20 sites, all safe

Both files use a deliberate build-untracked-then-track idiom (`setobject.c:1348` even documents it:
*"Build a set/frozenset left GC-untracked; the caller must `_PyObject_GC_TRACK()` it once fully
built, so a half-built set is never exposed during filling"*). Every one of these frees a fully
initialized but **never-tracked** object on error:

* `dictobject.c`: `frozendict_new:8514`, `_PyDict_FromKeys:3627`, and the track sites at
  `:4602 PyDict_Copy`, `:4643 _PyDict_CopyAsDict`, `:5205 _PyDict_Or`, `:5393 dict_new`,
  `:5475 frozendict_vectorcall`, `:6797 dictitems_xor_lock_held`, `:8566 frozendict_copy_impl`.
* `setobject.c`: `make_new_set:1383`, `make_new_set_basetype:1405`, `set_copy_impl:1609`,
  `set_union_impl:1679`, `set_intersection:1762`/`:1794`, `set_difference_multi_impl:2198`,
  `set_sub:2214`, `set_symmetric_difference_impl:2374` (14 `Py_DECREF` sites in total between
  `:1589` and `:2399`).

All of them land in `dict_dealloc` / `set_dealloc` / `subtype_dealloc`, **all three of which use the
checked function** `PyObject_GC_UnTrack`. The family is safe — but safe by the coin-flip described in
§2.1, not by design. `dictiter_new` is the one member of the family whose dealloc reached for the
macro.

### 3.4 Explicitly checked and clean

* **No non-zeroing `tp_alloc`.** Both files register `_PyType_AllocNoTrack` (`dictobject.c:5536`,
  `:8592`; `setobject.c:2930`, `:3022`), which memsets. `nonzeroing_tp_allocs` in the sample envelope
  is `{}`, consistent. The `_datetimemodule.c` `time_alloc`/`datetime_alloc` shape does not occur here.
* **No `memset`-hidden zeroing and no wrapper macro** hiding a zeroing allocator in either file.
* **No enum/scalar discriminator read during teardown** (the blake2 `impl` shape). `dictiter_dealloc`,
  `dictview_dealloc`, `setiter_dealloc`, `dict_dealloc` and `set_dealloc` read only pointer members
  plus `so->used`/`so->mask`, all set by the zeroing allocator or immediately after it. The one
  discriminator-shaped read in the wider family — `odictiter_dealloc`'s `di->kind` — is in the
  *other* slice (CPY-0011).
* **No cross-file `tp_dealloc`** for any type constructed in these two files (the scanner's documented
  recall hole (i) does not bite here): every `tp_dealloc` referenced by a type defined in
  `dictobject.c`/`setobject.c` is defined in the same file.

---

## 4. Toolkit feedback

### 4.1 The recall gap, diagnosed precisely

**`scan_uninit_dealloc.py` cannot fire on `dictiter_new`, and its model is not wrong — its model is
a different bug.**

The rule's predicate (`_check_function`, lines 475-483) is
`unset_at_free = written_after − written_before`, filtered to members the destructor treats as
pointers. Trace it on `dictiter_new`:

* `di->di_result = _PyTuple_FromPairSteal(...)` at `:5644` sits in the consequent of the
  `itertype == &PyDictIterItem_Type` `if`; the free at `:5646` sits in a nested `if` **inside that
  same consequent**. `_dominates()` walks up from the write, reaches the shared `compound_statement`,
  finds it in `free_ancestors`, and returns `True`. So `di_result ∈ written_before`.
* `di->di_result = NULL` in the sibling `else` at `:5651` puts `di_result ∈ written_after` too.
* `written_after − written_before = ∅` → `continue`. No finding.

That is **correct**. At `:5646` no member of `di` holds allocator garbage: `di_dict`, `di_used`,
`len` and `di_pos` are all written above the branch, and `di_result` is NULL because the call that
failed assigned NULL. The crash happens *before* `dictiter_dealloc` ever reads a member — it is a
**GC-tracking-state** bug (`_PyObject_GC_UNTRACK` on a never-tracked object), not a
**member-initialization** bug. Two distinct mechanisms share one finding category.

**And the toolkit already detects it — in a different scanner.** Re-reading the slice's own
scanner samples:

* `scanners/scan_memory_patterns.sample.json` → `gc_untrack_without_track` at
  **`Objects/dictobject.c:5646 dictiter_new`** (bug class O6), the exact site.
* `scanners/scan_deprecated_apis.sample.json` → `gc-untrack-macro-form` at
  **`Objects/dictobject.c:5662 dictiter_dealloc`**, the dealloc half, with the detail line already
  naming `dictiter_new` and line 5646.

So this is a **routing/taxonomy gap, not a detection gap**: CPY-0015 is filed in the findings repo
under category `uninit-dealloc`, but the rule that surfaces it lives in `scan_memory_patterns.py`,
and `agents/uninitialized-dealloc-auditor.md` names `scan_uninit_dealloc.py` as its only input
(line 21). A slice whose only instance of the class is O6-shaped reads to this agent as a
structural zero.

### 4.2 Proposed rule change D-14 — MEASURED, and **REJECTED**

Hypothesis: add a track-state arm to `scan_uninit_dealloc.py` — for a GC allocator, if the object is
freed before any `*_GC_Track` and the file uses `_PyObject_GC_UNTRACK`, emit
`dealloc_of_never_tracked_object` regardless of member state.

Implemented in a scratchpad copy (the toolkit tree was never modified; `git status` verified clean,
`git diff --stat plugins/cpython-review-toolkit/scripts/` empty). Re-run over
`Objects/` + `Modules/` + `Python/` — 614 files, 19,521 functions, 171 non-zeroing allocation sites.

| | before | after |
|---|---|---|
| Objects/ | 3 | 6 |
| Modules/ | 1 | 1 |
| Python/ | 0 | 1 |
| **total** | **4** | **8** |

The four new findings:

| site | status |
|---|---|
| `Objects/dictobject.c:5621 dictiter_new` | **recovers CPY-0015** ✅ — but already emitted by `scan_memory_patterns.py` |
| `Objects/odictobject.c:1945 odictiter_new` | CPY-0011 — **already recovered by the unmodified rule** (same line), and *also* already emitted by `scan_memory_patterns.py`. A third copy |
| `Objects/listobject.c:250 PyList_New` | CPY-0014 — **duplicate of the existing finding on the same line** |
| `Python/context.c:879 contextvar_new` | **NET NEW — and a FALSE POSITIVE.** `contextvar_tp_dealloc` (`context.c:957`) uses `PyObject_GC_UnTrack`, the *checked function*. My cheap arm used the file-level `_PyObject_GC_UNTRACK in source_bytes` test, and `Python/context.c` contains that macro in *other* types' deallocs. `scan_memory_patterns.py`'s O6 rule resolves the type argument → `PyContextVar_Type` → its `tp_dealloc` → function form → correctly silent |

**Verdict: REJECT.** +4 findings; 1 net-new site; **0 net-new true positives; 1 net-new false
positive**. Net-new precision 0/1. Duplication rate 3/4. 100% of the arm's true positives are
already produced by `scan_memory_patterns.py`'s `gc_untrack_without_track`, which uses a *stricter*
type-resolved gate and emits exactly 2 findings tree-wide (`dictiter_new`, `odictiter_new`) with 0
false positives. This is D-13's shape: a widening that adds noise and recovers nothing that is not
already covered.

**Recall of CPY-0011 is worth calling out separately**, because it explains why the class *looked*
covered: the unmodified rule catches `odictiter_new` on its **member-garbage** half (`di_current`,
`di_odict`, `kind` are all written *after* the free at `:1952`). `odictiter_new` genuinely has both
defects; `dictiter_new` has only the tracking one. The plugin's own docstring lists
`odictiter_new` as a confirmed exemplar of this scanner — true, but for the other half of the bug.

### 4.3 Proposed change instead — routing, zero code, measured

Add `scan_memory_patterns.py`'s `gc_untrack_without_track` findings to this agent's Phase-1 input in
`plugins/cpython-review-toolkit/agents/uninitialized-dealloc-auditor.md` (§"Script-Assisted
Analysis", currently a single command at line 21), with one sentence naming the two half-mechanisms:

> The class has two mechanisms. `scan_uninit_dealloc.py` finds **member garbage** at the free point.
> `scan_memory_patterns.py --type gc_untrack_without_track` finds the **never-tracked** half, where
> every member is set but `tp_dealloc`'s unchecked `_PyObject_GC_UNTRACK` faults before reading any
> of them. Read both; a constructor can have one, the other, or both.

Measured effect:

* **This slice:** population 0 → 1; recall of the `uninit-dealloc` category 0/1 → **1/1**; new false
  positives **0**.
* **`Objects/` + `Modules/` + `Python/`:** population 4 → 6 findings over **5 distinct constructors**
  — `PyList_New`, `odictiter_new`, `template_iter`, `create_elementiter`, `dictiter_new`. All 5 are
  catalogued (CPY-0014, CPY-0011, CPY-0017, CPY-0016, CPY-0015). Precision **5/5**, new false
  positives **0**.

### 4.4 A false-positive-taxonomy entry that would have *suppressed* CPY-0015

`data/cpython_non_bugs.md`, "Uninitialized dealloc", third bullet:

> **`tp_dealloc` guards each member with `Py_XDECREF`** *and* the members were NULL-initialized
> before the failing step — `Py_XDECREF(NULL)` is a no-op, so no crash. Only a member left as
> *garbage* (not NULL) at the free point is a bug.

`dictiter_new` satisfies this exoneration **exactly** — `dictiter_dealloc` `Py_XDECREF`s both
members, and both are set (one to a valid pointer, one to NULL) before the free — and it aborts
anyway. Under informed-mode rule 2 an agent that found this by reading would be required to justify
flagging it against the taxonomy, and the honest justification is not obvious. Proposed amendment:

> …*unless* the object is freed before `_PyObject_GC_TRACK` and its `tp_dealloc` opens with the
> unchecked `_PyObject_GC_UNTRACK` **macro** — that faults (SIGABRT with assertions, SIGSEGV on a
> release GIL build) *before* any member is read, so member state is irrelevant. The checked
> function `PyObject_GC_UnTrack` is the tolerant spelling. Live instance: `dictiter_new`
> (CPY-0015).

### 4.5 Two OOM-methodology traps this run paid for — both freelist-shaped

Neither is in `run_oom_sweep.py`'s docstring, and both produce a **clean sweep on a bug that
reproduces 20/20**.

1. **A freelist pop is not an allocator call, so `set_nomemory` cannot fail it.** The first
   `dictiter_new` sweep reported `allocation_failure_points: 2`, `2/2 memory_error`, `thin_evidence:
   true` — a false clean. `_PyTuple_FromPairSteal` was taking the 2-tuple freelist every time.
   Same for `copy_lock_held_untracked`, which needs the `PyDictObject` freelist
   (`_Py_FREELIST_POP` at `dictobject.c:974`/`:988`) drained first.
2. **Draining in `--setup` does not survive the arming call.** `set_nomemory` is `METH_VARARGS`, so
   the interpreter builds a 2-element argument tuple for it and releases that tuple **after** the
   hooks are live — handing exactly one 2-tuple back to the freelist at the worst possible moment.
   A drain must therefore run **inside the armed payload**, and must keep the objects **referenced**
   (dropping them returns each one to the freelist immediately).

Both fixes also cure `thin_evidence`: `dictiter_new` went 2 → 25 failure points, `copy_lock_held_untracked`
19 → 40, purely by moving the drain into the payload and sizing it. Suggested addition to the
`run_oom_sweep.py` methodology docstring, since this generalizes to every freelisted type
(tuples, dicts, lists, floats, frames, slices, contexts):

> **Drain the freelist, inside the payload, and keep the references.** If the allocation under test
> is served by a freelist, no injected failure can ever reach it. Warm-up in `--setup` is not
> enough — the `METH_VARARGS` arming call itself refills the 2-tuple freelist. A sweep whose
> `allocation_failure_points` is far below the number of allocations you expect the payload to make
> is the symptom.

### 4.6 Precision per rule, this slice

| rule | fired | true positives | note |
|---|---|---|---|
| `scan_uninit_dealloc.dealloc_of_uninitialized_object` | 0 / 8 alloc sites | n/a | evidential zero; the 8 rejections are all correct (§3) |
| `scan_memory_patterns.gc_untrack_without_track` | 1 | **1/1** | CPY-0015, exact site. 100% precision here and tree-wide (2/2) |
| `scan_deprecated_apis.gc-untrack-macro-form` | 1 | **1/1** | the dealloc half of the same bug; its detail line already cross-references the constructor |
| D-14 experimental track-arm | 4 (tree-wide) | 3, **all duplicates** | + 1 false positive. Rejected |

---

## 5. Noticed outside slice

* `Objects/odictobject.c:1945 odictiter_new` (CPY-0011) carries **both** halves of this class —
  member garbage (`di_current`/`di_odict`/`kind` written after the free) *and* the never-tracked
  untrack. `dictiter_new` has only the second. Whoever writes that slice should record both.
* `Python/context.c:879 contextvar_new` — the D-14 arm's false positive. Genuinely safe:
  `contextvar_tp_dealloc:957` uses the checked `PyObject_GC_UnTrack`, and `var_hash` is the only
  member set after the last fallible step. Recording it so a future widening does not re-discover it.
* `Objects/typeobject.c:2802`/`:2824` — `subtype_dealloc` calls the unchecked
  `_PyObject_GC_UNTRACK(self)` inside the `tp_finalize` and `tp_del` arms, but each is paired with an
  `_PyObject_GC_TRACK(self)` three lines above, so the invariant holds. Not a finding; noted because
  it is the only other macro use on the path any slice-constructed subclass takes.
