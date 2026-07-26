# gil-discipline-checker — slice `mod-io`

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** the 7 files in `preflight/slice_files.txt`. `winconsoleio.c` excluded.
**Builds used:** `debug-gil-nojit`, `release-gil-nojit`, `debug-ft-nojit`, `debug-gil-nojit-asan`,
`release-ft-nojit-tsan` — all at `a1d580430c8`.
**Drift check (brief lesson 10):** all seven slice files, plus `Python/pystate.c`,
`Python/critical_section.c` and `Python/fileutils.c`, are **byte-identical** between
`a1d580430c8` and `4f3be1b5777` (`git diff --stat` empty). Every line number below is
valid at both refs.

**Scanner input:** `scan_gil_usage.sample.json` — 0 findings, 289 functions, 16 vocabulary
resolved. The whole-`Modules/` baseline is 68, so the rule is not globally quiet.
**Verdict on the zero: it is REAL** — the constructs are present (8 pairs) and all 8 are
clean. Detail and denominator below. The envelope's `rule_not_applicable: true` next to
those 16 resolved constructs is a **scanner defect**, not a property of `_io`; see
Toolkit assessment T1.

---

## Summary

| | |
|---|---|
| Functions analysed (scanner) | 289 across 7 files |
| `Py_BEGIN/END_ALLOW_THREADS` pairs **verified by hand** | **8 / 8** |
| Additional GIL-released regions reached interprocedurally | 4 (`_Py_read`/`_Py_write`, out of slice) |
| `PyGILState_Ensure` / `Release` | **0 / 0 — structurally absent** |
| `ENTER_BUFFERED` spans audited by hand | **12 / 12** (13 `LEAVE_BUFFERED` sites) |
| Files carrying a raw `PyThread_type_lock` | **1 of 7** (`bufferedio.c`, 7 occurrences; the other six: 0) |
| API call in a GIL-released region | 0 |
| Blocking call with the GIL held | 0 in-slice |
| Mismatched pairs | 0 |
| **New findings** | 1 FIX (+1 confirmed sibling site), 2 CONSIDER, 2 ACCEPTABLE |

---

## Findings

### [FIX] `_buffered_init` frees the buffered lock **and** the buffer out from under a live frame — `Modules/_io/bufferedio.c:847,854`

`_io.BufferedReader.__init__` is callable a second time on a live object. It reaches
`_buffered_init` (`bufferedio.c:838`), which does:

```c
846    if (self->buffer)
847        PyMem_Free(self->buffer);            /* the block a live memoryview points at */
848    self->buffer = PyMem_Malloc(self->buffer_size);
...
853    if (self->lock)
854        PyThread_free_lock(self->lock);      /* the lock the calling frame HOLDS */
855    self->lock = PyThread_allocate_lock();
```

Nothing excludes it:

* `__init__` never goes through `ENTER_BUFFERED`, so the buffered lock does not stop it,
  and the `self->owner == PyThread_get_thread_ident()` re-entrancy guard at `:299` — which
  lives only inside `_enter_buffered_busy` — is never consulted.
* `_io.BufferedReader.__init__` is one of the **eight** clinic blocks in `bufferedio.c` with
  no `@critical_section`, so the free-threaded per-object lock does not stop it either.
  (The other 26 clinic blocks in the file do have it.)
* The window is wide open: `_bufferedreader_fill_buffer:1681` computes `self->buffer + start`
  and hands it to `_bufferedreader_raw_read`, which at `:1628-1629` wraps it in a memoryview
  built with `PyBuffer_FillInfo(&buf, NULL, ...)` — **`obj == NULL`, the file's own comment
  says "the buffer needn't be released as its object is NULL"** — and passes it to user
  `raw.readinto()` at `:1640`. That memoryview pins nothing.

Two distinct memory-safety defects follow from one call.

**Measured — single-threaded, pure Python, no `_testcapi`, no revive-by-address.**
Reproducer: `repro/io_buffered_reinit_frees_lock.py`.

| build | `_io` | `_pyio` twin |
|---|---|---|
| `debug-gil-nojit` | `Fatal Python error: PyMutex_Unlock: unlocking mutex that is not locked` → **rc=134** | returns 64 bytes, rc=0 |
| `release-gil-nojit` | same fatal → **rc=134** | rc=0 |
| `debug-ft-nojit` | same fatal → **rc=134** | rc=0 |
| `debug-gil-nojit-asan` | **heap-use-after-free WRITE of size 1** | — |

