# refcount-auditor — Modules/ sample (informed)

**Toolkit:** cpython-review-toolkit v0.8.0 · `scan_refcounts.py`
**Target:** `/home/danzin/projects/cpython` main @ `4f3be1b5777` (3.16.0a0)
**Repro builds:** `/home/danzin/projects/python_build_matrix/builds/{debug-gil-nojit, debug-gil-nojit-asan, release-gil-nojit-asan, debug-ft-nojit}` @ `a1d580430c8`.
**File-identity check (required by RUN_CONTEXT):** `Modules/itertoolsmodule.c` differs between the two trees in exactly one hunk — `count_repr` at `:3675` (the TSAN-0006 `FT_ATOMIC_LOAD_SSIZE_RELAXED` fix, present in the matrix tree, absent at `4f3be1b5777`), which adds 2 lines. Therefore **every line below 3675 has the same number in both trees**, and matrix lines above 3675 are target + 2. `zip_longest_next_lock_held`, `batched_next` and `islice_next` are byte-identical in both trees (verified by function-body `diff`). `_heapqmodule.c`, `_pickle.c`, `_elementtree.c`, `_collectionsmodule.c`, `_json.c` are byte-identical.

## Scanner volume

```
raw: 2 | confirmed: 2 | dismissed: 0 | precision: 100%
```

Both candidates are `borrowed_ref_across_call` in `Modules/itertoolsmodule.c`
`zip_longest_next_lock_held`. **Both are real, both reproduce as heap-use-after-free
under ASan from ~15 lines of pure Python.** This is the rule's first firing on
territory outside `Objects/`; it goes 2/2 here, for a cumulative 4/4 since the v0.8
rewrite.

Hand-reading beyond the scanner produced **8 further confirmed memory-safety bugs**
that the rule structurally cannot see, every one reproduced as a SIGSEGV / SIGABRT /
ASan `heap-use-after-free` and **independently re-run by me**, not merely reported:

| site | function | crash |
|---|---|---|
| `Modules/itertoolsmodule.c:210` | `batched_next` | ASan heap-use-after-free |
| `Modules/itertoolsmodule.c:1711` | `islice_next` | ASan heap-use-after-free (GIL **and** FT) |
| `Modules/_pickle.c:7414` | `find_class_impl` | ASan heap-use-after-free |
| `Modules/_pickle.c:6586` | `load_extension` | ASan heap-use-after-free |
| `Modules/_struct.c:2436` / `:2102` / `:2274` | `s_pack_internal` / `s_unpack_internal` / `unpackiter_iternext` | ASan UAF + heap-buffer-overflow |
| `Modules/_csv.c:953` | `Reader_iternext_lock_held` | ASan SEGV on the zero page |
| `Modules/_zoneinfo.c:2436` | `find_in_strong_cache` | ASan heap-use-after-free |
| `Modules/_zoneinfo.c:2583` | `clear_strong_cache` | ASan heap-use-after-free |

Plus one **CONSIDER** (`_pickle.c:4689`, integrity failure without a memory fault).
The coordinator additionally supplied `Modules/itertoolsmodule.c:3641`
`count_nextlong` — confirmed and re-run (`EXIT=139`). See *Toolkit assessment* for the
verified root-cause diagnosis and two measured rule proposals.

---

## Findings

### [FIX] `Modules/itertoolsmodule.c:3988` and `:4018` — stale `it` dropped in `zip_longest_next_lock_held` (SCANNER, both candidates)

**What breaks.** `it` is loaded borrowed from the iterator tuple at `:3974` (and
`:4004` for the second, non-recycling branch):

```c
it = PyTuple_GET_ITEM(lz->ittuple, i);          /* :3974 — borrowed */
...
item = PyIter_Next(it);                          /* :3978 — runs arbitrary Python */
if (item == NULL) {
    lz->numactive -= 1;
    if (lz->numactive == 0 || PyErr_Occurred()) { ... }
    else {
        item = Py_NewRef(lz->fillvalue);
        PyTuple_SET_ITEM(lz->ittuple, i, NULL);  /* :3987 — slot cleared */
        Py_DECREF(it);                           /* :3988 — STALE local dropped */
    }
}
```

A re-entrant `next(zl)` from inside a user `__next__` takes the exhaustion branch
itself: it sets `ittuple[i] = NULL` and releases the tuple's reference. When the outer
frame's iterator then also raises `StopIteration`, the outer executes the *same*
teardown against its stale local — a second `Py_DECREF` of a reference the function
never owned and which is already gone.

`:3988` is the `_PyObject_IsUniquelyReferenced(result)` recycling branch; `:4018` is
the fresh-tuple branch, reached whenever the caller still holds a previously returned
result tuple. Both reproduce independently.

**Guarded twin.** Three, all strong:
- `Python/bltinmodule.c:3284` `zip_next` — the direct sibling. It re-reads
  `it = PyTuple_GET_ITEM(lz->ittuple, i)` on every iteration and **never NULLs an
  `ittuple` slot nor DECREFs `it`**. `zip` is immune by construction; `zip_longest`
  is the only member of the family that invalidates its own iterator tuple.
- `Modules/itertoolsmodule.c:1979`/`:1985`/`:1999` `chain_next_lock_held` — clears
  the exact same kind of slot with `Py_CLEAR(lz->source)` / `Py_CLEAR(lz->active)`,
  and never caches it into a local at all.
- `Modules/itertoolsmodule.c:361-374` `pairwise_next` — clears with `Py_CLEAR` *and*
  re-reads `it = po->it` after the call at `:364` with an explicit NULL check at
  `:365-368`.

`Py_CLEAR` appears **21 times** in `itertoolsmodule.c`; `zip_longest_next_lock_held`
is the only place in the file that open-codes a clear-and-drop. This is not a
convention — it is an outlier.

**How Python input reaches it.** `next()` on a `zip_longest` from inside one of its
own source iterators' `__next__`. Needs ≥3 iterables so `numactive` stays >0 after
the outer decrement.

**FP classes ruled out.**
- *Borrowed under a known-live owner* — **ruled out by construction.** The owner
  is `lz->ittuple` slot `i`, and the bug is precisely that the re-entrant call
  *removes* that owner (`PyTuple_SET_ITEM(..., NULL)` + release). Passing the source
  iterable as a temporary (`zip_longest(A(), ...)`) leaves `ittuple` holding the only
  strong reference, so the object is genuinely freed. Verified by ASan's `freed by
  thread T0 here` frame: `subtype_dealloc` reached from the popped `__next__` frame.
- *Stolen-ref API used correctly* — N/A: `lz->ittuple` is a long-lived published
  container, not a freshly created one.
- *`Py_CLEAR` NULLs its own operand* — N/A: the code does **not** use `Py_CLEAR`;
  that is the bug.
- *Owner swap* — N/A: the slot is set to `NULL`, not to a replacement, which the
  taxonomy names as exactly the dangerous variant.
- *`#if`/`#else`* — N/A: no preprocessor conditional in this function.

**Fix.** Replace the pair with a slot-re-reading clear:

```c
- PyTuple_SET_ITEM(lz->ittuple, i, NULL);
- Py_DECREF(it);
+ PyObject *stale = PyTuple_GET_ITEM(lz->ittuple, i);
+ PyTuple_SET_ITEM(lz->ittuple, i, NULL);
+ Py_XDECREF(stale);
```

(the tuple analogue of `Py_CLEAR` — re-read the slot, so a re-entrant clear makes the
second drop a no-op). Both sites.

**Reproduction — `:3988`, recycling branch.**

`repro_ziplongest.py`:
```python
from itertools import zip_longest

class A:
    n = 0
    def __iter__(self): return self
    def __next__(self):
        A.n += 1
        if A.n == 1:
            next(zl)          # re-enter the SAME zip_longest object
        raise StopIteration

zl = zip_longest(A(), iter([1, 2, 3]), iter([4, 5, 6]))
next(zl)
```

```
$ builds/debug-gil-nojit/python repro_ziplongest.py
before
./Modules/itertoolsmodule.c:3990: _Py_NegativeRefcount: Assertion failed: object has negative ref count
<object at 0x7c86a87d53a0 is freed>
Fatal Python error: _PyObject_AssertFailed: _PyObject_AssertFailed
Aborted (core dumped)          EXIT=134
```
(`:3990` matrix = `:3988` target.)

