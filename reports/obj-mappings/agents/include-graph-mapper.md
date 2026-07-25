# include-graph-mapper — obj-mappings preflight orientation

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e`
(`4f3be1b5777 gh-150452: use PyMutex in socket module (#150453)`)
**Scope:** exactly `Objects/dictobject.c` (8,597 lines) + `Objects/setobject.c` (3,228 lines)

> **Read this before you read the code.** Section 3 (macros that can run arbitrary
> Python) and Section 6 (dict/set divergences) are the two that will change your
> triage. Section 2 is the reading-division map.

---

## 0. Denominator

| measure | value |
|---|---|
| Files in scope | 2 |
| Lines read directly for this map | ~1,900 of 11,825 (targeted regions: both file preambles, both lookup families, both insert/resize families, both iterator families, both hash implementations, all 7 `LOCK_KEYS` sites, all 4 clinic-locking sites sampled) |
| Function definitions enumerated (tree-sitter, whole file) | **292** in `dictobject.c`, **112** in `setobject.c` — full lists at `/tmp/.../dict_funcs.txt`, `/tmp/.../set_funcs.txt`; the region tables in §2 are derived from them, so their coverage is 100% of both files, not a sample |
| `#include` directives resolved | 19 (dict) + 13 (set) = 32; **0 unresolved** |
| Headers reachable, first level | 16 distinct (see §1) |
| Cycles involving either file | 0 (both are `.c` leaves; the tree's only cycle is `pycore_structs.h` ↔ `pycore_context.h`, neither of which is in this slice's include closure) |

The include graph here is trivially small and clean, exactly as the task anticipated.
**Everything below §1 is where the value is.**

---

## 1. Header surface

### 1.1 Directives (all resolved; tier per `analyze_includes.py`)

| header | tier | dict | set | why it matters here |
|---|---|:--:|:--:|---|
| `Include/Python.h` | public | ● | ● | mega-include; do **not** read include fan-in for public headers (it scores ~1 for everything) |
| `pycore_dict.h` | internal | ● | ● | `DK_*`, `DKIX_*`, `DictKeysKind`, `_PyDict_HasSplitTable`, `_PyDict_NotifyEvent`, `PyFrozenDictObject`. **set includes it too** (for `_PyDict_Contains_KnownHash`) |
| `pycore_setobject.h` | internal | ● | ● | **exports only — zero macros** (44 lines). dict includes it for `_PySet_NextEntry()` |
| `pycore_critical_section.h` | internal | ● | ● | `Py_BEGIN/END_CRITICAL_SECTION[2][_MUTEX]`, `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` |
| `pycore_pyatomic_ft_wrappers.h` | internal | ● | ● | `FT_ATOMIC_LOAD_*` / `FT_ATOMIC_STORE_*` |
| `pycore_object.h` | internal | ● | ● | `_PyObject_GC_TRACK/UNTRACK`, `_PyObject_GC_IS_SHARED`, `_Py_IsOwnedByCurrentThread` |
| `pycore_pyerrors.h` | internal | ● | ● | `_PyErr_GetRaisedException`, `_PyErr_SetKeyError` |
| `pycore_ceval.h` | internal | ● | ● | `_PyEval_GetBuiltin` |
| `stringlib/eq.h` | other-local | ● | ● | `unicode_eq()` — the only shared *code* include |
| `clinic/<file>.c.h` | generated | ● | ● | **see §3.4 — this is where set's locking lives** |
| `pycore_bitutils.h` `pycore_call.h` `pycore_code.h` `pycore_freelist.h` `pycore_gc.h` `pycore_pystate.h` `pycore_tuple.h` `pycore_unicodeobject.h` | internal | ● | | dict only |
| `pycore_modsupport.h` `pycore_weakref.h` | internal | | ● | set only (`_PyArg_NoKwnames`, `FT_CLEAR_WEAKREFS`) |
| `stdbool.h` / `stddef.h` | system | ●/ | /● | |

Both files are core-build translation units in `Objects/`, so their use of
`Include/internal/` is **entitled, not a tier violation**. There is nothing to
report on the tier axis for this slice.

Blast-radius context (tree-wide reverse-graph counts, for judging whether a
header-level change is in play): `pycore_object.h` 106 includers,
`pycore_critical_section.h` 64, `pycore_dict.h` 32,
`pycore_pyatomic_ft_wrappers.h` 32, `pycore_setobject.h` 21.

### 1.2 Load-bearing macros, by where they live

**Asymmetry #0, and it shapes everything else:** dict's data model is declared in a
460-line internal header that other subsystems consume; set's data model is declared
in `Include/cpython/setobject.h` (**CPython tier, 71 lines — `setentry` and
`PySetObject` are public struct layout**) and `pycore_setobject.h` carries *no macros
at all*. Consequence: **every set-internal macro is file-local to `setobject.c` and
can be changed freely; every dict-internal macro is header-level with 32 includers.**

#### From `Include/internal/pycore_dict.h` (dict only)

| macro / inline | line | correctness role |
|---|---|---|
| `_PyDict_HasSplitTable(d)` | 56 | `((d)->ma_values != NULL)` — **the** split/combined discriminator. Read it as "values live in `ma_values`, keys are shared and refcounted" |
| `DKIX_EMPTY -1`, `DKIX_DUMMY -2`, `DKIX_ERROR -3`, `DKIX_KEY_CHANGED -4` | 185-188 | `DKIX_ERROR` = a comparison raised; `DKIX_KEY_CHANGED` = **the dict mutated under us, restart** (the re-entry signal) |
| `DICT_KEYS_GENERAL/UNICODE/SPLIT` | 190-194 | `DK_IS_UNICODE(dk)` is `dk_kind != GENERAL`, so **SPLIT counts as unicode**. Misreading this is the classic bug in this file |
| `DK_LOG_SIZE`, `DK_SIZE` | 272-277 | table size; `DK_MASK` is `DK_SIZE-1` |
| `DK_ENTRIES(dk)` / `DK_UNICODE_ENTRIES(dk)` | 285-292 | **asserting accessors**: `DK_ENTRIES` asserts `dk_kind == GENERAL`, `DK_UNICODE_ENTRIES` asserts `dk_kind != GENERAL`. Entry stride differs (`PyDictKeyEntry` has `me_hash`, `PyDictUnicodeEntry` does not) |
| `_PyDict_NotifyEvent(...)` | 317-329 | **runs arbitrary Python — see §3** |
| `shared_keys_usable_size` | 360-373 | documents the `dk_nentries`-then-`dk_usable` ordering contract that `split_keys_entry_added` implements |
| `SHARED_KEYS_MAX_SIZE 30` / `NEXT_LOG2_SHARED_KEYS_MAX_SIZE 6` | 255-256 | split-table cap; `insert_split_key` asserts `ix < SHARED_KEYS_MAX_SIZE` |
| `_Py_INCREF_DICT` / `_Py_DECREF_DICT` | 395-446 | FT per-thread refcounting keyed on `_PyDict_UniqueId` |

#### File-local, `dictobject.c` lines 163-336 — the FT macro layer

This block is `#ifdef Py_GIL_DISABLED` / `#else`, and the `#else` arm makes **almost
every one of these a no-op**. When you read a dict function, mentally instantiate it
twice.

| macro | FT arm | GIL arm | notes |
|---|---|---|---|
| `ASSERT_DICT_LOCKED(op)` | `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` | **empty** | 23 uses. See §3.5 — this assert is *also* silently skipped when `Py_REFCNT(op)==1` |
| `ASSERT_WORLD_STOPPED_OR_DICT_LOCKED` / `..._OR_OBJ_LOCKED` | as named | empty | 4 uses: 700, 7360, 7965, 8033 |
| `LOCK_KEYS(keys)` / `UNLOCK_KEYS(keys)` | `PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)` / `PyMutex_Unlock` | **empty** | **not a critical section** — see §3.3 |
| `LOCK_KEYS_IF_SPLIT(keys, kind)` / `UNLOCK_KEYS_IF_SPLIT` | conditional on `kind == DICT_KEYS_SPLIT` | empty | |
| `ASSERT_KEYS_LOCKED(keys)` | `assert(PyMutex_IsLocked(&keys->dk_mutex))` | empty | 1 use (`split_keys_entry_added`) |
| `IS_DICT_SHARED` / `SET_DICT_SHARED` | `_PyObject_GC_IS_SHARED` / `_SET_SHARED` | `(false)` / empty | gates QSBR-delayed frees |
| `LOAD_INDEX` / `STORE_INDEX` | `_Py_atomic_load/store_int##size##_relaxed` | plain array access | |
| `LOAD_SHARED_KEY` / `STORE_SHARED_KEY` | acquire / release | plain | for split-table `me_key` |
| `INCREF_KEYS` / `DECREF_KEYS` | `_Py_atomic_add_ssize(&dk->dk_refcnt, ±1)` | `dk_refcnt++/--` | returns **previous** value |
| `INCREF_KEYS_FT(dk)` / `DECREF_KEYS_FT(dk, shared)` | `dictkeys_incref/decref` | **empty** | note the FT arm calls the real refcount fn while the GIL arm does nothing |
| `set_keys` / `set_values` | `_Py_atomic_store_ptr_release` + `ASSERT_OWNED_OR_SHARED` | plain store | |
| `split_keys_entry_added` | asserts keys locked; `nentries+1` **then** `usable-1` | `usable--; nentries++` (**opposite order**) | the FT ordering is load-bearing (see `shared_keys_usable_size`) |
| `GET_USED` / `STORE_USED` | `FT_ATOMIC_*_SSIZE_RELAXED(ma_used)` | plain | |
| `STORE_KEY` / `STORE_VALUE` / `STORE_SPLIT_VALUE` | `FT_ATOMIC_STORE_PTR_RELEASE` | plain | |
| `STORE_HASH` / `STORE_KEYS_USABLE` / `STORE_KEYS_NENTRIES` | `FT_ATOMIC_STORE_SSIZE_RELAXED` | plain | |
| `USABLE_FRACTION(n)` | `((n)<<1)/3` (line 590) | same | load factor 2/3 |
| `GROWTH_RATE(d)` | `(d)->ma_used*3` (line 637) | same | |
| `ASSERT_CONSISTENT(op)` | `assert(_PyDict_CheckConsistency(op, 0|1))` (line 666/668) | same, `NDEBUG`-gated | 23 uses; takes `LOCK_KEYS_IF_SPLIT` internally at 738/794 |
| `can_modify_dict(mp)` | 304-322, `#ifndef NDEBUG` | same | for frozendict asserts *untracked + uniquely referenced*, else `ASSERT_DICT_LOCKED` |

#### File-local, `setobject.c` lines 61-218

Far thinner. Set has **no keys-level lock and no split table**, so there is nothing
corresponding to `LOCK_KEYS` / `DK_*` / `USABLE_FRACTION`.

| macro / constant | line | notes |
|---|---|---|
| `dummy` = `&_dummy_struct` | 62-64 | tombstone; `entry->hash == -1` is the equivalent test |
| `SET_LOOKKEY_FOUND 1` / `NO_MATCH 0` / `ERROR -1` / `CHANGED -2` / `EMPTY -3` | 66-70 | **`CHANGED` is set's `DKIX_KEY_CHANGED`** — the re-entry signal |
| `compare_func` typedef | 72-73 | the three comparators are passed as a function pointer to `set_do_lookup` |
| `SET_IS_SHARED` / `SET_MARK_SHARED` | 77-78 (FT) / 132-133 (GIL, `0`/empty) | |
| `LINEAR_PROBES 9` | 214 | **set-only**: hybrid linear+random probing. dict is pure `5i+1+perturb` |
| `PERTURB_SHIFT 5` | 218 (set) / 338 (dict) | same value, defined twice |
| `PySet_MINSIZE 8` | `Include/cpython/setobject.h:18` | vs `PyDict_MINSIZE 8` at `dictobject.c:116` |
| `PySet_GET_SIZE(so)` | `Include/cpython/setobject.h:64` | FT-atomic relaxed load of `used` |
| load factor | inline, not a macro: `(size_t)so->fill*5 < mask*3` at `setobject.c:324`, `(so->fill + other->used)*5 >= so->mask*3` at 849 | **3/5, hand-inlined, no named constant** — vs dict's named `USABLE_FRACTION` |

#### From `pycore_pyatomic_ft_wrappers.h`

Header comment is explicit: these are "**only atomic in free-threaded builds**".
On the default GIL build every `FT_ATOMIC_*` is a plain load/store. So an
`FT_ATOMIC_LOAD_..._ACQUIRE` next to a plain read is a *free-threading* asymmetry
finding, never a GIL-build one. Counts in this slice: dict 29 loads / 32 stores,
set 17 loads / 41 stores.

---

## 2. Structural map

### 2.1 `Objects/dictobject.c` — 292 functions

| lines | region | owns |
|---|---|---|
| 1-153 | **Preamble** | The `PyDictKeysObject` layout diagram (9-50), the split/combined + four-slot-kinds contract (53-89), insertion-order-in-a-bit-vector note (91-106), includes, forward decls |
| **156-336** | **FT macro layer** | Everything in §1.2 above. **Read this before anything else.** The `LOCK_KEYS` prohibition comment is at **218-227** |
| 340-430 | Probe-sequence essay | Tim Peters' notes; no code |
| 449-576 | Keys refcounting + index accessors | `dictkeys_incref/decref` (480-523), `dictkeys_get_index` (526-548), `dictkeys_set_index` (551-576) |
| 579-694 | Size math + debug | `USABLE_FRACTION` 590, `calculate_log2_keysize` 593, `GROWTH_RATE` 637, `Py_EMPTY_KEYS` 639, `get_index_from_order` 671 |
| **697-799** | `_PyDict_CheckConsistency` | The invariant oracle. **Takes `LOCK_KEYS_IF_SPLIT` at 738/794.** If you want to know what "consistent" means, read this, not the comments |
| 802-1088 | Keys/values allocation + dict construction | `new_keys_object` 847, `free_keys_object` 874, `new_values` 913, `new_dict_impl` 941, `new_dict_with_shared_keys` 1006, `clone_combined_dict_keys` 1022, `PyDict_New` 1083 |
| **1090-1445** | **LOOKUP FAMILY (locked path)** | `lookdict_index` 1091, `do_lookup` 1112 (the unrolled probe loop, takes a comparator fn ptr), the three comparators `compare_unicode_generic` 1156 / `compare_unicode_unicode` 1191 / `compare_generic` 1212, their `*_lookup_*` wrappers, `unicodekeys_lookup_split` 1270, `_PyDictKeys_StringLookup*` 1299/1309/1327, **`_Py_dict_lookup` 1358** (CPY-0107), `ensure_shared_on_read` 1425 |
| **1448-1780** | **LOOKUP FAMILY (FT lock-free path)** | `ensure_shared_on_resize` 1448, the `*_threadsafe` comparator triplet 1484/1527/1563, `_Py_dict_lookup_threadsafe` 1600, `lookup_threadsafe_unicode` 1698, `_Py_dict_lookup_threadsafe_stackref` 1730. **The 1756-1776 pair is the GIL-build alias** |
| 1782-1861 | Specialising helpers | `_PyDict_GetMethodStackRef` 1782, `_PyDict_HasOnlyStringKeys` 1828, `_PyDict_EnablePerThreadRefcounting` 1843 |
| **1863-2341** | **INSERT / RESIZE FAMILY** | `find_empty_slot` 1876, `insertion_resize` 1892, `insert_combined_dict` 1898, **`insert_split_key` 1942** (CPY-0096), `_PyDict_InsertSplitValue` 1989, **`insertdict` 2018**, `insert_to_emptydict` 2089, `build_indices_generic/unicode` 2136/2151, `invalidate_and_clear_inline_values` 2167, **`dictresize` 2191** |
| 2343-2413 | Presized constructors | `dict_new_presized` 2343, `_PyDict_FromItems` 2377 |
| 2415-2798 | C-API getters | `dict_getitem` 2425, `PyDict_GetItem` 2469, `_PyDict_LookupIndex*` 2498/2514, the `GetItemRef` family 2551-2645, `PyDict_GetItemWithError` 2651, `_PyDict_LoadGlobal*` 2715/2740, `_PyDict_LoadBuiltinsFromGlobals` 2766 |
| 2804-2934 | C-API setters | `setitem_take2_lock_held*` 2805/2820, `_PyDict_SetItem_Take2*` 2834/2844, `PyDict_SetItem` 2860, `setitem_lock_held` 2890, `_PyDict_SetItem_KnownHash*` 2900/2911 |
| **2936-3180** | Delete / clear | `delete_index_from_values` 2936, **`delitem_common` 2954** (CPY-0115), `PyDict_DelItem` 2997, `_PyDict_DelItem_KnownHash*` 3010/3043, `delitemif_lock_held` 3053, `clear_embedded_values` 3108, `clear_lock_held` 3123, `PyDict_Clear` 3174 |
| **3182-3272** | **Cursor API** | **`_PyDict_Next` 3187**, `PyDict_Next` 3268. Borrowed-ref cursor; the header comment above 3182 is the contract |
| 3276-3643 | pop / fromkeys | `_PyDict_Pop_KnownHash` 3276, `pop_lock_held` 3320, `PyDict_Pop` 3356, `dict_dict_fromkeys` 3405, `dict_set_fromkeys` 3432, **`_PyDict_FromKeys` 3459** (the CS/CS2 dispatch nest at 3507-3575) |
| 3645-3846 | Type basics | `dict_dealloc` 3647, `anydict_repr_impl` 3684, `dict_repr` 3776, `dict_length` 3786, `frozendict_length` 3792, `_PyDict_Subscript*` 3798-3846 |
| 3848-4013 | keys/values/items C-API | `dict_as_mapping` 3848, `keys_lock_held` 3854, `values_lock_held` 3903, `items_lock_held` 3951 |
| **4016-4437** | **UPDATE / MERGE FAMILY** | `dict_fromkeys_impl` 4026, `dict_update_arg` 4034, `merge_from_seq2_lock_held` 4094, `dict_dict_merge` 4210, **`dict_merge` 4299** (the CS2-vs-CS branch at 4312/4321), `PyDict_Update` 4420, `PyDict_Merge` 4426 |
| 4440-4656 | Copy family | `copy_values` 4455, **`copy_lock_held_untracked` 4473** (CPY-0079), `PyDict_Copy` 4589, `anydict_copy_untracked` 4609, `_PyDict_CopyAsDict` 4628 |
| 4658-4753 | Equality | `dict_equal_lock_held` 4662, `dict_equal` 4724 (CS2), `dict_richcompare` 4735 |
| 4757-5245 | dict methods (clinic bodies) | `dict___contains___impl` 4766, `dict_get_impl` 4790, `dict_setdefault_ref_lock_held` 4812, `PyDict_SetDefaultRef` 4914, `dict_clear_impl` 4965, `dict_pop_impl` 4987, `dict_popitem_impl` 5004, `dict_traverse` 5088, `dict_tp_clear` 5120, `_PyDict_SizeOf*` 5129-5176, `_PyDict_Or`/`IOr` 5191-5239 |
| 5250-5603 | Slot tables + type + string C-API | `mapp_methods` 5252, `dict_as_sequence` 5336, `dict_as_number` 5349, `dict_contains` 5275, `PyDict_Contains` 5288, **`PyDict_Type` 5497**, `dict_new/init/vectorcall` 5385-5433, `frozendict_vectorcall` 5435, `dict_iter` 5479, `PyDict_GetItemString` 5546ff |
| **5605-6423** | **ITERATORS** | `dictiterobject` struct 5607, **`dictiter_new` 5616** (CPY-0015 at 5646), `dictiter_dealloc` 5657, `dictiter_len` 5677. **Build-split:** `dictiter_iternext{key,value,item}_lock_held` (5711/5836/5957) exist **only on the GIL build** (`#ifdef Py_GIL_DISABLED ... #else` at 5703/5709/5778); FT uses `dictiter_iternext_threadsafe` 6062 + `acquire_key_value` 6038 / `acquire_iter_result` 6171. Reverse iterator `dictreviter_iter_lock_held` 6253 + `dictreviter_iternext` 6343 uses a critical section on **both** builds. Iterator types 5801/5924/6217/6360/6402/6414. `dictiter_reduce` 6387 |
| **6426-7255** | **VIEW OBJECTS** | `_PyDictView_New` 6460, `all_contained_in` 6509, `dictview_richcompare` 6533, `dictview_repr` 6592, `dictviews_to_set` 6651, `dictviews_sub` 6665, `_PyDictView_Intersect` 6687, `dictviews_or` 6772, `dictitems_xor_lock_held` 6787, `dictviews_xor` 6889, `dictviews_isdisjoint` 6930; three type objects at 6997/7109/7199. **This is where dict borrows set semantics** |
| **7256-8341** | **MANAGED DICT / INSTANCE ATTRS / VERSIONS / WATCHERS** | `_PyDict_NewKeysForClass` 7258, `_PyObject_InitInlineValues` 7305 (`LOCK_KEYS` at 7317), `_PyObject_MaterializeManagedDict*` 7357/7378, `store_instance_attr_lock_held` 7437, `_PyObject_StoreInstanceAttribute` 7551, `_PyObject_TryGetInstanceAttribute` 7634, `_PyObject_SetManagedDict` 7871, `PyObject_ClearManagedDict` 7986, keys-version machinery 8140-8192, **watcher API 8194-8321** incl. `_PyDict_SendEvent` 8297 |
| **8344-8597** | **frozendict** | `frozendict_getnewargs` 8346, `frozendict_repr` 8385, `_shuffle_bits` 8406, **`frozendict_pair_hash` 8414** (CPY-0019), **`frozendict_hash` 8446**, `frozendict_new*` 8488/8502, `PyFrozenDict_New` 8525, `frozendict_copy_impl` 8554, `PyFrozenDict_Type` 8572 |

### 2.2 `Objects/setobject.c` — 112 functions

`setobject.c` has almost no section banners (only the probing block at 209-411), so
these boundaries are derived from the function list.

| lines | region | owns |
|---|---|---|
| 1-59 | Preamble | Hettinger's header: derived from dictobject.c, Knuth Alg. D, **"Unlike the dictionary implementation, the lookkey function can return NULL if the rich comparison returns an error"** (24-25), the found-vs-not-found design rationale (27-31); includes; clinic class decls |
| **61-207** | **Constants + FT layer + the three comparators** | `dummy` 62-64, `SET_LOOKKEY_*` 66-70, `compare_func` 72, `ensure_shared_on_read` 80, **`set_compare_threadsafe` 96** (FT lock-free), **`set_compare_entry_lock_held` 137**, **`set_compare_frozenset` 171**, `set_zero_table` 195 |
| **209-411** | **PROBING** (banner-delimited) | `LINEAR_PROBES` 214, **`set_do_lookup` 220** (the shared probe loop; takes a `compare_func`), **`set_add_entry_takeref` 252** (the documented `restart:` loop — **the guarded-twin exemplar for this whole slice**), `set_add_entry` 337, `set_unhashable_type` 345, `_PySet_AddTakeRef` 361, `set_insert_clean` 383 |
| 413-476 | Lookup wrappers | **`set_lookkey` 413** (takes its *own* critical section), `set_lookkey_threadsafe` 436 (FT only), `free_entries` 467 |
| **478-643** | **RESIZE + entry ops** | **`set_table_resize` 483**, `set_contains_entry` 565, `set_discard_entry` 579, `set_add_key` 600, `set_contains_key` 611, `set_discard_key` 622, `set_empty_to_minsize` 633 |
| 645-731 | Clear + cursor | `set_clear_internal` 645, **`set_next` 709** (borrowed-ref cursor; its comment at 706-707 says "In general, it isn't safe to use set_next in a loop that mutates the table") |
| 733-826 | Type basics | `set_dealloc` 733 (`FT_CLEAR_WEAKREFS` at 743), `set_repr_lock_held` 756 (the gh-129967 note at 774-775 is worth reading), `set_repr` 810, `set_len` 821 |
| 828-946 | Merge / pop / traverse | **`set_merge_lock_held` 828** (asserts *both* objects locked), `set_pop_impl` 910, `set_traverse` 936 |
| **948-1027** | **HASH** | `_shuffle_bits` 953, **`frozenset_hash_impl` 972** — comment at 969-970: "*If you update this code, update also frozendict_hash() which copied this code*", `frozenset_hash` 1014 (cache in `so->hash`) |
| **1029-1169** | **ITERATOR** | `setiterobject` 1031, `setiter_dealloc` 1039, `setiter_len` 1057, **`setiter_reduce` 1069** (struct-copy-of-a-PyObject, see §6.7), **`setiter_iternext` 1095**, `PySetIter_Type` 1138 |
| 1171-1184 | `set_iter` | |
| **1186-1340** | **UPDATE FAMILY** | `set_update_dict_lock_held` 1186, `set_update_iterable_lock_held` 1221, `set_update_lock_held` 1246, `set_update_local` 1259, **`set_update_internal` 1283** (the CS/CS2 dispatch nest at 1291-1314), `set_update_impl` 1327 |
| 1342-1497 | Constructors | `make_new_set_untracked` 1350, `make_new_set` 1378, `make_new_set_basetype*` 1388/1400, `_PyFrozenSet_MaybeUntrack` 1411, `make_new_frozenset` 1430, `frozenset_new` 1448, `frozenset_vectorcall` 1466, `set_new` 1483, `copy_small_table` 1490 |
| 1500-1578 | **Body swap** | **`set_swap_bodies` 1511** (no dict analogue), `_PySet_Freeze` 1570 (mutates `ob_type` in place) |
| 1580-1646 | Copy / clear methods | `set_copy_untracked_lock_held` 1580, `set_copy_impl` 1603, `frozenset_copy_impl` 1622, `set_clear_impl` 1640 |
| **1649-2401** | **SET ALGEBRA** | union 1656/1683/1705; **`set_intersection` 1718** + 1811/1838/1859/1876/1891; `set_isdisjoint_impl` 1920; **`set_difference_update_internal` 1980**, `set_copy_and_difference_untracked` 2069, **`set_difference_untracked` 2083**, 2168/2202/2219; symmetric difference 2236/2267/2304/2357/2378/2387 |
| 2404-2531 | Ordering | `set_issubset_impl` 2413, `set_issuperset_impl` 2458, `set_richcompare` 2489 |
| **2534-2811** | **Methods** | `set_add_impl` 2545, **`_PySet_Contains` 2554**, `set_contains` 2576, `set___contains___impl` 2593, **`frozenset___contains___impl` 2615**, `set_remove_impl` 2650, `set_discard_impl` 2690, `set___reduce___impl` 2720, `set___sizeof___impl` 2751, `set_init` 2762, `set_vectorcall` 2791 |
| 2813-3030 | Slot tables + types | `set_as_sequence` 2813, `set_methods` 2826, `set_as_number` 2852, **`PySet_Type` 2890**, `frozenset_methods` 2940, `frozenset_as_number` 2957, **`PyFrozenSet_Type` 2982** |
| 3032-3189 | C API | `PySet_New` 3032, `PyFrozenSet_New` 3038, `PySet_Size` 3048, `PySet_Clear` 3058, `_PySet_ClearInternal` 3069, `PySet_Contains` 3075, `PySet_Discard` 3092, `PySet_Add` 3107, `_PySet_NextEntry` 3135, `_PySet_NextEntryRef` 3151, `PySet_Pop` 3168, `_PySet_Update` 3178 |
| 3191-3228 | Dummy type | `dummy_repr` 3193, `dummy_dealloc` 3199, `_PySetDummy_Type`, `_dummy_struct` 3228 |

---

## 3. Macros and inlines that can run arbitrary Python

**This is the central hazard of the slice.** Anything below can, directly or via a
callback, transfer control to a Python frame. After it returns, every cached local
derived from container state — `ma_keys`, `dk_entries`, `ep`, `ep0`, `so->table`,
`entry`, `mask`, `ix`, `hashpos`, a borrowed value, `di_pos`, `si_pos` — may be
stale, and the backing memory may be freed.

### 3.1 Tier 1 — obviously Python-invoking, but check *what is cached across them*

| construct | sites in slice | what it can run |
|---|---|---|
| `PyObject_RichCompareBool(a, b, Py_EQ)` | dict 7, set 4 | user `__eq__` |
| `PyObject_Hash(key)` / `_PyObject_HashDictKey` | dict 19, set 9 | user `__hash__` |
| `PyObject_GetIter` / `PyIter_Next` | update/merge/algebra regions | user `__iter__` / `__next__` |
| `PyObject_Repr` / `PyUnicode_FromFormat("%T"/"%S"/...)` | `set_repr_lock_held` 788, `anydict_repr_impl`, `set_unhashable_type` 355 | user `__repr__` |
| `PySequence_List` | `setiter_reduce` 1079, `dictiter_reduce` 6394 | the object's own `tp_iternext` — **on a stack-allocated struct copy** |

### 3.2 Tier 2 — *macros* that look inert and are not. **These are the ones agents miss.**

| macro / inline | expands to | reaches Python via |
|---|---|---|
| **`_PyDict_NotifyEvent(event, mp, key, value)`** (`pycore_dict.h:317`) | `_PyDict_SendEvent` when any watcher bit is set (`dictobject.c:8297`) | (a) the C watcher callback `cb(...)` at 8309 — arbitrary; (b) on callback failure, `PyErr_FormatUnraisable` at 8314 → **`sys.unraisablehook`**, which is Python. **14 call sites**: 1917, 1997, 2003, 2060, 2103, 3038, 3083, 3142, 3307, 3652, 4234, 5051, 5066, 7510. Several are *inside* mutation sequences, i.e. the dict is mid-update when the callback runs |
| **`Py_DECREF` / `Py_XDECREF` / `Py_CLEAR` / `Py_SETREF`** | refcount-- and possibly `tp_dealloc` | `__del__`, weakref callbacks, and (in FT) queued frees. dict has 130 `Py_DECREF` + 27 `Py_XDECREF`; set has 81 + 5. `insertdict:2076` even documents it: `Py_XDECREF(old_value); /* which **CAN** re-enter (see issue #22653) */` |
| **`FT_CLEAR_WEAKREFS(self, so->weakreflist)`** (`pycore_weakref.h:32/46`) | `PyObject_ClearWeakRefs(obj)` | weakref callbacks — arbitrary Python. `set_dealloc:743` |
| **`_PyObject_GC_TRACK` / any allocation** (`new_keys_object`, `PyObject_GC_New`, `PyMem_NEW`, `PyList_New`) | allocator | a GC collection, hence `__del__` / weakref callbacks / `gc` callbacks |
| **`_PyType_Modified_Unlocked(type)`** | type-watcher fan-out | arbitrary Python. Only one site: `insert_split_key:1971` — **and it is inside `LOCK_KEYS`** (CPY-0096) |

Explicitly **not** in this tier — verified, so don't flag them:

* `ASSERT_CONSISTENT` / `_PyDict_CheckConsistency` — reads cached unicode hashes only; no Python. (It *does* take `LOCK_KEYS_IF_SPLIT`, which matters for lock-order reasoning, not for re-entry.)
* `unicode_eq()` (`stringlib/eq.h`) — raw memcmp on two exact-`str` objects. This is precisely why `compare_unicode_unicode` is the safe comparator.
* `unicode_get_hash` / `PyUnstable_Unicode_GET_CACHED_HASH` — reads a cached field, returns -1 if absent, never calls `tp_hash`.
* `frozenset_hash_impl` — reads `entry->hash` only. **Contrast `frozendict_hash`, §6.6.**
* `_shuffle_bits`, `DK_*`, `DKIX_*`, `FT_ATOMIC_*`, `LOAD/STORE_INDEX` — pure.

### 3.3 The three lock primitives are **not** interchangeable

| primitive | reentrant? | suspended when the thread detaches? | held across Python? |
|---|---|---|---|
| `Py_BEGIN_CRITICAL_SECTION(op)` | **Only if `op`'s mutex is the *top-most* critical section** (`Python/critical_section.c:28-44`). `CS(a) → CS(b) → CS(a)` is a **self-deadlock**, not a no-op | **Yes** — `detach_thread` calls `_PyCriticalSection_SuspendAll` (`Python/pystate.c:2323`), `_PyCriticalSection_Resume` on re-attach (2304) | So a CS does **not** guarantee atomicity across a call that blocks or hits a safepoint. It guarantees it across a call that merely runs bytecode without detaching |
| `Py_BEGIN_CRITICAL_SECTION2(a, b)` | same rule, checks both mutexes; sorts by address to avoid ordering deadlock (`pycore_critical_section.h:163-170`) | yes | same |
| **`LOCK_KEYS(keys)`** = `PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)` | **No. Never.** A raw `PyMutex` | **No** — `_Py_LOCK_DONT_DETACH` is 0, i.e. a waiter does not release its outer critical section | **Forbidden by the file's own comment**, `dictobject.c:218-227` |

The `LOCK_KEYS` contract, quoted verbatim from `dictobject.c:218-227`:

> gh-151593: The `_Py_LOCK_DONT_DETACH` flag ensures that the outer critical section
> is not dropped if there is some contention on the keys lock. It also means that it
> will be important that `LOCK_KEYS()` is essentially the "inner-most" code and that
> **we don't call `Py_DECREF()` or similar while holding the keys lock.**
> We are not allowed to acquire other locks within `LOCK_KEYS()`. For example,
> **`PyType_Modified()` must not be called within `LOCK_KEYS()`** since it acquires
> the type lock.

**All 7 `LOCK_KEYS` regions in the slice**, with what runs inside each:

| # | region | function | inside the lock |
|---|---|---|---|
| 1 | 738-794 | `_PyDict_CheckConsistency` | pure reads + `assert` (debug only) |
| 2 | 1283-1285 | `unicodekeys_lookup_split` | `unicodekeys_lookup_unicode` → `compare_unicode_unicode` → `unicode_eq` only. **Clean** |
| 3 | 1317-1320 | `_PyDictKeys_StringLookupAndVersion` | same + version read. Clean |
| 4 | **1385-1389** | **`_Py_dict_lookup`** | `unicodekeys_lookup_generic` → `compare_unicode_generic` → **`PyObject_RichCompareBool` + `Py_INCREF`/`Py_DECREF`** — **CPY-0107**, the tree's only lock-order inversion |
| 5 | **1962-1981** | **`insert_split_key`** | **`_PyType_Modified_Unlocked` (1971)** + `Py_NewRef` — **CPY-0096**, and it is the exact case the 218-227 comment names |
| 6 | 2230-2261 | `dictresize` | `Py_NewRef(ep->me_key)` ×N + `build_indices_*`. No DECREF, no Python. Note the allocation (`new_keys_object`, 2220) is correctly *outside* |
| 7 | 7317-7321 | `_PyObject_InitInlineValues` | one atomic store. Clean |

**So: 5 of 7 are clean, 2 are the two recorded findings.** If you are hunting a new
`LOCK_KEYS` violation in this slice, the population is exhausted — say so with this
denominator rather than reporting a structural zero.

### 3.4 The locking you cannot see in the `.c` file

Argument Clinic's `@critical_section` directive emits the lock into
`Objects/clinic/<file>.c.h`. **An agent reading only the `.c` file will conclude
these functions are unlocked. They are not.**

| | `@critical_section` directives | `Py_BEGIN_CRITICAL_SECTION` in the clinic file |
|---|---|---|
| `dictobject.c` | **2** — `dict.setdefault` (4936), `dict.popitem` (4995) | 2 (`clinic/dictobject.c.h:147, 230`) |
| `setobject.c` | **13** — `set.pop` 901, `set.copy` 1596, `frozenset.copy` 1615, `set.clear` 1633, `set.isdisjoint` 1911*, `set.symmetric_difference` 2347*, `set.issubset` 2404*, `set.issuperset` 2449*, `set.add` 2534, `set.remove` 2639, `set.discard` 2678, `set.__reduce__` 2713, `set.__sizeof__` 2744 | 13 (`clinic/setobject.c.h`) |

`*` = `@critical_section so other` → emits `Py_BEGIN_CRITICAL_SECTION2`.

**This is asymmetry #1 and the single most consequential one for triage** (see §6.1).

### 3.5 `ASSERT_DICT_LOCKED` does not mean what it looks like

`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(op)` (`pycore_critical_section.h:64-67`) is:

```c
# define _Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(op)   \
    if (Py_REFCNT(op) != 1) {                            \
        _PyCriticalSection_AssertHeldObj(_PyObject_CAST(op)); \
    }
```

Three separate ways it is silently satisfied:

1. `Py_DEBUG` is off → the macro is **empty**.
2. `Py_GIL_DISABLED` is off → `ASSERT_DICT_LOCKED` is **empty**.
3. `Py_REFCNT(op) == 1` → **skipped**, by design (the header's own note at 54-61
   explains the FT GC can perturb `ob_tid`).

Case 3 fires routinely in this slice: every "build a fresh result set/dict, then
`set_add_entry(result, ...)` / `insertdict(new, ...)`" path passes the assert on a
refcount-1 object that no critical section covers. `set_intersection:1754` is the
clearest instance. **Do not cite "it asserts locked" as evidence that a lock is
held.** Trace the caller.

---

## 4. Who-calls-whom, hot paths

### 4.1 dict lookup

```
PyDict_GetItem / GetItemRef / Contains / SetItem / DelItem / SetDefault ...
  └─ Py_BEGIN_CRITICAL_SECTION(mp)          <- lock taken HERE, at the public entry
       └─ *_lock_held()                     <- 29 such functions; each ASSERT_DICT_LOCKED
            └─ _Py_dict_lookup(mp,key,hash,&value_addr)     [1358]
                 ├─ kind != GENERAL && PyUnicode_CheckExact(key)
                 │    ├─ SPLIT (FT only): unicodekeys_lookup_split [1270]
                 │    │      ├─ unicodekeys_lookup_unicode_threadsafe   (lock-free)
                 │    │      └─ on DKIX_KEY_CHANGED: LOCK_KEYS -> unicodekeys_lookup_unicode -> UNLOCK   #2
                 │    └─ else:              unicodekeys_lookup_unicode [1206]
                 │           └─ do_lookup(NULL, dk, ..., compare_unicode_unicode)      <- unicode_eq, NO Python
                 ├─ kind != GENERAL && !unicode(key)
                 │    └─ INCREF_KEYS_FT; LOCK_KEYS_IF_SPLIT  #4
                 │         └─ unicodekeys_lookup_generic [1185]
                 │              └─ do_lookup(mp, dk, ..., compare_unicode_generic)     <- RUNS PYTHON, under the keys lock if SPLIT
                 │       UNLOCK_KEYS_IF_SPLIT; DECREF_KEYS_FT; if DKIX_KEY_CHANGED goto start
                 └─ kind == GENERAL
                      └─ dictkeys_generic_lookup [1240]
                           └─ do_lookup(mp, dk, ..., compare_generic)                  <- RUNS PYTHON, no keys lock
                             ; if DKIX_KEY_CHANGED goto start
```

Lock-state rules you can apply mechanically:

* `_Py_dict_lookup` **requires** the dict critical section (`_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED(mp)` at 1365) — subject to §3.5.
* Under `LOCK_KEYS`: only `unicodekeys_lookup_unicode`, `unicodekeys_lookup_generic`, `_PyDict_CheckConsistency`'s body, `dictresize`'s copy loop, `insert_split_key`'s insert block, and `_PyObject_InitInlineValues`' decrement. Nothing else in the file.
* The `*_threadsafe` family (1484-1776) runs with **no lock at all** and re-validates via `_Py_TryIncrefCompare` + a table/key recheck. Its correctness argument is entirely "retry on `DKIX_KEY_CHANGED`", not mutual exclusion.
* The `_lock_held` suffix is the convention marker; **the absence of the suffix does not mean unlocked** (see the 13 clinic-locked set functions, and `dict_popitem_impl`).

### 4.2 dict insert / resize

```
PyDict_SetItem / _PyDict_SetItem_Take2 / setdefault / update ...
  └─ CS(mp) ─ insertdict(mp, key, hash, value)                [2018]  assert(can_modify_dict)
       ├─ split && unicode(key):
       │     insert_split_key(mp->ma_keys, key, hash)          [1942]
       │       ├─ FT: unicodekeys_lookup_unicode_threadsafe (fast, unlocked)
       │       ├─ FT: Py_BEGIN_CRITICAL_SECTION_MUTEX(types.mutex)   <- type lock BEFORE keys lock, deliberately
       │       └─ LOCK_KEYS -> [lookup; _PyType_Modified_Unlocked; find_empty_slot;
       │                        dictkeys_set_index; STORE_SHARED_KEY(Py_NewRef);
       │                        split_keys_entry_added] -> UNLOCK_KEYS
       │     then _PyDict_InsertSplitValue                     [1989]  -> _PyDict_NotifyEvent, Py_DECREF(old)
       └─ else: _Py_dict_lookup (§4.1)  -> old_value
             ├─ old_value == NULL: insert_combined_dict        [1898]
             │      ├─ kind mismatch  -> insertion_resize(mp,0) -> dictresize   [2191]
             │      ├─ dk_usable <= 0 -> insertion_resize(mp,1) -> dictresize
             │      ├─ _PyDict_NotifyEvent(ADDED)   <- ARBITRARY PYTHON, mid-insert
             │      ├─ find_empty_slot / dictkeys_set_index / STORE_KEY / STORE_VALUE
             │      └─ STORE_KEYS_USABLE / STORE_KEYS_NENTRIES
             └─ old_value != NULL: _PyDict_NotifyEvent(MODIFIED); STORE_VALUE; Py_XDECREF(old_value)  <- re-entry, documented at 2076
```

Note the ordering hazard shape to look for: in `insert_combined_dict`,
`_PyDict_NotifyEvent(ADDED)` at **1917** happens *before* `find_empty_slot` (1920) and
before the `dk_usable`/`dk_nentries` updates (1936-1937), but *after* the resize
decision (1910-1915). Anything a watcher does to `mp` in between invalidates the
`dk_usable > 0` premise that 1938's `assert(mp->ma_keys->dk_usable >= 0)` relies on.
That is the mechanism behind the inherited CPY-0096 consequences.

### 4.3 set lookup / insert

```
PySet_Contains / set.__contains__ / set_contains_key
  └─ set_contains_entry(so,key,hash)                        [565]     <- NO lock here
       ├─ FT:  set_lookkey_threadsafe                       [436]
       │         ├─ frozenset exact -> set_do_lookup(..., set_compare_frozenset)   (no lock, no incref)
       │         ├─ ensure_shared_on_read
       │         ├─ snapshot table+mask atomically; if it moved -> fall back to set_lookkey
       │         └─ set_do_lookup(..., set_compare_threadsafe); CHANGED -> set_lookkey
       └─ GIL: set_lookkey                                  [413]
                 ├─ frozenset exact -> set_do_lookup(..., set_compare_frozenset)
                 └─ else: Py_BEGIN_CRITICAL_SECTION(so)      <- lock taken HERE, at the LEAF
                            do { set_do_lookup(..., set_compare_entry_lock_held) }
                            while (status == SET_LOOKKEY_CHANGED);
                          Py_END_CRITICAL_SECTION()

PySet_Add / set.add / set_add_key
  └─ [CS(so) from PySet_Add:3112 or from clinic for set.add]
       └─ PyObject_Hash(key)                                  <- ARBITRARY PYTHON, inside the CS
       └─ set_add_entry [337] -> assert locked -> set_add_entry_takeref [252]
             restart: mask = so->mask; ...
               PyObject_RichCompareBool(startkey, key, Py_EQ)  <- ARBITRARY PYTHON
               if (table != so->table || entry->key != startkey) goto restart;   <- THE GUARD
               mask = so->mask;                               <- re-read after Python
             found_unused: ... if load factor exceeded -> set_table_resize [483]
```

`set_add_entry_takeref`'s `restart:` loop (264, reached from 295 and 312) is the
**canonical guarded twin for this entire slice**. What its guard defends against is
precisely: *the same thread's user `__eq__` mutated the set*. Note what it re-reads
after the comparison — `so->table`, `entry->key`, and `so->mask` (296) — and note
what it does **not** re-read: `freeslot` (set at 300, validated only by the
`freeslot->hash != -1` recheck at 311). When you cite this as the twin, cite that
specific threat model, per lesson 3 of the brief.

`set_do_lookup` (220) has no restart loop of its own; it *returns* `SET_LOOKKEY_CHANGED`
and the loop lives in the caller — but **only in `set_lookkey` (423-426)**.
`set_lookkey_threadsafe` (457) converts CHANGED into a single fall-back call to
`set_lookkey`, and `frozenset___contains___impl` (2631) calls `set_do_lookup`
**directly with no CHANGED handling at all** (relying on `set_compare_frozenset`
never returning CHANGED — verify that premise if you touch it).

---

## 5. Lock inventory (numbers to quote)

| construct | dictobject.c | setobject.c |
|---|---:|---:|
| `Py_BEGIN_CRITICAL_SECTION(...)` | 40 | 19 |
| `Py_BEGIN_CRITICAL_SECTION2(a, b)` | 6 | 11 |
| `Py_BEGIN_CRITICAL_SECTION_MUTEX(...)` | 1 (`insert_split_key`, types.mutex) | 0 |
| clinic-emitted `Py_BEGIN_CRITICAL_SECTION*` | 2 | 13 |
| **total lock-acquisition sites** | **49** | **43** |
| `LOCK_KEYS` acquisition sites | 7 | — (no such lock) |
| `*_lock_held` / `*_LockHeld` function definitions | 29 | 7 |
| `ASSERT_DICT_LOCKED` uses | 23 | — |
| `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` uses | (via `ASSERT_DICT_LOCKED`) + `_Py_dict_lookup:1365` | 15 (141, 340, 838, 839, 1191, 1194, 1224, 1583, 1983, 1984, 2239, 2242, 2270, 2271, 3160) |
| `ASSERT_WORLD_STOPPED_OR_*` uses | 4 (700, 7360, 7965, 8033) | 0 |

---

## 6. Where dict and set diverge — the asymmetry list

Both files solve the same problem (open-addressed hash table with user-controlled
`__hash__`/`__eq__`). `setobject.c:5` says so outright: *"Derived from
Objects/dictobject.c."* Every place they answer the same question differently is a
place where one of the two answers may be wrong.

### 6.1 Where the lock is taken — **root vs leaf**

* **dict**: the critical section is acquired at the **public entry point**, and the
  work is delegated to an explicitly-named `*_lock_held` helper that asserts the lock.
  29 such helpers. Only 2 clinic `@critical_section`.
* **set**: 13 of its Python-visible methods get their lock from **Argument Clinic**,
  and its *lookup primitive* `set_lookkey` takes the lock **itself, at the leaf**
  (422-427). So `PySet_Contains` (3075) holds no lock; `PySet_Discard` (3092) holds
  one; both end up inside `set_lookkey`'s own `Py_BEGIN_CRITICAL_SECTION(so)`.

Consequences to check:
1. Any set path that reads `so->table` / `so->mask` / `so->used` **between** two
   `set_lookkey` calls is unlocked in the gap even though both calls "were locked".
   `set_intersection` (1743-1761) and `set_difference_update_internal` (1980) are the
   dense cases.
2. Leaf-locking makes the reentrancy rule of §3.3 load-bearing. `CS(x) → CS(y) → CS(x)`
   self-deadlocks. The set algebra nests `CS2(a,b)`, then `CS(key)` (2569, 2626, 2662,
   2702 — locking the *key*, which is itself a set), then leaf `CS(so)`. Whether any
   ordering there can produce a non-top-most re-acquire is a genuine open question
   for the lock-discipline agent; I have not proven either way.

### 6.2 Restart-on-mutation: present in both, but expressed differently and not uniformly

| | dict | set |
|---|---|---|
| signal | `DKIX_KEY_CHANGED` (-4) returned by the comparator | `SET_LOOKKEY_CHANGED` (-2) returned by the comparator |
| who loops | `_Py_dict_lookup`'s `goto start` (1392, 1411) — **but only on the generic paths**; the `unicodekeys_lookup_unicode` path cannot produce it (no Python) | `set_lookkey`'s `do/while` (423-426); `set_add_entry_takeref`'s `goto restart` (295, 312) |
| what is re-read on restart | `dk = mp->ma_keys; kind = dk->dk_kind` (1367-1368) — the whole keys object | `mask = so->mask; i = hash & mask; freeslot = NULL; perturb = hash` (266-269) |
| non-looping consumer | — | **`set_lookkey_threadsafe`** falls back once (457) rather than looping; **`frozenset___contains___impl`** (2631) has no handling at all |

### 6.3 The comparator triplet is structurally the same and differently guarded

| | dict | set |
|---|---|---|
| no-Python fast comparator | `compare_unicode_unicode` (1191) — `unicode_eq` | none; `set_compare_entry_lock_held` (149-152) inlines the unicode fast path *inside* the general comparator |
| Python-running, locked | `compare_unicode_generic` (1156), `compare_generic` (1212) — `Py_INCREF(startkey)` → compare → `Py_DECREF` → recheck `dk == mp->ma_keys && ep->me_key == startkey` | `set_compare_entry_lock_held` (137) — `Py_INCREF` → compare → `Py_DECREF` → recheck `table != so->table \|\| entry->key != startkey` |
| Python-running, lock-free | `compare_unicode_generic_threadsafe` (1484), `compare_generic_threadsafe` (1563) | `set_compare_threadsafe` (96) — uses `_Py_TryIncrefCompare` |
| **frozen-container comparator** | **none** | **`set_compare_frozenset` (171)** — deliberately **omits the incref and omits the changed-check** ("we don't need to incref startkey before comparing and we don't need to check if the set has changed"). It still calls `PyObject_RichCompareBool` at 185 on a **borrowed** `startkey`. Its safety rests entirely on "a frozenset is immutable" |

`set_compare_frozenset` is the sharpest divergence in the comparator family: dict has
no equivalent "immutable, so skip the guards" comparator, and the premise is
falsifiable — `_PySet_Freeze` (1570) mutates `ob_type` in place on a live object, and
`PySet_Add` (3118-3129) deliberately mutates a uniquely-referenced frozenset.

### 6.4 Table growth

| | dict | set |
|---|---|---|
| load factor | `USABLE_FRACTION(n) = (n<<1)/3` — **named macro**, 2/3 | inline `fill*5 < mask*3` (324), `(fill+other->used)*5 >= mask*3` (849), `(fill+dictsize)*5 >= mask*3` (1203) — **3/5, unnamed, written out three times** |
| growth | `GROWTH_RATE(d) = ma_used*3`, via `calculate_log2_keysize` | `used > 50000 ? used*2 : used*4` (326) — inline, **only at that one site**; `set_merge_lock_held` uses `(used + other->used)*2` (850) |
| tombstones | `DKIX_DUMMY` in `dk_indices`; **FT never reuses dummy slots** (`is_unusable_slot`, 1863-1871, and the preamble note at 84-85) | `dummy` key + `hash == -1`; **dummy slots are reused** by `set_add_entry_takeref`'s `freeslot` path (309-317) **on both builds** |
| smalltable | none — always a heap `PyDictKeysObject` | `so->smalltable[8]` embedded in the object; `set_table_resize` may shrink back into it (507-524) and `set_swap_bodies` (1511) memcpys it |
| resize can run Python? | yes indirectly — `new_keys_object` allocates (GC) | yes indirectly; but `set_insert_clean` (383) exists **specifically** to avoid it. Its comment (375-381) cites *issue 1456209*: "using `set_add_entry()` risks making a callback in the middle of a `set_table_resize()`" |

dict's `dictresize` has **no** `insert_clean` equivalent: it does raw
`newentries[i] = *ep++` / `Py_NewRef` copies and calls `build_indices_*`, which is
equivalent in effect. So both avoid callbacks during resize, by different means —
worth confirming rather than assuming.

### 6.5 Iterators

| | dict | set |
|---|---|---|
| state | `di_dict, di_used, di_pos, di_result, len` | `si_set, si_used, si_pos, len` |
| size-change check | `di->di_used != d->ma_used` → `RuntimeError`, `di_used = -1` sticky (5722, 6261) | `si_used != so_used` → `RuntimeError`, `si_used = -1` sticky (1109) |
| extra "keys changed" check | **yes** — `if (di->len == 0)` → "dictionary keys changed during iteration" (5763) | **no equivalent** |
| where the check happens | **inside** the lock (`_lock_held` variants) or inside `dictiter_iternext_threadsafe` | **outside** the critical section (1107-1114, before `Py_BEGIN_CRITICAL_SECTION` at 1116) |
| where `pos` is advanced | inside the lock (5768) | **outside** the lock (`si->si_pos = i+1` at 1128, after `Py_END_CRITICAL_SECTION` at 1127) |
| FT vs GIL code path | **completely different functions**: `dictiter_iternext{key,value,item}_lock_held` are compiled **only when `Py_GIL_DISABLED` is undefined** (5703/5709/5778); FT uses `dictiter_iternext_threadsafe` (6062). The **reverse** iterator uses a critical section on both builds (6353) | **one** implementation, critical section on both builds |
| `__reduce__` | `dictiter_reduce` (6387): `dictiterobject tmp = *di;` then `PySequence_List((PyObject*)&tmp)` | `setiter_reduce` (1069): `setiterobject tmp = *si;` then `PySequence_List((PyObject*)&tmp)` |

The last row is **the CPY-0028 shape in both files**: the struct copy includes
`PyObject_HEAD`, i.e. `ob_refcnt`, `ob_tid`/`ob_ref_shared`, and (FT) `ob_mutex`, and
the copy is then passed to generic machinery as a real `PyObject *` on the **stack**.
The brief named `odictiter_reduce` (CPY-0028) as a structurally instructive
neighbour to hunt here; these are the two sites. Not my call to classify — handing
them to the FT / lock-discipline agents.

### 6.6 Hashing — the sharpest divergence

`setobject.c:969-970` says: *"If you update this code, update also `frozendict_hash()`
which copied this code."* The copy is not faithful, and could not have been:

| | `frozenset_hash_impl` (972) | `frozendict_hash` (8446) |
|---|---|---|
| what it hashes | `entry->hash` — the **cached** hash of every slot, including NULL/dummy, then corrects | key hash **and value** |
| runs Python? | **No.** Pure arithmetic over the table | **Yes** — `frozendict_pair_hash` (8414) calls `PyObject_Hash(value)` at 8427 |
| recursion | none | **unbounded** — nested frozendicts recurse through `PyObject_Hash`. **CPY-0019**, reproduced |
| cursor held across it | none | **`_PyDict_Next(op, &pos, NULL, &value, &key_hash)` at 8461**, with `value` a **borrowed ref**, and `frozendict_pair_hash` runs arbitrary Python at 8462 before the next `_PyDict_Next` call. That is the CPY-0115 shape, in the frozendict hash |
| result cached | `so->hash`, `FT_ATOMIC_STORE_SSIZE_RELEASE` (1025) | `self->ma_hash`, `FT_ATOMIC_STORE_SSIZE_RELAXED` (8481) — **release vs relaxed**, and the paired loads are `RELAXED`+`ACQUIRE` (1020-1021) vs `RELAXED` only (8450) |

The memory-ordering mismatch in the last row is a concrete, cheap thing for the
ft-race agent to adjudicate: set publishes its cached hash with a release store and
reads it back with an acquire load; frozendict uses relaxed on both sides for the
same cache-once pattern.

### 6.7 Smaller divergences worth a look

| # | topic | dict | set |
|---|---|---|---|
| a | error return of the lookup primitive | `DKIX_ERROR` (-3), never NULL | set's preamble (24-25) advertises that `lookkey` "can return NULL if the rich comparison returns an error" — but the modern code returns `SET_LOOKKEY_ERROR` and `epp` is left untouched. **The header comment is stale** |
| b | probe sequence | pure `i = (i*5 + perturb + 1) & mask`, manually unrolled 2× in `do_lookup` (1137-1151) | `LINEAR_PROBES 9` consecutive slots then the same recurrence (232-245). Different cache/collision behaviour on the same adversarial hash inputs |
| c | key kinds | three (`GENERAL` / `UNICODE` / `SPLIT`) with three comparators and a kind-transition machine (`insert_combined_dict:1904` forces GENERAL) | one; `unicode_eq` is an inline fast path inside the single comparator |
| d | split tables / shared keys | the whole `LOCK_KEYS` apparatus exists for this | **nothing analogous** — set has no shared-table concept, which is why it needs no second lock |
| e | body swap | none | `set_swap_bodies` (1511) swaps `table`/`mask`/`fill`/`used`/`hash` between two live objects and memcpys `smalltable`. No dict equivalent; used by the in-place algebra |
| f | in-place type mutation | none | `_PySet_Freeze` (1570) assigns `set->ob_type = &PyFrozenSet_Type` directly on a live object |
| g | weakrefs | dict has none | `PySetObject.weakreflist`; `set_dealloc:743` runs `FT_CLEAR_WEAKREFS` → **weakref callbacks (Python) during dealloc** |
| h | `used` counter | `ma_used` via `GET_USED`/`STORE_USED` (relaxed) | `used` via `FT_ATOMIC_*_SSIZE_RELAXED`, **plus a second counter `fill`** (active + dummy) that is **updated non-atomically** (`so->fill++` at 320, `so->fill = so->used` at 550, `so->fill = other->fill` at 867). `fill` has no atomic wrapper anywhere |
| i | unhashable-type error | `dict_unhashable_type` (2477) | `set_unhashable_type` (345) — reformats via `PyErr_Format("%T", key)`, which **calls `tp_repr`-adjacent formatting on a user object while an exception is live** |
| j | cursor contract | `_PyDict_Next` (3187) documented in the comment at 3182; `PyDict_Next` is public API | `set_next` (709) is static; the exported `_PySet_NextEntry` (3135) **does not assert the lock**, while `_PySet_NextEntryRef` (3151) **does** (3160). Same file, two exported cursors, one guarded |

Item (j) and item (h)'s `fill` are the two I would look at first if I were the
ft-race agent.

---

## 7. Structural leads (classified, not adjudicated)

I am the mapper; I read for structure, not to reproduce. Everything here is handed
to the owning agent with a classification of my confidence in the *shape*, not the
bug.

| tag | site | shape |
|---|---|---|
| **CONFIRM** | `dictobject.c:1385` `_Py_dict_lookup` | CPY-0107. Read the 218-227 comment against it; it is the *only* remaining `LOCK_KEYS`-plus-Python site besides 1962 |
| **CONFIRM** | `dictobject.c:1962` `insert_split_key` | CPY-0096. The `Py_BEGIN_CRITICAL_SECTION_MUTEX(types.mutex)` at 1959 shows the author knew about lock ordering and still put `_PyType_Modified_Unlocked` inside `LOCK_KEYS` |
| **CONFIRM** | `dictobject.c:5646` `dictiter_new` | CPY-0015 |
| **CONFIRM** | `dictobject.c:8427` `frozendict_pair_hash` | CPY-0019 |
| **CONFIRM** | `dictobject.c:4494` `copy_lock_held_untracked` | CPY-0079 — the brief says its OOM sweep needs re-running (denominator was 2) |
| **CONFIRM** | `dictobject.c:2987` `delitem_common` | CPY-0115 |
| **CONSIDER** | `setobject.c:1069` + `dictobject.c:6387` `setiter_reduce` / `dictiter_reduce` | struct-copy of a `PyObject` including `PyObject_HEAD` (and `ob_mutex` on FT), then used as a real object by `PySequence_List`. Twin of `odictobject.c` CPY-0028 (other slice). **Owner: ft-race / lock-discipline** |
| **CONSIDER** | `dictobject.c:8461-8462` `frozendict_hash` | live `_PyDict_Next` cursor + borrowed `value` across `PyObject_Hash(value)`. Same shape as CPY-0115 but a distinct site; also the recursion of CPY-0019 sits inside this loop. **Owner: recursion-guard / refcount** |
| **CONSIDER** | `setobject.c:171-193` `set_compare_frozenset` | deliberately drops both the incref and the changed-recheck on the premise of frozenset immutability, while `_PySet_Freeze` (1570) and `PySet_Add` (3118) both mutate live frozensets. **Owner: ft-race / refcount** |
| **CONSIDER** | `setobject.c` `so->fill` | second size counter, **21 accesses, not one of them atomic** (320, 324, 511, 521, 543, 550, 638, 651, 849, 858, 867, 873, 876, 993, 997, 1203, 1360, 1524, 2036, 2773, 2781), while its sibling `so->used` is uniformly `FT_ATOMIC_*` — often in the *same statement* (320-321, 867-868, 993-997). This is the textbook `atomic_plain_asymmetry` shape and the scanner reported 17 of those in-slice. **Owner: ft-race** |
| **CONSIDER** | `setobject.c:1107-1128` `setiter_iternext` | the `si_used` check and the `si_pos` advance are both **outside** the critical section that reads the table. dict does both inside. **Owner: ft-race** |
| **CONSIDER** | `dictobject.c:1917` vs `1920-1937` `insert_combined_dict` | `_PyDict_NotifyEvent(ADDED)` fires after the resize decision but before slot selection and the `dk_usable`/`dk_nentries` updates. This is the mechanism behind the three inherited CPY-0096 consequences the brief assigns to this slice |
| **CONSIDER** | `setobject.c:2631` `frozenset___contains___impl` | the only `set_do_lookup` caller with **no** `SET_LOOKKEY_CHANGED` handling. Depends on `set_compare_frozenset` never returning CHANGED — true today by construction, but nothing enforces it |
| **POLICY** | `setobject.c:24-25` preamble | "the lookkey function can return NULL if the rich comparison returns an error" — stale; the code returns `SET_LOOKKEY_ERROR` |
| **POLICY** | set load factor | `fill*5 < mask*3` written out inline at 324, 849 and 1203 with no named constant, vs dict's `USABLE_FRACTION`. A future tuning change has three places to miss |
| **ACCEPTABLE** | `LOCK_KEYS` sites 738, 1283, 1317, 2230, 7317 | verified: no Python, no `Py_DECREF`, no second lock inside |
| **ACCEPTABLE** | both files' use of `Include/internal/*` | core-build translation units in `Objects/` are entitled to internal headers |

---

## 8. Classes bounded

| class | denominator | result |
|---|---|---|
| Include-tier violations | 32 resolved directives (19 + 13) | **0.** Evidential, not structural: both files do include internal headers; they are simply entitled to. |
| Unresolved `#include` directives | 32 | **0.** No platform-conditional or third-party header in either file. |
| Include cycles touching the slice | both files are `.c` leaves with 0 outbound edges from any header back to them | **0**, structurally. The tree's one cycle (`pycore_structs.h` ↔ `pycore_context.h`) is not in the include closure of either file. |
| Missing include guards | 0 headers in scope (this slice contains no `.h` file) | **structural zero — do not report as clean.** |
| `LOCK_KEYS` regions that run Python, take a second lock, or `Py_DECREF` | **7 regions, all read** | 2 hits (both already recorded: CPY-0107 at 1385, CPY-0096 at 1962), 5 clean (738, 1283, 1317, 2230, 7317). **The population is exhausted; a new finding of this exact shape in this slice is not available.** |
| Set-side equivalent of `LOCK_KEYS` | 0 | structural zero — set has no second lock. Any "keys lock" rule reports 0 on `setobject.c` for that reason, not because set is safe. |

---

## 9. Toolkit feedback

1. **Recall gap — `@critical_section` is invisible to any `.c`-only analysis.**
   13 of `setobject.c`'s Python-visible methods are locked in
   `Objects/clinic/setobject.c.h`, not in `setobject.c`. Any rule that decides
   "is this function locked?" by scanning the `.c` file will mislabel
   `set.add`, `set.remove`, `set.discard`, `set.pop`, `set.copy`, `set.clear`,
   `set.__reduce__`, `set.__sizeof__` as unlocked and `set.isdisjoint`,
   `set.symmetric_difference`, `set.issubset`, `set.issuperset` as
   single-locked when they hold a `CS2`.
   **Proposal:** `scan_lock_discipline.py` / `scan_ft_races.py` should parse the
   `/*[clinic input]` blocks for `@critical_section [args]` and treat the
   corresponding `*_impl` as entered with those locks held. Cheap: the directive is
   a literal line immediately above the function's clinic signature. Note this also
   explains `scan_lock_discipline` reporting **0** in this slice — a slice containing
   43 set-side lock sites, 13 of which the scanner cannot see.

2. **Recall gap — `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED` is not a lock proof.**
   It is skipped when `Py_REFCNT(op) == 1` (`pycore_critical_section.h:64-67`), and
   compiled out entirely without `Py_DEBUG` or with the GIL. If any rule treats
   `ASSERT_DICT_LOCKED(mp)` as establishing "locked from here down", it will suppress
   real findings on every fresh-result path (`set_intersection:1754`,
   `set_difference_untracked`, `copy_lock_held_untracked`). **Proposal:** treat the
   assert as *documentation of intent*, never as a precondition that discharges a
   lock obligation.

3. **Recall gap — build-conditional function bodies.**
   `dictiter_iternext{key,value,item}_lock_held` exist **only** when
   `Py_GIL_DISABLED` is undefined (the `#else` arm at `dictobject.c:5709`). A scanner
   that parses one preprocessor arm — or that `scrub_macros` into a single arm —
   will either analyse dead code or miss `dictiter_iternext_threadsafe` entirely.
   Worth an explicit note in the FT rules' output about which arm was analysed.
   (Related: the whole 163-336 macro block has two arms, and the GIL arm makes 18 of
   those macros no-ops.)

4. **Precision note — `scan_ft_races` `atomic_plain_asymmetry`, 17 in-slice.**
   `so->fill` alone should account for a large share and is, in my reading, the most
   likely *true* positive of the group (its sibling `so->used` is uniformly atomic in
   the same statements — e.g. 320-321, 867-868). Suggest the rule rank
   same-struct-sibling asymmetry above cross-struct asymmetry; that ordering would
   have surfaced `fill` at the top rather than in a flat list of 17.

5. **Denominator hygiene.** Two of this slice's rule populations are structural
   zeros and should be reported as such by name, not as "clean": *missing include
   guard* (0 headers in scope) and any *keys-lock* rule against `setobject.c`
   (no such lock exists). §8 gives the wording.

---

## 10. Noticed outside slice

* `Objects/odictobject.c` — `odictiter_reduce`'s struct-copy (CPY-0028) has exact twins at `setobject.c:1069` and `dictobject.c:6387`; recorded above as leads for **this** slice, the odict site remains that slice's.
* `Objects/typeobject.c:1166` `_PyType_Modified_Unlocked` — self-documented "re-entrant"; it is the Python-reaching leg of CPY-0096 but lives outside the slice.
* `Include/cpython/setobject.h` — `setentry` and `PySetObject` are CPython-tier public struct layout, so set's table representation cannot be changed without an ABI story, while dict's `PyDictKeysObject` is fully internal. Structural, not a defect.
* `Python/critical_section.c:28-44` — the top-most-only reentrancy fast path that makes `CS(a)→CS(b)→CS(a)` a deadlock rather than a no-op. Worth a line in the toolkit's shared briefing, since every `Objects/` slice needs it.

---

### Files referenced (absolute)

* `/home/danzin/projects/cpython/Objects/dictobject.c`
* `/home/danzin/projects/cpython/Objects/setobject.c`
* `/home/danzin/projects/cpython/Objects/clinic/dictobject.c.h`
* `/home/danzin/projects/cpython/Objects/clinic/setobject.c.h`
* `/home/danzin/projects/cpython/Include/internal/pycore_dict.h`
* `/home/danzin/projects/cpython/Include/internal/pycore_setobject.h`
* `/home/danzin/projects/cpython/Include/internal/pycore_critical_section.h`
* `/home/danzin/projects/cpython/Include/internal/pycore_pyatomic_ft_wrappers.h`
* `/home/danzin/projects/cpython/Include/internal/pycore_weakref.h`
* `/home/danzin/projects/cpython/Include/internal/pycore_lock.h`
* `/home/danzin/projects/cpython/Include/cpython/setobject.h`
* `/home/danzin/projects/cpython/Include/cpython/critical_section.h`
* `/home/danzin/projects/cpython/Python/critical_section.c`
* `/home/danzin/projects/cpython/Python/pystate.c`
