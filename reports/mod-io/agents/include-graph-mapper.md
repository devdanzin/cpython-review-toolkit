# include-graph-mapper — slice `mod-io`

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777`
**Scope:** the 7 files in `preflight/slice_files.txt`, plus `Modules/_io/_iomodule.h` (header) and
`Modules/_io/winconsoleio.c` (mapped, never reviewed).
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; measurements on
`~/projects/python_build_matrix/builds/release-gil-nojit/python`.

**Drift check (brief §5.10):** `git diff a1d580430c8 4f3be1b5777 -- Modules/_io/{bufferedio,textio,bytesio}.c`
is **empty**. The build-matrix commit and the review target are byte-identical for every file I cite.

---

## 0. Corrections to AGENT_BRIEF.md §4 — read this first

Eight items. Three are material enough to change what agents should be doing right now.

### C1 — MAJOR. "bufferedio.c barely uses `Py_BEGIN_CRITICAL_SECTION` (one site, `:1512`)" is wrong at module scope.

`bufferedio.c:1512` is the only *hand-written* critical section in that `.c` file. That is true and
irrelevant. Argument Clinic emits **25 `Py_BEGIN_CRITICAL_SECTION(self)` wrappers** into
`Modules/_io/clinic/bufferedio.c.h`, one per `@critical_section`-annotated method. Measured
`Py_BEGIN_CRITICAL_SECTION` / `Py_END_CRITICAL_SECTION` counts:

| file | generated (`clinic/*.c.h`) | hand-written (`.c`) |
|---|---|---|
| bufferedio | **25 / 25** | 1 / 1 (`:1512`) |
| textio | **26 / 26** | 1 / 1 (`:3309`) |
| bytesio | **20 / 20** | 6 / 6 |
| stringio | **16 / 16** | 0 |
| fileio, iobase, _iomodule, winconsoleio | 0 | 0 |
| **total** | **87** | **8** |

Every method in the §6 hunt list is inside one: `_io._Buffered.{read,read1,readinto,readinto1,
readline,peek,seek,tell,truncate,flush,close,detach,seekable,readable,writable,fileno,isatty,
__sizeof__}`, the `closed`/`name`/`mode` getters, and `_io.BufferedWriter.write` (25 total,
enumerated at `bufferedio.c:449,508,535,549,612,635,648,661,675,689,705,718,930,952,985,1027,1177,
1191,1314,1330,1354,1457,2080` + `_BufferedIOBase.readinto`/`readinto1` at `:86,:100`).

**Consequences the brief's framing hides:**
1. On a free-threaded build the *outer* lock for these methods is the per-object critical section,
   acquired in the clinic wrapper. `ENTER_BUFFERED` nests **inside** it. Two locks, fixed order.
2. `scan_lock_discipline`'s `critical_section_functions: 8` is 8 of **95**, not 8 of a handful —
   the scan root is the 7 `.c` files and the generated `.c.h` files hold the other 87. §4d reads
   that 8 as a meaningful denominator; it is a scoping artifact.
3. `_enter_buffered_busy` (`:296-327`) does `Py_BEGIN_ALLOW_THREADS` around a blocking acquire.
   Detaching the thread state suspends the enclosing critical section. So a `@critical_section`
   method **does not** hold its critical section across an `ENTER_BUFFERED` wait. Any agent
   reasoning "the clinic critical section makes this atomic" must stop at that boundary.

### C2 — MAJOR. All 12 `ENTER_BUFFERED` spans are balanced. The brief points agents at the wrong hazard inside the right region.

The counts and line numbers in §4a are exactly right (12 `ENTER_BUFFERED`, 13 `LEAVE_BUFFERED`, same
lines). I audited all 12 spans by hand — every `goto end`/`goto error`, every `break` out of a loop
inside the region, every early `return`.

| ENTER | function | exits | verdict |
|---|---|---|---|
| `:561` | `_io__Buffered_close_impl` | `goto end`→`:607`; deliberate `:581` drop | balanced |
| `:583` | (re-entry in same fn) | `:607`; `return NULL` at `:584` only when ENTER *failed* | balanced |
| `:943` | `_io__Buffered_flush_impl` | `:946` | balanced |
| `:968` | `_io__Buffered_peek_impl` | `goto end`→`:980` | balanced |
| `:1008` | `_io__Buffered_read_impl` (n==-1) | `:1022` | balanced |
| `:1017` | `_io__Buffered_read_impl` (n>=0) | `:1022` (mutually exclusive with `:1008`) | balanced |
| `:1059` | `_io__Buffered_read1_impl` | `:1067`, `:1076`, `:1082` — every path takes exactly one | balanced |
| `:1115` | `_buffered_readinto_generic` | all `goto end`→`:1172`; loop `break`→`:1169` | balanced |
| `:1236` | `_buffered_readline` | `:1307`; ENTER-failure jumps to `end_unlocked:` `:1308`, correctly skipping LEAVE | balanced |
| `:1429` | `_io__Buffered_seek_impl` | `goto end`→`:1452` | balanced |
| `:1476` | `_io__Buffered_truncate_impl` | `goto end`→`:1493` | balanced |
| `:2097` | `_io_BufferedWriter_write_impl` | every `goto error`/`goto end` reaches `:2232` | balanced |

**Denominator: 12/12 spans, 13/13 `LEAVE_BUFFERED` sites accounted for.** The lock-leak class is a
real negative here. §4a's "if you find a leak here, that is simultaneously a CPython finding and a
scanner recall gap" is a bet that does not pay out.

The hazard that *is* live in those regions is a different one, and §4a's framing steers away from it:

> **`detach()` is the only state-destroying method on `buffered` that does not take `ENTER_BUFFERED`.**

`_io__Buffered_detach_impl` (`:616-630`) has `CHECK_INITIALIZED` and nothing else. `close()` is
protected — a re-entrant `close()` from inside a locked region hits
`_enter_buffered_busy`'s `self->owner == PyThread_get_thread_ident()` branch and raises
`"reentrant call inside %R"` (`:299-302`). `detach()` sails through, sets `self->raw = NULL`
(`:626`) and `self->ok = 0` (`:628`), and the outer frame keeps using both. Every user-code callback
fired from inside a locked region — `raw.readinto` (`:1640`), `raw.write` (`:1996`), `raw.read`
(`:1748`), `raw.seek` (`:818`), `raw.tell` (`:788`), `raw.truncate` (`:1485`), `PyErr_CheckSignals`
at `:2057`/`:2213` — can reach it.

### C3 — MAJOR. The §3 seeded lead is not a novel shape. It is an unswept sibling of a shipped CPython crash fix.

The textio "guarded twin" is the fix for two **closed** `type-crash` issues:

- **gh-142594** — "Null pointer dereference in `TextIOWrapper.close()` via re-entrant `closed`
  property that detaches `buffer`"
- **gh-143008** — "Null pointer dereference in `TextIOWrapper.truncate` via re-entrant `flush`"

shipped as **`db4b1948bc4`** (PR #145957, Cody Maloney, 2026-06-09). Commit message: *"The
implementation often checked at the start of functions if `self->buffer` is in a good state, but did
not always recheck after other Python code was called... rather than rely on reviewer effort create
better safety by making all `self->buffer` access go through helper functions."*

`git show --name-only --format= db4b1948bc4 | grep -c bufferedio` → **0**. The sweep was textio-only.

This changes the framing every agent should use: not "novel finding", but **incomplete fix
propagation on a known, labelled, already-triaged CPython crash class**. It also means prior-art
search must start from those two issue numbers, not from a cold `gh api` query.

The brief names one unswept site (`detach`). There is at least one more in the same function family
that it does not name, and it is arguably more reachable — `_io__Buffered_close_impl`:

```c
CHECK_INITIALIZED(self)                              /* :560 */
if (!ENTER_BUFFERED(self)) return NULL;              /* :561 */
r = IS_CLOSED(self);                                 /* :565  -> raw.closed, user Python */
...
LEAVE_BUFFERED(self)                                 /* :581  lock deliberately dropped */
r = _PyFile_Flush((PyObject *)self);                 /* :582  -> self.flush(), user Python */
if (!ENTER_BUFFERED(self)) return NULL;              /* :583 */
...
res = PyObject_CallMethodNoArgs(self->raw, close);   /* :591  no re-check of raw or ok */
```

Both `:565` and `:582` are the *exact* two entry points named in gh-142594 (`closed` property) and
gh-143008 (`flush`), transplanted into `bufferedio.c`. I did not run this — the repro directory
already contains `io_buffered_close_detach.py` and `io_close_null_raw.py` from sibling agents, and
duplicating their measurement would add nothing. I am reporting the structural correspondence, not
a crash.

### C4 — §4b's textio guard table is mismapped and incomplete.

Measured:

| macro | actual line | brief said |
|---|---|---|
| `CHECK_CLOSED` | `textio.c:1571` | 1571 ✓ |
| `CHECK_INITIALIZED` | `textio.c:1597` | 1604 ✗ (swapped) |
| `CHECK_ATTACHED` | `textio.c:1604` | 1597 ✗ (swapped) |
| `CHECK_ATTACHED_INT` | `textio.c:1612` | omitted |
| `CHECK_INITIALIZED_DECODER` | `textio.c:315` | omitted |

`buffer_access_safe` is also not a lone "post-re-entrancy accessor". It heads a family of four:
`buffer_access_safe` (`:740`), `buffer_getattr` (`:763`), `buffer_callmethod_noargs` (`:774`),
`buffer_callmethod_onearg` (`:785`), with 30 call sites. The design contract is stated at
`textio.c:734-738`. That gives a **mechanical audit rule** the brief does not state:

> Any bare `self->buffer` in `textio.c` outside `{buffer_access_safe, __init__, tp_clear,
> tp_traverse, detach}` is a deviation from the file's own written contract.

I checked. There are 11 `self->buffer` occurrences in textio.c: `:744`/`:759` (inside the accessor),
`:1213` (init `Py_CLEAR`), `:1275` (init assign), `:1516` (`textiowrapper_clear`), `:1552`
(traverse), `:1642` (detach assign), and 4 in the comment block. **Zero deviations.** textio holds
its contract.

The contrast is the point: `bufferedio.c` has **40** `self->raw` occurrences, **22** of them as a
call/getattr receiver, and **no accessor function at all**. The population that needs the
gh-143008 treatment is 22 sites in bufferedio, not 1.

One caveat on the twin itself: `buffer_access_safe` returns a **borrowed** reference and justifies
it (`:756-757`) with *"safe since TextIOWrapper methods are protected by critical sections."* That
justification covers cross-thread mutation. It does **not** cover same-thread re-entrancy, which is
precisely the bug class it was added to fix. `textio.c:2677-2681` takes the borrowed `buf` and
immediately calls `buf.seek(0, 2)`; if that user `seek` detaches and the result is dropped, the
borrowed pointer dangles. I did not measure this. Flagging it as a candidate for the refcount /
null-safety agents, labelled unverified.

### C5 — §4c's bytesio description is imprecise in three ways.

1. **"all through `FT_ATOMIC_LOAD_SSIZE_RELAXED`"** — the two *mutation* sites are not loads:
   `bytesio.c:1291` `FT_ATOMIC_ADD_SSIZE(b->exports, 1)` and `:1316` `FT_ATOMIC_ADD_SSIZE(b->exports, -1)`.
   Reads are relaxed; the RMWs are `FT_ATOMIC_ADD_SSIZE`. §4c's "hard look at the RELAXED ordering"
   is also weaker than it sounds: every relaxed read sits inside a function that already asserts
   `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(self)` (11 such assertions in bytesio.c), so the
   relaxed load reads a value the caller holds the object's critical section for.
2. **`exports` is not the whole invariant.** There is a second, FT-only sharing flag `buf_shared`
   (`bytesio.c:25-27`, `#ifdef Py_GIL_DISABLED`) with `set_shared_buf` (`:84`), `clear_shared_buf`
   (`:92`) and `SHARED_BUF` (`:78-82`). The three-state comment at `:39-44` that §4c cites as
   "the documented invariant" **predates it and does not mention it**. `resize_unshared_buffer_lock_held`
   (`:100-115`) carries a *different* invariant in a code comment: *"If the internal bytes object
   escaped via a zero-copy getvalue(), read(), or peek(), resizing it would mutate an object visible
   to Python code. Callers must detach first."* — enforced only by `assert(!self->buf_shared)`,
   i.e. debug builds only.
