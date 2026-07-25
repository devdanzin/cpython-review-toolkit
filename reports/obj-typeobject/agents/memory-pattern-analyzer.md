# Memory Pattern Analysis Results — slice `obj-typeobject`, PASS 1

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c` only (13,068 lines), plus the headers/helpers it
reaches for the allocation arithmetic (`Include/cpython/objimpl.h`,
`Python/slots.c`, `Include/internal/pycore_slots_generated.h`,
`Python/structmember.c`).
**Mode:** INFORMED. Briefing read; FP taxonomy section "Memory patterns (v0.8
additions)" applied.

**Build-vs-ref check (required by RUN_CONTEXT):** the build matrix source tree
is at `a1d580430c8`. `git diff 4f3be1b5777 a1d580430c8` over
`Objects/typeobject.c`, `Include/cpython/objimpl.h`, `Python/slots.c`,
`Include/internal/pycore_slots_generated.h`, `Objects/structseq.c` is **empty** —
every line number cited below is valid on both, and every reproduction below
runs the exact code reviewed.

---

## Summary

| category | count |
|---|---|
| varobject_nitems_unguarded (scanner) | 0 reported / **2 real sites, both invisible to the scanner** |
| alloc_size_overflow (scanner) | 0 |
| gc_untrack_without_track (scanner) | 0 |
| mismatched_alloc_free (scanner) | 0 |
| Hand-checked (type-dict sizes / UAF / double-free / memcpy / buffer) | 6 allocation lifetimes, 4 error ladders, 3 memcpy sites, 1 sprintf |
| **FIX** | 0 in my shape |
| **CONSIDER** | 3 (unconstrained metatype `tp_alloc`; `INT_MIN` negation; remote-guard robustness) |
| **ACCEPTABLE (bounded negatives, with the guard cited)** | 4 |
| **Cross-agent leads logged** | 2 (one FIX-grade, reproduced) |
| **CPython test-coverage gap found** | 1 (`PyType_FromSlots` has no repeated-slot test) |
| **Toolkit recall gap found** | 1, quantified corpus-wide (15 sites) |

---

## 0. Denominator check — the zero is NOT clean, it is blind

`scanners/scan_memory_patterns.sample.json`: `files_analyzed: 1`,
`functions_analyzed: 417`, `findings: []`. The denominator is non-zero, so the
canary works in the usual sense. **But it is structurally blind to this file's
only two variable-length-object allocation sites.**

Both go through the `tp_alloc` **slot pointer**:

- `Objects/typeobject.c:4413` — `metatype->tp_alloc(metatype, ctx->nslot)`
- `Objects/typeobject.c:5623` — `metaclass->tp_alloc(metaclass, nmembers)`

`scan_memory_patterns.py`'s `_VAROBJ_NITEMS_INDEX` (lines 454-460) matches
exactly six literal spellings: `PyObject_NewVar`, `PyObject_GC_NewVar`,
`PyObject_GC_Resize`, `_PyObject_NewVar`, `_PyObject_GC_NewVar`,
`_PyObject_GC_Resize`. An indirect call through `->tp_alloc` matches none of
them, and neither does `PyType_GenericAlloc` / `_PyType_AllocNoTrack`, which is
where the multiply actually lives:

```c
/* Objects/typeobject.c:2521, _PyType_AllocNoTrack */
size_t size = _PyObject_VAR_SIZE(type, nitems+1);
```

```c
/* Include/cpython/objimpl.h:23-27 — the invisible multiply */
static inline size_t _PyObject_VAR_SIZE(PyTypeObject *type, Py_ssize_t nitems) {
    size_t size = _Py_STATIC_CAST(size_t, type->tp_basicsize);
    size += _Py_STATIC_CAST(size_t, nitems) * _Py_STATIC_CAST(size_t, type->tp_itemsize);
    return _Py_SIZE_ROUND_UP(size, SIZEOF_VOID_P);
}
```

This is the *same* macro-hidden multiply that CPY-0012 (`structseq.c`) exploits.
The scanner catches it there only because structseq spells the allocator
`PyObject_GC_NewVar`. Reached through `tp_alloc` it is invisible.

### Quantifying the gap corpus-wide (toolkit feedback)

```
132   indirect `tp_alloc(` call sites in Objects/ + Modules/ + Python/
 15   of those pass a non-constant nitems   <-- the actual recall gap
 23   named  _?PyObject_(GC_)?(NewVar|GC_Resize)( call sites  (what the scanner sees)
 18   direct PyType_GenericAlloc( call sites (also unseen)
```

The 15 non-constant-`nitems` `tp_alloc` sites:

| file:line | nitems expression | my read |
|---|---|---|
| `Objects/typeobject.c:4413` | `ctx->nslot` | bounded (this report, §1) |
| `Objects/typeobject.c:5623` | `nmembers` | bounded (this report, §2) |
| `Objects/tupleobject.c:848` | `PyTuple_GET_SIZE(tmp)` | bounded-by-existing-allocation |
| `Objects/bytesobject.c:3191` | `n` | (other slice) |
| `Objects/longobject.c:6036` | `ndigits` | (other slice) |
| `Modules/_datetimemodule.c:1226,1331,4686,5463` | `aware` (0/1 flag) | bounded |
| `Modules/_datetimemodule.c:3847` | `3` | literal |
| `Modules/_cursesmodule.c:1275,1443,1518` | `len`, `n`, `slicelen` | (other slice) |
| `Modules/_cursesmodule.c:1544` | `Py_SIZE(sa) + Py_SIZE(sb)` | **sum** of two existing-allocation bounds — the current filter handles a product, not a sum |
| `Modules/_testcapi/heaptype.c:1297` | `size` | test code |

**Concrete tuning proposal.** Add to `_VAROBJ_NITEMS_INDEX`:
`"tp_alloc": 1` (receiver-matched on `->tp_alloc(` / `.tp_alloc(`, the same
receiver-matching trick the family already uses elsewhere),
`"PyType_GenericAlloc": 1`, `"_PyType_AllocNoTrack": 1`. Precision cost is low:
117 of the 132 `tp_alloc` sites pass the literal `0`, which the existing
literal filter drops, and most of the remaining 15 are already covered by the
`bounded-by-an-existing-allocation` rule. Net new noise on `Objects/` should be
~2 findings, both of which this report classifies. Also worth adding: treat a
**sum** of two bounded lengths as bounded (`_cursesmodule.c:1544`), otherwise
that one site becomes recurring noise.

---

## 1. [ACCEPTABLE — bounded negative] The `nslots` trace: `type_new` → `tp_alloc`

This was the assignment's highest-value target. Full trace, with the arithmetic
at every step.

### The chain

| step | site | what happens |
|---|---|---|
| 1 | `type_new_get_slots` :4866-4892 | `slots = PyDict_GetItemWithError(dict, &_Py_ID(__slots__))`. `str` → `PyTuple_Pack(1, slots)`; else `PySequence_Tuple(slots)`. `assert(PyTuple_CheckExact(new_slots))`. `ctx->nslot = PyTuple_GET_SIZE(new_slots)` :4892 |
| 2 | `type_new_visit_slots` :4212-4239 | every element must pass `valid_identifier` (⇒ `PyUnicode_Check` + `PyUnicode_IsIdentifier`), so each is a live `str` object. `__dict__`/`__weakref__` bump `add_dict`/`add_weak`, each at most once (a second occurrence is a `TypeError`) |
| 3 | `type_new_copy_slots` :4248-4312 | `new_nslot = nslot - add_dict - add_weak`; `PyList_New(new_nslot)`; filter + `_Py_Mangle` + `PyDict_Contains`; `PyList_Sort`; `PyList_AsTuple`; `assert(PyTuple_GET_SIZE(tuple) == new_nslot)` :4309 |
| 4 | `type_new_slots_impl` :4371 | **`ctx->nslot = PyTuple_GET_SIZE(new_slots)`** — re-read from the *new* tuple, so the count and the tuple can never disagree |
| 5 | `type_new_alloc` :4413 | `metatype->tp_alloc(metatype, ctx->nslot)` |
| 6 | `_PyType_AllocNoTrack` :2521 | `size = _PyObject_VAR_SIZE(type, nitems+1)` = `metatype->tp_basicsize + (nslot+1) * metatype->tp_itemsize`, all in `size_t` |
| 7 | :2542 / :2548 | whole allocation past the `PyObject` header memset to 0; `_PyObject_InitVar(obj, type, nitems)` writes `ob_size = nslot` |
| 8 | `type_new_init` :4919-4926 | `et->ht_slots = ctx->slots` — **the same tuple whose size was used at step 5** |
| 9 | `type_new_descriptors` :4655-4700 | writes `nslot = PyTuple_GET_SIZE(et->ht_slots)` `PyMemberDef`s; `type->tp_basicsize = base->tp_basicsize + nslot*sizeof(PyObject*)` :4699 |

### Can any step overflow?

- **`nslot < 0` is impossible.** It is `PyTuple_GET_SIZE` of an exact tuple at
  every one of the three points it is assigned (:4892, :4371, and re-derived at
  :4657). `PySequence_Tuple` materializes; a lying `__len__` cannot help,
  because the tuple is built by iteration.
- **`nslot * tp_itemsize` (step 6).** `metatype->tp_itemsize` is
  `sizeof(PyMemberDef)` = 40 on LP64 (`PyType_Type` :7294; inherited by every
  metaclass). Wrapping `size_t` needs `nslot > 2^64/40 ≈ 4.6e17`; the tuple that
  produced `nslot` occupies `≥ nslot*8` bytes ≈ **3.7 EiB**.
- **`base->tp_basicsize + nslot*8` (step 9, `Py_ssize_t`).** Overflow needs
  `nslot > 2^60`, i.e. an **8 EiB** tuple.
- **Both are the `bounded-by-an-existing-allocation` FP class**, in its
  strongest form: the count is not merely *a* container's length, it is the
  length of a tuple this very function materialized two calls earlier.
- The `assert` at :4309 is debug-only but is not load-bearing: `PyList_Sort`
  restores `ob_item`/`ob_size` and raises `list modified during sort` if a
  hostile `__lt__` mutates the list, so the post-sort length is invariant.

### Does typeobject.c have the guard structseq lacks?

**It does not have — and does not need — an arithmetic guard.** There is no
`PY_SSIZE_T_MAX / sizeof(...)` division check and no `__builtin_mul_overflow`
anywhere in the file. Saying "typeobject.c has the division guard structseq
lacks" would be false. What it has instead are **two ownership guards**, and
those are the actual difference:

**(a) The count is read once, at creation, and never re-read from a mutable
slot.** `PyStructSequence_New` (`structseq.c:77`) re-reads `n_fields` out of the
type's Python-writable `__dict__` on *every call* — that is CPY-0012.
`type_new` reads `__slots__` **once**, materializes it into `et->ht_slots`, and
from then on every consumer reads `et->ht_slots` or `Py_SIZE(type)`. The type
dict is never consulted again.

**(b) Every members-array walk is bounded by `Py_SIZE(type)`, not by the
count.** `traverse_slots` :2590, `clear_slots` :2665, `subtype_dealloc` :2839 —
all `n = Py_SIZE(type)`, the `ob_size` written by `_PyObject_InitVar` at
allocation time (:2548). **This is precisely the guard CPY-0013 shows structseq
lacking**: `structseq_new_impl` (`structseq.c:240-243`) walks `tp_members` with
the dict-read `n_fields` as its bound instead of `Py_SIZE`, which is why it
SEGVs in `strlen` with no integer overflow at all. `ob_size` on a type object
has **no Python-writable exposure**: `type_members` :1468-1477 marks
`__basicsize__`, `__itemsize__`, `__flags__`, `__weakrefoffset__`,
`__dictoffset__` all `Py_READONLY`, and `type` has no `__len__`.

**(c) Bonus — the CPY-0018 sibling also does not translate.** The members array
lives *inside* the type object's own varsize tail
(`_PyHeapType_GET_MEMBERS` :2578-2582 = `obj + Py_TYPE(obj)->tp_basicsize`), and
`PyDescr_NewMember` INCREFs the type, so a surviving member descriptor keeps
both the array **and** the `ht_slots` strings its `mp->name` points into alive.
`PyStructSequence_InitType2` (`structseq.c:700`) frees a *separately allocated*
`tp_members` while descriptors still hold `d_member` into it.

**(d) The `+1` at :2521 is load-bearing** and is the reason no count is needed
downstream: it guarantees a zeroed sentinel entry with `name == NULL`, which is
what `type_add_members` :8695-8701 (a pure NUL-terminator walk, no count) relies
on.

### Reproduction — the CPY-0012/0013 differential, applied to typeobject.c

`/tmp/claude-1000/.../scratchpad/cpy0012_differential.py`, run on
`debug-gil-nojit-asan`, `debug-ft-nojit-asan`, and `release-gil-nojit`:

```
after __slots__ = ('x','x',... 100000)  -> survived; basicsize 32
after __slots__ = 4611686018427387904   -> survived; basicsize 32
after __slots__ = -1                    -> survived; basicsize 32
after __slots__ = 'not a tuple'         -> survived; basicsize 32
readonly __basicsize__/__itemsize__/__dictoffset__/__weakrefoffset__/__flags__ -> AttributeError (all five)
structseq n_fields writable -> CPY-0012 shape live
```

Each mutation is followed by a full round of every members-array consumer
(instance construction, `gc.collect()` ⇒ `subtype_traverse`→`traverse_slots`,
attribute set/get, `del` ⇒ `subtype_dealloc`). **Zero ASan reports on either
sanitizer build.** The three-lines-of-Python attack that works on
`os.terminal_size` has no analogue here.

Additional adversarial probes (`scratchpad/slots_probe.py`, all three builds,
ASan silent): `__slots__` names that are `str` subclasses with hostile
`__hash__`/`__eq__`/`__lt__` (the `__lt__` allocates and forces GC *during*
`PyList_Sort`); `__slots__` supplied as a generator that collects
mid-iteration; duplicate slot names; a 200,000-entry `__slots__`
(`basicsize == object.__basicsize__ + n*8`, exactly); `tuple` subclasses with
slots at two inheritance levels (the `_Py_AFTER_ITEMS` path,
`Python/structmember.c:25-33`) — the layout invariant
`base_basicsize + ROUND_UP(n*itemsize) + nslot*8 ≤ basicsize + (n+1)*itemsize`
holds with exactly one item of slack, and the `base->tp_itemsize != 0 &&
!Py_TPFLAGS_TUPLE_SUBCLASS` rejection at :4660-4666 is what pins `itemsize` to
8 on that path.

**Verdict: ACCEPTABLE.** Confident bounded negative, with the guard cited.

**CPY-0012 / CPY-0013 confirm (one line, no re-litigation):** both still
reproduce at `4f3be1b5777` — `os.terminal_size.n_fields = 8` still succeeds on
all three builds.

---

## 2. [ACCEPTABLE, with a CONSIDER robustness note] `type_from_slots_or_spec` :5248-5801

> **Coordinator lead (from the complexity analyzer) resolved here — §2.0.**
> The `Py_tp_doc` overwrite at :5421-5428 and the `Py_tp_members` `memcpy` at
> :5682 are both unreachable: duplicate slots are rejected, and the *new* 3.16
> path validates **strictly more** than the old one, not less. Both proved
> empirically below. The real deliverable is a **test-coverage gap**, not a bug.

### 2.0 Is the duplicate-slot validation sound on every entry point? — YES, proved

**There are not three validations; there is one.** All five public entry points
funnel into the single `type_from_slots_or_spec` at :5248:

| entry point | site | iterator init | `states[0].slot_struct_kind` |
|---|---|---|---|
| `PyType_FromSlots` | :5801-5805 | `_PySlotIterator_Init` | `_PySlot_KIND_SLOT` (new 3.16) |
| `PyType_FromMetaclass` | :5807-5813 | `_PySlotIterator_InitLegacy` | `_PySlot_KIND_TYPE` |
| `PyType_FromModuleAndSpec` | :5815-5819 | `_PySlotIterator_InitLegacy` | `_PySlot_KIND_TYPE` |
| `PyType_FromSpecWithBases` | :5821-5825 | `_PySlotIterator_InitLegacy` | `_PySlot_KIND_TYPE` |
| `PyType_FromSpec` | :5827-5831 | `_PySlotIterator_InitLegacy` | `_PySlot_KIND_TYPE` |

The older `PyType_FromSpec` no longer carries separate validation code — it was
folded into the shared iterator. Both "paths" execute the same
`handle_first_run` in `Python/slots.c:363-388`, and the REJECT branch is taken
**before** any `slot_struct_kind` test:

```c
if (_PySlotIterator_SawSlot(it, id)) {
    if (duplicate_handling == _PySlot_PROBLEM_REJECT) {
        PyErr_Format(PyExc_SystemError, "... has multiple %s (%d) slots", ...);
        return -1;                                  /* kind-INDEPENDENT */
    }
    if (it->states[0].slot_struct_kind == _PySlot_KIND_SLOT) { /* warn */ }
    else { /* legacy: silent */ }                   /* DEPRECATED tier only */
}
```

**The tiering is deliberately correct.** Classifying every type slot by
`_PySlot_get_duplicate_handling` (`Include/internal/pycore_slots_generated.h:748-838`):

```
Py_tp_doc                -> REJECT (default)     <-- the leak site
Py_tp_members            -> REJECT (default)     <-- the memcpy site
Py_tp_basicsize          -> REJECT (default)
Py_tp_extra_basicsize    -> REJECT (default)
Py_tp_itemsize           -> REJECT (default)
Py_tp_name               -> REJECT (default)
Py_tp_metaclass          -> REJECT (default)
Py_tp_module             -> REJECT (default)
Py_tp_flags              -> REJECT (default)
Py_tp_alloc              -> DEPRECATED
Py_tp_token              -> DEPRECATED
```

**Exactly the two slots whose first-pass handlers have a side effect beyond a
plain pointer store — `Py_tp_doc` (allocates) and `Py_tp_members`
(accumulates `nmembers`) — are the ones in the REJECT tier.** Every
DEPRECATED-tier slot reaching typeobject.c's first-pass switch (`Py_tp_base`,
`Py_tp_bases`, `Py_tp_token`) is an idempotent last-wins store.

**Proof 1 — duplicates, legacy `PyType_FromSpec` path (live, ASan build).**
CPython already ships the exact test fixture: `repeated_doc_slots_spec` and
`repeated_members_slots_spec` in `Modules/_testcapi/heaptype.c:323-370`.

```
variant 0 (repeated Py_tp_doc):     SystemError: type RepeatedDocSlotClass has multiple Py_tp_doc (56) slots
variant 1 (repeated Py_tp_members): SystemError: type RepeatedMembersSlotClass has multiple Py_tp_members (72) slots
```

Rejected inside `_PySlotIterator_Next` **before** it returns, so neither the
:5421-5428 `tp_doc` overwrite nor the :5682 `memcpy` is ever entered. Locked in
by `Lib/test/test_capi/test_misc.py:802-806
test_pytype_fromspec_with_repeated_slots`.

**Proof 2 — `handle_first_run` is live on the new `PyType_FromSlots` path, and
the new path is the STRICTER one.** Same slot, NULL value, both paths
(`_testlimitedcapi.type_from_null_slot` vs `.type_from_null_spec_slot`,
`-W error`, ASan build):

| slot | tier | new `PyType_FromSlots` | legacy `PyType_Spec` |
|---|---|---|---|
| `Py_tp_members` (72) | REJECT | `SystemError` | `SystemError` — **identical** |
| `Py_tp_doc` (56) | ALLOW | accepted | accepted — identical |
| `Py_tp_getset` (73) | DEPRECATED | `DeprecationWarning` | **silently accepted** |
| `Py_tp_methods` (64) | DEPRECATED | `DeprecationWarning` | **silently accepted** |
| `Py_tp_clear` (51) | DEPRECATED | `DeprecationWarning` | **silently accepted** |
| `Py_tp_alloc` (47) | DEPRECATED | `DeprecationWarning` | **silently accepted** |

The REJECT tier behaves identically on both; the only divergence is that the
**legacy** path is laxer. **Answer to the lead: the new path does not validate
less than the old one — it validates strictly more.** The 81-day-old
`Python/slots.c` is the stricter code, not the weaker.

**The actual finding here is a test gap, and it is worth filing.**
`test_pytype_fromspec_with_repeated_slots` exercises **only**
`PyType_FromSpec`. There is **no** repeated-slot test for `PyType_FromSlots` —
`grep -rn "repeated" Lib/test/test_capi/ Lib/test/test_types.py` returns that
one test. Behaviour is shared today, so the gap is latent rather than live; but
that is precisely the regression a 1-commit, 0-fix, 81-day-old file is
unprotected against, and the consequence if it regressed is a heap-buffer-overflow
READ at :5682, not a cosmetic difference.
**Recommendation:** add a `"repeated_doc"` / `"repeated_members"` case to
`_testlimitedcapi.type_from_slots` (`Modules/_testlimitedcapi/slots.c:67-299`,
where the `CASE(...)` macro makes it a three-line addition) and assert
`SystemError` from the `PyType_FromSlots` side too.

### `nmembers` → `tp_alloc` :5623 → `memcpy` :5682

```c
/* first pass, :5373-5391 */
case Py_tp_members:
    for (const PyMemberDef *memb = it.current.sl_ptr; memb->name != NULL; memb++) {
        nmembers++;                      /* NOTE: no reset between slots */
        ...
    }
/* alloc, :5623 */
res = (PyHeapTypeObject*)metaclass->tp_alloc(metaclass, nmembers);
/* second pass, :5678-5692 */
case Py_tp_members: {
    size_t len = Py_TYPE(type)->tp_itemsize * nmembers;
    memcpy(_PyHeapType_GET_MEMBERS(res), it.current.sl_ptr, len);
```

`nmembers` **accumulates across slots without a reset**, while the second pass
`memcpy`s `nmembers` entries out of **one** array per `Py_tp_members` slot. Two
`Py_tp_members` slots would therefore read `count1+count2` entries out of an
array holding `count1` — a heap-buffer-overflow READ of the caller's static
`PyMemberDef[]`, and the second memcpy would clobber the first's output.

**It is guarded, but remotely** — see §2.0 for the proof on all five entry
points. In short: `Py_tp_members` falls to `_PySlot_PROBLEM_REJECT`, and
`handle_first_run` (`Python/slots.c:367-372`) raises `SystemError`
unconditionally, inside `_PySlotIterator_Next`, before the `case
Py_tp_members:` body can run. `_PySlotIterator_Rewind`
(`Python/slots.c:66-74`) sets `is_first_run = false`, so the second pass
correctly does not re-validate.

The same remote guard is the only thing preventing a leak at the `Py_tp_doc`
case :5414-5429, where the non-NULL branch overwrites `tp_doc` without freeing
the previous allocation. Note a consequence: because a duplicate `Py_tp_doc` is
rejected, the `PyMem_Free(tp_doc)` in that case's **NULL branch** (:5416-5418)
can only ever free a NULL — it is effectively dead code, since the only way to
reach it with a non-NULL `tp_doc` would be a second `Py_tp_doc` slot. Harmless,
but it is the kind of apparently-live cleanup that makes a reader (and the
complexity analyzer) assume the overwrite path is reachable.

- **CONSIDER (robustness, not a bug today):** the `memcpy` at :5682 has no local
  bound and no assertion, and the invariant that makes it safe lives in a
  *generated* table two files away. A one-line
  `assert(!_PySlotIterator_SawSlot(&it, Py_tp_members) || nmembers == 0);`-style
  local check, or simply resetting `nmembers = 0` at the top of the first-pass
  case (the pre-3.16 code did exactly that), would make it locally sound.
  Guarded twin: the `Py_tp_basicsize` / `Py_tp_extra_basicsize` mutual-exclusion
  check at :5449-5458 — that one *is* written locally in `typeobject.c` rather
  than delegated to the slot table.

### Does the new 3.16 PySlot path validate what `PyType_Spec` validated?

**It validates strictly more.** `Py_tp_basicsize` :5339-5350,
`Py_tp_extra_basicsize` :5352-5358 and `Py_tp_itemsize` :5359-5370 each reject
`<= 0` with a `SystemError`. The legacy `PyType_Spec` fields carry no such
check. The new path also accepts a full `Py_ssize_t` (`sl_size`) where the
legacy field is an `int` — wider input, but with the sign check the legacy path
never had. NULL and duplicate handling are uniform across both paths via the
generated table. I found no validation present on the `PyType_Spec` path and
absent from the `PySlot` path.

### CONSIDER — `-spec->basicsize` is signed-overflow UB for `INT_MIN` (legacy path only)

```c
/* Objects/typeobject.c:5289-5291 */
if (spec->basicsize < 0) {
    extra_basicsize = -spec->basicsize;   /* PyType_Spec.basicsize is `int` */
}
```

`PyType_Spec.basicsize` is `int` (`Include/object.h:332`), so `-spec->basicsize`
is evaluated in `int`. For `spec->basicsize == INT_MIN` that is signed overflow
(UB), yielding `INT_MIN` in practice; `extra_basicsize` becomes **negative**,
`_align_up` of a negative rounds toward −∞, and a **negative `tp_basicsize`**
reaches `PyType_Ready` at :5720. It is caught only afterwards, by
`check_basicsize_includes_size_and_offsets` :5154 (`tp_base->tp_basicsize >
tp_basicsize`) at :5723 — i.e. *after* `PyType_Ready` has already run with the
bad value.

- **Guarded twin (the fix, in this same function):** the new path's own
  `if (extra_basicsize <= 0) { SystemError }` at :5352-5358. The legacy negation
  has no equivalent.
- **Fix:** `extra_basicsize = -(Py_ssize_t)spec->basicsize;` (cast before
  negating), or an explicit `spec->basicsize == INT_MIN` rejection.
- **Classification: CONSIDER.** C-caller-only, not Python-reachable, and the
  post-hoc check does eventually reject the type. Static-only; not reproduced
  (no Python-level handle takes an arbitrary `spec->basicsize`).

### CONSIDER — the metatype's `tp_alloc` is unconstrained while its `tp_new` is rejected

Second half of the coordinator's lead. Both var-object allocation sites call an
allocator they do not constrain:

```c
/* :4413 */ type = (PyTypeObject *)metatype->tp_alloc(metatype, ctx->nslot);
/* :5623 */ res  = (PyHeapTypeObject*)metaclass->tp_alloc(metaclass, nmembers);
```

`type_from_slots_or_spec` **does** police the metaclass's `tp_new`:

```c
/* :5562-5567 */
if (metaclass->tp_new && metaclass->tp_new != PyType_Type.tp_new) {
    PyErr_SetString(PyExc_TypeError,
                    "Metaclasses with custom tp_new are not supported.");
    goto finally;
}
```

…and `type_new_alloc` **does** force the *created* type's allocator:

```c
/* :4436-4437 */
/* Always override allocation strategy to use regular heap */
type->tp_alloc = PyType_GenericAlloc;
```

but nothing constrains the **metatype's** `tp_alloc` — the one that actually
sizes the type object and its `PyMemberDef` tail. `Py_tp_alloc` is a fully
supported spec slot (`_PySlot_heaptype_apply_field_slot` stores it; and it is
only `DEPRECATED`-tier for duplicates, so `PyType_FromSpec` silently accepts it —
see the table in §2.0). The reachable chain:

1. a C extension builds metaclass `M` with `PyType_FromSpec` +
   `{Py_tp_alloc, my_alloc}` and `Py_tp_base = &PyType_Type`;
2. **pure Python** then writes `class C(metaclass=M): __slots__ = ('a','b')`;
3. `type_new_alloc` :4413 calls `my_alloc(M, 2)`.

Three separate invariants are then held only by the extension's good behaviour,
each with a concrete consequence in *this* file:

| if `my_alloc` … | breaks |
|---|---|
| under-allocates vs `_PyObject_VAR_SIZE(M, nitems+1)` | `type_new_descriptors` :4666-4680 writes `nslot` `PyMemberDef`s past the end; `memcpy` :5682 writes `tp_itemsize*nmembers` bytes past the end |
| does not zero the tail | the `+1` NUL-name sentinel is absent → `type_add_members` :8700 (`memb->name != NULL`, no count) walks off the end — the CPY-0013 shape |
| does not GC-track | `type_dealloc` :7012 runs the **macro** `_PyObject_GC_UNTRACK` on a never-tracked object → GC-list corruption — the CPY-0011/CPY-0015 shape |

**Classification: CONSIDER, not FIX.** It is a C-caller contract violation, not
reachable from Python alone, and the contract ("`tp_alloc` must behave like
`PyType_GenericAlloc` for a GC var-object type") is the long-standing implicit
one for every `tp_alloc` in CPython. But the *asymmetry* is worth a maintainer's
eye: the function spends five lines rejecting a custom `tp_new`, whose failure
mode is a wrong object, and says nothing about a custom `tp_alloc`, whose
failure mode is a heap overflow. Both guarded twins (:5562 and :4436) are in
this same file.

**Cheapest fix** — mirror the `tp_new` check for the allocator, at both sites:
```c
if (metaclass->tp_alloc != PyType_GenericAlloc) {
    PyErr_SetString(PyExc_TypeError,
                    "Metaclasses with custom tp_alloc are not supported.");
    goto finally;
}
```
Failing that, an `assert(Py_SIZE(res) == nmembers)` immediately after :5623 and
`assert(Py_SIZE(type) == ctx->nslot)` after :4413 would at least turn a silent
overflow into a debug-build abort — the `memcpy` at :5682 currently trusts the
allocator with no local check of any kind.

---

## 3. [ACCEPTABLE] Mismatched alloc/free families — zero, full inventory

Every allocation lifetime in the file, checked end-to-end:

| what | allocated | freed | family |
|---|---|---|---|
| `pmerge` `remain` | `PyMem_New` :3371 | `PyMem_Free` :3425 | pymem ✓ |
| `mro_implementation_unlocked` `to_merge` | `PyMem_New` :3488 | `PyMem_Free` :3505, :3513 | pymem ✓ |
| slot-update chunk | `PyMem_Malloc` :3838 | `PyMem_Free` :3854 + `slot_update_free_chunks` | pymem ✓ |
| `tp_doc` (type_new) | `PyMem_Malloc` :4556 | `PyMem_Free` :7029 (`type_dealloc`) | pymem ✓ |
| `tp_doc` (from spec/slots) | `PyMem_Malloc` :5423 | `PyMem_Free` :5418, :5795, :7029 | pymem ✓ |
| `_ht_tpname` | `PyMem_Malloc` :5501 | `PyMem_Free` :5797, :7039 | pymem ✓ |
| the type object | `tp_alloc` = `PyType_GenericAlloc` → `_PyObject_MallocWithType` + `_PyObject_GC_Link` :2528, :2538 | `tp_free` = `PyObject_GC_Del` :4438, :7043 | pyobject ✓ |

No raw `malloc`/`calloc`/`realloc`/`free` anywhere in the file. No `alloca`,
`strcpy`, `strcat`. One `sprintf` at :7206 into `char msg[200]`: the literal is
43 bytes and the only conversion is `%.100s`, so max 143+NUL — bounded, and it
is on the `_PyObject_ASSERT_FAILED_MSG` abort path. **ACCEPTABLE** (PEP 7 nit at
most, not worth a POLICY entry).

`memset(slotdefs_dups, -1, sizeof(slotdefs_dups))` :12231 and
`memcpy(&slotdefs_dups[idx], data, sizeof(uint8_t)*(n+1))` :12241: `slotdefs_dups`
is `[N][1 + MAX_EQUIV]` (:11787), the source bytearray is `1 + MAX_EQUIV` bytes
(:12206), and `n = data[0] < MAX_EQUIV` (:12224). In bounds, and driven only by
the static `slotdefs` table — no Python input reaches it. Clean.

---

## 4. [ACCEPTABLE] Double-free / use-after-free on the error ladders

**`type_from_slots_or_spec` `finally:` :5787-5799.** The function states its
invariant up front (:5252-5255: "A non-NULL value in one of these means this
function holds a strong reference or owns allocated memory"). I checked all four
transferred resources; each is NULLed in the same statement pair as the
transfer:

- `bases` → `set_tp_bases(type, bases, 1); bases = NULL;` :5642-5643
- `tp_doc` → `type->tp_doc = tp_doc; tp_doc = NULL;` :5645-5646
- `ht_name` → `res->ht_name = ht_name; ht_name = NULL;` :5648-5650
- `_ht_tpname` → `res->_ht_tpname = _ht_tpname; _ht_tpname = NULL;` :5652-5654

`Py_CLEAR(res)` runs **before** the `PyMem_Free`s, so `type_dealloc` frees the
transferred `tp_doc`/`_ht_tpname` and the locals are already NULL. No path frees
twice; no path leaks. Clean.

**`type_new_init` `error:` :4930-4934.** `Py_CLEAR(ctx->slots); Py_XDECREF(dict);`
— `dict` ownership passes to the type at `set_tp_dict` :4917, which is only
reached after `type_new_alloc` succeeded, and the label is only reachable before
that point or on `type_new_alloc` failure. Clean.

**`mro_implementation_unlocked` :3505 vs :3513.** The :3505 free is inside
`if (result == NULL) { ...; return NULL; }` — it returns, so it cannot fall
through to :3513. Not a double free.

**The `mp->name` dangling question.** `type_new_descriptors` :4657 stores
`mp->name = PyUnicode_AsUTF8(PyTuple_GET_ITEM(et->ht_slots, i))` — a raw `char*`
into a `str` owned by `ht_slots`. `type_dealloc` drops `ht_slots` at :7033,
*before* `tp_free` at :7043, and member descriptors published into `tp_dict`
hold `d_member` pointers into the members array. This is the CPY-0018 shape —
but it is guarded: `PyDescr_NewMember` INCREFs the type, so no descriptor can
outlive the array or the strings. Clean.

---

## 5. [ACCEPTABLE] GC-track invariant

Both constructors track the type **inside** `tp_alloc` — `PyType_GenericAlloc`
:2564-2566 calls `_PyObject_GC_TRACK(obj)` before returning — i.e. **before any
GC-visible field is set**. That is deliberate and safe:

- `_PyType_AllocNoTrack` :2542 memsets the entire allocation past the `PyObject`
  header to zero, so every pointer field is NULL;
- `type_traverse` :7211+ uses NULL-tolerant `Py_VISIT` throughout.

The OOM shape this agent hunts (object freed on an error path *before*
`PyObject_GC_Track`, then `_PyObject_GC_UNTRACK` in `tp_dealloc` — CPY-0011,
CPY-0015) **cannot occur here**: there is no window in which the object exists
untracked, so `type_dealloc`'s macro `_PyObject_GC_UNTRACK(type)` :7012 always
unlinks a genuinely tracked object.

**One unenforced ordering invariant, worth a comment not a fix.** Between the
`_PyObject_GC_TRACK` inside `tp_alloc` and the caller's `type_set_flags(...
Py_TPFLAGS_HEAPTYPE ...)` (:4422 / :5630), the object sits on a GC list with
`tp_flags == 0`. In that state `type_is_gc` (:7333, `tp_flags &
Py_TPFLAGS_HEAPTYPE`) reports it non-GC, and `type_traverse` :7204-7208 would
take its `_PyObject_ASSERT_FAILED_MSG` abort. **Not reachable today** —
`type_set_flags` :455-459 is a plain store, and nothing between the two points
allocates, so no collection can be triggered. `type_from_slots_or_spec`
documents the obligation ("The flags must be initialized early, before the GC
traverses us", :5629); `type_new_alloc` relies on the same thing silently.
**Suggestion: mirror the comment at :4419.** ACCEPTABLE.

---

## 6. Cross-agent leads (not my shape — logged, not claimed)

### 6.1 [FIX-grade, error-path / pyerr-clear agent] `same_slots_added` swallows a live exception — REPRODUCED

```c
/* Objects/typeobject.c:7608-7611 */
if (slots_a && slots_b) {
    if (PyObject_RichCompareBool(slots_a, slots_b, Py_EQ) != 1)
        return 0;                     /* -1 and 0 collapsed; exception left set */
    size += sizeof(PyObject *) * PyTuple_GET_SIZE(slots_a);
}
```

`valid_identifier` accepts `str` **subclasses**, so a `__slots__` name can carry
a user `__eq__`. `compatible_for_assignment` then does `goto differs;` :7683 and
`PyErr_Format(PyExc_TypeError, "%s assignment: '%s' object layout differs from
'%s'")` :7688 — clobbering whatever the user raised.

Reproduced on `debug-gil-nojit-asan` (`scratchpad/eq_raises.py`): a
`KeyboardInterrupt` raised from a `__slots__` name's `__eq__` during
`obj.__class__ = X` comes back as `TypeError`, with `__context__ = None` — **no
trace of the original at all**. `MemoryError` would be swallowed identically.
Reachable from both `obj.__class__ = X` (`setclass=1`) and `C.__bases__ = (...)`
(`setclass=0`).

**Guarded twin, same file:** `method_is_overloaded` :10566 returns
`PyObject_RichCompareBool`'s value **directly**, propagating `-1` to its caller.
**Fix:** `int r = PyObject_RichCompareBool(...); if (r < 0) return -1;` and give
`same_slots_added` / `compatible_for_assignment` a tri-state return, or at
minimum guard the `PyErr_Format` with `if (!PyErr_Occurred())`.
Catalog neighbours: CPY-0006, CPY-0051.

### 6.2 [ACCEPTABLE, recorded for completeness]

The same hostile `__eq__` lets `o.__class__ = S3` succeed between two classes
whose `__slots__` have the **same count but different names** (verified:
`o.aaa` becomes readable as `o.ddd`). Memory-safe — `type_new_descriptors`
:4670-4680 gives every slot `Py_T_OBJECT_EX` at the same offset, and the
independent `size == a->tp_basicsize && size == b->tp_basicsize` check :7613
still blocks any count mismatch (verified refused) and any base mismatch
(verified refused, including plain-vs-tuple-subclass). No type confusion.

---

## Reproduction artifacts

All under
`/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`:

- `slots_probe.py` — layout arithmetic + adversarial `__slots__` (hostile
  str-subclass dunders, generator, duplicates, 200k names, tuple-subclass
  `_Py_AFTER_ITEMS` at two levels). Clean on `debug-gil-nojit-asan`,
  `debug-ft-nojit-asan`.
- `cpy0012_differential.py` — the CPY-0012/0013 shape applied to `type`;
  post-creation `__slots__` mutation, size-member writability, structseq
  control. Clean on `debug-gil-nojit-asan`, `debug-ft-nojit-asan`,
  `release-gil-nojit`.
- `hostile_eq_class.py`, `eq_raises.py` — the §6 leads.
- `dup_slots.py` — duplicate `Py_tp_doc` / `Py_tp_members` via
  `_testcapi.create_type_from_repeated_slots` (§2.0 proof 1).
- `pyslot_validation.py` — new-vs-legacy path validation differential (§2.0
  proof 2).
- `dup_table.py` — extracts the per-slot duplicate-handling tier from
  `pycore_slots_generated.h`.
