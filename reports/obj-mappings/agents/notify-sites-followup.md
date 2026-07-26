# CPY-0117 follow-up: the remaining `_PyDict_NotifyEvent` stale-state sites

Scope: the eight lines the draft had not driven to a failure —
`:1997 :2003 :2060 :3083 :3307 :4234 :5066 :7510` — plus a confirmation pass on
the known-safe `:3652`.

Tree: `/home/danzin/projects/cpython` @ `4f3be1b5777`.
Builds: `~/projects/python_build_matrix/builds/<name>/python`.
ASan runs are the **GIL** build `release-gil-nojit-asan` with
`PYTHONMALLOC=malloc` and `ASAN_OPTIONS=detect_leaks=0`.

Mechanism is unchanged and not re-derived here: `_PyDict_SendEvent`
(`dictobject.c:8309-8317`) answers the callback's mandated `-1` with
`PyErr_FormatUnraisable`, which runs `sys.unraisablehook` — arbitrary Python —
inside the notify window. The callback is CPython's own
`dict_watch_callback_error` (`_testcapi.add_dict_watcher(1)`), which is
`PyErr_SetString(...); return -1;` and nothing else.

## Result table

| site | enclosing function | stale value held across the notify | verdict | evidence (N/M, build) |
|---|---|---|---|---|
| `:1997` | `_PyDict_InsertSplitValue` (ADDED) | `ix` (`insertdict:2028`); split-table-ness of `mp` | **crashed** | SIGSEGV at `dictobject.c:1998`, `mp->ma_values == NULL` — 5/5 `debug-gil-nojit`, 5/5 `release-gil-nojit`, 5/5 `debug-ft-nojit`, 5/5 `release-ft-nojit`; ASan SEGV at the same line 3/3 `release-gil-nojit-asan` |
| `:2003` | `_PyDict_InsertSplitValue` (MODIFIED) | `ix`; `old_value` (`:1995`, borrowed); split-table-ness of `mp` | **crashed** | SIGSEGV at `dictobject.c:2004` — 5/5 on all four GIL/FT debug+release builds, ASan SEGV at the same line 3/3 `release-gil-nojit-asan`; embedded-values variant: `clear_freelist` assert 5/5 `debug-ft-nojit`, ASan SEGV in `free_object`/`clear_freelist` 3/3 |
| `:2060` | `insertdict` (MODIFIED) | `ix` + `old_value` (`_Py_dict_lookup:2038`) | **crashed** | ASan heap-use-after-free READ at `insertdict:2076` — 3/3 `release-gil-nojit-asan`; ASan heap-buffer-overflow WRITE at `insertdict:2068` — 3/3 same build; `_Py_NegativeRefcount` 5/5 `debug-gil-nojit` and 5/5 `debug-ft-nojit` |
| `:3083` | `delitemif_lock_held` | `ix` + `old_value` (`:3070`), `hash` (`:3067`) | **crashed** | ASan SEGV — `Py_DECREF(NULL)` at `delitem_common:2990` via `delitemif_lock_held:3084` — 3/3 `release-gil-nojit-asan`; `assert(hashpos >= 0)` `delitem_common:2963` 5/5 `debug-gil-nojit` and 5/5 `debug-ft-nojit`; SIGSEGV 5/5 `release-ft-nojit`; `len()` returns NULL (ma_used < 0) 5/5 `release-gil-nojit` |
| `:3307` | `_PyDict_Pop_KnownHash` | `ix` + `old_value` (`:3291`) | **crashed** | ASan heap-use-after-free READ at `_PyDict_Pop_KnownHash:3308` — 3/3 `release-gil-nojit-asan`; SIGSEGV 5/5 `release-gil-nojit` and 5/5 `release-ft-nojit`; `assert(hashpos >= 0)` 5/5 `debug-gil-nojit` and 5/5 `debug-ft-nojit` |
| `:4234` | `dict_dict_merge` (CLONED) | every guard at `:4219`/`:4228-4232`; `okeys` (`:4225`) | **crashed (assert) + wrong result** | `assert(orig->ma_keys != Py_EMPTY_KEYS)` `clone_combined_dict_keys:1028` — 5/5 `debug-gil-nojit`, 5/5 `debug-ft-nojit`; silent loss of the hook's stores 5/5 on all four builds; **no memory-corruption crash in release, 0/3 ASan, ~9 attempts** |
| `:5066` | `dict_popitem_impl` (general keys) | `ep0` (`:5058`), `i` (`:5059-5063`) | **crashed** | ASan heap-use-after-free READ of size 8 **at the site itself**, `dict_popitem_impl:5067` — 3/3 `release-gil-nojit-asan`; `assert(j >= 0)` `dict_popitem_impl:5075` 5/5 `debug-gil-nojit` and 5/5 `debug-ft-nojit`; SIGSEGV in `dict_traverse:5104` during GC 5/5 `release-gil-nojit` and 5/5 `release-ft-nojit` |
| `:7510` | `store_instance_attr_lock_held` | `values` (`store_instance_attr_dict:7538`), `dict` (`:7446`), `ix` (`:7455`), `old_value` (`:7497`), the `dict->ma_values == values` identity | **crashed** | `assert(i < size)` `delete_index_from_values:2944` — 5/5 `debug-gil-nojit`, 5/5 `debug-ft-nojit` (`del`/`del_raw`); ASan heap-buffer-overflow WRITE in `_PyObject_GC_TRACK` 2/3 (`del_raw`), ASan heap-use-after-free READ in `dict_traverse` 3/3 (`detach`), ASan SEGV in `clear_freelist` 2/5 (`mod`), all `release-gil-nojit-asan`; SIGSEGV 4/5 `release-gil-nojit` (`del_raw`); `len()` = -1 and `list(d)` `ValueError` 5/5 `release-gil-nojit` (`del`) |
| `:3652` | `dict_dealloc` | *(none)* | **safe** | `values`/`keys` are read at `:3656-3657`, after the notify; `_PyObject_ResurrectStart`/`End` bracket makes a resurrecting hook return early at `:3653` |