```
$ ASAN_OPTIONS=detect_leaks=0 PYTHONMALLOC=malloc builds/release-gil-nojit-asan/python repro_ziplongest.py
before
=================================================================
==4039492==ERROR: AddressSanitizer: heap-use-after-free on address 0x7b3334ff4b20 at pc 0x601afdeba5a9
READ of size 4 at 0x7b3334ff4b20 thread T0
    #0 ... in _Py_IsImmortal ./Include/refcount.h:132:12
    #1 ... in Py_DECREF ./Include/refcount.h:421:9
    #2 ... in zip_longest_next_lock_held ./Modules/itertoolsmodule.c:3990:25
    #3 ... in zip_longest_next ./Modules/itertoolsmodule.c:4035:14
    #4 ... in builtin_next Python/bltinmodule.c:1776:11
0x7b3334ff4b20 is located 32 bytes inside of 328-byte region [0x7b3334ff4b00,0x7b3334ff4c48)
freed by thread T0 here:
    #1 ... in subtype_dealloc Objects/typeobject.c:2876:5
    #2 ... in _Py_Dealloc Objects/object.c:3319:5
    #5 ... in frame_dealloc Objects/frameobject.c:1952:13
    #9 ... in tb_dealloc Python/traceback.c:246:5
   #13 ... in StopIteration_dealloc Objects/exceptions.c:775:11
   #16 ... in iternext Objects/abstract.c:2887:9
   #17 ... in PyIter_Next Objects/abstract.c:2926:11
EXIT=1
```

Note the free path: the last reference dies inside `PyIter_Next` itself, when
clearing the `StopIteration` drops the traceback that held the `__next__` frame.

**Reproduction — `:4018`, fresh-tuple branch.** Same object, but the caller retains
the first result tuple so `lz->result` is no longer uniquely referenced
(`repro_ziplongest_else.py`):

```
$ builds/debug-gil-nojit/python repro_ziplongest_else.py
before
./Modules/itertoolsmodule.c:4020: _Py_NegativeRefcount: Assertion failed: object has negative ref count
<object at 0x78cfa22d13a0 is freed>          EXIT=134

$ ASAN .../release-gil-nojit-asan/python repro_ziplongest_else.py
ERROR: AddressSanitizer: heap-use-after-free ... 
    #2 ... in zip_longest_next_lock_held ./Modules/itertoolsmodule.c:4020:25
```
(`:4020` matrix = `:4018` target.)

Also reproduces on `debug-ft-nojit` — the `Py_BEGIN_CRITICAL_SECTION(op)` in
`zip_longest_next` (`:4032`) does **not** prevent same-thread re-entrancy.

---

### [FIX] `Modules/itertoolsmodule.c:210` — `batched_next` calls through a freed iterator (NEW, scanner-missed)

**What breaks.**

```c
PyObject *it = bo->it;                              /* :196 — borrowed slot cache */
...
iternextfunc iternext = *Py_TYPE(it)->tp_iternext;  /* :207 */
for (i=0 ; i < n ; i++) {
    item = iternext(it);                            /* :210 — runs arbitrary Python */
    ...
}
 null_item:
    ...
#ifndef Py_GIL_DISABLED
    Py_CLEAR(bo->it);                               /* :224, :234, :242 */
#endif
```

A re-entrant `next(b)` on the same `batched` object exhausts the source, reaches
`null_item` with `i == 0`, and executes `Py_CLEAR(bo->it)` — releasing the only
strong reference. Control returns to the outer frame, whose `it` local now dangles,
and the outer loop calls `iternext(it)` again. This is worse than a double-DECREF:
it is an **indirect call using a freed object's memory as `self`**, and
`slot_tp_iternext` immediately reads `Py_TYPE(self)` out of the freed block.

**Guarded twin — in the same function, in the other preprocessor arm.** gh-123471 /
GH-129416 ("make `itertools.batched` thread-safe") wrapped the three `Py_CLEAR(bo->it)`
calls in `#ifndef Py_GIL_DISABLED` — i.e. the free-threaded build *already* declines
to invalidate the cached iterator, and is correct. Only the default GIL build clears.
`pairwise_next:364` is the other twin: it re-reads `it = po->it` after the
Python-reaching call at `:358` and bails if the slot went NULL.

> **This is not the "mutually exclusive `#if`/`#else`" FP class.** That class is about
> reasoning *across* arms. Here the `Py_CLEAR` and the dangerous `iternext(it)` are
> both compiled into the *same* (default, GIL) build; the `#ifndef` only tells us the
> bug is GIL-build-only — and the FT arm is the fix.

**FP classes ruled out.** *Known-live owner*: ruled out — `bo->it` is the sole owner
when the iterable is passed as a temporary, and the re-entrant `Py_CLEAR` removes it;
ASan's free stack shows `subtype_dealloc` from the popped `__next__` frame.
*Ownership transfer*: N/A, `Py_CLEAR` releases, it does not move.

**How Python input reaches it.** `next(b)` from inside the source iterator's
`__next__`, swallowing the inner `StopIteration` and returning a value so the outer
loop takes another turn.

**Fix.** Re-read the slot after each `iternext` (the `pairwise_next` pattern), or —
simpler and matching what the FT arm already does — drop the GIL-build `Py_CLEAR`
entirely and let `tp_clear`/dealloc release `bo->it`.

**Reproduction** (`repro_batched.py`):
```python
from itertools import batched

class Evil:
    n = 0
    def __iter__(self): return self
    def __next__(self):
        Evil.n += 1
        if Evil.n == 1:
            try: next(b)          # inner call exhausts -> Py_CLEAR(bo->it)
            except StopIteration: pass
            return 1              # outer loop continues with a dangling `it`
        raise StopIteration

b = batched(Evil(), 3)
next(b)
```

```
$ builds/debug-gil-nojit/python repro_batched.py
before
Segmentation fault (core dumped)      EXIT=139

$ gdb -batch -ex run -ex "bt 6" --args builds/debug-gil-nojit/python repro_batched.py
Program received signal SIGSEGV, Segmentation fault.
0x... in _PyType_LookupStackRefAndVersion (type=0xdddddddddddddddd, ...)
#1  ... in lookup_method_ex (self=0x7ffff74d1500, ...) at Objects/typeobject.c:3001
#3  ... in vectorcall_method             at Objects/typeobject.c:3125
#4  ... in slot_tp_iternext              at Objects/typeobject.c:11121
#5  ... in batched_next (op=0x7ffff74f24e0) at ./Modules/itertoolsmodule.c:210
#6  ... in builtin_next                  at Python/bltinmodule.c:1776
```
`type=0xdddddddddddddddd` is the debug allocator's DEADBYTE fill — the freed
iterator's `ob_type`.

```
$ ASAN_OPTIONS=detect_leaks=0 PYTHONMALLOC=malloc builds/debug-gil-nojit-asan/python repro_batched.py
before
==4075852==ERROR: AddressSanitizer: heap-use-after-free on address 0x70f6e81f84e8
READ of size 8 at 0x70f6e81f84e8 thread T0
    #0 ... in _Py_TYPE_impl ./Include/object.h:234:16
    #1 ... in lookup_method_ex Objects/typeobject.c:3001:38
    #4 ... in slot_tp_iternext Objects/typeobject.c:11121:12
    #5 ... in batched_next ./Modules/itertoolsmodule.c:210:16
0x70f6e81f84e8 is located 40 bytes inside of 328-byte region
freed by thread T0 here:
    #1 ... in subtype_dealloc Objects/typeobject.c:2876:5
    #3 ... in Py_DECREF_MORTAL ./Include/internal/pycore_object.h:419:9
    #4 ... in PyStackRef_XCLOSE ./Include/internal/pycore_stackref.h:726:9
EXIT=1
```

`debug-ft-nojit`: prints `after`, exit 0 — **free-threaded builds are not affected**,
confirming the `#ifndef` arm is the guard.

---

### [FIX] `Modules/itertoolsmodule.c:1711` — `islice_next` calls through a freed iterator (NEW, scanner-missed)

