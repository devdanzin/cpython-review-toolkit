# uninitialized-dealloc-auditor — slice `obj-typeobject`, PASS 2

**Shape:** `dealloc-of-uninitialized-object` (briefing §bug-shape-catalog)
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), `Objects/typeobject.c` only (13,068 lines)
**Scanner baseline:** `scan_uninit_dealloc` = **0 findings**, `functions_analyzed=459`, `allocation_sites=1`
**Mode:** INFORMED. Pass 1's report (`reports/obj-typeobject/agents/uninitialized-dealloc-auditor.md`) is confirmed, not redone.
**Repro artifacts:** `<run>/repro/uninit-dealloc/`

---

## 0. Headline

The pass-2 regions contain **one reproduced, Python-reachable SIGSEGV of this exact
class** — and it is not in `typeobject.c`. `mro_implementation_unlocked`
(`Objects/typeobject.c:3503`) calls `PyList_New(1)`, and `PyList_New`'s
free-threaded branch frees a list whose `ob_item`/`ob_size` were never written
(**CPY-0014**). Pass 2 establishes that `class C(A, B): pass` and
`C.__bases__ = (...)` reach it — a first-class Python-level route where the
catalogued one was `marshal.loads`. Reproduced deterministically on
`debug-ft-nojit`, `release-ft-nojit` and `release-ft-nojit-asan`; clean on both
GIL builds.

The `allocator_model` decision from issue #28 is **audited first-hand and
upheld** — the `memset` at `typeobject.c:2542` plus the two preheader stores at
`:2534-2535` cover every field a dealloc can read, in all four
GC×PREHEADER×build combinations. H-1 was not right, not even narrowly, *about
the memset*. It remains right about the **dispatch** (CPY-0086), which is a
different claim.

The scanner's `allocation_sites=1` is **6.7% of the truth**: this file has
**15** runtime allocation sites.

And the OOM harness itself has a false-negative generator: **an exception in
`--setup` exits 1, which `run_oom_sweep.py` classifies as `memory_error` — the
SAFE outcome — for every index.** Measured here: four 400-iteration sweeps
reported a perfect 400/400 clean result that was entirely an artifact. After a
one-line fix to the payload, the same sweeps produced 13 aborts and 2 SIGSEGVs.

---

## 1. Task 1 — audit of the `allocator_model` decision, first-hand

**Verdict: CONFIRMED. `PyType_GenericAlloc` zeroes everything a `tp_dealloc` /
`tp_clear` can read. Forcing `tp_alloc` into the non-zeroing set (proposed
fix H-1) would have modelled a falsehood, and issue #28 was right not to ship
it.** I found no unzeroed region. Below is the region-by-region proof.

`_PyType_AllocNoTrack` (`Objects/typeobject.c:2512-2554`) lays out one
allocation of `size + presize` bytes and zeroes it in two pieces.

### 1.1 The tail: `tp_basicsize` vs `tp_itemsize` — COVERED

```c
size_t size = _PyObject_VAR_SIZE(type, nitems+1);          /* :2521 */
if (type->tp_flags & Py_TPFLAGS_INLINE_VALUES) {
    size += _PyInlineValuesSize(type);                     /* :2526 */
}
...
memset((char *)obj + sizeof(PyObject), 0, size - sizeof(PyObject));   /* :2542 */
```

The `memset` length is computed from the **same** `size` that sized the malloc,
so it covers, in one span: the rest of `tp_basicsize`, the whole
`(nitems+1) * tp_itemsize` item area *including the deliberate `+1` slack item*
(the GH-100659 / GH-81381 comment at `:2515-2520`), the `_PyObject_VAR_SIZE`
round-up padding to `SIZEOF_VOID_P`, and the `Py_TPFLAGS_INLINE_VALUES` block.
Note the ordering is load-bearing and correct: `size` is grown by
`_PyInlineValuesSize` *before* the `memset`, so inline values are inside the
zeroed span; `_PyObject_InitInlineValues` then overwrites the header fields.
The `PyObject` header itself is deliberately excluded and is written by
`_PyObject_Init` / `_PyObject_InitVar` immediately after.

`type_dealloc`'s variable-size reader is `traverse_slots` / `clear_slots`
(`:2584`, `:2622`) walking `Py_SIZE(type)` `PyMemberDef`s in the item area —
`Py_SIZE` is set by `_PyObject_InitVar`, the members by `memset`. Symmetric.

### 1.2 The preheader: managed dict + managed weakref — COVERED

```c
const size_t presize = _PyType_PreHeaderSize(type);        /* :2523 */
char *alloc = _PyObject_MallocWithType(type, size + presize);
obj = (PyObject *)(alloc + presize);
if (presize) {
    ((PyObject **)alloc)[0] = NULL;                        /* :2534 */
    ((PyObject **)alloc)[1] = NULL;                        /* :2535 */
}
```

`_PyType_PreHeaderSize` (`Include/internal/pycore_object.h:852`) is
`[GIL only: _PyType_IS_GC * sizeof(PyGC_Head)] + HasFeature(PREHEADER) * 2 * sizeof(PyObject *)`,
and the accessor offsets (`pycore_object.h:922-927`) are
`MANAGED_DICT = -3*ptr`, `MANAGED_WEAKREF = -4*ptr` under the GIL and `-1*ptr` /
`-2*ptr` free-threaded. Checked all four combinations:

| build | GC? | PREHEADER? | `presize` | `alloc[0]` is | `alloc[1]` is | verdict |
|---|---|---|---|---|---|---|
| GIL | yes | yes | `4*ptr` | `obj-4*ptr` = MANAGED_WEAKREF | `obj-3*ptr` = MANAGED_DICT | **both zeroed** |
| GIL | yes | no | `2*ptr` | `PyGC_Head._gc_next` | `PyGC_Head._gc_prev` | harmless — `_PyObject_GC_Link(obj)` at `:2538` overwrites both immediately after |
| GIL | no | yes | `2*ptr` | `obj-2*ptr` | `obj-1*ptr` | **would be out of bounds** — see below |
| FT | any | yes | `2*ptr` | `obj-2*ptr` = MANAGED_WEAKREF | `obj-1*ptr` = MANAGED_DICT | **both zeroed** |
| FT | any | no | `0` | (`if (presize)` skips) | — | nothing to zero |

Row 3 is the only way the preheader could be read outside its allocation, and it
is **structurally impossible**: `type_ready_preheader`
(`Objects/typeobject.c:9204`) rejects `Py_TPFLAGS_MANAGED_DICT` without
`Py_TPFLAGS_HAVE_GC` at `:9214-9219` and `Py_TPFLAGS_MANAGED_WEAKREF` without it
at `:9233-9238`, and it runs inside `type_ready` (`:9574`) — i.e. before the
type can ever be used to allocate an instance. That pair of checks is the
guarded twin that makes the `presize` arithmetic and the fixed
`MANAGED_*_OFFSET` constants agree. It is not commented as such, and it is
gated on `if (initial)` at `:9570`, so it is a *first-readying-only* invariant;
worth a one-line note upstream (see §5, POLICY-1).

### 1.3 What the model *is* still wrong about

The `tp_alloc_rule` in the envelope —

> a `->tp_alloc(...)` call counts as non-zeroing only in a file that registers
> its own allocfunc returning raw storage

— is a **file-local** resolution of a slot whose value can come from another
translation unit. Pass 1 recorded that as CPY-0086 and reproduced it as a
SIGSEGV. Pass 2 adds an in-tree citation that the premise "a heap type's
`tp_alloc` is `PyType_GenericAlloc`" is *known false* to CPython itself:

```c
static inline int
check_basicsize_includes_size_and_offsets(PyTypeObject* type)   /* :5146 */
{
    if (type->tp_alloc != PyType_GenericAlloc) {
        // Custom allocators can ignore tp_basicsize
        return 1;
    }
```

`PyType_FromSpec`-created types with a custom `Py_tp_alloc` are a supported,
in-tree-used configuration (`Modules/arraymodule.c:3163`), and this function
silently *skips its whole layout audit* for them — 61 lines before
`type_from_slots_or_spec` dispatches through the very same unvalidated slot at
`:5623`. That strengthens CPY-0086 without re-litigating it.

---

## 2. Task 2 — the honest allocation-site count

