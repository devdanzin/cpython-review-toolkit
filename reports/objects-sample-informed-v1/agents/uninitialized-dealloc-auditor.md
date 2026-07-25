# uninitialized-dealloc-auditor — Objects/ sample (informed)

Shape owned: **`dealloc-of-uninitialized-object`** (briefing §3).
Target: `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0, full clone).
Repro interpreter: `./python` @ `583cb03da83` — `--with-pydebug --with-address-sanitizer --without-pymalloc`, clang-21.

## Scanner volume

| scope | raw candidates | confirmed | dismissed | precision |
|---|---|---|---|---|
| 14-file sample | **2** | **2** | 0 | **100%** |
| all of `Objects/` (context) | 7 | 3 | 4 | 43% |

Hand-reading added **2 findings the scanner did not surface** (1 FIX + 1 CONSIDER),
one of which is inside `Objects/` but outside the 14-file sample.

---

## Findings

### FIX

#### 1. `Objects/odictobject.c:1945` · `odictiter_new` · **NEW · REPRODUCED**

```c
di = PyObject_GC_New(odictiterobject, &PyODictIter_Type);   /* 1945 — non-zeroing */
if (di == NULL)
    return NULL;

if ((kind & _odict_ITER_ITEMS) == _odict_ITER_ITEMS) {
    di->di_result = _PyTuple_FromPairSteal(Py_None, Py_None);   /* 1950 — fallible */
    if (di->di_result == NULL) {
        Py_DECREF(di);                                          /* 1952 — HALF-BUILT */
        return NULL;
    }
}
...
di->kind = kind;                                                /* 1960 — too late */
di->di_current = ...;                                           /* 1962 */
di->di_odict  = (PyODictObject*)Py_NewRef(od);                  /* 1965 */
_PyObject_GC_TRACK(di);                                         /* 1967 */
```

**What breaks.** At line 1952 the object is untracked and only `di_result` is set
(to NULL). `odictiter_dealloc` (1715) then does, in order:

1. `_PyObject_GC_UNTRACK(di)` (**1718**) — the *unchecked* internal variant. `di` was
   never tracked; `gc_alloc()` leaves `_gc_next = _gc_prev = 0` (`Python/gc.c:2017`,
   `_PyObject_GC_Link` at `gc.c:1982`), so `_PyGCHead_PREV(gc)` is `NULL` and
   `_PyGCHead_SET_NEXT(NULL, next)` is a **NULL store**. Debug build: assertion abort.
2. `Py_XDECREF(di->di_odict)` — **garbage pointer**, wild decref.
3. `Py_XDECREF(di->di_current)` — **garbage pointer**, wild decref.
4. `if (di->kind & _odict_ITER_ITEMS) Py_DECREF(di->di_result)` — `kind` is garbage,
   `di_result` is NULL → potential `Py_DECREF(NULL)`.

**FP classes ruled out.** (a) *zeroing allocator* — no: `PyObject_GC_New` →
`_PyObject_GC_New` → `gc_alloc`, which zeroes only the 2-word GC pre-header
(`Python/gc.c:2017-2018`, `Python/gc_free_threading.c:2733-2736`), never the object
body; there is no `memset` and no wrapper. (b) *`Py_XDECREF`-guarded dealloc* — the
dealloc *is* `Py_XDECREF`-guarded, but the members are **garbage, not NULL**, at the
free point, which is precisely the carve-out the taxonomy names. Step 1 crashes before
either guard even matters.

**Guarded twin.** `Objects/dictobject.c:5617 dictiter_new` — the function `odictiter_new`
was copy-pasted from. It performs the **same fallible call**
(`_PyTuple_FromPairSteal(Py_None, Py_None)`) but places it **last**, after `di_dict`,
`di_used`, `len` and `di_pos` are all assigned; so its `Py_DECREF(di)` frees a fully
initialized object. The odict copy simply inverted the order.

**Python reachability.** `iter(collections.OrderedDict(...).items())` →
`odictitems_iter` → `odictiter_new(od, _odict_ITER_ITEMS)`. OOM-only trigger (class O5).

**Reproduced** (targeted sweep: setup unarmed, then `set_nomemory(K)`, `K=0..39`):

```
K=1  →  Objects/odictobject.c:1702: _PyObject_GC_UNTRACK:
        Assertion "_PyObject_GC_IS_TRACKED(((PyObject*)(op)))" failed:
        object not tracked by the garbage collector
        object refcount : 0 ; object type name: odict_iterator
        Fatal Python error: _PyObject_AssertFailed