**What breaks.** Identical shape, and this one hits *both* build configurations:

```c
PyObject *it = lz->it;                              /* :1701 — borrowed slot cache */
if (it == NULL) return NULL;
iternext = *Py_TYPE(it)->tp_iternext;               /* :1709 */
while (lz->cnt < lz->next) {
    item = iternext(it);                            /* :1711 — runs arbitrary Python */
    ...
}
item = iternext(it);                                /* :1719 — same exposure */
...
empty:
    Py_CLEAR(lz->it);                               /* :1732 — unconditional */
```

The skip loop (`start > 0`) gives the attacker several turns. A re-entrant `next(sl)`
reaches `empty:` and `Py_CLEAR(lz->it)`; the outer loop then calls through the freed
object. Unlike `batched`, `:1732` is **not** `#ifndef`-guarded, so the free-threaded
build crashes too.

**Guarded twin.** `pairwise_next:358-368` — same file, same situation: it calls
`(*Py_TYPE(it)->tp_iternext)(it)` at `:358`, then **re-reads `it = po->it` at `:364`**
and returns on NULL at `:365-368`. Also `chain_next_lock_held:1977-1999`, which never
caches the slot at all.

**FP classes ruled out.** Same reasoning as `batched_next`; additionally, `it` is
*not* re-read anywhere between `:1701` and `:1719`, and `lz->it` is Python-settable
only through the C code itself, so `Py_CLEAR(lz->it)` at `:1732` is the sole
invalidation path — and it is reachable re-entrantly.

**Fix.** Re-read `lz->it` at the top of each loop turn and bail on NULL, or hold a
strong reference for the duration of `islice_next`.

**Reproduction** (`repro_islice.py` — `islice(Evil(), 5, 10)`, so the skip loop runs):

```
$ builds/debug-gil-nojit/python repro_islice.py
before
Segmentation fault (core dumped)      EXIT=139

$ ASAN_OPTIONS=detect_leaks=0 PYTHONMALLOC=malloc builds/debug-gil-nojit-asan/python repro_islice.py
before
==4081333==ERROR: AddressSanitizer: heap-use-after-free on address 0x7578229f84e8
READ of size 8 at 0x7578229f84e8 thread T0
    #0 ... in _Py_TYPE_impl ./Include/object.h:234:16
    #1 ... in lookup_method_ex Objects/typeobject.c:3001:38
    #4 ... in slot_tp_iternext Objects/typeobject.c:11121:12
    #5 ... in islice_next ./Modules/itertoolsmodule.c:1711:16

$ builds/debug-ft-nojit/python repro_islice.py
before
Segmentation fault (core dumped)      EXIT=139
```

---

### [FIX] `Modules/_pickle.c:7414` — `global_name` used after `PyImport_Import` in `_pickle_Unpickler_find_class_impl` (NEW, scanner-missed)

**What breaks.** In the `fix_imports` (protocol < 3) path:

```c
item = PyDict_GetItemWithError(st->name_mapping_2to3, key);   /* :7346 — borrowed */
...
module_name = PyTuple_GET_ITEM(item, 0);                      /* :7355 — borrowed */
global_name = PyTuple_GET_ITEM(item, 1);                      /* :7356 — borrowed */
...
module = PyImport_Import(module_name);                        /* :7392 — arbitrary Python */
...
global = PyObject_GetAttr(module, global_name);               /* :7414 — STALE */
```

`st->name_mapping_2to3` **is** the live `_compat_pickle.NAME_MAPPING` dict object
(captured by identity at `:329-330`), and its value 2-tuple is the sole owner of both
strings. `PyImport_Import` dispatches through `builtins.__import__`, any `sys.meta_path`
finder, and the imported module's top-level code — any of which can rebind or clear the
mapping. `PyImport_Import` incidentally protects `module_name` (the `__import__`
argument tuple INCREFs it), but **`global_name` is never INCREF'd anywhere** between
`:7356` and `:7414`. `:7328` `PySys_Audit("pickle.find_class", …)` is a second
Python-reaching call in the same window.

**Guarded twin.** `fix_imports` at `Modules/_pickle.c:3839-3840` — the *pickler-side
mirror of the identical table lookup* — takes strong references:
`*module_name = Py_NewRef(fixed_module_name); *global_name = Py_NewRef(fixed_global_name);`
and `:3856` `Py_XSETREF(*module_name, Py_NewRef(item));`. The borrow is therefore not a
house convention; `find_class` is the outlier. (Verified by reading both sites.)

**FP classes ruled out.** *Known-live owner*: ruled out — ASan's free chain is
`dict_clear` → `PyDict_Clear` → `dictkeys_decref` → `tuple_dealloc` → `unicode_dealloc`,
i.e. the dict entry was the only owner. *Out-parameter re-binding*: no `&`; `:7355`/`:7356`
are plain assignments. *`Py_CLEAR` self-NULL*: the pointer is never NULLed, it is left
stale. *Owner swap*: nothing is written back. *Shadowed re-declaration*: these are the
function's own parameters, rebound in place.

**Fix.** `Py_NewRef` the two `PyTuple_GET_ITEM` results at `:7355`/`:7356` (and `item`
at `:7380`) into owned locals, DECREF at exit — mirroring `:3839-3840`.

**Reproduction** (`repro_findclass_nofill.py`; pure Python, stdlib-only — a
`builtins.__import__` wrapper that clears `_compat_pickle.NAME_MAPPING`, then
`pickle.loads(b'\x80\x02cevilmod\nevilname\n.')`). **Independently re-run and confirmed
by this agent**, not only by the sub-sweep:

```
$ builds/debug-gil-nojit/python repro_findclass_nofill.py
about to loads()
Segmentation fault (core dumped)      EXIT=139

Program received signal SIGSEGV, Segmentation fault.
PyObject_GetAttr (v=0x7ffff7376cc0, name=0x7ffff74fc160) at Objects/object.c:1313
#1  _pickle_Unpickler_find_class_impl  at ./Modules/_pickle.c:7414
#7  find_class                         at ./Modules/_pickle.c:5402
#8  load_global                        at ./Modules/_pickle.c:6300
#10 _pickle_loads_impl (... fix_imports=1 ...) at ./Modules/_pickle.c:8157
```

```
$ ASAN .../release-gil-nojit-asan/python repro_findclass_nofill.py
==4079180==ERROR: AddressSanitizer: heap-use-after-free on address 0x77bbbc1fbb08
READ of size 8 at 0x77bbbc1fbb08 thread T0
    #0 PyObject_GetAttr Objects/object.c:1313:10
    #1 _pickle_Unpickler_find_class_impl ./Modules/_pickle.c:7414:18
freed by thread T0 here:
    #1 unicode_dealloc Objects/unicodeobject.c:1714:5
    #5 tuple_dealloc  Objects/tupleobject.c:277:9
    #9 dictkeys_decref Objects/dictobject.c:518:17
   #11 PyDict_Clear   Objects/dictobject.c:3178:5
   #13 dict_clear     Objects/clinic/dictobject.c.h:170:12
```

A heap-refill variant (`repro_findclass.py`) instead resolves a *different* attribute
name read out of the recycled block — silent wrong-object resolution rather than a
crash, which is the more security-relevant outcome for an unpickler.

### [FIX] `Modules/_pickle.c:6586` — `pair` borrowed across `find_class()` in `load_extension` (NEW, scanner-missed)

**What breaks.** Same shape, a second independent site:

```c
pair = PyDict_GetItemWithError(st->inverted_registry, py_code);  /* :6559 — borrowed */
...
module_name = PyTuple_GET_ITEM(pair, 0);                          /* :6575 */
class_name  = PyTuple_GET_ITEM(pair, 1);                          /* :6580 */
obj = find_class(self, module_name, class_name);                  /* :6586 */
```

`st->inverted_registry` **is** `copyreg._inverted_registry` (`:304-305`).
`object_vacall` (`Objects/call.c:823`) builds a *borrowed* vectorcall array, so
`find_class` never takes a reference either — both strings dangle inside it. This is a
**separate defect** from the previous one: fixing `find_class_impl`'s rebinding does not
help, because here the parameters arrive already dangling.

