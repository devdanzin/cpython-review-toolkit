# init-bypass-checker — slice `obj-sequences` (Group A2, INFORMED)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` · **Scope:** the 4 files in `preflight/slice_files.txt`
**Env:** `~/venvs/cpython-review-toolkit/bin/python`; builds under `~/projects/python_build_matrix/builds/`
**Build-matrix validity:** re-verified independently for a third time — `git diff a1d580430c8 4f3be1b5777 --` over the four slice files plus `Objects/clinic/{listobject,bytearrayobject,bytesobject}.c.h`, `Include/cpython/{listobject,bytearrayobject}.h` and `Include/bytearrayobject.h` is **empty**. Every line number below is valid on every matrix build.

**Read first:** `AGENT_BRIEF.md` §0, `agents/include-graph-mapper.md`, `agents/git-history-context.md`, `agents/refcount-auditor.md`, `agents/error-path-analyzer.md`, `preflight/informed_briefing.md`.

**Pre-run scanner:** `scan_init_bypass.sample.json` — 2 findings, `total_nullable_fields: 2`, `files_with_nullable_fields: 1`, `nullable_fields_by_reason: {new_bypass: 2}`, `functions_analyzed: 331`.

---

## Headline

Phase 1's confirmation of `bytearray.__new__(bytearray).append(1)` is correct and is **one of two independent NULL-field families** that the same missing `tp_new` opens. The second is novel, is not in gh-153419 or in PR #153498's description, and is **not covered by the regression test that fix shipped with**:

> `bytearray.__new__(bytearray)` leaves `ob_start == NULL`, and since commit `732224e1139` (gh-139871, 2025-11-13) `PyByteArray_AS_STRING()` is a bare `ob_start` read with **no empty-string fallback**. The documented `PyByteArray_AsString()` contract — a NUL-terminated buffer, no failure mode — is therefore breakable from three lines of pure Python. Six builtins segfault on it: **`int()`, `float()`, `compile()`, `exec()`, `eval()`, `ast.parse()`**, and `_testlimitedcapi.bytearray_asstring` goes to `SystemError` on release and **`Fatal Python error` / SIGABRT on debug**.

All of it is a **regression**. On `3.12.13` and `3.14.4`, `bytearray.__new__(bytearray)` is a fully working empty bytearray — `append` succeeds, `int()`/`float()`/`compile()` raise cleanly, `PyByteArray_AsString()` returns the same valid pointer as for `bytearray()`. **17 Python-visible operations that worked on both released interpreters segfault at `4f3be1b5777`.**

Both families are cured on today's main by **PR #153498 (gh-153419), merged 2026-07-24 — nine days after the review ref** — which replaces `PyType_GenericNew` with a real `bytearray_new`. That is the gh-144330 fix shape and it is the right one. Its shipped test `test_no_init_called` covers family A exactly (all 7 growth ops, matching my matrix op-for-op) and **none of family B**, so a future lazy-init of `ob_start` would silently reopen six segfaults with a green test suite.

---

## Summary

- **Nullable fields seen: 2** (across **1** of 4 files) — both `ob_bytes_object` in `Objects/bytearrayobject.c`, both `reason: new_bypass`, both `confidence: medium`.
  **The rule fired**, so this is not the silent-zero case. But two of its three reasons had a **structurally zero denominator**: the four slice files contain **zero `PyMemberDef` and zero `PyGetSetDef` tables** (grep, 0 hits in 12,100 lines), and all three public types declare `tp_members = 0` / `tp_getset = 0` (`bytearrayobject.c:2931-2932`, `listobject.c:3957-3958`, `bytesobject.c:3259-3260`). `deletable_member` and `deletable_getset` had nothing to check here.
