# tsan-stress-generator — obj-typeobject PASS 2, Phase 2E (dynamic verification)

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777`, `Objects/typeobject.c`
(byte-identical to the build-matrix ref `a1d580430c8`).
All builds: `~/projects/python_build_matrix/builds/<name>/python`.

---

# PART 1 — DEFECT E1: **CONFIRMED**, end to end, and the fix is verified

**Verdict: E1 is correct in every particular, and one of its claims is
stronger than stated.** The stock `*-ft-*-asan` builds do not merely fail to
attribute the overflow — they produce a report that actively points at the
wrong file.

## 1.1 The mechanism, confirmed at source and at build-config level

Four independent facts, each checked directly in the target tree:

| # | Claim | Verification |
|---|---|---|
| 1 | FT routes **every** `PyObject` through mimalloc | `Objects/obmalloc.c:431-435` — `#if defined(Py_GIL_DISABLED)` hardcodes `PYOBJ_ALLOC MIMALLOC_OBJALLOC`. `obmalloc.c:30-31` makes it a hard `#error` to build FT without mimalloc. |
| 2 | `MI_TRACK_ASAN` is vendored **commented out** | `Include/internal/mimalloc/mimalloc/types.h:88` — `// #define MI_TRACK_ASAN     1` |
| 3 | Nothing in the build system ever defines it | Tree-wide grep finds **6** hits, all **consumers**: `Objects/mimalloc/os.c:215,420,648`, `types.h:88,118`, `track.h:63`. **Zero definition sites.** `configure.ac:3482-3489` adds `-fsanitize=address` and `-fno-omit-frame-pointer` and nothing else. |
| 4 | `PYTHONMALLOC=malloc` is refused under FT | `Objects/obmalloc.c:797` — the `malloc` / `malloc_debug` arms are inside `#ifndef Py_GIL_DISABLED`. Confirmed at runtime: `Fatal Python error: preconfig_init_allocator: PYTHONMALLOC: unknown allocator`. |

The consequence is exact. `mimalloc/track.h` is a four-way `#if` chain; with
`MI_TRACK_ASAN` undefined it falls to the `#else // no tracking` arm at
`track.h:95-104`, where `mi_track_malloc_size` and `mi_track_free_size` expand
to **nothing**. The `ASAN_POISON_MEMORY_REGION` / `ASAN_UNPOISON_MEMORY_REGION`
calls in the `MI_TRACK_ASAN` arm (`track.h:63-77`) are never compiled. mimalloc
takes its memory from the OS and hands out blocks ASan has never been told
about, so there are no redzones between adjacent `PyObject`s.

**Build-config confirmation** (`pyconfig.h` + `Makefile` of each matrix build):

| build | `WITH_PYMALLOC` | `Py_GIL_DISABLED` | `MI_TRACK_ASAN` | object heap goes to | ASan shadow? |
|---|---|---|---|---|---|
| `release-gil-nojit-asan` | 0 | 0 | 0 | `MALLOC_ALLOC` (libc malloc) | **YES** |
| `debug-gil-nojit-asan` | 0 | 0 | 0 | `MALLOC_ALLOC` | **YES** |
| `release-ft-nojit-asan` | 0 | **1** | 0 | `MIMALLOC_OBJALLOC` | **NO** |
| `debug-ft-nojit-asan` | 0 | **1** | 0 | `MIMALLOC_OBJALLOC` | **NO** |
| `release-ft-nojit-asan-mitrack` | 0 | 1 | **1** | `MIMALLOC_OBJALLOC` | **YES** |

`--without-pymalloc` is what saves the GIL ASan builds: with `WITH_PYMALLOC`
undefined and `Py_GIL_DISABLED` undefined, `obmalloc.c:436-443` falls through to
`#else / PYOBJ_ALLOC MALLOC_ALLOC`. **This bounds the damage: every `*-gil-*-asan`
result in the campaign is trustworthy.**

## 1.2 The demonstration

Reproducer written for this: `<run>/repro/e1_inline_values_overflow.py`.
It drives P2-F17 directly — `type_ready_add_subclasses` links a new heap type
into `Base.__subclasses__()` **before** `type_ready_managed_dict` sets
`Py_TPFLAGS_INLINE_VALUES` (`type_ready`, `typeobject.c:9560-9590`), so a peer
thread grabs the half-ready type and straddles the two `tp_flags` reads in
`_PyType_AllocNoTrack` (`:2524` sizes the allocation, `:2550` gates the
initialization).