```

(line 1702 in the built revision = line 1718 at HEAD). Outcomes over K=0..39:
`{memory_error: 3, ABORT: 1, completed: 36}` — a one-allocation-wide crash window,
exactly the pattern the OOM-sweep doc warns about.

**Fix.** Move the `di_result` block to the end of the function (become `dictiter_new`),
or NULL `di_odict`/`di_current` and set `kind` immediately after line 1946.

---

#### 2. `Objects/listobject.c:250` · `PyList_New` · **NEW · free-threaded build only · SCANNER MISS**

Outside the 14-file sample, inside `Objects/`. Surfaced by hand + by the rule change
proposed below; the shipping scanner does **not** report it.

```c
PyListObject *op = _Py_FREELIST_POP(PyListObject, lists);
if (op == NULL) {
    op = PyObject_GC_New(PyListObject, &PyList_Type);   /* 250 — non-zeroing */
    if (op == NULL) return NULL;
}
if (size <= 0) {
    op->ob_item = NULL;                                 /* 255 — the OTHER branch */
}
else {
#ifdef Py_GIL_DISABLED
    _PyListArray *array = list_allocate_array(size);    /* 260 — fallible */
    if (array == NULL) {
        Py_DECREF(op);                                  /* 262 — HALF-BUILT */
        return PyErr_NoMemory();
    }
    memset(&array->ob_item, 0, size * sizeof(PyObject *));
    op->ob_item = array->ob_item;                       /* 266 — too late */
#else
    op->ob_item = (PyObject **) PyMem_Calloc(...);      /* 268 — assigned first */
#endif
    if (op->ob_item == NULL) { Py_DECREF(op); ... }     /* 271 */
}
Py_SET_SIZE(op, size);                                  /* 275 */
op->allocated = size;                                   /* 276 */
```

**What breaks.** On the `Py_DECREF(op)` at **262** (free-threaded build only), both
`op->ob_item` **and** `Py_SIZE(op)` are uninitialized — `PyObject_GC_New` (not `NewVar`)
never sets `ob_size`. `list_dealloc` (`listobject.c:555`) then does:

```c
if (op->ob_item != NULL) {          /* garbage, likely non-NULL */
    i = Py_SIZE(op);                /* garbage count */
    while (--i >= 0) Py_XDECREF(op->ob_item[i]);   /* wild decrefs */
    free_list_items(op->ob_item, false);           /* PyMem_Free(garbage) */
}
```

**Why it is FT-only.** The `#else` (GIL) branch assigns `op->ob_item` *before* its NULL
check, so its `Py_DECREF(op)` at 271 always sees `ob_item == NULL` and the loop is
skipped. The `_Py_FREELIST_POP` path is also safe — `list_dealloc:570` sets
`op->ob_item = NULL` before pushing to the freelist. **Only** the freelist-miss +
`Py_GIL_DISABLED` combination is exposed — which is exactly the combination you get
under memory pressure.

**FP classes ruled out.** (a) not a zeroing allocator (`PyObject_GC_New`, as above; no
`memset` of `op` — the `memset` at 265 targets `array`, not `op`). (b) `list_dealloc`
*is* `Py_XDECREF`-guarded per element, but it gates on `ob_item != NULL` and `ob_item`
is **garbage, not NULL**, at line 262.

**Guarded twin.** `Objects/listobject.c:281 list_new_prealloc`, 30 lines below — the
same `#ifdef Py_GIL_DISABLED` / `list_allocate_array` / `Py_DECREF(op)` sequence, but
`op` comes from `PyList_New(0)` (fully initialized) and the invariant is even asserted:
`assert(op->ob_item == NULL);` before the fallible call.

**Python reachability.** `list_allocate_array` returns NULL both on malloc failure and
on `capacity > PY_SSIZE_T_MAX/sizeof(PyObject*) - 1`, with no exception set. Direct
Python-controlled path: `Python/marshal.c:1448`, `v = PyList_New(n)` where `n` is read
from the stream and bounded only by `SIZE32_MAX` — `marshal.loads()` of a crafted
`TYPE_LIST` with a ~4e9 length asks for a 32 GB array, fails, and takes line 262.
Plus every OOM path through `PyList_New(n>0)`.

