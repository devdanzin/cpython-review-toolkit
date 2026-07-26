# Run context -- informed-explore, slice `obj-sequences`

**Slice:** Objects: sequence types -- tier A
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Size:** 4 files, 12,100 lines
**Scope:** `Objects` -- **WIDER THAN THIS SLICE.** No single path covers the slice exactly, so `Objects` will pull in files owned by other slices. Review only the files listed below (also in `preflight/slice_files.txt`); anything you notice outside them belongs to another slice's pass -- note it in one line and leave it there.

**Why this slice:** Resize-during-buffer-export is the _struct CPY-0044 shape in another guise. User __eq__/__lt__ re-entry during list ops is the other.

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
| `Objects/listobject.c` | 4,312 | CPY-0014, CPY-0153 |

**New territory** -- no prior review, no catalog entries. Novel findings live here:

| file | lines |
|---|---|
| `Objects/bytesobject.c` | 3,925 |
| `Objects/bytearrayobject.c` | 3,125 |
| `Objects/bytes_methods.c` | 738 |

## Pre-run scanner baseline (already executed)

Raw JSON in `<run>/scanners/`. `<scanner>.sample.json` is scoped to this slice -- **read that one**. The `Objects/` column is the whole directory, there to answer one question: is this rule noisy *here*, or noisy everywhere? A rule that fires 19 times in the slice and 19 times in the tree is telling you something different from one that fires 19 times out of 400.

| scanner | Objects/ | slice | slice finding types |
|---|---|---|---|
| `scan_deprecated_apis` | 141 | **10** | deprecated-api 10 |
| `scan_ft_races` | 60 | **6** | atomic_plain_asymmetry 4, guarded_writer_unguarded_reader 1, lazy_init_no_critical_section 1 |
| `scan_error_paths` | 56 | **3** | unconditional_pyerr_clear 3 |
| `scan_pyerr_clear` | 10 | **3** | pyerr_clear_unfiltered_after_python_call 3 |
| `scan_init_bypass` | 6 | **2** | init_bypass_null_deref 2 |
| `scan_recursion_guards` | 61 | **1** | recursion_descent_guarded_by_dispatcher 1 |
| `scan_uninit_dealloc` | 3 | **1** | dealloc_of_uninitialized_object 1 |
| `scan_gil_usage` | 16 | 0 | -- |
| `scan_lock_discipline` | 0 | 0 | -- |
| `scan_memory_patterns` | 7 | 0 | -- |
| `scan_null_checks` | 4 | 0 | -- |
| `scan_refcounts` | 21 | 0 | -- |
| `scan_stw_safety` | 21 | 0 | -- |

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

When the slice is finished, set `slices.obj-sequences.status` to `done` in `plugins/cpython-review-toolkit/data/review_slices.json` and commit it -- that is the campaign's cursor.
