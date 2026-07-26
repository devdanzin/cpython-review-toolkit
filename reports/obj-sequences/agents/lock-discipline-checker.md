# lock-discipline-checker — slice `obj-sequences` (Group B2, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`, plus the sibling `Objects/clinic/*.c.h` read as evidence (not reviewed as scope)
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-verified independently for a fifth time — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files plus `Objects/clinic/{listobject,bytesobject,bytearrayobject}.c.h` is **empty**. Every line number below is valid on every matrix build without adjustment.

**Read first:** `AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/gil-discipline-checker.md`, `agents/refcount-auditor.md`, `agents/error-path-analyzer.md`, `preflight/informed_briefing.md`.

**Pre-run scanner:** `scan_lock_discipline.sample.json` — 0 findings, `vocabulary_resolved: 134`, `critical_section_functions: 54`, `mutex_functions: 0`.

---

## Headline

**The 38% was measurable, and measuring it changed the conclusion twice.**

1. **The zero is now 100% earned for the class the rule models.** I walked **108 of 108** critical-section regions across all seven files with an independent comment- and string-stripped token walker: **0 leaks, 0 family mismatches, 0 unreachable-END artefacts.** 96 of the 108 have a one-statement body; the 12 that do not I hand-read, and the only two `goto`s in the slice both target a label *inside* their own region. The pre-run zero covered 62% of the population; the other 38% is also clean, and now it is stated rather than assumed.

2. **But the blind spot was never the scanner's.** `scan_common.C_EXTENSIONS` is `{".c", ".h"}` and `clinic` is not in `EXCLUDE_DIRS`, so `Objects/clinic/*.c.h` has always been inside the scanner's file set. I ran `scan_lock_discipline.py` directly on all three headers: it finds **8 / 33 / 0** critical-section functions and 0 findings, with clean envelopes. **The 41 invisible regions are a `slice_files.txt` scoping artefact, not a scanner limitation** — a materially different and far cheaper defect than the one three agents in this slice have now independently reported as a tooling gap.

3. **Two novel FIX findings, both reproduced, both live on today's main, from task (c).** `bytearray.__iadd__` and `bytearray.extend()` reach a second bytearray's per-object lock **nested**, through `PyObject_GetBuffer`, instead of taking `Py_BEGIN_CRITICAL_SECTION2` up front — the exact shape upstream fixed for `bytearray_ass_subscript` and `bytearray_mod` in **gh-130227 / PR #130227 (merged 2025-02-27)**, whose PR body enumerates the two-operand functions it double-checked and names neither of these two.
   - **L1** — `b1 += b2` racing `b2 += b1` fails with `BufferError` **45.2%** of the time on `release-ft-nojit` (43.7% debug-ft, 44.1% for `.extend()`), against **0 of 30.6M** under the GIL, **0 of 26.3M** single-threaded, **0 of 68.1M** on disjoint pairs, and **0 of 13.3M** through the `Py_BEGIN_CRITICAL_SECTION2` twin. Four controls, all clean.
   - **L2** — the same window is a **reachability escalation for gh-153578**: **3/3 SIGABRT** on `debug-ft-nojit` (`Objects/bytearrayobject.c:560: Assertion 'avail >= 0' failed`) and **uninitialised bytes handed to Python in 119 of 335 successful calls** on `release-ft-nojit`, with **two plain bytearrays, two threads, and no adversarial `__buffer__` at all**. The open PR #153579 fixes L2 and not L1.

4. **`nested_critical_sections` is miscalibrated for CPython, and I can show it.** 40 build×scenario cells of deliberate lock-order inversion — including a 3-object cycle — produced **0 deadlocks** across ~250M iterations. CPython's per-object critical sections *cannot* deadlock against each other: `PyMutex_Lock` carries `_PY_LOCK_DETACH`, so parking on a contended `ob_mutex` detaches and `_PyCriticalSection_SuspendAll` releases every outer section. **The hazard of nesting in CPython is not the deadlock the rule names — it is the silent suspend**, and that is precisely what produces L1 and L2.

5. **Task (b) answered, and it corrects the denominator the new rule would be built on.** `list` is **complete** — 0 gaps across 32 Python-visible entry points plus 8 iterator entry points. `bytes` is immutable and correctly has zero locks; its **only** gap is its iterator (CPY-0182 + the three G1 siblings). And `bytearray`'s real method surface is **56, not 36**: 37 clinic `METHODDEF`s (33 locked) **plus 19 hand-written `PyMethodDef` entries of which 18 carry their own `Py_BEGIN_CRITICAL_SECTION(self)`**. The gh-129107 sweep installed **51 critical sections across two different mechanisms** and missed exactly one entry point. That makes CPY-0187 a stronger finding and Group B's proposed clinic-only rule a weaker instrument.

---

# 1. Phase 0 — the denominator, measured seven ways

`scratchpad/walk_cs_regions.py` (independent of `scan_lock_discipline`: raw token scan over comment- and string-stripped source, stack-matched, family-checked).

| file | lines | regions | CS fns | `…SECTION(` | `…SECTION2(` | in pre-run scope? |
|---|---|---|---|---|---|---|
| `Objects/listobject.c` | 4,317 | 33 | 22 | 23 | 10 | ✅ |
| `Objects/bytearrayobject.c` | 3,120 | 34 | 32 | 32 | 2 | ✅ |
| `Objects/bytesobject.c` | 3,926 | **0** | 0 | 0 | 0 | ✅ |
| `Objects/bytes_methods.c` | 738 | **0** | 0 | 0 | 0 | ✅ |
| `Objects/clinic/listobject.c.h` | — | 8 | 8 | 8 (+1 comment) | 0 | ❌ scope only |
| `Objects/clinic/bytearrayobject.c.h` | — | 33 | 33 | 33 (+1 comment) | 0 | ❌ scope only |
| `Objects/clinic/bytesobject.c.h` | — | **0** | 0 | 0 | 0 | ❌ |
| **total** | | **108** | **95** | **65+2** | **12** | **67 / 108 = 62%** |

Three readings of the same shortfall, all correct, none interchangeable:

- **regions:** 67 of 108 = **62%**
- **functions:** 54 of 95 = **57%** (the mapper's figure)
- **macro tokens:** `vocabulary_resolved: 134` of 216 = **62%**

`vocabulary_resolved: 134` is exactly `55+12+55+12`, i.e. 67 regions × 2 tokens. It is internally consistent and it is 62% of the truth. **I covered 108 of 108 = 100%.**

## 1.1 The blind spot is scope, not capability — and this matters for the fix

`include-graph-mapper` §e, `gil-discipline-checker` §1.3 and `error-path-analyzer` §2.5 all report the 41 clinic regions as invisible *to the scanner*. They are invisible to **this run**. The scanner reads them fine:

```
Objects/clinic/listobject.c.h        files 1 funcs 14 findings 0 cs_fns  8 mutex_fns 0
Objects/clinic/bytearrayobject.c.h   files 1 funcs 38 findings 0 cs_fns 33 mutex_fns 0
Objects/clinic/bytesobject.c.h       files 1 funcs 26 findings 0 cs_fns  0 mutex_fns 0
```

`tree_sitter_utils.C_EXTENSIONS = frozenset({".c", ".h"})`, and `Objects/clinic/listobject.c.h` has `Path.suffix == ".h"`. `EXCLUDE_DIRS` contains `.git .tox .venv venv __pycache__ node_modules build dist .eggs egg-info` — **not** `clinic`. A whole-`Objects/` run has always scanned them. The fix is therefore one line in the slice manifest, not a change to `scan_common`. Proposal in §7.1.

## 1.2 One real envelope defect, found while doing this

On `clinic/listobject.c.h` the scanner reports `Py_BEGIN_CRITICAL_SECTION: 9` against `Py_END_CRITICAL_SECTION: 8`; on `clinic/bytearrayobject.c.h`, `34` against `33`. The extra BEGIN in each is line 10:

```c
#include "pycore_critical_section.h"// Py_BEGIN_CRITICAL_SECTION()
```

The **pairing logic is correct** — 0 findings, 8 and 33 CS functions, matching my walker exactly — but `vocabulary_counts` is computed over unstripped source. A reader who does the obvious sanity check (`BEGIN == END`?) sees a phantom unbalanced BEGIN in every clinic-emitted file in CPython. Cheap fix in §7.2.

---

# 2. Task (a) — the 41 clinic-only regions and the 18 pre-lock windows

## 2.1 The regions: 41 of 41, leak-free, and structurally incapable of leaking

Every one of the 41 has a **one-statement body**:

```c
    Py_BEGIN_CRITICAL_SECTION(self);
    return_value = list_insert_impl((PyListObject *)self, index, object);
    Py_END_CRITICAL_SECTION();
```

with the `exit:` label outside the region and every `goto exit` **above** the `BEGIN`. Both FIX shapes this agent models — `critical_section_missing_end` and `critical_section_end_on_error` — are impossible in that shape: there is no branch to leave on. This independently reproduces `error-path-analyzer` §2.5 from a different direction (token walker vs. clinic parse) and matches it exactly, 41/41.

The 12 non-trivial regions are all in the `.c` files. I read every one (`scratchpad/dump_cs_regions.py`). Two contain a `goto`, and both are the "silent by design" internal-jump case named in my agent contract:

| region | exit statement | label | verdict |
|---|---|---|---|
| `listobject.c:339-349` `list_item_impl` | `goto exit;` (:345) | `exit:` at **:348, inside** | internal jump — END still runs |
| `listobject.c:464-477` `list_ass_item_lock_held`'s caller | `goto end;` (:470) | `end:;` at **:476, inside** | internal jump |

The other ten assign to a result variable and fall through to the END. **The slice contains zero `return` statements between a BEGIN and its END** — CPython's canonical `res = ...; END; return res;` idiom, applied without exception.

## 2.2 The 18 pre-lock windows: swept, and the answer to the literal question is "yes, but harmlessly"

The question was: *does anything in the pre-lock window mutate state the lock is supposed to protect?* The converters (`_PyNumber_Index`, `_PyEval_SliceIndex`, `PyObject_GetBuffer`) operate on **arguments**, but the arbitrary Python they run can close over the receiver and mutate it. So the real question is whether the impl re-derives from current state under the lock.

`repro/lock_prelock_window_sweep.py` — **27 probe points** (18 wrappers; the `_PyEval_SliceIndex` family has two windows each), every one called with a converter whose `__index__`/`__buffer__` empties or regrows the receiver first:

| build | probes | crashes | asserts | wrong answers |
|---|---|---|---|---|
| `release-gil-nojit` | 27 | 0 | 0 | 0 |
| `debug-gil-nojit` | 27 | 0 | 0 | 0 |

Every impl re-derives correctly: `b.pop(Idx→clear)` → `IndexError: pop from empty bytearray`; `l.pop(Idx→clear)` → `IndexError: pop from empty list`; `b.index(...)` → `ValueError: subsection not found`; `find/rfind/count/startswith/endswith` → `-1`/`0`/`False`; `b.resize(Idx→clear, 4096)` → `len == 4096`. **0 defects across 27 windows × 2 builds.**

**And that is not where the FT hazard is.** The pre-lock window is the *safe* direction (converter before lock — the rule `_PyList_BinarySlice:725-733` states in prose). The dangerous window is **inside** the region, at the point where a nested lock acquisition blocks and `_PyCriticalSection_SuspendAll` releases the outer section. That is the mechanism behind both findings in §4, and it is invisible from the clinic header.

## 2.3 Using Group B's measured semantic, not re-deriving it

Per instruction I take `gil-discipline-checker` G3 as given: on FT the section excludes across a **busy** callback (mutator waits the full 1.200 s) and not across a **blocking** one (0.000 s), because `_PyThreadState_Detach` → `_PyCriticalSection_SuspendAll`. I add the third case they did not need and I did:

> **Blocking on another object's `ob_mutex` is itself a detaching operation.** `PyMutex_Lock` → `_PyMutex_LockTimed(m, -1, _PY_LOCK_DETACH)` (`Python/lock.c:654-657`) → `_PyParkingLot_Park(..., detach=1)` (`Python/lock.c:138`) → `PyEval_ReleaseThread(tstate)` (`Python/parking_lot.c:329-334`). So a *contended nested critical section* suspends the outer one just as surely as an `Event.wait()` does — with no user callback anywhere in the picture.

That single fact is what turns "nested locking, probably fine" into two reproduced bugs.

---

# 3. Task (b) — the per-type coverage tables

`scratchpad/coverage_table.py` computes the mechanical parts; every non-clinic entry was then read by hand.

```
type                clinic  clinicCS  nonclinic  nonclinicCS  nonclinicATOMIC  nonclinicNONE
PyList_Type             13         8          2            0                0              2
PyBytes_Type            25         0         14            0                0             14
PyByteArray_Type        37        33         19           18                1              0
```

## 3.1 `list` — complete, 0 gaps, 32 entry points

| entry point | mechanism | verdict |
|---|---|---|
| `append` `insert` `pop` `remove` `reverse` `sort` `clear` `copy` (8) | clinic `Py_BEGIN_CRITICAL_SECTION(self)` | ✅ |
| `extend` (clinic, no CS) | `_list_extend:1457-1510` — **all 10 branches lock**: 1 × CS(self) for the self-alias, 6 × **CS2(self, other)** for list/set/dict/dictkeys/dictvalues/dictitems, 3 × CS(self) | ✅ |
| `index` `count` (clinic, no CS) | lock-free `list_get_item_ref` (atomic `ob_item` load, `list_capacity` bound, `_Py_TryXGetRef`, fallback to the locking `list_item_impl:339`) | ✅ |
| `__init__` (clinic, no CS) | opens **its own** CS at `:3534` for `list_clear`, then `_list_extend` locks | ✅ — *the guarded twin of CPY-0187* |
| `__sizeof__` (clinic, no CS) | `_Py_atomic_load_ptr(&self->ob_item)` + `list_capacity`; safe because `ob_item` is freed through QSBR (`_PyMem_FreeDelayed`, `:65`) | ✅ (init-bypass-checker's finding H; not re-litigated) |
| `__reversed__` (clinic, no CS) | `PyList_GET_SIZE` = `_Py_atomic_load_ssize_relaxed`; `listreviter_next` re-bounds-checks | ✅ |
| `__getitem__` (non-clinic, `METH_O\|METH_COEXIST`) | → `list_item` (lock-free accessor) or `list_slice_subscript` (CS at `:735`/`:3655`) | ✅ |
| `__class_getitem__` | `Py_GenericAlias`, no `self` state | n/a |
| `sq_length` / `mp_length` | `PyList_GET_SIZE`, atomic | ✅ |
| `sq_item` | `list_item` | ✅ |
| `sq_ass_item` | `Py_BEGIN_CRITICAL_SECTION(a)` `:1165` | ✅ |
| `sq_contains` | `list_get_item_ref` | ✅ |
| `sq_concat` (`+`) | **CS2(a, b)** `:813` | ✅ |
| `sq_repeat` (`*`) | CS(a) `:866` | ✅ |
| `sq_inplace_concat` (`+=`) | `_list_extend` | ✅ |
| `sq_inplace_repeat` (`*=`) | CS(self) `:1126` | ✅ |
| `mp_subscript` | as `__getitem__` | ✅ |
| `mp_ass_subscript` | **CS2(self, value)** when value is an exact list `:3901`, else CS(self) `:3907` | ✅ |
| `tp_repr` | CS(v) `:643` | ✅ |
| `tp_richcompare` | **CS2(v, w)** `:3504` | ✅ |
| `tp_iter` | writes only the fresh iterator | ✅ |
| `_tp_iteritem` | `list_get_item_ref` | ✅ |
| `tp_init` | as `__init__` | ✅ |
| `tp_vectorcall` | `list___init___impl` on a not-yet-published list | ✅ |
| `tp_traverse` / `tp_clear` | GC; `_PyCriticalSection_BeginSlow:50` skips locking outright when the world is stopped | ✅ |
| `tp_dealloc` | refcount 0 | ✅ |
| `PyListIter_Type` ×4, `PyListRevIter_Type` ×4 | `FT_ATOMIC_LOAD/STORE_SSIZE_RELAXED(it_index)`; `it_seq` dropped only inside `#ifndef Py_GIL_DISABLED` (`:4078`, `:4237`) | ✅ |

**`list` has no CPY-0187.** The 2024 free-threading list retrofit reached every entry point. Two cosmetic asymmetries, both already named by Group A/B and neither actionable on its own: `list_remove_impl:3409` uses plain `Py_SIZE` where the accessor path uses the relaxed-atomic `PyList_GET_SIZE`, and `list_clear_impl` writes `a->allocated = 0` plainly at `:884` where `:3190` uses an atomic — both under the object's lock, so equivalent.

*One thing I explicitly checked and did not find:* `listreviter_len:4245` is textually the G1 shape — `if (it->it_seq == NULL || PyList_GET_SIZE(it->it_seq) < len)` tests the pointer and dereferences it in a separate expression. It is **safe**, because `listreviter_next` NULLs `it_seq` only inside `#ifndef Py_GIL_DISABLED`, so on a free-threaded build the field is never NULL. Group B's proposed suppression step 2 handles it correctly. Recorded so it is not re-opened.

## 3.2 `bytes` — immutable, and the only gap is the iterator

Post-publication mutable state in `Objects/bytesobject.c`, enumerated exhaustively:

| state | accessors | mechanism | verdict |
|---|---|---|---|
| `ob_sval`, `ob_size` | everything | **immutable after publication** | ✅ by construction |
| `ob_shash` | `set_ob_shash:52-63`, `get_ob_shash:65-76` | `_Py_atomic_store/load_ssize_relaxed` under `Py_GIL_DISABLED` | ✅ |
| in-place resize | `_PyBytes_Resize:3370` | gated on `_PyObject_IsUniquelyReferenced(v)` — on FT that means `ob_tid == this thread`, so no other thread can hold it; every other path copies | ✅ real negative |
| `PyBytesWriter` | 12 public functions | private per-call struct | ✅ |
| **`striter` `it_index`, `it_seq`** | `striter_next:3446-3452`, `striter_len:3461`, `striter_reduce:3478`, `striter_setstate:3494` | **5 plain accesses, 0 atomics, 0 critical sections** | ❌ **= CPY-0182 + G1's three siblings** |

All 25 clinic wrappers and all 14 non-clinic `PyMethodDef` entries take zero locks, and that is **correct**: the 14 non-clinic entries register `stringlib_capitalize`, `stringlib_isalnum`, … **directly**, because the data they read cannot move.

**The structural contrast that is the whole `bytes`/`bytearray` FT story in one line:** the same `Objects/stringlib/{ctype,transmogrify}.h` source is textually included by both files (the mapper's §a point 5), and

- `bytes_methods[]` registers **14 `stringlib_*` functions raw**, with no lock — correct, `bytes` is immutable;
- `bytearray_methods[]` registers **19 `bytearray_*` functions**, of which **18 are hand-written four-line wrappers** whose entire body is `Py_BEGIN_CRITICAL_SECTION(self); ret = stringlib_X(self, …); Py_END_CRITICAL_SECTION(); return ret;` — e.g. `bytearray_lower:2446`, `bytearray_center:2144`, `bytearray_isalnum:2029`.

That asymmetry is 18 of `bytearrayobject.c`'s 34 regions. **A clinic-only coverage rule cannot see any of them**, and therefore cannot tell "this type has a locking convention and one member escaped it" (bytearray) from "this type has no convention at all" (bytes).

## 3.3 `bytearray` — the corrected denominator, and why it strengthens CPY-0187

| bucket | count | locked | mechanism of the rest |
|---|---|---|---|
| clinic `METHODDEF`s | 37 | 33 | `clear` delegates to the locking `PyByteArray_Resize:298`; `__sizeof__` reads only `FT_ATOMIC_LOAD_SSIZE_RELAXED(ob_alloc)`; `fromhex`/`maketrans` have no `self` |
| clinic `tp_init` wrapper | 1 | **0** | **neither — CPY-0187** |
| non-clinic `PyMethodDef` | 19 | **18** | `__alloc__` → `FT_ATOMIC_LOAD_SSIZE_RELAXED(self->ob_alloc)` |
| type slots | 18 distinct fns | 12 | `str`→`repr` (CS); `richcompare` and `sq_concat` are **not** locked and are correct by the *buffer pin* (both operands exported, neither resized); `length` is `PyByteArray_GET_SIZE` (relaxed atomic); `iter` writes only the fresh iterator; `dealloc` at refcount 0 |
| `PyByteArrayIter_Type` | 4 | — | `FT_ATOMIC_*` index, `it_seq` never NULLed on FT (`:2997`) |

So the gh-129107 / PR #129108 sweep installed **51 critical sections across two mechanisms** (33 clinic annotations + 18 hand-written wrappers) and left exactly **one** entry point unlocked. **Group B's "33 of 36" is 92% of a denominator that is itself 64% of the real method surface.** The honest framing of CPY-0187 is stronger: *not* "the sweep skipped 3 of 36", but *"the sweep hand-installed 51 locks and missed one."*

**Correction to a sibling claim (informed rule 3).** `gil-discipline-checker` §4 lists 14 bytearray slots and says "all 14 slots lock", naming `bytearray_richcompare` among them. Three of the slots it names do **not** take a critical section: `bytearray_richcompare:1151` (no CS anywhere in its body), `PyByteArray_Concat:304` (no CS), and `bytearray_str:1139` (delegates). All three are nevertheless correct — the first two because both operands are pinned by live `Py_buffer` views, the third by delegation. The verdict stands; the mechanism attributed to it does not. This matters because §4 of this report turns on exactly which sites are protected by a *lock* and which by the *buffer pin*: `PyByteArray_Concat` and `bytearray_richcompare` are safe under the pin because they **only read and build a fresh result**, and that argument does not extend to `bytearray_iconcat`, which **resizes `self`**.

---

# 4. Findings

## [FIX] L1 — `bytearray.__iadd__` and `bytearray.extend()` take a second bytearray's lock nested instead of `Py_BEGIN_CRITICAL_SECTION2`, and make each other fail: **45% spurious `BufferError` on free-threaded builds** (`Objects/bytearrayobject.c:382`, `:2186`) · **NOVEL, REPRODUCED, LIVE ON MAIN**

### What

```c
382  bytearray_iconcat(PyObject *op, PyObject *other)
383  {
384      PyObject *ret;
385      Py_BEGIN_CRITICAL_SECTION(op);              /* ONE object */
386      ret = bytearray_iconcat_lock_held(op, other);
   356      PyObject_GetBuffer(other, &vo, PyBUF_SIMPLE)
   →  77      bytearray_getbuffer: Py_BEGIN_CRITICAL_SECTION(self)   /* the SECOND object */
   →  69        obj->ob_exports++
   ←  80      Py_END_CRITICAL_SECTION()   /* released immediately; the export stays up */
   367      bytearray_resize_lock_held(self, size + vo.len)
   →  235      _canresize(self)  ->  reads SELF->ob_exports
```

The inner section is released the moment `getbuffer` returns, but the **export is not**. So thread B's transient export on `b1` — taken during its own `b2 += b1` — is visible to thread A's `_canresize(b1)`. Two threads running `b1 += b2` and `b2 += b1` therefore make **each other** raise `BufferError: Existing exports of data: object cannot be re-sized`, an exception neither operation can raise single-threaded and which cannot occur under the GIL.

`bytearray.extend(other_bytearray)` is the identical shape one level down: `bytearray_extend_impl:2186` → `bytearray_setslice:666` `PyObject_GetBuffer(values)` → same nested `bytearray_getbuffer`.

### Guarded twin — same file, same upstream sweep, same commit

**gh-130227 / PR #130227, "gh-129107: two cases where second operand needs lock", merged 2025-02-27**, `+33/-6`, converted exactly two functions to the conditional two-object form:

```c
893      if (values != NULL && PyByteArray_Check(values)) {
894          Py_BEGIN_CRITICAL_SECTION2(op, values);        /* bytearray_ass_subscript */
2865     if (PyByteArray_Check(w)) {
2866         Py_BEGIN_CRITICAL_SECTION2(v, w);              /* bytearray_mod          */
```

Its PR body states the review it performed:

> *"Identified three cases where a second operand needs to be locked. Also double checked other two operand functions: `PyByteArray_Concat`, `bytearray_richcompare`. But those "lock" both self and their counterparts purely through the buffer mechanism so there are no issues in those."*

`bytearray_iconcat` and `bytearray_extend_impl` appear on **neither** list. And the buffer-mechanism argument that correctly clears `PyByteArray_Concat` and `bytearray_richcompare` does not transfer to them, because those two only read while these two **resize `self`** — which is exactly the operation `ob_exports` blocks.

### The test that would have caught it is in the same PR

`Lib/test/test_bytes.py`'s free-threading test distinguishes two patterns:

```python
def extend(b, a):          # MODIFIES!            <-- pre-existing
    c = bytearray(b'0' * 0x400000)                #  c is PRIVATE to each thread
    b.wait(); a.extend(c)
def iconcat(b, a):         # MODIFIES!            <-- pre-existing
    c = bytearray(b'0' * 0x400000)                #  c is PRIVATE to each thread
    b.wait(); a += c
def ass_subscript2(b, a, c):  # MODIFIES!         <-- ADDED BY PR #130227
    b.wait(); a[:] = c                            #  c is SHARED across threads
def mod2(b, a, c):                                <-- ADDED BY PR #130227
    b.wait(); d = a % c                           #  c is SHARED across threads
```

PR #130227 **invented the shared-second-operand pattern** (`clear2` plus a `c` passed through `check(...)`) and applied it to the two functions it fixed. `extend` and `iconcat` kept their private `c`, so no thread ever contends the second operand and the window never opens. This is the gh-129107→CPY-0187 story one function over: *the operand the test kept private is the operand that did not get the lock.*

### Measured — `repro/lock_iconcat_spurious_buffererror.py`, 4 threads, 4 s per cell

Rate, not presence: threads keep running after a failure instead of dying on the first one.

| build | `iconcat`<br>`b1 += b2` ‖ `b2 += b1` | `extend`<br>`b1.extend(b2)` ‖ … | `setslice` **CS2 twin**<br>`b1[len:] = b2` ‖ … | `iconcat_solo`<br>1 thread | `iconcat_disjoint`<br>per-thread pairs |
|---|---|---|---|---|---|
| `release-ft-nojit` | **45.22 %**<br>551,495 / 1,219,603 | **44.14 %**<br>542,643 / 1,229,270 | **0** / 13,327,713 | **0** / 26,263,722 | **0** / 68,149,173 |
| `debug-ft-nojit` | **43.71 %**<br>302,552 / 692,239 | *SIGABRT — see L2* | **0** / 6,898,274 | **0** / 6,442,149 | **0** / 19,243,844 |
| `release-gil-nojit` | 0 / 32,441,110 | 0 / 34,575,260 | 0 / 30,645,550 | 0 / 35,351,611 | 0 / 33,540,401 |
| `debug-gil-nojit` | 0 / 6,240,901 | 0 / 6,424,069 | 0 / 6,824,949 | 0 / 6,197,738 | 0 / 4,678,123 |

**Four controls, every one clean.** `setslice` is the sharpest: `b1[len(b1):] = b2` and `b1.extend(b2)` move the same bytes through the **same callee** (`bytearray_setslice` → `bytearray_setslice_linear`); the only difference is that `bytearray_ass_subscript` holds `Py_BEGIN_CRITICAL_SECTION2(op, values)` and `bytearray_extend` holds one lock. 0% vs 44%.

### Live on main

`repos/python/cpython/contents/Objects/bytearrayobject.c`, fetched 2026-07-26: `bytearray_iconcat` at `:390-397` still takes a single `Py_BEGIN_CRITICAL_SECTION(op)`; `bytearray_extend_impl:2212` still calls `bytearray_setslice` under the clinic's single section; the file still has exactly **2** `Py_BEGIN_CRITICAL_SECTION2` sites, both from PR #130227.

### Fix

Mirror `bytearray_ass_subscript:893` exactly:

```c
static PyObject *
bytearray_iconcat(PyObject *op, PyObject *other)
{
    PyObject *ret;
    if (PyByteArray_Check(other)) {
        Py_BEGIN_CRITICAL_SECTION2(op, other);
        ret = bytearray_iconcat_lock_held(op, other);
        Py_END_CRITICAL_SECTION2();
    }
    else {
        Py_BEGIN_CRITICAL_SECTION(op);
        ret = bytearray_iconcat_lock_held(op, other);
        Py_END_CRITICAL_SECTION();
    }
    return ret;
}
```

`Py_BEGIN_CRITICAL_SECTION2` sorts by mutex address (`pycore_critical_section.h:161-168`) and handles `m1 == m2` (`:153-158`), so `b += b` needs no special case. The nested `getbuffer` then becomes a no-op acquire: `_PyCriticalSection_BeginSlow:36-44` skips locking when the top-most section is a two-mutex section holding that mutex.

For `bytearray.extend`, Argument Clinic already supports the two-object form — `@critical_section so other` is used four times in `Objects/setobject.c` and `Tools/clinic/libclinic/clanguage.py:390` emits `Py_BEGIN_CRITICAL_SECTION2`. The narrower change, given `iterable_of_ints` may be any object, is to move the second acquire into `bytearray_extend_impl`'s buffer fast path with the same `PyByteArray_Check` guard.

Regression test: add `extend2(b, a, c)` and `iconcat2(b, a, c)` alongside `ass_subscript2`/`mod2` and register them with `check([clear2] + [extend2] * 10, None, bytearray(b'0' * 0x400000))`.

### Prior art: none

`gh api -X GET search/issues` for `bytearray iadd BufferError free-threading`, `bytearray __iadd__ thread BufferError`, `bytearray critical_section2 iconcat`, `spurious BufferError free threaded bytearray`, `bytearray inplace concat lock second operand` → 0 results. `bytearray_iconcat critical section` returns only gh-129107 / #129108 / #130227 — the sweep and the follow-up that missed it. Nothing in `cpython-review-findings` or `cpython-tsan-findings`.

### What I did not measure

I did not build a *deterministic* (non-racing) demonstrator; the window is the few instructions between the inner `Py_END_CRITICAL_SECTION` and `_canresize`, and I found no way to widen it from Python without an adversarial exporter, which would change the finding into a different one. The statistical case rests on 45% against four zero controls totalling ~140M iterations. Falsifier: a run where `iconcat_disjoint` also produces `BufferError`, which would mean the cause is concurrency per se rather than contention on the shared pair.

---

## [FIX] L2 — the same suspend window makes gh-153578 reachable with **no adversarial `__buffer__` at all**: SIGABRT 3/3 on `debug-ft-nojit`, uninitialised bytes to Python on `release-ft-nojit` (`Objects/bytearrayobject.c:666 → 673-678 → 556`) · **ESCALATION of a known open issue**

Per informed rule 1 I do not re-derive gh-153578 — `refcount-auditor` and `include-graph-mapper` A21 already confirmed it, and PR #153579 is open and unmerged at the review ref. **What is new is the reachability.**

### Mechanism — and it is the lock, not the callback

```c
2186  bytearray_extend_impl: bytearray_setslice(self, Py_SIZE(self), Py_SIZE(self), iterable)
                                               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  lo and hi evaluated HERE
 666  bytearray_setslice:    PyObject_GetBuffer(values, &vbytes, PyBUF_SIMPLE)
  77    -> bytearray_getbuffer: Py_BEGIN_CRITICAL_SECTION(values)
         contended -> PyMutex_Lock -> _PY_LOCK_DETACH -> _PyParkingLot_Park(detach=1)
         -> PyEval_ReleaseThread -> _PyCriticalSection_SuspendAll
         => SELF'S SECTION IS RELEASED; another thread shrinks self
 673-678  if (lo < 0) lo = 0;  if (hi < lo) hi = lo;  if (hi > Py_SIZE(self)) hi = Py_SIZE(self);
          /* hi is re-clamped to the NEW size.  lo is NOT. */
 556  bytearray_setslice_linear: avail = hi - lo;      ->  NEGATIVE
 560                             assert(avail >= 0);   ->  SIGABRT on a debug build
```

`lo == hi == Py_SIZE(self)` on entry, and the `hi` clamp can only *lower* `hi`. So `lo > hi` requires `Py_SIZE(self)` to have decreased between the argument evaluation and the clamp — which under a held lock is impossible **unless the lock was dropped**. `PyObject_GetBuffer` is the only call in between. The mechanism is therefore closed by construction, and the `extend_disjoint` control (0 of 9,596,781, no shared pair ⇒ no contention ⇒ `PyMutex_LockFast` never fails ⇒ no park, no detach) closes it empirically.

With the assert compiled out, `growth = bytes_len - avail > bytes_len`, self is grown to `lo + bytes_len`, `memmove(buf + lo + bytes_len, buf + hi, Py_SIZE(self) - lo - bytes_len)` moves **zero** bytes, and `memcpy(buf + lo, bytes, bytes_len)` writes only the tail — so the byte range **`[hi, lo)` is never written** and is handed to Python.

### Measured — `repro/lock_extend_ft_uninit_disclosure.py`

Only the byte values `0x41` and `0x42` are ever written by the program; any other byte in a result is memory the program never wrote.

| build | `extend` (the finding) | `setslice` (CS2 twin) | `extend_solo` | `extend_disjoint` |
|---|---|---|---|---|
| `debug-ft-nojit` | **SIGABRT 3/3** — `bytearrayobject.c:560: Assertion 'avail >= 0' failed` | clean, 0 foreign / 24,848 | 0 / 1,141,438 | 0 / 3,154,790 |
| `release-ft-nojit` | **DISCLOSURE — 119 foreign-byte results of 335 successful calls (35.5%)**, sample byte `0x00` | clean, 0 / 1,745 | 0 / 2,516,614 | 0 / 9,596,781 |
| `release-ft-nojit-asan-mitrack` | no ASan report, 0/3 | 0/3 | — | — |
| `debug-gil-nojit` | clean, 0 / 582,858 | 0 / 492,121 | 0 / 1,392,056 | 0 / 1,176,647 |
| `release-gil-nojit` | clean, 0 / 220,411 | 0 / 484,944 | 0 / 2,714,468 | 0 / 2,323,147 |

**ASan's silence is a confirmation, not a gap.** `release-ft-nojit-asan-mitrack` (the mimalloc-track build, which *does* shadow the object heap) reports nothing — exactly as the mechanism predicts, because the unwritten `[hi, lo)` region is *inside* the over-grown allocation. The defect is uninitialised-read, not out-of-bounds.

**Honest limit on severity:** the foreign bytes I observed were `0x00` — freshly zeroed pages, not recycled content. I did not construct a case where the hole exposes attacker-interesting stale data. The claim I am making is "memory the program never wrote is handed to Python", which is what I measured; "heap disclosure of secrets" is not.

### Relationship to PR #153579 — it fixes L2 and **not** L1

PR #153579 (open, unmerged) is `+3/-0`:

```c
+    // gh-153578: __buffer__() may have resized self; re-clamp both bounds.
     if (lo < 0)  lo = 0;
+    else if (lo > Py_SIZE(self))  lo = Py_SIZE(self);
```

That clamp closes L2 on both routes. It does **nothing** about L1 — the spurious `BufferError` comes from `_canresize` observing another thread's transient export, several frames away. And note the comment: the issue and the fix both attribute the window to `__buffer__`. On a free-threaded build **the window opens with no `__buffer__` in the program at all**; the second bytearray's own `bf_getbuffer` slot is enough. That is worth adding to gh-153578 whether or not L1 is filed separately.

---

## [ACCEPTABLE] L3 — nested two-object critical sections in this slice do **not** deadlock, and the rule's `CONSIDER` default is miscalibrated for CPython

My agent contract classifies `nested_critical_sections` as CONSIDER on deadlock grounds. For CPython's own primitive that premise is false, and I measured it rather than arguing it.

**Mechanism.** `Include/critical_section.h:8-22` states the design; `_PyCriticalSection_BeginMutex` (`pycore_critical_section.h:107-118`) tries `PyMutex_LockFast` and on failure calls `_PyCriticalSection_BeginSlow`, which either skips (same mutex as the top-most section, `Python/critical_section.c:29-45`), skips (world stopped, `:50-53`), or calls `PyMutex_Lock` — and `PyMutex_Lock` carries `_PY_LOCK_DETACH`, so parking releases every section this thread holds. A thread can therefore never hold lock A while blocking forever on lock B.

**Measured — `repro/lock_order_inversion.py` via `repro/lock_matrix_driver.py`, faulthandler dump at 25–30 s, 4 builds:**

| scenario | what it inverts | debug-ft | release-ft | debug-gil | release-gil |
|---|---|---|---|---|---|
| `list_usercode_inversion` | `l1.extend(gen(l2))` ‖ `l2.extend(gen(l1))`, where `gen` appends to the other list **inside** `list_extend_iter_lock_held:1318` | 685,304 ✓ | 996,429 ✓ | 1,287,384 ✓ | 5,544,550 ✓ |
| `ba_usercode_inversion` | same via `bytearray_extend_impl:2216` `PyIter_Next` | 638,178 ✓ | 885,349 ✓ | 1,059,674 ✓ | 5,244,908 ✓ |
| `ba_iconcat_inversion` | CS(b1)→CS(b2) ‖ CS(b2)→CS(b1) | 4,076,857 ✓ | 27,928,014 ✓ | 5,829,642 ✓ | 30,696,369 ✓ |
| `list_extend_inversion` | via `list_get_item_ref`'s `list_item_impl:339` slow path | 715,038 ✓ | 1,238,836 ✓ | 744,406 ✓ | 2,853,325 ✓ |
| `cs2_vs_nested` | a CS2 acquirer racing a nested single-lock acquirer, same pair | 155,805 ✓ | 310,170 ✓ | 1,265,342 ✓ | 3,590,328 ✓ |
| `ba_cs2_vs_nested` | same, bytearray | 4,951,649 ✓ | 16,583,241 ✓ | 4,223,645 ✓ | 26,583,300 ✓ |
| `chain3` | a **three-object cycle** 1→2, 2→3, 3→1 | 200,409 ✓ | 269,443 ✓ | 884,173 ✓ | 2,483,529 ✓ |
| `self_alias` | `b += b`, `b[0:0] = b`, `b.extend(iter(b))`, `l[0:0] = l`, `l.extend(l)` | 7,823,917 ✓ | 23,422,229 ✓ | 4,674,992 ✓ | 17,805,668 ✓ |
| `solo_control` / `solo_control_list` | 1 thread | ✓ | ✓ | ✓ | ✓ |

**40 cells, 0 deadlocks, ~250 million operations.** The `list_usercode_inversion` scenario is the general form and needs four lines of Python: a generator passed to `l1.extend()` that appends to `l2` runs **inside** `CS(l1)` and takes `CS(l2)`, and the mirror thread does the reverse.

**So the CONSIDER should be re-aimed, not dropped.** In CPython the cost of nesting is not a hang — it is that the outer section is *silently released*, which is exactly what produces L1 and L2. A finding of this shape should read *"the invariant this section protects does not survive the nested acquire"*, and should be judged by what the enclosing function does after the nested call, not by lock ordering. The one genuine deadlock shape in the tree is nesting a suspendable critical section inside a **non**-suspendable lock — `LOCK_KEYS`'s `_Py_LOCK_DONT_DETACH` in `dictobject.c`, i.e. CPY-0096 / CPY-0107 — and **there is none of that in this slice** (0 `PyMutex` acquires, §6).

## [ACCEPTABLE] L4 — two harness artefacts, separated by measurement rather than assumed

Recorded because both produced a *confident wrong answer* first, which is what makes them worth naming.

- **`l.extend(iter(l))` does not terminate.** My first `self_alias` probe hung on all four builds with the faulthandler dump naming `li` and `li_slice`. That reads exactly like a deadlock. `repro/lock_self_alias_isolate.py`, **single-threaded**: `li_extend_iter` times out at 10 s on `release-ft-nojit` **and** `release-gil-nojit`, while `li_extend_list`, `li_slice`, `ba_slice` and `ba_extend_iter` all complete. `list_extend_iter_lock_held` appends to the same list its iterator is walking, so the cursor never reaches the end — a non-termination, not a lock defect. This is the same trap `gil-discipline-checker` §G4 records for `list.remove`; it is worth a taxonomy entry.
- **`b += b` raises `BufferError`.** `bytearray_iconcat_lock_held:356` calls `PyObject_GetBuffer(other)` where `other is self`, bumping `self->ob_exports` to 1, so `_canresize` refuses. Reproduced on all four builds — **and on RustPython 0.5.0**, which mirrors CPython semantics, so it is long-standing intended behaviour, not a regression. Out of this agent's remit; recorded so it is not re-derived. (`b.extend(b)`, `b[len(b):] = b` and `b *= 2` all succeed — `bytearray_setslice:643` copies when `values == self`.)

---

# 5. Classes bounded (with denominators)

| class | denominator | verdict |
|---|---|---|
| **`critical_section_missing_end`** — a BEGIN with no matching END | **108 regions** / 95 functions / 216 macro tokens, across **7** files including all three `clinic/*.c.h` | **0.** Stack-matched with family checking; no unclosed BEGIN, no orphan END |
| **`critical_section_end_on_error`** — a `return`/`goto` out of a region without releasing | same 108 regions; 96 have a 1-statement body, 12 hand-read | **0.** The only two `goto`s (`listobject.c:345`, `:470`) target labels **inside** their own region. **Zero `return` statements exist between a BEGIN and its END anywhere in the slice** |
| **Family mismatch** — `SECTION2` closed by `SECTION` or vice versa | 12 `…SECTION2` regions, 96 `…SECTION` regions | **0** |
| **`nested_critical_sections`** — two objects locked at once via two single-object begins | 3 static sites (`bytearray_iconcat:382`, `bytearray_extend_impl:2186`→`setslice:666`, plus the general user-callback form reachable from every one of the 108 regions) | **2 FIX (L1, L2)** on invariant grounds; **0 deadlocks** across 40 cells / ~250M ops — see L3 for why those are different questions |
| **`mutex_leak_on_error` / `mutex_missing_unlock`** | **0** `PyMutex_Lock`/`LockFlags`/`Unlock`, 0 `LOCK_WEAKREFS*`, 0 `Py_BEGIN_CRITICAL_SECTION_MUTEX` across all 7 files | **structural zero — stated, not assumed.** §6 |
| **Clinic-emitted regions** (the 38%) | **41** (8 + 33) | **0 leaks.** All 41 are the trivial `BEGIN; return_value = impl(…); END;` wrapper with `exit:` outside |
| **Pre-lock converter windows** | **18 wrappers → 27 probe points** × 2 builds | **0 defects.** Every impl re-derives from post-mutation state; the two known stale-value bugs are elsewhere (gh-153570 inside the impl, gh-153578 in the caller's argument list) |
| **Per-type CS coverage, `PyList_Type`** | **32 entry points** (13 clinic METHODDEF + `tp_init` + 2 non-clinic + 17 distinct slot fns, `list_subscript` shared) **+ 8 iterator entry points** | **0 gaps.** Every mutator locks; every unlocked reader uses `list_get_item_ref`, a relaxed atomic, or QSBR |
| **Per-type CS coverage, `PyBytes_Type`** | **25 clinic + 14 non-clinic + 15 distinct slot fns**; mutable state = 3 kinds | **0 gaps for the type** (immutable; `ob_shash` atomic; `_PyBytes_Resize` gated on unique reference). **4 gaps in `PyBytesIter_Type`** = CPY-0182 + G1's three siblings — confirmed present, not re-litigated |
| **Per-type CS coverage, `PyByteArray_Type`** | **56 methods** (37 clinic METHODDEF + 19 non-clinic) + `tp_init` + 18 distinct slot fns | **1 gap** = `bytearray___init___impl` (**CPY-0187**), against **51 hand-installed critical sections** and 2 atomic-only entry points |
| **Deadlock via lock-order inversion** | 10 scenarios × 4 builds = **40 cells**, incl. a 3-object cycle and a CS2-vs-nested race | **0.** Structurally impossible: `PyMutex_Lock` uses `_PY_LOCK_DETACH` |

**Deliberately not re-litigated:** CPY-0180, CPY-0182, CPY-0186, CPY-0187, CPY-0014, gh-153570, gh-153419/#153498, F1–F11, G1–G6. gh-153578 is confirmed and **escalated** (L2), not re-derived.

---

# 6. Task (e) — `mutex_functions: 0`, stated

| token | occurrences across all 7 files |
|---|---|
| `PyMutex_Lock` | **0** |
| `PyMutex_LockFlags` | **0** |
| `PyMutex_Unlock` | **0** |
| `LOCK_WEAKREFS` / `LOCK_WEAKREFS_FOR_WR` / `UNLOCK_WEAKREFS*` | **0** |
| `Py_BEGIN_CRITICAL_SECTION_MUTEX` | **0** |
| `_PyMutex_*` | **0** |
| `PyMutex_IsLocked` | **1** |

The single `PyMutex` token in the entire scope is `Objects/listobject.c:82`:

```c
assert(Py_REFCNT(self) == 1 || PyMutex_IsLocked(&_PyObject_CAST(self)->ob_mutex));
```

inside `ensure_shared_on_resize` — a **read-only assertion** that the caller already holds the object's lock, not an acquire. So `mutex_functions: 0` is a genuine structural zero: this scope's entire concurrency control is per-object critical sections and `FT_ATOMIC_*`. The `PyMutex` family's two rules have nothing to check here, and — per L3 — the absence of any `_Py_LOCK_DONT_DETACH` lock is also why the one real deadlock shape in CPython (CPY-0096 / CPY-0107) cannot occur in this slice.

---

# 7. Toolkit assessment

## 7.1 [HIGH VALUE] The 38% is a scope bug, and the fix is one line in the manifest

Three agents have now reported the 41 clinic regions as a scanner gap. **It is not one.** The scanner reads `.c.h` files correctly (§1.1). What is missing is that `preflight/slice_files.txt` lists four `.c` files and nothing pulls in their generated siblings.

> **Proposal.** In `make_slice_context.py`, when a slice file is `<dir>/<name>.c`, automatically append `<dir>/clinic/<name>.c.h` to the scanner scope if it exists — as *scanner scope*, not as *review scope*, so it feeds denominators without expanding what agents must triage. Emit the added paths in the envelope as `generated_siblings_included: [...]`.

This is worth more than the finding count suggests. Argument Clinic emits critical sections into `clinic/*.c.h` for **every** `@critical_section` annotation in CPython. Any slice over `Objects/` or `Modules/` that lists only `.c` files has the same shortfall, and on mod-io it was 87 of 95 (92%) rather than 41 of 95 (43%). The cost of the fix is a `Path` join.

Second-order: it also fixes the `functions_analyzed` reading. `scan_lock_discipline` reports 331 functions for four files; adding the three headers adds 78 more (14 + 38 + 26), which is where 43% of the slice's critical-section *functions* live.

## 7.2 [LOW, but free] `vocabulary_counts` counts a comment

`scan_lock_discipline` reports `Py_BEGIN_CRITICAL_SECTION: 9 / Py_END_CRITICAL_SECTION: 8` for `clinic/listobject.c.h` and `34 / 33` for `clinic/bytearrayobject.c.h`. The extras are the trailing comment on the `#include` line that Argument Clinic emits into **every** file it generates a critical section for:

```c
#include "pycore_critical_section.h"// Py_BEGIN_CRITICAL_SECTION()
```

The pairing logic is unaffected (0 findings, correct `critical_section_functions`), so this is a reporting defect only — but `BEGIN == END` is the first sanity check a reader performs on this envelope, and it fails on every clinic file in the tree. **Fix:** count `vocabulary_counts` over comment-stripped source, as the rule logic already does. One call to the existing stripper.

## 7.3 [HIGH VALUE] Re-aim `nested_critical_sections` from deadlock to invariant

The rule's stated rationale — *"Two different objects locked at once via two single-object begins (deadlock risk)"* — is **false for CPython** and I measured it: 40 cells, 0 deadlocks, ~250M operations, including a deliberate 3-object cycle (§L3). `PyMutex_Lock` carries `_PY_LOCK_DETACH`; parking suspends every section the thread holds. A CONSIDER emitted on deadlock grounds against CPython's own source will be dismissed 100% of the time, and dismissing it is how L1 and L2 stay hidden.

> **Proposal — replace the trigger and the question.**
> **Trigger:** a function that holds a critical section (its own, its clinic wrapper's, or via `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED`) and calls, directly or one hop away, anything that can acquire a *different* object's `ob_mutex` — including the buffer-protocol slots (`PyObject_GetBuffer`, `PyBuffer_Release`), `tp_iternext`, and any `PyObject_Call*`.
> **Question:** does the enclosing function use, *after* that call, a value derived from `self` and computed *before* it? If yes → FIX. If it re-derives everything → ACCEPTABLE.
> **Suppress:** the call is `Py_BEGIN_CRITICAL_SECTION2`-guarded on the same pair; or the receiver-derived value is a scalar re-read after the call; or the section is entered while the world is stopped.

On this slice that rule fires on exactly `bytearray_iconcat:382` and `bytearray_extend_impl:2186` — **2 findings, 2/2 precision** — and correctly stays silent on `bytearray_ass_subscript:893` and `bytearray_mod:2865` (CS2-guarded), on `bytearray_iconcat_lock_held:362` reading `Py_SIZE(self)` *after* the getbuffer, and on all 41 clinic wrappers. Fixtures: positives `Objects/bytearrayobject.c:382` and `:2186`; negatives `:893` and `:2865`, both introduced by the same upstream commit.

This is also the rule that would have found gh-153578 statically, which no scanner in the toolkit currently can.

## 7.4 [HIGH VALUE] `clinic_critical_section_coverage` — three corrections before it is built

Group B proposes this rule and asks for my table as evidence. The table supports the *idea* and refutes the *specification* on three points.

**(i) The gate excludes exactly the types that have the bugs.** As specified — *"for every type with ≥1 `@critical_section` clinic method"* — the rule **cannot fire on `PyBytes_Type`**, which has 25 clinic wrappers and **zero** critical sections. That is the one type in this slice with a confirmed, reproduced free-threading crash (CPY-0182 and its three G1 siblings). A coverage rule gated on the presence of existing coverage is blind to un-retrofitted types by construction. **Replace the gate with "the type has mutable post-publication state"** — detectable from writes to `self->field` / `Py_SET_SIZE(self, …)` outside the constructor — and `PyBytesIter_Type` becomes a 0-of-4 POLICY finding instead of nothing.

**(ii) The denominator is wrong by 34% because it counts only clinic entries.** On `PyByteArray_Type` the rule would report **33/36** and name `__init__`, `clear`, `__sizeof__`. The truth is **51 hand-installed critical sections across 56 methods**: 33 from clinic annotations and **18 from hand-written four-line `PyMethodDef` wrappers** in the `.c` file (`bytearray_lower:2446`, `bytearray_center:2144`, the eight `bytearray_is*:2029-2105`, …). Those 18 are 53% of `bytearrayobject.c`'s 34 regions. Parse the type's `PyMethodDef` table and its slot tables, not the clinic header.

**(iii) With (i) and (ii), the rule finds both of this slice's reproduced FT bugs from one denominator:**

| type | entry points | protected | finding |
|---|---|---|---|
| `PyList_Type` | 32 | 32 | — (silent, correct) |
| `PyBytes_Type` | 39 + 15 slots | n/a — immutable | rule not applicable |
| **`PyBytesIter_Type`** | **4** | **0** | **POLICY — type has mutable state and zero synchronisation** = CPY-0182 + G1 |
| **`PyByteArray_Type`** | **56 + 18 slots** | **51 CS + 2 atomic** | **FIX — `bytearray___init___impl`** = CPY-0187 |
| `PyByteArrayIter_Type` / `PyListIter_Type` / `PyListRevIter_Type` | 4 each | atomic index | — |

Keeping Group B's gate ("the unlocked impl assigns to a field of `self` or calls `Py_SET_SIZE(self, …)` directly") is right and I verified it mechanically against all six of `list`'s unlocked clinic entries: `extend` (delegates to `_list_extend`), `index`, `count` (no writes), `__init__` (opens its own CS; `list_clear(self)` is a call, not a direct field write), `__sizeof__`, `__reversed__` (writes only the fresh iterator) — **6/6 correctly suppressed, 0 false positives on `list`.**

## 7.5 [MEDIUM] Two taxonomy entries this slice paid for

> **A contended nested critical section is a detaching operation.** `PyMutex_Lock` → `_PyMutex_LockTimed(m, -1, _PY_LOCK_DETACH)` (`Python/lock.c:654`) → `_PyParkingLot_Park(…, detach=1)` (`:138`) → `PyEval_ReleaseThread` (`Python/parking_lot.c:333`) → `_PyCriticalSection_SuspendAll`. So `PyObject_GetBuffer` on a *second object of the same type* opens the same window an `Event.wait()` does, with no user callback in the program. Measured: `bytearray.extend` between two threads, `assert(avail >= 0)` SIGABRT 3/3 on `debug-ft-nojit`, against 0 of 9,596,781 with per-thread pairs.

> **`l.extend(iter(l))` never terminates.** `list_extend_iter_lock_held` appends to the list its iterator is walking. Single-threaded, 10 s timeout: hangs on `release-ft-nojit` **and** `release-gil-nojit`, while `l.extend(l)`, `l[0:0] = l`, `b[0:0] = b` and `b.extend(iter(b))` all complete. Under concurrency the faulthandler dump is indistinguishable from a deadlock. Before reading a hang in a lock probe as a lock result, run the operation alone. (Sibling of the entry `gil-discipline-checker` §G4 added for `list.remove`.)

## 7.6 What the informed method bought and cost

**Bought.** The guarded-twin rule produced both findings directly and in the right order: building the §3.3 coverage table surfaced `Py_BEGIN_CRITICAL_SECTION2` as an idiom used exactly twice in `bytearrayobject.c`; asking *"which other two-operand functions are there"* produced `iconcat` and `extend`; the tracker search then produced PR #130227, whose body enumerates the twins and omits these two, and whose test file shows precisely why. Cold, I would have filed `nested_critical_sections` as a CONSIDER and moved on — which is what the rule as written asks for, and it would have been wrong.

**Cost.** I built the `self_alias` probe before checking whether its operations terminate, and got a four-build hang with a convincing faulthandler dump. Two runs and one OOM-kill (`rc=-9`, unbounded doubling) before the single-threaded isolation showed the cause was `l.extend(iter(l))`. The brief's method note 1 — *"a verdict rests on a crash count or a debugger frame"* — is not sufficient on its own: I had the frame, and the frame was pointing at my own probe.

---

# 8. What I did not do

- **No TSan run.** Both findings are hard failures with N-of-M and four clean controls; a TSan race pair would add nothing to L1 (it is an exception, not a race on memory) and L2's window is a lock suspend rather than an unsynchronised access, which TSan does not model.
- **No deterministic L1 demonstrator.** The window is a few instructions wide; the case is statistical (45% vs four zero controls, ~140M iterations).
- **I did not establish the content of L2's disclosed bytes.** Observed `0x00` — zeroed pages. I did not construct a recycled-heap case.
- **I did not build today's main.** The "live on main" claims are read from `gh api .../contents/…` fetched 2026-07-26. Falsifier: build main and re-run `lock_iconcat_spurious_buffererror.py extend`.
- **I did not file upstream.** L1 has no issue I could find; L2's escalation belongs on gh-153578. Filing is the campaign's call.
- **`bytes_methods.c` contributes nothing to this agent** — 0 regions, 0 locks, 0 atomics. It receives `(const char *, Py_ssize_t)`; its FT safety is entirely its callers' (mapper M-note, `refcount-auditor` §2).
- **No JIT builds.** All measurements on `*-nojit`.

---

# 9. Artifacts

Reproducers, all `.py` files in `reports/obj-sequences/repro/`:

- `lock_iconcat_spurious_buffererror.py` — **L1**; 5 scenarios (`iconcat`, `extend`, the `setslice` CS2 twin, `iconcat_solo`, `iconcat_disjoint`), reports the failure **rate**
- `lock_extend_ft_uninit_disclosure.py` — **L2**; foreign-byte detection with three controls
- `lock_order_inversion.py` — **L3**; 10 scenarios incl. `chain3`, `cs2_vs_nested`, `self_alias`, two solo controls
- `lock_self_alias_isolate.py` — **L4**; single-threaded separation of non-termination from deadlock
- `lock_prelock_window_sweep.py` — **task (a)**; 27 pre-lock converter windows, `LOCK_PRELOCK_MODE ∈ {clear, grow, shrink}`
- `lock_matrix_driver.py` — build-matrix driver; one subprocess per cell, hard timeout, `PYTHON_GIL` per family, captures the child's faulthandler dump on timeout

Analysis scripts (scratchpad, not reproducers): `walk_cs_regions.py` (the 108-region walker), `dump_cs_regions.py` (region bodies by size), `coverage_table.py` (the §3 denominators).

---

## Summary line

```
FIX=2  CONSIDER=0  POLICY=0  ACCEPTABLE=2
scan_lock_discipline: 0 candidates / 108 regions -- EARNED ZERO, 100% of the population walked
  pre-run covered 67/108 regions (62%), 54/95 functions (57%), 134/216 tokens (62%)
  the other 38% is ALSO clean -- and its invisibility was slice SCOPE, not scanner capability
mutex family: 0 acquires across 7 files; 1 PyMutex_IsLocked assert -- structural zero, stated
novel reproduced findings: 2 -- bytearray += / .extend take the 2nd bytearray's lock NESTED
                                L1 45.2% spurious BufferError on FT, 0/140M across 4 controls
                                L2 3/3 SIGABRT debug-ft + 119/335 uninit-byte results release-ft
                                   (gh-153578 reachable with NO adversarial __buffer__)
                                guarded twins = gh-130227's own two CS2 sites, same file
task (b): list 30/30 covered, 0 gaps; bytes immutable + 4 iterator gaps (=CPY-0182);
          bytearray denominator corrected 36 -> 56 methods / 51 locks, 1 gap (=CPY-0187)
task (c): 40 inversion cells, ~250M ops, 0 deadlocks -- nested_critical_sections' deadlock
          premise is FALSE for CPython (_PY_LOCK_DETACH); re-aim the rule at the invariant
```