**Not reproduced here** — the local build is `Py_GIL_DISABLED = 0`. Needs a
`--disable-gil` build; this is static-only evidence, but the control flow is
unambiguous.

**Fix.** Hoist `op->ob_item = NULL;` (and `Py_SET_SIZE(op, 0)`) to immediately after the
allocation, before the `if (size <= 0)`.

---

#### 3. `Objects/templateobject.c:225` · `template_iter` · gh-151815 — **confirm only**

**Still present at `4f3be1b5777`, unchanged**: `PyObject_GC_New` at 225, `Py_DECREF(iter)`
at 232 and 238 on `PyObject_GetIter` failure, members assigned only at 243-245; no fix
commit touches it (`git log --grep=151815` → empty; last relevant commit is
`785268fdceb`, unrelated). `templateiter_dealloc` → `templateiter_clear` →
`Py_CLEAR(self->stringsiter)` / `Py_CLEAR(self->interpolationsiter)` on garbage.

**On the v0.7 "60/60 clean MemoryError" result** — that is *not* evidence it is fixed on
main; the source says otherwise. My own sweep also failed to reproduce it (K=0..29, with
and without `PYTHONMALLOC=debug`, tuple-iterator freelist drained). Mechanistic reason:
this shape only crashes when the recycled allocator block is *dirty*, and the only type
that recycles a `templateiterobject`-sized block is `templateiterobject` itself — whose
`templateiter_clear` NULLs both members before `tp_free`. So the block almost always
comes back with NULL in exactly those two slots. Contrast finding #1, which crashes on
`_PyObject_GC_UNTRACK` **before** ever reading a member, and therefore reproduces
deterministically. **Classification unchanged: FIX, latent.** Recommend recording the
non-reproduction as "unstable trigger", not as "fixed".

### CONSIDER

#### 4. `Objects/structseq.c:700` · `PyStructSequence_InitType2` (and `:667` `_PyStructSequence_InitBuiltinWithFlags`)

Adjacent to my shape (half-built object left behind rather than freed), found during the
structseq hunt; not recorded anywhere I can see.

```c
initialize_static_fields(type, desc, members, n_members, 0);   /* 698 — sets type->tp_members = members (:592) */
if (initialize_static_type(type, desc, n_members, n_unnamed_members) < 0) {
    PyMem_Free(members);                                       /* 700 */
    return -1;
}
```

`initialize_static_type` calls `PyType_Ready(type)` **first** and only then the fallible
`initialize_structseq_dict`. `PyType_Ready` → `type_add_members` (`typeobject.c:8693`) →
`PyDescr_NewMember` (`descrobject.c:998`), which stores `descr->d_member = member` — a
raw pointer *into* the `members` array — in a descriptor placed in the type dict. So when
`initialize_structseq_dict` fails (OOM: `PyLong_FromSsize_t` / `PyTuple_New` /
`PyDict_SetItemString`), line 700 frees an array that a READY type and all of its member
descriptors still point at. Any subsequent `t.field` access is a use-after-free.
`PyStructSequence_InitType` (`:704`) returns `void`, so a third-party caller cannot even
observe the failure.

**Guarded twin.** `_PyStructSequence_NewType` (`structseq.c:781`) — frees `members` only
*after* `PyType_FromSpecWithBases`, which **memcpy**s them into the heap type
(`typeobject.c:5678-5694`, "Move the slots to the heap type itself"), and whose error path
uses `Py_DECREF(type)` rather than freeing an array the type still references.

**Fix.** On the failure path either leak the array deliberately (static types keep
`tp_members` forever anyway) or reset `type->tp_members = NULL` and clear the type dict
before freeing. CONSIDER rather than FIX because it is OOM-only, init-time, and needs a
maintainer call on the intended contract for a failed `PyStructSequence_InitType`.

### POLICY / ACCEPTABLE (one line each)

- `Objects/bytearrayobject.c:164` `PyByteArray_FromStringAndSize` — **scanner FP**: an
  explicit `new->ob_exports = 0;` with a comment naming this exact hazard; `ob_bytes_object`
  is assigned by the failing call itself. Safe. (FP class (b), with `= 0` not `= NULL`.)
- `Objects/typeobject.c:11343` `slot_bf_getbuffer` — **scanner FP**: `Py_XDECREF(wrapper)`
  is on the shared `fail:` label, reachable with a non-NULL `wrapper` only *after* both
  members are set; no fallible call sits between the allocation and the writes.