| build | runs | outcome | overflow attributed? |
|---|---|---|---|
| `release-ft-nojit` (plain FT) | 3 | **3/3 SIGSEGV** (exit 139) | n/a — no sanitizer |
| `release-gil-nojit` (control) | 3 | **3/3 clean**, ~20M instantiations and ~600–780M type creations per run | n/a — bug is FT-only |
| **`release-ft-nojit-asan`** | 3 | ASan fires, but as `SEGV on unknown address 0x0b` in `clear_inline_values` (`dictobject.c:7778`) at **dealloc** time | **NO — 0/3.** Zero occurrences of `heap-buffer-overflow`, `use-after-poison`, `_PyObject_InitInlineValues`, or `_PyType_AllocNoTrack` in any of the three reports. |
| **`debug-ft-nojit-asan`** | 2 | `Fatal Python error: _PyMem_DebugRawFree: bad trailing pad byte`, exit 134 — caught by **CPython's own debug allocator**, not by ASan | **NO — 0/2** overflow signatures |
| **`release-ft-nojit-asan-mitrack`** (pristine source + `-DMI_TRACK_ASAN=1`) | 3 | **3/3 `use-after-poison`, `WRITE of size 232`**, stack `__asan_memset ← _PyObject_InitInlineValues (dictobject.c:7336) ← _PyType_AllocNoTrack (typeobject.c:2551) ← PyType_GenericAlloc (:2559) ← object_new (:7468)` | **YES — exact** |

**This is worse than "reports as nothing", and that matters for the campaign.**
On `release-ft-nojit-asan` ASan does emit a report — a fully symbolized
24-frame stack. It just names `Objects/dictobject.c:7778` in
`clear_inline_values`, the *victim* reading the corrupted inline-values header
at dealloc time, with no allocation site, no region bounds, and no mention of
the writer. A reviewer who did not already know the answer would file this in
`dictobject.c`, in the wrong slice.

## 1.3 The fix — verified, and it genuinely needs **no CPython source change**

The pre-existing `release-ft-nojit-asan-mitrack` build carried a 2-line
`Objects/obmalloc.c` patch (dropping the `#ifndef Py_GIL_DISABLED` around the
`malloc` allocator names), so it did **not** by itself prove the
"no source change" claim.

I reverted that patch (`git checkout Objects/obmalloc.c`), rebuilt
incrementally, and confirmed the tree is clean (`git status --porcelain` empty)
and that `PYTHONMALLOC=malloc` is refused again — i.e. the binary is now the
pure-flag variant. It still produces the exact `use-after-poison` above, 3/3.

```
./configure --disable-gil --with-address-sanitizer --without-pymalloc \
            CC=clang-21 CFLAGS="-DMI_TRACK_ASAN=1 -fno-omit-frame-pointer -g"
```

**Verified:** `-DMI_TRACK_ASAN=1` alone restores object-heap shadow on an FT
build, with an unmodified CPython source tree, and attributes P2-F17 exactly.

**Not verified:** whether `-DMI_TRACK_ASAN=1` costs anything in false positives
or runtime over a long campaign; whether the debug FT ASan build behaves the
same (I only rebuilt the release variant); and whether `heap-buffer-overflow`
(rather than `use-after-poison`) reports need the additional
`PYTHONMALLOC=malloc` patch — E1 claims they do and I did not re-test that arm
after reverting.

**Caveat on what I changed:** `builds/release-ft-nojit-asan-mitrack` now
contains the **pure-flag** binary; the previous patched binary is preserved
beside it as `python.PATCHED-OBMALLOC.bak`. No other build was touched;
`/home/danzin/projects/cpython` is unmodified (`git status` clean apart from a
pre-existing untracked `curses.txt`).

## 1.4 Retrospective damage — scoped

Full sweep of the catalog (`/home/danzin/projects/cpython-review-findings`, 84+
records) and every slice report under `cpython-review-toolkit/reports/`.

### The headline is reassuring, and it is worth saying precisely

**Zero catalog records rest on an FT-ASan clean run.** And there is **not a
single `heap-buffer-overflow`, `heap-use-after-free`, or `use-after-poison`
attributed to a stock `*-ft-*-asan` build anywhere in either tree.** Every
FT-ASan *positive* in the campaign is a SEGV, an assertion abort, a
`_Py_NegativeRefcount`, or a stack overflow — detection classes that do **not**
need the mimalloc shadow. So the blindness removed a capability that was, in
practice, never producing positives; it only ever produced misleading negatives.

### Load-bearing negatives — exactly ONE

**R-1 · pass-1 `obj-typeobject` memory-pattern-analyzer, the `__slots__`
allocation-arithmetic bounded negative.**
`reports/obj-typeobject/agents/memory-pattern-analyzer.md:189-227` (+ the
artifacts table at `:638-647`), restated without build attribution in
`reports/obj-typeobject/SUMMARY.md:166-172`, and propagated into catalog record
**CPY-0088**'s report + meta.

