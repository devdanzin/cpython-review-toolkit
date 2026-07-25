# ft-race-scanner — `Objects/typeobject.c` PASS 2

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777313fb36ff4bda7e4a4197c932c30e` (3.16.0a0)
**Scope:** pass-2 regions — MRO C3, lookup cache, getattro/setattro, `__class__` assignment,
pickle, watchers/version tags, managed static types, `super` lookup surface.
**Mode:** INFORMED. Briefing FP taxonomy applied; pass 1's `ft-race-scanner.md` read and **not** redone.
**Repro builds:** `~/projects/python_build_matrix/builds/{debug,release}-ft-nojit{,-asan,-tsan}` at `a1d580430c8`.
`Objects/typeobject.c`, `Objects/dictobject.c`, `Python/optimizer_analysis.c` and
`Modules/_testcapi/watchers.c` are **byte-identical** between `a1d580430c8` and the target ref
(`git diff --quiet` clean on all four), so every line number quoted from a sanitizer stack
transfers to the target ref unchanged.

---

## Summary

| class | candidates | verdict |
|---|---|---|
| T4 `publish_before_init_complete` — `:2007` `type_set_bases_unlocked` (medium) | 1 | **FALSE POSITIVE** |
| T4 `publish_before_init_complete` — `:6620` `_Py_type_getattro_stackref` (low) | 1 | **FALSE POSITIVE** |
| T4 `publish_before_init_complete` — `:4958` `type_new_impl` (low) | 1 | CPY-0072, confirmed in one line |
| T1 `guarded_writer_unguarded_reader` — `:518` `is_readying` (low) | 1 | pass 1 FP, re-confirmed |
| T2 `lazy_init_no_critical_section` — `:5701` `type_from_slots_or_spec` (medium) | 1 | pass 1 FP, re-confirmed |
| **Found by reading, not by the scanner** | **8** | **4 FIX (3 reproduced), 3 CONSIDER (1 reproduced), 1 clean negative** |
| **Environment finding** | **1** | **FT ASan builds are blind to `PyObject` heap overflows — campaign-level, invalidates prior evidence (E1)** |

| # | verdict | finding |
|---|---|---|
| 3 | **FIX** | `Py_TPFLAGS_INLINE_VALUES` set post-publication + double-read in `_PyType_AllocNoTrack` ⇒ **heap-buffer-overflow WRITE**. Sibling of CPY-0072, **not** covered by PR gh-151394. **Reproduced: ASan stack 3/3 with allocation at `:2528` and overflow at `:2551` on one thread; 20/20 crash on plain FT, 0/10 on GIL. Attributes pass 1's unattributed corruption.** |
| 4 | **FIX** | `type_ready` makes two further plain `tp_flags` writes after its own publication point, read unlocked on the attribute-lookup hot path. |
| 6 | **FIX** | `PyType_Unwatch:1129` writes `tp_watched` with no lock; `PyType_Watch:1112` writes it under `TYPE_LOCK`. Guarded twin 17 lines away. **Reproduced: 3 TSan pairs + an observable lost update (5/40 FT, 0/40 GIL).** |
| 7 | **FIX** | `_PyType_Modified_Unlocked` runs arbitrary Python (4 entry points) while `insert_split_key` holds the raw non-reentrant keys mutex — **which `dictobject.c:218–227` forbids by name**. 19 days old. **Reproduced: 24/24 FT deadlock vs 0/12 GIL, plus an ASan heap-buffer-overflow, a broken `dk_usable` invariant and unbounded recursion — all three on the DEFAULT GIL build.** |
| 5 | CONSIDER | `type_from_slots_or_spec` mutates a published type's `tp_dict` outside `type_setattro`. |
| 8 | CONSIDER | `_PyType_SetFlags:6453` lacks all three protections its twin at `:6506` documents. |
| 9 | CONSIDER | `type_cache_clear:988–990` — plain stores, inverse order vs `update_cache`. **Reproduced: 24 TSan races, GIL control clean.** |
| 10 | ACCEPTABLE | Lookup-cache entry lifecycle is sound — clean negative, with the reason. |

**Scanner precision on the two NET NEW T4 candidates: 0/2.** Both are false positives, and both
fail on the *same* modelling error (below). The three T4 candidates in the envelope are the
scanner's entire T4 output on this file; one (`:4958`) is the true positive already recorded as
CPY-0072, so the rule's overall precision here is **1/3**.

**Denominators** (from `scanners/scan_ft_races.sample.json`, slice-scoped): `functions_analyzed=459`,
`iternext_functions=1`, `lock_held_functions=0`, `files_with_ft_regions=1`, 5 findings.
The `lock_held_functions=0` is the D2 defect pass 1 raised and it is **still unfixed** — see
Toolkit assessment.

---

## Answering the brief's question 2 directly

CPY-0072's record states gh-151394 closes the `fixup_slot_dispatchers` path but misses three
siblings: `_PyType_GetSubclasses:783`, `recurse_down_subclasses:12369` (GIL-build, out of scope),
and `type_set_flags:458`.

- **The two NET NEW T4 sites are neither among those three nor additional.** Both are false
  positives (Findings 1 and 2).
- **`type_set_flags:458` is not a site, it is a *primitive*.** I resolved it into its three
  concrete post-publication call sites (Finding 3), and one of them — `Py_TPFLAGS_INLINE_VALUES`
  at `:9487` — has a **heap-buffer-overflow WRITE** consequence via a double-read in
  `_PyType_AllocNoTrack`. That is the mechanism pass 1 reproduced 4/4 but explicitly could not
  attribute.
- **Two further siblings gh-151394 does not cover**, found by reading: the `ht_cached_keys` store
  at `:9480` (same function), and `type_from_slots_or_spec`'s post-`PyType_Ready` `tp_dict`
  mutations at `:5733–5782` (Finding 5).

I fetched and read the gh-151394 diff (`gh api repos/python/cpython/pulls/151394`, still **open,
unmerged, last touched 2026-07-03**). It changes exactly three things: `TYPE_IS_REVEALED` /
new `TYPE_SET_REVEALED` made atomic; `fixup_slot_dispatchers` converted to queue-and-apply under
`apply_type_slot_updates` and returning `int`; and `remove_all_subclasses` in
`type_dealloc_common` wrapped in `BEGIN_TYPE_LOCK()`. It touches neither `type_ready`,
`type_ready_managed_dict`, nor `_PyType_AllocNoTrack`.

---

## FINDING 1 — [ACCEPTABLE / FALSE POSITIVE] `:2007 type_set_bases_unlocked` (T4, medium)

**Scanner claim:** `type` is published at `add_all_subclasses()` (`:1966`) and `tp_base` is still
written afterwards at `:2007` with a plain non-atomic store and no critical section.

**False positive on two independent grounds.**

**(a) The store *is* under stop-the-world.** The flagged line does not stand alone:

```c
  bail:
    if (lookup_tp_bases(type) == new_bases) {
        assert(type->tp_base == best_base);

        type_lock_prevent_release();          // :2004
        types_stop_world();                   // :2005
        set_tp_bases(type, old_bases, 0);     // :2006
        type->tp_base = old_base;             // :2007  <-- flagged
        types_start_world();                  // :2008
        type_lock_allow_release();            // :2009
```

This is the *model citizen* of the file's documented discipline (design comment `:73–77`:
slot/flag updates are done "before the type has been revealed … or … while the stop-the-world
mechanism is active"), and it is one of only five sites in the file that also uses
`type_lock_prevent_release()` correctly. The forward path at `:1938–1943` is the identical
bracket. The scanner cannot see it because `types_stop_world()` is a `static void` **function**
wrapping `_PyEval_StopTheWorld`, not a macro — the include_map's blind spot §4.2 #1, which
`scan_stw_safety` also suffers (it sees 3 of the file's 11 stop-the-world regions).

**(b) The publish premise is inverted.** `type_set_bases_unlocked` is reachable only through
`T.__bases__ = …` (`type_set_bases:2024` ⇐ `type_getsets`). `type` is therefore a long-lived,
long-published type; `add_all_subclasses` at `:1966` *re-links an existing type into new bases'*
`tp_subclasses`, it does not publish a new object. The T4 rule's model — "pointer published to
shared state before its fields are initialised" — does not apply. Additionally, `:2007` is on the
`bail:` rollback path, reached only after the forward path already failed.

**Confirm, don't re-litigate:** CPY-0070 lives at `:1965/:1966` in this same function
(`res = add_all_subclasses(...)` at `:1966` is assigned but never branched on, so the rollback is
skipped and `__bases__` is committed with `MemoryError` pending). Unchanged at the target ref,
still live.

---

## FINDING 2 — [ACCEPTABLE / FALSE POSITIVE] `:6620 _Py_type_getattro_stackref` (T4, low)

**Scanner claim:** `type` is published at `PyType_Ready()` (`:6586`) and `*slot` is still written
afterwards in `_PyType_LookupStackRefAndVersion():6359`, 3 call hops away.

**False positive on two independent grounds.**

**(a) The publish premise is inverted, again.** `:6586` is

```c
    /* Initialize this type (we'll assume the metatype is initialized) */
    if (!_PyType_IsReady(type)) {
        if (PyType_Ready(type) < 0)
```

— a *lazy readiness check* on a type the caller already holds a reference to, because this
function is `type.__getattribute__`; you reach it by evaluating `T.x`. It is not a construction
site, and `PyType_Ready` returns 0 immediately when `Py_TPFLAGS_READY` is set (`:9613`), which it
is for every type reachable from Python. The rule keyed on the *name* `PyType_Ready` as a
publishing API without checking that the object is newly allocated in this frame.

**(b) The flagged write is `tp_version_tag`, under `TYPE_LOCK`.** `:6359` is
`assign_version_tag(interp, type)` — and it sits inside:

```c
    if (should_assign_version_tag(type, name, version_tag)) {
        BEGIN_TYPE_LOCK();                          // :6358
        assign_version_tag(interp, type);           // :6359  <-- flagged (3 hops)
        version_tag = type->tp_version_tag;
        res = find_name_in_mro(type, name, out);
        END_TYPE_LOCK();                            // :6362
    }
```

`assign_version_tag` opens with `ASSERT_TYPE_LOCK_HELD()` (`:1410`) and writes only through
`set_version_unlocked`. `tp_version_tag` is the *first* field the design comment at `:65–68` names
as protected by `TYPE_LOCK`. The scanner mislabelled the member as
`type.*slot (computed from tp)` because it lost the field identity across three call hops.

**Not a T4, but there IS a real hazard in this function** — outside this agent's class.
`metatype = Py_TYPE(type)` is captured once at `:6574` (borrowed) and used at `:6610` and `:6660`,
*after* the descriptor `__get__` calls at `:6609` and `:6641` have run arbitrary Python. That is a
borrowed-field-across-call shape that `scan_refcounts` cannot see, because it keys
`borrowed_field_deref_across_call` on the four `lookup_tp_*` accessors and `Py_TYPE()` is not one.
Recorded in the include_map §4.3; belongs to refcount-auditor.

---

## FINDING 3 — [FIX — **REPRODUCED**: ASan heap-buffer-overflow WRITE, 3/3; 20/20 crash on plain FT, 0/10 on GIL] `Py_TPFLAGS_INLINE_VALUES` is set on an already-published type, and `_PyType_AllocNoTrack` reads it twice

**Sites:** `Objects/typeobject.c:9487` (write), `:9480` (write), `:2524` and `:2550` (the two
reads), `Objects/dictobject.c:7306` (`_PyObject_InitInlineValues`, the overflowing writer).
**Class:** T4 `publish_before_init_complete`, sibling of CPY-0072. **Not covered by gh-151394.**

### The publication point and the write

```c
static int
type_ready(PyTypeObject *type, int initial)
{
    ...
    if (type_ready_add_subclasses(type) < 0) {   // :9581  PUBLISHES: add_subclass() inserts a
        goto error;                              //         weakref into every base's tp_subclasses,
    }                                            //         so Base.__subclasses__() now returns it
    if (initial) {
        if (type_ready_managed_dict(type) < 0) { // :9585
```

and `type_ready_managed_dict` (`:9466`) is:

```c
    PyHeapTypeObject* et = (PyHeapTypeObject*)type;
    if (et->ht_cached_keys == NULL) {
        et->ht_cached_keys = _PyDict_NewKeysForClass(et);   // :9480  plain pointer store
        ...
    }
    if (type->tp_itemsize == 0) {
        type_add_flags(type, Py_TPFLAGS_INLINE_VALUES);     // :9487  plain tp_flags store
    }
```

`type_add_flags` → `type_set_flags:455` → `ASSERT_WORLD_STOPPED_OR_NEW_TYPE(tp); tp->tp_flags = flags;`.
The world is **not** stopped, and the type is **not** new — it was published six lines earlier.
The assertion passes only because `_Py_TYPE_REVEALED_FLAG` is not set until `type_new_impl:4982`
(or `type_from_slots_or_spec:5787`), i.e. **the assertion is measuring the wrong event**. That is
precisely what CPY-0072 records about `fixup_slot_dispatchers`; this is the same defect one
function earlier, *inside* `PyType_Ready` itself, and therefore upstream of everything gh-151394
fixes.

### Why it is memory-unsafe, not merely stale

`_PyType_AllocNoTrack` — reached by any thread evaluating `SomeClass()` — reads that flag **twice**,
non-atomically, and uses the first read to size the allocation and the second to decide whether to
initialise the inline-values area:

```c
    size_t size = _PyObject_VAR_SIZE(type, nitems+1);                 // :2521
    const size_t presize = _PyType_PreHeaderSize(type);               // :2523
    if (type->tp_flags & Py_TPFLAGS_INLINE_VALUES) {                  // :2524  READ #1 -> SIZE
        assert(type->tp_itemsize == 0);
        size += _PyInlineValuesSize(type);                            // :2526
    }
    char *alloc = _PyObject_MallocWithType(type, size + presize);     // :2528
    ...
    if (type->tp_flags & Py_TPFLAGS_INLINE_VALUES) {                  // :2550  READ #2 -> INIT
        _PyObject_InitInlineValues(obj, type);                        // :2551
    }
```

If the flag is 0 at `:2524` and 1 at `:2550`, the object is allocated with **no** inline-values
tail and is then initialised **with** one. `_PyObject_InitInlineValues` (`dictobject.c:7306`)
writes, starting at the end of the allocation:

```c
struct _dictvalues { uint8_t capacity; uint8_t size; uint8_t embedded; uint8_t valid; PyObject *values[1]; };
    values->capacity = (uint8_t)size;   values->size = 0;
    values->embedded = 1;               values->valid = 1;
    for (size_t i = 0; i < size; i++) { values->values[i] = NULL; }
```

i.e. a 4-byte header plus `shared_keys_usable_size(keys)` NULL pointers — ~236 bytes past the end
for the default shared-keys size.

### This attributes pass 1's unattributed corruption

Pass 1 reproduced this crash 4/4 on `debug-ft-nojit` and recorded an honest limit:
*"I have NOT isolated which individual slot store produces the out-of-bounds write."* Its
debug-allocator dump was:

```
Debug memory block at address p=0x20004170060: API 'o'
    48 bytes originally requested
    The 8 pad bytes at tail=0x20004170090 are not all FORBIDDENBYTE (0xfd):
        at tail+0: 0x1d *** OUCH
        at tail+1: 0x00 *** OUCH
        at tail+2: 0x01 *** OUCH
        at tail+3: 0x00 *** OUCH
```

Those four bytes are **exactly a `struct _dictvalues` header**: `capacity = 0x1d = 29`,
`size = 0`, `embedded = 1`, `valid` (cleared back to 0 by `PyObject_ClearManagedDict` during the
teardown that detected the corruption). Nothing else in CPython writes that byte pattern, and it
can only land *past* the end of a 48-byte object if `:2524` declined to add
`_PyInlineValuesSize`. **It was never a slot store** — pass 1 was right to refuse the attribution
it could not prove, and this is the mechanism.

**The corruption is at the tail, and that is a third forensic match.** `presize`
(`_PyType_PreHeaderSize`, `:2523`) is driven by `Py_TPFLAGS_MANAGED_DICT` /
`Py_TPFLAGS_MANAGED_WEAKREF`, and `type_new_impl` sets both at `:4690`/`:4695` — inside
`type_new_set_attrs`, called at `:4948`, i.e. **before** `PyType_Ready` at `:4953`. So the
preheader size is stable across publication and only the *inline-values tail* is racy. Pass 1's
report is a `_PyMem_DebugRawFree: bad trailing pad byte` — corruption at `tail`, not at `p-7`,
where the dump explicitly notes *"The 7 pad bytes at p-7 are FORBIDDENBYTE, as expected."*

The `capacity` value pins it further. `_PyDict_NewKeysForClass` calls
`init_keys_object(keys, NEXT_LOG2_SHARED_KEYS_MAX_SIZE, …, SHARED_KEYS_MAX_SIZE, …)`
(`dictobject.c:7275`) with `SHARED_KEYS_MAX_SIZE == 30` (`pycore_dict.h:255`), so a fresh class has
`dk_usable = 30`, `dk_nentries = 0`, hence `shared_keys_usable_size == 30`.
`_PyObject_InitInlineValues` decrements `dk_usable` once per instance, so the value is **29 for the
second instance of a freshly-readied class** — which is `0x1d`, the observed `capacity`. The dump
is the second `Sub()` call on a class whose `Py_TPFLAGS_INLINE_VALUES` flipped mid-allocation.

### The adjacent hazard that IS defended — state it so nobody re-reports it

`_PyInlineValuesSize` (`pycore_dict.h:376`) and `_PyObject_InitInlineValues` **both** call
`shared_keys_usable_size(keys)`, so that quantity is also read twice across the allocation. That
one is defended, deliberately and with comments:

- `split_keys_entry_added` (`dictobject.c:242`, FT arm) stores `dk_nentries + 1` *before*
  `dk_usable - 1`, with the comment *"We increase before we decrease so we never get too small of
  a value when we're racing with reads"*;
- `shared_keys_usable_size` (`pycore_dict.h:361`) reads them in the mirrored order for the same
  stated reason;
- and `_PyInlineValuesSize` returns `prefix_size + (size + 1) * sizeof(PyObject *)` — the `+ 1`
  absorbs exactly the one-slot over-read the transient window can produce.

So the shared-keys double-read is safe. Only the **flag** double-read is not. **ACCEPTABLE.**

### Guarded twin / fix

The twin is the whole point of the deferred slot-update queue (`:3809–3941`) and of
`_PyType_SetFlagsRecursive:6506`, which does `BEGIN_TYPE_LOCK()` → invalidate →
`type_lock_prevent_release()` → `types_stop_world()` → mutate flags → restart. Two fixes, either
sufficient:

1. **Move `type_ready_managed_dict` above `type_ready_add_subclasses`** in `type_ready`
   (swap `:9581` and `:9584–9587`). **Verified dependency-safe in both directions:**
   `type_ready_managed_dict` reads only `tp_flags` (`Py_TPFLAGS_MANAGED_DICT` / `HEAPTYPE`, set by
   `type_new_impl` at `:4694–4695`, pre-Ready), `tp_itemsize` (`:4700`, pre-Ready) and `tp_dict`
   (set by `type_ready_set_dict`, called at `:9549` — already before `:9581`); and
   `_PyDict_NewKeysForClass` (`dictobject.c:7258`) touches only `cls->ht_type.tp_dict`'s
   `__static_attributes__`. Conversely `type_ready_add_subclasses` reads only `tp_bases`
   (set at `:9558`) and consumes nothing `type_ready_managed_dict` produces. So the swap has no
   dependency inversion. This is the smallest correct change; it does **not** on its own fix
   Finding 4's `:9595`/`:9600`, which need their own treatment.
2. **Read `tp_flags` once** in `_PyType_AllocNoTrack` — hoist `int inline_values = (type->tp_flags
   & Py_TPFLAGS_INLINE_VALUES) != 0;` above `:2524` and use it at both `:2524` and `:2550`. This
   is defence in depth and closes the whole double-read class regardless of who flips the flag.

Both are worth doing; (2) alone converts a heap overflow into a benign missing/extra inline-values
area, and (1) alone restores the file's own stated invariant.

---

## FINDING 4 — [FIX] `type_ready` performs two more plain `tp_flags` writes after publication, read unlocked by `find_name_in_mro`

**Sites:** `Objects/typeobject.c:9595` and `:9600` (writes), `:520` / `:6156` (unlocked read).
Same class as Finding 3; this is the rest of CPY-0072's `type_set_flags:458` sibling.

After `type_ready_add_subclasses` publishes the type at `:9581`, `type_ready` still does:

```c
    /* All done -- set the ready flag */
    if (initial) {
        type_add_flags(type, Py_TPFLAGS_READY);      // :9595  plain tp_flags store
    }
    stop_readying(type);                             // :9600  -> type_clear_flags(type, Py_TPFLAGS_READYING)  :510
```

**Why the architecture comment does not excuse this.** The comment at `:73–77` authorises
unlocked, non-atomic *reads* of `tp_flags` — but that authorisation is explicitly conditional on
the *writes* being done "before the type has been revealed to other threads **or** … while the
stop-the-world mechanism is active". These three writes (`:9487`, `:9595`, `:9600`) satisfy
**neither** disjunct. Quoting `:73–77` to dismiss the reads is only valid once the writes hold up
their end, and here they do not.

**Concrete unlocked reader.** `find_name_in_mro:6156` calls `is_readying(type)`, whose heap-type
arm is `return (type->tp_flags & Py_TPFLAGS_READYING) != 0;` (`:520`) — and `find_name_in_mro` is
reached from `_PyType_LookupStackRefAndVersion:6365` on the branch that takes **no** `TYPE_LOCK`.
So the `:9600` write races a genuine unlocked read on the hot attribute-lookup path. A losing
read makes `find_name_in_mro` call `PyType_Ready` on a type mid-ready; `PyType_Ready`
double-checks `Py_TPFLAGS_READY` under `BEGIN_TYPE_LOCK()` at `:9628`, so the consequence there is
benign — but the `:9487` write in the same window is not (Finding 3), and both are the same defect.

Pass 1 examined the `is_readying:518` T1 candidate and dismissed it as a false positive on the
*static-builtin* arm — correctly, and I re-confirm that. This is the **heap-type** arm at `:520`,
which pass 1 dismissed by quoting `:73–77`. That dismissal is the one I am reversing, on the
grounds above.

---

## FINDING 5 — [CONSIDER] `type_from_slots_or_spec` mutates a published type's `tp_dict` outside `type_setattro`

**Site:** `Objects/typeobject.c:5733–5782`. T4 sibling; **not covered by gh-151394**, which touches
only the cosmetic `TYPE_SET_REVEALED` line at `:5787` in this function.

`PyType_Ready(type)` at `:5725` publishes the type. The next 57 lines then mutate its `tp_dict`
directly:

```c
    PyObject *dict = lookup_tp_dict(type);                              // :5733
    ... PyDict_SetItem(dict, &_Py_ID(__doc__), __doc__);                // :5739
    ... PyDict_DelItem(dict, &_Py_ID(__weaklistoffset__));              // :5747
    ... PyDict_DelItem(dict, &_Py_ID(__dictoffset__));                  // :5752
    ... PyDict_SetItem(dict, &_Py_ID(__module__), modname);             // :5770
```

These bypass `type_setattro` entirely, and therefore bypass all three things it does: the
`BEGIN_TYPE_DICT_LOCK(dict)` two-mutex critical section (`:6829`), the
`_PyType_Modified_Unlocked` version invalidation (`:6712`), and `update_slot_after_setattr`
(`:6726`). A concurrent thread that reached the type via `Base.__subclasses__()` and looked up
`__doc__` in the window will have cached "not found" under a version tag that this thread never
invalidates.

`PyErr_WarnFormat` at `:5777` additionally runs arbitrary Python (a `warnings` filter /
`showwarning` override) on a published, still-being-finalised type.

**Why CONSIDER and not FIX.** The reachability is real but thin: `PyType_FromSpec` /
`PyType_FromMetaclass` are extension-module init paths, so the racing thread needs a reference to
a base of a type another thread is constructing during import. No memory-unsafety consequence —
this is stale cache / lost invalidation, not a bad write. Recording it because it is the same
publish-before-finish defect in the file's *other* type constructor, and because a reviewer fixing
CPY-0072 should fix both constructors, not one.

---

## FINDING 6 — [FIX] `PyType_Unwatch` writes `tp_watched` with no lock; `PyType_Watch` writes it under `TYPE_LOCK`

**Sites:** `Objects/typeobject.c:1129` (unguarded write), `:1112` (guarded write, the twin),
`:1212`/`:1214` (guarded read), `:6988`/`:6991` (unguarded read in `type_dealloc`).
`tp_watched` is `unsigned char` (`Include/cpython/object.h:236`).

The guarded twin is **17 lines above** the unguarded site, and nothing comments on the asymmetry:

```c
int PyType_Watch(int watcher_id, PyObject* obj) {
    ...
    BEGIN_TYPE_LOCK();                                 // :1110
    assign_version_tag(interp, type);
    type->tp_watched |= (1 << watcher_id);             // :1112   read-modify-write, LOCKED
    END_TYPE_LOCK();
}

int PyType_Unwatch(int watcher_id, PyObject* obj) {
    ...
    type->tp_watched &= ~(1 << watcher_id);            // :1129   read-modify-write, UNLOCKED
    return 0;
}
```

Both are non-atomic read-modify-writes on the same byte. A `|=` and a `&=` racing on the same byte
is a lost update: either the set or the clear silently disappears.

**`tp_watched` falls through both of the file's synchronisation regimes.** It is not in the
`TYPE_LOCK` list at `:65–68` (`tp_version_tag`, `_spec_cache`, `tp_mro`, `tp_bases`, `tp_base`),
and `PyType_Unwatch` does not stop the world, so it is not covered by the slot/flag regime at
`:73–77` either. It is the only mutable per-type field in this file with a *partially* applied
lock and no stated rule — the include_map's shared-state table records its guard column literally
as "inconsistent".

**Reachability.** `PyType_Watch` / `PyType_Unwatch` are public (unstable) C API, exposed to Python
as `_testcapi.watch_type` / `_testcapi.unwatch_type`. Any two threads calling them on one shared
type race. `_PyType_Modified_Unlocked:1212/1214` is a `TYPE_LOCK`-held **reader** of the same
byte, so this is also a guarded-reader / unguarded-writer pair.

**Severity ceiling, stated honestly.** The in-tree consumer that would make a lost `|=`
*dangerous* is the tier-2 optimizer: `watch_type` (`Python/optimizer_analysis.c:176`) calls
`PyType_Watch(TYPE_WATCHER_ID, type)` to register a trace dependency **in place of** an emitted
type guard — `optimizer_bytecodes.c:2209` and `:2229` emit `ADD_OP(_NOP, 0, 0)` instead of a
guard when the type is definite, and `:1469` inlines the raw `type->tp_iternext` pointer into the
trace. A lost watch bit there means `_Py_Executors_InvalidateDependency` never runs and a compiled
executor keeps calling a stale inlined slot pointer. **But that consumer is not reachable on a
free-threaded build**: `optimizer_analysis.c` is entirely `#ifdef _Py_TIER2`, `_Py_TIER2` is only
defined by `--enable-experimental-jit`, and `configure.ac:2961` warns
*"--enable-experimental-jit does not work correctly with --disable-gil"* (GH-133171: "builds the
JIT but never actually uses it"). So on FT the surviving consumers are `_testcapi` and
third-party extensions.

### Reproduced — and the lost update is observable without TSan

**TSan** (`debug-ft-nojit-tsan` and `release-ft-nojit-tsan`, `PYTHON_GIL=0`): three distinct
`size 1` access pairs on the same address, across three runs — 2 unique CPython races each, 0
noise:

```
  Write of size 1 by thread T1:
    #0 PyType_Watch              Objects/typeobject.c:1112:22   <- under BEGIN_TYPE_LOCK()
  Previous write of size 1 by thread T4:
    #0 PyType_Unwatch            Objects/typeobject.c:1129:22   <- NO lock

  Write of size 1 by thread T1:
    #0 PyType_Unwatch            Objects/typeobject.c:1129:22
  Previous read of size 1 by thread T4:
    #0 _PyType_Modified_Unlocked Objects/typeobject.c:1212:15   <- TYPE_LOCK held
```

plus a `:1129` vs `:1129` pair — two unlocked writers clearing *different bits of the same byte*,
which is the lost-update case directly.

**Semantic lost update, no sanitizer.** One canary watcher that is never unwatched, N storm
watchers whose last operation is always `unwatch`, then probe:

| build | clean rounds | rounds with a lost `&=` |
|---|---|---|
| `release-ft-nojit` | 35/40 | **5/40** |
| `debug-ft-nojit` | 37/40 | **3/40** |
| `debug-gil-nojit` (control) | **40/40** | **0/40** |

The failure mode is a watcher bit **surviving its own `PyType_Unwatch`** — the type stays watched
permanently. One round lost two bits at once. This upgrades the finding from "TSan warns" to
"the API silently does not do what it was asked", which is the part a maintainer can act on
without caring about the JIT.

Classified **FIX** on the data race, the guarded twin, and the demonstrated lost update — not on
the JIT consequence, which the FT build cannot reach. The fix is
one line: wrap `:1129` in `BEGIN_TYPE_LOCK()` / `END_TYPE_LOCK()` exactly as `:1110–1113` does.
Note that `type_watcher_callback` (`optimizer_analysis.c:153`) calls `PyType_Unwatch` from *inside*
`_PyType_Modified_Unlocked`'s watcher loop, which already holds `TYPE_LOCK` — so the fix must use
the re-entrant-safe form, or the clear must be hoisted out of the callback.

---

## FINDING 7 — [FIX — **REPRODUCED**: deadlock on FT, *and heap-buffer-overflow on the DEFAULT GIL build*] `_PyType_Modified_Unlocked` runs arbitrary Python while `insert_split_key` holds the raw, non-detaching keys mutex

> **This is the most severe finding in the pass, and it is not free-threading-specific.**
> Three of its four consequences reproduce on `release-gil-nojit` / `debug-gil-nojit`, from pure
> Python, single-threaded. It is 19 days old at the target ref.
>
> **`Objects/dictobject.c:218–227` forbids this in so many words, naming the function:**
>
> ```c
> // gh-151593: The _Py_LOCK_DONT_DETACH flag ensures that the outer critical
> // section is not dropped if there is some contention on the keys lock.
> // It also means that it will be important that LOCK_KEYS() is essentially the
> // "inner-most" code and that we don't call Py_DECREF() or similar while
> // holding the keys lock.
> //
> // We are not allowed to acquire other locks within LOCK_KEYS(). For example,
> // PyType_Modified() must not be called within LOCK_KEYS() since it acquires
> // the type lock.
> #define LOCK_KEYS(keys) PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)
> ```
>
> `insert_split_key:1971` calls `_PyType_Modified_Unlocked` — the same function, minus the lock
> acquisition — 1,744 lines below that comment. Taking `TYPE_LOCK` first and then calling the
> `_Unlocked` variant satisfies the rule's *letter* (no lock is acquired under `LOCK_KEYS`) and
> discards its *substance*: the rule also requires `LOCK_KEYS()` to be the **inner-most** code and
> forbids even `Py_DECREF()` there, while `_PyType_Modified_Unlocked` runs **arbitrary Python**.


**Sites:** `Objects/dictobject.c:1962` (`LOCK_KEYS`), `:1971` (the call), `:1981` (`UNLOCK_KEYS`);
`Objects/typeobject.c:1166` (the callee), `:1201–1208` (recursive descent), `:1222` (callback),
`:1223` (`PyErr_FormatUnraisable` with `%R`).
**Age: 19 days at the target ref.** This is the brief's item 4 and the history document's
single highest-priority hand-off.

### What take-2 actually changed, and what it did not

`20921922fa4` (2026-07-06, gh-150490 take 2) renamed `static void type_modified_unlocked` to the
exported `void _PyType_Modified_Unlocked` and gave it its first external caller. The two prior
attempts were reverted (`gh-152238`) with the note *"it violates locking discipline and results in
deadlocks … The attempted fix gh-152200 avoids the deadlock but introduces a data-race. The race
window is small but can be triggered with pure Python code."* Take 2 fixed the **lock ordering** —
and says so:

```c
    // We need to acquire the type lock before the keys mutex. Another lock
    // is never acquired below the keys mutex but a keys mutex can be acquired
    // elsewhere while we hold the types lock. To avoid deadlocks we must always
    // acquire the type lock first.
    Py_BEGIN_CRITICAL_SECTION_MUTEX(&_PyInterpreterState_GET()->types.mutex);
#endif
    LOCK_KEYS(keys);                                          // :1962
    ...
        PyTypeObject *type = FT_ATOMIC_LOAD_PTR_ACQUIRE(shared_keys->dsk_owning_type);
        if (type) {
            _PyType_Modified_Unlocked(type);                  // :1971
        }
    ...
    UNLOCK_KEYS(keys);                                        // :1981
    Py_END_CRITICAL_SECTION();
```

It did **not** remove the ability to run Python inside the keys mutex.

### The mutex is raw and non-reentrant

```c
#define LOCK_KEYS(keys) PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)   // dictobject.c:227
```

This is a bare `PyMutex`, **not** a critical section: it does not participate in
`_PyCriticalSection_SuspendAll`, so it is not released when the thread blocks, and
`_Py_LOCK_DONT_DETACH` additionally keeps the thread attached, so it also blocks stop-the-world
and the GC. Re-acquiring it on the same thread is a hard self-deadlock.

### What `_PyType_Modified_Unlocked` does inside that window

```c
    PyObject *subclasses = lookup_tp_subclasses(type);             // :1195  borrowed (CPY-0084)
    while (PyDict_Next(subclasses, &i, NULL, &ref)) {
        _PyType_Modified_Unlocked(subclass);                       // :1206  unbounded recursion (CPY-0087)
    }
    if (type->tp_watched) {
        ...
            PyType_WatchCallback cb = interp->type_watchers[i];
            if (cb && (cb(type) < 0)) {                            // :1222  arbitrary C
                PyErr_FormatUnraisable(
                    "Exception ignored in type watcher callback #%d for %R",
                    i, type);                                      // :1223  %R -> metaclass __repr__
            }
```

`PyErr_FormatUnraisable` is not one Python-entry point but **four**, all inside the mutex.
`format_unraisable_v` (`Python/errors.c`) does, in order:

1. `_PyErr_NormalizeException(...)` — instantiates the exception, running a user exception class's
   `__init__`;
2. `PyUnicode_FromFormatV(format, va)` — the `%R` is `PyObject_Repr((PyObject *)type)`, dispatching
   `Py_TYPE(type)->tp_repr`, i.e. **a Python-level `__repr__` on the metaclass**;
3. `_PySys_Audit(tstate, "sys.unraisablehook", "OO", hook, hook_args)` — runs every handler
   installed by **`sys.addaudithook()`**;
4. the hook call itself — **`sys.unraisablehook` is a documented, user-assignable pure-Python
   attribute**.

So no metaclass is even required: `sys.unraisablehook = f` is sufficient to get arbitrary Python
executing while the raw `dk_mutex` is held. The function's own header comment flags the re-entrancy
(`:1219–1220`: *"PyErr_FormatUnraisable is potentially re-entrant and the watcher callback might
be too"*) and asserts `!types_world_is_stopped()` at `:1187` — it is written in full awareness that
it re-enters, and that awareness was calibrated against callers that hold **only** `TYPE_LOCK`.

`dsk_owning_type` is the class itself (`_PyDict_NewKeysForClass` sets it at `dictobject.c:7273`),
so a watcher installed on class `C` fires from this path whenever a new attribute *name* is first
added to any instance of `C`.

### The guarded twin

`_PyType_Modified_Unlocked` has **seven** in-tree call sites. Six are in `typeobject.c`
(`:1206`, `:1251`, `:1744`, `:1971`, `:3687`, `:6517`, `:6712`) and every one of them holds either
`TYPE_LOCK` alone or `BEGIN_TYPE_DICT_LOCK` — a **critical section**, therefore suspendable, and
the one that is closest to this shape (`type_update_dict:6712`) carries a four-line comment
(`:6697–6702`) proving no re-entrancy can occur there because the keys are exact unicode.
`dictobject.c:1971` is **the only caller that holds a second, raw, non-suspendable mutex across
it**, and it is the only one with no such argument written down.

### The second defect in the same window: the guard is read before the Python runs, and not re-checked

```c
    LOCK_KEYS(keys);
    ix = unicodekeys_lookup_unicode(keys, key, hash);
    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {          // :1964  GUARD read here
        FT_ATOMIC_STORE_UINT32_RELAXED(keys->dk_version, 0);
        ...
        if (type) {
            _PyType_Modified_Unlocked(type);                // :1971  ARBITRARY PYTHON runs here,
        }                                                   //        and can consume every slot
        Py_ssize_t hashpos = find_empty_slot(keys, hash);
        ix = keys->dk_nentries;                             // :1974  re-read; guard NOT re-checked
        dictkeys_set_index(keys, hashpos, ix);
        PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(keys)[ix];   // :1976  OOB when slots exhausted
        STORE_SHARED_KEY(ep->me_key, Py_NewRef(key));
        split_keys_entry_added(keys);                       // dk_usable-- ⇒ can go negative
    }
    assert (ix < SHARED_KEYS_MAX_SIZE);                     // :1980  debug-only
```

**This is a `size_then_use_reread` instance** — the exact shape proposed as recall gap R4 below,
and the same shape as Finding 3's flag double-read and as catalogued CPY-0049 / CPY-0052. The
`dk_usable > 0` guard at `:1964` is stale by the time the write at `:1976` uses it, because the
re-entry at `:1971` can consume every remaining slot.

### Consequences — four, three of them on the DEFAULT GIL build

All reproduced (scripts and evidence under `scratchpad/`, see Reproduction):

1. **Self-deadlock (free-threaded builds).** Python executed at `:1223` that adds a
   previously-unseen attribute name to an instance sharing the same `keys` re-enters
   `insert_split_key` → `LOCK_KEYS(keys)` on a mutex this thread already holds. **24/24 hangs**
   across all four FT builds × both re-entry vectors, **0/12 on the GIL builds** (all exit 0).
   Because of
   `_Py_LOCK_DONT_DETACH` the hung thread also wedges stop-the-world for every other thread.
2. **Heap-buffer-overflow WRITE (default GIL build).** The stale guard above.
   `release-gil-nojit-asan`: `AddressSanitizer: heap-buffer-overflow` at
   `Objects/dictobject.c:7497` in `store_instance_attr_lock_held` — `values->values[ix]`,
   **16 bytes past a 304-byte region**.
3. **Broken `dk_usable` invariant from a SINGLE re-entry (default GIL build).** 5/5 runs on
   `debug-gil-nojit`:
   `Objects/dictobject.c:719: _PyDict_CheckConsistency: Assertion failed: 0 <= dk_usable && dk_usable <= usable`.
4. **Unbounded watcher recursion (default GIL build).** `set_version_unlocked(type, 0)` is at
   `typeobject.c:1233`, **after** the watcher loop at `:1212–1231`. So when a watcher callback
   re-enters `_PyType_Modified_Unlocked` on the same type, the early-out at `:1189`
   (`if (type->tp_version_tag == 0) return;`) does **not** fire — the version has not been cleared
   yet — and the same watcher fires again, unboundedly. 498 frames then SIGSEGV on
   `release-gil-nojit`; `assert(ix < SHARED_KEYS_MAX_SIZE)` at `dictobject.c:1980` on
   `debug-gil-nojit`. **This one is a design bug independent of the mutex**: the invalidation that
   would break the cycle is sequenced after the callback that needs it.

Consequences 2–4 are *not* free-threading bugs. They are ordinary re-entrancy bugs on the default
build, introduced 19 days before the target ref by a change whose two previous attempts were
reverted for a deadlock and then a data race.

**Fix shape.** Three independent changes, all cheap:
- **Get the Python out of the mutex.** Capture `type` under `LOCK_KEYS`, release it, then call
  `_PyType_Modified_Unlocked` while still holding `TYPE_LOCK` — which is what all six
  `typeobject.c` callers do, and what `dictobject.c:218–227` demands.
- **Re-check the guard.** After `:1971`, re-test `keys->dk_usable > 0` (and `ix == DKIX_EMPTY`)
  before the write at `:1976`, or hoist the write above the invalidation.
- **Move the invalidation before the watcher loop.** Sequence `set_version_unlocked(type, 0)`
  ahead of `:1212` so the `:1189` early-out terminates re-entrant callbacks. Note this interacts
  with the deliberate design that watchers observe the type with its *old* version still live
  (include_map §1/R6) — so this one needs a maintainer decision, not a mechanical patch.

---

## FINDING 8 — [CONSIDER] `_PyType_SetFlags` has none of the three protections its twin 53 lines below documents

**Sites:** `Objects/typeobject.c:6453` (unguarded) vs `:6506` (`_PyType_SetFlagsRecursive`, the
twin). Both are called from `Modules/_abc.c` — `:541` and `:601` respectively.

```c
void
_PyType_SetFlagsRecursive(PyTypeObject *self, unsigned long mask, unsigned long flags)
{
    BEGIN_TYPE_LOCK();                                              // :6508
    /* Ideally, changing flags and invalidating the old version tag would
       happen in one step. But _PyType_Modified_Unlocked() is re-entrant and
       cannot run with the world stopped, so we must invalidate first. ... */
    if (!PyType_HasFeature(self, Py_TPFLAGS_IMMUTABLETYPE) &&
        (self->tp_flags & mask) != flags) {
        _PyType_Modified_Unlocked(self);                            // :6517
    }
    /* Keep TYPE_LOCK held while waiting for stop-the-world so no thread
       can reassign a version tag before the flag update. */         // :6519-6520
    type_lock_prevent_release();                                    // :6521
    types_stop_world();
    set_flags_recursive(self, mask, flags);
    types_start_world();
    type_lock_allow_release();
    END_TYPE_LOCK();
}
```

```c
void
_PyType_SetFlags(PyTypeObject *self, unsigned long mask, unsigned long flags)   // :6453
{
    unsigned long new_flags = (self->tp_flags & ~mask) | flags;
    if (new_flags != self->tp_flags) {
        types_stop_world();                                         // :6457
        // can't use new_flags here since they could be out-of-date  // :6458
        self->tp_flags = (self->tp_flags & ~mask) | flags;
        types_start_world();
    }
}
```

No `TYPE_LOCK`, no version invalidation, no `type_lock_prevent_release()` — and its own comment at
`:6458` admits the read it just did was racy. The twin's comment at `:6519–6520` states exactly
the hazard this leaves open: *"so no thread can reassign a version tag before the flag update"*.
Because `_PyType_SetFlags` holds no lock, a concurrent
`_PyType_LookupStackRefAndVersion:6358` → `assign_version_tag` can hand out a fresh version tag
that outlives the flag change, and anything caching under that version observes the pre-change
`Py_TPFLAGS_SEQUENCE`/`Py_TPFLAGS_MAPPING`.

**History corroboration (from `preflight/history_context.md` §4.3):** `e7eaed56149a` (2026-05-23,
gh-148450, *"abc.register needs to update type_version when tp_flags is changed"*) added the
`_PyType_Modified_Unlocked` call **to the recursive function only**. The reported bug is a property
of mutating `tp_flags`, not of recursing; the fix was applied to the reported call site alone. It
is two months old and the twin is 53 lines away in the same file.

**Reachability check the history doc asked me to make** — *can the type already hold a version tag
when `_abc.c:541` runs?* `_abc_init` reaches `:541` after `PyObject_SetAttr(self, "_abc_impl", …)`
(`_abc.c:510`), and `Lib/abc.py`'s `ABCMeta.__new__` does `getattr(cls, name, …)` in its
abstract-method loop before calling `_abc_init` — every such lookup runs `assign_version_tag`. So
the class routinely acquires and loses version tags across this window on its *own* thread; under
free-threading a *second* thread can assign one at any point, and `_PyType_SetFlags` holds nothing
that prevents it. **CONSIDER**, not FIX: I could not construct a Python-visible wrong answer,
because `MATCH_SEQUENCE`/`MATCH_MAPPING` read `tp_flags` directly at runtime rather than through a
version-guarded cache. The exposure is to `_spec_cache` / specialization consumers.

**Relationship to CPY-0082 — adjacent family, distinct member.** CPY-0082 records
`type_set_abstractmethods:1745` and `PyType_Freeze:12523` as sites that *hold* `TYPE_LOCK` across
`types_stop_world()` but omit `type_lock_prevent_release()`, and its note asserts *"No fourth site
exists"* — correctly, within its frame, which enumerates STW-**under**-lock sites.
`_PyType_SetFlags:6453` is an STW-**without**-lock site: it never takes `TYPE_LOCK` at all, so it
falls outside CPY-0082's enumeration by construction, and it additionally omits the version
invalidation that CPY-0082's two sites do not need. Same family (the `fbbbc10055e0` /
`b83f379a972` / `e7eaed56149a` three-generation propagation gap CPY-0082 documents), different
member — and the same three commits are implicated: `e7eaed56149a` is exactly the commit that added
the invalidation to the recursive twin and not to this one.

**Sibling, one line, outside this slice:** `_abc_init` also does
`PyDict_Pop(dict, &_Py_ID(__abc_tpflags__))` at `_abc.c:523` on a live type's `tp_dict`, bypassing
`type_setattro` and therefore skipping version invalidation — the same shape as Finding 5, in
`Modules/_abc.c`.

---

## FINDING 9 — [CONSIDER — **REPRODUCED under TSan**] `type_cache_clear` writes the cache entry with plain stores in the inverse of the documented order

**Sites:** `Objects/typeobject.c:988–990` (the writer) vs `:6218–6230` (`update_cache`, the twin);
contract at `:6225–6229`. Reachable from pure Python via the deprecated
`sys._clear_type_cache()` (`Python/sysmodule.c:2204`) **and** via its supported, non-deprecated
replacement `sys._clear_internal_caches()` (`Python/sysmodule.c:2227`) — both call
`PyType_ClearCache()` → `_PyType_ClearCache:1016` → `type_cache_clear:981`.

This is the brief's item 3. **The contract holds at the site it was written for, and is violated
at the other one.**

`update_cache` obeys it exactly:

```c
    _Py_atomic_store_ptr_relaxed(&entry->value, value);          // :6218  value
    ...
    _Py_atomic_store_ptr_relaxed(&entry->name, Py_NewRef(name)); // :6224  name
    // We must write the version last to avoid _Py_TryXGetStackRef()
    // operating on an invalid (already deallocated) value inside
    // _PyType_LookupRefAndVersion(). ...                        // :6225-6229
    _Py_atomic_store_uint32_release(&entry->version, version_tag); // :6230  version LAST, release
```

`type_cache_clear` does the opposite, with plain non-atomic stores:

```c
        _PySeqLock_LockWrite(&entry->sequence);
        entry->version = 0;                                       // :988   version FIRST, plain
        Py_XSETREF(entry->name, _Py_XNewRef(value));              // :989   plain
        entry->value = NULL;                                      // :990   plain
        _PySeqLock_UnlockWrite(&entry->sequence);
```

### Every store site into a `type_cache_entry`, checked

The brief asks for the contract to be verified at *every* store site. There are five, and this is
all of them (`grep -n "entry->version\|entry->name\|entry->value" Objects/typeobject.c`):

| site | function | order | atomicity | verdict |
|---|---|---|---|---|
| `:6218`,`:6224`,`:6230` | `update_cache` | value → name → **version last** | 2× relaxed + 1× **release** | **obeys the contract** |
| `:6240`–`:6254` | `update_cache_gil_disabled` | delegates to `update_cache` | wrapped in `_PySeqLock_LockWrite`/`UnlockWrite` | obeys |
| `:988`–`:990` | `type_cache_clear` | **version first** → name → value | **all three plain** | **violates both** |
| `:1006`–`:1010` | `_PyType_InitCache` | version → name → value | plain | ACCEPTABLE — runs once per interpreter during `_PyType_InitCache`, before any thread can reach the cache; `assert(entry->name == NULL)` at `:1004` encodes that |
| `:6270`–`:6272` | `_PyTypes_AfterFork` | value → name → version | plain | ACCEPTABLE — only one thread exists after `fork()`, and `_PySeqLock_AfterFork` explicitly repairs a write section interrupted by the fork |

So exactly one of the five violates the contract, and it is the one reachable from Python at
arbitrary times.

### The read side is correct

`_PyType_LookupStackRefAndVersion:6313–6337` mirrors `update_cache` properly: `_PySeqLock_BeginRead`
→ `_Py_atomic_load_uint32_acquire(&entry->version)` (`:6315`, pairs with the release at `:6230`) →
acquire load of `type->tp_version_tag` (`:6316`) → name compare (`:6318`) →
`_Py_TryXGetStackRef(&entry->value, out)` (`:6321`) → `_PySeqLock_EndRead` (`:6323`). No reordering
hole: `_PySeqLock_EndRead` (`Python/lock.c:625`) carries an explicit `_Py_atomic_fence_acquire()`
added by gh-121368 precisely so the sequence re-read cannot float above the body loads.

### Why the violation is benign — and why it is still worth recording

I traced the exact torn window. Because `:988` lands first, a reader can observe
`entry->version == 0` while `entry->name` is still the *old* name and `entry->value` still points
at the *old* type's attribute. A lookup on a type whose own `tp_version_tag` is 0 (just
invalidated) then passes `entry_version == type_version` at `:6317`, and passes the name compare
at `:6318` whenever the old and new lookups share a name — which requires only that the old
entry's version had its low 12 bits zero, since the bucket is
`MCACHE_HASH(version, name>>3) = (version ^ (name>>3)) & 0xFFF`, i.e. 1 in 4096. The reader would
then return an attribute belonging to a **different type**.

It does not, because `_PySeqLock_LockWrite` (`Python/lock.c:579`) has already moved the sequence to
odd, so `_PySeqLock_EndRead` at `:6323` fails and the reader discards and retries.
**The seqlock is load-bearing here, and nothing at `:986–992` says so.** The last 18 months of
change in this region has been exclusively perf work that narrowed synchronization
(gh-145685 ×3, gh-139103 ×2, gh-148906 — history §3.2); a future fast path that skips the name
compare, or that trusts `entry_version == type_version` before `EndRead`, converts this into a
live wrong-value return.

Separately, the plain stores at `:988–990` race the atomic loads at `:6315`/`:6318`/`:6321` in the
formal C11 sense and are TSan-reportable, while the sibling writer 5,200 lines away uses three
atomics on the same three fields. That is the `atomic_plain_asymmetry` (T1) shape and the scanner
reported **zero** T1 findings on this file — see recall gap R2, which pass 1 also raised and which
is still open.

### Reproduced

`release-ft-nojit-tsan`, `PYTHON_GIL=0`, `scratchpad/repro_cache_clear.py` — one thread calling
`sys._clear_internal_caches()`, six threads doing `getattr()` on 64 classes. **24 data races**, and
they partition into exactly the three predicted pairs, one per field, with no others:

| writer (plain, `type_cache_clear`) | reader (atomic, `_PyType_LookupStackRefAndVersion`) | field | races |
|---|---|---|---|
| `:988` `entry->version = 0` | `:6315` `_Py_atomic_load_uint32_acquire(&entry->version)` | `version` | 9 |
| `:989` `Py_XSETREF(entry->name, …)` | `:6318` `_Py_atomic_load_ptr_relaxed(&entry->name)` | `name` | 6 |
| `:990` `entry->value = NULL` | `:6321` `_Py_TryXGetStackRef(&entry->value, out)` | `value` | 9 |

TSan names the asymmetry itself — the two accesses are reported as a plain *"Write of size 4"*
against a *"Previous **atomic** read of size 4"*:

```
WARNING: ThreadSanitizer: data race
  Write of size 4 at 0x555555e60470 by thread T1:
    #0 type_cache_clear                 Objects/typeobject.c:988:24
    #1 _PyType_ClearCache               Objects/typeobject.c:1021:5
    #2 PyType_ClearCache                Objects/typeobject.c:1031:12
    #3 sys__clear_internal_caches_impl  Python/sysmodule.c:2227:5
  Previous atomic read of size 4 at 0x555555e60470 by main thread:
    #0 _Py_atomic_load_uint32_acquire   Include/cpython/pyatomic_gcc.h:605:10
    #1 _PyType_LookupStackRefAndVersion Objects/typeobject.c:6315:34
    #2 _PyObject_LookupSpecialMethod    Objects/typeobject.c:2973:5
```

**Control:** `release-gil-nojit`, same script, 3/3 runs clean (exit 0). Free-threading-only.

Note the entry point in frame #3: `sys._clear_internal_caches()` — the **supported,
non-deprecated** public API, from ordinary Python, with no `_testcapi` involved.

**Fix:** make `:988–990` use `FT_ATOMIC_STORE_*` and write `version` last, matching `update_cache`.
Two-line change, removes the reliance on an unstated invariant.

---

## FINDING 10 — [ACCEPTABLE, clean negative] The lookup-cache entry lifecycle is sound

The brief asks: *can a reader see an entry whose type has since been freed?* **No**, and the reason
is worth recording so it is not re-derived.

A `struct type_cache_entry` holds a strong ref to `name`, a **borrowed** `value`, and a `version`.
It holds **no** reference to the type, and `type_dealloc` does not purge the type's entries — so
after a type dies, entries carrying its version and dangling `value` pointers persist in the table.
They are nevertheless unreachable:

1. **Version tags are never reused.** The two allocators both *saturate* rather than wrap.
   `next_global_version_tag` (`:1394`) returns 0 once
   `old >= _Py_MAX_GLOBAL_TYPE_VERSION_TAG`. The heap-type counter is
   `set_version_unlocked(type, NEXT_VERSION_TAG(interp)++)` (`:1451`) guarded by
   `if (NEXT_VERSION_TAG(interp) == 0) return 0;` (`:1447`) — after the post-increment rolls the
   counter to 0 it refuses permanently. So a dead type's version can never equal a live type's,
   and `entry_version == type_version` (`:6317`) can never match a stale entry.
2. **`update_cache` is never called with version 0** — `:6374` returns early on
   `version_tag == 0`, so no entry can be populated with a real name under the one version value
   that *is* shared between types.
3. **A stale `entry->value` is never dereferenced outside a validated read.** The only other
   accesses are pointer *comparisons* — `update_cache_gil_disabled:6243–6245` and
   `_PyTypes_AfterFork:6270` — never a load through the pointer.
4. The `_Py_TryXGetStackRef` at `:6321` does dereference optimistically before `EndRead` validates,
   but that is the sanctioned try-incref-under-QSBR pattern shared with `dict`, and by (1)–(2) it
   is only ever applied to an entry whose version genuinely matches a live type.

The one real cost is documented by the authors themselves at `:44–47`: the cache pins `name`
strings longer than they would otherwise live, which is why `MCACHE_MAX_ATTR_SIZE` exists.

---

## Confirmations (informed mode — one line each, not re-litigated)

| id | site | status at target ref |
|---|---|---|
| **CPY-0072** | `fixup_slot_dispatchers:12136` ⇐ `type_new_impl:4958` | **Unchanged.** `:12132–12138` still passes `NULL` for `queued_updates`, `:12056` still does the immediate `*ptr = slot_value`. PR gh-151394 still **open and unmerged** (last touched 2026-07-03, 22 days before the target ref). Findings 3–5 are three siblings it does not cover. |
| **CPY-0070** | `type_set_bases_unlocked:1965/1966` | Unchanged; `res = add_all_subclasses(...)` still never branched on. Encountered while triaging Finding 1. |
| **CPY-0073** | `set_flags_recursive:6493` | Unchanged. `_PyType_GetSubclasses` (`PyList_New` + `PyList_Append`) is still called from inside `_PyType_SetFlagsRecursive`'s `types_stop_world()` window (`:6522`→`:6523`→`:6493`). |
| **CPY-0084** | `_PyType_Modified_Unlocked:1195/1223` | Unchanged, and **widened**: Finding 7 adds `Objects/dictobject.c:1971` as a new external caller 19 days old. |
| **CPY-0087** | `_PyType_Modified_Unlocked:1206`, `assign_version_tag:1431`, `set_flags_recursive:6500` | Unchanged; all three descents still unguarded. `:1206` is now reachable from dict split-key insertion (Finding 7). |
| **CPY-0082** | `type_set_abstractmethods:1745`, `PyType_Freeze:12523` | Unchanged; both still enter `types_stop_world()` under `TYPE_LOCK` without `type_lock_prevent_release()`, while the other five STW-under-lock sites use it. |
| **CPY-0083** | `_PyType_GetSubclasses:783` | Unchanged; still the first of CPY-0072's three named uncovered siblings. |

---

## Also checked — clean, with the reason

- **`update_cache_gil_disabled:6237–6257`.** The early-out at `:6243–6245` does three plain reads,
  but writers are mutually exclusive: `_PySeqLock_LockWrite` (`Python/lock.c:579`) CASes the
  sequence to odd and spins otherwise, and `type_cache_clear:986` takes the same write lock. The
  local named `old_value` at `:6251` actually receives `update_cache`'s `old_name` — a misleading
  name, not a bug; the `Py_DECREF` at `:6256` correctly balances the `Py_NewRef(name)` at `:6224`.
- **`_PyTypes_AfterFork:6262–6276`.** Plain stores, but only one thread exists post-fork by
  definition, and `_PySeqLock_AfterFork` explicitly repairs an interrupted write section.
- **`should_assign_version_tag:6298`** (born 2026-03-12, zero fix history — the history doc's
  "do not read silence as safety" list). Reads `tp_versions_used` with
  `FT_ATOMIC_LOAD_UINT16_RELAXED` and is called before the lock is taken at `:6358`; the value is
  re-validated inside `assign_version_tag` under `ASSERT_TYPE_LOCK_HELD`, so a stale read costs at
  most one wasted lock acquisition. Clean.
- **`MCACHE_HASH_METHOD:53–55`** embeds `FT_ATOMIC_LOAD_UINT_RELAXED(type->tp_version_tag)` in the
  macro body, so the bucket index is computed from a relaxed load while `:6316` re-loads with
  acquire. A skewed bucket is a cache miss, never a wrong hit, because the version is re-compared
  at `:6317`. Clean — but this is exactly the macro-hidden atomic the include_map §4.2 #5 warns
  scanners cannot see.
- **`_PyType_CacheInitForSpecialization:6408` / `_PyType_CacheGetItemForSpecialization:6428`.**
  Both re-check `tp_version_tag == tp_version` under `BEGIN_TYPE_LOCK()` before the release store
  into `_spec_cache`. Correct, and the twin `_PyType_Modified_Unlocked:1237` /
  `type_mro_modified:1321` both null `_spec_cache.getitem` with `FT_ATOMIC_STORE_PTR_RELAXED`
  under the lock. Clean.
- **`type_from_slots_or_spec:5701` (`tp_dealloc` lazy init, the T2 candidate).** Re-confirmed as
  pass 1 found it: a pre-publication write into memory this thread allocated at `:5623` and reached
  through the interior alias `type = &res->ht_type` (`:5628`). Still a false positive, still for
  the reason pass 1 gave (scanner defect D1, unfixed).
- **`is_readying:518` (the T1 candidate).** Re-confirmed as a false positive on the
  `_Py_TPFLAGS_STATIC_BUILTIN` arm, for pass 1's reasons (per-interpreter state; writers under
  `BEGIN_TYPE_LOCK` via `type_ready:9530`). The **heap-type** arm at `:520` is a different matter —
  see Finding 4.

---

## Regions read and found clean of this agent's class

- **R11 MRO C3 (3217–3702).** `mro_implementation_unlocked`'s `bases` borrow across `class_name`
  is refcount-auditor territory (NET NEW `:3443`), not a race: `TYPE_LOCK` is held across the whole
  function and `set_tp_bases:599` asserts `ASSERT_NEW_TYPE_OR_LOCKED`. `set_tp_mro:651` wraps its
  `initial == 0` store in the full `prevent_release` + STW bracket. `mro_internal:3665–3667`'s
  `Py_XNewRef(old_mro)` / pointer-identity re-entrancy check is a deliberate ABA defence, not a
  redundant refcount pair.
- **R25 `__class__` assignment (7482–7846).** The `_PyObject_IsUniquelyReferenced` STW-skip fast
  path (`:7821`, 141 days old) and `object_set_class_world_stopped`'s per-object critical section
  taken *inside* the stopped world (`:7778–7780`) are the include_map's highest-value hunt target,
  but they are **stw-safety-checker's** class, not a data race on a type field. Flagged here so it
  is not assumed covered: I did not audit it.
- **R26 pickle (7848–8406).** No locking, no atomics, no shared type state mutated. Confirmed cold
  per the history doc (2 crash fixes in 3 years). Nothing in this agent's class.
- **R3 managed static types (228–522).** `interp_count` is the only atomically accessed field
  (`:268/347/395/402`); `state->type`, `state->tp_dict`, `num_initialized`, `next_index` are plain
  under (or without) `interp->types.mutex`. The builtins path takes no lock at all (`:319`,
  `:411–413`) but is single-threaded during `Py_Initialize`; `_PyStaticType_InitForExtension`'s only
  tree-wide caller is `Modules/_datetimemodule.c:7531`, under the import lock. Clean.

---

## Reproduction

All builds at `a1d580430c8`; `Objects/typeobject.c` and `Objects/dictobject.c` are byte-identical
to the target ref `4f3be1b5777`, so every line number below transfers unchanged.
Artifacts under `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`.

### Finding 7 — `insert_split_key` re-entrancy — **REPRODUCED, 36 runs, perfect separation**

Script: `repro_keysmutex_reentry.py` (modes `probe|deadlock|hook|threads|overflow`).

**Reachability first.** `--mode probe` confirms a plain `obj.new_attr = 1` on a watched class runs
the metaclass `__repr__` **inside** `LOCK_KEYS` → `PROBE-RESULT: REACHED`.

**Self-deadlock.** `timeout 30 … --mode deadlock --max-reentry 1` → **exit 124**. The gdb native
backtrace (`d2_gdb_bt.txt`) shows the entire cycle on **one thread** with the **same** `keys`
pointer:

```
#10 _PyMutex_LockTimed (m=0x20002ec1523, timeout=-1, flags=_Py_LOCK_DONT_DETACH) Python/lock.c:138
#11 PyMutex_LockFlags                     ./Include/internal/pycore_lock.h:65
#12 insert_split_key (keys=0x20002ec1518) Objects/dictobject.c:1962   <- INNER, parks forever
...
#30 slot_tp_repr                          Objects/typeobject.c:10890
#31 PyObject_Repr                         Objects/object.c:784
#35 format_unraisable_v                   Python/errors.c:1700
#36 PyErr_FormatUnraisable                Python/errors.c:1777
#37 _PyType_Modified_Unlocked             Objects/typeobject.c:1223
#38 insert_split_key (keys=0x20002ec1518) Objects/dictobject.c:1971   <- OUTER, holds dk_mutex
```

Reliability matrix (`matrix_results.txt`, `timeout 20`, exit 124 = hang), both re-entry vectors —
the metaclass `__repr__` route (`deadlock`) and the **metaclass-free `sys.unraisablehook`** route
(`hook`):

| build | `--mode deadlock` | `--mode hook` |
|---|---|---|
| `release-ft-nojit` | HANG 3/3 | HANG 3/3 |
| `debug-ft-nojit` | HANG 3/3 | HANG 3/3 |
| `debug-ft-nojit-tsan` | HANG 3/3 | HANG 3/3 |
| `release-ft-nojit-tsan` | HANG 3/3 | HANG 3/3 |
| `debug-gil-nojit` (control) | 0/3 (exit 0) | 0/3 (exit 0) |
| `release-gil-nojit` (control) | 0/3 (exit 0) | 0/3 (exit 0) |

**24/24 hangs on free-threaded builds, 0/12 on GIL builds.** All 36 runs executed under the same
machine load, so load cannot explain a result that tracks the build's threading model rather than
the machine's state — which retires the usual "was the timeout just slowness?" objection. The
load-bearing evidence remains the gdb stack, not the timeouts.

**The three GIL-build consequences** (single-threaded, default build, pure Python):

| consequence | build | evidence |
|---|---|---|
| heap-buffer-overflow WRITE | `release-gil-nojit-asan` | `AddressSanitizer: heap-buffer-overflow` at `Objects/dictobject.c:7497` in `store_instance_attr_lock_held` — `values->values[ix]`, **16 bytes after a 304-byte region** (`--max-reentry 40`) |
| broken `dk_usable` invariant | `debug-gil-nojit` | `Objects/dictobject.c:719: _PyDict_CheckConsistency: Assertion failed: 0 <= dk_usable && dk_usable <= usable`, **5/5 runs from a SINGLE re-entry** (`--max-reentry 1`) |
| unbounded watcher recursion | `release-gil-nojit` / `debug-gil-nojit` | 498 frames then SIGSEGV / `assert(ix < SHARED_KEYS_MAX_SIZE)` at `dictobject.c:1980` |

**Honest caveat carried forward:** the naive GIL control for the deadlock mode is *not* clean when
uncapped — uncapped it is a *different* crash (consequences 2–4). `--max-reentry 1` is what makes
it a fair control for the deadlock claim specifically.

### Finding 6 — `tp_watched` — **REPRODUCED**

Script: `tsan_stress_tp_watched.py` (`--scenario 1|2|3`). Evidence: `d1_s1_tsan.err`,
`d1_s2_tsan.err`, `d1_s1_reltsan.err`, `gil_control_s3.txt`. See the finding body for the three
TSan access pairs and the lost-update table (5/40 and 3/40 on FT, 0/40 on GIL).
`parse_tsan_report.py` scored these runs 2 unique CPython races each with **0 noise**.

### Finding 9 — `type_cache_clear` — **REPRODUCED**

Script: `repro_cache_clear.py`. Evidence: `cc_tsan.err`, `gil_cc_{1,2,3}.err`. 24 races
partitioning into exactly the three predicted field pairs; GIL control 3/3 clean. See the finding
body.

### Finding 3 — `Py_TPFLAGS_INLINE_VALUES` — **REPRODUCED, ASan stack proves the mechanism exactly**

Scripts: `repro_iv2.py` (primary), `repro_tsan.py`, `repro_tsan3.py`, `repro_tsan4.py`;
harnesses `runner.sh` / `runner_gil.sh`. Reports under `scratchpad/ARTIFACTS/`.

**Trivially reproducible.** The vanilla shape — plain `class Sub(Base)`, no tuning at all — is
**10/10 crash** on `debug-ft-nojit` in 12 seconds total. Tuning (fresh single-child base per
thread, 29 `__static_attributes__`, fat `__slots__`) only shortens the mean time to crash.

**GOAL A — the attributing stack.** `AddressSanitizer: heap-buffer-overflow`, **3/3 runs**:

```
ERROR: AddressSanitizer: heap-buffer-overflow
WRITE of size 1 at 0x7798ac750230 thread T5
    #0 _PyObject_InitInlineValues Objects/dictobject.c:7331:22
    #1 _PyType_AllocNoTrack       Objects/typeobject.c:2551:9
    #2 PyType_GenericAlloc        Objects/typeobject.c:2559:21
0x7798ac750230 is located 0 bytes after 688-byte region [0x7798ac74ff80,0x7798ac750230)
allocated by thread T5 here:
    #2 _PyType_AllocNoTrack       Objects/typeobject.c:2528:19
```

This is the predicted mechanism, exactly and completely: **the same thread T5** allocates at
`:2528` (sized by READ #1 at `:2524`) and overflows from `:2551` (gated by READ #2 at `:2550`),
landing **0 bytes after** the region. `dictobject.c:7331` is `values->capacity = (uint8_t)size;` —
the first byte of the `struct _dictvalues` header, which is precisely the byte pass 1 found in the
pad area.

Two corroborating faces of the same root cause, 3/3 each:
- a **READ** overflow at `_PyObject_StoreInstanceAttribute` (`dictobject.c:7555`) reading
  `values->valid` 3 bytes past the same 688-byte region;
- with `MI_TRACK_ASAN` alone and **zero CPython source change**, a `use-after-poison`
  **WRITE of size 240** with the identical `_PyObject_InitInlineValues` → `:2551` stack.

**GOAL B — TSan**, `debug-ft-nojit-tsan`, 19 `tp_flags`/`ht_cached_keys` race blocks over 10 runs:

| writer | reader | hits |
|---|---|---|
| `type_ready_managed_dict:9480` (`ht_cached_keys` store) | `_PyInlineValuesSize` (`pycore_dict.h:378`) ← `_PyType_AllocNoTrack:2526` | 9 |
| `type_ready_managed_dict:9480` | `_PyObject_InitInlineValues` (`dictobject.c:7311`) ← `_PyType_AllocNoTrack:2551` | 2 |
| `type_set_flags:458` ← `type_ready_managed_dict:9487` | `_PyType_AllocNoTrack:2537` (`PyType_HasFeature`) | 4 |

**Honest caveat:** the reads at literally `:2524` and `:2550` never appear as read PCs in the TSan
build — only `2526/2528/2537/2542/2551` do, because the compiler attributes the `tp_flags` loads
to the neighbouring uses. It is the same 8-byte `tp_flags` word in the same function and the ASan
report shows the consequence at `:2528`/`:2551` directly, but that exact line pair was not
captured. Isolating even this required removing three *competing* `tp_flags` readers
(`_PyType_GetDict`, `specialize_class_call`, `subtype_dealloc:2727`) — each of which is itself
another consumer of the racing flag, which is corroborating rather than incidental.

**GOAL C — crash rate and control.**

| build | runs | outcome |
|---|---|---|
| `debug-ft-nojit` | 20 × 30 s | **20/20 crash** (19 debug-allocator pad-byte, 1 SIGSEGV); 36 s total wall, mean < 2 s |
| `debug-gil-nojit` (control) | 10 × 30 s | **10/10 clean** — ~830k type creations, ~128M instantiations |
| `debug-ft-nojit-asan` + `PYTHONMALLOC=mimalloc` | — | `mimalloc: error: buffer overflow in heap block ... of size 760: write after 760 bytes` |

**Pass 1's signature reproduced byte-for-byte in structure**: `0x1e 0x02 0x01 0x00` at
`obj + tp_basicsize` = `capacity=30, size=2, embedded=1, valid=0`. Pass 1's was
`0x1d 0x00 0x01 0x00` = `capacity=29, size=0, embedded=1, valid=0`. Same struct, different
instance ordinal — which **confirms the forensic attribution of pass 1's dump** made above from
the byte pattern alone.

---

### ENVIRONMENT FINDING — the free-threaded ASan builds cannot see heap overflows in Python objects

This blocked the reproduction for an hour and it invalidates a class of prior evidence across the
whole campaign, so it is recorded as a finding in its own right.

**On a free-threaded build, ASan has no shadow for the `PyObject` heap.** Two independent causes,
both verified directly:

1. Free-threaded builds route every `PyObject` allocation through mimalloc, and CPython vendors
   mimalloc with ASan tracking **disabled**: `Include/internal/mimalloc/mimalloc/types.h:88` reads
   `// #define MI_TRACK_ASAN     1` — commented out. `grep -rn MI_TRACK_ASAN` over
   `configure.ac`, the Makefiles and the headers finds **only consumers** (`Objects/mimalloc/os.c`,
   `track.h`, `types.h:118`) and **no definition site**. Nothing in the build system ever sets it.
2. The obvious workaround is refused: `PYTHONMALLOC=malloc` is inside `#ifndef Py_GIL_DISABLED` at
   `Objects/obmalloc.c:797`, so it is not selectable on a free-threaded build.

Consequently, on `debug-ft-nojit-asan` the CPython **debug allocator's** pad bytes sit between the
object and any ASan redzone and fire first, at *free* time — which is why pass 1 got a
`_PyMem_DebugRawFree: bad trailing pad byte` report with no stack for the overflowing *write*, and
why my own first three ASan runs of this bug came back clean.

**Implication for the campaign: a clean ASan result on any `*-ft-*-asan` build is not evidence of
the absence of a heap-buffer-overflow in a Python object.** It should carry the same zero weight
the history document assigns to the clean-UBSan sweep (`3d40317ed24d`). Every prior slice that
certified an allocation-safety negative on an FT ASan build should be re-checked.

**The fix is cheap and should be added to the build matrix.** A working configuration exists now:

```
./configure --disable-gil --with-address-sanitizer --without-pymalloc CC=clang-21 \
            CFLAGS="-DMI_TRACK_ASAN=1 -fno-omit-frame-pointer -g"
```

plus a two-line patch dropping the `#ifndef Py_GIL_DISABLED` guard in `obmalloc.c` so
`PYTHONMALLOC=malloc` becomes selectable. Built at
`/home/danzin/projects/python_build_matrix/builds/release-ft-nojit-asan-mitrack/python`. Note the
`MI_TRACK_ASAN=1`-only variant needs **no CPython source change** at all and still produced a
`use-after-poison` with the correct stack — so the matrix can gain most of this capability with a
one-flag rebuild. All six pre-existing builds and `/home/danzin/projects/cpython` are unmodified
(verified via `git status`); the new build is a new directory not in `builds.csv`.

---

## Proposed catalog entries

Checked against all 84 existing records (`grep -rl` over `reports/` for `INLINE_VALUES`,
`tp_watched`, `insert_split_key`, `type_cache_clear`, `_PyType_AllocNoTrack`,
`type_ready_managed_dict`, `_PyType_SetFlags`): **no overlap**. The incidental hits are CPY-0082
(mentions `_PyType_SetFlags` in a different frame — see Finding 8) and CPY-0086 (mentions
`type_from_slots_or_spec` for the unvalidated-`tp_alloc` defect, not this one).

Recording is left to the campaign driver; these are the suggested shapes.

| proposed | finding | category | status | sites |
|---|---|---|---|---|
| new | 3 — `Py_TPFLAGS_INLINE_VALUES` post-publication + `_PyType_AllocNoTrack` double-read ⇒ heap-buffer-overflow WRITE | `tsan` | **reproduced** | `typeobject.c:9487`, `:9480`, `:2524`, `:2550`, `:2528`, `:2551`; `dictobject.c:7331` |
| new | 4 — `type_ready` post-publication `tp_flags` writes vs unlocked `is_readying` read | `tsan` | static-confirmed | `typeobject.c:9595`, `:9600`, `:510`, `:520`, `:6156` |
| new | 6 — `PyType_Unwatch` unguarded `tp_watched` RMW vs guarded twin | `tsan` | **reproduced** | `typeobject.c:1129`, `:1112`, `:1212`, `:6991` |
| new | 7 — `_PyType_Modified_Unlocked` runs Python inside `insert_split_key`'s raw keys mutex | `tsan` | **reproduced** | `dictobject.c:1962`, `:1971`, `:1981`; `typeobject.c:1166`, `:1222`, `:1223` |
| new | 7b — `insert_split_key` stale `dk_usable` guard across the re-entry ⇒ OOB write | `memory-pattern` | **reproduced (GIL build)** | `dictobject.c:1964`, `:1971`, `:1974`, `:1976`, `:7497` |
| new | 7c — watcher loop precedes `set_version_unlocked(type, 0)`, so a re-entrant callback never hits the `:1189` early-out ⇒ unbounded recursion | `recursion` | **reproduced (GIL build)** | `typeobject.c:1189`, `:1212`, `:1222`, `:1233` |
| new | 9 — `type_cache_clear` plain stores, inverse order | `tsan` | **reproduced** | `typeobject.c:988`, `:989`, `:990`, `:6315`, `:6318`, `:6321` |
| widen CPY-0072 | Findings 3–5 are three siblings PR gh-151394 does not cover; the record currently names three *different* ones | — | — | — |
| widen CPY-0084 | add `dictobject.c:1971` as a new external caller, 19 days old at the target ref | — | — | — |
| widen CPY-0087 | note `:1206` is now reachable from dict split-key insertion | — | — | — |

Finding 5 (`type_from_slots_or_spec` `tp_dict` mutation) and Finding 8 (`_PyType_SetFlags`) are
CONSIDER-grade and probably belong as widenings of CPY-0072 and CPY-0082 respectively rather than
as records of their own.

**Cross-agent note.** 7b and 7c are **not free-threading findings** — they reproduce on the
default GIL build, single-threaded, and belong to `memory-pattern-analyzer` and
`recursion-guard-auditor` respectively. They surfaced here only because they share a root cause
with Finding 7. They are also both in `Objects/dictobject.c`, i.e. **outside this slice**; the
campaign's `obj-dictobject` slice should inherit them rather than re-discover them. Flagging
explicitly so they are not lost between slice boundaries.

Both findings repos were checked: `/home/danzin/projects/cpython-review-findings` (84 records,
verified no overlap above) and `~/projects/cpython-tsan-findings` (`catalog/known_races.tsv` and
`reports/` — neither race present).

---

## TOOLKIT ASSESSMENT

### Precision of the new T4 rule (`publish_before_init_complete`) on this file

| candidate | conf | verdict |
|---|---|---|
| `:4958` `type_new_impl` | low | **TRUE POSITIVE** — independently recovers CPY-0072 |
| `:2007` `type_set_bases_unlocked` | medium | FALSE POSITIVE |
| `:6620` `_Py_type_getattro_stackref` | low | FALSE POSITIVE |

**1/3 overall; 0/2 on the NET NEW candidates.** Note the inversion: the one true positive is the
`low`-confidence one and the `medium` is a false positive, so the confidence signal is
anti-correlated with correctness on this file.

**The headline result is positive, though, and should be said first.** Pass 1 found CPY-0072 only
by reading, and its report proposed exactly this rule (pass-1 recall gap R1: *"an object is linked
into a globally reachable container at line A, and its fields are still being written at line
B > A … no current `ft_class` expresses that"*). The rule shipped in PR #29 and now recovers that
bug mechanically, at the same site, without a human. **That is the informed-explore loop closing
correctly.** The problem below is precision, not the concept.

### E1 — CAMPAIGN-LEVEL: the free-threaded ASan builds are blind to `PyObject` heap overflows

**This is the most consequential toolkit finding in the pass, because it invalidates evidence
rather than merely missing a bug.** Full detail in the Reproduction section; the short form:

- FT builds allocate every `PyObject` through mimalloc, and CPython vendors mimalloc with
  `MI_TRACK_ASAN` commented out (`Include/internal/mimalloc/mimalloc/types.h:88`) and never
  defined anywhere in the build system;
- `PYTHONMALLOC=malloc` — the obvious escape — is refused on FT builds
  (`Objects/obmalloc.c:797`, `#ifndef Py_GIL_DISABLED`).

So `debug-ft-nojit-asan` and `release-ft-nojit-asan` have **no ASan shadow for the object heap**.
Finding 3 is a 236-byte heap-buffer-overflow write that those builds report as nothing at all;
it took a purpose-built `-DMI_TRACK_ASAN=1` variant to see it.

**Action for the campaign, in priority order:**
1. **Stop treating a clean FT-ASan result as evidence.** It deserves the zero weight the history
   document already assigns to the clean-UBSan sweep. Any earlier slice that certified an
   allocation-safety or overflow negative on `*-ft-*-asan` should be re-checked.
2. **Add `-DMI_TRACK_ASAN=1` to the FT ASan builds in the matrix.** This alone needs **no CPython
   source change** and was sufficient to produce a correctly-stacked `use-after-poison`.
3. Optionally add the fuller variant (`--without-pymalloc` + the two-line `obmalloc.c` guard drop)
   for exact `heap-buffer-overflow` reports with allocation-site stacks.
4. Record the FT/ASan interaction in `RUN_CONTEXT.md`'s traps list alongside the RustPython-`python`
   and heredoc traps — it cost an hour here and would cost it again in every future slice.

### D3 — the T4 rule never checks that the "published" object was constructed in this frame

Both false positives have the **same** root cause, and it is one condition away from fixed.

In the true positive, the flagged object is a local bound to an allocation earlier in the same
function (`type_new_impl` builds `type` via the metatype's `tp_alloc`). In both false positives the
flagged object is a bare **function parameter** that arrived fully constructed and long published:

- `:6620` — `_Py_type_getattro_stackref(PyTypeObject *type, …)`; `type` is whatever `T.x` was
  evaluated on. `PyType_Ready` at `:6586` is a *lazy readiness check* that returns immediately
  (`:9613`) for every type reachable from Python.
- `:2007` — `type_set_bases_unlocked(PyTypeObject *type, …)`; `type` is a live class having its
  `__bases__` reassigned. `add_all_subclasses` re-links an existing type, it does not publish a new
  one.

The rule keys on the *name* of a publishing API and infers construction from it.

**Proposal:** require the flagged object's defining assignment **in the same function** to be an
allocation — `tp_alloc(...)`, `PyObject_New*`, `PyObject_GC_New*`, `PyType_GenericAlloc`,
`_PyType_AllocNoTrack`, `_PyObject_GC_New` — or the enclosing function to match
`*_new*` / `*_init*` / `*_from_*`. If the object is a parameter with no defining assignment in the
function, suppress (or cap at `low` and say so in `detail`). On this file that single condition
takes precision from **1/3 to 1/1** and loses nothing.

### D4 — the T4 rule loses field identity across call hops and does not check the writer's guard

The two low-confidence findings report their member as `type.*slot (computed from tp)` and
`type.*ptr (computed from type)`. Those are not field names; they are the scanner announcing that
transitive writer tracking lost the identity. Once the field cannot be named, the rule cannot ask
the question that decides the case on *this* file — *is this one of the fields the design comment
at `:65–68` says `TYPE_LOCK` protects?* For `:6620` the answer is yes (`tp_version_tag`), and the
write at `:6359` is three lines below a `BEGIN_TYPE_LOCK()`.

**Proposal (two parts):**
1. When `call_hops >= 1`, check whether the **writer function's** body contains a lock token or a
   lock-held assertion; if it does, suppress. `set_version_unlocked` is reached only from
   `assign_version_tag`, which opens with `ASSERT_TYPE_LOCK_HELD()` — that alone kills `:6620`.
   (This depends on D2 below.)
2. When the resolved member is a `*`-prefixed placeholder, put the **actual write expression** and
   the writer's guard state into `detail` rather than the placeholder, so a triager is not forced
   to re-walk the hop chain by hand.

### D2 — pass 1's `#define`-alias defect is STILL UNFIXED, and pass 2 can now prove it is cheap

`scan_ft_races` reports `lock_held_functions = 0` on a file containing **21** `BEGIN_TYPE_LOCK()`
call sites and **18** `ASSERT_TYPE_LOCK_HELD()` call sites. Pass 1 raised this as D2 and proposed
the fix; it did not land.

Pass 2 can now make the argument concretely, because the preflight measured the *other* scanners on
the same file (`preflight/include_map.md` §4.1): **`scan_lock_discipline` resolves this file's
aliases correctly** — `vocabulary_resolved=66`, `critical_section_functions=23`, and its own source
comments cite `BEGIN_TYPE_LOCK` by name as the motivating example. So **the resolution code already
exists in this toolkit; `scan_ft_races.py` simply does not share it.**

**Proposal:** lift `scan_lock_discipline`'s object-like-`#define` resolution into the shared utility
module and have `scan_ft_races` consume it, plus treat a body containing `ASSERT_*_LOCK_HELD()` /
`_Py_CRITICAL_SECTION_ASSERT_*` as lock-held-by-contract (the same way `*_lock_held` naming already
is). Near-zero cost. Effect on this file: enables D4's suppression, and unblocks the T1 rule on the
two real asymmetries it currently cannot see (R2 below).

### R3 — NEW recall gap: the rule only looks for post-publication writes in the *caller's* frame

This is the gap that cost the two real findings.

The scanner flagged `type_new_impl:4958` because `PyType_Ready(type)` at `:4953` is in the *same
function* as the later write. But the actual publication happens **inside** `PyType_Ready`, at
`type_ready_add_subclasses:9581` — and `type_ready` then performs **three** plain field writes after
its own publication point:

| line | write | reached via |
|---|---|---|
| `:9487` | `type_add_flags(type, Py_TPFLAGS_INLINE_VALUES)` | `type_ready:9585` → `type_ready_managed_dict` |
| `:9480` | `et->ht_cached_keys = _PyDict_NewKeysForClass(et)` | same |
| `:9595` | `type_add_flags(type, Py_TPFLAGS_READY)` | inline |
| `:9600` | `type_clear_flags(type, Py_TPFLAGS_READYING)` | `stop_readying:510` |

The scanner reported **none** of them, because it never considers the publishing function itself as
a candidate frame. Findings 3 and 4 — including the one with a heap-buffer-overflow consequence —
are entirely in that blind spot.

**Proposal:** when a function body contains a known-publishing call, treat the **remainder of that
same function** as post-publication and flag plain field stores on the published object in it,
including through one hop of a same-file static helper (`type_add_flags` → `type_set_flags`). Seed
the publishing-call list with `add_subclass` alongside `PyType_Ready` — `add_subclass` is the
*actual* publication primitive and `PyType_Ready` is merely its most common wrapper. That inversion
matters: keyed on `PyType_Ready` the rule finds the caller and misses the callee; keyed on
`add_subclass` it finds both.

### R4 — NEW recall gap: no rule models "the same value is read twice and the two reads must agree"

The memory-unsafety mechanism in Finding 3 is not the unsynchronised write on its own. It is that
`_PyType_AllocNoTrack` reads `type->tp_flags & Py_TPFLAGS_INLINE_VALUES` **twice** — at `:2524` to
size the allocation and at `:2550` to decide whether to initialise it — and the two reads can
disagree. Neither read is wrong in isolation; no existing rule expresses "these two must agree".

**This is not a one-off, and it is not free-threading-specific.** The findings catalog already
holds four instances of the identical shape, all single-threaded re-entrancy:

- **CPY-0049** `Struct_pack_impl` — allocates the writer with `s_size` at `:2473`, re-reads
  `self->s_size` at `:2485` after user code ran ⇒ oversized `bytes` filled with heap, and SIGSEGV.
- **CPY-0052** `s_unpack_internal` — sizes the result tuple with `soself->s_len` at `:2058`, fills
  it with a different bound at `:2101` ⇒ `PyTuple_SET_ITEM` overrun.
- **CPY-0044** `unpackiter_iternext` — captures `buf.len` at construction, re-reads `s_size` each
  step.
- **CPY-0046** — same object, zero stride from a re-read `s_size`.

**And this pass produced two more, one of them reproduced as a heap overflow on the default
build.** Finding 7's second defect is the same shape in `Objects/dictobject.c`:

```c
    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {   // :1964  guard read
        _PyType_Modified_Unlocked(type);             // :1971  arbitrary Python; consumes slots
        ix = keys->dk_nentries;                      // :1974  re-read, guard NOT re-checked
        PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(keys)[ix];   // :1976  OOB write
```

That one is confirmed by ASan (`heap-buffer-overflow`, `dictobject.c:7497`, 16 bytes past a
304-byte region) and by `_PyDict_CheckConsistency` failing `0 <= dk_usable && dk_usable <= usable`
5/5 from a single re-entry — **on `release-gil-nojit` and `debug-gil-nojit`**, i.e. with no
free-threading involved at all.

Six catalogued-or-reproduced findings of one shape and no rule for it. **Proposal — new rule
`size_then_use_reread` (medium):** within one function, flag when the same expression
(`X->field`, or `X->field & CONST`) is read at a site feeding an allocation size and re-read at a
site feeding a write bound, loop bound, or a branch guarding a write, with an intervening call or
allocation. Restricting the expression form to `X->field[ & CONST]` keeps it tractable. Because the
catalogued instances are single-threaded, this belongs in `scan_memory_patterns`, with
`scan_ft_races` inheriting the free-threaded variant (where the intervening "call" is instead
another thread's store).

Of everything in this assessment, **R4 is by far the highest-value addition**. It is the only
proposal here that would have found **memory-unsafety** bugs rather than synchronisation smells;
it has four independent prior confirmations in the catalog (CPY-0044/0046/0049/0052) and two more
reproduced in this pass (Findings 3 and 7); and one of this pass's two is a **heap-buffer-overflow
on the default GIL build**, so the rule pays off outside free-threading entirely. The four
catalogued instances are all in `Modules/_struct.c`, which is why the shape has read as a
`_struct`-specific quirk rather than a class — this pass shows it in `Objects/typeobject.c` and
`Objects/dictobject.c` too.

Note the intervening event has two forms and the rule should model both: an intervening **call
that runs user Python** (the single-threaded re-entrancy form — CPY-0044/0046/0049/0052 and
Finding 7) and an intervening **store by another thread** (the free-threaded form — Finding 3).
Same read-read-disagree structure, two different ways for the value to change in between.

### R2 — pass 1's T1 gap re-confirmed, with two concrete misses

One T1 finding on this file, and it is a false positive. Two textbook T1 shapes were missed:

- **`tp_watched`** — written under `BEGIN_TYPE_LOCK()` at `:1112`, written **unlocked** at `:1129`,
  read under the lock at `:1212`/`:1214` and unlocked at `:6988`/`:6991` (Finding 6). Missed
  because of D2: the guard is the unresolved alias `BEGIN_TYPE_LOCK`.
- **`type_cache_entry.{version,name,value}`** — three `_Py_atomic_store_*` at `:6218`–`:6230`,
  three **plain** stores at `:988`–`:990` (Finding 9). Both writers, same three fields, same struct.
  Missed for a different reason: the two writers are **5,200 lines apart in different functions**.
  Worth checking whether the T1 rule has a proximity window or a per-field site cap; if it does,
  this file is the argument for widening it to whole-file for struct-typed fields.

### Denominator honesty

`iternext_functions = 1` on this file, and that one is not an iterator over a shared container.
The **zero T3 findings are structural, not earned** — `Objects/typeobject.c` has essentially no
iterator surface, so the T3 rules (the scanner's highest-confidence class) had no denominator here.
Do not read this file's clean T3 result as evidence about the T3 rules.
`lock_held_functions = 0` is likewise not a property of the file (which is one of the most
lock-dense in `Objects/`) but of defect D2.

### What worked

- **The T4 rule recovering CPY-0072 unaided** — pass 1 proposed it, PR #29 shipped it, pass 2
  confirms it fires at the right site. Keep it; fix its precision with D3.
- **The preflight's cross-scanner denominator table** (`include_map.md` §4.1) turned pass 1's D2
  from "the scanner should do this" into "another scanner in this toolkit already does this". That
  comparison is what makes the fix obviously cheap, and it only exists because the preflight
  measured every scanner on the same file. Worth institutionalising.
- **The history document's §4.2 hand-off** pointed directly at `_PyType_Modified_Unlocked`'s
  19-day-old external caller, which is Finding 7. That finding is not reachable by any static rule
  in the toolkit — it required knowing that a `static` function had just been exported and to whom.
  The `git log -L` + revert-archaeology pass is earning its cost.

