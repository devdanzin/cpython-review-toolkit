# Run context -- informed-explore, slice `mod-io`

**Slice:** Modules: _io package -- tier A
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Size:** 7 files, 11,924 lines
**Scope:** `Modules/_io` -- **WIDER THAN THIS SLICE.** No single path covers the slice exactly, so `Modules/_io` will pull in files owned by other slices. Review only the files listed below (also in `preflight/slice_files.txt`); anything you notice outside them belongs to another slice's pass -- note it in one line and leave it there.
**Differential oracle:** `Lib/_pyio.py` -- a shipped pure-Python twin. Feed the same adversarial input to both and compare exit codes; a C-side SIGSEGV/SIGABRT where the twin raises cleanly is a confirmed, localized bug.

**Why this slice:** Shipped pure-Python twin is a free differential oracle. Buffer-protocol dense; detach/close re-entrancy and raw-buffer resize are the shapes to hunt.

## Purpose

Equal parts **bug hunt** and **toolkit feedback**. Two deliverables:

1. **Findings** -- triaged FIX / CONSIDER / POLICY / ACCEPTABLE, reproduced where possible.
2. **Toolkit assessment** -- precision per rule, recall gaps found by reading that the scanner missed (the most valuable output), and concrete tuning proposals.

A confident **negative** ("this class is clean here, and here is why") is a real result. Do not inflate.

**Check the denominator before reporting a clean negative.** A zero next to a zero denominator is silence, not safety -- four v0.8 rules produced structural zeros that were certified as clean. Every `*.sample.json` below was produced by re-running the scanner over exactly these files, so its denominators are slice-scoped and the canary works.

## Files in this slice

**New territory** -- no prior review, no catalog entries. Novel findings live here:

| file | lines |
|---|---|
| `Modules/_io/textio.c` | 3,523 |
| `Modules/_io/bufferedio.c` | 2,788 |
| `Modules/_io/bytesio.c` | 1,356 |
| `Modules/_io/fileio.c` | 1,349 |
| `Modules/_io/stringio.c` | 1,106 |
| `Modules/_io/iobase.c` | 1,055 |
| `Modules/_io/_iomodule.c` | 747 |

> Every file in this slice is new territory -- there is no recall measurement available. Weight the toolkit assessment toward *what you found by reading that the scanners missed*.

## Pre-run scanner baseline (already executed)

Raw JSON in `<run>/scanners/`. `<scanner>.sample.json` is scoped to this slice -- **read that one**. The `Modules/` column is the whole directory, there to answer one question: is this rule noisy *here*, or noisy everywhere? A rule that fires 19 times in the slice and 19 times in the tree is telling you something different from one that fires 19 times out of 400.

| scanner | Modules/ | slice | slice finding types |
|---|---|---|---|
| `scan_init_bypass` | 37 | **26** | init_bypass_null_deref 26 |
| `scan_ft_races` | 102 | **12** | guarded_writer_unguarded_reader 10, publish_before_init_complete 1, lazy_init_no_critical_section 1 |
| `scan_deprecated_apis` | 138 | **2** | deprecated-api 2 |
| `scan_error_paths` | 71 | **1** | unconditional_pyerr_clear 1 |
| `scan_null_checks` | 22 | **1** | unchecked_alloc 1 |
| `scan_pyerr_clear` | 25 | **1** | pyerr_clear_unfiltered_after_python_call 1 |
| `scan_gil_usage` | 68 | 0 | -- |
| `scan_lock_discipline` | 0 | 0 | -- |
| `scan_memory_patterns` | 6 | 0 | -- |
| `scan_recursion_guards` | 17 | 0 | -- |
| `scan_refcounts` | 9 | 0 | -- |
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

When the slice is finished, set `slices.mod-io.status` to `done` in `plugins/cpython-review-toolkit/data/review_slices.json` and commit it -- that is the campaign's cursor.
