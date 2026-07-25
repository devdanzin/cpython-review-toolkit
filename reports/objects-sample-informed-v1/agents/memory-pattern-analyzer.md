# memory-pattern-analyzer — Objects/ sample (informed)

**Shape owned:** `integer-overflow-in-allocation` (briefing §6), plus the scanner's other two
rules: `mismatched_alloc_free` (allocator-family crossing) and `gc_untrack_without_track`.
**Target:** CPython main @ `4f3be1b5777` (3.16.0a0). Differentials run on the in-tree
`./python` (`583cb03da83`, Py_DEBUG + ASan; `Objects/structseq.c` is byte-identical between the
two commits — verified with `git diff`).

## Scanner volume

```
raw candidates in sample:  1   |  confirmed: 1  |  dismissed: 0  |  precision: 100% (n=1)
raw candidates in Objects/: 4  |  confirmed: 1  |  dismissed: 3  |  precision: 25%
found by reading, not by the scanner: 5 (1 FIX cluster + 4 siblings)
```

Per-rule precision on all of `Objects/`:

| rule | candidates | confirmed | FP class |
|---|---|---|---|
| `alloc_size_overflow` | 2 | 0 | *bounded-by-an-existing-allocation* (new FP class, below) |
| `gc_untrack_without_track` | 2 | 1 | *file-level gate matched a different type's dealloc* |
| `mismatched_alloc_free` | 0 | 0 | — (genuine clean negative, see "Classes bounded") |

---

## Findings

### FIX

#### F1 — `Objects/structseq.c:77` · `PyStructSequence_New` · allocation size from a Python-writable type attribute wraps → heap-buffer-overflow WRITE

**This is my bug shape, live, and the scanner is structurally incapable of seeing it.**

```c
Py_ssize_t size = REAL_SIZE_TP(type), i;          /* :68  -> tp_dict["n_fields"] */
...
obj = PyObject_GC_NewVar(PyStructSequence, type, size);   /* :77 */
...
for (i = 0; i < size; i++)                        /* :84-85 */
    obj->ob_item[i] = NULL;
```

`REAL_SIZE_TP` is `get_type_attr_as_size(tp, &_Py_ID(n_fields))` → `PyLong_AsSsize_t` of the
type's own `n_fields` dict entry (`structseq.c:27-42`). Every structseq type built by the
**public** `PyStructSequence_NewType()` is a plain heap type with no `Py_TPFLAGS_IMMUTABLETYPE`,
so `n_fields` is writable from pure Python. Confirmed mutable: `os.stat_result`,
`time.struct_time`, `os.terminal_size`, `os.times_result`, `os.uname_result`,
`pwd.struct_passwd`, `grp.struct_group`, `resource.struct_rusage`, `signal.struct_siginfo`,
`_thread.ExceptHookArgs`, `_lsprof` entries, `_remote_debugging` types. (Static builtins —
`sys.version_info`, `sys.float_info` — *are* immutable; the boundary is exactly
`PyStructSequence_NewType` vs `_PyStructSequence_InitBuiltinWithFlags`.)

`PyObject_GC_NewVar` → `_PyObject_GC_NewVar` (`Python/gc.c:2044`) → `_PyObject_VAR_SIZE(tp, nitems)`
= `_Py_SIZE_ROUND_UP(tp_basicsize + nitems * tp_itemsize, 8)`. The multiply is **unchecked**.

gdb, at `structseq.c:84`, `os.terminal_size.n_fields = 2**62`:

```
$1 = size            = 4611686018427387904      (2**62)
$3 = tp_basicsize    = 32
$5 = sizeof(PyStructSequence) = 40
obj                  = 0x7c6ff6ef8270           <- allocation SUCCEEDED, 32 bytes
```

`2**62 * 8` wraps to 0, so a 32-byte object (room for exactly `ob_item[0]`) is returned with
`ob_size` claiming 2**62 slots. The fill loop at `:84-85` is compiled to a `memset` of
`size * 8 == 0` bytes and is elided, so the first out-of-bounds write is in the caller:

```
==3712581==ERROR: AddressSanitizer: heap-buffer-overflow ... WRITE of size 8
    #0 structseq_new_impl  Objects/structseq.c:235:25     res->ob_item[i] = Py_NewRef(v);
    #1 type_call           Objects/typeobject.c:2467
```

**Reproducer — 3 lines of pure Python, no C extension, no `ctypes`:**

```python
import os
os.terminal_size.n_fields = 2**62
os.terminal_size((7, 9))          # heap-buffer-overflow WRITE
```

**Guarded twin (the fix):** `Objects/tupleobject.c:52-57`, `tuple_alloc` — the sibling
variable-length type in the same family (structseq *is* a `PyTuple` subclass):

```c
if ((size_t)size > ((size_t)PY_SSIZE_T_MAX - (sizeof(PyTupleObject) -
            sizeof(PyObject *))) / sizeof(PyObject *)) {
    return (PyTupleObject *)PyErr_NoMemory();
}
PyTupleObject *result = PyObject_GC_NewVar(PyTupleObject, &PyTuple_Type, size);
```

Exactly the `n > MAX/elem` division guard the briefing names. `PyStructSequence_New` lacks it.
A second guarded twin for the multiply-then-size idiom is `_PyTuple_Repeat`
(`tupleobject.c:617`): `if (input_size > PY_SSIZE_T_MAX / n) return PyErr_NoMemory();`.

**Classification: FIX.** Wrong-size allocation followed by a write, reachable from pure Python,
in a public C-API entry point used by ~20 stdlib modules and arbitrary third-party extensions.
Not Class J — nothing here is an abort-vs-`MemoryError` question.

#### F1a — `Objects/structseq.c:242` · same root cause, **no integer overflow needed**, hard SEGV

```c
for (i = len; i < max_len; ++i) {
    const char *name = type->tp_members[i - n_unnamed_fields].name;   /* :242 */
    if (PyDict_GetItemStringRef(dict, name, &ob) < 0) {
```

`max_len` is the same unvalidated `n_fields`; `tp_members` is a fixed C array sized at type
creation. Any `n_fields` larger than the real field count walks off it, hits the `{NULL}`
sentinel and beyond, and hands a NULL/garbage `char*` to `PyDict_GetItemStringRef`:

```python
import os
os.terminal_size.n_fields = 64
os.terminal_size((7, 9), {"zzz": 1})
# AddressSanitizer: SEGV on unknown address 0x000000000000, READ
```

This is the most easily reachable member of the cluster — no wrapping arithmetic, no ASan
needed, deterministic SEGV on any build.

#### F1b — `Objects/structseq.c:281-283, 308` · `structseq_repr` — unguarded prealloc multiply + OOB `tp_members` read

```c
Py_ssize_t prealloc = (type_name_len + 1 + VISIBLE_SIZE(obj) * 5 + 1);   /* :281-282 */
...
const char *name_utf8 = typ->tp_members[i].name;                        /* :308 */
```

`VISIBLE_SIZE(obj)` is `Py_SIZE(obj)`, set from the equally writable `n_sequence_fields`.
`os.terminal_size.n_sequence_fields = 64` then `repr(...)` crashes.

**Guarded twins, in the same 14-file sample, for the *same* writer-prealloc idiom:**
`Objects/unionobject.c:286` and `Objects/genericaliasobject.c:95` both write
`estimate = (len <= PY_SSIZE_T_MAX / K) ? len * K : len;` — the guarded form. `structseq_repr`
and `tuplerepr` (`tupleobject.c:306`) use the bare multiply. For `tuplerepr` the operand is a
memory-bounded tuple length so it is ACCEPTABLE; for `structseq_repr` the operand is *not*
memory-bounded, which is precisely what makes it a bug.

