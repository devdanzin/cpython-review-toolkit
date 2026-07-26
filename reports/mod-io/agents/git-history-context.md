# Git History Context (preflight) — slice `mod-io`

Agent: `git-history-context` (early temporal orientation).
Dispatched mid-run, after ten analysis agents were already working. Written to serve
(a) the not-yet-dispatched agents — complexity, pep7, macro-hygiene, api-deprecation,
oom-reproducer — and (b) the final synthesis.

I do not adjudicate other agents' findings. Where I cite a sibling agent's artifact I say so.

---

## Clone health

| check | value |
|---|---|
| Shallow clone | **no** — `git rev-parse --is-shallow-repository` = `false` |
| Repo depth | **132,320 commits**, first commit **1990-08-09** |
| Target ref | `4f3be1b5777313fb36ff4bda7e4a4197c932c30e`, dated 2026-07-15 |
| Window analyzed | `--days 13000` → 1990-12-22 .. 2026-07-26 (covers all of `Modules/_io`) |
| Commits analyzed | **801** in `Modules/_io` |
| Commit cap applied | **no** (`max_commits` 50000, `commit_cap_applied: false`) |
| Script timeout | **no** (`timeout_hit: false`) |
| Envelope `notes[]` | one entry, informational, reproduced verbatim below |

> `"Prefer `watchlist` (recent crash-fix commits per KLOC) over `file_churn[].churn_rate` for prioritization: on CPython's Objects/ raw churn was anti-correlated with defect density."`

No shallow-clone, cap, or timeout warning fired. History analysis for this slice is **complete and trustworthy**.

**Scope note the script does not surface.** `Modules/_io/*.c` all begin at **2009-04-04** — that is
when the io-C branch was merged, not when the code was written. The predecessors
(`Modules/_fileio.c` 63 commits, `_bytesio.c` 10, `_textio.c` 8, `_stringio.c` 7,
`_bufferedio.c` 4, `_iobase.c` 4, back to 2007-03-08) are a **split**, not a rename, so
`git log --follow` does not reach them. Including them the module has **880** commits.
The 79 pre-2009 commits are all initial-implementation churn; no bug-fix signal is lost by
ignoring them, but the "17 years old" framing should say 2007, not 2009.

**Build-matrix diff (brief §5 rule 10).** All seven slice files are **byte-identical** between
the matrix build ref `a1d580430c8` and the target `4f3be1b5777`. Every line number in this
report is valid on both.

---

## (a) Per-file bug-fix-density watchlist

Whole scope is 7 files, so this is the whole scope, not a top-20.
`crash_fix_density` = crash-shaped fix commits in the last **730 days** (the script's
`--density-days` default, i.e. since ~2024-07-26) per KLOC. `crash_fix_commits` is the
whole-history count.

| # | File | Crash-fix density (/KLOC) | Recent crash fixes (2y) | Crash fixes (all time) | Commits | Recent example | Suggested agent |
|---|---|---|---|---|---|---|---|
| 1 | `Modules/_io/stringio.c` | **2.71** | 3 | 5 | 70 | `a2ee8996829` 2024-11-25 `__setstate__` crash when `None` is first value | null-safety, ft-race |
| 2 | `Modules/_io/bytesio.c` | **2.21** | 3 | 9 | 83 | `6d54b6ac7d5` 2026-01-09 UAF when BytesIO concurrently mutated during write | ft-race, memory-pattern |
| 3 | `Modules/_io/fileio.c` | 1.48 | 2 | 18 | 170 | `43cd7aa8cd8` 2024-09-19 memory leak in `FileIO.__init__()` | oom-reproducer, gil-discipline |
| 4 | `Modules/_io/iobase.c` | 0.95 | 1 | 5 | 94 | `b1056c2a446` 2025-06-30 weakref-list NULL check removal | pyerr-clear |
| 5 | `Modules/_io/textio.c` | 0.85 | 3 | **26** | **212** | `db4b1948bc4` 2026-06-09 **gh-143008 NULL derefs in underlying stream access** | init-bypass, null-safety |
| 6 | `Modules/_io/bufferedio.c` | 0.36 | 1 | **19** | 188 | `b1056c2a446` 2025-06-30 (module-wide sweep, not `_io`-specific) | **init-bypass, lock-discipline** |
| 7 | `Modules/_io/_iomodule.c` | 0.00 | 0 | 8 | 133 | — | complexity |

### Which file has been fixed most, and for what

