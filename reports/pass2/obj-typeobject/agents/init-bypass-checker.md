# Init-Bypass NULL-Deref Analysis — `Objects/typeobject.c` (PASS 2)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), `Objects/typeobject.c`, 13,068 lines.
**Mode:** INFORMED, pass 2. Read first: `RUN_CONTEXT.md`, `informed_briefing.md`, `PASS2_BASELINE.md`,
`include_map.md`, `history_context.md`, `scanners/scan_init_bypass.sample.json`, and pass 1's
`reports/obj-typeobject/agents/init-bypass-checker.md` (not redone).
**Build provenance:** `Objects/typeobject.c` is **byte-identical** (`diff -q` clean) between the target
ref and `debug-gil-nojit`, `debug-gil-nojit-asan`, `release-gil-nojit`, `debug-ft-nojit` at `a1d580430c8`.
Every reproduction below is against the exact source under review.

---

## Summary

- **Nullable fields seen: 3, across 1 file** (`total_nullable_fields: 3`, `files_with_nullable_fields: 1`).
  Non-zero → the rule fired. Hand-verified complete: `superobject::{type,obj,obj_type}` and nothing else
  (see the Phase-0 table). `functions_analyzed = 459`.
- **Candidate reads: 4** (0 `duplicate_locations`), of which 1 is NET NEW and 3 are already recorded.
- **FIX: 1** — NET NEW, found by *reading* the super lookup surface, not by the scanner:
  `do_super_lookup:12656` uses a borrowed `su_obj` freed by a re-entrant `super.__init__` →
  **heap-use-after-free, SIGSEGV exit 139**, reproduced on 4 local builds and on released 3.14.4.
- **CONSIDER: 0** new.
- **ACCEPTABLE: 1** — the sole NET NEW scanner candidate (`:3894 apply_slot_updates`) is a
  field-name-collision false positive.
- **CONFIRMED, not re-litigated: 3** — CPY-0007 (`:12797`), CPY-0080 (`:12793`), and the correctly
  suppressed dominated sink at `:12806`.
- **Bounded negatives: 5** classes closed with reasons, including a **correction to pass 1**.
- **Toolkit: 2 precision defects + 2 recall gaps**, each with a proposed rule shape.

---

## 1. The NET NEW scanner candidate — `:3894 apply_slot_updates`

### [ACCEPTABLE] `one_hop_param_deref` on `item->type` is a field-name collision — `Objects/typeobject.c:3894`

The scanner reports `'type' can be NULL … passed as argument 0 to type_clear_flags(), which
dereferences that parameter at line 478`. Full triage:

**Which parameter, which callee, can it be NULL.**

```c
/* Objects/typeobject.c:3883-3899  (inside #ifdef Py_GIL_DISABLED, 3789-3929) */
static void
apply_slot_updates(slot_update_t *updates)
{
    ...
            slot_update_item_t *item = &chunk->updates[i];
            *(item->slot_ptr) = item->slot_value;
            if (item->slot_value == slot_tp_call) {
                type_clear_flags(item->type, Py_TPFLAGS_HAVE_VECTORCALL);   /* :3894 */
```

`type_clear_flags:476` does deref its parameter (`tp->tp_flags` at `:478`, via `type_set_flags`), so the
callee half of the rule is correct. The **nullability** half is not:

- `item` is a `slot_update_item_t *` (`:3809-3813`), **not** a `superobject *`. Its `type` member is a
  `PyTypeObject *`, unrelated to `superobject::type` — the only `type` field in this file's nullable set.
- `slot_update_item_t::type` is written at exactly one site, `queue_slot_update:3875` (`item->type = type`),
  from the sole caller `update_one_slot:12051`, where `type` is the type being updated and is
  dereferenced dozens of times in the same function. It cannot be NULL.
- Belt and braces: the whole block is `#ifdef Py_GIL_DISABLED`, so on the default build the function
  does not exist.

