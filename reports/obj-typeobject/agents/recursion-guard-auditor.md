# Recursion-Guard Analysis Results — slice `obj-typeobject`

**Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0), scope `Objects/typeobject.c` only (13,068 lines).
**Interpreters:** `~/projects/python_build_matrix/builds/{debug,release}-gil-nojit[-asan]/python` @ `a1d580430c8`.
`git diff a1d580430c8 4f3be1b5777 -- Objects/typeobject.c Objects/abstract.c Include/internal/pycore_object.h` is **empty** — every file cited below is byte-identical between the build commit and the target ref, so ASan frame line numbers and target-ref line numbers agree.

---

## Summary

- Recursion-prone descents scanned by `scan_recursion_guards`: **1** (`missing_recursion_guard: 1`, guarded-by-dispatcher: 0)
- Recursion-prone descents found by reading: **12** (7 unguarded, 5 provably bounded)
- **FIX: 1** (unbounded, unguarded, reproduced on the default main-thread stack — debug, release and ASan)
- **CONSIDER: 5** (unguarded, reproduced as SIGSEGV, but the depth needed on an 8 MB main-thread stack costs O(depth²) MRO memory)
- **ACCEPTABLE: 6** (bound stated for each, including the single scanner candidate)

**The headline structural fact:** `Objects/typeobject.c` contains **zero** occurrences of
`Py_EnterRecursiveCall`, `_Py_EnterRecursiveCallTstate`, `Py_ReprEnter` or any other recursion guard.
Seven functions in it recurse over a Python-mutable object graph. The only reason most of them are
survivable is arithmetic, not engineering.

```
$ grep -c "Py_EnterRecursiveCall\|Py_LeaveRecursiveCall\|Py_ReprEnter\|Py_ReprLeave" Objects/typeobject.c
0
$ grep -c "recursion" Objects/typeobject.c
5        # all five are comments (":2908", ":6709", ":8323", ":8325", ":12454")
```

---

## Verdict on the scanner's single candidate

### [ACCEPTABLE] `find_name_in_mro` — `_PyObject_HashDictKey(name)` (Objects/typeobject.c:6147)

**Bound: 0 for an exact `str`, and eval-loop-guarded for a `str` subclass.** Every caller of
`_PyType_Lookup` / `_PyType_LookupStackRefAndVersion` / `find_name_in_mro` enforces
`PyUnicode_Check(name)` before the call — `_Py_type_getattro_stackref` at `:6577`, `PyObject_GetAttr`
in `Objects/object.c`, `_PyObject_GenericGetAttrWithDict`; the two internal callers pass a
`&_Py_ID(...)` singleton (`:1268`, `:2973`, `:3001`, `:11137`) or `p->name_strobj` (`:11942`).

- exact `str` → `PyUnstable_Unicode_GET_CACHED_HASH` fast path, or `unicode_hash` over a flat byte
  range. That is the scanner's own `bound_zero_excluded` family. **Bound 0.**
- `str` *subclass* with a Python `__hash__` → `slot_tp_hash` → the eval loop, which **is** guarded by
  `_Py_EnterRecursiveCallTstate`. Verified: `getattr(int, S("zz"))` with
  `class S(str): __hash__ = lambda self: hash(S(self))` returns `AttributeError`, not a crash, in a
  128 KB thread (`scratchpad/find_name_in_mro_probe.py`). Incidentally this exposes that the
  `RecursionError` raised by the name's `__hash__` is swallowed by the `PyErr_Clear()` at `:6149` and
  reported as a missing attribute — a cosmetic issue, not a crash, and out of scope here.
- non-`str` → `TypeError: attribute name must be string, not 'tuple'` before any hash happens.

This is the taxonomy's "hashed field is always a `str`" FP class. Not a bug.

---

## Findings

### [FIX] `merge_class_dict` walks an arbitrary `__bases__` object graph with no recursion guard (Objects/typeobject.c:7117)

**What.** `merge_class_dict()` (`:7078`) is `type.__dir__`'s worker. It reads `__bases__` off the
object it was handed — with an explicit comment *"We have no guarantee that bases is a real tuple"* —
then calls itself once per element:

```c
    if (PyObject_GetOptionalAttr(aclass, &_Py_ID(__bases__), &bases) < 0) {   /* :7098 */
        return -1;
    }
    ...
                PyObject *base = PySequence_GetItem(bases, i);               /* :7111 */
                ...
                status = merge_class_dict(dict, base);                       /* :7117 */
```

