# api-deprecation-tracker — obj-mappings slice

**Scope:** `Objects/dictobject.c` (8,597 lines) + `Objects/setobject.c` (3,228 lines),
`/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0).

**Build correspondence:** the matrix builds are at `a1d580430c8` (2026-07-18), three days
newer than the target ref. `git diff 4f3be1b5777 a1d580430c8 -- Objects/dictobject.c
Objects/setobject.c` is **empty** — both slice files are byte-identical between the target
ref and every build used below, so all dynamic evidence in this report applies to the
target ref unchanged.

---

## 1. Denominators

The scanner's own note, quoted verbatim from `scanners/scan_deprecated_apis.sample.json`:

> `"denominator_note": "line-based rule: functions_analyzed is -1 (N/A). The denominator is files_analyzed x apis_in_vocabulary."`

`functions_analyzed` is `-2` in the merged sample (`-1` per file) because this scanner's
population is **API call sites, not functions**. So the envelope's nominal denominator is
`2 files x 93 vocabulary APIs = 186 (file, API) pairs`. That number is real but nearly
useless: it says nothing about how much deprecated-API surface these files actually have.

I measured the population that matters by word-matching all 93 vocabulary names across both
files independently of the scanner
(`/tmp/.../scratchpad/denom_count.py`, re-runnable):

| denominator | dictobject.c | setobject.c | slice |
|---|---:|---:|---:|
| `Py*` / `_Py*` call-shaped tokens (all C-API call sites) | 1,381 | 495 | **1,876** |
| distinct `Py*` / `_Py*` callee names | 321 | 114 | 435 |
| vocabulary APIs with ≥1 textual occurrence | 3 | 1 | **4** |
| of those, **call sites** of a deprecated API | 1 | 1 | **2** |
| of those, **definition sites** of a deprecated API | 2 | 0 | **2** |
| `_PyObject_GC_UNTRACK` (strict macro) occurrences | 2 | 2 | **4** |
| `PyObject_GC_UnTrack` (tolerant function) occurrences | 1 | 1 | **2** |
| `_PyObject_GC_TRACK` occurrences | 12 | 9 | 21 |
| preprocessor conditionals (`#if` / `#ifdef` / `#ifndef`) | 61 | 10 | **71** |
| of those, version guards (`PY_VERSION_HEX`, `Py_LIMITED_API`) | 0 | 0 | **0 (structural)** |

So the headline numbers are:

- **deprecated-api: 2 findings out of 1,876 C-API call sites** (0.11%). Both reported, both
  `soft` tier, both `CONSIDER`, neither has a removal date.
- **gc-untrack-macro-form: 1 finding out of 4 strict-macro sites** in the slice. I read all
  four; the other three are true negatives, individually justified in §3.
- Two further vocabulary occurrences are the **definition sites** of `Py_DEPRECATED(3.14)`
  private APIs — correctly not reported as call sites, but they are real work this slice
  owes for 3.18 (§2, F4).

A "3 of 141 in `Objects/`" framing would be misleading: the slice is 3.3% of `Objects/` by
file count but the deprecated-API density here is *lower* than the directory average,
because `dictobject.c` and `setobject.c` were both swept during the 3.13–3.14 private-API
cleanup. This is a genuinely clean pair of files for this class — on an earned denominator.

---

## 2. Findings

### F1 — `Objects/dictobject.c:5662` `dictiter_dealloc` — **`gc-untrack-macro-form`** — FIX — **already recorded as CPY-0015**

**Confirmed, not re-litigated.** This scanner finding *is* CPY-0015, reached from the
opposite direction: CPY-0015 was found by `scan_uninit_dealloc` (constructor frees a
half-built object), this rule finds the same site by asking which dealloc uses the strict
untrack macro. Same file, same function pair (`dictiter_new:5646` / `dictiter_dealloc:5662`),
same mechanism, same symptom. That is a **recall corroboration**, and it is the only thing
the two rules independently agree on in this slice.

