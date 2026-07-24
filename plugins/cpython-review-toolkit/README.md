# CPython C Code Review Toolkit

A comprehensive collection of specialized agents for exploring and analyzing CPython's C source code. While general-purpose C linters (clang-tidy, cppcheck) catch generic issues, this toolkit targets **CPython-specific bug patterns** that no general tool understands: reference counting errors, GIL discipline violations, CPython error handling conventions, and PEP 7 style.

## Why CPython Needs Its Own Tool

CPython's C code has fundamentally different concerns from Python code or generic C:

| Concern | What makes it CPython-specific |
|---------|-------------------------------|
| **Memory** | Manual reference counting (Py_INCREF/DECREF), not malloc/free |
| **Error handling** | Return NULL + PyErr_SetString, goto-based cleanup |
| **Concurrency** | Must manage GIL explicitly (Py_BEGIN/END_ALLOW_THREADS) |
| **Style** | PEP 7 (4 spaces, 79 chars, C11), not K&R or LLVM style |
| **API surface** | Three-tier API (public, cpython, internal) with deprecation cycles |
| **Top bug class** | Refcount leaks and use-after-free, not logic errors |

## Installation

### Marketplace install (recommended)

```bash
claude plugin marketplace add devdanzin/cpython-review-toolkit
claude plugin install cpython-review-toolkit@cpython-review-toolkit
```

### Local install

```bash
git clone https://github.com/devdanzin/cpython-review-toolkit.git
cd cpython-review-toolkit
```

Then in Claude Code:

```bash
/plugin install plugins/cpython-review-toolkit
```

### Using without installing

```bash
git clone https://github.com/devdanzin/cpython-review-toolkit.git
claude --plugin-dir cpython-review-toolkit/plugins/cpython-review-toolkit
```

### Prerequisites

- **Claude Code** installed and running.
- **Python 3.10+** for the analysis scripts (type union syntax, match statements).
- **tree-sitter + tree-sitter-c** (`pip install tree-sitter tree-sitter-c`) — required by the tree-sitter-based detectors (`recursion-guard`, `pyerr-clear`, `uninit-dealloc`, `init-bypass`, the free-threading trio, `memory`), the `known-issues` command, and the `informed-explore` briefing. The legacy regex scanners (refcounts, error-paths, null-safety, GIL, complexity, PEP 7, includes) remain stdlib-only.

## Commands

### `/cpython-review-toolkit:explore [scope] [aspects] [options]`

The primary command. Runs the include-graph-mapper first for structural context, then dispatches selected agents.

```bash
# Full exploration (all agents)
/cpython-review-toolkit:explore

# Specific directory
/cpython-review-toolkit:explore Objects/

# Specific aspects only
/cpython-review-toolkit:explore . refcounts errors

# Quick summary mode
/cpython-review-toolkit:explore . all summary
```

**Aspects**: `includes`, `refcounts`, `errors`, `gil`, `complexity`, `style`, `null-safety`, `deprecation`, `macros`, `memory`, `recursion`, `pyerr-clear`, `uninit-dealloc`, `ft-races`, `stw-safety`, `lock-discipline`, `init-bypass`, `parity`, `history`, `all`

**Options**: `deep` (full detail), `summary` (top-level only), `parallel` (concurrent agents)

### `/cpython-review-toolkit:map [scope]`

Quick include graph mapping. The fastest way to understand CPython's C file structure, dependency relationships, and API tier boundaries.

```bash
/cpython-review-toolkit:map
/cpython-review-toolkit:map Modules/_io/
```

### `/cpython-review-toolkit:hotspots [scope]`

Find the worst functions to fix first: runs refcount-auditor, error-path-analyzer, and c-complexity-analyzer. Answers "where should I focus my review efforts?"

```bash
/cpython-review-toolkit:hotspots
/cpython-review-toolkit:hotspots Objects/
```

### `/cpython-review-toolkit:health [scope]`

Quick health dashboard — all agents in summary mode, producing a scored table across every dimension.

```bash
/cpython-review-toolkit:health
/cpython-review-toolkit:health Python/
```

### `/cpython-review-toolkit:known-issues [scope]`

Regression baseline. Cross-references `data/cpython_known_bugs.tsv` — a seed catalog of previously-found CPython crashes (from the fusil OOM/TSan findings repos and the tracker) — against a fresh scan, classifying each as `present` / `line_drifted` / `absent` / `file_missing` / `no_scanner`. Answers "which catalogued crashes are still here, and did any regress?" (Note: some crash shapes carry no scannable token, so `absent` is not proof of a fix.)

