# Slice `obj-sequences` — state at context compaction, 2026-07-26

**Status: IN PROGRESS.** Phase 1, Group A, A2, B and B2 are complete (13 agents).
Groups C, D and E have not been dispatched.

Target `/home/danzin/projects/cpython` @ `4f3be1b5777`. Scope: the 4 files in
`preflight/slice_files.txt`. All 4 are byte-identical between the build matrix
(`a1d580430c8`) and the target, so every matrix build is valid evidence.

---

## What remains

Run in this order — the phase discipline is the point of this slice.

| group | agents | notes |
|---|---|---|
| **C** | c-complexity-analyzer · pep7-style-checker | git-history-context says: churn/density flagged the file 6/6, complexity flagged the function 2/6 — but `_PyBytes_FormatEx` is a genuine counterexample (rank 1 in all of `Objects/`, still producing memory-safety fixes in 2025, in a file density ranks 21st). Point the complexity budget there. |
| **D** | api-deprecation-tracker · macro-hygiene-reviewer · memory-pattern-analyzer | `scan_deprecated_apis` has the largest untriaged pile in the slice (10). memory-pattern should own the buffer-protocol angle the other agents left. |
| **D2** | *skipped* | `explore.md` gates parity-checker on a shipped pure-Python twin. `list`/`bytes`/`bytearray` have none. Not a shortcut — state it in the synthesis. |
| **E** | git-history-analyzer | Runs last by design; cross-references all other agents. |
| extra | oom-reproducer | Not in a group. `scan_uninit_dealloc` denominator is 5 (really 6) allocation sites. Note A2 already ran a 70-scenario sweep — 903 failure points on release-gil, 777 on debug-ft — and it did NOT find CPY-0186, because exit-code classification cannot see a corrupted-global symptom. Design for that. |

Then Phase 3 synthesis → `SUMMARY.md`, and set `slices.obj-sequences.status = done` in
`plugins/cpython-review-toolkit/data/review_slices.json`.

---

## Findings recorded — CPY-0180 to CPY-0190 (11)

Catalog is at **182 records**, all pushed.

| id | what | live on main? |
|---|---|---|
| CPY-0180 | `bytearray.strip` UAF via `__release_buffer__`; **discloses** freed heap rather than crashing | fixed by #153498 |
| CPY-0181 | gh-153419's regression test uses `"x"`, which passes on the unfixed code | **test gap, live** |
| CPY-0182 | bytes iterator: `it_seq` pointer used as exhaustion sentinel; 4 functions, not 1 | **yes** |
| CPY-0183 | concat/join swap `BufferError` for a fixed `TypeError`; no adversarial code needed | **yes** |
| CPY-0184 | `PyByteArray_AS_STRING` regression → 6 builtins SIGSEGV; works on 3.12.13 and 3.14.4 | fixed by #153498 |
| CPY-0185 | `_PyBytes_Resize` is the only member of its family with no `*pv == NULL` guard | **yes** |
| CPY-0186 | slice-assign OOB **write** into `_PyRuntime` + OOB **reads** returning it to Python; FT-only STW deadlock mode | **yes** |
| CPY-0187 | `bytearray.__init__` is the one bytearray mutator with no critical section | **yes** |
| CPY-0188 | `PyBytesWriter_Format` uses a NULL result and destroys the real exception | **yes** |
| CPY-0189 | `__iadd__`/`extend` nest the second lock instead of CS2 → spurious `BufferError` on FT | **yes** |
| CPY-0190 | the missing `__init__` lock is a **tree-wide class**, 17 candidates in 9 files | **yes** |

### Security question, already answered
CPY-0186 is a memory-safety defect but **not a vulnerability under CPython's threat model** —
it needs arbitrary Python plus `_testcapi.set_nomemory` to aim the allocation failure. Report
**publicly**, but state the disclosure and the FT deadlock explicitly so a triager can disagree
on the record. CPY-0185 can ride along; same function family.

---

## Two things that must not be lost

### 1. CPY-0190 reopens the mod-io slice
I closed `mod-io` recording `CPY-0163` (`_buffered_init` freeing buffer and lock under a live
frame) as a **single-object re-init defect**. It is a **family**: `_io.StringIO.__init__`,
`BufferedReader.__init__` and `BufferedWriter.__init__` all corrupt the heap on FT, and a
mechanical gate finds 17 candidates across 9 files tree-wide.

**Before the campaign calls `Modules/_io` done, re-run mod-io against
`clinic_critical_section_coverage`.** I have not independently reproduced the three `_io` sites;
they want my own verification before any upstream report names them.

### 2. The clinic blind spot was MY scoping error, not a scanner defect
I told every agent in two slices that `scan_lock_discipline` "cannot see the clinic headers".
It can — `C_EXTENSIONS = {".c",".h"}` and `clinic` is not in `EXCLUDE_DIRS`; run straight at
`Objects/clinic/*.c.h` it reports 8/33/0 critical-section functions cleanly. The 41 invisible
regions were because **`slice_files.txt` does not list them**.

