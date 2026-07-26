# init-bypass-checker — slice `mod-io`

Target `/home/danzin/projects/cpython` @ `4f3be1b5777`. Build matrix @ `a1d580430c8`.
**`git diff a1d580430c8 4f3be1b5777 -- Modules/_io/ Lib/_pyio.py` is empty** — every line
number cited below is valid for both the reviewed ref and every binary I ran.

## Headline

The naive triage ("`CHECK_INITIALIZED` guards `self->ok`, so all 26 are false positives") is
wrong for **8 of the 26**. Reframing to *"can this field be NULL AFTER the guard ran?"* turns
the pile from 0% precision into **31% precision with 6 hard SIGSEGVs**, and reading around it
found **4 more live sites the scanner cannot see**.

| measured | count |
|---|---|
| Nullable fields seen | **32**, across **2 of 7** files (denominator non-zero — the rule fired) |
| Candidate reads | 26 (2 marked `dominated_by`, 0 `duplicate_locations`) |
| **FIX** (field provably NULL at the read, reproduced) | **8** of the 26 |
| **ACCEPTABLE** | 18 of the 26 |
| **Net-new FIX found by reading** (not in the 26) | **4** |
| Distinct live crash sites total | **12** (10 `bufferedio.c`, 2 `textio.c`) |
| Reproduced hard crashes (SIGSEGV/SIGABRT) | **9 of 12**; the other 3 are `SystemError` |
| All 12 also crash 3.14.6+ | yes → **longstanding, not regressions** |

The classic `__new__`-bypass remit came back almost clean and *one* case came back as a
segfault: an **811-execution sweep** (9 types × 2 constructions × 45 probes) found **0 crashes**,
but `_io.TextIOWrapper.reconfigure()` is the single method in the slice with **no state guard at
all**, and it segfaults on all four builds once its accidental guard is removed.

---

## 1. Why the field can be NULL — the three mechanisms, with denominators

### 1a. `del obj.field` — CLOSED, measured

* **9 `PyMemberDef` tables**, 26 entries, **5 `_Py_T_OBJECT` entries** (`raw` in
  `bufferedreader_members:2577` / `bufferedwriter_members:2636` / `bufferedrandom_members:2753`;
  `encoding` and `buffer` in `textiowrapper_members:3484-3485`). **All five carry
  `Py_READONLY`.**
* **11 `PyGetSetDef` tables**; exactly **one `@setter` in the whole slice**
  (`_io.TextIOWrapper._CHUNK_SIZE`, `Modules/_io/textio.c:3401`), and it rejects deletion at
  `:3409` — `if (value == NULL) { PyErr_SetString(PyExc_AttributeError, "cannot delete attribute"); return -1; }`.

Both `deletable_member` and `deletable_getset` paths are structurally closed. The scanner agrees
(`nullable_fields_by_reason` is `{"new_bypass": 32}` with no member/getset entries) — this is a
*confirmed* negative, not an unexercised one.

### 1b. `T.__new__(T)` — OPEN for 6 of 9 types, but almost always caught

`Modules/_io/bufferedio.c` and `Modules/_io/textio.c` register **`Py_tp_init` with no
`Py_tp_new`** for all six of their types (`bufferedio.c:2602, 2660, 2710, 2778`;
`textio.c:3447, 3513`) — the gh-144330 shape exactly. The other three types have a real `tp_new`
that establishes the invariant immediately after `tp_alloc`
(`bytesio_new`, `stringio_new`, `fileio_new`) — which is *why* `scan_init_bypass` reports zero
nullable fields in those five files. I verified that independently; it is a real negative.

I swept it: `reports/mod-io/repro/io_newbypass_sweep.py`, 9 types × {`T.__new__(T)`,
`class S(T): __init__ = no-op`} × 45 probes = **811 executions**, byte-identical results on
`debug-gil-nojit` and `release-gil-nojit`, **rc=0, zero crashes** (147 clean returns, 664 clean
`ValueError`/`AttributeError`/`UnsupportedOperation`). `CHECK_INITIALIZED` / `CHECK_ATTACHED` /
`CHECK_INITIALIZED_DECODER` / `_forward_call`'s `if (self == NULL)` cover every entry point —
**except one** (§3.1).

### 1c. Stale guard — the mechanism that actually matters here