ASan, GIL build (per brief lesson 6):

```
ERROR: AddressSanitizer: heap-use-after-free ... WRITE of size 1
  #0 pack_single            Objects/memoryobject.c:1972
  #1 memory_ass_sub         Objects/memoryobject.c:2771
  ...
  #9 _bufferedreader_raw_read      Modules/_io/bufferedio.c:1640
  #10 _bufferedreader_fill_buffer  Modules/_io/bufferedio.c:1681
  #12 _io__Buffered_read_impl      Modules/_io/bufferedio.c:1019
0x... is located 16 bytes inside of 8216-byte region
freed by thread T0 here:
  #1 _buffered_init                     Modules/_io/bufferedio.c:847
  #2 _io_BufferedReader___init___impl   Modules/_io/bufferedio.c:1609
```

The `PyMutex_Unlock` abort is the *second* defect: `LEAVE_BUFFERED` (`:334`) releases
`self->lock`, which by then is the **replacement** lock installed at `:855` and never
acquired by this frame. Its consequence beyond the abort is that mutual exclusion is
gone — the original lock is destroyed while logically held, and the fresh one is
unlocked, so any other thread's `ENTER_BUFFERED` fast path now succeeds and runs the
buffered internals concurrently with the in-flight operation.

**Threaded variant** (`repro/io_buffered_free_lock_under_waiter.py`) frees the lock while a
*second* thread is parked on it inside `_enter_buffered_busy`'s GIL-released region
(`bufferedio.c:308`) — i.e. `PyThread_free_lock` on an address a thread is blocked in
`_PyParkingLot_Park` on. Same fatal error on all three builds, rc=134; `_pyio` completes
both threads (`results=['t1-ok','t2-ok']`, rc=0). The faulthandler dump shows `Thread-2`
still in the stack at the abort.

**Guarded twin — same directory, same concern, explicitly handled.**
`_io_BytesIO___init___impl` (`Modules/_io/bytesio.c`) opens with:

```c
    /* In case, __init__ is called multiple times. */
    self->string_size = 0;
    self->pos = 0;

    if (FT_ATOMIC_LOAD_SSIZE_RELAXED(self->exports) > 0) {
        PyErr_SetString(PyExc_BufferError,
                        "Existing exports of data: object cannot be re-sized");
        return -1;
    }
```

BytesIO's `__init__` names the hazard in a comment *and* refuses to invalidate live
storage. `_buffered_init` does neither. The fix has the same shape: refuse re-init while
the object is in use — i.e. take the buffered lock (or fail with the existing
`"reentrant call inside %R"` error) before touching `self->buffer` / `self->lock`.

**Confirmed sibling site — the writer path.** `_bufferedwriter_raw_write` builds the
identical unpinned memoryview at `bufferedio.c:1983-1984` (`PyBuffer_FillInfo(&buf, NULL,
..., PyBUF_CONTIG_RO)`) and hands it to user `raw.write()` while
`_io_BufferedWriter_write_impl` holds the buffered lock (`ENTER_BUFFERED` at `:2097`).
Reproducer `repro/io_bufferedwriter_reinit_sibling.py`: same
`PyMutex_Unlock: unlocking mutex that is not locked` → rc=134 on `debug-gil-nojit` and
`release-gil-nojit`; `_pyio` raises a clean `OSError` (rc=1). I did **not** get an ASan
report for this one — the ASan build exceeded my 90 s budget on it, so the UAF half of the
writer path is inferred from structural identity, not measured. State it that way.

**Not the seeded lead.** The brief's §3 archetype is *stale state after a user callback*.
This is a different obligation: the *lock object and the buffer* are destroyed, not merely
made stale, and the damage lands in the locking machinery. It happens to be reachable
through the same re-entrancy door.

**Prior art:** none. `gh api -X GET search/issues` over
`_buffered_init reinitialize`, `BufferedReader __init__ twice use-after-free`,
`io __init__ called twice buffer`, `PyMutex_Unlock unlocking mutex that is not locked _io`,
`BufferedReader reinitialize crash`, `reinitialize object __init__ segfault io` returned
nothing relevant.

---

### [CONSIDER] The buffered lock is uninterruptible; `_pyio`'s is not — `Modules/_io/bufferedio.c:308`