```bash
/cpython-review-toolkit:known-issues
/cpython-review-toolkit:known-issues Objects/
```

### `/cpython-review-toolkit:informed-explore [scope]`

A catalog-seeded targeted pass. Builds a briefing from `data/cpython_bug_shapes.json` (each bug shape + its guarded twin + hunt directive + differential) and the `data/cpython_non_bugs.md` FP taxonomy, then runs the explore agents with three rules: confirm-don't-relitigate known findings, skip (or justify) known FP classes, and hunt siblings via the guarded twin. Optionally `--catalog-dir` a `cpython-review-findings` repo.

```bash
/cpython-review-toolkit:informed-explore Objects/
```

### `/cpython-review-toolkit:reproduce [finding-or-snippet] [--python <build>]`

Turn a static allocation-failure candidate into a **reproduced crash**. Runs a
dense `_testcapi.set_nomemory` sweep (one subprocess per allocation index) on a
locally-built CPython and classifies each outcome — `segv`/`abort` = reproduced,
`memory_error` = the failure was handled correctly (the safe outcome). Needs a
build providing `_testcapi.set_nomemory`; a debug or ASan build is best.

```bash
/cpython-review-toolkit:reproduce Objects/templateobject.c:225
/cpython-review-toolkit:reproduce 'import json; json.loads("[1,2]")' --python ~/projects/cpython/python
```

## Agents

### Safety-Critical (script-backed)

These agents find bugs that cause crashes, memory corruption, or undefined behavior. Each uses a dedicated analysis script for candidate detection, then performs deep qualitative review.

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **refcount-auditor** | Leaked references, use-after-free from borrowed refs, stolen-reference misuse, double-free risks, missing Py_CLEAR | `scan_refcounts.py` |
| **error-path-analyzer** | Missing NULL checks after API calls, return NULL without PyErr_Set*, incomplete goto cleanup, inconsistent error conventions | `scan_error_paths.py` |
| **null-safety-scanner** | Unchecked malloc/PyMem_Malloc, dereference before NULL check, PyArg_Parse without return check | `scan_null_checks.py` |
| **gil-discipline-checker** | Mismatched BEGIN/END_ALLOW_THREADS, Python API calls without GIL, blocking I/O with GIL held, PyGILState balance | `scan_gil_usage.py` |

### Crash-Class Detectors (tree-sitter based)

These target specific reachable-from-Python crash classes grounded in the fusil OOM/TSan findings and the CPython tracker. They parse a real C syntax tree and stay deliberately quiet — a whole-tree run surfaces a small, triageable set (each validated against confirmed CPython crashes).

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **recursion-guard-auditor** | Recursion-prone slots (`tp_hash`/`tp_richcompare`/`tp_repr`/`tp_str`, generic-alias parameter walks) that descend a user-controlled object graph without `Py_EnterRecursiveCall`/`Py_ReprEnter` → native-stack-overflow SIGSEGV (gh-154318, gh-154275) | `scan_recursion_guards.py` |
| **pyerr-clear-auditor** | `PyErr_Clear()` in the destructor family (`tp_dealloc`/`tp_clear`/`tp_finalize`/`tp_traverse`) with no save/restore, swallowing an in-flight `MemoryError`/`KeyboardInterrupt` (gh-152083) | `scan_pyerr_clear.py` |
| **uninitialized-dealloc-auditor** | Non-zeroing allocation freed on an error path before members are NULL-initialized → `tp_dealloc` reads garbage (gh-151815, gh-152851) | `scan_uninit_dealloc.py` |
| **init-bypass-checker** | A slot reads `self->field` and INCREFs/calls/derefs it with no NULL guard, where `__new__` can bypass `tp_init` or the member is deletable (gh-152954, gh-152817) | `scan_init_bypass.py` |

### Free-Threading / Data Races (tree-sitter based)

These target CPython's own free-threaded (`Py_GIL_DISABLED`, PEP 703) code, grounded in the fusil `cpython-tsan-findings` catalog. The ft-review-toolkit that seeded them was calibrated against CPython's own runtime, so they are at home here.

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **ft-race-scanner** | T3 iterator-exhaustion double-DECREF (gh-154130/gh-144357/gh-153296), T2 lazy-init cache without a critical section (TSAN-0043), T1 atomic/plain access asymmetry (TSAN-0006). Suppresses the `*_lock_held` convention | `scan_ft_races.py` |
| **stw-safety-checker** | Calls inside a `_PyEval_StopTheWorld` region that can invoke Python / GC / set an exception (intra-file call graph) | `scan_stw_safety.py` |
| **lock-discipline-checker** | Critical-section acquire/release pairing — missing `Py_END_CRITICAL_SECTION`, early return/goto out of a section, nested different-object locks | `scan_lock_discipline.py` |

