# gil-stw-lock-checker — Objects/ sample (informed)

Covers three scanners in one pass, all of which returned **0 on the sample**:
`scan_gil_usage.py`, `scan_stw_safety.py`, `scan_lock_discipline.py`.
The question for each is **recall, not triage**, so the Toolkit assessment is the main body.

## Scanner volume

| scanner | Objects/ | sample | confirmed | dismissed | precision |
|---|---|---|---|---|---|
| `scan_gil_usage` | 8 | 0 | 0 | 8 (all ACCEPTABLE) | n/a on sample |
| `scan_stw_safety` | 13 | 0 | 0 | 13 (all `stw_unknown_call`, all safe) | n/a on sample |
| `scan_lock_discipline` | 1 | 0 | 0 real leak | 1 FP (real dead code) | 0% |

Found by reading, not by any scanner: **1 FIX**, **1 CONSIDER**. Both outside the 14-file sample
(scope escapes reached via the sample's own guarded-twin hunt), so they are reported as such.

---

## Findings

### FIX

#### `odictiter_reduce` takes a per-object critical section on a **stack copy of a locked `PyMutex`** — `Objects/odictobject.c:1886`

```c
static PyObject *
odictiter_reduce(PyObject *op, PyObject *Py_UNUSED(ignored))
{
    odictiterobject *di = (odictiterobject*)op;
    odictiterobject tmp = *di;              /* <-- copies the PyObject header,
                                                  including ob_mutex          */
    ...
    PyObject *list = PySequence_List((PyObject*)&tmp);
```

**What breaks.** On `Py_GIL_DISABLED` builds `ob_mutex` is part of the object header
(`Include/object.h:162`). `PySequence_List(&tmp)` drives `tp_iternext`, which for this type is
`odictiter_iternext` (`odictobject.c:1866`) — and that function does
`Py_BEGIN_CRITICAL_SECTION(op)`, i.e. it locks **the iterator object itself**. If `di->ob_mutex`
is locked at the moment of the struct copy, `tmp.ob_mutex` is a *copy of a locked mutex at a
different address*. `_PyCriticalSection_BeginSlow` (`Python/critical_section.c:21`) then:

- checks the recursive-acquire fast path — `prev->_cs_mutex == &di->ob_mutex != &tmp.ob_mutex`, no skip;
- checks `world_stopped` — false;
- calls `PyMutex_Lock(&tmp.ob_mutex)` and **parks forever**. Nothing will ever unlock that address.

The thread hangs while still holding `di->ob_mutex`, so every other thread iterating the same
`OrderedDict` hangs behind it.

**How a Python-level input reaches it.** Two ways.

*Deterministic, single-threaded.* `odictiter_iternext` holds the iterator's section across
`PyDict_GetItemRef(di->di_odict, key, &value)` at `odictobject.c:1822`, which runs the key's
`__hash__`/`__eq__`. User code there can re-enter `__reduce__` on the same iterator:

```python
import copy
from collections import OrderedDict
it = None
class K:
    def __hash__(self):
        if it is not None:
            copy.copy(it)          # -> odictiter_reduce, while it->ob_mutex is held by us
        return 1
    def __eq__(self, o): return self is o
od = OrderedDict(); od[K()] = "v"
it = iter(od.items())
next(it)
```

I ran this on the GIL build at `/home/danzin/projects/cpython/python` (3.16.0a0,
`Py_GIL_DISABLED=0`): it recurses `next → K.__hash__ → copy.copy(it) → odictiter_reduce →
PySequence_List(&tmp) → PyDict_GetItemRef → K.__hash__ …` until `RecursionError`. That confirms
the **reachability half** — user Python code really does run inside
`Py_BEGIN_CRITICAL_SECTION(iterator)`, and it really can re-enter `odictiter_reduce`. On a
free-threaded build the *first* re-entry parks instead of recursing. No FT build exists in this
checkout, so this is **static-confirmed, not run-confirmed**.

*Racy, two-threaded.* Thread A in `next(it)`, thread B in `copy.copy(it)` / `pickle.dumps(it)`.
The copy only has to land while A holds the section.

**Guarded twin.** `dictiter_reduce` (`Objects/dictobject.c:6392`) and `setiter_reduce`
(`Objects/setobject.c:1075`) use the byte-identical `T tmp = *it;` idiom and are **safe**, because
their `tp_iternext` locks the *container* (`Py_BEGIN_CRITICAL_SECTION(d)` at `dictobject.c:6353`,
`Py_BEGIN_CRITICAL_SECTION(so)` at `setobject.c:1117`), never the iterator. Those three are the
only `PyObject`-struct-copy sites in `Objects/`+`Modules/`+`Python/`, and `odictiter_iternext` is
the only iterator in the tree whose `tp_iternext` locks the iterator object. The idiom's safety
precondition ("nobody locks the iterator") was silently invalidated by
`6481539a6d9` — *gh-125996: fix thread safety of `collections.OrderedDict` (#133734)* — which added
`Py_BEGIN_CRITICAL_SECTION(op)` to `odictiter_iternext` and did not revisit `odictiter_reduce`.

**Classification: FIX.** FT-only (the macros are no-ops on the default build), but a permanent
deadlock reachable from pure Python. Fix: drop the by-value copy — snapshot the fields into a
plain (non-`PyObject`) struct, or zero `tmp.ob_mutex` after the copy, or build the list without
routing through `tp_iternext`.

### CONSIDER

#### Dead `return -1;` after an unconditional `goto` — `Objects/dictobject.c:4380`

This is the **only finding the three scanners produced across all of `Objects/`**, reported as
`critical_section_end_on_error` (FIX). It is a lock-leak **false positive**: the flagged `return`
at 4380 sits immediately after `goto slow_exit;` at 4379 and is unreachable, so the section opened
at 4321 never actually leaks.

The dead code is real though. `git log -L4376,4382` attributes it to `92abb012403` —
*gh-112075: Add critical sections for most dict APIs (#114508)* — which inserted
`res = -1; goto slow_exit;` above the pre-existing `return -1;` and forgot to delete it. A
leftover from the same FT retrofit family as the FIX above. Harmless; worth deleting.

### POLICY / ACCEPTABLE (one line each)

- `blocking_with_gil` ×8 (`Objects/object.c` `PyObject_Print`/`PyObject_Dump`, `Objects/obmalloc.c`
  `_PyObject_DebugDumpAddress`/`_pymalloc_system_hugepage_size`) — **ACCEPTABLE**: all are debug /
  crash-dump / one-shot-init paths; `PyObject_Print` already brackets its `fprintf` with
  `Py_BEGIN_ALLOW_THREADS` where it matters (`object.c:644,650`).
- `stw_unknown_call` ×13 — **ACCEPTABLE**: `HEAD_LOCK`, `PyInterpreterState_Next`,
  `_PyObjectStack_Pop`, `PyMem_Free`, `has_own_state` etc. are pointer/list walks and raw-allocator
  calls, all inside the STW contract. None resolve to a Python-invoking API.
- `dict_merge` slow path holds `Py_BEGIN_CRITICAL_SECTION(a)` across `PyMapping_Keys(b)`,
  `PyIter_Next`, `PyObject_GetItem(b, key)` — arbitrary Python under a held per-object lock.
  **POLICY**, not a bug: CPython's critical sections are designed to be suspended on detach.
  Noting it because a naïve "no Python code inside a lock" rule would fire here and must not.

---

## New siblings of known shapes

- **`fix-not-propagated-to-the-sibling-idiom`** — the FIX above is the odict instance of a
  three-member family (`dictiter_reduce` / `setiter_reduce` / `odictiter_reduce`). This is the
  briefing's *guarded twin* rule inverted: the twins are the safe ones, the FT commit broke the
  precondition for exactly one member, and nothing swept the other two to check.
- **FT-retrofit leftovers** — `dictobject.c:4380` is a second artifact of the same wave of
  `Py_BEGIN_CRITICAL_SECTION` retrofit commits (gh-112075, gh-125996). Both findings in this report
  come from that wave. A targeted sweep of the ~40 commits that added critical sections to
  `Objects/` looks like the highest-yield follow-up.

---

## Classes bounded (clean negatives)

**The sample contains almost nothing in these three scanners' vocabularies.** Exhaustive grep over
the 14 files (13,264 lines):

| construct | sample count | verdict |
|---|---|---|
| `Py_BEGIN_CRITICAL_SECTION` / `2` | 8 | all clean |
| `_PyEval_StopTheWorld` / `StartTheWorld` | 1 pair | clean |
| `Py_BEGIN_ALLOW_THREADS` | **0** | — |
| `PyGILState_Ensure` / `Release` | **0** | — |
| `PyMutex_Lock` / `Unlock` | **0** | — |
| blocking-call vocabulary | **0** (one hit inside a docstring literal, `iterobject.c:465`) | — |
| `LOCK_WEAKREFS` / `UNLOCK_WEAKREFS` | 8 pairs | all clean, **all invisible to the scanner** |

1. **All 8 critical sections are the trivial wrapper idiom** — `begin; result = helper_lock_held(...); end;`
   with the begin and end **two lines apart** and no branch between them
   (`unionobject.c:330-337` is the only one with an `if`, still exit-free;
   `genericaliasobject.c:860-862`, `weakrefobject.c:209-211`, `odictobject.c:1541-1543/1670-1672/
   1694-1696/1801-1803/1869-1871`). No `return`, no `goto`, no early exit is structurally possible.
   Genuine true negative.

2. **The one STW region is safe.** `_PyFunction_ClearVersion` (`funcobject.c:361-363`) wraps a
   single call to the local `func_clear_version`, whose whole body is struct-field writes plus
   `get_cache_item` pointer arithmetic (`funcobject.c:334-350`). No allocation, no exception, no
   Python. Matches the `gc_free_threading.c:2223` contract.

3. **The `*_lock_held` / `*_LockHeld` convention does not over-suppress** (task item 4). I traced
   every call site of every such function in the sample. The only two lock-free callers of
   `clear_weakref_lock_held` are `_PyWeakref_ClearRef` (`weakrefobject.c:137`) and `gc_clear`
   (`weakrefobject.c:165`); `gc_clear` carries an explicit justification comment, and
   `_PyWeakref_ClearRef`'s complete caller set is `Python/gc.c:858,975,998`,
   `Python/gc_free_threading.c:1573,1605,1623` (all world-stopped),
   `_PyWeakref_ClearWeakRefsNoCallbacks` (holds `LOCK_WEAKREFS`, `weakrefobject.c:1132`) and
   `_PyStaticType_ClearWeakRefs` (documented single-thread, `weakrefobject.c:1117`). Clean.

4. **`weakrefobject.c`'s `LOCK_WEAKREFS` pairing is correct on every path** — including the
   four-exit `_PyWeakref_NewRef` body at `weakrefobject.c:434-447`, where each of the three
   `return`s is immediately preceded by `UNLOCK_WEAKREFS(obj)`. Verified by hand because
   **the scanner cannot see this mechanism at all** (see Recall gap #2).

5. **The 18 `Py_BEGIN_CRITICAL_SECTION` sites in `dictobject.c` that the chassis never parses are
   also clean.** Four of them contain a `goto` between begin and end
   (`_PyObject_MaterializeManagedDict:7392`, `try_set_dict_inline_only_or_other_dict:7812,7821`,
   `ensure_managed_dict:8054`, `ensure_nonmanaged_dict:8081`) — every one jumps to a label placed
   immediately *before* `Py_END_CRITICAL_SECTION()`, which is the correct release-then-exit idiom
   the agent definition describes. This is the check that decides the verdict below, so it was done
   by hand rather than by tooling.

**Scope escapes**, per the RUN_CONTEXT convention: `Objects/dictobject.c`, `Objects/setobject.c`,
`Objects/listobject.c`, `Objects/codeobject.c` were read (sibling hunt + chassis verification) but
are outside the 14-file sample.

---

## Toolkit assessment  ← main body

### 1. Quantifying the chassis defect, per scanner

The brief's hypothesis — brace-unbalanced `Py_BEGIN_*` macros make `tree-sitter-c` emit ERROR nodes
so `extract_functions()` fails *preferentially* on the constructs these scanners hunt — **is
confirmed at the `Objects/` level and refuted at the sample level**, and the dominant ERROR cause
turns out to be a different macro family.

**The two chassis are different.** `scan_stw_safety` and `scan_lock_discipline` use
`tree_sitter_utils.extract_functions()`. `scan_gil_usage` does **not** — it is the last scanner on
a private regex chassis (`scan_gil_usage.py:115 find_functions`, which requires a bare `{` in
column 0 whose *previous line* matches a signature regex). Both must be measured separately.

**Fraction of each construct falling outside any extracted function:**

| construct | total | outside (tree-sitter) | outside (gil regex) |
|---|---|---|---|
| **SAMPLE, 14 files, 13,264 lines** | | | |
| `Py_BEGIN_CRITICAL_SECTION*` | 8 | **0 (0.0%)** | 0 (0.0%) |
| `_PyEval_Stop/StartTheWorld` | 2 | **0 (0.0%)** | 0 (0.0%) |
| baseline: indented `return` | 1039 | 11 (1.1%) | 250 (24.1%) |
| baseline: `Py_(X)INCREF/DECREF` | 321 | 9 (2.8%) | 68 (21.2%) |
| **ALL `Objects/*.c`, 73 files, 130,452 lines** | | | |
| `Py_BEGIN_CRITICAL_SECTION*` | 186 | **20 (10.8%)** | 44 (23.7%) |
| `Py_END_CRITICAL_SECTION*` | 188 | 21 (11.2%) | 45 (23.9%) |
| `PyMutex_Lock*` | 15 | 2 (13.3%) | 5 (33.3%) |
| `Py_BEGIN/END_ALLOW_THREADS` | 7 | 0 (0.0%) | 2 (28.6%) |
| `_PyEval_Stop/StartTheWorld` | 18 | 0 (0.0%) | 5 (27.8%) |
| baseline: indented `return` | 9090 | 510 (5.6%) | 3017 (33.2%) |

Functions extracted from `Objects/*.c`: tree-sitter **4,203**, gil regex **2,861**.

**Per-scanner answer to "how many are invisible":**

- **`scan_lock_discipline`** — sample: **0 of 8** invisible; the chassis defect had *literally zero*
  effect on the sample result. `Objects/`: **20 of 186** (10.8%) invisible, 1.9× the 5.6% baseline —
  this reproduces the brief's 10% and confirms the preferential-failure claim. But the distribution
  is the story, not the rate: **17 of those 20 are in a single file**, `Objects/dictobject.c`, and
  they are contiguous.
- **`scan_stw_safety`** — sample: **0 of 2** invisible. `Objects/`: 0 of 18 *dropped*, but **3 of 18
  mis-attributed** (16.7%) because their enclosing function was **merged** rather than dropped.
- **`scan_gil_usage`** — sample: 0 invisible only because the sample contains none of its
  constructs. Its real number is the baseline: **24.1% of the sample's `return` statements and
  21.2% of its refcount ops are outside any function it extracts**, and it covers **52% of sample
  lines vs tree-sitter's 69%**. Worst files: `odictobject.c` 36%, `structseq.c` 45%,
  `descrobject.c` 46%.

**The dictobject.c cliff — the single most important number in this report.**
`extract_functions()` on `Objects/dictobject.c` (8,597 lines) returns 187 functions whose
**highest `end_line` is 5,239**. Lines **5240–8597 — 3,358 lines, 39% of the file — are covered by
no extracted function at all.** Inside that dead zone live **18 of the file's 47
`Py_BEGIN_CRITICAL_SECTION` sites (38%)**. `dictobject.c` is the file the `lock-discipline-checker`
agent definition names as its canonical target ("`Objects/dictobject.c` alone has dozens").
The single finding the scanner produced is at line 4380 — inside the covered region. It found the
one thing it could see and was structurally blind past line 5239.

**Function *merging* is the second failure mode, and it is worse than dropping.** 93 real functions
across `Objects/` are swallowed into a larger extracted record. The worst is `Objects/object.c`,
where the record named `_PyObject_GetAttrId` spans lines **1267–3521** — the real function ends at
1279 — absorbing **91 functions and 64% of the file**. This is directly visible in the shipped JSON:
`scan_stw_safety.Objects.json` reports an STW finding *"in `_PyObject_GetAttrId`"* at
**line 3412**, and lists `_PyObject_GetAttrId` at line 1267 in `stw_functions`. A 2,145-line gap
between the function and "its" finding. For `scan_lock_discipline` merging is the more dangerous
mode, because its LIFO begin/end pairing then runs *across function boundaries*: an unbalanced
`BEGIN` in function X can be silently paired with an `END` in unrelated function Y. That is exactly
the mechanism that would erase a `critical_section_missing_end`.

**The ERROR causes, measured (this refines the brief's hypothesis).** Top-level ERROR nodes in the
sample: 93 across 13 of 14 files. Their causes, by inspection:

1. **Argument Clinic `*_METHODDEF` macros inside `PyMethodDef[]` initializers** — bare identifiers
   with no comma separator (`odictobject.c:1381-1400`, `dictobject.c:5241+`). **This is what causes
   the 3,358-line `dictobject.c` cliff — the biggest blind spot the lock scanner has.**
2. **`_Py_COMP_DIAG_PUSH` / `_Py_COMP_DIAG_IGNORE_DEPR_DECLS` / `_Py_COMP_DIAG_POP`** — bare macro
   identifiers at statement position with no semicolon (`object.c:1271-1274`). **This is what causes
   the 91-function `object.c` merge.**
3. `Py_BEGIN/END_ALLOW_THREADS` — confirmed at `object.c:645` and `object.c:650`, exactly as the
   brief predicted.
4. `#ifdef Py_GIL_DISABLED` splitting a struct initializer (`dictobject.c:650`).
5. `#if SIZEOF_VOID_P >= 32` splitting an if/else chain (`dictobject.c:539`, `:815`).

So the brace-unbalanced sync macros are a **real but secondary** contributor. A fix aimed only at
them would leave the two largest blind spots (`dictobject.c` and `object.c`) untouched. The fix has
to be generic ERROR recovery, not a macro allowlist.

### 2. Recall gaps

**Gap 1 — the parse cliff (quantified above).** I re-ran the critical-section pairing logic over
`Objects/` with an independent brace-matching extractor immune to macro/preproc errors. It produced
4 findings vs the shipped 1. On hand triage **all 3 extras were FPs of my cruder repair**:
`codeobject.c:1627` (a `goto done;` whose `done:;` label — with the trailing semicolon my label
regex missed — is inside the section; the shipped tree-sitter `labeled_statement` walk gets this
right), and `listobject.c:3251` + `:3504` (`Py_END_CRITICAL_SECTION()` written **without a trailing
semicolon**, which my `;`-anchored regex missed; harmless, the macro ends in `}`, but note it is a
spelling that exists in the tree). I then hand-verified all 18 invisible `dictobject.c` sites
(clean, §Classes-bounded item 5). **Conclusion: the recall gap is large and real, but in this
codebase it is not currently hiding a critical-section bug.** Both halves of that sentence matter.

**Gap 2 — vocabulary, and it is self-inflicted.** `scan_lock_discipline.py:86
_get_critical_section_pair()` filters `data/lock_macros.json` to `pair["type"] == "critical_section"`
**only**. The data file already ships a `PyMutex` pair (`type: "python_mutex"`,
`PyMutex_Lock`/`PyMutex_Unlock`) and a `StopTheWorld` pair — the scanner loads them and throws them
away. Consequences: 31 `PyMutex_*` sites in `Objects/` are unchecked, and **the entire locking
scheme of `weakrefobject.c` — a sample file — is invisible**: its 16 `LOCK_WEAKREFS` /
`UNLOCK_WEAKREFS` / `LOCK_WEAKREFS_FOR_WR` sites are `PyMutex_Lock`-backed macros from
`Include/internal/pycore_weakref.h:18-30` and match nothing in the vocabulary. That file's
four-exit `_PyWeakref_NewRef` (`weakrefobject.c:434-447`) is precisely the
`critical_section_end_on_error` shape, expressed in a different macro family, and the scanner
cannot see it. **This is a one-line fix with a much better ratio than the chassis work.**

**Gap 3 — the shape that found the FIX is not in any scanner.** No rule models
`Py_BEGIN_CRITICAL_SECTION(x)` where `x` is a **by-value copy of a `PyObject`**. It is grep-able in
one line (`^\s*\w+(object|Object)\s+\w+ = \*\w+;` finds exactly the 3 sites in the tree) and it is
how the only real bug in this report was found.

**Gap 4 — intra-function scope hides real nesting.** `odictiter_iternext` (`odictobject.c:1869`)
locks the iterator, and three frames down `odictiter_nextkey` (`:1801`) locks
`di->di_odict` — two different objects held at once, which is the `nested_critical_sections` shape.
The scanner is intra-function so it sees neither. CPython documents the intent with
`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(di)` at `:1796`; that assert macro is a free, reliable
cross-function lock-state signal the scanner currently ignores (11 occurrences in `odictobject.c`
alone). I found no lock-order inversion for this pair, so no finding — but the shape is invisible,
not absent.

### 3. Prompt issues

- **`gil-discipline-checker.md` has no "what if the scan returns 0" branch**, and no instruction to
  report chassis coverage. Its Phase 1/2/3 all assume findings exist. `stw-safety-checker.md` and
  `lock-discipline-checker.md` both do better — they at least name their intra-file / intra-function
  limitation — but none of the three tells the agent to *measure* what fraction of its own
  vocabulary it could see. Given that a 0-finding result is the common case for these three on any
  given scope, that branch is the important one.
- **`lock-discipline-checker.md` Phase 3 asks for exactly the right manual check** ("`Py_BEGIN`
  counts vs `Py_END` counts per file") and it is the check that would have surfaced the
  `dictobject.c` cliff instantly (47 begins in the file, 29 visible). It is buried as the last
  bullet of the last phase. It should be Phase 0, and it should be automated.
- `gil-discipline-checker.md` lists `mismatched_gilstate` as a finding type, but
  `PyGILState_Ensure`/`Release` appear **zero** times in `Objects/`. Fine for `Modules/`; the agent
  should be told the type is scope-dependent so it doesn't hunt for absent things.

### 4. Concrete tuning proposals (ranked)

**1. ERROR-node recovery in `tree_sitter_utils.extract_functions()`, plus a mandatory
`parse_coverage` field in the JSON envelope.**
After collecting top-level `function_definition` nodes, walk the tree for `ERROR` nodes; for each,
re-scan its byte range with a column-0 brace matcher (signature line, then `^{` … `^}`) and
synthesize function records for whatever it finds. Additionally, split any extracted record whose
body contains a column-0 `}` — that is a merge, and it is detectable in one line.
*Measured payoff:* recovers 3,358 lines of `dictobject.c` (18 critical sections, 38% of the file's
total), un-merges the 91 functions inside `object.c`'s fake `_PyObject_GetAttrId`, and fixes the
2,145-line misattribution already visible in the shipped `scan_stw_safety` JSON. Benefits
`scan_lock_discipline`, `scan_stw_safety`, `scan_ft_races`, `scan_refcounts`, `scan_error_paths`,
`scan_null_checks` — every tree-sitter scanner.
Then add to `scan_common.build_report()`:
```python
"parse_coverage": {"total_lines": N, "covered_lines": M,
                   "coverage_pct": round(100*M/N, 1), "error_nodes": E}
```
so **every** report carries its own blind-spot number and no future agent has to re-derive it. On
`dictobject.c` today that field would read `coverage_pct: 61.0, error_nodes: 13` — enough to make
the "1 finding across 118k lines" result immediately suspect instead of reassuring.

**2. `scan_lock_discipline.py:86` — stop discarding the vocabulary the data file already ships.**
```python
_LOCK_TYPES = {"critical_section", "python_mutex"}          # was: == "critical_section"
```
and add the CPython-only weakref pair to `data/lock_macros.json`:
```json
{"name": "LOCK/UNLOCK_WEAKREFS",
 "acquire": ["LOCK_WEAKREFS", "LOCK_WEAKREFS_FOR_WR"],
 "release": ["UNLOCK_WEAKREFS", "UNLOCK_WEAKREFS_FOR_WR"],
 "type": "python_mutex",
 "note": "PyMutex-backed, Include/internal/pycore_weakref.h:18-30"}
```
Emit these under a new `type: "mutex_leak_on_error"` (FIX) so they are distinguishable from the
scoped-macro family — a `PyMutex` leak is *not* auto-released by scope exit, so it is strictly worse
than a missing `Py_END_CRITICAL_SECTION`. *Payoff:* 31 `PyMutex_*` sites in `Objects/` plus
`weakrefobject.c`'s 16 lock sites become visible. One line of code plus a data entry.

**3. `scan_gil_usage.py` — delete `find_functions()` (lines 115-163) and import
`extract_functions` from `tree_sitter_utils`.**
It is the only scanner still on a private regex chassis. Measured: 52% line coverage vs 69% on the
sample, 2,861 vs 4,203 functions on `Objects/`, 33.2% of `return` statements invisible vs 5.6%.
tree-sitter is already a hard dependency of the toolkit, so this removes code rather than adding
any. Do it *after* proposal 1 so the GIL scanner inherits the recovery.

**4. Dominance gate on `critical_section_end_on_error` — removes the run's only FP.**
Skip an exit statement that is unreachable: in `_analyze_critical_sections`, drop an `exit_` whose
immediately preceding non-comment sibling statement in the same `compound_statement` is an
unconditional `goto` / `return` / `break` / `continue`. That is exactly `dictobject.c:4380`. With
proposals 1+2 in place this takes the `Objects/` run to **0 findings and 0 false positives** — a
number that would then be *earned* rather than an artifact.

**5. New finding type `critical_section_on_stack_copy` (FIX) — the shape that found this report's
only real bug.**
Flag any local whose initializer is `*<ptr>` where the declared type name matches
`(?i)\w+(object)$`, that is subsequently passed (as `&local` or a cast) to
`Py_BEGIN_CRITICAL_SECTION*` or to any call that can reach `tp_iternext`. Conservative version:
flag every `<T>object <v> = *<p>;` in a file that also contains `Py_BEGIN_CRITICAL_SECTION`, and let
the agent triage. Tree-wide that is 3 candidates and 1 true positive — a 33% precision rule that
costs nothing to run. Add the guarded twin to `data/cpython_non_bugs.md`: *"a `PyObject` struct copy
is safe iff no code takes a per-object critical section on the copied object; `dictiter_reduce` and
`setiter_reduce` are the safe twins, `odictiter_reduce` is the counterexample."*

**6. Teach the STW/lock scanners `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(x)`.**
It is a free, authoritative, cross-function statement of "the caller holds `x`'s lock" — 11
occurrences in `odictobject.c` alone. Use it to (a) seed a caller-side lock-state map so the
intra-function limitation can be partly lifted, and (b) detect two-object nesting across function
boundaries (Gap 4). Also add `LOCK_WEAKREFS`-style asserts if any exist.

**7. Agent-prompt edit, all three files: add a "returned zero" protocol.**
> If the scan returns 0 findings for your scope, do not stop. (a) Grep the scope for every construct
> in your vocabulary and report the raw counts — a true negative must be backed by a denominator.
> (b) Report `parse_coverage` from the envelope, and per-file coverage for any file below 80%.
> (c) State explicitly whether the zero is "no constructs present" or "constructs present and all
> verified clean" — they are different results.

Add to `lock-discipline-checker.md` specifically, promoted from Phase 3 to Phase 0: *"Before
triaging, compare `grep -c Py_BEGIN_CRITICAL_SECTION <file>` against the count the scanner attributes
to that file. A gap means the chassis dropped functions; hand-check the difference."* On
`dictobject.c` that is 47 vs 29 and takes ten seconds.

---

## Verdict per scanner

| scanner | sample verdict | `Objects/`-wide |
|---|---|---|
| **`scan_gil_usage`** | **TRUE NEGATIVE** — the sample contains 0 `ALLOW_THREADS`, 0 `PyGILState_*`, 0 blocking calls. Nothing to find. | Latent recall gap, **not** exercised here: regex chassis, 52% line coverage, 33.2% of `return`s invisible. The 8 `Objects/` findings are all ACCEPTABLE debug paths. |
| **`scan_stw_safety`** | **TRUE NEGATIVE** — 1 STW region (`funcobject.c:361`), hand-verified safe. Chassis effect: zero. | 13 `stw_unknown_call`, all safe on triage. One real defect: 3 of 18 STW tokens land in merged blobs, producing a 2,145-line function misattribution. |
| **`scan_lock_discipline`** | **TRUE NEGATIVE** — 8 sections, all the trivial 3-line wrapper idiom, no exit structurally possible. Chassis effect: **zero — 0 of 8 invisible**. | **Recall gap, but the code is genuinely clean.** 20/186 (10.8%) sections invisible, 17 of them a contiguous 3,358-line blind spot in `dictobject.c`; plus a data-file filter that discards the `PyMutex` vocabulary entirely. I hand-verified all 18 invisible `dictobject.c` sites and `weakrefobject.c`'s 16 lock sites: all correct. The 1 shipped finding is an FP. **So: the 1-finding result is substantively right and structurally lucky.** |

**Direct answer to the framing question.** `scan_lock_discipline` finding exactly 1 thing across
`Objects/` is *not* explained by the chassis defect in the way the brief hypothesised — the sample's
8 critical sections were all perfectly visible, and the 18 that were invisible are all clean on hand
inspection. The honest explanation is a stack of three independent factors, and the chassis is only
the second-largest:

1. **CPython's critical-section usage is overwhelmingly the trivial-wrapper idiom** — begin, one call
   to a `*_lock_held` helper, end. 8 of the sample's 8 sections and the large majority of
   `Objects/`' 186 have no branch between begin and end, so the two FIX shapes the scanner hunts are
   structurally impossible there. This is the dominant factor and it is genuinely good news.
2. **A 3,358-line parse cliff in the one file that matters** (`dictobject.c`, 38% of its sections
   invisible) plus 93 merged functions tree-wide, which makes the low count *unverifiable* rather
   than wrong.
3. **A one-line data-file filter** that discards the `PyMutex` vocabulary, blinding the scanner to
   `weakrefobject.c`'s entire locking scheme and 31 further `PyMutex_*` sites.

Factor 1 is why the number is low. Factors 2 and 3 are why nobody could have known that from the
number alone — which is the argument for proposal 1's `parse_coverage` field.
