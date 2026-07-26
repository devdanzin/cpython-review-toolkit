# error-path-analyzer — slice `obj-sequences` (Group A, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-verified independently of the mapper — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files plus `Objects/clinic/{listobject,bytearrayobject,bytesobject}.c.h` is **empty**. Every line number below is valid on every matrix build without adjustment.

**Read first:** `preflight/RUN_CONTEXT.md`, `preflight/AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/git-history-context.md`, `preflight/informed_briefing.md`.

**Reproducers** (all `.py` files, all in `reports/obj-sequences/repro/`):
`errpath_pyerr_clear_swallow.py`, `errpath_concat_exception_clobber.py`, `errpath_init_canresize_bypass.py`, `errpath_ob_exports_reset.py`, `errpath_take_bytes_resize_fail.py`, `errpath_clinic_boundary.py`, `errpath_release_buffer_exception_state.py`, `errpath_resize_oom_dataloss.py`, `errpath_sort_assert_richcompare.py`, plus the static enumerator `errpath_sentinel_enumerate.py` (§2.2).

---

## Headline

Two of the findings below are **memory-safety bugs reproduced with ASan on the default GIL build from pure Python**, and neither was known:

- **F4** — `bytearray.__new__(bytearray)` + `memoryview` + `__init__()` drives `ob_exports` to **-1**, permanently disabling `_canresize`; a later resize with a live memoryview is a **heap-use-after-free READ** (`errpath_ob_exports_reset.py d2`).
- **F5** — `bytearray.take_bytes()` on an allocation failure leaves the bytearray pointing at the buffer `_PyBytes_Resize` just freed: `bytes(b)` is a **heap-use-after-free READ of size 4096**, `b.append(1)` is a **SIGSEGV** (`errpath_take_bytes_resize_fail.py`).

Both are cured by **PR #153498 (gh-153419), merged 2026-07-24 — nine days *after* the review ref** — so both are live at `4f3be1b5777` and neither appears in gh-153419's issue text. Their fix is incidental; see the F3 note on why upstream's own new regression test does not catch the class.

---

# 1. Findings

## [FIX] F1 — `bytearray_richcompare` swallows whatever a user `__buffer__` raised (bytearrayobject.c:1171, :1177)

```c
1170    if (PyObject_GetBuffer(self, &self_bytes, PyBUF_SIMPLE) != 0) {
1171        PyErr_Clear();
1172        Py_RETURN_NOTIMPLEMENTED;
1173    }
...
1176    if (PyObject_GetBuffer(other, &other_bytes, PyBUF_SIMPLE) != 0) {
1177        PyErr_Clear();
1178        PyBuffer_Release(&self_bytes);
1179        Py_RETURN_NOTIMPLEMENTED;
1180    }
```

**What.** `other` is any object passing `PyObject_CheckBuffer` — which every Python class defining PEP 688 `__buffer__` does — so `PyObject_GetBuffer` at :1176 runs arbitrary Python and can raise anything. `self` reaches the same call at :1170 when it is a `bytearray` **subclass** overriding `__buffer__`. The clear is unnarrowed; it discards the exception and returns `NotImplemented`, and the comparison then completes normally through the reflected operand.

**Why this is not the FP class.** The taxonomy's "clear after a sentinel-returning lookup is idiomatic" entry does not apply: this is the *refinement* case — "an unfiltered clear after a call that runs arbitrary Python is NOT in the idiomatic class". The author's intent is plainly to swallow the `TypeError` from a non-buffer operand; a `KeyboardInterrupt` is not that.

**Measured** (`errpath_pyerr_clear_swallow.py e1 e2`), 4/4 builds — `release-gil-nojit`, `debug-gil-nojit`, `release-ft-nojit`, `debug-ft-nojit`, all rc=0:

| probe | result |
|---|---|
| `bytearray(b"abc") == RaisingExporter()` | **`False`** — `KeyboardInterrupt` gone entirely |
| `bytearray(b"abc") < RaisingExporter()` | `TypeError: '<' not supported…` — replaced |
| `BA(b"abc") == b"abc"` (subclass `__buffer__`) | **`False`** — same, via :1171 |

`exc.__context__` is `None`: `_PyErr_SetObject` chains from the *handled* exception, not the pending one, so the original is destroyed outright, not buried.

**Guarded twin.** Four of the seven `PyErr_Clear()` sites in the slice narrow first — `listobject.c:1321-1322` and `bytearrayobject.c:1080-1082` (`StopIteration`), `bytesobject.c:2938-2940` and `bytearrayobject.c:996-998` (`!ExceptionMatches(TypeError) → return`). The house rule is established; these two and F2 are the outliers.

**Fix.** `if (!PyErr_ExceptionMatches(PyExc_TypeError)) { return NULL; } PyErr_Clear();` at both sites.

**Prior art:** none. `gh api -X GET search/issues -f q='repo:python/cpython bytearray richcompare PyErr_Clear'` → 0 results.

---

## [FIX] F2 — `_Py_bytes_contains` swallows whatever a user `__index__` raised — doubled across `bytes` and `bytearray` (bytes_methods.c:608)

```c
602 _Py_bytes_contains(const char *str, Py_ssize_t len, PyObject *arg)
603 {
604     Py_ssize_t ival = PyNumber_AsSsize_t(arg, NULL);
605     if (ival == -1 && PyErr_Occurred()) {
...
608         PyErr_Clear();
609         if (PyObject_GetBuffer(arg, &varg, PyBUF_SIMPLE) != 0)
610             return -1;
```