Mechanism, in one paragraph so the cross-check below is legible: `dictiter_new` allocates
with `PyObject_GC_New` (:5621), and for the two item-iterator types builds
`di->di_result = _PyTuple_FromPairSteal(...)` (:5643). On that allocation's failure it does
`Py_DECREF(di)` at :5646 — **before** `_PyObject_GC_TRACK(di)` at :5653. So `dictiter_dealloc`
runs on a never-tracked object and hits
`_PyObject_ASSERT_FROM(op, _PyObject_GC_IS_TRACKED(op), "object not tracked by the garbage
collector", ...)` (`Include/internal/pycore_gc.h:254`) → `SIGABRT`. With `NDEBUG` the assert
compiles out and the GIL build executes the doubly-linked-list unlink
(`pycore_gc.h:262-266`) on a `PyGC_Head` whose `_gc_prev`/`_gc_next` are zero — GC list
corruption rather than a clean abort.

**Guarded twin — and it is in this slice:** `set_dealloc` (`Objects/setobject.c:742`) uses
the **function** form `PyObject_GC_UnTrack(so)`, and it has *exactly the same shape*:
`make_new_set_untracked` (:1351) allocates with `_PyType_AllocNoTrack`, fills from the
iterable, and on `set_update_local` failure does `Py_DECREF(so)` at :1372 — while the
`_PyObject_GC_TRACK` lives in the **caller** (`make_new_set:1383`). The two deallocs even
carry the *verbatim identical* comment, `/* bpo-31095: UnTrack is needed before calling any
callbacks */`, so they are copies of one another that diverged only on macro-vs-function.
`Objects/dictobject.c:3661` `dict_dealloc` is a second tolerant twin in the other file.

**Does the twin's guard cover *this* threat model?** Yes, and this is the one case where I
can say that without hedging. `PyObject_GC_UnTrack` (`Python/gc.c:1948`,
`Python/gc_free_threading.c:2681`) is literally
`if (_PyObject_GC_IS_TRACKED(op)) { _PyObject_GC_UNTRACK(op); }`, and the strict macro's own
header comment (`pycore_gc.h:241-245`) says *"The object must be tracked by the GC. See also
the public PyObject_GC_UnTrack() which accept an object which is not tracked."* The guard
addresses untracked-state tolerance, which is precisely the failure mode. **It is not a NULL
guard** — `_PyObject_GC_IS_TRACKED` dereferences unconditionally in both the default and
free-threaded builds, so `PyObject_GC_UnTrack(NULL)` still segfaults. Do not describe the fix
as "NULL-safe".

**Verification of the twin (new evidence).** I exercised the untracked free path in
`make_new_set_untracked` from Python — `set([1,2,3,{}])`, `frozenset([1,2,3,[]])`,
`set(generator_that_raises())`, and a `set` subclass — 2,000 iterations per run:
`reports/obj-mappings/repro/untracked_set_dealloc_twin.py`.
**9/9 runs clean, exit 0** (3/3 on `debug-gil-nojit`, 3/3 on `debug-ft-nojit`, 3/3 on
`release-gil-nojit`). Debug builds have the assert live, so this positively demonstrates that
the tolerant form absorbs a never-tracked object on a path Python reaches trivially — while
the identical shape in `dictiter_new` needs OOM injection to reach and aborts when it does.
The asymmetry is the finding.

**Fix:** one token at `dictobject.c:5662`, `_PyObject_GC_UNTRACK(di)` →
`PyObject_GC_UnTrack(di)`. That is CPY-0015's fix as well; nothing further is owed here.

---

### F2 — `Objects/dictobject.c:2788` `_PyDict_LoadBuiltinsFromGlobals` — `PyEval_GetBuiltins` — CONSIDER

```c
return Py_NewRef(PyEval_GetBuiltins());
```

**Genuinely deprecated at this ref?** Yes, `soft` tier. `Doc/c-api/reflection.rst:10` carries
`.. deprecated:: 3.13` / *"Use PyEval_GetFrameBuiltins instead."* There is **no compiler
marker**: `Include/ceval.h:20` is a plain `PyAPI_FUNC`, so no build warns about this line.
No removal date.

**Is the replacement semantically identical?** The scanner attaches this caveat:

> *"NOT a drop-in rename: the replacement returns a STRONG reference (Py_XNewRef) where this
> returns a BORROWED one, so a bare rename leaks the referent at every call site."*

That caveat is correct in general and **inverted at this particular call site**, which is
worth stating plainly because it is the difference between a leak and a simplification.
Reading the two implementations:

- `PyEval_GetBuiltins` (`Python/ceval.c:2633`) = `_PyEval_GetBuiltins(tstate)` — borrowed.
- `PyEval_GetFrameBuiltins` (`Python/ceval.c:2878`) = `Py_XNewRef(_PyEval_GetBuiltins(tstate))`.

The call site already wraps the result in `Py_NewRef`. So
`Py_NewRef(PyEval_GetBuiltins())` and `PyEval_GetFrameBuiltins()` differ by exactly one
character — `Py_NewRef` vs `Py_XNewRef`. The correct migration is therefore **not** a rename
plus a compensating `Py_DECREF`; it is

```c
return PyEval_GetFrameBuiltins();
```

which is behaviour-preserving *and strictly more defensive*, because `Py_XNewRef` tolerates
the NULL that `Py_NewRef` would dereference. Not reachable today — `_PyEval_GetBuiltins`
falls back to `tstate->interp->builtins`, which is non-NULL for any running interpreter — but
the swap removes the latent `Py_NewRef(NULL)` (the CPY-0030 shape) for free.

**Difficulty:** trivial, one line, net −1 token.

---

### F3 — `Objects/setobject.c:527` `set_table_resize` — `PyMem_NEW` — CONSIDER

```c
newtable = PyMem_NEW(setentry, newsize);
if (newtable == NULL) {
    PyErr_NoMemory();
    return -1;
}
```

**Genuinely deprecated at this ref?** Yes, `soft` tier — `Doc/c-api/memory.rst:300-316`,
under the heading *"Deprecated aliases … These are soft deprecated aliases to existing
functions and macros. They exist solely for backwards compatibility."* No compiler marker,
no removal date.

**Is the replacement semantically identical?** Yes, verifiably, and this is the strongest
"drop-in" claim in the report: `Include/pymem.h:82` is literally

```c
#define PyMem_NEW(type, n)        PyMem_New(type, (n))
```

The alias expands to the replacement with an extra pair of parentheses. There is no
behavioural delta at all — same `PY_SSIZE_T_MAX / sizeof(type)` overflow guard
(`pymem.h:63`), same non-raising contract. The surrounding `PyErr_NoMemory()` is already
correct (`PyMem_New` does **not** raise — see the briefing's allocator section, which
explicitly corrects an earlier draft that exempted it).

**Difficulty:** trivial. `PyMem_NEW` → `PyMem_New`, one occurrence, no other edit.

---

### F4 — `Objects/dictobject.c:2691` and `:3401` — **definitions of two `Py_DEPRECATED(3.14)` APIs scheduled for removal in 3.18** — POLICY (becomes FIX at 3.18)

Not call sites, so correctly absent from the scanner's finding list — but they are the only
`hard`-tier vocabulary hits in the slice and they are *work this slice owns*.

| symbol | marker | pending removal | replacement |
|---|---|---|---|
| `_PyDict_GetItemStringWithError` (`dictobject.c:2691`) | `Py_DEPRECATED(3.14)` @ `Include/cpython/dictobject.h:48` | **3.18** (`Doc/deprecations/c-api-pending-removal-in-3.18.rst`) | `PyDict_GetItemStringRef` |
| `_PyDict_Pop` (`dictobject.c:3401`) | `Py_DEPRECATED(3.14)` @ `Include/cpython/dictobject.h:73` | **3.18** (same file) | `PyDict_Pop` |

Neither replacement is a mechanical swap for a *consumer*: `_PyDict_GetItemStringWithError`
returns a **borrowed** reference (it forwards `PyDict_GetItemWithError`), while
`PyDict_GetItemStringRef` returns a **strong** reference through an out-parameter and an
`int` status; `_PyDict_Pop` returns the popped object or the default, while `PyDict_Pop`
returns `1/0/-1` with an out-parameter. Nothing inside this slice calls either — the bodies
exist purely as the deprecated shims, which is ACCEPTABLE today. The action owed is
deletion at 3.18, not migration now. Flagged so the 3.18 removal PR does not have to
rediscover them.

---

### F5 — `Objects/dictobject.c:7280` `_PyDict_NewKeysForClass` — internal use of the exception-discarding `PyDict_GetItem` — CONSIDER (reproduced, user-visible, not memory-unsafe)

This is the deliverable-3 headline: the one place in the slice where CPython's own code
calls an API that discards an exception it did not raise.

