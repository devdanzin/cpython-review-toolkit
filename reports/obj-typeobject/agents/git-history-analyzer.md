# Git History Analysis — slice `obj-typeobject` (Phase 2, Group E)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0).
**Scope of this agent:** fix-completeness and similar-bug detection **across the whole tree**, using the
13 confirmed agent reports in `agents/` and `preflight/history_context.md` as raw material.

**Clone health.** `git rev-parse --is-shallow-repository` → `false`. 132,320 commits back to
`85a5fbbdfea` 1990-10-14. No shallow clone, no commit cap, no script timeout. Every temporal claim
below is sound. Method: `git log -L <range>:<file>`, `git log -S`, `gh api -X GET search/issues` and
`gh api repos/python/cpython/{pulls,issues}/…`.

---

## (a) UNFIXED SIBLINGS — ranked by reachability

### A1. [FIX, NET-NEW, REPRODUCED] `object.__dir__` is a second, strictly more reachable entry into the unguarded `merge_class_dict` recursion — `Objects/typeobject.c:8526`

The recursion agent found `merge_class_dict:7117` via `type.__dir__` (`type___dir___impl:7147`),
which needs a **metaclass** exposing a `__bases__` data descriptor. `merge_class_dict` has a
**second caller**:

```c
Objects/typeobject.c:8521    if (PyObject_GetOptionalAttr(self, &_Py_ID(__class__), &itsclass) < 0) {
                     8522        goto error;
                     8523    }
                     8526    if (itsclass != NULL && merge_class_dict(dict, itsclass) < 0)
```

That is `object___dir___impl`. `itsclass` is whatever ordinary attribute lookup returns for
`__class__` — a plain `@property` on any class. No metaclass, no `type.__dir__`, no C API:

```python
class Fake: pass
a = Fake(); a.__bases__ = (a,)          # 1-node cycle
class C:
    @property
    def __class__(self): return a       # object.__dir__ reads self.__class__
dir(C())
```

| build | result |
|---|---|
| `~/projects/python_build_matrix/builds/debug-gil-nojit/python` | **SIGSEGV, exit 139** |
| `.../release-gil-nojit/python` | **SIGSEGV, exit 139** |

Reproducer: `scratchpad/objdir_bases.py`. Default 8 MB main-thread stack, no threading, no depth.

**Guard absent.** Same function, same missing `Py_EnterRecursiveCall`. One fix closes both entries.
This materially raises the finding's severity: `dir(obj)` is not an exotic call, and the trigger is
a one-line `@property` rather than a metaclass descriptor.

### A2. [BOUNDED NEGATIVE — the sweep is complete] Every `__bases__` / `tp_bases` walker in the tree, and which carries the guard

`grep -rn "__bases__\|_Py_ID(__bases__)\|tp_bases" --include=*.c Objects/ Python/ Modules/` plus a
read of every hit. **Three walkers exist. Exactly one is unguarded.**

| # | Site | Recursive? | Guard | Verdict |
|---|---|---|---|---|
| 1 | `Objects/abstract.c:2536` `abstract_issubclass` | yes, over `__bases__` | **`_Py_EnterRecursiveCall(" in __issubclass__")` @ :2571** | guarded, `423fa1c1817` (bpo-30570 / GH-29048, 2021-10-22, fixing gh-74755) |
| 2 | `Objects/typeobject.c:7078` `merge_class_dict` | yes, over `__bases__` @ :7117 | **NONE** | **FIX** — two entry points (`:7147`, **`:8526`**) |
| 3 | `Modules/_abc.c:417` `compute_abstract_methods` | **NO** | n/a | **clean negative** |

On #3: the recursion agent's cross-slice hint does not hold. `compute_abstract_methods` is
`static`, has exactly one caller (`Modules/_abc.c:502`), and never calls itself — it reads
`__bases__` and does a **single-level** `__abstractmethods__` scan of each item. No guard is needed.
State this so the `mod-abc` slice does not re-litigate it.