Every one of the eight was driven to an observable failure. `:4234` is the only
one that did not reach memory corruption; its failure is an assertion violation
plus a silent wrong Python-visible result.

## Reproducers

All in `reports/obj-mappings/repro/`:

- `notify_site_1997_2003_insert_split_value.py [mod_clear|mod_detached|add_detached]`
- `notify_site_2060_insertdict_modified.py [clear|regrow|resize|clear_int|regrow_int|resize_int]`
- `notify_site_3083_delitemif.py [clear|regrow]`
- `notify_site_3307_pop_knownhash.py [clear|regrow]`
- `notify_site_4234_clone_combined.py [insert|clearsrc|growsrc]`
- `notify_site_5066_popitem_general.py [clear|regrow]`
- `notify_site_7510_store_instance_attr.py [mod|del|del_raw|detach]`
- `run_notify_sites.py <script> <args> -- <build...> [-n RUNS]` — matrix runner
- `asan_notify_one.sh <tag> <script> [args]` — single-case ASan runner
  (`RUNS=N bash ./asan_notify_one.sh <tag> ...`)

Raw ASan output is kept alongside as `asan_notify_<tag>.txt`:
`2060_clear`, `2060_regrow_int`, `3083`, `3307`, `4234_insert`,
`4234_clearsrc`, `5066`, `1997_add_detached`, `2003_mod_detached`,
`2003_mod_clear`, `7510_mod`, `7510_del`, `7510_del_v2`, `7510_del_raw`, `7510_detach`.

## Per-site detail

### `:1997` / `:2003` — `_PyDict_InsertSplitValue` (`dictobject.c:1989-2010`)