`_enter_buffered_busy:308` blocks in `PyThread_acquire_lock(self->lock, 1)`, which is
`PyThread_acquire_lock_timed(lock, -1, /*intr_flag=*/0)`. A signal does not break the wait.
The GIL *is* correctly released around it (`:306-318`), so the rest of the interpreter keeps
running — but the waiting thread never returns to the eval loop, so `KeyboardInterrupt` is
never raised even on the main thread. `_pyio` uses `threading.RLock`, whose `acquire()`
passes `intr_flag=1` on the main thread.

**Measured**, `repro/io_buffered_lock_uninterruptible.py` — a worker holds the lock, the main
thread blocks on it, a helper sends `SIGINT` at t=2 s:

| build | `_io` | `_pyio` |
|---|---|---|
| `debug-gil-nojit` | SIGINT ignored, still blocked at t=5 s (rc=8) | `KeyboardInterrupt` at 2.00 s (rc=0) |
| `release-gil-nojit` | rc=8 | `KeyboardInterrupt` at 2.00 s |
| `debug-ft-nojit` | rc=8 | `KeyboardInterrupt` at 2.00 s |

6 of 6. This is the C-specific half of the deadlock story below: the inversion itself is
shared, the inability to escape it is not.

---

### [CONSIDER] AB-BA lock-order inversion across two buffered objects — `Modules/_io/bufferedio.c:329` / `:308`

`ENTER_BUFFERED` is held across a dispatch into `self->raw`, which for a Python-level raw
object is arbitrary user code. That code may enter a *second* buffered object, so a thread
can hold two `_io` locks in whatever order it visited them. Two threads visiting the same
two objects in opposite orders wedge permanently.

**Measured** — `repro/io_buffered_lock_inversion.py` deadlocks on `debug-gil-nojit`,
`release-gil-nojit` and `debug-ft-nojit` (rc=7 each). Native frames, gdb attach with
`PR_SET_PTRACER_ANY` (yama `ptrace_scope=1` here), both worker threads:

```
#8  _PySemaphore_Wait                  Python/parking_lot.c:174
#9  _PyParkingLot_Park (detach=0)      Python/parking_lot.c:340
#10 _PyMutex_LockTimed (flags=_Py_LOCK_DONT_DETACH)  Python/lock.c:138
#11 _enter_buffered_busy (self=0x74248c975fd0)  ./Modules/_io/bufferedio.c:308
#11 _enter_buffered_busy (self=0x74248ca66bd0)  ./Modules/_io/bufferedio.c:308
```

Two different `self` pointers — a genuine AB-BA, not a self-deadlock. (The same-thread
nesting case is correctly caught: `:299`'s owner check raises
`RuntimeError: reentrant call inside …`.)

**Downgraded from FIX because the differential says so.** `_pyio` deadlocks identically
(rc=7 on all three builds). Per the brief's grading table, "same behaviour / same behaviour"
is not a C-side finding — this is a property of the locking design both implementations
share, and any two-lock scheme with user code in between has it. What is *not* shared is
interruptibility (previous finding). Recording it because it is the answer to task (c) and
because a maintainer weighing the previous finding needs to see the shape it enables.

**The GIL/buffered-lock ordering itself is correct**, and that is worth stating positively:
`ENTER_BUFFERED`'s first act is a *non-blocking* `PyThread_acquire_lock(self->lock, 0)` with
the GIL held, which cannot block; the only blocking acquire is inside
`Py_BEGIN_ALLOW_THREADS`. So the specific hazard task (c) asked about — a thread waiting on
the buffered lock **while holding the GIL**, against a lock-holder waiting for the GIL —
**cannot occur**. I looked for it and it is structurally excluded.

---

### [ACCEPTABLE] `relax_locking` is read before the GIL is dropped and acted on after — `Modules/_io/bufferedio.c:305`

Task (b). `relax_locking = _Py_IsInterpreterFinalizing(interp)` is computed at `:305`, the
GIL is released at `:306`, and the value selects the acquire variant at `:307-317` and gates
the fatal error at `:319`. Both post-release uses are the *same* variable, so they cannot
disagree with each other. The only staleness that matters: if finalization *begins* while
the thread is already inside the untimed `PyThread_acquire_lock(self->lock, 1)`, it cannot
retroactively switch to the 1-second grace path — the very deadlock the branch exists to
prevent. That window is narrow (finalization must start after `:305` and the lock owner must
then be an abruptly-terminated daemon thread) and pre-existing; gh-86883 and gh-129536 are
the observed symptom of this area. Not introduced by anything in this slice. Fixing it would
mean always using the timed acquire and re-checking finalization on timeout.