### Dynamic (TSan) — on-demand

Not part of the static explore pipeline; these consume/produce a ThreadSanitizer run on a `--disable-gil` build.

| Agent | What It Does | Script |
|-------|--------------|--------|
| **tsan-report-analyzer** | Parses/dedups a TSan report; for CPython, races in CPython's own frames ARE the target (the extension-oriented filter is inverted) | `parse_tsan_report.py` |
| **tsan-stress-generator** | Emits a concurrent stress script that hammers a shared stdlib object under `PYTHON_GIL=0` to trigger races | — (prompt) |
| **oom-reproducer** | Dense `_testcapi.set_nomemory` sweep (one subprocess per index) that turns a static allocation-failure candidate into a **reproduced** crash — or an honest negative. See the `reproduce` command | `run_oom_sweep.py` |

### Differential (parity)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **parity-checker** | Behavioral divergence between a C accelerator and its shipped pure-Python twin (`_decimal`/`_pydecimal`, `_io`/`_pyio`, `_datetime`/`_pydatetime`, …). A C crash where the twin raises is a confirmed, localized bug — CPython ships its own oracle | `find_parity_pairs.py` |

### Code Quality (script-backed)

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **c-complexity-analyzer** | Functions scored 1-10 by line count, nesting depth, cyclomatic complexity, parameter count, goto count | `measure_c_complexity.py` |
| **pep7-style-checker** | Tab indentation, line length > 79, keyword spacing, missing braces, trailing whitespace, missing header guards | `check_pep7.py` |
| **include-graph-mapper** | Include dependency graph, fan-in/fan-out metrics, circular includes, API tier classification (public/cpython/internal) | `analyze_includes.py` |

### Maintenance (qualitative — no script)

These agents search the codebase directly using Grep and read files for deep analysis. They don't need a script because the patterns are better checked qualitatively.

| Agent | What It Finds |
|-------|--------------|
| **api-deprecation-tracker** | Usage of deprecated APIs (PyModule_AddObject, PyUnicode_READY, Py_UNICODE, etc.) with migration paths |
| **macro-hygiene-reviewer** | Missing parentheses in macros, multiple evaluation, multi-statement macros without do-while, naming |

**memory-pattern-analyzer** became script-backed in 0.7 — see below. Its remaining
qualitative checks (sprintf/strcpy bounds, use-after-free, double-free) stay an
explicit by-hand phase in the agent prompt.

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **memory-pattern-analyzer** | Integer overflow in an allocation size from a Python-controlled multiply (gh-3493, gh-1779), the GC-track invariant (gh-152107), mismatched alloc/free families | `scan_memory_patterns.py` |

### Temporal

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **git-history-context** | Preflight (runs early): per-file bug-fix-density watchlist + recurring fix-keyword clusters + shallow-clone guard, so the safety agents scrutinize the historically-buggiest files first | `analyze_history.py` |
| **git-history-analyzer** | Post-hoc (runs last): fix-completeness review, similar-bug detection, churn-risk matrix, Argument-Clinic / API-modernization migration gaps | `analyze_history.py` |

## How It Works

### Scripts Find Candidates, Agents Confirm

The analysis scripts identify candidate issues; the agent then reads the real code and classifies each finding. Two generations of scanner coexist:

1. **Legacy regex scanners** (refcounts, error-paths, null-safety, GIL, complexity, PEP 7, includes) are stdlib-only. PEP 7's regularity makes function definitions, brace placement, and naming predictable enough for regex.
2. **Tree-sitter crash-class detectors** (`recursion-guard`, `pyerr-clear`, `uninit-dealloc`) parse a real C syntax tree via the shared `tree_sitter_utils` chassis (vendored from the cext sibling), so they track true function boundaries, calls, and slot tables instead of line patterns.
3. **Acceptable false positive rate**: Scripts report candidates (expect 10-50% false positives depending on the detector). The agent reads the actual code, tracks control flow, and classifies each finding as FIX / CONSIDER / POLICY / ACCEPTABLE.

### CPython Layout Awareness

Scripts auto-detect the CPython root by looking for `Include/Python.h` and `Objects/object.c`. They understand the directory structure:

| Directory | Contents | Criticality |
|-----------|----------|-------------|
| `Include/` | Public + internal C headers | High — API surface |
| `Objects/` | Core type implementations (list, dict, ...) | Critical — hot path |
| `Python/` | Interpreter core (ceval, compile, ...) | Critical — hot path |
| `Modules/` | Standard library C extensions | Medium |
| `Parser/` | Parser and tokenizer | Medium |
| `Programs/` | Entry points | Low |
| `PC/`, `Mac/` | Platform-specific code | Low |

Bugs in Objects/ and Python/ are weighted more heavily than bugs in PC/ or Mac/.

### Classification System

Every finding is tagged with a severity:

| Tag | Meaning | Example |
|-----|---------|---------|
| **FIX** | Unambiguously wrong — crash risk, memory corruption | Refcount leak on error path, NULL dereference |
| **CONSIDER** | Likely improvement, but trade-offs exist | High complexity that could be reduced, deprecated API usage |
| **POLICY** | Requires team-level decision | Error handling convention choices, deprecation timeline |
| **ACCEPTABLE** | Noted but no action needed | Intentional broad error handling, complexity inherent to the algorithm |

## Recommended Workflows

### Reviewing a CPython Module

```
1. /cpython-review-toolkit:map Modules/_json/       → Understand includes
2. /cpython-review-toolkit:hotspots Modules/_json/   → Find worst functions
3. /cpython-review-toolkit:explore Modules/_json/ refcounts errors deep  → Deep dive
```

### Auditing Core Safety

```
1. /cpython-review-toolkit:explore Objects/ refcounts errors null-safety gil
2. Focus on FIX findings in Objects/ and Python/
3. Re-run on specific files after fixes
```

### Pre-Release Health Check

```
1. /cpython-review-toolkit:health                     → Dashboard across all dimensions
2. /cpython-review-toolkit:explore . deprecation style → API and style compliance
3. /cpython-review-toolkit:explore . refcounts errors  → Safety audit
```

### Onboarding to CPython Development

```
1. /cpython-review-toolkit:map                         → Understand the structure
2. /cpython-review-toolkit:explore . includes style    → Learn conventions
3. /cpython-review-toolkit:explore Objects/listobject.c all deep  → Study one file in depth
```

## Explore Command Phases

The `explore` command runs agents in a structured pipeline:

| Phase | Agents | Purpose |
|-------|--------|---------|
| **0** | Project discovery | Detect CPython layout, count files, identify version |
| **1** | include-graph-mapper, git-history-context | Structural + temporal context for all other agents |
| **2A** | refcount-auditor, error-path-analyzer | Safety-critical (highest value) |
| **2A2** | recursion-guard-auditor, pyerr-clear-auditor, uninitialized-dealloc-auditor, init-bypass-checker | Crash-class detectors (tree-sitter) |
| **2B** | null-safety-scanner, gil-discipline-checker | Memory safety |
| **2B2** | ft-race-scanner, stw-safety-checker, lock-discipline-checker | Free-threading / data races (PEP 703) |
| **2C** | c-complexity-analyzer, pep7-style-checker | Code quality |
| **2D** | api-deprecation-tracker, macro-hygiene-reviewer, memory-pattern-analyzer | Maintenance |
| **2D2** | parity-checker | Differential vs the shipped pure-Python twin |
| **2E** | git-history-analyzer | Temporal fix-completeness (runs last) |
| **3** | Synthesis | Deduplicate, resolve conflicts, produce summary |

## Limitations

- **Mixed parsing**: the legacy scanners are regex-based (cannot track pointer aliasing, complex control flow, or code-generating macros); the crash-class detectors use tree-sitter but are still syntactic — they report candidates, not definitive bugs, and the agent triage step is where FIX-confidence is earned.
- **Dynamic verification needs a built interpreter**: the `reproduce` command / `oom-reproducer` agent run real `_testcapi.set_nomemory` OOM sweeps, and the TSan agents need a `--disable-gil`+TSan build. Without such a build the toolkit is static-only. Note a non-reproduction is **not** a refutation — the payload may simply not reach the flagged line.
- **No clang-tidy/cppcheck integration yet**: A future phase could integrate external C analysis tools alongside the CPython-specific scripts.
- **Single-file scope for scripts**: Scripts analyze each function independently. Cross-function reference ownership transfer is tracked only at the API boundary level, not through arbitrary call chains.
- **Best on idiomatic CPython code**: The regex patterns are tuned for PEP 7 style. Non-standard C code (vendored libraries, generated code) may produce more false positives.

