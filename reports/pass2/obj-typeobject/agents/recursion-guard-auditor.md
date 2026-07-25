# Recursion-Guard Analysis Results — slice `obj-typeobject`, **PASS 2**

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), scope `Objects/typeobject.c` (13,068 lines)
plus its one new external caller `Objects/dictobject.c:1971`.
**Interpreters:** `~/projects/python_build_matrix/builds/{debug-gil-nojit, debug-ft-nojit, release-gil-nojit, debug-gil-nojit-asan}/python` @ `a1d580430c8`.

`git diff a1d580430c8 4f3be1b5777 -- Objects/typeobject.c Objects/dictobject.c Objects/abstract.c Include/internal/pycore_typeobject.h Modules/_testcapi/heaptype.c`
is **empty** — every file cited below is byte-identical between the build commit and the target ref, so ASan frame line numbers and target-ref line numbers agree exactly. (The ASan build reports its own checkout path, `builds/debug-gil-nojit-asan/Objects/typeobject.c`; same content, same line numbers.)

Pass 1's report (`reports/obj-typeobject/agents/recursion-guard-auditor.md`) is **not** redone. Everything it recorded is confirmed in one line. Two of its verdicts are **overturned** and one finding is **extended** — both flagged as such.

---

## Summary

- Recursion-prone descents scanned: **8** (`missing_recursion_guard` 8, guarded-by-dispatcher 0) over
  denominators `functions_analyzed=459`, `recursion_prone_slot_functions=11`,
  `large_cycles_not_reported=1`, `cycle_reporting_cap=3`.
- Ground truth by independent tree-sitter sweep (`scratchpad/p2/recsweep.py`, 416 function definitions):
  **7 self-recursive functions, 2 multi-function SCCs** (size 2 and size 14).
- **FIX: 1** (`merge_class_dict` = CPY-0071, reconfirmed on the default stack, debug + release).
- **CONSIDER: 7** — the class-hierarchy descent family. **2 are net new to pass 2**
  (`solid_base` :3776, `get_base_by_token_recursive` :6002), 5 are CPY-0087 confirmed.
  One of the five (`_PyType_Modified_Unlocked` :1206) is **materially widened** and gets its own section.
- **POLICY: 1** — a recursive descent, watcher callbacks and `PyErr_FormatUnraisable("%R")` now run
  inside `LOCK_KEYS` (a `_Py_LOCK_DONT_DETACH` mutex) + the types-mutex critical section. Handed to
  **lock-discipline-checker / ft-race-scanner**, not litigated here.
- **ACCEPTABLE: 7** (bound stated for each), including the whole MRO C3 region and the 14-member SCC.

**The structural fact is unchanged from pass 1 and now measured against the whole file:**
`Objects/typeobject.c` still contains **zero** recursion guards, and the tree-sitter sweep says
**8 of the 9 recursion structures in it descend a Python-mutable object graph**. The ninth (the
14-member SCC) is the only one that is guard-broken, and by a reveal flag, not by a recursion guard.

```
$ grep -c "Py_EnterRecursiveCall\|_Py_EnterRecursiveCallTstate\|Py_ReprEnter" Objects/typeobject.c
0
```

**Prior art: none.** `gh api -X GET search/issues` for `get_base_by_token_recursive`, `solid_base`,
`type_modified_unlocked recursion stack overflow`, `tp_subclasses recursion segfault`,
`deep inheritance chain segfault`, `C stack overflow uncatchable RecursionError`,
`Py_EnterRecursiveCall typeobject` returns nothing on point. This matches the history agent's
result that the pass-2 *recursion* fix cluster is empty (1 commit, 2001, not a fix) — read as
**never tested**, not as safe.

---

## Part 1 — the net-new finding the brief asked for

### [CONSIDER — net new, overturns a pass-1 ACCEPTABLE] `get_base_by_token_recursive` descends `tp_bases` with no guard (Objects/typeobject.c:5987, recursion at :6002)

**What.** `get_base_by_token_recursive(PyObject *bases, void *token)` walks the base *graph* looking
for a heap type whose `ht_token` matches, recursing once per base:

```c
        base = get_base_by_token_recursive(lookup_tp_bases(base), token);   /* :6002 */
```

No guard, no depth counter, `void`-free signature that would happily take one. The recursive argument
is `lookup_tp_bases(base)` — the file's own `tp_bases` accessor — so the depth is exactly the depth of
a Python-built inheritance chain.

**Pass 1 called this ACCEPTABLE, "bound 0 for any Python-constructible type". That is wrong, and this
is the correction.** Its reasoning was that the only caller reaches it exclusively on the
`type->tp_mro == NULL` branch of `PyType_GetBaseByToken_DuringGC` (`:6037-6048`), and that a completed
hierarchy always has a non-NULL `tp_mro`. The premise about the call site is right; the conclusion is
not, on three counts:

1. **`tp_mro == NULL` is an advertised, supported state for this API, not an impossible one.**
   `Modules/_testcapi/heaptype.c:482-488` says so in the tree, in a comment, and then does it:
   > `// Test internal detail: PyType_GetBaseByToken works even with`
   > `// types that are only partially initialized (or torn down):`
   > `// if tp_mro=NULL we fall back to tp_bases.`

   and `type->tp_mro = NULL; … PyType_GetBaseByToken(type, token, …)`.
2. **`type_clear` manufactures exactly that state on every GC'd heap type.**
   `Objects/typeobject.c:7276` is `Py_CLEAR(type->tp_mro);`, and the 25-line comment above it
   (`:7238-7266`) *deliberately* leaves `tp_bases` and `tp_base` in place: *"tp_bases, tp_base: if
   these are involved in a cycle, there must be at least one other, mutable object in the cycle …"*.
   A GC-cleared class is therefore a live `PyTypeObject` with `tp_mro == NULL` and a full `tp_bases`
   chain — the precise input this function is unguarded against. The `_DuringGC` variants exist
   because in-tree consumers (`Modules/_ctypes/ctypes.h:621`, `Modules/xxlimited.c:173`) call it from
   `tp_traverse` / teardown paths.
3. **`PyType_GetBaseByToken` is public stable-ABI** (`Include/object.h:364`, `PC/python3dll.c:698`,
   `Lib/test/test_stable_abi_ctypes.py:760`) and the non-`_DuringGC` in-tree callers pass
   `Py_TYPE(operand)` from ordinary Python operations — `Modules/_decimal/_decimal.c:150/152/165/167/169`
   (Decimal binary ops), `Modules/_collectionsmodule.c:2417` (`defaultdict.__or__`).

**Guarded twin.** There is none in this file — it has zero guards. The twin is the same one pass 1
named for `merge_class_dict`: `abstract_issubclass()` (`Objects/abstract.c:2571`), which walks
`__bases__` and *does* `_Py_EnterRecursiveCall(" in __issubclass__")`, added by bpo-30570 / GH-29048
for gh-74755 — the identical segfault in the identical shape. The nearest sibling *inside* this file
is `_PyType_Modified_Unlocked` (:1206), which is CPY-0087 — same shape, opposite direction of travel
(down `tp_subclasses` instead of up `tp_bases`).

**Blame (from the pass-2 history agent).** `:6002` is blamed to `120b891e4dff` 2024-10-10,
*"simplify `PyType_GetBaseByToken`"* — a simplification, not a recursion audit. gh-124153's two
follow-ups (`124488`, `124323`) were both about *unstable optimization / performance*.

**Reachability, stated honestly.** Depth is the `tp_bases` chain length; the precondition is
`tp_mro == NULL` on the entry type. In-tree the deterministic trigger is `_testcapi`:

```python
C = type("C0", (object,), {})
for i in range(1, 3000):
    C = type("C%d" % i, (C,), {})
_testcapi.pytype_getbasebytoken(C, 1, False, False)   # use_mro=False -> tp_mro=NULL
```

run on a `threading.stack_size(100*1024)` worker. The non-test route (a GC-cleared class reaching a
`_ctypes` / `_decimal` / `_collections` caller during `delete_garbage`) is real by construction but I
did **not** drive it end to end — that is the only thing keeping this at CONSIDER rather than FIX.

**Confirmed.** `scratchpad/p2/token_recurse.py`:

| build | depth / thread stack | result |
|---|---|---|
| `debug-gil-nojit` | 800 / 100 KB | survives (control) |
| `debug-gil-nojit` | 1500 / 100 KB | **SIGSEGV, exit 139** |
| `debug-gil-nojit` | 3000 / 100 KB | **SIGSEGV, exit 139** |
| `release-gil-nojit` | 1500 / 100 KB | survives (control) |
| `release-gil-nojit` | 3000 / 100 KB | **SIGSEGV, exit 139** |
| `release-gil-nojit` | 6000 / 100 KB | **SIGSEGV, exit 139** |
| `release-gil-nojit` | 3000 / 512 KB | survives (control — it is the stack, not the data) |
| `debug-gil-nojit-asan` | 6000 / 256 KB | **`AddressSanitizer: stack-overflow`, exit 1** |

ASan frames are uniform and unambiguous — the descent is the crashing frame, nothing else:

```
==1318461==ERROR: AddressSanitizer: stack-overflow on address 0x73d728045ff8 ... T1
    #0  get_base_by_token_recursive  Objects/typeobject.c:6002:16
    #1  get_base_by_token_recursive  Objects/typeobject.c:6002:16
    #2  get_base_by_token_recursive  Objects/typeobject.c:6002:16
    ... (repeats to #25)
==1318461==ABORTING
```

