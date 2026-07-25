# git-history-context — Objects/ sample (informed)

**Role:** preflight temporal orientation. I seed priority; I do not judge findings.
Everything below is a *prediction* for the Phase-2 safety agents, not a confirmed bug.

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (main, 2026-07-15, CPython 3.16.0a0)
**Window:** full history — 1990-08-09 → 2026-07-15 (no truncation; see Clone health)

---

## Scanner volume

`analyze_history.py` is not a candidate-emitting scanner, so the usual precision table does
not apply. Substituting the equivalent numbers:

```
commits in Objects/ over full history:      9,203   (script, --days 13000, cap not hit)
commits touching the 14-file sample:        1,984   (raw git log --follow, union)
classified "fix" by the script:             4,133   (45% of Objects/ — over-broad, see Toolkit assessment)
classified "bug fix" by my regex:             442   (over the 14 sample files, --follow)
crash/safety subset:                          185
fix-propagation leads extracted:                8   (5 with left-behind siblings, 2 complete, 1 clean)
```

**One hard blocker:** `analyze_history.py` **crashes** on any window longer than ~10 years on
this repo. Every number above that comes from the script required a monkey-patch. Details in
Toolkit assessment; this is the single most important output of this run.

---

## Clone health

- **Shallow clone: NO.** `git rev-parse --is-shallow-repository` → `false`; no `.git/shallow`
  file; no grafts.
- **132,320 commits** on `HEAD`, first commit `7f777ed95a1` **1990-08-09**. The history is
  complete and every temporal signal below is trustworthy.
- Scope: `Objects/` = **9,203 commits**, 168 files, `commit_cap_applied: false` at
  `--max-commits 40000`. Nothing was truncated by the cap.
- ⚠ **The toolkit did not verify this for me.** `analyze_history.py` contains no shallow-clone
  detection whatsoever (`grep -n shallow` → nothing, in any script). I ran the check by hand
  per my agent definition. See Toolkit assessment T-3.

---

## Findings

### FIX — (temporal agent emits no code findings; the equivalent is the watchlist)

### 1. Bug-fix-density watchlist for the 14 sample files

