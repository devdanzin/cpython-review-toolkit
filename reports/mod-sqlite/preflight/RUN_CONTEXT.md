# Run context -- informed-explore, slice `mod-sqlite`

**Slice:** Modules: _sqlite package -- tier A
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope argument:** `Modules/_sqlite`
**Size:** 9 files, 6,570 lines

**Why this slice:** CPY-0066 (row hash recursion) came from row.c -- 307 lines of the package's 6,570. Converters and authorizer callbacks re-enter Python mid-statement.

## Purpose

Equal parts **bug hunt** and **toolkit feedback**. Two deliverables:

1. **Findings** -- triaged FIX / CONSIDER / POLICY / ACCEPTABLE, reproduced where possible.
2. **Toolkit assessment** -- precision per rule, recall gaps found by reading that the scanner missed (the most valuable output), and concrete tuning proposals.

A confident **negative** ("this class is clean here, and here is why") is a real result. Do not inflate.

**Check the denominator before reporting a clean negative.** A zero next to a zero denominator is silence, not safety -- four v0.8 rules produced structural zeros that were certified as clean. Every `*.sample.json` below was produced by re-running the scanner over exactly these files, so its denominators are slice-scoped and the canary works.

## Files in this slice

**Calibration subset** -- these carry catalog entries, so they measure *recall*:

| file | lines | prior entries |
|---|---|---|
| `Modules/_sqlite/connection.c` | 2,750 | gh-152954, gh-153800 |
| `Modules/_sqlite/cursor.c` | 1,448 | gh-152817 |
| `Modules/_sqlite/row.c` | 307 | CPY-0066 |

**New territory** -- no prior review, no catalog entries. Novel findings live here:

| file | lines |
|---|---|
| `Modules/_sqlite/module.c` | 803 |
| `Modules/_sqlite/blob.c` | 638 |
| `Modules/_sqlite/statement.c` | 219 |
| `Modules/_sqlite/util.c` | 186 |
| `Modules/_sqlite/microprotocols.c` | 143 |
| `Modules/_sqlite/prepare_protocol.c` | 76 |

## Pre-run scanner baseline (already executed)

Raw JSON in `<run>/scanners/`. `<scanner>.sample.json` is scoped to this slice -- **read that one**. The `Modules/` column is the whole directory, there to answer one question: is this rule noisy *here*, or noisy everywhere? A rule that fires 19 times in the slice and 19 times in the tree is telling you something different from one that fires 19 times out of 400.

| scanner | Modules/ | slice | slice finding types |
|---|---|---|---|
| `scan_init_bypass` | 26 | **4** | init_bypass_null_deref 4 |
| `scan_ft_races` | 97 | **2** | lazy_init_no_critical_section 1, iternext_double_decref 1 |
| `scan_gil_usage` | 68 | **1** | api_without_gil 1 |
| `scan_recursion_guards` | 13 | **1** | missing_recursion_guard 1 |
| `scan_deprecated_apis` | 138 | 0 | -- |
| `scan_error_paths` | 70 | 0 | -- |
| `scan_lock_discipline` | 0 | 0 | -- |
| `scan_memory_patterns` | 2 | 0 | -- |
| `scan_null_checks` | 13 | 0 | -- |
| `scan_pyerr_clear` | 25 | 0 | -- |
| `scan_refcounts` | 8 | 0 | -- |
| `scan_stw_safety` | 5 | 0 | -- |
| `scan_uninit_dealloc` | 1 | 0 | -- |

## Informed mode

`preflight/informed_briefing.md` is in this run directory. Every agent reads it first, then triages in informed mode:

1. **Confirm, don't re-litigate** a previously-recorded finding -- one line, move on.
2. **Skip the false-positive classes** in the taxonomy; flagging one anyway requires justifying why *this* instance is not that class.
3. **Hunt siblings**: for each finding, name its **guarded twin** (the correctly-handled sibling = the fix) and search for the other sites that lack it.

## Environment

```
source ~/venvs/cpython-review-toolkit/bin/activate
```
Builds: `~/projects/python_build_matrix/builds/<name>/python` -- `debug-gil-nojit`, `debug-ft-nojit`, `release-gil-nojit`, plus `-asan` / `-tsan` variants. **Diff any file you cite** between the matrix build's commit and the target ref, and say whether they are identical.

Traps, all previously measured:

- Ambient `python` is RustPython -- silently produces empty output. Name an explicit interpreter.
- Debug builds can *hide* bugs: CPY-0049's disclosure only reaches its site on release. Check crash clusters on both.
- `gh search issues` silently returns nothing here. Use `gh api -X GET search/issues -f q='repo:python/cpython <terms>'`.
- Heredoc-to-stdin mangles multi-line Python. Write a `.py` file and run it.

## Recording

Findings repo: `/home/danzin/projects/cpython-review-findings` -- one directory per finding under `reports/`, `meta.json` is the only source of truth (`INDEX.md` and `catalog/known_bugs.tsv` are generated). Check for prior art with the `gh api` form above **before** calling anything novel.

When the slice is finished, set `slices.mod-sqlite.status` to `done` in `plugins/cpython-review-toolkit/data/review_slices.json` and commit it -- that is the campaign's cursor.
