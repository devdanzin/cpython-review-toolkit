# git-history-analyzer — Modules/ sample (informed)

**Toolkit:** cpython-review-toolkit v0.8.0 · **Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Envelope:** `is_shallow_clone: false` · `repo_total_commits: 132320` · `repo_first_commit_date: 1990-08-09` ·
`timeout_hit: false` · no `COMMIT CAP APPLIED` · `notes[]` clean except the expected
"`function_churn` skipped by request". **16,017 commits** over `Modules/` analysed (`--days 13000 --no-function
--density-top 250 --workers 12`). Every temporal claim below rests on the full history, not a prefix.

**Build used for reproduction:** `/home/danzin/projects/python_build_matrix/builds/debug-gil-nojit{,-asan}/python`
@ `a1d580430c8`. **That revision is three days NEWER than the review HEAD** (Jul 18 vs Jul 15). I diffed every
file I cite: `_pickle.c`, `_collectionsmodule.c`, `_elementtree.c`, `enumobject.c` are **identical**;
`itertoolsmodule.c` differs by exactly one hunk — `5200f119242` (gh-153908), which fixes `count_repr`. That
divergence is load-bearing and is reported below rather than reconciled.

## Scanner volume

Not a scanner agent — no candidate list. Volume is commits read.

```
commits in window: 16017 | fix: 4106 | high-confidence: 1165 (7.3%) | crash-classed: 1388
high-confidence fixes touching the 12 sample files: 200 | read in detail: 31
--introduced-by anchors run: 5 | fix-completeness verdicts: 5 | reproduced: 1 (ASan UAF)
```

`commits_by_crash_class` over `Modules/`: memory-leak 409, crash 378, overflow 164, refcount 135,
data-race 124, use-after-free 44, null-deref 44, uninitialized 43, assertion 20, corruption 12,
recursion 9, double-free 6.

---

## 1. Crash-fix-density watchlist — the 12 sample files

Ranked by `crash_fix_density` (recent crash-shaped fix commits per KLOC, rename-following) against all
250 ranked `Modules/` files. **Raw churn is shown only to demonstrate the anti-correlation.**

| dens. rank | file | lines | **density** | crash-fix (recent/total) | commits | churn rank |
|---|---|---|---|---|---|---|
| **10** | `Modules/_queuemodule.c` | 641 | **4.68** | 3 / 4 | 40 | ~last |
| **19** | `Modules/_json.c` | 2099 | **2.86** | 6 / 28 | 157 | 8th of 12 |
| 31 | `Modules/itertoolsmodule.c` | 4241 | 2.36 | 10 / 37 | 280 | 2nd |
| 32 | `Modules/_struct.c` | 2960 | 2.36 | 7 / 26 | 206 | 6th |
| 35 | `Modules/_csv.c` | 1865 | 2.14 | 4 / 16 | 175 | 7th |
| 38 | `Modules/_collectionsmodule.c` | 2906 | 2.06 | 6 / 25 | 272 | 3rd |
| 42 | `Modules/_zoneinfo.c` | 2824 | 1.77 | 5 / 10 | 62 | 11th |
| 60 | `Modules/_elementtree.c` | 4573 | 1.31 | 6 / 39 | 271 | 4th |
| 66 | `Modules/_heapqmodule.c` | 813 | 1.23 | 1 / 6 | 61 | 10th |
| 68 | `Modules/_pickle.c` | 8298 | 1.21 | 10 / 49 | 355 | **1st** |
| 101 | `Modules/_datetimemodule.c` | 7944 | 0.38 | 3 / 30 | 266 | 5th |
| 116 | `Modules/_randommodule.c` | 683 | **0.00** | 0 / 6 | 98 | 9th |

**The anti-correlation reproduces on `Modules/`, and harder than on `Objects/`.** `_pickle.c` is **1st by raw
churn and 68th by density**; `_queuemodule.c` is **last by raw churn and 10th of 250 by density** — the single
highest-density file in the sample, in a file the run context called "small, seldom reviewed". The two files
the run context flagged as "large and old" split: `_pickle.c` is mid-pack, `_datetimemodule.c` is near the
bottom (0.38) and is the one file I would deprioritise on temporal evidence. `_zoneinfo.c` being
"comparatively recent" shows up as expected — only 62 commits — but its density (1.77) is *higher* than
`_elementtree.c`'s despite a quarter the commits, because 5 of its 10 crash fixes are recent.