#### F1c — `Objects/structseq.c:41` · `get_type_attr_as_size` returns `-1` without an exception → fatal error

```c
return PyLong_AsSsize_t(v);      /* :41 — -1 is both a legal value and the error sentinel */
```

Callers test `< 0` and `return NULL`. `n_fields = -1` therefore returns NULL with no exception set:

```python
import os
os.stat_result.n_fields = -1
os.stat_result((0,)*10)
# Fatal Python error: _Py_CheckFunctionResult: a function returned NULL without setting an exception
```

This is the briefing's `return-null-without-exception` shape (§5), not mine, but it lives in the
same four lines as F1 and shares the fix. Temporal note: `69f51625e63` (gh-148119, 2026-04-06,
"Refactor `get_type_attr_as_size` to improve error handling") touched this exact function three
months before HEAD and added a `PyErr_Occurred()` branch for the *lookup* failure while leaving
the `PyLong_AsSsize_t` return unchecked. A week-old-style guarded twin that missed the sibling.

**Single fix for F1/F1a/F1b/F1c:** validate once in `get_type_attr_as_size` —
reject negative, reject `> tp_itemsize`-implied bounds, and check `PyErr_Occurred()` after
`PyLong_AsSsize_t`; or (better) stop re-reading these from the mutable type dict at all and
cache them at type-creation time. `Lib/test/test_structseq.py` has **no** test that mutates
`n_fields` / `n_sequence_fields` — the whole class is untested.

#### F2 — `Objects/odictobject.c:1952` · `odictiter_new` · object freed before `GC_Track`, dealloc untracks unconditionally *(the scanner's one sample candidate — CONFIRMED)*

```c
di = PyObject_GC_New(odictiterobject, &PyODictIter_Type);       /* :1945 */
...
di->di_result = _PyTuple_FromPairSteal(Py_None, Py_None);
if (di->di_result == NULL) {
    Py_DECREF(di);                                              /* :1952 */
    return NULL;
}
...
_PyObject_GC_TRACK(di);                                         /* :1967 */
```

`odictiter_dealloc` (`:1715-1725`) opens with `_PyObject_GC_UNTRACK(di)` — the **macro**, which
asserts `_PyObject_GC_IS_TRACKED(op)` and in a release build does
`_PyGCHead_SET_NEXT(prev, next)` with `prev == next == NULL` (`gc_alloc` zeroes the GC head at
`Python/gc.c:2017-2018`) → NULL-pointer write. Debug build: `_PyObject_ASSERT_FROM` fires with
"object not tracked by the garbage collector".

Worse, at `:1952` only `di_result` has been assigned. `di->kind`, `di->di_odict`,
`di->di_current` are still garbage, and `odictiter_dealloc` does
`Py_XDECREF(di->di_odict); Py_XDECREF(di->di_current); if (di->kind & _odict_ITER_ITEMS) Py_DECREF(di->di_result);`
— a `Py_DECREF(NULL)` on a garbage `kind`. So this is simultaneously the
`dealloc-of-uninitialized-object` shape (briefing §3).

**Guarded twin (member init):** `Objects/dictobject.c:5617` `dictiter_new` — the function
`odictiter_new` was copied from — assigns `di_dict`, `di_used`, `len`, `di_pos` *before* the
fallible `_PyTuple_FromPairSteal`, so its early free only leaves `di_result` unset.

**Reachability:** `iter(od.items())` under allocation failure of the 2-tuple.
**Honest caveat:** I could not reproduce it dynamically. `_testcapi.set_nomemory` sweeps
(n = 0..40, with `sys._clear_internal_caches()` and a 50 000-tuple freelist drain) never made
`_PyTuple_FromPairSteal(Py_None, Py_None)` fail — `tuple_alloc(2)` is served from the size-2
freelist, which bypasses the allocator hook entirely. Classified **FIX on static evidence**
(the shape is the confirmed gh-152107 / OOM-0006/0017 pattern); the freelist makes it latent
rather than routine, and a `--without-freelists` or fresh-subinterpreter build removes that cover.

