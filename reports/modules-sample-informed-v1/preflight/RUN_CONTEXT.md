# Run context — informed-explore, `Modules/` sample

**Toolkit:** cpython-review-toolkit **v0.8.0** (post-correctness-release)
**Plugin root:** `/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit`
**Target:** `/home/danzin/projects/cpython` — main @ `4f3be1b5777`, 3.16.0a0, full clone
**Run dir:** `/home/danzin/projects/cpython-review-toolkit/reports/modules-sample-informed-v1`
**Env:** `source /home/danzin/venvs/cpython-review-toolkit/bin/activate`
**Builds:** in-tree `./python` is ASan+debug (from `583cb03da83` — older than HEAD; note the divergence rather than reconciling it, and its `_sre` is stale so `re`/`xml`/`traceback` fail to import). Prefer the matrix:
`/home/danzin/projects/python_build_matrix/builds/<name>/python` — `debug-gil-nojit`, `debug-gil-nojit-asan`, `debug-ft-nojit`, `debug-ft-nojit-asan`, `debug-ft-nojit-tsan`, `release-*`. Those are at `a1d580430c8`; **diff the files you cite** between that and `4f3be1b5777` and say whether they are identical.

## Purpose

Equal parts **bug hunt** and **toolkit feedback**. The previous run (`Objects/` sample) drove a 23-defect cleanup; this run tests whether the rebuilt scanners hold up on *different* code. Two deliverables:

1. **Findings** — triaged FIX / CONSIDER / POLICY / ACCEPTABLE, reproduced where possible.
2. **Toolkit assessment** — precision per rule, recall gaps found by reading, and concrete tuning
   proposals. A confident negative is a real result; do not inflate.

## Scope — 12 files, ~39,800 lines of `Modules/`

The stdlib C-accelerator family. **Calibration subset** (carries catalog entries — measures recall):

| file | lines | catalog |
|---|---|---|
| `Modules/_collectionsmodule.c` | 2906 | OOM-0039 `deque_clear`, gh-152951 `deque_extend` |
| `Modules/itertoolsmodule.c` | 4241 | TSAN-0006 `count_repr` |
| `Modules/_elementtree.c` | 4573 | CPY-0016 `create_elementiter` (reproduced SIGSEGV) |

**New territory** (no prior review, no catalog entries — where novel findings live):

| file | lines | why |
|---|---|---|
| `Modules/_pickle.c` | 8298 | memo tables + `__reduce__` callbacks re-entering during pickling — dense borrowed-ref surface |
| `Modules/_datetimemodule.c` | 7944 | has a shipped pure-Python twin (`_pydatetime`) → free differential oracle |
| `Modules/_struct.c` | 2960 | format parsing, sized buffer writes |
| `Modules/_zoneinfo.c` | 2824 | binary TZif parsing, caches |
| `Modules/_json.c` | 2099 | scanner/encoder recursion over user containers |
| `Modules/_csv.c` | 1865 | dialect state, parser buffers |
| `Modules/_heapqmodule.c` | 813 | user `__lt__` can mutate the heap mid-sift — classic borrowed-ref/OOB shape |
| `Modules/_randommodule.c` | 683 | small, seldom reviewed |
| `Modules/_queuemodule.c` | 641 | locks, FT-relevant |

## Pre-run scanner baseline (already executed)

Raw JSON in `<run>/scanners/`: `<scanner>.Modules.json` (all of `Modules/`) and
**`<scanner>.sample.json` (pre-filtered to the 12 files — read this one)**.

| scanner | Modules/ | sample | sample types |
|---|---|---|---|
| scan_ft_races | 58 | **16** | atomic_plain_asymmetry 5, iternext_double_decref 6, lazy_init_no_critical_section 5 |
| scan_deprecated_apis | 137 | 12 | deprecated-api 12 (10 in `_pickle.c`) |
| scan_error_paths | 61 | 5 | unconditional_pyerr_clear 3, alloc_null_no_memerror 2 |
| scan_recursion_guards | 13 | 5 | missing_recursion_guard 4, guarded_by_dispatcher 1 |
| scan_refcounts | 4 | **2** | **borrowed_ref_across_call 2** (both `itertoolsmodule.c`) |
| scan_pyerr_clear | 21 | 2 | pyerr_clear_in_dealloc 1, unfiltered_after_python_call 1 |
| scan_uninit_dealloc | 1 | 1 | dealloc_of_uninitialized_object 1 (`_elementtree.c`) |
| scan_gil_usage | 68 | 0 | — |
| scan_init_bypass | 24 | 0 | — |
| scan_null_checks | 13 | 0 | — |
| scan_memory_patterns | 2 | 0 | — |
| scan_stw_safety | 5 | 0 | — |
| scan_lock_discipline | 0 | 0 | — |

**If your scanner returned 0 on the sample**, your job shifts to **recall**: read the relevant code
by hand for your bug class and report either "genuinely clean, here is the evidence" or "the scanner
missed X — here is the pattern and the rule change". The v0.8 cleanup made several scanners much
quieter; confirming a zero is *earned* rather than accidental is exactly the feedback this run wants.
Where a scanner exposes a denominator field (`total_nullable_fields`, `mutex_functions`,
`vocabulary_counts`, `parse_health`), check it — a zero with a zero denominator is silence, not
safety.

## Informed mode

Read `<run>/preflight/informed_briefing.md` first: 6 recurring bug SHAPES each with its **guarded
twin**, the cross-cutting triage rules, the FP taxonomy, and **29 previously-recorded findings**
from `cpython-review-findings`.

1. **Confirm, don't re-litigate** a catalog finding — one line, then move on.
2. **Skip the known FP classes**; if you flag one anyway, justify why *this* instance differs.
3. **Hunt siblings via the guarded twin** — find the correctly-handled sibling in the same
   file/family, then the sites that lack it. New siblings and new territory are the goal.
   **Chase scope escapes** out of the 12 files if a shape leads there — two of the best findings
   last run (`dictobject.c:8427`, `listobject.c:250`) lay just outside the boundary.

## Output

Write to `<run>/agents/<your-agent-name>.md`:

```markdown
# <agent> — Modules/ sample (informed)
## Scanner volume
raw: N | confirmed: N | dismissed: N | precision: N%
## Findings          (FIX / CONSIDER / POLICY+ACCEPTABLE grouped)
Each: `file:line` · function · what breaks · **guarded twin** · how Python input reaches it · class + why
## New siblings of known shapes
## Classes bounded (clean negatives, with evidence)
## Toolkit assessment   ← REQUIRED
precision per rule · recall gaps found by reading · prompt problems · ranked concrete tuning proposals
```

Reproduce where you can and paste **real** transcripts (exit code + ASan/TSan/gdb frames). Never
claim a crash you did not observe. Also print a digest under 400 words in your final message.
