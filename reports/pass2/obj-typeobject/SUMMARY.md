# obj-typeobject — PASS 2 SUMMARY

**Slice:** `Objects/typeobject.c`, 13,068 lines, single file.
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` — **byte-identical** to the build-matrix ref
`a1d580430c8` (52 commits apart, none touching this file), verified independently by three agents.
**Agents dispatched:** 19 — 2 preflight, 16 static, 3 dynamic/temporal (one overlaps).
**Clone:** not shallow — 132,320 commits, 1,282 touching this file. No cap, no timeout.

Pass 1 covered slot wiring / `tp_new` / `tp_init` / `__new__` bypass → CPY-0068..0090.
Pass 2 owed two orthogonal things and did both: the **issue-#28 re-run** (rules that landed after pass 1,
several built from its misses) and the **unread regions** (MRO C3, lookup cache, getattro/setattro,
`__class__` assignment, pickle, watchers, managed static types, `super` beyond construction).

---

## Headline

**24 FIX findings, 19 of them reproduced** — 6 ASan-confirmed heap-use-after-free / overflow, 2
reproduced deadlocks, several SIGSEGV/SIGABRT with pure-Python triggers of 12–40 lines.

**Almost none came from a scanner.** The scanners' net-new contribution was **1 live finding**
(`:9403`). Everything else came from *reading* the unread regions — repeatedly by agents working
**outside their own bug class**. That is the single most important calibration result of this run.

**Three findings converged from three independent lanes.** `__class__` assignment's stopped-world
Python execution was reached separately by null-safety, memory-pattern and stw-safety; the deadlock by
lock-discipline and ft-race. Convergence is the evidence, not the count.

**Two campaign-level defects were found in our own dynamic verification, both producing false
negatives** — and both are now fixed or bounded. See "Toolkit" below.

---

## FIX findings (24)

### The `__class__` assignment cluster — one ~20-line script drives it
| id | site | evidence |
|---|---|---|
| **P2-F3** | `same_slots_added:7609` runs user Python **inside the stopped world** | SIGABRT debug-ft; **hang on release-ft at both `PYTHON_GIL=0` and `=1`**; 4 independent triggers incl. bare `gc.collect()` |
| **P2-F4** | `object_set_class:7825` stale `oldto` → over-DECREF of a type | ASan UAF; SIGSEGV release-gil after 2 iterations; **reproduces single-threaded on the default GIL build** |

**P2-F3 overturns a pass-1 ACCEPTABLE on one word:** `valid_identifier` uses `PyUnicode_Check`, not
`CheckExact`, so a `str` subclass survives into `ht_slots`.
**P2-F4's guarded twin is literally the three lines gh-120198 (`3bfc9c831ad9`, 2024-07-11) DELETED** — an
explicit `oldto = Py_TYPE(self)` re-fetch, with a comment saying why.

### Borrowed-reference UAFs — the dominant class
| id | site | evidence |
|---|---|---|
| **P2-F1** | `do_super_lookup:12656` — borrowed `su_*` freed by re-entrant `super.__init__` | ASan UAF; **SIGSEGV on all four configs**; reproduces on **released 3.14.4** → longstanding. Twin 9 lines away pins `mro` only |
| **P2-F5** | `type_mro_modified:1299` — borrowed MRO freed by re-entrant `__bases__` | ASan UAF, ~40 lines pure Python |
| **P2-F9** | `_PyType_Modified_Unlocked:1195` — live `PyDict_Next` cursor, dict freed under it | ASan **3/3 deterministic**, GIL build. Two in-file twins carry justifying comments; **the buggy loop carries none and is the only one that runs user code** |
| **P2-F20** | `recurse_down_subclasses:12386` — `PyDict_Contains` → user `__eq__` frees the iterated dict | verified live; **gh-30775, which proposed the fix, was closed UNMERGED** |
| **P2-F13** | `set_mro_error:3320` — UAF from a **plain `class` statement**, freed one line down | re-verified |

### Concurrency
| id | site | evidence |
|---|---|---|
| **P2-F6** | `insert_split_key` → `_PyType_Modified_Unlocked` runs Python under a non-reentrant `DONT_DETACH` mutex | **24/24 FT deadlock, 0/12 GIL**, gdb-proven. **`dictobject.c:218-227` forbids exactly this, naming `PyType_Modified`.** 4th iteration of a path reverted once (gh-152238); 3 further consequences on the **GIL** build |
| **P2-F21** | `_Py_dict_lookup:1385` — P2-F6's unswept sibling; only lock-order inversion found tree-wide | prohibiting comment written **26 months after** the violating code |
| **P2-F17** | `type_ready` publishes at `:9581`, sets `Py_TPFLAGS_INLINE_VALUES` at `:9487`; alloc reads it to size *and* init | ASan 3/3 (mitrack), **20/20 FT crash from `class Sub(Base)`**, 0/10 GIL. **gh-151394 does not fix it** |
| **P2-F18** | `PyType_Unwatch:1129` bare write vs `:1112` locked, 17 lines apart | 8 races/2 pairs incl. **`:1129` racing itself**; observable **lost update with no sanitizer** (8/12000 FT, 0/8000 GIL) |
| **P2-F19** | `type_ready`'s three flag writes are neither STW nor pre-publication | static; **reverses pass 1's dismissal** — the sanction pass 1 quoted is conditional |

### Error paths
| id | site | evidence |
|---|---|---|
| **P2-F2** | `type_update_dict:6714` — unnarrowed `AttributeError` **destroys MemoryError** | OOM-reproduced on 4 builds; instance path returns `MemoryError` correctly at the same index. Only unguarded one of 11 tree-wide sites |
| **P2-F10** | `update_slot_after_setattr:6744` + `update_all_slots:12177` discard `-1` in the `#else` arm | SIGABRT debug-gil; `SystemError` **misattributed to `TextIOWrapper.write`** on release-gil |
| **P2-F11** | `type_set_bases_unlocked` `bail:` is a **partial rollback** | a *failed* `__bases__` assignment leaves `__bases__`/`__mro__` inconsistent |
| **P2-F12** | `PyType_Watch:1111` discards `assign_version_tag` → **watcher permanently silent** | 3 lines of Python |
| **P2-F15** | `find_name_in_mro` returns `-1` with nothing set; caller converts to **success with a wrong answer** | composite of CPY-0076/0077 |
| **P2-F14** | `object_getstate_default:7990` — SIGSEGV, `pickle.dumps` racing `list.append` | re-verified |