`self->ok = 0` and `self->raw = NULL` are set **together** by
`_io__Buffered_detach_impl` (`bufferedio.c:625-628`) and by `buffered_clear` (`:417-419`), but a
function that already passed `CHECK_INITIALIZED` and then ran user Python holds a *stale*
verdict. That is the seeded lead's mechanism, and it generalises to every `self->raw` read that
follows a Python-reaching call.

---

## 2. Triage of all 26 scanner findings

The brief said the 26 were "all on `self->raw` or `self->buffer`". They are not: they split
`raw`×13 / `state`×7 / `decoder`×4 / `errors`×2, and **no finding is on `self->buffer`** —
textio's `buffer` was already hardened by gh-143008 / gh-142594 (`buffer_access_safe`,
`textio.c:740`).

### `Modules/_io/bufferedio.c`, field `raw` — 7 FIX / 6 ACCEPTABLE

| # | line | function | verdict | measured |
|---|---|---|---|---|
| 0 | 517 | `_io__Buffered_simple_flush_impl` | ACCEPTABLE | control rc=0 ×4 builds |
| 1 | **591** | `_io__Buffered_close_impl` | **FIX** | 134 dbg / 139 rel / 134 dbg-ft / 139 rel-ft / 139 on 3.14 |
| 2–6 | 644, 657, 670, 714, 727 | `seekable`/`readable`/`writable`/`fileno`/`isatty` | ACCEPTABLE | control rc=0 ×4 |
| 7 | **788** | `_buffered_raw_tell` | **FIX** | 134 / 139 / 134 / 139 / 139 |
| 8 | **818** | `_buffered_raw_seek` | **FIX (SystemError)** | `SystemError` ×4 builds + 3.14 |
| 9 | **1485** | `_io__Buffered_truncate_impl` | **FIX** | 134 / 139 / 134 / 139 / 139 |
| 10 | **1640** | `_bufferedreader_raw_read` | **FIX** | 134 / 139 / 134 / 139 / 139 |
| 11 | **1748** | `_bufferedreader_read_all` | **FIX** | 134 / 139 / 134 / 139 / 139 |
| 12 | **1996** | `_bufferedwriter_raw_write` | **FIX** | 134 / 139 / 134 / 139 / 139 |

The 6 ACCEPTABLE ones read `self->raw` on the statement immediately after `CHECK_INITIALIZED`,
with no intervening call — the guard is not stale because nothing runs. Under free-threading
they are additionally serialised against `detach` (both are `@critical_section` on `self`).
*Honest limit:* I measured these single-threaded only; the FT argument is reading, not a
concurrency stress.

### `Modules/_io/textio.c` — 1 FIX / 12 ACCEPTABLE

| # | line | field | verdict | why |
|---|---|---|---|---|
| 13,14,15 | 339, 551, 633 | `decoder` (nldecoder) | ACCEPTABLE | see below |
| **16** | **1380** | `errors` | **FIX** (crash is at **:1365**) | reproduced 139 ×4 builds |
| 17,18,19,21,22,23,24 | 1467, 1742, 2072, 2626, 2640, 2660, 2838 | `state` | ACCEPTABLE | `self->state` is a `_PyIO_State *`, set at `:1279` *before* `self->ok = 1` at `:1325`; every read is behind `CHECK_ATTACHED` (:1738, :2068, :2620, :2834) or behind `self->decoded_chars != NULL` (:1467), which itself implies initialisation |
| 20 | 2546 | `decoder` | ACCEPTABLE | all three call sites of `_textiowrapper_decoder_setstate` are guarded — `:2739` `if (self->decoder)`, and `:2941`/`:2964` sit past `tell`'s `if (self->decoder == NULL \|\| self->snapshot == NULL) return posobj;` at `:2857` |
| 25 | 3382 | `errors` | ACCEPTABLE | `_io_TextIOWrapper___init___impl` sets `self->ok = 0` at `:1176` **before** the `Py_CLEAR` block at `:1213-1222`, so a failed re-`__init__` cannot leave `ok > 0` with `errors == NULL`; `textiowrapper_change_encoding` only ever `Py_SETREF`s it |