**Fix: make `make_slice_context.py` include the sibling `clinic/*.c.h` for every `.c` in a
slice**, or at minimum emit them as a companion list. One-line class of change; three agents
filed it as a tooling gap under the wrong diagnosis because I kept repeating it.

Separately: `vocabulary_counts` miscounts the `#include ... // Py_BEGIN_CRITICAL_SECTION()`
comment, so BEGIN≠END in the envelope of every clinic file in CPython.

---

## Toolkit defects from this slice (none fixed yet)

**S-1 — the chassis truncation, now costed exactly.** `tree_sitter_utils.extract_functions`
stops at `Objects/bytesobject.c:2784` of 3925; the `bytes_methods[]` table interleaves brace
initialisers with bare `BYTES_*_METHODDEF` macros, tree-sitter-c ERRORs, and recovery eats
**1,141 lines / 40 functions**. Repairing the 30 bare `*_METHODDEF` lines and re-running the
**unmodified** scanner: `functions_analyzed` 331→371, `iternext_functions` 3→4, findings 6→7 —
and the single recovered finding is `striter_next` at **high** confidence, the slice's only
high-confidence output. This is the third instance of the D-2 family and the first where it
demonstrably hid a confirmed crash.

**The toolkit already ships the canary**: `tree_sitter_utils.parse_health()` reports coverage
`0.5667` for that file and only `find_parity_pairs.py` calls it. Wire it into every envelope.

**S-2 — `scan_null_checks` `_DEREF_TEMPLATE` lacks pointer arithmetic.** Sole reason CPY-0188
was missed. Measured +3 tree-wide over `Objects/`, 1 TP.

**S-3 — positional type tables defeat destructor/slot classification.** All four files use
positional-with-comments tables, so `slot_map == {}` and classification falls back to name
suffixes. Costs `scan_pyerr_clear` (`destructor_functions: 10` when 12 slots are registered) and
`scan_recursion_guards` (0 of 13 recursion-capable slots resolved from any table). Found
independently by three agents.

**S-4 — proposed rules, with honest scores.** `iterator_sentinel_field_asymmetry`: 17 TP / 9 FP
/ 3 ambiguous tree-wide (85% with existing suppressors) but only **2/5 recall** on the named
catalogue sites — the systematic miss is the local-alias spelling, which T3 already covers, so
ship both or neither. `clinic_critical_section_coverage`: produced CPY-0190, but the
lock-discipline agent refuted its spec on three points, including that bytearray's real
denominator is 56 methods / 51 locks (not 36/33) because 18 regions are hand-written non-clinic
wrappers a clinic-only rule cannot see.

**S-5 — D-7 is unexercised here, not validated.** 35 function-like `#define`s across the 7
files, 0 lock-shaped, 0 `*LOCK*(` invocations — the pre-D-7 heuristic gives the identical zero.
The next case for the same principle: 4 of 16 functions asserting
`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` are not named `*_lock_held`; tree-wide 6 of 112 such
functions are invisible to every mechanism the scanner has.

---

## Method notes

**Running Phase 1 first paid for itself twice.** The temporal preflight redirected the whole
slice from `listobject.c` to `bytearrayobject.c` before any analysis agent started, and produced
CPY-0180. The mapper falsified three of my brief's claims — including one that would have sent
every group hunting a borrowed-`ob_item` UAF in `listobject.c` that does not exist. On the
previous slice I discovered my map was wrong *after* ten agents had worked from it.

**The organising pattern of the slice is fix propagation.** `220f0b10777` and gh-143195 swept
bytearray's `ob_exports` pin and missed `strip`. gh-129107 installed 33 critical sections and
missed `__init__`. PR #153498 added the resize-failure guard to `take_bytes` and missed
`setslice_linear`. Three sibling iterators got `#ifndef Py_GIL_DISABLED`; `bytes` did not. And
#153498's own regression tests miss two of the families they were written for. Lead with that
upstream.

**My verification has a repeating failure mode.** Four times this session a truncated or
mis-targeted view gave me a wrong first reading: `tail` swallowing an exit code through a pipe;
reading a driver's rc instead of its children's; `tail -3` hiding the one interesting row; and a
grep vocabulary that did not match the probe's output. Each time the correction reversed my
conclusion. **Read the whole output first, then filter.**

**One agent claim I could not confirm, recorded as unconfirmed rather than dropped or asserted:**
that CPY-0189's suspend window makes gh-153578 reachable with no adversarial `__buffer__`. I saw
`verdict=DISCLOSURE` exactly once, under heavy CPU contention, and got `clean` on four later runs
across three builds and four scenarios.

**Positive controls belong in the standing brief.** The recursion agent ran `tuple_hash` and
`ga_hash` at depth 400,000 → SIGSEGV 4/4 on the same harness that produced its 148 consecutive
`rc=0` results. Without that, a long run of clean results is indistinguishable from a broken
harness.