The certified claim is the *absence of a heap-buffer-overflow write* in the
members-array / `tp_basicsize` arithmetic — the CPY-0012 shape transplanted to
`typeobject.c`. That is precisely the class the FT arm cannot see. The sweep
reports **"Zero ASan reports on either sanitizer build"** and **"all three
builds, ASan silent"**, counting `debug-ft-nojit-asan` as if it contributed
information.

**Mitigation: the negative does not collapse.** The same sweep also ran on
`debug-gil-nojit-asan`, whose shadow is intact (§1.1 table), and the argument is
primarily *structural* — `__slots__` is read once into `ht_slots` and never
re-read from the mutable type dict, and every members walk is bounded by
`Py_SIZE(type)`. The correct action is to **downgrade the evidence claim, not
retract the verdict**: strike the FT arm from the sentence, and re-run
`slots_probe.py` / `cpy0012_differential.py` on `release-ft-nojit-asan-mitrack`
to restore the FT arm properly.

### Negatives that are NOT invalidated

- **R-2 · P2-F9** (`gil-discipline-checker.md:138`, `FINDINGS_LEDGER.md:181`).
  The FT-ASan clean row is explicitly labelled *masking, not exoneration*, and
  the finding stands on `debug-gil-nojit-asan` 3/3. One correction worth making:
  the row attributes the silence to *deferred/freelist reclamation*, which may
  be the wrong mechanism — E1's missing shadow is at least as likely. The
  conclusion is unaffected.
- **R-3 · P2-F17 itself** (`ft-race-scanner.md:1148-1151`) — three clean FT-ASan
  runs on a real 236-byte overflow. Already caught and inverted in the same
  document; this is the canonical instance, not an outstanding liability.
- **R-4** · `memory-pattern-analyzer.md:291` STW row — an FT-ASan non-crash, but
  the claim it supports is "release builds silently proceed", not absence of a
  heap bug.

### Positives obtained on FT-ASan builds — all still valid

Enumerated so it is on record that the blindness manufactured no false
positives. Catalog: **CPY-0017, 0021, 0026, 0031, 0038, 0039, 0040, 0047,
0067**. Slice reports: the whole `modules-sample-informed-v1` FT crash set
(`ft-race-scanner.md:43-44`), **P2-F16/CPY-0014**, and the two
`error-path-analyzer` FT SEGVs (`set_mro_error:3320`,
`object_getstate_default:7990`). All are SEGV/abort-class.

### Unaffected

**37 catalog records** cite a `*-gil-*-asan` result — CPY-0001..0006, 0007,
0009, 0012, 0013, 0016, 0017, 0019..0023, 0030..0032, 0034..0037, 0044, 0047,
0048, 0050, 0053, 0054, 0059, 0065, 0066, 0068, 0069, 0084, 0087 — plus pass-2
P2-F1, F5, F9, F14, F19. Shadow intact per §1.1; nothing to re-check.

### Prior art — none

`gh api -X GET search/issues -f q='repo:python/cpython mimalloc MI_TRACK_ASAN'`
returns **zero results**. So this is not a known upstream limitation: CPython
ships a `--with-address-sanitizer` configure option that, on a free-threaded
build, silently provides **no shadow for the object heap** — and nothing on the
tracker says so. That is arguably reportable upstream in its own right
(a one-line `configure.ac` change: append `-DMI_TRACK_ASAN=1` to `BASECFLAGS`
when ASan and `Py_GIL_DISABLED` are both on).

### Actions

1. Add `CFLAGS=-DMI_TRACK_ASAN=1` to **both** FT ASan builds in
   `python_build_matrix/build_all.sh` and `builds.csv`.
2. Amend **R-1** and re-run its two probes on the mitrack build.
3. Correct **R-2**'s stated cause.
4. Add to `RUN_CONTEXT.md` traps: *a clean `*-ft-*-asan` result is not evidence
   of the absence of a heap overflow — and a `SEGV on unknown address` report
   from one may name the victim's file, not the writer's.*

---

# PART 2 — Stressing the confirmed races

Scripts (all self-contained, TSan-autodetecting, one shared object per
scenario, `threading.Barrier` start):

| script | target |
|---|---|
| `<run>/repro/tsan_stress_tp_watched.py` | P2-F18 / P2-C2 `tp_watched` |
| `<run>/repro/tsan_stress_tier2_watched.py` | P2-F18 through the **real tier-2 optimizer** |
| `<run>/repro/tsan_stress_type_cache.py` | `type_cache_clear` store-ordering |
| `<run>/repro/tsan_stress_type_mutation.py` | Part 3, new territory |
| `<run>/repro/run_tsan_stress.sh` | driver, one scenario per process |
| `<run>/repro/e1_inline_values_overflow.py` | P2-F17 / the E1 probe |

