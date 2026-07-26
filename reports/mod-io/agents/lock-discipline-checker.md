# lock-discipline-checker — slice `mod-io` (INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** the 7 files in `preflight/slice_files.txt`. `winconsoleio.c` not in scope.
**Build matrix @ `a1d580430c8`:** `bufferedio.c`, `bytesio.c`, `textio.c` and `Lib/_pyio.py`
are **byte-identical** between the matrix commit and the review target (`git diff --quiet`),
so every line number cited below is valid for the binaries that produced the measurements.

---

## Headline

The scanner's `0` was structural, and I proved it rather than asserting it: on the
version of `bufferedio.c` that contained a **real, reported, fixed** lock leak
(**gh-143689**), `scan_lock_discipline` emits `findings: 0`, `mutex_functions: 0`,
`critical_section_functions: 0`, `vocabulary_counts: {}`. It did not rank the bug low.
It never looked.

I hand-audited all 12 `ENTER_BUFFERED` spans instead: **all 12 release on every path** —
a real negative, backed by a mechanical exit inventory and 14 dynamic error-path
scenarios on 3 builds.

But the lock *lifecycle* is broken. `_buffered_init` frees the lock and the buffer that
an in-flight span is holding, and `__init__` is reachable from inside every span:

| | C `io` | `_pyio` twin |
|---|---|---|
| ASan (release-gil) | **heap-use-after-free WRITE, 32 bytes** | survives |
| debug-gil / release-gil / debug-ft | **SIGABRT** `PyMutex_Unlock: unlocking mutex that is not locked`, **10/10** | survives, exit 0 |

No prior art on the tracker; not in the 153-record catalog. Reported below as **FIX**.

> **Convergence note — read this before treating the finding as mine.** After finishing my
> reproduction I found that at least two sibling agents in this same run reached the same
> root cause independently and earlier: `repro/io_buffered_reinit_frees_lock.py` (04:24)
> and `repro/bufferedio_reinit_buffer_uaf.py` (04:28) versus my
> `repro/io_buffered_relock_uaf.py` (04:34). I did not read their work before reproducing,
> so this is three independent confirmations of one defect rather than one story relayed
> three times — which is worth more than novelty. What this report adds that theirs do not
> is the **lock** half: the `PyMutex_Unlock: unlocking mutex that is not locked` abort from
> releasing the *replacement* lock, and the mutual-exclusion consequence. Treat the
> `__init__`-frees-live-state defect as one finding with three witnesses, and de-duplicate
> at slice level.

---

## Findings

### [FIX] `_buffered_init` destroys the lock and buffer of an in-flight `ENTER_BUFFERED` span (`Modules/_io/bufferedio.c:847-856`)

**What.** `_buffered_init` rebuilds both pieces of state that a live span depends on:

```c
/* bufferedio.c:846-860 */
    if (self->buffer)
        PyMem_Free(self->buffer);                 /* :847  frees the live buffer */
    self->buffer = PyMem_Malloc(self->buffer_size);
    ...
    if (self->lock)
        PyThread_free_lock(self->lock);           /* :853  frees the HELD lock   */
    self->lock = PyThread_allocate_lock();        /* :855  fresh, UNLOCKED       */
    ...
    self->owner = 0;                              /* :860  forgets the owner     */
```

`_io.BufferedReader.__init__` carries **no `@critical_section`** decorator
(`bufferedio.c:1583-1589` — the clinic block has none) and its impl
(`_io_BufferedReader___init___impl:1591`, reaching `_buffered_init` at `:1609`) takes
**no `ENTER_BUFFERED`**. None of the 12 `ENTER_BUFFERED` sites is in an `__init__`.
So the one mutator that rebuilds the lock is the one mutator that cannot take it.

Every span reaches user Python through `self->raw`, so no threads are needed — a raw
object whose `readinto()` calls `f.__init__(other)` is enough:

```
f.read()              ENTER_BUFFERED  -> self->lock = L1, owner = me
  -> raw.readinto()   user Python
     -> f.__init__()  PyThread_free_lock(L1)        L1 destroyed while HELD
                      self->lock = L2 (unlocked)
                      PyMem_Free(self->buffer)      buffer freed under a live pointer
  <- span resumes, writes through the stale buffer pointer
LEAVE_BUFFERED        PyThread_release_lock(L2)     never acquired -> fatal
```

**Measured — heap-use-after-free WRITE** (`release-gil-nojit-asan`, scenario `readline`;
GIL ASan per the FT-mimalloc caveat):

```
ERROR: AddressSanitizer: heap-use-after-free
WRITE of size 32 ... thread T0
    #1 copy_base            Objects/memoryobject.c:353
    #3 memory_ass_sub       Objects/memoryobject.c:2794
   #10 _bufferedreader_raw_read      Modules/_io/bufferedio.c:1640
   #11 _bufferedreader_fill_buffer   Modules/_io/bufferedio.c:1681
   #12 _buffered_readline            Modules/_io/bufferedio.c:1265
   #13 _io__Buffered_readline_impl   Modules/_io/bufferedio.c:1325
freed by thread T0 here:
    #1 _buffered_init                Modules/_io/bufferedio.c:847
    #2 _io_BufferedReader___init___impl  Modules/_io/bufferedio.c:1609
previously allocated by thread T0 here:
    #1 _buffered_init                Modules/_io/bufferedio.c:848
```

The write is through the `memoryview` that `_bufferedreader_raw_read` hands to
`raw.readinto()` — so **size and content are attacker-chosen**.

**Measured — deterministic abort**, 5 scenarios × 2 builds, **10/10**:

| scenario | debug-gil-nojit | release-gil-nojit |
|---|---|---|
| `read`, `read_all`, `readline`, `readinto`, `write` | rc=134 | rc=134 |

```
Fatal Python error: PyMutex_Unlock: unlocking mutex that is not locked
```

Also rc=134 on `debug-ft-nojit`. Release builds abort too — this is not a debug-only assert.

