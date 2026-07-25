# Git History Context — `Objects/typeobject.c` PASS 2 (preflight, runs FIRST)

CPython `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0).
Scope: the **pass-2 regions only** — the parts of the file pass 1 never read.

**Relationship to pass 1.** `reports/obj-typeobject/preflight/history_context.md` established the
*region*-level picture and ran **late** (biased toward prior-raising). This document runs **first**,
is **function**-granular, and is a *where-to-look* document. It does not restate pass 1's region
table; it drills into the eight regions pass 1 deferred.

**Method.** Function bodies were extracted from the current tree (427 top-level functions), the 124
that fall inside the pass-2 regions were each fed to `git log -L <start>,<end>:Objects/typeobject.c`
(which follows the range backwards through renames and reflows), and each commit was classified by a
crash-shaped subject lexicon. **390 distinct commits** touch pass-2 function bodies.

> **Density caveat.** These densities are computed over *function bodies only*, excluding the
> inter-function whitespace, comment blocks, `slotdefs[]` rows and clinic output that pass 1's
> line-range regions included. Numbers here are therefore systematically **higher** than pass 1's
> and are **not** comparable to them. Compare rows within this document only.

---

## 0. Clone health

- **Shallow clone: NO.** `git rev-parse --is-shallow-repository` → `false`. Verified directly, and
  the envelope's `is_shallow_clone` agrees. History is complete and the analysis below is sound.
- **Repo depth:** 132,320 commits, back to **1990-08-09**. `Objects/typeobject.c` itself carries
  **1,282** commits (1,342 with `--follow`), oldest reaching the 1990 initial import. Decades deep,
  as expected.
- **Commits analyzed (script):** 1,280 in window `--days 13000` (1990-12-21 → 2026-07-25).
- **Commit cap applied: NO** (`commit_cap_applied: false`, `max_commits: 50000`). **Timeout: NO.**
- **Envelope `notes[]`** — one entry, reproduced verbatim:
  > "Prefer `watchlist` (recent crash-fix commits per KLOC) over `file_churn[].churn_rate` for
  > prioritization: on CPython's Objects/ raw churn was anti-correlated with defect density."

  No `is_shallow_clone`, no `COMMIT CAP APPLIED`, no `SCRIPT TIMEOUT`. Nothing is truncated.

---

## 1. Bug-fix density — per region, then per function

### 1a. Region rollup (commits deduped by hash within each region)

| Rank | Region | Lines (fn bodies) | Funcs | Commits | crash-fix all | 3y | 12mo | **3y /KLOC** |
|-----:|--------|------------------:|------:|--------:|--------------:|---:|-----:|-------------:|
| 1 | **R19 type-attribute lookup cache** (6140–6452) | 322 | 18 | 58 | 20 | **16** | 0 | **49.7** |
| 2 | **R21 getattro / setattro** (6529–6848) | 313 | 8 | 54 | 10 | **9** | **3** | **28.8** |
| 3 | **R3 managed static types** (228–522) | 275 | 21 | 19 | 7 | 7 | 0 | 25.5 |
| 4 | **R6 watchers / version tags** (971–1481) | 486 | 24 | 82 | 14 | **12** | 0 | 24.7 |
| 5 | **R11 MRO C3 linearisation** (3217–3702) | 413 | 14 | 76 | 13 | 9 | 1 | 21.8 |
| 6 | **R25 `__class__` assignment** (7482–7846) | 356 | 13 | 92 | 13 | 7 | 0 | 19.7 |
| 7 | **R37 super** (12534–13068) | 407 | 13 | 95 | 9 | 7 | 1 | 17.2 |
| 8 | **R26 pickle / `__reduce__`** (7848–8406) | 484 | 13 | 75 | 6 | **2** | 0 | **4.1** |

**The actionable read.** Pass 1 predicted four of the six most crash-fix-dense regions were in
pass-2 territory; that holds, and the ordering sharpens at function granularity. The top four
regions — R19, R21, R3, R6 — are **one subsystem**: the free-threaded type-version / cache
invalidation spine. They account for **44 of the 69** crash-shaped fixes across all pass-2 regions
in the last three years, in 1.40 KLOC.

**R26 (pickle) is the only genuinely cold pass-2 region** — 2 crash fixes in three years, newest
substantive fix 2024-10-11. Deprioritize it; the budget it frees belongs to R19/R21.

### 1b. Functions ranked by 3-year crash-fix density

Denominator artifacts (bodies under 20 lines, where a handful of fixes produces an absurd /KLOC)
are **separated out below** rather than allowed to top the table.

| /KLOC | 3y | 12mo | all | commits | lines | Region | Function |
|------:|---:|-----:|----:|--------:|------:|--------|----------|
| 153.8 | 4 | 0 | 4 | 7 | 26 | R37 | `PyType_Freeze`:12506–12531 |
| 133.3 | 4 | 0 | 4 | 8 | 30 | R6 | `set_version_unlocked`:1134–1163 |
| 125.0 | 5 | 0 | 6 | 24 | 40 | R11 | `mro_invoke`:3591–3630 |
| 125.0 | 5 | 0 | 6 | 29 | 40 | R25 | `object_set_class`:7800–7839 |
| 117.6 | 6 | 0 | **9** | 29 | 51 | R19 | `find_name_in_mro`:6145–6195 |
| 106.4 | 5 | 0 | 6 | 25 | 47 | R11 | `mro_internal`:3655–3701 |
| 93.3 | **7** | 0 | 7 | 33 | 75 | R6 | `_PyType_Modified_Unlocked`:1166–1240 |
| 90.9 | 2 | 0 | 2 | 4 | 22 | R3 | `static_ext_type_lookup`:261–282 |
| 90.9 | 2 | 0 | 2 | 3 | 22 | R21 | `update_slot_after_setattr`:6726–6747 |
| 86.4 | **7** | 0 | **10** | 38 | 81 | R19 | `_PyType_LookupStackRefAndVersion`:6306–6386 |
| 81.8 | 4 | 0 | 4 | 6 | 22 | R21 | `_PyType_SetFlagsRecursive`:6506–6527 |
| 72.9 | **7** | **2** | 7 | 30 | 96 | R21 | `type_setattro`:6750–6845 |
| 71.4 | 2 | 0 | 3 | 16 | 28 | R11 | `mro_check`:3547–3574 |
| 69.8 | 3 | 0 | 3 | 7 | 43 | R3 | `managed_static_type_state_clear`:382–424 |
| 66.7 | 3 | **1** | 4 | 38 | 45 | R37 | `_PySuper_LookupDescr`:12588–12632 |
| 65.2 | 3 | 0 | 4 | 23 | 46 | R6 | `type_mro_modified`:1279–1324 |
| 62.5 | 2 | 0 | 2 | 5 | 32 | R21 | `type_update_dict`:6692–6723 |
| 44.1 | 3 | 0 | 3 | 9 | 68 | R3 | `managed_static_type_state_init`:310–377 |
| 41.7 | 2 | 0 | 2 | 22 | 48 | R6 | `assign_version_tag`:1408–1455 |
| 40.8 | 4 | 0 | 5 | 38 | 98 | R25 | `object_set_class_world_stopped`:7700–7797 |
| 23.3 | 2 | **1** | 2 | 22 | 86 | R11 | `mro_implementation_unlocked`:3431–3516 |
| 16.8 | 2 | **1** | 3 | 21 | 119 | R21 | `_Py_type_getattro_stackref`:6571–6689 |

**Denominator artifacts** (real signal, unusable density): `type_set_flags`:455–459 (5 lines, 4
crash fixes/3y), `type_set_flags_with_mask`:462–467 (6 lines, 4), `_PyType_SetFlags`:6453–6462
(10 lines, **5 of its 5 lifetime commits are crash fixes**), `_PyType_Lookup`:6400–6405 (2 of 2),
`update_cache`:6216–6232 (3 of 5), `_PyType_LookupRefAndVersion`:6284–6295 (3 of 5),
`_PyType_SetVersion`:1336–1341 (2 of 2), `PyType_Modified`:1243–1253 (2 of 5). These are thin
wrappers around the flag/version/cache state — **every one of them exists because a race was found
there**, and several were *created* by a race fix. Treat them as part of their callee's risk, not
as independent hotspots.

**Answering the question directly — which pass-2 functions have absorbed the most crash/UAF/race
fixes?** `_PyType_LookupStackRefAndVersion` (10 lifetime, 7 in 3y), `find_name_in_mro` (9 / 6),
`_PyType_Modified_Unlocked` (7 / 7), `type_setattro` (7 / 7, **2 in the last 12 months**),
`mro_invoke` / `mro_internal` / `object_set_class` (6 / 5 each). The first four are the file's
type-version invalidation core, and all four are still being fixed.

---

## 2. Recurring fix clusters — restricted to the pass-2 regions

Deduped by hash over the 390 commits that touch pass-2 function bodies. Year histograms from 2019.

### **free-threading data race — 26 commits, ALL fix-shaped. THE dominant pass-2 cluster. STILL ACTIVE.**
`2024:16 · 2025:8 · 2026:2`

Every single commit in this cluster is 2024 or later — it did not exist before PEP 703 reached the
type system, and it has not stopped.

- `94bca40ff09c` 2026-05-13 R21 — gh-148906: fix performance scaling of descriptors on free-threading
- `fbbbc10055e0` 2025-05-27 **all 7 pass-2 regions** — gh-127266: avoid data races when updating type slots (the reland — see §4)
- `31d1342de948` 2025-04-28 R19 — gh-132942: **Fix races in type lookup cache**
- `1d3cf79a501a` 2024-07-08 R19 — gh-121368: **Fix seq lock memory ordering in `_PyType_Lookup`**

→ **ft-race-scanner, lock-discipline-checker, stw-safety-checker.** A hit anywhere in R19/R21/R6/R3
is high-prior: this exact code has been wrong repeatedly within 24 months.

### **type-cache invalidation — 24 commits (22 fix-shaped). STILL ACTIVE, longest-running cluster.**
`2019:1 · 2020:3 · 2021:2 · 2022:1 · 2023:1 · 2024:5 · 2025:4 · 2026:1`

The only cluster with continuous annual activity for **eight straight years**. It survived the
per-interpreter migration, the seqlock rewrite, and free-threading, and is still producing fixes.

- `495178ab058b` 2026-03-26 R6 — gh-146453: fix `_PyType_LookupByVersion` for types with **fixed pre-defined version tags**
- `31d1342de948` 2025-04-28 R19 — gh-132942: Fix races in type lookup cache
- `aa6579cb60b4` 2025-01-13 R6 — gh-127773: **Disable attribute cache on incompatible MRO entries**
- `c22323cd1c20` 2024-05-29 R19 — gh-119525: **Fix deadlock** with `_PyType_Lookup` and the GIL

→ **ft-race-scanner, lock-discipline-checker** on `update_cache` / `update_cache_gil_disabled` /
`_PyType_LookupStackRefAndVersion`.

### **version tag / `type_version` — 13 commits (11 fix-shaped). STILL ACTIVE; newest is 2 months old.**
`2019:1 · 2021:3 · 2023:1 · 2024:2 · 2025:1 · 2026:2`

- `e7eaed56149a` 2026-05-23 R21 — gh-148450: **`abc.register` needs to update `type_version` when `tp_flags` is changed`** (see §4.3 — landed on one path only)
- `495178ab058b` 2026-03-26 R6 — gh-146453: `_PyType_LookupByVersion` vs pre-defined version tags
- `57f45ee2d8ee` 2025-02-12 R6 — gh-128759: **Fix accesses to `tp_version_tag`**
- `992446dd5bd3` 2024-02-05 R6 — GH-113462: **Limit the number of versions** a single class can use

