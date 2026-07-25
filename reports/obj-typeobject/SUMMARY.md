# obj-typeobject — informed-explore, pass 1

**Slice:** `Objects/typeobject.c`, 13,068 lines, single file, tier A
**Target:** CPython main @ `4f3be1b5777` (3.16.0a0), full clone
**Toolkit:** cpython-review-toolkit @ `25e7bbe` (v0.8 + review-slice campaign)
**Agents:** 15 dispatched, 15 returned (of 23 in the toolkit; see *Not dispatched* below)
**Scanner candidates going in:** 11

Builds used: `~/projects/python_build_matrix/builds/<name>/python` @ `a1d580430c8`.
`typeobject.c` is **byte-identical** between that commit and the target ref, verified by
every agent that cited it.

---

## Headline

Eleven scanner candidates produced **two confirmed true positives**. Reading produced
**twelve more FIX-grade findings**, eight of them reproduced as crashes, corruption or
use-after-free.

The dominant structural fact: **`typeobject.c` is a file where CPython repeatedly wrote
the correct guard and then failed to apply it to a sibling.** Eleven of the fourteen FIX
findings have a *guarded twin in the same file* — often with a comment naming the exact
hazard, sometimes written decades earlier. That is what makes this slice reportable:
almost none of these require arguing that a hazard exists, only that a known fix was not
propagated.

---

## FIX (14)

Reproduction column: **ASan** = sanitizer-confirmed; **crash** = SIGSEGV/SIGABRT;
**corrupt** = silent wrong state on release; **static** = read-confirmed only.

| # | Site | Defect | Repro | Guarded twin |
|---|---|---|---|---|
| 1 | `supercheck:12763` ← `super_descr_get:12797` | NULL `su->type` deref (**CPY-0007**, recorded) | crash | `super_repr:12575`, `super_getattro:12643` (2001/2003) |
| 2 | `type_ready_inherit:9332` | borrowed `tp_mro` freed by user `__eq__` | **ASan** + crash | `find_name_in_mro:6170`, `_PySuper_LookupDescr:12597` |
| 3 | `recurse_down_subclasses:12369` | borrowed `tp_subclasses` freed under `PyDict_Next` | **ASan** | `mro_hierarchy_for_complete_type:1836` |
| 4 | `type_set_bases_unlocked:1966` | `add_all_subclasses` result never branched on | crash + **corrupt** | line **1967**, the next line |
| 5 | `merge_class_dict:7117`, via `type.__dir__:8478` **and `object___dir___impl:8526`** | unguarded recursion over cyclic `__bases__`; the second caller makes plain **`dir(obj)`** the trigger | crash + ASan | `abstract_issubclass` `abstract.c:2571` |
| 6 | `fixup_slot_dispatchers:12136` | slots rewritten after type published | **4/4 crash (FT)** | `update_all_slots:12144`, `update_slot_after_setattr:6726` |
| 7 | `set_flags_recursive:6494` | `PyList_New`/`Append` with world stopped, failure dropped | crash (OOM) | `queue_slot_update:3859` |
| 8 | `find_name_in_mro:6183` | swallow → slot silently cleared | reproduced | `vectorcall_maybe:3149` |
| 9 | `has_dunder_getitem:11090` → `slot_tp_iter:11108` | discards `-1`, overwrites live exception, `__context__ = None` | reproduced | `vectorcall_maybe:3145`, `slot_tp_finalize:11227` |
| 10 | `type_repr:2405` | bare clear eats user `__eq__` exception | reproduced | `type_add_method:8614` |
| 11 | `object_repr:7490` | same shape as #10 | reproduced | `type_add_method:8614` |
| 12 | `find_name_in_mro:6149` | swallow → wrong `AttributeError` | reproduced | `vectorcall_maybe:3149` |
| 13 | `same_slots_added:7609` | collapses `RichCompareBool` tri-state | reproduced | `:10566` |
| 14 | `dictobject.c:4493` **(cross-slice)** | `assert` derefs unchecked `d`; UB deletes the inlined NULL check | crash (debug) | — |

