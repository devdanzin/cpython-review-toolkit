# parity-checker — slice `mod-io`

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Oracle:** `Lib/_pyio.py` (read, never reviewed)
**Builds:** `debug-gil-nojit`, `release-gil-nojit`, `debug-ft-nojit`, `release-gil-nojit-asan`
(matrix @ `a1d580430c8`) plus released `/usr/bin/python3.14` (3.14.4).

**Ref hygiene (brief lesson 10):** all seven slice files *and* `Lib/_pyio.py` are byte-identical
between `a1d580430c8` and `4f3be1b5777` — `git diff` returns empty for each. Every line number
below is citable against the target ref as-is.

**Deliverables in `reports/mod-io/repro/`:**

| file | what it is |
|---|---|
| `io_parity_harness.py` | the reusable dual-backend differential harness — 146 cases, 6 groups, one payload run twice in two subprocesses, machine-classified |
| `io_bufferedio_stale_raw.py` | the finding: all 7 sites, `--all` prints an N-of-M matrix against any interpreter |
| `io_close_null_raw.py` | minimal standalone repro (site `:591`), 13 lines of driver |
| `io_raw_null_sweep.py` | entry-point sweep — which `_Buffered` methods reach a NULL `self->raw` |
| `io_detach_probe.py` | the seeded lead (pre-existing) |

Findings record written: `cpython-review-findings/reports/CPY-0162-bufferedio-stale-raw-after-user-python/meta.json`.

---

## 1. Harness result

146 cases × 2 backends × 2 subprocesses, twice (debug and release).

| verdict | debug-gil-nojit | release-gil-nojit |
|---|---|---|
| `C_CRASH` (C signals, twin survives/raises) | **6** | **3** |
| `C_CONTRACT` (C `SystemError`/fatal, twin normal) | 0 | **3** |
| `EXC_DIFF` | 20 | 20 |
| `OUTPUT_DIFF` | 46 | 46 |
| `PY_TIMEOUT` (twin hangs, C fine) | 1 | 1 |
| `AGREE` | 73 | 73 |
| `BOTH_CRASH` | **0** | **0** |

Group sizes: reentrancy 23, lying 32, codec 31, teardown 30, bounds 18, memoryview 12.

The debug/release split is exactly brief lesson 5 in action and worth reading twice: the *same three
cases* are `C_CRASH` on debug and `C_CONTRACT` on release. Debug's `_Py_CheckFunctionResult` escalates
the seeded `detach` shape from a survivable `SystemError` to `SIGABRT`. **Conversely, the three cases
that are `C_CRASH` on both are SIGSEGV on release** — a hard fault the debug build's assert merely
gets to first. Neither build alone tells the whole story; a release-only run would have understated
severity on three cases and a debug-only run would have overstated it on three others.

Zero `BOTH_CRASH`. Nothing here is a shared limitation below both backends.

---

## 2. Findings

### [FIX] CPY-0162 — `bufferedio.c` never re-validates `self->raw` after user Python runs; six NULL-receiver dispatches, five of them SIGSEGV on released 3.14