The recurring shape is *state changed without the version tag being invalidated*. It has been fixed
four times in three years, each time for a different mutation path.

### **MRO / C3 linearisation — 11 commits (9 fix-shaped). STILL ACTIVE.**
`2019:1 · 2022:1 · 2024:1 · 2025:2`

- `a8edca62fc6d` 2025-10-21 R11 — gh-132835: **Add defensive NULL checks to MRO resolution**
- `aa6579cb60b4` 2025-01-13 R6 — gh-127773: Disable attribute cache on incompatible MRO entries
- `85354ed78c0e` 2022-05-06 R6 — gh-92112: **Fix crash triggered by an evil custom `mro()`**

Low volume, but the two most recent are both about *the MRO not being what the code assumed* — a
user-supplied `mro()` returning something hostile, and MRO entries that are not types. Pass 1
already flagged this as reactively-enumerated. → **null-safety-scanner, init-bypass-checker.**

### **`__class__` assignment — 12 commits (7 fix-shaped). STILL ACTIVE; newest 2026-03-06.**
`2021:1 · 2024:2 · 2025:1 · 2026:1`

- `1d091a336e60` 2026-03-06 R25 — gh-145566: **Skip stop-the-world** when reassigning `__class__` on newly created objects (see §3.1 and §4.2)
- `29d026f93e14` 2025-09-15 R25 — gh-37817: Allow assignment to `__bases__` of direct subclasses of builtin classes
- `3bfc9c831ad9` 2024-07-11 R25 — gh-120198: **Stop the world** when setting `__class__` on free-threaded build
- `203565b2f9c7` 2024-06-12 R25 — gh-120198: Fix **race condition** when editing `__class__` with an audit hook active