```c
if (cls->ht_type.tp_dict) {
    PyObject *attrs = PyDict_GetItem(cls->ht_type.tp_dict, &_Py_ID(__static_attributes__));
    if (attrs != NULL && PyTuple_Check(attrs)) {
        for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(attrs); i++) {
            PyObject *key = PyTuple_GET_ITEM(attrs, i);
            ...
            if (insert_split_key(keys, key, hash) == DKIX_EMPTY) break;
```

**Three corrections to the naive framing, all load-bearing:**

1. **`PyDict_GetItem` is not deprecated**, and reporting it as such would be wrong. The
   vocabulary excludes it deliberately (`deliberately_excluded`: *"NOT deprecated.
   `Include/dictobject.h:22` is a plain `PyAPI_FUNC` … Discouraged != deprecated"*). It is
   *discouraged*, in this very file, by a 10-line comment at `:2415-2424` explaining the
   historical error suppression.

2. **At this ref it no longer *silently* swallows.** `dict_getitem` (`:2425`, the shared
   implementation) saves the ambient exception with `_PyErr_GetRaisedException`, performs the
   lookup, and then — for any pending exception that is **not** a `KeyError` — calls
   `PyErr_FormatUnraisable(warnmsg)` before restoring the saved one. So the exception is
   *reported*, not discarded. Any report claiming this is a silent swallow at 3.16 is
   describing an older CPython.

3. **The remaining defect is that the report names the wrong culprit.** The warning text
   `PyDict_GetItem` passes is *"Exception ignored in PyDict_GetItem(); consider using
   PyDict_GetItemRef() or PyDict_GetItemWithError()"* — advice aimed at an extension author.
   Emitted from `:7280` it is advice CPython is giving to a user about **CPython's own code**.

**Reproduced, 9/9** (`reports/obj-mappings/repro/newkeysforclass_pydict_getitem.py`;
3/3 on `debug-gil-nojit`, 3/3 on `release-gil-nojit`, 3/3 on `debug-ft-nojit`). `type()` is
reached for every heap type with `Py_TPFLAGS_MANAGED_DICT` via
`Objects/typeobject.c:9480 type_ready_managed_dict`. `type('C', (), ns)` copies `ns` verbatim
into `tp_dict`, so `tp_dict` can be a `DICT_KEYS_GENERAL` dict holding a non-`str` key; a key
whose `__hash__` returns `hash('__static_attributes__')` puts a user `__eq__` in the probe
sequence. Creating a perfectly ordinary class then prints, to stderr:

```
Exception ignored in PyDict_GetItem(); consider using PyDict_GetItemRef() or PyDict_GetItemWithError():
Traceback (most recent call last):
  ...
RuntimeError: __eq__ from a class-body key
```

and the class is still created (exit 0), with `__static_attributes__` silently not used to
pre-seed the shared keys — a lost optimisation nobody can observe. The `__eq__` fires exactly
**1** time per creation, confirming the probe is the only trigger.

**Guarded twin:** `_PyDict_GetItemWithError` (`:2680`), 100 lines above in the same file, is
the non-suppressing variant, and the file's own comment at `:2647` says *"Variant of
PyDict_GetItem() that doesn't suppress exceptions."* The mechanical fix is
`PyDict_GetItemRef(cls->ht_type.tp_dict, &_Py_ID(__static_attributes__), &attrs)` with a
`< 0` branch and a `Py_XDECREF(attrs)` after the loop — which also **pins the borrow**
(below).

**On the borrowed `attrs` across `insert_split_key` — checked, and safe today, but only by
an invariant nobody states.** `attrs` is borrowed out of `tp_dict` and read via
`PyTuple_GET_ITEM` on every loop iteration, across `insert_split_key` — which is CPY-0096's
exact site, the one that calls `_PyType_Modified_Unlocked` under the non-reentrant
`DONT_DETACH` keys mutex. `_PyType_Modified_Unlocked` (`typeobject.c:1195-1238`) runs Python
only through (a) a type-watcher callback and (b) `PyErr_FormatUnraisable`. On *this* path the
type is mid-`type_ready`: `tp_watched == 0` and `tp_subclasses == NULL`, so neither fires —
consistent with the measured "exactly 1 `__eq__` invocation". So there is **no UAF here**, and
I am not reporting one. What there is: an unpinned borrowed reference held across the single
most re-entrancy-prone call in the file, protected by nothing but the freshness of the type,
with no comment saying so. Migrating to `PyDict_GetItemRef` removes the question entirely at
zero cost. That is the whole argument for the change — not the deprecation, which does not
exist.

---

### F6 — `Objects/dictobject.c:7601-7628` — dead `#if 0` block, **provably rotted** — CONSIDER (delete)

The slice's only never-compiled code, and the answer to deliverable 4's spirit even though it
is not a version guard.

```c
/* Sanity check for managed dicts */
#if 0
#define CHECK(val) assert(val); if (!(val)) { return 0; }

int
_PyObject_ManagedDictValidityCheck(PyObject *obj)
{
    ...
        int size = ((uint8_t *)values)[-2];
```

- **Zero references tree-wide.** `grep -rn _PyObject_ManagedDictValidityCheck --include=*.c
  --include=*.h .` returns exactly one hit: the definition itself, inside the `#if 0`. No
  declaration in any header, no call site, no test.
- **It would not do what it says if enabled.** `((uint8_t *)values)[-2]` was correct in the
  2022 layout — `git blame` puts the line at `de388c0a7b7` (2022-08-01), when
  `struct _dictvalues` was `{ PyObject *values[1]; }` with the prefix comment *"[-1] = prefix
  size. [-2] = used size."*. `c32dc47aca6` (2024-04-02, GH-115776) moved the size into a
  **leading** field — `struct _dictvalues { uint8_t capacity; uint8_t size; uint8_t embedded;
  uint8_t valid; PyObject *values[1]; }` — so the used size is now `values->size`, at a
  *positive* offset.
- **And the rot is demonstrable, not inferred.** `git show c32dc47aca6 -- Objects/dictobject.c`
  shows that same commit editing the two lines immediately above
  (`PyDictOrValues` → `PyManagedDictPointer`) and leaving the `[-2]` read untouched. The block
  was mechanically updated without being read. Flipping it to `#if 1` today would read a byte
  from the owning object's header region and assert on garbage.

Recommendation: delete the block. If a managed-dict sanity check is wanted, it needs to be
rewritten against the current struct anyway, and the `#if 0` is actively misleading — it
looks like a working diagnostic one `#define` away.

---

## 3. Classes bounded — clean, with denominators

### 3a. `gc-untrack-macro-form` — 3 of 4 strict-macro sites are true negatives

I read every `_PyObject_GC_UNTRACK` in the slice. The rule fires on 1; the other 3 are clean
for individually-verified reasons, not by assumption:

| site | function | verdict | why |
|---|---|---|---|
| `dictobject.c:5662` | `dictiter_dealloc` | **FIX (F1 = CPY-0015)** | `dictiter_new:5646` frees before `_PyObject_GC_TRACK:5653` |
| `dictobject.c:6437` | `dictview_dealloc` | clean | `_PyDictView_New:6463-6480` has **no fallible step** between `PyObject_GC_New` and `_PyObject_GC_TRACK` — the only intervening statement is `dv->dv_dict = (PyDictObject *)Py_NewRef(dict)`. No pre-track free path exists |
| `setobject.c:1044` | `setiter_dealloc` | clean | `set_iter:1172-1184` likewise: `PyObject_GC_New`, four infallible field stores, `_PyObject_GC_TRACK`. No error path between them |
| `setobject.c:1427` | `_PyFrozenSet_MaybeUntrack` | clean | not a `tp_dealloc`. Both call sites (`make_new_frozenset:1443`, `PyFrozenSet_New:3043`) pass an object that `make_new_set` has just tracked, and each calls it once |

Per the toolkit's own precision note, I did **not** promote the macro form as wrong in
general — for `dictview` and `setiter` it is the correct, cheaper choice, because those types
are unconditionally tracked for their whole lifetime.

Conversely, all GC-tracked **dict** objects in the slice (12 `_PyObject_GC_TRACK` sites:
`:964`, `:3640`, `:4602`, `:4643`, `:5205`, `:5393`, `:5475`, `:6797`, `:8520`, `:8566`, …)
dealloc through `dict_dealloc:3661`, which already uses the tolerant function form. Even if
one of those constructors grows a pre-track free path, it cannot produce this bug.

### 3b. Exception-discarding APIs — the class the brief asked to prioritise

Grepped both files for the full set. Results, with real denominators:

| API | occurrences in slice | verdict |
|---|---|---|
| `PyDict_GetItem` (error-discarding form) | **1 call site** (`dictobject.c:7280`) + 4 definition/doc-comment lines | **F5** |
| `PyDict_GetItemString` | 0 call sites (definition only, `:5547`) | clean |
| `PyDict_SetItemString` / `PyDict_DelItemString` | 0 call sites (definitions only, `:5578` / `:5593`) | clean |
| `PyObject_HasAttr` / `PyObject_HasAttrString` | **0** | structural zero — neither file does attribute lookup |
| `PyMapping_HasKey` / `PyMapping_HasKeyString` | **0** | structural zero |
| `PySequence_In` | **0** | structural zero |
| `PyErr_Clear` | **6** (`dictobject.c:1336`, `:7269`; `setobject.c:2565`, `:2625`, `:2660`, `:2700`) | all ACCEPTABLE, see below |
| `PyErr_FormatUnraisable` / `PyErr_WriteUnraisable` | 5 (`dictobject.c:2435`, `:2461`, `:5552`, `:8013`, `:8314`) | deliberate, correct — reporting rather than discarding |

**All six `PyErr_Clear` sites are narrowed or exempt** — 6 of 6, nothing promoted:

- `dictobject.c:1336` — the taxonomy's own named exemplar of the *statically-known type slot*
  FP class (`PyUnicode_Type.tp_hash`, no user code runs). Confirmed, not re-litigated.
- `dictobject.c:7269` — `_PyDict_NewKeysForClass`'s `PyMem_Malloc` failure branch. This is
  the raw-allocator layer deliberately declining to raise; the function's own doc-comment two
  lines above says *"Returns NULL if cannot allocate a new PyDictKeysObject, but does not set
  an error"*, and the caller (`typeobject.c:9481`) raises `PyErr_NoMemory()` itself. Obligation
  discharged at the call site — the "obligation deferred to the caller" class.
- `setobject.c:2565`, `:2625`, `:2660`, `:2700` — all four are the *early-return guard clause*
  class: `if (!PySet_Check(key) || !PyErr_ExceptionMatches(PyExc_TypeError)) { ...; return ...; }`
  immediately precedes the clear, so it is fully dominated by a two-part narrowing. This is
  the documented "set allows membership testing with a set key" carve-out.

  One honest caveat, worth exactly one line: the narrowing is `PySet_Check` (subclass-permissive)
  + `TypeError`, so a `set` **subclass** whose `__hash__` deliberately raises `TypeError` has
  that exception replaced by the content hash, while the same subclass raising `ValueError`
  propagates. An inconsistency, not a bug — no memory-safety or exception-loss consequence
  beyond the intended carve-out. Not promoted.

### 3c. Deliverable 4 — version-guarded dead code: **structural zero**

`PY_VERSION_HEX`: 0. `Py_LIMITED_API`: 0. `PYTHON_API_VERSION`: 0. Across **71** preprocessor
conditionals in the slice.

This zero is **structural, not evidential**, and I want that on the record rather than
banked as a clean result: `Objects/dictobject.c` and `Objects/setobject.c` are core
interpreter object implementations, compiled exactly once as part of the interpreter they
belong to. They can never be built against a different CPython version, so a
`#if PY_VERSION_HEX < ...` guard *cannot* appear. The rule cannot fire here. Reporting "0
version-guarded dead blocks — clean" without that sentence would be one of the four
zero-denominator certifications the brief warns about.

The 71 conditionals break down as: 48 `Py_GIL_DISABLED`, 5 `Py_REF_DEBUG`, 4 `NDEBUG`,
3 `Py_DEBUG`, 3 `SIZEOF_VOID_P > 4`, 2 `!Py_GIL_DISABLED`, 2 `DEBUG_PYDICT`
(opt-in, `// #define DEBUG_PYDICT` commented out at `dictobject.c:663` — deliberate),
1 `LINEAR_PROBES` (`#ifndef` with an in-file default at `setobject.c:213`), 1 `Py_STATS`,
1 `SIZEOF_LONG == SIZEOF_SIZE_T`, and **1 `#if 0`** — the only genuinely never-compiled block,
reported as F6.

### 3d. Deliverable 3 — legacy internal APIs: **structural zero**

- `Py_UNICODE` / `PY_UNICODE_TYPE`: **0 occurrences** in either file. (Both are `hard`-tier,
  removal in **3.16** — i.e. this release — so a hit would have been urgent. There is none.)
- Old buffer protocol (`PyObject_AsCharBuffer`, `PyObject_AsReadBuffer`,
  `PyObject_AsWriteBuffer`, `bf_getreadbuffer`, `getwritebuffer`, `getsegcount`,
  `getcharbuffer`): **0 occurrences**. These were removed in 3.13; no call site can exist,
  and the vocabulary excludes them for that reason.
- `PyUnicode_READY` / `PyUnicode_IS_READY`, `PyErr_Fetch` / `PyErr_Restore` /
  `PyErr_NormalizeException`, `PyModule_AddObject`, the `_PyUnicodeWriter_*` family
  (~75 sites elsewhere in `Objects/` + `Modules/`, all removal-in-3.18): **0 in this slice**.

Both files use the modern spellings throughout — `PyErr_GetRaisedException` /
`PyErr_SetRaisedException` at `dictobject.c:2449/:2464` and `:2478/:2483`, `_Py_ID(...)`
rather than `_PyUnicode_FromId`, `PyDict_GetItemRef` / `PyDict_Pop` as the public entry
points. This is a genuinely modernised pair of files.

---

## 4. Toolkit feedback

### 4a. Precision: 3/3 findings are true positives (1 FIX, 2 CONSIDER). No false positives.

Both `deprecated-api` hits are real, correctly tiered, correctly severity-ranked (no removal
date → CONSIDER), and correctly *not* accompanied by the definition sites at `:2691` / `:3401`
— the definition-site suppression that the vocabulary's `_meta` says was built to fix the old
scanner works, and it worked on the two `hard`-tier symbols where it mattered most.
`gc-untrack-macro-form` fired 1/4 with the three correct suppressions. On this slice the
scanner is at **100% precision on an earned denominator**.

### 4b. Recall gap 1 (highest-value output): the `drop_in`/`caveat` fields are computed
per-**API**, but drop-in-ness is a property of the **call site**.

F2 is the demonstration. `PyEval_GetBuiltins` ships `drop_in: false` with a caveat warning
that a bare rename **leaks at every call site**. At `dictobject.c:2788` the opposite is true:
the call is already `Py_NewRef(PyEval_GetBuiltins())`, so the rename *removes* a wrapper and
is strictly safer. A maintainer who trusts the emitted caveat will believe this site needs a
compensating `Py_DECREF` and will introduce a refcount bug by adding one.

The vocabulary's own `drop_in_rule` note says this class of error *"is the second time this
class of error reached a recommendation"*. It is now arguably the third, in the other
direction: the field is right about the API and wrong about the site.

**Concrete proposal.** For the borrowed→strong family (`PyEval_GetBuiltins` /
`GetGlobals` / `GetLocals`, `_PyDict_GetItemStringWithError`), add a call-site gate: if the
matched call is *immediately* wrapped in `Py_NewRef(...)` / `Py_XNewRef(...)` on the same
line, emit a **different** detail — "`drop_in` at this site: replace
`Py_NewRef(API(...))` with `Replacement(...)` and delete the wrapper" — and downgrade the
caveat. This is a one-line lookbehind on the captured `code` string, which the finding
already carries. Suggested new field: `site_drop_in: true|false|null` alongside `drop_in`.

### 4c. Recall gap 2: the guarded twin for `gc-untrack-macro-form` is computable and is not emitted.

The finding names the replacement (`PyObject_GC_UnTrack`) but not the *in-tree exemplar*. In
this slice the exemplar is 3,000 lines away in the sibling file, has the **verbatim identical
comment**, and has the **verbatim identical shape** (constructor frees pre-track, dealloc
untracks) — `setobject.c:742 set_dealloc` / `make_new_set_untracked:1372`. That pairing is
the single most persuasive artifact in this report and I found it by reading, not from the
scanner.

**Concrete proposal.** The scanner already computes, per file, the set of macro-form and
function-form untrack sites (it must, to fire the rule). Emit both, plus a `guarded_twin`
field naming any function-form `tp_dealloc` **whose own constructor also has a pre-track free
path** — i.e. the same analysis already run for the positive, run for the negatives too. That
turns "use the tolerant form" into "use the tolerant form, like `set_dealloc:742` does for the
identical shape", which is what makes a maintainer act.

### 4d. Recall gap 3: `PyDict_GetItem` is correctly excluded from the deprecation vocabulary
and consequently invisible, but its *internal* call sites are a real bug class.

F5 is the only site in the slice and I found it by hand-grepping the brief's list, not from
any scanner. The distinction the vocabulary draws — "discouraged ≠ deprecated" — is right,
and I am **not** proposing it be added to `deprecated_c_apis.json`; that would recreate the
0/13 problem. The right home is a separate, much narrower rule.

**Concrete proposal.** New rule `internal-use-of-discouraged-api`, in
`scan_deprecated_apis.py` but with its own finding `type` and its own denominator, seeded with
the small set of APIs whose *own doc or implementation* tells callers to prefer something
else: `PyDict_GetItem`, `PyDict_GetItemString`, `PyMapping_HasKey`, `PyMapping_HasKeyString`,
`PyObject_HasAttr`, `PyObject_HasAttrString`. Severity CONSIDER, never FIX. The
discriminator that makes it non-noisy is already available: report only when the call appears
in `Objects/` / `Python/` / `Modules/` — CPython's own code — and suppress the API's own
definition site, exactly as the existing rule does. Tree-wide this should be a handful of
sites, and each one is a place where CPython emits advice to users about CPython.

The self-referential quality is what makes it worth having: `dictobject.c:7280` calls the
function whose implementation 4,800 lines above prints *"consider using PyDict_GetItemRef()"*.

### 4e. Denominator reporting: the note is correct but the number it points at is not useful.

`"The denominator is files_analyzed x apis_in_vocabulary"` yields 186 for this slice, which is
a constant times the file count and carries no information about the code. The number a reader
actually needs — *how much C-API surface did you search, and how much of it was deprecated* —
is 1,876 call sites → 2 hits.

**Concrete proposal.** Add `denominators.capi_call_sites` (count of `Py*`/`_Py*` call-shaped
tokens in the scanned files) and `denominators.vocabulary_occurrences` (raw textual matches,
before definition-site suppression). Both are one regex pass over source the scanner has
already read. With `suppressed_compat_shim` (already present, 0 here) that gives the full
funnel: 1,876 call sites → 4 textual matches → 2 definition-site suppressions → 2 reported.
Every agent that quotes this scanner currently has to reconstruct that funnel by hand, as I
did.

### 4f. One rule the scanner does not have and should: `dead-preprocessor-block`.

F6 was found by inventorying all 71 conditionals, not by any rule. `#if 0` blocks that
(a) define a symbol referenced nowhere in the tree and (b) were last *edited* by a commit that
also changed the layout they depend on, are cheap to detect and reliably rotten. This is
adjacent to the agent's stated "stale compatibility code" remit and currently has no
mechanical support.

---

## 5. Noticed outside slice

- `Include/internal/pycore_dict.h:258-262` — the `/* Layout of dict values: … [-1] = prefix
  size. [-2] = used size. */` comment above `struct _dictvalues` is stale for the same reason
  as F6: `c32dc47aca6` (2024) moved those fields to leading struct members and left the
  comment describing the 2022 negative-offset layout. Two readers have now been misled by it
  (the `#if 0` block, and this comment's own survival). Belongs to whichever slice owns
  `Include/internal/`.
- `Python/ceval.c:2878` — `PyEval_GetFrameBuiltins` is `Py_XNewRef(_PyEval_GetBuiltins(tstate))`
  while the deprecated `PyEval_GetBuiltins` is the bare call. Any tree-wide migration of the
  PEP 667 family should be driven by the *call site's* existing `Py_NewRef` wrapper, per §4b;
  there are more of these outside this slice.
- `Objects/typeobject.c:9480` `type_ready_managed_dict` — the caller that makes F5 reachable.
  It is also the point at which arbitrary user `__eq__` can execute during `type_ready`
  (measured, 9/9), on a type that is half-built. That re-entrancy surface is
  `obj-typeobject`'s, not mine; noting only that this slice supplies the trigger.
