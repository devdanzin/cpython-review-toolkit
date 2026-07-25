# Run context -- informed-explore, slice `obj-typeobject`

**Slice:** Objects: type machinery -- tier A
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Size:** 1 files, 13,068 lines
**Scope:** `Objects/typeobject.c` -- covers exactly this slice.

> **This slice needs 2 passes.** It is a single file above the 13,000-line cap and cannot be split by file. Split it by subsystem -- see the slice notes -- and run one informed-explore per pass, each scoped to its subsystem by function, not by line range.

**Why this slice:** TWO-PASS (13,068 lines, one file): pass 1 slot wiring / tp_new / tp_init / __new__ bypass; pass 2 MRO, super, inheritance, watchers. CPY-0007 came from here via a sweep. PASS 1 DONE 2026-07-25 (16 agents, 15 FIX, recorded as CPY-0068..0090 + CPY-0007 widened). PASS 2 NOT STARTED: MRO C3 3217-3702, lookup cache 6140-6452, getattro/setattro 6529-6848, __class__ assignment 7482-7846, pickle 7848-8406, watchers/versions 971-1481, managed static types 228-522, super beyond the construction surface. History says 4 of the 6 most crash-fix-dense regions are in pass 2. PASS 2 ALSO OWES THE #28 RE-RUN: pass 1 ran before the nine recall rules (PR #29) existed and several were built from its own misses, so re-running the scanners on this file yields 32 new-rule candidates -- 14 of which independently recover CPY-0068/0070/0071/0072/0087, and 18 of which are NET NEW and unreviewed. The table is at reports/obj-typeobject/preflight/PASS2_BASELINE.md (untracked, regenerate with the scanners if missing). 13 of the 18 are borrowed_field_deref_across_call, so triage that class as a group: the discriminator is whether the field can actually be cleared re-entrantly from that window, and lookup_tp_dict sites are the likeliest ACCEPTABLE.

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
| `Objects/typeobject.c` | 13,068 | CPY-0007, CPY-0068, CPY-0069, CPY-0070, CPY-0071, CPY-0072, CPY-0073, CPY-0074, CPY-0075, CPY-0076, CPY-0077, CPY-0078, CPY-0080, CPY-0081, CPY-0082, CPY-0083, CPY-0084, CPY-0085, CPY-0086, CPY-0087, CPY-0088, CPY-0089, CPY-0090 |

## Pre-run scanner baseline (already executed)

Raw JSON in `<run>/scanners/`. `<scanner>.sample.json` is scoped to this slice -- **read that one**. The `Objects/` column is the whole directory, there to answer one question: is this rule noisy *here*, or noisy everywhere? A rule that fires 19 times in the slice and 19 times in the tree is telling you something different from one that fires 19 times out of 400.

| scanner | Objects/ | slice | slice finding types |
|---|---|---|---|
| `scan_refcounts` | 2 | **18** | borrowed_field_deref_across_call 18 |
| `scan_recursion_guards` | 50 | **8** | missing_recursion_guard 8 |
| `scan_error_paths` | 37 | **6** | unconditional_pyerr_clear 5, int_status_never_tested 1 |
| `scan_ft_races` | 54 | **5** | publish_before_init_complete 3, guarded_writer_unguarded_reader 1, lazy_init_no_critical_section 1 |
| `scan_init_bypass` | 3 | **4** | init_bypass_null_deref 3, vararg_null_truncation 1 |
| `scan_memory_patterns` | 4 | **2** | varobject_nitems_unguarded 2 |
| `scan_deprecated_apis` | 141 | **1** | deprecated-api 1 |
| `scan_pyerr_clear` | 10 | **1** | pyerr_clear_unfiltered_after_python_call 1 |
| `scan_gil_usage` | 16 | 0 | -- |
| `scan_lock_discipline` | 0 | 0 | -- |
| `scan_null_checks` | 1 | 0 | -- |
| `scan_stw_safety` | 13 | 0 | -- |
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

When the slice is finished, set `slices.obj-typeobject.status` to `done` in `plugins/cpython-review-toolkit/data/review_slices.json` and commit it -- that is the campaign's cursor.