**Differential oracle.** `_pyio` survives all five scenarios cleanly, exit 0, with mutual
exclusion intact (`Lib/_pyio.py`'s `__init__` never replaces a live `_read_lock`). Per the
brief's grading table — C SIGABRT vs twin raising/returning cleanly — this is a
**confirmed, localized C bug**.

**Guarded twin.** The rest of the file: every state-mutating buffered method brackets
itself in `ENTER_BUFFERED`/`LEAVE_BUFFERED`. `close_impl:581` even drops the lock
*deliberately* before `_PyFile_Flush` with the comment *"flush() will most probably
re-take the lock, so drop it first"* — the author's own awareness of exactly this
re-entrancy. `_buffered_init` is the one mutator that opted out.

This is the briefing's **"a raw `PyMem_Malloc` buffer hanging off a live object is NOT
protected by its owner"** carve-out, with a `PyThread_type_lock` in place of the malloc'd
array — structurally the `_struct` `s_codes` shape (CPY-0048 / CPY-0050), where a
re-entrant `__init__` frees an array under a live cursor.

**Fix direction (not verified by me).** Either make `_buffered_init` refuse to
re-initialise an already-`ok` object while a span is active, or take `ENTER_BUFFERED`
around the teardown — noting that the second cannot work as written, because the lock
being replaced is the lock being held. The `self->ok = 0` at `:1596` is set *before*
`_buffered_init` runs, so a guard on `ok` is available.

**Prior art:** none upstream. `gh api search/issues` over four phrasings
(`_io BufferedReader use-after-free`, `buffered __init__ twice crash io`,
`_buffered_init reinitialize`, `io BufferedWriter reinit segfault`) returned nothing
relevant; not among the 153 catalog records (only `CPY-0008`, `winconsoleio.c`, touches
`_io`). **Independently found by two sibling agents in this run** — see the convergence
note at the top.

**Reproducers:** `repro/io_buffered_relock_uaf.py` (`[--pyio] [scenario]`).

---

### [CONSIDER — `Lib/_pyio.py`, outside the 7-file slice] the differential oracle itself deadlocks where the C accelerator raises

Same-thread re-entry into a buffered object from a raw callback:

| backend | debug-gil-nojit | release-gil-nojit |
|---|---|---|
| C `io` | `RuntimeError: reentrant call inside <_io.BufferedWriter>` | same |
| `_pyio` | **permanent deadlock** (10 s watchdog) | **permanent deadlock** |

`_pyio.BufferedWriter` locks with a plain `threading.Lock` and tracks no owner, so it has
no equivalent of the `_enter_buffered_busy` owner check. Recording it because it is a
pure-Python-reachable permanent hang **and** because it calibrates the oracle: on this
axis the C side is the correct one and the twin is not a specification. No prior art.
Belongs to whichever slice owns `Lib/`. Reproducer: `repro/pyio_reentrant_hang.py`.

---

### [ACCEPTABLE] one-line note, belongs to error-path-analyzer

`_io__Buffered_close_impl:582-584`: if `_PyFile_Flush` fails and the re-`ENTER_BUFFERED`
at `:583` then fails, `_enter_buffered_busy`'s `PyErr_Format` overwrites the flush
exception before `return NULL`. Lock discipline is correct on that path; the exception
clobber is not my class.

---

## Classes bounded — what I covered by hand, and the denominator that makes it mean something

### The scanner's zero was structural — measured, not asserted

`scanners/scan_lock_discipline.sample.json`: `findings 0`, `critical_section_functions 8`,
**`mutex_functions 0`**, `vocabulary_counts {Py_BEGIN_CRITICAL_SECTION: 9, Py_END: 8}`.

Phase-0 attribution check — the scanner lost nothing it could see:

| file | raw `BEGIN` | real | raw `END` | real |
|---|---|---|---|---|
| `bytesio.c` | 7 | **6** (line 2 is `// Py_BEGIN_CRITICAL_SECTION()`) | 6 | 6 |
| `bufferedio.c` | 1 | 1 | 1 | 1 |
| `textio.c` | 1 | 1 | 1 | 1 |
| **total** | | **8** | | **8** |

8 real sections = the scanner's 8 `critical_section_functions`. No tree-sitter attribution
gap on the vocabulary it *has*. `mutex_functions: 0` is literally true — there is no
`PyMutex` token in any of the 7 files — and completely uninformative, because the real
lock is `ENTER_BUFFERED`.

**The measured recall miss.** `git show 375e372c666^:Modules/_io/bufferedio.c` is the file
as it stood with gh-143689 live (*"BufferedReader.read1() leaves object in reentrant state
after MemoryError"*, fixed 2026-01-19 by adding exactly one `LEAVE_BUFFERED(self)`).
Running the shipped scanner on it:

```
findings: 0   critical_section_functions: 0   mutex_functions: 0   vocabulary_counts: {}
```

A live instance of the precise class this agent exists to find, in this precise file,
scored an empty denominator.

### The 12 `ENTER_BUFFERED` spans — all clean

Entries `:561 :583 :943 :968 :1008 :1017 :1059 :1115 :1236 :1429 :1476 :2097`;
13 `LEAVE_BUFFERED` at `:581 :607 :946 :980 :1022 :1067 :1076 :1082 :1172 :1307 :1452
:1493 :2232`. Every span read by hand, then re-derived mechanically
(`repro/span_audit.py` enumerates every `return` / `goto` / `break` / `continue` and every
`return`-expanding macro per span). The two accounts agree.

| span | function | exits inside | verdict |
|---|---|---|---|
| `:561→:581` | `_io__Buffered_close_impl` | 2× `goto end` → `:607` | releases |
| `:583→:607` | `_io__Buffered_close_impl` | fall-through only | releases |
| `:943→:946` | `flush_impl` | none | releases |
| `:968→:980` | `peek_impl` | `goto end` → `:980` | releases |
| `:1008→:1022` | `read_impl` (`n == -1` arm) | fall-through | releases |
| `:1017→:1022` | `read_impl` (`else` arm) | fall-through | releases |
| `:1059→:1082` | `read1_impl` | 2 early returns, **each preceded by `LEAVE`** (`:1067`, `:1076`) | releases |
| `:1115→:1172` | `_buffered_readinto_generic` | 2× `goto end`, 2× `break`, 1× `continue` | releases |
| `:1236→:1307` | `_buffered_readline` | 8× `goto end`, `goto found`, 2× `break` | releases |
| `:1429→:1452` | `seek_impl` | 2× `goto end` | releases |
| `:1476→:1493` | `truncate_impl` | 2× `goto end` | releases |
| `:2097→:2232` | `BufferedWriter_write_impl` | 8× `goto error`, 2× `goto end`, 1× `break` | releases |

Three details that make the negative real rather than lucky:

- **No `return`-expanding macro fires inside a span.** `CHECK_INITIALIZED`,
  `CHECK_INITIALIZED_INT` and `CHECK_CLOSED` all expand to a bare `return`. In all
  10 functions they sit *before* the `ENTER_BUFFERED`. Checked individually — this is the
  invisible-exit shape that would defeat the eye and the scanner alike.
- **`_buffered_readline:1237` is the correct idiom for the acquire-failure path**:
  `if (!ENTER_BUFFERED(self)) goto end_unlocked;` jumps *past* the `LEAVE` at `:1307` to a
  second label. Every other site uses `return NULL` directly, which is equally correct
  because no lock is held.
- **`ENTER_BUFFERED` failure always sets an exception.** `_enter_buffered_busy` returns 0
  only at `:302`, immediately after `PyErr_Format`; the `&&` short-circuit makes the macro
  0 exactly in that case. So none of the 12 sites is a `return-null-without-exception`.

**Dynamic confirmation.** `repro/io_buffered_lock_leak.py` drives 14 error paths through
the spans and then probes whether the object is bricked (a leaked span leaves
`self->owner` set, so the next same-thread call raises `reentrant call inside` *forever*).
**0 leaks / 14 scenarios** on `debug-gil-nojit`, `release-gil-nojit`, `debug-ft-nojit`, and
under `_pyio`. Includes gh-143689's own reproducer as a regression check.

**The oracle is validated, not assumed** (`repro/io_buffered_reentrancy.py`): the
`reentrant call inside` string is *reached on purpose* in 2 scenarios on all 3 builds, so
its absence above is evidence rather than silence. I could not obtain a true
leaked-span positive control without building pre-fix CPython; 3.14.6+ is not one
(its `read1` predates the `PyBytesWriter` refactor that introduced the bug).

### (b) `:1008` and `:1017` — no double-acquire, and the stated reasoning holds

The two entries are the two arms of `if (n == -1) { … } else { … }` (`:1006-1020`) —
mutually exclusive, one per call, both falling through to the single `LEAVE` at `:1022`.

The counterfactual reasoning in the assignment is **correct, and measured**. A second
`ENTER_BUFFERED` from the same thread takes `PyThread_acquire_lock(lock, 0)` → fails →
`_enter_buffered_busy` → `self->owner == PyThread_get_thread_ident()` (`:299`) →
`PyErr_Format(RuntimeError, "reentrant call inside %R")` → returns 0. A self-inflicted
`RuntimeError`, **not** a deadlock, because only the *busy* helper ever blocks and it
checks the owner before blocking. Measured on debug-gil, release-gil and debug-ft:
`GUARD-FIRED  RuntimeError: reentrant call inside <_io.BufferedReader>`, and the object is
still usable afterwards.

### (c) The `owner` check is correct on the free-threaded build

`volatile unsigned long owner` (`:259`). `volatile` is a compiler barrier, not an atomic;
the field is written by two plain stores under the lock (`:332` ENTER, `:336` LEAVE) and
**read at `:299` by a thread that has just failed to take that lock** — formally
unsynchronised.

It is nonetheless correct, for a reason specific to the value being compared: the only
value that changes control flow is *the reading thread's own ident*, and only the reading
thread ever writes that value. `ENTER` sets owner to self after acquiring; `LEAVE` zeroes
it **before** releasing, so a stale self-ident cannot outlive one's own span — unless a
span leaked, and no span leaks (above). Live thread idents are unique, so another thread's
value can never compare equal.

- **Measured, functional:** guard fires correctly on `debug-ft-nojit` (`gil=False`); a
  second *thread* contending the same object blocks and proceeds normally and never sees
  the reentrancy error.
- **Measured, TSan:** `release-ft-nojit-tsan`, `PYTHON_GIL=0`, 8 threads on one shared
  `BufferedRandom`, **91,381 operations in 4 s, 0 threads stuck, 0 ThreadSanitizer
  warnings**, *process exited* (`EXIT=0`, log terminated with `DONE`).

  Honest caveat on that number: my **first** TSan run was worthless and I nearly reported
  it. `SlowRaw` had no `\n` and wrapped at EOF, so `readline()` never terminated — 7 ops
  in 246 s with all 8 threads stuck, which reads exactly like a discovered deadlock. It
  was my harness. Fixed in `repro/tsan_buffered_owner.py`; the 91,381-op figure is from
  the fixed version.

Verdict: **no live defect.** The `volatile`-instead-of-`_Py_atomic` spelling is a
pre-atomics anachronism worth a cleanup, not a bug — the argument above does not depend on
ordering, only on which thread writes which value.

### (d) `_enter_buffered_busy`'s `_Py_FatalErrorFormat` — deliberate, not a finding

`:319-325`. When `_Py_IsInterpreterFinalizing()`, the acquire becomes a 1-second timed wait
and a timeout aborts. The in-code comment (`:310-315`) states the intent: a daemon thread
abruptly shut down while owning the lock would otherwise hang finalization forever. This is
a documented, deliberate abort at shutdown. **Not a finding. Please do not re-litigate it.**

### (e) Critical section × buffered lock — no order inversion is possible

The 8 critical sections the scanner *did* see are all the trivial-wrapper idiom —
`begin; ret = helper_lock_held(...); end; return ret;` with no branch between begin and
end, so both leak shapes are structurally impossible:
`bytesio.c:303, 790, 980, 1068, 1175, 1305`; `textio.c:3309`; `bufferedio.c:1512`.
(`bytesio_get_closed:303` has an `if/else` but both arms only assign a singleton.)
**No `Py_BEGIN_CRITICAL_SECTION2`, and no two-object nesting anywhere in the slice** —
`bytesiobuf_getbuffer:1305` locks `->source` rather than `op`, but nothing ever locks the
`bytesiobuf`, so there is no second order to invert against.

**Scope note the scanner cannot see:** `Modules/_io/clinic/bufferedio.c.h` holds **25 more
`Py_BEGIN_CRITICAL_SECTION(self)`** wrappers emitted by `@critical_section`. The `clinic/`
subdirectory is not in `slice_files.txt`, so those never entered any denominator. That is
the briefing's "the lock is in the Argument Clinic wrapper" FP class showing up as a
*coverage* gap rather than a false positive.

**Why the two schemes cannot deadlock.** Both orderings that matter are safe, and the
reason is mechanical, not stylistic:

- **CS(self) then buffered lock** is the only order that occurs (25 clinic wrappers, plus
  `buffered_iternext:1512` → `_buffered_readline` → `ENTER_BUFFERED`). Nothing takes the
  buffered lock and then a critical section on another `_io` object.
- **A thread can never hold a critical section while blocking on the buffered lock.**
  Blocking happens only inside `_enter_buffered_busy`'s `Py_BEGIN_ALLOW_THREADS`
  (`:306-318`) → `_PyThreadState_Detach` → `detach_thread` → `_PyCriticalSection_SuspendAll`
  (`Python/pystate.c:2323`, `Python/critical_section.c:109`), which `PyMutex_Unlock`s
  **every** section the thread holds. So the critical section is provably released before
  the wait begins.

  Confirmed empirically: `cross_thread_contention` on `debug-ft-nojit` — thread 1 holds
  CS(self) and the buffered lock for 250 ms inside `raw.write()` while thread 2 calls
  `f.write()`; thread 2 completes normally (`other=ok`), which could not happen if the
  clinic critical section were held across the span.

  The corollary is worth stating plainly: **`@critical_section` provides no atomicity
  across a buffered span.** It is suspended at every blocking point. `ENTER_BUFFERED` is
  the only real mutual exclusion here, which is exactly why the finding above — destroying
  that lock mid-span — is severe.

### Where the FT ASan caveat bit and where it did not

Heap evidence was taken on `release-gil-nojit-asan` per briefing note 6 (FT ASan has no
shadow for the mimalloc object heap). The abort reproduces identically on FT, but the
use-after-free trace is from the GIL ASan build.

---

## Toolkit assessment

### (f) Making `scan_lock_discipline` see `ENTER_BUFFERED` — three changes, and the data one alone does nothing

I tested each on a throwaway copy of the plugin (`/tmp/ldtest`); the real toolkit is
untouched. Measured on two ground-truth files: the pre-fix `bufferedio.c` (gh-143689 live)
and the current one (fixed).

| configuration | pre-fix (bug present) | current (fixed) |
|---|---|---|
| **as shipped** | 0 findings, `vocabulary_counts {}` | 0 findings |
| **+ data entry only** | 0 findings, `mutex_functions 0→10` | 0 findings |
| **+ data entry + semicolon fix** | **2: gh-143689 TP** + 1 FP | 1 FP |

#### Change 1 (necessary, insufficient) — the `lock_macros.json` entry

Add to `lock_pairs`, and to `all_acquire_macros` / `all_release_macros`:

```json
{
  "name": "ENTER/LEAVE_BUFFERED",
  "acquire": ["ENTER_BUFFERED"],
  "release": ["LEAVE_BUFFERED"],
  "type": "python_mutex",
  "note": "Modules/_io/bufferedio.c:329-338. A raw PyThread_type_lock, NOT scoped: it is released on several branches and `goto end; ... end: LEAVE_BUFFERED(self)` is the idiomatic ladder, so it needs the PyMutex family's textual-dominance rule, not the critical-section pairing rule. ENTER_BUFFERED is an EXPRESSION returning success (`if (!ENTER_BUFFERED(self)) return NULL;`), so the exit in the acquire's own failure branch must not count as a leak. The only ENTER/LEAVE macro pair of this shape left in CPython."
}
```

**`type` must be `python_mutex`, not `extension_lock`.** Two reasons, both measured:

1. `_MODELLED_FAMILIES = ("critical_section", "python_mutex")` (`scan_lock_discipline.py:89-91`).
   Anything typed otherwise is **inert data** — it never reaches a rule. Variant B
   (`extension_lock`) produced no change to any count.
2. The semantics match exactly. A `PyThread_type_lock` is not scope-bound, so
   "between the acquire and its matched release" is the wrong window; the family's
   textual-dominance rule (`_mutex_leaking_exits`) is the right one, and its
   `mutex_leak_on_error` wording ("not released by scope exit") is already accurate.

**Two pre-existing data defects found while checking this**, both worth fixing in the same
pass:

- `ENTER_ZLIB` / `LEAVE_ZLIB` / `ENTER_BROTLI` / `LEAVE_BROTLI` are **dead data**:
  **0 occurrences** across `Modules/`, `Objects/`, `Python/`. `zlibmodule.c` migrated to
  `PyMutex` (`:223, :767, :809, …`). They are also the only `extension_lock` entries, i.e.
  the whole family is dead *and* unmodelled. Delete them or retype them.
- The `python_thread` family (`PyThread_acquire_lock` / `_timed` / `PyThread_release_lock`)
  is in the data file, is **not** in `_MODELLED_FAMILIES`, and has **74 acquires / 80
  releases across 11 files** (`faulthandler.c`, `_tkinter.c`, `_interpqueuesmodule.c`,
  `_interpchannelsmodule.c`, `sysmodule.c`, `thread.c`, …). That is the largest unmodelled
  population in the file. Modelling `python_thread` would subsume `ENTER_BUFFERED`
  automatically through the existing `resolve_local_lock_macros` `#define` resolver
  (`:184`) — a cleaner fix than the one-off entry, with a wider blast radius that wants
  its own measurement.

#### Change 2 (the real blocker, and it is chassis-wide, not lock-specific)

`LEAVE_BUFFERED(self)` is written **without a trailing semicolon at 12 of its 13 call
sites** (only `:1172` has one), because the macro body already ends `} while(0);`. That is
the brief's §6 macro-hygiene note — and it is far more damaging than a style nit.

tree-sitter-c does not merely miss the call. It **re-parses the following statement**:

```
LEAVE_BUFFERED(self)          ->  declaration
  return NULL;                      macro_type_specifier  "LEAVE_BUFFERED(self)"
                                    identifier            "return"      <-- the type's "name"
                                    ERROR                 "NULL"
LEAVE_BUFFERED(self);         ->  expression_statement -> call_expression
  return NULL;                    return_statement
```

So the release **and the exit** both vanish. A rule that needs to see "is there a `return`
between the acquire and a release" is deprived of both operands.

Measured on `bufferedio.c` (41 whole-line semicolon-less invocations of `LEAVE_BUFFERED`,
`CHECK_INITIALIZED`, `CHECK_CLOSED`):

| | as shipped | desugared | delta |
|---|---|---|---|
| `ERROR` nodes | 39 | 22 | **−17** |
| `MISSING` nodes | 15 | 3 | **−12** |
| `return_statement` nodes | **137** | **157** | **+20** |
| `function_definition` nodes | 84 | 79 | −5 (the shipped parse *invents* 5) |

**Every scanner in the toolkit is blind to 20 `return` statements — 12.7% of the file —
and analyses 5 functions that do not exist.** `bufferedio.c` is the worst file in the
slice by a wide margin (`textio.c` 9 ERROR, `bytesio.c` 4, `_iomodule.c` 1).

Two concrete proposals:

- **A pre-parse normalisation pass in the shared `tree_sitter_utils`** (upstream in
  cext-review-toolkit): append `;` to a whole-line invocation of a `#define`d
  statement-macro before parsing, preserving byte offsets so reported line numbers stay
  correct. **Use `[^;{}\n]*`, not `[^;{}]*`** — the negated class matches newlines, so the
  greedy version spans lines and rewrites `if (!ENTER_BUFFERED(self))` into
  `if (!ENTER_BUFFERED(self));`. I made exactly that mistake, and it silently manufactured
  3 plausible-looking false positives that I reported to myself before catching them. My
  corrected script asserts no line containing `if (` is ever rewritten
  (`/tmp/ldtest/desugar2.py`, method preserved in `repro/`-adjacent notes).
- **Put the parse health in the envelope.** Add `parse_error_nodes` and
  `parse_missing_nodes` to `denominators` for every scanner. A file with 39 ERROR nodes
  must never be certified clean by anything. This is a cheap, universal canary and it
  generalises well beyond `_io` — it is the structural-zero detector for the *chassis*,
  the way `vocabulary_counts` is for the *vocabulary*.

#### Change 3 (the FP gate) — and a correction to the assignment's premise

With changes 1+2, the current, correct `bufferedio.c` yields **exactly one** false positive:

```
line 1008 | mutex_missing_unlock | _io__Buffered_read_impl | FIX | confidence medium
```

That is item (b)'s shape: two acquires (`:1008`, `:1017`) in **mutually exclusive** `if` /
`else` arms sharing one release (`:1022`). `_pair_events` matches `:1017` with the release
and reports `:1008` as a partially-paired acquire. The gate: when two acquires of the same
lock lie in sibling branches of one `if`/`else` (or distinct `switch` arms), they are
alternatives, not a sequence, and one release discharges both.

**The premise about the expression form turns out to be already handled**, which I would
not have guessed: `if (!ENTER_BUFFERED(self)) return NULL;` produced **no** false positive
at any of the 12 sites once the parse was fixed. `_mutex_leaking_exits` uses block-chain
dominance (`_dominates` / `_block_chain`), not textual order, so the acquire-failure exit
does not register as dominated by the acquire. Worth keeping a regression test for, since
it is load-bearing and non-obvious — but no new gate is needed for it.

#### Net effect

On the pre-fix file the tuned rule reports **2 findings, one of which is gh-143689 at the
exact line the upstream fix touched** (`_io__Buffered_read1_impl`, the `return NULL` inside
`if (writer == NULL)`), and it correctly **stops** reporting it on the fixed file.
Precision 1/2 on the buggy file, 1 FP on the clean file — and 0/0 today.

### What reading found that no scanner rule would have

The FIX above is not a leak, so **no amount of vocabulary tuning would have surfaced it**.
Every `ENTER_BUFFERED` is correctly paired; the defect is that a *different* function
destroys the lock object itself. The rule family models acquire/release pairing, not lock
*lifetime*. A candidate new rule, stated concretely enough to build:

> **`lock_freed_while_live`** — a function that calls `PyThread_free_lock` /
> `PyMutex`-bearing-struct free / `PyMem_Free` on a field that is the *subject* of an
> acquire macro elsewhere in the same file, without itself taking that lock.

On this slice that rule has a denominator of 1 and would have fired on
`_buffered_init:853`. It is the lock-discipline analogue of the briefing's already-known
"`PyMem_Malloc` buffer is not protected by its owner" carve-out (`_struct` `s_codes`,
`_zoneinfo` `StrongCacheNode`, `_elementtree` `extra`) — which suggests the same rule,
generalised from locks to any raw buffer hanging off a live object, would have real
tree-wide recall.

---

## Reproducers (`reports/mod-io/repro/`)

| file | what it does |
|---|---|
| `io_buffered_relock_uaf.py` | **the FIX.** 5 scenarios; ASan heap-UAF WRITE + SIGABRT 10/10; `--pyio` oracle survives |
| `io_buffered_lock_leak.py` | 14 error-path scenarios probing the bricked-object oracle; 0 leaks × 4 configurations |
| `io_buffered_reentrancy.py` | positive control for that oracle + items (b) and (c); guard fires on 3 builds |
| `pyio_reentrant_hang.py` | isolates the `_pyio` deadlock vs C `RuntimeError` |
| `tsan_buffered_owner.py` | 8-thread contention on one buffered object; 91,381 ops, 0 races (fixed harness) |
| `span_audit.py` | mechanical exit inventory for all 12 spans |

## Verdict

- **FIX: 1** — `_buffered_init` destroys the lock and buffer of a live span. Reproduced two ways on three builds with a clean `_pyio` differential; no upstream prior art. **Independently confirmed by two sibling agents in this run — de-duplicate at slice level; this report contributes the lock half (the `PyMutex_Unlock` abort and the lost mutual exclusion).**
- **CONSIDER: 1** — `_pyio` re-entrancy deadlock (out of slice, `Lib/_pyio.py`).
- **Leaked `ENTER_BUFFERED` spans: 0 of 12** — a real negative, hand-audited, mechanically cross-checked, dynamically probed, with a validated oracle.
- **Critical-section leaks: 0 of 8** — all trivial wrappers; the shape is structurally impossible there.
- **Nested two-object locking: 0** — no `CRITICAL_SECTION2`, no second lock order to invert.
- **Toolkit: the zero was structural and is now quantified** — the shipped scanner scores an empty denominator on a file with a known live bug; the fix needs a data entry, a chassis-level parse repair worth 20 lost `return` statements in this file alone, and one FP gate.