3. The brief's exports line list omits `:164` and `:185`, the two `assert(...exports == 0)` in
   `unshare_buffer_lock_held` / `resize_buffer_lock_held`. Those are the load-bearing ones: they are
   the *only* statement of "resize requires exports==0" at the resize primitives themselves, and
   they vanish in a release build.

The genuinely interesting FT asymmetry in this module is a different one:

| file | `@critical_section` methods | `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` | `_lock_held` naming |
|---|---|---|---|
| bytesio | 20 | 11 | yes |
| textio | 26 | 3 | partial |
| stringio | 16 | **0** | **no** |
| bufferedio | 25 | **0** | no (uses `_unlocked` for the opposite meaning) |

`stringio.c` has 16 clinic critical sections and **zero** internal assertions that any of its
helpers run locked. Its FT protection is entirely implicit in the clinic annotations, with nothing
that would catch a helper being called from an unannotated path.

### C6 — §4d's `scan_uninit_dealloc` row: right reasoning, wrong conclusion to draw.

The allocator claim is correct and I verified it. There are exactly **4** `tp_alloc(type, 0)` sites
in the slice (`stringio.c:656`, `bytesio.c:395`, `bytesio.c:1098`, `fileio.c:202`) and **3**
`Py_tp_new` slots (StringIO, BytesIO, FileIO; plus `bytesiobuf` allocated directly at `:395`).
`BufferedReader`/`BufferedWriter`/`BufferedRandom`/`BufferedRWPair`/`TextIOWrapper`/
`IncrementalNewlineDecoder`/`_IOBase` define **no** `Py_tp_new` at all — they inherit
`object.__new__` → `PyType_GenericAlloc`, which zeroes. No `PyObject_New`/`PyObject_GC_New`
anywhere. So the classic uninit-dealloc shape is genuinely absent.