This is exactly the trap the agent brief names — *"a collision through a local of a third type can still
slip through — confirm the flagged `x->field` really belongs to the type whose slot table you resolved."*
Pass 1 flagged it as a *precision hazard* on a hypothetical; pass 2 has the concrete instance, and it
arrived through the new rule. Root cause and fix in the Toolkit assessment (P1).

**Verdict: ACCEPTABLE — not a bug, and not reachable.**

---

## 2. `super` beyond the construction surface — the big one

### 2a. Confirmations (one line each, per informed rule 1)

| site | record | status |
|---|---|---|
| `:12797` `super_descr_get` → `supercheck:12763` | **CPY-0007** | Still live. `super.__new__(super).__get__(1)` → **rc 139**; the third entry point pass 1 added (`class C: d = super.__new__(super)` … `C().d`) → **rc 139**. The new `one_hop_param_deref` rule now lands on `:12797` — the exact coordinate CPY-0007 records by hand. |
| `:12793` `super_descr_get` `PyObject_CallFunctionObjArgs` | **CPY-0080** | Still live. `class S(super)` with a no-op `__init__` → `S(int,1).__get__(1)` returns `<super: <class 'NULL'>, NULL>`, **rc 0**, silently truncated call. |
| `:12806` `Py_NewRef(su->type)` | — | Correctly annotated `dominated_by: 12797` and correctly *not* counted as a separate finding. |
| `:12839` `assert(PyCell_Check(firstarg))` | **CPY-0081** | Still assert-only at HEAD. |
| `:709` `state->tp_subclasses = subclasses` (`init_tp_subclasses`) | **CPY-0089** | Still unasserted at HEAD. |

### 2b. Complete NULL-surface enumeration of `super` (the pass-2 mandate: *find every remaining unguarded reader*)

All 23 reads of `su->type` / `su->obj` / `su->obj_type` in the file, and their guard status under the
`super.__new__(super)` bypass (all three fields NULL):

| site | read | guard | verdict |
|---|---|---|---|
| `:12561-12563` `super_dealloc` | `Py_XDECREF` ×3 | X-form | safe |
| `:12572-12580` `super_repr` | `su->obj_type` / `su->type ? … : "NULL"` | **explicit ternary + `if`** | **the guarded twin** (Guido, 2001) |
| `:12699` `super_getattro` → `do_super_lookup` | all three, borrowed | `do_super_lookup:12643` `if (su_obj_type == NULL) goto skip;` | safe *for NULL* — but see 2c |
| `:12608` `_PySuper_LookupDescr` | `su_type` | none needed — pointer compare only; NULL never matches, loop falls through to `i >= n` → `return NULL` | safe |
| `:12786` `super_descr_get` | `su->obj != NULL` | explicit | safe — and this is the line that guards *one* of the three siblings and not the other two |
| `:12793`, `:12797`, `:12806` | `su->type` | **none** | **CPY-0080 / CPY-0007** |
| `:12950-12952` `super_init_impl` | `Py_XSETREF` ×3 | X-form | safe |
| `:12977-12979` `super_traverse` | `Py_VISIT` ×3 | NULL-safe macro | safe |

**Bounded negative:** apart from the two already-recorded sites in `super_descr_get`, the `super` NULL
surface is clean. Two non-obvious reasons worth recording so nobody re-derives them:

1. The mixed state `su->type == NULL && su->obj_type != NULL` is also safe — `_PySuper_LookupDescr`
   only *pointer-compares* `su_type` against MRO entries (`:12608`), so a NULL simply never matches and
   the function returns NULL without dereferencing anything.
2. `PySuper_Type` has `tp_traverse = super_traverse` and **`tp_clear = 0`** (`:13049-13050`). That closes
   the CPY-0031 (`lazyimportobject`) avenue entirely: there is no `tp_clear` to NULL the fields on a
   cleared-then-resurrected object.

### 2c. [FIX] **NET NEW** — `do_super_lookup` uses a borrowed `su_obj` / `su_obj_type` across a user `__hash__` that re-`__init__`s the same super object

