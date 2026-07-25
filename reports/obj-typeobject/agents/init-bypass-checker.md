# Init-Bypass NULL-Deref Analysis — `Objects/typeobject.c` (Pass 1, construction surface)

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0)
**Scope:** `/home/danzin/projects/cpython/Objects/typeobject.c` only — 13,068 lines, 417 functions.
**Mode:** INFORMED. Briefing + FP taxonomy + `scan_init_bypass.sample.json` read first.
**Build provenance:** `Objects/typeobject.c` in `debug-gil-nojit`, `debug-gil-nojit-asan`,
`release-gil-nojit` and `debug-ft-nojit` is **byte-identical** to the target ref (`diff -q` clean),
even though the matrix builds sit at `a1d580430c8`. Every reproduction below is therefore against
the exact source under review.

---

## Summary

- **Nullable fields seen: 3, across 1 file** (all `new_bypass`; `superobject::type`, `::obj`,
  `::obj_type`). The denominator is non-zero, so the rule *did* fire — and I hand-verified it is
  also **complete**: I enumerated every type defined in this file and found exactly these three.
- Candidate reads reported by the scanner: **1** (0 `duplicate_locations`).
- **FIX: 1** — CPY-0007, confirmed still live at HEAD, **not re-litigated**; three Python-reachable
  entry points instead of the one on record.
- **CONSIDER: 2** — both new.
- **ACCEPTABLE / CLEAN classes: 3** (bounded negatives, stated below).
- Toolkit: **2 concrete recall gaps + 1 precision hazard**, with proposed rule shapes.

---

## Phase 0 — enumeration (the base for every claim below)

`typeobject.c` defines exactly **four** `PyTypeObject`s. Complete list, with the two questions the
brief asked of each:

| Type | line | `tp_new` | `tp_init` | payload fields | verdict |
|---|---|---|---|---|---|
| `PyType_Type` | 7290 | `type_new` (real) | `type_init` (4115) | — | **CLEAN**: `type_init` only validates the 1-or-3 argument count and returns 0. It sets **nothing**, so there is no tp_init-only field to leave NULL. `type.__new__(type)` raises `TypeError: type() takes 1 or 3 arguments`. |
| `PyBaseObject_Type` | 8557 | `object_new` (real) | `object_init` | — | **CLEAN**: no payload. |
| `_PyBufferWrapper_Type` | 11306 | `0` → inherits `object_new` | `0` | `mv`, `obj` | **CLEAN, and it is the in-file guarded twin.** Instantiable, and both fields are set outside any `tp_init` (in `slot_bf_getbuffer:11348-11349`), so the bypass shape *is* present — but the only method that reads them, `bufferwrapper_releasebuf:11275`, opens with `if (bw->mv == NULL \|\| bw->obj == NULL) { return; }`. This is what the correct handling looks like, in the same file, 1,300 lines from the bug. |
| `PySuper_Type` | 13025 | **`PyType_GenericNew`** | `super_init` (12898) | `type`, `obj`, `obj_type` | **THE SHAPE.** `tp_init` sets all three at `12950-12952`; `tp_new` is `PyType_GenericNew`; no `DISALLOW_INSTANTIATION`. `super.__new__(super)` → zeroed object, all three NULL. |

`tp_new_wrapper`'s completeness (brief question (a)): the guard at `10454-10471` is the
`staticbase->tp_new != type->tp_new` check — the anti-`object.__new__(dict)` check, *not*
`hackcheck` (`10117-10218`, which is the unrelated Carlo-Verre `object.__setattr__`-on-a-type
guard and is not on this path at all). For `super` the staticbase check **passes by construction**:
`staticbase == &PySuper_Type` and `staticbase->tp_new == type->tp_new == PyType_GenericNew`, so
equality holds and the call proceeds to `PyType_GenericNew(super, (), NULL)`. It also passes for
`super.__new__(S)` where `S(super)` inherits `PyType_GenericNew`. **`PyType_GenericNew` is exactly
what defeats this guard** — that is the gh-144330 lesson, and `super` is an unfixed instance of it.

---

## Findings

### [FIX] CPY-0007 — confirmed at HEAD, **not re-litigated**, with two additional entry points

`Objects/typeobject.c:12797` (`super_descr_get`) → `Objects/typeobject.c:12763` (`supercheck`).

