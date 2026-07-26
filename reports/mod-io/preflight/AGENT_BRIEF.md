# Agent brief — slice `mod-io`

Read this **after** `RUN_CONTEXT.md` and `informed_briefing.md`, before you run anything.
It carries the method lessons from the two completed slices and the structural map of `_io`
that the scanners cannot see.

---

## 1. Scope — hard boundary

Exactly **seven** files, listed in `preflight/slice_files.txt`:

```
Modules/_io/textio.c        3,523
Modules/_io/bufferedio.c    2,788
Modules/_io/bytesio.c       1,356
Modules/_io/fileio.c        1,349
Modules/_io/stringio.c      1,106
Modules/_io/iobase.c        1,055
Modules/_io/_iomodule.c       747
```

`Modules/_io/winconsoleio.c` and `Modules/_io/_iomodule.h` are **not** in the slice —
`winconsoleio.c` is Windows-only and unbuildable here, so anything found in it is unreproducible
and belongs to a later slice. The directory `Modules/_io` is *wider* than the slice; if your
scanner's scope argument is the directory, filter its output to the seven files above before
you triage. Do not wander into `Objects/` or `Python/` — cite them as context, review them never.

**All seven files are new territory.** No prior review, no catalog entries, no recall
measurement. Weight your toolkit assessment toward *what you found by reading that the scanner
missed* — that is the deliverable a cold scanner run cannot produce.

---

## 2. The differential oracle — this slice's distinguishing asset

`Lib/_pyio.py` is a shipped, maintained, pure-Python reimplementation of this exact module.
It is a **free oracle**. Feed the same adversarial input to both backends in separate
subprocesses and compare:

```python
import io           # the C accelerator  -> Modules/_io
import _pyio as io  # the pure-Python twin
```

Grading:

| C backend | `_pyio` backend | verdict |
|---|---|---|
| SIGSEGV / SIGABRT | raises cleanly | **confirmed, localized C bug** — FIX |
| `SystemError` / fatal | returns or raises normally | **C-contract violation** — FIX |
| raises `X` | raises `Y` | parity gap — CONSIDER, worth recording |
| same behaviour | same behaviour | not a finding |

Two cautions, both earned:

- `_pyio` is **not** a specification. Where the twin is itself sloppy (it can return `None` where
  the C code returns an object), the divergence is evidence about the C side's *contract*
  compliance, not proof the twin is right. Say which one you are claiming.
- A crash that reproduces in **both** backends is not an `_io` bug — it is somewhere below both.
  Out of scope for this slice; note it and move on.

---

## 3. Seeded lead — already reproduced, confirm and hunt siblings

I probed one shape before dispatching you. It reproduced on the first attempt, and it is
the archetype for this slice, so it is the model for what you are looking for.

**`_io__Buffered.detach` does not re-check its own state after driving a user flush.**

`Modules/_io/bufferedio.c:617-629`:

```c
_io__Buffered_detach_impl(buffered *self)
{
    PyObject *raw;
    CHECK_INITIALIZED(self)               /* checked HERE ... */
    if (_PyFile_Flush((PyObject *)self) < 0) {   /* ... runs arbitrary Python ... */
        return NULL;
    }
    raw = self->raw;                      /* ... and read again with NO re-check */
    self->raw = NULL;
    self->detached = 1;
    self->ok = 0;
    return raw;                           /* raw == NULL -> NULL with no exception set */
}
```

**The guarded twin is in the sibling file, with a comment naming the exact hazard.**
`Modules/_io/textio.c:1634-1641`:

```c
    if (_PyFile_Flush((PyObject *)self) < 0) {
        return NULL;
    }
    /* _PyFile_Flush could detach before returning; raise an exception. */
    buffer = buffer_access_safe(self);
    if (buffer == NULL) {
        return NULL;
    }
```

Measured on `4f3be1b5777`, trigger is pure Python (a `BufferedReader` subclass whose `flush()`
calls `detach()`), no `_testcapi`:

```
debug-gil-nojit    Fatal Python error: _Py_CheckFunctionResult:
                   a function returned NULL without setting an exception   -> SIGABRT (rc=134)
release-gil-nojit  SystemError: ... returned NULL without setting an exception  (survives)
_pyio twin         returns None, survives
```

Reproducer: `repro/io_detach_probe.py`.

**This is the shape.** `_io` is built out of C methods that call into arbitrary user Python —
`flush()`, `write()`, `read()`, `readable()`, `close()`, `seek()`, `tell()`, and every codec
method on a `TextIOWrapper`'s encoder/decoder — and then keep using state they read before the
call. Every such site is a candidate. **`_PyFile_Flush`, `PyObject_CallMethod*`,
`_PyObject_CallMethodNoArgs`, and any call through `self->raw` / `self->buffer` /
`self->encoder` / `self->decoder` is a re-entrancy point.** After each one, ask: *what did this
function read before the call that it still trusts after?*

Your job on this lead is not to re-derive it — it is to **find the other sites that lack the
re-check the textio twin has.**

---

## 4. Structural map — what the scanners are blind to here

### 4a. The dominant lock is invisible to `scan_lock_discipline`

`bufferedio.c` does **not** use `PyMutex` and barely uses `Py_BEGIN_CRITICAL_SECTION`
(one site, `:1512`). Its real lock is a raw `PyThread_type_lock` behind two macros
(`bufferedio.c:329-338`):

```c
#define ENTER_BUFFERED(self) \
    ( (PyThread_acquire_lock(self->lock, 0) ? 1 : _enter_buffered_busy(self)) \
     && (self->owner = PyThread_get_thread_ident(), 1) )

#define LEAVE_BUFFERED(self) \
    do { self->owner = 0; PyThread_release_lock(self->lock); } while(0);
```

`scan_lock_discipline` reports **0 findings with `mutex_functions: 0`** on this slice. That zero
is *structural*, not a clean bill of health — the scanner's vocabulary does not contain
`ENTER_BUFFERED`/`LEAVE_BUFFERED`, so it never looked. **There are 12 `ENTER_BUFFERED` sites and
13 `LEAVE_BUFFERED` sites in the file** (`:561 :583 :943 :968 :1008 :1017 :1059 :1115 :1236
:1429 :1476 :2097` against `:581 :607 :946 :980 :1022 :1067 :1076 :1082 :1172 :1307 :1452 :1493
:2232`). Some of those spans are hundreds of lines long — `:1115→:1172`, `:1236→:1307`,
`:2097→:2232` — and full of `goto end`, `goto error`, and early `return`.

**Audit every span by hand.** A path that leaves an `ENTER_BUFFERED` region without
`LEAVE_BUFFERED` leaves the object permanently locked *and* leaves `self->owner` set to the
current thread — which makes the next entry from that same thread hit the
`"reentrant call inside %R"` branch of `_enter_buffered_busy` forever. This is precisely the leak
class `scan_lock_discipline` exists to find, in the one dialect it cannot read. **If you find a
leak here, that is simultaneously a CPython finding and a scanner recall gap** — report both.

Note also `_enter_buffered_busy` calls `_Py_FatalErrorFormat` at interpreter shutdown when the
grace period expires (`:321-327`). That is a deliberate, documented abort, not a finding.

### 4b. The state machine you must know before triaging anything

Four independent guards, all of which a re-entrant call can invalidate:

| guard | file:line | meaning |
|---|---|---|
| `CHECK_INITIALIZED` / `_INT` | `bufferedio.c:340,352` | `self->ok <= 0` → `ValueError`, message forks on `self->detached` |
| `IS_CLOSED` / `CHECK_CLOSED` | `bufferedio.c:364,370` | closed-file guard |
| `VALID_READ_BUFFER` / `VALID_WRITE_BUFFER` | `bufferedio.c:384,387` | which half of the buffer is live |
| `CHECK_ATTACHED` / `CHECK_INITIALIZED` / `CHECK_CLOSED` | `textio.c:1597,1604,1571` | textio's equivalents |

