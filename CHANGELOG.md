# Changelog

All notable changes to this project will be documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

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
