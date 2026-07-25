# Pass-2 orientation — `Objects/typeobject.c`

CPython main @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0), 13,068 lines.
Companion generated file: `Objects/clinic/typeobject.c.h` (265 lines, included at line 40).

**Read pass 1's include map first for the structural layer** —
`/home/danzin/projects/cpython-review-toolkit/reports/obj-typeobject/preflight/include_map.md`.
It covers the 30 `#include` directives, the API-tier table (26 internal `pycore_*.h`, all
resolving, no tier violation, no cycle on this path), the full 37-region section map, the
external-consumer table (who links against this file's non-static symbols), and the clinic
inventory. **None of that is repeated here.** This document covers only what pass 1 did not
read: the eight regions below, their locking vocabulary, their shared state, and the
macro-driven blind spots that make three scanners under-report on this file.

Scope note: this is an orientation document. Candidates are named as *hunt targets* with the
evidence needed to triage them; nothing here is a triaged finding.

---

## 0. The pass-2 regions at a glance

| # | Lines | Region | Fns | Entry point (Python-level) |
|---|-------|--------|-----|----------------------------|
| R2 | 42–227 | FT lock/world-stop preamble | 11 | — (infrastructure) |
| R3 | 228–522 | Managed static types + flag setters | 17 | interpreter init/fini; `PyType_Ready` on a static type |
| R4 | 524–810 | `tp_dict`/`tp_bases`/`tp_mro`/`tp_subclasses` accessors | 19 | every other region |
| R6 | 971–1481 | Type cache, watchers, version tags | 21 | any attribute write on any type |
| R11 | 3217–3702 | MRO — C3 linearisation | 11 | `class` stmt; `T.__bases__ = …`; `T.mro()` |
| R18 | 5834–6139 | `PyType_Get*` / module-by-token / type data | 20 | C-API only (no Python surface) |
| R19 | 6140–6452 | MRO name lookup + method cache | 12 | **every attribute read in the interpreter** |
| R20 | 6453–6528 | `_PyType_SetFlags{,Recursive}` / `_PyType_Validate` | 4 | `abc.ABCMeta.register` / `_abc` |
| R21 | 6529–6848 | `type` getattro / setattro | 6 | `T.x`; `T.x = v`; `del T.x` |
| R25 | 7482–7846 | `object` repr/richcompare, **`__class__` assignment** | 11 | `o.__class__ = C` |
| R26 | 7848–8406 | pickle / `__reduce__` | 12 | `pickle.dumps(o)`; `copy.copy(o)` |
| R37 | 12534–13068 | `super` | 13 | `super()`; `super(T, o).x` |

R18 and R20 are not in the brief's list but are read here because they are contiguous with
R19 and share its state; R20 in particular owns two of the file's stop-the-world sites.

Pass 1's territory (slot wiring, `type_new`, `PyType_FromSpec`, `object_new`/`object_init`,
`PyType_Ready`, `slotdefs[]`, `update_one_slot`) is **not** re-read here.

---

## 1. Regions in detail

### R2 — FT lock / world-stop preamble (42–227)

Infrastructure. No Python entry point, but **every concurrency judgement in the other seven
regions is defined here**, so read it first.

Functions: `types_world_is_stopped` 90, `types_stop_world` 124, `types_start_world` 133,
`type_lock_prevent_release` 144, `type_lock_allow_release` 166, `type_from_ref` 218.
Plus forward decls of four `slot_*` dispatchers at 203–215.

Headers leaned on: `pycore_lock.h` (`_PySeqLock_*`, `Py_BEGIN_CRITICAL_SECTION*_MUTEX`),
`pycore_pystate.h` (`_PyInterpreterState_GET`, `interp->stoptheworld.world_stopped`,
`tstate->critical_section`), `pycore_pyatomic_ft_wrappers.h` (`FT_ATOMIC_*`),
`pycore_interp_structs.h` (`interp->types.mutex`).

**The design comment at 65–77 is the file's central concurrency contract.** Quote it when
triaging anything in R3/R4/R6/R11/R19/R20:

> There's a global lock for types that ensures that `tp_version_tag` and `_spec_cache` are
> correctly updated if the type is modified. It also protects `tp_mro`, `tp_bases`, and
> `tp_base`. […] Note that this lock does **not** protect updates of other type slots or the
> `tp_flags` member. Instead, we either ensure those updates are done before the type has been
> revealed to other threads or we only do those updates while the stop-the-world mechanism is
> active. The slots and flags are read in many places without holding a lock and without atomics.

So the file has **two** synchronisation regimes, and which one applies depends on *which field*
you are touching:

| Field | Protected by | Assertion |
|---|---|---|
| `tp_version_tag`, `_spec_cache`, `tp_mro`, `tp_bases`, `tp_base` | `TYPE_LOCK` | `ASSERT_TYPE_LOCK_HELD()` 108 |
| `tp_flags`, all other slots | stop-the-world **or** type not yet revealed | `ASSERT_WORLD_STOPPED_OR_NEW_TYPE(tp)` 112 |
| either, depending on reveal state | `ASSERT_NEW_TYPE_OR_LOCKED(tp)` 115 | |

`TYPE_IS_REVEALED(tp)` (102) reads `_Py_TYPE_REVEALED_FLAG` out of `ob_flags`, and **is
hard-coded to 0 on 32-bit** (104) — so all three assertions are vacuous there. A finding that
depends on a reveal-state assertion firing is 64-bit-only.

### R3 — Managed static types (228–522)

Entry points: interpreter init (`_PyStaticType_InitBuiltin`, 9686, pass-1 territory) and fini;
`_PyStaticType_InitForExtension` from `_datetime`. No direct Python surface.

Functions: the 1-based index encoding `managed_static_type_index_{is_set,get,set,clear}`
232–255, `static_ext_type_lookup` 261, `managed_static_type_state_get` 285,
`_PyStaticType_GetState` 302, `managed_static_type_state_init` 310,
`managed_static_type_state_clear` 382, `_PyStaticType_GetBuiltins` 429.
Then the flag setters `type_set_flags` 455, `type_set_flags_with_mask` 462, `type_add_flags`
470, `type_clear_flags` 476, and the readying trio `start_readying` 482 / `stop_readying` 497 /
`is_readying` 512.

**The load-bearing trick:** for a `_Py_TPFLAGS_STATIC_BUILTIN` type, `tp_subclasses` is *not a
pointer* — it stores a 1-based index into the per-interpreter state array (242–243, 250–251).
Any scanner or reader treating `tp_subclasses` as a `PyObject*` on a static builtin is wrong.
`lookup_tp_subclasses` (732) is the only correct reader.

State touched: `interp->types.builtins.initialized[]`, `interp->types.for_extensions.initialized[]`,
`_PyRuntime.types.managed_static.types[full_index].{type,interp_count}`.
Locking: `interp->types.mutex` taken **raw** via `PyMutex_Lock`/`PyMutex_Unlock` at 323/326 and
416/422 — the only three `PyMutex_Lock` sites in the file, and note this is the *same mutex*
`TYPE_LOCK` names (78). Builtins take no lock at all (319, 411–413); only the
`for_extensions` path locks. That asymmetry is deliberate (builtins are initialised
single-threaded) but is worth confirming against `_PyStaticType_InitForExtension`'s callers.

