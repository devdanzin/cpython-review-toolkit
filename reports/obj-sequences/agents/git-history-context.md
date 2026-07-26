# Git history context — slice `obj-sequences` (Phase 1 preflight)

**Agent:** `git-history-context` (early temporal orientation; not the post-hoc `git-history-analyzer`)
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** exactly the 4 files in `preflight/slice_files.txt`

---

## 0. Clone health — verified, not assumed

| check | value |
|---|---|
| Shallow clone | **no** (`git rev-parse --is-shallow-repository` → `false`) |
| Repo depth | **132,320 commits**, back to **1990-08-09** |
| HEAD | `4f3be1b5777313fb36ff4bda7e4a4197c932c30e`, 2026-07-15, *"gh-150452: use PyMutex in socket module (#150453)"* |
| HEAD == review target | yes |
| Commits analyzed (`Objects/`, `--days 13000`) | **9,201** |
| Commit cap applied | **no** (`max_commits: 50000`) |
| Script timeout | **no** (`timeout_hit: false`) |
| Envelope `notes[]` | one note only, the standard advisory: *"Prefer `watchlist` (recent crash-fix commits per KLOC) over `file_churn[].churn_rate` for prioritization: on CPython's `Objects/` raw churn was anti-correlated with defect density."* No shallow-clone, cap, or timeout warning. |

**Build-matrix diff (AGENT_BRIEF §4 rule 11).** `git diff a1d580430c8 4f3be1b5777` over all four slice files is **empty** — the files are byte-identical between the matrix build and the review target. Every line number in this report is valid on `debug-gil-nojit` / `release-gil-nojit` / the ASan variants without adjustment.

---

## 1. (a) Fix-propagation hunt — the headline

This is the deliverable the previous slice got by accident. Four propagation results, ordered by confidence.

### FP-1 — `bytearray.extend()` OOB write: an OPEN PR that fixes one member of a class. **LIVE IN THE TARGET.**