**Sites:** `Objects/typeobject.c:12656` (the use) ← `Objects/typeobject.c:12699` (`super_getattro`, the
borrowed load) ← `Objects/typeobject.c:12622` (`_PySuper_LookupDescr`, the window) / `:12951`
(`super_init_impl`, the free).

**What.** `super_getattro` hands `do_super_lookup` three **raw borrowed** pointers:

```c
/* Objects/typeobject.c:12699 */
    return do_super_lookup(su, su->type, su->obj, su->obj_type, name, NULL);
```

`do_super_lookup` calls `_PySuper_LookupDescr`, which does
`PyDict_GetItemRef(dict, name, &res)` (`:12622`). `name` reaches `super_getattro` straight from
`getattr(obj, name)` and only has to satisfy `PyUnicode_Check` — **a `str` subclass with a Python
`__hash__` runs arbitrary Python inside that lookup**. That Python can call
`super.__init__(s, T, o)` on the *same* super instance, and `super_init_impl:12950-12952` executes three
`Py_XSETREF`s that drop the old references. Control returns to `do_super_lookup`, which then uses the
now-dangling locals:

```c
/* Objects/typeobject.c:12656-12660 */
                res2 = f(res,
                    (su_obj == (PyObject *)su_obj_type) ? NULL : su_obj,
                    (PyObject *)su_obj_type);
```

**Guarded twin — two of them, and they are the whole story.**

1. **In the very same function**, `_PySuper_LookupDescr:12597-12601`:
   ```c
   /* Keep a strong reference to mro because su_obj_type->tp_mro can be
      replaced during PyDict_GetItemRef(dict, name, &res). */
   _PyCStackRef mro_ref;
   _PyThreadState_PushCStackRefNew(tstate, &mro_ref, mro);
   ```
   The author identified *this exact window* and pinned `mro` against it. `su_obj` and `su_obj_type`
   cross the same window and are pinned by nobody. The lineage is old: bpo-14199 / gh-58407
   (*"Keep a reference to mro in `_PyType_Lookup()` and `super_getattro()`"*, label `type-crash`, 2012),
   restrengthened in `bdf6de8c3f0` (gh-145685). Twice the same window was recognised — for `mro` only.
2. **The adjacent caller**, `_PySuper_Lookup:12771-12776`, is the correct ownership discipline:
   ```c
   PyTypeObject *su_obj_type = supercheck(su_type, su_obj);   /* strong ref */
   ...
   PyObject *res = do_super_lookup(NULL, su_type, su_obj, su_obj_type, name, method);
   Py_DECREF(su_obj_type);
   ```
   It holds a **strong** `su_obj_type` across the whole call, and its `su_obj` is the eval loop's `self`,
   alive on the value stack. That is why the `LOAD_SUPER_ATTR` / zero-arg `super()` path does **not**
   crash — the empirical negative control below (probe 5).

**Reproduction — confirmed, exit 139.**
`/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/super_lookup_uaf.py`

```python
class Descr:
    def __get__(self, obj, objtype=None):
        return "GOT " + repr(type(obj))

class Base:  x = Descr()
class Mid(Base):  pass
class Sub(Mid):   pass

inst = Sub()
s = super(Mid, inst)      # su->type=Mid, su->obj=inst, su->obj_type=Sub
del inst                  # the super object now holds the ONLY reference to inst

class K(str):
    def __hash__(self):
        super.__init__(s, Mid, Sub())   # Py_XSETREF(su->obj, new) frees the old inst
        return str.__hash__(self)

getattr(s, K("x"))        # -> SIGSEGV
```

ASan frame (`debug-gil-nojit-asan`), which matches the static reading line for line:

```
ERROR: AddressSanitizer: heap-use-after-free ... READ of size 4
    #0 _Py_IsImmortal                 Include/refcount.h:132
    #1 _PyStackRef_FromPyObjectNew     Include/internal/pycore_stackref.h:602
    #2 _PyEval_Vector                  Python/ceval.c:2154
    #4 slot_tp_descr_get               Objects/typeobject.c:11153
    #5 do_super_lookup                 Objects/typeobject.c:12656      <-- the use
    #6 PyObject_GetAttr                Objects/object.c:1322
freed by thread T0 here:
    #1 subtype_dealloc                 Objects/typeobject.c:2876
    #5 super_init_impl                 Objects/typeobject.c:12951      <-- the free
    #6 super_init                      Objects/typeobject.c:12907
```

