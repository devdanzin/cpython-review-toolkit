# refcount-auditor — slice `mod-io` (informed)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** exactly the 7 files in `preflight/slice_files.txt`. `winconsoleio.c` not touched.
**Build matrix:** `a1d580430c8`. **All 7 slice files + `Lib/_pyio.py` are byte-identical between
`a1d580430c8` and `4f3be1b5777`** (`git diff --quiet` clean for each), so every line number cited
from a run is also correct at the review ref.
**Pre-run scanner:** `scan_refcounts.sample.json` — **0 findings**, denominators
`functions_analyzed 292`, `borrowed_slot_load_sites 26`, `borrowed_accessor_load_sites 6`,
`borrowed_field_accessors 1` (`buffer_access_safe`).

**Headline:** the scanner's zero is *honest for its rules* — I re-derived all 26 + 6 sites and
every one of them is genuinely clean. But **four heap-use-after-free / NULL-dispatch bugs are
present in this slice**, and **not one of them has the shape any current rule can express.**
Three are reproduced as hard crashes from pure Python; the fourth is reproduced under ASan on
both a debug and a release ASan build.

| # | verdict | site | measured |
|---|---|---|---|
| 1 | **FIX** | `textio.c:350/365/380/389/435/511` + `:575` — `IncrementalNewlineDecoder` derefs and **writes** its own freed `self` | ASan HUAF ×3 sites, debug **and** release ASan |
| 2 | **FIX** | `bufferedio.c:2018` + `:2232` — nested `Buffered` used as a borrowed receiver | **SIGSEGV 5/5 debug-gil, 5/5 release-gil**, also FT; ASan frame; `_pyio` survives |
| 3 | **FIX** | `textio.c:2937/2938/2982` — `tell()` derefs `next_input` borrowed from `self->snapshot` | **SIGSEGV 5/5 debug-gil**; ASan HUAF on debug **and** release ASan; `_pyio` survives |
| 4 | **FIX** | `bufferedio.c:591` (close), `:1389` (seek) — `self->raw` re-read after user Python, no re-check | **SIGABRT 5/5 debug-gil, SIGSEGV 5/5 release-gil** at each site; `_pyio` survives |
| 5 | **CONSIDER** | `textio.c:977`, `:1034` — raw store into a slot across a user codec factory | 1 object leaked, measured, both builds |
| 6 | **POLICY** | `textio.c:941` — `self->writenl` is a raw `char*` into `self->readnl`'s payload | static; fragile, not currently reachable |

Reproducers: `reports/mod-io/repro/{nldecoder_self_uaf,buffered_borrowed_raw_uaf,tell_snapshot_uaf,buffered_raw_recheck_siblings,set_decoder_raw_store_leak}.py`.

---

## The unifying shape: **borrowed receiver**

The crown jewel in this slice is not `x = self->field` followed by a use. It is:

> **`PyObject_CallMethod*(self->slot, ...)` — or a direct C call `f(self->slot, ...)` — hands the
> callee a `self` that is owned by nothing but that slot. The callee runs user Python and then
> keeps touching `self`. Re-entrant code clears or replaces the slot; the callee finishes running
> inside a freed object.**

`_io` is built out of exactly this: `Buffered` dispatches to `self->raw`, `TextIOWrapper` to
`self->buffer` / `self->encoder` / `self->decoder`, `IncrementalNewlineDecoder` to `self->decoder`.
None of those dispatches takes a reference. The re-entrancy weapons are ordinary public API:

| weapon | line | clears |
|---|---|---|
| `TextIOWrapper.__init__` (re-init) | `textio.c:1213-1222` | `buffer`, `encoder`, `decoder`, `readnl`, `decoded_chars`, `pending_bytes`, `snapshot`, `errors`, `raw` |
| `TextIOWrapper.reconfigure` → `_textiowrapper_set_decoder` | `textio.c:976` | `decoder` |
| `BufferedWriter/Reader/Random.__init__` (re-init) | `bufferedio.c:1957`, `:1604`, `:2504` | replaces `raw` |
| `_Buffered.detach` | `bufferedio.c:626` | `raw = NULL` |