`Objects/genericaliasobject.c:674` `"__bases__"` is a row of `attr_blocked[]` — a name to *refuse
to proxy*, not a walker.

**The asymmetry is per-file and total.** `Objects/abstract.c` carries **five**
`_Py_EnterRecursiveCall*` guards — `abstract_issubclass:2571`, `object_recursive_isinstance` ×2
(`:2658`, `:2677`), `object_issubclass` ×2 (`:2750`, `:2769`). `Objects/typeobject.c` carries
**zero**, tree-wide grep confirmed, while hosting seven recursive descents. The `__bases__` walk was
guarded in 2021 in one file and never propagated to the other.

### A3. [BOUNDED NEGATIVE — sweep complete, zero net-new] Borrowed container across a user-code call

`grep -n "= *lookup_tp_\(mro\|bases\|subclasses\)(" Objects/typeobject.c` → **28 borrowed loads.**
Each was read together with the following 25 lines. Exactly **five** hold a borrowed container
across a call that can reach Python — and all five are already reported by
`refcount-auditor` / `ft-race-scanner`:

| line | function | crossing call | already reported as |
|---|---|---|---|
| 783 | `_PyType_GetSubclasses` | FT-only (`PyDict_Next` vs concurrent writer) | ft-race FINDING 2, CONSIDER |
| 1195 | `_PyType_Modified_Unlocked` | watcher callback + `PyErr_FormatUnraisable("%R")` | refcount, CONSIDER |
| 9332 | `type_ready_inherit` | `inherit_slots`→`overrides_hash`→`PyDict_Contains` | refcount, **FIX**, ASan UAF |
| 9400 | `type_ready_add_subclasses` | `add_subclass`→`PyWeakref_NewRef`→GC | refcount, CONSIDER |
| 12369 | `recurse_down_subclasses` | `PyDict_Contains` on a subclass `tp_dict` | refcount, **FIX**, ASan UAF |

**Clean, with the reason:** `:5936` `PyType_GetModuleByToken_DuringGC` (flag tests + `_PyModule_GetToken`
only), `:8771` `inherit_special` (`is_subtype_with_mro` is a linear tuple scan), `:10128`
`hackcheck_unlocked` (pointer comparisons), `:11368` `releasebuffer_maybe_call_super_unlocked`
(`tp_as_buffer` pointer comparisons), `:3440/:3458/:3497` `mro_implementation_unlocked` (flat
merge), `:9262` `type_ready_mro`, `:9708/:9754/:9775` `add_subclass`/`remove_subclass`,
`:6851/:6863` `type_dealloc_common`, `:1772/:1808` `mro_hierarchy_for_complete_type`, `:591`,
`:1427`, `:1934/:1957/:2001`, `:3683`, `:6002/:6040`, `:9110`, `:752`. Plus the two INCREF-first
twins the parent named: `:6154/:6161` `find_name_in_mro` and `:12593` `_PySuper_LookupDescr`.

**Tree-wide outside `typeobject.c`: zero.** `lookup_tp_bases/mro/subclasses` are file-static. Every
direct `->tp_dict` / `->tp_mro` read elsewhere in `Objects/`, `Python/`, `Modules/` is either
module-init (`blake2module.c:244/266`, `_decimal.c:7740-7771`, `_multiprocessing.c:226`,
`pyexpat.c:2118`, `_ctypes.c:1685/2476` — own type, single-threaded init) or `_testcapi`. There is
no sixth instance of this shape in CPython.

**New guarded twin, worth citing in the report to maintainers.** `add_subclass:9705-9708` already
does the structurally correct thing for `type_ready_add_subclasses:9400`:

```c
9705    /* Only get tp_subclasses after creating the key and value.
9706       PyWeakref_NewRef() can trigger a garbage collection which can execute
9707       arbitrary Python code and so modify base->tp_subclasses. */
9708    PyObject *subclasses = lookup_tp_subclasses(base);
```

It **defers** the borrowed load until after the Python-invoking call. That is the minimal fix for
`:9400` — and it is written 300 lines away, in the function `:9400`'s loop body calls.

