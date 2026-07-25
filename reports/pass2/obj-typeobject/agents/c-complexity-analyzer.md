# C Complexity Analysis — `Objects/typeobject.c` PASS 2

INFORMED-EXPLORE, slice `obj-typeobject`, **pass 2**. CPython `/home/danzin/projects/cpython` @
`4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0).
Scope: the eight pass-2 regions — R3 (228–522), R6 (971–1481), R11 (3217–3702), R19 (6140–6452),
R21 (6529–6848), R25 (7482–7846), R26 (7848–8406), R37 (12534–13068).

Pass 1's complexity report (`reports/obj-typeobject/agents/c-complexity-analyzer.md`) is **not**
restated. Everything below is new work over regions pass 1 never read.

**Build-matrix diff, per the run rule.** `Objects/typeobject.c` is **byte-identical** between the
target `4f3be1b5777` and the build-matrix commit `a1d580430c8`. The two are 52 commits apart and
**zero** of those commits touch this file (`git log 4f3be1b5777..a1d580430c8 -- Objects/typeobject.c`
returns nothing; `git diff --stat` is empty). Every line number cited here is valid on every build in
`~/projects/python_build_matrix/builds/`.

---

> ## The calibration, up front — it is sharper in pass 2 than anywhere it has been measured
>
> A complexity ranking is a **reading order, not a risk score**. On the measured 14-file `Objects/`
> sample the top 10 by score held 5 of 25 defect-bearing functions (~10× enrichment) while **20 of
> those 25 sat at the score floor**.
>
> **This slice reproduces that failure rate almost exactly, and independently.** Of the **25
> functions the history agent independently prioritized** for pass 2 (15 watchlist + 5 just-below-cut
> + 5 density rows, §1b/§5 of `history_context.md`), **19 score exactly 1.0 — the floor.** Complexity
> and 3-year crash-fix density are close to orthogonal here.
>
> **And the metric inverts within this slice, demonstrably.** The single highest-scoring pass-2
> function, `_Py_type_getattro_stackref` (2.1), has **zero** locking operations, **zero** atomics and
> **two** synchronization regimes — it is the *least* concurrency-entangled function in the pass-2
> top five. The function history ranks **#1** (`_PyType_Modified_Unlocked`, 7 crash fixes in 3 years,
> newly re-entered from `dictobject.c:1971` 19 days before HEAD) scores **1.3** and interleaves
> **six** regimes. **Do not use any number in this document as a severity input for a
> free-threading, locking, or recursion finding.**

---

## 1. THE CORRELATION TABLE — complexity × fix density × live finding

This is the deliverable. Three independent signals joined per function:
**C** = complexity score (this document) · **H** = 3-year crash-fix count and watchlist rank
(`preflight/history_context.md` §1b/§5) · **F** = live scanner findings
(`preflight/PASS2_BASELINE.md` + `<run>/scanners/*.sample.json`, mapped to containing functions here).

### 1a. Cell A — triple-positive: complexity-ranked **and** fix-dense **and** a live finding

Read these first. The scanner already has a hypothesis and history says the code has been wrong here.

| # | Function : line | Reg | **C** score | **H** 3y fixes (wl#) | **F** live finding(s) | Why this cell |
|--:|-----------------|-----|------------:|---------------------:|-----------------------|---------------|
| **A1** | `_Py_type_getattro_stackref`:6570 | R21 | **2.1** — #1 in pass 2 | 2 (**#12**), 1 in 12mo | `ft_races@6620` `publish_before_init_complete` (low) | Only pass-2 function that is #1 by complexity. Last touched **2026-02-06 for namedtuple perf** (`d891b2bbd16c`) — the commit that *created* the flagged line, and it was not a race audit (§3.1). 80 lines, 3 live `_PyCStackRef`s, 6 gotos, 6 user-callback entry points. |
| **A2** | `mro_implementation_unlocked`:3430 | R11 | **1.3** | 2 (**#11**), 1 in 12mo | `refcounts@3443` `borrowed_field_deref_across_call` (**high**) | `a8edca62fc6d` 2025-10-21 landed "**defensive** NULL checks to MRO resolution" here — *defensive* means the invariants were never enumerated. The flagged `lookup_tp_bases`→`_PyType_CAST` loop at 3440–3451 is the exact code that commit touched. |
| **A3** | `_PyType_Modified_Unlocked`:1165 | R6 | **1.3** | **7** (**#1**) | `recursion_guards@1206` `missing_recursion_guard` (**CPY-0087**, recorded) | History's **highest-priority pass-2 target** (§4.2). Newly exported and re-entered from `Objects/dictobject.c:1971` as of 19 days before HEAD; the two prior attempts at that call path produced a **deadlock** and then a **pure-Python-triggerable data race**. Six interleaved regimes in 45 lines. |
| **A4** | `find_name_in_mro`:6144 | R19 | 1.0 (floor) | **6** (**#5**) | **4 findings** — `error_paths@6149`, `error_paths@6158`, `pyerr_clear@6158`, `recursion_guards@6147` | **The densest finding cluster in pass 2 and it is at the score floor.** 9 lifetime crash fixes. Return convention changed to `_PyStackRef` 2026-03 (`f26eca7732ca`) for perf. See §4d — its stated contract ("It never set an exception") makes every `PyErr_Clear()` look intentional. |
| **A5** | `assign_version_tag`:1407 | R6 | 1.0 (floor) | 2 (below-cut) | `recursion_guards@1431` (**CPY-0087**, recorded) | Version-tag write path; 3 of the 4 version-tag cluster fixes touched it. |

### 1b. Cell B — the cell that matters more: complexity-high × fix-dense × **scanner-SILENT**

**A zero here is a coverage hole, not a clearance — and I can prove it for two of the three.**

| # | Function : line | Reg | **C** score | **H** 3y fixes (wl#) | **F** | Why the zero is not evidence |
|--:|-----------------|-----|------------:|---------------------:|-------|------------------------------|
| **B1** | `type_setattro`:6749 | R21 | **1.7** — #2 in pass 2 | **7** (**#3**), **2 in 12mo** — the only pass-2 function with two crash fixes in the last year | **none** | `scan_ft_races` reports **`lock_held_functions: 0`** over a file where I independently count **23** lock-taking functions. `type_setattro` holds `BEGIN_TYPE_DICT_LOCK` across `type_update_dict` + `update_slot_after_setattr`. The FT scanner's lock-held rules **never fired on this file at all**. |
| **B2** | `_PyType_LookupStackRefAndVersion`:6305 | R19 | **1.6** — #3 in pass 2 | **7** (**#2**) — **10 lifetime, the most-fixed function in pass 2** | **none** | Same `lock_held_functions: 0` gap. Also `scan_gil_usage` has `vocabulary_resolved: 0` / `vocabulary_tokens_seen: 4` — a **structural zero**, its 0 findings carry no information. Rewritten twice for perf in 2026 after 4 race fixes. |
| **B3** | `object_set_class_world_stopped`:7699 | R25 | **1.3** | 4 (**#4**) | **none** | `scan_stw_safety` reports **`stw_functions: 3`**. I count **8** functions calling `types_stop_world()` (`set_tp_mro`, `type_set_abstractmethods`, `type_set_bases_unlocked`, `apply_type_slot_updates`, `_PyType_SetFlags`, `_PyType_SetFlagsRecursive`, `object_set_class`, `PyType_Freeze`). **3-of-8 recall on the denominator.** §4.4's finding — a `_world_stopped` function whose sole caller now conditionally does *not* stop the world (7821–7829) — is in the missing 5. |

**These three are the report's headline.** They are the top of the complexity ranking *and* the top
of the fix-density ranking *and* have nothing pointed at them, because the three scanners that would
speak to them each have a measurable denominator defect on this file. Cell B outranks Cell A for
where the next bug is.

### 1c. The 19-at-the-floor list — history-prioritized, complexity-invisible

Named so nobody mistakes a floor score for a clearance. Every one scores exactly **1.0**:

`find_name_in_mro` (6/3y) · `object_set_class` (5) · `mro_invoke` (5) · `mro_internal` (5) ·
`_PyType_SetFlags` (5 of 5 lifetime commits are crash fixes) · `set_version_unlocked` (4) ·
`_PyType_SetFlagsRecursive` (4) · `PyType_Freeze` (4) · `type_mro_modified` (3) ·
`_PySuper_LookupDescr` (3) · `managed_static_type_state_init` (3) ·
`managed_static_type_state_clear` (3) · `update_cache` (3) · `assign_version_tag` (2) ·
`update_cache_gil_disabled` (2) · `type_update_dict` (2) · `static_ext_type_lookup` (2) ·
`update_slot_after_setattr` (2) · `mro_check` (2).

### 1d. A region-map hole the correlation exposes

`_PyType_SetFlags`:6453, `set_flags_recursive`:6482 and `_PyType_SetFlagsRecursive`:6505 fall in the
**6453–6528 gap between R19 (ends 6452) and R21 (starts 6529)** — no named pass-2 region covers them.
History §4.3 rates the `_PyType_SetFlags` / `_PyType_SetFlagsRecursive` sibling gap (gh-148450's
version-tag invalidation landed on the recursive one only; both called from `Modules/_abc.c`, 53
lines apart) as **High** priority. All three score 1.0 and all three are outside the scoped regions.
**POLICY:** extend R19's upper bound to 6528, or the run drops its own High-priority item.

---

## 2. Raw ranking — top 15 pass-2 functions by complexity

103 of the file's 423 parsed functions fall in pass-2 regions. **Max pass-2 score is 2.1**, against
the file max of 6.8 (`type_from_slots_or_spec`, pass 1). Only **4** pass-2 functions reach the
file-wide top-2% threshold of 1.7. Pass 2 is structurally a region of small functions; the metric has
correspondingly little to discriminate with.

| # | Function | Line | Reg | Score | Lines | Nest | Cyc | Params | Gotos | Switch | Exits | Locks | Atomics |
|--:|----------|-----:|-----|------:|------:|-----:|----:|-------:|------:|-------:|------:|------:|--------:|
| 1 | `_Py_type_getattro_stackref` | 6570 | R21 | **2.1** | 80 | 4 | 17 | 3 | 6 | 0 | 3 | 0 | 0 |
| 2 | `object_getstate_default` | 7922 | R26 | 1.7 | 101 | 4 | 24 | 2 | 3 | 0 | 8 | 0 | 0 |
| 3 | `reduce_newobj` | 8221 | R26 | 1.7 | 86 | 2 | 17 | 1 | 0 | 0 | 11 | 0 | 0 |
| 4 | `type_setattro` | 6749 | R21 | 1.7 | 78 | 2 | 18 | 3 | 2 | 0 | 5 | 4 | 0 |
| 5 | `_PyType_LookupStackRefAndVersion` | 6305 | R19 | 1.6 | 66 | 4 | 14 | 3 | 0 | 0 | 5 | 2 | 4 |
| 6 | `check_duplicates` | 3269 | R11 | 1.5 | 23 | **5** | 6 | 1 | 0 | 0 | 2 | 0 | 0 |
| 7 | `_PyObject_GetNewArguments` | 8089 | R26 | 1.4 | 73 | 2 | 14 | 3 | 0 | 0 | 16 | 0 | 0 |
| 8 | `super_init_without_args` | 12813 | R37 | 1.4 | 67 | 3 | 14 | 3 | 0 | 0 | 8 | 0 | 0 |
| 9 | `mro_implementation_unlocked` | 3430 | R11 | 1.3 | 61 | 2 | 13 | 1 | 0 | 0 | 8 | 1 | 0 |
| 10 | `compatible_for_assignment` | 7627 | R25 | 1.3 | 48 | 1 | 16 | 4 | 4 | 0 | 3 | 0 | 0 |
| 11 | `_PyType_Modified_Unlocked` | 1165 | R6 | 1.3 | 45 | 4 | 11 | 1 | 0 | 0 | 1 | 0 | 1 |
| 12 | `object_set_class_world_stopped` | 7699 | R25 | 1.3 | 37 | 4 | 11 | 2 | 0 | 0 | 5 | 2 | 0 |
| 13 | `_PyType_LookupByVersion` | 1343 | R6 | 1.2 | 40 | 1 | 15 | 1 | 0 | **12** | 15 | 0 | 0 |
| 14 | `pmerge` | 3360 | R11 | 1.1 | 45 | 3 | 12 | 3 | 3 | 0 | 2 | 0 | 0 |
| 15 | `set_mro_error` | 3309 | R11 | 1.1 | 44 | 3 | 13 | 3 | 0 | 0 | 3 | 0 | 0 |

**Rows 2, 3 and 7 are all R26 pickle** — which history rates **8 of 8** regions (2 crash fixes in
3 years, cluster closed 2024) and `RUN_CONTEXT.md` explicitly deprioritizes. **Three of the top seven
complexity slots are spent on the coldest region in pass 2.** That is the anti-correlation stated as
plainly as this slice can state it: if the run followed the complexity ranking it would spend its
first three reads on the region history says is finished.

### 2b. Cleanup ladders within pass-2 regions

`manual_cleanup_ladder = owned_locals × returns_with_cleanup`, reported only when `goto_count == 0`.
In CPython a `goto` ladder is a **positive** signal — the cleanup is written once; its absence with
several owned locals means cleanup was hand-copied at every exit.

| # | Function | Line | Reg | Ladder | Owned | Returns w/ cleanup | Score |
|--:|----------|-----:|-----|-------:|------:|-------------------:|------:|
| 1 | `reduce_newobj` | 8221 | R26 | **27** | 3 | 9 | 1.7 |
| 2 | `_PyObject_GetNewArguments` | 8089 | R26 | 14 | 2 | 7 | 1.4 |
| 3 | `super_init_without_args` | 12813 | R37 | 8 | 2 | 4 | 1.4 |
| 4 | `_PyType_GetSlotNames` | 7873 | R26 | 6 | 2 | 3 | 1.0 |
| 5 | `mro_invoke` | 3590 | R11 | **6** | 2 | 3 | 1.0 |
| 6 | `mro_implementation_unlocked` | 3430 | R11 | 4 | 2 | 2 | 1.3 |
| 7 | `set_mro_error` | 3309 | R11 | 4 | 2 | 2 | 1.1 |
| 8 | `mro_internal` | 3654 | R11 | 3 | 1 | 3 | 1.0 |

The ladder metric is **much weaker in pass 2 than in pass 1** (max 27 vs. pass 1's 27 but with only
3 functions above 6, versus 10 in pass 1). Pass-2 code is short and mostly borrows rather than owns.
The only ladder entry that lands on a watchlist function is **`mro_invoke` (ladder 6, watchlist #9,
5 crash fixes/3y)** — it owns `mro_result` and `new_mro` across a **user-supplied `mro()` call**
(3603) and hand-copies the DECREF at three exits (3614/3620/3626). That is the one row worth handing
to the refcount agent.

### 2c. A third counter-metric this slice suggests: **interleaved-regime count**

Neither existing metric recovered the history watchlist. So I counted, per function, the number of
**distinct synchronization/ownership regimes** whose rules a reviewer must hold simultaneously —
`#ifdef Py_GIL_DISABLED` fork, TYPE_LOCK, TYPE_DICT_LOCK, stop-the-world, critical section, seqlock,
raw atomics, `_PyStackRef`/`_PyCStackRef`, deferred refcounting, documented re-entrancy, user-callback
entry, raw `PyMem_*`.

| Function | Regimes | Score | Watchlist rank |
|----------|--------:|------:|---------------:|
| `_PyType_Modified_Unlocked`:1165 | **6** — ft-fork, re-entrancy, type-lock, stop-world, atomics, user-callback | 1.3 | **#1** |
| `type_setattro`:6749 | **5** — ft-fork, type-lock, dict-lock, user-callback, deferred-rc | 1.7 | **#3** |
| `_PyType_LookupStackRefAndVersion`:6305 | **5** — ft-fork, stackref, atomics, type-lock, seqlock | 1.6 | **#2** |
| `_PyType_CacheInitForSpecialization`:6407 | 4 | 1.0 | — |
| `_PyType_CacheGetItemForSpecialization`:6427 | 4 | 1.0 | — |
| `mro_invoke`:3590 | 3 | 1.0 | #9 |
| `_Py_type_getattro_stackref`:6570 | **2** | **2.1** | #12 |

**The regime count recovers the history watchlist's top three exactly, and in order.** The complexity
score does not — it puts the 2-regime function first and the 6-regime function eleventh.

**Honest limits.** This is a *review-cost* metric in the cleanup ladder's spirit, not a defect
predictor, and unlike the ladder it has **no ground-truth validation** — n = 1 file, and it correlates
with *fix density*, i.e. with bugs already **found**, not with bugs remaining. It is offered as a
toolkit-feedback hypothesis worth measuring on the `Objects/` defect sample, not as a ranking to act
on. It also would **not** invert for FT findings the way the score does, because it counts regimes
rather than guards — which is precisely why it is worth testing.

---

## 3. Inherent vs. reducible

### INHERENT — no refactor recommended

| Function | Why the complexity **is** the algorithm |
|----------|----------------------------------------|
| `pmerge`:3360 (score 1.1, cyc 12, 3 gotos incl. the `again:` restart) | **The published C3 linearisation merge.** The `goto again` at 3415 is the algorithm's restart-after-accept step and `goto skip` at 3402 is its "candidate appears in some tail" rejection. Rewriting these as loops would rename the states, not remove them. |
| `check_duplicates`:3269 (nesting **5** — deepest in pass 2) | O(n²) duplicate scan over the bases tuple. Nesting 5 in 23 lines is two nested index loops plus the error-message construction. |
| `set_mro_error`:3309 (score 1.1, cyc 13) | Builds the "Cannot create a consistent MRO" message. Complexity is string assembly for a diagnostic; it runs only on the failure path. |
| `_Py_type_getattro_stackref`:6570 (score **2.1**, #1 in pass 2) | The four-level descriptor-precedence lattice **is** the Python attribute-lookup protocol: metatype data descriptor → type dict (with a `staticmethod` fast path) → metatype non-data descriptor → metatype ordinary attribute → `AttributeError`. Its 6 gotos all target one `done:` label (6685) and the three `_PyCStackRef`s are popped there unconditionally — the *best* available shape. High score, **inherent**. |
| `_PyType_LookupByVersion`:1343 (12 switch cases) | A generated dispatch over pre-defined version tags. Table-driven by design. |
| `object_set_class_world_stopped`:7699 | 37 counted lines, of which ~50 source lines are a decade-old policy comment (7704–7752) explaining the `HEAPTYPE`/`ModuleType` allowlist. The comment is the value. |
| `compatible_for_assignment`:7627 (cyc 16, 4 gotos) | Layout/slot compatibility checking. Every branch is a distinct documented incompatibility. |
| `object_getstate_default` / `reduce_newobj` / `_PyObject_GetNewArguments` (R26) | Pickle protocol enumeration. Also the **coldest** region in pass 2 — 2 crash fixes in 3 years. No refactor, and no review budget. |

### REDUCIBLE — three, each with the invariant that becomes locally checkable

#### R1. `_PyType_LookupStackRefAndVersion`:6305 — split the preprocessor fork behind one name
**Score 1.6 · 5 regimes · watchlist #2 · 10 lifetime crash fixes · zero scanner findings (Cell B2).**

Lines 6311–6346 are a `#ifdef Py_GIL_DISABLED` / `#else` fork implementing the *same* cache-hit test
with two different protocols. The FT arm is a `while (1)` seqlock read with acquire loads, an inner
`return` (6324), a `PyStackRef_XCLOSE(*out)` + implicit retry (6326), and **two `break`s** (6330,
6335) that fall into the shared tail. The default arm is a straight-line compare with one `return`.

**Strategy.** Extract `static int try_type_cache_hit(struct type_cache_entry *entry, PyTypeObject *type, PyObject *name, _PyStackRef *out, unsigned int *version)`
with two implementations behind one `#ifdef`, returning `1` = hit (out set, version set), `0` = miss
(out untouched). The 40-line fork collapses to a 4-line call in the body.

**Invariant that becomes locally checkable:** *control reaching line 6347 implies exactly one of
{returned a cached value, cache miss}, and on cache miss `*out` is untouched.* Today proving that
requires tracing the FT arm's `XCLOSE`-then-retry at 6326 (which does dirty `*out`, then loops) and
distinguishing it from the two `break`s (which do not). That trace has to be redone by every reviewer
of every perf patch — and there have been two in 2026 alone.

**Estimated reduction:** score 1.6 → ~1.1; regimes 5 → 3 in the body (seqlock and atomics move into
the helper); the `while(1)` with 3 distinct exits disappears from the caller entirely.

#### R2. `type_setattro`:6749 — one function doing three jobs, sharing one epilogue
**Score 1.7 · 5 regimes · watchlist #3 · the only pass-2 function with 2 crash fixes in 12 months ·
zero scanner findings (Cell B1).**

Three jobs: **(a)** validation + name interning (6754–6785), **(b)** metatype data-descriptor
delegation (6802–6810), **(c)** dict update + slot update under two different locks (6812–6838). The
`goto done` at 6808 jumps from (b) over (c) to an epilogue that releases `name`, `descr` **and
`old_value`** — but `old_value` is only ever written by (c). Two of the three cleanup targets are
provably dead on the (b) path.

**Strategy.** Extract `static int setattro_intern_name(PyObject **name)` covering 6768–6785 (the
exact-unicode fast path, the `_PyUnicode_Copy` path, the intern, and the `MemoryError` path).

**Invariant that becomes locally checkable:** *`name` is exact-unicode, interned, and owned by this
frame at every point where a lock is held.* This is not cosmetic — it is the stated precondition of
`type_update_dict`'s load-bearing comment at 6697–6702: *"Because we have an exact unicode and our
dict has exact unicodes we know that this will all complete without releasing the locks."*
`type_update_dict` calls `_PyType_Modified_Unlocked` (6712) between the dict read and the dict write,
and its no-re-entrancy argument rests entirely on that property. **The property is established in
`type_setattro` across three branches and consumed 40 lines later inside a different function, on the
far side of `BEGIN_TYPE_DICT_LOCK`.** That is the exact shape in which a future perf patch silently
invalidates a distant correctness argument.

**Estimated reduction:** score 1.7 → ~1.2; the epilogue's three cleanup targets become two live paths
instead of one path with two dead targets.

#### R3. `_PyType_Modified_Unlocked`:1165 — hoist the watcher dispatch out of the middle
**Score 1.3 · 6 regimes — the most in the whole file · watchlist #1 · history's single
highest-priority pass-2 target (§4.2).**

Three jobs, and the middle one is arbitrary user code: **(i)** recursive invalidation descent over
`tp_subclasses` (1195–1209, the CPY-0087 `self_recursion` site at 1206); **(ii)** type-watcher
callback dispatch (1211–1231) — user C callbacks plus `PyErr_FormatUnraisable`, which the in-body
comment at 1219–1220 concedes "is potentially re-entrant and the watcher callback might be too";
**(iii)** this type's own version-tag and `_spec_cache.getitem` clearing (1233–1239).

Job (ii) runs **after** every subclass has been invalidated but **before** this type's own version tag
is cleared. Nothing states whether that ordering is required. A watcher callback observing a type
whose subclasses are all invalidated but whose own `tp_version_tag` is still live is either fine or a
bug, and the code does not say which.

**Strategy.** Move the watcher loop into `static void notify_type_watchers(PyTypeObject *type)` and
call it **after** `set_version_unlocked(type, 0)`.

**Invariant that becomes locally checkable:** *a watcher callback never observes a type whose
subclasses are invalidated but whose own version tag is not.* If the move is safe, the ordering was
incidental and the function drops from 6 regimes to 5 in 30 lines. If it breaks, the ordering is a
real invariant that should be commented and asserted. Either way **the question becomes askable**,
which it currently is not.

**Do not read this as a defect claim.** Per the calibration this is a recursion + free-threading
function and this document's score is not admissible as severity input for it. This is a
reviewability recommendation only; the verdict belongs to lock-discipline-checker and
ft-race-scanner, who history §4.2 says should be believed here.

**Estimated reduction:** score 1.3 → ~1.1; regimes **6 → 5** in the descent, with the user-callback
regime isolated in a 20-line leaf.

---

## 4. Where complexity is actively OBSTRUCTING review

Pass 1's result: 6 of its 9 top findings sat in lines a recent commit had already audited for a
*different* defect class. History §3.1/§3.2 shows the pass-2 setup is worse, not better — **the last
18 months of change in R19, R21 and R37 is exclusively performance work** (five commits: gh-145685 ×3,
gh-139103 ×2, gh-148906), every one of which *removed or narrowed* synchronization, and **none of
which was a race audit**. Below: which functions will keep absorbing missed defects, and the specific
structural reason.

### (a) `type_setattro`:6749 — an FT-only block spliced into the middle of a linear function
**Reason: a `#ifdef Py_GIL_DISABLED` hunk inserted at a point unrelated to its own effect.**

Lines 6791–6800 sit between the metatype assertions (6788–6789) and the descriptor lookup (6803):

```c
#ifdef Py_GIL_DISABLED
    // gh-139103: Enable deferred refcounting for functions and descriptors
    // assigned to type objects. ...
    if (value != NULL && (PyFunction_Check(value) || Py_TYPE(value)->tp_descr_get != NULL))
    {
        PyUnstable_Object_EnableDeferredRefcount(value);
    }
#endif
```

This mutates a **caller-supplied object's refcounting mode**, before any lock is taken and before any
check that the assignment will succeed. It is reviewed as an isolated hunk answering "does this
improve `dataclass.__init__` scaling" (`ce791541769a`, 2025-11-19). The question it is positioned to
hide — *what happens to `value` when the assignment then fails at 6807, 6824 or 6830* — is never
prompted, because nothing in the hunk's neighbourhood is about failure. This function has **2 crash
fixes in the last 12 months**, more than any other in pass 2, and **zero scanner coverage** (Cell B1).

### (b) `_PyType_LookupStackRefAndVersion`:6305 — macro-hidden control flow, two different shapes per build
**Reason: brace-introducing macros that vanish entirely on the default build.**

`BEGIN_TYPE_LOCK()` / `END_TYPE_LOCK()` at 6358/6362 expand to
`Py_BEGIN_CRITICAL_SECTION_MUTEX(TYPE_LOCK)` / `Py_END_CRITICAL_SECTION()` (definitions at 79–80) —
**brace-introducing**, so 6358–6362 is a lexical scope the source does not display. On the default
build (definitions at 184–195) they expand to **nothing**, as do `ASSERT_TYPE_LOCK_HELD`,
`ASSERT_NEW_TYPE_OR_LOCKED`, `types_stop_world` and `types_start_world`. Combined with the
`#ifdef` fork at 6311–6346, a reviewer on a GIL build and a reviewer on an FT build are reading two
different control-flow graphs from the same 80 lines. This is the **most crash-fixed function in
pass 2** (10 lifetime, 7 in 3 years) and it was rewritten **twice for performance in 2026**
(`cd5217283112` "Improve scaling of type attribute lookups", `f26eca7732ca` "return a `_PyStackRef`").

### (c) `_PyType_Modified_Unlocked`:1165 — a contract enforced by an assert that exists in one build configuration
**Reason: the invariant is machine-checkable only under FT **and** debug, while the caller is in another file.**

```c
    ASSERT_NEW_TYPE_OR_LOCKED(type);
#ifdef Py_GIL_DISABLED
    // This function is re-entrant and it's not safe to call it
    // with the world stopped.
    assert(!types_world_is_stopped());
#endif
```

`assert(!types_world_is_stopped())` (1187) is the **only** mechanical statement of the contract, and
it compiles out on release builds and on every non-FT build. `ASSERT_NEW_TYPE_OR_LOCKED` is likewise
a no-op outside FT (line 191). Its new external caller — `Objects/dictobject.c:1971`, added by
`20921922fa4` **19 days before HEAD** as "take 2" of a change that previously **deadlocked** and then
**raced** — is in a different translation unit and cannot see any of this. The recursion at 1206
re-checks the assert at every level, but the **watcher callback at 1222 can re-enter from arbitrary
user C code with no assert on that path at all**. Six regimes, 45 lines, score 1.3 — invisible to
every ranking in this document.

### (d) `find_name_in_mro`:6144 — the defect and the design are visually identical
**Reason: a documented "never sets an exception" contract makes every `PyErr_Clear()` look intentional.**

The header comment (6140–6143) states *"It never set an exception."* Three `PyErr_Clear()` calls
(6149, 6158, 6183) exist to uphold it. Two of them are flagged (`error_paths@6149`,
`error_paths@6158` + `pyerr_clear@6158`), and 6158 is the interesting one:

```c
        if (!is_readying(type)) {
            if (PyType_Ready(type) < 0) {
                PyErr_Clear();          /* <-- swallows whatever PyType_Ready raised */
                return -1;
            }