**Neither the GIL nor a critical section stops this.** All four findings reproduce identically on
`debug-ft-nojit` / `release-ft-nojit`, where the `@critical_section` clinic annotations are live:
CPython's critical sections suspend on re-entry, so they defend against a *second thread*, never
against the *same thread* coming back through user Python.

---

## Findings

### [FIX] 1 — `IncrementalNewlineDecoder` dereferences and WRITES to its own freed `self` (`Modules/_io/textio.c:350`, `:511`, `:575`)

**What.** `_PyIncrementalNewlineDecoder_decode` reads `self->decoder` and calls the inner decoder:

```c
337    if (self->decoder != Py_None) {
338        output = PyObject_CallMethodObjArgs(self->decoder,
339            &_Py_ID(decode), input, final ? Py_True : Py_False, NULL);   /* user Python */
```

and then, with no re-read and no strong reference anywhere, keeps using `self`:

```c
350    if (self->pendingcr && (final || output_len > 0)) {   /* UAF read  */
365        self->pendingcr = 0;                              /* UAF write */
380            self->pendingcr = 1;                          /* UAF write */
389        int seennl = self->seennl;                        /* UAF read  */
435        else if (!self->translate) {                      /* UAF read  */
511        self->seennl |= seennl;                           /* UAF read-modify-write */
```

`_io_IncrementalNewlineDecoder_getstate_impl` has the identical shape:
`:551` calls `self->decoder.getstate()`, `:575` reads `self->pendingcr`.

`self` here is the newline decoder, whose **only** owner is `textio->decoder`. The two callers hand
it over borrowed — `textio.c:2092-2093` `_PyIncrementalNewlineDecoder_decode(self->decoder, bytes, 1)`
(a direct C call) and `textio.c:1952` `PyObject_CallMethodNoArgs(self->decoder, &_Py_ID(getstate))`.
A re-entrant `TextIOWrapper.__init__` runs `Py_CLEAR(self->decoder)` at `textio.c:1216`, and
`incrementalnewlinedecoder_dealloc` (`textio.c:296`) frees the block under the running callee.

**Measured.**

```
debug-gil-nojit-asan   heap-use-after-free READ  textio.c:350  _PyIncrementalNewlineDecoder_decode
                       freed by incrementalnewlinedecoder_dealloc textio.c:296
                         <- _io_TextIOWrapper___init___impl textio.c:1216
                         <- _PyIncrementalNewlineDecoder_decode textio.c:339
release-gil-nojit-asan same report, same two lines  (rc=1)
debug-gil-nojit-asan   heap-use-after-free at textio.c:511  (`--del` mode, see below)  rc=1
debug-gil-nojit-asan   heap-use-after-free at textio.c:575  (`--getstate` mode)        rc=1
debug-gil-nojit / release-gil-nojit (no ASan)   0/5 crash — silent corruption
_pyio twin                                       survives, returns 49152 chars
```

`--del` mode moves the free past `:350` by returning a `str` subclass whose `__del__` detonates,
so the `Py_DECREF(output)` at `textio.c:505` is the free and `self->seennl |= seennl` at `:511`
becomes the *first* post-free access. ASan labels `:511` a READ of size 1 because it instruments
the load half of the bit-field read-modify-write and halts there; the store to the same freed byte
is the next instruction. **Honest limit:** I have an ASan report for the read half only. The
non-ASan run completes and returns, which requires `:511` to have executed, so the store did run —
but I did not observe the store itself. The build is not compiled with `-fsanitize-recover=address`,
so `halt_on_error=0` does not expose a second report.

**Guarded twin.** In the same file, `textiowrapper_change_encoding` (`textio.c:1355-1379`) takes
`encoding`/`errors` out of the same kind of slot and `Py_INCREF`s both before doing anything that
runs Python; `textio.c:1917` and `:2325` do `Py_NewRef(self->decoded_chars)` for the same reason.

**Fix.** Take a strong reference on the receiver at the two dispatch sites:
`textio.c:1952` and `textio.c:2092-2093` should hold `PyObject *dec = Py_NewRef(self->decoder);`
across the call. (Fixing the callee instead would require re-reading a slot it does not own.)

---

### [FIX] 2 — a nested `Buffered` is used as a borrowed receiver; `LEAVE_BUFFERED` then unlocks a freed lock (`Modules/_io/bufferedio.c:2018`, `:2232`)

