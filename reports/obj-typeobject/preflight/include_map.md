# Include Graph Analysis — Objects/typeobject.c (Phase 1 preflight)

CPython main @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0).
Slice: `/home/danzin/projects/cpython/Objects/typeobject.c` — 13,068 lines, 30 `#include` directives.
Companion generated file: `/home/danzin/projects/cpython/Objects/clinic/typeobject.c.h` — 265 lines.

**Scope note:** this is a structural orientation document. No code was audited.

---

## 1. What typeobject.c includes

### API tiers (this file only)

| Tier | Headers | Notes |
|------|---------|-------|
| Public (`Include/*.h`) | 2 | `Python.h`, `opcode.h` |
| CPython (`Include/cpython/*.h`) | 0 | reached transitively via `Python.h` |
| Internal (`Include/internal/pycore_*.h`) | 26 | see below |
| Generated | 1 | `clinic/typeobject.c.h` |
| System | 1 | `<stddef.h>` |

Entitled to the internal tier: `Objects/` is core build. No tier violation.

### The 26 internal headers (line 4–29)

`pycore_abstract.h`, `pycore_call.h`, `pycore_code.h`, `pycore_descrobject.h`, `pycore_dict.h`,
`pycore_function.h`, `pycore_interpframe.h`, `pycore_lock.h`, `pycore_long.h`,
`pycore_memoryobject.h`, `pycore_modsupport.h`, `pycore_moduleobject.h`, `pycore_object.h`,
`pycore_object_alloc.h`, `pycore_pyatomic_ft_wrappers.h`, `pycore_pyerrors.h`, `pycore_pystate.h`,
`pycore_slots.h`, `pycore_symtable.h`, `pycore_tuple.h`, `pycore_typeobject.h`,
`pycore_unicodeobject.h`, `pycore_unionobject.h`, `pycore_weakref.h`, `pycore_cell.h`,
`pycore_stats.h`.

All 26 resolve. All are included by bare name (CPython's convention — do not key triage on
directive text). Nothing is unresolved; nothing is platform-conditional.

### Unusual / noteworthy includes

| Include | Why it is here | Why it matters for Phase 2 |
|---------|----------------|----------------------------|
| `opcode.h` (line 30) | `MAKE_CELL` — consumed by `super_init_without_args` (12814) | An `Objects/` file reaching into the **bytecode** layer. Only `frameobject.c` and `codeobject.c` do the same. Zero-arg `super()` reads the caller's frame and pattern-matches on opcodes; this is the file's most fragile cross-layer coupling. |
| `pycore_interpframe.h` (line 10) | `_PyInterpreterFrame` — same zero-arg `super()` path | Same coupling. Eight `Objects/` files include it. |
| `pycore_slots.h` (line 21) | `_PySlotIterator_Init` — backs the **new 3.16 `PyType_FromSlots(PySlot *)` API** (`Include/object.h:368`) | Only 4 files in the tree include it: `typeobject.c`, `Objects/moduleobject.c`, `Python/slots.c`, `Python/slots_generated.c`. **The slot-description machinery is now split across two files.** A bug in slot iteration/validation has its sibling in `Python/slots.c` (404 lines) — not in `Objects/`. |
| `pycore_memoryobject.h` (line 13) | `_PyMemoryView_FromBufferProc` in `slot_bf_getbuffer` (11319) | typeobject.c manufactures memoryviews for the buffer-protocol slot path. |
| `pycore_lock.h` + `pycore_pyatomic_ft_wrappers.h` | `_PySeqLock_*`, FT atomics | The whole file is free-threading-aware; see §3 regions 1, 13. |
| `pycore_cell.h`, `pycore_stats.h` (28–29) | classcell; stats | **Out of alphabetical order** — lines 4–27 are sorted, 28–29 are appended. POLICY-tier nit only. |

Notably *absent*: no `pycore_ceval.h`, no `pycore_gc.h` despite heavy GC-slot work (both arrive
transitively through `pycore_object.h`).

Tree-wide `cycles`: exactly one, `pycore_structs.h ↔ pycore_context.h` — **not** on this file's
include path. Not relevant to this slice.

---

## 2. Who reaches into typeobject.c's internals

