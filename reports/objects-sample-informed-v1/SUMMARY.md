# Informed-explore — CPython `Objects/` sample · SUMMARY

**Toolkit:** cpython-review-toolkit v0.7.0 · **Target:** CPython main @ `4f3be1b5777` (3.16.0a0)
**Scope:** 14 files / 13,250 lines (28% of `Objects/` files, ~11% of its lines)
**Agents:** 12 (2 preflight + 10 analysis) · **Scanners:** 12, all 50 files of `Objects/` for baseline
**Date:** 2026-07-24 · **Purpose:** primarily toolkit assessment; bug-finding secondary

Detail lives in [`FINDINGS.md`](FINDINGS.md) and [`TOOLKIT_ASSESSMENT.md`](TOOLKIT_ASSESSMENT.md);
per-agent reports in `agents/`, reproducers and evidence in `repro/`.

---

## Executive summary

The run found **13 FIX-class bugs**, every one reproduced live on an ASan/debug, free-threaded, or
TSan build — including a heap use-after-free in `iter(seq)`, a pure-Python SIGSEGV regression in
`bytearray`, and a 3-line heap-buffer-overflow via `os.terminal_size`. Two were re-reproduced
independently by the orchestrator rather than taken on report.

It also found that **the scanners contributed almost none of that**. Across the three largest
scanners — `error_paths`, `null_checks`, `refcounts` — **0 of 69 candidates were real**. Every
genuine finding came from an agent *reading code* under the guidance of the informed briefing's bug
shapes and guarded-twin method. Three of those scanners' headline rules turned out to be **dead
code or structurally disabled**, and the toolkit's own false-positive taxonomy contained **two
factual errors about CPython**, one of which would have suppressed the entire confirmed recursion
class.

The honest reading: **the methodology is working and the tooling underneath it is not.** The
briefing, the guarded-twin discipline, the sibling hunt and the calibration/new-territory split all
earned their keep. The scanners currently function as a file-prioritisation hint, not as detectors.

## Key metrics

| dimension | result |
|---|---|
| FIX-class bugs, reproduced live | **13** (+ ~10 CONSIDER) |
| Reproduced independently by orchestrator | 2 (`iterobject.c:80`, `bytearray`) |
| Scanner precision, 3 largest scanners | **0 / 69** |
| Findings originating from reading, not scanners | **~100%** |
| Recall on the calibration subset (`known-issues`) | **6 / 6** |
| Fix-completeness: findings with a prior fixing commit | 7 of 9 — **all 7 incomplete** |
| Defects *introduced* by a safety-motivated commit | **3** |
| Toolkit defects found | **23** (4 fixed this session) |
| Whole-`Objects/` scan cost, all 12 scanners | ~8.3 s (performance is a non-issue) |

## The five results that matter most

**1. `Objects/iterobject.c:80` — the crown-jewel shape, live, one-line fix.**
`iter_iternext` holds `it->it_seq` borrowed across `PySequence_GetItem`, which runs arbitrary
Python. A re-entrant `next()` nulls the slot and drops the ref; the outer frame drops its stale
local again. ASan attributes the free to `iterobject.c:80`. The guarded twin — `calliter_iternext`
using `Py_CLEAR` — is **160 lines below in the same file**, and three separate upstream sweeps that
fixed this exact shape elsewhere walked past it.

**2. Fix-completeness is the toolkit's strongest capability, and it is damning.**
7 of 9 findings had a prior fixing commit; **all 7 fixes were incomplete**, and **3 defects were
introduced by the commit meant to improve safety** — including `genericaliasobject.c:302`, added by
a commit titled *"Fix various refleaks"* and then walked past by one titled *"Fix NULL dereference
bugs in genericaliasobject.c"* which removed the identical line 60 lines above. `genericaliasobject.c`:
6 commits, 18 months, 6 different authors, 4 incomplete fixes, 5 live defects — driven not by
velocity but by four undocumented invariants that each author learns one of.

**3. Three scanner rules do not work at all.**
- `scan_refcounts.py` — the crown-jewel `borrowed-ref-across-call` rule **does not exist**;
  `BORROWED_REF_APIS` feeds a regex that is never used, alongside other dead identifiers.
- `scan_null_checks.py` — `deref-before-check` builds state and appends nothing, so
  `high_confidence` is permanently 0 — and the agent prompt instructs agents to prioritise that
  empty set.
- `scan_error_paths.py` — an off-by-one reads the return type from the line *above* it, so 82% of
  functions get an empty type and `return_null_no_exception` has been evaluating ~1% of its
  population since it was written.

