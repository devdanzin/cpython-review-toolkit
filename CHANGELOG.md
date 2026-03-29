# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- `init_not_reinit_safe` finding: detect tp_init functions that allocate without re-init guards.
- `new_missing_member_init` finding: detect tp_new functions using non-zeroing allocators without member initialization.
- Initial implementation of cpython-review-toolkit plugin.
- 7 analysis scripts: analyze_includes, measure_c_complexity, check_pep7, scan_refcounts, scan_error_paths, scan_null_checks, scan_gil_usage.
- 10 agent definitions: refcount-auditor, error-path-analyzer, gil-discipline-checker, c-complexity-analyzer, include-graph-mapper, pep7-style-checker, null-safety-scanner, api-deprecation-tracker, macro-hygiene-reviewer, memory-pattern-analyzer.
- 4 command definitions: explore, map, hotspots, health.
- Test helper (TempProject for C projects) and 7 test files with 61 tests.
- Plugin scaffolding: plugin.json, marketplace.json, LICENSE, .gitignore.
- Project and plugin READMEs.
