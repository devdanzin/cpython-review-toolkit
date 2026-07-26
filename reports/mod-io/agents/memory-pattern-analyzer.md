# memory-pattern-analyzer — slice `mod-io` (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** the 7 files in `preflight/slice_files.txt`. `winconsoleio.c` not reviewed.
**Build matrix @ `a1d580430c8`:** all 7 slice files are **byte-identical** between `a1d580430c8` and
`4f3be1b5777` (`git diff --quiet` clean for each). Every line number cited below is valid for both
the reviewed ref and the binaries I ran.

## Summary

| | count |
|---|---|
| **FIX** (reproduced crash) | **5** |
| CONSIDER | 4 |
| ACCEPTABLE (bounded, stated with denominator) | 6 classes |
| scanner findings triaged | 0 (`scan_memory_patterns` and `scan_uninit_dealloc` both emitted 0) |
| findings found **by reading**, invisible to every scanner in the slice | **5 of 5** |

All five FIXes are reproduced from **pure Python**, single-threaded, no `_testcapi`, on the default
GIL build. None has prior art (`gh api search/issues`, 12 query variants, all empty except
unrelated hits).

Differential oracle (`Lib/_pyio.py`), `release-gil-nojit`:

| case | C `_io` | `_pyio` twin | verdict |
|---|---|---|---|
| bytesio truncate/`__index__` | **rc=139** SIGSEGV | rc=0, `BufferError` | confirmed C bug |
| bytesiobuf after close | **rc=139** SIGSEGV | rc=0, completes | confirmed C bug |
| `PyBuffer_FillInfo` discarded | rc=0 (rc=134 on debug) | rc=0 | confirmed C bug |
| BufferedReader re-init | **rc=139** SIGSEGV | rc=0, completes | confirmed C bug |
| BufferedWriter re-init | **rc=139** SIGSEGV | rc=0, `OSError` | confirmed C bug |
| `_buffered_init` lock free | **rc=134** SIGABRT | rc=0, completes | confirmed C bug |
| nldecoder self-free | rc=134 on debug | rc=0, `UnsupportedOperation` | confirmed C bug |

---

## Findings

### [FIX] bytesio.c:748 — `truncate()` runs a user `__index__` between `CHECK_EXPORTS` and the resize

**What.** `_io_BytesIO_truncate_impl` validates the export count and *then* converts its argument:

```c
CHECK_CLOSED(self);
CHECK_EXPORTS(self);                 /* :740 — reads exports, sees 0        */
...
new_size = PyLong_AsLong(size);      /* :748 — PyLong_AsLongAndOverflow ->  */
                                     /*        _PyNumber_Index -> __index__ */
...
if (new_size < self->string_size) {
    self->string_size = new_size;
    if (resize_buffer_lock_held(self, new_size) < 0)   /* :761 */
```

`PyLong_AsLong` on a non-`PyLong` dispatches `__index__` (`Objects/longobject.c:1599`,
`_PyNumber_Index(vv)`). An `__index__` that calls `bio.getbuffer()` takes `exports` 0 → 1, and
`resize_buffer_lock_held` then reallocs the buffer a live `memoryview` points into.
`resize_buffer_lock_held` **asserts the invariant it is being handed a violation of**
(`bytesio.c:185 assert(FT_ATOMIC_LOAD_SSIZE_RELAXED(self->exports) == 0)`).

**Guarded twin — in the same file, 500 lines up.** `write_bytes_lock_held` performs its
Python-reaching call *first* and validates *after*:

```c
if (PyObject_GetBuffer(b, &buf, PyBUF_CONTIG_RO) < 0)   /* :241 — runs __buffer__ */
    return -1;
if (check_closed(self) || check_exports(self)) {        /* :245 — re-validated    */
```

`writelines` (`:900`) is the same discipline at the loop level: it re-enters
`write_bytes_lock_held` per item, so each `PyIter_Next` callback is re-checked. `__setstate__`
(`:1008`) is doubly guarded for the same reason. **`truncate` is the only mutation path in the
file that validates before running Python.**

**Measured** (`repro/bytesio_truncate_index_export.py`, `repro/pyio_differential.py truncate_index`):

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT rc=134** — `Assertion 'FT_ATOMIC_LOAD_SSIZE_RELAXED(self->exports) == 0' failed` at `bytesio.c:185` |
| `release-gil-nojit` | **SIGSEGV rc=139**, 10/10 runs |
| `debug-ft-nojit` | SIGABRT rc=134 (same assertion) |
| `release-ft-nojit` | rc=0 — survives, and hands Python a **200,000-byte memoryview over a 1-byte buffer** (`len(v)==200000` while `len(bio.getvalue())==0`); read and write through it both "succeed" |
| `_pyio` | rc=0, `BufferError: Existing exports of data` |

ASan (`release-gil-nojit-asan`):

```
ERROR: AddressSanitizer: heap-use-after-free ... READ of size 200000
    #1 PyBuffer_ToContiguous  Objects/memoryobject.c:1063
freed by thread T0 here:
    #1 _PyBytes_Resize                     Objects/bytesobject.c:3389
    #2 resize_unshared_buffer_lock_held    Modules/_io/bytesio.c:110
    #3 resize_buffer_lock_held             Modules/_io/bytesio.c:218
    #4 _io_BytesIO_truncate_impl           Modules/_io/bytesio.c:761
```