Confirmed in one line as instructed: `super.__new__(super).__get__(1)` still SIGSEGVs
(**exit 139**), `su->type` still reaches `supercheck` unguarded, and the ASan frame is exact:

```
#0 supercheck        Objects/typeobject.c:12763:50   (type->tp_name, type == NULL)
#1 super_descr_get   Objects/typeobject.c:12797:34   (supercheck(su->type, obj))
#2 wrap_descr_get    Objects/typeobject.c:10311:12
```

Reproduces identically on `debug-gil-nojit`, `release-gil-nojit`, `debug-ft-nojit`,
`debug-gil-nojit-asan` (rc 139 on all four) **and on released 3.14.4** → longstanding, not a
regression. Prior-art search returned nothing (`gh api -X GET search/issues` over
`super.__new__ crash`, `super __get__ segfault`, `supercheck NULL`,
`super object uninitialized __new__`) — still unreported.

**What is new and worth adding to the record: the crash has three Python-reachable entry points,
not one, and the third needs no explicit `__get__` call at all.**

| entry point | rc |
|---|---|
| `super.__new__(super).__get__(1)` (on record) | 139 |
| `super.__new__(super).__get__(1, int)` | 139 |
| `class C: d = super.__new__(super)` … `C().d` — **the ordinary descriptor protocol** | 139 |

The third is the one that matters for triage: no C-API-flavoured incantation, just storing the
object on a class and reading the attribute. `__get__(None)` is *not* an entry point — it is
filtered upstream by `wrap_descr_get:10306-10311` (`__get__(None, None) is invalid`).

**Full method sweep of `super` under the bypass** (24 probes, one process each). Only the three
above crash; everything else degrades cleanly, which is what makes the `__get__` asymmetry a bug
rather than a design:

- SURVIVE (`repr`, `str`) — `super_repr:12575,12580` writes `su->type ? su->type->tp_name : "NULL"`.
  **This is the guarded twin.** It prints `<super: <class 'NULL'>, NULL>`.
- SURVIVE (`.anything`, `.__class__`, `dir()`) — `super_getattro:12699` hands all three fields to
  `do_super_lookup`, which opens with `if (su_obj_type == NULL) goto skip;` (`12643`).
- SURVIVE (`gc.collect`, `hash`, `==`, `copy.copy`, `pickle.dumps`) — `super_dealloc:12561-12563`
  is `Py_XDECREF`×3; `super_traverse:12977-12979` is `Py_VISIT`×3 (NULL-safe).
- SURVIVE (`.__thisclass__`, `.__self__`, `.__self_class__`) — a NULL `_Py_T_OBJECT` member reads
  as `None`.
- CRASH — the three `__get__` forms above.