**Harness note worth carrying into the toolkit:** the standard template's
`os.fork()`-per-scenario isolation **deadlocks under TSan** — scenario 1 hit the
120 s timeout with 4 threads and 400 iterations, work that takes under a second
un-sanitized. The scripts now honour `STRESS_NO_FORK=1` and accept a scenario
name on argv, so `run_tsan_stress.sh` provides process isolation by running one
scenario per interpreter invocation instead. Recommend making this the default
for TSan builds in the agent template.

## 2.1 P2-F18 `tp_watched` — **ESCALATED. Both writers are the tier-2 optimizer.**

The ledger records the JIT consequence via `Python/optimizer_analysis.c:177`
arming the bit. It understates the finding. Enumerating **all** in-tree callers
of both functions:

```
LOCKED writer   -> typeobject.c:1112   tp_watched |=  (1 << id)   under BEGIN_TYPE_LOCK()
    Python/optimizer_analysis.c:177    watch_type()
    Python/optimizer_bytecodes.c:1462, :2215, :2236
    (+ optimizer_cases.c.h:2599, 2624, 2750, 3890, 4406 — generated from the above)

BARE writer     -> typeobject.c:1129   tp_watched &= ~(1 << id)   no lock, no atomic
    Python/optimizer_analysis.c:156    type_watcher_callback()
```

and both use **the same watcher id** — `#define TYPE_WATCHER_ID 0`,
`optimizer_analysis.c:138`. The only other callers of either function in the
whole tree are `Modules/_testcapi/watchers.c:301` and `:315`.

So this is **not a test-only race, and it needs no third party**:

- thread A optimizes a trace → `watch_type(T)` → locked `|=` at `:1112`;
- thread B modifies `T` → `_PyType_Modified_Unlocked`'s watcher loop
  (`typeobject.c:1212-1222`) → `type_watcher_callback(T)` →
  `_Py_Executors_InvalidateDependency` → **bare `&= ~` at `:1129`**.

Note also that `type_watcher_callback` performs its bare write **from inside**
the loop at `:1212-1214` that is reading `type->tp_watched` to decide which
watchers to notify — the callback mutates the byte the loop is iterating.

### The consequence, measured — and the dangerous direction is the LOST SET

Two threads, non-atomic RMW on one `unsigned char`. If B loads the byte, A's
locked `|=` completes, then B stores its stale-derived value, **A's set is
lost**. `PyType_Watch` returned `0`. The type is not watched. The next
modification never reaches `type_watcher_callback`, so
`_Py_Executors_InvalidateDependency` never runs, and a live tier-2 executor
keeps running against a type assumption that no longer holds.

`scenario_lost_set_consequence` measures exactly this, with **no sanitizer**.
It arms a watcher on a fresh shared type (the `watch_type()` role), then
modifies it and checks whether a notification arrived; peer threads spam
`PyType_Unwatch` for a **different** watcher id, so they arm nothing and every
observed notification must come from the armed bit.

| build | rounds | lost sets |
|---|---|---|
| `release-ft-nojit`, `PYTHON_GIL=0` | 4000 × 3 | **5, 1, 2 — 8/12000** |
| `release-ft-nojit`, `PYTHON_GIL=1` (same binary) | 4000 × 2 | **0/8000** |
| `release-gil-nojit` | 4000 × 2 | **0/8000** |

This is the stronger half of the finding: it is the direction P2-F18 did not
measure (it measured a bit *surviving* its own unwatch), it is the direction the
JIT cares about, and it needs no sanitizer to see.

### TSan, `release-ft-nojit-tsan` — reproduced, **and a pair the ledger does not record**

`run_tsan_stress.sh`, one scenario per process, 4 threads / 400 iterations each:

| scenario | races | distinct site-pairs |
|---|---|---|
| `scenario_watch_vs_unwatch` | 4 | 2 |
| `scenario_watched_bits_vs_notify` | 4 | 2 |

Deduped by unordered `file:func` pair (`summarize_tsan.py`):

| n | pair |
|---|---|
| 4 | `write PyType_Unwatch typeobject.c:1129` **vs** `write PyType_Watch typeobject.c:1112` |
| 4 | `write PyType_Unwatch typeobject.c:1129` **vs** `write PyType_Unwatch typeobject.c:1129` |

`Write of size 1` in both cases — the `unsigned char tp_watched`
(`Include/cpython/object.h:236`).

**The second pair is new.** P2-F18 is recorded as an asymmetry between a locked
writer and a bare one. `:1129` racing **itself** means the bare writer is unsafe
with *no locked writer involved at all* — two threads each executing
`tp_watched &= ~(1 << id)` lose each other's updates. That matters because in
the real optimizer the bare writer is `type_watcher_callback`, which runs on
**whichever thread modified the type** — so two threads modifying the same
watched type is sufficient, and no concurrent trace optimization is needed.
The proposed fix must therefore make `:1129` atomic (or locked); making `:1112`
and `:1129` merely *consistent* is not enough if both remain plain RMWs.