**Control:** `bio.truncate(0)` with a plain live export raises `BufferError` on every build. The
only variable is the `__index__` callback.

**Fix.** Move the conversion above the check, or re-run `check_exports(self)` after `:757` — the
`write_bytes_lock_held` ordering. (Note the C-level `-1` sentinel is also unambiguous here because
`new_size < 0` is rejected at `:752`, so re-checking costs nothing.)

---

### [FIX] bytesio.c:1282 — `bytesiobuf_getbuffer` has no closed check; NULL deref after `close()`

**What.** `_io.BytesIO.getbuffer()` checks `CHECK_CLOSED` (`:393`) before building the
intermediate `_io._BytesIOBuffer`, but that intermediate is reachable from Python as
`memoryview.obj`, and re-exporting it enters `bf_getbuffer` directly:

```c
static int
bytesiobuf_getbuffer_lock_held(PyObject *op, Py_buffer *view, int flags)
{
    bytesio *b = bytesio_CAST(obj->source);
    ...
    if (FT_ATOMIC_LOAD_SSIZE_RELAXED(b->exports) == 0 && SHARED_BUF(b)) {   /* :1282 */
```

`SHARED_BUF(b)` is `!_PyObject_IsUniquelyReferenced(b->buf)` (`:78/:80`), which dereferences
`b->buf->ob_refcnt`. `close()` sets `b->buf = NULL` (`:929`). There is no `check_closed` anywhere
in the `bytesiobuf` half of the file; `PyBytes_AS_STRING(b->buf)` at `:1289` would fault too.

**Guarded twin.** Every other entry point in `bytesio.c` opens with `check_closed` / `CHECK_CLOSED`
— `:325 :340 :355 :370 :393 :415 :447 :462 :523 :572 :610 :638 :701 :739 :776 :816 :894`, and
`write_bytes_lock_held:245` re-checks it after running Python. `bytesiobuf_getbuffer` is the
single omission.

**Measured** (`repro/bytesio_buf_after_close.py`), three lines of setup:

```python
bio = io.BytesIO(b"hello world" * 100)
m = bio.getbuffer(); inner = m.obj      # inner is the _io._BytesIOBuffer
m.release(); bio.close()                # exports -> 0, then buf -> NULL
memoryview(inner)                       # -> bf_getbuffer on a closed BytesIO
```

| build | result |
|---|---|
| `debug-gil-nojit` / `release-gil-nojit` / `release-ft-nojit` | **SIGSEGV rc=139**, 10/10 |
| `_pyio` | rc=0, completes |

ASan: `SEGV on unknown address 0x000000000000 ... READ`, frame
`#0 _PyObject_IsUniquelyReferenced Include/internal/pycore_object.h:187`
`#1 bytesiobuf_getbuffer_lock_held Modules/_io/bytesio.c:1282`.

**Control:** the identical sequence without `bio.close()` re-exports cleanly (`len == 1100`) on
every build.

**Fix.** `if (b->buf == NULL) { PyErr_SetString(PyExc_ValueError, "I/O operation on closed file."); return -1; }` at the top of
`bytesiobuf_getbuffer_lock_held`.

---

### [FIX] bytesio.c:1288 — `(void)PyBuffer_FillInfo(...)`: failure discarded, export count leaked

**What.** The comment is factually wrong:

```c
/* cannot fail if view != NULL and readonly == 0 */
(void)PyBuffer_FillInfo(view, op, PyBytes_AS_STRING(b->buf), b->string_size, 0, flags);
FT_ATOMIC_ADD_SSIZE(b->exports, 1);
return 0;
```

`PyBuffer_FillInfo` (`Objects/abstract.c:774`) has a **third** failure mode, independent of
`view` and `readonly`:

```c
if (flags != PyBUF_SIMPLE) {
    if (flags == PyBUF_READ || flags == PyBUF_WRITE) {
        PyErr_BadInternalCall();
        return -1;
    }
```

`PyBUF_READ` is `0x100`, `PyBUF_WRITE` is `0x200` (`Include/pybuffer.h:137-138`). PEP 688 exposes
`bf_getbuffer` to Python as `__buffer__(flags)`, and `wrap_buffer` (`Objects/typeobject.c:10345`)
forwards **any** int in `[INT_MIN, INT_MAX]` unmodified. So `inner.__buffer__(0x100)` returns
from `bytesiobuf_getbuffer_lock_held` with: the `Py_buffer` **entirely unwritten**, `exports`
incremented, an exception set, and a return value of `0` (success).

**Guarded twin — the same call, in CPython's other resizable-buffer exporter.**
`Objects/bytearrayobject.c:66 bytearray_getbuffer_lock_held`:

```c
if (PyBuffer_FillInfo(view, (PyObject*)obj, ptr, Py_SIZE(obj), 0, flags) < 0) {
    return -1;
}
obj->ob_exports++;
```

`bytesio.c:1288` is the only `PyBuffer_FillInfo` call in `Objects/` or `Modules/_io/` that casts
the result to `(void)` while also mutating state on the strength of it.

