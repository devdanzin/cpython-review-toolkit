# null-safety-scanner — obj-mappings slice

Target: `/home/danzin/projects/cpython` @ `4f3be1b5777`
Files: `Objects/dictobject.c` (8,597) and `Objects/setobject.c` (3,228) — **both read end to end**.
Builds: `~/projects/python_build_matrix/builds/<name>/python` @ `a1d580430c8`.
`git diff a1d580430c8 4f3be1b5777 -- Objects/dictobject.c Objects/setobject.c` is **empty** — the
matrix builds are byte-identical to the target ref for both files, so every repro below is valid
for the reviewed ref.

---

## 1. Denominator line

| population | count |
|---|---|
| `scan_null_checks.sample.json` findings for this slice | **1** (`unchecked_alloc`, `copy_lock_held_untracked:4489/4494`) |
| …against `Objects/` tree-wide | 4 |
| scanner denominators (slice-scoped) | 403 functions, 1,333 assignment sites, 214 fallible sources resolved, 125 local nullable helpers |
| `deref_before_check` in slice | 0 — **evidential**, the rule ran (403 functions) and matched nothing |
| `decref_of_nulled_outparam` in slice | 0 — **structural**; the envelope's own `decref_of_nulled_outparam_call_sites: 8` shows the rule's denominator here is 8 call sites, none of which is a `PyObject**` out-param wrapper. Not a clean bill of health. |
| lines read by hand | 11,825 (100% of the slice) |
| `assert()` sites examined | **265** (237 dict + 28 set); 110 of them dereference a pointer or index |
| `_PyDict_NotifyEvent` call sites examined | **14** |
| `insert_split_key` call sites examined | **4** |
| `mp->ma_values->values[...]` index sites examined | **13** |
| `PyDict_GetItem`-family call sites examined | **3** |
| optional-argument (`failobj`/`default`) parameters examined | **5** |

**Scanner net-new contribution to this report: 0 live findings.** Its one candidate is
already-recorded CPY-0079. All six findings below came from reading. Lesson 1 held exactly.

---

## 2. Findings

### Already recorded — confirmed in one line each

- **CPY-0079** `dictobject.c:4489/4494 copy_lock_held_untracked` — still present verbatim: `d =
  frozendict_new_untracked(...)` / `dict_new_untracked(...)`, then `assert(!_PyObject_GC_IS_TRACKED(d))`
  with no NULL test. Confirmed, not re-litigated. **Its sibling hunt is the body of this report.**
- **CPY-0096** `dictobject.c:1971 insert_split_key` — confirmed; findings 4 and 5 below are its
  NULL-deref consequences on the **default GIL build**, which the brief assigned to this slice.

### CPY-0079 sibling census (the assigned hunt)

Of the 110 deref-bearing `assert()`s, the ones that guard a **genuinely fallible** value — i.e. where
the asserted predicate is falsifiable by a callback rather than by a broken invariant — are:

| # | site | asserted | fallible because | verdict |
|---|---|---|---|---|
| 0 | `dictobject.c:4494` | `!_PyObject_GC_IS_TRACKED(d)` | allocation may fail | CPY-0079 (recorded) |
| 1 | `dictobject.c:5075` | `j >= 0` | `lookdict_index` returns `DKIX_EMPTY` | **FIX** (finding 1) |
| 2 | `dictobject.c:2963` | `hashpos >= 0` | same | **FIX** (finding 2) |
| 3 | `dictobject.c:2944`/`:2946` | `i < size` | search key absent from the order array | **FIX** (finding 6) |
| 4 | `dictobject.c:2967` | `old_value == mp->ma_values->values[ix]` | `ix` stale after a callback | **FIX** (fires in finding 6's repro) |
| 5 | `dictobject.c:7518`/`:7526` | `dict->ma_values == values` | callback can detach/combine | CONSIDER |
| 6 | `dictobject.c:3209`, `:5737`, `:5860`, `:5984`, `:6279` | `value != NULL` on a split slot | order/values desync | CONSIDER |
| 7 | `dictobject.c:2242`, `:2255` | `oldvalues->values[index] != NULL` | same | CONSIDER |

**Answer to the assigned question: 4 new FIX-grade siblings of CPY-0079's shape, plus 3 CONSIDER
clusters (9 sites), out of 110 deref-bearing asserts.**

---

### [FIX] `dict.popitem()` returns a tuple containing a raw C NULL — `Objects/dictobject.c:5051`, `:5066`, assert at `:5075`

**Function:** `dict_popitem_impl`

**Mechanism.** The function reads the last live entry (`key` at :5050, `ep0` at :5044), then calls
`_PyDict_NotifyEvent(PyDict_EVENT_DELETED, self, key, NULL)` at :5051 — which runs a dict-watcher
callback and, if the callback fails, `PyErr_FormatUnraisable` → `sys.unraisablehook`, i.e. arbitrary
Python (`_PyDict_SendEvent:8309`/`:8314`). Every value used after that line was captured before it:
`hash = unicode_get_hash(key)` at :5052 reads a possibly-freed key; `STORE_KEY(&ep0[i], NULL)` at
:5054 writes through a possibly-freed entries array; and

```c
j = lookdict_index(self->ma_keys, hash, i);   /* :5074  fallible: returns DKIX_EMPTY */
assert(j >= 0);                               /* :5075  the ONLY guard */
assert(dictkeys_get_index(self->ma_keys, j) == i);
dictkeys_set_index(self->ma_keys, j, DKIX_DUMMY);  /* :5077  indices[-1] = -2 on release */
PyTuple_SET_ITEM(res, 0, key);
PyTuple_SET_ITEM(res, 1, value);               /* :5080  value may be NULL */
STORE_KEYS_NENTRIES(self->ma_keys, i);         /* :5081 */
```

`assert()` is compiled out under `NDEBUG`, so the two builds fail differently — the exact CPY-0079
signature the scanner's own `detail` string describes.

**Guarded twin, and what it actually defends.** The twin is *inside the same function*: lines
5013–5021 comment that "this allocation could trigger a garbage collection which could empty the
dict", allocate `res` first, and re-check `self->ma_used` at :5024. `keys_lock_held:3872`,
`values_lock_held:3921` and `items_lock_held:3982` use the identical `n != mp->ma_used` re-check with
the comment *"Durnit. The allocations caused the dict to resize."*, plus the explicit "Nothing we do
below makes any function calls."

Per lesson 3: **that guard addresses GC-during-allocation only.** It is not evidence of safety
against a watcher callback, which is strictly more powerful (arbitrary Python, not a deferred
collection — `_PyObject_GC_Link` only sets the eval-breaker bit, so `PyTuple_New` genuinely cannot
run Python here). The keys/values/items twin is *correct* precisely because it makes no calls at all
after the check; `dict_popitem_impl` makes one, at :5051.

The systemic twin is `dict_dealloc:3652`, the **only one of the 14 `_PyDict_NotifyEvent` call sites**
that protects itself from the callback — via `_PyObject_ResurrectStart`/`_PyObject_ResurrectEnd`.
That guard addresses *resurrection*, not state invalidation.

**Reproduction.** `repro/popitem_null_in_tuple.py` (keys kept alive, so the crash is the NULL, not a
UAF):

| build | result | N/M |
|---|---|---|
| `debug-gil-nojit` | `Assertion 'j >= 0' failed` at `Objects/dictobject.c:5075` (SIGABRT, 134) | 8/8 |
| `debug-ft-nojit` | same assertion, same line | 8/8 |
| `release-gil-nojit` | `popitem() -> ('key5', <NULL>)`, then `t[1]` → SIGSEGV (139) | 8/8 |
| `release-ft-nojit` | same | 8/8 |

`repro/popitem_notify_reentry.py` (hook calls `d.clear()`) gives the memory-safety view — ASan on
`release-gil-nojit-asan`:

```
ERROR: AddressSanitizer: heap-use-after-free READ of size 8
    #0 dict_popitem_impl Objects/dictobject.c:5052
freed by: unicode_dealloc <- dictkeys_decref Objects/dictobject.c:509
          <- clear_lock_held <- PyDict_Clear Objects/dictobject.c:3178
          <- dict_clear <- format_unraisable_v Python/errors.c:1737
```
(24/24 SIGSEGV across all four non-ASan builds; full log in `repro/asan_popitem.txt`.)

**Note on the release path.** `dictkeys_set_index(keys, -1, DKIX_DUMMY)` writes one byte *before*
`dk_indices`, i.e. into `dk_nentries`, and :5081 then stores `i` into `dk_nentries` — and when the
callback ran `d.clear()`, `self->ma_keys` is the **immortal, process-wide `empty_keys_struct`**
(`:645`) shared by every empty dict in the interpreter.

---

### [FIX] `del d[k]` / `d.pop(k)` — `Py_DECREF(NULL)` in `delitem_common` — `Objects/dictobject.c:3038` → `:2963`, `:2990`

**Function:** `_PyDict_DelItem_KnownHash_LockHeld` → `delitem_common`

**Mechanism.** Identical shape at the other notify site:

```c
ix = _Py_dict_lookup(mp, key, hash, &old_value);         /* :3030 */
_PyDict_NotifyEvent(PyDict_EVENT_DELETED, mp, key, NULL);/* :3038  ARBITRARY PYTHON */
delitem_common(mp, hash, ix, old_value);                 /* :3039  ix and old_value STALE */
```
and inside `delitem_common`:
```c
hashpos = lookdict_index(mp->ma_keys, hash, ix);   /* :2962 */
assert(hashpos >= 0);                              /* :2963  ONLY guard */
dictkeys_set_index(mp->ma_keys, hashpos, DKIX_DUMMY);  /* :2976 */
ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[ix];         /* :2978 */
old_key = ep->me_key;                              /* :2979  NULL if already deleted */
Py_DECREF(old_key);                                /* :2990  Py_DECREF(NULL) */
Py_DECREF(old_value);                              /* :2992  second DECREF */
```

**Guarded twin.** Same as finding 1 (`dict_dealloc:3652`, and `keys/values/items_lock_held`'s
allocation re-check). Additionally `dict_dict_merge:4290` shows the file *does* know how to detect
mid-operation mutation — `if (orig_size != other->ma_used) { PyErr_SetString(..., "dict mutated
during update"); }` — that guard addresses *the merge source being mutated*, and is exactly the
check `delitem_common`'s caller lacks for its own target.

**Reproduction.** `repro/delitem_common_stale_ix.py`.

| build | result | N/M |
|---|---|---|
| `debug-gil-nojit` | `Assertion 'hashpos >= 0' failed` at `Objects/dictobject.c:2963` (134) | 8/8 |
| `debug-ft-nojit` | same | 8/8 |
| `release-gil-nojit` | SIGSEGV, gdb frame `#0 delitem_common … at Objects/dictobject.c:2990  Py_DECREF(old_key)` | 8/8 |
| `release-ft-nojit` | SIGSEGV (139) | 4/4 |

**Reach:** `_PyDict_Pop_KnownHash:3307` and `delitemif_lock_held:3083` reach the identical
`delitem_common` with the identical staleness, so `d.pop(k)` and `_PyDict_DelItemIf` share the defect.

---

### [FIX] `reversed(dict)` reads `DK_ENTRIES(k)[di_pos]` with no upper bound — `Objects/dictobject.c:6283`, `:6294` (seeded at `:5636`)

**Function:** `dictreviter_iter_lock_held`

This is the highest-severity finding: **pure Python, no `_testcapi`, no threads, no watchers, no
allocation failure** — and it reproduces on released 3.12 and 3.14 as well as on the target ref.

**Mechanism.** `dictiter_new:5636` seeds `di->di_pos = load_keys_nentries(dict) - 1` — the
`dk_nentries` of the keys object *that existed when `reversed()` was called*. The only staleness
check in `dictreviter_iter_lock_held` is `di->di_used != d->ma_used` (:6261), which constrains
`ma_used` and says nothing about `dk_nentries`. The dereference is then:

```c
if (i < 0) { goto fail; }                                    /* :6272  ONLY bound */
PyDictUnicodeEntry *entry_ptr = &DK_UNICODE_ENTRIES(k)[i];   /* :6283 */
while (entry_ptr->me_value == NULL) {                        /* :6284  OOB READ */
    if (--i < 0) { goto fail; }
    entry_ptr--;
}
key = entry_ptr->me_key;                                     /* :6290 */
```

`d.clear()` followed by one insertion replaces `ma_keys` with a fresh `PyDict_MINSIZE` object with
**5 usable entries** while restoring `ma_used` to its old value — so `di_used == ma_used` passes and
`di_pos` is still `N-1`.

**Guarded twin.** The three **forward** iterators, all in the same file, all bound `i` against the
*current* `dk_nentries` before dereferencing:

| forward iterator | bound |
|---|---|
| `dictiter_iternextkey_lock_held` | `n = k->dk_nentries` (:5740), `if (i >= n) goto fail;` (:5747) |
| `dictiter_iternextvalue_lock_held` | :5863 / :5870 |
| `dictiter_iternextitem_lock_held` | :5987 / :5994 |
| `dictiter_iternext_threadsafe` (FT) | :6108 / :6117, :6134 |

They can afford the weaker `di_pos` seed because a forward `di_pos` starts at 0 and only grows.
The reverse iterator starts at the far end — it is the one that needs the bound, and it is the one
that does not have it. Their guard addresses *exactly this threat model* (a `di_pos` that outran the
table), so this twin is load-bearing, unlike the ones in findings 1–2.

The split-table arm (:6276–:6279) is **not** affected, and for an instructive reason:
`dictiter_new:5633` seeds a split dict's `di_pos` from `used - 1`, and `values->size == ma_used`, so
the `di_used == ma_used` check *does* bound it. The defect is precisely that the combined arm's seed
(`dk_nentries`) and its only invariant check (`ma_used`) are different quantities.

**History.** `git log -L 6253,6300:Objects/dictobject.c` shows the last touch to this exact code is
`2e3d873d3bd` (bpo-38555, 2019, Serhiy Storchaka, *"Fix an undefined behavior"*), which hoisted the
`i < 0` test and rewrote the loop. That fix was about the **lower** bound / forming `&entries[-1]`.
The upper bound was never added. bpo-38525 (`type-crash`, 2019) added the `di_used != ma_used` check
that this repro walks straight through. Tracker search (`gh api -X GET search/issues`) finds nothing
open covering it.

**Reproduction.** `repro/reversed_dict_oob.py` (default N=60000) and
`repro/reversed_dict_oob_variants.py`.

| build | result | N/M |
|---|---|---|
| `debug-gil-nojit` | SIGSEGV (139) | 6/6 |
| `release-gil-nojit` | SIGSEGV (139) | 6/6 |
| `debug-ft-nojit` | SIGSEGV (139) | 6/6 |
| `release-ft-nojit` | SIGSEGV (139) | 6/6 |
| `~/venvs/.../python` **3.12.13 (released)** | SIGSEGV (139) | 5/5 |
| `/usr/bin/python3.14` (released) | SIGSEGV (139) | 1/1 |

ASan (`release-gil-nojit-asan`, N=200), full log `repro/asan_reversed.txt`:

```
ERROR: AddressSanitizer: heap-buffer-overflow READ of size 8
    #0 dictreviter_iter_lock_held Objects/dictobject.c:6284
    #1 dictreviter_iternext       Objects/dictobject.c:6354
    #2 builtin_next               Python/bltinmodule.c:1776
0x... is located 42 bytes after 118-byte region
```

All five reverse entry points reach it — `reversed(d)`, `reversed(d.keys())`, `reversed(d.items())`,
`reversed(d.values())` (via `dictkeys_reversed:7050`, `dictitems_reversed:7162`,
`dictvalues_reversed:7252`) and the `DICT_KEYS_GENERAL` arm at :6294 — 5/5 SIGSEGV on
`release-gil-nojit`.

---

### [FIX] `insertdict` dereferences `mp->ma_values` after `insert_split_key` ran arbitrary Python — `Objects/dictobject.c:2027-2030` → `:1995`

**Function:** `insertdict` → `_PyDict_InsertSplitValue`

**Mechanism.** This is CPY-0096's mechanism landing as a **plain NULL dereference on the default GIL
build** — the class the brief assigned to this slice.

```c
if (_PyDict_HasSplitTable(mp) && PyUnicode_CheckExact(key)) {   /* :2027  the test */
    ix = insert_split_key(mp->ma_keys, key, hash);              /* :2028  RUNS PYTHON */
    if (ix != DKIX_EMPTY) {
        _PyDict_InsertSplitValue(mp, key, value, ix);           /* :2030 */
```
```c
PyObject *old_value = mp->ma_values->values[ix];                /* :1995  ma_values == NULL */
```

`insert_split_key:1971` calls `_PyType_Modified_Unlocked(type)`, which runs a type-watcher callback
and `PyErr_FormatUnraisable` (`typeobject.c:1222`/`:1223`). Critically, that region of
`insert_split_key` (lines 1962–1981) is **not** inside `#ifdef Py_GIL_DISABLED` — only the
lock-free prelude and the type-lock critical section are — so the GIL build takes it too.

The re-entrant Python stores a non-unicode key, which drives
`insert_combined_dict:1904` → `insertion_resize` → `dictresize` → `set_values(mp, NULL)` (:2264).
`_PyDict_HasSplitTable(mp)` was evaluated at :2027, **before** the call that falsifies it.

**Guarded twin.** `_Py_dict_lookup_threadsafe:1625-1641` performs the *same* split-table value read
and does it correctly: it re-loads `ma_values` after the lookup, bails on NULL, re-checks
`ix < values->capacity`, and re-checks `values` did not change:

```c
PyDictValues *values = _Py_atomic_load_ptr(&mp->ma_values);
if (values == NULL) goto read_failed;                       /* :1626-1628 */
uint8_t capacity = _Py_atomic_load_uint8_relaxed(&values->capacity);
if (ix >= (Py_ssize_t)capacity) goto read_failed;           /* :1631 */
```

Per lesson 3: **that guard's threat model is a different thread mutating the dict during a lock-free
read** — it is `#ifdef Py_GIL_DISABLED`-only and says nothing about single-threaded re-entrancy. It
is nonetheless the correct shape, and the same three lines placed at :1995 and :4861 fix both
findings. The other 11 `ma_values->values[...]` sites in the file (`:1398`, `:3206`, `:4685`,
`:5098`, `:5737`, `:5859`, `:5983`, `:6278`, plus the debug-only `:791` and the two macro/assert
sites) all re-test `_PyDict_HasSplitTable` immediately before the index, with no intervening call.

**Reproduction.** `repro/insertdict_ma_values_null.py`.

| build | result | N/M |
|---|---|---|
| `debug-gil-nojit` | SIGSEGV; gdb `#0 _PyDict_InsertSplitValue … Objects/dictobject.c:1995  PyObject *old_value = mp->ma_values->values[ix]; #1 insertdict … :2030 #2 PyObject_SetItem` | 6/6 |
| `release-gil-nojit` | SIGSEGV (139) | 6/6 |
| `debug-ft-nojit` | **hang** (rc=124) — this is CPY-0096's already-recorded self-deadlock on `keys->dk_mutex`, reached first | 3/3 |
| `release-ft-nojit` | hang (rc=124) | 3/3 |

The FT builds deadlock before they can crash, which is why this consequence was invisible in
CPY-0096's FT-focused reproduction and why the brief handed it to this slice.

---

### [FIX] `dict.setdefault()` — same `ma_values` NULL deref — `Objects/dictobject.c:4858` → `:4861`

**Function:** `dict_setdefault_ref_lock_held`

The sibling of finding 4, one function over and structurally identical: the `_PyDict_HasSplitTable`
test is at :4858 and `PyObject *value = mp->ma_values->values[ix];` is at :4861, with
`insert_split_key` (:4859) in between.

**Reproduction.** `repro/setdefault_ma_values_null.py`.

| build | result | N/M |
|---|---|---|
| `debug-gil-nojit` | SIGSEGV; gdb `#0 dict_setdefault_ref_lock_held … Objects/dictobject.c:4861 #1 dict_setdefault_impl :4954 #2 dict_setdefault` | 1/1 (gdb) |
| `release-gil-nojit` | SIGSEGV (139) | 6/6 |
| `release-ft-nojit` | hang (rc=124), same CPY-0096 deadlock | 6/6 |

**Remaining `insert_split_key` call sites (denominator 4):** `store_instance_attr_lock_held:7456`
(CONSIDER — `values` is the inline array so there is no NULL, but the write at :7513 lands in an
array `dictresize` may have invalidated, and the only consistency guard is `assert(dict->ma_values ==
values)` at :7518/:7526) and `_PyDict_NewKeysForClass:7286` (CONSIDER — borrowed `attrs`/`key` held
across it; refcount-auditor's class, routed not duplicated).

---

### [FIX] `delete_index_from_values` scans off the end of the insertion-order array — `Objects/dictobject.c:2943`

**Function:** `delete_index_from_values`, reached from `delitem_common:2971`

**Mechanism.** The purest instance of the CPY-0079 shape in the slice — a **search loop whose only
termination bound is an `assert`**:

```c
uint8_t *array = get_insertion_order_array(values);
int size = values->size;
int i;
for (i = 0; array[i] != ix; i++) {   /* :2943 */
    assert(i < size);                /* :2944  the ONLY bound */
}
assert(i < size);                    /* :2946 */
size--;
for (; i < size; i++) { array[i] = array[i+1]; }   /* :2949  shifts from OOB */
```

The order array is at most `SHARED_KEYS_MAX_SIZE` bytes. When the re-entrant Python at
`_PyDict_NotifyEvent:3038` removes the same key, `ix` is no longer in the array, and on a release
build the loop scans heap memory for a byte equal to `ix`.

**Guarded twin.** `clear_embedded_values:3111-3113` does the mirror-image walk and *is* bounded —
`PyObject *refs[SHARED_KEYS_MAX_SIZE]; assert(nentries <= SHARED_KEYS_MAX_SIZE); for (i = 0; i <
nentries; i++)`. Its guard addresses the same threat (an index beyond the array) with a real loop
bound rather than an assert. `set_next:721` in `setobject.c` is the same idea in the sibling file:
`while (i <= mask && (entry->key == NULL || ...))` — the bound is in the loop condition, not in an
assert.

**Reproduction.** `repro/delete_index_from_values_scan.py`. **Honest result — this one does not
SIGSEGV on a plain release build:**

| build | result | N/M |
|---|---|---|
| `debug-gil-nojit` | `Assertion 'old_value == mp->ma_values->values[ix]' failed` at `Objects/dictobject.c:2967` — the *neighbouring* assert (census row 4) fires one line earlier (SIGABRT, 134) | 6/6 |
| `debug-ft-nojit` | same | 6/6 |
| `release-gil-nojit` | **exit 0**, silent corruption: `obj` had `a,b,c`; after `del d['b']` the dict is `{'a': 1}` — entry `c` vanished and `ma_used` is wrong | 0/6 crash |
| `release-ft-nojit` | exit 0, same silent corruption | 0/6 crash |
| `release-gil-nojit-asan` | `AddressSanitizer: heap-buffer-overflow READ`, `#0 delete_index_from_values Objects/dictobject.c:2943  #1 delitem_common :2971  #2 _PyDict_DelItem_KnownHash_LockHeld :3039` | 1/1 |

Log: `repro/asan_delidx.txt`. Classified FIX on the strength of the ASan report plus the silent
entry loss, not on a segfault I did not observe.

---

## 3. Classes bounded — checked here, clean, with the denominator

- **`PyDict_GetItem`-family NULL-without-exception. 3 call sites in the slice, 0 findings.**
  `dictitems_xor_lock_held:6812` (`_PyDict_GetItem_KnownHash`) has the correct
  `if (val1 == NULL) { if (PyErr_Occurred()) goto error; ... }` at :6815-6819;
  `dictitems_contains:7077` uses the tri-state `PyDict_GetItemRef` and propagates -1;
  `_PyDict_NewKeysForClass:7280` uses `PyDict_GetItem` on `tp_dict` with the interned
  `_Py_ID(__static_attributes__)` — the key's hash cannot fail and a unicode-keyed type dict cannot
  dispatch a user `__eq__`, so "NULL means error" is unreachable there. The result is NULL-tested
  (`attrs != NULL && PyTuple_Check(attrs)`). Clean.

- **CPY-0030's shape (`Py_NewRef(failobj)` with no NULL check). 5 optional-argument parameters
  examined, 0 findings.** `dict_pop_default:3390` is the **guarded twin** of
  `_odict_popkey_hash:1098` — it has the `if (default_value != NULL)` test that odict lacks, so
  `dict.pop(k)` with an inconsistent `__eq__` raises `KeyError` where `OrderedDict.pop(k)` segfaults.
  `dict.get` and `dict.setdefault` take Argument-Clinic `default: object = None`, never NULL.
  `PyDict_SetDefault`/`PyDict_SetDefaultRef` would `Py_NewRef(NULL)` on a NULL `defaultobj`, but
  that violates their documented C contract and is not Python-reachable — ACCEPTABLE.
  `setobject.c` has **no** optional-argument parameter of this shape at all (`set.pop` takes none),
  so the class is *structurally* absent there.

- **Deref-before-check in the iterator/view code. 5 iterator `iternext` implementations + 4 view
  types examined; 1 finding (#3 above), the rest clean.** The three forward iterators and the
  free-threaded `dictiter_iternext_threadsafe` all bound before dereferencing; every view entry
  point (`dictkeys_iter:6622`, `dictitems_iter:7059`, `dictvalues_iter:7171`, the three
  `*_reversed`, `dictkeys_contains:6632`, `dictitems_contains:7071`, `dictview_len:6455`) tests
  `dv->dv_dict == NULL` first. `setiter_iternext:1103` tests `si_set == NULL` first.

- **`ma_values` / split-table indexing. 13 sites examined; 2 FIX (findings 4, 5), 1 CONSIDER
  (`:6278`, latent — bounded by the `used`-derived seed), 10 clean.** The clean ones all re-test
  `_PyDict_HasSplitTable` with no intervening call. `_Py_dict_lookup:1398` deserves a note: it
  branches on `dk->dk_kind == DICT_KEYS_SPLIT` rather than on `ma_values != NULL`. Those are *not*
  the same predicate, but the window where they disagree is unreachable —
  `dictresize:2262-2264` publishes the new (non-SPLIT) keys **before** NULLing `ma_values`, and
  `clear_lock_held:3155-3156` NULLs `ma_values` first but under the dict lock with no Python in
  between, and the lock-free reader (`:1626`) checks for NULL explicitly. Clean, but fragile.

- **`ma_keys` / `so_table` captured across a Python-running call.** `setobject.c` is *clean here and
  is the model*: `set_add_entry_takeref:264-295` has the documented `restart:` loop and
  short-circuits `table != so->table || entry->key != startkey` so the stale `entry` is never
  dereferenced after the table check fails; `set_compare_entry_lock_held:159` and
  `set_compare_threadsafe:117` do the same. `set_lookkey:423-426` even loops on
  `SET_LOOKKEY_CHANGED`. `dictobject.c`'s equivalents (`compare_generic:1229`,
  `compare_unicode_generic:1173`) use the same short-circuit correctly. **The dict-side failures are
  all at the `_PyDict_NotifyEvent` / `insert_split_key` sites, not in the lookup machinery.**

- **`setobject.c` overall: 0 NULL-safety findings out of 28 asserts, 3,228 lines, 20 functions
  touching `so->table`.** `set_repr_lock_held:776-785` — where `PyList_New(so->used)` precedes a
  `set_next` walk that could overrun or under-fill the list — is **clean**, because `PyList_New`
  provably cannot run Python: `gc_alloc` → `_PyObject_GC_Link` only calls `_Py_ScheduleGC`, which
  sets an eval-breaker bit rather than collecting inline. I checked this specifically because it is
  the shape that would have been a heap overflow (grow) or a `Py_TYPE(NULL)` in `list_repr`
  (shrink). Reported as a bounded negative, not silence.

- **Scanner rule `deref_before_check`: 0 out of 403 functions.** Evidential, not structural — the
  rule ran over the whole slice. I found no instance by reading either.

- **Scanner rule `decref_of_nulled_outparam`: 0, structural.** `summary.decref_of_nulled_outparam_call_sites`
  is 8; none of those 8 is a `PyObject**`-out-param API. `_PyTuple_Resize` is not called from either
  file. Do not report this zero as a clean result.

---

## 4. Toolkit feedback

### Recall gaps — the highest-value output

**Gap A (largest): `assert()` is the toolkit's blind spot in exactly one direction.**
`scan_null_checks` already models `deref_is_assert` and its `detail` string is *excellent* — it is
what pointed me at the shape. But the rule only fires when the asserted value came from a
**resolved fallible allocation source**. Four of my six findings are asserts on values from
`lookdict_index` (returns `DKIX_EMPTY`), from a search loop, and from a re-entrancy-invalidated
index — none of which is an "allocation". Concrete proposal:

> Add a finding type `assert_only_bound`: an `assert(X)` whose predicate is the *only* guard on a
> value subsequently used as an array index or dereferenced, where the value's producer is any
> function in the file that has a `return <negative constant>` / `return DKIX_*` path. On this slice
> that fires on `:2963`, `:2944`, `:5075` and `:2967` — 4 net-new FIX findings — and its denominator
> is 110 (deref-bearing asserts), not 265.

**Gap B: no rule models "value captured before a callback, used after".** All six findings and the
already-recorded CPY-0079/0096 share one abstract shape: *a predicate or index established before a
call that can run arbitrary Python, and consumed after it.* The re-entrancy vehicle is a closed,
enumerable set in CPython — `_PyDict_NotifyEvent`, `_PyType_Modified_Unlocked`,
`PyErr_FormatUnraisable`, `PyObject_RichCompareBool`, `PyObject_Hash`, `Py_DECREF`. Proposal:

> A `stale_guard_across_python_call` rule: for each `if (P) { … CALL … USE }` where `P` reads a
> struct field, `CALL` is in the Python-reaching set, and `USE` dereferences the same field or an
> index derived from it, emit a finding. On this slice: 13 of the 14 `_PyDict_NotifyEvent` call
> sites match (only `dict_dealloc:3652` is protected), plus 3 of the 4 `insert_split_key` sites.
> That single rule recovers findings 1, 2, 4, 5 and 6.

**Gap C: nothing models "a bound check exists on the sibling but not here".** Finding 3 —
the highest-severity one, pure-Python-reachable and present in released 3.12/3.14 — has no
allocation, no assert, and no callback. It is visible *only* by comparing the reverse iterator
against its three forward siblings. Proposal:

> A `missing_sibling_bound` heuristic: for functions in the same file with near-identical bodies
> (same struct fields, same `DK_*_ENTRIES` access), diff the set of comparison operators applied to
> the index variable. `dictreviter_iter_lock_held` tests only `i < 0`; the three forward twins test
> `i < 0` **and** `i >= n`. That asymmetry is mechanically detectable and is the whole finding.

### Precision per rule (this slice)

| rule | fired | true positive | note |
|---|---|---|---|
| `unchecked_alloc` | 1 | 1 (CPY-0079, already recorded) | 100% precision, 0 net-new recall |
| `deref_before_check` | 0 | — | evidential zero over 403 functions |
| `decref_of_nulled_outparam` | 0 | — | structural zero, denominator 8 |

Precision is fine. **Recall is the problem: 1 candidate surfaced, 6 FIX findings exist.**

### Other tuning notes

- The envelope's `denominators` block did its job — I could state "evidential" vs "structural" for
  both zeros without guessing. Keep it.
- `fallible_sources_resolved: 214` against `assignment_sites: 1333` means the discovery pass
  resolves ~16% of assignment sources in this slice. `lookdict_index` (a static function with a
  literal `return DKIX_EMPTY`) is *not* among them because its return type is `Py_ssize_t`, not a
  pointer. Widening the fallible-source discovery to integer-returning functions with a negative
  sentinel return would have surfaced findings 1 and 2 directly.

---

## 5. Noticed outside slice

- `Objects/typeobject.c:1222-1223` `_PyType_Modified_Unlocked` — the Python-running site that makes
  findings 4 and 5 reachable; already CPY-0084/CPY-0096, not re-reported.
- `Python/errors.c:1737` `format_unraisable_v` — `PyObject_CallOneArg` on `sys.unraisablehook` is
  the single choke point through which every finding above reaches user Python; a
  "re-entrancy-source" catalog entry for the toolkit rather than a bug.
- `Objects/listobject.c` `PyList_New` — confirmed *not* to run Python (`_Py_ScheduleGC` defers to
  the eval breaker); worth recording in the FP taxonomy, since "allocation can trigger GC which can
  run `__del__`" is stated as fact in several CPython comments (`dictobject.c:5013-5021`,
  `:3965-3968`) and is no longer true.