### The tier-2 consequence — reachability, calibrated

Two things must be said, and they pull in opposite directions.

**In favour of severity:** the mitigation proposed upstream is **not in the
reviewed tree.** Open PR gh-141595 (*"gh-141594: A free-threaded JIT (Part 1)"*)
describes the design as *"Creation of >1 threads cause global invalidation of
all executors and disables JIT. Once we drop to 1 thread, JIT is re-enabled."*
That mechanism is **absent at `4f3be1b5777`** — no `git log` match for
gh-141594/141595, and no thread-count JIT gate in `Python/optimizer.c` or
`pystate.c`. So on the reviewed source, nothing prevents two threads from
reaching `:1112` and `:1129` concurrently through the optimizer. And the
upstream mitigation is explicitly *Part 1* — it is scheduled to go away.

**Against over-claiming:** I did not observe the race with
`optimizer_analysis.c` frames in the stack.

### Attempt to drive it through the real tier-2 optimizer — **not achieved**

Stated plainly because it bounds the claim. I built a purpose-made
free-threaded + tier-2 + TSan interpreter for this:

```
./configure --disable-gil --enable-experimental-jit=interpreter \
            --with-thread-sanitizer CC=clang-21
```
→ `~/projects/python_build_matrix/builds/release-ft-jit-tsan/python`
(`sys._jit.is_available()` and `is_enabled()` both `True`; TSan CFLAGS
confirmed). It is left in place for follow-up; it is **not** in `builds.csv`.

`tsan_stress_tier2_watched.py` runs hot loops over 12 distinct code objects that
all guard on one shared type, with a quiet warm-up phase, then forces
re-optimization via `_testinternalcapi.invalidate_executors` while peer threads
mutate the type. **Two runs, 0 races, 0 frames in `optimizer_analysis.c`.** I
could not confirm executors were actually being installed (`dis(adaptive=True)`
shows `JUMP_BACKWARD_JIT` but no `ENTER_EXECUTOR` — and it shows the same on the
`release-gil-jit` reference build, so that probe is uninformative rather than
negative).

**So: the tier-2 reachability of P2-F18 is established statically and by the
`_testcapi` proxy that exercises the identical two lines; it is not established
by a race observed with `optimizer_analysis.c` frames in the stack.** That
remains open, and the build to do it on now exists.

## 2.2 `type_cache_clear` — the store-ordering inversion, sharpened

The finding is best stated against its own in-file contract.

`update_cache` (`typeobject.c:6215-6231`) carries an explicit comment:

> `// We must write the version last to avoid _Py_TryXGetStackRef() operating on an invalid (already deallocated) value inside _PyType_LookupRefAndVersion(). If we write the version first then a reader could pass the "entry_version == type_version" check but could be using the old entry value.`

and obeys it — `value` (relaxed), `name` (relaxed), then `version`
(**release**).

`type_cache_clear` (`:986-991`) does the reverse, and drops the atomics:

```c
entry->version = 0;                                  /* version FIRST */
Py_XSETREF(entry->name, _Py_XNewRef(value));
entry->value = NULL;                                 /* value LAST   */
```

All three are **plain** stores, racing the reader's
`_Py_atomic_load_uint32_acquire(&entry->version)` and
`_Py_atomic_load_ptr_relaxed(&entry->name)` at `:6315-6321`. The seqlock
bracket protects the sequence number, not these stores' visibility ordering.
**A third site does the same:** `_PyTypes_AfterFork` (`:6275-6281`) —
`entry->value = NULL; Py_SETREF(entry->name, Py_None); entry->version = 0;`,
plain stores again, inside `#ifdef Py_GIL_DISABLED`.

Python-level trigger: `sys._clear_type_cache()` → `PyType_ClearCache` →
`_PyType_ClearCache` (`:1021`) → `type_cache_clear`.
`tsan_stress_type_cache.py` drives three scenarios against 24 shared types and
8 names, so many buckets are live simultaneously: clear-vs-lookup,
clear-vs-`update_cache_gil_disabled`-vs-readers, and clear-vs-bound-method
dispatch.

### TSan — **REPRODUCED, and it partitions cleanly one pair per field**

`release-ft-nojit-tsan`, 4 threads / 300 iterations. **51 races across two
independent scenarios, and both resolve to exactly the same 3 site-pairs:**