### The four worth filing first

**#4 `type_set_bases_unlocked:1966`** — the strongest single item. `remove_all_subclasses`
has already unlinked the type at `:1965`; the unchecked failure at `:1966` falls into
`update_all_slots()` with a live exception and skips the `goto bail` rollback. Debug:
SIGABRT on `assert(!PyErr_Occurred())`. Release: **silent permanent corruption** —
`C.__bases__` is committed while `MemoryError` is raised, and `C` lands in *neither*
parent's `__subclasses__()`, so slot updates never reach it again for the life of the
process. The control (`N=1,2` → correct rollback; `N=4,5,6` → corruption) is the proof.

A patch for exactly this was proposed in 2019 — **bpo-38554 / PR python/cpython#16879,
`merged=false`, diff exactly `if (add_all_subclasses(type, new_bases) < 0) goto undo;`**.
It was **not** lost to a bulk closure: gh-82735 was closed `state_reason=completed` with
an explicit rationale — *"Since 2019, the code changed, maybe the bug was fixed in the
meanwhile. Since the bug cannot be reproduced anymore, I close the issue"* — citing
`2d03b73cc9c0` (bpo-46417, `remove_subclass`), **which does not touch
`add_all_subclasses`**. Presumption of fix, not administrative loss. The orphaned-patch
hypothesis was tested and rejected: of 15 unmerged PRs closed 2023-06-07..09, only one
predates 2020 and none mention typeobject/PyType/crash/refleak.

The sharpest framing for a maintainer: **two calls on consecutive lines — `update_all_slots`
got its `< 0` check in `fbbbc10055e` (2025-05-27); `add_all_subclasses` was offered one in
2019 and refused.**