`_randommodule.c` is a **true zero with a non-zero denominator**: 98 commits and 6 lifetime fix commits, but
zero crash-shaped fixes in the recent window. That is earned quiet, not silence.

Practical read: **`_queuemodule.c` and `_json.c` deserved more of this run's attention than they got**, and
`_datetimemodule.c` deserved less.

---

## 2. Fix-completeness verdicts on the catalogued sites

### [ACCEPTABLE] OOM-0039 — `deque_clear` PyErr_Clear swallow · `Modules/_collectionsmodule.c:750`

Confirmed still present, one line as instructed. `--introduced-by Modules/_collectionsmodule.c:750` →
introduced `41290a68baa4` (2015, *feature*), last touched `8299e9b59ece` (2015, *refactor*). **No fix commit
has ever touched this line** — it is not an incomplete fix, it is an untouched one.

Completeness note that *is* new: `newblock()` (`:302-314`) is the only caller-visible failure and it calls
`PyErr_NoMemory()` itself, so the cleared exception is normally the one `newblock` just set. The residual
hazard is narrow and real: `deque_clear` is registered as `{Py_tp_clear, deque_clear}` (`:1874`), so it also
runs from `delete_garbage` where an unrelated exception may be pending. The clear is **unfiltered** — no
`PyErr_ExceptionMatches(PyExc_MemoryError)` narrowing — which is the exact refinement the FP taxonomy calls
out. Sibling swept: the only other `PyErr_Clear()` in the file (`:442`, `finalize_iterator`) **is**
`ExceptionMatches`-narrowed and is the guarded twin. **1 of 2 clears in this file is narrowed.**

### [ACCEPTABLE — now complete] gh-152951 `deque_extend` double-DECREF · `Modules/_collectionsmodule.c:514`

Fixed by `a90576d72c8` (2026-07-03). The interesting result is *what it was fixing*, in §4.

Sweep completeness **verified as now complete**: `deque_append_lock_held` / `deque_appendleft_lock_held` take
ownership of `item` on the failure path (`:346`, `:395`). All 5 call sites are consistent —
`:383` and `:432` pass `Py_NewRef(item)`, `:633` passes `Py_NewRef(item)`, `:514` and `:569` pass the
new reference from `iternext(it)`. **5 of 5 call sites correct.** No residual sibling.

### [FIX — un-swept sibling] TSAN-0006 `count_repr` · `Modules/itertoolsmodule.c:3680`

Confirmed present at review HEAD. Anchored: `--introduced-by Modules/itertoolsmodule.c:3659` shows the
*atomic* read in `count_next` was introduced by **`87939bd5790a` (gh-117657, 2024-05-21, `fix_confidence:
high`, `crash_class: data-race`, 3 files)** — and `--introduced-by :3680` shows the plain read in
`count_repr` was last touched in **2017** by an unrelated repr commit. So gh-117657 made `count_next` atomic
and never looked at the other reader of the same field. Textbook incomplete fix, held for two years.

**It was fixed three days after the review HEAD** by `5200f119242` (gh-153908, 2026-07-18) — a two-line
change adding `FT_ATOMIC_LOAD_SSIZE_RELAXED`. **And gh-153908 is itself incomplete** — see §3.

### [FIX — introduced by a "fix"] CPY-0016 `create_elementiter` · `Modules/_elementtree.c:2374-2379`

Confirmed present. The temporal result is the finding:

`--introduced-by Modules/_elementtree.c:2376` → **`d917dcbe5e85`, "Issue #18408: Fix constructors of
`_elementtree.c`" (Victor Stinner, 2013), classified `type: fix`.** Its own commit message is
*"Use `Py_DECREF()` instead of `PyObject_GC_Del()` to release correctly all resources"* — and that is exactly
the change that created the crash. Before it, the allocation-failure branch used `PyObject_GC_Del(it)`, which
does **not** run `elementiter_dealloc`. After it, the branch is `Py_DECREF(it)` on a GC-tracked,
half-initialised iterator. A commit whose stated purpose was correct resource release introduced a SIGSEGV.

