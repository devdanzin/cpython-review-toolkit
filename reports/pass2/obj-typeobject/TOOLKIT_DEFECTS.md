# obj-typeobject pass 2 — running toolkit-defect ledger

Accumulated as agents report. Each row becomes either a toolkit issue or a PR at synthesis.

## ⭐ D-13 — THE UNIFYING DEFECT: three scanners blind for one reason

**Accessor-name keying vs plain field reads.** Three independent lanes converged on the same root cause,
each having found real bugs by reading past it:

| scanner | what it keys on | what it therefore misses | cost measured on this file |
|---|---|---|---|
| `scan_refcounts` | the 4 `lookup_tp_*` accessor names | `su->type`, `Py_TYPE(x)` into a local, any plain field read | **recall 0 of 6**; missed P2-F1 and P2-F5, both ASan UAFs |
| `scan_recursion_guards` | `_OBJECT_GRAPH_WALK_OPS` accessor names (`scan_recursion_guards.py:236-246`) | `solid_base` recursing on the plain field read `type->tp_base` | **recall 6 of 7**; missed P2-F7, exit 139 debug+release |
| `scan_null_checks` | `nullable_source_calls` needs a literal `return NULL` or a call-forwarder | a **field-forwarding** accessor (`return self->tp_mro;`) | hides **53 sites** on this file |

**One shared fix serves all three: a graph-field table** — `tp_base` / `tp_bases` / `tp_mro` /
`tp_subclasses` (+ the instance-owning fields for `scan_refcounts`) — keyed on the **member name**, not
on the accessor function name. Belongs in `scan_common`, consumed by all three scanners.

**Precision gate (from `scan_refcounts` W1, the strongest version):** treat `X->field` as a borrowed
load **iff** the file contains a re-binding writer for that member name (`Py_XSETREF` / `Py_SETREF` /
`Py_CLEAR` / save-old-then-store). The *writer* is what keeps it cheap instead of noisy. That gate alone
recovers both of pass 2's net-new UAFs.

**Why the blindness exists at all** — worth writing into the design notes: the accessor names came from
a **2023 encapsulation refactor (`f73abf8e03fd`, gh-94673) that was never a lifetime audit**. The
toolkit inherited a naming convention as if it were a semantic boundary. And `tp_dict`, the field that
refactor mostly touched, is the one field of the four with **no Python-reachable writer at all**
(`type_getsets:2384` registers `__dict__` with a NULL setter) — so the rules are best-calibrated
precisely where nothing can go wrong.

**Supersedes D-3, D-9, and the `scan_null_checks` RG-1 gap.**

## 🔴 D-14 — `run_oom_sweep.py` reports a FALSE CLEAN when the setup raises

**The most dangerous defect found in this run, because its failure mode is a clean bill of health.**
**Source:** uninitialized-dealloc-auditor, pass 2.

The child harness runs `exec(_SETUP_CODE, _NS)` **outside any `try`**. A raising setup exits 1, and
`classify()` maps exit 1 to **`memory_error` — the SAFE outcome**. So a broken setup produces a perfect
score.

**Measured:** the agent's first **four** sweeps reported **400/400 clean**, caused entirely by a
`pickle.dumps` in the setup. After deleting **one line**, the same sweeps produced **13 aborts and
2 SIGSEGVs**.

**Fix:** guard the setup `exec` with `sys.exit(3)` → a distinct `setup_error` outcome, and make
`sweep()` run one **unarmed dry-run** before the loop. Reference implementation at
`repro/uninit-dealloc/dryrun.py`.

**✅ FIXED — oom-reproducer, pass 2.** Shipped in `run_oom_sweep.py` (uncommitted on `main`): guarded
setup+compile → exit 3 → `setup_error`; `dry_run()` rehearses setup+payload unarmed before the loop and
returns an `error` with **no `summary`/`reproduced` key** on failure; `sweep()` aborts mid-loop on any
`setup_error`; `--no-dry-run` escape marks the clean verdict UNVERIFIED. Tests 796 → **812**, ruff + mypy
clean. Live A/B on `debug-gil-nojit` with a raising setup: pre-fix child `{memory_error: 20}`, fixed child
`{setup_error: 20}` (`repro/oom/d14_demo.py`).
**Also shipped, same family:** the child now names non-`MemoryError` exceptions
(`payload_exceptions` on the result). P2-F2's entire signature was an anonymous `other_exception` — the
finding was in the output and unreadable. It is now self-reporting.
**Retrospective scope: see `agents/oom-reproducer.md` §5.** Two items MUST be re-run (pass-1
uninit-dealloc's 1150-iteration verdict; modules-sample error-path's four clean sweeps). Every *crash*
result is structurally unaffected. **The defect is cloned in 8 harnesses outside `run_oom_sweep.py`**,
including five catalog `repro.py` files — the fix does not reach them.