**This is gh-143008's fix, not propagated.** The identical bug shape was reported twice against
`TextIOWrapper` (gh-143008 "Null pointer dereference in `TextIOWrapper.truncate` via re-entrant
`flush`", gh-142594 "…via re-entrant `closed` property that detaches `buffer`"), both closed, and
fixed on 2026-06-09 by `db4b1948bc4` (PR #145957). That commit introduced the guarded accessor and
named the hazard in its own comment:

`Modules/_io/textio.c:734-759`
```c
/* Helpers to safely operate on self->buffer.

   self->buffer can be detached (set to NULL) by any user code that is called
   leading to NULL pointer dereferences (see gh-143008, gh-142594). Protect
   against that by using helpers to check self->buffer validity at callsites. */
static PyObject *
buffer_access_safe(textio *self)
```

`db4b1948bc4`'s diffstat is `Lib/test/test_io/test_textio.py`, two NEWS entries,
`Modules/_io/clinic/textio.c.h` and `Modules/_io/textio.c`. **It never touched `bufferedio.c`**, which
has the same shape in the same directory. `textio.c` routes all six of its `self->buffer` dispatches
through `buffer_access_safe()`; `bufferedio.c` has **one** guarded `self->raw` dispatch out of the
fifteen in the file.

**The enabler.** `_io__Buffered_detach_impl` (`bufferedio.c:617`) is the only `_Buffered` method that
takes **no** `ENTER_BUFFERED`, and it dispatches the *Python-level* `flush` through `_PyFile_Flush`.
So a subclass whose `flush()` is a no-op lets `detach()` run to completion — `self->raw = NULL;
self->ok = 0` — from inside a callback that any other `_Buffered` method is holding the buffered
lock across. No `_testcapi`, no threads, no `ctypes`; the trigger is a three-line subclass.

**Why some sites crash and one does not.** `PyObject_CallMethodNoArgs` / `PyObject_CallMethodOneArg`
do **not** NULL-check their receiver — they go straight to `_PyObject_GetMethodStackRef`
(`Objects/object.c:1764`), which reads `Py_TYPE(obj)` → SEGV at address `0x8`.
`PyObject_CallMethodObjArgs` **does** check. That single API choice is the whole difference between
`:818` failing soft and the other six failing hard. Do not let this be read as "the shape is already
handled".

#### Two-backend evidence table

`python io_bufferedio_stale_raw.py --all --python <interp> -n 5`

| site | C function | debug-gil-nojit | release-gil-nojit | debug-ft-nojit | **released 3.14.4** | `_pyio` twin |
|---|---|---|---|---|---|---|
| `bufferedio.c:591` | `_io__Buffered_close_impl` | SIGABRT 5/5 | **SIGSEGV 5/5** | SIGABRT 5/5 | **SIGSEGV 5/5** | `AttributeError` |
| `bufferedio.c:788` | `_buffered_raw_tell` | SIGABRT 5/5 | **SIGSEGV 5/5** | SIGABRT 5/5 | **SIGSEGV 5/5** | completes, rc=0 |
| `bufferedio.c:1485` | `_io__Buffered_truncate_impl` | SIGABRT 5/5 | **SIGSEGV 5/5** | SIGABRT 5/5 | **SIGSEGV 5/5** | `AttributeError` |
| `bufferedio.c:1640` | `_bufferedreader_raw_read` | SIGABRT 5/5 | **SIGSEGV 5/5** | SIGABRT 5/5 | **SIGSEGV 5/5** | completes, rc=0 |
| `bufferedio.c:1748` | `_bufferedreader_read_all` | SIGABRT 5/5 | **SIGSEGV 5/5** | SIGABRT 5/5 | **SIGSEGV 5/5** | `AttributeError` |
| `bufferedio.c:1996` | `_bufferedwriter_raw_write` | SIGABRT 5/5 | **SIGSEGV 5/5** | SIGABRT 5/5 | **SIGSEGV 5/5** | `AttributeError` |
| `bufferedio.c:818` | `_buffered_raw_seek` | `SystemError` 5/5 | `SystemError` 5/5 | `SystemError` 5/5 | `SystemError` 5/5 | completes, rc=0 |
| `bufferedio.c:625` | `_io__Buffered_detach_impl` (seeded) | SIGABRT 5/5 | `SystemError` | — | `SystemError` | returns `None` |

**36 of 36 crashing runs crashed; 0 of 36 `_pyio` runs crashed.** No input crashed the twin.

#### ASan frames (release-gil-nojit-asan, GIL build per brief lesson 6)

All four captured report `SEGV on unknown address 0x000000000008`, `READ`, `Hint: address points to
the zero page`:

```
#0  _PyObject_GetMethodStackRef          Objects/object.c:1764:24
#1  PyObject_VectorcallMethod            Objects/call.c:900:19
#2  PyObject_CallMethodNoArgs            Include/cpython/abstract.h:65:12
#3  _io__Buffered_close_impl             Modules/_io/bufferedio.c:591:11
#4  _io__Buffered_close                  Modules/_io/clinic/bufferedio.c.h:388:20
```
```
#3  _bufferedreader_raw_read             Modules/_io/bufferedio.c:1640:15
#4  _bufferedreader_read_generic         Modules/_io/bufferedio.c:1837:13
#5  _io__Buffered_read_impl              Modules/_io/bufferedio.c:1019:15
```
```
#3  _bufferedwriter_raw_write            Modules/_io/bufferedio.c:1996:15
```
```
#3  _buffered_raw_tell                   Modules/_io/bufferedio.c:788:11
#4  _io__Buffered_truncate_impl          Modules/_io/bufferedio.c:1489:9
```

#### Minimal input (site `:591`)

```python
import io
class Evil(io.BufferedWriter):
    armed = True
    def flush(self):
        if self.armed:
            self.armed = False
            super().detach()          # NULLs self->raw mid-close()
Evil(io.BytesIO()).close()            # bufferedio.c:591 -> CallMethodNoArgs(NULL, "close")
```

`_pyio`: `AttributeError: 'NoneType' object has no attribute 'close'`, rc=1 — the twin has its own
(much milder) unguarded second `self.raw.close()` at `_pyio.py:1347` after a correct guard at `:1345`.
**That is a `_pyio` defect and not a slice finding**; I cite it only so the divergence is not read
as the twin being clean. The claim I am making is about the C side's *contract* — `close()` must not
segfault — not about `_pyio` being right.

#### The offending code, and the fix

`bufferedio.c:551-604` (`_io__Buffered_close_impl`), the clearest instance:

```c
    CHECK_INITIALIZED(self)                       /* :556  validates self->raw ... */
    if (!ENTER_BUFFERED(self)) { return NULL; }
    ...
    LEAVE_BUFFERED(self)                          /* :581  lock deliberately dropped */
    r = _PyFile_Flush((PyObject *)self);          /* :582  ... ARBITRARY user Python ... */
    if (!ENTER_BUFFERED(self)) { return NULL; }   /* :583 */
    ...
    res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close));   /* :591  ... no re-check */
```

The in-file guarded twin is 100 lines above, `bufferedio.c:489`:

```c
    if (self->ok && self->raw) {                  /* the only re-check in the file */
        r = PyObject_CallMethodOneArg(self->raw, &_Py_ID(_dealloc_warn), source);
```

The mechanical fix is a `raw_access_safe(buffered *self)` mirroring `textio.c:740`, applied at every
`self->raw` dispatch that follows a call able to run Python. Full site classification (all fifteen
`self->raw` dispatches in the file):

| line | dispatch | preceded by user Python? | verdict |
|---|---|---|---|
| 491 | `_dealloc_warn` | yes | **guarded** (`self->ok && self->raw`) |
| 517, 526, 545, 644, 657, 670, 685, 699, 714, 727 | `flush`/`closed`/`seekable`/`readable`/`writable`/`name`/`mode`/`fileno`/`isatty` | no — `CHECK_INITIALIZED` is the immediately preceding statement | safe |
| 367 | `_PyFileIO_closed(self->raw)` in `IS_CLOSED` | guards on `self->buffer`, not `self->raw` | **unreached by me** — `fast_closed_checks` requires `Py_IS_TYPE(self, BufferedReader)` exactly, which a subclass fails. Static concern only; no measurement. |
| 1713 | `PyObject_GetOptionalAttr(self->raw, readall)` | yes (`buffered_flush_and_rewind_unlocked`) | **unreached** — shadowed: `:818` fires first on every readable path. Static concern only. |
| 591, 788, 1485, 1640, 1748, 1996 | see table | yes | **SIGSEGV, reproduced** |
| 818 | `seek` via `CallMethodObjArgs` | yes | **`SystemError`, reproduced** |

#### Prior art check (brief lesson 7)

`gh api -X GET search/issues -f q='repo:python/cpython …'` over five term sets. The two closed
TextIOWrapper issues (gh-143008, gh-142594) are the fixed sibling. **No open or closed issue names
the `bufferedio.c` side.** `git log --since=2025-01-01 -- Modules/_io/bufferedio.c` shows eight
commits, none of them this class (`gh-149816` critical section, `gh-144380` type check, `gh-143689`
reentrant-state-on-error, `gh-138720` closed-check-match-flush). Novel.

---

### [CONSIDER] `_io._Buffered.detach()` takes no `ENTER_BUFFERED` — this is the enabler, and it is a design decision worth stating explicitly

Every other `_Buffered` method brackets its work with `ENTER_BUFFERED`/`LEAVE_BUFFERED`;
`_io__Buffered_detach_impl` (`:617-629`) does not. The re-entrancy check in `_enter_buffered_busy`
(`:321-327`) is what normally makes "user code re-enters the same buffered object" fail loudly with
`RuntimeError: reentrant call inside <…>`. `detach()` bypasses it, and that bypass is the single
precondition for every crash above. Even with the six sites hardened, `detach()` remains the one
method that can mutate `self->raw` from inside another method's locked region — the class is closed
by hardening the readers *or* by making `detach()` participate in the lock, and a maintainer should
pick deliberately rather than by omission.

I did not measure a second consequence of this; it is a structural observation about the fix's shape,
not an independent bug claim.

---

### [CONSIDER] `Py_SAFE_DOWNCAST(chars_to_skip, Py_ssize_t, int)` at `textio.c:2970` is assert-only

`cookie.chars_to_skip` is an `int` (`textio.c:2460`) filled from a `Py_ssize_t`. `Py_SAFE_DOWNCAST`
is an assertion on debug and a bare cast on release. The consumer at `:2784` does bound-check
(`PyUnicode_GetLength(self->decoded_chars) < cookie.chars_to_skip`), so a truncated value produces a
wrong seek position rather than an OOB. **Not measured** — reaching it needs >2 GiB of decoded text.
Recorded as a static observation with its falsifier stated: build a `TextIOWrapper` over >`INT_MAX`
decoded characters, `tell()` past that point, and check whether the returned cookie round-trips.

---

### [ACCEPTABLE] 66 non-crashing divergences, examined; none is a C-side defect

I read the per-line stdout diff for every `EXC_DIFF` (20) and `OUTPUT_DIFF` (46). They fall into
three classes, and **in every case where the two backends disagree on strictness the C side is the
stricter one**:

1. **The twin does not go through `__index__`/C-width conversion.** `_pyio.StringIO.truncate(2**100)`
   returns `2**100`; `_io` raises `OverflowError`. `_pyio.BytesIO.seek(2**100)` succeeds; `_io` raises.
   `_pyio.BufferedReader(raw, buffer_size=0)` raises `TypeError` where `_io` raises `ValueError`.
   The twin is the sloppy one throughout. 24 cases.
2. **The twin dereferences `None` where C raises a diagnostic.** After `detach()`, every `_io` method
   raises `ValueError: raw stream has been detached` / `underlying buffer has been detached`; `_pyio`
   raises `AttributeError: 'NoneType' object has no attribute 'read'`. Same for
   `TextIOWrapper.__new__(TextIOWrapper)`. 18 cases.
3. **Message-text and exception-type wording.** `OSError("raw readinto() returned invalid length")`
   vs `ValueError`; `TypeError` vs `ValueError` in `IncrementalNewlineDecoder.setstate`. The twins are
   documented as not byte-identical. 24 cases.

One case worth a line for a maintainer's judgement rather than a fix:
`_io.BytesIO.__init__(b"z")` on a **closed** `BytesIO` **resurrects** it (`closed` goes `True`→`False`,
`read()` works); `_pyio.BytesIO.__init__` leaves it closed. `_pyio.StringIO` resurrects like the C
side, so the twin disagrees with *itself* and cannot arbitrate. Nothing memory-unsafe.

One `PY_TIMEOUT`: `reent-detach-from-raw-write` hangs `_pyio` and completes in `_io`. That is a twin
bug, out of scope.

---

## 3. Classes bounded — clean negatives with their denominators

These are real results. Each names what was driven and how many ways.

| class | denominator driven | outcome |
|---|---|---|
| **`bytesio` exports counter vs resize** (brief §4c) | **12** memoryview cases: `getbuffer` then `write`, `truncate`, `seek`-past-end + `write`, `close`, `__setstate__`, `writelines`, `readinto`-into-itself, `__init__`, write-through a wrapping `BufferedWriter`, two live views, `memoryview(mv)` chain, release-then-write | **airtight.** All 8 mutation paths raise `BufferError` with a live export; the count is correctly reference-counted through a `memoryview(mv)` chain and correctly *released* (`mv.release()` then `write` succeeds; two views require both released). `_pyio` allows 2 of the 8. C is strictly better. `check_exports` has no hole I could find. |
| **`__new__`-bypass / uninitialised payload** (the 26 `init_bypass_null_deref` scanner candidates) | **7 types × 6 methods = 39** `T.__new__(T).<method>()` probes across `BufferedReader/Writer/Random/RWPair`, `TextIOWrapper`, `FileIO`, `BytesIO`, `StringIO`, plus 16 detach-then-use probes | **clean.** Every one raises `ValueError: I/O operation on uninitialized object` or the `UnsupportedOperation` sibling; zero crashes on either build. Reframed per brief §6.5 as "can this field be NULL *after* the guard ran?" — the answer is yes, and that is CPY-0162; but the *`__init__`-bypass* half of the class is fully discharged by `CHECK_INITIALIZED` / `CHECK_ATTACHED` / `buffer_access_safe`. |
| **`textio.c` re-entrancy** | 12 codec-attack cases driving detach/close/reconfigure from inside `decode`, `encode`, `getstate`, `setstate`, plus the 3 direct `TextIOWrapper.detach`-in-`flush` cases | **clean.** All 6 `self->buffer` dispatches go through `buffer_access_safe()`; every attack yields `ValueError: underlying buffer has been detached`. gh-143008's fix holds. This is what makes the `bufferedio.c` gap a propagation failure rather than an unknown. |
| **`TextIOWrapper` cookie handling** | forged cookies at `0xFFFFFFFF`, `2**64`, `2**64 \| (0xFF<<96)`, `2**128-1`, and `tell()±{2**63, 2**70, 2**100}`; lying `getstate()` returning a non-tuple, wrong arity, `2**100`, `-1`, an `int` buffer, 4 KiB of bytes, and one that raises | **clean.** `_io` rejects every out-of-range cookie with `ValueError`; `_pyio` **accepts** two of them and reads from the resulting position. The `chars_to_skip` bound check at `textio.c:2784` is present and fires. |
| **`ENTER_BUFFERED` span audit** (brief §4a — scanner-invisible) | **10** functions using `ENTER_BUFFERED`, **12** enter sites / **13** leave sites, mechanically walked with comments stripped, then the 2 flagged returns read by hand | **no leak.** Both flags (`_io__Buffered_read_impl:1015`, `:1023`) are false positives of my branch-insensitive counter — that function's two `ENTER_BUFFERED`s are in mutually exclusive arms and the `return res;` at `:1015` precedes any enter. Audit script at `scratchpad/enter_buffered_audit.py`; worth promoting (see §4). |
| **boundary sizes** | 18 cases: `read/readline/readlines/truncate/seek/peek/read1` at `-1, -2, 0, sys.maxsize, ±2**63, ±2**100`, all four object families; `open()` with 9 bad mode strings and 6 buffering values | **clean.** All divergences are the twin being *permissive*. No crash on either side. |
| **lying `readinto`/`write`/`read` return values** | 14 cases: counts larger than the buffer (`2**30`, `len(b)+16`), negative, `2**100` via `__index__`, non-int, raising, `read()` returning `str`, `bytes` longer than requested, `readall()` returning non-bytes | **clean, and this is the buffer-overflow primitive the brief called out.** `_bufferedreader_raw_read:1633-1640` and `_bufferedwriter_raw_write:2011-2016` both validate `n < 0 \|\| n > len` *after* `PyNumber_AsSsize_t` and raise `OSError("raw readinto() returned invalid length %zd (should have been between 0 and %zd)")`. No path writes past the buffer. |

---

## 4. Toolkit assessment

### Should the toolkit ship a reusable dual-backend differential harness? **Yes — and this slice is the case for it.**

The evidence: **the harness found in one run what a static scanner did not find at all.** The pre-run
baseline fired 26 `init_bypass_null_deref` candidates across `bufferedio.c` and `textio.c` — and every
one of those 26 is *discharged* (§3, row 2). The actual bug is at seven sites that no scanner rule
flagged, because the shape is not "field read without a guard" but "field re-read after a guard that
an intervening call invalidated". Precision on the rule that fired here was **0/26**; recall on the
bug that mattered was **0/7**. The differential found 7/7 in a single automated pass, with exit codes
rather than argument.

What makes it generalisable rather than a one-off:

- **`find_parity_pairs.py` already inventories the pairs** — `_decimal`/`_pydecimal`,
  `_datetime`/`_pydatetime`, and the `from _X import *` families. The harness needs only the two
  import lines swapped; everything else (subprocess isolation, exit-code classification, N-of-M,
  stdout diffing) is pair-agnostic. `io_parity_harness.py` is written that way deliberately: cases
  are `(name, group, body)` triples, the backend is a format field.
- **The classification table is the reusable part.** `C_CRASH` / `C_CONTRACT` / `EXC_DIFF` /
  `OUTPUT_DIFF` / `BOTH_CRASH` / `PY_TIMEOUT` maps 1:1 onto the brief's grading rubric, and
  `BOTH_CRASH` implements the shared-crash rule mechanically instead of by memory.
- **It is a regression detector for free.** Re-running `io_bufferedio_stale_raw.py --all` against a
  patched interpreter is the acceptance test for the fix. That is `known-issues` for dynamic findings.

**Concrete proposal:** promote `io_parity_harness.py` to
`plugins/cpython-review-toolkit/scripts/run_parity_differential.py` with the case list moved to a
per-pair data file (`data/parity_cases/<module>.py`), keeping `analyze(target, *, max_files=0) -> dict`
for the envelope and adding `--python` for the interpreter under test. Seed it with the 146 `_io`
cases; they are the widest set anyone has written for this pair.

### Recall gaps found by reading that the scanners missed

1. **No rule models "guard is stale after an intervening Python-running call".** This is the whole
   finding. `scan_init_bypass` asks *is there a guard*; the question that mattered is *does the guard
   still hold at the point of use*. Proposed rule, and it is tractable with the existing tree-sitter
   plumbing: within one function, find a load of `self-><ptr>` that is (a) preceded by a validating
   macro or `if` on that field, (b) separated from the use by a call in the Python-running set
   (`PyObject_Call*`, `_PyFile_Flush`, `PyObject_GetAttr`, `PyObject_GetOptionalAttr`, any call
   through another `self->` field), and (c) not re-validated between. On `bufferedio.c` that yields
   the seven sites and excludes the ten that sit directly under `CHECK_INITIALIZED`. The guarded twin
   is a *sibling file in the same directory*, which the rule can surface automatically.

2. **The receiver-NULL-safety of the call API is not modelled, and it decides severity.**
   `CallMethodNoArgs`/`OneArg` crash on a NULL receiver; `CallMethodObjArgs` raises `SystemError`;
   `GetAttr` raises. Same defect, three severities. A small table
   (`data/api_tables.json` → `receiver_null_checked: true/false`) would let a scanner rank a
   `NoArgs` site above an `ObjArgs` one instead of reporting them identically — and would have
   stopped me briefly mis-reading `:818`'s `SystemError` as evidence the class was handled.

3. **`ENTER_BUFFERED`/`LEAVE_BUFFERED` really is invisible to `scan_lock_discipline`** (brief §4a
   confirmed). But the *right* fix is not to hardcode those two names. The generalisable rule is:
   **a macro whose body contains `PyThread_acquire_lock` / `PyThread_release_lock` / `PyMutex_Lock` is
   a lock macro**, discoverable by parsing `#define` bodies in the file under scan. That covers
   `ENTER_BUFFERED` here, `LOCK_WEAKREFS` in `pycore_weakref.h`, and whatever the next module invents.
   The audit itself must be **branch-aware** — my linear depth counter produced 2/2 false positives on
   `_io__Buffered_read_impl`, which is exactly the trap a naive implementation would ship.
   `scratchpad/enter_buffered_audit.py` is the prototype and its FP rate is the warning label.

4. **A fix commit's diffstat is a first-class recall signal, and no agent uses it.** `db4b1948bc4`
   introduced `buffer_access_safe`, said in its comment that the hazard is generic ("*any user code
   that is called*"), and touched one file. A `git-history-analyzer` rule — *when a fix introduces a
   named guard helper, list the sibling files in the same directory that have the same call shape and
   did not receive it* — would have produced CPY-0162 mechanically from the commit alone. This is the
   highest-value rule I can propose from this slice: it is cheap, it is data the repo already has,
   and it found a released-CPython segfault.

### Precision of the rules that fired on this slice

| rule | fired | my triage |
|---|---|---|
| `init_bypass_null_deref` | 26 | **0 confirmed.** All discharged dynamically (§3 row 2). The rule is not wrong about the fields; it is asking the wrong question. |
| `scan_lock_discipline` | 0 (denominator 0 mutex fns) | **structural zero, as the brief warned.** Audited by hand: 10 functions, 12 spans, no leak. |
| `scan_uninit_dealloc` | 0 (0 allocation sites) | correct-by-construction; `_io` allocates via `tp_alloc`. Confirmed. |

### Method notes for the next slice

- **The twin is not a specification, and saying which side you are claiming matters.** Every
  divergence in §3 has the C side stricter. If I had treated `_pyio` as ground truth I would have
  filed 24 bogus findings about `_io` being too strict. The rubric's "say which you claim" is load-
  bearing, not a formality.
- **`_pyio` raising `AttributeError: 'NoneType'` is a *positive* signal, not noise.** It means the
  twin has the same stale-field structure and merely inherits memory safety from the language. Those
  cases are where to look hardest at the C side — the `:591` crash and the `_pyio.py:1347`
  `AttributeError` are the *same defect* in two languages.
- **Reachability required a subclass, and that is worth stating up front.** `detach()` from inside a
  raw method fails with `RuntimeError: reentrant call inside …` unless the buffered object's `flush()`
  is overridden to skip the C flush. Two hours of the run went into finding that; a future agent
  should reach for "override the Python-level hook the C code dispatches" as the first key to any
  `ENTER_BUFFERED`-style reentrancy lock.