But "the uninit-dealloc class is structurally absent" should not be read as "no half-constructed
object class here". The analogue that **is** present is **re-initialization of a live object**:

```c
static int _buffered_init(buffered *self)          /* bufferedio.c:837 */
{
    ...
    if (self->buffer) PyMem_Free(self->buffer);    /* :846-847 */
    self->buffer = PyMem_Malloc(self->buffer_size);/* :848 */
    ...
    if (self->lock) PyThread_free_lock(self->lock);/* :853-854 */
    self->lock = PyThread_allocate_lock();         /* :855 */
```

Its three callers are the `Py_tp_init` slots of BufferedReader (`:1609`), BufferedWriter (`:1962`)
and BufferedRandom (`:2509`). **None of those `__init__`s is `@critical_section`**, and none takes
`ENTER_BUFFERED`. Measured annotation split:

| `__init__` | `@critical_section`? |
|---|---|
| `_io.BytesIO.__init__` (`bytesio.c:1116`) | **yes** (`:1115`) |
| `_io.TextIOWrapper.__init__` (`textio.c:1127`) | **yes** (`:1126`) |
| `_io.BufferedReader.__init__` (`:1584`) | no |
| `_io.BufferedWriter.__init__` (`:1932`) | no |
| `_io.BufferedRWPair.__init__` (`:2256`) | no |
| `_io.BufferedRandom.__init__` (`:2473`) | no |
| `_io.StringIO.__init__` (`stringio.c:673`) | no |
| `_io.FileIO.__init__` (`fileio.c:225`) | no |

So `f.__init__(other_raw)` on a live BufferedReader frees `self->buffer` and `self->lock` with no
lock held and no critical section, while every read/write path on the same object holds both.
`scan_ft_races` independently flags `bufferedio.c:846`, `:847`, `:849` as
`guarded_writer_unguarded_reader` at medium confidence — the scanner is right, for a reason it does
not articulate. No scanner in the suite models re-init as a class.

### C7 — minor. §6.9's macro-hygiene worry produces zero hits, and one premise is wrong.

- `grep -nE '(if|else|while|for)[^;{]*\)[ \t]*CHECK_[A-Z_]+\('` over `Modules/_io/*.c` → **none**.
- `LEAVE_BUFFERED` as an unbraced if/else body → **none**. The trailing `;` inside
  `do{...}while(0);` is real but unexploited.
- "`CHECK_*` macros expand to bare `if` statements with no `do{}while(0)` wrapper" is only
  half-true. `bufferedio.c`'s `CHECK_CLOSED` (`:370`) and `ADJUST_POSITION` (`:390`) **are**
  `do{}while(0)`-wrapped. The bare ones are `CHECK_INITIALIZED`/`_INT` (`:340`/`:352`) and
  `bytesio.c`'s `CHECK_CLOSED`/`CHECK_EXPORTS` (`:66`/`:72`).
- The one that would genuinely misbehave as an unbraced body is `textio.c:1604 CHECK_ATTACHED`,
  which expands to **two** statements (`CHECK_INITIALIZED(self); if (self->detached) {...}`).
  No such use exists. Worth a POLICY note, not a hunt.

