# ft-race-scanner — slice `mod-io` (INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (2026-07-15)
**Scope:** the 7 files in `preflight/slice_files.txt`. `winconsoleio.c` not touched.
**Scanner input:** `scanners/scan_ft_races.sample.json` — 12 findings / 289 functions / 7 files.
**Build provenance:** all seven files are **byte-identical** between the build-matrix commit
`a1d580430c8` and the target `4f3be1b5777` (`git diff --quiet` clean for each). Every line
number below is valid on both.

---

## Headline

The 10 `guarded_writer_unguarded_reader` findings are not 10 problems. **Seven of them
(`bufferedio.c:800, 846, 847, 849, 1580, 1927, 1928`) are one root cause**, and it is a
reproduced memory-safety bug:

> `_io.BufferedReader/BufferedWriter/BufferedRandom.__init__` is the only entry point of
> those types that takes **neither** the clinic critical section **nor** `ENTER_BUFFERED`,
> and it frees `self->buffer`, frees `self->lock`, and drops `self->raw` while other
> threads are inside both.

Measured: **17/17 crashes** on the two free-threaded builds, **0/16** on the two GIL builds.
Three distinct faces captured — a `PyMem` double free, a mimalloc double free, and a
SIGSEGV on a `0xdddddddddddddddd`-poisoned `self->raw`.

The same `__init__`-guard hole is present in `_io.StringIO` (5/5 FT, 0/5 GIL, corrupted
`str`) and in `_io.FileIO` (12/12 crashes **including on the default GIL build**). The
guarded twins are in the same package: `_io.BytesIO.__init__` and
`_io.TextIOWrapper.__init__` both carry `@critical_section`, and both survive the identical
harness 0/10.

Separately, one **incomplete-fix residual**: gh-144777 (`8db8fc9b510`, Feb 2026) added
`@critical_section` to four `IncrementalNewlineDecoder` methods and left the fifth accessor
— the hand-written `newlines` getter — unguarded. TSan reproduces it against all four
sibling writers. That is scanner finding #9, and the scanner named the right twin.

---

## Findings

### [FIX] `_io.Buffered*.__init__` mutates a live shared object outside both locks — `Modules/_io/bufferedio.c:1584, 1932, 2473` (root of scanner findings at :800, :846, :847, :849, :1580, :1927, :1928)

**What.** `bufferedio.c` protects the `buffered` struct with two independent mechanisms:
25 `@critical_section` clinic directives (→ 25 `Py_BEGIN_CRITICAL_SECTION` in
`clinic/bufferedio.c.h`) and 12 `ENTER_BUFFERED` / 13 `LEAVE_BUFFERED` spans. The four
`__init__` impls take **neither**:

```
_io_BufferedReader___init___impl   bufferedio.c:1592   clinic block has no @critical_section
_io_BufferedWriter___init___impl   bufferedio.c:1944   "
_io_BufferedRandom___init___impl   bufferedio.c:2485   "
_io_BufferedRWPair___init___impl   bufferedio.c:2274   "
```
(verified mechanically: `awk '/^_io_BufferedReader___init__\(/,/^}/' clinic/bufferedio.c.h |
grep -c Py_BEGIN_CRITICAL_SECTION` → `0`, same for the other three.)

What they then do to a still-live object:

```c
/* _io_BufferedReader___init___impl */
1596    self->ok = 0;                            /* invalidates every CHECK_INITIALIZED */
1604    Py_XSETREF(self->raw, Py_NewRef(raw));   /* drops the old raw */
1605    self->buffer_size = buffer_size;
1609    if (_buffered_init(self) < 0)
1611    _bufferedreader_reset_buf(self);

/* _buffered_init, bufferedio.c:838 */
 846    if (self->buffer)
 847        PyMem_Free(self->buffer);            /* frees the buffer a reader is memcpy'ing */
 848    self->buffer = PyMem_Malloc(self->buffer_size);
 853    if (self->lock)
 854        PyThread_free_lock(self->lock);      /* frees the lock a thread is HOLDING */
 855    self->lock = PyThread_allocate_lock();
 860    self->owner = 0;
 869    if (_buffered_raw_tell(self) == -1)      /* -> :800 self->abs_pos = n */
```

Every one of the seven scanner sites is reached from here:
`_buffered_raw_tell:800` ← `_buffered_init:869`; `_buffered_init:846/847/849` directly;
`_bufferedreader_reset_buf:1580` ← `:1611` / `:2511`;
`_bufferedwriter_reset_buf:1927/1928` ← `:1964` / `:2512`.

**Guarded twin.** Two, in the same package, both `@critical_section` on `__init__`:
`_io.BytesIO.__init__` (`bytesio.c:1116`, whose body even carries the comment
`/* In case, __init__ is called multiple times. */`) and `_io.TextIOWrapper.__init__`
(`textio.c:1127`). Two of nine `_io` types guard `__init__`; the seven that don't are
exactly the ones that crash.

**What I measured.**

| build | scenario | crashes |
|---|---|---|
| `debug-ft-nojit` | reinit_read / reinit_close / reinit_reinit | **9/9** |
| `release-ft-nojit` | reinit_read / reinit_close | **8/8** |
| `debug-gil-nojit` | reinit_read / reinit_close | 0/8 |
| `release-gil-nojit` | reinit_read / reinit_close | 0/8 |

Three faces, all from `repro/ftrace_buffered_reinit_uaf.py` on `debug-ft-nojit`:

1. **Double free of `self->buffer`** — `gdb` frame `#11 _buffered_init … bufferedio.c:847`
   under `Fatal Python error: _PyMem_DebugRawFree: bad ID: Allocated using API '%c',
   verified using API '%c'` (SIGABRT).
