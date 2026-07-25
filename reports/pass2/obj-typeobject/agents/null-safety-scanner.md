# NULL Safety Analysis — `Objects/typeobject.c` PASS 2 (informed-explore, slice `obj-typeobject`)

**Target:** `/home/danzin/projects/cpython/Objects/typeobject.c` @ `4f3be1b5777` (3.16.0a0), 13,068 lines.
**Builds:** `~/projects/python_build_matrix/builds/*` @ `a1d580430c8`.
**File-identity check:** `git diff a1d580430c8 4f3be1b5777 -- Objects/typeobject.c` → **empty**. Every line number below is valid on both the target ref and the matrix builds.
**Scope:** the eight pass-2 regions (R3, R4, R6, R11, R18–R21, R25, R26, R37) plus the issue-#28 re-run over the whole file. Pass 1's report is not repeated.

---

## 1. THE DENOMINATOR — is the zero earned?

**Verdict up front: the zero is EARNED for the shape the rule models, and STRUCTURALLY SILENT for three shapes it does not.** Both halves are measured below.

### 1a. The widening fired — and it more than doubled the rule's reach

Pass 1 measured 49 of 760 assignment-from-call sites resolved (6.4%). Issue #28's rule 8
(`nullable_source_calls`: same-file `static` pointer producers + 4-round forwarder chasing)
is now shipped. Re-running the scanner's own machinery with per-stage instrumentation
(`scratchpad/p2null/stages.py`, importing `scan_null_checks` directly):

| measure | pass 1 | pass 2 | delta |
|---|---|---|---|
| assignment-from-call sites in file | 760 | 751¹ | — |
| **sites the rule's alphabet matches** | **49** | **115** | **+135 %** |
| distinct callees matched | 16 | **53** | +37 |
| `fallible_sources_resolved` | 45 (enum only) | **164** (45 enum + 119 local) | +119 |

¹ my count applies a small noise filter (`sizeof`, `Py_TYPE`, the `*_CAST` macros, `strlen`…);
the underlying population is the same.

So **rule 8 did fire here, hard.** The widening pulled in 37 new callees — `type_from_ref`(5),
`_PyType_LookupRef`(4), `type_qualname`(3), `find_best_base`(3), `import_copyreg`(3),
`call_attribute`(3), `_PyHeapType_GET_MEMBERS`(6, resolved through a **two-hop** forwarder chase
`_PyHeapType_GET_MEMBERS → PyObject_GetItemData → getitemdata`), `supercheck`(2),
`mro_implementation_unlocked`(2), `_PyType_GetSubclasses`(2), … That two-hop resolution is the
rule working exactly as designed.

Note the envelope's `assignment_sites=1432` is **not** the rule's candidate population — it is
every `lvalue =` in scope, scalars included. The number that matters is **115**.

### 1b. Pipeline stages on the 115 matched sites