### C8 — scope note. `_iomodule.h` is not optional reading.

§1 lists `_iomodule.h` as "not in the slice". Several behaviours of the seven `.c` files are
undecidable without it: the module-state struct (`:145-167`), the three state-lookup inlines
(`:169-191`), the `Py_off_t`/`PY_OFF_T_MAX` typedef family (`:95-135`), and the contract comments
for `_PyIOBase_check_*` (*"when args=Py_True is passed, they return a borrowed reference"*, `:31-34`)
and `_PyFileIO_closed` (*"Doesn't check the argument type, so be careful!"*, `:50-52`). Agents
should read it.

---

## 1. Include graph and coupling (task a)

`analyze_includes.py Modules/_io` — 17 files, 121 include directives, 40 unique headers,
1 tree-wide cycle (`pycore_structs.h` ↔ `pycore_context.h`; not reachable from this slice's
directives, and the standing CONSIDER already recorded for it).

### API tiers, scan root `Modules/_io`

| tier | n | headers |
|---|---|---|
| public | 3 | `Python.h`, `exports.h`, `structmember.h` |
| cpython | 0 | — |
| internal | 20 | see below |
| generated | 8 | `clinic/*.c.h` |
| vendored | 0 | — |
| other-local | 1 | `_iomodule.h` |
| unresolved | 0 | — |
| system | 8 | `fcntl.h io.h stdbool.h stddef.h sys/stat.h sys/types.h unistd.h windows.h` |

Zero unresolved directives — unusual, and it means every claim below is about a header that really
exists in the tree.

### Per-file internal-header reach

| file | `pycore_*` headers |
|---|---|
| `textio.c` | call, codecs, fileutils, interp, long, object, pyerrors, pystate, unicodeobject, weakref (**10**) |
| `_iomodule.c` | abstract, interp, long, pyerrors, pystate (5) |
| `bytesio.c` | critical_section, object, pyatomic_ft_wrappers, sysmodule, weakref (5) |
| `iobase.c` | call, fileutils, long, object, pyerrors, weakref (6) |
| `bufferedio.c` | call, fileutils, object, pyerrors, pylifecycle, weakref (6) |
| `fileio.c` | fileutils, object, pyerrors, weakref (4) |
| `stringio.c` | object, weakref (2) |
| `_iomodule.h` | moduleobject, typeobject (2) |
| `clinic/*.c.h` (generated) | gc, runtime, abstract, modsupport, **critical_section** |

Fan-out ranking (directives per file): `fileio.c` 13, `textio.c` 13, `winconsoleio.c` 12,
`_iomodule.c` 11, `iobase.c` 10, `bufferedio.c` 9, `bytesio.c` 9, `stringio.c` 6.

Fan-in (tree-wide / within `Modules/_io`): `Python.h` 338/8, `pycore_modsupport.h` 180/8,
`pycore_runtime.h` 156/8, `pycore_gc.h` 115/7, `pycore_object.h` 106/7, `pycore_pystate.h` 94/2,
`pycore_critical_section.h` 64/5, `pycore_fileutils.h` 43/5, `pycore_weakref.h` 40/7.
The three highest in-scope internal headers (`modsupport`, `runtime`, `gc`) reach `_io` **only
through the generated clinic headers** — no `.c` file in the slice includes them directly.

### Internal-API dependencies that constrain a fix

Ranked by how much they restrict the fix space.

1. **`_PyFile_Flush` (`pycore_fileutils.h:308`) — 11 call sites, the module's dominant re-entrancy
   vector.** `bufferedio.c:582,622`; `iobase.c:282`; `textio.c:1492,1634,1863,2664,2715,2849,3059,3253`.
   Every one of these dispatches `self.flush()` through normal attribute lookup, so every one is a
   user-code entry point. **A fix must not assume any `self->` field survives a `_PyFile_Flush`.**
   That is the whole gh-143008 class.
2. **`_PyFileIO_closed` (`_iomodule.h:52`, impl `fileio.c:94-97`) — an unchecked cast across a file
   boundary.** `return (PyFileIO_CAST(self)->fd < 0);`, no type check, header says so. Two callers:
   `bufferedio.c:367` (inside `IS_CLOSED`, gated on `self->fast_closed_checks`) and `textio.c:1577`
   (gated on `self->raw != NULL`). Both gates are set at `__init__` from `Py_IS_TYPE(raw, FileIO_Type)`.
   **A fix that changes when `self->raw` is cached, or that clears `raw` without also clearing
   `fast_closed_checks`/`ok`, turns this into a type-confused read.** I checked the current
   reachability of `_PyFileIO_closed(NULL)`: `detach()` sets `raw = NULL` and `ok = 0` with no
   Python call in between (`:626-628`), and every `IS_CLOSED` site is preceded by `CHECK_INITIALIZED`
   — so the window does not currently exist. **That negative is fragile and is a direct constraint
   on any reordering in `detach`/`close`.**
3. **`_PyIOBase_finalize` (`_iomodule.h:44-48`) — object revival.** Called from
   `iobase.c` `tp_finalize`, `bufferedio.c:430`, `textio.c`, `fileio.c`. Header: *"will revive an
   object ready to be deallocated and try to close() it... returns -1 if it is alive again."*
   `buffered_dealloc` (`:424-446`) `return`s early when it revives. **A fix that adds cleanup to
   `buffered_dealloc` must place it after the revival check or it will not run.**
4. **`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` — 14 sites (bytesio 11, textio 3), debug-only.**
   The `_lock_held` contract in `bytesio.c` is enforced by nothing in a release build. **A fix that
   moves a `*_lock_held` helper to a new caller will pass all release testing and fail only on a
   debug FT build.**