**What.** `PyNumber_AsSsize_t` dispatches the operand's `__index__`. The clear is unnarrowed, so a `KeyboardInterrupt`/`MemoryError` from `__index__` is discarded and the function silently retries the buffer protocol. If the object also exports a buffer, `x in b` returns a **normal bool** with the exception gone. If it does not, the exception is *replaced* by the `TypeError` from `PyObject_GetBuffer`.

Per the mapper's §B.2 M-note, `bytes_methods.c` is shared: this single site backs **`bytes.__contains__` and `bytearray.__contains__` at once** (`bytesobject.c:1625`, `bytearrayobject.c:1382`).

**Measured** (`errpath_pyerr_clear_swallow.py e3 e3_bytearray e3_wrongtype`), 4/4 builds:

| probe | result |
|---|---|
| `EvilIndexPlusBuffer() in b"hello"` | **`True`** — `KeyboardInterrupt` gone |
| `EvilIndexPlusBuffer() in bytearray(b"hello")` | **`True`** — same |
| `EvilIndexOnly() in b"hello"` | `TypeError` — replaced |

**Guarded twin — in the same file, on the same conversion of the same argument.** `parse_args_finds_byte` (`bytes_methods.c:403-430`) does the identical `PyNumber_AsSsize_t(*subobj, NULL)` at :418 and **propagates** at :419-421 instead of clearing. It is the converter behind `find` / `index` / `rfind` / `rindex` / `count` on both types. Measured (`errpath_pyerr_clear_swallow.py twin`), 5 entry points × 4 builds = **20/20 `PROPAGATED_KeyboardInterrupt`**.

Note when reading `parse_args_finds_byte`: it checks `PyObject_CheckBuffer` **first** (:406-408), so an object that exports a buffer never reaches `__index__` at all. The twin probe must use an object with a raising `__index__` and *no* buffer, or it silently proves nothing.

**Fix.** Reorder to match `parse_args_finds_byte` (buffer-check first, then `_PyIndex_Check`, then convert-and-propagate), or minimally narrow: `if (!PyErr_ExceptionMatches(PyExc_TypeError)) return -1; PyErr_Clear();`.

**Prior art:** none.

---

## [FIX] F3 — Unnarrowed `PyErr_Format(TypeError, "can't concat …")` destroys the user's exception — 5 sites, 2 guarded twins

`PyObject_GetBuffer(user_object, …)` runs PEP 688 `__buffer__` and can raise anything. Five sites in the slice overwrite that exception with a fixed `TypeError`:

| site | function | Python entry point | measured |
|---|---|---|---|
| `bytesobject.c:1551` | `_PyBytes_Concat` | `b"a" + x` | `TypeError` (was `KeyboardInterrupt`) |
| `bytesobject.c:3291` | `PyBytes_Concat` | C API; reached from `Modules/_io/{textio.c:2032,bufferedio.c:1729}` | static only |
| `bytearrayobject.c:314` | `PyByteArray_Concat` (`sq_concat`, `:2757`) | `bytearray(b"a") + x` | `TypeError` |
| `bytearrayobject.c:357` | `bytearray_iconcat_lock_held` | `bytearray(b"a") += x` | `TypeError` |
| `bytearrayobject.c:664` | `bytearray_setslice` | `bytearray(b"a").extend(x)` (via `:2186`) | `TypeError` |