**Measured** (`repro/bytesio_fillinfo_unchecked.py`):

| build | result |
|---|---|
| `debug-gil-nojit`, `debug-ft-nojit` | **SIGABRT rc=134**, 5/5 — `Fatal Python error: _Py_CheckFunctionResult: a function returned a result with an exception set` / `SystemError: Objects/abstract.c:785: bad argument to internal function` |
| `release-gil-nojit`, `release-ft-nojit` | rc=0, but the **export count is permanently leaked**: every later `truncate()` and `close()` raises `BufferError`, and teardown prints `SystemError: deallocated BytesIO object has exported buffers` |

The leak is structural, not a timing accident: the increment happened but `view->obj` was never
set, so `PyBuffer_Release` short-circuits on `obj == NULL` and `bf_releasebuffer` is never called.
Same result for `flags=0x200`.

**Fix.** Check the return value and increment only on success, exactly as bytearray does.

---

### [FIX] bufferedio.c:847 and :854 — `_buffered_init` frees the buffer and the lock from outside every `ENTER_BUFFERED` region

**What.** `_buffered_init` is the shared body of all three `__init__`s:

```c
if (self->buffer)
    PyMem_Free(self->buffer);              /* :847 */
self->buffer = PyMem_Malloc(self->buffer_size);
...
if (self->lock)
    PyThread_free_lock(self->lock);        /* :854 — frees a lock the caller HOLDS */
self->lock = PyThread_allocate_lock();
```

Meanwhile `_bufferedreader_raw_read` hands the user a writable `memoryview` straight over that
block and calls into arbitrary Python:

```c
PyBuffer_FillInfo(&buf, NULL, start, len, 0, PyBUF_CONTIG);   /* start = self->buffer + k */
memobj = PyMemoryView_FromBuffer(&buf);
res = PyObject_CallMethodOneArg(self->raw, &_Py_ID(readinto), memobj);   /* :1640 */
```

`_bufferedwriter_raw_write:1995` is the symmetric read-only case (`raw.write(memobj)`).

The file's re-entrancy defence is `ENTER_BUFFERED`: a second entry from the same thread hits
`_enter_buffered_busy`'s `"reentrant call inside %R"` branch (`:300`). There are **12**
`ENTER_BUFFERED` sites — `:561 :583 :943 :968 :1008 :1017 :1059 :1115 :1236 :1429 :1476 :2097` —
and `__init__` is **not one of them**. The three `_buffered_init` callers are
`_io_BufferedReader___init___impl:1609`, `_io_BufferedWriter___init___impl:1962`,
`_io_BufferedRandom___init___impl:2509`.

**Guarded twin — the identical free, done correctly, 250 lines up.**
`_io__Buffered_close_impl` performs the very same `PyMem_Free(self->buffer)` at `:594` but does it
*inside* an `ENTER_BUFFERED` region (`:583`→`:607`), so a re-entrant `close()` is rejected instead
of corrupting the heap.

**Measured.** Four distinct victims of the one root cause
(`repro/bufferedio_reinit_buffer_uaf.py`, `repro/bufferedio_reinit_siblings.py`):

| victim | debug-gil | release-gil | evidence |
|---|---|---|---|
| `BufferedReader` — `raw.readinto` writes the view | **139** | **139**, 5/5 | ASan `heap-use-after-free WRITE of size 1048576`, `#10 _bufferedreader_raw_read bufferedio.c:1640`; freed at `#1 _buffered_init bufferedio.c:847` ← `#2 _io_BufferedReader___init___impl:1609` |
| `BufferedWriter` — `raw.write` reads the view | **139** | **139**, 5/5 | ASan `heap-use-after-free READ of size 1048575`; freed at `_buffered_init:847` ← `_io_BufferedWriter___init___impl:1962` |
| the **lock**, in isolation (view never touched) | **134** | **134**, 5/5 | `Fatal Python error: PyMutex_Unlock: unlocking mutex that is not locked` — `LEAVE_BUFFERED` releases the *new* lock while the *old* one was freed while held |
| `BufferedRandom` | **139** | **139** | same shape via `_io_BufferedRandom___init___impl:2509` |

`_pyio`: rc=0 for the reader/lock cases, clean `OSError` for the writer.

**Two controls, both clean, which is what localises this to `__init__`:**
- `mode=control` — identical raw object, identical 1 MiB view, no re-init → `rc=0`, `read(4) == b'XXXX'`.
- `mode=reentry` — the same callback calls `br.read(1)` instead of `br.__init__` → `RuntimeError:
  reentrant call inside <_io.BufferedReader>` on both builds. **`ENTER_BUFFERED` demonstrably
  works; `__init__` simply is not behind it.**

**Fix.** Bracket the reinitialising part of `_buffered_init` with `ENTER_BUFFERED`/`LEAVE_BUFFERED`
(the lock must be reallocated outside it, or reused when `buffer_size` is unchanged), or add an
explicit "already initialised and currently in use" rejection to the three `__init__` impls.
The lock half additionally needs `self->owner` cleared, since a freed-and-replaced lock leaves the
owner field naming a thread that holds nothing.

---

