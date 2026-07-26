# gil-discipline-checker — slice `obj-sequences` (Group B, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-verified independently for a fourth time — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files plus `Objects/clinic/{listobject,bytearrayobject,bytesobject}.c.h` is **empty**. Every line number below is valid on every matrix build without adjustment.

**Read first:** `AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/git-history-context.md`, `agents/refcount-auditor.md`, `agents/error-path-analyzer.md`, `agents/init-bypass-checker.md`, `agents/uninitialized-dealloc-auditor.md`, `preflight/informed_briefing.md`.

**Pre-run scanner:** `scan_gil_usage.sample.json` — 0 findings, `vocabulary_resolved: 0`, `vocabulary_tokens_seen: 16`, `rule_not_applicable: true`.

---

## Headline

**(a) The structural zero is a true negative, and I can state the number.** Hand-verified: **0** occurrences of **16 distinct GIL-release spellings** across **7 files** (the four `.c` plus the three `clinic/*.c.h`), **0** file-local macro wrappers that could hide one, and **0** call-form occurrences of the scanner's 45-name blocking-call vocabulary. These four files never release the GIL and never touch `PyGILState_*`. The rule has nothing to check here. *But the mod-io second reading does apply, to a different scanner* — see §2.

**(b) Two novel FIX findings, both reproduced, both live on today's `main`, neither previously reported.**

- **G1 — CPY-0182 has three un-found siblings.** `striter_len`, `striter_reduce` and `striter_setstate` all test `it->it_seq` and then dereference it in a *separate* expression. Group A found the double-DECREF in `striter_next`; the design that causes it — `it_seq == NULL` as the exhaustion sentinel, dropped unconditionally — makes **the whole four-function family** GIL-dependent. Reproduced: **SIGSEGV on `debug-ft-nojit` for all three probes**, use-after-free via `tuple_dealloc`, **3 TSan races with the exact pairs**, and a **12/12 clean guarded-twin control** (`bytearray`/`list` iterators, same script).
- **G2 — `bytearray.__init__` is the one bytearray mutator with no critical section.** 33 of the 36 clinic instance-method entry points take `Py_BEGIN_CRITICAL_SECTION(self)`; the three that do not are `__sizeof__` (safe — its only state read is `FT_ATOMIC_LOAD_SSIZE_RELAXED(self->ob_alloc)`), `clear` (safe — its whole body delegates to the locking `PyByteArray_Resize`), and **`__init__`, which is neither**: it performs a read-modify-write on `ob_size` and two writes through `ob_start` with no lock (`:1094-1100`). Reproduced from **pure Python, no `_testcapi`, no OOM injection**: `Fatal Python error: _PyMem_DebugRawRealloc: bad trailing pad byte` (**heap buffer overflow**) on `debug-ft-nojit`, SIGSEGV on `release-ft-nojit`, **clean on both GIL builds, clean single-threaded, clean with the locked-mutator control**, 51 TSan races. The upstream sweep that added the 33 locks — **gh-129107 / PR #129108, "make bytearray free-thread safe", merged 2025-02-15** — never touched `__init__`, and the reporter's own script never exercised it.

**(c) Measured, and it corrects a natural over-claim.** On a free-threaded build `Py_BEGIN_CRITICAL_SECTION(self)` **does** exclude another thread across a non-detaching user callback (mutator waits the full 1.200 s of a busy loop) and **does not** across a detaching one (mutator gets in at 0.000 s), because `_PyThreadState_Detach` → `_PyCriticalSection_SuspendAll`. Under the GIL there is no such distinction — the mutator gets in after the 5 ms switch interval either way (0.016 s). So the FT lock is *strictly stronger* than the GIL for busy callbacks and *equally weak* for blocking ones, and "I hold `self`'s critical section" is not a sound basis for carrying receiver-derived state across a Python-reaching call under either.

**(d) I agree with Group A: ACCEPTABLE.** But their load-bearing premise was asserted, not measured. I measured it: the per-object lock **is** genuinely dropped mid-impl on a detach (`remove_mutator_proceeded_while_eq_blocked=True after=0.001s`, 4/4 builds), and `list_remove_impl` survives that anyway because `Py_SIZE(self)` is the loop *condition* and `self->ob_item` is re-loaded on every iteration — both after `_PyCriticalSection_Resume`.

**(e) Yes, the FT build widens CPY-0186, and by a measurable amount.** `_PyRuntime` is 406,016 bytes on `release-ft-nojit` vs 346,472 on `release-gil-nojit` (+17.2%); the corruptible tail — `_main_interpreter`, the field immediately after the write base's enclosing struct and the *last* field of `struct pyruntimestate` — grows from 226,256 to 249,536 bytes, and the added bytes are the free-threading synchronisation state. The `runtime` scenario, which Group A2 ran only on the GIL builds, **reproduces on both FT builds (SIGSEGV at n=2)**.

---

# 1. Task (a) — resolving the structural zero

## 1.1 The hand-verified count

`grep` over **7 files** (`Objects/{listobject,bytesobject,bytearrayobject,bytes_methods}.c` = 12,100 lines, plus `Objects/clinic/{listobject,bytesobject,bytearrayobject}.c.h`):

| vocabulary | scanner's? | occurrences |
|---|---|---|
| `Py_BEGIN_ALLOW_THREADS` | ✅ | **0** |
| `Py_END_ALLOW_THREADS` | ✅ | **0** |
| `PyGILState_Ensure` | ✅ | **0** |
| `PyGILState_Release` | ✅ | **0** |
| `PyGILState_Check`, `PyGILState_GetThisThreadState` | — | **0** |
| `PyEval_SaveThread`, `PyEval_RestoreThread` | — | **0** |
| `PyEval_AcquireThread`, `PyEval_ReleaseThread` | — | **0** |
| `PyEval_AcquireLock`, `PyEval_ReleaseLock`, `PyEval_InitThreads` | — | **0** |
| `Py_BLOCK_THREADS`, `Py_UNBLOCK_THREADS` | — | **0** |
| `_PyThreadState_Attach`, `_PyThreadState_Detach` | — | **0** |
| `HEAD_LOCK`, `HEAD_UNLOCK` | — | **0** |
| **file-local `#define` mentioning thread/gil/lock/save/restore** | — | **0** |
| **call-form** hits of the scanner's 45-name `BLOCKING_CALLS` set (`read(`, `write(`, `select(`, `poll(`, `sleep(`, …) | ✅ | **0** |

The only `Py_GIL`-shaped tokens in the slice are **23 `#ifdef/#ifndef Py_GIL_DISABLED`** conditional-compilation gates (20 in `listobject.c`, 2 in `bytesobject.c`, 1 in `bytearrayobject.c`) — free-threading build gates, i.e. the *opposite* construct.

The 25 raw word hits for `accept` / `read` / `write` / `system` are **all prose** in comments and docstrings; none is a call. The scanner strips comments and strings and requires `\bNAME\s*\(` before matching, so its `blocking_with_gil: 0` is earned for the same reason mine is.

**Verdict: genuinely absent, not mis-spelled.** This is the *"no constructs present"* case, not the *"constructs present and all verified clean"* case. It is structural and expected: these are four pure in-memory object implementations with no I/O, no foreign library, and no callback registration surface. The `_ssl`/`_io`/`socketmodule` habitat this rule was built for does not exist here.

This independently confirms the mapper's one-line finding (§f) and adds the extended vocabulary, the clinic headers, the macro-wrapper check and the blocking-call check, which it did not run.

## 1.2 The one thing the envelope says that is not true

`denominators.vocabulary_tokens_seen: 16` reads as *"sixteen vocabulary tokens were seen"*. **Zero were seen.** `scan_common._denominators()` computes `vocabulary_tokens_seen = len(vocabulary_counts)` — i.e. **4**, the number of *names checked*, not the number found — and the sample's `merge_policy` declares that key `"summed"`, so re-running per-file over 4 files and merging produced `4 names × 4 files = 16`. The brief's phrasing (*"vocabulary_resolved: 0 against vocabulary_tokens_seen: 16"*) inherits the confusion. Tuning proposal in §6.3.

## 1.3 The mod-io second reading — it *does* apply, to a different scanner

