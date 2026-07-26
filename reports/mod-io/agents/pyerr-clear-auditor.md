# pyerr-clear-auditor — slice `mod-io` (informed mode)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** the 7 files in `preflight/slice_files.txt`. `winconsoleio.c` excluded.
**Builds:** `debug-gil-nojit`, `release-gil-nojit` (both @ `a1d580430c8`).
**Diff check:** all seven slice files **and** `Lib/_pyio.py` are **byte-identical**
between `a1d580430c8` (build matrix) and `4f3be1b5777` (review target) —
`git diff --quiet` clean for each. Every line number below is valid on both.

---

## Summary

- **Clears examined: 22** (raw `grep -c 'PyErr_Clear'` over the 7 files = 22;
  scanner `total_pyerr_clear_calls` = 22 — **reconciled exactly, no attribution gap**).
  No `_PyErr_Clear(tstate)` spellings in this slice.
- **Destructor-family functions: 27 attributed / 30 real** (see D-1 — `fileio.c`'s
  three are invisible to the AST). **3 of the 22 clears sit in a destructor**, all
  three in `iobase_finalize`, all three correctly bracketed.
- Scanner findings: 1. Confirmed as a true positive; **4 un-flagged siblings found
  by reading**; **2 provably-dead clears**; **1 exception-state write in a
  tp_dealloc the rule cannot see**; **1 reproduced SIGSEGV** downstream of a clear site.

**FIX: 3 · CONSIDER: 4 · POLICY: 2 · ACCEPTABLE: 13**

| verdict | sites |
|---|---|
| FIX | `bufferedio.c:495`, `textio.c:3250`, `bufferedio.c:591` (cross-class, reproduced SIGSEGV) |
| CONSIDER | `bufferedio.c:870`, `bufferedio.c:1490`, `fileio.c:303`, `bytesio.c:1081-1084` |
| POLICY | `bufferedio.c:759`, `iobase.c:320` |
| ACCEPTABLE | `iobase.c:307/314`, `bufferedio.c:892/1551/2150/2205`, `textio.c:3094`, `_iomodule.c:542`, `fileio.c:664/702/841/904/946` |
| dead code (no verdict) | `bufferedio.c:578`, `fileio.c:184` |

---

## Findings

### [FIX] `_io._Buffered._dealloc_warn` bare-clears whatever the raw object raised (`Modules/_io/bufferedio.c:495`)

This is the scanner's one finding. **Confirmed true positive**, and the guarded
twin is 400 lines away in a sibling file with the *same method name*.

```c
/* Modules/_io/bufferedio.c:485-498 */
_io__Buffered__dealloc_warn_impl(buffered *self, PyObject *source)
{
    if (self->ok && self->raw) {
        PyObject *r;
        r = PyObject_CallMethodOneArg(self->raw, &_Py_ID(_dealloc_warn), source);
        if (r)
            Py_DECREF(r);
        else
            PyErr_Clear();              /* :495 */
    }
    Py_RETURN_NONE;
}
```

**Guarded twin — `fileio_dealloc_warn`, `Modules/_io/fileio.c:103-118`.** Same
`_dealloc_warn` protocol, same teardown role, and it does all four things the
buffered version does none of:

```c
PyObject *exc = PyErr_GetRaisedException();                 /* 1. save        */
if (PyErr_ResourceWarning(source, 1, "unclosed file %R", source)) {
    if (PyErr_ExceptionMatches(PyExc_Warning)) {            /* 2. narrow      */
        PyErr_FormatUnraisable("Exception ignored "         /* 3. report      */
                               "while finalizing file %R", self);
    }
}
PyErr_SetRaisedException(exc);                              /* 4. restore     */
```

**What I measured** (`repro/io_dealloc_warn_swallow.py`, a raw object whose
`_dealloc_warn` raises `KeyboardInterrupt`):

| build | backend | direct `b._dealloc_warn(b)` | rc |
|---|---|---|---|
| debug-gil-nojit | `io` (C) | returned `None`, **KeyboardInterrupt swallowed** | 0 |
| debug-gil-nojit | `_pyio` | **propagated** `KeyboardInterrupt` | 0 |
| release-gil-nojit | `io` (C) | returned `None`, **KeyboardInterrupt swallowed** | 0 |
| release-gil-nojit | `_pyio` | **propagated** `KeyboardInterrupt` | 0 |