## Plugin Structure

```
cpython-review-toolkit/
├── .claude-plugin/
│   └── plugin.json
├── README.md
├── agents/
│   ├── refcount-auditor.md
│   ├── error-path-analyzer.md
│   ├── null-safety-scanner.md
│   ├── gil-discipline-checker.md
│   ├── recursion-guard-auditor.md          # crash-class (tree-sitter)
│   ├── pyerr-clear-auditor.md              # crash-class (tree-sitter)
│   ├── uninitialized-dealloc-auditor.md    # crash-class (tree-sitter)
│   ├── init-bypass-checker.md              # crash-class (tree-sitter)
│   ├── ft-race-scanner.md                  # free-threading (tree-sitter)
│   ├── stw-safety-checker.md               # free-threading (tree-sitter)
│   ├── lock-discipline-checker.md          # free-threading (tree-sitter)
│   ├── tsan-report-analyzer.md             # dynamic TSan (on-demand)
│   ├── tsan-stress-generator.md            # dynamic TSan (on-demand)
│   ├── oom-reproducer.md                   # dynamic OOM (on-demand)
│   ├── parity-checker.md                   # differential vs pure-Python twin
│   ├── c-complexity-analyzer.md
│   ├── pep7-style-checker.md
│   ├── include-graph-mapper.md
│   ├── api-deprecation-tracker.md
│   ├── macro-hygiene-reviewer.md
│   ├── memory-pattern-analyzer.md
│   ├── git-history-context.md              # preflight temporal
│   └── git-history-analyzer.md             # post-hoc temporal
├── commands/
│   ├── explore.md
│   ├── informed-explore.md
│   ├── known-issues.md
│   ├── reproduce.md
│   ├── reproduce.md
│   ├── health.md
│   ├── hotspots.md
│   └── map.md
├── data/
│   ├── cpython_known_bugs.tsv              # known-issues regression catalog
│   ├── cpython_bug_shapes.json             # informed-explore bug shapes
│   ├── cpython_reachability_sources.json   # T1/T2/T3 reachability tiers
│   ├── cpython_non_bugs.md                 # false-positive taxonomy
│   ├── atomic_patterns.json                # free-threading (atomics)
│   ├── stw_safe_apis.json                  # free-threading (STW safety)
│   ├── lock_macros.json                    # free-threading (locks)
│   └── critical_section_apis.json          # free-threading (critical sections)
└── scripts/
    ├── tree_sitter_utils.py                # vendored C parsing chassis
    ├── scan_common.py                      # shared helpers
    ├── analyze_includes.py
    ├── analyze_history.py
    ├── check_pep7.py
    ├── measure_c_complexity.py
    ├── scan_error_paths.py
    ├── scan_gil_usage.py
    ├── scan_null_checks.py
    ├── scan_refcounts.py
    ├── scan_recursion_guards.py            # tree-sitter detector
    ├── scan_pyerr_clear.py                 # tree-sitter detector
    ├── scan_uninit_dealloc.py              # tree-sitter detector
    ├── scan_ft_races.py                    # free-threading detector
    ├── scan_stw_safety.py                  # free-threading detector
    ├── scan_lock_discipline.py             # free-threading detector
    ├── parse_tsan_report.py                # dynamic TSan analyzer
    ├── scan_init_bypass.py                 # crash-class detector
    ├── scan_memory_patterns.py             # alloc-overflow / GC-track
    ├── run_oom_sweep.py                    # OOM reproducer harness
    ├── find_parity_pairs.py                # C <-> pure-Python twin discovery
    ├── check_known_issues.py               # known-issues command
    └── build_informed_briefing.py          # informed-explore briefing
```

## Comparison with code-review-toolkit

| Dimension | code-review-toolkit | cpython-review-toolkit |
|-----------|--------------------|-----------------------|
| **Language** | Python | C (CPython source) |
| **Parsing** | Python `ast` module | Regex (legacy scanners) + tree-sitter (crash-class detectors) |
| **Root detection** | `pyproject.toml`, `.git` | `Include/Python.h`, `Objects/object.c` |
| **Top bug class** | Logic errors, dead code | Refcount leaks, NULL deref, native-stack-overflow SIGSEGV, GIL violations |
| **Style guide** | PEP 8 | PEP 7 |
| **Agents** | 14 | 23 |
| **Scripts** | 8 | 23 |

## Author

Danzin
