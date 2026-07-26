# null-safety-scanner — slice `obj-sequences` (Group B, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-verified independently for a fourth time — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files plus `Objects/clinic/{listobject,bytesobject,bytearrayobject}.c.h`, `Include/cpython/{listobject,bytesobject,bytearrayobject}.h` and `Include/bytearrayobject.h` is **empty**. Every line number below is valid on every matrix build without adjustment.

**Read first:** `AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/git-history-context.md`, `agents/refcount-auditor.md`, `agents/error-path-analyzer.md`, `agents/init-bypass-checker.md`, `agents/uninitialized-dealloc-auditor.md`, `agents/recursion-guard-auditor.md`, `preflight/informed_briefing.md`.

**Pre-run scanner:** `scan_null_checks.sample.json` — **0 findings**; `assignment_sites: 1309`, `fallible_sources_resolved: 314`, `local_nullable_helpers: 137`, `outparam_wrappers: 1`, `functions_analyzed: 370`.

---

## Headline

**The 0-of-1,309 is not a truncation artifact — and it is also not a 1,309-wide negative.** The tree-sitter
truncation that blinds nine scanners to 1,147 lines of `Objects/bytesobject.c` **does not touch this one**:
`scan_null_checks` has its own regex `find_functions`, which returns **107** functions for that file where
`extract_functions` returns 67, and **40 functions / 104 assignment sites past the `:2779` boundary are inside
the 1,309**. That is the first clean counter-example to the truncation story in this slice, and it is worth
recording because it tells the toolkit *which* fix is load-bearing.

But instrumenting the rule's own pipeline collapses the headline by **77×**:

```
1,309  `lvalue = ...` assignment sites
  112  ... whose RHS is a resolved fallible call          (8.6%)
    1  ... suppressed as an if/while controlling test
   46  ... with a modelled dereference in the 12-line window
   17  ... where the dereference is dominated             <- the rule's REAL denominator
   17  ... NULL-checked before the deref                  <- 17/17, an earned zero
    0  findings
```

**17, not 1,309.** 17-of-17 clean is a real negative and I list all 17 sites below — but a reader who quotes
1,309 is quoting the number of assignments in the files, not the number of obligations the rule discharged.

**Two novel findings, both reproduced, neither visible to any scanner in the toolkit.**

- **N1 — `PyBytesWriter_Format` uses `bytes_fromformat`'s result with no NULL check** (`bytesobject.c:3920 → :3923`).
  Textbook `unchecked_alloc`. Missed for exactly one reason: the use is **pointer arithmetic**
  (`buf - byteswriter_data(writer)`), which `_DEREF_TEMPLATE` does not model. Adding it surfaces the bug at
  **+3 findings tree-wide over `Objects/`**, of which 1 is this true positive.
- **N2 — a negative `Py_SIZE` defeats the slice's bounds checks on the READ side**, at three sinks CPY-0186
  never recorded. `b[0:-1]` on the corrupted object returns **3,896 bytes of `_PyRuntime` to Python** with rc=0
  on all four builds, and `b.split()` **SIGSEGVs on all four**. ASan: `global-buffer-overflow READ of size
  799,996` at `bytearray_subscript_lock_held:511`.

---

# 1. Findings

## [FIX] N1 — `PyBytesWriter_Format` dereferences an unchecked `bytes_fromformat` result (`Objects/bytesobject.c:3920 → :3923`) · **NOVEL, REPRODUCED**

```c
3910 int
3911 PyBytesWriter_Format(PyBytesWriter *writer, const char *format, ...)
3912 {
3913     Py_ssize_t pos = writer->size;
3914     if (PyBytesWriter_Grow(writer, strlen(format)) < 0) {
3915         return -1;
3916     }
3918     va_list vargs;
3919     va_start(vargs, format);
3920     char *buf = bytes_fromformat(writer, pos, format, vargs);   /* may be NULL */
3921     va_end(vargs);
3923     Py_ssize_t size = buf - byteswriter_data(writer);            /* UB on NULL */
3924     return PyBytesWriter_Resize(writer, size);
3925 }
```

**What.** `bytes_fromformat` (`bytesobject.c:196`, `static char *`) returns NULL on every one of its error
paths — an out-of-range `%c` argument (`:271-277`, sets `OverflowError`), an unsupported format character, and
any `PyBytesWriter_Grow` failure inside the `WRITE_BYTES_LEN` macro (`:219-222`, sets `MemoryError`).
`PyBytesWriter_Format` neither tests it nor propagates. It subtracts the writer's data pointer from NULL —
undefined behaviour, and in practice a large negative `Py_ssize_t` — and feeds that to `PyBytesWriter_Resize`,
whose `size < 0` guard (`:3830-3832`) raises `ValueError: size must be >= 0` **over** the exception
`bytes_fromformat` already set.

**Guarded twin — the same helper, the same file, 3,537 lines up.** `PyBytes_FromFormatV` (`:374-390`):

```c
383     char *s = bytes_fromformat(writer, 0, format, vargs);
384     if (s == NULL) {
385         PyBytesWriter_Discard(writer);
386         return NULL;
387     }
```

**Measured** (`repro/nullsafe_byteswriter_format_unchecked.py`), **4/4 builds identical** —
`release-gil-nojit`, `debug-gil-nojit`, `release-ft-nojit`, `debug-ft-nojit`, all rc=0:

| probe | result |
|---|---|
| `writer.format_i(b'x=%i', 123456)` (control) | `b'x=123456'` |
| `writer.format_i(b'x=%c', 300)` | **`ValueError: size must be >= 0`**, `__context__` is `None` |

The `OverflowError: %c argument not in range(256)` that `bytes_fromformat` raised is **destroyed, not chained**
— the same `_PyErr_SetObject` behaviour `error-path-analyzer` measured for F1/F2/F3. On the `MemoryError` path
the destroyed exception is a `MemoryError`.

**Reachability.** `PyBytesWriter_Format` is **public C API since 3.15** (`Include/cpython/bytesobject.h:71`).
The only in-tree caller is `Modules/_testcapi/bytes.c:179` (`writer.format_i`), which is what the reproducer
drives, so the *pure-Python* exposure today is test-support only — but the API is shipped and a third-party
caller reaches it directly. I have **not** attempted to make the negative `size` land on a value
`PyBytesWriter_Resize` accepts (it would need `buf` to be a small non-NULL value, which cannot happen), so the
observable is exception destruction plus null-pointer UB, not a crash.

**Fix.** Copy the twin verbatim:
```c
     char *buf = bytes_fromformat(writer, pos, format, vargs);
     va_end(vargs);
+    if (buf == NULL) {
+        return -1;
+    }
     Py_ssize_t size = buf - byteswriter_data(writer);
```