**The Python-reaching call is the very first statement of `find_class`.** ASan pinned
the free not to `PyImport_Import` but to `PySys_Audit("pickle.find_class", "OO",
module_name, global_name)` at `:7328-7329`: the audit args tuple INCREFs both borrowed
strings, the hook clears the registry (destroying `pair`), and then `sys_audit_tstate`
(`Python/sysmodule.c:358`) releases that tuple → `tuple_dealloc` → `unicode_dealloc`.
Both parameters are dead the instant `PySys_Audit` returns; `:7392` then INCREFs the
corpse. Two consequences: (a) the dangling window is the **whole function**, so a fix
that guards only the import region is incomplete — `load_extension` must hold the
reference across all of `:6586`; (b) **`sys.addaudithook` alone reaches it** — no
`__import__` override, no import side-effect, no `find_class` subclass. Audit hooks are
the security-monitoring mechanism, which makes this an unusually plausible trigger.

Notably, `copyreg.remove_extension()` is also **public documented API** that deletes
exactly this entry — so invalidating the borrow does not require adversarial code.

**Guarded twin.** `save_global:3919` — the pickler side of the symmetric table — uses
the strong-reference accessor `PyDict_GetItemRef(st->extension_registry, extension_key,
&code_obj)` and `Py_DECREF`s at `:3931`. `load_extension` uses the borrowing
`PyDict_GetItemWithError` for the mirror table.

**Precondition on reachability:** the application must have called
`copyreg.add_extension(...)`, since `_inverted_registry` is empty by default. Within
that precondition it is fully Python-reachable.

**Fix.** `Py_INCREF(pair)` around `:6586`, or switch `:6559` to `PyDict_GetItemRef`.

**Reproduction** (`repro_loadext.py` — 5-byte payload `b'\x80\x04\x82\xf0.'`
= PROTO 4 / EXT1 240 / STOP, plus a `sys.addaudithook` hook whose entire body is
`copyreg._inverted_registry.clear()`). **Independently re-run and confirmed:**

```
$ builds/debug-gil-nojit/python repro_loadext.py
about to loads()
Segmentation fault (core dumped)      EXIT=139

Program received signal SIGSEGV, Segmentation fault.
PyImport_ImportModuleLevelObject (name=0x7ffff74f2570, ...) at Python/import.c:4212
#1  builtin___import___impl (..., globals=0xdddddddddddddddd, ...)   <-- DEADBYTE fill
#7  PyImport_Import (module_name=0x7ffff74f2570)  at Python/import.c:4749
#8  _pickle_Unpickler_find_class_impl             at ./Modules/_pickle.c:7392
#14 find_class                                    at ./Modules/_pickle.c:5402
#15 load_extension (st=..., self=..., nbytes=1)   at ./Modules/_pickle.c:6586
```

ASan on the same script, which traps one step earlier on the `Py_INCREF` into freed
memory and gives the exact free chain:

```
==4087377==ERROR: AddressSanitizer: heap-use-after-free on address 0x78a56f114540
READ of size 4 at 0x78a56f114540 thread T0
    #0 Py_INCREF                        ./Include/refcount.h:286:31
    #1 do_mkvalue                        Python/modsupport.c:463:21
    #3 _Py_VaBuildStack                  Python/modsupport.c:586:11
    #6 PyImport_Import                   Python/import.c:4749:9
    #7 _pickle_Unpickler_find_class_impl ./Modules/_pickle.c:7392:14
   #11 PyObject_CallMethodObjArgs        Objects/call.c:960:24
freed by thread T0 here:
    #1 unicode_dealloc                   Objects/unicodeobject.c:1714:5
    #5 tuple_dealloc                     Objects/tupleobject.c:277:9
    #9 sys_audit_tstate                  ./Python/sysmodule.c:358:5
   #10 PySys_Audit                       ./Python/sysmodule.c:388:15
   #11 _pickle_Unpickler_find_class_impl ./Modules/_pickle.c:7328:9
previously allocated by thread T0 here:
    #4 unicode_join                      Objects/unicodeobject.c:12588:12
```

The allocation site is the reproducer's own `''.join([...])`, whose only owner was the
`copyreg._inverted_registry[240]` tuple — attribution is airtight end to end. The debug
build survives the `Py_INCREF` (the DEADBYTE-filled refcount is garbage-but-nonzero) and
faults slightly later at `Python/import.c:4212`; same root, two crash points.

### [CONSIDER] `Modules/_pickle.c:4689` — `self->dispatch_table` freed by a metaclass `__hash__` in `save()`

`PicklerObject.dispatch_table` is a writable **and deletable** `Py_T_OBJECT_EX` member
(`:5358`). At `:4689`,
`PyMapping_GetOptionalItem(self->dispatch_table, (PyObject *)type, &reduce_func)`
descends to `_PyObject_HashDictKey(key)` — the **metaclass `__hash__`** — *before*
`_PyDict_GetItemRef_KnownHash` dereferences the mapping. `del p.dispatch_table` from
inside that `__hash__` drops the pickler's only reference.

**Guarded twin, and the asymmetry is the finding:** `dump()` at `:4796`/`:4803` caches
`persistent_id` and `reducer_override` into *dedicated owned slots*, and
`Pickler_setattr` (`:5341-5348`) deliberately routes user assignment to
`persistent_id_attr` so a live hook cannot be swapped mid-dump. `dispatch_table` is the
one hook that skipped that treatment.

**Rated CONSIDER, not FIX**, because `PyDictObject` goes to a freelist, so the read
lands on recycled-but-mapped memory rather than faulting — no crash was observed and
none is claimed. What *was* observed (`repro_dispatch2.py`, `debug-gil-nojit`): with an
**empty** dispatch table, where a correct lookup must miss and fall through to
`__reduce_ex__`, the pickler instead picked up a reduce function out of a dict allocated
*after* the free, and called it:

```
$ builds/debug-gil-nojit/python repro_dispatch2.py
about to dump()
  !!! reduce_func came from the RECYCLED dict object !!!
done
```

That is an attacker-chosen reduce function selected out of freed memory — no memory
fault, but a real integrity failure.

### [FIX] `Modules/_struct.c:2436` / `:2102` / `:2274` — `Struct.__init__()` frees `s_codes` under an in-flight pack/unpack (NEW, scanner-missed)

`s_pack_internal:2371` and `s_unpack_internal:2062` walk `code = soself->s_codes` — a
raw `PyMem_Malloc` array, **not** a refcounted object. `e->pack(...)` at `:2429`
reaches `get_pylong:113` → `_PyNumber_Index:119` → a user `__index__`; the unpack side
reaches `PyErr_WarnEx(DeprecationWarning, …)` at `:2084`/`:2091` →
`warnings.showwarning`. Either can call `s.__init__("...")`, which routes to
`set_format` → `prepare_s:1803` `PyMem_Free(self->s_codes)` and installs a new array.
`code` then dangles at `:2436` (`res += code->size`).

**Why the "known-live owner" FP class does not apply — and this is the instructive
part.** The `PyStructObject` *is* alive; it is the bound-method receiver. That is
exactly what does not help, because `s_codes` is a raw malloc block which the live
owner explicitly frees. The taxonomy's rule is "a *strong reference* is provably held
for the duration"; a strong reference to the *container* says nothing about a raw
buffer hanging off it.

**Escalation beyond a UAF read:** the freed block is the same size class as the
replacement, so `code` silently retargets the *new* format's array while `buf` is
still sized for the *old* one — `res = buf + code->offset` becomes an
attacker-positioned write. `s_unpack_internal` additionally sized
`result = PyTuple_New(soself->s_len)` at `:2058` with the old `s_len`, so
`PyTuple_SET_ITEM(result, i++, v)` at `:2101` overruns the tuple.

**Guarded twin: none** — nothing in the file snapshots `s_codes`/`s_size`/`s_len` or
takes a re-entrancy guard. Re-init is deprecated (`FutureWarning` at `:1994`,
gh-143715/gh-94532) but live on main and not an error. A third face,
`unpackiter_iternext:2259`, re-reads `self->so->s_size`/`s_len` between two `next()`
calls with only a debug-only `assert` at `:2274`.