**Build / release matrix — all exit 139:**

| build | rc |
|---|---|
| `debug-gil-nojit` | **139** |
| `release-gil-nojit` | **139** |
| `debug-ft-nojit` | **139** |
| `debug-gil-nojit-asan` | heap-use-after-free (frame above) |
| **released `/usr/bin/python3.14` (3.14.4)** | **139** |

→ **longstanding, not a regression.** Same urgency class as CPY-0007 (`super` segfaults on 3.14.4 too).

**Crash / survive split** (`super_lookup_sweep.py`, one probe per process; identical on
`debug-gil-nojit` and released 3.14.4 — this is what makes the finding actionable):

| # | shape | rc | what it isolates |
|---|---|---|---|
| 0 | descriptor attribute + 2-arg re-init in `__hash__` | **139** | the bug |
| 1 | descriptor attribute + **1-arg** re-init (`super.__init__(s, Mid)`, which also NULLs `su->obj`/`su->obj_type`) | **139** | the free happens on both re-init forms |
| 2 | descriptor attribute, no re-init | 0 | it is the re-init, not the str subclass |
| 3 | **non**-descriptor attribute + re-init | 0 | `f == NULL`, so `:12656` never executes → the sink is exactly `f(res, su_obj, su_obj_type)` |
| 4 | exact `str` name (no Python in the hash) + descriptor | 0 | the window is `PyDict_GetItemRef`'s hash, nothing else |
| 5 | zero-arg `super().x` inside a method (`_PySuper_Lookup`) | 0 | **the guarded twin, empirically** |
| 6 | same as 0 but the caller's frame still holds `inst` | 0 | it is a liveness bug, not a logic error |
| 7 | name is `"__class__"` (a `str` subclass) | 0 | the `:12694-12697` fast path returns first |

**Prior art:** none. `gh api -X GET search/issues` over `super_getattro`, `do_super_lookup`,
`super getattr use after free`, `super borrowed reference __hash__`, `LOAD_SUPER_ATTR use-after-free`,
`super() reinitialize __init__ crash` returns only the 2012 `mro`-pin issue (gh-58407) and unrelated
specialisation PRs. Nearest structural relative on the tracker is gh-98783
(*"`int.from_bytes` crashes if `byteorder` is a string subclass"*) — same trigger mechanism, different site.

**Fix.** Either take strong references in `super_getattro` before the call:

```c
/* Objects/typeobject.c:12699 */
    PyTypeObject *type = (PyTypeObject *)Py_XNewRef(su->type);
    PyObject *obj = Py_XNewRef(su->obj);
    PyTypeObject *obj_type = (PyTypeObject *)Py_XNewRef(su->obj_type);
    PyObject *res = do_super_lookup(su, type, obj, obj_type, name, NULL);
    Py_XDECREF(obj_type); Py_XDECREF(obj); Py_XDECREF(type);
    return res;
```

…or pin them with `_PyCStackRef` exactly the way `mro` already is nine lines earlier — which is the
smaller, more idiomatic change and the one that matches the file's existing vocabulary.

**Class and sibling propagation.** This is *not* the init-bypass class; it is
**borrowed-ref-across-call** (briefing shape 4) specialised to the **re-init-in-place** family:
a C type whose `tp_init` can be re-run on a live object from Python (`T.__init__(obj, …)`), whose methods
load owning fields into locals before calling into Python. That family already has six catalog members in
one file — CPY-0044/0045/0046/0048/0049/0050, all `Struct.__init__` re-entrancy. **`super` is the same
shape in a second file.** `super` is the only type in `typeobject.c` with re-runnable owning state
(`type_init` sets nothing; `object_init` sets nothing; `_buffer_wrapper` is not instantiable — §4), so
the in-file sibling hunt is exhausted; the tree-wide hunt is "every `tp_init` that `Py_XSETREF`s an owning
field", and it belongs to `scan_refcounts`, not here (recall gap R2).