## 🟠 D-17 — a "clean" sweep's real denominator is `iterations_run − completed`, and it is often ~4

**Source:** oom-reproducer, pass 2. **Survives the D-14 fix** — it is the mirror image of it.

With `width=1`, every index past the payload's last allocation is `completed` and tests **nothing**. A
400-iteration sweep reporting 4 `memory_error` is 4 pieces of evidence, not 400 — but it prints as
`"no crash in this range (all failures handled cleanly)"` either way, and the dry run passes happily.

**Measured on this file:** `__class__` assignment, pickle and lookup/getattro were certified clean over
**4, 12 and 5** allocation-failure points. The cause is over-warming — `p2_setup.py` warms the very paths
it then tests. De-warmed variants (`repro/oom/cold_*.py`) raise those to 11 / 83 / 11.

D-14 makes a *broken* setup look clean; D-17 makes a *working* setup test almost nothing. Both produce
false confidence, and only the first now fails loudly.

**Fix:** `sweep()` computes `allocation_failure_points = iterations_run − completed` and refuses the
phrase "handled cleanly" below a threshold (~20), saying "clean over N=4 failure points — too thin to
certify" instead.

## 🔴 E1 — `*-ft-*-asan` builds have NO ASan shadow for the object heap (campaign-level)

**Second evidence-invalidating defect of this run, alongside D-14.**
**Source:** ft-race-scanner, pass 2.

Free-threaded builds route **every `PyObject`** through mimalloc. CPython vendors mimalloc with
`MI_TRACK_ASAN` **commented out**, and it is never defined anywhere in the build system.
`PYTHONMALLOC=malloc` is **refused** under FT. Net effect: on `debug-ft-nojit-asan` and
`release-ft-nojit-asan`, ASan sees nothing on the object heap.

**Measured:** pass 2's Finding 3 is a **236-byte heap overflow** that those builds report as **nothing**.

**Fix:** add `-DMI_TRACK_ASAN=1`. **No CPython source change required.**

**⚠ Retrospective action required:** any prior slice that certified an overflow/UAF **negative** on an
`*-ft-*-asan` build must be re-checked. Combined with D-14, this run found two independent ways the
campaign's *dynamic verification* has been producing false negatives.

**✅ E1 CONFIRMED END TO END — tsan-stress-generator, pass 2. Verified at three levels.**
**Source:** `Objects/obmalloc.c:431-435` hardcodes `PYOBJ_ALLOC MIMALLOC_OBJALLOC` under
`Py_GIL_DISABLED`; `MI_TRACK_ASAN` is commented out at `mimalloc/types.h:88` — 6 tree-wide hits, all
consumers, **zero definition sites**; `configure.ac:3488` adds only `-fsanitize=address`. With
`MI_TRACK_ASAN` undefined, `track.h` falls to `#else // no tracking` and the `ASAN_POISON`/`UNPOISON`
calls are never compiled. `PYTHONMALLOC=malloc` refusal confirmed at runtime.

**DAMAGE IS BOUNDED — and the bound is good news.** The GIL ASan builds have `WITH_PYMALLOC=0` **and**
`Py_GIL_DISABLED=0`, so they fall through to plain `MALLOC_ALLOC`. **Every `*-gil-*-asan` result in the
campaign is trustworthy.** Zero catalog records rest on an FT-ASan clean; 37 GIL-ASan records unaffected;
zero false positives were manufactured.

**E1 UNDERSTATES THE HAZARD — stock FT-ASan does NOT stay silent.** It emits a symbolized 24-frame
report naming `Objects/dictobject.c:7778` — **the victim, not the writer**. A reviewer would file it in
the wrong slice. Silence would be safer than this.

| build | outcome | overflow attributed |
|---|---|---|
| `release-ft-nojit` | 3/3 SIGSEGV | — |
| `release-gil-nojit` | 3/3 clean (~20M instantiations/run) | — |
| `release-ft-nojit-asan` | ASan `SEGV on unknown address` in `clear_inline_values` | **0/3** |
| `debug-ft-nojit-asan` | CPython's own debug allocator: `bad trailing pad byte` | **0/2** |
| mitrack (`-DMI_TRACK_ASAN=1`) | `use-after-poison WRITE of size 232`, stack `InitInlineValues ← AllocNoTrack:2551` | **3/3 exact** |