```

`PyType_Ready` can fail with `MemoryError`, and the clear is indistinguishable in shape from the two
legitimate ones. A reviewer changing the return convention to `_PyStackRef` (`f26eca7732ca`,
2026-03) is checking refcounts, not asking which of three identical-looking clears is load-bearing.
**Four live findings — the densest cluster in pass 2 — on a function at the score floor with 9
lifetime crash fixes.** This is the clearest single instance in the slice of complexity ranking
pointing away from the evidence.

### (e) `mro_invoke`:3590 / `mro_internal`:3654 — a safety property encoded in the *lifetime of a local*
**Reason: an ABA guard written as a comment plus a `Py_XNewRef` whose only purpose is to reserve an address.**

```c
    /* Keep a reference to be able to do a reentrancy check below.
       Don't let old_mro be GC'ed and its address be reused for
       another object, like (suddenly!) a new tp_mro.  */
    old_mro = Py_XNewRef(lookup_tp_mro(type));
    new_mro = mro_invoke(type);  /* might cause reentrance */
    reent = (lookup_tp_mro(type) != old_mro);
```

Re-entrancy — from a user-supplied `mro()` (3603 `call_method_noarg`), from a descriptor found while
looking up `mcls.mro`, or from a finalizer of `mro()`'s return value, all three enumerated in the
3579–3588 comment — is detected by **pointer identity**, and the correctness of that comparison
depends entirely on the `Py_XNewRef` at 3665 preventing address reuse. Both functions score **1.0**;
both are watchlist top-10 with **5 crash fixes each in 3 years**; `mro_invoke` additionally carries
cleanup ladder 6 across a user callback. No metric in this document and no scanner in the baseline
can see an invariant of this shape.

### Which regions will keep producing missed defects: **R19 + R21 as a pair**

The three conditions all hold simultaneously and only here:
1. **25 of pass 2's 69 three-year crash fixes** land in R19+R21, in 635 lines (§1a of history).
2. The last 18 months of change there is **entirely performance work that narrowed synchronization**
   (§3.2) — five commits, no correctness audit among them.
3. The two functions that work touched most (`type_setattro`, `_PyType_LookupStackRefAndVersion`) are
   **#2 and #3 by complexity, tied #1 by fix density, top-3 by interleaved regimes, and have zero
   scanner findings against them because of three separately-demonstrated denominator defects**
   (Cell B).

Everything that produced pass 1's "audited for a different defect" result is present here in a
stronger form.

---

## 5. Classification

| Verdict | Item |
|---------|------|
| **CONSIDER** | **`_PyType_LookupStackRefAndVersion`:6305 (R1)** — extract `try_type_cache_hit()` behind the `#ifdef` fork. Makes "control at 6347 ⇒ cache miss and `*out` untouched" locally checkable. Score 1.6 → ~1.1. |
| **CONSIDER** | **`type_setattro`:6749 (R2)** — extract `setattro_intern_name()`. Makes "`name` is exact-unicode, interned and owned wherever a lock is held" — the stated precondition of `type_update_dict`'s re-entrancy argument at 6697–6702 — locally checkable. Score 1.7 → ~1.2. |
| **CONSIDER** | **`_PyType_Modified_Unlocked`:1165 (R3)** — hoist the watcher dispatch after `set_version_unlocked(type, 0)`. Makes the ordering between subclass invalidation and watcher notification an explicit, testable claim. **Reviewability only — not a severity claim; the score is inadmissible for this function's class.** |
| **CONSIDER** | `mro_invoke`:3590 — cleanup ladder 6 (2 owned locals hand-released at 3 exits) spanning a user-supplied `mro()` call. The only ladder row in pass 2 that lands on a watchlist function. → refcount auditor. |
| **ACCEPTABLE** | `pmerge`, `check_duplicates`, `set_mro_error`, `_Py_type_getattro_stackref`, `_PyType_LookupByVersion`, `object_set_class_world_stopped`, `compatible_for_assignment`, and all of R26. **Explicitly no refactor recommended** — the complexity is the published algorithm, the protocol, a dispatch table, or a policy comment. |
| **POLICY** | **The pass-2 region map has a hole at 6453–6528** containing `_PyType_SetFlags`, `set_flags_recursive` and `_PyType_SetFlagsRecursive` — i.e. history §4.3's **High**-priority sibling-gap item is outside every scoped region. Extend R19 to 6528. |
| **POLICY** | **Do not route pass-2 agent budget by this document's ranking.** 19 of the 25 history-prioritized functions sit at the score floor; three of the top seven complexity slots are in R26, the region history and `RUN_CONTEXT.md` both call cold. Route by the history watchlist and the Cell A/B tables in §1. |
| **TOOLKIT — recall gap** | `scan_ft_races` reports **`lock_held_functions: 0`** on a file with **23** independently-counted lock-taking functions. Its lock-held rule family did not fire once. This is why the two most crash-fixed functions in pass 2 have no FT finding. |
| **TOOLKIT — recall gap** | `scan_stw_safety` reports **`stw_functions: 3`**; **8** functions call `types_stop_world()` (`set_tp_mro`, `type_set_abstractmethods`, `type_set_bases_unlocked`, `apply_type_slot_updates`, `_PyType_SetFlags`, `_PyType_SetFlagsRecursive`, `object_set_class`, `PyType_Freeze`). **3-of-8 recall.** History §4.4's target `object_set_class` is in the missing five. |
| **TOOLKIT — structural zero** | `scan_gil_usage`: `vocabulary_resolved: 0` with `vocabulary_tokens_seen: 4`. Its 0 findings carry **no** information about this file. |
| **TOOLKIT — clean negative, certified** | `scan_lock_discipline`: 0 findings with `critical_section_functions: 23` — and I independently counted exactly **23** lock-taking functions. The denominator is correct, so this zero is a **real** clean negative for the critical-section-leak class in this file. |
| **TOOLKIT — proposal** | An **interleaved-regime count** (§2c) recovered the history watchlist's top three exactly and in order, where the complexity score did not. Unvalidated against defect ground truth (n = 1 file, correlates with fixes *found*). Worth measuring on the `Objects/` 25-defect sample before adopting. |

