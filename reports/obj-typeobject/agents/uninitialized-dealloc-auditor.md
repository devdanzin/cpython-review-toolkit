# uninitialized-dealloc-auditor — slice `obj-typeobject`, PASS 1 (construction surface)

**Shape:** `dealloc-of-uninitialized-object` (briefing §bug-shape-catalog)
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), `Objects/typeobject.c` only (13,068 lines)
**Scanner baseline:** `scan_uninit_dealloc` = **0 findings** in slice, 3 in all of `Objects/`
**Mode:** INFORMED

---

## 1. THE DENOMINATOR — is the zero earned or structural?

**Answer: PART-EARNED, MOSTLY STRUCTURAL. The rule resolved 1 of 8 object-allocation
sites in this file (12.5%), and 0 of the 3 constructors in the assigned pass-1
territory.**

Measured by instrumenting the scanner's own `_matched_allocator()` over
`Objects/typeobject.c` (script: `<scratch>/uid_denom.py`; 417 functions extracted,
matching the sample JSON's `functions_analyzed: 417`):

| line | function | var | allocation expression | rule resolved? | frees the object on error? |
|---|---|---|---|---|---|
| 2504 | `_PyType_NewManagedObject` | `obj` | `PyType_GenericAlloc(type, 0)` | no — **correct** (zeroing) | no |
| 2528 | `_PyType_AllocNoTrack` | `alloc` | `_PyObject_MallocWithType(...)` | no — **correct** (raw storage; memset at :2540) | no |
| 2559 | `PyType_GenericAlloc` | `obj` | `_PyType_AllocNoTrack(type, nitems)` | no — **correct** (zeroing) | no |
| **4413** | **`type_new_alloc`** | `type` | `metatype->tp_alloc(metatype, ctx->nslot)` | **NO — silent skip** | via `type_new_impl:4986` |
| **5623** | **`type_from_slots_or_spec`** | `res` | `metaclass->tp_alloc(metaclass, nmembers)` | **NO — silent skip** | **yes** (`Py_CLEAR(res)` :5793) |
| 7468 | `object_new` | `obj` | `type->tp_alloc(type, 0)` | NO — silent skip | no |
| **11343** | `slot_bf_getbuffer` | `wrapper` | `PyObject_GC_New(PyBufferWrapper, …)` | **YES** | yes (`fail:` label) |
| **12999** | `super_vectorcall` | `su` | `self_type->tp_alloc(self_type, 0)` | **NO — silent skip** | **yes** (`fail:` :13020) |

- **8** object-allocation sites present. **3** of them free the object on an error path.
- The rule resolved **1 of those 3** (`:11343`), and correctly produced no finding there.
- The rule resolved **0 of 3** functions named in the task brief
  (`type_new_impl`/`type_new_alloc`, `type_from_slots_or_spec`, `PyType_Ready`).

**Why the skip.** `_matched_allocator()` returns `"tp_alloc"` **only if
`_nonzeroing_tp_allocs()` found a non-zeroing `allocfunc` defined in the same file**.
`Objects/typeobject.c` registers exactly one `tp_alloc` slot value —
`PyType_GenericAlloc` — which zeroes, so `local_nonzeroing == set()` and every
`->tp_alloc(…)` call in the file is silently treated as zeroing. That is 4 of the 5
`tp_alloc` virtual-call sites skipped without a decision being recorded.

**So the zero on this slice is not evidence of anything by itself.** I therefore
hand-read the three target constructors and ran a 1150-iteration OOM sweep. Both
agree with the zero — see §2 and §4. The zero is *true*; it just was not
*demonstrated* by the scanner.

---

## 2. Findings

### Summary
- Candidate constructors examined by hand: **5** (the 3 with an early free, plus
  `type_new_impl`'s full `goto error` chain and `type_ready`'s no-unwind path)
- **FIX: 0**
- **CONSIDER: 1** (F-6 — **reproduced SIGSEGV**, extension-reachable only)
- **ACCEPTABLE: 5**

> **F-6 was upgraded from POLICY to CONSIDER during this run and reproduced as a
> deterministic SIGSEGV.** It came in as a lead from the concurrently-running
> complexity analyzer, which enumerated the 36 `goto` sites in
> `type_from_slots_or_spec` and split them into 25 pre-transfer and 11
> post-transfer paths. See §2/F-6 and §4.2.

---

### [ACCEPTABLE] F-1 — `type_new_impl` / `type_new_alloc` (`Objects/typeobject.c:4941`, alloc at `:4413`)

**Why the shape cannot apply.** `metatype->tp_alloc` resolves to
`PyType_GenericAlloc` (`:2557`) → `_PyType_AllocNoTrack` (`:2512`), which does
`memset((char *)obj + sizeof(PyObject), 0, size - sizeof(PyObject));` at **`:2540`** —
the *whole* object after the `PyObject` header, including the variable-size
`PyMemberDef` item area (`size = _PyObject_VAR_SIZE(type, nitems+1)`). Every
`PyHeapTypeObject` field is therefore NULL/0 before the first fallible step, and
every `Py_XDECREF` in `type_dealloc` is a no-op on an unset field.

**The eight `goto error` sites in `type_new_impl` (:4948, :4954, :4968, :4972, :4976)
and the `error:` label at :4985 were traced individually.** At each one, every field
already written holds a *fully-owned strong reference or a scalar*, never a borrowed
or half-transferred pointer:

- `type_new_alloc` writes `tp_bases` (`set_tp_bases(type, Py_NewRef(ctx->bases), 1)`),
  `tp_base` (`Py_NewRef`), `ht_name` (`Py_NewRef`), and explicitly NULLs
  `ht_module` / `_ht_tpname` / `ht_token`.
- `type_new_init` sets `tp_dict` (owned, from `_PyDict_CopyAsDict`) and `ht_slots`
  (ownership moved out of `ctx->slots`, which is NULLed — `:4924-4931`).
- `type_new_set_ht_name` (`:4502`) assigns `et->ht_qualname = qualname` (a strong ref
  from `PyDict_GetItemRef`) and *then* can fail at `PyDict_DelItem` — a strong ref is
  live at that error point, which is the safe direction.
- `type_new_set_name` (`:4460`) sets `tp_name` to a pointer into `ht_name`'s UTF-8
  cache and can fail on the next line; `ht_name` outlives it and `type_dealloc` never
  reads `tp_name` after `Py_XDECREF(et->ht_name)`.

**Untrack variant:** `type_dealloc` opens with the **unchecked macro**
`_PyObject_GC_UNTRACK(type)` (`:7014`) — the severity multiplier from the briefing.
It is safe here only because `PyType_GenericAlloc` GC-TRACKs the object before
returning (`:2566`, `_PyType_IS_GC(metatype)` is true for any metatype). That is a
real load-bearing invariant, not an accident, and it is why any future change that
routed a heap type through a non-tracking allocator would abort immediately.

---

### [ACCEPTABLE] F-2 — `type_from_slots_or_spec` (`Objects/typeobject.c:5248`, alloc at `:5623`, free at `:5793`)

553 lines, ~11 `goto finally` sites after the allocation. Same zeroing allocator as
F-1. What makes this one safe beyond the memset is a **consistent
ownership-transfer-then-NULL discipline** — every local whose ownership moves into
the type is NULLed in the same or next statement, so the `finally:` block's
`Py_XDECREF`/`PyMem_Free` of those same locals cannot double-free:

```c
set_tp_bases(type, bases, 1);   bases = NULL;        // :5643-5644
type->tp_doc = tp_doc;          tp_doc = NULL;       // :5646-5647
res->ht_name = ht_name;         ht_name = NULL;      // :5650-5651
res->_ht_tpname = _ht_tpname;   _ht_tpname = NULL;   // :5654-5655
```

against

```c
finally:
    if (PyErr_Occurred()) { Py_CLEAR(res); }
    Py_XDECREF(bases); PyMem_Free(tp_doc);
    Py_XDECREF(ht_name); PyMem_Free(_ht_tpname);     // :5786-5793
```

All four are NULLed. There is no aliased-and-still-owned local at any `goto finally`.

Two sub-checks that could have gone the other way:

- **`case Py_slot_invalid: goto finally;` (`:5671`)** bails out of the *second* slot
  pass before `if (type->tp_dealloc == NULL) type->tp_dealloc = subtype_dealloc;`
  (`:5701`). Not a bug: `type->tp_dealloc` is the dealloc of *instances*; the type
  object itself is torn down by `Py_TYPE(res)->tp_dealloc` = `type_dealloc`.
  Also verified that this path always has an exception set (`Python/slots.c:189`
  calls `_PySlot_err_bad_slot()` before `goto error`), so the
  `if (PyErr_Occurred())` gate on `Py_CLEAR(res)` always fires.
- **`_PySlot_heaptype_apply_field_slot`** (`Include/internal/pycore_slots_generated.h:380`)
  writes only function-pointer fields (`as_number.*`, `as_mapping.*`, …). It cannot
  install a borrowed `PyObject *` into `ht_name`/`ht_module`/`ht_slots`, so the second
  slot pass cannot manufacture a dangling refcounted field.

---

### [ACCEPTABLE] F-3 — `PyType_Ready` / `type_ready` (`Objects/typeobject.c:9524` / `:9600`)

`type_ready`'s `error:` label does **not** unwind — it only calls `stop_readying()`
and returns -1, deliberately leaving `tp_dict`, `tp_bases`, `tp_mro`,
`ht_cached_keys` and any partial subclass registrations in place for the caller's
`Py_DECREF` to clean up. Each of those is safe for `type_dealloc`:

- **`tp_dict` / `tp_bases` / `tp_mro` / `tp_base` / `tp_cache`** — owned strong refs
  or still NULL; `Py_XDECREF` in `type_dealloc:7016-7020`.
- **Partial subclass registration.** `type_ready_add_subclasses` can fail on base *k*
  of *n*, leaving bases `0..k-1` registered. `type_dealloc_common` → `remove_all_subclasses`
  → **`remove_subclass` (`:9769`) explicitly tolerates this** and carries the comment
  *"This can happen if the type initialization errored out before the base subclasses
  were updated"*, clearing the resulting `PyDict_DelItem` failure. **This is the
  guarded twin for the partial-readying shape, and it is already in place.**
  `type_dealloc_common` additionally brackets the whole thing with
  `PyErr_GetRaisedException`/`SetRaisedException` (`:6852-6854`), satisfying
  `remove_all_subclasses`'s `assert(!PyErr_Occurred())`.
- **`ht_cached_keys`.** `type_ready_managed_dict` (`:9465`) sets it; `type_ready_post_checks`
  can then fail. `type_dealloc:7035` calls `_PyDict_RemoveKeysForClass`, which is the
  exact inverse of `_PyDict_NewKeysForClass` (clears `dsk_owning_type`, drops the keys
  refcount) and needs no readied state. Symmetric.

---

### [ACCEPTABLE] F-4 — `slot_bf_getbuffer` (`Objects/typeobject.c:11343`) — **the one site the rule resolved**

**Confirm-don't-relitigate:** this is the briefing's named exemplar for the
*shared-`fail:`-label* FP class. Re-verified: `PyObject_GC_New` at `:11343` is
followed immediately by `wrapper->mv = ret; wrapper->obj = Py_NewRef(self);
_PyObject_GC_TRACK(wrapper);` (`:11350-11352`) with **no fallible step in between**,
and every `goto fail` before the allocation leaves `wrapper == NULL` (initialised at
`:11325`) so the `Py_XDECREF(wrapper)` at `:11357` is a no-op. The scanner's silence
here is a correct negative, and it is the only place in this file where that can be
said with the scanner's own evidence.

---

### [ACCEPTABLE] F-5 — `super_vectorcall` (`Objects/typeobject.c:12999`) — the in-file guarded twin

`su = self_type->tp_alloc(self_type, 0)` (PyType_GenericAlloc, `:13064`), then
`goto fail` → `Py_DECREF(su)` (`:13020`) on two paths. `super_dealloc` (`:12556`)
opens with the **unchecked** `_PyObject_GC_UNTRACK` and then `Py_XDECREF`s
`su->obj`, `su->type`, `su->obj_type`.

Safe on both counts: `PyType_GenericAlloc` tracked it (PySuper_Type has
`Py_TPFLAGS_HAVE_GC`), and `super_init_impl` (`:12945`) writes all three fields
**all-or-nothing at the very end** via `Py_XSETREF` (`:12976-12978`) — every earlier
failure returns -1 with nothing written, so the three fields are still the
allocator's zeros.

**This is the shape's guarded twin inside the slice:** an object freed on an error
path whose destructor `Py_XDECREF`s three members it never wrote, made safe by
(a) a zeroing allocator and (b) deferring all member writes past the last fallible
step. Note the contrast with catalogued CPY-0011 `odictiter_new` / CPY-0015
`dictiter_new`, which have the same free-before-write ordering but a *non-zeroing*
allocator — that single difference is the whole bug.

*(Related but a different class: CPY-0007 — `super.__new__(super).__get__(1)`
SIGSEGVs because `supercheck` dereferences a legitimately-NULL `su->type`. That is
`init_bypass`/`null-deref`, not uninitialized-garbage; confirmed still present at
`:12763`/`:12797`, not re-litigated here.)*

---

### [CONSIDER] F-6 — `type_from_slots_or_spec` constrains the metaclass's `tp_new` but not its `tp_alloc`, so a custom metatype allocator makes every post-transfer error path a wild `Py_XDECREF` — **REPRODUCED (SIGSEGV)**

**Sites:** `Objects/typeobject.c:5623` (the allocation), `:5792` (`Py_CLEAR(res)`),
`:7034` (`Py_XDECREF(et->ht_slots)` in `type_dealloc`). Guarded-twin-that-is-missing:
`:5562` (the `tp_new` rejection).

**The gap.** `type_from_slots_or_spec` validates exactly one property of the
caller-supplied metaclass:

```c
if (metaclass->tp_new && metaclass->tp_new != PyType_Type.tp_new) {   // :5562
    PyErr_SetString(PyExc_TypeError,
        "Metaclasses with custom tp_new are not supported.");
    goto finally;
}
```

61 lines later it dispatches through that same metaclass's **`tp_alloc`** with no
check at all:

```c
res = (PyHeapTypeObject*)metaclass->tp_alloc(metaclass, nmembers);   // :5623
```

Every one of the 11 post-ownership-transfer `goto finally` paths then hands the
object to `Py_CLEAR(res)` at `:5792` → the metatype's `tp_dealloc` → `type_dealloc`,
which `Py_XDECREF`s six `PyObject *` members and `PyMem_Free`s two buffers. That is
safe only if `metaclass->tp_alloc` zeroes.

**`ht_slots` is the sharpest instance: `type_from_slots_or_spec` never writes it on
*any* path.** Grep of all 9 `ht_slots` references in the file — the only two writes
are `:4923` / `:4927`, both inside `type_new_init`, i.e. the `type(...)` path. On the
`PyType_FromSpec*` path the field carries whatever the allocator left, and
`type_dealloc:7034` unconditionally `Py_XDECREF`s it.

**Reachability — the precise boundary.**

- **NOT Python-reachable.** `type_new_alloc:4437` does
  `type->tp_alloc = PyType_GenericAlloc;` under the comment
  *"Always override allocation strategy to use regular heap"*. Every class created
  by `type(...)` — including a Python metaclass `class M(type): ...` — therefore
  carries the zeroing allocator, unconditionally. There is no Python-level trigger.
- **IS extension-reachable, through a documented slot.** `Py_tp_alloc` is an
  accepted `PyType_Spec` slot: `_PySlot_heaptype_apply_field_slot`
  (`Include/internal/pycore_slots_generated.h:509-510`) writes
  `ht->ht_type.tp_alloc`, it is not on the `must_be_static` list, and
  **`Modules/arraymodule.c:3163` already uses it in-tree**
  (`{Py_tp_alloc, _PyType_AllocNoTrack}`). Unlike `type_new_alloc`,
  `type_from_slots_or_spec` never re-forces `tp_alloc`, so the override sticks on a
  heap metaclass. A static C metatype with `.tp_alloc = my_alloc` reaches the same
  place even more directly.

**Reproduction — deterministic SIGSEGV, no fuzzing.** Probe extension
`<scratch>/metaalloc.c`, built against `debug-gil-nojit` (its `Objects/typeobject.c`
is byte-identical to the target ref). It creates a metaclass via
`PyType_FromSpecWithBases(&meta_spec, &PyType_Type)` carrying
`{Py_tp_alloc, dirty_meta_alloc}`, where `dirty_meta_alloc` models the
`_datetimemodule.c` `time_alloc`/`datetime_alloc` shape by leaving `ht_slots` as
allocator trash (`0xdddddddddddd0001`), then calls
`PyType_FromMetaclass(that_metaclass, …)`.

OOM sweep over the probe: **120 iterations, 1 SIGSEGV at `n=5`** (`memory_error` 1,
`completed` 118, `segv` 1). Reduced to a standalone repro with
`_testcapi.set_nomemory(5, 0)`; gdb backtrace:

```
#0  Py_XDECREF (op=0xdddddddddddd0001) at ./Include/refcount.h:520
#1  type_dealloc (self=0x555555ec7850) at Objects/typeobject.c:7034
#2  meta_dealloc (self=0x555555ec7850) at metaalloc.c:51        <- the metatype's tp_dealloc
#3  _Py_Dealloc (op=0x555555ec7850) at Objects/object.c:3319
#4  Py_DECREF (lineno=5792, op=0x555555ec7850)
#5  type_from_slots_or_spec (...) at Objects/typeobject.c:5792  <- Py_CLEAR(res)
```

That is exactly the predicted chain: post-transfer `goto finally` → `Py_CLEAR(res)`
→ `type_dealloc` → wild `Py_XDECREF` of an uninitialized member. `n=5` fails an
allocation early inside `PyType_Ready`, which additionally leaves `tp_dict` /
`tp_mro` unset — a second and third exposed field on the same path.

**Members exposed at `:5792` with a non-zeroing metatype allocator** (all read by
`type_dealloc`):

| member | written by `type_from_slots_or_spec`? | read at |
|---|---|---|
| `ht_slots` | **never, on any path** | `:7034` `Py_XDECREF` |
| `tp_cache` | never | `:7020` `Py_XDECREF` |
| `tp_watched` | never | `:6987` — drives the watcher-callback loop |
| `tp_dict`, `tp_mro`, `tp_subclasses` | only by `PyType_Ready` | `:7017`, `:7019`, `:7021` |
| `ht_cached_keys` | only by `type_ready_managed_dict` | `:7035` `_PyDict_RemoveKeysForClass` |
| `unique_id` (FT) | `:5716`, after the second slot pass | `:7041` assert |

**Non-reproducing sibling, reported honestly.** The `type_ready_post_checks` failure
(`Py_TPFLAGS_HAVE_GC` with no traverse — a post-transfer path needing no OOM at all)
does **not** crash: by then `PyType_Ready` has built a self-referential `tp_mro`, so
`Py_CLEAR(res)` only drops the refcount to 1 and the half-built type becomes cyclic
garbage that neither an explicit `gc.collect()` nor interpreter shutdown reclaimed in
my test. The wild decref is deferred indefinitely there — a **leak**, not a crash.
Likewise, on the *success* path `ht_slots` is trash too, but nothing tore the type
down in my test, so that stays latent. **Only the early-`PyType_Ready`-failure window
gives the immediate, unconditional teardown — which is why the OOM sweep found
exactly one crashing index out of 120.**

**Why CONSIDER and not FIX.** It is not Python-reachable, and it requires an
extension to install a metatype allocator that CPython's own documentation
(`Doc/c-api/typeobj.rst:2145-2147`) describes as the non-default: *"For heap
subtypes, this field is always set to `PyType_GenericAlloc`."* But that "always" is
enforced only on the `type_new` path (`:4437`) and is already false on the
`PyType_FromSpec` path (`arraymodule.c`), so the doc cannot be cited as the contract
that makes `:5623` safe. Nor is there any documented requirement that a `tp_alloc`
zero — `PyType_GenericAlloc`'s own doc (`Doc/c-api/type.rst:196`) says *it* "zeros the
memory", which is a statement about that function, not about the slot.

**Fix — either half is sufficient, and both are one-liners:**

```c
/* (a) mirror the tp_new constraint, next to it at :5562 */
if (metaclass->tp_alloc != PyType_GenericAlloc) {
    PyErr_SetString(PyExc_TypeError,
        "Metaclasses with custom tp_alloc are not supported.");
    goto finally;
}
```

```c
/* (b) or the canonical fix for this shape: NULL what the function never writes,
       immediately after :5623 */
res->ht_slots = NULL;
```

CPython already writes assertion (a) for its own use —
`assert(type->tp_alloc == PyType_GenericAlloc);` at `:2502` in
`_PyType_NewManagedObject`. Option (a) also gives `scan_uninit_dealloc` something to
resolve, which is why I prefer it.

**Tree-wide inventory** (`<scratch>/uid_tree_alloc.py`, all `*.c`) — no *in-tree*
metatype is affected today:

| slot value | files | zeroing? |
|---|---|---|
| `PyType_GenericAlloc` | 26 | yes |
| `_PyType_AllocNoTrack` | 3 (`arraymodule.c`, `dictobject.c`, `setobject.c`) | yes (`memset` at `typeobject.c:2540`) |
| `bytes_alloc` | 1 | yes |
| `time_alloc`, `datetime_alloc` | 1 (`_datetimemodule.c`) | **no**, but not on a metatype |

The same gap exists at `:4413` (`metatype->tp_alloc` in `type_new_alloc`), but there
it is unreachable for a second, independent reason: `type_new` is only entered for a
metatype whose `tp_new` is `type_new`, and `:4437` immediately re-forces
`tp_alloc = PyType_GenericAlloc` on the result.

---

### [ACCEPTABLE] F-7 — the `PyErr_Occurred()`-gated cleanup obligation *is* discharged, including across the `Python/slots.c` boundary

`type_from_slots_or_spec` gates its entire teardown on
`finally: if (PyErr_Occurred()) { Py_CLEAR(res); }` (`:5787-5788`) rather than a
status flag, so all 36 `goto finally` sites must leave a live exception. Two of them
(`:5318` and `:5672`, `case Py_slot_invalid:`) delegate that obligation into another
translation unit. Verified exhaustively:

- `Python/slots.c:302-304` — `error: it->current.sl_id = Py_slot_invalid; return true;`
  is the only producer of `Py_slot_invalid` from the iterator.
- All **5** `goto error` sites in `_PySlotIterator_Next` set an exception first:
  `_PySlot_err_bad_slot()` (`:189`), and `PyErr_Format` at `:196` (bad `Py_slot_end`
  flags), `:218` (non-static PyModuleDef slot), `:226` (nesting depth), plus
  `handle_first_run(it) < 0` at `:298`.
- `_PySlot_err_bad_slot` (`Include/internal/pycore_slots.h:119-134`) calls
  `PyErr_Format` on **all three** of its branches — there is no silent path.
- `handle_first_run` has exactly **5** `return -1` sites (`Python/slots.c:319, 345,
  358, 381, 394`), each immediately preceded by a `PyErr_Format` or a failed
  `PyErr_WarnFormat` (which leaves the warning-turned-error raised).

So the half-built type can neither leak out nor be returned to the caller through
this route. **ACCEPTABLE**, with one cheap hardening suggestion: an
`assert(PyErr_Occurred());` on each `case Py_slot_invalid:` arm would pin the
cross-TU contract locally, the way `remove_all_subclasses` already pins its own.

---

## 3. Toolkit assessment — three recall holes, ranked

### H-1 (highest value) — virtual-dispatch resolution is file-local, and the skip is silent

`_matched_allocator()` returns `"tp_alloc"` only when `_nonzeroing_tp_allocs()` finds
a non-zeroing `allocfunc` *in the same file*. For `Objects/typeobject.c` that set is
empty, so **4 of 5 `tp_alloc` sites — including 2 that free the object on an error
path — were dropped with no record**. The scanner's `functions_analyzed: 417` gives
the impression of coverage the allocator gate does not deliver.

The gate is *sound in effect* today only because the tree-wide inventory in F-6 says
so. It is not sound in *reasoning*: for a caller-supplied metatype the resolution is
not a file-local question at all.

**Proposal (cheap, no false positives).** Add a `sites_considered` /
`sites_skipped_unresolved_allocator` counter to the report envelope, listing each
`->tp_alloc(…)` assignment the gate dropped with `file:line:function`. That converts
a structural zero into a *reported* denominator, which is precisely the failure mode
this campaign keeps hitting. It costs nothing in precision because it emits counts,
not findings.

**Proposal (stronger, optional).** Resolve `tp_alloc` tree-wide rather than
per-file: build the set of non-zeroing allocfuncs once over the whole scan root
(the script already has `_nonzeroing_tp_allocs`, it just runs it per file), and treat
`X->tp_alloc(...)` where `X` is a *parameter or caller-supplied* variable as
unresolved-but-considered rather than assumed-zeroing.

### H-2 — alias blindness in `_member_writes()`

`_member_writes()` matches only `VAR->member = …` where `VAR` is the exact
assignment target of the allocation. `type_from_slots_or_spec` allocates into `res`
and then writes ~15 fields through **`type = &res->ht_type;`** (`:5626`);
`type_new_alloc` writes through both `type` and `et = (PyHeapTypeObject *)type`
(`:4419`).

**Measured, not assumed.** I monkey-patched `_matched_allocator` to force every
`->tp_alloc(…)` non-zeroing and re-ran `_check_function` over this file
(`<scratch>/uid_force.py`): **still 0 findings.** So even if H-1 were fixed in
isolation, this file would report nothing — H-2 masks it.

Tree-wide, H-2 currently costs little among *already-resolved* sites (169 resolved
non-zeroing allocations, 50 with an early free; a crude alias probe found 3 candidate
aliases, all spurious on inspection). Its cost is latent: it becomes the binding
constraint the moment H-1 is relaxed.

**Proposal.** Seed an alias set: on `T *A = (T *)VAR;`, `A = &VAR->field;`, or
`A = VAR;` after the allocation, treat `A->member = …` as a write to `VAR`. Bound it
to same-scope, single-assignment locals to keep it sound.

### H-3 — initialization through a helper or a macro is invisible

`_check_function` bails at `if not writes: continue`. A constructor that delegates
member initialization to a helper produces zero in-scope writes and is dropped —
whether it is safe or not.

**Measured tree-wide:** 6 of the 50 resolved free-carrying sites have zero in-scope
`var->member =` writes:

| site | why invisible | hand verdict |
|---|---|---|
| `Objects/typeobject.c:12999` `super_vectorcall` | fields set inside `super_init_impl` | ACCEPTABLE (F-5) |
| `Modules/_datetimemodule.c:3847` `iso_calendar_date_new_impl` | `PyTuple_SET_ITEM`, a macro, not a field write | ACCEPTABLE — tuple-subclass `tp_alloc` inherits `PyType_GenericAlloc`, so the 3 items are NULL and `tuple_dealloc`'s `Py_XDECREF`s are no-ops |
| `Modules/_testcapi/mem.c:422`, `:436`; `_testcapi/object.c:180`; `_testcapi/structmember.c:67`; `_testcapimodule.c:259` | test-support code | ACCEPTABLE (diagnostic code, briefing FP class) |

So H-3 costs 0 findings today, but the two non-test sites are **structurally
unexamined, not correctly silent** — exactly the distinction this run was asked to
police. `super_vectorcall` in particular is a textbook instance of the shape that the
scanner cannot see at all.

**Proposal.** When a constructor has an early free and zero in-scope member writes,
emit a `confidence: "unresolved"` record rather than dropping it, so the denominator
survives into the report.

### H-4 (minor) — `_member_evidence()` is file-scoped, so evidence is cross-contaminated

`_member_evidence()` collected **26** members for this file, pooled across every type
in it: `mv`/`obj` (from `bufferwrapper_dealloc`), `type`/`obj_type` (from
`super_dealloc`), `slots` (from `type_new_ctx`, `:4928`), `cached_objects` (an
interpreter-state field, `:6972`). This is the same "a sibling type's macro is not
this type's macro" defect the briefing already documents for the GC-untrack rule; it
applies to the destructor-evidence filter too. It costs nothing here (no findings to
inflate) but would raise `confidence` spuriously on any multi-type file.

---

## 4. Reproduction

### 4.1 — Stock CPython: 1150 OOM-sweep iterations, 0 crashes

All builds verified byte-identical to the target ref for the file under test:
`diff builds/<name>/Objects/typeobject.c cpython/Objects/typeobject.c` → **IDENTICAL**
for `debug-gil-nojit`, `debug-gil-nojit-asan`, `debug-ft-nojit` (matrix is at
`a1d580430c8`, newer than `4f3be1b5777`, but this file did not change between them).

Setup ran **unarmed** (warm-up construction, imports, freelist priming) per the
briefing; the child's stderr was checked for sanitizer reports by the harness.

| build | payload | iters | MemoryError | completed | crashes |
|---|---|---|---|---|---|
| `debug-gil-nojit` | `type()` w/ `__slots__`, `__qualname__`, `__doc__`, `__set_name__`, `__init_subclass__`, custom metaclass, 2-base MRO | 400 | 47 | 353 | **0** |
| `debug-gil-nojit` | `PyType_FromSpec` / `FromMetaclass` / `FromSpecWithBases` (6 `_testcapi` entry points) | 300 | 300 | 0 | **0** |
| `debug-gil-nojit-asan` | same FromSpec payload | 200 | 200 | 0 | **0** (no ASan reports) |
| `debug-ft-nojit` | same FromSpec payload | 250 | 250 | 0 | **0** |
| `debug-ft-nojit` | `type()` payload | 400 | 42 | 358 | **0** |

Scripts: `<scratch>/tn_setup.py`, `tn_payload.py`, `fs_setup.py`, `fs_payload.py`.

**Honest reading of the sweep.** Per the briefing, *a clean sweep is not an
exoneration* — gh-151815 survives 60/60 and is still live. The difference here is
that the sweep **corroborates a static reason** (a verified `memset` at
`typeobject.c:2540` plus the ownership-transfer discipline of §F-2) rather than
standing alone. Two caveats I will not paper over:

1. The GIL sweeps are the load-bearing ones. On the **free-threaded** build,
   `_PyObject_AssignUniqueId` (`:4449`, `:5716`) calls `_PyObject_SetDeferredRefcount`,
   so a heap type is deferred-refcounted from the moment it is allocated and the
   error-path `Py_DECREF(type)` / `Py_CLEAR(res)` **does not run `type_dealloc`
   synchronously at all** — the GC does, later, after `disable_deferred_refcounting`
   (`Python/gc_free_threading.c:277-286`) clears `unique_id`, which is what satisfies
   `assert(et->unique_id == _Py_INVALID_UNIQUE_ID)` at `typeobject.c:7041`. The FT
   sweep therefore exercises teardown-on-a-half-built-type only in the narrow window
   where the unique-id pool resize itself fails (`Python/uniqueid.c:87`), leaving the
   object non-deferred. Recorded, not resolved.
2. The `type()` sweeps report 353/358 "completed" — those indices exhausted the
   budget outside the payload window. The 47 (GIL) / 42 (FT) `MemoryError` outcomes
   are the real count of distinct allocation-failure points reached inside
   `type_new_impl`'s chain. The FromSpec sweeps hit 300/300 and 250/250, which is the
   denser and more meaningful of the two.

### 4.2 — With an extension-supplied non-zeroing metatype allocator: **SIGSEGV at n=5**

The §4.1 sweeps establish that the *stock* construction surface is clean. They say
nothing about the missing `tp_alloc` constraint of F-6, because no in-tree metatype
exercises it. Probe extension `<scratch>/metaalloc.c` (compiled
`clang-21 -g -O0 -shared -fPIC -I$B -I$B/Include`) closes that gap:

| build | payload | iters | MemoryError | completed | **crashes** |
|---|---|---|---|---|---|
| `debug-gil-nojit` | `metaalloc.probe(DirtyMeta)` → `PyType_FromMetaclass` | 120 | 1 | 118 | **1 SIGSEGV @ n=5** |

Standalone reduction (`<scratch>/gdb_meta.py`): build the metaclass unarmed, then
`_testcapi.set_nomemory(5, 0)`, then one `PyType_FromMetaclass` call — SIGSEGV every
time, backtrace in §2/F-6. The faulting instruction is
`Py_XDECREF(op=0xdddddddddddd0001)` reached from `Objects/typeobject.c:7034`, i.e. the
exact byte pattern the probe's allocator left in `ht_slots`, proving the read comes
from the uninitialized member and not from anything the probe does itself.

Scripts: `<scratch>/metaalloc.c`, `ma_setup.py`, `ma_payload.py`, `gdb_meta.py`,
`run_meta.py`, `succ.py`.

---

## 5. Prior art checked

`gh api -X GET search/issues -f q='repo:python/cpython …'`:

- **gh-40467** *"partially initialized heap allocated type objects"* — closed. The
  historical instance of this concern for heap types; the current code's zeroing
  allocator + `remove_subclass`'s tolerant path are its residue.
- **gh-92678** — MRO behaviour change, unrelated.
- **gh-103968** *"PyType_FromSpec refuses to create classes with tp_new"* — closed.
  Adjacent to F-6 and reinforcing it: the metaclass `tp_new` constraint at `:5562`
  has its own tracker history, i.e. it was reasoned about deliberately. Nothing
  equivalent exists for `tp_alloc`.
- **gh-93012** (`PyType_FromMetaclass` addition), **gh-105970** — no `tp_alloc`
  discussion.
- No open or closed issue matching `Py_tp_alloc` + metaclass, or
  `PyType_FromMetaclass` + `tp_alloc`. **F-6 appears to be novel.**

Catalog entries confirmed, not re-litigated: **CPY-0007** (`supercheck` NULL-deref,
`:12763`/`:12797` — still present, different class), **CPY-0011** / **CPY-0015** /
**CPY-0017** (the non-zeroing-allocator instances this file's `super_vectorcall` is
the guarded twin of).

---

## 6. Verdict

**With the stock in-tree allocator, the `dealloc-of-uninitialized-object` shape is
absent from the pass-1 construction surface of `Objects/typeobject.c`** — for a
reason that is verifiable and verified: every object allocation in the file routes
through `PyType_GenericAlloc` → `_PyType_AllocNoTrack`, whose `memset` at `:2540`
zeroes the entire post-header object including the variable-size item area. 1150
OOM-sweep iterations across four build/payload combinations agree.

**That safety is load-bearing and unasserted.** `type_from_slots_or_spec` validates
the caller-supplied metaclass's `tp_new` at `:5562` and then dispatches through its
unvalidated `tp_alloc` at `:5623`, 61 lines later, with the correctness of the
remaining ~170 lines of unwind depending on the result being zeroed. `Py_tp_alloc` is
an accepted spec slot that `arraymodule.c` already uses in-tree, and unlike
`type_new_alloc:4437` this path never re-forces `PyType_GenericAlloc`. **F-6 is
reproduced as a deterministic SIGSEGV at `Objects/typeobject.c:7034`
(`Py_XDECREF(et->ht_slots)`) reached from `:5792`** — extension-reachable, not
Python-reachable, fixed by one line either way.

**The scanner established none of this.** It resolved 1 of 8 allocation sites and 0
of the 3 assigned constructors; the zero it reported was structural. The single
highest-value toolkit output of this run is **H-1**: make the allocator gate report
what it skipped, so the next slice's zero can be read — and note that F-6 is exactly
the bug the gate's own heuristic ("no non-zeroing allocfunc in this file ⇒ `tp_alloc`
zeroes") is constitutionally unable to see, because the offending allocfunc lives in
a *different* translation unit by construction.