**Prior art: none.** `gh api -X GET search/issues` for `PyBytesWriter_Format`,
`PyBytesWriter NULL check bytes_fromformat`, `bytes_fromformat NULL` returns only the PEP 782 implementation
PRs (gh-129813, gh-138824, gh-121726) and gh-146353 (a *documentation* issue about `PyBytesWriter_GetData`
pointer invalidation — adjacent family, different defect). Nothing in the 178-record findings catalog
(`grep -rn PyBytesWriter_Format reports/` → 0).

**Cross-reference, not a duplicate.** `uninitialized-dealloc-auditor` named this site in one line as a
"neighbour, out of my class" at the end of their U4. It is squarely in mine, so I took it, reproduced it on
four builds, found the guarded twin, and measured the scanner gap that hid it (§3.3). Their U4 —
`byteswriter_resize` leaving `writer->size` describing a freed buffer — is a **different** defect in the same
API and remains theirs.

---

## [FIX] N2 — a negative `Py_SIZE(bytearray)` defeats the slice's READ-side bounds checks: OOB read disclosed to Python, plus a SIGSEGV · **NOVEL SINKS of the CPY-0186 root**

*CPY-0186 is confirmed present at the ref and not re-derived. What follows is the other half of that root
cause: the recorded finding is a **write** through `mp_ass_subscript`; these are **reads** through
`mp_subscript` and through `bytearray.split()`, and they were not recorded.*

### The unsafe shape the brief asked for

Both of the slice's read guards assume `Py_SIZE >= 0`:

```c
/* Objects/bytearrayobject.c:505-512  bytearray_subscript_lock_held (mp_subscript) */
505     slicelength = PySlice_AdjustIndices(PyByteArray_GET_SIZE(self),
506                                         &start, &stop, step);
508     if (slicelength <= 0)                                   /* <- assumes size >= 0 */
509         return PyByteArray_FromStringAndSize("", 0);
510     else if (step == 1) {
511         return PyByteArray_FromStringAndSize(
512             PyByteArray_AS_STRING(self) + start, slicelength);   /* start < 0 */
513     }
514     else {
515         char *source_buf = PyByteArray_AS_STRING(self);
524         for (cur = start, i = 0; i < slicelength; cur += step, i++) {
526                 result_buf[i] = source_buf[cur];                 /* cur wraps size_t */
527         }
```

With `length < 0`, `PySlice_AdjustIndices` (`Objects/sliceobject.c:257-296`) clamps a **non-negative** `start`
to `length` itself (`:271-273`, because `*start >= length` is trivially true) and a **negative** `stop` to `0`
(`:275-279`), so the `*start < *stop` test at `:291` holds with `start == length < 0` and the function returns
a **positive** `slicelength == -length`. `slicelength <= 0` therefore does not fire, and `:512` reads
`-length` bytes *before* `ob_start`. In the extended-slice branch `cur` is declared `size_t` (`:501`), so
`(size_t)start` wraps and `source_buf[cur]` lands at the same negative offset.

`bytearray_split_impl` has the same shape one level up:

```c
/* Objects/bytearrayobject.c:1806-1814 */
1806     const char *sbuf = PyByteArray_AS_STRING(self);
1807     Py_ssize_t slen  = PyByteArray_GET_SIZE((PyObject *)self);   /* negative */
1814         list = stringlib_split_whitespace((PyObject*)self, sbuf, slen, maxsplit);
```
`stringlib_split_whitespace` (`Objects/stringlib/split.h:66-83`) loops `while (i < str_len …)` and breaks on
`i == str_len`; with `str_len < 0` neither test ever holds, `i` grows without bound, and every iteration
`SPLIT_ADD(str, j, i)` copies a byte from progressively further past the end.

### Measured — `repro/nullsafe_negative_size_reads.py`, one probe per subprocess, controls on every build

