# Toolkit defects found by the `mod-io` slice

Status key: **FIXED** (committed, tested) · **CONFIRMED** (measured by me, not yet fixed) ·
**REPORTED** (an agent's claim I have not independently measured).

---

## D-1 — FIXED — `measure_c_complexity.py` collapsed functions to zero length

A `#ifdef`/`#else` whose branches each open a brace and share one closing brace
(`Modules/_io/fileio.c:483-491`) left the character-wise counter permanently above depth 0. The
search ran off the end of the file and the loop then left `body_end` at its `body_start`
initialization — **line_count 0, cyclomatic 1, score 1.00**, while `coverage_pct` still counted
the function as parsed, because the *signature* had resolved fine.

Measured over `Objects/` `Modules/` `Python/`: **31 of 18,403 functions** collapsed. Among them
`fileio.c:249 _io_FileIO___init___impl` (really 299 lines, cyclomatic 57, the highest-churn
function in `_io`), `dictobject.c dictiter_iternextitem` — **inside the already-certified
obj-mappings slice** — `ceval.c _Py_CheckRecursiveCall`, and `socketmodule.c sock_initobj_impl`.

Fix (`a33752b`): `_find_body_end` counts braces in the first branch of each preprocessor
conditional only, skips the alternates, and falls back to the first column-0 `}` rather than
collapsing to nothing. New `coverage.extents_unresolved` makes the failure non-silent.
After: **31 → 11**, and all 11 remaining are genuinely empty functions. 7 tests added.

---

## D-2 — CONFIRMED, NOT FIXED — `tree_sitter_utils.extract_functions` swallows whole files

**Same root cause as D-1, different chassis.** tree-sitter-c cannot model a brace that straddles
`#ifdef`/`#else`, so the function node runs to the next construct it can close on.

Measured by me on `4f3be1b5777`:

```
Modules/_io/fileio.c   extract_functions -> 6 functions      (the file has 32)
                       _io_FileIO___init___impl  249-1349    span = 1,100 lines
                                                             (real extent: 249-547)
```

Tree-wide over `Modules/`: 251 files, 8,152 functions, **14 with a span over 400 lines**. Not all
14 are defects — `socketmodule.c socket_exec`, `posixmodule.c all_ins`, `errnomodule.c
errno_exec` and expat's `doProlog` are genuinely that large. Confirmed spurious so far:
`_io/fileio.c _io_FileIO___init___impl` (1,100) and `signalmodule.c signal_siginterrupt_impl`
(691) — the latter also appeared in D-1's zero-length list, which is corroborating.

**Blast radius.** This is the shared C chassis: ten scanners consume it. The concrete consequence
measured on this slice is that `scan_gil_usage` reports `functions_analyzed: 6` for `fileio.c`
and balances `Py_BEGIN/END_ALLOW_THREADS` across a 1,100-line conglomerate — so its clean
negative for that file is **structural, not earned**. The `mod-io` RUN_CONTEXT's
`scan_gil_usage` row must be read with that caveat.

**Not independently verified:** the error-path agent's tree-wide figure of 875 of 8,424 functions
lost (10.4%) across `Modules/`, with 17 files losing ≥30%, `faulthandler.c` losing 43/43 and
`fileio.c` 26/32. The `fileio.c` 26/32 matches my measurement. The rest I have not checked.

**Why it is not fixed here.** Two reasons, both deliberate:

1. **`cext-review-toolkit` is upstream for `tree_sitter_utils.py`.** The rule is never to fork a
   shared file — the fix belongs upstream and gets synced forward to cpython / ft / and the
   Rust-side toolkits that inherit the pattern. That is a cross-repo change deserving its own
   focused pass, not a mid-slice patch.
2. **Six agents were still running scanners that import this module.** Changing it underneath a
   live run would invalidate their measurements.

---

## D-2b — CONFIRMED, NOT FIXED — a macro call without a trailing `;` eats the next statement

**A second, independent trigger for the same chassis blindness as D-2**, and the one with the
wider reach, because it needs no preprocessor conditional at all.

`LEAVE_BUFFERED` is defined as `do { ... } while(0);` — with the semicolon *inside* the macro —
so call sites are written bare. Measured on `bufferedio.c`: **13 call sites, 12 of them with no
trailing `;`**. tree-sitter-c then parses `LEAVE_BUFFERED(self)` as a declaration and swallows
the statement that follows it, typically a `return`.

My measurement (desugaring `LEAVE_BUFFERED` alone, nothing else):

```
as shipped        ERROR= 39  MISSING= 15  return_statement=137  functions=78
with ';' added    ERROR= 28  MISSING= 15  return_statement=146  functions=78
```

So **9 return statements — 6.6% of the file's 137 — are invisible to every tree-sitter scanner
in the toolkit**, in the single file holding this slice's crash cluster.

The lock-discipline agent reported the same 39 ERROR nodes and 137 returns as a baseline but
larger post-desugar deltas (22 ERROR, 157 returns, and 5 phantom functions). Its desugaring
covered more macros than mine. **I have reproduced the mechanism and the direction, not those
specific figures** — my narrower experiment moved the function count not at all.

The strongest evidence for this class is historical, and belongs to that agent: it ran the
shipped `scan_lock_discipline.py` against `git show 375e372c666^:Modules/_io/bufferedio.c` — the
revision that still contained the real, reported, since-fixed leak **gh-143689**
("BufferedReader.read1() leaves object in reentrant state after MemoryError"). Result:
`findings: 0`, `vocabulary_counts: {}`. The scanner did not rank the known bug low; it never
looked at all. That turns the recall gap from a hypothesis into a measured historical miss.

Proposed canary, and I think it is the right one: put `parse_error_nodes` / `parse_missing_nodes`
into every envelope's `denominators`. **A file with 39 ERROR nodes should never be certified
clean by anything.** That is to the chassis what `vocabulary_counts` already is to the
vocabulary.

Also confirmed while checking this: `lock_macros.json`'s `ENTER_ZLIB` and `ENTER_BROTLI` entries
are **dead data** — 0 occurrences across `Modules/` and `Objects/` — and they are the only
`extension_lock`-family entries, a family `_MODELLED_FAMILIES` does not model anyway.

---

## D-3 — CONFIRMED, NOT FIXED — `scan_error_paths.find_functions` is blind to Argument Clinic

Verified by me directly:

```
scan_error_paths.find_functions(bufferedio.c) -> 38 functions
   _io__Buffered_close_impl     MISSING     <- the SIGSEGV site
   _io__Buffered_detach_impl    MISSING
   _io__Buffered_seek_impl      MISSING
   _buffered_init               FOUND
```

It requires the whole signature on the line directly above the `{`. Argument Clinic puts
`/*[clinic end generated code: ...]*/` there, so **every clinic `_impl` is invisible** — which is
to say, the scanner is structurally blind to exactly the functions where this slice's bugs live.

The agent measured 145 of 289 functions missed across the slice (50%), and only 32% of
`bufferedio.c`'s lines analyzed; `functions_analyzed: 157` in the envelope understates the true
289 by 46%. I confirmed the mechanism and the `bufferedio.c` numbers, not the slice-wide ratio.

**This is a propagation gap inside the toolkit itself** — pleasingly on-theme for a slice whose
headline finding is an unpropagated fix. `scan_refcounts.py` already solves this and says so in
its docstring; `measure_c_complexity.py:186-190` fixed the sibling multi-line-signature case and
documented it as "22.4% of functions on a measured CPython sample". Neither fix reached
`scan_error_paths.py`.

Deferred for the same live-run reason as D-2. Unlike D-2 this one is local to
`cpython-review-toolkit` and the fix already exists in a sibling script to copy.

---

## D-7 — CONFIRMED, NOT FIXED — my own `_lock_coverage()` fix is one rename from regressing

The `scan_ft_races._lock_coverage()` change I shipped in the obj-mappings slice replaced a
blanket "this function mentions a lock, suppress all of it" with per-span coverage — because that
blanket had hidden TSAN-0053, TSAN-0054 and TSAN-0062, every instance of the class the rule
exists for.

It only narrowed the suppression for `Py_BEGIN/END_CRITICAL_SECTION`, which is delimitable.
Everything else still falls back to whole-function suppression, and whether that fires is decided
by `_LOCK_MACRO_RE = r"\b[A-Z][A-Z0-9_]*LOCK[A-Z0-9_]*\s*\("` — **a name pattern**. I ran the
same function body both ways:

```
ENTER_BUFFERED / LEAVE_BUFFERED (as CPython spells it)
    spans=[]  opaque=False  -> suppressed=False
identical body, macros renamed ACQUIRE_BUFFERED_LOCK / RELEASE_BUFFERED_LOCK
    spans=[]  opaque=True   -> suppressed=True    <- whole function goes dark
```

So on `bufferedio.c` the rule is simply blind to the file's real lock (12 spans, invisible), and
had CPython chosen a name containing `LOCK`, the fallback would have swallowed
`buffered_iternext` whole — reinstating the exact defect the fix removed, on a live catalogued
race, with no code change at all. Credit to the ft-race agent for the framing: *the regression is
one rename away.*

The right fix is the same capability D-2/D-2b/D-3 keep pointing at, and two agents proposed it
independently: **discover lock macros from their `#define` bodies** — does the expansion call
`PyThread_acquire_lock` / `PyMutex_Lock` / `PyThread_release_lock` — rather than from their
names. `scan_lock_discipline` already has a `local_lock_macros` facility for this that returned
`{}` on this slice for the same naming reason.

Interim hardening worth shipping alongside it: put `functions_suppressed_opaque_lock` in the
envelope, so a suppression this broad can never again be invisible in the denominators.

---

## D-4 — REPORTED — `scan_gil_usage`'s `rule_not_applicable` poisons a merged run

Per the gil-discipline agent: the flag merges across files with policy `"or"`, so five
zero-vocabulary files make the whole slice report "recognised NONE of its vocabulary" while
sitting next to `vocabulary_resolved: 16`. Proposed fix is to recompute it from the merged
`vocabulary_counts`. Not independently measured by me.

Same agent reports the true GIL-released count for the slice is 12, not 8, because `_Py_read` /
`_Py_write` release internally — so the hand-audit denominator should be 12.

---

## D-5 — REPORTED — `scan_pyerr_clear` misses file-local static wrappers and drops empty files

Per the pyerr-clear agent: no depth-1 intra-file closure over static wrappers, costing four
sibling sites (`bufferedio.c:578`, `:870`, `:1490`, `textio.c:3250`); and `if not functions:
continue` drops a file with no trace, which is how `winconsoleio.c` contributes 0 to a run whose
envelope still reports it as analyzed. Not independently measured by me.

---

## D-6 — REPORTED — no rule expresses this slice's dominant bug shape

Three agents independently proposed the same missing rule under different names
(`stale_slot_after_python_call`, `stale_field_guard`, `lock_freed_while_reachable_as_held`).
The shape: a **struct field** that was non-NULL when the guard ran and NULL — or freed — when
used, because a call in between ran user Python. It produces no assignment site, so the
NULL-safety scanner (which reasons about locals fresh from a call) cannot see it at all: it
scored 0 findings against a 1,012-site denominator on a file family holding nine reproduced
crashes.

Load-bearing sub-requirements the slice proves are necessary: expand macro bodies and treat
`CHECK_*` as a **re-entrancy point** rather than only a guard; read the sibling `clinic/*.c.h` to
learn whether a parameter converts before or inside the guard (that alone separates `bytesio`'s
clean `seek` from its crashing `truncate`); and put `PyLong_As*` / `PyNumber_As*` /
`PyObject_GetAttr` in the Python-reaching set.

Design and cost this after the slice's findings are recorded, not before.