### Recursion / allocation
| id | site | evidence |
|---|---|---|
| **P2-F7** | `solid_base:3776` unguarded self-recursion; trigger `class X(Deep): pass` | exit 139 **debug and release**. Pass 1 dismissed it on an assumed tail call that **`objdump` disproves** |
| **P2-F8** | CPY-0087 widened — `_PyType_Modified_Unlocked:1206` trigger drops to `obj.newattr = value` | exit 139 on 3 builds; control isolates the crash |
| **P2-F16** | `mro_implementation_unlocked:3503` — CPY-0014 reached by `class C(A,B)` and `__bases__ =` | 5/5 FT, both routes; both GIL builds clean |
| **P2-F22-24** | siblings outside this slice: tracemalloc `TABLES_LOCK`, `HEAD_LOCK`, 4 in `Objects/object.c` | **gh-84139 was closed `not_planned` for lack of a repro — the ask is a repro** |

### ⚠ Filing interactions — do not file these independently
1. **P2-F11 gates CPY-0070.** Fixing CPY-0070 naively makes P2-F11 reachable on the GIL build too.
2. **CPY-0070's cited guarded twin (`:1967`) is dead code on the default build** (P2-F10) — the record's
   claim holds only for the FT arm.
3. **CPY-0069 is not a regression of gh-30775** — that PR was closed **unmerged**. Reframe.
4. **The P2-F3 fix direction does not, on its own, close the third defect on the same `RichCompareBool`.**
5. **P2-F18 cannot be fixed by making the two writers consistent** — `:1129` races *itself*.

