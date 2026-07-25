# Toolkit cleanup — final results

All 23 defects from [`TOOLKIT_ASSESSMENT.md`](TOOLKIT_ASSESSMENT.md) are fixed, across **6 repos**.
Full detail in the repo `CHANGELOG.md` under `[0.8.0]`. This file is the measurement record.

## Test suites — all green

| repo | tests | note |
|---|---|---|
| cpython-review-toolkit | **556** | was 243 |
| cext-review-toolkit | 309 | upstream for the shared chassis (+23 chassis tests) |
| ft-review-toolkit | 144 | chassis synced |
| rust-ext-review-toolkit | 242 | `analyze_history` fixes |
| rustpy-review-toolkit | 117 | `analyze_history` fixes |
| code-review-toolkit | 300 | `analyze_history` fixes |
| pyo3-review-toolkit | 355 | `analyze_history` fixes |

## Scanner volume — `Objects/` before → after

| scanner | before | after | what changed |
|---|---|---|---|
| `scan_error_paths` | 148 | **33** | off-by-one fixed; rule re-scoped + gated |
| `scan_null_checks` | 113 | **1** | positive-danger requirement; 6 FP classes killed |
| `scan_refcounts` | 426+152+23+24+14 = **639** (tree-wide) | **7** (tree-wide) | 5 noise rules → 0; crown jewel implemented |
| `check_pep7` | 5,736 | **64** | 1 rule deleted, 2 gated behind `--diff-only` |
| `scan_uninit_dealloc` | 8 | **3** | predicate inverted, dominator-aware |
| `scan_lock_discipline` | 1 (an FP) | **0** | FP suppressed; `PyMutex` family now loads |
| `scan_deprecated_apis` | 13 (0 real) | **113** | data-driven, definition sites suppressed |
| `scan_init_bypass` | 0 | **2** | positional slot form now parsed |
| `scan_memory_patterns` | 4 | **2**(+new rule) | var-object rule added; taint split |

## Line accuracy — measured by `tools/validate_precision.py`

Across `Objects/` + `Modules/` + `Python/`. "Line accuracy" = the reported line (±1) actually
carries the construct the finding describes.

| scanner | findings | line accuracy |
|---|---|---|
| `scan_pyerr_clear` | 51 | **100%** |
| `scan_uninit_dealloc` | 4 | **100%** |
| `scan_init_bypass` | 27 | **100%** |
| `scan_refcounts` | 7 | **100%** |
| `scan_memory_patterns` | 9 | **100%** |
| `scan_gil_usage` | 136 | **100%** |
| `scan_stw_safety` | 86 | 97.7% |
| `scan_error_paths` | 184 | 96.7% |
| `scan_recursion_guards` | 52 | 95.3% |
| `scan_ft_races` | 180 | 92.8% |
| `scan_null_checks` | 14 | 78.6% |
| `scan_lock_discipline` | 0 | n/a (denominator-backed zero) |

Before the cleanup the same measure was **1.8%** for `scan_null_checks` (2/113) and **7.1%** for
`scan_error_paths` (8/113).

> **Caveat on the residual sub-100% figures — read this before quoting them.** The metric is a
> *proxy*: it extracts identifiers from a finding's own `detail` prose and checks the source line
> for them. I got that heuristic wrong three times in a row and each time it produced a *false
> indictment* of a correct scanner — first expecting `dealloc_of_uninitialized_object` on the
> `Py_DECREF` line when it correctly points at the **allocation** (scored 0%); then keying on the
> enclosing function name, which never appears on the defect line (scored `scan_recursion_guards`
> 3.9%, `scan_gil_usage` 2.2%); then failing to match quoted member expressions like
> `'descr->d_qualname'` (scored `scan_ft_races` 63%). Every sampled "miss" I inspected by hand was
> the harness being wrong, not the scanner. Treat the remaining gaps as *unverified*, not as
> defects.

## New bugs found by the repaired scanners

The strongest evidence the fixes work — these were found *after* the cleanup, on code the review run
never examined. Recorded in [`FINDINGS.md`](FINDINGS.md) as F13–F15.

- **`Objects/typeobject.c:12747`** — `super.__new__(super).__get__(1)` → SIGSEGV. Verified on main
  and on released 3.14, so longstanding, not a regression. Corrects the review's earlier
  ACCEPTABLE verdict on `super`.
- **`Modules/_io/winconsoleio.c:957`** — unchecked allocation dereferenced on the next line;
  guarded twin 65 lines below at `:1022`.