`interp_count` is the only field accessed atomically (`_Py_atomic_load_int64` /
`_Py_atomic_add_int64`, 268/347/395/402); `state->type`, `state->tp_dict`, `num_initialized`
and `next_index` are plain accesses under (or without) the mutex.

### R4 — Accessors (524–810)

**This region is the single most important input to the refcount agent**, because
`scan_refcounts` reports `borrowed_field_accessors=4` for this file and these are the four:
`lookup_tp_dict` 527, `lookup_tp_bases` 580, `lookup_tp_mro` 645, `lookup_tp_subclasses` 732.
45 `borrowed_accessor_load_sites` and 13 of the 18 slice findings hang off them.

The discriminator the baseline asks for — *can this field actually be reassigned re-entrantly
from that window?* — resolves per field, not per call site:

| Field | Reassigned by | Reachable from Python? | Verdict shape |
|---|---|---|---|
| `tp_dict` | `set_tp_dict` 553 / `clear_tp_dict` 566 | Only `PyType_Ready` (9129) and `type_dealloc`. Never on a live ready type. | borrowed `lookup_tp_dict` on a ready type is **ACCEPTABLE** — the baseline's hint is correct |
| `tp_bases` | `set_tp_bases` 599 | **Yes** — `type_set_bases_unlocked` 1929 ⇐ `T.__bases__ = …` | live hazard |
| `tp_mro` | `set_tp_mro` 651 | **Yes** — `mro_internal` 3678 ⇐ `T.__bases__ = …`, custom `mro()` | live hazard, but see below |
| `tp_subclasses` | `init_tp_subclasses` 700 / `clear_tp_subclasses` 717; **entries** mutated by `add_subclass` 9693 / `remove_subclass` 9773 | Yes (any subclass creation/teardown) | dict-*mutation* hazard, not pointer-swap |

`set_tp_mro` (651) carries its own mitigation: for `initial == 0` it wraps the store in
`type_lock_prevent_release(); types_stop_world(); … types_start_world();
type_lock_allow_release();` (667–675). So a *reader* of `tp_mro` that never yields cannot
observe a torn swap — but a reader that calls into Python between load and use still can, since
the world restarts the moment the store completes. That is the exact discriminator for the
`:3443` `mro_implementation_unlocked` NET-NEW candidate.

`_PyType_GetBases` 586 is the only accessor that takes `TYPE_LOCK` (590–593) and returns a
strong ref. `_PyType_GetDict` 539 explicitly documents "It returns a borrowed reference."

`_PyType_GetSubclasses` 776 is the `:793` NET-NEW candidate. Its own comment (788–789) asserts
the discharge: *"The loop cannot modify tp_subclasses, there is no need to hold a strong
reference"* — but the loop calls `PyList_Append` (799), which can trigger GC, which can run a
`__del__`, which can drop the last reference to a subclass and reach `remove_subclass` (9773)
→ `PyDict_DelItem` on the very dict `PyDict_Next` is walking. **The comment states an
invariant; check whether it still holds.** This is the highest-value single lead in R4.

### R6 — Type cache, watchers, version tags (971–1481)

Entry points: `PyType_Watch`/`PyType_Unwatch`/`PyType_AddWatcher`/`PyType_ClearWatcher` (C-API
+ `_testcapi`); indirectly **every** attribute write on any type, via `PyType_Modified`.

Functions: `get_type_cache` 973, `type_cache_clear` 981, `_PyType_InitCache` 999,
`_PyType_ClearCache` 1016, `PyType_ClearCache` 1028, `_PyTypes_Fini` 1036,
`PyType_AddWatcher` 1054, `validate_watcher_id` 1071, `PyType_ClearWatcher` 1085,
`PyType_Watch` 1098, `PyType_Unwatch` 1118, `set_version_unlocked` 1134,
**`_PyType_Modified_Unlocked` 1166**, `PyType_Modified` 1243, `has_custom_mro` 1261,
`type_mro_modified` 1279, `_PyType_SetVersion` 1336, `_PyType_LookupByVersion` 1344,
`next_global_version_tag` 1395, **`assign_version_tag` 1408**.

Constants from `pycore_interp_structs.h`: `TYPE_MAX_WATCHERS` 8 (:22), `MCACHE_SIZE_EXP` 12
(:574), `TYPE_VERSION_CACHE_SIZE` `1<<12` (:596), `struct type_cache_entry` :565,
`struct type_cache` :576, `managed_static_type_state` :594.
From `pycore_typeobject.h`: `_Py_TYPE_BASE_VERSION_TAG (2<<16)` :34,
`_Py_MAX_GLOBAL_TYPE_VERSION_TAG` :35, the twelve `_Py_TYPE_VERSION_*` fixed tags :18–29.
File-local: `MAX_VERSIONS_PER_CLASS 1000` (1389, with a compile-time guard at 1390–1392).

**Two invariants are stated in comments here, and both are candidates for violation checks:**

1. `_PyType_Modified_Unlocked` 1172–1176: *"before `tp_version_tag` can be set on a type, it
   must first be set on all super types."* `assign_version_tag` 1412–1415 restates it and
   enforces it by recursing over `lookup_tp_bases` first (1427–1434). The *clearing* direction
   is the mirror: clear on all subclasses before self (1195–1209). Anything that sets or clears
   a version tag outside these two functions breaks the invariant — `set_version_unlocked`
   (1134) is called directly at 1233, 1316, 1339, 1442, 1451.
2. 1185–1188: *"This function is re-entrant and it's not safe to call it with the world
   stopped"*, backed by `assert(!types_world_is_stopped())`. This is why
   `_PyType_SetFlagsRecursive` (6506) invalidates **before** stopping the world and says so
   at 6509–6513. Any new stop-the-world region that wants to invalidate must follow that order.

