# stw-safety-checker — slice `obj-sequences` (Group B2, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** carried from the mapper and re-confirmed by three prior agents — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files plus the three `clinic/*.c.h` is empty. Every line number and every gdb offset below is valid on every matrix build without adjustment.

**Read first:** `AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/gil-discipline-checker.md`, `agents/uninitialized-dealloc-auditor.md`, `preflight/informed_briefing.md`.

**Pre-run scanner:** `scan_stw_safety.sample.json` — 0 findings, `stw_functions: 0`, `stw_wrappers: 0`, `function_classifications: {}`.

---

## Headline

**(a) The structural zero is real and I can put a number on it: 0 occurrences of 12 STW spellings across 7 files.** My rule does not apply to this slice directly. That is the whole of the direct finding, and I am not manufacturing one.

**(b) The severity question is the answer worth having, and it is now measured, not argued.** A Python-chosen `lo` in CPY-0186 walks **forward only** (`lo` is clamped `>= 0` at `bytearrayobject.c:667`), from `&bytes_empty.ob_sval[0]` through 73,024 B (GIL) / 95,200 B (FT) of interned singletons and then through the **entire** `PyInterpreterState` — 226,256 B (GIL) / 249,536 B (FT) — to the end of `_PyRuntime` and beyond. `_PyRuntime._main_interpreter.stoptheworld` sits at a **measured** delta of **+83,800 / +106,016** and is 100 % Python-reachable.

I aimed the write there. **On both free-threaded builds a single 0xFF byte written over `stoptheworld.mutex` makes the process hang permanently in `gc.freeze()`**; the other six fields of the struct, corrupted identically, are harmless because `stop_the_world()` re-initialises them. **On `release-gil-nojit` the identical write at the identical offset produces the identical `len()` → `SystemError` and then every stop-the-world probe completes normally**, because `_PyEval_StopTheWorld` is `#ifdef Py_GIL_DISABLED` (`Python/pystate.c:2547`). So CPY-0186 has a **failure mode on the free-threaded build that the GIL build does not have: a silent, permanent deadlock of the interpreter's entire pause facility rather than a crash.** That is the concrete form of Group B's `[CONSIDER] G5` third bullet, which they recorded as *"structural, and I could not turn it into a measurement."*

**(c) The transitive form of my rule does apply, and it is clean.** On the free-threaded build `deduce_unreachable_heap` and `handle_resurrected_objects` run **inside** `_PyEval_StopTheWorld` (`Python/gc_free_threading.c:2067-2141` and `:2151-2161`), so the slice's **five `tp_traverse` functions execute with the world stopped**. All five are pure `Py_VISIT` — direct struct reads plus the GC's own `visitproc`. `tp_clear` and every `tp_dealloc` run **after** `_PyEval_StartTheWorld`, by upstream's explicit design. **0 violations over a denominator of 5.**

---

# 1. Task (a) — the structural zero, with the number

`scan_stw_safety` reports `stw_functions: 0`. **Chassis caveat, and it is why I used grep and not the AST:** `tree_sitter_utils.extract_functions` truncates `Objects/bytesobject.c` at 2,779 of 3,926 lines (`parse_health` coverage 0.5667, 37 ERROR nodes — measured by `uninitialized-dealloc-auditor` §3), so the scanner's `functions_analyzed: 331` misses ~1,147 lines and its AST-derived zero is not by itself trustworthy. A token-level sweep over the raw bytes is truncation-proof.

Hand-verified over **7 files** — `Objects/{listobject,bytesobject,bytearrayobject,bytes_methods}.c` (12,100 lines) plus `Objects/clinic/{listobject,bytesobject,bytearrayobject}.c.h`:

| spelling | occurrences |
|---|---|
| `_PyEval_StopTheWorld`, `_PyEval_StartTheWorld` | **0**, **0** |
| `_PyRuntimeState_StopTheWorld`, `_PyRuntimeState_StartTheWorld` | **0**, **0** |
| `stop_the_world`, `start_the_world`, `stop_world`, `start_world`, `STOP_THE_WORLD` | **0** each |
| `types_stop_world`, `types_start_world` | **0**, **0** |
| `_PyEval_IsGILEnabled` | **0** |

**0 hits over 12 spellings × 7 files.** For calibration, the tree carries **58** `_PyEval_StopTheWorld` references in `Objects/` + `Python/` + `Modules/` + `Include/`. None is in this slice. This is the *"no constructs present"* case, and unlike `scan_gil_usage`'s zero it needed no vocabulary rescue: the scanner's zero is correct, it is just AST-derived, and the grep is what makes it safe to rely on.

## 1.1 Phase 2, the intra-file blind spot — checked in the other direction, and it is also zero

The agent contract's honest limitation is that the call graph is intra-file: *a function the scanner classified `stw_unsafe` may be called from a StopTheWorld region in a different file the scanner never connected.* I ran that check by hand (`scratchpad/stw_region_callers.py`): extract every `_PyEval_St{op,art}TheWorld[All]` span tree-wide, then intersect the calls inside each span with the **373** function names *defined* in the four slice files, plus every `PyList_* / _PyList_* / PyBytes_* / _PyBytes_* / PyByteArray_* / _Py_bytes_*` spelling.

**56 candidate spans across 21 files. 3 are not regions** — `Python/tracemalloc.c:893` is a comment (verified by reading it) and `Python/pystate.c:2531`/`:2547` are the `_PyEval_StopTheWorld*` **definitions**. **53 real Stop…Start regions; 0 of them call anything defined in this slice.**

And upstream says why, twice, in its own comments:

```c
Python/gc_free_threading.c:2393   // NOTE: We can't append to the PyListObject during gc_visit_heaps()
Python/gc_free_threading.c:2394   // because PyList_Append() may reclaim an abandoned mimalloc segments
Python/gc_free_threading.c:2395   // while we are traversing them.
```
(identically at `:2437-2439`). In both `_PyGC_GetReferrers` and `_PyGC_GetObjects` the results are accumulated into a raw `_PyObjectStack` inside the region and converted with `list_from_object_stack` **after** `_PyEval_StartTheWorld`. That is the guarded twin for the cross-file blind spot, written by the maintainers, and it is honoured at every site.

---

# 2. Task (b) — what a Python-chosen `lo` reaches, and which of it is stop-the-world state

## 2.1 The bound on `lo`: forward only, unbounded upward

`bytearray_setslice:667` clamps `if (lo < 0) lo = 0;`, and the `mp_ass_subscript` path (the one the ASan stack in CPY-0186 shows) derives `lo` from `PySlice_AdjustIndices`, which is also non-negative. `hi` is clamped to `Py_SIZE(self)` at `:671`, and `lo <= hi`. So:

> **`lo ∈ [0, len(b)]`, and `len(b)` is whatever the attacker allocated.** Nothing below the write base is reachable; everything above it is, out to the end of `_PyRuntime` and past it (Group A2's ASan `global-buffer-overflow` at `lo=200000` with a 100 kB payload is that overrun).

That immediately settles one question: **the *global* stop-the-world state is out of reach.** `_PyRuntime.stoptheworld` and `_PyRuntime.stoptheworld_mutex` sit at deltas **−36,552 / −53,048** and **−36,560 / −53,056** (gdb-measured, debug-gil / debug-ft) — *behind* the write base, because `static_objects` is the second-to-last field of `struct pyruntimestate`. Only the **per-interpreter** `_main_interpreter.stoptheworld` is reachable.

## 2.2 The measured map

All four numbers are byte deltas from `&_PyRuntime.static_objects.singletons.bytes_empty.ob_sval[0]`, read out of the matrix binaries with gdb (`scratchpad/stw_layout.gdb`, `stw_layout2.gdb`). `— (FT)` = the field does not exist on a GIL build.

| delta rel-gil | delta rel-ft | region | STW / GC / sync? |
|---:|---:|---|---|
| +8 | +8 | `singletons.bytes_characters[256]` (12,288 / 16,384 B) | no — the shared 1-byte `bytes` (CPY-0186's `singleton` scenario) |
| +12,296 | +16,392 | `singletons.strings` — every `_Py_ID()` immortal str (~60.6 kB / ~78.6 kB) | no, but every dict lookup by name reads it |
| +72,912 | +94,992 | `tuple_empty`, `hamt_bitmap_node_empty`, `context_token_missing` | no |
| **+73,024** | **+95,208** | **`_main_interpreter` begins** — `interp.ceval` | `ceval.gil` +73,040/+95,224; `ceval.pending.mutex` +73,064/+95,248 |
| +80,360 | +102,544 | `interp.threads.head` / `.main` / `.count` | **yes** — the thread list `stop_the_world` walks under `HEAD_LOCK` |
| +80,424 | +102,608 | **`interp.gc`** — `enabled`, generations, `collecting` (+80,536/+102,724), `garbage`, `callbacks`, `freeze_active`, `long_lived_total` | **yes** |
| **+83,800** | **+106,016** | **`interp.stoptheworld`** (24 B) | **yes — see §2.3** |
| +83,824 | +106,040 | `interp.qsbr` (`_qsbr_shared`, 56 B) | **yes** — deferred-free reclamation |
| — | +106,152 | `interp.mimalloc` (320 B) | **yes (FT)** — the heaps `gc_visit_heaps` walks *while the world is stopped* |
| — | +106,472 | `interp.brc` (6,168 B) | **yes (FT)** — biased-refcount queues, merged inside STW at `gc_free_threading.c:2093` |
| — | +112,672 | `interp.weakref_locks[127]` | **yes (FT)** |
| *(dbg 83,912)* | *(dbg 112,856)* | `interp.obmalloc` — I measured this one on the **debug** builds only | allocator state |
| +154,968 | +183,264 | `interp.types` | type-version / TYPE_LOCK state |
| +298,176 | +326,520 | **`interp._initial_thread`** (`_PyThreadStateImpl`, 18,200 B, the **last** field): `.base.next` +298,184/+326,528, `.base.eval_breaker` +298,200/+326,544, `.base.state` +298,224/+326,568, `.qsbr` +299,240/+327,584 | **yes** — `stop_the_world` *works* by CAS-ing `t->state` and setting `_PY_EVAL_PLEASE_STOP_BIT` in `t->eval_breaker` (`Python/pystate.c:2418-2426`) |
| +299,264 | +342,312 | **end of `_PyRuntime`** | past here is whatever `.bss` follows |

Group B's +17.2 % measurement is confirmed and localised: the extra FT bytes in the blast radius are `mimalloc` (320) + `brc` (6,168) + `weakref_locks` (127) + wider static objects + the per-object `ob_tid`/`ob_ref_local`/`ob_ref_shared` inflation of the singletons.

**Two of these have consequences that are qualitatively worse than "a wrong byte":**

1. **`interp._initial_thread.base.state` / `.eval_breaker` (+326,544/+326,568 on release-ft).** These are not read *by* an STW region — they are the mechanism `stop_the_world` uses to *stop threads*. A corrupted `state` that is neither `_Py_THREAD_ATTACHED` nor `_Py_THREAD_DETACHED` is counted into `thread_countdown` at `:2469` and never parks, so `PyEvent_Wait(&stw->stop_event)` never returns. Structural; **not measured** (`lo ≈ 326,520` needs a >653 kB bytearray and I did not sweep it).
2. **`interp.mimalloc` (+106,152, FT only).** `gc_visit_heaps` walks these heaps *with the world stopped* — the one place in CPython where corrupt allocator metadata is read while every other thread is frozen and cannot be scheduled to notice. Structural; **not measured.**

## 2.3 Measured: one byte at `stoptheworld.mutex` hangs the free-threaded interpreter

`struct _stoptheworld_state` (`Include/internal/pycore_interp_structs.h:412-425`) is 24 bytes. `stop_the_world()` (`Python/pystate.c:2439-2470`) re-initialises **four** of its seven fields on every entry:

```c
2450    PyMutex_Lock(&stw->mutex);          /* NOT re-initialised -- persistent */
2452    stw->requested = 1;
2453    stw->thread_countdown = 0;
2454    stw->stop_event = (PyEvent){0};
2455    stw->requester = _PyThreadState_GET();
```

So the *prediction* is that only `mutex` (persistent) and `is_global` (set once at interpreter creation) matter. **Measured** — `repro/stw_field_granularity.py`, `release-ft-nojit`, one 0xFF byte per field, injection index found by sweep, `len(b)` → `SystemError` proving the OOB write executed in **every** row:

| offset | field | outcome |
|---:|---|---|
| **+0** | **`mutex`** | **TIMEOUT — permanent hang** |
| +1 | `requested` | rc=0, `gc.freeze`/`unfreeze`/`collect` all fine |
| +2 | `world_stopped` | rc=0, fine |
| +3 | `is_global` | rc=0, fine |
| +4 | `stop_event` | rc=0, fine |
| +8 | `thread_countdown` | rc=0, fine |
| +16 | `requester` | rc=0, fine |

**1 of 7 bytes is load-bearing, and it is the lock.** `PyMutex_Lock` on a byte with `_Py_LOCKED` already set parks a thread waiting for an unlock that will never come, because nobody holds it.

**A prediction of mine that the measurement falsified.** I expected `is_global = 0xFF` to hang too, because `interp_for_stop_the_world` (`:2398`) then returns `PyInterpreterState_Head()` instead of the containing interpreter and `stop_the_world` takes the runtime rwmutex as a *writer* rather than a *reader*. It does not hang: in a single-interpreter process `PyInterpreterState_Head() == &_PyRuntime._main_interpreter`, `i->next` is NULL, and `start_the_world` reads the same corrupted `is_global` so the rwmutex is released the same way it was taken. **Whether it matters with subinterpreters is unmeasured** — falsifier: repeat with a live `interpreters.create()`.

### The whole-struct probe, across the build matrix

`repro/stw_runtime_oob_target.py` — `bytearray(b'x'*400_000)`, `b[lo:400000] = b'\xFF'*24` with `lo` = the measured `stoptheworld` offset, `_testcapi.set_nomemory` swept 0..13, probes `gc.freeze` → `gc.unfreeze` → `gc.collect` → `gc.get_objects`.

| build | `lo` | OOB write executed? | STW probes |
|---|---:|---|---|
| `release-ft-nojit` | 106,016 | yes, n=2 (`len` → `SystemError`) | **TIMEOUT (hang)** |
| `debug-ft-nojit` | 106,016 | yes, n=2 | **TIMEOUT (hang)** |
| `release-gil-nojit` | 83,800 | yes, n=2 **and** n=3 (`len` → `SystemError`) | **all clean, rc=0** |
| `debug-gil-nojit` | 83,808 | yes, n=2/3 | aborts in `len()` first — `Fatal Python error: _Py_CheckSlotResult: Slot __len__ of type bytearray failed without setting an exception` (the already-recorded CPY-0186 debug symptom) |

**Control, same payload, same injection index, different target** (`lo = 8`, i.e. `bytes_characters`): rc=0 with all STW probes clean on **all four** builds. **Control, no injection:** rc=0 everywhere.

**Naming the hang** (`scratchpad/stw_hang_where.py`, `faulthandler.dump_traceback_later(8, exit=True)`):

```
Timeout (0:00:08)!
Thread 0x0000711aea8e3780 [python] (most recent call first):
  File "<string>", line 22 in <module>          <- gc.freeze()
```
identically on `release-ft-nojit` and `debug-ft-nojit`; the `lo=8` control on the same builds prints `FREEZE-RETURNED / DONE`. `gc.freeze()` → `_PyGC_Freeze` → `_PyEval_StopTheWorld` (`Python/gc_free_threading.c:2492`).

**Why the GIL row is the strongest evidence, not the weakest.** Both build families show the *same* write landing at the *same* measured offset (`len(b)` raises `SystemError` in both). The only thing that differs is whether `_PyEval_StopTheWorld` compiles to anything. So this is not a timing artefact and not an allocator-layout artefact — it is the STW machinery itself.

### What this changes for CPY-0186's record

Add one line: **on a free-threaded build the corruptible region contains a lock whose corruption deadlocks the interpreter's entire stop-the-world facility — every subsequent `gc.collect()`, `gc.freeze()`, `sys.settrace`/`setprofile`, `PyType_Freeze`, `type.__name__` assignment, `func.__code__` assignment, and `Py_Finalize` — with no crash, no exception and no traceback.** Detection-hostile, and 100 % Python-reachable at a fixed offset. Severity of the existing finding goes up on FT; the finding itself is not re-litigated.

**Prior art: none.** `gh api -X GET search/issues` for `stoptheworld corruption` (0 hits), `_PyRuntime out of bounds write`, `stop the world deadlock PyMutex`, `bytearray _PyRuntime overflow` returns only gh-91153 (the 2022 bytearray `__index__` UAF) and unrelated FT deadlock reports. Nothing in `cpython-review-findings`.

---

# 3. Task (c) — the transitive form: `tp_traverse` in this slice **does** run with the world stopped

`gc_collect_internal` (`Python/gc_free_threading.c:2065`) brackets the whole mark/sweep:

| region | lines | slice code that runs inside |
|---|---|---|
| STW #1 | `:2067` → `:2141` | `gc_mark_alive_from_roots` (`:2105`) and `deduce_unreachable_heap` (`:2117`) dispatch `tp_traverse` |
| STW #2 | `:2151` → `:2161` | `handle_resurrected_objects` calls `traverse(op, visit_decref_unreachable, NULL)` directly at `:1877` |

So the slice's five traverse slots run with every other thread suspended:

| function | body | verdict |
|---|---|---|
| `list_traverse` `listobject.c:3427` | `for (i = Py_SIZE(o); --i >= 0;) Py_VISIT(o->ob_item[i]);` | **ACCEPTABLE** — `Py_SIZE` + direct `ob_item` read; both are explicitly on the STW allow-list |
| `listiter_traverse` `:4059` | one `Py_VISIT(it_seq)` | ACCEPTABLE |
| `listreviter_traverse` `:4213` | one `Py_VISIT(it_seq)` | ACCEPTABLE |
| `bytearrayiter_traverse` `bytearrayobject.c:2965` | one `Py_VISIT(it_seq)` | ACCEPTABLE |
| `striter_traverse` `bytesobject.c:3427` | one `Py_VISIT(it_seq)` | ACCEPTABLE |

**5 of 5 clean.** No allocation, no `PyErr_*`, no `PyObject_*`, no lock, no re-entrant STW. `PyBytes_Type` and `PyByteArray_Type` themselves have `tp_traverse = 0` (`bytesobject.c:3252`, `bytearrayobject.c:2924`).

**The other three doors into a stopped world are closed by construction:**

- **`tp_clear` and `tp_dealloc` run *outside*.** `_PyEval_StartTheWorld` is at `:2161`, and only then does the comment *"Call tp_clear on objects in the unreachable set"* introduce `delete_garbage`. `call_weakref_callbacks` and `finalize_garbage` are placed at `:2147-2149` with the in-code reason — *"after unpausing other threads to avoid potential deadlocks."* Same ordering invariant as `gc_free_threading.c:2223` / `:2253`.
- **`clear_weakrefs` (inside STW #2, `:2158`) can never reach this slice** — all **7** type objects in the four files declare `tp_weaklistoffset = 0` (`listobject.c:3953/4020/4174`, `bytesobject.c:3255/3539`, `bytearrayobject.c:2927/3102`).
- **`_PyGC_ClearAllFreeLists` (inside STW #2, `:2155`) does not enter the slice.** `list`, `list_iters` and `bytes_writers` all have freelists here, but they are drained by `Objects/object.c:915 free_object` → `tp->tp_free` (`PyObject_GC_Del`) / `PyMem_Free`. `list_dealloc:569` has already NULLed `ob_item` before pushing, so no slice code runs.

**One thing I checked and it is a negative worth recording.** `list_sort_impl:2971-2973` detaches `ob_item`/`ob_size`/`allocated` before running the key function, so a GC pause during a user `__lt__` sees an *empty* list and `list_traverse` visits nothing. The sorted items survive only through the C-local `saved_ob_item[]`, which holds real strong references — so the FT collector's refcount-difference computation counts them as externally reachable and does not collect them. **Safe, in the conservative direction.** Recorded so nobody re-derives it as a leak or a UAF.

---

# 4. Classes bounded (with denominators)

| class | denominator | verdict |
|---|---|---|
| **StopTheWorld regions in the slice** | **12 spellings × 7 files** (4 `.c`, 12,100 lines + 3 `clinic/*.c.h`) | **structural zero, hand-verified by grep** (the AST-derived zero would not have been trustworthy — `bytesobject.c` is 56.7 % parsed). Tree-wide comparator: 58 `_PyEval_StopTheWorld` references exist; none here |
| **Slice functions called from an STW region in *another* file** (the Phase 2 blind spot, run backwards) | **53** real Stop…Start regions across 21 files × **373** slice-defined names + 6 API prefixes | **zero.** Upstream states the rule itself at `gc_free_threading.c:2393-2395` and `:2437-2439` and honours it at both sites |
| **Slice code reached transitively from inside an STW region** | **5** `tp_traverse` slots (2 STW regions, `:2067-2141` and `:2151-2161`) | **5/5 STW-safe** — every body is `Py_VISIT` over a direct struct read |
| **Slice code in the *other* GC callbacks** | `tp_clear` ×1, `tp_dealloc` ×7, weakref clear, freelist clear | **0 inside STW** — `tp_clear`/`tp_dealloc` are after `StartTheWorld` by design; 7/7 types have `tp_weaklistoffset = 0`; freelist drain goes through `Objects/object.c`, not the slice |
| **`_PyRuntime` bytes a Python-chosen `lo` can reach from the CPY-0186 write base** | `sizeof(_PyRuntime) − offsetof(bytes_empty.ob_sval)` = **299,264** (rel-gil) / **342,312** (rel-ft) / 299,280 (dbg-gil) / 344,736 (dbg-ft), **plus** everything past the end | **100 % reachable**, forward only. The *global* `stoptheworld` + `stoptheworld_mutex` are at negative deltas and are **not** reachable |
| **Bytes of `interp->stoptheworld` whose corruption has a consequence** | **7 fields / 24 bytes**, probed one byte at a time | **1** — `mutex` (+0). The other 6 are re-initialised by `stop_the_world():2452-2455` and measured harmless |

**Deliberately not re-litigated:** CPY-0186 (I add severity only), CPY-0180, CPY-0182, CPY-0014, G1–G6, U1–U5, F1–F11.

---

# 5. Toolkit assessment

## 5.1 `scan_stw_safety` on this slice: correct, and correctly quiet

**Precision N/A, recall N/A — 0 candidates against a hand-verified 0 population.** No false positives, no re-litigation cost. The envelope carries `stw_functions: []` and `stw_wrappers: []` explicitly rather than only a summary count, which is the right shape: it distinguishes *"no regions"* from *"regions, all clean"* without an agent having to guess.

## 5.2 [MEDIUM] The zero is AST-derived and the AST is 56.7 % of one file

This is the fourth agent in this slice to hit the `extract_functions` truncation. For *this* rule the consequence is bounded but real: `stw_functions` comes from walking parsed functions, so if a `_PyEval_StopTheWorld` had lived in `Objects/bytesobject.c:2780-3926` — the region containing `striter_*`, the whole `PyBytesWriter_*` API, `_PyBytes_Resize` and `PyBytes_Concat` — the scanner would have reported the same `0` and been wrong. I second `refcount-auditor` §6.1 / `uninitialized-dealloc-auditor` §5.2 / `gil-discipline-checker` §6.2 on wiring `parse_health()` into every envelope, and add the rule-specific cheap fix:

> **Emit a `stw_token_count` computed by regex over the whole stripped file source, alongside the AST-derived `stw_functions`.** `scan_gil_usage` already counts its vocabulary this way and is therefore truncation-proof in exactly the dimension that matters. Two numbers that disagree (`stw_token_count > 0` while `stw_functions == 0`) is a self-reporting parse failure; two numbers that agree at zero is an *earned* structural zero. Ten lines, and it converts this section into one field.

## 5.3 [HIGH VALUE] The check the rule does not do at all: cross-file callers of an STW region

The agent contract names this as the honest limitation and tells the human to do it by hand. It is mechanisable, it took ~40 lines (`scratchpad/stw_region_callers.py`), and it is the half of the analysis that produced a real answer here.

> **`stw_region_external_callee`.** Extract every `_PyEval_St{op,art}TheWorld[All]` span **tree-wide** (token-level, not AST — the spans are one-liners and immune to parse failure). For each call inside a span whose callee is not defined in the same file, resolve it against a tree-wide symbol index and classify by the existing `stw_safe_apis.json` categories; a callee that is a `tp_slot` implementation or that transitively reaches one is the interesting case. Report unresolved callees as `stw_unknown_call` as today, but now with the region's *file* and the callee's *defining* file, so the human check is a read rather than a search.

Two guards to keep it honest, both of which fired on my hand run: skip spans whose opener is inside a comment (`Python/tracemalloc.c:893`), and skip the two `_PyEval_StopTheWorld*` **definitions** in `Python/pystate.c` — 3 of 56 candidate spans were non-regions.

Ready-made fixtures: negative `Python/gc_free_threading.c:2392-2405` (`_PyGC_GetReferrers`, the accumulate-raw-then-`list_from_object_stack`-after-`StartTheWorld` idiom, with the maintainers' comment as the rationale); positive-shaped-but-safe `:2151-2161` (`handle_resurrected_objects` dispatching `tp_traverse` through a function pointer — the transitive edge no intra-file graph can see).

## 5.4 [LOW] `_PyEval_StopTheWorld` is a no-op on the default build; the envelope should say so

`Python/pystate.c:2547` wraps the body in `#ifdef Py_GIL_DISABLED`. A `stw_functions: 0` on a GIL-only scan is trivially true and means nothing. One `notes[]` line — *"STW is compiled out unless Py_GIL_DISABLED; findings apply to free-threaded builds"* — would stop a future reader from reading this slice's zero as broader than it is. It is also, on this slice, exactly the fact that made §2.3's control experiment work.

## 5.5 What the informed method bought

The briefing's *"a verdict rests on a crash count or a debugger frame"* is what turned this report from an essay into an experiment. Group B's `[CONSIDER] G5` said plainly *"structural, and I could not turn it into a measurement"* about the third of its three widening arguments and listed the falsifier — *"target `lo` at the measured `interp->stoptheworld` offset per build rather than a build-independent constant."* I did exactly that and it worked on the first index. **Their honesty about what they had not measured is the entire reason §2.3 exists** — the same mechanism `uninitialized-dealloc-auditor` §5.5 credits for U1. That is now three findings in this slice produced by one agent's "what I did not do" section.

**Cost:** one wrong hypothesis, cheaply falsified. My first sweep used `SIZE=200_000` with `lo=106_016` and found nothing, because `bytearray_resize_lock_held:240` only reaches `_PyBytes_Resize` on a shrink when `requested_size < ob_alloc / 2` — `106_040` is *more* than half of `200_000`, so the "Minor downsize; quick exit" at `:246` returned 0 without allocating. `SIZE` must exceed `2 × (lo + payload)`. Recorded because any future probe of this path has the same precondition.

---

# 6. What I did not do

- **I did not reach `interp._initial_thread` or `interp.mimalloc`.** Both are named in §2.2 as structurally worse than the `stoptheworld` mutex and both are **unmeasured**; `_initial_thread` needs `lo ≈ 326,520` and therefore a >653 kB bytearray, which I did not sweep.
- **The `is_global` negative is single-interpreter only.** Falsifier: repeat `repro/stw_field_granularity.py` with a live `interpreters.create()` so `PyInterpreterState_Head() != &_main_interpreter`.
- **I did not confirm the hang's C frame.** `ptrace_scope` on this host refuses `gdb -p`, so the attribution is the faulthandler Python frame (`gc.freeze()`) plus the byte-granular result (only `mutex` hangs) plus the source path (`_PyGC_Freeze` → `_PyEval_StopTheWorld` → `PyMutex_Lock`). I did not exhibit a `_PyMutex_LockTimed` frame.
- **`debug-gil-nojit` is inconclusive for the STW question** — it SIGABRTs in the `len()` verification probe before reaching `gc.freeze()`. The GIL-side control that matters is `release-gil-nojit`, which is clean and where the write is proven identical.
- **No JIT builds, no ASan/TSan runs.** Nothing here needed them; the observable is a hang, and a hang is not a sanitizer's business.
- **I did not file anything upstream.** This is severity on an existing, unfiled finding (CPY-0186).

# 7. Artifacts

- `reports/obj-sequences/repro/stw_runtime_oob_target.py` — §2.3 whole-struct probe, 4 builds, with the `len(b)` verification that the OOB write executed and the `lo=8` matched control
- `reports/obj-sequences/repro/stw_field_granularity.py` — §2.3 byte-granular probe, 7 fields × 1 byte
- `scratchpad/stw_layout.gdb`, `scratchpad/stw_layout2.gdb` — the §2.2 offset map, 4 builds
- `scratchpad/stw_region_callers.py` — the §1.1 tree-wide Stop…Start region / slice-symbol intersection
- `scratchpad/stw_hang_where.py` — the faulthandler attribution of the hang

---

## Summary line

```
FIX=0  CONSIDER=0  POLICY=0  ACCEPTABLE=5 (the five tp_traverse slots)
scan_stw_safety: 0 candidates / 0 population -- STRUCTURAL ZERO, hand-verified 0 hits over 12 spellings x 7 files
  (grep, not AST: extract_functions parses only 56.7% of Objects/bytesobject.c)
phase-2 blind spot run backwards: 53 real Stop..Start regions tree-wide, 0 call any of the slice's 373 symbols
transitive form DOES apply: 5 tp_traverse slots run inside gc_free_threading.c:2067-2141 / :2151-2161 -- 5/5 safe
task (b): CPY-0186's reachable region is 299,264 B (rel-gil) / 342,312 B (rel-ft) forward-only, + past the end
  interp->stoptheworld measured at +83,800 (gil) / +106,016 (ft); global stoptheworld is at a NEGATIVE delta, unreachable
  MEASURED: 1 byte at stoptheworld.mutex -> PERMANENT HANG in gc.freeze(), 2/2 FT builds
            identical write, identical offset, release-gil -> all STW probes clean (STW is #ifdef Py_GIL_DISABLED)
            6 of 7 struct fields harmless -- stop_the_world() re-initialises them
  => CPY-0186 gains an FT-only failure mode: silent deadlock of the whole pause facility, not a crash. No prior art.
```