It became a *segfault* rather than a benign early-free three years later, in a **performance** commit:
`22adf2ac022` ("Issue #25873: Optimized iterating ElementTree", Serhiy Storchaka) replaced the linked-list
`parent_stack` with an array plus the scalar `parent_stack_used`, assigned at `:2380` — **after** the
`Py_DECREF` at `:2376` — while `elementiter_dealloc` (`:2176-2181`) uses it as the loop bound over a NULL
`parent_stack`. Two commits, neither a bug fix for this path, three years apart, compose into CPY-0016.

**On the upstream PRs:** gh-148731 is open with gh-148838 / gh-152120 unmerged. **Neither is present in this
clone** — `git log --all --grep=148731` returns nothing, and the site is unchanged at both `4f3be1b5777` and
the newer `a1d580430c8`. I cannot read PR diffs offline, so I state only what the repo proves: **no fix for
this site has landed on `main` as of Jul 18 2026, five days after the review HEAD.** Whether the open PRs
cover the reproduced site is not answerable from this clone; it needs a network fetch of the PR refs. Flagged
as an evidence gap rather than guessed.

---

## 3. New siblings of known shapes

### [FIX] `repeat_repr` / `repeat_len` — un-swept siblings of TSAN-0006 · `Modules/itertoolsmodule.c:3796, 3802, 3809, 3813`

**Guarded twin:** `count_repr` after `5200f119242`, and `repeat_next` itself at `:3781/:3787`.