**`_Py_FatalErrorFormat` is reached with the GIL genuinely held.** Verified: `:318`'s
`Py_END_ALLOW_THREADS` is unconditional straight-line code with no `goto`, `return` or
`break` between it and `:306`, so *both* the acquired and the timed-out paths pass through
`PyEval_RestoreThread` before `:319`. `_Py_FatalErrorFormat` is declared
`_Py_NO_RETURN` (`Include/internal/pycore_pyerrors.h:54`), so no path returns 1 without
holding the lock.

---

### [ACCEPTABLE] `PyObject_ASCII(self)` runs user Python on the shutdown fatal path — `Modules/_io/bufferedio.c:320`

`:320` calls `PyObject_ASCII((PyObject *)self)` → `buffered_repr` →
`PyObject_GetOptionalAttr(self, "name")`, which forwards to `self->raw` and can execute
arbitrary user code, at interpreter shutdown, in a thread that has just *failed* to acquire
the buffered lock. Checked for self-deadlock: `buffered_repr` does **not** take the buffered
lock (it uses `Py_ReprEnter`/`Py_ReprLeave`), so it cannot wedge here. It is a deliberate,
documented abort path (brief §4a says so). Leaking `ascii` immediately before `abort()` is
irrelevant. No action.

---

### [POLICY] `self->owner` is read outside every lock that protects its writes — `Modules/_io/bufferedio.c:258,299,332,336`

Task (d). `owner` is `volatile unsigned long` (`:258`). `bufferedio.c` contains **zero**
`FT_ATOMIC` uses. The read at `:299` runs precisely when the trylock at `:330` failed —
i.e. when another thread holds the lock and is free to be executing `:332`/`:336`.
`volatile` is not atomic and orders nothing.

