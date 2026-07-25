# Run context — informed-explore, Objects/ sample

**Toolkit:** cpython-review-toolkit v0.7.0
**Plugin root:** `/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit`
**Target:** `/home/danzin/projects/cpython` — CPython 3.16.0a0, main @ `4f3be1b5777`, full (non-shallow) clone
**Run dir:** `/home/danzin/projects/cpython-review-toolkit/reports/objects-sample-informed-v1`
**Venv:** `source /home/danzin/venvs/cpython-review-toolkit/bin/activate` (python3.12 + tree_sitter + tree_sitter_c)

## Purpose of this run

This is **primarily a toolkit assessment and calibration run**, and secondarily a bug hunt.
Two deliverables, both required:

1. **Findings** — real bugs in the sample, triaged FIX / CONSIDER / POLICY / ACCEPTABLE.
2. **Toolkit assessment** — how well *your scanner and your agent prompt* performed:
   precision (how many candidates were real), recall gaps (bugs you found by reading that
   the scanner missed — these are the most valuable output), noisy rules, and concrete
   tuning proposals (a regex, a gate, a new finding `type`, a data-file entry).

A confident **negative** ("this class is clean in this scope, here is why") is a real
result and must be reported. Do not inflate.

## Scope — the 14-file sample (Objects/, 13,250 lines, 28% of Objects/ files)

**Calibration subset** (these carry catalog entries — measure *recall* against them):

| file | lines | catalog entries |
|---|---|---|
| `Objects/tupleobject.c` | 1302 | CPY-0001 / gh-154318 (tuple_hash, tuple_richcompare), gh-149146 (tuple_dealloc) |
| `Objects/genericaliasobject.c` | 1070 | CPY-0002 / gh-154275 (`_Py_make_parameters`:231) |
| `Objects/unionobject.c` | 601 | gh-154318 sibling (union_hash) |
| `Objects/templateobject.c` | 416 | gh-151815 (`template_iter`:225) |
| `Objects/descrobject.c` | 2096 | TSAN-0043 (`descr_get_qualname` lazy-init) |

**New-territory subset** (no prior review, no catalog entries — this is where novel findings live):

| file | lines | why included |
|---|---|---|
| `Objects/odictobject.c` | 2385 | linked-list + dict duality, refcount-dense, historically fragile |
| `Objects/funcobject.c` | 1929 | function versioning, FT-relevant caches |
| `Objects/weakrefobject.c` | 1143 | callback re-entrancy — classic UAF territory |
| `Objects/structseq.c` | 800 | alloc + late member init (uninit-dealloc shape) |
| `Objects/iterobject.c` | 542 | iterator double-DECREF (the FT race shape) |
| `Objects/capsule.c` | 366 | raw void* pointer handling |
| `Objects/interpolationobject.c` | 231 | new in 3.14 (t-strings), least-reviewed |
| `Objects/cellobject.c` | 212 | small, closure cells |
| `Objects/lazyimportobject.c` | 157 | newest type in the tree — least-reviewed code in Objects/ |

## Pre-run scanner baseline (already executed for you)

Every scanner was run once over **all of `Objects/`**. Raw JSON is in `<run>/scanners/`:
- `<scanner>.Objects.json` — full Objects/ output
- `<scanner>.sample.json` — **pre-filtered to the 14 sample files** (read this one)

| scanner | Objects/ | sample |
|---|---|---|
| scan_error_paths | 148 | 29 |
| scan_null_checks | 113 | 21 |
| scan_refcounts | 98 | 19 |
| scan_ft_races | 24 | 6 |
| scan_stw_safety | 13 | 0 |
| scan_recursion_guards | 9 | 4 |
| scan_gil_usage | 8 | 0 |
| scan_uninit_dealloc | 7 | 2 |
| scan_memory_patterns | 4 | 1 |
| scan_lock_discipline | 1 | 0 |
| scan_pyerr_clear | **0** | 0 |
| scan_init_bypass | **0** | 0 |

You may re-run your scanner (per-file or on `Objects/`) if you want different flags — but the
pre-filtered JSON is authoritative for the sample and saves you a run.

**If your scanner returned 0 on the sample**: your job shifts to recall. Read the relevant code
in the sample by hand for your bug class and report either "genuinely clean, here is the evidence"
or "the scanner missed X — here is the pattern and the fix to the scanner". Note especially that
`scan_pyerr_clear` returns 0 across *all* of Objects/ while the known-bugs catalog lists
`OOM-0023 Objects/typeobject.c subtype_dealloc` as a pyerr-clear entry — someone should determine
whether that is a fix upstream or a scanner recall gap.

## Informed mode — read this first

Read `<run>/preflight/informed_briefing.md` before triaging. It carries the 6 recurring CPython
bug SHAPES (each with its **guarded twin** = the fix), the 5 cross-cutting triage rules, the
2 previously-recorded findings, and the false-positive taxonomy.

Your three informed-mode rules:
1. **Confirm, don't re-litigate** a catalog finding — one line, then move on.
2. **Skip the known FP classes**; if you flag one anyway, justify why *this* instance differs.
3. **Hunt siblings via the guarded twin** — for each shape, find the correctly-handled sibling
   in the same file/family, then find the other sites that lack it. New siblings and new
   territory are the goal.

## Output

Write your report to `<run>/agents/<your-agent-name>.md` using this structure:

```markdown
# <agent name> — Objects/ sample (informed)

## Scanner volume
raw candidates in sample: N   |   confirmed: N   |   dismissed: N   |   precision: N%

## Findings
### FIX
### CONSIDER
### POLICY / ACCEPTABLE  (one line each, grouped)

Each finding: `file:line` · function · what breaks · **guarded twin** (the correct sibling) ·
how a Python-level input reaches it · classification + why.

## New siblings of known shapes
(the fix-propagation yield — the highest-value section)

## Classes bounded (clean negatives)
(what you checked and found clean, with the evidence)

## Toolkit assessment  ← REQUIRED
- **Precision**: which scanner rules produced FPs, and the FP class for each.
- **Recall gaps**: bugs/shapes you found by reading that the scanner did not surface.
  Include the source snippet and why the current rule misses it.
- **Prompt issues**: anything in your agent definition that misdirected you.
- **Concrete tuning proposals**: specific, implementable (a regex, a gate, a new `type`
  string, a `data/*.json` entry, a doc line). Ranked by value.
```

Also print a short summary in your final message — the orchestrator reads that, not the file.