**The two disclosure sinks (rc=0 everywhere — inspect the VALUE, per the brief's evidence note):**

| probe | control (no injection) | corrupted (`n=2`) |
|---|---|---|
| `b[0:-1]` | `len(103) b'xxxx…'` | **`len(3896)` `b'(fr\xc7;W\x00\x00\x0c\x00\x00\x00…'`** |
| `b[0:-1:2]` | `len(52) b'xxxx…'` | **`len(1948)` `b'(\xc0\xec\x00\x0c\x00\x00\x00\x88…'`** |

The bytes differ run to run (ASLR) — this is live `_PyRuntime` memory, not a constant. `rc=0` on
`release-gil-nojit`, `debug-gil-nojit`, `release-ft-nojit` and `debug-ft-nojit`: **4/4, no crash, no
sanitizer report on the 4 KB variant** — exactly the CPY-0180/CPY-0186 signature where a plain build
discloses instead of failing.

**Scaled to 1 MB (`big_read`), `release-gil-nojit-asan`:**
```
==193388==ERROR: AddressSanitizer: global-buffer-overflow
READ of size 799996 at 0x5f101c01948c thread T0
    #0 __asan_memcpy
    #1 PyByteArray_FromStringAndSize   Objects/bytearrayobject.c:187:9
    #2 bytearray_subscript_lock_held   Objects/bytearrayobject.c:511:20
    #3 bytearray_subscript             Objects/bytearrayobject.c:544:11
    #4 PyObject_GetItem                Objects/abstract.c:163:26
    #5 _PyEval_EvalFrameDefault        Python/generated_cases.c.h:1459:33
Address 0x5f101c01948c is a wild pointer inside of access range of size 0x0000000c34fc.
```
Different function, different slot and opposite direction from CPY-0186, whose ASan frame is
`bytearray_setslice_linear:631` ← `bytearray_ass_subscript_lock_held:824`.

**The crash sink, `b.split()` — SIGSEGV on 4/4 builds:**
```
==209684==ERROR: AddressSanitizer: global-buffer-overflow
READ of size 1 at 0x58cdf4e67a88
    #1 PyByteArray_FromStringAndSize   Objects/bytearrayobject.c:187:9
    #2 stringlib_split_whitespace      Objects/stringlib/split.h:82:9
    #3 bytearray_split_impl            Objects/bytearrayobject.c:1814:16
    #4 bytearray_split                 Objects/clinic/bytearrayobject.c.h:982:20
```

**Full read matrix, 52 probes × (control, corrupted), `release-gil-nojit`:** 2 disclose, 4 crash, 46 either
raise (`SystemError: Negative size passed to …`, `ValueError`, `IndexError`) or return a wrong-but-harmless
value (`repr` → `bytearray(b'')`, `isalpha` → `True`, `__sizeof__` → 64). Cross-build:

| probe | release-gil | debug-gil | release-ft | debug-ft | ASan |
|---|---|---|---|---|---|
| `b[0:-1]` | rc=0, 3,896 B disclosed | rc=0, disclosed | rc=0, disclosed | rc=0, disclosed | g-b-o READ (1 MB variant) |
| `b[0:-1:2]` | rc=0, 1,948 B disclosed | rc=0 | rc=0 | rc=0 | — |
| `b.split()` | **SIGSEGV** | **SIGSEGV** | **SIGSEGV** | **SIGSEGV** | g-b-o READ |
| `b.center(4)` | **SIGSEGV** | **SIGSEGV** | SIGSEGV (after returning) | **SIGABRT** | heap-b-o **WRITE** of 1,950 |
| `b.ljust(4)` | **SIGABRT** `free(): invalid pointer` | **SIGSEGV** | SIGSEGV | **`_Py_NegativeRefcount`** | `negative-size-param: (size=-3896)` |
| `b.zfill(4)` | **SIGSEGV** | **SIGSEGV** | SIGSEGV | SIGSEGV | heap-b-o **WRITE** of 3,900 |

*One-line out-of-slice note, per the scope rule:* `center`/`ljust`/`zfill` crash inside
`Objects/stringlib/transmogrify.h:105-129` `pad()`, which computes
`STRINGLIB_NEW(NULL, left + STRINGLIB_LEN(self) + right)` and then `memset`/`memcpy`s `left`, `LEN(self)` and
`right` bytes into it — with a negative `LEN(self)` the allocation is tiny and the writes are not, producing a
**heap-buffer-overflow WRITE**:
```
==232765==ERROR: AddressSanitizer: heap-buffer-overflow
WRITE of size 3900 at 0x73eea2835675 thread T0
    #1 pad                    Objects/stringlib/transmogrify.h:121:13
    #2 stringlib_zfill_impl   Objects/stringlib/transmogrify.h:235:9
    #4 bytearray_zfill        Objects/bytearrayobject.c:2460:11     <- the in-slice entry point
```
`transmogrify.h` is textually included by both `bytesobject.c` and `bytearrayobject.c`, so the *header* belongs
to whichever slice owns `Objects/stringlib/`; the **root and all three entry points are in mine**
(`bytearrayobject.c:2148` `bytearray_center`, `:2576` `bytearray_ljust`, `:2460` `bytearray_zfill` — plus the
`#include "stringlib/transmogrify.h"` at `:1244` that instantiates the header with
`STRINGLIB_MUTABLE 1`). On `debug-ft-nojit` the overflow lands on a
neighbouring object's refcount (`_Py_NegativeRefcount`), which is how far the corruption reaches.

### Reachability — bounded, and I checked it rather than assuming

**`Objects/bytearrayobject.c:603` is the only writer of a negative `Py_SIZE` in the file.** I enumerated all
five `Py_SET_SIZE` sites (`:50, 248, 603, 1095, 1592`) and all five `bytearray_reinit_from_bytes` call sites
(`:185, 285, 926, 972, 1617`); every one except `:603` receives or computes a provably non-negative size.
So these sinks are reachable **only** through CPY-0186's root, which needs an allocation failure. That is why
this is reported as *new sinks of a recorded root* rather than a standalone bug, and why **the fix is
CPY-0186's fix** (delete the stale bpo-19568 recovery).

**What it changes about CPY-0186.** The recorded record describes one out-of-bounds *write*. The blast radius
is larger: the same corrupted object is an **information-disclosure primitive** (`b[0:-1]` hands back
attacker-sized `_PyRuntime` bytes with a clean exit code) and a **four-way crash primitive**, and the
disclosure is invisible to ASan at the sizes that stay inside the 346 KB global — the same sanitizer blind
spot `uninitialized-dealloc-auditor` recorded for the small writes, now confirmed on the read side.

**Hardening worth considering independently of the root fix:** `bytearray_subscript_lock_held:508`'s
`slicelength <= 0` and `bytearray_split_impl:1807`'s `slen` are both written as if `Py_SIZE` could not be
negative. `list` answers the same question defensively — `list_resize:114` asserts
`self->ob_item != NULL || newsize == 0` and `list_ass_subscript_lock_held:3805` derives its new size from a
`slicelength` that `adjust_slice_indexes:3717` bounded against a freshly-read `Py_SIZE`. An
`assert(Py_SIZE(self) >= 0)` in `bytearray_subscript_lock_held` would have turned this into a debug abort.

**Prior art: none.** `gh api -X GET search/issues` for `bytearray negative size slice out of bounds`,
`bytearray Py_SIZE negative read`, `PySlice_AdjustIndices negative length`,
`bytearray subscript out-of-bounds read MemoryError` returns only gh-140128 (the commit that caused CPY-0186's
root), gh-72054 (2016, `PySlice_GetIndicesEx` misuse) and gh-150450 (`sqlite3.Blob` negative-step slices — a
different type). Nothing in the findings catalog.

---

## [CONFIRMED — known] CPY-0185 · `_PyBytes_Resize` is the only member of its `PyObject **` family with no `*pv == NULL` guard (`Objects/bytesobject.c:3348-3349`)

Confirmed present at the ref in one line, per informed-mode rule 1: `v = *pv; if (!PyBytes_Check(v) || newsize < 0)` — `PyBytes_Check` reads `Py_TYPE(v)`. **My contribution is the completed family table for the slice** (§2.1) and the in-slice guarded-twin spelling: `PyBytes_Concat:3276-3278` opens with `assert(pv != NULL); if (*pv == NULL) return;`, and its `error:` label at `:3308-3311` uses `Py_CLEAR(*pv)` — the NULL-safe form — where the shape's failure mode is `Py_DECREF(NULL)`.

## [CONFIRMED — known] CPY-0184 · `PyByteArray_AS_STRING` is a bare `ob_start` read

Confirmed, not re-derived. **My contribution is the caller census the brief asked for and the guarded twin nobody has cited.**

- **56 uses of `PyByteArray_AS_STRING` in the slice** (53 in `bytearrayobject.c`, 3 in `bytesobject.c`) and
  **49 more tree-wide** outside `bytearrayobject.c`. **Zero of the 105 test the result for NULL.** The single
  grep hit that looks like a check (`bytearrayobject.c:1022`) is `if (PyBuffer_ToContiguous(…))`, not a NULL test.
- **The guarded twin is `_PyBytesWriter_GetData` (`Include/internal/pycore_bytesobject.h:97-107`)** — a
  field-returning accessor in the *same* type family that keeps exactly the fallback `732224e1139` deleted from
  `PyByteArray_AS_STRING`:
  ```c
  static inline char* _PyBytesWriter_GetData(PyBytesWriter *writer) {
      if (writer->obj == NULL) { return writer->small_buffer; }
      else if (writer->use_bytearray) { return PyByteArray_AS_STRING(writer->obj); }
      else { return PyBytes_AS_STRING(writer->obj); }
  }
  ```
  Two accessors, one file family, one keeps the empty-state fallback and one lost it. That is the fix's shape
  in one quote.
- **In-slice consequence: none beyond the recorded family A.** Of the 11 unguarded `self->ob_start` reads in
  `bytearrayobject.c` (`:219, 288, 466, 497, 568, 598, 1591, 1597` + 3 writes), every one is either dominated
  by a `Py_SIZE`-derived bound that is 0 on a bypassed object (`:466, :497, :1591, :1597`), or on a path that
  reaches `bytearray_resize_lock_held:280` and crashes there first (`:568, :598`), or operates on a
  freshly-reinitialised non-NULL `ob_bytes` (`:288`). `:219` computes `ob_start - ob_bytes` = `NULL - NULL`.
  This corroborates `init-bypass-checker`'s measured 7-crash / 74-survive split from the static side.

## [POLICY] `PyByteArray_AsString` / `PyByteArray_Size` guard their input with `assert` only (`Objects/bytearrayobject.c:193-208`)

```c
Py_ssize_t PyByteArray_Size(PyObject *self)      { assert(self != NULL); assert(PyByteArray_Check(self)); return PyByteArray_GET_SIZE(self); }
char * PyByteArray_AsString(PyObject *self)      { assert(self != NULL); assert(PyByteArray_Check(self)); return PyByteArray_AS_STRING(self); }
```

`assert` is not a NULL check — it is the scanner's own `_ASSERT_CALL_DEREF` shape (CPY-0079's class): on a
release build `NDEBUG` removes it and `_PyByteArray_CAST(NULL)` dereferences `Py_TYPE(NULL)`. The **guarded
twin is in the sibling type**: `PyBytes_AsString` (`bytesobject.c:1341-1350`) tests `!PyBytes_Check(op)` and
returns NULL with a `TypeError`, and `PyBytes_AsStringAndSize` (`:1352-1381`) additionally tests
`if (s == NULL) { PyErr_BadInternalCall(); return -1; }`. Passing NULL to a C-API function is the documented
caller contract, so this is POLICY, not FIX — but the asymmetry between the two types is real and it is the
same asymmetry CPY-0184 exploits from the other end.