2. **Same, seen by the allocator** — `mimalloc: error: thread …: double free detected of
   block 0x200041a0000 with size 10240`.
3. **UAF on `self->raw`** (SIGSEGV) —
   `#0 PyType_IsSubtype (a=0xdddddddddddddddd, …)` ← `descr_check` ← `getset_get` ←
   `PyObject_GetAttr` ← `#6 buffered_closed … bufferedio.c:526` ←
   `#7 _io__Buffered_read_impl … bufferedio.c:1004` (`CHECK_CLOSED` → `IS_CLOSED` →
   `buffered_closed`). `0xdd` is the debug allocator's free-fill, so `self->raw` was freed
   by the concurrent `Py_XSETREF` at `:1604`.

TSan (`repro/ftrace_io_scenarios.py buffered_iternext` / `bufferedwriter`,
`debug-ft-nojit-tsan`, rc=134 and rc=66) independently pairs the same sites:

```
_io_BufferedReader___init___impl:1604 | buffered_closed:526
_io_BufferedReader___init___impl:1605 | _bufferedreader_fill_buffer:1676
_io_BufferedReader___init___impl:1606 | _bufferedreader_fill_buffer:1680
_io_BufferedReader___init___impl:1613 | _buffered_readline:1213
_buffered_init:848                    | _buffered_readline:1213
_bufferedreader_reset_buf:1580        | _buffered_readline:1218
_buffered_raw_tell:800                | _bufferedreader_raw_read:1667
_buffered_init:860 (self->owner = 0)  | _enter_buffered_busy:299
```
— i.e. TSan confirms the scanner's `:800`, `:846-849`, `:1580` sites specifically.

**A stale written premise makes it worse.** `_buffered_readline` — one of the two functions
TSan pairs against `__init__` — deliberately runs its fast path *outside* `ENTER_BUFFERED`
(which it only takes at `:1236`) on the strength of this comment:

```c
1215    /* First, try to find a line in the buffer. This can run unlocked because
1216       the calls to the C API are simple enough that they can't trigger
1217       any thread switch. */
1218    n = Py_SAFE_DOWNCAST(READAHEAD(self), Py_off_t, Py_ssize_t);
1221    start = self->buffer + self->pos;
1222    s = memchr(start, '\n', n);
```

"can't trigger any thread switch" is a **GIL-era claim**: on a free-threaded build there is
no switch to trigger, because the other thread is already running. This is the briefing's
*"guarded twins are twin for a specific threat model"* trap, same class as CPY-0083 and
CPY-0154 — a comment that correctly addresses re-entrancy and is read as addressing
concurrency. `:1221` is exactly the memcpy source `_buffered_init:847` frees.

**What I did not measure.** I did not observe the `PyThread_free_lock(self->lock)` face in
isolation; the buffer and `raw` faces fire first. That path is read from the source, not
measured — treat it as a hypothesis, falsifiable by making `_buffered_init` skip the buffer
free and re-running.

**Fix.** Add `@critical_section` to the four `_io.Buffered*.__init__` clinic blocks —
matching `_io.BytesIO.__init__` and `_io.TextIOWrapper.__init__`. A critical section alone
does not exclude an in-flight `ENTER_BUFFERED` holder, so `_buffered_init` must also take
`ENTER_BUFFERED` (or the four impls must) before touching `self->buffer` / `self->lock`.

**Prior art.** None. `gh api search/issues` for `BufferedReader __init__ critical section
free-threading`, `_buffered_init data race`, `bufferedio critical_section __init__` → 0
hits. The nearest is the umbrella gh-149816 ("22 free-threading race conditions", open),
which does not contain this. Not in `cpython-tsan-findings` either (TSAN-0032 is the
`detach`/`iternext` face, a different root).

---

### [FIX] `_io.StringIO.__init__` — same hole, corrupted `str` handed to Python (`Modules/_io/stringio.c:673`)

`_io_StringIO___init___impl` (`stringio.c:684`) has no `@critical_section` while the other
16 `_io.StringIO` entry points do. On re-init it does, with nothing held:

```c
 723    self->ok = 0;
 725    PyUnicodeWriter_Discard(self->writer);
 726    self->writer = NULL;
 727    Py_CLEAR(self->readnl);
 728    Py_CLEAR(self->writenl);
 729    Py_CLEAR(self->decoder);
 770    if (resize_buffer(self, 0) < 0)          /* reallocs self->buf out from under a reader */
 773    self->pos = 0;
```
against a concurrent `_io_StringIO_read_impl`, which caches
`output = self->buf + self->pos` at `stringio.c:352` and then reads `size` UCS4 code
points out of it at `:354`.

**Measured** (`repro/ftrace_io_reinit_matrix.py stringio`): **5/5 `debug-ft-nojit`,
5/5 `release-ft-nojit`, 0/5 `debug-gil-nojit`**. Two faces:

* `mimalloc: assertion failed: at "Objects/mimalloc/alloc.c":278, mi_page_usable_size_of`
  — heap corruption.
* `Fatal Python error: _PyObject_AssertFailed` at
  `#9 _PyUnicode_CheckConsistency … unicodeobject.c:677` ←
  `#10 _PyUnicode_FromUCS4 … unicodeobject.c:2232` ←
  `#11 _io_StringIO_read_impl … stringio.c:354`.
  **On a release build with `NDEBUG` this assertion is gone and the malformed `str`
  — `maxchar > 0x10ffff` — is returned to Python.** The `release-ft-nojit` run still
  crashed 5/5, so I did not observe the silent-corruption outcome; it is the inference the
  assertion text supports, not something I measured.

Guarded twin: same as above — `_io.BytesIO.__init__` / `_io.TextIOWrapper.__init__`.
Fix: `@critical_section` on `_io.StringIO.__init__`.