5. **`find_io_state_by_def` / `get_io_state_by_cls` (`_iomodule.h:177-191`) — 19 call sites,
   assert-only error handling.** `find_io_state_by_def` does `PyType_GetModuleByDef(...)` then
   `assert(mod != NULL)`; in a release build a NULL return feeds `_PyModule_GetState(NULL)`.
   Counts: bufferedio 7+5, fileio 1+6, iobase 4+3, textio 2+4, stringio 2+0, bytesio 1+1.
   **A fix that introduces a new type into the hierarchy, or calls one of these from a slot
   reachable on a non-`_io` type, has no error path.**
6. **`_PyObject_IsUniquelyReferenced` (`bytesio.c:78-82`, via `SHARED_BUF`).** FT refcount
   introspection gating the zero-copy buffer. **Any fix touching `bytesio`'s buffer lifetime has to
   preserve the three-state invariant at `:39-44` *and* the undocumented `buf_shared` flag (C5).**
7. **Deprecated internals** (`scan_deprecated_apis`, 2 findings, its own classification):
   `bytesio.c:110` `_PyBytes_Resize` (soft, CONSIDER) and `stringio.c:230`
   `_PyUnicodeWriter_WriteStr` (hard-internal, **FIX**, removal 3.18). Also `_iomodule.h:9`
   `#include "structmember.h"`, which the scanner did not flag.

### Coupling shape

`_iomodule.h` is the only intra-module header; there is no `_io` → `_io` `.c`-to-`.c` include. The
seven files are coupled through five exported C symbols (`_PyIOBase_check_{readable,writable,seekable,closed}`,
`_PyIOBase_finalize`, `_PyFileIO_closed`, `_PyIncrementalNewlineDecoder_decode`,
`_PyIO_find_line_ending`, `_PyIO_trap_eintr`, `_PyIOBase_cannot_pickle`, `PyNumber_AsOff_t`) and
through the 14 `PyType_Spec`s that `_iomodule.c` wires together. **No file-scope mutable state
anywhere in the seven files** — the only file-scope array is `textio.c:894 static const
encodefuncentry encodefuncs[]`.

---

## 2. Type hierarchy and the re-entrancy attack surface (task b)

### Hierarchy, as actually constructed

Built in `_iomodule.c:683-717`, in this order. Bases come from the third `ADD_TYPE` argument, not
from any `Py_tp_base` slot (there are none in the module).

```
object
├── _io.IncrementalNewlineDecoder   nldecoder_object   textio.c:3451   [base NULL]
├── _io._BytesIOBuffer              bytesiobuf         bytesio.c:1351  [base NULL, DISALLOW_INSTANTIATION]
└── _io._IOBase                     iobase             iobase.c:889    [base NULL]
    ├── _io._RawIOBase              (no struct)        iobase.c:1050
    │   ├── _io.FileIO              fileio             fileio.c:1343
    │   └── _io._WindowsConsoleIO   winconsoleio       winconsoleio.c:1257   [Windows only, out of review scope]
    ├── _io._BufferedIOBase         (no struct)        bufferedio.c:2543
    │   ├── _io.BytesIO             bytesio            bytesio.c:1260
    │   ├── _io.BufferedReader      buffered           bufferedio.c:2606
    │   ├── _io.BufferedWriter      buffered           bufferedio.c:2664
    │   ├── _io.BufferedRWPair      rwpair             bufferedio.c:2714
    │   └── _io.BufferedRandom      buffered           bufferedio.c:2782
    └── _io._TextIOBase             (no struct)        textio.c:211
        ├── _io.StringIO            stringio           stringio.c:1100
        └── _io.TextIOWrapper       textio             textio.c:3517
```

The three abstract middles (`_RawIOBase`, `_BufferedIOBase`, `_TextIOBase`) set `basicsize` to 0 and
**deliberately omit `Py_TPFLAGS_HAVE_GC`** so `tp_traverse`/`tp_clear` are inherited from `_IOBase`
— comments at `iobase.c:1049`, `bufferedio.c:2542`, `textio.c:210`. They contribute only
`Py_tp_doc`, `Py_tp_methods` (and `Py_tp_getset` for `_TextIOBase`). Every one of their methods is a
stub raising `UnsupportedOperation`.

Three C structs back six concrete types: `buffered` backs BufferedReader, BufferedWriter and
BufferedRandom (identical `basicsize`, differing only in slot tables and `__init__`); `rwpair` backs
BufferedRWPair alone; the rest are 1:1.

### Slot inheritance vs override

| type | dealloc | traverse/clear | repr | iter | iternext | init | new | buffer |
|---|---|---|---|---|---|---|---|---|
| `_IOBase` | `iobase_dealloc` | own | — | `iobase_iter` | `iobase_iternext` | — | inherit `object` | — |
| `_RawIOBase` | inherit | **inherit** (no HAVE_GC) | inherit | inherit | inherit | inherit | inherit | — |
| `_BufferedIOBase` | inherit | **inherit** | inherit | inherit | inherit | inherit | inherit | — |
| `_TextIOBase` | inherit | **inherit** | inherit | inherit | inherit | inherit | inherit | — |
| `FileIO` | own | own | own | inherit | inherit | own | **own** | — |
| `BytesIO` | own | own | inherit | `PyObject_SelfIter` | own | own | **own** | — |
| `BufferedReader` | `buffered_dealloc` | `buffered_*` | `buffered_repr` | inherit `_IOBase` | `buffered_iternext` | own | inherit | — |
| `BufferedWriter` | `buffered_dealloc` | `buffered_*` | `buffered_repr` | inherit | **inherit `iobase_iternext`** | own | inherit | — |
| `BufferedRWPair` | own | own | **inherit** | inherit | inherit | own | inherit | — |
| `BufferedRandom` | `buffered_dealloc` | `buffered_*` | `buffered_repr` | inherit | `buffered_iternext` | own | inherit | — |
| `StringIO` | own | own | inherit | inherit `_IOBase` | own | own | **own** | — |
| `TextIOWrapper` | own | own | own | inherit | own | own | inherit | — |
| `IncrementalNewlineDecoder` | own | own | inherit | inherit `object` | inherit | own | inherit | — |
| `_BytesIOBuffer` | own | traverse only, **no clear** | — | — | — | — | none (DISALLOW) | `bf_getbuffer`/`bf_releasebuffer` |

