# c-complexity-analyzer — obj-mappings slice

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777` (read directly at the target ref; no
build involved, so no build/ref diff applies).
Scope: exactly `Objects/dictobject.c` (8,597 lines) and `Objects/setobject.c` (3,228 lines).
Tool: `measure_c_complexity.py`, run per-file and merged.

---

## 1. Denominator

| | dictobject.c | setobject.c | merged slice |
|---|---|---|---|
| lines | 8,597 | 3,228 | 11,825 |
| **functions parsed** | **291** | **112** | **403** |
| extraction coverage (tool-reported) | 100.0% | 100.0% | 100.0% |
| **coverage independently verified** | **291 / 291** | **112 / 112** | **403 / 403** |
| avg cyclomatic | 4.2 | 4.5 | 4.3 |
| avg line count | 16.7 | 15.2 | 16.3 |
| max nesting | 6 | 4 | 6 |
| max score | 3.6 | 1.8 | 3.6 |
| hotspots (top 2%) | 8 | 4 | 8 |
| **functions at the score floor (1.0)** | **261 (89.7%)** | **101 (90.2%)** | **362 (89.8%)** |
| distinct score values | 14 | 7 | 15 |

**Coverage is real, not asserted.** I checked the tool's `coverage_pct: 100.0` against ground
truth rather than trusting it: CPython PEP 7 opens every function body with `{` in column 0, and
`grep -cE '^\{'` returns **291** for dictobject.c and **112** for setobject.c — exactly the
parsed counts. Macro pollution is one function (`ASSERT_DICT_LOCKED`), and that is a real
`static inline void`, not a `#define`. No functions were silently dropped.

Specifically: **the `preproc_ifdef` extraction gap that cost the multidict review a whole scanner
class does not affect this toolkit's C extractor.** 28 dictobject.c functions and 4 setobject.c
functions are defined entirely inside a `#ifdef Py_GIL_DISABLED` arm, and all 32 were parsed,
including the 6 dual-build twins that appear twice under the same name (`load_keys_nentries`,
`set_keys`, `set_values`, `split_keys_entry_added`, `_Py_dict_lookup_threadsafe`,
`_Py_dict_lookup_threadsafe_stackref`). That is a positive calibration result.

Lines read for this report: roughly 900, across the top 15 by score, the 6 recorded finding
sites, the 5 lock-across-Python sites, and the top cleanup ladders.

---

## 2. The headline: complexity does **not** predict where these bugs are

Answering the question that was asked, with the actual numbers.

### 2a. The hit rate

| id | function | score | rank (merged, N=403) | in the 8-function hotspot list? |
|---|---|---|---|---|
| CPY-0015 | `dictiter_new` | **1.0 (floor)** | tied with 361 others | no |
| CPY-0019 | `frozendict_pair_hash` | **1.0 (floor)** | tied with 361 others | no |
| CPY-0079 | `copy_lock_held_untracked` | 1.8 | 12 | no |
| CPY-0096 | `insert_split_key` | **1.0 (floor)** | tied with 361 others | no |
| CPY-0107 | `_Py_dict_lookup` | 1.1 | in the 1.1 tier (10 fns) | no |
| CPY-0115 | `delitem_common` | **1.0 (floor)** | tied with 361 others | no |

**0 of 6. Four of the six sit at the exact score floor**, where the metric cannot distinguish
them from 361 other functions. Note that "rank 33" or "rank 109" for a floor-tier function is an
artifact of whatever tiebreaker you impose — the metric itself assigns no order inside that tier.

### 2b. But the hit rate is not the evidence — and I will not claim it is

With 6 findings, 403 functions and an 8-item hotspot list, the *expected* number of hits is
**0.16**, and P(observe 0 | no association whatsoever) = **0.845**. Observing zero is the
overwhelmingly likely outcome under *any* hypothesis. The 0/6 headline is a real fact about this
slice but it is not, by itself, statistically meaningful. Stating otherwise would be the same
error as certifying a clean zero on a zero denominator.

The evidence is the permutation test on the whole population (200,000 resamples, dictobject.c,
N=291, one-sided):

| metric | population mean | findings mean | p |
|---|---|---|---|
| **composite `score`** | 1.066 | 1.150 | **0.210 — not significant** |
| `cyclomatic_complexity` | 4.20 | 8.00 | 0.044 — marginal |
| `line_count` | 16.73 | 43.17 | **0.009 — significant** |

**The composite score carries no detectable signal. Its own raw inputs do.** The findings really
are longer and somewhat branchier than the average function in the file — and the scoring
function throws that away.

### 2c. Why: every threshold in the scorer is above this file's entire distribution

The score adds points only above hard cutoffs: line_count > 50, nesting > 3, cyclomatic > 10,
parameter_count > 6, goto_count > 5. dictobject.c's median function is ~11 lines with cyclomatic
3. **89.8% of the slice clears no threshold at all and lands on the 1.0 base**, and the whole
403-function population resolves into 15 distinct values. This is not a ranking; it is a
15-bucket histogram with 90% of the mass in bucket one.

Reading-order cost, stated plainly: reading in descending score order, you reach the 30
non-floor dictobject.c functions after 30 reads and then hit a wall — the remaining 4 findings
are somewhere in an unordered block of 261. Expected reads to cover all six: **~239 of 291**. A
reading order that requires 82% of the file is not a reading order.

### 2d. Where the metric *does* land, and why that is the wrong place

The one recorded finding the metric elevated at all is CPY-0079 `copy_lock_held_untracked`
(1.8, rank 12) — and it is still outside the hotspot list. Meanwhile the metric's top 4 are
dominated by a shape that is *designed*, not defective (see §5), and its #4 is a function that
does not exist in a release build.

Three of the six findings are in classes the plugin's own documentation says the metric inverts
on, and I am not using score as a severity input for any of them:

- **CPY-0019 `frozendict_pair_hash`** — recursion. Score 1.0, cyclomatic 3, 19 lines. Its guarded
  twin would score *higher* precisely because a recursion guard is itself a branch. Textbook
  inversion.
- **CPY-0096 `insert_split_key`** — free-threading. Score 1.0, cyclomatic 5.
- **CPY-0107 `_Py_dict_lookup`** — lock-order inversion. Score 1.1.

---

## 3. The structural blind spots (why this is not just a tuning problem)

Two of the six findings are **invisible in principle** to a per-function complexity metric, not
merely underweighted. Tuning thresholds will not fix these.

**CPY-0107 is split across two functions, and both halves are simple.** The lock
(`LOCK_KEYS_IF_SPLIT`) is taken in `_Py_dict_lookup` (score 1.1). The
`PyObject_RichCompareBool` that runs user Python under it is in `compare_generic` /
`compare_unicode_generic` (**score 1.0 each, cyclomatic 6 and 6**), reached indirectly through
the `check_lookup` function-pointer parameter of `do_lookup`. No single function's metrics
contain the hazard. My own intra-function scan for "lock + Python-running call in the same body"
also missed it, for exactly the same reason — I only found it by reading.

**CPY-0019's defect is an absence.** `frozendict_pair_hash` is a guardless copy of `tuple_hash`.
The bug is a missing `Py_EnterRecursiveCall`. Complexity measures what is present; it has no
term for what should be there and is not.

---

## 4. `#ifdef Py_GIL_DISABLED` density — the dual-build question

`#if/#ifdef/#elif` conditionals mentioning `Py_GIL_DISABLED`: **44 in dictobject.c, 7 in
setobject.c.**

| | dictobject.c | setobject.c |
|---|---|---|
| functions defined entirely inside a `Py_GIL_DISABLED` arm | 28 | 4 |
| functions with **internal** `Py_GIL_DISABLED` branching | **26** | **4** |
| ...of those, scoring 1.0 | 25 | 3 |
| ...of those, scoring 1.1 | 1 | 1 |
| ...of those, scoring above 1.1 | **0** | **0** |

**Every one of the 30 dual-build functions in the slice scores 1.0 or 1.1.** Not one appears in
any hotspot list, per-file or merged. The reason is mechanical: the scorer counts `if`, `for`,
`while`, `case`, `&&`, `||` — it does not count `#if`. Preprocessor branching contributes
**zero** to cyclomatic complexity, so the exact marker the campaign wants (a check live in one
arm and dead in the other) is the one thing the metric is guaranteed not to see.

`insert_split_key` (CPY-0096, reproduced, 24/24 FT hang) carries **2 internal
`#ifdef Py_GIL_DISABLED` blocks and scores 1.0**.

### The dual-build reading list (this is the actionable output of §4)

Ranked by `Py_GIL_DISABLED` ifdef count, since score is useless here. All at score 1.0–1.1.

| gil `#ifdef`s | function | dictobject.c:line | note |
|---|---|---|---|
| 3 | `free_keys_object` | 874 | |
| 2 | **`insert_split_key`** | 1942 | CPY-0096, already recorded |
| 2 | `_PyDict_GetItemRef_KnownHash` | 2574 | |
| 2 | `_PyDict_Contains_KnownHash` | 5312 | |
| 2 | `_PyObject_MaterializeManagedDict` | 7378 | |
| 2 | `ensure_managed_dict` | 8038 | |
| 2 | `ensure_nonmanaged_dict` | 8070 | |
| 1 | `_Py_dict_lookup` | 1358 | CPY-0107, already recorded |
| 1 | `unicodekeys_lookup_split` | 1270 | FT arm calls `unicodekeys_lookup_unicode_threadsafe` then re-locks on `DKIX_KEY_CHANGED`; GIL arm has neither path |
| 1 | `_PyDict_GetMethodStackRef` | 1782 | |
| 1 | `_PyObject_StoreInstanceAttribute` | 7551 | |
| 1 | `_PyObject_TryGetInstanceAttribute` | 7634 | |
| 1 | `_PyObject_SetManagedDict` | 7871 | |
| 1 | `get_next_dict_keys_version` | 8146 | |

plus 12 more at 1 ifdef in dictobject.c, and in setobject.c: `set_zero_table` (195),
`free_entries` (467), `set_contains_entry` (565), `set_swap_bodies` (1511).

Note that **2 of these 14 are already recorded findings** — a 14% hit rate on a 14-item list,
versus 0% on the 8-item complexity hotspot list. On this slice, `#ifdef Py_GIL_DISABLED` density
is a strictly better reading order than the complexity score. That is a cheap, concrete
proposal, quantified in §7.

---

## 5. The lock-across-Python correlation

I scanned every function for a lock acquisition (`LOCK_KEYS`, `LOCK_KEYS_IF_SPLIT`,
`Py_BEGIN_CRITICAL_SECTION[2]`, `PyMutex_Lock`) co-occurring with a call that can run arbitrary
Python. **43 of 291 dictobject.c functions and 22 of 112 setobject.c functions take a lock**;
of those, 4 and 3 respectively also contain a Python-invoking call in the same body.

| function | score | rank | lock kind | Python-running call under it |
|---|---|---|---|---|
| `_PyDict_FromKeys` | 3.6 | **1** | `Py_BEGIN_CRITICAL_SECTION[2]` | `PyIter_NextItem`, `PyObject_SetItem`, `_PyObject_CallNoArgs` |
| `dict_merge` | 2.2 | **3** | `Py_BEGIN_CRITICAL_SECTION[2]` | `PyMapping_Keys`, `PyObject_GetIter`, `PyIter_Next`, `PyObject_GetItem`, `dict_contains` |
| `_PyDict_CheckConsistency` | 2.1 | **4** | `LOCK_KEYS_IF_SPLIT` | `PyObject_Hash` |
| **`insert_split_key`** | **1.0** | floor | **`LOCK_KEYS`** | `_PyType_Modified_Unlocked` |
| `_Py_dict_lookup` + `compare_generic` | 1.1 / 1.0 | — | `LOCK_KEYS_IF_SPLIT` | `PyObject_RichCompareBool` (inter-procedural) |

At first glance this looks like the metric working: three of five are in the top 4. **It is not,
and the reason matters for the whole campaign.**

The discriminator between these sites is the **lock kind**, which complexity does not model at
all:

- `Py_BEGIN_CRITICAL_SECTION` is **detach-capable**. A thread that blocks inside one releases it
  and re-acquires on resume. Holding it across Python code is the designed behavior. That is
  what `_PyDict_FromKeys` and `dict_merge` do, and it is why they are big — the *dispatch* is
  what makes them big, not the hazard.
- `LOCK_KEYS` is `PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)` (dictobject.c:227).
  Non-reentrant, non-detaching. Running Python under it is what hangs, and dictobject.c:218–227
  says so explicitly, naming `PyType_Modified`.

So the honest reading: **the metric ranked highly the two sites where the pattern is safe by
design, and put the one site where it is a reproduced hang at the floor.** Per the pass-2 lesson
about guarded twins — `_PyDict_FromKeys`'s critical section defends a different threat model
than `insert_split_key`'s keys mutex, and citing the former's structure as evidence about the
latter would be exactly the mistake that lesson warns against.

`_PyDict_CheckConsistency` is a third category: it is invoked only through
`ASSERT_CONSISTENT(op)`, which is `assert(_PyDict_CheckConsistency(...))` (dictobject.c:666–668)
and therefore compiled out under `NDEBUG`. **The metric's #4 hotspot does not exist in a release
build.**

---

## 6. Top 5 — inherent or reducible

Merged ranking. All 8 merged hotspots are in dictobject.c; see §7 for why that silences
setobject.c.

#### 1. `_PyDict_FromKeys` — dictobject.c:3459 — score 3.6 (149 lines, nest 3, cyc 34, 15 gotos) — **CONSIDER, reducible**

The only function in the slice above 2.4, and the gap to #2 (2.4) is larger than the gap from #2
to #30. Its complexity is a **dispatch matrix**, not an algorithm: `d ∈ {exact dict, exact
frozendict, other}` × `iterable ∈ {exact dict, exact frozendict, anyset, other}`, written out as
six near-identical `Py_BEGIN_CRITICAL_SECTION*` / `dict_dict_fromkeys` / `dict_set_fromkeys` /
`goto Done` blocks that differ only in which object(s) get locked and which of two helpers is
called. Three further copies of the iterate-and-insert loop follow, differing only in the setitem
helper.

Concrete reduction: collapse the six fast-path blocks into one, by selecting `(helper,
lock_target_a, lock_target_b)` up front and using `Py_BEGIN_CRITICAL_SECTION2(a, b)` with `b == a`
for the single-lock cases; then fold the three tail loops into one parameterised by the setitem
helper. Estimated **cyclomatic 34 → ~14, lines 149 → ~70, score 3.6 → ~1.9**.

Caveat, and it is the point of this whole report: reducing this score would not have found any
bug. The value here is maintainability, not defect yield.

#### 2. `_Py_dict_lookup_threadsafe` — dictobject.c:1600 — score 2.4 (78 lines, nest 4, cyc 17, 10 gotos) — **ACCEPTABLE, inherent**

Every one of the 10 gotos targets `read_failed`, and each is a distinct optimistic-read
validation: `ix == DKIX_KEY_CHANGED`, `values == NULL`, `ix >= capacity`, `_Py_TryXGetRef`
failure, and a re-check that `ma_values` / `ma_keys` did not change under the read. This is
lock-free retry logic; the branch count *is* the correctness argument. Per the plugin's own
guidance, the `goto` ladder here is a positive signal — the retry exit is written once. Do not
touch.

#### 3. `dict_merge` — dictobject.c:4299 — score 2.2 (79 lines, nest 6, cyc 14, 6 gotos) — **ACCEPTABLE (structure), with a handoff**

The nesting depth of 6 — the deepest in the slice — comes from `for (PyIter_Next) { if (override
!= 1) { if (status != 0) { if (status > 0) { if (dupkey == NULL) …`, a four-level error triage
inside the generic slow path. Mildly reducible by hoisting the `status` triage into a helper, but
the shape is conventional CPython and it is well-covered.

Handoff, not my class to adjudicate: the generic path holds `Py_BEGIN_CRITICAL_SECTION(a)` across
`PyMapping_Keys(b)`, `PyObject_GetIter`, `PyIter_Next`, `dict_contains(a, key)` and
`PyObject_GetItem(b, key)` — all arbitrary Python, with `mp` cached in a local across every one
of them. The critical section is detach-capable so this is not CPY-0096's shape, but it is the
brief's "what state was cached in a local across the call" question. Worth a read by the
re-entrancy agent.

#### 4. `_PyDict_CheckConsistency` — dictobject.c:697 — score 2.1 (80 lines, nest 5, cyc 23) — **POLICY, exclude from ranking**

Cyclomatic 23 — second-highest in the slice — because it is a wall of invariant checks. It is
correct that it is branchy; it is wrong that it occupies a hotspot slot, because
`ASSERT_CONSISTENT` wraps it in `assert()` and it vanishes under `NDEBUG`. It cannot harbour a
shipped defect. See §7 for the proposal.

#### 5. `dictiter_iternext_threadsafe` — dictobject.c:6062 — score 2.1 (83 lines, nest 3, cyc 16, 8 gotos) — **ACCEPTABLE, inherent**

Same shape as #2: 8 gotos to a `concurrent_modification` label, each a distinct optimistic-read
validation (`di_used` vs. atomic `ma_used`, `values == NULL`, split-table bounds, `_Py_TryXGetRef`,
`ma_keys` re-check). Inherent to lock-free iteration.

#### Honourable mention — `set_add_entry_takeref` — setobject.c:252 — score 1.8, **#1 in setobject.c** — **ACCEPTABLE, and the inversion made visible**

This is the documented restart loop the brief names as the model. Its `restart:` label, its
`table = so->table` snapshot, and its `if (table != so->table || entry->key != startkey) goto
restart;` revalidation after `PyObject_RichCompareBool` are *the guard*. It scores 1.8 — highest
in setobject.c — **because it is correct**. Its unguarded siblings score lower. This is the
metric inversion the plugin documents, observed directly on this slice rather than cited from a
prior sample: on the re-entrancy class, a higher score is weak evidence of *safety*, not danger.

#### Also inherent: `do_lookup` (1112) and `set_do_lookup` (220)

Open-addressing probe loops with manual unrolling. Genuinely branchy; leave alone. But
`do_lookup`'s *score* is fabricated — see §7.

---

## 7. Toolkit feedback

### 7a. FIX — function-pointer parameters inflate `parameter_count`, and it fabricated a hotspot

`do_lookup` (dictobject.c:1112) has **5** parameters:

```c
do_lookup(PyDictObject *mp, PyDictKeysObject *dk, PyObject *key, Py_hash_t hash,
          int (*check_lookup)(PyDictObject *, PyDictKeysObject *, void *, Py_ssize_t ix, PyObject *key, Py_hash_t))
```

`measure_c_complexity.py` reports **10** — the 4 leading scalars plus the 6 parameter *types of
the function pointer*, with the pointer itself dropped. Reproduced in isolation:

```c
static int
five_params(int a, int b, int c, int d, int (*cb)(int, int, int, int, int, int))
{ return a + b + c + d + cb(a,b,c,d,a,b); }
```
→ `parameter_count=10` (ground truth 5), `score=2.0` (correct value 1.0).

Impact on this slice: `do_lookup`'s entire score is the spurious penalty. 36 lines (< 50 → +0),
nesting 3 (not > 3 → +0), cyclomatic 10 (not > 10 → +0), 0 gotos (+0), and
`min((10-6)*0.3, 1.0) = 1.0`. **Correct the parameter count and `do_lookup` scores 1.0 — the
floor — and drops out of the hotspot list.** One of the slice's 8 merged hotspots is a pure
artifact.

`set_do_lookup` is *not* affected (7 real parameters, 7 reported) because its callback is a
`compare_func` typedef rather than an inline pointer type — which confirms the defect is
specifically inline function-pointer parameter declarations. Fix: when walking
`parameter_declaration` children, do not descend into a nested `function_declarator`'s own
`parameter_list`; count the declarator as one parameter.

### 7b. FIX — the score's absolute thresholds have no resolution on the object layer

89.8% of the slice at the floor; 403 functions into 15 buckets. The thresholds (50 lines / depth
3 / cyclomatic 10 / 6 params / 5 gotos) are calibrated for functions far larger than CPython's
object-layer code, whose median function is ~11 lines.

The measured consequence is not merely aesthetic: **`line_count` alone separates the findings
from the population at p = 0.009 and `cyclomatic_complexity` at p = 0.044, while the composite
score built from them scores p = 0.210.** The thresholding destroys signal that its own inputs
carry. That is a strong argument for a **percentile-relative score** (per-file z-score or rank
across the scanned population) either replacing the absolute score or shipping alongside it.
`--top-percent` already selects relatively; the score it selects *on* does not.

### 7c. FIX — count preprocessor conditionals

`#if` / `#ifdef` / `#elif` contribute 0 to cyclomatic complexity. On a slice with 51
`Py_GIL_DISABLED` conditionals, that means **all 30 dual-build functions score 1.0–1.1** and none
can ever surface. Proposal: add `preproc_ifdef` / `preproc_elif` nodes to the decision-point
count, and emit a separate `preproc_branch_count` field (and specifically
`gil_disabled_ifdef_count` for CPython) so the dual-build reading order in §4 is a first-class
output rather than something an agent has to compute by hand. On this slice that list had a 2/14
hit rate against recorded findings versus 0/8 for the complexity hotspots.

### 7d. CONSIDER — exclude assert-only functions from hotspot selection

`_PyDict_CheckConsistency` occupies merged hotspot slot #4 and does not exist in a release build.
It is reachable only via `assert(...)`. A cheap heuristic — a function whose only in-file callers
are inside `assert(` — would free the slot. Combined with 7a, **2 of the 8 merged hotspots on this
slice are noise** (one artifact, one debug-only).

### 7e. CONSIDER — `--top-percent` is scope-sensitive in a way that silently drops files

Run per-file, setobject.c gets 4 hotspots (top: `set_add_entry_takeref`, 1.8). Run merged with
dictobject.c, the top-2% cut lands at score 2.0 and **all 8 hotspots are in dictobject.c —
setobject.c contributes zero.** Nothing in the output says "one file in your scope produced no
hotspots because another file outscored it." On a multi-file slice that is a silent denominator
problem of exactly the kind the campaign is trying to eliminate. Proposal: emit per-file hotspot
counts in `summary`, or offer `--top-percent-per-file`.

### 7f. Recall gap — the metric is structurally blind to inter-procedural hazards

CPY-0107's lock and its Python-invoking call are in different functions, each individually
simple, connected by a function pointer. No per-function metric can express this. This is not a
tuning issue and I am not proposing a fix to the complexity scanner — I am recording it so the
campaign does not later read a clean complexity report as coverage of this class. It also defeated
my own intra-function lock+Python-call scan; I found it only by reading `_Py_dict_lookup`'s
callees.

### 7g. Positive — coverage is honest, and the `preproc_ifdef` gap is absent

`coverage_pct: 100.0` verified against independent ground truth on both files, including all 32
functions inside `#ifdef Py_GIL_DISABLED` arms and all 6 dual-build twins. The multidict-era
extraction gap does not apply to this toolkit's C extractor. Worth locking in a regression test.

---

## 8. Reading order for the other agents

Since the complexity score is not a usable reading order on this slice, here is what I would
hand over instead. Complexity rank is given only to show how badly it disagrees.

**A. Cleanup-ladder sites (`goto_count == 0`, several owned locals, many early returns).**
The two metrics genuinely disagree here: every one of the merged top-8 complexity hotspots has
ladder ≤ 2, and the top ladders are at or near the floor.

| ladder | function | file:line | score | cx rank | why it is worth reading |
|---|---|---|---|---|---|
| 8 | `set_difference_untracked` | setobject.c:2083 | 1.6 | 17 | holds a live `set_next` cursor (`pos`, `entry`) across `_PyDict_Contains_KnownHash` / `set_contains_entry`, both of which run user `__eq__`/`__hash__`. This is CPY-0115's shape on the set side. |
| 4 | `dict_equal_lock_held` | dictobject.c:4662 | **1.0** | 43 | caches `i` and an `ep` pointer into `a->ma_keys` across `_Py_dict_lookup(b, …)`, which reaches `PyObject_RichCompareBool`. Both dicts are locked, but the guard defends against another *thread*, not against same-thread re-entry mutating `a`. |
| 4 | `dictviews_isdisjoint` | dictobject.c:6930 | 1.1 | 34 | `it`/`item` across `PySequence_Contains(self, item)`; cleanup hand-copied at 4 exits. |
| 4 | `set_issubset_impl` | setobject.c:2413 | 1.0 | 63 | |
| 4 | `set_update_iterable_lock_held` | setobject.c | 1.0 | 116 | |
| 4 | `dictviews_xor` | dictobject.c | 1.0 | 122 | |
| 4 | `dictviews_sub` | dictobject.c | 1.0 | 185 | |
| 3 | `set_isdisjoint_impl` | setobject.c:1920 | 1.1 | 32 | |
| 3 | `set_difference_update_internal` | setobject.c:1980 | 1.1 | 33 | |

**B. The 30 dual-build functions in §4**, ranked by `Py_GIL_DISABLED` ifdef count.

**C. `set_intersection` (setobject.c:1718)** — the sibling of `set_add_entry_takeref` that has
*no* restart loop. It swaps `so`/`other` by size, then reads `PySet_GET_SIZE(so)` and passes `so`
to `set_contains_entry` inside a `PyIter_Next(it)` loop. `set_contains_entry` re-reads
`so->table` each call, so this is not obviously broken — but it is the exact asymmetry the brief
asks about, and it scores #2 in setobject.c, so it is cheap to read.

---

## 9. Classes bounded

- **Complexity hotspots, merged slice: 8 of 403 functions (top 2%, threshold score 2.0).** All 8
  read. **2 are noise** (`do_lookup` = parameter-count artifact §7a; `_PyDict_CheckConsistency` =
  assert-only §7d), **3 are inherent lock-free retry or probing logic** and should not be touched,
  **1 is genuinely reducible** (`_PyDict_FromKeys`), 2 are moderate.
- **Reducible accidental complexity: 1 of 403 functions.** `_PyDict_FromKeys`, 3.6 → ~1.9. No
  other function in the slice has enough accidental complexity to be worth a patch. This is a
  well-factored pair of files; the complexity that exists is largely load-bearing.
- **Functions taking a lock: 43/291 (dict), 22/112 (set).** Of those, 4 and 3 also contain a
  Python-invoking call in the same body; all 7 read (§5). One inter-procedural case
  (CPY-0107) found only by reading — the intra-function denominator does **not** bound this class,
  and I am not claiming it does.
- **Dual-build functions: 30 of 403 with internal `Py_GIL_DISABLED` branching, plus 32 defined
  entirely inside an FT arm.** All enumerated; none surfaced by score; none individually
  adjudicated here (not my class) — handed off in §4.
- **Extraction: 403/403 verified.** No dropped functions, no `preproc_ifdef` gap.
- **Zero crash claims in this report**, therefore zero reproductions attempted. Nothing here is
  asserted as a defect on the basis of a complexity score.

**Bottom line for the campaign.** On obj-mappings, the complexity score is not a usable reading
order and must not be used as a severity input. Where it disagrees with the cleanup-ladder
counter-metric it is the ladder that points at the re-entrancy sites, and where it disagrees with
raw `#ifdef Py_GIL_DISABLED` density it is the ifdef count that points at recorded findings. The
score's own raw inputs (`line_count`, p=0.009) outperform the composite it feeds. I would report
the sub-metrics and drop the composite until it is recalibrated relatively.

---

## 10. Noticed outside slice

- `Objects/odictobject.c` — CPY-0011 `odictiter_new` is the structural twin of CPY-0015
  `dictiter_new` (both score at the floor); if the campaign wants a cheap recall check on the
  untracked-DECREF shape, complexity will not supply it in either file. Other slice.