Note the arc: 2024 *added* stop-the-world to `object_set_class`; 2026 *removed* it again on a fast
path. → **stw-safety-checker, ft-race-scanner.**

### **lock / critical section / world-stop — 7 commits, all fix-shaped. STILL ACTIVE.**
`2024:5 · 2025:1 · 2026:1`

- `1d091a336e60` 2026-03-06 R25 — skip STW for uniquely-referenced objects
- `6e1e78054060` 2025-01-13 R6 — gh-128759: **fix data race in `type_modified_unlocked`**
- `c22323cd1c20` 2024-05-29 R19 — gh-119525: **Fix deadlock** with `_PyType_Lookup` and the GIL

### **subclasses bookkeeping — 9 commits (6 fix-shaped). Sporadic; newest 2025-09-15.**
`2022:1 · 2025:1` (the rest are pre-2019)

- `29d026f93e14` 2025-09-15 R25 — gh-37817: Allow `__bases__` assignment on direct subclasses of builtins
- `87154d8dd890` 2022-08-04 R3/R6 — gh-94673: Add Per-Interpreter `tp_subclasses` for Static Builtin Types
- `84745ab464f9` 2013-10-29 R6 — Issue #17936: Fix **O(n\*\*2) behaviour** when adding or removing many subclasses

### **refcount / leak — 16 commits (15 fix-shaped). Cooling; newest 2025-03-26.**
`2019:1 · 2022:2 · 2023:3 · 2024:1 · 2025:1`