`sys.exc_info()` after the call is `(None, None, None)` on the C side: the
exception is gone, not deferred. The oracle (`Lib/_pyio.py:863`) is a plain
two-line dispatch with no swallow, so this is a genuine C-vs-Python parity gap
with the C side losing the exception.

**Second measured difference — diagnosability at teardown.** In the same run the
`_pyio` backend emitted two `Exception ignored while calling deallocator ...
KeyboardInterrupt` unraisable reports when the objects were finalized. The C
backend emitted **nothing at all**. `iobase_finalize` reports a failing
`close()` unraisably (`iobase.c:323`), but `_dealloc_warn` has already eaten the
exception before `close()` returns, so the report never fires.

**Why this is not the "sentinel-returning lookup is idiomatic" FP class:**
`PyObject_CallMethodOneArg` on a runtime object dispatches arbitrary user
Python. Its `NULL` means only that *something* was raised. The taxonomy's own
refinement (briefing §"PyErr_Clear — widened-scanner FP classes") names exactly
this case as *not* idiomatic.

**Reachability is not limited to `__del__`.** `_dealloc_warn` is a public
`METH_O` method (`clinic/bufferedio.c.h:308`), and `_finalizing` is a **writable**
`Py_T_BOOL` member (`bufferedio.c:2578`, `:2637`, `:2754`, flags `0`), so
`b._finalizing = True; b.close()` drives `bufferedio.c:573→574→491` from pure
Python with no finalizer involved. Measured: works, no exception surfaces.

**Fix:** adopt `fileio_dealloc_warn`'s shape verbatim — save, narrow to
`PyExc_Warning`, `PyErr_FormatUnraisable`, restore.

---

### [FIX] `_io_TextIOWrapper_close_impl` repeats the same bare clear (`Modules/_io/textio.c:3250`) — scanner-missed sibling

```c
/* Modules/_io/textio.c:3241-3252 */
if (self->finalizing) {
    res = buffer_callmethod_onearg(self, &_Py_ID(_dealloc_warn), (PyObject *)self);
    if (res) { Py_DECREF(res); }
    else     { PyErr_Clear(); }        /* :3250 */
}
```

Structurally identical to `bufferedio.c:495`: the buffer is a runtime object,
`_dealloc_warn` is user-overridable, the clear is unnarrowed, and there is no
save/restore or unraisable report. Same fix, same twin.

`TextIOWrapper._finalizing` is likewise a writable member (`textio.c:3488`), so
this is reachable from pure Python via `tw._finalizing = True; tw.close()`.

**Why the scanner missed it:** rule 3 requires the innermost enclosing branch to
test the failure of a call in its Python-reaching table.
`buffer_callmethod_onearg` is a **file-local static helper** (`textio.c:785`)
that wraps `PyObject_CallMethodOneArg`, so the table lookup fails. See T-1.

---

### [FIX / cross-class] `_io._Buffered.close()` dereferences a NULL `self->raw` at `Modules/_io/bufferedio.c:591` — **reproduced SIGSEGV**

This is briefing §3's archetype at an unswept site, reached through my own clear
sites, and it is the strongest result of this pass. It is **not** a PyErr_Clear
bug — flagging it here because task item (d) asked for the cross-check, and
because the two clears on the same path (`:495`, `:578`) are what makes the
preceding user callback silent. **Dedupe against `null-safety-scanner` /
`init-bypass-checker`.**

```c
/* Modules/_io/bufferedio.c:580-591 */
    /* flush() will most probably re-take the lock, so drop it first */
    LEAVE_BUFFERED(self)                                   /* :581 lock DROPPED  */
    r = _PyFile_Flush((PyObject *)self);                   /* :582 user Python   */
    if (!ENTER_BUFFERED(self)) { return NULL; }            /* :583               */
    PyObject *exc = NULL;
    if (r < 0) { exc = PyErr_GetRaisedException(); }
    res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close));   /* :591 stale  */
```

`_io__Buffered_detach_impl` sets `self->raw = NULL` (`:626`). Because `:581`
drops the buffered lock before the user flush, a subclass whose `flush()` calls
`self.detach()` lands inside that window; `:591` then calls a method on `NULL`.