### [FIX] textio.c:350 — `_PyIncrementalNewlineDecoder_decode` uses its own `self` after the nested decoder call freed it

**What.** The function receives `myself` **borrowed** and dereferences it after calling into the
inner, user-supplied decoder:

```c
_PyIncrementalNewlineDecoder_decode(PyObject *myself, PyObject *input, int final)
{
    nldecoder_object *self = nldecoder_object_CAST(myself);
    ...
    if (self->decoder != Py_None) {
        output = PyObject_CallMethodObjArgs(self->decoder,        /* :339  USER PYTHON */
            &_Py_ID(decode), input, final ? Py_True : Py_False, NULL);
    }
    ...
    if (self->pendingcr && (final || output_len > 0)) {           /* :350  UAF READ    */
        ...
        self->pendingcr = 0;                                      /* :365  UAF WRITE   */
    }
    ...
    self->seennl |= seennl;                                       /* :511  UAF WRITE   */
```

Both textio call sites pass `self->decoder` borrowed — `:1001` (through
`_textiowrapper_decode(state, self->decoder, ...)` at `:2012`) and `:2093` — and
`TextIOWrapper.reconfigure()` drops it: `_textiowrapper_set_decoder:976 Py_CLEAR(self->decoder)`,
reached from `textiowrapper_change_encoding:1402` ← `_io_TextIOWrapper_reconfigure_impl:1501`.
`incrementalnewlinedecoder_dealloc:296` then `free()`s the object the C frame is standing in.

`reconfigure()` *does* have a guard — `if (self->decoded_chars != NULL)` at `:1465` — and it is
defeated the same way the seeded lead in AGENT_BRIEF §3 is: called from inside
`textiowrapper_read_chunk`, it runs *before* `textiowrapper_set_decoded_chars` at `:2018`, so on
the first read `decoded_chars` is still NULL and the guard passes.

**Guarded twins, all three in-tree:**
- **`textio.c:1003` — the `else` arm of the very same `if`.**
  `PyObject_CallMethodObjArgs(decoder, &_Py_ID(decode), ...)` never dereferences `decoder` after
  the call, so the *generic* decoder path is immune. Only the C fast path at `:1001` is exposed.
  One `if`, one safe branch, one unsafe branch.
- `textio.c:533` `_io_IncrementalNewlineDecoder_decode_impl` reaches the same function with `self`
  pinned by the bound-method machinery.
- **`stringio.c:199` is the same borrowed shape and is safe *by construction*** — StringIO always
  builds its `IncrementalNewlineDecoder` with an inner decoder of `Py_None`
  (`stringio.c:754-757`), so `textio.c:338` takes the `output = Py_NewRef(input)` branch and no
  user Python ever runs. Worth stating: this is why the sibling site is *not* a second bug.

**Measured** (`repro/textio_nldecoder_self_uaf.py`, `repro/pyio_differential.py nldecoder_self`).
A registered codec (`codecs.register`) whose `decode()` calls `f.reconfigure(newline="\r")`:

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGABRT rc=134, 5/5** — pymalloc's own guard: `Debug memory block at address p=...: API 'o' / 47 bytes originally requested / The 8 pad bytes at tail are not all FORBIDDENBYTE (0xfd): at tail+1: 0xfc *** OUCH`. That is the `self->…` **write** landing in the recycled block. |
| `release-gil-nojit-asan` | `heap-use-after-free READ of size 1` at `_PyIncrementalNewlineDecoder_decode textio.c:350`, freed at `incrementalnewlinedecoder_dealloc textio.c:296` ← `_textiowrapper_set_decoder textio.c:976` ← `reconfigure textio.c:1501`, and the freeing frame is nested **inside** `#17 _PyIncrementalNewlineDecoder_decode textio.c:339` |
| `release-gil-nojit` | rc=0 but returns corrupted data — `f.read(5)` yields `'\rabcd'`, a spurious `\r` from the stale `pendingcr` |
| `_pyio` | rc=0, `UnsupportedOperation: It is not possible to set the encoding or newline of stream after the first read` — **the twin refuses the operation the C code performs** |

**Control:** the identical codec without the `reconfigure()` call is clean on all builds
(`rc=0`, `fired=0`).

**Fix.** `Py_INCREF(myself)` for the duration of `_PyIncrementalNewlineDecoder_decode` (or INCREF
the decoder at the two call sites, matching what the `:1003` branch gets for free from the
vectorcall machinery). The `reconfigure()` guard should additionally be re-evaluated after
`_PyFile_Flush:1491`, but that is a hardening, not the fix — the borrowed reference is.

---

### [CONSIDER] fileio.c:828 — `PyBytesWriter_Resize` failure leaks the writer

```c
if (PyBytesWriter_GetSize(writer) < (Py_ssize_t)bufsize) {
    if (PyBytesWriter_Resize(writer, bufsize) < 0)
        return NULL;                      /* :829 — no PyBytesWriter_Discard(writer) */
}
```

`PyBytesWriter_Resize` (`Objects/bytesobject.c:3828`) returns `-1` without discarding. Every other
error exit in `_io_FileIO_readall_impl` does discard — `:820`, `:843`, `:846`. Leaks the writer
struct and its backing bytes object. Only reachable under allocation failure, so no crash; not
run through OOM injection. **CONSIDER**, one-line fix.

