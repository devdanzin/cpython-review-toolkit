# refcount-auditor — obj-mappings slice

Scope: `Objects/dictobject.c` (8,597 lines) + `Objects/setobject.c` (3,228 lines)
@ `4f3be1b5777`. Both files are **byte-identical** to the build-matrix commit
`a1d580430c8` (`git diff --stat` empty for both), so every crash below is against the
target ref.

---

## 1. Denominator

| quantity | value |
|---|---|
| `scan_refcounts.sample.json` findings | **0** |
| …out of `functions_analyzed` | 390 |
| …out of `borrowed_slot_load_sites` | 91 |
| …out of `borrowed_field_accessors` | **0** ← structural |
| Whole-`Objects/` population of the same rule | 20 |

The zero splits in two:

* **Structural.** `borrowed_field_accessors: 0` means the accessor-discovery rule found no
  `X_GET_ITEM`-style borrowed accessor defined in these two files, so
  `borrowed_field_deref_across_call` **cannot fire here at all**. dict and set express the
  identical hazard through `DK_ENTRIES(...)[ix].me_value`, `mp->ma_values->values[ix]`,
  `so->table`, and out-parameters of `_Py_dict_lookup` — none of which the rule models.
* **Evidential.** 91 borrowed-slot loads were examined and none matched
  `stale_slot_decref` / `owner_freed_before_use`, because the drop and the load are never
  adjacent at the same brace depth in this code — the Python-running call is a *third*
  statement between them (see §4).

Read by hand: **all 11,825 lines**, with close reads of the lookup, insert, delete, resize,
merge, copy, iterator, view, and managed-dict families. Manual candidate count: **63**
borrowed-load / index-capture sites audited against an intervening Python-reaching call;
14 `_PyDict_NotifyEvent` sites, 19 `_Py_dict_lookup` sites, 14 `_PyDict_Next` sites, 14
`set_next` sites, 4 `PyObject_RichCompareBool` sites in setobject.c.

Three new crashes reproduced. Scanner net-new contribution: **0**.

---

## 2. Findings

### [FIX] `_PyDict_NotifyEvent` executes `sys.unraisablehook` from inside 10 windows that hold stale state — `Objects/dictobject.c`, 10 sites

This is one defect with one root cause and ten expressions. Three are reproduced as hard
crashes on the **default release GIL build**, not merely under ASan.

**Mechanism.** `_PyDict_SendEvent` (`dictobject.c:8298`) calls each registered watcher
callback and then, at `:8314`, does:

```c
if (cb && (cb(event, (PyObject*)mp, key, value) < 0)) {
    PyErr_FormatUnraisable(
        "Exception ignored in %s watcher callback for <dict at %p>", ...);
}
```

`PyErr_FormatUnraisable` invokes `sys.unraisablehook` — **arbitrary Python, settable from
pure Python**. The dict-watcher contract (`Doc/c-api/dict.rst:582-584`) says:

> The callback may inspect but must not modify *dict* … Do not trigger Python code
> execution in the callback, as it could modify the dict as a side effect.

and two lines further down it *sanctions* the exact trigger:

> If the callback sets an exception, it must return ``-1``; this exception will be printed
> as an unraisable exception …

So a **fully conforming** callback — one that touches nothing and merely reports an error
the way the docs instruct — makes CPython itself run Python in the middle of every dict
mutation primitive. The contract constrains the callback; it does not constrain CPython's
own error reporting. Every caller of `_PyDict_NotifyEvent` was written as if it did.

**The ten windows.** Each captures state before `:NotifyEvent` and uses it after:

| # | site | captured before | used after | consequence |
|---|---|---|---|---|
| 1 | `insert_combined_dict:1917` | `dk_usable > 0` precondition (`:1910`) | `DK_*_ENTRIES(ma_keys)[dk_nentries]` (`:1925-1927`) | **heap-buffer-overflow WRITE** — reproduced |
| 2 | `_PyDict_DelItem_KnownHash_LockHeld:3038` | `ix`, borrowed `old_value` (`:3030`) | `delitem_common` (`:3039`) | **SIGABRT / SIGSEGV** — reproduced |
| 3 | `dict_popitem_impl:5051` / `:5066` | `ep0`, `i`, `key` (`:5043-5050`) | `:5052-5055`, `:5067-5071` | **heap-use-after-free R+W** — reproduced |
| 4 | `_PyDict_InsertSplitValue:1997`/`:2003` | `old_value = ma_values->values[ix]` (`:1995`) | `STORE_SPLIT_VALUE`, `Py_DECREF(old_value)` (`:1998`, `:2004`, `:2007`) | stale store + double free |
| 5 | `insertdict:2060` | `ix`, `old_value` (`:2038`) | `STORE_VALUE(ep,…)` (`:2064-2073`), `Py_XDECREF(old_value)` (`:2076`) | stale store + double free |
| 6 | `insert_to_emptydict:2103` | `assert(ma_values == NULL)` premise, `newkeys` | `FT_ATOMIC_STORE_PTR_RELEASE(mp->ma_keys, newkeys)` (`:2129`) | overwrites/leaks keys the hook installed |
| 7 | `delitemif_lock_held:3083` | `ix`, `old_value` (`:3070`) | `delitem_common` (`:3084`) | same as #2 |
| 8 | `clear_lock_held:3142` | `oldkeys`, `oldvalues` (`:3136-3137`) | `assert(oldkeys->dk_refcnt == 1)` (`:3148`), `dictkeys_decref(oldkeys)` (`:3149`) | double free of the keys object |
| 9 | `dict_dict_merge:4234` | `okeys`, `mp->ma_used == 0` premise (`:4219-4225`) | `set_keys`, `STORE_USED(mp, other->ma_used)` (`:4241-4242`) | silently drops + leaks hook-inserted entries |
| 10 | `store_instance_attr_lock_held:7510` | `ix`, `old_value` (`:7497`) | `values->values[ix]` store (`:7513`), `Py_DECREF(old_value)` (`:7530`) | stale store + double free |

**Guarded twin — and what its guard actually addresses (lesson 3).**
`dict_dealloc:3650-3658` is the **only** one of the fourteen `_PyDict_NotifyEvent` sites in
the file that is safe, and it is safe for the right structural reason: it brackets the event
with `_PyObject_ResurrectStart(self)` / `_PyObject_ResurrectEnd(self)` and reads
`mp->ma_values` / `mp->ma_keys` at `:3657-3658`, i.e. **after** the notification, capturing
nothing across it.

But its guard was written for a *different* threat. `_PyObject_ResurrectStart/End` defends
against the callback taking a new reference and resurrecting the dict; the comment at
`_PyDict_SendEvent:8309-8312` confirms that is the modelled adversary —

> We don't want to resurrect the dict by potentially having an unraisablehook keep a
> reference to it, so we don't pass the dict as context …

The authors explicitly identified `sys.unraisablehook` as hostile **and reasoned only about
it retaining a reference**, not about it mutating the dict. `dict_dealloc`'s mutation-safety
is incidental to the resurrection guard. That is why the other ten sites, which had no
resurrection concern, got no guard at all. Citing `dict_dealloc` as "the fix" is correct
only if you take the *read-after-notify ordering*, not the resurrect bracket.

The sibling shape is already catalogued for the type machinery: CPY-0084 and CPY-0098 are
`_PyType_Modified_Unlocked` holding borrowed `tp_subclasses` across a type-watcher callback
**and `PyErr_FormatUnraisable("%R")`**. This is the dict half of the same class; nothing in
the catalog covers dictobject.c's ten.

**Reproduction.** Reproducers in `reports/obj-mappings/repro/`. All use
`_testcapi.add_dict_watcher(1)` — the in-tree `dict_watch_callback_error`, which sets a
`RuntimeError` and returns `-1` and touches nothing else — plus a pure-Python
`sys.unraisablehook`.

