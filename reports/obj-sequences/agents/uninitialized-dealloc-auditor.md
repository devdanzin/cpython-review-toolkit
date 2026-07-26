# uninitialized-dealloc-auditor — slice `obj-sequences` (Group A2)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-verified independently — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files is **empty**. Every line number below is valid on every matrix build without adjustment.

**Read first:** `AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/git-history-context.md`, `agents/refcount-auditor.md`, `agents/error-path-analyzer.md`, `preflight/informed_briefing.md`.

**Pre-run scanner:** `scan_uninit_dealloc.sample.json` — 1 finding, denominators `files_analyzed: 4`, `functions_analyzed: 331`, `allocation_sites: 5`.

---

## Headline

**One novel, reproduced, Python-reachable out-of-bounds WRITE into `_PyRuntime`.** A `bytearray` slice
assignment whose reallocation fails leaves `Py_SIZE(self)` **negative** and `ob_start` pointing at the
interpreter's immortal empty-`bytes` singleton, and then `memcpy`s attacker-chosen bytes at an
attacker-chosen offset into it. Reproduced **6/6 builds**, ASan-confirmed as
`global-buffer-overflow WRITE`, **still live on today's `main`**, no prior art.

The cause is an **incomplete migration**: `732224e1139` (gh-139871, 2025-11-13) changed
`bytearray_resize_lock_held` to *destroy* the buffer on allocation failure, invalidating a recovery
path Victor Stinner wrote in 2013 (`8455723cfb0c`, bpo-19568 / gh-63767) that is still in the tree
assuming the buffer survives.

Secondary: the scanner's `allocation_sites: 5` denominator is **short by one**, and the missing site is
in the `bytesobject.c` region the tree-sitter truncation hides.

---

# 1. Findings

## [FIX] U1 — `bytearray_setslice_linear`'s bpo-19568 recovery runs on an object `bytearray_resize_lock_held` has already destroyed → negative `Py_SIZE` and an OOB WRITE into `_PyRuntime` (bytearrayobject.c:586-605, :631) · **NOVEL, REPRODUCED, LIVE ON MAIN**

### What

`bytearray_resize_lock_held` no longer leaves the buffer intact when its reallocation fails. Since
`732224e1139` it installs the immortal empty `bytes` and zeroes size and alloc:

```c
280      int ret = _PyBytes_Resize(&obj->ob_bytes_object, alloc);
281      if (ret == -1) {
282          obj->ob_bytes_object = Py_GetConstant(Py_CONSTANT_EMPTY_BYTES);
283          size = alloc = 0;
284      }
285      bytearray_reinit_from_bytes(obj, size, alloc);   /* ob_bytes = ob_start = empty sval */
```

`bytearray_setslice_linear`'s shrink branch still recovers as if the old buffer were there:

```c
586      if (bytearray_resize_lock_held((PyObject *)self, Py_SIZE(self) + growth) < 0) {
588          /* Issue #19578: Handling the memory allocation failure here is
589             tricky here because the bytearray object has already been modified. ... */
597          if (lo == 0) {
598              self->ob_start += growth;               /* growth<0 -> ob_start BELOW ob_bytes */
599              return -1;
600          }
601          /* memmove() removed bytes, the bytearray object cannot be
602             restored in its previous state. */
603          Py_SET_SIZE(self, Py_SIZE(self) + growth);  /* 0 + negative == NEGATIVE */
604          res = -1;
605      }
606      buf = PyByteArray_AS_STRING(self);              /* == &bytes_empty.ob_sval[0] */
...
630      if (bytes_len > 0)
631          memcpy(buf + lo, bytes, bytes_len);         /* OOB WRITE into _PyRuntime */
```

`Py_SIZE(self)` is **0** on entry to `:603` (the failure handler just zeroed it), so
`Py_SET_SIZE(self, 0 + growth)` writes a negative size. `buf` is
`&_PyRuntime.static_objects.singletons.bytes_empty.ob_sval[0]`, and the next members of that struct
are `bytes_characters[256]` — the shared single-byte `bytes` objects — then
`struct _Py_global_strings` (`Include/internal/pycore_runtime_structs.h:102-126`). Both `lo` and the
assigned bytes come straight from Python, so this is a bounded **write-what-where** into the
interpreter runtime.

Reachable from `b[lo:hi] = shorter` and `del b[lo:hi]` with `lo != 0`. The ASan stack shows the entry
point is `mp_ass_subscript`, not `bytearray_setslice`, so PR #153579's `setslice` clamp reorder does
not touch it.

### Measured — `repro/bytearray_setslice_resize_fail_oob.py`, dense `set_nomemory` sweep, controls on every build

**`state`** — `b = bytearray(b'x'*4000); b[100:4000] = b'\xAA'*4`, failure index n=2/3:

| build | result |
|---|---|
| `release-gil-nojit` | rc=0. `len(b)` → **`SystemError: <built-in function len> returned NULL without setting an exception`**; `bytes(b)` → `ValueError: size must be >= 0`; `b.append(65)` → `ValueError: Can only resize to positive sizes, got -3895`. The bytearray is **permanently unusable** |
| `release-ft-nojit` | identical |
| `release-gil-nojit-asan` | identical (the 4-byte write stays inside `_PyRuntime`, crosses no redzone — see the caveat below) |
| `debug-gil-nojit` | **SIGABRT** — `Fatal Python error: _Py_CheckSlotResult: Slot __len__ of type bytearray failed without setting an exception` |
| `debug-ft-nojit` | **SIGABRT** — same |
| control (no injection, all 6) | `len=104`, correct contents |

`state_del` (`del b[100:4000]`, `bytes_len == 0`) reproduces the negative size **without** any write —
the corrupted size and the OOB write are independent halves.

**`singleton`** — same, but 40 bytes at `lo=100`, so the write lands on `bytes_characters[]`:

| build | result |
|---|---|
| `release-gil-nojit` | `b'\x00'` ok, `b'\x01'` ok, **`bytes([2])` → SIGSEGV** in `_PyObject_MakeTpCall` |
| `debug-gil-nojit` | **SIGSEGV**, same point |
| `release-ft-nojit` / `debug-ft-nojit` | **SIGSEGV** one singleton earlier (`bytes([1])`) |
| `release-gil-nojit-asan` | **SIGSEGV** |
| control (all 6) | all six singletons correct |

Note what this means: the fault is not in the failing operation. It is in the **next unrelated line of
Python** that touches a corrupted interpreter singleton.

**`runtime`** — `b = bytearray(b'x'*1_000_000); b[200_000:1_000_000] = b'\xAA'*100_000`:

| build | result |
|---|---|
| `release-gil-nojit` | **SIGSEGV (rc=-11)** at n=3/4 |
| `debug-gil-nojit` | **SIGSEGV** |
| `release-gil-nojit-asan` | **`AddressSanitizer: global-buffer-overflow`** at n=2 |

ASan report, verbatim:

```
==128296==ERROR: AddressSanitizer: global-buffer-overflow
WRITE of size 100000 at 0x57c87946da88 thread T0
    #0 __asan_memcpy
    #1 bytearray_setslice_linear          Objects/bytearrayobject.c:631:9
    #2 bytearray_ass_subscript_lock_held  Objects/bytearrayobject.c:824:16
    #3 bytearray_ass_subscript_lock_held  Objects/bytearrayobject.c:806:15
    #4 _PyEval_EvalFrameDefault           Python/generated_cases.c.h:12565:27
0x57c87946da88 is located 0 bytes after global variable '_PyRuntime'
    defined in 'Python/pylifecycle.c:122' (0x57c87b78120) of size 346472
```

**ASan caveat worth recording:** the small-write variants are *invisible* to ASan because the write
stays **inside** the 346 KB `_PyRuntime` global and never crosses a redzone. ASan only reports the
variant large enough to run off the end. A reviewer who ran only the small case on an ASan build and
saw nothing would have concluded the class was clean — the plain-build value inspection
(`len()` → `SystemError`, `bytes([2])` → SIGSEGV) is what actually shows the bug. This is the
task's point (e) landing on a *sanitizer*, not just on an exit code.

### The `lo == 0` half (Group A's unverified second-order) — **verified, and it is the benign one**

`error-path-analyzer` F7 predicted that `:598` `self->ob_start += growth` drives `ob_start` below
`ob_bytes` and trips `assert(logical_offset <= alloc)` at `:224`, and recorded it as unverified.
**Confirmed:**

| build | `lo_zero` at the failure index |
|---|---|
| `debug-gil-nojit` | **SIGABRT** — `Objects/bytearrayobject.c:224: Assertion 'logical_offset <= alloc' failed` |
| `debug-ft-nojit` | **SIGABRT** — same |
| `release-gil-nojit` / `release-ft-nojit` | rc=0, benign — the `size_t` underflow forces the growing branch, `memmove` copies `Py_MIN(1, 0) == 0` bytes, and the object self-heals |

So the arm Group A named is the debug-only one; the arm they did not name (`lo != 0`, `:603`/`:631`)
is the memory-safety one. Both come from the same root cause and take the same fix.

### Guarded twins — three, one of them written by upstream two days ago

1. **`bytearray_take_bytes_impl`, on today's `main` (`:1632-1638`).** PR #153498 (gh-153419, merged
   2026-07-24) fixed Group A's F5 with exactly the repair this site needs:
   ```c
   if (_PyBytes_Resize(&self->ob_bytes_object, to_take) == -1) {
       assert(self->ob_bytes_object == NULL);
       self->ob_bytes_object = Py_GetConstant(Py_CONSTANT_EMPTY_BYTES);
       bytearray_reinit_from_bytes(self, 0, 0);
       ...
   ```
   `bytearray_setslice_linear` is the sibling that PR did not sweep — and note it does the *opposite*:
   it takes the already-repaired object and un-repairs it.