**What.** `_bufferedwriter_raw_write` dispatches through the slot:

```c
1996        res = PyObject_CallMethodOneArg(self->raw, &_Py_ID(write), memobj);
```

When `self->raw` is itself an `_io.BufferedWriter`, the callee `_io_BufferedWriter_write_impl`
runs on a borrowed `self`, drives arbitrary user Python through *its own* raw, and then:

```c
2018        if (n > 0 && self->abs_pos != -1)      /* UAF read  */
2232    LEAVE_BUFFERED(self)                        /* self->owner = 0; PyThread_release_lock(self->lock) */
```

`buffered_dealloc` has already run `PyThread_free_lock(self->lock); self->lock = NULL;`
(`bufferedio.c:437-441`) and `tp_free` (`:444`), so `LEAVE_BUFFERED` dereferences NULL.
The weapon is `Py_XSETREF(self->raw, raw)` in `_io_BufferedWriter___init___impl` at
`bufferedio.c:1957` — re-init takes **no** `ENTER_BUFFERED`, so it succeeds from inside a
lock-held window.

**Measured.**

```
debug-gil-nojit     SIGSEGV  5/5   (rc=139)
release-gil-nojit   SIGSEGV  5/5   (rc=139)
debug-ft-nojit      SIGSEGV        release-ft-nojit  SIGSEGV
_pyio twin          raises OSError("write() returned incorrect number of bytes"), survives (rc=0)

gdb, release-gil-nojit:
  #0 _Py_atomic_compare_exchange_uint8 (obj=0x0, ...)
  #1 _PyMutex_Unlock (m=0x0)
  #2 PyThread_release_lock (lock=0x0)
  #3 _io_BufferedWriter_write_impl ... bufferedio.c:2232

ASan, debug-gil-nojit-asan:
  heap-use-after-free READ of size 8 at bufferedio.c:2018 in _bufferedwriter_raw_write
  freed by buffered_dealloc bufferedio.c:444
    <- _io_BufferedWriter___init___impl bufferedio.c:1957
    <- _bufferedwriter_raw_write bufferedio.c:1996
```

Per the brief's oracle table (C SIGSEGV / twin raises cleanly) this is a confirmed, localized C bug.

**Guarded twin.** None in `bufferedio.c` — the file has **zero** sites that `Py_INCREF` a slot
before dispatching through it. The nearest correct model is `textio.c`'s
`textiowrapper_change_encoding` (finding 1).

**Fix.** `PyObject *raw = Py_NewRef(self->raw); res = PyObject_CallMethodOneArg(raw, ...); Py_DECREF(raw);`
at `bufferedio.c:1996`, and the same at the other `self->raw` dispatch sites (`:517`, `:591`,
`:788`, `:818`, `:1485`, `:1640`, `:1713`, `:1748`).

---

### [FIX] 3 — `TextIOWrapper.tell()` dereferences `next_input`, borrowed out of `self->snapshot`, after `decoder.getstate()` (`Modules/_io/textio.c:2937`, `:2938`, `:2982`)

**What.**

```c
2873    if (!PyArg_ParseTuple(self->snapshot, "iO", &cookie.dec_flags, &next_input))
                                     /* next_input is BORROWED out of the snapshot tuple */
2889    saved_state = PyObject_CallMethodNoArgs(self->decoder, &_Py_ID(getstate));  /* user Python */
...
2937    assert(skip_bytes <= PyBytes_GET_SIZE(next_input));     /* UAF read (debug) */
2938    input = PyBytes_AS_STRING(next_input);                  /* raw pointer into freed payload */
2982    input_end = input + PyBytes_GET_SIZE(next_input);       /* UAF read (all builds) */
```

`input` / `input_end` are then fed byte-by-byte into `decoder.decode("y#", ...)`, so freed heap is
copied into a `bytes` and handed to Python. A re-entrant `TextIOWrapper.__init__` runs
`Py_CLEAR(self->snapshot)` at `textio.c:1220`, dropping the only reference to the tuple and to
`next_input`.

**Measured.**

