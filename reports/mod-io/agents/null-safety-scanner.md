# null-safety-scanner — slice `mod-io` (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** the 7 files in `preflight/slice_files.txt`. `winconsoleio.c` not reviewed.
**Builds:** `debug-gil-nojit`, `release-gil-nojit`, `release-gil-nojit-asan` @ `a1d580430c8`.
**Drift check:** all seven slice files are **byte-identical** between `a1d580430c8` and
`4f3be1b5777` (`git diff --stat a1d580430c8 4f3be1b5777 -- Modules/_io/<f>` empty for each).
Every line number below is valid on both refs.

**Headline:** the scanner's one finding is a false positive (precision 0/1). Reading found
**8 reproduced SIGSEGVs** plus **1 ASan heap-use-after-free**, none of which the scanner can
represent, all instances of one shape: *a field that was non-NULL when its guard ran and NULL
when it was used.*

---

## Measured results

`repro/final_measure.py`, 5 reps per cell, plus `io_textio_encoder.py` measured separately.
`_pyio` column is the pure-Python oracle on the release build.

| case | C site | debug-gil-nojit | release-gil-nojit | `_pyio` |
|---|---|---|---|---|
| `_Buffered.close` | `bufferedio.c:591` | SIGABRT 5/5 | **SIGSEGV 5/5** | clean `AttributeError` |
| `_Buffered.seek` (`closed` property) | `bufferedio.c:1389` | SIGABRT 5/5 | **SIGSEGV 5/5** | clean |
| `_Buffered.truncate` | `bufferedio.c:1485` | SIGABRT 5/5 | **SIGSEGV 5/5** | clean `AttributeError` |
| `_bufferedreader_read_all` loop | `bufferedio.c:1748` | SIGABRT 5/5 | **SIGSEGV 5/5** | clean `AttributeError` |
| `_bufferedwriter_raw_write` loop | `bufferedio.c:1996` | SIGABRT 5/5 | **SIGSEGV 5/5** | survives |
| `TextIOWrapper.tell` | `textio.c:2873` | **SIGSEGV 5/5** | **SIGSEGV 5/5** | clean `TypeError` |
| `TextIOWrapper.write` | `textio.c:1783` | SIGABRT 5/5 | **SIGSEGV 5/5** | clean `LookupError` |
| `BytesIO.truncate` | `bytesio.c:193` (from `:761`) | SIGABRT 5/5 | **SIGSEGV 5/5** | survives |
| `BytesIO.truncate` stale exports | `bytesio.c:761` | SIGABRT 5/5 | ASan **heap-use-after-free** | survives |
| `_Buffered.detach` (seeded lead) | `bufferedio.c:625` | SIGABRT 5/5 | `SystemError` | survives |
| `_buffered_raw_seek` | `bufferedio.c:818` | `SystemError` | `SystemError` | survives |

All triggers are **pure Python**: no `_testcapi`, no threads, no OOM injection.

---

## Findings

### [ACCEPTABLE] The scanner's only finding is a false positive — `bufferedio.c:2444`

`bufferedrwpair_isatty` assigns `_forward_call(self->writer, isatty, NULL)` to `ret`, and the
scanner reports the `Py_DECREF(ret)` at `:2450` as an unchecked deref. It is checked — by the
early return three lines up:

```c
PyObject *ret = _forward_call(self->writer, &_Py_ID(isatty), NULL);   /* :2444 */
if (ret != Py_False) {
    /* either True or exception */                                     /* :2447 */
    return ret;                                                        /* :2448 */
}
Py_DECREF(ret);                                                        /* :2450 */
```

`NULL != Py_False`, so a NULL `ret` leaves at `:2448` and never reaches `:2450`. The in-code
comment at `:2447` states the invariant. This is a new sub-case of the taxonomy's
*positive-form check* class: the guard is an **inequality against a non-NULL singleton**, not
an `== NULL` / `!v` / `if (v)` form.

**Family sweep (all 13 `_forward_call` sites, `bufferedio.c:2361`–`:2452`) — clean.**
Nine (`read` `:2361`, `peek` `:2368`, `read1` `:2375`, `readinto` `:2382`, `readinto1` `:2389`,
`write` `:2396`, `flush` `:2403`, `readable` `:2410`, `writable` `:2417`) `return _forward_call(...)`
directly — the *result returned directly* FP class. **The guarded twin is `bufferedrwpair_close`
at `:2425-2431`**, the one sibling that keeps the value and therefore writes the explicit test:

```c
PyObject *ret = _forward_call(self->writer, &_Py_ID(close), NULL);
if (ret == NULL) { exc = PyErr_GetRaisedException(); }
else { Py_DECREF(ret); }
```

Separately, `_forward_call` NULL-checks its own `self` argument at `:2340-2344`, so a NULL
`self->reader`/`self->writer` (the GC'd-half state) raises `ValueError` rather than crashing.
`bufferedrwpair_closed_get:2459` duplicates that check with a different message
("being garbage-collected"); a cosmetic inconsistency, not a bug.

---

### [FIX] `bufferedio.c` never received the `self->buffer` hardening `textio.c` got — 5 SIGSEGVs

**What.** Every `_Buffered` method checks `CHECK_INITIALIZED` / `CHECK_CLOSED` on entry, then
runs arbitrary user Python, then keeps using `self->raw`. `_io__Buffered_detach_impl:625-628`
sets `self->raw = NULL`, so a re-entrant `detach()` invalidates the guard mid-function. The
five sites below then hand `NULL` to a call API that dereferences it.

| site | function | re-entrancy point in between | gdb frame |
|---|---|---|---|
| `:591` | `_io__Buffered_close_impl` | `_PyFile_Flush` at `:582`, with the buffered lock explicitly dropped at `:581` | `PyObject_CallMethodNoArgs (self=0x0)` → `_PyObject_GetMethodStackRef` (`Objects/object.c:1764`) |
| `:1389` | `_io__Buffered_seek_impl` | `CHECK_CLOSED` at `:1386` → `buffered_closed:526` → `PyObject_GetAttr(self->raw, closed)` — **a user `closed` property** | `_PyIOBase_check_seekable` (`iobase.c:416`), `self=0x0` |
| `:1485` | `_io__Buffered_truncate_impl` | `buffered_flush_and_rewind_unlocked` at `:1479` → `self->raw.write()` | `PyObject_CallMethodOneArg (self=0x1)` |
| `:1748` | `_bufferedreader_read_all` | the previous iteration's `self->raw.read()` in the same loop | `PyObject_CallMethodNoArgs (self=0x0)`, via `_io__Buffered_read_impl:1010` |
| `:1996` | `_bufferedwriter_raw_write` | the previous short `self->raw.write()`, loop at `:2039-2059` | `PyObject_CallMethodOneArg (self=0x1)`, via `_bufferedwriter_flush_unlocked:2040` ← `buffered_flush_and_rewind_unlocked:912` |

**Vehicle.** A `_Buffered` **subclass** with an inert `flush()`. `detach()` calls
`_PyFile_Flush(self)` on itself at `:622`; overriding `flush()` to a no-op both stops the
infinite recursion and lets `detach()` complete even while another frame holds
`ENTER_BUFFERED` — the buffered lock does *not* protect these sites, because `detach()` never
takes it.

**Guarded twin — in the sibling file, with the fix already written.**
`textio.c:739-760` `buffer_access_safe()`, whose header comment names this exact hazard:

```c
/* Helpers to safely operate on self->buffer.
   self->buffer can be detached (set to NULL) by any user code that is called
   leading to NULL pointer dereferences (see gh-143008, gh-142594). Protect
   against that by using helpers to check self->buffer validity at callsites. */
```

and its use at `textio.c:1637-1641`, the structural twin of `bufferedio.c:625`:

```c
/* _PyFile_Flush could detach before returning; raise an exception. */
buffer = buffer_access_safe(self);
if (buffer == NULL) { return NULL; }
```

A second twin for the `IS_CLOSED` shape: `bufferedio.c:367` calls `_PyFileIO_closed(self->raw)`
bare, while `textio.c:1576-1577` writes `if (self->raw != NULL) r = _PyFileIO_closed(self->raw);`.
(`_PyFileIO_closed` is `return (PyFileIO_CAST(self)->fd < 0);` — `fileio.c:94-97`, an
unconditional deref.) I could not build a Python path to `bufferedio.c:367` with a NULL `raw`,
because `fast_closed_checks` requires an *exact* `BufferedReader` over an *exact* `FileIO`;
recorded as latent, not reproduced.

**Prior art — this is an unpropagated fix, and the propagation gap is documented in the commits.**
- `db4b1948bc4` (gh-143008, PR #145957) — *"Fix Null pointer dereferences in TextIOWrapper
  underlying stream access"*. Its message: *"The cases which need to be re-checked are hard to
  spot so rather than rely on reviewer effort create better safety by making all self->buffer
  access go through helper functions."* `--stat`: **`Modules/_io/textio.c` only** (157 lines).
- `1d3854a19a3` (gh-142594, PR #142706) — *"fix by property calls io.TextIOWrapper.detach"*,
  the `closed`-property route. `--stat`: **`Modules/_io/textio.c` only** (3 lines).

Neither swept `bufferedio.c`. Two issues found the same class in `TextIOWrapper` and it was
fixed in place both times.

**The open PR does not cover this.** gh-143375 (OPEN) — *"Null pointer dereference in
`BufferedWriter.seek` during re-entrant close"* — is an independently-found instance. Its
PR #143577 C diff is a **single reorder**: it moves `PyNumber_AsOff_t` above `CHECK_CLOSED` in
`_io__Buffered_seek_impl`. That closes the `__index__` entry route into `seek`, and closes
nothing else: `CHECK_CLOSED` still runs the raw's `closed` property before `:1389`, so my
`seek_after_closed_property` reproducer still crashes after that PR, and `:591`, `:1485`,
`:1748`, `:1996` are untouched.

**Fix.** Port `buffer_access_safe` to `bufferedio.c` as `raw_access_safe(buffered *self)` and
route all eleven post-re-entrancy `self->raw` reads through it, rather than continuing to
point-patch entry routes. This is what gh-143008 concluded for `textio.c`.

**Repro:** `repro/io_postguard_null.py`, `repro/io_postguard_null2.py`.

---

### [FIX] `textio.c:2873` — `self->snapshot` NULLed between its own guard and its use

`_io_TextIOWrapper_tell_impl`:

```c
posobj = buffer_callmethod_noargs(self, &_Py_ID(tell));            /* :2853 user buffer.tell() */
if (self->decoder == NULL || self->snapshot == NULL) { ... }       /* :2857 THE GUARD        */
cookie.start_pos = PyLong_AsLongLong(posobj);                      /* :2863 runs __index__   */
...
assert(PyTuple_Check(self->snapshot));                             /* :2872 debug-only       */
if (!PyArg_ParseTuple(self->snapshot, "iO", &cookie.dec_flags, &next_input))  /* :2873 */
```

`posobj` is whatever the underlying buffer's `tell()` returned. `PyLong_AsLongLong` falls back
to `__index__` for a non-int, so arbitrary Python runs at `:2863` — **after** the `:2857`
guard. `_io_TextIOWrapper_write_impl:1868-1870` does `Py_CLEAR(self->snapshot)`, so an
`__index__` that writes one character NULLs it inside that window.

**Measured:** SIGSEGV 5/5 on **both** debug and release.
`PyArg_ParseTuple (args=0x0, format="iO") at Python/getargs.c:109`, frame #1
`_io_TextIOWrapper_tell_impl (self=...) at ./Modules/_io/textio.c:2873`. `_pyio` raises
`TypeError` cleanly. The `assert` at `:2872` is *also* a NULL deref (`PyTuple_Check(NULL)` →
`Py_TYPE(NULL)`), so the debug build crashes one line earlier rather than trapping.

**Guarded twin:** `textio.c:2857` itself — the guard is correct, it is merely stale. The
structural twin is `buffer_access_safe`, which exists in this very file for `self->buffer`.
**gh-143008's helper is field-specific**: `self->buffer` is protected, `self->snapshot`,
`self->decoder` and `self->encoder` are not.

**Prior art:** none found (`gh api search/issues`, four query forms).
**Repro:** `repro/io_textio_snapshot.py`.

---

### [FIX] `textio.c:1783` — `self->encoder` NULLed between its guard and its use

```c
if (self->encoder == NULL) { return _unsupported(...); }               /* :1741 THE GUARD */
...
PyObject *newtext = _PyObject_CallMethod(text, &_Py_ID(replace), ...); /* :1754 user code */
...
b = PyObject_CallMethodOneArg(self->encoder, &_Py_ID(encode), text);   /* :1783           */
```

`text` is caller-supplied; a `str` **subclass** overriding `replace()` runs Python at `:1754`.
`_textiowrapper_set_encoder:1032-1036` does `Py_CLEAR(self->encoder)` and leaves it NULL when
`_PyCodecInfo_GetIncrementalEncoder` fails, so `f.reconfigure(encoding=…)` to a codec with no
incremental encoder NULLs the field inside the window. (`:1754` is reached when
`writetranslate && writenl != NULL`, i.e. an explicit `newline="\r\n"`; the `:1783` arm is
taken when `self->encodefunc == NULL`, i.e. a codec without a fast path.)

**Measured:** SIGABRT 5/5 debug, **SIGSEGV 5/5 release**.
`PyObject_CallMethodOneArg (self=0x1)` ← `_io_TextIOWrapper_write_impl … textio.c:1783`.
`_pyio` raises `LookupError`.

**Guarded twin:** `textio.c:2798` `if (self->encoder) { … }` in `_io_TextIOWrapper_seek_impl` —
a *guarded at use* read of the same field, in the same file.

**Prior art:** none found.
**Repro:** `repro/io_textio_encoder.py`.

---

### [FIX] `bytesio.c:761` — `CHECK_CLOSED` is stale because the converter runs after it

```c
_io_BytesIO_truncate_impl(bytesio *self, PyObject *size)
{
    CHECK_CLOSED(self);                       /* :739  self->buf != NULL          */
    CHECK_EXPORTS(self);                      /* :740  exports == 0               */
    ...
    new_size = PyLong_AsLong(size);           /* :748  runs a user __index__       */
    ...
    if (new_size < self->string_size) {
        self->string_size = new_size;
        if (resize_buffer_lock_held(self, new_size) < 0)   /* :761 */
```

`close()` sets `self->buf = NULL` (`check_closed:49`). An `__index__` that closes the BytesIO
makes `resize_buffer_lock_held` run with `self->buf == NULL`:
`assert(self->buf != NULL)` at `:184` aborts on debug; on release `:193`
`size_t alloc = PyBytes_GET_SIZE(self->buf);` segfaults.

**Measured:** SIGABRT 5/5 debug, **SIGSEGV 5/5 release**; `_pyio` survives.
`resize_buffer_lock_held (self=…, size=2) at ./Modules/_io/bytesio.c:193`, frame #1
`_io_BytesIO_truncate_impl … bytesio.c:761`.

**Guarded twin — and it names the discriminator.** `_io_BytesIO_seek_impl` (`:813`) is safe
from the identical attack, and the only difference is the Argument Clinic signature:

| | converter | when `__index__` runs | verdict |
|---|---|---|---|
| `seek` | `pos: Py_ssize_t` (`:813`) | in `clinic/bytesio.c.h`, **before** the impl body | guard at `:816` is fresh — measured clean `ValueError` |
| `truncate` | `size: object` (`:736`) + manual `PyLong_AsLong` at `:748` | **after** `CHECK_CLOSED` at `:739` | crash |

**Sibling consequence — [FIX, routes to `memory-pattern-analyzer`]:** `CHECK_EXPORTS` at `:740`
is stale for the same reason. An `__index__` that takes `self.getbuffer()` makes `:761` resize
the buffer with a live export. **ASan on `release-gil-nojit-asan` reports
`heap-use-after-free`**, freed by
`_io_BytesIO_truncate_impl:761 → resize_buffer_lock_held:218 → resize_unshared_buffer_lock_held:110
→ _PyBytes_Resize → realloc`, read afterwards through the still-live `memoryview`
(`PyBuffer_ToContiguous`, `Objects/memoryobject.c:1063`). The `memoryview` keeps reporting
`len == 4096` over a 3-byte allocation.

**Prior art:** none for `truncate`. gh-143378 (closed) was an adjacent `BytesIO` UAF on the
**`write`** path under concurrent mutation — different function, different route.
**Repro:** `repro/io_other_types.py bytesio_truncate_index_closes`,
`repro/io_bytesio_stale_exports.py`.

---

### [CONSIDER] `bufferedio.c:818` — same shape, graceful outcome

`_buffered_raw_seek` reads `self->raw` at `:818`, reached from
`buffered_flush_and_rewind_unlocked:921` *after* `:912`'s writer-flush already ran
`self->raw.write()`. Measured: `SystemError: null argument to internal routine`, no crash,
identically on debug and release, because `PyObject_CallMethodObjArgs` routes through
`null_error()` while the `NoArgs`/`OneArg` forms do not (see the toolkit section). Still a
pure-Python-reachable `SystemError` from a documented API. Repro:
`repro/io_postguard_null2.py flush_rewind_seek`.

### [CONSIDER] Enumerated, not individually reproduced

- `bufferedio.c:1640` — `_bufferedreader_raw_read`'s `do { self->raw.readinto(…) } while (res == NULL && _PyIO_trap_eintr())` re-reads `self->raw` on retry. Needs an EINTR-shaped failure; I did not construct one.
- `bufferedio.c:1713` — `PyObject_GetOptionalAttr(self->raw, readall, …)` after `:1706`'s flush. `PyObject_GetAttr` derefs `Py_TYPE(v)` unconditionally, so this would crash; my probe took the no-`readall` branch instead and landed on `:1748`.
- `bufferedio.c:367` — bare `_PyFileIO_closed(self->raw)`; latent, blocked by the `fast_closed_checks` type constraint (see above).

### [CONSIDER, routes to `refcount-auditor`] `textio.c:2938` / `:2981-2982`

`next_input` is a **borrowed** reference into `self->snapshot`, parsed at `:2873`, then used at
`:2938` `PyBytes_AS_STRING(next_input)` and `:2981-2982` — across the whole `DECODER_GETSTATE`
/ `decoder.decode()` loop, which runs a **user-replaceable codec**. A decoder method that
replaces `self->snapshot` frees the tuple under the borrowed pointer. `assert(PyBytes_Check(next_input))`
at `:2876` is debug-only. Borrowed-ref-across-call, not NULL — handing over rather than
double-reporting.

---

## Classes bounded (clean negatives, with denominators)

- **`unchecked_alloc` / assignment-source NULL propagation — real negative, and the denominators
  make it meaningful.** 1 finding over **1,012 assignment sites**, **466 fallible sources
  resolved**, **151 local nullable helpers**, **312 functions**, 7 files. That one finding is a
  false positive, so the true rate is **0 of 1,012**. `_io` genuinely checks the results of its
  own allocations and fallible calls: every `PyBytes_FromStringAndSize`, `PyList_New`,
  `PyMem_Malloc` and `PyObject_Call*` result I read on the crash paths was tested before use.
  This is a real, earned negative for the class the scanner models.
- **`deref_before_check`: 0**, and `decref_of_nulled_outparam`: 0 over **28 call sites** — a
  small but non-zero denominator. `_io` has no `_PyTuple_Resize`-style out-parameter pattern.
- **(c) deref-like macros on fallible values — clean, 153 macro uses across the seven files.**
  Every `PyBytes_AS_STRING` / `PyBytes_GET_SIZE` / `PyUnicode_DATA` / `PyList_GET_ITEM`
  application to a value produced by a *user* call is preceded by an explicit type check:
  `bufferedio.c:1751` `if (data != Py_None && !PyBytes_Check(data))` before `:1755/:1766/:1768`;
  `textio.c:1789` `if (b != text && !PyBytes_Check(b))` before `:1802`; `textio.c:2760`
  `if (!PyBytes_Check(input_chunk))`; `check_decoded()` before every
  `PyUnicode_DATA(decoded)`. The two exceptions are **assert-only** and internal-invariant:
  `textio.c:2876` `assert(PyBytes_Check(next_input))` and `textio.c:2872`
  `assert(PyTuple_Check(self->snapshot))` — the latter is part of the `:2873` finding above.
- **`self->dict` — clean, and structurally so.** Across all seven files `self->dict` appears
  only in `tp_traverse` / `tp_clear` / `tp_dealloc` (`bufferedio.c:420,470,2308,2320`;
  `textio.c:1527,1563`; `fileio.c:554,562`; `iobase.c:356,364,390`). Python-level `__dict__`
  access goes through `tp_dictoffset` and `PyObject_GenericGetDict`, which are NULL-tolerant.
  No unguarded read exists to find.
- **`bytesio` exports counter vs resize (brief §4c) — the guard holds on the *direct* path.**
  `repro/io_other_types.py bytesio_resize_with_export` gets the intended
  `BufferError: Existing exports of data: object cannot be re-sized`. The counter is only
  defeated by the *ordering* bug above (export taken after `CHECK_EXPORTS` ran), not by the
  counter itself. I did **not** evaluate the `FT_ATOMIC_LOAD_SSIZE_RELAXED` ordering under a
  free-threaded build — that remains open for `ft-race-scanner`.
- **`stringio.c` / `fileio.c` / `iobase.c` / `_iomodule.c` — no instance of this shape found.**
  Probed `StringIO.truncate`/`.seek` and `FileIO.truncate` with a closing `__index__`
  (`repro/io_other_types.py`): all four produce clean `ValueError` / `OSError` on both builds.
  `stringio`'s converters are clinic-declared `Py_ssize_t`-or-`object`-with-early-conversion,
  i.e. the safe side of the `bytesio` seek/truncate discriminator.

## What I did **not** do

- No free-threaded or TSan runs. Every finding here is single-threaded re-entrancy; FT adds a
  second, concurrent route to the same fields that I did not measure. `textio.c` `self->buffer`
  already has an open FT issue (gh-154523).
- No OOM injection. None of these sites is on an allocation-failure path, so
  `run_oom_sweep.py` has nothing to drive; the reachability is ordinary Python.
- Did not reproduce `bufferedio.c:1640`, `:1713`, `:367` individually (see CONSIDER above).
- Did not audit the 12 `ENTER_BUFFERED` spans (brief §4a) end-to-end — outside this agent's
  class. One observation in passing: `detach()` takes no buffered lock at all, which is *why*
  the lock does not protect the five `bufferedio.c` sites.
- Did not review `winconsoleio.c` (out of scope).

---

## Toolkit assessment

### Precision of the rule that fired

`unchecked_alloc`: **1 candidate, 0 true positives, precision 0/1.** The miss is a new FP
sub-class worth adding to `data/cpython_non_bugs.md`:

> **Inequality against a non-NULL singleton is a NULL check.**
> `if (ret != Py_False) return ret;` guards the `Py_DECREF(ret)` below it, because
> `NULL != Py_False`. Same for `!= Py_None` / `!= Py_True` followed by a terminating
> statement. The existing "positive-form and loop-condition checks" entry covers `if (v)`
> and `v != NULL` but not comparison against a *different* known-non-NULL pointer.
> Exemplar: `Modules/_io/bufferedio.c:2446`.

### The recall gap — the deliverable

**The scanner reasons about locals; every bug here is about fields.** `scan_null_checks`
derives NULL-ness from an *assignment site*: "this local was assigned from a fallible call, is
it checked before it is dereferenced?" Its whole denominator — 1,012 assignment sites, 466
fallible sources — measures that question, and `_io` answers it well (0 real hits). The nine
findings above have **no assignment site in the crashing function at all**. `self->raw` is
non-NULL when `CHECK_INITIALIZED` reads it and NULL forty lines later because a *different*
function ran in between. There is no rule in the toolkit that can express this, so the
near-zero is accurate and uninformative at the same time.

### Proposed rule: `stale_field_guard`

Report a read of a nullable receiver field that is dominated by a NULL guard **and** by a
re-entrancy point, with no re-guard in between.

1. **Nullable field set** — receiver struct members that are `Py_CLEAR`ed or assigned `NULL`
   anywhere in the file *outside* `tp_dealloc`/`tp_traverse`/`tp_clear`, or that are compared
   against NULL anywhere in the file. In this slice that yields exactly the right set:
   `raw`, `buffer`, `encoder`, `decoder`, `snapshot`, `buf`.
2. **Guard** — a `CHECK_*` macro or an `if (self->F == NULL) return …` that dominates the read.
3. **Re-entrancy point** — any call between guard and read that can run Python.
4. Emit when (2) and (3) both dominate the read and no re-guard intervenes.

Three sub-requirements this slice proves are *load-bearing*, each of which flips a verdict:

- **Macro bodies must be expanded, and `CHECK_*` must be classified as a re-entrancy point,
  not only as a guard.** `CHECK_CLOSED(self, …)` at `bufferedio.c:1386` reads like a guard and
  *is itself* the re-entrancy point: it reaches `buffered_closed:526` →
  `PyObject_GetAttr(self->raw, closed)`, i.e. a user-defined property. A rule that treats
  `CHECK_*` as opaque gets `:1389` exactly backwards.
- **Argument Clinic converter position is the discriminator, and it lives in a different
  file.** `bytesio` `seek(pos: Py_ssize_t)` is safe and `truncate(size: object)` +
  `PyLong_AsLong` at `:748` is a SIGSEGV, for no reason other than *where* `__index__` runs.
  Deciding this requires reading `Modules/_io/clinic/bytesio.c.h` alongside `bytesio.c`.
  Recommend `scan_null_checks` (and `scan_error_paths`) load the sibling `clinic/*.c.h` and
  classify each impl parameter as *converted pre-guard* or *converted in-body*. This is
  cheap, mechanical, and would flag `bytesio.c:748` and `bufferedio.c:1393` directly.
- **The Python-reaching call set must include the `__index__`/`__buffer__`/property
  converters, not just `PyObject_Call*`.** Four of the eight crashes are driven by
  `PyLong_AsLong` / `PyLong_AsLongLong` / `PyNumber_AsOff_t` / `PyObject_GetAttr`, none of
  which looks like a call into user code.

### Severity model: a NULL-tolerance table for the call APIs

Measured on this slice, and it decides crash-vs-clean-error for otherwise identical code:

| API | NULL `self` | evidence |
|---|---|---|
| `PyObject_CallMethodNoArgs` | **SIGSEGV** | `static inline`, `Include/cpython/abstract.h:62-66` → `PyObject_VectorcallMethod` → `_PyObject_GetMethodStackRef` → `Py_TYPE(obj)`, `Objects/object.c:1764`. Sites `:591`, `:1748` |
| `PyObject_CallMethodOneArg` | **SIGSEGV** | same path, `abstract.h:68-75`. Sites `:1485`, `:1996`, `textio.c:1783` |
| `PyObject_GetAttr` | **SIGSEGV** | `Py_TYPE(v)` on the first line, `Objects/object.c` |
| `PyArg_ParseTuple` | **SIGSEGV** | `Python/getargs.c:109`. Site `textio.c:2873` |
| `_PyFileIO_closed` | **SIGSEGV** | `PyFileIO_CAST(self)->fd`, `fileio.c:94-97` |
| `PyObject_CallMethodObjArgs` | clean `SystemError` | `null_error()`. Site `bufferedio.c:818` |

A rule that ranks `unchecked_alloc` / `stale_field_guard` findings should treat the first five
as FIX-severity and the last as CONSIDER, rather than scoring all "passes a possibly-NULL
pointer to a call" alike.

### Cheap high-yield rule: the in-file accessor as an oracle

When a file defines an `X_access_safe()`-style accessor for a nullable field — `textio.c:739` —
**every direct `self->X` read outside that accessor is a finding by construction**, because the
accessor's existence *is* the maintainer's statement that direct reads are unsafe. In this
slice that rule is silent for `self->buffer` (gh-143008 converted all of them, correctly) but
it generalises: flag the case where a file has an accessor for field X and *sibling fields in
the same struct* (`snapshot`, `encoder`, `decoder`) have none. That single asymmetry check
would have surfaced `textio.c:2873` and `textio.c:1783`.

### Cross-file fix-propagation rule

The strongest signal in this slice is historical, not syntactic: a commit
(`db4b1948bc4`) added a guard helper to `textio.c` for a shape, and the structurally identical
sites in `bufferedio.c` — same directory, same object model, same `detach()` API — were never
touched. Recommend `analyze_history.py` grow a check: for a fix commit that adds a
guard/accessor helper to one file, list sibling files in the same directory containing the
pre-fix shape. That is a directly actionable query and it is what produced five of the eight
crashes here.