2. **The same function's own `growth > 0` arm, 9 lines below** (`:614-617`): identical callee,
   identical failure, and it simply `return -1`s without touching a single field.
3. **`Objects/listobject.c:143-149` / `:180-186`, in this slice.** `list_resize` answers the same
   question the right way twice: *"Never fail when shrinking allocations"* — it shrinks by adjusting
   `ob_size` only and returns 0, and on a real failure leaves `ob_item`, `allocated` and `ob_size`
   exactly as they were. `bytearray` shrink-failure destroys all four fields.

### Sibling sweep — 20 call sites, 1 defective

Every caller of `bytearray_resize_lock_held` / `PyByteArray_Resize` in `bytearrayobject.c`
(`:368, 437, 586, 614, 862, 932, 1006, 1021, 1034, 1044, 1098, 1299, 1522, 1715, 2011, 2125, 2241,
2260, 2314, 2347` — 20 sites) read by hand. **19 return the sentinel immediately and mutate no field
of `self` after the failure. `:586` is the only one that does not.** `:862` (extended-slice delete)
does its `memmove` *before* the resize, so a failure there loses data — that is Group A's F7 class, not
this one: no negative size, no write.

### Fix

The minimal, in-keeping-with-upstream change is to delete the 2013 recovery, since its premise is
gone:

```c
         if (bytearray_resize_lock_held((PyObject *)self,
                                Py_SIZE(self) + growth) < 0) {
-            /* Issue #19578: ... */
-            if (lo == 0) {
-                self->ob_start += growth;
-                return -1;
-            }
-            /* memmove() removed bytes, the bytearray object cannot be
-               restored in its previous state. */
-            Py_SET_SIZE(self, Py_SIZE(self) + growth);
-            res = -1;
+            /* bytearray_resize_lock_held() has already reset self to the
+               empty bytes on failure (see :281-285); the object is
+               self-consistent and must not be adjusted further. */
+            return -1;
         }
```

That makes the shrink arm match the growth arm at `:614-617`, `take_bytes` on main, and the other 19
call sites. The remaining behaviour change (a failed shrink empties the bytearray instead of leaving
it alone) is Group A's F7 and wants the separate, larger fix of resizing into a fresh object.

The comment's `Issue #19578` is a typo for **bpo-19568 = gh-63767**, closed 2013-11-21; worth fixing
in passing so the next reader can find the history.

### Prior art — none

`gh api -X GET search/issues` for `bytearray setslice MemoryError negative size`,
`bytearray_setslice_linear`, `bytearray resize failure ob_start`,
`bytearray slice assignment MemoryError corrupt`, `bytearray negative length`,
`bytearray Py_SET_SIZE negative MemoryError`, `bytearray setslice out of bounds write allocation
failure`, `_PyBytes_Resize bytearray failure empty`, `bytearray _PyRuntime overflow`,
`bytearray corrupt singleton MemoryError` → the only hit is **gh-63767 (bpo-19568, closed 2013-11-21)**,
which is the origin of the now-stale recovery. Nothing in `cpython-review-findings`.

**Merge-date check, per the brief.** Both halves are **verbatim on today's `main`** (fetched
2026-07-26 via `gh api repos/python/cpython/contents/Objects/bytearrayobject.c`): the wrecking handler
at main `:291-295`, the stale recovery at main `:608-615`. Unlike gh-153570 and gh-153419 this one was
**not** fixed nine days after our ref. It is live and fileable.

---

## [CONFIRMED — known] CPY-0014 · `PyList_New` — `Objects/listobject.c:250 → 262`, destructor `:567`

The scanner's single finding. Present verbatim at the ref: `op = PyObject_GC_New(PyListObject,
&PyList_Type)` at `:250`, then under `#ifdef Py_GIL_DISABLED` a failing `list_allocate_array` reaches
`Py_DECREF(op)` at `:261` with `ob_item`, `ob_size` and `allocated` all still allocator garbage;
`list_dealloc:560-570` tests `op->ob_item != NULL`, uses `Py_SIZE(op)` as a loop bound over
`Py_XDECREF(op->ob_item[i])`, then `free_list_items(op->ob_item, false)`. Confirmed, not re-litigated.

Two things worth adding, because both bear on how the class should be triaged generally:

- **`list_dealloc:559` uses `PyObject_GC_UnTrack` — the untracked-*tolerant* function, not the
  unchecked `_PyObject_GC_UNTRACK` macro.** By the severity rule in my own instructions this predicts a
  *latent*, non-deterministic instance rather than an `odictiter_new`-style deterministic abort, and
  that is what I measured.
- **`PyList_New` pops the freelist first** (`_Py_FREELIST_POP(PyListObject, lists)` at `:248`), and
  `list_dealloc:569` sets `op->ob_item = NULL` *before* pushing. A freelist block therefore always comes
  back clean, and the `ob_item != NULL` guard makes the crash conditional on a **dirty recycled
  pymalloc block**. This is the gh-151815 shape exactly.

