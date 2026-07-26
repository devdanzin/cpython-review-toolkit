# Slice `mod-io` — summary

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** `Modules/_io`, 7 files,
11,924 lines · **Agents:** 12 · **Records minted:** 13 (`CPY-0162`–`CPY-0174`)

All seven slice files are byte-identical between the build-matrix ref `a1d580430c8` and the
target, so every line number cited here is valid on both. Every record is `reproduced`, every
trigger is ordinary Python with no `_testcapi`, and `Lib/_pyio.py` survives or raises cleanly in
every case.

---

## 1. The organising finding: a fix that did not propagate, on two axes

`db4b1948bc4` (PR #145957, gh-143008 / gh-142594) introduced `buffer_access_safe()` at
`textio.c:740`. Its commit message is unusually explicit about intent:

> the cases "are hard to spot so rather than rely on reviewer effort … make all `self->buffer`
> access go through helper functions"

`git show --stat db4b1948bc4` touches `Modules/_io/textio.c` and nothing else. Within textio the
sweep is *complete* — all 11 surviving bare `self->buffer` references are legitimate
teardown/traverse/init uses. It missed on two different boundaries:

| | | |
|---|---|---|
| **CPY-0162** | the sibling **file** | `bufferedio.c` has **40 `self->raw` derefs and no accessor**. Seven sites; six segfault on **released 3.14.4**. |
| **CPY-0169** | the sibling **fields** | inside `textio.c` the helper covers `self->buffer` only — `encoder`, `decoder` and `snapshot` are unprotected. |

Measured on `/usr/bin/python3.14` (3.14.4), three runs each, against the `_pyio` oracle:

```
close:591      SIGSEGV 3/3     raw_read:1640   SIGSEGV 3/3
raw_tell:788   SIGSEGV 3/3     read_all:1748   SIGSEGV 3/3
truncate:1485  SIGSEGV 3/3     raw_write:1996  SIGSEGV 3/3
raw_seek:818   exception       _pyio           clean on all seven
```

`:818` fails soft only because `PyObject_CallMethodObjArgs` NULL-checks its receiver while
`CallMethodNoArgs` and `CallMethodOneArg` do not. That API accident, not any guard, is the entire
crash/no-crash split — and it is the reason the debug and release columns differ across this
whole cluster.

The enabler is structural: **`_io__Buffered_detach_impl` is the only `_Buffered` method that
takes no `ENTER_BUFFERED`.** `close()` *is* protected and a re-entrant `close()` correctly raises
`"reentrant call inside %R"` — but `close()` deliberately drops the lock at `:581` to run the
user flush, and `detach()` needs no lock, so `detach()` is what a callback fired from inside a
"locked" region can still do.

**Two further incomplete fixes surfaced the same way.** gh-144777 (`8db8fc9b510`) guarded four
`IncrementalNewlineDecoder` methods and missed the one hand-written getter — CPY-0171, and the
`cpython-tsan-findings` TSAN-0046 record needs updating because it captures the pre-fix state.
gh-151707 is open against `FileIO.__init__` scoped to `self->fd`, while `self->stat_atopen` in
the same function has the identical defect — CPY-0167, which crashes on **all four builds**
because its window is an explicit `Py_BEGIN_ALLOW_THREADS` rather than the absence of the GIL.

## 2. Findings not in that family

- **CPY-0163** — `_buffered_init` frees `self->buffer` *and* `self->lock` under a live frame.
  `BufferedReader/Writer/Random.__init__` takes neither `ENTER_BUFFERED` nor a critical section.
  ASan heap-use-after-free **WRITE**, plus `Fatal Python error: PyMutex_Unlock: unlocking mutex
  that is not locked`. Reached independently by four agents. Guarded twin: `BytesIO.__init__`
  opens with `/* In case, __init__ is called multiple times. */`.
- **CPY-0164** — `BytesIO.truncate()` runs `CHECK_EXPORTS` *before* `PyLong_AsLong` dispatches a
  user `__index__`; a callback taking `getbuffer()` makes the following resize realloc a buffer a
  live memoryview points into. The discriminator across the type is the Argument Clinic
  signature: `seek(pos: Py_ssize_t)` converts before the guard and is clean, `truncate(size:
  object)` converts after it and is not.
- **CPY-0170** — `IncrementalNewlineDecoder` reads and writes its own freed `self`. **The only
  finding in the slice with no non-ASan signal**: 0/5 on every plain build. A plain-build sweep
  would have certified it clean.
- **CPY-0165 / 0166 / 0172 / 0173 / 0174** — a `bytesiobuf` getbuffer with no closed check, an
  ignored `PyBuffer_FillInfo` failure that permanently wedges the exports counter, a borrowed
  nested `Buffered` receiver released after free, an unfiltered `PyErr_Clear` in `_dealloc_warn`
  whose `fileio` twin saves/narrows/reports, and a `releasebuffer` with no critical section where
  its `getbuffer` twin has one.

## 3. Classes bounded — negatives worth stating

- **All 12 `ENTER_BUFFERED` spans are balanced.** Audited independently by four agents
  (include-graph-mapper, lock-discipline, error-path, parity), every `goto end`/`goto error`,
  every loop `break`, every early return. **This was the slice brief's headline hunt and it was
  wrong** — see §5.
- **No lock-order inversion is possible.** The only blocking acquire sits inside
  `Py_BEGIN_ALLOW_THREADS`, and on FT the detach runs `_PyCriticalSection_SuspendAll` first. AB-BA
  across two buffered objects does deadlock, but `_pyio` deadlocks identically — a design
  property, not a C defect. The C-specific half is that the wait is uninterruptible
  (`intr_flag=0`): `_io` ignores SIGINT 6/6 where `_pyio` raises `KeyboardInterrupt` at 2.00s.
- **The `exports` RELAXED ordering is not the defect.** Measured, not reasoned: a two-thread
  getbuffer-vs-resize stress is clean on both FT builds, because both sides hold the same
  critical section. Check-then-resize is atomic against a *concurrent* getbuffer and not atomic
  against a *re-entrant* one. This falsifies a hypothesis the brief seeded.
- **`__new__`-bypass is clean** — 39 probes across 8 types, 811 executions, zero crashes;
  5 `_Py_T_OBJECT` members all `Py_READONLY`; 11 getset tables, one setter, and it rejects
  deletion.
- **8 `Py_BEGIN/END_ALLOW_THREADS` pairs** hand-verified straight-line (the true GIL-released
  count is 12 once `_Py_read`/`_Py_write` are included); `PyGILState_*` is structurally absent.
- **Lying `readinto`/`write` counts are bounds-checked** at `:1633` and `:2011` — the obvious
  overflow primitive is closed. Cookie forging: C rejects all 10 forged cookies, `_pyio` accepts two.

## 4. Toolkit assessment

Seven defects, in `TOOLKIT_DEFECTS.md`. One fixed, three confirmed by my own measurement, three
recorded as agent claims I did not re-measure.

**Every one is the chassis trusting a name or a line count instead of what the code does.**

| | |
|---|---|
| **D-1 fixed** (`a33752b`) | A brace shared across `#ifdef` arms made `measure_c_complexity` collapse **31 of 18,403** functions to zero length — score 1.00, inside a denominator reporting 100% coverage. One was in the already-certified obj-mappings slice. Now 31 → 11, all 11 genuinely empty. |
| **D-2, D-2b confirmed** | The same blindness in the shared tree-sitter chassis, from two independent triggers: a straddling brace (`fileio.c` yields **6 of 32** functions, `__init__` spanning 1,100 lines) and a `do{}while(0);` macro whose semicolon lives in the definition (**12 of 13** `LEAVE_BUFFERED` sites bare, hiding **9 of 137** returns). |
| **D-3 confirmed** | `scan_error_paths` misses **every** Argument Clinic `_impl`, including the segfault site. `scan_refcounts` already fixes this and documents it; the fix never propagated — a propagation gap in the toolkit, on a slice about propagation gaps. |
| **D-7 confirmed** | My own `_lock_coverage()` fix from the previous slice is **one macro rename from regressing**. Same body: spelled `ENTER_BUFFERED` → not suppressed; renamed to contain `LOCK` → whole function goes dark. |

The single best piece of toolkit evidence came from an agent: it ran the shipped
`scan_lock_discipline.py` against `git show 375e372c666^:Modules/_io/bufferedio.c` — the revision
that still held the real, reported, since-fixed leak **gh-143689** — and got `findings: 0,
vocabulary_counts: {}`. The scanner did not rank the known bug low. It never looked.

**Proposed canary, and it is the right one:** put `parse_error_nodes` / `parse_missing_nodes` in
every envelope's `denominators`. A file with 39 ERROR nodes should never be certified clean by
anything. That is to the chassis what `vocabulary_counts` already is to the vocabulary.

**Scanner scorecard on this slice:**

| rule | result |
|---|---|
| `init_bypass_null_deref` | 26 candidates, 8 real → **31% precision**, and **7/7 recall** on the crash sites (591, 788, 818, 1485, 1640, 1748, 1996 all reported at exact line numbers) |
| `scan_ft_races` | 12 candidates, 9 TP / 3 FP → **75%** |
| `unchecked_alloc` | 1 candidate, **0 real** — but against a 1,012-site denominator, so the near-zero is earned |
| `scan_refcounts` borrowed-slot | 0 findings against a headline denominator of 26 — which on inspection is **5 genuine `PyObject*` borrows**; 13 are scalars and 8 are module state. A zero against 5 is much weaker evidence than the headline implies |
| `scan_lock_discipline` | structural zero; blind to the file's only real lock |
| `scan_uninit_dealloc` | structural zero, and **correctly** so — zero non-zeroing allocator calls and zero `Py_tp_alloc` registrations in the slice |

## 5. Method — what went wrong on my side

Recorded because the campaign's value depends on these being written down.

1. **I skipped Phase 1 and hand-wrote the structural map, then dispatched ten agents on it.** The
   mapper, run late as a correction, found three material errors: the critical-section count
   missed 87 clinic-generated wrappers; the `ENTER_BUFFERED` leak hunt I made the headline had
   **no bug to find**; and I framed a shipped-fix sibling as a novel discovery. Corrections are in
   `preflight/AGENT_BRIEF.md` §0, with the wrong text left standing so the record shows what the
   agents were actually given.

2. **I also skipped `git-history-context`,** the early temporal preflight. That is what would have
   surfaced gh-143008 by design; I found it by accident through a tracker search. Dispatched late.

3. **My §6.9 macro-hygiene note was called a wrong premise — and it was, for the reason I gave.**
   The macro turned out to matter for a completely different reason (D-2b). Right macro, wrong
   mechanism, and I do not get credit for the hit.

4. **An agent's headline toolkit claim was inverted, and I caught it only by pulling the raw
   scanner output.** The parity agent reported `init_bypass_null_deref` at "0/26 precision, 0/7
   recall" and recommended retiring the rule for its harness. The scanner's own output lists all
   seven crash sites at exact line numbers. Acting on that claim would have retired a rule with
   perfect recall on the crash class.

5. **The agents corrected themselves well**, and that is worth recording as the healthy case: one
   caught its own TSan "deadlock" as a harness bug (a raw stream with no `\n`), one caught a
   `[^;{}]*` regex silently rewriting `if (!ENTER_BUFFERED(self))` and manufacturing three false
   positives it had already written down, one flagged that two siblings reached its root cause
   earlier and declined to claim novelty, and one stated plainly that its ASan evidence covered
   the load half of a read-modify-write and the store was inferred, not observed.

6. **The validator earned its place again** — six function-name mismatches on the first pass,
   two of them wrong line numbers from agent reports, both corrected against the source.