### CONSIDER

#### C1 — `Objects/dictobject.c:5646` · `dictiter_new` · **un-found sibling of F2**, invisible to every scanner in the toolkit

Identical shape: `Py_DECREF(di)` at `:5646` precedes `_PyObject_GC_TRACK(di)` at `:5653`, and
`dictiter_dealloc` (`:5658`) opens with `_PyObject_GC_UNTRACK(di)` at `:5662`. So
`dictiter_new`'s member initialisation is the guarded twin for F2 but its **GC-track discipline
is not** — the two functions share the defect.

The scanner did not report it, and *not because of the GC gate* (`dictobject.c` contains the
macro). It was missed because `extract_functions` sees only 187 of dictobject.c's functions and
stops at line 5232 — see "Toolkit assessment / chassis defect A". CONSIDER rather than FIX only
because, like F2, the failing allocation is freelist-shadowed.

### POLICY / ACCEPTABLE

- **ACCEPTABLE** `Objects/call.c:491` `_PyObject_Call_Prepend` — `PyMem_Malloc((argcount + 1) * sizeof(PyObject *))`; `argcount = PyTuple_GET_SIZE(args)`, and an *n*-tuple already occupies ≥ 8*n* bytes, so `argcount ≤ PY_SSIZE_T_MAX/8`. Cannot overflow. **Scanner FP.**
- **ACCEPTABLE** `Objects/listobject.c:2985` `list_sort_impl` — `PyMem_Malloc(sizeof(PyObject *) * saved_ob_size)`, `saved_ob_size = Py_SIZE(self)`. Same bound. **Scanner FP.**
- **ACCEPTABLE** `Objects/listobject.c:262` `PyList_New` — early `Py_DECREF(op)` before track, but `list_dealloc` (`:555`) uses the NULL-safe **function** `PyObject_GC_UnTrack`. **Scanner FP** caused by the file-level gate matching `_PyObject_GC_UNTRACK` at `:4052`, which belongs to `listiter_dealloc`, a *different type*.
- **ACCEPTABLE** `Objects/templateobject.c:232` `template_iter`, `Objects/interpolationobject.c:218` `_PyInterpolation_Build`, `Objects/codeobject.c:751` `_PyCode_New` — all three have the free-before-track pattern, all three deallocs use the safe `PyObject_GC_UnTrack` function. Gate-suppressed and **correctly** so (see recall verdict).
- **ACCEPTABLE** `Objects/weakrefobject.c:1054` `PyObject_ClearWeakRefs` — `PyTuple_New(num_weakrefs * 2)`; each weakref is a live heap object, so the count is memory-bounded. Stated bound: the only thing preventing an OOB `PyTuple_SET_ITEM` if the list grew mid-loop is the debug-only `assert(num_items / 2 < num_weakrefs)` at `:1071`, and growth is excluded by the `Py_REFCNT(object) != 0` early return at `:1021`.
- **ACCEPTABLE** `Objects/capsule.c:236` — `PyMem_Malloc((strlen(name) + 1) * sizeof(char))`; `sizeof(char) == 1`, and `name` is a C-string literal from a C extension, not Python input. Correctly paired with `PyMem_Free` at `:280`.
- **POLICY** `Objects/tupleobject.c:306` `tuplerepr` — `prealloc = 1 + 1 + (2 + 1) * (n - 1) + 1` is the unguarded sibling of the `unionobject.c:286` / `genericaliasobject.c:95` guarded idiom. Safe today because `n` is a tuple length. Worth normalising for consistency, not a bug.
- **POLICY** `Objects/structseq.c:775` — `spec.basicsize = (int)(sizeof(PyStructSequence) + (hidden - 1) * sizeof(PyObject *))` truncates to `int`. `hidden` comes from the C `PyStructSequence_Desc`, not from Python, so it is a C-caller contract issue only.

