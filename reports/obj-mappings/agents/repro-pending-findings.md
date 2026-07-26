# Reproduction pass — four pending records from the obj-mappings slice

Target ref `4f3be1b5777`. Every claim below carries **N/M runs and a build name**;
where a claim is a negative it carries the denominator that bounds it.

Builds used: `~/projects/python_build_matrix/builds/<name>/python` —
`debug-ft-nojit`, `release-ft-nojit`, `release-ft-nojit-tsan`,
`release-ft-nojit-asan-mitrack`, `debug-gil-nojit`, `release-gil-nojit`,
`release-gil-nojit-asan`.

## Verdicts

| record | before | after | one line |
|---|---|---|---|
| **CPY-0127** `set_clear_internal` as raw `tp_clear` | static-confirmed | **reproduced** | SIGSEGV/SIGABRT on both FT builds, two distinct crash faces, GIL clean; falsifies the premise written into merged PR gh-130126 |
| **CPY-0115** `_PyType_GetSubclasses` cursor | lead | **reproduced — and NOT a duplicate of CPY-0117** | SIGSEGV 6/8 `debug-ft-nojit`, GIL control clean 0/8 ×2; the crash face is an in-slice `dictobject.c` defect (`_PyDict_Next` double-loads `me_value`) |
| **CPY-0130** plain `ma_values` stores | lead | **static-confirmed (TSan) + reachability answered YES for both sites** | 14 TSan warnings, 5 summaries naming `:7976`; `:8019` reached under a 1-index OOM sweep |
| **CPY-0129** `dictiter_len` torn/UAF read | static-confirmed | **static-confirmed (unchanged) — but the record's stated cause is wrong** | 0 events in **5,759,584** exhaustion drops incl. a 240 s oversubscribed run on `release-ft-nojit-asan-mitrack`; "QSBR delays the free" is false for `ma_used`, it is biased refcounting, and `:6158` clearing the field *before* `:6159` frees it makes the reader-side crash structurally near-impossible |

Matrix outputs: `repro/matrix_CPY-0127_gcobjects.txt` (GIL controls; the
`release-ft-nojit` row appends there when that run finishes) and
`repro/matrix_CPY-0127_gcobjects_debugft.txt`.

Reproducers: `reports/obj-mappings/repro/CPY-0127_gc_tp_clear_vs_mutator.py`,
`CPY-0115_getsubclasses_borrowed_dict.py`, `CPY-0130_ma_values_plain_store.py`,
`CPY-0130_site8019_reachability.py`, `CPY-0129_dictiter_len_uaf.py`,
`run_repro_matrix.py`. gdb output in `repro/gdb_CPY-0127_debug-ft.txt`,
`repro/gdb_CPY-0115_debug-ft.txt`; TSan raw in `repro/tsan2/`; ASan raw in
`repro/asan_CPY-0129_*.txt`.

---

## 1. CPY-0127 — `set_clear_internal` registered raw as `tp_clear`: **REPRODUCED**

**Sites:** `Objects/setobject.c:2916` (`PySet_Type.tp_clear`), `:3008`
(`PyFrozenSet_Type.tp_clear`), body at `:645-694`.

### What was missing was reachability, not the race

The static half was already solid: `delete_garbage` dispatches `tp_clear` at
`Python/gc_free_threading.c:1761`, and `gc_collect_internal` calls
`delete_garbage` at `:2176` — **fifteen lines after `_PyEval_StartTheWorld` at
`:2161`**. The world is running. `set_clear_internal` takes no critical section
while all twelve other routes into it do.

The 8/8-clean result came from not being able to get a second thread to hold a
set the GC had already decided was garbage. Two facts make that constructible,
and both are in `delete_garbage` itself:

```c
/* Python/gc_free_threading.c:1738-1769 */
while ((op = worklist_pop(&state->unreachable)) != NULL) {
    _PyObject_ASSERT(op, gc_is_unreachable(op));
    gc_clear_unreachable(op);              /* :1742  the UNREACHABLE bit is cleared */
    ...
        inquiry clear = Py_TYPE(op)->tp_clear;
        if (clear != NULL) {
            (void) clear(op);              /* :1761  ...BEFORE tp_clear runs */
```

**(a)** For the whole duration of `tp_clear` the object is no longer flagged
unreachable, so `gc.get_objects()` — whose free-threaded implementation
*deliberately* filters in-progress-GC objects at
`gc_free_threading.c:2423-2427` with the comment *"Exclude unreachable objects
(in-progress GC)"* — hands it straight back to another thread. That filter has a
hole exactly the width of the tp_clear window.

**(b)** Python still runs inside `delete_garbage`. Everything in the unreachable
set was already finalized at `:2149`, so its `__del__` cannot fire again — but an
object **created by a finalizer** was never in the unreachable set, was never
finalized, and its `__del__` therefore runs from inside `delete_garbage`.
`subtype_clear` (`Objects/typeobject.c:2680-2714`) clears the instance dict
**before** calling the base `tp_clear`, so parking such an object in the instance
dict of a garbage `set` subclass stops the collector immediately *before*
`set_clear_internal` reads `so->table / so->fill / so->used / so->mask`.