`Objects/typeobject.c` is **never `#include`d** by anything (`reverse_graph` confirms). Its only
generated dependency is one-way: it includes `clinic/typeobject.c.h`.

The coupling is via non-static symbols declared in `Include/internal/pycore_typeobject.h`
(26 includers tree-wide) and `pycore_object.h`. **These are where a sibling of a bug found here
would live:**

| Exported symbol (def. line) | External consumers |
|---|---|
| `_Py_slot_tp_getattro` (10959), `_Py_slot_tp_getattr_hook` (10991) | `Python/specialize.c` |
| `_PyType_Validate` (6465), `_PyType_CacheGetItemForSpecialization` (6428), `_PyType_CacheInitForSpecialization` (6408) | `Python/specialize.c` |
| `_PyType_LookupByVersion` (1344) | `Python/optimizer_symbols.c` |
| `_PyType_HasSlotTpIternext` (11125) | `Python/optimizer_bytecodes.c`, `Python/optimizer_cases.c.h` |
| `_PySuper_Lookup` (12769) | `Python/bytecodes.c` → `generated_cases.c.h`, `executor_cases.c.h`; `Modules/_testinternalcapi/testbytecodes.c` |
| `_PySuper_LookupDescr` (12588) | `Python/optimizer_analysis.c` |
| `_PyType_InitSlotDefs` (12185) | `Python/pylifecycle.c` |
| `_PyType_GetSlotWrapperNames` (12256) | `Modules/_testinternalcapi.c` |
| `_PyType_AllocNoTrack` (2512) | `Objects/setobject.c`, `Objects/dictobject.c`, `Objects/moduleobject.c`, `Modules/arraymodule.c` |
| `_PyType_NewManagedObject` (2497) | via `pycore_object.h` |
| `_PyObject_LookupSpecial` (2950) | 10 files: `Objects/{object,dictobject,bytesobject,complexobject,weakrefobject,enumobject,abstract}.c`, `Python/sysmodule.c`, `Modules/{mathmodule,posixmodule}.c` |
| `_PyObject_MaybeCallSpecialNoArgs` (3206) | `Python/bltinmodule.c`, `Modules/mathmodule.c` |
| `_PyType_SetFlagsRecursive` (6506) | `Modules/_abc.c` |
| `_PyType_AddMethod` (8668) | `Modules/posixmodule.c` |
| `_PyBufferWrapper_Type` (11306) | `Objects/object.c` |
| `_PyObject_SetDict` (4012) | `Objects/object.c` |

**Sibling-hunt guidance for Phase 2:**

- The slot-wiring engine (`pytype_slotdef`, `update_one_slot`, `fixup_slot_dispatchers`) exists
  **only** in `typeobject.c` — grep confirms zero other definitions. A slot-wiring bug has no
  in-tree structural twin; its siblings are *other rows of the `slotdefs` table*.
- The **new** `PySlot`-descriptor path is the exception: `Python/slots.c` + `Python/slots_generated.c`
  + `Objects/moduleobject.c` share `pycore_slots.h`. A `PyType_FromSlots` bug plausibly mirrors
  into module-slot handling in `moduleobject.c`.
- For `tp_new`/`__new__`-bypass shapes, the consumer-side siblings are types that set
  `tp_new = PyType_GenericNew` — in `Objects/`: `listobject.c`, `descrobject.c`, `bytearrayobject.c`.
- For specialization-cache invalidation shapes, `Python/specialize.c` is the mirror.

---

## 3. Internal section map (the load-bearing part)

Regions are contiguous; the line is the first line of the region's leading comment or first
definition. **Bold = Pass 1 territory (slot wiring / tp_new / tp_init / `__new__` bypass).**