```
debug-gil-nojit     SIGSEGV  5/5   (rc=139)
release-gil-nojit   0/5 — survives, returns a wrong cookie (silent)
debug-ft-nojit      SIGSEGV
debug-gil-nojit-asan    heap-use-after-free READ size 8 at textio.c:2937
release-gil-nojit-asan  heap-use-after-free READ size 8 at textio.c:2982   <- the non-assert read
   both: freed by _io_TextIOWrapper___init___impl textio.c:1220
         <- _io_TextIOWrapper_tell_impl textio.c:2889
_pyio twin          survives, `tell` returns 1
```

The release-ASan report at `:2982` matters: it proves the hazard is not merely the debug-only
assertion at `:2937`.

**Guarded twin.** `_textiowrapper_readline` at `textio.c:2325` does
`line = Py_NewRef(self->decoded_chars);` and only then `ptr = PyUnicode_DATA(line)` at `:2340` —
the identical idiom (a raw data pointer derived from a slot-owned object, held across a
Python-reaching loop that clears that very slot at `:2405`), with the strong reference.
`textiowrapper_change_encoding:1381` does the same for a `char*` from `PyUnicode_AsUTF8`.

**Fix.** `Py_INCREF(next_input)` immediately after the `PyArg_ParseTuple` at `:2873` and
`Py_DECREF` it on every exit (or route it through a `Py_XSETREF`-managed local).

---

### [FIX] 4 — `bufferedio.c` re-reads `self->raw` after user Python with no re-check; the gh-143008 fix was applied to `textio.c` only (`bufferedio.c:591`, `:1389`)

**What.** This is the sibling family of the run's seeded lead (`detach`, `bufferedio.c:625`).
`_io._Buffered.close` deliberately drops its own lock to run a user flush and then re-reads the
slot with no re-check:

```c
580    /* flush() will most probably re-take the lock, so drop it first */
581    LEAVE_BUFFERED(self)
582    r = _PyFile_Flush((PyObject *)self);          /* user Python, lock NOT held */
583    if (!ENTER_BUFFERED(self)) { return NULL; }
...
591    res = PyObject_CallMethodNoArgs(self->raw, &_Py_ID(close));   /* self->raw may be NULL */
```

`_io._Buffered.seek` has the same hole *before* it takes the lock: `CHECK_CLOSED(self)` at `:1385`
expands to `buffered_closed(self)` → `PyObject_GetAttr(self->raw, &_Py_ID(closed))` +
`PyObject_IsTrue`, which runs a Python `closed` property; then

```c
1389    if (_PyIOBase_check_seekable(state, self->raw, Py_True) == NULL) {
```

A re-entrant `detach()` inside either window sets `self->raw = NULL` (`bufferedio.c:626`) —
`detach` takes no `ENTER_BUFFERED` at all.

**Measured** (`repro/buffered_raw_recheck_siblings.py <site>`):

| site | debug-gil-nojit | release-gil-nojit | debug-ft / release-ft | `_pyio` |
|---|---|---|---|---|
| `close` (`:591`) | **SIGABRT 5/5** `Assertion 'obj != NULL' failed` @ `pycore_stackref.h:628` | **SIGSEGV 5/5** | 134 / 139 | `AttributeError`, survives |
| `seek` (`:1389`) | **SIGABRT 5/5**, same assertion | **SIGSEGV 5/5** | 134 / 139 | `AttributeError`, survives |
| `truncate` (`:1485`) | survives | survives | — | (twin hangs, see note) |
| `readall` (`:1713`/`:1748`) | survives | survives | — | survives |

**Be precise about the two that survive.** `truncate` and `read_all` have the *same* unguarded
re-read, and they are safe only *incidentally*: they run their user code inside `ENTER_BUFFERED`,
and `detach`'s own `_PyFile_Flush` re-enters a locked method and is rejected with
`RuntimeError: reentrant call inside <_io.BufferedWriter>`. That protection is a side effect of
`detach` happening to flush — it is not a re-check, and it does **not** protect against the
`__init__` weapon (finding 2 detonates from inside exactly such a lock-held window). I classify
these two as latent, not clean.

**Guarded twin — and this is the whole point.** `textio.c:740 buffer_access_safe()` exists for
precisely this hazard. It was introduced by `db4b1948bc4` *"gh-143008: Fix Null pointer
dereferences in TextIOWrapper underlying stream access"*, whose own commit message says:

> *"The cases which need to be re-checked are hard to spot so rather than rely on reviewer effort
> create better safety by making all self->buffer access go through helper functions."*

`git show --stat db4b1948bc4` touches `Lib/test/test_io/test_textio.py`, two NEWS files,
`Modules/_io/clinic/textio.c.h` and `Modules/_io/textio.c` — **and nothing else**.
`grep -c access_safe Modules/_io/bufferedio.c` → **0**. The sibling file with the structurally
identical hazard on `self->raw` was never converted.

**Fix.** Port the helper: a `raw_access_safe(buffered *self)` that raises the
`CHECK_INITIALIZED`-style error when `self->raw == NULL`, and route all 19 `self->raw` uses
through it. Combined with finding 2's `Py_NewRef`, one helper closes both.

**Caveat on `buffer_access_safe`'s own comment** (`textio.c:757-759`):

```c
    /* Returning a borrowed reference is safe since TextIOWrapper methods are
       protected by critical sections. */
```

That claim covers *concurrent* detach only. Every finding in this report reproduces
single-threaded on `debug-ft-nojit`, where those critical sections are real. Nothing is broken at
the six `buffer_access_safe` call sites today — all six consume the borrowed value with no
intervening call — but the comment should not be read as licensing a borrow held across user code.
(This is the briefing's "guarded twins are twin for a specific threat model" lesson, live.)

---

### [CONSIDER] 5 — raw store into `self->decoder` / `self->encoder` across a user codec factory leaks the re-entrant value (`Modules/_io/textio.c:977`, `:1034`)

```c
976    Py_CLEAR(self->decoder);
977    self->decoder = _PyCodecInfo_GetIncrementalDecoder(codec_info, errors);  /* calls codec_info.incrementaldecoder(errors) */
...
988        Py_XSETREF(self->decoder, incrementalDecoder);   /* <- the guarded form, 11 lines below */
```

`_PyCodecInfo_Get*` invokes the codec's `incrementaldecoder` factory — arbitrary user Python. If
that factory re-enters (`TextIOWrapper.__init__` / `.reconfigure()`) and installs a decoder of its
own, the **raw** store at `:977` overwrites the slot without releasing what the re-entrant call put
there. `_textiowrapper_set_encoder` at `:1032-1034` is identical.

**Measured** (`repro/set_decoder_raw_store_leak.py`, counting constructions vs. `__del__`s after
two `gc.collect()`s):

```
debug-gil-nojit     CountedDecoder constructed=3 finalized=2 leaked=1
release-gil-nojit   CountedDecoder constructed=3 finalized=2 leaked=1
```

The leaked object is #3, installed by the inner `reconfigure` and overwritten by the outer raw
store — exactly the predicted mechanism, not a collectable cycle.

**Fix.** `Py_XSETREF(self->decoder, _PyCodecInfo_GetIncrementalDecoder(...))` — but note
`Py_XSETREF` evaluates its source first, so the `Py_CLEAR` at `:976` must go, otherwise the slot
is NULL during the factory call. The clean form is a local:
`PyObject *dec = _PyCodecInfo_GetIncrementalDecoder(...); if (dec == NULL) return -1; Py_XSETREF(self->decoder, dec);`

`stringio.c:752-753` has the same raw-store spelling
(`self->decoder = PyObject_CallFunctionObjArgs(IncrementalNewlineDecoder_Type, Py_None, ...)`) but
the callee is a C constructor over `Py_None`, so no user code runs; only a GC-triggered `__del__`
could re-enter. Not reported.

---

### [POLICY] 6 — `self->writenl` is a raw `char *` into `self->readnl`'s payload (`Modules/_io/textio.c:941`)

```c
941        self->writenl = (const char *)PyUnicode_1BYTE_DATA(self->readnl);
```

`writenl` (`textio.c:683`) is a refcount-less alias into another `PyObject`'s buffer, consumed at
`:1755` `_PyObject_CallMethod(text, &_Py_ID(replace), "ss", "\n", self->writenl)`.
`set_newline` itself is correct — it installs the new `readnl`, repoints `writenl`, and only then
`Py_XDECREF(old)` at `:952`. The two `Py_CLEAR(self->readnl)` sites (`:1217` in `__init__`, `:1520`
in `tp_clear`) leave `writenl` dangling, but both also leave `self->ok == 0` (`:1176`, `:1515`), so
`CHECK_ATTACHED` blocks every reader before it can be used. **Fragile, not broken.** Recommend
`self->writenl = NULL;` next to each `Py_CLEAR(self->readnl)`.

---

## Classes bounded — negatives with their denominators

**The 26 `borrowed_slot_load_sites` are not 26 borrows.** I re-ran the scanner's own
`_borrowed_slot_loads()` over the seven files and typed every hit:

| bucket | n | examples |
|---|---|---|
| non-`PyObject` scalars / raw pointers | **13** | `n = self->buffer_size` (bufferedio:1036), `written = buffer->len` (×3), `fd = self->fd` (fileio:1085), `pos = self->pos` (stringio:534/537), `start = self->buffer` — a `char *` (bufferedio:1270) |
| module state / static type objects (immortal, module lifetime) | **8** | `state = self->state` (textio:979, :2088), `Buffered_class = state->PyBufferedWriter_Type` (_iomodule:318/322/394/397/400), `type = state->PyBytesIOBuffer_Type` (bytesio:386) |
| genuine `PyObject *` borrows | **5** | see below |

The rule's **effective denominator on this slice is 5, not 26.** All five reviewed:

| site | verdict |
|---|---|
| `textio.c:922 old = self->readnl` | CLEAN — `writenl` repointed before `Py_XDECREF(old)` at `:952`; nothing runs Python in the window |
| `textio.c:1358 encoding = self->encoding` | CLEAN — `Py_INCREF` at `:1365`. **This is the file's guarded twin** |
| `textio.c:1360 errors = self->errors` | CLEAN — `Py_INCREF` at `:1379` |
| `textio.c:1655 pending = self->pending_bytes` | CLEAN — slot NULLed at `:1707` *before* `Py_DECREF(pending)` at `:1708`; no Python-reaching call between the load and the clear (`PyBytes_FromStringAndSize` is not GC-tracked) |
| `bufferedio.c:622 raw = self->raw` | the run's **seeded lead** — confirmed still live, `repro/io_detach_probe.py`. Ownership transfer itself is exact (`:625-626` moves the slot's strong ref to the return value with nothing running Python in between); the defect is the missing NULL re-check |