**Reproduction attempt: 0/25 on `release-ft-nojit`, 0/25 on `debug-ft-nojit`**
(`repro/list_new_uninit_dealloc_ft.py`: drains the list freelist by holding 4,000 lists alive, dirties
the 64-byte size class with 20,000 two-tuples and 4,000 small dicts, then sweeps `set_nomemory`
indices 0-24). Per the FP taxonomy's explicit instruction this is recorded as **"unstable trigger",
not "fixed"** — the catalog status stays `reproduced`.

---

## [ACCEPTABLE] U2 — `PyByteArray_FromStringAndSize:164-181` is the in-slice guarded twin, and its comment names the exact failure mode

```c
164      new = PyObject_New(PyByteArrayObject, &PyByteArray_Type);   /* non-zeroing */
...
170      /* Fill values used in bytearray_dealloc.
171         In an optimized build the memory isn't zeroed and ob_exports would be
172         uninitialized when when PyBytes_FromStringAndSize errored leading to
173         intermittent test failures. */
174      new->ob_exports = 0;
178      new->ob_bytes_object = PyBytes_FromStringAndSize(NULL, size);
179      if (new->ob_bytes_object == NULL) {
180          Py_DECREF(new);
```

At `:180` exactly two fields are initialized — and `bytearray_dealloc:1207-1217` reads exactly those
two: `self->ob_exports` (the scalar, 0) and `Py_XDECREF(self->ob_bytes_object)` (NULL, no-op).
`ob_alloc`, `ob_bytes`, `ob_start` and `ob_size` are still garbage and the destructor never touches
them. Correct, and the scanner correctly stayed silent. This is the file's own record of having been
bitten by this class once already and fixing precisely the member the destructor reads.

## [ACCEPTABLE] U3 — the four iterator constructors

`list_iter:4029`, `list___reversed___impl:4188`, `bytes_iter:3547`, `bytearray_iter:3110`: in all four
the only statements between the allocation and full initialization are `it->it_index = …` and
`it->it_seq = Py_NewRef(seq)`. `Py_NewRef` is infallible; there is no early free at all. Real
negatives — including `bytes_iter`, which the scanner never saw (§3).

## [CONSIDER] U4 — `byteswriter_resize` leaves `writer->size` describing a buffer it just freed (bytesobject.c:3622-3644)

```c
3634      if (writer->obj != NULL) {
3635          if (writer->use_bytearray) { if (PyByteArray_Resize(writer->obj, size)) return -1; }
3641          else { if (_PyBytes_Resize(&writer->obj, size)) return -1; }   /* obj now NULL, old freed */
3644          assert(writer->obj != NULL);
```

The same shape as U1, one layer up: `_PyBytes_Resize` NULLs `writer->obj` and frees the old bytes, and
`byteswriter_resize` returns -1 without resetting `writer->size` (neither `PyBytesWriter_Resize:3828`
nor `PyBytesWriter_Grow:3855` updates it on the failure path). The writer then silently reverts to its
256-byte inline `small_buffer` — `_PyBytesWriter_GetData` returns `writer->small_buffer` when
`obj == NULL` — while `writer->size` still claims the old, larger length. A caller that ignores the
-1 and calls `PyBytesWriter_Finish` gets
`PyBytes_FromStringAndSize(writer->small_buffer, writer->size)` — an OOB **read** of
`size - 256` bytes past a `PyMem_Malloc`'d 280-byte struct.

**Why CONSIDER, not FIX:** I found no in-tree caller that ignores the failure. All eleven writer
consumers I checked (`bytes_methods.c:364`, `memoryobject.c:2379/2435`, `longobject.c:6407`,
`unicodeobject.c:4781/5991/6327/6789/7035/7212/7906`) either `Discard` or propagate. `PyBytesWriter_*`
is public C API since 3.15, so a third-party caller is the exposure. **Not reproduced** — I did not
construct a `_testcapi` driver for it, and I say so rather than asserting reachability.

**One-line neighbour, out of my class, recorded so it is not lost:** `PyBytesWriter_Format:3911-3925`
does not check `bytes_fromformat`'s NULL return — `buf - byteswriter_data(writer)` on a NULL `buf` is
UB and yields a large negative, which `PyBytesWriter_Resize` then converts into
`ValueError: size must be >= 0`, destroying whatever the format machinery raised. Guarded twin
**`PyBytes_FromFormatV:382-386`**, which does check. Only `_testcapi` calls it in-tree. Both are in the
region §3 shows the scanner cannot see.

## [ACCEPTABLE] U5 — the `bytes` constructors and `PyBytes_Concat`

`_PyBytes_FromSize:102-130`, `PyBytes_FromStringAndSize:134-158`, `PyBytes_FromString:162-192` and
`_PyBytes_Repeat:1586-1621` (the `PyObject_Malloc` at `:1609`) all use raw `PyObject_Malloc`/`Calloc`
and complete initialization with no
intervening fallible call and no early free. `PyBytes_Concat:3275-3321`'s `error:` label does
`Py_CLEAR(*pv)` after `_PyBytes_Resize` already NULLed it — a documented no-op, and the NULL-on-failure
contract is stated at `:3335-3337`. Real negatives.