- **`Objects/dictobject.c:5662`** — second `gc-untrack-macro-form` true positive (2 of ~80 macro
  uses fire; both real).
- **`Modules/_elementtree.c:2367`** — uninit-dealloc via a scalar member, recovered by adding a
  second evidence tier.
- **`Objects/structseq.c:242`** — the sibling I had wrongly marked unreproducible; it needs the
  optional dict argument (`os.terminal_size((7,9), {'zzz': 1})` → ASan SEGV) and needs **no integer
  overflow at all**.

## Dynamic confirmations during the cleanup

- **TSan** (FT+TSan build): `data race Objects/genericaliasobject.c:583 in ga_getitem` ×2 and
  `:419 in _Py_subs_parameters` ×2, with the other side being the *guarded* accessor
  `ga_parameters_lock_held` — the "a critical section held by one of two accessors serializes
  nothing" claim, reproduced live.
- **FT+ASan**: `SEGV` → `Py_DECREF` → `ga_iternext` under `PYTHON_GIL=0`.
- **OOM A/B** proving the `--setup` fix: setup inlined → `{memory_error: 25}` (no crash); setup
  moved to `--setup` → `abort` at K=2 with
  `_PyObject_GC_UNTRACK: Assertion "_PyObject_GC_IS_TRACKED" failed … odict_iterator`.
- **ASan exit-code fix**: `ctypes.string_at(1,4)` on the ASan build reports SEGV with **exit 1**,
  previously classified as the *safe* `memory_error` outcome.

## Corrections agents made to my briefing (kept, because they were right)

- `PyMem_New` does **not** set `MemoryError` — it is a plain macro over `PyMem_Malloc`. My "exempt"
  list was wrong, and correcting it is what surfaced the one true positive in `frameobject.c:1586`.
- `func-call-space` **does** have a PEP 7 basis; the lexical implementation was the problem.
- `PyDict_GetItem` / `PyMapping_HasKey` are **not** formally deprecated (a "prefer X" note is not a
  `.. deprecated::` directive); `PyWeakref_GetObject` and `PyEval_CallObject` were *removed*, not
  deprecated, so they have zero possible call sites.
- The `ExceptionMatches` gate is **not** the load-bearing one for `PyErr_Clear` widening — ablation
  shows the *Python-reaching* gate is (+24 vs +6 on `Objects/`).
- The brace-depth gate does **not** get the out-param rule to 1/1; failure-branch containment does.
- `strip_comments_and_strings` had a **third** bug nobody had spotted: a string regex running away
  across newlines on the legal char literal `'"'`, silently eating **3,210 of 13,068 lines** in
  `typeobject.c`.
- `Py_TRASHCAN_BEGIN`/`END` are empty shims — they must **not** go in a brace-substitution table.
- Argument Clinic substitution makes the chassis *worse* (`dictobject.c` 292 → 72 functions).

## Known residue (deliberately not fixed)

- **One function still lost tree-wide**: `dictobject.c:insert_split_key`, whose critical-section
  macros sit in separate `#ifdef Py_GIL_DISABLED` blocks. The only fix found costs 105 other
  functions.
- **`bytesobject.c:3451 striter_next`** is a genuine unguarded T3 that `extract_functions` still
  does not extract. Flagged in the agent prompt as a recall gap with a `grep -c tp_iternext`
  cross-check.
- **Type-argument macros** (`PyMem_New(struct _Py_stat_struct, 1)`) still ERROR — 8 occurrences in
  4 files; left rather than adding a fourth macro class unmeasured.
- **`deref_before_check`** ships correct but empty on CPython main; the measured zero is recorded in
  the docstring and prompt so it is never read as an audit result.
- **Popen pipe leak** in the five siblings' `analyze_history.py` (fixed in cpython-review-toolkit).
- `scan_ft_races` R3/R4 (multi-line conditions, Clinic `@critical_section`) documented as recall
  gaps — fixing R3 without R4 would turn every clinic-guarded lazy init into a false positive.

## Not done

Nothing is committed. The repo is at **v0.8.0** in the manifests and CHANGELOG, 66 tracked files
modified, with `data/deprecated_c_apis.json`, `scripts/scan_deprecated_apis.py`,
`tests/test_scan_deprecated_apis.py` and `tools/` as new untracked files.
`docs/python-wrapper-new-without-init.md` and `reports/` are **pre-existing** untracked items and
must not be swept in by a blind `git add -A`.