**Suggested catalog record: CPY-0091**, category `refcount`, status `reproduced`.

### 2d. Bounded negative — the rest of the `include_map` "unflagged lead"

`include_map` §R37 flagged the `do_super_lookup` borrow generally. Resolving the rest of it:

- `su_type` is **not** part of the finding: within `_PySuper_LookupDescr` it is only pointer-compared
  (`:12608`), and its only other use is the `skip:` vectorcall (`:12673`), which runs only when `su == NULL`
  — i.e. from `_PySuper_Lookup`, which owns its arguments.
- `f`'s own re-entrancy is **not** a second window: `slot_tp_descr_get:11153` binds `obj`/`type` into the
  callee frame with no intervening Python, so there is no gap between "`f` entered" and "arguments
  increfed". The freeing has to happen *before* `f` is entered, which is what the `__hash__` window does.
- `Py_SETREF(res, res2)` at `:12661` runs a `__del__`, but neither borrowed value is read after it.

---

## 3. `__class__` assignment (7482–7846) and managed static types (228–522)

**`__class__` assignment — ACCEPTABLE for this class, three independent reasons:**

1. `object_set_class:7803-7807` **rejects deletion** outright (`"can't delete __class__ attribute"`), so
   the `deletable_getset` half of the class does not apply. It is the second of the two explicit
   NULL-rejecting setters in the file (the other is `type_set_annotate:2131`).
2. The mandate's question — *what reads a field whose validity was tied to the OLD type* — is answered
   by `compatible_for_assignment:7628-7695`: the swap requires identical `tp_free`, identical
   `tp_dictoffset` / `tp_weaklistoffset`, matching `Py_TPFLAGS_HAVE_GC|INLINE_VALUES|PREHEADER`, and
   equal solid bases via `compatible_with_tp_base` / `same_slots_added`. So the new type's slots read the
   *same C struct* the old type's did — there is no field whose existence was tied to the old type.
   The `super` payload is reachable only from `PySuper_Type` descendants (probe 13:
   `S1.__new__(S1).__class__ = S2` between two `super` subclasses works and reprs cleanly, rc 0).
3. The oldto/newto refcount ordering is sound: `Py_INCREF(newto)` at `:7787` precedes `Py_SET_TYPE` at
   `:7790`; `Py_DECREF(oldto)` at `:7832` happens after `types_start_world()`, so at no point is `self`
   typed by an object with no reference.

Two things in this region are **real but belong to other agents**, and I flag them rather than triage them:
`object_set_class_world_stopped:7778-7780` takes `Py_BEGIN_CRITICAL_SECTION(dict)` **inside** the
`!unique` stop-the-world window (stw-safety / lock-discipline), and `same_slots_added:7609` runs
`PyObject_RichCompareBool` on `ht_slots` inside that same stopped world (already CPY-0078 for its
tri-state collapse; the *STW* angle is new and is not mine).

**Managed static types — ACCEPTABLE, no Python surface.** `managed_static_type_state_get:285-297` can
return a state whose `->type != self` when `index > _Py_MAX_MANAGED_STATIC_EXT_TYPES` (`:294-296`), and
`init_tp_subclasses:708-709` writes through `_PyStaticType_GetState` with no NULL/identity assert — that
is CPY-0089's third item, **confirmed still present**. The region's entry points are interpreter init/fini
and `_PyStaticType_InitForExtension`; there is no `__new__`/`del` path into it, so it is out of this class.

---

## 4. Deletable members / getsets, and the `T.__new__(T)` sweep

### 4a. Deletable members/getsets — confirming pass 1, plus one extension