Note the debug build did **not** hide this — it crashed at a *lower* depth than release (1500 vs 3000),
i.e. the larger debug frame makes it easier, not harder. The CPY-0049 caveat does not apply here.

**Impact.** Uncatchable native C-stack overflow → SIGSEGV, not a catchable `RecursionError`, on a
stable-ABI entry point whose documented contract explicitly includes the unguarded branch.

**Fix.** One `Py_EnterRecursiveCall(" while resolving a type token")` /
`Py_LeaveRecursiveCall()` pair around the loop, or an explicit depth parameter — the function already
returns `PyTypeObject *`, so a `NULL` return on overflow costs nothing. It should ride along with the
CPY-0087 family fix, not be filed separately.

---

## Part 2 — `_PyType_Modified_Unlocked:1206`: CONFIRM AND EXTEND

**Confirm (one line, per informed-mode rule 1).** `Objects/typeobject.c:1206` is CPY-0087: the
`tp_subclasses` self-recursion is still present, still unguarded, still ASan-reproducible.

**Extend — and this is the pass-2 result.** The history agent established that gh-150490 "take 2"
(`20921922fa4`, 2026-07-06, 19 days before HEAD) renamed `static void type_modified_unlocked` to the
exported `void _PyType_Modified_Unlocked` (`Include/internal/pycore_typeobject.h:152`) and gave it one
external caller: `Objects/dictobject.c:1971`, inside `insert_split_key`:

```c
    LOCK_KEYS(keys);
    ix = unicodekeys_lookup_unicode(keys, key, hash);
    if (ix == DKIX_EMPTY && keys->dk_usable > 0) {
        ...
        PyTypeObject *type = FT_ATOMIC_LOAD_PTR_ACQUIRE(shared_keys->dsk_owning_type);
        if (type) {
            // we acquired the type lock above
            _PyType_Modified_Unlocked(type);          /* dictobject.c:1971 */
        }
```

**The depth is not bounded there.** `insert_split_key` passes `dsk_owning_type` — the class that owns
the shared keys — and `_PyType_Modified_Unlocked` then descends *that class's entire subclass tree*.
Nothing in the dict path bounds it; the `#ifdef Py_GIL_DISABLED` at `dictobject.c:1948` gates only the
critical section, not the call, so **both the GIL and the free-threaded build take this path**.

**It widens CPY-0087, and the widening is the interesting part: the trigger drops from class mutation
to ordinary instance attribute assignment.** Before this commit, the descent was reachable from
`type_setattro`, `__bases__` assignment, `__abstractmethods__`, ABC registration and the watcher API —
all operations on a *class*. `insert_split_key` fires on `obj.newattr = value` for a name not yet in
the owning class's shared keys. That is the single most common write in Python.

**Confirmed, with an isolating control.** `scratchpad/p2/dict_split_modified.py` builds a chain of
`depth` classes, populates each class's split keys, primes every `tp_version_tag` with pure reads
(the descent short-circuits at `:1189` when the tag is already 0), and then does
`insts[0].zzz = 1` on a `threading.stack_size(100*1024)` worker:

| build | depth / stack | result |
|---|---|---|
| `debug-gil-nojit` | 1500 / 100 KB | **SIGSEGV, exit 139** |
| `debug-ft-nojit` | 1500 / 100 KB | **SIGSEGV, exit 139** |
| `debug-gil-nojit` | 3000 / 100 KB | **SIGSEGV, exit 139** |
| `debug-ft-nojit` | 3000 / 100 KB | **SIGSEGV, exit 139** |
| `release-gil-nojit` | 3000 / 100 KB | **SIGSEGV, exit 139** |

`scratchpad/p2/dict_split_control.py` is byte-for-byte the same script plus one extra
`insts[0].yyy = 1` executed on the 8 MB main thread first, which drains every version tag in the chain
so `_PyType_Modified_Unlocked` returns at `:1189` before the `:1206` descent:

| build | depth / stack | result |
|---|---|---|
| `debug-gil-nojit` | 3000 / 100 KB | **survives, exit 0** |
| `release-gil-nojit` | 3000 / 100 KB | **survives, exit 0** |

Same data, same stack, same statement — the only difference is whether the `:1206` descent runs. That
isolates the crashing frame to it without needing a symbolizer.

**Verdict.** CPY-0087 stands as recorded; its *trigger set* should be amended to include
`Objects/dictobject.c:1971` / plain instance attribute assignment, and its priority raised
accordingly. The guarded twin remains `abstract_issubclass` (`Objects/abstract.c:2571`).

### [POLICY — hand-off, not litigated here] the descent now runs inside two locks

