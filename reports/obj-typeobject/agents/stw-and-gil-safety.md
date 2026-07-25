# StopTheWorld safety + GIL/thread-state discipline — `Objects/typeobject.c`

**Slice:** `obj-typeobject` (informed-explore) · **Target:** `/home/danzin/projects/cpython` @ `4f3be1b5777` (3.16.0a0)
**Scope:** `Objects/typeobject.c` only, 13,068 lines
**Builds used:** `~/projects/python_build_matrix/builds/{debug,release}-{gil,ft}-nojit` @ `a1d580430c8`.
`git diff 4f3be1b5777 a1d580430c8 -- Objects/typeobject.c Include/internal/pycore_interpframe.h Python/critical_section.c` is **empty** — every file cited below is byte-identical between the target ref and the matrix builds.

---

## 0. Denominator verdict (read this before the findings)

Both scanners reported **0 findings**. They are not the same kind of zero.

### `scan_gil_usage` — STRUCTURAL ZERO. Nothing was resolved. Do not read as clean.

The sample's own `vocabulary_counts` is the canary and it is all zeros:

```json
"vocabulary_counts": {
  "Py_BEGIN_ALLOW_THREADS": 0, "Py_END_ALLOW_THREADS": 0,
  "PyGILState_Ensure": 0, "PyGILState_Release": 0
}
```

**The rule's entire vocabulary is absent from this file.** Zero findings out of zero
resolved constructs. `functions_analyzed: 417` is a parse count, not a denominator —
none of those 417 functions contained a single construct the rule can classify.

This is not because `typeobject.c` has no thread-state discipline. It has a great
deal of it, none of which this rule can see:

| construct in the file | count | visible to `scan_gil_usage`? |
|---|---|---|
| `_PyEval_StopTheWorld` / `types_stop_world()` regions | 11 | no |
| `Py_BEGIN_CRITICAL_SECTION` / `_MUTEX` / `2_MUTEX` families | 3 macro families, 5 sites | no |
| raw `PyMutex_Lock` / `Unlock` | 3 pairs (`:323`, `:416`, `:4630`) | no |
| `_PyThreadState_GET()` | 14 | no |
| `_PyThreadState_GetFrame()` (frame introspection) | 1 (`:12921`) | no |
| `type_lock_prevent_release()` / `_allow_release()` | 5 pairs | no |

**Toolkit assessment.** `scan_gil_usage` is calibrated for the GIL-era
`Py_BEGIN_ALLOW_THREADS` idiom, which is a *C-extension / blocking-I/O* pattern.
Core `Objects/*.c` files essentially never use it — the tree-wide count is 16 for
all of `Objects/`, and this file contributes 0 of them. On the free-threaded build
the equivalent discipline is expressed with critical sections, `PyMutex`, and
stop-the-world, and the rule is blind to all three. **Reporting "GIL discipline:
clean" for this file would be certifying a rule that never ran.**

*Tuning proposal:* the rule needs a second, FT-era vocabulary
(`_PyEval_StopTheWorld`/`StartTheWorld`, `Py_BEGIN_CRITICAL_SECTION*`, raw
`PyMutex_Lock` outside a critical section, `_PyThreadState_GET`/`GetFrame`), or it
should emit an explicit `rule_not_applicable` marker when `vocabulary_counts` sums
to zero so that downstream synthesis cannot silently read it as a pass.

### `scan_stw_safety` — PARTIAL DENOMINATOR: 2 of 11 regions. The zero is real for what it saw, but it saw 18%.

The rule *did* resolve constructs here — 417 function classifications and 3
`stw_functions` — so this is not a structural zero. But it found only the regions
delimited by a **literal** `_PyEval_StopTheWorld(...)` … `_PyEval_StartTheWorld(...)`
token pair.

`typeobject.c` wraps the primitive in a file-local pair at `:123` / `:132`:

```c
static void types_stop_world(void)  { ... _PyEval_StopTheWorld(interp);  ... }   // :124-130
static void types_start_world(void) { ... _PyEval_StartTheWorld(interp); ... }   // :133-139
```