Re-entrancy hazards inside `_PyType_Modified_Unlocked`: the watcher callback loop (1216–1230)
calls arbitrary C (and thus possibly Python) at 1222 **while walking `type->tp_watched` and
before `set_version_unlocked(type, 0)` at 1233**. The comment at 1219–1220 acknowledges
`PyErr_FormatUnraisable` is re-entrant. So a watcher callback observes the type with its *old*
version tag still live — deliberate, but it means a callback that itself mutates the type
re-enters this function on a type mid-invalidation. Also note the recursion at 1206 is
unbounded in subclass-tree depth (this is CPY-0087's shape — confirm, do not re-litigate).

`type_mro_modified` 1279 is the other version-clearing path; it calls `has_custom_mro` (1261),
which performs **two full `_PyType_LookupStackRefAndVersion` lookups** (1268–1269) — i.e. R6
calls into R19 while `ASSERT_TYPE_LOCK_HELD()` (1295) is in force. Confirm R19's
`BEGIN_TYPE_LOCK()` at 6358 is re-entrant-safe on this path (critical sections nest, but check
the `type_lock_prevent_release` interaction).

`PyType_Unwatch` 1118 writes `type->tp_watched` **without `TYPE_LOCK`**, while `PyType_Watch`
1098 writes the same field under it (1110–1113). That asymmetry is visible, small, and
unexplained by any comment — a clean lead for the FT agents.

### R11 — MRO / C3 linearisation (3217–3702)

Entry points: the `class` statement (via `type_new` → `type_ready`), `T.__bases__ = …`
(`type_set_bases_unlocked` 1929), `T.mro()` (clinic `type_mro_impl` 3535), and
`PyType_Ready`.

Functions: `tail_contains` 3246, `class_name` 3259, `check_duplicates` 3270, `set_mro_error`
3310, `pmerge` 3361, **`mro_implementation_unlocked` 3431**, `mro_implementation` 3519,
`type_mro_impl` 3535, `mro_check` 3547, **`mro_invoke` 3591**, **`mro_internal` 3655`**.

Locking: `mro_implementation` 3519 is the only lock-taker (`BEGIN_TYPE_LOCK()` 3522);
`mro_implementation_unlocked` 3433, `mro_invoke` 3596 and `mro_internal` 3657 each open with
`ASSERT_TYPE_LOCK_HELD()`. Raw allocator use: `PyMem_New`/`PyMem_Free` at 3371/3425 (`remain`)
and 3488/3505/3513 (`to_merge`) — both hand-paired, both on `goto`-heavy paths.

**The re-entrancy contract is documented three times and is the whole story of this region:**

- 3579–3588 (on `mro_invoke`): *"during execution of this function `type->tp_mro` can be
  replaced due to possible reentrance"* — via the `mcls.mro` attribute lookup, from inside a
  custom `mro()`, or **through a finalizer of the return value of `mro()`**.
- 3600–3603: the custom-metaclass branch calls `call_method_noarg(…, &_Py_ID(mro))` and is
  flagged "This is potentially re-entrant."
- 3632–3653 (on `mro_internal`): the three-valued return contract. `1` = new MRO set, `0` =
  re-entered and *someone else already updated the whole subclass hierarchy*, `-1` = error.
  The re-entrancy check is pointer identity: `old_mro = Py_XNewRef(lookup_tp_mro(type))` 3665,
  `reent = (lookup_tp_mro(type) != old_mro)` 3667 — with the strong ref taken **specifically**
  so the address cannot be recycled (3662–3664). That is a deliberate ABA defence; note it
  before flagging the `Py_XNewRef`/`Py_XDECREF` pair as redundant.

`mro_implementation_unlocked` is the `:3443` NET-NEW `borrowed_field_deref_across_call`
candidate: `bases` is `lookup_tp_bases(type)` (3440, borrowed) and the loop at 3442–3451 calls
`PyErr_Format` (3445) and later `check_duplicates` (3476) → `class_name` (3259) →
`PyObject_GetOptionalAttr` / `PyObject_Repr` — **arbitrary Python**, reachable via a
`__name__` descriptor on the metaclass. `class_name`'s own comment says "Note that this is
potentially re-entrant" (3262). After that call `bases` is still used at 3495–3501 to build
`to_merge`. The discriminator: `TYPE_LOCK` is held across the whole thing, and
`set_tp_bases` (599) opens with `ASSERT_NEW_TYPE_OR_LOCKED(self)` — so a *concurrent thread*
cannot swap it, but a *re-entrant* `T.__bases__ = …` on this same thread re-acquires the same
critical section and can. Decide on that axis.

`mro_check` 3547 calls `solid_base` (3771, pass-1 region 12) and `is_subtype_with_mro`; it runs
only for the `custom` path (3625).

### R18 — `PyType_Get*` / module-by-token (5834–6139)

Not in the brief, included because `get_base_by_token_recursive` 5988 is a NET-NEW
`self_recursion` candidate (`:6002`) and because this region is the `pycore_moduleobject.h`
consumer the brief asks about.

`pycore_moduleobject.h` is included at line 15 with the comment `// _PyModule_GetDef()`. In
practice this region reaches module state through `ht_module` directly (5866, 5882, 5889,
5927, 5955) and through `_PyType_GetModuleState` (defined in `pycore_typeobject.h`:76–88, not
here). `ht_module` is also written at 4444 and 5632 (pass-1 territory) and torn down at 7038,
7216, 7271 (region 22/23). There is **no** `PyModuleObject` field access in the pass-2 regions
proper — the header earns its place through `_PyType_GetModuleState`'s `md_state` deref, which
asserts `et->ht_module != NULL` rather than checking it.

`get_base_by_token_recursive` 5988 recurses on `lookup_tp_bases(base)` (6002) with no depth
guard. Bases graphs are DAGs of bounded depth in practice, but `PyType_GetBaseByToken` is
callable on any type from any extension. The guarded twin to compare against is
`_PyType_Modified_Unlocked`'s subclass recursion (1206) — also unguarded, which is why
CPY-0087 exists. Same shape, different direction of travel.

Note the `_DuringGC` API family here (5859, 5893, 5916, 6012, 6092, 6129): these are the
variants that must not INCREF. Their non-`_DuringGC` wrappers add the `Py_INCREF`/`Py_XNewRef`
(5967, 6083–6085). Confusing one for the other is a refcount bug shape specific to this region.

### R19 — MRO name lookup + method cache (6140–6452)

**The hottest code in the file.** Entry point: every attribute read in the interpreter
(`_PyType_Lookup`, `_PyType_LookupRef`), plus `LOAD_ATTR`/`LOAD_METHOD` specialisation.
External consumers: `Objects/object.c` (1782, 1919, 2043) and `Python/specialize.c`
(571, 608, 624, 1181, 1633, 2043, 2432).

Functions: `find_name_in_mro` 6145, `is_dunder_name` 6200, `update_cache` 6216,
`update_cache_gil_disabled` 6237, `_PyTypes_AfterFork` 6262, `_PyType_LookupRefAndVersion`
6284, `should_assign_version_tag` 6298, **`_PyType_LookupStackRefAndVersion` 6306**,
`_PyType_LookupRef` 6392, `_PyType_Lookup` 6400, `_PyType_CacheInitForSpecialization` 6408,
`_PyType_CacheGetItemForSpecialization` 6428.

Cache-entry macros (file-local, 48–58): `MCACHE_MAX_ATTR_SIZE 100`, `MCACHE_HASH`,
`MCACHE_HASH_METHOD` (note it embeds `FT_ATOMIC_LOAD_UINT_RELAXED` at 54 — see §3),
`MCACHE_CACHEABLE_NAME`.

**The store-ordering invariant is documented at 6225–6229 and is the sharpest thing to check
in this region:**

> We must write the version **last** to avoid `_Py_TryXGetStackRef()` operating on an invalid
> (already deallocated) value inside `_PyType_LookupRefAndVersion()`. If we write the version
> first then a reader could pass the `entry_version == type_version` check but could be using
> the old entry value.

`update_cache` 6216 obeys it: value (relaxed, 6218) → name (relaxed, 6224) → version
(**release**, 6230). The reader at 6313–6337 mirrors it: seqlock begin → version acquire
(6315) → type version acquire (6316) → name compare (6318) → `_Py_TryXGetStackRef` (6321) →
seqlock end (6323). Any new store into a `type_cache_entry` must preserve that order.

Three distinct synchronisation mechanisms coexist in ~150 lines: the **seqlock**
(`_PySeqLock_*`, 6240/6247/6254/6268/6314/6323 and 986/992 in R6), **acquire/release atomics**
on `entry->version`, and **`TYPE_LOCK`** around `assign_version_tag` (6358–6362). The GIL and
FT builds take structurally different paths (`#ifdef Py_GIL_DISABLED` at 6311 vs `#else` 6338).
A finding must state which build it applies to.

`find_name_in_mro` 6145 calls `PyErr_Clear()` three times (6149, 6158, 6183) — this is the
region's `unconditional_pyerr_clear` cluster. Its contract is in the header comment
(6140–6143): *"It never set an exception"*, and the caller asserts `!PyErr_Occurred()` at 6351
with the comment *"We may end up clearing live exceptions below, so make sure it's ours."* So
the clears are contract-discharging, not swallowing — **but only if the assert holds in
release builds too**, where it compiles out. `_Py_dict_lookup_threadsafe_stackref` (6180) can
run a user `__eq__` on a non-string key; the `DKIX_ERROR` clear at 6183 therefore discards a
user exception. Whether that is reachable depends on whether a non-unicode key can reach a
type's `tp_dict`. That is the one real question in this cluster.

`find_name_in_mro` also pins `mro` with a `_PyCStackRef` (6172–6174) *"because `type->tp_mro`
can be replaced during dict lookup"* (6170–6171) — the same defence `_PySuper_LookupDescr`
uses (12597–12601). Note that R19 pins it and R11's `mro_implementation_unlocked` does not
pin `bases`; that asymmetry is the sibling-hunt axis.

`_Py_type_getattro_stackref`'s `:6620` NET-NEW `publish_before_init_complete` (confidence
`low`) sits at the `_PyType_LookupStackRefAndVersion(type, name, &attribute_ref.ref)` call —
see R21.