| field | writer (plain store) | reader (atomic load) | `clear_vs_lookup` | `clear_vs_fill` |
|---|---|---|---|---|
| `entry->version` | `type_cache_clear` `typeobject.c:988` | `_Py_atomic_load_uint32_acquire` `pyatomic_gcc.h:605` | 10 | 8 |
| `entry->name` | `type_cache_clear` `typeobject.c:989` | `_Py_atomic_load_ptr_relaxed` `pyatomic_gcc.h:387` | 9 | 7 |
| `entry->value` | `type_cache_clear` `typeobject.c:990` | `_Py_atomic_load_ptr_relaxed` `pyatomic_gcc.h:387` | 10 | 7 |
| | | **totals** | **29** | **22** |

Every pair is **plain store vs atomic load** — the exact signature of the
defect, with no other shape mixed in, and one pair per field with none missing
and nothing extra. Adding a concurrent contract-abiding writer
(`update_cache_gil_disabled`, `clear_vs_fill`) does **not** introduce a fourth
pair: the correctly-ordered atomic writer never races, only the clear does.
That is a clean natural control — it isolates the defect to
`type_cache_clear`'s three plain stores rather than to the cache design.

This independently confirms the ft-race agent's Finding 9 (24 races, "three
predicted field pairs") from a different script and a different driver, and
makes the per-field partition explicit. Trigger is one line of ordinary Python
(`sys._clear_type_cache()`).

**Fix direction:** make the three stores atomic and reverse their order to
match `update_cache`'s documented contract — `value`, then `name`, then
`version` **last** and with release semantics. `_PyTypes_AfterFork`
(`:6275-6281`) needs the same treatment.

## 2.3 P2-F6 `insert_split_key` deadlock — independently re-verified

Re-ran `<run>/repro/lock_insert_split_key_deadlock.py` first-hand:

| build | runs | result |
|---|---|---|
| `release-ft-nojit`, `PYTHON_GIL=0` | 8 | **8/8 hang** (exit 124 at the 15 s timeout) |
| `release-gil-nojit` | 6 | **0/6** — all exit 0 |

Consistent with the 24/24 vs 0/12 already recorded. No new scenario added: the
existing reproducer is deterministic, and the recursive-descent surface it
shares with `_PyType_Modified_Unlocked` is covered by
`scenario_watcher_callbacks_concurrent` in Part 3.

---

# PART 3 — New territory: the type-mutation surface under concurrency

`tsan_stress_type_mutation.py`, five scenarios, each sharing **one** type or
instance across all threads. None of this surface had been hammered
concurrently in pass 2 — every prior reproduction of P2-F3 / P2-F4 / P2-F5 /
P2-F9 was single-threaded re-entrancy.

| scenario | surface | sites under test |
|---|---|---|
| `setclass_pingpong` | `__class__` assignment on one shared instance | `object_set_class:7825` (stale `oldto`, P2-F4), `object_set_class_world_stopped:7763`, `compatible_for_assignment:7683`, `same_slots_added:7609` (P2-F3) |
| `setbases_shared_type` | `__bases__` on one shared type | `type_set_bases_unlocked:1949`, partial-rollback `bail:` at `:1952`/`:1968` (P2-F11), `add_all_subclasses` / `remove_subclass:9791` |
| `mro_recompute_vs_lookup` | MRO recomputation vs MRO-walking lookup | `mro_internal:3678` → `type_mro_modified:1299` borrowed MRO (P2-F5), `mro_implementation_unlocked:3503` (P2-F16/CPY-0014), `find_name_in_mro` |
| `watcher_callbacks_concurrent` | watcher callbacks + 24-node subclass descent vs live reparenting | `_PyType_Modified_Unlocked:1201/1206/1212/1222/1223` (P2-F8, P2-F9 cursor-invalidation UAF), `remove_subclass:9791` |
| `mixed` | all of the above plus `sys._clear_type_cache()` | the realistic shape |

### Results — 1 of 5 scenarios produced races

| scenario | races | pairs |
|---|---|---|
| `setclass_pingpong` | 0 | — |
| **`setbases_shared_type`** | **6** | **3** |
| `mro_recompute_vs_lookup` | 0 | — |
| `watcher_callbacks_concurrent` | 0 | — |
| **`mixed`** | **4** | **2** |

`mixed` reproduces the **same** `_PyType_GetSubclasses:793` walker race
independently, from a different thread mix — so the finding below is confirmed
by two scenarios, not one. The three zeros are **weak** negatives at TSan sizing
(4 threads / 300 iterations) and should not be read as clean — see the sizing
caveat below.

### NET NEW · `T.__subclasses__()` races `X.__bases__ = (...)` cross-thread

Two lines of ordinary Python on two threads. Full chains from the TSan stacks:

**Reader** — walks `tp_subclasses` with a live `PyDict_Next` cursor, unlocked:
```
type___subclasses___impl:7058 -> _PyType_GetSubclasses:793 -> PyDict_Next
                              -> _PyDict_Next  dictobject.c:3201 / :3212 / :3229
```