- Candidate reads: **2** (+0 `duplicate_locations` — the field is absent from both findings).
- **FIX: 2** (1 confirming the scanner, 1 novel and outside the scanner's model)
- **CONSIDER: 1** (`_PyBytes_Resize` family asymmetry — still live on upstream main today)
- **POLICY: 1**
- **ACCEPTABLE: 4** (1 of the 2 scanner findings, plus `list`, `bytes`, and the 4 iterator types)
- Reproduction: **7 + 10 = 17 crashing operations**, each in its own subprocess, confirmed on `release-gil-nojit`, `debug-gil-nojit`, `release-ft-nojit` and `debug-ft-nojit`; 2 ASan frames; a 4-interpreter differential.

---

# 1. Findings

## [FIX] A — `bytearray.__new__` leaves `ob_bytes_object` NULL; `_PyBytes_Resize` derefs it (`bytearrayobject.c:280` → `bytesobject.c:3349`)

**Confirmed in one line, per informed-mode rule 1.** This is `scan_init_bypass` finding #1 and Phase 1's F3. `PyByteArray_Type` sets `tp_init = bytearray___init__` (`:2938`) and `tp_new = PyType_GenericNew` (`:2940`) with no `DISALLOW_INSTANTIATION`; the zeroed instance reaches `_PyBytes_Resize(&obj->ob_bytes_object, alloc)` at `:280` with `*pv == NULL`, and `bytesobject.c:3348-3349` (`v = *pv; if (!PyBytes_Check(v) …)`) evaluates `Py_TYPE(NULL)`.

ASan, `release-gil-nojit-asan`, verbatim:

```
AddressSanitizer: SEGV on unknown address 0x000000000008 ... READ memory access
    #0 _PyBytes_Resize            Objects/bytesobject.c:3349:10
    #1 bytearray_resize_lock_held Objects/bytearrayobject.c:280:15
    #2 bytearray_append_impl      Objects/bytearrayobject.c:2125:9
    #3 bytearray_append           Objects/clinic/bytearrayobject.c.h:1248:20
```

**What Phase 1 did not have: the reachable set.** `repro/initbypass_matrix.py` + `initbypass_matrix_driver.py` run **81 bytearray operations, one subprocess each**, on a `__new__`-bypassed receiver:

| | crashes | survives |
|---|---|---|
| **7 / 81** | `append`, `extend(b"AB")`, `extend(iter([1,2]))`, `insert`, `+=`, `b[0:0] = b"XY"`, `resize(4)` — all rc=**139** | 74 |

Identical set on **`release-gil-nojit`, `debug-gil-nojit`, `release-ft-nojit` (`PYTHON_GIL=0`), `debug-ft-nojit` (`PYTHON_GIL=0`)**, and identical for both constructions (`bytearray.__new__(bytearray)` and a subclass whose `__init__` skips `super().__init__()`).

**The 74 survivors are the useful half.** Everything that reads — `len`, `repr`, `hex`, `decode`, `split`, `strip`, `find`, `join`, `center`, `copy`, `take_bytes`, `memoryview(b)`, `+`, `*`, `%`, `__reduce_ex__` — returns correct empty-bytearray results. Three near-misses explain the boundary:

- `b *= 3` and `b.__imul__(0)` do **not** crash: `bytearray_repeat` computes `0 * 3 == 0` and `bytearray_resize_lock_held:232` returns 0 on `requested_size == Py_SIZE(self)` before reaching `:280`.
- `b.clear()` / `b.resize(0)` do not crash for the same reason.
- `memoryview(b)` **prevents** the crash: `bytearray_getbuffer_lock_held:69` bumps `ob_exports`, so `_canresize:115` raises `BufferError` at `:235` — *before* `:280`. `repro/initbypass_reinit_siblings.py ba_new_then_memoryview_then_append` measures exactly that. An accidental guard, worth knowing when reading a bug report that says "it doesn't reproduce".

**Fix:** landed upstream — PR #153498 adds `bytearray_new` (`Objects/bytearrayobject.c`, `tp_new = bytearray_new`) and deletes the first-init branch from `__init__`. This is the gh-144330 shape and closes all 7 entry points at once. **Live at `4f3be1b5777`.**

---

## [FIX] B — `bytearray.__new__` leaves `ob_start` NULL, breaking the `PyByteArray_AsString()` C-API contract: 6 more builtins SIGSEGV · **NOVEL**

**Why the scanner cannot see this and family A is not the whole story.** `bytearray___init___impl:926` calls `bytearray_reinit_from_bytes(self, 0, 0)`, which at `:49` writes

```c
self->ob_bytes = self->ob_start = PyBytes_AS_STRING(self->ob_bytes_object);
```

Two more fields, `char *` not `PyObject *`, assigned through a file-local helper rather than in the `tp_init` body. `scan_init_bypass` models neither the indirection nor the type, so `ob_start` never enters `total_nullable_fields`, and its sink is not in the sink set: the sink is an **accessor that returns the field**.

`Include/cpython/bytearrayobject.h:24-28` (this tree):

```c
static inline char* PyByteArray_AS_STRING(PyObject *op)
{
    return _PyByteArray_CAST(op)->ob_start;
}
```

Commit **`732224e1139`** (gh-139871, `take_bytes` / bytes-backed bytearray, 2025-11-13) deleted the `if (Py_SIZE(self)) { return self->ob_start; } return _PyByteArray_empty_string;` fallback and the `_PyByteArray_empty_string` symbol with it (`git show 732224e1139 -- Include/cpython/bytearrayobject.h`). `Include/bytearrayobject.h:32` still declares `PyAPI_FUNC(char *) PyByteArray_AsString(PyObject *)` with no documented failure mode, and `PyByteArray_AsString` (`bytearrayobject.c:202-208`) is a bare `return PyByteArray_AS_STRING(self);`.

**Measured** (`repro/initbypass_bytearray_capi_contract.py capi`), 4/4 builds:

```
PROBE:AsString_normal=<valid pointer>
PROBE:AsString_bypassed=None            <- PyByteArray_AsString() returned NULL
PROBE:Size_bypassed=0
PROBE:tlc_asstring_normal=bytearray(b'')
PROBE:tlc_asstring_bypassed=RAISED SystemError: <built-in function bytearray_asstring>
                                          returned NULL without setting an exception
```

`_testlimitedcapi.bytearray_asstring` (`Modules/_testlimitedcapi/bytearray.c:67-69`) is CPython's own model consumer — `s = PyByteArray_AsString(obj); if (s == NULL) return NULL;` with no `PyErr_Set*`, because the contract said NULL was impossible. On the **debug** builds that path is fatal:

```
debug-gil-nojit / debug-ft-nojit:  rc=134
Fatal Python error: _Py_CheckFunctionResult: a function returned NULL without setting an exception
SystemError: <built-in function bytearray_asstring> returned NULL without setting an exception
```

**Six builtins segfault** (`repro/initbypass_bytearray_consumers.py`, 46 probes, **10 crash / 36 survive**, one subprocess each, identical on all four builds):

| Python | C path | crash |
|---|---|---|
| `int(b)` / `int(b,0)` / `int(b,10)` / `int(b,16)` / subclass | `abstract.c:1578` `_PyLong_FromBytes(PyByteArray_AS_STRING(o), …)` → `longobject.c:3065` | SIGSEGV @ 0x0 |
| `float(b)` | `floatobject.c:202` → `pystrtod.c:356` `strchr(NULL, …)` | SIGSEGV @ 0x0 |
| `compile(b, …)` / `exec(b)` / `eval(b)` / `ast.parse(b)` | `pythonrun.c:1652` `str = PyByteArray_AS_STRING(cmd);` → `:1673` `strlen(str)` | SIGSEGV @ 0x0 |

ASan frames, verbatim:

```
    #0 PyLong_FromString      Objects/longobject.c:3065:12
    #1 _PyLong_FromBytes      Objects/longobject.c:3168:14
    #2 PyNumber_Long          Objects/abstract.c

    #0 __strlen_evex
    #2 _Py_SourceAsString     Python/pythonrun.c:1673:9
    #3 builtin_compile_impl   Python/bltinmodule.c:951:11
```

**Guarded twin — the branch immediately below the crashing one, in all three files.** Each of `pythonrun.c:1651-1665`, `abstract.c:1577-1586` and `floatobject.c:201-215` has the same structure:

```c
    else if (PyByteArray_Check(x)) {
        str  = PyByteArray_AS_STRING(x);        /* NULL after the bypass */
        size = PyByteArray_GET_SIZE(x);
    }
    else if (PyObject_GetBuffer(x, &view, PyBUF_SIMPLE) == 0) {
        /* Copy to NUL-terminated buffer. */    /* <- the twin, verbatim comment */
        ...
    }
```

The `PyByteArray_Check` fast path exists precisely to *skip* that copy, on the strength of the invariant `732224e1139` broke. The three consumers are innocent; the defect is in `bytearrayobject.c` and the fix belongs there.

**Differential — this is a regression, not longstanding:**

| operation | 3.12.13 | 3.14.4 | main @ `4f3be1b5777` |
|---|---|---|---|
| `bytearray.__new__(bytearray).append(1)` | `bytearray(b'\x01')` | `bytearray(b'\x01')` | **SIGSEGV 139** |
| `int(bytearray.__new__(bytearray))` | `ValueError` | `ValueError` | **SIGSEGV 139** |
| `float(…)` | `ValueError` | `ValueError` | **SIGSEGV 139** |
| `compile(…)` / `exec` / `eval` / `ast.parse` | code object / `None` / `SyntaxError` / `Module` | same | **SIGSEGV 139** |
| `PyByteArray_AsString(bypassed)` | valid, `== normal` | valid, `== normal` | **NULL** |

**Upstream status.** PR #153498's `bytearray_new` calls `bytearray_reinit_from_bytes(self, 0, 0)`, which writes `ob_start = PyBytes_AS_STRING(<empty bytes>)` — non-NULL — so family B is cured **incidentally**. *(Read from the patch, not measured: I did not build today's main.)*

**The test gap, and why it matters.** `test_no_init_called` (`Lib/test/test_bytes.py`, added by #153498) probes `insert`, `extend(b"x")`, `extend([1,2,3])`, `resize(4)`, `__init__(5)`, `__init__(b"xyz")`, `take_bytes()`, `take_bytes(0)`, `append`, `+=`, `a[:] = b"xyz"` — **family A exactly, and nothing else**. Nothing pins `PyByteArray_AS_STRING(bytearray.__new__(bytearray)) != NULL`. A later change that lazily defers `ob_start` (a natural follow-on to the bytes-backed rework) reopens six segfaults with a green suite. **Concrete addition:** `self.assertEqual(int(bytearray.__new__(bytearray).join([b"1"])), 1)` is too indirect — use `self.assertRaises(ValueError, int, bytearray.__new__(bytearray))` and `self.assertRaises(SyntaxError, eval, bytearray.__new__(bytearray))`, which fail loudly (SIGSEGV) on the unfixed tree.

**Prior art: none.** `gh api -X GET search/issues` for `bytearray __new__ segfault`, `PyByteArray_AS_STRING NULL`, `bytearray tp_new PyType_GenericNew` returns gh-139871, gh-140128, gh-153419/#153498, gh-140557 ("Force alignment of empty bytearray … buffers") and gh-88082 (the `Py_TPFLAGS_DISALLOW_INSTANTIATION` sweep) — none mentions a NULL `AS_STRING`. Nothing in the 172-record findings catalog.

---

## [CONSIDER] C — `_PyBytes_Resize` is the only member of its `PyObject **` family with no `*pv == NULL` guard — **still live on upstream main today**

Per the standing instruction to sweep the sink's whole API family. Measured on this tree:

| API | site | `*pv == NULL` handled? |
|---|---|---|
| `_PyTuple_Resize` | `tupleobject.c:1044` | **yes** — `if (v == NULL \|\| !Py_IS_TYPE(v, &PyTuple_Type) \|\| …) { *pv = 0; Py_XDECREF(v); PyErr_BadInternalCall(); return -1; }` |
| `PyBytes_Concat` | `bytesobject.c:3277` | **yes** — `if (*pv == NULL) return;` |
| `PyBytes_ConcatAndDel` | `bytesobject.c:3326` | **yes** — delegates |
| `PyUnicode_Append` | `unicodeobject.c:11736` | **yes** — `left == NULL` → `PyErr_BadInternalCall()` |
| **`_PyBytes_Resize`** | **`bytesobject.c:3348-3349`** | **NO** — `v = *pv; if (!PyBytes_Check(v) \|\| newsize < 0)` |

`_PyTuple_Resize` is the direct analogue and writes `v == NULL ||` in front of the identical type check; `_PyBytes_Resize` does not. A one-token change (`if (v == NULL || !PyBytes_Check(v) || newsize < 0)`) turns all 7 family-A SIGSEGVs into `SystemError: bad argument to internal function` — defence in depth that is independent of any `tp_new`.

**Verified still missing on upstream main** (fetched `repos/python/cpython/contents/Objects/bytesobject.c` today): PR #153498's `Objects/bytesobject.c +1/-0` is `assert(v != bytes_get_empty());`, an unrelated invariant assert, **not** a NULL guard. `_PyBytes_Resize` has ~25 callers tree-wide, of which several pass a `PyObject **` that is a struct member of a Python-visible object (`Modules/_io/bytesio.c:110 &self->buf`, `Modules/_elementtree.c:2832 &self->data`, `Modules/cjkcodecs/multibytecodec.c:221 &buf.outobj`). I read only the two in-slice callers; the rest are named as leads, not findings.

---

## [ACCEPTABLE] D — `scan_init_bypass` finding #2 (`bytearray_take_bytes_impl:1609`) is **dominated and unreachable** with a NULL field

The scanner flags `_PyBytes_Resize(&self->ob_bytes_object, to_take)` at `:1609` with the same `new_bypass` reason. It cannot fire on a bypassed object: `:1579`

```c
    if (to_take == 0 || size == 0) {
        return Py_GetConstant(Py_CONSTANT_EMPTY_BYTES);
    }
```

dominates `:1609`, and `size = Py_SIZE(self)` is **always 0** on a `__new__`-bypassed bytearray — every path that could make it non-zero goes through `bytearray_resize_lock_held:280` and crashes there first. **Measured:** `take_bytes()` and `take_bytes(0)` return `b''` cleanly on 4 builds × 2 constructions.

**Do not merge with `error-path-analyzer`'s F5**, which is the *same line* with a disjoint mechanism (`_PyBytes_Resize` *failing* under OOM and storing NULL into a field that is then left dangling). Same site, different bug, different fix; #153498 fixes F5 with a three-line repair at `:1609` and fixes this one by never letting the field be NULL.

---

## [ACCEPTABLE] E — `list` is clean, and it is clean *by design*, not by accident

`PyList_Type` has the identical shape — `tp_init = list___init__` (`listobject.c:3964`) + `tp_new = PyType_GenericNew` (`:3966`), no `DISALLOW_INSTANTIATION` — and is the natural sibling. It does not have the bug, for a reason worth recording:

- **The zeroed state is a documented valid state.** `Include/cpython/listobject.h:5-22` states the invariant `ob_item == NULL ⇒ ob_size == allocated == 0`, which `PyType_GenericAlloc` establishes for free.
- **`list___init___impl` says so out loud.** `listobject.c:3524-3530`: `/* Verify list invariants established by PyType_GenericAlloc() */` followed by three asserts, the third being `assert(self->ob_item != NULL || self->allocated == 0 || self->allocated == -1);`. It then guards its own clear with `if (self->ob_item != NULL)`.
- **Every reader tolerates it.** `list_resize:114` asserts `self->ob_item != NULL || newsize == 0`; `list_clear_impl:874` returns early on `items == NULL`.

**Measured:** 39 operations × `list.__new__(list)` and a `super()`-skipping subclass → **0 crashes**. Plus a 12-scenario `__init__`-re-entry battery (`repro/initbypass_reinit_siblings.py`): re-entry from a sort key, from a user `__lt__` during sort, from a generator inside `__init__`, from a generator inside `extend`, `l.__init__(l)`, during iteration, from `__eq__` inside `index`/`remove`/`count`/`in`, from `__repr__`, from a `__del__` run by `list_ass_slice`'s recycle array, and three-deep nested `__init__` — **0 crashes across `release-gil-nojit`, `debug-gil-nojit` and `debug-ft-nojit`** (54 runs). The sort path is correctly self-detecting: every re-entrant mutation ends in `ValueError: list modified during sort`, and `keyfunc_fail:3186-3200` restores the saved array.

The F4-shaped question ("does `__init__` perform a constructor-only write on a live object?") is **no** for `list`: `list___init___impl` writes no field directly — it calls `list_clear` and `_list_extend`, both ordinary mutation paths.

---

## [ACCEPTABLE] F — `bytes` cannot be bypassed

`PyBytes_Type` has `tp_init = 0` and a real `tp_new = bytes_new` (`bytesobject.c:3266/3268`). Measured: `bytes.__init__ is object.__init__` → `True`; `b"abcd".__init__(b"zzzz")` / `("x","ascii")` / `(1,2,3)` are all silent no-ops; a subclass whose `__init__` skips `super().__init__()` still gets a fully-initialised `b'payload'` because the work is in `tp_new`. 54 operations × 2 constructions → **0 crashes**. `bytes` is the in-slice model of the correct shape.

---

## [ACCEPTABLE] G — the four iterator types: unguarded `it_seq` reads that are **not reachable**, and the reason is a rule the scanner's model gets wrong

`PyListIter_Type` (`listobject.c:3994`), `PyListRevIter_Type` (`:4148`), `PyByteArrayIter_Type` (`bytearrayobject.c:3076`) and `PyBytesIter_Type` (`bytesobject.c:3513`) all end their positional slot tables at `tp_members`, so `tp_init`, `tp_alloc` **and `tp_new` are all 0** and `tp_base` is `object`. Their `it_seq` is set only by a C factory (`list_iter`, `bytearray_iter`, `bytes_iter`), never by a `tp_init` — which is why the scanner reports no nullable field for them at all.

Five reads would fault on a NULL `it_seq`:

| site | read | guarded on |
|---|---|---|
| `listobject.c:4074` | `list_get_item_ref(it->it_seq, index)` → `Py_SIZE(NULL)` | `it_index >= 0` only |
| `listobject.c:4096` | `PyList_GET_SIZE(it->it_seq)` | `it_index >= 0` only |
| `bytearrayobject.c:2983-2988` | `seq = it->it_seq; assert(PyByteArray_Check(seq)); Py_BEGIN_CRITICAL_SECTION(seq); … Py_SIZE(seq)` | `it_index >= 0` only; the `assert` is debug-only |
| `bytearrayobject.c:3013` | `PyByteArray_GET_SIZE(it->it_seq)` | `it_index >= 0` only |
| `bytearrayobject.c:3054` | `PyByteArray_GET_SIZE(it->it_seq)` in `setstate` | `it_index >= 0` only |

Their guarded twin is `striter_next:3441-3442` (`seq = it->it_seq; if (seq == NULL) return NULL;`) and `striter_len`/`striter_setstate`/`striter_reduce`, which all test `it_seq` **directly**. That asymmetry is exactly the "a guard that looks like a NULL check but isn't" shape — and it is nevertheless **safe**, twice over:

1. **`T.__new__(T)` is blocked.** `type_ready_set_new` (`Objects/typeobject.c:9415-9438`) *automatically* sets `Py_TPFLAGS_DISALLOW_INSTANTIATION` on any static, non-heap type whose `tp_new == NULL` and whose `tp_base` is `&PyBaseObject_Type`, then NULLs `tp_new`. **Measured on all four:** `DISALLOW= True`, `TypeError: cannot create 'list_iterator' instances`, and `type 'list_iterator' is not an acceptable base type` (so no subclass route either).
2. **The only reachable NULL window is ordered.** Exhaustion writes `it_index = -1` **before** dropping `it_seq` (`listobject.c:4077-4082`, `bytearrayobject.c:2996-2999`), and the drop's `Py_DECREF` is the last statement. A bytearray/list *subclass* with a `__del__` that re-enters the iterator therefore always sees `it_index == -1` and takes the guarded path in all five sites. I checked this explicitly rather than assuming it.

**This is the precision correction the toolkit needs** — see §3.4. `tp_new == 0` is a bypass shape only when `tp_base` is a *non-`object`* type with a real `tp_new`, or when `tp_new` is explicitly `PyType_GenericNew`.

---

## [POLICY] H — `list.__sizeof__()` under-reports by 8 bytes during `list.sort()`

`list_sort_impl:2973` sets `self->allocated = -1` for the duration of the sort; `list___sizeof___impl:3589` (the non-`Py_GIL_DISABLED` arm) computes

```c
    res += (size_t)self->allocated * sizeof(PyObject *);
```

`(size_t)(-1) * 8` wraps to `2^64 - 8`, so the result is `base - 8`. **Measured**, `release-gil-nojit` and `debug-gil-nojit` identically: `[3,1,2].__sizeof__()` is `72` normally and **`32`** from inside a sort key — below the 40-byte object header. `sys.getsizeof` reports 48 instead of ≥56. The `Py_GIL_DISABLED` arm reads `list_capacity(ob_item)` behind an `ob_item != NULL` test and is unaffected (measured `56` on `release-ft-nojit`). Not memory-unsafe, no crash on any build; a signed sentinel read through an unsigned cast. One line: guard with `if (self->allocated > 0)`.

---

## [CONFIRMED — known] I — `error-path-analyzer`'s F4 reaches the same corruption through the **clinic pre-lock converter**

Confirmed, not re-derived: `bytearray.__new__(bytearray)` + `memoryview` + `__init__()` drives `ob_exports` to -1. My contribution is a second delivery route, relevant to task (d). `repro/initbypass_clinic_boundary.py export_then_index_inits_then_resize`:

```python
b = bytearray.__new__(bytearray); mv = memoryview(b)      # ob_exports == 1
b.resize(Idx())            # Idx.__index__ calls b.__init__()
```

The `_PyNumber_Index` converter runs at `Objects/clinic/bytearrayobject.c.h:618`, **ten lines before** `Py_BEGIN_CRITICAL_SECTION(self)` at `:628`, so `__init__`'s `self->ob_exports = 0` (`bytearrayobject.c:927`) lands *before* the impl's `_canresize` is consulted. The impl's guard looks like it protects and does not.

| build | result |
|---|---|
| `release-gil-nojit` | `resize=None \| len(b)=8 \| release=ok \| post_extend=ok \| **COUNTER_BROKEN: grew with a live view**`, rc=0 |
| `debug-gil-nojit` | **rc=134**, `Assertion 'obj->ob_exports >= 0' failed` (`bytearrayobject.c:89`) |

Same root, same upstream fix (#153498 replaces the branch with an unconditional `_canresize`). Recorded so the clinic window is on the record as a route, per the brief's item (d).

---

# 2. Classes bounded (with denominators)

| class | denominator | verdict |
|---|---|---|
| **Deletable `PyMemberDef` fields** | **0 member tables in 4 files / 12,100 lines**; `tp_members = 0` on all 3 public types and all 4 iterator types | **structural zero — not a verified negative.** The rule had nothing to check. `delattr` on any of the three types raises `AttributeError`/`TypeError` (measured, 3 types × 3 names) |
| **Deletable `PyGetSetDef` setters** | **0 getset tables**; `tp_getset = 0` on all types | **structural zero**, same |
| **`__new__`-bypassable types with a `tp_init`** | **2 of 3** public types (`bytearray`, `list`); `bytes` has a real `tp_new` | **1 broken** (bytearray, 2 independent field families), **1 clean by documented design** (list) |
| **Full reachable set on a bypassed `bytearray`** | **81 in-slice operations** + **46 out-of-slice consumers** = 127 probes, each in its own subprocess, × 2 constructions × 4 builds | **17 crash, 110 survive.** 7 in-slice (family A), 10 consumer probes covering 6 distinct builtins (family B) |
| **Value corruption on the survivors** | **28 operations** compared bypassed-vs-`bytearray()` | **0 differences.** This is a pure crash/no-crash split, not a wrong-value class — the (e) instruction's other half comes back negative here |
| **`list` under `__new__` bypass and `__init__` re-entry** | 39 ops × 2 constructions; 12 re-entry scenarios × 3 builds (54 runs) | **clean, 0 crashes** |
| **`bytes` under `__new__` bypass** | 54 ops × 2 constructions | **clean** |
| **Iterator types** | **4** types, **12** accessors (`next`/`len`/`length_hint`/`reduce`/`setstate` across the four) | **clean.** 4/4 `DISALLOW_INSTANTIATION` (measured); the 5 unguarded `it_seq` reads are ordered-safe on the only reachable NULL window |
| **Other `ob_exports` writers** (task b: "does bytearray have other re-init paths?") | **22** `ob_exports` accesses in the file; **2** are assignments (`:174` construction-time, `:927` the F4 bug); the other 20 are `++`/`--` pairs | **1 broken (`:927`, known/F4).** 16 Python-visible operations run against a bytearray with a live `memoryview` (`append`, `extend`, `+=`, `*=`, slice-grow, slice-shrink, `del` slice, `del` ext-slice, `clear`, `pop`, `remove`, `resize`, `take_bytes`, `__init__`, `__init__()`, `__reduce_ex__`): **15/16 raise `BufferError` and 16/16 leave the counter healthy**. `__reduce__`/`__setstate__` is not a re-init path — `bytearray.__reduce_ex__(2)` yields `(bytearray, ('AAAA','latin-1'), None)`, i.e. *construction*, and `bytearray` has no `__setstate__` (measured `hasattr` → `False`) |
| **The `PyObject **` sink family** | **5** APIs | **1 missing the guard** (`_PyBytes_Resize`), 4 have it |
| **Clinic-boundary converters vs a bypassed receiver** | **10** probes over all three converter families (`_PyNumber_Index`, `_PyEval_SliceIndex`, `PyObject_GetBuffer`) | 7 behave correctly (the converter's `__init__` initialises the receiver, the impl then runs normally); 2 crash **inside** the converter for the family-A reason; 1 is the F4 route above. **No defect is created by the clinic layer itself** — it touches only the object header |

**Deliberately not re-litigated:** CPY-0180 (`strip` UAF), gh-153578 (`extend`/`setslice` OOB), gh-153570 (`take_bytes` stale size), F1/F2/F3/F5/F6 (error-path-analyzer), the `striter_next` FT race (refcount-auditor), CPY-0014.

---

# 3. Toolkit assessment

### 3.1 `scan_init_bypass` on this slice: **precision 1/2, and the miss is cheap to explain**

Finding #1 (`:280`) is a live SIGSEGV with 7 entry points. Finding #2 (`:1609`) is unreachable — dominated by the `to_take == 0 || size == 0` early return at `:1579`, which is a *value* guard on a different field (`Py_SIZE`) that happens to be perfectly correlated with the nullable one. `summary.dominated_sinks: 0` shows the scanner's domination analysis did not consider it, correctly: it only models NULL checks on the field itself.

**I would not trade recall for this.** Both findings point at the same root cause and the same fix; a triager who reads #1 reads #2 in thirty seconds. The cheap improvement is *reporting*, not suppression: emit the dominating early-return conditions between function entry and the sink so a reader can dismiss #2 without opening the file.

### 3.2 [HIGH VALUE] Recall gap — non-`PyObject *` bypass-nullable fields are invisible, and that is where 6 of the 8 crashing builtins live

Family B is a `char *` field (`ob_start`), assigned through a file-local helper (`bytearray_reinit_from_bytes`) rather than in the `tp_init` body, and consumed by an accessor that *returns* it (`PyByteArray_AS_STRING`) rather than by any listed sink. Three independent reasons the current model cannot reach it, and the finding is a **regression that turned 6 clean `ValueError`s into segfaults**.

**Proposal (informational, not a finding — keeps precision at zero cost):** for every type whose slot table matches the `new_bypass` shape, emit

```json
"bypass_nullable_fields": [
  {"type": "PyByteArray_Type", "field": "ob_bytes_object", "ctype": "PyObject *",
   "assigned_at": "Objects/bytearrayobject.c:925", "via": "tp_init"},
  {"type": "PyByteArray_Type", "field": "ob_start", "ctype": "char *",
   "assigned_at": "Objects/bytearrayobject.c:49", "via": "tp_init -> bytearray_reinit_from_bytes"},
  {"type": "PyByteArray_Type", "field": "ob_bytes", "ctype": "char *", ...},
  {"type": "PyByteArray_Type", "field": "ob_exports", "ctype": "Py_ssize_t", ...}
]
```

— i.e. *every* field the bypassable `tp_init` writes, directly or through a one-hop file-local helper, regardless of C type, with no finding attached. On this slice that turns "2 nullable fields" into a 4-field lead list that names `ob_start` and `ob_exports`, the two fields behind the novel finding and behind F4. The recall win is in the *denominator*, which is what an agent actually reads. One hop is enough here and bounds the cost; say so in the envelope.

Second half of the proposal: add **accessor-returns-the-field** to the sink taxonomy. A `static inline` function or macro in a public header whose whole body is `return X->field;` publishes the field to the entire tree, and every caller becomes a sink. `PyByteArray_AS_STRING` is that; `PyBytes_AS_STRING` is the guarded twin (`ob_sval` is a flexible array member and can never be NULL). This is one grep over `Include/cpython/*.h`, and it is how a reviewer would have found family B by hand.

### 3.3 [CONFIRMED, independently] `functions_analyzed: 331` overstates coverage — `bytesobject.c` is parsed to 71%

Independently re-measured with `scratchpad/check_truncation.py` (a fresh `parse_bytes_for_file` + `extract_functions` per file, cross-checked against a column-0 `name(` ground truth):

| file | lines | functions returned | last parsed start | ground-truth defs beyond it |
|---|---|---|---|---|
| `listobject.c` | 4,312 | 132 | 4,280 (99%) | 1 |
| `bytearrayobject.c` | 3,125 | 106 | 3,109 (99%) | 1 |
| `bytes_methods.c` | 738 | 26 | 728 (99%) | 1 |
| **`bytesobject.c`** | **3,925** | **67** | **2,779 (71%)** | **46** |

Same root cause `refcount-auditor` §6.1 reports (the `bytes_methods[]` table at `:2787` interleaving brace initialisers with bare clinic macro names). Seconding it with independent numbers, and adding what it costs *this* rule specifically: **nothing in findings, but the whole invisible tail is exactly where the init-bypass-relevant code of `bytesobject.c` lives** — `striter_next/len/reduce/setstate`, `bytes_iter`, `_PyBytes_Resize` (the sink itself), `PyBytes_Concat`, and the 12-function `PyBytesWriter_*` API. I hand-read all of it (§1.G, §1.C) and it is clean, so the earned answer here is "no missed finding, verified by hand". The envelope should still say ~377, not 331. `refcount-auditor`'s proposed envelope canary (`functions_analyzed` vs an independent count, with a `notes[]` entry above 20% disagreement) would have surfaced this without a hand check; I second it.

### 3.4 [PRECISION] `tp_new == 0` is **not** a bypass shape for a static type whose base is `object`

The agent contract says a type whose `tp_new` is "`0` (inherited)" can be instantiated as `T.__new__(T)`. For a **static** `PyTypeObject` whose `tp_base` is `0` or `&PyBaseObject_Type`, that is false and has been since bpo-43916: `type_ready_set_new` (`Objects/typeobject.c:9425-9438`) sets `Py_TPFLAGS_DISALLOW_INSTANTIATION` automatically and then sets `tp_new = NULL`. The in-code comment says why — *"tp_new is not inherited for static types whose base class is 'object'; … object.__new__ wouldn't insure the invariants that the extension type's own factory function ensures."*

Measured on all four slice iterator types: `TypeError: cannot create 'X' instances`.

**Proposal.** In the slot-table resolution, treat a static `PyTypeObject` with `tp_new == 0` **and** `tp_base ∈ {0, &PyBaseObject_Type}` as `DISALLOW_INSTANTIATION`, and keep `tp_new == 0` as a bypass reason only when `tp_base` names another in-tree type. Note the two ways to read `tp_base`: designated (`.tp_base = &PyX_Type`) and positional — and in a positional table `tp_base` is the slot *after* `tp_iternext`/`tp_methods`/`tp_members`/`tp_getset`, so a table that simply **stops early** (as all four of these do) leaves it 0, which is the DISALLOW case. On this slice the correction is denominator-only. Tree-wide it is the difference between flagging every static iterator, view and dict-view type in `Objects/` and flagging none of them — and per the brief's own framing, that class is large.

### 3.5 [ENVELOPE] Distinguish "no deletable members" from "no member tables"

`nullable_fields_by_reason: {new_bypass: 2}` implies the other two reasons found nothing, but not *why*. Here it is because the files contain **zero** `PyMemberDef` and **zero** `PyGetSetDef` tables — the rules were not exercised at all. Add `denominators.member_tables_seen`, `denominators.member_entries_seen`, `denominators.getset_tables_seen`, `denominators.getset_entries_seen`. Three integers turn "high-confidence rules found nothing" into "high-confidence rules had nothing to look at", which is the exact distinction the standing rule exists to force. On this slice all four would be **0**.

### 3.6 What the informed method bought

Three times, concretely.

- **The guarded-twin rule found family B**, not the scanner. I went looking for `PyByteArray_AS_STRING`'s consumers only because the *twin* of `_PyBytes_Resize` (`_PyTuple_Resize`, which does check NULL) made me ask what else about the bypassed object was NULL. Three of the crashing call sites carry their own guarded twin in the very next `else if`, with the comment `/* Copy to NUL-terminated buffer. */` spelling out the invariant that was broken.
- **The released-interpreter differential turned a bug into a regression.** `int(bytearray.__new__(bytearray))` raising `ValueError` on 3.12.13 and 3.14.4 and segfaulting on main is what makes this worth a tracker issue rather than a footnote, and it is one `git log -S` away from the commit that caused it.
- **Phase 1's F3 saved the whole budget for (b)–(e).** Confirming `append` took one line; everything above came from the time that bought.

---

## 4. Artifacts

All under `reports/obj-sequences/repro/`, all `.py` files, one probe per subprocess:

- `initbypass_matrix.py` + `initbypass_matrix_driver.py` — the 81-operation per-method matrix (9 receiver constructions) (family A; also the `list`/`bytes` negatives)
- `initbypass_bytearray_consumers.py` — the 46-probe out-of-slice consumer sweep (family B); also the 4-interpreter differential driver
- `initbypass_bytearray_capi_contract.py` — `values` (28-op bypassed-vs-normal value differential, 0 diffs) and `capi` (`PyByteArray_AsString` → NULL, `_testlimitedcapi` SystemError/SIGABRT)
- `initbypass_reinit_siblings.py` — 18 probes: the `list` `__init__`-re-entry battery, the `bytes` no-op proof, the 16-operation `ob_exports` writer sweep, `delattr`, the memoryview accidental guard
- `initbypass_clinic_boundary.py` — 10 probes for task (d), including the F4 pre-lock-converter route
- `scratchpad/check_truncation.py` — the independent §3.3 measurement

## 5. What I did not do

- **I did not build today's main.** PR #153498 curing family B is read from the patch (`bytearray_new` → `bytearray_reinit_from_bytes(self, 0, 0)` → `ob_start = PyBytes_AS_STRING(<empty bytes>)`), not measured. Falsifier: build main and run `initbypass_bytearray_consumers.py int`.
- **I did not file anything upstream.** Family B and the `_PyBytes_Resize` guard gap have no issue I could find; filing is the campaign's call.
- **I read only 2 of the ~25 tree-wide `_PyBytes_Resize` callers.** The other 23 are enumerated leads, not findings; three of them pass a `PyObject **` that is a struct member of a Python-visible object.
- **No TSan work.** This class is single-threaded; the FT builds were used only to confirm the crash set is build-independent (spot check, 10 of 81 ops + 5 of 46 consumers).
- **No parity oracle** — as the brief states, these three types have no shipped pure-Python twin, so every verdict here rests on an exit code, an ASan frame, or an N-of-M count.
- **The `list.__sizeof__` wrap (H) is measured but not chased** — I did not check whether any stdlib consumer of `sys.getsizeof` is affected.
