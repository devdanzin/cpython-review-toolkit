# Run context -- informed-explore, slice `obj-mappings`

**Slice:** Objects: dict + set -- tier A
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Size:** 2 files, 11,825 lines
**Scope:** `Objects` -- **WIDER THAN THIS SLICE.** No single path covers the slice exactly, so `Objects` will pull in files owned by other slices. Review only the files listed below (also in `preflight/slice_files.txt`); anything you notice outside them belongs to another slice's pass -- note it in one line and leave it there.

**Why this slice:** Both already yielded on shallow sweeps (CPY-0015, CPY-0019) with 11,825 lines never read. Iterator invalidation and user __hash__ re-entry. INHERITS FROM obj-typeobject PASS 2 -- start here, these are already reproduced: (1) CPY-0096, insert_split_key holds keys->dk_mutex (non-reentrant, _Py_LOCK_DONT_DETACH) and calls _PyType_Modified_Unlocked, which runs arbitrary Python via a watcher callback and sys.unraisablehook -- 24/24 FT hang, 0/12 GIL, gdb-proven, and dictobject.c:218-227 EXPLICITLY forbids this, naming PyType_Modified. Its THREE further consequences reproduce on the DEFAULT GIL build and belong to this slice: an ASan heap-buffer-overflow, a dk_usable invariant break 5/5 from a single re-entry, and unbounded recursion -> SIGSEGV. (2) CPY-0107, _Py_dict_lookup:1385 -- compare_unicode_generic runs PyObject_RichCompareBool under LOCK_KEYS; the only lock-order inversion found tree-wide, and the prohibiting comment (bef570622263, 2026-06-25) was written 26 MONTHS after the violating code (d5df25268b03) and never applied backwards. (3) CPY-0079 needs re-running: its clean OOM sweep had a denominator of 2.

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
| `Objects/dictobject.c` | 8,597 | CPY-0015, CPY-0019, CPY-0079, CPY-0096, CPY-0107, CPY-0115, TSAN-0053, gh-152107, gh-154318 |
| `Objects/setobject.c` | 3,228 | TSAN-0054 |

## Pre-run scanner baseline (already executed)

Raw JSON in `<run>/scanners/`. `<scanner>.sample.json` is scoped to this slice -- **read that one**. The `Objects/` column is the whole directory, there to answer one question: is this rule noisy *here*, or noisy everywhere? A rule that fires 19 times in the slice and 19 times in the tree is telling you something different from one that fires 19 times out of 400.

| scanner | Objects/ | slice | slice finding types |
|---|---|---|---|
| `scan_recursion_guards` | 61 | **25** | missing_recursion_guard 25 |
| `scan_ft_races` | 58 | **20** | atomic_plain_asymmetry 17, guarded_writer_unguarded_reader 2, publish_before_init_complete 1 |
| `scan_error_paths` | 38 | **4** | alloc_null_no_memerror 2, unconditional_pyerr_clear 2 |
| `scan_deprecated_apis` | 141 | **3** | deprecated-api 2, gc-untrack-macro-form 1 |
| `scan_memory_patterns` | 7 | **1** | gc_untrack_without_track 1 |
| `scan_null_checks` | 4 | **1** | unchecked_alloc 1 |
| `scan_gil_usage` | 16 | 0 | -- |
| `scan_init_bypass` | 6 | 0 | -- |
| `scan_lock_discipline` | 0 | 0 | -- |
| `scan_pyerr_clear` | 10 | 0 | -- |
| `scan_refcounts` | 20 | 0 | -- |
| `scan_stw_safety` | 21 | 0 | -- |
| `scan_uninit_dealloc` | 3 | 0 | -- |

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

When the slice is finished, set `slices.obj-mappings.status` to `done` in `plugins/cpython-review-toolkit/data/review_slices.json` and commit it -- that is the campaign's cursor.