---

## New siblings of known shapes

1. **`dictobject.c:5646` `dictiter_new`** — sibling of F2 (`odictobject.c:1952`). Same
   free-before-track, same untrack-macro dealloc. *(C1)*
2. **`structseq.c:242` and `structseq.c:308`** — siblings of F1 within the same file: three
   different unvalidated reads of the same Python-writable type-dict fields, two of which
   crash without needing any arithmetic overflow at all. *(F1a, F1b)*
3. **`structseq.c:41`** — a `return-null-without-exception` sibling (briefing §5) sitting inside
   the function a 3-month-old "improve error handling" commit already touched. *(F1c)*
4. **Guarded-twin map for the "compute a size from a Python-controlled count" idiom across the
   sample** — `tupleobject.c:52` ✅ guarded, `tupleobject.c:617` ✅ guarded,
   `unionobject.c:286` ✅ guarded, `genericaliasobject.c:95` ✅ guarded,
   `odictobject.c:569` ✅ (`PyMem_NEW` overflow-checks internally),
   `structseq.c:545` ✅ (`PyMem_NEW`), **`structseq.c:77` ❌**, **`structseq.c:281` ❌**.
   structseq is the only outlier in the sample, and it is the only one whose operand is not
   memory-bounded.

---

## Classes bounded (clean negatives)

- **`mismatched_alloc_free`: genuinely clean, in the sample and across all of `Objects/` (0 candidates, 0 by reading).** The complete allocator surface of the 14 sample files is nine call sites: `odictobject.c:569` (`PyMem_NEW`) / `:582,589,799` (`PyMem_Free`); `odictobject.c:704` (`PyMem_Malloc`) / `:722` (`PyMem_Free`); `structseq.c:545` (`PyMem_NEW`) / `:667,700,737,781` (`PyMem_Free`); `capsule.c:236` (`PyMem_Malloc`) / `:280` (`PyMem_Free`). Every pair stays inside the `PyMem_*` family. Object allocation is uniformly `PyObject_GC_New*` / `PyObject_GC_Del`. No raw `malloc`/`free`, no `sprintf`/`strcpy`/`strcat`/`alloca` anywhere in the sample (`PyOS_snprintf(buf, sizeof(buf), ...)` at `tupleobject.c:1297` is the bounded form).
- **Double-free / use-after-free on the odict node tables: clean.** `_odict_clear_nodes` (`:794-812`) sets `od_fast_nodes = NULL` immediately after `PyMem_Free`; `_odict_resize` (`:589-590`) frees the old table and assigns the new one in the same statement pair; `_odictnode_DEALLOC` decrefs the key *before* `PyMem_Free`, with a comment saying why.
- **`od_fast_nodes` resize multiply: clean.** `size = ONE << dk_log2_size` (`:568`) then `PyMem_NEW(_ODictNode *, size)` — `PyMem_NEW` overflow-checks, and the operand is a dict's log2 table size, not a Python integer. This was the site the assignment flagged; it is correctly guarded.
- **`tupleobject.c`: clean for this class**, and it supplies the two canonical guarded twins.
- **`genericaliasobject.c`, `unionobject.c`, `funcobject.c`, `descrobject.c`, `iterobject.c`, `cellobject.c`, `lazyimportobject.c`: no allocator calls and no free-before-track sites** (verified by running the GC rule with its gate disabled over all of `Objects/`).

---

## Toolkit assessment

### Headline: did the v0.7 `_PyObject_GC_UNTRACK` file-level macro gate cost recall?

**No — zero false negatives in this scope. But it is right for the wrong reason, and it is
simultaneously producing false positives.**

Method: I monkey-patched `S._GC_UNTRACK_MACRO = b""` (gate off) and re-ran
`_check_gc_untrack_without_track` over all of `Objects/`. 2 candidates → **6**. The four extra:

| site | why the scanner missed it | is it real? |
|---|---|---|
| `templateobject.c:232` `template_iter` | **gate** (file has no macro) | **No** — `templateiter_dealloc:44` uses `PyObject_GC_UnTrack` (function) |
| `interpolationobject.c:218` `_PyInterpolation_Build` | **gate** | **No** — `interpolation_dealloc` uses the function |
| `codeobject.c:751` `_PyCode_New` | **gate** | **No** — `code_dealloc:2410` uses the function |
| `dictobject.c:5646` `dictiter_new` | **parse truncation, not the gate** | **YES** — `dictiter_dealloc:5662` uses the macro *(finding C1)* |

So the gate suppressed 3 sites and all 3 are genuine true negatives. **The tightening did not
cost recall.** It is nonetheless unsound in principle and imprecise in practice:

- **Unsound direction (FN):** the premise "no untrack macro in *this file* ⇒ no `tp_dealloc` in
  this file can hit O6" assumes the constructor and the `tp_dealloc` live in the same file.
  Nothing enforces that. A type allocated in file A and deallocated by a `tp_dealloc` in file B
  (or by an inherited/`subtype_dealloc` path) is silently exempted.
- **Imprecise direction (FP):** conversely, a file that contains the macro *anywhere* lets
  every `PyObject_GC_New` through. `PyList_New` (`listobject.c:262`) is exactly this — it was
  reported only because `listiter_dealloc:4052` (a different type) uses the macro, while
  `list_dealloc:555` uses the safe function. That is 1 of the 2 `gc_untrack_without_track`
  findings on all of `Objects/`, i.e. 50% of the rule's noise.

**The fix is the one the assignment anticipated, made type-level rather than file-level:**
resolve the second argument of `PyObject_GC_New(T, &SomeType)` → find `SomeType`'s
`tp_dealloc` (designated initialiser `.tp_dealloc = f` or the positional slot table) → check
whether **that function's body** contains `_PyObject_GC_UNTRACK`. All the information is
in-file for every site in `Objects/`; a cross-file fallback can keep the current file-level
behaviour. This turns 6 raw candidates into 2 findings, both true positives — 100% precision
with no recall loss.

### Chassis defect A (highest value, affects **every** scanner) — Argument Clinic `*_METHODDEF` tables break the parse

`tree_sitter_utils.extract_functions` returns **187 functions for `Objects/dictobject.c` and
stops at line 5232 of 8597**. `dictiter_new`, `dictiter_dealloc`, the whole dict-view family
and `dict_popitem` are invisible to all 12 scanners. Root cause: a `PyMethodDef[]` initialiser
that mixes bare Clinic macro identifiers with brace entries and no separators —

```c
static PyMethodDef mapp_methods[] = {
    DICT___CONTAINS___METHODDEF                      /* dictobject.c:5253 */
    {"__getitem__", _PyDict_Subscript, METH_O | METH_COEXIST, getitem__doc__},
```

tree-sitter-c cannot parse `IDENT {…}` in an initialiser list, emits an `ERROR` node, and in
dictobject.c **never recovers**: one top-level `ERROR` spans lines 5268-8597 (39% of the file).
Across `Objects/`, 3 493 lines sit inside top-level `ERROR` nodes; 3 344 of them are this one
file. `bytearrayobject.c` loses 60 lines to the same construct.

**Verified fix** (byte-length preserving, so all line/byte offsets survive):

```python
CLINIC_MACRO = re.compile(rb'(?m)^([ \t]*)([A-Z][A-Z0-9_]*_METHODDEF)([ \t]*)$')
def _neutralize_clinic_macros(src: bytes) -> bytes:
    return CLINIC_MACRO.sub(
        lambda m: m.group(1) + b'{0},' + b' ' * (len(m.group(2)) - 4) + m.group(3), src)
```

Measured: `dictobject.c` 187 → **214** functions, `dictiter_new` recovered, error lines
3 344 → 2 417. `bytearrayobject.c` error lines 60 → **0**, no function-count change (pure win).
This one regex is what turns finding C1 from invisible into reportable.