- `Objects/codeobject.c:736,738` `_PyCode_New`, `Objects/stringlib/unicode_format.h:1254`
  `formatter_field_name_split` — scanner FPs of the same kind (no member write after the free).
- `Objects/interpolationobject.c:191` `_PyInterpolation_Build` — safe, and a **model
  citizen**: `interpolation->conversion = NULL;` is written explicitly before the `switch`
  whose `default:` arm does `Py_DECREF(interpolation)`.
- `Objects/funcobject.c:123,202` — safe by design; `PyFunction_NewWithQualName` carries the
  invariant in a comment: *"No failures from this point on, since func_dealloc() does not
  expect a partially-created object."* All fallible work precedes the allocation.
- `Objects/weakrefobject.c:402` `allocate_weakref` — FP class (a): `type->tp_alloc(type, 0)`
  = `PyType_GenericAlloc`, zeroing.
- `Objects/structseq.c:77` `PyStructSequence_New` — safe, see the verdict below.
- `Objects/rangeobject.c:1352` `range_reverse` (`it->start = it->step = NULL;` first),
  `Objects/typevarobject.c:649,1293,1579` (`*_alloc`, sole fallible step is last),
  `Objects/listobject.c:281` `list_new_prealloc` — all safe.
- `Objects/templateobject.c:407`, `lazyimportobject.c:29`, `capsule.c:69`, `iterobject.c:26,196,533`,
  `cellobject.c:15`, `descrobject.c:1285,1512`, `genericaliasobject.c:1019`, `unionobject.c:582`,
  `tupleobject.c:57,1262` — no fallible call between allocation and member init. Silent negatives.

---

## New siblings of known shapes

The fix-propagation yield for `dealloc-of-uninitialized-object`:

| # | site | relation to the known instance | status |
|---|---|---|---|
| 1 | `odictobject.c:1945 odictiter_new` | direct sibling of `dictobject.c:5617 dictiter_new` (same fallible call, inverted order) | **reproduced (ABORT)** |
| 2 | `listobject.c:250 PyList_New` (FT) | sibling of `listobject.c:281 list_new_prealloc` | static, FT-only |
| 3 | `structseq.c:700 InitType2` | sibling of `structseq.c:781 _PyStructSequence_NewType` | static, CONSIDER |

The generative rule behind all three: **a constructor whose first fallible step is not the
allocation itself.** `dictiter_new`, `list_new_prealloc`, `_PyStructSequence_NewType`,
`PyFunction_NewWithQualName` and `_PyInterpolation_Build` all order the code so that either
(i) everything fallible precedes the allocation, or (ii) every member the destructor reads
is written before the first fallible call. The three findings each break that ordering in a
different way.

---

## `Objects/structseq.c` verdict — **clean for this shape**

`PyStructSequence_New` (`:65-86`) is not a candidate; it is **the guarded twin**:

```c
obj = PyObject_GC_NewVar(PyStructSequence, type, size);   /* size = REAL_SIZE_TP = n_fields */
if (obj == NULL) return NULL;
_PyTuple_RESET_HASH_CACHE(obj);       /* infallible */
Py_SET_SIZE(obj, vsize);              /* infallible */
for (i = 0; i < size; i++)
    obj->ob_item[i] = NULL;           /* ALL n_fields slots, before any fallible call */
return (PyObject*)obj;
```

Every one of `n_fields` slots is NULLed immediately, before anything fallible, and
`structseq_dealloc` (`:132`) walks `REAL_SIZE(obj)` slots with `Py_XDECREF` — so an early
free sees NULL, never garbage. Both consumers depend on that and are correct:

- `structseq_new_impl` (`:167`) — `PyDict_GetItemStringRef` failure at `:243` and the
  duplicate-field-name error at `:257` both `Py_DECREF(res)` with slots `[i, max_len)`
  still NULL. Safe.
- `structseq_replace` (`:389`) — `PyDict_PopString` failure → `goto error` → `Py_DECREF(result)`
  with the tail slots still NULL. Safe.