## [ACCEPTABLE] `bytes_methods.c`'s only `PyObject **` helper dereferences its out-param unguarded — correct by construction (`Objects/bytes_methods.c:404-406`)

`parse_args_finds_byte(const char *function_name, PyObject **subobj, char *byte)` does
`if (PyObject_CheckBuffer(*subobj))` with no `subobj`/`*subobj` NULL test, and reads `*subobj` five times.
Its only callers are `find_internal:461` and the file's `_Py_bytes_*` entry points, all of which pass `&subobj`
where `subobj` is a **required** clinic `O` argument (never NULL). Verified against both clinic headers.
The shared-file doubling rule the mapper established applies but costs nothing here.

---

# 2. Classes bounded (with denominators, and whether the truncation touched them)

## 2.0 Task (a) — the 1,309, verified

**The truncation does NOT apply to `scan_null_checks`.** It uses its own regex `find_functions`
(`scan_null_checks.py:212-261`), not `tree_sitter_utils.extract_functions`. Measured
(`scratchpad/nullscan_denominator.py`):

| file | lines | regex fns (this scanner) | tree-sitter fns (the other 9) | assignment sites | local nullable helpers | `parse_health` coverage |
|---|---|---|---|---|---|---|
| `listobject.c` | 4,313 | 132 | 132 | 542 | 44 | 0.8129 |
| **`bytesobject.c`** | 3,926 | **107** | **67** | **402** | 49 | **0.5667** |
| `bytearrayobject.c` | 3,126 | 105 | 106 | 283 | 35 | 0.7156 |
| `bytes_methods.c` | 739 | 26 | 26 | 82 | 9 | 0.7605 |
| **total** | | **370** | 331 | **1,309** | 137 distinct | |

370 and 1,309 reproduce the envelope exactly. **40 functions and 104 assignment sites in `bytesobject.c` past
the `:2779` tree-sitter boundary are inside the 1,309** — `bytes_mod`, `_PyBytes_From{Buffer,List,Tuple,Iterator}`,
`PyBytes_FromObject`, `PyBytes_Concat`, `PyBytes_ConcatAndDel`, `_PyBytes_Resize`, the six `striter_*`, and the
whole 20-function `PyBytesWriter_*` API. **N1 lives at `:3920`, 1,141 lines inside the region the other nine
scanners cannot see, and this scanner did read it** — it stayed silent for a different reason (§3.3).

So: the negative needs **no** hand-coverage of the tail. I read it anyway (§1 N1, §2.1, §2.2) because it is
where the slice's only novel NULL finding turned out to be.

## 2.1 Task (b) part 1 — every `T **` out-parameter helper in the slice: **12 helpers, 1 unguarded, and it is the recorded one**

Sweep: `scratchpad/sweep_outparams.py` over all 370 functions.

| helper | site | out-param | guards its input? |
|---|---|---|---|
| `list_capacity` | `listobject.c:50` | `PyObject **items` | n/a — never derefs `*items`; `_Py_CONTAINER_OF` arithmetic |
| `free_list_items` | `:58` | `PyObject** items` | n/a — passes to the allocator |
| `ptr_wise_atomic_memmove` | `:917` | `PyObject **dest/src` | n/a — internal, callers bound |
| `reverse_slice` | `:1620` | `PyObject **lo/hi` | n/a — sort-internal, `lo <= hi` by construction |
| `gallop_left` / `gallop_right` | `:2066`, `:2155` | `PyObject **a` | n/a — sort-internal |
| `formatfloat` | `bytesobject.c:443` | `PyObject **p_result` | writes only, never reads `*p_result` |
| `format_obj` | `:574` | `const char **pbuf` | writes only |
| `_PyBytes_DecodeEscape2` | `:1176` | `const char **first_invalid_escape_ptr` | writes only; initialises to NULL at `:1189` |
| `PyBytes_AsStringAndSize` | `:1352` | `char **s` | **yes** — `if (s == NULL) { PyErr_BadInternalCall(); return -1; }` `:1357-1360`, then `!PyBytes_Check(obj)` |
| `PyBytes_Concat` | `:3274` | `PyObject **pv` | **yes** — `assert(pv != NULL); if (*pv == NULL) return;` `:3276-3278` |
| `PyBytes_ConcatAndDel` | `:3323` | `PyObject **pv` | **yes** — delegates to `PyBytes_Concat` |
| **`_PyBytes_Resize`** | **`:3343`** | **`PyObject **pv`** | **NO** — `v = *pv; if (!PyBytes_Check(v) …)` `:3348-3349` = **CPY-0185** |
| `parse_args_finds_byte` | `bytes_methods.c:404` | `PyObject **subobj` | no — correct by construction, see §1 |

