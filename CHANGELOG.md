# Changelog

All notable changes to this project will be documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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