- `type_members:1468-1480` — 6 entries, **6 `Py_READONLY`**. `super_members:12545-12552` — 3 entries,
  **3 `Py_READONLY`**. Confirmed by eye at HEAD; pass 1 confirmed all nine `delattr` attempts raise
  `AttributeError: readonly attribute`. There is **no `del` twin** of CPY-0007 — unlike gh-152817
  (`del cursor.row_factory`), the `super` fields are sealed.
- `type_getsets:2376-2391` (12) + `object_getsets:7841-7845` (1) + `subtype_getset_dict:4087` +
  `subtype_getset_weakref:4092`: 7 route through `check_set_special_type_attr:1485-1489`
  (*"cannot delete '%s' attribute of type '%s'"*), 2 reject explicitly, 3 have no setter
  (`__mro__`, `type.__dict__`, `__text_signature__`; `subtype_getset_weakref` also has a NULL setter),
  2 accept deletion but touch only the type dict, and 1 — `subtype_setdict:4038` → `_PyObject_SetDict:4032`
  — forwards NULL to a `Py_NewRef`. That last one is pass 1's finding 3 and CPY-0089's fourth item;
  **confirmed unchanged**, still latent (managed-dict branch always wins today).
- **Extension pass 1 did not cover:** `__slots__` entries become **deletable** `_Py_T_OBJECT_EX`
  members (`type_new_descriptors` → `_PyHeapType_GET_MEMBERS`, `:4656`). `del o.slot` genuinely NULLs a
  `PyObject *` in the instance. No C reader in this file consumes a `__slots__` slot value —
  `same_slots_added:7606-7611` reads `ht_slots`, the tuple of *names*, and `subtype_dealloc` /
  `subtype_clear` use `Py_XDECREF` / `Py_CLEAR`. Empirically clean: probes 9–12 (delete a slot then read
  it → `AttributeError`; delete then dealloc; `__class__`-swap two slotted classes then read the unset
  slot → `AttributeError`; swap then dealloc) all rc 0. **ACCEPTABLE.**

### 4b. `T.__new__(T)` sweep — and a **correction to pass 1**

`typeobject.c` defines exactly four `PyTypeObject`s (grep-verified: `:7290`, `:8557`, `:11306`, `:13025`;
no `PyType_Spec` in this file):

| type | `tp_new` | `tp_init` | bypassable? |
|---|---|---|---|
| `PyType_Type` `:7290` | `type_new` (real) | `type_init` `:4115` | **No.** `type_init` sets nothing; `type.__new__(type)` → `TypeError: type.__new__() takes exactly 3 arguments (0 given)` (verified). |
| `PyBaseObject_Type` `:8557` | `object_new` (real) | `object_init` | **No.** No payload. |
| `_PyBufferWrapper_Type` `:11306` | `0` | `0` | **No — pass 1 says otherwise and pass 1 is wrong.** See below. |
| `PySuper_Type` `:13025` | **`PyType_GenericNew`** | `super_init` `:13063` | **Yes.** The shape. → CPY-0007 / CPY-0080. |

**Correction to pass 1.** Pass 1's Phase-0 table records `_PyBufferWrapper_Type` as
*"`0` → inherits `object_new` … Instantiable"* and calls it the in-file guarded twin on that basis.
It is **not instantiable**:

```c
/* Objects/typeobject.c:9428-9437  type_ready_set_new */
    if (type->tp_new == NULL
        && base == &PyBaseObject_Type
        && !(type->tp_flags & Py_TPFLAGS_HEAPTYPE))
    {
        if (initial) {
            type_add_flags(type, Py_TPFLAGS_DISALLOW_INSTANTIATION);
        } ...
```

A **static** type with `tp_new == 0` whose base is `object` gets `Py_TPFLAGS_DISALLOW_INSTANTIATION`
applied *automatically* and `tp_new` forced to NULL (`:9457-9460`). Verified from Python: the type is
reachable (`type(memoryview(B()).obj)` → `<class '_buffer_wrapper'>`), its `__flags__` is `0x5182` with
`Py_TPFLAGS_DISALLOW_INSTANTIATION` set, and both `BW()` and `BW.__new__(BW)` raise
`TypeError: cannot create '_buffer_wrapper' instances` (probes 3–7).