and then uses the wrapper almost everywhere. Full census:

| # | region (stop → start) | enclosing function | opened via | scanner saw it |
|---|---|---|---|---|
| 1 | `:669` → `:673` | `set_tp_mro` | wrapper | **no** |
| 2 | `:1572` → `:1576` | `type_set_name` | literal | yes |
| 3 | `:1598` → `:1601` | `type_set_qualname` | literal | yes |
| 4 | `:1745` → `:1750` | `type_set_abstractmethods` | wrapper | **no** |
| 5 | `:1939` → `:1942` | `type_set_bases_unlocked` | wrapper | **no** |
| 6 | `:2005` → `:2008` | `type_set_bases_unlocked` (bail) | wrapper | **no** |
| 7 | `:3923` → `:3925` | `apply_type_slot_updates` | wrapper | **no** |
| 8 | `:6457` → `:6460` | `_PyType_SetFlags` | wrapper | **no** |
| 9 | `:6522` → `:6524` | `_PyType_SetFlagsRecursive` | wrapper | **no** |
| 10 | `:7823` → `:7828` | `object_set_class` | wrapper | **no** |
| 11 | `:12523` → `:12525` | `PyType_Freeze` | wrapper | **no** |

**9 of 11 regions (82%) were never analysed** — including every region the run
brief specifically asked about (`type_set_abstractmethods`, `apply_type_slot_updates`,
`_PyType_SetFlagsRecursive`, `PyType_Freeze`, `object_set_class`). The one real FIX
below is in region 9, which the scanner did not open.

Note also `:192-194`: on the default (GIL) build `types_stop_world()` /
`types_start_world()` are **empty macros** and `types_world_is_stopped()` is `1`.
Everything here is `Py_GIL_DISABLED`-only.

And for the two regions it *did* open, both were classified `unsafe` at the function
level yet produced no finding. That is correct — in `type_set_name` and
`type_set_qualname` the unsafe calls (`check_set_special_type_attr`, `PyErr_Format`,
`PyUnicode_AsUTF8AndSize`) all sit **before** `_PyEval_StopTheWorld`, and the regions
themselves are three pointer stores. But it means the reported zero is an accidental
zero on an 18% sample, not evidence of safety.

*Tuning proposal (high value, cheap):* before matching, resolve **file-local
one-line wrappers** around `_PyEval_StopTheWorld` / `_PyEval_StartTheWorld` and treat
them as region delimiters. A function whose body is a single call to
`_PyEval_StopTheWorld(...)` plus asserts is a stop-the-world *alias*. This is not a
typeobject.c quirk: it is how a subsystem that stops the world repeatedly is
normally written. The same wrapper shape should be looked for tree-wide before the
`Objects/` denominator of 13 is trusted.

---

## 1. STW census result

**11 regions. 1 FIX, 1 POLICY, 9 clean.**

`typeobject.c` is, on the whole, an *exemplary* stop-the-world citizen, and it
carries its own in-file statements of the invariant — which is what makes the one
violation findable:

- **`apply_type_slot_updates` (`:3902-3927`)** is the canonical guarded twin in this
  file. It carries a 20-line comment documenting the lock order (TYPE_LOCK then STM
  mutex), and — decisively — all of its *allocation* is hoisted out:
  `queue_slot_update` (`:3859`) does the `PyMem_Malloc` + `PyErr_NoMemory` **before**
  the world stops, and `apply_slot_updates` (`:3883`) then does nothing but
  `*(item->slot_ptr) = item->slot_value;` with an `assert(types_world_is_stopped())`.
  That is exactly `Python/gc_free_threading.c:2223` expressed in this file.
- **`type_set_name` (`:1571-1577`)** / **`type_set_qualname` (`:1597-1602`)** put
  `Py_DECREF(old_name)` **after** `_PyEval_StartTheWorld` — deliberately, since a
  DECREF can run `__del__`. Same ordering discipline.