These two are **not** in `insertdict`; they are in `_PyDict_InsertSplitValue`,
which `insertdict:2030` calls and which is also a public `PyAPI_FUNC` exported
for external JIT support.

```c
1995:  PyObject *old_value = mp->ma_values->values[ix];   // borrowed, pre-notify
1996:  if (old_value == NULL) {
1997:      _PyDict_NotifyEvent(PyDict_EVENT_ADDED, mp, key, value);
1998:      STORE_SPLIT_VALUE(mp, ix, Py_NewRef(value));   // mp->ma_values->values[ix]
1999:      _PyDictValues_AddToInsertionOrder(mp->ma_values, ix);
2000:      STORE_USED(mp, mp->ma_used + 1);
2002:  else {
2003:      _PyDict_NotifyEvent(PyDict_EVENT_MODIFIED, mp, key, value);
2004:      STORE_SPLIT_VALUE(mp, ix, Py_NewRef(value));
2007:      Py_DECREF(old_value);
```

`ix` comes from `insert_split_key(mp->ma_keys, key, hash)` at `insertdict:2028`;
the branch decision comes from the pre-notify read at `:1995`. Neither is
re-validated, and neither is the *precondition* that `mp` still has a split
table — established by `_PyDict_HasSplitTable(mp)` at `insertdict:2027`.

Reaching a split table from Python: `d = obj.__dict__` on an instance of a
plain class is a split dict whose `ma_values` aliases the object's inline
values, and `d[key] = v` then routes through `insert_split_key` ->
`_PyDict_InsertSplitValue`.

The decisive hook is `d.clear()` **after the dict has been detached from its
object** (`del obj`, which runs `PyObject_ClearManagedDict` ->
`detach_dict_from_object:7970`, giving the dict a heap values array with
`embedded == 0`). `clear_lock_held` then takes its `:3154` branch —
`set_values(mp, NULL); set_keys(mp, Py_EMPTY_KEYS)` — so `mp->ma_values` is
NULL when control returns.

gdb at the fault (`release-gil-nojit`, `mod_detached`):

```
Program received signal SIGSEGV, Segmentation fault.
0x... in _PyDict_InsertSplitValue (mp=0x7ffff7434340, ..., ix=2)
    at Objects/dictobject.c:2004
2004        STORE_SPLIT_VALUE(mp, ix, Py_NewRef(value));
(gdb) p mp->ma_values   $1 = (PyDictValues *) 0x0
(gdb) p mp->ma_keys     $2 = (PyDictKeysObject *) 0x... <empty_keys_struct>
(gdb) p mp->ma_used     $3 = 0
(gdb) p ix              $4 = 2
```

`add_detached` gives the identical fault at `:1998` (the ADDED branch).

Both are 5/5 SIGSEGV on `debug-gil-nojit`, `release-gil-nojit`,
`debug-ft-nojit` and `release-ft-nojit`, and 3/3 ASan SEGV on
`release-gil-nojit-asan` naming `_PyDict_InsertSplitValue` `:2004` / `:1998`
directly.

The simpler embedded-values variant (`mod_clear`, object still alive, hook just
does `d.clear()`) exercises the *other* consequence — `clear_embedded_values`
DECREFs `old_value` to zero, and `:2007` DECREFs it again. That one is:

- 5/5 `debug-ft-nojit`:
  `Objects/object.c:909: clear_freelist: Assertion 'freelist->size == 0 || freelist->size == -1' failed.`
- 3/3 `release-gil-nojit-asan`: SEGV in `free_object` (`Objects/object.c:921`)
  from `clear_freelist` / `_PyObject_ClearFreeLists` — the doubly-freed list
  sitting on the list freelist twice.
- flaky on the plain GIL builds (3/5 release, 2/5 debug, 0/6 under gdb).

### `:2060` — `insertdict`, MODIFIED branch