**The three `IncrementalNewlineDecoder` findings are the prompt's signature trap and are still
ACCEPTABLE — but for a fragile reason worth a POLICY note.** `CHECK_INITIALIZED_DECODER`
(`textio.c:315`) tests **`self->errors == NULL`**, and the code then branches on
`if (self->decoder != Py_None)` — a *different field*, and a `!= Py_None` test that is not a
NULL guard. It survives only because `__init__` (`textio.c:261-262`) writes
`Py_XSETREF(self->errors, …)` then `Py_XSETREF(self->decoder, …)` with **no Python call in
between**, and `Py_XSETREF` stores before it decrefs, so the field is never observably NULL
after the first init. I stress-tested the residual first-init window on the FT builds
(`io_initbypass_residual.py nldecoder_ft_race`, 3 re-init threads × 3 poke threads × 400k
iterations): `PYTHON_GIL=0` on `debug-ft-nojit` and `release-ft-nojit`, **rc=0, no crash**. The
window is two adjacent stores; I could not hit it. Recording as ACCEPTABLE-with-caveat, not as a
proven negative.

---

## 3. FIX findings

### 3.1 [FIX] `TextIOWrapper.reconfigure()` has no state guard at all — `Py_INCREF(NULL)` (textio.c:1365, via :1501)

**Novel** — not flagged by any sibling agent in this slice, and no prior art
(`gh api search/issues` on `TextIOWrapper reconfigure crash` returns only gh-111942, a different,
closed argument-type bug).

`_io_TextIOWrapper_reconfigure_impl` (`textio.c:1434`) is **the only `TextIOWrapper` method with
no `CHECK_ATTACHED` / `CHECK_INITIALIZED`** — the file uses those macros 25 times and this
function uses neither. Its only protection is incidental: `_PyFile_Flush((PyObject *)self)` at
`:1490` normally dispatches to `_io_TextIOWrapper_flush_impl`, which *does* guard. A pure-Python
subclass that overrides `flush()` removes it. Then:

```c
    if (encoding == Py_None) {
        encoding = self->encoding;          /* NULL after __new__ */
        if (errors == Py_None)
            errors = self->errors;          /* NULL */
        Py_INCREF(encoding);                /* textio.c:1365  <-- dies here */
    }
    ...
    Py_INCREF(errors);                      /* textio.c:1380  <-- the scanner's line */
```

**Measured**: `repro/io_reconfigure_newbypass.py newline` →
**139 (SIGSEGV) on all four builds** (`debug-gil-nojit`, `release-gil-nojit`, `debug-ft-nojit`,
`release-ft-nojit`) and **139 on 3.14.6+**. ASan frame on `debug-gil-nojit-asan`:

```
SEGV on unknown address 0x000000000000 ... READ
 #0 Py_INCREF                            Include/refcount.h:286
 #1 textiowrapper_change_encoding        Modules/_io/textio.c:1365
 #2 _io_TextIOWrapper_reconfigure_impl   Modules/_io/textio.c:1501
```
gdb on the 3.14 debug build lands on the same two functions (3.14's `textio.c:1303` / `:1439`).

**Differential oracle**: `_pyio` raises `AttributeError: 'S' object has no attribute '_decoder'`
and exits 1. C accelerator segfaults. Per the brief's grading table this is a *confirmed,
localized C bug*.

**N-of-M**: `reconfigure(newline=…)` crashes; `reconfigure(encoding=…)` raises `ValueError`
cleanly, because that arm reaches `_textiowrapper_set_decoder` → `buffer_callmethod_noargs` →
`buffer_access_safe` first. **1 of 2 reconfigure arms.**

**Guarded twin**: literally every other method of the type — and specifically
`buffer_access_safe` (`textio.c:740`), which the gh-143008 fix threaded through six call sites
but which `reconfigure` reaches only on one of its two arms.

**Fix**: add `CHECK_ATTACHED(self)` as the first statement of
`_io_TextIOWrapper_reconfigure_impl`. (Preferred shape per gh-144330 would be a real `tp_new`
for `TextIOWrapper`, which closes this and any future omission at once.)

### 3.2 [FIX ×6, hard crash] `bufferedio.c` reads `self->raw` after user Python NULLed it

Six sites, all the same shape, all reproduced. Independently corroborated by
`null-safety-scanner.md` and `error-path-analyzer.md` in this slice (they cite the same
`bufferedio.c` line numbers) — I am reporting my own measurements, not relaying theirs.

| site | function | how the re-entrancy gets in | debug-gil / release-gil / debug-ft / release-ft / 3.14 |
|---|---|---|---|
| `:591` | `_io__Buffered_close_impl` | `LEAVE_BUFFERED` then `_PyFile_Flush` at `:580` runs a Python `flush()` override that calls `detach()` | 134 / 139 / 134 / 139 / **139** |
| `:788` | `_buffered_raw_tell` (from `truncate:1489`) | the user's `raw.truncate()` detaches, *after* `:1485` returned | 134 / 139 / 134 / 139 / **139** |
| `:1485` | `_io__Buffered_truncate_impl` | `CHECK_CLOSED` → `buffered_closed:526` → a `closed` **property** on the raw detaches — before `ENTER_BUFFERED` is even taken | 134 / 139 / 134 / 139 / **139** |
| `:1640` | `_bufferedreader_raw_read` | loop-carried: iteration *N*'s `raw.readinto()` detaches, iteration *N+1* re-reads `self->raw` | 134 / 139 / 134 / 139 / **139** |
| `:1748` | `_bufferedreader_read_all` | loop-carried: `raw.read()` detaches, next iteration re-reads `self->raw` | 134 / 139 / 134 / 139 / **139** |
| `:1996` | `_bufferedwriter_raw_write` | loop-carried from `_io_BufferedWriter_write_impl:2187`; a *partial* `raw.write()` keeps the loop going | 134 / 139 / 134 / 139 / **139** |

Every one attributed by a **gdb frame**, e.g.

```
#11 _bufferedwriter_raw_write (self=0x…, len=8176)  Modules/_io/bufferedio.c:1996
#12 _io_BufferedWriter_write_impl (self=0x…)         Modules/_io/bufferedio.c:2187
```

`_pyio` returns cleanly or raises `AttributeError: 'NoneType' object has no attribute 'write'`
on all six.

**One non-obvious enabling condition, worth recording.** A re-entrant `detach()` is
*accidentally* blocked whenever the outer frame holds `ENTER_BUFFERED`, because
`detach → _PyFile_Flush → self.flush() → _io__Buffered_flush_impl → ENTER_BUFFERED` hits
`_enter_buffered_busy`'s `RuntimeError("reentrant call inside %R")`. My first round of probes
(`io_buffered_reentrant_detach.py`) therefore *survived* on 5 of 8 scenarios. The protection
evaporates the moment the buffered object is a Python subclass overriding `flush()`
(`io_buffered_reentrant_detach2.py`) — which is exactly the seeded lead's shape. **Do not read a
surviving probe of this class as a clean negative until the `flush()` override has been tried.**

**Guarded twin**: `bufferedio.c:489` `_io__Buffered__dealloc_warn_impl` — `if (self->ok && self->raw)`,
in the same file; and `bufferedio.c:2339` `_forward_call`'s `if (self == NULL)` (which is why the
whole `BufferedRWPair` surface came back clean in the 811-probe sweep). The *architectural* twin
is `textio.c:740 buffer_access_safe()`, whose header comment names this exact hazard
("`self->buffer` can be detached (set to NULL) by any user code that is called"). **The gh-143008 /
gh-142594 fix was applied to `textio.c`'s `self->buffer` and never propagated to
`bufferedio.c`'s `self->raw`.** That is the fix-propagation finding.

**Fix**: mirror `buffer_access_safe` as a `raw_access_safe(buffered *self)` in `bufferedio.c` and
route all 22 `self->raw` receiver reads through it.

### 3.3 [FIX] The seeded lead, confirmed — `detach()` returns NULL with no exception (bufferedio.c:625)

`repro/io_detach_probe.py`: `debug-gil-nojit` **rc=134** (`Fatal Python error:
_Py_CheckFunctionResult`), `debug-ft-nojit` 134, `release-gil-nojit` and `release-ft-nojit` rc=0
with `SystemError: … returned NULL without setting an exception`, 3.14 rc=0 same, `_pyio` returns
`None`. Confirmed, not re-litigated. **The scanner does not see this site at all** — see §4.

### 3.4 [FIX] Two sites where the callee's own NULL check downgrades a segfault to `SystemError`