**Fix.** The one-line form that closes both this and finding 2 below, and matches how
`super_repr` / `super_getattro` already treat a NULL `type` (degrade, don't crash):

```c
/* super_descr_get:12786 */
if (obj == NULL || obj == Py_None || su->obj != NULL || su->type == NULL)
    return Py_NewRef(self);
```

The gh-144330-shape alternative — give `super` a real `tp_new` and drop `PyType_GenericNew` — also
works and closes every future entry point, but for `super` there is no sensible non-NULL initial
value for `su_type`, so the guard is the honest fix here. Note that `su->obj` is *already*
NULL-checked in that very condition: the author guarded one bypass-nullable field and not its
two siblings.

---

### [CONSIDER] `super_descr_get` silently truncates a call's argument list when `su->type` is NULL — `Objects/typeobject.c:12793`

**New.** Same function, same NULL, different branch — the one taken when `su` is an instance of a
*strict subclass* of `super`:

```c
/* Objects/typeobject.c:12790-12794 */
if (!Py_IS_TYPE(su, &PySuper_Type))
    /* If su is an instance of a (strict) subclass of super, call its type */
    return PyObject_CallFunctionObjArgs((PyObject *)Py_TYPE(su),
                                        su->type, obj, NULL);
```

`PyObject_CallFunctionObjArgs` is variadic and **NULL-terminated**: `object_vacall`
(`Objects/call.c`) counts with `while (1) { arg = va_arg(...); if (arg == NULL) break; nargs++; }`.
So when `su->type == NULL`, the intended two-argument call `SubSuper(su->type, obj)` becomes a
**zero-argument** call `SubSuper()`, and `obj` is silently dropped. Nothing crashes — but only
because NULL happens to be the sentinel. This is not a guard; it is a coincidence.

Reproduced (rc 0, wrong behaviour, identical on debug/release/FT and on 3.14.4):

| construction | result |
|---|---|
| `class S(super): pass` → `S.__new__(S).__get__(1)` | `RuntimeError: super(): no arguments` — the zero-arg `super()` path fires and inspects *the caller's* frame |
| `class S2(super):` with a no-op `__init__` → `S2(int,1).__get__(1)` | returns `<super: <class 'NULL'>, NULL>` — a **silently broken super object escapes into user code** |
| same, used as a class attribute descriptor | same broken object |

The second row is the interesting one: the descriptor protocol hands back a super whose three
fields are all NULL, with no error anywhere. That object is then a live carrier of the CPY-0007
shape.

**Guarded twin:** the same `if` at `:12786`, which NULL-checks `su->obj` (`su->obj != NULL`) three
lines above. Also `super_repr:12575`, which ternary-guards the same field.

**Fix:** the identical one-line change proposed for CPY-0007 — adding `|| su->type == NULL` to the
`:12786` early return makes both branches unreachable with a NULL `type`.

**Why this is not an FP-taxonomy class:** it is not "value returned directly", not
"NULL-tolerant consumer" (`PyObject_CallFunctionObjArgs` is not NULL-tolerant in the trailing
positions — it *reinterprets* NULL), and not a `!= Py_None` pseudo-guard. It is an unguarded read
of a `new_bypass`-nullable field feeding an API whose contract assigns NULL a different meaning.

---

### [CONSIDER] `_PyObject_SetDict` INCREFs a possibly-NULL value on one of its two branches — `Objects/typeobject.c:4032` (latent)

**New. State the reachability caveat first: I could not reach this from Python today.** Reporting
it because the asymmetry is exactly the bug shape and the fix is one character.

```c
/* Objects/typeobject.c:4012-4034  _PyObject_SetDict(PyObject *obj, PyObject *value) */
    if (Py_TYPE(obj)->tp_flags & Py_TPFLAGS_MANAGED_DICT) {
        return _PyObject_SetManagedDict(obj, value);      /* NULL-tolerant  */
    }
    PyObject **dictptr = _PyObject_ComputedDictPointer(obj);
    ...
    _PyObject_XSetRefDelayed(dictptr, Py_NewRef(value));  /* :4032  Py_NewRef, not Py_XNewRef */
```

`value == NULL` **is** part of this function's input domain: its in-file caller `subtype_setdict`
(`:4038`, the setter half of `subtype_getset_dict`) is the `__dict__` getset setter, and `del
obj.__dict__` passes `value == NULL`. `subtype_setdict` performs no NULL rejection — it forwards
NULL straight through at `:4057`. `Py_NewRef` is `Py_INCREF` + return; on NULL that is a
zero-page write.

**Guarded twin, two of them:** (a) the public wrapper `PyObject_GenericSetDict`
(`Objects/object.c:2124`) opens with `if (value == NULL) { PyErr_SetString(PyExc_TypeError,
"cannot delete __dict__"); return -1; }`; (b) the *sibling branch* three lines above,
`_PyObject_SetManagedDict(obj, NULL)`, handles NULL correctly.

**Why it is unreachable today** (I verified this rather than assuming it): `subtype_getset_dict` is
installed at exactly one site, `type_new_descriptors:4705-4711`, gated on `type->tp_dictoffset`.
At that point in `type_new_impl` (`:4941`), `type_new_set_attrs` runs **before** `PyType_Ready`, so
`tp_dictoffset` has not yet been inherited from the base and is non-zero only when
`ctx->add_dict` set it to `-1` — which also sets `Py_TPFLAGS_MANAGED_DICT` (`:4694-4696`). So every
object whose `__dict__` setter is `subtype_setdict` takes the managed branch. Confirmed empirically
across 12 object shapes: `del o.__dict__` succeeds (managed path) for plain instances, `__slots__ =
('__dict__',)`, `int` subclasses and dict-ful bases; raises `TypeError: cannot delete __dict__`
wherever `get_builtin_base_with_dict` delegates to a static base's own descriptor (`Exception`
subclass, `function`, `_testcapi.HeapCTypeWithDict`); no probe reached `:4032`.