- **`_PyType_SetFlagsRecursive` (`:6508-6518`)** states the rule in a comment:
  *"`_PyType_Modified_Unlocked()` is re-entrant and cannot run with the world
  stopped, so we must invalidate first."* **`PyType_Freeze` (`:12522-12527`)** obeys
  the same ordering. This is CPython telling you what the STW contract is, in this
  file, in its own words.

Regions 1, 4, 6, 7, 8, 11 are pure field stores (`self->tp_mro = mro`,
`type_add_flags`, `type_clear_flags`, `self->tp_flags = ...`) and are unambiguously
clean. No region has a `goto`/early-`return` that skips its `StartTheWorld` — I
checked all 11, including the `undo:`/`bail:` ladder in `type_set_bases_unlocked`
(`:1984-2020`) and the `if (!unique)` pairing in `object_set_class` (`:7821-7829`),
where `unique` is cached in a local so the stop and start cannot disagree.

---

## 2. Findings

### [FIX] Allocation inside the STW region, failure discarded — `set_flags_recursive` (Objects/typeobject.c:6483-6503, region opened at :6522)

**What.** `_PyType_SetFlagsRecursive` stops the world at `:6522` and calls
`set_flags_recursive`, which for **every node of the subclass tree** calls
`_PyType_GetSubclasses` (`:776-807`). That helper allocates:

```c
PyObject *list = PyList_New(0);            // :778   allocation #1, world stopped
if (list == NULL) { return NULL; }
...
    if (PyList_Append(list, _PyObject_CAST(subclass)) < 0) {   // :799  allocation #2..N
        Py_DECREF(list); Py_DECREF(subclass); return NULL;
    }
```

and the caller then **throws the failure away**:

```c
    PyObject *children = _PyType_GetSubclasses(self);
    if (children == NULL) {
        return;                             // :6494-6496  <-- error discarded
    }
```

`set_flags_recursive` returns `void`; `_PyType_SetFlagsRecursive` returns `void`.
So on allocation failure the function returns **normally**, with the flag applied to
`self` and to *none* of its subclasses, and with a `MemoryError` **set inside the
stop-the-world region and still pending on return**.

**Call chain:** `_PyType_SetFlagsRecursive` (`:6506`) → `types_stop_world()` (`:6522`)
→ `set_flags_recursive` (`:6483`) → `_PyType_GetSubclasses` (`:776`) →
`PyList_New` (`:778`) / `PyList_Append` (`:799`) → `PyErr_NoMemory` with the world stopped.

**Python reachability:** `collections.abc.Sequence.register(C)` →
`Modules/_abc.c:601` → `_PyType_SetFlagsRecursive(subclass, COLLECTION_FLAGS, ...)`.
Measured: registering a root with 200 children × 20 grandchildren walks **4,201
types with the world stopped**, doing one `PyList_New` plus N `PyList_Append` per node.

**Guarded twin.** Two, both in this file:
1. `queue_slot_update` (`:3859`) / `apply_slot_updates` (`:3883`) — the deferred-update
   queue that exists *precisely* to move allocation out of the STW region. Same author,
   same subsystem, 2,600 lines earlier.
2. `recurse_down_subclasses` (`:~12330`) — the *other* subclass walker in this file.
   It walks the identical `PyDict_Next(subclasses, ...)` loop but **materializes no
   list at all** and returns `int`, propagating every error to its caller. The
   allocating variant is the one used inside STW; the non-allocating one is used
   outside it. That inversion is the finding.

**Reproduced** (`_testcapi.set_nomemory` sweep, dense OOM injection):

```
seq   n=30 start= 26 register_raised=-   PENDING-EXC-AFTER-register  root=True  kids=0/30
seq   n=30 start= 29 register_raised=-   PENDING-EXC-AFTER-register  root=True  kids=0/30
plain n=30 start=25..36  (ABC with no collection flags -> SetFlagsRecursive never called)
                         register_raised=-   -                       root=False kids=0/30
seq   n=0  start=25..36  (no subclasses -> no PyList_Append)
                         register_raised=-   -                       root=True  kids=0/0
```