### A4. Reachability ranking of the unfixed siblings

1. `object___dir___impl:8526` → `merge_class_dict:7117` — **`dir()` on an ordinary instance**, reproduced SIGSEGV on two builds, one `@property`.
2. `type___dir___impl:7147` → same — needs a metaclass descriptor, reproduced SIGSEGV + ASan stack-overflow.
3. `type_ready_inherit:9332` — pure Python via `types.CellType()`, ASan heap-UAF.
4. `recurse_down_subclasses:12369` — pure Python, no imports, ASan heap-UAF.
5. `type_set_bases_unlocked:1966` — reproduced SIGABRT/corruption (see (b)).
6. `set_flags_recursive:6494` — `collections.abc.Sequence.register`, static only.
7. `_PyType_GetSubclasses:783`, `type_set_abstractmethods:1745`, `PyType_Freeze:12523` — FT-only, static.

---

## (b) bpo-38554 / PR gh-16879 — verified, with a correction to the framing

**Verified independently via `gh api`.**

```
gh api repos/python/cpython/pulls/16879
  → num=16879 state=closed merged=false created=2019-10-22T03:20:30Z
    closed=2023-06-07T23:24:45Z files=1 +4-3
```

The diff is **exactly** the fix this run's `null-safety` / `error-path` agents want at `:1966`:

```diff
         remove_all_subclasses(type, old_bases);
-        res = add_all_subclasses(type, new_bases);
+        if (add_all_subclasses(type, new_bases) < 0) {
+            goto undo;
+        }
         update_all_slots(type);
```

**At HEAD the call is still unchecked.** `Objects/typeobject.c:1966`:

```c
1965        remove_all_subclasses(type, old_bases);
1966        res = add_all_subclasses(type, new_bases);          /* return DISCARDED */
1967        if (update_all_slots(type) < 0) {                   /* return CHECKED */
1968            goto bail;
1969        }
```

**Correction: this was not a bulk closure.** Issue **gh-82735** (the BPO-migrated bpo-38554,
*"A fatal error in test_descr"*) was closed `state_reason=completed` by **vstinner** at
`2023-06-07T23:24:10Z`, 35 seconds before the PR, with an explicit rationale:

> **furkanonder, 2023-06-07:** *"I couldn't produce the error and I didn't see it in builds. It has
> been three years since the issue was opened. I think we can close the problem."*
>
> **vstinner, 2023-06-07:** *"Since 2019, the code changed, maybe the bug was fixed in the meanwhile.
> Since the bug cannot be reproduced anymore, I close the issue."* — citing `2d03b73cc9c0`
> (bpo-46417, *"remove_subclass() clears tp_subclasses"*) as *an example* of an intervening bugfix.

`2d03b73cc9c0` does not touch `add_all_subclasses` or its return value. The issue was closed
`completed` on a *presumption* that an unrelated commit had fixed it, and the PR that contained the
actual diff was closed as collateral. This is a **root-cause-vs-symptom** closure, not administrative
loss — and it is a stronger story for maintainers, because the reporter's original symptom
(`update_one_slot: Assertion '!PyErr_Occurred()' failed` from `test_descr.test_mutable_bases`) is
precisely what an unchecked `add_all_subclasses` failure produces.

**Orphaned-patch quantification: the "bulk closure orphaned other patches" hypothesis is NOT
supported.** Measured:

| query | count |
|---|---|
| unmerged PRs closed `2023-06-07..2023-06-09` | **15** |
| …of which pre-2020 | **1** (gh-16879) |
| …with `typeobject`/`type_new`/`__bases__`/`PyType`/`segfault`/`crash`/`refleak` in title | **0** |
| unmerged PRs closed in all of June 2023 | 81 |
| …with `bpo-` in title | 7 |
| …with `bpo-` in title closed on 2023-06-07 | **1** (gh-16879) |

No other still-valid patch touching `typeobject.c` or the object layer was orphaned in that window.
This is a real negative and should be reported as one — the loss is a single, specific, still-valid
2019 patch, not a class.

