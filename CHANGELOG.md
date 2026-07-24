# Changelog

All notable changes to this project will be documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.8.0] - 2026-07-24

The **correctness release**. A full `informed-explore` run over a 14-file
`Objects/` sample was used to audit the toolkit against real CPython source. It
found 15 FIX-class bugs — but it also found that **0 of 69 candidates from the
three largest scanners were real**, that three headline rules were dead code,
and that two entries in the false-positive taxonomy were factually wrong about
CPython. This release fixes 23 toolkit defects found that way.

Every number below was measured on CPython main @ `4f3be1b5777` (3.16.0a0).
Tests: **243 → 556**.

### Fixed — the chassis (shared; fixed upstream in cext-review-toolkit and synced)
- **`extract_functions()` silently dropped and merged functions.** CPython's
  brace-unbalanced macros (`Py_BEGIN_ALLOW_THREADS`, `Py_BEGIN_CRITICAL_SECTION`),
  the 48-name `PyObject_HEAD` punctuation family, and the `_Py_COMP_DIAG_*`
  pragma family desynchronize `tree-sitter-c`. Worst case was **misattribution,
  not omission**: `Objects/object.c` returned one record spanning **lines
  1267–3521 (2,254 lines, ~91 functions)**, so findings were confidently
  reported against the wrong function. Now: max span **126**; `dictobject.c`
  **187 → 292** functions reaching line 8569 of 8598; `Py_BEGIN_CRITICAL_SECTION`
  outside any function **19/187 → 3/187**; 3,559 → 3,751 functions tree-wide with
  **no per-file regression** and all byte offsets verified.
  New `scrub_macros()` / `parse_health()` primitives; `scrub=` on all `parse_*`.
  *Measured and rejected*: Argument Clinic substitution (would take `dictobject.c`
  to **72** functions) and ERROR-node recovery (all candidates garbage).
- **`strip_comments()` destroyed line numbers** by collapsing block comments
  without their newlines — 14 lines of drift in a single 1,070-line file. Now
  line-count-preserving, verified across all 50 `Objects/*.c`.

### Fixed — dead or structurally disabled rules
- **`scan_refcounts.py`: `borrowed-ref-across-call` did not exist.** The
  toolkit's flagship analysis was dead code (`BORROWED_REF_APIS` fed an unused
  regex). Implemented as `stale_slot_decref` + `owner_freed_before_use` with a
  `PYTHON_REACHING_APIS` table (122 → 226 entries incl. private `_Py*` aliases).
  Whole-tree volume **639 → 7**; on the sample where the old scanner scored
  **0/19** it now emits **2 findings, both ASan-confirmed bugs**.
- **`scan_error_paths.py`: an off-by-one read the return type from the line
  *above* it**, so 82% of functions had an empty type and `return_null_no_exception`
  had been evaluating ~1% of its population. `PyObject`-returning **22 → 1045**.
  Rule re-scoped to gated `alloc_null_no_memerror`; new `unconditional_pyerr_clear`.
  `Objects/` **148 → 33**, `Modules/` **458 → 61**.
- **`scan_null_checks.py`: `deref-before-check` appended nothing**, so
  `high_confidence` was permanently 0 while the agent prompt told agents to
  prioritize that empty set. Implemented properly — and it finds **exactly zero**
  on CPython main, a fact now recorded in the docstring and prompt so the zero is
  never read as an audit result. `Objects/` **113 → 1**, `Modules/` **311 → 13**.
- **`scan_init_bypass.py` saw 2 of 44 slot declarations.** `Objects/` uses the
  positional `X, /* tp_init */` form, and the marker lives in a *comment* that
  `strip_comments()` deleted. Now parsed on raw source; nullable fields
  **24 → 38**; new `addr_deref` sink; getset setters modeled.
- **`scan_memory_patterns.py` could not express its own bug shape** — no
  var-object allocator entry, and the multiply lives inside `_PyObject_VAR_SIZE`.
  New `varobject_nitems_unguarded`; GC gate made type-level; taint table split.
- **`scan_lock_discipline.py` discarded half its data file**, filtering out the
  `PyMutex` family and going blind to `weakrefobject.c`'s 16-site `LOCK_WEAKREFS`
  scheme. Both families now load and pair independently.

### Fixed — factual errors in the shipped knowledge base
- **`PyObject_Hash` was listed as recursion-guarded. It is not**
  (`Objects/object.c:1158`, unlike `PyObject_Repr` :759 / `PyObject_Str` :800 /
  `PyObject_RichCompare` :1099). An agent trusting the taxonomy would have
  dismissed the entire confirmed recursion class, both catalogued findings
  included. Corrected in `cpython_non_bugs.md` and `recursion-guard-auditor.md`.
