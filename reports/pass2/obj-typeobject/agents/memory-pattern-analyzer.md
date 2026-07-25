# Memory Pattern Analysis Results — slice `obj-typeobject`, PASS 2

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c` (13,068 lines) — pass-2 regions: MRO C3 3217-3702,
lookup cache 6140-6452, getattro/setattro 6529-6848, `__class__` assignment 7482-7846,
pickle 7848-8406, watchers/versions 971-1481, managed static types 228-522, super.
**Mode:** INFORMED. Briefing + FP taxonomy + `PASS2_BASELINE.md` + pass 1's report read first.

**Build-vs-ref check (required).** Matrix builds are at `a1d580430c8`, newer than the target
`4f3be1b5777`. `git diff` over every file cited below is **empty** — `Objects/typeobject.c`,
`Include/cpython/objimpl.h`, `Python/ceval.h`, `Include/internal/pycore_interp_structs.h`,
`Objects/structseq.c`, `Objects/tupleobject.c` are all **byte-identical** between the two
refs. Every line number is valid on both, and every reproduction below runs the exact code
reviewed.

---

## Summary

| category | count |
|---|---|
| `varobject_nitems_unguarded` (scanner) | 2 — triaged in full (§1) |
| census sites audited | 7 of 7 (§2); **true `non_constant_nitems` is 3, not 2** |
| `alloc_size_overflow` | 0 (structural: the file has no visible multiply into an allocator) |
| `gc_untrack_without_track` | 0 (denominator: 3 `_PyObject_GC_TRACK` / 4 `_PyObject_GC_UNTRACK` sites, all audited §6) |
| `mismatched_alloc_free` | 0 (full inventory re-verified, §7) |
| Hand-checked regions | 8 pass-2 regions read |
| **FIX** | **2** (both reproduced, both new, both in `__class__` assignment) |
| **CONSIDER** | 3 |
| **ACCEPTABLE** | 5 (bounded negatives, guard cited) |
| Prior findings confirmed without re-litigation | 3 (CPY-0078, CPY-0088, CPY-0089) |
| Toolkit recall gaps found | 1, quantified and reproduced synthetically |

**Headline:** two new crash-grade bugs in the `__class__`-assignment region, reproduced on
five builds from one ~20-line pure-Python script. Both are *downstream of the site CPY-0078
already records* (`same_slots_added:7609`) — the sibling hunt on a recorded finding paid,
which is exactly what informed mode is for.

---

## 1. The two `varobject_nitems_unguarded` findings — full triage

### 1.1 [CONSIDER] `PyType_GenericAlloc` :2559 — `dispatch: direct`, the shared trunk

```c
/* Objects/typeobject.c:2556 */
PyObject *
PyType_GenericAlloc(PyTypeObject *type, Py_ssize_t nitems)
{
    PyObject *obj = _PyType_AllocNoTrack(type, nitems);   /* :2559 */
```
```c
/* :2521, inside _PyType_AllocNoTrack */
size_t size = _PyObject_VAR_SIZE(type, nitems+1);
/* Include/cpython/objimpl.h:23-27 — the invisible multiply */
size += (size_t)nitems * (size_t)type->tp_itemsize;
```

**Where `nitems` comes from:** it is this function's own parameter. `PyType_GenericAlloc`
is a **public, limited-API** entry point *and* the default `tp_alloc` of every heap type, so
the finding sits on the trunk that all seven sites in this file and all 132 indirect
`tp_alloc(` sites tree-wide funnel through. There is no guard at the trunk: no
`PY_SSIZE_T_MAX / tp_itemsize` division check, no `__builtin_mul_overflow`, no `nitems < 0`
test. `nitems+1` at :2521 is itself signed-overflow UB at `PY_SSIZE_T_MAX`.

**Can Python control it?** I traced every Python-reachable caller with a non-constant
`nitems` (pass 1 enumerated 15 tree-wide; I re-checked the four reachable from a Python
constructor):

| caller | `nitems` | bound |
|---|---|---|
| `Objects/typeobject.c:4413` `type_new_alloc` | `ctx->nslot` | `PyTuple_GET_SIZE` of a tuple this function materialized (pass 1 §1) |
| `Objects/tupleobject.c:848` `tuple_subtype_new` | `PyTuple_GET_SIZE(tmp)` | bounded-by-existing-allocation |
| `Objects/bytesobject.c:3191` `bytes_subtype_new` | `n = PyBytes_GET_SIZE(tmp)` | bounded-by-existing-allocation |
| `Objects/longobject.c:6036` `long_subtype_new` | `ndigits = size ? size : 1`, `size = _PyLong_DigitCount(tmp)` | bounded-by-existing-allocation |

Every one is the `bounded-by-an-existing-allocation` class: the count is the length of an
object that already occupies ≥ `n·elemsize` live bytes. **No Python program can reach an
unbounded `nitems` here.** A C extension can (`PyType_GenericAlloc(&MyType, 2**62)`), which
is a C-caller contract, not a Python-reachable bug.

**Classification: CONSIDER.** Not FIX — the operand is C-caller-controlled only. But this is
the *correct location* for the central guard, and it is the strongest argument in the file
for adding one, because it discharges the obligation for all 132 indirect sites at once:

```c
/* _PyType_AllocNoTrack, before :2521 */
if (nitems < 0 ||
    (type->tp_itemsize &&
     (size_t)nitems + 1 > (SIZE_MAX - (size_t)type->tp_basicsize) / (size_t)type->tp_itemsize))
{
    return PyErr_NoMemory();
}
```
**Guarded twin:** `Objects/tupleobject.c:52 tuple_alloc` —
`n > (PY_SSIZE_T_MAX - base) / sizeof(PyObject *)`. Also `Objects/structseq.c:77`
`PyStructSequence_New` is the *unguarded* twin and is a live Python-reachable heap overflow
(CPY-0012), which is precisely why the trunk guard is worth having.

### 1.2 [ACCEPTABLE, with pass 1's CONSIDER carried] `type_from_slots_or_spec` :5623 — `dispatch: slot_pointer`

`metaclass->tp_alloc(metaclass, nmembers)`. **Confirming pass 1 §2, not re-litigating:**
`nmembers` accumulates across `Py_tp_members` slots with no reset, but a second
`Py_tp_members` slot is rejected by `_PySlot_PROBLEM_REJECT` inside `_PySlotIterator_Next`
before the first-pass `case` body can run, on all five entry points. `nmembers` is therefore
the length of one caller-supplied static `PyMemberDef[]` — C-caller-controlled, not
Python-reachable. Pass 1's CONSIDER (the `memcpy` at :5682 has no local bound, and the
invariant that makes it safe lives in a generated table two files away) stands unchanged.

---

## 2. The other five census sites — the real split

`varobject_allocation_census` reports `sites=7, via_slot_pointer=5, non_constant_nitems=2`.
I enumerated all seven by hand:

| line | call | `nitems` text | dispatch | scanner's verdict | truth |
|---|---|---|---|---|---|
| 2504 | `PyType_GenericAlloc(type, 0)` | `0` | direct | constant | **constant ✓** |
| 2559 | `_PyType_AllocNoTrack(type, nitems)` | `nitems` | direct | non-constant, reported | **correct ✓** |
| 2573 | `type->tp_alloc(type, 0)` | `0` | slot | constant | **constant ✓** |
| 4413 | `metatype->tp_alloc(metatype, ctx->nslot)` | `ctx->nslot` | slot | **counted as constant** | **NON-CONSTANT — MISSED** |
| 5623 | `metaclass->tp_alloc(metaclass, nmembers)` | `nmembers` | slot | non-constant, reported | **correct ✓** |
| 7468 | `type->tp_alloc(type, 0)` | `0` | slot | constant | **constant ✓** |
| 12999 | `self_type->tp_alloc(self_type, 0)` | `0` | slot | constant | **constant ✓** |

**`via_slot_pointer = 5` is exactly right** (2573, 4413, 5623, 7468, 12999) — the
`_TP_ALLOC_SLOT_RE` addition works, and it is the reason this file has any var-object
visibility at all (pass 1 reported a structural zero here).

**`non_constant_nitems` should be 3, not 2.** Site :4413 — the single most interesting
var-object site in the file, the one pass 1 spent its §1 on — is silently discharged
*before* the census counter increments, so it is missing from both the findings and the
denominator. Root cause and reproduction in the Toolkit Assessment below.

None of the five constant sites is a bug: four pass the literal `0`, one (`:2504`) is
`PyType_GenericAlloc(type, 0)` inside `_PyType_NewManagedObject` guarded by
`assert(type->tp_itemsize == 0)`.

---

## 3. [FIX] `__class__` assignment: stale `oldto` across a re-entrant assignment → over-DECREF of a type object → heap-use-after-free

**Site:** `Objects/typeobject.c:7799` `object_set_class`, with the re-entrancy window opened
at `:7763` → `:7609`.

```c
/* Objects/typeobject.c:7821-7838 */
    int unique = _PyObject_IsUniquelyReferenced(self);
    if (!unique) { types_stop_world(); }
    PyTypeObject *oldto = Py_TYPE(self);                       /* :7825 captured HERE */
    int res = object_set_class_world_stopped(self, newto);     /* :7826 can run Python */
    if (!unique) { types_start_world(); }
    if (res == 0) {
        if (oldto->tp_flags & Py_TPFLAGS_HEAPTYPE) {
            Py_DECREF(oldto);                                  /* :7832 STALE */
        }
```
```c
/* object_set_class_world_stopped, :7786-7790 */
        if (newto->tp_flags & Py_TPFLAGS_HEAPTYPE) { Py_INCREF(newto); }
        Py_SET_TYPE(self, newto);        /* drops the CURRENT type's ref with no DECREF */
```

**The window.** `object_set_class_world_stopped` :7763 calls `compatible_for_assignment`,
which reaches `same_slots_added` :7587, which at **:7609** does
`PyObject_RichCompareBool(slots_a, slots_b, Py_EQ)` on the two types' `ht_slots` tuples.
CPY-0078 already established that a `str` **subclass** survives into `ht_slots`, so this is
a user `__eq__` call — arbitrary Python, in the middle of the type swap. If that `__eq__`
reassigns `self.__class__`, then:

- the **inner** call installs its own type and correctly `Py_DECREF`s the entry type;
- the **outer** call then `Py_SET_TYPE`s over the intermediate type, dropping its reference
  with no `Py_DECREF` (**leak**), and `Py_DECREF`s `oldto` — a reference the inner call
  already released (**over-DECREF**).

**Impact.** One extra `Py_DECREF` per nested assignment on a live, still-referenced type
object. Reproduced:

| build | outcome |
|---|---|
| `debug-gil-nojit` (single round) | `Python/gc.c:96: gc_decref: Assertion "gc_get_refs(g) > 0" failed: refcount is too small` |
| `debug-gil-nojit` (loop) | `Include/refcount.h:520: _Py_NegativeRefcount: object has negative ref count`, `object type name: type` |
| `release-gil-nojit` (loop) | **SIGSEGV** (core dumped) |
| `release-gil-nojit-asan` (loop) | **`heap-use-after-free` READ of size 4** at `Py_INCREF` / `refcount.h:286`, on the 1040-byte type object, reached from `_PyEval_LoadName`; freed by `Py_DECREF` in `_PyObject_GenericSetAttrWithDict` |

Refcount evidence from the minimal script (`scratchpad/setclass_min.py`), identical on debug
and release GIL builds:

```
A refcnt before: 5   C refcnt before: 4
A refcnt after : 3   C refcnt after : 5
expected: A -1, C unchanged;  actual: A -2 (over-decref), C +1 (leak)
```

**Reproducer** (`scratchpad/setclass_min.py`, also `setclass_reentry.py` for the SIGSEGV):

```python
armed = True
class S(str):
    def __eq__(self, other):
        global armed
        if armed:                # one-shot: unbounded re-entry just RecursionErrors
            armed = False        # inside CPY-0078's swallow
            o.__class__ = C
        return True
    __hash__ = str.__hash__

A = type("A", (), {"__slots__": (S("x"),)})
B = type("B", (), {"__slots__": (S("x"),)})
C = type("C", (), {"__slots__": (S("x"),)})
o = A(); keep = o
o.__class__ = B                  # -> A over-decref'd, C leaked
```

**Guarded twins — both in this same file:**

1. `subtype_dealloc` :2860-2861 —
   ```c
   /* Extract the type again; tp_del may have changed it */
   type = Py_TYPE(self);
   ```
   Same hazard (`__class__` reassigned by re-entrant Python), correctly handled by
   **re-reading** rather than trusting a value captured before the call. `subtype_dealloc`
   even carries a second guard for the same reason at :2870-2873
   (`type_needs_decref` computed before `basedealloc`, with the comment "Don't read type
   memory after calling basedealloc()").
2. `mro_internal` :3665-3667 — the canonical re-entrancy idiom in this file:
   ```c
   old_mro = Py_XNewRef(lookup_tp_mro(type));
   new_mro = mro_invoke(type);              /* might cause reentrance */
   reent = (lookup_tp_mro(type) != old_mro);
   ```
   Keeps a reference across the callback *and* compares on return. `object_set_class` does
   neither.

**Fix.** Move the DECREF next to the `Py_SET_TYPE` and read the outgoing type immediately
before it, inside `object_set_class_world_stopped`:

```c
        PyTypeObject *cur = Py_TYPE(self);         /* re-read AFTER the callback */
        if (newto->tp_flags & Py_TPFLAGS_HEAPTYPE) { Py_INCREF(newto); }
        Py_SET_TYPE(self, newto);
        if (cur->tp_flags & Py_TPFLAGS_HEAPTYPE) { Py_DECREF(cur); }
```
and drop the `oldto` DECREF at :7832. That closes both the over-DECREF and the leak. The
stronger fix (which also closes §4 and CPY-0078) is to remove the callback entirely:
normalize `__slots__` names to exact `str` at type creation, or compare `ht_slots` with
`_PyUnicode_Equal` element-wise instead of `PyObject_RichCompareBool`. Given that `__slots__`
names are already required to be identifiers and are already `_Py_Mangle`d, normalizing them
to exact `str` costs nothing and eliminates a whole family of re-entrancy in this region.

**Not previously reported.** `gh api search/issues` over
`__class__ assignment reentrant refcount`, `object_set_class oldto`,
`__slots__ __eq__ __class__ crash` returns nothing; the findings catalog has CPY-0078 on the
*same call site* but only for the tri-state collapse (`kind: abort`, `category: pyerr-clear`).
This is a distinct, more severe consequence of the same window.

---

## 4. [FIX] `__class__` assignment runs arbitrary Python with the world stopped

**Site:** `Objects/typeobject.c:7821-7829` `object_set_class`.

The same `same_slots_added` :7609 callback runs **inside the `types_stop_world()` region**.
No re-entrancy is needed — a single plain `o.__class__ = B` is enough, provided `o` is not
uniquely referenced (which is the normal case: a local variable plus the evaluation stack).

`Python/ceval.h:140-149` already asserts against exactly this, and the assert's own comment
names the culprit:

```c
static void
check_invalid_reentrancy(void)
{
#if defined(Py_DEBUG) && defined(Py_GIL_DISABLED)
    // In the free-threaded build, the interpreter must not be re-entered if
    // the world-is-stopped.  If so, that's a bug somewhere (quite likely in
    // the painfully complex typeobject code).
    PyInterpreterState *interp = _PyInterpreterState_GET();
    assert(!interp->stoptheworld.world_stopped);
#endif
}
```

**Reproduced:**

| build | script | outcome |
|---|---|---|
| `debug-ft-nojit` | `setclass_stw.py` (no re-entrancy, 20 lines) | `Assertion '!interp->stoptheworld.world_stopped' failed` — **hard abort** |
| `release-ft-nojit` / `release-ft-nojit-asan` | same | user `__eq__` executes with the world stopped; silently "succeeds" |
| `release-ft-nojit` | `setclass_stw_threads.py` (nested assignment + one live spinning thread) | **HANG** — killed at 30 s. Nested `types_stop_world()` → `_PyEval_StopTheWorld` with the world already stopped |
| `release-ft-nojit` | `setclass_reentry.py` | **HANG** — killed at 30 s (and at 120 s on the first run) |
| GIL builds | any | `types_stop_world()` is `#define`d away at :193-195, so no STW effect |

So on free-threaded builds this is a debug abort and a **release deadlock**, not merely a
theoretical violation.

**Guarded twins:** CPY-0073 (`_PyType_SetFlagsRecursive` :6522) and CPY-0082 are the same
class already recorded from pass 1 — *allocating* with the world stopped. This is worse:
it *executes user Python* with the world stopped. The correctly-shaped code in this file is
`set_tp_mro` :667-675, which wraps only the plain pointer store in
`type_lock_prevent_release(); types_stop_world(); ...; types_start_world();` and does nothing
fallible inside.

**Fix.** `compatible_for_assignment` performs no mutation — it is a pure predicate. Hoist it
(and the managed-dict materialization at :7766-7785, which allocates) **out** of the stopped
region: run the compatibility check and the dict detach first, then `types_stop_world()`
around only the `Py_INCREF` / `Py_SET_TYPE` / `Py_DECREF` triple. Re-validate `Py_TYPE(self)`
inside the region (which is also the §3 fix). Removing the `PyObject_RichCompareBool`
callback per §3's stronger fix would close this too.

**Note the two findings compose:** the same three-class script gives a UAF on GIL builds and
a deadlock on FT builds.

---

## 5. Lookup cache 6140-6452 — dedicated read

The task's three questions, answered.

**What owns the entries.** Nothing is heap-allocated per entry. The cache is a fixed
`struct type_cache_entry hashtable[1 << MCACHE_SIZE_EXP]` living inside
`interp->types.type_cache` (`pycore_interp_structs.h`). Per entry:

| field | ownership |
|---|---|
| `version` (uint32) | plain value |
| `name` | **strong reference** — `Py_NewRef(name)` at :6224; the displaced one is returned to the caller and DECREF'd at :6383 (GIL) / :6256 (FT). Initialized to `Py_None` at `_PyType_InitCache` :1009 so `Py_SETREF` is always legal |
| `value` | **borrowed** — `/* borrowed */` at :6218 |
| `sequence` | FT seqlock |

**When they are freed.** Never individually. `type_cache_clear` :980-995 (from
`_PyType_ClearCache` / `PyType_ClearCache` / `_PyTypes_Fini`) walks all slots, sets
`version = 0` **first**, then `Py_XSETREF`s the name and NULLs the value. That ordering is the
mirror image of the install ordering and is correct: on install the version is written **last**
(:6225-6230, "We must write the version last"), on teardown it is written **first**. I checked
both and they agree. `_PyTypes_AfterFork` :6261-6276 (FT only) repairs entries caught
mid-write by a fork, in the same version-first order.

**Can a stale entry be read after the type it names is deallocated?** No — the borrowed
`entry->value` at :6343 / :6321 is reachable only when `entry->version == type->tp_version_tag`
for the type being looked up, and **version tags are never reused**:

- static/immutable types draw from `next_global_version_tag()` :1394-1405, a monotonic
  CAS-increment that returns 0 (refusing to allocate) at `_Py_MAX_GLOBAL_TYPE_VERSION_TAG`;
- heap types draw from `NEXT_VERSION_TAG(interp)++` :1447-1452, with an explicit `== 0`
  exhaustion check before the post-increment.

Neither wraps into a live value. So a dead type's version can never match a live type's, and
the value is kept alive by the MRO dict it was found in for as long as that match can happen
(any removal from any MRO dict runs `_PyType_Modified_Unlocked`, which recurses into
`tp_subclasses` and zeroes every dependent version). **ACCEPTABLE — confident bounded
negative, guard cited.** This is the region's real invariant and it holds.

`_PyType_Lookup` :6399-6405 (`LookupRef` then `Py_XDECREF` then return the pointer) is a
deliberate borrowed-reference API, safe for the same reason: the value is owned by a type
dict.

### 5.1 [CONSIDER] `type_dealloc` leaves a dangling borrowed pointer in `interp->types.type_version_cache`

A **second**, separate cache keyed on the same version numbers:

```c
/* set_version_unlocked, :1140-1162, non-FT only */
    if (tp->tp_version_tag != 0) {                 /* clear the OLD slot */
        interp->types.type_version_cache[tp->tp_version_tag % TYPE_VERSION_CACHE_SIZE] = NULL;
    }
    ...
    if (version != 0) {                            /* publish the NEW slot */
        interp->types.type_version_cache[version % TYPE_VERSION_CACHE_SIZE] = tp;
    }
```
```c
/* _PyType_LookupByVersion, :1378-1385 */
    PyTypeObject **slot = interp->types.type_version_cache + (version % TYPE_VERSION_CACHE_SIZE);
    if (*slot && (*slot)->tp_version_tag == version) {   /* DEREFERENCES *slot */
        return *slot;
    }
```

The slot holds a **raw, non-owning** `PyTypeObject *` (`pycore_interp_structs.h:655-658`
documents it as such). `type_dealloc` :6977-7045 **never** calls `set_version_unlocked(type, 0)`
and has no `assert(type->tp_version_tag == 0)`. The only thing that clears the slot on the way
out is `type_clear` :7229 → `PyType_Modified(type)` :7266, and `type_clear` is a `tp_clear`
slot — it runs only when the **GC** breaks the type's `tp_mro` self-cycle.

Today that coupling holds: a versioned type is by construction `Py_TPFLAGS_READY`
(`assign_version_tag` :1419-1421 refuses otherwise), a ready type always has a `tp_mro` tuple
whose element 0 is the type itself, so every versioned heap type is cyclic and every versioned
heap type is therefore reclaimed through `type_clear`. But nothing *enforces* it, the consumer
(`Python/optimizer_symbols.c:385`, `:695`) dereferences the slot unconditionally, and the
failure mode is a use-after-free read that can return a bogus `PyTypeObject *` to the Tier-2
optimizer if the freed memory happens to still hold the matching version word.

**Not reproduced.** I stressed 4,000 short-lived versioned heap types under
`release-gil-jit-asan` and `debug-gil-jit-asan` with `-X uops`
(`scratchpad/version_cache_stress.py`); ASan is silent. I could not drive the optimizer to
`_PyType_LookupByVersion` with a dead type's version.

**Classification: CONSIDER** (unenforced invariant, one-line hardening). Add to `type_dealloc`,
next to the existing `_PyObject_GC_UNTRACK(type)` at :7012:
```c
    assert(type->tp_version_tag == 0);   /* type_clear must have run */
```
or, defensively, `_PyType_SetVersion(type, 0);`. **Guarded twin:** `_PyType_Modified_Unlocked`
:1233 does exactly this (`set_version_unlocked(type, 0)`) for every *other* way a type stops
being cacheable; the destruction path is the one exit that skips it.

---

## 6. GC-track invariant across the pass-2 regions — [ACCEPTABLE], with one note

Denominator: `_PyObject_GC_TRACK` ×3 (:2565, :2797/:2818/:2867 in `subtype_dealloc`, :11350),
`_PyObject_GC_UNTRACK` ×4 (:2802, :2824, :7012, :11268, :12560), `PyObject_GC_UnTrack` ×1
(:2785), `_PyObject_GC_Link` ×1 (:2538).

- **`type_dealloc` :7012 `_PyObject_GC_UNTRACK(type)`** — the unchecked macro, on an object
  that is always tracked: `PyType_GenericAlloc` :2564-2566 tracks inside `tp_alloc`, before
  the constructor can fail. The OOM shape this agent hunts (freed on an error path before
  `PyObject_GC_Track`, then the macro in `tp_dealloc` — CPY-0011 / CPY-0015 / gh-152107
  OOM-0006) **cannot occur**: there is no window in which a type object exists untracked.
  Confirming pass 1 §5, not re-litigating.
- **`type_dealloc`'s watcher notification :6988-7010** is correctly bracketed by
  `_PyObject_ResurrectStart` / `_PyObject_ResurrectEnd`, and it runs **before**
  `_PyObject_GC_UNTRACK` — i.e. the object is still tracked while a watcher callback can run
  arbitrary code, which is the right order (an untracked-but-live object would be invisible to
  a collection triggered from the callback). The comment at :6985-6987 is accurate.
- **`subtype_dealloc` :2783-2824** is the textbook untrack/retrack dance around finalizers,
  with the load-bearing comment at :2804-2812. Every `_PyObject_GC_TRACK` is paired, and both
  resurrection early-returns (`:2800`, `:2822`) leave the object **tracked**, which is correct.
- **`object_set_class` changes an object's type while it is tracked** (:7790). Safe: the GC
  dispatches on `Py_TYPE(op)->tp_traverse`, and `compatible_for_assignment` has already
  required identical `Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_INLINE_VALUES | Py_TPFLAGS_PREHEADER`
  (:7643-7646), identical `tp_dictoffset` / `tp_weaklistoffset` (:7655-7664), identical
  `tp_free` (:7634), and layout equivalence up to the solid base — so the new `tp_traverse`
  reads the same fields at the same offsets, and no generation relink is needed. The *refcount*
  half of that same statement is §3.

---

## 7. Mismatched alloc/free families — [ACCEPTABLE], zero, inventory re-verified

I re-ran the full-file primitive inventory rather than trusting pass 1. `grep` for
`PyMem_* | PyObject_Malloc/Free/Realloc | malloc | free | realloc | calloc | alloca |
memcpy | memmove | memset | strcpy | strcat | sprintf | snprintf` over all 13,068 lines
yields exactly pass 1's seven allocation lifetimes, all family-consistent (pymem↔pymem,
pyobject↔pyobject), plus:

- **the entire pass-2 range 7722→8596 contains no allocator call at all** — the pickle
  region (`_PyObject_GetNewArguments`, `_PyObject_GetItemsIter`, `reduce_newobj`,
  `_common_reduce`, `object___reduce_ex___impl`) has zero raw-memory surface. Its risks are
  refcount/error-path, not mine. I did read `reduce_newobj` :8221-8316 for the
  borrowed-`Py_TYPE(obj)`-across-callback shape: :8264 and :8280 both read `Py_TYPE(obj)` and
  `Py_NewRef`/`PyTuple_Pack` it in the same expression, after `__getnewargs_ex__` may have
  reassigned `__class__`. The value may be semantically stale but is never dangling — `obj`
  owns its current type. Clean.
- **managed static types 228-522 have no allocator either.** The state lives in fixed arrays
  (`interp->types.builtins.initialized[]`, `.for_extensions.initialized[]`,
  `_PyRuntime.types.managed_static.types[]`); `managed_static_type_state_init` / `_clear`
  only bump counters and null pointers. There is nothing here to mismatch.
  One latent nit (**ACCEPTABLE**, not worth a record): `managed_static_type_state_get` :294
  tests `index > _Py_MAX_MANAGED_STATIC_EXT_TYPES` where `>=` is meant, and it indexes
  `builtins.initialized[index]` at :290 *before* any bound test. Both are dead today because
  `_Py_MAX_MANAGED_STATIC_EXT_TYPES` is 10 while `_Py_MAX_MANAGED_STATIC_BUILTIN_TYPES` is the
  (much larger) static-builtin count, so an ext index is always in range for both arrays.
  CPY-0089 already records :709 in this same function.
- `char buf[1000]` :3313 in `set_mro_error` — every write is `PyOS_snprintf` with a correctly
  decremented remaining size, and the two manual `buf[off++]` writes at :3352-3353 are guarded
  by `(size_t)(off+1) < sizeof(buf)`. Bounded.
- `sprintf(msg, ...)` into `char msg[200]` :7205-7206 — 43-byte literal plus one `%.100s`,
  max 143+NUL, on the `_PyObject_ASSERT_FAILED_MSG` abort path. Bounded. PEP 7 nit at most;
  confirming pass 1, not re-litigating.

---

## 8. MRO C3 3217-3702 — [ACCEPTABLE]

- **`pmerge` :3360-3428.** `remain = PyMem_New(Py_ssize_t, to_merge_size)` / `PyMem_Free`
  :3425 — one exit, matched family. `to_merge[]` holds **borrowed** references to each base's
  `tp_mro`. The only call inside the merge loop is `PyList_Append` :3404, which reaches
  `PyMem_Realloc` on the list's `ob_item` and cannot trigger a GC collection (the collector is
  scheduled from `_PyObject_GC_Alloc`, not from the raw/pymalloc paths) and cannot run Python.
  So the borrowed array cannot be invalidated mid-merge. Bounded negative.
- **`mro_implementation_unlocked` :3430-3516.** `PyMem_New(PyObject *, n + 1)` :3488 with
  `PyMem_Free` at :3505 **and** :3513 — not a double free: the :3505 free is inside
  `if (result == NULL) { ...; return NULL; }` and returns. `check_duplicates` :3476 can run
  Python via `class_name` → `PyObject_GetOptionalAttr(cls, __name__)` (:3262 flags it), but
  only on the branch that immediately `return -1`s, i.e. before `to_merge` exists. The
  `n == 1` fast path :3453-3473 builds `PyTuple_New(k + 1)` from `PyTuple_GET_SIZE(base_mro)` —
  bounded-by-existing-allocation, no overflow (`k + 1` on a `Py_ssize_t` that is a live tuple's
  size).
  *Cross-agent:* :3443 is on the NET NEW list as `borrowed_field_deref_across_call` (high) for
  `scan_refcounts`; from a memory standpoint the borrowed `bases` is not dereferenced across
  anything that can run Python on the success path, so I do not claim it.
- **`mro_internal` :3654-3701 is the guarded twin of §3** and is itself correct. The
  `Py_XNewRef` / `Py_XDECREF` pair at :3665/:3668 is a re-entrancy *detector*, not an
  ownership transfer: `set_tp_mro` :650-676 is a **plain store** that does not DECREF the old
  value, so the reference handed to `*p_old_mro` at :3696 is the type's original one. The
  comment at :3662-3664 ("Don't let old_mro be GC'ed and its address be reused") is exactly
  right, and the `reent` comparison at :3667 only compares pointer identity, never
  dereferences. Balanced; no double-free, no UAF.

---

## 9. getattro / setattro 6529-6848 — [CONSIDER], one note

`_Py_type_getattro_stackref` :6570-6689 is clean for my shape: all three intermediate handles
are `_PyCStackRef`s pushed on the thread state (GC-visible) and popped on the single `done:`
exit, so a descriptor `__get__` that triggers a collection cannot see an untracked live
reference.

**[CONSIDER] `type_update_dict` :6693-6723 documents an invariant its own callee breaks.**

```c
    // We don't want any re-entrancy between when we update the dict
    // and call _PyType_Modified_Unlocked, including running the destructor
    // of the current value ... we know that this will all complete without
    // releasing the locks.
    if (_PyDict_GetItemRef_Unicode_LockHeld(dict, name, old_value) < 0) { return -1; }
    _PyType_Modified_Unlocked(type);                       /* :6712 */
    if (_PyDict_SetItem_LockHeld(dict, name, value) < 0) { /* :6714 */
```

`_PyType_Modified_Unlocked` :1211-1231 invokes every registered **type watcher callback**
(`cb(type)`) and `PyErr_FormatUnraisable`, whose own comment at :1219-1220 says both are
"potentially re-entrant". So arbitrary C — and, through a watcher that calls back into Python,
arbitrary Python — runs between the read at :6703 and the write at :6714, on a `dict` pointer
borrowed from `type->tp_dict` at :6812. Today this is not exploitable: `type.__dict__` is
read-only from Python, `tp_dict` is only replaced during `type_new`/`type_ready`/`type_dealloc`,
and the caller of `type_setattro` holds a strong reference to `type`, so `dict` cannot be freed
under the write. `PyType_AddWatcher` is C-API-only. **CONSIDER**, not FIX: the comment asserts
an invariant ("this will all complete without releasing the locks") that is false as written,
and the FT build's `BEGIN_TYPE_DICT_LOCK(dict)` makes a re-entrant `type_setattro` from a
watcher a lock-discipline question I am leaving to that agent.

---

## 10. Confirmations (one line each, no re-litigation)

- **CPY-0078** (`same_slots_added:7609` tri-state collapse) — still present; it is the *entry
  window* for both §3 and §4, and it is also what turns an unbounded re-entrant `__eq__` into a
  silent `TypeError` (which is why the §3 reproducer needs a one-shot flag).
- **CPY-0088** (`-spec->basicsize` `INT_MIN` at :5290) — unchanged.
- **CPY-0089** (`managed_static_type_state_get` :709) — unchanged; see the sibling nit in §7.
- **CPY-0012 / CPY-0013** (`structseq.c`) — the scanner's own exemplar still fires
  (`PyStructSequence_New:77`, `low`, `nitems: size`, `dispatch: direct`), verified by running
  `scan_memory_patterns.py` on `Objects/structseq.c` this pass.

---

## Reproduction artifacts

All under
`/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`:

| file | what |
|---|---|
| `setclass_min.py` | §3 minimal — refcount deltas + `gc_decref` abort on `debug-gil-nojit` |
| `setclass_reentry.py` | §3 full — SIGSEGV on `release-gil-nojit`, negative-refcount abort on `debug-gil-nojit`, ASan `heap-use-after-free` on `release-gil-nojit-asan`, hang on `release-ft-nojit` |
| `setclass_stw.py` | §4 minimal, no re-entrancy — `check_invalid_reentrancy` abort on `debug-ft-nojit` |
| `setclass_stw_threads.py` | §4 with a live second thread — hang on `release-ft-nojit` |
| `version_cache_stress.py` | §5.1 — 4,000 short-lived versioned heap types under `-X uops` on both JIT+ASan builds; **negative** |
| `varobj_gap.py` | toolkit-assessment harness for the recall gap below; synthetic, five cases |

### Proposed catalog records (IDs to be assigned by the coordinator — other pass-2 agents are running concurrently)

```
slug   object-set-class-stale-oldto-over-decref
kind   crash            status reproduced      category memory-pattern
sites  Objects/typeobject.c:7825 object_set_class  (oldto captured before a Python-running call)
       Objects/typeobject.c:7832 object_set_class  (Py_DECREF of the stale oldto)
       Objects/typeobject.c:7790 object_set_class_world_stopped (Py_SET_TYPE drops the current type's ref)
       Objects/typeobject.c:7609 same_slots_added  (the PyObject_RichCompareBool that runs user code)
guarded_twin  Objects/typeobject.c:2860 subtype_dealloc re-reads Py_TYPE(self) after tp_del
              ("Extract the type again; tp_del may have changed it"); Objects/typeobject.c:3667
              mro_internal compares lookup_tp_mro(type) against the pre-call value

slug   object-set-class-runs-python-with-world-stopped
kind   hang/abort       status reproduced      category ft-race
sites  Objects/typeobject.c:7823 object_set_class (types_stop_world)
       Objects/typeobject.c:7763 object_set_class_world_stopped (compatible_for_assignment)
       Objects/typeobject.c:7609 same_slots_added
guarded_twin  Objects/typeobject.c:667-675 set_tp_mro stops the world around a plain pointer
              store only; CPY-0073 / CPY-0082 are the recorded siblings of this class
```

---

## TOOLKIT ASSESSMENT

### Precision of the new VarObject rule on this file

**2 findings, 0 noise, 0 FIX.** Both are genuine unguarded `_PyObject_VAR_SIZE` multiplies
that a reader must resolve — neither is a false positive in the "this isn't the pattern" sense.
After triage, one is CONSIDER (C-caller-controlled trunk, `PyType_GenericAlloc`) and one is
ACCEPTABLE-with-a-CONSIDER (already pass 1's §2). So:

- precision as *"the pattern really is present and unguarded"*: **2/2**
- precision as *"this is a bug"*: **0/2**

That is the right ratio for this rule on this file, and the rule earned its keep: pass 1's
scanner reported a **structural zero** here and pass 1 had to find the sites by hand. The
`_TP_ALLOC_SLOT_RE` slot-pointer modelling from issue #28 is the whole reason the file has any
var-object visibility, and `via_slot_pointer=5` is exactly correct. The `dispatch` field is
useful and correct on both findings.

### The true census split

`sites=7 ✓`, `via_slot_pointer=5 ✓`, `non_constant_nitems=2` → **should be 3**. Full per-site
table in §2. The missed site is `:4413` `metatype->tp_alloc(metatype, ctx->nslot)`.

### RECALL GAP (found by reading, reproduced synthetically) — a struct-field `nitems` is silently treated as a literal

`_check_varobject_nitems` computes `atoms = list(_arith_atoms(nitems))` and
`idents = [text for kind, n in atoms if kind == "identifier"]`, then:

```python
        # A literal count, or one built only from small constants, is fine.
        if not idents and all(
            kind != "call"
            and (kind != "number_literal" or _small_constant(n, source_bytes))
            for kind, n in atoms
        ):
            continue
        if census is not None:
            census["non_constant_nitems"] += 1
```

`_arith_atoms` has no case for `field_expression` or `subscript_expression`, so both fall to
the terminal `yield ("other", node)`. The gate then sees `idents == []` and an atom list whose
only kinds are `"other"` — which is neither `"call"` nor `"number_literal"` — so `all(...)` is
`True` and the site is dropped **before** the census counter, i.e. it vanishes from the
findings *and* from the denominator. A zero next to a silently-shrunk denominator is exactly
the failure mode `RUN_CONTEXT.md` warns about.

Reproduced on five synthetic one-liners (`scratchpad/varobj_gap.py`):

```
census: {'sites': 5, 'via_slot_pointer': 5, 'non_constant_nitems': 1}
  bare_identifier      reported=True
  field_expression     reported=False      <-- ctx->nslot
  array_subscript      reported=False      <-- sizes[i]
  field_times_two      reported=False      <-- ctx->nslot * 2   (!)
  literal              reported=False
```

`ctx->nslot * 2` being dropped is the sharpest form of the defect: an explicit multiply by a
struct field is discharged as a constant.

**Why it matters beyond this file.** A struct field is the *normal* carrier for a count in
CPython (`self->size`, `st->n`, `ctx->nslot`, `state->count`), and it is the shape closest to
the class's own exemplar — `PyStructSequence_New` escapes only because `size` happens to be
copied into a bare local first. Had structseq written `PyObject_GC_NewVar(PyStructSequence,
type, REAL_SIZE_TP(type))` inline, the rule would not fire on its own catalog exemplar.

**Concrete tuning proposal (two parts, both small):**

1. Make the literal gate *positive* instead of *negative* — accept only atoms that are provably
   constant, rather than rejecting a blocklist of three kinds:
   ```python
   if not idents and all(
       kind == "sizeof"
       or (kind == "number_literal" and _small_constant(n, source_bytes))
       for kind, n in atoms
   ):
       continue
   ```
   This alone fixes the census and surfaces `:4413`.
2. Teach `_arith_atoms` about `field_expression` and `subscript_expression`, yielding a
   `("member", node)` atom whose text is the full expression (`ctx->nslot`). Then extend
   `_collect_taint` / `_bounded_by_existing_allocation` to accept a member atom bounded the
   usual way, so `:4413` (`ctx->nslot`, provably `PyTuple_GET_SIZE` of a just-materialized
   tuple — pass 1 §1) can still be discharged by the existing bounded rule rather than becoming
   permanent noise, and so the `operands` field stays populated for triage.

Expected net effect on `Objects/`: `non_constant_nitems` rises by the number of struct-field
`nitems` sites (3 → 1 new finding in this file, `:4413`, immediately ACCEPTABLE-by-pass-1's-trace),
plus whatever the sibling slices carry. Precision cost is low; denominator honesty gain is large.

### Second, smaller gap — no rule sees §3 or §4

Neither of this pass's two FIX findings came from a scanner. Both are the shape
"**a value captured before a call that can run Python is used after it**", which
`scan_refcounts`' `borrowed_field_deref_across_call` models for *fields* but not for
`Py_TYPE(x)` captured into a local. `object_set_class`'s `oldto` is a local holding a type
pointer across `object_set_class_world_stopped`. A cheap extension with a real precedent in
this file (`subtype_dealloc:2860` re-reads; `mro_internal:3667` compares) would be:

> flag a local assigned from `Py_TYPE(...)` / `Py_SIZE(...)` / `lookup_tp_*(...)` that is
> **used after** an intervening call which the callee-classifier says can run Python, unless
> the function re-reads or compares the same expression in between.

`same_slots_added`'s `PyObject_RichCompareBool` is already known to the toolkit as a
Python-running callee (CPY-0078 was recorded from exactly this call), so the "can run Python"
side of the rule needs no new vocabulary — only the local-capture side is missing.

### Denominator note

`functions_analyzed = 459` for this file, `varobject_allocation_census.sites = 7`. Both are
honest and slice-scoped; the only compromised denominator is `non_constant_nitems`, addressed
above.