The control is decisive: the window exists **only** when `_PyType_SetFlagsRecursive`
runs **and** the type has subclasses. Two observable consequences:

- **Silent partial application.** `Root.__flags__` gains `Py_TPFLAGS_SEQUENCE`,
  all 30 subclasses do not. `match Root(): case [*_]` now behaves differently from
  `match K0(): case [*_]` even though `K0` inherits from `Root`. `register()` returns
  the subclass and raises nothing.
- **Exception-state desync.** The `MemoryError` set with the world stopped is still
  pending when the next C function returns a result:
  - `release-ft-nojit` (`PYTHON_GIL=0`) and `release-gil-nojit`:
    `SystemError: <built-in function ...> returned a result with an exception set`
  - `debug-gil-nojit` / `debug-ft-nojit`: **SIGABRT**,
    `Python/ceval.c:823: Assertion '(res != NULL) ^ (PyErr_Occurred() != NULL)' failed`

Reproducers: `scratchpad/stw_oom.py`, `scratchpad/stw_oom2.py` (control),
`scratchpad/stw_abc.py` (reachability).

**Not the "who owes the MemoryError" FP class.** The taxonomy entry says a failure
branch that returns the sentinel after `PyList_New`/`PyList_Append` is correct
*because those raise for you*. That is not what happens here — the sentinel is not
returned, it is **dropped on the floor** by a `void` function two frames up, so the
exception the allocator correctly raised is never delivered to Python.

**Fix.** Mirror `queue_slot_update` / `gc_free_threading.c:2223`: collect the
transitive subclass set into a `PyMem_Malloc`'d array (or reuse the non-allocating
`recurse_down_subclasses` walk) *before* `types_stop_world()`, then apply the flags
with the world stopped and nothing else. Minimum viable fix: give
`set_flags_recursive` and `_PyType_SetFlagsRecursive` an `int` return and propagate
it to `Modules/_abc.c:601`.