**`textio.c`, decisively** — 212 commits, 26 crash-shaped fixes, the most of any file in the
module. Its fix history is dominated by three themes, in order: **decoder/encoder/newline state**
(16 fix commits — the `tell`/`seek` cookie machinery), **NULL deref/crash** (9), and
**re-entrancy/detach** (5). `bufferedio.c` is second on every absolute measure (188 commits,
19 crash fixes) and its themes are **lock discipline** (16) and **finalization/dealloc** (8).

### Read the density column with care — it inverts the slice's actual risk

The density metric normalizes by KLOC and truncates to two years, which pushes the two
files this slice is actually about (`textio.c` 3,523 lines; `bufferedio.c` 2,788 lines)
to ranks 5 and 6. That ranking is **wrong for this slice**, and I say so as a measured
claim rather than a preference:

- `bufferedio.c` ranks **last but one by density (0.36)** and holds the slice's two confirmed
  live crashes.
- `textio.c` ranks **5th by density (0.85)** and holds the largest single crash-fix commit in
  the module's history (`db4b1948bc4`, +157/−38).

The `notes[]` guidance ("prefer density over churn") was calibrated on `Objects/`, where files
are of comparable size. In `Modules/_io` the size spread is 5× (747 → 3,523 lines), so
per-KLOC normalization is doing most of the work in the ranking. **For this slice, rank by
absolute recent crash-fix count and by function-level churn** (below), not by density.
That is a toolkit observation, recorded for the synthesis.

---

## (b) Recurring fix-keyword clusters

Method: regex clustering over all 880 commits touching `Modules/_io` and its pre-2009
predecessors, restricted to commits whose subject or body matches a fix-indicating pattern.
The regexes are mine, not the script's; the script does the crash-class tagging in the
watchlist above but does not cluster. Cluster membership overlaps by design — a commit can
be both a lock fix and a re-entrancy fix.

The five clusters the dispatch predicted are all **real**. Four of five are **still active**.

### 1. Re-entrancy / detach-during-callback — 17 fix commits, **8 since 2024**, ACTIVE

```
2010:2  2011:2  2013:3  2017:1  2020:1  2024:2  2025:3  2026:3
```
Files: `textio.c` 5, `bufferedio.c` 5, `bytesio.c` 3, `fileio.c` 1.

Nearly half of this cluster's 17-year history lands in the last 30 months. This is the
slice's live class and the subject of section (c).
→ **init-bypass-checker, null-safety-scanner, error-path-analyzer.**

### 2. Deadlock / lock ordering / lock leak — 65 fix commits, **14 since 2024**, ACTIVE

```
2008:6 2009:4 2010:6 2012:1 2013:2 2015:5 2016:2 2017:6 2018:1 2019:2
2020:2 2021:1 2022:4 2023:9 2024:4 2025:8 2026:2
```
Files: `fileio.c` 17, `bufferedio.c` 16, `textio.c` 14, `stringio.c` 12, `_iomodule.c` 7,
`bytesio.c` 6, `iobase.c` 5.