---

## CONSIDER (7)
`get_base_by_token_recursive:6002` (overturns pass 1's ACCEPTABLE — the `tp_mro == NULL` precondition is
*advertised* and `type_clear` manufactures it); `_PyType_SetFlags:6453` (gh-148450 sibling — invariant
break reproduced, harm latent); `type_set_abstractmethods:1745` (omits `type_lock_prevent_release`,
two-line fix); `managed_static_type_state_get:294` (off-by-one, no Python route); `init_static_type`
failure path; `_PyCStackRef` migration incomplete (9 candidates, 4 net new); `type_dealloc` never clears
`type_version_cache`.

## POLICY (3)
`PyType_FromSpec` soft-deprecated in 3.16 with **0 of 173** internal sites migrated; the new slot API's
deprecations enforced **only against the new API** (8/8 warn vs 0/8); `Py_TPFLAGS_HAVE_FINALIZE` in
`posixmodule.c`.

---

## Bounded negatives — stated because a cold run never does

- **Lookup cache: clean, with reasons** — version tags never reused (both counters saturate), install
  writes version last, teardown writes version first. Densest crash-fix region in the file.
- **Lock discipline: certified zero** — all 24 critical-section regions + 3 `PyMutex` pairs hand-checked,
  zero `return`/`goto` between acquire and release. Was **2** resolved in pass 1, now **23**.