**Provenance.** The STW wrapping of this walk is recent —
`e7eaed56149` (gh-148450, *"abc.register needs to update type_version when tp_flags
is changed"*, #148623). No tracker issue found for the swallowed failure
(`gh api search/issues` for `set_flags_recursive MemoryError` → 0 results).

---

### [CONSIDER] `super()` zero-arg frame introspection: debug-only assert guards a `PyCellObject` cast — `super_init_without_args` (Objects/typeobject.c:12833-12845)

The run brief asked what `super_init_without_args` assumes about the caller's frame.
It assumes one thing that is only checked by an `assert`.

```c
    if ((_PyLocals_GetKind(co->co_localspluskinds, 0) & CO_FAST_CELL) &&
            (_PyInterpreterFrame_LASTI(cframe) >= 0)) {
        assert(_PyCode_CODE(co)[0].op.code == MAKE_CELL ||
                _PyCode_CODE(co)[0].op.code == COPY_FREE_VARS);   // :12837-12838
        assert(PyCell_Check(firstarg));                            // :12839  DEBUG ONLY
        firstarg = PyCell_GetRef((PyCellObject *)firstarg);        // :12840  unchecked cast
```

With `NDEBUG`, `localsplus[0]` is cast to `PyCellObject *` and `ob_ref` is read at
offset 16 and `Py_XNewRef`'d, based purely on `co_localspluskinds[0]` claiming
`CO_FAST_CELL`.

**Guarded twin — 20 lines below, in the same function.** The `__class__` cell lookup
does the runtime check the `firstarg` path only asserts:

```c
    PyObject *cell = PyStackRef_AsPyObjectBorrow(_PyFrame_GetLocalsArray(cframe)[i]);
    if (cell == NULL || !PyCell_Check(cell)) {                     // :12859
        PyErr_SetString(PyExc_RuntimeError, "super(): bad __class__ cell");
```

Same function, same kind of value, one gets a `RuntimeError` and one gets a cast.

**Reproduced.** Take a real method whose first argument is a cell (captured by a
nested closure) and whose `__class__` is a freevar, then NOP out its `MAKE_CELL 0`.
The compiler-emitted prologue is `[0] COPY_FREE_VARS 1 / [2] MAKE_CELL 0 / [4] RESUME`,
so `_PyCode_CODE(co)[0]` is still `COPY_FREE_VARS` and the `:12837` assert **also
passes** — only the debug-only `PyCell_Check` stands between the caller and the cast.

| build | result |
|---|---|
| `debug-gil-nojit` | SIGABRT — `Assertion 'PyCell_Check(firstarg)' failed` at typeobject.c:12839 |
| `debug-ft-nojit` (`PYTHON_GIL=0`) | SIGABRT — same |
| `release-gil-nojit` | **SIGSEGV (139)** |
| `release-ft-nojit` (`PYTHON_GIL=0`) | **SIGSEGV (139)** |

Reproducer: `scratchpad/super_cell.py` (calls the patched function with `1.5`, so
`PyCell_GetRef` dereferences the float's double bits `0x3FF8000000000000`).

**Why CONSIDER and not FIX.** Reachability is via a hand-crafted code object
(`code.replace(co_code=...)`), which CPython does not validate and generally does not
treat as a security boundary. I could not find a compiler-emitted path that breaks the
invariant: `_PyThreadState_GetFrame` → `_PyFrame_GetFirstComplete`
(`Include/internal/pycore_interpframe.h:302-315`) skips frames that have not yet
reached `_co_firsttraceable` (the `RESUME`), and `MAKE_CELL`/`COPY_FREE_VARS` are
emitted *before* `RESUME` — so for any *complete* frame the `MAKE_CELL` has provably
run. The `LASTI >= 0` test at `:12834` and the "very unlikely … called from the C-API
before the first `MAKE_CELL` op" comment at `:12831-12832` are now belt-and-braces
for a case `_PyFrame_GetFirstComplete` already excludes.

**Fix is one line** and matches the twin: replace the `assert(PyCell_Check(firstarg))`
with `if (!PyCell_Check(firstarg)) { PyErr_SetString(PyExc_RuntimeError, "super(): bad
argument cell"); Py_DECREF(firstarg); return -1; }`. Note gh-127272 ("Replace use of
`PyCell_GET`/`SET`") already touched exactly these lines.

**The rest of the zero-arg `super()` frame contract is sound** — I tested the cases
the brief named, all on `debug-gil-nojit` (`scratchpad/super_noframe.py`):

| case | result |
|---|---|
| no Python frame at all (`atexit.register(super)`, called from C at shutdown) | clean `RuntimeError: super(): no current frame` (`:12923`) — `_PyThreadState_GetFrame` returns NULL and `super_init_impl:12922` checks it |
| frame with `co_argcount == 0` (thread bootstrap target) | clean `RuntimeError: super(): no arguments` (`:12819`) |
| zero-arg `super()` inside a `__del__` fired mid-method | resolves against the `__del__`'s **own** frame — correct, not the outer method's |
| frame with no `__class__` freevar | clean `RuntimeError: super(): __class__ cell not found` (`:12884`) |

`_PyThreadState_GET()` cannot be NULL here: reaching `super_init_impl` requires an
attached thread state.

---

### [POLICY] `type.__name__ = ...` takes an unconditional stop-the-world; the fast-path sibling landed only on `__class__`

Measured `type.__name__ = "n"` in a loop (one `_PyEval_StopTheWorld` per assignment,
`typeobject.c:1572`), 2s per data point:

| threads | free-threaded (`PYTHON_GIL=0`) assignments/s | GIL build assignments/s |
|---|---|---|
| 1 | 14,288,802 | 23,056,093 |
| 2 | **3,450** | 21,334,412 |
| 4 | 10,471 | 21,205,256 |
| 8 | 10,895 | 20,975,351 |

Adding a **second** thread to a free-threaded interpreter costs a **4,141×**
throughput collapse on a plain Python attribute assignment. The GIL build is flat
because `types_stop_world()` is an empty macro there (`:193`). Reproducer:
`scratchpad/stw_throughput.py`. This is a design consequence, not a defect — but it
is the reason the sibling hunt matters:

`object_set_class` was given a fast path by `1d091a336e6` (gh-145566, *"Skip
stop-the-world when reassigning `__class__` on newly created objects"*):

```c
    int unique = _PyObject_IsUniquelyReferenced(self);
    if (!unique) { types_stop_world(); }                 // :7821-7824
```

`type_set_name` (`:1571`) and `type_set_qualname` (`:1597`) stop the world
**unconditionally** — and neither consults `TYPE_IS_REVEALED(tp)` (`:102`), the
predicate this file already defines for exactly this question and already relies on
in `ASSERT_WORLD_STOPPED_OR_NEW_TYPE` (`:112-113`): *"Checks if we can safely update
type slots or `tp_flags`"* — i.e. a not-yet-revealed type needs no world stop. In
practice a type reachable from Python has been revealed, so the early-out would
rarely fire; recording this as POLICY (worth a maintainer decision, not a bug).
Callers to watch: anything that rewrites `__name__`/`__qualname__` in bulk
(`functools.wraps` on classes, `unittest.mock`, ORM/proxy factories).

---

### [ACCEPTABLE] `object_set_class` STW region (`:7823-7828`) — documented reasoning

This region is the most aggressive in the file: `object_set_class_world_stopped`
(`:7700-7797`) runs entirely with the world stopped and contains four constructs
that are on the STW deny-list at first glance. All four are defensible here, and the
reasoning is worth recording because it is the same reasoning any future reviewer
will have to redo:

1. **`PyErr_Format` ×3** (`:7757`, and `:7635`/`:7688` via `compatible_for_assignment`).
   Every format string is `%s` on a `char *` (`tp_name`, `attr`) — the `%R`/`%S` repr
   machinery is never entered, so no Python code runs. Falls under the
   conditionally-safe exception rule: exception type is built-in (`PyExc_TypeError`),
   no exception is pending on entry (`object_set_class` has already returned on every
   earlier error).
2. **`PyObject_RichCompareBool(slots_a, slots_b, Py_EQ)`** in `same_slots_added`
   (`:7609`). This is the FP taxonomy's *"type-constrained operand makes a protocol
   call non-Python-reaching"* class, and it holds strictly: `ht_slots` is assigned in
   exactly one place (`:4923`, from `ctx->slots`), every element passed through
   `valid_identifier` (`PyUnicode_Check` + `PyUnicode_IsIdentifier`), and the file's
   own comment at `:7221` states *"`ht_slots` is a tuple of strings"*. The compare
   resolves to `tuple_richcompare` → `unicode_richcompare`, all C.
3. **`PyDict` allocation** (`_PyObject_MaterializeManagedDict_LockHeld`, `:7769`).
   Safe under the 3.14+ rule — object allocation no longer triggers GC synchronously
   on the free-threaded build.
4. **`Py_BEGIN_CRITICAL_SECTION(dict)`** (`:7778`). This looks like the classic
   "take a lock a stopped thread holds" deadlock, and it is not, for a specific
   reason: a thread being stopped goes through `detach_thread`
   (`Python/pystate.c:2316-2331`), which calls `_PyCriticalSection_SuspendAll(tstate)`
   **before** parking — every mutex held via a critical section is released. That is
   also the whole reason `type_lock_prevent_release()` (`:143-163`) has to exist: it
   *opts TYPE_LOCK out* of that release so the STW-initiating thread can keep it.

**The corollary is the interesting part for the toolkit.** Critical sections are
safe during STW; a **raw `PyMutex_Lock` outside a critical section is not** — nothing
suspends it. This file has exactly three such sites: `:323` and `:416`
(`managed_static_type_state_init/_clear`, both on `&interp->types.mutex` = TYPE_LOCK)
and `:4630` (`descriptor_mutex`). None is reachable from an STW region — `:323`/`:416`
run only from `_PyStaticType_InitBuiltin` / `fini_static_type` during startup and
finalization, which are single-threaded. So: clean, but **that** is the shape
`scan_stw_safety` should be flagging, not `Py_BEGIN_CRITICAL_SECTION`.

---

## 3. Finalization (question 3)

No findings. Recorded for completeness:

- **`_PyTypes_Fini` (`:1040-…`)** is `type_cache_clear` plus an `assert`-only body.
  Under `NDEBUG` the entire managed-static verification compiles away. This is the
  briefing's *"teardown, fork-child and assert-only paths"* FP class — not reported.
- **`fini_static_type` (`:~7050`)** clears `Py_TPFLAGS_READY` and calls
  `set_version_unlocked(type, 0)` under `BEGIN_TYPE_LOCK()` **without** stopping the
  world, even though `type_clear_flags` → `type_set_flags` carries
  `ASSERT_WORLD_STOPPED_OR_NEW_TYPE(tp)` (`:457`). Consistent, not a violation:
  that assert is `!TYPE_IS_REVEALED(tp) || types_world_is_stopped()`, and
  `_Py_TYPE_REVEALED_FLAG` is set only by `type_new()` and `PyType_FromMetaclass()`
  — never on a static builtin. Static types are permanently "not revealed", so the
  no-STW path is exactly what the invariant permits.
- **`managed_static_type_state_clear` (`:382-427`)** takes the raw
  `PyMutex_Lock(&interp->types.mutex)` at `:416` — the one genuinely
  STW-incompatible lock acquisition in the file (see §2 corollary). Finalization-only,
  single-threaded, unreachable from any STW region.

---

## 4. Summary table

| # | classification | site | status |
|---|---|---|---|
| 1 | **FIX** | `Objects/typeobject.c:6494` (`set_flags_recursive`), region opened `:6522`; helper `:776-807` | reproduced — SIGABRT (debug), `SystemError` + silent partial flag application (release, both GIL and FT) |
| 2 | **CONSIDER** | `Objects/typeobject.c:12839-12840` (`super_init_without_args`) | reproduced — SIGSEGV on `release-{gil,ft}-nojit`, SIGABRT on debug; crafted-code-object reachability only |
| 3 | **POLICY** | `Objects/typeobject.c:1572`, `:1598` (`type_set_name` / `type_set_qualname`) | measured — 4,141× multi-thread throughput collapse; `gh-145566` fast path applied to `__class__` but not here |
| 4 | **ACCEPTABLE** | `Objects/typeobject.c:7823-7828` (`object_set_class`) | documented above; 4 deny-list constructs, all defensible |
| — | **ACCEPTABLE** | 8 remaining STW regions | pure field stores; no region skips its `StartTheWorld` on any path |

## 5. Toolkit feedback (the deliverable that outlives the findings)

1. **`scan_gil_usage` produced a structural zero and must not be counted as a pass
   on this file.** Emit `rule_not_applicable` when `vocabulary_counts` sums to 0.
2. **`scan_stw_safety` misses file-local stop-the-world wrappers** — 9 of 11 regions
   here, including the one containing the FIX. Resolving one-line wrappers around
   `_PyEval_StopTheWorld`/`StartTheWorld` as region delimiters is the single highest-value
   change; the tree-wide `Objects/` denominator of 13 regions is probably a large undercount
   for the same reason.
3. **The deny-list is mis-tuned for FT-era CPython in one specific way.**
   `Py_BEGIN_CRITICAL_SECTION` inside STW is *safe* (stopped threads suspend their
   critical sections — `Python/pystate.c:2323`), while a raw `PyMutex_Lock` outside a
   critical section is a genuine deadlock. The rule should invert its intuition here.
4. **Add an "error discarded inside STW" check.** The FIX is not a Python-invoking
   call; it is an allocation whose failure is dropped by a `void` caller while the
   world is stopped. `PyErr_NoMemory` reachable inside an STW region *whose enclosing
   function returns `void`* is a cheap, high-precision rule — and it is precisely the
   inverse of the `gc_free_threading.c:2223` pattern the checker is built around.