On mod-io the equivalent zero was earned for `scan_gil_usage`'s own rules while the file's real lock was invisible to a different scanner. **The same is true here.** The concurrency control that actually governs this slice is per-object critical sections and atomics, and its visibility is partial:

| file | `Py_*_CRITICAL_SECTION` invocations | `FT_ATOMIC_*` | `_Py_atomic_*` | scanned by `scan_lock_discipline`? |
|---|---|---|---|---|
| `Objects/listobject.c` | 66 | 32 | 7 | ✅ |
| `Objects/bytearrayobject.c` | 68 | 10 | 0 | ✅ |
| `Objects/bytesobject.c` | **0** | **0** | 2 | ✅ |
| `Objects/bytes_methods.c` | **0** | **0** | 0 | ✅ |
| `Objects/clinic/listobject.c.h` | 16 | 0 | 0 | ❌ never scanned |
| `Objects/clinic/bytearrayobject.c.h` | 66 | 0 | 0 | ❌ never scanned |
| `Objects/clinic/bytesobject.c.h` | 0 | 0 | 0 | ❌ |
| **total** | **216** (108 regions) | **42** | **9** | **134 of 216 = 62% visible** |

So the honest one-line reading of the whole GIL/lock picture for this slice is:

> *This scope contains **zero** GIL-release constructs — a true negative over a 16-token, 7-file sweep — and **216** critical-section macro invocations plus **51** atomic accesses, of which **82 macro invocations (41 regions) are in clinic headers no scanner reads**. The GIL rule's zero is silence about the right thing; the lock rule's zero is silence about 38% of the population.*

And the third row of that table is itself a finding: **`Objects/bytesobject.c` has 0 critical sections and 0 `FT_ATOMIC_*` in 3,925 lines**, against 33 and 32 for `listobject.c` and 34 and 10 for `bytearrayobject.c`. That file-level asymmetry is the structural explanation for CPY-0182 and for G1 below, and it is unchanged on upstream `main` (fetched 2026-07-26: `grep -cE "Py_BEGIN_CRITICAL_SECTION|FT_ATOMIC_" Objects/bytesobject.c` → **0**).

---

# 2. Task (b) — the GIL-dependence sweep

*"Which operations are correct only because the GIL makes them atomic, and what is the FT story for each?"*

Method: I enumerated every access to the mutable state of the three types — `ob_item`, `allocated`, `ob_start`, `ob_bytes`, `ob_alloc`, `ob_exports`, `ob_bytes_object`, `ob_sval`, `ob_shash`, `it_index`, `it_seq` — **293 sites across the four files** (`scratchpad/sweep_ft_fields.py`), and classified each by the synchronisation available at that site: `ATOMIC` / `CS_SELF` / `CS_CLINIC` / `PLAIN`. Then I hand-read every `PLAIN` site on a *published, Python-reachable* object.

## 2.1 The result table

| type | field | writers | readers | GIL-dependent? |
|---|---|---|---|---|
| `list` | `ob_item` (base + elements) | **all 17 writers** use `FT_ATOMIC_STORE_PTR_RELEASE` **and** hold the object lock (`PyList_SetItem:464-473`, `ins1:504-505` under `PyList_Insert:543`, `_PyList_AppendTakeRefListResize:534` under `PyList_Append:543` / `_CALL_LIST_APPEND` `bytecodes.c:5067 DEOPT_IF(!LOCK_OBJECT)`, `list_ass_slice_lock_held`, `list_ass_subscript_lock_held`, `list_sort_impl`, `_STORE_SUBSCR_LIST_INT` `bytecodes.c:1425-1431`) | lock-free readers use `list_get_item_ref` / `_PyList_GetItemRefNoLock` / `_BINARY_OP_SUBSCR_LIST_INT`'s `#ifdef` arm | **no** |
| `list` | `ob_size` | `Py_SET_SIZE` → `_Py_atomic_store_ssize_relaxed` under `Py_GIL_DISABLED` (`Include/object.h:257`) | `PyList_GET_SIZE` → `_Py_atomic_load_ssize_relaxed` (`Include/cpython/listobject.h:32`) | **no** |
| `list` | `allocated` | plain at `:166, 192, 226, 885, 2973`; atomic at `:3190` — **all under the object lock** | lock-free readers use `list_capacity()` (`:54`) instead | **no** (style asymmetry only) |
| `bytes` | `ob_sval`, `ob_size` | written only before publication | — | **no** — immutable |
| `bytes` | `ob_shash` | `_Py_atomic_store_ssize_relaxed` (`:58`) | `_Py_atomic_load_ssize_relaxed` (`:71`) | **no** |
| **`bytes`** | **`it_seq`, `it_index`** | **5 plain accesses, 0 atomic, 0 lock** | same | **YES → G1** |
| `bytearray` | `ob_alloc` | `FT_ATOMIC_STORE_SSIZE_RELAXED` (`:51`) | `FT_ATOMIC_LOAD_SSIZE_RELAXED` (`:2538`, `:2747`) — **plus one plain read at `:1094`** | **partly → G2** |
| `bytearray` | `ob_start`, `ob_bytes` | plain, written only by `bytearray_reinit_from_bytes:49`; every caller holds the lock **except `bytearray___init___impl:926` and `:972`** | plain via `PyByteArray_AS_STRING` | **YES → G2** |
| `bytearray` | `ob_bytes_object` | plain; `:180` (construction), `:282`/`:285` (locked), **`:925` and `:971` (unlocked, in `__init__`)** | plain | **YES → G2** |
| `bytearray` | `ob_size` | `Py_SET_SIZE` atomic under FT; **`:1095` is an unlocked read-modify-write** | `Py_SIZE`/`PyByteArray_GET_SIZE` | **YES → G2** |
| `bytearray` | `ob_exports` | **22 accesses, 0 atomic.** 18 under a critical section (`bytearray_getbuffer:69` and `bytearray_releasebuffer:88` both take it; the 7 `ob_exports++/--` pin brackets are all inside CS-wrapped impls). **4 are not**: `:174` (construction, object not yet reachable — benign), **`:927` and `:938` (`bytearray___init___impl`, unlocked → G2)**, `:1210` (`bytearray_dealloc` — benign, teardown) | same | **YES at `:927`/`:938` → G2** |
| `bytearray` | iterator `it_index`, `it_seq` | `FT_ATOMIC_*` ×7; `it_seq` never dropped on FT (`#ifndef Py_GIL_DISABLED` at `:2997`) | same | **no** — this is the guarded twin |

**So the answer is bounded: of the 293 field-access sites, exactly two clusters are correct only because the GIL serialises them** — the five `striter_*` accesses in `bytesobject.c` (G1) and the whole of `bytearray___init___impl` (G2). `list` is fully retrofitted; `bytes` is immutable except for its iterator; `bytearray`'s data fields are locked everywhere except `__init__`.

---

# 3. Findings

## [FIX] G1 — CPY-0182 is a family of four, not one site: `striter_len`, `striter_reduce`, `striter_setstate` are TOCTOU on `it_seq` (`Objects/bytesobject.c:3461-3462`, `:3478-3479`, `:3494-3498`) · **NOVEL, REPRODUCED, TSan-CONFIRMED, LIVE ON MAIN**

### What

Group A recorded the double-DECREF in `striter_next:3451-3452` (CPY-0182). The *cause* is a design choice, and the design has four consumers:

```c
3451:    it->it_seq = NULL;      /* plain store, NOT inside #ifndef Py_GIL_DISABLED */
3452:    Py_DECREF(seq);         /* the only owning reference */
```

Because the sentinel *is* the field, every other accessor must test the field and then dereference it — in a separate expression:

```c
3461-3462  striter_len       if (it->it_seq)  len = PyBytes_GET_SIZE(it->it_seq) - it->it_index;
3478-3479  striter_reduce    if (it->it_seq != NULL) return Py_BuildValue("N(O)n", iter, it->it_seq, it->it_index);
3494-3498  striter_setstate  if (it->it_seq != NULL) { ... index > PyBytes_GET_SIZE(it->it_seq) ... }
```