---

## 6. Method and limits

Metrics from `plugins/cpython-review-toolkit/scripts/measure_c_complexity.py`, run with
`/home/danzin/venvs/cpython-review-toolkit/bin/python` on
`/home/danzin/projects/cpython/Objects/typeobject.c`.

- **Coverage: 99.1%** — 427 brace blocks seen, **423 parsed**, 4 signatures unparsed, 29 multi-line
  signatures. The 4 unparsed are the `SLOT*` macro dispatchers in R32 (pass-1 territory) and do not
  affect this ranking.
- File-wide: hotspot threshold **1.7** (top 2%), max score **6.8**, 9 hotspots, avg cyclomatic 4.9,
  avg length 17.9, max nesting 5.
- **Pass-2 subset: 103 of 423 functions, max score 2.1, only 4 at or above the file threshold.**
- Exit counts, goto labels, lock-op counts, atomic counts and regime counts were computed separately
  over comment-stripped function bodies and are **not** part of the script's output.
- Finding→function attribution was done by mapping every line in `<run>/scanners/*.sample.json` into
  the complexity output's function spans (45 findings collected across 13 scanners).
- Region boundaries are those given in `RUN_CONTEXT.md` / `history_context.md` §1a.

**What this document cannot do.** It cannot clear anything. 19 of the 25 functions history
independently prioritized score at the floor, and the highest-scoring pass-2 function is the least
concurrency-entangled of its peers. Every function not named above remains exactly as suspect as
before this ran. What is delivered is a reading order (§1, Cell B first), three reducibility
proposals with the invariant each would expose (§3), five named structural reasons review keeps
missing things here (§4), and three measured scanner-denominator defects that explain the silence
over the file's two most-fixed functions (§5).