**Guarded twins, both in the slice:**
- `bytearrayobject.c:1062` `bytearray___init___impl` — `if (PyErr_ExceptionMatches(PyExc_TypeError)) PyErr_Format(...)`; measured `bytearray(Raiser())` → **`KeyboardInterrupt`**.
- `bytes_methods.c:697` `_Py_bytes_tailmatch` — same guard; measured `b"abc".startswith(Raiser())` and `bytearray(b"abc").endswith(Raiser())` → **`KeyboardInterrupt`**.
- (`bytearrayobject.c:2194` `bytearray_extend_impl`'s iterator arm is a third guarded instance, on `PyObject_GetIter`.)

**Measured** (`errpath_concat_exception_clobber.py`), `release-gil-nojit` + `debug-gil-nojit`, 16 entry points: 4 clobber, 12 propagate (including 9 non-twin controls — `strip`, `split`, `replace`, `removeprefix`, `translate` on both types, and `bytearray[1:3] = x`, which reaches `PyByteArray_FromObject` and therefore the *guarded* `__init__` path rather than `:664`).

**One-line out-of-slice note.** `Objects/stringlib/join.h:75` has the same unnarrowed reformat; measured `b"".join([b"a", Raiser()])` → `TypeError`. Because `stringlib` is textually included by both `bytesobject.c` and `bytearrayobject.c`, it is a two-type defect. Belongs to another slice's pass; recording the location only.

**Fix.** Wrap each of the five in `if (PyErr_ExceptionMatches(PyExc_TypeError)) { … }`, exactly as `bytearray___init___impl:1062` does eight hundred lines away in the same file.

---

## [FIX] F4 — `bytearray___init___impl:927` resets `ob_exports = 0` on an object whose buffer is already exported → counter goes negative → `_canresize` permanently defeated → heap-use-after-free

```c
923     /* First __init__; set ob_bytes_object so ob_bytes is always non-null. */
924     if (self->ob_bytes_object == NULL) {
925         self->ob_bytes_object = Py_GetConstant(Py_CONSTANT_EMPTY_BYTES);
926         bytearray_reinit_from_bytes(self, 0, 0);
927         self->ob_exports = 0;          /* <-- unconditional */
928     }
```

**Reachability.** `bytearray.__new__(bytearray)` yields a zeroed object with `ob_bytes_object == NULL` **and** `Py_SIZE == 0`. Because `PyByteArray_AS_STRING` returns the shared empty string when the size is 0, `bytearray_getbuffer_lock_held:55-70` succeeds on it and bumps `ob_exports` to 1. `__init__()` then takes the branch above and writes `ob_exports = 0` with the memoryview still live. Releasing the view runs `bytearray_releasebuffer:88` `obj->ob_exports--` → **-1**.

`_canresize:115` tests `ob_exports > 0`, so from then on it **never fires again for that object's whole lifetime**, and the `BufferError` that protects every reallocating path is gone.

**Measured** (`errpath_ob_exports_reset.py`):

| probe | build | result |
|---|---|---|
| `d1` (reach the negative counter) | `debug-gil-nojit` | **rc=134**, `Assertion 'obj->ob_exports >= 0' failed` at `bytearrayobject.c:89` |
| `d1` | `release-gil-nojit` | rc=0, counter silently at -1 |
| `d2` (resize under a live view) | `release-gil-nojit` | `b.extend(b"C"*(1<<20))` **succeeds** where it must raise `BufferError`; the live view then reads `b'dAAABBBB'` instead of `b'AAAABBBB'` |
| `d2` | `release-gil-nojit-asan` | **`AddressSanitizer: heap-use-after-free`, READ of size 8** |

ASan frames, verbatim:
```
READ of size 8 ...
    #1 PyBuffer_ToContiguous          Objects/memoryobject.c:1063
    #2 _PyBytes_FromBuffer            Objects/bytesobject.c:2974
    #3 bytes_new_impl                 Objects/bytesobject.c
freed by thread T0 here:
    #1 _PyBytes_Resize                Objects/bytesobject.c:3389
    #2 bytearray_resize_lock_held     Objects/bytearrayobject.c:280
    #3 bytearray_setslice_linear      Objects/bytearrayobject.c:614
    #4 bytearray_setslice             Objects/bytearrayobject.c:680
    #5 bytearray_extend_impl          Objects/bytearrayobject.c:2186
```
Single-threaded, default GIL build, no `_testcapi`, six lines of Python.

**Guarded twin.** `PyByteArray_FromStringAndSize:174` writes the identical `new->ob_exports = 0;` — and is correct, because the object is not yet reachable from Python and cannot have exports. The `__init__` site copies the initializer onto an object the caller already holds. (This is the FP-taxonomy's "non-NULL sentinel initializer" entry seen from the other side: the *write* is fine at :174 and a bug at :927 purely because of reachability.)

**Upstream status.** PR #153498 (gh-153419), **merged 2026-07-24**, moves this initialization into a new `bytearray_new` `tp_new` and deletes it from `__init__`, which cures F4 as a side effect. gh-153419's issue text never mentions `ob_exports`. **Live at `4f3be1b5777` (2026-07-15).**

**Prior art:** none. Tracker searches for `bytearray ob_exports negative`, `bytearray __new__ memoryview ob_exports`, `bytearray.__new__ buffer BufferError bypass` → 0 relevant results. Nothing in `cpython-review-findings` (172 records).

---

## [FIX] F5 — `bytearray_take_bytes_impl:1609` leaves the object pointing at freed memory when `_PyBytes_Resize` fails

```c
1609     if (_PyBytes_Resize(&self->ob_bytes_object, to_take) == -1) {
1610         Py_DECREF(remaining);
1611         return NULL;
1612     }
```

**What.** Every failure path of `_PyBytes_Resize` (`bytesobject.c:3344-3400`) releases the old object and stores **NULL** into `*pv` — verified by reading all four of them. So on return `self->ob_bytes_object == NULL` while `self->ob_bytes`, `self->ob_start` and `Py_SIZE(self)` still describe the buffer that was just freed. `MemoryError` is correctly set, so the *exception* contract holds — the *object* contract does not.

**Measured** (`errpath_take_bytes_resize_fail.py`, dense `_testcapi.set_nomemory` sweep, indices 0..40):

| index | outcome |
|---|---|
| 0 | `MemoryError`, `len=4096`, `append` OK — the `remaining` allocation at `:1597` failed; harmless |
| **1, 2** | `MemoryError`, then `bytes(b)` returns freed heap, then `b.append(1)` → **SIGSEGV (rc=139)** on `release-gil-nojit` |
| 3–40 | clean |

`release-gil-nojit-asan` at index 1:
```
AddressSanitizer: heap-use-after-free, READ of size 4096
    #1 PyBuffer_ToContiguous          Objects/memoryobject.c:1063
    #2 _PyBytes_FromBuffer            Objects/bytesobject.c:2974
freed by thread T0 here:
    #1 _PyBytes_Resize                Objects/bytesobject.c:3394
    #2 bytearray_take_bytes_impl      Objects/bytearrayobject.c:1609
    #3 bytearray_take_bytes           Objects/clinic/bytearrayobject.c.h:666
```
`debug-gil-nojit` at index 1 discloses the debug fill pattern to Python: `VAL b'\xff\xff\xff\xff\xff\xff\xff\xff'`.

**Guarded twin — same file, same callee, 1,329 lines up.** `bytearray_resize_lock_held:280-285` handles the identical `_PyBytes_Resize` failure by reinstalling the empty-bytes constant and zeroing size/alloc *before* returning -1. `take_bytes` was added later and did not copy it.

**Upstream status.** PR #153498 adds exactly that three-line repair at `:1609`. Merged 2026-07-24; **live at the review ref**.

**Do not merge with my Group A partner's finding in the same function.** `refcount-auditor`'s `bytearray_take_bytes_index_uaf.py` targets the *stale `size`* carried from `:1548` across the user `__index__` at `:1554` — a re-entrancy defect with a mutating-`__index__` trigger. F5 is the `_PyBytes_Resize`-failure path at `:1609` with an allocation-failure trigger. Same function, disjoint mechanisms, disjoint fixes.

---

## [FIX] F6 — `bytearray.__init__` skips its only `_canresize` check when the object is empty (bytearrayobject.c:930-938)

*This is the specific lead handed to me. Verdict: **missing check**, and the release-build `BufferError` is an **accident**.*

```c
930     if (Py_SIZE(self) != 0) {
931         /* Empty previous contents (yes, do this first of all!) */
932         if (PyByteArray_Resize((PyObject *)self, 0) < 0)
933             return -1;
934     }
935
936     /* Should be caused by first init or the resize to 0. */
937     assert(self->ob_bytes_object == Py_GetConstantBorrowed(Py_CONSTANT_EMPTY_BYTES));
938     assert(self->ob_exports == 0);
```

`PyByteArray_Resize` is the *only* thing on this path that calls `_canresize`. Guarding it with `Py_SIZE(self) != 0` means an **empty** bytearray with live exports walks straight past both the check and the assert.

**Wrong assert or missing check? — missing check, and upstream says so.** PR #153498 keeps the assert verbatim and replaces the guarded resize with:

```c
+    /* Disallow any __init__ call if the object is not resizable (has exports)
+       to make the handling of non-null `source` init values simpler. */
+    if (!_canresize(self)) {
+        return -1;
+    }
+    /* Empty any previous contents (do this first of all!). */
+    if (PyByteArray_Resize((PyObject *)self, 0) < 0) {
+        return -1;
+    }
```

**Is the release `BufferError` correct behaviour or an accident? — an accident, and I can name the mechanism.** The mapper's probe used `__init__("x", "ascii")`. A 1-character `str` encodes to the **cached single-character `PyBytes`** (`bytesobject.c characters[]`), which fails `_PyObject_IsUniquelyReferenced` at `:967`, so control falls past the `:967-973` fast path to `bytearray_iconcat` at `:975` — and *that* is where `_canresize` finally raises. Use a 2-character string and the fast path is taken and **nothing is raised at all**.

**Measured** (`errpath_init_canresize_bypass.py`), exit codes over 4 builds:

| probe | `release-gil` | `debug-gil` | `release-ft` | `debug-ft` |
|---|---|---|---|---|
| `a1` `a.__init__("x","ascii")` with live `memoryview` | `BufferError`, rc=0 | **rc=134** (`:938` assert) | rc=0 | rc=134 |
| `a2` `a.__init__("xy","ascii")` with live `memoryview` | **`NO_EXCEPTION`**, `a == b'xy'`, rc=0 | **rc=134** | rc=0 | rc=134 |
| `a5` same on a `clear()`ed non-empty bytearray | `NO_EXCEPTION` | rc=134 | rc=0 | rc=134 |

**Consequence worth flagging to whoever reviews the upstream fix.** Upstream's new regression test uses the 1-character string:

```python
    def test_reinit_with_view(self):
        a = bytearray()
        with memoryview(a):
            self.assertRaises(BufferError, a.__init__, "x", "ascii")
```

Measured on the **unfixed** review ref (`errpath_init_canresize_bypass.py a3`): `PROBE:a3_upstream_test=PASSES (BufferError raised)`. The test passes without the fix — it is exercising the 1-char-bytes-cache accident, not the check it was written for. Changing `"x"` to `"xy"` makes it a real regression test.

---

## [CONSIDER] F7 — a failed `bytearray` resize destroys the object's contents (bytearrayobject.c:280-291)

```c
280     int ret = _PyBytes_Resize(&obj->ob_bytes_object, alloc);
281     if (ret == -1) {
282         obj->ob_bytes_object = Py_GetConstant(Py_CONSTANT_EMPTY_BYTES);
283         size = alloc = 0;
284     }
285     bytearray_reinit_from_bytes(obj, size, alloc);
```

The exception contract is correct (`MemoryError` set, -1 returned) and the state is *consistent* — but the object is silently emptied. **Measured** (`errpath_resize_oom_dataloss.py`), `release-gil-nojit` and `debug-gil-nojit` identical:

```
PROBE:r1_before=b'IMPORTANT-DATA' len=14
PROBE:r1_resize=raised MemoryError
PROBE:r1_after=b'' len=0          # b.resize(1<<46)
PROBE:r2_setslice=raised MemoryError
PROBE:r2_after=b'' len=0          # b *= (1<<46)//14
```

`list` does not behave this way: `list_resize:182-186` (both `#ifdef` arms) explicitly refuses to fail on a shrink and never discards data. And the sibling `bytearray_setslice_linear:588-605` carries a long Issue #19578 comment reasoning about exactly which state survives an allocation failure — the file plainly treats post-failure state as a contract, and `:282-283` is the one place that answers it with "throw everything away".

**Second-order.** When `bytearray_setslice_linear:586` sees this failure with `lo == 0`, it executes `self->ob_start += growth;` (:598) on the `ob_start` that `bytearray_reinit_from_bytes` has just reset, driving `ob_start` *below* `ob_bytes`. `bytearray_resize_lock_held:219` then computes `logical_offset = ob_start - ob_bytes` as a **`size_t`** and trips `assert(logical_offset <= alloc)`. I did not reach this second-order state dynamically (it needs the failure at :586 specifically, and my sweep only hit the `:614` growth arm); recording it as unverified.

**Fix.** Leave the object untouched on failure, as `list_resize` does — the old `ob_bytes_object` is already gone, so this needs the failure handled inside `_PyBytes_Resize`'s caller by resizing into a fresh object first.

---

## [CONSIDER] F8 — `unsafe_{latin,long,float}_compare` discard `PyObject_RichCompareBool`'s -1 inside an assert (listobject.c:2833, :2858, :2873) — **gh-148268, OPEN**

Confirmed present, not re-derived. `assert(res == PyObject_RichCompareBool(v, w, Py_LT));` compares a 0/1 `res` against a call that returns **-1 with a live exception** on failure: the assert fires (debug) *and* leaves a pending exception that the sort's `IFLT` path never sees, because the fast comparators do not report failure at all.

For exact `str`/compact `int`/`float` operands the only realistic failure is `_Py_EnterRecursiveCallTstate` inside `PyObject_RichCompare`. **I could not construct the window** (`errpath_sort_assert_richcompare.py`): sweeping nested-sort depths 50/100/200/400/800/1600/3200 on `debug-gil-nojit` and `release-gil-nojit`, every depth either completed or raised a clean `RecursionError` — 14/14 runs, rc=0, no abort. The guard that would fire is reached on the way *in* to `list.sort` before the assert's own call can trip it. Recording the negative rather than asserting reachability.

---

## [CONSIDER] F9 — `bytearray_dealloc` clobbers a pending exception (bytearrayobject.c:1210-1213)

```c
1210     if (self->ob_exports > 0) {
1211         PyErr_SetString(PyExc_SystemError,
1212                         "deallocated bytearray object has exported buffers");
1213         PyErr_Print();
1214     }
```

A `tp_dealloc` can run during exception unwinding. `PyErr_SetString` overwrites the pending exception and `PyErr_Print` then consumes it — the destructor-clobber shape from the briefing's `pyerr-clear-in-destructor` template, spelled with `SetString`+`Print` instead of `Clear`. The guarded form is `PyErr_FormatUnraisable`, which saves and restores.

**Reachability: I could not establish it, and I say so.** The branch needs `ob_exports > 0` at refcount 0, but every route I tried keeps the bytearray alive through the exporter's own reference. F4's counter corruption only ever *lowers* `ob_exports`, so it cannot be used to get here. Latent; worth fixing on shape alone (one-line change to `PyErr_FormatUnraisable`), not worth a bug report without a trigger.

---

## [POLICY] F10 — two undocumented sentinel conventions in the slice

- **`list_allocate_array` (listobject.c:36-48, `Py_GIL_DISABLED` only)** returns NULL from *two* branches — the `capacity > PY_SSIZE_T_MAX/sizeof(PyObject*) - 1` overflow guard at :39-41 and the `PyMem_Malloc` failure at :43-45 — with **no exception set** in either. Correct today only because all four call sites discharge the obligation: `list_resize:143-149` (`PyErr_NoMemory`, and it correctly returns 0 rather than failing when shrinking), `list_preallocate_exact:212-215`, `PyList_New:261-264`, `list_new_prealloc:292-295`. **4/4.** Worth a one-line contract comment on the helper; a fifth caller that forgets is a `SystemError: error return without exception set`.
- **`_PyBytes_Find` (bytesobject.c:1400-1423)** returns -1 for "not found", which reads exactly like an error sentinel. **This one is fine**: `Include/internal/pycore_bytesobject.h:33-38` documents it ("If the needle is not found, return -1"). Recording the dismissal so the next reader does not re-open it.

---

## [ACCEPTABLE] F11 — the CPY-0180 `PyBuffer_Release` angle is not a defect of `bytearrayobject.c:2391`

Task item (e) asked me to assess `PyBuffer_Release(&vbytes)` at `:2391` "discarding any exception the user's `__release_buffer__` raised" as its own defect. **It does not discard it.** The behaviour is decided centrally in `releasebuffer_call_python` (`Objects/typeobject.c:11417-11473`), which:

- saves the in-flight exception with `PyErr_GetRaisedException()` at **:11423**,
- **reports** any `__release_buffer__` failure via `PyErr_FormatUnraisable` (:11454) — it is printed through `sys.unraisablehook`, not swallowed,
- asserts `!PyErr_Occurred()` and restores the saved exception at **:11472**.

**Measured** (`errpath_release_buffer_exception_state.py`), `release-gil-nojit` + `debug-gil-nojit` identical:

| probe | result |
|---|---|
| `p1` `bytearray.strip(RaisingReleaser())` | `unraisable=KeyboardInterrupt`, returns `bytearray(b'hello')` |
| `p2` `bytearray.replace(RaisingReleaser(), object())` | callback's `KeyboardInterrupt` → unraisable; the **`TypeError` from the failing second converter survives** |
| `p3` `bytes.strip(RaisingReleaser())` | same, returns `b'hello'` |

So on the three questions I was asked to apply at every re-entrancy site: **(i)** the exception state on the way out is exactly the caller's own; **(ii)** no sentinel is returned without an exception; **(iii)** *yes*, `strip` returns success after the callback failed — but that is the PEP 3118/688 contract (`bf_releasebuffer` is `void`; there is no error channel), applied identically by every `PyBuffer_Release` caller in CPython. **The defect at `:2375-2392` is the stale `myptr`, i.e. CPY-0180 — not the exception handling.** Confirmed, not re-derived.

---

# 2. Classes bounded (with denominators)

### 2.1 `unconditional_pyerr_clear` — the scanner's own rule: **3 candidates, 3 confirmed, 0 dismissed**

I enumerated every `PyErr_Clear()` in the four files by hand. **7 total:**

| site | narrowed? | verdict |
|---|---|---|
| `listobject.c:1322` | yes — `ExceptionMatches(StopIteration)` :1321 | ACCEPTABLE |
| `bytesobject.c:2940` | yes — `!ExceptionMatches(TypeError) → return` :2938 | ACCEPTABLE |
| `bytearrayobject.c:998` | yes — same guard-clause form :996 | ACCEPTABLE |
| `bytearrayobject.c:1082` | yes — `!ExceptionMatches(StopIteration)` :1080 | ACCEPTABLE |
| **`bytearrayobject.c:1171`** | **no** | **FIX (F1)** |
| **`bytearrayobject.c:1177`** | **no** | **FIX (F1)** |
| **`bytes_methods.c:608`** | **no** | **FIX (F2)** |

**Precision 3/3 = 100%. Recall 3/3 = 100%.** 4 guarded : 3 unguarded is precisely the informed-method signal, and the guarded ones include the guard-clause form the taxonomy flags as an FP class — the scanner correctly suppressed all four.

### 2.2 Sentinel returns reachable without a pending exception — **324 enumerated, 25 read in full, 0 defects**

Task item (d). Mechanical enumeration (`repro/errpath_sentinel_enumerate.py`) of every `return NULL;` / `return -1;` / `return -2;`:

| file | `NULL` | `-1` | `-2` |
|---|---|---|---|
| `listobject.c` | 56 | 52 | 0 |
| `bytesobject.c` | 86 | 19 | 0 |
| `bytearrayobject.c` | 55 | 39 | 0 |
| `bytes_methods.c` | 12 | 3 | 2 |
| **total 324** | 209 | 113 | 2 |

Bucketed by evidence within the preceding 8 lines: **157** dominated by an explicit `PyErr_Set*`/`PyErr_Format`/`PyErr_Occurred` test, **142** dominated by a failed Python-C API call, **25** with no evidence in the window. I read all 25:

| class | count | sites | verdict |
|---|---|---|---|
| `tp_iternext` exhaustion — NULL-with-no-exception **is** the protocol | 4 | `listobject.c:4071`, `:4226`, `bytearrayobject.c:2981`, `bytesobject.c:3443` | ACCEPTABLE |
| sort-internal `fail:` labels reached only from `IFLT`/`MERGE_GETMEM`, both of which raise (`merge_getmem:PyErr_NoMemory` on both arms, verified) | 9 | `listobject.c:1931, 2038, 2138, 2227, 2338, 2470, 2630, 2716, 2740` | ACCEPTABLE |
| propagating a callee's exception past the 8-line window | 9 | `listobject.c:1189, 1527, 1568, 3636`, `bytesobject.c:370, 522`, `bytearrayobject.c:599, 749`, `bytes_methods.c:465, 574` | ACCEPTABLE |
| documented "not found" sentinel | 1 | `bytesobject.c:1411` | ACCEPTABLE (F10) |
| undocumented no-exception helper, obligation discharged 4/4 by callers | 1 | `listobject.c:40` | **POLICY (F10)** |

**No `return NULL` / `return -1` in these four files reaches a Python-visible entry point with nothing raised.** This is a real negative with a real denominator, and it is the same conclusion the scanner's own `alloc_null_no_memerror: 0` and `pylong_sentinel_no_errcheck: 0` reach from the other direction.

Corroborating: the two nearest misses are *upstream-known*. `listobject.c:262` `PyList_New` is CPY-0014 (confirmed present, not re-litigated), and `bytearrayobject.c:280` is F7 — both set `MemoryError` correctly and fail on *object state*, not exception state.

### 2.3 Discarded int-status returns — **0 defects, and the scanner's `int_status_never_tested: 0` is earned**

Denominator from the envelope: **165 `int_status_callees_resolved`, 45 discovered, 32 fallible assignment sites.** I independently swept for statement-position calls to 19 fallible int-returning helpers across the four files. Two hits, both correct:

- `listobject.c:1615` `list_resize(self, size_after_pop);  // NB: shrinking a list can't fail` — the comment is **true** on both `#ifdef` arms (`:182-186` and `:143-149` both `return 0` when `newsize < allocated`). ACCEPTABLE.
- `bytesobject.c:2377` `_PyBytes_Resize(&result, output - output_start);` in `bytes_translate_impl` — the status is discarded, but every `_PyBytes_Resize` failure path stores NULL into `*pv`, and the next statement is `return result;`. This is the taxonomy's "value returned directly — NULL propagation *is* the error handling" class. ACCEPTABLE.

### 2.4 Exception clobbering by an unnarrowed reformat — **19 candidates, 5 confirmed, 4 guarded twins, 10 FP**

Hand sweep for `PyErr_Format`/`PyErr_SetString` within 6 lines after a call that runs user Python (`PyObject_GetBuffer`, `PyObject_GetIter`, `PyNumber_AsSsize_t`, `_PyNumber_Index`, `PyIter_Next`, `PySequence_Fast`, `PyObject_LengthHint`, `_PyEval_SliceIndex`, `PySlice_Unpack`, `PyObject_Call*`, `_getbytevalue`, `PyObject_Repr/Str/ASCII`):

- **5 true positives** — F3's site list.
- **4 narrowed** — `bytearrayobject.c:1062`, `:2194`, `:2218`, `bytes_methods.c:697`.
- **10 false positives**, all one shape: the reformat sits on a *wrong-type-result* or *range* branch **after** the call succeeded, with the failure branch returning separately above it — `bytesobject.c:598`, `:1743`, `:2920`, `:3010`, `:3051`, `bytes_methods.c:423`, `bytearrayobject.c:1661`, `listobject.c:3845`, and the two duplicate `||` arms of `bytesobject.c:1551` / `bytearrayobject.c:314`.

This shape is **not currently modelled by any scanner** — see §3.3.

### 2.5 The clinic boundary — **41 critical-section regions, 18 pre-lock converters, 0 defects**

Task item (c). Two independent checks.

**Structural.** I parsed both clinic headers and examined the body of every `Py_BEGIN_CRITICAL_SECTION` region:

| file | clinic CS regions | early exits (`goto`/`return`/`PyErr_*`) inside a region |
|---|---|---|
| `Objects/clinic/listobject.c.h` | 8 | **0** |
| `Objects/clinic/bytearrayobject.c.h` | 33 | **0** |
| `Objects/clinic/bytesobject.c.h` | 0 | — |
| **total** | **41** | **0** |

Every region is exactly `BEGIN; return_value = impl(...); END;` with the `exit:` label *outside*. **A lock leak across this boundary is structurally impossible**, and all 18 arbitrary-Python converters run strictly before `BEGIN` and jump to an `exit:` that is never inside a region. (This confirms the mapper's count of 41 from a different direction and completes the `scan_lock_discipline` denominator to 95.)

**Dynamic** (`errpath_clinic_boundary.py`), `release-gil-nojit` + `debug-gil-nojit` identical:

| check | result |
|---|---|
| C1 — 8 converters raising `KeyboardInterrupt` (`list.insert`, `list.pop`, `bytearray.pop`, `.insert`, `.find` start, `.find` end, `.resize`, `.hex` — covering all three converter families) | all 8 deliver **`KeyboardInterrupt`**, not `TypeError`; object usable afterwards on the same thread **and** from a second thread (no hang, 5 s join) |
| C2 — 2nd `Py_buffer` converter fails after the 1st succeeded (`bytearray.replace`) | 1st buffer **is** released at `exit:`; the 2nd's `TypeError` **survives** the 1st's `__release_buffer__` |
| C2b — the 1st exporter's `__release_buffer__` also raises | reported unraisable; the `TypeError` still reaches Python |
| C4 — successful call, `__release_buffer__` raises at `exit:` | result kept, callback exception reported unraisable |

**Clean negative.** The mapper was right that the region is invisible from the `.c` file, and right that the ordering is the safe one; I add the measurement.

### 2.6 What I did not do

- **No parity oracle** — as the brief states, these types have no pure-Python twin, so every verdict above rests on exit codes, ASan frames, or N-of-M, never on a differential.
- **F9's reachability** is unresolved, and I did not manufacture a story for it.
- **F8** is static-confirmed only; the recursion window sweep is a recorded negative, not a reproduction.
- **F7's second-order `ob_start` underflow** is unverified.
- I did not read `Objects/stringlib/*.h` beyond locating `join.h:75`; it is out of slice.
- **CPY-0180 / gh-153578 / CPY-0014 / the two `scan_init_bypass` findings**: confirmed present at the ref, not re-derived.

---

# 3. Toolkit assessment

### 3.1 The clinic-signature fix — **it behaved, and it is worth more than its finding count suggests**

**Verdict: correct, high-recall, one residual gap of 1 in 371.**

Measured by re-running `scan_error_paths.find_functions` against a reconstruction of the pre-fix single-line rule:

| file | new | old | recovered | of which clinic `_impl` |
|---|---|---|---|---|
| `listobject.c` | 132 | 110 | 22 | 18 |
| `bytesobject.c` | 107 | 67 | 40 | 26 |
| `bytearrayobject.c` | 105 | 64 | 41 | 38 |
| `bytes_methods.c` | 26 | 16 | 10 | 0 |
| **total** | **370** | **257** | **113 (31%)** | **82** |

The 82 clinic `_impl` functions were **all** invisible before — every one has the `/*[clinic end generated code: …]*/` marker between signature and brace. That set includes `bytearray___init___impl` (F4, F6), `bytearray_take_bytes_impl` (F5) and `bytearray_strip_impl_helper` (CPY-0180): **the three functions carrying this slice's memory-safety findings would all have been attributed to no function at all.** The 3 findings themselves live in non-clinic functions, so the fix did not change the *finding* count here — it changed the denominator from 257 to 370 and made every finding attributable.

Ground-truth check against column-0 definitions in the four files: 371 definitions, **370 found, 1 missed**.

**Residual gap (1 site, low value, cheap fix).** `find_functions` requires `line.startswith('{')`, so a definition whose opening brace is on the signature line is invisible. Exactly one in the slice: `bytearray_reinit_from_bytes` (`bytearrayobject.c:46-48`) —

```c
static void
bytearray_reinit_from_bytes(PyByteArrayObject *self, Py_ssize_t size,
                            Py_ssize_t alloc) {
```

It has no error path, so it costs `scan_error_paths` nothing — but it is the function doing the plain `ob_start`/`ob_alloc` writes that `scan_ft_races` flagged, so the *same* gap in `scan_ft_races`/`scan_refcounts` would matter. **Proposal:** also accept a brace at the end of the accumulated signature chunk, i.e. treat `…) {` as an opening-brace line and strip the trailing `{` before the name regex. Non-PEP-7 but present.

Everything else the ground-truth diff flagged is correctly skipped: `PyDoc_STRVAR`, `PyDoc_STRVAR_shared`, `_Py_DECLARE_STR` (macro invocations), `Py_LOCAL_INLINE` (a return-type macro — the real name, e.g. `parse_args_finds_byte`, **is** found), and `bytes` / `bytearray` / `stringlib_parse_args_finds` (clinic-input blocks and a prose comment).

### 3.2 The errno-narrowing suppression — **no evidence either way on this slice**

The new suppression for `PyErr_Clear` narrowed by a saved `errno` rather than by `PyErr_ExceptionMatches` did not fire: there is no `errno` in any of the four files (`grep` → 0 hits). It neither helped nor hurt here. **Zero denominator — do not read this slice as validating it.**

### 3.3 Recall gap found by reading: the reformat-clobber shape has no rule

The largest class I found by reading (**F3, 5 confirmed sites**) is invisible to every scanner in the toolkit:

- `scan_error_paths.unconditional_pyerr_clear` matches `PyErr_Clear` only.
- `scan_pyerr_clear` is scoped to clears.

But the *semantics* are identical: an exception raised by user code is destroyed on a failure branch without a narrowing test. The only difference is that the destroying call is `PyErr_Format`/`PyErr_SetString` instead of `PyErr_Clear`. `_PyErr_SetObject` does **not** chain a merely-pending exception into `__context__` (measured: `context=None` at all four sites), so this is total destruction, not a demotion.

**Concrete proposal — extend `unconditional_pyerr_clear` into `unconditional_pyerr_replace`:**

1. Trigger on `PyErr_Set*` / `PyErr_Format` / `_PyErr_Set*` / `_PyErr_Format` inside the failure branch of a call that can run arbitrary Python — reuse the existing arbitrary-Python callee set (`PyObject_GetBuffer`, `PyObject_GetIter`, `PyObject_Hash`, `PyNumber_AsSsize_t`, `_PyNumber_Index`, `PyObject_Call*`, `PyIter_Next`, `PyObject_RichCompare*`, `_PyEval_SliceIndex`, `PySlice_Unpack`, `PyObject_LengthHint`, `PyObject_Repr/Str/ASCII`, plus file-local `int`-status helpers already resolved by rule 4).
2. Suppress when `PyErr_ExceptionMatches` appears anywhere between the failing call and the setter — including the guard-clause form, per the existing FP entry.
3. **Suppress when the setter is not on the callee's failure branch.** This is the whole precision story: **10 of my 19 candidates** were a reformat on a *wrong-type-result* or *range* branch reached only after the call **succeeded**. The discriminator is mechanical — the failure branch has already returned above (`if (x == NULL) return NULL;` / `if (i == -1 && PyErr_Occurred()) return -1;`). Without rule 3 this fires at ~26% precision; with it, my hand sweep was **5 true / 5 candidates after suppression, plus 4 correctly-narrowed twins**.

The guarded twins for the new rule already exist in-tree and make good fixtures: `bytearrayobject.c:1062`, `:2194`, `bytes_methods.c:697`.

### 3.4 Recall gap: "the sentinel is correct but the object is wrecked"

**F5** and **F7** share a shape no rule models: the error *protocol* is honoured (sentinel returned, exception set) while `self` is left in a state that later crashes. F5 is a reproduced heap-use-after-free; F7 is silent data loss.

The mechanical tell in both is narrow enough to be a rule: **a call that writes NULL into an out-parameter that is a member of `self` on failure, where the failure branch returns without repairing that member.** `_PyBytes_Resize(&self->ob_bytes_object, …)` is the archetype; `Py_SETREF`-style repair (`bytearray_resize_lock_held:282-284`, and PR #153498's addition at `:1609`) is the guarded twin. The denominator tree-wide is small — helpers taking `PyObject **` and NULLing it on failure — which is exactly the profile of a rule that stays silent on clean code.

I would rank this **above** §3.3 for value per line of scanner: it is the shape that produced this slice's crashes.

### 3.5 Envelope / denominator notes

- Denominators were slice-scoped and correct: 4 files / 370 functions / 32 fallible assignments / 165 int-status callees resolved / 45 discovered. `functions_analyzed` matched my ground truth to 370 of 371.
- Per-file re-run of `scan_error_paths.py` on `bytearrayobject.c` alone reproduces 2 of the 3 findings with `functions_analyzed: 105` — consistent, no merge artefacts.
- The `_sample` block's "re-run, not post-hoc filter" claim held: I re-ran the scanner myself and got identical output.

---

## Summary line

```
FIX=6  CONSIDER=3  POLICY=1  ACCEPTABLE=1
scan_error_paths.unconditional_pyerr_clear: 3 candidates / 3 confirmed / 0 dismissed (precision 100%, recall 3/3 hand-enumerated)
scan_error_paths (all other rules): 0 candidates, denominators non-zero (32 / 165 / 45) -> earned zeros
sentinel returns audited: 324 enumerated, 25 read in full, 0 defects
clinic critical-section regions audited: 41 / 41, 0 lock leaks, 0 exception-state defects
novel reproduced memory-safety findings: 2 (F4, F5), both ASan-confirmed on release-gil-nojit-asan
```