**Answer to the calibration question: no, the v0.5 tightening did not cost recall *here*.**
The scanner reports nothing in `structseq.c` for the correct reason — the raw allocator is
wrapped by a helper that NULL-inits, so the constructor call sites never match the
allocator regex at all. That is a *silent correct negative*, and it should be written into
the FP taxonomy so a future agent does not re-hunt this file (proposal #5).

Two structseq caveats that are **not** my shape and are logged for someone else:

- `PyStructSequence_InitType2` / `_PyStructSequence_InitBuiltinWithFlags` — finding #4 above.
- **NULL holes escaping to Python.** `structseq_repr` (`:274`) does
  `assert(value != NULL)` at `:324` then `PyUnicodeWriter_WriteRepr(writer, value)`, and
  `structseq_reduce` (`:343`) does `PyDict_SetItemString(dict, n, self->ob_item[i])` at
  `:370` — both crash on a slot a
  C consumer never filled. 45 `PyStructSequence_New` call sites tree-wide
  (posixmodule, timemodule, resource, sysmodule, signalmodule, _remote_debugging, …).
  I did **not** sweep them — out of scope for this shape and this run's file list. Recommend
  a dedicated pass; a debug build's `assert` in `structseq_repr` is a cheap tripwire.

---

## Classes bounded (clean negatives)

- **The 14-file sample is clean for `dealloc-of-uninitialized-object` except
  `odictobject.c` and `templateobject.c`.** Every non-zeroing allocation in the sample was
  read by hand (17 sites, listed under ACCEPTABLE); 15 have no fallible call between the
  allocation and the last member write.
- **`weakrefobject.c` is structurally immune** — it allocates through `tp_alloc`
  (`PyType_GenericAlloc`, zeroing) and `init_weakref` is infallible.
- **`funcobject.c` is immune by an explicit documented invariant** (comment at `:206`).
- **`interpolationobject.c` and `lazyimportobject.c` — the newest, least-reviewed types in
  the sample — are clean**, and `_PyInterpolation_Build` is a positive example of the fix.
- **`capsule.c` / `cellobject.c` (dormant per Phase 1) are clean** and were cheap to confirm.

---

## Toolkit assessment

### Precision

Sample precision was 2/2. On all of `Objects/` the shipping rule is **3/7 real** (43%);
the 4 FPs split into exactly two classes:

| FP class | instances | cause |
|---|---|---|
| **Member initialized to a non-NULL sentinel** | `bytearrayobject.c:164` | `_member_null_init_re` only matches `= NULL`, so an explicit `new->ob_exports = 0;` (guarding the exact hazard, with a comment saying so) does not count as initialization |
| **Free site not dominated by the allocation** | `typeobject.c:11343`, `codeobject.c:736/738`, `unicode_format.h:1254` | the `Py_XDECREF(var)` is on a shared `fail:` label reachable only *before* the allocation (var still NULL) or *after* all members are written; the scanner only asks "is there a free somewhere in the tail" |

### Recall gaps

**The `_member_null_init_re` gate is branch-insensitive, and one `= NULL` anywhere in the
pre-free text dismisses the whole function.** Evidence — `PyList_New`:

```c
if (size <= 0) {
    op->ob_item = NULL;          /* <- satisfies the gate ... */
}
else {
#ifdef Py_GIL_DISABLED
    _PyListArray *array = list_allocate_array(size);
    if (array == NULL) {
        Py_DECREF(op);           /* <- ... but this free is in the SIBLING branch */
```

The gate sees `op->ob_item = NULL` textually before `Py_DECREF(op)` and dismisses the
function, even though that assignment cannot execute on the path that reaches the free.
This is a real, Python-reachable (via `marshal.loads`) crash in the free-threaded build.

The blake2 exemplar (gh-152851) named in the scanner's own docstring would fall to the
same gate: a constructor that NULLs one pointer member but leaves an `impl` enum garbage
is dismissed on the first `= NULL`.

### Prompt issues

Minor, two of them:

1. The agent prompt says *"a bare `unsafe`…"* — no, that's the PyO3 sibling. Here the issue
   is that **Phase 2 ("verify the allocator really doesn't zero") is listed after Phase 1**,
   but the cheap decisive check is the *dominator* question, not the allocator question:
   in all 4 FPs the allocator genuinely does not zero, and the finding was still bogus. Suggest
   reordering to: (i) does a member write occur *after* the free? (ii) is that member a pointer
   the destructor reads? (iii) allocator/memset check last.
2. The prompt never mentions **`_PyObject_GC_UNTRACK` vs `PyObject_GC_UnTrack`**. The
   unchecked variant on a never-tracked object is a crash *before* any member is read, and
   it is what made finding #1 reproduce deterministically while gh-151815 (which uses the
   checked variant) does not. That distinction should be in the Phase-1 checklist — it is
   both a severity multiplier and a reproducibility predictor.

