# Refcount audit — `Objects/typeobject.c`, PASS 2

**Slice:** `obj-typeobject`, informed-explore pass 2
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), 13,068 lines
**Scope:** (a) the 18-row NET NEW baseline from `preflight/PASS2_BASELINE.md`, triaged as a class;
(b) the `borrowed_field_deref_across_call` recall gap named in the brief;
(c) the pass-2 regions R3 / R4 / R6 / R11 / R19 / R21 / R25 / R26 / R37, read for refcount errors.
**Builds:** `debug-gil-nojit`, `release-gil-nojit`, `debug-ft-nojit`, `release-ft-nojit`,
`debug-gil-nojit-asan` (all at `a1d580430c8`).
`git diff 4f3be1b5777 a1d580430c8 -- Objects/typeobject.c` is **empty** — the file is
byte-identical between the target ref and every matrix build, so all line numbers below are
valid on both.

---

## Summary

| | |
|---|---|
| Functions in file (scanner denominator) | 403 |
| `scan_refcounts` findings, slice-scoped | 18 (`borrowed_field_deref_across_call` ×18; every other rule 0) |
| … already recorded | 5 (CPY-0068, CPY-0069 ×2, CPY-0070, CPY-0083) |
| … **confirmed live hazard** | 1 (`:9403`, pass 1's CONSIDER — re-confirmed) |
| … **ACCEPTABLE by the per-field rule** | 8 |
| … **false positive** (mechanical scanner defect) | 4 |
| **New FIX found by reading** | **2**, both ASan-confirmed heap-use-after-free + SIGSEGV |
| New CONSIDER found by reading | 4 |
| Bounded negatives established | 9 |

**Scanner precision on the borrowed-field rule for this file: 6/18 = 33 %** (5 recorded + 1 live).
**Scanner recall: it found 0 of the 2 new FIXes**, for the exact reason the preflight predicted —
both borrows are plain struct-field / parameter loads, not `lookup_tp_*()` returns.

Both new FIXes are the briefing's crown-jewel *borrowed-ref-across-call* shape, both have a guarded
twin **in this same file**, and both are reachable from ~40 lines of pure Python with no imports.

---

# Part 1 — The `borrowed_field_deref_across_call` class

## 1.1 The per-FIELD verdict (the discriminator)

The brief is right that this resolves per field, not per call site. The load-bearing fact for
`tp_dict` is mechanical and checkable in one place — **`type_getsets[]` (`:2376`) registers
`{"__dict__",  type_dict,  NULL, NULL}` at `:2384`: the setter slot is `NULL`.** Same for
`__mro__` (`:2380`).
`__bases__` is the only one of the four with a live Python-level writer.

| Field | Writers | Python-reachable writer? | Can the *pointer* change on a live ready type? | Verdict for a borrow across a Python-reaching call |
|---|---|---|---|---|
| `tp_dict` | `set_tp_dict` :4919 (`type_new_init`, before `PyType_Ready`), :9139 (`PyType_Ready`); `clear_tp_dict` :6915 (static-type fini) and `type_dealloc` | **No** — `type.__dict__` has a NULL setter; there is no other path | **No** | **ACCEPTABLE**, provided the *type* is held for the window (it is, at all 10 sites) |
| `tp_bases` | `set_tp_bases` :1940 / :2006 / :4432 / :5645 / :9122 | **Yes** — `T.__bases__ = …` → `type_set_bases_unlocked:1940`; the old tuple is released at :1978 or :2015 | **Yes** | **live hazard** |
| `tp_mro` | `set_tp_mro` :1828, :1994, :3678; `clear_tp_mro` :6917 | **Yes** — `T.__bases__ = …` and a custom `mro()` both reach `mro_internal:3678` | **Yes** (GIL builds). On FT, `set_tp_mro:664` calls `PyUnstable_Object_EnableDeferredRefcount(mro)`, which defers the free — the FT arm is a *mitigation*, not an absence of the bug | **live hazard** |
| `tp_subclasses` | `init_tp_subclasses` :9710, `clear_tp_subclasses` :9791 / :6905 / :7024; *entries* mutated by `add_subclass` :9719 / `remove_subclass` :9782 | **Yes** — any subclass creation/teardown; the pointer is cleared at :9791 when the dict empties | **Yes**, but only via the empty→clear edge | **live hazard** (dict-mutation *and* pointer-clear) |

Denominators for this rule on this file: `borrowed_field_accessors = 4`,
`borrowed_accessor_load_sites = 45`, `borrowed_slot_load_sites = 52`. Raw accessor call counts:
`lookup_tp_dict` 31, `lookup_tp_mro` 26, `lookup_tp_bases` 15, `lookup_tp_subclasses` 11.

## 1.2 The per-SITE table (inherits from 1.1, with the exceptions called out)

| line | function | field | verdict | reason |
|---|---|---|---|---|
| :793 | `_PyType_GetSubclasses` | subclasses | **ACCEPTABLE + baseline correction** | see 1.3 |
| :1965 | `type_set_bases_unlocked` | bases | **CPY-0070** (confirm) | pass 1 bounded-negative #6 already established this is an *owned* use, not a stale borrow: `set_tp_bases:1940` overwrites without decref, so `old_bases` becomes the owner |
| :3443 | `mro_implementation_unlocked` | bases | **FP as stated → CONSIDER elsewhere in the fn** | see 1.4 |
| :4815 | `type_new_set_attrs` | dict | ACCEPTABLE (**POLICY** note) | `tp_dict` rule. `e42bda944111` did widen the borrow window for speed, and did so without a comment; it is safe only because `__dict__` has no setter |
| :5747 | `type_from_slots_or_spec` | dict | ACCEPTABLE | `tp_dict` rule; type not yet returned to any caller |
| :8655 | `type_add_method` | dict | **FP — branch exclusivity** | see 1.5 |
| :8706 | `type_add_members` | dict | ACCEPTABLE | `tp_dict` rule |
| :8731 | `type_add_getset` | dict | ACCEPTABLE | `tp_dict` rule |
| :8816 | `overrides_hash` | dict | ACCEPTABLE *as refcount* | `tp_dict` rule. This is the **trigger** site of CPY-0068, not a victim: `PyDict_Contains(dict, __eq__)` at :8814 is what runs the user `__eq__`; `dict` itself stays valid |
| :9166 | `type_dict_set_doc` | dict | ACCEPTABLE | `tp_dict` rule |
| :9272 | `type_ready_mro` | mro | **FP — early return + no Python** | see 1.5 |
| :9336 | `type_ready_inherit` | mro | **CPY-0068** (confirm) | reproduces; see also the *new* sibling on `tp_base` in Part 2.4 |
| :9388 | `type_ready_set_hash` | dict | ACCEPTABLE | `tp_dict` rule |
| :9403 | `type_ready_add_subclasses` | bases | **CONSIDER** (re-confirm) | pass 1 raised this; `add_subclass:9705-9707`'s own comment says `PyWeakref_NewRef` can run a GC and thus arbitrary Python. Still latent for the same accidental reason (`type_new:4432` holds a second strong ref to `ctx->bases`). Unchanged, still un-annotated at :9400 |
| :9790 | `remove_subclass` | subclasses | **CPY-0069 / CPY-0083** (confirm) | note the *intra*-function window is actually empty: the dict is `PyLong`-keyed and the values are `PyWeakref`s created with a NULL callback (:9699), so `PyDict_DelItem` at :9782 runs no Python. The hazard is the caller's iteration |
| :10516 | `add_tp_new_wrapper` | dict | ACCEPTABLE | `tp_dict` rule |
| :12377 | `recurse_down_subclasses` | subclasses | **CPY-0069** (confirm) | reproduces |
| :12477 | `add_operators` | dict | ACCEPTABLE | `tp_dict` rule |

**Baseline correction.** `PASS2_BASELINE.md` lists 14 `borrowed_field_deref_across_call` rows under
NET NEW (`RUN_CONTEXT.md` says 13 — the file's own two counts disagree; the scanner JSON has 14).
One of those 14, **`:793`, is not net new**: CPY-0083 already records
`_PyType_GetSubclasses` at `:783`/`:788` for exactly this borrow. The baseline's ALREADY-RECORDED
join is by exact line number, so a finding whose `line` is the *use* (793) and whose `load_line` is
the *load* (783) misses a catalog entry keyed on the load. **The join should be on
`(file, function, rule)`, not `(file, line)`.** Net new is therefore **17**, not 18, and the
borrowed-field class is **13**, not 14.

## 1.3 `:793 _PyType_GetSubclasses` — the comment holds, for a different reason than it states

```c
783    PyObject *subclasses = lookup_tp_subclasses(self);  // borrowed ref
788    // The loop cannot modify tp_subclasses, there is no need
789    // to hold a strong reference (use a borrowed reference).
793    while (PyDict_Next(subclasses, &i, NULL, &ref)) {
794        PyTypeObject *subclass = type_from_ref(ref);
799        if (PyList_Append(list, _PyObject_CAST(subclass)) < 0) { … }
804        Py_DECREF(subclass);
```

The scanner anchored on `PyList_Append`. **`PyList_Append` runs no Python here**: `list` is an exact
list, and `list_resize` reallocates `ob_item` with `PyMem_Realloc` — that is not a GC allocation, so
it cannot trigger a collection and cannot reach a `__del__`. The other candidate,
`Py_DECREF(subclass)` at :804, cannot reach zero because `list` took a strong reference at :799 one
line earlier. `type_from_ref` → `_PyWeakref_GET_REF` runs no Python either.

So the single-threaded claim at :788-789 is correct. What the comment does **not** cover is a
*concurrent* thread, and that is CPY-0083 (`static-confirmed`, "guarded only by a stale GIL-era
comment"). **Confirm, do not re-litigate.**

Contrast with the guarded twin 1,050 lines below: `mro_hierarchy_for_complete_type:1836-1846`
materialises a strong-ref list *precisely because* the loop it feeds can be re-entered. The
asymmetry between the two comments (`:788` "cannot modify" vs `:1838` "might be altered") is
justified.

## 1.4 `:3443 mro_implementation_unlocked` — FP as flagged; the real window is 50 lines later

The scanner anchored on `PyErr_Format` at :3445. That call is followed **unconditionally** by
`return NULL` at :3448, so no later use of `bases` can follow it — and its format arguments are
`%.100s` over `base->tp_name`, a C string, so it runs no Python anyway. **False positive: the
scanner does not model that the anchoring call is dominated by a `return`.**

Reading the whole function, the genuinely Python-reaching calls are:

* `check_duplicates(bases)` at :3476 → `class_name:3259` → `PyObject_GetOptionalAttr(__name__)` /
  `PyObject_Repr`, whose own comment at :3262 says *"Note that this is potentially re-entrant."*
  But `class_name` is only reached on the duplicate-found path, which returns `-1` immediately, and
  the caller then returns `NULL` at :3477. **Dead as well.**
* `set_mro_error(to_merge, …)` at :3420 (via `pmerge`) → `PyDict_SetItem(set, c, Py_None)` at
  :3322, which hashes a *class* through `Py_TYPE(c)->tp_hash` — a metaclass `__hash__` is arbitrary
  Python — while the loop at :3318-3327 is still reading `to_merge[i]`. `to_merge[0..n-1]` are the
  bases' borrowed `lookup_tp_mro()` tuples and `to_merge[n]` is the borrowed `bases`.
  → **[CONSIDER]**, see Part 3.1.

## 1.5 Two mechanical scanner defects

**(a) `:8655` — the two "calls" are the two arms of one `if`/`else`.**

```c
8650    PyObject *dict = lookup_tp_dict(type);
8651    if (!(meth->ml_flags & METH_COEXIST)) {
8652        err = PyDict_SetDefaultRef(dict, name, descr, NULL) < 0;
8653    }
8654    else {
8655        err = PyDict_SetItem(dict, name, descr) < 0;
8656    }
```

The finding says `api_call: PyDict_SetDefaultRef` (:8652) "precedes the use" at :8655. They are
**mutually exclusive branches** and never both execute. This is the ordinary-control-flow analogue
of the taxonomy's existing "*mutually exclusive preprocessor branches*" entry, which the scanner
already models for `#if`/`#else` but not for `if`/`else`. **New FP class.**

**(b) `:9272` — early return, plus a loop that only runs for static types.**

```c
9268    if (!(type->tp_flags & Py_TPFLAGS_HEAPTYPE)) {
9271        for (…) { PyTypeObject *base = _PyType_CAST(PyTuple_GET_ITEM(mro, i));
9273            if (base->tp_flags & Py_TPFLAGS_HEAPTYPE) {
9274                PyErr_Format(…, "%.100s", …);
9278                return -1;
```

Same `PyErr_Format`-then-`return` shape as :3443, and the loop body is guarded to non-heap types
whose MRO tuples are immortalised by `set_tp_mro:661`. Two independent reasons this cannot fire.

**Both defects share one root cause: the rule treats "a `PYTHON_REACHING_APIS` call textually
between the load and the use" as sufficient, without checking (i) that the call is not dominated by
a `return`/`goto` before the use, and (ii) that the call and the use are on the same control-flow
path.** `PyErr_Format` is in the API set because of `%R`/`%S`/`%T`; a `%.100s`-only format string
never reaches Python and could be excluded cheaply by inspecting the literal.

---

# Part 2 — The recall gap: borrowed **plain struct fields**, and what it hides

The brief asked me to chase the gap the preflight found: `borrowed_field_deref_across_call` keys on
the four `lookup_tp_*` accessors, so a *plain struct-field read* and a *borrowed field passed as a
function parameter* are both out of scope. Both of the following were found by reading, both are
reproduced, and neither is visible to any current rule.

## 2.1 [FIX] `super_getattro` hands three borrowed `su->` fields to `do_super_lookup`, which uses them after a call that runs arbitrary Python — heap-use-after-free (`Objects/typeobject.c:12699`, used at `:12656`-`:12660`)

**What.**

```c
12688  super_getattro(PyObject *self, PyObject *name)
12690      superobject *su = superobject_CAST(self);
…
12699      return do_super_lookup(su, su->type, su->obj, su->obj_type, name, NULL);
                                     ^^^^^^^^  ^^^^^^^  ^^^^^^^^^^^  all borrowed, no INCREF
```

Inside `do_super_lookup`:

```c
12647      res = _PySuper_LookupDescr(su_type, su_obj_type, name);
                → 12622  PyDict_GetItemRef(dict, name, &res)     /* runs arbitrary Python */
…
12653          descrgetfunc f = Py_TYPE(res)->tp_descr_get;
12656          res2 = f(res,
12659                   (su_obj == (PyObject *)su_obj_type) ? NULL : su_obj,
12660                   (PyObject *)su_obj_type);                 /* <- use after free */
```

`_PySuper_LookupDescr` walks `su_obj_type`'s MRO doing `PyDict_GetItemRef` on each class dict. A
class dict is allowed to hold **non-string keys** (`type_new_impl:4960` warns but permits), so a key
whose `__hash__` collides with `name` dispatches a user `__eq__` through `compare_generic`. That
user code calls `super.__init__(s, …)` on the *same live super object*, and
`super_init_impl:12950-12952` re-initialises it in place:

```c
12950      Py_XSETREF(su->type,     (PyTypeObject*)type);
12951      Py_XSETREF(su->obj,      obj);
12952      Py_XSETREF(su->obj_type, obj_type);
```

Each `Py_XSETREF` drops the old reference. If `su->obj` was the sole owner of the instance — the
normal case for `super(C, C())` — the instance is freed while `do_super_lookup`'s parameter still
points at it. `func_descr_get` then builds a bound method: `PyMethod_New` → `Py_INCREF(su_obj)`.

**Why the neighbouring code is not enough.** `_PySuper_LookupDescr` *does* defend itself — it pins
the MRO with `_PyThreadState_PushCStackRefNew` at :12601 with the comment *"Keep a strong reference
to mro because `su_obj_type->tp_mro` can be replaced during `PyDict_GetItemRef`."* The author saw
exactly this re-entrancy and protected the *tuple*, not the three `su->` fields that `do_super_lookup`
is holding one frame up. `do_super_lookup` pins nothing.

**Guarded twin.** `super_descr_get:12797-12806`, 100 lines below, does the identical thing correctly:

```c
12797      PyTypeObject *obj_type = supercheck(su->type, obj);   /* runs Python (__class__ lookup) */
…
12806      newobj->type = (PyTypeObject*)Py_NewRef(su->type);    /* RE-READ the field, then INCREF */
```

It re-reads `su->type` from the struct after the Python-reaching call instead of carrying a stale
local — the taxonomy's "*a re-read of the slot after the call is the guarded twin*" entry.
`_PySuper_LookupDescr:12601` is the second twin (pin, don't borrow).

**Reproduction.** `repro/refcount_super_getattro_uaf.py` — 40 lines of pure Python, **no imports**.
Four classes `C → B → A → object`; `A` holds the target function `foo`, `B` holds one non-string key
with `__hash__ == hash('foo')`, so the MRO walk hits the collision in `B` before finding `foo` in `A`.

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGSEGV** (139) |
| `release-gil-nojit` | **SIGSEGV** (139) |
| `debug-ft-nojit` | **SIGSEGV** (139) |
| `release-ft-nojit` | **SIGSEGV** (139) |
| `debug-gil-nojit-asan` | **heap-use-after-free**, READ of size 4 |

ASan, verbatim spine:

```
READ of size 4 at 0x6f20dfe0fee0
  #0 Py_INCREF            Include/refcount.h:286
  #2 PyMethod_New         Objects/classobject.c:80
  #3 do_super_lookup      Objects/typeobject.c:12656
  #4 _PyObject_GetAttrStackRef Objects/object.c:1369
freed by thread T0 here:
  #1 subtype_dealloc      Objects/typeobject.c:2876
  #5 super_init_impl      Objects/typeobject.c:12951
  #6 super_init           Objects/typeobject.c:12907
  #7 wrap_init            Objects/typeobject.c:10407
 #19 slot_tp_richcompare  Objects/typeobject.c:11074
 #23 compare_generic      Objects/dictobject.c:1224
 #29 _PySuper_LookupDescr Objects/typeobject.c:12622
 #30 do_super_lookup      Objects/typeobject.c:12647
previously allocated by thread T0 here:
  #2 _PyType_AllocNoTrack Objects/typeobject.c:2528
```

Note this is the **rare all-four-configurations** crash: unlike CPY-0068 and CPY-0069, deferred
refcounting does not mask it, because the freed object is an ordinary instance, not an MRO tuple.

**Fix.** Take strong references for the duration, or re-read the fields. The minimal change is at
the call site, matching `super_descr_get`:

```c
static PyObject *
super_getattro(PyObject *self, PyObject *name)
{
    superobject *su = superobject_CAST(self);
    …
    PyTypeObject *su_type = (PyTypeObject *)Py_XNewRef(su->type);
    PyObject *su_obj = Py_XNewRef(su->obj);
    PyTypeObject *su_obj_type = (PyTypeObject *)Py_XNewRef(su->obj_type);
    PyObject *res = do_super_lookup(su, su_type, su_obj, su_obj_type, name, NULL);
    Py_XDECREF(su_obj_type); Py_XDECREF(su_obj); Py_XDECREF(su_type);
    return res;
}
```

(The `_PySuper_Lookup:12775` entry point is **not** affected: there `su_type`/`su_obj` come off the
interpreter stack in `LOAD_SUPER_ATTR` and are alive for the whole call. Only the `super_getattro`
entry passes borrowed *struct fields*.)

**Prior art:** none. `gh api search/issues` over `super_getattro use-after-free`,
`do_super_lookup`, `super __init__ reinitialize crash` returns nothing relevant.

## 2.2 [FIX] `type_mro_modified` uses its borrowed `bases` parameter after `has_custom_mro()` runs arbitrary Python — heap-use-after-free (`Objects/typeobject.c:1299`, `:1301`)

**What.** `mro_internal` publishes the new MRO into `tp_mro` and then hands the *same borrowed
pointer* to `type_mro_modified`:

```c
3678      set_tp_mro(type, new_mro, initial);   /* tp_mro now owns MRO_B; the local is borrowed */
3680      type_mro_modified(type, new_mro);
3683      type_mro_modified(type, lookup_tp_bases(type));
```

and `type_mro_modified` runs arbitrary Python *before* it touches `bases`:

```c
1296      if (!Py_IS_TYPE(type, &PyType_Type) && has_custom_mro(type)) goto clear;
              → 1268  _PyType_LookupStackRefAndVersion(Py_TYPE(tp), &_Py_ID(mro), …)
                       → find_name_in_mro → dict lookup in the METAclass dict
                          → non-string key, user __eq__  → re-entrant  T.__bases__ = (…)
1299      n = PyTuple_GET_SIZE(bases);                 /* <- use after free */
1301          PyObject *b = PyTuple_GET_ITEM(bases, i);
```

The re-entrant `T.__bases__ = (…)` runs a nested `mro_internal`, which replaces `tp_mro` and hands
the outer MRO to `mro_hierarchy_for_complete_type` as its `old_mro`; the last reference dies with
the rollback list at `type_set_bases_unlocked:1952 Py_DECREF(temp)`.

**`mro_internal`'s own re-entrancy defence does not cover this.** The pointer-identity test at
`:3667` (`reent = (lookup_tp_mro(type) != old_mro)`, with the `Py_XNewRef` at `:3665` taken
specifically so the address cannot be recycled) sits **before** `set_tp_mro`. Everything from
`:3678` onward — including both `type_mro_modified` calls — is unprotected. The comment block at
`:3579-3588` even enumerates three re-entrancy sources for this region and stops at `mro_invoke`.

**Guarded twin.** `find_name_in_mro:6170-6174` and `_PySuper_LookupDescr:12597-12601`, the same two
twins as CPY-0068: both pin the tuple with a `_PyCStackRef` before doing dict lookups, with a comment
naming this hazard. `type_mro_modified` pins nothing, and its `bases` parameter comes from an
already-published `tp_mro`.

**Reproduction.** `repro/refcount_type_mro_modified_uaf.py` — 45 lines of pure Python, no imports.
Two steering steps were needed and are documented in the file:

1. The metaclass's version-tag budget (`MAX_VERSIONS_PER_CLASS` = 1000, `:1389`) is exhausted so
   `should_assign_version_tag` refuses, `tp_version_tag` stays 0, and every `mro` lookup genuinely
   walks `find_name_in_mro` instead of hitting the method cache.
2. The **first** `mro` lookup of the outer assignment is `mro_invoke:3603`'s
   `call_method_noarg(type, "mro")`, and that one *is* protected — the `:3667` identity test catches
   it and `mro_internal` returns 0. The re-entrant assignment must therefore be delayed to the
   **second** lookup, which is `has_custom_mro`'s. A probe
   (`repro/scratch/scratch_mro_probe2.py`) confirmed exactly three lookups per assignment, with
   hit 0 observing the old `tp_mro` and hits 1–2 observing the new one.

| build | result |
|---|---|
| `debug-gil-nojit` | **SIGSEGV** (139) |
| `release-gil-nojit` | silent (exit 0) — recycled block reads as intact |
| `debug-ft-nojit` / `release-ft-nojit` | silent — `set_tp_mro:664` `PyUnstable_Object_EnableDeferredRefcount(mro)` defers the free. Per the briefing's `#ifdef`-asymmetry rule this is evidence *for* the finding: the FT arm is the mitigation |
| `debug-gil-nojit-asan` | **heap-use-after-free**, READ of size 8 |

ASan, verbatim spine:

```
READ of size 8 at 0x738040e5ad58
  #0 _Py_TYPE_impl             Include/object.h:234
  #1 PyTuple_GET_SIZE          Include/cpython/tupleobject.h:24
  #2 type_mro_modified         Objects/typeobject.c:1299
  #3 mro_internal              Objects/typeobject.c:3680
  #4 mro_hierarchy_for_complete_type Objects/typeobject.c:1803
  #5 type_set_bases_unlocked   Objects/typeobject.c:1949
freed by thread T0 here:
  #4 tuple_dealloc             Objects/tupleobject.c:277     (the rollback 3-tuple)
  #8 list_dealloc              Objects/listobject.c:567      (temp)
 #11 type_set_bases_unlocked   Objects/typeobject.c:1952     (Py_DECREF(temp))
 #20 maybe_call_special_one_arg Objects/typeobject.c:3199    (the user __eq__)
```

MROs are built 32 entries deep so the freed tuple escapes the `PyTuple_MAXSAVESIZE`(20) freelist —
the same masking the briefing records for CPY-0004 and pass 1 records for CPY-0068.

**Fix.** Pin the tuple for the duration of `type_mro_modified`, exactly as `find_name_in_mro` does:

```c
static void
type_mro_modified(PyTypeObject *type, PyObject *bases)
{
    ASSERT_TYPE_LOCK_HELD();
    PyThreadState *tstate = _PyThreadState_GET();
    _PyCStackRef bases_ref;
    _PyThreadState_PushCStackRefNew(tstate, &bases_ref, bases);
    …                              /* single exit through a pop */
```

Alternatively, hoist `has_custom_mro(type)` out of `type_mro_modified` and evaluate it in
`mro_internal` *before* `set_tp_mro` — but that changes what the check observes, so the pin is the
conservative fix. Note `:3683` passes `lookup_tp_bases(type)`, which has the same exposure through
the same `has_custom_mro` call.

**Prior art:** none found.

## 2.3 [CONSIDER] `_Py_type_getattro_stackref` carries a borrowed `metatype = Py_TYPE(type)` across two Python-reaching lookups (`:6574`, used at `:6610` and `:6660`)

The second miss the preflight named. `metatype` is captured once at `:6574` and passed to
`meta_get(...)` at `:6609-6610` and `:6659-6660`, after `PyType_Ready(type)` (`:6586`) and
`_PyType_LookupStackRefAndVersion(metatype, …)` (`:6598`) — both of which can run arbitrary Python.
`Py_TYPE(type)` **is** mutable from Python: `C.__class__ = OtherMeta` is legal between two
layout-compatible heap metaclasses and reaches `object_set_class:7826` → `Py_SET_TYPE` (`:7790`) →
`Py_DECREF(oldto)` (`:7832`).

**Why this is CONSIDER and not FIX — the bound, stated explicitly.** A heap metatype cannot reach
refcount 0 promptly: its own `tp_mro` tuple holds a strong reference to it, so it is always in a
cycle and needs the cycle collector. And the collector cannot take it during the dangerous window,
because `find_name_in_mro:6172-6174` pins `metatype->tp_mro` with a GC-visible `_PyCStackRef` — and
that tuple's item 0 *is* `metatype`. The pin is released when `_PyType_LookupStackRefAndVersion`
returns, and between there and the `meta_get` call the only allocation is inside the descriptor
dispatch itself, which INCREFs `metatype` as it builds its argument vector. The residual window is
a GC triggered by that dispatch's own first GC-tracked allocation (`_PyObject_MakeTpCall` →
`_PyTuple_FromArray` → `PyTuple_New` *before* the arguments are INCREF'd). I did not reproduce it.

There is a second, non-memory-safety consequence that *is* certain: after a re-entrant
`C.__class__ = M2`, the descriptor's `__get__` receives the **pre-swap** metaclass as its `objtype`
argument. That is a silent wrong answer, not a crash.

**Fix if taken:** re-read `Py_TYPE(type)` immediately before each `meta_get` call, or push a
`_PyCStackRef` on `metatype` alongside the three the function already maintains.

## 2.4 [CONSIDER] `type_ready_inherit` also carries a borrowed `tp_base` across `inherit_slots` (`:9326`, used at `:9346`)

A second, independent borrow in **the same function as CPY-0068**, on a different field, invisible
for the same reason (plain struct field):

```c
9326      PyTypeObject *base = type->tp_base;          /* borrowed, no INCREF */
9328      if (base != NULL) inherit_special(type, base);
9335      for (…) { … inherit_slots(type, b) … }       /* runs arbitrary Python — CPY-0068's window */
9345      if (base != NULL) {
9346          type_ready_inherit_as_structs(type, base);   /* reads base->tp_as_async, … */
```

The same re-entrant `X.__bases__ = …` that drives CPY-0068 also runs
`type_set_bases_unlocked:1941` (`type->tp_base = Py_NewRef(best_base)`) and releases the old
`tp_base` at `:1979`/`:2016`. `type_ready_inherit_as_structs` then dereferences the stale `base`.

**Why CONSIDER:** for the old base to be *freed* it must be uniquely referenced by `type->tp_base`,
and it is normally also held by `tp_bases`, by `tp_mro` and by whatever name bound it — the same
"latent, protected by an accident" situation as `:9403`. But the three fields are all replaced in
the same `type_set_bases_unlocked` call, so an anonymous base class is a plausible trigger. Pass 1's
CPY-0068 reproducer already stands in this exact window; it crashes at `:9336` first only because
the MRO loop has more iterations left. **The fix for CPY-0068 (pin the MRO) does not fix this one**
— that is the reason to record it separately.

## 2.5 The rule-widening proposal (toolkit assessment input)

`borrowed_field_deref_across_call` currently keys on `field_accessors_discovered` — static inline
functions whose body is a bare `return X->field;`. That set is 4 on this file and it is the *right*
seed; the gap is that it stops there. Three additive changes, in decreasing order of yield:

**W1 — treat a plain `X->field` load of a `PyObject*`/`PyTypeObject*` member as an accessor return,
when the file contains a re-binding writer for that member.** The gate that keeps this from
exploding is *not* the load, it is the writer: only report `X->f` if the same translation unit
contains `Py_XSETREF(X->f, …)`, `Py_SETREF(X->f, …)`, `Py_CLEAR(X->f)`, or the save-old-then-store
idiom (`old = X->f; X->f = new; Py_XDECREF(old);`) on the *same struct member name*. On this file
that gate resolves `su->type` / `su->obj` / `su->obj_type` (written by `Py_XSETREF` at
`:12950-12952`) and `type->tp_base` (written at `:1941`, released at `:1979`), and it *rejects*
`ht->ht_module`, `bw->mv`, `entry->name` and the rest that have no re-binding writer reachable from
Python. That single rule finds both of my new FIX/CONSIDER pairs, and finding 2.1 is the one it
would have caught with high confidence. The mechanical sweep is cheap: on this file the candidate
load set is 24 lines (`grep -E '^\s*(PyObject|PyTypeObject)\s*\*\s*\w+\s*=\s*(Py_TYPE\(|\w+->…)'`).

**W2 — follow a borrowed load into a call as a *parameter*.** `super_getattro:12699` loads all
three fields directly in the argument list of `do_super_lookup`; there is no local at all in the
caller, and the dangerous use is one frame down. The scanner already builds an intra-file call graph
for `[same-file helper, reaches Python]` attribution (it is in the `detail` strings). Extend it one
hop: if `f(…, X->field, …)` is called and `f`'s corresponding parameter is used after `f` makes a
Python-reaching call, report at the *caller* line with the callee use line. Same treatment for
`type_mro_modified(type, new_mro)` at `:3680`, where the borrow is the *argument* and the use is in
the callee.

**W3 — two cheap precision gates that pay for W1/W2's extra volume** (both are real defects
measured in §1.5, and both would also help the existing rule):
  * **Reject an anchoring call that is dominated by a `return`/`goto` before the use.** Kills
    `:3443` and `:9272`.
  * **Reject an anchoring call and a use that live in different arms of the same `if`/`else`.**
    Kills `:8655`. The scanner already does this for `#if`/`#else`; the same brace-depth machinery
    that computes `potential_double_free`'s "nested deeper than the steal" can express it.
  * Optional third: **a `PyErr_Format` whose literal format string contains no `%R`/`%S`/`%T`/`%N`
    runs no Python.** `%.100s`/`%U`/`%s`/`%d` are pure C. This is a one-line literal scan and it
    removes `PyErr_Format` from two of this file's four FPs.

**W4 — the per-field discriminator is computable.** The whole of §1.1 is mechanical: for each field
the accessor reads, find its writers in the file, and ask whether any writer is reachable from a
`PyGetSetDef` entry with a non-NULL setter, a `tp_setattro`, or a `PyMethodDef`. For `tp_dict` the
answer is "no writer is Python-reachable" and all 10 sites collapse to ACCEPTABLE in one step. Emit
this as a `field_verdicts` block in the envelope rather than 10 separate findings — it would have
cut this file's borrowed-field output from 18 to 8 with no loss.

---

# Part 3 — Regions read, with what was found

## 3.1 [CONSIDER] `set_mro_error` reads the borrowed `to_merge[]` tuples across a metaclass `__hash__` (`Objects/typeobject.c:3319-3322`)

```c
3318      for (i = 0; i < to_merge_size; i++) {
3319          PyObject *L = to_merge[i];                    /* borrowed tp_mro / tp_bases */
3321              PyObject *c = PyTuple_GET_ITEM(L, remain[i]);
3322              if (PyDict_SetItem(set, c, Py_None) < 0) { … }   /* hashes a CLASS */
```

`to_merge[0..n-1]` are `lookup_tp_mro(base)` results and `to_merge[n]` is `lookup_tp_bases(type)`
(`mro_implementation_unlocked:3497`, `:3501`) — all borrowed, none INCREF'd, and the array is a raw
`PyMem_New` block. `PyDict_SetItem(set, c, …)` hashes `c`, a class, through
`Py_TYPE(c)->tp_hash` — a metaclass `__hash__` is arbitrary Python and can reassign
`base.__bases__`, freeing the very MRO tuple the next loop iteration reads at `:3319-3321`.

Reachability is genuinely narrow: `set_mro_error` runs only when C3 linearisation *fails*, i.e. the
class statement is already going to raise `TypeError`. I did not attempt a reproducer. Recorded
because the raw `PyMem_New` cursor over borrowed tuples is exactly the taxonomy's
"*a raw `PyMem_Malloc` buffer hanging off a live object is NOT protected by its owner*" carve-out,
here in its stack-allocated form.

## 3.2 [CONSIDER] `bufferwrapper_releasebuf` uses two borrowed struct fields across a `bf_releasebuffer` slot call (`:11284-11285`, used at `:11291-11294`)

```c
11284      PyObject *mv  = bw->mv;
11285      PyObject *obj = bw->obj;
11287      assert(PyMemoryView_Check(mv));
11288      Py_TYPE(mv)->tp_as_buffer->bf_releasebuffer(mv, view);
11291      if (((PyMemoryViewObject *)mv)->view.obj != obj
11292              && Py_TYPE(obj)->tp_as_buffer != NULL
11293              && Py_TYPE(obj)->tp_as_buffer->bf_releasebuffer == slot_bf_releasebuffer) {
11294          releasebuffer_call_python(obj, view);
11297      Py_CLEAR(bw->mv);
11298      Py_CLEAR(bw->obj);
```

`PyMemoryView_Check` accepts subclasses, so `Py_TYPE(mv)->bf_releasebuffer` at `:11288` can be
`slot_bf_releasebuffer` → a Python `__release_buffer__`. `mv` and `obj` are then dereferenced at
`:11291-11293`. The `Py_CLEAR`s at `:11297-11298` are the *correct* spelling (they re-read the
field), which is precisely what makes the two stale locals above them the exposed part. Low
confidence on reachability — a re-entrant path back to the same `PyBufferWrapper` needs
establishing — and this is pass-1 territory (`slot_bf_*`), so it is recorded, not pursued.

## 3.3 Bounded negatives — these classes are clean here, and why

Each was read line by line; these are results, not unexamined areas.

1. **R26 pickle / `__reduce__` (7848-8406) is refcount-clean.** `reduce_newobj` balances
   `args`/`kwargs`/`copyreg`/`newobj`/`newargs`/`state`/`listitems`/`dictitems` on all three
   branches and on the `PyTuple_Pack` failure path (`:8309-8314`). Crucially it never caches
   `Py_TYPE(obj)`: every read (`:8230`, `:8233`, `:8264`, `:8280`) is a **fresh** field load
   immediately consumed by `Py_NewRef`/`PyTuple_Pack`, so the `__getnewargs__` and
   `import_copyreg` Python calls in between cannot leave a stale metatype. That is the guarded-twin
   shape for finding 2.3 — in the region history calls *cold*. The 2016/2013 Coverity NULL fixes
   (`07a2a1b7e512`, `2489bd83f576`) are the only enumeration this region ever got, and this pass
   adds a refcount clean bill.
2. **`object_getstate_default:7975-8041` is a guarded twin, not a finding.** `PyList_GET_ITEM` at
   `:7990` is wrapped in `Py_NewRef` on the same line, so the borrowed list item survives the user
   `__getattr__` that `PyObject_GetOptionalAttr` (`:7991`) may run. The size-stability check at
   `:8010-8014` is a *semantic* guard only — but nothing runs between it and the next iteration's
   `PyList_GET_ITEM`, so the list cannot shrink in the gap and there is no OOB. The
   `if (0) { error: … }`-inside-the-loop idiom (`:8016-8023`) disposes `slotnames`, `slots` and
   `state` exactly once on every arm.
3. **R25 `__class__` assignment (7482-7846) is refcount-clean.** `object_set_class:7825` loads
   `oldto` **after** the `PySys_Audit` at `:7816` (this ordering is `203565b2f9c7` / gh-120198's
   fix and it still holds), `Py_SET_TYPE` at `:7790` is the owner-swap, and the
   `Py_INCREF(newto)` / `Py_DECREF(oldto)` pair is symmetric under the same HEAPTYPE predicate.
   `same_slots_added:7609`'s `PyObject_RichCompareBool(slots_a, slots_b, Py_EQ)` looks like a
   Python-reaching call inside the stopped world, but `ht_slots` is validated to a tuple of
   identifier strings at class creation, so the comparison resolves to `unicode_richcompare` and
   runs no user code. (Its tri-state collapse is CPY-0078 — an error-path finding, not refcount.)
4. **`mro_internal:3665-3698` is net-balanced** — re-confirming pass 1's bounded negative #5. The
   `Py_XNewRef`/`Py_XDECREF` pair at `:3665`/`:3668` exists solely so the identity test at `:3667`
   cannot be fooled by address reuse; the disposal at `:3696`/`:3698` releases a *different*
   reference, the one `set_tp_mro:3678` orphaned. Do not "simplify" it.
5. **`type_set_bases_unlocked:1934-2020` ownership transfer is correct** — re-confirming pass 1's
   #6. Both the success path (`:1978-1979`) and `bail:` (`:2006`+`:2011-2012` / `:2015-2016`)
   dispose of exactly one reference to `old_bases` and `old_base`.
6. **R3 managed static types (228-522) does almost no refcounting.** The only `PyObject` traffic is
   `_PyStaticType_GetBuiltins:443`, which `Py_NewRef`s into a pre-sized `PyList_SET_ITEM`. The
   `state->type`/`interp_count` bookkeeping in `managed_static_type_state_init/_clear` is raw
   pointers and atomics with no ownership. Its hazards are concurrency, not refcounts.
7. **The type-watcher API (1054-1131) holds no references at all** — `tp_watched` is a bitmask and
   `interp->type_watchers[]` holds function pointers. The `%R` in `PyErr_FormatUnraisable` at
   `:1224` runs the metaclass `__repr__` inside `_PyType_Modified_Unlocked`'s borrowed
   `tp_subclasses` walk, which is pass 1's CPY-0084 / the `:1195` CONSIDER — unchanged.
8. **`update_cache:6216-6231`'s deliberately borrowed `entry->value` is not a leak or a UAF at this
   level.** The `/* borrowed */` store at `:6218` is the documented method-cache design; validity
   is carried by the version tag, and the release-store ordering at `:6230` is the invariant stated
   at `:6225-6229`. The returned `old_name` (which both callers spell `old_value`, `:6251` and
   `:6382`) is the previous cached *name*, and it is DECREF'd exactly once against the
   `Py_NewRef(name)` at `:6224`. **Misleading variable name, correct refcounting.**
9. **`type_setattro:6802-6843` and `type_update_dict:6697-6702`** — `metatype` at `:6787` is used
   only at `:6803` with nothing Python-reaching in between; `dict` at `:6812` is loaded *after* the
   `_PyType_LookupRef` at `:6803` and used at `:6829-6830` with nothing in between. The deferred
   `Py_XDECREF(old_value)` at `:6843` (past `END_TYPE_DICT_LOCK`) is deliberate and documented.
   Clean — re-confirming pass 1's #10.

---

# TOOLKIT ASSESSMENT

## Precision per rule, this file, this pass

| rule | fired | true positive | already recorded | FP | precision |
|---|---|---|---|---|---|
| `borrowed_field_deref_across_call` | 18 | 1 (`:9403`, CONSIDER) | 5 | 4 | **33 %** (6/18) counting recorded hits as correct; **6 %** (1/18) counting only net-new value |
| every other `scan_refcounts` rule | 0 | — | — | — | structural zero, see below |

**Denominators, as required before calling any zero clean.** `functions_analyzed = 403`,
`borrowed_field_accessors = 4`, `borrowed_accessor_load_sites = 45`,
`borrowed_slot_load_sites = 52`. The nine rules that reported 0 —
`potential_leak`, `potential_double_free`, `stale_slot_decref`, `owner_freed_before_use`,
`borrowed_ref_across_call`, `slot_transfer_across_call`, `stale_slot_use`, `init_not_reinit_safe`,
`new_missing_member_init` — have **no denominator field in the envelope at all**, so their zeros
are unaudited. Pass 1 established by scripted sweep that `stale_slot_decref`'s zero is a *real*
zero on this file (no `X->f = NULL;` followed by `Py_X?DECREF(local)` anywhere in 13,068 lines);
that remains true and I re-checked it against `set_tp_mro:671` and `set_tp_bases:620`, both of which
store without decref'ing on purpose and hand ownership to a caller local. **The other eight zeros
are silence, not safety** — in particular `owner_freed_before_use` has a `borrowed_slot_load_sites =
52` denominator sitting right next to it that it does not consume.

**Actionable:** add a per-rule `denominators` sub-block, not one flat map. `borrowed_slot_load_sites
= 52` versus `borrowed_accessor_load_sites = 45` tells me the scanner *saw* 52 plain slot loads and
reported on none of them — that number is the size of the recall gap in Part 2, and it is already
being computed.

## Recall

The rule found **0 of the 2 net-new FIXes and 0 of the 4 net-new CONSIDERs**. Every one of the six
is the *same* bug shape the rule exists to find; every one was invisible for the same structural
reason. Cumulatively across both passes, `scan_refcounts` has now reported **0 of the 4
ASan-confirmed heap-use-after-frees found by reading this file** (CPY-0068, CPY-0069 were pass 1;
2.1 and 2.2 are this pass) while emitting 18 findings of which 4 are mechanical false positives.

That is the strongest single message of this pass: **the accessor seed is the wrong end of the
problem.** The four `lookup_tp_*` functions were introduced by one 2023 encapsulation refactor
(`f73abf8e03fd`) that mechanically converted `type->tp_dict` into `lookup_tp_dict(type)` — so the
rule is keyed on a *naming convention introduced by a commit that was not a lifetime audit*, and
`tp_dict` turns out to be the one field of the four that cannot be swapped from Python at all. The
history's blame table predicted this precisely: six of the flagged sites are that refactor's direct
output and all six are ACCEPTABLE. Meanwhile `su->type` and `type->tp_base`, which *are* re-bound
from Python by `Py_XSETREF`, were never in scope.

## The rule-widening proposal

Reproduced from §2.5 for the record; W1 alone would have found 2.1 and 2.4, W2 adds 2.2, W3 removes
all four FPs, W4 collapses the ten `tp_dict` sites into one line.

- **W1** Treat `X->field` (`PyObject*`/`PyTypeObject*` member) as a borrowed-field load **iff** the
  same file contains a re-binding writer for that member name: `Py_XSETREF(X->f, …)`,
  `Py_SETREF(X->f, …)`, `Py_CLEAR(X->f)`, or `old = X->f; X->f = new; Py_XDECREF(old);`.
  The writer, not the load, is the precision gate.
- **W2** Follow a borrowed load one hop as a **call argument** — report at the caller when the
  callee uses the corresponding parameter after its own Python-reaching call. Reuses the existing
  `[same-file helper, reaches Python]` call graph.
- **W3** Three precision gates: (a) suppress when the anchoring call is dominated by a
  `return`/`goto` before the use; (b) suppress when the call and the use are in different arms of
  the same `if`/`else`; (c) treat `PyErr_Format` as non-Python-reaching when its literal format
  string contains no `%R`/`%S`/`%T`/`%N`.
- **W4** Emit a `field_verdicts` block computed from writer-reachability (is any writer reachable
  from a non-NULL `PyGetSetDef` setter / `tp_setattro` / `PyMethodDef`?) and collapse whole-field
  ACCEPTABLE verdicts into one finding instead of N.

## Two process notes for the campaign

- **The ALREADY-RECORDED join in `PASS2_BASELINE.md` must key on `(file, function, rule)`, not
  `(file, line)`.** `:793` was presented as net new although CPY-0083 records the same borrow at
  its *load* line `:783`. One of 18 rows, but the same failure would silently re-open any finding
  whose catalog entry cites the load and whose scanner finding cites the use.
- **`RUN_CONTEXT.md` says 13 borrowed-field rows, `PASS2_BASELINE.md`'s table has 14, the scanner
  JSON has 14.** Minor, but the two preflight documents should be generated from the JSON rather
  than transcribed.

## Artifacts

- `/home/danzin/projects/cpython-review-toolkit/reports/pass2/obj-typeobject/repro/refcount_super_getattro_uaf.py`
- `/home/danzin/projects/cpython-review-toolkit/reports/pass2/obj-typeobject/repro/refcount_type_mro_modified_uaf.py`
- `/home/danzin/projects/cpython-review-toolkit/reports/pass2/obj-typeobject/repro/scratch/scratch_mro_probe2.py`
  (the lookup-ordering probe that located the reachable window in 2.2)