Reproduced (independently re-run by me, all `debug-gil-nojit`): `struct_uaf.py`
`EXIT=139`, `struct_oob.py` `EXIT=139`, `struct_unpack_uaf.py` `EXIT=139`,
`struct_iter_oob.py` `EXIT=134`. ASan (`release-gil-nojit-asan`):

```
==4072882==ERROR: AddressSanitizer: heap-use-after-free on address 0x77cf23ec6f10
    #0 s_pack_internal    Modules/_struct.c:2436      <- res += code->size
0x77cf23ec6f10 is located 16 bytes inside of 64-byte region  <- 2 * sizeof(formatcode)
freed by thread T0 here:
    #1 set_format         Modules/_struct.c:1803      <- PyMem_Free(self->s_codes)
    #2 s_init             Modules/_struct.c:1997
   #12 get_long           Modules/_struct.c:119       <- _PyNumber_Index -> __index__
   #14 s_pack_internal    Modules/_struct.c:2429      <- e->pack(...)
```
`struct_iter_oob.py` under ASan: `heap-buffer-overflow`, `READ of size 4`,
`2 bytes after 41-byte region`.

### [FIX] `Modules/_csv.c:953` — incomplete fix of gh-145105 (NEW, scanner-missed)

`Reader_iternext_lock_held:945` `lineobj = PyIter_Next(self->input_iter)` runs a user
`__next__`, which can re-enter and execute `:992-993`
(`fields = self->fields; self->fields = NULL;`). The **`lineobj == NULL` arm**
(`:946-957`) then calls `parse_save_field(self)` at `:953`, which does
`PyList_Append(self->fields /* NULL */, field)` at `:688`.