Two asymmetries worth an agent's attention:
- **BufferedWriter has no `Py_tp_iternext`** while BufferedReader and BufferedRandom do. It falls
  back to `iobase_iternext` (`iobase.c:632`), which calls `self.readline()` through Python. On a
  write-only object that is a different, slower, *user-overridable* code path.
- **`_BytesIOBuffer` declares `Py_tp_traverse` but no `Py_tp_clear`** while carrying
  `Py_TPFLAGS_HAVE_GC` and holding a strong `bytesio *source`. A GC cycle through it can only be
  broken via the bytesio side.

### The re-entrancy attack surface — the part a 7-file grep does not give you

**Class 1 — methods C calls on `self`, so a Python subclass override runs on the parent's behalf.**
These are the ones where `class Evil(io.BufferedReader): def flush(self): ...` takes control inside
a C frame.

| receiver | method | call sites |
|---|---|---|
| `self` | `flush` (via `_PyFile_Flush`) | `iobase.c:282`; `bufferedio.c:582,622`; `textio.c:1492,1634,2664,2715,2849,3059,3253` |
| `self` | `seek` | `iobase.c:137` |
| `self` | `closed` (getattr) | `iobase.c:204,306` |
| `self` | `seekable` / `readable` / `writable` | `iobase.c:416,449,482` |
| `self` | `close` | `iobase.c:510` |
| `self` | `peek` (getattr) | `iobase.c:577` |
| `self` | `read` | `iobase.c:632,979` |
| `self` | `readline` | `iobase.c:690`; `textio.c:3281`; `stringio.c:425` |
| `self` | `write` | `iobase.c:816` |
| `self` | `readall` / `readinto` | `iobase.c:928,936` |
| `self` | `__IOBase_closed` / `_finalizing` (**setattr**) | `iobase.c:284,319` |
| `self` | `name` / `mode` (getattr) | `textio.c:3089,3105` |

Note `iobase.c:284` and `:319` are `PyObject_SetAttr` on `self` — a subclass with `__setattr__` or a
`name`-shadowing descriptor gets control during **finalization**.

**Class 2 — methods C calls on a user-supplied wrapped object.** Unconditionally arbitrary; no
subclassing needed, just pass a Python object as `raw`/`buffer`.

| receiver | methods | file |
|---|---|---|
| `self->raw` | `_dealloc_warn`(491) `close`(591) `closed`(526,545) `fileno`(714) `flush`(517) `isatty`(727) `mode`(699) `name`(685) `read`(1748) `readable`(657) `readall`(1713) `readinto`(1640) `seek`(818) `seekable`(644) `tell`(788) `truncate`(1485) `writable`(670) `write`(1996) — **18 names, 22 receiver sites** | bufferedio.c |
| `self->buffer` | via `buffer_getattr`(770) / `buffer_callmethod_*`(781,792) / direct: `raw`(1294) `seekable`(1305) `seek`(2681) `flush`(1863) | textio.c |
| `self->encoder` | `encode`(1783) `reset`(2564) `setstate`(2568) | textio.c |
| `self->decoder` | `decode`(1003,2775) `reset`(633,1874,2546,2671) `setstate`(611,2549) `newlines`(3364) `getstate`(551,1952,2889,2896) | textio.c |
| `self->decoder` | `newlines`(1045) | stringio.c |
| `raw` / `opener` / `wrapper` | `_blksize`(367) `_isatty_open_only`(349) `close`(446) `mode` setattr(438); `opener(...)`(fileio.c:440); `name` setattr(fileio.c:514) | _iomodule.c, fileio.c |

**Class 3 — the leaves.** `bytesio.c` has **zero** Class-1/2 dispatch. Its only user-code entry is
`PyObject_GetBuffer(b, ...)` at `:241` (a user `__buffer__`). `fileio.c` has three:
`RawIOBase._dealloc_warn`(167), the `opener` callable(440), and a `name` setattr on self(514). Those
two files are where a re-entrancy finding is *least* likely and where a scanner zero is most
believable.

**The practical rule for the other nine agents:** anything reachable from Class 1 or Class 2 is a
re-entrancy point, and the question to ask after it is the gh-143008 question — *what did this
function read before the call that it still trusts after?* The population is 22 `self->raw`
receiver sites in bufferedio (no accessor, no re-checks) versus 30 accessor-mediated sites in textio
(all re-checked).

### C-vs-`_pyio` MRO divergence — a caveat on the differential oracle

Measured on `release-gil-nojit`:

```
issubclass(StringIO, TextIOWrapper):        C=False  _pyio=True    DIVERGES
issubclass(BufferedRandom, BufferedWriter): C=False  _pyio=True    DIVERGES
issubclass(BufferedRandom, BufferedReader): C=False  _pyio=True    DIVERGES
issubclass(BytesIO, BufferedIOBase):        C=True   _pyio=True    (same)
issubclass(BufferedReader, BufferedIOBase): C=True   _pyio=True    (same)
```

`_pyio.StringIO` subclasses `TextIOWrapper` (`Lib/_pyio.py:2747`) while `_io.StringIO` subclasses
`_TextIOBase`. `_pyio.BufferedRandom` is `(BufferedWriter, BufferedReader)` (`:1423`) while
`_io.BufferedRandom` is a flat `_BufferedIOBase` subclass. Most other apparent divergences are
papered over by the ABC `register()` calls in `Lib/io.py:83-98`; these three are not.