### R20 — flag setters (6453–6528)

`_PyType_SetFlags` 6453, `_PyType_Validate` 6465, `set_flags_recursive` 6483,
`_PyType_SetFlagsRecursive` 6506. Both public entry points have exactly one caller each:
`Modules/_abc.c:541` and `Modules/_abc.c:601` — i.e. `abc.ABCMeta` / `__subclasshook__`.

**These two functions are an unexplained asymmetry and the cleanest concurrency lead in the
pass-2 set:**

| | `_PyType_SetFlags` 6453 | `_PyType_SetFlagsRecursive` 6506 |
|---|---|---|
| takes `TYPE_LOCK` | **no** | yes (6508) |
| invalidates version tag first | **no** | yes, `_PyType_Modified_Unlocked` 6517 |
| `type_lock_prevent_release` | **no** | yes (6521) |
| stop-the-world | yes (6457) | yes (6522) |
| re-reads flags inside STW | yes, with comment *"can't use new_flags here since they could be out-of-date"* (6458) | via `set_flags_recursive` |

The recursive version's comment (6509–6513, 6519–6520) explains *why* both the ordering and
the `prevent_release` are required. The non-recursive version does neither, and its own comment
(6458) admits it raced on read. `set_flags_recursive` 6483 also recurses without a depth guard
(6500) — that is the already-recorded CPY-0087 shape, confirm and move on.

`_PyType_Validate` 6465 is the specialisation gate consumed by `Python/specialize.c`; it calls
a caller-supplied function pointer (`validate(ty)`, 6469) **while holding `TYPE_LOCK`**. Every
`_py_validate_type` implementation in the tree is a candidate for "does this take another lock
or call Python."

### R21 — `type` getattro / setattro (6529–6848)

Entry points: `T.x` (`tp_getattro = _Py_type_getattro`, `PyType_Type` line ~7307),
`T.x = v` / `del T.x` (`tp_setattro = type_setattro`, ~7308).

Functions: `_Py_type_getattro_impl` 6542, `_Py_type_getattro` 6554,
**`_Py_type_getattro_stackref` 6571**, `type_update_dict` 6694, `update_slot_after_setattr`
6726, **`type_setattro` 6750**.

**Read path.** `_Py_type_getattro_stackref` 6571 is a three-`_PyCStackRef` dance
(6591–6595 push, 6686–6688 pop) with **six** `goto done` exits. Each of the three refs must be
in a consistent state at every exit; the hand-offs at 6650–6651 and 6669–6670 NULL the source
after transferring. It calls user code at three points: `meta_get` (6609), `local_get`
(6641), `meta_get` again (6659) — all descriptor `__get__`. `metatype` is captured **once** at
6574 (`Py_TYPE(type)`, borrowed) and used at 6610 and 6660, after those calls. That is a
borrowed-field-across-call shape the scanner did not flag (it keys on the four `lookup_tp_*`
accessors, not on `Py_TYPE`). Whether `Py_TYPE(type)` can change under it is exactly the
`__class__`-assignment question of R25 — the two regions meet here.

**Write path.** `type_setattro` 6750 is the file's most lock-dense function:
`BEGIN_TYPE_LOCK()` 6817–6822 (the lazy `tp_dict` creation) then `BEGIN_TYPE_DICT_LOCK(dict)`
6829–6838, which is `Py_BEGIN_CRITICAL_SECTION2_MUTEX(TYPE_LOCK, &dict->ob_mutex)` — the only
two-mutex site in the file. Under it: `type_update_dict` (6694) then, if the name is a dunder
with a slotdef, `update_slot_after_setattr` (6726) which on FT reaches
`apply_type_slot_updates` (3902) and **stops the world while holding two mutexes**. The
ordering rationale is at 3906–3921 (*"we always acquire locks in the same order: first the
TYPE_LOCK mutex and then the STM mutex"*). Any new lock introduced on this path must respect
that order or it deadlocks.

`type_update_dict` 6694 opens with a four-line comment (6697–6702) stating a hard invariant:

> We don't want any re-entrancy between when we update the dict and call
> `_PyType_Modified_Unlocked`, **including running the destructor of the current value** as it
> can observe the cache in an inconsistent state. Because we have an exact unicode and our dict
> has exact unicodes we know that this will all complete without releasing the locks.

The claim rests on `type_setattro`'s interning of `name` (6768–6785). The old value's
destructor is deferred: `old_value` is returned to `type_setattro` and only DECREF'd at 6843,
after `END_TYPE_DICT_LOCK()`. Verify no path DECREFs it earlier. The `_PyDict_SetItem_LockHeld`
failure branch (6714–6720) reports `AttributeError` for what is really a set failure — worth a
look but likely intentional (matches `__delattr__` semantics when `value == NULL`).

`update_slot_after_setattr` 6726 stack-allocates one `slot_update_chunk_t` (6731) and asserts
`SLOT_UPDATE_CHUNK_SIZE >= MAX_EQUIV` (6730) — `SLOT_UPDATE_CHUNK_SIZE` is 30 (3822, file-local),
`MAX_EQUIV` is 10 (`pycore_interp_structs.h`:741). The post-condition
`assert(chunk.prev == NULL)` (6741) is the "should never allocate another chunk" claim. Both
asserts vanish in release builds; if the invariant can be broken, release silently leaks a chunk.

### R25 — `__class__` assignment (7482–7846)

Entry point: `o.__class__ = C` (`object_getsets` 7841–7845).

Functions: `object_repr` 7482, `object_str` 7510, `object_richcompare` 7521,
`_Py_BaseObject_RichCompare` 7565, `object_get_class` 7571, `compatible_with_tp_base` 7577,
`same_slots_added` 7588, `compatible_flags` 7617, **`compatible_for_assignment` 7628**,
**`object_set_class_world_stopped` 7700**, **`object_set_class` 7800**.

The 49-line comment at 7704–7752 is the region's design rationale (bpo-24912 / bpo-22986: the
interned-`int` disaster) and states the current rule: HEAPTYPE→HEAPTYPE **or** ModuleType
subtype→ModuleType subtype only.