### [CONSIDER] fileio.c:313 — Windows-only `stringobj` leak

```c
if (!PyUnicode_FSDecoder(nameobj, &stringobj)) return -1;   /* :308 */
widename = PyUnicode_AsWideCharString(stringobj, NULL);     /* :311 */
if (widename == NULL)
    return -1;                                              /* :313 — bypasses done: Py_CLEAR(stringobj) at :545 */
```

`MS_WINDOWS` only, unbuildable here, unreproducible. Reported as a code fact.

### [CONSIDER] textio.c:941 — `self->writenl` is a raw alias into `self->readnl`'s UCS1 storage

```c
self->writenl = (const char *)PyUnicode_1BYTE_DATA(self->readnl);   /* :941 */
```

`set_newline` orders this correctly (`Py_XDECREF(old)` *after* re-pointing, `:953`). Two sites
`Py_CLEAR(self->readnl)` **without** resetting `writenl`: `textiowrapper_clear:1520` and
`_io_TextIOWrapper___init___impl:1217`. The `__init__` window spans
`_PyCodec_LookupTextEncoding:1257`, which runs registered Python codec search functions. Both are
gated by `self->ok = 0` (`:1176`, `:1515`), and the only reader — `_io_TextIOWrapper_write_impl`
`:1749/:1753/:1755` — opens with `CHECK_ATTACHED`. So it is **latent, not live**: the safety is
non-local and one `self->ok` regression away from a UAF. I did not find a live trigger.

### [CONSIDER] bytesio.c:748, stringio.c:472 — `PyLong_AsLong` where `PyLong_AsSsize_t` is meant

Both `truncate` impls narrow through C `long`. On LP64 this is harmless; on Windows LLP64 it caps
`truncate()` at 2**31−1 while `_pyio` accepts any `Py_ssize_t`. Cannot be measured on this
platform. Fixing this also fixes the FIX-1 finding's conversion (`PyLong_AsSsize_t` still runs
`__index__`, so the re-check is still required).

---

## (b) The `RELAXED` ordering on `exports` — measured, and it is *not* the defect

The brief asks whether `FT_ATOMIC_LOAD_SSIZE_RELAXED` on a counter that gates a resize is
sufficient. I split the question and measured both halves
(`repro/bytesio_exports_ft_stress.py`, `PYTHON_GIL=0`, 40 iterations × 200 rounds × 2–3 threads):

| scenario | `debug-ft-nojit` | `release-ft-nojit` |
|---|---|---|
| `concurrent` — thread A loops `write`/`truncate`/`seek`, threads B,C loop `getbuffer` + touch + `release`. No Python callback anywhere in the window. | **clean, 0 unexpected exceptions** | **clean, 0** |
| `suspend` — same, but the resizing thread uses `truncate(Evil())` whose `__index__` runs Python inside the section | **SIGABRT rc=134**, `bytesio.c:185` assertion | rc=0 (survives, same silent oversized-view outcome as the single-threaded case) |

**Conclusion, stated as measured rather than reasoned.** The relaxed load is adequate:

- Every **increment** (`bytesio.c:1291`) runs inside `Py_BEGIN_CRITICAL_SECTION(source)` (`:1305`),
  and every reader (`check_exports:59`, `getvalue:416`, `peek:478`, `__init__:1130`,
  `clear:1196`) runs inside a critical section on the same object. The section's
  acquire/release supplies the ordering the relaxed load does not; a plain relaxed read under the
  same lock cannot observe a torn or stale increment.
- The one **unlocked** access is the decrement in `bytesiobuf_releasebuffer:1316`. Observing it
  late is conservative (a stale `>0` blocks a resize), and `PyBuffer_Release` makes no use of
  `view->buf` after `bf_releasebuffer` returns, so there is nothing for a late-visible decrement
  to expose.

**The check-then-resize is therefore atomic against a concurrent `getbuffer` — and is *not*
atomic against a re-entrant one**, because `PyLong_AsLong` runs arbitrary Python inside the
section (and, under FT, at a point where the section can be suspended). That is FIX-1, and the
`concurrent` row above is the control proving the counter's memory model is not what is broken.

*Hypothesis I did not falsify:* whether `release-ft-nojit` survives FIX-1 because mimalloc keeps
the shrunk page mapped, or for another reason. The observable — a 200,000-byte view over a 1-byte
buffer, readable and writable — is recorded; the allocator explanation is not established.

---

## Classes bounded (with denominators)

Negatives here are real results; each is stated with the count that makes it meaningful.

### Structural zeros — both confirmed correct, not silence

**`scan_uninit_dealloc`: 0 findings, `allocation_sites: 0`.** The brief flags this as structural
and asks for it to be addressed explicitly rather than reported as a bare zero. **Confirmed, and
correctly so, on two independent grounds:**

1. `grep -n "PyObject_New\|PyObject_GC_New\|_PyObject_New\|_PyObject_GC_New"` over all 7 files:
   **0 hits.** The slice contains no non-zeroing allocator call at all, so the denominator is
   genuinely zero rather than under-counted.