**Measured** (`repro/io_close_flush_detach.py`):

| build | backend | result | rc |
|---|---|---|---|
| debug-gil-nojit | `io` (C) | `Assertion 'obj != NULL' failed` — `Include/internal/pycore_stackref.h:628` (`PyStackRef_FromPyObjectBorrow`) → **SIGABRT**, **5/5 runs** | **134** |
| release-gil-nojit | `io` (C) | **SIGSEGV**, core dumped, **5/5 runs** | **139** |
| debug-gil-nojit | `_pyio` | `AttributeError: 'NoneType' object has no attribute 'close'` | 0 |
| release-gil-nojit | `_pyio` | `AttributeError: 'NoneType' object has no attribute 'close'` | 0 |

**gdb frame** (release build) — `self=0x0` at the exact line:

```
#2  PyObject_CallMethodNoArgs (self=0x0, name=...) at ./Include/cpython/abstract.h:65
#3  _io__Buffered_close_impl (self=0x7ffff74a80f0) at ./Modules/_io/bufferedio.c:591
```

C SIGSEGV / `_pyio` raises cleanly → confirmed, localized C bug per the brief's
grading table.

**Guarded twin — `_io_TextIOWrapper_close_impl`, `textio.c:3257`.** The identical
"flush, then close the underlying object" sequence, but it reaches the buffer
through `buffer_callmethod_noargs` → `buffer_access_safe` (`textio.c:740`),
which re-checks `CHECK_ATTACHED` after the re-entrancy point. Measured
(`repro/io_close_dealloc_warn_detach.py textio`): raises
`ValueError: underlying buffer has been detached`, rc=0, both builds.