---

### [FIX] gh-144777 is an incomplete fix: the `newlines` getter is the accessor it missed (`Modules/_io/textio.c:634`, scanner finding #9 at `:644`)

Commit `8db8fc9b510` *"gh-144777: Fix data races in IncrementalNewlineDecoder"* (Sam Gross,
2026-02-23) added `@critical_section` to `decode`, `getstate`, `setstate` and `reset`. It
did **not** touch `incrementalnewlinedecoder_newlines_get` (`textio.c:634`), which is a
**hand-written getset getter** — so it cannot inherit the clinic guard, and it takes none.
It reads `self->seennl` at `:644`.

`seennl` is a 3-bit bitfield sharing one storage unit with `pendingcr:1` and `translate:1`
(`textio.c:224-226`), so the guarded writers are read-modify-writes of the same byte:

```
:365  self->pendingcr = 0;      (decode)
:380  self->pendingcr = 1;      (decode)
:511  self->seennl |= seennl;   (decode)
:631  self->pendingcr = 0;      (reset)
```

**Measured** (`repro/ftrace_nldecoder_newlines_residual.py`, `debug-ft-nojit-tsan`,
`TSAN_OPTIONS=exitcode=66`): **rc=66, 4 data races, 1/1 run.** All four have the same
reader and the four distinct writers above:

```
SUMMARY: ThreadSanitizer: data race Modules/_io/textio.c:644:19 in incrementalnewlinedecoder_newlines_get   (×4)
  Read of size 1  … incrementalnewlinedecoder_newlines_get textio.c:644:19
  Previous write of size 1 … _PyIncrementalNewlineDecoder_decode textio.c:365 / :380 / :511
  Previous write of size 1 … _io_IncrementalNewlineDecoder_reset_impl textio.c:631
```
(TSan reporting size **1** for both confirms the bitfield packing.)

Value-benign — a stale or intermediate `.newlines` report; no memory unsafety, and the
re-init harness for this type is clean 0/5 on all three builds.

**Guarded twin:** the four siblings the same commit fixed. **Fix:** one line — give the
getter `@critical_section` (convert it to a clinic `@critical_section` + `@getter`, as
`_io.TextIOWrapper.name` already is at `textio.c:3316-3318`), or make the byte atomic.

**Prior art:** gh-144777 (CLOSED 2026-02-23) and `cpython-tsan-findings` **TSAN-0046**,
which records the *pre-fix* state (`reset:630` vs `newlines_get:644`). The catalogue entry
needs updating: the fix landed, and this residual is what survived it.

---

### [FIX] `bytesiobuf_releasebuffer` is the one export-lifecycle op with no critical section — `Modules/_io/bytesio.c:1311` (task (d): the memory-ordering answer)

`bytesio`'s `exports` counter is read through `FT_ATOMIC_LOAD_SSIZE_RELAXED` at 12 sites and
gates every resize. I was asked whether RELAXED is sufficient and whether check-then-resize
is atomic. Both halves, answered:

**Check-then-resize IS atomic against new exports, and the relaxed load is fine for that
direction.** The increment `FT_ATOMIC_ADD_SSIZE(b->exports, 1)` (`:1291`) runs *inside*
`Py_BEGIN_CRITICAL_SECTION(source)` opened by `bytesiobuf_getbuffer:1304`, and every
gating read (`check_exports:59`, plus `:416 :478 :1080 :1130 :1196 :1282`) is reached only
from a `@critical_section` impl or a `*_lock_held` helper on the same `bytesio`. The
critical section — not the atomic — supplies the mutual exclusion, so a `getbuffer` cannot
land between a `check_exports() == 0` and the following `resize_buffer_lock_held`.

**RELAXED is NOT sufficient on the release side, and that is a real defect.**
`bytesiobuf_releasebuffer` (`:1311-1317`) takes **no critical section at all** — it is the
only export-lifecycle operation that doesn't, and its `getbuffer` twin ten lines above does:

```c
static void
bytesiobuf_releasebuffer(PyObject *op, Py_buffer *Py_UNUSED(view))
{
    bytesiobuf *obj = bytesiobuf_CAST(op);
    bytesio *b = bytesio_CAST(obj->source);
    FT_ATOMIC_ADD_SSIZE(b->exports, -1);        /* :1316 — nothing else */
}
```

`FT_ATOMIC_ADD_SSIZE` is `__atomic_fetch_add(…, __ATOMIC_SEQ_CST)`
(`pyatomic_gcc.h:62-63`), but the gating side is a **relaxed load**, which does not
`acquire`, so there is no *synchronizes-with* edge between "the exporter's last write
through the view" and "the resizer observing `exports == 0`". Nothing else supplies one
either, because the releaser never touches the `bytesio`'s mutex.

**Measured** (`repro/ftrace_io_scenarios.py bytesio_exports`, `debug-ft-nojit-tsan`,
rc=66, **5 data races**):

```
  Read of size 8  … __tsan_memcpy
    #1 unshare_buffer_lock_held  Modules/_io/bytesio.c:169:5
    #2 write_bytes_lock_held     Modules/_io/bytesio.c:264:13
    #3 _io_BytesIO_write_impl    Modules/_io/bytesio.c:871:20
  Previous write of size 1 … __tsan_memcpy
    #1 copy_base  Objects/memoryobject.c:353:13
    #2 copy_single Objects/memoryobject.c:423:5
    #3 memory_ass_sub Objects/memoryobject.c:2794:15
```
i.e. a race **on the buffer bytes themselves** between a resizing `write()` and a write
through a `getbuffer()` memoryview. 2 more of the 5 are the same shape via
`PyBytes_FromStringAndSize`.