**✅ FIX NEEDS NO SOURCE CHANGE — my earlier "2-line `obmalloc.c` patch" note was WRONG and is retracted.**
The pre-existing mitrack build happened to carry a patch, so it did not prove the claim. The agent
**reverted the patch, rebuilt, confirmed the tree is clean and `PYTHONMALLOC=malloc` is refused again** —
and it still produces the exact report, **3/3**. `-DMI_TRACK_ASAN=1` alone is sufficient.
**Not verified:** the debug-FT variant, false-positive cost over a full campaign, and whether true
`heap-buffer-overflow` (as opposed to `use-after-poison`) reports still need a patch.
**Upstream:** `MI_TRACK_ASAN` returns **zero** CPython tracker hits — this is unreported.

**Retrospective: exactly ONE load-bearing negative.** Pass-1's `__slots__` allocation-arithmetic bounded
negative (`obj-typeobject/agents/memory-pattern-analyzer.md:189-227` → `SUMMARY.md:166-172` → CPY-0088)
certifies absence of precisely the overflow class FT-ASan cannot see, and counts `debug-ft-nojit-asan` as
evidence. It does **not** collapse — the GIL arm ran too and the argument is structural — so **downgrade
the claim, do not retract it.**

**(superseded) VERIFICATION IN PROGRESS — E1 CONFIRMED by construction.** A patched build
`~/projects/python_build_matrix/builds/release-ft-nojit-asan-mitrack/` now exists and **does** report the
overflow the stock `*-ft-*-asan` builds miss: heap-buffer-overflow **WRITE** at
`_PyObject_InitInlineValues` → `_PyType_AllocNoTrack:2551`, **3/3**. Companion measurement: **20/20 FT
crash, 10/10 GIL clean**.
**Correction to the fix description:** it is **not** purely `-DMI_TRACK_ASAN=1` — the working build also
carries a **2-line `obmalloc.c` patch**. State it that way.
**⚠ Build-matrix housekeeping:** `release-ft-nojit-asan-mitrack` is **not in `builds.csv`** and its patch
is **not in `patches/main/`**. If it is to become a permanent matrix entry, add the row and move the
patch. Do not delete the directory while pass-2 reproducers are running against it.

## D-15 `scan_ft_races` T4 — "just constructed" inferred from the API NAME, not from allocation

**Source:** ft-race-scanner, pass 2. **Both** NET NEW T4 candidates (`:2007`, `:6620`) are false positives.
The rule infers "this object was just constructed" from the *name* of a publishing API without checking
the object was **allocated in this frame**. In both cases the flagged object is a bare **function
parameter** that arrived fully live: `:2007`'s store is inside a `types_stop_world()` bracket (invisible
because the wrapper is a function, not a macro — see D-2), and `:6620`'s "write" is `tp_version_tag`
three lines below a `BEGIN_TYPE_LOCK()`.
**Fix:** one added condition — require an in-frame allocation for the published object.
**Measured effect:** T4 precision on this file goes **1/3 → 1/1**.

## D-16 the "same value read twice, the two reads must disagree" shape is a CLASS, not a `_struct` quirk

**Source:** ft-race-scanner, pass 2. Its highest-value proposal.
Catalogued as CPY-0044 / 0046 / 0049 / 0052 — **all four in `Modules/_struct.c`**, which is why it has
read as a module-specific quirk. Pass 2 gives it instances in **`typeobject.c` and `dictobject.c`**, and
**one of them is a heap overflow with no free-threading involved at all**.
**Owner:** `scan_memory_patterns`, not `scan_ft_races`.

## Confirmed defects

### D-1 `check_pep7.py:299-312` — fixed 2-line lookahead breaks on multi-line conditions
**Source:** pep7-style-checker, pass 2.
**Shape:** the `missing-braces` rule looks ahead a fixed 2 lines from the matched control line, assuming
the condition ends there. When a condition spans multiple lines *and* the brace is on its own line —
this file's deliberate ~60-site Allman sub-convention for multi-line conditions — the first
continuation line neither starts nor ends with `{`, so the rule fires despite the block being braced.
**Measured:** precision 149/153 = 97.4% on typeobject.c. All 4 FPs share this shape
(L1676→1679, L5449→5451, L7655→7657, L7660→7662).
**Fix:** track paren balance to the true end of the condition before the brace test. This also removes
the arbitrary 2-line cap, which independently mis-fires on any 3+-line condition.
**Why it matters more over time:** the FP sites skew 2024–2026. Multi-line Allman conditions are how
*new* code in this file is written, so the defect's cost grows.

