# error-path-analyzer — slice `mod-io` (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** the 7 files in `preflight/slice_files.txt` (winconsoleio.c excluded)
**Builds:** `~/projects/python_build_matrix/builds/*` @ `a1d580430c8`.
**Diff check:** all seven slice files, plus `Lib/_pyio.py` and `Python/fileutils.c`, are
**byte-identical** between `a1d580430c8` and `4f3be1b5777` (`git diff --stat` empty). Every line
number quoted below is valid for both the review target and the binaries I ran.

Exit codes below are raw process statuses: `0` survived, `134` SIGABRT, `139` SIGSEGV.

---

## Headline

`gh-143008` (commit `db4b1948bc4`, Jun 2026) fixed exactly this slice's dominant bug shape —
"user code called from a C method detaches the stream, and the method keeps using the pointer it
read before the call" — and fixed it **wholesale in `textio.c` only**, by routing every
`self->buffer` access through a new `buffer_access_safe()` accessor. Its commit message says the
quiet part out loud:

> *"The cases which need to be re-checked are hard to spot so rather than rely on reviewer effort
> create better safety by making all self->buffer access go through helper functions."*

The commit touched `Modules/_io/textio.c` and nothing else. `bufferedio.c` holds the structurally
identical field (`self->raw`), nulled by the structurally identical method (`_Buffered.detach`),
and has **no accessor at all**. I reproduced **eight distinct crash sites** there. `textio.c`
itself still has **two** uncovered sites, because the hardening covered `self->buffer` but not
`self->encoder` / `self->decoder`, which a re-entrant `TextIOWrapper.__init__` `Py_CLEAR`s at
`textio.c:1215-1216`.

Ten reproduced crashes, all from pure Python, no `_testcapi`. Every one of them is a clean
differential: **`_pyio` raises an ordinary `AttributeError` on the same input.**

---

## Findings

### [FIX] EP-1 — `bufferedio.c` reads `self->raw` after user Python with no re-check (8 sites)

**Guarded twin:** `Modules/_io/textio.c:739-760` `buffer_access_safe()`, and its use at
`textio.c:1634-1641` — the *same* `_PyFile_Flush` → re-check → `return NULL` sequence that
`bufferedio.c:622-625` omits, with a comment naming the hazard.

**What:** `_Buffered.detach()` sets `self->raw = NULL` and takes **no** `ENTER_BUFFERED` lock, so
it succeeds from inside any user callback a buffered method drives — `flush()`, `raw.write()`,
`raw.read()`, or the `raw.closed` property that `CHECK_CLOSED` itself invokes. Every site below
then passes the NULL through a call helper that dereferences it.

| # | site | function | trigger | debug-gil | release-gil | debug-ft | release-ft |
|---|---|---|---|---|---|---|---|
| a | `bufferedio.c:591` | `_io__Buffered_close_impl` | `flush()` detaches | 134 | **139** | 134 | **139** |
| b | `bufferedio.c:1389` | `_io__Buffered_seek_impl` → `iobase.c:416` | `raw.closed` detaches | 134 | **139** | 134 | **139** |
| c | `bufferedio.c:1485` | `_io__Buffered_truncate_impl` | `raw.write` detaches | 134 | **139** | 134 | **139** |
| d | `bufferedio.c:1640` | `_bufferedreader_raw_read` (via `:1681`) | `raw.closed` detaches | 134 | **139** | 134 | **139** |
| e | `bufferedio.c:1713` | `_bufferedreader_read_all` | `raw.closed` detaches | **139** | **139** | **139** | **139** |
| f | `bufferedio.c:1748` | `_bufferedreader_read_all` loop | `raw.read` detaches | 134 | **139** | — | — |
| g | `bufferedio.c:1996` | `_bufferedwriter_raw_write` (via `:2040`) | `raw.write` detaches | 134 | **139** | 134 | **139** |
| h | `bufferedio.c:818` | `_buffered_raw_seek` | `raw.write` detaches | 0 | 0 | — | — |

Site (d) is reached from **three** public entry points — `peek()`, `readline()`, `readinto()` —
each independently reproduced. Site (h) does *not* crash: `PyObject_CallMethodObjArgs` happens to
NULL-check its receiver, so it degrades to `SystemError: null argument to internal routine` where
`_pyio` returns `b'yyyy'`. That is still a C-contract violation (FIX by the brief's grading
table), and it is a pure accident of which call helper the line uses —
`PyObject_CallMethodNoArgs` / `PyObject_CallMethodOneArg` do not check.

**Measured, not inferred** — gdb frame for each (release-gil-nojit):