**What I did not measure:** no corruption or crash — TSan-only. On x86-64 the relaxed load
is a plain coherent load and the seq-cst RMW is a full fence, so an observable wrong value
is unlikely there; the defect is the missing formal edge, which weakly-ordered targets do
not forgive. Severity: medium.

**Fix:** wrap `bytesiobuf_releasebuffer`'s body in
`Py_BEGIN_CRITICAL_SECTION(obj->source)` — copying its `bytesiobuf_getbuffer` twin exactly
— or make the decrement a release store and every gating load
`FT_ATOMIC_LOAD_SSIZE_ACQUIRE`. Taking the section is preferable: it also removes the
counter's dependence on load ordering entirely.

Handoff to the memory-pattern agent per the brief: I own the ordering; the
`bytesio_dealloc:1080` "`exports > 0` → `SystemError` → `Py_CLEAR(self->buf)` anyway" branch
and the `bytesio_clear:1196` GC path are the use-after-free angle and are yours. Note that
the `bytesiobuf → source` strong reference makes the dealloc branch hard to reach.

---

### [FIX] `FileIO.__init__` NULL-derefs `self->stat_atopen` after a concurrent `close()` — on the **default GIL build** (`Modules/_io/fileio.c:501`; scanner finding at `:532`)

`Modules/_io/fileio.c` has **zero** critical sections — 0 `@critical_section` directives and
0 `Py_BEGIN_CRITICAL_SECTION` in `clinic/fileio.c.h`. `_io.FileIO` is an entirely
unsynchronised type.

The sharp face is not the `int fd` (that is gh-151707, below) but the `PyMem` block:

```c
 470    PyMem_Free(self->stat_atopen);
 471    self->stat_atopen = PyMem_New(struct _Py_stat_struct, 1);
 476    Py_BEGIN_ALLOW_THREADS                                     /* <-- GIL RELEASED */
 477    fstat_result = _Py_fstat_noraise(self->fd, self->stat_atopen);
 478    Py_END_ALLOW_THREADS
 ...
 501        if (S_ISDIR(self->stat_atopen->st_mode)) {             /* <-- deref */
```
against
```c
/* internal_close, fileio.c:121 */
 138    PyMem_Free(self->stat_atopen);
 139    self->stat_atopen = NULL;
```

Because the window is an **explicit `Py_BEGIN_ALLOW_THREADS`**, this is reachable with the
GIL — it is not a free-threading-only bug.

**Measured** (`repro/ftrace_fileio_stat_atopen_uaf.py`): **4/4 `debug-gil-nojit`,
4/4 `release-gil-nojit`, 4/4 `debug-ft-nojit`** — 12/12, all SIGSEGV. Localized on the
**GIL ASan** build (per brief lesson 6):

```
==3188942==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000018 … READ
    #0 _io_FileIO___init___impl  ./Modules/_io/fileio.c:501:13
    #1 _io_FileIO___init__       ./Modules/_io/clinic/fileio.c.h:140:20
    #2 wrap_init                 Objects/typeobject.c:10407:9
```
Address `0x18` = `NULL + offsetof(struct _Py_stat_struct, st_mode)` — `internal_close`
NULLed the pointer during the `ALLOW_THREADS` window and `__init__` dereferenced it. A
NULL-pointer read, not a use-after-free (close NULLs after freeing). `_Py_fstat_noraise`
writing into an already-freed block at `:477` is the UAF sibling of the same window; I did
not isolate it.

TSan on the same object (`ftrace_io_scenarios.py fileio_fd`, rc=66, 16 races) pairs
`internal_close:128/:138/:139` against `_io_FileIO___init___impl:278/:418` and
`_io_FileIO_fileno_impl:612`.

**Prior art.** gh-151707 *"Race condition in `FileIO` under free-threading"* — **OPEN** —
and `cpython-tsan-findings` **TSAN-0024**. Both are scoped to `self->fd` (`read`/`write`/
`lseek` vs `internal_close`); neither mentions `stat_atopen`, and neither claims a GIL-build
SIGSEGV. This site is a **new sibling** of an open issue and belongs on it.

**On the scanner's own claim.** The finding is `publish_before_init_complete` with
`publish_api: "PyObject_SetAttr"`, `publish_line: 514`, `line: 532`. The **site is right,
the mechanism is wrong**: `PyObject_SetAttr((PyObject *)self, &_Py_ID(name), nameobj)` sets
an attribute *on* `self`; it does not publish `self` anywhere. `self` was already reachable
before `__init__` ran. And `:532` (`self->fd = -1;`) is on the *error* path, which is not
the interesting write. See the toolkit section.

---

### [CONFIRM — known, still live at this ref] `stringio_iternext` bypasses the critical section every sibling takes (`Modules/_io/stringio.c:410`)

TSAN-0007 / **gh-153296** *"Use-after-free with a concurrent `stringio_iternext`"*.
`stringio_iternext` calls `_stringio_readline(self, -1)` with no critical section while all
16 `_io.StringIO` clinic impls are `@critical_section`. Also note the history: a fix was
merged (`e6c3039cb39`, gh-135410) and then **reverted** (`73431356d32`, gh-135439).

Confirmed live at `4f3be1b5777`, and my run **escalates the recorded severity**. TSAN-0007's
`meta.json` says *"low as observed (value-benign …), medium latent"*. Measured
(`repro/ftrace_io_scenarios.py stringio_iternext`, `debug-ft-nojit-tsan`): **rc=134, 44 data
races, and a hard abort** —
`Objects/unicodeobject.c:677: _PyUnicode_CheckConsistency: Assertion failed: maxchar <=
0x10ffff` → `Fatal Python error: _PyObject_AssertFailed`. Top pairs:
`_stringio_readline:365/:374/:378/:383` vs `_io_StringIO_seek_impl:543`,
`resize_buffer:122`, `write_str:196/:267/:269`, `_io_StringIO_truncate_impl:487`.