- **Issue:** [gh-153578](https://github.com/python/cpython/issues/153578), opened **2026-07-11**, **OPEN**
- **PR:** [#153579](https://github.com/python/cpython/pull/153579), **OPEN / unmerged**, last touched 2026-07-11, `+25/-0` across 3 files, of which **`Objects/bytearrayobject.c` gets exactly `+3/-0`**
- **Status in the review target:** the target is dated 2026-07-15, four days *after* the PR opened, and the PR is unmerged. **The bug is present at `4f3be1b5777`.**

Mechanism, verified by reading (not from the issue text):

- `bytearray_extend_impl` **:2186** passes `Py_SIZE(self)` as *both* `lo` and `hi` into `bytearray_setslice`, evaluated **before** any user code runs.
- Inside `bytearray_setslice`, **:663** `PyObject_GetBuffer(values, …)` runs the argument's `__buffer__`, which can shrink `self`.
- The clamp that follows is ordered wrong: **:675** `if (hi < lo) hi = lo;` runs **before** **:677** `if (hi > Py_SIZE(self)) hi = Py_SIZE(self);`. Only `hi` is clamped to the new size; `lo` keeps the pre-callback value. Result `lo > hi` → negative-size `memmove` in `bytearray_setslice_linear`.

**Guarded twin** — named by the reporter, and I confirmed it in the source: `bytearray_iconcat_lock_held` **:355** acquires the buffer *first* and only then reads **:368** `Py_ssize_t size = Py_SIZE(self);`. Same operation, correct order.

**Sibling sweep — I enumerated all five call sites of `bytearray_setslice`:**

| line | caller | pre-callback bound? | verdict |
|---|---|---|---|
| :653 | `bytearray_setslice` self-alias recursion | no — copies first, recurses | safe |
| :709 | `bytearray_setitem_lock_held` | `values == NULL`, no `GetBuffer` | safe |
| **:2186** | **`bytearray_extend_impl`** | **yes** | **the reported bug** |
| :2265 | `bytearray_extend_impl` tail | argument is a real `bytearray`; `bf_getbuffer` is C-level | safe |
| — | `bytearray_ass_subscript_lock_held` | bounds recomputed by the copy-and-recurse at :802-808 | safe |

**Conclusion: PR #153579 is complete *within* `setslice`.** It is *not* complete for the class — see FP-2.

### FP-2 — `gh-142560`'s buffer-export sweep skipped `strip`/`lstrip`/`rstrip`. **Un-found sibling.**

Commit `220f0b10777` (2025-12-19, *"gh-142560: prevent use-after-free in search-like methods by exporting buffer in bytearray"*) introduced the helper `_bytearray_with_buffer` (**:98**) and applied the `ob_exports++ / ob_exports--` bracket to **ten** methods: `find`, `count`, `index`, `rfind`, `rindex`, `contains`, `startswith`, `endswith`, plus hand-rolled brackets in `split` (**:1806**) and `rsplit` (**:1930**).

`bytearray_strip_impl_helper` (**:2358**) — backing `strip`, `lstrip`, `rstrip` — has the same shape and **did not get it**:

```c
myptr = PyByteArray_AS_STRING(self);      /* :2377 */
mysize = Py_SIZE(self);
... compute left/right by scanning myptr ...
if (bytes != Py_None)
    PyBuffer_Release(&vbytes);            /* :2390 — can run user __release_buffer__ */
return PyByteArray_FromStringAndSize(myptr + left, right - left);   /* :2392 — myptr used after */
```

`PyBuffer_Release` dispatches `bf_releasebuffer`, which for a Python-level buffer provider runs `__release_buffer__` (PEP 688, 3.12+). Because `self->ob_exports` is **not** elevated here, `_canresize` (**:113**) permits a `self.clear()` from that callback, freeing `myptr` before it is read at :2392.

**Guarded twin:** `bytearray_split_impl` (**:1799–1828**) does the identical dance — `PyObject_GetBuffer(sep)`, scan, `PyBuffer_Release(&vsub)` — but releases *inside* the `ob_exports` window, so the callback cannot resize.

Corroborating signal: **PR #14771** *"bpo-25433: Align bytearray strip methods to those found in byteobject.c"* has been **OPEN since 2019-07-14** — seven years stale, on exactly these functions.

I did **not** write a reproducer (out of scope for a Phase 1 preflight). Hand to `refcount-auditor` / `memory-pattern-analyzer`; it is a 10-line repro on the GIL ASan build.

### FP-3 — the `ob_exports++` idiom is now the file's guard vocabulary. Inventory of who lacks it.

Three separate 2025 fixes converged on the same idiom (`220f0b10777` gh-142560, `61ee04834b0` gh-142557 `__mod__`, `9976c2b6349` gh-143195 `hex(sep)`). It is now the file's canonical answer to "I hold a raw pointer into `self` across user code". Machine sweep of every `bytearray*` function that reads `self`'s raw buffer **and** contains a Python-reaching call:

| function | line | guarded | python-reaching calls | note |
|---|---|---|---|---|
| `bytearray_getbuffer_lock_held` | 55 | **yes** | `_getbuffer` | |
| `bytearray_hex_impl` | 2664 | **yes** | `_Py_strhex` | gh-143195 |
| `bytearray_mod_lock_held` | 2844 | **yes** | `_PyBytes_FormatEx` | gh-142557 |
| `bytearray_split_impl` | 1799 | **yes** | `PyObject_GetBuffer` | gh-142560 |
| `bytearray_rsplit_impl` | 1923 | **yes** | `PyObject_GetBuffer` | gh-142560 |
| **`bytearray_strip_impl_helper`** | **2358** | **NO** | `PyObject_GetBuffer`, `PyBuffer_Release` | **FP-2 — confirmed by reading** |
| **`bytearray_extend_impl`** | **2176** | **NO** | `PyIter_Next`, `PyObject_GetIter`, `PyObject_Length`, `_getbytevalue` | **FP-1 — gh-153578, open** |
| `bytearray___init___impl` | 915 | NO | `PyObject_GetBuffer`, `PyObject_GetIter`, `PyNumber_AsSsize_t`, `_getbytevalue` | **open bug gh-153419**, see §5 |
| `bytearray_translate_impl` | 1640 | NO | `PyObject_GetBuffer` | reads `input` *after* both `GetBuffer`s → ordering OK; but `inlen` (**:1682**) is read before a `PyByteArray_FromStringAndSize` that can trigger GC. **Unverified — hand off.** |
| `bytearray_setslice` | 636 | NO | `PyObject_GetBuffer` | the FP-1 vehicle |
| `bytearray_iconcat_lock_held` | 350 | NO | `PyObject_GetBuffer` | **correct by ordering** — the guarded twin |
| `bytearray_ass_subscript_lock_held` | 728 | NO | `PyNumber_AsSsize_t`, `_getbytevalue` | correct by copy-and-recurse |
| `bytearray_setitem_lock_held` | 687 | NO | `_getbytevalue` | fixed by ordering, gh-91153 |
| `bytearray_subscript_lock_held` | 480 | NO | `PyNumber_AsSsize_t` | **flagged by sweep, not hand-verified** |
| `bytearray_take_bytes_impl` | 1544 | NO | `PyNumber_AsSsize_t` | **flagged by sweep, not hand-verified** |
| `bytearray_repr_lock_held` | 1114 | NO | `PyUnicode_*` | likely benign |

The last two rows are the honest residue: the sweep flagged them, I ran out of budget before reading them. They are the first thing a downstream agent should pick up.

### FP-4 — `gh-91153` needed two rounds three years apart. Incomplete-fix precedent *in this exact file*.

- Round 1: `f36589510b8` (2022-07-19) moved `_getbytevalue` *above* the size check in `bytearray_setitem` / `bytearray_ass_subscript`, so a mutating `__index__` on the **value** could not invalidate the bounds.
- Round 2: `5e1e21dee35` (2025-07-12), **same issue number**, had to come back because a `char *buf = PyByteArray_AS_STRING(self)` was still hoisted above `PyNumber_AsSsize_t(index, …)` — the mutating `__index__` on the **index** was never covered. The fix deletes the hoist and re-loads the pointer at each use (**:749**, **:810**, **:851**).

This is a documented three-year gap between two halves of one bug in the slice's own file. It is the strongest prior for expecting more unswept siblings here, and it is why FP-2 and the two unverified rows above deserve real attention rather than a glance.

### FP-5 — list side: the strong-reference fix **did** propagate. One FT asymmetry, one dismissed candidate.

The 2019–2020 sweep (`d9e561d23d9` bpo-38610 `index`/`count`/`remove`; `4dbf2d8c678` bpo-39453 `__contains__`; `2d5bf568eaa` bpo-38588) established "hold an owned reference across `PyObject_RichCompareBool`". I checked **every** `RichCompareBool` site in the current `listobject.c`:

| line | function | owned ref? |
|---|---|---|
| 665 | `list_contains` | yes — `list_get_item_ref` |
| 3345 | `list_index_impl` | yes — `list_get_item_ref` |
| 3381 | `list_count_impl` | yes — `list_get_item_ref` |
| 3412 | `list_remove_impl` | yes — but raw `self->ob_item[i]` + manual `Py_INCREF` |
| 3467 | `list_richcompare_impl` | yes — `Py_INCREF` both sides (gh-120298, `141babad9b4`) |
| 2903, 2916 | `unsafe_tuple_compare` | **no** — see below |

**Asymmetry worth a look:** `list_remove_impl` (**:3412**) is the only one of the four that reads `self->ob_item[i]` directly and then `Py_INCREF`s, while its three siblings use `list_get_item_ref()` — the free-threading-safe accessor that does an atomic `ob_item` load plus `_Py_TryXGetRef`. The load-then-INCREF pair is not equivalent under free-threading. This is a **free-threading retrofit that reached three of four call sites**. Hand to `ft-race-scanner`.

**Candidate I checked and discarded:** `unsafe_tuple_compare` (**:2903**, **:2916**) calls `PyObject_RichCompareBool(vt->ob_item[i], …)` with no `Py_INCREF`, which looks like a textbook instance of the class. It is not: during `list.sort` the tuples are strongly held by the sort's saved item array / keys array, and tuples are immutable, so `vt->ob_item[i]` cannot be dropped by a user `__eq__`. Reporting the dismissal per AGENT_BRIEF §4.1 — the story was plausible and the code refutes it.

---

## 2. (b) Per-file bug-fix-density watchlist

Script-computed (`watchlist[]`, ranked by crash-fix commits per KLOC, `--density-days 4000`, rename-following on), scoped to the four files out of 60 ranked `Objects/` entries:

| rank (of 60) | file | crash-fix density /KLOC | recent crash fixes | total crash-fix commits | lines |
|---|---|---|---|---|---|
| **4** | `Objects/listobject.c` | **8.81** | **38** | 67 | 4,312 |
| **16** | `Objects/bytearrayobject.c` | **5.12** | 16 | 34 | 3,125 |
| **21** | `Objects/bytesobject.c` | **4.59** | 18 | 32 | 3,925 |
| **32** | `Objects/bytes_methods.c` | 2.71 | 2 | 3 | 738 |

For calibration, the top of the `Objects/` list is `boolobject.c` (22.03, a small-denominator artifact), `object.c` (11.64), `tupleobject.c` (10.75), then **`listobject.c` at 4th**. `listobject.c` is a genuine tree-level hotspot, not merely a slice-level one.

**Does density disagree with raw churn?** Mildly, and in a direction worth acting on:

| file | density rank | churn rank (of 168) | commits | churn_rate |
|---|---|---|---|---|
| `listobject.c` | 4 | 6 | 540 | 5.88 |
| `bytesobject.c` | **21** | **8** | 432 | 8.76 |
| `bytearrayobject.c` | **16** | **19** | 246 | 4.03 |
| `bytes_methods.c` | 32 | 46 | 47 | 4.2 |

The one real inversion is **`bytesobject.c`: 8th by churn, 21st by density.** Its churn is inflated by refactors and optimization work (`PyBytesWriter` / PEP 782 churn, `sprintf` removal, immortal-object plumbing) rather than by crash fixes. **I would trust density here**, and specifically I would *not* let `bytesobject.c`'s high churn rank pull attention away from `bytearrayobject.c`, which has fewer commits but a far higher proportion of them being UAF/re-entrancy fixes (see §3 per-file split: 3 UAF + 4 re-entrancy commits since 2024 in `bytearrayobject.c`, versus 1 + 0 in `bytesobject.c`).

**Repeat-offender functions** (from the fix corpus, by hand — the function-churn pass ranks by edit count, which over-weights the FT sweep):
- `bytearray_setslice` / `bytearray_setslice_linear` — gh-153578 (open), gh-143003, gh-19568, gh-101444 (open PR)
- `bytearray_ass_subscript_lock_held` — gh-91153 twice (2022, 2025), gh-19578
- `_PyBytes_FormatEx` — gh-142557 (UAF), gh-140939 (leak), gh-23466, gh-19969
- `list_ass_slice` / `list_resize` — gh-120384, gh-97616, gh-18408 ×3, gh-27660
- `list_sort_impl` — gh-88027 (double free), gh-35623, gh-11335 ×2, gh-112087 (FT)

---

## 3. (c) Recurring fix-keyword clusters

Year histogram over all 1,113 all-time commits touching the four files. A commit can land in several clusters.

```
cluster                            total |   13   14   15   16   17   18   19   20   21   22   23   24   25   26
----------------------------------------------------------------------------------------------------------------
user-callback re-entrancy             26 |    1    .    2    .    1    .    1    2    .    2    .    2    3    1
resize/borrowed-ptr invalidation      31 |    7    .    2    2    1    1    1    .    .    1    .    2    5    1
integer overflow in size arith        45 |    3    3    3    9    4    .    5    1    1    1    1    .    1    .
free-threading retrofit               32 |    .    .    .    .    .    .    .    .    .    .    .   17   11    4
use-after-free / double-free           6 |    .    .    .    .    .    .    .    .    .    1    .    1    4    .
refcount / leak                       20 |    .    .    .    1    .    .    .    .    .    .    4    2    1    2
crash / NULL deref                    25 |    .    2    3    2    .    3    2    2    1    .    .    4    1    .
error-path / MemoryError              14 |    9    .    2    .    1    .    .    .    .    .    .    .    1    .
```

Measured against the four classes the task predicted:

**1. User-comparison / user-callback re-entrancy — REAL, ACTIVE, and it has *migrated*.** 26 commits, live every year since 2020. But the shape changed: 2019–2020 it was `__eq__` during list `index`/`count`/`remove`/`contains` (bpo-38610, bpo-39453, bpo-38588) — **that sub-class is now closed**, see FP-5. Since 2022 every instance is on the bytes/bytearray side and the callback is a *buffer-protocol or coercion* dunder, not a comparison: `__index__` (gh-91153, twice), `__buffer__` (gh-126980, gh-153578), `__len__` (gh-143195), `__repr__` via `%`-formatting (gh-142557). **The prediction was right about the class and would have pointed at the wrong file.** The habitat is `bytearrayobject.c`, not `listobject.c`. Hypothesis for why: `bytearray` is the only mutable buffer-exporting type here, so it is the only one where a callback can invalidate a pointer the caller still holds. *Falsifier:* find a post-2022 re-entrancy fix in `listobject.c` driven by a non-comparison dunder — I found none.

**2. Resize invalidating a borrowed pointer / live buffer export — REAL, and the largest active cluster.** 31 commits, **5 in 2025 alone**, the year's peak across all clusters. This is the same population as cluster 1 seen from the other end, plus the `ob_exports` machinery itself. Still active: gh-153578 is open.

**3. Integer overflow in size arithmetic — REAL BUT HISTORICAL.** The largest all-time cluster (45) and the most misleading. It peaks in **2016 (9 commits)** and is essentially **dead since 2020**: one commit in 2023, one in 2025, **zero in 2024 and 2026**. The 2016–2019 sweep (Xiang Zhang, Serhiy Storchaka) systematically hardened `list_resize`, `bytearray.extend`, concatenation and slicing. **I would deprioritize this class** — it is the clearest "fixed long ago, do not re-litigate" signal in the slice. Contrast the ongoing tree-wide UBSan work (gh-148286, gh-151847) which is a *different*, narrower concern (`PY_SSIZE_T_MIN` negation UB), not allocation sizing.

**4. Free-threading retrofits — REAL, RECENT, AND CONCENTRATED.** Zero commits before 2024, then **17 (2024) + 11 (2025) + 4 (2026)**. The single largest recent force on this slice. **25 of `listobject.c`'s 89 commits since 2024 are FT retrofits** — that one cluster in that one file explains most of the slice's recent churn. This is also the class most likely to have left asymmetries (FP-5's `list_remove_impl` is one).

**Unpredicted cluster worth naming: use-after-free/double-free is brand new.** Six commits all-time, **four of them in 2025**, none before 2022. gh-142557, gh-143195, gh-142560, gh-133767. Combined with the open gh-153578, this is the class with the steepest recent slope in the slice and the one where the guarded twin is best established (`ob_exports++`). If the slice produces one novel finding, my prediction is that it comes from here.

**Per-file split, 2024→present:**

| file | commits | dominant clusters |
|---|---|---|
| `listobject.c` | 89 | FT retrofit 25, crash 3, re-entrancy 2 |
| `bytesobject.c` | 54 | refcount/leak 4, FT 3, resize 3 |
| `bytearrayobject.c` | 32 | resize 4, FT 4, **re-entrancy 4, UAF 3** |
| `bytes_methods.c` | 6 | none matched |

`bytearrayobject.c` has the *lowest* recent commit count of the three big files and by far the *highest* concentration of memory-safety fixes.

---

## 4. (d) Churn × complexity

Complexity from `measure_c_complexity.py` over `Objects/`; the four files place 7 functions in the directory's top 50.

| Objects/ rank | file | function | lines | CC | nest | score |
|---|---|---|---|---|---|---|
| **1** | `bytesobject.c` | `_PyBytes_FormatEx` | 480 | **159** | 6 | 7.3 |
| 6 | `listobject.c` | `list_sort_impl` | 192 | 57 | 4 | 5.5 |
| 9 | `bytearrayobject.c` | `bytearray___init___impl` | 159 | 45 | 5 | 5.1 |
| 20 | `bytesobject.c` | `_PyBytes_DecodeEscape2` | 93 | 43 | 5 | 4.2 |
| 26 | `bytesobject.c` | `bytes_fromformat` | 135 | 37 | 5 | 3.7 |
| 40 | `listobject.c` | `merge_hi` | 111 | 33 | 3 | 3.0 |
| 48 | `bytearrayobject.c` | `bytearray_ass_subscript_lock_held` | 127 | 34 | 3 | 2.8 |

`_PyBytes_FormatEx` is the **single most complex function in all of `Objects/`**.

**Testing the mod-io lesson rather than assuming it.** On mod-io, churn predicted the bugs and complexity did not. Here I scored both predictors against the four specific defect sites this report actually surfaced:

| defect site | predicted by density/churn? | predicted by complexity? |
|---|---|---|
| `bytearray_extend_impl` (gh-153578, open) | yes — `bytearrayobject.c` is 16th by density | **no** — not in the top 50 |
| `bytearray_strip_impl_helper` (FP-2) | yes | **no** — ~35 lines, trivially simple |
| `bytearray_setslice` (FP-1 vehicle) | yes | **no** — ~50 lines |
| `list_remove_impl` FT asymmetry (FP-5) | yes | **no** — 20 lines |
| `bytearray___init___impl` (open gh-153419) | yes | **yes** — rank 9 |
| `_PyBytes_FormatEx` (gh-142557 UAF, gh-140939 leak) | partly — `bytesobject.c` is only 21st | **yes** — rank 1 |

**Result: the mod-io lesson holds, but not universally.** Density/churn flagged the file for 6 of 6; complexity flagged the function for 2 of 6. The four bugs complexity missed are all *short, simple functions whose danger is entirely in the ordering of their calls* — which is exactly what a line-count/CC metric cannot see. But the two it did catch it caught hard, and `_PyBytes_FormatEx` is a case where complexity is the *better* predictor, because density ranks its file only 21st.

**For the Group C complexity agent:** `bytearrayobject.c` is where churn, density and defect concentration agree; but its bugs are not in its complex functions. Spend the complexity budget on **`_PyBytes_FormatEx`** — rank 1 in the directory, 480 lines, CC 159, and demonstrably still producing memory-safety fixes in 2025 (a UAF and a leak). It is the one place in this slice where "most complex" and "still buggy" are the same function.

---

## 5. (e) Open tracker state

`gh api -X GET search/issues` per the environment note (plain `gh search issues` returns nothing here).

### Open issues directly on slice files

| # | opened | title | relevance |
|---|---|---|---|
| **153578** | 2026-07-11 | Out-of-bounds write in `bytearray.extend()` with a reentrant `__buffer__` | **FP-1. PR #153579 open/unmerged → live in target.** |
| **153419** | 2026-07-09 | debug abort/ref leak calling `__init__(<str>,<enc>)` on a one-byte bytearray | `bytearray___init___impl` :915 — flagged unguarded by my sweep **and** complexity rank 9. Triple signal. |
| **148268** | 2026-04-08 | `unsafe_{latin,long,float}_compare` have an `assert` that assumes `PyObject_RichCompareBool` can't fail | `listobject.c` **:2833, :2858, :2873** — the exact asserts I read in FP-5. In-slice, open, unclaimed. |
| **151818** | 2026-06-20 | Double free / UAF in `list.append()` when the list grows under `MemoryError` | `list_resize` / `_CALL_LIST_APPEND` |
| **126559** | 2024-11-07 | nogil inconsistent empty list while sorting | `list_sort_impl`, FT |
| **151847** | 2026-06-21 | `PY_SSIZE_T_MIN` negate UB in `unicode_format.c` and `bytesobject.c` | in-slice |
| **106693** | 2023-07-12 | Possible UB in `bytes_characters` | `bytesobject.c` |
| **149816** | 2026-05-14 | **22 free-threading race conditions** (umbrella) | see below |

### The umbrella that is half-fixed

**gh-149816 "22 free-threading race conditions"** is open, and two of its sub-fixes have already landed *in this slice*: `00ea77613b9` (2026-05-18, list slice deletion) and `46afba7b932` (2026-05-16, `_PyBytes_FromList`). An open umbrella whose members are being fixed one at a time is structurally the same finding shape as the previous slice's gh-154523/PR #154565 — **the remaining unfixed members of that list are the highest-yield place for `ft-race-scanner` to start.** I did not enumerate which of the 22 are still open; that is a cheap follow-up (`gh api repos/python/cpython/issues/149816`).

### Open PRs touching slice files

| # | opened | title | note |
|---|---|---|---|
| **153579** | 2026-07-11 | gh-153578: Fix OOB write in `bytearray.extend()` with a reentrant `__buffer__` | **`+3/-0` to `bytearrayobject.c`. Fixes one method; see FP-2 for the sibling it does not reach.** |
| **14771** | 2019-07-14 | bpo-25433: Align bytearray strip methods to those found in byteobject.c | **7 years stale, on the FP-2 functions** |
| 101445 | 2023-01-31 | gh-101444: Optimize bytearray slice assignment for bytes-like object | touches `bytearray_setslice` — the FP-1 function |
| 141862 | 2025-11-22 | gh-139871: Optimize bytearray unique bytes iconcat | touches the FP-1 guarded twin |
| 148456 | 2026-04-12 | gh-148323: release the GIL in `bytes.join` when operands are immutable | `bytesobject.c` |
| 152092 | 2026-06-24 | gh-152030: Clarify `sequence.index()` signature | docs |

---

## 6. Priority hand-off

Ranked, with the agent that should look first and the specific thing to look at.

| # | site | agent | what to check |
|---|---|---|---|
| 1 | `bytearrayobject.c:2358` `bytearray_strip_impl_helper` | `refcount-auditor`, `memory-pattern-analyzer` | **FP-2** — `myptr` used at :2392 after `PyBuffer_Release` at :2390 runs `__release_buffer__`. Guarded twin `bytearray_split_impl:1799`. Repro on GIL ASan. |
| 2 | `bytearrayobject.c:2186` + `:636` `extend`/`setslice` | `memory-pattern-analyzer` | **FP-1 / gh-153578** — confirm it still reproduces at `4f3be1b5777` (PR unmerged). Confirm-don't-relitigate, then check whether the class extends past `setslice`. |
| 3 | `bytearrayobject.c:915` `bytearray___init___impl` | `error-path-analyzer`, `uninitialized-dealloc-auditor` | **gh-153419 open** + complexity rank 9 + unguarded in my sweep. Three independent signals on one function. |
| 4 | `bytearrayobject.c:480`, `:1544` `subscript`/`take_bytes` | `null-safety-scanner`, `refcount-auditor` | Sweep-flagged, **not hand-verified by me** — `PyNumber_AsSsize_t` runs `__index__`; check for a hoisted buffer pointer, the exact gh-91153 round-2 shape. |
| 5 | `listobject.c:3412` `list_remove_impl` | `ft-race-scanner` | **FP-5** — raw `ob_item[i]` + `Py_INCREF` where its three siblings use `list_get_item_ref()`. FT retrofit reached 3 of 4 sites. |
| 6 | gh-149816's unfixed members | `ft-race-scanner` | Open umbrella, 2 members already fixed in-slice. Enumerate the remainder first. |
| 7 | `listobject.c:2833/2858/2873` | `error-path-analyzer` | **gh-148268 open** — asserts assume `PyObject_RichCompareBool` cannot fail. Confirm, don't re-derive. |
| 8 | `bytesobject.c` `_PyBytes_FormatEx` | `c-complexity-analyzer`, `refcount-auditor` | Rank 1 in all of `Objects/`; two 2025 memory-safety fixes. The one place complexity beats density here. |
| 9 | `bytearrayobject.c:1640` `bytearray_translate_impl` | `memory-pattern-analyzer` | Lower confidence — `inlen` read at :1682 before an allocation that can trigger GC; `input` reloaded after. **Unverified.** |

**Deprioritize:** integer overflow in size arithmetic. 45 all-time commits but the class was swept 2016–2019 and has produced ~1 commit in six years. A zero here is a real negative, not silence.

**Class most likely to yield a novel finding:** re-entrancy-during-buffer-export in `bytearrayobject.c`. Steepest recent slope (4 of 6 all-time UAF commits in 2025), a well-established guarded twin (`ob_exports++`), an open unmerged PR, and a documented three-year incomplete-fix precedent in the same file (gh-91153).

---

## 7. What I did not do

- **No reproducers.** Phase 1 preflight; FP-2 is static-confirmed by reading only.
- **Two rows of the FP-3 table (`bytearray_subscript_lock_held`, `bytearray_take_bytes_impl`) are sweep output I did not hand-verify.** Treat as leads.
- **Did not enumerate gh-149816's 22 sub-issues** — named as a cheap follow-up.
- **`bytes_methods.c` got the least attention.** 3 crash-fix commits all-time, 6 commits since 2024, 738 lines, no complexity hotspots, density rank 32. It is genuinely quiet — but note it is *shared* by bytes and bytearray, so a defect there is automatically a two-type defect. Its quietness is a reason to read it cold, not a reason to skip it.
- **The `function_churn` pass** ranks by edit count and is dominated by the 2024–25 FT sweep; I used the hand-built fix corpus for repeat offenders instead and flagged that choice rather than quoting a number I do not trust.