**#2 and #3, the borrowed-container UAFs** — both ASan-confirmed, both from pure Python
(#3 in 45 lines with no imports). The generalisable edge: **a type dict may hold
non-string keys** (`type_new_impl:4960` only warns), so *any* `PyDict_*` call on a
`tp_dict` can dispatch a user `__eq__`. #3 is especially clean to report: the comment at
`:12366` asserts safety on grounds that the *callback* is harmless, while the *guard test*
at `:12386` runs arbitrary Python — and the twin's comment at `:1836` directly contradicts
it. Two comments, same file, opposite conclusions, and the wrong one is load-bearing.
Note #2's free-threaded arm is already correct: `set_tp_mro:664` enables deferred
refcounting, i.e. **FT ships the fix the GIL path lacks**.

**#5 `merge_class_dict:7117`, reached from two callers** — self-recursion over `__bases__`,
which from level 2 is an arbitrary Python object and may be **cyclic**. The twin
(`abstract_issubclass`) received precisely this guard from **bpo-30570 / GH-29048 to fix
gh-74755 — the same segfault in the sibling `__bases__` walker.** `merge_class_dict` has
been unchanged since 2011.

The second caller is the reachable one. `type.__dir__` (`:8478`) needs a metaclass
`__bases__` descriptor; **`object___dir___impl:8526` passes `itsclass` from
`PyObject_GetOptionalAttr(self, &_Py_ID(__class__))` — an ordinary `@property`.** So the
primary trigger is **`dir(obj)` on an object whose `__class__` property returns something
with cyclic `__bases__`**: 10 lines, no metaclass, default 8 MB stack, SIGSEGV exit 139 on
both `debug-gil-nojit` and `release-gil-nojit`.

The tree-wide census closes the class: **3 `__bases__`/`tp_bases` walkers exist, exactly 1
is unguarded.** `Modules/_abc.c:417` — flagged as a cross-slice lead — is **not recursive**
(single-level scan, one caller); that is a clean negative and should not be re-litigated in
a later slice. `abstract.c` carries five `_Py_EnterRecursiveCall*` guards; `typeobject.c`
carries **zero** and hosts seven descents.

**#6 `fixup_slot_dispatchers`** — gh-151377 open, PR gh-151394 unmerged for 43 days.
**Correction to the initial reading: the PR *does* close the dispatch path.** Verified
against `update_one_slot`'s tail — with `queued_updates != NULL` the FT build performs no
slot store there (including the `HAVE_VECTORCALL` clear), so every write lands in
`apply_slot_updates` under `types_stop_world()` and the dispatching thread is stopped for
the whole window. The 4/4 heap corruption is closed by the pending patch.

So the finding's value is not "the PR is insufficient" but: **the PR is correct, has been
open 43 days, and the reproduced heap corruption is the argument for merging it** — plus
three siblings it does *not* cover: `_PyType_GetSubclasses:783` (reader untouched; the new
`BEGIN_TYPE_LOCK` in `type_dealloc_common` synchronises only the writer),
`recurse_down_subclasses:12369` (**a GIL-build UAF, outside the PR's scope entirely**), and
`type_set_flags:458`.

---

## CONSIDER (19)

Two are reproduced crashes held below FIX purely on reachability:

- **`type_from_slots_or_spec:5623`** — the function rejects a custom metaclass `tp_new` at
  `:5562` and then dispatches through that same metaclass's **unvalidated `tp_alloc`**.
  `ht_slots` is never written on any path of that function, and `type_dealloc:7034`
  unconditionally `Py_XDECREF`s it. **Reproduced SIGSEGV** (probe extension, OOM sweep,
  n=5; gdb shows `Py_XDECREF(op=0xdddddddddddd0001)`). Not Python-reachable
  (`type_new_alloc:4437` force-sets the allocator) but **extension-reachable** —
  `Py_tp_alloc` is an accepted spec slot and `arraymodule.c:3163` already uses it.
  `Doc/c-api/typeobj.rst:2145` claims heap subtypes always get `PyType_GenericAlloc`; that
  is already false. Appears novel.
- **`super_init_without_args:12839`** — `assert(PyCell_Check(firstarg))` is debug-only;
  release casts on `co_localspluskinds[0]` alone. **Reproduced exit 139 on
  `release-gil-nojit` and `release-ft-nojit`.** Crafted-code-object only; guarded twin 20
  lines below at `:12859`. One-line fix.

Then: `super_descr_get:12793` (vararg NULL truncation — `*ObjArgs` counts to the first
NULL, so a NULL `su->type` silently turns a 2-arg call into 0-arg and drops `obj`;
**closed by the same one line as #1**); `_PyType_GetSubclasses:783` (borrowed ref across
`PyDict_Next`, guarded by a **GIL-era comment**, untouched by PR 151394);
`_PyType_Modified_Unlocked:1195` (third instance of the borrowed-container shape, via a
type-watcher callback); `slot_tp_finalize:11243` (restores over a live exception — proven
by minimal pair against `type_new_set_names:12288`, which propagates on the identical
trigger); `types_stop_world()` under TYPE_LOCK without `type_lock_prevent_release()` at
`type_set_abstractmethods:1745` and `PyType_Freeze:12523` (**a guard propagated across
three commits missed both, and the first of those added the STW call**);
`-spec->basicsize:5290` signed-overflow UB; `_PyObject_SetDict:4032` (latent);
`type_ready_add_subclasses:9400` (latent); `find_name_in_mro:6158`; `:592`, `:5946`,
`:709`/`:725`; and five class-hierarchy recursion descents, of which
**`update_subclasses:12356 ↔ recurse_down_subclasses:12397` is now ASan-confirmed** — a
mutual recursion that per-function analysis structurally cannot see.

**POLICY (4):** `type_set_name`/`type_set_qualname` (`:1572`/`:1598`) measured at 14.3M/s
single-threaded → **3,450/s at two threads, a 4,141× collapse** under FT;
`object_set_class` got the `_PyObject_IsUniquelyReferenced` fast path in gh-145566 and
these did not. Plus three exception-loss sites reported as POLICY by the clear auditor.

---

## Bounded negatives

Kept because a cold run never states them, and because two were *wrong* in a way that
matters:

- **`__slots__` allocation arithmetic is safe — but not for the reason CPY-0012 suggests.**
  `typeobject.c` has **no** `PY_SSIZE_T_MAX/elemsize` guard anywhere. It is safe because
  `__slots__` is read *once* into `ht_slots` (never re-read from the mutable type dict the
  way `PyStructSequence_New` re-reads `n_fields`) and every members walk is bounded by
  `Py_SIZE(type)`. Wrapping needs a 3.7 EiB tuple. Verified: post-creation
  `C.__slots__` mutation changes nothing under ASan, while `os.terminal_size.n_fields = 8`
  still works — CPY-0012/0013 remain live.
- **Duplicate slots: the new `PyType_FromSlots` path validates *more*, not less.** Both
  `Py_tp_doc` and `Py_tp_members` are REJECT-tier at `Python/slots.c:367`, proven live via
  `_testcapi.create_type_from_repeated_slots`. The real deliverable is a **test gap** —
  `test_pytype_fromspec_with_repeated_slots` covers only `PyType_FromSpec`, and a
  regression there is a heap-overflow read at `:5682`.
- 25 critical-section regions, **all balanced, zero early exits**. `type_ready`'s 14 gotos
  target one intra-function label and the function holds no lock at all.
- Destructor family clean: 15 destructors, 0 clears inside. `subtype_dealloc` — the target
  I flagged as highest-value — has no clear, no fetch, no dropped fallible result.
- `type_dealloc:6998` provably unreachable with a pending exception (6000 watcher failures
  across two builds, zero lost).
- Deletable-member bypass: **structurally empty** — both member tables are 100%
  `Py_READONLY`.
- 69 macros, **zero live defects**; the `slotdefs[]` builders derive `#NAME` and
  `_Py_ID(NAME)` from one token, so name drift is structurally impossible.
- `type_repr`/`object_repr` are **not recursive at all** (`%U` over precomputed strings).

---

## Toolkit defects (29)

Full detail in the per-agent reports. Ranked by blast radius:

### Chassis — affects all six sibling toolkits

1. **`extract_functions` silently drops 686 lines (5.3%) of this file** to `SLOT0`/`SLOT1`
   statement macros. The gap contains `slot_tp_hash`, `slot_tp_call`, `slot_tp_getattro`
   and the async/buffer slots — exactly the user-Python-dispatching population the rules
   police. Scanner reported 9 `PyErr_Clear` against a true 11. Same family as the
   multidict `preproc_ifdef` gap (cext #64 / ft #24). Fix upstream in
   `tree_sitter_utils.py`; add an unattributed-line canary.
2. **`run_oom_sweep.py` arms `set_nomemory(n)` unbounded**, so any allocating payload
   cascades at n=1 with `lost sys.stderr` — a false positive that reads as a crash.
   Bounded `set_nomemory(n, n+1)` moved the same payload to the real failure at n=127.
   Needs `--width`. This is the reproduction harness used in previous runs.

### Structural blindness — measured denominators

| scanner | resolved | of | note |
|---|---|---|---|
| `scan_null_checks` | 49 | 760 (**6.4%**) | closed API enum omits `PyDict_SetItem`, `PyTuple_Pack`, ~120 in-file statics. `decref_of_nulled_outparam` denominator = **0** |
| `scan_uninit_dealloc` | 1 | 8 | every `->tp_alloc(…)` silently assumed zeroing; 4 of 5 sites dropped with no record |
| `scan_gil_usage` | 0 | — | vocabulary sums to zero; should emit `rule_not_applicable` |
| `scan_stw_safety` | 2 | 11 (18%) | matches only literal `_PyEval_StopTheWorld`; the file wraps it at `:124`/`:133` |
| `scan_lock_discipline` | 2 | 25 (8%) | 3 of 6 vocabulary hits are the `#define` lines themselves |
| `scan_recursion_guards` | 0 | 7 | `:596` gates `self_recursion` on being a *type slot*; all 7 descents are static helpers |
| `scan_refcounts` | 0 | 403 fns | every v0.9 rule anchors on a **release**; all three UAFs only *read* the borrowed local |

**A correction worth recording:** the `Objects/`-wide `scan_lock_discipline` zero is **not**
a macro-alias artifact. `Objects/` contains 156 raw `Py_BEGIN_CRITICAL_SECTION` + 30 `CS2`
across 18 files, all in the vocabulary; **17 of 18 are an earned zero and `typeobject.c` is
the outlier**. The initial hypothesis over-generalised and the agent measured it down.

### New rules proposed (with the finding each would have caught)

- `borrowed_field_deref_across_call` → #2, #3. Seed field accessors from
  `static inline PyObject *f(...) { return X->field; }`; **add `PyDict_Contains`/`SetItem`/
  `GetItem*`/`DelItem`/`SetDefaultRef`/`Next` to `PYTHON_REACHING_APIS`**; require a
  *dereference*, never a pointer comparison; emit `high` when a guarded twin exists in the
  same file — that test alone separates the 3 findings from the 8 clean borrowed-MRO loops.
- int-sentinel **assigned but never branched on** → #4. `scan_error_paths`'s
  "value returned directly" suppression is right for pointers and **wrong for ints**.
- un-gate `self_recursion` from slot-hood → #5 at `high`; add a mutual-recursion shape
  using the call graph `scan_stw_safety` already builds → the ASan-confirmed pair.
- `publish_before_init_complete` (published at line A, fields still written at B>A) → #6.
- one-hop interprocedural sink (nullable field → same-file static that derefs it) → #1,
  whose reported line `:12806` **is dead code**; the catalog's `:12763`/`:12797` are right
  and the scanner was correct by luck.
- `_VARARG_SENTINEL_SINKS` scanning args 1..n-1 → the `*ObjArgs` truncation.
- `_VAROBJ_NITEMS_INDEX` to model the `tp_alloc` **slot pointer**: 132 indirect sites
  tree-wide, **15 with non-constant nitems**.
- treat `assert(EXPR(x))` as a deref, not a check.
- emit denominators in every JSON envelope.

### Data-file defects

- **All three PEP 667 entries carry `drop_in: true` with an empty caveat.**
  `PyEval_GetGlobals` returns **borrowed**, `PyEval_GetFrameGlobals` returns **strong** —
  a rename alone leaks a module `__dict__` per class creation. `PyEval_GetBuiltins` even
  states *"Returns a borrowed reference"* in its own `notes` while asserting drop-in. This
  is the second firing of the trap that produced the `drop_in`/`caveat` schema (gh-148241).
  `findings_needing_a_caveat: 0` was a **false all-clear**; needs a test asserting
  `drop_in: false` ⇒ non-empty caveat, plus a canary on `replacement` containing
  `Ref`/`Frame`/`New`.
- `Py_BEGIN_CRITICAL_SECTION2_MUTEX` absent from `lock_macros.json` while its partner is
  present.
- The `stop_the_world` family is present in the data file but **unmodelled** — both
  lock-discipline findings live in that gap.
- **The STW deny-list is inverted:** `Py_BEGIN_CRITICAL_SECTION` inside STW is *safe*
  (`_PyCriticalSection_SuspendAll`); a raw `PyMutex_Lock` is the hazard.
- `H-2 masks H-1` in `scan_uninit_dealloc`: forcing `tp_alloc` non-zeroing *still* yields
  zero findings, because alias blindness on `type = &res->ht_type` sits in front of the
  virtual-dispatch gap. Fixing either alone changes nothing.

---

## Fix-completeness sweep (tree-wide)

Three classes were swept to exhaustion. Two came back **complete**, which is as useful as a
finding:

- **Borrowed-container UAF: 28 borrowed `lookup_tp_{mro,bases,subclasses}` loads in this
  file; exactly 5 span a Python-invoking call — all 5 already reported. Outside
  `typeobject.c`: zero**, because the accessors are file-static and every external
  `->tp_dict`/`->tp_mro` is module-init or `_testcapi`. Four more (`:5936`, `:8771`,
  `:10128`, `:11368`) are pointer-comparison-only, exactly the class the proposed rule must
  exclude. A **new guarded twin** surfaced for `:9400`: `add_subclass:9705-9708` defers the
  borrowed load until after `PyWeakref_NewRef()`, with a comment naming GC re-entrancy —
  that is the minimal fix template.
- **Allocation inside stop-the-world: 38 regions across 13 files, 3 allocate, only
  `set_flags_recursive:6494` discards the failure.** `qsbr.c:203` checks and propagates;
  `codeobject.c:3539` calls `_PyEval_StartTheWorld` *then* `PyErr_NoMemory()` — the
  reference pattern. All 20 `instrumentation.c` regions clean.
- **`type_lock_prevent_release()`: exactly two broken sites, no fourth.** Line-history
  confirms `fbbbc10055e` — *the commit that invented the guard* — edited both
  `type_set_abstractmethods` and `PyType_Freeze` without applying it. The three functions
  outside `typeobject.c` that combine a lock with STW (`pylifecycle.c:2281`, `qsbr.c:193`,
  `brc.c:55`) all use a raw `PyMutex_Lock`, which `_PyCriticalSection_SuspendAll` never
  suspends, so they are unaffected.

### Six of nine findings sit in lines a recent commit already audited

The pattern is worth stating to maintainers, because it explains how code this
heavily-reviewed still holds these bugs — the reviews were **scoped to a different defect
class**:

| finding | prior pass over the same lines |
|---|---|
| `type_ready_inherit:9332` | `a8edca62fc6` (2025-10-21) *"Add defensive **NULL checks** to MRO resolution"* — touched L9330-9345, **one line above** the UAF read |
| `set_flags_recursive:6494` | `baae9cb159e` (2025-02-25) *"Use an **atomic store** to set type flags"* — audited the store **three lines above** the allocation |
| `recurse_down_subclasses:12369` | `fc32522b081` (2023-06-21) a **refcount** pass *inside this very loop* that missed the container's own refcount |
| `PyType_Freeze:12523` | three flag/lock passes over six lines |
| `super_descr_get:12797` | `a781484c8e98` (2023, refleak), `3d40317ed24d` (2025, UBSan) |

## Process notes

- **Cross-agent lead routing worked.** `c-complexity-analyzer` produced the `tp_alloc`
  hypothesis; forwarded to `uninitialized-dealloc-auditor`, which reproduced it as a
  SIGSEGV with a purpose-built extension. Its second forwarded item was cleanly *killed*.
  A `Py_tp_doc` lead was also killed, and the kill produced the test-gap finding.
- **Agents corrected the brief three times**, each time with measurement: the "36 error
  paths" framing, the CPY-0012 guarded-twin framing, and the `Objects/`-wide lock-macro
  hypothesis. All three corrections are kept above.
- **The scope defect** — the manifest's hand-written `scope` sent agents at all 50 files of
  `Objects/`. Found within 30 seconds of the first real run, after 24 tests, mypy and ruff
  all passed it. Fixed on `fix/slice-scope-derivation`.
- **Reproducers:** 3 curated in `repro/`, 86 raw probes preserved in `repro/scratch/`
  (session scratchpad is not durable — collect before it is lost).

## Not dispatched

`parity-checker` (no pure-Python twin for `typeobject.c`), `pep7-style-checker` (volume
without soundness bearing on a 13k-line file), `tsan-report-analyzer` /
`tsan-stress-generator` / `oom-reproducer` (demand-driven; the relevant agents were given
the TSan builds and sweep script directly and used them). `macro-hygiene-reviewer` was
folded into the deprecation agent. `git-history-analyzer` runs last — pending at time of
writing.

## Pass 2 (not started)

Regions deliberately left: MRO C3 (3217–3702), lookup cache (6140–6452), getattro/setattro
(6529–6848), `__class__` assignment (7482–7846), pickle (7848–8406), watchers/versions
(971–1481), managed static types (228–522), and the `super` region beyond the construction
surface. Note the history agent's per-region density puts **four of the top six
crash-fix-dense regions in pass 2**, so the yield there should not be assumed lower.
