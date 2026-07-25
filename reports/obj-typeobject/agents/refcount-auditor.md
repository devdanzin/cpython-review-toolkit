# Refcount audit — `Objects/typeobject.c`, PASS 1 (construction surface)

**Slice:** `obj-typeobject`, informed-explore pass 1
**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope read:** the 12 pass-1 ranges from `preflight/include_map.md` §3 (~3,400 lines), plus the
callees they reach in regions 2, 4, 6, 7, 11, 35 (accessors, `mro_internal`, `type_set_bases`,
`recurse_down_subclasses`) and the new 3.16 `Python/slots.c` seam.
**Builds used:** `debug-gil-nojit`, `debug-gil-nojit-asan`, `release-gil-nojit`, `debug-ft-nojit`
(all at `a1d580430c8`). `git diff 4f3be1b5777 a1d580430c8 -- Objects/typeobject.c` is **empty** —
the file is byte-identical between the target ref and the matrix builds.

### Summary

| | |
|---|---|
| Functions in file | 403 (scanner count) |
| `scan_refcounts` findings, slice-scoped | **0** |
| Confirmed issues found by reading | **2 FIX** (both ASan-confirmed heap-use-after-free, one also SIGSEGV) |
| Likely issues | **2 CONSIDER** |
| Bounded negatives established | 11 (listed at the end) |

Both FIX findings are the briefing's crown-jewel shape — *borrowed-ref-across-call* — and both have
an **explicit guarded twin in the same file whose comment states the exact hazard**. The scanner
missed both for the same structural reason, given in the RECALL section.

---

## Findings

### [FIX] `type_ready_inherit` holds a borrowed `tp_mro` across `overrides_hash()` — heap-use-after-free (`Objects/typeobject.c:9332`, used at `:9336`)

**What.** `type_ready_inherit` loads the MRO tuple as a borrowed pointer and never re-reads it:

```c
9332    PyObject *mro = lookup_tp_mro(type);          /* borrowed, no INCREF */
9333    assert(mro != NULL);
9334    Py_ssize_t n = PyTuple_GET_SIZE(mro);
9335    for (Py_ssize_t i = 1; i < n; i++) {
9336        PyObject *b = PyTuple_GET_ITEM(mro, i);   /* <-- use after free */
9337        if (PyType_Check(b)) {
9338            if (inherit_slots(type, (PyTypeObject *)b) < 0) {
```

`inherit_slots` calls `overrides_hash(type)` at `:8964`, which is
`PyDict_Contains(lookup_tp_dict(type), &_Py_ID(__eq__))` at `:8814`. A class dict is allowed to
contain **non-string keys** — `type_new_impl:4960` only emits a `RuntimeWarning` for them — so that
lookup can dispatch a user `__eq__` through `compare_generic`. The user code reassigns
`X.__bases__`, which runs `mro_internal` → the old MRO tuple loses its last reference in
`type_set_bases_unlocked:1952 Py_DECREF(temp)` and is freed. Control returns into the loop, which
then reads `PyTuple_GET_ITEM(mro, i)` out of the freed tuple.

Reaching the half-built type from Python needs no C API: `type_new_set_classcell:4768` publishes
the type into a caller-supplied `__classcell__` cell **before** `PyType_Ready` runs at `:4953`, and
`types.CellType()` is pure Python.

**Guarded twin.** Two, both in this file, both with a comment naming this exact hazard:

- `find_name_in_mro:6170-6174` — *"Keep a strong reference to mro because type->tp_mro can be
  replaced during dict lookup, e.g. when comparing to non-string keys."* → `PyStackRef_FromPyObjectNew(mro)`.
- `_PySuper_LookupDescr:12597-12601` — *"Keep a strong reference to mro because
  su_obj_type->tp_mro can be replaced during PyDict_GetItemRef(dict, name, &res)."* →
  `_PyThreadState_PushCStackRefNew(tstate, &mro_ref, mro)`.

