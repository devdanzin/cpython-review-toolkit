# obj-typeobject pass 2 — preflight baseline

Generated after issue #28's nine recall rules landed (PR #29, cpython-review-toolkit `c2401a5`). Pass 1 ran **before** these rules existed and several were built from
pass 1's own misses, so everything under NET NEW is a candidate pass 1 could not see.

CPython tree: 4f3be1b5777

## Denominators — read before calling any zero clean

| scanner | findings | denominators |
|---|---|---|
| `scan_deprecated_apis` | 1 | functions_analyzed=0 |
| `scan_error_paths` | 6 | functions_analyzed=362, fallible_assignment_sites=45, int_status_callees_resolved=116, int_status_callees_discovered=86 |
| `scan_ft_races` | 5 | functions_analyzed=459, iternext_functions=1, lock_held_functions=0 |
| `scan_gil_usage` | 0 | functions_analyzed=459, vocabulary_resolved=0, vocabulary_tokens_seen=4 |
| `scan_init_bypass` | 4 | functions_analyzed=459, files_with_nullable_fields=1, total_nullable_fields=3 |
| `scan_lock_discipline` | 0 | functions_analyzed=459, vocabulary_resolved=66, vocabulary_tokens_seen=11, critical_section_functions=23, mutex_functions=3 |
| `scan_memory_patterns` | 2 | functions_analyzed=459, varobject_allocation_census.sites=7, varobject_allocation_census.via_slot_pointer=5, varobject_allocation_census.non_constant_nitems=2 |
| `scan_null_checks` | 0 | functions_analyzed=423, assignment_sites=1432, fallible_sources_resolved=164, local_nullable_helpers=119, outparam_wrappers=0 |
| `scan_pyerr_clear` | 1 | functions_analyzed=459, destructor_functions=15 |
| `scan_recursion_guards` | 8 | functions_analyzed=459, recursion_prone_slot_functions=11 |
| `scan_refcounts` | 18 | functions_analyzed=403, borrowed_field_accessors=4, borrowed_accessor_load_sites=45, borrowed_slot_load_sites=52 |
| `scan_stw_safety` | 0 | functions_analyzed=459, stw_functions=3 |
| `scan_uninit_dealloc` | 0 | functions_analyzed=459, allocation_sites=1 |

## Candidates from the new rules: 32 (14 already recorded, **18 net new**)

### ALREADY RECORDED — confirm in one line, do not re-litigate

| line | function | rule | conf | record |
|---|---|---|---|---|
| :1206 | `_PyType_Modified_Unlocked` | self_recursion | high | CPY-0087 |
| :1431 | `assign_version_tag` | self_recursion | high | CPY-0087 |
| :1854 | `mro_hierarchy_for_complete_type` | self_recursion | high | CPY-0087 |
| :1965 | `type_set_bases_unlocked` | borrowed_field_deref_across_call | high | CPY-0070 |
| :1966 | `type_set_bases_unlocked` | int_status_never_tested | high | CPY-0070 |
| :4958 | `type_new_impl` | publish_before_init_complete | low | CPY-0072 |
| :6500 | `set_flags_recursive` | self_recursion | high | CPY-0087 |
| :7117 | `merge_class_dict` | self_recursion | high | CPY-0071 |
| :9336 | `type_ready_inherit` | borrowed_field_deref_across_call | high | CPY-0068 |
| :9790 | `remove_subclass` | borrowed_field_deref_across_call | medium | CPY-0069 |
| :12359 | `update_subclasses` | mutual_recursion | high | CPY-0087 |
| :12377 | `recurse_down_subclasses` | borrowed_field_deref_across_call | medium | CPY-0069 |
| :12793 | `super_descr_get` | vararg_sentinel | medium | CPY-0080 |
| :12797 | `super_descr_get` | one_hop_param_deref | medium | CPY-0007 |

### NET NEW — pass 2's inbox

| line | function | scanner | rule | conf |
|---|---|---|---|---|
| :793 | `_PyType_GetSubclasses` | `scan_refcounts` | borrowed_field_deref_across_call | medium |
| :2007 | `type_set_bases_unlocked` | `scan_ft_races` | publish_before_init_complete | medium |
| :3443 | `mro_implementation_unlocked` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :3894 | `apply_slot_updates` | `scan_init_bypass` | one_hop_param_deref | medium |
| :4815 | `type_new_set_attrs` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :5747 | `type_from_slots_or_spec` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :6002 | `get_base_by_token_recursive` | `scan_recursion_guards` | self_recursion | high |
| :6620 | `_Py_type_getattro_stackref` | `scan_ft_races` | publish_before_init_complete | low |
| :8655 | `type_add_method` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :8706 | `type_add_members` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :8731 | `type_add_getset` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :8816 | `overrides_hash` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :9166 | `type_dict_set_doc` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :9272 | `type_ready_mro` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :9388 | `type_ready_set_hash` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :9403 | `type_ready_add_subclasses` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :10516 | `add_tp_new_wrapper` | `scan_refcounts` | borrowed_field_deref_across_call | high |
| :12477 | `add_operators` | `scan_refcounts` | borrowed_field_deref_across_call | high |

## Pass-2 regions (from the pass-1 history agent)

The four most crash-fix-dense regions still unread: MRO C3 linearisation,
the type-attribute lookup cache, `getattro`/`setattro`, `__class__` assignment,
pickle/`__reduce__`, type watchers, managed static types.

The NET NEW table above is orthogonal to those regions — it is what the new
rules see anywhere in the file. Work both.