**Measured, and the negative is honest about its limits.** `release-ft-nojit-tsan`,
`PYTHON_GIL=0`, `repro/io_buffered_owner_race.py`: 8 threads / 8 s → **0 races**;
16 threads / 12 s with a 4 ms lock hold → **0 races**. Both processes ran to completion
(brief lesson: a partial TSan log is indistinguishable from a clean one — these were not
partial; the script's final line printed in both).

I checked the denominator before believing the zero. gdb breakpoint on `bufferedio.c:299`,
`debug-ft-nojit`, `PYTHON_GIL=0`: **the line does execute**, reached from
`_io__Buffered_read_impl:1017` via `clinic/bufferedio.c.h:677`. So it is reachability-real
but *rare*.

Two facts explain the rarity, and both are useful FT results in their own right:

1. **On the free-threaded build the clinic `@critical_section` is taken first.** The
   generated wrapper is `Py_BEGIN_CRITICAL_SECTION(self); _io__Buffered_read_impl(...)`,
   and `ENTER_BUFFERED` runs inside it. A second thread therefore blocks on `self`'s
   `ob_mutex` and never reaches `ENTER_BUFFERED` at all — unless the holder *detaches*.
   My first TSan run got 0 races for exactly this reason; I had to put a real
   `time.sleep()` in `readinto` to force the section to be suspended.
2. **`Py_BEGIN_ALLOW_THREADS` is load-bearing on FT too, for a non-GIL reason.**
   `PyEval_SaveThread` → `_PyThreadState_Detach` → `detach_thread` (`Python/pystate.c:2317,
   :2323`) → `_PyCriticalSection_SuspendAll` (`Python/critical_section.c:105`), which
   *releases* every critical section the thread holds; `_PyThreadState_Attach` resumes them
   (`pystate.c:2304`). So `_enter_buffered_busy`'s detach at `:306` is what stops the
   critical-section-outside / raw-lock-inside nesting from inverting. The task premise that
   "on the FT build the GIL macros are largely no-ops" is **not correct** — they still
   detach, and that detach is what makes the two-lock nesting safe.

Given (1) and (2), the residual race window is roughly two instructions wide: between the
holder's successful CAS at `:330` and its `owner =` store at `:332`. The only wrong outcome
is a torn read that coincidentally equals the reader's own thread ident — a spurious
`RuntimeError: reentrant call inside`. I observed **0** such errors in 12 s × 16 threads.
That mechanism is a **hypothesis**; what would falsify it is a TSan report on `owner`, which
I did not obtain. POLICY: make `owner` `_Py_atomic_ullong` with relaxed load/store for
correctness under the C memory model and to keep it off future TSan reports; not a behaviour
bug today.

**The real FT gap in this slice is not `owner`.** It is that `_io.FileIO` has **zero**
`@critical_section` on **any** of its 13 clinic methods, and `_io.BufferedReader/Writer/
RWPair/Random.__init__` have none either — which is what makes the FIX above reachable
concurrently as well as re-entrantly. `FileIO`'s raw `PyMem_New` block `self->stat_atopen`
is freed at `:138`, `:470`, `:493`, `:538`, `:578`, `:1138` and read at `:765`, `:1250`,
`:1306` with no lock anywhere. I chased the one cross-release case and it is **clean**:
`_io_FileIO_readall_impl:765-766` copies `stat_atopen->st_size` into a local `end` *before*
the `Py_BEGIN_ALLOW_THREADS` at `:795`, and neither `readall` nor `new_buffersize` derefs
`stat_atopen` again after any release (checked lines 795–915). Flagging the missing locking
as POLICY and leaving `FileIO`'s field-level FT audit to `ft-race-scanner`.

---

## Classes bounded — negatives with their denominators

Each of these is a "looked and found nothing", with the count that makes it mean something.

**1. `mismatched_allow_threads` — 0 of 8 pairs. REAL.**
Every pair enumerated and read by hand:

| # | file:lines | function | region body | verdict |
|---|---|---|---|---|
| 1 | `bufferedio.c:306-318` | `_enter_buffered_busy` | `if/else`, both arms assign `st`, `PyThread_acquire_lock` / `_timed` | balanced |
| 2 | `fileio.c:130-136` | `internal_close` | `close(fd)` + errno save, inside `_Py_BEGIN/END_SUPPRESS_IPH` | balanced |
| 3 | `fileio.c:414-420` | `_io_FileIO___init___impl` | `open()`/`_wopen()`; the `END` is inside the `do{}` body, the `while` condition with `PyErr_CheckSignals()` is outside it (`:421-422`) | balanced |
| 4 | `fileio.c:476-478` | `_io_FileIO___init___impl` | `_Py_fstat_noraise` | balanced |
| 5 | `fileio.c:795-803` | `_io_FileIO_readall_impl` | `lseek`/`_lseeki64` | balanced |
| 6 | `fileio.c:992-1000` | `_io_FileIO_seek_impl` | `lseek`/`_lseeki64` | balanced |
| 7 | `fileio.c:1117-1126` | `_io_FileIO_truncate_impl` | `ftruncate`/`_chsize_s` | balanced |
| 8 | `fileio.c:1230-1234` | `_io_FileIO_isatty_impl` | `isatty` | balanced |

All eight are straight-line: **no `goto`, `return`, `break` or `continue` between any
`BEGIN` and its `END`**, and every `#ifdef MS_WINDOWS` inside them has both arms fully
contained in the region. The `END` is therefore unconditionally reached on every path.

**2. `api_without_gil` — 0 of 8 regions. REAL.**
No Python C-API call appears inside any of the eight. The only calls are `close`, `open`,
`_wopen`, `lseek`, `_lseeki64`, `ftruncate`, `_chsize_s`, `isatty`, `_Py_fstat_noraise`,
`PyThread_acquire_lock`, `PyThread_acquire_lock_timed` — the last two being the
GIL-independent raw-lock primitives the scanner already allow-lists. `_Py_fstat_noraise` is
so named because it does not touch the exception state.

**3. `mismatched_gilstate` — 0 of 0. STRUCTURAL, and correctly so.**
`PyGILState_Ensure`/`Release` appear **zero** times in all seven files. `_io` has no entry
point called from a foreign thread without a thread state; every path in is a method call
from the interpreter. There is nothing here for this rule to check — say that, do not
report it as a clean bill.

**4. `blocking_with_gil` — 0 in-slice, but the in-slice count is not the whole story.**
The four real blocking-I/O sites in the slice are `_Py_read`/`_Py_write` calls at
`fileio.c:696`, `:833`, `:898`, `:940`. Those wrappers live in `Python/fileutils.c` and do
their own `Py_BEGIN_ALLOW_THREADS` around `read(2)`/`write(2)`. So the GIL *is* correctly
released for all raw file I/O — but one frame up, invisibly to an intra-function scanner.
True count of GIL-released regions reachable from this slice: **12** (8 in-slice + 4
interprocedural), against the envelope's 8.

**5. `ENTER_BUFFERED` span discipline — 12 spans, 0 leaks.**
Not a `scan_gil_usage` rule (brief §4a says `scan_lock_discipline` is blind to it), but
finding (c) depends on it, so I audited all twelve by hand. Every `goto` target lands
immediately *before* its span's `LEAVE_BUFFERED`: `:606→607`, `:979→980`, `:1171→1172`,
`:1306→1307`, `:1451→1452`, `:1492→1493`, `:2228`/`:2231→2232`. `readline`'s
`end_unlocked:` at `:1308` sits *after* the `LEAVE` at `:1307` and is reached only by the
`goto` at `:1237`, which is the ENTER-*failure* path. Every bare `return NULL;` inside a
span is the line immediately following its `if (!ENTER_BUFFERED(self))` — the lock was never
taken. `_io__Buffered_read_impl:1015`'s `return res;` precedes the `ENTER` at `:1017` and
follows no other. 12 ENTER against 13 LEAVE reconciles exactly: `read` has 2 ENTERs sharing
1 LEAVE, `read1` has 1 ENTER with 3 LEAVEs.

**6. Buffer invalidation across a GIL-released syscall — 8 of 8 mutations refused. REAL.**
Task (e). `repro/io_fileio_buffer_pinning.py` parks a thread inside a blocking
`FileIO.readinto()` (GIL released in `_Py_read`) with a live writable export, then attempts
every stdlib mutation that could realloc or free the exporter's storage:
`bytearray.clear()`, `bytearray += 4096`, `bytearray[:] = b''`, `del bytearray[0:32]`,
`bytearray.pop()`, `BytesIO.close()` with a live `getbuffer()`, `BytesIO.truncate(0)`,
`BytesIO.write()` growing past the export. **All eight raise `BufferError` on both
`debug-gil-nojit` and `debug-ft-nojit`**; the read then completes and the bytearray shows
the expected bytes. The clinic `Py_buffer(accept={rwbuffer})` argument holds a
`PyBUF_WRITABLE` export for the whole call, and every stdlib exporter honours it —
`_io_BytesIO_close_impl` guards with `CHECK_EXPORTS`, `bytearray` with `ob_exports`.
`readall`'s destination is a `PyBytesWriter` the caller never publishes, and its data
pointer is recomputed after each `PyBytesWriter_Resize` (`fileio.c:833-835`), so it is safe
by construction. **Caveat:** a user-defined PEP 688 `__buffer__` that lies about pinning
would break this, but that is the exporter violating its documented contract, not `_io`.

**7. Macro hygiene — compile-proven clean.**
Brief §6.9. `LEAVE_BUFFERED` carries a trailing `;` inside its `do{}while(0);`, and the
`CHECK_*` macros expand to bare `if` statements with no `do{}while(0)` wrapper. A grep for
either used as a sub-statement of an `if`/`else` returns nothing — and it must, because
`if (x) LEAVE_BUFFERED(self) else y;` would not compile. `bufferedio.c:1172`'s
`LEAVE_BUFFERED(self);` adds a harmless empty statement. No dangling-else.

---

## Toolkit assessment

### Precision of the rules that fired here
None fired. Precision undefined; see the negatives above for what the rules *did* cover.

### T1 — `rule_not_applicable` inverts under sample-scoped merge. **HIGH. Fix this first.**

The slice envelope carries `rule_not_applicable: true` and the note *"This scanner
recognised NONE of its vocabulary in this scope"* — next to
`vocabulary_counts: {Py_BEGIN_ALLOW_THREADS: 8, Py_END_ALLOW_THREADS: 8}` and
`vocabulary_resolved: 16`. The two statements contradict each other, and the flag is the
wrong one.

Root cause, isolated:

* `scan_common.build_report` computes it correctly — `report["rule_not_applicable"] = resolved == 0`
  (`scan_common.py:222`). Re-running the scanner myself confirms: whole `Modules/_io`
  → `False`; `fileio.c` alone → `False`; `textio.c` alone → `True`.
* The **sample-scoped merge policy** in `_sample.merge_policy` declares
  `"rule_not_applicable": "or"`. Five of the seven slice files have zero vocabulary
  (`textio.c`, `bytesio.c`, `stringio.c`, `iobase.c`, `_iomodule.c`), so their `true`
  OR-poisons the whole scoped report.

The field exists specifically to stop a zero being misread. Under `"or"` it manufactures
exactly the misreading it was built to prevent, and it does so **whenever a multi-file scope
concentrates the constructs in a minority of its files** — which is the normal case. An
agent trusting the flag would have skipped the eight pairs that turned out to need hand
verification, and would have reported "no constructs present" for a slice with 16.

**Proposed fix:** change the merge policy for `rule_not_applicable` from `"or"` to
`"recomputed from the merged vocabulary_counts"` (operationally `"and"`, but recomputing is
safer because it stays correct if a scanner adds a vocabulary key). Concretely, in the
sample-merge path, drop any per-file `rule_not_applicable` and re-run
`build_report`'s `resolved == 0` test over the summed `vocabulary_counts`. Audit every other
scanner's merge policy for the same `"or"` on a boolean whose semantics are "nothing here":
the same inversion applies to any of them.

### T2 — no model of "a non-GIL lock held across a GIL release", and none of lock *lifetime*. **This is the recall gap that hid the FIX.**

`scan_gil_usage` knows about `PyThread_acquire_lock`, `PyThread_release_lock`,
`PyThread_allocate_lock` and `PyThread_free_lock` — but only as members of `_GIL_FREE_API`,
i.e. "calls that are *permitted inside* a released region". It has no rule in the other
direction. Nothing in the toolkit asks:

* Is a raw lock acquired outside a GIL-released region and still held across it?
  (True for all 12 `ENTER_BUFFERED` spans; the correct answer here is "yes, deliberately",
  but no rule establishes that.)
* Is a lock **freed** on a path reachable while another frame holds it, or while a thread is
  parked on it? That is the FIX above, and no scanner in the slice — not `scan_gil_usage`,
  not `scan_lock_discipline` (0 findings, `mutex_functions: 0`), not `scan_ft_races` — has
  the vocabulary to see it.

Proposed rule, `lock_freed_while_reachable_as_held`, cheap and intra-file:

1. Collect the set of fields passed to `PyThread_free_lock` / `PyMutex`-destroying calls
   (here `self->lock`).
2. Collect the set of fields passed to `PyThread_acquire_lock` / `_timed` (here `self->lock`,
   via the `ENTER_BUFFERED` macro body — so the rule must expand file-local macros, which
   §4a already establishes is necessary).
3. If the same field appears in both, and the freeing function is a Python-reachable entry
   point (a clinic `_impl`, a `tp_init`, a method-table entry) that is **not** the
   destructor family, emit a finding.

On `bufferedio.c` that fires exactly once, on `_buffered_init` — a true positive with
essentially no noise, because destructors are excluded and `_io` frees the lock in only two
places. Generalising: the same shape is `PyMem_Free` of a field that is also handed to user
code as a raw pointer (`self->buffer` here), which is the briefing's already-recorded
"a raw `PyMem_Malloc` buffer hanging off a live object is NOT protected by its owner"
carve-out — but that entry exists to stop a *reader* dismissing it, and there is still no
*scanner* for it. This slice is the fourth reproduced instance of that class (`_struct`
`s_codes`, `_zoneinfo` `StrongCacheNode`, `_elementtree` `extra`, now `_io`
`buffered->buffer` + `buffered->lock`). Four instances is enough to promote it from a
taxonomy note to a rule.

### T3 — interprocedural GIL-release wrappers are invisible, and the denominator says so

`fileio.c`'s four real blocking-I/O sites (`:696`, `:833`, `:898`, `:940`) call `_Py_read` /
`_Py_write`, which release the GIL in `Python/fileutils.c`. The scanner neither flags them
(correct — they are not bugs) nor credits them, so the envelope's
`Py_BEGIN_ALLOW_THREADS: 8` understates the slice's real GIL-released surface by a third.
That matters for the same reason the vocabulary counts matter at all: a reviewer sizing
"how much GIL-releasing does this scope do" gets 8 when the answer is 12.

Proposed: a small allow-list of known GIL-releasing wrappers (`_Py_read`, `_Py_write`,
`_Py_fstat`, `_Py_open`, `_Py_wait`, …) reported in the envelope as a separate
`interprocedural_gil_release_sites` count. Do **not** fold it into
`vocabulary_counts.Py_BEGIN_ALLOW_THREADS` — the two are different claims and merging them
would make the hand-verification denominator wrong.

### T4 — confirming brief §4a, and sharpening it

`ENTER_BUFFERED`/`LEAVE_BUFFERED` are indeed outside every scanner's vocabulary, and I
confirm the pairing itself is clean (12/12, section 5 above) — so the *pairing* rule §4a
anticipated would have found nothing. The valuable rule is not pairing but **lifetime**
(T2). Worth recording so the next slice does not spend its budget re-auditing the spans.

### One line for another slice's owner
`_io_StringIO___init___impl` (`stringio.c`) does `PyUnicodeWriter_Discard(self->writer);
self->writer = NULL;` on re-init, and `_io_FileIO___init___impl` (`fileio.c:278-287`) closes
the existing fd via `internal_close`. Neither involves a lock so neither is mine, but both
are the same re-init-destroys-live-state family as the FIX above and belong in the
re-entrancy agent's sweep.

---

## Reproducers

All in `reports/mod-io/repro/`, all `.py` files, all runnable as
`<build>/python <file> [io|pyio]`.

| file | what it shows | result |
|---|---|---|
| `io_buffered_reinit_frees_lock.py` | FIX, single-threaded | `_io` SIGABRT ×3 builds + ASan heap-UAF WRITE; `_pyio` rc=0 |
| `io_buffered_free_lock_under_waiter.py` | FIX, lock freed under a parked waiter | `_io` SIGABRT ×3 builds; `_pyio` rc=0 |
| `io_bufferedwriter_reinit_sibling.py` | FIX sibling, writer path | `_io` SIGABRT ×2 GIL builds; `_pyio` clean `OSError` |
| `io_buffered_lock_uninterruptible.py` | CONSIDER, SIGINT ignored | `_io` rc=8 ×3; `_pyio` `KeyboardInterrupt` @2.00 s ×3 |
| `io_buffered_lock_inversion.py` | CONSIDER, AB-BA deadlock | rc=7 on both backends, all 3 builds |
| `io_buffered_lock_inversion_ptrace.py` | same + `PR_SET_PTRACER_ANY` for gdb | native frames at `bufferedio.c:308` |
| `io_buffered_owner_race.py` | POLICY, FT `owner` race | TSan 0 races @16 threads/12 s; line proven reachable by gdb |
| `io_fileio_buffer_pinning.py` | bounded class (e) | 8/8 mutations refused, rc=0 |

## Overlap note for the synthesis step

Sibling agents were writing into `repro/` concurrently with me, and some of their filenames
(`bufferedio_reinit_buffer_uaf.py`, `io_buffered_lock_leak.py`, `io_buffered_relock_uaf.py`,
`tsan_buffered_owner.py`, `span_audit.py`) suggest independent coverage of the same
`_buffered_init` re-init shape and the same `owner` question. I did not read their scripts or
reports, so nothing above relays another agent's conclusion (brief lesson 2) — every claim
here rests on a run I made. If the same site surfaces from two agents, treat that as
independent confirmation and dedupe on the site, keeping whichever record carries the
measurement. My distinct contribution is the **lock-lifetime** framing: the freed
`PyThread_type_lock`, the `PyMutex_Unlock` abort as its signature, the parked-waiter variant,
and the resulting T2 scanner proposal.

## What I did not do

* No ASan report for the BufferedWriter sibling — the ASan build exceeded my time budget on
  it twice. The writer finding rests on the SIGABRT (×2 builds) plus structural identity
  with the measured reader path, not on a measured UAF.
* No TSan report on `owner`. The negative is real but its window is ~2 instructions; I state
  the mechanism as a hypothesis and named what would falsify it.
* `gdb -p` needs the sandbox disabled *and* `PR_SET_PTRACER_ANY` in this environment;
  plain attach fails with `ptrace: Inappropriate ioctl for device`. Recorded so the next
  agent does not lose the same twenty minutes.