The reproducer stages exactly that, and offers two acquisition modes:
`ctypes` (a finalizer publishes `id(target)`) and **`gcobjects` (pure Python, no
ctypes, no C API)**.

### Results

| build | mode | result |
|---|---|---|
| `debug-ft-nojit` | ctypes | **8/8 nonzero** — 6× SIGSEGV, 2× SIGABRT (60 rounds, 4 hammers) |
| `release-ft-nojit` | ctypes | **8/8 SIGSEGV** |
| `debug-ft-nojit` | **gcobjects** | **5/8 nonzero** — 4× SIGSEGV, 1× SIGABRT (150 rounds, 4 hammers) |
| `release-ft-nojit` | **gcobjects** | **2/6 SIGSEGV, 2 clean, 2 indeterminate** — `codes=[-11, TIMEOUT, TIMEOUT, 0, 0, -11]` (60 rounds, 4 hammers). See the TIMEOUT note below; the crash count claimed is the strict one, 2/6. |
| `debug-gil-nojit` | **gcobjects (the control)** | **0/6 clean** (60 rounds, 4 hammers) |
| `release-gil-nojit` | **gcobjects (the control)** | **0/6 clean** |
| `release-ft-nojit-asan-mitrack` | ctypes | 6/6 ASan ABORT — **inadmissible for the same reason as the other ctypes rows**; not used as evidence |

**Honest correction, stated because it changes what counts as evidence.** The
`ctypes` mode is *not* a clean control: `debug-gil-nojit` came back **2/6** and
`release-gil-nojit-asan` **2/6** in that mode. That is the reproducer's own
artifact — a hammer can read `ADDR[0]` from the previous round after the object
has died and `ctypes.cast` a dangling address. `gc.get_objects()` cannot dangle
(it increfs under stop-the-world), so **only the `gcobjects` rows are admissible
as evidence**. In that mode the contrast is clean and one-sided:
**`debug-ft-nojit` 5/8 and `release-ft-nojit` 2/6 crashed; `debug-gil-nojit` 0/6
and `release-gil-nojit` 0/6 did not.**

**The two TIMEOUTs in the `release-ft-nojit` row are not claimed as hangs.** The
brief's rule applies — a TIMEOUT on this machine has twice turned out to be CPU
contention, and four long-running fuzzer processes were resident for the whole
pass while `gcobjects` mode pays a stop-the-world per `gc.get_objects()` call. I
re-ran that configuration standalone rather than reporting the timeout at face
value; the result is in `repro/standalone_CPY-0127_releaseft.txt`. The row is
reported at its strict crash count of **2/6**, with the two timeouts counted as
indeterminate rather than as either crashes or clean runs.

Standalone run 1, on an otherwise-quiet machine, **SIGSEGV in 2.33 s wall**:

```
=== standalone run 1 ===
rounds=60 hammers=4 nelem=3000 mode=gcobjects gil=False
round 0 staged=1 acquired=4
Command terminated by signal 11
  wall=2.33 s
  exit=139
```

So when this configuration crashes, it crashes almost immediately — a 240 s
timeout is not "the crash path took a while".

**What that does and does not establish.** It rules out the benign reading of the
TIMEOUTs (a slow crash). It does **not** by itself prove the timed-out runs were
CPU starvation rather than a genuine stall, and I am not claiming it does.
Standalone run 2 was still executing at 4 min wall when this report was
finalized, which is consistent with the mode simply being slow — four hammer
threads each calling `gc.get_objects()` (one stop-the-world per call) against a
collecting thread — but 4 min is short of the 600 s cap, so "a clean run exceeds
the cap" is **not** a measurement I have. Runs 2-3 append to
`repro/standalone_CPY-0127_releaseft.txt` as they finish; whoever picks this up
should read that file before drawing a conclusion about the two TIMEOUTs.

Either way the row is reported at 2/6, which is the count that does not depend on
resolving them.

### Two crash faces, both from gdb (`repro/gdb_CPY-0127_debug-ft.txt`)

**Face 1 — double free of the set's entry table, with the lock-takers queued behind:**

```
#1  _PyMem_DebugCheckAddress ... "The 7 pad bytes at p-7 are not all FORBIDDENBYTE (0xfd): 0xdd *** OUCH"
#3  _PyMem_DebugFree
#4  free_entries (entries=0x2000424e010, size=2048, use_qsbr=...) Objects/setobject.c:475
#5  set_clear_internal (self=0x200026b0e20)                       Objects/setobject.c:692
#6  delete_garbage (state=0x7fffffffb920)                  Python/gc_free_threading.c:1761
#7  gc_collect_internal (...)                              Python/gc_free_threading.c:2176
```