**Fix:** `Py_XNewRef(value)` at `:4032`. `_PyObject_XSetRefDelayed` / `Py_XSETREF` are already
X-forms, so the whole line becomes NULL-correct and the function matches its own other branch.

*(Incidental, out of this slice: probe `del o.__dict__` on `class C(_testcapi.HeapCTypeWithDict)`
aborts at interpreter shutdown with `_PyMem_DebugRawFree: bad ID: Allocated using API ' ',
verified using API 'o'` (rc 134) on the debug build. That is `Modules/_testcapi/heaptype.c`, not
this slice; the `del` itself raised cleanly. Flagging it only so it is not lost.)*

---

## Classes checked and found CLEAN (bounded negatives)

### 1. `deletable_member` — structurally empty in this file

Both `PyMemberDef` tables are 100% `Py_READONLY`:

- `type_members` (`:1468`) — 6 entries, **6 `Py_READONLY`**, including the one `_Py_T_OBJECT`
  (`__base__` → `tp_base`).
- `super_members` (`:12545`) — 3 entries, **3 `Py_READONLY`**, all `_Py_T_OBJECT`
  (`__thisclass__`/`__self__`/`__self_class__` → the three nullable fields).

Empirically confirmed: all nine `delattr` attempts raise `AttributeError: readonly attribute`.
This is why CPY-0007 is `new_bypass`-only and has no `del` twin — unlike gh-152817
(`del cursor.row_factory`), the `super` fields are sealed.

### 2. `deletable_getset` — three NULL-accepting setters, none stores NULL into a struct field

Of the 12 `type_getsets` entries plus `object_getsets` and the two `subtype_getset_*`:

- **Reject deletion (7):** `__name__`, `__qualname__`, `__bases__`, `__module__`, `__doc__`,
  `__type_params__` all route through `check_set_special_type_attr:1483`, whose first statement is
  `if (!value) { PyErr_Format(..., "cannot delete '%s' attribute of type '%s'"); return 0; }`.
- **Reject deletion (2, explicitly):** `type_set_annotate:2131` (`value == NULL` →
  `"cannot delete __annotate__ attribute"`), `object_set_class:7803` (`"can't delete __class__
  attribute"`).
- **No setter (3):** `__mro__`, `__dict__` (on `type`), `__weakref__`.
- **Accept deletion but touch only the type dict (2):** `type_set_abstractmethods:1716` — on NULL
  it does `PyDict_Pop` + `type_clear_flags(Py_TPFLAGS_IS_ABSTRACT)`, no struct pointer written;
  `type_set_annotations:2239` — `PyDict_Pop` only. Verified post-deletion that the type still
  reprs, MROs, instantiates and reports flags correctly.
- **Accept deletion, forwards NULL (1):** `subtype_setdict:4038` — this is finding 3 above.

Note this is a *narrower* result than the scanner's own conservative posture: the briefing warns
that a setter routed through an unmodelled helper (`check_set_special_type_attr`) is *assumed* to
reject deletion. Here I read the helper and it genuinely does.

### 3. `new_bypass` on the other three types — clean, with reasons

See the Phase-0 table. Worth restating the one that is *most* instructive:
`_PyBufferWrapper_Type` has the full bypass shape (instantiable, two `PyObject *` fields set
outside any `tp_init`) and is safe purely because `bufferwrapper_releasebuf:11279` NULL-checks
both fields before any use. Same file, same author-population, opposite outcome.

### 4. Late-initialised `PyTypeObject` fields (`tp_dict`, `tp_mro`) — not this class

26 `lookup_tp_mro(` and 31 `lookup_tp_dict(` call sites. Every `tp_mro` consumer on a
Python-reachable path guards: `_PySuper_LookupDescr:12594` (`if (mro == NULL) return NULL`),
`hackcheck_unlocked:10129`, `is_subtype_with_mro:2905` (explicit `a_mro != NULL` arm),
`type_get_mro:1776`. The `tp_dict` consumers are unguarded (`_PySuper_LookupDescr:12620` is an
`assert`), but a NULL `tp_dict` requires a pre-`PyType_Ready` type, which `type.__new__` cannot
produce — it demands 3 arguments and always Readies. ACCEPTABLE, interprocedural invariant.

### 5. `super_init_impl` re-entrancy — noted, not a NULL finding