| repro | build | result |
|---|---|---|
| `dict_notify_popitem_uaf.py` | `debug-gil-nojit-asan` | **5/5** `heap-use-after-free` READ, `unicode_get_hash` ← `dict_popitem_impl:5052`, freed by `clear_lock_held:3149` ← `PyErr_FormatUnraisable` ← `_PyDict_SendEvent:8314` ← `dict_popitem_impl:5051` |
| `dict_notify_popitem_uaf.py` | `release-gil-nojit` | **3/3** SIGSEGV (exit 139) |
| `dict_notify_delitem_stale.py` | `debug-gil-nojit-asan` | **5/5** SIGABRT — `Objects/dictobject.c:2963: Assertion 'hashpos >= 0' failed` in `delitem_common` |
| `dict_notify_delitem_stale.py` | `release-gil-nojit` | **3/3** SIGSEGV (exit 139) |
| `dict_notify_insert_dk_usable_oob.py` | `debug-gil-nojit-asan` | **5/5** `heap-buffer-overflow` **WRITE** of 8 bytes, `0 bytes after` the 232-byte keys block, at `insert_combined_dict:1927` |
| `dict_notify_insert_dk_usable_oob.py` | `debug-gil-nojit` | 1/1 SIGABRT — `Objects/dictobject.c:1938: Assertion 'mp->ma_keys->dk_usable >= 0' failed` |
| `dict_notify_insert_dk_usable_oob.py` | `release-gil-nojit` | 3/3 **exit 0 — silent heap corruption** (the OOB entry lands in malloc slack) |

Note the last row honestly: on release, #1 is *silent*. The debug assert at `:1938` and ASan
are the only things that see it.

**Relationship to CPY-0096.** The brief hands this slice three inherited consequences of
CPY-0096 (ASan heap-buffer-overflow, `dk_usable` invariant break, unbounded recursion).
Window #1 above is the same *consequence* reached by a different and much shorter route:
CPY-0096 gets to arbitrary Python through `insert_split_key` → `_PyType_Modified_Unlocked`
→ type watcher; this reaches it through `insert_combined_dict` → dict watcher →
`PyErr_FormatUnraisable`. The `dk_usable` invariant break is therefore reachable on
**combined** dicts, not only split ones, and is reproduced here 5/5 as an actual
out-of-bounds write rather than only as an invariant violation.

**Fix.** Two options, of increasing cost:

1. Move every `_PyDict_NotifyEvent` call to a point where no state is live across it — i.e.
   notify *before* the lookup, or re-read `ma_keys` / `ix` / `old_value` after it. This is
   what `dict_dealloc` does by accident.
2. Better, because it fixes all ten at once and every future one: stop running Python inside
   the notification. Defer the `PyErr_FormatUnraisable` in `_PyDict_SendEvent:8310-8315`
   until the mutation completes, or use `_PyErr_WriteUnraisableDefaultHook` directly
   (bypassing `sys.unraisablehook`) for this specific report. The docs already promise the
   callback will not run Python; making CPython honour the same promise for its own error
   path is the smaller, safer change.