and, in the same dump, threads 2/3/4:

```
#12 _PyCriticalSection_BeginSlow (tstate=..., c=..., m=0x200026b0e2a) Python/critical_section.c:59
```

`0x200026b0e2a` is `0x200026b0e20 + 0xa` — the `ob_mutex` of *the very set the
collector is clearing*. Three threads are queued on the lock; the collector did
not take it. That single dump is the whole finding.

**Face 2 — the thread that DID take the lock is the one that segfaults:**

```
Thread 2 "hammer" received signal SIGSEGV
#0  set_add_entry_takeref (so=0x200028302a0, ...)  Objects/setobject.c:275
        if (entry->hash == 0 && entry->key == NULL)
#1  set_add_key                                    Objects/setobject.c:608
#2  set_update_iterable_lock_held                  Objects/setobject.c:1233
#3  set_update_internal                            Objects/setobject.c:1313
#4  set_update_impl / set_update                   Objects/clinic/setobject.c.h:55   <-- clinic critical section
```

`set_empty_to_minsize` (`setobject.c:636`) stores `so->table = NULL` and restores
it at `:642`. Under the normal discipline nobody can observe that window because
every caller holds the critical section; the `tp_clear` caller does not, so a
correctly-locked `set.update()` on another thread dereferences NULL.

### Prior art — this contradicts a merged, explicit decision

`gh-130313` / **merged** PR `gh-130126` ("Avoid locking when clearing objects")
says, in the PR body:

> "This requires us to relax an assert because we clear objects **after we've
> restarted the world**. But we're past the point of resurrection so **no one
> else can be referring to this object**."

CPython already knows the world is running. The load-bearing claim is the second
sentence, and (a)+(b) above are the two ways it fails. Nothing in the tracker
covers it: `gh api search/issues` on `set tp_clear critical section free-threading`,
`delete_garbage tp_clear race`, `gc tp_clear data race free threading` returns
only gh-130313/gh-130126 and unrelated closed issues.

### Second finding falling out of the same mechanism (worth its own record)

**`gc.get_objects()`'s in-progress-GC filter does not cover the `tp_clear`
window.** `visit_get_objects` (`gc_free_threading.c:2416-2434`) excludes
`_PyGC_BITS_UNREACHABLE` specifically to keep mid-collection objects out of a
documented public API; `delete_garbage:1742` clears that bit before `:1761`
dispatches `tp_clear`, so every object is briefly visible again *while it is
being torn down*. This is what makes the pure-Python mode of the reproducer work
at all, and it is not specific to sets.

### Verdict

`status: reproduced`. Classification **FIX**. Fix is the guarded twin already
named in the record: wrap like `dict_tp_clear` → `PyDict_Clear` →
`Py_BEGIN_CRITICAL_SECTION`, i.e. add a `set_tp_clear` shim that takes the
object's critical section and calls `set_clear_internal`.

---

## 2. CPY-0115 — `_PyType_GetSubclasses` cursor: **REPRODUCED, and DISTINCT**

### Is it a duplicate of CPY-0117? **No.**

| | CPY-0115 | CPY-0117 (`_PyDict_DelItem_KnownHash_LockHeld:3038`) |
|---|---|---|
| threads | two | one |
| trigger | `X.__bases__ = (B,)` on another thread | a doc-conforming dict watcher whose mandated `-1` makes `_PyDict_SendEvent` run `sys.unraisablehook` |
| stale thing | the cursor `i` and the borrowed `subclasses` dict / `ref` | the `ix` captured at `:3030` before the notify at `:3038` |
| anchor | `Objects/typeobject.c:793` | `Objects/dictobject.c:3038` |
| failure | `PyDict_Next` yields `*pvalue == NULL` → `type_from_ref(NULL)` → SIGSEGV | stale `ix` → `assert(hashpos >= 0)` / `Py_DECREF(NULL)` |
| fix | lock (or strong-ref) the subclasses dict around the walk | do not run Python inside the notify window / re-read after it |

`delitem_common` appears in both records' site lists in **different roles**: in
CPY-0117 it is the victim of its own re-entrant notify; in CPY-0115 it is the
*other thread's* mutator. Merging them would lose one of the two fixes. Keep both.

### The crash face is an in-slice `dictobject.c` defect

`_PyDict_Next` reads `entry_ptr->me_value` **twice**, with plain loads, and the
second read is what it hands back:

```c
/* Objects/dictobject.c:3228-3238  (the non-unicode arm) */
PyDictKeyEntry *entry_ptr = &DK_ENTRIES(mp->ma_keys)[i];
while (i < n && entry_ptr->me_value == NULL) {     /* :3229  load #1 */
    entry_ptr++; i++;
}
if (i >= n) return 0;
key   = entry_ptr->me_key;
hash  = entry_ptr->me_hash;
value = entry_ptr->me_value;                       /* :3237  load #2 */
...
*pvalue = value;  return 1;
```