---

# 2. Classes bounded (with denominators)

| class | denominator | verdict |
|---|---|---|
| **Non-zeroing allocation freed before its members are initialized** | **6** ground-truth sites (scanner reported 5 — §3) | **1 known-live** (`PyList_New`, CPY-0014, FT-only, latent); 5 clean, of which `PyByteArray_FromStringAndSize:164` is the guarded twin with the failure mode in its own comment |
| **`_PyBytes_Resize(&<member of a live object>)` failure leaves the object describing freed memory** | **5** in-slice call sites: `bytesobject.c:2377, 3302, 3644, 3763`; `bytearrayobject.c:280, 1609` | `:2377` returns the value directly (NULL propagation *is* the handling); `:3302` is contract-documented; **`:3644`/`:3763` = U4 (CONSIDER)**; `:280` repairs; `:1609` = Group A F5, fixed upstream after the ref |
| **A caller that mutates `self` after a failed resize** | **20** `bytearray_resize_lock_held` / `PyByteArray_Resize` call sites in `bytearrayobject.c` | **1 defective** (`:586`, = U1). 19 return the sentinel and touch nothing |
| **`list` resize/constructor failure paths** | `list_resize` (both `#ifdef` arms), `list_preallocate_exact`, `list_new_prealloc`, `PyList_New` | **clean by design** — "Never fail when shrinking allocations"; on real failure `ob_item`/`allocated`/`ob_size` are left untouched. This is the slice's best twin for the whole class |
| **`bytes` constructors** | 4 raw-`PyObject_Malloc` sites (`:119, :121, :185, :1609`) | **clean** — no fallible step between allocation and initialization, no early free, and `bytes` has no member pointer for a destructor to read |
| **`bytearray.__init__` resize failure paths** | 6 (`:932, 1006, 1021, 1034, 1044, 1098`) | **clean** — every one returns -1 (or `goto fail` → release buffer → -1) with no post-failure field write. `:1044`'s `PyByteArray_Resize(self, 0)` cannot fail (`_PyBytes_Resize`'s `newsize == 0` arm returns 0 unconditionally) |
| **Allocation-failure sweep over every Python-reachable constructor and resize path in the slice** | **70 scenarios; 903 real allocation-failure points on `release-gil-nojit`, 777 on `debug-ft-nojit`** | **1 scenario crashes** (`bytearray.take_bytes`, = Group A F5). See §4 for the index map and for why a clean row here is not an exoneration |

**Deliberately not re-litigated:** CPY-0180 (`bytearray.strip`), gh-153570 (`take_bytes` stale size),
gh-153578 (`extend`/`setslice` clamp order), gh-153419 / F4 / F6 (`__init__`), F5 (`take_bytes` resize
failure), F7 (resize-failure data loss — I escalate only its *second-order* half, as U1). All owned by
Group A; confirmed present, cited, not re-derived.

---

# 3. Task (f) — the tree-sitter truncation, measured against my own denominator

**The caveat applies, and it costs exactly one allocation site.**