**Already fixed upstream, after this ref.** PR **#153368** merged **2026-07-25**
(`merge_commit_sha bff0051`), ten days after the target commit `4f3be1b5777` (2026-07-15).
No action for CPython. Suggest updating the TSAN-0007 record with the
`_PyUnicode_CheckConsistency` evidence.

### [CONFIRM — known, still live] `buffered_iternext`'s leading `CHECK_INITIALIZED` sits outside its own critical section (`Modules/_io/bufferedio.c:1504`)

TSAN-0032, residual of PR **#150295** (gh-149816 item #84, `e8545ed3eaf`), which added the
critical section at `:1512` and left the `self->ok` read at `:1504` outside it. Reproduced
again here — TSan pair
`_io__Buffered_detach_impl:628 | buffered_iternext:1504`
in `ftrace_io_scenarios.py buffered_iternext`. One line, unchanged since the catalogue entry.
Confirmed, not re-litigated.

---

### [CONSIDER] `_enter_buffered_busy` reads `self->owner` before acquiring the lock (`Modules/_io/bufferedio.c:299`) — task (e)

```c
#define ENTER_BUFFERED(self) \
    ( (PyThread_acquire_lock(self->lock, 0) ? 1 : _enter_buffered_busy(self)) \
     && (self->owner = PyThread_get_thread_ident(), 1) )
#define LEAVE_BUFFERED(self) \
    do { self->owner = 0; PyThread_release_lock(self->lock); } while(0);

static int _enter_buffered_busy(buffered *self) {
    if (self->owner == PyThread_get_thread_ident()) {   /* :299 — no lock held */
```
`owner` is `volatile unsigned long` (`:258`). `volatile` is not an atomic; TSan reports it.

**The self-reentrancy logic is sound in isolation.** `owner` only ever holds thread T's id
while T holds the lock, and `LEAVE_BUFFERED` zeroes it *before* releasing, so a thread can
never read its own stale id and take the bogus `"reentrant call inside %R"` branch. Reading
another thread's id, or `0`, both fall through to the blocking acquire. `unsigned long` is
aligned and does not tear on LP64. So: value-benign.

**What TSan actually caught** in 6 scenarios × 3 s is *not* the `ENTER`/`LEAVE` pair — it is:

```
SUMMARY: ThreadSanitizer: data race Modules/_io/bufferedio.c:299:15 in _enter_buffered_busy
  Read of size 8   … _enter_buffered_busy:299   ← _io_BufferedWriter_write_impl:2097
  Previous write   … _buffered_init:860 (self->owner = 0)  ← _io_BufferedWriter___init___impl:1962
```
i.e. the **same `__init__` root cause** as the FIX above, not an independent defect. I did
**not** observe the `LEAVE_BUFFERED` pairing.

Verdict: CONSIDER, low. Annotate with `_Py_atomic_load_ulong_relaxed` /
`_Py_atomic_store_ulong_relaxed` for TSan cleanliness; it is not independently exploitable,
and once `__init__` takes the lock the reported pair disappears.

### [CONSIDER — out of my class, handing off] `TextIOWrapper.__init__` crashes 5/5 on the **GIL** build and 0/5 under free-threading

The `@critical_section` on `_io.TextIOWrapper.__init__` (`textio.c:1127`) makes the
concurrent case safe (0/5 `debug-ft-nojit`, 0/5 `release-ft-nojit`) but compiles to nothing
on the GIL build, where `__init__` calling out to `buffer.readable()` / `.seekable()` /
codec lookup yields the interpreter and a second `__init__` tears down `self->encoder` /
`self->decoder` / `self->buffer` under the first frame. **5/5 SIGSEGV/SIGABRT on
`debug-gil-nojit`** (`repro/ftrace_io_reinit_matrix.py textiowrapper`).

This is the §3 re-entrancy class, not mine, and I did not localize it to a line — reported
as a measurement only. It is a live instance of the briefing's *"guarded twins are twin for
a specific threat model"* warning: the critical section fixes concurrency and does nothing
for re-entrancy.

### [CONSIDER] `_io_BufferedRWPair___init___impl` leaks on re-init (`Modules/_io/bufferedio.c:2286, :2292`)

`self->reader = (buffered *) PyObject_CallFunction(...)` / `self->writer = ...` are plain
assignments over live pointers with no `Py_XSETREF`, so a second `__init__` leaks both
sub-objects. Refcount, not a race — refcount-auditor's. Measured 0/5 on all three builds,
and the leak is *why*: nothing is freed, so nothing dangles. A `Py_XSETREF` fix here would
turn this row of the matrix from green to red unless `__init__` is guarded at the same
time — worth saying out loud before someone "fixes" the leak in isolation.

---

### [ACCEPTABLE — false positive] `utf16_encode:826` / `utf32_encode:855` read `self->encoding_start_of_stream` (scanner findings #10, #11)

Both are `encodefunc_t` leaves. `self->encodefunc` is called from exactly **one** site,
`textio.c:1778` inside `_io_TextIOWrapper_write_impl`, which **is** `@critical_section`
(`textio.c:1722`) — and which writes the same field two lines later at `:1780`. Every other
accessor of `encoding_start_of_stream` (`:1064` in `_textiowrapper_fix_encoder_state`,
reached from `__init__:1321` and `reconfigure:1414`; `:1320`; `:2565/:2570` in
`_textiowrapper_encoder_reset`) is likewise inside a `@critical_section` impl. The field is
uniformly guarded.

This is the briefing's largest FP class ("the caller holds the section, transitively",
named for `Modules/_io/textio.c`), with the specific twist that the missed call edge is
through a **function pointer**. See the toolkit section for the fix.