Per the agent contract ("if a recursive descent runs *inside* a critical section, that is a second,
separate finding"), recorded and handed to **lock-discipline-checker** and **ft-race-scanner**:

On the free-threaded build, `dictobject.c:1971` calls `_PyType_Modified_Unlocked` while holding
**both** `Py_BEGIN_CRITICAL_SECTION_MUTEX(&interp->types.mutex)` (`dictobject.c:1958`) and
`LOCK_KEYS(keys)`, which is `PyMutex_LockFlags(&keys->dk_mutex, _Py_LOCK_DONT_DETACH)`
(`dictobject.c:227`) — a mutex the holder is explicitly forbidden to detach from. Inside that window
`_PyType_Modified_Unlocked` runs, at `:1222-:1226`, **arbitrary type-watcher callbacks** and
`PyErr_FormatUnraisable("Exception ignored in type watcher callback #%d for %R", i, type)`, whose
`%R` invokes `repr(type)` and therefore a metaclass `__repr__` — arbitrary Python. The function's own
comment at `:1219-1220` concedes both are re-entrant. Re-entering any split-dict insertion on the same
keys object from there meets a non-detaching mutex the same thread holds.

This is the third attempt at gh-150490; the first deadlocked (gh-151593) and the second's fix
introduced *"a data-race … triggered with pure Python code"* (gh-152238). The lock-discipline and
ft-race agents were already pointed at this by the history agent; this section only adds that the
unguarded **recursion** and the **arbitrary-Python-under-two-locks** hazard are the same window.

---

## Part 3 — MRO C3 linearisation (3217–3702), read by hand

Pass 1 never read this region. Read in full for pass 2. **Result: a clean negative, with the bound.**

- **`pmerge` (:3361) is iterative.** The C3 merge is a `goto again` loop over a flat
  `to_merge` array of MRO tuples with a `remain[]` index vector (`PyMem_New` at :3371,
  `PyMem_Free` at :3425). Zero C recursion regardless of hierarchy shape. **Bound 0.**
- **`mro_implementation_unlocked` (:3431) is iterative.** Two flat loops over `lookup_tp_bases(type)`
  plus the `n == 1` fast path. **Bound 0** for C recursion. (Its `:3443` borrowed-`bases`-across-
  `class_name` candidate is a `borrowed_field_deref_across_call` question, not mine.)
- **`mro_invoke` (:3591) re-enters through the eval loop, which is guarded.** The custom branch calls
  `call_method_noarg((PyObject *)type, &_Py_ID(mro))` (:3603) — a Python call, so a custom `mro()`
  that creates another class recurses through `_PyEval_EvalFrameDefault`'s
  `_Py_EnterRecursiveCallTstate` and degrades to `RecursionError`. Pass 1 verified the analogous
  `__init_subclass__` / `__set_name__` re-entries empirically; the mechanism is identical.
- **`mro_internal` (:3655) / `type_mro_modified` (:1279) / `is_subtype_with_mro` are flat.**
  `mro_internal`'s `Py_XNewRef(lookup_tp_mro(type))` / `Py_XDECREF` pair at :3665/:3668 is the
  documented ABA defence for the `reent` pointer-identity check (:3662-3664), not a leak, and the
  ownership transferred at :3696 is the type's own reference — balanced.
- **`mro_check` (:3547)** calls `solid_base` twice per MRO entry. That is where the region's one real
  recursion lives — see below. `mro_check` itself is a flat loop.
- **`__mro_entries__` is resolved in `Python/bltinmodule.c:47` in one flat pass**, and typeobject.c
  touches it only at `:5013` (`PyObject_HasAttrWithError`). No descent.

**But the region does contain one unguarded recursion, and pass 1 dismissed it on a claim the
compiler contradicts:**

### [CONSIDER — net new, overturns a pass-1 ACCEPTABLE] `solid_base` self-recurses down `tp_base` (Objects/typeobject.c:3770, recursion at :3776)

```c
static PyTypeObject *
solid_base(PyTypeObject *type)
{
    PyTypeObject *base;
    if (type->tp_base) {
        base = solid_base(type->tp_base);      /* :3776 */
    }
    ...
    if (shape_differs(type, base)) { return type; }   /* work AFTER the call */
```

**Pass 1 recorded: "the call is in tail position and clang turns it into a jump … Bound:
compiler-dependent; not reproducible here." Both halves are false.** The call at `:3776` is not in
tail position — `shape_differs(type, base)` consumes its result — and `objdump -d` shows a real
recursive `call` in **both** builds, with a 32-byte frame (`push %rbp; push %rbx; push %rax` + return
address):

```
debug-gil-nojit   21e790 <solid_base>:  ...  21e7a5:  call  21e790 <solid_base>
release-gil-nojit 185a70 <solid_base>:  ...  185a85:  call  185a70 <solid_base>
```

32 bytes/frame is simply small: 3,000 frames is 96 KB, which is why pass 1's 128 KB probe survived by
1.3×. It is not an optimization, it is a rounding error.

**Reachability — the most ordinary trigger in the whole family.** `find_best_base` (:3737) calls
`solid_base(base_i)` for **every base of every class ever created**, so `class X(Deep): pass` is the
trigger. It is also reached from `mro_check` (:3552, :3564) on the custom-`mro()` path and from
`type_new`. No `_testcapi`, no metaclass, no primed cache, no `tp_mro == NULL` precondition.

**Confirmed, with an attribution control.** `scratchpad/p2/solid_base_probe.py` builds a chain of
`depth` classes, primes every `tp_version_tag` (so `assign_version_tag` :1431 returns at :1417 after
one frame) and then evaluates `type("X", (C_deep,), {})` on a 100 KB worker. The new class has no
subclasses, so `_PyType_Modified_Unlocked` :1206 is one frame; the MRO build is iterative. The only
`depth`-deep descent left is `solid_base`.

| script | build | depth / stack | result |
|---|---|---|---|
| `solid_base_probe.py` | `debug-gil-nojit` | 2000 / 100 KB | survives (64 KB of frames) |
| `solid_base_probe.py` | `debug-gil-nojit` | 4000 / 100 KB | **SIGSEGV, exit 139** |
| `solid_base_probe.py` | `release-gil-nojit` | 4000 / 100 KB | **SIGSEGV, exit 139** |
| `solid_base_probe.py` | `debug-gil-nojit` | 4000 / 512 KB | survives, exit 0 |
| **`solid_base_control.py`** | `debug-gil-nojit` | 4000 / 100 KB | **survives, exit 0** |
| **`solid_base_control.py`** | `release-gil-nojit` | 4000 / 100 KB | **survives, exit 0** |

`solid_base_control.py` is identical — same 4,000-class chain, same priming, same `type()` call —
except the new class derives from `classes[0]` instead of the deep end, so only the `tp_base` walk
loses its depth. Deep base crashes, shallow base survives: the crashing descent is `find_best_base`
→ `solid_base`.

**Guarded twin / fix.** Same as the family: no twin in this file, `abstract_issubclass`
(`Objects/abstract.c:2571`) outside it. `solid_base` returns `PyTypeObject *` and every caller already
handles a failure path, so `Py_EnterRecursiveCall` fits without a signature change — unlike the
`void`-returning members of the family, which is presumably why none of them got one.

**Why CONSIDER and not FIX.** Same arithmetic as the rest of the family: 32 bytes/frame means the
8 MB main-thread stack needs ~260,000 classes, and a chain of *N* classes costs ~4·N² bytes of MRO
tuples (≈ 270 GB at that N). It is trivially reachable on a worker thread using only the documented
minimum `threading.stack_size(102400)`.

---

## Part 4 — sweep of the pass-2 regions for the class

An independent tree-sitter sweep of all 416 function definitions (`scratchpad/p2/recsweep.py`,
guard-agnostic and vocabulary-agnostic, so it measures the scanner rather than agreeing with it):

**Self-recursive: 7 — every one unguarded.**

| function | def | recursive call | walks | verdict |
|---|---|---|---|---|
| `_PyType_Modified_Unlocked` | :1165 | **:1206** | `tp_subclasses` | CPY-0087 — confirmed **+ widened** (Part 2) |
| `assign_version_tag` | :1407 | **:1431** | `tp_bases` | CPY-0087 — confirmed, ASan-reproduced in pass 1 |
| `mro_hierarchy_for_complete_type` | :1797 | **:1854** | `tp_subclasses` | CPY-0087 — confirmed |
| **`solid_base`** | :3770 | **:3776** | `tp_base` | **net new, reproduced** (Part 3) |
| **`get_base_by_token_recursive`** | :5987 | **:6002** | `tp_bases` | **net new, reproduced** (Part 1) |
| `set_flags_recursive` | :6482 | **:6500** | `tp_subclasses` | CPY-0087 — confirmed, still unreproduced |
| `merge_class_dict` | :7077 | **:7117** | `__bases__` | **CPY-0071 — FIX**, reconfirmed below |

**Multi-function cycles: 2.**

| SCC | members | verdict |
|---|---|---|
| size 2 | `update_subclasses` :12359 ↔ `recurse_down_subclasses` :12397 | CPY-0087 — confirmed |
| size 14 | `PyType_Ready` / `type_ready` / `type_ready_set_base` / `type_ready_mro` / `mro_internal` / `mro_invoke` / `mro_implementation_unlocked` / `call_method_noarg` / `lookup_method` / `lookup_method_ex` / `_PyType_LookupStackRefAndVersion` / `find_name_in_mro` / `type_mro_modified` / `has_custom_mro` | **ACCEPTABLE — guard-broken, see below** |

**CPY-0071 reconfirmation, one line as required:** `scratchpad/p2/r25_r26_probe.py a` (pass 1's
cyclic-`__bases__`-through-a-metaclass-`__bases__`-property `dir()`) still **SIGSEGVs, exit 139, on
the default 8 MB main-thread stack, on both `debug-gil-nojit` and `release-gil-nojit`**. Unchanged, no
depth needed, still the strongest form of the class in this file.

### The 14-member SCC is not a live recursion driver — stated, because the cap suppressed it

The scanner logged it under `large_cycles_not_reported` rather than reporting it. That was the right
call, and it is worth writing down *why*, because a silently dropped cycle would otherwise be an
unaudited hole:

The load-bearing edge is `find_name_in_mro → PyType_Ready` (:6157) and it is conditioned on
`if (!is_readying(type))` (:6156). `type_ready` (:9532-9533) asserts `!is_readying(type)` and then
`start_readying(type)`, which sets `Py_TPFLAGS_READYING` for the whole of `type_ready` — including
`type_ready_mro` → `mro_internal` → `mro_invoke` → `call_method_noarg` → … → `find_name_in_mro`.
So the cycle **cannot close on the same type**: the second trip finds `is_readying(type)` true and
returns −1 at :6164. It could only close on a *different* type each trip, i.e. on a chain of distinct
**not-yet-ready** types, via `type_ready_set_base → PyType_Ready(base)` (:9073). Every Python-created
class is readied at creation with all its bases already ready, so that chain has length 0 from Python;
for static/extension types its length is fixed by C source. **Bound: the number of distinct un-ready
types in a base chain — 0 for anything Python can build.**

### Other pass-2 regions: clean negatives, with the bound

- **R19 `find_name_in_mro` :6147** — `_PyObject_HashDictKey(name)`, the file's only call into the
  unguarded `PyObject_Hash` dispatcher (`grep` confirms: `:6147` is the sole hash-dispatcher call site
  in 13,068 lines; the other seven `PyObject_HashNotImplemented` hits are slot-table plumbing). Every
  caller `PyUnicode_Check`s `name` first. **Bound 0** for exact `str`; a `str` subclass with a Python
  `__hash__` goes through the eval loop, which is guarded. Pass 1 verified this empirically —
  confirmed, not re-litigated.
- **R25 ↔ R26, the joint historical segfault** — the comment at `:8318-8332` records that *"Pickling
  objects that lied about their type by overwriting the `__class__` descriptor could lead to infinite
  recursion at C level and eventual segfault"*, fixed structurally by splitting `_common_reduce` out
  of `object.__reduce__` / `object.__reduce_ex__`. Probed with a mutually-lying pair of `__class__`
  properties across protocols 0/1/2/5 plus `copy.copy` (`r25_r26_probe.py b`): **survives, exit 0, on
  debug and release**. The split still holds. **Bound 0.**
- **R25 `compatible_for_assignment` :7683-7686** — walks up `tp_base` with
  `while (compatible_with_tp_base(newbase)) newbase = newbase->tp_base;`, i.e. **iteratively**. Same
  graph as `solid_base`, zero stack. **Bound 0.** A useful contrast: the file already contains the
  iterative spelling of the exact walk `solid_base` does recursively 50 lines away.
- **R37 `supercheck` :12703 / `do_super_lookup` :12637 / `super_getattro` :12688** — linear scans of
  the `tp_mro` tuple. `supercheck` honours a lying `__class__` via `PyObject_GetOptionalAttr`
  (:12733) — one hop, then `PyType_IsSubtype`, which is iterative. Probed (`r25_r26_probe.py c`):
  **survives, exit 0, debug and release**. **Bound 0** for recursion. (The `su->type` /
  `su->obj_type` borrowed-across-call question is the refcount agent's; see the include map §4.3.)
- **R6 `type_mro_modified` :1279 → `has_custom_mro` :1261 → two `_PyType_LookupStackRefAndVersion`
  lookups** — part of the 14-SCC above, flat in itself. **Bound 0.**
- **R26 `object_getstate_default` :7923, `reduce_newobj` :8222, `_PyType_GetSlotNames` :7874** — no
  recursion of any kind; all user re-entry is through the eval loop. **Bound 0.**
- **R11 `check_duplicates` :3270 / `class_name` :3259 / `set_mro_error` :3310** — `class_name` calls
  `PyObject_GetOptionalAttr(__name__)` and falls back to `PyObject_Repr`, which **is**
  dispatcher-guarded (`Objects/object.c:759`). Flat loops. **Bound 0.**

---

## Classification

| verdict | finding | site |
|---|---|---|
| **FIX** | `merge_class_dict` — cyclic `__bases__` through `dir()`, default stack, no depth needed | :7117 — **CPY-0071**, reconfirmed |
| **CONSIDER** | `get_base_by_token_recursive` — **net new**, reproduced debug/release/ASan; overturns pass-1 ACCEPTABLE | :6002 |
| **CONSIDER** | `solid_base` — **net new**, reproduced debug/release with attribution control; overturns pass-1 ACCEPTABLE | :3776 |
| **CONSIDER** | `_PyType_Modified_Unlocked` — CPY-0087, **trigger set widened** to `obj.newattr = v` via `dictobject.c:1971` | :1206 |
| **CONSIDER** | `assign_version_tag` | :1431 — CPY-0087 |
| **CONSIDER** | `mro_hierarchy_for_complete_type` | :1854 — CPY-0087 |
| **CONSIDER** | `set_flags_recursive` (still unreproduced) | :6500 — CPY-0087 |
| **CONSIDER** | `update_subclasses` ↔ `recurse_down_subclasses` | :12359 / :12397 — CPY-0087 |
| **POLICY** | recursive descent + watcher callbacks + `PyErr_FormatUnraisable("%R")` under `LOCK_KEYS` (`_Py_LOCK_DONT_DETACH`) + types-mutex CS | `dictobject.c:1943-1985` → hand-off |
| **ACCEPTABLE** | `find_name_in_mro` hash entry point — bound 0 / eval-guarded | :6147 |
| **ACCEPTABLE** | the 14-member `PyType_Ready` SCC — guard-broken by `is_readying` / `Py_TPFLAGS_READY`, bound 0 from Python | :6156, :9532 |
| **ACCEPTABLE** | `pmerge` / `mro_implementation_unlocked` / `mro_internal` / `mro_invoke` / `type_mro_modified` — iterative, bound 0 | :3361, :3431, :3655, :3591, :1279 |
| **ACCEPTABLE** | `compatible_for_assignment` `tp_base` walk — iterative, bound 0 | :7683 |
| **ACCEPTABLE** | R26 pickle / `__reduce__` with a lying `__class__` — structurally fixed, probed clean | :8318-8332 |
| **ACCEPTABLE** | `supercheck` / `do_super_lookup` — linear MRO scan, probed clean | :12703, :12637 |
| **ACCEPTABLE** | `check_duplicates` / `class_name` / `set_mro_error` — flat, `PyObject_Repr` dispatcher-guarded | :3270, :3259, :3310 |

**Fix propagation.** One `Py_EnterRecursiveCall()` / `Py_LeaveRecursiveCall()` pair per site fixes all
eight CONSIDER/FIX entries, and `abstract_issubclass` (`Objects/abstract.c:2571`) is the reference
implementation for every one of them. The three that return a pointer or an `int`
(`solid_base`, `get_base_by_token_recursive`, `merge_class_dict`) can take the guard as-is; the
`void`-returning ones (`_PyType_Modified_Unlocked`, `set_flags_recursive`) need a signature change or
an explicit depth counter, which is the most likely reason none of them has one.

---

## TOOLKIT ASSESSMENT

### Precision of the two new rules on this file

**Rule A — `self_recursion` un-gated from slot-hood (issue #28).** 6 findings, **6 true positives,
precision 6/6 = 100%.** Every one is an unguarded descent over a Python-mutable class graph; five are
already-recorded catalog entries and one (`get_base_by_token_recursive` :6002) is **net new and
reproduced this pass**. This is the rule that pass 1's toolkit assessment asked for, built from pass
1's own misses, and it recovered 5 of the 7 descents pass 1 had to find by hand — plus the one pass 1
found and then wrongly dismissed.

**Rule B — `mutual_recursion` (iterative Tarjan SCC, `_MAX_REPORTED_CYCLE = 3`).** 1 finding,
**1 true positive, precision 1/1 = 100%** (`update_subclasses` ↔ `recurse_down_subclasses`, CPY-0087 —
invisible to any per-function analysis, since neither function calls itself). The cap fired once, and
I verified by hand that the suppressed 14-member SCC is genuinely guard-broken (`is_readying` /
`Py_TPFLAGS_READY`), so **the cap hid nothing here**. Keep both the cap and the
`large_cycles_not_reported` log — the log is exactly what made that verifiable rather than invisible.

Combined precision of the two new rules on this file: **7/7**. The file's eighth finding
(`find_name_in_mro`, `hash_entry_point`, `low`) is neither rule's output and is correctly carried at
`low` per the triage rules for that shape.

### RECALL GAP found by reading

**Recall of rule A: 6 of 7. The miss is `solid_base` (:3776) — and its cause is one line of
vocabulary.**

`scan_recursion_guards.py:800` gates emission on `descending_self_calls and (slot is not None or
element_op is not None)`, and `element_op` comes from `_OBJECT_GRAPH_WALK_OPS`
(`scan_recursion_guards.py:236-246`), which lists **accessor function names only**:

```python
_OBJECT_GRAPH_WALK_OPS = frozenset({
    "lookup_tp_bases", "lookup_tp_mro", "lookup_tp_subclasses",
    "_PyType_GetBases", "_PyType_GetMRO", "_PyType_GetSubclasses", "type_from_ref",
})
```

`solid_base`'s recursive argument is `type->tp_base` — a **plain struct-field read**, no accessor call
— so `descent_element_op` returns `None`, `slot` is `None`, and the finding is dropped. That is the
one true positive in this file the two new rules did not surface, and it is a *reproduced SIGSEGV on
the most ordinary trigger in the family* (`class X(Deep): pass`).

**This is the same root cause the include map already recorded for a different scanner.** Per
`preflight/include_map.md` §4.3, `scan_refcounts`'s `borrowed_field_deref_across_call` is likewise
keyed on the four `lookup_tp_*` accessors and is therefore blind to `su->type` / `su->obj` /
`su->obj_type` in `super_getattro` (:12699) and to `metatype = Py_TYPE(type)` in
`_Py_type_getattro_stackref` (:6574). Two scanners, one gap: **the toolkit models the *accessor*
spelling of CPython's type-graph fields and not the *field* spelling.**

**Proposed fix (one change, two scanners).** Add a shared graph-field table —
`tp_base`, `tp_bases`, `tp_mro`, `tp_subclasses` — and treat a recursive/borrowed argument that
syntactically matches `<expr>-><field>` for any field in it exactly as an
`_OBJECT_GRAPH_WALK_OPS` hit, with `element_op` set to the field name. In
`scan_recursion_guards.py` that recovers `solid_base` at `high` with
`element_op: "tp_base"`, taking rule A to 7/7 on this file. In `scan_refcounts.py` it recovers the two
misses the include map names. The table belongs next to `_OBJECT_GRAPH_WALK_OPS` and should be
imported by both, not duplicated.

**Second, softer gap (state it, do not fix it).** The scanner has no notion of a *precondition* on a
descent. `get_base_by_token_recursive` is only reached when `type->tp_mro == NULL`; that is why pass 1
dismissed it and why a static tool cannot settle it either way. The scanner was right to emit at
`high` and leave the precondition to triage — but the `detail` string should say *"reachability
depends on the caller's guard; confirm the precondition"* rather than asserting depth outright, so a
future triager does not repeat pass 1's dismissal from the finding text alone.

### Addition proposed for the false-positive taxonomy (`data/cpython_non_bugs.md`)

Pass 1 dismissed `solid_base` with *"the call is in tail position and clang turns it into a jump"*.
The call is not in tail position (`shape_differs(type, base)` runs after it) and `objdump -d` shows a
real `call solid_base` in **both** the debug and the release build. The observation that produced the
dismissal — "it survived depth 3,000 in a 128 KB thread" — was arithmetic (32 bytes/frame × 3,000 =
96 KB), not an optimization.

> **Never dismiss a self-recursion on an assumed tail-call optimization.** The `mappingproxy_hash`
> precedent in the catalog is real, but it is a *measured* result, not a default. Before claiming TCO:
> (a) check the source — is the call actually the last thing the function does? and (b) run
> `objdump -d --no-show-raw-insn <python> | awk '/<fn>:/{f=1} f{print} f&&/^$/{exit}'` and look for a
> self-`call`. A surviving small-stack probe is evidence about *frame size*, not about *frame count*;
> re-run at 1.5× the depth before concluding.

---

## Reproducers

All under `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/p2/`:

| file | what |
|---|---|
| `token_recurse.py` | `get_base_by_token_recursive` :6002 — exit 139 debug @1500/100 KB, release @3000/100 KB; ASan stack-overflow @6000/256 KB |
| `dict_split_modified.py` | CPY-0087 :1206 reached from `dictobject.c:1971` by `obj.newattr = v` — exit 139 on debug-gil, debug-ft, release-gil |
| `dict_split_control.py` | same, version tags drained on the main thread first — survives, isolating the `:1206` descent |
| `solid_base_probe.py` | `solid_base` :3776 via `type("X", (Deep,), {})` — exit 139 debug + release @4000/100 KB |
| `solid_base_control.py` | same chain, shallow base — survives, attributing the crash to the `tp_base` walk |
| `r25_r26_probe.py` | `a` = CPY-0071 reconfirm (exit 139, default stack); `b` = R26 lying-`__class__` reduce (clean); `c` = `supercheck` lying `__class__` (clean) |
| `recsweep.py` | guard-agnostic tree-sitter ground truth: 7 self-recursive fns, 2 SCCs, over 416 function definitions |
| `asan6000.txt` | ASan stack-overflow report for `get_base_by_token_recursive`, uniform `:6002` frames |