**1 unguarded of 12, and it is already recorded.** The family sweep the brief asked for produced no
un-found sibling *inside the slice*; `init-bypass-checker`'s cross-tree table (`_PyTuple_Resize`,
`PyUnicode_Append`) is the out-of-slice half and I did not re-derive it.

**The `decref_of_nulled_outparam` zero is earned, with a denominator of 7.** All seven in-slice
`_PyBytes_Resize(&x, …)` call sites read by hand:

| site | failure branch | verdict |
|---|---|---|
| `bytesobject.c:2377` | none — result returned directly | ACCEPTABLE (taxonomy: NULL propagation *is* the handling) |
| `bytesobject.c:3302` | `goto error;` → `PyBuffer_Release(&wb); Py_CLEAR(*pv);` | ACCEPTABLE — `Py_CLEAR(NULL)` is a documented no-op |
| `bytesobject.c:3644` | `return -1;` | ACCEPTABLE |
| `bytesobject.c:3763` | `goto error;` → `PyBytesWriter_Discard` (`Py_XDECREF`) | ACCEPTABLE |
| `bytearrayobject.c:280` | repairs the field before returning | ACCEPTABLE — the in-file guarded twin |
| `bytearrayobject.c:1609` | `Py_DECREF(remaining)` — a **different** object | ACCEPTABLE for this rule; the *state* defect is `error-path-analyzer` F5 |
| `bytesobject.c:3344` | (the definition) | — |

**No `Py_DECREF` of a NULLed out-parameter exists in the slice. 0 of 7, earned.**

## 2.2 Task (b) parts 2–3 — field-returning accessors and their callers

Sweep: `scratchpad/sweep_field_accessors.py` over the four `.c` files and the seven public/internal headers.

| accessor | returns | NULL-capable? | callers that check |
|---|---|---|---|
| **`PyByteArray_AS_STRING`** | `ob_start` | **yes** since `732224e1139` | **0 of 105** (56 in-slice, 49 tree-wide) |
| `PyByteArray_AsString` | forwards | **yes** | 1 of 1 in-tree consumer (`_testlimitedcapi`), and it has no way to report — CPY-0184 |
| `PyBytes_AS_STRING` | `ob_sval` | no — flexible array member | n/a |
| `PyBytes_AsString` | `ob_sval` after `PyBytes_Check` | yes (wrong type) | guarded twin |
| `PyList_GET_ITEM` / `_PyList_ITEMS` | `ob_item[i]` / `ob_item` | **yes when the list is empty** (documented invariant, `Include/cpython/listobject.h:12`) | all 97 `ob_item` reads bounded by `Py_SIZE`; see §2.3 |
| `PyList_GET_SIZE` / `PyByteArray_GET_SIZE` / `PyBytes_GET_SIZE` | `ob_size` | n/a | — |
| `_PyBytesWriter_GetData` | `obj`'s data **or `small_buffer`** | **no — has the fallback** | **the guarded twin** |
| `_PyBytesWriter_GetSize` | `writer->size` | n/a | can be stale — `uninit-dealloc` U4, theirs |
| `list_capacity` | `array->allocated` | n/a | — |

## 2.3 Task (c) — deref-like macros on a value a fallible call produced: **418 applications, 0 defects**

Sweep: `scratchpad/sweep_deref_macros.py`, over `Py_SIZE`, `Py_TYPE`, `Py_SET_SIZE`, `PyBytes_AS_STRING`,
`PyBytes_GET_SIZE`, `PyByteArray_AS_STRING`, `PyByteArray_GET_SIZE`, `PyList_GET_ITEM`, `PyList_GET_SIZE`,
`PyList_SET_ITEM`, `_PyList_ITEMS`, `PyTuple_GET_ITEM`, `PyTuple_GET_SIZE`, `PySequence_Fast_ITEMS`.

```
418  macro applications in the 4 files
 86  whose operand's most recent binding is from a call
 32  NULL-checked between the binding and the use
 54  not checked  -->  ALL 54 are `_PyBytes_CAST` / `_PyByteArray_CAST` /
                       `_striterobject_CAST` / `_bytesiterobject_CAST`, which are
                       infallible cast macros, not fallible calls
  0  defects
```

**Earned zero.** Separately, the specific unsafety the brief named — *"`i < Py_SIZE(x)` is not sufficient on
its own"* — **is present and is finding N2**. The three in-slice guards that assume a non-negative size are
`bytearray_subscript_lock_held:493` / `:508`, and `bytearray_split_impl:1807`. The 97 `ob_item` reads in
`listobject.c` are **not** in that class: I enumerated all 23 `Py_SET_SIZE` sites in `listobject.c` and the
only arithmetic one (`:3805`, `Py_SIZE(self) - slicelength`) takes its `slicelength` from
`adjust_slice_indexes:3717`, which bounds it against a freshly-read `Py_SIZE(lst)` with **no user Python
between** (`PyMem_Malloc`, `PyList_GET_ITEM`, `memmove`, `Py_SET_SIZE`, `list_resize`, *then* the
`Py_DECREF(garbage[i])` that can run `__del__`). `Py_SIZE(list)` cannot go negative; `Py_SIZE(bytearray)`
can, at exactly one site.

## 2.4 Task (d) — the clinic boundary: **67 wrappers, 7 nullable args, 0 defects**

Sweep: `scratchpad/sweep_clinic_null.py` over the three clinic headers.

| wrapper | nullable arg | impl | guarded? |
|---|---|---|---|
| `list___init__` | `iterable` | `list___init___impl` | yes (in the impl) |
| `bytes_translate` | `deletechars` | `bytes_translate_impl` | yes |
| `bytes_new` | `x` | `bytes_new_impl` | yes |
| `bytearray___init__` | `arg` | `bytearray___init___impl` | yes |
| `bytearray_translate` | `deletechars` | `bytearray_translate_impl` | yes |
| `bytes_hex` | `sep` | `bytes_hex_impl` | **in the callee** — `_Py_strhex_impl` `Python/pystrhex.c:123` `if (sep) {` |
| `bytearray_hex` | `sep` | `bytearray_hex_impl` | **in the callee**, same |

The last two are the taxonomy's "correct by construction / interprocedural" class, verified rather than
assumed. **All 14 `Py_buffer` converters in the two headers are mandatory** — none is inside a
`skip_optional` block — so no impl can receive an unfilled `Py_buffer`. Every converter failure routes to
`goto exit` with the `_impl` not entered.