Under the GIL there is no window between the test and the dereference. Under free threading a concurrent `next()` NULLs the field and frees the object inside it. `striter_reduce` is the worst: `Py_BuildValue`'s `"O"` code `Py_INCREF`s the object, so a *freed* `it_seq` becomes a UAF **write** to a recycled block, and the resulting tuple's `tuple_dealloc` then `Py_DECREF`s it.

`striter_next:3446-3448` has a second, independent instance of the same shape: `it->it_index` is read once for the bound check and **re-read** for the index, so N racing threads can drive it past `PyBytes_GET_SIZE(seq)` and read past `ob_sval`.

### Guarded twins — three, and the discriminator is one `#ifndef`

| twin | exhaustion sentinel | `it_seq` dropped on FT? | accessors gate on |
|---|---|---|---|
| `bytearrayobject.c:2979-3059` `bytearrayiter_*` | `it_index < 0`, `FT_ATOMIC_LOAD/STORE_SSIZE_RELAXED` | **no** — `Py_CLEAR` is inside `#ifndef Py_GIL_DISABLED` (`:2997`) | the atomic index (`:3011`, `:3033`, `:3049`) |
| `listobject.c:4065-4123` `listiter_*` | `it_index < 0`, `FT_ATOMIC_*` | **no** (`:4078`) | the atomic index (`:4093`) |
| `listobject.c:4226-4240` `listreviter_*` | same | **no** (`:4237`) | same |
| **`bytesobject.c:3435-3501` `striter_*`** | **`it_seq == NULL`, plain** | **YES, unconditionally** | **the pointer itself, in a separate expression** |

Because the twins never NULL `it_seq` on a free-threaded build, their plain `it->it_seq` dereferences (`bytearrayiter_length_hint:3013`, `listiter_setstate:4118`) are safe *by construction*. The single `#ifndef` is the whole difference.

### Measured — `repro/gil_striter_family_race.py`, driven by `repro/gil_matrix_driver.py`

One `drain-0` thread calls `next()`; 7 `probe-N` threads call **only** the accessor under test, so a crash in a `probe-*` frame is attributable to the accessor and not to `striter_next`. 300,000 fresh iterators per cell.

| build | `bytes_len` | `bytes_reduce` | `bytes_setstate` | `bytes_none` (drain only) |
|---|---|---|---|---|
| `debug-ft-nojit` | **SIGSEGV** | **SIGSEGV** | **SIGSEGV** | rc=0 |
| `release-ft-nojit` | rc=0 | **SIGSEGV** | rc=0 | rc=0 |
| `debug-gil-nojit` | rc=0 | rc=0 | rc=0 | — |
| `release-gil-nojit` | rc=0 | rc=0 | rc=0 | — |

**Guarded-twin control — the same script, the same thread mix, the same round count, only the iterator design changed: 12/12 clean.**

| build | `ba_len` | `ba_reduce` | `ba_setstate` | `list_len` | `list_reduce` | `list_setstate` |
|---|---|---|---|---|---|---|
| `debug-ft-nojit` | rc=0 | rc=0 | rc=0 | rc=0 | rc=0 | rc=0 |
| `release-ft-nojit` | rc=0 | rc=0 | rc=0 | rc=0 | rc=0 | rc=0 |

**Workload control:** `solo_bytes_len` / `solo_bytes_reduce` — one thread, same call volume — rc=0 on both FT builds. So the variable is concurrency, not volume.

Attribution, `debug-ft-nojit`, `bytes_reduce`, verbatim:
```
Fatal Python error: Segmentation fault
Stack (most recent call first):
  File ".../gil_striter_family_race.py", line 97 in prober      <- the PROBE thread, not the drainer
```
`addr2line` on the C frames: `tuple_dealloc` → `Include/refcount.h:360` (`Py_DECREF`) — the tuple `striter_reduce` built, carrying the freed `it_seq`.