### D-11 `scan_refcounts` — THE headline toolkit result of pass 2: precision 33%, **recall 0 of 6**
**Source:** refcount-auditor, pass 2, measured on this file.
**Cumulative:** `scan_refcounts` has now reported **0 of the 4 ASan-confirmed UAFs** found by *reading*
this file. Pass 2's two net-new UAFs (P2-F1, P2-F5) were both invisible to it.
**Root cause, and it is a good one:** the rule's accessor seed (`lookup_tp_dict` / `lookup_tp_mro` /
`lookup_tp_bases` / `lookup_tp_subclasses`) is keyed on a **naming convention introduced by a 2023
encapsulation refactor (`f73abf8e03fd`, gh-94673) that was never a lifetime audit**. Worse, `tp_dict` —
the field that refactor mostly touched, and therefore the field the rule sees best — is the one of the
four that **cannot be swapped from Python at all**: `type_getsets[:2384]` registers `__dict__` with a
**NULL setter**. The rule is best-calibrated on the only safe field.
**Proposed fix W1 (finds BOTH new FIXes on its own):** treat `X->field` as a borrowed load **iff** the
file contains a re-binding writer for that member name (`Py_XSETREF` / `Py_SETREF` / `Py_CLEAR` /
save-old-then-store). **The writer is the precision gate** — that is what makes it cheap rather than
noisy. W2–W4 in the agent's report.
**Supersedes D-3 and D-9**, which described this gap from two narrower angles.
**Mechanical FP defects also found (4 of the 18):** an anchoring call dominated by a `return`; an
anchoring call and its use in opposite `if`/`else` arms.

### D-12 `PASS2_BASELINE.md` joins the catalog on LINE, not on `(file, function, rule)`
**Source:** refcount-auditor, pass 2.
`:793` was reported as NET NEW but is **already recorded as CPY-0083**, which cites it at its *load*
line `783`. The baseline's recorded-vs-net-new split is therefore approximate. Also:
`RUN_CONTEXT.md` says "13" where the baseline table and the JSON both say **14**.
**Consequence:** the "18 NET NEW" headline is really 17. Fix the join key before the next slice reuses
this tooling.

### D-10 `tree_sitter_utils._FUNCTION_MACROS` cannot express a token-pasted function name (recall)
**Source:** macro-hygiene-reviewer, pass 2, verified by direct probe (`prefix 'richcmp_': 0 seen`).
**Shape:** `_FUNCTION_MACROS` is a `{name: arg_index}` dict, so it can name a macro that *wraps* a
function but cannot express a macro that *generates* a function name by token pasting. The 6
`RICHCMP_WRAPPER`-generated `richcmp_*` functions in `typeobject.c` are therefore invisible to
`extract_functions()` — they are not in any scanner's `functions_analyzed` denominator.
**Generalises:** `UNSIGNED_INT_CONVERTER` in `longobject.c` (9 fns), `COMPLEX_BINOP` in
`complexobject.c` (4 fns) — **19 functions tree-wide** currently invisible to every C scanner.
**UPSTREAM WARNING:** `tree_sitter_utils.py` lives in **cext-review-toolkit** (the C chassis is in
exactly 3 repos: cext → ft → cpython). Fix there and sync forward. Do NOT fork.

### D-2 `scan_stw_safety` — recall gap from an unresolved file-local wrapper
**CORRECTED by macro-hygiene-reviewer, pass 2 — twice, and both corrections matter:**
1. **The gap is worse than "3 of 11" (73%).** `stw_functions=3` counts the wrapper *definition* itself,
   so only **2 of 11** real regions are seen — **18% recall**, not 27%.
2. **`resolve_local_lock_macros` CANNOT fix this**, contrary to what the preflight and my own first
   framing assumed. In the FT build `types_stop_world` is a static **function**, not a macro; in the GIL
   build it is a `#define` with an **empty** body, which the resolver explicitly skips. The needed
   primitive is **trivial-static-wrapper resolution** — a different mechanism.