`textio.c` additionally has `buffer_access_safe()` — the post-re-entrancy accessor. **Its
existence is the signal.** Wherever `textio.c` calls it and a structurally identical
`bufferedio.c` path does not, you have a candidate (see §3).

### 4c. `bytesio`'s exports counter — buffer-protocol resize

`bytesio.c` exposes a `bytesiobuf` view over a resizable buffer and guards resize with an
`exports` count (`:24 :57 :73 :245 :416 :478 :1080 :1130 :1196 :1282 :1291 :1316`), all through
`FT_ATOMIC_LOAD_SSIZE_RELAXED`. The documented invariant is at `:40-43`. Check that **every**
mutation path passes `check_exports` — `SHARED_BUF`, `truncate`, `write`, `writelines`, `seek`
past the end, `__setstate__`, and `close`. A resize while a `memoryview` is live is a
use-after-free. The `RELAXED` ordering on a counter that gates a *resize* is worth a hard look
on the FT build specifically.

### 4d. Real negatives vs structural zeros — check the denominator

Already computed for you. Do not certify a clean class without reading this row.

| scanner | findings | denominator | reading |
|---|---|---|---|
| `scan_refcounts` | 0 | 26 borrowed-slot load sites, 6 accessor loads | **real negative** — it looked and found nothing |
| `scan_memory_patterns` | 0 | 4 varobject sites, 0 non-constant nitems | **real negative**, narrow |
| `scan_null_checks` | 1 | 1,012 assignment sites, 466 fallible sources resolved | **real** — dense coverage, one hit |
| `scan_error_paths` | 1 | 157 functions, 35 fallible assignments, 232 int-status callees | **real** |
| `scan_gil_usage` | 0 | 16/28 vocabulary resolved | **real** for its rules; `fileio.c` does use `Py_BEGIN_ALLOW_THREADS` |
| `scan_lock_discipline` | 0 | 8 critical-section fns, **0 mutex fns** | **STRUCTURAL — see §4a. Do not certify clean.** |
| `scan_uninit_dealloc` | 0 | **0 allocation sites** | structural, but *correctly* so: `_io` allocates via `type->tp_alloc(type, 0)`, which zeroes. Say that explicitly rather than reporting a bare zero. |
| `scan_recursion_guards` | 0 | **0 recursion-prone slot fns** | structural; `_io` has no container-descending `repr`/`hash`. Plausible — verify briefly, don't belabour. |
| `scan_stw_safety` | 0 | **0 STW fns, 0 wrappers** | structural; there is no StopTheWorld in `_io`. Genuinely nothing to check. |

---

## 5. Method — the lessons that cost time on the previous slices

These are not style notes. Each one is a mistake that was actually made and had to be retracted.

1. **A verdict rests on a crash count or a debugger frame. An explanation does not.**
   On the last slice **six causal stories failed while four verdicts held**, because the story
   was reached for ahead of the data. Report *what you measured* — exit codes, N-of-M, the ASan
   or gdb frame. If you also have a mechanism, mark it as a hypothesis and say what would falsify
   it. A finding with a measurement and no explanation is publishable. A finding with an
   explanation and no measurement is not.

2. **Do not relay another agent's causal story as established.** If you cite a sibling agent's
   conclusion, cite its evidence too, or label it unverified.

3. **Check the denominator before certifying a clean negative.** §4d does this for you. A zero
   next to a zero denominator is silence, not safety.

4. **A revive-by-address acquisition is never a valid control.** If your harness obtains a
   pointer to a freed object by address (`ctypes`, `id()`-round-trip, `gc.get_objects()` on a
   dead object), any crash you get is your harness's, not CPython's. Acquire through a live
   reference or not at all.

5. **Debug builds can hide bugs and can manufacture them.** The seeded lead in §3 aborts on
   debug and merely `SystemError`s on release — the debug build's `_Py_CheckFunctionResult` is
   what escalates it. Always report both. Conversely `CPY-0049` only reaches its site on release.
   **Run crash clusters on both `debug-gil-nojit` and `release-gil-nojit` before you claim a
   severity.**