### [ACCEPTABLE — false positive] `set_newline:930` `lazy_init_no_critical_section` (scanner finding #12)

Two independent dismissals.

1. **It is not a lazy init.** The shape is assign-then-check-then-*roll back*:
   ```c
   PyObject *old = self->readnl;                    /* :924 */
   ...
   self->readnl = PyUnicode_FromString(newline);    /* :929 */
   if (self->readnl == NULL) {                      /* :930 */
       self->readnl = old;                          /* :931 restore, not initialise */
       return -1;
   }
   ```
   `_LAZY_INIT_RE` matches `if (X->Y == NULL) { X->Y = …` and cannot tell a rollback from
   an initialisation.
2. **Both callers hold the lock anyway.** `set_newline` is called only from
   `_io_TextIOWrapper___init___impl:1271` and `_io_TextIOWrapper_reconfigure_impl:1497`,
   and both clinic blocks carry `@critical_section` (`textio.c:1127`, `:1418`).

---

## Classes bounded (with denominators)

| class | denominator | result |
|---|---|---|
| **T3 iterator-exhaustion double-DECREF** | **7** iternext functions (5 `tp_iternext` slots + 2 `*_lock_held` helpers) | **0 — real, and structurally explained.** `iobase_iter` returns `Py_NewRef(self)` (`iobase.c:678-684`), so no `_io` iterator holds an owning reference to a separate container. There is no `it_seq`/`di_dict` analogue to drop, in any of the seven. The T3 candidate set is empty *before* lock coverage is consulted. |
| **T3 variant the rule has no vocabulary for** | same 7 | **2 of 5 slots hit it.** `stringio_iternext` takes no critical section at all; `buffered_iternext`'s leading `CHECK_INITIALIZED` is outside its own. Both are catalogued CPython bugs (gh-153296, gh-149816#84). See recall gap R1. |
| **T2 lazy-init** | 1 candidate | 1 FP (`set_newline`). No `lazy_init_partial_guard` anywhere in the slice — I looked: no `_io` field is lazily computed-and-cached by two accessors. Real negative, small denominator. |
| **T1 guarded/unguarded field asymmetry** | 10 candidates over 289 functions | 7 TP (one root), 1 TP (`newlines_get`), 2 FP (`utf16/utf32_encode`). **Precision 8/10 = 80 %**, and the one root cause behind 7 of them is a reproduced crash. |
| **T4 publish-before-init** | 1 candidate | Right site, wrong mechanism; the underlying defect is real and is gh-151707 + a new sibling. Counted as 1 TP-by-site / 0 TP-by-mechanism. |
| `ENTER_BUFFERED` span balance | 12 `ENTER_BUFFERED` / 13 `LEAVE_BUFFERED` | Spot-checked, **not** exhaustively audited — this is `scan_lock_discipline`'s structural blind spot per brief §4a and I did not claim it. The count asymmetry is explained by `_io__Buffered_read_impl` taking `ENTER_BUFFERED` on two mutually exclusive branches (`:1008`, `:1017`) against one `LEAVE` at `:1022`, and `:1059→:1067/:1076/:1082` having three exits. Handing to the lock-discipline pass. |
| `Modules/_io/iobase.c` | struct is `{ PyObject_HEAD; PyObject *dict; PyObject *weakreflist; }` | **Nothing to check.** No mutable C-level state. Structural zero, correctly so. |
| `Modules/_io/_iomodule.c` | 0 critical sections, all state in `_PyIO_State` module state | No per-object shared mutable state; the `open()` factory allocates fresh objects. No findings. |

**Per-file `@critical_section` census** (the fact that made the `__init__` asymmetry
visible — worth carrying forward):

| file | `@critical_section` in `.c` | `Py_BEGIN_CRITICAL_SECTION` emitted in `clinic/*.c.h` |
|---|---|---|
| `textio.c` | 26 | 26 |
| `bufferedio.c` | 25 | 25 |
| `bytesio.c` | 20 | 20 |
| `stringio.c` | 16 | 16 |
| `fileio.c` | **0** | **0** |
| `iobase.c` | 0 | 0 |
| `_iomodule.c` | 0 | 0 |

(Count the emitted sections with `grep -c "^ *Py_BEGIN_CRITICAL_SECTION"` — a bare
`grep -c Py_BEGIN_CRITICAL_SECTION` is off by one on every file, matching the
`#include "pycore_critical_section.h"// Py_BEGIN_CRITICAL_SECTION()` comment that Argument
Clinic emits at `clinic/*.c.h:10`.)

**`__init__`-guard matrix** — the single most useful artefact of this pass:

One process per cell, `repro/ftrace_io_reinit_matrix.py <type> 2`, 5 runs, crash = exit ≥ 128.

| type | `__init__` `@critical_section`? | `debug-ft` | `release-ft` | `debug-gil` |
|---|---|---|---|---|
| `_io.BytesIO` (`bytesio.c:1116`) | **yes** | 0/5 | 0/5 | 0/5 |
| `_io.TextIOWrapper` (`textio.c:1127`) | **yes** | 0/5 | 0/5 | **5/5** ← re-entrancy, not a race |
| `_io.StringIO` (`stringio.c:673`) | no | **5/5** | **5/5** | 0/5 |
| `_io.IncrementalNewlineDecoder` (`textio.c:232`) | no | 0/5 | 0/5 | 0/5 |
| `_io.BufferedReader` (`bufferedio.c:1584`) | no | **5/5** | **5/5** | 0/5 |
| `_io.BufferedWriter` (`bufferedio.c:1932`) | no | **5/5** | **3/5** | 0/5 |
| `_io.BufferedRandom` (`bufferedio.c:2473`) | no | **5/5** | **4/5** | 0/5 |
| `_io.BufferedRWPair` (`bufferedio.c:2256`) | no | 0/5 | 0/5 | 0/5 |
| `_io.FileIO` (`fileio.c:225`) | no | **5/5** | **5/5** | **5/5** |