`type___dir___impl` (`:7146`) enters with a real type, but from level 2 on `aclass` is whatever
`__bases__` yielded — an **arbitrary Python object**, whose `__bases__` is then an ordinary instance
attribute. The graph is therefore fully user-controlled and may be **cyclic**. No depth is required
and no memory grows: a 2-node cycle recurses forever.

**Guarded twin — and it is a documented fix-propagation gap.** The other `__bases__` walker in the
runtime, `abstract_issubclass()` (`Objects/abstract.c:2522-2582`), does exactly this walk and *does*
guard it:

```c
    if (_Py_EnterRecursiveCall(" in __issubclass__")) {      /* Objects/abstract.c:2571 */
```

That guard was added by **bpo-30570 / GH-29048** (`423fa1c1817`, 2021-10-22, *"Use
Py_EnterRecursiveCall() in issubclass()"*), which fixed gh-74755 *"issubclass segfaults on objects
with weird `__getattr__`"* — the same segfault, in the same shape, in the sibling function.
`merge_class_dict` was never touched: `git log -S"merge_class_dict(dict, base)"` shows its last
substantive change is `82b00c1d307` (2011). The fix landed in one of the two `__bases__` walkers.

**Reachability (pure Python, ~10 lines, no depth, default stack).**

```python
class Fake: pass
a = Fake()
a.__bases__ = (a,)                    # the cycle

class Meta(type):
    @property
    def __bases__(cls):               # a metaclass data descriptor wins over type.__bases__
        return (a,)

class C(metaclass=Meta): pass
dir(C)                                # or type.__dir__(C)
```

**Confirmed.**

| build | command | result |
|---|---|---|
| `debug-gil-nojit` | `python rec_mergedict.py` | **SIGSEGV, exit 139** |
| `release-gil-nojit` | `python rec_mergedict.py` | **SIGSEGV, exit 139** |
| `debug-gil-nojit-asan` | `python rec_mergedict.py` | **`AddressSanitizer: stack-overflow`, exit 1** |
| `debug-gil-nojit` | `python rec_mergedict_dir.py` (builtin `dir()`) | **SIGSEGV, exit 139** |

ASan frames (uniform repetition, hundreds of frames):

```
==950485==ERROR: AddressSanitizer: stack-overflow on address 0x7fff5f22fff0 ... T0
    #0 _PyType_LookupStackRefAndVersion  Objects/typeobject.c:6307
    #1 _PyObject_GenericGetAttrWithDict  Objects/object.c:1919:5
    #2 PyObject_GetOptionalAttr          Objects/object.c:1405:19
    #3 merge_class_dict                  Objects/typeobject.c:7087:9
    #4 merge_class_dict                  Objects/typeobject.c:7117:26
    #5 merge_class_dict                  Objects/typeobject.c:7117:26
    ... (repeats)
```

Note this crashes on the **default 8 MB main-thread stack** with no threading tricks and no deep data
structure — it is the strongest form of this class.

**Prior art: none found.** `gh api -X GET search/issues` for `merge_class_dict`, `__bases__ recursion
segfault`, `dir() cyclic __bases__`, `type.__dir__ recursion crash` returns only gh-35361, gh-37773,
gh-78111 (bpo-33930, `object().__dir__` via deep `__getattr__`, fixed 2021 elsewhere) and gh-74755
(the guarded twin's own historical bug). Novel.

**Impact.** Uncatchable native stack overflow → SIGSEGV, not a catchable `RecursionError`. Reachable
from `dir()` on any class whose metaclass exposes a `__bases__` descriptor.

**Fix.** Bracket the loop with `Py_EnterRecursiveCall(" while merging class dicts")` /
`Py_LeaveRecursiveCall()`, exactly as `abstract_issubclass` does.

---

### The class-hierarchy descent family (5 findings, one shape)

Five more functions in this file recurse over `tp_subclasses`, `tp_bases` or `tp_base` with no guard.
They share one shape, one trigger family, and one fix.

| # | function | recursion site | walks | verdict |
|---|---|---|---|---|
| 1 | `_PyType_Modified_Unlocked` | **:1206** | `tp_subclasses` | CONSIDER (ASan-confirmed) |
| 2 | `assign_version_tag` | **:1431** | `tp_bases` | CONSIDER (ASan-confirmed) |
| 3 | `mro_hierarchy_for_complete_type` | **:1854** | `tp_subclasses` | CONSIDER (SIGSEGV, isolated) |
| 4 | `update_subclasses` ↔ `recurse_down_subclasses` | **:12359 / :12397** | `tp_subclasses` | CONSIDER (ASan-confirmed) |
| 5 | `set_flags_recursive` | **:6500** | `tp_subclasses` | CONSIDER (unreproduced) |

**Why CONSIDER and not FIX — the bound, stated honestly.** Depth here equals inheritance-chain depth,
and a chain of depth *N* costs `Σ d·8 ≈ 4N²` bytes just in `tp_mro` tuples. Measured frame cost on
`debug-gil-nojit` is ~43–85 bytes/frame (3,000 frames overflow a 128 KB stack but survive 256 KB), so
overflowing the **default 8 MB main-thread stack** needs ~95k–190k classes ⇒ ~36–144 GB of MRO
tuples. Not practically reachable on the main thread.

**But it is trivially reachable on a worker thread**, using only the public, documented
`threading.stack_size()`:

```
depth 1,000 @ 100 KB stack -> survives
depth 1,500 @ 100 KB stack -> SIGSEGV (exit 139)      ~9 MB of chain
depth 3,000 @ 128 KB stack -> SIGSEGV (exit 139)
depth 3,000 @ 256 KB stack -> survives
```

`100 KB` is `threading.stack_size()`'s own documented minimum (`ValueError: size must be at least
102400 bytes`).

**The control run is what makes this a missing guard rather than "the stack was too small."** In the
*same* 128 KB thread, every *guarded* CPython descent degrades correctly
(`scratchpad/control_smallstack.py`):

```
A python-recursion     -> RecursionError: maximum recursion depth exceeded
B abstract_issubclass  -> RecursionError: Stack overflow (used 60 kB) in __issubclass__
C nested list repr     -> RecursionError: Stack overflow (used 60 kB) while getting the repr of an object
controls done, no crash                                          exit 0
```

CPython's `c_stack_soft_limit` is derived from the *actual* thread stack, so a guarded descent adapts
to a 100 KB thread and an unguarded one does not. That asymmetry is the finding.

#### 1. `_PyType_Modified_Unlocked` (:1166, recursion at :1206)

Invalidates the type version tag for a type and every subclass, recursively. Triggered by **any**
`setattr` on a class (`type_setattro` → `type_update_dict` → `_PyType_Modified_Unlocked`), and by
`__bases__` assignment, `__abstractmethods__`, ABC registration, and the type watchers.

Reproduced (`scratchpad/deep_chain2.py 3000 modified 128`), ASan:

```
==973451==ERROR: AddressSanitizer: stack-overflow ... T1
    #2 _PyType_Modified_Unlocked  Objects/typeobject.c:1167
    #3 _PyType_Modified_Unlocked  Objects/typeobject.c:1206:13
    #4 _PyType_Modified_Unlocked  Objects/typeobject.c:1206:13
    ... (repeats)
```

Note the descent short-circuits when a subclass already has `tp_version_tag == 0`, so a repro must
prime version tags bottom-up first (the same "prime the caches" discipline as `tuple_hash`).

#### 2. `assign_version_tag` (:1408, recursion at :1431)

Walks *up* `tp_bases` to satisfy the invariant "before `tp_version_tag` can be set on a type, it must
first be set on all super types". Called from `_PyType_LookupStackRefAndVersion` (`:6361`) on any
type-cache miss for a cacheable name — i.e. from ordinary attribute access. Reproduced
(`deep_chain2.py 2000 version 100`), ASan:

```
==979603==ERROR: AddressSanitizer: stack-overflow ... T1
    #2 assign_version_tag  Objects/typeobject.c:1428:20
    #3 assign_version_tag  Objects/typeobject.c:1431:14
    #4 assign_version_tag  Objects/typeobject.c:1431:14
    ... (repeats)
```

#### 3. `mro_hierarchy_for_complete_type` (:1798, recursion at :1854)

Recomputes `tp_mro` for a type and all of its subclasses after `__bases__` assignment. Trigger:
`root.__bases__ = (other_object_subclass,)` on the root of a deep chain. SIGSEGV exit 139 at
depth 2,500 / 128 KB with version tags pre-cleared so `_PyType_Modified_Unlocked` cannot be the
crashing frame (`scratchpad/isolate_descent.py 2500 mrohier 128`).

No ASan frames for this one: under `debug-gil-nojit-asan` the 128 KB thread cannot start at all
(ASan's redzones inflate every frame, and the overflow is reported inside `pthread_create` /
`ThreadHandle_start` before the descent is reached). The evidence here is therefore the non-ASan
SIGSEGV only — stated rather than reconciled.

#### 4. `update_subclasses` ↔ `recurse_down_subclasses` (:12353 / :12363; sites :12359, :12397)

**Mutual** recursion — the file's own comment says so: *"recurse_down_subclasses() and
update_subclasses() are mutually recursive functions to call a callback for all subclasses"*.
Trigger: assigning a *slot* name on the root (`root.__len__ = f`) → `update_slot_after_setattr` →
`update_slot` → `update_subclasses`. SIGSEGV exit 139 at depth 2,500 / 128 KB with version tags
pre-cleared so `_PyType_Modified_Unlocked` cannot be the crashing frame
(`isolate_descent.py 2500 slot 128`). ASan attributes the overflow to the mutual pair directly:

```
==982081==ERROR: AddressSanitizer: stack-overflow ... T1
    #4 find_name_in_mro         Objects/typeobject.c:6180:25
    #5 update_one_slot          Objects/typeobject.c:11942:19
    #6 update_slots_callback    Objects/typeobject.c:12080:13
    #7 update_subclasses        Objects/typeobject.c:12356:9
    #8 recurse_down_subclasses  Objects/typeobject.c:12397:13
    ... (the :12356 / :12397 pair repeats)
```

This one is invisible to any single-function scanner: neither function calls *itself*.

#### 5. `set_flags_recursive` (:6483, recursion at :6500)

Propagates `Py_TPFLAGS_SEQUENCE` / `Py_TPFLAGS_MAPPING` down `tp_subclasses`. Python-reachable via
`collections.abc.Sequence.register(cls)` → `_abc._abc_register` (`Modules/_abc.c:601`).
**Not reproduced**: at depth 4,000 / 100 KB the `ABCMeta.register` Python frames trip the eval-loop
guard first (`RecursionError: Stack overflow (used 32 kB)`) before the C descent is entered. The
descent itself is unguarded and structurally identical to #1/#3; listed for completeness and for the
fix, not as a confirmed crash.

**Fix for the family.** One `Py_EnterRecursiveCall()` / `Py_LeaveRecursiveCall()` pair per function
(for #4, one pair in `update_subclasses`, which is the cycle's entry). The `void`-returning ones
(`_PyType_Modified_Unlocked`, `set_flags_recursive`) need either a signature change or an explicit
depth counter, which is presumably why nobody did it. `abstract_issubclass` (`Objects/abstract.c:2571`)
remains the reference implementation.

---

## Classes bounded (clean negatives, with the bound)

- **`find_name_in_mro`** (`:6147`) — hashes `name`, which every caller has already
  `PyUnicode_Check`-ed. **Bound 0** for exact `str` (cached hash / flat-byte `unicode_hash`);
  eval-loop-guarded for a `str` subclass. Verified empirically.
- **`get_base_by_token_recursive`** (`:5988`, recursion at `:6002`) — self-recursive over `tp_bases`,
  but its only caller reaches it exclusively on the `type->tp_mro == NULL` branch
  (`PyType_GetBaseByToken_DuringGC`, `:6037-6046`); a completed user hierarchy always has a non-NULL
  `tp_mro` and takes the *iterative* MRO scan at `:6053`. **Bound 0** for any Python-constructible
  type. Would become live only inside `PyType_Ready` / GC, where the depth is the same
  memory-bounded chain.
- **`solid_base`** (`:3771`, recursion at `:3776`) — self-recursive over `tp_base`. Survives depth
  3,000 in a 128 KB thread where `_PyType_Modified_Unlocked` dies at the same depth: the call is in
  tail position and clang turns it into a jump. Same compiler-dependence caveat as `mappingproxy_hash`
  in the toolkit catalog. **Bound: compiler-dependent; not reproducible here.**
- **`type_repr`** (`:2394`) and `object_repr` — **not recursive at all**. `type_repr` formats two
  already-computed strings with `PyUnicode_FromFormat("%U.%U")`; it never descends into an element.
  The dispatcher guard in `PyObject_Repr` (`Objects/object.c:759`) is therefore not even load-bearing
  here. **Bound 0.**
- **`mro_implementation_unlocked`** (`:3431`) — iterative; the C3 linearization is done by `pmerge`
  over a flat `to_merge` array, not by recursion. `mro_invoke` (`:3591`) calls the metaclass's
  `mro()`, which is a **Python** call and therefore eval-loop-guarded. `type_mro_modified` (`:1279`)
  is a flat loop over `bases`. `is_subtype_with_mro` scans the MRO tuple linearly. **Bound 0** for all
  four. A metaclass returning an arbitrary `mro()` changes the *contents* of the MRO, not the C
  recursion depth.
- **`super_getattro`** (`:12688`) → `do_super_lookup` — linear scan of the `tp_mro` tuple, no
  recursion. **Bound 0.**
- **`type_new_set_names`** (`:12276`) and **`type_new_init_subclass`** (`:12322`) — both re-enter type
  creation only through `PyObject_CallFunctionObjArgs` / `PyObject_VectorcallDict`, i.e. through the
  eval loop. Verified: a `__init_subclass__` that creates a subclass, and a `__set_name__` that
  creates a class, both raise `RecursionError: Stack overflow (used 64 kB / 62 kB)` in a 128 KB thread
  and exit 0 (`scratchpad/init_subclass_reentry.py`). **Bounded by the eval-loop guard.**

---

## Toolkit assessment

**Precision on this slice: 0/1.** The one `missing_recursion_guard` finding is a false positive of a
class the taxonomy already names ("the hashed field is always a `str`").

**Recall on this slice: 0/7 of the real unguarded descents.** All seven were found by reading. The
cause is one line:

`plugins/cpython-review-toolkit/scripts/scan_recursion_guards.py:596`

```python
    if slot is not None and descending_self_calls:
```

The `self_recursion` shape is **gated on the function being a recognized type slot**. Every recursive
descent in `typeobject.c` is a non-slot `static` helper (`merge_class_dict`,
`_PyType_Modified_Unlocked`, `assign_version_tag`, `mro_hierarchy_for_complete_type`,
`set_flags_recursive`, `get_base_by_token_recursive`, `solid_base`), so `descending_self_calls` is
computed and then discarded. `_has_guard()` whole-function suppression is *not* the cause here — the
file has zero guards.

Three concrete tuning proposals, in value order:

1. **Un-gate `self_recursion` from `slot is not None`.** Emit non-slot self-recursion at `medium`
   confidence when the recursive argument derives from an object-graph walk — `PyTuple_GET_ITEM`,
   `PyList_GET_ITEM`, `PySequence_GetItem`, `PyDict_Next`, or the result of
   `PyObject_GetAttr`/`PyObject_GetOptionalAttr`. That single change surfaces 5 of the 7 (it would
   have surfaced the FIX, `merge_class_dict`, at `high`: its recursive argument is a
   `PySequence_GetItem` of a `PyObject_GetOptionalAttr` result — maximal user control).
2. **Add a mutual-recursion shape.** `update_subclasses` ↔ `recurse_down_subclasses` is invisible to
   any per-function analysis, and CPython's own comment advertises it. `scan_stw_safety.py` already
   builds an intra-file call graph — reuse it and flag 2- and 3-cycles that pass a `PyObject *` /
   `PyTypeObject *` derived from a container walk.
3. **Broaden `element_op` beyond the four dispatchers.** The scanner's vocabulary is
   hash/repr/richcompare-centric. None of these seven descents calls a dispatcher at all; they walk
   `tp_bases`, `tp_subclasses`, `tp_base` and `__bases__` directly. An `object_graph_walk` element_op
   keyed on those four accessors (`lookup_tp_bases`, `lookup_tp_subclasses`, `_PyType_GetSubclasses`,
   `_Py_ID(__bases__)`) would cover this whole file.

**Cross-slice hint:** `Modules/_abc.c:417` does `PyObject_GetAttr(self, &_Py_ID(__bases__))` inside
`_abc__abc_subclasscheck_impl`'s helper — the third `__bases__` walker in the tree. Worth checking in
the `mod-abc`/`Modules` slice against the same guarded twin.

---

## Reproducers

All under `/tmp/claude-1000/-home-danzin-projects-pyo3-review-toolkit/ccc76c44-4d07-4e2a-a62c-6383cfd6c620/scratchpad/`:

| file | what |
|---|---|
| `rec_mergedict.py` | FIX — cyclic `__bases__` via `type.__dir__`, default stack, exit 139 |
| `rec_mergedict_dir.py` | same via the builtin `dir()`, exit 139 |
| `rec_twin_issubclass.py` | guarded twin control — `RecursionError`, exit 0 |
| `control_smallstack.py` | 128 KB-thread control: eval loop / `abstract_issubclass` / `Py_ReprEnter` all degrade cleanly |
| `deep_chain2.py` | `_PyType_Modified_Unlocked` / `assign_version_tag`, version tags primed bottom-up |
| `deep_chain3.py`, `isolate_descent.py` | `update_subclasses` / `mro_hierarchy_for_complete_type`, version tags pre-cleared to isolate |
| `abc_flags.py` | `set_flags_recursive` via `collections.abc.Sequence.register` (not reproduced) |
| `find_name_in_mro_probe.py` | the scanner candidate's three name kinds |
| `init_subclass_reentry.py` | `__init_subclass__` / `__set_name__` re-entrancy — bounded |