**Implication for the oracle:** a differential harness that branches on `isinstance`/`issubclass`
will take different paths on the two backends *before* reaching the code under test. Compare
behaviour and exit codes, not type relationships. This also means `_pyio.StringIO` inherits
TextIOWrapper's `_pyio` implementation of methods that `_io.StringIO` implements standalone — an
exception-type mismatch there is a `_pyio` artifact, not necessarily a C contract violation.

---

## 3. Module state and initialization (task d)

### Multi-phase init, `_iomodule.c:723-747`

```c
static struct PyModuleDef_Slot iomodule_slots[] = {
    _Py_ABI_SLOT,
    {Py_mod_exec, iomodule_exec},
    {Py_mod_multiple_interpreters, Py_MOD_PER_INTERPRETER_GIL_SUPPORTED},
    {Py_mod_gil, Py_MOD_GIL_NOT_USED},
    {0, NULL},
};
```

`m_size = sizeof(_PyIO_State)`, `m_traverse`/`m_clear`/`m_free` all present and complete
(`:565-620`). `PyInit__io` is a bare `PyModuleDef_Init`. This is textbook-correct PEP 489 / PEP 630.

`Py_MOD_PER_INTERPRETER_GIL_SUPPORTED` + `Py_MOD_GIL_NOT_USED` is a strong claim on a module with
`bufferedio`'s raw `PyThread_type_lock` and `bytesio`'s hand-rolled sharing flag. Nothing in the
static structure contradicts it — the state is fully per-module, the lock is per-object — but it
means **every FT finding in this slice is a finding against a declared guarantee**, not against an
unclaimed one. That raises the severity ceiling relative to the brief's default.

### State struct, `_iomodule.h:145-167`

15 fields: `int initialized`, `PyObject *unsupported_operation`, 13 (14 on Windows) `PyTypeObject *`.
All 14 type pointers plus `unsupported_operation` appear in both `iomodule_traverse` (`:566-588`)
and `iomodule_clear` (`:592-614`). **Traverse/clear coverage is complete.**

**`int initialized` is dead.** `grep -rn 'initialized' Modules/_io/*.c Modules/_io/*.h` returns only
the declaration at `_iomodule.h:146` plus unrelated `CHECK_INITIALIZED` macros and two `char ok;
/* initialized? */` struct comments. Never written, never read. POLICY: remove, or it will
eventually be mistaken for a guard.

### No global state

Zero file-scope mutable objects across the seven files. The only file-scope array is
`textio.c:894 static const encodefuncentry encodefuncs[]`. No `static PyObject *` caches, no
process-global type pointers. Everything goes through `get_io_state` / `get_io_state_by_cls` /
`find_io_state_by_def` (19 call sites, §1 item 5).

### Type creation order, `_iomodule.c:683-717`

1. `DEFAULT_BUFFER_SIZE` int macro.
2. `UnsupportedOperation` — built at runtime by calling `type("UnsupportedOperation", (OSError, ValueError), {})`
   (`:660-662`), then `__module__` set to `"io"` (`:665`), then `PyModule_AddObjectRef` (`:670`).
3. `BlockingIOError` re-exported.
4. Bases with no `_io` parent: `IncrementalNewlineDecoder`, `_BytesIOBuffer`, `_IOBase`.
5. `_IOBase` children: `_TextIOBase`, `_BufferedIOBase`, `_RawIOBase`.
6. `_BufferedIOBase` children: `BytesIO`, `BufferedWriter`, `BufferedReader`, `BufferedRWPair`, `BufferedRandom`.
7. `_RawIOBase` children: `FileIO` (+ `_WindowsConsoleIO`).
8. `_TextIOBase` children: `StringIO`, `TextIOWrapper`.

The `ADD_TYPE` macro (`:638-648`) bails with `return -1` on either failure, leaving already-created
types owned by `state->`; `m_free` → `iomodule_clear` collects them. Order is a strict topological
sort of the base graph — correct, and it is the only place the hierarchy is expressed.

### What a second interpreter or a re-import breaks

**Second interpreter — fine by construction, with one consequence to know.** State is per-module, so
each interpreter gets its own 14 types and its own `UnsupportedOperation` *class object*. Therefore
`io.UnsupportedOperation` from interpreter A is **not** the same object as interpreter B's, and an
`except io.UnsupportedOperation` clause carrying a class across the boundary will not match. Same
for every `isinstance(x, io.BufferedReader)`. This is the intended semantics of
`Py_MOD_PER_INTERPRETER_GIL_SUPPORTED`, not a defect — but it is the thing to check first if a
subinterpreter test reports a spurious "not an instance of".

**Re-import in the same interpreter — genuinely breaks identity.** `del sys.modules['_io']` followed
by `import _io` re-runs `Py_mod_exec` and mints a **second, disjoint** set of 14 types plus a second
`UnsupportedOperation`. Everything already bound to the first set silently stops matching. The
sharpest edge is `Lib/io.py`, which at import time does:

```python
class IOBase(_io._IOBase, metaclass=abc.ABCMeta):     # io.py:71
RawIOBase.register(FileIO)                            # io.py:83
for klass in (BytesIO, BufferedReader, ...): BufferedIOBase.register(klass)   # io.py:87
for klass in (StringIO, TextIOWrapper): TextIOBase.register(klass)            # io.py:90
```

Those `register()` calls bind the *first* generation of C types into the ABC caches. After a
`_io` re-import, `io.BufferedIOBase` still claims the old `_io.BufferedReader` and knows nothing
about the new one. `issubclass(new_io.BufferedReader, io.BufferedIOBase)` → False, and ABCMeta's
negative cache makes it sticky.

