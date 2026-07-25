# obj-mappings — slice summary

`Objects/dictobject.c` (8,597) + `Objects/setobject.c` (3,228) = **11,825 lines** @ `4f3be1b5777`.
19 agents. 2026-07-25.

Both files verified **byte-identical** between the target ref and the build-matrix commit
`a1d580430c8`, so every reproduction below is valid against the reviewed source.

---

## Result

**16 findings, 14 reproduced.** Recorded as **CPY-0116 … CPY-0130** (catalog 109 → 124),
plus **TSAN-0062** in `cpython-tsan-findings` (4 net-new race signatures).

| | |
|---|---|
| FIX | 12 |
| CONSIDER | 3 |
| POLICY | 3 |
| Toolkit defects | 14 (D-18 … D-31) |
| Records upgraded `static-confirmed` → `reproduced` | 2 (CPY-0079, CPY-0107) |
| Inherited items discharged | 4 of 4 |

### The one to file first

**CPY-0116 — `reversed(dict)` reads out of bounds with no upper bound.**
SIGSEGV on **released CPython 3.14.4 and 3.12.13**, from **pure Python** — no `_testcapi`,
no threads, no C API. `di_pos` is seeded from the `dk_nentries` that existed when
`reversed()` was called; the only staleness check is on `ma_used`, which says nothing about
`dk_nentries`; and the combined branch forms `&DK_UNICODE_ENTRIES(k)[i]` before any upper
test. All three forward iterators carry the bound it lacks.

Orchestrator-verified 3/3 on each of `release-gil`, `debug-ft`, and `/usr/bin/python3` 3.14.4.

The history pass established the framing: the bound was **written, reviewed, and deleted**
in PR GH-16846 (bpo-38525) after a reviewer asked "Is this change still needed?". The removal
was defensible for the split branch it was on and never applied to the combined branch.
*Caveat recorded in the finding:* the cited PR-branch hash is not resolvable in the local
clone (squashed on merge), so that narrative must be re-confirmed on the PR page before it is
quoted upstream.

Propagation swept: **16 sites / 9 distinct bodies tree-wide, exactly one unguarded.**
It is not a class — and saying so is worth as much upstream as the bug.

### The most interesting mechanism

**CPY-0117 — a doc-CONFORMING dict watcher makes CPython itself re-enter Python.**

`Doc/c-api/dict.rst` says, four lines apart, *"Do not trigger Python code execution in the
callback"* and *"If the callback sets an exception, it must return `-1`"*. And
`_PyDict_SendEvent:8314` answers that mandated `-1` with `PyErr_FormatUnraisable` →
`sys.unraisablehook` — arbitrary Python, settable from pure Python. The contract puts the
burden on the callback and then CPython discharges it wrongly on the callback's behalf.

Ten of fourteen `_PyDict_NotifyEvent` sites hold a stale `ix`, borrowed `old_value`, `ep0`
pointer, or `dk_usable` precondition across that window. Three reproduce on the **default
release GIL build**: an ASan heap-buffer-overflow **WRITE** "0 bytes after" the keys block
from a *single re-entry on a plain dict*; `popitem()` returning a tuple whose second element
is a raw C NULL; and `Py_DECREF(NULL)`.

**Four agents from four different bug classes converged on this independently.**
CPython's own `typeobject.c:1219` already says *"PyErr_FormatUnraisable is potentially
re-entrant"* — cite their words.

---

## What the scanners contributed

| agent | slice population | net-new findings from the scanner |
|---|---|---|
| refcount-auditor | 0 | 0 — all from reading |
| null-safety-scanner | 1 (already-recorded) | 0 — all six from reading |
| memory-pattern-analyzer | 1 (already-recorded) | 0 — six reproduced crashes from reading |
| error-path-analyzer | 4 | 0 — all four ACCEPTABLE |
| ft-race-scanner | 20 | 2 promoted (10%); 3 of 5 FIX from reading |
| recursion-guard-auditor | 25 | 0 net-new (1/25 precision, and that 1 is CPY-0019) |

**Third slice running where the scanners index the work rather than do it.** That is now a
stable, measured property of the campaign, not an impression.

---

## Bounded negatives — stated with denominators

These are results, not absences.

- **The CPY-0096 sibling hunt is EMPTY.** All 7 `LOCK_KEYS` regions enumerated; 2 are the
  recorded violations, 5 clean with reasons. The `DONT_DETACH` population is actually **nine**
  — two sites nobody had counted (`PyDict_AddWatcher:8252`, `PyDict_ClearWatcher:8272`) spell
  the acquire with `FT_MUTEX_LOCK_FLAGS`, a macro absent from `lock_macros.json`. Seven of
  nine conform to the file's own written rule. **The fix surface for that class is exactly two
  call sites.**
- **Init bypass is structurally clean** — proven with a counterfactual, not asserted.
  `type_ready_set_new` grants `DISALLOW_INSTANTIATION` implicitly; 2,048 probe runs, 0 crashes.