**Prior art — this is an unswept sibling of a fix that already landed.**
`db4b1948bc4` (gh-143008 / PR #145957, *"Fix Null pointer dereferences in
TextIOWrapper underlying stream access"*, 2026-06-09) introduced
`buffer_access_safe()` precisely to close this class, and its commit message
says *"The cases which need to be re-checked are hard to spot so rather than
rely on reviewer effort create better safety by making all self->buffer access
go through helper functions."* `git show --stat` confirms it touched
**`textio.c` only** — `bufferedio.c`, which has the structurally identical
`self->raw` field, was never swept. Related: gh-142594 (closed, `TextIOWrapper.close`),
gh-143008 (closed, `TextIOWrapper.truncate`), **gh-143375 (OPEN,
`BufferedWriter.seek` during re-entrant close)** — same family, different site;
I found no issue covering `_io__Buffered_close_impl` itself.

**Fix:** give `bufferedio.c` the `buffer_access_safe` treatment —
a `raw_access_safe(self)` helper that re-checks `self->ok`/`self->raw` and
raises, used at `:591` and at every other post-callback `self->raw` read.

**Not-measured / negative result on the same page.** My first attempt
(`repro/io_close_dealloc_warn_detach.py buffered`) tried to reach `:591` through
`raw._dealloc_warn` at `:574` instead of through the flush. That path is
**blocked**: `_dealloc_warn` runs while `ENTER_BUFFERED` is held, so the
re-entrant `detach()` → `_PyFile_Flush` → `buffered_flush` hits
`_enter_buffered_busy`'s same-thread check and raises
`RuntimeError: reentrant call inside <_io.BufferedWriter>` (measured, rc=0 both
builds). The `:581` lock-drop window is what makes the bug reachable. State this
if the fix is scoped: the hole is the unlocked interval, not the callback.

---

### [CONSIDER] Unnarrowed clear of a user `raw.tell()` exception — two sites, one flagged (`Modules/_io/bufferedio.c:870` and `:1490`)

```c
/* :869-870 in _buffered_init   and   :1489-1490 in _io__Buffered_truncate_impl */
if (_buffered_raw_tell(self) == -1)
    PyErr_Clear();
```

`_buffered_raw_tell` calls `PyObject_CallMethodNoArgs(self->raw, &_Py_ID(tell))`
— arbitrary user Python. Its `-1` means only that something was raised, so a
`MemoryError`, `KeyboardInterrupt` or `RecursionError` from a user `tell()` is
discarded along with the expected `OSError`. At `:870` the constructor then
returns 0 (success) with the position simply marked unknown; at `:1490`
`truncate()` returns its result normally.

**Coordination:** `:870` is `scan_error_paths`' `unconditional_pyerr_clear`
finding. I do **not** re-litigate it — same site, one report. My added value is
(a) the mechanism: it is not clearing "nothing", it is clearing whatever a user
`tell()` raised, and (b) **`:1490` is the un-flagged twin neither scanner
reported**. Both need the same fix.

**Guarded twin in the same file:** `buffered_repr` (`bufferedio.c:1547-1551`)
narrows with `if (!PyErr_ExceptionMatches(PyExc_ValueError)) return NULL;`
before its clear. The narrowing idiom is already house style here.

CONSIDER rather than FIX: the outcome is a wrong-but-valid object (an
initialised buffer with `abs_pos == -1`), not memory unsafety.

---

### [CONSIDER] `FileIO.__init__` clears whatever a user `__index__` raised (`Modules/_io/fileio.c:303`)

```c
fd = PyLong_AsInt(nameobj);
if (fd < 0) {
    if (!PyErr_Occurred()) { PyErr_SetString(PyExc_ValueError, ...); return -1; }
    PyErr_Clear();                       /* :303 */
}
```

`PyLong_AsInt` calls `__index__` on a non-int `nameobj`. The intended discard is
the `TypeError` that says "this is a path, not an fd", but the clear is
unnarrowed, so a `KeyboardInterrupt` or `MemoryError` raised inside a user
`__index__` is destroyed and `open()` continues to
`PyUnicode_FSConverter`/`PyUnicode_FSDecoder`, which then raises its own
`TypeError`. Net effect: an async signal is converted into a wrong exception
type. No crash. Narrow to `PyExc_TypeError` before clearing.

---

### [CONSIDER] `bytesio_dealloc` mutates exception state in a `tp_dealloc` with no bracket (`Modules/_io/bytesio.c:1081-1084`)

```c
static void
bytesio_dealloc(PyObject *op)
{
    ...
    _PyObject_GC_UNTRACK(self);
    if (FT_ATOMIC_LOAD_SSIZE_RELAXED(self->exports) > 0) {
        PyErr_SetString(PyExc_SystemError,
                        "deallocated BytesIO object has exported buffers");
        PyErr_Print();                    /* :1084 */
    }
```

This is a destructor-class hit that **`scan_pyerr_clear.py` structurally cannot
see**: the rule matches only `PyErr_Clear` / `_PyErr_Clear`, and here the clear
is spelled `PyErr_Print()`. If reached with a live exception, `PyErr_SetString`
chains it into the `SystemError` as `__context__`, and `PyErr_Print()` then
prints and **clears** it — and additionally runs `sys.excepthook`, i.e.
arbitrary user Python, from inside a `tp_dealloc` after `_PyObject_GC_UNTRACK`.
`bytesio_dealloc` does **not** route through `_PyIOBase_finalize`, so
`iobase_finalize`'s save/restore never covers it.

**What I measured** (`repro/io_bytesio_dealloc_exports.py`): four shapes —
cycle through the instance `__dict__`, cycle through a self-referential list,
cycle via the inner `_io._BytesIOBuffer`, and a `BytesIO` subclass with
`__del__` — **none reached `:1084`** on either build (rc=0, no `SystemError`
printed). Mechanism, offered as a hypothesis with its falsifier: `exports` is
decremented by `bytesiobuf_releasebuffer`, and the `bytesiobuf` holds a strong
reference to the `BytesIO` (`obj->source`) while having **no `Py_tp_clear` slot**
(`bytesiobuf_slots`, `bytesio.c:1340-1348`), so the collector must break such a
cycle at the `memoryview` or at `bytesio_clear`, either of which drives
`exports` to 0 before the `BytesIO` dies. Falsifier: any construction that
destroys a `BytesIO` while a `Py_buffer` over its `bytesiobuf` is still live.

The probe surfaced a related, *correctly handled* behaviour worth recording:
during those collections `_io.BytesIO.close()` raised
`BufferError: Existing exports of data: object cannot be re-sized`
(`_io_BytesIO_close_impl` → `CHECK_EXPORTS`, `bytesio.c:928`), and
`iobase_finalize` reported it as
`Exception ignored while finalizing file <_io.BytesIO ...>` — 3 reports per run,
both builds. That is the guarded path working.

CONSIDER, not FIX: latent. But the guarantee that makes it unreachable is a
missing `tp_clear` on an internal type, which is exactly the sort of thing a
free-threading or GC change flips.

---

### Two provably-dead clears

- **`Modules/_io/bufferedio.c:578`** — `if (r) Py_DECREF(r); else PyErr_Clear();`
  where `r = _io__Buffered__dealloc_warn_impl(self, (PyObject *)self)` (`:574`).
  That impl's only exit is `Py_RETURN_NONE` (`:497`), so `r` is never `NULL` and
  the clear is unreachable.
- **`Modules/_io/fileio.c:184`** — identical shape around
  `fileio_dealloc_warn(...)` (`:179`), whose only exit is likewise
  `Py_RETURN_NONE` (`fileio.c:117`).

Neither is a live bug. Both are worth deleting: they are error handling that
*looks* like it covers the callback, and their presence is part of why
`bufferedio.c:495`'s real swallow reads as intentional.

---

### [POLICY] Deliberate, in-code-documented swallows

- **`Modules/_io/bufferedio.c:759`** (`_set_BlockingIOError`). Unconditional
  `PyErr_Clear()` at function entry, documented by the function's own header
  comment *"Sets the current error to BlockingIOError"* and by the caller
  comment at `:2160-2162` (*"we just replace with a new error"*). It is an error
  *translation*, and one of its two callers reaches it with nothing pending.
- **`Modules/_io/iobase.c:320`** — `if (PyObject_SetAttr(self, &_Py_ID(_finalizing),
  Py_True)) PyErr_Clear();` inside `iobase_finalize`'s save/restore bracket. The
  outer exception is safe. Recorded as POLICY rather than ACCEPTABLE because of
  an internal asymmetry: 3 lines below, the `close()` failure gets
  `PyErr_FormatUnraisable` (`:323`), while this failure is dropped with no
  report at all — and dropping it silently changes behaviour, because `close()`
  then does not know it is finalizing and skips the ResourceWarning.

---

### ACCEPTABLE — 13 sites, each with the reason

| site | why |
|---|---|
| `iobase.c:307`, `:314` | inside `iobase_finalize`'s `PyErr_GetRaisedException()` (`:302`) / `PyErr_SetRaisedException(exc)` (`:332`) bracket; I verified the pair positionally brackets **all three** clears, per the taxonomy's warning about whole-function suppression |
| `bufferedio.c:892` | `_PyIO_trap_eintr`: the exception is already in `exc` via `PyErr_GetRaisedException()`; the clear only drops what `PyLong_AsLongAndOverflow` set, and `PyErr_SetRaisedException(exc)` follows |
| `bufferedio.c:1551` | narrowed: `if (!PyErr_ExceptionMatches(PyExc_ValueError)) return NULL;` |
| `bufferedio.c:2150` | narrowed by `_buffered_check_blocking_error()` (`:770`), which uses `PyErr_GivenExceptionMatches(exc, PyExc_BlockingIOError)`; the clear discards a `BlockingIOError` the function itself provoked, on a path that then succeeds |
| `bufferedio.c:2205` | **success-path clear, provably a no-op.** `_bufferedwriter_raw_write` returns `-2` only when `raw.write()` returned `Py_None`, i.e. with nothing set (`:2002-2008`); every other loop exit `goto error`s. Since a C method is never entered with a pending exception, the clear can only ever clear nothing. Static-confirmed by reading, not measured. The odict fix applies verbatim: delete it, add `assert(!PyErr_Occurred())` |
| `textio.c:3094` | narrowed to `PyExc_ValueError` |
| `_iomodule.c:542` | narrowed by `PyErr_GivenExceptionMatches(runerr, PyExc_OverflowError)` |
| `fileio.c:702`, `:841`, `:904`, `:946` | the four `errno == EAGAIN` clears. **Not** an unnarrowed clear: `_Py_read`/`_Py_write` restore `errno` to the syscall's own value before returning `-1` (`Python/fileutils.c:1909`, `:1915`), and the signal-interrupted path is asserted to leave `errno == EINTR` with the exception set (`fileutils.c:1910`). So `errno == EAGAIN` implies the pending exception is the `OSError` that `PyErr_SetFromErrno` just raised — errno *is* the narrowing predicate. `fileio.c:702/904/946` additionally snapshot `errno` before any `Py_buffer`/writer teardown |
| `fileio.c:664` | `portable_lseek(self, NULL, SEEK_CUR, false)` with `posobj == NULL` runs no Python (`fileio.c:982-990` is skipped); the only reachable exceptions are the `lseek` `OSError` and a `PyLong_From*` `MemoryError`. Deliberate: `seekable()` must answer False for a pipe |

---

## Classes bounded

Each negative below is stated with the denominator that makes it meaningful.

1. **Unguarded clears in the destructor family: ZERO, over a real denominator.**
   27 destructor-family functions attributed (enumerated below), **3** of the 22
   clears live in one — `iobase_finalize` (`iobase.c:295`), slot `tp_finalize`,
   clears at `:307`, `:314`, `:320`. All three are inside the
   `PyErr_GetRaisedException()`/`PyErr_SetRaisedException()` bracket at
   `:302`/`:332`. I verified positionally, not by whole-function suppression.
   `iobase_finalize` **is** the guarded twin the briefing's
   `pyerr-clear-in-destructor` shape asks for, and `_io` inherits it: every
   `_io` type routes teardown through `_PyIOBase_finalize` →
   `PyObject_CallFinalizer{,FromDealloc}` → `iobase_finalize`, so the
   *caller's* exception is protected across the whole close-driven-by-`__del__`
   path. That is why the `_dealloc_warn` findings above are scoped to "eats what
   the callback raised", not "eats the caller's exception".

   The 27, with per-function clear counts:

   ```
   textio.c:270  incrementalnewlinedecoder_traverse  tp_traverse  0
   textio.c:280  incrementalnewlinedecoder_clear     tp_clear     0
   textio.c:289  incrementalnewlinedecoder_dealloc   tp_dealloc   0
   textio.c:1511 textiowrapper_clear                 tp_clear     0
   textio.c:1531 textiowrapper_dealloc               tp_dealloc   0
   textio.c:1547 textiowrapper_traverse              tp_traverse  0
   bufferedio.c:414  buffered_clear                  tp_clear     0
   bufferedio.c:424  buffered_dealloc                tp_dealloc   0
   bufferedio.c:464  buffered_traverse               tp_traverse  0
   bufferedio.c:2303 bufferedrwpair_traverse         tp_traverse  0
   bufferedio.c:2314 bufferedrwpair_clear            tp_clear     0
   bufferedio.c:2324 bufferedrwpair_dealloc          tp_dealloc   0
   bytesio.c:1074 bytesio_dealloc                    tp_dealloc   0   <-- see CONSIDER above
   bytesio.c:1181 bytesio_traverse                   tp_traverse  0
   bytesio.c:1191 bytesio_clear                      tp_clear     0
   bytesio.c:1319 bytesiobuf_traverse                tp_traverse  0
   bytesio.c:1328 bytesiobuf_dealloc                 tp_dealloc   0
   stringio.c:609 stringio_traverse                  tp_traverse  0
   stringio.c:621 stringio_clear                     tp_clear     0
   stringio.c:632 stringio_dealloc                   tp_dealloc   0
   iobase.c:295  iobase_finalize                     tp_finalize  3   <-- all bracketed
   iobase.c:335  _PyIOBase_finalize                  tp_finalize  0
   iobase.c:351  iobase_traverse                     tp_traverse  0
   iobase.c:360  iobase_clear                        tp_clear     0
   iobase.c:370  iobase_dealloc                      tp_dealloc   0
   _iomodule.c:565 iomodule_traverse                 tp_traverse  0
   _iomodule.c:591 iomodule_clear                    tp_clear     0
   ```

   **Two caveats that keep this from being a full clean bill:** (i) the real
   count is **30** — `fileio_dealloc` (`fileio.c:567`), `fileio_clear` (`:559`)
   and `fileio_traverse` (`:550`) are invisible to the AST (D-1), so the
   destructor rule **never ran on `fileio.c`**. I read all three by hand: they
   contain no `PyErr_*` call at all, so the negative holds — but it holds
   because I read it, not because the scanner checked. (ii) the rule is blind to
   non-`PyErr_Clear` exception-state writes; `bytesio_dealloc` is one (T-3).

2. **`tp_traverse` clears: zero over 11 traverse functions.** All 11 are pure
   `Py_VISIT` chains. Genuine negative.

3. **`_PyErr_Clear(tstate)` private-alias clears: zero over the slice.**
   `grep -c '_PyErr_Clear'` = 0 in all 7 files. `_io` is module code, not
   runtime internals, so this is expected rather than surprising — stated so the
   zero is not read as coverage of the private spelling elsewhere.

4. **Success-path clears reachable with a caller exception live: zero.** I traced
   all 22. The only clear that runs with nothing this function set is
   `bufferedio.c:2205`, and it sits in a `METH_*` implementation which the eval
   loop never enters with a pending exception — a provable no-op, not a swallow.
   `bufferedio.c:759` runs with nothing pending from one of its two callers, but
   the other reaches it deliberately to translate a live `BlockingIOError`.

5. **Clear-count reconciliation: exact.** 22 raw greps = 22 attributed. Unlike
   the destructor denominator, no clear escaped attribution — the `fileio.c`
   parse damage misattributes 7 clears to the wrong function but does not lose
   them.

---

## Toolkit assessment

### Precision of the rule that fired

`pyerr_clear_unfiltered_after_python_call`: **1 fired, 1 true positive, precision
1.0 (n=1).** The `failing_call` field (`PyObject_CallMethodOneArg`) was correct
and the prescribed remedy (narrow before clearing) is the right shape, though
the actual fix here is stronger — the in-tree twin also saves/restores and
reports unraisably, which the finding text does not suggest.

### Recall gaps found by reading

**T-1 (highest value). Rule 3 does not resolve file-local static wrappers, and
that cost two FIX-grade siblings.** `bufferedio.c:578`, `textio.c:3250` and
`fileio.c:184` are the same `_dealloc_warn` shape as the one finding, and none
fired, because the failing call at each is a file-local helper
(`_io__Buffered__dealloc_warn_impl`, `buffer_callmethod_onearg`,
`fileio_dealloc_warn`) rather than a name in the Python-reaching table. Same
cause suppressed `bufferedio.c:870` and `:1490` (`_buffered_raw_tell`).

> **Proposal.** `analyze()` already extracts every function in the file before
> triaging. Compute a one-level transitive closure: a file-local static callee
> whose own body contains a Python-reaching call is itself Python-reaching.
> Cost is a dict build over data the scanner already has. Measured effect on
> this slice: +4 candidates (`bufferedio.c:578`, `:870`, `:1490`,
> `textio.c:3250`), of which 3 are real and 1 (`:578`) is dead code worth
> deleting anyway — i.e. **0 false positives added**. Cap the closure at depth 1
> to stay affordable.

**T-2 (slice-wide, affects every scanner). `Modules/_io/fileio.c` is 81%
unparsed, and nothing in the envelope says so.** `extract_functions` returns
**6** functions for a 1,349-line file with **32** column-0 definitions, and
reports `_io_FileIO___init___impl` as spanning **lines 249–1349** — the rest of
the file is swallowed into one pseudo-function. Two `ERROR` nodes drive it:
`fileio.c:471` (`PyMem_New(struct _Py_stat_struct, 1)` — a type name in a call
argument list, which tree-sitter-c cannot parse) and `fileio.c:542` (a `#ifdef`
directive immediately after the `done:` label). Consequences measured on this
slice: `destructor_functions` is 27 where it should be 30; the destructor rule
never ran on `fileio.c`; and the 5 clears past line 330 were triaged against a
1,101-line pseudo-function's dominance context. It cost no finding *here* (I
read all five by hand; all ACCEPTABLE), but that is luck.