```
#0  _PyObject_GetMethodStackRef (...)                       Objects/object.c:1764
#2  PyObject_CallMethodNoArgs (self=0x0, ...)               Include/cpython/abstract.h:65
#3  _io__Buffered_close_impl (self=0x7ffff74aa1f0)          ./Modules/_io/bufferedio.c:591
```
```
#0  PyObject_GetOptionalAttr (v=0x0, ...)                   Objects/object.c:1457
#1  _bufferedreader_read_all (self=0x7ffff74aa2a0)          ./Modules/_io/bufferedio.c:1713
```
```
#3  _PyIOBase_check_seekable (state=..., self=<optimized out>) ./Modules/_io/iobase.c:416
#4  _io__Buffered_seek_impl (...)                           ./Modules/_io/bufferedio.c:1389
```
(full set in `repro/` — every case reproduced under gdb with `self=0x0` at frame #2.)

**`_pyio` oracle:** `AttributeError: 'NoneType' object has no attribute 'close'` /
`'…truncate'` / `'…read'`, exit 0, on every one of the eight. This is a confirmed, localized C bug
by the brief's §2 grading table.

**Minimal reproducer** (`repro/io_buffered_close_detach.py`), 12 lines of pure Python:

```python
import io
class W(io.BufferedWriter):
    armed = True
    def flush(self):
        if self.armed:
            self.armed = False
            super().detach()
        return None
W(io.BytesIO()).close()          # SIGSEGV on release, SIGABRT on debug
```

**Prior art — partial, and the open fix does not cover this.**
`gh-143375` ("Null pointer dereference in `BufferedWriter.seek` during re-entrant close") is
**open**, with **open** PR `gh-143577`. It is the *same function* as site (b) but a different
trigger (`__index__` on the target, not the `closed` property). I read the PR diff: it only
**reorders** `PyNumber_AsOff_t` to run *before* `CHECK_CLOSED` and
`_PyIOBase_check_seekable(state, self->raw, …)`. It inserts no `self->raw` re-check. My site-(b)
reproducer detaches from inside the `closed` property that `CHECK_CLOSED` invokes, i.e. *after*
the conversion in both the old and the new ordering, and the crashing statement at `:1389` is
untouched — **so the patch as written does not fix it.** (This is a static argument read off the
diff; I did not rebuild with the patch applied.) None of sites (a), (c)–(h) appear on the tracker
at all: `gh api search/issues` over `BufferedWriter detach crash`, `_io detach self->raw NULL`,
`io buffered reentrant flush segfault`, `label:type-crash io detach` returns only the three
already-closed `textio` issues (`142594`, `143007`, `143008`) and the open `143375`.

**Fix:** the twin *is* the fix. Add `raw_access_safe(buffered *self)` to `bufferedio.c`, mirroring
`buffer_access_safe`, and route all 8 post-call reads through it. A point fix at `:1389` (which is
what the open PR effectively is) leaves seven crashes standing — that asymmetry is precisely why
`gh-143008` chose the accessor over per-site checks.

---

### [FIX] EP-2 — `_io__Buffered.detach` returns NULL with no exception set

`Modules/_io/bufferedio.c:622-629`. Seeded lead; **confirmed, not re-litigated**.
`debug-gil-nojit` rc=134 (`Fatal Python error: _Py_CheckFunctionResult: a function returned NULL
without setting an exception`), `release-gil-nojit` rc=0 with
`SystemError: <method 'detach' of '_io.BufferedReader' objects> returned NULL without setting an
exception`, `debug-ft` 134 / `release-ft` 0. Same guarded twin (`textio.c:1638`). It is site (i)
of the same family and the same accessor fixes it. Reproducer: `repro/io_detach_probe.py`
(pre-existing) and `--case detach_via_flush`.

---

### [FIX] EP-3 — `textio.c` `self->encoder` / `self->decoder` were left out of the gh-143008 hardening

`gh-143008` routed `self->buffer`. It did not touch `self->encoder` or `self->decoder`, which are
equally user-nullable: `_io_TextIOWrapper___init___impl` `Py_CLEAR`s both at
`textio.c:1215-1216`, and the `error:` label at `:1327-1329` returns `-1` **leaving them NULL** —
reachable by re-entering `__init__` with an unknown encoding (the lookup at `:1257` fails, `goto
error` at `:1260`).

| site | function | pre-call guard | intervening user code | debug | release | ft-debug | ft-release |
|---|---|---|---|---|---|---|---|
| `textio.c:1783` | `_io_TextIOWrapper_write_impl` | `:1741 if (self->encoder == NULL)` | `:1754 _PyObject_CallMethod(text, "replace", …)` — a `str` **subclass**'s `replace` | 134 | **139** | 134 | **139** |
| `textio.c:2896/2926/3008/3023/3038` | `_io_TextIOWrapper_tell_impl` | `:2857 if (self->decoder == NULL …)` | `:2889 decoder.getstate()` — a user incremental decoder | 134 | **139** | 134 | 134 |

gdb frames:

```
#2  PyObject_CallMethodOneArg (self=0x1, ...)     Include/cpython/abstract.h:74
#3  _io_TextIOWrapper_write_impl (...)            ./Modules/_io/textio.c:1783
```
```
#2  PyObject_CallMethodNoArgs (self=0x0, ...)     Include/cpython/abstract.h:65
#3  _textiowrapper_decoder_setstate (...)         ./Modules/_io/textio.c:2546
#4  _io_TextIOWrapper_tell_impl (...)             ./Modules/_io/textio.c:2964
```

`_pyio` survives both (exit 0). Reproducer: `repro/io_textio_stale_codec_sweep.py`
(`--case write_encoder_cleared`, `--case tell_decoder_cleared`).

**Guarded twin:** `buffer_access_safe` itself — the right fix is a matching
`encoder_access_safe` / `decoder_access_safe`, or extending the existing helper family. Note
`textio.c:1874` and `:2670` *do* re-check `if (self->decoder)` immediately before use; those two
are the in-file guarded twins that show the intended discipline.

**Note on the `write` site:** `self->encodefunc` (a C function pointer, `NULL`ed at `:1225`) has
the same lifetime, so clearing it is what routes control to the unchecked `self->encoder` at
`:1783` rather than to `(*self->encodefunc)(…)` at `:1778`. Both halves need the guard.

---

### [FIX] EP-4 — the scanner's one finding, confirmed, plus an unreported identical sibling

`bufferedio.c:869-870` (`_buffered_init`) — **the scanner's single `unconditional_pyerr_clear`.**

```c
if (_buffered_raw_tell(self) == -1)
    PyErr_Clear();
```

`_buffered_raw_tell` is `PyObject_CallMethodNoArgs(self->raw, &_Py_ID(tell))` (`:788`) —
arbitrary user Python. The clear is unnarrowed, so it discards whatever the raw stream's `tell()`
raised. Measured (`repro/io_buffered_init_swallow.py`), identical on debug and release:

```
KeyboardInterrupt  -> constructor SUCCEEDED, exception swallowed
MemoryError        -> constructor SUCCEEDED, exception swallowed
RecursionError     -> constructor SUCCEEDED, exception swallowed
SystemExit         -> constructor SUCCEEDED, exception swallowed
```

`io.BufferedReader(raw)` returns a live object with a `KeyboardInterrupt` thrown away.

**Sibling the scanner did not report: `bufferedio.c:1489-1490`,
`_io__Buffered_truncate_impl`** — a character-for-character copy of the same two lines, reached
from `BufferedWriter.truncate()`. Also measured swallowing `KeyboardInterrupt`. It is invisible to
`scan_error_paths` because `_io__Buffered_truncate_impl` is one of the 145 functions the scanner's
`find_functions` never extracts (see Toolkit assessment, gap A) — **not** a dedup collapse; the
dedup was already fixed and I verified it is not the cause.

**Guarded twins, same file:** `:881` `_PyIO_trap_eintr` narrows with
`PyErr_ExceptionMatches(PyExc_OSError)`; `:1547` `buffered_repr` narrows with
`PyErr_ExceptionMatches(PyExc_ValueError)` and even comments why. Two of nine clears in the file
narrow; these two do not.

**Oracle note (honest limit):** `_pyio.BufferedReader.__init__` never calls `raw.tell()` and
`_pyio`'s `truncate` only calls `tell()` when `pos is None`, so the twin makes no corresponding
call. `_pyio` is **silent** on this shape — it is not evidence either way, and the finding rests
on the C reachability measurement alone.

**Fix:** `if (!PyErr_ExceptionMatches(PyExc_OSError)) { return -1; } PyErr_Clear();` at both
sites (matching `_PyIO_trap_eintr`'s house style), or `PyErr_ExceptionMatches(PyExc_Exception)` if
"any ordinary error means unseekable" is the intent — the point is that `BaseException` must not
be swallowed.

---

### [CONSIDER] EP-5 — `fileio.c:296-304` swallows a user `__index__` exception and mis-reports it

```c
fd = PyLong_AsInt(nameobj);
if (fd < 0) {
    if (!PyErr_Occurred()) { PyErr_SetString(PyExc_ValueError, "negative file descriptor"); return -1; }
    PyErr_Clear();                 /* unnarrowed */
}
```

`nameobj` is an arbitrary object, so `PyLong_AsInt` dispatches its `__index__`. Measured
(`repro/io_fileio_index_swallow.py`), identical debug and release:

```
KeyboardInterrupt  -> SWALLOWED, reported as TypeError: expected str, bytes or os.PathLike object, not EvilIndex
MemoryError        -> SWALLOWED, reported as TypeError: ...
RecursionError     -> SWALLOWED, reported as TypeError: ...
SystemExit         -> SWALLOWED, reported as TypeError: ...
```

The clear is intended to mean "not an fd, try it as a path"; it also discards `KeyboardInterrupt`
and replaces it with a misleading `TypeError` about the filename.

**Guarded twin:** `_iomodule.c:539-542` narrows with
`PyErr_GivenExceptionMatches(runerr, PyExc_OverflowError)` before its clear.
**CONSIDER, not FIX:** no crash, and the exception is user-supplied through a conversion the
function is explicitly probing — but `BaseException` should be re-raised.
**Oracle not applicable:** `_pyio` uses `isinstance(file, int)`, which never calls `__index__`,
so it never has an exception to swallow.

---

### [CONSIDER] EP-6 — `close()`'s re-entry clobbers the flush's exception

`bufferedio.c:581-589`:

```c
LEAVE_BUFFERED(self)
r = _PyFile_Flush((PyObject *)self);        /* may leave an exception pending */
if (!ENTER_BUFFERED(self)) { return NULL; } /* _enter_buffered_busy PyErr_Format()s over it */
PyObject *exc = NULL;
if (r < 0) { exc = PyErr_GetRaisedException(); }
```

If the flush fails **and** the re-acquire finds the lock re-entered, `_enter_buffered_busy`'s
`PyErr_Format(PyExc_RuntimeError, "reentrant call inside %R", self)` (`:300-302`) overwrites the
flush's exception with no chaining, and the `_PyErr_ChainExceptions1` five lines below never runs.
Narrow window, not reproduced — the same user callback has to both fail the flush and re-enter.
Fix: capture with `PyErr_GetRaisedException()` **before** the re-`ENTER_BUFFERED`.

---

## Classes bounded

Real negatives, each with the denominator that makes it mean something.

### `return NULL` / `return -1` reachable with no pending exception — enumerated, 1 defect

344 textual sentinel returns in the seven files (218 `return NULL;`, 126 `return -1;`). Rather
than read all 344 I enumerated the **three mechanisms** by which a sentinel can be produced
without an exception, and checked every instance of each:

1. **Returning a pointer variable that can be NULL** — 79 `return <identifier>;` sites across the
   seven files; 48 of them return a pointer-typed local (the rest are scalar status/count
   returns: `ret`, `n`, `r`, `written`, `closed`, …). All 48 read. **One defect:
   `bufferedio.c:629` `return raw;`** (EP-2). The other 47
   are either provably non-NULL at the return or NULL only on a path where a callee set the
   exception. `textio.c:1644 return buffer;` is the guarded twin (the value came from
   `buffer_access_safe`).
2. **A sentinel comparison with no `PyErr_Occurred()` narrowing** — 22 `PyLong_As*` /
   `PyNumber_AsSsize_t` / `PyNumber_AsOff_t` / `PyObject_Size` sites.
   **21 of 22 use the correct `x == -1 && PyErr_Occurred()` form** (`bytesio.c:642,749,1032`;
   `stringio.c:473,969`; `fileio.c:297,451,985,1109`; `_iomodule.c:372`; `textio.c:1345,2868`;
   `iobase.c:943`; `bufferedio.c:1394,1653`; …). The one exception, `iobase.c:695`
   `if (PyObject_Size(line) <= 0) { Py_DECREF(line); return NULL; }`, is a **`tp_iternext`**,
   where NULL-with-no-exception *is* the StopIteration protocol; the error case does carry the
   exception. **ACCEPTABLE.** This is a real negative for the `pylong_sentinel_no_errcheck` class
   at a dense denominator — the `_zoneinfo` shape (CPY-0033 / CPY-0058) is absent from `_io`.
3. **A macro-expanded `return NULL` whose condition does not imply an exception** —
   `CHECK_INITIALIZED` (`:340`), `CHECK_INITIALIZED_INT` (`:352`), `CHECK_CLOSED` (`:370`),
   `IS_CLOSED` (`:364`), textio's `CHECK_CLOSED`/`CHECK_ATTACHED`/`CHECK_ATTACHED_INT`
   (`:1571,1604,1612`), and `ENTER_BUFFERED` failure. All checked: each sets an exception on its
   own error branch, and `_enter_buffered_busy` sets `RuntimeError` at `:300-302` before its only
   `return 0`. `_PyFileIO_closed` (`fileio.c:94`) cannot return a negative value, so
   `IS_CLOSED`'s `< 0` arm is only ever reached from `buffered_closed`, which sets one.
   **0 defects.**

Plus two `tp_iternext` NULL-without-exception exits (`bufferedio.c:1534`, `iobase.c:698`) that are
correct by protocol.

### `ENTER_BUFFERED` span audit (brief §4a, exception-state half)

All **12** `ENTER_BUFFERED` call sites (`:561 :583 :943 :968 :1008 :1017 :1059 :1115 :1236 :1429
:1476 :2097`) and **13** `LEAVE_BUFFERED` call sites (`:581 :607 :946 :980 :1022 :1067 :1076
:1082 :1172 :1307 :1452 :1493 :2232`) walked by hand, including the three long spans
(`:1115→:1172`, `:1236→:1307`, `:2097→:2232`).

- **Exception state: every error exit from every span carries a pending exception**, with the one
  exception documented as EP-6 above. `_buffered_readline`'s `if (!ENTER_BUFFERED(self)) goto
  end_unlocked;` is correct — the `RuntimeError` is set by `_enter_buffered_busy` and the label
  correctly skips the `LEAVE`.
- **Lock discipline (cross-check for the lock-discipline agent, not my verdict): I found no leak.**
  Every `goto end` / `goto error` inside a span lands on a label that executes `LEAVE_BUFFERED`;
  the two bare `return NULL`s inside a span region (`:562`, `:584`) are both on the *failure* of
  `ENTER_BUFFERED` itself, where no lock is held. The extra `LEAVE` (13 vs 12) is the
  `LEAVE_BUFFERED(self)` at `:581` that deliberately drops the lock across the flush in `close()`,
  re-acquired at `:583`.
- The `_enter_buffered_busy` `_Py_FatalErrorFormat` at `:319-325` is the documented
  shutdown-grace-period abort. Not a finding, per the brief.
- **Macro hygiene (brief §6.9):** `LEAVE_BUFFERED`'s trailing `;` inside `do{}while(0);` and the
  `CHECK_*` macros' bare `if` bodies are latent dangling-else hazards, but there is **no**
  `if (…) LEAVE_BUFFERED(self) else …` or `if (…) CHECK_INITIALIZED(self) else …` anywhere in the
  file. Latent, not live. **ACCEPTABLE.**

### `PyErr_Clear()` census — 22 sites in the seven files

| verdict | n | sites |
|---|---|---|
| **FIX** | 2 | `bufferedio.c:870`, `bufferedio.c:1490` (EP-4) |
| **CONSIDER** | 2 | `fileio.c:303` (EP-5); `fileio.c:664` `_io_FileIO_seekable_impl` probes with `portable_lseek` and clears any failure, including a `MemoryError` from `PyLong_FromLongLong` |
| **guarded twin (narrowed)** | 3 | `bufferedio.c:1551` (ValueError), `textio.c:3094` (ValueError), `_iomodule.c:542` (OverflowError) |
| **ACCEPTABLE — destructor / `_dealloc_warn` family** | 7 | `bufferedio.c:495`, `:578`; `fileio.c:184`; `textio.c:3250`; `iobase.c:307`, `:314`, `:320` |
| **ACCEPTABLE — deliberate self-reset** | 3 | `bufferedio.c:759` (`_set_BlockingIOError` clears before raising its own), `:2150`, `:2205` (clear the `BlockingIOError` the same function raised) |
| **ACCEPTABLE — `errno`-narrowed after `_Py_read`/`_Py_write`** | 4 | `fileio.c:702`, `:841`, `:904`, `:946` — `_Py_read`/`_Py_write` set exactly `PyErr_SetFromErrno(OSError)`, and their `PyErr_CheckSignals()` path exits with `errno == EINTR`, never `EAGAIN`, so a signal exception cannot reach these clears |
| **ACCEPTABLE — clears its own conversion error** | 1 | `bufferedio.c:892` (`_PyIO_trap_eintr`; the live exception was already taken by `PyErr_GetRaisedException()` at `:884`) |

(`iobase.c:307/:314/:320` are all inside `iobase_finalize`, fully bracketed by
`PyErr_GetRaisedException()` at `:283` / `PyErr_SetRaisedException()` at `:332` — verified the
save/restore really does bracket all three, per the taxonomy's warning about whole-function
suppression.)

`scan_pyerr_clear` (which owns the destructor family) reported exactly 1 of these 22
(`bufferedio.c:495`). I agree with its verdict there and do not re-litigate it.

### The `textio.c` `self->buffer` class — clean, and the control proves the guard works

`textio.c` mentions `self->buffer` on only **11** lines, and every one of them is
lifecycle: the accessor's own test and return (`:744`, `:759`), `Py_CLEAR` in `__init__` and
`tp_clear` (`:1213`, `:1516`), the assignment (`:1275`), `Py_VISIT` (`:1552`), the `detach`
NULL-out (`:1642`), and three comment lines. **Every actual *use* goes through one of the four
helpers, at 26 call sites.** That ratio — 26 guarded uses, 0 raw ones — is what `gh-143008` bought,
and it is exactly what `bufferedio.c` lacks. I walked all **11**
`_PyFile_Flush` call sites in the slice and confirmed each `textio` one is followed by a safe
accessor (`:1492`, `:1634`, `:1863`, `:2664`, `:2715`, `:2849`, `:3059`, `:3253`).
Two control cases run through the same harness as the crashing ones:

```
textio_close_via_flush    debug rc=0  release rc=0  -> ValueError: underlying buffer has been detached
textio_detach_via_flush   debug rc=0  release rc=0  -> ValueError: underlying buffer has been detached
```

**Real negative.** (Note `_pyio` is *worse* here — it raises `AttributeError: 'NoneType' object
has no attribute 'close'` — so the C side is the better contract, and the twin is not a
specification.)

### Re-entrancy surface actually walked

- `_PyFile_Flush`: **11** call sites, all read.
- `PyObject_CallMethod*` / `_PyObject_CallMethod`: **63** call sites in the seven files, all read
  for a post-call use of pre-call state.
- `self->raw` / `self->buffer` / `self->decoder` / `self->encoder`: **146** textual reads.
- **19 adversarial cases** built and run (`io_buffered_stale_raw_sweep.py` 15 +
  `io_textio_stale_codec_sweep.py` 4), each in its own subprocess on 4 builds:
  **12 crashing case-instances covering 10 distinct source sites**; 3 clean negatives
  (`rewind_via_write` → `SystemError` not a crash, `tell_via_closedprop` — `tell()` has no
  `CHECK_CLOSED` so the property never fires, `write_via_closedprop` — the fast path only
  `memcpy`s into `self->buffer` and never touches `self->raw`); 2 `textio` controls clean;
  2 textio codec cases crash.

---

## Toolkit assessment

### Precision of the rules that fired

| rule | candidates | confirmed | dismissed | precision |
|---|---|---|---|---|
| `unconditional_pyerr_clear` | 1 | **1** (FIX, `bufferedio.c:870`) | 0 | **1/1** |

All other `scan_error_paths` rules returned 0 on this slice. Two of those zeros are honest
(`unchecked_return`, `unchecked_parse` — Argument Clinic), one is **not** (below).

### Recall gap A — `scan_error_paths.find_functions` cannot see an Argument Clinic `_impl`. It misses **50%** of this slice.

`scan_error_paths.py:195-255` requires the *complete* signature to be on the line immediately
preceding the `{`:

```python
prev = lines[i - 1].strip()
m = re.match(r'^(\w+)\s*\(([^)]*)\)\s*$', prev)
```

Argument Clinic puts a comment there:

```c
static PyObject *
_io__Buffered_close_impl(buffered *self)
/*[clinic end generated code: output=7280b7b42033be0c input=56d95935b03fd326]*/
{
```

so **every clinic `_impl` in CPython is invisible to this scanner** — which is to say, every
Python-visible entry point, exactly where a user-triggerable bug lives.

Measured against the tree-sitter `extract_functions` that the other ten scanners use:

| file | functions | `scan_error_paths` misses | `scan_null_checks` | `scan_refcounts` | `measure_c_complexity` |
|---|---|---|---|---|---|
| textio.c | 82 | **41 (50%)** | 0 | 4 | 0 |
| bufferedio.c | 78 | **40 (51%)** | 0 | 9 | 0 |
| bytesio.c | 49 | **25 (51%)** | 1 | 5 | 1 |
| fileio.c | 6 | 2 (33%) | 0 | 0 | 0 |
| stringio.c | 27 | **17 (63%)** | 0 | 0 | 0 |
| iobase.c | 38 | **15 (39%)** | 0 | 2 | 0 |
| _iomodule.c | 9 | 5 (56%) | 2 | 2 | 2 |
| **total** | **289** | **145 (50%)** | 3 (1%) | 22 (8%) | 3 (1%) |

By line coverage on `bufferedio.c`: the extracted functions span **881 of 2,788 lines (32%)**.
`_io__Buffered_close_impl`, `_io__Buffered_detach_impl`, `_io__Buffered_seek_impl`,
`_io__Buffered_truncate_impl` — the homes of four of my findings — are all absent.

**This is a fix already made and never propagated.** `scan_refcounts.py:859-864`'s docstring says
verbatim: *"Handles both single-line and multi-line signatures, including Argument Clinic `_impl`
functions with `/*[clinic ...]*/` comments between `)` and `{`."* `measure_c_complexity.py:186-190`
says the naive form *"silently dropp[ed] every multi-line parameter list — 22.4% of functions on a
measured CPython sample."* `scan_error_paths.py` never received either fix.

**Concrete consequences in this run**, all verified:
- The `bufferedio.c:1490` sibling of the scanner's own finding is unreportable (EP-4).
- `denominators.functions_analyzed: 157` for the seven files understates the truth (**289**) by
  **46%**, so the sample JSON's headline denominator is itself wrong.
- `pylong_sentinel_no_errcheck`, `int_status_never_tested`, `missing_null_check` and
  `alloc_null_no_memerror` all ran over half the slice and their zeros are half-structural.

**Proposal (P1, highest value in this report):** delete
`scan_error_paths.find_functions` and import `scan_refcounts`'s implementation (or, better, move
it into `scan_common.py` and have all four regex-based scanners import the one copy —
`scan_null_checks` and `measure_c_complexity` already agree with tree-sitter to within 1%, so the
shared version should be one of those two). Add a regression test asserting that
`find_functions("static PyObject *\nf_impl(x *s)\n/*[clinic end generated code: output=a input=b]*/\n{\n    return NULL;\n}\n")`
yields `f_impl`. Then re-run `scan_error_paths` over the whole campaign — every slice completed so
far ran with this blind spot.

### Recall gap B — `tree_sitter_utils.extract_functions` loses **10.4%** of `Modules/`, and a brace straddling a `#ifdef` is why

The other ten scanners use `tree_sitter_utils.extract_functions`. On `fileio.c` it returns **6 of
32** function definitions.

Root cause, localized: `Modules/_io/fileio.c:483-490`

```c
#ifdef MS_WINDOWS
        if (GetLastError() == ERROR_INVALID_HANDLE) {
            PyErr_SetFromWindowsErr(0);
#else
        if (errno == EBADF) {
            PyErr_SetFromErrno(PyExc_OSError);
#endif
            goto error;
        }
```

Two `{`, one shared `}` after `#endif`. The C is correct — the preprocessor picks one arm — but
tree-sitter-c cannot model a brace that straddles a conditional. Measured consequences:
`_io_FileIO___init___impl`'s `function_definition` node spans **lines 249–1349** (the rest of the
file), with `ERROR` nodes at `:471` and `:542` and a `MISSING` at `:1349`; unmatched-brace analysis
on the comment-stripped source confirms depth 2 at `:546` where it should be 1. Setting
`scrub=False` does not help (still 6). Two simpler suspects — a label followed by `#ifdef`, and
`PyMem_New(struct T, 1)` — each produce an `ERROR` node in isolation but **do not** lose functions;
I tested both.

Tree-wide over `Modules/**/*.c` (198 files with ≥5 column-0 definitions):

```
extract_functions missed 875 / 8424 definitions (10.4%)
files losing >= 30%: 17
   190/ 296  Modules/_cursesmodule.c        43/  43  Modules/faulthandler.c
   102/ 289  Modules/_decimal/_decimal.c    30/  30  Modules/_io/winconsoleio.c
    64/ 109  Modules/_tkinter.c             26/  32  Modules/_io/fileio.c
    40/  62  Modules/hmacmodule.c           23/  63  Modules/signalmodule.c
    22/  60  Modules/mmapmodule.c            9/  30  Modules/_ctypes/_ctypes_test.c
```

`faulthandler.c` loses **43 of 43**.

The harm is subtler than gap A and worth stating precisely: the *text* of the swallowed region is
still inside the giant node, so a purely textual rule still sees it — but

- every finding in it is **attributed to the wrong function** (all of `fileio.c:547-1349` reports
  as `_io_FileIO___init___impl`, with line numbers computed from that function's body start);
- every **per-function invariant** is evaluated over a 1,100-line conglomerate. `scan_gil_usage`
  on `fileio.c` reports `functions_analyzed: 6` and `Py_BEGIN_ALLOW_THREADS: 7 / END: 7` —
  balanced *in aggregate*, so an unbalanced pair in one real function would be masked by another
  function's compensating pair. The brief called this a "real negative" for `scan_gil_usage`
  because `fileio.c` uses `Py_BEGIN_ALLOW_THREADS`; **it is not** — it is one 1,100-line
  pseudo-function's aggregate balance;
- every **name-keyed gate** misfires: the destructor-family skip, `*_lock_held`, the clinic
  `_impl` heuristics all see one name.

**Proposal (P2):** in `extract_functions`, after collecting nodes, detect the pathology
mechanically — a `function_definition` whose span exceeds, say, 400 lines *or* that contains an
`ERROR`/`MISSING` descendant — and fall back to the regex extractor for that file, emitting
`degraded_files: [...]` in the envelope. Cheap, and it converts a silent wrong answer into a
labelled one. **Proposal (P3):** add `extract_functions_recall` (tree-sitter names vs column-0
definition names) to every envelope's `denominators`. The campaign's whole "check the denominator"
discipline is defeated by a denominator that is itself derived from the broken extractor —
`scan_gil_usage`'s `functions_analyzed: 6` for a 32-function file was the tell, and nothing
surfaced it.

### Recall gap C — no rule models this slice's dominant bug shape

Nine of my ten reproduced crashes are one shape that no scanner in the toolkit expresses:

> a field read from `self` is guarded, then a call that can run arbitrary Python intervenes, then
> the *same field* is read again and used without a re-check.

`scan_refcounts`'s `stale_slot_use` is the nearest relative but is about *ownership* (a stale
local across a DECREF), not about a **slot that a re-entrant call can set to NULL**. The v0.9 FP
taxonomy entry "*A re-read of the slot after the call is the guarded twin*" has this exactly
backwards for `_io`: here the re-read **is** the bug, because the writer sets the slot to `NULL`
rather than swapping it.

**Proposal (P4) — new rule `stale_slot_after_python_call`:**
- find `self-><field>` where `<field>` is `Py_CLEAR`ed or assigned `NULL` anywhere in the file
  (`bufferedio.c:419,626`; `textio.c:1213-1225,1516-1525,1642`) — that is what makes the field
  *user-nullable*;
- inside one function, find a guard on that field (`if (self->f == NULL)`, `CHECK_*`, or a
  named accessor), then a call in `PYTHON_REACHING_APIS` (already defined), then another read of
  the same field with no intervening re-guard;
- **suppression:** if the file defines an accessor helper whose body tests the field and every
  read goes through it (`buffer_access_safe`), the field is covered — report nothing.
  That suppression is what makes the rule quiet on the hardened half of `textio.c` while still
  firing on `self->encoder` / `self->decoder`.

Expected yield on this slice from the sites I confirmed by hand: 8 in `bufferedio.c` + 2 in
`textio.c`, and the suppression correctly silences the 32 hardened `self->buffer` reads. Seed the
rule's regression fixture from `Modules/_io/bufferedio.c` before it is fixed, and from
`textio.c`'s `buffer_access_safe` for the negative.

**Proposal (P5), cheaper and orthogonal:** a one-line lint — `PyObject_CallMethodNoArgs` /
`PyObject_CallMethodOneArg` / `PyObject_GetOptionalAttr` / `PyObject_GetAttr` called with a
`self-><ptr field>` receiver that is `Py_CLEAR`ed elsewhere in the same file. That alone flags
seven of my eight `bufferedio.c` sites with no dataflow at all. Site (h) shows why the receiver
matters: `PyObject_CallMethodObjArgs` NULL-checks and the others do not.

### Method notes

- Ambient `python` is RustPython; every run named an explicit interpreter. All reproducers are
  `.py` **files** in `repro/`, no heredocs.
- Every crash claim carries an exit code **and** a gdb frame. EP-6 is the one finding with a
  mechanism and no measurement, and is labelled CONSIDER for that reason.
- `_pyio` is used as an oracle only where it makes the corresponding call. For EP-4 and EP-5 it
  does not, and I say so rather than counting its silence as agreement.

---

## Files written

```
reports/mod-io/repro/io_buffered_close_detach.py       EP-1(a) minimal, 12 lines of pure Python
reports/mod-io/repro/io_buffered_truncate_detach.py    EP-1(c) minimal
reports/mod-io/repro/io_buffered_stale_raw_sweep.py    EP-1 all sites + 2 textio controls (14 cases)
reports/mod-io/repro/io_textio_stale_codec_sweep.py    EP-3 (4 cases)
reports/mod-io/repro/io_buffered_init_swallow.py       EP-4 (both sites)
reports/mod-io/repro/io_fileio_index_swallow.py        EP-5
reports/mod-io/repro/sweep_driver.py                   subprocess-per-case matrix runner
reports/mod-io/repro/run_matrix.sh                     single-script debug/release/_pyio runner
```

## Recommended findings records

New, no tracker prior art: EP-1 (8 sites, one record — the family, with the un-propagated
`gh-143008` fix as the guarded twin, and the note that open PR `gh-143577` does not cover it),
EP-2 (seeded; fold into EP-1's record or keep separate as the "NULL without exception" variant),
EP-3 (2 sites), EP-4 (2 sites). EP-5 and EP-6 are CONSIDER. Gaps A/B/C are **toolkit** issues for
`cpython-review-toolkit`, not CPython findings; A is the one to fix before the next slice, since
every slice completed so far ran `scan_error_paths` over half its code.