This is the *static* half. `error-path-analyzer` §2.5 measured the dynamic half (41 critical-section regions,
0 lock leaks; 8 converters raising `KeyboardInterrupt` all delivered intact) and `init-bypass-checker` §1.I
found the one real defect at this boundary — `__init__` running inside a pre-lock `_PyNumber_Index`. Neither
is a NULL. **No NULL arrives from a converter in this slice.**

## 2.5 The 17 sites that actually exercised the rule

The honest denominator behind the zero. All 17 dominated dereferences of a fallible-call result, all
NULL-checked first:

`listobject.c` — `list_allocate_array:42` (`PyMem_Malloc`), `list_preallocate_exact:211`, `PyList_New:260`,
`list_new_prealloc:285`/`:291`, `list_contains:660` (`list_get_item_ref`), `list_concat_lock_held:781`,
`_PyList_FromStackRefStealOnSuccess:3298`, `list_index_impl:3340`, `list_slice_step_lock_held:3634`.
`bytesobject.c` — `PyBytes_FromString:185` (`PyObject_Malloc`), `_PyBytes_Repeat:1609`,
`bytes_translate_impl:2323`, `_PyBytes_FromIterator:3087` (`PyIter_Next`).
`bytearrayobject.c` — `_PyByteArray_FromBufferObject:142`, `PyByteArray_FromStringAndSize:164`
(`PyObject_New`), `bytearray_extend_impl:2209`.

## 2.6 Deliberately not re-litigated

CPY-0180 (`strip` UAF), CPY-0181, CPY-0182 (`striter_next` FT double-DECREF), CPY-0183, CPY-0184, CPY-0185,
CPY-0186, CPY-0014, gh-153570, gh-153578, gh-153419/#153498, gh-148268; `error-path-analyzer` F1–F11,
`init-bypass-checker` A–I, `uninitialized-dealloc-auditor` U1–U5, `recursion-guard-auditor`'s POLICY.
Confirmed present where I touched them, cited, not re-derived.

Two handoffs closed independently:
- **git-history-context §6 row 4 (`bytearrayobject.c:480`, addressed to this agent).**
  `bytearray_subscript_lock_held` is **correct by ordering**: `PyNumber_AsSsize_t(index, …)` at `:488` runs
  the user `__index__`, and `PyByteArray_GET_SIZE(self)` (`:491`), `Py_SIZE(self)` (`:493`) and
  `self->ob_start` (`:497`) are all read **after** it; the slice branch re-reads `PyByteArray_GET_SIZE` at
  `:505` after `PySlice_Unpack` at `:502` and `PyByteArray_AS_STRING` at `:512`/`:515` after that. No hoisted
  buffer pointer — it is **not** the gh-91153 round-2 shape. (This holds for a non-negative size; see N2.)
- **`bytearrayobject.c:1544` `take_bytes`** — `refcount-auditor` reproduced it as gh-153570; not re-derived.

---

# 3. Toolkit assessment

## 3.1 [HIGH VALUE] `assignment_sites` is a file statistic, not the rule's denominator — the funnel is 77× narrower

Measured (`scratchpad/nullscan_pipeline.py`), reproducing the scanner's own stages:

| stage | count |
|---|---|
| `lvalue = ...` assignment sites (`_EXTRA_TARGET_RE`) — **the reported denominator** | **1,309** |
| RHS is a resolved fallible call (`_alloc_re_for(...)`) | 112 |
| suppressed as an if/while controlling test | 1 |
| a modelled dereference exists in the 12-line window | 46 |
| the dereference is dominated (`_dominates` / `_join_after_full_ifelse`) | **17** |
| NULL-checked first — silently correct | 17 |
| reported | 0 |

`assignment_sites` counts **every** `x = …` in the files, including `i = 0`, `p++ = c` and struct-field stores
— 1,197 of the 1,309 never had a fallible source on the right-hand side. The number a reviewer needs in order
to certify the zero is **17**, and the two gates that discard the other 95 candidates (`_DEREF_TEMPLATE` 112→46,
`_dominates` 46→17) are exactly where a recall gap would hide — as §3.3 shows, one did.

**Proposal.** Emit the funnel, not the top of it:
```json
"denominators": {
  "assignment_sites": 1309,
  "assignments_from_fallible_source": 112,
  "candidates_with_modelled_deref": 46,
  "candidates_dominated": 17,
  "candidates_checked": 17,
  "findings": 0
}
```
Five extra integers, all already computed inside `analyze_function_null_safety`; only the counters need
hoisting. This is the same discipline `RUN_CONTEXT.md` demands of agents, applied to the scanner's own
reporting — and it turns "0 of 1,309" into "0 of 17, and here is why the other 1,292 were dropped".

## 3.2 [MEDIUM] Two set-cardinality fields are summed across files by the merge policy, inflating them 4×

`_sample.merge_policy` sums every numeric field. That is right for counts and wrong for the cardinality of a
**set**:

| field | reported | distinct | why |
|---|---|---|---|
| `fallible_sources_resolved` | **314** | **179** | `len(ALLOC_APIS \| PYOBJ_APIS \| local_helpers)` = 45 built-ins + this file's helpers, summed over 4 files. 180 of the 314 are the same 45 names counted four times (minus 3 for `PyBytes_FromStringAndSize`, `PyBytes_FromString`, `PyList_New`, which the files also *define*). |
| `decref_of_nulled_outparam_call_sites` | **17** | **5** | `len(wrappers) + len(NULLING_OUTPARAM_APIS)` = (1 + 4) for `bytesobject.c` and (0 + 4) × 3 for the rest. |

The brief reads 314 as "a dense denominator"; the honest figure is 179 distinct names, of which **137 are the
locally-discovered helpers** — which is the genuinely impressive half and is currently buried.

**Proposal.** Tag set-valued fields in `merge_policy` as `"union"` rather than `"summed"`, and emit the
underlying name lists (`fallible_source_names`, `outparam_api_names`) so the merge can do it. `outparam_wrappers`
already does exactly this and merges as `"concatenated"` — correctly. Also: for
`decref_of_nulled_outparam_call_sites`, the useful number is not the size of the API set at all but **the
number of `API(&x, …)` call sites found**, which is 7 in this slice and 0 in most files. That is the figure
that says whether the rule had anything to check. The field's own standing note ("this rule has a denominator
of literally zero on CPython") is contradicted by this slice, where it is 7 — worth updating.

## 3.3 [HIGH VALUE] `_DEREF_TEMPLATE` does not model pointer arithmetic — and that is the only reason N1 was missed

`bytes_fromformat` **is** resolved as a fallible source (verified: `"bytes_fromformat" in nullable_source_calls(...)`
→ `True`, one of the 137). The assignment at `:3920` is found, the window is correct, the check is absent. The
rule then discards the candidate at `if (deref is None): continue` (`scan_null_checks.py:731-734`), because the
use is `buf - byteswriter_data(writer)` and `_DEREF_TEMPLATE` models only `->`, `[`, the `Py_*` macros and
`Py_INCREF`/`Py_DECREF`.