**The sharper temporal fact.** The sibling call on the *very next line*, `update_all_slots(type)`,
**did** get its `< 0` check — added by `fbbbc10055e` (2025-05-27, gh-127266,
`git log -S'if (update_all_slots(type) < 0)'`). So in 2025 a maintainer added return-checking to one
of two adjacent calls in this exact hunk, six years after the other one had been proposed and
refused. `git log -L 1960,1975:Objects/typeobject.c` shows the hunk was touched again 19 days before
HEAD by `20921922fa4` (2026-07-06).

---

## (c) Fix-completeness verdicts on items 4, 5, 6

### Item 4 — allocation inside a stop-the-world region: **`set_flags_recursive` is the only offender in CPython**

Full census of `_PyEval_StopTheWorld` / `_PyEval_StopTheWorldAll` / `types_stop_world()` regions in
`Objects/`, `Python/`, `Modules/` — **38 regions across 13 files**. Each region read for allocation
between the stop and the start.

| file | regions | allocates inside | failure handling | verdict |
|---|---|---|---|---|
| `Objects/typeobject.c:6522` (`_PyType_SetFlagsRecursive` → `set_flags_recursive:6494`) | 1 | **YES** — `_PyType_GetSubclasses` builds a `PyList` per node | **DISCARDED** — `set_flags_recursive` is `void`, bare `return` on NULL | **FIX** |
| `Python/qsbr.c:203` (`_Py_qsbr_reserve`) | 1 | **YES** — `grow_thread_array(shared)` | checked (`if (grow_thread_array(shared) == 0)`), `qsbr` stays NULL, caller returns −1 | ACCEPTABLE |
| `Objects/codeobject.c:3537` (`_Py_ClearUnusedTLBC`) | 1 | **YES** — `get_indices_in_use` + `_PyObjectStack_Push` | `goto err` → `_PyEval_StartTheWorld(interp)` **then** `PyErr_NoMemory()` | ACCEPTABLE — **reference pattern** |
| `Python/instrumentation.c` | 20 | no (every `Py_NewRef` is after StartTheWorld; `PyErr_Format` at `:2131`/`:2483` is likewise after) | — | clean |
| `Objects/typeobject.c` other 8 (`:669`, `:1572`, `:1598`, `:1745`, `:1939`, `:2005`, `:3923`, `:6457`, `:7823`, `:12523`) | 10 | no — `Py_NewRef` on an existing object, flag stores, pointer stores | — | clean |
| `Objects/obmalloc.c:1470`, `:1942`; `Objects/object.c:3410`; `Objects/funcobject.c:361`; `Python/ceval_gil.c:1120`; `Python/pylifecycle.c:2355/:3831`; `Python/legacy_tracing.c`; `Python/ceval.c:17` | rest | no | — | clean |

**Twin confirmed.** `queue_slot_update:3859` is the hoist-out template:
`slot_update_new_chunk()` (`PyMem_Malloc` + `PyErr_NoMemory()`) runs **outside** the region, returns
`-1` on OOM which `update_one_slot` propagates; `apply_slot_updates:3883` — the only thing that runs
with the world stopped — is `void` and does nothing but `*(item->slot_ptr) = item->slot_value` and
`type_clear_flags`. `Objects/codeobject.c:3554-3563` shows the other legal shape: allocate inside,
but `StartTheWorld` **before** `PyErr_NoMemory()`, mirroring
`Python/gc_free_threading.c:2223`.

**Verdict: item 4 is complete and singular.** One site in 38 allocates and discards. The consequence
is not a crash but a silently **partial** `Py_TPFLAGS_SEQUENCE`/`Py_TPFLAGS_MAPPING` propagation
under memory pressure — an inconsistent flag state across a subtree, with `abc.register` returning
success.

### Item 5 — the three-generation gap is confirmed; **there is no fourth site**

Line-history, per site:

```
git log -L 1743,1752:Objects/typeobject.c   (type_set_abstractmethods)
  20921922fa4 2026-07-06  gh-150490 take 2
  fbbbc10055e 2025-05-27  gh-127266 avoid data races when updating type slots   <-- introduced type_lock_prevent_release
  eecafc33800 / e414a2d81c3 2025-04-28  (the revert and the first landing)
  baae9cb159e 2025-02-25  gh-117657 use an atomic store to set type flags

git log -L 12522,12528:Objects/typeobject.c (PyType_Freeze)
  20921922fa4 2026-07-06 · fbbbc10055e 2025-05-27 · eecafc33800/e414a2d81c3 2025-04-28
  baae9cb159e 2025-02-25 · db96327203b 2024-10-25 (created it)

git log -L 6504,6528:Objects/typeobject.c (_PyType_SetFlagsRecursive — the site that GOT the guard)
  20921922fa4 2026-07-06 · e7eaed56149 2026-05-23  gh-148450   <-- guard added here
  fbbbc10055e 2025-05-27 · … · f268e328ed5 2024-04-11 (created it)
```

Both broken sites were **edited by `fbbbc10055e` itself** — the commit that invented
`type_lock_prevent_release()` and wrote the explanatory comment at `:3910-3921`. It added
`types_stop_world()` into their pre-existing `BEGIN_TYPE_LOCK()` regions and added
`ASSERT_TYPE_LOCK_HELD()` after each, without the guard. `b83f379a972` (gh-140549) and
`e7eaed56149` (gh-148450) then propagated the guard to three further sites and skipped these two
again. **Three generations, confirmed by line-history, not by inference.**

**Fourth site: none, tree-wide.** Two independent checks.

1. `TYPE_LOCK` is `static` to `Objects/typeobject.c`. All 9 `types_stop_world()` use-sites are in
   that file. The partition the lock agent gives is exact and I re-verified it by reading all 9.
2. Generalized sweep (`scratchpad/stw_cs.py`): every function in `Objects/`, `Python/`, `Modules/`
   containing **both** a lock-acquire (`Py_BEGIN_CRITICAL_SECTION*`, `BEGIN_TYPE_LOCK`,
   `BEGIN_TYPE_DICT_LOCK`, `PyMutex_Lock`) **and** `_PyEval_StopTheWorld*`. Four hits outside
   `typeobject.c`:
   - `Python/pylifecycle.c:2281` `make_pre_finalization_calls` — `PyMutex_Lock(&interp->ceval.pending.mutex)`
   - `Python/qsbr.c:193` `_Py_qsbr_reserve` — `PyMutex_Lock(&shared->mutex)`
   - `Python/brc.c:55` `_Py_brc_queue_object` — `PyMutex_Lock`
   - `Modules/_testinternalcapi/test_critical_sections.c:418` — a test

   All three real ones use a **raw `PyMutex_Lock`**, not a critical section.
   `_PyCriticalSection_SuspendAll` (`Python/critical_section.c:113-124`) only unlocks mutexes
   registered in the thread's critical-section chain; a raw `PyMutex` is never suspended. **The drop
   hazard does not apply to any of them.**

   *Stated limitation:* the sweep is intra-function and would miss a critical section held by a
   *caller*. I checked the two callers that could matter: `_PyType_SetFlags`'s only caller is
   `Modules/_abc.c:541` and `_PyType_SetFlagsRecursive`'s only caller is `Modules/_abc.c:601`;
   neither is inside a critical section. Both clean.

**Verdict: the two findings are the complete set. The propagation gap is exactly two sites, and it is
three generations deep.**

### Item 6 — PR gh-151394 **does** close the demonstrated dispatch path; it does **not** cover `_PyType_GetSubclasses:783`

Issue and PR state fetched at analysis time:

```
issue 151377  [open]   created 2026-06-11  Data races on type slots and tp_subclasses
                                            between type_setattro and type_new / type_dealloc
PR    151394  [open]   merged=false draft=false  created 2026-06-12  updated 2026-07-03
                                            2 files, +42 −7
```