| stage | count |
|---|---|
| 0 — matched the widened alphabet | **115** |
| 1 — dropped: assignment is a control condition | 1 |
| 2 — dropped: no deref of the result in the 12-line window | 55 |
| 3 — reached "has a deref in window" | **59** |
| 4 — dropped: deref not dominated by the assignment | **36** |
| 5 — rescued by `_join_after_full_ifelse` (issue #28) | **0** |
| 6 — dropped: NULL check precedes the deref | **23** |
| **7 — findings** | **0** |

Two issue-#28 rules and their behaviour here:
- **`_ASSERT_CALL_DEREF`** (the rule built from CPY-0079) **matched 4 windows** on this file. It
  fires; nothing survived the later stages. Not silent, just empty.
- **`_join_after_full_ifelse`** (join-point dominance) **rescued 0**. It never fired on this file —
  a structural zero for that sub-rule specifically.

### 1c. Precision test — did the check-recognition logic actually work?

The 23 stage-6 drops are the direct evidence. I read **all 23** by eye
(`scratchpad/p2null/drops.py`): every one has a genuine NULL test before the deref
(`type_from_ref` at :1202, `PyList_New` at :1945, `PyTuple_New` at :3461, `PyMem_Malloc` at :3838,
`_PyType_GetSubclasses` at :6493, `supercheck` at :12771, …). **0 false suppressions.**

The 36 stage-4 drops split 32 checked / 4 unchecked. I read all 4 (§3.1) — **all 4 are true
negatives.** So on this file the dominance filter cost **zero** recall, even though it is the
single largest suppressor (61 % of deref-in-window candidates).

### 1d. Did the widening leave anything on the table? — the alphabet experiment

I re-ran the **identical** pipeline (same window, same check regexes, same dominance test) with
the alphabet widened further (`scratchpad/p2null/widened.py`):

| alphabet | resolved names | matched sites | surviving candidates |
|---|---|---|---|
| **W0 — shipped** | 164 | 115 | **0** |
| W1 = W0 + the 4 field-forwarding accessors | 168 | 168 | 14 |
| W2 = W0 + ~70 common fallible APIs absent from the enum | 235 | 181 | 4 |
| W3 = W1 ∪ W2 | 239 | 234 | 18 |

**All 18 W3 candidates are already-triaged.** 13 are the `lookup_tp_dict`/`PyType_GetDict`-on-a-
ready-type class pass 1 dismissed as ACCEPTABLE (`:1611 :1649 :1707 :2057 :2098 :2148 :2177 :2249
:2333 :7882 :9149 :9379 :10502 :12278`), 3 are the `lookup_tp_bases` + `Py_TPFLAGS_READY`-dominated
class (`:1427 :3440 :9400`), and 1 is `_PyType_GetBases:591`, **already recorded as CPY-0089**.

**Zero net-new candidates from widening the alphabet further.** That is the strongest single piece
of evidence that the zero is earned rather than lucky: the detector's blind spot has been probed
from outside and the space behind it is empty *of this rule's shape*.

### 1e. `outparam_wrappers = 0` — is it structural?

**Yes, and in a stronger sense than "the file has no out-parameters."**

`discover_outparam_wrappers()` is seeded exclusively by forwards to four names
(`_PyTuple_Resize`, `_PyBytes_Resize`, `PyUnicode_Resize`, `_PyUnicode_Resize`).
`grep` for all four in `Objects/typeobject.c` → **0 call sites**. No seed, so no wrapper can be
discovered, so the rule's denominator is 0 and its zero carries no information.

But the file *does* have five genuine `PyObject **` out-parameter functions the rule cannot reach:

| function | out-param |
|---|---|
| `mro_internal` :3655 | `PyObject **p_old_mro` |
| `type_new_get_bases` :4994 | `PyObject **type` |
| `type_update_dict` :6695 | `PyObject **old_value` |
| `_PyObject_GetNewArguments` :8090 | `PyObject **args`, `PyObject **kwargs` |
| `_PyObject_GetItemsIter` :8182 | `PyObject **listitems`, `PyObject **dictitems` |

I hand-checked all five for the `Py_DECREF`-of-a-callee-NULLed-pointer shape. **All five are
disposed correctly** — `old_value` is initialised `NULL` at :6802 and released with the NULL-safe
`Py_XDECREF` at :6843; `args`/`kwargs` use `Py_XDECREF` at :8241-8242; `listitems` uses
`Py_CLEAR(*listitems)` at :8205/:8211; `p_old_mro` is analysed in §3.1. So the answer is still 0 —
but it comes from **reading**, not from the rule.

### VERDICT

- **`decref_of_nulled_outparam`: structural silence.** Denominator 0 (no seed call sites). The 5 real
  out-param contracts in the file were cleared by hand, not by the scanner.
- **`unchecked_alloc` / `deref_before_check`: the zero is EARNED**, at ~15 % alphabet coverage but
  with a *measured-empty* margin: widening the alphabet by 46 % more names (W3) produces no
  candidate that is not already triaged, the check logic has 0 false suppressions on 23 drops, and
  the dominance filter has 0 false suppressions on its 4 unchecked drops.
- **The residual risk is not in this rule's alphabet — it is in shapes the rule cannot express.**
  Three of them are named in §5, and one of them is where this pass's only new finding came from.

---

## 2. STRATIFIED HAND-CHECK — the full pass-2 population

The brief asked for ≥ 25 sites. I enumerated and hand-checked the **entire population** rather than
a sample, so the hit rate has a real denominator (`scratchpad/p2null/sample.py`, 1,867 lines of
source-with-context reviewed):

| region | assignment-from-call sites | hand-check hits |
|---|---|---|
| R3 managed static types (228–522) | 8 | 0 |
| R4 accessors (524–810) | 13 | 0 |
| R6 watchers / versions (971–1481) | 10 | 0 |
| R11 MRO C3 (3217–3702) | 33 | 0 |
| R18 `PyType_Get*` (5834–6139) | 13 | 0 |
| R19 lookup cache (6140–6452) | 24 | 0 |
| R20 set-flags (6453–6528) | 3 | 0 |
| R21 getattro/setattro (6529–6848) | 16 | 0 |
| R25 `__class__` assignment (7482–7846) | 19 | 0 |
| R26 pickle / `__reduce__` (7848–8406) | 40 | 0 |
| R37 super (12534–13068) | 22 | 0 |
| **TOTAL** | **201** | **0** |

**Hand-check hit rate for the rule's own shape: 0 / 201.** The zero is real. §3 records what the
reading *did* turn up — none of it is this shape.

### Why this file is clean of the classic shape

1. **Almost every fallible call in the pass-2 regions is checked on the very next line.** The regions
   are old, heavily fixed code: R11 alone has 76 commits, R25 has 92, R37 has 95.
2. **The dominant idiom is a borrowed field accessor, not an allocation.** `lookup_tp_dict`(25),
   `lookup_tp_mro`(13), `lookup_tp_subclasses`(8), `lookup_tp_bases`(7) account for 53 of the sites.
   `tp_dict` is only ever reassigned by `PyType_Ready` (:9131) and `type_dealloc`, never on a live
   ready type, and `_PyType_CheckConsistency:882` asserts `lookup_tp_dict(type) != NULL` as a
   file-wide invariant — so the preflight's prediction that these are ACCEPTABLE is correct and
   holds for every one of the 25.
3. **The NULL-capable fields (`tp_mro`, `tp_bases`) are guarded where they can be NULL.**
   `find_name_in_mro:6155` handles `mro == NULL` by calling `PyType_Ready` and re-testing;
   `mro_implementation_unlocked:3444` errors with "Cannot extend an incomplete type";
   `_PySuper_LookupDescr:12594` returns NULL; `PyType_GetBaseByToken_DuringGC:6038` takes the
   `tp_bases` fallback path. The one place that does *not* is `PyType_GetModuleByToken_DuringGC:5938`
   — already recorded as CPY-0089.

---

## 3. FINDINGS

### 3.1 Confirmed, not re-litigated

| id | site | status |
|---|---|---|
| **CPY-0007** | `super_descr_get:12797` → `supercheck:12763` reads `type->tp_name` off the `super.__new__(super)`-uninitialised `su->type` | present verbatim |
| **CPY-0070** | `type_set_bases_unlocked:1966` `res = add_all_subclasses(...)` never branched on | present verbatim |
| **CPY-0089** | `_PyType_GetBases:592` `Py_INCREF(res)` unchecked; `PyType_GetModuleByToken_DuringGC:5938` assert-only `tp_mro`; `init_tp_subclasses:709` / `clear_tp_subclasses:725` unasserted `_PyStaticType_GetState` | all four present verbatim |

Sibling hunt on CPY-0089's assert-only shape across the pass-2 regions found **one new instance**
(§3.2 is a different shape; the assert-only `tp_dict` derefs at `find_name_in_mro:6179` and
`_PySuper_LookupDescr:12620` are ACCEPTABLE — see §4).

### 3.2 [CONSIDER] `managed_static_type_state_get` bounds test is off by one — a one-element OOB read of `for_extensions.initialized[]` — `Objects/typeobject.c:294`

```c
284  static managed_static_type_state *
285  managed_static_type_state_get(PyInterpreterState *interp, PyTypeObject *self)
286  {
287      // It's probably a builtin type.
288      size_t index = managed_static_type_index_get(self);
289      managed_static_type_state *state =
290              &(interp->types.builtins.initialized[index]);
291      if (state->type == self) {
292          return state;
293      }
294      if (index > _Py_MAX_MANAGED_STATIC_EXT_TYPES) {   /* <-- must be >= */
295          return state;
296      }
297      return &(interp->types.for_extensions.initialized[index]);
298  }
```

**What.** `_Py_MAX_MANAGED_STATIC_EXT_TYPES` is `10`
(`Include/internal/pycore_interp_structs.h:544`) and `for_extensions.initialized` is declared
`managed_static_type_state initialized[_Py_MAX_MANAGED_STATIC_EXT_TYPES]` (`:650`) — valid indices
`0..9`. At `index == 10` the test `10 > 10` is **false**, so line 297 returns
`&for_extensions.initialized[10]`: one element past the end.

**Impact.** The struct immediately following `for_extensions` in `struct _types_state` is
`PyMutex mutex;` then `PyTypeObject *type_version_cache[TYPE_VERSION_CACHE_SIZE]`
(`pycore_interp_structs.h:652-658`). So the returned `state`'s `tp_dict` member (`:587`) lands
inside `type_version_cache[]` — an array of **borrowed `PyTypeObject *`**. `lookup_tp_dict:533`
then returns a `PyTypeObject *` to callers that immediately do `PyDict_GetItemRef(dict, …)` /
`PyDict_Contains(dict, …)`. That is type confusion, not merely a NULL read.

**Guarded twin.** Every other bound test against these two constants in the same region is a
strict `<` on the correct side: `:320` `assert(index < _Py_MAX_MANAGED_STATIC_BUILTIN_TYPES)`,
`:327` and `:338` `assert(index < _Py_MAX_MANAGED_STATIC_EXT_TYPES)`, `:335`, and
`_PyTypes_Fini:1043` `for (size_t i = 0; i < _Py_MAX_MANAGED_STATIC_EXT_TYPES; i++)`. Line 294 is
the only `>` in the family, and it is the only one that indexes rather than asserts.

**Why CONSIDER, not FIX.** Reaching line 297 with `index == 10` requires a
`_Py_TPFLAGS_STATIC_BUILTIN` type whose builtin index is exactly 10 **and** for which
`interp->types.builtins.initialized[10].type != self`. Builtins are registered in a fixed order, so
slot 10 normally holds that very type and the function returns at :292. The two windows where the
identity test fails are (a) before that slot is initialised in this interpreter and (b) after
`managed_static_type_state_clear:382` NULLs `state->type` during finalisation. I could not build a
Python-level route to either. No prior art:
`gh api -X GET search/issues -f q='repo:python/cpython managed_static_type_state_get'` → 2 hits,
neither this (gh-120161 is the adjacent `managed_static_type_index_is_set` assertion).

**Fix.** One character: `if (index >= _Py_MAX_MANAGED_STATIC_EXT_TYPES)`.

**Routing.** Out-of-bounds indexing is `memory-pattern-analyzer`'s class, not mine. Recorded here
because this pass's reading found it; handing it over rather than double-reporting.

### 3.3 [ACCEPTABLE — record the invariant] `Py_DECREF(old_value)` on `update_cache()`'s return — `Objects/typeobject.c:6256`, `:6383`

`update_cache:6216` returns `old_name` — the **previous `entry->name`**, despite both call sites
naming the result `old_value`. Both callers `Py_DECREF` it with no guard:

```c
6251      PyObject *old_value = update_cache(entry, name, version_tag, value);
...
6256      Py_DECREF(old_value);
```

This is safe, and the invariant is stated in-tree twice. `_PyType_InitCache:1009` sets
`entry->name = Py_None` with the comment *"Set to None so `_PyType_LookupRef()` can use
`Py_SETREF()`, rather than using slower `Py_XSETREF()`"*; `_PyType_ClearCache:1021` restores
`Py_None`; `_PyTypes_AfterFork:6271` does `Py_SETREF(entry->name, Py_None)`. The **sole** caller
that NULLs it is `_PyTypes_Fini:1039` (`type_cache_clear(cache, NULL)`), which runs at
`Python/pylifecycle.c:2086` — after `_PyTypes_FiniTypes`, with only `_PyCode_Fini` /
`_PyUnicode_ClearInterned` / `_PyUnicode_Fini` following, none of which perform a type-attribute
lookup. So `Py_DECREF(NULL)` is unreachable.

Recorded because (a) `update_cache` is **invisible to the scanner** — it is a field-forwarding
return with no literal `return NULL`, exactly recall gap RG-1 — and (b) the `old_value` naming
invites a future edit that assumes it is `entry->value`, which *is* NULLable (`:990`, `:6270`).
`type_cache_clear`'s own `Py_XSETREF(entry->name, _Py_XNewRef(value))` at `:989` is the guarded
twin: it is written NULL-tolerantly precisely because Fini passes NULL.

### 3.4 [ACCEPTABLE — C-API contract] `cls->tp_base` dereferenced unguarded — `Objects/typeobject.c:6095`, `:6107`

`PyObject_GetTypeData_DuringGC:6095` and `PyType_GetTypeDataSize:6107` both do
`cls->tp_base->tp_basicsize` with no NULL test. `tp_base` is NULL for `object`. Both are public
C-API whose documented contract requires a type created by `PyType_FromMetaclass` with
`Py_TPFLAGS_ITEMS_AT_END`, which always has a base. No Python-level route; a C caller passing
`&PyBaseObject_Type` is out of contract. Noted so it is not re-flagged.

---

## 4. VERIFIED NEGATIVES — read, cleared, recorded so nobody re-flags them

These are the shapes that *looked* like findings during the read. Each one cost real time; the
point of recording them is that the next pass does not pay it again. **Two of them correct claims
made in this run's own preflight.**

- **`mro_internal:3665/3668/3696/3698` is NOT a double-DECREF.** The sequence
  `old_mro = Py_XNewRef(lookup_tp_mro(type)); … Py_XDECREF(old_mro); … *p_old_mro = old_mro;`
  reads like a use-after-drop. It is not: **`set_tp_mro:671` plain-stores `self->tp_mro = mro`
  with no DECREF of the old value**, so at :3678 the type's own reference to `old_mro` is orphaned,
  and :3696/:3698 disposes exactly that orphan. The `Py_XNewRef`/`Py_XDECREF` pair at :3665/:3668
  is purely the ABA defence the comment at :3662-3664 describes ("Don't let old_mro be GC'ed and
  its address be reused"). Between :3668 and :3678, `old_mro` is kept alive by `tp_mro` itself,
  which the `reent` test at :3667 proves still points at it.

- **`object_getstate_default`'s `__slotnames__` guard is correctly placed — the preflight's
  `include_map.md` states the opposite.** `include_map.md` §R26 claims *"`PyList_GET_ITEM(slotnames, i)`
  at 7990 is unchecked after `PyObject_GetOptionalAttr` (7991) ran user code on the previous
  iteration."* It is checked: the size test at **:8010-8014** sits at the **end** of the loop body,
  so it runs after iteration *i*'s user code and before iteration *i+1*'s `PyList_GET_ITEM`. A
  shrink raises `RuntimeError` and never indexes out of bounds. The residual hole is a *same-size*
  mutation, which yields wrong pickle contents — a correctness wart, not a memory bug.

- **`traverse_slots:2591` / `clear_slots:2666` — `_PyHeapType_GET_MEMBERS` is genuinely fallible,
  and still safe.** It is not pointer arithmetic: `:2579` forwards to `PyObject_GetItemData` →
  `getitemdata(obj, true)` (`:6115`), which returns NULL **and raises TypeError** when
  `Py_TYPE(obj)` lacks `Py_TPFLAGS_ITEMS_AT_END`. The scanner resolved this correctly through two
  forwarder hops. The deref is safe because the flag is on `PyType_Type` itself (`:7313`), is
  unconditionally inherited by every subclass at `:8802-8803`, and `PyType_FromMetaclass` refuses a
  variable-size base without it (`:5584-5588`). So no metaclass can lack it.

- **`slot_nb_power:10777` / `:10788` — a NEW false-positive class.** `Py_DECREF(r)` after an
  unchecked `r = vectorcall_maybe(...)` is guarded by `if (r != Py_NotImplemented) return r;`.
  Because `NULL != Py_NotImplemented`, that comparison *is* a NULL check with an early return —
  but it names a singleton, not `NULL`, so `_NULL_CHECK_TEMPLATE` cannot see it. See §5 RG-4.

- **`_PyType_Modified_Unlocked:1222` — the `cb &&` is load-bearing, not defensive noise.**
  `PyType_ClearWatcher:1091` sets `interp->type_watchers[id] = NULL` but **never clears the
  `tp_watched` bit** on types that were watching it. A stale bit is therefore normal, and
  `if (cb && (cb(type) < 0))` is the only thing between it and a NULL call through a function
  pointer. Do not "simplify" it.

- **`PyType_GetBaseByToken:6085` `Py_INCREF(*result)` is safe.** `res > 0` is returned only from
  the `found:` label (`:6030-6034`), which assigns `*result` whenever `result != NULL`; and
  `PyType_GetBaseByToken_DuringGC` pre-NULLs `*result` at `:6015`.

- **`_Py_type_getattro_stackref:6658` `PyStackRef_AsPyObjectBorrow(meta_attribute_ref.ref)` is
  safe.** `meta_get != NULL` at :6657 implies the ref was non-NULL at :6602, and the only
  `PyStackRef_CLEAR` of it (:6627) is inside a block whose every exit is a `goto done`
  (:6636, :6646, :6652).

- **`do_super_lookup:12643` short-circuits `su_obj_type == NULL`**, which is what makes
  `super_getattro:12699` safe on a `super.__new__(super)` object — pass 1's conclusion, re-confirmed.

- **`find_name_in_mro:6179` / `_PySuper_LookupDescr:12620` assert-only `tp_dict` on MRO entries.**
  Both do `assert(dict && PyDict_Check(dict))` then dereference. A custom metaclass `mro()` chooses
  the entries and `mro_check:3547` validates only `PyType_Check` + layout, never readiness — so the
  shape is the CPY-0089 family. It is ACCEPTABLE because there is no Python-level way to obtain a
  type object with `tp_dict == NULL`: `type_new` sets it before returning, `PyType_Ready` sets it
  at :9131, `type_clear:7267-7270` `PyDict_Clear`s without NULLing, and `_PyType_CheckConsistency:882`
  asserts the invariant file-wide. Flagging it would require a C extension exposing an un-readied
  static type. Stated here so the sibling hunt is on record as *done*, not skipped.

- **`type_setattro` / `type_update_dict` out-parameter.** `old_value = NULL` at :6802,
  `_PyDict_GetItemRef_Unicode_LockHeld` writes it at :6703, released with the NULL-safe
  `Py_XDECREF` at :6843. Correct on every path including the `goto done` at :6825.

---

## 5. DYNAMIC PROBES

23 adversarial payloads across the pass-2 attack surface (custom `mro()` incl. the
finalizer-replaces-MRO path the `mro_invoke` comment at :3579-3588 names, `__class__` assignment
incl. the `same_slots_added` evil-`__eq__` path, `super` re-init during a descriptor `__get__`,
`super.__new__(super)`, type watchers, the `setattr` dunder storm through the lookup cache,
`__slotnames__` shrink-during-iteration, lying `__class__` through `__reduce_ex__`, `abc.register`
flag churn, subinterpreter static-type init/fini), each run on **debug-gil-nojit**,
**release-gil-nojit** and **debug-ft-nojit**. Script: `scratchpad/p2null/probes.py`.

**21 of 23 payloads: rc=0 on all three builds.** Two abnormal results.

### 5.1 [FIX — cross-agent referral to `stw-safety-checker`] `o.__class__ = B` runs a user `__eq__` **inside the stopped world** — `Objects/typeobject.c:7609`

Not a NULL dereference, so not mine to own — but it is this pass's only reproduced crash, it came
out of my probe sweep, and the tracker has no record of it. Filed here, routed there.

**Payload** (`scratchpad/p2null/slots_stw.py`, 12 lines, pure Python, no C extension, no threads):

```python
class Evil(str):
    def __eq__(self, other): return True
    def __hash__(self): return hash(str(self))

class A: __slots__ = (Evil("x"),)
class B: __slots__ = (Evil("y"),)

a = A()
a.__class__ = B          # <-- SIGABRT on the free-threaded debug build
```

**Differential.**

| build | outcome |
|---|---|
| debug-gil-nojit | rc=0, assignment succeeds (`types_stop_world` is a no-op in the GIL build) |
| release-gil-nojit | rc=0, assignment succeeds |
| **debug-ft-nojit** | **SIGABRT** — `python: Python/ceval.h:148: check_invalid_reentrancy: Assertion '!interp->stoptheworld.world_stopped' failed.` |
| release-ft-nojit | rc=0 — the assert is compiled out, so the Python code **really does run with the world stopped**, silently |
| release-ft-nojit-o0 | rc=0, same |

**Chain** (gdb on debug-ft-nojit, frames #33→#9):

```
o.__class__ = B
  PyObject_SetAttr                        Objects/object.c:1533
  _PyObject_GenericSetAttrWithDict        Objects/object.c:2049
  object_set_class                   Objects/typeobject.c:7826   <- types_stop_world() at :7823
  object_set_class_world_stopped      Objects/typeobject.c:7763
  compatible_for_assignment           Objects/typeobject.c:7683
  same_slots_added                    Objects/typeobject.c:7609   <- PyObject_RichCompareBool(slots_a, slots_b, Py_EQ)
    tuple_richcompare                Objects/tupleobject.c:755
    slot_tp_richcompare              Objects/typeobject.c:11074   <- dispatches the user __eq__
      _PyEval_EvalFrameDefault           Python/ceval.c:1232
        check_invalid_reentrancy           Python/ceval.h:148     <- assertion fails
```

**Why the user's `__eq__` is reachable at all.** `__slots__` entries are passed through `_Py_Mangle`,
which returns *the same object* (`Py_NewRef(name)`) for any name not beginning with `__`. So a
`str` **subclass** instance survives verbatim into `ht_slots`, and `same_slots_added`'s
`PyObject_RichCompareBool` on the two `ht_slots` tuples dispatches its `__eq__`. Verified directly:
`[type(s).__name__ for s in A.__slots__]` → `['Evil']`.

**Why the world is stopped.** `object_set_class:7821-7824` skips the stop only when
`_PyObject_IsUniquelyReferenced(self)` (the gh-145566 fast path added 2026-03-06). Here `a` is held
by both the module global and the evaluation stack, so `unique` is false and `types_stop_world()`
runs.

**Guarded twin.** `_PyType_SetFlagsRecursive:6506-6527` is the in-file model for doing the
Python-invoking work *before* stopping the world: it calls `_PyType_Modified_Unlocked` at :6517 and
only then `types_stop_world()` at :6522, with the rationale spelled out at :6509-6513. The fix
shape for `object_set_class` is the same — run `compatible_for_assignment` (which is a pure
predicate over the two types, needing no STW) **before** :7823, and keep only the `Py_SET_TYPE`
mutation inside the stopped window.

**Second-order observation.** On every build that does not abort, the assignment **succeeds** even
though `A.__slots__ != B.__slots__` — `same_slots_added` delegates the compatibility decision to a
user-controlled `__eq__`. Layout is still safe here (both types add exactly one slot of the same
size, and `compatible_for_assignment`'s `tp_basicsize`/`tp_itemsize` checks are independent), so
this is data confusion (`a`'s `x` slot is now read as `y`), not memory unsafety. It is the same
tri-state-collapse root as **CPY-0078** (`same_slots_added` at :7609), now with a second consequence.

**Prior art:** none. `gh api -X GET search/issues -f q='repo:python/cpython same_slots_added stop the world'`,
`'... check_invalid_reentrancy world_stopped'` and `'... object_set_class stop-the-world __slots__'`
all return `total_count: 0`.

**Scanner blindness.** `scan_stw_safety` reports `stw_functions=3` on this file and cannot see this:
the region is opened by the `types_stop_world()` wrapper rather than the literal
`_PyEval_StopTheWorld` token, and the offending call is two frames below the opener. Both limits
are exactly the ones `preflight/include_map.md` §4.1/§4.3 predicted — this reproduces that
prediction rather than discovering it.

### 5.2 [POLICY — test-support, ranks low] `_testcapi.add_type_watcher` asserts on its Python argument

`Modules/_testcapi/watchers.c:239` — `add_type_watcher(PyObject *self, PyObject *kind)` does
`assert(PyLong_Check(kind))`. Passing a callable (my probe's own API misuse) aborts the interpreter
on **all three** builds, `release-gil-nojit` included — `_testcapi` is compiled with assertions
live even in the release build. Assert-on-Python-supplied-argument is a real shape, but this is
test-support code reachable only from `_testcapi`, so it ranks low; recorded for completeness, not
proposed as a fix.

---

## 6. TOOLKIT ASSESSMENT

### 6.1 Verdict on my own zero

**Earned for the modelled shape; structurally silent for three shapes outside it.**

- Rule 8's widening **fired**: 49 → 115 matched sites (+135 %), 16 → 53 distinct callees, including
  a correct two-hop forwarder resolution (`_PyHeapType_GET_MEMBERS`).
- **Measured precision: 0 false suppressions in 27 examined drops** (23 stage-6 "checked first",
  4 stage-4 unchecked-but-undominated). Every drop I read was correct.
- **Measured margin: 0 net-new candidates** when the alphabet is widened a further 46 % (W3, 239
  names). Everything W3 surfaces is already triaged as ACCEPTABLE or already recorded as CPY-0089.
- **Independent hand-check: 0 hits in 201/201 pass-2 assignment-from-call sites.**

This is a genuinely clean bill for `unchecked_alloc` / `deref_before_check` on this file — the
first time in this campaign a zero has been backed by a full-population hand-check rather than a
sample. It is **not** a clean bill for `decref_of_nulled_outparam`, whose denominator is 0.

**And it is not a clean bill for the file.** The pass produced two findings, and *neither is the
shape this scanner models*: an off-by-one OOB index (§3.2, found by reading R3) and a
stop-the-world reentrancy abort (§5.1, found by probing R25). Both were surfaced by the parts of
the method that are not the scanner. The honest summary of a null-safety zero on this file is:
**the NULL-dereference class really is absent here, and that fact says nothing about the file's
other classes** — which is precisely why the campaign runs sixteen agents and not one.

### 6.2 Recall gaps found by reading

**RG-1 — `nullable_source_calls` cannot admit a field-forwarding accessor. (highest value)**

The rule admits a same-file pointer-returning function only if its body has a literal
`return NULL;` / `return PyErr_NoMemory();`, or it forwards via `return NAME(...)`. It cannot admit:

```c
static inline PyObject *lookup_tp_mro(PyTypeObject *self) { return self->tp_mro; }
```

whose nullability comes from the **field**. On this file that class is the central idiom and hides
**53 assignment sites**: `lookup_tp_dict`(25), `lookup_tp_mro`(13), `lookup_tp_subclasses`(8),
`lookup_tp_bases`(7). `scan_refcounts` models exactly these four functions
(`borrowed_field_accessors=4`) — **two scanners look at the same four functions and only one can
see them.** `update_cache` (§3.3) is a fifth instance.

> Proposed rule `field_forwarding_accessor`: admit `TYPE *F(...) { … return X->FIELD; }` as a
> nullable source when `FIELD` is `Py_CLEAR`ed, `Py_XSETREF`ed, or assigned `NULL` anywhere in the
> same file. Measured here: `tp_mro` qualifies (`Py_CLEAR` at :7273; `set_tp_mro:653` accepts NULL),
> as do `tp_bases` and `tp_subclasses`. `tp_dict` also qualifies, which is why 13 of W1's 14
> candidates are the known-ACCEPTABLE class — so emit at **medium** confidence and pair it with a
> "is the receiver a ready type" suppressor keyed on `Py_TPFLAGS_READY` / `_PyType_IsReady` /
> in-`type_ready`-pipeline dominance. Without that suppressor this rule is 13/14 noise on this file.

**RG-2 — `decref_of_nulled_outparam`'s discovery is seeded by a closed 4-name list, so it cannot
reach a file's own out-parameter contracts.**

`outparam_wrappers=0` here means "no call site of the 4 resize APIs", not "no out-parameters":
the file has five (§1e). The rule can only ever fire in files that call `_PyTuple_Resize` & co.

> Proposed: derive the contract from the **callee body** instead of the seed list — a
> `PyObject **p` parameter that is assigned `NULL` on at least one path and non-NULL on another
> establishes "may be NULL on return"; then classify each caller's `Py_DECREF(x)` (finding) vs
> `Py_XDECREF(x)` / `Py_CLEAR(x)` (fine) after `f(&x)`. On this file that yields 5 contracts and
> 0 findings — a real zero instead of a structural one.

**RG-3 — `_ALT_PATH_RE` is the largest single suppressor and is currently uninstrumented.**

`_dominates()` returns False if any `goto|return|case|default` sits at the assignment's brace depth
between assignment and deref. `Objects/typeobject.c` is `goto`-dense, and this filter drops
**36 of 59** deref-in-window candidates (61 %). On this file all 36 are correct (32 have a check
anyway; the 4 that do not are true negatives, §4). But a filter carrying 61 % of the suppression
mass should be visible in the envelope.

> Proposed: emit `denominators.dominance_drops` and `denominators.check_drops` alongside `findings`.
> A run that reports `findings: 0, dominance_drops: 36` is readable; one that reports `findings: 0`
> is not. This is the §1b table, which I had to reverse-engineer for the second pass running.

**RG-4 — new FP class for `data/cpython_non_bugs.md`: singleton comparison with an early return.**

```c
r = vectorcall_maybe(tstate, &_Py_ID(__pow__), stack, 3);
if (r != Py_NotImplemented)
    return r;
Py_DECREF(r);            /* reached only when r == Py_NotImplemented */
```

`NULL != Py_NotImplemented`, so the comparison discharges NULL — but it names a singleton, so
`_NULL_CHECK_TEMPLATE` cannot match it and only the dominance filter saves the day (by accident:
it fires on the `return`, not on the semantics). Exemplars: `Objects/typeobject.c:10775-10777`,
`:10785-10788`. Generalise to `Py_None` / `Py_True` / `Py_False` / `Py_Ellipsis`.

> Proposed: add `{var}\s*[!=]=\s*Py_(?:NotImplemented|None|True|False|Ellipsis)` to
> `_NULL_CHECK_TEMPLATE` when the controlled statement terminates (return/goto/break/continue).

### 6.3 Correction to this run's preflight

`preflight/include_map.md` §R26 asserts that `object_getstate_default`'s `PyList_GET_ITEM` at :7990
is unguarded against a user-code-driven shrink. It is guarded — the size test at :8010 is at the
**end** of the loop body, i.e. between iteration *i*'s user code and iteration *i+1*'s index. §4
records the correction so the claim is not carried into a later pass.

---

## 7. SUMMARY

| | |
|---|---|
| `decref_of_nulled_outparam` | 0 — **denominator 0, structural silence**; 5 real out-param contracts cleared by hand instead |
| `deref_before_check` | 0 reported, **0 found by reading** (201/201 pass-2 sites hand-checked) |
| `unchecked_alloc` | 0 reported; 18 candidates under a 46 %-wider alphabet, **all already triaged**, 0 net new |
| Confirmed by OOM injection | 0 new (no allocation-failure-only candidate survived triage) |
| **Reproduced by dynamic probing** | **1 FIX** (`:7609` user `__eq__` inside stop-the-world; SIGABRT on debug-ft, silent violation on release-ft) — routed to `stw-safety-checker` |
| **New static findings** | **1 CONSIDER** (`:294` off-by-one OOB, routed to `memory-pattern-analyzer`) |
| Recorded ACCEPTABLE-with-invariant | 2 (`:6256`/`:6383` cache-name DECREF, `:6095`/`:6107` `tp_base`) |
| Test-support POLICY | 1 (`_testcapi/watchers.c:239` assert on a Python argument) |
| Confirmed prior findings | 3 (CPY-0007, CPY-0070, CPY-0089 — one line each) |
| Verified negatives recorded | 9 |
| Dynamic probes run | 23 payloads × 3 builds (+2 builds on the reproducer) |
| Recall gaps proposed | 4 (RG-1 field-forwarding accessors, RG-2 out-param contracts, RG-3 dominance instrumentation, RG-4 singleton-comparison FP class) |
| Preflight corrections | 1 (`include_map.md` §R26) |

Artifacts: `scratchpad/p2null/stages.py` (pipeline instrumentation), `drops.py` (per-stage drop
dump), `widened.py` (W1/W2/W3 alphabet experiment), `sample.py` + `sample.txt` (the 201-site
population with context), `probes.py` + `probes.out` (dynamic sweep).