| # | Lines | Region | Landmarks |
|---|-------|--------|-----------|
| 1 | 1–41 | Includes, clinic class decls | `clinic/typeobject.c.h` at 40 |
| 2 | 42–227 | Method-cache preamble + **FT world-stop / type-lock helpers** | `types_world_is_stopped` 90, `types_stop_world` 124, `types_start_world` 133, `type_lock_prevent_release` 144, `type_lock_allow_release` 166; fwd decls of `slot_bf_releasebuffer`/`slot_tp_new`/`slot_tp_setattro`/`slot_tp_call` at 203–215 |
| 3 | 228–522 | **Managed static types** (per-interpreter state) | `managed_static_type_index_*` 232–259, `_PyStaticType_GetState` 302, `..._state_init` 310, `..._state_clear` 382, `_PyStaticType_GetBuiltins` 429; `type_set_flags*` 455–520 |
| 4 | 524–810 | Accessors for `tp_dict` / `tp_bases` / `tp_mro` / `tp_subclasses` | `lookup/set/clear_tp_*`; `_PyType_GetSubclasses` 776 |
| 5 | 812–970 | Docstring & `__text_signature__` helpers; `_PyType_CheckConsistency` 866 | |
| 6 | 971–1481 | **Type cache, watchers, version tags** | `get_type_cache` 973, `_PyType_InitCache` 999, `_PyType_ClearCache` 1016, `PyType_AddWatcher` 1054, `PyType_Watch` 1098, `set_version_unlocked` 1134, `_PyType_Modified_Unlocked` 1166, `PyType_Modified` 1243, `type_mro_modified` 1279, `_PyType_SetVersion` 1336, `_PyType_LookupByVersion` 1344, `assign_version_tag` 1408 |
| 7 | 1483–2341 | `type` getsets (`__name__`/`__qualname__`/`__module__`/`__bases__`/`__mro__`/`__dict__`/`__doc__`/`__annotations__`/`__type_params__`) | `type_set_name` 1547, `mro_hierarchy_for_complete_type` 1798, `type_check_new_bases` 1866, `type_set_bases_unlocked` 1929, `type_set_bases` 2024, `type_set_annotate` 2129 |
| 8 | 2343–2575 | instancecheck/subclasscheck, `type_repr`, **`type_call` 2431**, allocation | `_PyType_NewManagedObject` 2497, `_PyType_AllocNoTrack` 2512, `PyType_GenericAlloc` 2557, **`PyType_GenericNew` 2571** |
| 9 | 2576–2886 | Subtyping GC/dealloc helpers | `traverse_slots` 2585, `subtype_traverse` 2607, `clear_slots` 2660, `subtype_clear` 2680, `subtype_dealloc` 2719 |
| 10 | 2889–3216 | `PyType_IsSubtype` + special-method lookup helpers | `PyType_IsSubtype` 2929, `_PyObject_LookupSpecial` 2950, `lookup_method_ex` 2998, `vectorcall_method` 3116, `maybe_call_special_*` 3164–3213 |
| 11 | 3217–3702 | **MRO (C3 linearization)** | `tail_contains` 3246, `check_duplicates` 3270, `set_mro_error` 3310, `pmerge` 3361, `mro_implementation_unlocked` 3431, `type_mro_impl` 3535, `mro_check` 3547, `mro_invoke` 3591, `mro_internal` 3655 |
| 12 | 3703–3834 | Base selection | `find_best_base` 3707, `shape_differs` 3762, `solid_base` 3771 |
| 13 | **3836–3941** | **Deferred slot-update queue (FT)** | `slot_update_new_chunk` 3836, `slot_update_free_chunks` 3849, `queue_slot_update` 3860, `apply_slot_updates` 3884, `apply_type_slot_updates` 3902 |
| 14 | 3951–4150 | `subtype_dict` / `subtype_setdict` / `subtype_getweakref`; `type_init` 4115 | `_PyObject_SetDict` 4012 |
| 15 | 4151–4190 | `_PyType_CalculateMetaclass` 4153 | |
| 16 | **4191–5135** | **`type_new` subsystem — the `class` statement path** | `type_new_ctx` 4207, `visit_slots` 4212, `copy_slots` 4250, `slots_bases` 4319, `slots_impl` 4358, `alloc` 4407, `set_name` 4457, `set_module` 4475, `set_ht_name` 4503, `set_doc` 4534, `staticmethod` 4569, `classmethod` 4596, `add_common_descriptor` 4624, `descriptors` 4651, `set_slots` 4731, `set_classcell` 4750, `set_classdictcell` 4776, `set_attrs` 4802, `get_slots` 4867, `init` 4898, **`type_new_impl` 4941** (calls `fixup_slot_dispatchers` at **4958**), `get_bases` 4994, **`type_new` 5065**, `type_vectorcall` 5120 |
| 17 | **5136–5832** | **`PyType_FromSpec` / `FromMetaclass` / `FromSlots` — the C-API type-creation path** | `_align_up` 5140, `check_basicsize_includes_size_and_offsets` 5146, `check_immutable_bases` 5186, `special_offset_from_member` 5215, **`type_from_slots_or_spec` 5248–5801 (553 lines, the file's largest function)**, `PyType_FromSlots` 5802 (new 3.16), `FromMetaclass` 5808, `FromModuleAndSpec` 5816, `FromSpecWithBases` 5822, `FromSpec` 5828 |
| 18 | 5834–6139 | `PyType_Get*` accessors, module-by-token/def, type data | `PyType_GetModuleByToken` 5967, `PyType_GetBaseByToken` 6067, `PyObject_GetTypeData` 6099 |
| 19 | 6140–6452 | **MRO name lookup + method cache read/write** | `find_name_in_mro` 6145, `is_dunder_name` 6200, `update_cache` 6216, `update_cache_gil_disabled` 6237, `_PyTypes_AfterFork` 6262, `_PyType_LookupRefAndVersion` 6284, `_PyType_LookupStackRefAndVersion` 6306, `_PyType_Lookup` 6400, specialization caches 6408–6452 |
| 20 | 6453–6528 | `_PyType_SetFlags` 6453, `_PyType_Validate` 6465, `_PyType_SetFlagsRecursive` 6506 | |
| 21 | 6529–6848 | **`type` getattro/setattro** | `_Py_type_getattro_impl` 6542, `_Py_type_getattro` 6554, `_Py_type_getattro_stackref` 6571, `type_update_dict` 6694, `update_slot_after_setattr` 6726, **`type_setattro` 6750** |
| 22 | 6849–7054 | Type dealloc + static-type finalization | `type_dealloc_common` 6849, `clear_static_tp_subclasses` 6861, `fini_static_type` 6923, `_PyTypes_FiniExtTypes` 6946, `type_dealloc` 6978 |
| 23 | 7055–7337 | `type` methods, traverse/clear/is_gc, `PyType_Type` | `type_prepare` 7062, `merge_class_dict` 7078, `type_traverse` 7198, `type_clear` 7229, `PyType_Type` **7290–7337** |
| 24 | **7338–7520** | **`object.__new__` / `object.__init__` — the canonical excess-args / bypass rules** | `excess_args` 7379, **`object_init` 7386**, **`object_new` 7406**, `object_dealloc` 7476 |
| 25 | 7482–7846 | `object` repr/str/richcompare, **`__class__` assignment** | `object_richcompare` 7521, `compatible_with_tp_base` 7577, `same_slots_added` 7588, `compatible_for_assignment` 7628, `object_set_class_world_stopped` 7700, `object_set_class` 7800 |
| 26 | 7848–8406 | Pickle support (`__reduce_ex__`, `__getstate__`) | `_PyType_GetSlotNames` 7874, `object_getstate_default` 7923, `_PyObject_GetNewArguments` 8090, `reduce_newobj` 8222, `_common_reduce` 8334 |
| 27 | 8408–8600 | Remaining `object` methods; `PyBaseObject_Type` **8557–8600** | `object___format___impl` 8445, `object___dir___impl` 8493 |
| 28 | 8601–9016 | Method/member/getset installation + **inheritance of slots** | `type_add_method` 8601, `type_add_methods` 8676, `type_add_members` 8693, `type_add_getset` 8717, `inherit_special` 8742, `overrides_hash` 8809, **`inherit_slots` 8822–9015** |
| 29 | **9017–9691** | **`PyType_Ready` pipeline** | `type_ready_pre_checks` 9023, `set_base` 9053, `set_type` 9082, `set_bases` 9100, `set_dict` 9129, `type_dict_set_doc` 9147, `fill_dict` 9182, `preheader` 9204, `mro` 9246, `inherit_as_structs` 9294, `inherit_patma_flags` 9314, `inherit` 9321, `set_hash` 9373, `add_subclasses` 9398, **`type_ready_set_new` 9415**, `managed_dict` 9466, `post_checks` 9493, `type_ready` 9528, `PyType_Ready` 9611, `init_static_type` 9640, `_PyStaticType_InitBuiltin` 9686 |
| 30 | 9693–9811 | Subclass-list bookkeeping | `add_subclass` 9693, `add_all_subclasses` 9726, `remove_subclass` 9773, `remove_all_subclasses` 9796 |
| 31 | **9812–10519** | **Slot wrapper functions (`wrap_*`) — C slot → Python callable** | `check_num_args` 9812, `check_pow_args` 9828, `wrap_lenfunc` 9857 … `wrap_objobjargproc` 10087, **`hackcheck_unlocked` 10122 / `hackcheck` 10170** (the `object.__setattr__`-applied-to-a-type guard), `wrap_setattr` 10184, `wrap_hashfunc` 10219, `wrap_call` 10233, `wrap_del` 10241, RICHCMP wrappers 10253–10277, `wrap_descr_*` 10294–10345, `wrap_buffer` 10346, `wrap_releasebuffer` 10368, **`wrap_init` 10403**, **`tp_new_wrapper` 10413** (the `__new__`-bypass safety check), `tp_new_methoddef` 10491, `add_tp_new_wrapper` 10500 |
| 32 | **10521–11533** | **`slot_*` dispatchers — Python dunder → C slot** | SLOT0/SLOT1/SLOT1BIN(FULL) macros 10521–10618, `method_is_overloaded` 10543, `slot_sq_*` 10620–10714, `slot_mp_*` 10715–10738, `slot_nb_*` 10739–10886, `slot_tp_repr` 10888, `slot_tp_hash` 10904, `slot_tp_call` 10942, `_Py_slot_tp_getattro` 10959, `call_attribute` 10966, `_Py_slot_tp_getattr_hook` 10991, `slot_tp_setattro` 11042, `slot_tp_richcompare` 11072, `has_dunder_getitem` 11085, `slot_tp_iter` 11097, `slot_tp_iternext` 11118, `slot_tp_descr_get` 11131, `slot_tp_descr_set` 11159, **`slot_tp_init` 11180**, **`slot_tp_new` 11197**, `slot_tp_finalize` 11217, bufferwrapper type 11255–11318, `slot_bf_getbuffer` 11319, `releasebuffer_maybe_call_super*` 11365–11416, `releasebuffer_call_python` 11417, `slot_bf_releasebuffer` 11487, `slot_am_*` 11494–11533 |
| 33 | **11535–11782** | **The `slotdefs[]` table** | slot-def macros `TPSLOT`/`FLSLOT`/`ETSLOT`/`AMSLOT`/`SQSLOT`/`MPSLOT`/`NBSLOT`/`UNSLOT`/`IBSLOT`/`BINSLOT`/`RBINSLOT`(+`NOTINFIX`) 11547–11584; **`static pytype_slotdef slotdefs[]` 11585–11782** |
| 34 | **11784–12272** | **The slot-wiring engine** | `slotdefs_dups` 11787, `slotptr` 11795, `has_slotdef` 11834, long design comment 11844–11906, **`update_one_slot` 11907–12069**, `update_slots_callback` 12073, `update_slot` 12089, **`fixup_slot_dispatchers` 12132**, `update_all_slots` 12144 (FT) / 12172 (GIL), `_PyType_InitSlotDefs` 12185, `_PyType_GetSlotWrapperNames` 12256 |
| 35 | 12273–12406 | `__set_name__` / `__init_subclass__` / subclass recursion | `type_new_set_names` 12276, `type_new_init_subclass` 12322, `update_subclasses` 12353, `recurse_down_subclasses` 12363 |
| 36 | **12407–12533** | **Slot inheritance + operator installation; `PyType_Freeze`** | `slot_inherited` 12407, **`add_operators` 12457**, `PyType_Freeze` 12506 |
| 37 | 12534–13068 | **`super`** | `super_members` 12545, `super_dealloc` 12556, `super_repr` 12568, `_PySuper_LookupDescr` 12588, `do_super_lookup` 12637, `super_getattro` 12688, `supercheck` 12703, `_PySuper_Lookup` 12769, `super_descr_get` 12781, **`super_init_without_args` 12814** (frame/opcode introspection), `super_init` 12898, `super_init_impl` 12914, `super_traverse` 12973, `super_vectorcall` 12985, `PySuper_Type` 13025–13068 |

### Pass 1 line budget (slot wiring / tp_new / tp_init / `__new__` bypass)

Contiguous ranges to hand the Phase 2 agents, ~3,400 lines total (26% of the file):

```
3836–3941    deferred slot-update queue (FT)
4191–5135    type_new subsystem (class statement)
5136–5832    PyType_FromSpec / FromMetaclass / FromSlots (C-API creation)
7338–7520    object_new / object_init  (excess-args + bypass rules)
8742–9015    inherit_special / inherit_slots
9017–9691    PyType_Ready pipeline (esp. type_ready_set_new 9415–9465)
10117–10218  hackcheck (object.__setattr__ applied to a type)
10403–10519  wrap_init, tp_new_wrapper, add_tp_new_wrapper
11180–11216  slot_tp_init, slot_tp_new
11535–11782  slotdefs[] table
11784–12272  update_one_slot / fixup_slot_dispatchers / update_all_slots
12407–12533  slot_inherited / add_operators / PyType_Freeze
```

Pass 2 residue: regions 2–12 (locks, static types, accessors, cache/watchers/versions, getsets,
MRO), 19–27 (lookup cache, getattro/setattro, dealloc, `__class__` assignment, pickle), 35, 37 (super).

---

## 4. Generated / clinic-derived code

`Objects/clinic/typeobject.c.h` (265 lines) is included at **line 40** and covers **12 methods**:

| Class | Clinic-generated methods |
|-------|--------------------------|
| `type` | `__instancecheck__`, `__subclasscheck__`, `mro`, `__subclasses__`, `__dir__`, `__sizeof__` |
| `object` | `__getstate__`, `__reduce__`, `__reduce_ex__`, `__format__`, `__sizeof__`, `__dir__` |

Corresponding `_impl` bodies in typeobject.c: 2353, 2369, 3535, 7055, 7141, 7161, 8083, 8358,
8374, 8445, 8466, 8493. The `[clinic input]` blocks live at 2343, 2359, 3528, 7048, 7134, 7154,
8076, 8351, 8364, 8433, 8459, 8486.

**The critical negative result for Pass 1:** clinic covers **none** of the object-construction
surface. `type_new` (5065), `type_call` (2431), `type_init` (4115), `object_new` (7406),
`object_init` (7386), `type_vectorcall` (5120), `tp_new_wrapper` (10413), `type_prepare` (7062)
and `super_init` (12898) are **all hand-written argument parsing**. Every argument-count,
keyword, and type check on the `tp_new`/`tp_init` path is hand-rolled — treat those as
first-class review targets, not as clinic-guarded boilerplate.

No other generated code is present in the file. `Python/slots_generated.c` (119 lines) is
generated but belongs to the sibling `Python/slots.c` unit, not this one.

---

## Architecture assessment

**Strengths.** Include hygiene is clean: 26 well-annotated internal headers, every directive
resolves, no tier violation, no cycle on this file's path, and only one file in the tree
(`typeobject.c` itself) owns the slot-wiring engine — so the blast radius of a slot bug is
concentrated rather than scattered.

**Concerns.** (a) Two cross-layer reaches — `opcode.h` and `pycore_interpframe.h` — exist purely
to make zero-arg `super()` work by inspecting the caller's frame; that is the file's most
brittle external coupling. (b) `type_from_slots_or_spec` (5248–5801) is a 553-line function, the
largest in the file, and now carries three entry points including the brand-new
`PyType_FromSlots`. (c) The slot-description machinery is newly split between `typeobject.c` and
`Python/slots.c` via `pycore_slots.h` — a genuinely new seam in 3.16 worth checking for
asymmetry between the type and module paths.

**Recommendation for Phase 2.** Pass 1 should treat the twelve ranges above as the working set,
and should treat the four external mirrors — `Python/specialize.c` (cache/version invalidation),
`Python/slots.c` + `Objects/moduleobject.c` (`PySlot` iteration), `Python/optimizer_*.c`
(`_PyType_HasSlotTpIternext`, `_PySuper_LookupDescr`), and `Objects/{list,descr,bytearray}object.c`
(`PyType_GenericNew` consumers) — as the sibling-hunt targets for anything it confirms.
