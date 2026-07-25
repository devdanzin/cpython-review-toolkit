# Git History Context — Objects/typeobject.c (preflight, run LATE)

CPython `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0).
Slice: `Objects/typeobject.c`, 13,068 lines, 37 regions per `preflight/include_map.md`.

**Framing note.** This agent ran late — six safety agents were already in flight. Output is
therefore biased toward *prior-raising*: which regions have a fix history that makes a finding
there more likely to be real, and which regions are new-and-therefore-untested. It is not a
"where to look" document.

---

## 0. Clone health

- **Shallow clone: NO.** `git rev-parse --is-shallow-repository` → `false`. Confirmed full clone.
- **Repo depth:** 132,320 commits, back to `85a5fbbdfea` **1990-10-14** ("Initial revision").
- **Commits analyzed:** 1,280 in-window (script) / **1,342 total touching this file with `--follow`**;
  1,282 without follow. Window `--days 13000` (1990-12-21 → 2026-07-25) — covers the entire history.
- **Commit cap applied: NO** (`commit_cap_applied: false`, max 50000). **Timeout: NO.**
- **Envelope `notes[]`** — one entry, reproduced verbatim:
  > "Prefer `watchlist` (recent crash-fix commits per KLOC) over `file_churn[].churn_rate` for
  > prioritization: on CPython's Objects/ raw churn was anti-correlated with defect density."

  No `is_shallow_clone`, no `COMMIT CAP APPLIED`, no `SCRIPT TIMEOUT` warning. Analysis is sound.

File-level watchlist entry (single-file scope): `crash_fix_density: 4.67`/KLOC,
`crash_fix_commits_recent: 61`, `crash_fix_commits: 139`, `commits: 1200`.

Script crash-class distribution over the whole history:
`memory-leak 46 · crash 28 · refcount 22 · data-race 21 · null-deref 8 · overflow 5 ·
uninitialized 3 · assertion 2 · corruption 1 · recursion 1`.

---

## 1. Bug-fix density per region

Attribution is **line-exact**: `git log -L <start>,<end>:Objects/typeobject.c` for each of the 37
regions, which follows the line range backwards through renames and reflows. Commits are deduped
per region; a commit spanning several regions counts in each.

`crash-fix` = commit subject matches a crash-shaped lexicon (crash/segfault/abort/UAF/double-free/
race/deadlock/refcount/leak/NULL/overflow/uninitialized/recursion/assert/corruption).

### 1a. Ranked by 3-year crash-fix density (since 2023-07-25)

| Rank | R | Lines | KLOC | crash-fix 3y | /KLOC | 12mo | Region | Prior-raise for |
|-----:|---|-------|-----:|-------------:|------:|-----:|--------|-----------------|
| 1 | **R2** | 42–227 | 0.19 | **13** | 69.9 | 3 | FT world-stop / type-lock helpers | lock-discipline, stw-safety, ft-race |
| 2 | **R19** | 6140–6452 | 0.31 | **15** | 47.9 | 4 | MRO name lookup + method cache | ft-race, lock-discipline, refcount |
| 3 | **R4** | 524–810 | 0.29 | **10** | 34.8 | 3 | tp_dict/tp_bases/tp_mro/tp_subclasses accessors | null-safety, ft-race |
| 4 | R22 | 6849–7054 | 0.21 | 7 | 34.0 | 5 | Type dealloc + static-type finalization | refcount, uninit-dealloc |
| 5 | R36 | 12407–12533 | 0.13 | 4 | 31.5 | 1 | slot_inherited / add_operators / PyType_Freeze | refcount, error-path |
| 6 | **R6** | 971–1481 | 0.51 | **13** | 25.4 | 2 | Type cache, watchers, version tags | ft-race, lock-discipline |
| 7 | R14 | 3951–4150 | 0.20 | 5 | 25.0 | 1 | subtype_dict/setdict/getweakref/type_init | null-safety, init-bypass |
| 8 | **R21** | 6529–6848 | 0.32 | **8** | 25.0 | 4 | type getattro/setattro | ft-race, pyerr-clear |
| 9 | R3 | 228–522 | 0.29 | 7 | 23.7 | 0 | Managed static types | refcount, stw-safety |
| 10 | R12 | 3703–3834 | 0.13 | 3 | 22.7 | 2 | Base selection (find_best_base/solid_base) | null-safety |
| 11 | R25 | 7482–7846 | 0.36 | 7 | 19.2 | 2 | object richcompare + `__class__` assignment | stw-safety, memory-pattern |
| 12 | **R11** | 3217–3702 | 0.49 | **8** | 16.5 | 3 | MRO (C3 linearization) | null-safety, recursion-guard |
| 13 | **R34** | 11784–12272 | 0.49 | **7** | 14.3 | 4 | Slot-wiring engine (update_one_slot/fixup) | memory-pattern, refcount |
| 14 | **R7** | 1483–2341 | 0.86 | **12** | 14.0 | **10** | type getsets (`__name__`/`__bases__`/`__mro__`) | ft-race, refcount |
| 15 | R10 | 2889–3216 | 0.33 | 4 | 12.2 | 0 | IsSubtype + special-method lookup | refcount |
| 16 | **R29** | 9017–9691 | 0.68 | **8** | 11.9 | 3 | PyType_Ready pipeline | null-safety, error-path |
| 17 | **R16** | 4191–5135 | 0.94 | **10** | 10.6 | 7 | type_new subsystem | init-bypass, null-safety, error-path |
| 18 | **R17** | 5136–5832 | 0.70 | **7** | 10.0 | 4 | PyType_FromSpec/FromMetaclass/**FromSlots** | error-path, uninit-dealloc |
| 19 | R18 | 5834–6139 | 0.31 | 3 | 9.8 | 5 | PyType_Get* accessors, module-by-token | ft-race |
| 20 | R13 | 3836–3941 | 0.11 | 1 | 9.4 | 0 | **Deferred slot-update queue (FT)** | see §3 — NEW, not safe |
| 21 | **R32** | 10521–11533 | 1.01 | **8** | 7.9 | 8 | slot_* dispatchers | pyerr-clear, error-path, refcount |
| 22 | R9 | 2576–2886 | 0.31 | 2 | 6.4 | 1 | subtype traverse/clear/dealloc | refcount |
| 23 | R24 | 7338–7520 | 0.18 | 1 | 5.5 | 1 | `object.__new__` / `object.__init__` | init-bypass |
| 24 | R28 | 8601–9016 | 0.42 | 2 | 4.8 | 1 | inherit_special / inherit_slots | — |
| 25 | **R37** | 12534–13068 | 0.54 | **2** | 3.7 | 3 | **super** | init-bypass — see §4 |
| 26 | R26 | 7848–8406 | 0.56 | 2 | 3.6 | 1 | Pickle (`__reduce_ex__`) | — |
| 27 | R31 | 9812–10519 | 0.71 | 2 | 2.8 | 1 | Slot wrapper functions (wrap_*) | — |
| — | R5, R15, R23, R30, R33, R35 | — | — | **0** | 0.0 | 0–1 | docstrings, CalculateMetaclass, PyType_Type, subclass bookkeeping, **slotdefs[] table**, `__set_name__` | cold |

Small regions R20 (6453–6528, 76 lines, 7 crash-fixes → 92/KLOC) and R1 (includes) top the raw
density column but are **denominator artifacts** — 76 lines cannot host 92 bugs. They are listed
here rather than at rank 1 deliberately. R20 (`_PyType_SetFlags` / `_PyType_Validate` /
`_PyType_SetFlagsRecursive`) is nonetheless genuinely hot in absolute terms for its size: 7 of its
8 lifetime commits are crash-shaped, most recently `e7eaed56149a` 2026-05-23 "gh-148450:
`abc.register` needs to update `type_version` when `tp_flags` is changed".

### 1b. The actionable read

Three clusters carry the file's defect mass, and they are **not** the regions with the most raw
churn:

1. **The free-threading synchronization spine — R2 + R19 + R6 + R21 + R4.** 49 crash-shaped fixes
   in three years across 1.42 KLOC. Every one of these regions exists to make type objects safe
   under `--disable-gil`, and every one has been re-fixed repeatedly. A `ft-race-scanner`,
   `lock-discipline-checker`, or `stw-safety-checker` finding in these five regions should be
   treated as **probably real** — this exact code has been wrong before, repeatedly, within the
   last 24 months.
2. **The MRO / base-selection axis — R11 + R12 + R4.** 21 crash-shaped fixes / 3y. Notably
   `a8edca62fc6d` 2025-10-21 "gh-132835: **Add defensive NULL checks** to MRO resolution
   (GH-134763)" and `44605aa93d31` 2025-03-25 "gh-131711: **Preventing the use of a null pointer**
   in `set_tp_mro`". Two separate NULL-guard-retrofit commits in 7 months means the MRO plumbing's
   NULL invariants were never fully enumerated. A `null-safety-scanner` hit here is high-prior.
3. **`type_new` + `PyType_FromSpec` construction — R16 + R17.** 17 crash-shaped fixes / 3y, and
   `f160f16373f9` **2026-07-14 — eleven days before HEAD** — "gh-151912: Fix **segfault** in
   `type()` with **NULL `tp_new`** metaclasses". Same shape as `2b90796be695` 2023-06-12
   "gh-103968: PyType_FromMetaclass: Allow metaclasses with **`tp_new=NULL`**". The NULL-`tp_new`
   metaclass shape has now bitten this file **twice, three years apart, in two different regions** —
   it is a recurring, incompletely-propagated fix.

Function-level churn (from the script) agrees: `type_from_slots_or_spec` 109 commits (the file's
553-line giant), `managed_static_type_state_init` 69, `subtype_dealloc` 57,
`_PyType_Modified_Unlocked` 53, `type_call` 40, `mro_implementation_unlocked` 38, `pmerge` 36.

---

## 2. Recurring fix-keyword clusters

Deduped by hash across the whole file history. Year histograms from 2019 forward.

### **free-threading / data-race — 40 commits. STILL ACTIVE, the dominant modern cluster.**
`2022:1 · 2024:21 · 2025:13 · 2026:4`

Effectively a *new* cluster: 38 of 40 commits are 2024 or later. This is PEP 703 landing in the
type system, and it is still being debugged.

- `1ec6596828b0` 2026-06-11 R7 — gh-150858: fix data race while changing `__qualname__` of a type object
- `94bca40ff09c` 2026-05-13 R32/R16/R21 — gh-148906: fix performance scaling of descriptors on free-threading
- `29917d51ab41` 2026-04-23 R18 — gh-148907: fix performance regression in `PyType_GetModuleByDef` on free-threading
- `d891b2bbd16c` 2026-02-06 R32/R21 — gh-139103: Improve namedtuple scaling in free-threaded build
- `b83f379a972c` 2025-11-06 R7/R2 — **gh-133467: Fix typeobject `tp_base` race in free threading**
- `e99bc7fd44bb` 2025-08-01 R7 — gh-133467: fix data race in `type_set_name`
- `485b16b4f7b1` 2025-08-05 R32 — gh-137238: Fix data race in `_Py_slot_tp_getattr_hook`

Note `gh-133467` produced **two** fixes three months apart in the same region (R7) — `type_set_name`
then `tp_base`. The getset family (R7) was audited race-by-race, not systematically.
→ **ft-race-scanner, lock-discipline-checker, stw-safety-checker.**

### **refcount — 42 commits. Active, shifted to contention-avoidance.**
`2019:2 · 2020:1 · 2022:4 · 2023:5 · 2024:7 · 2025:2`

- `da53660f35db` 2025-04-21 R10 — gh-131586: Avoid refcount contention in context managers
- `67fbfb42bd5d` 2025-03-26 R32 — gh-131586: Avoid refcount contention in some "special" calls
- `3ea488aac448` 2024-10-15 R22 — gh-124218: Use per-thread refcounts for code objects
- `c9014374c50d` 2024-10-10 R22 — GH-125174: Make immortal objects more robust (PEP 683)
- `b482538523415` 2024-10-01 R22 — gh-124218: Refactor per-thread reference counting
- `a781484c8e98` 2023-05-12 R37 — **Fix refleak in `super_descr_get`** (see §4)

The 2024 spike is concentrated in R22 (type dealloc / static-type finalization) and is the
per-thread-refcount + immortality migration. → **refcount-auditor** should treat R22 and R32 as
high-prior; R22's mechanism changed four times in one year.

### **memory-leak — 29 commits. Mostly dormant; one live 2025 hit.**
`2019:1 · 2020:1 · 2025:1`

- `71db05a12d99` 2025-10-21 R32 — gh-140406: Fix memory leak upon `__hash__` returning a non-integer
- `1db76394ea79` 2020-11-21 R17 — bpo-42412: Fix possible leaks and check arguments in `PyType_FromModuleAndSpec()`
- `d5a551c2694e` 2019-02-25 R37 — bpo-36115: Fix some reference leaks in typeobject.c
- `484c913ed9fc` 2016-06-05 R16 — Issue #27225: reference leak in `type_new` when setting `__new__` fails
- `3d7497608ba8` 2016-04-13 R37 — Issue #26718: `super.__init__` no longer leaks memory if called multiple times

The 2025 hit is the *error path of a user-callable dunder* — same shape as the 2016 `type_new`
`__new__`-failure leak. → **error-path-analyzer** on dunder-failure paths in R32.

### **NULL-deref — 48 commits. STILL ACTIVE, and demonstrably under-propagated.**
`2020:2 · 2021:1 · 2022:1 · 2023:1 · 2024:1 · 2025:2 · 2026:1`

- `f160f16373f9` 2026-07-14 R16 — **gh-151912: Fix segfault in `type()` with NULL `tp_new` metaclasses**
- `a8edca62fc6d` 2025-10-21 R29/R11 — gh-132835: **Add defensive NULL checks** to MRO resolution
- `44605aa93d31` 2025-03-25 R4 — gh-131711: **Preventing the use of a null pointer** in `set_tp_mro`
- `7e6fcab20003` 2024-05-10 R24 — Fix some missing null checks
- `2b90796be695` 2023-06-12 R17 — gh-103968: PyType_FromMetaclass: Allow metaclasses with **`tp_new=NULL`**

Low per-year count but a **steady drip that never stops**, and two matched pairs
(`tp_new=NULL` in 2023 R17 → again 2026 R16; MRO NULLs in 2025 R4 → again 2025 R29/R11).
This is the single strongest signal in the file that NULL invariants here are enumerated
reactively, one crash report at a time. → **null-safety-scanner, init-bypass-checker.**

### **crash / segfault / abort — 84 commits.** `2022:4 · 2023:1 · 2024:2 · 2025:1 · 2026:3`
2026 already has three, matching the worst year on record:
- `f160f16373f9` 2026-07-14 R16 — segfault in `type()` with NULL `tp_new` metaclasses
- `1ec6596828b0` 2026-06-11 R7 — `__qualname__` data race
- `e7eaed56149a` 2026-05-23 R20 — `abc.register` / `type_version` vs `tp_flags`
- `b6c552f9e614` 2025-04-15 R4 — gh-132176: Fix crash on `type()` when `tuple` subclass passed as `bases`

### **assertion — 8 commits, 2 of them 2025, both in R2.**
`f6f456f95092` 2025-12-03 "gh-142038: Expand guard for `types_world_is_stopped()` to fix debug
builds without assertions"; `1e1f43519605` 2025-10-14 "gh-140126: Fix compile error if
`--with-assertions` is enabled". The FT world-stop assertions in R2 have been wrong twice in
two months. → **stw-safety-checker.**

### Cold clusters (mentioned only to say they are cold)
**recursion — 3 commits, newest 2015** (`f488fb422a64`, and two from 2001).
**overflow — 5, newest 2019** (`05f16416d99d` bpo-26423 `wrap_lenfunc`).
**uninitialized — 2, newest 2016.** **corruption — 2, newest 2024** (`69a4063ca516` gh-123339
`__module__`/`__firstlineno__` inconsistency).

The recursion cluster being cold is *itself* notable: `typeobject.c` has never had a recursion-guard
crash fixed, despite hosting `slot_tp_repr`/`slot_tp_hash`/`slot_tp_richcompare` (R32) and the
`pmerge`/`mro` descent (R11). Read that as **unexplored, not safe**.

---

## 3. Recently-touched-and-therefore-risky code

**57 commits in the last 12 months; 126 in 24 months.** The last year rewrote large parts of this
file. New code with no fix history is *under-tested*, not proven.

### 3.1 Zero-fix-history new code — the highest-value blind spots

| Code | Region | Landed | Age | Subsequent fixes |
|------|--------|--------|-----|------------------|
| **PEP 820 `PyType_FromSlots` / `pycore_slots.h` / `Python/slots.c`** | R17 (5802–5807), R1, R2, R18 | `508b49845d2` 2026-05-05 | **81 days** | **ZERO** |
| **Deferred slot-update queue (FT)** — `slot_update_new_chunk`, `queue_slot_update`, `apply_slot_updates`, `apply_type_slot_updates` | **R13 (3836–3941)** | `fbbbc10055e0` 2025-05-27 "gh-127266: avoid data races when updating type slots (gh-133177)" | 14 months | **ZERO** |

`git log -L 3836,3941:Objects/typeobject.c` returns **exactly one commit** — the one that created
the region. In 14 months, in the most race-fixed file in `Objects/`, during the most active
free-threading year on record, this 106-line lock-free chunk allocator has never been touched
again. Likewise `Python/slots.c`, `Python/slots_generated.c` and `Include/internal/pycore_slots.h`
have **one commit total each**, all `508b49845d2`.

Both sit directly inside the two hottest clusters (FT synchronization, slot wiring). Treat any
finding in R13 or in the `PyType_FromSlots` path as **elevated**, not deprioritized. Per the
include map, the `PySlot` machinery's only in-tree mirror is `Objects/moduleobject.c` +
`Python/slots.c` — the asymmetry check between the type path and the module path has never been
exercised by a bug report.

Also new and thin: `8923ca418ce` 2026-04-08 "_DuringGC functions for tp_traverse" (R18,
`PyType_GetModuleByToken_DuringGC` @5916) and `589a03a8ce6` 2025-11-05 "PEP 793 PyModExport"
(R18) — both under 9 months old with no follow-up fixes.

### 3.2 The revert-and-reland — the single riskiest live change in the file

A landed → deadlocked → patched → raced → **reverted** → relanded cycle completed **19 days
before HEAD**:

```
efb2fffae1f  2026-06-11  gh-150490: Raise PyType_Modified for insertion into split dictionary (#150489)
             ↓ caused
             gh-151593: dead lock in PyDict insert_split_key  (hit in CI)
             ↓ attempted fix
             gh-152200  — "avoids the deadlock but introduces a data-race.
                           The race window is small but can be triggered with pure Python code."
             ↓
9626ef87f42  2026-06-26  gh-152238: Revert gh-150490 and gh-152200. (gh-152232)
             ↓ relanded
20921922fa4  2026-07-06  gh-150490: Raise PyType_Modified for insertion into split dictionary (take 2) (#152914)
```

The reland touches **R6, R7, R11, R20, R21, R22, R36** — seven regions, including four of the top-8
by crash-fix density. The maintainers' own revert message documents a *deadlock* and a
*pure-Python-triggerable data race* in the first attempt. "Take 2" is 19 days old and has had zero
soak time. → **lock-discipline-checker and ft-race-scanner should treat every `PyType_Modified` /
`_PyType_Modified_Unlocked` call site touched by `20921922fa4` as unproven.**

### 3.3 Slot-wiring regression-and-repair (R34), still settling

- `e6e376a760b` 2025-10-03 — gh-132042: **Remove `resolve_slotdups()`** to speedup class creation
- `f1052655388` 2026-03-13 — GH-132042: **Fix calculation of slotdef index in `update_one_slot()`**
- `8e10bd77ea0` 2026-03-30 — gh-146587: fix type slot assignment in case of **multiple slots for same name**

The 2025-10 optimization broke slotdef index calculation; the fix landed 5 months later, and a
*second* multi-slot-name bug surfaced 17 days after that. R34 (`update_one_slot` /
`fixup_slot_dispatchers`) is mid-repair. Per the include map this engine has **no in-tree
structural twin** — its siblings are other rows of `slotdefs[]` (R33, which has **zero** crash-fix
history in 5 years and 75 lifetime commits). If a scanner flags a `slotdefs[]` row, there is no
historical precedent to lean on either way.

### 3.4 Other last-12-month error-path repairs worth cross-referencing
- `7928a8b730b` 2026-06-23 R17 — gh-151126: **Add missing `PyErr_NoMemory()`** in `type_from_slots_or_spec` → prior-raise for **error-path-analyzer / oom-reproducer** on the 553-line giant.
- `0ec7c9d17e0` 2026-06-15 R32 — gh-146102: Fix type `slot_bf_getbuffer()` error handling → this is the **gh-146102 success-path-`PyErr_Clear` class** the pyerr-clear-auditor already tracks, landing *in this file* 40 days ago.
- `c398490fbf1` 2026-04-04 R29 — gh-148074: Fix `typeobject.c` **missing error return**.
- `da8199f8842` 2025-12-11 R18 — gh-123241: **Don't modify ref count during visitation** (traverse).

---

## 4. `super` specifically — the guarded twin for CPY-0007

**CPY-0007:** `super.__new__(super).__get__(1)` → SIGSEGV, reproduced, unreported.
Region **R37 (12534–13068)**, `super_descr_get` @ **12781–12811**.

### 4.1 Region-level history

R37 is **cold by density** — rank 25 of 37, 2 crash-shaped fixes in 3 years, 3.74/KLOC, and only 3
commits in the last 12 months (all incidental: a docstring length fix, a JIT folding change, and a
TYPE_LOCK contention change). Its 118 lifetime commits are overwhelmingly pre-2010.

**This does not weaken the finding — it strengthens the "unreported" claim.** Nobody has been
looking at `super`'s NULL invariants recently. But `super` *has* had NULL-guard work, and it is the
right kind.

### 4.2 The guarded twins — three of them, in the same 250 lines

The bug is: an uninitialized `superobject` (from `super.__new__(super)`, which bypasses
`super.__init__`) has `su->type == NULL`, `su->obj == NULL`, `su->obj_type == NULL`.
**Three of the four consumers of that state guard it. `super_descr_get` does not.**

**Twin 1 — `super_repr` (12568–12581). The strongest twin: same field, same NULL, guarded.**
```c
    if (su->obj_type)
        return PyUnicode_FromFormat(
            "<super: <class '%s'>, <%s object>>",
            su->type ? su->type->tp_name : "NULL",     /* <-- guarded */
            su->obj_type->tp_name);
    else
        return PyUnicode_FromFormat(
            "<super: <class '%s'>, NULL>",
            su->type ? su->type->tp_name : "NULL");    /* <-- guarded */
```
Introduced by **`41eb14dffab5c5b559028f062d209b5cde8224f4`, 2001-08-30, Guido van Rossum** —
*"Give 'super' a decent repr(), and readonly attributes to access the type and obj properties.
The 'bogus super object' message is gone — this will now just raise an AttributeError."*
The diff adds the literal `su->type ? su->type->tp_name : "NULL"` lines. **The author of `super`
explicitly recognized, six days after writing it, that `su->type` can be NULL, and defended
against exactly that.** `super_repr` reads both fields defensively; `super_descr_get` reads
`su->type` bare.

**Twin 2 — `do_super_lookup` (12643–12645). The behavioral twin: same object, attribute access, guarded.**
```c
    if (su_obj_type == NULL) {
        goto skip;
    }
```
`super_getattro` (12688–12700) passes `su->type`, `su->obj`, `su->obj_type` straight through, and
`do_super_lookup` short-circuits on the uninitialized state. Introduced as
`int skip = su->obj_type == NULL;` by **`76ba09fd812`, 2003-04-16, Guido van Rossum**
("super() no longer ignores data descriptors, except `__class__`"), and preserved through every
rewrite since — including the 2023 `LOAD_SUPER_ATTR` specialization work
(`0dc8b50d33208e9`, `ef25febcf2e`, `77262458fe3`), which explicitly re-tested `su_obj_type == NULL`
rather than dropping the guard.

Consequence: `super.__new__(super).foo` raises `AttributeError` cleanly, while
`super.__new__(super).__get__(1)` segfaults. **Two adjacent slots on the same object, one guarded
since 2003, one never.**

**Twin 3 — `super_dealloc` (12556–12565).** Uses `Py_XDECREF` on all three of `su->obj`,
`su->type`, `su->obj_type` — the `X` is the acknowledgement that each may be NULL.

### 4.3 The unguarded path

```c
static PyObject *
super_descr_get(PyObject *self, PyObject *obj, PyObject *type)
{
    superobject *su = superobject_CAST(self);
    ...
    if (obj == NULL || obj == Py_None || su->obj != NULL) {   /* 12786: guards su->obj ONLY */
        return Py_NewRef(self);
    }
    if (!Py_IS_TYPE(su, &PySuper_Type))
        return PyObject_CallFunctionObjArgs((PyObject *)Py_TYPE(su),
                                            su->type, obj, NULL);   /* 12794: su->type unguarded */
    else {
        PyTypeObject *obj_type = supercheck(su->type, obj);          /* 12797: NULL propagates in */
        ...
        newobj->type = (PyTypeObject*)Py_NewRef(su->type);           /* 12806: Py_NewRef(NULL) */
```

Line **12786 checks `su->obj` and stops there** — it never checks `su->type`. The NULL then reaches
two unguarded dereference sites:
- **`supercheck` @ 12763** — `type->tp_name` in the `PyErr_Format` on the failure path, with
  `type == NULL`. (`supercheck` 12703–12766 has no NULL check on its `type` parameter; its two
  earlier uses, `PyType_IsSubtype(…, type)` @12721/12726, tolerate NULL by returning 0.)
- **`super_descr_get` @ 12806** — `Py_NewRef(su->type)` with `su->type == NULL`.

Which one fires depends on the `obj` argument; both are reachable and both are unguarded. The
repro agent should pin the exact frame — this analysis establishes the *shape*, not the line.

### 4.4 Why this materially strengthens the report

1. **Same file, same struct, same field, same NULL, three guarded siblings.** This is not a
   speculative invariant. `su->type == NULL` is a state CPython's own authors documented in code
   (2001) and in a second slot (2003) and have preserved through 25 years of rewrites.
2. **The guard author is the API author.** `41eb14dffab5` is Guido guarding `su->type` in `super`'s
   own repr. That makes "`su->type` may be NULL" a *stated* invariant of the type, not an inference.
3. **`super_descr_get` was audited for a different bug and the NULL was missed.**
   `a781484c8e98` 2023-05-12 "**Fix refleak in `super_descr_get`**" (#104408) touched exactly this
   function three years ago, for a refcount bug, and did not add the NULL check. So does
   `3d40317ed24d` 2025-02-25 "gh-111178: fix UBSan failures in `Objects/typeobject.c`", which
   touched both `super_repr` and `super_descr_get` — a UBSan pass over both functions that did not
   surface the missing guard (UBSan does not reach it without the `__new__`-bypass input).
4. **It is a textbook instance of the toolkit's known `init-bypass` class**, alongside
   `sqlite3.Connection.__new__` (gh-152954), `del cursor.row_factory` (gh-152817), gh-144330
   (classmethod/staticmethod) and `bytearray.__new__(bytearray).append(1)` — the last of which is
   likewise a live SIGSEGV on main.

**Recommended framing for the eventual report:** lead with `super_repr`'s
`su->type ? su->type->tp_name : "NULL"` (commit `41eb14dffab5`, 2001-08-30) and
`do_super_lookup`'s `if (su_obj_type == NULL) goto skip;` (commit `76ba09fd812`, 2003-04-16) as the
guarded twins, then show `super_descr_get:12786` checking `su->obj` while silently passing
`su->type` to `supercheck`. The fix is one line, and its shape is already written twice in the same
file.

---

## 5. Priority hand-off

Ranked by *prior-raising value* — how much a scanner hit in this region should be believed.

| Prior | Regions | Agent that should be believed there |
|-------|---------|-------------------------------------|
| **Highest** | R2 (42–227), R19 (6140–6452), R6 (971–1481), R21 (6529–6848), R4 (524–810) | **ft-race-scanner, lock-discipline-checker, stw-safety-checker** — 49 crash fixes/3y, cluster still active in 2026 |
| **Highest** | R6/R7/R11/R20/R21/R22/R36 lines touched by `20921922fa4` (2026-07-06) | **lock-discipline-checker, ft-race-scanner** — reverted once for deadlock + pure-Python-triggerable race; 19 days old |
| **High** | R11 (3217–3702), R12 (3703–3834), R4 (524–810) | **null-safety-scanner** — two NULL-guard retrofits in 7 months (`a8edca62fc6d`, `44605aa93d31`) |
| **High** | R16 (4191–5135), R17 (5136–5832) | **null-safety-scanner, init-bypass-checker, error-path-analyzer** — NULL-`tp_new` metaclass shape recurred 2023→2026; missing `PyErr_NoMemory` fixed 2026-06-23 |
| **High** | R22 (6849–7054), R32 (10521–11533) | **refcount-auditor, pyerr-clear-auditor** — per-thread-refcount migration churned R22 4× in 2024; gh-146102 class landed in R32 40 days ago |
| **High (new, untested)** | **R13 (3836–3941)**, `PyType_FromSlots` @5802 + `Python/slots.c` | **any agent** — ZERO fix commits since creation. Do not read silence as safety. |
| **Medium** | R34 (11784–12272) | **memory-pattern-analyzer, refcount-auditor** — mid-repair after the gh-132042 regression cycle |
| **Medium** | R29 (9017–9691) | **error-path-analyzer, null-safety-scanner** — `c398490fbf1` missing error return, 2026-04 |
| **Medium** | R32 (10521–11533), R11 (3217–3702) | **recursion-guard-auditor** — cluster is *cold* (newest fix 2015), i.e. unexplored, and these are the descent sites |
| **Targeted** | **R37 (12534–13068), `super_descr_get` 12781–12811** | **init-bypass-checker** — CPY-0007; guarded twins at `super_repr` 12572/12575/12580 (`41eb14dffab5`, 2001) and `do_super_lookup` 12643 (`76ba09fd812`, 2003) |
| **Cold** | R5, R15, R23, R30, R33, R35 | Zero crash-fix history in 5 years. Deprioritize — but note R33 (`slotdefs[]`) is cold because nobody audits tables, not because tables are safe. |