- `67fbfb42bd5d` 2025-03-26 R11/R19/R6 — gh-131586: Avoid **refcount contention** in some "special" calls
- `17c484998190` 2024-03-04 R26 — gh-116296: **Fix refleak in `reduce_newobj()` corner case**
- `a781484c8e98` 2023-05-12 R37 — **Fix refleak in `super_descr_get`** (pass 1's CPY-0007 function)

### **pickle / `__reduce__` — 33 commits (24 fix-shaped) but DORMANT. Newest 2024-10-11.**
`2022:2 · 2024:3`

Historically the single largest fix cluster in pass-2 territory *by lifetime count*, and now
effectively finished. The last three were `b12e99261e65` 2024-10-11 (gh-125221 FT data race in
`object.__reduce_ex__`), `17c484998190` 2024-03-04 (refleak in `reduce_newobj`), and a typo fix.
This is why R26 ranks 8/8 above. **Confirmed cold — do not spend pass-2 budget here.**

### **type watchers — 2 commits. DORMANT. Newest 2024-06-08.**
`2022:1 · 2024:1`

The watcher API (`PyType_AddWatcher` / `PyType_Watch` / `PyType_Unwatch` / `validate_watcher_id`)
has had **essentially no fix history since it was introduced**. Per the pass-1 rule this is
*unexplored, not safe* — it is a public unstable API with callback dispatch inside the invalidation
path, and nobody has filed a bug against it.

### **NULL guard — 11 commits (10 fix-shaped). Thin but never stops.**
`2022:1 · 2025:1`

- `a8edca62fc6d` 2025-10-21 R11 — Add defensive NULL checks to MRO resolution
- `07a2a1b7e512` 2016-09-09 R26 — Additional safe-guard against **dereferencing NULL in `reduce_newobj`**
- `2489bd83f576` 2013-11-23 R26 — Fixed NULL check in `_PyObject_GetItemsIter()` (Coverity)

### **recursion / stack — 1 commit, from 2001. DEAD.**

`875eeaa193fd` 2001-10-11 is the only match, and it is not a fix. Pass 1 flagged the file-wide
recursion cluster as cold; restricted to pass-2 regions it is **nonexistent** — despite pass-2
containing `pmerge` (the C3 merge loop), `mro_implementation_unlocked`, the
`_PyType_Modified_Unlocked` subclass descent, `set_flags_recursive`, and
`get_base_by_token_recursive`. The scanner baseline flags 8 `missing_recursion_guard` /
`self_recursion` candidates in this file. **There is no historical precedent either way here — read
the silence as "never tested", not "safe".** → **recursion-guard-auditor.**

---

## 3. The "recently audited for a different defect" list

Pass 1 found 6 of its 9 top findings sat in lines a recent commit had already audited for a
*different* defect class. Reproduced for pass 2, this is the strongest single result in this
document.

### 3.1 The headline: **7 of the 18 NET NEW candidates were created by one refactor that was not a safety audit**

`git blame` on all 18 NET NEW lines from `PASS2_BASELINE.md`:

| Line | Function | Blamed to | Stated purpose | Class NOT being looked for |
|------|----------|-----------|----------------|----------------------------|
| :793 | `_PyType_GetSubclasses` | `f73abf8e03fd` 2023-05-01 | encapsulation | borrowed-field lifetime |
| :5747 | `type_from_slots_or_spec` | `f73abf8e03fd` 2023-05-01 | encapsulation | borrowed-field lifetime |
| :8655 | `type_add_method` | `f73abf8e03fd` 2023-05-01 | encapsulation | borrowed-field lifetime |
| :9166 | `type_dict_set_doc` | `f73abf8e03fd` 2023-05-01 | encapsulation | borrowed-field lifetime |
| :9388 | `type_ready_set_hash` | `f73abf8e03fd` 2023-05-01 | encapsulation | borrowed-field lifetime |
| :10516 | `add_tp_new_wrapper` | `f73abf8e03fd` 2023-05-01 | encapsulation | borrowed-field lifetime |
| :8706 | `type_add_members` | `ef3ceab09d2d` 2024-02-07 | **borrowed *return* value** | borrowed *receiver* |
| :8731 | `type_add_getset` | `ef3ceab09d2d` 2024-02-07 | **borrowed *return* value** | borrowed *receiver* |
| :8816 | `overrides_hash` | `81c72044a181` 2022-02-08 | `_Py_IDENTIFIER` → global objects | lifetime |
| :12477 | `add_operators` | `b510e101f8b5` 2020-10-26 | use `PyDict_Contains`/`SetDefault` | lifetime |
| :3443 | `mro_implementation_unlocked` | `bc67f189fdd6` 2022-01-21 | add `_PyType_CAST()` macro | lifetime |
| :9272 | `type_ready_mro` | `bc67f189fdd6` 2022-01-21 | add `_PyType_CAST()` macro | lifetime |
| :9403 | `type_ready_add_subclasses` | `f95a1b3c53bd` 2010-05-09 | (svnmerge) | — |
| :4815 | `type_new_set_attrs` | `e42bda944111` 2025-04-17 | **speed up class creation** | lifetime |
| :2007 | `type_set_bases_unlocked` | `104b9e0ccac4` 2015-02-05 | custom `mro()` edge cases | publish-before-init |
| :6002 | `get_base_by_token_recursive` | `120b891e4dff` 2024-10-10 | **simplify** `PyType_GetBaseByToken` | recursion depth |
| :3894 | `apply_slot_updates` | `fbbbc10055e0` 2025-05-27 | **the reverted-and-relanded race fix** | param NULL |
| :6620 | `_Py_type_getattro_stackref` | `d891b2bbd16c` 2026-02-06 | **namedtuple perf scaling** | publish-before-init |

**`f73abf8e03fd` (gh-94673, 2023-05-01, Eric Snow) is the commit that created the pattern the
scanner now flags.** Its own message states the purpose: *"This makes it much cleaner to move more
`PyTypeObject` fields to `PyInterpreterState`."* It mechanically introduced ~20 `PyObject *dict =
lookup_tp_dict(type);` sites across a 559-line diff in this file. It was an **encapsulation
refactor, not a lifetime audit** — the question of whether `dict` stays valid across the following
call was never asked, because the diff was replacing `type->tp_dict` with an accessor returning the
same borrowed pointer. Six of the 13 `borrowed_field_deref_across_call` candidates are its direct
output.

**`ef3ceab09d2d` (gh-112066, 2024-02-07, Sam Gross) is the sharpest case.** It converted
`PyDict_SetDefault` → `PyDict_SetDefaultRef` in `type_add_members` and `type_add_getset` — an audit
*explicitly about borrowed references*. It fixed the borrowed **return value** and left the borrowed
**receiver** (`dict`) untouched, at the same two lines the scanner now flags (:8706, :8731). The
adjacent defect class was audited; this one was not.

**Triage guidance for the 13-candidate `borrowed_field_deref_across_call` group.** Per
`RUN_CONTEXT.md` the discriminator is whether the field can actually be cleared re-entrantly from
that window. The blame data splits the group cleanly:
- the six `f73abf8e03fd` sites and the two `bc67f189fdd6` sites are `lookup_tp_dict` / `_PyType_CAST`
  mechanical rewrites — **most likely ACCEPTABLE**, exactly as `RUN_CONTEXT.md` predicted;
- **:8706 and :8731 are the outliers worth real attention** — they are the two the borrowed-ref
  audit already visited and half-fixed;
- **:4815 (`type_new_set_attrs`) is the second outlier** — blamed to `e42bda944111` 2025-04-17
  "gh-132042: **Do not lookup `tp_dict` each time to speedup class creation**", i.e. a commit whose
  entire purpose was to *hoist the borrowed `dict` load out of the loop and hold it longer*. That is
  the one change in the set that actively widened the borrow window, and it was made for speed.

### 3.2 Region-level recent touches, and what they were not looking for

32 commits since 2025-01-01 touch pass-2 function bodies. The ones that matter:

| Commit | Date | Regions | Stated reason | Therefore NOT audited for |
|--------|------|---------|---------------|---------------------------|
| `cd5217283112` | 2026-03-12 | R19 | "**Improve scaling** of type attribute lookups" | correctness of the new fast path |
| `f26eca7732ca` | 2026-03-10 | R19 | "Update `find_name_in_mro()` to **return a `_PyStackRef`**" | refcount semantics of the new return convention |
| `bdf6de8c3f0c` | 2026-03-11 | R37 | "**Avoid contention** on TYPE_LOCK in `super()` lookups" | what the removed lock was protecting |
| `d891b2bbd16c` | 2026-02-06 | R21 | "Improve **namedtuple scaling**" | publish-before-init in `_Py_type_getattro_stackref` |
| `ce791541769a` | 2025-11-19 | R21 | "fix free-threading `dataclass.__init__` **perf** issue" | correctness of `type_setattro` |
| `94bca40ff09c` | 2026-05-13 | R21 | "fix **performance scaling** of descriptors" | correctness |
| `458aca92370d` | 2026-04-09 | R37 | "**fold** super method lookups **in JIT**" | NULL/init-bypass state |
| `3d40317ed24d` | 2025-02-25 | R21/R25/R37 | "fix **UBSan** failures" | anything UBSan cannot reach |
| `ecbd31ee3906` | 2026-06-15 | R19 | "Replace `_PyObject_HashFast()` with `PyObject_Hash()`" | hash re-entrancy |
| `a933e9ccee6d` | 2026-03-28 | R26 | "Use `_PyTuple_FromPair[Steal]`" | steal semantics on error paths |

**The pattern is unambiguous: the last 18 months of activity in R19, R21 and R37 is
*performance work*, not correctness work.** Five separate commits (gh-145685 ×3, gh-139103 ×2,
gh-148906) rewrote the hottest lookup paths in the file to reduce lock contention and improve
free-threaded scaling. Every one of them *removed or narrowed* synchronization. None of them was a
race audit. The regions with the highest historical race-fix density have just been rewritten by
people optimizing them.

`3d40317ed24d` (gh-111178 UBSan) deserves its own note: it swept R21, R25 **and** R37, touching
`_Py_type_getattro_stackref`, `object_repr`, `type_setattro`, `super_repr` **and `super_descr_get`**
— and, as pass 1 established for CPY-0007, did not surface the missing `su->type` NULL guard,
because UBSan never reaches it without the `__new__`-bypass input. A clean-UBSan signal over these
functions should be given **zero** weight against an init-bypass or race finding.

---

## 4. Reverted and incomplete work

### 4.1 gh-127266 — landed, reverted **within 10 hours**, relanded a month later

```
e414a2d81c3e  2025-04-28 13:28 -0700  gh-127266: avoid data races when updating type slots (gh-131174)
              ↓  10 hours later
eecafc33800c  2025-04-28 23:38 -0700  Revert gh-127266 (gh-133129)
              "This is triggering deadlocks in test_opcache. See GH-133130 for stack trace."
              ↓  29 days later
fbbbc10055e0  2025-05-27 18:27 -0700  gh-127266: avoid data races when updating type slots (gh-133177)
```

This is the **widest-reaching** change in pass-2 territory: the reland touches functions in **all
seven** pass-2 regions with function-level hits — `PyType_Freeze`, `_PyType_Modified_Unlocked`,
`_PyType_SetFlags`, `_PyType_SetFlagsRecursive`, `class_name`, `find_name_in_mro`, `mro_internal`,
`mro_invoke`, `object_set_class`, `set_version_unlocked` and more. Its stated design is: *"Remove
the use of atomics when reading or writing type flags. The use of atomics is not sufficient to avoid
races (since flags are sometimes read without a lock and without atomics) and are no longer
required."* — i.e. it **deleted** the atomics and replaced them with a stop-the-world discipline,
and the first attempt deadlocked.

It also created `apply_slot_updates` (:3894), one of the NET NEW candidates, in the deferred
slot-update queue that pass 1 identified as having **zero** fix commits since creation.

### 4.2 gh-150490 — landed, deadlocked, patch raced, **reverted**, relanded 19 days before HEAD

```
efb2fffae1f  2026-06-11  gh-150490: Raise PyType_Modified for insertion into split dictionary
             ↓ gh-151593: dead lock in PyDict insert_split_key (hit in CI)
             ↓ gh-152200 attempted fix
9626ef87f42  2026-06-26  gh-152238: Revert gh-150490 and gh-152200. (gh-152232)
             "it violates locking discipline and results in deadlocks ... The attempted fix
              gh-152200 avoids the deadlock but introduces a data-race. The race window is
              small but can be triggered with pure Python code."
20921922fa4  2026-07-06  gh-150490: ... (take 2) (#152914)
```

Pass 1 flagged this cycle. **Pass 2 can now say what "take 2" actually did in this file**, and it
changes the priority. The diff is 13 lines, and it is almost entirely a rename:

> `static void type_modified_unlocked(...)` → **`void _PyType_Modified_Unlocked(...)`**

plus one substantive change in `type_dealloc` (`_PyDictKeys_DecRef(et->ht_cached_keys)` →
`_PyDict_RemoveKeysForClass(et)`). The rename un-statics the invalidator and exports it via
`Include/internal/pycore_typeobject.h:152`. Its one external caller is:

- **`Objects/dictobject.c:1971` → `_PyType_Modified_Unlocked(type)`**

So as of 19 days before HEAD, **`_PyType_Modified_Unlocked` — the highest-3y-crash-fix function in
R6 (7 fixes/3y), which recursively descends `tp_subclasses` and is documented in-tree as
"re-entrant and cannot run with the world stopped" — is now re-entered from inside dict split-key
insertion**, which is precisely the call path that produced a deadlock and then a
pure-Python-triggerable data race on the two previous attempts. Its recursive descent is also the
`CPY-0087` `self_recursion` candidate at `:1206`.

→ **lock-discipline-checker and ft-race-scanner: this is the single highest-value target in pass 2.**

### 4.3 Fix landed on one path and not its sibling — `_PyType_SetFlags` vs `_PyType_SetFlagsRecursive`

`e7eaed56149a` 2026-05-23 (gh-148450, *"`abc.register` needs to update `type_version` when
`tp_flags` is changed"*) added version-tag invalidation to `_PyType_SetFlagsRecursive`
(`Objects/typeobject.c:6506`):

```c
    BEGIN_TYPE_LOCK();
    if (!PyType_HasFeature(self, Py_TPFLAGS_IMMUTABLETYPE) &&
        (self->tp_flags & mask) != flags)
    {
        _PyType_Modified_Unlocked(self);      /* <-- the fix */
    }
    type_lock_prevent_release();
    types_stop_world();
    set_flags_recursive(self, mask, flags);
```

Its non-recursive sibling `_PyType_SetFlags` (`Objects/typeobject.c:6453`), 53 lines earlier in the
same file, still reads in full:

```c
_PyType_SetFlags(PyTypeObject *self, unsigned long mask, unsigned long flags)
{
    unsigned long new_flags = (self->tp_flags & ~mask) | flags;
    if (new_flags != self->tp_flags) {
        types_stop_world();
        // can't use new_flags here since they could be out-of-date
        self->tp_flags = (self->tp_flags & ~mask) | flags;
        types_start_world();
    }
}
```

**No version-tag invalidation.** Both functions are declared adjacently in
`Include/internal/pycore_typeobject.h:142/148`, and **both are called from the same module**:

- `Modules/_abc.c:541` → `_PyType_SetFlags(self, 0, val & COLLECTION_FLAGS)` — sets
  `Py_TPFLAGS_SEQUENCE`/`Py_TPFLAGS_MAPPING` from `__abc_tpflags__` during ABC init
- `Modules/_abc.c:601` → `_PyType_SetFlagsRecursive(subclass, ...)` — the path gh-148450 fixed

The stated bug — *"needs to update `type_version` when `tp_flags` is changed"* — is a property of
mutating `tp_flags`, not of recursing. The fix was applied to the reported call site only.

*This is a history observation, not a verdict.* Whether it is exploitable depends on whether a type
can already hold a cached version tag when `_abc.c:541` runs (the type is comparatively fresh at ABC
init). **That determination belongs to the safety agents** — but the asymmetry is real, it is two
months old, and the guarded twin is 53 lines away in the same file.

### 4.4 The 2024→2026 stop-the-world reversal in `object_set_class`

Not a revert, but the same shape: a synchronization decision made for correctness in 2024 and
partially undone for performance in 2026.

- `3bfc9c831ad9` 2024-07-11 — gh-120198: **Stop the world** when setting `__class__` on free-threaded build
- `1d091a336e60` 2026-03-06 — gh-145566: **Skip stop-the-world** when reassigning `__class__` on newly created objects

```c
    int unique = _PyObject_IsUniquelyReferenced(self);
    if (!unique) {
        types_stop_world();
    }
    PyTypeObject *oldto = Py_TYPE(self);
    int res = object_set_class_world_stopped(self, newto);
    if (!unique) {
        types_start_world();
    }
```

`object_set_class_world_stopped` (`Objects/typeobject.c:7700`, 98 lines, 5 lifetime crash fixes) has
**exactly one caller** — this one. A function whose name asserts the world is stopped now runs with
the world **not** stopped whenever `_PyObject_IsUniquelyReferenced(self)` holds. Every invariant its
body relies on must now be re-derived from unique-reference-ness alone. It is 141 days old.

→ **stw-safety-checker** (highest-value target after §4.2), **ft-race-scanner.**

### 4.5 Other new-and-untested pass-2 code

Functions in pass-2 regions whose oldest touching commit is recent and which have **zero** crash-fix
history: `should_assign_version_tag`:6298–6303 (born 2026-03-12, gh-145685 perf),
`PyObject_GetItemData_DuringGC`:6129–6132 and `PyObject_GetItemData`:6135–6138 (born 2026-04-08),
`_PyType_LookupByVersion`:1344–1387 (born 2024-06-08, 0 crash fixes but *fixed for correctness*
2026-03-26), `_PyStaticType_GetBuiltins`:429–447 (born 2024-08-12, 1 commit ever),
`_PySuper_Lookup`:12769–12778 (born 2023-04-24, 1 commit ever). Per pass 1's rule: **do not read
silence as safety.**

---

## 5. WATCHLIST — the 15 pass-2 functions to read first

Ranked by crash-fix density with recency weighting, then adjusted upward for the §4 findings.
All paths are `/home/danzin/projects/cpython/Objects/typeobject.c`.

| # | Function : lines | Region | 3y crash-fix (/KLOC) | Why it is here | Read with |
|--:|------------------|--------|---------------------:|----------------|-----------|
| 1 | `_PyType_Modified_Unlocked`:1166–1240 | R6 | 7 (93.3) | Top R6 density **and** §4.2 — newly exported, now re-entered from `dictobject.c:1971`, 19 days old, two prior attempts deadlocked/raced. Recursive over `tp_subclasses` (CPY-0087 `:1206`). | **lock-discipline, ft-race, recursion-guard** |
| 2 | `_PyType_LookupStackRefAndVersion`:6306–6386 | R19 | 7 (86.4) | Most crash-fixed function in pass 2 (**10 lifetime**). Rewritten twice for *perf* in 2026 (§3.2) after 4 race fixes. | **ft-race, lock-discipline, refcount** |
| 3 | `type_setattro`:6750–6845 | R21 | 7 (72.9), **2 in 12mo** | Only pass-2 function with 2 crash fixes in the last year. Touched by the §4.2 reland. | **ft-race, lock-discipline, error-path** |
| 4 | `object_set_class_world_stopped`:7700–7797 | R25 | 4 (40.8) | §4.4 — its sole caller now skips stop-the-world on a fast path added 141 days ago. Name asserts an invariant that no longer always holds. | **stw-safety, ft-race** |
| 5 | `find_name_in_mro`:6145–6195 | R19 | 6 (117.6) | 9 lifetime crash fixes; return convention changed to `_PyStackRef` in 2026-03 for perf (§3.2). | **ft-race, refcount** |
| 6 | `_PyType_SetFlagsRecursive`:6506–6527 + `_PyType_SetFlags`:6453–6462 | R21/R19 | 4 (181.8) / 5 of 5 | §4.3 — gh-148450's version-tag fix landed on the recursive one only; the sibling is 53 lines away and called from the same module. | **ft-race, lock-discipline** |
| 7 | `object_set_class`:7800–7839 | R25 | 5 (125.0) | The STW-skip fast path itself. 6 lifetime crash fixes. | **stw-safety, ft-race** |
| 8 | `set_version_unlocked`:1134–1163 | R6 | 4 (133.3) | Version-tag write path; touched by the §4.2 reland and by 3 of the 4 version-tag cluster fixes. | **ft-race, lock-discipline** |
| 9 | `mro_invoke`:3591–3630 | R11 | 5 (125.0) | Calls user-supplied `mro()`. gh-92112 "crash triggered by an evil custom `mro()`" is its cluster. | **null-safety, init-bypass, error-path** |
| 10 | `mro_internal`:3655–3701 | R11 | 5 (106.4) | 6 lifetime crash fixes; touched by both §4.1 and §4.2. | **null-safety, ft-race** |
| 11 | `mro_implementation_unlocked`:3431–3516 | R11 | 2 (23.3), 1 in 12mo | gh-132835 "Add **defensive** NULL checks to MRO resolution" landed here 2025-10-21 — *defensive* means the invariants were not enumerated. Holds NET NEW candidate `:3443`. | **null-safety, recursion-guard** |
| 12 | `_Py_type_getattro_stackref`:6571–6689 | R21 | 2 (16.8), 1 in 12mo | Largest pass-2 function (119 lines). Last touched 2026-02-06 for *namedtuple perf*; holds NET NEW candidate `:6620` (`publish_before_init_complete`). | **ft-race, pyerr-clear, error-path** |
| 13 | `type_mro_modified`:1279–1324 | R6 | 3 (65.2) | The cache-disabling path (gh-127773 "Disable attribute cache on incompatible MRO entries"). Interacts with #1 and #9. | **ft-race, null-safety** |
| 14 | `_PySuper_LookupDescr`:12588–12632 | R37 | 3 (66.7), 1 in 12mo | The hottest `super` function by a wide margin (38 commits). Lost TYPE_LOCK contention 2026-03-11 and was JIT-folded 2026-04-09 — both perf, neither a correctness audit. | **ft-race, lock-discipline** |
| 15 | `managed_static_type_state_init`:310–377 + `_clear`:382–424 | R3 | 3+3 (44.1 / 69.8) | R3's only substantial functions; the whole region's 7 crash fixes/3y concentrate here. Last touched by gh-129824 "fix data races in subinterpreters under TSAN". | **ft-race, stw-safety, refcount** |

**Just below the cut**, listed so they are not mistaken for cleared: `update_cache`:6216–6232 and
`update_cache_gil_disabled`:6237–6257 (3 and 2 of very few lifetime commits are race fixes;
`c22323cd1c20` fixed a *deadlock* here), `type_update_dict`:6692–6723 (2/3y; the re-entrancy comment
in its body is load-bearing), `PyType_Freeze`:12506–12531 (4/3y, touched by both §4.1 and §4.2),
`assign_version_tag`:1408–1455 (CPY-0087 `:1431`).

**Explicitly deprioritized:** all of **R26 pickle / `__reduce__`** (`object_getstate_default`,
`reduce_newobj`, `_PyObject_GetNewArguments`, `_PyType_GetSlotNames`, `_common_reduce`,
`object___reduce_ex___impl`). 2 crash fixes in three years, newest substantive fix 2024-03-04, and
its historically large fix cluster is closed. The one caveat: its two NULL-guard fixes
(`07a2a1b7e512` 2016, `2489bd83f576` 2013 — both Coverity-driven) show the region's NULL invariants
were only ever enumerated by a static analyser, never by a fuzzer.

---

## 6. Priority hand-off

| Prior | Target | Agent that should be believed there |
|-------|--------|-------------------------------------|
| **Highest** | `_PyType_Modified_Unlocked`:1166–1240 + its new caller `Objects/dictobject.c:1971` | **lock-discipline-checker, ft-race-scanner** — §4.2, 19 days old, two prior attempts produced a deadlock and a pure-Python-triggerable race |
| **Highest** | `object_set_class`:7800 / `object_set_class_world_stopped`:7700 | **stw-safety-checker** — §4.4, a `_world_stopped` function whose only caller now conditionally does not stop the world |
| **Highest** | R19 (6140–6452) + R21 (6529–6848) as a whole | **ft-race-scanner, lock-discipline-checker** — 25 of 69 pass-2 crash fixes/3y, and the last 18 months of change there was *perf work that narrowed synchronization* (§3.2) |
| **High** | `_PyType_SetFlags`:6453 vs `_PyType_SetFlagsRecursive`:6506, both `_abc.c` call sites | **ft-race-scanner** — §4.3 sibling gap, 2 months old |
| **High** | R11 `mro_invoke` / `mro_internal` / `mro_implementation_unlocked` | **null-safety-scanner, init-bypass-checker** — "defensive NULL checks" landed 2025-10-21; user-controlled `mro()` is the input |
| **High** | NET NEW `:8706`, `:8731`, `:4815` | **refcount-auditor** — the three `borrowed_field_deref_across_call` candidates whose blame is *not* the mechanical `lookup_tp_dict` refactor (§3.1); `:4815`'s commit deliberately widened the borrow window for speed |
| **Medium-high** | R3 `managed_static_type_state_init` / `_clear` | **ft-race-scanner, stw-safety-checker** — all of R3's crash-fix mass, subinterpreter TSAN fixes 2025-07 |
| **Medium** | Type watchers (`PyType_AddWatcher`/`Watch`/`Unwatch`/`validate_watcher_id`, 1054–1131) | **any agent** — 2 commits ever, zero fix history, callback dispatch inside the invalidation path. Unexplored, not safe |
| **Medium** | `pmerge`:3361–3428, `set_flags_recursive`:6490, `_PyType_Modified_Unlocked`:1206, `get_base_by_token_recursive`:6002 | **recursion-guard-auditor** — the pass-2 recursion cluster is **empty** (1 commit, 2001, not a fix) while the scanner reports 8 candidates. No precedent either way |
| **Low weight against findings** | Anything `3d40317ed24d` (UBSan) touched: `_Py_type_getattro_stackref`, `object_repr`, `type_setattro`, `super_repr`, `super_descr_get` | A clean UBSan sweep is **not** evidence — it missed CPY-0007 in `super_descr_get` on this very pass |
| **Cold** | R26 pickle / `__reduce__` (7848–8406) | Deprioritize. 2 crash fixes/3y, cluster closed 2024 |