**Reproduction.** `repro/refcount_type_ready_inherit_uaf.py` — 60 lines of pure Python, no imports
beyond `types`.

- `debug-gil-nojit`: **SIGSEGV** (exit 139).
- `debug-gil-nojit-asan`: **heap-use-after-free**, `READ of size 8`
  - use: `_Py_TYPE_impl` ← `type_ready_inherit typeobject.c:9336` ← `type_ready:9571` ←
    `PyType_Ready:9629` ← `type_new_impl:4953`
  - freed: `tuple_dealloc` ← `list_dealloc` ← `type_set_bases_unlocked:1952` ←
    `type_setattro:6807` ← … ← `slot_tp_richcompare:11074` ← `do_richcompare` ←
    `compare_generic dictobject.c:1224` ← `overrides_hash:8814` ← `inherit_slots:8964` ←
    `type_ready_inherit:9338`
  - allocated: `PySequence_Tuple` ← `mro_invoke:3613` ← `mro_internal:3666` ← `type_ready_mro:9259`
- `release-gil-nojit`: silent (exit 0) — recycled block reads as intact.
- `debug-ft-nojit`: silent (exit 0) — `set_tp_mro:664` calls
  `PyUnstable_Object_EnableDeferredRefcount(mro)`, so on the free-threaded build the old MRO is not
  freed at that point. **The free-threaded arm is effectively the fix**; per the briefing's
  `#ifdef` asymmetry rule this is evidence *for* the finding, not against it.

The MRO must be longer than `PyTuple_MAXSAVESIZE` (20) or the tuple freelist recycles the block and
the read looks intact — the same masking the briefing records for CPY-0004. The reproducer builds a
33-entry MRO.

**Fix.** Take a strong reference for the duration of the loop, exactly as the two twins do:

```c
    _PyCStackRef mro_ref;
    _PyThreadState_PushCStackRefNew(tstate, &mro_ref, lookup_tp_mro(type));
    PyObject *mro = PyStackRef_AsPyObjectBorrow(mro_ref.ref);
```

(Re-reading the field per iteration is *not* sufficient here: `n` is also captured, and the
semantics of inheriting from a hierarchy that changed mid-loop are undefined.)

**Prior art:** none. `gh api search/issues` over `type_ready_inherit`, `tp_mro use-after-free`,
`inherit_slots overrides_hash`, `__bases__ reentrancy PyType_Ready` returns nothing relevant.

---

### [FIX] `recurse_down_subclasses` iterates a borrowed `tp_subclasses` across `PyDict_Contains` — heap-use-after-free (`Objects/typeobject.c:12369`, used at `:12377`)

**What.** The function documents an invariant that the *guard test three lines below* violates:

```c
12366    // It is safe to use a borrowed reference because update_subclasses() is
12367    // only used with update_slots_callback() which doesn't modify
12368    // tp_subclasses.
12369    PyObject *subclasses = lookup_tp_subclasses(type);  // borrowed ref
...
12377    while (PyDict_Next(subclasses, &i, NULL, &ref)) {   /* <-- use after free */
...
12386            int r = PyDict_Contains(dict, attr_name);   /* runs arbitrary Python */
```

The comment is about the *callback*. The hazard is `PyDict_Contains(dict, attr_name)` at `:12386`,
where `dict` is a **subclass's** `tp_dict` and may hold a non-string key with a user `__eq__`. That
code reassigns the subclass's `__bases__`; `type_set_bases_unlocked:1965 remove_all_subclasses` →
`remove_subclass:9790` sees `PyDict_Size(subclasses) == 0` and calls `clear_tp_subclasses(base)`,
freeing the very dict the outer `PyDict_Next` is walking.

**Guarded twin.** `mro_hierarchy_for_complete_type:1836-1846`, whose comment is the direct
refutation of the one at `:12366`:

> *"Obtain a copy of subclasses list to iterate over. Otherwise `type->tp_subclasses` might be
> altered in the middle of the loop, for example, through a custom `mro()`, by invoking
> `type_set_bases` on some subclass of the type which in turn calls
> `remove_subclass`/`add_subclass` on this type."*

— it calls `_PyType_GetSubclasses(type)`, which materialises a `list` of strong references first.

**Reproduction.** `repro/refcount_recurse_down_subclasses_uaf.py` — 45 lines of pure Python, no
imports at all. Trigger is `Base.__eq__ = <fn>` on a class with 8 subclasses each carrying one
non-string key.

- `debug-gil-nojit-asan`: **heap-use-after-free**, `READ of size 8`
  - use: `_PyDict_Next dictobject.c:3212` ← `recurse_down_subclasses typeobject.c:12377` ←
    `update_subclasses:12359` ← `update_slot:12124` ← `update_slot_after_setattr:6744` ←
    `type_setattro:6835`
  - freed: `dict_dealloc` ← `remove_subclass:9791` ← `remove_all_subclasses:9805` ←
    `type_set_bases_unlocked:1965` ← `type_setattro:6807` ← `slot_tp_richcompare:11074` ←
    `compare_generic dictobject.c:1224` ← `_PyDict_Contains_KnownHash:5322`
- `debug-gil-nojit`: silent (exit 0) — the dict object comes straight back off CPython's dict
  freelist, so the read hits valid recycled memory. **ASan is the only observer here**; do not read
  the clean debug run as safety.