The NULL guard at `bufferwrapper_releasebuf:11279` is therefore *defensive*, not load-bearing — it is
still the right pattern to cite as a twin, but the type cannot reach it via a bypass. This matters beyond
bookkeeping: it is a general rule of the object model that the scanner currently gets wrong (P2 below).

---

## Toolkit assessment

### Precision of the two new rules, on this file

| rule | fired | TP | FP | precision |
|---|---|---|---|---|
| `one_hop_param_deref` | 2 | 1 (`:12797` = CPY-0007) | 1 (`:3894`) | **1/2** |
| `_VARARG_SENTINEL_SINKS` → `vararg_null_truncation` | 1 | 1 (`:12793` = CPY-0080) | 0 | **1/1** |

Both rules did the job they were built for, and the two supporting mechanisms are verified correct here:

- **Line accuracy is fixed.** The `one_hop` rule lands on `:12797` — the body line where control actually
  dies — not on the signature. That is precisely the coordinate CPY-0007 records by hand, and it is the
  frame ASan prints. The `func["start_line"]` → body-node fix is confirmed working on a real case.
- **`dominated_by` / `dominates` is correct.** `:12806` (`Py_NewRef(su->type)`) is annotated
  `dominated_by: 12797` and `:12797` carries `dominates: [12806]`. This is exactly right — pass 1's
  recall-gap-1 argument was that `:12806` *can never execute* on the crashing path, and the annotation now
  says so in the JSON instead of requiring a human to notice.
- **`_same_path` mutual-exclusion analysis works.** `:12793` and `:12797` are the two arms of the
  `if (!Py_IS_TYPE(su, &PySuper_Type))` at `:12790`; both were reported, neither suppressed the other.
  A naive dominance rule would have collapsed one into the other.

### P1 — precision defect: field names are still not type-qualified through a **local**

`_resolve_target_field:615-628` accepts any `X->field` whose trailing member name is in the file's
nullable set, and rejects only when the base is in `foreign` — and `_non_receiver_params` builds `foreign`
from **non-receiver parameters only**. A local of an unrelated struct type (`slot_update_item_t *item`)
is neither the receiver nor a parameter, so nothing rejects it. That is the `:3894` FP, and it is the
documented blind spot arriving in practice.

> **Proposed rule.** `_collect_nullable_fields` already knows the `tp_init` function and its receiver;
> capture the receiver's **declared struct type** (`superobject *su = superobject_CAST(self);` →
> `superobject`) alongside each `new_bypass` field. At the read site, resolve the base's declared type
> from local declarations in the enclosing function body (`(\w+)\s*\*\s*<base>\s*=`) and require it to
> equal the owner type, be a cast to the owner type, or be unresolvable. On this file that kills `:3894`
> (`item` is declared `slot_update_item_t *`) and keeps `:12793`/`:12797` (`su` is declared
> `superobject *`). Pass 1 asked for a regression fixture built from `super_descr_get`; build it from
> `apply_slot_updates` **and** `super_descr_get` — one pins the negative, the other the positive.

### P2 — precision defect: `tp_new = 0` is **not** bypassable for a static type whose base is `object`

`_INHERITED_NEW = {"0", "NULL", "PyType_GenericNew"}` (`:187`) treats `tp_new = 0` as bypassable for all
three slot-table forms. For the **positional / designated static `PyTypeObject`** form that is wrong
whenever `tp_base` is `0` or `&PyBaseObject_Type`: `type_ready_set_new:9428-9437` auto-sets
`Py_TPFLAGS_DISALLOW_INSTANTIATION` and NULLs `tp_new`. The comment in CPython spells it out —
*"tp_new is not inherited for static types whose base class is 'object' … Heap types, of course, are under
our control, so they do inherit tp_new."*

Cost here: zero (no `tp_init`-bearing type in this file has `tp_new == 0`), but it is a tree-wide
over-report generator — `Modules/` is full of static types with `tp_base = 0`.