### Concrete tuning proposals (ranked)

**1. Replace the "any `= NULL` before the free" gate with "∃ a member written *after* the
free that is not also written before it".**
In `_check_function`, drop the `_member_null_init_re(var).search(pre_free)` early-`continue`
and use:

```python
mw = rf"\b{re.escape(var)}\s*->\s*(\w+)\s*="
before = set(re.findall(mw, tail[: free_m.start()]))
after  = set(re.findall(mw, tail[free_m.end():]))
unset_at_free = sorted(after - before)
if not unset_at_free:
    continue
```

Measured on `Objects/` (113 files, 4627 functions): **7 → 3 candidates, all 3 real** —
it drops all 4 FPs *and* gains `PyList_New`. Emit `unset_at_free` as a finding field so the
agent's Phase 1 starts from the exact member list. Regression-test with `template_iter`
(true positive), `bytearrayobject.c:164` (must not fire), `typeobject.c:11343` (must not fire).

**2. Make the "written before the free" set dominator-aware.**
Proposal 1 is still textual. Walk the AST: a member write counts as *before* the free only
if its statement is an ancestor-or-preceding-sibling of the free within a common enclosing
block — i.e. it dominates the free. This is what actually exposes `PyList_New`'s
`Py_GIL_DISABLED` branch, and it generalizes to every `if/else`-split constructor.
`tree_sitter_utils` already hands back `func["body_node"]`, so this is a node-walk, not
a new dependency.

**3. Filter `unset_at_free` to members the destructor treats as a pointer.**
Proposal 1 applied to `Modules/` yields 5 candidates, of which the two I spot-checked are
FPs *because the unset members are scalars*: `Modules/_sre/sre.c:2955 pattern_new_match`
(`pos`, `endpos`, `lastindex` are all `Py_ssize_t`) and
`Modules/_decimal/_decimal.c:1443 context_new` (`tstate`, `modstate`, neither touched by
`context_clear`). Cheap in-file check: keep a member only if the same file contains
`Py_(X)?(DECREF|CLEAR)\s*\(\s*\w+\s*->\s*MEMBER\b` or `Py_VISIT\(\s*\w+\s*->\s*MEMBER\b`.
Emit the matching destructor line in the finding so Phase 1 is pre-answered.

**4. Add `= 0` / `= -1` to the initializer forms** (`_member_null_init_re` → a general
`_member_init_re` matching `= NULL|0|-1`). Subsumed by proposal 1, but keep it if the
current gate is retained anywhere; `bytearrayobject.c:164` is the evidence.

**5. Two entries for `data/cpython_non_bugs.md`, under *Uninitialized dealloc*:**
   - *Wrapper constructor.* An allocation routed through a project-local helper that
     NULL-inits every slot — `PyStructSequence_New` (`Objects/structseq.c:65`),
     `PyTuple_New` (`Objects/tupleobject.c:75`), `PyList_New(0)` — is safe, and the scanner
     never sees the raw allocator. `Objects/structseq.c` is a *silent correct negative*, not
     an unexamined file; do not re-hunt it.
   - *Shared `fail:` label.* A `Py_XDECREF(var)` on an error label reached only before the
     allocation (var still NULL) or after all members are written is not a finding. Confirm
     the free is reachable from a branch between the allocation and the last member write.

**6. Doc line for the agent prompt.** "`_PyObject_GC_UNTRACK` (unchecked) in a `tp_dealloc`
turns this shape into a *deterministic* crash — it faults on a never-tracked object before
any member is read. `PyObject_GC_UnTrack` (checked) is a no-op there, which is why those
instances are latent and often do not reproduce under OOM injection. Record which variant
the destructor uses; it predicts reproducibility."

### Reproduction harness note

`run_oom_sweep.py` arms `set_nomemory` before the **whole** payload, so any freelist-draining
setup burns the allocation budget and pushes the interesting index past `--max-n`. Finding #1
only reproduced with a variant that runs setup **unarmed** and then arms immediately before
the trigger. Suggest adding a `--setup` argument that is executed before
`_testcapi.set_nomemory(start)`. Also: **classify exit code 1 carefully** — the stock harness
maps it to `memory_error`, but an ASan-instrumented build also exits 1 on a sanitizer report;
grep the child's stderr for `AddressSanitizer` before calling it a clean `MemoryError`.