**ThreadSanitizer, `release-ft-nojit-tsan`, 3 races** (process exited rc=66, log complete — checked before reading, per the taxonomy's partial-log trap):

```
Objects/bytesobject.c:3448:53 in striter_next   <-> striter_len:3462:50     (it_index)
Objects/bytesobject.c:3451:16 in striter_next   <-> striter_len:3461:13     (it_seq  -- THE TOCTOU PAIR)
__tsan_memset (allocator recycling the freed bytes) <-> striter_len:3462:15 (the UAF READ)
```
The second is the exact `if (it->it_seq)` / `it->it_seq = NULL` pair. The third is TSan catching the allocator scribbling on the block while `striter_len` reads through the stale pointer.

### Why this is a bug under CPython's own stated policy

Group A already cited gh-124397 point 3 — *"minimal changes necessary to cause them to not crash in a free-threaded build … concurrent access is allowed to return duplicate values, skip values, or raise an exception."* A SIGSEGV in `__length_hint__` is not a duplicate value. I add: the three twins in this slice **are** that minimal change, applied in gh-129107 (bytearray) and the 2024 list FT sweep. `bytesobject.c` received neither.

### Fix

Adopt the twins' design wholesale — not just in `next()`:
```c
/* striter_next */
    if (FT_ATOMIC_LOAD_SSIZE_RELAXED(it->it_index) < 0) return NULL;
    ...
    FT_ATOMIC_STORE_SSIZE_RELAXED(it->it_index, -1);
#ifndef Py_GIL_DISABLED
    Py_CLEAR(it->it_seq);
#endif
/* striter_len / _reduce / _setstate: gate on the atomic index, never on it_seq */
```
No inline-bytecode constraint applies (the mapper's §a point 1): `bytes` iteration is not reimplemented in `Python/bytecodes.c`; Group A's gdb frame shows `_PyForIter_VirtualIteratorNext` dispatching through `tp_iternext`.

### Live on main

Fetched `repos/python/cpython/contents/Objects/bytesobject.c` on 2026-07-26: `striter_next` and `striter_len` are **verbatim**, and the file still has **0** critical sections and **0** `FT_ATOMIC_*`.

### Prior art: none

`gh api -X GET search/issues` for `bytes iterator free-threading __length_hint__`, `striter_len race`, `bytes iterator __reduce__ thread`, `bytes iterator thread safety` returns only the gh-124397 umbrella and gh-129108 (`bytearray`, the twin). Nothing in `cpython-review-findings`.

### What I did not measure

I did not construct the out-of-bounds `ob_sval` read from the `it_index` double-read at `:3446`/`:3448` as a distinct observable; TSan reports the race but I did not show a value read past the end. Falsifier for that half: a run where `it_index` provably exceeds `PyBytes_GET_SIZE(seq)` and the returned int does not match any byte of the payload.

---

## [FIX] G2 — `bytearray.__init__` runs with no critical section; the append fast path is an unsynchronised read-modify-write on `ob_size` plus two writes through `ob_start` (`Objects/bytearrayobject.c:1094-1100`, and `:924-927`, `:971-972`) · **NOVEL, REPRODUCED, LIVE ON MAIN**

### What

`Objects/clinic/bytearrayobject.c.h:102` calls the impl with **no** `Py_BEGIN_CRITICAL_SECTION`, and the impl body has none either. The clinic header has **38 wrapper call sites**; 2 have no receiver (`fromhex` is a classmethod, `maketrans` a staticmethod), leaving **36 instance-method entry points**:

| clinic entry point | lock | why it is fine / not |
|---|---|---|
| 33 (`append`, `extend`, `insert`, `pop`, `remove`, `resize`, `strip`, `replace`, `split`, `join`, `translate`, `hex`, …) | `Py_BEGIN_CRITICAL_SECTION(self)` | — |
| `bytearray.__sizeof__` (`clinic:1883` → `:2743-2752`) | **none** | **safe by atomics** — its only state read is `FT_ATOMIC_LOAD_SSIZE_RELAXED(self->ob_alloc)` at `:2747` |
| `bytearray.clear` (`clinic:227` → `:1296-1302`) | **none** | **safe by delegation** — its whole body is `PyByteArray_Resize(self, 0)`, and `PyByteArray_Resize:293-300` takes the section itself |
| **`bytearray.__init__` (`clinic:102`)** | **none** | **neither** — it writes `self`'s fields *between* the locking calls |

The unsynchronised statements, on a live Python-reachable object:

```c
 924-927   if (self->ob_bytes_object == NULL) { ... bytearray_reinit_from_bytes(self,0,0); self->ob_exports = 0; }
 930       if (Py_SIZE(self) != 0) ...
 971-972   self->ob_bytes_object = encoded;  bytearray_reinit_from_bytes(self, size, size);
1094-1100  if (Py_SIZE(self) + 1 < self->ob_alloc) {          /* plain read of ob_alloc */
               Py_SET_SIZE(self, Py_SIZE(self) + 1);          /* lost-update RMW      */
               PyByteArray_AS_STRING(self)[Py_SIZE(self)] = '\0';
           }
           else if (PyByteArray_Resize(...) < 0) ...
           PyByteArray_AS_STRING(self)[Py_SIZE(self)-1] = value;   /* OOB write        */
```

Under the GIL, `:1094-1100` is atomic: the loop's only thread-switch point is `iternext(it)` at `:1076`, which is above them, and nothing between them runs Python. Under free threading they are not atomic with respect to any other thread, and `ob_size` can be driven past `ob_alloc`.

### Guarded twins — three, one of them the sibling `__init__` in this same slice

1. **`list___init___impl` (`Objects/listobject.c:3523-3541`)** — *the* twin. It also has **no clinic critical section** (`clinic/listobject.c.h:430`), and it is correct because it takes the section **itself**:
   ```c
   3528:    if (self->ob_item != NULL) {
   3529:        Py_BEGIN_CRITICAL_SECTION(self);
   3530:        list_clear(self);
   3531:        Py_END_CRITICAL_SECTION();
   3532:    }
   3534:    if (iterable != NULL) { if (_list_extend(self, iterable) < 0) ... }   /* takes CS2 */
   ```
   Same shape, same file family, opposite outcome.
2. **`bytearray_append_impl` (`:2119-2129`)** — the identical append arithmetic (`n = Py_SIZE(self); resize(n+1); AS_STRING(self)[n] = item;`) executed **inside** `Py_BEGIN_CRITICAL_SECTION(self)` from `clinic:1247`.
3. **`bytearray.clear`** — the other unlocked instance method, safe by full delegation.

### Measured — `repro/gil_bytearray_init_unlocked.py`, 10 s per cell, 4 threads

| build | `init_vs_append` | `init_vs_init` | `init_vs_read` | `append_vs_append` (locked-mutator control) | `init_solo` (workload control) |
|---|---|---|---|---|---|
| `debug-ft-nojit` | **SIGABRT** | **SIGABRT** | **SIGABRT** | rc=0 | rc=0 |
| `release-ft-nojit` | **SIGSEGV** | **SIGSEGV** | **SIGSEGV** | rc=0 | rc=0 |
| `debug-gil-nojit` | rc=0 | rc=0 | rc=0 | rc=0 | rc=0 |
| `release-gil-nojit` | rc=0 | rc=0 | — | rc=0 | rc=0 |

The `debug-ft-nojit` abort is CPython's own debug allocator catching the overflow, verbatim:
```
Fatal Python error: _PyMem_DebugRawRealloc: bad trailing pad byte
Stack (most recent call first):
  File ".../gil_bytearray_init_unlocked.py", line 70 in initer      <- b.__init__(gen())
```
*"bad trailing pad byte"* is a **write past the end of the allocated block** — exactly `PyByteArray_AS_STRING(self)[Py_SIZE(self)-1] = value` with a `Py_SIZE` another thread's lost update pushed past `ob_alloc`.

**ThreadSanitizer, `release-ft-nojit-tsan`, 51 races** (process exited rc=66). Distinct sites, ranked:

| races | site | statement |
|---|---|---|
| 8 | `bytearrayobject.c:1100:54` | `PyByteArray_AS_STRING(self)[Py_SIZE(self)-1] = value` — **the OOB write** |
| 7 | `__tsan_memcpy` | the buffer being reallocated under the write |
| 6 | `pyatomic_gcc.h:513` `_Py_atomic_store_ssize_relaxed` | `Py_SET_SIZE` racing plain `Py_SIZE` reads |
| 5 | `:1096:56` | `AS_STRING(self)[Py_SIZE(self)] = '\0'` |
| 6 | `:1094:13` / `:1094:39` | the `Py_SIZE(self) + 1 < self->ob_alloc` test |
| 4+3 | `:2123:20` / `:2128:36` | `bytearray_append_impl`, the *locked* partner of the race |
| 2 | `:924:15` | `self->ob_bytes_object == NULL` |
| 2 | `:49:37` | `bytearray_reinit_from_bytes` |

The `:1094-1100` ↔ `:2123/:2128` pairs are the finding in one line: the locked mutator and the unlocked `__init__` writing the same fields.

### The incomplete-migration story

**gh-129107 "bytearray is not free-thread safe"** (closed) / **PR #129108 "gh-129107: make bytearray free-thread safe"**, merged **2025-02-15**, `+904/-100` across 5 files, is the sweep that installed all 33 `@critical_section` annotations and the `*_lock_held` split. Its diff touches `Objects/bytearrayobject.c` in 30 hunks and **never modifies `bytearray___init___impl`** — the one hunk whose header names it (`@@ -954,8 +1059,9 @@ bytearray___init___impl`) is the enclosing context of a change to `bytearray_repr`. The reporter's own 300-line stress script exercises `clear`, `pop`, `append`, `insert`, `extend`, `remove`, `reverse`, `reduce`, `reduce_ex`, `count` … and **never `__init__`**. The method that was not in the repro is the method that did not get the lock. This is the gh-91153 precedent the history agent documented (two rounds three years apart, in this same file), one file over.

### Fix

Add `@critical_section` to the `bytearray.__init__` clinic input block and regenerate, exactly as the other 33 have. Note that `PyByteArray_Resize` (called at `:932`, `:1006`, `:1021`, `:1034`, `:1044`, `:1098`) takes the same per-object section, so the annotation alone is not sufficient — those calls must become `bytearray_resize_lock_held` (the `_lock_held` variant already exists at `:211`), the pattern PR #129108 used everywhere else.

### Live on main

`repos/python/cpython/contents/Objects/clinic/bytearrayobject.c.h`, fetched 2026-07-26: **34 `Py_BEGIN_CRITICAL_SECTION` (33 real + the include comment), and `bytearray___init___impl` is still called at `:102` with none**. PR #153498 (gh-153419, merged 2026-07-24) added `bytearray_new` and deleted the first-init branch, which removes the `:924-927` cluster but **not** the `:1094-1100` one, and adds no lock.

### Prior art: none

`gh api -X GET search/issues` for `bytearray __init__ critical section free-threading`, `bytearray init thread safety free threaded`, `bytearray___init___impl critical_section` returns gh-129107/#129108 (the sweep that missed it), gh-124397 and unrelated hits. Nothing in `cpython-review-findings`.

### What I did not measure

I did not isolate *which* of the four unsynchronised clusters produces the heap overflow — TSan names `:1100:54` most often and the debug allocator's message is consistent with it, but I did not build a variant that exercises only the `:1094-1100` path. I also did not attempt a controlled corruption (choosing what is written past the end).

---

## [POLICY] G3 — "the impl holds `self`'s critical section" is not a basis for carrying receiver-derived state across a Python-reaching call, on **either** build

*This is task (c) answered as a measurement rather than an argument, and it corrects an over-claim I was about to make.*

`Py_BEGIN_CRITICAL_SECTION` is released when the thread detaches: `_PyThreadState_Detach` (`Python/pystate.c:2335`) → `_PyCriticalSection_SuspendAll` (`:2323`), and `_PyCriticalSection_Resume` (`:2304`) re-takes it on attach. What that means in practice had not been measured anywhere in this slice.

**Measured** — `repro/gil_clinic_lock_window.py`. One thread runs a clinic-wrapped bytearray method whose user callback either **blocks** (detaches) or **busy-loops** (does not); a second thread then tries `b.clear()`, which needs the same per-object section via `PyByteArray_Resize`. The number is how long the mutator waited.

| probe | window | callback | `release-ft` | `debug-ft` | `release-gil` |
|---|---|---|---|---|---|
| `strip_busy` | **W2 — inside the lock** (`bytearray_strip_impl_helper:2391` `PyBuffer_Release` → `__release_buffer__`) | busy-loop 1.2 s | **waited 1.200 s** | **waited 1.200 s** | 0.016 s |
| `strip_w2` | same | blocking `Event.wait` | **0.000 s** | **0.000 s** | 0.000 s |
| `append_lock` / `append_busy` | **W1 — before the lock** (`clinic:1244` `_getbytevalue` runs above `Py_BEGIN_CRITICAL_SECTION` at `:1247`) | either | 0.000 s | 0.001 s | 0.016 s |
| `find_w1` | W1 (`_PyEval_SliceIndex`) | blocking | 0.000 s | 0.000 s | 0.000 s |

Three conclusions, each with the number behind it:

1. **On FT the section really does exclude** — the busy-loop mutator waited the full 1.200 s. So the lock is not decorative, and a blanket "critical sections don't protect across callbacks" would be wrong.
2. **It stops excluding the instant the callback detaches** — 1.200 s → 0.000 s, same site, same build, only the callback's blocking-ness changed.
3. **Under the GIL there is no such distinction** — 0.016 s in both cases, the 5 ms switch interval. So the FT lock is *strictly stronger* than the GIL for busy callbacks and *exactly as weak* for blocking ones.

**Consequence, and it is why upstream chose `ob_exports`.** The `ob_exports++/--` pin survives a detach because it is **object state, not a lock** — a suspended critical section is released, a bumped counter is not. That is the mechanical reason `bytearray_hex_impl:2673` (gh-143195) and `_bytearray_with_buffer:106` (gh-142560) are correct under both threat models while a site that relies on the enclosing critical section is correct under neither. This is the taxonomy's *"guarded twins are twin for a specific threat model"* entry, instantiated with a measurement.

**Corollary, measured — CPY-0180 is reachable with a callback that mutates nothing.** `repro/gil_clinic_lock_window.py strip_uaf_xthread`: `b.strip(x)` where `x.__release_buffer__` merely **blocks**, and a *second thread* does `b.clear(); b.extend(b"Z"*4096)`.

| build | `bytearray.strip()` returned |
|---|---|
| `debug-ft-nojit` | `bytearray(b'\xdd' * 21)` — the debug allocator's freed-memory fill |
| `release-gil-nojit` | `bytearray(b'\x10x\x13\x0b?~\x00\x00\x10x\x13\x0b?~\x00\x00\x00\x00\x00\x00\x00')` — heap pointers |
| `release-gil-nojit-asan` | **ASan abort** |
| `release-ft-nojit` | correct (timing) |
| expected | `bytearray(b'PAYLOADPAYLOADPAYLOAD')` |

The recorded CPY-0180 reproduction has `__release_buffer__` itself resize `self`. This shows the callback only has to *block*; the resize can come from another thread. That is an escalation of the recorded finding's reachability on **both** build families, not an FT-only issue — recorded here rather than re-litigated.

**Task (c)'s literal question — what is unprotected in the 18 pre-lock converter windows on FT?** Nothing of `self`. I checked all 18 wrappers: `_PyNumber_Index`, `_PyEval_SliceIndex` and `PyObject_GetBuffer` all operate on *arguments*; the only thing the wrapper touches of the receiver before `Py_BEGIN_CRITICAL_SECTION` is the `PyObject *self` pointer itself. So W1 does not create an unprotected access — it produces a `Py_ssize_t` computed with no lock that the impl then uses under the lock, and every impl must re-bounds-check. The two that don't are already known (gh-153570 `take_bytes`, gh-153578 `extend`/`setslice`). **The FT-specific effect of W1 is that it removes the *precondition*: under the GIL an adversarial `__index__` had to do the mutating; under FT an ordinary `b.clear()` on another thread suffices, at 0.000 s.**

---

## [ACCEPTABLE] G4 — `list_remove_impl:3410`'s raw `ob_item[i]` read (task d): **I agree with `refcount-auditor`, not with `git-history-context`** — and here is the premise neither of them measured

The history agent (FP-5) called it *"a free-threading retrofit that reached three of four call sites"*. `refcount-auditor` (§4) called it a defensible difference and ruled ACCEPTABLE, on three grounds. I reach the same verdict on my own evidence, and I upgrade their argument 2 from an assertion to a measurement.

**1. Their structural point holds, and I re-derived it independently.** `list.remove` is the only one of the four that runs under a lock (`clinic/listobject.c.h:391`); `index`, `count` and `__contains__` run unlocked, which is exactly why they need `list_get_item_ref`'s `Py_GIL_DISABLED` body (atomic `ob_item` load + `list_capacity` bound + `_Py_TryXGetRef`). Under a held lock all of that is redundant.

**2. Their load-bearing premise was asserted; I measured it, and it is true — the lock *is* dropped.** `repro/gil_critical_section_semantics.py detach_window`: thread A runs `L.remove(x)` where `x.__eq__` parks on an `Event`; thread B then appends.

| build | `remove_mutator_proceeded_while_eq_blocked` | waited |
|---|---|---|
| `release-ft-nojit` | **True** | 0.001 s |
| `debug-ft-nojit` | **True** | 0.001 s |
| `release-gil-nojit` | True | 0.001 s |
| `debug-gil-nojit` | True | 0.000 s |

So `_PyCriticalSection_SuspendAll` really does release `self`'s section mid-loop, on a Python-triggerable path, 4/4 builds. Their story is correct.

**3. The site survives it anyway, and the reason is stronger than "Resume re-acquires."** `Py_SIZE(self)` is the **loop condition**, re-evaluated every iteration, and `self->ob_item` is **re-loaded** on the same line as the index — both after `_PyCriticalSection_Resume`:
```c
3409:    for (i = 0; i < Py_SIZE(self); i++) {
3410:        PyObject *obj = self->ob_item[i];
3411:        Py_INCREF(obj);
3412:        int cmp = PyObject_RichCompareBool(obj, value, Py_EQ);
```
A shrink or a realloc during the window cannot produce an out-of-bounds load, because neither the bound nor the base survives the callback.

**4. I checked their falsifier myself.** *"A writer to `self->ob_item` or `Py_SIZE(self)` that does not take `self`'s critical section."* I enumerated all 17 `FT_ATOMIC_STORE_PTR_RELEASE` / `Py_SET_SIZE` writers in `listobject.c` and both specialised list uops. Every one is under the lock: `PyList_SetItem:464`, `PyList_Insert:543`, `PyList_Append:543`, `PyList_Sort:3217`, `list_ass_slice_lock_held`, `list_ass_subscript_lock_held`, `list_sort_impl` (clinic `:272`), `_STORE_SUBSCR_LIST_INT` (`bytecodes.c:1425 DEOPT_IF(!LOCK_OBJECT(list))`) and `_CALL_LIST_APPEND` (`bytecodes.c:5067 DEOPT_IF(!LOCK_OBJECT(self_o))`). **No unlocked writer exists.** The verdict does not flip.

**5. Two negatives worth recording, because I expected them to be positives.**
- *Re-entrant same-thread mutation does not deadlock.* `L.remove(x)` where `x.__eq__` does a **bounded** `L.append(99)` returns cleanly on all four builds (`reentrant_same=returned len=9`). My first version of this probe hung on all four — because appending unboundedly inside `remove`'s `__eq__` grows the list faster than the cursor advances, which is a well-known non-termination, not a lock defect. Recorded so nobody re-derives the wrong conclusion from the wrong probe.
- *The stale cursor does not remove the wrong element in the obvious construction.* `wrong_element`: with `del L[0]` executed from another thread while `__eq__` parks, the correct element is still removed on all four builds, because the cursor re-reads and simply skips one position. The residual Group A named is real but needs the *matching* element to shift across the cursor.

**Where I disagree with the history agent:** its framing ("the retrofit reached 3 of 4") implies a missed site. The four sites are not the same site — three run unlocked and one runs locked, and the accessor choice tracks that exactly. What *is* a real (cosmetic) asymmetry, and Group A's parenthetical already names it: `:3409` uses plain `Py_SIZE(self)` where the accessor path uses the relaxed-atomic `PyList_GET_SIZE`. Under the lock they are equivalent; switching costs nothing and would silence a future `atomic_plain_asymmetry` hit. I second that as the only actionable item here.

---

## [CONSIDER] G5 — CPY-0186's exposure on the free-threaded build (task e): wider by 17%, and the added region is the synchronisation state

CPY-0186 (`uninitialized-dealloc-auditor` U1) writes `memcpy(buf + lo, bytes, bytes_len)` at `bytearrayobject.c:631` where `buf` is `&_PyRuntime.static_objects.singletons.bytes_empty.ob_sval[0]` and both `lo` and the payload are Python-chosen.

**1. It reproduces on the FT builds.** Group A2 ran the `runtime` scenario (1 MB bytearray, `lo=200000`, 100 kB payload) only on the GIL builds. I ran their `repro/bytearray_setslice_resize_fail_oob.py` unchanged:

| build | result |
|---|---|
| `release-ft-nojit` | control rc=0; n=0,1 `MemoryError` clean; **n=2 SIGSEGV** |
| `debug-ft-nojit` | control rc=0; n=0,1 clean; **n=2 SIGSEGV** |

Combined with their four, the finding is now **6/6 builds**.

**2. The corruptible region is measurably larger, and structurally worse.** `nm -S` for `_PyRuntime`, and `gdb` for the field offsets:

| | `release-gil` | `release-ft` | `debug-gil` | `debug-ft` |
|---|---|---|---|---|
| `sizeof(_PyRuntime)` | 346,472 | **406,016** (+17.2%) | 362,496 | **424,448** (+17.1%) |
| `offsetof(static_objects)` | — | — | 30,224 | 30,224 |
| `offsetof(…singletons.bytes_empty)` — the write base | — | — | 63,184 | **79,664** |
| `sizeof(static_objects)` | — | — | 106,016 | **144,640** (+36%) |
| `offsetof(_main_interpreter)` | — | — | 136,240 | 174,912 |
| `sizeof(PyInterpreterState)` | — | — | 226,256 | **249,536** (+10.3%) |
| `offsetof(PyInterpreterState.stoptheworld)` | — | — | 10,784 | 10,816 |

`_main_interpreter` is the field **immediately after** `static_objects` and the **last** field of `struct pyruntimestate` (`Include/internal/pycore_runtime_structs.h:286-300`, with the in-code comment *"_main_interpreter should be the last field"*). So everything downstream of the write base is interpreter state, and on the FT build there is 23,280 more of it, plus 38,624 more `static_objects` — because on a free-threaded build every static `PyObject` carries `ob_tid` / `ob_ref_local` / `ob_ref_shared` instead of a single `ob_refcnt`. **A Python-chosen `lo` reaches `interp->stoptheworld` on either build** (`lo ≈ 83,800` on debug-gil, `≈ 106,000` on debug-ft).

**3. Three ways the exposure genuinely widens, ranked by how well I can support them.**

- **Measured.** The blast radius grows by 62 KB (debug) / 60 KB (release), and 100% of it is reachable from Python because `lo` is unconstrained.
- **Structural, read from the headers.** The added bytes are the free-threading machinery: `struct _stoptheworld_state` (interp+10,816), the per-interpreter `PyMutex`es, the QSBR shared state, `_PyBrcState`. On the GIL build those fields exist but are largely inert; on FT every object operation reads them. Corrupting a `PyMutex` byte is a **hang**, not a crash — a silent failure mode the GIL build does not have.
- **Structural, and I could not turn it into a measurement.** The `memcpy` runs while holding only `self`'s per-object critical section, which says nothing about `_PyRuntime`. On the GIL build no other thread can observe the intermediate state; on FT the write races every other thread's reads of the singletons, the interned strings and the interpreter state.

**4. What I measured for the concurrent-observation angle, and it came back mostly negative.** `repro/gil_runtime_oob_ft_exposure.py` runs 4 worker threads doing ordinary work (building the shared single-byte `bytes` singletons, dict lookups on interned names, lock acquire/release, attribute lookup) across the corruption, `lo ∈ {100, 90000}`, `n ∈ 0..5`:

| build | outcome |
|---|---|
| `release-gil-nojit`, `lo=100`, n=3/4 | **SIGSEGV in a `worker` thread** (`Thread-4 (worke)`, `File "<string>", line 17 in worker` — the `bytes([i])` line) |
| `release-gil-nojit`, `lo=90000`, n=3/4 | **SIGSEGV in the main thread, `Garbage-collecting`**; workers reported `bad=[]` and joined 4/4 |
| `release-ft-nojit`, both `lo` | **no interesting cell in n=0..5** |

I did not get an FT-side crash from this probe in the indices I swept, and I am not going to dress that up: **my concurrent-observation claim for FT is unverified.** The GIL run is the one that shows another thread faulting on the corrupted singleton, which is a nice demonstration that the corruption is process-global — but it is a demonstration on the *wrong* build for the claim I wanted. Falsifier/next step: sweep `n` further on FT (the FT allocator's index for the same scenario differs — Group A2 saw n=2 on FT vs n=3/4 on GIL for `runtime`), and target `lo` at the measured `interp->stoptheworld` offset per build rather than a build-independent constant.

**Net for CPY-0186's record:** the finding is build-family-independent (6/6), the FT build makes it *wider* (+17% region, all of it Python-reachable) and gives it a failure mode the GIL build lacks (lock corruption → hang), and the "concurrent observation" half is asserted, not measured.

---

## [ACCEPTABLE] G6 — the three plain `ob_exports` accesses outside a critical section that are **not** bugs

For completeness of the `ob_exports` denominator in §2.1, since `bytearray___init___impl`'s two are G2:

- `PyByteArray_FromStringAndSize:174` `new->ob_exports = 0` — the object is not yet reachable from Python. This is the twin `error-path-analyzer` F4 cites from the other side: the same write is correct at `:174` and a bug at `:927` purely because of reachability.
- `bytearray_dealloc:1210` `if (self->ob_exports > 0)` — refcount 0, teardown. The taxonomy's "teardown paths" FP class.
- `_canresize:115` and `_bytearray_with_buffer:106/108` read/write it plainly, but both are `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` callees.

No FT-atomic access to `ob_exports` exists anywhere (0 of 22), which the mapper flagged as a contrast with `memoryview`'s `FT_ATOMIC_ADD_SSIZE` (`memoryobject.c:1632/1641`). Given that 18 of 22 are under the object's own critical section and the remaining 4 are the three above plus G2, **the missing atomics are not independently a defect** — fixing G2 closes the gap. Recording the dismissal so it is not re-opened.

---

# 4. Classes bounded (with denominators)

| class | denominator | verdict |
|---|---|---|
| **GIL-release constructs** (`Py_BEGIN/END_ALLOW_THREADS`) | **16 spellings × 7 files** (4 `.c` + 3 `clinic/*.c.h`), 12,100 + ~5,000 lines; plus 0 file-local macro wrappers | **structural zero — genuinely absent, hand-verified.** Not a spelling problem. `mismatched_allow_threads` and `api_without_gil` have nothing to check |
| **`PyGILState_Ensure`/`Release` balance** | **0 occurrences of either** | **structural zero.** Directionally irrelevant here — this is `Objects/`, and the note in the agent contract about `Objects/` having 1 `PyGILState_Ensure` applies to the directory, not to this slice, which has 0 |
| **Blocking calls with the GIL held** | **45-name `BLOCKING_CALLS` set; 25 raw word hits, 0 in call form** | **earned zero.** All 25 are prose in comments/docstrings; the scanner's comment-stripping suppressed them correctly |
| **Per-object critical-section coverage of Python-visible `bytearray` entry points** | **38 clinic call sites → 36 taking `self`**, plus 14 protocol slots | **1 unlocked mutator (`__init__`, = G2).** `__sizeof__` is atomic, `clear` delegates, `fromhex`/`maketrans` have no `self`; all 14 slots lock (`bytearray_getbuffer:76`, `bytearray_releasebuffer:83`, `bytearray_length`, `bytearray_subscript`, `bytearray_ass_subscript`, `bytearray_iconcat`, `bytearray_irepeat`, `bytearray_repeat`, `bytearray_getitem`, `bytearray_setitem`, `bytearray_contains`, `bytearray_repr`, `bytearray_mod`, `bytearray_richcompare`) |
| **Same, for `list`** | **14 clinic call sites**, 8 with a clinic section | **clean for this rule.** The 6 without: `list.__init__` (`:3529` opens its own), `extend` (`_list_extend` takes `Py_BEGIN_CRITICAL_SECTION2`), `index`/`count` (lock-free `list_get_item_ref`), `__reversed__` (`PyList_GET_SIZE`, atomic). `__sizeof__` reads `self->ob_item` then `list_capacity()` unlocked — safe only because `ob_item` is freed through QSBR (`_PyMem_FreeDelayed`, `:65`); it is also the site of `init-bypass-checker`'s finding H and I do not re-litigate it |
| **Mutable-state field accesses across the slice** | **293 sites** over 11 fields, classified ATOMIC / CS_SELF / CS_CLINIC / PLAIN | **2 GIL-dependent clusters** — 5 `striter_*` accesses (G1) and `bytearray___init___impl` (G2). Every other `PLAIN` site is either on an unpublished object, in teardown, on immutable `bytes` data, or inside a lock |
| **Iterator owning-reference discipline** | **4 `tp_iternext` slots** in the slice | **1 broken family of 4 accessors** (`striter_*`, G1); 3 guarded (`listiter`, `listreviter`, `bytearrayiter`), each with the `#ifndef Py_GIL_DISABLED` elision and an atomic index |
| **Writers to `list`'s `ob_item` / `ob_size` outside the object lock** | **17 write sites** in `listobject.c` + 2 specialised uops in `bytecodes.c` | **zero.** This is Group A's falsifier for task (d) and it does not fire |
| **The 18 pre-lock clinic converters, as an FT hazard** | **18 wrappers** across 3 converter families | **0 unprotected accesses to `self`** — every converter operates on an argument. The FT effect is on the *precondition*, not on the window's contents (G3) |
| **Critical-section exclusion across a user callback** | 4 probes × 3 builds | **excludes across a non-detaching callback (1.200 s), does not across a detaching one (0.000 s)** — measured, G3 |

**Deliberately not re-litigated:** CPY-0180 (`strip` UAF — I add a reachability escalation in G3, not a re-derivation), CPY-0182 (`striter_next` — I add its three siblings), CPY-0186 (`setslice` OOB — task (e) only), gh-153570, gh-153578, gh-153419/#153498, CPY-0014, F1–F11, families A/B of the init-bypass report.

---

# 5. What I did not do

- **No parity oracle**, per the brief. Every verdict here rests on an exit code, a TSan race pair, a symbolised frame, or a measured wait time.
- **G5's concurrent-observation claim for FT is unverified** — my probe produced no interesting FT cell in `n ∈ 0..5`; the crash it did produce in a worker thread was on the *GIL* build. Stated as unmeasured rather than dressed up.
- **G1's out-of-bounds `ob_sval` read** (from the double-read of `it_index` at `:3446`/`:3448`) is TSan-confirmed as a race but I did not exhibit a value read past the end.
- **I did not build today's `main`.** The "live on main" claims for G1 and G2 are read from `gh api .../contents/...` (fetched 2026-07-26) — the source is verbatim and the clinic header still has no `__init__` section. Falsifier: build main and re-run both repros.
- **I did not file anything upstream.** G1 and G2 have no issue I could find; filing is the campaign's call.
- **`bytes_methods.c` contributes nothing to this agent.** It has no locks, no atomics, no GIL vocabulary and no per-object state — it receives `(const char *, Py_ssize_t)`. Its FT safety is entirely its callers' (the mapper's M-note, confirmed by `refcount-auditor` §2). Zero sites in my 293.
- **No JIT builds.** All measurements are on `*-nojit`. The specialised list uops I read in `bytecodes.c` are tier-1; I did not check the tier-2 executor variants.

---

# 6. Toolkit assessment

## 6.1 `scan_gil_usage` on this slice: the rule behaved, and its envelope told the truth about itself

`rule_not_applicable: true` plus the note is exactly the right design and it is what stopped this agent from certifying a clean result in one line. **Precision N/A, recall N/A — zero candidates against a hand-verified zero population.** The `_sample` block's "re-run, not post-hoc filter" claim held: my per-file re-run reproduced the same vocabulary counts.

Two defects, both in reporting rather than detection.

## 6.2 [HIGH VALUE] The rule that would have caught CPY-0182 — and its three siblings

`scan_ft_races` structurally could not see `striter_next`: `tree_sitter_utils.extract_functions` truncates `Objects/bytesobject.c` at line 2,779 of 3,926 (`parse_health` coverage **0.5667**, 37 ERROR nodes), so the entire iterator block is invisible. `refcount-auditor` §6.1 and `uninitialized-dealloc-auditor` §5.2 both diagnose the root cause (the `bytes_methods[]` table at `:2787-2843` interleaving brace initialisers with bare `BYTES_*_METHODDEF` macro names) and both propose the `parse_health()` canary. **I third that**, and add the datum that this scanner is immune to it in one specific way worth knowing: `scan_gil_usage` counts its vocabulary with a regex over the **whole file source**, not per-function, so `vocabulary_counts` is truncation-proof while `functions_analyzed: 331` is not. A reader must not treat the two numbers as having the same reach.

**But fixing discovery is necessary and not sufficient.** T3 as written targets the *drop* — `member = NULL; Py_DECREF(x)` — so with discovery fixed it would recover `striter_next` and stop there. It would still miss `striter_len`, `striter_reduce` and `striter_setstate`, which are the same bug and which is where two of my three FT crashes actually landed. So the rule I would add is not "find the drop":

> **`iterator_sentinel_field_asymmetry`.** For every type registering a `tp_iternext`:
> 1. Identify the **exhaustion sentinel** the iternext writes — either an index field set to a negative value, or an owning-object field set to `NULL`.
> 2. If the field-NULLing store is **not** inside `#ifndef Py_GIL_DISABLED` (i.e. it happens on a free-threaded build), mark the field *volatile-by-concurrency*.
> 3. Then flag **every method of that type** that tests the marked field and dereferences it in a *separate expression* — `if (it->F) ... G(it->F)`, `if (it->F != NULL) return H(..., it->F, ...)`.
> 4. Suppress when the sentinel is an index accessed through `FT_ATOMIC_LOAD_*` **and** the owning field is never NULLed on FT.

Measured against this slice: **4 true positives** (`striter_next:3451`, `striter_len:3461-3462`, `striter_reduce:3478-3479`, `striter_setstate:3494-3498`), **0 false positives** across the 12 twin accessors (`bytearrayiter_next/length_hint/reduce/setstate`, `listiter_next/len/reduce/setstate`, `listreviter_*`) — *including* `listiter_setstate:4114-4118`, which is textually the flagged shape (`if (it->it_seq != NULL) ... PyList_GET_SIZE(it->it_seq)`) and which step 2 correctly suppresses because `listiter_next:4078` NULLs `it_seq` only under `#ifndef Py_GIL_DISABLED`. **That suppression is the whole design of the rule** and it is what keeps precision at 4/4 rather than 4/16.

It generalises directly to the catalog: CPY-0067 (`arrayiter_next`), CPY-0062 (`elementiter_next`), CPY-0038 (`pairwise_next`), CPY-0039 (`islice_next`) and CPY-0026 (`ga_iternext`) are all recorded at the `next` site only. Whether any of them has a `__length_hint__`/`__reduce__` sibling with the same window is exactly the question this rule answers, and nobody has asked it.

Fixtures ready to use: positive `Objects/bytesobject.c:3435-3501`; negatives `Objects/bytearrayobject.c:2974-3062` and `Objects/listobject.c:4065-4123`.

## 6.3 [MEDIUM] `scan_gil_usage`'s envelope: two small changes that would have written §1 for me

**(i) `vocabulary_tokens_seen` is wrong under the merge policy.** `scan_common._denominators()` sets it to `len(vocabulary_counts)` — the number of *names checked*, 4 — and `merge_policy` declares it `"summed"`, so a 4-file per-file re-run reports **16**, which reads as "16 tokens seen" when the true answer is zero. Either rename it `vocabulary_names_checked` and set the merge policy to `"first"` (it is a constant, like `project_root`), or drop it and let `vocabulary_resolved` stand alone. Cheap; it directly caused the brief to phrase the zero as "0 resolved against 16 seen".

**(ii) When `rule_not_applicable` fires, say what vocabulary *is* present.** The note correctly says "either the constructs genuinely are absent, or they are spelled in a way the vocabulary does not cover", and then leaves the agent to spend an hour establishing which. On this slice a ten-line addition — count `Py_BEGIN_CRITICAL_SECTION`, `Py_BEGIN_CRITICAL_SECTION2`, `FT_ATOMIC_`, `_Py_atomic_`, `PyMutex_`, `Py_GIL_DISABLED` over the same stripped source and emit them as `adjacent_vocabulary` — would have printed:

```json
"adjacent_vocabulary": {"Py_BEGIN_CRITICAL_SECTION": 108, "FT_ATOMIC_": 42,
                        "_Py_atomic_": 9, "PyMutex_": 0, "Py_GIL_DISABLED": 23}
```

which says in one glance: *this scope's concurrency control is per-object locking and atomics, not GIL release; go read `scan_lock_discipline` and `scan_ft_races`.* That converts a structural zero from a dead end into a pointer, at essentially no cost, and it is the generalisable form of the lesson this slice and mod-io both paid for.

## 6.4 [HIGH VALUE] The rule that would have caught G2 — a per-type critical-section *denominator*, not a finding

`bytearray.__init__` is invisible to every current scanner for a compound reason: `scan_lock_discipline` never reads the clinic header (so it cannot know 33 siblings are locked and this one is not), and `scan_ft_races`'s T1 fired on `:924` and `:1094` at **`low` confidence** with no indication that the enclosing function is the only unlocked mutator of its type. The signal is not in any single site; it is in the **ratio**.

> **`clinic_critical_section_coverage`.** For every type with ≥1 `@critical_section` clinic method, parse the sibling `clinic/*.c.h` and emit, per type: the count of instance-method entry points, the count that take `Py_BEGIN_CRITICAL_SECTION(self)`, and the **names of those that do not**. Gate the *finding* (as opposed to the denominator) on: the unlocked impl assigns to a field of `self`, or calls `Py_SET_SIZE(self, …)`, **directly** — i.e. not merely by delegating to a locking helper.

On this slice, without the gate the denominator alone reads `PyByteArray_Type 33/36` (naming `__init__`, `clear`, `__sizeof__`) and `PyList_Type 8/14` (naming `__init__`, `extend`, `index`, `count`, `__sizeof__`, `__reversed__`) — already enough for a human to spot the odd one out. With the gate: **1 finding, `bytearray___init___impl`, 1/1 precision** — `bytearray.clear` excluded (body is one call to `PyByteArray_Resize`), `bytearray.__sizeof__` excluded (reads only an `FT_ATOMIC_*` field), `list.__init__` excluded (opens its own `Py_BEGIN_CRITICAL_SECTION` at `:3529`), `list_extend`/`index`/`count`/`__reversed__`/`__sizeof__` excluded (no direct field write).

This also **fixes the 41-region blind spot as a side effect** — you cannot compute the denominator without reading the clinic header, which is the thing three agents in this slice have now independently asked for. And the FP taxonomy already contains the entry that makes the gate necessary (*"the lock is in the Argument Clinic wrapper"* — listed as the single largest FP class of the retargeted T1 rule); this proposal is that entry inverted into a detector.

Fixtures: positive `Objects/clinic/bytearrayobject.c.h:102` + `Objects/bytearrayobject.c:1094-1100`; negatives `clinic/bytearrayobject.c.h:225-228` (delegating) and `Objects/listobject.c:3528-3532` (self-locking).

## 6.5 [MEDIUM] A taxonomy entry to add, with the number behind it

The FP taxonomy's *"guarded twins are twin for a specific threat model"* entry should gain the mechanism and the measurement:

> **A per-object critical section is released on thread detach.** `_PyThreadState_Detach` (`Python/pystate.c:2335`) calls `_PyCriticalSection_SuspendAll` (`:2323`); `_PyCriticalSection_Resume` (`:2304`) re-takes it. Measured on `release-ft-nojit` and `debug-ft-nojit`: with `bytearray.strip`'s `__release_buffer__` busy-looping (no detach) a competing `b.clear()` waits the full **1.200 s**; with the same callback blocking on an `Event` it gets in at **0.000 s**. Under the GIL there is no distinction — 0.016 s either way, the switch interval.
> **Consequence:** "the enclosing critical section is held" never justifies carrying a receiver-derived raw pointer or size across a Python-reaching call. Object-state guards (`ob_exports++`) survive a detach; locks do not. Do not accept a candidate on lock-held grounds alone.

## 6.6 What the informed method bought, and what it cost

**Bought, three times.** The guarded-twin rule produced G1 directly: I was building the §2.1 field table for task (b), noticed that three of four iterators gate on an atomic index and the fourth gates on the pointer, and the crash followed in one repro. It produced G2 the same way — the "which entry point is the odd one out" question is only askable once you have the twin population, and the answer was a one-line grep. And Group A2's discipline about *what it had not measured* is what made G3 reachable: their §4 stated the `_PyCriticalSection_Resume` mechanism as an argument, which is precisely what made it worth measuring.

**Cost, once, and it was mine not the method's.** I built the `append_lock`/`append_busy` probes believing `bytearray.append`'s `_getbytevalue` ran inside the critical section. It does not — `clinic/bytearrayobject.c.h:1244` runs it **before** `Py_BEGIN_CRITICAL_SECTION` at `:1247`. Two probes and one wrong conclusion ("the section never excludes") before I read the wrapper. This is the mapper's §e warning — *"you cannot trust the `.c` file alone"* — firing on an agent who had read the warning. The corrected `strip_busy` probe is the one that produced the real result. Recorded because the failure mode is instructive: I checked the *impl* and not the *wrapper*, which is exactly the trap the preflight named.

---

# 7. Artifacts

All under `reports/obj-sequences/repro/`, all `.py` files, no heredocs:

- `gil_striter_family_race.py` — **G1**; 9 scenarios (3 bytes probes × drain/len/reduce/setstate, 6 twin controls, solo control), one probe per subprocess
- `gil_bytearray_init_unlocked.py` — **G2**; 5 scenarios incl. the locked-mutator and single-thread controls
- `gil_critical_section_semantics.py` — **task (d)**; `detach_window`, `contains_detach`, `reentrant_same`, `reentrant_read`, `wrong_element`
- `gil_clinic_lock_window.py` — **task (c) / G3**; `strip_w2`, `strip_busy`, `find_w1`, `append_lock`, `append_busy`, `append_busy_nogc`, `hold_only`, `strip_uaf_xthread`
- `gil_runtime_oob_ft_exposure.py` — **task (e)**; 4 worker threads across the corruption, `lo`-parameterised
- `gil_matrix_driver.py` — build-matrix driver (one subprocess per cell, timeout, `PYTHON_GIL` per build family)
- `gil_sweep_ft_fields.py` — the 293-site field-access classification behind §2.1 (also at `scratchpad/sweep_ft_fields.py`)

TSan logs: `/tmp/tsan_striter.log` (3 races), `/tmp/tsan_bainit.log` (51 races). Both processes exited (rc=66) before the logs were read.

---

## Summary line

```
FIX=2  CONSIDER=1  POLICY=1  ACCEPTABLE=2
scan_gil_usage: 0 candidates / 0 population -- STRUCTURAL ZERO, hand-verified across 16 spellings x 7 files
  -> "no constructs present", NOT "constructs present and verified clean"
adjacent vocabulary the rule cannot see: 108 critical-section regions (41 of them clinic-only), 42 FT_ATOMIC, 9 _Py_atomic
novel reproduced findings: 2 -- striter_* family TOCTOU/UAF (3 FT SIGSEGV + 3 TSan races, 12/12 twin control clean)
                                bytearray.__init__ unlocked (heap overflow, 6/6 FT crash, 51 TSan races, 4/4 GIL clean)
task (d) tie-breaker: ACCEPTABLE -- agree with refcount-auditor, disagree with git-history-context; lock-drop measured 4/4
task (e): CPY-0186 reproduces on FT (6/6 builds total); region +17.2%, +23,280 bytes of PyInterpreterState in the blast radius
```