The subtler one: `TextIOWrapper` caches `self->state` **as a raw `_PyIO_State *`** in its instance
struct (`textio.c:729`) and uses it in `CHECK_CLOSED` (`:1575`, `Py_IS_TYPE(self, self->state->PyTextIOWrapper_Type)`).
That pointer is the module state of whichever generation created the object. It is kept alive by the
instance's reference to its type, which references the module — so it is not dangling. But it does
mean a `TextIOWrapper` from generation 1 compares its type against generation 1's
`PyTextIOWrapper_Type`, which is the right answer, while a naive "is this a TextIOWrapper" check
elsewhere using generation 2's table gets the opposite. Nothing here is a bug at `4f3be1b5777`;
it is the constraint any fix touching `self->state` has to respect.

---

## 4. Classes bounded — real negatives with their denominators

| class | denominator | verdict |
|---|---|---|
| `ENTER_BUFFERED` lock leak | 12/12 spans, 13/13 LEAVE sites, every goto/break/early-return read by hand | **clean** (C2) |
| Dangling-else on `CHECK_*` / `LEAVE_BUFFERED` | all `Modules/_io/*.c`, regex over if/else/while/for bodies | **clean**, 0 hits (C7) |
| Bare `self->buffer` outside the accessor family in textio | 11 occurrences, all classified | **clean**, 0 deviations (C4) |
| Non-zeroing allocation | 4 `tp_alloc(type,0)` sites, 3 `Py_tp_new` slots, 0 `PyObject_New`/`PyObject_GC_New` | **clean**; but see C6 for the re-init analogue |
| Module traverse/clear completeness | 15/15 state fields in both `iomodule_traverse` and `iomodule_clear` | **clean** |
| File-scope mutable state | 7 files scanned; 1 file-scope array, `const` | **clean** |
| `_PyFileIO_closed(NULL)` reachability | 2 call sites, both gated; `detach` sets `raw=NULL` and `ok=0` with no intervening Python call | **clean today, fragile** (§1 item 2) |

---

## 5. Toolkit assessment

### Recall gaps found by reading

1. **`analyze_includes.py` resolves `clinic/*.c.h` but no scanner follows them.** Every
   `scan_*.py` in this run was scoped to the 7 `.c` files, so 87 of the module's 95 critical
   sections were invisible. This is not a `scan_lock_discipline` bug specifically — it is a
   **scope-model** bug that affects every FT/lock/state rule on any Argument-Clinic-heavy file.
   *Proposal:* when a scanned `.c` file `#include`s a `clinic/<same-basename>.c.h`, pull that header
   into the same translation unit for analysis and attribute its findings to the `.c` file. Failing
   that, emit `clinic_critical_sections: N` into `denominators` so a zero is visibly incomplete.

2. **`scan_lock_discipline` has a `local_lock_macros` field and it came back `{}`.** The scanner
   already has the machinery to discover project-local lock macro pairs; it did not discover
   `ENTER_BUFFERED`/`LEAVE_BUFFERED`, which are a textbook instance (a `#define` whose body contains
   `PyThread_acquire_lock` paired with one containing `PyThread_release_lock`, in the same file).
   *Proposal:* seed discovery from the macro *body* vocabulary (`PyThread_acquire_lock`,
   `PyThread_release_lock`, `PyMutex_Lock`, `PyMutex_Unlock`) rather than from the macro name. That
   one change would have turned this slice's structural zero into a real 12-span audit — which
   happens to come out clean, but the scanner should have been the one to say so.

3. **No rule models re-initialization of a live object.** `_buffered_init` frees and reallocates
   `self->buffer` and `self->lock` from a `Py_tp_init` slot with no lock and no critical section
   (C6). `scan_uninit_dealloc` looks only at construction-time non-zeroing allocators;
   `scan_init_bypass` looks only at `__new__`-without-`__init__`. The complementary shape —
   `__init__`-called-twice — is unmodelled. *Proposal:* a rule that flags a `Py_tp_init` impl which
   frees a heap member (`PyMem_Free`, `PyThread_free_lock`, `Py_CLEAR` of a buffer-like field) when
   the type's other methods take a lock the init does not. On this slice it would fire 4 times
   (BufferedReader/Writer/Random `__init__` → `_buffered_init`, plus `StringIO.__init__`).

4. **`@critical_section` annotation asymmetry is a first-class signal nobody computes.** The
   annotated/asserted ratio table in C5 (bytesio 20/11, textio 26/3, stringio 16/0, bufferedio 25/0)
   took one grep and immediately located the weakest file. *Proposal:* emit
   `clinic_critical_sections`, `lock_held_assertions`, and their per-file ratio in
   `scan_lock_discipline`'s denominators block. A file with N annotations and 0 assertions is a
   ranked target, not a clean bill.

5. **Prior-art linkage.** The seeded lead's guarded twin carries `gh-143008` and `gh-142594` in the
   commit that created it, and the shape is one `git log -S buffer_access_safe` away. Nothing in the
   pipeline surfaced that. *Proposal:* when a briefing cites a "guarded twin", have
   `build_informed_briefing.py` run `git log -1 -S '<twin symbol>' -- <twin file>` and paste the
   commit subject into the brief. It costs one git call and converts "novel finding" framing into
   "incomplete fix" framing before ten agents commit to the wrong one.

### Precision of the rules that fired here

I did not triage the findings themselves — that is the other agents' job — but two structural notes
they should carry:

- `scan_ft_races` `bufferedio.c:846,847,849` (medium, `guarded_writer_unguarded_reader`) are the
  `_buffered_init` free/realloc lines. **These are true positives** for the reason in C6, even
  though the rule's stated model (guarded writer / unguarded reader) is not quite the right
  description of why.
- `scan_init_bypass`'s 26 `init_bypass_null_deref` findings split 13/13 across bufferedio and textio
  on `self->raw` / `self->buffer`. The brief's reframing ("can this field be NULL *after* the guard
  ran?") is the right question. Add the population number: **22** of the bufferedio ones are
  `self->raw`-as-receiver sites with no accessor, versus **0** unmediated `self->buffer` sites in
  textio. The 13/13 split is symmetric; the actual exposure is not.