`Py_XSETREF(su->type, …); Py_XSETREF(su->obj, …); Py_XSETREF(su->obj_type, …)` (`:12950-12952`)
are three non-atomic stores, and each `Py_XSETREF`'s trailing DECREF can run Python (a metaclass
`__del__`). A re-entrant `su.__init__()` in that window can leave `type` from the inner call
paired with `obj`/`obj_type` from the outer. That is a state-consistency hole, **not** a NULL
deref — the store precedes the DECREF in every case, so no field is ever transiently NULL.
Out of this agent's class; recorded so it is not re-derived.

---

## Toolkit assessment

**Precision on this slice: 1/1.** The single reported finding is a real, reproduced SIGSEGV.

**Denominator is verified, not merely non-zero.** `total_nullable_fields: 3` matches my hand
enumeration exactly (`superobject::{type,obj,obj_type}`). The rule is exercised here.

### Recall gap 1 — no one-hop interprocedural sink (the important one)

The scanner reported **line 12806** (`Py_NewRef(su->type)`). **That line can never execute on the
crashing path** — control dies nine lines earlier at `:12797` → `supercheck:12763`, as the ASan
frame proves. The finding was correct *by luck*: the field happens to be read a second time by a
modelled sink further down the same block. Remove the `Py_NewRef` and the bug becomes invisible to
the scanner while remaining a segfault.

`supercheck` is a `static` function in the same file whose parameter `type` is dereferenced at
`:12763` (`type->tp_name`, inside `PyErr_Format`) with no NULL check anywhere in its 64 lines.
Proposed rule — same-file, one hop, no whole-program analysis needed:

> For each nullable field `F`, if `F` is passed as argument *i* to a `static` function `G` defined
> in the same translation unit, and `G`'s parameter *i* is dereferenced (`p->m`, `Py_TYPE(p)`, or
> forwarded to an existing sink) on a path with no `p == NULL` / `!p` / `p &&` guard in `G`'s body,
> emit a finding at the **call site**, citing the callee's deref line as evidence.

This would have reported `:12797 → supercheck:12763` — the true fault — instead of dead code at
`:12806`, and would generalise: `do_super_lookup` is the negative control (it *does* guard, at
`:12643`), so the rule discriminates correctly on this very file.

### Recall gap 2 — `_CALL_SINKS` checks only argument 0; the `*ObjArgs` variadics are NULL-sensitive in *every* position

`_check_function` does `target = _first_arg(args)` for the whole `_CALL_SINKS` family. That is
right for `PyObject_Vectorcall`/`PyObject_CallOneArg`/etc., where only the callable must be
non-NULL. It is **wrong for the NULL-terminated variadics** — `PyObject_CallFunctionObjArgs` and
`PyObject_CallMethodObjArgs` (both already in `_CALL_SINKS`) treat a NULL in *any* trailing
position as end-of-arguments. That is finding 2 above, at `:12793`, and the scanner cannot see it.

Proposed: split those two out of `_CALL_SINKS` into a `_VARARG_SENTINEL_SINKS` set, scan arguments
1..n-1 (excluding the deliberate terminating `NULL`), and emit a **distinct finding type** —
`vararg_null_truncation`, severity CONSIDER rather than FIX, because the failure mode is a silently
wrong call, not a crash. Same detection machinery, new sink arity model.

### Precision hazard (no miss here, but write a regression test)

The flagged field is literally named `type` — the same identifier as `super_descr_get`'s **third
parameter** (`super_descr_get(PyObject *self, PyObject *obj, PyObject *type)`), exactly the
"field names are not type-qualified" trap. Two things saved it and both should be locked in:

1. `_resolve_target_field` matched on the receiver (`su->type`), so the parameter did not shadow
   the field.
2. `_has_null_guard`'s `\b` boundary correctly did **not** treat the body's
   `if (obj_type == NULL)` (`:12798`) as a NULL guard on `type` — `obj_type` has no word boundary
   before `type`.

A regression fixture built from `super_descr_get` would pin both behaviours in one case.

---

## Reproduction artefacts

| file | purpose |
|---|---|
| `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/super_sweep.py` | 24-probe method sweep of `super` under all three bypass constructions; `python super_sweep.py <n>` runs one probe per process |
| `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/deldict.py` | 12-shape `del obj.__dict__` reachability probe for finding 3 |
| `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/del_and_state.py` | exhaustive `del` probe over every Python-visible attribute of `super` / `type` / `object` defined in this file |