**Locking.** `object_set_class` 7800 gates the world stop on refcount uniqueness:

```
int unique = _PyObject_IsUniquelyReferenced(self);   // 7821
if (!unique) { types_stop_world(); }                 // 7822-7824
PyTypeObject *oldto = Py_TYPE(self);                 // 7825
int res = object_set_class_world_stopped(self, newto);
if (!unique) { types_start_world(); }                // 7827-7829
```

Two things to check here, both real:

1. **`object_set_class_world_stopped` takes a lock while the world is stopped.** At 7778–7780
   it does `Py_BEGIN_CRITICAL_SECTION(dict) … _PyDict_DetachFromObject … Py_END_CRITICAL_SECTION()`,
   and at 7769 it calls `_PyObject_MaterializeManagedDict_LockHeld`. That is inside the
   `!unique` stop-the-world window. Taking a per-object lock that a *stopped* thread may hold
   is the textbook STW deadlock shape — the same class `stw-safety-checker` exists to find. It
   is invisible to `scan_stw_safety` for two compounding reasons (see §3): the region is opened
   by `types_stop_world()`, not `_PyEval_StopTheWorld`, and it spans a function boundary.
   **This is the single highest-value hunt target in the pass-2 set.**
2. The `unique` fast path means the check `_PyObject_IsUniquelyReferenced` must be sound under
   deferred/immortal refcounting. If it can return true for an object another thread can still
   reach, the whole mutation runs unsynchronised.

`Py_SET_TYPE(self, newto)` (7790) is the actual mutation; the INCREF of `newto` happens at
7786–7788 *before* it and the DECREF of `oldto` at 7831–7833 *after* the world restarts, using
the `oldto` captured at 7825. Check that `oldto` cannot be the same object as `newto` in a way
that makes the incref/decref ordering matter.

`same_slots_added` 7588 calls `PyObject_RichCompareBool(slots_a, slots_b, Py_EQ)` at 7609 —
arbitrary Python, on the `ht_slots` tuples, **inside** the stopped world (reached via
`compatible_for_assignment` 7683 ← 7763). Running Python while the world is stopped is the
other STW-safety violation class. `ht_slots` holds strings in every normal case, but
`__slots__` accepts anything with `__iter__` at class-creation time.

### R26 — pickle / `__reduce__` (7848–8406)

Entry points: `pickle.dumps(o)`, `copy.copy/deepcopy(o)`, `o.__reduce__()`,
`o.__reduce_ex__(p)`, `o.__getstate__()`. Four of the twelve functions are clinic-generated
wrappers (`object___getstate___impl` 8083, `object___reduce___impl` 8358,
`object___reduce_ex___impl` 8374 — clinic input blocks at 8076, 8351, 8364).

Functions: `import_copyreg` 7856, `_PyType_GetSlotNames` 7874, **`object_getstate_default`
7923**, `object_getstate` 8048, `_PyObject_GetState` 8071, `_PyObject_GetNewArguments` 8090,
`_PyObject_GetItemsIter` 8182, **`reduce_newobj` 8222**, `_common_reduce` 8334.

**No locking at all in this region** — no `TYPE_LOCK`, no critical section, no atomics. It is
pure refcount/error-path territory, and it is the region where the *most* user Python runs per
call.

The comment at 8318–8332 records the region's scar tissue: splitting `__reduce__` and
`__reduce_ex__` was necessary because *"Pickling objects that lied about their type by
overwriting the `__class__` descriptor could lead to infinite recursion at C level and eventual
segfault."* That is a direct pointer at R25 — **the two regions have already produced a joint
crash once.** Any new `Py_TYPE(obj)` / `__class__` disagreement in R26 is a sibling of that bug.
`reduce_newobj` reads `Py_TYPE(obj)` at 8230, 8233, 8264, 8280 — four times, across the
`_PyObject_GetNewArguments` (8236) and `import_copyreg` (8239) calls, both of which run Python.

`object_getstate_default` 7923 has the region's most intricate error handling: a `goto error`
target *inside* the loop body wrapped in `if (0) { error: … }` (8016–8023), plus a
mutation-during-iteration guard at 8010–8014 (`__slotnames__ changed size during iteration`)
whose comment (8008–8009) states the invariant: *"The list is stored on the class so it may
mutate while we iterate over it."* The guard checks size only — a same-size mutation slips
through, and `PyList_GET_ITEM(slotnames, i)` at 7990 is unchecked after
`PyObject_GetOptionalAttr` (7991) ran user code on the previous iteration. Size-stability is a
weaker invariant than the one actually needed.

`_PyType_GetSlotNames` 7874 reads `lookup_tp_dict(cls)` (7882) then immediately
`PyDict_GetItemRef` (7883) — a borrowed-accessor site, but on `tp_dict`, i.e. the ACCEPTABLE
column of R4's table.

### R37 — `super` (12534–13068)

Pass 1 covered the construction surface (`super_descr_get` 12781 → CPY-0080/CPY-0007,
`super_init` 12898, `super_vectorcall` 12985, `super_init_without_args` 12814). Pass 2 owns the
**lookup** surface.

Functions pass 2 owns: `super_dealloc` 12556, `super_repr` 12568, **`_PySuper_LookupDescr`
12588**, **`do_super_lookup` 12637**, **`super_getattro` 12688**, **`supercheck` 12703**,
`_PySuper_Lookup` 12769, `super_traverse` 12973.

The payload is three raw borrowed pointers (12536–12541): `su->type`, `su->obj`,
`su->obj_type`. `PySuper_Type` (13025) has `tp_traverse = super_traverse` but **`tp_clear = 0`**
(13050) — a GC-tracked type with traverse and no clear. That is intentional for `super`
(it participates in cycle detection but relies on `tp_dealloc`), but it is the shape
`type-slot-checker` flags, so state it up front.

**The unflagged lead.** `super_getattro` 12688 passes three borrowed fields into
`do_super_lookup` in one call: `do_super_lookup(su, su->type, su->obj, su->obj_type, name, NULL)`
(12699). `do_super_lookup` then runs arbitrary Python at 12656 (`f(res, …)`, a descriptor
`__get__`) and at 12680 (`PyObject_GenericGetAttr((PyObject *)su, name)`). Meanwhile
`super_init_impl` 12914 **re-initialises an existing super object in place** —
`Py_XSETREF(su->type, …)` / `Py_XSETREF(su->obj, …)` / `Py_XSETREF(su->obj_type, …)` at
12950–12952 — and it is reachable from Python a second time on an already-live object via an
explicit `super.__init__(s, T, o)`. The three `Py_XSETREF`s drop the old references. A
descriptor `__get__` that re-invokes `super.__init__` on the same `super` instance therefore
frees the objects `do_super_lookup`'s parameters point at, while they are still live locals.
`scan_refcounts` did **not** flag this — it keys `borrowed_field_deref_across_call` on the four
`lookup_tp_*` accessors, and `su->type` is a plain struct field. Recall gap; see §4.