2. All **4** object-allocation sites in the slice go through `type->tp_alloc(type, 0)` —
   `fileio.c:202`, `stringio.c:656`, `bytesio.c:395`, `bytesio.c:1098`. Per the taxonomy amendment
   of 2026-07-25, `tp_alloc` is only non-zeroing in a file that registers its own `allocfunc`;
   `grep -n "Py_tp_alloc"` over all 7 files is **0 hits**, so all four resolve to
   `PyType_GenericAlloc` → `_PyType_AllocNoTrack`, which memsets. `bytesio_new:1102` states the
   assumption in a comment (*"tp_alloc initializes all the fields to zero"*) and it holds.

So: the classic uninitialised-dealloc shape is **absent by construction** in `_io`, and the zero
is trustworthy.

**`scan_memory_patterns`: 0 findings, 4 varobject sites / 0 non-constant `nitems`.** Confirmed:
the same 4 `tp_alloc(type, 0)` sites, all with a **literal** `0`. `grep` for
`PyObject_GC_NewVar` / `PyObject_NewVar` / `_PyObject_GC_NewVar` / `PyObject_GC_Resize` over the
slice: **0 hits**. The `varobject_nitems_unguarded` rule had nothing to look at; its zero is
correct and carries no information about the slice.

### Allocator families — clean, and the denominator is small

Raw-allocator call sites in the slice: **13 total** (`fileio.c` 6× `PyMem_Free` + 1× `PyMem_New`
+ 1× `PyMem_Free(widename)`; `stringio.c` 1× `PyMem_Realloc`, 2× `PyMem_Free`, 1× `PyMem_Malloc`;
`bufferedio.c` 3× `PyMem_Free`, 1× `PyMem_Malloc`; `textio.c` 1× `PyMem_Malloc`, 1× `PyMem_Free`).

- **Family mismatches: 0.** `grep -E '(^|[^_])(malloc|calloc|realloc|free)\s*\('` and
  `grep -E 'PyObject_(Malloc|Calloc|Realloc|Free)'` over all 7 files: **0 hits**. Every pair is
  `PyMem_*` ↔ `PyMem_Free`, including `PyUnicode_AsWideCharString` (`fileio.c:311`, `PyMem_New`
  internally) ↔ `PyMem_Free` (`:543`).
- **Double-frees: 0.** Every `PyMem_Free(self->stat_atopen)` is followed by a NULL store or an
  immediate reassignment — `:138→139`, `:470→471`, `:493→494`, `:538→539`, `:577→578`,
  `:1138→1139`. The one sequence that frees twice (`internal_close` at `:535` then `:538`) is safe
  *because* of that NULLing.
- **Dangling-after-failed-realloc: 0.** `_buffered_init:848` and `stringio.c:116` both assign the
  result before testing it, so a failed allocation leaves NULL, not a stale pointer.

### Allocation-size arithmetic — 5 expressions, all bounded or guarded

| site | expression | verdict |
|---|---|---|
| `stringio.c:116` | `PyMem_Realloc(self->buf, alloc * sizeof(Py_UCS4))` | **guarded** — explicit `if (alloc > SIZE_MAX / sizeof(Py_UCS4)) goto overflow;` at `:114`. This is the in-slice exemplar of the correct pattern. |
| `textio.c:475` | `PyMem_Malloc(kind * len)` | **bounded-by-an-existing-allocation** — `kind ∈ {1,2,4}` and `len = PyUnicode_GET_LENGTH(output)` of a live `str` that already occupies `kind*len` bytes. `len == 0` returns early at `:397`, so the size is ≥1. The fill loop at `:481-504` writes ≤ `len` slots (every branch consumes ≥1 input code point per output; the `\r\n` branch consumes 2 for 1), and the NUL terminator at index `len` drives the `if (in > len) break` exit. |
| `textio.c:356` | `PyUnicode_New(output_len + 1, ...)` | bounded-by-an-existing-allocation |
| `iobase.c:654` | `PyByteArray_Resize(buffer, old_size + PyBytes_GET_SIZE(b))` | bounded-by-an-existing-allocation (both operands are live objects) |
| `fileio.c:811/:828` | `PyBytesWriter_Create(bufsize)` / `_Resize` | **guarded** — `st_size < _PY_READ_MAX` at `:765`, clamp at `:777-779`, range check at `:805`, and the post-hoc `bufsize > PY_SSIZE_T_MAX \|\| bufsize <= 0` test at `:819` |
| `fileio.c:471` | `PyMem_New(struct _Py_stat_struct, 1)` | guarded inside the macro |

`bufferedio.c:848 PyMem_Malloc(self->buffer_size)` has no multiply; `buffer_size > 0` is checked
at `:841`.

### Buffer-protocol bookkeeping

- `bf_getbuffer`/`bf_releasebuffer` slot pairs in the slice: **1** (`bytesio.c:1345-1346`). It is
  the subject of two of the five FIXes.
- `PyObject_GetBuffer` sites: **2** (`bytesio.c:241`, `textio.c:2001`), both correctly released —
  `textio.c:2014` dominates every exit after the acquire, and the `goto fail` at `:2006` is on the
  branch where the buffer was never filled. `bytesio.c:294` releases at the shared `done:` label.