**Guarded twin: 15 lines below, in the same function.** `:967-972`
`if (self->fields == NULL) { … "iterator has already advanced the reader" … }` was
added by **`20994b1809f` (gh-145105, "Fix crash in csv.reader with re-entrant
iterator")** — I read the commit: it touches `Modules/_csv.c` in exactly 6 added lines
and adds `test_reader_reentrant_iterator`. The guard was placed only on the
`lineobj != NULL` path; the mutually exclusive `lineobj == NULL` path was left
unguarded, and the shipped regression test only exercises the guarded arm. This is an
incomplete fix, not a file convention.

Trigger: an inner re-entrant `__next__` that exits via the `break` at `:954` leaves
`self->fields == NULL` **and** `self->state == IN_QUOTED_FIELD` (the `break` skips the
`while (self->state != START_RECORD)` reset), so the outer frame satisfies
`!PyErr_Occurred() && self->state == IN_QUOTED_FIELD` at `:948-949` and, with
`strict` false by default, reaches `parse_save_field`.

Reproduced (re-run by me): `csv_reentrant.py` `EXIT=139`. ASan:
```
==4095922==ERROR: AddressSanitizer: SEGV on unknown address 0x000000000008
==4095922==Hint: address points to the zero page.
    #0 PyList_Append    Objects/listobject.c:541
    #1 parse_save_field Modules/_csv.c:688
    #2 Reader_iternext  Modules/_csv.c:953
```

### [FIX] `Modules/_zoneinfo.c:2436` — strong-cache node freed by a user `__eq__` (NEW, scanner-missed)

`find_in_strong_cache:2434` walks `node` — a bare `PyMem_Malloc` `StrongCacheNode`
(`:2357`) with no refcount — and compares with
`PyObject_RichCompareBool(key, node->key, Py_EQ)` at `:2436`, where `key` is the user's
`ZoneInfo(key)` argument. A user `__eq__` frees the node under comparison via
`ZoneInfo.clear_cache()` (`:523`→`:2583`), `clear_cache(only_keys=…)` (`:542`), or LRU
eviction from further `ZoneInfo(...)` constructions (`:2563-2567`). Uses after:
`:2444` `node = node->next`, `:2525` `move_strong_cache_node_to_front` (which *writes*
`node->prev`/`node->next` and reinstalls the freed pointer as the cache root at
`:2499`), `:2526` `Py_NewRef(node->zone)`, and — the double-free variant —
`eject_from_strong_cache:2466-2468` `remove_from_strong_cache(node); strong_cache_node_free(node);`.

**FP class ruled out:** there is no live owner to appeal to. The node is not a
`PyObject` and its sole owner is the module-state list, which three public APIs free.
The authors handled a *raising* `__eq__` (`:2437` returns NULL, `:2470` checks
`PyErr_Occurred`) but not a *re-entrant, cache-mutating* one. Direct precedent in this
file: `8307a14d0ed` (gh-142783) fixed the same shape on the **weak** cache.

Reproduced (re-run by me): `zi_a.py` `EXIT=139`, `zi_b.py` `EXIT=139`. ASan:
`heap-use-after-free`, `8 bytes inside of 32-byte region` (= `sizeof(StrongCacheNode)`),
read at `_zoneinfo.c:2412`, freed at `_zoneinfo.c:2377` (`PyMem_Free(node)` from
`clear_cache`).

### [FIX] `Modules/_zoneinfo.c:2583` — `clear_strong_cache` frees the chain while it is still published (NEW, scanner-missed)

```c
strong_cache_free(state->ZONEINFO_STRONG_CACHE);   /* :2583 frees every node ... */
state->ZONEINFO_STRONG_CACHE = NULL;               /* :2584 ... only then unpublishes */
```
`strong_cache_node_free:2374-2375` does `Py_XDECREF(node->key)` / `Py_XDECREF(node->zone)`
— arbitrary Python. Because `clear_cache` drops the weak cache first (`:518`), the node
holds the **last** reference to both, so a `str`-subclass key with `__del__` (or the
ZoneInfo instance's own dealloc chain) runs while `state->ZONEINFO_STRONG_CACHE` still
points into already-freed nodes; a re-entrant `ZoneInfo(...)` then walks that chain
(`:2520` → `:2434` → `:2436`).

**Guarded twin: twice in this same file, and I verified both.**
`eject_from_strong_cache:2466-2468` unlinks (`remove_from_strong_cache`) *before*
freeing; `update_strong_cache:2563-2567` detaches the tail (`node->prev->next = NULL`)
*before* `strong_cache_free`. `clear_strong_cache` is the only one of the three that
frees while published — a two-line reorder fixes it.

Reproduced (re-run by me): `zi_c.py` `EXIT=139`. ASan shows the read at `:2436`
(`RichCompareBool(key, node->key)`) reached *from* `zoneinfo_dealloc` inside
`clear_cache`, on memory freed at `:2377`.

## New siblings of known shapes

`batched_next` and `islice_next` are siblings of **CPY-0003** (`iter_iternext`,
`Objects/iterobject.c:80`) generalized one step: the invalidated thing is still a
slot the function cached, but the *use* is an indirect call rather than a
`Py_DECREF`, so the damage is a type-confused call instead of a refcount underflow.

`zip_longest_next_lock_held` is a sibling of **CPY-0003** in its literal form
(clear-slot-then-drop-stale-local), one level out: the slot is a tuple *element*
rather than a struct field.

Sibling sweep across `Modules/` + `Objects/` for the literal
`slot = NULL; Py_DECREF(stale)` form (script:
`scratchpad/hunt_stale_slot.py`) returned **12 sites, of which the two zip_longest
ones are the only true positives**; the rest are owner-swap, C-struct fields, or
constructor initialization. Sweep for the generalized
*cache-slot → Python call → clear-same-slot → reuse local* form
(`scratchpad/hunt_cached_slot_cleared.py`) returned **54 tree-wide**, correctly
re-deriving `batched_next` (`:196`/`:210`/`:224`), `islice_next`
(`:1701`/`:1711`/`:1732`) and CPY-0003 (`Objects/iterobject.c:61`/`:79`).

## Classes bounded (clean negatives, with evidence)

- **`Modules/_heapqmodule.c` — genuinely clean, empirically earned.** All four
  comparison sites (`siftdown:48`, `siftup:96`, `siftdown_max:430`, `siftup_max:478`)
  `Py_INCREF`/`Py_NewRef` **both** operands across `PyObject_RichCompareBool`,
  re-read `arr = _PyList_ITEMS(heap)` after the call (`:60`, `:102`, `:442`, `:484`),
  and check `size != PyList_GET_SIZE(heap)` *before* the next `arr` dereference.
  `heappushpop:275-290` INCREFs `top`, re-checks the size after the compare, and
  re-reads `returnitem` — the archetypal guarded form.
  `scratchpad/torture_heapq.py`: **800 adversarial calls** (10 entry points × 8
  mutation modes × 5 sizes incl. 2600 to hit `cache_friendly_heapify` × 2 re-entrancy
  budgets), where `__lt__` clears / pops / shrink-regrows / re-enters
  `heappush`/`heappop`/`heapify` / rewrites every slot / grows the heap.
  `debug-gil-nojit`: `ok, 800 adversarial calls, no crash`, exit 0.
  `release-gil-nojit-asan`: same, exit 0. Only `RuntimeError`/`IndexError`.
- **`Modules/_collectionsmodule.c` deque compare family — clean.**
  `deque_count:1173`, `deque_contains_lock_held:1208`, `deque_index:1305`,
  `deque_remove:1472` all use `item = Py_NewRef(b->data[index])` before the compare,
  and the `start_state != deque->state` sentinel check **dominates** the next
  `b = b->rightlink` dereference, so a freed block is never followed.
  `deque_richcompare:1700-1717` operates on owned `PyIter_Next` results and its
  `done:` label (`:1732`) correctly does not re-drop `x`/`y`.
- **`Modules/_elementtree.c` Element family — clean for this class.**
  `Element.find/findtext/findall` (`:1308`, `:1352`, `:1405`) and
  `Element.remove:1688` all `Py_INCREF(item)` + `Py_NewRef(tag)` across
  `PyObject_RichCompareBool` and re-validate `self->extra` and the bound afterwards
  (`:1707`). `elementiter_next:2259` (`elem = it->root_element; it->root_element =
  NULL;`) is an explicit ownership *transfer* — the comment says `/* steals a
  reference */` and every failure path `Py_DECREF(elem)`s. `:1091`
  (`oldextra = self->extra; self->extra = NULL;`) is deliberate re-entrancy hardening
  on a plain C struct, with the rationale in a comment.
  **CPY-0016 confirmed present, not re-litigated:** `create_elementiter:2377` still
  `Py_DECREF`s on the `PyMem_New` failure path with `parent_stack_used` uninitialized.
  (Different class — `scan_uninit_dealloc`'s.)
- **`Modules/itertoolsmodule.c`, the rest of the family — clean, and actively
  hardened.** `groupby_next:549-560` and `_grouper_next:678-685` carry explicit
  comments ("A user-defined `__eq__` can re-enter … Take local snapshots and hold
  strong references") and do exactly that. `teedataobject_getitem_lock_held:791`
  has a `tdo->running` re-entrancy guard that raises
  `RuntimeError("cannot re-enter the tee iterator")`. `tee_next` (FT arm, `:975-985`)
  snapshots `dataobj` with `Py_NewRef` under the lock and revalidates the advance.
  `cycle_next:1283` reads `lz->it` *inline* at the call site rather than caching it.
  `dropwhile`/`takewhile`/`filterfalse`/`starmap` cache `lz->it` but **nothing ever
  clears it while the object is alive**, so the cache cannot go stale.
  `product`/`combinations`/`cwr`/`permutations`: the `->result = NULL` /
  `->pool` writes at `:2139`, `:2379`, `:2631`, `:2896` are all *constructor*
  initialization, never mid-iteration clears.
- **`Python/bltinmodule.c:3267 zip_next` — clean (scope escape, checked).** Re-reads
  `it = PyTuple_GET_ITEM(lz->ittuple, i)` every turn and never NULLs a slot; it is
  the immune sibling of the `zip_longest` bug.
- **`Modules/_json.c` — clean, read in full.** Both historical instances of this
  shape are already fixed *with* an explicit `Py_INCREF` and a `gh-` comment:
  `_encoder_iterate_fast_seq_lock_held:1894-1897` and
  `_encoder_iterate_mapping_lock_held:1746-1749` (gh-142831),
  `_encoder_iterate_dict_lock_held:1778-1782` (gh-145244). Container size **is**
  re-read every iteration (`:1745`, `:1893`). `separator` borrowed from
  `indent_cache` (`get_item_separator:1434`) is safe because `indent_cache` is a
  private local list (`encoder_call:1466`) that only ever *grows*
  (`update_indent_cache:1411-1412`) and is never exposed to Python. All the
  `s->`/scanner hook fields are `Py_READONLY` with no setter.
- **`Modules/_queuemodule.c` — clean, read in full.** `RingBuf_Put:169` /
  `RingBuf_Get:149` re-read `buf->items` after any `resize_ringbuf`, which only calls
  `PyMem_Calloc`/`PyMem_Free`. `RingBuf_Fini:74-82` is the *correct* pattern: it
  snapshots and NULLs every struct field **before** the `Py_DECREF` loop, so a
  `__del__` re-entering `put()` sees an empty buffer.
- **`Modules/_randommodule.c` — clean, read in full.** `RandomObject.state` is a
  fixed-size `uint32_t state[N]` **inline in the struct**, so there is no heap pointer
  to invalidate. `PyObject_Hash:329` and `PyObject_CallOneArg:326` run Python but no
  borrowed pointer spans them.
- **`Modules/_csv.c` writer side — clean (only the reader arm is a finding).**
  `csv_writerow_lock_held:1330` holds `DialectObj *dialect = self->dialect` across
  `PyObject_GetIter:1334` / `PyIter_Next:1347` / `PyObject_Str:1381` — this **is** the
  legitimate "known-live owner" class: `self` is the bound-method receiver,
  `self->dialect` is write-once (`csv_writer:1591`) and `Py_READONLY` (`:1488`) on a
  `DISALLOW_INSTANTIATION` type, and every `DialectObj` field is `Py_READONLY` or a
  NULL-setter getset on an `IMMUTABLETYPE`. No mutation path exists.
- **`Modules/_zoneinfo.c` outside the two findings — clean.** `get_weak_cache:292`
  already returns a **strong** reference (the gh-142783 fix). `load_data:966` anchors
  every element pointer in the owned, `PyTuple_CheckExact`-verified `data_tuple`.
  `strong_cache_free:2386` itself caches `node->next` before freeing — the walker is
  correct; only `clear_strong_cache`'s ordering is not.
- **Cross-class, confirmed not re-litigated:** `Modules/_collectionsmodule.c:2237`
  `defdict_missing` still does the plain borrowed load `PyObject *factory =
  dd->default_factory;` before `_PyObject_CallNoArgs(factory)` — the gh-154527 shape,
  untouched since 2008 (`git log -S"dd->default_factory"`). Under the GIL there is no
  window (nothing runs Python between the load and the call, and the callee frame
  INCREFs `func`), so this is **FT-only** and belongs to `scan_ft_races`, not here.

---

## Toolkit assessment

### Precision per rule

| rule | fired (sample) | confirmed | precision |
|---|---|---|---|
| `borrowed_ref_across_call` | 2 | 2 | **100%** |
| `owner_freed_before_use` | 0 in sample (2 in `Modules/`, both `_testcapi`/`_testlimitedcapi`) | n/a | — |
| `potential_leak` / `_on_error` | 0 | — | — |
| `potential_double_free` | 0 | — | — |
| `stale_slot_decref` | 0 | — | — |
| `init_not_reinit_safe` / `new_missing_member_init` | 0 | — | — |

`borrowed_ref_across_call` is now **4/4 lifetime** (2 in `Objects/`, 2 here). The
rule is working and should not be loosened for precision reasons — it should be
*widened* for recall.

The two `owner_freed_before_use` hits outside the sample (`Modules/_testcapi/gc.c:108`
`slot_tp_del`, `Modules/_testlimitedcapi/slots.c:35` `demo_releasebuffer`) are in
test-support code that deliberately exercises the pattern; per the taxonomy's
"diagnostic and test-support code" entry they are ACCEPTABLE. Worth suppressing
`Modules/_testcapi*` / `Modules/_testlimitedcapi*` by default so the `Modules/`
headline number is 2, not 4.

### Are the two scanner candidates true positives *independently*?

**Yes, unambiguously.** `:3988` and `:4018` are in `zip_longest_next_lock_held`;
`count_nextlong` is a different function ~350 lines away with a different mechanism
(escape-by-return, not double-release) and a different trigger (a user `__radd__` on
`count`'s step, not a re-entrant `next()` on an exhausting source). Each of the two
candidates was reproduced by its own dedicated script, on two build configurations,
with no `count`/`__radd__` involved. **Rule precision on the sample: 2/2. Cumulative
since the v0.8 rewrite: 4/4.**

### Recall gaps — diagnosis verified against the scanner source

The coordinator supplied a confirmed miss (`count_nextlong:3633→3641`, ASan UAF) with
a three-point diagnosis. I read `scan_refcounts.py` to check it rather than accept it,
and **point 2 is incorrect**:

- **(1) correct.** `PyNumber_Add` is in `PYTHON_REACHING_APIS`, so the reaching table
  is not the cause.
- **(2) INCORRECT — struct-member loads are already a recognised borrowed source.**
  `BORROWED_GETTER_APIS` (`:218`) is only *one of two* load sources feeding
  `check_borrowed_ref_across_call`. The other is `_SLOT_LOAD_RE` (`:428-432`), whose
  optional declaration prefix matches `PyObject *result = lz->long_cnt;` exactly —
  groups `("result", "lz", "long_cnt")`, and the `m.group(1) != m.group(2)` guard at
  `:1085` compares *local vs owner*, so it does not reject it. Widening the borrowed
  source table is therefore **not** the fix, and doing so would be wasted work.
- **(3) correct in substance, and it is the *sole* root cause.** `:1093-1102`:
  ```python
  releases = [... _BARE_DECREF_RE ...] + [... _STEAL_CALL_RE ...]
  if not releases:
      continue
  ```
  The rule models exactly one hazard — *ownership released through the borrowed
  pointer*. `count_nextlong` **returns** `result`; `batched_next`/`islice_next`
  **call through** `it`. Neither is a release, so all three fall out at the same line.
  One change, not two.

So the real recall gaps are, in order:

**Gap 1 — the hazard set is release-only.** Two unmodelled hazards:
*escape* (`return local;` / `*out = local;` / `X->f = local;`) and *deref/call*
(`local->…`, `f(local)`).

**Gap 2 — the Python-reaching call may be an indirect slot dispatch.**
`iternext(it)` where `iternext = *Py_TYPE(it)->tp_iternext` (`:207`, `:1709`) is
invisible to a name-table lookup, as is the inline `(*Py_TYPE(it)->tp_iternext)(it)`
form, which appears **4 times** in `itertoolsmodule.c` alone (`:358`, `:371`, `:1536`,
`:1842`). The taxonomy already draws the right line under "Statically-known type slot"
(`PyUnicode_Type.tp_hash` is fine, `pb->bf_getbuffer` is not); the refcount scanner
just does not implement it. **Measured: without this, `batched_next` and `islice_next`
stay invisible even after Gap 1 is closed.**

**Gap 3 — loop-carried exposure.** In `batched_next` the borrowed local appears
*once* textually (`:210`); the danger is that iteration N+1's use follows iteration
N's call. A rule that only looks for a use positioned after a call misses it.
**Measured: without loop awareness, `batched_next` still stays invisible.**

**Gap 4 — a warning from my own prototyping.** My first sweep dropped both real hits
because I filtered `var == field`. `it = lz->it`, `seq = it->it_seq`,
`so = si->si_set` are the *commonest* spelling, precisely because CPython names the
local after the field. Do not add a "local must be named differently" heuristic.

**Gap 5 — borrowed *parameters* are not modelled at all.** The two `_pickle.c` UAFs
are a third sub-shape: the borrowed value is a **function parameter**
(`_pickle_Unpickler_find_class_impl(… module_name, global_name)`) or a value borrowed
from a Python-reachable **dict** and then rebound onto those parameters. There is no
struct slot and no release. Neither rule below reaches them; they were found only by
reading. Worth a separate rule keyed on *a parameter or dict-borrowed value used
after a Python-reaching call in a function whose caller passes it borrowed*, but I
have not measured that one and will not guess its volume.

### Measured volume of the proposed rules (coordinator request (b))

Prototyped in `scratchpad/proto_rules.py`, importing `scan_refcounts.py` and reusing
its own `_SLOT_LOAD_RE`, `python_reaching_calls`, `_REFCOUNT_ONLY_REACHING`,
`_reassigned_before`, `_block_end` and `find_functions`, so the numbers are directly
comparable. Swept **`Modules/` + `Objects/` + `Python/`**.

**Rule A — `slot_transfer_across_call`** (escape hazard).
`local = OWNER->FIELD` → Python-reaching call → `OWNER->FIELD = <new>` → `return
local` / store. Critical ordering gate: the overwrite must come **after** the call;
if it precedes the call the transfer completed while we were alone and the local is
the legitimate sole owner.

> **2 findings tree-wide. No flood.**
>
> | site | verdict |
> |---|---|
> | `Modules/itertoolsmodule.c:3641` `count_nextlong` | **TP** — reproduced (coordinator; re-run by me: `EXIT=139`, and the debug build prints `object type name: dict`, i.e. the freed counter was recycled as a dict → type confusion) |
> | `Objects/enumobject.c:196` `increment_longindex_lock_held` | **FP** — see new FP class below |

`increment_longindex_lock_held` is a *structural clone* of `count_nextlong`, comment
text and all ("We hold one reference to … we'll either return it or keep it in …"),
but it is **safe**: `en->one = _PyLong_GetOne()` (`Objects/enumobject.c:86`) and
`en_longindex` is only ever assigned from `PyLong_FromSsize_t` or from a previous
`long+long`, so `PyNumber_Add` dispatches to `long_add` and no user code runs.
`count`'s `long_step`, by contrast, is only asserted `PyLong_Check` in `fast_mode` —
and `count_nextlong` is the **slow** path, so `itertools.count(0, EvilStep())` puts an
arbitrary `__radd__` in that slot. That asymmetry is the entire difference.

**Rule B — `stale_slot_use`** (deref/call hazard).
`local = OWNER->FIELD` → Python-reaching call (including indirect slot dispatch, and
loop-carried) → `Py_CLEAR(OWNER->FIELD)` / `OWNER->FIELD = NULL` reachable later →
`local` dereferenced or called. Same ordering gate as Rule A.

> **3 findings tree-wide, 3/3 already-reproduced crashes. 100% precision.**
>
> | site | status |
> |---|---|
> | `Modules/itertoolsmodule.c:210` `batched_next` | **TP** — my ASan heap-use-after-free, exact line |
> | `Modules/itertoolsmodule.c:1711` `islice_next` | **TP** — my ASan heap-use-after-free, exact line |
> | `Objects/iterobject.c:80` `iter_iternext` | **TP** — CPY-0003, catalogued reproduced |

Two FPs that the ordering gate correctly suppresses, both worth recording:
`Modules/_tkinter.c:3023` `TimerHandler` (`v->func = NULL;` at `:3018` precedes
`PyObject_CallNoArgs` at `:3022` — transfer completed, local is sole owner) and
`Modules/_elementtree.c:2259` `elementiter_next` (`elem = it->root_element; /* steals
a reference */ it->root_element = NULL;` — explicit transfer, and `elem` is
reassigned at `:2270`, which `_reassigned_before` also catches).

An earlier, ungated draft of Rule B emitted 5; a hand-written, much cruder variant
(`scratchpad/hunt_cached_slot_cleared.py`) emitted 54. **The gating is what makes this
shippable**, and the three gates that do the work are: (i) suppress when the slot
clear precedes the first Python-reaching call, (ii) suppress when the local is
re-read from the slot after the call (`pairwise_next:364` — the guarded twin), (iii)
suppress on `_reassigned_before`.

### Answer to coordinator request (c): the same shape elsewhere in the sample

Rule A and Rule B, run over the whole tree, surface **nothing else in the 12-file
sample** beyond what is already in this report. Specifically, I checked the two places
the coordinator suggested:
- **the other `itertools` counters** — `count_next`'s fast path is pure `Py_ssize_t`
  arithmetic; `count_nextlong` is the only long-path escape. `repeat`, `cycle`,
  `accumulate` do not perform the transfer idiom (`accumulate` uses `Py_SETREF`).
- **`_pickle.c`'s memo machinery** — audited directly and found safe: `memo_get:1834`
  and `_Unpickler_MemoGet:1594` dereference the `Py_ssize_t *` before any call, and
  unpickler memo values go straight to `PDATA_APPEND` (INCREF + push) with no
  intervening call. `_pickle.c`'s real bugs are elsewhere and are Gap-5 shaped
  (borrowed parameters), not slot-transfer shaped.

### New false-positive class for `data/cpython_non_bugs.md`

> **Type-constrained operand makes a protocol call non-Python-reaching.**
> `PyNumber_Add` / `PyObject_RichCompare` / `PyObject_Hash` are in
> `PYTHON_REACHING_APIS`, but if *both* operands are provably of a concrete builtin
> type for the lifetime of the field, the dispatch resolves to a C slot and no user
> code runs. `Objects/enumobject.c:196` is the exemplar: `en->one` is
> `_PyLong_GetOne()` and `en_longindex` is only ever a `PyLong`, so the borrow across
> `PyNumber_Add` is safe. This is the numeric-protocol analogue of the existing
> "Statically-known type slot" entry. **Do not generalise it** — the sibling
> `count_nextlong` looks identical and *is* a bug, because its step field is
> deliberately untyped on the slow path.

### Prompt problems

- The prompt's guidance is **ahead of the shipped rule** ("lead with the shape, not
  the API"; "the container accessor is the narrow instance"). That mismatch is what
  produced the new findings — so it worked — but the prompt should state the shipped
  rule's actual boundary: *"`borrowed_ref_across_call` models one hazard only —
  ownership **released** through the borrowed pointer. Escapes (`return local`) and
  dereferences/calls of the borrowed local are **not** modelled and must be found by
  reading."* Struct-member sources *are* modelled; saying otherwise sends agents
  hunting the wrong gap.
- The Phase-3 (`tp_init`/`tp_new`) footnote is correctly de-emphasised; keep it.
- "A borrowed-ref finding you cannot reach from Python is CONSIDER, not FIX" is the
  right calibration and is what kept `dropwhile`/`takewhile` out of this report.

### Prompt problems

- The prompt's own guidance is **ahead of the shipped rule** ("lead with the shape,
  not the API"; "the container accessor is the narrow instance"; both calibration
  findings are non-container). An agent that follows the prompt hand-reads for a
  shape the script cannot corroborate. That mismatch is what produced the two new
  findings — so it worked — but the prompt should say so explicitly, e.g. *"the
  shipped `borrowed_ref_across_call` covers container accessors and `Py_DECREF` uses
  only; struct-field sources and call-receiver uses must be found by reading."*
- The Phase-3 (`tp_init`/`tp_new`) footnote is correctly de-emphasised and cost me
  nothing. Keep it.
- The prompt says a borrowed-ref finding you cannot reach from Python is CONSIDER,
  not FIX. That calibration is right and is what kept `dropwhile`/`takewhile` out
  of the report.

### Ranked tuning proposals

1. **Widen `borrowed_ref_across_call`'s borrowed-source set to struct-field loads,
   and its use set to call receivers.** Concretely, add the rule:
   *a local is assigned `OWNER->FIELD`; the same function contains
   `Py_CLEAR(OWNER->FIELD)` or `OWNER->FIELD = NULL` at a later line; the local is
   dereferenced, called, or DECREF'd after a Python-reaching call.* Emit
   `stale_slot_use` at **high** confidence when the use is a call/deref and
   **medium** when it is a `Py_DECREF`. Expected yield ~54 tree-wide.
   Known FP classes to gate out, all measured in this run:
   (a) ownership *transfer* out of the slot where every path treats the local as
   owned (`_elementtree.c:2259`, `_grouper_next:691`) — recognise the
   `read; slot = NULL; ... return local` / `... Py_DECREF(local)`-on-every-path
   shape; (b) non-`PyObject *` fields (`ElementObjectExtra *`, `char *`,
   `mmap` `self->data`); (c) the local is **re-read** from the slot after the call
   (`pairwise_next:364`) — this is the guarded twin and must suppress;
   (d) concrete-sequence `*_iternext` where nothing between the cache and the clear
   can run Python (`Objects/{list,tuple,bytes,unicode,bytearray}object.c`) — these
   are ~9 of the 54 and are true negatives.
2. **Teach the Python-reaching-call detector about slot dispatch off a runtime
   object.** Match `(*Py_TYPE(x)->tp_SLOT)(...)`, `x->ob_type->tp_SLOT(...)`, and a
   local function pointer assigned from `*Py_TYPE(x)->tp_SLOT` and later called, for
   `tp_iternext`/`tp_call`/`tp_richcompare`/`tp_hash`/`tp_repr`/`tp_str`/
   `tp_getattro`/`tp_descr_get`/`bf_getbuffer`. Keep the taxonomy's existing
   exemption for a *statically named* type (`PyUnicode_Type.tp_hash`). This alone
   closes Gap 3 and is shared infrastructure with `scan_pyerr_clear`, which already
   needs the same distinction.
3. **Treat `#ifndef Py_GIL_DISABLED` / `#ifdef Py_GIL_DISABLED` as a
   *configuration*, not as a reason to suppress.** The existing taxonomy entry
   ("Mutually exclusive preprocessor branches … Never reason across a
   `#if`/`#else`/`#endif` boundary") is right about *cross-arm* reasoning and would,
   applied bluntly, have suppressed `batched_next` — where the clear and the unsafe
   use are in the *same* arm and the other arm is the fix. Proposal: analyse each
   arm as its own program, report per-configuration (`gil-only`, `ft-only`, `both`),
   and specifically flag *asymmetry* — one arm guards, the other does not — as a
   promotion signal rather than a suppression signal. That asymmetry is the single
   highest-value pattern this run produced.

4. **A raw (non-`PyObject`) buffer hanging off a live object is not protected by its
   owner.** `_struct.c`'s `s_codes`, `_zoneinfo.c`'s `StrongCacheNode` chain and
   `_elementtree.c`'s `extra` are `PyMem_Malloc` blocks with no refcount, freed by
   ordinary methods on a *live* receiver. Three of this run's eight new findings are
   this shape, and the current taxonomy entry ("borrowed ref under a known-live
   owner") actively argues for dismissing them. Add the carve-out to
   `data/cpython_non_bugs.md` and consider a rule: *a `PyMem_Malloc`-backed struct
   field cached into a local across a Python-reaching call, where some function in
   the file `PyMem_Free`s that field.*

Lower priority: 5. suppress `Modules/_testcapi*`/`_testlimitedcapi*` from the default
`Modules/` scan; 6. the finding `detail` string should name the borrowed *source
line* as well as the api_call — I had to re-derive `:3974`/`:4004` by hand from a
finding that only cited `:3988`/`:4018`.

---

## Scripts and repros

All under `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`.

Reproducers (every one re-run by me on `debug-gil-nojit`, exit code recorded above):
`repro_ziplongest.py`, `repro_ziplongest_else.py`, `repro_batched.py`,
`repro_islice.py`, `repro_count_nextlong.py`, `repro_findclass.py`,
`repro_findclass_nofill.py`, `repro_loadext.py`, `repro_dispatch2.py`,
`struct_uaf.py`, `struct_oob.py`, `struct_unpack_uaf.py`, `struct_iter_oob.py`,
`csv_reentrant.py`, `zi_a.py`, `zi_b.py`, `zi_c.py`.

Negative evidence: `torture_heapq.py` (800 adversarial calls, clean on debug + ASan).

Rule prototypes: **`proto_rules.py`** — the measured Rule A / Rule B, importing
`scan_refcounts.py` and reusing its own regexes and gates; this is the artifact to
port into the scanner. Superseded exploratory sweeps: `hunt_stale_slot.py`,
`hunt_cached_slot_cleared.py`.