**Measured cost of the widening** (`scratchpad/check_ptr_arith_gap.py`, `scratchpad/treewide_ptr.py`), adding
```python
r"{var}\s*(?:\+|-)\s*[A-Za-z_(]|[A-Za-z_)\]]\s*(?:\+|-)\s*{var}\b"
```

| corpus | current | widened | delta |
|---|---|---|---|
| the 4 slice files | 0 | **1** | +1, and it is N1 |
| all of `Objects/` (113 files) | 3 | 6 | +3 |

The three new tree-wide rows:
- `bytesobject.c:3920 PyBytesWriter_Format` — **true positive** (N1).
- `unicodeobject.c:2321 _PyUnicode_FindMaxChar` and `:10994 replace` — **false positives of a class already in
  the baseline** (`unicodeobject.c:12922 _PyUnicode_Repeat` is the same shape and is 1 of the current 3).

So the widening in isolation is 1 TP / 3, but the 2 new FPs belong to a pre-existing FP class that §3.4
suppresses mechanically; with that suppressor the widening is **1/1**.

The shape generalises well beyond this slice: `p = alloc(); n = p - base;` and `p = alloc(); use(p + off);` are
how every writer/buffer API in CPython consumes a pointer, and none of them is currently a "use".

## 3.4 [MEDIUM] New FP class — a public API function that shadows an infallible macro of the same name

`PyUnicode_DATA` accounts for **3 of the 6** findings in the widened tree-wide run (1 baseline + 2 new).
`Objects/unicodeobject.c:15386-15395` really is a fallible function:
```c
#undef PyUnicode_DATA
void* PyUnicode_DATA(PyObject *op)
{
    if (!PyUnicode_Check(op)) { PyErr_Format(...); return NULL; }
    return _PyUnicode_DATA(op);
}
```
— but the `#undef` immediately above it is the tell: **inside CPython the call sites expand the infallible
macro**, not this function. `nullable_source_calls` resolves the function by name and mis-attributes every
in-file macro use to it.

**Proposal.** In `nullable_source_calls`, drop any function whose definition is immediately preceded by
`#undef <same name>`. Mechanical, exact, and cheap: `grep -c "^#undef Py" Objects/*.c` is **23** tree-wide
(`PyCFunction_New`, `PyObject_Length`, `PySequence_Length`, `Py_NewRef`, `Py_TYPE`, `Py_SIZE`,
`PySlice_GetIndicesEx`, …). Every one is a compatibility shim for a macro that never fails in-tree.
Regression fixture: `Objects/unicodeobject.c:15386-15395` plus a call site.

## 3.5 [INFORMATIONAL] The truncation counter-example — record which fix is load-bearing

`refcount-auditor` §6.1, `init-bypass-checker` §3.3 and `uninitialized-dealloc-auditor` §3 all report the
`extract_functions` truncation of `Objects/bytesobject.c` (67 of ~104 functions, coverage 0.5667). I second
the diagnosis and add the **negative** control: `scan_null_checks` and `scan_refcounts` both ship their own
regex `find_functions` and are **unaffected** — 107 functions, 402 assignment sites, the whole
`PyBytesWriter_*` API inside the denominator.

That matters for prioritising the fix. Of the thirteen scanners, the nine that share `extract_functions` are
the ones losing 1,147 lines; the two with private function finders are not. So:
- **`parse_health()` in every envelope** (the uninit-dealloc agent's proposal 1) is the right first move,
  because it is per-file and per-scanner and would print `0.5667` next to a zero.
- But it must not be read as an excuse to leave `extract_functions` broken, *and* it must not be attached to
  the two scanners that do not use tree-sitter at all — for them the honest canary is a comparison of
  `find_functions` against a column-0 ground truth (370 of 371 here, per `error-path-analyzer` §3.1).

The two counters therefore mean different things and should be named differently:
`denominators.parse_coverage` (tree-sitter-backed scanners) vs `denominators.function_discovery_ratio`
(regex-backed scanners).

## 3.6 [MEDIUM] No rule models "a bounds check that assumes a non-negative size"

N2's three sinks share a shape no scanner in the toolkit expresses:

> a comparison of an index or length against `Py_SIZE(x)` / `PyXxx_GET_SIZE(x)`, or a
> `slicelength <= 0` / `n <= 0` early return, used as the **only** guard before pointer arithmetic on
> `PyXxx_AS_STRING(x)` or `x->ob_item` — in a file that also contains a `Py_SET_SIZE(x, <expression>)` whose
> argument is not provably non-negative.

The second clause is what makes it tractable: it is a *file-level* precondition, and it fires on exactly one
of the four slice files. Measured gate cost on this slice:

| gate | effect |
|---|---|
| G1 file contains `Py_SET_SIZE(x, A ± B)` with a non-literal operand | `listobject.c` (1 site, `:3805`), `bytearrayobject.c` (1 site, `:603`) |
| G2 that site is not dominated by a re-read of the same size | `listobject.c:3805` **out** (`adjust_slice_indexes:3717` bounds it); `bytearrayobject.c:603` **in** |
| G3 report the reads in the same file guarded only by a `Py_SIZE`-derived bound | 3 sites: `bytearray_subscript_lock_held:493`, `:508`, `bytearray_split_impl:1807` |

Expected on this slice: **3 candidates, 3 confirmed** (2 reproduced as disclosure, 1 as SIGSEGV), 0 FPs.
This is a *different* rule from `uninitialized-dealloc-auditor`'s proposed `resize_failure_leaves_stale_state`
(which finds the **producer** at `:603`) and from `error-path-analyzer`'s §3.4 (which finds the
out-parameter). Mine finds the **consumers** that make the producer dangerous, and it is what turns one
recorded OOB write into six sinks. If only one of the three is implemented, implement theirs — it removes the
root. Implement mine second, because it is what tells a reviewer how bad the root is.

`Objects/listobject.c:114` (`assert(self->ob_item != NULL || newsize == 0)`) and `:3717` are ready-made
negative fixtures; `Objects/bytearrayobject.c:508` is the positive one.

## 3.7 Precision of each rule that fired

Nothing fired. Per-rule accounting against a hand denominator:

| rule | denominator (measured, not reported) | findings | verdict |
|---|---|---|---|
| `unchecked_alloc` | **17** dominated derefs of a fallible result | 0 | **earned** for the 17; **1 miss** (N1) outside the deref model — §3.3 |
| `deref_before_check` | same 17 | 0 | earned; consistent with the tree-wide `deref_before_check: 0` the agent contract records |
| `decref_of_nulled_outparam` | **7** in-slice `_PyBytes_Resize(&x, …)` call sites, all read by hand | 0 | **earned**, and the "denominator is literally zero on CPython" standing note is wrong here |
| the `assert(MACRO(x))` sub-rule | `assert_only_derefs: 0`; 4 assert-only guards found by hand (`PyByteArray_AsString`/`Size`, ×2 asserts) | 0 | **miss of a lesser kind** — the asserts are on a *parameter*, and the rule only tracks values bound from a call in the same function. POLICY-grade; not worth widening. |