- **NULL safety: earned zero** — real denominator **115** (not the envelope's 1,432); 0 hits in **201/201**
  pass-2 sites hand-checked; widened-alphabet probe yields 0 net-new.
- **PyErr_Clear: 0 of 11** destructor-family, **0 of 11** success-path.
- **PEP 7: 0 confirmed** across 5 bug-adjacent shapes, 149 braceless bodies walked.
- **`super` field enumeration complete** — 23 reads; only the recorded `:12793`/`:12797` unguarded.
- **`allocator_model` (issue #28's H-1 decision) UPHELD first-hand** — the memset covers the whole tail,
  padding and inline-values block; the one out-of-bounds combination is rejected at
  `type_ready_preheader`. H-1 was not right, not even narrowly.
- **`lookup_tp_*` migration complete** — 79/84 (94%); the remainder is gh-105020's deliberate revert.
- **`scan_gil_usage`'s zero is `rule_not_applicable`**, not a blind spot: `grep -c` = 0, no file-local
  macro wrapping the vocabulary.

---

## Toolkit — 17 defects; see `TOOLKIT_DEFECTS.md`

**⭐ D-13 is the unifying one: three scanners blind for one reason.** `scan_refcounts` (recall **0 of 6**),
`scan_recursion_guards` (6 of 7), `scan_null_checks` (hides 53 sites) all key on **accessor names**
rather than **member names**. One shared graph-field table in `scan_common` fixes all three; the
precision gate is *"only where the file contains a re-binding writer for that member"*.
The blindness has a cause worth recording: the accessor names came from a **2023 encapsulation refactor
that was never a lifetime audit**, and `tp_dict` — the field it mostly touched — is the one field with
**no Python-reachable writer at all**. The rules are best-calibrated exactly where nothing can go wrong.

**🔴 Two false-negative generators in dynamic verification:**
- **D-14 `run_oom_sweep.py` scored a raising setup as SAFE.** Four sweeps read 400/400 clean; one deleted
  line turned that into 13 aborts + 2 SIGSEGVs. **Fixed** — tests 796 → **812**, A/B proven. Cloned in
  **8 other harnesses** the fix does not reach, including 5 catalog `repro.py` files.
- **🔴 E1 `*-ft-*-asan` has no ASan shadow for the object heap.** Confirmed at source, build-config and
  demonstration level. **Worse than "silent":** it emits a 24-frame report naming the **victim**, so a
  reviewer files it in the wrong slice. **`-DMI_TRACK_ASAN=1` alone fixes it — no source change**
  (an earlier claim that a patch was needed was tested and retracted). **Damage bounded: every
  `*-gil-*-asan` result is trustworthy**; exactly one load-bearing negative (CPY-0088's arm) needs
  downgrading, not retracting. Unreported upstream — zero tracker hits.

**🟠 D-17, the third denominator defect:** a "clean" sweep's real denominator is
`iterations_run − completed`, often ~4. Three regions were certified clean over **4, 12 and 5** actual
failure points. D-14 makes a *broken* setup look clean; D-17 makes a *working* setup test almost nothing.

**Denominators were the story three times over:** `assignment_sites` 1432 → real **115**;
`allocation_sites` 1 → real **15**; OOM iterations 400 → real **4**.

**Measured recall wins:** `scan_pyerr_clear` recovered the `SLOT0` region (clears 9 → 11, functions
427 → 459); `scan_lock_discipline` 2 → 23 critical-section functions; the STW wrapper-resolution
prototype takes this file 27% → **100%** region recall (tree-wide 50 → 58 regions, 5 → 7
high-confidence, zero spurious detections).

**Baseline tooling defect (D-12):** `PASS2_BASELINE.md` joins the catalog on **line**, not on
`(file, function, rule)` — so "18 NET NEW" was really **17** (`:793` is CPY-0083 at its load line).

---

## Follow-ups

1. **File upstream.** 24 FIX, 19 reproduced, only 2 prior-art collisions in the whole run, **zero overlap
   with gh-151763 / gh-153852** (verified with specifics). Respect the 5 filing interactions above.
2. **Toolkit PRs.** D-13's shared graph-field table is the highest-value single change. D-14's fix is
   written but **uncommitted on `main`**. The STW wrapper resolver is prototyped and measured.
3. **Re-run under the fixed harness:** pass-1 uninit-dealloc's 1150-iteration verdict; modules-sample
   error-path's 4 clean sweeps; CPY-0079 (for D-17, not D-14).
4. **`obj-dictobject` slice inherits** P2-F6's three GIL-build sub-findings and P2-F21.
5. **Build matrix:** `release-ft-nojit-asan-mitrack` and `release-ft-jit-tsan` are not in `builds.csv`.
6. **The Part-3 TSan scenarios have since finished** and produced one net-new finding —
   **P2-F25, pending a GIL control**: `_PyType_GetSubclasses:793` walks `tp_subclasses` with a live
   `PyDict_Next` cursor while another thread replaces (`init_tp_subclasses:702`) or deletes from
   (`delitem_common:2987`) that dict. Confirmed by two independent scenarios.
   **⚠ This run cited that exact site as a GUARDED TWIN of P2-F9.** Its comment justifies the borrowed
   reference against **re-entrancy**, not against **concurrent mutation** — so under the second threat
   model the twin is itself a defect. **Method correction for informed-explore: a guarded twin is only a
   twin for the threat model its comment addresses.** Verify which one before citing it as the fix.
   Also run the GIL control and check gh-151377's coverage before recording it.

## Late addenda (after the body of this summary was written)

- **`type_cache_clear`: 68 races over three independent scenarios, always the same 3 pairs** — one per
  field, all plain-store vs atomic-load. Adding a contract-abiding writer introduced no fourth pair,
  isolating the defect to the three plain stores rather than the cache design.
- **P2-F18 sharpened:** both writers of the watcher bit are the tier-2 optimizer, and the *bare* one is
  `optimizer_analysis.c:156 type_watcher_callback`. **gh-141595's "disable JIT above one thread"
  mitigation is not in the reviewed tree.** The lost-SET consequence was **not** reproduced through the
  real optimizer despite an FT+tier-2+TSan build — recorded as a **driver limitation, not evidence of
  absence**.
- **Harness defect:** a partial TSan log is indistinguishable from a clean one. It misled this run
  **three times**; two scenarios read 0 races mid-run and finished at 29 and 22, and the third instance
  would have cost the second confirmation of P2-F25. Never conclude from a partial log.