- **The `Py_TRASHCAN` entry told agents to look for a marker that no longer
  exists** — the macros are empty backwards-compat shims with zero call sites in
  `Objects/`/`Modules/`; the live mechanism is automatic in `_Py_Dealloc`. The
  stale test biased toward *false positives*.
- **Catalog entry `OOM-0023` was mis-catalogued**, not fixed: `subtype_dealloc`
  has zero `PyErr_*` calls in 167 lines and no commit ever removed one. Removed
  with a tombstone; it was also the worked example in an agent prompt.

### Fixed — silent-wrongness in shared helpers
- `deduplicate_findings()` keyed on a *normalized* detail string that erased
  quoted names and line numbers, collapsing distinct bugs in the same file and
  hiding the second in `duplicate_locations`. Now exact on `(type, file, line)`.
- `resolve_roots()` set `scan_root = target.parent` for a file target, so
  **scanning one file silently scanned the whole directory**. Fixed there and in
  the four scanners carrying a local copy.
- `parse_common_args()` silently swallowed unknown flags; now warns on stderr.

### Fixed — history and regression tooling
- `analyze_history.py` **died on any window longer than ~10 years**
  (`text=True` with no `errors=`; one non-UTF-8 commit aborted everything). Full
  9,203-commit `Objects/` history now analyses in ~11 s. **The identical defect
  was propagated to all five sibling toolkits.**
- Unknown flags are now a hard error (`--months 420` used to run silently at the
  default 90-day window); `--max-commits` 2000 → 50000 with the cap surfaced in
  `notes[]`; `.py` dropped from discovery in a C-source toolkit.