### Chassis defect B — `#ifdef` straddling an `if` condition

After fix A, `dictobject.c` still loses lines 6181-8597 to a single `ERROR`. The trigger is
`dictiter_iternextitem` (`:6182`):

```c
#ifdef Py_GIL_DISABLED
    if (dictiter_iternext_threadsafe(d, self, &key, &value) == 0) {
#else
    if (dictiter_iternextitem_lock_held(d, self, &key, &value) == 0) {
#endif
```

This is the known `extract_functions` / `preproc_ifdef` family (the multidict gap, fixed in
cext #64 / ft #24) in a harder form: the conditional splits an expression, not a statement
list. A per-function recovery pass — when a top-level `ERROR` node exceeds N lines, re-slice on
`^\w[\w \t\*]*\n\w+\(` boundaries and parse each candidate function body independently — would
recover the rest and is scanner-agnostic.

### Recall gap in my own rule: `alloc_size_overflow` cannot express F1

Three independent reasons the highest-severity bug of my own class was invisible:

1. **No var-object allocator in `_SIZE_ARG_INDEX`.** The dict is
   `{malloc, PyMem_Malloc, PyMem_RawMalloc, PyObject_Malloc, realloc, PyMem_Realloc, PyMem_RawRealloc, PyObject_Realloc}`.
   `PyObject_GC_NewVar` / `PyObject_NewVar` / `PyObject_GC_Resize` take `(Type, typeobj, nitems)`
   — the multiply lives inside `_PyObject_VAR_SIZE`, so **there is no `a * b` in the source at
   all** and `_multiplies()` finds nothing. The rule can never flag a var-object
   over-allocation, which is the single most common shape of this bug in `Objects/`.
2. **Type-dict reads are not a taint source.** `_TAINT_CALL_NAMES` covers `PyLong_As*`,
   container lengths and `Py_SIZE`. It does not cover the `PyDict_GetItemWithError(tp_dict, …)`
   → `PyLong_AsSsize_t` idiom, which is how structseq gets its size. (It *does* contain
   `PyLong_AsSsize_t`, but only via `_head_call` on a direct assignment — here the call is
   wrapped in the helper `get_type_attr_as_size`, one frame away.)
3. **No interprocedural step.** `size` is a `Py_ssize_t` returned by a helper in the same file;
   `_collect_taint` is strictly intra-function.

### Precision: the one FP class worth encoding

Both `alloc_size_overflow` findings on `Objects/` are the same class:
**bounded-by-an-existing-allocation.** `PyTuple_GET_SIZE`, `PyList_GET_SIZE`, `Py_SIZE`,
`PyBytes_GET_SIZE`, `PyUnicode_GET_LENGTH`, `PyObject_Length` and friends return the length of
an object that is *already in memory*; multiplying such a value by a small element size cannot
overflow `Py_ssize_t`, because the container itself would have had to exceed
`PY_SSIZE_T_MAX / elemsize` bytes. Unbounded operands are a different set: `PyLong_As*` /
`PyNumber_AsSsize_t` results, `PyArg_Parse*` outputs, and values read out of a mutable type dict.

Splitting `_TAINT_CALL_NAMES` into `_BOUNDED_LENGTH_CALLS` (dismiss) and `_UNBOUNDED_INT_CALLS`
(keep) removes 2 of 4 `Objects/` findings — 100% of that rule's noise — and loses nothing,
because F1's operand is in the unbounded set.

### Prompt issues

- The agent definition's checklist (`sprintf` → `snprintf`, `strcpy` → `strncpy`, "PyMem vs
  malloc mismatch") is a **generic-C-extension** checklist. In `Objects/` those classes are
  empty by construction: PEP 7 forbids the unsafe string functions and CPython core uses one
  allocator family per object. I spent the first pass confirming three clean negatives that the
  briefing's shape catalog already implies are clean. The prompt should lead with the
  var-object / type-attribute size sources, and demote the string-function checklist to a
  one-line grep.
- Conversely the assignment's hand-picked reading list was correct and load-bearing:
  "`structseq.c` … allocates a *variable-length* struct sequence sized from `n_in_sequence` /
  `n_fields` … Check every size computation" is exactly where the bug is. The scanner had
  nothing there; the pointer did all the work.

### Concrete tuning proposals (ranked)

1. **Neutralise Clinic `*_METHODDEF` macros before parsing** — add
   `_neutralize_clinic_macros()` to `tree_sitter_utils.parse_bytes` (regex above, byte-length
   preserving). Recovers 27 functions in `dictobject.c` including `dictiter_new`, and zeroes
   `bytearrayobject.c`'s errors. **Benefits all 12 scanners, not just this one.** Regression
   test: assert `extract_functions` finds `dictiter_new` in `Objects/dictobject.c`.
2. **New rule `varobject_nitems_unguarded`.** Flag `PyObject_GC_NewVar` / `PyObject_NewVar` /
   `PyObject_GC_Resize` / `_PyObject_GC_Resize` whose `nitems` argument is a local that is not
   preceded in the function body by a `PY_SSIZE_T_MAX /` division guard or a
   `__builtin_mul_overflow`. There are exactly **9** such call sites in all of `Objects/`
   (`frameobject.c`, `structseq.c`×1, `tupleobject.c`×2, `memoryobject.c`, `codeobject.c`×2,
   `genobject.c`×2) — a tiny, hand-auditable population, and `structseq.c:77` is the only one
   whose operand is not derived from an already-allocated object. High precision by construction.
3. **Make the GC gate type-level, not file-level.** Resolve `PyObject_GC_New(T, &SomeType)` →
   `SomeType`'s `tp_dealloc` → test *that function body* for `_PyObject_GC_UNTRACK`; fall back to
   the current file-level test when the type or its dealloc is not in the file. Removes the
   `PyList_New` FP and closes the cross-file hole. 6 raw → 2 findings, both true.
4. **Split the taint table.** `_BOUNDED_LENGTH_CALLS` = {`Py_SIZE`, `PyTuple_Size`,
   `PyTuple_GET_SIZE`, `PyList_Size`, `PyList_GET_SIZE`, `PyBytes_*_SIZE`, `PyByteArray_*_SIZE`,
   `PyUnicode_GetLength`, `PyUnicode_GET_LENGTH`, `PySequence_Size`, `PyObject_Length`, …} —
   dismiss when the multiplier is a `sizeof(...)` ≤ 16. Keep `PyLong_As*`, `PyNumber_AsSsize_t`
   and `PyArg_Parse*` outputs as taint. Add `bounded-by-an-existing-allocation` to
   `data/cpython_non_bugs.md`.
5. **New taint source: mutable-type-dict reads.** Treat
   `PyDict_GetItemWithError(_PyType_GetDict(tp), …)` / `PyType_GetDict` / `_PyType_Lookup`
   feeding an integer conversion as *unbounded* Python-controlled input, and propagate it one
   call frame (a same-file static helper whose every `return` is tainted returns taint). Without
   the one-frame step F1 stays invisible even with proposal 2.
6. **Doc line for the agent prompt / `data/cpython_non_bugs.md`:** "In CPython core, `sprintf` /
   `strcpy` / `strcat` and cross-family `malloc`/`PyMem_Free` are effectively absent (PEP 7 +
   review). Do not spend a pass on them; spend it on variable-length object allocation and on
   sizes read from mutable type dictionaries."
7. **Per-function recovery for `ERROR` nodes > 200 lines** (chassis defect B). Lower confidence
   in the exact implementation, but the measurement is unambiguous: 2 417 lines of
   `dictobject.c` remain unreachable to every scanner even after proposal 1.