### D-2 (original framing, retained for the record)
**Source:** include-graph-mapper (preflight), pass 2. Assigned to stw-safety-checker to confirm + fix.
**Shape:** the rule keys on the literal `_PyEval_StopTheWorld` token. `Objects/typeobject.c` has **11**
stop-the-world regions; **9** go through the file-local wrapper `types_stop_world()` at `:124`.
The scanner reports `stw_functions=3` — the wrapper itself plus the 2 raw callers
(`type_set_name:1572`, `type_set_qualname:1598`).
**Precedent for the fix:** issue #28's blindness-fix A (`resolve_local_lock_macros`) took
`scan_lock_discipline` from 2 → 23 critical-section functions on this same file. Same class of fix.
**Status:** structural zero confirmed by hand at preflight; awaiting the agent's scanner proposal and a
tree-wide recall estimate.

### D-3 `scan_refcounts.borrowed_field_deref_across_call` — keyed on accessors, blind to plain field reads
**Source:** include-graph-mapper (preflight), pass 2. Assigned to refcount-auditor to confirm + widen.
**Shape:** the rule keys on the four `lookup_tp_*` accessor functions (`borrowed_field_accessors=4`), so
a **plain struct field read** of the same borrowed pointer is out of scope entirely.
**Concrete misses handed to the agent:**
- `super_getattro:12699` passes three borrowed struct fields (`su->type`, `su->obj`, `su->obj_type`)
  into a call that runs Python, while `super_init_impl:12950-12952` can `Py_XSETREF` all three
  **in place** on an already-live object.
- Same shape at `_Py_type_getattro_stackref:6574`.
**Status:** awaiting the agent's verdict and rule-widening proposal.

### D-4 `scan_ft_races` — `lock_held_functions: 0` on a file with 23 lock-taking functions
**Source:** c-complexity-analyzer, pass 2 (independent count), corroborating `scan_lock_discipline`'s
own `critical_section_functions=23`.
**Shape:** `scan_ft_races` reports `lock_held_functions: 0` for `Objects/typeobject.c`, while
`scan_lock_discipline` — on the same file, after issue #28's `resolve_local_lock_macros` fix — resolves
**23**. So the FT scanner is not consuming the lock vocabulary the lock scanner already knows how to
resolve. Every FT rule that asks "is this access guarded?" is therefore answering from an empty set.
**Consequence measured:** two of the three highest-value pass-2 functions are scanner-silent because of
it — `type_setattro:6749` (7 fixes/3y, 2 in the last 12 months, the only such function in pass 2) and
`_PyType_LookupStackRefAndVersion:6305` (10 lifetime fixes, the most-fixed function in pass 2).
**Fix:** share the `resolve_local_lock_macros` resolver between `scan_lock_discipline` and
`scan_ft_races` (it belongs in `scan_common`, not in one scanner).

### D-5 no rule can see unnarrowed exception *replacement*
**Source:** pyerr-clear-auditor, pass 2.
**Shape:** a live exception is destroyed not by `PyErr_Clear()` but by an unconditional
`PyErr_Format(...)` / `PyErr_SetString(...)` that overwrites it. No `PyErr_Clear` token, balanced
fetch/restore, not in a destructor — invisible to every current rule, yet identical harm.
**Evidence it is recurrent, not a one-off:** it is the shape of the already-recorded **CPY-0076** and
**CPY-0078** in this same file, and it is where pass 2's only net-new FIX came from
(`type_update_dict:6714`). Currently found only by hand.
**Proposed rule `unnarrowed_exception_replacement`:** reuse rule 3's branch machinery — an error-setting
call on a failure branch that is not dominated by a `PyErr_ExceptionMatches` test. On this slice it
yields exactly one tree-wide hit, with its guarded twin one file over (`Objects/object.c:2104`).
**Still outstanding from pass 1:** the `total_pyerr_clear_calls_unattributed` canary, and the
restore-over-a-live-exception rule.

### D-6 `scan_init_bypass` — field names not type-qualified through a LOCAL (precision)
**Source:** init-bypass-checker, pass 2. Precision on this file: `one_hop_param_deref` **1/2**.
**Shape:** `_resolve_target_field` rejects only non-receiver *parameters*. When the field is reached
through a **local** of a third struct type, the name collides and the rule fires. Live FP:
`:3894 apply_slot_updates` — `item->type` is `slot_update_item_t::type` (declared at
`Objects/typeobject.c:3809-3813`), not `superobject::type`. It is written at exactly one site
(`queue_slot_update:3875`) from `update_one_slot:12051`, where the type is dereferenced dozens of times
and is never NULL. The whole block is `#ifdef Py_GIL_DISABLED` (3789–3929) — on the default build the
function does not exist at all.
**Fix:** capture the `tp_init` receiver's declared struct type and match it at the read site.
**Regression fixture:** `apply_slot_updates` (negative) + `super_descr_get` (positive).