**4. The tree-sitter chassis silently misattributes, not merely omits.**
`object.c`'s extracted record for `_PyObject_GetAttrId` spans **lines 1267–3521 — 2,254 lines,
absorbing ~91 real functions**; 93 such merged records exist in `Objects/`. Findings in that range
are reported confidently against the wrong function. `dictobject.c` extraction stops at line 5239
of 8597, hiding 100% of the frozendict family — which is why the `frozendict_pair_hash` sibling was
never surfaced despite an upstream comment pointing straight at it. My initial diagnosis (the
brace-unbalanced sync macros) was **wrong**: the dominant causes are Argument Clinic `*_METHODDEF`
in `PyMethodDef[]` tables, the 48-name `PyObject_HEAD` punctuation family, and — worst — the
`_Py_COMP_DIAG_*` pragma family, which nests 87 functions **with no ERROR node at all**, so no
`has_error` check can detect it.

**5. Two entries in the FP taxonomy were factually wrong about CPython.**
`cpython_non_bugs.md` claimed `PyObject_Hash` wraps `Py_EnterRecursiveCall`. It does not
(`object.c:1158`, unlike Repr/Str/RichCompare) — and that asymmetry is exactly what makes
`tuple_hash`, `union_hash`, `ga_hash` and `frozendict_pair_hash` real. An agent trusting the
taxonomy would have dismissed the whole class, both catalogued findings included. Separately, the
`Py_TRASHCAN` entry told agents to look for a marker that is now an **empty backwards-compat shim
with zero call sites** — biasing the opposite way, toward false positives. Both fixed.

## What I changed this session (all verified, 243 tests green throughout)

| # | fix | evidence |
|---|---|---|
| TK-1 | `analyze_history.py`: added `errors="replace"` to both git subprocess sites | full 9,203-commit history now analyses in 11.0 s; previously died with `UnicodeDecodeError` |
| TK-2 | `build_informed_briefing.py`: render sites as `path:line (function)`, show all sites not just `[0]` | briefing no longer leaks dict reprs; CPY-0001's `union_hash` sibling is now visible to agents |
| TK-12 | `cpython_non_bugs.md` + `recursion-guard-auditor.md`: corrected the `PyObject_Hash` claim | verified against `object.c:759/800/1099/1158` |
| TK-13 | removed mis-catalogued `OOM-0023` from 3 files, with a tombstone comment | `subtype_dealloc` has 0 `PyErr_*` calls in 167 lines; no commit ever removed one. `known-issues` 26→25 entries |
| — | `cpython_non_bugs.md`: corrected the stale `Py_TRASHCAN` entry | macros are empty shims at `cpython/object.h:446-447`, 0 call sites |

**Deliberately not fixed mid-run:** the `scan_error_paths` off-by-one. The one-line correction takes
`Objects/` from 5 to 734 findings; it needs a gated replacement rule designed alongside it, not a
quick edit.

## Recommended next steps, in priority order

1. **Fix line numbers first** (TK-15). A constant `+2` drift plus `strip_comments_and_strings()`
   eating newlines makes **111 of 113** `Objects/` null-check findings point at the wrong line.
   Everything else the toolkit reports is degraded until this is fixed. Two-line fix + regression test.
2. **Add a `parse_coverage` / `parse_errors` field to every scanner envelope.** Cheap, and it makes
   a zero result *auditable* instead of silently wrong. This is the important half of the chassis
   problem; the extraction repair can follow.
3. **Repair `extract_functions()`** for the PRAGMA / PUNCT / Clinic classes — with the measured
   caveat that substituting Clinic makes things *worse* (+288 functions without it, +217 with).
   Fix goes **upstream in cext-review-toolkit** (the file is byte-identical) and syncs forward.
4. **Rewrite the three dead rules** (TK-9, TK-14) — starting with the crown-jewel
   `borrowed-ref-across-call`, which is the toolkit's stated reason to exist and currently is not
   implemented.
5. **Report the bugs upstream.** F1, F2, F3, F5 and the F11 siblings look novel; F2 is a regression
   with a clean bisect target (gh-139871). Nothing has been filed — see the reporting-status note in
   `FINDINGS.md`.
6. **Sweep TK-1 across the family** — all four sibling toolkits carry the identical
   `text=True`-without-`errors=` defect and will hit it on any long history.

## Method notes worth keeping

- **The calibration/new-territory split worked.** 5 catalogued files measured recall (6/6); the 9
  never-reviewed files produced most of the novel findings. Keep this structure for future runs.
- **The guarded twin is the single highest-value triage device.** Nearly every confirmed finding
  here was confirmed by locating a correctly-handled sibling, usually in the same file and often
  within 200 lines. It doubles as the fix.
- **Scope escapes must be chased, not deferred.** Two of the best findings
  (`dictobject.c:8427`, `listobject.c:250`) lay just outside the 14-file boundary and were only
  found because agents were told to follow the shape out of scope.
- **Agents disproved briefing hypotheses as often as they confirmed them** — `weakrefobject.c`
  refcounts are clean, the odict UAF is swept, `structseq.c` is clean for uninit-dealloc. Those
  negatives are load-bearing and a cold run never produces them.