- Clinic-managed `Py_buffer` parameters (`fileio.c:683/:927`, `bufferedio.c` 6×, `bytesio.c:696`):
  acquired and released in the generated wrappers under an unconditional `exit:` label. The two
  "extra" `PyBuffer_Release` greps in `fileio.c` (`:697`, `:941`) are **comments**, not calls — no
  double-release.
- `PyBuffer_FillInfo` sites: **3** (`bytesio.c:1288`, `bufferedio.c:1629`, `:1984`). The two in
  `bufferedio.c` test the return value; `bytesio.c:1288` does not — FIX-3.
- **A failed `getbuffer` leaving the count incremented:** the `unshare_buffer_lock_held < 0` path
  at `bytesio.c:1283-1284` correctly returns before the increment. The `PyBuffer_FillInfo` path
  does not — that is FIX-3, and it is the only instance.

### Checked and clean

`_io_BytesIO_readlines_impl:660` caches a raw `output` pointer into `self->buf` across
`PyBytes_FromStringAndSize` and `PyList_Append`; neither runs Python nor allocates through the GC
allocator, and `PyLong_AsSsize_t:641` is gated by `PyLong_Check:640`, so no `__index__` runs.
`_stringio_readline:373-378` writes a sentinel at `self->buf[pos+limit]`, in bounds because
`resize_buffer` reserves the documented extra slot (`stringio.c:90-91`).
`textio.c:2339-2348` and `iobase.c:610-629` cache pointers with no intervening Python.

---

## Toolkit assessment

`scan_memory_patterns` and `scan_uninit_dealloc` each fired **0 times** on this slice, and both
zeros are correct (above). **All five FIXes came from reading.** That is the honest headline: on a
buffer-protocol-dense module, the current memory ruleset has ~0 recall, because every one of its
four rules keys on an *allocation site*, and none of these bugs is at an allocation site — they
are at the **lifetime boundary between a heap block and the Python code that can reach it.**

### Recall gap 1 — export-counter discipline is not modelled by any rule (proposed: `export_count_gate_stale`)

The brief asked me to propose a rule if the slice justified one. It does; three of the five FIXes
are instances.

**Shape.** A counter/flag field that gates a destructive operation (resize, free, replace) is read,
then a call that can execute arbitrary Python intervenes, then the destructive operation runs
without re-reading it.

**Mechanisable form.** Within one function body:
1. a read of a field whose name matches `exports|ob_exports|refcnt|_count|locked|in_use`, or a
   call to a file-local predicate whose body reads such a field (`check_exports`), **used in a
   condition that can `return`/`goto`**; then
2. any member of the existing `PYTHON_REACHING_APIS` set — plus, critically,
   **`PyLong_As*`/`PyNumber_As*`/`PyFloat_As*` on an operand not dominated by a matching
   `Py<T>_Check`**, which is what makes `bytesio.c:748` visible at all; then