### D-7 `scan_init_bypass._INHERITED_NEW` — `tp_new = 0` wrongly treated as bypassable (precision)
**Source:** init-bypass-checker, pass 2.
**Shape:** for a **static** `PyTypeObject`, `tp_new = 0` is treated as "inherits `object.__new__`, so
`T.__new__(T)` is reachable". Wrong when `tp_base` is 0/`object`: `type_ready_set_new:9428-9437`
auto-applies `Py_TPFLAGS_DISALLOW_INSTANTIATION` in exactly that case. Tree-wide over-report generator.
**Negative-control fixture:** `_PyBufferWrapper_Type` (`__flags__ == 0x5182`; both `BW()` and
`BW.__new__(BW)` raise `TypeError`).

### D-8 `scan_init_bypass` — nullable fields discovered only via `tp_init` NAME matching (recall, fragile)
**Source:** init-bypass-checker, pass 2.
**Shape:** `new_bypass` fields are collected only from a function *named* like the `tp_init`.
`super_init` assigns nothing — the fields are set in `super_init_impl`, which is matched **only by the
Argument Clinic `_impl` naming coincidence**. Rename that function and this file's
`total_nullable_fields` drops 3 → 0, silently taking **CPY-0007 and CPY-0080 with it**.
**Fix:** follow one hop out of the `tp_init` body into a same-file `static` callee that receives the
receiver — this is the index `unguarded_param_sinks` already builds, run in reverse.

### D-9 no rule models a borrowed OWNING FIELD replaced by a re-entrant `tp_init` (recall)
**Source:** init-bypass-checker, pass 2. **This gap cost the run's headline FIX** — P2-F1
(`do_super_lookup:12656`, heap-UAF reproduced on 4 builds) was found by reading, by an agent whose own
bug class it is not.
**Shape:** a borrowed read of an owning struct field, used across a call that can reach Python, where a
re-entrant `tp_init` on the *same object* can `Py_XSETREF` that field. Distinct from
`borrowed_field_deref_across_call` (D-3), which keys on the `lookup_tp_*` accessors and models the
type-object fields, not arbitrary owning fields on a mutable instance.
**Evidence it is recurrent:** **seven** confirmed catalog members — CPY-0044/0045/0046/0048/0049/0050
(`_struct`) plus P2-F1, now in a second file.
**Owner:** `scan_refcounts`, not `scan_init_bypass`.

## Recall WINS to record (rules that measurably improved)

- **`scan_pyerr_clear` recovered the `SLOT0`-macro region.** `total_pyerr_clear_calls` 9 → **11**,
  `functions_analyzed` 427 → **459**, from issue #28 / PR #29. The two recovered clears live in the
  user-Python-dispatching slot functions — exactly the high-risk ones. This is a measured recall gain,
  not an assumed one.

## Cross-cutting fact for all reproduction work

**`Objects/typeobject.c` is byte-identical between the target `4f3be1b5777` and the build matrix's
`a1d580430c8`** — 52 commits apart, none touching this file. Verified by c-complexity-analyzer.
Every reproducer run on the build matrix therefore exercises exactly the reviewed source; no
diff-and-caveat needed for this file.

## Corrected records (not defects — bad prior conclusions)

### C-1 `scan_gil_usage`'s zero on typeobject.c is N/A, not a blind spot
Carried for a full session as "the one genuine blind spot left on this file". The preflight measured it:
the rule's vocabulary is `Py_BEGIN_ALLOW_THREADS` / `PyGILState_*`, and `grep -c` on this file returns
**0**. The file never releases the GIL that way. Correct verdict is `rule_not_applicable`, not "clean"
and not "blind spot". Memory `cpython-review-plan` must be updated at synthesis.

### C-2 `scan_lock_discipline`'s zero on typeobject.c is EARNED
`vocabulary_resolved=66`, `critical_section_functions=23`, `mutex_functions=3`. The preflight
independently confirms the rule works here — this file is the motivating example in the scanner's own
source comments. A real zero on a real denominator.