`delitem_common` stores `NULL` between them:

```c
/* Objects/dictobject.c:2984-2988 */
PyDictKeyEntry *ep = &DK_ENTRIES(mp->ma_keys)[ix];
old_key = ep->me_key;
STORE_KEY(ep, NULL);
STORE_VALUE(ep, NULL);        /* :2987  <-- the record's own second site */
```

so **the public `PyDict_Next` C-API can return 1 with `*pvalue == NULL`**.
`_PyType_GetSubclasses` then calls `type_from_ref(NULL)`, which does
`_PyWeakref_GET_REF(ref)` at `Objects/typeobject.c:220` with no NULL guard.

The subclasses dict is keyed by `PyLong_FromVoidPtr(type)`
(`get_subclasses_key`), i.e. a **non-unicode combined** dict — so the arm reached
is exactly `_PyDict_Next:3228-3238` against `delitem_common:2984-2988`, which is
precisely the `:2987` the record cites. That was not a coincidence.

### Reproduction

`X.__bases__ = (B,)` → `remove_all_subclasses(X, old_bases)` →
`remove_subclass(A, X)` → `PyDict_DelItem` → `delitem_common`, and when the
delete empties the dict, `clear_tp_subclasses(A)` **frees it**
(`typeobject.c:9790-9793` → `:728`) out from under a concurrent
`A.__subclasses__()`. `type_set_bases` holds `BEGIN_TYPE_LOCK()`;
`type___subclasses___impl` (`:7055`) and its clinic wrapper hold **nothing**.

| build | result |
|---|---|
| `debug-ft-nojit` | **SIGSEGV 6/8** (20 000 rounds, 6 readers, 2 flippers) |
| `debug-ft-nojit` under gdb | **3/3**, identical frame each time |
| `release-ft-nojit` | 0/8 |
| `debug-gil-nojit` | **0/8** — the GIL control the record was missing |
| `release-gil-nojit` | **0/8** |

```
Thread 7 "reader" received signal SIGSEGV
#0  type_from_ref (ref=0x0)                  Objects/typeobject.c:220
#1  _PyType_GetSubclasses (self=0x2000269dc10) Objects/typeobject.c:794
#2  cfunction_vectorcall_NOARGS              Objects/methodobject.c:508
```

`ref=0x0` — a clean NULL, not 0xdd debug fill, so this is the split-load escape
and not a read of freed memory.

**Why `release-ft-nojit` is 0/8, stated precisely.** gcc at `-O2` CSEs the two
source-level reads into one machine load, so the escape does not exist in that
binary:

```
release-ft-nojit  _PyDict_Next
  <+288>: mov (%r8),%rdi      ; load me_value for the loop test
  <+291>: test %rdi,%rdi
  ...
  <+338>: mov %rdi,(%r14)     ; *pvalue = the SAME register
```

versus `debug-ft-nojit`, where `:3229` is `cmpq $0x0,0x10(%r9)` and `:3237` is a
separate `mov`. The C source still contains two non-atomic reads of a field
another thread writes, which is UB under the memory model regardless of what one
compiler happens to emit — the `-O2` result is luck, not a guarantee. The *other*
hazard on this path (the borrowed `subclasses` dict being freed by
`clear_tp_subclasses` mid-walk) is unaffected by CSE and simply did not fire in
8 × 20 000 rounds on release.

### Prior-art coverage — the record's open question, answered