3. a call to a file-local function whose body contains a reallocation (`_PyBytes_Resize`,
   `PyMem_Realloc`, `PyMem_Free`, `Py_CLEAR` of the gated field's sibling) — with no second
   evaluation of the step-1 condition in between.

**Calibration data from this slice.** Candidates: `truncate:740→761` (TP),
`__setstate__:1008→1016` (FP — the inner `write_bytes_lock_held` re-checks),
`close:928→929` (FP — nothing runs between), `__init__:1130→1137` (FP). So 1 TP / 4 candidates
before the "re-check in the callee" suppressor, and **1/1 after** it, since only `truncate` has no
re-validation on any path. The suppressor is cheap and file-local: if the reallocating callee
itself re-reads the gate, drop the candidate.

**Guarded-twin emitter.** For every hit, the rule can name the sibling in the same file that runs
its Python-reaching call *before* the gate — `write_bytes_lock_held:241-245` here. That is what
makes the finding actionable, and it is exactly the informed-mode contract.

### Recall gap 2 — `ENTER_BUFFERED` is invisible, and §4a understated the consequence

AGENT_BRIEF §4a already records that `scan_lock_discipline` has no vocabulary for
`ENTER_BUFFERED`/`LEAVE_BUFFERED` and asks for a leak audit of the 12 spans. **The 12 spans are
balanced; I found no leak.** The real defect is the complement, which §4a does not ask for:
**a function that mutates lock-protected state and is *not* in the list at all.** `_buffered_init`
frees both the protected buffer and the lock itself, and it appears in none of the 12 spans.

Proposed rule, and it generalises past `_io`: given a file-local acquire/release macro pair
(detectable as a `#define X(self) ... acquire ...` / `#define Y(self) ... release ...` on the same
struct field), compute the set of fields written inside the protected spans; then flag any
**non-static, externally-reachable** function that writes one of those fields *outside* every
span. On `bufferedio.c` the protected-write set is `{buffer, pos, read_end, write_pos, write_end,
raw_pos, abs_pos}`; `_buffered_init` writes `buffer` (and frees `lock`) outside — a single hit,
which is the bug. This subsumes the classic leak check and finds the harder half.

### Recall gap 3 — the crown-jewel rule stops at `PyObject*` and misses raw blocks and `self`

Two of the five FIXes are borrowed-lifetime bugs the refcount scanner cannot see:

- **`bufferedio.c:1640`** — the endangered thing is a `PyMem_Malloc` block wrapped in an
  ephemeral `Py_buffer`, not a `PyObject*`. The FP taxonomy already carves this out in prose
  ("*A raw `PyMem_Malloc` buffer hanging off a live object is NOT protected by its owner*", with
  `_struct.c s_codes` / `_zoneinfo.c` / `_elementtree.c` as precedent) and notes none of the three
  is scanner-reachable. **`bufferedio.c` adds a fourth instance and a mechanisable signature the
  other three lack:** `PyBuffer_FillInfo(&buf, NULL, <ptr derived from self->F>, ...)` →
  `PyMemoryView_FromBuffer(&buf)` → `PyObject_Call*`. That three-call sequence is a precise,
  low-false-positive pattern — there are exactly 2 instances in the slice (`:1629`, `:1984`) and
  both are true positives.
- **`textio.c:350`** — the endangered object is the function's **own `self`**, freed by a callee.
  The scanner's borrowed-ref model tracks locals loaded *from* slots; here the parameter itself is
  the borrow. Proposed narrow rule: a non-`static` function taking `PyObject *` as its first
  parameter, which is called anywhere in the tree with a bare `x->field` argument (no
  `Py_NewRef`), and which dereferences that parameter after a Python-reaching call. In this slice
  that yields exactly one candidate and it is the bug; the `stringio.c:199` sibling is separated
  by the `Py_None` inner-decoder invariant, which needs the human step.

### What worked

- **The guarded twin was decisive in all five cases**, and in two of them it is textually adjacent:
  `bytesio.c` FIX-1 vs `write_bytes_lock_held` (same file, same counter, opposite ordering), and
  `textio.c` FIX-5 vs the `else` branch of the very same `if` statement. Both would be one-line
  emitter outputs for the rules above.
- **The `_pyio` oracle earned its billing.** 6 of 7 cases: hard C crash, clean twin. For FIX-5 the
  twin did better than "survives" — it *names the operation as unsupported*
  (`UnsupportedOperation: It is not possible to set the encoding or newline of stream after the
  first read`), which is precisely the guard the C side has at `textio.c:1465` and evaluates too
  early. The oracle located the intended contract, not just the absence of a crash.
- **Running the negative control mattered more than the crash.** `mode=reentry` proving
  `ENTER_BUFFERED` *does* reject a re-entrant `read()` is what localises FIX-4 to `__init__`
  rather than to re-entrancy in general; the `concurrent` FT scenario coming back clean is what
  kept me from misreporting the RELAXED load as the defect in (b).

### Corrections to carry forward

- **AGENT_BRIEF §4d's `scan_uninit_dealloc` row is right, and now has a second, stronger reason.**
  The row justifies the zero by "`_io` allocates via `type->tp_alloc(type, 0)`, which zeroes". That
  is true here but is the reasoning the 2026-07-25 taxonomy amendment warns against
  (`tp_alloc` is not unconditionally zeroing). The durable justification is that the slice has
  **zero non-zeroing allocator calls** and **zero `Py_tp_alloc` registrations** — both mechanical.
- **A `Py_BEGIN_CRITICAL_SECTION` / `@critical_section` annotation is not evidence that a
  check-then-act is atomic** when arbitrary Python runs inside it. Every one of `bytesio.c`'s
  mutation paths is `@critical_section`, and FIX-1 lives inside one. The FP taxonomy's entry
  "*The lock is in the Argument Clinic wrapper*" is correct as an FP suppressor for *field
  synchronisation* and must not be extended to *atomicity of a multi-step sequence*.
- The FP taxonomy's "*sizeof \*x inside the allocation's own argument list*" and
  "*bounded-by-an-existing-allocation*" entries both held perfectly here: they accounted for 4 of
  the 5 size expressions and produced no misclassification.

---

## Artefacts

Reproducers, all standalone `.py` files, all with a passing negative control:

| file | finding |
|---|---|
| `repro/bytesio_truncate_index_export.py` | FIX-1 (modes `probe`, `noindex`) |
| `repro/bytesio_buf_after_close.py` | FIX-2 (modes `probe`, `noclose`) |
| `repro/bytesio_fillinfo_unchecked.py` | FIX-3 (arg: flags int) |
| `repro/bufferedio_reinit_buffer_uaf.py` | FIX-4 reader (modes `probe`, `control`, `reentry`) |
| `repro/bufferedio_reinit_siblings.py` | FIX-4 writer / lock / random |
| `repro/textio_nldecoder_self_uaf.py` | FIX-5 (modes `probe`, `control`) |
| `repro/bytesio_exports_ft_stress.py` | question (b) (modes `concurrent`, `suspend`) |
| `repro/pyio_differential.py` | all seven cases × `{io, _pyio}` |

Suggested findings-repo records: five `memory-pattern` / `uaf` entries, status `reproduced`, sites
`Modules/_io/bytesio.c:748`, `:1282`, `:1288`, `Modules/_io/bufferedio.c:847`,
`Modules/_io/textio.c:350`.