- **The `#ifdef` divergence class (CPY-0099's shape) is clean, 24/24.**
- **Allocation-size macros are correct** — 40 call sites, 0 defects.
- **`listobject.c` is clean on the stale-index shape**, 0 of 22. (`odictobject.c` is 8 of 18.)
- **Three TSan stress targets bounded clean with the detector demonstrably armed** — resize
  under contention, re-entrant `__hash__`/`__eq__`, and split→combined each reported **0**
  warnings on the same build where the two iterator scripts reported **27** and **73**. That
  localizes the defect to the iterator-lifetime family rather than to broadly racy code.
- **The unpropagated-`memcpy` hypothesis is DISPROVEN, 13/13** — a lead I relayed to two
  agents, killed by measurement.

---

## Calibration

**Complexity does not predict these bugs.** 0 of 6 recorded findings in the 8-function
hotspot list — and the agent refused to sell that as evidence (P = 0.845 under the null),
running a 200k-resample permutation test instead: `line_count` discriminates (p = 0.009),
the **composite score built from it does not** (p = 0.210). `#if` contributes zero to
cyclomatic complexity, so the dual-build branching the campaign most wants is invisible:
30 functions with internal `Py_GIL_DISABLED` arms all score 1.0–1.1. An ifdef-density ranking
hit 2/14 against recorded findings versus 0/8 for complexity hotspots.

**How the findings were introduced** (measured with the tracker's `type-bug` label, not
subject heuristics):

| signal | share |
|---|---|
| introduced by a commit that was itself fixing a bug | 4/16 (25%) |
| introduced by the free-threading migration (all gh-112075) | 6/16 (37.5%) |
| — overlap between those two | **zero** |
| **sit in a function a later fix commit walked through without noticing** | **8/16 (50%)** |

`set_add_entry_takeref` had **six** fixes pass through it; `insertdict` had five.
Fix-*adjacency* is the stronger signal, and D-22 is exactly what blocks computing it
automatically.

**The irony worth keeping.** Three agents independently cite `set_add_entry_takeref:294` as
*the* guarded twin for the stale-state family — "the one place in the slice that re-validates
after running Python." It is also the function with **two defects of its own** (CPY-0118, the
revalidation unreachable on the match path; CPY-0119, the OOM hang). And the complexity agent
ranked it setobject.c's #1 hotspot *because* it contains the guard. A metric that rewards a
guard's presence cannot see its reachability.

---

## Toolkit defects — the two that matter most

**`scan_ft_races.py:373`** suppresses `iternext_double_decref` whenever `_has_lock(body)` is
true *anywhere* in the function, without checking whether the flagged drop lies inside the
section it found. Measured on a 22-line reduction: **0 findings as shipped, 1 with the two
critical-section lines deleted.** In `setiter_iternext` the section ends at `:1127`, two lines
before the drop at `:1130`. **One boolean hides TSAN-0053, TSAN-0054 and TSAN-0062** — every
instance of the class the rule exists for, on a non-zero denominator of 8 `iternext_functions`.

**`recursion_prone_slot_functions` (D-27)** increments on `slot is not None` rather than on the
recursion-prone slot set. The envelope reports **17**; the truth here is **6**. An agent
following the campaign's own "quote the denominator" rule overstates coverage by 2.8× — in the
one field that exists to prevent unearned zeros.

Full ledger: `TOOLKIT_DEFECTS.md` (D-18 … D-31).

Two proposals were **measured and rejected** on their own numbers (D-14's widening: 4 → 8
tree-wide, net-new precision 0/1) and one measured and **accepted-pending** (the clinic
`_impl` association: 0 → 3, precision 3/3, +57% `critical_section_functions` — but it does
**not** recover CPY-0096 or CPY-0107, which need a new lock-across-Python rule).

---

## Corrections made during the run

Recorded because the campaign's value depends on them being made, not avoided.

1. **Two wrong explanations for `scan_lock_discipline`'s zero** — mine (the clinic header is
   unread) and macro-hygiene's (the vocabulary lacks `Py_BEGIN_CRITICAL_SECTION`). Both false;
   the scanner does both. The zero is *earned* for leaks (99 windows read by hand, 0 leaks)
   and *structural* for lock-across-Python, which has no rule.
2. **My brief asserted dict/set iterators share the `bytearray.__new__` crashing shape.**
   They do not — `type_ready_set_new` grants `DISALLOW_INSTANTIATION` implicitly.
3. **The history pass implied a UAF in `set_add_entry_takeref`.** It is a lost update;
   `found_active` never dereferences `entry`.
4. **An agent summary said "creating a perfectly ordinary class"** emits an unraisable
   warning. It does not — the namespace must carry a hash-colliding key whose `__eq__` raises.
5. **bpo-38555 → bpo-38525** for gh-82706; two different 2019 commits three days apart.
6. **A PR-branch hash cited for the review narrative is not resolvable locally.**
7. **Three premises in my brief about macros** were wrong (`DK_ENTRIES` &co. are `static
   inline` functions; `DK_IXSIZE` does not exist at this ref).
8. **Two agent-side retractions**, both self-caught: a recursion probe that reported `ok` at a
   depth that crashes, and two "FT-only hang" signatures that were CPU contention from four
   concurrent fuzzer instances (completes in 90.6 s standalone).

---

## Reading

- `FINDINGS_LEDGER.md` — every finding with mechanism, guarded twin, and N/M reproduction
- `TOOLKIT_DEFECTS.md` — D-18 … D-31
- `agents/` — 19 reports
- `repro/` — reproducers, ASan and TSan logs