The largest cluster in the module and the one with the most sustained recent activity —
2023-2026 is its densest period ever (23 commits in 3.5 years vs 65 in 18). The 2023+ surge is
free-threading work (critical sections, `PyMutex`), not the classic `ENTER_BUFFERED` lock.
One commit in this cluster was **reverted** (`73431356d32` 2025-06-12, reverting
`gh-135410` critical section around `StringIO.__next__`) — a reverted concurrency fix is a
standing invitation to re-check that site.
→ **lock-discipline-checker** (and see AGENT_BRIEF §4a: the scanner is blind to
`ENTER_BUFFERED`/`LEAVE_BUFFERED`, so this cluster's dominant dialect is unscanned).

### 3. Decoder / encoder / newline state — 34 fix commits, 5 since 2024, SLOWING

```
2009:4 2010:3 2011:1 2013:3 2014:2 2015:2 2017:2 2018:2 2019:1 2020:3
2021:1 2022:2 2023:3 2024:1 2025:3 2026:1
```
Files: `textio.c` 16 — more than the rest combined.

Remarkably steady: 1-4 commits a year for seventeen straight years, never zero for long.
This is the module's chronic-illness cluster. Most recent: `8db8fc9b510` 2026-02-23
`gh-144777` data races in `IncrementalNewlineDecoder`.
→ **null-safety-scanner, ft-race-scanner** on `textio.c` `tell`/`seek`/`read_chunk`.

### 4. Buffer resize vs export — 18 fix commits, 6 since 2024, ACTIVE

```
2008:1 2009:2 2010:2 2013:2 2014:1 2015:1 2018:1 2023:2 2024:2 2025:3 2026:1
```
Files: `bytesio.c` 7, `fileio.c` 4, `bufferedio.c` 3.

Concentrated in `bytesio.c` as AGENT_BRIEF §4c predicts. The 2025 spike is the PEP 782
`PyBytesWriter` migration (three commits: `4554486f156`, `9b35f7cdfed`, `e814e6b38bc`) —
an in-flight API migration through exactly the buffer-growth paths, which is a classic
window for introducing new instances of an old class.
Most recent bug: `6d54b6ac7d5` 2026-01-09 `gh-143378` use-after-free when BytesIO is
concurrently mutated during write.
→ **memory-pattern-analyzer, ft-race-scanner** on `bytesio.c`.

### 5. ResourceWarning / finalization / dealloc — 24 fix commits, 3 since 2024, COOLING

```
2008:2 2009:3 2010:1 2011:2 2013:1 2016:4 2017:2 2019:1 2022:1 2023:4 2025:3
```
Files: `bufferedio.c` 8, `fileio.c` 5, `iobase.c` 5.

The only predicted cluster that is genuinely cooling — nothing in 2024, nothing in 2026 so far.
Its most recent entry (`db68bfc771e` 2025-09-18, `gh-138720`) is *also* a member of the
re-entrancy cluster, and matters to section (c).
→ **pyerr-clear-auditor** (the `PyErr_Clear()` in `_io__Buffered_close_impl:585` sits in
this cluster's territory).

### 6. NULL deref / crash / segfault — 38 fix commits, 3 since 2024

```
2008:4 2009:6 2011:2 2013:8 2014:2 2015:2 2016:3 2017:2 2018:1
2020:1 2022:1 2023:3 2024:1 2025:1 2026:1
```
Files: `textio.c` 9, `bufferedio.c` 8, `bytesio.c` 4.

Added because it is the crash class this slice is producing. Note the shape: a large
2008-2016 bulge (the module maturing), a long quiet stretch, and a low but non-zero
2023-2026 tail. The recent tail is *not* the module regressing — it is the module being
probed by new adversarial techniques (re-entrancy, free-threading), which is exactly what
this review is doing.

---

## (c) Fix-completeness on the re-entrancy class — the high-value item

### The fix as merged

**PR #145957** → merge commit **`db4b1948bc4ad5e7230fe1f2814da409ca1d669b`**, merged
**2026-06-09**, author **Cody Maloney**, 26 commits over 12 weeks (first commit 2026-03-15),
closing **gh-143008**, crediting yihong0618's earlier attempt in gh-143041.

Files changed — this is the whole list, verified via `gh api repos/python/cpython/pulls/145957/files`:

```
Lib/test/test_io/test_textio.py                              +50/-0
Misc/NEWS.d/next/Library/...gh-issue-143008.aakErJ.rst        +2/-0
Misc/NEWS.d/next/Library/...gh-issue-143008.z5tw-J.rst        +1/-0
Modules/_io/clinic/textio.c.h                                 +3/-1
Modules/_io/textio.c                                        +120/-37
```

**`Modules/_io/bufferedio.c`: zero lines changed. It is not in the PR at all.**

The PR's own description states the design intent, and it is the strongest single piece of
evidence in this report:

> "The cases which need to be re-checked are **hard to spot** so rather than rely on reviewer
> effort make a better safety net by changing **all** `self->buffer` access to go through
> helper functions."

The author correctly diagnosed that point-patching this class does not work, built the
systematic remedy — and scoped it to one of the two files that has the hazard.

### What the fix covered

`buffer_access_safe()` is defined at `Modules/_io/textio.c:740`, with three wrappers built on
it — `buffer_getattr` (`:763`), `buffer_callmethod_noargs` (`:773`), `buffer_callmethod_onearg`
(`:784`). Direct call sites on the target ref: **`:765, :776, :787, :1638, :1862, :2677`**
(matching the dispatch's list exactly), plus two dispatches through `buffer_getattr` at
`:3325` and `:3348`.

**Within `textio.c` the fix is complete.** I checked every surviving bare `self->buffer`
reference (11 total) and all are legitimate non-hazard uses:

| line | use | why it is fine |
|---|---|---|
| 744, 759 | inside `buffer_access_safe` itself | the accessor |
| 1213, 1516 | `Py_CLEAR(self->buffer)` | teardown, not a read-then-use |
| 1275 | `self->buffer = Py_NewRef(buffer)` | `__init__` store |
| 1552 | `Py_VISIT(self->buffer)` | `tp_traverse` |
| 1642 | `self->buffer = NULL` | the `detach` store |

This is a well-executed fix. That is the point — it is not sloppy work that failed to
propagate, it is careful work whose blast radius was set at the file boundary.

### What the fix left

`Modules/_io/bufferedio.c` has the identical hazard structure and **zero** access helpers:

- `grep -c "self->raw" Modules/_io/bufferedio.c` → **40**
- `grep -c "_access_safe\|check_detached" Modules/_io/bufferedio.c` → **0**

The two sites the dispatch reproduced, both structurally identical to what
`buffer_access_safe` was written to prevent:

**`_io__Buffered_close_impl`, `bufferedio.c:582→591`**
```c
    LEAVE_BUFFERED(self)
    r = _PyFile_Flush((PyObject *)self);      /* :582 — runs arbitrary Python */
    if (!ENTER_BUFFERED(self)) { return NULL; }
    ...
    res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close));   /* :591 — no re-check */
```
Guarded twin: `_io_TextIOWrapper_close_impl`, `textio.c:3253` and `:3257`, which after the
same `_PyFile_Flush` calls `buffer_callmethod_noargs(self, &_Py_ID(close))` — and *additionally*
carries the `if (self->detached) Py_RETURN_NONE; /* gh-142594 null pointer issue */` early-out
at `textio.c:3237`. The bufferedio twin has **neither** guard.

**`_io__Buffered_detach_impl`, `bufferedio.c:622→625`**
```c
    if (_PyFile_Flush((PyObject *)self) < 0) { return NULL; }   /* :622 */
    raw = self->raw;                                             /* :625 — no re-check */
    self->raw = NULL;
```
Guarded twin: `_io_TextIOWrapper_detach_impl`, `textio.c:1634-1641`, whose comment names the
exact hazard — `/* _PyFile_Flush could detach before returning; raise an exception. */`.

### Is this a repeatedly re-fixed class? Yes — three times, three places, never once across both files

This is the framing the dispatch asked me to test. It holds, and the evidence is stronger
than "three times in three places".

| # | date | commit | issue | file | scope of fix |
|---|---|---|---|---|---|
| 1 | 2011-10-05 | `42c28cdd1db` | #13070 | **bufferedio.c** | bespoke `if (self->writer == NULL)` NULL check in `bufferedrwpair_closed_get`, after the GC cleared the field |
| 2 | 2025-12-15 | `1d3854a19a3` | gh-142594 | **textio.c** | bespoke 3-line `if (self->detached) Py_RETURN_NONE;` in `_io_TextIOWrapper_close_impl` |
| 3 | 2026-06-09 | `db4b1948bc4` | gh-143008 | **textio.c** | systematic — `buffer_access_safe()` + 3 wrappers, 6 direct sites, **textio.c only** |

Fix #1 and fix #2 are the same bug shape — a field NULLed out from under a function by
something that ran in the middle of it — fourteen years and one file apart, each patched at
exactly the site that was reported. Fix #3 finally generalized, and generalized within one file.

Three adjacent commits in the same window widen the picture further:

| date | commit | issue | file | why it matters here |
|---|---|---|---|---|
| 2024-06-03 | `52586f930f6` | gh-119506 | textio.c | `write()` re-read `self->pending_bytes` after `_textiowrapper_writeflush()` ran user code. Its own comments cite **gh-87426, gh-118138, gh-119506** — the *same sub-shape re-fixed three times on one field*. |
| 2025-09-18 | `db68bfc771e` | gh-138720 | **bufferedio.c** | `close()` and `flush()` disagreed about closedness; fixed by swapping `buffered_closed` → `IS_CLOSED` **inside `_io__Buffered_close_impl`** — the very function that now segfaults at `:591`. |
| 2026-01-19 | `375e372c666` | gh-143689 | **bufferedio.c** | `read1()` left the object in a reentrant (locked) state when `PyBytesWriter_Create` failed. A one-line `LEAVE_BUFFERED(self)` — again patched at exactly the reported site. |

### The two facts that make this land upstream

**Fact 1 — the same contributor worked both files' close/flush logic, nine months apart.**
`db68bfc771e` (2025-09-18, **bufferedio.c** `_io__Buffered_close_impl`) and `db4b1948bc4`
(2026-06-09, **textio.c** systematic re-entrancy fix) are both authored by **Cody Maloney**.
The person who wrote the "make all access go through helpers" safety net had, nine months
earlier, been editing the exact bufferedio function that now crashes for want of it.
This is not a lapse of attention across unrelated maintainers — it is one careful maintainer
whose fix was scoped by file, not by hazard.

**Fact 2 — the re-entrancy fixes interleave between the two files and never coincide.**
Every re-entrancy-adjacent commit since 2025-01-01, in date order:

```
2025-09-18  bufferedio.c  gh-138720   Cody Maloney    close/flush closed-check mismatch
2025-11-05  bufferedio.c  gh-140650   Sachin Shah     BufferedWriter write/flush/close
2025-12-15  textio.c      gh-142594   yihong          close() via re-entrant `closed` property
2026-01-19  bufferedio.c  gh-143689   Yongtao Huang   read1 leaves object in reentrant state
2026-02-23  textio.c      gh-144777   Sam Gross       IncrementalNewlineDecoder data races
2026-06-09  textio.c      gh-143008   Cody Maloney    systematic buffer_access_safe()
```

Six fixes, alternating between the two files, **not one of them touches both**. Meanwhile the
files co-change constantly on everything else — `bufferedio.c`↔`textio.c` is the **#1
co-change pair in the module**, 66 shared commits out of 188/212.

**A hypothesis I tested and had to discard.** I expected to find that the two files *stopped*
co-changing in the re-entrancy era, which would have been a tidy story. It is false. Jaccard
co-change by era:

| era | textio commits | bufferedio commits | both | Jaccard |
|---|---|---|---|---|
| 2009-2015 | 82 | 89 | 26 | 0.179 |
| 2016-2021 | 59 | 47 | 18 | 0.205 |
| 2022-2024 | 54 | 36 | 16 | 0.216 |
| 2025-2026 | 17 | 16 | 6 | **0.222** |

Coupling is flat, in fact very slightly *rising*. The files are maintained together as much as
they ever were. So the causal story is **not** "the module drifted apart".

**Hypothesis (labelled as such):** these fixes are *issue-driven* rather than *class-driven* —
each lands in whichever file the reporter's traceback pointed at, and the shared hazard is
never enumerated. **What would falsify it:** finding any commit in the class whose message or
PR discussion considers the other file and rules it out. I looked at the PR body, all 26
commit subjects of #145957, and the three issue reports, and found no such consideration —
but I did not read the full PR review threads, so this is not conclusive.

Two further predictions consistent with the hypothesis, both confirmed:

- The **December 2025 issue cluster was a TextIOWrapper-only sweep.** Three issues filed within
  10 days by the same reporting effort — gh-142594 (2025-12-11, `close()` via re-entrant
  `closed` property), gh-143007 (2025-12-20, `seek` via re-entrant `__int__`), gh-143008
  (2025-12-20, `truncate` via re-entrant `flush`). **All three are `TextIOWrapper`. None
  mentions `Buffered*`.** The class was swept by *type*, not by *hazard*.
- **It is happening again right now.** gh-154523's linked fix **PR #154565 (open, unmerged)**
  touches `Lib/test/test_free_threading/test_io.py`, a NEWS entry, and **`Modules/_io/textio.c`
  only** — while `bufferedio.c:626 self->raw = NULL;` is the same plain non-atomic store with
  no `FT_ATOMIC_STORE`. The propagation gap is repeating in an open PR as of this writing.

---

## (d) gh-154523 and siblings — duplicate, adjacent, or novel?

Searched with the `gh api -X GET search/issues` form (per brief §5 rule 7; plain
`gh search issues` returns nothing here).

**gh-154523** — "Data race: `TextIOWrapper.detach()` stores `self->buffer = NULL`
non-atomically", **OPEN**, filed 2026-07-23, linked PR **gh-154565 (open)**.
It reports that `_io_TextIOWrapper_detach_impl` (`textio.c:1642`) writes the slot with a plain
store while `LOAD_ATTR_SLOT` reads it with `FT_ATOMIC_LOAD_PTR`, with a TSan trace. Its
reproducer is a threaded `tw.buffer` vs `tw.detach()` race.

**Verdict: ADJACENT, not a duplicate.** Same field, same function family, different bug:

| | gh-154523 | this slice's finding |
|---|---|---|
| file | `textio.c:1642` | `bufferedio.c:591` and `:624` |
| class | data race (plain store vs atomic load) | NULL deref / NULL-without-exception after re-entrancy |
| build | free-threaded only | **GIL builds**, single-threaded |
| trigger | two threads | pure single-threaded Python, no `_testcapi` |
| symptom | TSan warning | SIGSEGV / SIGABRT / SystemError |

They share a root *location* — `detach()` NULLs a field other code reads — but not a root
*cause*. Fixing gh-154523 (making the store atomic) would not affect our crash at all.

**Prior art on our actual finding: none.** Queries run and their yields:

| query | relevant hits |
|---|---|
| `BufferedReader detach flush NULL` | 0 relevant (3 unrelated: 64028, 75912, 45943) |
| `_io._Buffered close self->raw NULL` | **0** |
| `BufferedWriter detach returned NULL without setting an exception` | 0 relevant |
| `io buffer detached SystemError NULL exception` | **0** |
| `TextIOWrapper detach re-entrant flush null` | 4, **all TextIOWrapper**: gh-142594, gh-143007, gh-143008, PR gh-143041 |

**The `Buffered*` family has never been reported for this class.** Our finding is **novel**.

**A second novel sibling, free.** gh-154523's own bug has an unreported `bufferedio.c` twin:
`bufferedio.c:626 self->raw = NULL;` is a plain store, and `bufferedio.c` contains **zero**
`FT_ATOMIC_STORE` calls. If gh-154523 is valid for `textio.c`, the same argument applies to
`_io__Buffered_detach_impl`. Worth raising in the same upstream issue or as a direct comment
on gh-154523 / PR #154565 — recorded here as a lead, not verified by me under TSan.

**Recommended upstream framing.** File one issue against the **class**, not the site:
*"`_PyFile_Flush`-then-use-the-field: `buffer_access_safe()` (gh-143008) was never propagated
to `Buffered*`"*, leading with the two reproduced crashes, then the three-fix history table,
then the authorship and interleaving facts. The previous slice's two filings turned on exactly
this "the check was written, reviewed, and not propagated" shape.

---

## (e) Churn × complexity — where the complexity agent should look

Joined `analyze_history.py` function churn with `measure_c_complexity.py` scores.

### Per file

| file | commits | churn/yr | crash fixes | fns | max cyclo | sum cyclo | score ≥3.0 | gotos |
|---|---|---|---|---|---|---|---|---|
| `textio.c` | 212 | 2.38 | 26 | 82 | 40 | **545** | **6** | **95** |
| `bufferedio.c` | 188 | 2.85 | 19 | 78 | 29 | 361 | 0 | 52 |
| `bytesio.c` | 83 | 2.45 | 9 | 48 | 12 | 155 | 0 | 7 |
| `stringio.c` | 70 | 1.99 | 5 | 27 | 35 | 147 | 0 | 8 |
| `fileio.c` | 170 | **5.18** | 18 | 32 | 22 | 136 | 0 | 0 |
| `iobase.c` | 94 | 2.45 | 5 | 38 | 26 | 133 | 0 | 14 |
| `_iomodule.c` | 133 | 4.01 | 8 | 7 | **60** | 82 | 1 | 23 |

**Answer: `textio.c`.** It is simultaneously the highest-churn file by absolute commits (212),
the highest crash-fix count (26), and by a wide margin the most complex — 545 total cyclomatic
complexity, 6 of the module's 7 functions scoring ≥3.0, and 95 `goto`s. Nothing else in the
slice is in both top tiers. **Start there.**

Second priority is split by what you are looking for: `fileio.c` has the highest *rate*
(5.18 commits/yr) but low complexity and zero gotos; `_iomodule.c` holds the single most
complex function in the module.

### Top functions by churn × complexity

| function | file | churn | score | cyclo | lines | nest | product |
|---|---|---|---|---|---|---|---|
| `_io_open_impl` | `_iomodule.c` | 53 | 5.00 | 60 | 206 | 2 | **265** |
| `_textiowrapper_readline` | `textio.c` | 37 | 3.80 | 36 | 130 | 4 | 141 |
| `_io_TextIOWrapper___init___impl` | `textio.c` | 35 | 3.40 | 32 | 129 | 3 | 119 |
| `_io_FileIO___init___impl` | `fileio.c` | **100** | 1.00* | 1* | 0* | 0* | 100* |
| `_io_TextIOWrapper_tell_impl` | `textio.c` | 20 | 4.60 | 39 | 177 | 3 | 92 |
| `_io_TextIOWrapper_seek_impl` | `textio.c` | 19 | 3.90 | 37 | 147 | 3 | 74 |
| `_PyIncrementalNewlineDecoder_decode` | `textio.c` | 17 | 4.20 | 40 | 150 | **6** | 71 |
| `_io_TextIOWrapper_write_impl` | `textio.c` | 21 | 3.30 | 40 | 118 | 3 | 69 |

\* mis-measured — see the scanner defect below.

### A real negative worth stating: complexity does not predict this slice's crashes

The two confirmed crash sites are among the *simplest* functions in the module:

| function | churn | complexity score | cyclomatic | lines | rank by churn×score |
|---|---|---|---|---|---|
| `_io__Buffered_close_impl` | **26** | 1.00 | 10 | 43 | 21 / 312 |
| `_io__Buffered_detach_impl` | 11 | 1.00 | 2 | **10** | 53 / 312 |

`_io__Buffered_detach_impl` is a ten-line function with cyclomatic complexity 2 that segfaults
from pure Python. `_io__Buffered_close_impl` is the **5th-highest-churn function in the entire
module** (26 commits) at complexity score 1.00.

**The predictive signal in this slice is churn, not complexity.** A function that has been
edited 26 times is carrying 26 rounds of accreted assumptions regardless of how simple it
looks today. The complexity agent should report its hotspot list on its own terms, but the
synthesis should not read "low complexity" as "low risk" anywhere in this slice.

---

## Hand-off to the not-yet-dispatched agents

### complexity-analyzer — one confirmed scanner defect, act on it

`measure_c_complexity.py` mis-parses **`_io_FileIO___init___impl`**, which is the
**highest-churn function in the entire module (100 commits, 2× the runner-up)**:

```
reported:  start_line 249, end_line 254, line_count 0, cyclomatic_complexity 1, score 1.0
actual:    Modules/_io/fileio.c:250 .. 547  (~300 lines)
```

Cause: the function body opens at `:253` and the very next line is `#ifdef MS_WINDOWS` /
`#else` / `#endif` (`:254-258`). The brace/extent tracker terminates at the preprocessor
conditional. It is the **only** zero-length parse in the slice (1 of 312 functions), and the
envelope's own `coverage.signatures_unparsed: 1` is the corresponding canary — but coverage
still reports **99.8%**, so the defect is invisible unless you look for it.

Consequence: the module's single most-churned function is scored 1.00 and never appears in
any hotspot list. **Measure it by hand.** This is the same class as the previously-recorded
`extract_functions` / `preproc_ifdef` gap; propose the fix in the toolkit assessment.

### pep7-style-checker — the history says this file costs reviewers real time

PR #145957 needed **five** dedicated style commits out of 26 to get through review:
`c52559be04be` "PEP 7 fixes", `150fceef9c72` "Improve PEP-7", `d7f14fc87152` "PEP-7: Move to
shorter function names, reformat arg lists", plus `ec39a6118f23` and `009cf121ea6c` on
formatting and comments. That is ~19% of the PR's commits spent on style in `textio.c` alone.
`textio.c` also carries **95 `goto`s** (52 in `bufferedio.c`, 0 in `fileio.c`). Prioritize
`textio.c`, and note in the report that style churn here has a measured review cost.

### macro-hygiene-reviewer — history hands you a confirmed instance

AGENT_BRIEF §6.9 asks you to check `CHECK_*` for dangling-else. **CPython already hit this and
fixed it**: `1d25b751c53` (2025-11-05, `gh-140650`) rewrote `CHECK_CLOSED` from a bare
`if (...) { ... }` into a `do { ... } while (0);` and changed `IS_CLOSED` from
`(!self->buffer || ...)` to `(!self->buffer ? 1 : ...)` so it can return −1. That is
prior art *in this file* for the exact defect class.

Still unremediated on the target ref (`bufferedio.c:329-388`):
- `LEAVE_BUFFERED` — `do{...}while(0)` **with a trailing `;` inside the macro** (`:338`)
- `CHECK_CLOSED` — same trailing-`;`-inside pattern after its fix (`:381`)
- `ADJUST_POSITION` — same pattern (`:390`)
- `CHECK_INITIALIZED` / `CHECK_INITIALIZED_INT` (`:341`, `:352`) — **still bare `if` statements
  with no `do{}while(0)` wrapper**, i.e. the exact shape gh-140650 fixed for `CHECK_CLOSED`,
  left in place two sites over. *Another instance of this slice's theme.*
- `ENTER_BUFFERED` (`:329`) — an expression macro whose arg `self` is used **three times**;
  multiple-evaluation is safe only because every call site passes a bare identifier. Worth
  stating as a latent hazard rather than a live bug.

### api-deprecation-tracker — three migrations, one still in flight

- **Argument Clinic**: `f24131ff312` (2015, whole module), `ed41124bb55` + `7dabb35f833`
  (2023, `_io._Buffered*` and `_io.TextIOBase`). Long-completed; check for stragglers.
- **PEP 782 `PyBytesWriter`**: `e814e6b38bc`, `4554486f156`, `9b35f7cdfed` — all **Sept 2025**,
  `bufferedio.c` and `fileio.c`. **In flight.** Check for remaining old-style
  `PyBytes_FromStringAndSize`/`_PyBytes_Resize` growth paths not yet converted; a
  half-migrated buffer-growth path is where cluster 4 lives.
- **`_Py_ABI_SLOT`**: `145770` (2026-03-24), recent.
- **Deprecation emitted by this module**: `4e294f6feb3` (2025-04-30) deprecated `codecs.open`.

### oom-reproducer — history gives you an exact template

`375e372c666` (2026-01-19, `gh-143689`) is your model, and it is only 6 months old:
`PyBytesWriter_Create(n)` returned NULL inside an `ENTER_BUFFERED` span in
`_io__Buffered_read1_impl`, and the error path returned without `LEAVE_BUFFERED` — leaking
the lock permanently and poisoning `self->owner`. The fix was a single added
`LEAVE_BUFFERED(self)` at `bufferedio.c:1076`.

**That fix was applied to exactly one of the module's twelve `ENTER_BUFFERED` spans.** Target
allocation-failure injection at the *other eleven* (AGENT_BRIEF §4a lists them:
`:561 :583 :943 :968 :1008 :1017 :1059 :1115 :1236 :1429 :1476 :2097`), concentrating on the
long ones with multiple exits — `:1115→:1172`, `:1236→:1307`, `:2097→:2232`. An OOM-induced
lock leak is observable without a crash: the next same-thread entry hits
`"reentrant call inside %R"` forever.

Secondary targets, per cluster 4 and the brief §6.8: `_bufferedreader_read_all`,
`stringio` resize, `bytesio` resize, and the constructors (all `tp_alloc`-zeroed, so the
classic uninit-dealloc shape is genuinely absent — say so rather than reporting a bare zero).

### Synthesis — the three things to carry forward

1. **The headline is fix-completeness, not a crash.** Two reproduced crashes are the evidence;
   the finding is that `buffer_access_safe()` (gh-143008, PR #145957, `db4b1948bc4`) was built
   as an explicit systematic safety net, applied to 6 sites in `textio.c`, and never carried to
   `bufferedio.c` — which has 40 `self->raw` dereferences, 0 helpers, and the identical hazard.
   The class has now been fixed three times in three places (2011 bufferedio, 2025-12 textio,
   2026-06 textio) and never once across both files.
2. **The same maintainer touched both sides.** Cody Maloney authored the bufferedio close/flush
   fix (`db68bfc771e`, 2025-09) and the textio safety net (`db4b1948bc4`, 2026-06). Six
   re-entrancy fixes since 2025-09 alternate between the two files and not one touches both —
   while the pair co-changes more than any other pair in the module (66 shared commits) and
   coupling is *not* declining (Jaccard flat at 0.18→0.22 across four eras; my "they drifted
   apart" hypothesis was tested and is false).
3. **It is recurring live.** PR #154565 (open) fixes gh-154523's data race in `textio.c` only,
   while `bufferedio.c:626` carries the same non-atomic store and `bufferedio.c` has zero
   `FT_ATOMIC_STORE` calls. Raising the class now catches both the crash and the race.

---

## What I did not do

- I did not read PR #145957's full review threads, only the body, the 26 commit subjects, and
  the file list. The claim that no reviewer considered `bufferedio.c` is therefore
  **unverified** and is labelled a hypothesis above.
- I did not run any reproducer or build. Every crash claim in this report is relayed from the
  dispatch brief and the sibling agents' artifacts in `reports/mod-io/repro/`, not
  independently measured by me.
- I did not verify the `bufferedio.c:626` non-atomic-store lead under TSan. It is a static
  observation (`grep` for `FT_ATOMIC_STORE` in `bufferedio.c` → 0 hits) offered as a lead.
- The cluster regexes in section (b) are mine and are recall-oriented; a commit whose subject
  does not name its theme is missed. Cluster counts are lower bounds.
- `crash_fix_commits_recent` uses the script's default 730-day window. I did not re-run with a
  different `--density-days`, so the density column's recency cut is fixed at ~2024-07-26.