- New `--introduced-by FILE:LINE` (validated: `genericaliasobject.c:542` →
  `1da989be74e`); crash-weighted `fix_confidence`/`crash_class` (the `fix` bucket
  was **44.9%** of commits, now 26.0%); per-file crash-fix density with
  `--follow` (ranks `genericaliasobject.c` **#1** where raw churn ranked it 36th);
  shallow-clone detection.
- `known-issues` gains **`absent_in_function`** — "the named function still
  exists and is clean" is a different signal from "the bug moved". 4 of 5
  `line_drifted` rows reclassify; `no_scanner: 0` preserved.

### Added
- **`tools/validate_precision.py`** — measures scanner volume and **line
  accuracy** (does the reported line actually carry the construct the finding
  describes?) across `Objects/`, `Modules/` and `Python/`, with baseline diffing.
- **`scan_deprecated_apis.py`** + `data/deprecated_c_apis.json` (66 verified
  entries) replacing a 2021-era pattern list that scored **0/13**; includes the
  `_Py_DEPRECATED_EXTERNALLY` tier the compiler never warns on under
  `Py_BUILD_CORE`. New `gc-untrack-macro-form` rule (2 hits tree-wide, both real).
- New FT rules `iternext_setref_null_decref` and `lazy_init_partial_guard`
  (gated on ≥2 accessors with ≥1 guarded); `Py_GIL_DISABLED` region modeling;
  positional `tp_iternext` detection. Sample precision **3/6 → 5/5**.
- `run_oom_sweep.py` gains `--setup` (arming before setup burned the budget) and
  sanitizer-aware classification — ASan's exit 1 was being read as the *safe*
  `memory_error` outcome.
- `analyze_includes.py`: directives resolved to real paths before tiering, so
  `api_tiers` and `cycles` stop being tautologies (`Objects/` internal
  **0 → 87**; edge targets matching a node key **5/1110 → 669/670**; the tree's
  one real cycle surfaced). Symbol-based fan-in alongside include fan-in.

### Changed
- `check_pep7.py`: **5,736 → 64** findings on `Objects/`. `func-call-space`
  deleted (it fired on `#define X (…)`, where removing the space changes an
  object-like macro into a function-like one); `missing-braces` and
  `line-too-long` gated behind `--diff-only` (PEP 7 says braces are required
  *"but do not add them to code you are not otherwise modifying"*); generated and
  `stringlib` headers excluded from `header-guard`. Envelope normalized to
  `findings[]`.
- `measure_c_complexity.py`: multi-line signatures and Clinic `_impl` functions
  were dropped (**+35.7%** functions recovered); hotspot threshold made relative
  (absolute `5.0` flagged **3 functions in all of `Objects/`**, max score 6.5);
  new `manual_cleanup_ladder` metric — **24 of 25 defect functions have zero
  gotos**, so in CPython a `goto` cleanup ladder is a *positive* signal.
  Documented that complexity **inverts** for the recursion class: the guard is a
  branch, so the correct twin outscores the buggy sibling.

## [0.7.0] - 2026-07-24

The dynamic-verification release. Adds the harness that turns a *static*
candidate into a *reproduced* crash, plus the two remaining static detectors
whose designs were already written down, and a differential oracle built from
CPython's own shipped dual implementations.

### Added
- **oom-reproducer** + `run_oom_sweep.py` + the **`reproduce`** command: dense
  `_testcapi.set_nomemory` OOM injection with one subprocess per iteration and
  exit-code classification (139/-11 SIGSEGV, 134/-6 SIGABRT, 1 = a clean
  `MemoryError` — the *safe* outcome). This is the technique that already found
  gh-146092 (`_PyFrame_GetLocals`) by hand. Validated against a local CPython
  build: abort detection confirmed end-to-end; the interpreter guard rejects a
  python without `_testcapi`.
- **parity-checker** + `find_parity_pairs.py`: CPython *ships* pure-Python twins
  of several C accelerators (`_pydecimal`, `_pyio`, `_pydatetime`, …), which are
  a free differential oracle — if the C side crashes where the twin raises, the
  bug is confirmed and localized. Discovery finds 39 pairs on the current tree
  (6 high-confidence).
- **init-bypass-checker** + `scan_init_bypass.py`: builds the design in
  `docs/python-wrapper-new-without-init.md` for the C side — a slot reads
  `self->field` and INCREFs/calls/derefs it with no NULL guard on a type whose
  `tp_new` doesn't guarantee initialization, or whose field is deletable
  (gh-152954, gh-152817).
- **memory-pattern-analyzer promoted to a real scanner** (`scan_memory_patterns.py`):
  integer overflow in an allocation size from a Python-controlled multiply
  (gh-3493, gh-1779) and the GC-track invariant (gh-152107); previously
  qualitative-only. The patterns the script cannot cover stay documented as an
  explicit by-hand phase.

### Changed
- `known-issues`: the `init-bypass` category is now scanned, closing the last
  `no_scanner` gap in the catalog.
- `explore` / `health` / `hotspots` wire the new agents; version → 0.7.0.

## [0.6.0] - 2026-07-24

The free-threading release. Uses the v0.5 tree-sitter chassis to add data-race
detectors for CPython's own free-threaded (`Py_GIL_DISABLED`, PEP 703) code,
grounded in the fusil `cpython-tsan-findings` catalog.

### Added
- **ft-race-scanner** + `scan_ft_races.py`: three TSan-grounded race classes —
  T3 iterator-exhaustion double-DECREF (gh-154130 / gh-144357 / gh-153296), T2
  lazy-init cache without a critical section (TSAN-0043 `descr_get_qualname`),
  T1 atomic/plain access asymmetry (TSAN-0006 `count_repr`). Suppresses the
  `*_lock_held` / `*_locked` caller-holds-the-lock convention.
- **stw-safety-checker** + `scan_stw_safety.py` (ported from ft-review-toolkit):
  flags calls inside a `_PyEval_StopTheWorld` region that can invoke Python / GC
  / set an exception, via an intra-file call graph (now possible on the chassis).
- **lock-discipline-checker** + `scan_lock_discipline.py` (ported): critical-
  section acquire/release pairing, including the `Py_BEGIN_CRITICAL_SECTION_MUTEX`
  spelling.
- **tsan-report-analyzer** + `parse_tsan_report.py` and **tsan-stress-generator**
  (ported, inverted for CPython: races in CPython's own frames ARE the target,
  not noise to filter).
- FT data files: `stw_safe_apis.json`, `lock_macros.json`,
  `critical_section_apis.json`, `atomic_patterns.json`.

### Changed
- `known-issues`: the `tsan` catalog category is now scanned by `scan_ft_races`
  (was `no_scanner` in v0.5). Only `init-bypass` remains scanner-less.
- `explore` / `hotspots` / `health` wire the free-threading agents.
- Version → 0.6.0 (both manifests).

## [0.5.0] - 2026-07-24

The chassis-and-crash-classes release. Adopts a tree-sitter-C parsing chassis
(shared with the cext/ft siblings) and adds the first crash-class detectors
grounded in the fusil OOM/TSan findings repos and the CPython tracker, plus the
informed-explore and known-issues workflow machinery.

### Added
- **Tree-sitter-C chassis**: vendored `tree_sitter_utils.py` (verbatim from the
  cext sibling) and a CPython-adapted `scan_common.py` (`find_cpython_root`,
  `resolve_roots`, `discover_c_files`, `parse_common_args`, `build_report`,
  comment-based suppression, dedup). New scanners parse real function boundaries
  instead of regex. Requires `pip install tree-sitter tree-sitter-c`.
- **recursion-guard-auditor** + `scan_recursion_guards.py`: recursion-prone slots
  (`tp_hash`/`tp_richcompare`/`tp_repr`/`tp_str`, generic-alias parameter walks)
  that descend a user-controlled object graph without `Py_EnterRecursiveCall` /
  `Py_ReprEnter` → native-stack-overflow SIGSEGV. Validates against gh-154318
  (`tuple_hash`) and gh-154275 (`_Py_make_parameters`).
- **pyerr-clear-auditor** + `scan_pyerr_clear.py`: `PyErr_Clear()` in the
  destructor family (`tp_dealloc`/`tp_clear`/`tp_finalize`/`tp_traverse`) with no
  save/restore, swallowing an in-flight `MemoryError`/`KeyboardInterrupt`.
  Validates against `deque_clear` and gh-152083 (`context_tp_dealloc`).
- **uninitialized-dealloc-auditor** + `scan_uninit_dealloc.py`: non-zeroing
  allocation freed on an error path before members are initialized → `tp_dealloc`
  reads garbage. Validates against gh-151815 (`template_iter`).
- **known-issues** command + `check_known_issues.py`: cross-references
  `data/cpython_known_bugs.tsv` (seeded from cpython-oom-findings /
  cpython-tsan-findings / the tracker) against a fresh scan.
- **informed-explore** command + `build_informed_briefing.py`: a catalog-seeded
  targeted pass driven by `data/cpython_bug_shapes.json` (guarded-twin / hunt /
  differential) and `data/cpython_non_bugs.md` (FP taxonomy), with a
  `--catalog-dir` hook into a `cpython-review-findings` repo.
- **git-history-context** preflight agent: an early per-file bug-fix-density
  watchlist (distinct from the post-hoc `git-history-analyzer`), with
  shallow-clone detection.
- **`data/` layer**: `cpython_known_bugs.tsv`, `cpython_bug_shapes.json`,
  `cpython_reachability_sources.json`, `cpython_non_bugs.md`.
- `docs/improvement-plan.md`: the v0.5+ roadmap this release begins executing.

### Changed
- `explore` command wires the three new crash-class detectors (Group A2) and adds
  the `recursion`, `pyerr-clear`, and `uninit-dealloc` aspects.
- Version → 0.5.0 across both manifests.

## [0.4.0]

Everything shipped through 0.4.0 (previously tracked under `[Unreleased]`).

### Enhanced
- `analyze_history.py`: parallelize git subprocess calls using `ThreadPoolExecutor` for ~4-8x speedup on diff extraction. Add `--workers N` option (default 8).
- `git-history-analyzer` agent: add operational guidance (unique temp filenames, long Bash timeouts, fallback on timeout).

### Added
- `git-history-analyzer` agent: fix completeness review, similar bug detection via git history, churn-risk matrix, and CPython-specific analyses (module family propagation, Argument Clinic migration completeness, API modernization gaps).
- `analyze_history.py` script: git log parsing, commit classification with CPython-extended keywords, C function boundary detection, file/function churn metrics, co-change clusters, and module family awareness.
- `init_not_reinit_safe` finding: detect tp_init functions that allocate without re-init guards.
- `new_missing_member_init` finding: detect tp_new functions using non-zeroing allocators without member initialization.
- Initial implementation of cpython-review-toolkit plugin.
- 7 analysis scripts: analyze_includes, measure_c_complexity, check_pep7, scan_refcounts, scan_error_paths, scan_null_checks, scan_gil_usage.
- 10 agent definitions: refcount-auditor, error-path-analyzer, gil-discipline-checker, c-complexity-analyzer, include-graph-mapper, pep7-style-checker, null-safety-scanner, api-deprecation-tracker, macro-hygiene-reviewer, memory-pattern-analyzer.
- 4 command definitions: explore, map, hotspots, health.
- Test helper (TempProject for C projects) and 7 test files with 61 tests.
- Plugin scaffolding: plugin.json, marketplace.json, LICENSE, .gitignore.
- Project and plugin READMEs.