**Fix.** Either replace the borrowed load with `_PyType_GetSubclasses(type)` (the twin's approach),
or `Py_INCREF(subclasses)` for the duration of the loop and re-validate `lookup_tp_subclasses(type)`
after each `PyDict_Contains`. The comment at `:12366-12368` must be corrected either way — it
states an invariant the code does not have.

**Prior art:** none found.

---

### [CONSIDER] `_PyType_Modified_Unlocked` iterates a borrowed `tp_subclasses` across a type-watcher callback (`Objects/typeobject.c:1195`, used at `:1201`)

**What.** Third instance of the identical shape. `subclasses = lookup_tp_subclasses(type)` at
`:1195` is walked with `PyDict_Next` at `:1201`; the loop body recurses into
`_PyType_Modified_Unlocked(subclass)` at `:1206`, whose own body at `:1212-1231` invokes registered
`PyType_WatchCallback`s and, when one fails, `PyErr_FormatUnraisable("… for %R", i, type)` — the
`%R` runs the **metaclass `__repr__`**, i.e. arbitrary Python, while the parent's iteration is live.
The code even acknowledges re-entrancy at `:1219` (*"PyErr_FormatUnraisable is potentially
re-entrant and the watcher callback might be too"*) without protecting the borrowed dict.

**Not reproduced as a fault.** `repro/refcount_type_modified_subclasses.py` demonstrates the
mechanism end-to-end on `debug-gil-nojit-asan`: the metaclass `__repr__` *does* run inside the
parent's loop and *does* drive `Base.__subclasses__()` to `[]`, i.e. `clear_tp_subclasses` frees the
dict under the live `PyDict_Next`. ASan does not fire because the freed `PyDictObject` is returned
by the dict freelist. I am reporting it as CONSIDER rather than FIX only for that reason — the
free-under-iteration is confirmed, the observable fault is not.

**Reachability caveat:** getting Python to run inside that loop requires `PyType_AddWatcher` /
`PyType_Watch` (public C API; `_testcapi.add_type_watcher(1)` in the reproducer, and used in the
wild by specializing JITs). Same guarded twin as the previous finding
(`mro_hierarchy_for_complete_type:1846`).

---

### [CONSIDER] `type_ready_add_subclasses` holds a borrowed `tp_bases` across `add_subclass` (`Objects/typeobject.c:9400`, used at `:9403`)

**What.** `bases = lookup_tp_bases(type)` is borrowed and indexed in a loop whose body is
`add_subclass(base, type)` — and `add_subclass`'s **own comment** at `:9705-9707` says the calls it
makes run arbitrary Python:

> *"Only get tp_subclasses after creating the key and value. `PyWeakref_NewRef()` can trigger a
> garbage collection which can execute arbitrary Python code and so modify `base->tp_subclasses`."*

The same GC-run `__del__` can reassign `type.__bases__`, replacing `tp_bases`.

**Why this is CONSIDER, not FIX.** The old bases tuple is not actually freed on either live path:

- `type_new`: `set_tp_bases(type, Py_NewRef(ctx->bases), 1)` at `:4432` means `ctx.bases` holds a
  *second* strong reference for the whole of `type_new_impl`, released only at `type_new:5114`. The
  refcount cannot reach zero inside `PyType_Ready`.
- `type_from_slots_or_spec`: the type is not reachable from Python during `PyType_Ready:5725`, so
  no re-entrant `__bases__` assignment is possible.

So this is a **latent** instance protected by an accident of an unrelated function's local. I would
still take the `Py_INCREF` here — `add_subclass` already documents that re-entrancy is live in this
loop, and the protection is not stated anywhere near `:9400`.

---

## Bounded negatives (this class is clean here, and why)

These are real results, not unexamined areas. Each was read line by line.

1. **`Py_CLEAR`-omission / `stale_slot_decref`: zero instances in the file.** A scripted sweep for
   `X->f = NULL;` followed within 4 lines by `Py_X?DECREF(local)` returns **no hits** across all
   13,068 lines. `type_dealloc:7019-7043` uses `Py_XDECREF` on fields it is about to free with the
   object, which is the terminal case, not the hazardous one.
2. **`type_new` error paths are balanced.** `type_new_init:4933` (`Py_CLEAR(ctx->slots)` +
   `Py_XDECREF(dict)`), `type_new_impl:4987` (`Py_DECREF(type)`, and `type_dealloc` `Py_XDECREF`s
   every heap-type member so a partially built type unwinds safely), `type_new:5114`
   (`Py_DECREF(ctx.bases)` on both the `res == 0` and error returns; `ctx.bases` is a *new*
   reference on both branches of `type_new_get_bases` — `PyTuple_Pack` at `:5000`,
   `Py_NewRef` at `:5059`). No leaked or double-freed `ht_name` / `ht_qualname` / `ht_slots` /
   `tp_bases` / `tp_mro` on any early exit.
3. **`type_from_slots_or_spec`'s `finally:` invariant holds.** The comment at `:5252-5255` promises
   each of `res` / `bases` / `tp_doc` / `ht_name` / `_ht_tpname` is owned-or-NULL at the label.
   Every transfer point NULLs its local immediately (`:5646`, `:5649`, `:5653`, `:5657`), and every
   `goto finally` has an exception set. Clean.
4. **`Py_tp_doc` specified twice does not leak the first `PyMem_Malloc`** (`:5423`). It looks like it
   should — the `NULL` arm at `:5418` frees, the non-NULL arm overwrites — but
   `_PySlot_get_duplicate_handling(Py_tp_doc)` falls through to `_PySlot_PROBLEM_REJECT`
   (`pycore_slots_generated.h:748`), and `handle_first_run` (`Python/slots.c:305`) rejects on the
   *second* occurrence **before** the caller's `switch` sees it, so `finally: PyMem_Free(tp_doc)`
   frees the first buffer. Same reasoning clears the `Py_tp_members` duplicate, which would
   otherwise over-read `nmembers` entries out of the first array at `:5682`.
5. **`mro_internal:3665-3698` is correct despite looking like a double-DECREF.** The
   `Py_XDECREF(old_mro)` at `:3668` balances the `Py_XNewRef` at `:3665` (taken solely so the
   pointer-identity re-entrancy test at `:3667` cannot be fooled by address reuse — the comment at
   `:3662-3664` says so). The second disposal at `:3696`/`:3698` disposes of a *different*
   reference: the one `type->tp_mro` held and that `set_tp_mro:3678` orphaned by overwriting the
   field without decref'ing. Net-balanced.
6. **`type_set_bases_unlocked:1934-1979` ownership transfer is correct.** `old_bases` / `old_base`
   are borrowed at `:1934`/`:1936` and become owning at `:1940`/`:1941` when `set_tp_bases` and the
   raw `tp_base` store overwrite the fields without decref'ing. Their use at `:1965` (after the
   Python-reaching `mro_hierarchy_for_complete_type`) is therefore an owned use, not a stale borrow.
   Both the success (`:1978-1979`) and `bail:` (`:2006`/`:2015`) paths dispose of exactly one
   reference each.
7. **`type_new_set_names:12279` is a guarded twin, not a finding.** It `PyDict_Copy`s the type dict
   before iterating, so `key`/`value` stay valid across `_PyObject_LookupSpecial` and the
   `__set_name__` call, including the `%R`/`tp_name` use in `_PyErr_FormatNote:12300` on the error
   path. `type_new_init_subclass:12322` holds no borrowed state at all.
8. **The other three borrowed-`tp_mro` loops are clean because their bodies are pure C.**
   `hackcheck_unlocked:10128` (compares `tp_setattro` pointers; the only call is `PyErr_Format` with
   `%s` after the loop), `releasebuffer_maybe_call_super_unlocked:11368` (reads
   `tp_as_buffer->bf_releasebuffer`, and `releasebuffer_maybe_call_super:11411` deliberately makes
   the actual call *after* `END_TYPE_LOCK`), `PyType_GetModuleByToken_DuringGC:5936` (reads
   `ht_module` / `_PyModule_GetToken`).
9. **`type_add_common_descriptor:4632` is a borrowed-under-a-known-live-owner FP, correctly.** The
   descriptor is borrowed from `interp->cached_objects.dict_descriptor`, which is only released by
   `_PyTypes_FiniCachedDescriptors` at interpreter finalization. Refcounting is balanced: the cache
   keeps the `PyDescr_NewGetSet` reference, `PyDict_SetDefaultRef` takes its own.
10. **`type_setattro:6802-6843` defers `Py_XDECREF(old_value)` past `update_slot_after_setattr` and
    `END_TYPE_DICT_LOCK` on purpose** (`type_update_dict:6697-6702` explains why). That is the
    correct shape, not a leak.
11. **`update_one_slot:11939-12039` is clean.** `descr` is borrowed out of `descr_ref`, but the only
    values that outlive `PyStackRef_CLOSE(descr_ref)` are C function pointers (`d->d_wrapped`,
    `d_method->ml_meth`, `p->function`), never the object. The `continue` on `res <= 0` skips the
    `CLOSE` correctly because `descr_ref` was never filled.

---

## RECALL — what the scanner missed, and the rule that would catch it

`scan_refcounts` reported **0 findings over 403 functions** in the file with the most refcount-dense
code in CPython. That is a recall gap, and it is a *systematic* one: all three instances of the same
shape share one structure the current rules cannot express.

### Why the rebuilt `borrowed_ref_across_call` rule cannot see these

The v0.9 rules are release-hazard rules. `borrowed_ref_across_call` fires when a borrowed pointer is
*released* (`Py_DECREF`'d) after a Python-reaching call; `stale_slot_use` / `slot_transfer_across_call`
fire on a slot cleared to `NULL` and a stale local dropped. **In all three findings here nothing is
ever released through the borrowed local.** The local is only *read*:

```c
mro = lookup_tp_mro(type);           /* borrowed load from a struct field */
...  <call that can run arbitrary Python> ...
b = PyTuple_GET_ITEM(mro, i);        /* dereferencing READ, not a DECREF */
```

There is no drop to anchor on, so no rule fires. The `owner_freed_before_use` rule is the closest
shape but it requires a syntactically visible `Py_DECREF(V)` *in the same function*; here the free
happens several frames away, inside re-entrant Python.

### Proposed rule: `borrowed_field_deref_across_call`

Fire when **all** of the following hold inside one function:

1. A local is assigned from a **field-load accessor**, not a constructor. Seed the accessor set
   syntactically per-file: any `static inline PyObject *NAME(...)` whose body is a bare
   `return X->field;` (this picks up `lookup_tp_mro`, `lookup_tp_bases`, `lookup_tp_subclasses`,
   `lookup_tp_dict` automatically), plus direct `x = y->field` loads of a `PyObject *`-typed member,
   plus the documented borrowed-return APIs (`_PyType_Lookup`, `PyDict_GetItem*` without `Ref`,
   `PyList_GET_ITEM`, `PyTuple_GET_ITEM`).
2. No `Py_INCREF` / `Py_NewRef` / `PyStackRef_FromPyObjectNew` / `_PyThreadState_PushCStackRefNew`
   on that local between the load and the use.
3. A `PYTHON_REACHING_APIS` call occurs between them — **with `PyDict_Contains`, `PyDict_SetItem`,
   `PyDict_GetItem*`, `PyDict_DelItem`, `PyDict_SetDefaultRef` and `PyDict_Next` added to that set.**
   This is the single highest-leverage change. A dict operation whose key is a `str` is only
   C-level if the *dict* is unicode-only; a type's `tp_dict` is attacker-controllable and may hold
   non-string keys (`type_new_impl:4960` warns but permits), so every `PyDict_*` call on a type dict
   reaches `compare_generic` → `PyObject_RichCompareBool` → a user `__eq__`. Both confirmed findings
   go through exactly that edge and *only* that edge. The existing FP taxonomy entry
   "statically-known type slot" already draws the right distinction — it just has not been applied
   to dict lookups.
4. The **use is a dereference**: `PyTuple_GET_ITEM(V, …)`, `PyList_GET_ITEM`, `Py_TYPE(V)`,
   `Py_SIZE(V)`, `PyDict_Next(V, …)`, `V->field`. A plain pointer *comparison* (`if (V == W)`,
   `lookup_tp_bases(type) == new_bases`) must **not** count — that is the whole basis of the
   correct re-entrancy tests at `:1957`, `:1993`, `:3667`, and flagging them would drown the rule.

Confidence lift, cheaply: emit **high** when the same file contains a *guarded twin* — another
function that loads from the same accessor and does INCREF/`PushCStackRefNew` it. In this file that
test alone separates the three findings from the eight clean sites, because `find_name_in_mro:6170`
and `_PySuper_LookupDescr:12597` are right there. A "twin exists, this site lacks it" signal is
mechanically computable and is the strongest evidence class the briefing names.

Estimated yield on this file: 3 candidates (`:9332`, `:12369`, `:1195`) plus `:9400`; the eight
pure-C loops are excluded by criterion 3, and the identity tests by criterion 4.

### Second, smaller gap: comments asserting invariants

Both FIX sites carry a comment claiming the borrow is safe (`:12366-12368` explicitly; `:9333` an
`assert`). A cheap lint — *a borrowed-field load whose nearby comment contains "safe"/"borrowed
ref"/"no need to hold a strong reference" and that is followed by a Python-reaching call* — would
have surfaced `recurse_down_subclasses` on its own. Three of the four sites in this file that make
such a claim are wrong (`:12366` is wrong, `:788` in `_PyType_GetSubclasses` is right, `:9705` in
`add_subclass` is right and is the fix).

### Note on denominators

`scan_recursion_guards` (1), `scan_error_paths` (5), `scan_pyerr_clear` (1), `scan_init_bypass` (1),
`scan_ft_races` (2) all produced non-zero slice-scoped counts, so the harness is wired correctly and
`scan_refcounts`'s 0 is a genuine rule-coverage zero rather than a plumbing zero.
