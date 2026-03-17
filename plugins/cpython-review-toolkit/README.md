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
- No third-party Python packages — all scripts use only the standard library.

## Commands

### `/cpython-review-toolkit:explore [scope] [aspects] [options]`

The primary command. Runs the include-graph-mapper first for structural context, then dispatches selected agents.

```bash
# Full exploration (all 10 agents)
/cpython-review-toolkit:explore

# Specific directory
/cpython-review-toolkit:explore Objects/

# Specific aspects only
/cpython-review-toolkit:explore . refcounts errors

# Quick summary mode
/cpython-review-toolkit:explore . all summary
```

**Aspects**: `includes`, `refcounts`, `errors`, `gil`, `complexity`, `style`, `null-safety`, `deprecation`, `macros`, `memory`, `all`

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

## Agents

### Safety-Critical (script-backed)

These agents find bugs that cause crashes, memory corruption, or undefined behavior. Each uses a dedicated analysis script for candidate detection, then performs deep qualitative review.

| Agent | What It Finds | Script |
|-------|--------------|--------|
| **refcount-auditor** | Leaked references, use-after-free from borrowed refs, stolen-reference misuse, double-free risks, missing Py_CLEAR | `scan_refcounts.py` |
| **error-path-analyzer** | Missing NULL checks after API calls, return NULL without PyErr_Set*, incomplete goto cleanup, inconsistent error conventions | `scan_error_paths.py` |
| **null-safety-scanner** | Unchecked malloc/PyMem_Malloc, dereference before NULL check, PyArg_Parse without return check | `scan_null_checks.py` |
| **gil-discipline-checker** | Mismatched BEGIN/END_ALLOW_THREADS, Python API calls without GIL, blocking I/O with GIL held, PyGILState balance | `scan_gil_usage.py` |

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
| **memory-pattern-analyzer** | Mismatched alloc/free families, sprintf without bounds, integer overflow in allocation sizes |

## How It Works

### Scripts Find Candidates, Agents Confirm

The 7 analysis scripts use regex-based scanning — not a C parser — to identify candidate issues. This is a deliberate design choice:

1. **Stdlib-only**: No pycparser, tree-sitter, or libclang dependency.
2. **CPython's regularity**: PEP 7 makes function definitions, brace placement, and naming conventions predictable enough for regex.
3. **Acceptable false positive rate**: Scripts report candidates (expect 30-50% false positives). The agent reads the actual code, tracks control flow, and classifies each finding as confirmed, likely, or false positive.

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
| **1** | include-graph-mapper | Structural context for all other agents |
| **2A** | refcount-auditor, error-path-analyzer | Safety-critical (highest value) |
| **2B** | null-safety-scanner, gil-discipline-checker | Memory safety |
| **2C** | c-complexity-analyzer, pep7-style-checker | Code quality |
| **2D** | api-deprecation-tracker, macro-hygiene-reviewer, memory-pattern-analyzer | Maintenance |
| **3** | Synthesis | Deduplicate, resolve conflicts, produce summary |

## Limitations

- **Regex-based, not a real C parser**: Cannot track through pointer aliasing, complex control flow, or macros that generate code. Reports candidates, not definitive bugs.
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
│   ├── gil-discipline-checker.md
│   ├── c-complexity-analyzer.md
│   ├── include-graph-mapper.md
│   ├── pep7-style-checker.md
│   ├── null-safety-scanner.md
│   ├── api-deprecation-tracker.md
│   ├── macro-hygiene-reviewer.md
│   └── memory-pattern-analyzer.md
├── commands/
│   ├── explore.md
│   ├── health.md
│   ├── hotspots.md
│   └── map.md
└── scripts/
    ├── analyze_includes.py
    ├── check_pep7.py
    ├── measure_c_complexity.py
    ├── scan_error_paths.py
    ├── scan_gil_usage.py
    ├── scan_null_checks.py
    └── scan_refcounts.py
```

## Comparison with code-review-toolkit

| Dimension | code-review-toolkit | cpython-review-toolkit |
|-----------|--------------------|-----------------------|
| **Language** | Python | C (CPython source) |
| **Parsing** | Python `ast` module | Regex-based |
| **Root detection** | `pyproject.toml`, `.git` | `Include/Python.h`, `Objects/object.c` |
| **Top bug class** | Logic errors, dead code | Refcount leaks, NULL deref, GIL violations |
| **Style guide** | PEP 8 | PEP 7 |
| **Agents** | 14 | 10 |
| **Scripts** | 8 | 7 |

## Author

Danzin