`_PySuper_LookupDescr` 12588 does pin its MRO (`_PyThreadState_PushCStackRefNew`, 12601) with
the comment *"Keep a strong reference to mro because `su_obj_type->tp_mro` can be replaced
during `PyDict_GetItemRef`"* (12597–12598). Compare against `do_super_lookup`, which pins
nothing. It also has three exit paths (12613, 12624, 12630) that must each pop the CStackRef —
they do.

`supercheck` 12703 calls `PyObject_GetOptionalAttr(obj, &_Py_ID(__class__), …)` (12733), i.e.
it honours a *lying* `__class__` — the same trapdoor R26's comment blames for a historical
segfault. `super_repr` 12568 reads `su->type->tp_name` and `su->obj_type->tp_name` with a NULL
guard on `su->type` but **not** on the deref of `su->obj_type->tp_name` (12576) — guarded by
the `if (su->obj_type)` at 12572, so it is fine; note it so nobody re-flags it.

---

## 2. Locking and versioning vocabulary — the exact list for the concurrency agents

This is the answer to "tell the concurrency agents exactly what vocabulary lives here."

### 2.1 The three families

**A. `TYPE_LOCK` — a per-interpreter `PyMutex` driven through the critical-section macros.**

```
#define TYPE_LOCK &_PyInterpreterState_GET()->types.mutex          // 78
#define BEGIN_TYPE_LOCK()      Py_BEGIN_CRITICAL_SECTION_MUTEX(TYPE_LOCK)          // 79
#define END_TYPE_LOCK()        Py_END_CRITICAL_SECTION()                            // 80
#define BEGIN_TYPE_DICT_LOCK(d) Py_BEGIN_CRITICAL_SECTION2_MUTEX(TYPE_LOCK, &_PyObject_CAST(d)->ob_mutex)  // 82-83
#define END_TYPE_DICT_LOCK()   Py_END_CRITICAL_SECTION2()                           // 85
```

All four are no-ops in the GIL build (184–187). 21 `BEGIN_TYPE_LOCK()` call sites, 3
`BEGIN_TYPE_DICT_LOCK()`:

`BEGIN_TYPE_LOCK` → 590, 1110, 1250, 1338, 1461, 1743, 2029, 3522, 5934, 6358, 6415, 6434,
6468, 6508, 6817, 6934, 9627, 9669, 10177, 11407, 12522.
`BEGIN_TYPE_DICT_LOCK` → 6829 (plus 2 in pass-1 territory).

**B. Stop-the-world.**

```
static void types_stop_world(void)   // 124  → _PyEval_StopTheWorld(interp)   at 128
static void types_start_world(void)  // 133  → _PyEval_StartTheWorld(interp)  at 137
```

No-op macros in the GIL build (193–194). **Nine wrapper call-site pairs** plus **two raw
`_PyEval_StopTheWorld` sites** that bypass the wrapper entirely:

| Site | Function | Under `TYPE_LOCK`? | `type_lock_prevent_release`? |
|---|---|---|---|
| 669 / 673 | `set_tp_mro` (651) | yes (caller's) | **yes** 668/674 |
| 1745 / 1750 | `type_set_abstractmethods` (1716) | yes 1743 | **no** |
| 1939 / 1942 | `type_set_bases_unlocked` (1929) | yes (caller 2029) | **yes** 1938/1943 |
| 2005 / 2008 | `type_set_bases_unlocked` (1929) | yes | **yes** 2004/2009 |
| 3923 / 3925 | `apply_type_slot_updates` (3902) | yes | **yes** 3922/3926 |
| 6457 / 6460 | `_PyType_SetFlags` (6453) | **no** | n/a |
| 6522 / 6524 | `_PyType_SetFlagsRecursive` (6506) | yes 6508 | **yes** 6521/6525 |
| 7823 / 7828 | `object_set_class` (7800) | **no** | n/a |
| 12523 / 12525 | `PyType_Freeze` (12506) | yes 12522 | **no** |
| **1572 / 1576** | `type_set_name` (1546) | **no — raw `_PyEval_StopTheWorld`** | n/a |
| **1598 / 1601** | `type_set_qualname` (1581) | **no — raw `_PyEval_StopTheWorld`** | n/a |

Two asymmetries fall out of that table and are handed to the agents as leads:

- **`type_lock_prevent_release` is used at 5 of the 7 STW-inside-`TYPE_LOCK` sites.**
  `type_set_abstractmethods` (1745) and `PyType_Freeze` (12523) omit it. The rationale for
  needing it is spelled out at 3906–3921 and again at 6519–6520 ("Keep `TYPE_LOCK` held while
  waiting for stop-the-world so no thread can reassign a version tag before the flag update").
  Both omitting sites *do* re-assert `ASSERT_TYPE_LOCK_HELD()` immediately after
  `types_start_world()` (1751, 12526), which suggests the authors were aware the lock could
  have been dropped. **`_PyType_SetFlagsRecursive` is the guarded twin; those two are the
  candidates.**
- **`type_set_name` / `type_set_qualname` call `_PyEval_StopTheWorld` raw**, bypassing the
  wrapper's `assert(!types_world_is_stopped())` bracket (126, 129, 135, 138). They are the only
  two sites that do. Cosmetically a style break; functionally it means a nested stop from those
  two paths would not trip the assertion.

**C. `PyMutex` raw** — 3 pairs, all in R3 (`managed_static_type_state_init` 323/326,
`managed_static_type_state_clear` 416/422). Same mutex as `TYPE_LOCK`, acquired without the
critical-section wrapper, so it is **not** suspendable and does **not** participate in
`type_lock_prevent_release`'s bookkeeping.

**D. Seqlock** — `_PySeqLock_LockWrite` / `UnlockWrite` / `AbandonWrite` / `BeginRead` /
`EndRead` / `AfterFork` at 986, 992, 6240, 6247, 6254, 6268, 6314, 6323. Guards the method
cache entries only. `_PySeqLock_AbandonWrite` (6247) restores the previous sequence on a raced
update — an early-return-from-a-write-section that a naive acquire/release pairing check will
read as a leak.

### 2.2 The debug assertions

```
ASSERT_TYPE_LOCK_HELD()             // 108  → _Py_CRITICAL_SECTION_ASSERT_MUTEX_LOCKED(TYPE_LOCK)
                                    //        but ONLY if !types_world_is_stopped()
ASSERT_WORLD_STOPPED_OR_NEW_TYPE(t) // 112  → assert(!TYPE_IS_REVEALED(t) || types_world_is_stopped())
ASSERT_NEW_TYPE_OR_LOCKED(t)        // 115  → if (TYPE_IS_REVEALED(t)) ASSERT_TYPE_LOCK_HELD()
```

All three are `Py_DEBUG`-only (117–121) **and** all three are no-ops in the GIL build
(188–191). `TYPE_IS_REVEALED` is additionally hard-zero on 32-bit (104). So an invariant
enforced only by these assertions is enforced on exactly one of the four build configurations:
free-threaded + `Py_DEBUG` + 64-bit. Call sites: `ASSERT_TYPE_LOCK_HELD` ×18 (incl. 1295, 1410,
3433, 3596, 3657, 1751, 6739, 12526), `ASSERT_WORLD_STOPPED_OR_NEW_TYPE` ×3 (457, 464),
`ASSERT_NEW_TYPE_OR_LOCKED` ×5 (602, 1183).

### 2.3 Versioning

`tp_version_tag` (write: `set_version_unlocked` 1134 only; read: everywhere),
`tp_versions_used` (16-bit, `_Py_atomic_add_uint16` at 1151 on FT, plain `++` at 1147 on GIL),
`_Py_ATTR_CACHE_UNUSED` as the "never cache this type" sentinel (1304, 1317, 1423),
`MAX_VERSIONS_PER_CLASS` 1000 (1389), `next_global_version_tag` 1395 (CAS loop),
`NEXT_VERSION_TAG(interp)` (60–61, expands to `(interp)->types.next_version_tag`),
`_PyType_Modified_Unlocked` 1166 / `PyType_Modified` 1243 / `type_mro_modified` 1279 as the
three invalidation entry points, `_spec_cache.getitem` / `.init` as the two things that
*must* be nulled on invalidation (1234–1239, 1318–1323).

`interp->types.type_version_cache` (a `TYPE_VERSION_CACHE_SIZE`-entry array of borrowed
`PyTypeObject*`) exists **only in the GIL build** — `_PyType_LookupByVersion` returns `NULL`
unconditionally under `Py_GIL_DISABLED` (1346–1347), and `set_version_unlocked`'s cache
maintenance is `#ifndef Py_GIL_DISABLED` (1137–1145, 1155–1162). Anything reasoning about that
cache is GIL-build-only.

---

## 3. Shared state per region

| State | Written by | Read by | Guard | Invariant documented at |
|---|---|---|---|---|
| `tp_dict` | `set_tp_dict` 553, `clear_tp_dict` 566, lazy init 6820 | `lookup_tp_dict` 527 (45 sites) | none on read | — (comment 6813–6816 on the lazy path) |
| `tp_bases` | `set_tp_bases` 599 ⇐ `type_set_bases_unlocked` 1929 | `lookup_tp_bases` 580 | `TYPE_LOCK`; `ASSERT_NEW_TYPE_OR_LOCKED` 602 | 65–77 |
| `tp_mro` | `set_tp_mro` 651 ⇐ `mro_internal` 3678 | `lookup_tp_mro` 645 | `TYPE_LOCK` + STW for the store (667–675) | 3579–3588, 3632–3653 |
| `tp_subclasses` | `init_tp_subclasses` 700, `clear_tp_subclasses` 717; entries via `add_subclass` 9693 / `remove_subclass` 9773 | `lookup_tp_subclasses` 732 | none | 719–721, 788–789 |
| `tp_version_tag` | `set_version_unlocked` 1134 | everywhere, `FT_ATOMIC_LOAD_UINT*` | `TYPE_LOCK` | **1172–1176** (super-types-first) |
| `tp_versions_used` | 1147/1151, 1317 | 1304, 1422, 6301 | `TYPE_LOCK` (write), relaxed atomic (read) | 1422–1424 |
| `tp_watched` | `PyType_Watch` 1112 (**locked**), `PyType_Unwatch` 1129 (**unlocked**) | 1212–1214 | inconsistent | — |
| `tp_flags` | `type_set_flags` 455 and the three wrappers | everywhere, unguarded | STW or not-yet-revealed | **73–77** |
| `_spec_cache.{getitem,init}` | 1237, 1321, 6421, 6445 | `Python/specialize.c` | `TYPE_LOCK` + release store | 1235–1236, 1319–1320, 6436–6437 |
| `type_cache.hashtable[]` | `update_cache` 6216, `update_cache_gil_disabled` 6237, `type_cache_clear` 981 | `_PyType_LookupStackRefAndVersion` 6313–6345 | seqlock + acquire/release | **6225–6229** (store order) |
| `interp->types.type_version_cache` | `set_version_unlocked` 1144, 1160 | `_PyType_LookupByVersion` 1379–1384 | GIL only | 1326–1333 |
| `managed_static.types[].{type,interp_count}` | R3 310/382 | R3 261/285 | `PyMutex` (ext only); atomics for `interp_count` | 362, 367–369 |
| `ht_module` | 4444, 5632; cleared 7038, 7271 | 5866–5955, 7216 | none | 7245–7246 |
| `su->{type,obj,obj_type}` | `super_init_impl` 12950–12952 | `super_getattro` 12699, `super_repr` 12572–12580, `super_traverse` | none | — |

---

## 4. File-local macros and wrappers a tree-sitter scanner will not follow

This is the blind-spot inventory the brief asks for, with measured consequences.

### 4.1 Measured: what each scanner actually resolved on this file

| Scanner | Denominator | Reality | Verdict |
|---|---|---|---|
| `scan_lock_discipline` | `vocabulary_resolved=66`, `critical_section_functions=23` | Correctly resolves `BEGIN_TYPE_LOCK` / `END_TYPE_LOCK` / `BEGIN_TYPE_DICT_LOCK` / `END_TYPE_DICT_LOCK` as `critical_section` acquire/release aliases, and picks up `ASSERT_TYPE_LOCK_HELD` as a lock-held assertion. Counts: `BEGIN_TYPE_LOCK` 23, `END_TYPE_LOCK` 23, dict-lock 3+3, `PyMutex_Lock/Unlock` 3+3, raw CS 2+3+1+1. | **Working.** The `#define`-resolution fix landed and this file is its motivating example (the scanner's own source comments cite `BEGIN_TYPE_LOCK` by name). The 0 findings is a real 0 over a real denominator. |
| `scan_gil_usage` | `vocabulary_resolved=0`, `vocabulary_tokens_seen=4` | Its vocabulary is `Py_BEGIN_ALLOW_THREADS` / `Py_END_ALLOW_THREADS` / `PyGILState_Ensure` / `PyGILState_Release`, each with count **0**. `grep -c 'ALLOW_THREADS\|PyGILState_'` on this file returns **0**. | **Structural zero, not a blind spot.** This file never releases the GIL by the ALLOW_THREADS mechanism — it uses stop-the-world instead. The right conclusion is "N/A for this file", not "clean". Do not certify GIL discipline here from this scanner. |
| `scan_stw_safety` | `stw_functions=3` | Finds `types_stop_world` (123, classification `unknown`), `type_set_name` (1546, `unsafe`), `type_set_qualname` (1581, `unsafe`). It keys on the literal token `_PyEval_StopTheWorld` in a function body — which appears in exactly those three places. | **73% blind spot.** There are **11** stop-the-world regions (§2.1 table); the scanner sees 3. The nine wrapper-mediated ones — including `object_set_class` 7823 and `_PyType_SetFlagsRecursive` 6522 — are invisible. |

### 4.2 The wrappers that cause it

1. **`types_stop_world()` / `types_start_world()` (124 / 133).** A `static void` function in the
   FT build, an empty `#define` in the GIL build (193–194). Neither form is reachable by a
   token match on `_PyEval_StopTheWorld`, and `resolve_local_lock_macros`-style `#define`
   aliasing cannot help because the FT form is a *function*, not a macro. **This is the
   highest-impact single blind spot in the file.** Fix shape: teach `scan_stw_safety` to treat
   a one-line static wrapper whose body is a single `_PyEval_StopTheWorld(...)` call as an STW
   opener, or seed it from `local_lock_macros`-style resolution extended to trivial wrappers.
2. **`type_lock_prevent_release()` / `type_lock_allow_release()` (144 / 166).** Static functions
   that mutate `tstate->critical_section` in place, splicing `TYPE_LOCK` out of the current
   critical section's mutex list so a blocking wait cannot suspend it. No scanner models this.
   Any tool that reasons about "is `TYPE_LOCK` held here" gets the wrong answer between a
   `prevent` and its matching `allow`. Note the counts are **6 `prevent` vs 5 `allow`** by raw
   grep — but that is the definitions plus the GIL-build `#define`s; the real call sites are
   5 and 5, correctly paired (668/674, 1938/1943, 2004/2009, 3922/3926, 6521/6525).
3. **`ASSERT_TYPE_LOCK_HELD()` / `ASSERT_WORLD_STOPPED_OR_NEW_TYPE()` /
   `ASSERT_NEW_TYPE_OR_LOCKED()` (108/112/115).** Compile to nothing outside
   `Py_DEBUG`+FT+64-bit. A scanner that treats their presence as proof of a discharged
   obligation is trusting a no-op on three of four build configurations.
4. **`TYPE_IS_REVEALED(tp)` (102 / 104 / 189).** Three definitions, one of which is the constant
   `0`. Branch-reachability analysis over the 64-bit FT definition and over the other two give
   opposite answers.
5. **The `MCACHE_*` family (48–58).** `MCACHE_HASH_METHOD` (53) *contains* an
   `FT_ATOMIC_LOAD_UINT_RELAXED(type->tp_version_tag)`. A scanner looking for atomic reads of
   `tp_version_tag` will miss the one at 6308 because the atomic is inside the macro body, not
   at the call site.
6. **`NEXT_VERSION_TAG(interp)` (60).** Expands to a **modifiable lvalue** —
   `set_version_unlocked(type, NEXT_VERSION_TAG(interp)++)` at 1451 is a post-increment through
   a macro. Nothing looking for writes to `interp->types.next_version_tag` will see it.
7. **`PyTypeObject_CAST(op)` (200), `superobject_CAST(op)` (12543).** Plain casts; harmless, but
   they mean type-based reasoning on `self` fails one hop.
8. **`SLOT_UPDATE_CHUNK_SIZE` 30 (3822) vs `MAX_EQUIV` 10 (`pycore_interp_structs.h`:741).** A
   cross-file constant relationship asserted only at 6730.
9. **`CHECK(expr)` (868, `#undef` 896).** A locally scoped assert macro inside
   `_PyType_CheckConsistency`; nine uses. Same name is a common macro elsewhere in the tree.

### 4.3 Scanner recall gaps found by reading (net-new, for the toolkit assessment)

- **`scan_refcounts`'s `borrowed_field_deref_across_call` is keyed on the four `lookup_tp_*`
  accessors** (`borrowed_field_accessors=4`). It therefore cannot see the same bug shape when
  the borrowed pointer is a plain struct-field read. Two concrete misses in pass-2 regions:
  `super_getattro` 12699 (`su->type` / `su->obj` / `su->obj_type` across a descriptor call that
  can re-enter `super_init_impl`'s `Py_XSETREF` at 12950–12952), and
  `_Py_type_getattro_stackref` 6574 (`metatype = Py_TYPE(type)` used at 6610 and 6660 after
  descriptor calls). Proposed widening: treat `X->field` where `field` is a `PyObject*`/
  `PyTypeObject*` member of a type with a known in-file `Py_XSETREF`/`Py_SETREF` writer the
  same way as an accessor return.
- **`scan_stw_safety` cannot cross a function boundary from the STW opener.** Even if it learned
  `types_stop_world`, `object_set_class` (7800) opens the region and the risky work happens one
  level down in `object_set_class_world_stopped` (7700). The scanner's intra-file call graph
  exists for the *callee* direction; it needs to also expand the region *across* a call made
  between stop and start.

---

## 5. Where the 18 NET-NEW candidates land relative to these regions

Four of the eighteen fall inside pass-2 territory and should be triaged with this document open:

| line | function | rule | region | discriminator to apply |
|---|---|---|---|---|
| :793 | `_PyType_GetSubclasses` | `borrowed_field_deref_across_call` | R4 | Does `PyList_Append`'s GC → `__del__` → `remove_subclass` path invalidate the `PyDict_Next` cursor? The comment at 788–789 claims it cannot. |
| :3443 | `mro_implementation_unlocked` | `borrowed_field_deref_across_call` | R11 | `bases` is borrowed across `class_name` (re-entrant per its own comment 3262). `TYPE_LOCK` blocks another *thread*; it does not block a re-entrant `T.__bases__ = …` on this thread. |
| :6002 | `get_base_by_token_recursive` | `self_recursion` | R18 | Unguarded recursion over `lookup_tp_bases`. Same shape as CPY-0087 but travelling up the base graph instead of down the subclass tree. |
| :6620 | `_Py_type_getattro_stackref` | `publish_before_init_complete` (low) | R21 | Three `_PyCStackRef`s with six `goto done` exits; check each exit leaves all three consistent. |

The other fourteen are pass-1 territory (`type_new_set_attrs`, `type_from_slots_or_spec`,
`type_add_method/members/getset`, `overrides_hash`, `type_dict_set_doc`, `type_ready_*`,
`add_tp_new_wrapper`, `add_operators`) and are not covered by this document.

---

## 6. Recommended reading order for downstream pass-2 agents

1. **R2 (42–227)** — mandatory for anyone touching concurrency. 185 lines.
2. **R4 (524–810)** — mandatory for the refcount agent; the four-accessor discriminator table
   in §1/R4 is the whole triage.
3. Then the agent's own region.

Cross-region pairs that must be read together:

- **R25 ↔ R26** — the `__class__`-lying segfault (R26's comment 8318–8332) is a joint bug.
- **R25 ↔ R21** — `_Py_type_getattro_stackref`'s `metatype = Py_TYPE(type)` (6574) is only safe
  if R25's `Py_SET_TYPE` (7790) cannot land in between.
- **R6 ↔ R19** — `type_mro_modified` (1279) calls into the lookup cache via `has_custom_mro`
  (1261) with `TYPE_LOCK` held; the cache's `BEGIN_TYPE_LOCK` at 6358 re-enters it.
- **R11 ↔ R6** — `mro_internal` calls `type_mro_modified` twice (3680, 3683) and
  `_PyType_Modified_Unlocked` once (3687), in that order, with a documented reason (3681–3682).
- **R20 ↔ R6** — the invalidate-before-stop ordering (6509–6513) exists because of R6's
  "not safe with the world stopped" assertion (1185–1188).