**Does the PR close the dispatch path?** Yes. I verified it against `update_one_slot`'s write path
rather than trusting the shape:

```c
Objects/typeobject.c (update_one_slot, tail)
#ifdef Py_GIL_DISABLED
    if (queued_updates != NULL) {
        // queue the update to perform later, while the world is stopped
        if (queue_slot_update(queued_updates, type, ptr, slot_value) < 0) { … }
    } else {
        *ptr = slot_value;
    }
#else
    assert(queued_updates == NULL);
    *ptr = slot_value;
#endif
```

In the free-threaded build, a non-NULL `queued_updates` means **no** slot store happens inside
`update_one_slot` — including the `Py_TPFLAGS_HAVE_VECTORCALL` clear, which is gated on
`queued_updates == NULL` and instead performed by `apply_slot_updates:3893`. The PR passes
`&queued_updates` and routes everything through `apply_type_slot_updates()`, which is
`type_lock_prevent_release(); types_stop_world(); apply_slot_updates(); types_start_world();
type_lock_allow_release();`. **With the world stopped, the dispatching thread in the FT agent's
`repro_call_only.py` cannot be running at all during the write window.** The 4/4 SIGSEGV/SIGABRT
heap-corruption reproduction is closed by the PR as written, not merely the TSan update-vs-update
race the issue describes.

The PR also correctly changes the signature to `int` and propagates the `queue_slot_update` OOM
failure into `type_new_impl`'s `goto error` — closing the allocation-failure path that the current
`void` signature silently swallows.

**What the PR does not cover** (diff fetched via `gh api repos/python/cpython/pulls/151394/files`;
it touches exactly `TYPE_IS_REVEALED`/`TYPE_SET_REVEALED`, `fixup_slot_dispatchers`, and
`type_dealloc_common`):

- **`_PyType_GetSubclasses:783` — NOT covered.** The reader side is untouched. Its comment
  > `// The loop cannot modify tp_subclasses, there is no need`
  > `// to hold a strong reference (use a borrowed reference).`

  is the same false invariant as `recurse_down_subclasses:12366`, and `type___subclasses___impl:7058`
  reaches it with no lock and no `@critical_section`. The PR's new `BEGIN_TYPE_LOCK()` around
  `remove_all_subclasses` in `type_dealloc_common` synchronises the **writer** only; the unlocked
  reader at `:783` still races it. **Recommendation: `_PyType_GetSubclasses` needs its own
  `BEGIN_TYPE_LOCK()`, and the comment at `:790-791` must be deleted, in the same PR.**
- `recurse_down_subclasses:12369` — not covered, and it is a **GIL-build** heap-UAF (ASan-confirmed
  by `refcount-auditor`), i.e. not an FT-only concern at all.
- `type_set_flags:458` — named by the FT agent as a co-racing site, not touched.

**Verdict: gh-151394 is correct and sufficient for what it claims, understates its own value (it
fixes memory corruption, not a stale slot read), and is incomplete against the sibling set by three
sites.** It has been open 43 days.

---

## (d) Recently-audited-but-still-broken

Where a confirmed finding sits in lines a recent commit deliberately audited *for something else*.
This is the evidence that the reviews which missed these were **scoped, not exhaustive** — the
strongest argument against "this code has been looked at".