Ranked by **count of commits whose message indicates a bug fix** (`--follow`, so renames are
traced; this is why my totals exceed the script's path-scoped `file_churn`). `crash` = the
narrower crash/safety subset (crash/segfault/UAF/leak/refcount/race/overflow/NULL/assert/
corrupt/uninit). `fix24` = bug-fix commits since 2024-01-01 — **the recency-weighted signal,
which outranks lifetime totals per my agent definition.**

| # | file | fix | crash | total | fix24 | lines | fix24/KLOC | first → last |
|---|------|----:|------:|------:|------:|------:|-----------:|---|
| 1 | `Objects/tupleobject.c` | **96** | 37 | 329 | 13 | 1302 | 10.0 | 1990-10-14 → 2026-06-11 |
| 2 | `Objects/descrobject.c` | **60** | 30 | 255 | 8 | 2096 | 3.8 | 2001-08-02 → 2026-07-13 |
| 3 | `Objects/funcobject.c` | **57** | 34 | 266 | 12 | 1929 | 6.2 | 1990-10-14 → 2026-04-13 |
| 4 | `Objects/weakrefobject.c` | **53** | 24 | 166 | 7 | 1143 | 6.1 | 2001-02-01 → 2026-06-10 |
| 5 | `Objects/structseq.c` | **42** | 19 | 135 | 6 | 800 | 7.5 | 2001-10-18 → 2026-04-06 |
| 5 | `Objects/odictobject.c` | **42** | 17 | 126 | 12 | 2385 | 5.0 | 2015-05-29 → 2026-06-29 |
| 7 | `Objects/genericaliasobject.c` | **26** | 8 | 64 | 10 | 1070 | 9.3 | 2020-04-07 → 2026-07-08 |
| 8 | `Objects/iterobject.c` | 21 | 7 | 83 | 4 | 542 | 7.4 | 2001-04-20 → 2025-08-14 |
| 9 | `Objects/cellobject.c` | 18 | 6 | 57 | 3 | 212 | 14.2 | 2001-01-25 → 2024-12-03 |
| 9 | `Objects/unionobject.c` | 18 | 6 | 53 | 6 | 601 | 10.0 | 2020-09-09 → 2026-06-02 |
| 11 | `Objects/capsule.c` | 6 | 4 | 20 | 2 | 366 | 5.5 | 2009-05-05 → 2025-01-08 |
| 12 | `Objects/templateobject.c` | 2 | 1 | 6 | 2 | 416 | 4.8 | 2025-04-30 → 2026-06-02 |
| 13 | `Objects/interpolationobject.c` | 1 | 0 | 4 | 1 | 231 | 4.3 | 2025-04-30 → 2026-06-02 |
| 14 | `Objects/lazyimportobject.c` | 0 | 0 | 2 | 0 | 157 | 0 | 2026-02-12 → 2026-05-19 |

**Top exemplars per hot file** (most recent bug-fix commits):

- `tupleobject.c` — `244300162d2` gh-149807 fix `hash(frozendict)` pair hash (2026-05-20);
  `8614f86b716` gh-131525 cache `tuple_hash` result (2025-03-27);
  `44e4c479fbf` GH-124715 move trashcan into `Py_Dealloc` (2025-04-30);
  `388e1ca9f08` gh-115999 make list/tuple iteration more thread-safe (2025-02-18).
- `descrobject.c` — `701a7c5408f` gh-152405 don't expose internal mapping in `mappingproxy`
  richcompare (2026-07-13, **2 days before HEAD**); `72d29ea363f` gh-146615 fix crash in
  `__get__()` for METH_METHOD descriptors with invalid type arg (2026-03-31);
  `fa70bf85931` gh-132747 fix NULL deref calling a method's `__get__` manually (2025-04-21).
- `funcobject.c` — `ffa68529b40` gh-144330 initialize classmethod/staticmethod in `__new__`
  (2026-02-05); `e0f7c1097e1` gh-145779 classmethod/staticmethod FT scaling (2026-03-17);
  `55f17b77c30` gh-128714 fix function-object races in `__annotate__`/`__annotations__` (2025-02-06).
- `weakrefobject.c` — `ca32ebf7933` gh-80384 check callback callable at creation (2026-06-10);
  `a640a605a` gh-121652 handle `allocate_weakref` returning NULL (2024-07-13);
  `43fa76638` gh-118331 don't raise if tuple alloc fails when clearing weakrefs (2024-04-29).
- `odictobject.c` — `7d128e319f3` gh-148660 **UAF in `OrderedDict.copy()` on reentrant
  mutation** (2026-06-28); `b3b7c0025be` gh-146102 don't clear exception on success (2026-06-15);
  `6481539a6d9` gh-125996 thread-safety of `collections.OrderedDict` (2025-10-13).
- `genericaliasobject.c` — `68abf17fa92` gh-153298 **data race in `GenericAlias` parameter
  init on FT** (2026-07-08, **7 days before HEAD**); `f621ba16b72` gh-150146 NULL deref in
  `_Py_subs_parameters` (2026-05-20); `634568d030f` gh-148222 NULL deref bugs (2026-04-18);
  `8060aa5d7dd` gh-145376 various refleaks (2026-03-09); `bdba5f0db2a` gh-143635 crash in
  `ga_repr_items_list` (2026-01-15).
- `structseq.c` — `69f51625e63` gh-148119 refactor `get_type_attr_as_size` error handling
  (2026-04-06); `00a25859a94` gh-145376 fix GC tracking in `structseq.__replace__` (2026-03-13);
  `8060aa5d7dd` gh-145376 refleak fix (2026-03-09).

**Priority hand-off — which agent should look hardest where:**

| Rank | File | Dominant fix theme in history | Agent to route |
|---|---|---|---|
| **1** | `genericaliasobject.c` | 5 *distinct* error-path/NULL/refleak fixes in 2026 alone, plus an FT lazy-init race 7 days ago | **null-safety, error-path, ft-races** — highest live-cluster signal in the sample |
| **2** | `odictobject.c` | reentrancy → UAF (2015 and 2026), PyErr_Clear-on-success, FT | **refcount-auditor (borrowed-ref), pyerr-clear, ft-races** |
| **3** | `tupleobject.c` | recursion/trashcan (10), overflow (25), refcount (32), FT (13) | **recursion-guard (CPY-0001), memory-pattern, ft-races** |
| **4** | `descrobject.c` | `__get__` NULL crashes (2025 + 2026), lazy-init under FT | **null-safety, ft-races (TSAN-0043)** |
| **5** | `funcobject.c` | init-bypass (2026-02), FT versioning/caches, refcount (36) | **init-bypass, ft-races, refcount** |
| 6 | `weakrefobject.c` | callback validation + OOM-on-clear paths | **error-path, refcount** |
| 7 | `structseq.c` | refleak + GC-tracking + alloc-size, 3 fixes in 5 weeks in 2026 | **uninit-dealloc, error-path, memory-pattern** |
| 8 | `unionobject.c` | leak-on-failed-construction, `|` on unusual objects | **error-path, recursion-guard (gh-154318 sibling)** |
| 9 | `templateobject.c` | 2 fixes / 6 commits — 33% defect rate on brand-new code | **uninit-dealloc, null-safety** |
| 10 | `iterobject.c`, `cellobject.c`, `capsule.c` | dormant | low priority |
| 11 | `interpolationobject.c`, `lazyimportobject.c` | never fixed; never reviewed | **read-by-hand, all agents** |

---

### CONSIDER — 2. Recurring fix-keyword clusters

A cluster is a **prediction** that the shape recurs unfixed elsewhere. Counts are over the
14-file sample, full history.

#### C-1. Reentrant-mutation → use-after-free in `odictobject.c` — *11 years and still recurring* ★★★
The same shape has been fixed three times, a decade apart, always in a different method:
- `710cd34bd` (2015-11-04) Issue #25449 — "crash and leaking NULL in `repr()` of OrderedDict
  **that was mutated by direct calls of dict methods**"
- gh-119004 (2024) — `odict_richcompare` snapshots `od_state`
- `7d128e319f3` (2026-06-28) gh-148660 — `OrderedDict_copy_impl`; the commit message *itself*
  says "Detect this the same way `OrderedDict.__eq__` already does (gh-119004)"

**Guarded twin:** snapshot `od_state` before the loop, `Py_NewRef` the key, read the hash
before any reentrant call, `RuntimeError` if state changed (odictobject.c:1256/1274/1291 and
:824/847). **Status of propagation:** the two remaining `_odict_FOREACH` loops (`_odict_resize`
:578, `odict_traverse` :1482) do not run Python, so the FOREACH family looks complete. The
open question the refcount-auditor must answer is the *non-FOREACH* methods that iterate and
call Python — `odict_repr` (which has `Py_ReprEnter` but no `od_state` snapshot),
`mutablemapping_update`, `odict_popitem`, `odict_move_to_end`, `odict_setdefault`.
→ **refcount-auditor (borrowed-ref-across-call).**

#### C-2. `genericaliasobject.c` error paths — *four independent fixes in five months, still open* ★★★
| date | commit | what was missed |
|---|---|---|
| 2026-01-15 | `bdba5f0db2a` gh-143635 | crash in `ga_repr_items_list` |
| 2026-03-09 | `8060aa5d7dd` gh-145376 | refleak in `subs_tvars` + refleak in `_Py_subs_parameters` (`PySequence_Tuple` failure path) |
| 2026-04-18 | `634568d030f` gh-148222 | double-DECREF in `_Py_make_parameters` + unchecked vectorcall in `ga_vectorcall` |
| 2026-05-20 | `f621ba16b72` gh-150146 | **NULL deref in `_Py_subs_parameters`** — `_unpack_args()` result unchecked |

The March fix and the May fix are in the **same function**, two months apart. Nobody has swept
the file exhaustively. This is the single strongest "the next bug of this class is right here"
signal in the sample. Note also that `634568d030f` edited `_Py_make_parameters` at line 242 —
i.e. someone touched the exact function that **CPY-0002 / gh-154275** flags for a missing
`Py_EnterRecursiveCall` and did not add one.
→ **null-safety-scanner, error-path-analyzer.**

#### C-3. Free-threading lazy-init / data-race sweep — *active, 25+ commits since 2025, most recent 7 days before HEAD* ★★★
Tree-wide, `Objects/` has taken 25 "data race" fixes since 2025-01. The most recent is
`68abf17fa92` (2026-07-08) gh-153298 which wrapped `GenericAlias`'s lazy `parameters`
initialization in `Py_BEGIN_CRITICAL_SECTION`.
- Sample-local siblings: `1ec6596828b` gh-150858 `__qualname__` of a type object (2026-06-11),
  `6112d70abee` gh-151228 embedded dict values (2026-06-11), `55f17b77c30` gh-128714 function
  object `__annotate__`/`__annotations__` (2025-02-06), `19c1dd60c` gh-117323 `cell`
  thread-safety (2024-03-29), `df7317904` gh-111926 weakrefs (2024-04-08).
→ **ft-races agent.** See lead L-1 below — this cluster has a named left-behind sibling.

#### C-4. Recursion-guard asymmetry in `tupleobject.c` — *10 commits, all about repr/trashcan, none about hash* ★★
Every recursion commit in tupleobject.c's history guards **repr** or **dealloc**, never
**hash**: `aa975432d` (2008, "tuple's tp_repr did not take into account self-referential
tuple"), `1fb72d2ad` (2017, gh-32137 "repr of deeply nested dict now raises RecursionError"),
`351c67416` (2019, trashcan), `44e4c479fbf` (2025, trashcan into `Py_Dealloc`).
**Guarded twin:** `tuple_repr` at tupleobject.c:298/346/351 (`Py_ReprEnter`/`Py_ReprLeave`).
**Left behind:** `tuple_hash` (:372) and `tuple_richcompare` (:728) have neither guard.
Guard-call counts across the sample confirm the asymmetry is systemic:
`genericaliasobject.c` **0**, `unionobject.c` **0**, `funcobject.c` **0**, `structseq.c` **0**,
`iterobject.c` **0**, `templateobject.c` **0**, `weakrefobject.c` **0**; only
`odictobject.c` (1, in `odict_repr`) and `descrobject.c` (2, in the *call* path
`method_enter_call`, not in a hash/richcmp slot) have any.
→ **recursion-guard-auditor.** Confirms CPY-0001; predicts unionobject/genericalias siblings.

#### C-5. Refcount / leak — the largest cluster, 152 commits sample-wide ★★
`funcobject.c` 36, `tupleobject.c` 32, `descrobject.c` 20, `weakrefobject.c` 17,
`structseq.c` 15, `odictobject.c` 10, `cellobject.c` 8, `iterobject.c` 7,
`unionobject.c` 4, `genericaliasobject.c` 2. Mostly historical hygiene sweeps
(`Py_NewRef`/`Py_SETREF`/`Py_CLEAR` migrations), but the 2026 tail is real leak fixes:
`8060aa5d7dd` gh-145376 (Objects/-wide refleak sweep), `6710156bd27` gh-139988 (leak on failed
Union creation), `ffa68529b40` gh-144330.
→ **refcount-auditor**, weight toward `funcobject.c` and `structseq.c`.

#### C-6. Exception-state / `PyErr_Clear` — 76 commits, and one *open sweep* ★★
`b3b7c0025be` (2026-06-15) gh-146102 removed a `PyErr_Clear()` from
`mutablemapping_add_pairs` in odictobject.c with the rationale *"Calling PyErr_Clear() on
success can mask a pending exception. Replace it with an assertion."*
**gh-146102 has produced only two commits so far** (`b3b7c0025be` on odictobject.c and
`0ec7c9d17e0` on typeobject.c/stringlib/join.h). The sweep is unfinished by construction.
8 `PyErr_Clear()` calls remain in the sample: `iterobject.c` :78/:248/:404/:433,
`descrobject.c` :1910, `funcobject.c` :1362, `genericaliasobject.c` :637, `unionobject.c` :172.
→ **pyerr-clear-auditor** — and see Toolkit assessment R-1, this is why `scan_pyerr_clear`
returned 0.

#### C-7. Weakref callback re-entrancy — 73 commits, but historically the *fixed* class ★
`weakrefobject.c`'s callback machinery is the most-fixed thing in the file. The 2024 FT
rewrite (`df7317904` gh-111926, `e8752d7b8` gh-118789, `7b811d056` gh-128008) plus
`10cd00a9e` bpo-38395 (proxy ownership) plus `ca32ebf7933` gh-80384 (2026) means the obvious
holes are closed. Only 6 commits since 2025-01. **Treated as a *lower*-priority prediction**
than its raw rank-4 position suggests.

#### C-8. GC / traverse — 82 commits ★
`00a25859a94` gh-145376 (2026-03-13) fixed GC tracking in `structseq.__replace__`;
`f92360565` gh-124538 (2024-09-26) fixed a crash in `gc.get_referents` on an untracked
capsule; `0c2b509f9` bpo-39778 (2020) removed weakref-list traversal from OrderedDict's
`tp_traverse`. Recurring theme: **objects that are GC-tracked before they are fully populated,
or not tracked when they should be.** → **uninit-dealloc + memory-pattern.**

---

### 3. Recent activity (last ~18 months) — new code is under-reviewed code

Cut-off 2025-01-24. Sample-wide, **2025 is the peak fix year in 36 years of history (29
bug-fix commits) and 2026 is at 26 in seven months** — the sample is in an accelerating fix
regime, not a quiescent one.

| year | bug-fix commits | all commits |
|---|---:|---:|
| 2021 | 23 | 83 |
| 2022 | 19 | 88 |
| 2023 | 18 | 104 |
| 2024 | 21 | 82 |
| **2025** | **29** | 87 |
| **2026 (7 mo)** | **26** | 49 |

Per-file, last 18 months: `funcobject.c` 23, `tupleobject.c` 21, `odictobject.c` 15,
`genericaliasobject.c` 14, `descrobject.c` 12, `unionobject.c` 11, `structseq.c` 8,
`iterobject.c` 7, `weakrefobject.c` 6, `templateobject.c` 6, `interpolationobject.c` 4,
`lazyimportobject.c` 2, **`cellobject.c` 0, `capsule.c` 0**.

`cellobject.c` (last touched 2024-12-03) and `capsule.c` (last touched 2025-01-08) are the two
**dormant** files in the sample — deprioritize.

#### The three new files

| file | born | age at HEAD | commits | bug fixes | verdict |
|---|---|---|---|---|---|
| `templateobject.c` | `60202609a2c` **2025-04-30** (gh-132661, PEP 750 t-strings) | 14.5 months | 6 | **2** | 33% of all commits are crash fixes |
| `interpolationobject.c` | `60202609a2c` **2025-04-30** (same commit) | 14.5 months | 4 | **0** | never had a bug fix |
| `lazyimportobject.c` | `46d5106cfa9` **2026-02-12** (gh-142349, PEP 810 lazy imports) | **5.1 months** | **2** | **0** | newest code in `Objects/`; never fixed, never reviewed |

- `templateobject.c` **has had fix commits**, and both were real crashes in a 416-line file:
  `fc7f4c36664` (2025-05-17, gh-134119) added a missing NULL check after `PyIter_Next` in
  `templateiter_next` — 17 days after the file landed; and `785268fdceb` (2025-12-10,
  gh-142433) moved a `Py_DECREF(laststring)` to *after* the `PyTuple_SET_ITEM` that replaces
  it in `template_new`, a decref-before-replace ordering bug. **Two crash bugs in the first
  seven months of a brand-new 416-line file** is the defect density Phase 2 should expect
  from `interpolationobject.c` and `lazyimportobject.c` too.
- `interpolationobject.c` and `lazyimportobject.c` have had **zero** bug-fix commits. That is
  not evidence of correctness — it is evidence of **zero exposure**. `lazyimportobject.c` in
  particular has been in the tree for five months and its only non-birth commit
  (`de9c32fc34f`) is an error-string wording change. Every agent should read these two files
  by hand rather than trusting scanner output on them.
- Note `50fe49c879a` (2026-06-02) touches all three but is a docs-only PEP-585 sweep — it
  inflates the raw churn of the new files and should be discounted.

---

### 4. Fix-propagation leads (the highest-value section)

For each: a commit that fixed **one** site of a catalog shape, and whether the siblings went
with it. **Left-behind = a direct Phase-2 lead.**

#### L-1 ★★★ FT lazy-init race — `ga_parameters` fixed 7 days before HEAD, `descr_get_qualname` left behind
- **Fix:** `68abf17fa92` (2026-07-08) gh-153298 — `ga_parameters` was
  `if (alias->parameters == NULL) { alias->parameters = ...; }` read/write with no lock. The
  fix split it into `ga_parameters_lock_held()` + a `Py_BEGIN_CRITICAL_SECTION(self)` wrapper.
- **Guarded twin:** `Objects/genericaliasobject.c` `ga_parameters` at HEAD.
- **LEFT BEHIND:** `Objects/descrobject.c` `descr_get_qualname`:
  ```c
  descr_get_qualname(PyObject *self, void *Py_UNUSED(ignored))
  {
      PyDescrObject *descr = (PyDescrObject *)self;
      if (descr->d_qualname == NULL)
          descr->d_qualname = calculate_qualname(descr);
      return Py_XNewRef(descr->d_qualname);
  }
  ```
  Structurally **identical** to the pre-fix `ga_parameters` — unsynchronized lazy init of a
  `PyObject*` field, reachable from four separate `PyGetSetDef` tables plus
  `wrapper_qualname`. This is **TSAN-0043**, and it now has a seven-day-old guarded twin
  in the same directory. → **ft-races agent, top priority.**

#### L-2 ★★★ `_Py_subs_parameters` — fixed twice, two months apart, same function
- **Fixes:** `8060aa5d7dd` (2026-03-09) added `Py_DECREF(item)` on the `PySequence_Tuple`
  failure path; `f621ba16b72` (2026-05-20) added the missing `if (item == NULL) return NULL;`
  after `_unpack_args(item)`.
- **Sibling status: LEFT BEHIND, demonstrably.** The March commit was a deliberate
  *"Fix various refleaks in Objects/"* sweep that touched this exact function and still missed
  the NULL deref two lines away. A sweep of one error class does not sweep the other.
- **Lead:** every remaining fallible call in `_Py_subs_parameters`, `subs_tvars`,
  `_Py_make_parameters`, and `ga_getitem` deserves a line-by-line read. Also check the *other*
  `_unpack_args()` call sites. → **null-safety-scanner, error-path-analyzer.**

#### L-3 ★★★ Reentrancy guard — `OrderedDict.copy()` fixed, non-FOREACH methods untouched
- **Fix:** `7d128e319f3` (2026-06-28) gh-148660 (backported to 3.13/3.14/3.15 —
  `9dc1ae09183`, `6f8628842fc`, `404113bd376`).
- **Guarded twin:** explicitly named in the commit message — `odict_richcompare` (gh-119004),
  odictobject.c:824/847.
- **Sibling status:** complete *within* the `_odict_FOREACH` family (the other two loops don't
  run Python). **Untested elsewhere:** `odict_repr` guards recursion (`Py_ReprEnter` :1448)
  but does **not** snapshot `od_state`, and it was the site of the *2015* version of this
  exact bug (`710cd34bd`, Issue #25449). → **refcount-auditor.**

#### L-4 ★★ Init-bypass — `classmethod`/`staticmethod` fixed, rest of the sample unchecked
- **Fix:** `ffa68529b40` (2026-02-05) gh-144330 "Initialize classmethod and staticmethod in
  new" — added `cm_new`/`sm_new` doing `PyType_GenericAlloc` + `cm_set_callable`, and deleted
  the `cm_callable == NULL → RuntimeError("uninitialized classmethod object")` band-aid from
  `cm_descr_get`. Also touched `Objects/object.c`.
- **Guarded twin:** `funcobject.c` `cm_new`/`sm_new` at HEAD.
- **Sibling status: UNKNOWN — nobody swept.** The band-aid pattern it removed
  (`if (field == NULL) { PyErr_SetString(RuntimeError, "uninitialized ... object"); }`) is the
  fingerprint of the same hole elsewhere. → **init-bypass agent** — and note that
  `scan_init_bypass.py` returns **0** across all of `Objects/` while this class was live in
  `funcobject.c` five months before HEAD (Toolkit assessment R-2).

#### L-5 ★★ `PyErr_Clear()`-on-success — an open sweep with 2 of N commits done
- **Fix:** `b3b7c0025be` (2026-06-15) gh-146102 on `odictobject.c`; `0ec7c9d17e0` same day on
  `typeobject.c` + `stringlib/join.h`.
- **Guarded twin:** `assert(!PyErr_Occurred());` at function entry, in place of a defensive
  `PyErr_Clear()`.
- **LEFT BEHIND: 8 `PyErr_Clear()` sites in the sample** (listed in C-6), none of which the
  sweep has reached. → **pyerr-clear-auditor**, with the scope widened past dealloc/clear/
  finalize.

#### L-6 ★★ `tuple_hash` copy-paste — the copy was *formalized* two months before HEAD, still unguarded
- `244300162d2` (2026-05-20) gh-149807 fixed `hash(frozendict)` and **added this comment above
  `tuple_hash`**: `"If you update this code, update also frozendict_pair_hash() which copied
  this code."` `frozendict_pair_hash`/`frozendict_hash` live at `Objects/dictobject.c`:8415/8447.
- Neither copy has a recursion guard. The briefing's `gh-154318` names exactly this pair
  (`tuple_hash` + `frozendict_hash`). The maintainers documented the copy-paste link two months
  ago and propagated the *hash-value* fix without propagating a guard.
- → **recursion-guard-auditor.** Confirms CPY-0001; `dictobject.c` is out of sample but the
  sibling is now explicitly documented in-tree.

#### L-7 ★ `descrobject.c` `__get__` NULL crashes — two fixes a year apart
- `fa70bf85931` (2025-04-21) gh-132747 "Fix NULL dereference when calling a method's `__get__`
  manually"; `72d29ea363f` (2026-03-31) gh-146615 "Fix crash in `__get__()` for METH_METHOD
  descriptors with invalid type argument". Two different descriptor `tp_descr_get`
  implementations, same class of hole, twelve months apart, no sweep of the remaining
  descriptor types in the file. → **null-safety-scanner.**

#### L-8 — COMPLETE (negative result, do not re-chase)
- `a640a605a` (2024-07-13) gh-121652 added NULL checks after **both** `allocate_weakref` call
  sites (weakrefobject.c:440 and :451). Verified at HEAD: `allocate_weakref` is defined at
  :400 and called at exactly :440 and :451, both guarded. **Fix propagation complete.**
- `fc7f4c36664` (2025-05-17) gh-134119 guarded the first `PyIter_Next` in `templateiter_next`.
  I checked the two remaining `PyIter_Next` results in that function: one flows through
  `Py_SETREF(item, ...)` (NULL-safe) and one is returned directly (correct StopIteration
  propagation). **No left-behind sibling here** — do not re-flag.

---

### 5. Co-change coupling — "when you fix X, check Y"

From FIX-classified commits since 2020, within the sample:

| pairs | co-fixes |
|---|---|
| `genericaliasobject.c` ↔ `unionobject.c` | **5** |
| `funcobject.c` ↔ `unionobject.c` | 3 |
| `genericaliasobject.c` ↔ `tupleobject.c` | 2 |
| `funcobject.c` ↔ `odictobject.c` | 2 |
| `odictobject.c` ↔ `unionobject.c` | 2 |
| `genericaliasobject.c` ↔ `iterobject.c` | 2 |

**`genericaliasobject.c` ↔ `unionobject.c` is the strongest in-sample coupling.** They share
the parameter-substitution machinery (`_Py_subs_parameters` is called from both). Any finding
in one should be checked against the other — and `unionobject.c` has **zero** recursion guards
while sitting downstream of the same `_Py_make_parameters` walk that CPY-0002 flags.

Outside the sample, the most-coupled fix partners are `typeobject.c` (with `funcobject.c` 5,
`unionobject.c` 4, `descrobject.c` 3), `dictobject.c` (with `odictobject.c` 4, `tupleobject.c` 4)
and `object.c` (with `genericaliasobject.c` 4, `structseq.c` 3).

---

### POLICY / ACCEPTABLE

- The 2022-era `Py_NewRef`/`Py_SETREF`/`Py_CLEAR` migration commits (`3a1dde8f2`, `8211cf5d2`,
  `135ec7cef`, `c0feb9918`, `584e55bd3`) dominate the raw refcount cluster counts but are
  mechanical hygiene, not bug fixes. Discount them.
- The pre-2009 SVN mega-merges (`0e3f591ae`, `477c8d5e7`, `00ee7baf4`, `be2c0a9fe`) match
  every keyword and carry no per-file signal. Discount them.
- `50fe49c879a` (PEP-585 docs sweep) and the `gh-111178` signature-fixing series inflate 2025
  churn on nearly every sample file with zero safety content.

---

## New siblings of known shapes

Summarized from §4. Ranked by confidence that the sibling is real and unfixed at HEAD:

1. **`descr_get_qualname`** (descrobject.c) — FT unsynchronized lazy init. Guarded twin
   `ga_parameters` fixed `68abf17fa92` **7 days** before HEAD. = TSAN-0043.
2. **`_Py_subs_parameters` / `subs_tvars` / `ga_getitem`** (genericaliasobject.c) — remaining
   unchecked fallible calls. Two fixes in this function in 2026 already.
3. **`odict_repr` + `mutablemapping_update` + `odict_popitem`** (odictobject.c) — reentrancy
   without an `od_state` snapshot. Guarded twins: `odict_richcompare`, `OrderedDict_copy_impl`.
4. **`tuple_hash` (:372) and `tuple_richcompare` (:728)** — no recursion guard while
   `tuple_repr` (:298) has one. = CPY-0001. Plus the newly-documented `frozendict_pair_hash`
   copy at dictobject.c:8415.
5. **The 8 remaining `PyErr_Clear()` sites** — gh-146102's sweep reached 2 files.
6. **`_Py_make_parameters`** — no `Py_EnterRecursiveCall`, and was edited (not guarded) by
   `634568d030f` three months before HEAD. = CPY-0002.
7. **Any remaining `"uninitialized ... object"` RuntimeError band-aid** — the fingerprint
   gh-144330 removed from `cm_descr_get`.

## Classes bounded (clean negatives)

- **Shallow-clone truncation: BOUNDED CLEAN.** Non-shallow, 132,320 commits, back to 1990-08-09,
  no grafts. Every temporal claim in this report rests on complete history.
- **`allocate_weakref` NULL handling: BOUNDED CLEAN.** Both call sites guarded since
  `a640a605a`; verified at HEAD.
- **`templateiter_next` NULL handling: BOUNDED CLEAN.** All three `PyIter_Next` results are
  correctly handled at HEAD after `fc7f4c36664`.
- **`_odict_FOREACH` family reentrancy: BOUNDED CLEAN.** The two loops outside
  `OrderedDict_copy_impl` (`_odict_resize`:578, `odict_traverse`:1482) cannot run Python.
- **`cellobject.c` and `capsule.c`: temporally cold.** Zero commits in 18 months, 6 and 4
  crash-fixes lifetime respectively. Not a correctness claim — a priority claim.
- **Catalog shapes are LIVE at HEAD.** `gh-149146` (tuple_dealloc), `gh-151815` (template_iter),
  `gh-152083` (context_tp_dealloc) and `gh-148382` (_decimal) have **no fix commit anywhere in
  the repo**, despite being numerically below issues that *do* have merged fixes
  (`gh-151403`, `gh-151126`, `gh-148660`). They are genuinely unfixed, not already-fixed
  history. (`gh-154318`, `gh-154275`, `gh-154527` are numbered above HEAD's newest referenced
  issue `gh-153298`, so their absence is expected and proves nothing either way.)

---

## Toolkit assessment  ← REQUIRED

### Precision

Not applicable in the usual sense — but the commit classifier is badly over-broad:

- **P-1 (high).** `CLASSIFICATION_RULES` puts `"fix"` first and its keyword list includes
  `"error"`, `"issue"`, `"null"`, `"correct"`, `"patch"`. First-match-wins over an ordered list
  means **4,133 of 9,203 `Objects/` commits (45%) land in the `fix` bucket**, including
  `"gh-111178: Fix function signatures in structseq.c"` (a docs/typing cleanup),
  `"gh-99300: Use Py_NewRef() in Objects/"` (a mechanical sweep), and every commit whose body
  happens to say "error". A 45% fix rate is not a signal; it is noise. My hand-rolled regex
  over the same 14 files gives 442/1984 ≈ 22%, and the crash/safety subset is 185 ≈ 9%.
- **P-2 (medium).** The `"chore"` rule contains `"update"` and `"merge"`, and the `"feature"`
  rule contains `"add"`/`"support"`. Because `fix` is checked first, a commit titled
  *"gh-121652: Handle `allocate_weakref` returning NULL"* is classified `fix` (correctly, but
  via the keyword `null`), while *"Add a NULL check"* would also be `fix`. The buckets do not
  separate.

### Recall gaps — bugs/shapes I found by reading that the script did not surface

- **R-1 ★★★ `scan_pyerr_clear` returns 0 on all of `Objects/` because it is scoped to
  dealloc/clear/finalize only.** RUN_CONTEXT asks whether that zero is an upstream fix or a
  recall gap. **History answers it: it is a scoping gap.** Six weeks before HEAD, `b3b7c0025be`
  (gh-146102) removed a `PyErr_Clear()` from `mutablemapping_add_pairs` — an ordinary
  *success* path, not a destructor — with the rationale "Calling PyErr_Clear() on success can
  mask a pending exception". The `data/cpython_non_bugs.md` taxonomy explicitly justifies the
  narrow scoping ("`PyErr_Clear()` after a sentinel-returning lookup is idiomatic … *outside*
  the destructor family"), but gh-146102 is CPython upstream saying that framing is too
  generous. 8 unswept sites remain in the sample. The scanner will never see them.
- **R-2 ★★ `scan_init_bypass` returns 0 on all of `Objects/`** while `ffa68529b40`
  (gh-144330, 2026-02-05) fixed exactly that class in `Objects/funcobject.c` five months before
  HEAD. The pre-fix shape was `tp_init`-only construction with a
  `RuntimeError("uninitialized classmethod object")` guard in `tp_descr_get`. Whatever
  `scan_init_bypass.py` looks for, it does not look for *that*.
- **R-3 ★★ There is no per-file bug-fix-density output.** `analyze_history.py` emits
  `file_churn` ranked by **raw commit count**, which is the metric my own agent definition
  tells me *not* to use ("Recency matters more than raw churn"; the deliverable is "density of
  crash/safety fix commits"). The rankings genuinely differ: by raw churn
  `genericaliasobject.c` is rank 36 of 168 in `Objects/`; by 2024+ bug-fix density per KLOC it
  is rank 1 in the sample and carries the most active unfinished cluster in the whole run. I
  had to write ~150 lines of raw `git log` + regex to produce the watchlist this agent exists
  to produce.
- **R-4 ★ `file_churn` does not use `--follow`.** Sample totals differ by up to 15%
  (`tupleobject.c`: 287 script vs 329 with `--follow`). For `Objects/` this is mostly the
  1990s `stringobject.c`-era renames, but for any scope with a renamed file the churn is
  silently understated. `stringobject.c` appears at rank 12 with `churn_rate: 0.0` because the
  file no longer exists on disk — a dead entry polluting the top-20.
- **R-5 ★ No cluster/theme extraction at all.** `co_change_clusters` is file-pair coupling, not
  keyword clustering. The agent definition asks for "recurring fix-keyword clusters … with
  commit counts and a year-by-year histogram"; nothing in the script produces either. Both the
  clusters (C-1…C-8) and the histogram in §3 are hand-built.
- **R-6 ★ No fix-propagation primitive.** Deliverable §4 — "did the sibling get fixed in the
  same commit?" — required manually reading eight commit diffs. The script hands back
  `recent_fixes[].diff` (truncated at 150 lines) but nothing correlates a fix to unfixed
  structural siblings.

### Prompt issues

- **PR-1.** The agent definition says "Focus the watchlist on `Modules/`, `Objects/`, and
  `Python/`" and "Output the top ~20 as a table". This run's scope is a **14-file sample**, so
  "top 20" is degenerate. The prompt needs a scope-aware phrasing ("top 20 or the whole scope,
  whichever is smaller").
- **PR-2.** The definition says *"For a long window, pass a generous `--max-commits` so a
  `--days`-style window isn't silently truncated."* That is the wrong worry on CPython: the
  cap was never hit (`commit_cap_applied: false` at 40000), and the real failure was a hard
  crash (T-1). The guidance should name `--days` explicitly with a CPython-scale value and warn
  about the crash.
- **PR-3.** The definition's Step 0 tells me to run `git rev-parse --is-shallow-repository`
  by hand. Correct instruction, but it should be in the *script*, not the prompt — a prompt
  can be skipped, a script cannot (see T-3).

### Concrete tuning proposals — ranked by value

**T-1 ★★★★★ (blocker) — `analyze_history.py` crashes on CPython history longer than ~10 years.**
```
{"error": "'utf-8' codec can't decode byte 0xf3 in position 403: invalid continuation byte",
 "type": "UnicodeDecodeError"}
```
Reproduces at `--days 5475` and every longer window; `--days 3650` is fine. Root cause,
`analyze_history.py:206-210`:
```python
def _run_git(args, cwd, timeout=_GIT_TIMEOUT):
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True,   # <-- no errors=
        cwd=str(cwd), timeout=timeout,
    )
```
The failing call is `_fetch_one_diff` → `git show --patch <hash> -- Objects/` on a **single**
commit: `42bb126f0aa1de4817babb8c56b9ad6df8bab458` (2015-12-18, *"Issue #25899: Converted
Objects/listsort.txt to UTF-8"*), whose diff necessarily contains the pre-conversion Latin-1
bytes (`Alejandro L\xf3pez-Ortiz`). One commit in 35 years takes down the entire analysis, and
the top-level `except Exception` in `main()` converts it into a bare error blob with no
traceback and no partial results. **Fix — two lines:**
```python
def _run_git(args, cwd, timeout=_GIT_TIMEOUT):
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, errors="replace",
        cwd=str(cwd), timeout=timeout,
    )

def _run_git_streaming(args, cwd):
    return subprocess.Popen(
        ["git"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", cwd=str(cwd),
    )
```
Verified: with this patch the full-history run completes in **11 seconds** (9,203 commits,
`--no-function`). Add a regression test that shells `git show 42bb126f0aa` if a CPython
checkout is available, or a unit test feeding `_run_git` a `bytes` stream with a `0xf3`.

**T-2 ★★★★ — add per-file bug-fix density to the JSON envelope.** New key alongside
`file_churn`:
```python
CRASH_FIX_KEYWORDS = frozenset({
    "crash", "segfault", "sigsegv", "use-after-free", "use after free", "uaf",
    "double free", "double-free", "dangling", "refleak", "leak", "refcount",
    "reference count", "data race", "race", "tsan", "deadlock", "hang",
    "overflow", "underflow", "out-of-bounds", "buffer", "uninitialized",
    "assertion", "corrupt", "undefined behavior", "regression",
})
# per file: {"crash_fix_commits": N, "fix_commits": N,
#            "crash_fix_commits_24mo": N, "top_crash_fixes": [ {hash, date, subject}, ... ]}
```
This is the deliverable this agent exists to produce and it currently has to be hand-rolled.
Rank `file_churn` by `crash_fix_commits_24mo` when a new `--rank-by density` flag is passed.

**T-3 ★★★★ — put the shallow-clone guard in the script.** No script in the toolkit calls
`git rev-parse --is-shallow-repository`. Add to `analyze()`:
```python
shallow = _run_git(["rev-parse", "--is-shallow-repository"], project_root)
result["is_shallow_clone"] = shallow.stdout.strip() == "true"
result["total_repo_commits"] = int(_run_git(["rev-list", "--count", "HEAD"], project_root).stdout or 0)
result["first_commit_date"] = _run_git(["log", "--reverse", "--format=%aI", "-1"], project_root).stdout.strip()
if result["is_shallow_clone"]:
    result["warnings"] = ["Shallow clone: history is truncated; run `git fetch --unshallow`."]
```
`rust-ext-review-toolkit` v0.2 fix #3 already does this; port it forward. Today a shallow clone
produces confident-looking, silently wrong output.

**T-4 ★★★ — raise the `--days` default and document a CPython-scale value.** The default is
**90 days**, which on this sample yields ~5 commits and no signal at all. Either raise the
default to 730, or make `--days 0` mean "all history". Also update the docstring: the current
`Usage:` block does not mention that `--days` is the only way to get a long window (`--since`
works but is undocumented as the CPython-scale lever).

**T-5 ★★★ — widen `scan_pyerr_clear.py` past the destructor family, behind a flag.** Add a
finding `type: "pyerr_clear_on_success_path"` for a `PyErr_Clear()` that is **not** dominated
by a failure branch (no preceding `if (x == NULL)` / `< 0` / `PyErr_Occurred()` in the same
block). Seed `data/cpython_non_bugs.md` with the counter-example so triage stays calibrated:
> *gh-146102 (2026-06-15): CPython upstream now treats a `PyErr_Clear()` on a success path as
> a bug in its own right — "calling PyErr_Clear() on success can mask a pending exception" —
> and replaces it with `assert(!PyErr_Occurred())`. The destructor-only scoping is too narrow.*
Expected yield on this sample alone: 8 candidates (C-6).

**T-6 ★★★ — add `--follow` to per-file churn.** One flag on the `git log` invocation in
`parse_git_log`'s caller. Also drop `file_churn` entries whose path no longer exists on disk
(`churn_rate == 0.0` with `line_count == 0`) or mark them `"deleted": true` — `stringobject.c`
currently sits at rank 12 of the `Objects/` top-20 as a ghost.

**T-7 ★★ — narrow the `fix` classification and add a `crash_fix` tier.** Remove `"error"`,
`"issue"`, `"correct"`, `"patch"` from the `fix` keyword list (they fire on refactors and
docs), and add a distinct `crash_fix` category checked *before* `fix`, using
`CRASH_FIX_KEYWORDS` from T-2. Target: `fix` share of `Objects/` drops from 45% to ~20%.

**T-8 ★★ — emit a year-by-year fix histogram and keyword clusters.** Two new envelope keys:
```python
"fix_histogram": {"2024": {"fix": 21, "crash_fix": 9}, "2025": {...}, ...},
"keyword_clusters": [{"cluster": "recursion-guard", "commits": 10, "files": [...],
                      "exemplars": [...], "years": {...}, "still_active": True}, ...]
```
Cluster patterns should be a `data/cpython_fix_clusters.json` table, not hard-coded — the eight
clusters in §2 of this report are a ready-made seed.

**T-9 ★★ — teach `scan_init_bypass.py` the gh-144330 fingerprint.** Add a rule: a type with a
`tp_init` that sets a required field and **no** `tp_new`, whose slots contain a
`PyErr_SetString(PyExc_RuntimeError, "uninitialized ...")` guard, is an init-bypass candidate.
That literal string is the marker CPython uses for the band-aid. Zero hits across `Objects/`
today; gh-144330 proves the class was live in `funcobject.c` in 2026-02.

**T-10 ★ — `CPYTHON_MODULE_FAMILIES` is missing the typing/generics family.** Add:
```python
"typing_generics": ["Objects/genericaliasobject.c", "Objects/unionobject.c",
                    "Objects/typevarobject.c"],
"callables": ["Objects/funcobject.c", "Objects/classobject.c", "Objects/descrobject.c"],
"pep750": ["Objects/templateobject.c", "Objects/interpolationobject.c"],
```
`genericaliasobject.c ↔ unionobject.c` is the strongest fix co-change pair in the sample (5)
and the family table would have surfaced it for free. `core_objects` also omits
`Objects/odictobject.c` despite a co-change of 4 with `dictobject.c`.

**T-11 ★ — surface a `new_files` section.** Files whose first commit is inside the window,
with age and fix count. `lazyimportobject.c` (5.1 months old, 157 lines, 0 fixes) and
`interpolationobject.c` (14.5 months, 0 fixes) are exactly the "new code is under-reviewed
code" signal downstream agents want, and the script has all the data (`first_commit_in_range`)
but never distinguishes "first commit in range" from "file was born here".

**T-12 ★ — `_SCRIPT_TIMEOUT = 300` is a silent-truncation hazard.** It is checked inside the
diff/hunk loops and, on hit, quietly breaks out leaving `recent_fixes` partial with **no
marker in the output**. It didn't fire on this run (11 s), but on `Python/` or a full-tree
scope it will. Set `result["timeout_hit"] = True` when `_check_script_timeout()` short-circuits
any loop.