## 3.8 What the informed method bought, and what it cost

**Bought, twice.**
- The guarded-twin rule found N1's *fix* before I had a reproducer: I was enumerating the `T **` family for
  §2.1, saw `PyBytes_FromFormatV` check `bytes_fromformat`'s result, asked "who else calls it", and
  `PyBytesWriter_Format` was the only other caller in the tree.
- `uninitialized-dealloc-auditor`'s explicit *"what I did not do"* on U1 — that a small OOB write inside
  `_PyRuntime` is invisible to ASan — is what made me write N2's probe to inspect **returned values** rather
  than trust an exit code. The 4 KB disclosure has `rc=0` and no sanitizer report on every build; an
  exit-code-only harness scores it clean.

**Cost: one wrong hypothesis, recorded.** I predicted that `732224e1139` deleting
`PyByteArray_AS_STRING`'s empty-string fallback would also have broken the NUL-termination contract that
`_Py_SourceAsString` (`Python/pythonrun.c:1652-1672`) relies on via `strlen(str)`. **It did not.**
`bytearray_resize_lock_held` maintains the mid-buffer NUL on both arms (`:250`
`PyByteArray_AS_STRING(self)[size] = '\0'` on the minor-downsize quick exit; `:288`
`obj->ob_bytes[size] = '\0'` when `alloc != size`), and when `alloc == size` the backing `PyBytes`'
own `ob_sval[alloc] == 0` supplies it — which is what the code's own comment *"end provided by bytes"* means.
Measured: `compile(bytearray(...), …)` behaves identically on a fresh and a slice-mutated bytearray on both
GIL builds. Recorded so nobody spends the time again.

---

# 4. Artifacts

Reproducers (`reports/obj-sequences/repro/`, `.py` files, one probe per subprocess):
- `nullsafe_byteswriter_format_unchecked.py` — **N1**; control + the `%c` overflow, 4 builds
- `nullsafe_negative_size_reads.py` — **N2**; 4 scenarios (`slice_read`, `ext_slice`, `big_read`,
  `read_matrix` = 52 probes × control), dense `set_nomemory` sweep 0–7, controls on every build

Scratch (`/tmp/.../scratchpad/`): `nullscan_denominator.py` (§2.0 table),
`nullscan_pipeline.py` (§3.1 funnel), `sweep_outparams.py` (§2.1),
`sweep_field_accessors.py` (§2.2), `sweep_deref_macros.py` (§2.3),
`sweep_nullable_fields.py` (the 149-read field census behind §2.2/§2.3),
`sweep_clinic_null.py` (§2.4), `check_ptr_arith_gap.py` + `treewide_ptr.py` (§3.3/§3.4),
`negsize_one.py` + `ljust.txt` (the ASan frames).

# 5. What I did not do

- **No TSan work.** This class is single-threaded; the FT builds were used only to confirm N2's crash set is
  build-independent (6 probes × 2 FT builds under `PYTHON_GIL=0`).
- **N1 is not turned into a crash.** I established exception destruction and null-pointer UB on 4 builds; I did
  not build a UBSan Python to capture the `buf - byteswriter_data(writer)` diagnostic, and I could not
  construct a value of `size` that `PyBytesWriter_Resize` accepts.
- **N2's `pad()` half is out of slice.** `Objects/stringlib/transmogrify.h:121` gets one line and an ASan
  frame; I did not audit the rest of `stringlib`, which belongs to another slice's pass.
- **I did not weaponise the N2 disclosure.** I established that the size is caller-chosen and the source is
  `_PyRuntime.static_objects.singletons`; I did not try to steer it onto a chosen field.
- **The 49 tree-wide `PyByteArray_AS_STRING` consumers outside `bytearrayobject.c` are a count, not a review.**
  `init-bypass-checker` read the six that segfault (CPY-0184); the rest are leads.
- **No parity oracle**, per the brief — every verdict here rests on an exit code, an ASan frame, an inspected
  return value, or an N-of-M count.
- **I did not file anything upstream.** N1 and N2 have no issue I could find; filing is the campaign's call.

---

## Summary line

```
FIX=2  CONSIDER=0  POLICY=1  ACCEPTABLE=1  CONFIRMED-KNOWN=2
scan_null_checks: 0 findings. Reported denominator 1,309 assignment sites; the rule's REAL
    denominator is 17 (1309 -> 112 fallible -> 46 with a modelled deref -> 17 dominated -> 17 checked).
    17/17 EARNED. Truncation: DOES NOT APPLY -- this scanner's own regex find_functions returns
    107 functions for bytesobject.c where tree-sitter returns 67; 40 fns / 104 assignment sites
    past line 2779 ARE inside the 1,309, including the site of finding N1.
denominator defects: fallible_sources_resolved 314 is a per-file SUM (distinct 179);
    decref_of_nulled_outparam_call_sites 17 is a per-file SUM (distinct 5; real in-slice call sites 7).
classes bounded: 12 `T**` out-param helpers (1 unguarded = CPY-0185); 105 PyByteArray_AS_STRING
    consumers, 0 NULL-checking; 418 deref-macro applications, 0 on a fallible result;
    67 clinic wrappers / 7 nullable args / 14 mandatory Py_buffer converters, 0 defects;
    7 _PyBytes_Resize call sites, 0 Py_DECREF of a NULLed out-param.
novel reproduced findings: 2
    N1 bytesobject.c:3920 PyBytesWriter_Format -- unchecked bytes_fromformat, 4/4 builds,
       guarded twin PyBytes_FromFormatV:384, no prior art
    N2 bytearrayobject.c:508/511/524 + :1807/1814 -- negative Py_SIZE defeats the read guards:
       3,896 B of _PyRuntime disclosed to Python (rc=0, 4/4 builds), SIGSEGV on b.split() (4/4),
       ASan global-buffer-overflow READ of 799,996 at bytearray_subscript_lock_held:511.
       NEW SINKS of CPY-0186's root (which recorded only the mp_ass_subscript WRITE); no prior art.
toolkit: pointer arithmetic missing from _DEREF_TEMPLATE (+3 tree-wide over Objects/, 1 TP = N1);
    #undef-shadowed API functions are a new FP class (23 sites in Objects/); propose the funnel
    denominator, union-merge for set fields, and a `negative-size-bounds-check` rule (3/3 on this slice).
```