**Writer A** — *replaces the whole dict* the reader is walking:
```
add_all_subclasses:9734 -> add_subclass:9710 -> init_tp_subclasses:702
                        -> PyDict_New -> new_dict_impl  dictobject.c:955 / :956
```

**Writer B** — *deletes an entry* from the dict the reader is walking:
```
remove_subclass -> PyDict_DelItem -> _PyDict_DelItem_KnownHash_LockHeld
                -> delitem_common:2987  (_Py_atomic_store_ptr_release)
```

**Why this matters more than a generic race: pass 2 recorded this exact site as
a *guarded twin*.** P2-F9 (`FINDINGS_LEDGER.md`) names
`_PyType_GetSubclasses:788-789` as one of three in-file twins that "carry an
explicit comment justifying the borrowed reference", and cleared it on that
basis. The comment justifies the borrowed reference against **re-entrancy** —
it says nothing about **concurrent mutation**, and under free-threading the
justification does not carry. So this is the cross-thread sibling of P2-F9,
at a walker pass 2 explicitly exonerated.

It is also distinguishable from the recorded prior art. The ledger notes open
issue **gh-151377** covers "the *cross-thread* race on a **different** walker".
This is `_PyType_GetSubclasses`, reached from the public
`type.__subclasses__()`. Whether gh-151377's proposed locking covers this
walker too is the one open question before filing — check that before opening
anything.

**Confidence: medium-high.** 10 races over **two independent scenarios**
(`setbases_shared_type` 6, `mixed` 4), same walker, mechanically consistent
pairs. Still missing before it becomes a record: a **GIL control** run and a
higher-iteration re-run. Do those two things first.

### `scenario_mixed` hung — chased down, and it is NOT a typeobject defect

Worth recording because the first three readings of it were all wrong, and the
last one is a genuine harness lesson.

The driver reported `scenario_mixed` **exit=124** (killed at the 600 s cap)
while the other four scenarios printed `OK`. Off TSan it reproduced **5/6 on
`release-ft-nojit`, 0/4 on `release-gil-nojit`** — an FT-only hang, which looked
like a strong new finding (and plausibly P2-F3's known nested-STW hang arriving
by a concurrent route rather than a re-entrant one).

It is not. Bisecting the scenario (`<run>/repro/mixed_hang_probe.py`):

| mode | result |
|---|---|
| `bases_only` (4 threads) | 0.7 s |
| `class_only` (4 threads) | 0.4 s |
| `cache_only` (4 threads) | 0.2 s |
| `bases_class` (both) | **7.3 s** — 10x either alone, but completes |
| `all` | **HANGS** |

`faulthandler.dump_traceback_later` (gdb could not attach —
`/proc/sys/kernel/yama/ptrace_scope` is `1`, so a hung process must be *launched*
under gdb as the P2-F6 reproducer does, not attached to) named the culprit:

```
Thread-7 (the clearcache thread):
  abc.__new__  <-  <frozen io>:77  <-  ... import tokenize ...
  <-  linecache.getline  <-  _py_warnings._formatwarnmsg_impl
  <-  mixed_hang_probe.py:74 in clearcache
```