Comparison for the other six files (AST count vs a crude column-0 `name(`
regex): `textio.c` 82/84, `bufferedio.c` 78/79, `bytesio.c` 49/48,
`stringio.c` 27/27, `iobase.c` 38/40, `_iomodule.c` 9/11 — all within noise.
`fileio.c` 6/32 is the sole outlier. `scan_error_paths.py` sees 17 functions in
the same file, so the damage is shared across the toolkit but not identically.

> **Proposal A (free, catches this exact case).** The destructor slot map is
> already built by **regex over the raw source** (`_collect_destructor_slot_map`).
> If a name in the slot map has no matching extracted function, that is a
> zero-cost, zero-false-positive parse-gap alarm. On `fileio.c` the slot map has
> all three of `fileio_dealloc`/`fileio_traverse`/`fileio_clear` and the AST has
> none. Emit them into `skipped_files` and add the count to the envelope.
>
> **Proposal B (general).** `if not functions: continue` (`scan_pyerr_clear.py:1140`)
> drops a file with **no trace at all** — that is how `winconsoleio.c` (4
> `PyErr_Clear` calls, 3 destructors, and the site of catalogued CPY-0008)
> contributes 0 to a whole-`Modules/_io` run whose envelope still says
> `files_analyzed: 16`. Record it in `skipped_files` with
> `reason: "no functions extracted (root.has_error=True)"`. Additionally flag any
> single extracted function spanning >40% of its file as `degraded`.