Reading: **2 of 9 `_io` types guard `__init__`; neither of those two crashes under
free-threading. 5 of the 7 unguarded ones do.** The two unguarded non-crashers are
explained rather than lucky — `IncrementalNewlineDecoder.__init__` frees no heap (it only
`Py_XSETREF`s `decoder`/`errors` and resets the bitfield), and
`BufferedRWPair.__init__` *leaks* its old sub-objects instead of freeing them (see the
CONSIDER below), so there is nothing to dangle. `release-ft` scores below 5/5 for
`bufwriter`/`bufrandom` because that build has no debug allocator to catch the double free
at the point of the free; the crash then depends on the corruption landing somewhere fatal.

---

## Toolkit assessment

### Verdict on the new `_lock_coverage()` per-span logic — asked for explicitly

**It behaved correctly, but this slice barely exercised it, and it is blind to the lock that
actually matters here.** Three separate statements, measured:

**1. The span computation is right where it applies.** The only function in the slice with a
`Py_BEGIN_CRITICAL_SECTION` inside a `tp_iternext` is `buffered_iternext`. Driving
`_lock_coverage` directly:

```
_lock_coverage(buffered_iternext body) -> spans=[(387, 497)], opaque=False
  offset of "CHECK_INITIALIZED(self);"   = 121  -> in span? False
  offset of "_buffered_readline(self,-1)" = 435 -> in span? True
```
That boundary is **exactly** the TSAN-0032 boundary (`:1504` outside, `:1512-1514` inside).
Had this function contained a T3-shaped drop before its section, the new logic would have
surfaced it and the old blanket "function mentions a lock → suppress" would not. This is a
clean positive test of the shipped fix.

**2. It did not change the outcome here, because there were zero T3 candidates.**
`_check_t3` consults coverage only after `_iter_drop_candidates` matches. In `_io` that set
is empty for structural reasons (self-iterating iterators, see the denominator table). The
slice count went 11 → 12 for T1/T2 reasons, not T3 ones. Do not read this slice as
validating the fix on a corpus — read statement 1 as validating it on one function.

**3. The residual hazard the fix did not remove: `opaque ⇒ whole-function suppression`.**
`_LOCK_MACRO_RE = r"\b[A-Z][A-Z0-9_]*LOCK[A-Z0-9_]*\("` requires the literal substring
`LOCK`. `ENTER_BUFFERED(` / `LEAVE_BUFFERED(` do **not** contain it, so on `bufferedio.c`:

```
_lock_coverage(_io__Buffered_close_impl) -> opaque=False, spans=0
```
— a function that holds **both** the clinic critical section and `ENTER_BUFFERED` for its
entire body is reported as completely unlocked. Two consequences pulling in opposite
directions:

* Today, the scanner gets the right answer by accident: the guarded-twin set for
  `bufferedio.c` comes entirely from `_clinic_guarded_functions` +
  `_caller_propagated_guards`, and `_lock_coverage` contributes nothing.
* Had CPython spelled the macro `ACQUIRE_BUFFERED_LOCK(`, `_lock_coverage` would have
  returned `opaque=True` and `_check_t3` would have suppressed **the whole of
  `buffered_iternext`** — re-creating precisely the bug the fix was written to remove, on
  the function that is a live catalogued race (TSAN-0032). The fix removed the blanket
  suppression for `Py_BEGIN_CRITICAL_SECTION` and left it intact for the macro and
  `PyMutex` families. The vulnerability is a rename away.

**Proposal T-1 (high value).** Learn `#define`d lock macros from the file being scanned
instead of matching on the name. Any object-like or function-like macro whose body contains
`PyThread_acquire_lock` / `PyMutex_Lock` / `Py_BEGIN_CRITICAL_SECTION` is an acquire; one
containing `PyThread_release_lock` / `PyMutex_Unlock` / `Py_END_CRITICAL_SECTION` is a
release. That makes `ENTER_BUFFERED`/`LEAVE_BUFFERED` delimitable, converts
`bufferedio.c`'s 12 spans from invisible to `cs_spans`, and drops the `opaque` fallback for
the commonest case. Denominator for this: `bufferedio.c` alone, 12 acquire sites the scanner
currently cannot see.

**Proposal T-2.** Where a lock genuinely is opaque, prefer "report at low confidence with
`lock_is_opaque: true`" over silent whole-function suppression, and put the count in the
envelope (`functions_suppressed_opaque_lock`) so a reader can see how much of the file was
skipped. A suppression with no denominator is exactly the structural zero the campaign
warns about.

### Recall gaps found by reading

**R1 — no rule for "the hand-written slot is the only unguarded entry point of an otherwise
clinic-guarded type."** This is *the* `_io` bug shape and the scanner has no vocabulary for
it. Two live instances in this slice, both already catalogued CPython bugs:

* `stringio_iternext` (`stringio.c:410`) — 16 `@critical_section` impls, slot takes none →
  gh-153296, TSAN-0007. 44 TSan races + `_PyUnicode_CheckConsistency` abort.
* `buffered_iternext` (`bufferedio.c:1498`) — `CHECK_INITIALIZED` outside the section →
  gh-149816#84, TSAN-0032.