`sys._clear_type_cache()` is **deprecated** (*"scheduled for removal ... Use
`sys._clear_internal_caches()`"*). Its DeprecationWarning is formatted via
`linecache`, which lazily imports `tokenize` -> `io` -> `abc.ABCMeta.__new__` —
i.e. **class creation on a worker thread**, while four other threads stop the
world continuously with `__bases__` / `__class__` assignment.

Confirmed by removing exactly that: warning suppressed and the modules
pre-imported, **0/6 hangs**, completing in 1.3–7.0 s. After patching both
scripts, `scenario_mixed` is **0/5 hangs** where it was 5/6.

**What it does and does not show.** It does *not* identify a `typeobject.c`
defect and it is not attributable to any line — it is a harness artifact
introduced by a deprecated API's warning path. What it does show, and this is
real, is that a thread performing first-time imports (hence class creation) can
be starved for >60 s by continuous stop-the-world type mutation on other
threads. Whether that is starvation or a true deadlock is **not settled** by my
data: both the 20 s and 60 s deadlines fired, which leans deadlock, but healthy
runs already vary 1.3–7.0 s, so severe starvation alone could explain it. If
anyone pursues it, drive it deliberately (an import loop against an STW-mutation
loop) rather than through a deprecation warning.

**Two corrections it forces on this report's own artifacts:** both
`tsan_stress_type_cache.py` and `tsan_stress_type_mutation.py` now suppress
`DeprecationWarning` and pre-import `linecache`/`tokenize`/`io`. The
`type_cache` race results above are unaffected — they were collected before the
patch and the warning path does not touch `type_cache_clear`'s stores — but any
re-run should use the patched scripts.

### Sizing caveat — do not read the four zeros as clean

This bit three times in this run. `scenario_clear_vs_lookup` read as **0
races** mid-run and finished at **29**; `clear_vs_fill` did the same and
finished at **22**; `scenario_mixed` read as **0** and finished at **4** — and
that last one would have cost the second confirmation of the net-new finding.
A partial TSan log is indistinguishable from a clean one. Before
concluding anything from the four zero scenarios, raise `ITERATIONS` (they run
at 300 under the TSan auto-reduction) and confirm the process actually exited.
`scenario_watcher_callbacks_concurrent` in particular is the one aimed at
P2-F9's own loop and deserves a proper re-run.

Re-run `/usr/bin/python3 <run>/repro/summarize_tsan.py` at any time to
re-aggregate every log in `tsan-logs/` — no re-execution needed.

---

# Summary of what was measured

| item | result |
|---|---|
| E1 — mimalloc/ASan blindness | **CONFIRMED** at source, build-config, and runtime; and the report it *does* emit names the wrong file |
| E1 fix `-DMI_TRACK_ASAN=1`, no source change | **VERIFIED**, 3/3 correctly-stacked `use-after-poison` on a pristine tree |
| E1 retrospective damage | **1** load-bearing negative (pass-1 `__slots__`, CPY-0088 lineage); 0 catalog records; 0 false positives; 37 GIL-ASan records unaffected |
| P2-F17 heap overflow | 3/3 SIGSEGV plain FT, 3/3 clean GIL, 0/3 attributed on stock FT-ASan, 3/3 attributed on mitrack |
| P2-F18 `tp_watched` TSan | **8 races, 2 pairs** — incl. the previously unrecorded `:1129` × `:1129` self-race |
| P2-F18 lost-set consequence | **8/12000** on FT; **0/8000** on `PYTHON_GIL=1`; **0/8000** on the GIL build |
| P2-F18 tier-2 reachability | static: both writers are the optimizer on the same bit, and gh-141595's mitigation is **not in the reviewed tree**. Dynamic: **not achieved** |
| `type_cache_clear` TSan | **68 races over 3 scenarios, always the same 3 pairs — one per field**, all plain-store vs atomic-load; the contract-abiding writer adds no 4th pair |
| P2-F6 deadlock | re-verified **8/8 hang** FT, **0/6** GIL |
| Part 3 `scenario_mixed` hang | chased down: **harness artifact**, not a typeobject defect — deprecated `sys._clear_type_cache()` warning triggers a lazy import starved by concurrent STW mutation. Fixed; 5/6 hangs -> 0/5 |
| Part 3 (new territory) | **1 NET NEW race**: `T.__subclasses__()` vs `X.__bases__ = (...)` cross-thread on `_PyType_GetSubclasses:793` — a site pass 2 cleared as a guarded twin. confirmed by 2 independent scenarios (10 races); 3 scenarios at 0, all weak negatives |

---

# Artifacts

All under `/home/danzin/projects/cpython-review-toolkit/reports/pass2/obj-typeobject/repro/`:

- `e1_inline_values_overflow.py` — P2-F17 driver / E1 probe
- `tsan_stress_tp_watched.py` — P2-F18, incl. the no-sanitizer lost-set measurement
- `tsan_stress_tier2_watched.py` — P2-F18 through the real tier-2 optimizer (inconclusive)
- `tsan_stress_type_cache.py` — `type_cache_clear`
- `tsan_stress_type_mutation.py` — Part 3
- `mixed_hang_probe.py` — bisects the `scenario_mixed` hang (`bases_only` /
  `class_only` / `cache_only` / `bases_class` / `all`), with a `faulthandler`
  watchdog since gdb cannot attach here
- `run_tsan_stress.sh` — driver (one scenario per process; TSan-safe)
- `summarize_tsan.py` — re-aggregates every log in `tsan-logs/` into unordered
  `file:func` race pairs; run it after any re-run, no re-execution needed
- `tsan-logs/` — raw stdout/stderr per scenario (10 scenarios)

Reproduce the whole suite with:
```
cd <run>/repro && ./run_tsan_stress.sh          # defaults to release-ft-nojit-tsan
/usr/bin/python3 summarize_tsan.py              # aggregate
```

New build, not in `builds.csv`:
`~/projects/python_build_matrix/builds/release-ft-jit-tsan/` — FT + tier 2 + TSan.

Modified build: `~/projects/python_build_matrix/builds/release-ft-nojit-asan-mitrack/`
is now the **pure `-DMI_TRACK_ASAN=1`** variant on a pristine source tree; the
previous patched binary is kept as `python.PATCHED-OBMALLOC.bak`.