- **gh-151377** ("Data races on type slots and `tp_subclasses` between
  `type_setattro` and `type_new`/`type_dealloc`") is **open**.
- Its PR **gh-151394** is **open and unmerged**. Its whole diff is
  `Misc/NEWS.d/... +2` and `Objects/typeobject.c +40/-7`; `grep` of the diff for
  `GetSubclasses`, `PyDict_Next`, `lookup_tp_subclasses` returns **nothing**. It
  adds `BEGIN_TYPE_LOCK()` around `remove_all_subclasses` in
  `type_dealloc_common` only — a **writer**-side fix for one of several writers.
  The reader, `_PyType_GetSubclasses`, still takes no lock. **The fix does not
  cover this.**
- **gh-153852 / TSAN-0018** does mention `_PyDict_Next` via
  `_PyType_GetSubclasses` — but for plain loads of `dk_nentries`, fixed by
  **#153881**. Different field, different failure; `me_value` is untouched.

### Verdict

`status: reproduced`, **not** `duplicate-of-CPY-0117`. Classification **FIX**.
Two independent one-line-ish fixes: (i) load `entry_ptr->me_value` once in
`_PyDict_Next` (both arms, `:3217/:3225` and `:3229/:3237`) — this one is
**in-slice** and fixes a public C-API contract violation; (ii) hold the type lock
(or a strong reference plus the dict's critical section) across
`_PyType_GetSubclasses`'s walk.

---

## 3. CPY-0130 — plain `ma_values` stores: reachability **YES**, TSan-**confirmed**

The record asked first whether either site is reachable with a *live, shared*
dict. Both are.

### `:7976` — reachable, and trivially so

```c
/* Objects/dictobject.c:7953-7983  detach_dict_from_object */
if (FT_ATOMIC_LOAD_PTR_RELAXED(mp->ma_values) != _PyObject_InlineValues(obj))
    return 0;                                    /* :7959 */
ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED(mp);          /* :7965 */
PyDictValues *values = copy_values(mp->ma_values);   /* :7970 */
mp->ma_values = values;                          /* :7976  PLAIN STORE */
invalidate_and_clear_inline_values(_PyObject_InlineValues(obj));  /* :7978 */
```

Two pure-Python routes on the free-threaded build, both requiring only that the
dict has been **materialized but not yet detached** — which is exactly what
`d = obj.__dict__` produces:

- `obj.__dict__ = X` → `_PyObject_SetManagedDict:7900` →
  `replace_dict_probably_inline_materialized:7846` → `_PyDict_DetachFromObject` → `:7976`
- `obj.__class__ = Y` → `Objects/typeobject.c:7778-7780` → `_PyDict_DetachFromObject` → `:7976`

`d` is an ordinary strong reference any other thread can hold, so the dict is
neither uniquely referenced nor private. **Measured**: a gdb breakpoint on
`dictobject.c:7976` is hit **2,719 times in a 60-round run** of
`CPY-0130_ma_values_plain_store.py` on `debug-ft-nojit`, with two reader threads
concurrently iterating and `.get()`-ing those very dicts.

`ASSERT_WORLD_STOPPED_OR_OBJ_LOCKED(mp)` at `:7965` excludes other *writers*. It
does not touch the lock-free *readers*, which take nothing.

### TSan — `release-ft-nojit-tsan`, upstream FT suppressions, 400 rounds / 4 readers / 2 writers

**14 warnings, 0 noise.** Five summaries name the flagged line directly:

```
Write of size 8 by thread T5:
  #0 detach_dict_from_object                Objects/dictobject.c:7976:19
  #1 _PyDict_DetachFromObject               Objects/dictobject.c:8035
  #2 replace_dict_probably_inline_materialized Objects/dictobject.c:7846
  #3 _PyObject_SetManagedDict               Objects/dictobject.c:7900
  #4 _PyObject_SetDict / subtype_setdict    Objects/typeobject.c:4021 / :4057
Previous read of size 8 by thread T2:
  #0 dictiter_iternext_threadsafe           Objects/dictobject.c:6085:9
SUMMARY: data race Objects/dictobject.c:7976:19 in detach_dict_from_object
```

Pairs, by innermost slice-file frame:

| n | pair |
|---|---|
| 5 | `detach_dict_from_object:7976` ↔ `dictiter_iternext_threadsafe:6085` / `:6100` / `_Py_dict_lookup_threadsafe:1634` / `_Py_atomic_load_ptr_acquire` |
| 4 | `_Py_atomic_load_ptr` (reader) ↔ `copy_values:4467` |
| 2 | `_Py_atomic_load_uint8` (reader) ↔ `new_values:922` / `copy_values:4462` |
| 1 | `get_insertion_order_array` ↔ `new_values:924` |
| 1 | `get_index_from_order:677` ↔ `__tsan_memcpy` |

The second group is the important one and it is **not** noise: it is the missing
release edge, observed. `copy_values` fills the fresh array with plain stores
(`newvalues->size`, the insertion-order `memcpy`, `newvalues->values[i]`), `:7976`
publishes the pointer with a plain store, and the reader loads the pointer with
`_Py_atomic_load_ptr_consume` and then loads `values->size` / `values->values[i]`
atomically. Because the publish is plain, **there is no happens-before edge at
all** between the array-filling writes and the reader's reads — which is exactly
what `set_values()`'s `_Py_atomic_store_ptr_release` (`:215`) exists to
establish, and exactly what TSan reports.

Consequence on a weakly-ordered target: a reader observes the new `ma_values`
pointer with `size` / the order array / the values not yet visible, so
`get_index_from_order` produces an index from uninitialised memory and
`acquire_key_value` increfs whatever `values->values[index]` lands on.

### `:8019` — also reachable, but OOM-gated

```c
/* Objects/dictobject.c:8016-8023, inside PyObject_ClearManagedDict */
Py_BEGIN_CRITICAL_SECTION(dict);
PyDictKeysObject *oldkeys = dict->ma_keys;
set_keys(dict, Py_EMPTY_KEYS);     /* :8018  atomic release store */
dict->ma_values = NULL;            /* :8019  plain store, one line below */
```

`detach_dict_from_object` fails on exactly one condition — `copy_values`
returning NULL — so `:8019` is an OOM-only line. `CPY-0130_site8019_reachability.py`
sweeps `_testcapi.set_nomemory`: **index 0 drives execution through `:8019`**
(gdb breakpoint hit, `debug-gil-nojit`), with the materialized dict still held by
an external `d`. So the answer to the record's question is yes for this site too
— but its severity is strictly lower than `:7976`'s, because the value stored is
`NULL` (no publish ordering to lose) and the readers all handle a NULL
`ma_values` explicitly.

### Verdict

`status: static-confirmed` (TSan-confirmed, reachability proven, not
crash-reproduced). Classification **FIX** for `:7976`, **CONSIDER** for `:8019`.
Not crash-reproduced, and I do not expect it to be on x86-64: the machine is TSO,
so the plain store behaves as a release store and the reordering cannot be
observed. The evidence that matters here is the missing edge, and TSan has it.
Fix is mechanical and already exists in the file: `set_values(mp, values)` and
`set_values(dict, NULL)`.

---

## 4. CPY-0129 — `dictiter_len` torn / UAF read: the record's stated cause is **wrong**

**Sites:** `Objects/dictobject.c:5682` (the read), `:6158` (the write).

```c
/* :5678-5685 */
dictiterobject *di = (dictiterobject *)self;
Py_ssize_t len = 0;
if (di->di_dict != NULL && di->di_used == GET_USED(di->di_dict))   /* :5682 */
    len = FT_ATOMIC_LOAD_SSIZE_RELAXED(di->len);                   /* :5683 */

/* :6157-6160, dictiter_iternext_threadsafe */
fail:
    di->di_dict = NULL;      /* :6158 */
    Py_DECREF(d);            /* :6159 */
```

### Correction 1 — "QSBR delays the free" is false for the field being read

The record and the ft-race-scanner report both explain the 0/3000 result as
*"FT's QSBR-delayed free keeps the stale dict readable"*. QSBR defers the
**keys** and **values** arrays (`free_keys_object`, `free_values`, gated on
`IS_DICT_SHARED`). It does not defer the `PyDictObject` header — and `ma_used`,
the field `GET_USED` dereferences, is in the header:

```c
/* Python/gc_free_threading.c, PyObject_GC_Del -- the whole tail of the function */
record_deallocation(_PyThreadState_GET());
PyObject_Free(((char *)op)-presize);
```

What actually defers the free is **biased reference counting**: when a
*non-owning* thread drops the last reference, `_Py_MergeZeroLocalRefcount` hands
the object to the owner's queue. So the drainer must be the thread that created
the dict. The rewritten reproducer does that (each drainer builds and exhausts
its own iterators; only the probers are foreign threads, which is sound because
`dictiter_len` never calls `ensure_shared_on_read`).

### Correction 2 — the "compiler rematerialises `di_dict`" escalation does not exist here

If `:5682`'s two uses of `di->di_dict` compiled to two loads, the second could
return NULL and `GET_USED(NULL)` would be a hard SIGSEGV. It does not:

```
release-ft-nojit  dictiter_len
  <+0> : mov 0x20(%rdi),%rax    ; di->di_dict, loaded ONCE
  <+4> : test %rax,%rax
  <+13>: mov 0x20(%rax),%rax    ; ma_used, off the SAME register
```

### Correction 3 — why no crash is *structurally* expected, not just unlucky

`:6158` clears the field **before** `:6159` drops the reference. A prober that
loaded a non-NULL `di_dict` did so before `:6158`; for its dereference ~10
instructions later to touch freed memory, the whole
`Py_DECREF → _Py_MergeZeroLocalRefcount → dict_dealloc → PyObject_GC_Del →
PyObject_Free` chain (hundreds of ns) has to complete inside those ~10
instructions. It cannot — **unless the prober is descheduled between its two
loads**, which requires more runnable threads than cores.

That is a materially different explanation from the one in the record, and it
changes what a reproduction attempt has to do: oversubscribe, and run on a build
that poisons freed blocks.

### What was run

| build | configuration | result |
|---|---|---|
| `release-ft-nojit` | 6 probers / 3 drainers, 15 s | **0 crashes**, 1,727,000 exhaustion drops staged |
| `release-ft-nojit` | 6 probers / 3 drainers, 8 s, iterators pinned | **0 crashes**, 929,584 drops |
| `release-ft-nojit-asan-mitrack` | 6 probers / 3 drainers, 150 s, pin ring 2^16 | **0 reports** — but **I killed this run at 150 s**, so it printed no drop count. Not usable as a denominator; superseded by the last row. |
| `release-ft-nojit-asan-mitrack` | 40 probers / 4 drainers, pin ring 2^16, 240 s requested | **aborted at ~4 s on the OFF-TARGET site** (`list_get_item_ref` use-after-poison, below). Oversubscription stretched prober deschedules past the ring's eviction horizon, so the ring stopped pinning. **No result for the target site.** |
| **`release-ft-nojit-asan-mitrack`** | **24 probers / 4 drainers / 2 slots each, pin ring 2^22, 240 s** (oversubscribed 1.75× on 16 cores) | **0 ASan reports, exit 0, 3,103,000 exhaustion drops staged** |

The last row is the decisive one — it is the configuration Correction 3 argues is
the only one that can produce the crash (oversubscribed so probers are
involuntarily descheduled mid-`dictiter_len`, on a build that poisons freed
mimalloc blocks), and it is clean. **Total across the three runs that reported a
denominator: 5,759,584 exhaustion drops, 0 events at `:5682`.**

`asan_CPY-0129_oversub2.txt` in full:

```
seconds=240.0 probers=24 drainers=4 gil=False
survived: 3103000 exhaustion drops staged
```

`release-ft-nojit-asan-mitrack` is the right instrument and it does work: it is a
free-threaded ASan build with `MI_TRACK_ASAN`, so mimalloc blocks are poisoned on
free, and the disassembly confirms the dereference at `:5682` is instrumented —

```
<+52>: add $0x20,%rax             ; %rax = di->di_dict + offsetof(ma_used)
<+56..70>: shadow check on (%rax)
<+72>: mov (%rax),%rax
```

### A trap this pass paid for, worth recording

The first ASan-mitrack run reported in **4 seconds** — but on the wrong site:

```
ERROR: AddressSanitizer: use-after-poison ... READ of size 8 at ... thread T7
  #0 _Py_IsOwnedByCurrentThread   ./Include/cpython/object.h:580
  #1 _Py_TryIncrefFast            ./Include/internal/pycore_object.h:530
  #2 _Py_TryIncrefCompare         ./Include/internal/pycore_object.h:569
  #3 _Py_TryXGetRef               ./Include/internal/pycore_object.h:608
  #4 list_get_item_ref            Objects/listobject.c:373
```

That is a prober doing `SLOT[i]` — CPython's **deliberate** optimistic-incref
read of a list element's `ob_tid`/`ob_ref_local` after another thread replaced
it. On `release-ft-nojit-asan-mitrack` every such read is a use-after-poison
report, and with `halt_on_error=1` it ends the run before the target site can
fire. The reproducer now pins retired iterators in a 65 536-entry ring so the
*dict* dies but the *iterator* does not. **Anyone using that build must isolate
the target from every `_Py_TryXGetRef` path first** — see toolkit feedback.

### Verdict

**`status: static-confirmed` — unchanged, but the record's stated *cause* for the
null result must be corrected.** Not crash-reproduced in **5,759,584 exhaustion
drops** across `release-ft-nojit` and `release-ft-nojit-asan-mitrack`, including
a 240 s oversubscribed run on the poisoning build (3,103,000 drops, 0 reports).
The TSan pair the record already carries (`:5682` ↔ `:6158`) stands; the read is
still an unsynchronised dereference of a field another thread clears and frees,
and `it.__length_hint__()` is public. Classification stays **CONSIDER**, at the
strength actually measured.

**The reusable part, and the reason this is not just another negative.** The
record says the 0/3,000 result is because *"QSBR delays the free"*. That is
false for the field being dereferenced. `GET_USED` reads `ma_used`, which lives
in the `PyDictObject` **header**, and `PyObject_GC_Del`
(`Python/gc_free_threading.c`) ends in a plain `PyObject_Free` — no QSBR
deferral. QSBR defers the **keys** and **values** arrays only
(`free_keys_object` / `free_values`, gated on `IS_DICT_SHARED`). What actually
defers the header free is **biased reference counting**: a *non-owning* thread's
final `Py_DECREF` goes through `_Py_MergeZeroLocalRefcount` and hands the object
to the owner's queue instead of freeing it. So **the thread that drains the
iterator must be the thread that created the dict**, or the free is deferred for
a reason that has nothing to do with QSBR and the run is a null result for the
wrong reason. Anyone re-running this needs that, plus a poisoning build
(`release-ft-nojit-asan-mitrack`), plus oversubscription.

And with all three in place it still does not fire — for a reason that is now
understood rather than assumed: `:6158` sets `di->di_dict = NULL` **before**
`:6159` drops the reference, so a prober that read a non-NULL pointer must remain
parked between two adjacent loads for the entire
`Py_DECREF → dealloc → free → poison` chain. That is a structural argument that
the reader-side crash is very unlikely, not merely unobserved — which is worth
more to a maintainer than another round of trials. The fix is unchanged and
cheap (`FT_ATOMIC_LOAD_PTR_ACQUIRE` into a local, or a critical section over the
two lines), and it is worth taking on the strength of the TSan pair alone.

Two escalations were tested and **do not exist** on this build, so nobody needs
to re-try them: the compiler does not rematerialise `di_dict` (single load at
`<+0>`, §Correction 2), and the FT ASan build does not catch the read because
the free has not happened yet when the read retires.

---

## Classes bounded

- **`gc.get_objects()` as a route into mid-teardown objects — 1 hole out of 1
  filter.** `visit_get_objects` / `visit_get_referrers` both check
  `_PyGC_BITS_UNREACHABLE`; `delete_garbage:1742` clears it before `:1761`. The
  filter is correct for `finalize_garbage` (bit still set) and wrong for
  `delete_garbage`.
- **Callers of `detach_dict_from_object` — 3/3 enumerated.**
  `replace_dict_probably_inline_materialized:7846` (FT arm of
  `_PyObject_SetManagedDict`), `_PyObject_SetManagedDict:7927` (GIL arm, not
  compiled on FT), `PyObject_ClearManagedDict:8007`, plus the external caller
  `Objects/typeobject.c:7779` (`object_set_class`). Two of the four reach `:7976`
  with a live externally-referenced dict; the GIL-arm one cannot race.
- **`_PyDict_Next` double reads of `me_value` — 2/2 arms.** Unicode arm
  `:3217`/`:3225`, generic arm `:3229`/`:3237`. Both have the same shape. The
  split-table arm (`:3201-3209`) reads `values->values[index]` once and asserts
  `value != NULL`, so it has the same escape as an *assertion* rather than as a
  NULL return.
- **CPY-0115 vs CPY-0117 overlap — 1 shared site, 0 shared mechanisms.** The
  only common token is `delitem_common`, in opposite roles.
- **`_PyType_GetSubclasses` locking — 0 of 2 entry points hold anything.**
  `type___subclasses___impl:7055` and the clinic wrapper
  (`Objects/clinic/typeobject.c.h:94`) both call straight through.

---

## Toolkit feedback

1. **`release-ft-nojit-asan-mitrack` needs a documented caveat, and it is the
   single highest-value item here.** The build finally gives the free-threaded
   object heap an ASan shadow (the `AGENT_BRIEF` trap "FT ASan builds have no
   shadow for the object heap" is out of date for *this* build) — but CPython's
   optimistic-incref fast paths (`_Py_TryIncrefCompare`, `_Py_TryXGetRef`,
   `list_get_item_ref`, `acquire_key_value`) *intentionally* read the header of a
   possibly-freed object. Every one of those is a `use-after-poison` report. Any
   run on that build must (a) use `halt_on_error=0` or (b) keep the objects those
   paths touch alive, or the first 4 seconds of the run will be spent on
   somebody else's site.

2. **`symbolize=1` in-process can wedge.** One ASan-mitrack run printed the error
   header and then sat in `llvm-symbolizer-21` for minutes with the other threads
   still running. `symbolize=0` plus offline symbolization is the safer default
   for a multi-threaded stress.

3. **A `ctypes`-based "revive by address" acquisition is not admissible as a
   control.** It produced 2/6 crashes on `debug-gil-nojit` for CPY-0127, which
   would have been reported as "the GIL build crashes too" — it was the harness
   dangling. Where a reproducer has to reach an object the runtime considers
   dead, prefer a documented API that increfs (`gc.get_objects()`), and always
   run the same mode on the GIL build.

4. **`scan_ft_races`, CPY-0129 follow-up.** The record already notes the scanner
   fired on the right line with the wrong member. The deeper miss is that its
   `detail` text asserts a *cause* ("QSBR delays the free") that is false for the
   header fields; a triager who trusts it stops. Suggest the rule emit the
   *store-then-drop ordering* (`field = NULL` at line X, `Py_DECREF` at line X+1)
   as the reason a reader-side crash is unlikely, rather than QSBR.

5. **Recall gap found by reading, not by a scanner:** `_PyDict_Next` reading
   `entry_ptr->me_value` twice. No rule in the current set looks for *the same
   field read twice in one function with no atomic and a write to it elsewhere in
   the file*. That shape produced a public C-API contract violation
   (`PyDict_Next` returning 1 with `*pvalue == NULL`) and a reproduced SIGSEGV.
   It is a cheap rule: same-function, same-member-expression, ≥2 plain reads,
   ≥1 `STORE_*`/plain write to that member elsewhere in the file.

---

## Noticed outside slice

- `Python/gc_free_threading.c:1742` clears `_PyGC_BITS_UNREACHABLE` before
  `:1761` dispatches `tp_clear`, defeating the `gc.get_objects()` /
  `gc.get_referrers()` filter at `:2423` and `:2371`. Own record candidate.
- `Objects/typeobject.c:220` `type_from_ref` dereferences `ref` with no NULL
  guard; every caller assumes `PyDict_Next` cannot yield NULL.
- `Objects/typeobject.c:775-807` `_PyType_GetSubclasses` holds `subclasses`
  borrowed across `PyList_Append` (which allocates and can run the GC) while
  `remove_subclass:9790` can `Py_CLEAR` that same dict.
- PR gh-151394 (open) fixes only the `type_dealloc_common` writer; the
  `type_set_bases` writer already holds the type lock and the reader holds
  nothing, so the PR as written does not close gh-151377's reader side.