```c
2038:  ix = _Py_dict_lookup(mp, key, hash, &old_value);   // both stale
2060:  _PyDict_NotifyEvent(PyDict_EVENT_MODIFIED, mp, key, value);
2067:  PyDictUnicodeEntry *ep = &DK_UNICODE_ENTRIES(mp->ma_keys)[ix];
2068:  STORE_VALUE(ep, value);      // stale ix into a re-read ma_keys
2076:  Py_XDECREF(old_value);       // stale borrowed ref
```

**Why the previous attempt failed 0/5 + 0/5 + 0/3.** Two separate reasons, one
per consequence.

1. `Py_XDECREF(old_value)` at `:2076`. The previous reproducer stored small
   ints (`d["k%d" % i] = i` for `i < 257`), which are **immortal**, so the
   stale DECREF was a no-op. Storing heap objects with refcount 1 instead makes
   it a deterministic use-after-free:
   - `debug-gil-nojit` 5/5 and `debug-ft-nojit` 5/5:
     `Include/refcount.h:520: _Py_NegativeRefcount: Assertion failed: object has
     negative ref count / <object at 0x... is freed>`
   - `release-gil-nojit-asan` 3/3:
     ```
     ERROR: AddressSanitizer: heap-use-after-free
     READ of size 4 at ...
         #0 _Py_IsImmortal Include/refcount.h:132
         #1 Py_DECREF     Include/refcount.h:421
         #2 Py_XDECREF    Include/refcount.h:520
         #3 insertdict    Objects/dictobject.c:2076
     freed by thread T0 here:
         #4 dictkeys_decref     Objects/dictobject.c:510
         #5 clear_lock_held     Objects/dictobject.c
         #6 PyDict_Clear        Objects/dictobject.c:3178
         ...
         #17 format_unraisable_v Python/errors.c:1737
     ```
     The `format_unraisable_v:1737` frame is the notify window itself.
   - `release-gil-nojit` plain: exit 0, silent corruption 5/5.

2. The stale-`ix` WRITE at `:2068`. The earlier diagnosis was correct — after a
   bare `d.clear()` the write lands inside the static `empty_keys_struct`, which
   ASan does not instrument. Making the hook `d.clear()` **and then re-insert a
   few keys** puts a fresh heap keys object in `ma_keys`, and the same stale
   `ix` write goes off the end of it. With immortal values (so `:2076` stays a
   no-op and the run isolates the write), `release-gil-nojit-asan` gives 3/3:

   ```
   ERROR: AddressSanitizer: heap-buffer-overflow
   WRITE of size 8 at 0x6e953b9ff520
       #0 insertdict Objects/dictobject.c:2068
   0x6e953b9ff520 is located 2146 bytes after 126-byte region
   ```

   Mode `regrow_int` in the reproducer.

Negative controls kept in the reproducer, so the two failure reasons above are
not confused with a general "any hook crashes it" claim:

- `clear_int` (immortal values **and** a bare `d.clear()`): 3/3 exit 0 on
  `debug-gil-nojit` — both consequences neutralised at once. This is the exact
  configuration the previous 0/5 + 0/5 + 0/3 attempt was in.
- `resize` (hook grows the dict rather than clearing it): 3/3 exit 0.
  `dictresize` migrates the entries and keeps every value reference alive, so
  the stale `ix` lands on a valid-but-wrong slot and `old_value` is still live.
  A resize hook is *not* sufficient here; the hook has to free something.

### `:3083` — `delitemif_lock_held`

`ix` and `old_value` come from `_Py_dict_lookup` at `:3070` and `hash` from
`PyObject_Hash` at `:3067`; all three are handed to `delitem_common` at `:3084`
after the notify.

Python-reachable through `_weakref._remove_dead_weakref(dct, key)`
(`Modules/_weakref.c:59`), the only caller of `_PyDict_DelItemIf`. That entry
point carries an explicit atomicity promise at `dictobject.c:3090-3094`:

> This function promises that the predicate -> deletion sequence is atomic […]
> assuming the predicate itself doesn't release the GIL (or cause re-entrancy
> which would release the per-dict mutex)

The predicate here (`is_dead_weakref`) does neither. The notify does.

- `release-gil-nojit-asan` 3/3, `Py_DECREF` of a NULL `old_key` read out of the
  emptied keys table:
  ```
  ERROR: AddressSanitizer: SEGV on unknown address 0x000000000000
      #0 _Py_IsImmortal                    Include/refcount.h:132
      #1 Py_DECREF                         Include/refcount.h:421
      #2 delitem_common                    Objects/dictobject.c:2990
      #3 delitemif_lock_held               Objects/dictobject.c:3084
      #4 _PyDict_DelItemIf                 Objects/dictobject.c:3103
      #5 _weakref__remove_dead_weakref_impl Modules/_weakref.c:59
  ```
- `debug-gil-nojit` 5/5 and `debug-ft-nojit` 5/5:
  `Objects/dictobject.c:2963: delitem_common: Assertion 'hashpos >= 0' failed.`
- `release-ft-nojit` 5/5 SIGSEGV.
- `release-gil-nojit` 5/5
  `SystemError: <built-in function len> returned NULL without setting an exception`
  — `dict_length` returned a negative `ma_used`.

### `:3307` — `_PyDict_Pop_KnownHash` (plain `dict.pop()`)

```c
3291:  Py_ssize_t ix = _Py_dict_lookup(mp, key, hash, &old_value);
3307:  _PyDict_NotifyEvent(PyDict_EVENT_DELETED, mp, key, NULL);
3308:  delitem_common(mp, hash, ix, Py_NewRef(old_value));
3312:  *result = old_value;      // handed back to the Python caller
```

Directly reachable: `dict.pop(k)` -> `pop_lock_held:3353` ->
`_PyDict_Pop_KnownHash`. Also reached from `odictobject.c:1096` and
`_functoolsmodule.c:1523`.

- `release-gil-nojit-asan` 3/3:
  ```
  ERROR: AddressSanitizer: heap-use-after-free
  READ of size 4 at ...
      #0 Py_INCREF            Include/refcount.h:286
      #1 _Py_NewRef           Include/refcount.h:536
      #2 _PyDict_Pop_KnownHash Objects/dictobject.c:3308
      #3 dict_pop_default     Objects/dictobject.c:3389
      #4 dict_pop_impl        Objects/dictobject.c:4991
  ```
  i.e. `Py_NewRef` resurrecting an object the hook already freed.
- `release-gil-nojit` 5/5 and `release-ft-nojit` 5/5 SIGSEGV; gdb:
  ```
  #0 _PyObject_GC_UNTRACK   Include/internal/pycore_gc.h:264
  #1 PyObject_GC_UnTrack    Python/gc.c:1954
  #2 list_dealloc           Objects/listobject.c:559
  #3 _Py_Dealloc            Objects/object.c:3319
  #4 _PyDict_Pop_KnownHash  Objects/dictobject.c:3308
  ```
- `debug-gil-nojit` 5/5 and `debug-ft-nojit` 5/5
  `assert(hashpos >= 0)` at `delitem_common:2963`.

Note the third consequence, which needs no crash to be alarming: `*result` at
`:3312` is the pointer the dict used to own, and `dict.pop()` hands it straight
back to Python. When the hook has emptied the dict, `pop()` returns a freed
object.

### `:4234` — `dict_dict_merge`, the CLONED fast path

Unlike the other sites, the stale thing here is not an index or a pointer but
the **entire guard set**:

```c
4219:  if (mp->ma_used == 0) {
4225:      PyDictKeysObject *okeys = other->ma_keys;
4228:      if (mp->ma_values == NULL &&
               other->ma_values == NULL &&
               other->ma_used == okeys->dk_nentries && ...
4234:          _PyDict_NotifyEvent(PyDict_EVENT_CLONED, mp, other, NULL);
4235:          PyDictKeysObject *keys = clone_combined_dict_keys(other);
4240:          dictkeys_decref(mp->ma_keys, IS_DICT_SHARED(mp));
4241:          set_keys(mp, keys);
4242:          STORE_USED(mp, other->ma_used);
```

Nothing between `:4234` and `:4242` re-checks any of it.

**Wrong Python-visible result (5/5 on all four builds).** `d.update(src)` with a
watcher on `d`; the hook writes two keys into `d`. `:4240-4242` then replaces
`d`'s keys wholesale and overwrites `ma_used` with `other->ma_used`, so both
stores vanish with no error:

```
[main] len(d)=3
[main] d={'a': 1, 'b': 2, 'c': 3}
[main] *** LOST: the hook's stores vanished from d ***
```

**Assertion violation (5/5 `debug-gil-nojit`, 5/5 `debug-ft-nojit`).** The hook
clears `src` instead. `clone_combined_dict_keys` then runs against
`Py_EMPTY_KEYS` and trips its own precondition:

```
Objects/dictobject.c:1028: clone_combined_dict_keys:
    Assertion `orig->ma_keys != Py_EMPTY_KEYS' failed.