`tree_sitter_utils.extract_functions` stops at `bytes_getnewargs` (`start_line 2779`) in
`Objects/bytesobject.c`; the file is 3,926 lines. Measured directly
(`scratchpad/check_trunc.py`, using the toolkit's own `parse_health`):

| file | file lines | functions found | last function | `parse_health` coverage | `error_nodes` |
|---|---|---|---|---|---|
| `bytesobject.c` | 3,926 | **67** | `bytes_getnewargs` @2779 | **0.5667** | 37 |
| `listobject.c` | 4,313 | 132 | `listiter_reduce_general` @4280 | 0.8129 | 11 |
| `bytearrayobject.c` | 3,126 | 106 | `bytearray_iter` @3109 | 0.7156 | 10 |
| `bytes_methods.c` | 739 | 26 | `_Py_bytes_isascii` @728 | 0.7605 | 0 |

`132 + 67 + 106 + 26 = 331` — exactly the envelope's `functions_analyzed`.

**Effect on `allocation_sites: 5`.** Ground truth by `grep` for the scanner's own
`allocator_model.non_zeroing` set is **6**:

| # | site | function | seen by scanner? | verdict |
|---|---|---|---|---|
| 1 | `listobject.c:250` | `PyList_New` | yes | **CPY-0014** |
| 2 | `listobject.c:4037` | `list_iter` | yes | clean |
| 3 | `listobject.c:4193` | `list___reversed___impl` | yes | clean |
| 4 | `bytearrayobject.c:164` | `PyByteArray_FromStringAndSize` | yes | clean — the guarded twin |
| 5 | `bytearrayobject.c:3118` | `bytearray_iter` | yes | clean |
| **6** | **`bytesobject.c:3555`** | **`bytes_iter`** | **NO** | clean (audited by hand, §U3) |

Per-file re-run confirms it: `scan_uninit_dealloc.py` on `Objects/bytesobject.c` alone reports
`allocation_sites: 0` for a file that contains
`it = PyObject_GC_New(striterobject, &PyBytesIter_Type);` at `:3555`.

**So: the denominator is 5 where it should be 6, and the miss is a correct negative.** No bug was
hidden *at that site*. But the 1,147 invisible lines contain `striter_dealloc` (`:3418`),
`striter_next` (`:3435` — Group A's reproduced FT double-DECREF), the whole 12-function
`PyBytesWriter_*` API including **U4**, `_PyBytes_Resize` itself, `PyBytes_Concat`, `_PyBytes_FromList`
/ `FromTuple` / `FromIterator`. Two of the five `_PyBytes_Resize` call sites in my §2 denominator
(`:3644`, `:3763`) live there. **Both of my non-CPY-0014 findings, U1 and U4, were found by reading, not
by the scanner** — U1 because its file is visible but the shape is not modelled, U4 because its file
is not visible at all.

I independently reproduce `refcount-auditor`'s §6.1 root cause and its proposal, and add one thing:
**the toolkit already ships the canary.** `tree_sitter_utils.parse_health()` (`:1155`) computes
`coverage`, `error_nodes` and `missing_nodes`, and only `find_parity_pairs.py` calls it. Wiring it into
every scanner's envelope — `denominators.parse_coverage`, plus a `notes[]` entry below a threshold —
is a one-line-per-scanner change that would have printed `bytesobject.c: 0.5667` next to
`allocation_sites: 5` and made this section unnecessary.

---

# 4. Task (d) — the allocation-failure index map

`repro/oom_sweep_sequences.py` — 70 scenarios covering every Python-reachable constructor and
resize/realloc path in the four files, driving the toolkit's `run_oom_sweep.py` with `--width 1`
(one allocation fails per iteration) and unarmed setup.

| build | scenarios | real allocation-failure points | scenarios with a crash | crash indices |
|---|---|---|---|---|
| `release-gil-nojit` | 70 | **903** | 1 | `bytearray_take_bytes_survive` → **n=6, 7** |
| `debug-ft-nojit` | 70 | **777** | 1 | `bytearray_take_bytes_survive` → **n=4** |

Targeted sweeps, `repro/bytearray_setslice_resize_fail_oob.py` (0-11, `--width 1`):

| scenario | `release-gil-nojit` | `debug-gil-nojit` | `release-gil-nojit-asan` | `release-ft-nojit` | `debug-ft-nojit` |
|---|---|---|---|---|---|
| `state` (lo=100, 4 B) | n=2,3 → corrupt (rc 0) | n=2,3 → **SIGABRT** | n=2 → corrupt (rc 0) | n=2 → corrupt (rc 0) | n=2 → **SIGABRT** |
| `state_del` (lo=100, 0 B) | n=1,2 → corrupt | — | — | — | — |
| `singleton` (lo=100, 40 B) | n=2,3 → **SIGSEGV** | n=2,3 → **SIGSEGV** | n=2 → **SIGSEGV** | n=2 → **SIGSEGV** | n=2 → **SIGSEGV** |
| `runtime` (lo=200k, 100 kB) | n=3,4 → **SIGSEGV** | n=3 → **SIGSEGV** | n=2 → **ASan g-b-o** | — | — |
| `lo_zero` (lo=0, 4 B) | n=2,3 → benign | n=2,3 → **SIGABRT** (:224 assert) | n=2 → benign | n=2 → benign | n=2 → **SIGABRT** |

`repro/list_new_uninit_dealloc_ft.py` (CPY-0014): **0 crashes / 25 indices** on `release-ft-nojit` and
`debug-ft-nojit`.

### Two methodological results from running this, both of which changed the answer

**(i) The payload shape decides what the sweep can find, and the default shape cannot find this class.**
`run_oom_sweep.py` classifies by the child's exit code: exit 1 = clean `MemoryError` = SAFE. For the
uninitialized-dealloc class that is right, because the crash happens inside the constructor's own
`Py_DECREF` before `MemoryError` reaches Python. For the *"the error return left the object describing
memory that is gone"* class it is exactly wrong: the operation raises correctly, the sweep scores
`memory_error`, and the object is only lethal when something **uses** it afterwards. Every one of my
resize scenarios is therefore written as
`try: <op> except MemoryError: _c = 1` followed by `len` / `bytes()` / `repr` / `append`. Group A
reached `bytearrayobject.c:1609` for the same reason.

**(ii) A "survive" payload silently destroys the sweep's own denominator.** Swallowing `MemoryError`
makes the child exit 0, so `run_oom_sweep` counts `completed`, `allocation_failure_points` goes to
~1, and the verdict reads *"clean over N=1 — TOO THIN TO CERTIFY"* — a harness artifact that looks
exactly like a payload which never reaches the failure. Measured: `bytes_iconcat_survive` reported
**1** point before and **90** after. The fix is to re-raise `MemoryError` *after* the use
(`oom_sweep_sequences.py:run_one`), which restores the honest denominator and keeps crash detection.
Without it the 903-point figure above would have read as roughly 90.

**And the honest limit of the whole sweep: it did not find U1.** `bytearray_setslice_survive`
(`b[10:20] = b'y'*3000`) ran 15 clean allocation-failure points, because it is a slice **growth** and
U1 needs a **shrink with `lo != 0`** whose remainder is under half the allocation. A 70-scenario,
903-point clean sweep across the whole slice missed a reproducible OOB write into `_PyRuntime`.
U1 came from reading `bytearray_resize_lock_held`'s failure handler and then asking what its callers
assume. **The sweep confirmed; it did not discover.**

---

# 5. Toolkit assessment

### 5.1 `scan_uninit_dealloc` — precision 1/1, recall 1/1 against the class it models

The single finding is a true positive and a catalog recovery (CPY-0014) that the rule re-found
unaided, with the right member (`ob_item`), the right free line (`:262`), the right destructor
evidence (`kind: pointer` @ `:567`) and the right confidence (`medium`). The three-way
`unset_members` / `unset_members_all` / `destructor_evidence` split did its job: `allocated` appears in
`unset_members_all` and is correctly filtered out of `unset_members` because `list_dealloc` never reads
it. Four of the five sites it *could* see are clean and it stayed silent on all four — including
`PyByteArray_FromStringAndSize`, where the `ob_exports = 0` non-NULL sentinel initializer is exactly
the FP the v0.8 taxonomy warns about, and the scanner did not fire. **No false positives, no
re-litigation cost.**

### 5.2 The denominator defect — 5 should be 6, and the canary already exists

§3. **Proposal, in priority order:**

1. **Emit `parse_health()` in every scanner envelope.** `denominators.parse_coverage`,
   `denominators.parse_error_nodes`, and a `notes[]` entry when coverage drops below ~0.75:
   *"tree-sitter attributed 56.7% of Objects/bytesobject.c to functions (37 ERROR nodes); results for
   that file are partial."* The function is written, tested and unused outside
   `find_parity_pairs.py`. This is strictly cheaper than the `extract_functions` fix and it makes
   *every* under-count self-reporting rather than requiring an agent to discover it per slice.
2. **Fix `extract_functions` to skip top-level `ERROR` nodes and keep walking siblings**
   (`refcount-auditor` §6.1 proposal 1). Second instance of this family in the toolkit line after the
   `preproc_ifdef` gap (cext #64 / ft #24).
3. **Regression fixture:** `Objects/bytesobject.c:2787-2843` verbatim (the `bytes_methods[]` table
   interleaving brace initialisers with bare `BYTES_*_METHODDEF` macros), asserting
   `allocation_sites >= 1` for the file, i.e. that `bytes_iter:3555` is reached.

### 5.3 The missing rule — `resize_failure_leaves_stale_state`

U1 and U4 are the same shape and **no scanner in the toolkit models it**. It is a close cousin of
`scan_error_paths`'s §3.4 gap (`error-path-analyzer` proposed *"a call that writes NULL into an
out-parameter that is a member of `self` on failure, where the failure branch returns without repairing
that member"*), but the two are not the same rule and mine is the one that produced the write:

> A function calls a fallible **resize helper** `F(…)` that, on failure, leaves the receiver's fields
> describing memory it has already freed. The **caller's** failure branch then **writes a field of the
> receiver** (`Py_SET_SIZE`, `self->ob_start = …`, `self->ob_bytes = …`) or **reads a cached pointer or
> size derived from it**, instead of returning the sentinel immediately.

Three gates make it tractable, measured on this slice:

| gate | effect |
|---|---|
| **G1** `F` is in a per-file set of "destructive resize helpers" — derived, not hand-listed: any static function whose own body contains `_PyBytes_Resize(&X->member, …)` / `PyMem_Realloc` and whose failure branch **assigns** to that member. Catches `bytearray_resize_lock_held` (`:281-284`) and `_PyBytes_Resize` (every `*pv` store on a failure path: `bytesobject.c:3350, 3361, 3366, 3372, 3378, 3388-3395`) mechanically. | defines the trigger |
| **G2** the caller's failure branch contains an assignment to `self->…` or `Py_SET_SIZE(self, …)`, **or** a use of a local cached from `self` before the call | 20 `bytearray` call sites → **1** |
| **G3** suppress when the failure branch's only statement is `return`/`goto` to a label that only returns | removes the 19 |

Expected on this slice: `bytearrayobject.c:586` and `bytesobject.c:3641` — **2/2 precision, 2/2 recall**
against what I found by reading. `listobject.c:143-149` / `:180-186` and
`bytearrayobject.c:614-617` are ready-made negative fixtures; `bytearray_take_bytes_impl` on `main`
(`:1632-1638`) is a ready-made positive-then-fixed fixture.

The rule generalises past this slice: the same shape is what `Modules/_io/bytesio.c:110`,
`Modules/_elementtree.c:2832` and `Modules/_pickle.c:1103` all have to get right with
`_PyBytes_Resize(&self->member, …)`, and none of them is in any current rule's reach.

### 5.4 `run_oom_sweep.py` — two changes, both measured in §4

1. **Name the payload shape in the docstring and the envelope.** The module's methodology section is
   excellent on *arming* and *isolation* and silent on the fact that exit-code classification only sees
   crashes that happen **before** `MemoryError` propagates. Add the "survive" shape as a documented
   second mode (`--use-after-failure`), which wraps the payload in
   `try/except MemoryError` + a caller-supplied probe + a re-raise, and emit `payload_shape` in the
   envelope. Without this the harness structurally cannot see the class that produced this slice's two
   reproduced crashes (U1 and Group A's F5).
2. **`allocation_failure_points` must not be inferred from exit code 1 alone.** Have the child report
   consumption explicitly (a marker on stderr when `set_nomemory` fired), so a payload that legitimately
   handles the failure is not scored as "the payload allocates too little". Today the metric and the
   `THIN_EVIDENCE_POINTS` verdict are both wrong by ~90× for any well-behaved payload — measured, §4(ii).

### 5.5 What the informed method bought, and what it cost

Bought: the guarded-twin rule found U1's *fix* before I had a repro — `take_bytes` on `main` showed me
what the correct handler looks like, and the question "which caller does the opposite?" produced the
site in one grep. Group A's F7, recorded as CONSIDER with an explicitly unverified second-order note,
is what pointed me at `setslice_linear` at all; I verified their arm (debug-only SIGABRT) and found the
adjacent one they had not named (the OOB write). **Their honesty about what they had not measured is
what made the finding reachable** — the value of the "what I did not do" section, demonstrated.

Cost: nearly nothing here, because the FP taxonomy's two directly relevant entries
(*"non-NULL sentinel initializer"* → `ob_exports = 0`, *"a clean OOM sweep is not an exoneration"*)
were both exactly right and both saved time — the first stopped me re-opening
`PyByteArray_FromStringAndSize`, the second stopped me writing "CPY-0014 appears fixed" after 50 clean
FT indices.

---

# 6. Artifacts

- `reports/obj-sequences/repro/bytearray_setslice_resize_fail_oob.py` — **U1**; 5 scenarios × controls, 6 builds
- `reports/obj-sequences/repro/oom_sweep_sequences.py` — the 70-scenario allocation-failure sweep (§4)
- `reports/obj-sequences/repro/list_new_uninit_dealloc_ft.py` — CPY-0014 attempt (freelist drain + pool dirtying), 0/25
- `scratchpad/check_trunc.py` — the §3 `parse_health` measurement
- `scratchpad/oom_release_gil.json`, `scratchpad/oom_debug_ft.json` — full per-scenario sweep output

# 7. What I did not do

- **U4 is not reproduced.** I did not build a `_testcapi` driver that ignores a `PyBytesWriter_Grow`
  failure and then calls `Finish`. The mechanism is read from the source; the *reachability* claim is
  limited to third-party C API users, and I found no in-tree caller that ignores the failure.
- **I did not attempt to weaponise U1.** I demonstrated that `lo` and the written bytes are both
  Python-controlled and that the target is `_PyRuntime.static_objects.singletons`; I did not try to
  land a chosen value on a chosen field, and the write requires an allocation failure.
- **U1's FT indices are shallower than the GIL ones.** The 1 MB `runtime` scenario is slow enough on
  the FT builds that I ran it only on the GIL builds; the 4 KB `state` and `singleton` scenarios cover
  all six builds. The FT/GIL difference in *which* singleton faults first (`bytes([1])` vs `bytes([2])`)
  is an allocator-layout artifact, not a semantic one — I did not verify that claim.
- **No parity oracle**, per the brief: every verdict here rests on an exit code, an ASan frame, or an
  inspected return value.
- **CPY-0014's dirty-block trigger is unsolved.** I established *why* it is latent (freelist NULLs
  `ob_item`; `PyObject_GC_UnTrack` tolerates untracked) but did not construct the recycled block that
  makes it fire.

---

## Summary line

```
FIX=1  CONSIDER=1  ACCEPTABLE=3  CONFIRMED-KNOWN=1
scan_uninit_dealloc: 1 candidate / 1 confirmed (CPY-0014) / 0 dismissed -- precision 1/1, recall 1/1 of the class it models
denominator defect: allocation_sites 5, ground truth 6 (missing Objects/bytesobject.c:3555 bytes_iter, tree-sitter truncation)
allocation-failure sweep: 70 scenarios, 903 points (release-gil) / 777 (debug-ft), 1 known crash scenario
novel reproduced finding: 1 -- OOB WRITE into _PyRuntime, 6/6 builds, ASan global-buffer-overflow, LIVE ON MAIN, no prior art
```