`repeat_next` writes `ro->cnt` with `FT_ATOMIC_STORE_SSIZE_RELAXED` (that was gh-123471 / #131247, "Make
concurrent iteration over `itertools.repeat` safe under free-threading"). Four plain reads of the same field
survive in two functions the sweep never opened:

```c
/* Modules/itertoolsmodule.c:3793-3803  repeat_repr */
    if (ro->cnt == -1)                                    /* plain read */
        return PyUnicode_FromFormat("%s(%R)", ..., ro->element);
    else
        return PyUnicode_FromFormat("%s(%R, %zd)", ..., ro->element,
                                    ro->cnt);             /* plain read */

/* Modules/itertoolsmodule.c:3806-3814  repeat_len  (__length_hint__) */
    if (ro->cnt == -1) { ... }                            /* plain read */
    return PyLong_FromSize_t(ro->cnt);                    /* plain read */
```

Reachable from Python as `repr(r)` / `operator.length_hint(r)` concurrent with `next(r)` on the FT build.
Structurally identical to TSAN-0006, in the same file, from the same sweep programme.
`repeat_len` is marginally worse than `count_repr` was: it reads `ro->cnt` **twice** with a `-1` test between,
so a concurrent `repeat_next` can make the second read disagree with the first and `PyLong_FromSize_t(-1)`
yields `2**64-1` as a length hint.

**Class: FIX.** Not an FP: it is not immortal-object noise, not critical-section-protected (there is no
critical section anywhere in `repeat`), and the writer is *provably* concurrent — it is atomic precisely
because upstream established it races.

### [FIX] `islice` — the itertools type the FT sweep never reached · `Modules/itertoolsmodule.c:1699-1732`

`islice_next` is the only itertools iterator that mutates **four** fields (`lz->cnt`, `lz->next`, `lz->it`)
with no atomic and no critical section anywhere, *and* drops an owning reference at exhaustion:

```c
    PyObject *it = lz->it;                 /* borrowed, held across user code */
    if (it == NULL) return NULL;
    iternext = *Py_TYPE(it)->tp_iternext;
    while (lz->cnt < lz->next) { ... lz->cnt++; }   /* plain RMW */
    ...
empty:
    Py_CLEAR(lz->it);                      /* non-atomic read-NULL-DECREF */
```
Two threads both pass the `it == NULL` guard, both reach `empty:`, both `Py_CLEAR` → double DECREF; and the
loser's local `it` is a freed pointer it has already called `iternext` on. Same shape as
`dictiter`/`setiter`, in a type the sweep skipped.

**Family-coverage evidence.** `Lib/test/test_free_threading/test_itertools.py` at HEAD covers exactly
**8 of 21** itertools iterator types: `accumulate`, `batched`, `chain`, `combinations_with_replacement`,
`cycle`, `permutations`, `zip_longest`, `tee`. Not covered: `count`, `repeat`, `islice`, `product`,
`combinations`, `groupby`, `_grouper`, `pairwise`, `starmap`, `compress`, `filterfalse`, `dropwhile`,
`takewhile`. gh-123471 has run for 18 months across 9 merged PRs and is roughly 57% complete by type.

### [FIX] `arrayiter_next` — the `Modules/` protocol iterator the named-container sweep missed · `Modules/arraymodule.c:3247-3248`

**This settles the hypothesis in the task with data: yes, `Modules/` protocol iterators were left un-swept.**

```c
/* Modules/arraymodule.c:3237-3249 */
    arrayobject *ao = it->ao;
    if (ao == NULL) return NULL;
    if (it->index < Py_SIZE(ao))
        return (*it->getitem)(ao, it->index++);
    it->ao = NULL;          /* plain store  */
    Py_DECREF(ao);          /* plain DECREF */
```
Byte-for-byte the `dictiter_iternextitem` `fail:` shape (`Objects/dictobject.c:6157-6159`), with none of the
protection. Guarded twin for the *same* shape one directory away: `Modules/_csv.c:999-1006` and
`Modules/_collectionsmodule.c:1995-2008` both wrap the identical drop in `Py_BEGIN_CRITICAL_SECTION`.

### [FIX] `unpackiter_iternext` — same shape, in the sample, with a second resource · `Modules/_struct.c:2269-2271`

```c
    if (self->so == NULL) return NULL;
    if (self->index >= self->buf.len) {
        Py_CLEAR(self->so);              /* double DECREF under a race */
        PyBuffer_Release(&self->buf);    /* DOUBLE bf_releasebuffer */
        return NULL;
    }
```
Worse than the pure refcount form: a second thread re-runs `PyBuffer_Release` on the same `Py_buffer`,
calling `bf_releasebuffer` twice. On a `bytearray` source that drives `ob_exports` negative; on `mmap` it can
unmap live memory. `_struct.c` is in the sample and ranks 32nd by density.

### [FIX] `count_nextlong` — borrowed `lz->long_cnt` returned without INCREF across user `__radd__` · `Modules/itertoolsmodule.c:3633-3641` — **REPRODUCED (ASan heap-use-after-free)**

Found by reading `886bc6e` (gh-145376, "Fix various reference leaks in `Objects/` and `Modules/`") and its
sibling hunk in `Objects/enumobject.c`. The commit *added a comment asserting the exact ownership invariant
that re-entrancy breaks*:

```c
/* Modules/itertoolsmodule.c:3631-3641 */
    // We hold one reference to "result" (a.k.a. the old value of
    // lz->long_cnt); we'll either return it or keep it in lz->long_cnt.
    PyObject *result = lz->long_cnt;                       /* BORROWED */
    PyObject *stepped_up = PyNumber_Add(result, lz->long_step);  /* user __radd__ */
    if (stepped_up == NULL) return NULL;
    lz->long_cnt = stepped_up;      /* struct's ref silently transferred to `result` */
    return result;                  /* returned with NO Py_INCREF */
```
`lz->long_step` is user-supplied (`itertools.count(start, step)` accepts any `PyNumber_Check` object — the
in-file docstring says "may be a float, Fraction, or Decimal"). A re-entrant `next()` inside `__radd__` runs
`count_nextlong` again, hands the *same* object out, and the struct's single reference is transferred twice.

**Honest attribution:** the pre-image of `886bc6e` has the same borrow (`long_cnt = lz->long_cnt; ... return
long_cnt;`), so this is **pre-existing, not introduced** by that commit. What `886bc6e` did was touch the
function to fix a leak, codify the broken invariant in a comment, and apply the same transformation to
`Objects/enumobject.c:180-196` — where it is **safe**, because `en->one` is always the internal `PyLong(1)`
and cannot run Python. That asymmetry is why the sweep missed it.

**Reproduction** — `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/repro_count_nextlong.py`,
run on `debug-gil-nojit-asan` @ `a1d580430c8`:

```
outer  : <class 'int'>
stolen : <class 'int'>
same object: True
=================================================================
==4082659==ERROR: AddressSanitizer: heap-use-after-free on address 0x73562e83b178
READ of size 8 at 0x73562e83b178 thread T0
    #0 _Py_TYPE_impl  Include/object.h:234:16
    #1 _PyObject_IsFreed  Objects/object.c:712:52
    #2 visit_decref  Python/gc.c:443:5
    #3 dict_traverse  Objects/dictobject.c:5104:17
    #4 subtract_refs  Python/gc.c:497:16
    #5 deduce_unreachable  Python/gc.c:1177:5
    #6 gc_collect_main  Python/gc.c:1509:24
freed by thread T0 here:
    #1 _Py_Dealloc  Objects/object.c:3319:5
    #4 tuple_dealloc  Objects/tupleobject.c:277:9
    #8 code_dealloc  Objects/codeobject.c:2434:5
EXIT=1
```
Non-ASan `debug-gil-nojit`: `Python/gc.c:443: visit_decref: Assertion "!_PyObject_IsFreed(op)" failed`,
then **SIGSEGV (core dumped), exit 139**. The freed block is the `10**30` literal in `co_consts`: it was
over-released, so the code object's own DECREF drove it to zero while the module globals still held it.
Three lines of pure Python, GIL build, no threads.

---

## 4. Commits that fixed one site and left siblings behind

This is where the temporal pass earns its keep. Four results, in descending order of value.

### 4.1 [FIX] `b770b23` — a **revert** that restored two defects and deleted the NEWS entry · `Modules/_pickle.c:3495, 3501-3502`

Chain: `e62a611` (gh-146452, 2026-05-17) fixed a pickle segfault → `57a0e57` (2026-05-18) narrowed the
locking *and* fixed two further defects → **`b770b23` (Victor Stinner, 2026-05-22) reverted `57a0e57`**,
along with the 3.14 and 3.15 backports (`0bd80a1d486`, `795dd3bd350`). The revert also deleted
`Misc/NEWS.d/next/Library/2026-05-18-…gh-issue-146452.RM0EVJ.rst`, so **there is no record in the changelog
that these two defects are live.**

`57a0e57`'s own message names exactly what the revert undid: *"Remove assertion that could fail in rare race
condition… Also handle `PyDict_Next` returning 0 in the single-item fast path."* Both are back at HEAD:

```c
/* Modules/_pickle.c:3494-3503  (post-revert) */
    dict_size = PyDict_GET_SIZE(obj);
    assert(dict_size);                                   /* :3495  can fail  -> SIGABRT (debug FT) */
    Py_ssize_t total = 0;
    do {
        if (dict_size - total == 1) {
            PyDict_Next(obj, &ppos, &key, &value);       /* :3501  return value DISCARDED */
            Py_INCREF(key);                              /* :3502  Py_INCREF(NULL) if it returned 0 */
            Py_INCREF(value);
```
`key`/`value` are initialised to NULL and re-NULLed by `Py_CLEAR` each batch, so a `PyDict_Next` returning 0
gives `Py_INCREF(NULL)` → SIGSEGV. Single-threaded it is unreachable (the `PyDict_GET_SIZE(obj) != dict_size`
check at `:3540` catches mutation from `save()`), which is exactly why it survives review. **On the FT build
another thread can delete and add one entry — size unchanged, check passes, `ppos` now past the end.**

Compounding: the coarse `Py_BEGIN_CRITICAL_SECTION(obj)` the revert restored (`batch_dict_exact`, `:3557-3563`)
wraps `save()`, i.e. arbitrary `__reduce_ex__`. Because `_PyCriticalSection_SuspendAll` releases outer
critical sections whenever the thread detaches, that lock is **not actually held across the user call** — it
gives the appearance of protecting `PyDict_Next`'s iteration state without doing so. The comment at `:3554-3555`
claims it does.

I could not determine the revert's *reason* from the clone (no PR body offline). The verdict is on the code,
not the decision: **two concrete defects are live at HEAD with no changelog trace.**

### 4.2 [ACCEPTABLE — historical, now fixed] `1aa1d76` — a refleak fix that introduced a double-DECREF

The cleanest instance of the shape in the sample's history, and worth recording because it validates the
detector. `1aa1d76b7f2b` (gh-145376, 2026-03-04, `crash_class: memory-leak`) moved ownership of `item` into
`deque_append_lock_held` **and** `deque_appendleft_lock_held` (2 sites), then removed the now-double
caller-side `Py_DECREF(item)` from **`deque_extendleft_impl` only** — leaving the identical one in
`deque_extend_impl`. Result: a double-DECREF on `deque.extend()` under memory pressure. Fixed **four months
later** by `a90576d72c8` (gh-152951), whose entire diff is the one deleted line.

**2 callees updated, 1 of 2 callers updated.** `--introduced-by Modules/_collectionsmodule.c:346` names
`1aa1d76b7f2b` directly, with `fix_confidence: high`.

### 4.3 [FIX] `5200f119242` (gh-153908) — fixed one field reader, left three · already reported in §3

### 4.4 [ACCEPTABLE] `a91b5c3` (gh-143543) `groupby_next` — checked, complete on the crash axis

Included because it *looks* like an incomplete fix and is not. The fix snapshots `tgtkey`/`currkey` with
`Py_INCREF` around `PyObject_RichCompareBool`. The obvious next question is whether `Py_INCREF(gbo->currkey)`
at `:567` — after the user `__eq__` ran — can see NULL. **It cannot:** `currkey` is written only by
`groupby_step` (`Py_XSETREF(gbo->currkey, newkey)` with `newkey != NULL`) and at construction, and
`groupby` registers **no `tp_clear`**. Semantics may be wrong under re-entrancy (the post-compare `currkey`
may be a different object); crash-wise it is sound. **Bounded: 3 post-user-code reads of `gbo->*key` in
`groupby_next`; all 3 provably non-NULL.**

---

## 5. Classes bounded (clean negatives, with evidence)

Stated per shape, including the ones that came back clean.

- **"field written with `FT_ATOMIC_STORE` in `_next` but read plainly elsewhere"** occurs on **4 fields** in
  `Modules/itertoolsmodule.c`. `bo->batch_size` (batched) — 1 reader, atomic — **clean**.
  `lz->index` (cycle) — 1 reader, atomic — **clean**. `lz->cnt` (count) — 1 plain reader, fixed post-HEAD by
  `5200f119242`. `ro->cnt` (repeat) — **4 plain readers in 2 functions, unfixed.** 2 of 4 fields clean.
- **`iternext_double_decref` tree-wide**: the shape is present at **31 `tp_iternext` sites**; **11 guarded,
  17 unguarded, 3 mixed**. Coverage was complete — all 38 `Py_tp_iternext` entries in `Modules/`, all 28 slot
  functions in `Objects/`, plus `Python/`; **zero** functions left unclassified. `Modules/` uses *only* the
  `PyType_Slot` form, so the positional-comment trap does not bite here (verified on raw source).
  Guarded twins: `dictreviter_iternext` (`Objects/dictobject.c:6344-6355`) wrapping the *identical* `fail:`
  text under a critical section; and the compile-out twin `Objects/tupleobject.c:1146-1149`
  (`#ifndef Py_GIL_DISABLED`). The 5 unguarded sites worth acting on first, all reachable from Python:
  `Modules/arraymodule.c:3248`, `Modules/_struct.c:2270`, `Modules/itertoolsmodule.c:1732`,
  `Objects/iterobject.c:80` (already CPY-0003), `Modules/_sqlite/cursor.c:1164,1172`.
- **borrowed-ref sub-shape A, list/sequence variant**: occurs at **~21 sites** in `Modules/` where a
  user-reachable list is indexed and a Python-executing call follows. **All 21 take a strong reference
  first** — 0 hits. Notably **`Modules/_heapqmodule.c` is entirely clean**: all 7 sift sites INCREF both
  operands *and* re-read `_PyList_ITEMS` and re-check the size after every `PyObject_RichCompareBool`. The
  "user `__lt__` mutates the heap mid-sift" shape the run context flagged as a classic **does not exist
  here**; that is a confident negative, and `_heapqmodule.c`'s density rank (66) agrees.
- **borrowed-ref sub-shape A, dict variant**: **19 `PyDict_Next` sites** in `Modules/`; 13 have no
  Python-executing call in the loop body; of the remaining 6, **4 protect both key and value**
  (`_json.c:1778`, `_pickle.c:2063`, `:3501`, `:3521`) and **2 do not** — `_pickle.c:6954` (value only) and
  `_pickle.c:6983` (neither).
- **borrowed-ref sub-shape B, `self->field` accessors**: **3 helpers** in `Modules/_elementtree.c`; `0469e6d`
  fixed 2, and **only `element_get_attrib` (`:558`) still returns a borrowed reference** — its comment still
  says so — with one dangerous caller at `:1765`. In `Modules/_struct.c`, **2** `soself->s_codes` loops;
  only `:2371` reaches user code, and it is unguarded.
- **`Modules/_zoneinfo.c` and `Modules/_datetimemodule.c` come back clean** for the borrowed-ref shape: their
  `self->trans_list_utc` / `self->_ttinfos` reads and all `PyTuple_GET_ITEM` reads are on immutable tuples or
  have no intervening Python-executing call. Consistent with `_datetimemodule.c`'s density rank of 101/250.
- **`Objects/enumobject.c:180-196`** — the sibling `886bc6e` transformed identically to `count_nextlong` — is
  **safe**: `en->one` is the internal `PyLong(1)`, so `PyNumber_Add` there cannot run Python. Bounded: the
  shape exists at 2 sites, 1 exploitable.
- **`PyDict_SetDefault` migration**: all **13** sites in `Modules/`, `Objects/` and `Python/` use the `…Ref`
  form. Migration complete.

**Phase-5 CPython-specific analyses, gated on scope as instructed:** Argument Clinic completeness,
`PyModule_AddObject` → `PyModule_AddObjectRef`, and stable-ABI progress were **not run**. The sample is 12
already-Clinic-converted accelerator modules; `module_families` in the output groups hash/dbm/io families,
none of which intersect the sample. These would have yielded zero and the effort went to §3–§4 instead.

---

## Toolkit assessment

**Does `analyze_history.py` support fix-completeness work end to end now? Substantially yes — for the first
time.** Concretely, the v0.8 rebuild is what produced §2 and §4:

- **The long-window fix is real and load-bearing.** `--days 13000` over `Modules/` completed in ~90 s across
  16,017 commits with an empty stderr. Both defining results — the 2013 `_elementtree` commit and the 2015
  `deque_clear` line — are **outside any 90-day window**, so the pre-v0.8 default would have reported a
  confident "no fix commit touched this" for the wrong reason.
- **`--introduced-by` is the single highest-value addition.** Five invocations produced five verdicts. The
  `type` + `fix_confidence` + `crash_class` + `files` annotation on each `line_history[]` entry is what turned
  "who wrote this line" into "**a commit classified `fix` introduced this crash**" without a second tool call.
  It found `d917dcbe5e85` and `1aa1d76b7f2b` directly.
- **Density ranking beat raw churn again, by a wider margin than on `Objects/`.** `_pickle.c` 1st by churn /
  68th by density; `_queuemodule.c` last by churn / 10th by density. Anyone prioritising by `commits` would
  have inverted the sample.
- **The `fix_confidence` sort works as advertised.** 1165 of 16017 commits (7.3%) scored `high`, and the
  crash-shaped prefix of `recent_fixes` was a tractable read that surfaced the pickle revert, the gh-123471
  sweep, and the gh-145376 sweep within the first 30 entries.

### What is still missing — ranked tuning proposals

1. **`--introduced-by` should accept a range and should follow renames. It silently does neither well.**
   `--introduced-by FILE:LINE` on `_collectionsmodule.c:750` returned a `line_history` whose oldest hunk is
   `-static void deque_clear(dequeobject *deque);` — a *forward declaration*, not the `PyErr_Clear()` at all.
   The tool reported `introduced_by: 41290a68baa4 (feature)` for a line that commit never wrote. The
   docstring's caveat ("widen the range") puts the burden on the agent, but nothing in the output *signals*
   that the recovered hunk's text doesn't match `line_text`. **Proposal:** compare the `+` side of the oldest
   hunk against `line_text`; when they differ, emit
   `"warning": "introduced_by hunk does not contain line_text — the line was rewritten; widen the range"`.
   That is a two-line check that converts a silent wrong answer into a prompt to re-run. Without it this is
   the tool's most dangerous output, because it is confidently specific.

2. **Add a `reverted_by` / `revert_of` link and surface reverts as a first-class commit type.** §4.1 is the
   best finding of this pass and I found it by eye, because `b770b23`'s subject happens to start with
   `Revert "`. The classifier scored it `data-race` / high — i.e. it looked like a *fix* — when it is the
   opposite: a fix being withdrawn. A reverted crash fix means the defect is live **and** the NEWS entry is
   gone, so it is invisible to every other signal. **Proposal:** parse `This reverts commit <sha>` from the
   body, add `"reverts": "<sha>"` and `"reverted_by": "<sha>"` to commit records, add `type: "revert"`, and
   add a top-level `reverted_fixes[]` listing every `fix`-classified commit that was later reverted and not
   re-landed. On `Modules/` that list would be short and every entry would be worth reading.

3. **Add a `sweep_gaps[]` pass: for every multi-site fix commit, report the sites it did *not* touch.**
   The shape behind §4.2, §4.3 and §3 is always the same — a commit edits N of M structurally identical
   sites. The tool already has the diff and the file list. **Proposal:** for each `fix` commit, extract the
   set of function names it modified; grep the same file for other functions reading/writing the same struct
   field or calling the same API; emit `{"commit": …, "touched": [...], "untouched_siblings": [...]}`.
   Applied to `5200f119242` that emits `touched: [count_repr], untouched: [repeat_repr, repeat_len]`
   mechanically. Applied to `1aa1d76b7f2b` it emits `touched: [deque_extendleft_impl],
   untouched: [deque_extend_impl]` — the gh-152951 bug, four months before it was filed. This is the single
   change that would move the agent's best capability from "an analyst noticed" to "the tool reported".

### Prompt / environment problems

- **The agent prompt does not tell you to pin the interpreter, and the ambient one here is RustPython.**
  `which python` → `/home/danzin/venvs/rustpython_venv/bin/python` (RustPython 0.5.0). My first
  `analyze_history.py` run therefore executed under RustPython, produced a 0-byte JSON, and wrote
  `removal index (is 0) should be < len (is 0)` plus `ResourceWarning`s from RustPython's bundled
  `subprocess.py` to stderr — **no non-zero exit, no error JSON**. Had I not checked stderr I would have
  reported an empty analysis as a clean negative. Two fixes: RUN_CONTEXT should say `source
  /home/danzin/venvs/cpython-review-toolkit/bin/activate` in the *agent* prompt, not only in the shared
  context; and `analyze_history.py` should assert `sys.implementation.name == "cpython"` at startup and exit
  non-zero otherwise. Cheap, and it prevents a whole class of silent-garbage runs.
- **The prompt asked me to read PRs gh-148838 / gh-152120; a git clone cannot.** Unmerged PR refs are not
  fetched by default (`git log --all --grep=148731` → empty). Either the run should pre-fetch
  `refs/pull/*/head`, or the deliverable should be scoped to what the clone proves. I reported the evidence
  gap instead of guessing, but the task as written was not satisfiable offline.
- **The build matrix is *newer* than the review HEAD (`a1d580430c8` Jul 18 vs `4f3be1b5777` Jul 15).**
  RUN_CONTEXT frames the builds as "older than HEAD"; for `itertoolsmodule.c` the opposite is true, and the
  delta is precisely the fix for one of the three catalogued sites. Any agent that trusted the framing and
  skipped the diff would have reported TSAN-0006 as reproduced-on-a-matching-build when the build already
  contains its fix. The instruction to diff the cited files is what saved this; it should be stated as
  "the matrix may be newer **or** older — diff, don't assume".

### Precision note

No scanner rule to score. Of the 31 commits read in detail, **5 yielded a fix-completeness verdict** and
**4 yielded an actionable finding** — the two-thirds that yielded nothing were mostly single-site fixes with
no structural sibling, which is the expected base rate and the reason proposal 3 matters.