* `bufferedio.c:818` `_buffered_raw_seek` (scanner finding #8)
* `textio.c:2775` `_io_TextIOWrapper_seek_impl` (**net new**, §4)

Both pass a NULL receiver to `PyObject_CallMethodObjArgs`, which — unlike its siblings —
**does** NULL-check. gdb frames:

```
#0 null_error                          Objects/call.c:16
#1 PyObject_CallMethodObjArgs (obj=0x0) Objects/call.c:942
#2 _buffered_raw_seek                   Modules/_io/bufferedio.c:818
#2 _io_TextIOWrapper_seek_impl          Modules/_io/textio.c:2775
```

These are still C-contract violations (the brief's grading table: `SystemError` where the twin
raises normally ⇒ FIX), but they are *not* memory-unsafe.

**API-family asymmetry — report this upstream alongside the sites.** Exactly the
`_PyBytes_Resize` shape from the agent charter:

| API | receiver NULL check | outcome on NULL |
|---|---|---|
| `PyObject_CallMethodObjArgs` | **yes**, `Objects/call.c:941` | `SystemError: null argument to internal routine` |
| `PyObject_CallMethodNoArgs` / `PyObject_CallMethodOneArg` → `PyObject_VectorcallMethod` | **no** — only `assert(args != NULL)` on the *array* (`Objects/call.c:890-891`), `args[0]` unchecked | debug: abort at `pycore_stackref.h:628 assert(obj != NULL)`; release: **SIGSEGV** |
| `PyObject_GetAttr` / `PyObject_GetOptionalAttr` | **no**, and no assert either | **SIGSEGV on debug *and* release** |

That table explains the entire debug-vs-release split in my matrix, and it is why
`bufferedio.c:1713` (below) is the one site that segfaults even on a debug build.

---

## 4. Net-new findings the scanner cannot see

Four live sites outside the 26. Each is a concrete recall gap with a named cause.

### 4.1 [FIX] `bufferedio.c:1713` — `PyObject_GetOptionalAttr(self->raw, readall)`

`_bufferedreader_read_all` calls `buffered_flush_and_rewind_unlocked` (which runs the user's
`raw.seek()`/`raw.write()`) and then does `PyObject_GetOptionalAttr(self->raw, &_Py_ID(readall), …)`.
**Measured: rc=139 on all four builds** — `debug-gil-nojit` included, because
`PyObject_GetOptionalAttr` has neither a check nor an assert. gdb:
`#1 _bufferedreader_read_all … bufferedio.c:1713`. `_pyio` raises `AttributeError`.
139 on 3.14 too. Repro: `io_buffered_scanner_gaps.py readall_getattr`.
**Cause of the miss:** `PyObject_GetAttr`/`PyObject_GetOptionalAttr` are not in the sink set.

### 4.2 [FIX] `bufferedio.c:1389` — `_PyIOBase_check_seekable(state, self->raw, Py_True)`

`_io__Buffered_seek_impl` runs `CHECK_CLOSED` (which dispatches the raw object's `closed`
property → user Python → `detach()`) and then hands `self->raw` to a helper that calls a method
on it. **134 debug / 139 release / 134 debug-ft / 139 release-ft / 139 on 3.14.** gdb:
`#11 _PyIOBase_check_seekable (self=0x0) Modules/_io/iobase.c:416`,
`#12 _io__Buffered_seek_impl … bufferedio.c:1389`. `_pyio`: `AttributeError`.
Repro: `io_buffered_scanner_gaps.py seek_seekable`.
**Cause of the miss:** a local helper taking the nullable field as a parameter. The scanner
already has this rule (`sink_kind: one_hop_param_deref`, used for `_unsupported`) but it only
resolves callees whose deref is a *direct* member access, not ones that pass the parameter on to
a call.

### 4.3 [FIX] `textio.c:2775` — `seek()` calls `decode()` on a NULL decoder

Inside `_io_TextIOWrapper_seek_impl`:

```c
    if (self->decoder) {                                       /* :2739  guarded */
        if (_textiowrapper_decoder_setstate(self, &cookie) < 0)
            goto fail;
    }
    if (cookie.chars_to_skip) {                                /* :2743  different if */
        ...
        decoded = PyObject_CallMethodObjArgs(self->decoder,     /* :2775  UNGUARDED */
                                             &_Py_ID(decode), input_chunk, …);
```

`cookie.chars_to_skip` is decoded straight out of the integer the caller passes to `seek()`
(`textiowrapper_parse_cookie:2497`), so it is fully attacker-controlled. Two routes to
`self->decoder == NULL` with `self->ok == 1`:

1. `_textiowrapper_set_decoder:963` returns **success without creating a decoder** when the
   buffer answers `readable() == False` — a buffer that lies about `readable()` but implements
   `read()` reaches the sink.
2. **Much more realistic**: `_textiowrapper_set_decoder:976-978` does `Py_CLEAR(self->decoder)`
   and leaves it NULL when `_PyCodecInfo_GetIncrementalDecoder` fails. So on a *plain*
   `io.TextIOWrapper(io.BytesIO(b"..."))`, a failed `t.reconfigure(encoding=<codec with
   incrementaldecoder=None>)` leaves a fully-readable wrapper with `decoder == NULL`.

**Measured** (`io_textio_seek_null_decoder.py`, both routes): `SystemError: null argument to
internal routine` on `debug-gil-nojit`, `release-gil-nojit`, and 3.14 release; `_pyio` returns
the cookie / raises `LookupError`. gdb frame `#2 _io_TextIOWrapper_seek_impl … textio.c:2775`.
It stays a `SystemError` rather than a SIGSEGV only because of the `PyObject_CallMethodObjArgs`
asymmetry in §3.4 — one line of the same function using `PyObject_CallMethodOneArg` would be a
segfault.

**N-of-M on the NULL-decoder state**: of 9 probed operations, **8 are correctly guarded**
(`read`/`readline`/`iter` → `UnsupportedOperation: not readable` from
`if (self->decoder == NULL)` at `textio.c:1943` and `:2071`; `tell` → the model guard at
`:2857`; `newlines`/`seek(0)`/`write`/`close` clean) and **only `seek()` with
`chars_to_skip != 0`** is not. Controls `seek_zero_skip`, `tell_null_decoder`,
`seek_honest_buffer` all rc=0.

**Guarded twin**: `textio.c:2857` in `_io_TextIOWrapper_tell_impl` —
`if (self->decoder == NULL || self->snapshot == NULL) { … return posobj; }` — and `:2739`,
36 lines above the bug, in the same function.
**Cause of the miss:** the documented "guard scope is the whole function body" trade-off. The
scanner saw `if (self->decoder)` at `:2739` and suppressed every later read of `decoder` in that
function. **This is a measured instance of that trade-off hiding a live bug.**

### 4.4 [FIX] `bufferedio.c:625` — the seeded lead is invisible to the rule

`raw = self->raw; self->raw = NULL; … return raw;` — the sink is *return-NULL-without-exception*,
not a deref, so no sink in the set matches. Worth adding as an explicit shape: **a nullable field
returned directly from a `PyObject *` function with no NULL check** is the
`return-null-without-exception` catalogue shape, reachable from the same field model.

### 4.5 Also missed, but ACCEPTABLE here

`PyObject_GetAttr(self->raw, …)` at `bufferedio.c:526`, `:545`, `:685`, `:699` — same sink-set
gap as 4.1, but each sits on the statement after `CHECK_INITIALIZED`, so not live. Reported so
the sink-set widening in §6 is costed honestly: **+2 live, +4 benign.**

`bufferedio.c:367` (`_PyFileIO_closed(self->raw)` inside `IS_CLOSED`) is ACCEPTABLE by a
non-obvious argument: `fast_closed_checks` requires both `self` and `raw` to be *exact* builtin
types, which forbids the Python `flush()` override, so `detach()` cannot succeed while
`self->ok` is still 1.

---

## 5. Classes bounded (clean negatives, with the denominator that makes them mean something)

| class | denominator | verdict |
|---|---|---|
| `deletable_member` | 9 member tables, 26 entries, 5 `_Py_T_OBJECT`, **5/5 `Py_READONLY`** | **clean, structurally** |
| `deletable_getset` | 11 getset tables, **1 setter total**, and it rejects `value == NULL` (textio.c:3409) | **clean, structurally** |
| pure `T.__new__(T)` reachability | 811 probe executions, 9 types × 2 constructions × 45 probes, identical on debug and release | **clean except reconfigure (§3.1)** |
| `bytesio.c` / `stringio.c` / `fileio.c` / `iobase.c` / `_iomodule.c` | 0 nullable fields — but *because* the first three install real `tp_new`s (`bytesio_new`, `stringio_new`, `fileio_new`) and the last two define no `tp_init`-assigned object field | **real negative, cause identified** |
| `BufferedRWPair` `reader`/`writer` NULL | 13 forwarding methods, all through `_forward_call`'s `if (self == NULL)` (`bufferedio.c:2339`); sweep rows all `RuntimeError`/`UnsupportedOperation` | **clean by design — the model guard of the slice** |
| nldecoder first-init window under FT | 3 re-init × 3 poke threads × 400 000 iterations, `PYTHON_GIL=0`, `debug-ft-nojit` + `release-ft-nojit` | **not reproduced** (window is 2 adjacent stores); ACCEPTABLE-with-caveat, not proven safe |

---

## 6. Toolkit assessment

### 6.1 Precision as it stands

**8 of 26 = 31% precision.** Not the 0% the naive triage predicts, and not high enough to ship
unfiltered. The 18 false positives decompose cleanly:

* **14** are discharged by the *initialisation-completion flag* idiom (`self->ok` / `self->errors`
  set last by `tp_init`, tested first by a `CHECK_*` macro that `return`s).
* **7 of those 14** are on `textio.state`, which is a `_PyIO_State *` — **not a `PyObject *`**
  and not refcounted. It should never have been in the nullable-field set.
* **1** (`textio.c:2546`) is discharged interprocedurally (all three call sites guard).
* **3** (nldecoder) are discharged by an ordering argument inside `Py_XSETREF`.

### 6.2 Recall: 4 live sites missed, 1 line misattributed

| miss | cause | fix |
|---|---|---|
| `bufferedio.c:1713` (SIGSEGV ×4 builds) | `PyObject_GetAttr` / `PyObject_GetOptionalAttr` not in the sink set | R3 |
| `bufferedio.c:1389` (134/139) | one-hop helper that *passes the parameter to a call* rather than dereferencing it directly | R3 |
| `textio.c:2775` (SystemError, 2 routes) | whole-function guard suppression: `if (self->decoder)` 36 lines earlier | R4 |
| `bufferedio.c:625` (the seeded lead) | sink is *return-NULL-without-exception*, not a deref | R7 |
| `textio.c:1365` vs the reported `:1380` | `dominates`/`dominated_by` is applied only to the `one_hop_param_deref` sink, and `encoding` is a **parameter** reassigned from `self->encoding`, which the alias rule drops | R5 |

### 6.3 Concrete tuning proposals

**R1 — model the completion-flag idiom (precision, −14 FPs, 0 TP loss).**
When a file defines `#define CHECK_X(self) if (self-><scalar> <= 0) { PyErr_Set*(…); return …; }`
and the type's `tp_init` assigns `<scalar>` **after** assigning object field *F*, then a read of
*F* in a function whose first statement is `CHECK_X(self)` is discharged **at that point**.
Measured on this slice: suppresses `bufferedio` 517/644/657/670/714/727 and `textio`
1467/1742/2072/2626/2640/2660/2838/3382 — 14 findings — while keeping all 8 true positives.

**R2 — re-flag as a new type `stale_init_guard` (recall; this is the whole point of the slice).**
R1 must **not** discharge the read if, between the guard and the read, the body contains a
Python-reaching call (`scan_refcounts`' `PYTHON_REACHING_APIS`, plus `_PyFile_Flush`, plus any
call whose *receiver* is itself a nullable field of `self`) **and** the same file contains a
Python-reachable function that stores NULL into *F* (`detach`, `close`, `tp_clear`, a re-`__init__`
that `Py_CLEAR`s before it can fail). Emit as its own finding type, and point the line at the
**first** post-call read. Measured: keeps all 8, and R2+R3 together recover 3 of the 4 net-new
sites. Net effect on this slice: **26 findings → 11, of which 11 are real. 31% → 100%.**

**R3 — widen the sink set** to `PyObject_GetAttr`, `PyObject_GetOptionalAttr`, `PyObject_SetAttr`
(all three deref the receiver with no check *and no assert* — the harshest sink in the family),
and make `one_hop_param_deref` resolve callees that *pass* the parameter to a call, not only ones
that dereference it inline. Costed: **+2 live, +4 benign** on this slice.

**R4 — scope guard suppression to the guard's enclosing block, not the whole function.** On
`textio.c` this recovers `:2775` and does not resurrect `:2546`, `:2891`, `:3363` (each inside its
own guard). This is the single highest-value change: the trade-off is documented in the charter,
and here it is a measured live-bug loss.

**R5 — extend `dominates`/`dominated_by` to the `incref` and `call` sinks**, and add *"a parameter
reassigned from a nullable field"* to the alias set. Without it the scanner reports `textio.c:1380`
for a crash that happens at `:1365`.

**R6 — type the nullable-field set.** Exclude fields whose declared type is not `PyObject *` (or a
`PyObject`-derived struct pointer). Drops 7 of 26 findings (27%) on this slice for free.

**R7 — add a `returned_nullable_field_no_exception` shape**: a nullable field returned directly
from a `PyObject *`-returning function with no NULL check. Recovers the seeded lead, and ties this
rule to the catalogue's existing `return-null-without-exception` shape.

**R8 — reproducer guidance, for the agent prompt rather than the scanner.** A surviving probe of
this class is not a negative until the re-entrancy hook has been tried from behind a
**Python-level `flush()` override**. `_enter_buffered_busy`'s `RuntimeError("reentrant call inside %R")`
silently protects 5 of 8 scenarios on an exact builtin type and protects none of them on a
subclass. I lost a full round of probes to this.

### 6.4 One envelope-level note

`summary.dominated_sinks: 2` and `files_with_nullable_fields: 2` were both load-bearing during
triage. `total_nullable_fields: 32` against `files_analyzed: 7` correctly signalled that the rule
had fired; I then verified the five zero-files independently (real `tp_new`s) rather than trusting
the zero. That canary worked.

---

## 7. Prior art

| issue | state | relation |
|---|---|---|
| gh-143008 "Null pointer dereference in `TextIOWrapper.truncate` via re-entrant `flush`" | closed (PR gh-145957) | **the same bug class**, fixed for `textio.c`'s `self->buffer` via `buffer_access_safe` |
| gh-142594 "Null pointer dereference in `TextIOWrapper.close()` via re-entrant `closed` property that detaches `buffer`" | closed | same, same fix |
| gh-143375 "Null pointer dereference in `BufferedWriter.seek` during re-entrant close" | **open** | adjacent: re-entrant `close()` freeing `self->buffer` (the `PyMem` block) rather than NULLing `self->raw`. Confirms `bufferedio.c` is known-unhardened, and that the `buffer_access_safe` fix was never propagated there |
| gh-154523 "Data race: `TextIOWrapper.detach()` stores `self->buffer = NULL` non-atomically" | open | the FT half of the same field |
| gh-111942 `TextIOWrapper.reconfigure()` crashes if encoding is not string or None | closed | *different* bug in the same function; the missing `CHECK_ATTACHED` was not addressed |

Searches run: `gh api -X GET search/issues -f q='repo:python/cpython …'` on
*TextIOWrapper reconfigure crash*, *BufferedReader detach reentrant*, *_io detach NULL raw*,
*TextIOWrapper seek decoder NULL*, *in:title BufferedWriter null pointer*, *buffer_access_safe*.
**No prior art for §3.1 (reconfigure) or §4.3 (seek/decoder).**

## 8. Overlap with sibling agents in this slice

`agents/null-safety-scanner.md` and `agents/error-path-analyzer.md` independently cite
`bufferedio.c` 591 / 818 / 1389 / 1485 / 1640 / 1713 / 1748 / 1996 — the §3.2 and §4.1/§4.2 sets.
Treat those as corroborated-by-two-agents rather than as two findings.
`null-safety-scanner.md` also reports the `self->encoder` analogue (`textio.c:1783`, NULLed by a
failed `reconfigure`), which is a *different field and site* from my §4.3 but the same mechanism —
and it is what led me to the second, far more reachable route to `textio.c:2775`.
**§3.1 (`textio.c:1365`) and §4.3 (`textio.c:2775`) do not appear in any sibling report.**

## 9. Artifacts

All under `reports/mod-io/repro/`:

| file | what |
|---|---|
| `io_newbypass_sweep.py` | the 811-execution `__new__`/subclass sweep (§1b) |
| `io_reconfigure_newbypass.py` | §3.1, SIGSEGV ×4 builds |
| `io_buffered_reentrant_detach.py` | round 1; `close`, `closed_getattr`, seeded `detach` |
| `io_buffered_reentrant_detach2.py` | round 2, with the `flush()` override (§3.2) |
| `io_initbypass_residual.py` | `:788`, `:818`, the nldecoder FT race |
| `io_buffered_scanner_gaps.py` | §4.1, §4.2, and the `:489` guarded-twin control |
| `io_textio_seek_null_decoder.py` | §4.3, both routes + 3 controls |
| `run_scenarios_initbypass.sh` | per-scenario runner: debug-gil / release-gil / `_pyio` oracle |
| `run_confirmed_matrix.sh` | the full 12-site × 6-interpreter matrix in §Headline |

`PYIO=1` selects the `_pyio` oracle in every scenario file.