**The 6 `borrowed_accessor_load_sites`** (`buffer_access_safe` callers: `textio.c:765`, `:776`,
`:787`, `:1638`, `:1862`, `:2677`) — all six consume the borrowed value in the *next statement*
with no intervening call. Real negative.

**Teardown ordering (brief item b).** Verified exact:
- `bufferedio.c:625-626` `raw = self->raw; self->raw = NULL;` — transfer is exact; the slot's strong reference becomes the returned value's, and no Python runs in the window.
- `textio.c:1638-1642` — same, and it re-derives the handle through `buffer_access_safe()` after its flush. This is the guarded twin the `bufferedio` twin lacks.
- `bufferedio.c:419-420`, `textio.c:1213-1222`, `:1516-1525` — all use `Py_CLEAR`, which NULLs before dropping and is therefore re-entrancy-safe. **No open-coded `field = NULL; Py_DECREF(local);` clear exists anywhere in the slice** (`_textiowrapper_writeflush:1707-1708` is the correct ordering, not the dangerous one). The `stale_slot_decref` class is genuinely absent here — a clean negative for the one rule that has a real, non-trivial denominator in `Objects/`.

**`bytesio` `exports` / `SHARED_BUF` invariants (brief item c).** The documented state machine
(`bytesio.c:40-43`) holds. Every mutation and every buf-clearing path is gated:
`truncate:740`, `close:928`, `__setstate__:1008` use `CHECK_EXPORTS`; `__init__:1130` open-codes the
same test; `bytesio_clear:1196` refuses to clear `buf` while `exports > 0`; `bytesio_dealloc:1080`
reports a `SystemError`. The two zero-copy escapes that raise `Py_REFCNT(buf)` — `getvalue:416`
and `peek_bytes_lock_held:478` — are both gated on `exports == 0`, and
`bytesiobuf_getbuffer_lock_held:1282` unshares before taking the first export, so
`exports > 0 ⟹ refcount 1` is maintained. `write_bytes_lock_held:245` re-checks
`check_closed || check_exports` *after* `PyObject_GetBuffer` ran a user `__buffer__` — that
re-check is itself a guarded twin of the shape findings 3 and 4 violate. **No finding.** (The
`FT_ATOMIC_..._RELAXED` ordering question on `exports` is FT territory; not mine.)