> **Proposed rule.** In `_positional_bypassable_inits` / the designated branch, treat `0`/`NULL` as
> bypassable **only if** the same block sets a non-zero `tp_base` **or** the type is a heap type
> (`PyType_Spec` / `PyType_FromSpec`). `PyType_GenericNew` stays unconditionally bypassable — that is
> gh-144330 / `bytearray` / `super`. Negative-control fixture: `_PyBufferWrapper_Type` from this file,
> which must produce **zero** nullable fields.

### R1 — recall gap (fragility): `new_bypass` fields are found only in a function *named* like the `tp_init`

`_collect_nullable_fields:584-591` matches the slot-table `tp_init` name and `<name>_impl`. In this file
the `tp_init` is `super_init:12898`, which **assigns nothing** — it parses arguments and delegates to
`super_init_impl:12914`, where the three `Py_XSETREF`s live. The rule only sees it because that helper
happens to be spelled `_impl`, which is the Argument Clinic convention — and `super_init` is **not**
clinic-generated. Had the helper been called `super_do_init`, `total_nullable_fields` for this file would
be **0**, and CPY-0007 / CPY-0080 would both be invisible. The rule's single most-cited success rests on
a naming coincidence.

> **Proposed rule.** Follow one hop out of the `tp_init` body: if the `tp_init` calls a same-file `static`
> function passing its receiver (`self` / `su`) as an argument, union that callee's
> `_fields_set_in_init` into the nullable set. This is the same one-hop machinery
> `unguarded_param_sinks` already builds for `one_hop_param_deref`, run in the other direction — no new
> analysis, just a second use of an existing index. Negative control: it must not pull in
> `supercheck`/`do_super_lookup` (they do not receive the receiver as `self`).

### R2 — recall gap (out of class, stated for the owning agent)

Nothing in the toolkit models **"borrowed owning field, loaded into a local or passed as a parameter,
used across a call that runs Python, where the field can be replaced by a re-entrant `tp_init`."**
That is §2c, the run's only FIX. `scan_refcounts` keys `borrowed_field_deref_across_call` on this file's
four `lookup_tp_*` accessors, so a plain struct field like `su->obj` is invisible to it — the
`include_map` predicted exactly this and it held. The shape has **seven** confirmed catalog members now
(CPY-0044/0045/0046/0048/0049/0050 in `_struct`, plus this one), which is more than enough to justify a
rule.

> **Proposed rule (for `scan_refcounts`, not this scanner).** For each type with a `tp_init` that
> `Py_X?SETREF`s an owning field, mark those fields *re-init-volatile*. Then flag any function that loads
> a re-init-volatile field into a local (or passes it as an argument) and uses that value after an
> intervening Python-reaching call, with no intervening `Py_INCREF` / `_PyCStackRef` pin. The guarded twin
> is in-file and machine-checkable in this very case: `_PySuper_Lookup` pins by owning, `super_getattro`
> does not.

### Denominator note

`total_nullable_fields: 3` was verified against a hand enumeration of every struct in the file, not merely
observed non-zero. The rule is genuinely exercised here. `files_with_nullable_fields: 1` of 1;
`functions_analyzed: 459`; `duplicate_locations: 0` on all four findings.

---

## Reproduction artefacts

All under
`/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`:

| file | purpose |
|---|---|
| `super_lookup_uaf.py` | the §2c FIX; `<interp> super_lookup_uaf.py uaf` → rc 139, `… unbind` → rc 139 |
| `super_lookup_sweep.py` | the 8-probe crash/survive split for §2c, incl. the `_PySuper_Lookup` negative control |
| `super_lookup_uaf2.py` | `su_obj_type` variant + exact-str negative control (both rc 0 — the `su_obj` variant is the reachable one) |
| `pass2_probes.py` | 14 probes: CPY-0007/0080 confirmation, `_buffer_wrapper` instantiability (§4b correction), `__slots__` deletable-member sweep, `__class__`-swap probes |