**15 runtime allocation sites. The scanner resolves 1 (6.7%).**
Census script: `<run>/repro/uninit-dealloc/uid_denom2.py` (reuses the scanner's
own `extract_functions` / `find_assignments_in_scope` / `_matched_allocator`, so
the numbers are directly comparable to the envelope's).

| # | line | function | allocation | resolved | why not |
|---|---|---|---|---|---|
| 1 | 2504 | `_PyType_NewManagedObject` | `PyType_GenericAlloc(type, 0)` | no | zeroing — **correct** silence |
| 2 | 2528 | `_PyType_AllocNoTrack` | `_PyObject_MallocWithType(...)` | no | raw storage; `memset` at :2542 — **correct** |
| 3 | 2559 | `PyType_GenericAlloc` | `_PyType_AllocNoTrack(...)` | no | zeroing — **correct** |
| 4 | **2573** | `PyType_GenericNew` | `return type->tp_alloc(type, 0);` | no | **return-form**: `find_assignments_in_scope` walks `assignment_expression` + `init_declarator` only, so an allocation that is never bound to a name is structurally invisible. New hole, call it **H-5** |
| 5 | 3371 | `pmerge` | `PyMem_New(Py_ssize_t, …)` | no | `PyMem_*` is out of the allocator family by design |
| 6 | 3488 | `mro_implementation_unlocked` | `PyMem_New(PyObject *, n+1)` | no | same |
| 7 | 3838 | `slot_update_new_chunk` | `PyMem_Malloc(sizeof(chunk))` | no | same |
| 8 | 4413 | `type_new_alloc` | `metatype->tp_alloc(metatype, ctx->nslot)` | no | **H-1** tp_alloc gate |
| 9 | 4556 | `type_new_set_doc` | `PyMem_Malloc(size)` | no | `PyMem_*` |
| 10 | 5423 | `type_from_slots_or_spec` | `PyMem_Malloc(len)` → `tp_doc` | no | `PyMem_*` |
| 11 | 5501 | `type_from_slots_or_spec` | `PyMem_Malloc(name_buf_len)` → `_ht_tpname` | no | `PyMem_*` |
| 12 | 5623 | `type_from_slots_or_spec` | `metaclass->tp_alloc(metaclass, nmembers)` | no | **H-1** — and this is CPY-0086's site |
| 13 | 7468 | `object_new` | `type->tp_alloc(type, 0)` | no | **H-1** |
| 14 | **11343** | `slot_bf_getbuffer` | `PyObject_GC_New(PyBufferWrapper, …)` | **YES** | — |
| 15 | 12999 | `super_vectorcall` | `self_type->tp_alloc(self_type, 0)` | no | **H-1** |

Plus **4** statically-allocated `PyTypeObject`s (`PyType_Type` :7290,
`PyBaseObject_Type` :8557, `_PyBufferWrapper_Type` :11306, `PySuper_Type`
:13025). These are the "static-type allocations" the brief asks about: C
guarantees objects with static storage duration are zero-initialised, so every
field a dealloc could read is `0`/`NULL` before `PyType_Ready` runs. There is no
uninitialised-member risk on the static-type *struct*; the managed-static
*per-interpreter* state is a different question and is F2-2 below.

**Breakdown of the 14 misses:** 5 are `->tp_alloc(...)` (H-1: 4 assignment-form
+ 1 return-form), 6 are `PyMem_*` (deliberately out of family), 3 are the
zeroing helpers the rule correctly ignores. **Only 3 of those 14 misses are
defensible as "correct silence"**; 11 are unrecorded skips.

`allocation_sites` is documented in `analyze()` as *"counts every non-zeroing
allocation the rule saw at all"* — but it is incremented only when
`_matched_allocator()` returns non-`None`, i.e. it is a **resolved**-site
counter. A zero next to it therefore cannot be read as "0 of N sites were
dangerous"; it reads as "0 of the 1 site I could classify".

---

## 3. Task 3 — findings in the pass-2 regions

### Summary
- Candidate constructors / teardown paths hand-read: **11**
- **FIX: 1** (F2-1 — reproduced SIGSEGV, Python-reachable, free-threaded builds)
- **CONSIDER: 1** (F2-2)
- **POLICY: 2** (POLICY-1, POLICY-2)
- **ACCEPTABLE: 7**
- Confirmations of prior records: CPY-0086 (strengthened), CPY-0070 (re-reproduced), CPY-0014 (widened)

---

### [FIX] F2-1 — `class C(A, B): pass` and `C.__bases__ = (...)` reach the free-threaded `PyList_New` uninit-dealloc — **REPRODUCED SIGSEGV on 3 build configurations**

**Sites:** `Objects/typeobject.c:3503` (`PyList_New(1)` in
`mro_implementation_unlocked`), reached from `:9259` `type_ready_mro` and from
`:1803` `mro_hierarchy_for_complete_type` ← `:1949` `type_set_bases_unlocked`.
The defect itself is `Objects/listobject.c:250` / `:262` / `:569` — **CPY-0014**.

**The bug, restated.** `PyList_New(size>0)`:

```c
PyListObject *op = _Py_FREELIST_POP(PyListObject, lists);
if (op == NULL) {
    op = PyObject_GC_New(PyListObject, &PyList_Type);   /* :250 — non-zeroing */
    ...
}
if (size <= 0) { op->ob_item = NULL; }
else {
#ifdef Py_GIL_DISABLED
    _PyListArray *array = list_allocate_array(size);
    if (array == NULL) {
        Py_DECREF(op);                                  /* :262 — ob_item NEVER written */
        return PyErr_NoMemory();
    }
    ...
    op->ob_item = array->ob_item;                       /* :266 */
#else
    op->ob_item = (PyObject **) PyMem_Calloc(...);      /* :268 — written BEFORE the check */
#endif
    if (op->ob_item == NULL) { Py_DECREF(op); return PyErr_NoMemory(); }
}
Py_SET_SIZE(op, size);                                  /* :275 */
```

**Guarded twin, three lines away:** the `#else` (GIL) half *assigns*
`op->ob_item` the failing allocation's own result, so it is `NULL` on failure
and `list_dealloc`'s `if (op->ob_item != NULL)` guard at `:560` skips correctly.
The `Py_GIL_DISABLED` half frees with the field still holding
`PyObject_GC_New`'s garbage, the guard passes, and `free_list_items` hands that
garbage to `PyMem_Free`. (`list_new_prealloc` at `:282` is the second twin —
same `list_allocate_array` call, but only after `PyList_New(0)` and an explicit
`assert(op->ob_item == NULL)`.)

**What pass 2 adds.** CPY-0014's recorded reachability is
`Python/marshal.c:1448` — `marshal.loads` of a crafted `TYPE_LIST`. Pass 2 shows
the same crash on the **ordinary class machinery**, with no marshal, no C
extension, no crafted input:

```
#3  free_list_items (items=…) at Objects/listobject.c:68
#4  list_dealloc (self=0x20002bb0a90) at Objects/listobject.c:569
#7  PyList_New (size=1)
#8  mro_implementation_unlocked   at Objects/typeobject.c:3503
#9  mro_invoke                    at Objects/typeobject.c:3608
#10 mro_internal                  at Objects/typeobject.c:3666
#11 mro_hierarchy_for_complete_type at Objects/typeobject.c:1803
#12 type_set_bases_unlocked       at Objects/typeobject.c:1949
#13 type_set_bases                at Objects/typeobject.c:2032
```

faulting on `_PyMem_DebugFree(p=0xcdcdcdcdcdcdcdc5)` — the debug allocator's
CLEANBYTE fill, i.e. provably the freshly-`malloc`'d uninitialised member and
not anything the payload wrote. The second entry point is a plain class
statement:

```
#8  mro_implementation_unlocked at Objects/typeobject.c:3503
#11 type_ready_mro              at Objects/typeobject.c:9259
```

Both need **≥ 2 bases**: `mro_implementation_unlocked` (`:3431`) short-circuits
to a `PyTuple_New` build for the single-base case (`:3453-3472`) and only
reaches `PyList_New(1)` at `:3503` once it has to run the real C3 merge.

**Reproduction (§4.2 for the full table).** `debug-ft-nojit`: SIGSEGV, 3/3 runs,
at two distinct sweep indices. `release-ft-nojit`: SIGSEGV at the *same* two
indices with the asserts compiled out — so this is not a debug artifact.
`release-ft-nojit-asan`: SEGV report at the same indices.
`debug-gil-nojit` / `release-gil-nojit`: **clean at every index** — the
`#ifdef Py_GIL_DISABLED` differential, exactly as CPY-0014 predicts.

**Freelist caveat, stated honestly.** `_Py_FREELIST_POP` hands back a *recycled*
list whose `ob_item` `list_dealloc` already NULLed, which hides the window
completely. A naive minimal repro does **not** crash for this reason (I wrote
one and it did not). The freelist must be drained first —
`_hold = [[] for _ in range(4000)]` in the setup — after which
`class Victim(M1, M2): pass` segfaults at `n=10` and
`Warm.__bases__ = (M2, M1)` at `n=4` on a 120-index sweep. This is the
briefing's "dirty recycled block" caveat operating in the opposite direction
from gh-151815, and it is why a *narrow* payload can produce a false clean.

**Fix** (the canonical one for this shape, in `Objects/listobject.c`):

```c
    op = PyObject_GC_New(PyListObject, &PyList_Type);
    if (op == NULL) {
        return NULL;
    }
+   op->ob_item = NULL;
+   Py_SET_SIZE(op, 0);
```

i.e. NULL the members immediately after the non-zeroing allocation, before the
first fallible step — rather than restructuring the two `#ifdef` arms.

**Ownership note.** The defect is in `listobject.c`, outside this slice; the
pass-2 contribution is the reachability. Recommend **widening CPY-0014's
`sites`** with `Objects/typeobject.c:3503 mro_implementation_unlocked` and its
two callers, and updating `cpython_behavior` from "e.g. via marshal.loads" to
"any class statement with two or more bases, or any `__bases__` assignment".

---

### [CONSIDER] F2-2 — `init_static_type`'s failure path clears the managed-static index but leaves `_Py_TPFLAGS_STATIC_BUILTIN` set, so every later accessor computes `index = SIZE_MAX`

**Sites:** `Objects/typeobject.c:9640` `init_static_type`, unwind at
`:9671-9675`; `:254-258` `managed_static_type_index_clear`; `:238-245`
`managed_static_type_index_get`; `:526-537` `lookup_tp_dict`.

For a managed static type the **index into the per-interpreter state array is
stored in `tp_subclasses`** as a 1-based integer:

```c
static inline size_t managed_static_type_index_get(PyTypeObject *self) {
    assert(managed_static_type_index_is_set(self));      /* tp_subclasses != NULL */
    return (size_t)self->tp_subclasses - 1;              /* :243 */
}
```

`init_static_type` sets `_Py_TPFLAGS_STATIC_BUILTIN` on the type *before*
calling `type_ready`, and on failure does:

```c
    res = type_ready(self, initial);
    if (res < 0) {
        _PyStaticType_ClearWeakRefs(interp, self);
        managed_static_type_state_clear(interp, self, isbuiltin, initial);
    }
```

With `initial == 1` (a first-time init) `final == 1`, so
`managed_static_type_state_clear` calls `managed_static_type_index_clear(self)`
→ `self->tp_subclasses = NULL` — **while `_Py_TPFLAGS_STATIC_BUILTIN` stays
set**. Any subsequent `lookup_tp_dict` / `lookup_tp_subclasses` /
`_PyStaticType_GetState` on that type therefore takes the static-builtin branch
and computes `(size_t)NULL - 1 == SIZE_MAX`, indexing
`interp->types.builtins.initialized[SIZE_MAX]` — a wild pointer. Debug builds
trip the assert at `:241`; release builds read it.

The failure path is also **not** the inverse of the success path: it skips
`type_dealloc_common` (so partial `type_ready_add_subclasses` registrations
survive — and `type_ready_add_subclasses` runs at `:9581`, *before*
`type_ready_managed_dict` (`:9585`) and `type_ready_post_checks` (`:9588`), so
the window is real) and skips `clear_static_type_objects` (so `state->tp_dict`
stays populated at a slot whose `state->type` is now `NULL`, and
`managed_static_type_state_get` falls through its `state->type == self` test to
the **`for_extensions` array** at `:297`, i.e. a different type's slot).

**Guarded twin:** `fini_static_type` (`:6923`) is the correct unwind — it
calls `type_dealloc_common`, then `clear_static_type_objects`, then
`_PyStaticType_ClearWeakRefs`, then `managed_static_type_state_clear`, and it
carries the explicit comment `/* We leave _Py_TPFLAGS_STATIC_BUILTIN set on
tp_flags. */`. Leaving the flag set is safe *there* because nothing touches the
type again. The init-failure path copies that decision into a context where the
interpreter keeps running.

**Why CONSIDER and not FIX — reachability is currently closed.** Every in-tree
route to `initial == 1` is fatal before the corrupted type can be observed:

- `_PyStaticType_InitBuiltin` ← `_PyTypes_InitTypes` (`Objects/object.c:2677`)
  and friends run inside `pycore_interp_init`; `initial = _Py_IsMainInterpreter(interp)`,
  so a subinterpreter gets `initial == 0` → `final == 0` → the index is **not**
  cleared, and the main interpreter's failure aborts startup.
- `_PyStaticType_InitForExtension` has exactly one in-tree caller,
  `Modules/_datetimemodule.c:7531` (`_PyDateTime_InitTypes`), which is invoked
  from `Python/pylifecycle.c:832` — again inside interpreter init, returning
  `_PyStatus_ERR`.

It is nonetheless a `PyAPI_FUNC`-exported entry point, and the shape is
"discriminator member cleared on an error path while the flag that selects it
stays set" — the blake2 (gh-152851) family. I swept subinterpreter creation
(`_interpreters.create()`) looking for a live route and found none in range.

**Fix (one line, at `Objects/typeobject.c:9673-9675`):** clear the flag together with
the index on the failure path —

```c
    if (res < 0) {
        _PyStaticType_ClearWeakRefs(interp, self);
        managed_static_type_state_clear(interp, self, isbuiltin, initial);
+       if (initial) {
+           type_clear_flags(self, _Py_TPFLAGS_STATIC_BUILTIN);
+       }
    }
```

— or, more conservatively, keep the index and only clear the runtime slot, so
the flag and the index never disagree.

---

### [POLICY] POLICY-1 — the preheader zeroing depends on an invariant enforced 6,700 lines away

`_PyType_AllocNoTrack:2533-2536` zeroes `alloc[0]`/`alloc[1]` on the assumption
that `presize`'s two preheader words sit at exactly `MANAGED_WEAKREF_OFFSET` /
`MANAGED_DICT_OFFSET`. Under the GIL that is true only because
`Py_TPFLAGS_PREHEADER` implies `Py_TPFLAGS_HAVE_GC`, which is enforced by
`type_ready_preheader:9214` / `:9233` — and only on the `if (initial)` branch
(`:9570`). Neither site references the other. An
`assert(!(type->tp_flags & Py_TPFLAGS_PREHEADER) || _PyType_IS_GC(type));`
next to `:2523` would pin it locally. Cost: one line. No behaviour change.

### [POLICY] POLICY-2 — `type_dealloc`'s watcher loop is the blake2 shape, bounded only by a type width

`type_dealloc` opens by reading `type->tp_watched` as a scalar discriminator and
driving a bit loop over `interp->type_watchers[]` **before** any member is
touched (`:6988-7009`, mirrored in `_PyType_Modified_Unlocked` at `:1212`):

```c
    int bits = type->tp_watched;
    int i = 0;
    while (bits) {
        assert(i < TYPE_MAX_WATCHERS);
        if (bits & 1) { PyType_WatchCallback cb = interp->type_watchers[i]; … }
        i++; bits >>= 1;
    }
```

This is exactly the gh-152851 pattern (`switch`/loop on a member that a
half-built object never wrote), and it is what CPY-0086's non-zeroing metatype
allocator exposes. It is **safe today for a non-obvious reason**: `tp_watched`
is `unsigned char` (`Include/cpython/object.h:236`), so `bits ≤ 255` and the
loop reads at most `type_watchers[7]`, exactly matching
`TYPE_MAX_WATCHERS == 8` (`Include/internal/pycore_interp_structs.h:22`). Any
future widening of `tp_watched`, or any reduction of `TYPE_MAX_WATCHERS`,
converts garbage in that field into an out-of-bounds **function-pointer call**.
`_PyObject_ResurrectStart(self)` also runs on that garbage-driven branch.
A `static_assert((1u << (8 * sizeof(((PyTypeObject*)0)->tp_watched))) - 1 <
(1u << TYPE_MAX_WATCHERS))` would make the coupling explicit.

---

### [ACCEPTABLE] F2-3 — `slot_update_new_chunk` is the in-slice guarded twin for the raw-malloc variant

`Objects/typeobject.c:3835-3846`. `PyMem_Malloc(sizeof(slot_update_chunk_t))`
is followed *immediately* by `chunk->prev = NULL; chunk->n = 0;` with no
fallible step in between, and the 30-element `updates[]` array is left
uninitialised but is only ever read under `i < chunk->n`. That is the textbook
correct form of this shape: NULL the pointer members up front, bound the array
read with an initialised counter. `slot_update_free_chunks` walks `prev` and
frees; it never reads `updates[]`.

### [ACCEPTABLE] F2-4 — the MRO `PyMem_New` arrays

`pmerge:3371` fills `remain[0..to_merge_size-1]` with `0` in an unconditional
loop on the next line. `mro_implementation_unlocked:3488` fills
`to_merge[0..n]` in a non-fallible loop before any use, and its only early exit
(`PyList_New` failure at `:3504`) **frees** `to_merge` without reading it. Both
clean. (One adjacent observation for the null-safety agent, not mine:
`:3494 assert(mro_to_merge != NULL)` is the only guard against a base with a
`NULL` `tp_mro`; in a release build that becomes `PyTuple_GET_SIZE(NULL)` inside
`pmerge`.)

### [ACCEPTABLE] F2-5 — `object_set_class` (`__class__` assignment, 7482-7846)

An object mid-reassignment cannot reach a dealloc that reads the wrong fields,
because `compatible_for_assignment` (`:7628`) requires, before the
`Py_SET_TYPE`: identical `tp_free`; identical
`Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_INLINE_VALUES | Py_TPFLAGS_PREHEADER`;
identical `tp_dictoffset` and `tp_weaklistoffset`; and, via
`compatible_with_tp_base` + `same_slots_added`, an identical `__slots__` layout
up to a common supertype. The `INLINE_VALUES` materialise-and-detach step
(`:7766-7783`) is the only fallible action, and both of its failure exits
`return -1` **before** `Py_SET_TYPE`, so the object is never observed with the
new type and the old layout. The `Py_INCREF(newto)` / `Py_DECREF(oldto)` pair is
correctly split across `object_set_class_world_stopped` and its caller so the
old type cannot be freed while the world is stopped.

### [ACCEPTABLE] F2-6 — `object_new` (`:7406`)

The allocation `obj = type->tp_alloc(type, 0)` at `:7468` is the **last**
statement; all eight early `return NULL`s precede it and free
nothing. No window exists.

### [ACCEPTABLE] F2-7 — the type-attribute lookup cache (6140-6452)

`struct type_cache_entry` lives in the interpreter's `type_cache.hashtable[]`,
which is part of `PyInterpreterState` — not a per-object non-zeroing allocation.
`update_cache` (`:6216-6232`) writes `value`, then `name`, then `version`
**last**, with the ordering documented as load-bearing against a concurrent
`_PyType_LookupRefAndVersion`. Nothing here is freed on an error path. Not this
class.

### [ACCEPTABLE] F2-8 — getattro/setattro (6529-6848), pickle/`__reduce_ex__` (7848-8406), super beyond construction

No allocation site in the census falls in these ranges; every object they build
(`PyTuple_New`, `PyList_New(0)`, `PyDict_New`, `_PyObject_GetState`'s
intermediates) is self-initialising, and every teardown is `Py_XDECREF` of a
local that was NULL-initialised at declaration. Swept: 400 indices each on GIL
and FT, plus the 700-index wide payload; zero crashes attributable to these
regions. `super_dealloc`'s three-member `Py_XDECREF` was settled by pass 1 (F-5)
— confirmed unchanged.

### Confirmations (one line each, per informed mode)

- **CPY-0086** (pass-1 F-6, `type_from_slots_or_spec` dispatches through an
  unvalidated metatype `tp_alloc` at `:5623`): still present, unchanged;
  strengthened by `check_basicsize_includes_size_and_offsets:5148`, which
  documents in-tree that a custom `tp_alloc` is a supported configuration whose
  layout CPython declines to audit.
- **CPY-0070** (`type_set_bases_unlocked` never tests `add_all_subclasses`;
  `update_one_slot:11938` `assert(!PyErr_Occurred())`): re-reproduced
  incidentally by four independent pass-2 payloads across eight payload/build
  combinations (54 aborting indices total). Already `status: reproduced`; nothing new
  to add beyond "still live at `4f3be1b5777`".
- **CPY-0014**: still live; see F2-1 for the widened reachability.
- **CPY-0011 / CPY-0015 / CPY-0017**: the odict/dict/template iterator
  instances of this shape remain the exemplars; `super_vectorcall` (`:12999`)
  remains this file's in-slice guarded twin for them (pass-1 F-5, unchanged).

---

## 4. Reproduction

All builds verified byte-identical to the target ref for every file cited:
`diff builds/<name>/Objects/typeobject.c` (and `Objects/object.c`,
`Objects/listobject.c`, `Include/internal/pycore_object.h`) against
`cpython@4f3be1b5777` → **IDENTICAL** for `debug-gil-nojit`, `debug-ft-nojit`,
`release-gil-nojit`, `release-ft-nojit`, `release-ft-nojit-asan`,
`debug-gil-nojit-asan`. (The matrix is at `a1d580430c8`, newer than the target;
these files did not change between the two.)

### 4.1 Method correction — the harness was lying, and I nearly published the lie

The first four sweeps returned a *perfect* result: 400 iterations each, 400/400
`memory_error`, 0 `completed`, 0 crashes. That reads as "every allocation-failure
point in the payload was handled cleanly". It was an artifact: `p2_setup.py`
contained `pickle.dumps(dobj, 2)`, which cannot pickle a class defined inside an
`exec`'d namespace. **`run_oom_sweep.py`'s child harness does not guard the
setup phase**, so the child died with a `PicklingError` traceback and exit code
**1** — the code the harness maps to `memory_error`, the SAFE outcome.

A binary search over the failure index (`probe_depth.py`) exposed it: the
payload still "raised MemoryError" at `n = 4,194,304`, which no six-statement
payload can. After deleting one line from the setup, the same `p2_bases.py`
sweep produced 13 aborts on GIL and 13 aborts + 2 SIGSEGVs on FT.

Every sweep below is now preceded by an unarmed dry-run
(`<run>/repro/uninit-dealloc/dryrun.py`) that proves setup **and** payload run
clean and exits 3/4 with a traceback if not. See §5, TOOL-1.

### 4.2 Sweeps (`--width 1`, setup unarmed, stderr checked for sanitizer reports)

"points" = iterations that actually reached an allocation-failure inside the
payload (`memory_error` + crashes); the rest completed before the budget was
consumed. That is the real denominator.

| build | payload | iters | points | completed | crashes |
|---|---|---|---|---|---|
| `debug-gil-nojit` | `p2_bases` (`__bases__` × 4, MRO recompute) | 400 | 75 | 325 | 13 abort — CPY-0070 |
| `debug-gil-nojit` | `p2_setclass` (`__class__` × 5) | 400 | 4 | 396 | **0** |
| `debug-gil-nojit` | `p2_pickle` (`__reduce_ex__` × 6) | 400 | 12 | 388 | **0** |
| `debug-gil-nojit` | `p2_super_lookup` (super, getattro/setattro, cache) | 400 | 5 | 395 | **0** |
| `debug-gil-nojit` | `p2_watch` (watchers + version tags + type teardown) | 300 | 98 | 201 | 3 abort — CPY-0070 |
| `debug-gil-nojit` | **`p2_wide`** (all pass-2 regions + teardown) | 700 | 164 | 536 | 6 abort — CPY-0070 |
| `debug-ft-nojit` | `p2_bases` | 400 | 77 | 323 | 13 abort + **2 SIGSEGV** (n=30, 44) |
| `debug-ft-nojit` | `p2_setclass` | 400 | 4 | 396 | **0** |
| `debug-ft-nojit` | `p2_pickle` | 400 | 14 | 386 | **0** |
| `debug-ft-nojit` | `p2_super_lookup` | 400 | 5 | 395 | **0** |
| `debug-ft-nojit` | `p2_watch` | 300 | 97 | 203 | 3 abort |
| `debug-ft-nojit` | **`p2_wide`** | 700 | 169 | 531 | 6 abort + **1 SIGSEGV** (n=147) |
| `release-ft-nojit` | `p2_bases` | 400 | 77 | 323 | **2 SIGSEGV — same indices 30, 44**, asserts compiled out |
| `release-ft-nojit-asan` | `p2_bases` | 400 | 77 | 323 | **2 sanitizer_error (SEGV)**, same indices |
| `release-gil-nojit` | `p2_bases` (**GIL control**) | 400 | 75 | 325 | **0** — same 75 failure points, no SEGV, no abort |
| `debug-gil-nojit` | `mini_class` (`class V(M1,M2)`) | 120 | 17 | 103 | **0** |
| `debug-gil-nojit` | `mini_bases` (`W.__bases__=…`) | 120 | 13 | 107 | 5 abort |
| `debug-ft-nojit` | `mini_class` | 120 | 21 | 99 | **1 SIGSEGV (n=10)** |
| `debug-ft-nojit` | `mini_bases` | 120 | 15 | 105 | **1 SIGSEGV (n=4)** + 5 abort |

`p2_setclass` / `p2_pickle` / `p2_super_lookup` expose only 4–14 distinct
allocation-failure points each — **too thin a denominator to certify anything**,
which is exactly why `p2_wide` exists: it walks every pass-2 region in one
payload and yields 164 (GIL) / 169 (FT) points. Its only crashes are CPY-0070
and F2-1.

### 4.3 Deterministic reductions

`Objects/typeobject.c:3503` → CPY-0014, free-threaded builds
(`<run>/repro/uninit-dealloc/mini_setup.py` + `mini_class.py` / `mini_bases.py`,
or the self-contained `repro_cpy0014_typeobject.py`):

```
$ python -c "$(cat mini_child_class10.py)"     # debug-ft-nojit
Program received signal SIGSEGV
#0 _PyMem_DebugCheckAddress (p=0xcdcdcdcdcdcdcdc5) Objects/obmalloc.c:3344
#3 free_list_items                              Objects/listobject.c:68
#4 list_dealloc (self=0x200027d…)               Objects/listobject.c:569
#7 PyList_New (size=1)
#8 mro_implementation_unlocked                  Objects/typeobject.c:3503
#11 type_ready_mro (type=…, initial=1)          Objects/typeobject.c:9259
```

3/3 runs at n=10 (`class Victim(M1, M2): pass`), 3/3 at n=4
(`Warm.__bases__ = (M2, M1)`), and 3/3 at n=30 / n=44 with the larger payload.
Same script on `debug-gil-nojit`: clean at every index.

CPY-0070 (`<run>/repro/uninit-dealloc/` + the scan in §4.2): the
`update_one_slot:11938` assert fires at n=7..11 for a single
`Sub.__bases__ = (M1, M2)` on `debug-gil-nojit`. Already recorded; noted only as
"still live".

---

## 5. Toolkit assessment

### The verdict on the zero: **STRUCTURAL, and worse than pass 1 measured**

Pass 1 reported the rule resolved 1 of 8 *object* allocation sites. Counting the
whole allocation surface the brief asks about, it is **1 of 15 (6.7%)**. The
zero this envelope reports is therefore not evidence about
`Objects/typeobject.c`; it is a statement that exactly one site in the file was
classifiable. The class is nonetheless **genuinely absent from this file** — for
the reason §1 proves and 6,880 sweep iterations corroborate — but the finding of
the run came from *outside* the scanner's model entirely: a `PyList_New` call in
another translation unit, reached from `typeobject.c:3503`.

### TRUE allocation-site count: **15** (scanner: 1). Static types: 4 more, zero-risk.

Breakdown in §2. Of the 14 misses, **3 are correct silence** (zeroing helpers)
and **11 are unrecorded skips**.

### Audit of the `allocator_model` decision: **UPHELD, with its scope sharpened**

Issue #28 was right not to ship H-1's "force `tp_alloc` into `non_zeroing`". The
`memset` at `:2542` plus the preheader stores at `:2534-2535` are complete
across all four GC×PREHEADER×build combinations (§1). Treating `tp_alloc` as
non-zeroing would fire on every heap-type constructor in the tree and model a
falsehood.

But the envelope's `tp_alloc_rule` still asserts a **file-local** resolution of
a slot that is caller-supplied by design, and CPY-0086 is the reproduced
counter-example. The right change is not to reclassify — it is to **report the
skip**.

### Ranked proposals

**TOOL-1 (highest value, and it is a correctness bug, not a tuning knob) —
`run_oom_sweep.py`: an exception in `--setup` is reported as the SAFE outcome.**
The child harness (`_HARNESS_TEMPLATE`, lines 106-107) runs
`exec(_SETUP_CODE, _NS)` **outside** any `try`. A setup that raises kills the
child with exit code 1, which `classify()` maps to `memory_error`. A totally
broken sweep therefore reports `{"memory_error": N, "crashes": [], "verdict":
"no crash in this range (all failures handled cleanly)"}` — a perfect,
maximally convincing, entirely unearned zero. Measured cost this run: four
400-iteration sweeps certified clean that, once fixed, produced 13 aborts and
2 SIGSEGVs. This is the same defect class the campaign exists to police, living
inside the campaign's own verification tool.

Two-part fix, both cheap:
1. In `_HARNESS_TEMPLATE`, wrap the setup `exec` and `sys.exit(3)` on any
   exception; add `3: "setup_error"` to `classify()` and make `sweep()` abort
   the whole run on the first one.
2. In `sweep()`, run **one unarmed iteration** (`set_nomemory` never called)
   before the loop and refuse to proceed unless it exits 0. That also catches a
   payload that is broken independently of the injection —
   `p2_pickle.py` had one (`sobj.__reduce__()` on a `__slots__` class raises
   `TypeError`), which the current harness would have reported as
   `other_exception` on all 400 indices, i.e. *also* as a non-crash.

A reference implementation of the check is at
`<run>/repro/uninit-dealloc/dryrun.py`.

**TOOL-2 — `allocation_sites` must count sites *considered*, not sites
*resolved*.** This is pass 1's H-1 restated with a measured cost: 11 unrecorded
skips in one file. The docstring in `analyze()` already claims the field
"counts every non-zeroing allocation the rule saw at all", which is not what the
code does. Emit alongside it:

```json
"allocation_sites": 1,
"allocation_sites_considered": 15,
"sites_skipped_unresolved_allocator": [
  {"file": "Objects/typeobject.c", "line": 5623,
   "function": "type_from_slots_or_spec", "expr": "metaclass->tp_alloc(...)",
   "reason": "tp_alloc via a caller-supplied type; no file-local non-zeroing allocfunc"}
]
```

Counts and a list, no findings — zero precision cost, and it converts a
structural zero into a readable one. Note the self-referential argument for it:
CPY-0086 is precisely the bug the gate's heuristic ("no non-zeroing allocfunc in
*this file* ⇒ `tp_alloc` zeroes") is constitutionally unable to see, because the
offending allocfunc lives in a different translation unit **by construction**.

**TOOL-3 (new, H-5) — allocations that are never bound to a name are invisible.**
`_check_function` iterates `find_assignments_in_scope`, which walks
`assignment_expression` and `init_declarator` only.
`Objects/typeobject.c:2573` — `return type->tp_alloc(type, 0);` in
`PyType_GenericNew` — is a real allocation site the rule cannot represent at all.
Harmless here (no free follows), but the pattern `return PyObject_GC_New(...)`
and `foo(PyObject_New(...))` exist in the tree. Cheap partial fix: also walk
`return_statement` and `call_expression` arguments for allocator calls and count
them toward `allocation_sites_considered`, even if no finding can be built
without a variable to track.

**TOOL-4 — H-3 and H-4 from pass 1 are still open** (H-2, the alias fix, shipped
in PR #29 and I confirmed `_alias_names` is present and correct):
- `_check_function:472` still does `if not writes: continue`, silently dropping
  any constructor that delegates member init to a helper. In this file that is
  `super_vectorcall` (`:12999`), a textbook instance of the shape. Proposal
  unchanged: emit `confidence: "unresolved"` instead of dropping.
- `_member_evidence` (`:207`) is still file-scoped, pooling destructor evidence
  across every type in a file. Costs nothing here (no findings to inflate) but
  inflates `confidence` on any multi-type file.

**TOOL-5 (recall, cross-cutting) — the "same file" constraint cost this run its
only FIX.** The documented hole (i) — "the destructor must be in the same file
as the constructor" — is joined by a stronger one: the *allocator* need not be
in the same file either. `mro_implementation_unlocked` calls `PyList_New`, whose
uninitialised-member window is 9,000 lines away in `listobject.c`. No per-file
scan can see that. A cheap corpus-level mitigation: once a constructor is
confirmed (CPY-0011/0014/0015/0016/0017 are all already in the catalog), emit a
`callers_of_known_bad_constructor` cross-reference — every call site of
`PyList_New(n>0)`, `PyObject_GetIter`, etc. is a *reachability* candidate, not a
new bug. That is what turned a marshal-only record into a
`class C(A, B): pass` crash.

### Suggested catalog action

- **Widen CPY-0014** with `Objects/typeobject.c:3503 mro_implementation_unlocked`
  (+ callers `:9259 type_ready_mro`, `:1803 mro_hierarchy_for_complete_type`,
  `:1949 type_set_bases_unlocked`) and restate `cpython_behavior` as "any class
  statement with ≥2 bases, or any `__bases__` assignment, on a free-threaded
  build". Add the freelist-draining note — the trigger is *unstable* without it.
- **New record for F2-2** (`init_static_type` leaves `_Py_TPFLAGS_STATIC_BUILTIN`
  set after clearing the index), `status: static-confirmed`, class
  `uninit-dealloc` / discriminator sub-shape, guarded twin `fini_static_type`.
- No new record for POLICY-1 / POLICY-2 — hardening suggestions, not defects.