**Stolen-reference APIs (brief item d).** Three uses in the slice, all correct:
`textio.c:1837-1838` `PyList_SET_ITEM(list, 0, self->pending_bytes)` steals the slot's reference
into a fresh, unpublished list and immediately reassigns the slot — with an in-code comment
justifying why `PyList_New` cannot trigger GC there; `textio.c:2037` and `:577` use
`Py_BuildValue("NN"/"NK")`, whose `N` consumes on both the success and failure paths;
`_iomodule.c:670/677` use `PyModule_AddObjectRef` (non-stealing). No `PyTuple_SET_ITEM`, no
`PyModule_AddObject`, no `PyList_SetItem` anywhere in the seven files.

**`_io_open_impl` (`_iomodule.c:206-452`), the slice's most leak-prone constructor.** Traced all
four layers: `result` tracks the outermost object, each `Py_DECREF(raw)` / `Py_DECREF(buffer)`
follows the ownership transfer into the next wrapper, and the `error:` label at `:442` closes and
drops `result` while `Py_XDECREF`-ing `path_or_fd` and `modeobj`. No leak on any of the 14 error
paths. Clean.

**`fileio.c` audit-hook window.** `PySys_Audit("open", "Osi", nameobj, ...)` at `:395` runs an
arbitrary Python audit hook, and `name` at `:417` is a raw `char *` from
`PyBytes_AS_STRING(stringobj)` set at `:316`. This is the CPY-0054 shape — but `stringobj` is a
strong out-parameter of `PyUnicode_FSConverter`, held to the `error:`/`done:` labels. Correctly
guarded; **no finding**, stated so the next reader does not re-derive it.

**`iobase.c`.** `_io__IOBase_readlines_impl` and `iobase_finalize` are refcount-correct;
`iobase_finalize` brackets the whole teardown with `PyErr_GetRaisedException` /
`PyErr_SetRaisedException` and reports via `PyErr_FormatUnraisable`. No finding.

---

## Toolkit assessment

### Precision of the rules that fired here

None fired. **0 false positives out of 0 findings** — nothing to grade. What is gradeable is the
denominator, and it is misleading (below).

### Recall gaps — the deliverable

**Gap A (highest value): the borrowed *receiver*.** All four FIX findings' central move is
`Call(self->slot, ...)` where the callee is C code that touches its own `self` after running
Python. The scanner's whole borrowed-ref family keys off `_SLOT_LOAD_RE` — `local = owner->field;`
— so it is structurally incapable of seeing this: the borrow never becomes a local in the caller,
it becomes the *callee's* first parameter.

There is a fully intra-file, high-precision version of this rule. Proposed
`borrowed_receiver_across_call`, two halves joined inside one translation unit:

- *Callee half.* A function `f(T *self, ...)` whose body contains a `PYTHON_REACHING_APIS` call
  followed by a `self->` read or write. Cheap; reuses `reaching_calls_with_slots`.
- *Caller half.* A call that passes a **slot expression** as argument 0 — either
  `PyObject_CallMethod*(X->field, ...)` or a direct `f(X->field, ...)` where `f` is defined in the
  same file — with no `Py_INCREF`/`Py_NewRef` on `X->field` in the caller, **and** where some
  function in the same file does `Py_CLEAR(Y->field)` or `Py_XSETREF(Y->field, ...)` for the same
  field name.

The direct-C-call form alone (`textio.c:2092 _PyIncrementalNewlineDecoder_decode(self->decoder, ...)`
→ `_PyIncrementalNewlineDecoder_decode` at `:326` writes `self->pendingcr` at `:365` after the call
at `:339`) is **decidable with zero interprocedural guessing** and would have found finding 1
outright. Ship that arm first; gate the `CallMethod*` arm on the same-file-clear condition to hold
precision.

