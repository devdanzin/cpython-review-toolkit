# cpython-review-toolkit — Improvement Plan (toward v0.5+)

> Status: proposal, 2026-07-24. Synthesized from four research streams: (1) a full
> inventory of the current toolkit, (2) mining the fusil findings repos
> (`cpython-oom-findings`, `cpython-tsan-findings`, `rustpython-findings`), (3) recent
> CPython `type-crash` tracker issues, (4) transferable techniques from the sibling
> toolkits (rustpy, cext, rust-ext, ft).
>
> **All file:line and issue anchors below were gathered on 2026-07-24 and should be
> re-confirmed at implementation time** — CPython moves and line numbers drift.

---

## 1. Where the toolkit stands today

`cpython-review-toolkit` is **v0.4.0** in its manifests but is the family's least-built-out
member. It is a purely-static, regex-based, single-pass reviewer:

- **11 agents, 8 scripts, 4 commands (`explore`/`map`/`hotspots`/`health`), 110 tests.**
- **No shared tree-sitter chassis.** Every script is stdlib-only regex; `find_cpython_root()`
  is copy-duplicated across all 8. There is no `tree_sitter_utils.py` / `scan_common.py`.
- **No `data/` directory.** Every table (deprecated APIs, refcount semantics, fallible-API
  list) is inlined as prose inside agent prompts — nothing is machine-readable or refreshable
  from a CPython tree.
- **3 of the 11 agents are script-less** (memory-pattern, macro-hygiene, api-deprecation are
  qualitative Grep-only, with no tests).

What it already has that we can build on:

- A **manual** dynamic-verification track: `reports/reproducers/` holds ~18 hand-authored
  Python reproducers (grouper reentrancy, ctypes overflow, zstd refleak, sentinel UAF, …) —
  evidence the analyze→reproduce loop already happens by hand, but **nothing in the plugin
  reads, writes, or regression-checks against them.**
- `docs/python-wrapper-new-without-init.md` — a complete design for an **unbuilt** init-bypass
  rule (currently routed *out of house* to cext/code-review).
- The user's own campaign has already produced upstream umbrella issues **#151763** (OOM: 31
  distinct finds), **#153852** (FT: 15 races), **#146102** (toolkit umbrella) — i.e. a proven
  supply of ground-truth bugs to calibrate against.

### Capability gaps vs. the siblings (the map of the opportunity)

| Capability | rustpy | cext | rust-ext | ft | **cpython** |
|---|:--:|:--:|:--:|:--:|:--:|
| Shared tree-sitter chassis | ✅ | ✅ | ✅ | ✅ | ❌ (regex only) |
| `data/` JSON tables | ✅ | ✅ | ✅ | ✅ | ❌ (prose-inlined) |
| Preflight orientation mapper | ✅ | ✅ | ✅ | — | ⚠️ include-graph only |
| Early history-context preflight | — | — | — | — | ❌ (history runs last only) |
| Informed-explore loop | ✅ | ✅ | ✅ | — | ❌ |
| known-issues regression command | ✅ | — | — | — | ❌ |
| Findings-repo feedback loop | ✅ | ✅ | ✅ | — | ❌ |
| Reproducer / dynamic harness | — | ✅ (doc) | — | ✅ | ❌ (manual only) |
| Reachability-tiering (S/N control) | ✅ | ⚠️ | ⚠️ | — | ❌ |
| Free-threading / TSan detection | ⚠️ | — | — | ✅ | ❌ |
| Recursion-guard detection | ✅ | — | ✅ | — | ❌ |
| C-vs-Python parity | — | ✅ | ✅ | — | ❌ |

---

## 2. The core insight

The toolkit's **own fuzzing siblings and the CPython tracker are a ready-made, high-signal
catalog of exactly the bug classes a static reviewer should catch — and most are uncovered.**
Three research streams independently converged on the same short list of classes. The plan has
three levers:

- **A. New detectors** for the uncovered classes (recursion-guard, FT races, uninit-dealloc,
  exception-clobber-in-dealloc, OOM-alloc, init-bypass).
- **B. Loop-closing machinery** — informed-explore, known-issues, a reproducer harness, and a
  findings-repo tie-in — so the fuzz→static→reproduce→regression cycle is a *loop*, not a
  one-shot scan.
- **C. Infrastructure** — a chassis decision, a `data/` layer, and reachability-tiering — to
  keep signal/noise usable at CPython scale (the candidate count is inherently huge).

---

## 3. Tier 1 — New detection capabilities (biggest coverage gaps)

Ranked by (value × static-detectability × how uncovered it is). Every row is corroborated by
≥2 of the research streams.

| # | New/changed scanner + agent | Class | Concrete static signal | Evidence anchors | Port from |
|---|---|---|---|---|---|
| **1.1** | **`scan_recursion_guards.py`** + `recursion-guard-auditor` (NEW) | Unguarded native recursion in a recursive `tp_` slot → C-stack SIGSEGV | A fn wired to `tp_hash`/`tp_richcompare`/`tp_repr`/`tp_traverse` (or named `*_hash`/`*_repr`/`make_parameters`) that self-recurses or descends into `PyObject_Hash`/`Repr`/`GET_ITEM` with **no** `Py_EnterRecursiveCall`/`Py_ReprEnter` in its text | #154318 (`tuple_hash`,`frozendict_hash` — noted *copy-pasted* guardless), #154275 (`_Py_make_parameters`), #149146 (`tuple_dealloc`) | rustpy `recursion-guard-auditor`, rust-ext recursion agent |
| **1.2** | **`scan_ft_races.py`** + `ft-race-scanner` (NEW) — the richest single seam | Free-threading data races in CPython's own C | Three sub-rules: **T3** iterator-exhaustion double-DECREF (`it->seq=NULL; Py_DECREF(seq)` outside a critical section — fixed template, ~zero FP); **T2** lazy-init without CS (`if(!self->f) self->f=…` with no `Py_BEGIN_CRITICAL_SECTION`); **T1** atomic/plain access asymmetry (same `x->field` via `FT_ATOMIC_*` at one site, plain at another) | T3: #154130, #144357, #153296, #154535; T2: #154527; T1: `itertoolsmodule.c` count_repr, #153852 cluster | ft `lock-discipline-checker`, `atomic-candidate-finder`; new intra-file field-access map |
| **1.3** | **Extend `scan_refcounts.py` `check_new_member_init`** → `scan_uninit_dealloc.py` scope | Dealloc/clear of a half-constructed payload | Non-zeroing `*_New` (`PyObject_New`/`PyObject_GC_New`) + fields set only *after* a fallible call + an early error-return that decrefs; or `tp_clear`/`tp_dealloc` reading fields / `switch`ing an enum with `Py_UNREACHABLE()` default the ctor sets late | #151815 (`template_iter`), #152851 (`blake2 .copy()`), OOM-0024/0043, RUSTPY-0008 | own existing `check_new_member_init` (already the closest thing) |
| **1.4** | **`scan_pyerr_clear_in_dealloc.py`** + agent (NEW) | Exception-clobber in destructors | `PyErr_Clear()` (or a dropped fallible call) inside `*_dealloc`/`*_clear`/`tp_finalize`/`*_traverse` **without** a surrounding `PyErr_Fetch`/`Restore` (or `PyErr_GetRaisedException`/`SetRaisedException`) save/restore | #152083 (`context_tp_dealloc`), `typeobject.c:2719`, `_collectionsmodule.c` `deque_clear` (OOM-0039) | **direct port of cext `pyerr-clear-auditor`**, scoped to destructor family |
| **1.5** | **`scan_init_bypass.py`** + agent (NEW) — *design already exists in-repo* | Init-bypass / deletable slot → NULL field a C slot derefs | A slot reads `self->field` → `Py_INCREF`/`PyObject_Call*`/deref with no NULL guard, where the type has no `tp_new`/`DISALLOW_INSTANTIATION`, or the field is a settable/deletable member | #152954 (`sqlite3.Connection.__new__`), #152817 (`del cursor.row_factory`) | **build the already-written `docs/python-wrapper-new-without-init.md`** for the C side |
| **1.6** | **Promote `memory-pattern-analyzer`** from script-less agent → real scanner `scan_memory_patterns.py` | (a) Integer overflow in alloc size; (b) GC-track invariant | (a) `PyMem_*`/`malloc(n * size)` where `n` is a `Py_ssize_t` from Python args with no `n > MAX/size` guard / no safe-multiply helper; (b) `PyObject_GC_UnTrack` reachable on a `tp_dealloc` error path where `GC_Track` may not have run | (a) #3493/#1779, RUSTPY huge-alloc; (b) #152107 (OOM-0006), OOM-0017 | own agent prose (currently un-automated) |
| **1.7** | **Widen `scan_error_paths.py` / `scan_null_checks.py`** (tables + flow) | O1/O2/F fallible-return + cross-statement null | Add missing constructors to the fallible-API table (`PyStructSequence_New`, `PyUnicode_AsUTF8`, `_PyUnicode_FromUCS4`, import.c hashtable helpers); add **store-then-deref** (alloc stored into a field/local, deref'd on a distant later path); add **pending-exception desync** (`res!=NULL` trusted while `PyErr_Occurred()`) | #154385, #153800, #152682; #151968 (O2), #151126 (missing `PyErr_NoMemory`) | own scanners (widen tables + light error-state model) |
| **1.8** | **Strengthen `scan_refcounts.py` borrowed-across-call set** (crown jewel) | Borrowed ref used across a call that can free it | Widen the "call that can run Python" list to include converter callbacks (`PyUnicode_FSConverter`, `PyObject_GetIter`), `PyObject_Repr`/`Str`, warnings, and state mutators (`setcontext`-style); flag borrowed loads from **slots**, not just `*_GET_ITEM` macros | #148382 (`_decimal CURRENT_CONTEXT`), #151403 (`_posixsubprocess __fspath__`), #154527 (FT variant) | own scanner (this is the family's crown-jewel shape) |

Cheapest first wins in this tier: **1.1 (recursion-guard)** and **1.4 (pyerr-clear-in-dealloc)** —
both have a ready sibling implementation to port, crisp signals, near-zero-FP, and ≥2 live 2026
tracker exemplars usable directly as regression fixtures. **1.2 (FT races)** is the highest-yield
but gated on the chassis decision (§5).

---

## 4. Tier 2 — Loop-closing workflow machinery

These are chassis-light and wire straight into the fuzzing findings repos. Highest value/cost
ratio in the whole plan.

### 2.1 Informed-explore loop
Port `rust-ext-review-toolkit/.../scripts/build_informed_briefing.py` + `commands/informed-explore.md`.
The generator emits a Markdown briefing from three catalogs:
- **`data/cpython_bug_shapes.json`** — reusable bug *shapes* (not file:line), each with a
  `guarded_twin` (the correct sibling = the fix), a `hunt` directive (search siblings), severity,
  `confirmed_examples`, and a `differential`. Seed from the classes in Tier 1 (borrowed-across-call,
  return-NULL-no-exc, PyErr_Clear-in-dealloc, unguarded recursion, uninit-dealloc, alloc-overflow).
- **`data/cpython_non_bugs.md`** — the FP taxonomy (e.g. `PyErr_Clear` after `PyDict_GetItem`-with-
  sentinel is idiomatic; a borrowed ref held under a known-live owner is fine). The toolkit currently
  has **zero** codified FP suppression — worth adding even if nothing else here lands.
- **`--catalog-dir`** → reads `reports/*/meta.json` from `cpython-oom-findings` / `cpython-tsan-findings`.

Every agent then gets three rules: confirm-don't-relitigate known findings, skip-or-justify known-FP
classes, and hunt siblings via the guarded twin. Per-*module* memory (`Modules/_io/.cpython-review/
findings.json`) fits CPython's directory-scoped review cadence.

### 2.2 known-issues regression command
Port `rustpy-review-toolkit/.../scripts/check_known_issues.py` + `commands/known-issues.md`.
Build **`data/cpython_known_bugs.tsv`** (`bug_id \t Modules/foo.c:line \t category`) seeded from the
oom/tsan findings repos + notable fixed segfaults. A fresh run of the category-matching scanner
(`scan_null_checks` for a NULL catalog, `scan_refcounts` for a refcount catalog) classifies each site
`present` / `line_drifted` / `absent` / `file_missing`, drift-tolerant. This is the natural home for
the **version differential**: run at a CPython tag and answer "which catalogued crashes are still
present / regressed after a revert."

### 2.3 Reproducer / dynamic-verification harness
Formalize `cext-review-toolkit/docs/reproducer-techniques.md` (+ `libfiu_helpers.py`,
`mallocfault_harness.py`) — the doc that **already found a CPython-own bug** (`_PyFrame_GetLocals`
NULL deref, `frameobject.c`, gh-146092, fixed upstream). Core engine: `_testcapi.set_nomemory(n,0)`
dense-sweep OOM injection with subprocess-per-iteration isolation and exit-code semantics
(139=SIGSEGV, 134=SIGABRT, 1=clean MemoryError). For CPython the target *is* the binary — run against
a locally-built **debug/ASan CPython** (no install step). Add a thin `reproduce` command wrapping the
dense-sweep harness. This upgrades a known-issues `present` verdict and a new candidate from *static
match* to *reproduced crash*.

### 2.4 Findings-repo feedback loop
Adopt the family's `analyze → reproduce → record → regenerate` loop. **Decision needed (§5):** either
feed the existing `cpython-oom-findings` / `cpython-tsan-findings` repos, or stand up a new
`cpython-review-findings` (prefix e.g. `CPY-`) as the static-first counterpart (mirrors
`rust-ext-review-findings` / `cext-review-findings`). Recording keeps `found_by` (which agent surfaced
it), `guarded_twin`, and `cpython_behavior` (the differential). `gen_known_*` scripts regenerate the
Tier-2.2 TSV, closing the loop back into the toolkit.

---

## 5. Tier 3 — Infrastructure to keep signal/noise usable

### 3.1 THE CHASSIS DECISION (the architectural fork — needs your call)
The FT/TSan detectors (1.2) and the STW-safety/lock-discipline ports need a **C call-graph
primitive** (`extract_functions` + `find_calls_in_scope`) the toolkit does not have. Two paths:

- **(A, recommended) Adopt cext/ft's `tree_sitter_utils.py` as a shared vendored chassis.** Larger
  up-front cost, but it (i) unlocks the entire FT dimension, (ii) lets the existing regex scanners
  gain precision (real function boundaries, real call detection — killing whole FP classes), and
  (iii) removes the 8×-duplicated `find_cpython_root`. It also aligns this toolkit with the family's
  vendoring model (sync forward from the shared root, never fork).
- **(B) Write a regex call-graph analog.** Cheaper, lossier, and a permanent maintenance island.

Recommendation: **A.** The FT seam is the single richest source of catalogued, mechanically-detectable
bugs; a regex call-graph would cap its precision permanently. Note: `tsan-report-analyzer` (regex over
TSan text) and `tsan-stress-generator` (a pure prompt) need **no** call graph and can ship ahead of
this decision.

### 3.2 A `data/` layer
Create `data/` and extract the inlined prose tables into refreshable JSON: `deprecated_apis.json`,
`refcount_api_semantics.json`, `fallible_apis.json`. Transfer **wholesale** from ft (they are already
CPython-API vocabularies): `stw_safe_apis.json`, `lock_macros.json`, `critical_section_apis.json`,
`atomic_patterns.json`, `thread_safe_apis.json`. Add `cpython_reachability_sources.json` (§3.3).

### 3.3 Reachability-tiering (the S/N mechanism)
CPython's candidate count is inherently huge; a flat dump is unusable. Adopt rustpy's tiering:
- **T1** — directly Python-exposed C fns (entries in `PyMethodDef`/`PyGetSetDef`/`PyMemberDef`
  arrays and `tp_*` slots): a bug here is directly reachable from a Python program.
- **T2** — public `Py*`/`PyAPI_FUNC` C-API (reachable by extensions/embedders).
- **T3** — `_Py*` / `static` internal helpers (transitive only, caller-guaranteed preconditions) →
  **default-quieter.**

Plus **input-provenance** signals (`data/cpython_reachability_sources.json`): a candidate whose
triggering value flows from a parsed argument, a user `__index__`/`__hash__`/`__repr__`, or a
user-supplied index/length is a *reachable* bug; one gated by an internal invariant (`assert`,
"cannot fail") is weak and down-ranked. The preflight mapper stamps the tier; the safety agents rank
by it.

### 3.4 Preflight upgrade + history-context split
- **`cpython-source-mapper`** (NEW preflight agent): classify each file hand-written vs
  **Argument-Clinic-generated** (`Modules/clinic/*.h` markers), emit ACCEPTABLE-noise grep patterns
  (Clinic boilerplate, `Py_RETURN_NONE`), and catalog init-style / GC types up front. The current
  `include-graph-mapper` is *structural* only — this adds *triage* orientation. Every claim must be a
  grep regex or a script finding-ID, not prose.
- **Split history into two agents** (the code-review-toolkit shape): a new **`git-history-context`**
  preflight that runs *early* (per-file `Modules/`/`Objects/`/`Python/` bug-fix-density watchlist to
  prioritize the safety agents) — `pyo3-history-context` is a near-drop-in template — while keeping the
  existing `git-history-analyzer` as the Group-E fix-completeness pass.
- Add **shallow-clone detection** to discovery (CPython has decades of history; a `--depth` clone
  silently truncates every temporal signal — the rust toolkits already guard this).

### 3.5 C-vs-pure-Python parity agent (built-in oracle, novel, low effort)
CPython *ships* dual implementations: `_decimal`/`_pydecimal`, `_io`/`_pyio`, `_json`, `_heapq`/`heapq`,
`_datetime`/`datetime`, `_asyncio`/`asyncio`, `_statistics`, `_collections`, `_functools`. The pure-Python
twin is a free differential oracle. A `cpython-parity-checker` agent (port cext's `parity-checker`
prompt) finds behavioral divergences — a genuinely new capability, mostly a prompt. Pairs with 2.3:
when a C candidate diverges from its twin (twin raises, C segfaults) the bug is confirmed and localized.

---

## 6. Tier 4 — Housekeeping (do first; cheap, unblocks trust)

- **Fix CHANGELOG versioning.** It has only a flat `[Unreleased]` despite manifests at 0.4.0 and git
  bumps for 0.2.0/0.3.0/0.4.0. Add dated `## [0.4.0]`/`[0.3.0]`/`[0.2.0]` sections; adopt git tags.
- **Fix doc drift:** manifests/READMEs advertise "10 agents / 7 scripts"; the tree has **11 / 8**
  (`git-history-analyzer` + `analyze_history.py` shipped under `[Unreleased]` uncounted).
- **Establish a design doc.** Every other family member has an authoritative spec; this one's design
  lives only in two READMEs. A short `cpython-review-toolkit-design.md` (agent roster, classification,
  reachability tiers, the informed/known-issues grammar) would anchor future work — this file can seed it.
- **Add tests for the 3 script-less agents** once 1.4/1.5/1.6 give them scripts.

---

## 7. Recommended sequencing

**v0.5 — chassis-light wins + first flagship detectors** (no tree-sitter dependency):
- Tier 4 housekeeping (CHANGELOG, counts, design doc).
- 3.2 `data/` bootstrap + 3.3 reachability data + `cpython_non_bugs.md`.
- 2.1 informed-explore + 2.2 known-issues (wired to oom/tsan findings repos).
- 1.1 recursion-guard + 1.4 pyerr-clear-in-dealloc + 1.3 uninit-dealloc (all portable, near-zero-FP,
  live regression fixtures ready).
- 3.4 history-context split + shallow-clone detection.

**v0.6 — the free-threading dimension** (gated on the §5 chassis decision):
- 3.1 adopt tree-sitter-C chassis.
- 1.2 `scan_ft_races.py` (T3→T2→T1) + STW-safety / lock-discipline / atomic-candidate ports from ft.
- `tsan-report-analyzer` + `tsan-stress-generator` (can land at the *start* of v0.6 — no call graph).
- Precision re-baseline of existing scanners now that real function boundaries exist.

**v0.7 — dynamic loop + remaining detectors:**
- 2.3 reproducer harness + `reproduce` command + 2.4 findings-repo loop.
- 3.5 parity agent. 1.5 init-bypass. 1.6 memory-pattern promotion. 1.7/1.8 error-path/borrowed-ref
  strengthening.
- 3.1 `cpython-source-mapper` preflight (Argument-Clinic classification).

---

## 8. Decisions needed from you

1. **Chassis (§3.1):** adopt the shared tree-sitter-C chassis (recommended — unlocks FT + upgrades
   existing scanners), or stay regex + write a call-graph analog? This gates all FT work.
2. **Findings repo (§2.4):** new `cpython-review-findings` (`CPY-`, static-first counterpart), or feed
   the existing `cpython-oom-findings` / `cpython-tsan-findings`?
3. **FT scope:** is reviewing CPython's *own* free-threaded C code in-scope here, or deferred to
   `ft-review-toolkit`? (Argument for in-scope: the ft toolkit was literally calibrated against
   CPython's own code — `scan_stw_safety.py` cites `Python/gc_free_threading.c` as ground truth — and
   the tsan findings repo is CPython-specific.)
4. **Scope of v0.5:** land the whole chassis-light bundle above, or start with the two flagship
   detectors (1.1 + 1.4) + informed/known-issues as a smaller first cut?
```
```

---

### Appendix — evidence sources
- Current-state inventory: `plugins/cpython-review-toolkit/{agents,scripts,commands}`, manifests, CHANGELOG, `docs/python-wrapper-new-without-init.md`, `reports/`.
- Fuzzing catalogs: `~/projects/cpython-oom-findings` (schema + `ingest.py`/`gen_known_sites.py`; OOM classes O1–O7), `~/projects/cpython-tsan-findings` (races T1–T7), `~/projects/rustpython-findings` (mirror classes R1–R7).
- Tracker (gh, authenticated, 2026-07-24): shapes A–I; umbrellas #151763/#153852/#146102.
- Sibling ports: `rust-ext-review-toolkit/.../build_informed_briefing.py`+`informed-explore.md`; `rustpy-review-toolkit/.../check_known_issues.py`+`known-issues.md`+`known_panics.tsv`+`rustpython_reachability_sources.json`; `cext-review-toolkit/docs/reproducer-techniques.md`(+helpers)+`parity-checker.md`+`pyerr-clear-auditor.md`; `ft-review-toolkit/.../scan_stw_safety.py`+six `data/*.json`+`{stw-safety-checker,tsan-stress-generator,tsan-report-analyzer}.md`; this repo's own `pyo3-history-context` template (in the pyo3 sibling).