The `%R`-formatting variant of this same problem was already avoided here (`:8311`, "Dict
repr can call arbitrary code, so we invent a simpler version") — the hook call is the one
that was missed.

---

### [CONSIDER] `set_compare_frozenset` documents an invariant CPython itself violates — `Objects/setobject.c:171-193`

**Verdict on the UAF: not reachable. Here is what I eliminated.**

`set_compare_frozenset` drops both protections its sibling `set_compare_entry_lock_held`
(`:137-165`) has — no `Py_INCREF(startkey)` across `PyObject_RichCompareBool` (`:185`), and
no post-compare `table != so->table || entry->key != startkey` revalidation — on the stated
grounds that a frozenset cannot change.

Routes tested (`repro/frozenset_compare_no_incref_probe.py`, run on
`debug-gil-nojit-asan` and `release-gil-nojit`, both exit 0, all SURVIVED):

* **Free `startkey` by dropping the frozenset.** Eliminated. The frozenset holds the only
  strong reference to each element, so `startkey` dies only if the frozenset's table is torn
  down — `set_clear_internal` (via `tp_clear`, registered at `:3008`) or `set_dealloc`.
  Both require the frozenset to be unreachable, and it is always on the interpreter value
  stack with a strong reference during the lookup, so `gc_refs > 0` makes it a GC root.
  Probed with a tracked `frozenset → dict → frozenset` cycle plus a double `gc.collect()`
  inside `__eq__`: survives, correct answer.
* **`PySet_Add` on a live frozenset.** Eliminated by the code: `:3118` gates the frozenset
  branch on `_PyObject_IsUniquelyReferenced(anyset)`. `marshal.c:1552` — the only in-tree
  caller that fills a frozenset — is explicitly written around this ("must use delayed
  registration of frozensets because they must be init with a refcount of 1"). Round-trip
  probed, survives.
* **`_PySet_Freeze`.** Eliminated. Its single caller, `make_frozenset` in
  `Python/intrinsics.c:211-216`, asserts `PySet_CheckExact(set)` **and**
  `_PyObject_IsUniquelyReferenced(set)`; the set is a compiler-built constant with refcount
  1, so no mutable alias survives the freeze. The coordinator's premise that `_PySet_Freeze`
  falsifies frozenset immutability does not hold for any reachable call.
* **`set_swap_bodies`.** Eliminated. Its only two callers (`:1846`, `:1870`) reach it from
  `set.intersection_update` / `set_iand`, both of which live only in `set_methods` /
  `set_as_number`, never in `frozenset_methods` / `frozenset_as_number`. Probed all nine
  mutating `set.*` methods called unbound with a `frozenset` receiver: **all nine raise
  TypeError**.
* **`set_table_resize` under the compare.** Eliminated: a frozenset resizes only while being
  filled, and filling never goes through `set_lookkey`/`set_compare_frozenset` — the insert
  path (`set_add_entry_takeref:264-335`) has its own inline loop with its own INCREF and
  restart.

**But the comment states the wrong reason, and that is the finding.** CPython *does* mutate
an exact frozenset's table in place, through this very comparator. `frozenset - frozenset`
with `len(so) >> 4*len(other)` takes `set_difference_untracked:2105` →
`set_copy_and_difference_untracked:2074` → `set_copy_untracked_lock_held` → an **exact
frozenset** copy → `set_difference_update_internal:2008` → `set_discard_entry:584` →
`set_lookkey:417` → `PyFrozenSet_CheckExact` true → `set_compare_frozenset`, after which
`set_discard_entry:592-596` writes `entry->hash = -1`, `entry->key = dummy` and
`Py_DECREF(old_key)` through the returned pointer.

Confirmed under gdb on `debug-gil-nojit` with `break set_discard_entry if
((PyObject*)so)->ob_type == &PyFrozenSet_Type`:

```
receiver type = frozenset  used=200 fill=200
#1  set_difference_update_internal (…) at Objects/setobject.c:2008
#2  set_copy_and_difference_untracked (…) at Objects/setobject.c:2077
#3  set_difference_untracked (…) at Objects/setobject.c:2106
#4  set_sub (…) at Objects/setobject.c:2211
```

So the real safety property is **"this particular frozenset is not reachable from Python"**,
not **"frozensets are immutable"**. Those are different invariants with different failure
modes, and the file records the weaker, false one. The next person who makes an
exact-frozenset intermediate reachable before draining it — e.g. by GC-tracking
`set_copy_and_difference_untracked`'s result earlier, or by adding a `frozenset` method that
returns a partially-drained copy — converts this into a live use-after-free with no comment
warning them off. Same lesson-3 shape as the `dict_dealloc` twin above.

**Suggested change:** amend the comment at `:167-170` to state the actual precondition
("callers must guarantee `so` is not reachable from Python for the duration of the lookup"),
and add `assert(PyFrozenSet_CheckExact(so))` plus a debug-only reachability assertion, so the
constraint is machine-checked rather than folklore. No functional change; classification
CONSIDER, not FIX, because nothing is broken today.

---

### [CONSIDER] `PyObject_ClearManagedDict` runs `sys.unraisablehook` while the object is being freed — `Objects/dictobject.c:8013`

Same root as the FIX above but a different reporter. `PyObject_ClearManagedDict`'s own
comment (`:7989-7990`) says it "is called when the object is being freed or cleared by the
GC and therefore known to have no references", and then at `:8013` calls
`PyErr_FormatUnraisable("Exception ignored while clearing an object managed dict")` — pure
Python — before `Py_CLEAR(_PyObject_ManagedDictPointer(obj)->dict)` at `:8027`. Reachable
only on the `detach_dict_from_object` OOM branch, so an OOM-sweep target rather than
something I can drive from Python. Not reproduced.

---

### [CONSIDER] `dict_dict_merge`'s mutation guard tests size, not identity — `Objects/dictobject.c:4290-4294`

`while (_PyDict_Next(other, &pos, &key, &value, &hash))` runs `insertdict` (user `__eq__`,
`__hash__`) each iteration and then checks `orig_size != other->ma_used` → `RuntimeError
"dict mutated during update"`. A callback that deletes one key and adds another leaves
`ma_used` unchanged while moving the `pos` cursor's meaning, so entries are silently
duplicated or skipped. Not memory-unsafe — `key` and `value` are `Py_INCREF`'d at
`:4266-4267` before any Python runs, and `_PyDict_Next` re-reads `mp->ma_keys` on every call
so the cursor never dangles. Correctness only. The guarded twin is
`dictiter_iternextkey_lock_held:5763-5767`, which additionally tests `di->len == 0` and
raises `"dictionary keys changed during iteration"` — a content check the merge loop lacks.

---

### [ACCEPTABLE] `_Py_dict_lookup`'s restart loop, and the set restart loop, are correct

`_Py_dict_lookup:1366-1421` re-reads `dk = mp->ma_keys` at the `start:` label and restarts on
`DKIX_KEY_CHANGED`; `compare_generic:1229` / `compare_unicode_generic:1173` validate
`dk == mp->ma_keys && ep->me_key == startkey` **after** every `PyObject_RichCompareBool` and
`Py_INCREF(startkey)` before it. `*value_addr` is read after the loop returns
(`:1401`, `:1414`), so the value is always fresh. This is the correct template and the
cross-file twin the coordinator already identified for `set_add_entry_takeref`. 19 call
sites audited; no stale use of `ix`/`old_value` except through `_PyDict_NotifyEvent`.

Residual, not reported as a finding: the `dk == mp->ma_keys` identity test is an ABA
comparison. A callback that clears and repopulates the dict such that `PyMem_Malloc` returns
the same address *and* index `ix` again holds `startkey` would pass. I could not construct
it and it is not distinguishable from correct behaviour in practice.

---

### One-line confirmations of recorded findings

* **CPY-0015** — `dictiter_new:5646` still `Py_DECREF(di)`s a never-tracked iterator whose
  `dictiter_dealloc:5662` uses the asserting `_PyObject_GC_UNTRACK`. Present, unchanged.
* **CPY-0079** — `copy_lock_held_untracked:4494` `assert(!_PyObject_GC_IS_TRACKED(d))` on the
  unchecked result of `dict_new_untracked`/`frozendict_new_untracked`. Present, unchanged;
  its OOM sweep still needs re-running on a real denominator.
* **CPY-0096** — `insert_split_key:1971` still calls `_PyType_Modified_Unlocked` under
  `LOCK_KEYS`. Present; see the FIX above for the combined-dict route to the same
  `dk_usable` consequence.
* **CPY-0107** — `compare_unicode_generic:1168` still runs `PyObject_RichCompareBool` under
  `LOCK_KEYS_IF_SPLIT` (`_Py_dict_lookup:1385`). Present, unchanged.
* **CPY-0115** — `delitem_common:2987` unchanged. My FIX #2/#7 hit the same function by a
  different (single-threaded, re-entrant) route; they are separate defects.
* **`set_add_entry_takeref:290-295`** — confirmed by reading: `if (cmp > 0) goto found_active;`
  precedes the `table != so->table || entry->key != startkey` test, so the guard is dead on
  the match path. Already reproduced by the coordinator 4/4; not re-litigated.

---

## 3. Classes bounded clean *here*, with denominators

| class | denominator | verdict |
|---|---|---|
| Leaked new reference on an error path | 167 `Py_*DECREF`/`Py_CLEAR` + 98 `Py_*INCREF`/`Py_NewRef` sites in dictobject.c; 87 + 27 in setobject.c | clean. Every `goto error`/`fail` label audited: `dictitems_xor_lock_held:6864-6870`, `_PyDictView_Intersect:6765-6769`, `set_intersection:1796-1800`, `merge_from_seq2_lock_held:4172-4178`, `anydict_repr_impl:3761-3766`, `set___reduce___impl:2736-2740` all balance. |
| CPY-0015 shape (`Py_DECREF` of a never-tracked object) in **new** constructors | 11 constructors audited: `make_new_set_untracked:1350`, `make_new_set_basetype_untracked`, `set_copy_untracked_lock_held:1581`, `set_intersection:1729`, `set_difference_untracked:2109`, `set_union_impl:1665`, `set_symmetric_difference_impl:2361`, `_PyDictView_New:6475`, `copy_lock_held_untracked:4474`, `dictitems_xor_lock_held:6793`, `dictiter_new:5617` | **10 of 11 clean, 1 = CPY-0015.** setobject.c's `make_new_set_untracked:1370` DECREFs an untracked set, but `set_dealloc:742` uses the *public* `PyObject_GC_UnTrack`, which is untracked-safe (`Python/gc.c:1948-1956`, `if (_PyObject_GC_IS_TRACKED(op))`); `subtype_dealloc` uses the same guarded form, so the subclass path via `make_new_frozenset:1434` is safe too. `setiter_dealloc:1044`, `dictview_dealloc:6437` and `dictiter_dealloc:5662` *do* use the asserting `_PyObject_GC_UNTRACK` macro, but only `dictiter_new` has a fallible step (`_PyTuple_FromPairSteal:5644`) between the allocation and `_PyObject_GC_TRACK` — `set_iter:1175-1182` and `_PyDictView_New:6475-6479` are pure stores, so no untracked-DECREF path exists there. |
| Borrowed ref across a user `__eq__`/`__hash__` in setobject.c | 4 `PyObject_RichCompareBool` sites (`:112`, `:155`, `:185`, `:288`) and all 14 `set_next` loops | clean. Every `set_next` consumer INCREFs `entry->key` *before* the call (`:1746`, `:1945`, `:2007`, `:2117`, `:2140`, `:2276`, `:2435`), and `set_next:719-720` re-reads `so->table`/`so->mask` on every call so the cursor never dangles across a resize. |
| Borrowed ref across a user `__eq__` in the dict lookup family | 19 `_Py_dict_lookup` call sites | clean apart from the `_PyDict_NotifyEvent` windows — see the ACCEPTABLE entry above. |
| Stolen-reference asymmetry in the insert family | `insertdict:2019` (`Fail:` decrefs both), `insert_to_emptydict:2090` (both on the `new_keys_object` failure), `_PyDict_InsertSplitValue:1990`, `insert_combined_dict:1899`, `_PySet_AddTakeRef:362`, `set_add_entry_takeref:264` (`found_active`/`comparison_error` both `Py_DECREF(key)`) | clean. All consume-on-all-paths as documented. `PyModule_AddObject`-style half-stealing does not occur in either file. |
| `_PyTuple_FromPairSteal` failure handling | `dictiter_new:5644`, `dictitems_xor_lock_held:6838`, `dictiter_iternextitem:6210` | clean; steals on both outcomes and the callers null their locals. |
| Iterator result-tuple recycling | `dictiter_iternextitem:6197-6207`, `dictreviter_iter_lock_held:6314-6327` | clean. `Py_DECREF(oldkey)`/`Py_DECREF(oldvalue)` do run `__del__`, but `result` is INCREF'd first (`acquire_iter_result:6174-6178` / `:6321`), so a re-entrant `next()` sees refcount 2, fails `_PyObject_IsUniquelyReferenced`, and allocates a fresh tuple instead of stealing the live one. |
| "Allocation can trigger GC, which can run `__del__`" | `copy_lock_held_untracked:4509` (`PyObject_GC_New` between the un-increfed `copy_values:4500` and the `Py_XINCREF` loop at `:4515`); `items_lock_held:3975`; `dict_popitem_impl:5022` | **clean, and the comments claiming otherwise are stale.** On main, `_PyObject_GC_Link` (`Python/gc.c:1974-1993`) only calls `_Py_ScheduleGC`, setting the eval-breaker; the collection runs at the next bytecode boundary, never inline. So `PyObject_GC_New` is **not** a Python-reaching call. `copy_lock_held_untracked`'s split path would otherwise be a live UAF (it holds `capacity` un-owned pointers across the allocation), and its guarded twin `clone_combined_dict_keys:1036-1071` shows the safe ordering — `PyMem_Malloc`, `memcpy`, INCREF, with no allocation in between. Worth a comment refresh; the `dict_popitem_impl:5013-5021` and `items_lock_held:3965-3968` comments now describe a hazard the deferred-GC design removed. |
| Managed-dict / inline-values ownership transfer | `detach_dict_from_object:7970-7978`, `invalidate_and_clear_inline_values:2166-2174`, `clear_inline_values:7773-7781`, `dictresize:2229-2272`, `_PyObject_SetManagedDict:7872` | clean. `invalidate_and_clear_inline_values` NULLs without decref, transferring ownership to the copy; `clear_inline_values` `Py_CLEAR`s because it is the terminal path. The pairing is correct on every branch. |
| `dict_equal_lock_held` | `:4663-4722` | clean — INCREFs `aval`, `key`, and `bval` before `PyObject_RichCompareBool:4713`, and re-reads `a->ma_keys` and `LOAD_KEYS_NENTRIES` on every loop iteration. This is the textbook version of the pattern the ten NotifyEvent sites get wrong. |

---

## 4. Toolkit feedback

**Recall gap 1 — `_PyDict_NotifyEvent` is not in `PYTHON_REACHING_APIS` (highest value).**
Fourteen call sites in this file alone, ten of them holding live state. The scanner cannot
see it because it is a static inline in `pycore_dict.h` whose Python-reaching-ness comes from
a function pointer (`interp->dict_state.watchers[i]`) *plus* an error-reporting call two
frames down. Concrete proposal: add `_PyDict_NotifyEvent`, `_PyDict_SendEvent`,
`_PyType_Modified`, `_PyType_Modified_Unlocked`, `PyErr_FormatUnraisable`,
`_PyErr_FormatUnraisable`, `PyErr_WriteUnraisable`, and `_PyErr_WriteUnraisable` to the
table. `PyErr_FormatUnraisable` alone is the mechanism behind CPY-0084, CPY-0098, CPY-0096
and all three crashes here — it is the single highest-leverage entry the table is missing.

**Recall gap 2 — the `borrowed_field_deref_across_call` rule requires an accessor
*definition* in the scanned file.** `borrowed_field_accessors: 0` for a 11,825-line pair of
files that is nothing but borrowed field access. Proposal: seed the accessor set from
`Include/internal/pycore_dict.h` / `pycore_setobject.h` as well as the `.c` file, and add
three literal patterns that cover this slice: `DK_ENTRIES(<x>)[<i>]`,
`DK_UNICODE_ENTRIES(<x>)[<i>]`, `<x>->ma_values->values[<i>]`, `<x>->table[<i>]`.

**Recall gap 3 — model the "index captured before a call, dereferenced after" shape, not just
the pointer shape.** Seven of the ten windows are stale *`Py_ssize_t ix`*, not a stale
pointer. A rule of the form "an integer assigned from `_Py_dict_lookup`/`insert_split_key`
and later used as an array subscript, with a Python-reaching call in between" would have
found #2, #4, #5, #7, #10 mechanically.

**Recall gap 4 — out-parameters of lookup functions are borrowed and the scanner does not
know it.** `_Py_dict_lookup(mp, key, hash, &old_value)` yields a *borrowed* `old_value`;
`_Py_dict_lookup_threadsafe` at `:1757` yields an *owned* one. Same-shaped call, opposite
ownership, one letter apart in the name. Proposal: add an out-parameter ownership table
keyed on `(function, arg_index)`; seed it with these two plus `_PyDict_Next`/`PyDict_Next`
(borrowed key+value), `_PySet_NextEntry` (borrowed) vs `_PySet_NextEntryRef` (owned) — that
last pair is another same-name-different-ownership trap in this slice.

**Precision fact for calibration — allocation is *not* a Python-reaching call on main.**
`_PyObject_GC_Link` only schedules a collection. Any rule treating `PyObject_GC_New` /
`PyType_GenericAlloc` / `PyMem_Malloc` as arbitrary-Python will produce false positives; I
chased one to a dead end here (`copy_lock_held_untracked:4509`). Recommend documenting this
in `data/cpython_non_bugs.md` — CPython's own comments in `dict_popitem_impl` and
`items_lock_held` still assert the opposite, so an agent reading the source will be misled.

**Precision fact 2 — a documented contract is not a closed class if CPython violates it
itself.** The dict-watcher docs forbid the *callback* from running Python. A naive triage
reads that and marks all fourteen sites ACCEPTABLE. The bug is that CPython runs Python
after the callback. Proposal for `cpython_non_bugs.md`: when dismissing on a documented
caller contract, check whether the runtime's own error/reporting path re-enters — that is
where three reproduced crashes were hiding.

---

## 5. Noticed outside slice

* `Python/gc.c:1948-1956` — `PyObject_GC_UnTrack` is guarded; `_PyObject_GC_UNTRACK` is not.
  The CPY-0015/CPY-0011 class is exactly the set of constructors that reach the second form.
  A one-line scanner rule ("`Py_DECREF` of an object allocated by `PyObject_GC_New` before
  `_PyObject_GC_TRACK`, where `tp_dealloc` uses `_PyObject_GC_UNTRACK`") would find them all.
* `Python/errors.c:1737` (`format_unraisable_v` → `PyObject_CallOneArg`) is the single choke
  point through which `sys.unraisablehook` reaches every one of these sites; a `Py_ssize_t`
  re-entrancy depth guard there would blunt the whole family.
* `Objects/setobject.c:361-373` `_PySet_AddTakeRef` takes no critical section and carries no
  `_Py_CRITICAL_SECTION_ASSERT_OBJECT_LOCKED`, while `set_add_entry:340` asserts one and
  `PySet_Add:3112` takes one. Called unlocked from `Python/bytecodes.c:1383` and `:2565`
  (`SET_ADD`, `BUILD_SET`). FT lock-discipline, not refcount — for that agent.
* `Objects/setobject.c:1129-1133` `setiter_iternext` open-codes `si->si_set = NULL;
  Py_DECREF(so);` outside the critical section it just closed at `:1127`; the three
  `dictiter_*_lock_held` `fail:` labels (`:5772-5775`, `:5895-5898`, `:6027-6030`) and
  `dictiter_iternext_threadsafe:6157-6160` do the same. `Py_CLEAR` re-reads the field and is
  the guarded twin (`calliter_iternext` in `Objects/iterobject.c`). FT double-DECREF class —
  for `ft-race-scanner`; single-threaded GIL re-entrancy is not reachable here because no
  Python runs between the load and the drop.