**T-3. The destructor rule's vocabulary is `PyErr_Clear` only; it should include
exception-state *writes*.** `bytesio_dealloc` destroys the pending exception via
`PyErr_SetString` + `PyErr_Print` and is invisible.

> **Proposal.** In the destructor family only, also trigger on
> `PyErr_Print` / `PyErr_PrintEx` / `PyErr_SetString` / `PyErr_SetObject` /
> `PyErr_Format` / `PyErr_SetFromErrno*` when no save/restore bracket and no
> `PyErr_*Unraisable` sits in the function. **Measured cost on this slice: 27
> destructor functions examined, exactly 1 would fire (`bytesio.c:1074`), and it
> is the one real hit.** Keep it destructor-scoped — outside teardown,
> `PyErr_SetString` is just raising.

**T-4. `total_pyerr_clear_calls_in_destructors` is reported but never surfaced
in the finding stream.** The envelope says `3`, and that `3` is the whole reason
the destructor negative is trustworthy here — all three are in one bracketed
function. A one-line `destructor_clears_suppressed` breakdown
(`function → count → suppression reason`) would let a reader confirm an earned
zero without re-deriving it, which is exactly what the brief's §4d asks for.

### Rules that did not fire, and whether that is earned

- `pyerr_clear_in_dealloc`: 0 over **3** destructor clears examined (of 27
  attributed / 30 real destructor functions). **Earned** for the clears it saw;
  **structurally blind** for `fileio.c` (T-2) and for non-`PyErr_Clear` writes (T-3).