```

In a release build that path silently `memcpy`s `empty_keys_struct`, whose
`dk_refcnt` is `_Py_DICT_IMMORTAL_INITIAL_REFCNT` (`dictobject.c:646`). `d`
therefore ends up owning a **heap** keys object carrying an immortal refcount:
it can never be freed (permanent leak), and `clear_lock_held`'s
`assert(oldkeys->dk_refcnt == 1)` at `:3148` no longer holds for it.
`clone_combined_dict_keys`'s other two preconditions —
`assert(orig->ma_values == NULL)` at `:1027` and `assert(orig->ma_keys->dk_refcnt == 1)`
at `:1029` — are equally unprotected, but I could not find a pure-Python way to
falsify either during the window.

**Honest limit.** I did not drive `:4234` to memory corruption. Attempts:
`insert`, `clearsrc`, `growsrc`, insert-then-clear, oversized inserts forcing
`mp` to resize, and post-hoc stress of the cloned-immortal-keys dict — ~9
variants, 0/3 ASan reports on `release-gil-nojit-asan` for `insert` and
`clearsrc`. The blocker is that `mp` is a plain dict, so the hook cannot make
`mp->ma_values` non-NULL, and `other` cannot be turned back into a split table;
the two guards whose falsification would give a type-confused dict are
therefore out of reach from Python. **Verdict: holds stale state, driven to an
assertion violation and a wrong result, not to memory corruption.**

### `:5066` — `dict_popitem_impl`, the general (non-unicode) keys branch

Exact twin of the already-reproduced `:5051` unicode branch, on the
`DICT_KEYS_GENERAL` path:

```c
5058:  PyDictKeyEntry *ep0 = DK_ENTRIES(self->ma_keys);   // raw entry pointer
5059:  i = self->ma_keys->dk_nentries - 1;
5065:  key = ep0[i].me_key;
5066:  _PyDict_NotifyEvent(PyDict_EVENT_DELETED, self, key, NULL);
5067:  hash  = ep0[i].me_hash;      // READ through a possibly-freed ep0
5068:  value = ep0[i].me_value;     // READ
5069:  STORE_KEY(&ep0[i], NULL);    // WRITE
5070:  STORE_HASH(&ep0[i], -1);     // WRITE
5071:  STORE_VALUE(&ep0[i], NULL);  // WRITE
5074:  j = lookdict_index(self->ma_keys, hash, i);
5082:  STORE_KEYS_NENTRIES(self->ma_keys, i);
```

Reached by putting one non-`str` key in the dict, which forces a general keys
table for the whole dict.

- `release-gil-nojit-asan` 3/3, naming the site itself — the read at `:5067`
  goes through the freed `ep0`:
  ```
  ERROR: AddressSanitizer: heap-use-after-free
  READ of size 8 at 0x7a204bc51fc8
      #0 dict_popitem_impl Objects/dictobject.c:5067
      #1 dict_popitem      Objects/clinic/dictobject.c.h:231
  ```
- `debug-gil-nojit` 5/5 and `debug-ft-nojit` 5/5:
  `Objects/dictobject.c:5075: dict_popitem_impl: Assertion 'j >= 0' failed.`
- `release-gil-nojit` 5/5 and `release-ft-nojit` 5/5 SIGSEGV, deferred into the
  next GC because the entry writes leave a wild pointer in the table:
  ```
  #0 _PyObject_IS_GC   Include/internal/pycore_object.h:829
  #1 visit_decref      Python/gc.c:445
  #2 dict_traverse     Objects/dictobject.c:5104
  #3 subtract_refs     Python/gc.c:497
  #4 deduce_unreachable Python/gc.c:1177
  #5 gc_collect_main   Python/gc.c:1509
  ```
  (the visited "object" is `&dict___contains__`, a function symbol — i.e. the
  dict's entry array now holds a non-object pointer).

### `:7510` — `store_instance_attr_lock_held` (the instance-`__dict__` path)

The largest stale-state set of the eight:

| value | latched at | used at |
|---|---|---|
| `values` (`_PyObject_InlineValues(obj)`) | `store_instance_attr_dict:7538`, before the critical section | `:7513`, `:7516`, `:7524` |
| `dict` | `:7446` | `:7518-7519`, `:7526-7527` |
| `ix` | `insert_split_key`, `:7455` | `:7513`, `:7516`, `:7524` |
| `old_value` | `:7497` | `:7530` |
| the identity `dict->ma_values == values` | checked by the caller at `:7541` | *asserted* at `:7518` / `:7526`, after the window |

This is the inline-values path and does reach `delete_index_from_values`, so it
is adjacent to CPY-0128 — but the two are distinct. CPY-0128 is
`_PyObject_InitInlineValues` leaving the insertion-order array *uninitialised*;
here the array was correctly initialised and the hook resets `values->size` to
0 behind the caller's back.

**`del` mode — `values->size` underflow (the sharpest result).** `del o.c` fires
DELETED at `:7510`; the hook does `d.clear()`, whose `clear_embedded_values`
(`:3109-3121`) sets `values->size = 0`. Control returns to `:7524`:

```c
2939:  uint8_t *array = get_insertion_order_array(values);
2940:  int size = values->size;                       // now 0
2943:  for (i = 0; array[i] != ix; i++) {
2944:      assert(i < size);
2947:  size--;                                        // -1
2951:  values->size = size;                           // uint8_t: 255
```

- `debug-gil-nojit` 5/5 and `debug-ft-nojit` 5/5:
  `Objects/dictobject.c:2944: delete_index_from_values: Assertion 'i < size' failed.`
- `release-gil-nojit` — `:7527` then drives `dict->ma_used` to -1, which is
  Python-visible on a perfectly ordinary live object:
  ```
  [main] *** len(d) raised <built-in function len> returned NULL without
         setting an exception -> ma_used < 0 ***
  [main] list(d) raised ValueError('length must be positive')
  [main] vars(o) raised ValueError('length must be positive')
  ```
  5/5. (Without those guards the SystemError propagates and the double-freed
  `old_value` at `:7530` bites instead: SIGSEGV 3/5.)
- `del_raw` mode — same deletion, then allocator/GC churn instead of the
  invariant probes, which is what the double-freed `old_value` corrupts:
  SIGSEGV 4/5 `release-gil-nojit`; `release-gil-nojit-asan` 2/3:
  ```
  ERROR: AddressSanitizer: heap-buffer-overflow
  WRITE of size 8 at ...
      #0 _PyGCHead_SET_NEXT  Include/internal/pycore_gc.h:151
      #1 _PyObject_GC_TRACK  Include/internal/pycore_gc.h:231
      #2 PyList_New          Objects/listobject.c:277
  ```
  — a later allocation walking a GC list corrupted by the double free.

**`mod` mode — stale `old_value`.** `o.c = ["replacement"]` fires MODIFIED; the
hook clears the dict, `clear_embedded_values` DECREFs `old_value` to 0, and
`:7530` DECREFs it again.

- `debug-ft-nojit` 5/5:
  `Objects/object.c:909: clear_freelist: Assertion 'freelist->size == 0 || freelist->size == -1' failed.`
- `release-gil-nojit` SIGSEGV 5/12; `release-gil-nojit-asan` 2/5
  `SEGV` inside `free_object` / `clear_freelist` / `_PyObject_ClearFreeLists`.
- `debug-gil-nojit` 5/5 exit 0 (the debug allocator's `0xDD` fill makes the
  second DECREF read a huge positive refcount, so no negative-refcount assert).

**`detach` mode — the `dict->ma_values == values` identity.** The hook does
`o.__dict__ = {"x": 1}`, which runs `detach_dict_from_object` and
`invalidate_and_clear_inline_values`. `:7513` then stores into an array the
dict no longer points at, and `:7530` DECREFs an already-freed `old_value`.

- `debug-gil-nojit` 5/5:
  `Python/gc.c:443: visit_decref: Assertion "!_PyObject_IsFreed(op)" failed`
  (`object type name: dict`).
- `debug-ft-nojit` 5/5:
  `validate_gc_objects: Assertion "gc_get_refs(op) >= 0" failed: refcount is too small`.
- `release-gil-nojit-asan` 3/3 heap-use-after-free READ of size 8 in
  `_PyObject_IS_GC` / `visit_decref` / `dict_traverse:5104`.
- `release-gil-nojit` 5/5 exit 0 (silent).

### `:3652` — `dict_dealloc`, confirmed SAFE

```c
3650:  PyDictObject *mp = (PyDictObject *)self;
3651:  _PyObject_ResurrectStart(self);
3652:  _PyDict_NotifyEvent(PyDict_EVENT_DEALLOCATED, mp, NULL, NULL);
3653:  if (_PyObject_ResurrectEnd(self)) {
3654:      return;
3655:  }
3656:  PyDictValues *values = mp->ma_values;
3657:  PyDictKeysObject *keys = mp->ma_keys;
```

Nothing is captured before the notify. `values` and `keys` are read at
`:3656-3657`, *after* it, and every subsequent use (`:3662-3674`) goes through
those post-notify reads. The `ResurrectStart`/`ResurrectEnd` bracket handles the
one remaining hazard — a hook that stores the dict somewhere — by returning
early at `:3653`. Confirms the draft's reading: the transferable property is the
**ordering**, not the bracket.

## What this changes in the draft

The draft's "…plus four more that I have not driven to a crash" can be
replaced. All fourteen sites are now accounted for:

- **13 hold stale state across the notify window**, all thirteen driven to an
  observable failure (12 to memory corruption or an assertion violation on a
  memory invariant; `:4234` to an assertion violation plus silent data loss).
- **1 is safe** (`:3652`), and it is safe because it reads its state *after*
  the notify.

Two additions worth carrying into the issue text:

1. **`:3083` breaks an explicitly documented atomicity promise.** The comment at
   `dictobject.c:3090-3094` tells callers the predicate -> deletion sequence is
   atomic provided *the predicate* does not re-enter. The predicate doesn't; the
   notify does. That is a stronger statement than "a stale index".
2. **`:3307` returns freed memory to Python.** `dict.pop()` is the plainest
   possible call, and `*result` at `:3312` is the dict's own reference handed
   straight back to the caller after the hook has dropped it.