| Finding | Last commit to touch those exact lines | What that pass was auditing | Gap |
|---|---|---|---|
| `type_ready_inherit:9332` borrowed `tp_mro` heap-UAF | **`a8edca62fc6` 2025-10-21** — gh-132835 *"Add defensive **NULL checks** to MRO resolution (GH-134763)"* | NULL invariants in MRO resolution | 9 months ago, in `L9330-9345`, one line above the UAF read at `:9336` |
| `set_flags_recursive:6494` alloc-in-STW | **`baae9cb159e` 2025-02-25** — gh-117657 *"Use an **atomic store** to set type flags"* | atomicity of the flag store at `:6491` | the `_PyType_GetSubclasses` call three lines below was not examined |
| `PyType_Freeze:12523` missing `type_lock_prevent_release` | `20921922fa4` 2026-07-06 · `fbbbc10055e` 2025-05-27 · **`baae9cb159e` 2025-02-25** (atomic flag stores) | three separate flag/lock-safety passes over the same 6 lines | guard missed by all three |
| `type_set_abstractmethods:1745` same | `20921922fa4` 2026-07-06 (19 days) · **`fbbbc10055e` 2025-05-27 — the commit that invented the guard** | free-threaded type-slot races | the guard's own author edited this function without applying it |
| `type_set_bases_unlocked:1966` unchecked `add_all_subclasses` | `20921922fa4` 2026-07-06 · **`fbbbc10055e` 2025-05-27 — which added `if (update_all_slots(type) < 0)` on line 1967** | return-value checking in this exact hunk | the adjacent call got the check; this one did not |
| `recurse_down_subclasses:12369` borrowed `tp_subclasses` UAF | **`fc32522b081` 2023-06-21** — gh-105927 *"type_from_ref() uses `_PyWeakref_GET_REF()`"* | reference correctness **inside this very loop** | a refcount pass over the loop that missed the container's own refcount |
| `_PyType_GetSubclasses:783` | `fc32522b081` 2023-06-21 · `f73abf8e03f` 2023-05-01 (accessor migration) | same | same |
| `super_descr_get:12786` NULL `su->type` (CPY-0007) | **`a781484c8e98` 2023-05-12** *"Fix refleak in `super_descr_get`"* · **`3d40317ed24d` 2025-02-25** gh-111178 UBSan | a refcount pass and a UBSan pass over the same function | parent's example — confirmed verbatim |
| `merge_class_dict:7117` unguarded recursion | `be1b968dc1e` 2023-07-12 (mechanical `_PyObject_LookupAttr` removal); last **substantive** change `82b00c1d307` **2011-05-24** | — | 14 years untouched; the guarded twin was fixed in 2021 |

Six of the nine were audited within the last 3 years by a commit whose stated purpose was a
*different* bug class in the *same lines*.

---

## Sweep-completeness statements (one per shape, including the clean ones)

1. **`__bases__` / `tp_bases` recursive walker.** 3 walkers tree-wide; 1 guarded
   (`abstract.c:2571`), 1 not recursive (`_abc.c:417`), 1 unguarded with **two** entry points
   (`typeobject.c:7117` via `:7147` and `:8526`). Complete.
2. **Borrowed type-container across user code.** 28 borrowed loads in `typeobject.c`; 5 span a
   Python-invoking call, all 5 already reported. Zero instances outside `typeobject.c` — the
   accessors are file-static and every external `->tp_dict`/`->tp_mro` read is module-init or
   `_testcapi`. Complete, zero net-new.
3. **Allocation inside a stop-the-world region.** 38 regions across 13 files; 3 allocate; 2 handle
   the failure correctly (`qsbr.c:203`, `codeobject.c:3539`); 1 discards it
   (`typeobject.c:6494`). Complete.
4. **Critical section held across `_PyEval_StopTheWorld` without `type_lock_prevent_release()`.**
   9 `types_stop_world()` sites (5 guarded, 2 lock-free and correct, 2 broken); 3 further
   functions tree-wide combine a lock with STW, all using a raw `PyMutex` that
   `_PyCriticalSection_SuspendAll` never suspends. **No fourth site.** Complete for intra-function
   scope; caller-held critical sections checked for the two reachable entry points.
5. **`Py_EnterRecursiveCall` in `Objects/typeobject.c`.** Zero, confirmed by grep. Seven recursive
   descents. Complete.

---

## Artifacts

| path | what |
|---|---|
| `scratchpad/objdir_bases.py` | **A1** — `dir()` via `object.__dir__` + `__class__` property, exit 139 on debug and release |
| `scratchpad/stw_cs.py` | tree-wide critical-section-across-StopTheWorld sweep (item 5) |

(`scratchpad` = `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`)