- `pyerr_clear_on_success_path`: 0 over 22 clears. **Earned but marginal** — it
  correctly suppressed `bufferedio.c:759` (a re-raise API follows) and
  `bufferedio.c:2150`, and it missed `bufferedio.c:2205` because the enclosing
  `n == -2` reads as an error test. That miss is *correct behaviour for the gate
  as specified*; widening it would re-import exactly the class the taxonomy
  warns about. Leave it.

---

## Reproducers

All in `reports/mod-io/repro/`, all `.py` files, all run against
`~/projects/python_build_matrix/builds/<name>/python`:

| file | what it shows | measured |
|---|---|---|
| `io_dealloc_warn_swallow.py [io\|_pyio]` | `_dealloc_warn` swallows a user `KeyboardInterrupt`; `_pyio` propagates | rc 0 both builds; C swallows, `_pyio` propagates |
| `io_close_flush_detach.py [io\|_pyio]` | `_io__Buffered_close_impl:591` NULL-derefs `self->raw` | **debug rc=134 (SIGABRT), release rc=139 (SIGSEGV)**; `_pyio` rc=0 with `AttributeError` |
| `io_close_dealloc_warn_detach.py [buffered\|textio]` | negative control: the `_dealloc_warn` route to `:591` is blocked by `ENTER_BUFFERED`; textio's guarded twin raises `ValueError` | rc 0 all four combinations |
| `io_bytesio_dealloc_exports.py` | 4 shapes, none reaches `bytesio.c:1084` | rc 0 both builds; surfaced the `BufferError`-at-finalize path instead |

---

## Out-of-scope note

`Modules/_io/winconsoleio.c` (not in this slice) holds 4 `PyErr_Clear()` calls
and 3 destructor-family functions that **no** scanner in the toolkit currently
sees, because the file yields zero functions from `extract_functions` and is
then silently dropped. Whoever takes the winconsole slice should not trust a
zero from any of these scanners until T-2 Proposal B lands.