Both are invisible because T3 only looks for an owning-ref *drop*. Proposed rule
**T5 `unguarded_slot_in_guarded_type`**: for each file, compute
`clinic_guarded_impls / total_public_entry_points`; if that ratio is ≥ some threshold
(0.5 works here: 16/16, 25/25) and a hand-written `tp_*` slot function reads a `self->`
field with neither a critical section nor a `*_lock_held` name, emit at **high** confidence.
On this slice it would fire on `stringio_iternext` and `buffered_iternext` and on nothing
else. Also flag when the slot *has* a section but a `self->` read precedes it — that is the
TSAN-0032 sub-shape and it is one `_offset_in_spans` call, which the new `_lock_coverage`
already provides.

**R2 — `__init__` is not modelled as a shared-object mutator.** The single highest-value
finding of this pass is a `tp_init` that frees live heap outside every lock, and the scanner
reached it only sideways, as 7 unattributed `guarded_writer_unguarded_reader` sites in
callee helpers. Worse, the `_INITIALIZER_NAME_RE` suppression
(`(?:^|_)(?:init\w*|new|alloc\w*|create|make)(?:_|$)`) exists precisely to treat
`init`-named functions as pre-publication — the opposite of the truth for a `tp_init` slot,
which by definition runs on an object Python already holds and can call twice.

Proposed rule **T6 `reinit_without_critical_section`**: a function wired as `Py_tp_init`
(or named `*___init___impl`) that (a) calls a free/clear API on a `self->` field —
`PyMem_Free`, `PyThread_free_lock`, `Py_CLEAR`, `Py_XSETREF`, `PyUnicodeWriter_Discard`,
`PyObject_Free` — and (b) is not clinic-guarded, in a file where ≥ 1 other entry point is.
On this slice that emits exactly the 7 unguarded `__init__`s and skips the 2 guarded ones —
a 7/9 hit rate with the two guarded ones as the built-in control. And it must **override**
`_INITIALIZER_NAME_RE`, which currently argues the other way. This rule is worth a slice of
its own: the same shape is why `_pickle`, `_struct` (CPY-0044/0048/0049) and `socket`
(`sock_initobj_impl | socket_close`, already noted-but-uncatalogued in
`cpython-tsan-findings`) keep producing findings.

**R3 — guard propagation stops at function pointers.** `_caller_propagated_guards` walks
direct call edges, so `utf16_encode` / `utf32_encode` look unguarded although their only
caller is `@critical_section`; the edge is `(*self->encodefunc)(...)` at `textio.c:1778`.
Fix: when a `static const` table in the same file assigns function pointers to a field
(`encodefuncs[]`, `textio.c:894-905`, names all 9 candidates), treat every table entry as a
call target of every site that invokes that field. Cost: 2 of 10 T1 findings in this slice —
20 points of precision from one edge kind.

**R4 — `publish_before_init_complete`'s publish set includes a non-publishing API.**
`PyObject_SetAttr((PyObject *)self, k, v)` was taken as the point where `self` "becomes
reachable from other threads". It is not: it stores *into* `self`. The publish set should be
restricted to APIs that store the *object under construction* into something else —
`PyDict_SetItem(other, k, self)`, `PyList_Append(other, self)`, `PyModule_AddObject*`,
`PyObject_SetAttr(other, k, self)` — i.e. `self` must appear in a **value** position, not
the receiver position. As written the rule will fire on every C `__init__` that sets an
attribute on itself. (The `fileio.c` finding survives triage anyway, but for a reason the
rule did not state.)

**R5 — `_LAZY_INIT_RE` cannot distinguish a rollback from an initialisation.**
`self->X = compute(); if (self->X == NULL) { self->X = old; return -1; }` matches. One-line
fix: require the RHS of the in-guard assignment to contain a `(` (a call) — a bare
identifier RHS is a restore. No catalogued instance is lost (`ga_getitem`'s is
`_Py_make_parameters(...)`, `descr_get_qualname`'s is `calculate_qualname(...)`).

### Precision summary

| rule | fired | TP | FP | precision |
|---|---|---|---|---|
| `guarded_writer_unguarded_reader` (T1) | 10 | 8 | 2 | **80 %** |
| `publish_before_init_complete` (T4) | 1 | 1 site / 0 mechanism | — | site right, story wrong |
| `lazy_init_no_critical_section` (T2) | 1 | 0 | 1 | 0 % (n=1) |
| **total** | **12** | **9** | **3** | **75 %** |

Reading: the T1 rule earns its keep here — but note that its *value* came from the seven
findings it emitted with low/medium confidence and an incidental guarded twin, whose real
significance (one `tp_init` freeing live heap) only appeared on reading. The rule found the
symptom; T6 would find the disease.

---

## Reproducers

All in `reports/mod-io/repro/`, all `.py` files, all run with an explicit interpreter path.

| file | what it demonstrates |
|---|---|
| `ftrace_buffered_reinit_uaf.py` | the headline: `Buffered*.__init__` double-free / `raw` UAF. 3 scenarios. |
| `ftrace_io_reinit_matrix.py` | the `__init__`-guard asymmetry across all 9 `_io` types, incl. the 2 guarded controls. |
| `ftrace_nldecoder_newlines_residual.py` | gh-144777's residual, TSan, rc=66, 4 races. |
| `ftrace_fileio_stat_atopen_uaf.py` | `fileio.c:501` NULL deref, GIL builds included; ASan-localized. |
| `ftrace_io_scenarios.py` | 6 TSan scenarios: stringio/buffered iternext, fileio fd, bytesio exports, buffered owner, bufferedwriter. |

TSan runs used `TSAN_OPTIONS="exitcode=66 halt_on_error=0 history_size=4"`, no `fork`
isolation (brief lesson: the fork template deadlocks under TSan), one scenario per process.
Every count above is from a process observed to have **exited** — the partial-log trap in
lesson 5 bit once during this pass (a `grep -c` against `stringio_iternext.log` read 0
mid-run and 44 at completion) and is the reason the counts are quoted with their exit codes.