**Gap B: a borrow that arrives through a parse/out-parameter.**
`PyArg_ParseTuple(self->snapshot, "iO", &flags, &next_input)` is a slot borrow with no
`local = owner->field` anywhere. Finding 3 is invisible for exactly this reason. The `"O"`, `"O!"`,
`"O&"`-with-no-converter and `"S"`/`"U"`/`"b"` codes of `PyArg_Parse*`, and `PyDict_GetItemRef`-era
out-params, all produce borrows. Extend `_borrowed_slot_loads` with a second producer: *an address-of
argument (`&x`) handed to a `PyArg_Parse*` whose first argument is a slot expression makes `x` a
borrow from that slot.* Everything downstream (the reaching-call scan, the clear detection, the
`Py_INCREF` suppression) then works unchanged.

**Gap C: derived raw pointers.** `input = PyBytes_AS_STRING(next_input)` and
`self->writenl = PyUnicode_1BYTE_DATA(self->readnl)` create `char *` aliases that outlive their
owner's refcount. The existing rules track the `PyObject *` local; the `char *` derived from it is
what actually gets dereferenced 50 lines later. A **taint edge** from
`PyBytes_AS_STRING` / `PyUnicode_DATA` / `PyUnicode_1BYTE_DATA` / `PyByteArray_AS_STRING` /
`PyBytes_AsString` to the derived local, propagated into the existing use-detection, is a small
change with two true positives in this slice (finding 3, finding 6). The FP taxonomy's
"raw `PyMem_Malloc` buffer hanging off a live object" entry already anticipates this class
verbally; nothing implements it.

**Gap D: the denominator is 5× inflated.** `borrowed_slot_load_sites: 26` reads as strong coverage.
Typed, it is 13 scalars, 8 module-state/static-type pointers, and **5** real `PyObject *` borrows.
A zero against 26 sounds like a swept file; a zero against 5 is a coin flip. Concrete fix: split
the counter into `borrowed_slot_load_sites` (raw) and `borrowed_slot_load_sites_pyobject`
(after excluding (i) targets whose declared type is not a pointer-to-PyObject-ish struct — the
declaration is in the same function body and is cheap to grep — and (ii) owners named `state` /
fields matching `Py[A-Za-z_]*_Type$`, which are module-lifetime by construction). Emit both;
report the second in the sample summary. This is the same "check the denominator" discipline the
brief applies to `scan_lock_discipline`, applied one level down — inside a rule whose denominator
is nonzero but mostly noise.

**Gap E (cross-scanner, cheap): the un-ported-fix detector.** Finding 4 is a `git`-visible fact —
a commit whose message says *"make all `self->X` access go through helper functions"*, that added
`buffer_access_safe` to `textio.c` and nothing to `bufferedio.c`, which has the same field family
and zero helpers. `analyze_history.py` already reads commit messages. A rule of the form *"a commit
introduced an `*_access_safe` / `*_check_*` accessor into file A; sibling file B in the same
directory has the same slot-dispatch density and no such accessor"* would have surfaced this
mechanically. That is the fix-propagation the informed loop exists for.

### What the scanner got right, and should keep

`field_accessors_discovered: ["buffer_access_safe"]` is exactly the right instinct — the scanner
*found the guarded twin by itself*. It then had no rule that asks the obvious next question:
**which sibling file has the same hazard and no accessor?** That question is finding 4, worth two
crash sites. Discovering the accessor and not asking that is the single largest missed
opportunity in this run.

### Method notes worth carrying forward

- **`_pyio` earned its keep.** Four of four crash sites survive on the twin, which converted three
  "C crashes on adversarial input" into "confirmed, localized C bug" with no further argument.
- **The FT builds are a control, not a target.** Every finding reproduces on `debug-ft-nojit`
  identically to `debug-gil-nojit`. That single fact retires the "critical sections make this safe"
  defence for the whole re-entrancy class in `_io`.
- **Release-vs-debug split is real and bidirectional here.** Finding 3 is 5/5 on debug-gil and
  **0/5 on release-gil** — a reviewer who only ran release would have called it clean. Finding 2 is
  5/5 on both. Both were needed.
- **ASan halts on the first access.** For a bug whose *write* is the dangerous half, engineer the
  free to land between the reads and the write (finding 1's `--del` mode uses a `str` subclass
  `__del__` to move the free 155 lines down). Without `-fsanitize-recover=address` in the matrix
  builds there is no second report; consider adding one recovering ASan build.