6. **FT ASan builds have no shadow for the object heap** (mimalloc, `MI_TRACK_ASAN` undefined).
   For heap evidence — use-after-free, out-of-bounds — use a **GIL** ASan build. An FT ASan run
   that reports nothing has told you nothing about the heap.

7. **Prior art before novelty.** `gh search issues` silently returns nothing in this environment.
   Use:
   ```
   gh api -X GET search/issues -f q='repo:python/cpython <terms>'
   ```
   bpo numbers map to gh issues as **gh = bpo + 44181**.

8. **Never write multi-line Python through a heredoc or stdin** — it mangles silently and then
   prints success. Write a `.py` file into `repro/` and run it.

9. **Ambient `python` is RustPython here.** It produces empty output and exits 0. Always name an
   explicit interpreter: `~/projects/python_build_matrix/builds/<name>/python` or
   `~/venvs/cpython-review-toolkit/bin/python`.

10. **Diff before you cite.** The build matrix is at `a1d580430c8`; the review target is
    `4f3be1b5777`. If you cite a line number from a file you also ran, diff that file between the
    two refs and state whether they are identical.

---

## 6. Hunt list — ranked, specific

1. **Re-entrancy after a user callback** (§3). Every `_PyFile_Flush`, `PyObject_CallMethod*`,
   and dispatch through `self->raw` / `self->buffer` / `self->encoder` / `self->decoder`.
   Which reads survive the call? `bufferedio.c`, `textio.c`, `iobase.c`.
2. **`ENTER_BUFFERED` span audit** (§4a). All 12 spans, every exit path. Scanner-invisible.
3. **`detach()` / `close()` teardown ordering.** Both NULL out a field a concurrent or re-entrant
   caller may already be inside. `bufferedio.c:419,617`; `textio.c:1213,1222,1516,1525,1630`.
4. **`bytesio` resize vs live export** (§4c), including the `RELAXED` ordering under FT.
5. **The 26 `init_bypass_null_deref` findings** — 13 in `bufferedio.c`, 13 in `textio.c`, all on
   `self->raw` / `self->buffer`. Most are probably discharged by `CHECK_INITIALIZED`, *but*
   §3 proves the guard can be stale. **Triage all 26 against the question "can this field be
   NULL *after* the guard ran?", not "is there a guard".** That reframing is the whole point.
6. **The 12 `scan_ft_races` findings**, especially `fileio.c:532 publish_before_init_complete`
   and `textio.c:930 set_newline lazy_init_no_critical_section`.
7. **`textio` decoder/encoder state machine** — `pending_bytes`, `decoded_chars`, `snapshot`,
   `telling`. `tell()`/`seek()` on a `TextIOWrapper` is the most intricate state in the module
   and calls into a user-replaceable codec.
8. **Allocation-failure paths.** `_io` allocates through `tp_alloc` (zeroing), so the classic
   uninit-dealloc shape is absent — but the OOM sweep is still worth running against the
   *constructors* and the buffer-growth paths (`_bufferedreader_read_all`, `stringio` resize,
   `bytesio` resize).
9. **Macro hygiene.** `LEAVE_BUFFERED` has a trailing `;` inside the `do{}while(0);` — check
   whether any `if (...) LEAVE_BUFFERED(self) else ...` shape exists. `CHECK_*` macros expand to
   bare `if` statements with no `do{}while(0)` wrapper; check for a dangling-else.

---

## 7. Output

Write your report to `reports/mod-io/agents/<your-agent-name>.md`. Reproducers go in
`reports/mod-io/repro/`. Structure:

- **Findings** — FIX / CONSIDER / POLICY / ACCEPTABLE, each with `file:line`, the guarded twin
  where one exists, what you measured, and what you did not.
- **Classes bounded** — shapes you checked and found clean *with the denominator that makes the
  negative meaningful*. This is a real result; state it.
- **Toolkit assessment** — precision of each rule that fired here, and any recall gap you found
  by reading. §4a is one such gap already; find the others. Propose concrete tuning, do not just
  observe.
